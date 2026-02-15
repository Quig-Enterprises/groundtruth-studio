"""
Frigate snapshot ingester for Groundtruth Studio.

Periodically fetches snapshots from Frigate cameras and ingests them
into Groundtruth Studio for auto-detection.
"""

import argparse
import cv2
import hashlib
import logging
import os
import threading
import time
from datetime import datetime
from typing import Optional, Dict, List

import numpy as np
import requests

# These imports work because the app directory is on sys.path
from database import VideoDatabase
from vehicle_detect_runner import trigger_vehicle_detect

logger = logging.getLogger(__name__)


class FrigateIngester:
    """Periodically capture snapshots from Frigate cameras and ingest into GT Studio."""

    def __init__(self, frigate_url: str = "http://localhost:5000", interval: int = 60):
        """
        Initialize FrigateIngester.

        Args:
            frigate_url: Base URL for Frigate API
            interval: Polling interval in seconds (default 60)
        """
        self.frigate_url = frigate_url.rstrip('/')
        self.interval = interval
        self.thumbnail_dir = "/opt/groundtruth-studio/thumbnails"
        self.db = VideoDatabase()
        self._last_hashes = {}  # camera_name -> hash of last captured frame
        self._stop_flag = threading.Event()

        # Ensure thumbnail directory exists
        os.makedirs(self.thumbnail_dir, exist_ok=True)

    def get_cameras(self) -> List[str]:
        """
        Fetch list of enabled cameras from Frigate.

        Returns:
            List of enabled camera names
        """
        try:
            response = requests.get(f"{self.frigate_url}/api/config", timeout=10)
            response.raise_for_status()
            config = response.json()

            cameras = []
            for camera_name, camera_config in config.get('cameras', {}).items():
                if camera_config.get('enabled', True):  # Default to enabled if not specified
                    cameras.append(camera_name)

            logger.debug(f"Found {len(cameras)} enabled cameras: {cameras}")
            return cameras

        except Exception as e:
            logger.error(f"Failed to fetch camera list from Frigate: {e}")
            return []

    def capture_snapshot(self, camera_name: str) -> Optional[Dict]:
        """
        Capture a snapshot from a Frigate camera.

        Args:
            camera_name: Name of the camera

        Returns:
            Dict with video_id and filename if new snapshot captured, None if duplicate/error
        """
        try:
            # Fetch latest snapshot
            response = requests.get(
                f"{self.frigate_url}/api/{camera_name}/latest.jpg",
                timeout=10
            )
            response.raise_for_status()
            image_bytes = response.content

            # Calculate hash of first 4KB to detect duplicates
            hash_bytes = image_bytes[:4096]
            current_hash = hashlib.md5(hash_bytes).hexdigest()

            # Check if this is a duplicate
            last_hash = self._last_hashes.get(camera_name)
            if last_hash == current_hash:
                logger.debug(f"Skipping duplicate frame for camera {camera_name}")
                return None

            # Decode image to get dimensions
            np_arr = np.frombuffer(image_bytes, np.uint8)
            img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)

            if img is None:
                logger.warning(f"Failed to decode image from camera {camera_name}")
                return None

            height, width = img.shape[:2]

            # Generate filename with timestamp
            timestamp = datetime.now()
            timestamp_str = timestamp.strftime("%Y%m%d_%H%M%S")
            filename = f"frigate_{camera_name}_{timestamp_str}.jpg"
            thumbnail_path = os.path.join(self.thumbnail_dir, filename)

            # Save the image
            with open(thumbnail_path, 'wb') as f:
                f.write(image_bytes)

            # Create title with readable timestamp
            title = f"Frigate {camera_name} {timestamp.strftime('%Y-%m-%d %H:%M:%S')}"

            # Add to database
            video_id = self.db.add_video(
                filename=filename,
                title=title,
                thumbnail_path=thumbnail_path,
                width=width,
                height=height,
                file_size=len(image_bytes),
                camera_id=camera_name
            )

            # Update hash cache
            self._last_hashes[camera_name] = current_hash

            # YOLO-World pre-screen: detects vehicles + gates person-face-v1
            trigger_vehicle_detect(video_id, thumbnail_path, force_review=True)

            logger.info(
                f"Captured snapshot from {camera_name}: {filename} "
                f"(video_id={video_id}, {width}x{height}, {len(image_bytes)} bytes)"
            )

            return {
                'video_id': video_id,
                'filename': filename,
                'camera': camera_name,
                'width': width,
                'height': height,
                'size': len(image_bytes)
            }

        except requests.exceptions.RequestException as e:
            logger.warning(f"Failed to fetch snapshot from camera {camera_name}: {e}")
            return None
        except Exception as e:
            logger.error(f"Error capturing snapshot from camera {camera_name}: {e}", exc_info=True)
            return None

    def run_cycle(self) -> Dict:
        """
        Run one capture cycle across all cameras.

        Returns:
            Summary dict with cameras_checked, new_snapshots, skipped_duplicates, errors
        """
        cameras = self.get_cameras()

        summary = {
            'cameras_checked': len(cameras),
            'new_snapshots': 0,
            'skipped_duplicates': 0,
            'errors': 0
        }

        for camera_name in cameras:
            result = self.capture_snapshot(camera_name)

            if result is None:
                # Could be duplicate or error - check if we have a hash for this camera
                if camera_name in self._last_hashes:
                    summary['skipped_duplicates'] += 1
                else:
                    summary['errors'] += 1
            else:
                summary['new_snapshots'] += 1

        logger.info(
            f"Cycle complete: {summary['cameras_checked']} cameras checked, "
            f"{summary['new_snapshots']} new snapshots, "
            f"{summary['skipped_duplicates']} duplicates skipped, "
            f"{summary['errors']} errors"
        )

        return summary

    def start(self):
        """
        Run capture cycles in a loop with interval sleep between them.

        Handles KeyboardInterrupt gracefully.
        """
        logger.info(f"Starting Frigate ingester (interval: {self.interval}s)")
        logger.info(f"Frigate URL: {self.frigate_url}")
        logger.info(f"Thumbnail directory: {self.thumbnail_dir}")

        try:
            while not self._stop_flag.is_set():
                cycle_start = time.time()

                self.run_cycle()

                # Calculate sleep time to maintain consistent interval
                cycle_duration = time.time() - cycle_start
                sleep_time = max(0, self.interval - cycle_duration)

                if sleep_time > 0:
                    logger.debug(f"Sleeping for {sleep_time:.1f}s until next cycle")
                    self._stop_flag.wait(sleep_time)
                else:
                    logger.warning(
                        f"Cycle took {cycle_duration:.1f}s, longer than interval {self.interval}s"
                    )

        except KeyboardInterrupt:
            logger.info("Received KeyboardInterrupt, stopping ingester")
        finally:
            logger.info("Frigate ingester stopped")

    def stop(self):
        """Stop the ingester loop."""
        logger.info("Stopping Frigate ingester...")
        self._stop_flag.set()


