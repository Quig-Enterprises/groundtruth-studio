from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory, render_template, g
from werkzeug.utils import secure_filename
import os
import json
import logging
import atexit
from pathlib import Path
from database import VideoDatabase
from db_connection import init_connection_pool, close_connection_pool, get_connection
from psycopg2 import extras
from downloader import VideoDownloader
from video_utils import VideoProcessor
from download_queue import DownloadQueue
from yolo_exporter import YOLOExporter
from camera_topology import CameraTopologyLearner
from ecoeye_sync import EcoEyeSyncClient
from unifi_protect_client import UniFiProtectClient, UniFiProtectIntegration
from sync_config import SyncConfigManager
from training_queue import TrainingQueueClient, init_training_jobs_table
from vibration_exporter import VibrationExporter
from location_exporter import LocationExporter
from sample_router import SampleRouter
from face_clustering import FaceClusterer
from auto_detect_runner import trigger_auto_detect, run_detection_on_thumbnail

app = Flask(__name__,
            template_folder='../templates',
            static_folder='../static')
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024 * 1024  # 2GB max upload
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
DOWNLOAD_DIR = BASE_DIR / 'downloads'
THUMBNAIL_DIR = BASE_DIR / 'thumbnails'
EXPORT_DIR = BASE_DIR / 'exports'

# Initialize PostgreSQL connection pool
init_connection_pool()
atexit.register(close_connection_pool)

# EcoEye API Configuration
ECOEYE_API_BASE = 'https://alert.ecoeyetech.com'
# Legacy PHP endpoints (api-thumbnails.php, api-filters.php, api-tags.php)
ECOEYE_PHP_API_KEY = '-3tsV7gFLF-nxAAUt-zRETAJLWEyxEWszwdT4fCKpeI'
# New /api/ endpoints with HMAC-SHA256 signing
ECOEYE_API_KEY = '2cVrlQ2XW3wxDwZmVzQ3lOCi96jnqKnH8v1wyU97lM0'
ECOEYE_API_SECRET = os.environ.get('ECOEYE_API_SECRET', '8SyPU2FW05yjtaNVOlCGPoyfqFSXJiGp36SEiiKqT-c0dSZDTBr89M8RsMTsD7_pyDHW2b6MxfPxuVUVlzpb8g')

def ecoeye_request(method, endpoint, **kwargs):
    """Make authenticated request to EcoEye API with HMAC-SHA256 signing"""
    import requests
    import hmac
    import hashlib
    import time as _time
    import json as _json

    headers = kwargs.pop('headers', {})
    is_legacy = '.php' in endpoint

    if is_legacy:
        # Legacy PHP endpoints use simple API key auth
        headers['X-API-Key'] = ECOEYE_PHP_API_KEY
    else:
        # New /api/ endpoints use HMAC-SHA256 signing
        headers['X-API-Key'] = ECOEYE_API_KEY
        headers['X-Timestamp'] = str(int(_time.time()))

        if ECOEYE_API_SECRET:
            canonical = f"{method.upper()}\n/{endpoint}\n"

            params = kwargs.get('params')
            if params:
                param_str = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
                canonical += param_str + "\n"
            else:
                canonical += "\n"

            json_data = kwargs.get('json')
            if json_data:
                body_json = _json.dumps(json_data, sort_keys=True)
                body_hash = hashlib.sha256(body_json.encode()).hexdigest()
                canonical += body_hash

            signature = hmac.new(
                ECOEYE_API_SECRET.encode(),
                canonical.encode(),
                hashlib.sha256
            ).hexdigest()
            headers['X-Signature'] = signature

    url = f"{ECOEYE_API_BASE}/{endpoint}"
    return requests.request(method, url, headers=headers, **kwargs)

db = VideoDatabase()  # Uses DATABASE_URL from environment
downloader = VideoDownloader(str(DOWNLOAD_DIR))
processor = VideoProcessor(str(THUMBNAIL_DIR))
download_queue = DownloadQueue(DOWNLOAD_DIR, THUMBNAIL_DIR, db)
yolo_exporter = YOLOExporter(db, DOWNLOAD_DIR, EXPORT_DIR)
vibration_exporter = VibrationExporter(db, EXPORT_DIR)
location_exporter = LocationExporter(db, EXPORT_DIR, THUMBNAIL_DIR, DOWNLOAD_DIR)
topology_learner = CameraTopologyLearner()  # Uses DATABASE_URL from environment
sample_router = SampleRouter(db)
face_clusterer = FaceClusterer()

# Initialize sync components
sync_config = SyncConfigManager()  # Uses DATABASE_URL from environment
ecoeye_client = None  # Initialized on-demand with credentials
unifi_client = None  # Initialized on-demand with credentials

# Initialize training queue
init_training_jobs_table(db)
training_queue = TrainingQueueClient(db)

# Run schema migrations (add camera_id to videos, etc.)
from schema import run_migrations
try:
    run_migrations()
except Exception as e:
    print(f"[Migrations] Warning: {e}")

ALLOWED_EXTENSIONS = {'mp4', 'avi', 'mov', 'mkv', 'webm'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# ---- RBAC: Role-based access control via nginx auth_request ----

@app.before_request
def check_role_permissions():
    """Enforce write protection for viewers. Nginx passes X-Auth-Role header."""
    g.user_role = request.headers.get('X-Auth-Role', 'viewer')
    g.can_write = g.user_role in ('super', 'admin', 'user')

    # Block write operations for viewers
    if request.method in ('POST', 'PUT', 'DELETE') and not g.can_write:
        # Allow static files and client logging through
        if request.path.startswith('/static/') or request.path == '/api/client-log':
            return None
        return jsonify({'success': False, 'error': 'Insufficient permissions. Write access requires user role or above.'}), 403

@app.context_processor
def inject_role():
    """Make role info available in all templates."""
    return {
        'user_role': getattr(g, 'user_role', 'viewer'),
        'can_write': getattr(g, 'can_write', False)
    }

@app.context_processor
def inject_static_helpers():
    """Provide static_v() for cache-busting static files with their mtime."""
    def static_v(filename):
        filepath = app.static_folder and os.path.join(app.static_folder, filename)
        try:
            mtime = int(os.path.getmtime(filepath))
        except OSError:
            mtime = 0
        return f'/static/{filename}?v={mtime}'
    return {'static_v': static_v}

@app.route('/api/client-log', methods=['POST'])
def client_log():
    """Receive client-side log entries from gt-utils.js"""
    try:
        entry = request.get_json(silent=True) or {}
        print(f"[CLIENT] [{entry.get('type','info')}] {entry.get('page','?')}: {entry.get('message','')}", flush=True)
        if entry.get('caller'):
            print(f"[CLIENT]   caller: {entry.get('caller')}", flush=True)
    except Exception:
        pass
    return jsonify({'success': True})

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/add-content')
def add_content():
    return render_template('add_content.html')

@app.route('/annotate')
def annotate():
    return render_template('annotate.html')

@app.route('/ecoeye-preview')
def ecoeye_preview():
    return render_template('ecoeye_preview.html')

@app.route('/api/ecoeye/events', methods=['GET'])
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

            print(f"[EcoEye Events] Returned {len(result['events'])} events, {len(local_imports)} imported locally")

        return jsonify(result)
    except Exception as e:
        print(f"[EcoEye Events] Error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/ecoeye/sync-sample', methods=['POST'])
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

            dl_result = downloader.download(video_url, event.get('camera_name', 'ecoeye'))

            if dl_result.get('success'):
                video_id = db.add_video(
                    filename=dl_result['filename'],
                    title=f"{event.get('camera_name', 'Unknown')} - {event.get('event_type', 'event')}",
                    original_url=video_url,
                    notes=notes,
                    camera_id=event.get('camera_id')
                )
                processor.generate_thumbnail(str(DOWNLOAD_DIR / dl_result['filename']), video_id)

                # Auto-detect persons/faces on thumbnail
                video_record = db.get_video(video_id)
                if video_record and video_record.get('thumbnail_path'):
                    trigger_auto_detect(video_id, video_record['thumbnail_path'])

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

            # Auto-detect persons/faces on thumbnail
            if thumbnail_path:
                trigger_auto_detect(record_id, thumbnail_path)

            return jsonify({
                'success': True,
                'record_id': record_id,
                'message': 'Metadata imported successfully (no video)'
            })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/ecoeye/request-download', methods=['POST'])
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

@app.route('/api/ecoeye/cameras', methods=['GET'])
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

@app.route('/api/ecoeye/sites', methods=['GET'])
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

@app.route('/api/ecoeye/tags', methods=['GET'])
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

@app.route('/api/ecoeye/tags', methods=['POST'])
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

@app.route('/api/ecoeye/tags/<int:tag_id>', methods=['DELETE'])
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

@app.route('/api/ecoeye/tags/assign', methods=['POST'])
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

@app.route('/api/ecoeye/tags/remove', methods=['POST'])
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

@app.route('/api/videos', methods=['GET'])
def get_videos():
    """Get all videos with optional search"""
    query = request.args.get('search', '')
    limit = int(request.args.get('limit', 100))
    offset = int(request.args.get('offset', 0))

    if query:
        videos = db.search_videos(query)
    else:
        library_id = request.args.get('library')
        videos = db.get_all_videos(limit, offset, library_id=int(library_id) if library_id else None)

    # Enrich videos with EcoEye import flags
    for video in videos:
        original_url = video.get('original_url', '') or ''
        filename = video.get('filename', '') or ''

        # Detect EcoEye imports
        is_ecoeye = original_url.startswith('ecoeye://')
        has_video_file = not filename.endswith('.placeholder')

        video['is_ecoeye_import'] = is_ecoeye
        video['has_video_file'] = has_video_file

        if is_ecoeye:
            # Extract event ID from ecoeye:// URL
            video['ecoeye_event_id'] = original_url.replace('ecoeye://', '')

            # Parse camera/event info from title if available
            # Title format: "[No Video] Camera Name - event_type"
            title = video.get('title', '') or ''
            if title.startswith('[No Video] '):
                video['ecoeye_camera'] = title[11:].split(' - ')[0] if ' - ' in title else title[11:]

    # Add library memberships
    for video in videos:
        video['libraries'] = db.get_video_libraries(video['id'])

    # Attach bbox data for thumbnail overlays
    video_ids = [v['id'] for v in videos]
    bboxes_by_video = db.get_bboxes_for_video_ids(video_ids)
    for video in videos:
        video['bboxes'] = bboxes_by_video.get(video['id'], [])

    return jsonify({'success': True, 'videos': videos})


# ── Content Libraries ──────────────────────────────────────────────

@app.route('/api/libraries', methods=['GET'])
def get_libraries():
    """Get all content libraries with item counts"""
    libraries = db.get_all_libraries()
    return jsonify({'success': True, 'libraries': libraries})

@app.route('/api/libraries', methods=['POST'])
def create_library():
    """Create a new content library"""
    data = request.get_json()
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'success': False, 'error': 'Library name is required'}), 400
    try:
        library_id = db.create_library(name)
        return jsonify({'success': True, 'id': library_id, 'name': name})
    except Exception as e:
        return jsonify({'success': False, 'error': f'Library name already exists'}), 409

