"""
Document training data exporter.

Two export modes:
  - Detection export: YOLO format for document bounding box detector retraining
  - OCR export: DocTR fine-tuning format (images/ + labels/ with word boxes and text)

Uses corrected_tags when available (human-reviewed corrections take priority).
"""

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Dict, List, Optional

from db_connection import get_cursor

logger = logging.getLogger(__name__)

# Document class IDs for YOLO detection export
DETECTION_CLASSES = {
    'passport': 0,
    'drivers_license': 1,
    'twic_card': 2,
    'merchant_mariner_credential': 3,
    'id_card_generic': 4,
}


class DocumentTrainingExporter:
    """Export document training data in YOLO and DocTR formats."""

    def __init__(self, output_dir: str = '/opt/groundtruth-studio/exports/documents'):
        self.output_dir = output_dir

    def export_detection_dataset(self, val_split: float = 0.2) -> Dict:
        """Export YOLO-format detection dataset for document bounding box training.

        Output structure:
            output_dir/detection/
                data.yaml
                train/images/  train/labels/
                val/images/    val/labels/
        """
        det_dir = os.path.join(self.output_dir, 'detection')
        for split in ('train', 'val'):
            os.makedirs(os.path.join(det_dir, split, 'images'), exist_ok=True)
            os.makedirs(os.path.join(det_dir, split, 'labels'), exist_ok=True)

        # Fetch approved document detection predictions
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT p.id, p.video_id, p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
                       p.predicted_tags, p.corrected_tags,
                       v.thumbnail_path, v.video_width, v.video_height
                FROM ai_predictions p
                JOIN videos v ON v.id = p.video_id
                WHERE p.scenario = 'document_detection'
                  AND p.review_status IN ('approved', 'auto_approved')
                  AND p.bbox_width > 0 AND p.bbox_height > 0
                ORDER BY p.id
            """)
            predictions = [dict(row) for row in cursor.fetchall()]

        if not predictions:
            logger.warning("No approved document detections found for export")
            return {'exported': 0}

        # Split into train/val
        import random
        random.shuffle(predictions)
        split_idx = int(len(predictions) * (1 - val_split))
        splits = {
            'train': predictions[:split_idx],
            'val': predictions[split_idx:],
        }

        exported = 0
        for split_name, preds in splits.items():
            for pred in preds:
                tags = pred.get('corrected_tags') or pred.get('predicted_tags') or {}
                if isinstance(tags, str):
                    tags = json.loads(tags)

                doc_class = tags.get('class', tags.get('document_type', 'id_card_generic'))
                class_id = DETECTION_CLASSES.get(doc_class, 4)

                # Get image dimensions
                img_path = pred['thumbnail_path']
                if not img_path or not os.path.exists(img_path):
                    continue

                img_w = pred.get('video_width')
                img_h = pred.get('video_height')
                if not img_w or not img_h:
                    try:
                        from PIL import Image
                        with Image.open(img_path) as img:
                            img_w, img_h = img.size
                    except Exception:
                        continue

                # Normalize bbox to YOLO format (center x, center y, width, height, all 0-1)
                bx = pred['bbox_x']
                by = pred['bbox_y']
                bw = pred['bbox_width']
                bh = pred['bbox_height']

                x_center = (bx + bw / 2) / img_w
                y_center = (by + bh / 2) / img_h
                w_norm = bw / img_w
                h_norm = bh / img_h

                # Copy image
                img_dest = os.path.join(det_dir, split_name, 'images', f"{pred['id']}.jpg")
                if not os.path.exists(img_dest):
                    shutil.copy2(img_path, img_dest)

                # Write label
                label_path = os.path.join(det_dir, split_name, 'labels', f"{pred['id']}.txt")
                with open(label_path, 'a') as f:
                    f.write(f"{class_id} {x_center:.6f} {y_center:.6f} {w_norm:.6f} {h_norm:.6f}\n")

                exported += 1

        # Write data.yaml
        class_names = sorted(DETECTION_CLASSES.keys(), key=lambda k: DETECTION_CLASSES[k])
        yaml_content = {
            'path': det_dir,
            'train': 'train/images',
            'val': 'val/images',
            'nc': len(DETECTION_CLASSES),
            'names': class_names,
        }
        import yaml
        with open(os.path.join(det_dir, 'data.yaml'), 'w') as f:
            yaml.dump(yaml_content, f, default_flow_style=False)

        logger.info(f"Detection export complete: {exported} predictions "
                     f"(train: {len(splits['train'])}, val: {len(splits['val'])})")
        return {
            'exported': exported,
            'train': len(splits['train']),
            'val': len(splits['val']),
            'output_dir': det_dir,
        }

    def export_ocr_dataset(self) -> Dict:
        """Export DocTR fine-tuning dataset from OCR predictions.

        Output structure:
            output_dir/ocr/
                images/    -- cropped document images
                labels/    -- JSON files with word-level bounding boxes and text
        """
        ocr_dir = os.path.join(self.output_dir, 'ocr')
        os.makedirs(os.path.join(ocr_dir, 'images'), exist_ok=True)
        os.makedirs(os.path.join(ocr_dir, 'labels'), exist_ok=True)

        # Fetch approved OCR predictions with their document scans
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT p.id, p.predicted_tags, p.corrected_tags,
                       ds.crop_image_path, ds.document_type
                FROM ai_predictions p
                JOIN ai_predictions parent ON parent.id = p.parent_prediction_id
                JOIN document_scans ds ON ds.prediction_id = parent.id
                WHERE p.scenario = 'document_ocr'
                  AND p.review_status IN ('approved', 'auto_approved')
                ORDER BY p.id
            """)
            ocr_preds = [dict(row) for row in cursor.fetchall()]

        if not ocr_preds:
            logger.warning("No approved OCR predictions found for export")
            return {'exported': 0}

        exported = 0
        for pred in ocr_preds:
            crop_path = pred.get('crop_image_path')
            if not crop_path or not os.path.exists(crop_path):
                continue

            # Use corrected_tags if available (human corrections), else predicted_tags
            tags = pred.get('corrected_tags') or pred.get('predicted_tags') or {}
            if isinstance(tags, str):
                tags = json.loads(tags)

            ocr_fields = tags.get('ocr_fields', {})
            if not ocr_fields:
                continue

            # Copy crop image
            img_dest = os.path.join(ocr_dir, 'images', f"{pred['id']}.jpg")
            shutil.copy2(crop_path, img_dest)

            # Build DocTR label format: list of word-level annotations
            words = []
            for field_name, field_data in ocr_fields.items():
                if isinstance(field_data, dict) and 'value' in field_data:
                    word_entry = {
                        'text': field_data['value'],
                        'field': field_name,
                    }
                    if 'bbox' in field_data:
                        word_entry['bbox'] = field_data['bbox']
                    words.append(word_entry)

            label = {
                'prediction_id': pred['id'],
                'document_type': pred.get('document_type', 'unknown'),
                'words': words,
            }

            label_path = os.path.join(ocr_dir, 'labels', f"{pred['id']}.json")
            with open(label_path, 'w') as f:
                json.dump(label, f, indent=2)

            exported += 1

        logger.info(f"OCR export complete: {exported} documents")
        return {
            'exported': exported,
            'output_dir': ocr_dir,
        }
