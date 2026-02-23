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

FRIGATE_URL = os.environ.get('FRIGATE_URL', 'http://localhost:5000')

def _get_frigate_url() -> str:
    """Get Frigate URL from env, or ingester singleton, or default."""
    if FRIGATE_URL:
        return FRIGATE_URL
    try:
        from frigate_ingester import get_ingester
        ingester = get_ingester()
        if ingester:
            return ingester.frigate_url
    except Exception:
        pass
    return 'http://localhost:5000'


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


# ── Crossing Line Configuration ───────────────────────────────────────────

@tracks_bp.route('/crossing-line-config')
def crossing_line_config():
    """Page for crossing line configuration."""
    return render_template('crossing_line_config.html')


@tracks_bp.route('/api/ai/crossing-lines', methods=['GET'])
def get_crossing_lines():
    """List all crossing lines, optionally filtered by camera_id."""
    try:
        camera_id = request.args.get('camera_id')

        with get_cursor(commit=False) as cursor:
            if camera_id:
                cursor.execute("""
                    SELECT id, camera_id, line_name, x1, y1, x2, y2,
                           forward_dx, forward_dy, paired_camera_id, paired_line_id,
                           lane_mapping_reversed, created_at
                    FROM camera_crossing_lines
                    WHERE camera_id = %s
                    ORDER BY camera_id, line_name
                """, (camera_id,))
            else:
                cursor.execute("""
                    SELECT id, camera_id, line_name, x1, y1, x2, y2,
                           forward_dx, forward_dy, paired_camera_id, paired_line_id,
                           lane_mapping_reversed, created_at
                    FROM camera_crossing_lines
                    ORDER BY camera_id, line_name
                """)

            lines = [dict(row) for row in cursor.fetchall()]

            # Also fetch all cameras that have tracks (for the UI camera selector)
            cursor.execute("""
                SELECT DISTINCT camera_id FROM camera_object_tracks ORDER BY camera_id
            """)
            cameras = [row['camera_id'] for row in cursor.fetchall()]

        # Serialize datetime fields
        for line in lines:
            if line.get('created_at'):
                line['created_at'] = line['created_at'].isoformat()

        return jsonify({'success': True, 'lines': lines, 'count': len(lines), 'cameras': cameras})
    except Exception as e:
        logger.error("Get crossing lines error: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@tracks_bp.route('/api/ai/crossing-lines/<camera_id>/frame', methods=['GET'])
def get_camera_frame(camera_id):
    """Get a sample frame for the given camera."""
    try:
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT v.id, v.thumbnail_path
                FROM videos v
                WHERE v.camera_id = %s
                ORDER BY v.id DESC
                LIMIT 1
            """, (camera_id,))

            result = cursor.fetchone()
            if not result:
                return jsonify({'success': False, 'error': 'No videos found for camera'}), 404

            video = dict(result)

            if video['thumbnail_path']:
                thumb_path = video['thumbnail_path']
                if os.path.isabs(thumb_path):
                    # Absolute path stored in DB
                    if os.path.exists(thumb_path):
                        return send_from_directory(os.path.dirname(thumb_path), os.path.basename(thumb_path))
                else:
                    thumbnail_file = os.path.join(THUMBNAIL_DIR, thumb_path)
                    if os.path.exists(thumbnail_file):
                        return send_from_directory(THUMBNAIL_DIR, thumb_path)

        return jsonify({'success': False, 'error': 'No thumbnail available'}), 404
    except Exception as e:
        logger.error("Get camera frame error: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@tracks_bp.route('/api/ai/crossing-lines', methods=['POST'])
def create_crossing_line():
    """Create or update a crossing line."""
    try:
        data = request.json or {}
        camera_id = data.get('camera_id')
        line_name = data.get('line_name')
        x1 = data.get('x1')
        y1 = data.get('y1')
        x2 = data.get('x2')
        y2 = data.get('y2')
        forward_dx = data.get('forward_dx', 1.0)
        forward_dy = data.get('forward_dy', 0.0)

        if not all([camera_id, line_name, x1 is not None, y1 is not None,
                    x2 is not None, y2 is not None]):
            return jsonify({'success': False, 'error': 'Missing required fields'}), 400

        with get_cursor() as cursor:
            cursor.execute("""
                INSERT INTO camera_crossing_lines
                    (camera_id, line_name, x1, y1, x2, y2, forward_dx, forward_dy)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (camera_id, line_name)
                DO UPDATE SET
                    x1 = EXCLUDED.x1,
                    y1 = EXCLUDED.y1,
                    x2 = EXCLUDED.x2,
                    y2 = EXCLUDED.y2,
                    forward_dx = EXCLUDED.forward_dx,
                    forward_dy = EXCLUDED.forward_dy
                RETURNING id, camera_id, line_name, x1, y1, x2, y2,
                          forward_dx, forward_dy, paired_camera_id, paired_line_id,
                          lane_mapping_reversed, created_at
            """, (camera_id, line_name, x1, y1, x2, y2, forward_dx, forward_dy))

            line = dict(cursor.fetchone())

        # Serialize datetime
        if line.get('created_at'):
            line['created_at'] = line['created_at'].isoformat()

        return jsonify({'success': True, 'line': line})
    except Exception as e:
        logger.error("Create crossing line error: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@tracks_bp.route('/api/ai/crossing-lines/<int:line_id>', methods=['DELETE'])
def delete_crossing_line(line_id):
    """Delete a crossing line and clear any pairings."""
    try:
        with get_cursor() as cursor:
            # Clear paired_line_id on any line that was paired to this one
            cursor.execute("""
                UPDATE camera_crossing_lines
                SET paired_line_id = NULL, paired_camera_id = NULL
                WHERE paired_line_id = %s
            """, (line_id,))

            # Delete the line
            cursor.execute("""
                DELETE FROM camera_crossing_lines
                WHERE id = %s
            """, (line_id,))

        return jsonify({'success': True})
    except Exception as e:
        logger.error("Delete crossing line error: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@tracks_bp.route('/api/ai/crossing-lines/<int:line_id>/pair', methods=['POST'])
def pair_crossing_lines(line_id):
    """Pair two crossing lines for cross-camera matching."""
    try:
        data = request.json or {}
        paired_line_id = data.get('paired_line_id')
        lane_mapping_reversed = data.get('lane_mapping_reversed', False)

        if not paired_line_id:
            return jsonify({'success': False, 'error': 'paired_line_id required'}), 400

        with get_cursor() as cursor:
            # Get both lines
            cursor.execute("""
                SELECT id, camera_id, line_name
                FROM camera_crossing_lines
                WHERE id IN (%s, %s)
            """, (line_id, paired_line_id))

            lines = [dict(row) for row in cursor.fetchall()]
            if len(lines) != 2:
                return jsonify({'success': False, 'error': 'One or both lines not found'}), 404

            line_a = next(l for l in lines if l['id'] == line_id)
            line_b = next(l for l in lines if l['id'] == paired_line_id)

            # Update both lines to point at each other
            cursor.execute("""
                UPDATE camera_crossing_lines
                SET paired_line_id = %s,
                    paired_camera_id = %s,
                    lane_mapping_reversed = %s
                WHERE id = %s
                RETURNING id, camera_id, line_name, x1, y1, x2, y2,
                          forward_dx, forward_dy, paired_camera_id, paired_line_id,
                          lane_mapping_reversed, created_at
            """, (paired_line_id, line_b['camera_id'], lane_mapping_reversed, line_id))

            updated_a = dict(cursor.fetchone())

            cursor.execute("""
                UPDATE camera_crossing_lines
                SET paired_line_id = %s,
                    paired_camera_id = %s,
                    lane_mapping_reversed = %s
                WHERE id = %s
                RETURNING id, camera_id, line_name, x1, y1, x2, y2,
                          forward_dx, forward_dy, paired_camera_id, paired_line_id,
                          lane_mapping_reversed, created_at
            """, (line_id, line_a['camera_id'], lane_mapping_reversed, paired_line_id))

            updated_b = dict(cursor.fetchone())

        # Serialize datetime fields
        for line in [updated_a, updated_b]:
            if line.get('created_at'):
                line['created_at'] = line['created_at'].isoformat()

        return jsonify({'success': True, 'line_a': updated_a, 'line_b': updated_b})
    except Exception as e:
        logger.error("Pair crossing lines error: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@tracks_bp.route('/api/ai/crossing-lines/<int:line_id>/pair', methods=['DELETE'])
def unpair_crossing_lines(line_id):
    """Remove pairing from a crossing line (and its partner)."""
    try:
        with get_cursor() as cursor:
            # Find the partner line
            cursor.execute("""
                SELECT paired_line_id FROM camera_crossing_lines WHERE id = %s
            """, (line_id,))
            row = cursor.fetchone()
            if not row:
                return jsonify({'success': False, 'error': 'Line not found'}), 404

            paired_id = row['paired_line_id']

            # Clear pairing on this line
            cursor.execute("""
                UPDATE camera_crossing_lines
                SET paired_line_id = NULL, paired_camera_id = NULL, lane_mapping_reversed = FALSE
                WHERE id = %s
            """, (line_id,))

            # Clear pairing on partner line
            if paired_id:
                cursor.execute("""
                    UPDATE camera_crossing_lines
                    SET paired_line_id = NULL, paired_camera_id = NULL, lane_mapping_reversed = FALSE
                    WHERE id = %s
                """, (paired_id,))

        return jsonify({'success': True})
    except Exception as e:
        logger.error("Unpair crossing lines error: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@tracks_bp.route('/api/ai/cross-camera/match-spatial', methods=['POST'])
def cross_camera_match_spatial():
    """Run crossing-line spatial matcher only."""
    try:
        data = request.json or {}
        entity_type = data.get('entity_type', 'vehicle')

        from crossing_line_matcher import CrossingLineMatcher
        matcher = CrossingLineMatcher()
        result = matcher.match_all(entity_type)

        return jsonify({'success': True, **result})
    except Exception as e:
        logger.error("Spatial matching error: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@tracks_bp.route('/api/ai/cross-camera/reid-training-pairs', methods=['GET'])
def get_reid_training_pairs():
    """Export confirmed spatial matches as image pairs for ReID training."""
    try:
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT
                    ccl.id as link_id,
                    ccl.track_a_id,
                    ccl.track_b_id,
                    ccl.match_confidence,
                    ta.representative_prediction_id as pred_a_id,
                    tb.representative_prediction_id as pred_b_id
                FROM cross_camera_links ccl
                JOIN camera_object_tracks ta ON ccl.track_a_id = ta.id
                JOIN camera_object_tracks tb ON ccl.track_b_id = tb.id
                WHERE ccl.status IN ('confirmed', 'auto_confirmed')
                  AND ccl.match_method IN ('direction', 'crossing_line')
                  AND ta.representative_prediction_id IS NOT NULL
                  AND tb.representative_prediction_id IS NOT NULL
                ORDER BY ccl.match_confidence DESC
            """)

            links = [dict(row) for row in cursor.fetchall()]

            if not links:
                return jsonify({'success': True, 'pairs': [], 'count': 0})

            # Get prediction thumbnails
            pred_ids = []
            for link in links:
                pred_ids.extend([link['pred_a_id'], link['pred_b_id']])

            cursor.execute("""
                SELECT id, thumbnail_path
                FROM ai_predictions
                WHERE id = ANY(%s)
            """, (pred_ids,))

            preds = {row['id']: dict(row) for row in cursor.fetchall()}

        # Build pairs
        pairs = []
        for link in links:
            pred_a = preds.get(link['pred_a_id'])
            pred_b = preds.get(link['pred_b_id'])

            if not pred_a or not pred_b:
                continue

            image_a = pred_a.get('thumbnail_path')
            image_b = pred_b.get('thumbnail_path')

            if image_a and image_b:
                pairs.append({
                    'link_id': link['link_id'],
                    'track_a_id': link['track_a_id'],
                    'track_b_id': link['track_b_id'],
                    'image_a_path': image_a,
                    'image_b_path': image_b,
                    'confidence': float(link['match_confidence']) if link['match_confidence'] else 0.0,
                })

        return jsonify({'success': True, 'pairs': pairs, 'count': len(pairs)})
    except Exception as e:
        logger.error("Get ReID training pairs error: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# ── Cross-Camera Entity Tracking ──────────────────────────────────────────

@tracks_bp.route('/api/ai/cross-camera/match', methods=['POST'])
def cross_camera_match():
    """Run cross-camera matching: spatial first, then ReID for remaining."""
    data = request.json or {}
    camera_a = data.get('camera_a')
    camera_b = data.get('camera_b')
    entity_type = data.get('entity_type', 'vehicle')

    # Phase 1: Run crossing-line spatial matcher for configured pairs
    spatial_result = {}
    spatial_matched_pairs = set()
    try:
        from crossing_line_matcher import CrossingLineMatcher
        spatial_matcher = CrossingLineMatcher()
        spatial_result = spatial_matcher.match_all(entity_type)
        # Collect pairs matched spatially to exclude from ReID
        if spatial_result.get('total_links_created'):
            with get_cursor(commit=False) as cursor:
                cursor.execute("""
                    SELECT track_a_id, track_b_id FROM cross_camera_links
                    WHERE match_method IN ('direction', 'crossing_line')
                """)
                for row in cursor.fetchall():
                    spatial_matched_pairs.add((row['track_a_id'], row['track_b_id']))
    except Exception as e:
        logger.warning("Spatial matching skipped: %s", e)
        spatial_result = {'error': str(e)}

    # Phase 2: Run ReID-based matcher for remaining pairs
    from cross_camera_matcher import CrossCameraMatcher
    matcher = CrossCameraMatcher()
    matcher.exclude_pairs = spatial_matched_pairs

    if camera_a and camera_b:
        result = matcher.match_cameras(camera_a, camera_b, entity_type)
    else:
        result = matcher.match_all_pairs(entity_type)

    result['spatial_matching'] = spatial_result
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


@tracks_bp.route('/api/ai/cross-camera/links/<int:link_id>/clips', methods=['GET'])
def get_cross_camera_link_clips(link_id):
    """Get video clip info for both cameras in a cross-camera link."""
    try:
        with get_cursor(commit=False) as cursor:
            # 1. Get the cross_camera_link
            cursor.execute(
                "SELECT id, track_a_id, track_b_id, source_track_type FROM cross_camera_links WHERE id = %s",
                (link_id,)
            )
            link = cursor.fetchone()
            if not link:
                return jsonify({'success': False, 'error': 'Link not found'}), 404

            source_type = link.get('source_track_type') or 'camera_object'

            def _get_clip_data_camera_object(track_id):
                """Find best prediction for a camera_object track."""
                cursor.execute("""
                    SELECT p.id as pred_id, p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
                           p.confidence,
                           v.id as video_id, v.width as video_width, v.height as video_height,
                           v.metadata, v.camera_id
                    FROM ai_predictions p
                    JOIN videos v ON p.video_id = v.id
                    WHERE p.camera_object_track_id = %s
                      AND p.review_status IN ('approved', 'pending', 'processing')
                    ORDER BY p.confidence DESC
                    LIMIT 1
                """, (track_id,))
                row = cursor.fetchone()
                if not row:
                    return None

                metadata = row.get('metadata') or {}
                if isinstance(metadata, str):
                    try:
                        metadata = json.loads(metadata)
                    except (json.JSONDecodeError, TypeError):
                        metadata = {}

                frigate_event_id = metadata.get('frigate_event_id')
                has_clip = metadata.get('has_clip', False)
                frigate_camera = metadata.get('frigate_camera', '')
                frigate_box = metadata.get('frigate_box')

                clip_url = None
                if frigate_event_id and has_clip:
                    clip_url = f"/api/ai/predictions/{row['pred_id']}/clip"

                return {
                    'camera_id': row['camera_id'] or frigate_camera,
                    'pred_id': row['pred_id'],
                    'clip_url': clip_url,
                    'has_clip': bool(frigate_event_id and has_clip),
                    'bbox': {
                        'x': row['bbox_x'] or 0,
                        'y': row['bbox_y'] or 0,
                        'width': row['bbox_width'] or 0,
                        'height': row['bbox_height'] or 0,
                    },
                    'video_width': row['video_width'] or 0,
                    'video_height': row['video_height'] or 0,
                    'frigate_box': frigate_box,
                }

            def _get_clip_data_video_track(track_id):
                """Get clip data from a video_track."""
                cursor.execute("""
                    SELECT vt.id, vt.camera_id, vt.best_crop_path,
                           vt.bbox_centroid_x, vt.bbox_centroid_y,
                           vt.avg_bbox_width, vt.avg_bbox_height,
                           vt.avg_confidence, vt.class_name,
                           vt.trajectory, vt.first_seen, vt.first_seen_epoch,
                           v.id as video_id, v.width as video_width, v.height as video_height,
                           v.metadata
                    FROM video_tracks vt
                    JOIN videos v ON vt.video_id = v.id
                    WHERE vt.id = %s
                """, (track_id,))
                row = cursor.fetchone()
                if not row:
                    return None

                metadata = row.get('metadata') or {}
                if isinstance(metadata, str):
                    try:
                        metadata = json.loads(metadata)
                    except (json.JSONDecodeError, TypeError):
                        metadata = {}

                frigate_event_id = metadata.get('frigate_event_id')
                has_clip = metadata.get('has_clip', False)

                # For video tracks, serve the crop image directly
                crop_url = None
                if row.get('best_crop_path'):
                    crop_url = f"/api/ai/video-tracks/{track_id}/crop"

                # Build clip URL from the video's frigate event
                clip_url = None
                if frigate_event_id and has_clip:
                    clip_url = f"/api/ai/video-tracks/{track_id}/clip"

                # Compute average bbox position from centroid
                avg_w = row['avg_bbox_width'] or 0
                avg_h = row['avg_bbox_height'] or 0
                cx = row['bbox_centroid_x'] or 0
                cy = row['bbox_centroid_y'] or 0

                # Parse trajectory for per-frame bbox tracking
                trajectory = row.get('trajectory') or []
                if isinstance(trajectory, str):
                    try:
                        trajectory = json.loads(trajectory)
                    except (json.JSONDecodeError, TypeError):
                        trajectory = []

                # Find continuation tracks: same video, using velocity
                # extrapolation. Searches both forward and backward, and
                # accepts compatible vehicle classes (ATV/UTV/pickup/SUV
                # often confused by YOLO for distant vehicles).
                first_seen_t = trajectory[0].get('timestamp', 0) if trajectory else 0
                last_seen_t = trajectory[-1].get('timestamp', 0) if trajectory else 0

                compatible_classes = {'ATV', 'UTV', 'pickup truck', 'SUV', 'car', 'motorcycle'}

                def _extract_velocity(pts):
                    """Compute velocity from trajectory points."""
                    real = [p for p in pts if not p.get('gap_filled')]
                    if len(real) < 2:
                        real = pts[-5:]
                    else:
                        real = real[-5:]
                    if len(real) < 2:
                        return 0, 0
                    p0, p1 = real[0], real[-1]
                    dt = p1.get('timestamp', 0) - p0.get('timestamp', 0)
                    if dt <= 0:
                        return 0, 0
                    vvx = ((p1.get('x', 0) + p1.get('w', 0) / 2) - (p0.get('x', 0) + p0.get('w', 0) / 2)) / dt
                    vvy = ((p1.get('y', 0) + p1.get('h', 0) / 2) - (p0.get('y', 0) + p0.get('h', 0) / 2)) / dt
                    return vvx, vvy

                def _centroid(pt):
                    return pt.get('x', 0) + pt.get('w', 0) / 2, pt.get('y', 0) + pt.get('h', 0) / 2

                def _try_merge(cand_traj, cand_class, anchor_pt, anchor_vx, anchor_vy, anchor_t, forward=True):
                    """Check if candidate track is a continuation based on velocity extrapolation."""
                    if not cand_traj:
                        return False
                    ref_pt = cand_traj[0] if forward else cand_traj[-1]
                    ref_t = ref_pt.get('timestamp', 0)
                    dt_gap = abs(ref_t - anchor_t)
                    # Extrapolate anchor position to candidate time
                    direction = 1 if forward else -1
                    proj_x = anchor_pt[0] + anchor_vx * dt_gap * direction
                    proj_y = anchor_pt[1] + anchor_vy * dt_gap * direction
                    fx, fy = _centroid(ref_pt)
                    dist = ((fx - proj_x) ** 2 + (fy - proj_y) ** 2) ** 0.5
                    speed = (anchor_vx ** 2 + anchor_vy ** 2) ** 0.5
                    threshold = 300 + speed * dt_gap * 0.5
                    threshold = min(threshold, 1500)
                    # Compatible class gets 2x threshold
                    track_cls = row.get('class_name', '')
                    if cand_class in compatible_classes and track_cls in compatible_classes:
                        threshold *= 2.0
                    elif cand_class == track_cls:
                        threshold *= 2.0
                    return dist < threshold

                predecessor_traj = []
                continuation_traj = []
                track_class = row.get('class_name', '')

                if trajectory and last_seen_t:
                    # --- Forward search (tracks starting after this one ends) ---
                    last_pt_c = _centroid(trajectory[-1])
                    vx, vy = _extract_velocity(trajectory)

                    cursor.execute("""
                        SELECT id, trajectory, first_seen, class_name
                        FROM video_tracks
                        WHERE video_id = %s AND id != %s
                          AND first_seen > %s AND first_seen < %s + 8.0
                        ORDER BY first_seen
                    """, (row['video_id'], track_id, last_seen_t, last_seen_t))

                    for cont_row in cursor.fetchall():
                        ct = cont_row.get('trajectory') or []
                        if isinstance(ct, str):
                            try: ct = json.loads(ct)
                            except: ct = []
                        if not ct:
                            continue
                        if _try_merge(ct, cont_row.get('class_name', ''), last_pt_c, vx, vy, last_seen_t, forward=True):
                            continuation_traj.extend(ct)
                            last_pt_c = _centroid(ct[-1])
                            last_seen_t = ct[-1].get('timestamp', last_seen_t)
                            all_pts = trajectory + continuation_traj
                            vx, vy = _extract_velocity(all_pts[-10:])

                    # --- Backward search (tracks ending before this one starts) ---
                    first_pt_c = _centroid(trajectory[0])
                    bvx, bvy = _extract_velocity(trajectory[:10])

                    cursor.execute("""
                        SELECT id, trajectory, last_seen, class_name
                        FROM video_tracks
                        WHERE video_id = %s AND id != %s
                          AND last_seen < %s AND last_seen > %s - 8.0
                        ORDER BY last_seen DESC
                    """, (row['video_id'], track_id, first_seen_t, first_seen_t))

                    for pred_row in cursor.fetchall():
                        pt = pred_row.get('trajectory') or []
                        if isinstance(pt, str):
                            try: pt = json.loads(pt)
                            except: pt = []
                        if not pt:
                            continue
                        if _try_merge(pt, pred_row.get('class_name', ''), first_pt_c, bvx, bvy, first_seen_t, forward=False):
                            predecessor_traj = pt + predecessor_traj
                            first_pt_c = _centroid(pt[0])
                            first_seen_t = pt[0].get('timestamp', first_seen_t)
                            all_pts = predecessor_traj + trajectory
                            bvx, bvy = _extract_velocity(all_pts[:10])

                # Merge predecessor + main + continuation, sorted by timestamp
                full_trajectory = predecessor_traj + trajectory + continuation_traj
                full_trajectory.sort(key=lambda p: p.get('timestamp', 0))

                return {
                    'camera_id': row['camera_id'],
                    'video_track_id': track_id,
                    'crop_url': crop_url,
                    'clip_url': clip_url,
                    'has_clip': bool(frigate_event_id and has_clip),
                    'bbox': {
                        'x': max(0, cx - avg_w // 2),
                        'y': max(0, cy - avg_h // 2),
                        'width': avg_w,
                        'height': avg_h,
                    },
                    'trajectory': full_trajectory,
                    'first_seen': row.get('first_seen') or 0,
                    'first_seen_epoch': row.get('first_seen_epoch') or 0,
                    'video_width': row['video_width'] or 0,
                    'video_height': row['video_height'] or 0,
                    'class_name': row.get('class_name') or '',
                    'source_track_type': 'video_track',
                }

            if source_type == 'video_track':
                camera_a = _get_clip_data_video_track(link['track_a_id'])
                camera_b = _get_clip_data_video_track(link['track_b_id'])
            else:
                camera_a = _get_clip_data_camera_object(link['track_a_id'])
                camera_b = _get_clip_data_camera_object(link['track_b_id'])

            if not camera_a and not camera_b:
                return jsonify({'success': False, 'error': 'No track data found'}), 404

            return jsonify({
                'success': True,
                'link_id': link_id,
                'source_track_type': source_type,
                'camera_a': camera_a,
                'camera_b': camera_b,
            })

    except Exception as e:
        logger.error(f'Failed to get cross-camera link clips for link {link_id}: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


@tracks_bp.route('/api/ai/cross-camera/<int:link_id>/consensus')
def get_cross_camera_consensus(link_id):
    """Get consensus bbox from the other camera when one camera's bbox is projected.

    When camera A has a projected/lost bbox, camera B may still have a real detection.
    This endpoint returns the other camera's detection status to confirm presence.

    Query params:
        camera: 'A' or 'B' -- which camera needs help (has projected bbox)
        time: float -- current video time on that camera
    """
    try:
        requesting_camera = request.args.get('camera', 'A').upper()
        video_time = request.args.get('time', type=float)

        if requesting_camera not in ('A', 'B'):
            return jsonify({'has_consensus': False, 'error': 'camera must be A or B'}), 400
        if video_time is None:
            return jsonify({'has_consensus': False, 'error': 'time parameter required'}), 400

        with get_cursor(commit=False) as cursor:
            # 1. Get the cross_camera_link
            cursor.execute(
                "SELECT id, track_a_id, track_b_id, source_track_type FROM cross_camera_links WHERE id = %s",
                (link_id,)
            )
            link = cursor.fetchone()
            if not link:
                return jsonify({'has_consensus': False, 'error': 'Link not found'}), 404

            source_type = link.get('source_track_type') or 'camera_object'

            # 2. Get camera IDs and trajectories for both tracks
            if source_type == 'video_track':
                cursor.execute("""
                    SELECT id, camera_id, trajectory, first_seen, first_seen_epoch
                    FROM video_tracks WHERE id = %s
                """, (link['track_a_id'],))
                track_a = cursor.fetchone()

                cursor.execute("""
                    SELECT id, camera_id, trajectory, first_seen, first_seen_epoch
                    FROM video_tracks WHERE id = %s
                """, (link['track_b_id'],))
                track_b = cursor.fetchone()
            else:
                # camera_object tracks don't have trajectory, skip consensus
                return jsonify({'has_consensus': False, 'reason': 'camera_object tracks lack trajectory data'})

            if not track_a or not track_b:
                return jsonify({'has_consensus': False, 'error': 'One or both tracks not found'}), 404

            camera_a_id = track_a['camera_id']
            camera_b_id = track_b['camera_id']

            # 3. Get temporal offset from camera_topology_learned
            # Try both orderings since topology may be stored in either direction
            cursor.execute("""
                SELECT camera_a, camera_b, avg_transit_seconds
                FROM camera_topology_learned
                WHERE (camera_a = %s AND camera_b = %s)
                   OR (camera_a = %s AND camera_b = %s)
            """, (camera_a_id, camera_b_id, camera_b_id, camera_a_id))
            topo = cursor.fetchone()

            temporal_offset = 0.0
            if topo:
                avg_transit = topo['avg_transit_seconds'] or 0.0
                # If requesting camera is A, other camera is B
                # The transit time represents A->B travel time
                if topo['camera_a'] == camera_a_id:
                    # Topology is A->B
                    if requesting_camera == 'A':
                        temporal_offset = avg_transit   # A sees first, B sees later
                    else:
                        temporal_offset = -avg_transit  # B is requesting, A saw earlier
                else:
                    # Topology is B->A
                    if requesting_camera == 'A':
                        temporal_offset = -avg_transit
                    else:
                        temporal_offset = avg_transit

            # 4. Determine which track is the "other" camera
            if requesting_camera == 'A':
                other_track = track_b
                other_camera_id = camera_b_id
            else:
                other_track = track_a
                other_camera_id = camera_a_id

            # 5. Parse the other camera's trajectory
            other_traj = other_track.get('trajectory') or []
            if isinstance(other_traj, str):
                try:
                    other_traj = json.loads(other_traj)
                except (json.JSONDecodeError, TypeError):
                    other_traj = []

            if not other_traj:
                return jsonify({'has_consensus': False, 'reason': 'Other camera has no trajectory'})

            # 6. Compute the corresponding time on the other camera
            # The video_time is relative to clip start. We need to convert via epoch.
            # Both tracks have first_seen (clip-relative) and first_seen_epoch (absolute).
            if requesting_camera == 'A':
                req_epoch_base = track_a.get('first_seen_epoch') or 0
                req_first_seen = track_a.get('first_seen') or 0
                other_epoch_base = track_b.get('first_seen_epoch') or 0
                other_first_seen = track_b.get('first_seen') or 0
            else:
                req_epoch_base = track_b.get('first_seen_epoch') or 0
                req_first_seen = track_b.get('first_seen') or 0
                other_epoch_base = track_a.get('first_seen_epoch') or 0
                other_first_seen = track_a.get('first_seen') or 0

            # Convert requesting camera's video time to absolute epoch
            # video_time is relative to clip start (first_seen in clip time)
            # The trajectory timestamps are also clip-relative (first_seen based)
            # We use the temporal_offset to find the other camera's corresponding clip time
            # other_time = video_time + temporal_offset (adjusted for clip offsets)
            if req_epoch_base > 0 and other_epoch_base > 0:
                # Convert to absolute time, apply offset, convert back
                abs_time = req_epoch_base + (video_time - req_first_seen)
                other_abs_time = abs_time + temporal_offset
                other_time = other_first_seen + (other_abs_time - other_epoch_base)
            else:
                # Fallback: just use video_time with offset
                other_time = video_time + temporal_offset

            # 7. Find closest detection in other camera's trajectory
            closest_point = None
            closest_dist = float('inf')
            for pt in other_traj:
                ts = pt.get('timestamp') or pt.get('ts') or 0
                dist = abs(ts - other_time)
                if dist < closest_dist:
                    closest_dist = dist
                    closest_point = pt

            if not closest_point:
                return jsonify({'has_consensus': False, 'reason': 'No detection found on other camera'})

            # 8. Determine confidence level based on temporal proximity
            if closest_dist <= 0.2:
                confidence = 'high'
            elif closest_dist <= 1.0:
                confidence = 'medium'
            else:
                confidence = 'low'

            # If the closest point is too far away (> 5s), no useful consensus
            if closest_dist > 5.0:
                return jsonify({
                    'has_consensus': False,
                    'reason': 'No temporally close detection on other camera',
                    'closest_gap_seconds': round(closest_dist, 2)
                })

            return jsonify({
                'has_consensus': True,
                'other_camera_id': other_camera_id,
                'other_camera_detection': {
                    'x': closest_point.get('x', 0),
                    'y': closest_point.get('y', 0),
                    'w': closest_point.get('w', 0),
                    'h': closest_point.get('h', 0),
                    'conf': closest_point.get('conf') or closest_point.get('confidence', 0)
                },
                'time_offset_used': round(temporal_offset, 2),
                'other_time_computed': round(other_time, 3),
                'temporal_gap': round(closest_dist, 3),
                'confidence': confidence
            })

    except Exception as e:
        logger.error(f'Failed to get cross-camera consensus for link {link_id}: {e}', exc_info=True)
        return jsonify({'has_consensus': False, 'error': str(e)}), 500


@tracks_bp.route('/api/ai/video-tracks/<int:track_id>/crop', methods=['GET'])
def get_video_track_crop(track_id):
    """Serve the best crop image for a video track."""
    try:
        with get_cursor(commit=False) as cursor:
            cursor.execute("SELECT best_crop_path FROM video_tracks WHERE id = %s", (track_id,))
            row = cursor.fetchone()
            if not row or not row.get('best_crop_path'):
                return jsonify({'success': False, 'error': 'No crop found'}), 404

            import os
            crop_path = row['best_crop_path']
            if not os.path.isfile(crop_path):
                return jsonify({'success': False, 'error': 'Crop file missing'}), 404

            directory = os.path.dirname(crop_path)
            filename = os.path.basename(crop_path)
            return send_from_directory(directory, filename, mimetype='image/jpeg')
    except Exception as e:
        logger.error(f'Failed to serve video track crop {track_id}: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


@tracks_bp.route('/api/ai/video-tracks/<int:track_id>/clip', methods=['GET'])
def get_video_track_clip(track_id):
    """Serve the video clip for a video track (via its parent video's Frigate event)."""
    try:
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT vt.video_id, v.metadata
                FROM video_tracks vt
                JOIN videos v ON vt.video_id = v.id
                WHERE vt.id = %s
            """, (track_id,))
            row = cursor.fetchone()
            if not row:
                return jsonify({'success': False, 'error': 'Track not found'}), 404

            metadata = row.get('metadata') or {}
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except (json.JSONDecodeError, TypeError):
                    metadata = {}

            frigate_event_id = metadata.get('frigate_event_id')
            if not frigate_event_id:
                return jsonify({'success': False, 'error': 'No Frigate event for this track'}), 404

            import os
            # Check for cached clip first (downloaded during backfill)
            clip_path = f'/opt/groundtruth-studio/clips/frigate_{frigate_event_id}.mp4'
            if not os.path.isfile(clip_path):
                # Try fetching from Frigate if not cached
                from video_utils import VideoProcessor
                processor = VideoProcessor()
                result = processor.fetch_frigate_clip(
                    frigate_url=_get_frigate_url(),
                    event_id=frigate_event_id,
                    camera=metadata.get('frigate_camera', '')
                )
                if not result.get('success'):
                    return jsonify({'success': False, 'error': result.get('error', 'Clip not available')}), 404
                clip_path = result['clip_path']

            directory = os.path.dirname(clip_path)
            filename = os.path.basename(clip_path)
            return send_from_directory(directory, filename, mimetype='video/mp4')
    except Exception as e:
        logger.error(f'Failed to serve video track clip {track_id}: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


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


@tracks_bp.route('/api/interpolation/scan', methods=['POST'])
def scan_and_trigger_interpolation():
    """Scan all approved predictions and trigger interpolation for eligible pairs."""
    try:
        camera_id = request.json.get('camera_id') if request.json else None

        # Find all approved non-interpolation predictions with valid timestamps
        from db_connection import get_cursor
        with get_cursor(commit=False) as cur:
            conditions = [
                "p.review_status IN ('approved', 'auto_approved')",
                "p.timestamp IS NOT NULL",
                "p.bbox_x IS NOT NULL",
                "p.bbox_width > 0",
            ]
            params = []
            if camera_id:
                conditions.append("v.camera_id = %s")
                params.append(camera_id)

            query = """
                SELECT p.id, p.video_id, p.timestamp, p.scenario,
                       p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
                       p.predicted_tags, p.corrected_tags,
                       v.camera_id
                FROM ai_predictions p
                JOIN videos v ON p.video_id = v.id
                WHERE {conditions}
                ORDER BY p.video_id, p.timestamp
            """.format(conditions=' AND '.join(conditions))

            if params:
                cur.execute(query, params)
            else:
                cur.execute(query)
            preds = [dict(r) for r in cur.fetchall()]

        if not preds:
            return jsonify({'success': True, 'message': 'No approved predictions found', 'triggered': 0})

        # Group by (video_id, class_name)
        from collections import defaultdict
        groups = defaultdict(list)
        for p in preds:
            tags = p.get('corrected_tags') or p.get('predicted_tags') or {}
            if isinstance(tags, str):
                tags = json.loads(tags)
            # Skip interpolation-generated predictions
            if tags.get('source') == 'interpolation':
                continue
            class_name = tags.get('class')
            if not class_name:
                continue
            groups[(p['video_id'], class_name)].append(p)

        triggered = 0
        skipped = 0

        for (video_id, class_name), group_preds in groups.items():
            # Sort by timestamp
            group_preds.sort(key=lambda p: float(p['timestamp'] or 0))

            # Check consecutive pairs
            for i in range(len(group_preds) - 1):
                pred_a = group_preds[i]
                pred_b = group_preds[i + 1]

                gap = float(pred_b['timestamp'] or 0) - float(pred_a['timestamp'] or 0)
                if gap < 2.0 or gap > 120.0:
                    continue

                # Check no track already exists
                if db.interpolation_track_exists(pred_a['id'], pred_b['id']):
                    skipped += 1
                    continue

                # Trigger in background
                from interpolation_runner import run_guided_interpolation
                threading.Thread(
                    target=run_guided_interpolation,
                    args=(video_id, pred_a['id'], pred_b['id']),
                    daemon=True,
                    name=f"interp-scan-{pred_a['id']}-{pred_b['id']}"
                ).start()
                triggered += 1

        return jsonify({
            'success': True,
            'triggered': triggered,
            'skipped_existing': skipped,
            'groups_scanned': len(groups),
        })
    except Exception as e:
        logger.error("Interpolation scan error: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@tracks_bp.route('/api/interpolation/tracks', methods=['GET'])
def get_interpolation_tracks():
    """List interpolation tracks, optionally filtered."""
    try:
        video_id = request.args.get('video_id', type=int)
        status = request.args.get('status')
        limit = request.args.get('limit', type=int, default=50)
        offset = request.args.get('offset', type=int, default=0)

        result = db.get_interpolation_tracks(
            video_id=video_id,
            status=status,
            limit=limit,
            offset=offset
        )

        # Ensure numeric types are JSON-serializable
        for t in result['tracks']:
            for key in ('start_timestamp', 'end_timestamp', 'frame_interval'):
                if key in t and t[key] is not None:
                    t[key] = float(t[key])

        return jsonify({
            'success': True,
            'tracks': result['tracks'],
            'total': result['total'],
            'has_more': result['has_more']
        })
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
        reviewer = data.get('reviewer') or request.headers.get('X-Auth-User', 'anonymous')

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
