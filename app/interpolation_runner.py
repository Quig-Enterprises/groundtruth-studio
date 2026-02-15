"""
Guided Keyframe Interpolation Runner.

When two approved keyframe predictions bracket the same object, this module
runs YOLO-World on every 1-second intermediate frame guided by the anchor
positions. Instead of blindly interpolating bbox coordinates, we run real
detection and match results to the expected object using class + IoU.

Frame images are cached to disk for the filmstrip review page.
"""

import os
import time
import logging
import threading
import json
from pathlib import Path
from typing import Optional, Dict, List, Tuple

import cv2
import requests

logger = logging.getLogger(__name__)

# Constants
INTERP_INTERVAL = 1.0  # seconds between interpolated frames
INTERP_IOU_THRESHOLD = 0.2  # minimum IoU to match a detection to expected position
FRAME_CACHE_DIR = "/opt/groundtruth-studio/frame_cache"
DOWNLOAD_DIR = "/opt/groundtruth-studio/downloads"
API_BASE_URL = "http://localhost:5050"
MODEL_NAME = "vehicle-world-v1"
MODEL_VERSION = "2.0"


def _interpolate_bbox(bbox_start: Dict, bbox_end: Dict, fraction: float) -> Dict:
    """Linearly interpolate between two bboxes.

    Args:
        bbox_start: {x, y, width, height} at start keyframe
        bbox_end: {x, y, width, height} at end keyframe
        fraction: 0.0 = start, 1.0 = end

    Returns:
        Interpolated bbox dict
    """
    return {
        'x': int(bbox_start['x'] + (bbox_end['x'] - bbox_start['x']) * fraction),
        'y': int(bbox_start['y'] + (bbox_end['y'] - bbox_start['y']) * fraction),
        'width': int(bbox_start['width'] + (bbox_end['width'] - bbox_start['width']) * fraction),
        'height': int(bbox_start['height'] + (bbox_end['height'] - bbox_start['height']) * fraction),
    }


def _compute_iou(box_a: Dict, box_b: Dict) -> float:
    """Compute IoU between two bbox dicts {x, y, width, height}."""
    ax1, ay1 = box_a['x'], box_a['y']
    ax2, ay2 = ax1 + box_a['width'], ay1 + box_a['height']
    bx1, by1 = box_b['x'], box_b['y']
    bx2, by2 = bx1 + box_b['width'], by1 + box_b['height']

    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)

    if inter == 0:
        return 0.0

    area_a = box_a['width'] * box_a['height']
    area_b = box_b['width'] * box_b['height']
    return inter / (area_a + area_b - inter)


def _match_to_expected(detections: List[Dict], expected_class: str,
                       expected_bbox: Dict, iou_threshold: float = INTERP_IOU_THRESHOLD) -> Optional[Dict]:
    """Match YOLO detections to the expected object.

    Args:
        detections: List of detection dicts with 'class', 'bbox', 'confidence'
        expected_class: Class name to match
        expected_bbox: Expected bbox position (from linear interpolation)
        iou_threshold: Minimum IoU to consider a match

    Returns:
        Best matching detection dict, or None if no match
    """
    best_match = None
    best_iou = iou_threshold

    for det in detections:
        if det['class'] != expected_class:
            continue

        iou = _compute_iou(det['bbox'], expected_bbox)
        if iou > best_iou:
            best_iou = iou
            best_match = det
            best_match['match_iou'] = iou

    return best_match


def _get_db_connection():
    """Get a database connection."""
    import psycopg2
    DATABASE_URL = os.environ.get(
        'DATABASE_URL',
        'postgresql://groundtruth:bZv6QbJ8KCAQubJFb+frmbGNKUiPm7lBUg0XgMvEzNQ=@localhost:5432/groundtruth_studio'
    )
    return psycopg2.connect(DATABASE_URL)