@app.route('/api/libraries/<int:library_id>', methods=['PUT'])
def rename_library(library_id):
    """Rename a content library"""
    data = request.get_json()
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'success': False, 'error': 'Library name is required'}), 400
    try:
        success = db.rename_library(library_id, name)
        if success:
            return jsonify({'success': True})
        return jsonify({'success': False, 'error': 'Cannot rename default library'}), 400
    except Exception:
        return jsonify({'success': False, 'error': 'Library name already exists'}), 409

@app.route('/api/libraries/<int:library_id>', methods=['DELETE'])
def delete_library(library_id):
    """Delete a content library (not the default)"""
    success = db.delete_library(library_id)
    if success:
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': 'Cannot delete default library'}), 400

@app.route('/api/libraries/<int:library_id>/items', methods=['POST'])
def add_library_items(library_id):
    """Add videos to a library"""
    data = request.get_json()
    video_ids = data.get('video_ids', [])
    if not video_ids:
        return jsonify({'success': False, 'error': 'No video IDs provided'}), 400
    added = db.add_to_library(library_id, video_ids)
    return jsonify({'success': True, 'added': added})

@app.route('/api/libraries/<int:library_id>/items/<int:video_id>', methods=['DELETE'])
def remove_library_item(library_id, video_id):
    """Remove a video from a library"""
    success = db.remove_from_library(library_id, video_id)
    return jsonify({'success': True, 'removed': success})

@app.route('/api/libraries/<int:library_id>/next-unannotated', methods=['GET'])
def get_next_unannotated(library_id):
    """Get the next unannotated video in a library"""
    current_video_id = request.args.get('current')
    current_video_id = int(current_video_id) if current_video_id else None
    video = db.get_next_unannotated_in_library(library_id, current_video_id)
    if video:
        return jsonify({'success': True, 'video': video})
    return jsonify({'success': True, 'video': None, 'message': 'All videos in this library are annotated'})

@app.route('/api/next-unannotated', methods=['GET'])
def get_next_unannotated_global():
    """Get the next unannotated video globally (no library filter)"""
    current_video_id = request.args.get('current')
    current_video_id = int(current_video_id) if current_video_id else None
    video = db.get_next_unannotated(current_video_id)
    if video:
        return jsonify({'success': True, 'video': video})
    return jsonify({'success': True, 'video': None, 'message': 'All videos are annotated'})

@app.route('/api/videos/<int:video_id>', methods=['GET'])
def get_video(video_id):
    """Get single video details"""
    video = db.get_video(video_id)
    if not video:
        return jsonify({'success': False, 'error': 'Video not found'}), 404

    # Add has_video_file flag
    filename = video.get('filename', '') or ''
    has_video_file = bool(filename and not filename.endswith('.placeholder'))
    if has_video_file:
        # Also check if file actually exists on disk
        video_path = DOWNLOAD_DIR / filename
        has_video_file = video_path.exists()
    video['has_video_file'] = has_video_file

    # Add is_ecoeye_import flag
    original_url = video.get('original_url', '') or ''
    video['is_ecoeye_import'] = original_url.startswith('ecoeye://')

    # Include location info if camera_id exists
    camera_id = video.get('camera_id')
    if not camera_id:
        # Try to parse from original_url: ecoeye://{timestamp}_{MAC}_{type}
        original_url = video.get('original_url', '') or ''
        if original_url.startswith('ecoeye://'):
            parts = original_url.replace('ecoeye://', '').split('_')
            if len(parts) >= 2:
                camera_id = parts[1]
                video['camera_id'] = camera_id

    if camera_id:
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
            cursor.execute('SELECT location_name FROM camera_locations WHERE camera_id = %s', (camera_id,))
            loc_row = cursor.fetchone()
            if loc_row:
                video['location_name'] = loc_row['location_name']

    # Add library memberships
    video['libraries'] = db.get_video_libraries(video_id)

    behaviors = db.get_video_behaviors(video_id)
    return jsonify({
        'success': True,
        'video': video,
        'behaviors': behaviors
    })

@app.route('/api/videos/<int:video_id>', methods=['DELETE'])
def delete_video(video_id):
    """Delete video from database and filesystem"""
    video = db.get_video(video_id)
    if not video:
        return jsonify({'success': False, 'error': 'Video not found'}), 404

    video_path = DOWNLOAD_DIR / video['filename']
    if video_path.exists():
        video_path.unlink()

    if video['thumbnail_path']:
        thumb_path = Path(video['thumbnail_path'])
        if thumb_path.exists():
            thumb_path.unlink()

    db.delete_video(video_id)
    return jsonify({'success': True})

@app.route('/api/download', methods=['POST'])
def download_video():
    """Add video URL to download queue"""
    data = request.get_json()
    url = data.get('url')

    if not url:
        return jsonify({'success': False, 'error': 'URL required'}), 400

    # Add to queue (handles duplicate detection)
    result = download_queue.add_to_queue(url)

    return jsonify(result)

@app.route('/api/download/status', methods=['GET'])
def download_status():
    """Get download queue status"""
    status = download_queue.get_queue_status()
    return jsonify(status)

@app.route('/api/video-info', methods=['POST'])
def get_video_info():
    """Get video info without downloading"""
    data = request.get_json()
    url = data.get('url')

    if not url:
        return jsonify({'success': False, 'error': 'URL required'}), 400

    result = downloader.get_video_info(url)
    return jsonify(result)

@app.route('/api/upload', methods=['POST'])
def upload_video():
    """Upload video file manually"""
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'No file provided'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'error': 'No file selected'}), 400

    if not allowed_file(file.filename):
        return jsonify({'success': False, 'error': 'Invalid file type'}), 400

    filename = secure_filename(file.filename)
    filepath = DOWNLOAD_DIR / filename

    counter = 1
    while filepath.exists():
        name, ext = os.path.splitext(filename)
        filename = f"{name}_{counter}{ext}"
        filepath = DOWNLOAD_DIR / filename
        counter += 1

    file.save(str(filepath))

    metadata_result = processor.get_video_metadata(str(filepath))
    if metadata_result['success']:
        metadata = metadata_result['metadata']
    else:
        metadata = {}

    thumb_result = processor.extract_thumbnail(str(filepath))
    thumbnail_path = thumb_result.get('thumbnail_path') if thumb_result['success'] else None

    title = request.form.get('title', filename)
    notes = request.form.get('notes', '')

    video_id = db.add_video(
        filename=filename,
        title=title,
        duration=metadata.get('duration'),
        width=metadata.get('width'),
        height=metadata.get('height'),
        file_size=metadata.get('file_size'),
        thumbnail_path=thumbnail_path,
        notes=notes
    )

    # Auto-detect persons/faces on thumbnail
    if thumbnail_path:
        trigger_auto_detect(video_id, thumbnail_path)

    return jsonify({
        'success': True,
        'video_id': video_id,
        'filename': filename
    })

@app.route('/api/videos/<int:video_id>/tags', methods=['POST'])
def add_tag(video_id):
    """Add tag to video"""
    data = request.get_json()
    tag = data.get('tag', '').strip()

    if not tag:
        return jsonify({'success': False, 'error': 'Tag required'}), 400

    success = db.tag_video(video_id, tag)
    return jsonify({'success': success})

@app.route('/api/videos/<int:video_id>/tags/<tag_name>', methods=['DELETE'])
def remove_tag(video_id, tag_name):
    """Remove tag from video"""
    success = db.untag_video(video_id, tag_name)
    return jsonify({'success': success})

@app.route('/api/tags', methods=['GET'])
def get_tags():
    """Get all tags"""
    tags = db.get_all_tags()
    return jsonify({'success': True, 'tags': tags})

@app.route('/api/videos/<int:video_id>/behaviors', methods=['POST'])
def add_behavior(video_id):
    """Add behavior annotation"""
    data = request.get_json()

    behavior_id = db.add_behavior_annotation(
        video_id=video_id,
        behavior_type=data.get('behavior_type'),
        start_time=data.get('start_time'),
        end_time=data.get('end_time'),
        confidence=data.get('confidence'),
        notes=data.get('notes')
    )

    return jsonify({'success': True, 'behavior_id': behavior_id})

@app.route('/downloads/<path:filename>')
def serve_video(filename):
    """Serve video file"""
    return send_from_directory(DOWNLOAD_DIR, filename)

