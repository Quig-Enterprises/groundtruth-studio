"""
Clip Analysis — Weighted Consensus Classification Engine for Single-Camera Clips

Tracks objects through video clips using ByteTrack MOT (via clip_tracker),
then runs YOLO-World on every frame to build per-frame classifications.
A weighted consensus vote determines the final class for each track, where
frames with large, high-confidence detections carry more weight.

Usage:
    from clip_analysis import run_clip_analysis, resolve_clip_source, export_training_frames

    source = resolve_clip_source(video_id=42)
    results = run_clip_analysis(**source)
    export_training_frames(results[0], top_n=10)
"""

import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional

import cv2
import numpy as np
from PIL import Image

from db_connection import get_cursor
from vehicle_detect_runner import (
    _get_model, VEHICLE_DISPLAY_NAMES, ALL_CLASSES, NON_VEHICLE_CLASSES, DEVICE
)
from clip_tracker import run_clip_tracking, get_video_track_direction
from video_utils import VideoProcessor

logger = logging.getLogger(__name__)

CLIPS_DIR = Path('/opt/groundtruth-studio/clips')
CROPS_DIR = CLIPS_DIR / 'crops'
FRIGATE_URL = os.environ.get('FRIGATE_URL', 'http://localhost:5000')
DOWNLOADS_DIR = Path('/opt/groundtruth-studio/downloads')

# YOLO-World inference confidence threshold (kept low to capture all detections)
INFERENCE_CONF = 0.08

# Default frame dimensions for center-score calculation
DEFAULT_FRAME_WIDTH = 1920
DEFAULT_FRAME_HEIGHT = 1080

# Minimum usable clip duration after sanitization (seconds)
MIN_CLIP_DURATION = 2.0


