"""
Vote Aggregator — Multi-voter consensus system for vehicle classification.

Multiple independent voters (YOLO-World, VLM, FastReID, spatial scale, seasonal prior)
contribute votes per prediction. Consensus determines review queue routing:
- All/most agree → select_all (fast batch approval)
- Majority agree → review (individual inspection)
- No consensus → triage (careful review, high training value)
"""

import logging
from typing import Dict, List, Optional
from datetime import datetime, timedelta, timezone

from db_connection import get_cursor

logger = logging.getLogger(__name__)


class VoteAggregator:
    """Aggregates classification votes from multiple independent voters."""

    def record_vote(self, prediction_id: int, voter: str,
                    voted_tier1: Optional[str] = None,
                    voted_tier2: Optional[str] = None,
                    voted_tier3: Optional[str] = None,
                    voted_role: Optional[str] = None,
                    voted_cargo: Optional[str] = None,
                    confidence: Optional[float] = None,
                    metadata: Optional[dict] = None) -> dict:
        """
        Record a classification vote from a voter.

        UPSERT into classification_votes, then recompute consensus.

        Returns:
            The consensus result dict from compute_consensus()
        """
        with get_cursor() as cursor:
            cursor.execute("""
                INSERT INTO classification_votes
                    (prediction_id, voter, voted_tier1, voted_tier2, voted_tier3,
                     voted_role, voted_cargo, confidence, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (prediction_id, voter) DO UPDATE SET
                    voted_tier1 = EXCLUDED.voted_tier1,
                    voted_tier2 = EXCLUDED.voted_tier2,
                    voted_tier3 = EXCLUDED.voted_tier3,
                    voted_role = EXCLUDED.voted_role,
                    voted_cargo = EXCLUDED.voted_cargo,
                    confidence = EXCLUDED.confidence,
                    metadata = EXCLUDED.metadata,
                    created_at = NOW()
            """, (prediction_id, voter, voted_tier1, voted_tier2, voted_tier3,
                  voted_role, voted_cargo, confidence,
                  __import__('json').dumps(metadata) if metadata else None))

        return self.compute_consensus(prediction_id)

    def compute_consensus(self, prediction_id: int) -> dict:
        """
        Compute voter consensus for a prediction.

        Counts agreements at tier1 and tier2 levels, determines consensus level,
        and sets review_queue on the prediction.

        Returns:
            dict with voter_count, voter_agreement, consensus_tier, review_queue
        """
        votes = self.get_votes(prediction_id)
        if not votes:
            return {
                'voter_count': 0, 'voter_agreement': 0,
                'consensus_tier': 'none', 'review_queue': 'triage'
            }

        voter_count = len(votes)

        # Count tier2 agreements (primary consensus signal)
        tier2_votes = [v['voted_tier2'] for v in votes if v['voted_tier2']]
        tier1_votes = [v['voted_tier1'] for v in votes if v['voted_tier1']]

        # Find most common tier2 vote
        tier2_agreement = 0
        consensus_tier2 = None
        if tier2_votes:
            from collections import Counter
            tier2_counts = Counter(tier2_votes)
            consensus_tier2, tier2_agreement = tier2_counts.most_common(1)[0]

        # Find most common tier1 vote
        tier1_agreement = 0
        if tier1_votes:
            from collections import Counter
            tier1_counts = Counter(tier1_votes)
            _, tier1_agreement = tier1_counts.most_common(1)[0]

        # Use tier2 agreement as primary signal
        voter_agreement = tier2_agreement

        # Determine consensus level
        if voter_count == 1:
            # Single voter: check confidence
            conf = votes[0].get('confidence') or 0
            if conf >= 0.8:
                consensus_tier = 'high'
                review_queue = 'select_all'
            else:
                consensus_tier = 'none'
                review_queue = 'review'
        elif tier2_agreement == voter_count:
            # All voters agree on tier2
            consensus_tier = 'high'
            review_queue = 'select_all'
        elif tier2_agreement > voter_count / 2:
            # Majority agree on tier2
            consensus_tier = 'majority'
            review_queue = 'review'
        elif tier1_agreement == voter_count:
            # All agree on tier1 but not tier2
            consensus_tier = 'majority'
            review_queue = 'review'
        else:
            # No consensus
            consensus_tier = 'none'
            review_queue = 'triage'

        # Apply seasonal prior adjustment to routing
        try:
            from seasonal_prior import SeasonalPrior
            sp = SeasonalPrior()
            if consensus_tier2:
                prior_weight = sp.get_prior_weight(consensus_tier2)
                if prior_weight < 0.3 and review_queue == 'select_all':
                    # Out-of-season detection — demote to review
                    review_queue = 'review'
                    logger.debug("Seasonal prior demoted prediction %d (weight=%.2f)",
                                 prediction_id, prior_weight)
        except Exception:
            pass  # Seasonal priors not available

        # Update prediction
        with get_cursor() as cursor:
            cursor.execute("""
                UPDATE ai_predictions SET
                    voter_count = %s,
                    voter_agreement = %s,
                    consensus_tier = %s,
                    review_queue = %s
                WHERE id = %s
            """, (voter_count, voter_agreement, consensus_tier, review_queue,
                  prediction_id))

        result = {
            'voter_count': voter_count,
            'voter_agreement': voter_agreement,
            'consensus_tier': consensus_tier,
            'review_queue': review_queue,
            'consensus_tier2': consensus_tier2,
        }
        logger.debug("Consensus for prediction %d: %s", prediction_id, result)
        return result

    def get_votes(self, prediction_id: int) -> List[dict]:
        """Return all votes for a prediction as list of dicts."""
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT voter, voted_tier1, voted_tier2, voted_tier3,
                       voted_role, voted_cargo, confidence, metadata, created_at
                FROM classification_votes
                WHERE prediction_id = %s
                ORDER BY created_at
            """, (prediction_id,))
            return [dict(row) for row in cursor.fetchall()]

    def get_disagreement_stats(self, hours: int = 168) -> List[dict]:
        """
        Return per-class-pair disagreement counts for the last N hours.

        Finds predictions where voters disagree on tier2, groups by the
        disagreeing class pairs.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                WITH vote_pairs AS (
                    SELECT
                        v1.prediction_id,
                        v1.voted_tier2 AS class_a,
                        v2.voted_tier2 AS class_b
                    FROM classification_votes v1
                    JOIN classification_votes v2
                        ON v1.prediction_id = v2.prediction_id
                        AND v1.voter < v2.voter
                    WHERE v1.voted_tier2 IS NOT NULL
                      AND v2.voted_tier2 IS NOT NULL
                      AND v1.voted_tier2 != v2.voted_tier2
                      AND v1.created_at >= %s
                )
                SELECT
                    LEAST(class_a, class_b) AS class_a,
                    GREATEST(class_a, class_b) AS class_b,
                    COUNT(DISTINCT prediction_id) AS disagreement_count
                FROM vote_pairs
                GROUP BY LEAST(class_a, class_b), GREATEST(class_a, class_b)
                ORDER BY disagreement_count DESC
            """, (cutoff,))
            return [dict(row) for row in cursor.fetchall()]

    def get_voter_accuracy(self, voter: str, hours: int = 168) -> dict:
        """
        Compare a voter's votes to final human-approved classification.

        Returns accuracy stats for the voter.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE cv.voted_tier2 = p.vehicle_tier2) AS tier2_correct,
                    COUNT(*) FILTER (WHERE cv.voted_tier1 = p.vehicle_tier1) AS tier1_correct,
                    COUNT(*) FILTER (WHERE cv.voted_role = p.vehicle_role
                                     AND cv.voted_role IS NOT NULL) AS role_correct
                FROM classification_votes cv
                JOIN ai_predictions p ON cv.prediction_id = p.id
                WHERE cv.voter = %s
                  AND cv.created_at >= %s
                  AND p.review_status = 'approved'
                  AND p.vehicle_tier2 IS NOT NULL
            """, (voter, cutoff))
            row = cursor.fetchone()

            total = row['total'] or 0
            return {
                'voter': voter,
                'total_reviewed': total,
                'tier1_accuracy': round(row['tier1_correct'] / total, 4) if total > 0 else None,
                'tier2_accuracy': round(row['tier2_correct'] / total, 4) if total > 0 else None,
                'role_accuracy': round(row['role_correct'] / total, 4) if total > 0 else None,
                'period_hours': hours,
            }
