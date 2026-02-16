"""
Lightweight auto-detect runner for integration with GT Studio API.

Runs person-face-v1 YOLO model on a single video thumbnail in a background thread.
Designed to be called inline after thumbnail creation without blocking the request.
"""

import os
import time
import logging
import threading
from pathlib import Path
from typing import Optional, Dict, List

import requests

logger = logging.getLogger(__name__)

# Try to import person recognizer (optional)
try:
    from person_recognizer import get_recognizer
    RECOGNITION_AVAILABLE = True
except ImportError:
    RECOGNITION_AVAILABLE = False
    logger.debug("Person recognizer not available")

MODEL_PATH = "/models/custom/people-vehicles-objects/models/person-face-v1-best.pt"
API_BASE_URL = "http://localhost:5050"
MODEL_NAME = "person-face-v1"
MODEL_VERSION = "1.0"
MODEL_TYPE = "yolo"
CONF_THRESHOLD = 0.15

CLASS_NAMES = {0: "person", 1: "face"}
SCENARIO_MAP = {0: "person_detection", 1: "face_detection"}

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
    """Lazy-load the YOLO model (singleton)."""
    global _model
    if _model is not None:
        return _model

    with _model_lock:
        if _model is not None:
            return _model

        if not Path(MODEL_PATH).exists():
            logger.warning(f"Auto-detect model not found: {MODEL_PATH}")
            return None

        try:
            from ultralytics import YOLO
            logger.info(f"Loading auto-detect model from {MODEL_PATH}...")
            _model = YOLO(MODEL_PATH)
            logger.info("Auto-detect model loaded successfully")
            return _model
        except Exception as e:
            logger.error(f"Failed to load auto-detect model: {e}")
            return None


def _has_existing_predictions(video_id: int) -> bool:
    """Check if video already has predictions from this model."""
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
        logger.error(f"Failed to check existing predictions for video {video_id}: {e}")
        return False


def run_detection_on_thumbnail(video_id: int, thumbnail_path: str, device: str = "cpu", force_review: bool = False) -> Optional[Dict]:
    """
    Run person/face detection on a thumbnail and submit predictions.

    Args:
        video_id: GT Studio video ID
        thumbnail_path: Full path to thumbnail image
        device: Inference device (cpu or 0 for GPU)
        force_review: If True, bypass confidence-based routing and force pending review

    Returns:
        Result dict with counts, or None on failure
    """
    # Per-video lock to prevent concurrent detection on the same video
    video_lock = _get_video_lock(video_id)
    if not video_lock.acquire(blocking=False):
        logger.info(f"Auto-detect skipped video {video_id}: detection already in progress")
        return {'video_id': video_id, 'persons': 0, 'faces': 0, 'submitted': 0, 'skipped': True}

    try:
        return _run_detection_locked(video_id, thumbnail_path, device, force_review)
    finally:
        video_lock.release()


