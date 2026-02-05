from flask import Flask, request, jsonify, send_from_directory, render_template
from werkzeug.utils import secure_filename
import os
import json
import atexit
from pathlib import Path
from database import VideoDatabase
from db_connection import init_connection_pool, close_connection_pool
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

app = Flask(__name__,
            template_folder='../templates',
            static_folder='../static')
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024 * 1024  # 2GB max upload

BASE_DIR = Path(__file__).parent.parent
DOWNLOAD_DIR = BASE_DIR / 'downloads'
THUMBNAIL_DIR = BASE_DIR / 'thumbnails'
EXPORT_DIR = BASE_DIR / 'exports'

# Initialize PostgreSQL connection pool
init_connection_pool()
atexit.register(close_connection_pool)

# EcoEye API Configuration
ECOEYE_API_BASE = 'https://alert.ecoeyetech.com'
ECOEYE_API_KEY = '-3tsV7gFLF-nxAAUt-zRETAJLWEyxEWszwdT4fCKpeI'

def ecoeye_request(method, endpoint, **kwargs):
    """Make authenticated request to EcoEye API"""
    import requests
    headers = kwargs.pop('headers', {})
    headers['X-API-KEY'] = ECOEYE_API_KEY
    url = f"{ECOEYE_API_BASE}/{endpoint}"
    return requests.request(method, url, headers=headers, **kwargs)

db = VideoDatabase()  # Uses DATABASE_URL from environment
downloader = VideoDownloader(str(DOWNLOAD_DIR))
processor = VideoProcessor(str(THUMBNAIL_DIR))
download_queue = DownloadQueue(DOWNLOAD_DIR, THUMBNAIL_DIR, db)
yolo_exporter = YOLOExporter(db, DOWNLOAD_DIR, EXPORT_DIR)
vibration_exporter = VibrationExporter(db, EXPORT_DIR)
topology_learner = CameraTopologyLearner()  # Uses DATABASE_URL from environment

# Initialize sync components
sync_config = SyncConfigManager()  # Uses DATABASE_URL from environment
ecoeye_client = None  # Initialized on-demand with credentials
unifi_client = None  # Initialized on-demand with credentials

# Initialize training queue
init_training_jobs_table(db)
training_queue = TrainingQueueClient(db)

ALLOWED_EXTENSIONS = {'mp4', 'avi', 'mov', 'mkv', 'webm'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/')
def index():
    return render_template('index.html')

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
            with db.get_connection() as conn:
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
    with db.get_connection() as conn:
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
            video_url = f'https://alert.ecoeyetech.com/videos/{video_filename}'

            dl_result = downloader.download(video_url, event.get('camera_name', 'ecoeye'))

            if dl_result.get('success'):
                video_id = db.add_video(
                    filename=dl_result['filename'],
                    title=f"{event.get('camera_name', 'Unknown')} - {event.get('event_type', 'event')}",
                    original_url=video_url,
                    notes=notes
                )
                processor.generate_thumbnail(str(DOWNLOAD_DIR / dl_result['filename']), video_id)

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
                notes=notes + "\n[Metadata only - no video file]"
            )
            print(f"[EcoEye Sync] Created record_id: {record_id}")

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
    import requests

    data = request.json
    event_id = data.get('event_id')

    if not event_id:
        return jsonify({'success': False, 'error': 'event_id required'}), 400

    try:
        # Send retry request to EcoEye relay
        resp = ecoeye_request('POST', f'api/events/{event_id}/retry',
            timeout=30
        )

        if resp.status_code == 200:
            return jsonify({
                'success': True,
                'message': 'Download request sent to EcoEye relay'
            })
        elif resp.status_code == 404:
            # API endpoint not implemented yet
            return jsonify({
                'success': False,
                'error': 'Video download request feature not yet available on relay'
            }), 501  # Not Implemented
        else:
            return jsonify({
                'success': False,
                'error': f'Relay returned status {resp.status_code}'
            }), 500

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/ecoeye/cameras', methods=['GET'])
def get_ecoeye_cameras():
    """Get list of cameras from EcoEye for filter dropdown"""
    try:
        # Use dedicated filters API for fast lookup
        resp = ecoeye_request('GET', 'api-filters.php',
            params={'type': 'cameras'},
            timeout=15
        )
        result = resp.json()

        if result.get('success'):
            cameras = result.get('cameras', [])
            print(f"[EcoEye Cameras] Found {len(cameras)} cameras")
            return jsonify({'success': True, 'cameras': cameras})

        print(f"[EcoEye Cameras] Failed to fetch: {result}")
        return jsonify({'success': False, 'error': 'Failed to fetch cameras'}), 500
    except Exception as e:
        print(f"[EcoEye Cameras] Error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/ecoeye/sites', methods=['GET'])
def get_ecoeye_sites():
    """Get list of sites from EcoEye for filter dropdown"""
    try:
        # Use dedicated filters API for fast lookup
        resp = ecoeye_request('GET', 'api-filters.php',
            params={'type': 'sites'},
            timeout=15
        )
        result = resp.json()

        if result.get('success'):
            sites = result.get('sites', [])
            print(f"[EcoEye Sites] Found {len(sites)} sites")
            return jsonify({'success': True, 'sites': sites})

        print(f"[EcoEye Sites] Failed to fetch: {result}")
        return jsonify({'success': False, 'error': 'Failed to fetch sites'}), 500
    except Exception as e:
        print(f"[EcoEye Sites] Error: {e}")
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
        videos = db.get_all_videos(limit, offset)

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

    return jsonify({'success': True, 'videos': videos})

@app.route('/api/videos/<int:video_id>', methods=['GET'])
def get_video(video_id):
    """Get single video details"""
    video = db.get_video(video_id)
    if not video:
        return jsonify({'success': False, 'error': 'Video not found'}), 404

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
            with db.get_connection() as conn:
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
            include_reviewed_only=data.get('include_reviewed_only', 0),
            include_ai_generated=data.get('include_ai_generated', 1),
            include_negative_examples=data.get('include_negative_examples', 1),
            min_confidence=data.get('min_confidence', 0.0)
        )

        return jsonify({'success': True, 'config_id': config_id})
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
        with db.get_connection() as conn:
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
        with db.get_connection() as conn:
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
        with db.get_connection() as conn:
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

        with db.get_connection() as conn:
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

        with db.get_connection() as conn:
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

        with db.get_connection() as conn:
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
            'base_url': 'https://alerts.ecoeyetech.com'
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

        return jsonify({'success': True, 'message': 'EcoEye credentials saved'})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/sync/ecoeye/test', methods=['POST'])
