"""
EcoEye Automatic Alert Sync Module

Monitors the EcoEye alert feed and automatically syncs new alerts,
downloads associated video clips, processes them through the vehicle
detection and clip tracking pipelines, and cleans up expired files.
Runs as a background daemon thread, polling every 5 minutes.

Defaults:
    Poll interval: 300 seconds (5 minutes)
    Retention: 24 hours
    Max downloads per cycle: 10
    Min free disk: 500 MB
"""

import logging
import shutil
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional

from db_connection import get_connection

logger = logging.getLogger(__name__)

# Poll interval in seconds (5 minutes)
DEFAULT_POLL_INTERVAL = 300

# How long to keep downloaded alert videos before cleanup
DEFAULT_RETENTION_HOURS = 24

# Cap downloads per cycle to avoid GPU overload
MAX_DOWNLOADS_PER_CYCLE = 10

# Skip downloads when disk is too full
MIN_DISK_FREE_MB = 500

# Module-level singleton state
_sync_instance: Optional["EcoEyeAutoSync"] = None
_sync_thread: Optional[threading.Thread] = None
_stop_event: Optional[threading.Event] = None


class EcoEyeAutoSync:
    """
    Synchronises EcoEye alerts to the local database, downloads video
    clips for new alerts, feeds them into vehicle detection and clip
    tracking, and removes expired files after the retention window.
    """

    def __init__(self, db, processor, ecoeye_client, download_dir, thumbnail_dir, sync_config):
        """
        Args:
            db: VideoDatabase instance.
            processor: Video processor instance (thumbnail extraction, etc.).
            ecoeye_client: EcoEyeSyncClient instance for API communication.
            download_dir: Path where downloaded alert videos are stored.
            thumbnail_dir: Path where extracted thumbnails are stored.
            sync_config: SyncConfig instance for persistent key/value settings.
        """
        self.db = db
        self.processor = processor
        self.ecoeye_client = ecoeye_client
        self.download_dir = Path(download_dir)
        self.thumbnail_dir = Path(thumbnail_dir)
        self.sync_config = sync_config
        self._enabled = sync_config.get_config('ecoeye_auto_sync.enabled', 'true') == 'true'
        self._last_cycle_result = None

    # ------------------------------------------------------------------
    # Main orchestration
    # ------------------------------------------------------------------

    def run_cycle(self) -> Dict:
        """
        Execute one full sync cycle: pull alerts, download videos,
        clean up expired files.

        Returns:
            Dict describing the actions taken during this cycle.
        """
        if not self._enabled:
            logger.debug("EcoEye auto-sync is disabled, skipping cycle")
            return {"action": "skipped", "reason": "disabled"}

        sync_result = self._sync_new_alerts()
        download_result = self._download_and_process()
        cleanup_result = self._cleanup_expired()

        self.sync_config.set_config('ecoeye_auto_sync.last_sync_time', datetime.now().isoformat())

        result = {
            "action": "completed",
            "sync": sync_result,
            "download": download_result,
            "cleanup": cleanup_result,
        }
        self._last_cycle_result = result
        return result

    # ------------------------------------------------------------------
    # Alert sync
    # ------------------------------------------------------------------

    def _sync_new_alerts(self) -> Dict:
        """
        Pull new alerts from the EcoEye API into the local database.
        """
        try:
            result = self.ecoeye_client.sync_alerts_to_database()
            logger.info("EcoEye alert sync result: %s", result)
            return result
        except Exception:
            logger.warning("Failed to sync EcoEye alerts", exc_info=True)
            return {"error": "sync_alerts_failed"}

    # ------------------------------------------------------------------
    # Download and process
    # ------------------------------------------------------------------

    def _download_and_process(self) -> Dict:
        """
        Download video clips for alerts that have video available but
        not yet downloaded, then feed each through vehicle detection
        and clip tracking.
        """
        # Check disk space first
        disk = shutil.disk_usage(str(self.download_dir))
        free_mb = disk.free / (1024 * 1024)
        if free_mb < MIN_DISK_FREE_MB:
            logger.warning(
                "EcoEye auto-sync: disk too full (%.0f MB free, need %d MB) - skipping downloads",
                free_mb, MIN_DISK_FREE_MB,
            )
            return {"downloaded": 0, "failed": 0, "skipped_disk": True}

        # Fetch pending alerts
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT alert_id, camera_id, timestamp FROM ecoeye_alerts "
                "WHERE video_available = true AND video_downloaded = false "
                "ORDER BY timestamp DESC LIMIT %s",
                (MAX_DOWNLOADS_PER_CYCLE,),
            )
            rows = cursor.fetchall()

        downloaded = 0
        failed = 0

        for row in rows:
            alert_id = row[0]
            camera_id = row[1]
            # row[2] is timestamp, not used directly in download logic

            try:
                # Download the video clip
                success, filepath = self.ecoeye_client.download_alert_video(alert_id)
                if not success:
                    logger.warning("EcoEye download failed for alert %s", alert_id)
                    failed += 1
                    continue

                # Mark as downloaded in DB
                with get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "UPDATE ecoeye_alerts SET video_downloaded = true, "
                        "local_video_path = %s, downloaded_at = NOW() WHERE alert_id = %s",
                        (filepath, alert_id),
                    )
                    conn.commit()

                # Acknowledge the download with EcoEye
                self.ecoeye_client.acknowledge_download(alert_id)

                # Register the video in our database
                video_id = self.db.add_video(
                    filename=Path(filepath).name,
                    original_url=f'ecoeye://{alert_id}',
                    title=f'{camera_id or "unknown"} - ecoeye alert',
                    camera_id=camera_id,
                )

                # Extract thumbnail
                thumb = self.processor.extract_thumbnail(filepath)
                if thumb.get('success'):
                    self.db.update_video(video_id, thumbnail_path=thumb['thumbnail_path'])

                    # Trigger vehicle detection (requires thumbnail)
                    from vehicle_detect_runner import trigger_vehicle_detect
                    trigger_vehicle_detect(video_id, thumb['thumbnail_path'])

                # Kick off clip tracking in a background thread
                from clip_tracker import run_clip_tracking
                threading.Thread(
                    target=run_clip_tracking,
                    args=(video_id, camera_id or ''),
                    kwargs={'clip_path': filepath},
                    daemon=True,
                    name=f'clip-track-ecoeye-{alert_id}',
                ).start()

                downloaded += 1
                logger.info(
                    "EcoEye alert %s: downloaded and processing (video_id=%s)",
                    alert_id, video_id,
                )

            except Exception:
                logger.warning(
                    "EcoEye auto-sync: error processing alert %s", alert_id,
                    exc_info=True,
                )
                failed += 1

        return {"downloaded": downloaded, "failed": failed, "skipped_disk": False}

    # ------------------------------------------------------------------
    # Cleanup expired
    # ------------------------------------------------------------------

    def _cleanup_expired(self) -> Dict:
        """
        Remove downloaded video files that have exceeded the retention
        window. Database records and thumbnails are kept for history.
        """
        retention_hours = int(
            self.sync_config.get_config('ecoeye_auto_sync.retention_hours', str(DEFAULT_RETENTION_HOURS))
        )
        cutoff = datetime.now() - timedelta(hours=retention_hours)

        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT alert_id, local_video_path FROM ecoeye_alerts "
                "WHERE video_downloaded = true AND COALESCE(downloaded_at, timestamp) < %s",
                (cutoff,),
            )
            rows = cursor.fetchall()

        cleaned = 0
        for row in rows:
            alert_id = row[0]
            local_video_path = row[1]

            try:
                # Delete the local video file
                if local_video_path:
                    Path(local_video_path).unlink(missing_ok=True)

                # Also remove from download_dir by filename if present
                if local_video_path:
                    download_copy = self.download_dir / Path(local_video_path).name
                    download_copy.unlink(missing_ok=True)

                cleaned += 1
                logger.debug("EcoEye cleanup: removed expired alert %s video", alert_id)

            except Exception:
                logger.warning(
                    "EcoEye cleanup: error removing files for alert %s", alert_id,
                    exc_info=True,
                )

        # Second pass: clean up exporter-upgraded EcoEye videos past retention
        upgraded_cleaned = 0
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, filename FROM videos
                WHERE original_url LIKE 'ecoeye://%%'
                  AND filename NOT LIKE '%%.placeholder'
                  AND (metadata->>'upgraded_at') IS NOT NULL
                  AND (metadata->>'upgraded_at')::timestamp < %s
                  AND COALESCE((metadata->>'retain')::boolean, false) = false
            """, (cutoff,))
            upgraded_rows = cursor.fetchall()

        for row in upgraded_rows:
            vid_id = row[0]
            filename = row[1]

            try:
                # Delete the downloaded video file
                video_file = self.download_dir / filename
                video_file.unlink(missing_ok=True)

                # Revert to placeholder state
                with get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT original_url FROM videos WHERE id = %s", (vid_id,)
                    )
                    url_row = cursor.fetchone()
                    if url_row and url_row[0]:
                        event_id = url_row[0].replace('ecoeye://', '', 1)
                        placeholder_name = f'ecoeye_metadata_{event_id}.placeholder'
                        cursor.execute(
                            "UPDATE videos SET filename = %s, "
                            "metadata = COALESCE(metadata, '{}'::jsonb) - 'upgraded_at' - 'upgraded_by' "
                            "WHERE id = %s",
                            (placeholder_name, vid_id)
                        )
                        conn.commit()

                upgraded_cleaned += 1
                logger.debug("EcoEye cleanup: reverted upgraded video %s to placeholder", vid_id)

            except Exception:
                logger.warning(
                    "EcoEye cleanup: error reverting upgraded video %s", vid_id,
                    exc_info=True,
                )

        return {"cleaned": cleaned, "upgraded_cleaned": upgraded_cleaned}

    # ------------------------------------------------------------------
    # Status reporting
    # ------------------------------------------------------------------

    def get_status(self) -> Dict:
        """
        Returns current EcoEye auto-sync status for API consumption.
        """
        try:
            with get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT COUNT(*) FROM ecoeye_alerts "
                    "WHERE video_available = true AND video_downloaded = false"
                )
                pending_row = cursor.fetchone()
                pending_downloads = pending_row[0] if pending_row else 0

            return {
                "enabled": self._enabled,
                "poll_interval": DEFAULT_POLL_INTERVAL,
                "retention_hours": int(
                    self.sync_config.get_config(
                        'ecoeye_auto_sync.retention_hours', str(DEFAULT_RETENTION_HOURS)
                    )
                ),
                "last_sync_time": self.sync_config.get_config('ecoeye_auto_sync.last_sync_time'),
                "last_cycle_result": self._last_cycle_result,
                "pending_downloads": pending_downloads,
            }
        except Exception:
            logger.exception("Error getting EcoEye auto-sync status")
            return {
                "enabled": self._enabled,
                "poll_interval": DEFAULT_POLL_INTERVAL,
                "retention_hours": DEFAULT_RETENTION_HOURS,
                "last_sync_time": None,
                "last_cycle_result": self._last_cycle_result,
                "pending_downloads": None,
                "error": "Failed to query status",
            }

    # ------------------------------------------------------------------
    # Enable / disable
    # ------------------------------------------------------------------

    def enable(self):
        """Enable automatic EcoEye sync."""
        self._enabled = True
        self.sync_config.set_config('ecoeye_auto_sync.enabled', 'true')
        logger.info("EcoEye auto-sync enabled")

    def disable(self):
        """Disable automatic EcoEye sync."""
        self._enabled = False
        self.sync_config.set_config('ecoeye_auto_sync.enabled', 'false')
        logger.info("EcoEye auto-sync disabled")


# ======================================================================
# Background thread and singleton management
# ======================================================================

def _background_loop(syncer: EcoEyeAutoSync, stop_event: threading.Event):
    """
    Background loop that runs run_cycle() every DEFAULT_POLL_INTERVAL seconds.
    Catches all exceptions to prevent the thread from dying.
    """
    logger.info(
        "EcoEye auto-sync background loop started (interval=%ds)",
        DEFAULT_POLL_INTERVAL,
    )

    while not stop_event.is_set():
        try:
            result = syncer.run_cycle()
            logger.debug("EcoEye auto-sync cycle result: %s", result)
        except Exception:
            logger.exception("Unhandled exception in EcoEye auto-sync background loop")

        # Wait for the interval or until stop is requested
        stop_event.wait(timeout=DEFAULT_POLL_INTERVAL)

    logger.info("EcoEye auto-sync background loop stopped")


def start_ecoeye_auto_sync(db, processor, ecoeye_client, download_dir, thumbnail_dir, sync_config) -> EcoEyeAutoSync:
    """
    Start the background EcoEye auto-sync as a daemon thread.

    Args:
        db: VideoDatabase instance.
        processor: Video processor instance.
        ecoeye_client: EcoEyeSyncClient instance.
        download_dir: Path where downloaded alert videos are stored.
        thumbnail_dir: Path where extracted thumbnails are stored.
        sync_config: SyncConfig instance for persistent settings.

    Returns:
        The EcoEyeAutoSync instance.
    """
    global _sync_instance, _sync_thread, _stop_event

    if _sync_thread is not None and _sync_thread.is_alive():
        logger.warning("EcoEye auto-sync is already running")
        return _sync_instance

    _sync_instance = EcoEyeAutoSync(db, processor, ecoeye_client, download_dir, thumbnail_dir, sync_config)
    _stop_event = threading.Event()

    _sync_thread = threading.Thread(
        target=_background_loop,
        args=(_sync_instance, _stop_event),
        daemon=True,
        name="ecoeye-auto-sync",
    )
    _sync_thread.start()

    logger.info("EcoEye auto-sync background thread started")
    return _sync_instance


def stop_ecoeye_auto_sync():
    """
    Stop the background EcoEye auto-sync thread gracefully.
    """
    global _sync_thread, _stop_event

    if _stop_event is None or _sync_thread is None:
        logger.debug("EcoEye auto-sync is not running, nothing to stop")
        return

    logger.info("Stopping EcoEye auto-sync background thread...")
    _stop_event.set()
    _sync_thread.join(timeout=10)

    if _sync_thread.is_alive():
        logger.warning("EcoEye auto-sync thread did not stop within timeout")
    else:
        logger.info("EcoEye auto-sync thread stopped cleanly")

    _sync_thread = None
    _stop_event = None


def get_auto_sync_instance() -> Optional[EcoEyeAutoSync]:
    """
    Get the singleton EcoEyeAutoSync instance, or None if not started.
    """
    return _sync_instance


def get_ecoeye_auto_sync_status() -> Dict:
    """
    Get the current EcoEye auto-sync status.

    Returns:
        Dict with status information, or a disabled status if the
        sync daemon has not been started.
    """
    if _sync_instance is None:
        return {
            "enabled": False,
            "poll_interval": DEFAULT_POLL_INTERVAL,
            "retention_hours": DEFAULT_RETENTION_HOURS,
            "last_sync_time": None,
            "last_cycle_result": None,
            "pending_downloads": None,
        }
    return _sync_instance.get_status()