def _run_detection_locked(video_id: int, thumbnail_path: str, device: str = "cpu", force_review: bool = False) -> Optional[Dict]:
    """Internal: run detection while holding per-video lock."""
    # Skip if already processed
    if _has_existing_predictions(video_id):
        logger.info(f"Auto-detect skipped video {video_id}: already has predictions from {MODEL_NAME}")
        return {'video_id': video_id, 'persons': 0, 'faces': 0, 'submitted': 0, 'skipped': True}

    model = _get_model()
    if model is None:
        return None

    if not thumbnail_path or not os.path.exists(thumbnail_path):
        logger.warning(f"Thumbnail not found for video {video_id}: {thumbnail_path}")
        return None

    try:
        from PIL import Image

        img = Image.open(thumbnail_path)
        img_width, img_height = img.size

        # Run inference
        start_time = time.time()
        results = model.predict(
            source=thumbnail_path,
            conf=CONF_THRESHOLD,
            device=device,
            verbose=False
        )
        inference_time_ms = (time.time() - start_time) * 1000

        predictions = []
        result = results[0]

        if result.boxes is not None and len(result.boxes) > 0:
            for box in result.boxes:
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

                predictions.append({
                    'prediction_type': 'keyframe',
                    'confidence': round(confidence, 4),
                    'timestamp': 0.0,
                    'scenario': SCENARIO_MAP.get(class_id, 'unknown'),
                    'tags': {
                        'class': CLASS_NAMES.get(class_id, f'class_{class_id}'),
                        'class_id': class_id
                    },
                    'bbox': bbox,
                    'inference_time_ms': round(inference_time_ms, 2)
                })

        # Ensure every face has a person bbox
        predictions = _ensure_person_for_faces(predictions, img_width, img_height)

        # --- Person Recognition ---
        # For each face detection, try to identify the person
        face_predictions = [p for p in predictions if p['tags'].get('class_id') == 1]
        if RECOGNITION_AVAILABLE and face_predictions:
            try:
                recognizer = get_recognizer()
                gallery = recognizer.get_reference_gallery()
                if gallery:  # Only run if we have reference embeddings
                    face_bboxes = [p['bbox'] for p in face_predictions]
                    id_predictions = recognizer.recognize_faces_in_thumbnail(
                        thumbnail_path, face_bboxes
                    )
                    if id_predictions:
                        predictions.extend(id_predictions)
                        logger.info(
                            f"Auto-detect video {video_id}: {len(id_predictions)} person identifications"
                        )
            except Exception as e:
                logger.warning(f"Person recognition failed for video {video_id}: {e}")
                # Non-fatal - continue with regular predictions

        person_count = sum(1 for p in predictions if p['tags']['class_id'] == 0)
        face_count = sum(1 for p in predictions if p['tags']['class_id'] == 1)
        logger.info(f"Auto-detect video {video_id}: {person_count} persons, {face_count} faces ({inference_time_ms:.0f}ms)")

        if not predictions:
            return {'video_id': video_id, 'persons': 0, 'faces': 0, 'submitted': 0}

        # Submit to API
        payload = {
            'video_id': video_id,
            'model_name': MODEL_NAME,
            'model_version': MODEL_VERSION,
            'model_type': MODEL_TYPE,
            'batch_id': f"auto-detect-{int(time.time())}",
            'predictions': predictions,
            'force_review': force_review
        }

        response = requests.post(
            f"{API_BASE_URL}/api/ai/predictions/batch",
            json=payload,
            headers={'X-Auth-Role': 'admin'},
            timeout=30
        )
        response.raise_for_status()

        return {
            'video_id': video_id,
            'persons': person_count,
            'faces': face_count,
            'submitted': len(predictions)
        }

    except Exception as e:
        logger.error(f"Auto-detect failed for video {video_id}: {e}")
        return None


def _ensure_person_for_faces(predictions: List[Dict], img_width: int, img_height: int) -> List[Dict]:
    """For every face without a containing person bbox, synthesize one."""
    persons = [p for p in predictions if p['tags']['class_id'] == 0]
    faces = [p for p in predictions if p['tags']['class_id'] == 1]

    if not faces:
        return predictions

    new_persons = []
    for face in faces:
        fb = face['bbox']
        face_cx = fb['x'] + fb['width'] / 2
        face_cy = fb['y'] + fb['height'] / 2

        contained = any(
            pb['bbox']['x'] <= face_cx <= pb['bbox']['x'] + pb['bbox']['width'] and
            pb['bbox']['y'] <= face_cy <= pb['bbox']['y'] + pb['bbox']['height']
            for pb in persons
        )

        if not contained:
            body_height = int(fb['height'] * 6)
            body_width = int(fb['width'] * 2.5)
            px = max(0, int(face_cx - body_width / 2))
            py = max(0, fb['y'])
            pw = min(body_width, img_width - px)
            ph = min(body_height, img_height - py)

            new_persons.append({
                'prediction_type': 'keyframe',
                'confidence': round(face['confidence'] * 0.9, 4),
                'timestamp': 0.0,
                'scenario': 'person_detection',
                'tags': {'class': 'person', 'class_id': 0},
                'bbox': {'x': px, 'y': py, 'width': pw, 'height': ph},
                'inference_time_ms': face['inference_time_ms']
            })

    return predictions + new_persons


def trigger_auto_detect(video_id: int, thumbnail_path: str, device: str = "cpu", force_review: bool = False):
    """Fire-and-forget: run auto-detect in a background thread."""
    thread = threading.Thread(
        target=run_detection_on_thumbnail,
        args=(video_id, thumbnail_path, device, force_review),
        daemon=True,
        name=f"auto-detect-{video_id}"
    )
    thread.start()
    logger.info(f"Auto-detect triggered in background for video {video_id}")
