"""
YOLO-World pre-screening and vehicle detection runner.

Single-pass pre-screener that detects both people and vehicles.
- Vehicle detections: submitted as predictions for human review/classification
- Person detections: triggers person-face-v1 for precise person/face detection + recognition
- Empty scenes: skips all downstream models (saves compute)
"""

import os
import time
import logging
import threading
from pathlib import Path
from typing import Optional, Dict, List
from image_quality import compute_crop_quality

import requests

logger = logging.getLogger(__name__)

MODEL_PATH = "/models/custom/people-vehicles-objects/models/yolov8x-worldv2.pt"
API_BASE_URL = "http://localhost:5050"
MODEL_NAME = "vehicle-world-v1"
MODEL_VERSION = "2.0"
MODEL_TYPE = "yolo-world"
CONF_THRESHOLD = 0.08  # Base threshold for YOLO-World inference (low to catch everything)
DEVICE = "1"  # GPU 1

# Per-class confidence thresholds applied AFTER inference.
# Classes below their threshold are discarded. Keyed by display name.
CLASS_CONF_THRESHOLDS = {
    # Pre-screen
    "person": 0.15,
    # Land - Common (well-represented in YOLO-World pretraining)
    "sedan": 0.15,
    "pickup truck": 0.15,
    "SUV": 0.15,
    "minivan": 0.15,
    "van": 0.15,
    # Land - Rural/Specialty (less common in pretraining, lower raw confidence)
    "tractor": 0.12,
    "ATV": 0.40,
    "UTV": 0.10,
    "snowmobile": 0.10,
    "golf cart": 0.10,
    "skid loader": 0.12,
    "motorcycle": 0.12,
    "trailer": 0.12,
    # Land - Large (very distinct silhouettes)
    "bus": 0.20,
    "semi truck": 0.20,
    "dump truck": 0.18,
    # Watercraft
    "rowboat": 0.12,
    "fishing boat": 0.12,
    "speed boat": 0.12,
    "pontoon boat": 0.12,
    "kayak": 0.10,
    "canoe": 0.10,
    "sailboat": 0.12,
    "jet ski": 0.12,
    # Stationary / infrastructure
    "fence": 0.15,
    # Animals
    "dog": 0.15,
    "deer": 0.12,
    "turkey": 0.12,
    "squirrel": 0.12,
    "other animal": 0.15,
}
DEFAULT_CONF_THRESHOLD = 0.15

# Multi-frame video detection settings
VIDEO_EXTENSIONS = {'.mp4', '.avi', '.mov', '.mkv', '.webm'}
MULTIFRAME_INTERVAL = 10  # seconds between frame samples
MULTIFRAME_MAX_FRAMES = 60  # cap for long videos
MULTIFRAME_START_OFFSET = 10  # skip first 10s (thumbnail covers ~1s)
DOWNLOAD_DIR = "/opt/groundtruth-studio/downloads"

# All classes for YOLO-World single-pass detection.
# "person" is included for pre-screening only — person detections are NOT
# submitted as vehicle-world-v1 predictions. Instead they trigger person-face-v1.
ALL_CLASSES = [
    # Pre-screen (not submitted as vehicle predictions)
    "person",
    # Land - Common
    "sedan car",
    "pickup truck",
    "SUV sport utility vehicle",
    "minivan",
    "cargo van delivery van",
    # Land - Rural/Specialty
    "farm tractor",
    "ATV four wheeler quad",
    "UTV side by side utility vehicle",
    "snowmobile",
    "golf cart utility cart",
    "skid loader skid steer bobcat",
    "motorcycle",
    "trailer flatbed trailer",
    # Land - Large
    "bus school bus",
    "semi truck tractor trailer",
    "dump truck",
    # Watercraft
    "rowboat small boat dinghy",
    "fishing boat motorboat",
    "speed boat powerboat",
    "pontoon boat",
    "kayak",
    "canoe",
    "sailboat",
    "jet ski personal watercraft",
    # Stationary / infrastructure
    "fence",
    # Animals
    "dog",
    "deer white tailed deer",
    "turkey wild turkey",
    "squirrel",
    "other animal unknown animal",
    # POLICY: No decoy classes. Use hard negative training data instead.
    # See clip analysis export for corrective training pipeline.
]

# Index of person class (for filtering pre-screen vs vehicle predictions)
PERSON_CLASS_ID = 0  # "person" is first in ALL_CLASSES

# Classes excluded from vehicle predictions
NON_VEHICLE_CLASSES = {"person", "dog", "deer", "turkey", "squirrel", "other animal"}

# Infrastructure classes — detected by the same model but not vehicles.
# These get their own scenario instead of 'vehicle_detection'.
INFRASTRUCTURE_CLASSES = {"fence"}

# Animal classes — detected by the same model but not vehicles.
# These get their own scenario instead of 'vehicle_detection'.
ANIMAL_CLASSES = {"dog", "deer", "turkey", "squirrel", "other animal"}

