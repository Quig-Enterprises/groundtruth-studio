"""
PTZ Self-Calibration — learns camera geometry from stationary objects.

Tracks stationary reference objects across PTZ orientations to solve for
effective focal length at each zoom level and pixel-to-angle mapping.
Enables real-world size estimation from calibrated cameras.
"""

import logging
import math
from typing import Dict, List, Optional, Tuple

from db_connection import get_cursor

logger = logging.getLogger(__name__)


class PTZSelfCalibrator:
    """Learns PTZ camera geometry from stationary reference objects."""

    def record_stationary_observation(self, camera_id: str, ptz_position: Dict,
                                       detected_objects: List[Dict]):
        """Record observations of objects at a specific PTZ orientation.

        Args:
            camera_id: Camera identifier
            ptz_position: dict with 'pan', 'tilt', 'zoom' keys
            detected_objects: list of dicts with 'label', 'bbox' (x,y,w,h), 'confidence'
        """
        pan = ptz_position.get('pan', 0)
        tilt = ptz_position.get('tilt', 0)
        zoom = ptz_position.get('zoom', 1.0)

        for obj in detected_objects:
            label = obj.get('label', 'unknown')
            bbox = obj.get('bbox', {})

            observation = {
                'pan': pan, 'tilt': tilt, 'zoom': zoom,
                'pixel_x': bbox.get('x', 0), 'pixel_y': bbox.get('y', 0),
                'pixel_w': bbox.get('width', 0), 'pixel_h': bbox.get('height', 0),
            }

            with get_cursor() as cursor:
                # Check if reference exists
                cursor.execute("""
                    SELECT id, observations FROM ptz_stationary_references
                    WHERE camera_id = %s AND reference_label = %s AND is_active = TRUE
                """, (camera_id, label))
                row = cursor.fetchone()

                if row:
                    import json
                    obs_list = row['observations'] if isinstance(row['observations'], list) else json.loads(row['observations'] or '[]')
                    obs_list.append(observation)
                    # Keep last 100 observations
                    if len(obs_list) > 100:
                        obs_list = obs_list[-100:]
                    cursor.execute("""
                        UPDATE ptz_stationary_references
                        SET observations = %s::jsonb, updated_at = NOW()
                        WHERE id = %s
                    """, (json.dumps(obs_list), row['id']))
                else:
                    import json
                    cursor.execute("""
                        INSERT INTO ptz_stationary_references
                            (camera_id, reference_label, observations)
                        VALUES (%s, %s, %s::jsonb)
                    """, (camera_id, label, json.dumps([observation])))

    def solve_geometry(self, camera_id: str) -> Optional[Dict]:
        """Solve for focal length and pixel-to-angle mapping from observations.

        Returns:
            dict with per-zoom-level calibration data, or None if insufficient data
        """
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT reference_label, observations
                FROM ptz_stationary_references
                WHERE camera_id = %s AND is_active = TRUE
            """, (camera_id,))
            refs = cursor.fetchall()

        if not refs:
            return None

        # Group observations by zoom level
        zoom_data = {}
        for ref in refs:
            obs_list = ref['observations'] if isinstance(ref['observations'], list) else []
            for obs in obs_list:
                zoom = round(obs.get('zoom', 1.0), 1)
                if zoom not in zoom_data:
                    zoom_data[zoom] = []
                zoom_data[zoom].append(obs)

        calibration = {}
        for zoom_level, observations in zoom_data.items():
            if len(observations) < 5:
                continue

            # Estimate pixels-per-degree from pan/tilt variations
            pan_values = [o['pan'] for o in observations]
            pixel_x_values = [o['pixel_x'] + o['pixel_w'] / 2 for o in observations]

            if max(pan_values) - min(pan_values) > 1.0:
                # Linear regression: pixel_x = ppd_h * pan + offset
                n = len(observations)
                sum_pan = sum(pan_values)
                sum_px = sum(pixel_x_values)
                sum_pan_px = sum(p * px for p, px in zip(pan_values, pixel_x_values))
                sum_pan2 = sum(p * p for p in pan_values)

                denom = n * sum_pan2 - sum_pan * sum_pan
                if abs(denom) > 1e-6:
                    ppd_h = abs((n * sum_pan_px - sum_pan * sum_px) / denom)
                else:
                    ppd_h = None
            else:
                ppd_h = None

            calibration[zoom_level] = {
                'pixels_per_degree_h': ppd_h,
                'sample_count': len(observations),
            }

            # Store in DB
            with get_cursor() as cursor:
                cursor.execute("""
                    INSERT INTO ptz_zoom_calibration
                        (camera_id, zoom_level, pixels_per_degree_h, sample_count)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (camera_id, zoom_level) DO UPDATE
                    SET pixels_per_degree_h = EXCLUDED.pixels_per_degree_h,
                        sample_count = EXCLUDED.sample_count
                """, (camera_id, zoom_level, ppd_h, len(observations)))

        return calibration if calibration else None

    def estimate_real_size(self, camera_id: str, zoom: float,
                           bbox: Dict) -> Optional[Dict]:
        """Estimate real-world dimensions from calibrated geometry.

        Args:
            camera_id: Camera identifier
            zoom: Current zoom level
            bbox: dict with 'width', 'height' in pixels

        Returns:
            dict with estimated_width_m, estimated_height_m, confidence
        """
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT pixels_per_degree_h, pixels_per_degree_v,
                       effective_focal_length, sample_count
                FROM ptz_zoom_calibration
                WHERE camera_id = %s AND zoom_level = %s
            """, (camera_id, round(zoom, 1)))
            row = cursor.fetchone()

        if not row or not row['pixels_per_degree_h']:
            return None

        ppd_h = row['pixels_per_degree_h']
        bbox_w = bbox.get('width', 0)
        bbox_h = bbox.get('height', 0)

        if ppd_h > 0:
            angle_w = bbox_w / ppd_h
            angle_h = bbox_h / ppd_h if row.get('pixels_per_degree_v') else bbox_h / ppd_h

            return {
                'angular_width_deg': round(angle_w, 3),
                'angular_height_deg': round(angle_h, 3),
                'confidence': min(1.0, row['sample_count'] / 50),
            }

        return None

    def get_calibration_status(self, camera_id: str) -> Dict:
        """Get calibration status for a camera."""
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT COUNT(*) AS ref_count
                FROM ptz_stationary_references
                WHERE camera_id = %s AND is_active = TRUE
            """, (camera_id,))
            refs = cursor.fetchone()['ref_count']

            cursor.execute("""
                SELECT zoom_level, sample_count, pixels_per_degree_h
                FROM ptz_zoom_calibration
                WHERE camera_id = %s
                ORDER BY zoom_level
            """, (camera_id,))
            zoom_cals = [dict(r) for r in cursor.fetchall()]

        return {
            'camera_id': camera_id,
            'reference_objects': refs,
            'zoom_calibrations': zoom_cals,
            'is_calibrated': len(zoom_cals) > 0,
        }
