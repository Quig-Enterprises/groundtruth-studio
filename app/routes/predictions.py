from flask import Blueprint, request, jsonify, render_template, send_from_directory, send_file, g, redirect
from pathlib import Path
from psycopg2 import extras
from db_connection import get_connection, get_cursor
import services
from services import db, sample_router, processor, THUMBNAIL_DIR, DOWNLOAD_DIR, BASE_DIR
from auto_detect_runner import run_detection_on_thumbnail
from vehicle_detect_runner import trigger_vehicle_detect
from frigate_ingester import get_ingester
import os
import io
import json
import logging
import time
import threading

predictions_bp = Blueprint('predictions', __name__)
logger = logging.getLogger(__name__)


# ---- Page Routes ----

@predictions_bp.route('/prediction-review')
def prediction_review_page():
    return render_template('prediction_review.html')


@predictions_bp.route('/vehicle-metrics')
def vehicle_metrics_page():
    return render_template('vehicle_metrics.html')


@predictions_bp.route('/review')
def mobile_review_page():
    return render_template('prediction_review_mobile.html')


@predictions_bp.route('/cross-camera-review')
def cross_camera_review_page():
    return redirect('/review?filter=cross_camera')


@predictions_bp.route('/vehicle-metrics/<path:class_name>')
def vehicle_class_detail_page(class_name):
    """Render vehicle class detail page for a specific class."""
    return render_template('vehicle_class_detail.html', class_name=class_name)


# ---- Prediction Clip ----

