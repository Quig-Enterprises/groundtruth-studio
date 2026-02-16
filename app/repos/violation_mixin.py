import json
import uuid
from datetime import datetime
from typing import List, Dict, Optional

from psycopg2 import extras

from db_connection import get_cursor


class ViolationMixin:
    """Violations and visits."""

    # ==================== Violations ====================

    def create_violation(self, violation_type: str, camera_id: str,
                         confidence: float, person_identity_id: str = None,
                         vehicle_identity_id: str = None,
                         boat_identity_id: str = None,
                         trailer_identity_id: str = None,
                         evidence_paths: list = None) -> Dict:
        """Create a new violation record."""
        with get_cursor() as cursor:
            cursor.execute('''
                INSERT INTO violations
                (violation_type, camera_id, confidence, person_identity_id,
                 vehicle_identity_id, boat_identity_id, trailer_identity_id,
                 evidence_paths)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
            ''', (violation_type, camera_id, confidence,
                  person_identity_id, vehicle_identity_id,
                  boat_identity_id, trailer_identity_id,
                  evidence_paths or []))
            row = cursor.fetchone()
            return dict(row)

    def get_violations(self, status: str = None, camera_id: str = None,
                       violation_type: str = None, limit: int = 100,
                       offset: int = 0) -> List[Dict]:
        """Get violations with optional filters. Includes identity names via LEFT JOINs."""
        conditions = []
        params = []
        if status is not None:
            conditions.append('v.status = %s')
            params.append(status)
        if camera_id is not None:
            conditions.append('v.camera_id = %s')
            params.append(camera_id)
        if violation_type is not None:
            conditions.append('v.violation_type = %s')
            params.append(violation_type)

        where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
        params.extend([limit, offset])

        with get_cursor(commit=False) as cursor:
            cursor.execute(f'''
                SELECT v.*,
                       ip.name AS person_name,
                       iv.name AS vehicle_name,
                       ib.name AS boat_name,
                       it.name AS trailer_name
                FROM violations v
                LEFT JOIN identities ip ON v.person_identity_id = ip.identity_id
                LEFT JOIN identities iv ON v.vehicle_identity_id = iv.identity_id
                LEFT JOIN identities ib ON v.boat_identity_id = ib.identity_id
                LEFT JOIN identities it ON v.trailer_identity_id = it.identity_id
                {where}
                ORDER BY v.timestamp DESC
                LIMIT %s OFFSET %s
            ''', params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def review_violation(self, violation_id: str, status: str,
                         reviewed_by: str, notes: str = None) -> Optional[Dict]:
        """Review a violation - update its status, reviewer, and notes."""
        with get_cursor() as cursor:
            cursor.execute('''
                UPDATE violations
                SET status = %s, reviewed_by = %s, notes = %s
                WHERE violation_id = %s
                RETURNING *
            ''', (status, reviewed_by, notes, violation_id))
            row = cursor.fetchone()
            return dict(row) if row else None

    # ==================== Visits ====================

    def create_visit(self, person_identity_id: str = None,
                     vehicle_identity_id: str = None,
                     boat_identity_id: str = None,
                     track_ids: list = None,
                     camera_timeline: list = None) -> Dict:
        """Create a new visit record."""
        with get_cursor() as cursor:
            cursor.execute('''
                INSERT INTO visits
                (person_identity_id, vehicle_identity_id, boat_identity_id,
                 track_ids, camera_timeline)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
            ''', (person_identity_id, vehicle_identity_id, boat_identity_id,
                  track_ids or [],
                  extras.Json(camera_timeline) if camera_timeline else extras.Json([])))
            row = cursor.fetchone()
            return dict(row)

    def get_visit(self, visit_id: str) -> Optional[Dict]:
        """Get a visit by ID with identity names via LEFT JOINs."""
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT v.*,
                       ip.name AS person_name,
                       iv.name AS vehicle_name,
                       ib.name AS boat_name
                FROM visits v
                LEFT JOIN identities ip ON v.person_identity_id = ip.identity_id
                LEFT JOIN identities iv ON v.vehicle_identity_id = iv.identity_id
                LEFT JOIN identities ib ON v.boat_identity_id = ib.identity_id
                WHERE v.visit_id = %s
            ''', (visit_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_visits(self, person_identity_id: str = None, date_start=None,
                   date_end=None, limit: int = 100, offset: int = 0) -> List[Dict]:
        """Get visits with optional filters, ordered by arrival_time DESC."""
        conditions = []
        params = []
        if person_identity_id is not None:
            conditions.append('v.person_identity_id = %s')
            params.append(person_identity_id)
        if date_start is not None:
            conditions.append('v.arrival_time >= %s')
            params.append(date_start)
        if date_end is not None:
            conditions.append('v.arrival_time <= %s')
            params.append(date_end)

        where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
        params.extend([limit, offset])

        with get_cursor(commit=False) as cursor:
            cursor.execute(f'''
                SELECT v.*,
                       ip.name AS person_name,
                       iv.name AS vehicle_name,
                       ib.name AS boat_name
                FROM visits v
                LEFT JOIN identities ip ON v.person_identity_id = ip.identity_id
                LEFT JOIN identities iv ON v.vehicle_identity_id = iv.identity_id
                LEFT JOIN identities ib ON v.boat_identity_id = ib.identity_id
                {where}
                ORDER BY v.arrival_time DESC
                LIMIT %s OFFSET %s
            ''', params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def end_visit(self, visit_id: str, departure_time=None) -> bool:
        """End a visit by setting departure_time (defaults to NOW())."""
        with get_cursor() as cursor:
            if departure_time is not None:
                cursor.execute('''
                    UPDATE visits SET departure_time = %s
                    WHERE visit_id = %s AND departure_time IS NULL
                ''', (departure_time, visit_id))
            else:
                cursor.execute('''
                    UPDATE visits SET departure_time = NOW()
                    WHERE visit_id = %s AND departure_time IS NULL
                ''', (visit_id,))
            return cursor.rowcount > 0

    def add_violation_to_visit(self, visit_id: str, violation_id: str) -> bool:
        """Append a violation_id to a visit's violation_ids array."""
        with get_cursor() as cursor:
            cursor.execute('''
                UPDATE visits
                SET violation_ids = array_append(violation_ids, %s::uuid)
                WHERE visit_id = %s
            ''', (violation_id, visit_id))
            return cursor.rowcount > 0
