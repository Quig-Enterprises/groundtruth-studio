from flask import Blueprint, request, jsonify, render_template, g
from db_connection import get_connection, get_cursor
from psycopg2 import extras
import services
from services import (
    db, downloader, processor, download_queue, ecoeye_request,
    sync_config, DOWNLOAD_DIR, THUMBNAIL_DIR,
    ECOEYE_API_BASE, ECOEYE_PHP_API_KEY, ECOEYE_API_KEY, ECOEYE_API_SECRET,
)
from ecoeye_sync import EcoEyeSyncClient
from vehicle_detect_runner import trigger_vehicle_detect
import os
import json
import logging

ecoeye_bp = Blueprint('ecoeye', __name__)
logger = logging.getLogger(__name__)

# Module-level client cache (initialized on-demand)
ecoeye_client = None


@ecoeye_bp.route('/ecoeye-preview')
def ecoeye_preview():
    return render_template('ecoeye_preview.html')


@ecoeye_bp.route('/api/ecoeye/events', methods=['GET'])
def get_ecoeye_events():
    """Fetch events with thumbnails from EcoEye Alert Relay, enriched with local status"""
    import requests

    # Build query params
    params = {
        'limit': request.args.get('limit', 50),
        'offset': request.args.get('offset', 0),
    }

    # Only add status filter if explicitly provided (don't default to completed)
    if request.args.get('status'):
        params['status'] = request.args.get('status')
    if request.args.get('camera'):
        params['camera'] = request.args.get('camera')
    if request.args.get('event_type'):
        params['event_type'] = request.args.get('event_type')
    if request.args.get('since'):
        params['since'] = request.args.get('since')
    if request.args.get('site'):
        params['site'] = request.args.get('site')
    if request.args.get('sort'):
        params['sort'] = request.args.get('sort')
    if request.args.get('has_thumbnail'):
        params['has_thumbnail'] = request.args.get('has_thumbnail')
    if request.args.get('tag_id'):
        params['tag_id'] = request.args.get('tag_id')
    if request.args.get('untagged'):
        params['untagged'] = request.args.get('untagged')

    try:
        resp = ecoeye_request('GET', 'api-thumbnails.php',
            params=params,
            timeout=30
        )
        result = resp.json()

        if result.get('success') and result.get('events'):
            # Cross-reference with local database to find imported events
            # Look for videos with ecoeye:// URLs in original_url
            with get_connection() as conn:
                cursor = conn.cursor(cursor_factory=extras.RealDictCursor)

                # Get all ecoeye event IDs that have been imported locally
                cursor.execute('''
                    SELECT
                        REPLACE(original_url, 'ecoeye://', '') as event_id,
                        id as local_id,
                        filename,
                        thumbnail_path,
                        title
                    FROM videos
                    WHERE original_url LIKE 'ecoeye://%%'
                ''')

                local_imports = {}
                for row in cursor.fetchall():
                    event_id = row['event_id']
                    filename = row['filename']
                    # Check if it's just metadata or has actual video
                    has_local_video = not filename.endswith('.placeholder')
                    local_imports[event_id] = {
                        'local_id': row['local_id'],
                        'filename': filename,
                        'thumbnail_path': row['thumbnail_path'],
                        'has_local_video': has_local_video,
                        'title': row['title']
                    }

            # Enrich events with local status
            for event in result['events']:
                event_id = event.get('event_id')
                if event_id and event_id in local_imports:
                    local_info = local_imports[event_id]
                    event['imported_to_studio'] = True
                    event['local_video_id'] = local_info['local_id']
                    event['has_local_video'] = local_info['has_local_video']
                    if local_info['has_local_video']:
                        event['local_video_path'] = f"/downloads/{local_info['filename']}"
                    if local_info['thumbnail_path']:
                        # Use local thumbnail if available
                        thumb_name = os.path.basename(local_info['thumbnail_path'])
                        event['local_thumbnail'] = f"/thumbnails/{thumb_name}"
                else:
                    event['imported_to_studio'] = False
                    event['local_video_id'] = None
                    event['has_local_video'] = False

            # Filter out test alerts (FAKE_MAC)
            result['events'] = [e for e in result['events'] if e.get('camera_id') != 'FAKE_MAC']

            print(f"[EcoEye Events] Returned {len(result['events'])} events, {len(local_imports)} imported locally")

        return jsonify(result)
    except requests.exceptions.Timeout:
        print(f"[EcoEye Events] Timeout connecting to alert relay")
        return jsonify({'success': False, 'error': 'EcoEye alert relay timed out — try again', 'events': [], 'total': 0}), 200
    except requests.exceptions.ConnectionError as e:
        print(f"[EcoEye Events] Connection error: {e}")
        return jsonify({'success': False, 'error': 'Cannot reach EcoEye alert relay', 'events': [], 'total': 0}), 200
    except Exception as e:
        print(f"[EcoEye Events] Error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@ecoeye_bp.route('/api/ecoeye/sync-sample', methods=['POST'])
def sync_ecoeye_sample():
    """Import a single event from EcoEye to Groundtruth Studio"""
    import requests

    data = request.json
    event_id = data.get('event_id')
    include_video = data.get('include_video', True)  # Default to including video

    if not event_id:
        return jsonify({'success': False, 'error': 'event_id required'}), 400

    # Check if already imported
    with get_connection() as conn:
        cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
        cursor.execute("SELECT id FROM videos WHERE original_url LIKE %s", (f"%{event_id}%",))
        existing = cursor.fetchone()
        if existing:
            print(f"[EcoEye Sync] Event already imported: {event_id} -> record {existing['id']}")
            return jsonify({
                'success': True,
                'record_id': existing['id'],
                'message': 'Event already imported',
                'already_imported': True
            })

    print(f"[EcoEye Sync] Looking for event: {event_id}")

    try:
        # Direct lookup by event_id (requires updated api-thumbnails.php)
        resp = ecoeye_request('GET', 'api-thumbnails.php',
            params={'event_id': event_id},
            timeout=30
        )
        result = resp.json()

        event = None
        events = result.get('events', [])

        if events:
            event = events[0]
            print(f"[EcoEye Sync] Found event via direct lookup")
        else:
            # Fallback: Search through recent events if direct lookup not supported
            print(f"[EcoEye Sync] Direct lookup returned no events, trying search...")
            resp = ecoeye_request('GET', 'api-thumbnails.php',
                params={'limit': 2000},
                timeout=60
            )
            result = resp.json()

            for e in result.get('events', []):
                if e.get('event_id') == event_id:
                    event = e
                    print(f"[EcoEye Sync] Found event via search")
                    break

        if not event:
            print(f"[EcoEye Sync] Event not found: {event_id}")
            return jsonify({'success': False, 'error': f'Event not found: {event_id}'}), 404

        # Log event details to console for debugging
        print(f"\n[EcoEye Sync] Event: {event_id}")
        print(f"  Camera: {event.get('camera_name', 'Unknown')}")
        print(f"  Type: {event.get('event_type', 'unknown')}")
        print(f"  Alarm: {event.get('alarm_name', 'N/A')}")
        print(f"  Site: {event.get('site_name', 'N/A')}")
        print(f"  Time: {event.get('timestamp', 'unknown')}")
        print(f"  Status: {event.get('status', 'unknown')}")
        print(f"  Video Path: {event.get('video_path', 'None')}")
        print(f"  Has Thumbnail: {bool(event.get('thumbnail'))}")
        print(f"  Include Video: {include_video}")

        video_path = event.get('video_path')
        has_video = bool(video_path)

        # Build notes with all metadata
        notes = f"EcoEye event: {event_id}\n"
        notes += f"Camera: {event.get('camera_name', 'Unknown')}\n"
        notes += f"Type: {event.get('event_type', 'unknown')}\n"
        notes += f"Time: {event.get('timestamp', 'unknown')}\n"
        if event.get('alarm_name'):
            notes += f"Alarm: {event.get('alarm_name')}\n"
        if event.get('site_name'):
            notes += f"Site: {event.get('site_name')}\n"
        notes += f"Status: {event.get('status', 'unknown')}\n"

        if include_video and has_video:
            # Download video
            video_filename = video_path.split('/videos/')[-1] if '/videos/' in video_path else video_path
            video_url = f'{ECOEYE_API_BASE}/videos/{video_filename}'

            dl_result = downloader.download_video(video_url)

            if dl_result.get('success'):
                # Check if a video with this filename already exists
                with get_connection() as conn:
                    dup_cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
                    dup_cursor.execute("SELECT id FROM videos WHERE filename = %s", (dl_result['filename'],))
                    dup_existing = dup_cursor.fetchone()

                if dup_existing:
                    print(f"[EcoEye Sync] Video file already in DB: {dl_result['filename']} -> record {dup_existing['id']}")
                    return jsonify({
                        'success': True,
                        'record_id': dup_existing['id'],
                        'message': 'Video already imported (matched by filename)',
                        'already_imported': True
                    })

                video_id = db.add_video(
                    filename=dl_result['filename'],
                    title=f"{event.get('camera_name', 'Unknown')} - {event.get('event_type', 'event')}",
                    original_url=video_url,
                    notes=notes,
                    camera_id=event.get('camera_id')
                )
                thumb_result = processor.extract_thumbnail(str(DOWNLOAD_DIR / dl_result['filename']))
                if thumb_result.get('success'):
                    db.update_video(video_id, thumbnail_path=thumb_result['thumbnail_path'])
                    trigger_vehicle_detect(video_id, thumb_result['thumbnail_path'])

                return jsonify({
                    'success': True,
                    'video_id': video_id,
                    'message': 'Video imported successfully'
                })
            else:
                return jsonify({'success': False, 'error': dl_result.get('error', 'Download failed')}), 500
        else:
            # Import metadata only (no video)
            print(f"[EcoEye Sync] Importing metadata only (no video)")

            # Save thumbnail if available
            thumbnail_path = None
            if event.get('thumbnail'):
                import base64
                import uuid
                thumb_data = event['thumbnail']
                if thumb_data.startswith('data:image'):
                    thumb_data = thumb_data.split(',')[1]
                thumb_bytes = base64.b64decode(thumb_data)
                thumb_filename = f"ecoeye_{event_id}_{uuid.uuid4().hex[:8]}.jpg"
                thumb_full_path = THUMBNAIL_DIR / thumb_filename
                with open(thumb_full_path, 'wb') as f:
                    f.write(thumb_bytes)
                thumbnail_path = str(thumb_full_path)
                print(f"[EcoEye Sync] Saved thumbnail: {thumbnail_path}")

            # Store as a record without video
            record_id = db.add_video(
                filename=f"ecoeye_metadata_{event_id}.placeholder",
                title=f"[No Video] {event.get('camera_name', 'Unknown')} - {event.get('event_type', 'event')}",
                original_url=f"ecoeye://{event_id}",
                thumbnail_path=thumbnail_path,
                notes=notes + "\n[Metadata only - no video file]",
                camera_id=event.get('camera_id')
            )
            print(f"[EcoEye Sync] Created record_id: {record_id}")

            # YOLO-World pre-screen: detects vehicles + gates person-face-v1
            if thumbnail_path:
                trigger_vehicle_detect(record_id, thumbnail_path)

            return jsonify({
                'success': True,
                'record_id': record_id,
                'message': 'Metadata imported successfully (no video)'
            })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@ecoeye_bp.route('/api/ecoeye/request-download', methods=['POST'])
def request_ecoeye_download():
    """Request EcoEye relay to download video for an event"""
    data = request.json
    event_id = data.get('event_id')

    if not event_id:
        return jsonify({'success': False, 'error': 'event_id required'}), 400

    try:
        # Send retry/download request to EcoEye relay
        resp = ecoeye_request('POST', f'api/events/{event_id}/retry',
            timeout=30
        )

        if resp.status_code == 200:
            return jsonify({
                'success': True,
                'message': 'Download request sent to EcoEye relay'
            })
        elif resp.status_code == 404:
            return jsonify({
                'success': False,
                'error': 'Event not found on relay'
            }), 404
        else:
            # Pass through relay's error message if available
            try:
                relay_json = resp.json()
                relay_error = relay_json.get('detail', relay_json.get('error', resp.text[:200]))
            except Exception:
                relay_error = resp.text[:200] if resp.text else f'Status {resp.status_code}'
            print(f"[EcoEye Download] Relay error {resp.status_code}: {relay_error}")
            return jsonify({
                'success': False,
                'error': f'Relay error: {relay_error}'
            }), resp.status_code if resp.status_code in (400, 409, 429) else 500

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@ecoeye_bp.route('/api/ecoeye/cameras', methods=['GET'])
def get_ecoeye_cameras():
    """Get list of cameras from EcoEye for filter dropdown"""
    try:
        # Try new API first
        resp = ecoeye_request('GET', 'api/cameras', timeout=15)
        if resp.status_code == 200:
            result = resp.json()
            cameras = result if isinstance(result, list) else result.get('cameras', [])
            print(f"[EcoEye Cameras] Found {len(cameras)} cameras (new API)")
            return jsonify({'success': True, 'cameras': cameras})
        print(f"[EcoEye Cameras] New API returned {resp.status_code}, falling back to PHP")
    except Exception as e:
        print(f"[EcoEye Cameras] New API error: {e}, falling back to PHP")

    # Fallback to PHP endpoint
    try:
        resp = ecoeye_request('GET', 'api-filters.php', params={'type': 'cameras'}, timeout=15)
        result = resp.json()
        if result.get('success'):
            cameras = result.get('cameras', [])
            print(f"[EcoEye Cameras] Found {len(cameras)} cameras (PHP fallback)")
            return jsonify({'success': True, 'cameras': cameras})
        return jsonify({'success': False, 'error': 'Failed to fetch cameras'}), 500
    except Exception as e:
        print(f"[EcoEye Cameras] PHP fallback error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@ecoeye_bp.route('/api/ecoeye/sites', methods=['GET'])
def get_ecoeye_sites():
    """Get list of sites from EcoEye for filter dropdown"""
    try:
        # Try new API first
        resp = ecoeye_request('GET', 'api/sites', timeout=15)
        if resp.status_code == 200:
            result = resp.json()
            sites = result if isinstance(result, list) else result.get('sites', [])
            print(f"[EcoEye Sites] Found {len(sites)} sites (new API)")
            return jsonify({'success': True, 'sites': sites})
        print(f"[EcoEye Sites] New API returned {resp.status_code}, falling back to PHP")
    except Exception as e:
        print(f"[EcoEye Sites] New API error: {e}, falling back to PHP")

    # Fallback to PHP endpoint
    try:
        resp = ecoeye_request('GET', 'api-filters.php', params={'type': 'sites'}, timeout=15)
        result = resp.json()
        if result.get('success'):
            sites = result.get('sites', [])
            print(f"[EcoEye Sites] Found {len(sites)} sites (PHP fallback)")
            return jsonify({'success': True, 'sites': sites})
        return jsonify({'success': False, 'error': 'Failed to fetch sites'}), 500
    except Exception as e:
        print(f"[EcoEye Sites] PHP fallback error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ===== EcoEye Event Tags API (proxy to relay) =====

@ecoeye_bp.route('/api/ecoeye/tags', methods=['GET'])
def get_ecoeye_tags():
    """Get all available tags or tags for specific event(s)"""
    try:
        params = {}
        if request.args.get('event_id'):
            params['event_id'] = request.args.get('event_id')
        if request.args.get('event_ids'):
            params['event_ids'] = request.args.get('event_ids')

        resp = ecoeye_request('GET', 'api-tags.php',
            params=params,
            timeout=30
        )
        return jsonify(resp.json())
    except Exception as e:
        print(f"[EcoEye Tags] Error fetching tags: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@ecoeye_bp.route('/api/ecoeye/tags', methods=['POST'])
def create_ecoeye_tag():
    """Create a new tag"""
    try:
        resp = ecoeye_request('POST', 'api-tags.php',
            json=request.json,
            timeout=30
        )
        return jsonify(resp.json())
    except Exception as e:
        print(f"[EcoEye Tags] Error creating tag: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@ecoeye_bp.route('/api/ecoeye/tags/<int:tag_id>', methods=['DELETE'])
def delete_ecoeye_tag(tag_id):
    """Delete a tag"""
    try:
        resp = ecoeye_request('DELETE', f'api-tags.php?tag_id={tag_id}',
            timeout=30
        )
        return jsonify(resp.json())
    except Exception as e:
        print(f"[EcoEye Tags] Error deleting tag: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@ecoeye_bp.route('/api/ecoeye/tags/assign', methods=['POST'])
def assign_ecoeye_tags():
    """Assign tags to events"""
    data = request.json
    print(f"[EcoEye Tags] Assigning tags: {data.get('tag_ids')} to events: {data.get('event_ids')}")

    try:
        resp = ecoeye_request('POST', 'api-tags.php?action=assign',
            json=data,
            timeout=30
        )
        result = resp.json()
        print(f"[EcoEye Tags] Assignment result: {result}")
        return jsonify(result)
    except Exception as e:
        print(f"[EcoEye Tags] Error assigning tags: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@ecoeye_bp.route('/api/ecoeye/tags/remove', methods=['POST'])
def remove_ecoeye_tags():
    """Remove tags from events"""
    data = request.json
    print(f"[EcoEye Tags] Removing tags: {data.get('tag_ids')} from events: {data.get('event_ids')}")

    try:
        resp = ecoeye_request('POST', 'api-tags.php?action=remove',
            json=data,
            timeout=30
        )
        result = resp.json()
        print(f"[EcoEye Tags] Removal result: {result}")
        return jsonify(result)
    except Exception as e:
        print(f"[EcoEye Tags] Error removing tags: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ===== EcoEye Sync Endpoints =====

@ecoeye_bp.route('/sync-settings')
def sync_settings():
    """Sync settings interface"""
    return render_template('sync_settings.html')


@ecoeye_bp.route('/api/sync/ecoeye/config', methods=['GET'])
def get_ecoeye_config():
    """Get EcoEye configuration status (without exposing credentials)"""
    try:
        has_credentials = sync_config.has_ecoeye_credentials()

        return jsonify({
            'success': True,
            'configured': has_credentials,
            'base_url': ECOEYE_API_BASE
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@ecoeye_bp.route('/api/sync/ecoeye/config', methods=['POST'])
def set_ecoeye_config():
    """Set EcoEye API credentials"""
    try:
        data = request.json
        api_key = data.get('api_key')
        api_secret = data.get('api_secret')

        if not api_key or not api_secret:
            return jsonify({'success': False, 'error': 'API key and secret required'}), 400

        sync_config.set_ecoeye_credentials(api_key, api_secret)
        # Note: base_url comes from ECOEYE_API_BASE constant, not stored separately

        return jsonify({'success': True, 'message': 'EcoEye credentials saved'})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@ecoeye_bp.route('/api/sync/ecoeye/test', methods=['POST'])
def test_ecoeye_connection():
    """Test connection to EcoEye API"""
    try:
        global ecoeye_client

        if not ECOEYE_API_KEY or not ECOEYE_API_SECRET:
            return jsonify({'success': False, 'error': 'EcoEye credentials not configured in api.py'}), 400

        # Initialize client with credentials
        ecoeye_client = EcoEyeSyncClient(
            download_dir=DOWNLOAD_DIR,
            api_key=ECOEYE_API_KEY,
            api_secret=ECOEYE_API_SECRET,
            base_url=ECOEYE_API_BASE
        )

        result = ecoeye_client.test_connection()
        return jsonify(result)

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@ecoeye_bp.route('/api/sync/ecoeye/alerts', methods=['POST'])
def sync_ecoeye_alerts():
    """Sync alerts from EcoEye"""
    try:
        global ecoeye_client

        if not ECOEYE_API_KEY or not ECOEYE_API_SECRET:
            return jsonify({'success': False, 'error': 'EcoEye credentials not configured in api.py'}), 400

        # Initialize client if not already done
        if not ecoeye_client:
            ecoeye_client = EcoEyeSyncClient(
                download_dir=DOWNLOAD_DIR,
                api_key=ECOEYE_API_KEY,
                api_secret=ECOEYE_API_SECRET,
                base_url=ECOEYE_API_BASE
            )

        # Get time range from request
        data = request.json or {}
        start_time = data.get('start_time')  # ISO format
        end_time = data.get('end_time')  # ISO format

        from datetime import datetime
        if start_time:
            start_time = datetime.fromisoformat(start_time)
        if end_time:
            end_time = datetime.fromisoformat(end_time)

        # Log sync start
        sync_id = sync_config.log_sync_start('ecoeye_alerts')

        # Perform sync
        result = ecoeye_client.sync_alerts_to_database(start_time, end_time)

        if result['success']:
            sync_config.log_sync_complete(
                sync_id,
                result['total_synced'],
                result['total_synced'],
                0,
                result
            )
        else:
            sync_config.log_sync_error(sync_id, result.get('error', 'Unknown error'))

        return jsonify(result)

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@ecoeye_bp.route('/api/sync/ecoeye/videos', methods=['POST'])
def download_ecoeye_videos():
    """Download videos for EcoEye alerts"""
    try:
        global ecoeye_client

        if not ECOEYE_API_KEY or not ECOEYE_API_SECRET:
            return jsonify({'success': False, 'error': 'EcoEye credentials not configured in api.py'}), 400

        # Initialize client if not already done
        if not ecoeye_client:
            ecoeye_client = EcoEyeSyncClient(
                download_dir=DOWNLOAD_DIR,
                api_key=ECOEYE_API_KEY,
                api_secret=ECOEYE_API_SECRET,
                base_url=ECOEYE_API_BASE
            )

        # Get limit from request
        data = request.json or {}
        limit = data.get('limit', 10)

        # Log sync start
        sync_id = sync_config.log_sync_start('ecoeye_videos')

        # Download videos
        result = ecoeye_client.download_pending_videos(limit)

        if result['success']:
            sync_config.log_sync_complete(
                sync_id,
                result['total_pending'],
                result['downloaded'],
                result['failed'],
                result
            )
        else:
            sync_config.log_sync_error(sync_id, result.get('error', 'Unknown error'))

        return jsonify(result)

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@ecoeye_bp.route('/api/sync/ecoeye/status', methods=['GET'])
def get_ecoeye_status():
    """Get EcoEye sync status"""
    try:
        global ecoeye_client

        if not ECOEYE_API_KEY or not ECOEYE_API_SECRET:
            return jsonify({'success': False, 'error': 'EcoEye credentials not configured in api.py'}), 400

        # Initialize client if not already done
        if not ecoeye_client:
            ecoeye_client = EcoEyeSyncClient(
                download_dir=DOWNLOAD_DIR,
                api_key=ECOEYE_API_KEY,
                api_secret=ECOEYE_API_SECRET,
                base_url=ECOEYE_API_BASE
            )

        result = ecoeye_client.get_sync_status()
        return jsonify(result)

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@ecoeye_bp.route('/api/sync/ecoeye/alerts/list', methods=['GET'])
def list_ecoeye_alerts():
    """Get list of synced EcoEye alerts"""
    try:
        limit = int(request.args.get('limit', 50))
        offset = int(request.args.get('offset', 0))

        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)

            cursor.execute('''
                SELECT alert_id, camera_id, timestamp, alert_type, confidence,
                       video_available, video_downloaded, local_video_path, synced_at
                FROM ecoeye_alerts
                ORDER BY timestamp DESC
                LIMIT %s OFFSET %s
            ''', (limit, offset))

            alerts = [dict(row) for row in cursor.fetchall()]

            # Get total count
            cursor.execute('SELECT COUNT(*) as count FROM ecoeye_alerts')
            total = cursor.fetchone()['count']

        return jsonify({
            'success': True,
            'alerts': alerts,
            'total': total,
            'limit': limit,
            'offset': offset
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
