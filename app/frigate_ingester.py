"""
Frigate MQTT event-driven ingester for Groundtruth Studio.

Subscribes to Frigate's MQTT events and captures snapshots when
objects (people, vehicles, boats) are detected. Much more efficient
than time-based polling — only ingests when there's something to see.
"""

import argparse
import cv2
import json
import logging
import os
import threading
import time
from datetime import datetime
from typing import Optional, Dict

import numpy as np
import paho.mqtt.client as mqtt
import requests

from database import VideoDatabase
from vehicle_detect_runner import trigger_vehicle_detect

logger = logging.getLogger(__name__)


class FrigateEventIngester:
    """Subscribe to Frigate MQTT events and ingest snapshots on detection."""

    # Frigate event types we care about
    TRACKED_LABELS = {'person', 'car', 'truck', 'motorcycle', 'boat', 'bus'}

    # Minimum score to trigger capture
    MIN_SCORE = 0.5

    # Cooldown per camera to avoid flooding (seconds)
    CAMERA_COOLDOWN = 30

    def __init__(self, frigate_url: str = "http://localhost:5000",
                 mqtt_host: str = "127.0.0.1", mqtt_port: int = 1883,
                 topic_prefix: str = "frigate"):
        self.frigate_url = frigate_url.rstrip('/')
        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self.topic_prefix = topic_prefix
        self.thumbnail_dir = "/opt/groundtruth-studio/thumbnails"
        self.db = VideoDatabase()
        self._stop_flag = threading.Event()
        self._client = None
        self.interval = 60  # For API status reporting

        # Cooldown tracking: camera_name -> last capture timestamp
        self._last_capture = {}
        self._lock = threading.Lock()

        # Stats
        self.stats = {
            'events_received': 0,
            'snapshots_captured': 0,
            'duplicates_skipped': 0,
            'cooldown_skipped': 0,
            'errors': 0
        }

        os.makedirs(self.thumbnail_dir, exist_ok=True)

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        """Called when MQTT connection is established."""
        if reason_code == 0:
            logger.info(f"Connected to MQTT broker at {self.mqtt_host}:{self.mqtt_port}")
            # Subscribe to Frigate event topics
            # frigate/events — JSON event data for new/update/end
            client.subscribe(f"{self.topic_prefix}/events")
            logger.info(f"Subscribed to {self.topic_prefix}/events")
        else:
            logger.error(f"MQTT connection failed with code: {reason_code}")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        """Called when MQTT connection is lost."""
        if reason_code != 0:
            logger.warning(f"MQTT disconnected unexpectedly (code={reason_code}), will auto-reconnect")

    def _on_message(self, client, userdata, msg):
        """Handle incoming MQTT messages."""
        try:
            if msg.topic == f"{self.topic_prefix}/events":
                self._handle_event(msg.payload)
        except Exception as e:
            logger.error(f"Error handling MQTT message on {msg.topic}: {e}", exc_info=True)
            self.stats['errors'] += 1

    def _handle_event(self, payload: bytes):
        """Process a Frigate event message."""
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            logger.warning("Failed to decode event JSON")
            return

        self.stats['events_received'] += 1

        event_type = event.get('type')
        before = event.get('before', {})
        after = event.get('after', {})

        # Use 'after' state for current event data
        data = after if after else before
        if not data:
            return

        event_id = data.get('id', '')
        camera = data.get('camera', '')
        label = data.get('label', '')
        score = data.get('top_score', 0) or data.get('score', 0)
        has_snapshot = data.get('has_snapshot', False)

        # Only process events we care about
        if label not in self.TRACKED_LABELS:
            return

        if score < self.MIN_SCORE:
            return

        # Only capture on 'new' events (first detection) or 'end' events (best frame)
        # 'update' events fire constantly and would flood the system
        if event_type not in ('new', 'end'):
            return

        # Prefer 'end' events — they have the best snapshot after tracking completes
        # But also accept 'new' to catch things quickly
        # Use cooldown to prevent both 'new' and 'end' from same detection
        with self._lock:
            cooldown_key = f"{camera}_{event_id}"
            last = self._last_capture.get(cooldown_key, 0)
            now = time.time()

            if now - last < self.CAMERA_COOLDOWN:
                self.stats['cooldown_skipped'] += 1
                logger.debug(f"Cooldown skip: {camera} {label} (event {event_id[:8]})")
                return

            self._last_capture[cooldown_key] = now

            # Also enforce per-camera cooldown to avoid rapid-fire from busy cameras
            cam_last = self._last_capture.get(camera, 0)
            if now - cam_last < 10:  # 10s per-camera minimum
                self.stats['cooldown_skipped'] += 1
                return
            self._last_capture[camera] = now

        logger.info(f"Event: {event_type} {camera} {label} score={score:.2f} event={event_id[:8]}")

        # Capture in background to not block MQTT
        thread = threading.Thread(
            target=self._capture_event_snapshot,
            args=(camera, label, score, event_id, has_snapshot),
            daemon=True,
            name=f"capture-{event_id[:8]}"
        )
        thread.start()

    def _capture_event_snapshot(self, camera: str, label: str, score: float,
                                 event_id: str, has_snapshot: bool):
        """Fetch and ingest a snapshot for a Frigate event."""
        try:
            # Try event snapshot first (best quality, cropped to detection area)
            # Then fall back to camera latest.jpg
            image_bytes = None

            if has_snapshot:
                try:
                    resp = requests.get(
                        f"{self.frigate_url}/api/events/{event_id}/snapshot.jpg",
                        params={'crop': 0, 'quality': 95},  # Full frame, high quality
                        timeout=10
                    )
                    if resp.status_code == 200 and len(resp.content) > 1000:
                        image_bytes = resp.content
                except Exception as e:
                    logger.debug(f"Event snapshot failed, falling back to latest: {e}")

            if image_bytes is None:
                resp = requests.get(
                    f"{self.frigate_url}/api/{camera}/latest.jpg",
                    params={'quality': 95},
                    timeout=10
                )
                resp.raise_for_status()
                image_bytes = resp.content

            # Decode to get dimensions
            np_arr = np.frombuffer(image_bytes, np.uint8)
            img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
            if img is None:
                logger.warning(f"Failed to decode snapshot for {camera}")
                self.stats['errors'] += 1
                return

            height, width = img.shape[:2]

            # Generate filename
            timestamp = datetime.now()
            ts_str = timestamp.strftime("%Y%m%d_%H%M%S")
            filename = f"frigate_{camera}_{label}_{ts_str}.jpg"
            thumbnail_path = os.path.join(self.thumbnail_dir, filename)

            with open(thumbnail_path, 'wb') as f:
                f.write(image_bytes)

            # Create title
            title = f"{camera} - {label} ({score:.0%}) {timestamp.strftime('%Y-%m-%d %H:%M:%S')}"

            # Add to database
            video_id = self.db.add_video(
                filename=filename,
                title=title,
                thumbnail_path=thumbnail_path,
                width=width,
                height=height,
                file_size=len(image_bytes),
                camera_id=camera,
                notes=f"Frigate alert: {label} (score={score:.2f}, event={event_id[:12]})"
            )

            # Trigger our YOLO-World pipeline for detailed classification
            trigger_vehicle_detect(video_id, thumbnail_path, force_review=True)

            self.stats['snapshots_captured'] += 1
            logger.info(
                f"Captured: {camera} {label} score={score:.2f} -> video_id={video_id} "
                f"({width}x{height}, {len(image_bytes)//1024}KB)"
            )

        except Exception as e:
            logger.error(f"Failed to capture snapshot for {camera}: {e}", exc_info=True)
            self.stats['errors'] += 1

    def _cleanup_cooldowns(self):
        """Periodically clean up old cooldown entries to prevent memory growth."""
        while not self._stop_flag.is_set():
            self._stop_flag.wait(300)  # Every 5 minutes
            with self._lock:
                now = time.time()
                expired = [k for k, v in self._last_capture.items() if now - v > 600]
                for k in expired:
                    del self._last_capture[k]
                if expired:
                    logger.debug(f"Cleaned up {len(expired)} expired cooldown entries")

    def start(self):
        """Connect to MQTT and start processing events."""
        logger.info(f"Starting Frigate event ingester")
        logger.info(f"  MQTT: {self.mqtt_host}:{self.mqtt_port}")
        logger.info(f"  Frigate: {self.frigate_url}")
        logger.info(f"  Topic: {self.topic_prefix}/events")
        logger.info(f"  Tracked: {', '.join(sorted(self.TRACKED_LABELS))}")
        logger.info(f"  Min score: {self.MIN_SCORE}")
        logger.info(f"  Cooldown: {self.CAMERA_COOLDOWN}s per event")

        # Start cooldown cleanup thread
        cleanup_thread = threading.Thread(target=self._cleanup_cooldowns, daemon=True)
        cleanup_thread.start()

        # Set up MQTT client
        self._client = mqtt.Client(
            client_id="groundtruth-frigate-ingester",
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2
        )
        # Set credentials from environment if available
        mqtt_user = os.environ.get('MQTT_USER')
        mqtt_pass = os.environ.get('MQTT_PASS')
        if mqtt_user:
            self._client.username_pw_set(mqtt_user, mqtt_pass)
            logger.info(f"  MQTT auth: user={mqtt_user}")
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)

        try:
            self._client.connect(self.mqtt_host, self.mqtt_port, keepalive=60)
            # loop_forever handles reconnection automatically
            self._client.loop_forever()
        except KeyboardInterrupt:
            logger.info("Received KeyboardInterrupt")
        except Exception as e:
            logger.error(f"MQTT error: {e}", exc_info=True)
        finally:
            self._stop_flag.set()
            if self._client:
                self._client.disconnect()
            logger.info(
                f"Ingester stopped. Stats: {self.stats['events_received']} events, "
                f"{self.stats['snapshots_captured']} captured, "
                f"{self.stats['cooldown_skipped']} cooldown skips, "
                f"{self.stats['errors']} errors"
            )

    def get_cameras(self):
        """Get list of Frigate cameras."""
        try:
            resp = requests.get(f"{self.frigate_url}/api/config", timeout=5)
            if resp.ok:
                config = resp.json()
                return list(config.get('cameras', {}).keys())
        except Exception as e:
            logger.warning(f"Failed to get Frigate cameras: {e}")
        return []

    def run_cycle(self):
        """Run a single capture cycle (manual trigger). Returns stats dict."""
        return {
            'message': 'MQTT-based ingester processes events automatically',
            'stats': dict(self.stats),
            'running': not self._stop_flag.is_set()
        }

    def stop(self):
        """Stop the ingester."""
        logger.info("Stopping Frigate event ingester...")
        self._stop_flag.set()
        if self._client:
            self._client.disconnect()


