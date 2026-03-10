"""
License Plate OCR Pipeline — detects and reads license plates from PTZ captures.

Uses VLM for plate region detection and text extraction,
with format validation against known state/province patterns.
"""

import logging
import re
from typing import Dict, List, Optional

from db_connection import get_cursor

logger = logging.getLogger(__name__)

# Common US/Canadian plate format patterns
PLATE_PATTERNS = {
    'US_standard': r'^[A-Z0-9]{5,8}$',
    'US_vanity': r'^[A-Z0-9 ]{3,8}$',
    'CA_standard': r'^[A-Z]{3,4}[- ]?\d{3,4}$',
}


class PlateReader:
    """License plate detection, OCR, and validation."""

    def detect_plate_region(self, image_b64: str) -> Optional[Dict]:
        """Locate license plate region in an image using VLM.

        Args:
            image_b64: Base64-encoded image

        Returns:
            dict with plate bbox (x, y, width, height) and confidence
        """
        try:
            import requests
            import json

            prompt = (
                'Find any license plate in this image. '
                'If you can see a license plate, describe its approximate location '
                'as a percentage of the image dimensions. '
                'Respond with JSON: {"found": true/false, "x_pct": 0-100, "y_pct": 0-100, '
                '"w_pct": 0-100, "h_pct": 0-100, "confidence": 0.0-1.0} '
                'If no plate is visible, respond: {"found": false}'
            )

            resp = requests.post(
                'http://localhost:11434/api/generate',
                json={
                    'model': 'llama3.2-vision',
                    'prompt': prompt,
                    'images': [image_b64],
                    'stream': False,
                    'options': {'temperature': 0.1, 'num_predict': 128}
                },
                timeout=30
            )
            resp.raise_for_status()
            response_text = resp.json().get('response', '')
            result = json.loads(response_text.strip())

            if not result.get('found'):
                return None

            return {
                'x_pct': result.get('x_pct', 0),
                'y_pct': result.get('y_pct', 0),
                'w_pct': result.get('w_pct', 0),
                'h_pct': result.get('h_pct', 0),
                'confidence': result.get('confidence', 0.0),
            }

        except Exception as e:
            logger.warning(f"Plate detection failed: {e}")
            return None

    def extract_plate_text(self, image_b64: str) -> Optional[Dict]:
        """Extract plate text from an image using VLM.

        Args:
            image_b64: Base64-encoded image (ideally cropped to plate region)

        Returns:
            dict with plate_text, confidence, state_guess
        """
        try:
            import requests
            import json

            prompt = (
                'Read the license plate text in this image. '
                'If you can read a plate, provide the text exactly as shown. '
                'Also guess the US state or Canadian province if possible. '
                'Respond with JSON: {"plate_text": "ABC1234", "confidence": 0.0-1.0, '
                '"state_guess": "..." or null}'
            )

            resp = requests.post(
                'http://localhost:11434/api/generate',
                json={
                    'model': 'llama3.2-vision',
                    'prompt': prompt,
                    'images': [image_b64],
                    'stream': False,
                    'options': {'temperature': 0.1, 'num_predict': 128}
                },
                timeout=30
            )
            resp.raise_for_status()
            response_text = resp.json().get('response', '')
            result = json.loads(response_text.strip())

            plate_text = result.get('plate_text', '').strip().upper()
            if not plate_text:
                return None

            return {
                'plate_text': plate_text,
                'confidence': result.get('confidence', 0.0),
                'state_guess': result.get('state_guess'),
            }

        except Exception as e:
            logger.warning(f"Plate text extraction failed: {e}")
            return None

    def validate_plate_format(self, text: str, state: str = None) -> Dict:
        """Check if plate text matches known format patterns.

        Args:
            text: Plate text string
            state: Optional US state or CA province code

        Returns:
            dict with is_valid, matched_pattern, cleaned_text
        """
        cleaned = re.sub(r'[^A-Z0-9]', '', text.upper())

        for pattern_name, pattern in PLATE_PATTERNS.items():
            if re.match(pattern, cleaned):
                return {
                    'is_valid': True,
                    'matched_pattern': pattern_name,
                    'cleaned_text': cleaned,
                }

        return {
            'is_valid': False,
            'matched_pattern': None,
            'cleaned_text': cleaned,
        }

    def link_to_identity(self, plate_text: str, prediction_id: int) -> Optional[str]:
        """Create or update an identity with plate metadata.

        Args:
            plate_text: Cleaned plate text
            prediction_id: Source prediction ID

        Returns:
            identity_id string if linked, None otherwise
        """
        import uuid
        import json

        with get_cursor() as cursor:
            # Check if plate already known
            cursor.execute("""
                SELECT identity_id FROM identities
                WHERE metadata->>'plate_number' = %s
                LIMIT 1
            """, (plate_text,))
            row = cursor.fetchone()

            if row:
                identity_id = row['identity_id']
                # Add sighting
                cursor.execute("""
                    INSERT INTO sightings (identity_id, prediction_id, sighting_type)
                    VALUES (%s, %s, 'plate_ocr')
                    ON CONFLICT DO NOTHING
                """, (identity_id, prediction_id))
                return str(identity_id)

            # Create new identity
            identity_id = uuid.uuid4()
            cursor.execute("""
                INSERT INTO identities (identity_id, identity_type, metadata)
                VALUES (%s, 'vehicle', %s::jsonb)
            """, (str(identity_id), json.dumps({'plate_number': plate_text})))

            # Link prediction
            cursor.execute("""
                INSERT INTO sightings (identity_id, prediction_id, sighting_type)
                VALUES (%s, %s, 'plate_ocr')
                ON CONFLICT DO NOTHING
            """, (str(identity_id), prediction_id))

            return str(identity_id)

    def process_prediction(self, prediction_id: int) -> Optional[Dict]:
        """Full pipeline: detect plate, extract text, validate, link.

        Args:
            prediction_id: Prediction ID to process

        Returns:
            dict with plate_text, confidence, identity_id, or None
        """
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT p.id, p.video_id, p.timestamp,
                       p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
                       v.filename
                FROM ai_predictions p
                JOIN videos v ON v.id = p.video_id
                WHERE p.id = %s
            """, (prediction_id,))
            pred = cursor.fetchone()

        if not pred:
            return None

        try:
            from vlm_reviewer import _extract_frame, _crop_with_padding, _image_to_base64
            from PIL import Image
            from io import BytesIO

            frame_bytes = _extract_frame(pred['filename'], pred['timestamp'] or 0)
            if not frame_bytes:
                return None

            full_frame = Image.open(BytesIO(frame_bytes)).convert('RGB')
            bbox = {
                'x': pred['bbox_x'], 'y': pred['bbox_y'],
                'width': pred['bbox_width'], 'height': pred['bbox_height']
            }

            # Crop rear area (lower portion of bbox where plate typically is)
            rear_bbox = {
                'x': bbox['x'],
                'y': bbox['y'] + bbox['height'] * 0.5,
                'width': bbox['width'],
                'height': bbox['height'] * 0.5,
            }
            crop = _crop_with_padding(full_frame, rear_bbox, padding=0.2)
            crop_b64 = _image_to_base64(crop)

            # Extract text directly from crop
            text_result = self.extract_plate_text(crop_b64)
            if not text_result:
                return None

            # Validate
            validation = self.validate_plate_format(
                text_result['plate_text'],
                text_result.get('state_guess')
            )

            if not validation['is_valid']:
                return None

            # Link to identity
            identity_id = self.link_to_identity(
                validation['cleaned_text'], prediction_id
            )

            return {
                'prediction_id': prediction_id,
                'plate_text': validation['cleaned_text'],
                'confidence': text_result['confidence'],
                'state_guess': text_result.get('state_guess'),
                'identity_id': identity_id,
            }

        except Exception as e:
            logger.warning(f"Plate processing failed for prediction {prediction_id}: {e}")
            return None