@app.route('/thumbnails/<path:filename>')
def serve_thumbnail(filename):
    """Serve thumbnail file"""
    return send_from_directory(THUMBNAIL_DIR, filename)

@app.route('/api/videos/<int:video_id>/time-range-tags', methods=['GET'])
def get_time_range_tags(video_id):
    """Get all time-range tags for a video"""
    tags = db.get_time_range_tags(video_id)
    return jsonify({'success': True, 'tags': tags})

@app.route('/api/videos/<int:video_id>/time-range-tags', methods=['POST'])
def add_time_range_tag(video_id):
    """Add time-range tag"""
    data = request.get_json()

    tag_name = data.get('tag_name', '').strip()
    start_time = data.get('start_time')

    if not tag_name or start_time is None:
        return jsonify({'success': False, 'error': 'tag_name and start_time required'}), 400

    tag_id = db.add_time_range_tag(
        video_id=video_id,
        tag_name=tag_name,
        start_time=start_time,
        end_time=data.get('end_time'),
        is_negative=data.get('is_negative', False),
        comment=data.get('comment')
    )

    return jsonify({'success': True, 'tag_id': tag_id})

@app.route('/api/time-range-tags/<int:tag_id>', methods=['PUT'])
def update_time_range_tag(tag_id):
    """Update time-range tag (close tag, add comment)"""
    data = request.get_json()

    success = db.update_time_range_tag(
        tag_id=tag_id,
        tag_name=data.get('tag_name'),
        end_time=data.get('end_time'),
        is_negative=data.get('is_negative'),
        comment=data.get('comment')
    )

    return jsonify({'success': success})

@app.route('/api/time-range-tags/<int:tag_id>', methods=['GET'])
def get_time_range_tag(tag_id):
    """Get a single time-range tag by ID"""
    tag = db.get_time_range_tag_by_id(tag_id)
    if tag:
        return jsonify({'success': True, 'tag': tag})
    return jsonify({'success': False, 'error': 'Tag not found'}), 404

@app.route('/api/time-range-tags/<int:tag_id>', methods=['DELETE'])
def delete_time_range_tag(tag_id):
    """Delete time-range tag"""
    success = db.delete_time_range_tag(tag_id)
    return jsonify({'success': success})

@app.route('/api/videos/<int:video_id>/keyframe-annotations', methods=['GET'])
def get_keyframe_annotations(video_id):
    """Get all keyframe annotations for a video"""
    annotations = db.get_keyframe_annotations(video_id)
    return jsonify({'success': True, 'annotations': annotations})

@app.route('/api/videos/<int:video_id>/keyframe-annotations', methods=['POST'])
def add_keyframe_annotation(video_id):
    """Add keyframe annotation with bounding box"""
    data = request.get_json()

    required = ['timestamp', 'bbox_x', 'bbox_y', 'bbox_width', 'bbox_height']
    if not all(k in data for k in required):
        return jsonify({'success': False, 'error': 'Missing required fields'}), 400

    annotation_id = db.add_keyframe_annotation(
        video_id=video_id,
        timestamp=data['timestamp'],
        bbox_x=data['bbox_x'],
        bbox_y=data['bbox_y'],
        bbox_width=data['bbox_width'],
        bbox_height=data['bbox_height'],
        activity_tag=data.get('activity_tag'),
        moment_tag=data.get('moment_tag'),
        is_negative=data.get('is_negative', False),
        comment=data.get('comment'),
        reviewed=data.get('reviewed', True)
    )

    return jsonify({'success': True, 'annotation_id': annotation_id})

@app.route('/api/keyframe-annotations/<int:annotation_id>', methods=['GET'])
def get_keyframe_annotation(annotation_id):
    """Get a single keyframe annotation by ID"""
    annotation = db.get_keyframe_annotation_by_id(annotation_id)
    if annotation:
        return jsonify({'success': True, 'annotation': annotation})
    return jsonify({'success': False, 'error': 'Annotation not found'}), 404

@app.route('/api/keyframe-annotations/<int:annotation_id>', methods=['PUT'])
def update_keyframe_annotation(annotation_id):
    """Update keyframe annotation"""
    data = request.get_json()

    success = db.update_keyframe_annotation(
        annotation_id=annotation_id,
        bbox_x=data.get('bbox_x'),
        bbox_y=data.get('bbox_y'),
        bbox_width=data.get('bbox_width'),
        bbox_height=data.get('bbox_height'),
        activity_tag=data.get('activity_tag'),
        moment_tag=data.get('moment_tag'),
        is_negative=data.get('is_negative'),
        comment=data.get('comment'),
        reviewed=data.get('reviewed')
    )

    return jsonify({'success': success})

@app.route('/api/keyframe-annotations/<int:annotation_id>', methods=['DELETE'])
def delete_keyframe_annotation(annotation_id):
    """Delete keyframe annotation"""
    success = db.delete_keyframe_annotation(annotation_id)
    return jsonify({'success': success})

@app.route('/api/activity-tags', methods=['GET'])
def get_activity_tags():
    """Get all unique activity tags for auto-suggest"""
    tags = db.get_all_activity_tags()
    return jsonify({'success': True, 'tags': tags})

@app.route('/api/moment-tags', methods=['GET'])
def get_moment_tags():
    """Get all unique moment tags for auto-suggest"""
    tags = db.get_all_moment_tags()
    return jsonify({'success': True, 'tags': tags})

@app.route('/api/tag-suggestions', methods=['GET'])
def get_tag_suggestions():
    """Get tag suggestions, optionally filtered by category"""
    category = request.args.get('category')
    suggestions = db.get_tag_suggestions_by_category(category)
    return jsonify({'success': True, 'suggestions': suggestions})

@app.route('/api/tag-suggestions/categories', methods=['GET'])
def get_suggestion_categories():
    """Get all tag suggestion categories"""
    categories = db.get_all_suggestion_categories()
    return jsonify({'success': True, 'categories': categories})

@app.route('/api/tag-suggestions', methods=['POST'])
def add_tag_suggestion():
    """Add new tag suggestion"""
    data = request.get_json()

    category = data.get('category', '').strip()
    tag_text = data.get('tag_text', '').strip()

    if not category or not tag_text:
        return jsonify({'success': False, 'error': 'category and tag_text required'}), 400

    suggestion_id = db.add_tag_suggestion(
        category=category,
        tag_text=tag_text,
        is_negative=data.get('is_negative', False),
        description=data.get('description'),
        sort_order=data.get('sort_order', 0)
    )

    return jsonify({'success': True, 'suggestion_id': suggestion_id})

@app.route('/api/tag-suggestions/<int:suggestion_id>', methods=['PUT'])
def update_tag_suggestion(suggestion_id):
    """Update tag suggestion"""
    data = request.get_json()

    success = db.update_tag_suggestion(
        suggestion_id=suggestion_id,
        category=data.get('category'),
        tag_text=data.get('tag_text'),
        is_negative=data.get('is_negative'),
        description=data.get('description'),
        sort_order=data.get('sort_order')
    )

    return jsonify({'success': success})

@app.route('/api/tag-suggestions/<int:suggestion_id>', methods=['DELETE'])
def delete_tag_suggestion(suggestion_id):
    """Delete tag suggestion"""
    success = db.delete_tag_suggestion(suggestion_id)
    return jsonify({'success': success})

@app.route('/api/tag-suggestions/seed', methods=['POST'])
def seed_tag_suggestions():
    """Seed database with default tag suggestions"""
    db.seed_default_tag_suggestions()
    return jsonify({'success': True, 'message': 'Default tag suggestions added'})

# Tag Group API Endpoints
@app.route('/api/tag-groups', methods=['GET'])
def get_tag_groups():
    """Get all tag groups, optionally filtered by annotation type"""
    annotation_type = request.args.get('annotation_type')
    groups = db.get_tag_groups(annotation_type)

    # Add options for each group
    for group in groups:
        group['options'] = db.get_tag_options(group['id'])

    return jsonify({'success': True, 'groups': groups})

@app.route('/api/tag-groups/<group_name>', methods=['GET'])
def get_tag_group_by_name(group_name):
    """Get a specific tag group with its options"""
    group = db.get_tag_group_by_name(group_name)
    if not group:
        return jsonify({'success': False, 'error': 'Tag group not found'}), 404

    group['options'] = db.get_tag_options(group['id'])
    return jsonify({'success': True, 'group': group})

@app.route('/api/tag-schema', methods=['GET'])
def get_tag_schema():
    """Get complete tag schema for dynamic form generation

    Query params:
    - annotation_type: 'time_range' or 'keyframe'
    - ground_truth: optional filter by ground truth value
    - is_negative: optional filter for negative examples
    """
    annotation_type = request.args.get('annotation_type')
    ground_truth = request.args.get('ground_truth')
    is_negative = request.args.get('is_negative') == 'true'

    groups = db.get_tag_groups(annotation_type)

    # Add options for each group
    result_groups = []
    for group in groups:
        group['options'] = db.get_tag_options(group['id'])

        # Apply conditional display logic
        # This is a simplified version - full logic would be more complex
        include_group = True

        # False positive groups only show when is_negative is true
        if group['group_name'].startswith('false_positive_') and not is_negative:
            include_group = False

        # Present/Absent indicators conditional on ground truth and negative flag
        if group['group_name'] == 'present_indicators' and (ground_truth != 'power_loading' or is_negative):
            include_group = False
        if group['group_name'] == 'absent_indicators' and (ground_truth != 'power_loading' or not is_negative):
            include_group = False

        # Power loading specific groups
        power_loading_groups = ['violation_context', 'motor_state', 'boat_motion']
        if group['group_name'] in power_loading_groups and ground_truth != 'power_loading':
            include_group = False

        # License plate specific groups
        license_plate_groups = ['vehicle_type', 'plate_state']
        if group['group_name'] in license_plate_groups and ground_truth != 'license_plate':
            include_group = False

        # Face detection specific groups
        face_groups = ['face_angle', 'face_obstruction']
        if group['group_name'] in face_groups and ground_truth != 'face_detected':
            include_group = False

        if include_group:
            result_groups.append(group)

    return jsonify({'success': True, 'groups': result_groups})

