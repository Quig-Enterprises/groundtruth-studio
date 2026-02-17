from flask import Blueprint, request, jsonify, render_template, send_from_directory, g
from werkzeug.utils import secure_filename
from pathlib import Path
from psycopg2 import extras
from db_connection import get_connection, get_cursor
from vehicle_detect_runner import trigger_vehicle_detect
from services import (
    db, downloader, processor, download_queue,
    allowed_file, DOWNLOAD_DIR, THUMBNAIL_DIR, ALLOWED_EXTENSIONS, BASE_DIR,
)
import os
import json
import logging
import time

videos_bp = Blueprint('videos', __name__)
logger = logging.getLogger(__name__)

CLIPS_DIR = BASE_DIR / 'clips'
CLIPS_DIR.mkdir(exist_ok=True)

# Legacy path prefix that may exist in old database records
_LEGACY_PREFIXES = ['/var/www/html/groundtruth-studio/downloads/', '/var/www/html/groundtruth-studio/thumbnails/']

def _resolve_video_path(filename):
    """Resolve a filename from the DB to an actual disk path and normalized name.
    Handles absolute legacy paths, new absolute paths, and relative filenames.
    Returns (disk_path: Path, normalized_name: str)."""
    if not filename:
        return None, filename
    # Strip legacy absolute prefixes to just the basename
    stripped = False
    for prefix in _LEGACY_PREFIXES:
        if filename.startswith(prefix):
            filename = filename[len(prefix):]
            stripped = True
            break
    if not stripped and filename.startswith(str(DOWNLOAD_DIR) + '/'):
        filename = filename[len(str(DOWNLOAD_DIR)) + 1:]
    return DOWNLOAD_DIR / filename, filename


@videos_bp.route('/api/client-log', methods=['POST'])
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


@videos_bp.route('/add-content')
def add_content():
    return render_template('add_content.html')


@videos_bp.route('/annotate')
def annotate():
    return render_template('annotate.html')


@videos_bp.route('/api/videos', methods=['GET'])
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
        # Normalize legacy absolute paths
        if has_video_file and filename:
            _, normalized = _resolve_video_path(filename)
            video['filename'] = normalized

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

@videos_bp.route('/api/libraries', methods=['GET'])
def get_libraries():
    """Get all content libraries with item counts"""
    libraries = db.get_all_libraries()
    return jsonify({'success': True, 'libraries': libraries})

@videos_bp.route('/api/libraries', methods=['POST'])
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

@videos_bp.route('/api/libraries/<int:library_id>', methods=['PUT'])
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

@videos_bp.route('/api/libraries/<int:library_id>', methods=['DELETE'])
def delete_library(library_id):
    """Delete a content library (not the default)"""
    success = db.delete_library(library_id)
    if success:
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': 'Cannot delete default library'}), 400

@videos_bp.route('/api/libraries/<int:library_id>/items', methods=['POST'])
def add_library_items(library_id):
    """Add videos to a library"""
    data = request.get_json()
    video_ids = data.get('video_ids', [])
    if not video_ids:
        return jsonify({'success': False, 'error': 'No video IDs provided'}), 400
    added = db.add_to_library(library_id, video_ids)
    return jsonify({'success': True, 'added': added})

@videos_bp.route('/api/libraries/<int:library_id>/items/<int:video_id>', methods=['DELETE'])
def remove_library_item(library_id, video_id):
    """Remove a video from a library"""
    success = db.remove_from_library(library_id, video_id)
    return jsonify({'success': True, 'removed': success})

@videos_bp.route('/api/libraries/<int:library_id>/next-unannotated', methods=['GET'])
def get_next_unannotated(library_id):
    """Get the next unannotated video in a library"""
    current_video_id = request.args.get('current')
    current_video_id = int(current_video_id) if current_video_id else None
    video = db.get_next_unannotated_in_library(library_id, current_video_id)
    if video:
        return jsonify({'success': True, 'video': video})
    return jsonify({'success': True, 'video': None, 'message': 'All videos in this library are annotated'})

@videos_bp.route('/api/next-unannotated', methods=['GET'])
def get_next_unannotated_global():
    """Get the next unannotated video globally (no library filter)"""
    current_video_id = request.args.get('current')
    current_video_id = int(current_video_id) if current_video_id else None
    video = db.get_next_unannotated(current_video_id)
    if video:
        return jsonify({'success': True, 'video': video})
    return jsonify({'success': True, 'video': None, 'message': 'All videos are annotated'})

