"""
Clip Tracker — Multi-Object Tracking on Video Clips via ByteTrack

Runs YOLO-World + ByteTrack MOT on video clips to produce real tracked objects
with trajectories, timestamps, best-quality crops, and ReID embeddings.

Replaces the IoU-based track builder for cross-camera matching by providing:
- Real timestamps (seconds within clip + absolute epoch)
- Per-frame trajectories with bounding boxes
- Best-crop extraction for high-quality ReID embeddings
- Direction-of-travel from trajectory analysis

Usage:
    from clip_tracker import run_clip_tracking
    run_clip_tracking(video_id=42, camera_id='mwcam8', frigate_event_id='abc123')
"""

import logging
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

import cv2
import numpy as np
import requests

from db_connection import get_cursor

logger = logging.getLogger(__name__)

CLIPS_DIR = Path('/opt/groundtruth-studio/clips')
CROPS_DIR = CLIPS_DIR / 'crops'
REID_API_URL = 'http://localhost:5061'  # FastReID API (vehicle + person)
FRIGATE_URL = os.environ.get('FRIGATE_URL', 'http://localhost:5000')

# Reuse the same model as vehicle_detect_runner
MODEL_PATH = "/models/custom/people-vehicles-objects/models/yolov8x-worldv2.pt"
DEVICE = "1"
CONF_THRESHOLD = 0.10

# Vehicle classes that YOLO commonly confuses — treat as mergeable
COMPATIBLE_VEHICLE_CLASSES = [
    {'ATV', 'UTV', 'pickup truck', 'SUV'},
    {'sedan', 'SUV', 'car'},
    {'box truck', 'delivery truck', 'truck'},
]


def _get_frigate_url() -> str:
    """Get Frigate URL from environment or ingester singleton."""
    if FRIGATE_URL:
        return FRIGATE_URL
    try:
        from frigate_ingester import get_ingester
        ingester = get_ingester()
        if ingester:
            return ingester.frigate_url
    except Exception:
        pass
    return ''


def _get_model():
    """Get the shared YOLO-World model singleton from vehicle_detect_runner."""
    from vehicle_detect_runner import _get_model as get_vdr_model
    return get_vdr_model()


def _fetch_clip(video_id: int, frigate_event_id: str) -> Optional[str]:
    """Fetch a clip from Frigate for the given event. Returns clip path or None."""
    frigate_url = _get_frigate_url()
    if not frigate_url:
        logger.warning("No FRIGATE_URL configured, cannot fetch clip for video %d", video_id)
        return None

    from video_utils import VideoProcessor
    processor = VideoProcessor()
    result = processor.fetch_frigate_clip(
        frigate_url=frigate_url,
        event_id=frigate_event_id,
        camera=''
    )
    if result.get('success'):
        return result['clip_path']

    logger.warning("Failed to fetch Frigate clip for event %s: %s",
                    frigate_event_id, result.get('error'))
    return None


def _get_clip_start_epoch(video_id: int, frigate_event_id: str) -> Optional[float]:
    """Get the absolute epoch time when the clip starts.

    For Frigate events, the event ID encodes the start timestamp.
    Format: <epoch_seconds>.<fractional>-<random>
    """
    try:
        # Frigate event IDs start with epoch timestamp
        epoch_str = frigate_event_id.split('-')[0]
        return float(epoch_str)
    except (ValueError, IndexError):
        pass

    # Fallback: use video upload_date
    try:
        with get_cursor(commit=False) as cursor:
            cursor.execute(
                "SELECT upload_date FROM videos WHERE id = %s", (video_id,)
            )
            row = cursor.fetchone()
            if row and row['upload_date']:
                return row['upload_date'].timestamp()
    except Exception:
        pass

    return None


