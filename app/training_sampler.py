"""
Training Data Sampler — Deduplication, class rebalancing, and day/night separation.

Operates on ai_predictions using quality scores, track membership, and class
distribution to produce a balanced, deduplicated training set for YOLO export.
"""

import logging
import statistics
from typing import Dict, List, Optional

from db_connection import get_cursor

logger = logging.getLogger(__name__)

DAY_START_HOUR = 6
DAY_END_HOUR = 18
MIN_TIER3_SAMPLES = 10


class TrainingSampler:
    """Manages training data selection from ai_predictions."""

    def select_best_per_track(self, camera_id: Optional[str] = None) -> dict:
        """
        For each camera_object_track, pick the prediction with highest quality_score
        as best_crop. Mark others as duplicate_track.

        Returns:
            {tracks_processed, duplicates_excluded}
        """
        tracks_processed = 0
        duplicates_excluded = 0

        with get_cursor(commit=False) as cursor:
            conditions = ["camera_object_track_id IS NOT NULL"]
            params = []
            if camera_id:
                # Join to get camera_id from video
                cursor.execute("""
                    SELECT DISTINCT p.camera_object_track_id
                    FROM ai_predictions p
                    JOIN videos v ON v.id = p.video_id
                    WHERE p.camera_object_track_id IS NOT NULL
                      AND v.camera_id = %s
                """, (camera_id,))
            else:
                cursor.execute("""
                    SELECT DISTINCT camera_object_track_id
                    FROM ai_predictions
                    WHERE camera_object_track_id IS NOT NULL
                """)
            track_rows = cursor.fetchall()

        track_ids = [row['camera_object_track_id'] for row in track_rows]

        for track_id in track_ids:
            with get_cursor() as cursor:
                cursor.execute("""
                    SELECT id, quality_score
                    FROM ai_predictions
                    WHERE camera_object_track_id = %s
                    ORDER BY quality_score DESC NULLS LAST, id ASC
                """, (track_id,))
                preds = cursor.fetchall()
                if not preds:
                    continue

                best_id = preds[0]['id']
                dup_ids = [r['id'] for r in preds[1:]]

                cursor.execute("""
                    UPDATE camera_object_tracks
                    SET best_crop_prediction_id = %s
                    WHERE id = %s
                """, (best_id, track_id))

                if dup_ids:
                    cursor.execute("""
                        UPDATE ai_predictions
                        SET is_training_candidate = FALSE,
                            training_exclusion_reason = 'duplicate_track'
                        WHERE id = ANY(%s)
                    """, (dup_ids,))
                    duplicates_excluded += len(dup_ids)

                tracks_processed += 1

        logger.info("select_best_per_track: %d tracks, %d duplicates excluded",
                    tracks_processed, duplicates_excluded)
        return {'tracks_processed': tracks_processed, 'duplicates_excluded': duplicates_excluded}

    def compute_class_balance(self, scenario: Optional[str] = None) -> Dict[str, int]:
        """
        Count approved training-eligible predictions per classification.

        Uses vehicle_tier2 when available, falls back to classification.
        """
        with get_cursor(commit=False) as cursor:
            sql = """
                SELECT COALESCE(NULLIF(vehicle_tier2, ''), classification) AS class_name,
                       COUNT(*) AS cnt
                FROM ai_predictions
                WHERE review_status = 'approved'
                  AND (is_training_candidate IS NULL OR is_training_candidate = TRUE)
            """
            params = []
            if scenario:
                sql += " AND scenario = %s"
                params.append(scenario)
            sql += " GROUP BY class_name ORDER BY cnt DESC"
            cursor.execute(sql, params)
            return {r['class_name']: r['cnt'] for r in cursor.fetchall() if r['class_name']}

    def apply_rebalancing(self, target_per_class: Optional[int] = None,
                          strategy: str = 'undersample') -> dict:
        """
        Cap overrepresented classes to target_per_class samples.

        Preserves hard examples (voter disagreements, human corrections).
        Ensures minimum MIN_TIER3_SAMPLES per tier3 class.
        """
        distribution = self.compute_class_balance()
        if not distribution:
            return {'classes_rebalanced': 0, 'samples_excluded': 0}

        if target_per_class is None:
            target_per_class = int(statistics.median(distribution.values()))

        classes_rebalanced = 0
        samples_excluded = 0

        for class_name, count in distribution.items():
            if count <= target_per_class:
                continue
            excess = count - target_per_class

            with get_cursor() as cursor:
                # Find excess non-hard-example predictions to exclude
                cursor.execute("""
                    SELECT id FROM ai_predictions
                    WHERE review_status = 'approved'
                      AND (is_training_candidate IS NULL OR is_training_candidate = TRUE)
                      AND COALESCE(NULLIF(vehicle_tier2, ''), classification) = %s
                      AND NOT (voter_agreement IS NOT NULL AND voter_count IS NOT NULL
                               AND voter_agreement < voter_count)
                      AND corrected_tags IS NULL
                    ORDER BY quality_score ASC NULLS FIRST, id ASC
                    LIMIT %s
                """, (class_name, excess))
                ids = [r['id'] for r in cursor.fetchall()]
                if ids:
                    cursor.execute("""
                        UPDATE ai_predictions
                        SET is_training_candidate = FALSE,
                            training_exclusion_reason = 'class_overrepresented'
                        WHERE id = ANY(%s)
                    """, (ids,))
                    samples_excluded += len(ids)
                    classes_rebalanced += 1

        logger.info("Rebalancing: target=%d, %d classes, %d excluded",
                    target_per_class, classes_rebalanced, samples_excluded)
        return {'classes_rebalanced': classes_rebalanced, 'samples_excluded': samples_excluded}

    def split_day_night(self) -> dict:
        """
        Classify approved training predictions as day or night by timestamp hour.
        """
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT COALESCE(NULLIF(vehicle_tier2, ''), classification) AS class_name,
                       EXTRACT(HOUR FROM created_at AT TIME ZONE 'UTC') AS hour
                FROM ai_predictions
                WHERE review_status = 'approved'
                  AND (is_training_candidate IS NULL OR is_training_candidate = TRUE)
                  AND created_at IS NOT NULL
            """)
            rows = cursor.fetchall()

        per_class_day: Dict[str, int] = {}
        per_class_night: Dict[str, int] = {}
        day_count = night_count = 0

        for row in rows:
            cls = row['class_name'] or 'unknown'
            hour = int(row['hour'])
            if DAY_START_HOUR <= hour < DAY_END_HOUR:
                day_count += 1
                per_class_day[cls] = per_class_day.get(cls, 0) + 1
            else:
                night_count += 1
                per_class_night[cls] = per_class_night.get(cls, 0) + 1

        return {
            'day_count': day_count, 'night_count': night_count,
            'per_class_day': dict(sorted(per_class_day.items(), key=lambda x: -x[1])),
            'per_class_night': dict(sorted(per_class_night.items(), key=lambda x: -x[1])),
        }

    def get_sampling_report(self) -> dict:
        """Return comprehensive snapshot of training set state."""
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT COALESCE(training_exclusion_reason, 'none') AS reason,
                       COUNT(*) AS cnt
                FROM ai_predictions
                GROUP BY reason ORDER BY cnt DESC
            """)
            exclusions = {r['reason']: r['cnt'] for r in cursor.fetchall()}

            cursor.execute("""
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE is_training_candidate = TRUE
                                        OR is_training_candidate IS NULL) AS candidates,
                       COUNT(*) FILTER (WHERE is_training_candidate = FALSE) AS excluded
                FROM ai_predictions
            """)
            totals = cursor.fetchone()

        return {
            'class_distribution': self.compute_class_balance(),
            'day_night_split': self.split_day_night(),
            'exclusion_breakdown': exclusions,
            'total_predictions': totals['total'],
            'total_candidates': totals['candidates'],
            'total_excluded': totals['excluded'],
        }

    def reset_sampling(self) -> dict:
        """Reset all sampling decisions."""
        with get_cursor() as cursor:
            cursor.execute("""
                UPDATE ai_predictions
                SET is_training_candidate = TRUE, training_exclusion_reason = NULL
                WHERE is_training_candidate = FALSE OR training_exclusion_reason IS NOT NULL
            """)
            rows = cursor.rowcount
        logger.info("Reset sampling: %d predictions reset", rows)
        return {'rows_reset': rows}
