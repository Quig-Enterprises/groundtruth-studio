"""
Correction-based calibration pipeline for per-camera velocity correction factors.

Reads bbox corrections from feedback/bbox_corrections.jsonl and computes
per-camera calibration factors for motion projection.
"""

import json
import os
from pathlib import Path
from datetime import datetime
from collections import defaultdict
import logging

logger = logging.getLogger(__name__)

# Minimum number of corrections required before we trust the calibration
MIN_CORRECTIONS = 3

# Default estimate: assume detection is lost around 40-50% through the clip
# (based on empirical observation)
ASSUMED_LOSS_RATIO = 0.4


def get_feedback_dir():
    """Get the feedback directory path."""
    app_dir = Path(__file__).parent
    return app_dir / 'feedback'


def get_corrections_file():
    """Get the bbox corrections JSONL file path."""
    return get_feedback_dir() / 'bbox_corrections.jsonl'


def get_calibration_file():
    """Get the camera calibration JSON file path."""
    return get_feedback_dir() / 'camera_calibration.json'


def read_corrections():
    """
    Read bbox corrections from JSONL file.

    Returns:
        list: List of correction dicts
    """
    corrections_file = get_corrections_file()
    if not corrections_file.exists():
        return []

    corrections = []
    with open(corrections_file, 'r') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                corrections.append(entry)
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse correction line: {e}")
                continue

    return corrections


def compute_calibration():
    """
    Compute per-camera calibration factors from bbox corrections.

    Returns:
        dict: Per-camera calibration data
            {
                "camera_id": {
                    "sample_count": int,
                    "avg_position_error_x": float,
                    "avg_position_error_y": float,
                    "avg_velocity_error_x": float,
                    "avg_velocity_error_y": float,
                    "velocity_multiplier_x": float,
                    "velocity_multiplier_y": float,
                    "last_updated": str (ISO timestamp)
                },
                ...
            }
    """
    corrections = read_corrections()

    # Filter for projected bboxes only (these are the motion model failures)
    projected_corrections = [
        c for c in corrections
        if (c.get('original_bbox') or {}).get('projected') is True
    ]

    if not projected_corrections:
        logger.info("No projected bbox corrections found")
        return {}

    # Group by camera_id
    by_camera = defaultdict(list)
    for correction in projected_corrections:
        camera_id = correction.get('camera_id')
        if camera_id:
            by_camera[camera_id].append(correction)

    # Compute calibration for each camera
    calibration = {}

    for camera_id, camera_corrections in by_camera.items():
        if len(camera_corrections) < MIN_CORRECTIONS:
            logger.info(f"Camera {camera_id}: only {len(camera_corrections)} corrections, need {MIN_CORRECTIONS}")
            continue

        # Accumulate errors
        position_errors_x = []
        position_errors_y = []
        velocity_errors_x = []
        velocity_errors_y = []

        for corr in camera_corrections:
            original = corr.get('original_bbox', {})
            corrected = corr.get('corrected_bbox', {})
            video_time = corr.get('video_time', 0)
            video_duration = corr.get('video_duration', 0)

            # Position errors
            pos_error_x = corrected.get('x', 0) - original.get('x', 0)
            pos_error_y = corrected.get('y', 0) - original.get('y', 0)

            position_errors_x.append(pos_error_x)
            position_errors_y.append(pos_error_y)

            # Estimate elapsed time since detection was lost
            # Assume detection lost around 40% through the clip
            detection_lost_at = video_duration * ASSUMED_LOSS_RATIO
            elapsed_time = video_time - detection_lost_at

            # Only compute velocity error if elapsed time is positive and reasonable
            if elapsed_time > 0 and elapsed_time < 10:
                vel_error_x = pos_error_x / elapsed_time
                vel_error_y = pos_error_y / elapsed_time

                velocity_errors_x.append(vel_error_x)
                velocity_errors_y.append(vel_error_y)

        # Compute averages
        avg_pos_error_x = sum(position_errors_x) / len(position_errors_x) if position_errors_x else 0
        avg_pos_error_y = sum(position_errors_y) / len(position_errors_y) if position_errors_y else 0

        avg_vel_error_x = sum(velocity_errors_x) / len(velocity_errors_x) if velocity_errors_x else 0
        avg_vel_error_y = sum(velocity_errors_y) / len(velocity_errors_y) if velocity_errors_y else 0

        # Compute velocity multipliers
        # If the average velocity error is positive, we're under-projecting (velocity too slow)
        # so we need to increase velocity. The multiplier is: (actual_vel) / (predicted_vel)
        # Since vel_error = (actual - predicted), we have: actual = predicted + vel_error
        # So: multiplier = (predicted + vel_error) / predicted = 1 + (vel_error / predicted)
        #
        # But we don't know "predicted" directly. We can estimate it from the position error and time:
        # If the projected position is off by X pixels after T seconds, and the error is E,
        # then the velocity should have been (predicted_v + E/T) instead of predicted_v.
        #
        # For simplicity, we'll compute a correction factor based on how much we need to
        # scale the velocity to eliminate the average error.

        # Simple heuristic: if we're consistently off by avg_vel_error, scale velocity
        # to compensate. This is a first-order approximation.
        # We'll use the ratio of corrected velocity to original velocity.

        # To avoid division by zero and extreme multipliers, cap the multiplier
        # We need to estimate the "base" velocity from the corrections
        # For now, use a simple heuristic: multiplier = 1 + (error_ratio)

        # Calculate multiplier from velocity errors
        # If avg_vel_error_x = 450 px/s, and we assume base velocity was ~100-200 px/s,
        # then multiplier ≈ 1 + (450/150) ≈ 4.0

        # Better approach: compute from actual data
        # For each correction, compute what multiplier would have fixed it
        multipliers_x = []
        multipliers_y = []

        for corr in camera_corrections:
            original = corr.get('original_bbox', {})
            corrected = corr.get('corrected_bbox', {})
            video_time = corr.get('video_time', 0)
            video_duration = corr.get('video_duration', 0)

            detection_lost_at = video_duration * ASSUMED_LOSS_RATIO
            elapsed_time = video_time - detection_lost_at

            if elapsed_time <= 0 or elapsed_time > 10:
                continue

            # Position delta
            pos_delta_x = corrected.get('x', 0) - original.get('x', 0)
            pos_delta_y = corrected.get('y', 0) - original.get('y', 0)

            # What velocity would have been correct?
            correct_vel_x = pos_delta_x / elapsed_time
            correct_vel_y = pos_delta_y / elapsed_time

            # We don't know the predicted velocity directly, but we can estimate it
            # from the position error. If the bbox moved from start to original position,
            # the predicted velocity was approximately:
            # predicted_v ≈ (original_x - start_x) / elapsed_time
            # But we don't have start_x.

            # Simpler: just use the ratio of needed correction to what we got
            # If we need to add correct_vel_x to fix it, and we assume the prediction
            # was some base velocity, the multiplier is the ratio.

            # For first version, use absolute velocity error as a fraction of a
            # typical base velocity (assume ~100 px/s as baseline for most scenarios)
            base_velocity_estimate = 100.0  # pixels/sec

            if abs(correct_vel_x) > 1:  # Avoid noise
                # Multiplier needed: (base + correction) / base
                multiplier_x = 1.0 + (correct_vel_x / base_velocity_estimate)
                multipliers_x.append(multiplier_x)

            if abs(correct_vel_y) > 1:
                multiplier_y = 1.0 + (correct_vel_y / base_velocity_estimate)
                multipliers_y.append(multiplier_y)

        # Average multipliers
        avg_mult_x = sum(multipliers_x) / len(multipliers_x) if multipliers_x else 1.0
        avg_mult_y = sum(multipliers_y) / len(multipliers_y) if multipliers_y else 1.0

        # Cap multipliers to reasonable range (0.1x to 10x)
        avg_mult_x = max(0.1, min(10.0, avg_mult_x))
        avg_mult_y = max(0.1, min(10.0, avg_mult_y))

        calibration[camera_id] = {
            'sample_count': len(camera_corrections),
            'avg_position_error_x': round(avg_pos_error_x, 2),
            'avg_position_error_y': round(avg_pos_error_y, 2),
            'avg_velocity_error_x': round(avg_vel_error_x, 2),
            'avg_velocity_error_y': round(avg_vel_error_y, 2),
            'velocity_multiplier_x': round(avg_mult_x, 2),
            'velocity_multiplier_y': round(avg_mult_y, 2),
            'last_updated': datetime.utcnow().isoformat() + 'Z'
        }

    return calibration