@app.route('/api/annotations/<int:annotation_id>/tags', methods=['POST'])
def add_annotation_tags(annotation_id):
    """Add tags to an annotation

    Request body:
    {
        "annotation_type": "time_range" or "keyframe",
        "tags": {
            "ground_truth": "power_loading",
            "confidence_level": "certain",
            "lighting_conditions": ["sun_glare", "bright_overexposed"],
            ...
        }
    }
    """
    data = request.get_json()
    annotation_type = data.get('annotation_type')
    tags = data.get('tags', {})

    if not annotation_type or annotation_type not in ['time_range', 'keyframe']:
        return jsonify({'success': False, 'error': 'Invalid annotation_type'}), 400

    # Delete existing tags first
    db.delete_annotation_tags(annotation_id, annotation_type)

    # Handle structured scenario data (scenario, bboxes, notVisible, notPresent, skipped)
    # Store these as JSON in a special "_scenario_data" tag group
    scenario_data_keys = ['scenario', 'bboxes', 'notVisible', 'notPresent', 'skipped']
    scenario_data = {k: v for k, v in tags.items() if k in scenario_data_keys}

    print(f"[API] add_annotation_tags called for annotation_id={annotation_id}, type={annotation_type}")
    print(f"[API] scenario_data keys found: {list(scenario_data.keys())}")
    print(f"[API] scenario_data['bboxes'] has {len(scenario_data.get('bboxes', {}))} items")

    if scenario_data:
        # Ensure the _scenario_data tag group exists
        scenario_group = db.get_tag_group_by_name('_scenario_data')
        if not scenario_group:
            # Create it if it doesn't exist
            with get_connection() as conn:
                cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
                cursor.execute('''
                    INSERT INTO tag_groups (group_name, display_name, group_type, description, is_required, applies_to, sort_order)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (group_name) DO NOTHING
                ''', ('_scenario_data', 'Scenario Data', 'text', 'Structured scenario data (JSON)', 0, 'both', 999))
                conn.commit()
            scenario_group = db.get_tag_group_by_name('_scenario_data')
            print(f"[API] Created _scenario_data tag group with id={scenario_group['id']}")

        if scenario_group:
            import json
            json_data = json.dumps(scenario_data)
            print(f"[API] Saving JSON data ({len(json_data)} bytes): {json_data[:200]}...")
            db.add_annotation_tag(annotation_id, annotation_type, scenario_group['id'], json_data)

    # Add regular tags
    for group_name, tag_value in tags.items():
        # Skip scenario data keys (already handled above)
        if group_name in scenario_data_keys:
            continue

        group = db.get_tag_group_by_name(group_name)
        if not group:
            continue

        # For checkbox groups, tag_value is a list
        if group['group_type'] == 'checkbox' and isinstance(tag_value, list):
            tag_value_str = ','.join(tag_value)
        else:
            tag_value_str = str(tag_value) if tag_value else ''

        if tag_value_str:
            db.add_annotation_tag(annotation_id, annotation_type, group['id'], tag_value_str)

    return jsonify({'success': True})

@app.route('/api/annotations/<int:annotation_id>/tags', methods=['GET'])
def get_annotation_tags_api(annotation_id):
    """Get all tags for an annotation"""
    annotation_type = request.args.get('annotation_type')
    if not annotation_type:
        return jsonify({'success': False, 'error': 'annotation_type required'}), 400

    tags = db.get_annotation_tags(annotation_id, annotation_type)
    print(f"[API] get_annotation_tags for annotation_id={annotation_id}, type={annotation_type}")
    print(f"[API] Retrieved {len(tags)} tag records from database")

    # Format tags into a dictionary grouped by tag group
    tags_dict = {}
    for tag in tags:
        group_name = tag['group_name']
        tag_value = tag['tag_value']

        # Parse JSON scenario data
        if group_name == '_scenario_data':
            import json
            try:
                print(f"[API] Found _scenario_data tag, parsing JSON ({len(tag_value)} bytes)")
                scenario_data = json.loads(tag_value)
                print(f"[API] Parsed scenario_data keys: {list(scenario_data.keys())}")
                if 'bboxes' in scenario_data:
                    print(f"[API] scenario_data['bboxes'] has {len(scenario_data['bboxes'])} items")
                    print(f"[API] bbox keys: {list(scenario_data['bboxes'].keys())}")
                # Merge scenario data into tags_dict at top level
                tags_dict.update(scenario_data)
            except Exception as e:
                print(f"[API] Error parsing JSON scenario data: {e}")
                pass  # Skip if JSON parsing fails
            continue

        # For checkbox groups, split comma-separated values back into list
        if tag['group_type'] == 'checkbox' and ',' in tag_value:
            tags_dict[group_name] = tag_value.split(',')
        else:
            tags_dict[group_name] = tag_value

    print(f"[API] Returning tags_dict with keys: {list(tags_dict.keys())}")
    return jsonify({'success': True, 'tags': tags_dict})

