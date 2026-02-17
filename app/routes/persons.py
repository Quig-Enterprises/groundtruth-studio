from datetime import datetime
from flask import Blueprint, request, jsonify, render_template, redirect, g
from psycopg2 import extras
from db_connection import get_cursor, get_connection
import services
from services import db, topology_learner, face_clusterer, THUMBNAIL_DIR
from face_clustering import FaceClusterer
from person_recognizer import get_recognizer
import os
import json
import logging
import time
import threading

persons_bp = Blueprint('persons', __name__)
logger = logging.getLogger(__name__)


# ── Person Manager ────────────────────────────────────────────────

@persons_bp.route('/person-manager')
def person_manager():
    """Person name management interface"""
    return render_template('person_manager.html')

@persons_bp.route('/api/person-detections', methods=['GET'])
def get_person_detections():
    """Get all person identification annotations with names"""
    try:
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)

            # Get all keyframe annotations that are person identifications
            # (have person_name tag or are from person_identification scenario)
            cursor.execute('''
                SELECT
                    ka.id,
                    ka.video_id,
                    ka.timestamp,
                    ka.bbox_x,
                    ka.bbox_y,
                    ka.bbox_width,
                    ka.bbox_height,
                    ka.created_date,
                    v.title as video_title,
                    v.thumbnail_path,
                    STRING_AGG(
                        CASE WHEN at.tag_value LIKE '%%person_name%%'
                        THEN at.tag_value::json->>'person_name'
                        END, ','
                    ) as person_name_json,
                    STRING_AGG(
                        CASE WHEN at.tag_value LIKE '%%pose%%'
                        THEN REPLACE(REPLACE(at.tag_value, '"pose":', ''), '"', '')
                        END, ','
                    ) as pose,
                    STRING_AGG(
                        CASE WHEN at.tag_value LIKE '%%distance_category%%'
                        THEN REPLACE(REPLACE(at.tag_value, '"distance_category":', ''), '"', '')
                        END, ','
                    ) as distance_category
                FROM keyframe_annotations ka
                JOIN videos v ON ka.video_id = v.id
                LEFT JOIN annotation_tags at ON ka.id = at.annotation_id AND at.annotation_type = 'keyframe'
                WHERE EXISTS (
                    SELECT 1 FROM annotation_tags at2
                    WHERE at2.annotation_id = ka.id
                    AND at2.annotation_type = 'keyframe'
                    AND (at2.tag_value LIKE '%%person_identification%%' OR at2.tag_value LIKE '%%person_name%%')
                )
                GROUP BY ka.id, ka.video_id, ka.timestamp, ka.bbox_x, ka.bbox_y, ka.bbox_width, ka.bbox_height, ka.created_date, v.title, v.thumbnail_path
                ORDER BY ka.created_date DESC
            ''')

            detections = []
            for row in cursor.fetchall():
                # Parse person name from JSON-extracted value
                person_name = None
                if row['person_name_json']:
                    # STRING_AGG may produce comma-separated names, take first
                    name = row['person_name_json'].split(',')[0].strip()
                    if name and name != 'Unknown':
                        person_name = name

                detections.append({
                    'id': row['id'],
                    'video_id': row['video_id'],
                    'video_title': row['video_title'],
                    'timestamp': row['timestamp'],
                    'bbox_x': row['bbox_x'],
                    'bbox_y': row['bbox_y'],
                    'bbox_width': row['bbox_width'],
                    'bbox_height': row['bbox_height'],
                    'thumbnail_path': f"/thumbnails/{os.path.basename(row['thumbnail_path'])}" if row.get('thumbnail_path') else None,
                    'person_name': person_name,
                    'pose': row['pose'],
                    'distance_category': row['distance_category'],
                    'created_date': row['created_date']
                })

            # Get statistics
            cursor.execute('''
                SELECT COUNT(DISTINCT video_id) as count FROM keyframe_annotations
                WHERE id IN (
                    SELECT annotation_id FROM annotation_tags
                    WHERE annotation_type = 'keyframe'
                    AND (tag_value LIKE '%%person_identification%%' OR tag_value LIKE '%%person_name%%')
                )
            ''')
            videos_with_people = cursor.fetchone()['count']

            # Count distinct named people and unknown detections
            named_set = set(d['person_name'] for d in detections if d['person_name'] and d['person_name'] != 'Unknown')
            named_count = len(named_set)
            unknown_count = len([d for d in detections if not d['person_name'] or d['person_name'] == 'Unknown'])

            # Get unique people
            people = {}
            for d in detections:
                name = d['person_name'] or 'Unknown'
                if name not in people:
                    people[name] = {'name': name, 'count': 0}
                people[name]['count'] += 1

            people_list = sorted(people.values(), key=lambda x: x['count'], reverse=True)

            stats = {
                'total_detections': len(detections),
                'named_people': named_count,
                'unknown': unknown_count,
                'videos_with_people': videos_with_people
            }

        return jsonify({
            'success': True,
            'detections': detections,
            'people': people_list,
            'stats': stats
        })
    except Exception as e:
        print(f"Error in get_person_detections: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@persons_bp.route('/api/person-names/recent', methods=['GET'])
def get_recent_person_names():
    """Get recently used person names"""
    try:
        limit = int(request.args.get('limit', 10))
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)

            # Get unique person names from annotation_tags
            cursor.execute('''
                SELECT DISTINCT
                    tag_value::json->>'person_name' as person_name,
                    MAX(created_date) as last_used
                FROM annotation_tags
                WHERE annotation_type = 'keyframe'
                AND tag_value LIKE '%%person_name%%'
                AND tag_value NOT LIKE '%%Unknown%%'
                AND tag_value != ''
                GROUP BY tag_value::json->>'person_name'
                ORDER BY last_used DESC
                LIMIT %s
            ''', (limit,))

            names = [row['person_name'] for row in cursor.fetchall() if row['person_name']]

        return jsonify({'success': True, 'names': names})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@persons_bp.route('/api/tag-values/recent', methods=['GET'])
