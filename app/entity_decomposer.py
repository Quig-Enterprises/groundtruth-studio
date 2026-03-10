"""
Entity Decomposer — Decomposes compound/multi-entity detections into separate per-entity
bounding boxes with relationship labels using the VLM (llama3.2-vision via Ollama).

Handles "multiple_vehicles" or compound detections by asking the VLM to identify
individual entities and their relationships (is_towing, is_carrying, is_loaded_on).
"""

import base64
import json
import logging
import subprocess
from io import BytesIO
from typing import Dict, List, Optional

import requests
from PIL import Image

from db_connection import get_cursor

logger = logging.getLogger(__name__)

OLLAMA_URL = 'http://localhost:11434'
VLM_MODEL = 'llama3.2-vision'
VLM_TIMEOUT = 45


class EntityDecomposer:
    """Decomposes compound detections into individual entity predictions."""

    def __init__(self, ollama_url: str = OLLAMA_URL, vlm_model: str = VLM_MODEL):
        self.ollama_url = ollama_url
        self.vlm_model = vlm_model

    def decompose_compound(self, prediction_id: int) -> List[int]:
        """
        Decompose a compound detection into individual entity predictions.

        Uses VLM to identify individual entities within the compound bbox,
        creates child predictions with parent_entity_prediction_id pointing
        to the original.

        Returns:
            List of created child prediction IDs
        """
        # Fetch the prediction
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT p.id, p.video_id, p.model_name, p.model_version,
                       p.scenario, p.timestamp, p.bbox_x, p.bbox_y,
                       p.bbox_width, p.bbox_height, p.confidence,
                       v.filename
                FROM ai_predictions p
                JOIN videos v ON v.id = p.video_id
                WHERE p.id = %s
            """, (prediction_id,))
            pred = cursor.fetchone()

        if not pred:
            logger.warning("Prediction %d not found", prediction_id)
            return []

        # Extract frame
        video_path = pred['filename']
        timestamp = pred['timestamp'] or 0
        frame_bytes = self._extract_frame(video_path, timestamp)
        if not frame_bytes:
            logger.warning("Could not extract frame for prediction %d", prediction_id)
            return []

        # Crop the detection region with padding
        full_frame = Image.open(BytesIO(frame_bytes)).convert('RGB')
        bbox = {
            'x': pred['bbox_x'] or 0, 'y': pred['bbox_y'] or 0,
            'width': pred['bbox_width'] or 0, 'height': pred['bbox_height'] or 0
        }
        crop = self._crop_with_padding(full_frame, bbox, padding=0.15)

        # Encode crop for VLM
        crop_b64 = self._image_to_base64(crop)

        # Query VLM for entity decomposition
        entities = self._query_vlm_decompose(crop_b64)
        if not entities:
            logger.info("VLM found no decomposable entities in prediction %d", prediction_id)
            return []

        # Create child predictions
        child_ids = []
        crop_w, crop_h = crop.size
        parent_bbox_x = bbox['x'] - int(bbox['width'] * 0.15)
        parent_bbox_y = bbox['y'] - int(bbox['height'] * 0.15)

        with get_cursor() as cursor:
            for entity in entities:
                # Convert percentage bbox to absolute pixels
                pct = entity.get('bbox_pct', [0, 0, 100, 100])
                if len(pct) < 4:
                    continue

                child_x = max(0, int(parent_bbox_x + crop_w * pct[0] / 100))
                child_y = max(0, int(parent_bbox_y + crop_h * pct[1] / 100))
                child_w = max(1, int(crop_w * pct[2] / 100))
                child_h = max(1, int(crop_h * pct[3] / 100))

                classification = entity.get('classification', 'unknown')
                relationship = entity.get('relationship')
                if relationship == 'none':
                    relationship = None

                cursor.execute("""
                    INSERT INTO ai_predictions
                        (video_id, model_name, model_version, prediction_type,
                         confidence, timestamp, bbox_x, bbox_y, bbox_width, bbox_height,
                         scenario, predicted_tags, review_status,
                         parent_entity_prediction_id, entity_relationship,
                         classification)
                    VALUES (%s, %s, %s, 'keyframe', %s, %s, %s, %s, %s, %s,
                            %s, %s, 'pending', %s, %s, %s)
                    RETURNING id
                """, (
                    pred['video_id'], pred['model_name'], pred['model_version'],
                    pred['confidence'] * 0.8,  # slightly lower confidence for decomposed
                    pred['timestamp'],
                    child_x, child_y, child_w, child_h,
                    pred['scenario'],
                    json.dumps({'class': classification, 'decomposed_from': prediction_id}),
                    prediction_id, relationship, classification
                ))
                child_row = cursor.fetchone()
                if child_row:
                    child_ids.append(child_row['id'])

        logger.info("Decomposed prediction %d into %d child entities: %s",
                    prediction_id, len(child_ids), child_ids)
        return child_ids

    def queue_for_decomposition(self, limit: int = 100) -> List[int]:
        """
        Find compound predictions that haven't been decomposed yet.

        Returns list of prediction IDs eligible for decomposition.
        """
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT p.id
                FROM ai_predictions p
                WHERE (
                    p.classification ILIKE '%%multiple%%'
                    OR p.predicted_tags->>'class' ILIKE '%%multiple%%'
                    OR p.corrected_tags->>'actual_class' ILIKE '%%multiple%%'
                )
                AND NOT EXISTS (
                    SELECT 1 FROM ai_predictions child
                    WHERE child.parent_entity_prediction_id = p.id
                )
                ORDER BY p.created_at DESC
                LIMIT %s
            """, (limit,))
            return [row['id'] for row in cursor.fetchall()]

    def decompose_batch(self, limit: int = 50) -> dict:
        """
        Queue and decompose compound predictions.

        Returns summary dict.
        """
        queued = self.queue_for_decomposition(limit)
        total_children = 0
        errors = 0

        for pred_id in queued:
            try:
                children = self.decompose_compound(pred_id)
                total_children += len(children)
            except Exception as e:
                logger.warning("Decomposition failed for prediction %d: %s", pred_id, e)
                errors += 1

        summary = {
            'queued': len(queued),
            'children_created': total_children,
            'errors': errors
        }
        logger.info("Decompose batch complete: %s", summary)
        return summary

    def _extract_frame(self, video_path: str, timestamp: float) -> Optional[bytes]:
        """Extract a single frame from video at the given timestamp using ffmpeg."""
        try:
            cmd = [
                'ffmpeg', '-ss', str(float(timestamp)),
                '-i', str(video_path),
                '-frames:v', '1',
                '-f', 'image2pipe',
                '-vcodec', 'mjpeg',
                '-q:v', '2', '-'
            ]
            result = subprocess.run(cmd, capture_output=True, timeout=10)
            if result.returncode == 0 and result.stdout:
                return result.stdout
            return None
        except Exception as e:
            logger.warning("Frame extraction error: %s", e)
            return None

    @staticmethod
    def _crop_with_padding(image: Image.Image, bbox: dict, padding: float = 0.15) -> Image.Image:
        """Crop the detection region with padding for context."""
        x, y = bbox.get('x', 0), bbox.get('y', 0)
        w, h = bbox.get('width', 0), bbox.get('height', 0)
        pad_w, pad_h = w * padding, h * padding

        x1 = max(0, int(x - pad_w))
        y1 = max(0, int(y - pad_h))
        x2 = min(image.width, int(x + w + pad_w))
        y2 = min(image.height, int(y + h + pad_h))

        if x2 <= x1 or y2 <= y1:
            return image
        return image.crop((x1, y1, x2, y2))

    @staticmethod
    def _image_to_base64(image: Image.Image) -> str:
        """Convert PIL Image to base64 string."""
        buf = BytesIO()
        image.save(buf, format='JPEG', quality=85)
        return base64.b64encode(buf.getvalue()).decode('utf-8')

    def _query_vlm_decompose(self, image_b64: str) -> Optional[List[dict]]:
        """Send image to VLM for entity decomposition."""
        prompt = (
            'This image shows a compound vehicle detection containing multiple entities.\n'
            'Identify each distinct entity (vehicle, trailer, boat, motorcycle, person).\n'
            'For each entity, provide:\n'
            '1. classification (e.g., pickup truck, boat trailer, pontoon boat)\n'
            '2. approximate bounding box as percentages of the image: [x%, y%, width%, height%]\n'
            '3. relationship to other entities (is_towing, is_carrying, is_loaded_on, or none)\n\n'
            'Respond ONLY with JSON: {"entities": [{"classification": "...", '
            '"bbox_pct": [x, y, w, h], "relationship": "...", '
            '"relationship_target_index": 0}]}'
        )

        payload = {
            'model': self.vlm_model,
            'prompt': prompt,
            'images': [image_b64],
            'stream': False,
            'options': {'temperature': 0.1, 'num_predict': 512}
        }

        try:
            resp = requests.post(
                f'{self.ollama_url}/api/generate',
                json=payload, timeout=VLM_TIMEOUT
            )
            resp.raise_for_status()
            response_text = resp.json().get('response', '')
            return self._parse_decompose_response(response_text)
        except Exception as e:
            logger.warning("VLM decompose query failed: %s", e)
            return None

    @staticmethod
    def _parse_decompose_response(response_text: str) -> Optional[List[dict]]:
        """Parse VLM decomposition response."""
        import re
        try:
            result = json.loads(response_text.strip())
        except json.JSONDecodeError:
            match = re.search(r'\{[^{}]*"entities"[^{}]*\[.*?\]\s*\}', response_text, re.DOTALL)
            if not match:
                logger.warning("Could not parse VLM decompose response: %s", response_text[:200])
                return None
            try:
                result = json.loads(match.group())
            except json.JSONDecodeError:
                return None

        entities = result.get('entities', [])
        if not isinstance(entities, list) or len(entities) < 2:
            return None  # Need at least 2 entities for decomposition

        # Validate entity structure
        valid = []
        for e in entities:
            if isinstance(e, dict) and 'classification' in e:
                valid.append({
                    'classification': str(e.get('classification', 'unknown')),
                    'bbox_pct': e.get('bbox_pct', [0, 0, 100, 100]),
                    'relationship': str(e.get('relationship', 'none')),
                })
        return valid if len(valid) >= 2 else None
