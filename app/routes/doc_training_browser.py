"""Document Training Data Browser routes for Groundtruth Studio."""
from flask import Blueprint, request, jsonify, render_template, send_file
from pathlib import Path
from functools import lru_cache
from PIL import Image
import logging
import os
import zipfile
import json
import re

doc_training_browser_bp = Blueprint('doc_training_browser', __name__)
logger = logging.getLogger(__name__)

DATASET_ROOT = Path("/mnt/storage/training-material/documents/yolo-doc-detect/merged")
CLASS_NAMES = {
    0: 'passport',
    1: 'drivers_license',
    2: 'twic_card',
    3: 'merchant_mariner_credential',
    4: 'id_card_generic',
    5: 'uscg_medical_cert',
}
CLASS_COLORS = {
    0: '#3b82f6',
    1: '#22c55e',
    2: '#f59e0b',
    3: '#ec4899',
    4: '#8b5cf6',
    5: '#06b6d4',
}

_classes_cache = {'data': None, 'time': 0}
SPLITS = ('train', 'val', 'test')
SOURCE_PREFIXES = {
    'midv500': 'midv500_',
    'midv2020': 'midv2020_',
    'idnet': 'idnet_',
    'synthesizer': ['uspassport_', 'twic_', 'mmc_', 'wi21dl_'],
    'template': ['tpl_'],
}

# ── Dataset source directories ──────────────────────────────────────────
IDNET_DIR = "/mnt/storage/training-material/documents/datasets/idnet"
MIDV500_DIR = "/mnt/storage/training-material/documents/datasets/midv-500/dataset"
MIDV2020_TEMPLATE_DIR = "/mnt/storage/training-material/documents/datasets/midv-2020/dataset/template_annotations"

# ── Country code mapping for MIDV-500 / MIDV-2020 ──────────────────────
MIDV500_COUNTRY_MAP = {
    'alb': 'Albania', 'aut': 'Austria', 'aze': 'Azerbaijan', 'bra': 'Brazil',
    'chl': 'Chile', 'chn': 'China', 'cze': 'Czech Republic', 'deu': 'Germany',
    'dza': 'Algeria', 'esp': 'Spain', 'est': 'Estonia', 'fin': 'Finland',
    'grc': 'Greece', 'hrv': 'Croatia', 'hun': 'Hungary', 'irn': 'Iran',
    'ita': 'Italy', 'ltu': 'Lithuania', 'lva': 'Latvia', 'mda': 'Moldova',
    'nld': 'Netherlands', 'pol': 'Poland', 'prt': 'Portugal', 'rou': 'Romania',
    'rus': 'Russia', 'srb': 'Serbia', 'svk': 'Slovakia', 'tun': 'Tunisia',
    'tur': 'Turkey', 'ukr': 'Ukraine', 'usa': 'USA',
}

# ── In-memory caches ────────────────────────────────────────────────────
# IDNet: {state: {original_basename: metadata_dict}}
_idnet_meta_cache = {}
# MIDV-500: {doc_code: field_dict_or_None}
_midv500_meta_cache = {}
# MIDV-2020: {doc_type: {field_name: value}}
_midv2020_meta_cache = {}


def _get_midv2020_metadata(doc_type):
    """Load MIDV-2020 template field values from annotation JSON. Cached per doc_type."""
    if doc_type in _midv2020_meta_cache:
        return _midv2020_meta_cache[doc_type]
    _midv2020_meta_cache[doc_type] = None
    json_path = os.path.join(MIDV2020_TEMPLATE_DIR, f"{doc_type}.json")
    if not os.path.exists(json_path):
        return None
    try:
        with open(json_path, 'r') as f:
            data = json.load(f)
        meta = data.get('_via_img_metadata', {})
        # Extract fields from first image (all share same template values)
        skip_fields = {'face', 'photo', 'signature', 'doc_quad'}
        fields = {}
        for img_key, img_data in meta.items():
            for region in img_data.get('regions', []):
                ra = region.get('region_attributes', {})
                fn = ra.get('field_name', '')
                val = ra.get('value', '')
                if fn and fn not in skip_fields and val:
                    fields[fn] = val
            if fields:
                break  # Got fields from first image
        _midv2020_meta_cache[doc_type] = fields
    except Exception as e:
        logger.error(f"Failed to load MIDV-2020 metadata for {doc_type}: {e}")
    return _midv2020_meta_cache[doc_type]


