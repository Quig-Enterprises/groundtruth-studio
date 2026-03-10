"""
Cargo-on-Trailer Classification — identifies what's loaded on trailers.

Uses VLM to identify cargo type and count, then infers trailer type
and enforcement eligibility from cargo contents.
"""

import logging
from typing import Dict, Optional

from db_connection import get_cursor

logger = logging.getLogger(__name__)

# Cargo type to trailer type inference
CARGO_TO_TRAILER_TYPE = {
    'boat': ('boat_trailer', True),       # (tier2, enforcement_eligible)
    'motorboat': ('boat_trailer', True),
    'powerboat': ('boat_trailer', True),
    'sailboat': ('boat_trailer', False),
    'kayak': ('boat_trailer', False),      # non-motorized
    'canoe': ('boat_trailer', False),
    'paddleboard': ('boat_trailer', False),
    'jet ski': ('boat_trailer', True),
    'snowmobile': ('snowmobile_trailer', False),
    'atv': ('utility_trailer', False),
    'utv': ('utility_trailer', False),
    'vehicle': ('utility_trailer', False),
    'car': ('utility_trailer', False),
    'equipment': ('utility_trailer', False),
    'lumber': ('utility_trailer', False),
    'landscaping': ('utility_trailer', False),
    'nothing': ('utility_trailer', False),
    'empty': ('utility_trailer', False),
}


class CargoClassifier:
    """Classifies cargo on trailers using VLM."""

    def classify_cargo(self, prediction_id: int) -> Optional[Dict]:
        """Classify cargo for a trailer prediction using VLM.

        Args:
            prediction_id: ID of the trailer prediction

        Returns:
            dict with cargo_type, cargo_count, inferred_trailer_type, enforcement_eligible
        """
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT p.id, p.video_id, p.timestamp, p.bbox_x, p.bbox_y,
                       p.bbox_width, p.bbox_height, p.vehicle_tier1,
                       v.filename
                FROM ai_predictions p
                JOIN videos v ON v.id = p.video_id
                WHERE p.id = %s
            """, (prediction_id,))
            pred = cursor.fetchone()

        if not pred:
            return None

        # Only process trailer predictions
        if pred.get('vehicle_tier1') not in ('trailer', None):
            return None

        bbox = {
            'x': pred['bbox_x'], 'y': pred['bbox_y'],
            'width': pred['bbox_width'], 'height': pred['bbox_height']
        }

        # Query VLM for cargo identification
        try:
            from vlm_reviewer import _extract_frame, _crop_with_padding, _image_to_base64
            from PIL import Image
            from io import BytesIO
            import requests
            import json

            video_path = pred.get('filename')
            if not video_path:
                return None

            frame_bytes = _extract_frame(video_path, pred['timestamp'] or 0)
            if not frame_bytes:
                return None

            full_frame = Image.open(BytesIO(frame_bytes)).convert('RGB')
            crop = _crop_with_padding(full_frame, bbox, padding=0.3)
            crop_b64 = _image_to_base64(crop)

            prompt = (
                'This image shows a trailer. '
                'What is loaded on this trailer? '
                'Identify the cargo type (boat, snowmobile, ATV, UTV, vehicle, jet ski, '
                'kayak, canoe, equipment, landscaping, nothing/empty) and count. '
                'Respond ONLY with JSON: {"cargo_type": "...", "cargo_count": int}'
            )

            resp = requests.post(
                'http://localhost:11434/api/generate',
                json={
                    'model': 'llama3.2-vision',
                    'prompt': prompt,
                    'images': [crop_b64],
                    'stream': False,
                    'options': {'temperature': 0.1, 'num_predict': 128}
                },
                timeout=30
            )
            resp.raise_for_status()
            response_text = resp.json().get('response', '')

            # Parse response
            result = json.loads(response_text.strip())
            cargo_type = str(result.get('cargo_type', 'unknown')).lower().strip()
            cargo_count = int(result.get('cargo_count', 0))

        except Exception as e:
            logger.warning(f"Cargo classification failed for prediction {prediction_id}: {e}")
            return None

        # Infer trailer type and enforcement eligibility
        trailer_info = CARGO_TO_TRAILER_TYPE.get(cargo_type, ('utility_trailer', False))
        inferred_tier2, enforcement_eligible = trailer_info

        # Update prediction with cargo info
        with get_cursor() as cursor:
            cursor.execute("""
                UPDATE ai_predictions
                SET cargo_type = %s,
                    cargo_count = %s,
                    vehicle_tier2 = COALESCE(vehicle_tier2, %s),
                    enforcement_eligible = %s
                WHERE id = %s
            """, (cargo_type, cargo_count, inferred_tier2, enforcement_eligible, prediction_id))

        return {
            'prediction_id': prediction_id,
            'cargo_type': cargo_type,
            'cargo_count': cargo_count,
            'inferred_trailer_type': inferred_tier2,
            'enforcement_eligible': enforcement_eligible,
        }

    def determine_enforcement_eligibility(self, cargo_type: str) -> bool:
        """Determine if cargo makes a trailer enforcement-eligible.

        Only motorized boats on trailers are enforcement-eligible.
        """
        info = CARGO_TO_TRAILER_TYPE.get(cargo_type.lower().strip(), (None, False))
        return info[1]
