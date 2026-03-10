"""
Cross-Camera Overlap Zone Mapping — pre-computes overlap regions for camera pairs.

Identifies pixel regions in each camera that see the same real-world area,
enabling focused cross-camera matching in overlap zones only.
"""

import logging
from typing import Dict, List, Optional

from db_connection import get_cursor

logger = logging.getLogger(__name__)


class OverlapMapper:
    """Pre-computes and queries overlap zones between camera pairs."""

    def compute_overlap_zones(self, group_id: int) -> Optional[Dict]:
        """Compute overlap zones for cameras in an overlap group.

        Uses confirmed cross-camera matches to identify overlapping frame regions.

        Args:
            group_id: camera_overlap_groups ID

        Returns:
            dict with per-camera overlap zone polygons
        """
        with get_cursor(commit=False) as cursor:
            # Get cameras in this group
            cursor.execute("""
                SELECT camera_ids, overlap_scores
                FROM camera_overlap_groups
                WHERE id = %s
            """, (group_id,))
            group = cursor.fetchone()

        if not group:
            return None

        camera_ids = group['camera_ids']
        if not camera_ids or len(camera_ids) < 2:
            return None

        overlap_zones = {}

        # For each camera pair, find confirmed matches and compute overlap regions
        for i, cam_a in enumerate(camera_ids):
            for cam_b in camera_ids[i + 1:]:
                zones = self._compute_pair_overlap(cam_a, cam_b)
                if zones:
                    if cam_a not in overlap_zones:
                        overlap_zones[cam_a] = []
                    if cam_b not in overlap_zones:
                        overlap_zones[cam_b] = []
                    overlap_zones[cam_a].append({
                        'paired_camera': cam_b,
                        'zone': zones['camera_a_zone'],
                    })
                    overlap_zones[cam_b].append({
                        'paired_camera': cam_a,
                        'zone': zones['camera_b_zone'],
                    })

        # Store computed zones
        if overlap_zones:
            import json
            with get_cursor() as cursor:
                cursor.execute("""
                    UPDATE camera_overlap_groups
                    SET overlap_zones = %s::jsonb
                    WHERE id = %s
                """, (json.dumps(overlap_zones), group_id))

        return overlap_zones

    def _compute_pair_overlap(self, cam_a: str, cam_b: str) -> Optional[Dict]:
        """Compute overlap zone between two cameras from confirmed matches.

        Returns bounding box regions in each camera's frame where cross-camera
        matches have been observed.
        """
        with get_cursor(commit=False) as cursor:
            # Get confirmed cross-camera links between these cameras
            cursor.execute("""
                SELECT p1.bbox_x AS ax, p1.bbox_y AS ay,
                       p1.bbox_width AS aw, p1.bbox_height AS ah,
                       p2.bbox_x AS bx, p2.bbox_y AS by,
                       p2.bbox_width AS bw, p2.bbox_height AS bh
                FROM cross_camera_links ccl
                JOIN ai_predictions p1 ON p1.id = ccl.prediction_id_a
                JOIN ai_predictions p2 ON p2.id = ccl.prediction_id_b
                JOIN videos v1 ON v1.id = p1.video_id
                JOIN videos v2 ON v2.id = p2.video_id
                WHERE v1.camera_id = %s AND v2.camera_id = %s
                  AND ccl.status IN ('confirmed', 'auto')
                ORDER BY ccl.created_at DESC
                LIMIT 100
            """, (cam_a, cam_b))
            matches = cursor.fetchall()

        if len(matches) < 3:
            return None

        # Compute bounding box of all match locations in each camera
        a_xs = [m['ax'] for m in matches]
        a_ys = [m['ay'] for m in matches]
        a_ws = [m['aw'] for m in matches]
        a_hs = [m['ah'] for m in matches]

        b_xs = [m['bx'] for m in matches]
        b_ys = [m['by'] for m in matches]
        b_ws = [m['bw'] for m in matches]
        b_hs = [m['bh'] for m in matches]

        # Expand to cover the full range of match positions
        cam_a_zone = {
            'x_min': min(a_xs),
            'y_min': min(a_ys),
            'x_max': max(ax + aw for ax, aw in zip(a_xs, a_ws)),
            'y_max': max(ay + ah for ay, ah in zip(a_ys, a_hs)),
        }

        cam_b_zone = {
            'x_min': min(b_xs),
            'y_min': min(b_ys),
            'x_max': max(bx + bw for bx, bw in zip(b_xs, b_ws)),
            'y_max': max(by + bh for by, bh in zip(b_ys, b_hs)),
        }

        return {
            'camera_a_zone': cam_a_zone,
            'camera_b_zone': cam_b_zone,
            'match_count': len(matches),
        }

    def is_in_overlap_zone(self, camera_id: str, bbox: Dict) -> List[str]:
        """Check if a bbox falls within an overlap zone for a camera.

        Args:
            camera_id: Camera identifier
            bbox: dict with x, y, width, height

        Returns:
            List of paired camera IDs where this bbox is in the overlap zone
        """
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT overlap_zones
                FROM camera_overlap_groups
                WHERE %s = ANY(camera_ids)
                  AND overlap_zones IS NOT NULL
            """, (camera_id,))
            rows = cursor.fetchall()

        paired_cameras = []
        cx = bbox.get('x', 0) + bbox.get('width', 0) / 2
        cy = bbox.get('y', 0) + bbox.get('height', 0) / 2

        for row in rows:
            zones = row['overlap_zones']
            if not isinstance(zones, dict):
                continue
            camera_zones = zones.get(camera_id, [])
            for zone_info in camera_zones:
                zone = zone_info.get('zone', {})
                if (zone.get('x_min', 0) <= cx <= zone.get('x_max', 99999) and
                        zone.get('y_min', 0) <= cy <= zone.get('y_max', 99999)):
                    paired_cameras.append(zone_info['paired_camera'])

        return paired_cameras

    def get_overlap_groups(self) -> List[Dict]:
        """Get all overlap groups with their zones."""
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT id, camera_ids, overlap_scores, overlap_zones, is_auto_computed
                FROM camera_overlap_groups
                ORDER BY id
            """)
            return [dict(r) for r in cursor.fetchall()]