def _load_idnet_meta(state):
    """Load all IDNet metadata for a state from ZIP. Cached per state."""
    if state in _idnet_meta_cache:
        return
    _idnet_meta_cache[state] = {}
    zip_path = os.path.join(IDNET_DIR, f"{state}.zip")
    if not os.path.exists(zip_path):
        return
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            meta_files = [
                n for n in zf.namelist()
                if n.startswith(f'{state}/meta/basic/') and n.endswith('.json')
            ]
            for mf in meta_files:
                bn = os.path.splitext(os.path.basename(mf))[0]
                try:
                    data = json.loads(zf.read(mf))
                    _idnet_meta_cache[state][bn] = data
                except Exception:
                    pass
    except Exception as e:
        logger.error(f"Failed to load IDNet metadata for {state}: {e}")


def _get_idnet_metadata(state, original_basename):
    """Get IDNet metadata by original basename (e.g. 'generated.photos_v3_0000333').
    Returns the metadata dict or None."""
    _load_idnet_meta(state)
    return _idnet_meta_cache.get(state, {}).get(original_basename)


def _get_midv500_metadata(doc_code):
    """Load MIDV-500 template field values from ZIP. Cached per doc_code."""
    if doc_code in _midv500_meta_cache:
        return _midv500_meta_cache[doc_code]
    _midv500_meta_cache[doc_code] = None
    zip_path = os.path.join(MIDV500_DIR, f"{doc_code}.zip")
    if not os.path.exists(zip_path):
        return None
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            # The document-level ground truth is at {doc_code}/ground_truth/{doc_code}.json
            gt_path = f"{doc_code}/ground_truth/{doc_code}.json"
            if gt_path in zf.namelist():
                data = json.loads(zf.read(gt_path))
                _midv500_meta_cache[doc_code] = data
            else:
                # Fallback: find any top-level ground_truth JSON
                for name in zf.namelist():
                    if ('ground_truth' in name and name.endswith('.json')
                            and name.count('/') == 2):
                        bn = os.path.splitext(os.path.basename(name))[0]
                        if bn == doc_code:
                            data = json.loads(zf.read(name))
                            _midv500_meta_cache[doc_code] = data
                            break
    except Exception as e:
        logger.error(f"Failed to load MIDV-500 metadata for {doc_code}: {e}")
    return _midv500_meta_cache[doc_code]


def _parse_issuer(filename):
    """Extract issuer code from filename. Returns uppercase code string."""
    if filename.startswith('idnet_'):
        # idnet_AZ_generated.photos_v3_0000333.jpg -> AZ
        parts = filename.split('_', 2)
        if len(parts) >= 3:
            return parts[1].upper()
    elif filename.startswith('midv500_'):
        # midv500_01_alb_id_CA01_02.jpg -> ALB
        m = re.match(r'midv500_\d+_([a-z]+)_', filename)
        if m:
            return m.group(1).upper()
    elif filename.startswith('midv2020_'):
        # midv2020_clip_alb_id_00_000091.jpg -> ALB
        m = re.match(r'midv2020_[a-z]+_([a-z]+)_', filename)
        if m:
            return m.group(1).upper()
    elif filename.startswith(('uspassport_', 'twic_', 'mmc_', 'wi21dl_')):
        return 'SYNTH'
    return 'UNK'


@lru_cache(maxsize=4096)
def _get_image_size(image_path):
    """Get image dimensions, cached in memory."""
    try:
        with Image.open(image_path) as img:
            return img.size  # (width, height)
    except Exception:
        return (640, 480)  # fallback