# Module-level singleton for API integration
_ingester_instance = None
_ingester_thread = None


def get_ingester():
    """Get or create the singleton FrigateEventIngester instance."""
    global _ingester_instance
    if _ingester_instance is None:
        _ingester_instance = FrigateEventIngester()
        _ingester_instance.interval = 60  # default interval for status reporting
    return _ingester_instance


def start_background_ingester(interval=60):
    """Start the ingester in a background thread."""
    global _ingester_thread
    ingester = get_ingester()
    ingester.interval = interval

    if _ingester_thread and _ingester_thread.is_alive():
        logger.info("Ingester already running")
        return

    ingester._stop_flag.clear()
    _ingester_thread = threading.Thread(target=ingester.start, daemon=True)
    _ingester_thread.start()
    logger.info(f"Background ingester started (interval={interval}s)")


def stop_background_ingester():
    """Stop the background ingester."""
    global _ingester_thread
    ingester = get_ingester()
    ingester.stop()
    if _ingester_thread:
        _ingester_thread.join(timeout=5)
        _ingester_thread = None
    logger.info("Background ingester stopped")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    parser = argparse.ArgumentParser(description='Frigate MQTT event ingester for Groundtruth Studio')
    parser.add_argument('--mqtt-host', type=str, default='127.0.0.1', help='MQTT broker host')
    parser.add_argument('--mqtt-port', type=int, default=1883, help='MQTT broker port')
    parser.add_argument('--frigate-url', type=str, default='http://localhost:5000', help='Frigate API URL')
    parser.add_argument('--topic-prefix', type=str, default='frigate', help='Frigate MQTT topic prefix')
    parser.add_argument('--min-score', type=float, default=0.5, help='Minimum detection score')
    parser.add_argument('--cooldown', type=int, default=30, help='Per-event cooldown in seconds')

    args = parser.parse_args()

    ingester = FrigateEventIngester(
        frigate_url=args.frigate_url,
        mqtt_host=args.mqtt_host,
        mqtt_port=args.mqtt_port,
        topic_prefix=args.topic_prefix
    )
    ingester.MIN_SCORE = args.min_score
    ingester.CAMERA_COOLDOWN = args.cooldown
    ingester.start()
