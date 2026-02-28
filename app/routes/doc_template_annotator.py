"""Document Template Annotator routes for Groundtruth Studio.

Provides CRUD for document templates used by the synthesizer, plus
preview rendering and batch generation endpoints.
"""
import json
import logging
import os
import random
import shutil
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, request, jsonify, render_template, send_file, send_from_directory
from PIL import Image

doc_template_annotator_bp = Blueprint('doc_template_annotator', __name__)
logger = logging.getLogger(__name__)

# ── Storage paths ────────────────────────────────────────────────────────
TEMPLATES_DIR = Path("/mnt/storage/training-material/documents/synthesizer/templates")
TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)

SYNTH_DIR = Path("/mnt/storage/training-material/documents/synthesizer")
if str(SYNTH_DIR) not in sys.path:
    sys.path.insert(0, str(SYNTH_DIR))

SCENE_BG_DIR = SYNTH_DIR / "assets" / "scene_backgrounds"
SCENE_BG_DIR.mkdir(parents=True, exist_ok=True)

# ── Class name mapping (mirrors doc_training_browser) ────────────────────
CLASS_NAMES = {
    0: 'passport',
    1: 'drivers_license',
    2: 'twic_card',
    3: 'merchant_mariner_credential',
    4: 'id_card_generic',
    5: 'uscg_medical_cert',
}
CLASS_NAMES_REVERSE = {v: k for k, v in CLASS_NAMES.items()}

# ── In-memory status tracking for background generation ──────────────────
_generation_status = {}

ALLOWED_IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg'}


# ── Helpers ──────────────────────────────────────────────────────────────

def _update_index():
    """Scan all template directories and write index.json."""
    index = []
    for entry in sorted(TEMPLATES_DIR.iterdir()):
        if not entry.is_dir():
            continue
        tpl_json = entry / "template.json"
        if not tpl_json.exists():
            continue
        try:
            with open(tpl_json, 'r') as f:
                tpl = json.load(f)
            index.append({
                'id': tpl.get('id', entry.name),
                'name': tpl.get('name', entry.name),
                'class_id': tpl.get('class_id'),
                'class_name': tpl.get('class_name', ''),
                'created': tpl.get('created', ''),
                'modified': tpl.get('modified', ''),
                'region_count': len(tpl.get('regions', [])),
                'thumbnail': f"/api/doc-templates/{tpl.get('id', entry.name)}/background",
            })
        except Exception as e:
            logger.warning(f"Failed to read template {entry.name}: {e}")
    index_path = TEMPLATES_DIR / "index.json"
    with open(index_path, 'w') as f:
        json.dump(index, f, indent=2)
    return index


def _load_template(template_id):
    """Load a template definition from disk. Returns (dict, Path) or (None, None)."""
    safe_id = os.path.basename(template_id)
    tpl_dir = TEMPLATES_DIR / safe_id
    tpl_json = tpl_dir / "template.json"
    if not tpl_json.exists():
        return None, None
    with open(tpl_json, 'r') as f:
        return json.load(f), tpl_dir


def _save_template(tpl, tpl_dir):
    """Write template definition to disk and update the index."""
    tpl['modified'] = datetime.now(timezone.utc).isoformat()
    with open(tpl_dir / "template.json", 'w') as f:
        json.dump(tpl, f, indent=2)
    _update_index()


def _make_thumbnail(image_path, thumb_path, width=200):
    """Create a JPEG thumbnail of the given width."""
    try:
        with Image.open(image_path) as img:
            ratio = width / img.width
            new_h = int(img.height * ratio)
            img = img.resize((width, new_h), Image.LANCZOS)
            img = img.convert('RGB')
            img.save(str(thumb_path), 'JPEG', quality=80)
    except Exception as e:
        logger.warning(f"Thumbnail generation failed for {image_path}: {e}")


# ── Page Route ───────────────────────────────────────────────────────────

@doc_template_annotator_bp.route('/doc-template-annotator')
def doc_template_annotator_page():
    return render_template('doc_template_annotator.html')


