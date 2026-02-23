#!/usr/bin/env python3
"""
Backfill Clip Tracks — Process existing Frigate events with clips

Runs clip_tracker.run_clip_tracking() on existing videos that have
Frigate clips but no entries in video_tracks yet. After backfilling
tracks, optionally runs cross-camera matching on the new video tracks.

Usage:
    python backfill_clip_tracks.py [--dry-run] [--limit N] [--camera CAMERA_ID] [--match]

Flags:
    --dry-run       Show what would be processed without actually running
    --limit N       Process at most N videos (default: all)
    --camera ID     Only process videos from this camera
    --match         Run cross-camera matching after backfill
"""

import argparse
import logging
import os
import sys
import time

# Add app directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db_connection import init_connection_pool, get_cursor, close_connection_pool

logger = logging.getLogger('backfill_clip_tracks')


def find_backfill_candidates(camera_id: str = None, limit: int = None):
    """Find videos with Frigate clips that don't have video_tracks yet.

    Returns list of dicts with video_id, camera_id, frigate_event_id, filename.
    """
    query = """
        SELECT v.id as video_id,
               v.camera_id,
               v.filename,
               v.metadata->>'frigate_event_id' as frigate_event_id,
               v.metadata->>'frigate_camera' as frigate_camera
        FROM videos v
        WHERE v.metadata->>'frigate_event_id' IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM video_tracks vt WHERE vt.video_id = v.id
          )
    """
    params = []

    if camera_id:
        query += " AND (v.camera_id = %s OR v.metadata->>'frigate_camera' = %s)"
        params.extend([camera_id, camera_id])

    query += " ORDER BY v.id DESC"

    if limit:
        query += " LIMIT %s"
        params.append(limit)

    with get_cursor(commit=False) as cursor:
        cursor.execute(query, params)
        return cursor.fetchall()


def run_backfill(dry_run: bool = False, limit: int = None,
                 camera_id: str = None, run_match: bool = False):
    """Run the backfill process.

    Args:
        dry_run: If True, only show what would be processed
        limit: Max videos to process
        camera_id: Filter to specific camera
        run_match: Run cross-camera matching after backfill
    """
    candidates = find_backfill_candidates(camera_id, limit)

    if not candidates:
        logger.info("No videos found needing clip track backfill")
        return {'processed': 0, 'skipped': 0, 'failed': 0}

    logger.info("Found %d videos for clip track backfill", len(candidates))

    if dry_run:
        logger.info("DRY RUN — would process:")
        for c in candidates:
            cam = c.get('frigate_camera') or c.get('camera_id') or 'unknown'
            logger.info("  video_id=%d camera=%s event=%s file=%s",
                        c['video_id'], cam,
                        c.get('frigate_event_id', '?'),
                        c.get('filename', '?'))
        return {
            'processed': 0,
            'would_process': len(candidates),
            'dry_run': True
        }

    from clip_tracker import run_clip_tracking

    processed = 0
    skipped = 0
    failed = 0
    total_tracks = 0
    total_embeddings = 0

    for i, candidate in enumerate(candidates):
        video_id = candidate['video_id']
        cam = candidate.get('frigate_camera') or candidate.get('camera_id') or ''
        frigate_event_id = candidate.get('frigate_event_id')

        logger.info("[%d/%d] Processing video_id=%d camera=%s event=%s",
                    i + 1, len(candidates), video_id, cam, frigate_event_id or '?')

        try:
            result = run_clip_tracking(
                video_id=video_id,
                camera_id=cam,
                frigate_event_id=frigate_event_id,
            )

            if result is None:
                logger.warning("  Failed (no clip or error)")
                failed += 1
            elif result.get('skipped'):
                logger.info("  Skipped (already has tracks)")
                skipped += 1
            else:
                tracks = result.get('tracks_created', 0)
                embeds = result.get('embeddings_generated', 0)
                total_tracks += tracks
                total_embeddings += embeds
                processed += 1
                logger.info("  Created %d tracks, %d embeddings", tracks, embeds)

        except Exception as e:
            logger.error("  Error processing video %d: %s", video_id, e, exc_info=True)
            failed += 1

        # Brief pause to avoid overwhelming the system
        time.sleep(0.5)

    summary = {
        'processed': processed,
        'skipped': skipped,
        'failed': failed,
        'total_tracks': total_tracks,
        'total_embeddings': total_embeddings,
    }

    logger.info("Backfill complete: %s", summary)

    # Optionally run cross-camera matching
    if run_match and total_tracks > 0:
        logger.info("Running cross-camera matching on video tracks...")
        try:
            from cross_camera_matcher import CrossCameraMatcher
            matcher = CrossCameraMatcher()
            match_result = matcher.match_video_tracks()
            if match_result:
                summary['match_result'] = match_result
                logger.info("Cross-camera matching: %s", match_result)
        except Exception as e:
            logger.error("Cross-camera matching failed: %s", e, exc_info=True)
            summary['match_error'] = str(e)

    return summary


def main():
    parser = argparse.ArgumentParser(
        description='Backfill clip tracks for existing Frigate events'
    )
    parser.add_argument('--dry-run', action='store_true',
                        help='Show what would be processed without running')
    parser.add_argument('--limit', type=int, default=None,
                        help='Maximum number of videos to process')
    parser.add_argument('--camera', type=str, default=None,
                        help='Only process videos from this camera')
    parser.add_argument('--match', action='store_true',
                        help='Run cross-camera matching after backfill')

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    )

    init_connection_pool(min_conn=2, max_conn=5)

    try:
        result = run_backfill(
            dry_run=args.dry_run,
            limit=args.limit,
            camera_id=args.camera,
            run_match=args.match,
        )
        print(f"\nResult: {result}")
    finally:
        close_connection_pool()


if __name__ == '__main__':
    main()
