from flask import Blueprint, request, jsonify, render_template, send_from_directory, g
from db_connection import get_cursor, get_connection
import services
from services import db, DOWNLOAD_DIR, THUMBNAIL_DIR
import os
import json
import logging
import time
import threading

tracks_bp = Blueprint('tracks', __name__)
logger = logging.getLogger(__name__)


# ── Helper Functions ──────────────────────────────────────────────────────

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


# ── Camera Object Tracks ──────────────────────────────────────────────────

@tracks_bp.route('/api/ai/tracks/build', methods=['POST'])
def build_tracks():
    """Build camera object tracks from all predictions."""
    camera_id = request.json.get('camera_id') if request.json else None

    from track_builder import TrackBuilder
    builder = TrackBuilder()
    result = builder.build_tracks(camera_id=camera_id)
    return jsonify({'success': True, **result})

@tracks_bp.route('/api/ai/tracks/propagate', methods=['POST'])
def propagate_track_decisions():
    """Propagate anchor decisions to pending track members."""
    data = request.json or {}
    camera_id = data.get('camera_id')
    dry_run = data.get('dry_run', False)

    from track_builder import TrackBuilder
    builder = TrackBuilder()
    result = builder.propagate_decisions(camera_id=camera_id, dry_run=dry_run)
    return jsonify({'success': True, **result})

@tracks_bp.route('/api/ai/tracks/summary', methods=['GET'])
def get_track_summary():
    """Get summary statistics for camera object tracks."""
    camera_id = request.args.get('camera_id')

    from track_builder import TrackBuilder
    builder = TrackBuilder()
    summary = builder.get_track_summary(camera_id=camera_id)
    return jsonify({'success': True, **summary})

@tracks_bp.route('/api/ai/tracks/conflicts', methods=['GET'])
def get_track_conflicts():
    """Get tracks with conflicting decisions needing manual resolution."""
    camera_id = request.args.get('camera_id')
    conflicts = db.get_track_conflicts(camera_id=camera_id)

    # Serialize
    for c in conflicts:
        for key in ('anchor_classification', 'predicted_tags', 'corrected_tags'):
            if key in c and c[key] is not None and not isinstance(c[key], (dict, list)):
                import json
                try:
                    c[key] = json.loads(c[key])
                except (json.JSONDecodeError, TypeError):
                    pass
        for key in ('created_at', 'updated_at', 'reviewed_at'):
            if key in c and c[key]:
                c[key] = c[key].isoformat() if hasattr(c[key], 'isoformat') else str(c[key])

    return jsonify({'success': True, 'conflicts': conflicts, 'count': len(conflicts)})

@tracks_bp.route('/api/ai/tracks/<int:track_id>/resolve', methods=['POST'])
def resolve_track_conflict(track_id):
    """Resolve a conflict on a track."""
    data = request.json or {}
    decision = data.get('decision')  # 'approve' or 'reject'
    reviewer = data.get('reviewer', request.headers.get('X-Auth-User', 'studio_user'))
    vehicle_subtype = data.get('vehicle_subtype')
    actual_class = data.get('actual_class')

    if decision not in ('approve', 'reject'):
        return jsonify({'success': False, 'error': 'decision must be approve or reject'}), 400

    result = db.resolve_track_conflict(track_id, decision, reviewer, vehicle_subtype, actual_class)
    return jsonify({'success': True, **result})

@tracks_bp.route('/api/ai/tracks/<int:track_id>', methods=['GET'])
def get_track_detail(track_id):
    """Get track details with representative prediction."""
    track = db.get_camera_object_track(track_id)
    if not track:
        return jsonify({'success': False, 'error': 'Track not found'}), 404

    # Serialize datetime fields
    for key in ('created_at', 'updated_at'):
        if key in track and track[key]:
            track[key] = track[key].isoformat() if hasattr(track[key], 'isoformat') else str(track[key])

    return jsonify({'success': True, 'track': track})