# ── API: List / Create templates ─────────────────────────────────────────

@doc_template_annotator_bp.route('/api/doc-templates/', methods=['GET'])
def list_templates():
    """List all templates from index.json (rebuild if missing)."""
    try:
        index_path = TEMPLATES_DIR / "index.json"
        if index_path.exists():
            with open(index_path, 'r') as f:
                index = json.load(f)
        else:
            index = _update_index()
        return jsonify({'success': True, 'templates': index})
    except Exception as e:
        logger.error(f"Error listing templates: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@doc_template_annotator_bp.route('/api/doc-templates/', methods=['POST'])
def create_template():
    """Create a new template.

    Expects multipart form data:
      - background: image file (PNG or JPEG)
      - name: human-readable template name
      - class_id: integer class ID (0-4)
    """
    try:
        if 'background' not in request.files:
            return jsonify({'success': False, 'error': 'No background image provided'}), 400

        bg_file = request.files['background']
        name = request.form.get('name', '').strip()
        if not name:
            return jsonify({'success': False, 'error': 'Template name is required'}), 400

        try:
            class_id = int(request.form.get('class_id', 0))
        except (ValueError, TypeError):
            return jsonify({'success': False, 'error': 'Invalid class_id'}), 400

        if class_id not in CLASS_NAMES:
            return jsonify({'success': False, 'error': f'class_id must be one of {list(CLASS_NAMES.keys())}'}), 400

        # Validate image extension
        ext = os.path.splitext(bg_file.filename)[1].lower()
        if ext not in ALLOWED_IMAGE_EXTENSIONS:
            return jsonify({'success': False, 'error': 'Background must be PNG or JPEG'}), 400

        # Generate template ID from name
        template_id = name.lower().replace(' ', '_')
        template_id = ''.join(c for c in template_id if c.isalnum() or c == '_')
        if not template_id:
            template_id = str(uuid.uuid4())[:8]

        # Ensure uniqueness
        tpl_dir = TEMPLATES_DIR / template_id
        if tpl_dir.exists():
            template_id = f"{template_id}_{int(time.time())}"
            tpl_dir = TEMPLATES_DIR / template_id

        tpl_dir.mkdir(parents=True, exist_ok=True)
        (tpl_dir / 'overlays').mkdir(exist_ok=True)

        # Save background image
        bg_filename = f"background{ext}"
        bg_path = tpl_dir / bg_filename
        bg_file.save(str(bg_path))

        # Get dimensions
        with Image.open(bg_path) as img:
            width, height = img.size

        # Generate thumbnail
        _make_thumbnail(bg_path, tpl_dir / 'thumbnail.jpg')

        # Build template definition
        now = datetime.now(timezone.utc).isoformat()
        tpl = {
            'id': template_id,
            'name': name,
            'class_id': class_id,
            'class_name': CLASS_NAMES[class_id],
            'background_image': bg_filename,
            'dimensions': {'width': width, 'height': height},
            'created': now,
            'modified': now,
            'regions': [],
        }

        _save_template(tpl, tpl_dir)

        return jsonify({'success': True, 'template': tpl}), 201

    except Exception as e:
        logger.error(f"Error creating template: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ── API: Single template CRUD ────────────────────────────────────────────

@doc_template_annotator_bp.route('/api/doc-templates/<template_id>', methods=['GET'])
def get_template(template_id):
    """Get full template definition."""
    try:
        tpl, _ = _load_template(template_id)
        if tpl is None:
            return jsonify({'success': False, 'error': 'Template not found'}), 404
        return jsonify({'success': True, 'template': tpl})
    except Exception as e:
        logger.error(f"Error getting template {template_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@doc_template_annotator_bp.route('/api/doc-templates/<template_id>', methods=['PUT'])
def update_template(template_id):
    """Update template (regions, name, class).

    Expects JSON body with any of: name, class_id, regions.
    """
    try:
        tpl, tpl_dir = _load_template(template_id)
        if tpl is None:
            return jsonify({'success': False, 'error': 'Template not found'}), 404

        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'JSON body required'}), 400

        if 'name' in data:
            tpl['name'] = data['name']
        if 'class_id' in data:
            cid = int(data['class_id'])
            if cid in CLASS_NAMES:
                tpl['class_id'] = cid
                tpl['class_name'] = CLASS_NAMES[cid]
        if 'regions' in data:
            tpl['regions'] = data['regions']
        if 'overlays' in data:
            tpl['overlays'] = data['overlays']
        if 'print_quality' in data:
            tpl['print_quality'] = data['print_quality']
        if 'lighting' in data:
            tpl['lighting'] = data['lighting']

        _save_template(tpl, tpl_dir)
        return jsonify({'success': True, 'template': tpl})

    except Exception as e:
        logger.error(f"Error updating template {template_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@doc_template_annotator_bp.route('/api/doc-templates/<template_id>', methods=['DELETE'])
def delete_template(template_id):
    """Delete template and its directory."""
    try:
        safe_id = os.path.basename(template_id)
        tpl_dir = TEMPLATES_DIR / safe_id
        if not tpl_dir.exists() or not (tpl_dir / "template.json").exists():
            return jsonify({'success': False, 'error': 'Template not found'}), 404

        shutil.rmtree(tpl_dir)
        _update_index()
        return jsonify({'success': True})

    except Exception as e:
        logger.error(f"Error deleting template {template_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ── API: Preview & Generation ────────────────────────────────────────────

@doc_template_annotator_bp.route('/api/doc-templates/<template_id>/preview', methods=['POST'])
def preview_template(template_id):
    """Generate one preview image and return as JPEG.

    Optionally accepts JSON body with field overrides.
    """
    try:
        tpl, tpl_dir = _load_template(template_id)
        if tpl is None:
            return jsonify({'success': False, 'error': 'Template not found'}), 404

        overrides = request.get_json(silent=True) or {}

        try:
            from doc_synthesizer import render_from_template
        except ImportError:
            return jsonify({
                'success': False,
                'error': 'doc_synthesizer module not found. '
                         'Ensure it exists at /mnt/storage/training-material/documents/synthesizer/doc_synthesizer.py'
            }), 501

        # Render a single preview
        result = render_from_template(tpl_dir)

        # render_from_template returns (PIL.Image, metadata_dict)
        if isinstance(result, tuple):
            img = result[0]
        else:
            img = result

        # Save to temp and serve
        import io
        buf = io.BytesIO()
        if not isinstance(img, Image.Image):
            return jsonify({'success': False, 'error': 'render_from_template did not return a PIL Image'}), 500
        img.convert('RGB').save(buf, 'JPEG', quality=90)
        buf.seek(0)

        return send_file(buf, mimetype='image/jpeg', download_name=f'{template_id}_preview.jpg')

    except ImportError:
        return jsonify({
            'success': False,
            'error': 'render_from_template module not available yet'
        }), 501
    except Exception as e:
        logger.error(f"Error generating preview for {template_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@doc_template_annotator_bp.route('/api/doc-templates/<template_id>/generate', methods=['POST'])
def start_generation(template_id):
    """Start batch generation in a background thread.

    JSON body:
      - count: number of images to generate (default 10)
      - output_dir: optional override for output directory
    """
    try:
        tpl, tpl_dir = _load_template(template_id)
        if tpl is None:
            return jsonify({'success': False, 'error': 'Template not found'}), 404

        data = request.get_json(silent=True) or {}
        count = int(data.get('count', 10))
        output_dir = data.get('output_dir')
        scene_pct = float(data.get('scene_pct', 30)) / 100.0  # UI sends 0-100 integer, convert to 0-1
        max_perspective = float(data.get('max_perspective', 8)) / 100.0  # UI sends 0-25 integer %, convert to 0-1 fraction
        max_rotation = int(data.get('max_rotation', 3))
        photocopy_pct = float(data.get('photocopy_pct', 0)) / 100.0
        oversaturated_pct = float(data.get('oversaturated_pct', 30)) / 100.0
        washout_min = float(data.get('washout_min', 20)) / 100.0
        washout_max = float(data.get('washout_max', 70)) / 100.0
        oversat_min = float(data.get('oversat_min', 20)) / 100.0
        oversat_max = float(data.get('oversat_max', 70)) / 100.0
        overlay_pct = float(data.get('overlay_pct', 0)) / 100.0
        overlay_opacity_min = float(data.get('overlay_opacity_min', 30)) / 100.0
        overlay_opacity_max = float(data.get('overlay_opacity_max', 70)) / 100.0

        if template_id in _generation_status and _generation_status[template_id].get('status') == 'running':
            return jsonify({'success': False, 'error': 'Generation already in progress for this template'}), 409

        _generation_status[template_id] = {
            'status': 'running',
            'total': count,
            'completed': 0,
            'errors': 0,
            'started_at': time.time(),
        }

        def _run():
            try:
                from doc_synthesizer import render_from_template, apply_fold_crop
            except ImportError:
                _generation_status[template_id] = {
                    'status': 'failed',
                    'error': 'doc_synthesizer module not found',
                }
                return

            out = Path(output_dir) if output_dir else (SYNTH_DIR / "output")
            out.mkdir(parents=True, exist_ok=True)
            meta_dir = out / "metadata"
            meta_dir.mkdir(exist_ok=True)

            for i in range(count):
                try:
                    img = render_from_template(tpl_dir)
                    if isinstance(img, tuple):
                        img, metadata = img
                    else:
                        metadata = {}

                    # Apply fold cropping if template has fold lines
                    # Returns list of (img, metadata) tuples — 1 for full, 2 for folded
                    fold_lines = [r for r in tpl.get("regions", []) if r.get("type") == "line" and r.get("fold_line")]
                    if fold_lines:
                        variants = apply_fold_crop(img, fold_lines, metadata)
                    else:
                        metadata["fold_variant"] = "full"
                        variants = [(img, metadata)]

                    for vi, (vimg, vmeta) in enumerate(variants):
                        # Decide: photocopy or potential scene composite (mutually exclusive)
                        if random.random() < photocopy_pct:
                            try:
                                from doc_synthesizer import apply_photocopy_effect
                                mode = "oversaturated" if random.random() < oversaturated_pct else "washout"
                                if mode == "oversaturated":
                                    intensity = random.uniform(oversat_min, oversat_max)
                                else:
                                    intensity = random.uniform(washout_min, washout_max)
                                vimg = apply_photocopy_effect(vimg, washout_level=intensity, mode=mode)
                                vmeta["photocopy"] = True
                                vmeta["photocopy_intensity"] = round(intensity, 3)
                                vmeta["photocopy_mode"] = mode
                                # Apply photocopy overlay artifacts
                                if overlay_pct > 0:
                                    try:
                                        from doc_synthesizer import apply_photocopy_overlays
                                        vimg, overlay_meta = apply_photocopy_overlays(
                                            vimg,
                                            overlay_pct=overlay_pct,
                                            opacity_min=overlay_opacity_min,
                                            opacity_max=overlay_opacity_max,
                                        )
                                        vmeta.update(overlay_meta)
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                        elif random.random() < scene_pct:
                            try:
                                from doc_synthesizer import apply_scene_composite
                                scene_config = {
                                    "max_perspective": max_perspective,
                                    "max_rotation": max_rotation,
                                }
                                vimg, bbox = apply_scene_composite(vimg, scene_config)
                                vmeta["scene"] = True
                                vmeta["bbox"] = list(bbox)
                            except Exception:
                                pass

                        timestamp = int(time.time() * 1000)
                        fold_suffix = f"_{vmeta.get('fold_variant', 'full')}" if len(variants) > 1 else ""
                        filename = f"tpl_{tpl['class_name']}_{template_id}_{timestamp}_{i:04d}{fold_suffix}"
                        img_path = out / f"{filename}.jpg"
                        if isinstance(vimg, Image.Image):
                            vimg.convert('RGB').save(str(img_path), 'JPEG', quality=95)

                        # Save metadata sidecar
                        if vmeta:
                            with open(meta_dir / f"{filename}.json", 'w') as f:
                                json.dump(vmeta, f, indent=2)

                        # Save to merged YOLO dataset (train split)
                        yolo_root = Path("/mnt/storage/training-material/documents/yolo-doc-detect/merged/train")
                        yolo_img_dir = yolo_root / "images"
                        yolo_lbl_dir = yolo_root / "labels"
                        yolo_img_dir.mkdir(parents=True, exist_ok=True)
                        yolo_lbl_dir.mkdir(parents=True, exist_ok=True)

                        # Copy image to YOLO dataset
                        yolo_img_path = yolo_img_dir / f"{filename}.jpg"
                        if isinstance(vimg, Image.Image):
                            vimg.convert('RGB').save(str(yolo_img_path), 'JPEG', quality=95)

                        # Write YOLO label: class_id x_center y_center width height
                        class_id = tpl.get('class_id', 0)
                        if vmeta.get("bbox"):
                            # Scene composite: bbox is [x_center, y_center, width, height] normalized
                            bx, by, bw, bh = vmeta["bbox"]
                        else:
                            # Full document: fills entire image
                            bx, by, bw, bh = 0.5, 0.5, 1.0, 1.0
                        with open(yolo_lbl_dir / f"{filename}.txt", 'w') as f:
                            f.write(f"{class_id} {bx:.6f} {by:.6f} {bw:.6f} {bh:.6f}\n")

                    _generation_status[template_id]['completed'] = i + 1
                except Exception as e:
                    logger.error(f"Generation error for {template_id} image {i}: {e}")
                    _generation_status[template_id]['errors'] = \
                        _generation_status[template_id].get('errors', 0) + 1

            _generation_status[template_id]['status'] = 'completed'
            _generation_status[template_id]['finished_at'] = time.time()

        thread = threading.Thread(target=_run, daemon=True, name=f"gen-{template_id}")
        thread.start()

        return jsonify({
            'success': True,
            'message': f'Generation started: {count} images',
            'template_id': template_id,
        })

    except Exception as e:
        logger.error(f"Error starting generation for {template_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@doc_template_annotator_bp.route('/api/doc-templates/<template_id>/generate/status', methods=['GET'])
def generation_status(template_id):
    """Check generation progress."""
    try:
        status = _generation_status.get(template_id)
        if status is None:
            return jsonify({'success': True, 'status': 'idle'})
        return jsonify({'success': True, **status})
    except Exception as e:
        logger.error(f"Error getting generation status for {template_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ── API: Fonts ───────────────────────────────────────────────────────────

@doc_template_annotator_bp.route('/api/doc-templates/fonts', methods=['GET'])
def list_fonts():
    """List available system fonts (.ttf files)."""
    try:
        font_dirs = [
            Path('/usr/share/fonts'),
            Path('/usr/local/share/fonts'),
            Path.home() / '.fonts',
            SYNTH_DIR / 'fonts',
        ]
        fonts = []
        seen = set()
        for font_dir in font_dirs:
            if not font_dir.exists():
                continue
            for ttf in font_dir.rglob('*.ttf'):
                name = ttf.stem
                if name not in seen:
                    seen.add(name)
                    fonts.append({
                        'name': name,
                        'path': str(ttf),
                    })
            for otf in font_dir.rglob('*.otf'):
                name = otf.stem
                if name not in seen:
                    seen.add(name)
                    fonts.append({
                        'name': name,
                        'path': str(otf),
                    })

        fonts.sort(key=lambda f: f['name'].lower())
        return jsonify({'success': True, 'fonts': fonts})

    except Exception as e:
        logger.error(f"Error listing fonts: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@doc_template_annotator_bp.route('/api/doc-templates/fonts/<font_name>', methods=['GET'])
def serve_font(font_name):
    """Serve a font file for browser @font-face loading."""
    try:
        font_dirs = [
            Path('/usr/share/fonts'),
            Path('/usr/local/share/fonts'),
            Path.home() / '.fonts',
            SYNTH_DIR / 'fonts',
        ]
        # Search for font file by stem name
        target = font_name.lower()
        for font_dir in font_dirs:
            if not font_dir.exists():
                continue
            for ext in ('*.ttf', '*.otf'):
                for fpath in font_dir.rglob(ext):
                    if fpath.stem.lower() == target:
                        mime = 'font/ttf' if fpath.suffix == '.ttf' else 'font/otf'
                        return send_file(str(fpath), mimetype=mime,
                                         download_name=fpath.name,
                                         max_age=86400)
        return jsonify({'success': False, 'error': 'Font not found'}), 404
    except Exception as e:
        logger.error(f"Error serving font {font_name}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ── API: Overlay upload ──────────────────────────────────────────────────

@doc_template_annotator_bp.route('/api/doc-templates/<template_id>/upload-overlay', methods=['POST'])
def upload_overlay(template_id):
    """Upload a static overlay image for a template."""
    try:
        tpl, tpl_dir = _load_template(template_id)
        if tpl is None:
            return jsonify({'success': False, 'error': 'Template not found'}), 404

        if 'overlay' not in request.files:
            return jsonify({'success': False, 'error': 'No overlay file provided'}), 400

        overlay_file = request.files['overlay']
        ext = os.path.splitext(overlay_file.filename)[1].lower()
        if ext not in ALLOWED_IMAGE_EXTENSIONS:
            return jsonify({'success': False, 'error': 'Overlay must be PNG or JPEG'}), 400

        overlays_dir = tpl_dir / 'overlays'
        overlays_dir.mkdir(exist_ok=True)

        # Use the original filename (sanitized)
        safe_name = os.path.basename(overlay_file.filename)
        overlay_path = overlays_dir / safe_name
        overlay_file.save(str(overlay_path))

        return jsonify({'success': True, 'name': safe_name})

    except Exception as e:
        logger.error(f"Error uploading overlay for {template_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ── API: Serve images ────────────────────────────────────────────────────

@doc_template_annotator_bp.route('/api/doc-templates/<template_id>/background', methods=['GET'])
def serve_background(template_id):
    """Serve the background image for a template."""
    try:
        tpl, tpl_dir = _load_template(template_id)
        if tpl is None:
            return jsonify({'success': False, 'error': 'Template not found'}), 404

        bg_filename = tpl.get('background_image', 'background.png')
        bg_path = tpl_dir / bg_filename
        if not bg_path.exists():
            # Try common extensions
            for ext in ('.png', '.jpg', '.jpeg'):
                candidate = tpl_dir / f"background{ext}"
                if candidate.exists():
                    bg_path = candidate
                    break

        if not bg_path.exists():
            return jsonify({'success': False, 'error': 'Background image not found'}), 404

        mimetype = 'image/png' if bg_path.suffix.lower() == '.png' else 'image/jpeg'
        response = send_file(str(bg_path), mimetype=mimetype)
        response.headers['Cache-Control'] = 'public, max-age=86400'
        return response

    except Exception as e:
        logger.error(f"Error serving background for {template_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@doc_template_annotator_bp.route('/api/doc-templates/<template_id>/overlays/<name>', methods=['GET'])
def serve_overlay(template_id, name):
    """Serve an overlay image."""
    try:
        safe_id = os.path.basename(template_id)
        safe_name = os.path.basename(name)
        overlay_path = TEMPLATES_DIR / safe_id / 'overlays' / safe_name

        if not overlay_path.exists():
            return jsonify({'success': False, 'error': 'Overlay not found'}), 404

        mimetype = 'image/png' if overlay_path.suffix.lower() == '.png' else 'image/jpeg'
        response = send_file(str(overlay_path), mimetype=mimetype)
        response.headers['Cache-Control'] = 'public, max-age=86400'
        return response

    except Exception as e:
        logger.error(f"Error serving overlay {name} for {template_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ── API: Scene backgrounds ───────────────────────────────────────────────

@doc_template_annotator_bp.route('/api/doc-templates/scene-backgrounds', methods=['GET'])
def list_scene_backgrounds():
    """List all scene background images with metadata.

    Returns JSON array of objects with: filename, url, size (bytes), dimensions.
    """
    try:
        results = []
        for entry in sorted(SCENE_BG_DIR.iterdir()):
            if not entry.is_file():
                continue
            if entry.suffix.lower() not in ALLOWED_IMAGE_EXTENSIONS:
                continue
            try:
                with Image.open(entry) as img:
                    width, height = img.size
                results.append({
                    'filename': entry.name,
                    'url': f'/api/doc-templates/scene-backgrounds/{entry.name}',
                    'size': entry.stat().st_size,
                    'dimensions': {'width': width, 'height': height},
                })
            except Exception as e:
                logger.warning(f"Could not read scene background {entry.name}: {e}")
        return jsonify({'success': True, 'scene_backgrounds': results})
    except Exception as e:
        logger.error(f"Error listing scene backgrounds: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@doc_template_annotator_bp.route('/api/doc-templates/scene-backgrounds', methods=['POST'])
def upload_scene_backgrounds():
    """Upload one or more scene background images.

    Accepts multipart form upload (field name: 'files' or 'file').
    Validates extensions (.jpg, .jpeg, .png).
    Returns list of uploaded filenames.
    """
    try:
        uploaded_files = request.files.getlist('files') or request.files.getlist('file')
        if not uploaded_files:
            return jsonify({'success': False, 'error': 'No files provided'}), 400

        saved = []
        errors = []
        for f in uploaded_files:
            if not f.filename:
                continue
            ext = os.path.splitext(f.filename)[1].lower()
            if ext not in ALLOWED_IMAGE_EXTENSIONS:
                errors.append(f'{f.filename}: invalid extension (must be .jpg, .jpeg, or .png)')
                continue
            safe_name = os.path.basename(f.filename)
            dest = SCENE_BG_DIR / safe_name
            f.save(str(dest))
            saved.append(safe_name)

        if not saved and errors:
            return jsonify({'success': False, 'error': '; '.join(errors)}), 400

        response = {'success': True, 'uploaded': saved}
        if errors:
            response['warnings'] = errors
        return jsonify(response), 201

    except Exception as e:
        logger.error(f"Error uploading scene backgrounds: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@doc_template_annotator_bp.route('/api/doc-templates/scene-backgrounds/<filename>', methods=['DELETE'])
def delete_scene_background(filename):
    """Delete a scene background image.

    Validates filename is safe (no path traversal).
    """
    try:
        safe_name = os.path.basename(filename)
        if not safe_name or safe_name != filename:
            return jsonify({'success': False, 'error': 'Invalid filename'}), 400

        target = SCENE_BG_DIR / safe_name
        if not target.exists() or not target.is_file():
            return jsonify({'success': False, 'error': 'Scene background not found'}), 404

        target.unlink()
        return jsonify({'success': True, 'deleted': safe_name})

    except Exception as e:
        logger.error(f"Error deleting scene background {filename}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@doc_template_annotator_bp.route('/api/doc-templates/scene-backgrounds/<filename>', methods=['GET'])
def serve_scene_background(filename):
    """Serve a scene background image file."""
    try:
        safe_name = os.path.basename(filename)
        if not safe_name or safe_name != filename:
            return jsonify({'success': False, 'error': 'Invalid filename'}), 400

        target = SCENE_BG_DIR / safe_name
        if not target.exists() or not target.is_file():
            return jsonify({'success': False, 'error': 'Scene background not found'}), 404

        response = send_from_directory(str(SCENE_BG_DIR), safe_name)
        response.headers['Cache-Control'] = 'public, max-age=86400'
        return response

    except Exception as e:
        logger.error(f"Error serving scene background {filename}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
