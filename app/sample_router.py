"""
Sample Router - Confidence-based routing for AI predictions.

Routes predictions into three buckets based on per-model configurable thresholds:
- auto_approve: High confidence predictions become training data automatically
- review: Medium confidence predictions queued for human review
- auto_reject: Low confidence predictions rejected automatically

Thresholds are configurable per model via the model_registry table.
"""

import logging
from typing import Dict, List, Optional

from db_connection import get_cursor
from psycopg2 import extras

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLDS = {
    'auto_approve': 0.95,
    'review': 0.7,
    'auto_reject': 0.3
}


class SampleRouter:
    """Routes AI predictions based on per-model confidence thresholds."""

    def __init__(self, db):
        """
        Args:
            db: VideoDatabase instance for database operations
        """
        self.db = db

    def get_thresholds(self, model_name: str, model_version: str) -> Dict:
        """
        Fetch thresholds from model_registry. Falls back to defaults.

        Threshold behavior:
        - confidence >= auto_approve -> auto-approve into training data
        - confidence >= review -> queue for human review
        - confidence < auto_reject -> auto-reject
        - Between auto_reject and review -> also queued for review
        """
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT confidence_thresholds FROM model_registry
                WHERE model_name = %s AND model_version = %s
            ''', (model_name, model_version))
            row = cursor.fetchone()
            if row and row['confidence_thresholds']:
                thresholds = row['confidence_thresholds']
                # Ensure all keys exist with defaults
                return {
                    'auto_approve': thresholds.get('auto_approve', DEFAULT_THRESHOLDS['auto_approve']),
                    'review': thresholds.get('review', DEFAULT_THRESHOLDS['review']),
                    'auto_reject': thresholds.get('auto_reject', DEFAULT_THRESHOLDS['auto_reject']),
                }
            return DEFAULT_THRESHOLDS.copy()

    def route_prediction(self, prediction: Dict, thresholds: Dict) -> str:
        """
        Determine routing decision for a single prediction.

        Args:
            prediction: Dict with at least 'confidence' key
            thresholds: Dict with 'auto_approve', 'review', 'auto_reject' keys

        Returns:
            'auto_approve', 'review', or 'auto_reject'
        """
        confidence = prediction['confidence']

        if confidence >= thresholds['auto_approve']:
            return 'auto_approve'
        elif confidence < thresholds['auto_reject']:
            return 'auto_reject'
        else:
            return 'review'

    def route_batch(self, prediction_ids: List[int], predictions: List[Dict],
                    model_name: str, model_version: str) -> Dict:
        """
        Route an entire batch of predictions.

        Args:
            prediction_ids: List of database IDs for the predictions
            predictions: List of prediction dicts (must include 'confidence')
            model_name: Model name for threshold lookup
            model_version: Model version for threshold lookup

        Returns:
            Dict with routing summary and lists per bucket
        """
        thresholds = self.get_thresholds(model_name, model_version)

        result = {
            'thresholds': thresholds,
            'auto_approved': [],
            'review': [],
            'auto_rejected': [],
            'counts': {'auto_approved': 0, 'review': 0, 'auto_rejected': 0},
        }

        for pred_id, pred in zip(prediction_ids, predictions):
            decision = self.route_prediction(pred, thresholds)

            if decision == 'auto_approve':
                result['auto_approved'].append(pred_id)
                result['counts']['auto_approved'] += 1
            elif decision == 'auto_reject':
                result['auto_rejected'].append(pred_id)
                result['counts']['auto_rejected'] += 1
            else:
                result['review'].append(pred_id)
                result['counts']['review'] += 1

        return result

    def apply_auto_decisions(self, routing_result: Dict, model_name: str,
                              model_version: str) -> Dict:
        """
        Apply automatic routing decisions to predictions.

        For auto_approve: mark as auto_approved and create training annotations.
        For auto_reject: mark as auto_rejected.
        For review: leave as pending (no action needed).

        Args:
            routing_result: Output from route_batch()
            model_name: Model name for stats update
            model_version: Model version for stats update

        Returns:
            Summary of actions taken
        """
        thresholds = routing_result['thresholds']
        annotations_created = 0
        auto_approved = 0
        auto_rejected = 0
        errors = []

        # Auto-approve high confidence predictions
        for pred_id in routing_result['auto_approved']:
            try:
                self.db.update_prediction_routing(
                    pred_id, 'auto_approved', 'auto_confidence', thresholds
                )
                annotation_id = self.db.approve_prediction_to_annotation(pred_id)
                if annotation_id:
                    annotations_created += 1
                auto_approved += 1
            except Exception as e:
                logger.error(f"Failed to auto-approve prediction {pred_id}: {e}")
                errors.append({'prediction_id': pred_id, 'error': str(e)})

        # Auto-reject low confidence predictions
        for pred_id in routing_result['auto_rejected']:
            try:
                self.db.update_prediction_routing(
                    pred_id, 'auto_rejected', 'auto_confidence', thresholds
                )
                auto_rejected += 1
            except Exception as e:
                logger.error(f"Failed to auto-reject prediction {pred_id}: {e}")
                errors.append({'prediction_id': pred_id, 'error': str(e)})

        # Update model approval stats
        try:
            self.db.update_model_approval_stats(model_name, model_version)
        except Exception as e:
            logger.warning(f"Failed to update model stats for {model_name} v{model_version}: {e}")

        return {
            'auto_approved': auto_approved,
            'auto_rejected': auto_rejected,
            'queued_for_review': len(routing_result['review']),
            'annotations_created': annotations_created,
            'errors': errors
        }

    def route_and_apply(self, prediction_ids: List[int], predictions: List[Dict],
                        model_name: str, model_version: str) -> Dict:
        """
        Convenience method: route a batch and apply decisions in one call.

        Returns combined routing + application summary.
        """
        routing = self.route_batch(prediction_ids, predictions, model_name, model_version)
        application = self.apply_auto_decisions(routing, model_name, model_version)

        return {
            'thresholds': routing['thresholds'],
            'routing_counts': routing['counts'],
            **application
        }

    def force_all_to_review(self, prediction_ids: List[int]) -> Dict:
        """Force all predictions to pending review, bypassing confidence-based routing."""
        with get_cursor(commit=True) as cursor:
            if prediction_ids:
                extras.execute_batch(cursor, '''
                    UPDATE ai_predictions
                    SET review_status = 'pending', routed_by = 'manual'
                    WHERE id = %s AND review_status != 'pending'
                ''', [(pid,) for pid in prediction_ids])

        return {
            'auto_approved': [],
            'review': prediction_ids,
            'auto_rejected': [],
            'errors': []
        }