# Map YOLO-World prompt text to clean display names for GT Studio
VEHICLE_DISPLAY_NAMES = {
    "person": "person",  # pre-screen only, not submitted
    "sedan car": "sedan",
    "pickup truck": "pickup truck",
    "SUV sport utility vehicle": "SUV",
    "minivan": "minivan",
    "cargo van delivery van": "van",
    "farm tractor": "tractor",
    "ATV four wheeler quad": "ATV",
    "UTV side by side utility vehicle": "UTV",
    "snowmobile": "snowmobile",
    "golf cart utility cart": "golf cart",
    "skid loader skid steer bobcat": "skid loader",
    "motorcycle": "motorcycle",
    "trailer flatbed trailer": "trailer",
    "bus school bus": "bus",
    "semi truck tractor trailer": "semi truck",
    "dump truck": "dump truck",
    "rowboat small boat dinghy": "rowboat",
    "fishing boat motorboat": "fishing boat",
    "speed boat powerboat": "speed boat",
    "pontoon boat": "pontoon boat",
    "kayak": "kayak",
    "canoe": "canoe",
    "sailboat": "sailboat",
    "jet ski personal watercraft": "jet ski",
    # Stationary / infrastructure
    "fence": "fence",
    # Animals
    "dog": "dog",
    "deer white tailed deer": "deer",
    "turkey wild turkey": "turkey",
    "squirrel": "squirrel",
    "other animal unknown animal": "other animal",
}


def _apply_confusion_rules(predictions: list) -> list:
    """
    Apply heuristic rules to resolve common YOLO-World confusion pairs.

    Uses bbox geometry (aspect ratio, area) to reclassify detections
    that are commonly confused. Only fires when confidence is borderline.
    """
    for pred in predictions:
        cls = pred['tags']['class']
        conf = pred['confidence']
        bbox = pred['bbox']
        w, h = bbox['width'], bbox['height']

        if w == 0 or h == 0:
            continue

        aspect_ratio = w / h  # >1 = wide, <1 = tall
        area = w * h

        # SUV vs sedan: SUVs are taller relative to width (lower aspect ratio)
        # Only reclassify when confidence is moderate (model is uncertain)
        if cls == "SUV" and conf < 0.50 and aspect_ratio > 1.8:
            pred['tags']['class'] = "sedan"
            pred['tags']['vehicle_type'] = "sedan"
            pred['tags']['reclassified_from'] = "SUV"
            pred['tags']['reclassify_reason'] = "aspect_ratio"

        elif cls == "sedan" and conf < 0.50 and aspect_ratio < 1.3:
            pred['tags']['class'] = "SUV"
            pred['tags']['vehicle_type'] = "SUV"
            pred['tags']['reclassified_from'] = "sedan"
            pred['tags']['reclassify_reason'] = "aspect_ratio"

        # Pickup truck vs SUV: pickups tend to be longer (higher aspect ratio)
        elif cls == "SUV" and conf < 0.40 and aspect_ratio > 2.0:
            pred['tags']['class'] = "pickup truck"
            pred['tags']['vehicle_type'] = "pickup truck"
            pred['tags']['reclassified_from'] = "SUV"
            pred['tags']['reclassify_reason'] = "aspect_ratio"

        # ATV vs UTV: UTVs are significantly larger
        elif cls == "ATV" and conf < 0.40 and area > 40000:
            pred['tags']['class'] = "UTV"
            pred['tags']['vehicle_type'] = "UTV"
            pred['tags']['reclassified_from'] = "ATV"
            pred['tags']['reclassify_reason'] = "size_threshold"

        elif cls == "UTV" and conf < 0.40 and area < 8000:
            pred['tags']['class'] = "ATV"
            pred['tags']['vehicle_type'] = "ATV"
            pred['tags']['reclassified_from'] = "UTV"
            pred['tags']['reclassify_reason'] = "size_threshold"

    return predictions


def _is_likely_tree(img, bbox, green_threshold=0.55):
    """Check if a bbox region is predominantly green/brown foliage.

    Crops the bbox from the image, converts to HSV, and counts pixels
    in the green-foliage and brown-bark color ranges. If the combined
    ratio exceeds the threshold, this detection is likely a tree.

    Args:
        img: PIL Image (already loaded for inference)
        bbox: dict with x, y, width, height
        green_threshold: fraction of pixels that must be green/brown (default 0.55)

    Returns:
        True if likely a tree, False otherwise
    """
    import numpy as np
    import cv2

    crop = img.crop((bbox['x'], bbox['y'],
                     bbox['x'] + bbox['width'],
                     bbox['y'] + bbox['height']))
    crop_np = np.array(crop.convert('RGB'))
    hsv = cv2.cvtColor(crop_np, cv2.COLOR_RGB2HSV)

    total_pixels = hsv.shape[0] * hsv.shape[1]
    if total_pixels == 0:
        return False

    # Green foliage: H=35-85, S>40, V>30 (broad green range)
    green_mask = ((hsv[:,:,0] >= 35) & (hsv[:,:,0] <= 85) &
                  (hsv[:,:,1] >= 40) & (hsv[:,:,2] >= 30))

    # Brown bark/branches: H=10-25, S>30, V>20
    brown_mask = ((hsv[:,:,0] >= 10) & (hsv[:,:,0] <= 25) &
                  (hsv[:,:,1] >= 30) & (hsv[:,:,2] >= 20))

    foliage_ratio = (np.count_nonzero(green_mask) + np.count_nonzero(brown_mask)) / total_pixels
    return foliage_ratio >= green_threshold