def _get_prediction(prediction_id: int) -> Optional[Dict]:
    """Load a prediction from the database."""
    import psycopg2.extras
    conn = _get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT p.*, v.filename, v.width as video_width, v.height as video_height
                FROM ai_predictions p
                JOIN videos v ON p.video_id = v.id
                WHERE p.id = %s
            """, (prediction_id,))
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


def _ensure_frame_cache_dir(video_id: int) -> str:
    """Create and return frame cache directory for a video."""
    cache_dir = os.path.join(FRAME_CACHE_DIR, str(video_id))
    os.makedirs(cache_dir, exist_ok=True)
    return cache_dir


def run_guided_interpolation(video_id: int, start_pred_id: int, end_pred_id: int) -> Optional[Dict]:
    """
    Run guided interpolation between two approved keyframe predictions.

    For each 1-second intermediate frame:
    1. Extract the frame from video
    2. Save frame JPEG to cache for review page
    3. Run YOLO-World inference
    4. Match detections to expected class + interpolated position
    5. Submit matched detections as predictions

    Args:
        video_id: GT Studio video ID
        start_pred_id: ID of the earlier approved prediction (anchor)
        end_pred_id: ID of the later approved prediction (anchor)

    Returns:
        Result dict with counts, or None on failure
    """
    from database import VideoDatabase
    db = VideoDatabase()

    # Load anchor predictions
    start_pred = _get_prediction(start_pred_id)
    end_pred = _get_prediction(end_pred_id)

    if not start_pred or not end_pred:
        logger.error(f"Interpolation failed: anchor predictions not found (start={start_pred_id}, end={end_pred_id})")
        return None

    # Determine class from predictions (use corrected_tags if available)
    start_tags = start_pred.get('corrected_tags') or start_pred['predicted_tags']
    end_tags = end_pred.get('corrected_tags') or end_pred['predicted_tags']

    if isinstance(start_tags, str):
        start_tags = json.loads(start_tags)
    if isinstance(end_tags, str):
        end_tags = json.loads(end_tags)

    class_name = start_tags.get('class', '')

    # Build anchor bboxes
    start_bbox = {
        'x': start_pred.get('corrected_bbox', {}).get('x') if start_pred.get('corrected_bbox') else start_pred['bbox_x'],
        'y': start_pred.get('corrected_bbox', {}).get('y') if start_pred.get('corrected_bbox') else start_pred['bbox_y'],
        'width': start_pred.get('corrected_bbox', {}).get('width') if start_pred.get('corrected_bbox') else start_pred['bbox_width'],
        'height': start_pred.get('corrected_bbox', {}).get('height') if start_pred.get('corrected_bbox') else start_pred['bbox_height'],
    }
    end_bbox = {
        'x': end_pred.get('corrected_bbox', {}).get('x') if end_pred.get('corrected_bbox') else end_pred['bbox_x'],
        'y': end_pred.get('corrected_bbox', {}).get('y') if end_pred.get('corrected_bbox') else end_pred['bbox_y'],
        'width': end_pred.get('corrected_bbox', {}).get('width') if end_pred.get('corrected_bbox') else end_pred['bbox_width'],
        'height': end_pred.get('corrected_bbox', {}).get('height') if end_pred.get('corrected_bbox') else end_pred['bbox_height'],
    }

    start_ts = float(start_pred['timestamp'])
    end_ts = float(end_pred['timestamp'])

    if start_ts >= end_ts:
        logger.error(f"Interpolation failed: start_ts ({start_ts}) >= end_ts ({end_ts})")
        return None

    # Create track record
    batch_id = f"interp-{int(time.time())}-{start_pred_id}-{end_pred_id}"
    track_id = db.create_interpolation_track(
        video_id=video_id,
        class_name=class_name,
        start_pred_id=start_pred_id,
        end_pred_id=end_pred_id,
        start_ts=start_ts,
        end_ts=end_ts,
        batch_id=batch_id
    )

    # Update track to processing
    db.update_interpolation_track(track_id, status='processing')

    # Find video file
    video_path = os.path.join(DOWNLOAD_DIR, start_pred['filename'])
    if not os.path.exists(video_path):
        logger.error(f"Interpolation failed: video file not found: {video_path}")
        db.update_interpolation_track(track_id, status='rejected')
        return None

    # Open video
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        logger.error(f"Interpolation failed: could not open video: {video_path}")
        db.update_interpolation_track(track_id, status='rejected')
        return None

    try:
        fps = cap.get(cv2.CAP_PROP_FPS)
        if fps <= 0:
            logger.error(f"Interpolation failed: invalid FPS for {video_path}")
            db.update_interpolation_track(track_id, status='rejected')
            return None

        # Build sample timestamps (exclusive of anchor times)
        sample_times = []
        t = start_ts + INTERP_INTERVAL
        while t < end_ts:
            sample_times.append(round(t, 2))
            t += INTERP_INTERVAL

        if not sample_times:
            logger.info(f"Interpolation: no intermediate frames between {start_ts}s and {end_ts}s")
            db.update_interpolation_track(track_id, status='ready', frames_generated=0, frames_detected=0)
            return {'track_id': track_id, 'frames_generated': 0, 'frames_detected': 0}

        logger.info(
            f"Guided interpolation: video {video_id}, class '{class_name}', "
            f"{start_ts}s -> {end_ts}s, {len(sample_times)} intermediate frames"
        )

        # Load YOLO-World model (reuse singleton from vehicle_detect_runner)
        from vehicle_detect_runner import _get_model, CONF_THRESHOLD, DEVICE, ALL_CLASSES, VEHICLE_DISPLAY_NAMES, CLASS_CONF_THRESHOLDS, DEFAULT_CONF_THRESHOLD, PERSON_CLASS_ID
        model = _get_model()
        if model is None:
            logger.error("Interpolation failed: YOLO-World model not available")
            db.update_interpolation_track(track_id, status='rejected')
            return None

        # Prepare frame cache directory
        cache_dir = _ensure_frame_cache_dir(video_id)

        predictions = []
        frames_detected = 0
        total_duration = end_ts - start_ts

        for timestamp in sample_times:
            # Seek to frame
            frame_number = int(timestamp * fps)
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_number)
            ret, frame_bgr = cap.read()
            if not ret:
                logger.warning(f"Interpolation: could not read frame at {timestamp}s")
                continue

            img_height, img_width = frame_bgr.shape[:2]

            # Save frame to cache
            timestamp_ms = int(timestamp * 1000)
            frame_filename = f"frame_{timestamp_ms}.jpg"
            frame_path = os.path.join(cache_dir, frame_filename)
            cv2.imwrite(frame_path, frame_bgr, [cv2.IMWRITE_JPEG_QUALITY, 85])

            # Compute expected position via linear interpolation
            fraction = (timestamp - start_ts) / total_duration
            expected_bbox = _interpolate_bbox(start_bbox, end_bbox, fraction)

            # Run YOLO-World inference
            from PIL import Image
            frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            pil_img = Image.fromarray(frame_rgb)

            start_time = time.time()
            results = model.predict(
                source=pil_img,
                conf=CONF_THRESHOLD,
                device=DEVICE,
                verbose=False
            )
            inference_ms = (time.time() - start_time) * 1000

            # Parse detections
            frame_detections = []
            result = results[0]
            if result.boxes is not None and len(result.boxes) > 0:
                for box in result.boxes:
                    x1, y1, x2, y2 = box.xyxy[0].tolist()
                    confidence = float(box.conf[0])
                    class_id = int(box.cls[0])

                    if class_id == PERSON_CLASS_ID:
                        continue

                    raw_class = ALL_CLASSES[class_id] if class_id < len(ALL_CLASSES) else "unknown"
                    det_class = VEHICLE_DISPLAY_NAMES.get(raw_class, raw_class)

                    min_conf = CLASS_CONF_THRESHOLDS.get(det_class, DEFAULT_CONF_THRESHOLD)
                    if confidence < min_conf:
                        continue

                    det_bbox = {
                        'x': int(x1), 'y': int(y1),
                        'width': int(x2 - x1), 'height': int(y2 - y1)
                    }

                    # Skip degenerate boxes
                    if det_bbox['width'] < 5 or det_bbox['height'] < 5:
                        continue

                    frame_detections.append({
                        'class': det_class,
                        'bbox': det_bbox,
                        'confidence': confidence,
                    })

            # Match to expected object
            match = _match_to_expected(frame_detections, class_name, expected_bbox)

            if match:
                frames_detected += 1
                predictions.append({
                    'prediction_type': 'keyframe',
                    'confidence': round(match['confidence'], 4),
                    'timestamp': timestamp,
                    'scenario': 'vehicle_detection',
                    'tags': {
                        'class': class_name,
                        'vehicle_type': class_name,
                        'source': 'interpolation',
                        'track_id': track_id,
                        'match_iou': round(match.get('match_iou', 0), 4),
                        'frame_cache': frame_filename,
                    },
                    'bbox': match['bbox'],
                    'inference_time_ms': round(inference_ms, 2),
                })
            else:
                # Store unmatched frame info as prediction with low confidence
                predictions.append({
                    'prediction_type': 'keyframe',
                    'confidence': 0.0,
                    'timestamp': timestamp,
                    'scenario': 'vehicle_detection',
                    'tags': {
                        'class': class_name,
                        'vehicle_type': class_name,
                        'source': 'interpolation',
                        'track_id': track_id,
                        'unmatched': True,
                        'frame_cache': frame_filename,
                    },
                    'bbox': expected_bbox,
                    'inference_time_ms': round(inference_ms, 2),
                })

        # Submit predictions via API
        if predictions:
            payload = {
                'video_id': video_id,
                'model_name': MODEL_NAME,
                'model_version': MODEL_VERSION,
                'model_type': 'yolo-world',
                'batch_id': batch_id,
                'predictions': predictions,
                'force_review': True
            }

            try:
                response = requests.post(
                    f"{API_BASE_URL}/api/ai/predictions/batch",
                    json=payload,
                    headers={'X-Auth-Role': 'admin'},
                    timeout=60
                )
                response.raise_for_status()
            except Exception as e:
                logger.error(f"Interpolation: failed to submit predictions: {e}")
                db.update_interpolation_track(track_id, status='rejected')
                return None

        # Update track to ready
        db.update_interpolation_track(
            track_id,
            status='ready',
            frames_generated=len(sample_times),
            frames_detected=frames_detected
        )

        logger.info(
            f"Guided interpolation complete: track {track_id}, "
            f"{frames_detected}/{len(sample_times)} frames matched, "
            f"batch_id={batch_id}"
        )

        return {
            'track_id': track_id,
            'video_id': video_id,
            'class_name': class_name,
            'frames_generated': len(sample_times),
            'frames_detected': frames_detected,
            'batch_id': batch_id,
        }

    except Exception as e:
        logger.error(f"Guided interpolation failed: {e}", exc_info=True)
        try:
            db.update_interpolation_track(track_id, status='rejected')
        except Exception:
            pass
        return None
    finally:
        cap.release()
