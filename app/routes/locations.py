from flask import Blueprint, request, jsonify, render_template, g
from db_connection import get_connection
from psycopg2 import extras
import services
from services import db, location_exporter, training_queue, THUMBNAIL_DIR, DOWNLOAD_DIR
import os
import json
import logging
import threading

locations_bp = Blueprint('locations', __name__)
logger = logging.getLogger(__name__)


# ===== Camera Location Endpoints =====

@locations_bp.route('/api/camera-locations', methods=['GET'])
def get_camera_locations():
    """List all camera-location mappings"""
    try:
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
            cursor.execute('''
                SELECT cl.*,
                       (SELECT COUNT(*) FROM videos v WHERE v.camera_id = cl.camera_id) as frame_count
                FROM camera_locations cl
                ORDER BY cl.location_name
            ''')
            locations = [dict(row) for row in cursor.fetchall()]
        return jsonify({'success': True, 'locations': locations})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@locations_bp.route('/api/camera-locations', methods=['POST'])
def create_or_update_camera_location():
    """Create or update a camera-location mapping"""
    try:
        data = request.json
        camera_id = data.get('camera_id', '').strip()
        location_name = data.get('location_name', '').strip()

        if not camera_id or not location_name:
            return jsonify({'success': False, 'error': 'camera_id and location_name required'}), 400

        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
            cursor.execute('''
                INSERT INTO camera_locations (camera_id, camera_name, location_name, location_description, site_name, latitude, longitude)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (camera_id) DO UPDATE SET
                    camera_name = EXCLUDED.camera_name,
                    location_name = EXCLUDED.location_name,
                    location_description = EXCLUDED.location_description,
                    site_name = EXCLUDED.site_name,
                    latitude = EXCLUDED.latitude,
                    longitude = EXCLUDED.longitude,
                    updated_date = CURRENT_TIMESTAMP
                RETURNING id
            ''', (
                camera_id,
                data.get('camera_name'),
                location_name,
                data.get('location_description'),
                data.get('site_name'),
                data.get('latitude'),
                data.get('longitude')
            ))
            result = cursor.fetchone()
            conn.commit()

        return jsonify({'success': True, 'id': result['id']})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@locations_bp.route('/api/camera-locations/<int:location_id>', methods=['DELETE'])
def delete_camera_location(location_id):
    """Remove a camera-location mapping"""
    try:
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
            cursor.execute('DELETE FROM camera_locations WHERE id = %s', (location_id,))
            success = cursor.rowcount > 0
            conn.commit()
        return jsonify({'success': success})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@locations_bp.route('/api/camera-locations/lookup/<camera_id>', methods=['GET'])
def lookup_camera_location(camera_id):
    """Lookup location by camera MAC address"""
    try:
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
            cursor.execute('SELECT * FROM camera_locations WHERE camera_id = %s', (camera_id,))
            row = cursor.fetchone()
        if row:
            return jsonify({'success': True, 'location': dict(row)})
        return jsonify({'success': True, 'location': None})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@locations_bp.route('/api/camera-locations/cameras', methods=['GET'])
def get_discovered_cameras():
    """List unique camera_ids from ecoeye_alerts + videos with event counts"""
    try:
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
            cursor.execute('''
                WITH camera_sources AS (
                    -- From ecoeye_alerts
                    SELECT camera_id, MAX(timestamp) as last_seen, COUNT(*) as event_count,
                           NULL as camera_name
                    FROM ecoeye_alerts
                    WHERE camera_id IS NOT NULL AND camera_id != ''
                    GROUP BY camera_id

                    UNION ALL

                    -- From videos
                    SELECT camera_id, MAX(upload_date) as last_seen, COUNT(*) as event_count,
                           NULL as camera_name
                    FROM videos
                    WHERE camera_id IS NOT NULL AND camera_id != ''
                    GROUP BY camera_id
                ),
                aggregated AS (
                    SELECT camera_id,
                           MAX(last_seen) as last_seen,
                           SUM(event_count) as total_events
                    FROM camera_sources
                    GROUP BY camera_id
                )
                SELECT a.camera_id, a.last_seen, a.total_events,
                       cl.id as location_id, cl.location_name, cl.camera_name, cl.site_name
                FROM aggregated a
                LEFT JOIN camera_locations cl ON a.camera_id = cl.camera_id
                ORDER BY a.total_events DESC
            ''')
            cameras = [dict(row) for row in cursor.fetchall()]
        return jsonify({'success': True, 'cameras': cameras})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@locations_bp.route('/api/camera-locations/<int:location_id>/reference-image', methods=['POST'])
def set_reference_image(location_id):
    """Set reference image for a camera location from an existing thumbnail"""
    try:
        data = request.json
        image_path = data.get('image_path', '').strip()

        if not image_path:
            return jsonify({'success': False, 'error': 'image_path required'}), 400

        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
            cursor.execute('''
                UPDATE camera_locations SET reference_image_path = %s, updated_date = CURRENT_TIMESTAMP
                WHERE id = %s
            ''', (image_path, location_id))
            success = cursor.rowcount > 0
            conn.commit()

        return jsonify({'success': success})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ===== Location Export Endpoints =====

@locations_bp.route('/api/location-export/stats', methods=['GET'])
def get_location_export_stats():
    """Get available training data statistics for location classification"""
    try:
        stats = location_exporter.get_export_stats()
        return jsonify({'success': True, 'stats': stats})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@locations_bp.route('/api/location-export/export', methods=['POST'])
def export_location_dataset():
    """Export location classification training dataset"""
    try:
        data = request.json or {}
        result = location_exporter.export_dataset(
            output_dir=data.get('output_dir'),
            format=data.get('format', 'imagefolder'),
            val_split=data.get('val_split', 0.2),
            seed=data.get('seed', 42)
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@locations_bp.route('/location-export')
def location_export_page():
    return render_template('location_export.html')


# ==================== Location Export & Train Endpoint ====================

@locations_bp.route('/api/location-export/export-and-train', methods=['POST'])
def location_export_and_train():
    """One-click: export location dataset then submit training job"""
    try:
        data = request.get_json() or {}

        # Step 1: Export
        export_result = location_exporter.export_dataset(
            output_dir=data.get('output_dir'),
            format=data.get('format', 'imagefolder'),
            val_split=data.get('val_split', 0.2),
            seed=data.get('seed', 42)
        )

        if not export_result.get('success') or not export_result.get('export_path'):
            return jsonify({'success': False, 'error': 'Export produced no data',
                            'export_result': export_result}), 400

        export_path = export_result['export_path']

        # Step 2: Submit training job
        config = {
            'model_type': data.get('model_type', 'resnet18'),
            'epochs': data.get('epochs', 50),
            'labels': ','.join(export_result.get('location_counts', {}).keys()),
        }

        job = training_queue.submit_job(
            job_type='location',
            export_path=export_path,
            config=config
        )

        return jsonify({
            'success': True,
            'job': job,
            'export_result': export_result,
            'message': f'Exported {export_result.get("total_frames", 0)} frames across {export_result.get("locations", 0)} locations and submitted training job'
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
