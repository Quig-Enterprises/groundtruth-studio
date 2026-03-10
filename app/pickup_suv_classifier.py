"""
Pickup-SUV Binary Tiebreaker — resolves the most common classification confusion.

Lightweight binary classifier specifically trained on the pickup/SUV boundary.
Invoked only when other voters disagree on these two classes.
Registers as a voter in the voting system.
"""

import logging
from typing import Dict, Optional

from db_connection import get_cursor

logger = logging.getLogger(__name__)

# Visual cues that distinguish pickups from SUVs
PICKUP_INDICATORS = [
    'open bed', 'truck bed', 'tailgate', 'bed liner',
    'tonneau cover', 'bed cap', 'cargo bed',
]

SUV_INDICATORS = [
    'enclosed rear', 'rear window', 'rear hatch', 'liftgate',
    'third row', 'cargo area enclosed',
]


class PickupSUVClassifier:
    """Binary tiebreaker for pickup vs SUV classification."""

    def classify(self, prediction_id: int) -> Optional[Dict]:
        """Classify a prediction as pickup or SUV using VLM tiebreaker.

        Only invoked when voters disagree on pickup vs SUV.

        Args:
            prediction_id: ID of the disputed prediction

        Returns:
            dict with classification, confidence, reasoning
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
            import requests
            import json
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
            crop = _crop_with_padding(full_frame, bbox, padding=0.15)
            crop_b64 = _image_to_base64(crop)

            prompt = (
                'This is either a pickup truck or an SUV. Which is it? '
                'Look for these distinguishing features: '
                '- Pickup truck: has an open cargo bed behind the cabin, visible tailgate '
                '- SUV: has an enclosed rear cargo area with a rear window or hatch '
                'Respond ONLY with JSON: '
                '{"classification": "pickup" or "SUV", "confidence": 0.0-1.0, '
                '"reasoning": "brief explanation"}'
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
            result = json.loads(response_text.strip())

            classification = result.get('classification', '').lower().strip()
            if classification not in ('pickup', 'suv'):
                return None

            # Map to tier2 names
            tier2 = 'pickup' if classification == 'pickup' else 'small_vehicle'

            return {
                'prediction_id': prediction_id,
                'classification': classification,
                'tier2': tier2,
                'confidence': result.get('confidence', 0.5),
                'reasoning': result.get('reasoning', ''),
            }

        except Exception as e:
            logger.warning(f"Pickup-SUV classification failed for {prediction_id}: {e}")
            return None

    def should_invoke(self, prediction_id: int) -> bool:
        """Check if this prediction has pickup/SUV voter disagreement.

        Returns True if different voters disagree specifically on pickup vs SUV.
        """
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT voter, voted_tier2
                FROM classification_votes
                WHERE prediction_id = %s
                  AND voted_tier2 IN ('pickup', 'small_vehicle')
            """, (prediction_id,))
            votes = cursor.fetchall()

        if len(votes) < 2:
            return False

        tier2_values = set(v['voted_tier2'] for v in votes)
        return len(tier2_values) > 1

    def resolve_and_vote(self, prediction_id: int) -> Optional[Dict]:
        """Check if tiebreaker is needed, classify, and record vote.

        Returns:
            Classification result dict, or None if not needed/failed
        """
        if not self.should_invoke(prediction_id):
            return None

        result = self.classify(prediction_id)
        if not result:
            return None

        # Record as voter
        try:
            from vote_aggregator import VoteAggregator
            aggregator = VoteAggregator()
            aggregator.record_vote(
                prediction_id=prediction_id,
                voter='pickup_suv_tiebreaker',
                voted_tier1='vehicle',
                voted_tier2=result['tier2'],
                confidence=result['confidence'],
                metadata={'reasoning': result['reasoning']}
            )
        except Exception as e:
            logger.debug(f"Could not record tiebreaker vote: {e}")

        return result
