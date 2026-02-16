import json
import math
import uuid
from datetime import datetime
from typing import List, Dict, Optional

from psycopg2 import extras

from db_connection import get_cursor, get_connection


# Helper for cosine similarity computation
try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False


def _cosine_similarity(vec_a, vec_b):
    """Compute cosine similarity between two vectors."""
    if _HAS_NUMPY:
        a = np.array(vec_a, dtype=np.float32)
        b = np.array(vec_b, dtype=np.float32)
        dot = np.dot(a, b)
        norm = np.linalg.norm(a) * np.linalg.norm(b)
        return float(dot / norm) if norm > 0 else 0.0
    else:
        dot = sum(a * b for a, b in zip(vec_a, vec_b))
        norm_a = math.sqrt(sum(a * a for a in vec_a))
        norm_b = math.sqrt(sum(b * b for b in vec_b))
        norm = norm_a * norm_b
        return dot / norm if norm > 0 else 0.0


class PersonMixin:
    """Person identities, embeddings, associations, person-tracks, and sightings."""

    # ==================== Identities ====================

    def create_identity(self, identity_type: str, name: str = None,
                        metadata: dict = None, is_flagged: bool = False,
                        notes: str = None) -> Dict:
        """Create a new identity record."""
        with get_cursor() as cursor:
            cursor.execute('''
                INSERT INTO identities (identity_type, name, metadata, is_flagged, notes)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
            ''', (identity_type, name,
                  extras.Json(metadata) if metadata else extras.Json({}),
                  is_flagged, notes))
            row = cursor.fetchone()
            return dict(row)

    def get_identity(self, identity_id: str) -> Optional[Dict]:
        """Get an identity by ID."""
        with get_cursor(commit=False) as cursor:
            cursor.execute('SELECT * FROM identities WHERE identity_id = %s',
                           (identity_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_identities(self, identity_type: str = None, is_flagged: bool = None,
                       search: str = None, limit: int = 100, offset: int = 0) -> List[Dict]:
        """Get identities with optional filters. Search matches name or notes."""
        conditions = []
        params = []
        if identity_type is not None:
            conditions.append('identity_type = %s')
            params.append(identity_type)
        if is_flagged is not None:
            conditions.append('is_flagged = %s')
            params.append(is_flagged)
        if search is not None:
            conditions.append('(name ILIKE %s OR notes ILIKE %s)')
            params.extend([f'%{search}%', f'%{search}%'])

        where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
        params.extend([limit, offset])

        with get_cursor(commit=False) as cursor:
            cursor.execute(f'''
                SELECT * FROM identities
                {where}
                ORDER BY last_seen DESC
                LIMIT %s OFFSET %s
            ''', params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def update_identity(self, identity_id: str, **kwargs) -> Optional[Dict]:
        """Update an identity. Allowed fields: name, metadata, is_flagged, notes, last_seen."""
        allowed = {'name', 'metadata', 'is_flagged', 'notes', 'last_seen'}
        updates = []
        values = []
        for key, value in kwargs.items():
            if key not in allowed:
                continue
            if key == 'metadata':
                updates.append('metadata = %s')
                values.append(extras.Json(value))
            else:
                updates.append(f'{key} = %s')
                values.append(value)

        if not updates:
            return None

        values.append(identity_id)
        with get_cursor() as cursor:
            cursor.execute(f'''
                UPDATE identities SET {', '.join(updates)}
                WHERE identity_id = %s
                RETURNING *
            ''', values)
            row = cursor.fetchone()
            return dict(row) if row else None

    def delete_identity(self, identity_id: str) -> bool:
        """Delete an identity by ID."""
        with get_cursor() as cursor:
            cursor.execute('DELETE FROM identities WHERE identity_id = %s',
                           (identity_id,))
            return cursor.rowcount > 0

    # ==================== Embeddings ====================

    def insert_embedding(self, identity_id: str, embedding_type: str,
                         vector: list, confidence: float,
                         source_image_path: str = None, camera_id: str = None,
                         is_reference: bool = False, session_date=None) -> Dict:
        """Insert a new embedding vector."""
        with get_cursor() as cursor:
            cursor.execute('''
                INSERT INTO embeddings
                (identity_id, embedding_type, vector, confidence,
                 source_image_path, camera_id, is_reference, session_date)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
            ''', (identity_id, embedding_type, vector, confidence,
                  source_image_path, camera_id, is_reference, session_date))
            row = cursor.fetchone()
            return dict(row)

    def get_embeddings(self, identity_id: str = None, embedding_type: str = None,
                       is_reference: bool = None, limit: int = 100) -> List[Dict]:
        """Get embeddings with optional filters."""
        conditions = []
        params = []
        if identity_id is not None:
            conditions.append('identity_id = %s')
            params.append(identity_id)
        if embedding_type is not None:
            conditions.append('embedding_type = %s')
            params.append(embedding_type)
        if is_reference is not None:
            conditions.append('is_reference = %s')
            params.append(is_reference)

        where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
        params.append(limit)

        with get_cursor(commit=False) as cursor:
            cursor.execute(f'''
                SELECT * FROM embeddings
                {where}
                ORDER BY created_at DESC
                LIMIT %s
            ''', params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def find_similar_embeddings(self, vector: list, embedding_type: str,
                                threshold: float = 0.6, limit: int = 10,
                                session_date=None) -> List[Dict]:
        """Find similar embeddings by cosine similarity. Fetches candidates then
        computes similarity in Python."""
        conditions = ['embedding_type = %s']
        params = [embedding_type]
        if session_date is not None:
            conditions.append('session_date = %s')
            params.append(session_date)

        where = 'WHERE ' + ' AND '.join(conditions)

        with get_cursor(commit=False) as cursor:
            cursor.execute(f'''
                SELECT * FROM embeddings
                {where}
                ORDER BY created_at DESC
            ''', params)
            rows = cursor.fetchall()

        results = []
        for row in rows:
            row_dict = dict(row)
            candidate_vector = row_dict['vector']
            similarity = _cosine_similarity(vector, candidate_vector)
            if similarity >= threshold:
                row_dict['similarity'] = round(similarity, 6)
                results.append(row_dict)

        results.sort(key=lambda x: x['similarity'], reverse=True)
        return results[:limit]

    def delete_embedding(self, embedding_id: str) -> bool:
        """Delete an embedding by ID."""
        with get_cursor() as cursor:
            cursor.execute('DELETE FROM embeddings WHERE embedding_id = %s',
                           (embedding_id,))
            return cursor.rowcount > 0

    # ==================== Associations ====================

    def upsert_association(self, identity_a: str, identity_b: str,
                           association_type: str,
                           confidence_delta: float = 0.1) -> Dict:
        """Create or update an association between two identities.
        On conflict, increments observation_count and adds confidence_delta (capped at 1.0)."""
        with get_cursor() as cursor:
            cursor.execute('''
                INSERT INTO associations (identity_a, identity_b, association_type,
                                          confidence, observation_count)
                VALUES (%s, %s, %s, %s, 1)
                ON CONFLICT (identity_a, identity_b, association_type) DO UPDATE SET
                    observation_count = associations.observation_count + 1,
                    confidence = LEAST(associations.confidence + %s, 1.0),
                    last_observed = NOW()
                RETURNING *
            ''', (identity_a, identity_b, association_type,
                  confidence_delta, confidence_delta))
            row = cursor.fetchone()
            return dict(row)

    def get_associations(self, identity_id: str,
                         association_type: str = None) -> List[Dict]:
        """Get all associations involving an identity (as either side)."""
        conditions = ['(identity_a = %s OR identity_b = %s)']
        params = [identity_id, identity_id]
        if association_type is not None:
            conditions.append('association_type = %s')
            params.append(association_type)

        where = 'WHERE ' + ' AND '.join(conditions)

        with get_cursor(commit=False) as cursor:
            cursor.execute(f'''
                SELECT a.*,
                       ia.name AS identity_a_name, ia.identity_type AS identity_a_type,
                       ib.name AS identity_b_name, ib.identity_type AS identity_b_type
                FROM associations a
                LEFT JOIN identities ia ON a.identity_a = ia.identity_id
                LEFT JOIN identities ib ON a.identity_b = ib.identity_id
                {where}
                ORDER BY a.confidence DESC
            ''', params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_association_chain(self, identity_id: str) -> Dict:
        """Get the full association chain for an identity.
        Returns dict with keys: persons, vehicles, trailers, boats containing
        associated identities found by walking the association graph."""
        result = {
            'persons': [],
            'vehicles': [],
            'trailers': [],
            'boats': []
        }
        visited = set()
        queue = [identity_id]

        while queue:
            current_id = queue.pop(0)
            if current_id in visited:
                continue
            visited.add(current_id)

            associations = self.get_associations(current_id)
            for assoc in associations:
                # Determine the other identity in the association
                other_id = assoc['identity_b'] if str(assoc['identity_a']) == str(current_id) else assoc['identity_a']
                other_id_str = str(other_id)
                if other_id_str not in visited:
                    queue.append(other_id_str)

        # Now fetch all visited identities (except the original)
        visited.discard(identity_id)
        if not visited:
            return result

        with get_cursor(commit=False) as cursor:
            placeholders = ','.join(['%s'] * len(visited))
            cursor.execute(f'''
                SELECT * FROM identities
                WHERE identity_id IN ({placeholders})
            ''', list(visited))
            rows = cursor.fetchall()

        type_map = {
            'person': 'persons',
            'vehicle': 'vehicles',
            'trailer': 'trailers',
            'boat': 'boats'
        }
        for row in rows:
            row_dict = dict(row)
            key = type_map.get(row_dict['identity_type'])
            if key:
                result[key].append(row_dict)

        return result

    # ==================== Tracks ====================

    def create_track(self, camera_id: str, entity_type: str,
                     identity_id: str = None, identity_method: str = None,
                     identity_confidence: float = None) -> Dict:
        """Create a new track for entity observation within a camera."""
        with get_cursor() as cursor:
            cursor.execute('''
                INSERT INTO tracks (camera_id, entity_type, identity_id,
                                    identity_method, identity_confidence)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
            ''', (camera_id, entity_type, identity_id,
                  identity_method, identity_confidence))
            row = cursor.fetchone()
            return dict(row)

    def end_track(self, track_id: str) -> bool:
        """End a track by setting end_time to NOW()."""
        with get_cursor() as cursor:
            cursor.execute('''
                UPDATE tracks SET end_time = NOW()
                WHERE track_id = %s AND end_time IS NULL
            ''', (track_id,))
            return cursor.rowcount > 0

    def get_track(self, track_id: str) -> Optional[Dict]:
        """Get a track by ID."""
        with get_cursor(commit=False) as cursor:
            cursor.execute('SELECT * FROM tracks WHERE track_id = %s',
                           (track_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_tracks(self, camera_id: str = None, entity_type: str = None,
                   identity_id: str = None, start_after=None, start_before=None,
                   active_only: bool = False, limit: int = 100,
                   offset: int = 0) -> List[Dict]:
        """Get tracks with optional filters."""
        conditions = []
        params = []
        if camera_id is not None:
            conditions.append('camera_id = %s')
            params.append(camera_id)
        if entity_type is not None:
            conditions.append('entity_type = %s')
            params.append(entity_type)
        if identity_id is not None:
            conditions.append('identity_id = %s')
            params.append(identity_id)
        if start_after is not None:
            conditions.append('start_time >= %s')
            params.append(start_after)
        if start_before is not None:
            conditions.append('start_time <= %s')
            params.append(start_before)
        if active_only:
            conditions.append('end_time IS NULL')

        where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
        params.extend([limit, offset])

        with get_cursor(commit=False) as cursor:
            cursor.execute(f'''
                SELECT * FROM tracks
                {where}
                ORDER BY start_time DESC
                LIMIT %s OFFSET %s
            ''', params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def link_track_to_identity(self, track_id: str, identity_id: str,
                               method: str, confidence: float) -> bool:
        """Link a track to an identity with identification method and confidence."""
        with get_cursor() as cursor:
            cursor.execute('''
                UPDATE tracks
                SET identity_id = %s, identity_method = %s, identity_confidence = %s
                WHERE track_id = %s
            ''', (identity_id, method, confidence, track_id))
            return cursor.rowcount > 0

    def get_active_tracks(self, camera_id: str = None) -> List[Dict]:
        """Get all active tracks (end_time IS NULL), optionally filtered by camera."""
        conditions = ['end_time IS NULL']
        params = []
        if camera_id is not None:
            conditions.append('camera_id = %s')
            params.append(camera_id)

        where = 'WHERE ' + ' AND '.join(conditions)

        with get_cursor(commit=False) as cursor:
            cursor.execute(f'''
                SELECT * FROM tracks
                {where}
                ORDER BY start_time DESC
            ''', params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    # ==================== Sightings ====================

    def batch_insert_sightings(self, sightings: List[Dict]) -> int:
        """Batch insert sightings using execute_values for performance.
        Each sighting dict should have: track_id, timestamp, bbox, confidence, face_visible."""
        if not sightings:
            return 0

        values = [
            (s['track_id'], s['timestamp'], s['bbox'],
             s['confidence'], s.get('face_visible', False))
            for s in sightings
        ]

        with get_cursor() as cursor:
            extras.execute_values(
                cursor,
                '''INSERT INTO sightings (track_id, timestamp, bbox, confidence, face_visible)
                   VALUES %s''',
                values,
                template='(%s, %s, %s, %s, %s)'
            )
            return cursor.rowcount

    def get_track_sightings(self, track_id: str,
                            limit: int = None) -> List[Dict]:
        """Get all sightings for a track, ordered by timestamp ASC."""
        params = [track_id]
        limit_clause = ''
        if limit is not None:
            limit_clause = 'LIMIT %s'
            params.append(limit)

        with get_cursor(commit=False) as cursor:
            cursor.execute(f'''
                SELECT * FROM sightings
                WHERE track_id = %s
                ORDER BY timestamp ASC
                {limit_clause}
            ''', params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    # ==================== Camera Topology Learned ====================

    def upsert_camera_transit(self, camera_a: str, camera_b: str,
                              transit_seconds: int) -> Dict:
        """Insert or update camera transit time observation.
        On insert: sets min=max=avg=transit_seconds, count=1.
        On conflict: updates min/max, recalculates running avg, increments count."""
        with get_cursor() as cursor:
            cursor.execute('''
                INSERT INTO camera_topology_learned
                (camera_a, camera_b, min_transit_seconds, max_transit_seconds,
                 avg_transit_seconds, observation_count)
                VALUES (%s, %s, %s, %s, %s, 1)
                ON CONFLICT (camera_a, camera_b) DO UPDATE SET
                    min_transit_seconds = LEAST(camera_topology_learned.min_transit_seconds, EXCLUDED.min_transit_seconds),
                    max_transit_seconds = GREATEST(camera_topology_learned.max_transit_seconds, EXCLUDED.max_transit_seconds),
                    avg_transit_seconds = (
                        camera_topology_learned.avg_transit_seconds * camera_topology_learned.observation_count
                        + EXCLUDED.avg_transit_seconds
                    ) / (camera_topology_learned.observation_count + 1),
                    observation_count = camera_topology_learned.observation_count + 1
                RETURNING *
            ''', (camera_a, camera_b, transit_seconds, transit_seconds,
                  float(transit_seconds)))
            row = cursor.fetchone()
            return dict(row)

    def get_adjacent_cameras(self, camera_id: str) -> List[Dict]:
        """Get all cameras adjacent to the given camera (in either direction)."""
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT * FROM camera_topology_learned
                WHERE camera_a = %s OR camera_b = %s
                ORDER BY avg_transit_seconds ASC
            ''', (camera_id, camera_id))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
