"""Camera Map — interactive map view for camera placement and FOV planning."""
from flask import Blueprint, request, jsonify, render_template
from db_connection import get_connection
from psycopg2 import extras
import logging

camera_map_bp = Blueprint('camera_map', __name__)
logger = logging.getLogger(__name__)


@camera_map_bp.route('/camera-map')
def camera_map_page():
    """Render the camera map view."""
    return render_template('camera_map.html')


@camera_map_bp.route('/api/camera-map/placements', methods=['GET'])
def get_placements():
    """Get all camera placements that have coordinates."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
            cursor.execute('''
                SELECT id, camera_id, camera_name, location_name, latitude, longitude,
                       bearing, fov_angle, fov_range, map_color, is_ptz, ptz_pan_range, location_description, is_indoor,
                       onvif_host, onvif_port, onvif_username, onvif_password, ptz_home_bearing, ptz_travel_limits
                FROM camera_locations
                WHERE latitude IS NOT NULL AND longitude IS NOT NULL
                ORDER BY camera_name, camera_id
            ''')
            placements = [dict(row) for row in cursor.fetchall()]
        return jsonify({'success': True, 'placements': placements})
    except Exception as e:
        logger.error(f"Error fetching placements: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@camera_map_bp.route('/api/camera-map/preview/<camera_id>', methods=['GET'])
def get_camera_preview(camera_id):
    """Get the most recent thumbnail for a camera."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
            cursor.execute('''
                SELECT thumbnail_path, upload_date
                FROM videos
                WHERE (camera_id = %s OR camera_id IN (
                    SELECT alias_id FROM camera_aliases WHERE primary_camera_id = %s
                ) OR camera_id IN (
                    SELECT primary_camera_id FROM camera_aliases WHERE alias_id = %s
                ))
                AND thumbnail_path IS NOT NULL
                ORDER BY upload_date DESC
                LIMIT 1
            ''', (camera_id, camera_id, camera_id))
            row = cursor.fetchone()
        if row and row['thumbnail_path']:
            import os
            filename = os.path.basename(row['thumbnail_path'])
            return jsonify({
                'success': True,
                'preview_url': f'/thumbnails/{filename}',
                'captured_at': str(row['upload_date'])
            })
        return jsonify({'success': True, 'preview_url': None})
    except Exception as e:
        logger.error(f"Error fetching preview: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@camera_map_bp.route('/api/camera-map/unplaced', methods=['GET'])
def get_unplaced_cameras():
    """Get cameras that exist in the system but don't have map coordinates yet."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
            # Cameras from camera_locations without coordinates
            cursor.execute('''
                WITH placed AS (
                    SELECT camera_id FROM camera_locations
                    WHERE latitude IS NOT NULL AND longitude IS NOT NULL
                ),
                all_camera_ids AS (
                    SELECT camera_id FROM camera_locations
                    UNION
                    SELECT DISTINCT camera_id FROM videos
                    WHERE camera_id IS NOT NULL AND camera_id != ''
                    UNION
                    SELECT DISTINCT camera_id FROM ecoeye_alerts
                    WHERE camera_id IS NOT NULL AND camera_id != ''
                ),
                resolved AS (
                    SELECT COALESCE(ca.primary_camera_id, a.camera_id) AS camera_id
                    FROM all_camera_ids a
                    LEFT JOIN camera_aliases ca ON a.camera_id = ca.alias_id
                )
                SELECT DISTINCT r.camera_id,
                       cl.camera_name,
                       cl.location_name
                FROM resolved r
                LEFT JOIN camera_locations cl ON r.camera_id = cl.camera_id
                WHERE r.camera_id NOT IN (SELECT camera_id FROM placed)
                ORDER BY r.camera_id
            ''')
            cameras = [dict(row) for row in cursor.fetchall()]
        return jsonify({'success': True, 'cameras': cameras})
    except Exception as e:
        logger.error(f"Error fetching unplaced cameras: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@camera_map_bp.route('/api/camera-map/placements', methods=['POST'])
def create_placement():
    """Create a new camera placement (manual or from imported camera)."""
    try:
        data = request.json
        camera_id = data.get('camera_id', '').strip()
        if not camera_id:
            return jsonify({'success': False, 'error': 'camera_id is required'}), 400

        lat = data.get('latitude')
        lng = data.get('longitude')
        if lat is None or lng is None:
            return jsonify({'success': False, 'error': 'latitude and longitude are required'}), 400

        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
            cursor.execute('''
                INSERT INTO camera_locations
                    (camera_id, camera_name, location_name, latitude, longitude,
                     bearing, fov_angle, fov_range, map_color, is_ptz, ptz_pan_range, is_indoor,
                     onvif_host, onvif_port, onvif_username, onvif_password)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (camera_id) DO UPDATE SET
                    camera_name = COALESCE(EXCLUDED.camera_name, camera_locations.camera_name),
                    latitude = EXCLUDED.latitude,
                    longitude = EXCLUDED.longitude,
                    bearing = EXCLUDED.bearing,
                    fov_angle = EXCLUDED.fov_angle,
                    fov_range = EXCLUDED.fov_range,
                    map_color = EXCLUDED.map_color,
                    is_ptz = EXCLUDED.is_ptz,
                    ptz_pan_range = EXCLUDED.ptz_pan_range,
                    is_indoor = EXCLUDED.is_indoor,
                    onvif_host = EXCLUDED.onvif_host,
                    onvif_port = EXCLUDED.onvif_port,
                    onvif_username = EXCLUDED.onvif_username,
                    onvif_password = EXCLUDED.onvif_password,
                    updated_date = CURRENT_TIMESTAMP
                RETURNING *
            ''', (
                camera_id,
                data.get('camera_name', camera_id),
                data.get('location_name', 'Map Placement'),
                lat, lng,
                data.get('bearing', 0),
                data.get('fov_angle', 90),
                data.get('fov_range', 30),
                data.get('map_color', '#4CAF50'),
                data.get('is_ptz', False),
                data.get('ptz_pan_range', 180),
                data.get('is_indoor', False),
                data.get('onvif_host'),
                data.get('onvif_port', 80),
                data.get('onvif_username'),
                data.get('onvif_password'),
            ))
            placement = dict(cursor.fetchone())
            conn.commit()
        return jsonify({'success': True, 'placement': placement})
    except Exception as e:
        logger.error(f"Error creating placement: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@camera_map_bp.route('/api/camera-map/placements/<int:placement_id>', methods=['PUT'])
def update_placement(placement_id):
    """Update a camera placement's map properties."""
    try:
        data = request.json
        fields = []
        values = []

        allowed_fields = {
            'latitude': 'latitude', 'longitude': 'longitude',
            'bearing': 'bearing', 'fov_angle': 'fov_angle',
            'fov_range': 'fov_range', 'map_color': 'map_color',
            'camera_name': 'camera_name', 'location_name': 'location_name',
            'location_description': 'location_description',
            'is_ptz': 'is_ptz', 'ptz_pan_range': 'ptz_pan_range',
            'is_indoor': 'is_indoor',
            'onvif_host': 'onvif_host',
            'onvif_port': 'onvif_port',
            'onvif_username': 'onvif_username',
            'onvif_password': 'onvif_password',
        }

        for key, col in allowed_fields.items():
            if key in data:
                fields.append(f"{col} = %s")
                values.append(data[key])

        if not fields:
            return jsonify({'success': False, 'error': 'No fields to update'}), 400

        fields.append("updated_date = CURRENT_TIMESTAMP")
        values.append(placement_id)

        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
            cursor.execute(f'''
                UPDATE camera_locations
                SET {', '.join(fields)}
                WHERE id = %s
                RETURNING *
            ''', values)
            result = cursor.fetchone()
            conn.commit()

        if not result:
            return jsonify({'success': False, 'error': 'Placement not found'}), 404
        return jsonify({'success': True, 'placement': dict(result)})
    except Exception as e:
        logger.error(f"Error updating placement: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@camera_map_bp.route('/api/camera-map/placements/<int:placement_id>', methods=['DELETE'])
def delete_placement(placement_id):
    """Remove a camera from the map by clearing its coordinates (soft-delete)."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
            cursor.execute('''
                UPDATE camera_locations
                SET latitude = NULL, longitude = NULL, bearing = 0,
                    fov_angle = 90, fov_range = 30, map_color = '#4CAF50',
                    updated_date = CURRENT_TIMESTAMP
                WHERE id = %s
            ''', (placement_id,))
            success = cursor.rowcount > 0
            conn.commit()
        return jsonify({'success': success})
    except Exception as e:
        logger.error(f"Error deleting placement: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@camera_map_bp.route('/api/camera-map/aliases', methods=['GET'])
def get_aliases():
    """Get all camera aliases."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
            cursor.execute('SELECT * FROM camera_aliases ORDER BY primary_camera_id, alias_id')
            aliases = [dict(row) for row in cursor.fetchall()]
        return jsonify({'success': True, 'aliases': aliases})
    except Exception as e:
        logger.error(f"Error fetching aliases: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@camera_map_bp.route('/api/camera-map/aliases', methods=['POST'])
def create_alias():
    """Create a camera alias mapping."""
    try:
        data = request.json
        alias_id = data.get('alias_id', '').strip()
        primary_camera_id = data.get('primary_camera_id', '').strip()
        if not alias_id or not primary_camera_id:
            return jsonify({'success': False, 'error': 'alias_id and primary_camera_id required'}), 400
        if alias_id == primary_camera_id:
            return jsonify({'success': False, 'error': 'alias_id cannot be same as primary_camera_id'}), 400

        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
            cursor.execute('''
                INSERT INTO camera_aliases (alias_id, primary_camera_id, alias_type)
                VALUES (%s, %s, %s)
                ON CONFLICT (alias_id) DO UPDATE SET
                    primary_camera_id = EXCLUDED.primary_camera_id,
                    alias_type = EXCLUDED.alias_type
                RETURNING *
            ''', (alias_id, primary_camera_id, data.get('alias_type', 'unifi')))
            alias = dict(cursor.fetchone())
            conn.commit()
        return jsonify({'success': True, 'alias': alias})
    except Exception as e:
        logger.error(f"Error creating alias: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@camera_map_bp.route('/api/camera-map/aliases/<int:alias_id>', methods=['DELETE'])
def delete_alias(alias_id):
    """Remove a camera alias."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
            cursor.execute('DELETE FROM camera_aliases WHERE id = %s', (alias_id,))
            success = cursor.rowcount > 0
            conn.commit()
        return jsonify({'success': success})
    except Exception as e:
        logger.error(f"Error deleting alias: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