def _parse_label_file(label_path):
    """Parse a YOLO label .txt file and return list of bbox dicts."""
    bboxes = []
    try:
        with open(label_path, 'r') as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) < 5:
                    continue
                class_id = int(parts[0])
                x_center = float(parts[1])
                y_center = float(parts[2])
                w = float(parts[3])
                h = float(parts[4])
                bboxes.append({
                    'class_id': class_id,
                    'class_name': CLASS_NAMES.get(class_id, f'class_{class_id}'),
                    'color': CLASS_COLORS.get(class_id, '#888888'),
                    'x_center': x_center,
                    'y_center': y_center,
                    'w': w,
                    'h': h,
                })
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning(f"Error parsing label file {label_path}: {e}")
    return bboxes


SYNTH_META_DIR = Path("/mnt/storage/training-material/documents/synthesizer/output/metadata")

# Synthesizer document type labels by prefix
SYNTH_DOC_TYPES = {
    'uspassport_': 'US Passport (Synthetic)',
    'twic_': 'TWIC Card (Synthetic)',
    'mmc_': 'Merchant Mariner Credential (Synthetic)',
    'wi21dl_': 'WI Under-21 DL (Synthetic)',
}


def _detect_source(filename):
    """Detect data source from filename prefix."""
    fn_lower = filename.lower()
    for source_key, prefix in SOURCE_PREFIXES.items():
        if isinstance(prefix, list):
            if any(fn_lower.startswith(p) for p in prefix):
                return source_key
        elif fn_lower.startswith(prefix):
            return source_key
    return 'other'


# ---- Page Route ----

@doc_training_browser_bp.route('/doc-training-data')
def doc_training_browser_page():
    return render_template('doc_training_browser.html')


# ---- API Routes ----

@doc_training_browser_bp.route('/api/doc-training/classes')
def get_classes():
    """Return all class IDs found in the dataset with names and colors."""
    import time as _time
    if _classes_cache['data'] and (_time.time() - _classes_cache['time']) < 300:
        return jsonify({'success': True, 'classes': _classes_cache['data']})

    found_classes = set()
    for split in SPLITS:
        labels_dir = DATASET_ROOT / split / "labels"
        if not labels_dir.exists():
            continue
        for lbl_file in labels_dir.iterdir():
            if lbl_file.suffix != '.txt':
                continue
            try:
                with open(lbl_file) as f:
                    for line in f:
                        parts = line.strip().split()
                        if len(parts) >= 5:
                            found_classes.add(int(parts[0]))
            except Exception:
                continue
    classes = []
    for cid in sorted(found_classes):
        classes.append({
            'id': cid,
            'name': CLASS_NAMES.get(cid, f'class_{cid}'),
            'color': CLASS_COLORS.get(cid, '#888888'),
        })
    _classes_cache['data'] = classes
    _classes_cache['time'] = _time.time()
    return jsonify({'success': True, 'classes': classes})