# Static object exclusion zones — cached per camera
_exclusion_zones = {}  # camera_id -> list of (x, y, radius)
_exclusion_zones_loaded_at = 0  # timestamp of last load

def _load_exclusion_zones():
    """Load static object exclusion zones from rejection history.

    Queries the database for bbox locations that have been rejected 5+ times
    on the same camera (rounded to 20px grid). These represent fixed objects
    like wreaths, signs, etc. that the model keeps misdetecting.

    Cached for 1 hour to avoid repeated DB queries.
    """
    global _exclusion_zones, _exclusion_zones_loaded_at

    try:
        conn = _get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute('''
                    SELECT v.camera_id,
                           ROUND(p.bbox_x / 20.0) * 20 as bx,
                           ROUND(p.bbox_y / 20.0) * 20 as by,
                           COUNT(*) as rejection_count
                    FROM ai_predictions p
                    JOIN videos v ON p.video_id = v.id
                    WHERE p.review_status IN ('rejected', 'auto_rejected')
                    AND p.bbox_x IS NOT NULL
                    AND v.camera_id IS NOT NULL
                    GROUP BY v.camera_id, bx, by
                    HAVING COUNT(*) >= 5
                ''')
                zones = {}
                for row in cur.fetchall():
                    cam = row[0]
                    if cam not in zones:
                        zones[cam] = []
                    zones[cam].append((int(row[1]), int(row[2]), 30))  # 30px radius
                _exclusion_zones = zones
                _exclusion_zones_loaded_at = time.time()
                total = sum(len(v) for v in zones.values())
                if total > 0:
                    logger.info(f"Loaded {total} static exclusion zones across {len(zones)} cameras")
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Failed to load exclusion zones: {e}")


def _get_camera_id(video_id):
    """Look up camera_id for a video."""
    try:
        conn = _get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT camera_id FROM videos WHERE id = %s", (video_id,))
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            conn.close()
    except Exception:
        return None


def _is_in_exclusion_zone(camera_id, bbox):
    """Check if a bbox center falls within a static object exclusion zone.

    Args:
        camera_id: Camera identifier string
        bbox: dict with x, y, width, height

    Returns:
        True if bbox center is within an exclusion zone
    """
    global _exclusion_zones_loaded_at

    if not camera_id:
        return False

    # Reload zones if stale (every hour)
    if time.time() - _exclusion_zones_loaded_at > 3600:
        _load_exclusion_zones()

    zones = _exclusion_zones.get(camera_id)
    if not zones:
        return False

    cx = bbox['x'] + bbox['width'] // 2
    cy = bbox['y'] + bbox['height'] // 2

    for zx, zy, radius in zones:
        if abs(cx - zx) <= radius and abs(cy - zy) <= radius:
            return True
    return False


def _cross_class_nms(predictions: list, iou_threshold: float = 0.5) -> list:
    """
    Suppress overlapping detections across different classes.

    When YOLO-World detects the same object as multiple classes
    (e.g., fishing boat 0.91 + speed boat 0.89), keep only the
    highest-confidence prediction for each spatial region.
    """
    if len(predictions) <= 1:
        return predictions

    # Sort by confidence descending
    preds = sorted(predictions, key=lambda p: p['confidence'], reverse=True)
    keep = []

    for pred in preds:
        box_a = pred['bbox']
        is_suppressed = False

        for kept in keep:
            box_b = kept['bbox']

            # Compute IoU
            ax1, ay1 = box_a['x'], box_a['y']
            ax2, ay2 = ax1 + box_a['width'], ay1 + box_a['height']
            bx1, by1 = box_b['x'], box_b['y']
            bx2, by2 = bx1 + box_b['width'], by1 + box_b['height']

            ix1, iy1 = max(ax1, bx1), max(ay1, by1)
            ix2, iy2 = min(ax2, bx2), min(ay2, by2)
            inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)

            if inter > 0:
                area_a = box_a['width'] * box_a['height']
                area_b = box_b['width'] * box_b['height']
                iou = inter / (area_a + area_b - inter)

                if iou >= iou_threshold:
                    is_suppressed = True
                    break

        if not is_suppressed:
            keep.append(pred)

    return keep


