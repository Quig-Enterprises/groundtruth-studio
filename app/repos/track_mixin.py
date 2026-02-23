import json
from datetime import datetime
from typing import List, Dict, Optional

from psycopg2 import extras

from db_connection import get_cursor, get_connection


class TrackMixin:
    """Camera object tracks and interpolation tracks."""

    # --------------- Camera Object Track Methods ---------------

    def get_camera_object_track(self, track_id):
        """Get a camera object track with its representative prediction details."""
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT t.*, p.video_id, p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
                       p.confidence, p.timestamp, p.predicted_tags, p.corrected_tags,
                       p.scenario as pred_scenario, p.model_name, p.model_version,
                       v.title as video_title, v.thumbnail_path,
                       v.width as video_width, v.height as video_height
                FROM camera_object_tracks t
                LEFT JOIN ai_predictions p ON p.id = t.representative_prediction_id
                LEFT JOIN videos v ON p.video_id = v.id
                WHERE t.id = %s
            """, (track_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_track_members(self, track_id):
        """Get all predictions belonging to a track."""
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT p.id, p.video_id, p.model_name, p.model_version,
                       p.confidence, p.timestamp, p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
                       p.scenario, p.predicted_tags, p.corrected_tags,
                       p.review_status, p.reviewed_by, p.reviewed_at, p.review_notes,
                       p.created_annotation_id,
                       v.title as video_title, v.thumbnail_path, v.camera_id
                FROM ai_predictions p
                JOIN videos v ON p.video_id = v.id
                WHERE p.camera_object_track_id = %s
                ORDER BY p.review_status, p.confidence DESC
            """, (track_id,))
            return [dict(r) for r in cursor.fetchall()]

    def get_tracks_for_camera(self, camera_id, scenario=None, anchor_status=None):
        """Get all tracks for a camera with optional filters."""
        with get_cursor(commit=False) as cursor:
            conditions = ["camera_id = %s"]
            params = [camera_id]
            if scenario:
                conditions.append("scenario = %s")
                params.append(scenario)
            if anchor_status:
                conditions.append("anchor_status = %s")
                params.append(anchor_status)
            cursor.execute(f"""
                SELECT * FROM camera_object_tracks
                WHERE {' AND '.join(conditions)}
                ORDER BY member_count DESC
            """, params)
            return [dict(r) for r in cursor.fetchall()]

    def get_track_conflicts(self, camera_id=None):
        """Get tracks with review or classification conflicts needing manual resolution."""
        with get_cursor(commit=False) as cursor:
            conditions = ["(t.anchor_status = 'conflict' OR t.classification_conflict = TRUE)"]
            params = []
            if camera_id:
                conditions.append("t.camera_id = %s")
                params.append(camera_id)
            cursor.execute(f"""
                SELECT t.*,
                       p.video_id, p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
                       p.confidence, p.timestamp, p.predicted_tags,
                       v.title as video_title, v.thumbnail_path,
                       v.width as video_width, v.height as video_height
                FROM camera_object_tracks t
                LEFT JOIN ai_predictions p ON p.id = t.representative_prediction_id
                LEFT JOIN videos v ON p.video_id = v.id
                WHERE {' AND '.join(conditions)}
                ORDER BY t.member_count DESC
            """, params if params else None)
            return [dict(r) for r in cursor.fetchall()]

    def resolve_track_conflict(self, track_id, decision, reviewer='studio_user',
                                vehicle_subtype=None, actual_class=None):
        """Resolve a conflict on a track by applying a definitive decision.

        Args:
            track_id: track ID
            decision: 'approve' or 'reject'
            reviewer: who is resolving
            vehicle_subtype: optional classification to apply (for approve)
            actual_class: optional reclassification class (for reject)

        Returns:
            dict with counts
        """
        results = {'updated': 0, 'annotations_created': 0, 'classified': 0}
        status = 'approved' if decision == 'approve' else 'rejected'

        with get_cursor() as cursor:
            # Update all pending members
            if status == 'rejected' and actual_class:
                cursor.execute("""
                    UPDATE ai_predictions
                    SET review_status = %s,
                        reviewed_by = %s,
                        reviewed_at = NOW(),
                        review_notes = %s,
                        corrected_tags = COALESCE(corrected_tags, '{}'::jsonb) ||
                            jsonb_build_object(
                                'actual_class', %s,
                                'reclassified_by', %s,
                                'reclassified_at', NOW()::text
                            )
                    WHERE camera_object_track_id = %s
                      AND review_status = 'pending'
                    RETURNING id
                """, (
                    status, reviewer,
                    f"Conflict resolved by {reviewer}: {decision}",
                    actual_class, reviewer,
                    track_id
                ))
            else:
                cursor.execute("""
                    UPDATE ai_predictions
                    SET review_status = %s,
                        reviewed_by = %s,
                        reviewed_at = NOW(),
                        review_notes = %s
                    WHERE camera_object_track_id = %s
                      AND review_status = 'pending'
                    RETURNING id
                """, (
                    status, reviewer,
                    f"Conflict resolved by {reviewer}: {decision}",
                    track_id
                ))
            updated = cursor.fetchall()
            results['updated'] = len(updated)

            # Update track status
            cursor.execute("""
                UPDATE camera_object_tracks
                SET anchor_status = %s, classification_conflict = FALSE, updated_at = NOW()
                WHERE id = %s
            """, (status, track_id))

        # Increment class usage for reclassified rejections
        if decision == 'reject' and actual_class and results['updated'] > 0:
            self.increment_class_usage(actual_class)

        # Create annotations for approvals
        if decision == 'approve':
            for row in updated:
                ann_id = self.approve_prediction_to_annotation(row['id'])
                if ann_id:
                    results['annotations_created'] += 1

            # Apply classification if provided
            if vehicle_subtype:
                with get_cursor() as cursor:
                    cursor.execute("""
                        UPDATE ai_predictions
                        SET corrected_tags = COALESCE(corrected_tags, '{}'::jsonb) || %s::jsonb
                        WHERE camera_object_track_id = %s
                          AND scenario = 'vehicle_detection'
                          AND (corrected_tags IS NULL OR corrected_tags->>'vehicle_subtype' IS NULL)
                    """, (
                        extras.Json({
                            'vehicle_subtype': vehicle_subtype,
                            'classified_by': reviewer
                        }),
                        track_id
                    ))
                    results['classified'] = cursor.rowcount

                    # Update track classification
                    cursor.execute("""
                        UPDATE camera_object_tracks
                        SET anchor_classification = %s, classification_conflict = FALSE
                        WHERE id = %s
                    """, (extras.Json({'vehicle_subtype': vehicle_subtype}), track_id))

        return results

    def get_track_summary_stats(self, camera_id=None):
        """Get summary statistics for all tracks."""
        with get_cursor(commit=False) as cursor:
            conditions = []
            params = []
            if camera_id:
                conditions.append("camera_id = %s")
                params.append(camera_id)
            where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

            cursor.execute(f"""
                SELECT
                    anchor_status,
                    COUNT(*) as track_count,
                    SUM(member_count) as total_predictions,
                    SUM(pending_count) as total_pending,
                    SUM(approved_count) as total_approved,
                    SUM(rejected_count) as total_rejected,
                    SUM(auto_approved_count) as total_auto_approved,
                    COUNT(*) FILTER (WHERE classification_conflict) as classification_conflicts
                FROM camera_object_tracks
                {where}
                GROUP BY anchor_status
                ORDER BY anchor_status
            """, params if params else None)
            return [dict(r) for r in cursor.fetchall()]

    def get_unique_entity_count(self, camera_ids=None, entity_type='vehicle'):
        """Count unique entities across cameras (de-duplicated via cross_camera_identity_id).

        Tracks WITH an identity: COUNT(DISTINCT cross_camera_identity_id)
        Tracks WITHOUT an identity: COUNT(*) (each assumed unique)
        Total unique = identified_unique + unlinked_count
        """
        scenario = 'vehicle_detection' if entity_type == 'vehicle' else entity_type + '_detection'
        with get_cursor(commit=False) as cursor:
            conditions = [
                "scenario = %s",
                "anchor_status IN ('approved', 'conflict')"
            ]
            params = [scenario]
            if camera_ids:
                conditions.append("camera_id = ANY(%s)")
                params.append(camera_ids)

            where = "WHERE " + " AND ".join(conditions)

            cursor.execute(f"""
                SELECT
                    COUNT(*) as total_tracks,
                    COUNT(cross_camera_identity_id) as linked_tracks,
                    COUNT(DISTINCT cross_camera_identity_id) as unique_identities,
                    COUNT(*) - COUNT(cross_camera_identity_id) as unlinked_tracks
                FROM camera_object_tracks
                {where}
            """, params)
            row = dict(cursor.fetchone())
            row['unique_entities'] = row['unique_identities'] + row['unlinked_tracks']
            return row

    # ==================== Interpolation Tracks ====================

    def create_interpolation_track(self, video_id: int, class_name: str,
                                    start_pred_id: int, end_pred_id: int,
                                    start_ts: float, end_ts: float,
                                    batch_id: str = None) -> int:
        """Create a new interpolation track record. Returns track ID."""
        with get_cursor() as cursor:
            cursor.execute('''
                INSERT INTO interpolation_tracks
                (video_id, class_name, start_prediction_id, end_prediction_id,
                 start_timestamp, end_timestamp, batch_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            ''', (video_id, class_name, start_pred_id, end_pred_id,
                  start_ts, end_ts, batch_id))
            return cursor.fetchone()['id']

    def update_interpolation_track(self, track_id: int, status: str = None,
                                    frames_generated: int = None,
                                    frames_detected: int = None,
                                    reviewed_by: str = None) -> bool:
        """Update an interpolation track's status and/or counts."""
        updates = []
        values = []
        if status is not None:
            updates.append('status = %s')
            values.append(status)
        if frames_generated is not None:
            updates.append('frames_generated = %s')
            values.append(frames_generated)
        if frames_detected is not None:
            updates.append('frames_detected = %s')
            values.append(frames_detected)
        if reviewed_by is not None:
            updates.append('reviewed_by = %s')
            values.append(reviewed_by)
            updates.append('reviewed_at = NOW()')

        if not updates:
            return False

        values.append(track_id)
        with get_cursor() as cursor:
            cursor.execute(f'''
                UPDATE interpolation_tracks SET {', '.join(updates)}
                WHERE id = %s
            ''', values)
            return cursor.rowcount > 0

    def get_interpolation_tracks(self, video_id: int = None, status: str = None,
                                  limit: int = 50, offset: int = 0) -> Dict:
        """Get interpolation tracks, optionally filtered by video_id and/or status.

        Args:
            video_id: Optional video ID filter
            status: Optional status filter
            limit: Number of tracks to return (default 50)
            offset: Number of tracks to skip (default 0)

        Returns:
            Dict with 'tracks', 'total', and 'has_more' keys
        """
        with get_cursor(commit=False) as cursor:
            conditions = []
            params = []
            if video_id is not None:
                conditions.append('t.video_id = %s')
                params.append(video_id)
            if status is not None:
                conditions.append('t.status = %s')
                params.append(status)

            where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''

            # Get total count first
            cursor.execute(f'''
                SELECT COUNT(*) as count
                FROM interpolation_tracks t
                JOIN videos v ON t.video_id = v.id
                {where}
            ''', params)
            total = cursor.fetchone()['count']

            # Get paginated results
            paginated_params = params + [limit, offset]
            cursor.execute(f'''
                SELECT t.*, v.filename as video_filename, v.title as video_title
                FROM interpolation_tracks t
                JOIN videos v ON t.video_id = v.id
                {where}
                ORDER BY t.created_at DESC
                LIMIT %s OFFSET %s
            ''', paginated_params)
            rows = cursor.fetchall()
            tracks = [dict(row) for row in rows]

            return {
                'tracks': tracks,
                'total': total,
                'has_more': offset + len(tracks) < total
            }

    def get_interpolation_track(self, track_id: int) -> Optional[Dict]:
        """Get a single interpolation track with anchor prediction details."""
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT t.*,
                       v.filename as video_filename, v.title as video_title,
                       v.width as video_width, v.height as video_height,
                       sp.timestamp as start_pred_timestamp,
                       sp.bbox_x as start_bbox_x, sp.bbox_y as start_bbox_y,
                       sp.bbox_width as start_bbox_width, sp.bbox_height as start_bbox_height,
                       sp.confidence as start_confidence,
                       sp.predicted_tags as start_predicted_tags,
                       sp.corrected_tags as start_corrected_tags,
                       sp.corrected_bbox as start_corrected_bbox,
                       ep.timestamp as end_pred_timestamp,
                       ep.bbox_x as end_bbox_x, ep.bbox_y as end_bbox_y,
                       ep.bbox_width as end_bbox_width, ep.bbox_height as end_bbox_height,
                       ep.confidence as end_confidence,
                       ep.predicted_tags as end_predicted_tags,
                       ep.corrected_tags as end_corrected_tags,
                       ep.corrected_bbox as end_corrected_bbox
                FROM interpolation_tracks t
                JOIN videos v ON t.video_id = v.id
                LEFT JOIN ai_predictions sp ON t.start_prediction_id = sp.id
                LEFT JOIN ai_predictions ep ON t.end_prediction_id = ep.id
                WHERE t.id = %s
            ''', (track_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_track_predictions(self, batch_id: str) -> List[Dict]:
        """Get all predictions belonging to an interpolation track batch."""
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT * FROM ai_predictions
                WHERE batch_id = %s
                ORDER BY timestamp ASC
            ''', (batch_id,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_approved_predictions_for_class(self, video_id: int, class_name: str,
                                            model_name: str) -> List[Dict]:
        """Get approved predictions matching a class for a video."""
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT * FROM ai_predictions
                WHERE video_id = %s
                AND model_name = %s
                AND review_status IN ('approved', 'auto_approved')
                AND (
                    predicted_tags->>'class' = %s
                    OR corrected_tags->>'class' = %s
                )
                ORDER BY timestamp ASC
            ''', (video_id, model_name, class_name, class_name))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def interpolation_track_exists(self, pred_id_a: int, pred_id_b: int) -> bool:
        """Check if an interpolation track already exists for a pair of predictions."""
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT COUNT(*) as count FROM interpolation_tracks
                WHERE (start_prediction_id = %s AND end_prediction_id = %s)
                   OR (start_prediction_id = %s AND end_prediction_id = %s)
            ''', (pred_id_a, pred_id_b, pred_id_b, pred_id_a))
            return cursor.fetchone()['count'] > 0
