"""
Spatial Scale Model for Vehicle Classification Pipeline

Provides a per-camera, per-class statistical model of expected object size at
each frame position. Used as a sanity-check voter: detections whose bounding
box dimensions fall outside the learned size distribution for that grid cell
are flagged as implausible.

The frame is divided into a GRID_SIZE x GRID_SIZE grid. For each (camera,
classification, grid_x, grid_y) combination the module tracks running mean and
standard deviation of object width and height using Welford's online algorithm,
and maintains approximate 5th/95th percentile bounds.
"""

import logging
import math
from typing import Optional

from db_connection import get_cursor

logger = logging.getLogger(__name__)

# Grid resolution: divide the frame into GRID_SIZE x GRID_SIZE cells
GRID_SIZE = 10

# Minimum observations required before the model is considered trustworthy
MIN_OBSERVATIONS = 20


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _compute_grid_cell(bbox: dict, frame_dims: tuple) -> tuple:
    """Return (grid_x, grid_y) for the centre of bbox.

    Args:
        bbox: dict with x, y, width, height keys (pixel coords)
        frame_dims: (frame_width, frame_height) tuple

    Returns:
        (grid_x, grid_y) each in [0, GRID_SIZE-1]
    """
    frame_w, frame_h = frame_dims

    cx = bbox['x'] + bbox['width'] / 2.0
    cy = bbox['y'] + bbox['height'] / 2.0

    # Normalise to [0, 1) then scale to grid
    gx = int(cx / frame_w * GRID_SIZE)
    gy = int(cy / frame_h * GRID_SIZE)

    # Clamp to valid range (handles edge-on-boundary case)
    gx = max(0, min(gx, GRID_SIZE - 1))
    gy = max(0, min(gy, GRID_SIZE - 1))

    return gx, gy


def _welford_update(count: int, mean: float, m2: float, new_value: float) -> tuple:
    """One step of Welford's online algorithm for running mean and variance.

    Returns updated (count, mean, m2) where variance = m2 / count.
    """
    count += 1
    delta = new_value - mean
    mean += delta / count
    delta2 = new_value - mean
    m2 += delta * delta2
    return count, mean, m2


def _std_from_m2(count: int, m2: float) -> float:
    """Population standard deviation from Welford M2 accumulator."""
    if count < 2:
        return 0.0
    return math.sqrt(m2 / count)


def _approx_percentile_update(p5: Optional[float], p95: Optional[float],
                               mean: float, std: float) -> tuple:
    """Approximate percentile bounds from mean and std.

    Uses a Gaussian approximation:  p5  ~= mean - 1.645 * std
                                    p95 ~= mean + 1.645 * std

    This avoids storing all samples while still giving a reasonable estimate.
    The values are only used once MIN_OBSERVATIONS is reached, so early
    imprecision is harmless.
    """
    z = 1.645  # z-score for 5th/95th percentile under a normal distribution
    new_p5 = mean - z * std
    new_p95 = mean + z * std
    return new_p5, new_p95


# ---------------------------------------------------------------------------
# SpatialScaleModel
# ---------------------------------------------------------------------------

