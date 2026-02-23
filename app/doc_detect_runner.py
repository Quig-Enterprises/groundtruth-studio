"""
Document detection runner for identity document scanning.

Runs doc-detect-v1 YOLO model on images to detect identity documents
(passports, driver's licenses, TWIC cards, merchant mariner credentials).
Saves cropped document regions and triggers OCR processing.

Follows the same singleton model / per-video lock / fire-and-forget pattern
as auto_detect_runner.py.
"""

import os
import time
import logging
import threading
from pathlib import Path
from typing import Optional, Dict, List

import requests

logger = logging.getLogger(__name__)

MODEL_PATH = "/models/custom/document-detect/doc-detect-v1-best.pt"
API_BASE_URL = "http://localhost:5050"
MODEL_NAME = "doc-detect-v1"
MODEL_VERSION = "1.0"
MODEL_TYPE = "yolo"
CONF_THRESHOLD = 0.25

CROP_DIR = "/opt/groundtruth-studio/document_crops"

CLASS_NAMES = {
    0: "passport",
    1: "drivers_license",
    2: "twic_card",
    3: "merchant_mariner_credential",
    4: "id_card_generic",
}

SCENARIO = "document_detection"

# Singleton model instance (loaded once, reused across requests)
_model = None
_model_lock = threading.Lock()

# Per-video locks to prevent concurrent detection on the same video
_video_locks = {}
_video_locks_lock = threading.Lock()


def _get_video_lock(video_id: int) -> threading.Lock:
    """Get or create a lock for a specific video to prevent concurrent detection."""
    with _video_locks_lock:
        if video_id not in _video_locks:
            _video_locks[video_id] = threading.Lock()
        return _video_locks[video_id]


def _get_model():
    """Lazy-load the YOLO model (singleton with double-checked locking)."""
    global _model
    if _model is not None:
        return _model

    with _model_lock:
        if _model is not None:
            return _model

        if not Path(MODEL_PATH).exists():
            logger.warning(f"Document detect model not found: {MODEL_PATH}")
            return None

        try:
            from ultralytics import YOLO
            logger.info(f"Loading document detect model from {MODEL_PATH}...")
            _model = YOLO(MODEL_PATH)
            logger.info("Document detect model loaded successfully")
            return _model
        except Exception as e:
            logger.error(f"Failed to load document detect model: {e}")
            return None


def _has_existing_predictions(video_id: int) -> bool:
    """Check if video already has document detection predictions from this model."""
    try:
        import psycopg2
        DATABASE_URL = os.environ.get(
            'DATABASE_URL',
            'postgresql://groundtruth:bZv6QbJ8KCAQubJFb+frmbGNKUiPm7lBUg0XgMvEzNQ=@localhost:5432/groundtruth_studio'
        )
        conn = psycopg2.connect(DATABASE_URL)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM ai_predictions WHERE video_id = %s AND model_name = %s AND model_version = %s",
                    (video_id, MODEL_NAME, MODEL_VERSION)
                )
                return cur.fetchone()[0] > 0
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Failed to check existing document predictions for video {video_id}: {e}")
        return False


def _save_crop(image_path: str, bbox: Dict, video_id: int, detection_index: int) -> Optional[str]:
    """Crop and save the detected document region."""
    try:
        from PIL import Image

        os.makedirs(CROP_DIR, exist_ok=True)
        video_dir = os.path.join(CROP_DIR, str(video_id))
        os.makedirs(video_dir, exist_ok=True)

        img = Image.open(image_path)
        x, y, w, h = bbox['x'], bbox['y'], bbox['width'], bbox['height']
        cropped = img.crop((x, y, x + w, y + h))

        crop_filename = f"doc_{video_id}_{detection_index}_{int(time.time())}.jpg"
        crop_path = os.path.join(video_dir, crop_filename)
        cropped.save(crop_path, quality=95)

        logger.debug(f"Saved document crop: {crop_path}")
        return crop_path
    except Exception as e:
        logger.error(f"Failed to save document crop for video {video_id}: {e}")
        return None


def _create_document_scan(prediction_id: int, video_id: int, document_type: str,
                          crop_image_path: Optional[str], source_method: str = 'camera') -> Optional[int]:
    """Create a document_scans record for this detection."""
    try:
        from db_connection import get_cursor
        with get_cursor() as cursor:
            cursor.execute("""
                INSERT INTO document_scans
                    (prediction_id, video_id, document_type, source_method, crop_image_path)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
            """, (prediction_id, video_id, document_type, source_method, crop_image_path))
            return cursor.fetchone()['id']
    except Exception as e:
        logger.error(f"Failed to create document scan for prediction {prediction_id}: {e}")
        return None


def run_document_detection(video_id: int, image_path: str, device: str = "cpu",
                           force_review: bool = True,
                           source_method: str = 'camera') -> Optional[Dict]:
    """
    Run document detection on an image and submit predictions.

    Args:
        video_id: GT Studio video ID
        image_path: Full path to image file
        device: Inference device (cpu or 0 for GPU)
        force_review: If True, force all predictions to pending review (default True for documents)
        source_method: How the image was captured (camera/scanner/manual_upload)

    Returns:
        Result dict with counts, or None on failure
    """
    video_lock = _get_video_lock(video_id)
    if not video_lock.acquire(blocking=False):
        logger.info(f"Document detect skipped video {video_id}: detection already in progress")
        return {'video_id': video_id, 'documents': 0, 'submitted': 0, 'skipped': True}

    try:
        return _run_detection_locked(video_id, image_path, device, force_review, source_method)
    finally:
        video_lock.release()


