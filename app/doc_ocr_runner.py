"""
Document OCR runner using DocTR for identity document text extraction.

Two-stage pipeline: doc_detect_runner detects document bounding boxes,
then this runner extracts text fields from the cropped document images.
Results are submitted as child predictions (scenario='document_ocr')
linked to the parent detection via parent_prediction_id.
"""

import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional, Dict, List, Tuple

import requests

logger = logging.getLogger(__name__)

API_BASE_URL = "http://localhost:5050"
MODEL_NAME = "doctr-ocr-v1"
MODEL_VERSION = "1.0"
MODEL_TYPE = "ocr"

# Singleton DocTR model
_ocr_model = None
_ocr_lock = threading.Lock()

# Per-document type field definitions with expected spatial regions
# Regions are approximate (top/middle/bottom, left/right) for heuristic mapping
DOCUMENT_FIELDS = {
    'passport': {
        'surname': {'region': 'bottom', 'keywords': ['surname', 'nom']},
        'given_names': {'region': 'bottom', 'keywords': ['given name', 'prenom']},
        'passport_number': {'region': 'top_right', 'keywords': ['passport no', 'no.']},
        'date_of_birth': {'region': 'bottom', 'keywords': ['date of birth', 'dob']},
        'expiry_date': {'region': 'bottom', 'keywords': ['date of expiry', 'expiration']},
        'nationality': {'region': 'bottom', 'keywords': ['nationality', 'nationalite']},
        'sex': {'region': 'bottom', 'keywords': ['sex', 'sexe']},
        'place_of_birth': {'region': 'bottom', 'keywords': ['place of birth']},
        'mrz_line1': {'region': 'mrz', 'keywords': ['P<']},
        'mrz_line2': {'region': 'mrz', 'keywords': []},
    },
    'drivers_license': {
        'full_name': {'region': 'top', 'keywords': ['name', 'fn', 'ln']},
        'address': {'region': 'middle', 'keywords': ['addr', 'address']},
        'license_number': {'region': 'top_right', 'keywords': ['dl', 'lic', 'no']},
        'date_of_birth': {'region': 'middle', 'keywords': ['dob', 'date of birth']},
        'expiry_date': {'region': 'middle_right', 'keywords': ['exp', 'expiration']},
        'issue_date': {'region': 'middle', 'keywords': ['iss', 'issued']},
        'class': {'region': 'middle', 'keywords': ['class', 'type']},
        'restrictions': {'region': 'bottom', 'keywords': ['restr', 'restrictions']},
        'sex': {'region': 'middle', 'keywords': ['sex']},
        'height': {'region': 'middle', 'keywords': ['hgt', 'height']},
        'eye_color': {'region': 'middle', 'keywords': ['eyes', 'eye']},
    },
    'twic_card': {
        'full_name': {'region': 'middle', 'keywords': ['name']},
        'card_number': {'region': 'bottom', 'keywords': ['card', 'no', 'number']},
        'expiry_date': {'region': 'bottom', 'keywords': ['exp', 'expires']},
    },
    'merchant_mariner_credential': {
        'full_name': {'region': 'top', 'keywords': ['name']},
        'credential_number': {'region': 'top_right', 'keywords': ['no', 'credential']},
        'date_of_birth': {'region': 'middle', 'keywords': ['dob', 'date of birth']},
        'issue_date': {'region': 'middle', 'keywords': ['issued']},
        'expiry_date': {'region': 'middle', 'keywords': ['exp', 'expires']},
        'endorsements': {'region': 'bottom', 'keywords': ['endorsement']},
        'limitations': {'region': 'bottom', 'keywords': ['limitation']},
    },
    'id_card_generic': {
        'full_name': {'region': 'top', 'keywords': ['name']},
        'id_number': {'region': 'top_right', 'keywords': ['no', 'id', 'number']},
        'date_of_birth': {'region': 'middle', 'keywords': ['dob', 'date of birth']},
        'expiry_date': {'region': 'middle', 'keywords': ['exp']},
    },
}