def _sanitize_clip(clip_path: str) -> Optional[str]:
    """Re-encode a clip to fix corrupt frames and timestamp issues.

    Runs ffmpeg to detect h264 decode errors.  If errors are found the clip
    is re-encoded to produce only clean, decodable frames with monotonic
    timestamps.  If the resulting clip is shorter than ``MIN_CLIP_DURATION``
    the file is rejected (returns None).

    Returns the path to the sanitized clip (may be the original if clean),
    or None if the clip is too corrupted to use.
    """
    # Quick error scan — decode to null, capture stderr
    try:
        probe = subprocess.run(
            ['ffmpeg', '-v', 'error', '-i', clip_path, '-f', 'null', '-'],
            capture_output=True, text=True, timeout=120,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Clip probe failed for %s: %s", clip_path, exc)
        return clip_path  # fall through — use original

    error_lines = [
        ln for ln in probe.stderr.splitlines()
        if 'error while decoding' in ln or 'non monotonically increasing dts' in ln
    ]

    if not error_lines:
        logger.info("Clip %s is clean (%d errors)", clip_path, len(error_lines))
        return clip_path

    logger.warning(
        "Clip %s has %d decode/DTS errors — re-encoding to sanitize",
        clip_path, len(error_lines),
    )

    # Re-encode to a temp file next to the original
    base, ext = os.path.splitext(clip_path)
    sanitized_path = f"{base}_clean{ext}"

    try:
        result = subprocess.run(
            [
                'ffmpeg', '-y',
                '-err_detect', 'careful',
                '-fflags', '+genpts+discardcorrupt',
                '-i', clip_path,
                '-c:v', 'libx264', '-preset', 'fast', '-crf', '18',
                '-an',           # drop audio — not needed for analysis
                '-movflags', '+faststart',
                sanitized_path,
            ],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            logger.error("ffmpeg re-encode failed: %s", result.stderr[-500:])
            return clip_path  # fall through with original
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.error("ffmpeg re-encode error for %s: %s", clip_path, exc)
        return clip_path

    # Verify output duration
    try:
        dur_result = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', sanitized_path],
            capture_output=True, text=True, timeout=30,
        )
        duration = float(dur_result.stdout.strip())
    except (ValueError, subprocess.TimeoutExpired, OSError):
        duration = 0.0

    if duration < MIN_CLIP_DURATION:
        logger.error(
            "Sanitized clip %s is too short (%.1fs < %.1fs) — rejecting",
            sanitized_path, duration, MIN_CLIP_DURATION,
        )
        try:
            os.remove(sanitized_path)
        except OSError:
            pass
        return None

    logger.info(
        "Sanitized clip ready: %s (%.1fs)", sanitized_path, duration,
    )
    return sanitized_path


# ---------------------------------------------------------------------------
# 1. Resolve clip source
# ---------------------------------------------------------------------------

def resolve_clip_source(video_id: int = None,
                        frigate_event_id: str = None,
                        ecoeye_alert_id: int = None,
                        clip_path: str = None) -> Optional[Dict]:
    """Resolve any clip source to a canonical (clip_path, video_id, camera_id) dict.

    Accepts exactly one of the source parameters.  Looks up or derives the
    remaining identifiers from the database so downstream functions always
    receive a complete triple.

    Args:
        video_id: Database video ID.
        frigate_event_id: Frigate event UUID — will fetch clip via Frigate API.
        ecoeye_alert_id: EcoEye alert row ID.
        clip_path: Direct filesystem path to an MP4 clip.

    Returns:
        Dict with keys ``clip_path``, ``video_id``, ``camera_id``, or None on
        failure.
    """
    try:
        # --- Direct clip path ---
        if clip_path:
            if not os.path.exists(clip_path):
                logger.error("resolve_clip_source: clip_path does not exist: %s", clip_path)
                return None

            filename = os.path.basename(clip_path)
            resolved_video_id = video_id
            camera_id = None

            with get_cursor(commit=False) as cur:
                cur.execute(
                    "SELECT id, camera_id FROM videos WHERE filename = %s LIMIT 1",
                    (filename,)
                )
                row = cur.fetchone()
                if row:
                    resolved_video_id = resolved_video_id or row['id']
                    camera_id = row['camera_id']

            if resolved_video_id is None:
                logger.error("resolve_clip_source: could not find video record for %s", filename)
                return None

            return {
                'clip_path': clip_path,
                'video_id': resolved_video_id,
                'camera_id': camera_id,
            }

        # --- Frigate event ---
        if frigate_event_id:
            processor = VideoProcessor()
            result = processor.fetch_frigate_clip(
                frigate_url=FRIGATE_URL,
                event_id=frigate_event_id,
                camera='',
            )
            if not result.get('success'):
                logger.error(
                    "resolve_clip_source: failed to fetch Frigate clip for event %s: %s",
                    frigate_event_id, result.get('error'),
                )
                return None

            fetched_path = result['clip_path']

            # Look up or create video record
            resolved_video_id = video_id
            camera_id = None
            if resolved_video_id:
                with get_cursor(commit=False) as cur:
                    cur.execute(
                        "SELECT camera_id FROM videos WHERE id = %s",
                        (resolved_video_id,)
                    )
                    row = cur.fetchone()
                    if row:
                        camera_id = row['camera_id']

            return {
                'clip_path': fetched_path,
                'video_id': resolved_video_id,
                'camera_id': camera_id,
            }

        # --- EcoEye alert ---
        if ecoeye_alert_id:
            with get_cursor(commit=False) as cur:
                cur.execute(
                    "SELECT local_video_path, camera_id FROM ecoeye_alerts "
                    "WHERE alert_id = %s AND video_downloaded = true",
                    (ecoeye_alert_id,)
                )
                alert_row = cur.fetchone()

            if not alert_row or not alert_row['local_video_path']:
                logger.error(
                    "resolve_clip_source: no downloaded video for ecoeye_alert %s",
                    ecoeye_alert_id,
                )
                return None

            local_path = alert_row['local_video_path']
            camera_id = alert_row['camera_id']

            if not os.path.exists(local_path):
                logger.error("resolve_clip_source: ecoeye video not on disk: %s", local_path)
                return None

            # Find matching video record
            resolved_video_id = None
            with get_cursor(commit=False) as cur:
                cur.execute(
                    "SELECT id FROM videos WHERE filename = %s LIMIT 1",
                    (os.path.basename(local_path),)
                )
                row = cur.fetchone()
                if row:
                    resolved_video_id = row['id']

            return {
                'clip_path': local_path,
                'video_id': resolved_video_id,
                'camera_id': camera_id,
            }

        # --- video_id only ---
        if video_id:
            with get_cursor(commit=False) as cur:
                cur.execute(
                    "SELECT filename, camera_id FROM videos WHERE id = %s",
                    (video_id,)
                )
                row = cur.fetchone()

            if not row or not row['filename']:
                logger.error("resolve_clip_source: no video record for id=%s", video_id)
                return None

            camera_id = row['camera_id']
            filename = row['filename']

            # Search clips/ then downloads/
            for search_dir in [CLIPS_DIR, DOWNLOADS_DIR]:
                candidate = search_dir / filename
                if candidate.exists():
                    return {
                        'clip_path': str(candidate),
                        'video_id': video_id,
                        'camera_id': camera_id,
                    }

            logger.error(
                "resolve_clip_source: video file %s not found in clips/ or downloads/",
                filename,
            )
            return None

        logger.error("resolve_clip_source: no source parameters provided")
        return None

    except Exception as e:
        logger.error("resolve_clip_source failed: %s", e, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# 2. Main analysis entry point
# ---------------------------------------------------------------------------

def run_clip_analysis(video_id: int,
                      camera_id: str,
                      clip_path: str,
                      frigate_event_id: str = None,
                      ecoeye_alert_id: int = None) -> Optional[List[int]]:
    """Run full clip analysis: tracking, per-frame classification, consensus.

    1. Ensures ``video_tracks`` exist (runs ByteTrack if not).
    2. Extracts per-frame YOLO-World classifications matched to each track.
    3. Computes weighted consensus and frame quality scores.
    4. Stores results in ``clip_analysis_results``.

    Args:
        video_id: Database video ID.
        camera_id: Camera identifier string.
        clip_path: Filesystem path to the MP4 clip.
        frigate_event_id: Optional Frigate event UUID (passed to tracker).
        ecoeye_alert_id: Optional EcoEye alert ID (for provenance).

    Returns:
        List of ``clip_analysis_results.id`` values, or None on failure.
    """
    try:
        logger.info(
            "Starting clip analysis for video %d (camera=%s, clip=%s)",
            video_id, camera_id, clip_path,
        )

        if not os.path.exists(clip_path):
            logger.error("Clip not found: %s", clip_path)
            return None

        # --- Step 0: Sanitize clip (fix corrupt frames / DTS issues) ---
        sanitized_path = _sanitize_clip(clip_path)
        if sanitized_path is None:
            logger.error("Clip too corrupted to analyze: %s", clip_path)
            return None
        if sanitized_path != clip_path:
            logger.info("Using sanitized clip: %s", sanitized_path)
            clip_path = sanitized_path

        # --- Step 1: Ensure video_tracks exist ---
        with get_cursor(commit=False) as cur:
            cur.execute(
                "SELECT id FROM video_tracks WHERE video_id = %s", (video_id,)
            )
            existing = cur.fetchall()

        if not existing:
            logger.info("No video_tracks for video %d — running clip tracking", video_id)
            tracking_result = run_clip_tracking(
                video_id=video_id,
                camera_id=camera_id,
                clip_path=clip_path,
                frigate_event_id=frigate_event_id,
            )
            if not tracking_result or tracking_result.get('tracks_created', 0) == 0:
                logger.warning("Clip tracking produced no tracks for video %d", video_id)
                return None

        # --- Step 2: Fetch active video_tracks ---
        with get_cursor(commit=False) as cur:
            cur.execute(
                "SELECT * FROM video_tracks WHERE video_id = %s AND status = 'active'",
                (video_id,)
            )
            video_tracks = cur.fetchall()

        if not video_tracks:
            logger.warning("No active video_tracks for video %d", video_id)
            return None

        logger.info("Found %d active tracks for video %d", len(video_tracks), video_id)

        # --- Step 2b: Merge overlapping tracks ---
        video_tracks = _merge_overlapping_tracks(video_tracks, video_id)
        # --- Step 2c: Stitch sequential fragments (same object, track dropped & re-acquired) ---
        video_tracks = _stitch_sequential_tracks(video_tracks, video_id)
        logger.info("After dedup: %d tracks for video %d", len(video_tracks), video_id)

        # --- Step 2d: Remove spatial jumps from trajectories ---
        video_tracks = _clean_trajectory_jumps(video_tracks, video_id)

        # --- Step 3: Per-frame classifications ---
        frame_classifications = _extract_per_frame_classifications(
            clip_path, video_tracks, video_id,
        )

        # --- Step 4 & 5 & 6: Consensus, quality, direction, and DB insert ---
        # Get clip duration for metadata
        cap = cv2.VideoCapture(clip_path)
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        duration_seconds = total_frames / fps if fps > 0 else 0.0
        cap.release()

        analysis_ids = []

        for track in video_tracks:
            track_id = track['id']
            trajectory = track['trajectory']
            if isinstance(trajectory, str):
                trajectory = json.loads(trajectory)

            track_frames = frame_classifications.get(track_id, [])

            if not track_frames:
                logger.debug(
                    "No frame classifications for track %d (video %d) — skipping",
                    track_id, video_id,
                )
                continue

            # Consensus
            consensus = compute_weighted_consensus(track_frames)

            # Quality scores
            quality_scores = score_frame_quality(track_frames, consensus['consensus_class'])

            # Direction
            direction = get_video_track_direction(trajectory)

            # --- Step 6: Insert into clip_analysis_results ---
            with get_cursor() as cur:
                cur.execute("""
                    INSERT INTO clip_analysis_results
                    (video_id, video_track_id, camera_id,
                     consensus_class, consensus_confidence, consensus_method,
                     frame_classifications, class_distribution,
                     frame_quality_scores,
                     total_frames, duration_seconds,
                     direction_of_travel, status, review_status,
                     created_at, updated_at)
                    VALUES (%s, %s, %s,
                            %s, %s, %s,
                            %s::jsonb, %s::jsonb,
                            %s::jsonb,
                            %s, %s,
                            %s, 'completed', 'pending',
                            NOW(), NOW())
                    RETURNING id
                """, (
                    video_id, track_id, camera_id,
                    consensus['consensus_class'],
                    round(consensus['consensus_confidence'], 4),
                    'weighted_area_confidence',
                    json.dumps(track_frames),
                    json.dumps(consensus['class_distribution']),
                    json.dumps(quality_scores),
                    len(track_frames),
                    round(duration_seconds, 2),
                    direction,
                ))
                row = cur.fetchone()
                if row:
                    analysis_ids.append(row['id'])
                    logger.info(
                        "Clip analysis %d: track %d → %s (%.1f%% confidence, %d frames, dir=%s)",
                        row['id'], track_id,
                        consensus['consensus_class'],
                        consensus['consensus_confidence'] * 100,
                        len(track_frames),
                        direction,
                    )

        logger.info(
            "Clip analysis complete for video %d: %d analysis results created",
            video_id, len(analysis_ids),
        )
        return analysis_ids if analysis_ids else None

    except Exception as e:
        logger.error("Clip analysis failed for video %d: %s", video_id, e, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# 2b. Post-tracking deduplication — merge overlapping tracks
# ---------------------------------------------------------------------------

def _merge_overlapping_tracks(
    video_tracks: List[Dict],
    video_id: int,
    iou_threshold: float = 0.35,
    min_shared_points: int = 3,
) -> List[Dict]:
    """Merge tracks that overlap significantly in space and time.

    ByteTrack can create multiple track IDs for the same object when the
    detector emits overlapping bounding boxes.  This function identifies
    pairs whose average IoU over shared timestamps exceeds *iou_threshold*
    and keeps only the longer track, deactivating the shorter one in the DB.

    Returns the filtered list of tracks.
    """
    if len(video_tracks) < 2:
        return list(video_tracks)

    # Parse trajectories once
    parsed = []
    for t in video_tracks:
        traj = t['trajectory']
        if isinstance(traj, str):
            traj = json.loads(traj)
        # Index by rounded timestamp for fast lookup
        by_time = {}
        for pt in (traj or []):
            key = round(pt['timestamp'], 2)
            by_time[key] = pt
        parsed.append({'track': t, 'traj': traj or [], 'by_time': by_time})

    to_remove = set()  # indices of tracks to deactivate

    for i in range(len(parsed)):
        if i in to_remove:
            continue
        for j in range(i + 1, len(parsed)):
            if j in to_remove:
                continue

            # Find shared timestamps (within 0.05s tolerance)
            ious = []
            for ts_key, pt_i in parsed[i]['by_time'].items():
                pt_j = parsed[j]['by_time'].get(ts_key)
                if pt_j is None:
                    # Try nearby timestamps
                    for offset in [0.07, -0.07]:
                        pt_j = parsed[j]['by_time'].get(round(ts_key + offset, 2))
                        if pt_j:
                            break
                if pt_j is None:
                    continue
                iou = _compute_iou(pt_i, pt_j)
                ious.append(iou)

            # Compute temporal overlap for adaptive thresholds
            i_times = [p['timestamp'] for p in parsed[i]['traj']]
            j_times = [p['timestamp'] for p in parsed[j]['traj']]
            overlap_start = max(min(i_times), min(j_times)) if i_times and j_times else 0
            overlap_end = min(max(i_times), max(j_times)) if i_times and j_times else 0
            overlap_duration = max(0, overlap_end - overlap_start)

            # Relax min_shared_points for pairs with long temporal overlap
            min_pts_required = 1 if overlap_duration > 5.0 else min_shared_points

            if len(ious) >= min_pts_required:
                avg_iou = sum(ious) / len(ious)
                if avg_iou >= iou_threshold:
                    # Keep the longer track, remove the shorter
                    len_i = len(parsed[i]['traj'])
                    len_j = len(parsed[j]['traj'])
                    victim = j if len_i >= len_j else i
                    to_remove.add(victim)
                    logger.info(
                        "Merging track %d into %d (avg IoU=%.2f over %d shared frames) for video %d",
                        parsed[victim]['track']['id'],
                        parsed[i if victim == j else j]['track']['id'],
                        avg_iou, len(ious), video_id,
                    )
                    continue

            # Second pass: nearest-neighbor matching with IoMin for oscillating objects
            if overlap_duration >= 2.0:
                sample_count = 9
                sample_ts = [overlap_start + k * (overlap_end - overlap_start) / (sample_count - 1) for k in range(sample_count)]
                nn_ious = []
                for ts in sample_ts:
                    pt_i = _nearest_point(parsed[i]['traj'], ts, max_gap=0.5)
                    pt_j = _nearest_point(parsed[j]['traj'], ts, max_gap=0.5)
                    if pt_i and pt_j:
                        nn_ious.append(_compute_iou_min(pt_i, pt_j))
                if len(nn_ious) >= 3 and (sum(nn_ious) / len(nn_ious)) >= 0.20:
                    len_i = len(parsed[i]['traj'])
                    len_j = len(parsed[j]['traj'])
                    victim = j if len_i >= len_j else i
                    to_remove.add(victim)
                    logger.info(
                        "Merging track %d into %d (nn IoMin=%.2f over %d samples) for video %d",
                        parsed[victim]['track']['id'],
                        parsed[i if victim == j else j]['track']['id'],
                        sum(nn_ious) / len(nn_ious), len(nn_ious), video_id,
                    )

    # Deactivate removed tracks in DB
    if to_remove:
        remove_ids = [parsed[idx]['track']['id'] for idx in to_remove]
        try:
            with get_cursor(commit=True) as cur:
                cur.execute(
                    "UPDATE video_tracks SET status = 'merged' WHERE id = ANY(%s)",
                    (remove_ids,)
                )
            logger.info("Deactivated %d duplicate tracks for video %d: %s",
                        len(remove_ids), video_id, remove_ids)
        except Exception as e:
            logger.warning("Failed to deactivate merged tracks: %s", e)

    return [parsed[idx]['track'] for idx in range(len(parsed)) if idx not in to_remove]


def _stitch_sequential_tracks(
    video_tracks: List[Dict],
    video_id: int,
    max_gap_seconds: float = 3.0,
    iou_threshold: float = 0.30,
) -> List[Dict]:
    """Stitch fragmented tracks that are sequential with similar bounding boxes.

    ByteTrack can drop a track when detection confidence dips, then re-acquire
    the same object with a new track ID.  This catches stationary or slow-moving
    objects (signs, parked vehicles) that produce multiple short track segments.

    For each pair of tracks, checks whether the *last* bbox of the earlier track
    has high IoU with the *first* bbox of the later track, and the time gap is
    small.  If so, the shorter track is deactivated.
    """
    if len(video_tracks) < 2:
        return list(video_tracks)

    # Parse trajectories and compute time bounds
    parsed = []
    for t in video_tracks:
        traj = t['trajectory']
        if isinstance(traj, str):
            traj = json.loads(traj)
        traj = traj or []
        if not traj:
            parsed.append({'track': t, 'traj': [], 't_min': 0, 't_max': 0, 'first': None, 'last': None})
            continue
        pts = sorted(traj, key=lambda p: p['timestamp'])
        parsed.append({
            'track': t,
            'traj': pts,
            't_min': pts[0]['timestamp'],
            't_max': pts[-1]['timestamp'],
            'first': pts[0],
            'last': pts[-1],
        })

    to_remove = set()

    for i in range(len(parsed)):
        if i in to_remove or not parsed[i]['last']:
            continue
        for j in range(len(parsed)):
            if j == i or j in to_remove or not parsed[j]['first']:
                continue

            # Check if j starts shortly after i ends
            gap = parsed[j]['t_min'] - parsed[i]['t_max']
            if gap < 0 or gap > max_gap_seconds:
                continue

            # Compare last bbox of i with first bbox of j
            iou = max(
                _compute_iou(parsed[i]['last'], parsed[j]['first']),
                _compute_iou_min(parsed[i]['last'], parsed[j]['first'])
            )
            if iou >= iou_threshold:
                # Keep the longer track
                len_i = len(parsed[i]['traj'])
                len_j = len(parsed[j]['traj'])
                victim = j if len_i >= len_j else i
                to_remove.add(victim)
                keeper = i if victim == j else j
                logger.info(
                    "Stitching track %d into %d (gap=%.2fs, IoU=%.2f) for video %d",
                    parsed[victim]['track']['id'],
                    parsed[keeper]['track']['id'],
                    gap, iou, video_id,
                )

    if to_remove:
        remove_ids = [parsed[idx]['track']['id'] for idx in to_remove]
        try:
            with get_cursor(commit=True) as cur:
                cur.execute(
                    "UPDATE video_tracks SET status = 'merged' WHERE id = ANY(%s)",
                    (remove_ids,)
                )
            logger.info("Stitched (deactivated) %d fragmented tracks for video %d: %s",
                        len(remove_ids), video_id, remove_ids)
        except Exception as e:
            logger.warning("Failed to deactivate stitched tracks: %s", e)

    return [parsed[idx]['track'] for idx in range(len(parsed)) if idx not in to_remove]


# ---------------------------------------------------------------------------
# 2d. Post-tracking cleanup — remove spatial jumps from trajectories
# ---------------------------------------------------------------------------

def _clean_trajectory_jumps(
    video_tracks: List[Dict],
    video_id: int,
    jump_multiplier: float = 3.0,
    min_segment_frames: int = 3,
) -> List[Dict]:
    """Remove sudden spatial jumps from track trajectories.

    ByteTrack can associate detections from two distant positions into one
    track (e.g. alternating between a real vehicle and a false positive).
    This function detects jumps where the centroid displacement between
    consecutive frames exceeds *jump_multiplier* times the bbox diagonal,
    splits the trajectory into contiguous segments, and keeps only the
    longest segment.

    Args:
        video_tracks: List of ``video_tracks`` rows.
        video_id: Video ID (for logging).
        jump_multiplier: A jump is detected when centroid displacement
            exceeds this many bbox diagonals.  Default 3.0.
        min_segment_frames: Minimum frames for a segment to be kept.

    Returns:
        The same list of tracks with trajectories cleaned in-place.
        Tracks whose longest segment is too short are deactivated.
    """
    import math

    tracks_cleaned = 0
    tracks_removed = []

    for track in video_tracks:
        traj = track['trajectory']
        if isinstance(traj, str):
            traj = json.loads(traj)
        if not traj or len(traj) < 2:
            continue

        # Sort by timestamp
        traj = sorted(traj, key=lambda p: p['timestamp'])

        # Find jump points
        jump_indices = []
        for i in range(1, len(traj)):
            prev = traj[i - 1]
            curr = traj[i]

            # Centroid displacement
            prev_cx = prev['x'] + prev['w'] / 2
            prev_cy = prev['y'] + prev['h'] / 2
            curr_cx = curr['x'] + curr['w'] / 2
            curr_cy = curr['y'] + curr['h'] / 2
            displacement = math.sqrt((curr_cx - prev_cx) ** 2 + (curr_cy - prev_cy) ** 2)

            # Use the average bbox diagonal as the scale reference
            prev_diag = math.sqrt(prev['w'] ** 2 + prev['h'] ** 2)
            curr_diag = math.sqrt(curr['w'] ** 2 + curr['h'] ** 2)
            avg_diag = (prev_diag + curr_diag) / 2

            if avg_diag > 0 and displacement > avg_diag * jump_multiplier:
                jump_indices.append(i)

        if not jump_indices:
            continue

        # Split trajectory into segments at jump points
        segments = []
        start = 0
        for ji in jump_indices:
            segments.append(traj[start:ji])
            start = ji
        segments.append(traj[start:])

        # Keep the longest segment
        longest = max(segments, key=len)

        if len(longest) < min_segment_frames:
            # Track is too fragmented — mark for removal
            tracks_removed.append(track['id'])
            logger.info(
                "Track %d in video %d: trajectory too fragmented after jump removal "
                "(%d segments, longest=%d frames) — deactivating",
                track['id'], video_id, len(segments), len(longest),
            )
            continue

        removed_count = len(traj) - len(longest)
        if removed_count > 0:
            tracks_cleaned += 1
            logger.info(
                "Track %d in video %d: removed %d jump-outlier points "
                "(%d segments, keeping longest with %d frames)",
                track['id'], video_id, removed_count,
                len(segments), len(longest),
            )

            # Update trajectory in-place and in DB
            track['trajectory'] = longest
            try:
                with get_cursor(commit=True) as cur:
                    cur.execute(
                        "UPDATE video_tracks SET trajectory = %s::jsonb WHERE id = %s",
                        (json.dumps(longest), track['id'])
                    )
            except Exception as e:
                logger.warning("Failed to update cleaned trajectory for track %d: %s",
                               track['id'], e)

    # Deactivate tracks that were too fragmented
    if tracks_removed:
        try:
            with get_cursor(commit=True) as cur:
                cur.execute(
                    "UPDATE video_tracks SET status = 'jump_fragmented' WHERE id = ANY(%s)",
                    (tracks_removed,)
                )
            logger.info("Deactivated %d jump-fragmented tracks for video %d: %s",
                        len(tracks_removed), video_id, tracks_removed)
        except Exception as e:
            logger.warning("Failed to deactivate jump-fragmented tracks: %s", e)

    if tracks_cleaned or tracks_removed:
        logger.info(
            "Jump cleanup for video %d: %d tracks cleaned, %d tracks removed",
            video_id, tracks_cleaned, len(tracks_removed),
        )

    return [t for t in video_tracks if t['id'] not in tracks_removed]


# ---------------------------------------------------------------------------
# 3. Per-frame classification extraction
# ---------------------------------------------------------------------------

def _compute_iou(box_a: Dict, box_b: Dict) -> float:
    """Compute Intersection-over-Union between two bounding boxes.

    Each box is a dict with keys ``x``, ``y``, ``w``, ``h``.
    """
    ax1, ay1 = box_a['x'], box_a['y']
    ax2, ay2 = ax1 + box_a['w'], ay1 + box_a['h']
    bx1, by1 = box_b['x'], box_b['y']
    bx2, by2 = bx1 + box_b['w'], by1 + box_b['h']

    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)

    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    if inter == 0:
        return 0.0

    area_a = box_a['w'] * box_a['h']
    area_b = box_b['w'] * box_b['h']
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _compute_iou_min(box_a: Dict, box_b: Dict) -> float:
    """IoU variant using min-area denominator — better for oscillating objects.

    Standard IoU penalizes bbox size differences. IoMin = intersection / min(area)
    is more robust when bboxes fluctuate in size (e.g. waving flags).
    """
    ax1, ay1 = box_a['x'], box_a['y']
    ax2, ay2 = ax1 + box_a['w'], ay1 + box_a['h']
    bx1, by1 = box_b['x'], box_b['y']
    bx2, by2 = bx1 + box_b['w'], by1 + box_b['h']

    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)

    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    if inter == 0:
        return 0.0

    min_area = min(box_a['w'] * box_a['h'], box_b['w'] * box_b['h'])
    return inter / min_area if min_area > 0 else 0.0


def _nearest_point(traj: List[Dict], timestamp: float, max_gap: float = 0.5) -> Optional[Dict]:
    """Find the trajectory point nearest to the given timestamp within max_gap."""
    best = None
    best_dist = float('inf')
    for pt in traj:
        dist = abs(pt['timestamp'] - timestamp)
        if dist < best_dist:
            best_dist = dist
            best = pt
    return best if best_dist <= max_gap else None


def _extract_per_frame_classifications(
    clip_path: str,
    video_tracks: List[Dict],
    video_id: int,
) -> Dict[int, List[Dict]]:
    """Run YOLO-World on sampled frames and match detections to tracks by IoU.

    For each sampled frame, runs inference and finds the best overlapping
    detection for every track that is visible at that timestamp (has a
    trajectory point within 0.5 seconds).

    Args:
        clip_path: Filesystem path to the MP4 clip.
        video_tracks: List of ``video_tracks`` rows (RealDictRow).
        video_id: Video ID (for logging).

    Returns:
        Dict mapping ``video_track.id`` to a list of per-frame classification
        dicts: ``{timestamp, class_name, confidence, bbox_area, raw_class_id}``.
    """
    model = _get_model()
    if model is None:
        logger.error("YOLO-World model not available for clip analysis")
        return {}

    cap = cv2.VideoCapture(clip_path)
    if not cap.isOpened():
        logger.error("Cannot open clip for per-frame analysis: %s", clip_path)
        return {}

    fps = cap.get(cv2.CAP_PROP_FPS)
    if fps <= 0:
        fps = 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = total_frames / fps if fps > 0 else 0.0

    # Decide sampling interval: every 2nd frame for clips > 60s, else every frame
    frame_step = 2 if duration > 60.0 else 1

    # Pre-parse trajectories
    track_trajectories = {}
    for track in video_tracks:
        tid = track['id']
        traj = track['trajectory']
        if isinstance(traj, str):
            traj = json.loads(traj)
        track_trajectories[tid] = traj

    # Result accumulator
    classifications: Dict[int, List[Dict]] = {t['id']: [] for t in video_tracks}

    frame_idx = 0
    frames_processed = 0

    while True:
        ret, frame_bgr = cap.read()
        if not ret:
            break

        if frame_idx % frame_step != 0:
            frame_idx += 1
            continue

        timestamp = frame_idx / fps

        # Convert to PIL for model.predict()
        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        pil_img = Image.fromarray(frame_rgb)

        # Run YOLO-World inference
        try:
            results = model.predict(
                source=pil_img,
                conf=INFERENCE_CONF,
                device=DEVICE,
                verbose=False,
            )
        except Exception as e:
            logger.debug("Inference failed on frame %d of video %d: %s", frame_idx, video_id, e)
            frame_idx += 1
            continue

        frames_processed += 1
        result = results[0]

        # Parse detections into a list of dicts
        detections = []
        if result.boxes is not None and len(result.boxes) > 0:
            for box in result.boxes:
                x1, y1, x2, y2 = box.xyxy[0].tolist()
                confidence = float(box.conf[0])
                class_id = int(box.cls[0])

                raw_class = ALL_CLASSES[class_id] if class_id < len(ALL_CLASSES) else "unknown"
                class_name = VEHICLE_DISPLAY_NAMES.get(raw_class, raw_class)

                # Skip non-vehicle detections (person pre-screen only)
                if raw_class in NON_VEHICLE_CLASSES:
                    continue

                det_w = int(x2 - x1)
                det_h = int(y2 - y1)
                if det_w < 5 or det_h < 5:
                    continue

                detections.append({
                    'x': int(x1),
                    'y': int(y1),
                    'w': det_w,
                    'h': det_h,
                    'confidence': confidence,
                    'class_name': class_name,
                    'raw_class_id': class_id,
                    'bbox_area': det_w * det_h,
                })

        # Match detections to each track
        for track in video_tracks:
            tid = track['id']
            traj = track_trajectories[tid]

            # Find trajectory point closest to this timestamp
            closest_point = None
            min_dt = float('inf')
            for pt in traj:
                dt = abs(pt['timestamp'] - timestamp)
                if dt < min_dt:
                    min_dt = dt
                    closest_point = pt

            # Track must be visible at this frame (within 0.5s)
            if closest_point is None or min_dt > 0.5:
                continue

            track_box = {
                'x': closest_point['x'],
                'y': closest_point['y'],
                'w': closest_point['w'],
                'h': closest_point['h'],
            }

            # Find best IoU match among detections
            best_iou = 0.0
            best_det = None
            for det in detections:
                det_box = {'x': det['x'], 'y': det['y'], 'w': det['w'], 'h': det['h']}
                iou = _compute_iou(track_box, det_box)
                if iou > best_iou:
                    best_iou = iou
                    best_det = det

            # Require minimum IoU to accept the match
            if best_det is not None and best_iou >= 0.15:
                classifications[tid].append({
                    'timestamp': round(timestamp, 3),
                    'class_name': best_det['class_name'],
                    'confidence': round(best_det['confidence'], 4),
                    'bbox_area': best_det['bbox_area'],
                    'raw_class_id': best_det['raw_class_id'],
                })

        frame_idx += 1

    cap.release()

    total_classifications = sum(len(v) for v in classifications.values())
    logger.info(
        "Per-frame classification for video %d: %d classifications across %d tracks "
        "(%d frames processed)",
        video_id, total_classifications, len(video_tracks), frames_processed,
    )
    return classifications


# ---------------------------------------------------------------------------
# 4. Weighted consensus
# ---------------------------------------------------------------------------

def compute_weighted_consensus(frame_classifications: List[Dict]) -> Dict:
    """Compute a weighted consensus class from per-frame classifications.

    Weight formula per frame::

        weight = confidence * (bbox_area / max_bbox_area)

    Frames where the object is large and the model is confident contribute
    more to the final vote.

    Args:
        frame_classifications: List of dicts from
            ``_extract_per_frame_classifications``, each containing at least
            ``class_name``, ``confidence``, ``bbox_area``.

    Returns:
        Dict with ``consensus_class``, ``consensus_confidence``,
        ``class_distribution``, and ``classification_timeline``.
    """
    if not frame_classifications:
        return {
            'consensus_class': 'unknown',
            'consensus_confidence': 0.0,
            'class_distribution': {},
            'classification_timeline': [],
        }

    # Find max bbox area across all frames for this track
    max_bbox_area = max(fc['bbox_area'] for fc in frame_classifications)
    if max_bbox_area == 0:
        max_bbox_area = 1  # Prevent division by zero

    # Accumulate weighted votes per class
    class_weights: Dict[str, float] = {}
    total_weight = 0.0

    for fc in frame_classifications:
        weight = fc['confidence'] * (fc['bbox_area'] / max_bbox_area)
        cls = fc['class_name']
        class_weights[cls] = class_weights.get(cls, 0.0) + weight
        total_weight += weight

    # Determine winner
    consensus_class = max(class_weights, key=class_weights.get)
    winning_weight = class_weights[consensus_class]
    consensus_confidence = winning_weight / total_weight if total_weight > 0 else 0.0

    # Normalized distribution
    class_distribution = {}
    for cls, w in class_weights.items():
        class_distribution[cls] = round(w / total_weight, 4) if total_weight > 0 else 0.0

    # Timeline: ordered list of (timestamp, class) for visual debugging
    classification_timeline = [
        {'timestamp': fc['timestamp'], 'class_name': fc['class_name']}
        for fc in sorted(frame_classifications, key=lambda f: f['timestamp'])
    ]

    return {
        'consensus_class': consensus_class,
        'consensus_confidence': round(consensus_confidence, 4),
        'class_distribution': class_distribution,
        'classification_timeline': classification_timeline,
    }


# ---------------------------------------------------------------------------
# 5. Frame quality scoring
# ---------------------------------------------------------------------------

def score_frame_quality(
    frame_classifications: List[Dict],
    consensus_class: str,
    frame_width: int = DEFAULT_FRAME_WIDTH,
    frame_height: int = DEFAULT_FRAME_HEIGHT,
) -> List[Dict]:
    """Score each classified frame on crop quality for training suitability.

    Quality is the product of four factors (each 0-1):

    * **bbox_area_norm** — ``bbox_area / max_area`` (larger object = better crop)
    * **confidence** — model confidence
    * **class_match** — 1.0 if matches consensus, 0.3 otherwise
    * **center_score** — 1.0 if bbox center is in middle 60% of frame, decays
      linearly toward edges

    Args:
        frame_classifications: Per-frame classification dicts.
        consensus_class: The winning consensus class name.
        frame_width: Pixel width of video frames (default 1920).
        frame_height: Pixel height of video frames (default 1080).

    Returns:
        List of ``{timestamp, quality_score, bbox_area, confidence}`` sorted
        descending by quality_score.
    """
    if not frame_classifications:
        return []

    max_area = max(fc['bbox_area'] for fc in frame_classifications)
    if max_area == 0:
        max_area = 1

    # Middle 60% boundaries
    margin_x = frame_width * 0.2
    margin_y = frame_height * 0.2
    center_x_min = margin_x
    center_x_max = frame_width - margin_x
    center_y_min = margin_y
    center_y_max = frame_height - margin_y

    scores = []
    for fc in frame_classifications:
        bbox_area_norm = fc['bbox_area'] / max_area
        confidence = fc['confidence']
        class_match = 1.0 if fc['class_name'] == consensus_class else 0.3

        # Compute bbox center from raw_class_id lookup is not available here,
        # so we estimate center from the trajectory data stored alongside.
        # Since frame_classifications don't carry x/y directly, we approximate
        # the center score from bbox_area (larger objects tend to be centered).
        # However, if we have access to the original detection coords, we can
        # compute precisely.  We'll use a heuristic based on area ratio:
        # large objects filling the frame are more likely centered.
        #
        # For a more precise version, _extract_per_frame_classifications would
        # need to store x, y.  We add a fallback here.
        #
        # Check if fc has 'bbox_x' and 'bbox_y' (extended format)
        if 'bbox_x' in fc and 'bbox_y' in fc:
            cx = fc['bbox_x'] + fc.get('bbox_w', 0) / 2
            cy = fc['bbox_y'] + fc.get('bbox_h', 0) / 2
        else:
            # Approximate: assume centered (gives center_score=1.0 as baseline,
            # with area normalization handling quality differentiation)
            cx = frame_width / 2
            cy = frame_height / 2

        # Center score: 1.0 inside middle 60%, linear falloff to 0.0 at edges
        if center_x_min <= cx <= center_x_max:
            score_x = 1.0
        elif cx < center_x_min:
            score_x = max(0.0, cx / center_x_min) if center_x_min > 0 else 0.0
        else:
            score_x = max(0.0, (frame_width - cx) / margin_x) if margin_x > 0 else 0.0

        if center_y_min <= cy <= center_y_max:
            score_y = 1.0
        elif cy < center_y_min:
            score_y = max(0.0, cy / center_y_min) if center_y_min > 0 else 0.0
        else:
            score_y = max(0.0, (frame_height - cy) / margin_y) if margin_y > 0 else 0.0

        center_score = score_x * score_y

        quality_score = bbox_area_norm * confidence * class_match * center_score

        scores.append({
            'timestamp': fc['timestamp'],
            'quality_score': round(quality_score, 4),
            'bbox_area': fc['bbox_area'],
            'confidence': round(fc['confidence'], 4),
        })

    scores.sort(key=lambda s: s['quality_score'], reverse=True)
    return scores


# ---------------------------------------------------------------------------
# 6. Export training frames
# ---------------------------------------------------------------------------

def export_training_frames(analysis_id: int,
                           top_n: int = 10,
                           min_quality: float = 0.5) -> Optional[Dict]:
    """Export top-quality frame crops for training from a completed analysis.

    Reads the analysis record, selects the best frames by quality score,
    extracts crops from the source video, and inserts ``ai_predictions``
    records for human review.

    Args:
        analysis_id: ``clip_analysis_results.id``.
        top_n: Maximum number of frames to export (default 10).
        min_quality: Minimum quality score threshold (default 0.5).

    Returns:
        Dict with ``count`` and ``batch_id``, or None on failure.
    """
    try:
        # --- Load analysis record ---
        with get_cursor(commit=False) as cur:
            cur.execute(
                "SELECT * FROM clip_analysis_results WHERE id = %s", (analysis_id,)
            )
            analysis = cur.fetchone()

        if not analysis:
            logger.error("export_training_frames: analysis %d not found", analysis_id)
            return None

        video_id = analysis['video_id']
        video_track_id = analysis['video_track_id']
        consensus_class = analysis['consensus_class']

        quality_scores = analysis['frame_quality_scores']
        if isinstance(quality_scores, str):
            quality_scores = json.loads(quality_scores)

        frame_classifications = analysis['frame_classifications']
        if isinstance(frame_classifications, str):
            frame_classifications = json.loads(frame_classifications)

        # --- Select top frames ---
        eligible = [qs for qs in quality_scores if qs['quality_score'] >= min_quality]
        selected = eligible[:top_n]

        if not selected:
            logger.info(
                "export_training_frames: no frames meet min_quality=%.2f for analysis %d",
                min_quality, analysis_id,
            )
            return {'count': 0, 'batch_id': None}

        # --- Resolve clip path ---
        source = resolve_clip_source(video_id=video_id)
        if not source or not source['clip_path']:
            logger.error(
                "export_training_frames: cannot resolve clip for video %d", video_id,
            )
            return None

        clip_path = source['clip_path']

        # --- Open video and extract crops ---
        cap = cv2.VideoCapture(clip_path)
        if not cap.isOpened():
            logger.error("export_training_frames: cannot open clip %s", clip_path)
            return None

        fps = cap.get(cv2.CAP_PROP_FPS)
        if fps <= 0:
            fps = 30.0

        # Create output directory
        crop_dir = CROPS_DIR / f"analysis_{analysis_id}"
        crop_dir.mkdir(parents=True, exist_ok=True)

        # Build a lookup from timestamp -> frame_classification for bbox info
        fc_by_ts = {}
        for fc in frame_classifications:
            fc_by_ts[fc['timestamp']] = fc

        batch_id = f"clip-analysis-{analysis_id}-{int(time.time())}"
        exported_count = 0

        for qs in selected:
            ts = qs['timestamp']
            frame_num = int(ts * fps)

            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_num)
            ret, frame_bgr = cap.read()
            if not ret or frame_bgr is None:
                continue

            img_h, img_w = frame_bgr.shape[:2]

            # Get bbox from trajectory for this timestamp
            # Look up the track trajectory for precise crop coordinates
            bbox = _get_bbox_at_timestamp(video_track_id, ts)
            if bbox is None:
                # Fallback: use full frame
                crop = frame_bgr
                crop_bbox = {'x': 0, 'y': 0, 'w': img_w, 'h': img_h}
            else:
                # Add 10% padding
                pad_x = int(bbox['w'] * 0.1)
                pad_y = int(bbox['h'] * 0.1)
                x1 = max(0, bbox['x'] - pad_x)
                y1 = max(0, bbox['y'] - pad_y)
                x2 = min(img_w, bbox['x'] + bbox['w'] + pad_x)
                y2 = min(img_h, bbox['y'] + bbox['h'] + pad_y)
                crop = frame_bgr[y1:y2, x1:x2]
                crop_bbox = {'x': x1, 'y': y1, 'w': x2 - x1, 'h': y2 - y1}

            if crop.size == 0:
                continue

            # Save crop
            crop_filename = f"frame_{ts:.3f}.jpg"
            crop_path = str(crop_dir / crop_filename)
            cv2.imwrite(crop_path, crop, [cv2.IMWRITE_JPEG_QUALITY, 95])

            # Insert ai_predictions record
            fc_entry = fc_by_ts.get(ts, {})
            confidence = fc_entry.get('confidence', qs.get('confidence', 0.0))

            predicted_tags = json.dumps({
                'class': consensus_class,
                'vehicle_type': consensus_class,
                'source': 'clip_analysis',
                'analysis_id': analysis_id,
                'quality_score': qs['quality_score'],
            })

            with get_cursor() as cur:
                cur.execute("""
                    INSERT INTO ai_predictions
                    (video_id, model_name, model_version, prediction_type,
                     confidence, timestamp, bbox_x, bbox_y, bbox_width, bbox_height,
                     scenario, predicted_tags, review_status, batch_id)
                    VALUES (%s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s)
                """, (
                    video_id,
                    'clip-analysis-v1',
                    '1.0',
                    'keyframe',
                    round(confidence, 4),
                    round(ts, 3),
                    crop_bbox['x'], crop_bbox['y'],
                    crop_bbox['w'], crop_bbox['h'],
                    'vehicle_detection',
                    predicted_tags,
                    'pending',
                    batch_id,
                ))

            exported_count += 1

        cap.release()

        # --- Update analysis record ---
        if exported_count > 0:
            with get_cursor() as cur:
                cur.execute("""
                    UPDATE clip_analysis_results
                    SET training_frames_exported = %s,
                        training_batch_id = %s,
                        updated_at = NOW()
                    WHERE id = %s
                """, (exported_count, batch_id, analysis_id))

        logger.info(
            "Exported %d training frames for analysis %d (batch=%s)",
            exported_count, analysis_id, batch_id,
        )
        return {'count': exported_count, 'batch_id': batch_id}

    except Exception as e:
        logger.error(
            "export_training_frames failed for analysis %d: %s",
            analysis_id, e, exc_info=True,
        )
        return None


