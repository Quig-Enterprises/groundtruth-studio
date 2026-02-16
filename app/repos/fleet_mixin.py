import json
from datetime import datetime
from typing import List, Dict, Optional

import psycopg2
from psycopg2 import extras

from db_connection import get_cursor


class FleetMixin:
    """Fleet vehicles and trailers."""

    # Fleet Vehicle Management
    def add_or_update_fleet_vehicle(self, fleet_id: str, fleet_type: str = None,
                                     vehicle_type: str = None, vehicle_make: str = None,
                                     vehicle_model: str = None, primary_color: str = None,
                                     secondary_color: str = None, agency_name: str = None,
                                     plate_number: str = None, plate_state: str = None,
                                     notes: str = None) -> int:
        """Add or update a fleet vehicle"""
        with get_cursor() as cursor:
            # Try to update first
            cursor.execute('''
                UPDATE fleet_vehicles
                SET fleet_type = COALESCE(%s, fleet_type),
                    vehicle_type = COALESCE(%s, vehicle_type),
                    vehicle_make = COALESCE(%s, vehicle_make),
                    vehicle_model = COALESCE(%s, vehicle_model),
                    primary_color = COALESCE(%s, primary_color),
                    secondary_color = COALESCE(%s, secondary_color),
                    agency_name = COALESCE(%s, agency_name),
                    plate_number = COALESCE(%s, plate_number),
                    plate_state = COALESCE(%s, plate_state),
                    notes = COALESCE(%s, notes),
                    last_seen_date = CURRENT_TIMESTAMP,
                    total_detections = total_detections + 1
                WHERE fleet_id = %s
                RETURNING id
            ''', (fleet_type, vehicle_type, vehicle_make, vehicle_model,
                  primary_color, secondary_color, agency_name, plate_number,
                  plate_state, notes, fleet_id))

            result = cursor.fetchone()
            if result:
                return result['id']

            # Insert new record
            cursor.execute('''
                INSERT INTO fleet_vehicles
                (fleet_id, fleet_type, vehicle_type, vehicle_make, vehicle_model,
                 primary_color, secondary_color, agency_name, plate_number, plate_state, notes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            ''', (fleet_id, fleet_type, vehicle_type, vehicle_make, vehicle_model,
                  primary_color, secondary_color, agency_name, plate_number, plate_state, notes))
            result = cursor.fetchone()
            return result['id']

    def get_fleet_vehicle(self, fleet_id: str) -> Optional[Dict]:
        """Get a fleet vehicle by fleet ID"""
        with get_cursor(commit=False) as cursor:
            cursor.execute('SELECT * FROM fleet_vehicles WHERE fleet_id = %s', (fleet_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_all_fleet_vehicles(self, fleet_type: str = None, limit: int = 100) -> List[Dict]:
        """Get all fleet vehicles, optionally filtered by type"""
        with get_cursor(commit=False) as cursor:
            if fleet_type:
                cursor.execute('''
                    SELECT * FROM fleet_vehicles
                    WHERE fleet_type = %s
                    ORDER BY last_seen_date DESC
                    LIMIT %s
                ''', (fleet_type, limit))
            else:
                cursor.execute('''
                    SELECT * FROM fleet_vehicles
                    ORDER BY last_seen_date DESC
                    LIMIT %s
                ''', (limit,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def search_fleet_vehicles(self, query: str) -> List[Dict]:
        """Search fleet vehicles by various fields"""
        with get_cursor(commit=False) as cursor:
            search_term = f'%{query}%'
            cursor.execute('''
                SELECT * FROM fleet_vehicles
                WHERE fleet_id LIKE %s OR plate_number LIKE %s
                   OR vehicle_make LIKE %s OR vehicle_model LIKE %s
                   OR agency_name LIKE %s OR notes LIKE %s
                ORDER BY last_seen_date DESC
            ''', (search_term, search_term, search_term, search_term, search_term, search_term))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def link_person_to_vehicle(self, fleet_id: str, person_name: str) -> int:
        """Link a person to a fleet vehicle"""
        with get_cursor() as cursor:
            # Try to update existing link
            cursor.execute('''
                UPDATE vehicle_person_links
                SET last_seen_together = CURRENT_TIMESTAMP,
                    times_seen_together = times_seen_together + 1
                WHERE vehicle_fleet_id = %s AND person_name = %s
                RETURNING id
            ''', (fleet_id, person_name))

            result = cursor.fetchone()
            if result:
                return result['id']

            # Create new link
            cursor.execute('''
                INSERT INTO vehicle_person_links (vehicle_fleet_id, person_name)
                VALUES (%s, %s)
                RETURNING id
            ''', (fleet_id, person_name))
            result = cursor.fetchone()
            return result['id']

    def get_vehicle_persons(self, fleet_id: str) -> List[Dict]:
        """Get all persons linked to a vehicle"""
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT * FROM vehicle_person_links
                WHERE vehicle_fleet_id = %s
                ORDER BY times_seen_together DESC
            ''', (fleet_id,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_person_vehicles(self, person_name: str) -> List[Dict]:
        """Get all vehicles linked to a person"""
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT fv.*, vpl.first_seen_together, vpl.last_seen_together, vpl.times_seen_together
                FROM fleet_vehicles fv
                JOIN vehicle_person_links vpl ON fv.fleet_id = vpl.vehicle_fleet_id
                WHERE vpl.person_name = %s
                ORDER BY vpl.times_seen_together DESC
            ''', (person_name,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    # Trailer Management
    def add_or_update_trailer(self, trailer_id: str, trailer_type: str = None,
                              trailer_color: str = None, plate_number: str = None,
                              plate_state: str = None, notes: str = None) -> int:
        """Add or update a trailer"""
        with get_cursor() as cursor:
            # Try to update first
            cursor.execute('''
                UPDATE trailers
                SET trailer_type = COALESCE(%s, trailer_type),
                    trailer_color = COALESCE(%s, trailer_color),
                    plate_number = COALESCE(%s, plate_number),
                    plate_state = COALESCE(%s, plate_state),
                    notes = COALESCE(%s, notes),
                    last_seen_date = CURRENT_TIMESTAMP,
                    total_detections = total_detections + 1
                WHERE trailer_id = %s
                RETURNING id
            ''', (trailer_type, trailer_color, plate_number, plate_state, notes, trailer_id))

            result = cursor.fetchone()
            if result:
                return result['id']

            # Insert new record
            cursor.execute('''
                INSERT INTO trailers
                (trailer_id, trailer_type, trailer_color, plate_number, plate_state, notes)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
            ''', (trailer_id, trailer_type, trailer_color, plate_number, plate_state, notes))
            result = cursor.fetchone()
            return result['id']

    def get_trailer(self, trailer_id: str) -> Optional[Dict]:
        """Get a trailer by trailer ID"""
        with get_cursor(commit=False) as cursor:
            cursor.execute('SELECT * FROM trailers WHERE trailer_id = %s', (trailer_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_all_trailers(self, limit: int = 100) -> List[Dict]:
        """Get all trailers"""
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT * FROM trailers
                ORDER BY last_seen_date DESC
                LIMIT %s
            ''', (limit,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def link_trailer_to_vehicle(self, fleet_id: str, trailer_id: str) -> int:
        """Link a trailer to a fleet vehicle"""
        with get_cursor() as cursor:
            # Try to update existing link
            cursor.execute('''
                UPDATE vehicle_trailer_links
                SET last_seen_together = CURRENT_TIMESTAMP,
                    times_seen_together = times_seen_together + 1
                WHERE vehicle_fleet_id = %s AND trailer_id = %s
                RETURNING id
            ''', (fleet_id, trailer_id))

            result = cursor.fetchone()
            if result:
                return result['id']

            # Create new link
            cursor.execute('''
                INSERT INTO vehicle_trailer_links (vehicle_fleet_id, trailer_id)
                VALUES (%s, %s)
                RETURNING id
            ''', (fleet_id, trailer_id))
            result = cursor.fetchone()
            return result['id']

    def get_vehicle_trailers(self, fleet_id: str) -> List[Dict]:
        """Get all trailers linked to a vehicle"""
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT t.*, vtl.first_seen_together, vtl.last_seen_together, vtl.times_seen_together
                FROM trailers t
                JOIN vehicle_trailer_links vtl ON t.trailer_id = vtl.trailer_id
                WHERE vtl.vehicle_fleet_id = %s
                ORDER BY vtl.times_seen_together DESC
            ''', (fleet_id,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_trailer_vehicles(self, trailer_id: str) -> List[Dict]:
        """Get all vehicles linked to a trailer"""
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT fv.*, vtl.first_seen_together, vtl.last_seen_together, vtl.times_seen_together
                FROM fleet_vehicles fv
                JOIN vehicle_trailer_links vtl ON fv.fleet_id = vtl.vehicle_fleet_id
                WHERE vtl.trailer_id = %s
                ORDER BY vtl.times_seen_together DESC
            ''', (trailer_id,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
