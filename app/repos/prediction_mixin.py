import json
from datetime import datetime
from typing import List, Dict, Optional

import psycopg2
from psycopg2 import extras

from db_connection import get_cursor, get_connection

VEHICLE_CLASSES = {
    'sedan', 'pickup truck', 'suv', 'minivan', 'van', 'tractor', 'atv', 'utv',
    'snowmobile', 'golf cart', 'motorcycle', 'trailer', 'bus', 'semi truck',
    'dump truck', 'rowboat', 'fishing boat', 'speed boat', 'pontoon boat',
    'kayak', 'canoe', 'sailboat', 'jet ski', 'person'
}


class PredictionMixin:
    """AI prediction CRUD, review, classification, and grouping methods."""

    # ==================== AI Predictions ====================

    def count_predictions_for_video(self, video_id: int, model_name: str, model_version: str) -> int:
        """Count existing predictions for a video from a specific model."""
        with get_cursor() as cursor:
            cursor.execute('''
                SELECT COUNT(*) FROM ai_predictions
                WHERE video_id = %s AND model_name = %s AND model_version = %s
            ''', (video_id, model_name, model_version))
            return cursor.fetchone()['count']

    def insert_predictions_batch(self, video_id: int, model_name: str, model_version: str,
                                  batch_id: str, predictions: List[Dict],
                                  initial_status: str = 'pending') -> List[int]:
        """Insert a batch of AI predictions. Returns list of prediction IDs.

        Args:
            initial_status: Initial review_status. Use 'processing' to hold predictions
                           from the review queue until automated processing completes.
        """
        ids = []
        with get_cursor() as cursor:
            for pred in predictions:
                cursor.execute('''
                    INSERT INTO ai_predictions
                    (video_id, model_name, model_version, prediction_type, confidence,
                     timestamp, start_time, end_time, bbox_x, bbox_y, bbox_width, bbox_height,
                     scenario, predicted_tags, batch_id, inference_time_ms, review_status, parent_prediction_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                ''', (
                    video_id, model_name, model_version,
                    pred['prediction_type'], pred['confidence'],
                    pred.get('timestamp'), pred.get('start_time'), pred.get('end_time'),
                    pred.get('bbox', {}).get('x'), pred.get('bbox', {}).get('y'),
                    pred.get('bbox', {}).get('width'), pred.get('bbox', {}).get('height'),
                    pred['scenario'],
                    extras.Json(pred.get('tags', {})),
                    batch_id,
                    pred.get('inference_time_ms'),
                    initial_status,
                    pred.get('parent_prediction_id')
                ))
                result = cursor.fetchone()
                ids.append(result['id'])
        return ids

    def get_child_predictions(self, parent_id: int) -> List[Dict]:
        """Get child predictions linked to a parent entity prediction."""
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT p.*, v.filename as video_filename, v.title as video_title
                FROM ai_predictions p
                JOIN videos v ON p.video_id = v.id
                WHERE p.parent_prediction_id = %s
                ORDER BY p.confidence DESC
            ''', (parent_id,))
            return [dict(row) for row in cursor.fetchall()]

    def cascade_reject_children(self, parent_id: int, reviewed_by: str = 'cascade') -> int:
        """When a parent entity is rejected, auto-reject its child predictions.
        Uses prediction_group_id to find siblings in the same group."""
        with get_cursor() as cursor:
            # Find the group of this prediction
            cursor.execute(
                'SELECT prediction_group_id FROM ai_predictions WHERE id = %s',
                (parent_id,))
            row = cursor.fetchone()
            if not row or not row['prediction_group_id']:
                return 0
            group_id = row['prediction_group_id']
            cursor.execute('''
                UPDATE ai_predictions
                SET review_status = 'auto_rejected',
                    reviewed_by = %s,
                    reviewed_at = NOW(),
                    review_notes = 'Auto-rejected: sibling in group was rejected'
                WHERE prediction_group_id = %s
                  AND id != %s
                  AND review_status NOT IN ('rejected', 'auto_rejected', 'approved')
            ''', (reviewed_by, group_id, parent_id))
            return cursor.rowcount

    def get_pending_predictions(self, video_id: int = None, model_name: str = None,
                                 limit: int = 100, offset: int = 0) -> List[Dict]:
        """Get pending predictions for review, optionally filtered."""
        with get_cursor(commit=False) as cursor:
            conditions = ["review_status = 'pending'"]
            params = []
            if video_id:
                conditions.append("video_id = %s")
                params.append(video_id)
            if model_name:
                conditions.append("model_name = %s")
                params.append(model_name)
            where = " AND ".join(conditions)
            params.extend([limit, offset])
            cursor.execute(f'''
                SELECT p.*, v.filename as video_filename, v.title as video_title
                FROM ai_predictions p
                JOIN videos v ON p.video_id = v.id
                WHERE {where}
                ORDER BY p.confidence DESC, p.created_at DESC
                LIMIT %s OFFSET %s
            ''', params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_predictions_for_video(self, video_id: int, limit: int = 200, offset: int = 0) -> List[Dict]:
        """Get all predictions for a video regardless of review status."""
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT p.*, v.filename as video_filename, v.title as video_title
                FROM ai_predictions p
                JOIN videos v ON p.video_id = v.id
                WHERE p.video_id = %s
                ORDER BY p.confidence DESC, p.created_at DESC
                LIMIT %s OFFSET %s
            ''', (video_id, limit, offset))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_prediction_by_id(self, prediction_id: int) -> Optional[Dict]:
        """Get a single prediction by ID."""
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT p.*, v.filename as video_filename, v.title as video_title
                FROM ai_predictions p
                JOIN videos v ON p.video_id = v.id
                WHERE p.id = %s
            ''', (prediction_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_prediction_counts(self, video_id: int = None) -> Dict:
        """Get count of predictions by status, optionally for a specific video."""
        with get_cursor(commit=False) as cursor:
            if video_id:
                cursor.execute('''
                    SELECT review_status, COUNT(*) as count
                    FROM ai_predictions
                    WHERE video_id = %s
                    GROUP BY review_status
                ''', (video_id,))
            else:
                cursor.execute('''
                    SELECT review_status, COUNT(*) as count
                    FROM ai_predictions
                    GROUP BY review_status
                ''')
            rows = cursor.fetchall()
            counts = {row['review_status']: row['count'] for row in rows}
            counts['total'] = sum(counts.values())
            return counts

    def review_prediction(self, prediction_id: int, action: str, reviewer: str,
                           notes: str = None, corrections: Dict = None) -> Optional[Dict]:
        """Review a prediction: approve, reject, or correct."""
        with get_cursor() as cursor:
            if action == 'approve':
                status = 'approved'
            elif action == 'reject':
                status = 'rejected'
            elif action == 'correct':
                status = 'approved'  # corrections are approved with modified data
            elif action == 'reclassify':
                status = 'needs_reclassification'
            else:
                return None

            update_fields = [
                "review_status = %s",
                "reviewed_by = %s",
                "reviewed_at = NOW()",
                "review_notes = %s"
            ]
            params = [status, reviewer, notes]

            if corrections:
                if corrections.get('tags'):
                    update_fields.append("corrected_tags = %s")
                    params.append(extras.Json(corrections['tags']))
                if corrections.get('bbox'):
                    update_fields.append("corrected_bbox = %s")
                    params.append(extras.Json(corrections['bbox']))
                if corrections.get('correction_type'):
                    update_fields.append("correction_type = %s")
                    params.append(corrections['correction_type'])

            params.append(prediction_id)
            cursor.execute(f'''
                UPDATE ai_predictions
                SET {", ".join(update_fields)}
                WHERE id = %s
                RETURNING *
            ''', params)
            row = cursor.fetchone()
            return dict(row) if row else None

    def approve_prediction_to_annotation(self, prediction_id: int) -> Optional[int]:
        """Convert an approved prediction into a training annotation. Returns annotation ID."""
        pred = self.get_prediction_by_id(prediction_id)
        if not pred or pred['review_status'] not in ('approved', 'auto_approved'):
            return None

        # Use corrected data if available, otherwise use predicted data
        tags = pred.get('corrected_tags') or pred['predicted_tags']
        bbox = pred.get('corrected_bbox')

        with get_cursor() as cursor:
            if pred['prediction_type'] == 'keyframe':
                bx = bbox['x'] if bbox else pred['bbox_x']
                by = bbox['y'] if bbox else pred['bbox_y']
                bw = bbox['width'] if bbox else pred['bbox_width']
                bh = bbox['height'] if bbox else pred['bbox_height']

                source = 'ai_auto_approved' if pred['review_status'] == 'auto_approved' else 'ai_prediction'
                # Human-reviewed predictions are already verified; auto-approved need review
                is_reviewed = pred['review_status'] == 'approved'
                cursor.execute('''
                    INSERT INTO keyframe_annotations
                    (video_id, timestamp, bbox_x, bbox_y, bbox_width, bbox_height,
                     activity_tag, comment, reviewed, source, source_prediction_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                ''', (
                    pred['video_id'], pred['timestamp'],
                    bx, by, bw, bh,
                    pred['scenario'],
                    f"AI prediction (model={pred['model_name']} v{pred['model_version']}, confidence={pred['confidence']:.2f})",
                    is_reviewed, source, prediction_id
                ))
                result = cursor.fetchone()
                annotation_id = result['id']

            elif pred['prediction_type'] == 'time_range':
                cursor.execute('''
                    INSERT INTO time_range_tags
                    (video_id, tag_name, start_time, end_time, comment)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING id
                ''', (
                    pred['video_id'], pred['scenario'],
                    pred['start_time'], pred['end_time'],
                    f"AI prediction (model={pred['model_name']} v{pred['model_version']}, confidence={pred['confidence']:.2f})"
                ))
                result = cursor.fetchone()
                annotation_id = result['id']
            else:
                return None

            # Link prediction to the created annotation
            cursor.execute('''
                UPDATE ai_predictions SET created_annotation_id = %s WHERE id = %s
            ''', (annotation_id, prediction_id))

            return annotation_id

    def update_prediction_routing(self, prediction_id: int, review_status: str,
                                    routed_by: str, threshold_used: Dict = None) -> bool:
        """Update a prediction's routing status (for auto-approve/auto-reject)."""
        with get_cursor() as cursor:
            cursor.execute('''
                UPDATE ai_predictions
                SET review_status = %s, routed_by = %s, routing_threshold_used = %s
                WHERE id = %s
            ''', (review_status, routed_by, extras.Json(threshold_used) if threshold_used else None, prediction_id))
            return cursor.rowcount > 0

    def get_review_queue(self, video_id=None, model_name=None, min_confidence=None,
                         max_confidence=None, limit=50, offset=0, scenario=None, review_status='pending'):
        """Get predictions for mobile review queue, with thumbnail paths."""
        with get_cursor(commit=False) as cursor:
            conditions = ["p.review_status = %s"]
            params = [review_status]
            if video_id:
                conditions.append("p.video_id = %s")
                params.append(video_id)
            if model_name:
                conditions.append("p.model_name = %s")
                params.append(model_name)
            if min_confidence is not None:
                conditions.append("p.confidence >= %s")
                params.append(min_confidence)
            if max_confidence is not None:
                conditions.append("p.confidence <= %s")
                params.append(max_confidence)
            if scenario:
                if scenario == '_other':
                    conditions.append("p.scenario NOT IN ('vehicle_detection', 'person_detection', 'face_detection', 'person_identification', 'license_plate', 'boat_registration')")
                elif ',' in scenario:
                    scenario_list = [s.strip() for s in scenario.split(',')]
                    placeholders = ', '.join(['%s'] * len(scenario_list))
                    conditions.append(f"p.scenario IN ({placeholders})")
                    params.extend(scenario_list)
                else:
                    conditions.append("p.scenario = %s")
                    params.append(scenario)
            where = " AND ".join(conditions)
            params.extend([limit, offset])
            cursor.execute(f'''
                SELECT p.id, p.video_id, p.model_name, p.model_version, p.prediction_type,
                       p.confidence, p.timestamp, p.start_time, p.end_time,
                       p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
                       p.scenario, p.predicted_tags, p.corrected_tags, p.inference_time_ms,
                       v.title as video_title, v.thumbnail_path, v.width as video_width, v.height as video_height, v.camera_id
                FROM ai_predictions p
                JOIN videos v ON p.video_id = v.id
                WHERE {where}
                ORDER BY p.confidence DESC, p.created_at DESC
                LIMIT %s OFFSET %s
            ''', params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_review_queue_summary(self, scenario=None, review_status='pending', min_confidence=None, max_confidence=None):
        """Get summary of predictions grouped by video for review queue entry screen."""
        with get_cursor(commit=False) as cursor:
            conditions = ["p.review_status = %s"]
            params = [review_status]
            if min_confidence is not None:
                conditions.append("p.confidence >= %s")
                params.append(min_confidence)
            elif max_confidence is None:
                conditions.append("p.confidence >= 0.10")
            if max_confidence is not None:
                conditions.append("p.confidence <= %s")
                params.append(max_confidence)
            if scenario:
                if scenario == '_other':
                    conditions.append("p.scenario NOT IN ('vehicle_detection', 'person_detection', 'face_detection', 'person_identification', 'license_plate', 'boat_registration')")
                elif ',' in scenario:
                    scenario_list = [s.strip() for s in scenario.split(',')]
                    placeholders = ', '.join(['%s'] * len(scenario_list))
                    conditions.append(f"p.scenario IN ({placeholders})")
                    params.extend(scenario_list)
                else:
                    conditions.append("p.scenario = %s")
                    params.append(scenario)
            where_clause = " AND ".join(conditions)
            cursor.execute('''
                SELECT v.id as video_id, v.title as video_title, v.thumbnail_path,
                       COUNT(*) as pending_count,
                       COUNT(*) FILTER (WHERE p.review_status IN ('approved', 'rejected')) as reviewed_count,
                       COUNT(*) FILTER (WHERE p.review_status = 'pending') +
                       COUNT(*) FILTER (WHERE p.review_status IN ('approved', 'rejected')) as total_count,
                       ROUND(AVG(p.confidence)::numeric, 3) as avg_confidence,
                       MIN(p.confidence) as min_confidence,
                       MODE() WITHIN GROUP (ORDER BY p.predicted_tags->>'class') as dominant_class
                FROM ai_predictions p
                JOIN videos v ON p.video_id = v.id
                WHERE ''' + where_clause + '''
                GROUP BY v.id, v.title, v.thumbnail_path
                ORDER BY pending_count DESC
            ''', params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_review_filter_counts(self, min_confidence=None, max_confidence=None):
        """Get counts for each review queue filter chip.
        Uses confidence >= 0.10 to match the review queue/summary filters.
        Optional min_confidence/max_confidence params filter prediction counts."""
        with get_cursor(commit=False) as cursor:
            # Build optional confidence filter for predictions
            conf_filter = "AND confidence >= 0.10"
            params = []
            if min_confidence is not None:
                conf_filter = "AND confidence >= %s"
                params.append(min_confidence)
            if max_confidence is not None:
                conf_filter += " AND confidence <= %s"
                params.append(max_confidence)
            elif min_confidence is None:
                # Default minimum
                pass

            cursor.execute('''
                SELECT
                    COUNT(*) FILTER (WHERE review_status = 'pending' ''' + conf_filter + ''') as predictions,
                    COUNT(*) FILTER (WHERE review_status = 'pending' AND scenario = 'vehicle_detection' ''' + conf_filter + ''') as vehicles,
                    COUNT(*) FILTER (WHERE review_status = 'pending' AND scenario IN ('person_detection', 'face_detection', 'person_identification') ''' + conf_filter + ''') as people,
                    COUNT(*) FILTER (WHERE review_status = 'pending' AND scenario = 'license_plate' ''' + conf_filter + ''') as plates,
                    COUNT(*) FILTER (WHERE review_status = 'pending' AND scenario = 'boat_registration' ''' + conf_filter + ''') as boat_reg,
                    COUNT(*) FILTER (WHERE review_status = 'needs_reclassification') as needs_reclassification
                FROM ai_predictions
            ''', params + params + params + params + params)
            row = cursor.fetchone()
            counts = dict(row) if row else {}

            # Cross-camera count
            cursor.execute("SELECT COUNT(*) as cnt FROM cross_camera_links WHERE status = 'auto'")
            cc_row = cursor.fetchone()
            counts['cross_camera'] = cc_row['cnt'] if cc_row else 0

            # Cluster count (only clusters with 2+ members)
            cursor.execute("""
                SELECT COUNT(*) as cnt FROM (
                    SELECT corrected_tags->>'static_cluster'
                    FROM ai_predictions
                    WHERE corrected_tags->>'batch_reviewable' = 'true'
                      AND review_status = 'pending'
                      AND predicted_tags->>'class' = 'unknown vehicle'
                    GROUP BY corrected_tags->>'static_cluster'
                    HAVING COUNT(*) >= 2
                ) sub
            """)
            cl_row = cursor.fetchone()
            counts['clusters'] = cl_row['cnt'] if cl_row else 0

            return counts

    def batch_review_predictions(self, reviews, reviewer='studio_user'):
        """Batch review multiple predictions. Returns summary."""
        results = {'approved': 0, 'rejected': 0, 'needs_reclassification': 0, 'failed': 0, 'annotation_ids': []}
        with get_cursor() as cursor:
            for review in reviews:
                pred_id = review.get('prediction_id')
                action = review.get('action')
                notes = review.get('notes')
                actual_class = review.get('actual_class')
                if action not in ('approve', 'reject', 'reclassify'):
                    results['failed'] += 1
                    continue
                if action == 'reclassify':
                    status = 'needs_reclassification'
                else:
                    status = 'approved' if action == 'approve' else 'rejected'

                # Handle approve-as-alternate-class (confirm reclassification)
                if status == 'approved' and actual_class:
                    cursor.execute('''
                        UPDATE ai_predictions
                        SET review_status = %s, reviewed_by = %s, reviewed_at = NOW(), review_notes = %s,
                            corrected_tags = (COALESCE(corrected_tags, '{}'::jsonb) ||
                                jsonb_build_object(
                                    'actual_class', %s,
                                    'reclassified_by', %s,
                                    'reclassified_at', NOW()::text
                                )) - 'needs_negative_review'
                        WHERE id = %s AND review_status IN ('pending', 'approved', 'needs_reclassification')
                        RETURNING id, model_name, model_version
                    ''', (status, reviewer, notes, actual_class, reviewer, pred_id))
                # Handle reclassification for rejections
                elif status == 'rejected' and actual_class:
                    cursor.execute('''
                        UPDATE ai_predictions
                        SET review_status = %s, reviewed_by = %s, reviewed_at = NOW(), review_notes = %s,
                            corrected_tags = COALESCE(corrected_tags, '{}'::jsonb) ||
                                jsonb_build_object(
                                    'actual_class', %s,
                                    'reclassified_by', %s,
                                    'reclassified_at', NOW()::text
                                )
                        WHERE id = %s AND review_status IN ('pending', 'approved', 'needs_reclassification')
                        RETURNING id, model_name, model_version
                    ''', (status, reviewer, notes, actual_class, reviewer, pred_id))
                else:
                    corrected_tags = review.get('corrected_tags')
                    if corrected_tags:
                        cursor.execute('''
                            UPDATE ai_predictions
                            SET review_status = %s, reviewed_by = %s, reviewed_at = NOW(), review_notes = %s,
                                corrected_tags = COALESCE(corrected_tags, '{}'::jsonb) || %s::jsonb
                            WHERE id = %s AND review_status IN ('pending', 'approved', 'needs_reclassification')
                            RETURNING id, model_name, model_version
                        ''', (status, reviewer, notes, json.dumps(corrected_tags), pred_id))
                    else:
                        cursor.execute('''
                            UPDATE ai_predictions
                            SET review_status = %s, reviewed_by = %s, reviewed_at = NOW(), review_notes = %s
                            WHERE id = %s AND review_status IN ('pending', 'approved', 'needs_reclassification')
                            RETURNING id, model_name, model_version
                        ''', (status, reviewer, notes, pred_id))

                row = cursor.fetchone()
                if row:
                    results[status] += 1
                    # Increment usage count for reclassification class
                    if status == 'rejected' and actual_class:
                        self.increment_class_usage(actual_class)
                    if status == 'approved' and actual_class:
                        self.increment_class_usage(actual_class)
                else:
                    results['failed'] += 1
        # Create annotations for approved predictions (outside the batch cursor)
        # We do this separately to avoid nested cursor issues
        for review in reviews:
            # Skip annotation creation for bad-bbox: valid detection but unsuitable for training
            corrected = review.get('corrected_tags') or {}
            if corrected.get('bad_bbox') or corrected.get('exclude_from_training'):
                continue
            if review.get('action') == 'approve' and not review.get('actual_class'):
                ann_id = self.approve_prediction_to_annotation(review['prediction_id'])
                if ann_id:
                    results['annotation_ids'].append(ann_id)
        # Create hard negative annotations for rejected predictions where actual_class
        # is NOT a known vehicle class (i.e., the detection was truly a non-vehicle object)
        for review in reviews:
            if review.get('actual_class') and review.get('action') in ('reject', 'approve'):
                actual_class = review['actual_class']
                if actual_class.lower() in VEHICLE_CLASSES:
                    continue
                pred_id = review['prediction_id']
                pred = self.get_prediction_by_id(pred_id)
                if not pred:
                    continue
                with get_cursor() as cursor:
                    cursor.execute('''
                        INSERT INTO keyframe_annotations
                        (video_id, timestamp, bbox_x, bbox_y, bbox_width, bbox_height,
                         activity_tag, is_negative, reviewed, source, source_prediction_id, comment)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, TRUE, %s, %s, %s)
                        RETURNING id
                    ''', (
                        pred['video_id'], pred['timestamp'],
                        pred['bbox_x'], pred['bbox_y'], pred['bbox_width'], pred['bbox_height'],
                        pred['scenario'],
                        'hard_negative',
                        pred_id,
                        f'Hard negative: confirmed as {actual_class}'
                    ))
                    row = cursor.fetchone()
                    if row:
                        annotation_id = row['id']
                        cursor.execute(
                            'UPDATE ai_predictions SET created_annotation_id = %s WHERE id = %s',
                            (annotation_id, pred_id)
                        )
        return results

    def get_classification_queue(self, video_id=None, limit=50, offset=0, include_pending=False):
        """Get vehicle_detection predictions that haven't been classified yet."""
        with get_cursor(commit=False) as cursor:
            if include_pending:
                status_condition = "p.review_status IN ('approved', 'pending')"
            else:
                status_condition = "p.review_status = 'approved'"
            params = []
            if video_id:
                video_filter = "AND p.video_id = %s"
                params.append(video_id)
            else:
                video_filter = ""
            params.extend([limit, offset])
            cursor.execute(f'''
                SELECT p.id, p.video_id, p.model_name, p.model_version, p.prediction_type,
                       p.confidence, p.timestamp, p.start_time, p.end_time,
                       p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
                       p.scenario, p.predicted_tags, p.corrected_tags, p.inference_time_ms,
                       p.prediction_group_id as group_id,
                       pg.member_count,
                       v.title as video_title, v.thumbnail_path, v.width as video_width, v.height as video_height
                FROM ai_predictions p
                JOIN videos v ON p.video_id = v.id
                LEFT JOIN prediction_groups pg ON pg.id = p.prediction_group_id
                WHERE (
                    ({status_condition}
                     AND p.scenario = 'vehicle_detection'
                     AND (p.corrected_tags IS NULL OR (p.corrected_tags->>'vehicle_subtype' IS NULL AND p.corrected_tags->>'actual_class' IS NULL)))
                    OR p.review_status = 'needs_reclassification'
                ) {video_filter}
                ORDER BY p.confidence DESC, p.created_at DESC
                LIMIT %s OFFSET %s
            ''', params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_classification_queue_summary(self, include_pending=False):
        """Get summary of vehicle detections needing classification, grouped by video."""
        with get_cursor(commit=False) as cursor:
            if include_pending:
                status_condition = "p.review_status IN ('approved', 'pending')"
            else:
                status_condition = "p.review_status = 'approved'"
            cursor.execute(f'''
                SELECT v.id as video_id, v.title as video_title, v.thumbnail_path,
                       COUNT(*) as pending_classification,
                       ROUND(AVG(p.confidence)::numeric, 3) as avg_confidence
                FROM ai_predictions p
                JOIN videos v ON p.video_id = v.id
                WHERE (
                    ({status_condition}
                     AND p.scenario = 'vehicle_detection'
                     AND (p.corrected_tags IS NULL OR (p.corrected_tags->>'vehicle_subtype' IS NULL AND p.corrected_tags->>'actual_class' IS NULL)))
                    OR p.review_status = 'needs_reclassification'
                )
                GROUP BY v.id, v.title, v.thumbnail_path
                ORDER BY pending_classification DESC
            ''')
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def batch_classify_vehicles(self, classifications, classifier='studio_user'):
        """Batch classify vehicle subtypes. Updates corrected_tags with vehicle_subtype.
        Also approves pending predictions when classified."""
        results = {'classified': 0, 'failed': 0}
        with get_cursor() as cursor:
            for item in classifications:
                pred_id = item.get('prediction_id')
                vehicle_subtype = item.get('vehicle_subtype')
                if not pred_id or not vehicle_subtype:
                    results['failed'] += 1
                    continue
                # Merge into existing corrected_tags (or create new)
                # Also set review_status to approved if still pending
                cursor.execute('''
                    UPDATE ai_predictions
                    SET corrected_tags = COALESCE(corrected_tags, '{}'::jsonb) || %s::jsonb,
                        review_status = 'approved',
                        reviewed_by = COALESCE(reviewed_by, %s),
                        reviewed_at = COALESCE(reviewed_at, NOW())
                    WHERE id = %s
                      AND review_status IN ('approved', 'pending')
                      AND scenario = 'vehicle_detection'
                    RETURNING id, review_status
                ''', (
                    extras.Json({
                        'vehicle_subtype': vehicle_subtype,
                        'classified_by': classifier,
                        'classified_at': datetime.utcnow().isoformat()
                    }),
                    classifier,
                    pred_id
                ))
                row = cursor.fetchone()
                if row:
                    results['classified'] += 1
                else:
                    results['failed'] += 1
        return results

    # --------------- Class Detail Page Methods ---------------

    def get_predictions_by_class(self, class_name, status=None, limit=200, offset=0):
        """Get predictions whose effective class matches class_name.
        Uses same COALESCE logic as vehicle-metrics endpoint."""
        with get_cursor(commit=False) as cursor:
            sql = '''
                SELECT p.id, p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
                       p.confidence, p.review_status, p.predicted_tags, p.corrected_tags,
                       p.video_id, p.created_at, p.reviewed_at,
                       v.thumbnail_path, v.camera_id,
                       COALESCE(
                           p.corrected_tags->>'vehicle_subtype',
                           p.corrected_tags->>'actual_class',
                           p.predicted_tags->>'vehicle_type',
                           p.predicted_tags->>'class'
                       ) as effective_class
                FROM ai_predictions p
                JOIN videos v ON p.video_id = v.id
                WHERE (p.scenario = 'vehicle_detection'
                       OR p.predicted_tags->>'vehicle_type' IS NOT NULL)
                  AND COALESCE(
                       p.corrected_tags->>'vehicle_subtype',
                       p.corrected_tags->>'actual_class',
                       p.predicted_tags->>'vehicle_type',
                       p.predicted_tags->>'class'
                  ) = %s
            '''
            params = [class_name]

            if status and status != 'all':
                sql += ' AND p.review_status = %s'
                params.append(status)

            sql += ' ORDER BY p.created_at DESC LIMIT %s OFFSET %s'
            params.extend([limit, offset])

            cursor.execute(sql, params)
            rows = cursor.fetchall()
            return [dict(r) for r in rows]

    def get_predictions_count_by_class(self, class_name, status=None):
        """Get total count for a class (for pagination)."""
        with get_cursor(commit=False) as cursor:
            sql = '''
                SELECT COUNT(*) as total
                FROM ai_predictions p
                WHERE (p.scenario = 'vehicle_detection'
                       OR p.predicted_tags->>'vehicle_type' IS NOT NULL)
                  AND COALESCE(
                       p.corrected_tags->>'vehicle_subtype',
                       p.corrected_tags->>'actual_class',
                       p.predicted_tags->>'vehicle_type',
                       p.predicted_tags->>'class'
                  ) = %s
            '''
            params = [class_name]
            if status and status != 'all':
                sql += ' AND p.review_status = %s'
                params.append(status)
            cursor.execute(sql, params)
            return cursor.fetchone()['total']

    def batch_update_vehicle_class(self, prediction_ids, vehicle_subtype, updater='studio_user'):
        """Bulk reclassify predictions to a new vehicle_subtype."""
        results = {'updated': 0, 'failed': 0}
        with get_cursor() as cursor:
            for pred_id in prediction_ids:
                cursor.execute('''
                    UPDATE ai_predictions
                    SET corrected_tags = COALESCE(corrected_tags, '{}'::jsonb) || %s::jsonb,
                        review_status = 'approved',
                        reviewed_by = COALESCE(reviewed_by, %s),
                        reviewed_at = COALESCE(reviewed_at, NOW())
                    WHERE id = %s
                      AND (scenario = 'vehicle_detection'
                           OR predicted_tags->>'vehicle_type' IS NOT NULL)
                    RETURNING id
                ''', (
                    extras.Json({
                        'vehicle_subtype': vehicle_subtype,
                        'classified_by': updater,
                        'classified_at': datetime.utcnow().isoformat()
                    }),
                    updater,
                    pred_id
                ))
                if cursor.fetchone():
                    results['updated'] += 1
                else:
                    results['failed'] += 1
        return results

    def batch_requeue_predictions(self, prediction_ids):
        """Reset predictions to pending, clearing vehicle_subtype and actual_class."""
        with get_cursor() as cursor:
            cursor.execute('''
                UPDATE ai_predictions
                SET review_status = 'pending',
                    reviewed_by = NULL,
                    reviewed_at = NULL,
                    corrected_tags = corrected_tags - 'vehicle_subtype' - 'actual_class' - 'classified_by' - 'classified_at'
                WHERE id = ANY(%s)
                  AND (scenario = 'vehicle_detection'
                       OR predicted_tags->>'vehicle_type' IS NOT NULL)
                RETURNING id
            ''', (prediction_ids,))
            updated = cursor.rowcount
            return {'requeued': updated, 'requested': len(prediction_ids)}

    def get_prediction_for_crop(self, prediction_id):
        """Get prediction bbox and thumbnail path for cropping."""
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT p.id, p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
                       v.thumbnail_path
                FROM ai_predictions p
                JOIN videos v ON p.video_id = v.id
                WHERE p.id = %s
            ''', (prediction_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    # --------------- Prediction Group Methods ---------------

    def create_prediction_group(self, camera_id, scenario, bbox_centroid_x, bbox_centroid_y,
                                 avg_bbox_width, avg_bbox_height, member_count,
                                 min_confidence, max_confidence, avg_confidence,
                                 min_timestamp, max_timestamp, representative_prediction_id=None):
        """Create a prediction group and return its ID."""
        with get_cursor() as cursor:
            cursor.execute("""
                INSERT INTO prediction_groups
                (camera_id, scenario, representative_prediction_id,
                 bbox_centroid_x, bbox_centroid_y, avg_bbox_width, avg_bbox_height,
                 member_count, min_confidence, max_confidence, avg_confidence,
                 min_timestamp, max_timestamp)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (camera_id, scenario, representative_prediction_id,
                  bbox_centroid_x, bbox_centroid_y, avg_bbox_width, avg_bbox_height,
                  member_count, min_confidence, max_confidence, avg_confidence,
                  min_timestamp, max_timestamp))
            row = cursor.fetchone()
            return row['id'] if row else None

    def assign_predictions_to_group(self, prediction_ids, group_id):
        """Assign predictions to a group."""
        with get_cursor() as cursor:
            cursor.execute("""
                UPDATE ai_predictions SET prediction_group_id = %s
                WHERE id = ANY(%s)
            """, (group_id, prediction_ids))
            return cursor.rowcount

    def update_prediction_group_stats(self, group_id):
        """Recompute group statistics from current members."""
        with get_cursor() as cursor:
            cursor.execute("""
                UPDATE prediction_groups pg SET
                    member_count = sub.cnt,
                    min_confidence = sub.min_conf,
                    max_confidence = sub.max_conf,
                    avg_confidence = sub.avg_conf,
                    min_timestamp = sub.min_ts,
                    max_timestamp = sub.max_ts,
                    representative_prediction_id = sub.rep_id,
                    updated_at = NOW()
                FROM (
                    SELECT
                        prediction_group_id,
                        COUNT(*) as cnt,
                        MIN(confidence) as min_conf,
                        MAX(confidence) as max_conf,
                        AVG(confidence) as avg_conf,
                        MIN(timestamp) as min_ts,
                        MAX(timestamp) as max_ts,
                        (SELECT id FROM ai_predictions
                         WHERE prediction_group_id = %s
                         ORDER BY (bbox_width * bbox_height) DESC NULLS LAST, confidence DESC LIMIT 1) as rep_id
                    FROM ai_predictions
                    WHERE prediction_group_id = %s
                    GROUP BY prediction_group_id
                ) sub
                WHERE pg.id = %s
            """, (group_id, group_id, group_id))

    def get_prediction_group(self, group_id):
        """Get a prediction group with its representative prediction details."""
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT pg.*, p.video_id, p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
                       p.confidence, p.timestamp, p.predicted_tags, p.scenario as pred_scenario,
                       p.model_name, p.model_version, p.inference_time_ms,
                       v.title as video_title, v.thumbnail_path, v.width as video_width,
                       v.height as video_height, v.camera_id as video_camera_id
                FROM prediction_groups pg
                LEFT JOIN ai_predictions p ON p.id = pg.representative_prediction_id
                LEFT JOIN videos v ON p.video_id = v.id
                WHERE pg.id = %s
            """, (group_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_group_members(self, group_id):
        """Get all predictions belonging to a group."""
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT p.id, p.video_id, p.model_name, p.model_version, p.prediction_type,
                       p.confidence, p.timestamp, p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
                       p.scenario, p.predicted_tags, p.review_status, p.corrected_tags,
                       p.inference_time_ms, p.reviewed_by, p.reviewed_at,
                       v.title as video_title, v.thumbnail_path
                FROM ai_predictions p
                JOIN videos v ON p.video_id = v.id
                WHERE p.prediction_group_id = %s
                ORDER BY p.confidence DESC
            """, (group_id,))
            return [dict(r) for r in cursor.fetchall()]

    def get_existing_groups_for_camera(self, camera_id, scenario, status='pending'):
        """Get existing groups for a camera+scenario with avg bbox info."""
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT id, bbox_centroid_x, bbox_centroid_y, avg_bbox_width, avg_bbox_height,
                       member_count, avg_confidence
                FROM prediction_groups
                WHERE camera_id = %s AND scenario = %s AND review_status = %s
            """, (camera_id, scenario, status))
            return [dict(r) for r in cursor.fetchall()]

    def clear_groups_for_camera(self, camera_id):
        """Delete all groups for a camera (for regroup_all)."""
        with get_cursor() as cursor:
            cursor.execute(
                "SELECT id FROM prediction_groups WHERE camera_id = %s", (camera_id,))
            group_ids = [r['id'] for r in cursor.fetchall()]
            if group_ids:
                cursor.execute(
                    "UPDATE ai_predictions SET prediction_group_id = NULL WHERE prediction_group_id = ANY(%s)",
                    (group_ids,))
                cursor.execute(
                    "DELETE FROM prediction_groups WHERE camera_id = %s", (camera_id,))
            return len(group_ids)

    def get_grouped_review_queue(self, video_id=None, min_confidence=None,
                                  max_confidence=None, limit=50, offset=0, scenario=None, review_status='pending'):
        """Get review queue with grouped predictions collapsed into single entries.
        Groups are found by looking for predictions with a group ID matching the given status."""
        with get_cursor(commit=False) as cursor:
            # Build conditions for grouped (predictions in a group) and ungrouped
            group_conditions = [
                "p.review_status = %s",
                "p.prediction_group_id IS NOT NULL",
                "p.confidence >= 0.10"
            ]
            ungrouped_conditions = [
                "p.review_status = %s",
                "p.prediction_group_id IS NULL",
                "p.confidence >= 0.10"
            ]
            group_params = [review_status]
            ungrouped_params = [review_status]

            if video_id:
                group_conditions.append("p.video_id = %s")
                group_params.append(video_id)
                ungrouped_conditions.append("p.video_id = %s")
                ungrouped_params.append(video_id)
            if min_confidence is not None:
                group_conditions.append("p.confidence >= %s")
                group_params.append(min_confidence)
                ungrouped_conditions.append("p.confidence >= %s")
                ungrouped_params.append(min_confidence)
            if max_confidence is not None:
                group_conditions.append("p.confidence <= %s")
                group_params.append(max_confidence)
                ungrouped_conditions.append("p.confidence <= %s")
                ungrouped_params.append(max_confidence)
            if scenario:
                group_conditions.append("p.scenario = %s")
                group_params.append(scenario)
                ungrouped_conditions.append("p.scenario = %s")
                ungrouped_params.append(scenario)

            group_where = " AND ".join(group_conditions)
            ungrouped_where = " AND ".join(ungrouped_conditions)

            # For grouped: pick the representative (first) pending prediction per group
            # DISTINCT ON + ORDER BY must be in a subquery before UNION ALL
            query = f"""
                SELECT * FROM (
                    SELECT * FROM (
                        SELECT DISTINCT ON (p.prediction_group_id)
                               p.prediction_group_id as group_id,
                               (SELECT COUNT(*) FROM ai_predictions p2
                                WHERE p2.prediction_group_id = p.prediction_group_id
                                  AND p2.review_status = 'pending') as member_count,
                               p.confidence as avg_confidence,
                               p.confidence as group_min_confidence,
                               p.scenario as group_scenario,
                               'pending'::text as group_status,
                               p.id, p.video_id, p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
                               p.confidence, p.timestamp, p.predicted_tags, p.corrected_tags, p.inference_time_ms,
                               p.scenario, p.model_name, p.model_version,
                               v.title as video_title, v.thumbnail_path,
                               v.width as video_width, v.height as video_height, v.camera_id
                        FROM ai_predictions p
                        JOIN videos v ON p.video_id = v.id
                        WHERE {group_where}
                        ORDER BY p.prediction_group_id, p.confidence ASC
                    ) grouped_items

                    UNION ALL

                    SELECT NULL::integer as group_id, 1 as member_count, p.confidence as avg_confidence,
                           p.confidence as group_min_confidence, p.scenario as group_scenario,
                           p.review_status as group_status,
                           p.id, p.video_id, p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
                           p.confidence, p.timestamp, p.predicted_tags, p.corrected_tags, p.inference_time_ms,
                           p.scenario, p.model_name, p.model_version,
                           v.title as video_title, v.thumbnail_path,
                           v.width as video_width, v.height as video_height, v.camera_id
                    FROM ai_predictions p
                    JOIN videos v ON p.video_id = v.id
                    WHERE {ungrouped_where}
                ) combined
                ORDER BY avg_confidence DESC
                LIMIT %s OFFSET %s
            """
            params = group_params + ungrouped_params + [limit, offset]
            cursor.execute(query, params)
            rows = cursor.fetchall()
            return [dict(r) for r in rows]

    def get_grouped_review_queue_summary(self, scenario=None, review_status='pending', min_confidence=None, max_confidence=None):
        """Get summary with groups counted as single items per video."""
        with get_cursor(commit=False) as cursor:
            scenario_filter_grouped = ""
            scenario_filter_ungrouped = ""
            # Build confidence filter string
            if min_confidence is not None:
                confidence_filter = f" AND p.confidence >= {float(min_confidence)}"
            elif max_confidence is None:
                confidence_filter = " AND p.confidence >= 0.10"
            else:
                confidence_filter = ""
            if max_confidence is not None:
                confidence_filter += f" AND p.confidence <= {float(max_confidence)}"
            # Build params per UNION part: [review_status, scenario_params...] for each
            grouped_params = [review_status]
            ungrouped_params = [review_status]
            if scenario:
                if scenario == '_other':
                    scenario_filter_grouped = " AND p.scenario NOT IN ('vehicle_detection', 'person_detection', 'face_detection', 'person_identification', 'license_plate', 'boat_registration')"
                    scenario_filter_ungrouped = scenario_filter_grouped
                elif ',' in scenario:
                    scenario_list = [s.strip() for s in scenario.split(',')]
                    placeholders = ', '.join(['%s'] * len(scenario_list))
                    scenario_filter_grouped = f" AND p.scenario IN ({placeholders})"
                    scenario_filter_ungrouped = scenario_filter_grouped
                    grouped_params.extend(scenario_list)
                    ungrouped_params.extend(scenario_list)
                else:
                    scenario_filter_grouped = " AND p.scenario = %s"
                    scenario_filter_ungrouped = " AND p.scenario = %s"
                    grouped_params.append(scenario)
                    ungrouped_params.append(scenario)
            params = grouped_params + ungrouped_params

            cursor.execute("""
                SELECT video_id, video_title, thumbnail_path,
                       SUM(item_count) as pending_count,
                       SUM(prediction_count) as total_predictions,
                       0 as reviewed_count,
                       SUM(item_count) as total_count,
                       ROUND(AVG(avg_conf)::numeric, 3) as avg_confidence,
                       MIN(min_conf) as min_confidence,
                       MODE() WITHIN GROUP (ORDER BY dominant_class) as dominant_class
                FROM (
                    -- Grouped items: groups matching the requested status (each group = 1 item)
                    SELECT p.video_id, v.title as video_title, v.thumbnail_path,
                           1 as item_count,
                           COUNT(*) as prediction_count,
                           AVG(p.confidence) as avg_conf,
                           MIN(p.confidence) as min_conf,
                           MODE() WITHIN GROUP (ORDER BY p.predicted_tags->>'class') as dominant_class
                    FROM ai_predictions p
                    JOIN videos v ON p.video_id = v.id
                    WHERE p.review_status = %s
                      AND p.prediction_group_id IS NOT NULL""" + confidence_filter + scenario_filter_grouped + """
                    GROUP BY p.prediction_group_id, p.video_id, v.title, v.thumbnail_path

                    UNION ALL

                    -- Ungrouped items
                    SELECT p.video_id, v.title as video_title, v.thumbnail_path,
                           1 as item_count, 1 as prediction_count,
                           p.confidence as avg_conf, p.confidence as min_conf,
                           p.predicted_tags->>'class' as dominant_class
                    FROM ai_predictions p
                    JOIN videos v ON p.video_id = v.id
                    WHERE p.review_status = %s
                      AND p.prediction_group_id IS NULL""" + confidence_filter + scenario_filter_ungrouped + """
                ) combined
                GROUP BY video_id, video_title, thumbnail_path
                ORDER BY pending_count DESC
            """, params)
            rows = cursor.fetchall()
            result = []
            for r in rows:
                d = dict(r)
                d['total_predictions'] = int(d.get('total_predictions') or 0)
                result.append(d)
            return result

    def get_grouped_classification_queue(self, video_id=None, limit=50, offset=0,
                                          include_pending=False):
        """Get classification queue with grouped predictions."""
        with get_cursor(commit=False) as cursor:
            if include_pending:
                review_status = "p.review_status IN ('approved', 'pending')"
            else:
                review_status = "p.review_status = 'approved'"

            if include_pending:
                group_review_filter = "pg.review_status != 'rejected'"
            else:
                group_review_filter = "pg.review_status = 'approved'"
            group_conditions = [
                group_review_filter,
                "p.scenario = 'vehicle_detection'",
                "(p.corrected_tags IS NULL OR (p.corrected_tags->>'vehicle_subtype' IS NULL AND p.corrected_tags->>'actual_class' IS NULL))"
            ]
            ungrouped_conditions = [
                review_status,
                "p.prediction_group_id IS NULL",
                "p.scenario = 'vehicle_detection'",
                "(p.corrected_tags IS NULL OR (p.corrected_tags->>'vehicle_subtype' IS NULL AND p.corrected_tags->>'actual_class' IS NULL))"
            ]

            group_params = []
            ungrouped_params = []
            if video_id:
                group_conditions.append("p.video_id = %s")
                group_params.append(video_id)
                ungrouped_conditions.append("p.video_id = %s")
                ungrouped_params.append(video_id)

            group_where = " AND ".join(group_conditions)
            ungrouped_where = " AND ".join(ungrouped_conditions)

            query = f"""
                SELECT pg.id as group_id, pg.member_count, pg.avg_confidence,
                       p.id, p.video_id, p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
                       p.confidence, p.timestamp, p.predicted_tags, p.corrected_tags,
                       p.inference_time_ms, p.scenario, p.model_name, p.model_version,
                       v.title as video_title, v.thumbnail_path,
                       v.width as video_width, v.height as video_height, v.camera_id
                FROM prediction_groups pg
                JOIN ai_predictions p ON p.id = pg.representative_prediction_id
                JOIN videos v ON p.video_id = v.id
                WHERE {group_where}

                UNION ALL

                SELECT NULL as group_id, 1 as member_count, p.confidence as avg_confidence,
                       p.id, p.video_id, p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
                       p.confidence, p.timestamp, p.predicted_tags, p.corrected_tags,
                       p.inference_time_ms, p.scenario, p.model_name, p.model_version,
                       v.title as video_title, v.thumbnail_path,
                       v.width as video_width, v.height as video_height, v.camera_id
                FROM ai_predictions p
                JOIN videos v ON p.video_id = v.id
                WHERE {ungrouped_where}

                ORDER BY avg_confidence DESC
                LIMIT %s OFFSET %s
            """
            params = group_params + ungrouped_params + [limit, offset]
            cursor.execute(query, params)
            rows = cursor.fetchall()
            return [dict(r) for r in rows]

    def batch_review_group(self, group_id, action, reviewer='studio_user', notes=None):
        """Review all members of a prediction group at once.

        Args:
            group_id: prediction group ID
            action: 'approve' or 'reject'
            reviewer: reviewer identifier
            notes: optional review notes

        Returns:
            dict with counts of approved/rejected predictions
        """
        status = 'approved' if action == 'approve' else 'rejected'
        results = {'approved': 0, 'rejected': 0, 'annotation_ids': []}

        with get_cursor() as cursor:
            # Update all member predictions (include approved so classify rule-outs work)
            cursor.execute("""
                UPDATE ai_predictions
                SET review_status = %s, reviewed_by = %s, reviewed_at = NOW(), review_notes = %s
                WHERE prediction_group_id = %s AND review_status IN ('pending', 'approved', 'needs_reclassification')
                RETURNING id
            """, (status, reviewer, notes, group_id))
            updated_rows = cursor.fetchall()
            results[status] = len(updated_rows)

            # Update group status
            cursor.execute("""
                UPDATE prediction_groups
                SET review_status = %s, updated_at = NOW()
                WHERE id = %s
            """, (status, group_id))

        # Create annotations for approved predictions
        if action == 'approve':
            for row in updated_rows:
                ann_id = self.approve_prediction_to_annotation(row['id'])
                if ann_id:
                    results['annotation_ids'].append(ann_id)

        return results

    def batch_classify_group(self, group_id, vehicle_subtype, classifier='studio_user'):
        """Classify all members of a prediction group with a vehicle subtype.

        Args:
            group_id: prediction group ID
            vehicle_subtype: vehicle subtype classification
            classifier: classifier identifier

        Returns:
            dict with classified count
        """
        results = {'classified': 0}
        with get_cursor() as cursor:
            cursor.execute("""
                UPDATE ai_predictions
                SET corrected_tags = COALESCE(corrected_tags, '{}'::jsonb) || %s::jsonb,
                    review_status = 'approved',
                    reviewed_by = COALESCE(reviewed_by, %s),
                    reviewed_at = COALESCE(reviewed_at, NOW())
                WHERE prediction_group_id = %s
                  AND scenario = 'vehicle_detection'
                RETURNING id
            """, (
                extras.Json({
                    'vehicle_subtype': vehicle_subtype,
                    'classified_by': classifier,
                    'classified_at': datetime.utcnow().isoformat()
                }),
                classifier,
                group_id
            ))
            updated = cursor.fetchall()
            results['classified'] = len(updated)

            # Update group status
            cursor.execute("""
                UPDATE prediction_groups SET review_status = 'approved', updated_at = NOW()
                WHERE id = %s
            """, (group_id,))

        return results

    def undo_group_review(self, group_id):
        """Undo review for all members of a prediction group.

        Reverts all members to pending, deletes any created annotations,
        and resets group status to pending.
        """
        results = {'reverted': 0, 'annotations_deleted': 0}
        with get_cursor() as cursor:
            # Get member predictions that were reviewed
            cursor.execute("""
                SELECT id, created_annotation_id FROM ai_predictions
                WHERE prediction_group_id = %s AND review_status != 'pending'
            """, (group_id,))
            members = cursor.fetchall()

            # Delete created annotations
            for m in members:
                if m['created_annotation_id']:
                    cursor.execute(
                        "DELETE FROM keyframe_annotations WHERE id = %s",
                        (m['created_annotation_id'],))
                    results['annotations_deleted'] += 1

            # Revert predictions to pending
            cursor.execute("""
                UPDATE ai_predictions
                SET review_status = 'pending', reviewed_by = NULL, reviewed_at = NULL,
                    review_notes = NULL, created_annotation_id = NULL
                WHERE prediction_group_id = %s AND review_status != 'pending'
                RETURNING id
            """, (group_id,))
            results['reverted'] = cursor.rowcount

            # Reset group status
            cursor.execute("""
                UPDATE prediction_groups SET review_status = 'pending', updated_at = NOW()
                WHERE id = %s
            """, (group_id,))

        return results

    def unreview_prediction(self, prediction_id):
        """Revert a prediction back to pending (for undo). Also removes created annotation."""
        with get_cursor() as cursor:
            # Get current state
            cursor.execute('SELECT created_annotation_id, review_status FROM ai_predictions WHERE id = %s', (prediction_id,))
            row = cursor.fetchone()
            if not row or row['review_status'] == 'pending':
                return False
            # Remove created annotation if any
            if row['created_annotation_id']:
                cursor.execute('DELETE FROM keyframe_annotations WHERE id = %s', (row['created_annotation_id'],))
            # Reset prediction to pending
            cursor.execute('''
                UPDATE ai_predictions
                SET review_status = 'pending', reviewed_by = NULL, reviewed_at = NULL,
                    review_notes = NULL, created_annotation_id = NULL
                WHERE id = %s
            ''', (prediction_id,))
            return cursor.rowcount > 0

    def get_review_history(self, status_filter=None, reviewer=None, limit=50, offset=0):
        """Get reviewed predictions for history view, most recent first"""
        with get_cursor() as cur:
            conditions = ["review_status IN ('approved', 'rejected')"]
            params = []

            if status_filter:
                conditions.append("review_status = %s")
                params.append(status_filter)
            if reviewer:
                conditions.append("reviewed_by = %s")
                params.append(reviewer)

            where = " AND ".join(conditions)
            params.extend([limit, offset])

            cur.execute(f"""
                SELECT p.id, p.video_id, v.title as video_title, p.prediction_type, p.scenario,
                       p.predicted_tags, p.confidence, p.review_status, p.reviewed_by, p.reviewed_at,
                       p.review_notes, p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
                       p.thumbnail_path
                FROM ai_predictions p
                LEFT JOIN videos v ON p.video_id = v.id
                WHERE {where}
                ORDER BY p.reviewed_at DESC NULLS LAST
                LIMIT %s OFFSET %s
            """, params)
            return [dict(row) for row in cur.fetchall()]

    def get_review_history_v2(self, status_filter=None, reviewer=None, scenario=None,
                               classification=None, video_id=None, limit=50, offset=0,
                               actual_class=None):
        """Get reviewed predictions with enhanced filtering, returns (predictions, total_count)"""
        with get_cursor() as cur:
            # Build base conditions
            if status_filter == 'classified':
                conditions = ["review_status = 'approved'", "corrected_tags->>'vehicle_subtype' IS NOT NULL"]
            else:
                conditions = ["review_status IN ('approved', 'rejected')"]

            params = []

            if status_filter and status_filter != 'classified':
                conditions.append("review_status = %s")
                params.append(status_filter)
            if reviewer:
                conditions.append("reviewed_by = %s")
                params.append(reviewer)
            if scenario:
                conditions.append("p.scenario = %s")
                params.append(scenario)
            if classification:
                conditions.append("corrected_tags->>'vehicle_subtype' = %s")
                params.append(classification)
            if actual_class:
                conditions.append("corrected_tags->>'actual_class' = %s")
                params.append(actual_class)
            if video_id:
                conditions.append("p.video_id = %s")
                params.append(video_id)

            where = " AND ".join(conditions)

            # Get total count
            count_params = params.copy()
            cur.execute(f"""
                SELECT COUNT(*) as total
                FROM ai_predictions p
                LEFT JOIN videos v ON p.video_id = v.id
                WHERE {where}
            """, count_params)
            total_count = cur.fetchone()['total']

            # Get paginated results
            params.extend([limit, offset])
            cur.execute(f"""
                SELECT p.id, p.video_id, v.title as video_title, p.prediction_type, p.scenario,
                       p.predicted_tags, p.confidence, p.review_status, p.reviewed_by, p.reviewed_at,
                       p.review_notes, p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
                       v.thumbnail_path, p.corrected_tags
                FROM ai_predictions p
                LEFT JOIN videos v ON p.video_id = v.id
                WHERE {where}
                ORDER BY p.reviewed_at DESC NULLS LAST
                LIMIT %s OFFSET %s
            """, params)
            return ([dict(row) for row in cur.fetchall()], total_count)

    def get_classification_filter_values(self):
        """Get distinct values for classification filter dropdowns"""
        with get_cursor() as cur:
            cur.execute("""
                SELECT DISTINCT corrected_tags->>'vehicle_subtype' as value
                FROM ai_predictions
                WHERE corrected_tags->>'vehicle_subtype' IS NOT NULL
                ORDER BY value
            """)
            vehicle_subtypes = [row['value'] for row in cur.fetchall()]

            cur.execute("""
                SELECT corrected_tags->>'actual_class' as value, COUNT(*) as cnt
                FROM ai_predictions
                WHERE corrected_tags->>'actual_class' IS NOT NULL
                GROUP BY value
                ORDER BY cnt DESC
            """)
            actual_classes = [{'value': row['value'], 'count': row['cnt']} for row in cur.fetchall()]

            return {'vehicle_subtypes': vehicle_subtypes, 'actual_classes': actual_classes}

    # ==================== Reclassification Classes ====================

    def get_reclassification_classes(self):
        """Get all known reclassification classes ordered by usage"""
        with get_cursor() as cursor:
            cursor.execute("""
                SELECT class_name, source, usage_count
                FROM reclassification_classes
                WHERE class_name NOT IN ('other', 'unknown')
                ORDER BY usage_count DESC, class_name
            """)
            return [dict(row) for row in cursor.fetchall()]

    def get_camera_top_classes(self, camera_name, limit=6):
        """Get most common reclassification classes for a specific camera"""
        with get_cursor() as cursor:
            cursor.execute("""
                SELECT corrected_tags->>'actual_class' as class_name, COUNT(*) as cnt
                FROM ai_predictions p
                JOIN videos v ON v.id = p.video_id
                WHERE p.review_status = 'rejected'
                  AND p.corrected_tags->>'actual_class' IS NOT NULL
                  AND p.corrected_tags->>'actual_class' NOT IN ('other', 'unknown')
                  AND v.camera_id = %s
                GROUP BY 1
                ORDER BY cnt DESC
                LIMIT %s
            """, (camera_name, limit))
            return [dict(row) for row in cursor.fetchall()]

    def add_reclassification_class(self, class_name):
        """Add a new custom reclassification class"""
        with get_cursor() as cursor:
            cursor.execute("""
                INSERT INTO reclassification_classes (class_name, source)
                VALUES (%s, 'custom')
                ON CONFLICT (class_name) DO NOTHING
                RETURNING id, class_name
            """, (class_name.lower().strip(),))
            row = cursor.fetchone()
            return dict(row) if row else None

    def increment_class_usage(self, class_name):
        """Increment usage count for a reclassification class"""
        with get_cursor() as cursor:
            cursor.execute("""
                UPDATE reclassification_classes
                SET usage_count = usage_count + 1
                WHERE class_name = %s
            """, (class_name.lower().strip(),))
            # If class doesn't exist yet, add it
            if cursor.rowcount == 0:
                cursor.execute("""
                    INSERT INTO reclassification_classes (class_name, source, usage_count)
                    VALUES (%s, 'custom', 1)
                    ON CONFLICT (class_name) DO UPDATE SET usage_count = reclassification_classes.usage_count + 1
                """, (class_name.lower().strip(),))

    def get_vlm_stats(self):
        """Get VLM review statistics: acceptance rate, breakdown by class."""
        with get_cursor(commit=False) as cursor:
            # Total VLM-reviewed predictions
            cursor.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE corrected_tags->>'vlm_model' IS NOT NULL) as vlm_reviewed,
                    COUNT(*) FILTER (
                        WHERE corrected_tags->>'vlm_model' IS NOT NULL
                          AND review_status IN ('approved', 'rejected')
                    ) as vlm_human_reviewed,
                    COUNT(*) FILTER (
                        WHERE corrected_tags->>'vlm_model' IS NOT NULL
                          AND review_status = 'approved'
                          AND corrected_tags->>'actual_class' IS NOT NULL
                    ) as vlm_accepted,
                    COUNT(*) FILTER (
                        WHERE corrected_tags->>'vlm_model' IS NOT NULL
                          AND review_status = 'rejected'
                    ) as vlm_overridden_reject,
                    COUNT(*) FILTER (
                        WHERE corrected_tags->>'vlm_model' IS NOT NULL
                          AND review_status = 'approved'
                          AND (corrected_tags->>'actual_class' IS NULL
                               OR corrected_tags->>'actual_class' != corrected_tags->>'vlm_suggested_class')
                    ) as vlm_overridden_approve
                FROM ai_predictions
                WHERE scenario = 'vehicle_detection'
            """)
            row = cursor.fetchone()
            stats = dict(row) if row else {}

            vlm_reviewed = stats.get('vlm_reviewed', 0)
            vlm_human_reviewed = stats.get('vlm_human_reviewed', 0)
            vlm_accepted = stats.get('vlm_accepted', 0)

            stats['acceptance_rate'] = round(vlm_accepted / vlm_human_reviewed * 100, 1) if vlm_human_reviewed > 0 else 0

            # Breakdown by suggested class
            cursor.execute("""
                SELECT
                    corrected_tags->>'vlm_suggested_class' as suggested_class,
                    COUNT(*) as count,
                    COUNT(*) FILTER (WHERE review_status = 'approved'
                                      AND corrected_tags->>'actual_class' IS NOT NULL) as accepted,
                    COUNT(*) FILTER (WHERE review_status IN ('approved', 'rejected')
                                      AND (corrected_tags->>'actual_class' IS NULL
                                           OR corrected_tags->>'actual_class' != corrected_tags->>'vlm_suggested_class')) as overridden
                FROM ai_predictions
                WHERE corrected_tags->>'vlm_model' IS NOT NULL
                  AND corrected_tags->>'vlm_suggested_class' IS NOT NULL
                GROUP BY suggested_class
                ORDER BY count DESC
            """)
            stats['by_class'] = [dict(r) for r in cursor.fetchall()]

            return stats