def run_clip_tracking(video_id: int, camera_id: str,
                      frigate_event_id: str = None,
                      clip_path: str = None) -> Optional[Dict]:
    """Run multi-object tracking on a video clip.

    Either fetches a clip from Frigate (using frigate_event_id) or uses
    a provided clip_path directly (for EcoEye videos).

    Args:
        video_id: Database video ID
        camera_id: Camera identifier
        frigate_event_id: Frigate event UUID (fetches clip from Frigate API)
        clip_path: Direct path to an MP4 clip (for EcoEye or local videos)

    Returns:
        Dict with tracks_created, embeddings_generated counts, or None on failure
    """
    try:
        # Get clip path
        if not clip_path and frigate_event_id:
            clip_path = _fetch_clip(video_id, frigate_event_id)
        if not clip_path or not os.path.exists(clip_path):
            logger.warning("No clip available for video %d", video_id)
            return None

        # Check if already tracked
        with get_cursor(commit=False) as cursor:
            cursor.execute(
                "SELECT COUNT(*) as cnt FROM video_tracks WHERE video_id = %s",
                (video_id,)
            )
            if cursor.fetchone()['cnt'] > 0:
                logger.info("Video %d already has video_tracks, skipping", video_id)
                return {'tracks_created': 0, 'skipped': True}

        # Determine clip start epoch for absolute timestamps
        clip_start_epoch = None
        if frigate_event_id:
            clip_start_epoch = _get_clip_start_epoch(video_id, frigate_event_id)

        # Run ByteTrack MOT
        tracked_objects = _run_bytetrack(clip_path, camera_id, video_id)
        if not tracked_objects:
            logger.info("No objects tracked in clip for video %d", video_id)
            return {'tracks_created': 0, 'embeddings_generated': 0}

        # Store tracks and get ReID embeddings
        tracks_created = 0
        embeddings_generated = 0

        for obj in tracked_objects:
            # Compute absolute epoch times
            first_seen_epoch = None
            last_seen_epoch = None
            if clip_start_epoch:
                first_seen_epoch = clip_start_epoch + obj['first_seen']
                last_seen_epoch = clip_start_epoch + obj['last_seen']

            # Get ReID embedding for best crop
            reid_embedding = None
            if obj.get('best_crop_path') and os.path.exists(obj['best_crop_path']):
                reid_embedding = _get_reid_embedding(obj['best_crop_path'])
                if reid_embedding is not None:
                    embeddings_generated += 1

            # Compute centroid and average bbox from trajectory
            traj = obj['trajectory']
            avg_cx = int(np.mean([p['x'] + p['w'] / 2 for p in traj]))
            avg_cy = int(np.mean([p['y'] + p['h'] / 2 for p in traj]))
            avg_w = int(np.mean([p['w'] for p in traj]))
            avg_h = int(np.mean([p['h'] for p in traj]))

            # Insert video_track
            try:
                with get_cursor() as cursor:
                    cursor.execute("""
                        INSERT INTO video_tracks
                        (video_id, camera_id, tracker_track_id, class_name,
                         first_seen, last_seen, first_seen_epoch, last_seen_epoch,
                         trajectory, best_crop_path, avg_confidence,
                         bbox_centroid_x, bbox_centroid_y,
                         avg_bbox_width, avg_bbox_height,
                         reid_embedding, status)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                                %s::jsonb, %s, %s, %s, %s, %s, %s, %s, 'active')
                        ON CONFLICT (video_id, tracker_track_id) DO NOTHING
                        RETURNING id
                    """, (
                        video_id, camera_id, obj['track_id'], obj['class_name'],
                        obj['first_seen'], obj['last_seen'],
                        first_seen_epoch, last_seen_epoch,
                        _trajectory_to_json(traj),
                        obj.get('best_crop_path'),
                        obj['avg_confidence'],
                        avg_cx, avg_cy, avg_w, avg_h,
                        reid_embedding,
                    ))
                    row = cursor.fetchone()
                    if row:
                        tracks_created += 1
            except Exception as e:
                logger.error("Failed to insert video_track for video %d track %d: %s",
                             video_id, obj['track_id'], e)

        logger.info("Clip tracking complete for video %d: %d tracks, %d embeddings",
                     video_id, tracks_created, embeddings_generated)
        return {
            'tracks_created': tracks_created,
            'embeddings_generated': embeddings_generated,
            'total_objects': len(tracked_objects),
        }

    except Exception as e:
        logger.error("Clip tracking failed for video %d: %s", video_id, e, exc_info=True)
        return None


def _split_track_on_anomalies(detections: List[Dict]) -> List[List[Dict]]:
    """Split a track into segments when area jumps or direction reversals indicate tracker switched objects.

    When a vehicle passes behind an occluder (e.g., sign), ByteTrack sometimes latches
    onto the occluder, causing bbox area to jump dramatically. Additionally, when a
    departing vehicle's track buffer overlaps with an arriving vehicle from the opposite
    direction, the Kalman filter can re-associate the dead track with the new vehicle,
    causing a sudden direction reversal. This splits the track at such anomaly points
    and returns only valid segments.

    Args:
        detections: List of detection dicts with keys: timestamp, x, y, w, h, conf

    Returns:
        List of detection segments (list of lists). First segment is kept for the track.
    """
    if len(detections) < 8:
        # Need at least 8 frames to establish baseline
        return [detections]

    # Compute baseline area from first 8 detections
    baseline_areas = [d['w'] * d['h'] for d in detections[:8]]
    baseline_area = np.mean(baseline_areas)

    if baseline_area == 0:
        return [detections]

    segments = []
    current_segment = [detections[0]]

    for i in range(1, len(detections)):
        curr = detections[i]
        prev = detections[i - 1]

        curr_area = curr['w'] * curr['h']

        # Compute centroid displacement
        curr_cx = curr['x'] + curr['w'] / 2
        curr_cy = curr['y'] + curr['h'] / 2
        prev_cx = prev['x'] + prev['w'] / 2
        prev_cy = prev['y'] + prev['h'] / 2

        displacement = np.sqrt((curr_cx - prev_cx)**2 + (curr_cy - prev_cy)**2)

        # Check for area jump (100% change from baseline) with minimal movement
        # This indicates tracker latched onto a different, stationary object
        area_change_ratio = abs(curr_area - baseline_area) / baseline_area
        is_area_jump = area_change_ratio > 1.0 and displacement < 50

        # Check for sudden jump to different object (large displacement + area change)
        prev_area = prev['w'] * prev['h']
        if prev_area > 0:
            area_change_from_prev = abs(curr_area - prev_area) / prev_area
        else:
            area_change_from_prev = 0
        is_sudden_jump = displacement > 300 and area_change_from_prev > 0.5

        # Direction reversal check — detect vx sign flip over sliding window
        if i >= 5:
            window = detections[i-5:i+1]
            old_vx = (window[2]['x'] + window[2]['w']/2) - (window[0]['x'] + window[0]['w']/2)
            new_vx = (window[-1]['x'] + window[-1]['w']/2) - (window[-2]['x'] + window[-2]['w']/2)
            # Sign flip with meaningful displacement (not momentary wobble)
            is_direction_reversal = (
                old_vx * new_vx < 0 and
                abs(new_vx) > 30 and
                abs(old_vx) > 30
            )
        else:
            is_direction_reversal = False

        if is_area_jump or is_sudden_jump or is_direction_reversal:
            # Split point detected
            if len(current_segment) >= 2:
                segments.append(current_segment)
            current_segment = [curr]
            # Update baseline for new segment
            baseline_area = curr_area
        else:
            current_segment.append(curr)

    # Add final segment
    if len(current_segment) >= 2:
        segments.append(current_segment)

    return segments if segments else [detections]