def get_recent_tag_values():
    """Get recently used values for any tag (make, model, fleet_id, etc.)"""
    try:
        tag_name = request.args.get('tag_name', '')
        limit = int(request.args.get('limit', 10))

        if not tag_name:
            return jsonify({'success': False, 'error': 'tag_name required'}), 400

        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)

            # Get unique values for this tag using JSON extraction
            cursor.execute('''
                SELECT DISTINCT
                    tag_value::json->>%s as tag_value,
                    MAX(created_date) as last_used
                FROM annotation_tags
                WHERE annotation_type = 'keyframe'
                AND tag_value LIKE %s
                AND tag_value != ''
                GROUP BY tag_value::json->>%s
                ORDER BY last_used DESC
                LIMIT %s
            ''', (tag_name, f'%{tag_name}%', tag_name, limit))

            values = [row['tag_value'].strip() for row in cursor.fetchall() if row['tag_value'] and row['tag_value'].strip()]

        return jsonify({'success': True, 'values': values})
    except Exception as e:
        print(f"Error in get_recent_tag_values: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@persons_bp.route('/api/person-detections/assign-name', methods=['POST'])
def assign_person_name():
    """Assign a name to one or more person detections"""
    try:
        data = request.get_json()
        detection_ids = data.get('detection_ids', [])
        person_name = data.get('person_name', '').strip()

        if not detection_ids or not person_name:
            return jsonify({'success': False, 'error': 'Missing required fields'}), 400

        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)

            updated_count = 0
            for detection_id in detection_ids:
                # Check if person_name tag already exists for this annotation
                cursor.execute('''
                    SELECT id FROM annotation_tags
                    WHERE annotation_id = %s AND annotation_type = 'keyframe'
                    AND tag_value LIKE '%%person_name%%'
                ''', (detection_id,))

                existing = cursor.fetchone()

                tag_value = json.dumps({'person_name': person_name})

                if existing:
                    # Update existing tag
                    cursor.execute('''
                        UPDATE annotation_tags
                        SET tag_value = %s, created_date = CURRENT_TIMESTAMP
                        WHERE id = %s
                    ''', (tag_value, existing['id']))
                else:
                    # Create new tag (need to get or create tag group first)
                    cursor.execute('''
                        SELECT id FROM tag_groups WHERE group_name = 'person_name'
                    ''')
                    group = cursor.fetchone()

                    if not group:
                        # Create tag group for person names
                        cursor.execute('''
                            INSERT INTO tag_groups (group_name, display_name, group_type, description)
                            VALUES ('person_name', 'Person Name', 'text', 'Name of identified person')
                            RETURNING id
                        ''')
                        group_id = cursor.fetchone()['id']
                    else:
                        group_id = group['id']

                    # Insert tag
                    cursor.execute('''
                        INSERT INTO annotation_tags (annotation_id, annotation_type, group_id, tag_value)
                        VALUES (%s, 'keyframe', %s, %s)
                    ''', (detection_id, group_id, tag_value))

                updated_count += 1

            conn.commit()

        return jsonify({'success': True, 'updated_count': updated_count})
    except Exception as e:
        print(f"Error in assign_person_name: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@persons_bp.route('/api/person-detections/unassign-name', methods=['POST'])
def unassign_person_name():
    """Remove name assignment from person detections"""
    try:
        data = request.get_json()
        detection_ids = data.get('detection_ids', [])

        if not detection_ids:
            return jsonify({'success': False, 'error': 'No detection IDs provided'}), 400

        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)

            # Delete person_name tags for these detections
            placeholders = ','.join(['%s'] * len(detection_ids))
            cursor.execute(f'''
                DELETE FROM annotation_tags
                WHERE annotation_id IN ({placeholders})
                AND annotation_type = 'keyframe'
                AND tag_value LIKE '%%person_name%%'
            ''', detection_ids)

            updated_count = cursor.rowcount
            conn.commit()

        return jsonify({'success': True, 'updated_count': updated_count})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ── Camera Topology Learning ─────────────────────────────────────