def _deduplicate_across_frames(all_detections: list) -> list:
    """Merge detections from different frames that represent the same object.

    Same class + (IoU >= 0.35 OR centroid within 40% of bbox size) = same object.
    Keep highest confidence detection, store earliest timestamp.
    """
    if len(all_detections) <= 1:
        return all_detections

    # Sort by confidence descending
    detections = sorted(all_detections, key=lambda d: d['confidence'], reverse=True)
    unique = []

    for det in detections:
        box_a = det['bbox']
        is_duplicate = False

        for kept in unique:
            # Only merge same class
            if det['tags']['class'] != kept['tags']['class']:
                continue

            box_b = kept['bbox']

            # Compute IoU
            ax1, ay1 = box_a['x'], box_a['y']
            ax2, ay2 = ax1 + box_a['width'], ay1 + box_a['height']
            bx1, by1 = box_b['x'], box_b['y']
            bx2, by2 = bx1 + box_b['width'], by1 + box_b['height']

            ix1, iy1 = max(ax1, bx1), max(ay1, by1)
            ix2, iy2 = min(ax2, bx2), min(ay2, by2)
            inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)

            area_a = box_a['width'] * box_a['height']
            area_b = box_b['width'] * box_b['height']
            union = area_a + area_b - inter
            iou = inter / union if union > 0 else 0.0

            if iou >= 0.35:
                # Good bbox overlap - same object
                if det['timestamp'] < kept['timestamp']:
                    kept['timestamp'] = det['timestamp']
                is_duplicate = True
                break

            # Fallback: centroid distance check for moving objects
            # If centroids are within 40% of the average bbox dimension, likely same object
            cx_a = box_a['x'] + box_a['width'] / 2
            cy_a = box_a['y'] + box_a['height'] / 2
            cx_b = box_b['x'] + box_b['width'] / 2
            cy_b = box_b['y'] + box_b['height'] / 2
            avg_size = (box_a['width'] + box_a['height'] + box_b['width'] + box_b['height']) / 4
            dist = ((cx_a - cx_b) ** 2 + (cy_a - cy_b) ** 2) ** 0.5

            if avg_size > 0 and dist < avg_size * 0.4:
                if det['timestamp'] < kept['timestamp']:
                    kept['timestamp'] = det['timestamp']
                is_duplicate = True
                break

        if not is_duplicate:
            unique.append(det)

    return unique


# Singleton model instance
_model = None
_model_lock = threading.Lock()

# Per-video locks
_video_locks = {}
_video_locks_lock = threading.Lock()


def _get_video_lock(video_id: int) -> threading.Lock:
    with _video_locks_lock:
        if video_id not in _video_locks:
            _video_locks[video_id] = threading.Lock()
        return _video_locks[video_id]


def _get_active_model_path():
    """Get active model path from model_deployments table, fallback to default."""
    try:
        conn = _get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT model_path FROM model_deployments
                    WHERE model_name = %s AND status = 'active'
                    ORDER BY deployed_at DESC LIMIT 1
                """, (MODEL_NAME,))
                row = cur.fetchone()
                if row and row[0] and os.path.exists(row[0]):
                    return row[0]
        finally:
            conn.close()
    except Exception:
        pass
    return MODEL_PATH  # fallback to hardcoded default


def _get_model():
    """Lazy-load the YOLO-World model (singleton)."""
    global _model
    if _model is not None:
        return _model

    with _model_lock:
        if _model is not None:
            return _model

        active_model_path = _get_active_model_path()
        if not Path(active_model_path).exists():
            logger.warning(f"Pre-screen model not found: {active_model_path}")
            return None

        try:
            from ultralytics import YOLO
            logger.info(f"Loading pre-screen model from {active_model_path}...")
            _model = YOLO(active_model_path)
            _model.set_classes(ALL_CLASSES)
            logger.info(f"Pre-screen model loaded with {len(ALL_CLASSES)} classes (including person pre-screen)")
            return _model
        except Exception as e:
            logger.error(f"Failed to load pre-screen model: {e}")
            return None


def reload_model():
    """Force reload of the detection model (called after deployment changes)."""
    global _model
    with _model_lock:
        _model = None
    logger.info("Detection model cache cleared, will reload on next inference")


def _get_db_connection():
    """Get a database connection."""
    import psycopg2
    DATABASE_URL = os.environ.get(
        'DATABASE_URL',
        'postgresql://groundtruth:bZv6QbJ8KCAQubJFb+frmbGNKUiPm7lBUg0XgMvEzNQ=@localhost:5432/groundtruth_studio'
    )
    return psycopg2.connect(DATABASE_URL)


def _has_existing_predictions(video_id: int) -> bool:
    """Check if video already has predictions from this model (including scan markers)."""
    try:
        conn = _get_db_connection()
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


def _store_scan_marker(video_id: int, person_count: int, inference_time_ms: float):
    """Store a marker indicating this video was scanned but no vehicles found.

    This prevents re-processing the video on subsequent batch runs.
    The marker has scenario='prescreen_scan' and review_status='no_detection'.
    """
    try:
        import json
        conn = _get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO ai_predictions
                        (video_id, model_name, model_version, prediction_type, confidence,
                         timestamp, scenario, predicted_tags, review_status, inference_time_ms, batch_id)
                    VALUES (%s, %s, %s, 'keyframe', 0, 0, 'prescreen_scan', %s, 'no_detection', %s, %s)
                """, (
                    video_id, MODEL_NAME, MODEL_VERSION,
                    json.dumps({'scan_result': 'no_vehicles', 'persons_prescreened': person_count}),
                    int(inference_time_ms),
                    f"scan-marker-{int(time.time())}"
                ))
                conn.commit()
                logger.debug(f"Stored scan marker for video {video_id} (no vehicles, {person_count} persons)")
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Failed to store scan marker for video {video_id}: {e}")


