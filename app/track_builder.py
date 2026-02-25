"""
Track Builder - Cross-status object tracking for decision propagation.

Builds "camera object tracks" that link predictions of the same real-world object
across ALL review statuses (approved, rejected, pending, auto_approved).
Enables propagating manual review decisions to unreviewed predictions.

Architecture:
    prediction_grouper.py groups only PENDING predictions for the batch review UI.
    This module groups ALL predictions regardless of status to enable decision
    propagation. They are complementary systems operating on the same spatial data.
"""

import json
import logging
from collections import Counter, defaultdict
from datetime import datetime as _dt

from psycopg2.extras import Json

from db_connection import get_cursor
from prediction_grouper import compute_iou

logger = logging.getLogger(__name__)

IOU_THRESHOLD = 0.3
# Per-camera temporal gap overrides (seconds)
CAMERA_TEMPORAL_GAPS = {
    'mwcam8': 120,        # Highway camera - vehicles transit in seconds
    'mwcam9': 120,        # Highway camera
    'mwparkinglot': 7200, # Parking lot - vehicles may park for hours
}
DEFAULT_TEMPORAL_GAP = 300  # 5 minutes default


def _running_avg_bbox(current, new_pred, n):
    """Incrementally update track bbox as running average."""
    if n <= 1:
        return new_pred
    w = 1.0 / n
    return {
        'x': current['x'] * (1 - w) + new_pred['x'] * w,
        'y': current['y'] * (1 - w) + new_pred['y'] * w,
        'width': current['width'] * (1 - w) + new_pred['width'] * w,
        'height': current['height'] * (1 - w) + new_pred['height'] * w,
    }