# Keywords for auto-classification of id_card_generic
CLASSIFICATION_KEYWORDS = {
    'passport': ['passport', 'passeport', 'P<'],
    'drivers_license': ['driver', 'license', 'licence', 'driving', 'dl'],
    'twic_card': ['transportation worker', 'twic', 'credential identification'],
    'merchant_mariner_credential': ['merchant mariner', 'mariner credential', 'coast guard'],
}


def _get_ocr_model():
    """Lazy-load the DocTR OCR model (singleton with double-checked locking)."""
    global _ocr_model
    if _ocr_model is not None:
        return _ocr_model

    with _ocr_lock:
        if _ocr_model is not None:
            return _ocr_model

        try:
            from doctr.models import ocr_predictor
            logger.info("Loading DocTR OCR model (db_resnet50 + crnn_vgg16_bn)...")
            _ocr_model = ocr_predictor(
                det_arch='db_resnet50',
                reco_arch='crnn_vgg16_bn',
                pretrained=True
            )
            logger.info("DocTR OCR model loaded successfully")
            return _ocr_model
        except Exception as e:
            logger.error(f"Failed to load DocTR OCR model: {e}")
            return None


def _classify_document_type(text_blocks: List[Dict]) -> Optional[str]:
    """Auto-classify document type from OCR text content."""
    full_text = ' '.join(b['text'].lower() for b in text_blocks)

    for doc_type, keywords in CLASSIFICATION_KEYWORDS.items():
        for keyword in keywords:
            if keyword.lower() in full_text:
                return doc_type
    return None


def _extract_text_blocks(ocr_result) -> List[Dict]:
    """Convert DocTR result to a flat list of text blocks with bounding boxes."""
    blocks = []
    for page in ocr_result.pages:
        page_h, page_w = page.dimensions
        for block in page.blocks:
            for line in block.lines:
                for word in line.words:
                    # DocTR uses relative coordinates (0-1)
                    (x1_rel, y1_rel), (x2_rel, y2_rel) = word.geometry
                    blocks.append({
                        'text': word.value,
                        'confidence': float(word.confidence),
                        'bbox': {
                            'x': int(x1_rel * page_w),
                            'y': int(y1_rel * page_h),
                            'width': int((x2_rel - x1_rel) * page_w),
                            'height': int((y2_rel - y1_rel) * page_h),
                        },
                        'bbox_rel': {
                            'x1': x1_rel, 'y1': y1_rel,
                            'x2': x2_rel, 'y2': y2_rel,
                        },
                    })
    return blocks


def _get_region(bbox_rel: Dict, img_height: int) -> str:
    """Determine spatial region of a text block."""
    y_center = (bbox_rel['y1'] + bbox_rel['y2']) / 2
    x_center = (bbox_rel['x1'] + bbox_rel['x2']) / 2

    # MRZ is at the very bottom (last 20% of document)
    if y_center > 0.80:
        return 'mrz'

    v_region = 'top' if y_center < 0.33 else ('middle' if y_center < 0.66 else 'bottom')
    h_region = '_right' if x_center > 0.6 else ''

    return v_region + h_region


