"""Visit Builder - Visit aggregation module for Multi-Entity Detection System.

Groups related tracks into visit records — the primary enforcement and reporting unit.
A visit captures a person's entire session at the facility: arrival, activities across
cameras, violations, and departure.
"""

from db_connection import get_cursor, get_connection
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

logging.basicConfig(level=logging.INFO)


class VisitBuilder:
    """Aggregates tracks into visit records for enforcement and reporting."""

    def __init__(self, config=None):
        """Initialize the visit builder.

        Args:
            config: Optional dict with visit_timeout_minutes (default: 30)
        """
        self.config = config or {}
        self.visit_timeout = self.config.get('visit_timeout_minutes', 30)
        self.logger = logging.getLogger('visit_builder')

    def build_visits(self) -> dict:
        """Main aggregation job. Runs periodically to:
        1. Find tracks not yet assigned to any visit
        2. Group tracks by identity + time proximity
        3. Create or update visit records
        4. End visits that have timed out

        Returns:
            dict: Summary stats with counts of new/updated visits
        """
        self.logger.info("Starting visit aggregation")

        tracks = self._get_unvisited_tracks()
        self.logger.info(f"Found {len(tracks)} unvisited tracks")

        new_visits = 0
        updated_visits = 0

        for track in tracks:
            visit_id = self._find_or_create_visit(track)
            if visit_id:
                # Check if this is a new visit (created just now)
                # by checking if track_id is already in the visit
                with get_cursor() as cur:
                    cur.execute(
                        "SELECT track_ids FROM visits WHERE visit_id = %s",
                        (visit_id,)
                    )
                    row = cur.fetchone()
                    if row and track['track_id'] not in row[0]:
                        self._add_track_to_visit(visit_id, track)
                        updated_visits += 1
                    elif not row:
                        new_visits += 1

        stale_ended = self.end_stale_visits()

        summary = {
            'unvisited_tracks': len(tracks),
            'new_visits': new_visits,
            'updated_visits': updated_visits,
            'stale_visits_ended': stale_ended
        }

        self.logger.info(f"Visit aggregation complete: {summary}")
        return summary

    def _get_unvisited_tracks(self) -> list:
        """Get tracks with identity_id that aren't in any visit's track_ids array.

        Returns:
            list: Track records as dicts
        """
        query = """
            SELECT t.track_id, t.identity_id, t.camera_id, t.start_time,
                   t.end_time, t.metadata
            FROM tracks t
            WHERE t.identity_id IS NOT NULL
            AND NOT EXISTS (
                SELECT 1 FROM visits v WHERE t.track_id = ANY(v.track_ids)
            )
            ORDER BY t.start_time ASC
        """

        with get_cursor() as cur:
            cur.execute(query)
            return cur.fetchall()

    def _find_or_create_visit(self, track: dict) -> str:
        """Find an existing active visit for this identity, or create a new one.

        Match criteria: same person (or associated person), within timeout window.

        Args:
            track: Track dict with identity_id, start_time, etc.

        Returns:
            str: visit_id (UUID)
        """
        identity_id = track['identity_id']
        track_start = track['start_time']

        # Calculate timeout threshold
        timeout_threshold = track_start - timedelta(minutes=self.visit_timeout)

        # Try to find an active visit for this identity within the timeout window
        query = """
            SELECT visit_id, departure_time
            FROM visits
            WHERE person_identity_id = %s
            AND (departure_time IS NULL OR departure_time > %s)
            ORDER BY arrival_time DESC
            LIMIT 1
        """

        with get_cursor() as cur:
            cur.execute(query, (identity_id, timeout_threshold))
            row = cur.fetchone()

            if row:
                visit_id = row[0]
                self.logger.info(f"Found existing visit {visit_id} for identity {identity_id}")
                return visit_id
            else:
                # Create new visit
                visit_id = self._create_visit(track)
                self.logger.info(f"Created new visit {visit_id} for identity {identity_id}")
                return visit_id

    def _add_track_to_visit(self, visit_id: str, track: dict):
        """Add a track to an existing visit.

        - Append track_id to track_ids array
        - Update camera_timeline JSONB
        - Update departure_time if track is more recent

        Args:
            visit_id: UUID of the visit
            track: Track dict
        """
        query = """
            UPDATE visits
            SET track_ids = array_append(track_ids, %s),
                departure_time = GREATEST(departure_time, %s)
            WHERE visit_id = %s
        """

        with get_cursor() as cur:
            cur.execute(
                query,
                (track['track_id'], track['end_time'] or track['start_time'], visit_id)
            )

        # Update camera timeline
        self._update_camera_timeline(
            visit_id,
            track['camera_id'],
            track['start_time'].isoformat() if isinstance(track['start_time'], datetime) else track['start_time'],
            track['end_time'].isoformat() if track['end_time'] and isinstance(track['end_time'], datetime) else track['end_time']
        )

        self.logger.debug(f"Added track {track['track_id']} to visit {visit_id}")

    def _create_visit(self, track: dict) -> str:
        """Create a new visit from an initial track.

        - Set person_identity_id from track's identity
        - Look up associated vehicle/boat via associations table
        - Set arrival_time from track start_time
        - Initialize camera_timeline

        Args:
            track: Initial track dict

        Returns:
            str: visit_id (UUID)
        """
        visit_id = str(uuid.uuid4())
        identity_id = track['identity_id']

        # Look up associated vehicle/boat
        vehicle_id = None
        boat_id = None

        with get_cursor() as cur:
            # Find vehicle association
            cur.execute("""
                SELECT CASE WHEN identity_a = %s THEN identity_b ELSE identity_a END as other_id
                FROM associations
                WHERE (identity_a = %s OR identity_b = %s)
                AND association_type = 'person_vehicle'
                ORDER BY last_observed DESC
                LIMIT 1
            """, (identity_id, identity_id, identity_id))
            row = cur.fetchone()
            if row:
                vehicle_id = row[0]

            # Find boat association
            cur.execute("""
                SELECT CASE WHEN identity_a = %s THEN identity_b ELSE identity_a END as other_id
                FROM associations
                WHERE (identity_a = %s OR identity_b = %s)
                AND association_type = 'person_boat'
                ORDER BY last_observed DESC
                LIMIT 1
            """, (identity_id, identity_id, identity_id))
            row = cur.fetchone()
            if row:
                boat_id = row[0]

        # Create the visit
        insert_query = """
            INSERT INTO visits (
                visit_id, person_identity_id, vehicle_identity_id,
                boat_identity_id, arrival_time, departure_time,
                violation_ids, track_ids, camera_timeline, created_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
        """

        camera_timeline = [{
            "camera": track['camera_id'],
            "enter_time": track['start_time'].isoformat() if isinstance(track['start_time'], datetime) else track['start_time'],
            "exit_time": track['end_time'].isoformat() if track['end_time'] and isinstance(track['end_time'], datetime) else track['end_time']
        }]

        with get_cursor() as cur:
            cur.execute(
                insert_query,
                (
                    visit_id,
                    identity_id,
                    vehicle_id,
                    boat_id,
                    track['start_time'],
                    track['end_time'] or track['start_time'],
                    [],  # violation_ids
                    [track['track_id']],  # track_ids
                    json.dumps(camera_timeline),
                    datetime.now(timezone.utc)
                )
            )

        return visit_id

    def _update_camera_timeline(self, visit_id: str, camera_id: str,
                                  enter_time: str, exit_time: str = None):
        """Update the camera_timeline JSONB array for a visit.

        Timeline format: [{"camera": "cam1", "enter_time": "...", "exit_time": "..."}]

        Args:
            visit_id: UUID of the visit
            camera_id: Camera identifier
            enter_time: ISO timestamp string
            exit_time: Optional ISO timestamp string
        """
        # Get current timeline
        with get_cursor() as cur:
            cur.execute(
                "SELECT camera_timeline FROM visits WHERE visit_id = %s",
                (visit_id,)
            )
            row = cur.fetchone()
            if not row:
                return

            timeline = row[0] if row[0] else []

            # Check if camera already in timeline
            camera_found = False
            for entry in timeline:
                if entry.get('camera') == camera_id:
                    # Update exit time if newer
                    if exit_time:
                        entry['exit_time'] = exit_time
                    camera_found = True
                    break

            if not camera_found:
                # Add new camera entry
                timeline.append({
                    "camera": camera_id,
                    "enter_time": enter_time,
                    "exit_time": exit_time
                })

            # Update the timeline
            cur.execute(
                "UPDATE visits SET camera_timeline = %s WHERE visit_id = %s",
                (json.dumps(timeline), visit_id)
            )

    def end_stale_visits(self) -> int:
        """End visits that have been inactive for longer than visit_timeout.

        Sets departure_time to the last track's end_time.

        Returns:
            int: Count of visits ended
        """
        timeout_threshold = datetime.now(timezone.utc) - timedelta(minutes=self.visit_timeout)

        query = """
            UPDATE visits
            SET departure_time = (
                SELECT MAX(t.end_time)
                FROM tracks t
                WHERE t.track_id = ANY(visits.track_ids)
            )
            WHERE departure_time IS NULL
            AND arrival_time < %s
            AND NOT EXISTS (
                SELECT 1 FROM tracks t
                WHERE t.track_id = ANY(visits.track_ids)
                AND t.end_time > %s
            )
        """

        with get_cursor() as cur:
            cur.execute(query, (timeout_threshold, timeout_threshold))
            count = cur.rowcount

        self.logger.info(f"Ended {count} stale visits")
        return count

    def get_active_visits(self) -> list:
        """Get all visits where departure_time IS NULL (still on-site).

        Returns:
            list: Visit records as dicts
        """
        query = """
            SELECT visit_id, person_identity_id, vehicle_identity_id,
                   boat_identity_id, arrival_time, departure_time,
                   violation_ids, track_ids, camera_timeline, created_at
            FROM visits
            WHERE departure_time IS NULL
            ORDER BY arrival_time DESC
        """

        with get_cursor() as cur:
            cur.execute(query)
            return cur.fetchall()

    def get_visit_summary(self, visit_id: str) -> dict:
        """Get comprehensive visit summary including:
        - Person identity + name
        - Vehicle identity + plate/make/model
        - Boat identity + registration
        - Camera timeline
        - Associated violations
        - Duration

        Args:
            visit_id: UUID of the visit

        Returns:
            dict: Comprehensive visit summary
        """
        query = """
            SELECT
                v.visit_id,
                v.person_identity_id,
                v.vehicle_identity_id,
                v.boat_identity_id,
                v.arrival_time,
                v.departure_time,
                v.violation_ids,
                v.track_ids,
                v.camera_timeline,
                p.name as person_name,
                p.metadata as person_metadata,
                veh.plate_number,
                veh.make,
                veh.model,
                veh.metadata as vehicle_metadata,
                b.registration,
                b.name as boat_name,
                b.metadata as boat_metadata
            FROM visits v
            LEFT JOIN identities p ON v.person_identity_id = p.identity_id
            LEFT JOIN identities veh ON v.vehicle_identity_id = veh.identity_id
            LEFT JOIN identities b ON v.boat_identity_id = b.identity_id
            WHERE v.visit_id = %s
        """

        with get_cursor() as cur:
            cur.execute(query, (visit_id,))
            visit = cur.fetchone()

            if not visit:
                return None

            # Calculate duration
            if visit['departure_time']:
                duration = visit['departure_time'] - visit['arrival_time']
                duration_minutes = int(duration.total_seconds() / 60)
            else:
                duration = datetime.now(timezone.utc) - visit['arrival_time']
                duration_minutes = int(duration.total_seconds() / 60)

            # Get violation details
            violations = []
            if visit['violation_ids']:
                cur.execute("""
                    SELECT violation_id, violation_type, severity,
                           timestamp, camera_id, description
                    FROM violations
                    WHERE violation_id = ANY(%s)
                    ORDER BY timestamp
                """, (visit['violation_ids'],))
                violations = cur.fetchall()

            summary = {
                'visit_id': visit['visit_id'],
                'arrival_time': visit['arrival_time'].isoformat() if visit['arrival_time'] else None,
                'departure_time': visit['departure_time'].isoformat() if visit['departure_time'] else None,
                'duration_minutes': duration_minutes,
                'status': 'active' if not visit['departure_time'] else 'completed',
                'person': {
                    'identity_id': visit['person_identity_id'],
                    'name': visit['person_name'],
                    'metadata': visit['person_metadata']
                } if visit['person_identity_id'] else None,
                'vehicle': {
                    'identity_id': visit['vehicle_identity_id'],
                    'plate': visit['plate_number'],
                    'make': visit['make'],
                    'model': visit['model'],
                    'metadata': visit['vehicle_metadata']
                } if visit['vehicle_identity_id'] else None,
                'boat': {
                    'identity_id': visit['boat_identity_id'],
                    'registration': visit['registration'],
                    'name': visit['boat_name'],
                    'metadata': visit['boat_metadata']
                } if visit['boat_identity_id'] else None,
                'camera_timeline': visit['camera_timeline'],
                'violations': violations,
                'track_count': len(visit['track_ids'])
            }

            return summary

    def link_violation_to_visit(self, violation_id: str):
        """Find the active visit for the violation's person/vehicle/boat
        and add the violation_id to the visit's violation_ids array.

        Args:
            violation_id: UUID of the violation
        """
        # Get the violation details
        with get_cursor() as cur:
            cur.execute("""
                SELECT timestamp, person_id, vehicle_id, boat_id
                FROM violations
                WHERE violation_id = %s
            """, (violation_id,))
            violation = cur.fetchone()

            if not violation:
                self.logger.warning(f"Violation {violation_id} not found")
                return

            # Find the active visit at the time of violation
            # Try person first, then vehicle, then boat
            identity_id = violation['person_id'] or violation['vehicle_id'] or violation['boat_id']

            if not identity_id:
                self.logger.warning(f"Violation {violation_id} has no associated identity")
                return

            # Find visit that was active at violation timestamp
            cur.execute("""
                SELECT visit_id
                FROM visits
                WHERE (person_identity_id = %s
                       OR vehicle_identity_id = %s
                       OR boat_identity_id = %s)
                AND arrival_time <= %s
                AND (departure_time IS NULL OR departure_time >= %s)
                ORDER BY arrival_time DESC
                LIMIT 1
            """, (identity_id, identity_id, identity_id,
                  violation['timestamp'], violation['timestamp']))

            visit = cur.fetchone()

            if visit:
                # Add violation to visit
                cur.execute("""
                    UPDATE visits
                    SET violation_ids = array_append(violation_ids, %s)
                    WHERE visit_id = %s
                    AND NOT (%s = ANY(violation_ids))
                """, (violation_id, visit['visit_id'], violation_id))

                self.logger.info(f"Linked violation {violation_id} to visit {visit['visit_id']}")
            else:
                self.logger.warning(f"No active visit found for violation {violation_id}")


if __name__ == '__main__':
    """Standalone mode for testing and manual execution."""
    builder = VisitBuilder()

    print("Running visit aggregation...")
    result = builder.build_visits()
    print(f"Visit aggregation results: {json.dumps(result, indent=2)}")

    print("\nEnding stale visits...")
    stale = builder.end_stale_visits()
    print(f"Ended {stale} stale visits")

    print("\nActive visits:")
    active = builder.get_active_visits()
    for visit in active:
        print(f"  Visit {visit['visit_id']}: arrived {visit['arrival_time']}")