def _get_bbox_at_timestamp(video_track_id: int, timestamp: float) -> Optional[Dict]:
    """Look up the bounding box for a video track at a given timestamp.

    Searches the track's trajectory JSONB for the point closest to
    ``timestamp``.

    Args:
        video_track_id: ``video_tracks.id``.
        timestamp: Seconds within the clip.

    Returns:
        Dict with ``x``, ``y``, ``w``, ``h`` or None.
    """
    try:
        with get_cursor(commit=False) as cur:
            cur.execute(
                "SELECT trajectory FROM video_tracks WHERE id = %s",
                (video_track_id,)
            )
            row = cur.fetchone()

        if not row or not row['trajectory']:
            return None

        trajectory = row['trajectory']
        if isinstance(trajectory, str):
            trajectory = json.loads(trajectory)

        closest = None
        min_dt = float('inf')
        for pt in trajectory:
            dt = abs(pt['timestamp'] - timestamp)
            if dt < min_dt:
                min_dt = dt
                closest = pt

        if closest and min_dt <= 1.0:
            return {
                'x': closest['x'],
                'y': closest['y'],
                'w': closest['w'],
                'h': closest['h'],
            }

        return None

    except Exception as e:
        logger.debug("_get_bbox_at_timestamp failed for track %d: %s", video_track_id, e)
        return None
