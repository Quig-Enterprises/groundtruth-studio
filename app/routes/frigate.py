from flask import Blueprint, request, jsonify
from frigate_ingester import get_ingester, start_background_ingester, stop_background_ingester
from db_connection import get_cursor
import logging
import requests as http_requests

frigate_bp = Blueprint('frigate', __name__)
logger = logging.getLogger(__name__)

# ==================== Frigate Ingester Endpoints ====================

@frigate_bp.route('/api/frigate/start', methods=['POST'])
def frigate_start():
    """Start the Frigate background ingester."""
    role = request.headers.get('X-Auth-Role', '')
    if role != 'admin':
        return jsonify({'success': False, 'error': 'Admin access required'}), 403

    try:
        data = request.get_json() or {}
        interval = data.get('interval', 60)

        start_background_ingester(interval)

        return jsonify({
            'success': True,
            'message': 'Frigate ingester started',
            'interval': interval
        })
    except Exception as e:
        logger.error(f"Failed to start Frigate ingester: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@frigate_bp.route('/api/frigate/stop', methods=['POST'])
def frigate_stop():
    """Stop the Frigate background ingester."""
    role = request.headers.get('X-Auth-Role', '')
    if role != 'admin':
        return jsonify({'success': False, 'error': 'Admin access required'}), 403

    try:
        stop_background_ingester()

        return jsonify({
            'success': True,
            'message': 'Frigate ingester stopped'
        })
    except Exception as e:
        logger.error(f"Failed to stop Frigate ingester: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@frigate_bp.route('/api/frigate/status', methods=['GET'])
def frigate_status():
    """Get the status of the Frigate ingester."""
    role = request.headers.get('X-Auth-Role', '')
    if role != 'admin':
        return jsonify({'success': False, 'error': 'Admin access required'}), 403

    try:
        ingester = get_ingester()

        try:
            cameras = ingester.get_cameras()
        except Exception as e:
            logger.warning(f"Failed to get cameras (Frigate may not be reachable): {e}")
            cameras = []

        return jsonify({
            'success': True,
            'running': not ingester._stop_flag.is_set(),
            'interval': ingester.interval,
            'cameras': cameras
        })
    except Exception as e:
        logger.error(f"Failed to get Frigate ingester status: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@frigate_bp.route('/api/frigate/capture', methods=['POST'])
def frigate_capture():
    """Run a single Frigate capture cycle."""
    role = request.headers.get('X-Auth-Role', '')
    if role != 'admin':
        return jsonify({'success': False, 'error': 'Admin access required'}), 403

    try:
        ingester = get_ingester()
        cycle_result = ingester.run_cycle()

        return jsonify({
            'success': True,
            **cycle_result
        })
    except Exception as e:
        logger.error(f"Failed to run Frigate capture cycle: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@frigate_bp.route('/api/frigate/backfill-metadata', methods=['POST'])
def frigate_backfill_metadata():
    """Backfill path_data and other metadata for existing Frigate events.

    Re-queries the Frigate API for each stored event to retrieve path_data,
    start_time, end_time, has_clip, and other fields that weren't originally stored.
    Only works for events still within Frigate's retention window (2-10 days).
    """
    role = request.headers.get('X-Auth-Role', '')
    if role != 'admin':
        return jsonify({'success': False, 'error': 'Admin access required'}), 403

    data = request.get_json() or {}
    camera_filter = data.get('camera_id')  # Optional: only backfill specific camera
    frigate_url = data.get('frigate_url', 'http://localhost:5000')
    dry_run = data.get('dry_run', False)

    try:
        # Find all videos with frigate_event_id but missing path_data
        with get_cursor(commit=False) as cursor:
            query = """
                SELECT id, camera_id, metadata
                FROM videos
                WHERE metadata->>'frigate_event_id' IS NOT NULL
                  AND (metadata->>'path_data' IS NULL)
            """
            params = []
            if camera_filter:
                query += " AND camera_id = %s"
                params.append(camera_filter)
            query += " ORDER BY id DESC"

            cursor.execute(query, params)
            videos = [dict(row) for row in cursor.fetchall()]

        total = len(videos)
        updated = 0
        failed = 0
        skipped = 0
        results = []

        for video in videos:
            event_id = video['metadata'].get('frigate_event_id')
            if not event_id:
                skipped += 1
                continue

            try:
                # Query Frigate API for full event data
                resp = http_requests.get(
                    f"{frigate_url}/api/events/{event_id}",
                    timeout=10
                )
                if resp.status_code == 404:
                    # Event expired from Frigate retention
                    skipped += 1
                    results.append({
                        'video_id': video['id'],
                        'status': 'expired',
                        'event_id': event_id[:12]
                    })
                    continue

                resp.raise_for_status()
                evt = resp.json()
                evt_data = evt.get('data', {})

                # Build updated metadata by merging new fields into existing
                existing_meta = video['metadata'] or {}
                new_fields = {
                    'start_time': evt.get('start_time'),
                    'end_time': evt.get('end_time'),
                    'path_data': evt_data.get('path_data'),
                    'frigate_box': evt_data.get('box'),
                    'frigate_region': evt_data.get('region'),
                    'frigate_top_score': evt_data.get('top_score'),
                    'average_estimated_speed': evt_data.get('average_estimated_speed'),
                    'velocity_angle': evt_data.get('velocity_angle'),
                    'has_clip': evt.get('has_clip', False),
                    'sub_label': evt.get('sub_label'),
                    'zones': evt.get('zones', []),
                    'entered_zones': evt.get('entered_zones', []),
                }
                # Strip None values
                new_fields = {k: v for k, v in new_fields.items() if v is not None}
                merged = {**existing_meta, **new_fields}

                path_points = len(evt_data.get('path_data', []) or [])

                if not dry_run:
                    with get_cursor() as cur:
                        from psycopg2.extras import Json
                        cur.execute(
                            "UPDATE videos SET metadata = %s WHERE id = %s",
                            (Json(merged), video['id'])
                        )

                updated += 1
                results.append({
                    'video_id': video['id'],
                    'camera': video['camera_id'],
                    'status': 'updated' if not dry_run else 'would_update',
                    'path_points': path_points,
                    'event_id': event_id[:12]
                })

            except Exception as e:
                failed += 1
                results.append({
                    'video_id': video['id'],
                    'status': 'error',
                    'error': str(e),
                    'event_id': event_id[:12]
                })

        return jsonify({
            'success': True,
            'total_candidates': total,
            'updated': updated,
            'skipped': skipped,
            'failed': failed,
            'dry_run': dry_run,
            'details': results[:50]  # Limit detail output
        })

    except Exception as e:
        logger.error(f"Backfill metadata error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500