class SpatialScaleModel:
    """Per-camera, per-class spatial size model backed by PostgreSQL.

    Thread-safe: each public method acquires its own cursor via get_cursor().
    """

    def record_observation(self, camera_id: str, classification: str,
                           bbox: dict, frame_dims: tuple) -> None:
        """Record a single detection and update the running statistics.

        Args:
            camera_id: Unique camera identifier string
            classification: Object class label (e.g. 'car', 'truck')
            bbox: dict with keys x, y, width, height (pixel coordinates)
            frame_dims: (frame_width, frame_height) in pixels
        """
        if not camera_id or not classification:
            logger.warning("record_observation: camera_id and classification must be non-empty")
            return

        width = bbox.get('width')
        height = bbox.get('height')

        if width is None or height is None:
            logger.warning("record_observation: bbox missing width or height")
            return

        width = float(width)
        height = float(height)

        if width <= 0 or height <= 0:
            logger.debug(
                "record_observation: skipping zero/negative-size bbox "
                "(w=%.1f, h=%.1f) for %s/%s", width, height, camera_id, classification
            )
            return

        frame_w, frame_h = frame_dims
        if frame_w <= 0 or frame_h <= 0:
            logger.warning("record_observation: invalid frame_dims %s", frame_dims)
            return

        try:
            grid_x, grid_y = _compute_grid_cell(bbox, frame_dims)
        except Exception as e:
            logger.warning("record_observation: could not compute grid cell: %s", e)
            return

        try:
            with get_cursor() as cursor:
                # Fetch existing row so we can apply Welford update in Python
                cursor.execute(
                    """
                    SELECT sample_count,
                           mean_width,  mean_height,
                           std_width,   std_height,
                           p5_width,    p95_width,
                           p5_height,   p95_height
                    FROM spatial_scale_models
                    WHERE camera_id = %s
                      AND classification = %s
                      AND grid_x = %s
                      AND grid_y = %s
                    FOR UPDATE
                    """,
                    (camera_id, classification, grid_x, grid_y),
                )
                row = cursor.fetchone()

                if row is None:
                    # First observation for this cell
                    new_count = 1
                    new_mean_w = width
                    new_mean_h = height
                    new_std_w  = 0.0
                    new_std_h  = 0.0
                    new_p5_w   = width
                    new_p95_w  = width
                    new_p5_h   = height
                    new_p95_h  = height
                else:
                    n          = row['sample_count'] or 0
                    old_mean_w = row['mean_width']   or 0.0
                    old_mean_h = row['mean_height']  or 0.0
                    old_std_w  = row['std_width']    or 0.0
                    old_std_h  = row['std_height']   or 0.0

                    # Reconstruct M2 from stored std and count
                    m2_w = (old_std_w ** 2) * n
                    m2_h = (old_std_h ** 2) * n

                    # Welford update for width
                    new_count, new_mean_w, m2_w = _welford_update(n, old_mean_w, m2_w, width)
                    # Welford update for height (count already incremented above)
                    _, new_mean_h, m2_h = _welford_update(n, old_mean_h, m2_h, height)

                    new_std_w = _std_from_m2(new_count, m2_w)
                    new_std_h = _std_from_m2(new_count, m2_h)

                    new_p5_w, new_p95_w = _approx_percentile_update(
                        row['p5_width'], row['p95_width'], new_mean_w, new_std_w
                    )
                    new_p5_h, new_p95_h = _approx_percentile_update(
                        row['p5_height'], row['p95_height'], new_mean_h, new_std_h
                    )

                cursor.execute(
                    """
                    INSERT INTO spatial_scale_models
                        (camera_id, classification, grid_x, grid_y,
                         sample_count,
                         mean_width,  mean_height,
                         std_width,   std_height,
                         p5_width,    p95_width,
                         p5_height,   p95_height,
                         updated_at)
                    VALUES
                        (%s, %s, %s, %s,
                         %s,
                         %s, %s,
                         %s, %s,
                         %s, %s,
                         %s, %s,
                         NOW())
                    ON CONFLICT (camera_id, classification, grid_x, grid_y)
                    DO UPDATE SET
                        sample_count = EXCLUDED.sample_count,
                        mean_width   = EXCLUDED.mean_width,
                        mean_height  = EXCLUDED.mean_height,
                        std_width    = EXCLUDED.std_width,
                        std_height   = EXCLUDED.std_height,
                        p5_width     = EXCLUDED.p5_width,
                        p95_width    = EXCLUDED.p95_width,
                        p5_height    = EXCLUDED.p5_height,
                        p95_height   = EXCLUDED.p95_height,
                        updated_at   = NOW()
                    """,
                    (
                        camera_id, classification, grid_x, grid_y,
                        new_count,
                        new_mean_w, new_mean_h,
                        new_std_w,  new_std_h,
                        new_p5_w,   new_p95_w,
                        new_p5_h,   new_p95_h,
                    ),
                )

            logger.debug(
                "record_observation: %s/%s cell=(%d,%d) n=%d w=%.1f h=%.1f",
                camera_id, classification, grid_x, grid_y, new_count, width, height
            )

        except Exception as e:
            logger.error(
                "record_observation: DB error for %s/%s cell=(%d,%d): %s",
                camera_id, classification, grid_x, grid_y, e
            )

    def check_plausibility(self, camera_id: str, classification: str,
                           bbox: dict, frame_dims: tuple) -> dict:
        """Check whether a detection's size is plausible given the learned model.

        Args:
            camera_id: Unique camera identifier string
            classification: Object class label
            bbox: dict with keys x, y, width, height (pixel coordinates)
            frame_dims: (frame_width, frame_height) in pixels

        Returns:
            dict with keys:
                plausible (bool): True if size is within expected range
                z_score_width (float): z-score of the observed width
                z_score_height (float): z-score of the observed height
                expected_range (dict): {w_min, w_max, h_min, h_max} p5/p95 bounds

            If insufficient data (< MIN_OBSERVATIONS):
                {plausible: True, insufficient_data: True}

            If bbox or frame_dims are invalid:
                {plausible: True, insufficient_data: True, reason: str}
        """
        width  = bbox.get('width')
        height = bbox.get('height')

        if width is None or height is None:
            return {'plausible': True, 'insufficient_data': True, 'reason': 'invalid_bbox'}

        width  = float(width)
        height = float(height)

        if width <= 0 or height <= 0:
            return {'plausible': True, 'insufficient_data': True, 'reason': 'zero_size_bbox'}

        frame_w, frame_h = frame_dims
        if frame_w <= 0 or frame_h <= 0:
            return {'plausible': True, 'insufficient_data': True, 'reason': 'invalid_frame_dims'}

        try:
            grid_x, grid_y = _compute_grid_cell(bbox, frame_dims)
        except Exception as e:
            logger.warning("check_plausibility: could not compute grid cell: %s", e)
            return {'plausible': True, 'insufficient_data': True, 'reason': 'grid_error'}

        try:
            with get_cursor(commit=False) as cursor:
                cursor.execute(
                    """
                    SELECT sample_count,
                           mean_width,  std_width,
                           mean_height, std_height,
                           p5_width,    p95_width,
                           p5_height,   p95_height
                    FROM spatial_scale_models
                    WHERE camera_id = %s
                      AND classification = %s
                      AND grid_x = %s
                      AND grid_y = %s
                    """,
                    (camera_id, classification, grid_x, grid_y),
                )
                row = cursor.fetchone()
        except Exception as e:
            logger.error("check_plausibility: DB error: %s", e)
            return {'plausible': True, 'insufficient_data': True, 'reason': 'db_error'}

        if row is None or (row['sample_count'] or 0) < MIN_OBSERVATIONS:
            return {'plausible': True, 'insufficient_data': True}

        mean_w = row['mean_width']  or 0.0
        mean_h = row['mean_height'] or 0.0
        std_w  = row['std_width']   or 0.0
        std_h  = row['std_height']  or 0.0
        p5_w   = row['p5_width']    if row['p5_width']  is not None else mean_w
        p95_w  = row['p95_width']   if row['p95_width'] is not None else mean_w
        p5_h   = row['p5_height']   if row['p5_height'] is not None else mean_h
        p95_h  = row['p95_height']  if row['p95_height'] is not None else mean_h

        # Z-scores (guard against degenerate zero-std distributions)
        z_w = (width  - mean_w) / std_w if std_w > 0 else 0.0
        z_h = (height - mean_h) / std_h if std_h > 0 else 0.0

        # Plausible = within the 5th-95th percentile band on both axes
        plausible = (p5_w <= width <= p95_w) and (p5_h <= height <= p95_h)

        if not plausible:
            logger.debug(
                "check_plausibility: implausible %s/%s cell=(%d,%d) "
                "w=%.1f h=%.1f expected w=[%.1f,%.1f] h=[%.1f,%.1f]",
                camera_id, classification, grid_x, grid_y,
                width, height, p5_w, p95_w, p5_h, p95_h
            )

        return {
            'plausible': plausible,
            'z_score_width':  round(z_w, 4),
            'z_score_height': round(z_h, 4),
            'expected_range': {
                'w_min': round(p5_w,  2),
                'w_max': round(p95_w, 2),
                'h_min': round(p5_h,  2),
                'h_max': round(p95_h, 2),
            },
        }

    def get_model_stats(self, camera_id: str,
                        classification: Optional[str] = None) -> list:
        """Return all grid cell statistics for a camera.

        Args:
            camera_id: Unique camera identifier string
            classification: Optional filter; if None returns all classes

        Returns:
            List of dicts, one per (classification, grid_x, grid_y) row,
            with all model columns included. Empty list on error.
        """
        try:
            with get_cursor(commit=False) as cursor:
                if classification is not None:
                    cursor.execute(
                        """
                        SELECT camera_id, classification, grid_x, grid_y,
                               sample_count,
                               mean_width,  mean_height,
                               std_width,   std_height,
                               p5_width,    p95_width,
                               p5_height,   p95_height,
                               updated_at
                        FROM spatial_scale_models
                        WHERE camera_id = %s
                          AND classification = %s
                        ORDER BY classification, grid_y, grid_x
                        """,
                        (camera_id, classification),
                    )
                else:
                    cursor.execute(
                        """
                        SELECT camera_id, classification, grid_x, grid_y,
                               sample_count,
                               mean_width,  mean_height,
                               std_width,   std_height,
                               p5_width,    p95_width,
                               p5_height,   p95_height,
                               updated_at
                        FROM spatial_scale_models
                        WHERE camera_id = %s
                        ORDER BY classification, grid_y, grid_x
                        """,
                        (camera_id,),
                    )

                rows = cursor.fetchall()
                return [dict(row) for row in rows]

        except Exception as e:
            logger.error("get_model_stats: DB error for camera %s: %s", camera_id, e)
            return []