def _store_multiframe_scan_marker(video_id: int, person_count: int, inference_time_ms: float, frames_processed: int):
    """Store marker indicating multi-frame scan found no vehicles."""
    try:
        import json
        conn = _get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO ai_predictions
                        (video_id, model_name, model_version, prediction_type, confidence,
                         timestamp, scenario, predicted_tags, review_status, inference_time_ms, batch_id)
                    VALUES (%s, %s, %s, 'keyframe', 0, 0, 'prescreen_scan', %s, 'no_detection', %s, %s)
                """, (
                    video_id, MODEL_NAME, MODEL_VERSION,
                    json.dumps({
                        'scan_result': 'no_vehicles_multiframe',
                        'persons_prescreened': person_count,
                        'frames_processed': frames_processed
                    }),
                    int(inference_time_ms),
                    f"video-multiframe-{int(time.time())}"
                ))
                conn.commit()
                logger.debug(f"Stored multiframe scan marker for video {video_id} ({frames_processed} frames, no vehicles)")
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Failed to store multiframe scan marker for video {video_id}: {e}")


def _has_multiframe_predictions(video_id: int) -> bool:
    """Check if video already has multi-frame detection results."""
    try:
        conn = _get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM ai_predictions WHERE video_id = %s AND model_name = %s AND batch_id LIKE 'video-multiframe-%%'",
                    (video_id, MODEL_NAME)
                )
                return cur.fetchone()[0] > 0
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Failed to check multiframe predictions for video {video_id}: {e}")
        return False


def _auto_reject_batch(batch_id: str):
    """Mark all predictions in a batch as auto_rejected."""
    try:
        conn = _get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE ai_predictions SET review_status = 'auto_rejected' WHERE batch_id = %s",
                    (batch_id,)
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Failed to auto-reject batch {batch_id}: {e}")


def run_vehicle_detection(video_id: int, thumbnail_path: str, force_review: bool = True) -> Optional[Dict]:
    """
    Run YOLO-World pre-screening + vehicle detection on a thumbnail.

    Single-pass detection that:
    - Submits vehicle predictions for human review
    - Triggers person-face-v1 only when people are detected (saves compute)
    - Skips everything on empty scenes

    Args:
        video_id: GT Studio video ID
        thumbnail_path: Full path to thumbnail image
        force_review: If True, force all predictions to pending review (default True)

    Returns:
        Result dict with counts, or None on failure
    """
    video_lock = _get_video_lock(video_id)
    if not video_lock.acquire(blocking=False):
        logger.info(f"Pre-screen skipped video {video_id}: detection already in progress")
        return {'video_id': video_id, 'vehicles': 0, 'persons_prescreened': 0, 'submitted': 0, 'skipped': True}

    try:
        return _run_detection_locked(video_id, thumbnail_path, force_review)
    finally:
        video_lock.release()


def _run_detection_locked(video_id: int, thumbnail_path: str, force_review: bool = True) -> Optional[Dict]:
    """Internal: run detection while holding per-video lock."""
    if _has_existing_predictions(video_id):
        logger.info(f"Pre-screen skipped video {video_id}: already has predictions from {MODEL_NAME}")
        return {'video_id': video_id, 'vehicles': 0, 'persons_prescreened': 0, 'submitted': 0, 'skipped': True}

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

        start_time = time.time()
        results = model.predict(
            source=thumbnail_path,
            conf=CONF_THRESHOLD,
            device=DEVICE,
            verbose=False
        )
        inference_time_ms = (time.time() - start_time) * 1000

        vehicle_predictions = []
        person_count = 0
        person_bboxes = []
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

                # Skip out-of-bounds or degenerate boxes
                if (bbox['width'] < 5 or bbox['height'] < 5 or
                        bbox['x'] < 0 or bbox['y'] < 0 or
                        bbox['x'] + bbox['width'] > img_width or
                        bbox['y'] + bbox['height'] > img_height):
                    continue

                # Person detections are pre-screen only — counted but not submitted
                if class_id == PERSON_CLASS_ID:
                    if confidence >= CLASS_CONF_THRESHOLDS.get("person", DEFAULT_CONF_THRESHOLD):
                        person_count += 1
                        person_bboxes.append(bbox)
                    continue

                # Vehicle detections → submit as predictions
                raw_class = ALL_CLASSES[class_id] if class_id < len(ALL_CLASSES) else "unknown vehicle"
                class_name = VEHICLE_DISPLAY_NAMES.get(raw_class, raw_class)

                # Per-class confidence threshold
                min_conf = CLASS_CONF_THRESHOLDS.get(class_name, DEFAULT_CONF_THRESHOLD)
                if confidence < min_conf:
                    continue

                # Compute quality metrics for this crop
                quality = compute_crop_quality(img, bbox)

                vehicle_predictions.append({
                    'prediction_type': 'keyframe',
                    'confidence': round(confidence, 4),
                    'timestamp': 0.0,
                    'scenario': 'infrastructure_detection' if class_name in INFRASTRUCTURE_CLASSES else 'animal_detection' if class_name in ANIMAL_CLASSES else 'vehicle_detection',
                    'tags': {
                        'class': class_name,
                        'class_id': class_id,
                        'vehicle_type': class_name,
                        'yolo_world_prompt': raw_class,
                        'quality': quality
                    },
                    'bbox': bbox,
                    'inference_time_ms': round(inference_time_ms, 2),
                    'quality_score': quality['quality_score'],
                    'quality_flags': {f: True for f in quality['flags']} if quality['flags'] else {}
                })

        # Suppress overlapping cross-class vehicle detections
        vehicle_predictions = _cross_class_nms(vehicle_predictions, iou_threshold=0.5)

        # Apply heuristic rules for commonly confused pairs
        vehicle_predictions = _apply_confusion_rules(vehicle_predictions)

        # Filter tree false positives (HSV color check on bbox crops)
        pre_tree_vehicle_count = len(vehicle_predictions)
        vehicle_predictions = [p for p in vehicle_predictions if not _is_likely_tree(img, p['bbox'])]
        tree_filtered_vehicles = pre_tree_vehicle_count - len(vehicle_predictions)

        pre_tree_person_count = person_count
        person_bboxes = [b for b in person_bboxes if not _is_likely_tree(img, b)]
        person_count = len(person_bboxes)
        tree_filtered_persons = pre_tree_person_count - person_count

        if tree_filtered_vehicles or tree_filtered_persons:
            logger.info(f"Tree filter video {video_id}: removed {tree_filtered_vehicles} vehicles, {tree_filtered_persons} persons")

        # Filter static object exclusion zones (fixed objects like wreaths, signs)
        camera_id = _get_camera_id(video_id)
        if camera_id:
            pre_zone_vehicles = len(vehicle_predictions)
            vehicle_predictions = [p for p in vehicle_predictions if not _is_in_exclusion_zone(camera_id, p['bbox'])]
            pre_zone_persons = person_count
            person_bboxes = [b for b in person_bboxes if not _is_in_exclusion_zone(camera_id, b)]
            person_count = len(person_bboxes)
            zone_filtered_vehicles = pre_zone_vehicles - len(vehicle_predictions)
            zone_filtered_persons = pre_zone_persons - person_count
            if zone_filtered_vehicles or zone_filtered_persons:
                logger.info(f"Exclusion zone filter video {video_id} ({camera_id}): removed {zone_filtered_vehicles} vehicles, {zone_filtered_persons} persons")

        vehicle_count = len(vehicle_predictions)
        logger.info(
            f"Pre-screen video {video_id}: {person_count} persons, "
            f"{vehicle_count} vehicles ({inference_time_ms:.0f}ms)"
        )

        # --- Conditional person-face-v1 trigger ---
        # Only run the specialized person/face model when YOLO-World detects people
        if person_count > 0:
            try:
                from auto_detect_runner import trigger_auto_detect
                logger.info(
                    f"Pre-screen video {video_id}: {person_count} person(s) detected, "
                    f"triggering person-face-v1 for precise detection + recognition"
                )
                trigger_auto_detect(video_id, thumbnail_path, force_review=force_review)
            except Exception as e:
                logger.error(f"Failed to trigger person-face-v1 for video {video_id}: {e}")
        else:
            logger.debug(f"Pre-screen video {video_id}: no persons detected, skipping person-face-v1")

        # --- Submit vehicle predictions ---
        if vehicle_predictions:
            # Log class breakdown
            class_counts = {}
            for p in vehicle_predictions:
                cls = p['tags']['class']
                class_counts[cls] = class_counts.get(cls, 0) + 1
            logger.info(f"Pre-screen video {video_id} vehicles: {class_counts}")

            payload = {
                'video_id': video_id,
                'model_name': MODEL_NAME,
                'model_version': MODEL_VERSION,
                'model_type': MODEL_TYPE,
                'batch_id': f"vehicle-detect-{int(time.time())}",
                'predictions': vehicle_predictions,
                'force_review': force_review
            }

            response = requests.post(
                f"{API_BASE_URL}/api/ai/predictions/batch",
                json=payload,
                headers={'X-Auth-Role': 'admin'},
                timeout=30
            )
            response.raise_for_status()
        else:
            # No vehicles found — store a scan marker so we don't re-process this video
            _store_scan_marker(video_id, person_count, inference_time_ms)

        result = {
            'video_id': video_id,
            'persons_prescreened': person_count,
            'vehicles': vehicle_count,
            'submitted': len(vehicle_predictions),
            'person_face_triggered': person_count > 0
        }

        # --- Multi-frame detection for video files ---
        # After thumbnail processing, check if there's a video file to sample
        _trigger_multiframe_if_video(video_id, force_review)

        return result

    except Exception as e:
        logger.error(f"Pre-screen failed for video {video_id}: {e}")
        return None


def _trigger_multiframe_if_video(video_id: int, force_review: bool = True):
    """Check if a video file exists for this video_id and trigger multi-frame detection."""
    try:
        conn = _get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT filename FROM videos WHERE id = %s", (video_id,))
                row = cur.fetchone()
                if not row or not row[0]:
                    return
                filename = row[0]
        finally:
            conn.close()

        # Check if it's a video file (not an image or placeholder)
        ext = os.path.splitext(filename)[1].lower()
        if ext not in VIDEO_EXTENSIONS:
            return

        video_path = os.path.join(DOWNLOAD_DIR, filename)
        if not os.path.exists(video_path):
            return

        # Fire multiframe detection in a background thread
        thread = threading.Thread(
            target=run_video_multiframe_detection,
            args=(video_id, video_path, force_review),
            daemon=True,
            name=f"multiframe-{video_id}"
        )
        thread.start()
        logger.info(f"Multi-frame detection triggered in background for video {video_id}")

    except Exception as e:
        logger.error(f"Failed to trigger multi-frame detection for video {video_id}: {e}")


def run_video_multiframe_detection(video_id: int, video_path: str, force_review: bool = True) -> Optional[Dict]:
    """
    Run YOLO-World detection on multiple frames sampled from a video file.

    Samples one frame every 10 seconds (starting at 10s), runs inference on each,
    deduplicates across frames, and submits unique detections.

    Args:
        video_id: GT Studio video ID
        video_path: Full path to the video file
        force_review: If True, force all predictions to pending review

    Returns:
        Result dict with counts, or None on failure
    """
    import cv2
    from PIL import Image
    import numpy as np

    if _has_multiframe_predictions(video_id):
        logger.debug(f"Multi-frame skipped video {video_id}: already processed")
        return {'video_id': video_id, 'vehicles': 0, 'persons_prescreened': 0, 'submitted': 0, 'skipped': True}

    model = _get_model()
    if model is None:
        return None

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        logger.warning(f"Multi-frame: could not open video {video_path}")
        return None

    try:
        fps = cap.get(cv2.CAP_PROP_FPS)
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if fps <= 0 or frame_count <= 0:
            logger.warning(f"Multi-frame: invalid video properties for {video_path} (fps={fps}, frames={frame_count})")
            return None

        duration = frame_count / fps
        if duration <= MULTIFRAME_START_OFFSET:
            logger.debug(f"Multi-frame skipped video {video_id}: too short ({duration:.1f}s)")
            return {'video_id': video_id, 'vehicles': 0, 'persons_prescreened': 0, 'submitted': 0, 'skipped': True}

        # Build list of timestamps to sample
        sample_times = []
        t = float(MULTIFRAME_START_OFFSET)
        while t < duration and len(sample_times) < MULTIFRAME_MAX_FRAMES:
            sample_times.append(t)
            t += MULTIFRAME_INTERVAL

        if not sample_times:
            return {'video_id': video_id, 'vehicles': 0, 'persons_prescreened': 0, 'submitted': 0, 'skipped': True}

        logger.info(f"Multi-frame video {video_id}: sampling {len(sample_times)} frames from {duration:.1f}s video")

        all_vehicle_detections = []
        total_person_count = 0
        total_inference_ms = 0.0
        frames_processed = 0
        mf_camera_id = _get_camera_id(video_id)

        for timestamp in sample_times:
            # Seek to frame
            frame_number = int(timestamp * fps)
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_number)
            ret, frame_bgr = cap.read()
            if not ret:
                continue

            img_height, img_width = frame_bgr.shape[:2]

            # Convert BGR to RGB for PIL/YOLO
            frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            pil_img = Image.fromarray(frame_rgb)

            # Run inference
            start_time = time.time()
            results = model.predict(
                source=pil_img,
                conf=CONF_THRESHOLD,
                device=DEVICE,
                verbose=False
            )
            frame_inference_ms = (time.time() - start_time) * 1000
            total_inference_ms += frame_inference_ms
            frames_processed += 1

            result = results[0]
            if result.boxes is None or len(result.boxes) == 0:
                continue

            for box in result.boxes:
                x1, y1, x2, y2 = box.xyxy[0].tolist()
                confidence = float(box.conf[0])
                class_id = int(box.cls[0])

                bbox = {
                    'x': int(x1), 'y': int(y1),
                    'width': int(x2 - x1), 'height': int(y2 - y1)
                }

                # Skip degenerate/out-of-bounds boxes
                if (bbox['width'] < 5 or bbox['height'] < 5 or
                        bbox['x'] < 0 or bbox['y'] < 0 or
                        bbox['x'] + bbox['width'] > img_width or
                        bbox['y'] + bbox['height'] > img_height):
                    continue

                # Person pre-screen
                if class_id == PERSON_CLASS_ID:
                    if confidence >= CLASS_CONF_THRESHOLDS.get("person", DEFAULT_CONF_THRESHOLD):
                        if not _is_likely_tree(pil_img, bbox) and not _is_in_exclusion_zone(mf_camera_id, bbox):
                            total_person_count += 1
                    continue

                # Vehicle detection
                raw_class = ALL_CLASSES[class_id] if class_id < len(ALL_CLASSES) else "unknown vehicle"
                class_name = VEHICLE_DISPLAY_NAMES.get(raw_class, raw_class)

                min_conf = CLASS_CONF_THRESHOLDS.get(class_name, DEFAULT_CONF_THRESHOLD)
                if confidence < min_conf:
                    continue

                # Tree filter: skip if bbox is predominantly foliage
                if _is_likely_tree(pil_img, bbox):
                    continue

                # Static object exclusion zone
                if _is_in_exclusion_zone(mf_camera_id, bbox):
                    continue

                # Compute quality metrics for this crop
                quality = compute_crop_quality(pil_img, bbox)

                all_vehicle_detections.append({
                    'prediction_type': 'keyframe',
                    'confidence': round(confidence, 4),
                    'timestamp': round(timestamp, 2),
                    'scenario': 'infrastructure_detection' if class_name in INFRASTRUCTURE_CLASSES else 'animal_detection' if class_name in ANIMAL_CLASSES else 'vehicle_detection',
                    'tags': {
                        'class': class_name,
                        'class_id': class_id,
                        'vehicle_type': class_name,
                        'yolo_world_prompt': raw_class,
                        'source': 'multiframe',
                        'source_frame_time': round(timestamp, 2),
                        'quality': quality
                    },
                    'bbox': bbox,
                    'inference_time_ms': round(frame_inference_ms, 2),
                    'quality_score': quality['quality_score'],
                    'quality_flags': {f: True for f in quality['flags']} if quality['flags'] else {}
                })

        logger.info(
            f"Multi-frame video {video_id}: {frames_processed}/{len(sample_times)} frames processed, "
            f"{len(all_vehicle_detections)} vehicles, {total_person_count} persons "
            f"({total_inference_ms:.0f}ms total)"
        )

        # Deduplicate across frames (same object appearing in multiple frames)
        vehicle_predictions = _deduplicate_across_frames(all_vehicle_detections)

        # Standard post-processing
        vehicle_predictions = _cross_class_nms(vehicle_predictions, iou_threshold=0.5)
        vehicle_predictions = _apply_confusion_rules(vehicle_predictions)

        # Person pre-screen: trigger person-face-v1 if any frame had people
        if total_person_count > 0:
            try:
                from auto_detect_runner import trigger_auto_detect
                # Look up thumbnail path for person-face-v1
                conn = _get_db_connection()
                try:
                    with conn.cursor() as cur:
                        cur.execute("SELECT thumbnail_path FROM videos WHERE id = %s", (video_id,))
                        row = cur.fetchone()
                        thumb_path = row[0] if row else None
                finally:
                    conn.close()

                if thumb_path and os.path.exists(thumb_path):
                    logger.info(
                        f"Multi-frame video {video_id}: {total_person_count} person(s) across frames, "
                        f"triggering person-face-v1"
                    )
                    trigger_auto_detect(video_id, thumb_path, force_review=force_review)
            except Exception as e:
                logger.error(f"Multi-frame: failed to trigger person-face-v1 for video {video_id}: {e}")

        # Submit vehicle predictions
        if vehicle_predictions:
            class_counts = {}
            for p in vehicle_predictions:
                cls = p['tags']['class']
                class_counts[cls] = class_counts.get(cls, 0) + 1
            logger.info(f"Multi-frame video {video_id}: submitting {len(vehicle_predictions)} vehicles: {class_counts}")

            payload = {
                'video_id': video_id,
                'model_name': MODEL_NAME,
                'model_version': MODEL_VERSION,
                'model_type': MODEL_TYPE,
                'batch_id': f"video-multiframe-{int(time.time())}",
                'predictions': vehicle_predictions,
                'force_review': force_review
            }

            response = requests.post(
                f"{API_BASE_URL}/api/ai/predictions/batch",
                json=payload,
                headers={'X-Auth-Role': 'admin'},
                timeout=30
            )
            response.raise_for_status()
        else:
            # No vehicles found across any frame - store multiframe scan marker
            _store_multiframe_scan_marker(video_id, total_person_count, total_inference_ms, frames_processed)

        return {
            'video_id': video_id,
            'persons_prescreened': total_person_count,
            'vehicles': len(vehicle_predictions),
            'submitted': len(vehicle_predictions),
            'frames_sampled': len(sample_times),
            'frames_processed': frames_processed,
            'person_face_triggered': total_person_count > 0
        }

    except Exception as e:
        logger.error(f"Multi-frame detection failed for video {video_id}: {e}")
        return None
    finally:
        cap.release()


def trigger_vehicle_detect(video_id: int, thumbnail_path: str, force_review: bool = True):
    """
    Fire-and-forget: run YOLO-World pre-screen + vehicle detection in a background thread.

    This is the single entry point for all detection. It:
    1. Runs YOLO-World (people + vehicles) in one pass
    2. Submits vehicle predictions for review
    3. Conditionally triggers person-face-v1 only when people are detected
    """
    thread = threading.Thread(
        target=run_vehicle_detection,
        args=(video_id, thumbnail_path, force_review),
        daemon=True,
        name=f"prescreen-{video_id}"
    )
    thread.start()
    logger.info(f"Pre-screen triggered in background for video {video_id}")


# Load exclusion zones on startup
_load_exclusion_zones()