@app.route('/api/tag-taxonomy/seed', methods=['POST'])
def seed_tag_taxonomy():
    """Seed database with comprehensive tag taxonomy"""
    try:
        db.seed_comprehensive_tag_taxonomy()
        return jsonify({'success': True, 'message': '29 tag groups seeded successfully'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/system/status', methods=['GET'])
def system_status():
    """Check system dependencies"""
    return jsonify({
        'success': True,
        'yt_dlp_installed': downloader.check_yt_dlp_installed(),
        'ffmpeg_installed': processor.check_ffmpeg_installed()
    })

# YOLO Export Endpoints
@app.route('/vibration-export')
def vibration_export_page():
    return render_template('vibration_export.html')

@app.route('/location-export')
def location_export_page():
    return render_template('location_export.html')

@app.route('/prediction-review')
def prediction_review_page():
    return render_template('prediction_review.html')

@app.route('/yolo-export')
def yolo_export_page():
    """YOLO export configuration page"""
    return render_template('yolo_export.html')

@app.route('/api/yolo/configs', methods=['GET'])
def get_yolo_configs():
    """Get all YOLO export configurations"""
    try:
        configs = yolo_exporter.get_export_configs()
        return jsonify({'success': True, 'configs': configs})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/yolo/configs', methods=['POST'])
def create_yolo_config():
    """Create new YOLO export configuration"""
    try:
        data = request.get_json()
        config_name = data.get('config_name')
        class_mapping = data.get('class_mapping')  # Dict: {activity_tag: class_id}
        description = data.get('description', '')

        if not config_name or not class_mapping:
            return jsonify({'success': False, 'error': 'Missing required fields'}), 400

        config_id = yolo_exporter.create_export_config(
            config_name=config_name,
            class_mapping=class_mapping,
            description=description,
            include_reviewed_only=bool(data.get('include_reviewed_only', False)),
            include_ai_generated=bool(data.get('include_ai_generated', True)),
            include_negative_examples=bool(data.get('include_negative_examples', True)),
            min_confidence=data.get('min_confidence', 0.0)
        )

        return jsonify({'success': True, 'config_id': config_id})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/yolo/configs/<int:config_id>', methods=['GET'])
def get_yolo_config(config_id):
    """Get a single YOLO export configuration"""
    try:
        configs = yolo_exporter.get_export_configs()
        config = next((c for c in configs if c['id'] == config_id), None)
        if not config:
            return jsonify({'success': False, 'error': 'Config not found'}), 404
        return jsonify({'success': True, 'config': config})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/yolo/configs/<int:config_id>', methods=['PUT'])
def update_yolo_config(config_id):
    """Update an existing YOLO export configuration"""
    try:
        data = request.get_json()

        with get_connection() as conn:
            cursor = conn.cursor()

            # Build update fields
            updates = []
            values = []

            if 'config_name' in data:
                updates.append('config_name = %s')
                values.append(data['config_name'])
            if 'description' in data:
                updates.append('description = %s')
                values.append(data['description'])
            if 'class_mapping' in data:
                updates.append('class_mapping = %s')
                values.append(json.dumps(data['class_mapping']))
            if 'include_reviewed_only' in data:
                updates.append('include_reviewed_only = %s')
                values.append(bool(data['include_reviewed_only']))
            if 'include_ai_generated' in data:
                updates.append('include_ai_generated = %s')
                values.append(bool(data['include_ai_generated']))
            if 'include_negative_examples' in data:
                updates.append('include_negative_examples = %s')
                values.append(bool(data['include_negative_examples']))

            if not updates:
                return jsonify({'success': False, 'error': 'No fields to update'}), 400

            values.append(config_id)
            cursor.execute(
                f'UPDATE yolo_export_configs SET {", ".join(updates)} WHERE id = %s',
                values
            )

            if cursor.rowcount == 0:
                return jsonify({'success': False, 'error': 'Config not found'}), 404

            conn.commit()

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/yolo/configs/<int:config_id>/health', methods=['GET'])
def get_yolo_config_health(config_id):
    """Dataset health analysis for a YOLO export config"""
    try:
        preview = yolo_exporter.get_export_preview(config_id)

        class_counts = preview.get('class_distribution', {})
        total_annotations = preview.get('total_annotations', 0)
        total_videos = preview.get('video_count', 0)

        # Count frames (distinct timestamps across videos)
        total_frames = 0
        with get_connection() as conn:
            cursor = conn.cursor()
            config_data = None
            cursor.execute('SELECT class_mapping FROM yolo_export_configs WHERE id = %s', (config_id,))
            row = cursor.fetchone()
            if row:
                config_data = json.loads(row[0]) if row[0] else {}

            # Count distinct frames that have matching annotations
            activity_tags = list((config_data or {}).keys())
            if activity_tags:
                placeholders = ','.join(['%s'] * len(activity_tags))
                cursor.execute(f'''
                    SELECT COUNT(DISTINCT (video_id, timestamp))
                    FROM keyframe_annotations
                    WHERE activity_tag IN ({placeholders}) AND bbox_x IS NOT NULL
                ''', activity_tags)
                result = cursor.fetchone()
                total_frames = result[0] if result else 0

        # Generate warnings
        warnings = []
        recommendations = []

        if total_annotations == 0:
            warnings.append({
                'level': 'critical',
                'code': 'NO_ANNOTATIONS',
                'message': 'No annotations match this configuration. Ensure the class mapping includes activity tags that have annotations.'
            })
            recommendations.append('Check that your class mapping includes activity tags with annotations')

        if 0 < total_annotations < 10:
            warnings.append({
                'level': 'critical',
                'code': 'INSUFFICIENT_DATA',
                'message': f'Only {total_annotations} samples found — need at least 10 for train/val split. Add {10 - total_annotations} more annotated samples to proceed.'
            })

        if 10 <= total_annotations < 50:
            warnings.append({
                'level': 'critical',
                'code': 'VERY_SMALL_DATASET',
                'message': f'Only {total_annotations}/50 samples — need at least 50 to avoid overfitting. Add {50 - total_annotations} more annotated samples to proceed.'
            })
            recommendations.append('Add more annotated data for better training results')

        # Per-class warnings
        for class_name, count in class_counts.items():
            if count == 0:
                warnings.append({
                    'level': 'critical',
                    'code': 'EMPTY_CLASS',
                    'message': f'Class "{class_name}" has 0 annotations. Add annotations for this class or remove it from the config.'
                })
            elif count < 100:
                warnings.append({
                    'level': 'warning',
                    'code': 'LOW_SAMPLES_CLASS',
                    'message': f'Class "{class_name}" has only {count} samples (recommend 100+)'
                })
                recommendations.append(f'Add more "{class_name}" annotations for better accuracy')

        # Class imbalance
        if class_counts and len(class_counts) > 1:
            counts = [c for c in class_counts.values() if c > 0]
            if counts:
                max_count = max(counts)
                min_count = min(counts)
                if min_count > 0 and max_count / min_count > 5:
                    warnings.append({
                        'level': 'warning',
                        'code': 'CLASS_IMBALANCE',
                        'message': f'Significant class imbalance (ratio {max_count}:{min_count})'
                    })
                    recommendations.append('Consider balancing classes by adding more annotations to underrepresented classes')

        # Low diversity
        if total_videos == 1:
            warnings.append({
                'level': 'warning',
                'code': 'LOW_DIVERSITY',
                'message': 'All data from a single video source - low diversity'
            })
            recommendations.append('Add annotations from different video sources for better generalization')

        return jsonify({
            'success': True,
            'health': {
                'total_annotations': total_annotations,
                'total_frames': total_frames,
                'total_videos': total_videos,
                'class_counts': class_counts,
                'warnings': warnings,
                'recommendations': list(set(recommendations))
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/yolo/configs/<int:config_id>/filters', methods=['POST'])
def add_yolo_filter(config_id):
    """Add filter to YOLO export configuration"""
    try:
        data = request.get_json()
        filter_type = data.get('filter_type')
        filter_value = data.get('filter_value')
        is_exclusion = data.get('is_exclusion', False)

        if not filter_type or not filter_value:
            return jsonify({'success': False, 'error': 'Missing required fields'}), 400

        yolo_exporter.add_filter(config_id, filter_type, filter_value, is_exclusion)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/yolo/configs/<int:config_id>/preview', methods=['GET'])
def preview_yolo_export(config_id):
    """Preview YOLO export without actually exporting"""
    try:
        preview = yolo_exporter.get_export_preview(config_id)
        return jsonify({'success': True, 'preview': preview})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/yolo/configs/<int:config_id>/export', methods=['POST'])
def export_yolo_dataset(config_id):
    """Export YOLO dataset"""
    try:
        data = request.get_json() or {}
        output_name = data.get('output_name')

        result = yolo_exporter.export_dataset(config_id, output_name)
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/yolo/activity-tags', methods=['GET'])
def get_yolo_activity_tags():
    """Get all unique activity tags from annotations for YOLO export"""
    try:
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
            cursor.execute('''
                SELECT DISTINCT activity_tag, COUNT(*) as count
                FROM keyframe_annotations
                WHERE activity_tag IS NOT NULL AND activity_tag != ''
                GROUP BY activity_tag
                ORDER BY count DESC
            ''')
            tags = [{'name': row['activity_tag'], 'count': row['count']} for row in cursor.fetchall()]
        return jsonify({'success': True, 'tags': tags})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# Person Name Management Routes
@app.route('/person-manager')
def person_manager():
    """Person name management interface"""
    return render_template('person_manager.html')

@app.route('/api/person-detections', methods=['GET'])
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
                        THEN REPLACE(at.tag_value, '"person_name":', '')
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
                # Parse person name from JSON tag value
                person_name = None
                if row['person_name_json']:
                    try:
                        # Extract value from JSON string
                        import re
                        match = re.search(r'"([^"]*)"', row['person_name_json'])
                        if match:
                            person_name = match.group(1)
                    except:
                        pass

                detections.append({
                    'id': row['id'],
                    'video_id': row['video_id'],
                    'video_title': row['video_title'],
                    'timestamp': row['timestamp'],
                    'bbox_x': row['bbox_x'],
                    'bbox_y': row['bbox_y'],
                    'bbox_width': row['bbox_width'],
                    'bbox_height': row['bbox_height'],
                    'thumbnail_path': row['thumbnail_path'],
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

            # Count named vs unknown
            named_count = len([d for d in detections if d['person_name'] and d['person_name'] != 'Unknown'])
            unknown_count = len(detections) - named_count

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

@app.route('/api/person-names/recent', methods=['GET'])
def get_recent_person_names():
    """Get recently used person names"""
    try:
        limit = int(request.args.get('limit', 10))
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)

            # Get unique person names from annotation_tags
            cursor.execute('''
                SELECT DISTINCT
                    REPLACE(REPLACE(tag_value, '"person_name":', ''), '"', '') as person_name,
                    MAX(created_date) as last_used
                FROM annotation_tags
                WHERE annotation_type = 'keyframe'
                AND tag_value LIKE '%%person_name%%'
                AND tag_value NOT LIKE '%%Unknown%%'
                AND tag_value != ''
                GROUP BY REPLACE(REPLACE(tag_value, '"person_name":', ''), '"', '')
                ORDER BY last_used DESC
                LIMIT %s
            ''', (limit,))

            names = [row['person_name'] for row in cursor.fetchall() if row['person_name']]

        return jsonify({'success': True, 'names': names})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/tag-values/recent', methods=['GET'])
def get_recent_tag_values():
    """Get recently used values for any tag (make, model, fleet_id, etc.)"""
    try:
        tag_name = request.args.get('tag_name', '')
        limit = int(request.args.get('limit', 10))

        if not tag_name:
            return jsonify({'success': False, 'error': 'tag_name required'}), 400

        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)

            # Get unique values for this tag
            cursor.execute('''
                SELECT DISTINCT
                    REPLACE(REPLACE(REPLACE(tag_value, %s, ''), '"', ''), ':', '') as tag_value,
                    MAX(created_date) as last_used
                FROM annotation_tags
                WHERE annotation_type = 'keyframe'
                AND tag_value LIKE %s
                AND tag_value != ''
                GROUP BY REPLACE(REPLACE(REPLACE(tag_value, %s, ''), '"', ''), ':', '')
                ORDER BY last_used DESC
                LIMIT %s
            ''', (f'"{tag_name}"', f'%{tag_name}%', f'"{tag_name}"', limit))

            values = [row['tag_value'].strip() for row in cursor.fetchall() if row['tag_value'] and row['tag_value'].strip()]

        return jsonify({'success': True, 'values': values})
    except Exception as e:
        print(f"Error in get_recent_tag_values: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/person-detections/assign-name', methods=['POST'])
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

@app.route('/api/person-detections/unassign-name', methods=['POST'])
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

# Camera Topology Learning Routes
@app.route('/api/camera-topology/graph', methods=['GET'])
def get_camera_topology():
    """Get learned camera graph based on person transitions"""
    try:
        graph = topology_learner.build_camera_graph()
        return jsonify({'success': True, 'graph': graph})
    except Exception as e:
        print(f"Error in get_camera_topology: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/camera-topology/person/<person_name>/transitions', methods=['GET'])
def get_person_transitions(person_name):
    """Get all camera transitions for a specific person"""
    try:
        transitions = topology_learner.analyze_person_transitions(person_name)
        return jsonify({'success': True, 'transitions': transitions})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/camera-topology/person/<person_name>/path', methods=['GET'])
def get_person_path(person_name):
    """Get complete movement path for a person"""
    try:
        path = topology_learner.get_person_movement_path(person_name)
        return jsonify({'success': True, 'path': path})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/camera-topology/suggest-links', methods=['POST'])
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

@app.route('/camera-topology')
def camera_topology_viewer():
    """Camera topology visualization interface"""
    return render_template('camera_topology.html')

# ===== EcoEye Sync Endpoints =====

@app.route('/sync-settings')
def sync_settings():
    """Sync settings interface"""
    return render_template('sync_settings.html')

@app.route('/api/sync/ecoeye/config', methods=['GET'])
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

@app.route('/api/sync/ecoeye/config', methods=['POST'])
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

@app.route('/api/sync/ecoeye/test', methods=['POST'])
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

@app.route('/api/sync/ecoeye/alerts', methods=['POST'])
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

@app.route('/api/sync/ecoeye/videos', methods=['POST'])
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

@app.route('/api/sync/ecoeye/status', methods=['GET'])
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

@app.route('/api/sync/ecoeye/alerts/list', methods=['GET'])
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

# ===== UniFi Protect Endpoints =====

@app.route('/api/sync/unifi/config', methods=['GET'])
def get_unifi_config():
    """Get UniFi Protect configuration status"""
    try:
        has_credentials = sync_config.has_unifi_credentials()
        credentials = sync_config.get_unifi_credentials() if has_credentials else None

        return jsonify({
            'success': True,
            'configured': has_credentials,
            'host': credentials['host'] if credentials else None,
            'port': credentials['port'] if credentials else None
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/sync/unifi/config', methods=['POST'])
def set_unifi_config():
    """Set UniFi Protect credentials"""
    try:
        data = request.json
        host = data.get('host')
        username = data.get('username')
        password = data.get('password')
        port = data.get('port', 443)
        verify_ssl = data.get('verify_ssl', True)

        if not host or not username or not password:
            return jsonify({'success': False, 'error': 'Host, username, and password required'}), 400

        sync_config.set_unifi_credentials(host, username, password, port, verify_ssl)

        return jsonify({'success': True, 'message': 'UniFi Protect credentials saved'})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/sync/unifi/test', methods=['POST'])
def test_unifi_connection():
    """Test connection to UniFi Protect"""
    try:
        global unifi_client

        credentials = sync_config.get_unifi_credentials()
        if not credentials:
            return jsonify({'success': False, 'error': 'UniFi Protect credentials not configured'}), 400

        # Initialize client
        unifi_client = UniFiProtectClient(
            host=credentials['host'],
            port=credentials['port'],
            username=credentials['username'],
            password=credentials['password'],
            verify_ssl=credentials['verify_ssl']
        )

        # Test authentication
        success = unifi_client.authenticate()

        return jsonify({
            'success': success,
            'message': 'UniFi Protect library not yet implemented' if not success else 'Connected successfully'
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ===== Sync History Endpoints =====

@app.route('/api/sync/history', methods=['GET'])
def get_sync_history():
    """Get sync history"""
    try:
        limit = int(request.args.get('limit', 50))
        history = sync_config.get_sync_history(limit)

        return jsonify({
            'success': True,
            'history': history
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/training-queue')
def training_queue_page():
    """Training queue management interface"""
    return render_template('training_queue.html')

@app.route('/model-training')
def model_training_page():
    """Unified model training page"""
    return render_template('model_training.html')

# ===== Training Job Queue Endpoints =====

@app.route('/api/training/submit', methods=['POST'])
def submit_training_job():
    """Submit a training job: export data, upload to S3, queue via SQS"""
    try:
        data = request.get_json()
        job_type = data.get('job_type', 'yolo-training')
        config = data.get('config', {})

        export_config_id = data.get('export_config_id')
        export_path = data.get('export_path')

        if export_config_id and not export_path:
            output_name = data.get('output_name')
            result = yolo_exporter.export_dataset(export_config_id, output_name)
            if not result.get('success'):
                return jsonify({'success': False, 'error': 'Export failed', 'details': result}), 500
            export_path = result['export_path']
            config['export_stats'] = {
                'video_count': result.get('video_count'),
                'frame_count': result.get('frame_count'),
                'annotation_count': result.get('annotation_count'),
                'class_mapping': result.get('class_mapping'),
            }

        if not export_path:
            return jsonify({'success': False, 'error': 'Either export_config_id or export_path required'}), 400

        if not os.path.isdir(export_path):
            return jsonify({'success': False, 'error': f'Export path not found: {export_path}'}), 400

        result = training_queue.submit_job(
            export_path=export_path,
            job_type=job_type,
            config=config,
            export_config_id=export_config_id
        )

        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/training/jobs', methods=['GET'])
def get_training_jobs():
    """List all training jobs"""
    try:
        limit = int(request.args.get('limit', 50))
        offset = int(request.args.get('offset', 0))
        export_config_id = request.args.get('export_config_id', type=int)
        jobs = training_queue.get_jobs(limit, offset, export_config_id=export_config_id)
        return jsonify({'success': True, 'jobs': jobs})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/training/jobs/<job_id>', methods=['GET'])
def get_training_job(job_id):
    """Get single training job details"""
    try:
        job = training_queue.get_job(job_id)
        if not job:
            return jsonify({'success': False, 'error': 'Job not found'}), 404
        return jsonify({'success': True, 'job': job})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/training/jobs/<job_id>/complete', methods=['POST'])
def complete_training_job(job_id):
    """Callback endpoint for training workers to report job completion with optional metrics"""
    try:
        data = request.get_json() or {}
        result_data = data.get('result')

        success = training_queue.complete_job(job_id, result_data)
        if not success:
            return jsonify({'success': False, 'error': 'Job not found or already completed'}), 404

        # Handle training metrics if provided
        metrics = data.get('metrics')
        if metrics:
            model_name = data.get('model_name', metrics.get('model_name', ''))
            model_version = data.get('model_version', metrics.get('model_version', ''))
            if model_name:
                try:
                    # Get the training job's DB id
                    job = training_queue.get_job(job_id)
                    job_db_id = job['id'] if job else None
                    db.insert_training_metrics(job_db_id, model_name, model_version, metrics)
                    # Auto-register model if not already in registry
                    job_type = job.get('config', {}).get('job_type', 'yolo') if job and job.get('config') else 'yolo'
                    db.get_or_create_model_registry(model_name, model_version, job_type)
                except Exception as e:
                    logger.warning(f'Failed to save training metrics for job {job_id}: {e}')

        return jsonify({'success': True, 'job_id': job_id, 'status': 'completed'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/training/jobs/<job_id>/fail', methods=['POST'])
def fail_training_job(job_id):
    """Report a training job failure"""
    try:
        data = request.get_json() or {}
        error_message = data.get('error', 'Unknown error')

        success = training_queue.fail_job(job_id, error_message)
        if not success:
            return jsonify({'success': False, 'error': 'Job not found'}), 404

        return jsonify({'success': True, 'job_id': job_id, 'status': 'failed'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/training/jobs/<job_id>/cancel', methods=['POST'])
def cancel_training_job(job_id):
    """Cancel a training job"""
    try:
        success = training_queue.cancel_job(job_id)
        if not success:
            return jsonify({'success': False, 'error': 'Job not found or already completed'}), 404

        return jsonify({'success': True, 'job_id': job_id, 'status': 'cancelled'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/training/jobs/<job_id>/processing', methods=['POST'])
def set_training_job_processing(job_id):
    """Worker calls this when it picks up a job. Returns cancelled=true if job was cancelled."""
    try:
        job = training_queue.get_job(job_id)
        if not job:
            return jsonify({'success': False, 'error': 'Job not found'}), 404

        if job['status'] == 'cancelled':
            return jsonify({'success': True, 'cancelled': True, 'job_id': job_id})

        success = training_queue.set_processing(job_id)
        return jsonify({'success': success, 'cancelled': False, 'job_id': job_id})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/training/jobs/<job_id>/retry', methods=['POST'])
def retry_training_job(job_id):
    """Retry a failed or cancelled training job by creating a new job with same config."""
    job = training_queue.get_job(job_id)
    if not job:
        return jsonify({'success': False, 'error': 'Job not found'}), 404

    if job['status'] not in ('failed', 'cancelled'):
        return jsonify({'success': False, 'error': f'Can only retry failed or cancelled jobs, current status: {job["status"]}'}), 400

    config = job.get('config') or {}
    if job.get('config_json'):
        try:
            config = json.loads(job['config_json'])
        except (json.JSONDecodeError, TypeError):
            pass

    export_path = config.get('export_path', '')
    if not export_path:
        # Try to reconstruct from S3 URI or re-export
        return jsonify({'success': False, 'error': 'Cannot retry: original export path not available. Please submit a new job.'}), 400

    result = training_queue.submit_job(
        export_path=export_path,
        job_type=job['job_type'],
        config=config,
        export_config_id=job.get('export_config_id')
    )

    return jsonify({
        'success': True,
        'new_job_id': result['job_id'],
        'message': f'Retry submitted as new job {result["job_id"][:8]}...',
        'original_job_id': job_id
    })


@app.route('/api/training/jobs/<job_id>', methods=['DELETE'])
def delete_training_job(job_id):
    """Delete a training job record"""
    try:
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
            cursor.execute('DELETE FROM training_jobs WHERE job_id = %s', (job_id,))
            success = cursor.rowcount > 0
            conn.commit()
        if not success:
            return jsonify({'success': False, 'error': 'Job not found'}), 404
        return jsonify({'success': True, 'job_id': job_id})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/training/queue-status', methods=['GET'])
def get_training_queue_status():
    """Get SQS queue depth for main queue and DLQ"""
    try:
        status = training_queue.get_queue_status()
        return jsonify({'success': True, **status})
    except Exception as e:
        logger.warning(f"Queue status unavailable: {e}")
        return jsonify({'success': True, 'queue_messages': 0, 'queue_in_flight': 0, 'dlq_messages': 0, 'unavailable': True})


@app.route('/api/worker/status', methods=['GET'])
def get_worker_status():
    """Get training worker status based on recent job activity."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)

            # Check for recently active jobs (processing in last 5 minutes)
            cursor.execute('''
                SELECT COUNT(*) as active_count
                FROM training_jobs
                WHERE status = 'processing'
            ''')
            active = cursor.fetchone()['active_count']

            # Check last completed job timestamp
            cursor.execute('''
                SELECT completed_at FROM training_jobs
                WHERE status IN ('completed', 'failed')
                ORDER BY completed_at DESC LIMIT 1
            ''')
            last_row = cursor.fetchone()
            last_activity = last_row['completed_at'].isoformat() if last_row and last_row['completed_at'] else None

            # Check queue status (degrade gracefully if AWS unavailable)
            try:
                queue_status = training_queue.get_queue_status()
            except Exception:
                queue_status = {'queue_messages': 0, 'queue_in_flight': 0, 'unavailable': True}

            return jsonify({
                'success': True,
                'worker': {
                    'active_jobs': active,
                    'last_activity': last_activity,
                    'status': 'busy' if active > 0 else 'idle',
                    'queue_messages': queue_status.get('queue_messages', 0),
                    'queue_in_flight': queue_status.get('queue_in_flight', 0)
                }
            })
    except Exception as e:
        logger.error(f'Failed to get worker status: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


# ==================== Vibration Export Endpoints ====================

@app.route('/api/vibration/tags', methods=['GET'])
def get_vibration_tags():
    """Get all unique time-range tag names with counts"""
    try:
        tags = vibration_exporter.get_available_tags()
        return jsonify({'success': True, 'tags': tags})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/vibration/export', methods=['POST'])
def export_vibration_data():
    """Export vibration/time-range annotations as CSV/Parquet"""
    try:
        data = request.get_json()
        result = vibration_exporter.export_dataset(
            output_name=data.get('output_name'),
            tag_filter=data.get('tag_filter'),
            formats=data.get('formats'),
            val_split=data.get('val_split', 0.2),
            seed=data.get('seed', 42)
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ==================== One-Click Export & Train ====================

@app.route('/api/training/export-and-train', methods=['POST'])
def export_and_train():
    """One-click: export dataset then submit training job"""
    try:
        data = request.get_json()
        job_type = data.get('job_type', 'yolo')

        # Step 1: Export
        if job_type == 'yolo':
            config_id = data.get('export_config_id')
            if not config_id:
                return jsonify({'success': False, 'error': 'export_config_id required for YOLO jobs'}), 400
            export_result = yolo_exporter.export_dataset(
                config_id=int(config_id),
                output_name=data.get('output_name'),
                val_split=data.get('val_split', 0.2),
                seed=data.get('seed', 42)
            )
        elif job_type in ('bearing-fault', 'vibration'):
            export_result = vibration_exporter.export_dataset(
                output_name=data.get('output_name'),
                tag_filter=data.get('tag_filter'),
                formats=data.get('formats'),
                val_split=data.get('val_split', 0.2),
                seed=data.get('seed', 42)
            )
            job_type = 'bearing-fault'
        else:
            return jsonify({'success': False, 'error': f'Unknown job_type: {job_type}'}), 400

        if not export_result.get('success') or not export_result.get('export_path'):
            return jsonify({'success': False, 'error': 'Export produced no data',
                            'export_result': export_result}), 400

        export_path = export_result['export_path']

        # Step 2: Submit training job
        config = {
            'model_type': data.get('model_type', 'yolov8n' if job_type == 'yolo' else 'default'),
            'epochs': data.get('epochs', 100),
            'labels': data.get('labels', ''),
        }
        if job_type == 'yolo' and data.get('export_config_id'):
            config['export_config_id'] = int(data['export_config_id'])

        job = training_queue.submit_job(
            job_type=job_type,
            export_path=export_path,
            config=config,
            export_config_id=int(data['export_config_id']) if data.get('export_config_id') else None
        )

        return jsonify({
            'success': True,
            'job': job,
            'export_result': export_result,
            'message': f'Exported {export_result.get("total_samples") or export_result.get("annotation_count", 0)} samples and submitted training job'
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ===== Camera Location Endpoints =====

@app.route('/api/camera-locations', methods=['GET'])
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


@app.route('/api/camera-locations', methods=['POST'])
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
                    camera_name = COALESCE(EXCLUDED.camera_name, camera_locations.camera_name),
                    location_name = EXCLUDED.location_name,
                    location_description = COALESCE(EXCLUDED.location_description, camera_locations.location_description),
                    site_name = COALESCE(EXCLUDED.site_name, camera_locations.site_name),
                    latitude = COALESCE(EXCLUDED.latitude, camera_locations.latitude),
                    longitude = COALESCE(EXCLUDED.longitude, camera_locations.longitude),
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


@app.route('/api/camera-locations/<int:location_id>', methods=['DELETE'])
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


@app.route('/api/camera-locations/lookup/<camera_id>', methods=['GET'])
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


@app.route('/api/camera-locations/cameras', methods=['GET'])
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


@app.route('/api/camera-locations/<int:location_id>/reference-image', methods=['POST'])
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

@app.route('/api/location-export/stats', methods=['GET'])
def get_location_export_stats():
    """Get available training data statistics for location classification"""
    try:
        stats = location_exporter.get_export_stats()
        return jsonify({'success': True, 'stats': stats})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/location-export/export', methods=['POST'])
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


# ==================== AI Prediction Endpoints ====================

@app.route('/api/ai/auto-detect/<int:video_id>', methods=['POST'])
def run_auto_detect(video_id):
    """Run person/face auto-detection on a video's thumbnail."""
    try:
        video = db.get_video(video_id)
        if not video:
            return jsonify({'success': False, 'error': 'Video not found'}), 404
        if not video.get('thumbnail_path'):
            return jsonify({'success': False, 'error': 'Video has no thumbnail'}), 400

        result = run_detection_on_thumbnail(video_id, video['thumbnail_path'])
        if result is None:
            return jsonify({'success': False, 'error': 'Auto-detect failed (model not available or detection error)'}), 500

        return jsonify({'success': True, **result})
    except Exception as e:
        logger.error(f"Auto-detect endpoint error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ai/auto-detect/all', methods=['POST'])
def run_auto_detect_all():
    """Run person/face auto-detection on all videos with thumbnails (background)."""
    try:
        import threading

        def _run_all():
            videos = db.get_all_videos()
            for v in videos:
                if v.get('thumbnail_path') and os.path.exists(v['thumbnail_path']):
                    run_detection_on_thumbnail(v['id'], v['thumbnail_path'])

        thread = threading.Thread(target=_run_all, daemon=True, name="auto-detect-all")
        thread.start()
        return jsonify({'success': True, 'message': 'Auto-detect started for all videos in background'})
    except Exception as e:
        logger.error(f"Auto-detect-all endpoint error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ai/predictions/batch', methods=['POST'])
def submit_predictions_batch():
    """Submit a batch of AI predictions for routing and review"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'Request body required'}), 400

        video_id = data.get('video_id')
        model_name = data.get('model_name')
        model_version = data.get('model_version')
        predictions = data.get('predictions', [])
        batch_id = data.get('batch_id')

        if not all([video_id, model_name, model_version, predictions]):
            return jsonify({'success': False, 'error': 'video_id, model_name, model_version, and predictions required'}), 400

        # Verify video exists
        video = db.get_video(video_id)
        if not video:
            return jsonify({'success': False, 'error': f'Video {video_id} not found'}), 404

        # Ensure model is registered
        model_type = data.get('model_type', 'yolo')
        db.get_or_create_model_registry(model_name, model_version, model_type)

        # Insert predictions
        prediction_ids = db.insert_predictions_batch(
            video_id, model_name, model_version, batch_id, predictions
        )

        # Route predictions based on confidence thresholds
        routing_summary = sample_router.route_and_apply(
            prediction_ids, predictions, model_name, model_version
        )

        return jsonify({
            'success': True,
            'batch_id': batch_id,
            'predictions_submitted': len(prediction_ids),
            'prediction_ids': prediction_ids,
            'routing': routing_summary
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ai/predictions/pending', methods=['GET'])
def get_pending_predictions():
    """Get pending predictions for review"""
    try:
        video_id = request.args.get('video_id', type=int)
        model_name = request.args.get('model_name')
        limit = request.args.get('limit', 100, type=int)
        offset = request.args.get('offset', 0, type=int)

        predictions = db.get_pending_predictions(video_id, model_name, limit, offset)
        return jsonify({'success': True, 'predictions': predictions})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ai/predictions/<int:prediction_id>', methods=['GET'])
def get_prediction(prediction_id):
    """Get a single prediction by ID"""
    try:
        prediction = db.get_prediction_by_id(prediction_id)
        if not prediction:
            return jsonify({'success': False, 'error': 'Prediction not found'}), 404
        return jsonify({'success': True, 'prediction': prediction})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ai/predictions/<int:prediction_id>/review', methods=['POST'])
def review_prediction(prediction_id):
    """Approve, reject, or correct a prediction"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'Request body required'}), 400

        action = data.get('action')
        reviewer = data.get('reviewer', 'anonymous')
        notes = data.get('notes')

        if action not in ('approve', 'reject', 'correct'):
            return jsonify({'success': False, 'error': 'action must be approve, reject, or correct'}), 400

        corrections = data.get('corrections') if action == 'correct' else None

        updated = db.review_prediction(prediction_id, action, reviewer, notes, corrections)
        if not updated:
            return jsonify({'success': False, 'error': 'Prediction not found'}), 404

        annotation_id = None
        if action in ('approve', 'correct'):
            annotation_id = db.approve_prediction_to_annotation(prediction_id)
            # Update model stats
            db.update_model_approval_stats(updated['model_name'], updated['model_version'])

        return jsonify({
            'success': True,
            'prediction_id': prediction_id,
            'review_status': updated['review_status'],
            'annotation_id': annotation_id
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ai/predictions/stats', methods=['GET'])
def get_prediction_stats():
    """Get prediction queue stats (counts by status)"""
    try:
        video_id = request.args.get('video_id', type=int)
        counts = db.get_prediction_counts(video_id)
        return jsonify({'success': True, 'counts': counts})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ai/predictions/all-pending', methods=['GET'])
def get_all_pending_predictions():
    """Get all pending predictions across all videos for global review page."""
    model_filter = request.args.get('model')
    min_confidence = request.args.get('min_confidence', type=float)
    max_confidence = request.args.get('max_confidence', type=float)
    limit = min(request.args.get('limit', 100, type=int), 500)
    offset = request.args.get('offset', 0, type=int)

    try:
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)

            query = '''
                SELECT p.*, v.title as video_title, v.thumbnail_path
                FROM ai_predictions p
                JOIN videos v ON p.video_id = v.id
                WHERE p.review_status IN ('pending', 'auto_approved')
            '''
            params = []

            if model_filter:
                query += ' AND p.model_name = %s'
                params.append(model_filter)
            if min_confidence is not None:
                query += ' AND p.confidence >= %s'
                params.append(min_confidence)
            if max_confidence is not None:
                query += ' AND p.confidence <= %s'
                params.append(max_confidence)

            # Get total count
            count_query = query.replace('SELECT p.*, v.title as video_title, v.thumbnail_path', 'SELECT COUNT(*) as total')
            cursor.execute(count_query, params)
            total = cursor.fetchone()['total']

            query += ' ORDER BY p.confidence DESC, p.created_at DESC LIMIT %s OFFSET %s'
            params.extend([limit, offset])

            cursor.execute(query, params)
            predictions = [dict(row) for row in cursor.fetchall()]

            # Convert datetime objects to strings
            for pred in predictions:
                for key in ('created_at', 'reviewed_at', 'routed_at'):
                    if pred.get(key):
                        pred[key] = pred[key].isoformat()
                # Parse JSONB fields that come back as strings
                for key in ('predicted_tags', 'bbox', 'metadata'):
                    if isinstance(pred.get(key), str):
                        try:
                            pred[key] = json.loads(pred[key])
                        except (json.JSONDecodeError, TypeError):
                            pass

            # Also get thumbnail paths as URL-friendly
            for pred in predictions:
                if pred.get('thumbnail_path'):
                    thumb_name = os.path.basename(pred['thumbnail_path'])
                    pred['thumbnail_url'] = f'/thumbnails/{thumb_name}'
                else:
                    pred['thumbnail_url'] = None

            return jsonify({
                'success': True,
                'predictions': predictions,
                'total': total,
                'limit': limit,
                'offset': offset
            })
    except Exception as e:
        logger.error(f'Failed to get all pending predictions: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


# ==================== Model Management Endpoints ====================

@app.route('/api/ai/models', methods=['GET'])
def get_registered_models():
    """List all registered models with stats"""
    try:
        model_name = request.args.get('model_name')
        active_only = request.args.get('active_only', 'true').lower() == 'true'
        models = db.get_model_registry(model_name, active_only)
        return jsonify({'success': True, 'models': models})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ai/models/<model_name>/stats', methods=['GET'])
def get_model_performance_stats(model_name):
    """Get model performance and prediction stats"""
    try:
        model_version = request.args.get('model_version')
        stats = db.get_model_stats(model_name, model_version)
        return jsonify({'success': True, 'model_name': model_name, 'stats': stats})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ai/models/<model_name>/thresholds', methods=['PUT'])
def update_model_thresholds(model_name):
    """Update routing confidence thresholds for a model"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'Request body required'}), 400

        model_version = data.get('model_version')
        thresholds = data.get('thresholds')

        if not model_version or not thresholds:
            return jsonify({'success': False, 'error': 'model_version and thresholds required'}), 400

        # Validate threshold values
        for key in ('auto_approve', 'review', 'auto_reject'):
            if key in thresholds:
                val = thresholds[key]
                if not isinstance(val, (int, float)) or val < 0 or val > 1:
                    return jsonify({'success': False, 'error': f'{key} must be between 0.0 and 1.0'}), 400

        updated = db.update_model_thresholds(model_name, model_version, thresholds)
        if not updated:
            return jsonify({'success': False, 'error': 'Model not found'}), 404

        return jsonify({'success': True, 'model': updated})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ai/models/<model_name>/metrics', methods=['GET'])
def get_model_metrics_history(model_name):
    """Get training metrics history for a model"""
    try:
        model_version = request.args.get('model_version')
        limit = request.args.get('limit', 20, type=int)
        metrics = db.get_training_metrics_history(model_name, model_version, limit)
        return jsonify({'success': True, 'model_name': model_name, 'metrics': metrics})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ai/models/<model_name>/toggle', methods=['POST'])
def toggle_model(model_name):
    """Activate or deactivate a model."""
    data = request.json or {}
    model_version = data.get('model_version')
    active = data.get('active')  # True/False

    if active is None:
        return jsonify({'success': False, 'error': 'active field required (true/false)'}), 400

    try:
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
            if model_version:
                cursor.execute('''
                    UPDATE model_registry SET is_active = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE model_name = %s AND model_version = %s
                ''', (active, model_name, model_version))
            else:
                cursor.execute('''
                    UPDATE model_registry SET is_active = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE model_name = %s
                ''', (active, model_name))

            if cursor.rowcount == 0:
                return jsonify({'success': False, 'error': 'Model not found'}), 404

            conn.commit()

        return jsonify({
            'success': True,
            'model_name': model_name,
            'active': active,
            'message': f'Model {"activated" if active else "deactivated"}'
        })
    except Exception as e:
        logger.error(f'Failed to toggle model {model_name}: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


# ==================== Location Export & Train Endpoint ====================

@app.route('/api/location-export/export-and-train', methods=['POST'])
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


# ── Multi-Entity Detection: Identities ────────────────────────────

@app.route('/api/identities', methods=['GET'])
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

@app.route('/api/identities', methods=['POST'])
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

@app.route('/api/identities/<identity_id>', methods=['GET'])
def get_identity(identity_id):
    """Get a single identity by ID"""
    try:
        identity = db.get_identity(identity_id)
        if not identity:
            return jsonify({'success': False, 'error': 'Identity not found'}), 404
        return jsonify({'success': True, 'identity': identity})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/identities/<identity_id>', methods=['PUT'])
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

@app.route('/api/identities/<identity_id>', methods=['DELETE'])
def delete_identity(identity_id):
    """Delete an identity"""
    try:
        deleted = db.delete_identity(identity_id)
        if not deleted:
            return jsonify({'success': False, 'error': 'Identity not found'}), 404
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/identities/<identity_id>/associations', methods=['GET'])
def get_identity_associations(identity_id):
    """Get associations and full chain for an identity"""
    try:
        association_type = request.args.get('type')
        associations = db.get_associations(identity_id, association_type=association_type)
        chain = db.get_association_chain(identity_id)
        return jsonify({'success': True, 'associations': associations, 'chain': chain})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/identities/<identity_id>/embeddings', methods=['GET'])
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

@app.route('/api/identities/<identity_id>/timeline', methods=['GET'])
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

@app.route('/api/embeddings', methods=['POST'])
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

@app.route('/api/embeddings/search', methods=['POST'])
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

@app.route('/api/embeddings/<embedding_id>', methods=['DELETE'])
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

@app.route('/api/tracks/active', methods=['GET'])
def get_active_tracks():
    """Get currently active tracks (defined BEFORE /api/tracks/<track_id> to avoid route conflict)"""
    try:
        camera_id = request.args.get('camera_id')
        tracks = db.get_active_tracks(camera_id=camera_id)
        return jsonify({'success': True, 'tracks': tracks})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/tracks', methods=['GET'])
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

@app.route('/api/tracks', methods=['POST'])
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

@app.route('/api/tracks/<track_id>', methods=['GET'])
def get_track(track_id):
    """Get a single track by ID"""
    try:
        track = db.get_track(track_id)
        if not track:
            return jsonify({'success': False, 'error': 'Track not found'}), 404
        return jsonify({'success': True, 'track': track})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/tracks/<track_id>/end', methods=['POST'])
def end_track(track_id):
    """End a track (set ended_at timestamp)"""
    try:
        ended = db.end_track(track_id)
        if not ended:
            return jsonify({'success': False, 'error': 'Track not found'}), 404
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/tracks/<track_id>/link', methods=['POST'])
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

@app.route('/api/tracks/<track_id>/sightings', methods=['GET'])
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

@app.route('/api/sightings/batch', methods=['POST'])
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

@app.route('/api/violations', methods=['GET'])
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

@app.route('/api/violations', methods=['POST'])
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

@app.route('/api/violations/<violation_id>/review', methods=['POST'])
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

@app.route('/api/visits', methods=['GET'])
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

@app.route('/api/visits', methods=['POST'])
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

@app.route('/api/visits/<visit_id>', methods=['GET'])
def get_visit(visit_id):
    """Get a single visit by ID"""
    try:
        visit = db.get_visit(visit_id)
        if not visit:
            return jsonify({'success': False, 'error': 'Visit not found'}), 404
        return jsonify({'success': True, 'visit': visit})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/visits/<visit_id>/end', methods=['POST'])
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

@app.route('/api/visits/<visit_id>/violation', methods=['POST'])
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

@app.route('/api/pipeline/status', methods=['GET'])
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

@app.route('/api/face-clusters', methods=['GET'])
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


@app.route('/api/face-clusters/run', methods=['POST'])
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


@app.route('/api/face-clusters/<identity_id>/assign', methods=['POST'])
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


@app.route('/api/face-clusters/<source_id>/merge/<target_id>', methods=['POST'])
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


# Support for reverse proxy
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5050, threaded=True)