def _map_fields(text_blocks: List[Dict], document_type: str,
                img_width: int, img_height: int) -> Dict:
    """Map text blocks to document fields using spatial layout and keywords."""
    field_defs = DOCUMENT_FIELDS.get(document_type, DOCUMENT_FIELDS['id_card_generic'])
    ocr_fields = {}

    # Group blocks by region
    region_blocks = {}
    for block in text_blocks:
        region = _get_region(block['bbox_rel'], img_height)
        if region not in region_blocks:
            region_blocks[region] = []
        region_blocks[region].append(block)

    # For each field, find best matching blocks
    for field_name, field_def in field_defs.items():
        target_region = field_def['region']
        keywords = field_def['keywords']

        # Collect candidate blocks from the target region and adjacent regions
        candidates = []
        for region, blocks in region_blocks.items():
            if region.startswith(target_region.split('_')[0]) or region == target_region:
                candidates.extend(blocks)

        if not candidates:
            continue

        # Try keyword matching first
        matched_blocks = []
        for block in text_blocks:
            text_lower = block['text'].lower()
            for kw in keywords:
                if kw.lower() in text_lower:
                    # Find the value block(s) near this keyword
                    # Look for blocks to the right or below the keyword
                    nearby = _find_nearby_value_blocks(block, text_blocks)
                    matched_blocks.extend(nearby)
                    break

        if matched_blocks:
            # Use matched blocks
            value = ' '.join(b['text'] for b in matched_blocks)
            avg_conf = sum(b['confidence'] for b in matched_blocks) / len(matched_blocks)
            bbox = matched_blocks[0]['bbox']
        elif candidates:
            # Fallback: use all text in the region
            value = ' '.join(b['text'] for b in candidates)
            avg_conf = sum(b['confidence'] for b in candidates) / len(candidates)
            bbox = candidates[0]['bbox']
        else:
            continue

        if value.strip():
            ocr_fields[field_name] = {
                'value': value.strip(),
                'confidence': round(avg_conf, 4),
                'bbox': bbox,
            }

    return ocr_fields


def _find_nearby_value_blocks(keyword_block: Dict, all_blocks: List[Dict],
                               max_distance: float = 0.15) -> List[Dict]:
    """Find value blocks near a keyword block (to the right or below)."""
    kw_rel = keyword_block['bbox_rel']
    kw_x2 = kw_rel['x2']
    kw_y1 = kw_rel['y1']
    kw_y2 = kw_rel['y2']

    nearby = []
    for block in all_blocks:
        if block is keyword_block:
            continue
        b_rel = block['bbox_rel']
        b_x1 = b_rel['x1']
        b_y1 = b_rel['y1']

        # To the right, same line
        if (abs(b_y1 - kw_y1) < 0.03 and b_x1 > kw_x2 and b_x1 - kw_x2 < max_distance):
            nearby.append(block)
        # Below, same column
        elif (abs(b_x1 - kw_rel['x1']) < 0.1 and b_y1 > kw_y2 and b_y1 - kw_y2 < 0.05):
            nearby.append(block)

    return nearby


