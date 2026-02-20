"""
Automatic Model Retraining Module

Monitors annotation review activity and triggers YOLOv8s retraining
when sufficient new reviewed annotations accumulate. Runs as a background
daemon thread, checking every 30 minutes.

Trigger: 200+ new reviewed annotations since last completed auto-retrain job.
Rate limit: Max 1 auto-retrain per 24 hours.
GPU: Uses GPU 1 (GPU 0 reserved for Frigate).
"""

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from psycopg2 import extras

from db_connection import get_connection

logger = logging.getLogger(__name__)

# Threshold: minimum new reviewed annotations to trigger retraining
ANNOTATION_THRESHOLD = 200

# Rate limit: minimum hours between auto-retrain jobs
RATE_LIMIT_HOURS = 24

# Background check interval in seconds (30 minutes)
CHECK_INTERVAL_SECONDS = 30 * 60

# Export config name used for auto-retrain
AUTO_RETRAIN_CONFIG_NAME = "auto-retrain-vehicle"

# All vehicle class names mapped to class IDs (must match classification_classes)
VEHICLE_CLASS_MAPPING = {
    "sedan": 0,
    "pickup truck": 1,
    "suv": 2,
    "minivan": 3,
    "van": 4,
    "tractor": 5,
    "atv": 6,
    "utv": 7,
    "snowmobile": 8,
    "golf cart": 9,
    "motorcycle": 10,
    "trailer": 11,
    "bus": 12,
    "semi truck": 13,
    "dump truck": 14,
    "rowboat": 15,
    "pontoon boat": 16,
    "ambulance": 17,
    "box truck": 18,
    "other truck": 19,
    "unknown vehicle": 20,
    "person": 21,
    "multiple_vehicles": 22,
}

# Module-level singleton state
_checker_instance: Optional["AutoRetrainChecker"] = None
_checker_thread: Optional[threading.Thread] = None
_stop_event: Optional[threading.Event] = None