def _run_detection_locked(video_id: int, image_path: str, device: str = "cpu",
                          force_review: bool = True,
                          source_method: str = 'camera') -> Optional[Dict]:
    """Internal: run detection while holding per-video lock."""
    if _has_existing_predictions(video_id):
        logger.info(f"Document detect skipped video {video_id}: already has predictions from {MODEL_NAME}")
        return {'video_id': video_id, 'documents': 0, 'submitted': 0, 'skipped': True}

    model = _get_model()
    if model is None:
        return None

    if not image_path or not os.path.exists(image_path):
        logger.warning(f"Image not found for video {video_id}: {image_path}")
        return None

    try:
        from PIL import Image

        img = Image.open(image_path)
        img_width, img_height = img.size

        start_time = time.time()
        results = model.predict(
            source=image_path,
            conf=CONF_THRESHOLD,
            device=device,
            verbose=False
        )
        inference_time_ms = (time.time() - start_time) * 1000

        predictions = []
        crop_paths = []
        result = results[0]

        if result.boxes is not None and len(result.boxes) > 0:
            for idx, box in enumerate(result.boxes):
                x1, y1, x2, y2 = box.xyxy[0].tolist()
                confidence = float(box.conf[0])
                class_id = int(box.cls[0])

                bbox = {
                    'x': int(x1), 'y': int(y1),
                    'width': int(x2 - x1), 'height': int(y2 - y1)
                }

                if (bbox['x'] < 0 or bbox['y'] < 0 or
                        bbox['x'] + bbox['width'] > img_width or
                        bbox['y'] + bbox['height'] > img_height):
                    continue

                doc_type = CLASS_NAMES.get(class_id, 'id_card_generic')

                crop_path = _save_crop(image_path, bbox, video_id, idx)
                crop_paths.append(crop_path)

                predictions.append({
                    'prediction_type': 'keyframe',
                    'confidence': round(confidence, 4),
                    'timestamp': 0.0,
                    'scenario': SCENARIO,
                    'tags': {
                        'class': doc_type,
                        'class_id': class_id,
                        'document_type': doc_type,
                    },
                    'bbox': bbox,
                    'inference_time_ms': round(inference_time_ms, 2),
                    '_crop_path': crop_path,
                    '_source_method': source_method,
                })

        doc_count = len(predictions)
        logger.info(f"Document detect video {video_id}: {doc_count} documents ({inference_time_ms:.0f}ms)")

        if not predictions:
            return {'video_id': video_id, 'documents': 0, 'submitted': 0}

        # Strip internal fields before API submission
        api_predictions = []
        for pred in predictions:
            api_pred = {k: v for k, v in pred.items() if not k.startswith('_')}
            api_predictions.append(api_pred)

        payload = {
            'video_id': video_id,
            'model_name': MODEL_NAME,
            'model_version': MODEL_VERSION,
            'model_type': MODEL_TYPE,
            'batch_id': f"doc-detect-{int(time.time())}",
            'predictions': api_predictions,
            'force_review': force_review,
        }

        response = requests.post(
            f"{API_BASE_URL}/api/ai/predictions/batch",
            json=payload,
            headers={'X-Auth-Role': 'admin'},
            timeout=30
        )
        response.raise_for_status()
        result_data = response.json()

        # Create document_scans records and trigger OCR for each detection
        prediction_ids = result_data.get('prediction_ids', [])
        for i, pred_id in enumerate(prediction_ids):
            if i < len(predictions):
                doc_type = predictions[i]['tags']['document_type']
                crop_path = predictions[i].get('_crop_path')
                scan_id = _create_document_scan(
                    pred_id, video_id, doc_type, crop_path, source_method
                )
                if scan_id and crop_path:
                    _trigger_ocr(scan_id, pred_id, video_id, crop_path, doc_type)

        return {
            'video_id': video_id,
            'documents': doc_count,
            'submitted': len(api_predictions),
            'prediction_ids': prediction_ids,
        }

    except Exception as e:
        logger.error(f"Document detect failed for video {video_id}: {e}")
        return None


def _trigger_ocr(scan_id: int, prediction_id: int, video_id: int,
                 crop_path: str, document_type: str):
    """Trigger OCR processing for a detected document (non-blocking)."""
    try:
        from doc_ocr_runner import trigger_document_ocr
        trigger_document_ocr(scan_id, prediction_id, video_id, crop_path, document_type)
    except ImportError:
        logger.debug("doc_ocr_runner not available yet, skipping OCR trigger")
    except Exception as e:
        logger.warning(f"Failed to trigger OCR for scan {scan_id}: {e}")


def trigger_document_detect(video_id: int, image_path: str, device: str = "cpu",
                            force_review: bool = True, source_method: str = 'camera'):
    """Fire-and-forget: run document detection in a background thread."""
    thread = threading.Thread(
        target=run_document_detection,
        args=(video_id, image_path, device, force_review, source_method),
        daemon=True,
        name=f"doc-detect-{video_id}"
    )
    thread.start()
    logger.info(f"Document detect triggered in background for video {video_id}")