@tracks_bp.route('/api/ai/tracks/<int:track_id>/members', methods=['GET'])
def get_track_members(track_id):
    """Get all predictions in a track."""
    members = db.get_track_members(track_id)

    # Serialize
    for m in members:
        for key in ('predicted_tags', 'corrected_tags'):
            if key in m and m[key] is not None and not isinstance(m[key], (dict, list)):
                import json
                try:
                    m[key] = json.loads(m[key])
                except (json.JSONDecodeError, TypeError):
                    pass
        for key in ('reviewed_at',):
            if key in m and m[key]:
                m[key] = m[key].isoformat() if hasattr(m[key], 'isoformat') else str(m[key])

    return jsonify({'success': True, 'predictions': members, 'count': len(members)})


# ── Cross-Camera Entity Tracking ──────────────────────────────────────────

@tracks_bp.route('/api/ai/cross-camera/match', methods=['POST'])
def cross_camera_match():
    """Run cross-camera matching between camera pairs."""
    data = request.json or {}
    camera_a = data.get('camera_a')
    camera_b = data.get('camera_b')
    entity_type = data.get('entity_type', 'vehicle')

    from cross_camera_matcher import CrossCameraMatcher
    matcher = CrossCameraMatcher()

    if camera_a and camera_b:
        result = matcher.match_cameras(camera_a, camera_b, entity_type)
    else:
        result = matcher.match_all_pairs(entity_type)

    return jsonify({'success': True, **result})


@tracks_bp.route('/api/ai/cross-camera/links', methods=['GET'])
def get_cross_camera_links():
    """Get cross-camera links with optional filters."""
    track_id = request.args.get('track_id', type=int)
    identity_id = request.args.get('identity_id', type=int)
    camera_ids = request.args.get('camera_ids')
    status = request.args.get('status')

    camera_list = camera_ids.split(',') if camera_ids else None

    from cross_camera_matcher import CrossCameraMatcher
    matcher = CrossCameraMatcher()
    links = matcher.get_links(
        track_id=track_id,
        identity_id=identity_id,
        camera_ids=camera_list,
        status=status
    )

    # Serialize datetime fields
    for link in links:
        for key in ('created_at',):
            if key in link and link[key]:
                link[key] = link[key].isoformat() if hasattr(link[key], 'isoformat') else str(link[key])

    return jsonify({'success': True, 'links': links, 'count': len(links)})


@tracks_bp.route('/api/ai/cross-camera/review-queue', methods=['GET'])
def get_cross_camera_review_queue():
    """Get cross-camera links filtered for human review."""
    status = request.args.get('status', 'auto')
    entity_type = request.args.get('entity_type')

    from cross_camera_matcher import CrossCameraMatcher
    matcher = CrossCameraMatcher()
    links = matcher.get_links(status=status)

    if entity_type:
        links = [l for l in links if l.get('entity_type') == entity_type]

    # Serialize datetime fields
    for link in links:
        for key in ('created_at', 'first_seen_a', 'last_seen_a', 'first_seen_b', 'last_seen_b'):
            if key in link and link[key]:
                link[key] = link[key].isoformat() if hasattr(link[key], 'isoformat') else str(link[key])

    return jsonify({'success': True, 'links': links, 'count': len(links)})


@tracks_bp.route('/api/ai/cross-camera/links/<int:link_id>/confirm', methods=['POST'])
def confirm_cross_camera_link(link_id):
    """Confirm or reject a cross-camera link."""
    data = request.json or {}
    reject = data.get('reject', False)
    reviewer = data.get('reviewer', request.headers.get('X-Auth-User', 'studio_user'))

    from cross_camera_matcher import CrossCameraMatcher
    matcher = CrossCameraMatcher()
    rejection_reason = data.get('rejection_reason')
    result = matcher.confirm_link(link_id, confirmed_by=reviewer, reject=reject, rejection_reason=rejection_reason)

    if result is None:
        return jsonify({'success': False, 'error': 'Link not found'}), 404

    return jsonify({'success': True, **result})


@tracks_bp.route('/api/ai/cross-camera/identities', methods=['GET'])
def get_cross_camera_identities():
    """Get unique entities with linked tracks per camera."""
    entity_type = request.args.get('entity_type', 'vehicle')
    camera_ids = request.args.get('camera_ids')
    camera_list = camera_ids.split(',') if camera_ids else None

    from cross_camera_matcher import CrossCameraMatcher
    matcher = CrossCameraMatcher()
    identities = matcher.get_identities(entity_type=entity_type, camera_ids=camera_list)

    return jsonify({'success': True, 'identities': identities, 'count': len(identities)})