# Singleton pattern for background ingester
_ingester_instance = None
_ingester_thread = None
_ingester_lock = threading.Lock()


def get_ingester() -> FrigateIngester:
    """Get or create singleton FrigateIngester instance."""
    global _ingester_instance

    with _ingester_lock:
        if _ingester_instance is None:
            _ingester_instance = FrigateIngester()
        return _ingester_instance


def start_background_ingester(interval: int = 60):
    """
    Start ingester in a daemon thread.

    Args:
        interval: Polling interval in seconds
    """
    global _ingester_instance, _ingester_thread

    with _ingester_lock:
        if _ingester_thread is not None and _ingester_thread.is_alive():
            logger.warning("Background ingester already running")
            return

        _ingester_instance = FrigateIngester(interval=interval)
        _ingester_thread = threading.Thread(
            target=_ingester_instance.start,
            daemon=True,
            name="frigate-ingester"
        )
        _ingester_thread.start()
        logger.info(f"Background ingester started with interval={interval}s")


def stop_background_ingester():
    """Stop the running background ingester."""
    global _ingester_instance

    with _ingester_lock:
        if _ingester_instance is not None:
            _ingester_instance.stop()
            _ingester_instance = None
            logger.info("Background ingester stopped")


if __name__ == "__main__":
    # Set up logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # Parse command line arguments
    parser = argparse.ArgumentParser(description='Frigate snapshot ingester for Groundtruth Studio')
    parser.add_argument(
        '--interval',
        type=int,
        default=60,
        help='Polling interval in seconds (default: 60)'
    )
    parser.add_argument(
        '--frigate-url',
        type=str,
        default='http://localhost:5000',
        help='Frigate API base URL (default: http://localhost:5000)'
    )

    args = parser.parse_args()

    # Create and run ingester
    ingester = FrigateIngester(frigate_url=args.frigate_url, interval=args.interval)
    ingester.start()