def _fill_detection_gaps(tracked_objects: List[Dict], clip_path: str, fps: float) -> List[Dict]:
    """Re-detect objects in trajectory gaps using targeted single-frame inference.

    For each track that has detection gaps (frames where ByteTrack lost the object),
    run YOLO-World on those specific frames with:
    - Lower confidence threshold (0.05 instead of 0.10)
    - A search region centered on the projected position from the last detection
    - IoU matching against the expected position

    Args:
        tracked_objects: List of tracked object dicts from _run_bytetrack()
        clip_path: Path to the video clip
        fps: Frames per second of the clip

    Returns:
        Updated tracked_objects with gap detections filled in
    """
    from vehicle_detect_runner import VEHICLE_DISPLAY_NAMES, ALL_CLASSES, NON_VEHICLE_CLASSES

    if not tracked_objects:
        return tracked_objects

    # Get total clip duration
    cap = cv2.VideoCapture(clip_path)
    if not cap.isOpened():
        return tracked_objects
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    clip_duration = total_frames / fps if fps > 0 else 0
    cap.release()

    if clip_duration <= 0:
        return tracked_objects

    GAP_CONF_THRESHOLD = 0.05
    MAX_GAP_FRAMES = 60  # Max frames to re-check per track (2s at 30fps)
    FRAME_SAMPLE_STEP = 3  # Check every 3rd frame in gaps
    TRAILING_GAP_THRESHOLD = 1.0  # seconds — trailing gap must exceed this
    INTERNAL_GAP_THRESHOLD = 0.5  # seconds — internal gap must exceed this
    SEARCH_REGION_SCALE = 3.0  # search region = Nx last known bbox size
    AREA_RATIO_MIN = 0.3
    AREA_RATIO_MAX = 3.0
    IOU_THRESHOLD = 0.1
    MAX_BBOX_DIMENSION = 400  # Max width or height in pixels — no real vehicle should be this large

    model = _get_model()
    if model is None:
        return tracked_objects

    total_filled = 0

    MIN_DETECTIONS_FOR_GAP_FILL = 5  # Need enough real detections for reliable velocity
    MIN_DETECTIONS_FOR_TRAILING = 8  # Trailing gaps extrapolate further, need more anchor data

    for obj in tracked_objects:
        trajectory = obj['trajectory']
        if len(trajectory) < MIN_DETECTIONS_FOR_GAP_FILL:
            continue

        track_class = obj['class_name']

        # --- Identify gaps ---
        gaps = []

        # 1) Trailing gap: track ends well before clip ends
        last_ts = trajectory[-1]['timestamp']
        if (clip_duration - last_ts) > TRAILING_GAP_THRESHOLD:
            gaps.append((last_ts, clip_duration, 'trailing'))

        # 2) Internal gaps: consecutive detections more than threshold apart
        for i in range(1, len(trajectory)):
            dt = trajectory[i]['timestamp'] - trajectory[i - 1]['timestamp']
            if dt > INTERNAL_GAP_THRESHOLD:
                gaps.append((trajectory[i - 1]['timestamp'], trajectory[i]['timestamp'], 'internal'))

        if not gaps:
            continue

        # --- For each gap, project position and re-detect ---
        new_detections = []
        frames_checked = 0

        for gap_start_ts, gap_end_ts, gap_type in gaps:
            if frames_checked >= MAX_GAP_FRAMES:
                break

            # Find the last N detections before the gap for extrapolation
            pre_gap = [d for d in trajectory if d['timestamp'] <= gap_start_ts]
            if len(pre_gap) < 1:
                continue

            # Trailing gaps extrapolate far — require more anchor data for reliable velocity
            if gap_type == 'trailing' and len(pre_gap) < MIN_DETECTIONS_FOR_TRAILING:
                continue

            # Use last 3 (or fewer) detections for linear extrapolation
            anchor_points = pre_gap[-3:] if len(pre_gap) >= 3 else pre_gap

            # Compute velocity from anchor points (pixels per second)
            if len(anchor_points) >= 2:
                dt_anchor = anchor_points[-1]['timestamp'] - anchor_points[0]['timestamp']
                if dt_anchor > 0:
                    vx = ((anchor_points[-1]['x'] + anchor_points[-1]['w'] / 2) -
                          (anchor_points[0]['x'] + anchor_points[0]['w'] / 2)) / dt_anchor
                    vy = ((anchor_points[-1]['y'] + anchor_points[-1]['h'] / 2) -
                          (anchor_points[0]['y'] + anchor_points[0]['h'] / 2)) / dt_anchor
                    vw = (anchor_points[-1]['w'] - anchor_points[0]['w']) / dt_anchor
                    vh = (anchor_points[-1]['h'] - anchor_points[0]['h']) / dt_anchor
                else:
                    vx = vy = vw = vh = 0.0
            else:
                vx = vy = vw = vh = 0.0

            last_det = anchor_points[-1]
            last_cx = last_det['x'] + last_det['w'] / 2
            last_cy = last_det['y'] + last_det['h'] / 2
            last_w = last_det['w']
            last_h = last_det['h']
            last_area = last_w * last_h

            # Determine which frames to check
            gap_start_frame = int(gap_start_ts * fps) + 1
            gap_end_frame = min(int(gap_end_ts * fps), total_frames - 1)

            # Sample every Nth frame
            sample_frames = list(range(gap_start_frame, gap_end_frame + 1, FRAME_SAMPLE_STEP))

            # Cap total frames checked per track
            remaining = MAX_GAP_FRAMES - frames_checked
            if len(sample_frames) > remaining:
                sample_frames = sample_frames[:remaining]

            if not sample_frames:
                continue

            # Open video for this gap
            gap_cap = cv2.VideoCapture(clip_path)
            if not gap_cap.isOpened():
                continue

            img_h = int(gap_cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            img_w = int(gap_cap.get(cv2.CAP_PROP_FRAME_WIDTH))

            for frame_num in sample_frames:
                frames_checked += 1
                frame_ts = frame_num / fps

                # Project expected position
                dt_from_last = frame_ts - last_det['timestamp']
                proj_cx = last_cx + vx * dt_from_last
                proj_cy = last_cy + vy * dt_from_last
                MIN_PROJ_DIM = 30  # Absolute floor — no projected dimension below 30px
                proj_w = max(min(last_w + vw * dt_from_last, last_w * 2.0), last_w * 0.5, MIN_PROJ_DIM)
                proj_h = max(min(last_h + vh * dt_from_last, last_h * 2.0), last_h * 0.5, MIN_PROJ_DIM)

                # Define search region — expand for trailing gaps with growing bbox
                if gap_type == 'trailing' and (vw > 0 or vh > 0):
                    search_scale = max(SEARCH_REGION_SCALE, SEARCH_REGION_SCALE + dt_from_last * 2.0)
                    search_scale = min(search_scale, 10.0)
                else:
                    search_scale = SEARCH_REGION_SCALE
                search_w = proj_w * search_scale
                search_h = proj_h * search_scale
                search_x1 = max(0, proj_cx - search_w / 2)
                search_y1 = max(0, proj_cy - search_h / 2)
                search_x2 = min(img_w, proj_cx + search_w / 2)
                search_y2 = min(img_h, proj_cy + search_h / 2)

                # Seek and read frame
                gap_cap.set(cv2.CAP_PROP_POS_FRAMES, frame_num)
                ret, frame_bgr = gap_cap.read()
                if not ret or frame_bgr is None:
                    continue

                # Convert to PIL for model.predict()
                from PIL import Image
                frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
                pil_img = Image.fromarray(frame_rgb)

                # Run single-frame detection with lower confidence
                try:
                    results = model.predict(
                        source=pil_img,
                        conf=GAP_CONF_THRESHOLD,
                        device=DEVICE,
                        verbose=False,
                    )
                except Exception as e:
                    logger.debug("Gap-fill predict failed on frame %d: %s", frame_num, e)
                    continue

                if not results or results[0].boxes is None or len(results[0].boxes) == 0:
                    continue

                result = results[0]
                best_match = None
                best_iou = -1.0

                for i, box in enumerate(result.boxes):
                    x1, y1, x2, y2 = box.xyxy[0].tolist()
                    confidence = float(box.conf[0])
                    class_id = int(box.cls[0])

                    raw_class = ALL_CLASSES[class_id] if class_id < len(ALL_CLASSES) else "unknown vehicle"
                    det_class = VEHICLE_DISPLAY_NAMES.get(raw_class, raw_class)

                    # Must be a vehicle class (or match track class)
                    if det_class in NON_VEHICLE_CLASSES:
                        continue

                    det_x = int(x1)
                    det_y = int(y1)
                    det_w = int(x2 - x1)
                    det_h = int(y2 - y1)
                    det_area = det_w * det_h

                    if det_w < 5 or det_h < 5:
                        continue

                    # Reject absurdly large detections (background false positives)
                    if det_w > MAX_BBOX_DIMENSION or det_h > MAX_BBOX_DIMENSION:
                        continue

                    # Area filter: compare against projected area (adapts as bbox grows)
                    projected_area = max(1, proj_w * proj_h)
                    if projected_area > 0:
                        area_ratio = det_area / projected_area
                        # For trailing gaps, allow more area growth based on elapsed time
                        if gap_type == 'trailing':
                            adaptive_area_max = AREA_RATIO_MAX * (1.0 + dt_from_last)
                        else:
                            adaptive_area_max = AREA_RATIO_MAX
                        if area_ratio < AREA_RATIO_MIN or area_ratio > adaptive_area_max:
                            continue

                    # Check if detection centroid is within search region
                    det_cx = det_x + det_w / 2
                    det_cy = det_y + det_h / 2
                    centroid_in_region = (search_x1 <= det_cx <= search_x2 and
                                         search_y1 <= det_cy <= search_y2)

                    # Compute IoU between detection and search region
                    ix1 = max(det_x, search_x1)
                    iy1 = max(det_y, search_y1)
                    ix2 = min(det_x + det_w, search_x2)
                    iy2 = min(det_y + det_h, search_y2)
                    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
                    union = det_area + (search_w * search_h) - inter
                    iou = inter / union if union > 0 else 0.0

                    if not centroid_in_region and iou < IOU_THRESHOLD:
                        continue

                    # Prefer matching track class, then highest confidence
                    class_match = (det_class == track_class)
                    score = (1.0 if class_match else 0.0) + confidence + iou

                    if score > best_iou:
                        best_iou = score
                        best_match = {
                            'timestamp': round(frame_ts, 3),
                            'x': det_x, 'y': det_y,
                            'w': det_w, 'h': det_h,
                            'conf': round(confidence, 4),
                            'gap_filled': True,
                        }

                # Fallback: for long trailing gaps where normal matching failed,
                # accept best class-matching detection anywhere in frame
                if best_match is None and gap_type == 'trailing' and dt_from_last > 2.0:
                    fallback_candidates = []
                    for j, box in enumerate(result.boxes):
                        x1f, y1f, x2f, y2f = box.xyxy[0].tolist()
                        conf_f = float(box.conf[0])
                        cls_id_f = int(box.cls[0])
                        raw_cls_f = ALL_CLASSES[cls_id_f] if cls_id_f < len(ALL_CLASSES) else "unknown vehicle"
                        det_cls_f = VEHICLE_DISPLAY_NAMES.get(raw_cls_f, raw_cls_f)
                        if det_cls_f != track_class:
                            continue
                        fw = int(x2f - x1f)
                        fh = int(y2f - y1f)
                        if fw < 5 or fh < 5:
                            continue
                        # Reject oversized false positives
                        if fw > MAX_BBOX_DIMENSION or fh > MAX_BBOX_DIMENSION:
                            continue
                        # Area ratio check against last known size
                        fallback_area = fw * fh
                        if last_area > 0:
                            fb_area_ratio = fallback_area / last_area
                            if fb_area_ratio < AREA_RATIO_MIN or fb_area_ratio > AREA_RATIO_MAX:
                                continue
                        fcx = x1f + fw / 2
                        fcy = y1f + fh / 2
                        dist = ((fcx - proj_cx) ** 2 + (fcy - proj_cy) ** 2) ** 0.5
                        fallback_candidates.append({
                            'dist': dist,
                            'det': {
                                'timestamp': round(frame_ts, 3),
                                'x': int(x1f), 'y': int(y1f),
                                'w': fw, 'h': fh,
                                'conf': round(conf_f, 4),
                                'gap_filled': True,
                            },
                        })
                    if fallback_candidates:
                        best_fallback = min(fallback_candidates, key=lambda c: c['dist'])
                        best_match = best_fallback['det']

                if best_match is not None:
                    new_detections.append(best_match)

            gap_cap.release()

        # Merge new detections into trajectory
        if new_detections:
            total_filled += len(new_detections)
            trajectory.extend(new_detections)
            trajectory.sort(key=lambda d: d['timestamp'])
            obj['trajectory'] = trajectory

            # Update last_seen if gap detections extended the track
            new_last_ts = trajectory[-1]['timestamp']
            if new_last_ts > obj['last_seen']:
                obj['last_seen'] = new_last_ts

            # Recompute avg_confidence with new detections
            all_confs = [d['conf'] for d in trajectory]
            obj['avg_confidence'] = round(float(np.mean(all_confs)), 4)

            logger.info(
                "Gap-fill: track %d gained %d detections (%d gaps), "
                "last_seen %.2fs -> %.2fs",
                obj['track_id'], len(new_detections), len(gaps),
                last_ts, obj['last_seen'],
            )

    if total_filled > 0:
        logger.info("Gap-fill pass complete: %d detections recovered across %d tracks",
                     total_filled, sum(1 for o in tracked_objects if
                                       any(d.get('gap_filled') for d in o['trajectory'])))

    return tracked_objects


def _merge_fragmented_tracks(tracked_objects: List[Dict], max_gap_seconds: float = 3.0,
                              max_distance_px: float = 150.0) -> List[Dict]:
    """Merge ByteTrack tracks that were fragmented by mid-journey class changes.

    When YOLO-World changes its class prediction (e.g., 'pickup truck' → 'ATV'),
    ByteTrack creates a new track. This function merges tracks that:
    1. Have compatible vehicle classes
    2. Are temporally close (track B starts shortly after track A ends)
    3. Are spatially close (track B's first position is near track A's last position)

    Uses velocity extrapolation from track A to predict where it should be when
    track B starts, for more accurate spatial matching.

    Args:
        tracked_objects: List of tracked object dicts from _run_bytetrack()
        max_gap_seconds: Maximum time gap between tracks to consider merging
        max_distance_px: Maximum distance between extrapolated end of A and start of B

    Returns:
        Merged list of tracked objects (fewer items if merges occurred)
    """
    if len(tracked_objects) < 2:
        return tracked_objects

    def _classes_compatible(cls_a, cls_b):
        if cls_a == cls_b:
            return True
        for group in COMPATIBLE_VEHICLE_CLASSES:
            if cls_a in group and cls_b in group:
                return True
        return False

    # Sort by first_seen time
    objects = sorted(tracked_objects, key=lambda o: o['first_seen'])
    merged_ids = set()  # track_ids that got merged into another
    merge_map = {}      # track_id → merged-into track_id

    for i, obj_a in enumerate(objects):
        if obj_a['track_id'] in merged_ids:
            continue

        traj_a = obj_a['trajectory']
        if len(traj_a) < 2:
            continue

        # Compute velocity from last few points of track A
        anchor_pts = traj_a[-3:] if len(traj_a) >= 3 else traj_a
        last_pt = traj_a[-1]
        last_cx = last_pt['x'] + last_pt['w'] / 2
        last_cy = last_pt['y'] + last_pt['h'] / 2

        vx, vy = 0.0, 0.0
        if len(anchor_pts) >= 2:
            dt = anchor_pts[-1]['timestamp'] - anchor_pts[0]['timestamp']
            if dt > 0:
                vx = ((anchor_pts[-1]['x'] + anchor_pts[-1]['w'] / 2) -
                      (anchor_pts[0]['x'] + anchor_pts[0]['w'] / 2)) / dt
                vy = ((anchor_pts[-1]['y'] + anchor_pts[-1]['h'] / 2) -
                      (anchor_pts[0]['y'] + anchor_pts[0]['h'] / 2)) / dt

        for j in range(i + 1, len(objects)):
            obj_b = objects[j]
            if obj_b['track_id'] in merged_ids:
                continue

            # Temporal check: B starts after A ends, within max_gap
            time_gap = obj_b['first_seen'] - obj_a['last_seen']
            if time_gap < -0.5:  # Allow slight overlap (0.5s)
                continue
            if time_gap > max_gap_seconds:
                break  # Sorted by time, no later tracks will match either

            # Class compatibility check
            if not _classes_compatible(obj_a['class_name'], obj_b['class_name']):
                continue

            # Spatial check: extrapolate track A's position to track B's start time
            traj_b = obj_b['trajectory']
            if not traj_b:
                continue

            first_b = traj_b[0]
            b_cx = first_b['x'] + first_b['w'] / 2
            b_cy = first_b['y'] + first_b['h'] / 2

            # Extrapolated position of A at B's start time
            dt_extrap = obj_b['first_seen'] - obj_a['last_seen']
            extrap_cx = last_cx + vx * dt_extrap
            extrap_cy = last_cy + vy * dt_extrap

            distance = ((extrap_cx - b_cx) ** 2 + (extrap_cy - b_cy) ** 2) ** 0.5

            if distance > max_distance_px:
                continue

            # --- MERGE B into A ---
            logger.info(
                "Merging track %d (%s, t=%.2f-%.2f) into track %d (%s, t=%.2f-%.2f) "
                "— gap=%.2fs, distance=%.1fpx",
                obj_b['track_id'], obj_b['class_name'], obj_b['first_seen'], obj_b['last_seen'],
                obj_a['track_id'], obj_a['class_name'], obj_a['first_seen'], obj_a['last_seen'],
                time_gap, distance
            )

            # Combine trajectories
            obj_a['trajectory'] = sorted(
                obj_a['trajectory'] + obj_b['trajectory'],
                key=lambda d: d['timestamp']
            )

            # Update temporal bounds
            obj_a['last_seen'] = max(obj_a['last_seen'], obj_b['last_seen'])
            obj_a['first_seen'] = min(obj_a['first_seen'], obj_b['first_seen'])

            # Recompute avg confidence
            all_confs = [d['conf'] for d in obj_a['trajectory']]
            obj_a['avg_confidence'] = round(float(np.mean(all_confs)), 4)

            # Use best crop from whichever track had higher confidence
            if obj_b.get('best_crop_path') and obj_b['avg_confidence'] > obj_a['avg_confidence']:
                obj_a['best_crop_path'] = obj_b['best_crop_path']

            # Mark B as merged
            merged_ids.add(obj_b['track_id'])
            merge_map[obj_b['track_id']] = obj_a['track_id']

            # Update A's endpoint for chained merges
            traj_a = obj_a['trajectory']
            last_pt = traj_a[-1]
            last_cx = last_pt['x'] + last_pt['w'] / 2
            last_cy = last_pt['y'] + last_pt['h'] / 2

    # Filter out merged tracks
    result = [o for o in objects if o['track_id'] not in merged_ids]

    if merged_ids:
        logger.info("Track merging: %d tracks merged into %d (removed %d fragments)",
                     len(tracked_objects), len(result), len(merged_ids))

    return result


def _run_bytetrack(clip_path: str, camera_id: str, video_id: int) -> List[Dict]:
    """Run YOLO-World + ByteTrack on a video clip.

    Args:
        clip_path: Path to MP4 clip
        camera_id: Camera identifier (for logging)
        video_id: Video ID (for crop filenames)

    Returns:
        List of tracked object dicts with trajectory, best_crop_path, etc.
    """
    model = _get_model()
    if model is None:
        logger.error("YOLO-World model not available for clip tracking")
        return []

    # Ensure crops directory exists
    CROPS_DIR.mkdir(parents=True, exist_ok=True)

    # Get video FPS for timestamp calculation
    cap = cv2.VideoCapture(clip_path)
    if not cap.isOpened():
        logger.error("Cannot open clip: %s", clip_path)
        return []

    fps = cap.get(cv2.CAP_PROP_FPS)
    if fps <= 0:
        fps = 30.0  # Fallback
    cap.release()

    # Run YOLO-World tracking with ByteTrack
    try:
        results = model.track(
            source=clip_path,
            persist=True,
            tracker=str(Path(__file__).parent / "bytetrack_custom.yaml"),
            conf=CONF_THRESHOLD,
            device=DEVICE,
            verbose=False,
            stream=True,
        )
    except Exception as e:
        logger.error("ByteTrack inference failed on %s: %s", clip_path, e)
        return []

    # Collect per-track detections across all frames
    # tracks_data[track_id] = {detections: [...], best_conf: float, best_frame: ndarray, ...}
    tracks_data = {}
    frame_idx = 0

    from vehicle_detect_runner import VEHICLE_DISPLAY_NAMES, ALL_CLASSES, NON_VEHICLE_CLASSES

    for result in results:
        timestamp = frame_idx / fps
        frame_idx += 1

        if result.boxes is None or len(result.boxes) == 0:
            continue

        # ByteTrack assigns IDs via result.boxes.id
        if result.boxes.id is None:
            continue

        frame_img = result.orig_img  # BGR numpy array

        for i, box in enumerate(result.boxes):
            track_id_tensor = result.boxes.id[i]
            if track_id_tensor is None:
                continue

            track_id = int(track_id_tensor.item())
            x1, y1, x2, y2 = box.xyxy[0].tolist()
            confidence = float(box.conf[0])
            class_id = int(box.cls[0])

            # Map class
            raw_class = ALL_CLASSES[class_id] if class_id < len(ALL_CLASSES) else "unknown vehicle"
            class_name = VEHICLE_DISPLAY_NAMES.get(raw_class, raw_class)

            # Skip non-vehicle classes for tracking
            if class_name in NON_VEHICLE_CLASSES:
                continue

            bbox_x = int(x1)
            bbox_y = int(y1)
            bbox_w = int(x2 - x1)
            bbox_h = int(y2 - y1)

            if bbox_w < 5 or bbox_h < 5:
                continue

            detection = {
                'timestamp': round(timestamp, 3),
                'x': bbox_x, 'y': bbox_y,
                'w': bbox_w, 'h': bbox_h,
                'conf': round(confidence, 4),
            }

            if track_id not in tracks_data:
                tracks_data[track_id] = {
                    'detections': [],
                    'class_votes': {},
                    'best_conf': 0.0,
                    'best_frame': None,
                    'best_bbox': None,
                }

            td = tracks_data[track_id]
            td['detections'].append(detection)

            # Vote on class name (majority wins)
            td['class_votes'][class_name] = td['class_votes'].get(class_name, 0) + 1

            # Track best frame (highest confidence) for crop extraction
            if confidence > td['best_conf']:
                td['best_conf'] = confidence
                # Clone the frame region for crop
                h_img, w_img = frame_img.shape[:2]
                cx1 = max(0, bbox_x)
                cy1 = max(0, bbox_y)
                cx2 = min(w_img, bbox_x + bbox_w)
                cy2 = min(h_img, bbox_y + bbox_h)
                if cx2 > cx1 and cy2 > cy1:
                    td['best_frame'] = frame_img[cy1:cy2, cx1:cx2].copy()
                    td['best_bbox'] = (cx1, cy1, cx2, cy2)

    # Build final tracked objects
    tracked_objects = []
    for track_id, td in tracks_data.items():
        detections = td['detections']
        if len(detections) < 2:
            continue  # Skip single-frame detections

        # Split track on anomalies (occluder detection or direction reversal)
        segments = _split_track_on_anomalies(detections)
        if len(segments) > 1:
            logger.warning(
                "Track %d split into %d segments due to area jumps or direction reversal "
                "(likely occluder interference or track ID reuse). "
                "Using first segment with %d detections, discarding %d spurious detections.",
                track_id, len(segments), len(segments[0]),
                sum(len(seg) for seg in segments[1:])
            )

        # Use only the first (longest valid) segment
        detections = segments[0]

        # Determine majority class
        class_name = max(td['class_votes'], key=td['class_votes'].get)

        # Save best crop
        crop_path = None
        if td['best_frame'] is not None:
            crop_filename = f"{video_id}_{track_id}.jpg"
            crop_path = str(CROPS_DIR / crop_filename)
            try:
                cv2.imwrite(crop_path, td['best_frame'],
                            [cv2.IMWRITE_JPEG_QUALITY, 95])
            except Exception as e:
                logger.warning("Failed to save crop for track %d: %s", track_id, e)
                crop_path = None

        confidences = [d['conf'] for d in detections]
        tracked_objects.append({
            'track_id': track_id,
            'class_name': class_name,
            'trajectory': detections,
            'first_seen': detections[0]['timestamp'],
            'last_seen': detections[-1]['timestamp'],
            'best_crop_path': crop_path,
            'avg_confidence': round(float(np.mean(confidences)), 4),
        })

    # Merge tracks fragmented by mid-journey class changes (e.g., ATV → pickup truck)
    tracked_objects = _merge_fragmented_tracks(tracked_objects)

    # Fill detection gaps with targeted re-detection
    tracked_objects = _fill_detection_gaps(tracked_objects, clip_path, fps)

    logger.info("ByteTrack on %s: %d tracks from %d frames (camera=%s)",
                 clip_path, len(tracked_objects), frame_idx, camera_id)
    return tracked_objects


def _get_reid_embedding(crop_path: str) -> Optional[list]:
    """Get ReID embedding from the Vehicle ReID API.

    Args:
        crop_path: Path to the crop image file

    Returns:
        List of floats (2048-dim embedding) or None on failure
    """
    try:
        with open(crop_path, 'rb') as f:
            resp = requests.post(
                f"{REID_API_URL}/embed",
                files={'image': (os.path.basename(crop_path), f, 'image/jpeg')},
                timeout=10,
            )

        if resp.status_code == 200:
            data = resp.json()
            embedding = data.get('embedding')
            if embedding and isinstance(embedding, list):
                return embedding
            # Some APIs nest it differently
            if 'embeddings' in data and data['embeddings']:
                return data['embeddings'][0]

        logger.warning("ReID API returned %d for %s", resp.status_code, crop_path)
        return None

    except requests.Timeout:
        logger.warning("ReID API timeout for %s", crop_path)
        return None
    except Exception as e:
        logger.warning("ReID API error for %s: %s", crop_path, e)
        return None


def _trajectory_to_json(trajectory: List[Dict]) -> str:
    """Convert trajectory list to JSON string for JSONB storage."""
    import json
    return json.dumps(trajectory)


def get_video_track_direction(trajectory: List[Dict]) -> Optional[str]:
    """Determine direction of travel from a trajectory.

    Compares the first and last trajectory points to determine
    overall movement direction.

    Args:
        trajectory: List of {timestamp, x, y, w, h, conf} dicts

    Returns:
        Direction string like 'left_to_right', 'right_to_left',
        'approaching', 'departing', or None if insufficient data
    """
    if not trajectory or len(trajectory) < 3:
        return None

    first = trajectory[0]
    last = trajectory[-1]

    dx = (last['x'] + last['w'] / 2) - (first['x'] + first['w'] / 2)
    dy = (last['y'] + last['h'] / 2) - (first['y'] + first['h'] / 2)

    # Size change indicates approaching/departing
    first_area = first['w'] * first['h']
    last_area = last['w'] * last['h']
    if first_area > 0:
        size_ratio = last_area / first_area
    else:
        size_ratio = 1.0

    # Dominant motion axis
    abs_dx = abs(dx)
    abs_dy = abs(dy)

    if abs_dx < 10 and abs_dy < 10:
        # Minimal movement — check size change
        if size_ratio > 1.5:
            return 'approaching'
        elif size_ratio < 0.67:
            return 'departing'
        return None  # Stationary

    if abs_dx > abs_dy:
        return 'left_to_right' if dx > 0 else 'right_to_left'
    else:
        if size_ratio > 1.3:
            return 'approaching'
        elif size_ratio < 0.77:
            return 'departing'
        return 'top_to_bottom' if dy > 0 else 'bottom_to_top'
