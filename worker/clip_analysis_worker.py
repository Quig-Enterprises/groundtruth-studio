#!/usr/bin/env python3
"""
Clip Analysis Worker — CLI entry point for remote clip analysis.

Called by training_worker.py when processing a 'clip_analysis' job.
Invokes the existing run_clip_analysis() function directly, keeping
all analysis logic in app/clip_analysis.py.

Usage:
    python3 clip_analysis_worker.py --video-id 42 --camera-id mwcam8 --clip-path /path/to/clip.mp4
    python3 clip_analysis_worker.py --video-id 42 --camera-id mwcam8 --clip-path /path/to/clip.mp4 --studio-url http://studio:5050
"""

import argparse
import logging
import sys
import os

# Ensure app modules are importable
APP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'app')
if APP_DIR not in sys.path:
    sys.path.insert(0, os.path.abspath(APP_DIR))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('clip-analysis-worker')


def main():
    parser = argparse.ArgumentParser(description='Run clip analysis for a video')
    parser.add_argument('--video-id', type=int, required=True, help='Database video ID')
    parser.add_argument('--camera-id', type=str, required=True, help='Camera identifier')
    parser.add_argument('--clip-path', type=str, required=True, help='Path to MP4 clip file')
    parser.add_argument('--studio-url', type=str, default=None,
                        help='Studio API URL (not used directly, but passed by worker)')
    parser.add_argument('--verbose', '-v', action='store_true', help='Debug logging')

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    video_id = args.video_id
    camera_id = args.camera_id
    clip_path = args.clip_path

    # Validate clip exists
    if not os.path.exists(clip_path):
        logger.error('Clip file not found: %s', clip_path)
        sys.exit(1)

    logger.info('Starting clip analysis: video_id=%d, camera_id=%s, clip=%s',
                video_id, camera_id, clip_path)

    try:
        from clip_analysis import run_clip_analysis

        result = run_clip_analysis(video_id, camera_id, clip_path)

        if result:
            logger.info('Clip analysis completed successfully: %d results created', len(result))
            logger.info('Analysis result IDs: %s', result)
            print(f'SUCCESS: {len(result)} analysis results created for video {video_id}')
            sys.exit(0)
        else:
            logger.error('Clip analysis returned no results for video %d', video_id)
            print(f'FAILURE: No analysis results for video {video_id}')
            sys.exit(1)

    except Exception as e:
        logger.error('Clip analysis failed: %s', e, exc_info=True)
        print(f'FAILURE: {e}')
        sys.exit(1)


if __name__ == '__main__':
    main()