@videos_bp.route('/api/videos/<int:video_id>', methods=['GET'])
def get_video(video_id):
    """Get single video details"""
    video = db.get_video(video_id)
    if not video:
        return jsonify({'success': False, 'error': 'Video not found'}), 404

    # Add has_video_file flag (normalize legacy absolute paths)
    filename = video.get('filename', '') or ''
    has_video_file = bool(filename and not filename.endswith('.placeholder'))
    if has_video_file:
        video_path, normalized = _resolve_video_path(filename)
        has_video_file = video_path.exists() if video_path else False
        video['filename'] = normalized
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

@videos_bp.route('/api/videos/<int:video_id>', methods=['DELETE'])
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

@videos_bp.route('/api/download', methods=['POST'])
def download_video():
    """Add video URL to download queue"""
    data = request.get_json()
    url = data.get('url')

    if not url:
        return jsonify({'success': False, 'error': 'URL required'}), 400

    # Add to queue (handles duplicate detection)
    result = download_queue.add_to_queue(url)

    return jsonify(result)

@videos_bp.route('/api/download/status', methods=['GET'])
def download_status():
    """Get download queue status"""
    status = download_queue.get_queue_status()
    return jsonify(status)

@videos_bp.route('/api/video-info', methods=['POST'])
def get_video_info():
    """Get video info without downloading"""
    data = request.get_json()
    url = data.get('url')

    if not url:
        return jsonify({'success': False, 'error': 'URL required'}), 400

    result = downloader.get_video_info(url)
    return jsonify(result)

@videos_bp.route('/api/upload', methods=['POST'])
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

    # YOLO-World pre-screen: detects vehicles + gates person-face-v1
    if thumbnail_path:
        trigger_vehicle_detect(video_id, thumbnail_path)

    return jsonify({
        'success': True,
        'video_id': video_id,
        'filename': filename
    })

@videos_bp.route('/api/videos/<int:video_id>/tags', methods=['POST'])
def add_tag(video_id):
    """Add tag to video"""
    data = request.get_json()
    tag = data.get('tag', '').strip()

    if not tag:
        return jsonify({'success': False, 'error': 'Tag required'}), 400

    success = db.tag_video(video_id, tag)
    return jsonify({'success': success})

@videos_bp.route('/api/videos/<int:video_id>/tags/<tag_name>', methods=['DELETE'])
def remove_tag(video_id, tag_name):
    """Remove tag from video"""
    success = db.untag_video(video_id, tag_name)
    return jsonify({'success': success})

@videos_bp.route('/api/tags', methods=['GET'])
def get_tags():
    """Get all tags"""
    tags = db.get_all_tags()
    return jsonify({'success': True, 'tags': tags})

@videos_bp.route('/api/videos/<int:video_id>/behaviors', methods=['POST'])
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

@videos_bp.route('/downloads/<path:filename>')
def serve_video(filename):
    """Serve video file"""
    return send_from_directory(DOWNLOAD_DIR, filename)

@videos_bp.route('/thumbnails/<path:filename>')
def serve_thumbnail(filename):
    """Serve thumbnail file"""
    return send_from_directory(THUMBNAIL_DIR, filename)

@videos_bp.route('/clips/<path:filename>')
def serve_clip(filename):
    """Serve cached video clip"""
    return send_from_directory(CLIPS_DIR, filename)

@videos_bp.route('/api/clips/cleanup', methods=['POST'])
def cleanup_clips():
    """Clean up cached video clips based on age and size limits."""
    if not g.can_write:
        return jsonify({'success': False, 'error': 'Read-only mode'}), 403
    max_age = request.json.get('max_age_days', 7) if request.is_json else 7
    max_size = request.json.get('max_size_mb', 500) if request.is_json else 500
    result = processor.cleanup_clips(max_age_days=max_age, max_size_mb=max_size)
    return jsonify(result)

@videos_bp.route('/api/clips/stats', methods=['GET'])
def clips_stats():
    """Get clip cache statistics."""
    clips_dir = CLIPS_DIR
    if not clips_dir.exists():
        return jsonify({'count': 0, 'total_mb': 0, 'oldest_days': 0})
    import time
    clips = list(clips_dir.glob('*.mp4'))
    total = sum(f.stat().st_size for f in clips) if clips else 0
    oldest_age = 0
    if clips:
        oldest_mtime = min(f.stat().st_mtime for f in clips)
        oldest_age = round((time.time() - oldest_mtime) / 86400, 1)
    return jsonify({
        'count': len(clips),
        'total_mb': round(total / (1024 * 1024), 1),
        'oldest_days': oldest_age
    })

@videos_bp.route('/api/system/status', methods=['GET'])
def system_status():
    """Check system dependencies"""
    return jsonify({
        'success': True,
        'yt_dlp_installed': downloader.check_yt_dlp_installed(),
        'ffmpeg_installed': processor.check_ffmpeg_installed()
    })
