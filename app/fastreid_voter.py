"""
FastReID Voter — Nearest-neighbor classification voter using DINOv2 embeddings.

For each new prediction, finds the closest approved prediction by embedding
similarity and votes with that neighbor's classification. Provides a
visual-similarity-based signal to the multi-voter consensus system.
"""

import logging
from typing import Dict, List, Optional

import numpy as np

from db_connection import get_cursor

logger = logging.getLogger(__name__)

DEFAULT_SIMILARITY_THRESHOLD = 0.75
DEFAULT_MAX_CANDIDATES = 50


class FastReIDVoter:
    """Nearest-neighbor voter using DINOv2/ReID embeddings."""

    def __init__(self, similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
                 max_candidates: int = DEFAULT_MAX_CANDIDATES):
        self.similarity_threshold = similarity_threshold
        self.max_candidates = max_candidates

    def find_nearest_approved(self, prediction_id: int) -> Optional[dict]:
        """
        Find the nearest approved prediction by embedding similarity.

        Looks up the embedding for the given prediction via its video_track,
        then compares against approved predictions with embeddings.

        Returns:
            dict with prediction_id, classification, vehicle_tier1/2/3, similarity
            or None if no match above threshold
        """
        # Get the prediction's embedding via video_track
        with get_cursor(commit=False) as cursor:
            # First try via camera_object_track -> video_tracks
            cursor.execute("""
                SELECT vt.reid_embedding, p.scenario
                FROM ai_predictions p
                LEFT JOIN video_tracks vt ON vt.video_id = p.video_id
                    AND vt.camera_id = (SELECT v.camera_id FROM videos v WHERE v.id = p.video_id)
                WHERE p.id = %s
                  AND vt.reid_embedding IS NOT NULL
                LIMIT 1
            """, (prediction_id,))
            row = cursor.fetchone()

            if not row or not row['reid_embedding']:
                logger.debug("No embedding found for prediction %d", prediction_id)
                return None

            query_embedding = np.array(row['reid_embedding'], dtype=np.float32)
            scenario = row['scenario']

            # Find approved predictions with embeddings in the same scenario
            cursor.execute("""
                SELECT p.id, p.classification, p.vehicle_tier1, p.vehicle_tier2,
                       p.vehicle_tier3, p.vehicle_role, vt.reid_embedding
                FROM ai_predictions p
                JOIN video_tracks vt ON vt.video_id = p.video_id
                    AND vt.camera_id = (SELECT v.camera_id FROM videos v WHERE v.id = p.video_id)
                WHERE p.review_status = 'approved'
                  AND p.id != %s
                  AND vt.reid_embedding IS NOT NULL
                  AND p.scenario = %s
                ORDER BY p.quality_score DESC NULLS LAST
                LIMIT %s
            """, (prediction_id, scenario, self.max_candidates))

            candidates = cursor.fetchall()

        if not candidates:
            logger.debug("No approved candidates found for prediction %d", prediction_id)
            return None

        # Compute similarities
        best_match = None
        best_similarity = -1.0

        for cand in candidates:
            cand_embedding = np.array(cand['reid_embedding'], dtype=np.float32)
            similarity = self._cosine_similarity(query_embedding, cand_embedding)

            if similarity > best_similarity:
                best_similarity = similarity
                best_match = cand

        if best_similarity < self.similarity_threshold:
            logger.debug("Best match for prediction %d has similarity %.3f (below threshold %.3f)",
                        prediction_id, best_similarity, self.similarity_threshold)
            return None

        return {
            'prediction_id': best_match['id'],
            'classification': best_match['classification'],
            'vehicle_tier1': best_match['vehicle_tier1'],
            'vehicle_tier2': best_match['vehicle_tier2'],
            'vehicle_tier3': best_match['vehicle_tier3'],
            'vehicle_role': best_match['vehicle_role'],
            'similarity': round(float(best_similarity), 4),
        }

    def vote_for_prediction(self, prediction_id: int) -> Optional[dict]:
        """
        Find nearest approved match and record vote via VoteAggregator.

        Returns:
            The consensus result from VoteAggregator, or None if no match found
        """
        match = self.find_nearest_approved(prediction_id)
        if not match:
            return None

        from vote_aggregator import VoteAggregator
        aggregator = VoteAggregator()
        return aggregator.record_vote(
            prediction_id, 'fastreid_nn',
            voted_tier1=match['vehicle_tier1'],
            voted_tier2=match['vehicle_tier2'],
            voted_tier3=match['vehicle_tier3'],
            voted_role=match.get('vehicle_role'),
            confidence=match['similarity'],
            metadata={'nearest_prediction_id': match['prediction_id']}
        )

    def vote_batch(self, prediction_ids: List[int]) -> dict:
        """
        Process multiple predictions through FastReID voting.

        Returns:
            Summary dict with voted, skipped, errors counts
        """
        voted = 0
        skipped = 0
        errors = 0

        for pred_id in prediction_ids:
            try:
                result = self.vote_for_prediction(pred_id)
                if result:
                    voted += 1
                else:
                    skipped += 1
            except Exception as e:
                logger.warning("FastReID vote failed for prediction %d: %s", pred_id, e)
                errors += 1

        summary = {'voted': voted, 'skipped': skipped, 'errors': errors,
                    'total': len(prediction_ids)}
        logger.info("FastReID vote_batch: %s", summary)
        return summary

    @staticmethod
    def _cosine_similarity(vec_a: np.ndarray, vec_b: np.ndarray) -> float:
        """Compute cosine similarity between two vectors."""
        norm_a = np.linalg.norm(vec_a)
        norm_b = np.linalg.norm(vec_b)
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return float(np.dot(vec_a, vec_b) / (norm_a * norm_b))