class AutoRetrainChecker:
    """
    Checks whether conditions are met for automatic model retraining
    and triggers the export + training pipeline when appropriate.
    """

    def __init__(self, db, yolo_exporter, training_queue):
        """
        Args:
            db: VideoDatabase instance (passed through for compatibility).
            yolo_exporter: YOLOExporter instance for dataset export.
            training_queue: TrainingQueueClient instance for job submission.
        """
        self.db = db
        self.yolo_exporter = yolo_exporter
        self.training_queue = training_queue
        self._enabled = True

    # ------------------------------------------------------------------
    # Core check logic
    # ------------------------------------------------------------------

    def check_and_trigger(self, force: bool = False) -> Dict:
        """
        Main check logic. Queries review counts and rate limits, then
        triggers a retrain if all conditions are met.

        Args:
            force: If True, bypass the 24-hour rate limit (for manual triggers).

        Returns:
            Dict describing the action taken or reason for skipping.
        """
        if not self._enabled:
            logger.debug("Auto-retrain checker is disabled, skipping")
            return {"action": "skipped", "reason": "disabled"}

        try:
            last_completed_at = self._get_last_completed_auto_retrain_time()
            review_count = self._count_reviews_since(last_completed_at)
            last_submitted_at = self._get_last_auto_retrain_submission_time()

            # Check annotation threshold
            if review_count < ANNOTATION_THRESHOLD:
                logger.info(
                    "Auto-retrain check: %d reviewed annotations since last training "
                    "(threshold: %d) - not enough, skipping",
                    review_count, ANNOTATION_THRESHOLD,
                )
                return {
                    "action": "skipped",
                    "reason": "below_threshold",
                    "review_count": review_count,
                    "threshold": ANNOTATION_THRESHOLD,
                }

            # Check 24-hour rate limit (skipped when force=True)
            if not force and last_submitted_at is not None:
                cutoff = datetime.now(timezone.utc) - timedelta(hours=RATE_LIMIT_HOURS)
                if last_submitted_at > cutoff:
                    next_eligible = last_submitted_at + timedelta(hours=RATE_LIMIT_HOURS)
                    logger.info(
                        "Auto-retrain check: %d reviews meet threshold but rate-limited "
                        "until %s - skipping",
                        review_count, next_eligible.isoformat(),
                    )
                    return {
                        "action": "skipped",
                        "reason": "rate_limited",
                        "review_count": review_count,
                        "next_eligible": next_eligible.isoformat(),
                    }

            # All conditions met - trigger retraining
            logger.info(
                "Auto-retrain triggered: %d reviewed annotations since last training",
                review_count,
            )
            return self._trigger_retrain(review_count)

        except Exception:
            logger.exception("Error during auto-retrain check")
            return {"action": "error", "reason": "exception"}

    # ------------------------------------------------------------------
    # Status reporting
    # ------------------------------------------------------------------

    def get_status(self) -> Dict:
        """
        Returns current auto-retrain status for API consumption.
        """
        try:
            last_completed_at = self._get_last_completed_auto_retrain_time()
            review_count = self._count_reviews_since(last_completed_at)
            last_submitted_at = self._get_last_auto_retrain_submission_time()

            if last_submitted_at is not None:
                next_eligible = last_submitted_at + timedelta(hours=RATE_LIMIT_HOURS)
                next_eligible_str = next_eligible.isoformat()
            else:
                next_eligible_str = None

            return {
                "reviews_since_last_training": review_count,
                "threshold": ANNOTATION_THRESHOLD,
                "last_training_job": last_completed_at.isoformat() if last_completed_at else None,
                "next_eligible": next_eligible_str,
                "auto_retrain_enabled": self._enabled,
            }
        except Exception:
            logger.exception("Error getting auto-retrain status")
            return {
                "reviews_since_last_training": None,
                "threshold": ANNOTATION_THRESHOLD,
                "last_training_job": None,
                "next_eligible": None,
                "auto_retrain_enabled": self._enabled,
                "error": "Failed to query status",
            }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_last_completed_auto_retrain_time(self) -> Optional[datetime]:
        """
        Get the submitted_at timestamp of the most recent completed
        auto-triggered training job, or None if no such job exists.
        """
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
            cursor.execute("""
                SELECT MAX(submitted_at) AS last_submitted
                FROM training_jobs
                WHERE status = 'completed'
                  AND config_json LIKE '%%auto_triggered%%'
            """)
            row = cursor.fetchone()
        if row and row["last_submitted"]:
            ts = row["last_submitted"]
            # Ensure timezone-aware
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return ts
        return None

    def _get_last_auto_retrain_submission_time(self) -> Optional[datetime]:
        """
        Get the submitted_at timestamp of the most recent auto-triggered
        training job that is not failed or cancelled (for rate-limiting).
        """
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
            cursor.execute("""
                SELECT MAX(submitted_at) AS last_submitted
                FROM training_jobs
                WHERE config_json LIKE '%%auto_triggered%%'
                  AND status NOT IN ('failed', 'cancelled')
            """)
            row = cursor.fetchone()
        if row and row["last_submitted"]:
            ts = row["last_submitted"]
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return ts
        return None

    def _count_reviews_since(self, since: Optional[datetime]) -> int:
        """
        Count reviewed predictions (approved or rejected) since the given
        timestamp. If since is None, count all reviewed predictions.
        """
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
            if since is not None:
                cursor.execute("""
                    SELECT COUNT(*) AS cnt
                    FROM ai_predictions
                    WHERE review_status IN ('approved', 'rejected')
                      AND reviewed_at > %s
                """, (since,))
            else:
                cursor.execute("""
                    SELECT COUNT(*) AS cnt
                    FROM ai_predictions
                    WHERE review_status IN ('approved', 'rejected')
                """)
            row = cursor.fetchone()
        return row["cnt"] if row else 0

    def _find_or_create_export_config(self) -> int:
        """
        Find an existing YOLO export config named AUTO_RETRAIN_CONFIG_NAME,
        or create one with the full vehicle class mapping.

        Returns:
            The config ID.
        """
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
            cursor.execute(
                "SELECT id FROM yolo_export_configs WHERE config_name = %s",
                (AUTO_RETRAIN_CONFIG_NAME,),
            )
            row = cursor.fetchone()

        if row:
            logger.info(
                "Using existing export config '%s' (id=%d)",
                AUTO_RETRAIN_CONFIG_NAME, row["id"],
            )
            return row["id"]

        # Create new config
        config_id = self.yolo_exporter.create_export_config(
            config_name=AUTO_RETRAIN_CONFIG_NAME,
            class_mapping=VEHICLE_CLASS_MAPPING,
            description="Auto-retrain export config for all vehicle types",
            include_reviewed_only=False,
            include_ai_generated=True,
            include_negative_examples=True,
        )
        logger.info(
            "Created new export config '%s' (id=%d)",
            AUTO_RETRAIN_CONFIG_NAME, config_id,
        )
        return config_id

    def _trigger_retrain(self, review_count: int) -> Dict:
        """
        Execute the full retrain pipeline: find/create export config,
        export dataset, and submit training job.
        """
        # Step 1: Find or create export config
        config_id = self._find_or_create_export_config()

        # Step 2: Export dataset
        timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        output_name = f"auto_retrain_{timestamp_str}"

        logger.info("Exporting dataset for auto-retrain (config_id=%d, output=%s)",
                     config_id, output_name)
        export_result = self.yolo_exporter.export_dataset(
            config_id=config_id,
            output_name=output_name,
            val_split=0.2,
            seed=42,
        )

        if not export_result.get("success"):
            logger.error("Dataset export failed for auto-retrain: %s", export_result)
            return {"action": "error", "reason": "export_failed", "details": export_result}

        export_path = export_result["export_path"]
        logger.info(
            "Dataset exported: %d frames, %d annotations -> %s",
            export_result["frame_count"], export_result["annotation_count"], export_path,
        )

        # Step 3: Submit training job
        training_config = {
            "model_type": "yolov8s",
            "epochs": 100,
            "gpu_device": 1,
            "auto_triggered": True,
            "trigger_review_count": review_count,
            "trigger_threshold": ANNOTATION_THRESHOLD,
            "export_stats": {
                "frame_count": export_result["frame_count"],
                "annotation_count": export_result["annotation_count"],
                "train_count": export_result["train_count"],
                "val_count": export_result["val_count"],
            },
        }

        job_result = self.training_queue.submit_job(
            export_path=export_path,
            job_type="yolov8_train",
            config=training_config,
            export_config_id=config_id,
        )

        if job_result.get("success"):
            logger.info(
                "Auto-retrain job submitted: job_id=%s, review_count=%d",
                job_result["job_id"], review_count,
            )
        else:
            logger.error("Failed to submit auto-retrain job: %s", job_result)

        return {
            "action": "triggered",
            "review_count": review_count,
            "export_path": export_path,
            "job_result": job_result,
        }


