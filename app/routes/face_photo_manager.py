"""Face Photo Manager routes for Groundtruth Studio.

Manages AI-generated face photos used in document synthesis training data.
Provides browsing, uploading, deletion, and background-removal preprocessing.
"""
import logging
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

from flask import Blueprint, request, jsonify, render_template, send_file

sys.path.insert(0, str(Path("/mnt/storage/training-material/documents/synthesizer")))
from profile_store import (
    load_profile, save_profile, get_or_create_profile,
    delete_profile, list_profiles, update_profile_identity, bulk_stats
)
from gender_detector import (
    detect_and_store_gender, detect_all_missing, ensure_models,
    read_gender_meta, write_gender_meta,
)

face_photo_manager_bp = Blueprint('face_photo_manager', __name__)
logger = logging.getLogger(__name__)

FACES_DIR = Path("/mnt/storage/training-material/documents/synthesizer/faces")
FACES_WHITE_DIR = Path("/mnt/storage/training-material/documents/synthesizer/faces_white")
FLUXSYNID_DIR = Path("/mnt/storage/training-material/documents/synthesizer/face-generators/FLUXSynID")
PREPROCESS_SCRIPT = Path("/mnt/storage/training-material/documents/synthesizer/preprocess_faces.py")
PYTHON_BIN = "/opt/groundtruth-studio/venv/bin/python"

# Filename pattern: face_NNNNN.png
FACE_RE = re.compile(r'^face_(\d+)\.png$')

# ── Background preprocessing state ──────────────────────────────────────
_preprocess_lock = threading.Lock()
_preprocess_state = {
    'running': False,
    'process': None,      # subprocess.Popen reference
    'total': 0,
    'progress': 0,
    'thread': None,
}


# ── Helpers ─────────────────────────────────────────────────────────────

def _list_faces(directory: Path) -> list[str]:
    """Return sorted list of face PNG filenames in *directory*."""
    if not directory.is_dir():
        return []
    return sorted(
        f for f in os.listdir(directory)
        if f.lower().endswith('.png')
    )


def _next_face_number() -> int:
    """Return the next available face number (max existing + 1)."""
    highest = -1
    for fname in os.listdir(FACES_DIR) if FACES_DIR.is_dir() else []:
        m = FACE_RE.match(fname)
        if m:
            highest = max(highest, int(m.group(1)))
    return highest + 1


def _safe_filename(filename: str) -> str:
    """Sanitize a filename to its basename only."""
    return os.path.basename(filename)


# ── Page Route ──────────────────────────────────────────────────────────

@face_photo_manager_bp.route('/face-photo-manager')
def face_photo_manager_page():
    return render_template('face_photo_manager.html')


# ── API: Stats ──────────────────────────────────────────────────────────