def run_ocr(scan_id: int, prediction_id: int, video_id: int,
            crop_path: str, document_type: str) -> Optional[Dict]:
    """
    Run OCR on a cropped document image and submit results as a child prediction.

    Args:
        scan_id: document_scans record ID
        prediction_id: parent detection prediction ID
        video_id: video ID for the source image
        crop_path: path to the cropped document image
        document_type: detected document type

    Returns:
        Result dict with extracted fields, or None on failure
    """
    model = _get_ocr_model()
    if model is None:
        _update_scan_status(scan_id, 'failed')
        return None

    if not crop_path or not os.path.exists(crop_path):
        logger.warning(f"Document crop not found for scan {scan_id}: {crop_path}")
        _update_scan_status(scan_id, 'failed')
        return None

    try:
        from PIL import Image
        import numpy as np

        _update_scan_status(scan_id, 'processing')

        img = Image.open(crop_path).convert('RGB')
        img_width, img_height = img.size
        img_array = np.array(img)

        start_time = time.time()
        result = model([img_array])
        ocr_time_ms = (time.time() - start_time) * 1000

        text_blocks = _extract_text_blocks(result)
        logger.info(f"OCR scan {scan_id}: {len(text_blocks)} text blocks in {ocr_time_ms:.0f}ms")

        if not text_blocks:
            _update_scan_status(scan_id, 'completed')
            return {'scan_id': scan_id, 'fields': {}, 'text_blocks': 0}

        # Auto-classify if generic
        actual_type = document_type
        if document_type == 'id_card_generic':
            classified = _classify_document_type(text_blocks)
            if classified:
                actual_type = classified
                logger.info(f"OCR auto-classified scan {scan_id} as {actual_type}")
                _update_document_type(scan_id, actual_type)

        # Map text to fields
        ocr_fields = _map_fields(text_blocks, actual_type, img_width, img_height)

        # Build child prediction with OCR results
        prediction_payload = {
            'prediction_type': 'keyframe',
            'confidence': _avg_field_confidence(ocr_fields),
            'timestamp': 0.0,
            'scenario': 'document_ocr',
            'parent_prediction_id': prediction_id,
            'tags': {
                'class': actual_type,
                'document_type': actual_type,
                'ocr_fields': ocr_fields,
                'text_block_count': len(text_blocks),
                'ocr_time_ms': round(ocr_time_ms, 2),
            },
            'bbox': {'x': 0, 'y': 0, 'width': img_width, 'height': img_height},
            'inference_time_ms': round(ocr_time_ms, 2),
        }

        payload = {
            'video_id': video_id,
            'model_name': MODEL_NAME,
            'model_version': MODEL_VERSION,
            'model_type': MODEL_TYPE,
            'batch_id': f"doc-ocr-{int(time.time())}",
            'predictions': [prediction_payload],
            'force_review': True,
        }

        response = requests.post(
            f"{API_BASE_URL}/api/ai/predictions/batch",
            json=payload,
            headers={'X-Auth-Role': 'admin'},
            timeout=30
        )
        response.raise_for_status()

        _update_scan_status(scan_id, 'completed')

        return {
            'scan_id': scan_id,
            'document_type': actual_type,
            'fields': ocr_fields,
            'text_blocks': len(text_blocks),
            'ocr_time_ms': ocr_time_ms,
        }

    except Exception as e:
        logger.error(f"OCR failed for scan {scan_id}: {e}")
        _update_scan_status(scan_id, 'failed')
        return None


def _avg_field_confidence(ocr_fields: Dict) -> float:
    """Calculate average confidence across all extracted fields."""
    if not ocr_fields:
        return 0.0
    confs = [f['confidence'] for f in ocr_fields.values() if 'confidence' in f]
    return round(sum(confs) / len(confs), 4) if confs else 0.0


def _update_scan_status(scan_id: int, status: str):
    """Update document_scans OCR status."""
    try:
        from db_connection import get_cursor
        with get_cursor() as cursor:
            if status == 'completed':
                cursor.execute(
                    "UPDATE document_scans SET ocr_status = %s, ocr_completed_at = NOW() WHERE id = %s",
                    (status, scan_id)
                )
            else:
                cursor.execute(
                    "UPDATE document_scans SET ocr_status = %s WHERE id = %s",
                    (status, scan_id)
                )
    except Exception as e:
        logger.error(f"Failed to update scan {scan_id} status to {status}: {e}")


def _update_document_type(scan_id: int, document_type: str):
    """Update document_scans document_type after auto-classification."""
    try:
        from db_connection import get_cursor
        with get_cursor() as cursor:
            cursor.execute(
                "UPDATE document_scans SET document_type = %s WHERE id = %s",
                (document_type, scan_id)
            )
    except Exception as e:
        logger.error(f"Failed to update scan {scan_id} document_type: {e}")


def trigger_document_ocr(scan_id: int, prediction_id: int, video_id: int,
                         crop_path: str, document_type: str):
    """Fire-and-forget: run OCR in a background thread."""
    thread = threading.Thread(
        target=run_ocr,
        args=(scan_id, prediction_id, video_id, crop_path, document_type),
        daemon=True,
        name=f"doc-ocr-{scan_id}"
    )
    thread.start()
    logger.info(f"Document OCR triggered in background for scan {scan_id}")
