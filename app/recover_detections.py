"""
recover_detections.py — Re-run vehicle detection on videos with no predictions.

Queries for videos that have no entries in ai_predictions and calls the
auto-detect HTTP API endpoint for each one so the full pipeline runs
(dedup, routing, grouping, VLM review).

Run:
    cd /opt/groundtruth-studio/app && python recover_detections.py
    cd /opt/groundtruth-studio/app && python recover_detections.py --dry-run
    cd /opt/groundtruth-studio/app && python recover_detections.py --limit 10
    cd /opt/groundtruth-studio/app && python recover_detections.py --batch-size 25
"""

import argparse
import logging
import os
import sys
import time

import requests

os.environ.setdefault(
    'DATABASE_URL',
    'postgresql://groundtruth:bZv6QbJ8KCAQubJFb+frmbGNKUiPm7lBUg0XgMvEzNQ=@localhost:5432/groundtruth_studio'
)

from db_connection import get_connection
from psycopg2 import extras
from vehicle_detect_runner import run_vehicle_detection

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_URL = 'http://localhost:5050'

INTER_REQUEST_SLEEP = 0.5   # seconds between individual requests
INTER_BATCH_SLEEP   = 5.0   # seconds between batches

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

QUERY = """
    SELECT v.id, v.thumbnail_path
    FROM videos v
    WHERE v.id NOT IN (
        SELECT DISTINCT video_id FROM ai_predictions
    )
    AND v.thumbnail_path IS NOT NULL
    AND v.id >= %s
    ORDER BY v.id
"""


def fetch_videos(conn, limit=None, min_id=1):
    """Return list of dicts with 'id' and 'thumbnail_path'."""
    with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
        cur.execute(QUERY, (min_id,))
        rows = cur.fetchall()

    videos = [dict(r) for r in rows]
    if limit is not None:
        videos = videos[:limit]
    return videos


# ---------------------------------------------------------------------------
# Detection caller
# ---------------------------------------------------------------------------

def call_detect(video_id, thumbnail_path):
    """
    Run vehicle detection directly via run_vehicle_detection().
    Returns (success: bool, detail: str).
    """
    try:
        result = run_vehicle_detection(video_id, thumbnail_path, force_review=True)
        if result is None:
            return False, 'Detection returned None (model not available or no detections)'
        return True, ''
    except Exception as exc:
        return False, str(exc)[:200]


# ---------------------------------------------------------------------------
# Main processing loop
# ---------------------------------------------------------------------------

def run(dry_run: bool, limit, batch_size: int, min_id: int = 1):
    log.info("=== recover_detections.py%s ===", " (DRY RUN)" if dry_run else "")

    with get_connection() as conn:
        log.info("Querying for videos with no predictions...")
        videos = fetch_videos(conn, limit=limit, min_id=min_id)

    total_found = len(videos)
    log.info("Found %d video(s) with no predictions%s",
             total_found,
             f" (showing first {limit})" if limit and limit < total_found else "")

    if dry_run:
        log.info("[DRY RUN] Would process %d video(s). No requests sent.", total_found)
        # Still check thumbnails so the user knows what would be skipped.
        missing = sum(1 for v in videos if not os.path.exists(v['thumbnail_path']))
        log.info("[DRY RUN] Thumbnails missing on disk: %d", missing)
        log.info("[DRY RUN] Thumbnails present (would be processed): %d", total_found - missing)
        return

    if total_found == 0:
        log.info("Nothing to do.")
        return

    # Counters
    success_count  = 0
    failed_count   = 0
    skipped_count  = 0   # thumbnail missing on disk
    processed      = 0

    start_time = time.time()

    for batch_start in range(0, total_found, batch_size):
        batch = videos[batch_start:batch_start + batch_size]
        batch_num  = batch_start // batch_size + 1
        batch_total = (total_found + batch_size - 1) // batch_size
        log.info("--- Batch %d/%d (videos %d-%d of %d) ---",
                 batch_num, batch_total,
                 batch_start + 1,
                 min(batch_start + batch_size, total_found),
                 total_found)

        for video in batch:
            video_id     = video['id']
            thumb_path   = video['thumbnail_path']

            # Check thumbnail exists on disk
            if not os.path.exists(thumb_path):
                log.warning("SKIP video_id=%s — thumbnail not found on disk: %s",
                            video_id, thumb_path)
                skipped_count += 1
                processed += 1
                continue

            # Run vehicle detection directly
            ok, detail = call_detect(video_id, thumb_path)
            processed += 1

            if ok:
                success_count += 1
                log.debug("OK    video_id=%s", video_id)
            else:
                failed_count += 1
                log.warning("FAIL  video_id=%s  detail=%s", video_id, detail)

            # Log progress every 50 videos
            if processed % 50 == 0:
                elapsed = time.time() - start_time
                rate    = processed / elapsed if elapsed > 0 else 0
                log.info("[%d/%d] success=%d failed=%d skipped=%d  (%.1f/s)",
                         processed, total_found,
                         success_count, failed_count, skipped_count, rate)

            # Throttle between requests
            time.sleep(INTER_REQUEST_SLEEP)

        # Pause between batches (unless this was the last one)
        if batch_start + batch_size < total_found:
            log.info("Batch %d/%d complete — pausing %.1fs before next batch...",
                     batch_num, batch_total, INTER_BATCH_SLEEP)
            time.sleep(INTER_BATCH_SLEEP)

    # ---------------------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------------------
    elapsed = time.time() - start_time
    log.info("")
    log.info("=== Summary ===")
    log.info("Total videos found (no predictions): %d", total_found)
    log.info("Processed:                           %d", processed)
    log.info("  Success:                           %d", success_count)
    log.info("  Failed:                            %d", failed_count)
    log.info("  Skipped (thumbnail missing):       %d", skipped_count)
    log.info("Elapsed:                             %.1fs", elapsed)
    if processed > 0:
        log.info("Average rate:                        %.2f videos/s",
                 processed / elapsed if elapsed > 0 else 0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Re-run vehicle detection on videos that have no predictions.'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Count videos and check thumbnails without sending any API requests.',
    )
    parser.add_argument(
        '--limit',
        type=int,
        default=None,
        metavar='N',
        help='Process only the first N videos (useful for testing).',
    )
    parser.add_argument(
        '--min-id',
        type=int,
        default=1,
        metavar='N',
        help='Only process videos with id >= N (default: %(default)s).',
    )
    parser.add_argument(
        '--batch-size',
        type=int,
        default=50,
        metavar='N',
        help='Number of videos per batch; a %(default)ss pause is inserted between batches (default: %(default)s).',
    )
    args = parser.parse_args()

    run(dry_run=args.dry_run, limit=args.limit, batch_size=args.batch_size, min_id=args.min_id)