@tracks_bp.route('/api/ai/cross-camera/propagate', methods=['POST'])
def cross_camera_propagate():
    """Propagate classifications across linked tracks."""
    data = request.json or {}
    identity_id = data.get('identity_id')

    from cross_camera_matcher import CrossCameraMatcher
    matcher = CrossCameraMatcher()
    result = matcher.propagate_classifications(identity_id=identity_id)

    return jsonify({'success': True, **result})


@tracks_bp.route('/api/ai/cross-camera/summary', methods=['GET'])
def get_cross_camera_summary():
    """Get cross-camera matching summary statistics."""
    camera_ids = request.args.get('camera_ids')
    camera_list = camera_ids.split(',') if camera_ids else None

    from cross_camera_matcher import CrossCameraMatcher
    matcher = CrossCameraMatcher()
    summary = matcher.get_summary(camera_ids=camera_list)

    return jsonify({'success': True, **summary})


# ── Interpolation ─────────────────────────────────────────────────────────

@tracks_bp.route('/interpolation-review')
def interpolation_review():
    return render_template('interpolation_review.html')


@tracks_bp.route('/api/interpolation/tracks', methods=['GET'])
def get_interpolation_tracks():
    """List interpolation tracks, optionally filtered."""
    try:
        video_id = request.args.get('video_id', type=int)
        status = request.args.get('status')
        tracks = db.get_interpolation_tracks(video_id=video_id, status=status)
        # Ensure numeric types are JSON-serializable
        for t in tracks:
            for key in ('start_timestamp', 'end_timestamp', 'frame_interval'):
                if key in t and t[key] is not None:
                    t[key] = float(t[key])
        return jsonify({'success': True, 'tracks': tracks})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@tracks_bp.route('/api/interpolation/track/<int:track_id>', methods=['GET'])
def get_interpolation_track(track_id):
    """Get track details with all frame predictions and frame image URLs."""
    try:
        track = db.get_interpolation_track(track_id)
        if not track:
            return jsonify({'success': False, 'error': 'Track not found'}), 404

        # Ensure numeric types
        for key in ('start_timestamp', 'end_timestamp', 'frame_interval',
                     'start_confidence', 'end_confidence'):
            if key in track and track[key] is not None:
                track[key] = float(track[key])

        # Get frame predictions
        frames = []
        if track.get('batch_id'):
            preds = db.get_track_predictions(track['batch_id'])
            for p in preds:
                tags = p.get('predicted_tags', {})
                if isinstance(tags, str):
                    import json as _json
                    tags = _json.loads(tags)
                frames.append({
                    'id': p['id'],
                    'timestamp': float(p['timestamp']) if p['timestamp'] else 0,
                    'confidence': float(p['confidence']) if p['confidence'] else 0,
                    'bbox_x': p['bbox_x'],
                    'bbox_y': p['bbox_y'],
                    'bbox_width': p['bbox_width'],
                    'bbox_height': p['bbox_height'],
                    'review_status': p['review_status'],
                    'predicted_tags': tags,
                    'frame_cache': tags.get('frame_cache', ''),
                })

        return jsonify({'success': True, 'track': track, 'frames': frames})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@tracks_bp.route('/api/interpolation/track/<int:track_id>/review', methods=['POST'])
