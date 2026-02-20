#!/usr/bin/env python3
"""
Static Cluster Generator

Identifies predictions that belong to the same tracked object (camera_object_track)
and tags them for batch review. Falls back to same-video spatial grouping for
predictions without track IDs.

Does NOT cluster different vehicles that happen to pass through the same pixel
location (e.g., a road/lane) — only same-track or same-video+spatial groups qualify.

Cluster key format:
  track_{track_id}           — track-based cluster
  vid_{video_id}_{qx}_{qy}  — same-video spatial fallback

Usage:
    python static_cluster_generator.py
    python static_cluster_generator.py --dry-run
    python static_cluster_generator.py --min-id 2043
    python static_cluster_generator.py --camera mwcam8
"""

import os
import sys
import json
import logging
import argparse
from collections import defaultdict

os.environ.setdefault(
    'DATABASE_URL',
    'postgresql://groundtruth:bZv6QbJ8KCAQubJFb+frmbGNKUiPm7lBUg0XgMvEzNQ=@localhost:5432/groundtruth_studio'
)

from db_connection import get_cursor

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)


def quantize(val, grid=50):
    """Snap a coordinate to the nearest grid boundary (floor)."""
    return (int(val) // grid) * grid


def fetch_eligible_predictions(min_id=None, camera_id=None):
    """
    Fetch all pending/processing predictions that don't already have
    a static_cluster tag.  Returns a list of dicts.
    """
    conditions = [
        "p.review_status IN ('pending', 'processing')",
        "(p.corrected_tags IS NULL OR p.corrected_tags->>'static_cluster' IS NULL)",
        "p.bbox_x IS NOT NULL",
        "p.bbox_y IS NOT NULL",
        "p.bbox_width IS NOT NULL",
        "p.bbox_height IS NOT NULL",
    ]
    params = []

    if min_id is not None:
        conditions.append("p.id >= %s")
        params.append(min_id)

    if camera_id is not None:
        conditions.append("v.camera_id = %s")
        params.append(camera_id)

    where_clause = " AND ".join(conditions)

    query = f"""
        SELECT
            p.id,
            p.bbox_x,
            p.bbox_y,
            p.bbox_width,
            p.bbox_height,
            p.video_id,
            p.camera_object_track_id,
            v.camera_id
        FROM ai_predictions p
        JOIN videos v ON v.id = p.video_id
        WHERE {where_clause}
        ORDER BY p.id
    """

    with get_cursor(commit=False) as cur:
        cur.execute(query, params or None)
        rows = cur.fetchall()

    return [dict(r) for r in rows]


def build_clusters(predictions):
    """
    Group predictions into clusters using two strategies:

    1. Track-based: predictions sharing the same camera_object_track_id
       are the same physical object — always a valid cluster.

    2. Same-video spatial fallback: predictions without a track ID
       are grouped by (video_id, quantized bbox position). This catches
       repeated detections of the same object within a single event
       without risking cross-event false clustering.

    Returns dict: key -> list of prediction IDs.
    """
    groups = defaultdict(list)

    for row in predictions:
        track_id = row.get('camera_object_track_id')

        if track_id is not None:
            # Same track = same physical object
            key = f"track_{track_id}"
        else:
            # Fallback: same video + same spatial location
            qx = quantize(row['bbox_x'])
            qy = quantize(row['bbox_y'])
            key = f"vid_{row['video_id']}_{qx}_{qy}"

        groups[key].append(row['id'])

    return groups


def apply_clusters(clusters, dry_run=False, min_cluster_size=3):
    """
    For each cluster with >= min_cluster_size members, update corrected_tags.
    Returns (clusters_created, predictions_tagged).
    """
    clusters_created = 0
    predictions_tagged = 0

    qualifying = {k: ids for k, ids in clusters.items() if len(ids) >= min_cluster_size}

    logger.info(
        "Found %d qualifying clusters (>= %d members) out of %d total groups.",
        len(qualifying), min_cluster_size, len(clusters)
    )

    for key, ids in qualifying.items():
        count = len(ids)
        tag_payload = json.dumps({
            'static_cluster': key,
            'batch_reviewable': 'true',
            'cluster_size': str(count),
        })

        logger.info(
            "  Cluster %-45s  %4d predictions  %s",
            key, count, "[DRY RUN]" if dry_run else ""
        )

        if not dry_run:
            with get_cursor(commit=True) as cur:
                cur.execute(
                    """
                    UPDATE ai_predictions
                    SET corrected_tags = COALESCE(corrected_tags, '{}'::jsonb) || %s::jsonb
                    WHERE id = ANY(%s)
                    """,
                    (tag_payload, ids)
                )

        clusters_created += 1
        predictions_tagged += count

    return clusters_created, predictions_tagged


def parse_args():
    parser = argparse.ArgumentParser(
        description="Tag static-cluster predictions for batch review."
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help="Show what would be done without writing to the database.",
    )
    parser.add_argument(
        '--min-id',
        type=int,
        default=None,
        metavar='N',
        help="Only consider predictions with id >= N.",
    )
    parser.add_argument(
        '--camera',
        default=None,
        metavar='CAMERA_ID',
        help="Only consider predictions from this camera.",
    )
    parser.add_argument(
        '--min-cluster-size',
        type=int,
        default=3,
        metavar='N',
        help="Minimum cluster size to tag (default: 3).",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    logger.info("=== Static Cluster Generator ===")
    if args.dry_run:
        logger.info("DRY RUN mode - no changes will be written.")
    if args.min_id is not None:
        logger.info("Filtering to predictions with id >= %d", args.min_id)
    if args.camera is not None:
        logger.info("Filtering to camera: %s", args.camera)

    logger.info("Fetching eligible predictions...")
    predictions = fetch_eligible_predictions(
        min_id=args.min_id,
        camera_id=args.camera,
    )
    logger.info("Fetched %d eligible predictions.", len(predictions))

    if not predictions:
        logger.info("Nothing to do.")
        return

    clusters = build_clusters(predictions)
    logger.info(
        "Grouped into %d distinct clusters.", len(clusters)
    )

    clusters_created, predictions_tagged = apply_clusters(
        clusters,
        dry_run=args.dry_run,
        min_cluster_size=args.min_cluster_size,
    )

    action = "Would tag" if args.dry_run else "Tagged"
    logger.info(
        "Done. %s %d predictions across %d clusters.",
        action, predictions_tagged, clusters_created
    )


def run_static_clustering(min_cluster_size=3):
    """Callable entry point for pipeline worker. Returns summary dict."""
    predictions = fetch_eligible_predictions()
    if not predictions:
        return {'eligible': 0, 'clusters_created': 0, 'predictions_tagged': 0}

    clusters = build_clusters(predictions)
    clusters_created, predictions_tagged = apply_clusters(
        clusters, dry_run=False, min_cluster_size=min_cluster_size
    )
    return {
        'eligible': len(predictions),
        'clusters_created': clusters_created,
        'predictions_tagged': predictions_tagged,
    }


if __name__ == '__main__':
    main()
