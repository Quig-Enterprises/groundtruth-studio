"""
backfill_hard_negatives.py — One-time backfill script.

Converts existing rejected ai_predictions into hard negative training annotations
using a three-tier strategy:

  Tier 1: Manually reviewed rejections → auto-backfill as keyframe_annotations
  Tier 2: Automated rejections that spatially align (IoU > 0.3) with a Tier 1
           rejection on the same camera → auto-backfill
  Tier 3: Remaining automated rejections → tag corrected_tags with
           needs_negative_review=true for human validation

Run:
    python3 backfill_hard_negatives.py [--dry-run]
"""

import argparse
import json
import os
import sys

os.environ.setdefault(
    'DATABASE_URL',
    'postgresql://groundtruth:bZv6QbJ8KCAQubJFb+frmbGNKUiPm7lBUg0XgMvEzNQ=@localhost:5432/groundtruth_studio'
)

from db_connection import get_connection
from psycopg2 import extras

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VEHICLE_CLASSES = {
    'sedan', 'pickup truck', 'suv', 'minivan', 'van', 'tractor', 'atv', 'utv',
    'snowmobile', 'golf cart', 'motorcycle', 'trailer', 'bus', 'semi truck',
    'dump truck', 'rowboat', 'fishing boat', 'speed boat', 'pontoon boat',
    'kayak', 'canoe', 'sailboat', 'jet ski', 'person', 'fence'
}

HUMAN_REVIEWERS = {'mobile_reviewer', 'studio_user', 'test_reviewer'}
AUTOMATED_REVIEWER = 'argus_refilter'

IOU_THRESHOLD = 0.3


# ---------------------------------------------------------------------------
# IoU helper
# ---------------------------------------------------------------------------