def review_interpolation_track(track_id):
    """Batch approve or reject all predictions in a track."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'Request body required'}), 400

        action = data.get('action')
        reviewer = data.get('reviewer', 'anonymous')

        if action not in ('approve', 'reject'):
            return jsonify({'success': False, 'error': 'action must be approve or reject'}), 400

        track = db.get_interpolation_track(track_id)
        if not track:
            return jsonify({'success': False, 'error': 'Track not found'}), 404

        # Get all predictions in this track
        predictions = db.get_track_predictions(track['batch_id']) if track.get('batch_id') else []

        approved_count = 0
        rejected_count = 0
        annotation_ids = []

        for pred in predictions:
            tags = pred.get('predicted_tags', {})
            if isinstance(tags, str):
                import json as _json
                tags = _json.loads(tags)

            # Skip unmatched frames (confidence 0) and already-reviewed
            if pred['review_status'] not in ('pending',):
                continue

            if action == 'approve' and not tags.get('unmatched'):
                db.review_prediction(pred['id'], 'approve', reviewer)
                ann_id = db.approve_prediction_to_annotation(pred['id'])
                if ann_id:
                    annotation_ids.append(ann_id)
                approved_count += 1
            elif action == 'reject' or tags.get('unmatched'):
                db.review_prediction(pred['id'], 'reject', reviewer)
                rejected_count += 1

        # Update track status
        new_status = 'approved' if action == 'approve' else 'rejected'
        db.update_interpolation_track(track_id, status=new_status, reviewed_by=reviewer)

        return jsonify({
            'success': True,
            'track_id': track_id,
            'status': new_status,
            'approved': approved_count,
            'rejected': rejected_count,
            'annotation_ids': annotation_ids,
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@tracks_bp.route('/api/interpolation/trigger', methods=['POST'])
def trigger_interpolation_manual():
    """Manually trigger interpolation from annotator movement tracking.

    Accepts keyframe data (bboxes + timestamps) and creates synthetic
    approved predictions, then triggers guided interpolation between
    consecutive keyframe pairs.
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'Request body required'}), 400

        video_id = data.get('video_id')
        class_name = data.get('class_name')
        track_label = data.get('track_label', '')
        keyframes = data.get('keyframes', [])

        if not video_id or not class_name:
            return jsonify({'success': False, 'error': 'video_id and class_name required'}), 400
        if len(keyframes) < 2:
            return jsonify({'success': False, 'error': 'At least 2 keyframes required'}), 400

        # Sort keyframes by timestamp
        keyframes.sort(key=lambda kf: kf['timestamp'])

        # Create synthetic approved predictions for each keyframe
        import json as _json
        pred_ids = []
        batch_id = f"manual-track-{int(time.time())}"

        for kf in keyframes:
            bbox = kf['bbox']
            pred_list = db.insert_predictions_batch(
                video_id=video_id,
                model_name='vehicle-world-v1',
                model_version='2.0',
                batch_id=batch_id,
                predictions=[{
                    'prediction_type': 'keyframe',
                    'confidence': 1.0,
                    'timestamp': kf['timestamp'],
                    'scenario': 'vehicle_detection',
                    'tags': {
                        'class': class_name,
                        'vehicle_type': class_name,
                        'source': 'manual_tracking',
                        'track_label': track_label,
                    },
                    'bbox': {
                        'x': bbox['x'],
                        'y': bbox['y'],
                        'width': bbox['width'],
                        'height': bbox['height'],
                    },
                }]
            )
            if pred_list:
                pred_id = pred_list[0]
                # Mark as approved
                db.review_prediction(pred_id, 'approve', 'movement_tracker')
                pred_ids.append(pred_id)

        # Trigger interpolation between consecutive keyframe pairs
        tracks_created = 0
        from interpolation_runner import run_guided_interpolation
        for i in range(len(pred_ids) - 1):
            start_id = pred_ids[i]
            end_id = pred_ids[i + 1]

            # Check no track already exists
            if db.interpolation_track_exists(start_id, end_id):
                continue

            threading.Thread(
                target=run_guided_interpolation,
                args=(video_id, start_id, end_id),
                daemon=True,
                name=f"interp-manual-{start_id}-{end_id}"
            ).start()
            tracks_created += 1

        return jsonify({
            'success': True,
            'prediction_ids': pred_ids,
            'tracks_created': tracks_created,
        })
    except Exception as e:
        logger.error(f"Manual interpolation trigger failed: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@tracks_bp.route('/frame-cache/<int:video_id>/<path:filename>')
def serve_frame_cache(video_id, filename):
    """Serve pre-extracted frame images from the frame cache."""
    import os
    cache_dir = os.path.join('/opt/groundtruth-studio/frame_cache', str(video_id))
    return send_from_directory(cache_dir, filename)