class TrackBuilder:
    """Builds cross-status object tracks and propagates decisions."""

    def _get_temporal_gap(self, camera_id):
        """Return the temporal gap threshold (seconds) for a camera."""
        return CAMERA_TEMPORAL_GAPS.get(camera_id, DEFAULT_TEMPORAL_GAP)

    def build_tracks(self, camera_id=None):
        """Build camera object tracks for all predictions.

        Groups ALL predictions (any review_status) by IoU on same camera+scenario.

        Args:
            camera_id: optional - only build for this camera. None = all cameras.

        Returns:
            dict with tracks_created, predictions_assigned counts
        """
        # Clear existing tracks
        with get_cursor() as cursor:
            if camera_id:
                cursor.execute(
                    "UPDATE ai_predictions SET camera_object_track_id = NULL "
                    "WHERE camera_object_track_id IN "
                    "(SELECT id FROM camera_object_tracks WHERE camera_id = %s)",
                    (camera_id,)
                )
                cursor.execute(
                    "DELETE FROM camera_object_tracks WHERE camera_id = %s",
                    (camera_id,)
                )
            else:
                cursor.execute(
                    "UPDATE ai_predictions SET camera_object_track_id = NULL "
                    "WHERE camera_object_track_id IS NOT NULL"
                )
                cursor.execute("DELETE FROM camera_object_tracks")

        # Fetch all predictions with camera info
        with get_cursor(commit=False) as cursor:
            conditions = [
                "p.bbox_x IS NOT NULL",
                "p.bbox_width > 0",
                "v.camera_id IS NOT NULL"
            ]
            params = []
            if camera_id:
                conditions.append("v.camera_id = %s")
                params.append(camera_id)

            cursor.execute("""
                SELECT p.id, p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
                       p.confidence, p.timestamp, p.scenario, p.review_status,
                       p.corrected_tags, p.predicted_tags, p.reviewed_by,
                       v.camera_id, v.upload_date,
                       EXTRACT(EPOCH FROM p.created_at) as created_epoch
                FROM ai_predictions p
                JOIN videos v ON p.video_id = v.id
                WHERE {conditions}
                ORDER BY v.camera_id, p.scenario
            """.format(conditions=' AND '.join(conditions)),
                params if params else None
            )
            all_preds = [dict(r) for r in cursor.fetchall()]

        if not all_preds:
            return {'tracks_created': 0, 'predictions_assigned': 0}

        # Partition by (camera_id, scenario)
        partitions = defaultdict(list)
        for p in all_preds:
            partitions[(p['camera_id'], p['scenario'])].append(p)

        total_tracks = 0
        total_assigned = 0

        for (cam_id, scenario), preds in partitions.items():
            if not preds:
                continue

            max_gap = self._get_temporal_gap(cam_id)

            # Sort by upload_date (chronological order)
            preds.sort(key=lambda p: (p.get('upload_date') or _dt.min).timestamp())

            # Active tracks: list of {preds: [...], last_epoch: float, bbox: {x,y,w,h}}
            active_tracks = []

            for pred in preds:
                pred_epoch = pred['upload_date'].timestamp() if pred.get('upload_date') else None
                pred_box = {
                    'x': pred['bbox_x'], 'y': pred['bbox_y'],
                    'width': pred['bbox_width'], 'height': pred['bbox_height']
                }

                best_track = None
                best_iou = 0

                for track in active_tracks:
                    # Skip expired tracks
                    if pred_epoch is not None and track['last_epoch'] is not None:
                        if pred_epoch - track['last_epoch'] > max_gap:
                            continue

                    iou = compute_iou(pred_box, track['bbox'])
                    if iou >= IOU_THRESHOLD and iou > best_iou:
                        best_iou = iou
                        best_track = track

                if best_track:
                    best_track['preds'].append(pred)
                    if pred_epoch is not None:
                        best_track['last_epoch'] = max(best_track['last_epoch'] or 0, pred_epoch)
                    # Update running average bbox for better matching
                    n = len(best_track['preds'])
                    best_track['bbox'] = _running_avg_bbox(best_track['bbox'], pred_box, n)
                else:
                    active_tracks.append({
                        'preds': [pred],
                        'last_epoch': pred_epoch,
                        'bbox': pred_box
                    })

            # Create DB tracks from accumulated groups
            for track_data in active_tracks:
                track_id = self._create_track(cam_id, scenario, track_data['preds'])
                if track_id:
                    pred_ids = [p['id'] for p in track_data['preds']]
                    self._assign_to_track(pred_ids, track_id)
                    total_tracks += 1
                    total_assigned += len(pred_ids)

        logger.info(
            "Track building complete: %d tracks, %d predictions",
            total_tracks, total_assigned
        )
        return {'tracks_created': total_tracks, 'predictions_assigned': total_assigned}

    def _create_track(self, camera_id, scenario, preds):
        """Create a camera_object_track from a list of predictions.

        Computes spatial statistics, status counts, anchor status, and
        classification consensus from the group members.

        Args:
            camera_id: camera identifier
            scenario: prediction scenario (e.g. 'vehicle_detection')
            preds: list of prediction dicts in this track

        Returns:
            int track ID, or None on failure
        """
        # Compute spatial stats
        centroids_x = [p['bbox_x'] + p['bbox_width'] / 2 for p in preds]
        centroids_y = [p['bbox_y'] + p['bbox_height'] / 2 for p in preds]
        widths = [p['bbox_width'] for p in preds]
        heights = [p['bbox_height'] for p in preds]
        confidences = [p['confidence'] for p in preds if p.get('confidence') is not None]

        # Compute temporal bounds from video upload_date (actual detection time)
        upload_epochs = [p['upload_date'].timestamp() if p.get('upload_date') else None for p in preds]
        upload_epochs = [e for e in upload_epochs if e is not None]

        # Count by status
        status_counts = defaultdict(int)
        for p in preds:
            status_counts[p['review_status']] += 1

        # Determine anchor_status from MANUAL reviews only (not auto)
        manual_approved = status_counts.get('approved', 0)
        manual_rejected = status_counts.get('rejected', 0)

        if manual_approved > 0 and manual_rejected > 0:
            anchor_status = 'conflict'
        elif manual_approved > 0:
            anchor_status = 'approved'
        elif manual_rejected > 0:
            anchor_status = 'rejected'
        else:
            anchor_status = 'pending'

        # Determine classification from corrected_tags
        anchor_classification = None
        classification_conflict = False
        classifications = []
        for p in preds:
            ct = p.get('corrected_tags')
            if ct and isinstance(ct, dict) and ct.get('vehicle_subtype'):
                classifications.append(ct['vehicle_subtype'])

        if classifications:
            unique = set(classifications)
            if len(unique) == 1:
                anchor_classification = {'vehicle_subtype': classifications[0]}
            else:
                classification_conflict = True
                most_common = Counter(classifications).most_common(1)[0][0]
                anchor_classification = {
                    'vehicle_subtype': most_common,
                    'conflict_options': sorted(unique)
                }

        # Representative = highest confidence
        rep = max(preds, key=lambda p: p.get('confidence') or 0)

        try:
            with get_cursor() as cursor:
                cursor.execute("""
                    INSERT INTO camera_object_tracks
                    (camera_id, scenario, bbox_centroid_x, bbox_centroid_y,
                     avg_bbox_width, avg_bbox_height, member_count,
                     approved_count, rejected_count, pending_count, auto_approved_count,
                     anchor_status, anchor_classification, classification_conflict,
                     representative_prediction_id,
                     min_confidence, max_confidence, avg_confidence,
                     first_seen, last_seen)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (
                    camera_id, scenario,
                    int(sum(centroids_x) / len(centroids_x)),
                    int(sum(centroids_y) / len(centroids_y)),
                    int(sum(widths) / len(widths)),
                    int(sum(heights) / len(heights)),
                    len(preds),
                    manual_approved,
                    manual_rejected,
                    status_counts.get('pending', 0),
                    status_counts.get('auto_approved', 0),
                    anchor_status,
                    Json(anchor_classification) if anchor_classification else None,
                    classification_conflict,
                    rep['id'],
                    min(confidences) if confidences else None,
                    max(confidences) if confidences else None,
                    sum(confidences) / len(confidences) if confidences else None,
                    min(upload_epochs) if upload_epochs else None,
                    max(upload_epochs) if upload_epochs else None
                ))
                row = cursor.fetchone()
                return row['id'] if row else None
        except Exception as e:
            logger.error("Error creating track for camera=%s scenario=%s: %s",
                         camera_id, scenario, e)
            return None

    def _assign_to_track(self, prediction_ids, track_id):
        """Assign predictions to a track by setting camera_object_track_id.

        Args:
            prediction_ids: list of prediction IDs
            track_id: target track ID
        """
        try:
            with get_cursor() as cursor:
                cursor.execute("""
                    UPDATE ai_predictions
                    SET camera_object_track_id = %s
                    WHERE id = ANY(%s)
                """, (track_id, prediction_ids))
        except Exception as e:
            logger.error("Error assigning %d predictions to track %d: %s",
                         len(prediction_ids), track_id, e)

    def propagate_decisions(self, camera_id=None, dry_run=False):
        """Propagate anchor decisions to pending members of each track.

        For tracks with anchor_status='approved':
            - Auto-approve all pending members
            - Create annotations for newly approved predictions
            - Propagate classification if available and not conflicting

        For tracks with anchor_status='rejected':
            - Auto-reject all pending members

        For tracks with anchor_status='conflict':
            - Skip (needs manual resolution)

        Args:
            camera_id: optional - only propagate for this camera
            dry_run: if True, return what WOULD happen without making changes

        Returns:
            dict with auto_approved, auto_rejected, classifications_propagated,
                 annotations_created, conflicts_found, tracks_processed, skipped counts
        """
        results = {
            'auto_approved': 0,
            'auto_rejected': 0,
            'classifications_propagated': 0,
            'annotations_created': 0,
            'conflicts_found': 0,
            'tracks_processed': 0,
            'skipped': 0
        }

        # Get tracks with anchor decisions that still have pending members
        with get_cursor(commit=False) as cursor:
            conditions = ["anchor_status != 'pending'", "pending_count > 0"]
            params = []
            if camera_id:
                conditions.append("camera_id = %s")
                params.append(camera_id)

            cursor.execute("""
                SELECT id, camera_id, scenario, anchor_status, anchor_classification,
                       classification_conflict, pending_count, member_count
                FROM camera_object_tracks
                WHERE {conditions}
                ORDER BY member_count DESC
            """.format(conditions=' AND '.join(conditions)),
                params if params else None
            )
            tracks = [dict(r) for r in cursor.fetchall()]

        if not tracks:
            logger.info("No tracks with pending members needing propagation")
            return results

        for track in tracks:
            results['tracks_processed'] += 1

            if track['anchor_status'] == 'conflict':
                results['conflicts_found'] += 1
                logger.debug("Skipping conflict track %d (%d pending)",
                             track['id'], track['pending_count'])
                continue

            if dry_run:
                if track['anchor_status'] == 'approved':
                    results['auto_approved'] += track['pending_count']
                elif track['anchor_status'] == 'rejected':
                    results['auto_rejected'] += track['pending_count']
                continue

            if track['anchor_status'] == 'approved':
                count = self._propagate_approval(track)
                results['auto_approved'] += count['approved']
                results['annotations_created'] += count['annotations']

                # Propagate classification if available and not conflicting
                if track.get('anchor_classification') and not track.get('classification_conflict'):
                    cls_count = self._propagate_classification(track)
                    results['classifications_propagated'] += cls_count

            elif track['anchor_status'] == 'rejected':
                count = self._propagate_rejection(track)
                results['auto_rejected'] += count

        # Update track stats after propagation
        if not dry_run:
            self._refresh_track_stats(camera_id)

        logger.info("Propagation complete: %s", results)
        return results

    def _propagate_approval(self, track):
        """Approve all pending predictions in a track and create annotations.

        Args:
            track: track dict with id, anchor_status, etc.

        Returns:
            dict with 'approved' and 'annotations' counts
        """
        count = {'approved': 0, 'annotations': 0}

        with get_cursor() as cursor:
            cursor.execute("""
                UPDATE ai_predictions
                SET review_status = 'approved',
                    reviewed_by = 'track_propagation',
                    reviewed_at = NOW(),
                    review_notes = %s
                WHERE camera_object_track_id = %s
                  AND review_status IN ('pending', 'processing')
                RETURNING id
            """, (
                "Auto-approved via track #%d (anchor has manual approvals)" % track['id'],
                track['id']
            ))
            updated = cursor.fetchall()
            count['approved'] = len(updated)

        # Create annotations for each approved prediction
        # Import here to avoid circular imports with database.py
        from database import VideoDatabase
        db = VideoDatabase()
        for row in updated:
            try:
                ann_id = db.approve_prediction_to_annotation(row['id'])
                if ann_id:
                    count['annotations'] += 1
            except Exception as e:
                logger.error("Error creating annotation for prediction %d: %s",
                             row['id'], e)

        return count

    def _propagate_rejection(self, track):
        """Reject all pending predictions in a track.

        Args:
            track: track dict with id, etc.

        Returns:
            int count of rejected predictions
        """
        with get_cursor() as cursor:
            cursor.execute("""
                UPDATE ai_predictions
                SET review_status = 'rejected',
                    reviewed_by = 'track_propagation',
                    reviewed_at = NOW(),
                    review_notes = %s
                WHERE camera_object_track_id = %s
                  AND review_status IN ('pending', 'processing')
                RETURNING id
            """, (
                "Auto-rejected via track #%d (anchor has manual rejections)" % track['id'],
                track['id']
            ))
            return cursor.rowcount

    def _propagate_classification(self, track):
        """Propagate classification to unclassified approved members of a track.

        Only propagates to predictions in the 'vehicle_detection' scenario that
        have been approved/auto_approved but lack a vehicle_subtype classification.

        Args:
            track: track dict with anchor_classification, id, etc.

        Returns:
            int count of predictions classified
        """
        classification = track.get('anchor_classification')
        if not classification or not isinstance(classification, dict):
            return 0

        vehicle_subtype = classification.get('vehicle_subtype')
        if not vehicle_subtype:
            return 0

        import datetime
        classification_payload = json.dumps({
            'vehicle_subtype': vehicle_subtype,
            'classified_by': 'track_propagation',
            'classified_at': datetime.datetime.utcnow().isoformat()
        })

        with get_cursor() as cursor:
            cursor.execute("""
                UPDATE ai_predictions
                SET corrected_tags = COALESCE(corrected_tags, '{}'::jsonb) || %s::jsonb
                WHERE camera_object_track_id = %s
                  AND scenario = 'vehicle_detection'
                  AND (corrected_tags IS NULL
                       OR corrected_tags->>'vehicle_subtype' IS NULL)
                  AND review_status IN ('approved', 'auto_approved')
                RETURNING id
            """, (classification_payload, track['id']))
            return cursor.rowcount

    def _refresh_track_stats(self, camera_id=None):
        """Recompute status counts for all tracks after propagation.

        Args:
            camera_id: optional - only refresh for this camera
        """
        try:
            with get_cursor() as cursor:
                if camera_id:
                    cursor.execute("""
                        UPDATE camera_object_tracks t SET
                            member_count = COALESCE(s.total, 0),
                            approved_count = COALESCE(s.approved, 0),
                            rejected_count = COALESCE(s.rejected, 0),
                            pending_count = COALESCE(s.pending, 0),
                            auto_approved_count = COALESCE(s.auto_approved, 0),
                            updated_at = NOW()
                        FROM (
                            SELECT camera_object_track_id,
                                   COUNT(*) as total,
                                   COUNT(*) FILTER (WHERE review_status = 'approved') as approved,
                                   COUNT(*) FILTER (WHERE review_status = 'rejected') as rejected,
                                   COUNT(*) FILTER (WHERE review_status = 'pending') as pending,
                                   COUNT(*) FILTER (WHERE review_status = 'auto_approved') as auto_approved
                            FROM ai_predictions
                            WHERE camera_object_track_id IS NOT NULL
                            GROUP BY camera_object_track_id
                        ) s
                        WHERE t.id = s.camera_object_track_id
                          AND t.camera_id = %s
                    """, (camera_id,))
                else:
                    cursor.execute("""
                        UPDATE camera_object_tracks t SET
                            member_count = COALESCE(s.total, 0),
                            approved_count = COALESCE(s.approved, 0),
                            rejected_count = COALESCE(s.rejected, 0),
                            pending_count = COALESCE(s.pending, 0),
                            auto_approved_count = COALESCE(s.auto_approved, 0),
                            updated_at = NOW()
                        FROM (
                            SELECT camera_object_track_id,
                                   COUNT(*) as total,
                                   COUNT(*) FILTER (WHERE review_status = 'approved') as approved,
                                   COUNT(*) FILTER (WHERE review_status = 'rejected') as rejected,
                                   COUNT(*) FILTER (WHERE review_status = 'pending') as pending,
                                   COUNT(*) FILTER (WHERE review_status = 'auto_approved') as auto_approved
                            FROM ai_predictions
                            WHERE camera_object_track_id IS NOT NULL
                            GROUP BY camera_object_track_id
                        ) s
                        WHERE t.id = s.camera_object_track_id
                    """)
        except Exception as e:
            logger.error("Error refreshing track stats: %s", e)

    def match_new_predictions(self, prediction_ids):
        """Match new predictions against existing tracks.

        For each new prediction:
        1. Get its camera_id from video
        2. Find existing tracks for that camera+scenario
        3. Compute IoU with track average bbox
        4. If IoU >= threshold: assign to track and apply anchor decision
        5. Otherwise: leave unmatched for next full build_tracks run

        Args:
            prediction_ids: list of new prediction IDs

        Returns:
            dict with matched, auto_approved, auto_rejected, auto_classified,
                 annotations_created, unmatched counts
        """
        if not prediction_ids:
            return {
                'matched': 0, 'auto_approved': 0, 'auto_rejected': 0,
                'auto_classified': 0, 'annotations_created': 0, 'unmatched': 0
            }

        results = {
            'matched': 0,
            'auto_approved': 0,
            'auto_rejected': 0,
            'auto_classified': 0,
            'annotations_created': 0,
            'unmatched': 0
        }

        # Fetch new predictions with camera info
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT p.id, p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
                       p.confidence, p.timestamp, p.scenario, p.review_status,
                       v.camera_id, v.upload_date
                FROM ai_predictions p
                JOIN videos v ON p.video_id = v.id
                WHERE p.id = ANY(%s)
                  AND p.bbox_x IS NOT NULL
                  AND p.bbox_width > 0
                  AND v.camera_id IS NOT NULL
            """, (prediction_ids,))
            preds = [dict(r) for r in cursor.fetchall()]

        if not preds:
            return results

        # Partition by (camera_id, scenario)
        partitions = defaultdict(list)
        for p in preds:
            partitions[(p['camera_id'], p['scenario'])].append(p)

        # Cache existing tracks per camera+scenario
        track_cache = {}

        for (cam_id, scenario), partition_preds in partitions.items():
            cache_key = (cam_id, scenario)
            if cache_key not in track_cache:
                with get_cursor(commit=False) as cursor:
                    cursor.execute("""
                        SELECT id, bbox_centroid_x, bbox_centroid_y,
                               avg_bbox_width, avg_bbox_height,
                               anchor_status, anchor_classification,
                               classification_conflict, last_seen
                        FROM camera_object_tracks
                        WHERE camera_id = %s AND scenario = %s
                    """, (cam_id, scenario))
                    track_cache[cache_key] = [dict(r) for r in cursor.fetchall()]

            existing_tracks = track_cache[cache_key]
            if not existing_tracks:
                results['unmatched'] += len(partition_preds)
                continue

            max_gap = self._get_temporal_gap(cam_id)

            for pred in partition_preds:
                pred_box = {
                    'x': pred['bbox_x'], 'y': pred['bbox_y'],
                    'width': pred['bbox_width'], 'height': pred['bbox_height']
                }
                pred_epoch = pred['upload_date'].timestamp() if pred.get('upload_date') else None

                best_track = None
                best_iou = 0

                for track in existing_tracks:
                    # Check temporal relevance
                    if pred_epoch is not None and track.get('last_seen') is not None:
                        if abs(pred_epoch - track['last_seen']) > max_gap:
                            continue

                    track_box = {
                        'x': track['bbox_centroid_x'] - track['avg_bbox_width'] // 2,
                        'y': track['bbox_centroid_y'] - track['avg_bbox_height'] // 2,
                        'width': track['avg_bbox_width'],
                        'height': track['avg_bbox_height']
                    }
                    iou = compute_iou(pred_box, track_box)
                    if iou >= IOU_THRESHOLD and iou > best_iou:
                        best_iou = iou
                        best_track = track

                if best_track:
                    self._assign_to_track([pred['id']], best_track['id'])
                    results['matched'] += 1

                    # Auto-approval/rejection DISABLED — all predictions must go
                    # through human review. Track assignment still happens above
                    # for grouping purposes, but review_status is not changed.
                    # Previously: auto-approved/rejected based on track anchor_status.
                else:
                    results['unmatched'] += 1

        # Refresh track stats for affected cameras
        affected_cameras = set(cam for cam, _ in partitions.keys())
        for cam in affected_cameras:
            self._refresh_track_stats(cam)

        logger.info("Track matching: %s", results)
        return results

    def _auto_approve_prediction(self, prediction_id, track, results):
        """Auto-approve a single prediction via track propagation.

        Args:
            prediction_id: prediction to approve
            track: the anchor track
            results: mutable results dict to update counts
        """
        try:
            with get_cursor() as cursor:
                cursor.execute("""
                    UPDATE ai_predictions
                    SET review_status = 'approved',
                        reviewed_by = 'track_propagation',
                        reviewed_at = NOW(),
                        review_notes = %s
                    WHERE id = %s AND review_status IN ('pending', 'processing')
                """, (
                    "Auto-approved via track #%d" % track['id'],
                    prediction_id
                ))
                if cursor.rowcount > 0:
                    results['auto_approved'] += 1

            # Create annotation
            from database import VideoDatabase
            db = VideoDatabase()
            ann_id = db.approve_prediction_to_annotation(prediction_id)
            if ann_id:
                results['annotations_created'] += 1
        except Exception as e:
            logger.error("Error auto-approving prediction %d: %s", prediction_id, e)

    def _auto_reject_prediction(self, prediction_id, track, results):
        """Auto-reject a single prediction via track propagation.

        Args:
            prediction_id: prediction to reject
            track: the anchor track
            results: mutable results dict to update counts
        """
        try:
            with get_cursor() as cursor:
                cursor.execute("""
                    UPDATE ai_predictions
                    SET review_status = 'rejected',
                        reviewed_by = 'track_propagation',
                        reviewed_at = NOW(),
                        review_notes = %s
                    WHERE id = %s AND review_status IN ('pending', 'processing')
                """, (
                    "Auto-rejected via track #%d" % track['id'],
                    prediction_id
                ))
                if cursor.rowcount > 0:
                    results['auto_rejected'] += 1
        except Exception as e:
            logger.error("Error auto-rejecting prediction %d: %s", prediction_id, e)

    def _auto_classify_prediction(self, prediction_id, track, results):
        """Auto-classify a single prediction from track anchor classification.

        Args:
            prediction_id: prediction to classify
            track: the anchor track with anchor_classification
            results: mutable results dict to update counts
        """
        classification = track.get('anchor_classification')
        if not classification or not isinstance(classification, dict):
            return

        vehicle_subtype = classification.get('vehicle_subtype')
        if not vehicle_subtype:
            return

        try:
            classification_payload = json.dumps({
                'vehicle_subtype': vehicle_subtype,
                'classified_by': 'track_propagation'
            })
            with get_cursor() as cursor:
                cursor.execute("""
                    UPDATE ai_predictions
                    SET corrected_tags = COALESCE(corrected_tags, '{}'::jsonb) || %s::jsonb
                    WHERE id = %s
                      AND (corrected_tags IS NULL
                           OR corrected_tags->>'vehicle_subtype' IS NULL)
                """, (classification_payload, prediction_id))
                if cursor.rowcount > 0:
                    results['auto_classified'] += 1
        except Exception as e:
            logger.error("Error auto-classifying prediction %d: %s", prediction_id, e)

    def get_conflicts(self, camera_id=None):
        """Get tracks with conflicting decisions that need manual resolution.

        Returns tracks where:
        - anchor_status = 'conflict' (mixed approve/reject from manual reviews)
        - classification_conflict = True (mixed vehicle subtypes)

        Args:
            camera_id: optional - filter by camera

        Returns:
            list of track dicts with representative prediction and video info
        """
        with get_cursor(commit=False) as cursor:
            conditions = [
                "(anchor_status = 'conflict' OR classification_conflict = TRUE)"
            ]
            params = []
            if camera_id:
                conditions.append("t.camera_id = %s")
                params.append(camera_id)

            cursor.execute("""
                SELECT t.*,
                       p.video_id, p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
                       p.confidence, p.timestamp, p.predicted_tags,
                       v.title as video_title, v.thumbnail_path,
                       v.width as video_width, v.height as video_height
                FROM camera_object_tracks t
                LEFT JOIN ai_predictions p ON p.id = t.representative_prediction_id
                LEFT JOIN videos v ON p.video_id = v.id
                WHERE {conditions}
                ORDER BY t.member_count DESC
            """.format(conditions=' AND '.join(conditions)),
                params if params else None
            )
            return [dict(r) for r in cursor.fetchall()]

    def get_track_summary(self, camera_id=None):
        """Get summary statistics for tracks grouped by anchor_status.

        Args:
            camera_id: optional - filter by camera

        Returns:
            dict with by_status breakdown, total_tracks, total_predictions,
                 total_pending, and propagatable count
        """
        with get_cursor(commit=False) as cursor:
            conditions = []
            params = []
            if camera_id:
                conditions.append("camera_id = %s")
                params.append(camera_id)

            where = "WHERE " + ' AND '.join(conditions) if conditions else ""

            cursor.execute("""
                SELECT
                    anchor_status,
                    COUNT(*) as track_count,
                    SUM(member_count) as total_predictions,
                    SUM(pending_count) as total_pending,
                    SUM(approved_count) as total_approved,
                    SUM(rejected_count) as total_rejected,
                    COUNT(*) FILTER (WHERE classification_conflict) as classification_conflicts
                FROM camera_object_tracks
                {where}
                GROUP BY anchor_status
                ORDER BY anchor_status
            """.format(where=where),
                params if params else None
            )
            rows = [dict(r) for r in cursor.fetchall()]

            summary = {
                'by_status': rows,
                'total_tracks': sum(r['track_count'] for r in rows),
                'total_predictions': sum(r['total_predictions'] or 0 for r in rows),
                'total_pending': sum(r['total_pending'] or 0 for r in rows),
                'propagatable': sum(
                    r['total_pending'] or 0 for r in rows
                    if r['anchor_status'] in ('approved', 'rejected')
                )
            }
            return summary