@persons_bp.route('/api/camera-topology/graph', methods=['GET'])
def get_camera_topology():
    """Get learned camera graph based on person transitions"""
    try:
        graph = topology_learner.build_camera_graph()
        return jsonify({'success': True, 'graph': graph})
    except Exception as e:
        print(f"Error in get_camera_topology: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@persons_bp.route('/api/camera-topology/person/<person_name>/transitions', methods=['GET'])
def get_person_transitions(person_name):
    """Get all camera transitions for a specific person"""
    try:
        transitions = topology_learner.analyze_person_transitions(person_name)
        return jsonify({'success': True, 'transitions': transitions})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@persons_bp.route('/api/camera-topology/person/<person_name>/path', methods=['GET'])
def get_person_path(person_name):
    """Get complete movement path for a person"""
    try:
        path = topology_learner.get_person_movement_path(person_name)
        return jsonify({'success': True, 'path': path})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@persons_bp.route('/api/camera-topology/suggest-links', methods=['POST'])
def suggest_track_links():
    """Suggest which unassigned detections might belong to a person"""
    try:
        data = request.get_json()
        person_name = data.get('person_name')
        time_window = data.get('time_window_seconds', 300)

        if not person_name:
            return jsonify({'success': False, 'error': 'person_name required'}), 400

        suggestions = topology_learner.suggest_track_links(person_name, time_window)
        return jsonify({'success': True, 'suggestions': suggestions})
    except Exception as e:
        print(f"Error in suggest_track_links: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@persons_bp.route('/camera-topology')
def camera_topology_viewer():
    """Legacy route — redirect to camera management."""
    return redirect('/camera-management')


@persons_bp.route('/camera-management')
def camera_management_viewer():
    """Camera management interface — topology, locations, and aliases"""
    return render_template('camera_management.html')


# ── Multi-Entity Detection: Identities ───────────────────────────

@persons_bp.route('/api/identities', methods=['GET'])
def list_identities():
    """List/search identities with optional filters"""
    try:
        identity_type = request.args.get('type')
        is_flagged = request.args.get('flagged')
        if is_flagged is not None:
            is_flagged = is_flagged.lower() in ('true', '1', 'yes')
        search = request.args.get('search')
        limit = int(request.args.get('limit', 100))
        offset = int(request.args.get('offset', 0))
        identities = db.get_identities(
            identity_type=identity_type,
            is_flagged=is_flagged,
            search=search,
            limit=limit,
            offset=offset
        )
        return jsonify({'success': True, 'identities': identities})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@persons_bp.route('/api/identities', methods=['POST'])
def create_identity():
    """Create a new identity"""
    try:
        data = request.get_json()
        identity_type = data.get('identity_type')
        if not identity_type:
            return jsonify({'success': False, 'error': 'identity_type is required'}), 400
        identity = db.create_identity(
            identity_type=identity_type,
            name=data.get('name'),
            metadata=data.get('metadata'),
            is_flagged=data.get('is_flagged', False),
            notes=data.get('notes')
        )
        return jsonify({'success': True, 'identity': identity})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@persons_bp.route('/api/identities/<identity_id>', methods=['GET'])
def get_identity(identity_id):
    """Get a single identity by ID"""
    try:
        identity = db.get_identity(identity_id)
        if not identity:
            return jsonify({'success': False, 'error': 'Identity not found'}), 404
        return jsonify({'success': True, 'identity': identity})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@persons_bp.route('/api/identities/<identity_id>', methods=['PUT'])
def update_identity(identity_id):
    """Update an existing identity"""
    try:
        data = request.get_json()
        allowed = {}
        for key in ('name', 'metadata', 'is_flagged', 'notes', 'last_seen'):
            if key in data:
                allowed[key] = data[key]
        identity = db.update_identity(identity_id, **allowed)
        if not identity:
            return jsonify({'success': False, 'error': 'Identity not found'}), 404
        return jsonify({'success': True, 'identity': identity})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@persons_bp.route('/api/identities/<identity_id>', methods=['DELETE'])
def delete_identity(identity_id):
    """Delete an identity"""
    try:
        deleted = db.delete_identity(identity_id)
        if not deleted:
            return jsonify({'success': False, 'error': 'Identity not found'}), 404
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@persons_bp.route('/api/identities/<identity_id>/associations', methods=['GET'])
def get_identity_associations(identity_id):
    """Get associations and full chain for an identity"""
    try:
        association_type = request.args.get('type')
        associations = db.get_associations(identity_id, association_type=association_type)
        chain = db.get_association_chain(identity_id)
        return jsonify({'success': True, 'associations': associations, 'chain': chain})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@persons_bp.route('/api/identities/<identity_id>/embeddings', methods=['GET'])
def get_identity_embeddings(identity_id):
    """Get embeddings for an identity"""
    try:
        embedding_type = request.args.get('type')
        is_reference = request.args.get('is_reference')
        if is_reference is not None:
            is_reference = is_reference.lower() in ('true', '1', 'yes')
        limit = int(request.args.get('limit', 100))
        embeddings = db.get_embeddings(
            identity_id=identity_id,
            embedding_type=embedding_type,
            is_reference=is_reference,
            limit=limit
        )
        return jsonify({'success': True, 'embeddings': embeddings})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@persons_bp.route('/api/identities/<identity_id>/timeline', methods=['GET'])
def get_identity_timeline(identity_id):
    """Get all tracks for an identity ordered by time"""
    try:
        limit = int(request.args.get('limit', 100))
        offset = int(request.args.get('offset', 0))
        tracks = db.get_tracks(
            identity_id=identity_id,
            limit=limit,
            offset=offset
        )
        return jsonify({'success': True, 'tracks': tracks})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ── Multi-Entity Detection: Embeddings ───────────────────────────

@persons_bp.route('/api/embeddings', methods=['POST'])
def insert_embedding():
    """Insert a new embedding vector"""
    try:
        data = request.get_json()
        identity_id = data.get('identity_id')
        embedding_type = data.get('embedding_type')
        vector = data.get('vector')
        confidence = data.get('confidence')
        if not all([identity_id, embedding_type, vector, confidence is not None]):
            return jsonify({'success': False, 'error': 'identity_id, embedding_type, vector, and confidence are required'}), 400
        embedding = db.insert_embedding(
            identity_id=identity_id,
            embedding_type=embedding_type,
            vector=vector,
            confidence=confidence,
            source_image_path=data.get('source_image_path'),
            camera_id=data.get('camera_id'),
            is_reference=data.get('is_reference', False),
            session_date=data.get('session_date')
        )
        return jsonify({'success': True, 'embedding': embedding})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@persons_bp.route('/api/embeddings/search', methods=['POST'])
def search_embeddings():
    """Find similar embeddings by vector similarity"""
    try:
        data = request.get_json()
        vector = data.get('vector')
        embedding_type = data.get('embedding_type')
        if not vector or not embedding_type:
            return jsonify({'success': False, 'error': 'vector and embedding_type are required'}), 400
        results = db.find_similar_embeddings(
            vector=vector,
            embedding_type=embedding_type,
            threshold=data.get('threshold', 0.6),
            limit=data.get('limit', 10),
            session_date=data.get('session_date')
        )
        return jsonify({'success': True, 'results': results})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@persons_bp.route('/api/embeddings/<embedding_id>', methods=['DELETE'])
def delete_embedding(embedding_id):
    """Delete an embedding"""
    try:
        deleted = db.delete_embedding(embedding_id)
        if not deleted:
            return jsonify({'success': False, 'error': 'Embedding not found'}), 404
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ── Multi-Entity Detection: Tracks ───────────────────────────────

@persons_bp.route('/api/tracks/active', methods=['GET'])
def get_active_tracks():
    """Get currently active tracks (defined BEFORE /api/tracks/<track_id> to avoid route conflict)"""
    try:
        camera_id = request.args.get('camera_id')
        tracks = db.get_active_tracks(camera_id=camera_id)
        return jsonify({'success': True, 'tracks': tracks})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@persons_bp.route('/api/tracks', methods=['GET'])
def list_tracks():
    """List tracks with optional filters"""
    try:
        camera_id = request.args.get('camera_id')
        entity_type = request.args.get('entity_type')
        identity_id = request.args.get('identity_id')
        start_after = request.args.get('start_after')
        start_before = request.args.get('start_before')
        active_only = request.args.get('active_only', 'false').lower() in ('true', '1', 'yes')
        limit = int(request.args.get('limit', 100))
        offset = int(request.args.get('offset', 0))
        if start_after:
            start_after = datetime.fromisoformat(start_after)
        if start_before:
            start_before = datetime.fromisoformat(start_before)
        tracks = db.get_tracks(
            camera_id=camera_id,
            entity_type=entity_type,
            identity_id=identity_id,
            start_after=start_after,
            start_before=start_before,
            active_only=active_only,
            limit=limit,
            offset=offset
        )
        return jsonify({'success': True, 'tracks': tracks})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@persons_bp.route('/api/tracks', methods=['POST'])
def create_track():
    """Create a new track"""
    try:
        data = request.get_json()
        camera_id = data.get('camera_id')
        entity_type = data.get('entity_type')
        if not camera_id or not entity_type:
            return jsonify({'success': False, 'error': 'camera_id and entity_type are required'}), 400
        track = db.create_track(
            camera_id=camera_id,
            entity_type=entity_type,
            identity_id=data.get('identity_id'),
            identity_method=data.get('identity_method'),
            identity_confidence=data.get('identity_confidence')
        )
        return jsonify({'success': True, 'track': track})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@persons_bp.route('/api/tracks/<track_id>', methods=['GET'])
def get_track(track_id):
    """Get a single track by ID"""
    try:
        track = db.get_track(track_id)
        if not track:
            return jsonify({'success': False, 'error': 'Track not found'}), 404
        return jsonify({'success': True, 'track': track})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@persons_bp.route('/api/tracks/<track_id>/end', methods=['POST'])
def end_track(track_id):
    """End a track (set ended_at timestamp)"""
    try:
        ended = db.end_track(track_id)
        if not ended:
            return jsonify({'success': False, 'error': 'Track not found'}), 404
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@persons_bp.route('/api/tracks/<track_id>/link', methods=['POST'])
def link_track_to_identity(track_id):
    """Link a track to an identity"""
    try:
        data = request.get_json()
        identity_id = data.get('identity_id')
        method = data.get('method')
        confidence = data.get('confidence')
        if not all([identity_id, method, confidence is not None]):
            return jsonify({'success': False, 'error': 'identity_id, method, and confidence are required'}), 400
        linked = db.link_track_to_identity(track_id, identity_id, method, confidence)
        if not linked:
            return jsonify({'success': False, 'error': 'Track not found'}), 404
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@persons_bp.route('/api/tracks/<track_id>/sightings', methods=['GET'])
def get_track_sightings(track_id):
    """Get sightings for a track"""
    try:
        limit = request.args.get('limit')
        if limit is not None:
            limit = int(limit)
        sightings = db.get_track_sightings(track_id, limit=limit)
        return jsonify({'success': True, 'sightings': sightings})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ── Multi-Entity Detection: Sightings ────────────────────────────

@persons_bp.route('/api/sightings/batch', methods=['POST'])
def batch_insert_sightings():
    """Batch insert sightings"""
    try:
        data = request.get_json()
        sightings = data.get('sightings')
        if not sightings or not isinstance(sightings, list):
            return jsonify({'success': False, 'error': 'sightings array is required'}), 400
        count = db.batch_insert_sightings(sightings)
        return jsonify({'success': True, 'inserted': count})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ── Multi-Entity Detection: Violations ───────────────────────────

@persons_bp.route('/api/violations', methods=['GET'])
def list_violations():
    """List violations with optional filters"""
    try:
        status = request.args.get('status')
        camera_id = request.args.get('camera_id')
        violation_type = request.args.get('violation_type')
        limit = int(request.args.get('limit', 100))
        offset = int(request.args.get('offset', 0))
        violations = db.get_violations(
            status=status,
            camera_id=camera_id,
            violation_type=violation_type,
            limit=limit,
            offset=offset
        )
        return jsonify({'success': True, 'violations': violations})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@persons_bp.route('/api/violations', methods=['POST'])
def create_violation():
    """Create a new violation"""
    try:
        data = request.get_json()
        violation_type = data.get('violation_type')
        camera_id = data.get('camera_id')
        confidence = data.get('confidence')
        if not all([violation_type, camera_id, confidence is not None]):
            return jsonify({'success': False, 'error': 'violation_type, camera_id, and confidence are required'}), 400
        violation = db.create_violation(
            violation_type=violation_type,
            camera_id=camera_id,
            confidence=confidence,
            person_identity_id=data.get('person_identity_id'),
            vehicle_identity_id=data.get('vehicle_identity_id'),
            boat_identity_id=data.get('boat_identity_id'),
            trailer_identity_id=data.get('trailer_identity_id'),
            evidence_paths=data.get('evidence_paths')
        )
        return jsonify({'success': True, 'violation': violation})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@persons_bp.route('/api/violations/<violation_id>/review', methods=['POST'])
def review_violation(violation_id):
    """Review a violation (approve/dismiss)"""
    try:
        data = request.get_json()
        status = data.get('status')
        reviewed_by = data.get('reviewed_by')
        if not status or not reviewed_by:
            return jsonify({'success': False, 'error': 'status and reviewed_by are required'}), 400
        violation = db.review_violation(violation_id, status, reviewed_by, notes=data.get('notes'))
        if not violation:
            return jsonify({'success': False, 'error': 'Violation not found'}), 404
        return jsonify({'success': True, 'violation': violation})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ── Multi-Entity Detection: Visits ───────────────────────────────

@persons_bp.route('/api/visits', methods=['GET'])
def list_visits():
    """List visits with optional filters"""
    try:
        person_identity_id = request.args.get('person_identity_id')
        date_start = request.args.get('date_start')
        date_end = request.args.get('date_end')
        limit = int(request.args.get('limit', 100))
        offset = int(request.args.get('offset', 0))
        if date_start:
            date_start = datetime.fromisoformat(date_start)
        if date_end:
            date_end = datetime.fromisoformat(date_end)
        visits = db.get_visits(
            person_identity_id=person_identity_id,
            date_start=date_start,
            date_end=date_end,
            limit=limit,
            offset=offset
        )
        return jsonify({'success': True, 'visits': visits})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@persons_bp.route('/api/visits', methods=['POST'])
def create_visit():
    """Create a new visit"""
    try:
        data = request.get_json()
        visit = db.create_visit(
            person_identity_id=data.get('person_identity_id'),
            vehicle_identity_id=data.get('vehicle_identity_id'),
            boat_identity_id=data.get('boat_identity_id'),
            track_ids=data.get('track_ids'),
            camera_timeline=data.get('camera_timeline')
        )
        return jsonify({'success': True, 'visit': visit})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@persons_bp.route('/api/visits/<visit_id>', methods=['GET'])
def get_visit(visit_id):
    """Get a single visit by ID"""
    try:
        visit = db.get_visit(visit_id)
        if not visit:
            return jsonify({'success': False, 'error': 'Visit not found'}), 404
        return jsonify({'success': True, 'visit': visit})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@persons_bp.route('/api/visits/<visit_id>/end', methods=['POST'])
def end_visit(visit_id):
    """End a visit (set departure time)"""
    try:
        data = request.get_json(silent=True) or {}
        departure_time = data.get('departure_time')
        ended = db.end_visit(visit_id, departure_time=departure_time)
        if not ended:
            return jsonify({'success': False, 'error': 'Visit not found'}), 404
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@persons_bp.route('/api/visits/<visit_id>/violation', methods=['POST'])
def add_violation_to_visit(visit_id):
    """Add a violation to a visit"""
    try:
        data = request.get_json()
        violation_id = data.get('violation_id')
        if not violation_id:
            return jsonify({'success': False, 'error': 'violation_id is required'}), 400
        added = db.add_violation_to_visit(visit_id, violation_id)
        if not added:
            return jsonify({'success': False, 'error': 'Visit or violation not found'}), 404
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ── Multi-Entity Detection: Pipeline ─────────────────────────────

@persons_bp.route('/api/pipeline/status', methods=['GET'])
def pipeline_status():
    """Health check for the multi-entity detection pipeline"""
    try:
        return jsonify({
            'success': True,
            'services': {
                'database': 'ok',
                'detector': 'standby',
                'embedder': 'standby',
                'tracker': 'standby',
                'identifier': 'standby'
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ── Face Clustering ──────────────────────────────────────────────

@persons_bp.route('/api/face-clusters', methods=['GET'])
def get_face_clusters():
    """Get all face clusters with summaries"""
    try:
        clusters = face_clusterer.get_clusters_summary()
        return jsonify({
            'success': True,
            'clusters': clusters,
            'total': len(clusters)
        })
    except Exception as e:
        logger.error(f"Error fetching face clusters: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@persons_bp.route('/api/face-clusters/run', methods=['POST'])
def run_face_clustering():
    """Trigger face clustering job"""
    try:
        # Get optional parameters from request
        params = request.get_json() or {}
        min_cluster_size = params.get('min_cluster_size', 5)
        min_samples = params.get('min_samples', 3)

        # Create clusterer with custom parameters if provided
        if params:
            clusterer = FaceClusterer(
                min_cluster_size=min_cluster_size,
                min_samples=min_samples
            )
        else:
            clusterer = face_clusterer

        summary = clusterer.run_clustering()

        return jsonify({
            'success': True,
            'summary': summary
        })
    except Exception as e:
        logger.error(f"Error running face clustering: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@persons_bp.route('/api/face-clusters/<identity_id>/assign', methods=['POST'])
def assign_face_cluster(identity_id):
    """Assign name to a cluster. JSON body: {"name": "John Doe"}"""
    try:
        data = request.get_json()
        if not data or 'name' not in data:
            return jsonify({
                'success': False,
                'error': 'Missing required field: name'
            }), 400

        new_name = data['name'].strip()
        if not new_name:
            return jsonify({
                'success': False,
                'error': 'Name cannot be empty'
            }), 400

        success = face_clusterer.assign_cluster(identity_id, new_name)

        if success:
            return jsonify({
                'success': True,
                'identity_id': identity_id,
                'name': new_name
            })
        else:
            return jsonify({
                'success': False,
                'error': 'Identity not found or not a cluster identity'
            }), 404

    except Exception as e:
        logger.error(f"Error assigning cluster name: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@persons_bp.route('/api/face-clusters/<source_id>/merge/<target_id>', methods=['POST'])
def merge_face_clusters(source_id, target_id):
    """Merge source cluster into target"""
    try:
        if source_id == target_id:
            return jsonify({
                'success': False,
                'error': 'Cannot merge an identity into itself'
            }), 400

        success = face_clusterer.merge_clusters(source_id, target_id)

        if success:
            return jsonify({
                'success': True,
                'source_id': source_id,
                'target_id': target_id,
                'message': f'Successfully merged {source_id} into {target_id}'
            })
        else:
            return jsonify({
                'success': False,
                'error': 'One or both identities not found or not cluster identities'
            }), 404

    except Exception as e:
        logger.error(f"Error merging clusters: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ── Person Recognition ───────────────────────────────────────────

@persons_bp.route('/api/person-recognition/build-gallery', methods=['POST'])
def build_person_gallery():
    """Build/rebuild reference gallery from tagged person identities."""
    try:
        recognizer = get_recognizer()
        result = recognizer.build_reference_gallery()
        return jsonify({"success": True, **result})
    except Exception as e:
        logger.error(f"Failed to build gallery: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@persons_bp.route('/api/person-recognition/gallery-stats', methods=['GET'])
def person_gallery_stats():
    """Return gallery statistics."""
    try:
        recognizer = get_recognizer()
        stats = recognizer.gallery_stats()
        return jsonify({"success": True, **stats})
    except Exception as e:
        logger.error(f"Failed to get gallery stats: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@persons_bp.route('/api/person-recognition/recognize', methods=['POST'])
def recognize_person_in_video():
    """Run recognition on a specific video's thumbnail."""
    data = request.get_json()
    if not data or 'video_id' not in data:
        return jsonify({"success": False, "error": "video_id required"}), 400

    video_id = data['video_id']

    try:
        # Get video thumbnail
        video = db.get_video(video_id)
        if not video:
            return jsonify({"success": False, "error": "Video not found"}), 404

        thumbnail_path = video.get('thumbnail_path', '')
        if not thumbnail_path or not os.path.exists(thumbnail_path):
            # Try fallback path
            basename = os.path.basename(thumbnail_path) if thumbnail_path else ''
            thumbnail_path = f"/opt/groundtruth-studio/thumbnails/{basename}"
            if not os.path.exists(thumbnail_path):
                return jsonify({"success": False, "error": "Thumbnail not found"}), 404

        recognizer = get_recognizer()

        # Get existing face detections for this video
        predictions = db.get_predictions_for_video(video_id)
        face_dets = [
            {'x': p['bbox_x'], 'y': p['bbox_y'], 'width': p['bbox_width'], 'height': p['bbox_height']}
            for p in predictions
            if p.get('scenario') in ('face_detection',) and p.get('bbox_x') is not None
        ]

        if not face_dets:
            # No existing face detections - send full image to InsightFace
            result = recognizer._get_embedding(thumbnail_path)
            if result and result.get('face_detected'):
                bbox = result['bbox']
                face_dets = [{'x': int(bbox[0]), 'y': int(bbox[1]),
                              'width': int(bbox[2] - bbox[0]), 'height': int(bbox[3] - bbox[1])}]
            else:
                return jsonify({"success": True, "predictions_created": 0, "matches": [],
                                "message": "No faces detected"})

        id_predictions = recognizer.recognize_faces_in_thumbnail(thumbnail_path, face_dets)

        # Submit predictions to the review queue
        created = 0
        matches = []
        if id_predictions:
            pred_ids = db.insert_predictions_batch(
                video_id, 'person-recognition', '1.0',
                f"recognition-{int(time.time())}", id_predictions
            )
            created = len(pred_ids)

            matches = [
                {"person_name": p['tags']['person_name'], "confidence": p['confidence']}
                for p in id_predictions
            ]

        return jsonify({
            "success": True,
            "predictions_created": created,
            "matches": matches
        })
    except Exception as e:
        logger.error(f"Recognition failed for video {video_id}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@persons_bp.route('/api/person-recognition/backfill', methods=['POST'])
def backfill_person_recognition():
    """Process all existing thumbnails for person recognition in background."""

    def _backfill():
        recognizer = get_recognizer()
        gallery = recognizer.get_reference_gallery()
        if not gallery:
            logger.warning("Backfill skipped: no reference gallery")
            return

        with get_cursor(commit=False) as cur:
            cur.execute("""
                SELECT v.id, v.thumbnail_path FROM videos v
                WHERE v.thumbnail_path IS NOT NULL
                AND NOT EXISTS (
                    SELECT 1 FROM ai_predictions ap
                    WHERE ap.video_id = v.id AND ap.scenario = 'person_identification'
                )
                ORDER BY v.id
            """)
            videos = cur.fetchall()

        logger.info(f"Backfill: processing {len(videos)} videos")
        processed = 0
        identified = 0

        for video in videos:
            vid_id = video['id']
            thumb = video['thumbnail_path']
            if not os.path.exists(thumb):
                basename = os.path.basename(thumb)
                thumb = f"/opt/groundtruth-studio/thumbnails/{basename}"
                if not os.path.exists(thumb):
                    continue

            try:
                # Get face detections
                preds = db.get_predictions_for_video(vid_id)
                face_dets = [
                    {'x': p['bbox_x'], 'y': p['bbox_y'],
                     'width': p['bbox_width'], 'height': p['bbox_height']}
                    for p in preds
                    if p.get('scenario') in ('face_detection',) and p.get('bbox_x') is not None
                ]

                if not face_dets:
                    result = recognizer._get_embedding(thumb)
                    if result and result.get('face_detected'):
                        bbox = result['bbox']
                        face_dets = [{'x': int(bbox[0]), 'y': int(bbox[1]),
                                      'width': int(bbox[2] - bbox[0]), 'height': int(bbox[3] - bbox[1])}]

                if face_dets:
                    id_preds = recognizer.recognize_faces_in_thumbnail(thumb, face_dets)
                    if id_preds:
                        db.insert_predictions_batch(
                            vid_id, 'person-recognition', '1.0',
                            f"backfill-{int(time.time())}", id_preds
                        )
                        identified += len(id_preds)

                processed += 1
            except Exception as e:
                logger.error(f"Backfill error for video {vid_id}: {e}")

        logger.info(f"Backfill complete: {processed} videos, {identified} identifications")

    thread = threading.Thread(target=_backfill, daemon=True, name="person-recognition-backfill")
    thread.start()

    return jsonify({
        "success": True,
        "message": "Backfill started in background"
    })