# ======================================================================
# Background thread and singleton management
# ======================================================================

def _background_loop(checker: AutoRetrainChecker, stop_event: threading.Event):
    """
    Background loop that runs check_and_trigger() every CHECK_INTERVAL_SECONDS.
    Catches all exceptions to prevent the thread from dying.
    """
    logger.info(
        "Auto-retrain background checker started (interval=%ds, threshold=%d)",
        CHECK_INTERVAL_SECONDS, ANNOTATION_THRESHOLD,
    )

    while not stop_event.is_set():
        try:
            result = checker.check_and_trigger()
            logger.debug("Auto-retrain check result: %s", result)
        except Exception:
            logger.exception("Unhandled exception in auto-retrain background loop")

        # Wait for the interval or until stop is requested
        stop_event.wait(timeout=CHECK_INTERVAL_SECONDS)

    logger.info("Auto-retrain background checker stopped")


def start_auto_retrain_checker(db, yolo_exporter, training_queue) -> AutoRetrainChecker:
    """
    Start the background auto-retrain checker as a daemon thread.

    Args:
        db: VideoDatabase instance.
        yolo_exporter: YOLOExporter instance.
        training_queue: TrainingQueueClient instance.

    Returns:
        The AutoRetrainChecker instance.
    """
    global _checker_instance, _checker_thread, _stop_event

    if _checker_thread is not None and _checker_thread.is_alive():
        logger.warning("Auto-retrain checker is already running")
        return _checker_instance

    _checker_instance = AutoRetrainChecker(db, yolo_exporter, training_queue)
    _stop_event = threading.Event()

    _checker_thread = threading.Thread(
        target=_background_loop,
        args=(_checker_instance, _stop_event),
        daemon=True,
        name="auto-retrain-checker",
    )
    _checker_thread.start()

    logger.info("Auto-retrain background checker thread started")
    return _checker_instance


def stop_auto_retrain_checker():
    """
    Stop the background auto-retrain checker thread gracefully.
    """
    global _checker_thread, _stop_event

    if _stop_event is None or _checker_thread is None:
        logger.debug("Auto-retrain checker is not running, nothing to stop")
        return

    logger.info("Stopping auto-retrain background checker...")
    _stop_event.set()
    _checker_thread.join(timeout=10)

    if _checker_thread.is_alive():
        logger.warning("Auto-retrain checker thread did not stop within timeout")
    else:
        logger.info("Auto-retrain checker thread stopped cleanly")

    _checker_thread = None
    _stop_event = None


def get_checker_instance() -> Optional[AutoRetrainChecker]:
    """
    Get the singleton AutoRetrainChecker instance, or None if not started.
    """
    return _checker_instance


def get_auto_retrain_status() -> Dict:
    """
    Get the current auto-retrain status.

    Returns:
        Dict with status information, or a disabled status if the
        checker has not been started.
    """
    if _checker_instance is None:
        return {
            "reviews_since_last_training": None,
            "threshold": ANNOTATION_THRESHOLD,
            "last_training_job": None,
            "next_eligible": None,
            "auto_retrain_enabled": False,
        }
    return _checker_instance.get_status()
