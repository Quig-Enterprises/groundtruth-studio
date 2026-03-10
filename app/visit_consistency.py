"""
Visit Consistency Checker — Flags entities that change classification within a visit.

When the same vehicle appears on multiple cameras during a single visit, its
classification should be consistent. This module detects mismatches and creates
flags for human review.
"""

import logging
from typing import Dict, List, Optional

from db_connection import get_cursor

logger = logging.getLogger(__name__)


class VisitConsistencyChecker:
    """Checks cross-camera classification consistency within visits."""

    def check_visit(self, visit_id: str) -> List[dict]:
        """
        Check a single visit for classification inconsistencies.

        Queries all tracks linked to the visit, gets their predictions and
        classifications, and compares across cameras for the same identity.

        Returns:
            List of flag dicts that were created
        """
        flags_created = []

        with get_cursor() as cursor:
            # Get visit and its track IDs
            cursor.execute("""
                SELECT visit_id, track_ids, vehicle_identity_id, person_identity_id
                FROM visits WHERE visit_id = %s
            """, (visit_id,))
            visit = cursor.fetchone()
            if not visit or not visit['track_ids']:
                return []

            identity_id = visit['vehicle_identity_id']
            track_ids = visit['track_ids']

            # Get classifications per camera from predictions linked via tracks
            # Use camera_object_tracks which aggregate per-camera predictions
            cursor.execute("""
                SELECT DISTINCT
                    t.camera_id,
                    COALESCE(p.vehicle_tier1, p.predicted_tags->>'class') AS tier1,
                    COALESCE(p.vehicle_tier2, p.classification) AS tier2,
                    p.vehicle_tier3 AS tier3
                FROM tracks t
                JOIN ai_predictions p ON p.video_id IN (
                    SELECT v.id FROM videos v WHERE v.camera_id = t.camera_id
                )
                WHERE t.track_id = ANY(%s)
                  AND p.review_status IN ('approved', 'auto_approved')
                  AND (p.vehicle_tier2 IS NOT NULL OR p.classification IS NOT NULL)
                  AND p.created_at >= t.start_time - INTERVAL '5 minutes'
                  AND (t.end_time IS NULL OR p.created_at <= t.end_time + INTERVAL '5 minutes')
                ORDER BY t.camera_id
            """, (track_ids,))
            camera_classes = cursor.fetchall()

            if len(camera_classes) < 2:
                return []

            # Compare all pairs
            seen_pairs = set()
            for i, a in enumerate(camera_classes):
                for b in camera_classes[i+1:]:
                    if a['camera_id'] == b['camera_id']:
                        continue

                    # Normalize pair order for dedup
                    pair_key = tuple(sorted([a['camera_id'], b['camera_id']]))
                    class_key = (pair_key, a.get('tier2'), b.get('tier2'))
                    if class_key in seen_pairs:
                        continue
                    seen_pairs.add(class_key)

                    tier1_a = (a.get('tier1') or '').lower()
                    tier1_b = (b.get('tier1') or '').lower()
                    tier2_a = (a.get('tier2') or '').lower()
                    tier2_b = (b.get('tier2') or '').lower()

                    flag_type = None
                    class_a_val = tier2_a or tier1_a
                    class_b_val = tier2_b or tier1_b

                    if not class_a_val or not class_b_val:
                        continue
                    if class_a_val == class_b_val:
                        continue

                    if tier1_a and tier1_b and tier1_a != tier1_b:
                        flag_type = 'tier_mismatch'
                    elif tier2_a and tier2_b and tier2_a != tier2_b:
                        flag_type = 'classification_change'

                    if not flag_type:
                        continue

                    cam_a, cam_b = pair_key
                    cursor.execute("""
                        INSERT INTO visit_consistency_flags
                            (visit_id, identity_id, camera_a, camera_b,
                             class_a, class_b, flag_type)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT DO NOTHING
                        RETURNING id, visit_id, camera_a, camera_b, class_a, class_b, flag_type
                    """, (visit_id, identity_id, cam_a, cam_b,
                          class_a_val, class_b_val, flag_type))
                    row = cursor.fetchone()
                    if row:
                        flags_created.append(dict(row))

        if flags_created:
            logger.info("Visit %s: created %d consistency flags", visit_id, len(flags_created))
        return flags_created

    def run_retroactive_audit(self, limit: int = 1000) -> dict:
        """
        Audit existing visits for classification consistency.

        Returns:
            dict with visits_checked, flags_created counts
        """
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT visit_id FROM visits
                WHERE vehicle_identity_id IS NOT NULL
                  AND array_length(track_ids, 1) >= 2
                ORDER BY arrival_time DESC
                LIMIT %s
            """, (limit,))
            visit_ids = [row['visit_id'] for row in cursor.fetchall()]

        total_flags = 0
        for vid in visit_ids:
            try:
                flags = self.check_visit(str(vid))
                total_flags += len(flags)
            except Exception as e:
                logger.warning("Consistency check failed for visit %s: %s", vid, e)

        summary = {'visits_checked': len(visit_ids), 'flags_created': total_flags}
        logger.info("Retroactive audit complete: %s", summary)
        return summary

    def get_flags(self, visit_id: Optional[str] = None,
                  resolved: Optional[bool] = None,
                  limit: int = 100) -> List[dict]:
        """Query visit_consistency_flags with optional filters."""
        conditions = []
        params = []

        if visit_id is not None:
            conditions.append("visit_id = %s")
            params.append(visit_id)
        if resolved is not None:
            conditions.append("resolved = %s")
            params.append(resolved)

        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        params.append(limit)

        with get_cursor(commit=False) as cursor:
            cursor.execute(f"""
                SELECT id, visit_id, identity_id, camera_a, camera_b,
                       class_a, class_b, flag_type, resolved, resolution, created_at
                FROM visit_consistency_flags
                {where}
                ORDER BY created_at DESC
                LIMIT %s
            """, params)
            return [dict(row) for row in cursor.fetchall()]

    def resolve_flag(self, flag_id: int, resolution: str) -> Optional[dict]:
        """Mark a flag as resolved with resolution text."""
        with get_cursor() as cursor:
            cursor.execute("""
                UPDATE visit_consistency_flags
                SET resolved = TRUE, resolution = %s
                WHERE id = %s
                RETURNING id, visit_id, flag_type, resolution
            """, (resolution, flag_id))
            row = cursor.fetchone()
            return dict(row) if row else None