def compute_iou(ax, ay, aw, ah, bx, by, bw, bh):
    """Compute Intersection over Union for two axis-aligned bounding boxes."""
    a_x1, a_y1, a_x2, a_y2 = ax, ay, ax + aw, ay + ah
    b_x1, b_y1, b_x2, b_y2 = bx, by, bx + bw, by + bh

    ix1 = max(a_x1, b_x1)
    iy1 = max(a_y1, b_y1)
    ix2 = min(a_x2, b_x2)
    iy2 = min(a_y2, b_y2)

    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0

    inter = (ix2 - ix1) * (iy2 - iy1)
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def fetch_candidates(conn):
    """
    Return all rejected predictions that:
      - have an actual_class in corrected_tags
      - have no annotation yet (created_annotation_id IS NULL)
      - actual_class (lowercased) is NOT a vehicle class

    Joined with videos to get camera_id.
    """
    with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT
                p.id,
                p.video_id,
                p.reviewed_by,
                p.corrected_tags,
                p.corrected_tags->>'actual_class'  AS actual_class,
                p.bbox_x,
                p.bbox_y,
                p.bbox_width,
                p.bbox_height,
                p.timestamp,
                p.confidence,
                v.camera_id
            FROM ai_predictions p
            JOIN videos v ON v.id = p.video_id
            WHERE p.review_status = 'rejected'
              AND p.corrected_tags->>'actual_class' IS NOT NULL
              AND p.created_annotation_id IS NULL
            ORDER BY p.id
        """)
        rows = cur.fetchall()

    # Filter out vehicle classes in Python (case-insensitive)
    candidates = []
    for row in rows:
        actual = (row['actual_class'] or '').strip().lower()
        if actual not in VEHICLE_CLASSES:
            candidates.append(dict(row))
    return candidates


# ---------------------------------------------------------------------------
# Annotation writer
# ---------------------------------------------------------------------------

def create_annotation(conn, prediction, source, comment, dry_run):
    """
    Insert a keyframe_annotation and link it back to the prediction.
    Returns the new annotation id (or a fake sentinel in dry-run).
    """
    if dry_run:
        print(
            f"  [DRY-RUN] Would create annotation for prediction {prediction['id']} "
            f"(video={prediction['video_id']}, {source!r})"
        )
        return -1  # sentinel

    with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
        cur.execute("""
            INSERT INTO keyframe_annotations
                (video_id, timestamp, bbox_x, bbox_y, bbox_width, bbox_height,
                 is_negative, reviewed, source, source_prediction_id, comment)
            VALUES (%s, %s, %s, %s, %s, %s, TRUE, TRUE, %s, %s, %s)
            RETURNING id
        """, (
            prediction['video_id'],
            prediction['timestamp'],
            prediction['bbox_x'],
            prediction['bbox_y'],
            prediction['bbox_width'],
            prediction['bbox_height'],
            source,
            prediction['id'],
            comment,
        ))
        row = cur.fetchone()
        annotation_id = row['id']

        cur.execute("""
            UPDATE ai_predictions
               SET created_annotation_id = %s
             WHERE id = %s
        """, (annotation_id, prediction['id']))

    return annotation_id


# ---------------------------------------------------------------------------
# Tier 3 tagger
# ---------------------------------------------------------------------------

def tag_for_review(conn, prediction, dry_run):
    """Append needs_negative_review=true to corrected_tags."""
    if dry_run:
        print(
            f"  [DRY-RUN] Would tag prediction {prediction['id']} "
            f"with needs_negative_review=true"
        )
        return

    with conn.cursor() as cur:
        cur.execute("""
            UPDATE ai_predictions
               SET corrected_tags = corrected_tags || '{"needs_negative_review": true}'::jsonb
             WHERE id = %s
        """, (prediction['id'],))


# ---------------------------------------------------------------------------
# Main backfill logic
# ---------------------------------------------------------------------------

def run_backfill(dry_run: bool):
    print(f"=== Hard Negative Backfill {'(DRY RUN) ' if dry_run else ''}===")
    print()

    with get_connection() as conn:

        # ------------------------------------------------------------------ #
        # 1. Fetch all candidate predictions
        # ------------------------------------------------------------------ #
        print("Fetching candidate predictions...")
        candidates = fetch_candidates(conn)
        print(f"  Found {len(candidates)} candidates (rejected, non-vehicle, no annotation yet)")
        print()

        # Separate manual vs automated
        manual_preds = [
            p for p in candidates
            if p['reviewed_by'] in HUMAN_REVIEWERS
        ]
        auto_preds = [
            p for p in candidates
            if p['reviewed_by'] == AUTOMATED_REVIEWER
        ]
        other_preds = [
            p for p in candidates
            if p['reviewed_by'] not in HUMAN_REVIEWERS
            and p['reviewed_by'] != AUTOMATED_REVIEWER
        ]

        print(f"  Manual reviewers  : {len(manual_preds)}")
        print(f"  Automated (argus) : {len(auto_preds)}")
        print(f"  Other/unknown     : {len(other_preds)}")
        print()

        # ------------------------------------------------------------------ #
        # TIER 1 — Manual rejections
        # ------------------------------------------------------------------ #
        print("--- Tier 1: Manual rejections ---")
        tier1_count = 0
        # Also track their bbox info indexed by camera_id for Tier 2
        manual_by_camera = {}  # camera_id -> list of {bbox, actual_class}

        for pred in manual_preds:
            actual_class = (pred['actual_class'] or '').strip()
            comment = f"Backfill: manually rejected as {actual_class}"
            annotation_id = create_annotation(
                conn, pred,
                source='hard_negative_backfill',
                comment=comment,
                dry_run=dry_run,
            )
            if not dry_run:
                print(
                    f"  Prediction {pred['id']:>8} → annotation {annotation_id} "
                    f"[{actual_class}] camera={pred['camera_id']}"
                )
            tier1_count += 1

            # Record for Tier 2 spatial matching
            cam = pred['camera_id']
            if cam:
                manual_by_camera.setdefault(cam, []).append({
                    'bbox_x': pred['bbox_x'],
                    'bbox_y': pred['bbox_y'],
                    'bbox_width': pred['bbox_width'],
                    'bbox_height': pred['bbox_height'],
                    'actual_class': actual_class,
                })

        if not dry_run:
            conn.commit()
        print(f"  Tier 1 done: {tier1_count} annotations {'would be ' if dry_run else ''}created")
        print()

        # ------------------------------------------------------------------ #
        # TIER 2 — Automated rejections spatially aligned with Tier 1
        # ------------------------------------------------------------------ #
        print("--- Tier 2: Automated rejections aligned with manual reviews ---")
        tier2_count = 0
        tier3_candidates = []

        for pred in auto_preds:
            cam = pred['camera_id']
            manual_on_same_cam = manual_by_camera.get(cam, [])

            best_iou = 0.0
            best_actual_class = None

            px = pred['bbox_x']
            py = pred['bbox_y']
            pw = pred['bbox_width']
            ph = pred['bbox_height']

            # Guard against null bboxes
            if None in (px, py, pw, ph):
                tier3_candidates.append(pred)
                continue

            for manual in manual_on_same_cam:
                mx = manual['bbox_x']
                my = manual['bbox_y']
                mw = manual['bbox_width']
                mh = manual['bbox_height']

                if None in (mx, my, mw, mh):
                    continue

                iou = compute_iou(px, py, pw, ph, mx, my, mw, mh)
                if iou > best_iou:
                    best_iou = iou
                    best_actual_class = manual['actual_class']

            if best_iou > IOU_THRESHOLD:
                actual_class = (pred['actual_class'] or '').strip()
                comment = (
                    f"Backfill: auto-rejected as {actual_class}, "
                    f"aligned with manual review (IoU={best_iou:.2f})"
                )
                annotation_id = create_annotation(
                    conn, pred,
                    source='hard_negative_backfill_aligned',
                    comment=comment,
                    dry_run=dry_run,
                )
                if not dry_run:
                    print(
                        f"  Prediction {pred['id']:>8} → annotation {annotation_id} "
                        f"[{actual_class}] IoU={best_iou:.2f} camera={cam}"
                    )
                tier2_count += 1
            else:
                tier3_candidates.append(pred)

        # Also push other/unknown reviewers straight to Tier 3
        tier3_candidates.extend(other_preds)

        if not dry_run:
            conn.commit()
        print(f"  Tier 2 done: {tier2_count} annotations {'would be ' if dry_run else ''}created")
        print()

        # ------------------------------------------------------------------ #
        # TIER 3 — Tag remaining automated rejections for review
        # ------------------------------------------------------------------ #
        print("--- Tier 3: Tag remaining automated rejections for human review ---")
        tier3_count = 0

        for pred in tier3_candidates:
            tag_for_review(conn, pred, dry_run=dry_run)
            if not dry_run:
                actual_class = (pred['actual_class'] or '').strip()
                print(
                    f"  Prediction {pred['id']:>8} → tagged needs_negative_review "
                    f"[{actual_class}] camera={pred['camera_id']}"
                )
            tier3_count += 1

        if not dry_run:
            conn.commit()
        print(f"  Tier 3 done: {tier3_count} predictions {'would be ' if dry_run else ''}tagged")
        print()

    # ---------------------------------------------------------------------- #
    # Summary report
    # ---------------------------------------------------------------------- #
    total = tier1_count + tier2_count + tier3_count
    print("=== Hard Negative Backfill Report ===")
    print(f"Tier 1 (manual reviews): {tier1_count} annotations {'would be ' if dry_run else ''}created")
    print(f"Tier 2 (aligned with manual): {tier2_count} annotations {'would be ' if dry_run else ''}created")
    print(f"Tier 3 (queued for review): {tier3_count} predictions {'would be ' if dry_run else ''}tagged")
    print(f"Total: {total} processed")
    if dry_run:
        print()
        print("(No changes were made — this was a dry run)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Backfill hard negative training annotations from rejected predictions.'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Print what would happen without making any database changes.',
    )
    args = parser.parse_args()

    run_backfill(dry_run=args.dry_run)