@predictions_bp.route('/api/ai/predictions/<int:prediction_id>/clip')
def get_prediction_clip(prediction_id):
    """Generate and serve a ~5 second video clip around a prediction's timestamp."""
    try:
        pred = db.get_prediction_by_id(prediction_id)
        if not pred:
            return jsonify({'success': False, 'error': 'Prediction not found'}), 404

        video = db.get_video(pred['video_id'])
        if not video:
            return jsonify({'success': False, 'error': 'Video not found'}), 404

        video_path = DOWNLOAD_DIR / video['filename']
        timestamp = pred.get('timestamp') or 0.0

        # Try local ffmpeg clip extraction first
        result = processor.extract_clip(
            str(video_path),
            float(timestamp),
            duration=5.0
        )

        if result['success']:
            clip_path = Path(result['clip_path'])
            return send_from_directory(clip_path.parent, clip_path.name, mimetype='video/mp4')

        # Fallback: try Frigate event clip if metadata has frigate_event_id
        metadata = video.get('metadata') or {}
        if isinstance(metadata, str):
            import json
            try:
                metadata = json.loads(metadata)
            except (json.JSONDecodeError, TypeError):
                metadata = {}

        frigate_event_id = metadata.get('frigate_event_id')
        if frigate_event_id:
            frigate_url = os.environ.get('FRIGATE_URL', 'http://localhost:5000')
            if not frigate_url:
                # Try to get from ingester singleton
                try:
                    ingester = get_ingester()
                    if ingester:
                        frigate_url = ingester.frigate_url
                except Exception:
                    pass

            if frigate_url:
                frigate_result = processor.fetch_frigate_clip(
                    frigate_url=frigate_url,
                    event_id=frigate_event_id,
                    camera=metadata.get('frigate_camera', '')
                )
                if frigate_result['success']:
                    clip_path = Path(frigate_result['clip_path'])
                    return send_from_directory(clip_path.parent, clip_path.name, mimetype='video/mp4')

        return jsonify({'success': False, 'error': result['error']}), 404

    except Exception as e:
        logger.error(f'Failed to generate clip for prediction {prediction_id}: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


# ---- Auto-Detect ----

@predictions_bp.route('/api/ai/auto-detect/<int:video_id>', methods=['POST'])
def run_auto_detect(video_id):
    """Run person/face auto-detection on a video's thumbnail."""
    try:
        video = db.get_video(video_id)
        if not video:
            return jsonify({'success': False, 'error': 'Video not found'}), 404
        if not video.get('thumbnail_path'):
            return jsonify({'success': False, 'error': 'Video has no thumbnail'}), 400

        result = run_detection_on_thumbnail(video_id, video['thumbnail_path'])
        if result is None:
            return jsonify({'success': False, 'error': 'Auto-detect failed (model not available or detection error)'}), 500

        return jsonify({'success': True, **result})
    except Exception as e:
        logger.error(f"Auto-detect endpoint error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@predictions_bp.route('/api/ai/auto-detect/all', methods=['POST'])
def run_auto_detect_all():
    """Run person/face auto-detection on all videos with thumbnails (background)."""
    try:
        def _run_all():
            videos = db.get_all_videos()
            for v in videos:
                if v.get('thumbnail_path') and os.path.exists(v['thumbnail_path']):
                    run_detection_on_thumbnail(v['id'], v['thumbnail_path'])

        thread = threading.Thread(target=_run_all, daemon=True, name="auto-detect-all")
        thread.start()
        return jsonify({'success': True, 'message': 'Auto-detect started for all videos in background'})
    except Exception as e:
        logger.error(f"Auto-detect-all endpoint error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ---- Batch Submission ----

@predictions_bp.route('/api/ai/predictions/batch', methods=['POST'])
def submit_predictions_batch():
    """Submit a batch of AI predictions for routing and review"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'Request body required'}), 400

        video_id = data.get('video_id')
        model_name = data.get('model_name')
        model_version = data.get('model_version')
        predictions = data.get('predictions', [])
        batch_id = data.get('batch_id')
        force_review = data.get('force_review', False)

        if not all([video_id, model_name, model_version, predictions]):
            return jsonify({'success': False, 'error': 'video_id, model_name, model_version, and predictions required'}), 400

        # Verify video exists
        video = db.get_video(video_id)
        if not video:
            return jsonify({'success': False, 'error': f'Video {video_id} not found'}), 404

        # Ensure model is registered
        model_type = data.get('model_type', 'yolo')
        db.get_or_create_model_registry(model_name, model_version, model_type)

        # Dedup: skip if this model/version already has predictions for this video
        existing_count = db.count_predictions_for_video(video_id, model_name, model_version)
        if existing_count > 0:
            return jsonify({
                'success': True,
                'batch_id': batch_id,
                'predictions_submitted': 0,
                'prediction_ids': [],
                'routing': {},
                'skipped': True,
                'reason': f'Video already has {existing_count} predictions from {model_name} v{model_version}'
            })

        # Insert predictions with 'processing' status so they don't appear
        # in the review queue until automated processing completes
        prediction_ids = db.insert_predictions_batch(
            video_id, model_name, model_version, batch_id, predictions,
            initial_status='processing'
        )

        # Run automated processing, then promote to reviewable status
        if prediction_ids:
            def _process_and_promote(pred_ids, _force_review):
                try:
                    # Phase 1: Match against existing tracks (auto-approve/reject known objects)
                    try:
                        from track_builder import TrackBuilder
                        builder = TrackBuilder()
                        track_result = builder.match_new_predictions(pred_ids)
                        if track_result.get('matched', 0) > 0:
                            logger.info(f"Track matching: {track_result['matched']} matched, "
                                       f"{track_result['auto_approved']} auto-approved, "
                                       f"{track_result['auto_rejected']} auto-rejected")
                    except Exception as track_err:
                        logger.warning(f"Track matching failed: {track_err}")

                    # Phase 2: Group remaining unmatched predictions
                    try:
                        from prediction_grouper import PredictionGrouper
                        PredictionGrouper().run_grouping_for_batch(pred_ids)
                    except Exception as group_err:
                        logger.warning(f"Prediction grouping trigger failed: {group_err}")

                    # Phase 2.5: VLM review — pre-analyze detections for false positives
                    try:
                        import vlm_reviewer
                        if vlm_reviewer.VLM_ENABLED:
                            with get_connection() as conn:
                                cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
                                cursor.execute("""
                                    SELECT p.id, p.confidence, p.timestamp,
                                           p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
                                           p.predicted_tags, p.scenario,
                                           v.filename
                                    FROM ai_predictions p
                                    JOIN videos v ON p.video_id = v.id
                                    WHERE p.id = ANY(%s)
                                      AND p.review_status = 'processing'
                                      AND p.scenario = 'vehicle_detection'
                                """, (pred_ids,))
                                vlm_candidates = cursor.fetchall()

                                vlm_count = 0
                                for cand in vlm_candidates:
                                    # Skip high-confidence YOLO detections
                                    if float(cand['confidence'] or 0) >= vlm_reviewer.SKIP_ABOVE_CONFIDENCE:
                                        continue

                                    video_path = DOWNLOAD_DIR / cand['filename']
                                    if not video_path.exists():
                                        continue

                                    tags = cand['predicted_tags'] or {}
                                    if isinstance(tags, str):
                                        tags = json.loads(tags)
                                    predicted_class = tags.get('class', tags.get('vehicle_type', 'vehicle'))

                                    bbox = {
                                        'x': cand['bbox_x'] or 0,
                                        'y': cand['bbox_y'] or 0,
                                        'width': cand['bbox_width'] or 0,
                                        'height': cand['bbox_height'] or 0
                                    }

                                    result = vlm_reviewer.classify_detection(
                                        str(video_path),
                                        cand['timestamp'] or 0,
                                        bbox,
                                        predicted_class,
                                        float(cand['confidence'] or 0),
                                        cand['scenario'] or 'vehicle_detection'
                                    )

                                    if result and not result['is_vehicle'] and result['confidence'] >= vlm_reviewer.VLM_CONFIDENCE_THRESHOLD:
                                        cursor.execute("""
                                            UPDATE ai_predictions
                                            SET corrected_tags = COALESCE(corrected_tags, '{}'::jsonb) || %s::jsonb
                                            WHERE id = %s
                                        """, (
                                            extras.Json({
                                                'actual_class': result['suggested_class'],
                                                'vlm_confidence': result['confidence'],
                                                'vlm_reasoning': result['reasoning'],
                                                'vlm_model': vlm_reviewer.VLM_MODEL,
                                                'vlm_suggested_class': result['suggested_class'],
                                                'needs_negative_review': True
                                            }),
                                            cand['id']
                                        ))
                                        vlm_count += 1

                                conn.commit()
                                if vlm_count > 0:
                                    logger.info(f"VLM review: {vlm_count}/{len(vlm_candidates)} detections flagged as non-vehicle")
                    except Exception as vlm_err:
                        logger.warning(f"VLM review failed (non-blocking): {vlm_err}")

                    # Phase 2.7: Clip-based tracking (if video has a Frigate clip)
                    try:
                        _video_meta = video.get('metadata') or {}
                        if isinstance(_video_meta, str):
                            _video_meta = json.loads(_video_meta)
                        _frigate_eid = _video_meta.get('frigate_event_id')
                        if _frigate_eid:
                            from clip_tracker import run_clip_tracking
                            _cam = _video_meta.get('frigate_camera', _video_meta.get('camera_id', ''))
                            threading.Thread(
                                target=run_clip_tracking,
                                args=(video_id, _cam, _frigate_eid),
                                daemon=True,
                                name=f"clip-track-{video_id}"
                            ).start()
                    except Exception as clip_err:
                        logger.warning(f"Clip tracking trigger failed (non-blocking): {clip_err}")

                finally:
                    # Phase 3: Promote remaining 'processing' predictions to reviewable status
                    # (Track matching may have already set some to auto_approved/auto_rejected)
                    try:
                        with get_connection() as conn:
                            cursor = conn.cursor()
                            if _force_review:
                                # Force all still-processing to pending
                                cursor.execute("""
                                    UPDATE ai_predictions
                                    SET review_status = 'pending', routed_by = 'manual'
                                    WHERE id = ANY(%s) AND review_status = 'processing'
                                """, (pred_ids,))
                            else:
                                # Apply confidence-based routing to remaining processing predictions
                                cursor.execute("""
                                    SELECT id, confidence FROM ai_predictions
                                    WHERE id = ANY(%s) AND review_status = 'processing'
                                """, (pred_ids,))
                                still_processing = [r[0] for r in cursor.fetchall()]
                                if still_processing:
                                    # Set to pending first, then let router decide
                                    cursor.execute("""
                                        UPDATE ai_predictions
                                        SET review_status = 'pending'
                                        WHERE id = ANY(%s) AND review_status = 'processing'
                                    """, (still_processing,))
                                    conn.commit()
                                    sample_router.route_and_apply(
                                        still_processing, predictions, model_name, model_version
                                    )
                                    return
                            conn.commit()
                            promoted = cursor.rowcount
                            if promoted > 0:
                                logger.info(f"Promoted {promoted} predictions from processing to review")
                    except Exception as promote_err:
                        logger.error(f"Failed to promote predictions from processing: {promote_err}")
                        # Safety net: promote all to pending so they don't get stuck
                        try:
                            with get_connection() as conn:
                                cursor = conn.cursor()
                                cursor.execute("""
                                    UPDATE ai_predictions
                                    SET review_status = 'pending'
                                    WHERE id = ANY(%s) AND review_status = 'processing'
                                """, (pred_ids,))
                                conn.commit()
                        except Exception:
                            logger.error(f"Safety net promotion also failed for {len(pred_ids)} predictions")

            t = threading.Thread(
                target=_process_and_promote,
                args=(prediction_ids, force_review),
                daemon=True
            )
            t.start()

        # Routing summary is deferred to the background thread now
        routing_summary = {'deferred': True, 'prediction_count': len(prediction_ids)}

        return jsonify({
            'success': True,
            'batch_id': batch_id,
            'predictions_submitted': len(prediction_ids),
            'prediction_ids': prediction_ids,
            'routing': routing_summary
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ---- Prediction CRUD & Review ----

@predictions_bp.route('/api/ai/predictions/pending', methods=['GET'])
def get_pending_predictions():
    """Get pending predictions for review. Pass include_all=1 to include auto_rejected."""
    try:
        video_id = request.args.get('video_id', type=int)
        model_name = request.args.get('model_name')
        limit = request.args.get('limit', 100, type=int)
        offset = request.args.get('offset', 0, type=int)
        include_all = request.args.get('include_all', '0') == '1'

        if include_all and video_id:
            predictions = db.get_predictions_for_video(video_id, limit=limit, offset=offset)
        else:
            predictions = db.get_pending_predictions(video_id, model_name, limit, offset)
        return jsonify({'success': True, 'predictions': predictions})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@predictions_bp.route('/api/ai/predictions/<int:prediction_id>', methods=['GET'])
def get_prediction(prediction_id):
    """Get a single prediction by ID"""
    try:
        prediction = db.get_prediction_by_id(prediction_id)
        if not prediction:
            return jsonify({'success': False, 'error': 'Prediction not found'}), 404
        return jsonify({'success': True, 'prediction': prediction})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@predictions_bp.route('/api/ai/predictions/<int:prediction_id>/children', methods=['GET'])
def get_prediction_children(prediction_id):
    """Get child predictions (plates, registrations) linked to a parent entity prediction."""
    try:
        children = db.get_child_predictions(prediction_id)
        return jsonify({
            'success': True,
            'parent_id': prediction_id,
            'children': children,
            'count': len(children)
        })
    except Exception as e:
        logger.error(f"Error fetching child predictions for {prediction_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@predictions_bp.route('/api/ai/predictions/<int:prediction_id>/review', methods=['POST'])
def review_prediction(prediction_id):
    """Approve, reject, or correct a prediction"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'Request body required'}), 400

        action = data.get('action')
        reviewer = data.get('reviewer') or request.headers.get('X-Auth-User', 'anonymous')
        notes = data.get('notes')

        if action not in ('approve', 'reject', 'correct', 'reclassify'):
            return jsonify({'success': False, 'error': 'action must be approve, reject, correct, or reclassify'}), 400

        corrections = data.get('corrections') if action == 'correct' else None

        updated = db.review_prediction(prediction_id, action, reviewer, notes, corrections)
        if not updated:
            return jsonify({'success': False, 'error': 'Prediction not found'}), 404

        # Cascade rejection to child predictions (plates, registrations)
        cascaded_count = 0
        if action == 'reject':
            cascaded_count = db.cascade_reject_children(prediction_id, reviewed_by=reviewer)
            if cascaded_count > 0:
                logger.info(f"Cascade-rejected {cascaded_count} child predictions for parent {prediction_id}")

        annotation_id = None
        if action in ('approve', 'correct'):
            annotation_id = db.approve_prediction_to_annotation(prediction_id)
            # Update model stats
            db.update_model_approval_stats(updated['model_name'], updated['model_version'])

            # Trigger guided interpolation for matching keyframe pairs
            if updated.get('model_name') == 'vehicle-world-v1':
                try:
                    _check_and_trigger_interpolation(updated)
                except Exception as interp_err:
                    logger.warning(f"Interpolation trigger check failed: {interp_err}")

        return jsonify({
            'success': True,
            'prediction_id': prediction_id,
            'review_status': updated['review_status'],
            'annotation_id': annotation_id,
            'cascaded_children': cascaded_count
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@predictions_bp.route('/api/ai/predictions/stats', methods=['GET'])
def get_prediction_stats():
    """Get prediction queue stats (counts by status)"""
    try:
        video_id = request.args.get('video_id', type=int)
        counts = db.get_prediction_counts(video_id)
        return jsonify({'success': True, 'counts': counts})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@predictions_bp.route('/api/ai/predictions/review-history', methods=['GET'])
def get_review_history():
    """Get reviewed predictions for history view"""
    try:
        status_filter = request.args.get('status')  # approved, rejected, classified, or None for all
        reviewer = request.args.get('reviewer')
        scenario = request.args.get('scenario')
        classification = request.args.get('classification')
        actual_class = request.args.get('actual_class')
        video_id = request.args.get('video_id', type=int)
        limit = request.args.get('limit', 50, type=int)
        offset = request.args.get('offset', 0, type=int)

        predictions, total_count = db.get_review_history_v2(
            status_filter, reviewer, scenario, classification, video_id, limit, offset,
            actual_class=actual_class
        )

        for p in predictions:
            for key in ['confidence', 'match_similarity']:
                if key in p and p[key] is not None:
                    p[key] = float(p[key])

        return jsonify({
            'success': True,
            'predictions': predictions,
            'count': len(predictions),
            'total_count': total_count
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@predictions_bp.route('/api/ai/predictions/classification-values', methods=['GET'])
def get_classification_values():
    """Get distinct classification values for filter dropdowns"""
    try:
        values = db.get_classification_filter_values()
        return jsonify({'success': True, **values})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ---- Reclassification Classes ----

@predictions_bp.route('/api/ai/reclassification-classes', methods=['GET'])
def get_reclassification_classes():
    """Get all reclassification classes, optionally with camera-specific top classes"""
    try:
        camera = request.args.get('camera')
        classes = db.get_reclassification_classes()
        result = {'success': True, 'classes': classes}
        if camera:
            result['camera_top'] = db.get_camera_top_classes(camera)
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@predictions_bp.route('/api/ai/reclassification-classes', methods=['POST'])
def add_reclassification_class():
    """Add a new custom reclassification class"""
    try:
        data = request.get_json()
        if not data or not data.get('class_name'):
            return jsonify({'success': False, 'error': 'class_name required'}), 400
        result = db.add_reclassification_class(data['class_name'])
        if result:
            return jsonify({'success': True, 'class': result})
        return jsonify({'success': True, 'message': 'Class already exists'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ---- Review Queue ----

@predictions_bp.route('/api/ai/predictions/review-queue', methods=['GET'])
def get_review_queue():
    """Get pending predictions for mobile review queue"""
    try:
        video_id = request.args.get('video_id', type=int)
        model_name = request.args.get('model_name')
        min_confidence = request.args.get('min_confidence', 0.10, type=float)
        max_confidence = request.args.get('max_confidence', type=float)
        limit = request.args.get('limit', 50, type=int)
        offset = request.args.get('offset', 0, type=int)
        grouped = request.args.get('grouped', '').lower() in ('1', 'true', 'yes')
        scenario = request.args.get('scenario')
        review_status = request.args.get('status', 'pending')

        if grouped:
            predictions = db.get_grouped_review_queue(video_id, min_confidence, max_confidence, limit, offset, scenario=scenario, review_status=review_status)
        else:
            predictions = db.get_review_queue(video_id, model_name, min_confidence, max_confidence, limit, offset, scenario=scenario, review_status=review_status)

        # Serialize for JSON (handle datetime, Decimal, etc.)
        for p in predictions:
            if p.get('predicted_tags') and hasattr(p['predicted_tags'], 'items'):
                pass  # already a dict from RealDictCursor
            for key in ['confidence', 'avg_confidence', 'min_confidence', 'group_min_confidence']:
                if key in p and p[key] is not None:
                    p[key] = float(p[key])

        return jsonify({'success': True, 'predictions': predictions, 'count': len(predictions)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@predictions_bp.route('/api/ai/predictions/cameras', methods=['GET'])
def get_camera_list():
    """Get cameras grouped by location for filter dropdown."""
    try:
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT cl.camera_id, cl.camera_name, cl.location_name,
                       COUNT(DISTINCT p.id) as prediction_count
                FROM camera_locations cl
                JOIN videos v ON v.camera_id = cl.camera_id
                JOIN ai_predictions p ON p.video_id = v.id
                WHERE p.review_status IN ('pending', 'processing')
                GROUP BY cl.camera_id, cl.camera_name, cl.location_name
                HAVING COUNT(DISTINCT p.id) > 0
                ORDER BY cl.location_name, cl.camera_name
            ''')
            rows = cursor.fetchall()
        cameras = [dict(r) for r in rows]
        return jsonify({'success': True, 'cameras': cameras})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@predictions_bp.route('/api/ai/predictions/review-queue/summary', methods=['GET'])
def get_review_queue_summary():
    """Get summary of pending predictions by video for queue entry screen"""
    try:
        grouped = request.args.get('grouped', '').lower() in ('1', 'true', 'yes')
        scenario = request.args.get('scenario')
        review_status = request.args.get('status', 'pending')
        min_confidence = request.args.get('min_confidence', type=float)
        max_confidence = request.args.get('max_confidence', type=float)
        camera_id = request.args.get('camera_id')
        if grouped:
            summary = db.get_grouped_review_queue_summary(scenario=scenario, review_status=review_status, min_confidence=min_confidence, max_confidence=max_confidence, camera_id=camera_id)
        else:
            summary = db.get_review_queue_summary(scenario=scenario, review_status=review_status, min_confidence=min_confidence, max_confidence=max_confidence, camera_id=camera_id)
        for s in summary:
            for key in ['avg_confidence', 'min_confidence']:
                if key in s and s[key] is not None:
                    s[key] = float(s[key])
        total_pending = sum(s['pending_count'] for s in summary)
        # Include classification queue count
        classify_summary = db.get_classification_queue_summary()
        total_needing_classification = sum(s['pending_classification'] for s in classify_summary)
        # Include total predictions count for grouped mode
        total_predictions = sum(s.get('total_predictions', s['pending_count']) for s in summary) if grouped else total_pending
        return jsonify({
            'success': True,
            'videos': summary,
            'total_pending': total_pending,
            'total_predictions': total_predictions,
            'video_count': len(summary),
            'total_needing_classification': total_needing_classification
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@predictions_bp.route('/api/ai/predictions/review-queue/filter-counts', methods=['GET'])
def get_review_filter_counts():
    """Get counts for each filter chip in the review queue"""
    try:
        min_confidence = request.args.get('min_confidence', type=float)
        max_confidence = request.args.get('max_confidence', type=float)
        camera_id = request.args.get('camera_id')
        counts = db.get_review_filter_counts(
            min_confidence=min_confidence,
            max_confidence=max_confidence,
            camera_id=camera_id
        )
        # Classify count (includes needs_reclassification)
        classify_summary = db.get_classification_queue_summary()
        classify_count = sum(s['pending_classification'] for s in classify_summary)
        counts['classify'] = classify_count + (counts.get('needs_reclassification') or 0)
        counts.pop('needs_reclassification', None)
        # Get conflict count
        try:
            conflicts = db.get_track_conflicts()
            counts['conflicts'] = len(conflicts) if conflicts else 0
        except Exception:
            counts['conflicts'] = 0
        # Compute 'other' as predictions minus known scenario counts
        known_scenario_sum = (
            (counts.get('vehicles') or 0) +
            (counts.get('people') or 0) +
            (counts.get('plates') or 0) +
            (counts.get('boat_reg') or 0)
        )
        counts['other'] = max(0, (counts.get('predictions') or 0) - known_scenario_sum)
        # Compute unified total across all types (no double counting)
        counts['total'] = (
            (counts.get('vehicles') or 0) +
            (counts.get('people') or 0) +
            (counts.get('plates') or 0) +
            (counts.get('boat_reg') or 0) +
            (counts.get('other') or 0) +
            (counts.get('classify') or 0) +
            (counts.get('cross_camera') or 0) +
            (counts.get('clusters') or 0) +
            (counts.get('conflicts') or 0)
        )
        return jsonify({'success': True, 'counts': counts})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ---- Batch Review ----

@predictions_bp.route('/api/ai/predictions/batch-review', methods=['POST'])
def batch_review_predictions():
    """Batch review multiple predictions at once (for mobile queue)"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'Request body required'}), 400
        reviews = data.get('reviews', [])
        reviewer = data.get('reviewer') or request.headers.get('X-Auth-User', 'studio_user')
        if not reviews:
            return jsonify({'success': False, 'error': 'reviews array required'}), 400
        if len(reviews) > 100:
            return jsonify({'success': False, 'error': 'Maximum 100 reviews per batch'}), 400
        results = db.batch_review_predictions(reviews, reviewer)

        # Cascade rejection to children for any rejected predictions in the batch
        total_cascaded = 0
        for review in reviews:
            if review.get('action') == 'reject':
                prediction_id = review.get('prediction_id')
                if prediction_id:
                    cascaded = db.cascade_reject_children(prediction_id, reviewed_by=reviewer)
                    if cascaded > 0:
                        total_cascaded += cascaded
                        logger.info(f"Cascade-rejected {cascaded} child predictions for parent {prediction_id}")

        if total_cascaded > 0:
            results['cascaded_children'] = total_cascaded

        return jsonify({'success': True, **results})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ---- Classification Queue ----

@predictions_bp.route('/api/ai/predictions/classification-queue', methods=['GET'])
def get_classification_queue():
    """Get approved vehicle detections needing classification"""
    try:
        video_id = request.args.get('video_id', type=int)
        limit = request.args.get('limit', 50, type=int)
        offset = request.args.get('offset', 0, type=int)
        include_pending = request.args.get('include_pending', '').lower() in ('true', '1', 'yes')
        grouped = request.args.get('grouped', '').lower() in ('1', 'true', 'yes')

        if grouped:
            predictions = db.get_grouped_classification_queue(video_id, limit, offset, include_pending=include_pending)
        else:
            predictions = db.get_classification_queue(video_id, limit, offset, include_pending=include_pending)

        for p in predictions:
            for key in ['confidence', 'avg_confidence']:
                if key in p and p[key] is not None:
                    p[key] = float(p[key])

        return jsonify({'success': True, 'predictions': predictions, 'count': len(predictions)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@predictions_bp.route('/api/ai/predictions/classification-queue/summary', methods=['GET'])
def get_classification_queue_summary():
    """Get summary of approved vehicle detections needing classification by video"""
    try:
        include_pending = request.args.get('include_pending', '').lower() in ('true', '1', 'yes')
        summary = db.get_classification_queue_summary(include_pending=include_pending)
        for s in summary:
            if 'avg_confidence' in s and s['avg_confidence'] is not None:
                s['avg_confidence'] = float(s['avg_confidence'])
        total = sum(s['pending_classification'] for s in summary)
        return jsonify({
            'success': True,
            'videos': summary,
            'total_needing_classification': total,
            'video_count': len(summary)
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@predictions_bp.route('/api/ai/predictions/batch-classify', methods=['POST'])
def batch_classify_vehicles():
    """Batch classify vehicle subtypes"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'Request body required'}), 400
        classifications = data.get('classifications', [])
        classifier = data.get('classifier', 'studio_user')
        if not classifications:
            return jsonify({'success': False, 'error': 'classifications array required'}), 400
        if len(classifications) > 100:
            return jsonify({'success': False, 'error': 'Maximum 100 classifications per batch'}), 400
        results = db.batch_classify_vehicles(classifications, classifier)
        return jsonify({'success': True, **results})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ---- Group Operations ----

@predictions_bp.route('/api/ai/predictions/group-review', methods=['POST'])
def group_review():
    """Review all members of a prediction group at once"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'Request body required'}), 400
        group_id = data.get('group_id')
        action = data.get('action')
        reviewer = data.get('reviewer') or request.headers.get('X-Auth-User', 'mobile_reviewer')
        notes = data.get('notes')
        if not group_id or action not in ('approve', 'reject'):
            return jsonify({'success': False, 'error': 'group_id and valid action required'}), 400
        results = db.batch_review_group(group_id, action, reviewer, notes)
        return jsonify({'success': True, **results})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@predictions_bp.route('/api/ai/predictions/group-classify', methods=['POST'])
def group_classify():
    """Classify all members of a prediction group with a vehicle subtype"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'Request body required'}), 400
        group_id = data.get('group_id')
        vehicle_subtype = data.get('vehicle_subtype')
        classifier = data.get('classifier', 'mobile_reviewer')
        if not group_id or not vehicle_subtype:
            return jsonify({'success': False, 'error': 'group_id and vehicle_subtype required'}), 400
        results = db.batch_classify_group(group_id, vehicle_subtype, classifier)
        return jsonify({'success': True, **results})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@predictions_bp.route('/api/ai/predictions/group-undo', methods=['POST'])
def group_undo():
    """Undo review for all members of a prediction group"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'Request body required'}), 400
        group_id = data.get('group_id')
        if not group_id:
            return jsonify({'success': False, 'error': 'group_id required'}), 400
        results = db.undo_group_review(group_id)
        return jsonify({'success': True, **results})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@predictions_bp.route('/api/ai/predictions/groups/<int:group_id>/members', methods=['GET'])
def get_group_members(group_id):
    """Get all predictions in a group"""
    try:
        members = db.get_group_members(group_id)
        for m in members:
            for key in ['confidence']:
                if key in m and m[key] is not None:
                    m[key] = float(m[key])
        return jsonify({'success': True, 'predictions': members, 'count': len(members)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@predictions_bp.route('/api/ai/predictions/regroup', methods=['POST'])
def regroup_predictions():
    """Re-group predictions (clear existing groups and recompute)"""
    try:
        data = request.get_json() or {}
        camera_id = data.get('camera_id')
        from prediction_grouper import PredictionGrouper
        grouper = PredictionGrouper()
        results = grouper.regroup_all(camera_id)
        return jsonify({'success': True, **results})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@predictions_bp.route('/api/ai/predictions/<int:prediction_id>/undo', methods=['POST'])
def undo_prediction_review(prediction_id):
    """Undo a prediction review (revert to pending)"""
    try:
        success = db.unreview_prediction(prediction_id)
        if not success:
            return jsonify({'success': False, 'error': 'Prediction not found or already pending'}), 404
        return jsonify({'success': True, 'prediction_id': prediction_id, 'review_status': 'pending'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ---- All Pending (Global) ----

@predictions_bp.route('/api/ai/predictions/all-pending', methods=['GET'])
def get_all_pending_predictions():
    """Get all pending predictions across all videos for global review page."""
    model_filter = request.args.get('model')
    scenario_filter = request.args.get('scenario')
    min_confidence = request.args.get('min_confidence', type=float)
    max_confidence = request.args.get('max_confidence', type=float)
    limit = min(request.args.get('limit', 100, type=int), 500)
    offset = request.args.get('offset', 0, type=int)

    try:
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)

            query = '''
                SELECT p.*, v.title as video_title, v.thumbnail_path
                FROM ai_predictions p
                JOIN videos v ON p.video_id = v.id
                WHERE p.review_status IN ('pending', 'auto_approved')
            '''
            params = []

            if model_filter:
                query += ' AND p.model_name = %s'
                params.append(model_filter)
            if scenario_filter:
                query += ' AND p.scenario = %s'
                params.append(scenario_filter)
            if min_confidence is not None:
                query += ' AND p.confidence >= %s'
                params.append(min_confidence)
            if max_confidence is not None:
                query += ' AND p.confidence <= %s'
                params.append(max_confidence)

            # Get total count
            count_query = query.replace('SELECT p.*, v.title as video_title, v.thumbnail_path', 'SELECT COUNT(*) as total')
            cursor.execute(count_query, params)
            total = cursor.fetchone()['total']

            query += ' ORDER BY p.confidence DESC, p.created_at DESC LIMIT %s OFFSET %s'
            params.extend([limit, offset])

            cursor.execute(query, params)
            predictions = [dict(row) for row in cursor.fetchall()]

            # Convert datetime objects to strings
            for pred in predictions:
                for key in ('created_at', 'reviewed_at', 'routed_at'):
                    if pred.get(key):
                        pred[key] = pred[key].isoformat()
                # Parse JSONB fields that come back as strings
                for key in ('predicted_tags', 'bbox', 'metadata'):
                    if isinstance(pred.get(key), str):
                        try:
                            pred[key] = json.loads(pred[key])
                        except (json.JSONDecodeError, TypeError):
                            pass

            # Also get thumbnail paths as URL-friendly
            for pred in predictions:
                if pred.get('thumbnail_path'):
                    thumb_name = os.path.basename(pred['thumbnail_path'])
                    pred['thumbnail_url'] = f'/thumbnails/{thumb_name}'
                else:
                    pred['thumbnail_url'] = None

            return jsonify({
                'success': True,
                'predictions': predictions,
                'total': total,
                'limit': limit,
                'offset': offset
            })
    except Exception as e:
        logger.error(f'Failed to get all pending predictions: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


# ---- Vehicle Metrics ----

@predictions_bp.route('/api/ai/vehicle-metrics')
def get_vehicle_metrics():
    """Get vehicle detection metrics: per-class counts, correction rates, confusion matrix."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)

            # 1. Per-class counts by review status
            # Use corrected class when available (vehicle_subtype from classify,
            # actual_class from reject-reclassify), fall back to model prediction
            cursor.execute('''
                SELECT
                    COALESCE(
                        corrected_tags->>'vehicle_subtype',
                        corrected_tags->>'actual_class',
                        predicted_tags->>'vehicle_type',
                        predicted_tags->>'class'
                    ) as vehicle_class,
                    review_status,
                    COUNT(*) as count
                FROM ai_predictions
                WHERE scenario = 'vehicle_detection'
                   OR predicted_tags->>'vehicle_type' IS NOT NULL
                GROUP BY vehicle_class, review_status
                ORDER BY vehicle_class, review_status
            ''')
            class_status_rows = cursor.fetchall()

            # Build per-class stats
            class_stats = {}
            for row in class_status_rows:
                cls = row['vehicle_class'] or 'unknown'
                if cls not in class_stats:
                    class_stats[cls] = {'pending': 0, 'approved': 0, 'rejected': 0, 'corrected': 0, 'auto_approved': 0, 'total': 0}
                status = row['review_status'] or 'pending'
                class_stats[cls][status] = class_stats[cls].get(status, 0) + row['count']
                class_stats[cls]['total'] += row['count']

            # 2. Correction counts (how many were reclassified/classified)
            cursor.execute('''
                SELECT
                    COALESCE(
                        corrected_tags->>'vehicle_subtype',
                        corrected_tags->>'actual_class',
                        predicted_tags->>'vehicle_type',
                        predicted_tags->>'class'
                    ) as vehicle_class,
                    COUNT(*) as corrected_count
                FROM ai_predictions
                WHERE (scenario = 'vehicle_detection'
                       OR predicted_tags->>'vehicle_type' IS NOT NULL)
                  AND (corrected_tags->>'vehicle_subtype' IS NOT NULL
                       OR corrected_tags->>'actual_class' IS NOT NULL)
                GROUP BY vehicle_class
            ''')
            for row in cursor.fetchall():
                cls = row['vehicle_class'] or 'unknown'
                if cls in class_stats:
                    class_stats[cls]['corrected'] = row['corrected_count']

            # 3. Confusion matrix: model prediction -> human correction
            cursor.execute('''
                SELECT
                    COALESCE(predicted_tags->>'vehicle_type', predicted_tags->>'class') as original_class,
                    COALESCE(corrected_tags->>'vehicle_subtype', corrected_tags->>'actual_class') as corrected_class,
                    COUNT(*) as count
                FROM ai_predictions
                WHERE (scenario = 'vehicle_detection'
                       OR predicted_tags->>'vehicle_type' IS NOT NULL)
                  AND (corrected_tags->>'vehicle_subtype' IS NOT NULL
                       OR corrected_tags->>'actual_class' IS NOT NULL)
                GROUP BY original_class, corrected_class
                ORDER BY count DESC
            ''')
            confusion_matrix = [dict(row) for row in cursor.fetchall()]

            # 4. Weekly detection trends (last 8 weeks)
            cursor.execute('''
                SELECT
                    date_trunc('week', created_at)::date as week,
                    COUNT(*) as detections,
                    COUNT(*) FILTER (WHERE review_status = 'approved') as approved,
                    COUNT(*) FILTER (WHERE review_status = 'rejected') as rejected,
                    COUNT(*) FILTER (WHERE corrected_tags->>'vehicle_subtype' IS NOT NULL
                                         OR corrected_tags->>'actual_class' IS NOT NULL) as corrected
                FROM ai_predictions
                WHERE (scenario = 'vehicle_detection' OR predicted_tags->>'vehicle_type' IS NOT NULL)
                  AND created_at >= NOW() - INTERVAL '8 weeks'
                GROUP BY week
                ORDER BY week
            ''')
            weekly_trends = []
            for row in cursor.fetchall():
                weekly_trends.append({
                    'week': row['week'].isoformat() if row['week'] else None,
                    'detections': row['detections'],
                    'approved': row['approved'],
                    'rejected': row['rejected'],
                    'corrected': row['corrected']
                })

            # 5. Fine-tuning readiness (200+ corrected samples per class)
            readiness = {}
            for cls, stats in class_stats.items():
                reviewed = stats.get('approved', 0) + stats.get('corrected', 0)
                readiness[cls] = {
                    'reviewed_count': reviewed,
                    'target': 200,
                    'ready': reviewed >= 200,
                    'progress_pct': min(100, round(reviewed / 200 * 100, 1))
                }

            # 6. Summary totals
            total_predictions = sum(s['total'] for s in class_stats.values())
            total_approved = sum(s.get('approved', 0) + s.get('auto_approved', 0) for s in class_stats.values())
            total_rejected = sum(s.get('rejected', 0) for s in class_stats.values())
            total_corrected = sum(s.get('corrected', 0) for s in class_stats.values())
            total_pending = sum(s.get('pending', 0) for s in class_stats.values())

            # 7. Today's review count
            cursor.execute('''
                SELECT COUNT(*) as count
                FROM ai_predictions
                WHERE (scenario = 'vehicle_detection' OR predicted_tags->>'vehicle_type' IS NOT NULL)
                  AND reviewed_at >= CURRENT_DATE
                  AND review_status IN ('approved', 'rejected')
            ''')
            reviewed_today = cursor.fetchone()['count']

            return jsonify({
                'success': True,
                'summary': {
                    'total': total_predictions,
                    'approved': total_approved,
                    'rejected': total_rejected,
                    'corrected': total_corrected,
                    'pending': total_pending,
                    'reviewed_today': reviewed_today,
                    'class_count': len(class_stats)
                },
                'class_stats': class_stats,
                'confusion_matrix': confusion_matrix,
                'weekly_trends': weekly_trends,
                'readiness': readiness
            })
    except Exception as e:
        logger.error(f'Failed to get vehicle metrics: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


# ---- Predictions by Class ----

@predictions_bp.route('/api/ai/predictions/by-class')
def get_predictions_by_class():
    """Get predictions for a specific vehicle class."""
    try:
        class_name = request.args.get('class')
        if not class_name:
            return jsonify({'success': False, 'error': 'class parameter required'}), 400

        status = request.args.get('status', 'all')
        limit = min(int(request.args.get('limit', 200)), 500)
        offset = int(request.args.get('offset', 0))

        predictions = db.get_predictions_by_class(class_name, status, limit, offset)
        total = db.get_predictions_count_by_class(class_name, status)

        results = []
        for p in predictions:
            results.append({
                'id': p['id'],
                'bbox_x': p['bbox_x'],
                'bbox_y': p['bbox_y'],
                'bbox_width': p['bbox_width'],
                'bbox_height': p['bbox_height'],
                'confidence': p['confidence'],
                'review_status': p['review_status'],
                'predicted_tags': p['predicted_tags'],
                'corrected_tags': p['corrected_tags'],
                'video_id': p['video_id'],
                'camera_id': p['camera_id'],
                'effective_class': p['effective_class'],
                'created_at': p['created_at'].isoformat() if p.get('created_at') else None,
                'reviewed_at': p['reviewed_at'].isoformat() if p.get('reviewed_at') else None,
            })

        return jsonify({
            'success': True,
            'predictions': results,
            'total': total,
            'limit': limit,
            'offset': offset
        })
    except Exception as e:
        logger.error(f'Failed to get predictions by class: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


# ---- Cropped Thumbnail ----

@predictions_bp.route('/thumbnails/crop/<int:prediction_id>')
def get_cropped_thumbnail(prediction_id):
    """Serve a cropped bbox thumbnail for a prediction."""
    from PIL import Image

    try:
        pred = db.get_prediction_for_crop(prediction_id)
        if not pred:
            return jsonify({'error': 'Prediction not found'}), 404

        thumbnail_path = pred.get('thumbnail_path')
        if not thumbnail_path:
            return jsonify({'error': 'No thumbnail available'}), 404

        # Resolve path relative to project base
        if not os.path.isabs(thumbnail_path):
            thumbnail_path = os.path.join(str(BASE_DIR), thumbnail_path)

        if not os.path.exists(thumbnail_path):
            return jsonify({'error': 'Thumbnail file not found'}), 404

        bbox_x = pred.get('bbox_x')
        bbox_y = pred.get('bbox_y')
        bbox_w = pred.get('bbox_width')
        bbox_h = pred.get('bbox_height')

        img = Image.open(thumbnail_path).convert('RGB')

        # If valid bbox, crop; otherwise serve full thumbnail
        if bbox_x is not None and bbox_w and bbox_h and bbox_w > 0 and bbox_h > 0:
            x1 = max(0, int(bbox_x))
            y1 = max(0, int(bbox_y))
            x2 = min(img.width, int(bbox_x + bbox_w))
            y2 = min(img.height, int(bbox_y + bbox_h))
            if x2 > x1 and y2 > y1:
                img = img.crop((x1, y1, x2, y2))

        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=85)
        buf.seek(0)

        response = send_file(buf, mimetype='image/jpeg')
        response.headers['Cache-Control'] = 'public, max-age=3600'
        return response
    except Exception as e:
        logger.error(f'Failed to crop thumbnail for prediction {prediction_id}: {e}')
        return jsonify({'error': 'Failed to generate crop'}), 500


@predictions_bp.route('/thumbnails/annotated/<int:prediction_id>')
def get_annotated_thumbnail(prediction_id):
    """Serve full frame with bounding box drawn for context."""
    from PIL import Image, ImageDraw

    try:
        pred = db.get_prediction_for_crop(prediction_id)
        if not pred:
            return jsonify({'error': 'Prediction not found'}), 404

        thumbnail_path = pred.get('thumbnail_path')
        if not thumbnail_path:
            return jsonify({'error': 'No thumbnail available'}), 404

        if not os.path.isabs(thumbnail_path):
            thumbnail_path = os.path.join(str(BASE_DIR), thumbnail_path)

        if not os.path.exists(thumbnail_path):
            return jsonify({'error': 'Thumbnail file not found'}), 404

        bbox_x = pred.get('bbox_x')
        bbox_y = pred.get('bbox_y')
        bbox_w = pred.get('bbox_width')
        bbox_h = pred.get('bbox_height')

        img = Image.open(thumbnail_path).convert('RGB')

        if bbox_x is not None and bbox_w and bbox_h and bbox_w > 0 and bbox_h > 0:
            draw = ImageDraw.Draw(img)
            x1 = max(0, int(bbox_x))
            y1 = max(0, int(bbox_y))
            x2 = min(img.width, int(bbox_x + bbox_w))
            y2 = min(img.height, int(bbox_y + bbox_h))
            draw.rectangle([x1, y1, x2, y2], outline='red', width=3)

        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=85)
        buf.seek(0)

        response = send_file(buf, mimetype='image/jpeg')
        response.headers['Cache-Control'] = 'public, max-age=3600'
        return response
    except Exception as e:
        logger.error(f'Failed to annotate thumbnail for prediction {prediction_id}: {e}')
        return jsonify({'error': 'Failed to generate annotated image'}), 500


# ---- Batch Update Class ----

@predictions_bp.route('/api/ai/predictions/batch-update-class', methods=['POST'])
def batch_update_prediction_class():
    """Batch reclassify or requeue predictions."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'JSON body required'}), 400

        prediction_ids = data.get('prediction_ids', [])
        if not prediction_ids or not isinstance(prediction_ids, list):
            return jsonify({'success': False, 'error': 'prediction_ids array required'}), 400
        if len(prediction_ids) > 200:
            return jsonify({'success': False, 'error': 'Maximum 200 IDs per request'}), 400

        requeue = data.get('requeue', False)
        vehicle_subtype = data.get('vehicle_subtype')

        if requeue:
            result = db.batch_requeue_predictions(prediction_ids)
            return jsonify({'success': True, 'action': 'requeue', **result})
        elif vehicle_subtype:
            classifier = request.headers.get('X-Auth-User', 'studio_user')
            result = db.batch_update_vehicle_class(prediction_ids, vehicle_subtype, classifier)
            # Increment usage count for the reclassification class
            try:
                db.increment_class_usage(vehicle_subtype)
            except Exception:
                pass
            return jsonify({'success': True, 'action': 'reclassify', **result})
        else:
            return jsonify({'success': False, 'error': 'Either vehicle_subtype or requeue=true required'}), 400
    except Exception as e:
        logger.error(f'Failed to batch update predictions: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


# ---- Static Cluster Batch Review ----

@predictions_bp.route('/batch-cluster-review')
def batch_cluster_review_page():
    return redirect('/review?filter=clusters')


@predictions_bp.route('/api/ai/predictions/static-clusters', methods=['GET'])
def get_static_clusters():
    """Get spatial clusters of repeated detections for batch review."""
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT
                    p.corrected_tags->>'static_cluster' as cluster_key,
                    v.camera_id,
                    COUNT(*) as count,
                    array_agg(DISTINCT p.predicted_tags->>'class') as yolo_classes,
                    ROUND(AVG(p.confidence)::numeric, 3) as avg_conf,
                    MIN(p.id) as sample_id,
                    json_agg(json_build_object(
                        'id', p.id, 'confidence', p.confidence,
                        'class', p.predicted_tags->>'class',
                        'vlm_class', p.corrected_tags->>'vlm_suggested_class',
                        'vlm_reasoning', p.corrected_tags->>'vlm_reasoning',
                        'previous_status', p.corrected_tags->>'previous_review_status'
                    ) ORDER BY p.id) as members
                FROM ai_predictions p
                JOIN videos v ON p.video_id = v.id
                WHERE p.corrected_tags->>'batch_reviewable' = 'true'
                AND p.review_status = 'pending'
                AND p.predicted_tags->>'class' = 'unknown vehicle'
                GROUP BY cluster_key, v.camera_id
                HAVING COUNT(*) >= 2
                ORDER BY COUNT(*) DESC
            """)
            clusters = []
            for row in cur.fetchall():
                clusters.append({
                    'cluster_key': row[0],
                    'camera_id': row[1],
                    'count': row[2],
                    'yolo_classes': row[3],
                    'avg_confidence': float(row[4]) if row[4] else 0,
                    'sample_id': row[5],
                    'members': row[6],
                })
            cur.close()
        return jsonify({'success': True, 'clusters': clusters, 'total': sum(c['count'] for c in clusters)})
    except Exception as e:
        logger.error(f'Failed to get static clusters: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


@predictions_bp.route('/api/ai/predictions/batch-cluster-review', methods=['POST'])
def batch_cluster_review():
    """Batch approve or reject an entire spatial cluster."""
    try:
        data = request.get_json()
        cluster_key = data.get('cluster_key')
        action = data.get('action')  # 'approve', 'reject', or 'reclassify'
        actual_class = data.get('actual_class', '').strip().lower()

        if not cluster_key or action not in ('approve', 'reject', 'reclassify'):
            return jsonify({'success': False, 'error': 'cluster_key and action (approve/reject/reclassify) required'}), 400

        if action == 'reclassify' and not actual_class:
            return jsonify({'success': False, 'error': 'actual_class required for reclassify'}), 400

        review_status = 'approved' if action == 'approve' else 'rejected'

        with get_connection() as conn:
            cur = conn.cursor()
            if action == 'reclassify':
                cur.execute("""
                    UPDATE ai_predictions
                    SET review_status = 'rejected',
                        reviewed_by = 'batch_cluster_review',
                        reviewed_at = NOW(),
                        corrected_tags = COALESCE(corrected_tags, '{}'::jsonb) ||
                            jsonb_build_object(
                                'batch_reviewed', true,
                                'batch_action', 'reclassify',
                                'actual_class', %s,
                                'needs_negative_review', true
                            )
                    WHERE corrected_tags->>'static_cluster' = %s
                    AND review_status = 'pending'
                """, (actual_class, cluster_key))
            else:
                cur.execute("""
                    UPDATE ai_predictions
                    SET review_status = %s,
                        reviewed_by = 'batch_cluster_review',
                        reviewed_at = NOW(),
                        corrected_tags = COALESCE(corrected_tags, '{}'::jsonb) ||
                            jsonb_build_object('batch_reviewed', true, 'batch_action', %s)
                    WHERE corrected_tags->>'static_cluster' = %s
                    AND review_status = 'pending'
                """, (review_status, action, cluster_key))
            updated = cur.rowcount
            conn.commit()
            cur.close()

        return jsonify({'success': True, 'updated': updated, 'action': action, 'cluster_key': cluster_key})
    except Exception as e:
        logger.error(f'Failed to batch review cluster: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


@predictions_bp.route('/api/ai/predictions/uncluster', methods=['POST'])
def uncluster():
    """Remove cluster tags from predictions, sending them back to individual review."""
    try:
        data = request.get_json()
        cluster_key = data.get('cluster_key')
        if not cluster_key:
            return jsonify({'success': False, 'error': 'cluster_key required'}), 400

        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE ai_predictions
                SET corrected_tags = corrected_tags - 'static_cluster' - 'batch_reviewable' - 'cluster_size' - 'suggested_class'
                WHERE corrected_tags->>'static_cluster' = %s
                AND review_status = 'pending'
            """, (cluster_key,))
            updated = cur.rowcount
            conn.commit()
            cur.close()

        return jsonify({'success': True, 'updated': updated, 'cluster_key': cluster_key})
    except Exception as e:
        logger.error(f'Failed to uncluster: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


# ---- VLM Endpoints ----

@predictions_bp.route('/api/ai/predictions/vlm-stats', methods=['GET'])
def get_vlm_stats():
    """Get VLM review statistics: acceptance rate, breakdown by class."""
    try:
        stats = db.get_vlm_stats()
        return jsonify({'success': True, **stats})
    except Exception as e:
        logger.error(f'Failed to get VLM stats: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


@predictions_bp.route('/api/ai/predictions/<int:prediction_id>/vlm-review', methods=['POST'])
def trigger_vlm_review(prediction_id):
    """Manually trigger VLM review on a specific prediction."""
    try:
        import vlm_reviewer

        pred = db.get_prediction_by_id(prediction_id)
        if not pred:
            return jsonify({'success': False, 'error': 'Prediction not found'}), 404

        video = db.get_video(pred['video_id'])
        if not video:
            return jsonify({'success': False, 'error': 'Video not found'}), 404

        video_path = DOWNLOAD_DIR / video['filename']
        if not video_path.exists():
            return jsonify({'success': False, 'error': 'Video file not found'}), 404

        tags = pred.get('predicted_tags') or {}
        if isinstance(tags, str):
            tags = json.loads(tags)
        predicted_class = tags.get('class', tags.get('vehicle_type', 'vehicle'))

        bbox = {
            'x': pred.get('bbox_x') or 0,
            'y': pred.get('bbox_y') or 0,
            'width': pred.get('bbox_width') or 0,
            'height': pred.get('bbox_height') or 0
        }

        result = vlm_reviewer.classify_detection(
            str(video_path),
            pred.get('timestamp') or 0,
            bbox,
            predicted_class,
            float(pred.get('confidence') or 0),
            pred.get('scenario') or 'vehicle_detection'
        )

        if not result:
            return jsonify({'success': False, 'error': 'VLM analysis returned no result (model may be unavailable)'}), 503

        # If VLM says not a vehicle with sufficient confidence, update corrected_tags
        if not result['is_vehicle'] and result['confidence'] >= vlm_reviewer.VLM_CONFIDENCE_THRESHOLD:
            with get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE ai_predictions
                    SET corrected_tags = COALESCE(corrected_tags, '{}'::jsonb) || %s::jsonb
                    WHERE id = %s
                """, (
                    extras.Json({
                        'actual_class': result['suggested_class'],
                        'vlm_confidence': result['confidence'],
                        'vlm_reasoning': result['reasoning'],
                        'vlm_model': vlm_reviewer.VLM_MODEL,
                        'vlm_suggested_class': result['suggested_class'],
                        'needs_negative_review': True
                    }),
                    prediction_id
                ))
                conn.commit()

        return jsonify({
            'success': True,
            'prediction_id': prediction_id,
            'vlm_result': result,
            'tags_updated': not result['is_vehicle'] and result['confidence'] >= vlm_reviewer.VLM_CONFIDENCE_THRESHOLD
        })
    except Exception as e:
        logger.error(f'VLM review failed for prediction {prediction_id}: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


@predictions_bp.route('/api/ai/predictions/vlm-config', methods=['GET'])
def get_vlm_config():
    """Get current VLM configuration and status."""
    try:
        import vlm_reviewer
        config = vlm_reviewer.get_config()
        status = vlm_reviewer.check_ollama_status()
        return jsonify({'success': True, 'config': config, 'status': status})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@predictions_bp.route('/api/ai/predictions/vlm-config', methods=['PUT'])
def update_vlm_config():
    """Update VLM configuration (enable/disable, model, thresholds)."""
    try:
        import vlm_reviewer
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'JSON body required'}), 400
        updated = vlm_reviewer.update_config(data)
        logger.info(f"VLM config updated: {updated}")
        return jsonify({'success': True, 'config': updated})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ---- Helper Functions ----

def _check_and_trigger_interpolation(approved_pred):
    """Find matching approved keyframes and trigger interpolation."""
    import json as _json

    tags = approved_pred.get('corrected_tags') or approved_pred.get('predicted_tags', {})
    if isinstance(tags, str):
        tags = _json.loads(tags)

    class_name = tags.get('class')
    if not class_name:
        return

    # Skip interpolation-generated predictions
    if tags.get('source') == 'interpolation':
        return

    # Find other approved predictions for same video + class
    other_approved = db.get_approved_predictions_for_class(
        video_id=approved_pred['video_id'],
        class_name=class_name,
        model_name='vehicle-world-v1'
    )

    for other in other_approved:
        if other['id'] == approved_pred['id']:
            continue

        # Skip interpolation-generated predictions
        other_tags = other.get('corrected_tags') or other.get('predicted_tags', {})
        if isinstance(other_tags, str):
            other_tags = _json.loads(other_tags)
        if other_tags.get('source') == 'interpolation':
            continue

        # Check time gap is reasonable (2s to 120s)
        gap = abs(float(other['timestamp'] or 0) - float(approved_pred['timestamp'] or 0))
        if gap < 2.0 or gap > 120.0:
            continue

        # Check no track already exists for this pair
        if db.interpolation_track_exists(approved_pred['id'], other['id']):
            continue

        # Determine start/end by timestamp order
        if float(approved_pred['timestamp'] or 0) < float(other['timestamp'] or 0):
            start_id, end_id = approved_pred['id'], other['id']
        else:
            start_id, end_id = other['id'], approved_pred['id']

        # Trigger in background
        logger.info(f"Triggering guided interpolation: video {approved_pred['video_id']}, "
                     f"class '{class_name}', preds {start_id}->{end_id}")
        from interpolation_runner import run_guided_interpolation
        threading.Thread(
            target=run_guided_interpolation,
            args=(approved_pred['video_id'], start_id, end_id),
            daemon=True,
            name=f"interp-{start_id}-{end_id}"
        ).start()


@predictions_bp.route('/api/ai/feedback', methods=['POST'])
def submit_ai_feedback():
    """Save user feedback about AI predictions to a JSONL file."""
    import json, datetime
    try:
        data = request.get_json()
        if not data or not data.get('feedback'):
            return jsonify({'success': False, 'error': 'feedback text required'}), 400
        
        feedback_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'feedback')
        os.makedirs(feedback_dir, exist_ok=True)
        
        entry = {
            'timestamp': datetime.datetime.utcnow().isoformat() + 'Z',
            'feedback': data['feedback'],
            'prediction_id': data.get('prediction_id'),
            'video_id': data.get('video_id'),
            'scenario': data.get('scenario'),
            'predicted_class': data.get('predicted_class'),
            'confidence': data.get('confidence'),
            'review_mode': data.get('review_mode'),
            'active_filter': data.get('active_filter'),
            'url': data.get('url'),
        }

        # Store video timestamps and clip references for cross-camera feedback
        if data.get('video_timestamps'):
            entry['video_timestamps'] = data['video_timestamps']
        if data.get('clip_data'):
            entry['clip_data'] = data['clip_data']
        if data.get('sync_offsets'):
            entry['sync_offsets'] = data['sync_offsets']
        if data.get('cross_camera_link_id'):
            entry['cross_camera_link_id'] = data['cross_camera_link_id']
        if data.get('match_confidence') is not None:
            entry['match_confidence'] = data['match_confidence']
        if data.get('match_factors'):
            entry['match_factors'] = data['match_factors']

        filepath = os.path.join(feedback_dir, 'ai_feedback.jsonl')
        with open(filepath, 'a') as f:
            f.write(json.dumps(entry) + '\n')
        
        logger.info(f"AI feedback saved: pred={entry.get('prediction_id')} feedback={entry['feedback'][:100]}")
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Error saving AI feedback: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@predictions_bp.route('/api/ai/feedback/bbox-correction', methods=['POST'])
def submit_bbox_correction():
    """Save a user-drawn bbox correction for retraining."""
    import json, datetime
    try:
        data = request.get_json()
        if not data or not data.get('corrected_bbox'):
            return jsonify({'success': False, 'error': 'corrected_bbox required'}), 400

        feedback_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'feedback')
        os.makedirs(feedback_dir, exist_ok=True)

        entry = {
            'timestamp': data.get('timestamp', datetime.datetime.utcnow().isoformat() + 'Z'),
            'type': 'bbox_correction',
            'camera_id': data.get('camera_id'),
            'video_track_id': data.get('video_track_id'),
            'clip_url': data.get('clip_url'),
            'video_time': data.get('video_time'),
            'video_duration': data.get('video_duration'),
            'video_width': data.get('video_width'),
            'video_height': data.get('video_height'),
            'original_bbox': data.get('original_bbox'),
            'corrected_bbox': data.get('corrected_bbox'),
            'class_name': data.get('class_name'),
            'cross_camera_link_id': data.get('cross_camera_link_id'),
        }

        filepath = os.path.join(feedback_dir, 'bbox_corrections.jsonl')
        with open(filepath, 'a') as f:
            f.write(json.dumps(entry) + '\n')

        logger.info(f"BBox correction saved: camera={entry.get('camera_id')} track={entry.get('video_track_id')} t={entry.get('video_time')}")
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Error saving bbox correction: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@predictions_bp.route('/api/ai/feedback/calibration')
def get_camera_calibration():
    """Get per-camera projection calibration data derived from bbox corrections."""
    try:
        import calibration
        cal_data = calibration.load_calibration()
        return jsonify({'success': True, 'calibration': cal_data})
    except Exception as e:
        logger.error(f"Failed to get camera calibration: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@predictions_bp.route('/api/ai/feedback/calibration/rebuild', methods=['POST'])
def rebuild_calibration():
    """Rebuild calibration from corrections data."""
    try:
        import calibration
        cal_data = calibration.rebuild_calibration()
        return jsonify({
            'success': True,
            'calibration': cal_data,
            'camera_count': len(cal_data)
        })
    except Exception as e:
        logger.error(f"Failed to rebuild calibration: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
