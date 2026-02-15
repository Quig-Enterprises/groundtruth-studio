"""
Violation Detector Module
=========================
Detects marina violations by analyzing tracked entity relationships
and behavioral patterns from the detection pipeline.
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from db_connection import get_cursor
from psycopg2 import extras

logger = logging.getLogger('violation_detector')


class ViolationDetector:
    """Detects and records violations from tracked entity data."""

    def __init__(self, config=None):
        self.config = config or {}
        self.violation_rules = self._init_rules()

    def _init_rules(self) -> list:
        """Initialize violation detection rules."""
        return [
            {
                'type': 'power_loading',
                'description': 'Boat motor running while on trailer at ramp',
                'required_entities': ['boat', 'trailer'],
                'camera_zones': self.config.get('ramp_cameras', []),
                'min_confidence': 0.6,
                'min_duration_seconds': 5,
            },
            {
                'type': 'unauthorized_dock',
                'description': 'Vessel docked at unauthorized location',
                'required_entities': ['boat'],
                'camera_zones': self.config.get('restricted_dock_cameras', []),
                'min_confidence': 0.7,
                'min_duration_seconds': 30,
            },
        ]

    def check_power_loading(self, camera_id: str, tracked_objects: list,
                             associations: list = None) -> dict:
        """Check for power loading violation.

        Power loading = boat + trailer co-detected at ramp, with boat propeller
        area showing spray/disturbance pattern.

        For Phase 1: simplified detection based on boat+trailer co-occurrence
        at designated ramp cameras for extended duration.

        Args:
            camera_id: Camera where objects were detected
            tracked_objects: List of tracked entities with bbox, entity_type, track_id
            associations: Known associations between entities

        Returns:
            dict with violation details if detected, None otherwise
        """
        boats = [o for o in tracked_objects if o.get('entity_type') == 'boat']
        trailers = [o for o in tracked_objects if o.get('entity_type') == 'trailer']

        if not boats or not trailers:
            return None

        # Check for boat-trailer spatial overlap (boat on trailer at ramp)
        for boat in boats:
            for trailer in trailers:
                overlap = self._compute_overlap(boat.get('bbox', []), trailer.get('bbox', []))
                if overlap > 0.3:  # Significant overlap = boat on trailer
                    return {
                        'violation_type': 'power_loading',
                        'camera_id': camera_id,
                        'confidence': min(boat.get('confidence', 0), trailer.get('confidence', 0)),
                        'boat_track_id': boat.get('track_id'),
                        'trailer_track_id': trailer.get('track_id'),
                        'boat_identity_id': boat.get('identity_id'),
                        'trailer_identity_id': trailer.get('identity_id'),
                        'details': {
                            'overlap': round(overlap, 4),
                            'boat_bbox': boat.get('bbox'),
                            'trailer_bbox': trailer.get('bbox'),
                        }
                    }
        return None

    def _compute_overlap(self, bbox_a: list, bbox_b: list) -> float:
        """Compute IoU between two [x, y, w, h] normalized bounding boxes."""
        if not bbox_a or not bbox_b or len(bbox_a) < 4 or len(bbox_b) < 4:
            return 0.0

        ax1, ay1 = bbox_a[0], bbox_a[1]
        ax2, ay2 = ax1 + bbox_a[2], ay1 + bbox_a[3]
        bx1, by1 = bbox_b[0], bbox_b[1]
        bx2, by2 = bx1 + bbox_b[2], by1 + bbox_b[3]

        ix1, iy1 = max(ax1, bx1), max(ay1, by1)
        ix2, iy2 = min(ax2, bx2), min(ay2, by2)

        if ix2 <= ix1 or iy2 <= iy1:
            return 0.0

        intersection = (ix2 - ix1) * (iy2 - iy1)
        area_a = bbox_a[2] * bbox_a[3]
        area_b = bbox_b[2] * bbox_b[3]
        union = area_a + area_b - intersection

        return intersection / union if union > 0 else 0.0

    def record_violation(self, violation: dict) -> dict:
        """Record a detected violation in the database.

        Also attempts to link to person and vehicle via association chain.
        """
        with get_cursor() as cursor:
            # Look up associated person and vehicle via association chain
            person_id = None
            vehicle_id = None
            boat_id = violation.get('boat_identity_id')
            trailer_id = violation.get('trailer_identity_id')

            # Try to find associated person via boat or trailer associations
            if boat_id or trailer_id:
                search_id = boat_id or trailer_id
                cursor.execute("""
                    SELECT a.identity_a, a.identity_b, a.association_type,
                           ia.identity_type as type_a, ib.identity_type as type_b
                    FROM associations a
                    JOIN identities ia ON a.identity_a = ia.identity_id
                    JOIN identities ib ON a.identity_b = ib.identity_id
                    WHERE (a.identity_a = %s OR a.identity_b = %s)
                    AND a.confidence >= 0.3
                    ORDER BY a.confidence DESC
                """, (search_id, search_id))

                for row in cursor.fetchall():
                    other_id = row[1] if str(row[0]) == str(search_id) else row[0]
                    other_type = row[4] if str(row[0]) == str(search_id) else row[3]

                    if other_type == 'person' and not person_id:
                        person_id = other_id
                    elif other_type == 'vehicle' and not vehicle_id:
                        vehicle_id = other_id

            # Insert violation
            evidence_paths = violation.get('evidence_paths', [])
            cursor.execute("""
                INSERT INTO violations
                (violation_type, camera_id, confidence,
                 person_identity_id, vehicle_identity_id,
                 boat_identity_id, trailer_identity_id,
                 evidence_paths)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
            """, (
                violation['violation_type'],
                violation['camera_id'],
                violation.get('confidence', 0.0),
                person_id, vehicle_id, boat_id, trailer_id,
                evidence_paths
            ))

            result = dict(cursor.fetchone())
            logger.info(
                f"Violation recorded: {violation['violation_type']} at {violation['camera_id']} "
                f"(id={result['violation_id']}, confidence={violation.get('confidence', 0):.2f})"
            )
            return result

    def get_pending_violations(self, limit: int = 50) -> list:
        """Get violations pending review."""
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT v.*,
                       p.name as person_name,
                       veh.name as vehicle_name,
                       b.name as boat_name
                FROM violations v
                LEFT JOIN identities p ON v.person_identity_id = p.identity_id
                LEFT JOIN identities veh ON v.vehicle_identity_id = veh.identity_id
                LEFT JOIN identities b ON v.boat_identity_id = b.identity_id
                WHERE v.status = 'detected'
                ORDER BY v.timestamp DESC
                LIMIT %s
            """, (limit,))
            return [dict(row) for row in cursor.fetchall()]

    def review_violation(self, violation_id: str, status: str,
                          reviewed_by: str, notes: str = None) -> dict:
        """Review a violation (confirm, false_positive, or action)."""
        valid_statuses = ('confirmed', 'false_positive', 'actioned')
        if status not in valid_statuses:
            raise ValueError(f"Status must be one of {valid_statuses}")

        with get_cursor() as cursor:
            cursor.execute("""
                UPDATE violations
                SET status = %s, reviewed_by = %s, notes = %s
                WHERE violation_id = %s
                RETURNING *
            """, (status, reviewed_by, notes, violation_id))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_violation_stats(self, days: int = 30) -> dict:
        """Get violation statistics for the last N days."""
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT
                    violation_type,
                    status,
                    COUNT(*) as count,
                    AVG(confidence) as avg_confidence
                FROM violations
                WHERE timestamp >= NOW() - INTERVAL '%s days'
                GROUP BY violation_type, status
                ORDER BY violation_type, status
            """, (days,))

            stats = {}
            for row in cursor.fetchall():
                row = dict(row)
                vtype = row['violation_type']
                if vtype not in stats:
                    stats[vtype] = {'total': 0, 'by_status': {}}
                stats[vtype]['total'] += row['count']
                stats[vtype]['by_status'][row['status']] = {
                    'count': row['count'],
                    'avg_confidence': float(row['avg_confidence']) if row['avg_confidence'] else 0
                }

            return stats


if __name__ == '__main__':
    detector = ViolationDetector()
    pending = detector.get_pending_violations()
    print(f"Pending violations: {len(pending)}")
    stats = detector.get_violation_stats()
    print(f"Violation stats (30d): {json.dumps(stats, indent=2)}")