def save_calibration(calibration):
    """
    Save calibration to JSON file.

    Args:
        calibration (dict): Calibration data
    """
    calibration_file = get_calibration_file()
    calibration_file.parent.mkdir(parents=True, exist_ok=True)

    with open(calibration_file, 'w') as f:
        json.dump(calibration, f, indent=2)

    logger.info(f"Saved calibration for {len(calibration)} cameras to {calibration_file}")


def load_calibration():
    """
    Load calibration from JSON file.

    Returns:
        dict: Calibration data, or empty dict if file doesn't exist
    """
    calibration_file = get_calibration_file()
    if not calibration_file.exists():
        return {}

    try:
        with open(calibration_file, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Failed to load calibration: {e}")
        return {}


def get_calibration(camera_id):
    """
    Get calibration for a specific camera.

    Args:
        camera_id (str): Camera identifier

    Returns:
        dict or None: Calibration data for the camera, or None if not enough data
    """
    calibration = load_calibration()
    return calibration.get(camera_id)


def rebuild_calibration():
    """
    Rebuild calibration from corrections data.

    Returns:
        dict: New calibration data
    """
    calibration = compute_calibration()
    save_calibration(calibration)
    return calibration


if __name__ == '__main__':
    # CLI usage: python calibration.py
    logging.basicConfig(level=logging.INFO)

    print("Reading bbox corrections...")
    corrections = read_corrections()
    print(f"Found {len(corrections)} total corrections")

    projected = [c for c in corrections if c.get('original_bbox', {}).get('projected')]
    print(f"Found {len(projected)} projected corrections")

    print("\nComputing calibration...")
    calibration = compute_calibration()

    print(f"\nCalibration for {len(calibration)} cameras:")
    for camera_id, cal in calibration.items():
        print(f"\n{camera_id}:")
        print(f"  Samples: {cal['sample_count']}")
        print(f"  Avg position error: ({cal['avg_position_error_x']:.1f}, {cal['avg_position_error_y']:.1f}) px")
        print(f"  Avg velocity error: ({cal['avg_velocity_error_x']:.1f}, {cal['avg_velocity_error_y']:.1f}) px/s")
        print(f"  Velocity multiplier: ({cal['velocity_multiplier_x']:.2f}, {cal['velocity_multiplier_y']:.2f})")

    print("\nSaving calibration...")
    save_calibration(calibration)
    print(f"Saved to {get_calibration_file()}")
