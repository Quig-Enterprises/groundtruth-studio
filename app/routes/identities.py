import logging

from flask import Blueprint, request, jsonify
from psycopg2 import extras
from psycopg2.extras import Json

from db_connection import get_cursor, get_connection

identities_bp = Blueprint('identities', __name__)
logger = logging.getLogger(__name__)

# Keys that are never allowed in any identity or face payload
_FORBIDDEN_KEYS = frozenset({'image', 'photo', 'thumbnail', 'url', 'path', 'file'})


def _has_forbidden_keys(d):
    """Return the first forbidden key found in dict d, or None."""
    for key in d:
        if key.lower() in _FORBIDDEN_KEYS:
            return key
    return None


# ── POST /api/identities/sync ─────────────────────────────────────

@identities_bp.route('/api/identities/sync', methods=['POST'])
def sync_identities():
    """Bulk upsert / delete identities from an external source system."""
    try:
        data = request.get_json(force=True) or {}

        # --- validate top-level fields ---
        source_system = data.get('source_system')
        if not source_system or not isinstance(source_system, str):
            return jsonify({'success': False, 'error': 'source_system is required and must be a string'}), 400
        if len(source_system) > 50:
            return jsonify({'success': False, 'error': 'source_system must be 50 characters or fewer'}), 400

        identities = data.get('identities')
        if identities is None or not isinstance(identities, list):
            return jsonify({'success': False, 'error': 'identities is required and must be a list'}), 400

        created = 0
        updated = 0
        deleted = 0
        errors = []

        with get_connection() as conn:
            try:
                cursor = conn.cursor(cursor_factory=extras.RealDictCursor)

                for idx, identity in enumerate(identities):
                    if not isinstance(identity, dict):
                        errors.append({'index': idx, 'error': 'each identity must be an object'})
                        continue

                    # reject forbidden keys
                    bad_key = _has_forbidden_keys(identity)
                    if bad_key:
                        errors.append({'index': idx, 'error': f'forbidden key: {bad_key}'})
                        continue

                    external_id = identity.get('external_id')
                    if not external_id or not isinstance(external_id, str):
                        errors.append({'index': idx, 'error': 'external_id is required and must be a string'})
                        continue
                    if len(external_id) > 255:
                        errors.append({'index': idx, 'error': 'external_id must be 255 characters or fewer'})
                        continue

                    action = identity.get('action')
                    if action not in ('upsert', 'delete'):
                        errors.append({'index': idx, 'error': 'action must be "upsert" or "delete"'})
                        continue

                    if action == 'upsert':
                        name = identity.get('name')
                        if not name or not isinstance(name, str) or not name.strip():
                            errors.append({'index': idx, 'error': 'name is required and must be a non-empty string for upsert'})
                            continue

                        cursor.execute(
                            '''
                            INSERT INTO identities (identity_type, name, external_id, source_system)
                            VALUES ('person', %s, %s, %s)
                            ON CONFLICT (source_system, external_id) WHERE external_id IS NOT NULL
                            DO UPDATE SET name = EXCLUDED.name, last_seen = NOW()
                            RETURNING identity_id, (xmax = 0) AS inserted
                            ''',
                            (name.strip(), external_id, source_system),
                        )
                        row = cursor.fetchone()
                        if row['inserted']:
                            created += 1
                        else:
                            updated += 1

                    else:  # delete
                        cursor.execute(
                            'DELETE FROM identities WHERE source_system = %s AND external_id = %s',
                            (source_system, external_id),
                        )
                        deleted += cursor.rowcount

                conn.commit()
            except Exception:
                conn.rollback()
                raise

        return jsonify({
            'success': True,
            'created': created,
            'updated': updated,
            'deleted': deleted,
            'errors': errors,
        })

    except Exception as e:
        logger.error(f'Error in sync_identities: {e}', exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# ── POST /api/identities/ingest-embeddings ────────────────────────

@identities_bp.route('/api/identities/ingest-embeddings', methods=['POST'])
def ingest_embeddings():
    """Ingest face embeddings for external identities."""
    try:
        data = request.get_json(force=True) or {}

        # --- validate top-level fields ---
        source_system = data.get('source_system')
        if not source_system or not isinstance(source_system, str):
            return jsonify({'success': False, 'error': 'source_system is required'}), 400

        results = data.get('results')
        if results is None or not isinstance(results, list):
            return jsonify({'success': False, 'error': 'results is required and must be a list'}), 400

        identities_resolved = 0
        embeddings_stored = 0
        errors = []

        with get_connection() as conn:
            try:
                cursor = conn.cursor(cursor_factory=extras.RealDictCursor)

                for idx, result in enumerate(results):
                    if not isinstance(result, dict):
                        errors.append({'index': idx, 'error': 'each result must be an object'})
                        continue

                    # reject forbidden keys at the result level
                    bad_key = _has_forbidden_keys(result)
                    if bad_key:
                        errors.append({'index': idx, 'error': f'forbidden key: {bad_key}'})
                        continue

                    external_id = result.get('external_id')
                    if not external_id or not isinstance(external_id, str):
                        errors.append({'index': idx, 'error': 'external_id is required'})
                        continue

                    name = result.get('name')
                    if not name or not isinstance(name, str) or not name.strip():
                        errors.append({'index': idx, 'error': 'name is required and must be a non-empty string'})
                        continue

                    faces = result.get('faces', [])
                    if not isinstance(faces, list):
                        errors.append({'index': idx, 'error': 'faces must be a list'})
                        continue

                    # 1. Upsert identity
                    cursor.execute(
                        '''
                        INSERT INTO identities (identity_type, name, external_id, source_system)
                        VALUES ('person', %s, %s, %s)
                        ON CONFLICT (source_system, external_id) WHERE external_id IS NOT NULL
                        DO UPDATE SET name = EXCLUDED.name, last_seen = NOW()
                        RETURNING identity_id
                        ''',
                        (name.strip(), external_id, source_system),
                    )
                    row = cursor.fetchone()
                    identity_id = row['identity_id']
                    identities_resolved += 1

                    # Clear existing reference embeddings for this identity from this source
                    cursor.execute(
                        '''
                        DELETE FROM embeddings
                        WHERE identity_id = %s
                          AND embedding_type = 'face'
                          AND is_reference = true
                          AND source_image_path IS NULL
                        ''',
                        (identity_id,),
                    )

                    # 2. Insert each face embedding
                    for face_idx, face in enumerate(faces):
                        if not isinstance(face, dict):
                            errors.append({'index': idx, 'face_index': face_idx, 'error': 'each face must be an object'})
                            continue

                        # reject forbidden keys at the face level
                        bad_key = _has_forbidden_keys(face)
                        if bad_key:
                            errors.append({'index': idx, 'face_index': face_idx, 'error': f'forbidden key: {bad_key}'})
                            continue

                        embedding = face.get('embedding')
                        if not isinstance(embedding, list) or len(embedding) != 512:
                            errors.append({
                                'index': idx,
                                'face_index': face_idx,
                                'error': 'embedding must be a list of exactly 512 floats',
                            })
                            continue

                        if not all(isinstance(v, (int, float)) for v in embedding):
                            errors.append({
                                'index': idx,
                                'face_index': face_idx,
                                'error': 'embedding values must all be numeric',
                            })
                            continue

                        confidence = face.get('confidence')
                        if confidence is None or not isinstance(confidence, (int, float)):
                            errors.append({
                                'index': idx,
                                'face_index': face_idx,
                                'error': 'confidence is required and must be a number',
                            })
                            continue

                        if not (0.0 <= float(confidence) <= 1.0):
                            errors.append({
                                'index': idx,
                                'face_index': face_idx,
                                'error': 'confidence must be between 0.0 and 1.0',
                            })
                            continue

                        cursor.execute(
                            '''
                            INSERT INTO embeddings (identity_id, embedding_type, vector, confidence, is_reference, source_image_path)
                            VALUES (%s, 'face', %s, %s, true, NULL)
                            ''',
                            (identity_id, embedding, float(confidence)),
                        )
                        embeddings_stored += 1

                conn.commit()
            except Exception:
                conn.rollback()
                raise

        return jsonify({
            'success': True,
            'identities_resolved': identities_resolved,
            'embeddings_stored': embeddings_stored,
            'errors': errors,
        })

    except Exception as e:
        logger.error(f'Error in ingest_embeddings: {e}', exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# ── GET /api/identities/external ─────────────────────────────────

@identities_bp.route('/api/identities/external', methods=['GET'])
def list_external_identities():
    """List identities that have an external_id, with embedding counts."""
    try:
        source_system_filter = request.args.get('source_system')

        with get_cursor(commit=False) as cursor:
            if source_system_filter:
                cursor.execute(
                    '''
                    SELECT i.identity_id, i.external_id, i.source_system, i.name,
                           COUNT(e.embedding_id) AS embedding_count,
                           i.first_seen, i.last_seen
                    FROM identities i
                    LEFT JOIN embeddings e ON i.identity_id = e.identity_id
                    WHERE i.external_id IS NOT NULL
                      AND i.source_system = %s
                    GROUP BY i.identity_id
                    ORDER BY i.source_system, i.name
                    ''',
                    (source_system_filter,),
                )
            else:
                cursor.execute(
                    '''
                    SELECT i.identity_id, i.external_id, i.source_system, i.name,
                           COUNT(e.embedding_id) AS embedding_count,
                           i.first_seen, i.last_seen
                    FROM identities i
                    LEFT JOIN embeddings e ON i.identity_id = e.identity_id
                    WHERE i.external_id IS NOT NULL
                    GROUP BY i.identity_id
                    ORDER BY i.source_system, i.name
                    '''
                )

            rows = cursor.fetchall()
            identities = [dict(row) for row in rows]

        return jsonify({'success': True, 'identities': identities})

    except Exception as e:
        logger.error(f'Error in list_external_identities: {e}', exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500