@face_photo_manager_bp.route('/api/faces/stats')
def face_stats():
    """Return face photo counts and FLUXSynID setup status."""
    try:
        originals = _list_faces(FACES_DIR)
        whites = _list_faces(FACES_WHITE_DIR)
        whites_set = set(whites)

        unprocessed = sum(1 for f in originals if f not in whites_set)

        # FLUXSynID: exists and has a venv directory
        fluxsynid_exists = FLUXSYNID_DIR.is_dir()
        fluxsynid_venv = (FLUXSYNID_DIR / 'venv').is_dir()

        # Profile stats
        try:
            pstats = bulk_stats()
            profile_count = pstats.get('total_profiles', 0)
        except Exception:
            profile_count = 0

        return jsonify({
            'success': True,
            'originals': len(originals),
            'preprocessed': len(whites),
            'unprocessed': unprocessed,
            'profiles': profile_count,
            'fluxsynid': {
                'installed': fluxsynid_exists,
                'venv_ready': fluxsynid_venv,
            },
        })
    except Exception as e:
        logger.error(f"Error computing face stats: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ── API: Paginated Listing ──────────────────────────────────────────────

@face_photo_manager_bp.route('/api/faces/items')
def face_items():
    """Paginated listing of face photos.

    Query params:
        page      - page number (default 1)
        per_page  - items per page (default 60)
        variant   - 'original', 'white', or 'both' (default 'white')
    """
    try:
        page = max(1, int(request.args.get('page', 1)))
        per_page = max(1, min(200, int(request.args.get('per_page', 60))))
        variant = request.args.get('variant', 'white')

        originals = set(_list_faces(FACES_DIR))
        whites = set(_list_faces(FACES_WHITE_DIR))

        if variant == 'original':
            all_filenames = sorted(originals)
        elif variant == 'white':
            all_filenames = sorted(whites)
        else:
            # 'both' — union of both sets, showing pair info
            all_filenames = sorted(originals | whites)

        total = len(all_filenames)
        total_pages = max(1, (total + per_page - 1) // per_page)
        start = (page - 1) * per_page
        page_filenames = all_filenames[start:start + per_page]

        items = []
        for fname in page_filenames:
            has_original = fname in originals
            has_white = fname in whites

            # Determine the image URL based on variant preference
            if variant == 'original':
                image_url = f'/api/faces/image/original/{fname}'
            elif variant == 'white':
                image_url = f'/api/faces/image/white/{fname}'
            else:
                # 'both' — prefer white if available, otherwise original
                image_url = (f'/api/faces/image/white/{fname}' if has_white
                             else f'/api/faces/image/original/{fname}')

            item = {
                'id': fname,
                'filename': fname,
                'image_url': image_url,
                'has_original': has_original,
                'has_white': has_white,
                'detected_gender': read_gender_meta(fname),
            }

            # Add profile info if available
            profile = load_profile(fname)
            if profile:
                identity = profile.get('identity', {})
                item['has_profile'] = True
                item['profile_name'] = f"{identity.get('first_name', '')} {identity.get('last_name', '')}"
                item['generation_count'] = profile.get('generation_count', 0)
                item['doc_types'] = list(profile.get('documents', {}).keys())
            else:
                item['has_profile'] = False
                item['profile_name'] = None
                item['generation_count'] = 0
                item['doc_types'] = []

            # For 'both' variant, also include URLs for both versions
            if variant == 'both':
                if has_original:
                    item['original_url'] = f'/api/faces/image/original/{fname}'
                if has_white:
                    item['white_url'] = f'/api/faces/image/white/{fname}'

            items.append(item)

        return jsonify({
            'success': True,
            'items': items,
            'total': total,
            'page': page,
            'pages': total_pages,
        })
    except Exception as e:
        logger.error(f"Error loading face items: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ── API: Serve Image ────────────────────────────────────────────────────

@face_photo_manager_bp.route('/api/faces/image/<variant>/<filename>')
def face_image(variant, filename):
    """Serve a face image file with long cache headers.

    variant: 'original' or 'white'
    """
    safe_name = _safe_filename(filename)

    if variant == 'original':
        image_path = FACES_DIR / safe_name
    elif variant == 'white':
        image_path = FACES_WHITE_DIR / safe_name
    else:
        return jsonify({'error': 'Invalid variant. Use "original" or "white".'}), 400

    if not image_path.exists():
        return jsonify({'error': 'Image not found'}), 404

    response = send_file(
        str(image_path),
        mimetype='image/png',
    )
    response.headers['Cache-Control'] = 'public, max-age=604800'  # 7 days
    return response


# ── API: Upload ─────────────────────────────────────────────────────────

@face_photo_manager_bp.route('/api/faces/upload', methods=['POST'])
def face_upload():
    """Upload new face photos to the originals directory.

    Accepts multipart form with multiple files. Each file is renamed
    to the next available face_NNNNN.png number.
    """
    try:
        files = request.files.getlist('files')
        if not files or all(f.filename == '' for f in files):
            return jsonify({'success': False, 'error': 'No files provided'}), 400

        FACES_DIR.mkdir(parents=True, exist_ok=True)
        next_num = _next_face_number()
        saved = []

        for f in files:
            if not f.filename:
                continue

            # Validate it's an image
            ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else ''
            if ext not in ('png', 'jpg', 'jpeg'):
                logger.warning(f"Skipping non-image upload: {f.filename}")
                continue

            new_name = f'face_{next_num:05d}.png'
            dest = FACES_DIR / new_name

            # Save — convert to PNG if needed
            if ext in ('jpg', 'jpeg'):
                from PIL import Image
                img = Image.open(f.stream).convert('RGB')
                img.save(str(dest), 'PNG')
            else:
                f.save(str(dest))

            saved.append(new_name)
            next_num += 1

        logger.info(f"Uploaded {len(saved)} face photos: {saved}")
        return jsonify({
            'success': True,
            'count': len(saved),
            'filenames': saved,
        })
    except Exception as e:
        logger.error(f"Face upload failed: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ── API: Delete ─────────────────────────────────────────────────────────

@face_photo_manager_bp.route('/api/faces/<filename>', methods=['DELETE'])
def face_delete(filename):
    """Delete a face from both original and white directories."""
    try:
        safe_name = _safe_filename(filename)
        deleted_original = False
        deleted_white = False

        original_path = FACES_DIR / safe_name
        white_path = FACES_WHITE_DIR / safe_name

        if original_path.exists():
            original_path.unlink()
            deleted_original = True

        if white_path.exists():
            white_path.unlink()
            deleted_white = True

        # Also delete associated profile
        try:
            delete_profile(safe_name)
        except Exception as e:
            logger.warning(f"Failed to delete profile for {safe_name}: {e}")

        if not deleted_original and not deleted_white:
            return jsonify({'success': False, 'error': 'File not found in either directory'}), 404

        logger.info(f"Deleted face {safe_name}: original={deleted_original}, white={deleted_white}")
        return jsonify({
            'success': True,
            'deleted_original': deleted_original,
            'deleted_white': deleted_white,
        })
    except Exception as e:
        logger.error(f"Face delete failed for {filename}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ── API: Bulk Delete ───────────────────────────────────────────────────

@face_photo_manager_bp.route('/api/faces/delete', methods=['POST'])
def face_bulk_delete():
    """Delete multiple faces from both original and white directories."""
    try:
        data = request.get_json() or {}
        ids = data.get('ids', [])
        if not ids:
            return jsonify({'success': False, 'error': 'No ids provided'}), 400

        deleted = 0
        for filename in ids:
            safe_name = _safe_filename(filename)
            original_path = FACES_DIR / safe_name
            white_path = FACES_WHITE_DIR / safe_name
            removed = False
            if original_path.exists():
                original_path.unlink()
                removed = True
            if white_path.exists():
                white_path.unlink()
                removed = True
            # Also delete associated profile
            try:
                delete_profile(safe_name)
            except Exception as e:
                logger.warning(f"Failed to delete profile for {safe_name}: {e}")
            if removed:
                deleted += 1

        logger.info(f"Bulk deleted {deleted}/{len(ids)} faces")
        return jsonify({'success': True, 'deleted': deleted})
    except Exception as e:
        logger.error(f"Bulk delete failed: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ── API: Profile Management ──────────────────────────────────────────────

@face_photo_manager_bp.route('/api/faces/profile-stats')
def face_profile_stats():
    """Get profile statistics."""
    try:
        stats = bulk_stats()
        return jsonify({'success': True, **stats})
    except Exception as e:
        logger.error(f"Error getting profile stats: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@face_photo_manager_bp.route('/api/faces/<filename>/profile')
def face_profile_get(filename):
    """Get the synthetic profile for a face."""
    try:
        safe_name = _safe_filename(filename)
        profile = load_profile(safe_name)
        if profile is None:
            return jsonify({'success': True, 'profile': None})
        return jsonify({'success': True, 'profile': profile})
    except Exception as e:
        logger.error(f"Error getting profile for {filename}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@face_photo_manager_bp.route('/api/faces/<filename>/profile', methods=['PUT'])
def face_profile_update(filename):
    """Update identity fields for a face's profile."""
    try:
        safe_name = _safe_filename(filename)
        data = request.get_json() or {}
        updates = data.get('identity', {})
        if not updates:
            return jsonify({'success': False, 'error': 'No identity updates provided'}), 400
        profile = update_profile_identity(safe_name, updates)
        if profile is None:
            return jsonify({'success': False, 'error': 'Profile not found'}), 404
        return jsonify({'success': True, 'profile': profile})
    except Exception as e:
        logger.error(f"Error updating profile for {filename}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@face_photo_manager_bp.route('/api/faces/<filename>/profile', methods=['DELETE'])
def face_profile_delete(filename):
    """Delete/reset a face's profile (will regenerate on next use)."""
    try:
        safe_name = _safe_filename(filename)
        deleted = delete_profile(safe_name)
        return jsonify({'success': True, 'deleted': deleted})
    except Exception as e:
        logger.error(f"Error deleting profile for {filename}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ── API: Preprocess (background removal) ────────────────────────────────

def _run_preprocess(filenames: list[str] | None, total_count: int):
    """Run the preprocessing script in a subprocess. Executed in a background thread."""
    global _preprocess_state
    try:
        cmd = [PYTHON_BIN, str(PREPROCESS_SCRIPT), '--workers', '4']
        logger.info(f"Starting face preprocessing: {' '.join(cmd)}")

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        with _preprocess_lock:
            _preprocess_state['process'] = proc
            _preprocess_state['total'] = total_count

        # Monitor output for progress lines
        for line in proc.stdout:
            line = line.strip()
            if line:
                logger.debug(f"preprocess: {line}")
                # Parse progress: "Progress: 150/500 (ok=...)"
                m = re.search(r'Progress:\s*(\d+)/(\d+)', line)
                if m:
                    with _preprocess_lock:
                        _preprocess_state['progress'] = int(m.group(1))
                        _preprocess_state['total'] = int(m.group(2))

        proc.wait()
        logger.info(f"Face preprocessing finished with return code {proc.returncode}")

    except Exception as e:
        logger.error(f"Face preprocessing failed: {e}")
    finally:
        with _preprocess_lock:
            _preprocess_state['running'] = False
            _preprocess_state['process'] = None
            _preprocess_state['thread'] = None


@face_photo_manager_bp.route('/api/faces/preprocess', methods=['POST'])
def face_preprocess():
    """Trigger background-removal preprocessing.

    Accepts optional JSON body: {"filenames": ["face_00100.png", ...]}
    If filenames omitted, processes all unprocessed faces.
    """
    try:
        with _preprocess_lock:
            if _preprocess_state['running']:
                return jsonify({
                    'success': False,
                    'error': 'Preprocessing is already running',
                }), 409

        data = request.get_json(silent=True) or {}
        filenames = data.get('filenames')

        # Count how many will be processed
        if filenames:
            count = len(filenames)
        else:
            originals = set(_list_faces(FACES_DIR))
            whites = set(_list_faces(FACES_WHITE_DIR))
            count = len(originals - whites)

        if count == 0:
            return jsonify({
                'success': True,
                'message': 'All faces are already preprocessed.',
                'count': 0,
            })

        with _preprocess_lock:
            _preprocess_state['running'] = True
            _preprocess_state['progress'] = 0
            _preprocess_state['total'] = count

        thread = threading.Thread(
            target=_run_preprocess,
            args=(filenames, count),
            daemon=True,
            name='face-preprocess',
        )
        thread.start()

        with _preprocess_lock:
            _preprocess_state['thread'] = thread

        return jsonify({
            'success': True,
            'message': f'Preprocessing started for {count} face(s).',
            'count': count,
        })
    except Exception as e:
        logger.error(f"Failed to start preprocessing: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@face_photo_manager_bp.route('/api/faces/preprocess/status')
def face_preprocess_status():
    """Check preprocessing status."""
    try:
        with _preprocess_lock:
            running = _preprocess_state['running']
            progress = _preprocess_state['progress']
            total = _preprocess_state['total']

            # Double-check: if we think it's running but the process is gone
            proc = _preprocess_state.get('process')
            if running and proc is not None and proc.poll() is not None:
                _preprocess_state['running'] = False
                running = False

        return jsonify({
            'success': True,
            'running': running,
            'progress': progress,
            'total': total,
        })
    except Exception as e:
        logger.error(f"Failed to get preprocessing status: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ── Gender detection background state ─────────────────────────────────
_gender_lock = threading.Lock()
_gender_state = {
    'running': False,
    'total': 0,
    'progress': 0,
    'thread': None,
}


def _run_gender_detection():
    """Run gender detection on all faces missing metadata. Background thread."""
    global _gender_state
    try:
        # Ensure model files are available
        if not ensure_models():
            logger.error("Gender detection: failed to download models.")
            return

        faces = sorted(
            f for f in os.listdir(FACES_DIR)
            if f.lower().endswith('.png')
        ) if FACES_DIR.is_dir() else []

        # Filter to those missing gender metadata
        missing = [f for f in faces if read_gender_meta(f) is None]

        with _gender_lock:
            _gender_state['total'] = len(missing)
            _gender_state['progress'] = 0

        for i, fname in enumerate(missing, 1):
            detect_and_store_gender(fname)
            with _gender_lock:
                _gender_state['progress'] = i

        logger.info(f"Gender detection complete: processed {len(missing)} faces.")

    except Exception as e:
        logger.error(f"Gender detection failed: {e}")
    finally:
        with _gender_lock:
            _gender_state['running'] = False
            _gender_state['thread'] = None


# ── API: Gender Detection ─────────────────────────────────────────────

@face_photo_manager_bp.route('/api/faces/detect-gender', methods=['POST'])
def face_detect_gender():
    """Trigger bulk gender detection on all faces missing gender metadata.

    Downloads model files on first run. Runs in a background thread.
    """
    try:
        with _gender_lock:
            if _gender_state['running']:
                return jsonify({
                    'success': False,
                    'error': 'Gender detection is already running',
                }), 409

        # Count how many need detection
        faces = sorted(
            f for f in os.listdir(FACES_DIR)
            if f.lower().endswith('.png')
        ) if FACES_DIR.is_dir() else []
        missing = [f for f in faces if read_gender_meta(f) is None]

        if not missing:
            return jsonify({
                'success': True,
                'message': 'All faces already have gender metadata.',
                'count': 0,
            })

        with _gender_lock:
            _gender_state['running'] = True
            _gender_state['progress'] = 0
            _gender_state['total'] = len(missing)

        thread = threading.Thread(
            target=_run_gender_detection,
            daemon=True,
            name='gender-detection',
        )
        thread.start()

        with _gender_lock:
            _gender_state['thread'] = thread

        return jsonify({
            'success': True,
            'message': f'Gender detection started for {len(missing)} face(s).',
            'count': len(missing),
        })
    except Exception as e:
        logger.error(f"Failed to start gender detection: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@face_photo_manager_bp.route('/api/faces/detect-gender/status')
def face_detect_gender_status():
    """Check gender detection status."""
    try:
        with _gender_lock:
            running = _gender_state['running']
            progress = _gender_state['progress']
            total = _gender_state['total']

        return jsonify({
            'success': True,
            'running': running,
            'progress': progress,
            'total': total,
        })
    except Exception as e:
        logger.error(f"Failed to get gender detection status: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@face_photo_manager_bp.route('/api/faces/<filename>/gender')
def face_gender_get(filename):
    """Get the detected gender for a single face."""
    try:
        safe_name = _safe_filename(filename)
        gender = read_gender_meta(safe_name)
        return jsonify({'success': True, 'filename': safe_name, 'detected_gender': gender})
    except Exception as e:
        logger.error(f"Error getting gender for {filename}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