def test_ecoeye_connection():
    """Test connection to EcoEye API"""
    try:
        global ecoeye_client

        credentials = sync_config.get_ecoeye_credentials()
        if not credentials:
            return jsonify({'success': False, 'error': 'EcoEye credentials not configured'}), 400

        # Initialize client with credentials
        ecoeye_client = EcoEyeSyncClient(
            download_dir=DOWNLOAD_DIR,
            api_key=credentials['api_key'],
            api_secret=credentials['api_secret']
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

        credentials = sync_config.get_ecoeye_credentials()
        if not credentials:
            return jsonify({'success': False, 'error': 'EcoEye credentials not configured'}), 400

        # Initialize client if not already done
        if not ecoeye_client:
            ecoeye_client = EcoEyeSyncClient(
                download_dir=DOWNLOAD_DIR,
                api_key=credentials['api_key'],
                api_secret=credentials['api_secret']
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

        credentials = sync_config.get_ecoeye_credentials()
        if not credentials:
            return jsonify({'success': False, 'error': 'EcoEye credentials not configured'}), 400

        # Initialize client if not already done
        if not ecoeye_client:
            ecoeye_client = EcoEyeSyncClient(
                download_dir=DOWNLOAD_DIR,
                api_key=credentials['api_key'],
                api_secret=credentials['api_secret']
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

        credentials = sync_config.get_ecoeye_credentials()
        if not credentials:
            return jsonify({'success': False, 'error': 'EcoEye credentials not configured'}), 400

        # Initialize client if not already done
        if not ecoeye_client:
            ecoeye_client = EcoEyeSyncClient(
                download_dir=DOWNLOAD_DIR,
                api_key=credentials['api_key'],
                api_secret=credentials['api_secret']
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

        with db.get_connection() as conn:
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
        jobs = training_queue.get_jobs(limit, offset)
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
    """Callback endpoint for training workers to report job completion"""
    try:
        data = request.get_json() or {}
        result_data = data.get('result')

        success = training_queue.complete_job(job_id, result_data)
        if not success:
            return jsonify({'success': False, 'error': 'Job not found or already completed'}), 404

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


@app.route('/api/training/jobs/<job_id>', methods=['DELETE'])
def delete_training_job(job_id):
    """Delete a training job record"""
    try:
        with db.get_connection() as conn:
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


# Support for reverse proxy
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5050)