@doc_training_browser_bp.route('/api/doc-training/stats')
def doc_training_stats():
    """Return counts per split, per class, and per source."""
    try:
        split_counts = {}
        class_counts = {}
        source_counts = {}

        for split in SPLITS:
            images_dir = DATASET_ROOT / split / 'images'
            labels_dir = DATASET_ROOT / split / 'labels'

            if not images_dir.exists():
                split_counts[split] = 0
                continue

            image_files = [f for f in os.listdir(images_dir)
                           if f.lower().endswith(('.jpg', '.jpeg', '.png'))]
            split_counts[split] = len(image_files)

            for img_file in image_files:
                # Source breakdown
                source = _detect_source(img_file)
                source_counts[source] = source_counts.get(source, 0) + 1

                # Class breakdown from labels
                basename = os.path.splitext(img_file)[0]
                label_path = labels_dir / (basename + '.txt')
                if label_path.exists():
                    try:
                        with open(label_path, 'r') as f:
                            for line in f:
                                parts = line.strip().split()
                                if len(parts) >= 5:
                                    cid = int(parts[0])
                                    cname = CLASS_NAMES.get(cid, f'class_{cid}')
                                    class_counts[cname] = class_counts.get(cname, 0) + 1
                    except Exception:
                        pass

        return jsonify({
            'success': True,
            'splits': split_counts,
            'classes': class_counts,
            'sources': source_counts,
            'total': sum(split_counts.values()),
        })
    except Exception as e:
        logger.error(f"Error computing doc training stats: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@doc_training_browser_bp.route('/api/doc-training/items')
def doc_training_items():
    """Paginated list of images with parsed bounding boxes."""
    try:
        split = request.args.get('split', 'train')
        class_id_filter = request.args.get('class_id', '')
        source_filter = request.args.get('source', '')
        issuer_filter = request.args.get('issuer', '')
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 60))

        if split not in SPLITS:
            return jsonify({'success': False, 'error': 'Invalid split'}), 400

        images_dir = DATASET_ROOT / split / 'images'
        labels_dir = DATASET_ROOT / split / 'labels'

        if not images_dir.exists():
            return jsonify({
                'success': True,
                'items': [],
                'total': 0,
                'page': page,
                'pages': 0,
            })

        # Get all image files, sorted for stable pagination
        image_files = sorted([
            f for f in os.listdir(images_dir)
            if f.lower().endswith(('.jpg', '.jpeg', '.png'))
        ])

        # Apply source filter
        if source_filter:
            prefix = SOURCE_PREFIXES.get(source_filter, '')
            if prefix:
                if isinstance(prefix, list):
                    image_files = [f for f in image_files
                                   if any(f.lower().startswith(p) for p in prefix)]
                else:
                    image_files = [f for f in image_files if f.lower().startswith(prefix)]

        # Apply issuer filter
        if issuer_filter:
            image_files = [f for f in image_files if _parse_issuer(f) == issuer_filter]

        # Apply class filter -- need to check label files
        if class_id_filter != '':
            try:
                target_class_id = int(class_id_filter)
            except ValueError:
                target_class_id = None

            if target_class_id is not None:
                filtered = []
                for img_file in image_files:
                    basename = os.path.splitext(img_file)[0]
                    label_path = labels_dir / (basename + '.txt')
                    if label_path.exists():
                        try:
                            with open(label_path, 'r') as f:
                                for line in f:
                                    parts = line.strip().split()
                                    if len(parts) >= 5 and int(parts[0]) == target_class_id:
                                        filtered.append(img_file)
                                        break
                        except Exception:
                            pass
                image_files = filtered

        total = len(image_files)
        total_pages = max(1, (total + per_page - 1) // per_page)
        start = (page - 1) * per_page
        end = start + per_page
        page_files = image_files[start:end]

        items = []
        for img_file in page_files:
            basename = os.path.splitext(img_file)[0]
            label_path = labels_dir / (basename + '.txt')
            image_path = images_dir / img_file

            w, h = _get_image_size(str(image_path))
            bboxes = _parse_label_file(str(label_path))

            items.append({
                'filename': img_file,
                'image_url': f'/api/doc-training/image/{split}/{img_file}',
                'width': w,
                'height': h,
                'bboxes': bboxes,
                'issuer': _parse_issuer(img_file),
            })

        return jsonify({
            'success': True,
            'items': items,
            'total': total,
            'page': page,
            'pages': total_pages,
        })
    except Exception as e:
        logger.error(f"Error loading doc training items: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@doc_training_browser_bp.route('/api/doc-training/filter-options')
def get_filter_options():
    """Get available sources and issuers based on current filter selections."""
    try:
        split = request.args.get('split', 'train')
        class_id_filter = request.args.get('class_id', '')
        source_filter = request.args.get('source', '')

        if split not in SPLITS:
            return jsonify({'success': False, 'error': 'Invalid split'}), 400

        img_dir = DATASET_ROOT / split / 'images'
        labels_dir = DATASET_ROOT / split / 'labels'
        if not img_dir.is_dir():
            return jsonify({'success': True, 'sources': [], 'issuers': []})

        image_files = [f for f in os.listdir(img_dir)
                       if f.lower().endswith(('.jpg', '.jpeg', '.png'))]

        # Apply class filter if set (need to check labels)
        if class_id_filter != '':
            try:
                target_class_id = int(class_id_filter)
            except ValueError:
                target_class_id = None
            if target_class_id is not None:
                filtered = []
                for img_file in image_files:
                    basename = os.path.splitext(img_file)[0]
                    label_path = labels_dir / (basename + '.txt')
                    if label_path.exists():
                        try:
                            with open(label_path, 'r') as f:
                                for line in f:
                                    parts = line.strip().split()
                                    if len(parts) >= 5 and int(parts[0]) == target_class_id:
                                        filtered.append(img_file)
                                        break
                        except Exception:
                            pass
                image_files = filtered

        # Compute source counts from class-filtered files
        source_counts = {}
        for f in image_files:
            source = _detect_source(f)
            source_counts[source] = source_counts.get(source, 0) + 1
        sources = [{'code': k, 'count': v} for k, v in sorted(source_counts.items())]

        # Apply source filter for issuer computation
        if source_filter:
            prefix = SOURCE_PREFIXES.get(source_filter, '')
            if prefix:
                image_files = [f for f in image_files if f.lower().startswith(prefix)]

        # Compute issuer counts from class+source filtered files
        issuer_counts = {}
        for f in image_files:
            issuer = _parse_issuer(f)
            issuer_counts[issuer] = issuer_counts.get(issuer, 0) + 1
        issuers = [{'code': k, 'count': v} for k, v in sorted(issuer_counts.items())]

        return jsonify({'success': True, 'sources': sources, 'issuers': issuers})
    except Exception as e:
        logger.error(f"Error loading filter options: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@doc_training_browser_bp.route('/api/doc-training/metadata/<split>/<filename>')
def get_image_metadata(split, filename):
    """Get annotation metadata for a specific image."""
    try:
        if split not in SPLITS:
            return jsonify({'success': False, 'error': 'Invalid split'}), 400

        # Sanitize
        filename = os.path.basename(filename)
        metadata = {'source': 'unknown', 'issuer': _parse_issuer(filename), 'fields': {}}

        if filename.startswith('idnet_'):
            # idnet_AZ_generated.photos_v3_0000333.jpg
            bare = os.path.splitext(filename)[0]  # strip extension
            # Split: idnet_{state}_{original_basename}
            parts = bare.split('_', 2)  # ['idnet', 'AZ', 'generated.photos_v3_0000333']
            if len(parts) >= 3:
                state = parts[1]
                original_basename = parts[2]  # e.g. 'generated.photos_v3_0000333'
                metadata['source'] = 'IDNet'
                metadata['issuer_name'] = f"{state} (US)"
                metadata['document_type'] = "Driver's License"

                basic = _get_idnet_metadata(state, original_basename)
                if basic:
                    metadata['fields'] = {
                        'Name': basic.get('name', ''),
                        'Address': basic.get('address', ''),
                        'Date of Birth': basic.get('birthday', ''),
                        'Gender': basic.get('gender', ''),
                        'License Number': basic.get('license_number', ''),
                        'Class': basic.get('class', ''),
                        'Issue Date': basic.get('issue_date', ''),
                        'Expiration Date': basic.get('expire_date', ''),
                        'Height': basic.get('height', ''),
                        'Weight': basic.get('weight', ''),
                        'Eye Color': basic.get('eye_color', ''),
                        'Hair Color': basic.get('hair_color', ''),
                        'Organ Donor': 'Yes' if basic.get('is_donor') else 'No',
                        'Veteran': 'Yes' if basic.get('is_veteran') else 'No',
                    }

        elif filename.startswith('midv500_'):
            # midv500_01_alb_id_CA01_02.jpg
            m = re.match(r'midv500_(\d+_[a-z]+_[a-z]+)', filename)
            metadata['source'] = 'MIDV-500'
            if m:
                doc_code = m.group(1)
                parts = doc_code.split('_')
                country_code = parts[1] if len(parts) >= 2 else ''
                doc_type_code = parts[2] if len(parts) >= 3 else ''
                metadata['issuer_name'] = MIDV500_COUNTRY_MAP.get(
                    country_code, country_code.upper())
                metadata['document_type'] = (
                    'Passport' if 'passport' in doc_type_code
                    else 'Driver\'s License' if 'drvlic' in doc_type_code
                    else 'ID Card')

                template_data = _get_midv500_metadata(doc_code)
                if template_data:
                    for key, val in template_data.items():
                        if isinstance(val, dict) and 'value' in val and val['value']:
                            label = key
                            if key == 'photo':
                                continue  # Skip photo region
                            elif key == 'signature':
                                continue  # Skip signature region
                            elif key.startswith('field'):
                                label = 'Field ' + key.replace('field', '')
                            metadata['fields'][label] = val['value']

        elif filename.startswith(('uspassport_', 'twic_', 'mmc_', 'wi21dl_')):
            # Synthesized document — load metadata from JSON sidecar
            bare = os.path.splitext(filename)[0]
            metadata['source'] = 'Synthesizer'
            metadata['issuer'] = 'SYNTH'

            # Detect document sub-type
            for pfx, doc_label in SYNTH_DOC_TYPES.items():
                if filename.startswith(pfx):
                    metadata['document_type'] = doc_label
                    break

            # Try loading JSON sidecar from synthesizer output metadata dir
            meta_json = SYNTH_META_DIR / f"{bare}.json"
            if meta_json.exists():
                try:
                    with open(meta_json, 'r') as f:
                        synth_meta = json.load(f)
                    metadata['fields'] = synth_meta.get('fields', {})
                    if 'mrz_line1' in synth_meta:
                        metadata['fields']['MRZ Line 1'] = synth_meta['mrz_line1']
                        metadata['fields']['MRZ Line 2'] = synth_meta.get('mrz_line2', '')
                    if 'variant' in synth_meta:
                        metadata['fields']['Variant'] = synth_meta['variant']
                except Exception as e:
                    logger.warning(f"Failed to load synth metadata for {filename}: {e}")

        elif filename.startswith('midv2020_'):
            # midv2020_clip_alb_id_00_000091.jpg
            # Extract: mode, doc_type (e.g. alb_id, aze_passport, rus_internalpassport)
            m = re.match(r'midv2020_([a-z]+)_(.+?)_\d+_\d+', os.path.splitext(filename)[0])
            metadata['source'] = 'MIDV-2020'
            if m:
                mode = m.group(1)
                doc_type = m.group(2)  # e.g. 'alb_id', 'aze_passport'
                # Extract country code (first part before _)
                country_code = doc_type.split('_')[0]
                doc_type_suffix = '_'.join(doc_type.split('_')[1:])
                metadata['issuer_name'] = MIDV500_COUNTRY_MAP.get(
                    country_code, country_code.upper())
                metadata['document_type'] = (
                    'Passport' if 'passport' in doc_type_suffix
                    else 'Internal Passport' if 'internal' in doc_type_suffix
                    else 'ID Card')
                metadata['capture_mode'] = mode.title()

                # Load template field values
                template_fields = _get_midv2020_metadata(doc_type)
                if template_fields:
                    # Format field names nicely
                    nice_names = {
                        'name': 'Name', 'surname': 'Surname', 'surname_eng': 'Surname (Eng)',
                        'name_eng': 'Name (Eng)', 'patronymic': 'Patronymic',
                        'surname_second': 'Second Surname',
                        'birth_date': 'Date of Birth', 'birth_place': 'Place of Birth',
                        'birth_country': 'Birth Country', 'birth_place_eng': 'Place of Birth (Eng)',
                        'gender': 'Gender', 'nationality': 'Nationality',
                        'nationality/nationality_eng': 'Nationality',
                        'number': 'Document Number', 'id_number': 'ID Number',
                        'issue_date': 'Issue Date', 'expiry_date': 'Expiry Date',
                        'authority': 'Issuing Authority', 'authority_eng': 'Authority (Eng)',
                        'authority_line0': 'Authority', 'authority_line1': 'Authority (cont)',
                        'type': 'Document Type', 'code': 'Country Code',
                        'height': 'Height',
                        'mrz_line0': 'MRZ Line 1', 'mrz_line1': 'MRZ Line 2',
                    }
                    skip = {'birth_date_2', 'birth_date_22', 'expiry_date_2',
                            'expiry_date2', 'name_code', 'number2', 'number3'}
                    for fn, val in template_fields.items():
                        if fn in skip:
                            continue
                        label = nice_names.get(fn, fn.replace('_', ' ').title())
                        metadata['fields'][label] = val

        return jsonify({'success': True, 'metadata': metadata})
    except Exception as e:
        logger.error(f"Failed to get metadata for {filename}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@doc_training_browser_bp.route('/api/doc-training/image/<split>/<filename>')
def doc_training_image(split, filename):
    """Serve an image from the dataset with cache headers."""
    if split not in SPLITS:
        return jsonify({'error': 'Invalid split'}), 400

    # Sanitize filename to prevent path traversal
    safe_name = os.path.basename(filename)
    image_path = DATASET_ROOT / split / 'images' / safe_name

    if not image_path.exists():
        return jsonify({'error': 'Image not found'}), 404

    mimetype = 'image/png' if safe_name.lower().endswith('.png') else 'image/jpeg'
    response = send_file(
        str(image_path),
        mimetype=mimetype,
    )
    response.headers['Cache-Control'] = 'public, max-age=604800'  # 7 days
    return response


@doc_training_browser_bp.route('/api/doc-training/thumbnail/<split>/<filename>')
def doc_training_thumbnail(split, filename):
    """Serve a cached 300x300 JPEG thumbnail, generating it on first request."""
    if split not in SPLITS:
        return jsonify({'error': 'Invalid split'}), 400

    # Sanitize filename to prevent path traversal
    safe_name = os.path.basename(filename)
    source_path = DATASET_ROOT / split / 'images' / safe_name

    if not source_path.exists():
        return jsonify({'error': 'Image not found'}), 404

    # Thumbnail is always stored as .jpg regardless of source extension
    thumb_basename = Path(safe_name).stem + '.jpg'
    thumb_dir = DATASET_ROOT / split / '.thumbcache'
    thumb_path = thumb_dir / thumb_basename

    force_refresh = request.args.get('refresh', '0') == '1'

    # Check if a valid cached thumbnail exists
    use_cache = (
        not force_refresh
        and thumb_path.exists()
        and thumb_path.stat().st_mtime >= source_path.stat().st_mtime
    )

    if not use_cache:
        try:
            thumb_dir.mkdir(parents=True, exist_ok=True)
            with Image.open(source_path) as img:
                img = img.convert('RGB')
                img.thumbnail((300, 300), Image.LANCZOS)
                img.save(str(thumb_path), 'JPEG', quality=80)
        except Exception as e:
            logger.error(f"Failed to generate thumbnail for {safe_name}: {e}")
            # Fall back to serving the original image
            response = send_file(str(source_path), mimetype='image/jpeg')
            response.headers['Cache-Control'] = 'public, max-age=604800'
            return response

    response = send_file(str(thumb_path), mimetype='image/jpeg')
    response.headers['Cache-Control'] = 'public, max-age=604800'  # 7 days
    return response
