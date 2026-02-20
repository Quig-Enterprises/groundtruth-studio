"""Clip Analysis routes for Groundtruth Studio."""
from flask import Blueprint, request, jsonify, render_template, send_from_directory, g, Response
from db_connection import get_cursor
import os
import json
import logging
import threading
import time
from pathlib import Path

clip_analysis_bp = Blueprint('clip_analysis', __name__)
logger = logging.getLogger(__name__)

CLIPS_DIR = Path('/opt/groundtruth-studio/clips')
CROPS_DIR = CLIPS_DIR / 'crops'
THUMBNAILS_DIR = Path('/opt/groundtruth-studio/thumbnails')
THUMB_CACHE_DIR = THUMBNAILS_DIR / 'small'

# Ensure directories exist
CLIPS_DIR.mkdir(exist_ok=True)
CROPS_DIR.mkdir(exist_ok=True)
THUMB_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# In-memory status tracking for background analyses
_analysis_status = {}


# ---- Page Route ----

@clip_analysis_bp.route('/clip-analysis')
def clip_analysis_page():
    """Render the clip analysis page."""
    return render_template('clip_analysis.html')


# ---- API Routes ----

@clip_analysis_bp.route('/api/clip-analysis/run', methods=['POST'])
def run_clip_analysis_api():
    """Start clip analysis in background thread.

    Accepts JSON body:
        source_type: 'frigate' or 'ecoeye'
        source_id: video id (frigate) or ecoeye_alerts id
        camera_id: optional override
    Or legacy fields: video_id, frigate_event_id, ecoeye_alert_id, clip_path
    """
    if not g.can_write:
        return jsonify({'success': False, 'error': 'Write access required'}), 403

    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'JSON body required'}), 400

        # Support both new (source_type/source_id) and legacy field formats
        source_type = data.get('source_type')
        source_id = data.get('source_id')
        camera_id = data.get('camera_id')
        mode = data.get('mode', 'local')

        # Map source_type/source_id to resolve_clip_source params
        video_id = data.get('video_id')
        frigate_event_id = data.get('frigate_event_id')
        ecoeye_alert_id = data.get('ecoeye_alert_id')
        clip_path = data.get('clip_path')

        if source_type and source_id:
            if source_type == 'frigate':
                video_id = source_id
                # Look up frigate_event_id from video metadata so we fetch the clip via Frigate API
                if not frigate_event_id:
                    with get_cursor() as cur:
                        cur.execute(
                            "SELECT metadata->>'frigate_event_id' as feid FROM videos WHERE id = %s",
                            (source_id,)
                        )
                        frow = cur.fetchone()
                        if frow and frow['feid']:
                            frigate_event_id = frow['feid']
            elif source_type == 'ecoeye':
                ecoeye_alert_id = source_id

        if not (video_id or frigate_event_id or ecoeye_alert_id or clip_path):
            return jsonify({'success': False, 'error': 'No clip source specified'}), 400

        # Import lazily to avoid circular dependencies
        from clip_analysis import run_clip_analysis, resolve_clip_source

        # Resolve the clip source — when frigate_event_id is set, that path fetches
        # the actual MP4 clip from Frigate API; video_id is passed along for metadata
        resolved = resolve_clip_source(
            video_id=video_id,
            frigate_event_id=frigate_event_id,
            ecoeye_alert_id=ecoeye_alert_id,
            clip_path=clip_path
        )

        if not resolved:
            return jsonify({'success': False, 'error': 'Could not resolve clip source'}), 400

        # Extract values from resolved dict
        resolved_clip_path = resolved['clip_path']
        resolved_video_id = resolved['video_id']
        resolved_camera_id = camera_id or resolved['camera_id']

        if mode == 'remote':
            from services import training_queue
            result = training_queue.submit_job(
                export_path=os.path.dirname(resolved_clip_path),
                job_type='clip_analysis',
                config={
                    'video_id': resolved_video_id,
                    'camera_id': resolved_camera_id,
                    'clip_path': resolved_clip_path
                }
            )
            _analysis_status[resolved_video_id] = {
                'status': 'processing',
                'started_at': time.time(),
                'job_id': result['job_id'],
                'mode': 'remote'
            }
            logger.info(f"Queued remote clip analysis for video_id={resolved_video_id}, job_id={result['job_id']}")
            return jsonify({
                'success': True,
                'message': 'Analysis queued for remote worker',
                'analysis_id': resolved_video_id,
                'video_id': resolved_video_id,
                'camera_id': resolved_camera_id,
                'job_id': result['job_id'],
                'mode': 'remote'
            })
        else:
            # Start background thread with error tracking
            _analysis_status[resolved_video_id] = {'status': 'processing', 'started_at': time.time()}

            def _run_wrapper():
                try:
                    result = run_clip_analysis(resolved_video_id, resolved_camera_id, resolved_clip_path)
                    if result:
                        _analysis_status[resolved_video_id] = {'status': 'completed'}
                    else:
                        _analysis_status[resolved_video_id] = {'status': 'failed', 'error': 'Analysis produced no results'}
                except Exception as exc:
                    logger.error("Background clip analysis failed for video %d: %s", resolved_video_id, exc, exc_info=True)
                    _analysis_status[resolved_video_id] = {'status': 'failed', 'error': str(exc)}

            thread = threading.Thread(target=_run_wrapper, daemon=True)
            thread.start()

            logger.info(f"Started clip analysis for video_id={resolved_video_id}, camera_id={resolved_camera_id}")

            return jsonify({
                'success': True,
                'message': 'Analysis started',
                'analysis_id': resolved_video_id,
                'video_id': resolved_video_id,
                'camera_id': resolved_camera_id
            })

    except Exception as e:
        logger.error(f"Error starting clip analysis: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@clip_analysis_bp.route('/api/clip-analysis/<int:video_id>/job-status', methods=['GET'])
def get_clip_analysis_job_status(video_id):
    """Check status of remote clip analysis jobs for a video."""
    try:
        # Check in-memory status first
        mem_status = _analysis_status.get(video_id)
        if mem_status and mem_status.get('mode') == 'remote' and mem_status.get('job_id'):
            from services import training_queue
            job = training_queue.get_job(mem_status['job_id'])
            if job:
                return jsonify({
                    'success': True,
                    'job_id': job['job_id'],
                    'status': job['status'],
                    'error': job.get('error_message'),
                    'submitted_at': job['submitted_at'].isoformat() if job.get('submitted_at') else None,
                    'completed_at': job['completed_at'].isoformat() if job.get('completed_at') else None,
                })

        # Also check training_jobs table directly for clip_analysis jobs for this video
        from db_connection import get_connection
        from psycopg2 import extras as pg_extras
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=pg_extras.RealDictCursor)
            cursor.execute("""
                SELECT job_id, status, error_message, submitted_at, completed_at
                FROM training_jobs
                WHERE job_type = 'clip_analysis'
                AND config_json::jsonb->>'video_id' = %s
                ORDER BY submitted_at DESC
                LIMIT 1
            """, (str(video_id),))
            row = cursor.fetchone()

        if row:
            return jsonify({
                'success': True,
                'job_id': row['job_id'],
                'status': row['status'],
                'error': row.get('error_message'),
                'submitted_at': row['submitted_at'].isoformat() if row.get('submitted_at') else None,
                'completed_at': row['completed_at'].isoformat() if row.get('completed_at') else None,
            })

        return jsonify({'success': True, 'status': None, 'message': 'No remote jobs found for this video'})

    except Exception as e:
        logger.error(f"Error getting job status for video {video_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@clip_analysis_bp.route('/api/clip-analysis/<int:video_id>/reanalyze', methods=['POST'])
def reanalyze_clip(video_id):
    """Delete existing analysis results and re-run clip analysis for a video.

    Clears clip_analysis_results and resets video_tracks to 'active' so the
    full pipeline (tracking + classification) runs again with any new
    post-processing steps (e.g. jump cleanup).
    """
    if not g.can_write:
        return jsonify({'success': False, 'error': 'Write access required'}), 403

    try:
        # Look up video info
        with get_cursor(commit=False) as cur:
            cur.execute("SELECT id, filename, camera_id, metadata FROM videos WHERE id = %s", (video_id,))
            video = cur.fetchone()

        if not video:
            return jsonify({'success': False, 'error': 'Video not found'}), 404

        # Delete existing analysis results and tracks for full re-tracking
        with get_cursor(commit=True) as cur:
            cur.execute("DELETE FROM clip_analysis_results WHERE video_id = %s", (video_id,))
            deleted_results = cur.rowcount
            cur.execute("DELETE FROM video_tracks WHERE video_id = %s", (video_id,))
            deleted_tracks = cur.rowcount

        logger.info(
            "Reanalyze video %d: deleted %d results and %d tracks",
            video_id, deleted_results, deleted_tracks,
        )

        # Resolve clip source and start analysis
        from clip_analysis import run_clip_analysis, resolve_clip_source

        # For Frigate-sourced videos, use the event ID to re-fetch the clip
        metadata = video.get('metadata') or {}
        if isinstance(metadata, str):
            import json as _json
            metadata = _json.loads(metadata)
        frigate_event_id = metadata.get('frigate_event_id')
        ecoeye_alert_id = metadata.get('ecoeye_alert_id')

        if frigate_event_id:
            resolved = resolve_clip_source(video_id=video_id, frigate_event_id=frigate_event_id)
        elif ecoeye_alert_id:
            resolved = resolve_clip_source(video_id=video_id, ecoeye_alert_id=ecoeye_alert_id)
        else:
            resolved = resolve_clip_source(video_id=video_id)

        if not resolved:
            return jsonify({'success': False, 'error': 'Could not resolve clip source'}), 400

        resolved_clip_path = resolved['clip_path']
        resolved_camera_id = resolved['camera_id'] or video['camera_id']

        # Run in background thread
        _analysis_status[video_id] = {'status': 'processing', 'started_at': time.time()}

        def _run_wrapper():
            try:
                result = run_clip_analysis(video_id, resolved_camera_id, resolved_clip_path)
                if result:
                    _analysis_status[video_id] = {'status': 'completed'}
                else:
                    _analysis_status[video_id] = {'status': 'failed', 'error': 'Analysis produced no results'}
            except Exception as exc:
                logger.error("Reanalysis failed for video %d: %s", video_id, exc, exc_info=True)
                _analysis_status[video_id] = {'status': 'failed', 'error': str(exc)}

        thread = threading.Thread(target=_run_wrapper, daemon=True)
        thread.start()

        return jsonify({
            'success': True,
            'message': f'Re-analysis started (cleared {deleted_results} results, {deleted_tracks} tracks)',
            'video_id': video_id,
        })

    except Exception as e:
        logger.error(f"Error reanalyzing video {video_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@clip_analysis_bp.route('/api/clip-analysis/list', methods=['GET'])
def list_clip_analyses():
    """Get paginated list of clip analyses grouped by video.

    Each item represents one video with aggregate track/review stats and
    a computed workflow_status:
        processing    — AI analysis still running
        failed        — AI analysis failed
        needs_review  — AI done, human review incomplete
        review_complete — all tracks reviewed/approved
    """
    try:
        camera_id = request.args.get('camera_id')
        workflow = request.args.get('workflow_status')
        limit = request.args.get('limit', 50, type=int)
        offset = request.args.get('offset', 0, type=int)

        with get_cursor() as cur:
            where_clauses = []
            params = []

            if camera_id:
                where_clauses.append('car.camera_id = %s')
                params.append(camera_id)

            where_sql = 'WHERE ' + ' AND '.join(where_clauses) if where_clauses else ''

            cur.execute(f"""
                SELECT car.video_id,
                       car.camera_id,
                       v.title as video_title,
                       MIN(car.created_at) as created_at,
                       COUNT(*) as track_count,
                       COUNT(*) FILTER (WHERE car.status = 'completed') as completed_count,
                       COUNT(*) FILTER (WHERE car.review_status IN ('approved', 'reviewed', 'corrected', 'flagged')) as reviewed_count
                FROM clip_analysis_results car
                LEFT JOIN videos v ON car.video_id = v.id
                {where_sql}
                GROUP BY car.video_id, car.camera_id, v.title
                ORDER BY MIN(car.created_at) DESC
                LIMIT %s OFFSET %s
            """, params + [limit, offset])

            rows = cur.fetchall()

            # Count total groups
            cur.execute(f"""
                SELECT COUNT(DISTINCT video_id) as total
                FROM clip_analysis_results car
                {where_sql}
            """, params)
            total = cur.fetchone()['total']

        analyses = []
        for row in rows:
            row = dict(row)
            track_count = row['track_count']
            completed = row['completed_count']
            reviewed = row['reviewed_count']

            # Check in-memory status for actively processing videos
            mem_status = _analysis_status.get(row['video_id'])

            if mem_status and mem_status['status'] == 'processing':
                wf_status = 'processing'
            elif mem_status and mem_status['status'] == 'failed':
                wf_status = 'failed'
            elif completed < track_count:
                wf_status = 'processing'
            elif reviewed >= track_count:
                wf_status = 'review_complete'
            else:
                wf_status = 'needs_review'

            # Apply workflow filter if requested
            if workflow and wf_status != workflow:
                continue

            analyses.append({
                'id': row['video_id'],
                'video_id': row['video_id'],
                'camera_id': row['camera_id'],
                'label': row['video_title'] or f"Video #{row['video_id']}",
                'created_at': row['created_at'].isoformat() if row.get('created_at') else None,
                'track_count': track_count,
                'reviewed_count': reviewed,
                'status': 'completed' if completed == track_count else 'processing',
                'workflow_status': wf_status,
            })

        return jsonify({
            'success': True,
            'analyses': analyses,
            'total': total,
            'limit': limit,
            'offset': offset
        })

    except Exception as e:
        logger.error(f"Error listing clip analyses: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@clip_analysis_bp.route('/api/clip-analysis/<int:analysis_id>', methods=['GET'])
def get_clip_analysis_detail(analysis_id):
    """Get full detail for a specific clip analysis."""
    try:
        with get_cursor() as cur:
            # First try by clip_analysis_results.id
            cur.execute("""
                SELECT car.*, vt.tracker_track_id, vt.best_crop_path
                FROM clip_analysis_results car
                LEFT JOIN video_tracks vt ON car.video_track_id = vt.id
                WHERE car.id = %s
            """, (analysis_id,))
            row = cur.fetchone()

            # If not found, try by video_id (for polling after run endpoint)
            if not row:
                cur.execute("""
                    SELECT car.*, vt.tracker_track_id, vt.best_crop_path
                    FROM clip_analysis_results car
                    LEFT JOIN video_tracks vt ON car.video_track_id = vt.id
                    WHERE car.video_id = %s
                    ORDER BY car.created_at DESC
                    LIMIT 1
                """, (analysis_id,))
                row = cur.fetchone()

            if not row:
                # Check in-memory status for running/failed analyses
                status_info = _analysis_status.get(analysis_id)
                if status_info:
                    return jsonify({
                        'success': True,
                        'status': status_info['status'],
                        'error': status_info.get('error'),
                        'video_id': analysis_id
                    })
                return jsonify({'success': False, 'error': 'Analysis not found'}), 404

            result = dict(row)

            # Convert datetime to ISO format
            if result.get('created_at'):
                result['created_at'] = result['created_at'].isoformat()
            if result.get('updated_at'):
                result['updated_at'] = result['updated_at'].isoformat()

        # Spread result at top level so JS can access data.status directly
        response = {'success': True}
        response.update(result)
        return jsonify(response)

    except Exception as e:
        logger.error(f"Error getting clip analysis detail: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@clip_analysis_bp.route('/api/clip-analysis/<int:analysis_id>', methods=['DELETE'])
def delete_clip_analysis(analysis_id):
    """Delete a clip analysis and associated predictions."""
    if not g.can_write:
        return jsonify({'success': False, 'error': 'Write access required'}), 403

    try:
        with get_cursor(commit=True) as cur:
            # Delete associated AI predictions (JSONB query)
            cur.execute("""
                DELETE FROM ai_predictions
                WHERE predicted_tags->>'analysis_id' = %s
            """, (str(analysis_id),))

            # Delete the analysis record
            cur.execute("""
                DELETE FROM clip_analysis_results
                WHERE id = %s
            """, (analysis_id,))

            if cur.rowcount == 0:
                return jsonify({'success': False, 'error': 'Analysis not found'}), 404

        return jsonify({'success': True})

    except Exception as e:
        logger.error(f"Error deleting clip analysis: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@clip_analysis_bp.route('/api/clip-analysis/sources', methods=['GET'])
def get_clip_sources():
    """List available clip sources (Frigate events, EcoEye alerts).

    Query params:
        type: 'frigate' or 'ecoeye'
        camera_id: optional filter
    Returns: {success, sources: [{id, label, camera_id, timestamp}], cameras: []}
    """
    try:
        source_type = request.args.get('type', 'frigate')
        camera_id = request.args.get('camera_id')

        sources = []
        cameras = set()

        with get_cursor() as cur:
            if source_type == 'frigate':
                sql = """
                    SELECT v.id, v.title, v.filename, v.upload_date, v.camera_id,
                           v.metadata->>'frigate_event_id' as frigate_event_id
                    FROM videos v
                    WHERE v.metadata->>'frigate_event_id' IS NOT NULL
                    AND v.upload_date > NOW() - INTERVAL '7 days'
                """
                params = []
                if camera_id:
                    sql += " AND v.camera_id = %s"
                    params.append(camera_id)
                sql += " ORDER BY v.upload_date DESC LIMIT 100"

                cur.execute(sql, params)
                frigate_rows = cur.fetchall()

                # Batch-fetch analysis status for all video IDs
                video_ids = [row['id'] for row in frigate_rows]
                analysis_map = {}
                if video_ids:
                    cur.execute("""
                        SELECT video_id,
                               COUNT(*) as track_count,
                               COUNT(*) FILTER (WHERE review_status IN ('approved', 'reviewed', 'corrected', 'flagged')) as reviewed_count
                        FROM clip_analysis_results
                        WHERE video_id = ANY(%s)
                        GROUP BY video_id
                    """, (video_ids,))
                    for arow in cur.fetchall():
                        analysis_map[arow['video_id']] = dict(arow)

                for row in frigate_rows:
                    cameras.add(row['camera_id'])
                    # Thumbnail — use resized small version for fast loading
                    thumb = None
                    if row.get('filename') and row['filename'].lower().endswith('.jpg'):
                        thumb = '/api/clip-analysis/thumb/' + row['filename']

                    # Analysis/review status
                    astats = analysis_map.get(row['id'])
                    if astats:
                        tc = astats['track_count']
                        rc = astats['reviewed_count']
                        if rc >= tc:
                            wf_status = 'review_complete'
                        else:
                            wf_status = 'needs_review'
                    else:
                        mem = _analysis_status.get(row['id'])
                        if mem and mem['status'] == 'processing':
                            wf_status = 'processing'
                        else:
                            wf_status = None

                    sources.append({
                        'id': row['id'],
                        'label': row['title'] or f"Frigate #{row['id']}",
                        'camera_id': row['camera_id'],
                        'timestamp': row['upload_date'].isoformat() if row.get('upload_date') else None,
                        'frigate_event_id': row['frigate_event_id'],
                        'thumbnail': thumb,
                        'workflow_status': wf_status,
                        'track_count': astats['track_count'] if astats else None,
                        'reviewed_count': astats['reviewed_count'] if astats else None,
                    })

            elif source_type == 'ecoeye':
                sql = """
                    SELECT id, alert_id, alert_type, timestamp, camera_id
                    FROM ecoeye_alerts
                    WHERE video_downloaded = TRUE
                    AND timestamp > NOW() - INTERVAL '7 days'
                """
                params = []
                if camera_id:
                    sql += " AND camera_id = %s"
                    params.append(camera_id)
                sql += " ORDER BY timestamp DESC LIMIT 100"

                cur.execute(sql, params)
                for row in cur.fetchall():
                    cameras.add(row['camera_id'])
                    sources.append({
                        'id': row['id'],
                        'label': f"{row['alert_type'] or 'Alert'} - {row['alert_id']}",
                        'camera_id': row['camera_id'],
                        'timestamp': row['timestamp'].isoformat() if row.get('timestamp') else None,
                    })

        return jsonify({
            'success': True,
            'sources': sources,
            'cameras': sorted(c for c in cameras if c)
        })

    except Exception as e:
        logger.error(f"Error getting clip sources: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@clip_analysis_bp.route('/api/clip-analysis/upload', methods=['POST'])
def upload_clip():
    """Upload an MP4 clip for analysis."""
    if not g.can_write:
        return jsonify({'success': False, 'error': 'Write access required'}), 403

    try:
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'No file provided'}), 400

        file = request.files['file']
        camera_id = request.form.get('camera_id', type=int)

        if not camera_id:
            return jsonify({'success': False, 'error': 'camera_id required'}), 400

        if file.filename == '':
            return jsonify({'success': False, 'error': 'Empty filename'}), 400

        if not file.filename.lower().endswith('.mp4'):
            return jsonify({'success': False, 'error': 'Only MP4 files allowed'}), 400

        # Save file
        from werkzeug.utils import secure_filename
        filename = secure_filename(file.filename)
        timestamp = int(time.time())
        filename = f"{timestamp}_{filename}"
        filepath = CLIPS_DIR / filename

        file.save(str(filepath))
        logger.info(f"Saved uploaded clip to {filepath}")

        # Create video record
        with get_cursor(commit=True) as cur:
            cur.execute("""
                INSERT INTO videos (filename, title, camera_id, original_url)
                VALUES (%s, %s, %s, %s)
                RETURNING id
            """, (str(filepath), filename, camera_id, f'upload://{filename}'))

            video_id = cur.fetchone()['id']

        return jsonify({
            'success': True,
            'video_id': video_id,
            'clip_path': str(filepath)
        })

    except Exception as e:
        logger.error(f"Error uploading clip: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@clip_analysis_bp.route('/api/clip-analysis/<int:analysis_id>/tracks', methods=['GET'])
def get_analysis_tracks(analysis_id):
    """Get all tracks with consensus for a video."""
    try:
        with get_cursor() as cur:
            # First try by clip_analysis_results.id
            cur.execute("""
                SELECT video_id FROM clip_analysis_results WHERE id = %s
            """, (analysis_id,))
            row = cur.fetchone()
            if not row:
                # Try treating analysis_id as video_id directly
                cur.execute("SELECT id FROM videos WHERE id = %s", (analysis_id,))
                vrow = cur.fetchone()
                if vrow:
                    video_id = analysis_id
                else:
                    return jsonify({'success': False, 'error': 'Analysis not found'}), 404
            else:
                video_id = row['video_id']

            # Get all tracks for this video, including trajectory for bbox overlay
            cur.execute("""
                SELECT car.id, car.video_track_id, car.consensus_class,
                       car.consensus_confidence, car.class_distribution,
                       car.total_frames, car.review_status, car.issue_reason,
                       vt.trajectory, vt.best_crop_path
                FROM clip_analysis_results car
                LEFT JOIN video_tracks vt ON car.video_track_id = vt.id
                WHERE car.video_id = %s
                ORDER BY car.video_track_id
            """, (video_id,))

            tracks = []
            for row in cur.fetchall():
                t = dict(row)
                # Map fields to what JS expects
                t['confidence'] = t['consensus_confidence']
                t['frame_count'] = t['total_frames']
                # Build crop URL
                if t.get('best_crop_path'):
                    t['crop_url'] = f"/api/clip-analysis/{analysis_id}/tracks/{t['id']}/crop"
                else:
                    t['crop_url'] = None
                # Parse trajectory if it's a string
                if isinstance(t.get('trajectory'), str):
                    t['trajectory'] = json.loads(t['trajectory'])
                t['has_issue'] = bool(t.get('issue_reason'))
                tracks.append(t)

        return jsonify({
            'success': True,
            'tracks': tracks
        })

    except Exception as e:
        logger.error(f"Error getting analysis tracks: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@clip_analysis_bp.route('/api/clip-analysis/<int:analysis_id>/tracks/<int:track_id>/timeline', methods=['GET'])
def get_track_timeline(analysis_id, track_id):
    """Get frame-by-frame timeline data for a track."""
    try:
        with get_cursor() as cur:
            # track_id from URL is clip_analysis_results.id (what JS track.id holds)
            cur.execute("""
                SELECT frame_classifications
                FROM clip_analysis_results
                WHERE id = %s
            """, (track_id,))

            row = cur.fetchone()
            if not row:
                return jsonify({'success': False, 'error': 'Track not found'}), 404

            frame_data = row['frame_classifications'] or []
            if isinstance(frame_data, str):
                frame_data = json.loads(frame_data)

        # Transform per-frame data into segments (merge consecutive same-class frames)
        segments = []
        if frame_data:
            frame_data_sorted = sorted(frame_data, key=lambda f: f['timestamp'])
            current_seg = None
            for fc in frame_data_sorted:
                if current_seg and current_seg['class_name'] == fc['class_name']:
                    current_seg['end'] = fc['timestamp']
                    current_seg['_weights'].append(fc.get('confidence', 0.5))
                else:
                    if current_seg:
                        current_seg['weight'] = sum(current_seg['_weights']) / len(current_seg['_weights'])
                        del current_seg['_weights']
                        segments.append(current_seg)
                    current_seg = {
                        'start': fc['timestamp'],
                        'end': fc['timestamp'],
                        'class_name': fc['class_name'],
                        '_weights': [fc.get('confidence', 0.5)]
                    }
            if current_seg:
                current_seg['weight'] = sum(current_seg['_weights']) / len(current_seg['_weights'])
                del current_seg['_weights']
                segments.append(current_seg)

        return jsonify({
            'success': True,
            'segments': segments
        })

    except Exception as e:
        logger.error(f"Error getting track timeline: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@clip_analysis_bp.route('/api/clip-analysis/<int:analysis_id>/tracks/<int:track_id>/reclassify', methods=['POST'])
def reclassify_track(analysis_id, track_id):
    """Override consensus classification for a track."""
    if not g.can_write:
        return jsonify({'success': False, 'error': 'Write access required'}), 403

    try:
        data = request.get_json()
        new_class = data.get('new_class') or data.get('class_name')

        if not new_class:
            return jsonify({'success': False, 'error': 'new_class required'}), 400

        with get_cursor(commit=True) as cur:
            # track_id from URL is clip_analysis_results.id (what JS track.id holds)
            cur.execute("""
                UPDATE clip_analysis_results
                SET consensus_class = %s,
                    review_status = 'reviewed',
                    reviewed_by = 'manual'
                WHERE id = %s
            """, (new_class, track_id))

            if cur.rowcount == 0:
                return jsonify({'success': False, 'error': 'Track not found'}), 404

        return jsonify({'success': True})

    except Exception as e:
        logger.error(f"Error reclassifying track: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@clip_analysis_bp.route('/api/clip-analysis/<int:analysis_id>/approve', methods=['POST'])
def approve_tracks(analysis_id):
    """Batch approve selected tracks (confirm predictions are correct)."""
    if not g.can_write:
        return jsonify({'success': False, 'error': 'Write access required'}), 403

    try:
        data = request.get_json()
        track_ids = data.get('track_ids', [])

        if not track_ids:
            return jsonify({'success': False, 'error': 'track_ids required'}), 400

        with get_cursor(commit=True) as cur:
            cur.execute("""
                UPDATE clip_analysis_results
                SET review_status = 'approved',
                    reviewed_by = 'manual'
                WHERE id = ANY(%s)
            """, (track_ids,))

            approved_count = cur.rowcount

        return jsonify({
            'success': True,
            'approved_count': approved_count
        })

    except Exception as e:
        logger.error(f"Error approving tracks: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@clip_analysis_bp.route('/api/clip-analysis/<int:analysis_id>/export-training', methods=['GET', 'POST'])
def export_training_frames(analysis_id):
    """Export top-quality frames for training data.

    GET: Preview which frames would be exported.
    POST: Actually export frames as ai_predictions.
    """
    if request.method == 'POST' and not g.can_write:
        return jsonify({'success': False, 'error': 'Write access required'}), 403

    try:
        if request.method == 'GET':
            # Preview mode
            top_n = request.args.get('top_n', 10, type=int)
            min_quality = request.args.get('min_quality', 0.5, type=float)
            track_ids_str = request.args.get('track_ids', '')

            with get_cursor() as cur:
                cur.execute("""
                    SELECT id, video_track_id, consensus_class, frame_quality_scores
                    FROM clip_analysis_results
                    WHERE video_id = (SELECT video_id FROM clip_analysis_results WHERE id = %s LIMIT 1)
                """, (analysis_id,))
                rows = cur.fetchall()

            if not rows:
                return jsonify({'success': True, 'frames': []})

            # Filter by track_ids if specified
            track_ids = set()
            if track_ids_str:
                for tid in track_ids_str.split(','):
                    tid = tid.strip()
                    if tid.isdigit():
                        track_ids.add(int(tid))

            frames = []
            for row in rows:
                row = dict(row)
                if track_ids and row['video_track_id'] not in track_ids:
                    continue
                quality_scores = row['frame_quality_scores']
                if isinstance(quality_scores, str):
                    quality_scores = json.loads(quality_scores)
                if not quality_scores:
                    continue
                for qs in quality_scores:
                    if qs.get('quality_score', 0) >= min_quality:
                        frames.append({
                            'class_name': row['consensus_class'],
                            'quality': qs['quality_score'],
                            'timestamp': qs.get('timestamp'),
                            'image_url': f"/api/clip-analysis/{analysis_id}/tracks/{row['video_track_id']}/crop"
                        })

            # Sort by quality descending, limit to top_n
            frames.sort(key=lambda f: f['quality'], reverse=True)
            frames = frames[:top_n]

            return jsonify({'success': True, 'frames': frames})

        else:
            # POST: actual export (existing behavior)
            data = request.get_json() or {}
            track_ids = data.get('track_ids')
            top_n = data.get('top_n', 10)
            min_quality = data.get('min_quality', 0.5)

            # Import lazily
            from clip_analysis import export_training_frames as export_fn

            result = export_fn(
                analysis_id=analysis_id,
                top_n=top_n,
                min_quality=min_quality
            )

            return jsonify({
                'success': True,
                'exported_count': result.get('count', 0),
                'batch_id': result.get('batch_id')
            })

    except Exception as e:
        logger.error(f"Error exporting training frames: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@clip_analysis_bp.route('/api/clip-analysis/export-training', methods=['POST'])
def export_training_corrections():
    """Export clip analysis corrections as YOLO training data.

    Exports three types of training signals:
    1. Hard negatives: non-vehicle reclassifications (sign, rock, false positive, etc.)
       - Frame exported WITHOUT the bbox → teaches model to suppress these detections
    2. Bbox corrections: tracks with corrected_bbox
       - Frame exported WITH corrected bbox → fixes localization errors
    3. Positive confirmations: approved tracks
       - Frame exported WITH original bbox → reinforces correct detections

    Returns: {success, exported: {hard_negatives, bbox_corrections, positives}, batch_id}
    """
    if not g.can_write:
        return jsonify({'success': False, 'error': 'Write access required'}), 403

    try:
        data = request.get_json() or {}
        # Optional filters
        video_ids = data.get('video_ids')  # limit to specific videos

        NON_VEHICLE_LABELS = {
            'sign', 'rock', 'transformer', 'false positive', 'flag',
            'shadow', 'reflection', 'glare', 'building', 'shed',
            'fence', 'gate', 'pole', 'tree', 'stump', 'bush',
            'vegetation', 'unknown', 'person', 'animal'
        }

        hard_negatives = []
        bbox_corrections = []
        positives = []

        with get_cursor() as cur:
            sql = """
                SELECT car.id, car.video_id, car.video_track_id,
                       car.consensus_class, car.review_status,
                       car.corrected_bbox, car.issue_reason,
                       car.consensus_confidence,
                       vt.trajectory, vt.best_crop_path,
                       v.filename, v.camera_id
                FROM clip_analysis_results car
                JOIN video_tracks vt ON car.video_track_id = vt.id
                JOIN videos v ON car.video_id = v.id
                WHERE car.review_status IN ('approved', 'reviewed', 'corrected')
            """
            params = []
            if video_ids:
                sql += " AND car.video_id = ANY(%s)"
                params.append(video_ids)

            cur.execute(sql, params)
            rows = cur.fetchall()

            for row in rows:
                traj = row['trajectory']
                if isinstance(traj, str):
                    traj = json.loads(traj)
                if not traj:
                    continue

                # Get representative frame (middle of trajectory)
                mid_pt = traj[len(traj) // 2]

                entry = {
                    'car_id': row['id'],
                    'video_id': row['video_id'],
                    'camera_id': row['camera_id'],
                    'filename': row['filename'],
                    'frame_time': mid_pt['timestamp'],
                    'class': row['consensus_class'],
                }

                if row['review_status'] == 'corrected' and row.get('corrected_bbox'):
                    cb = row['corrected_bbox']
                    if isinstance(cb, str):
                        cb = json.loads(cb)
                    entry['bbox'] = {'x': cb['x'], 'y': cb['y'], 'w': cb['w'], 'h': cb['h']}
                    entry['frame_time'] = cb.get('frame_time', mid_pt['timestamp'])
                    entry['type'] = 'bbox_correction'
                    entry['reason'] = row.get('issue_reason', '')
                    bbox_corrections.append(entry)
                elif row['consensus_class'] in NON_VEHICLE_LABELS:
                    entry['original_bbox'] = {'x': mid_pt['x'], 'y': mid_pt['y'], 'w': mid_pt['w'], 'h': mid_pt['h']}
                    entry['type'] = 'hard_negative'
                    hard_negatives.append(entry)
                elif row['review_status'] == 'approved':
                    entry['bbox'] = {'x': mid_pt['x'], 'y': mid_pt['y'], 'w': mid_pt['w'], 'h': mid_pt['h']}
                    entry['type'] = 'positive'
                    positives.append(entry)

        batch_id = f"clip-corrections-{int(time.time())}"

        return jsonify({
            'success': True,
            'batch_id': batch_id,
            'exported': {
                'hard_negatives': len(hard_negatives),
                'bbox_corrections': len(bbox_corrections),
                'positives': len(positives),
                'total': len(hard_negatives) + len(bbox_corrections) + len(positives)
            },
            'items': hard_negatives + bbox_corrections + positives
        })

    except Exception as e:
        logger.error(f"Error exporting training corrections: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@clip_analysis_bp.route('/api/clip-analysis/<int:analysis_id>/tracks/<int:track_id>/report-issue', methods=['POST'])
def report_track_issue(analysis_id, track_id):
    """Report an issue with a track and optionally provide corrected bbox."""
    if not g.can_write:
        return jsonify({'success': False, 'error': 'Write access required'}), 403

    try:
        data = request.get_json()
        reason = data.get('reason', '')
        corrected_bbox = data.get('corrected_bbox')  # {x, y, w, h} in video coords
        frame_time = data.get('frame_time')

        if not reason and not corrected_bbox:
            return jsonify({'success': False, 'error': 'reason or corrected_bbox required'}), 400

        with get_cursor(commit=True) as cur:
            if corrected_bbox:
                cur.execute("""
                    UPDATE clip_analysis_results
                    SET issue_reason = %s,
                        corrected_bbox = %s,
                        review_status = 'corrected'
                    WHERE id = %s
                """, (reason, json.dumps({
                    'x': corrected_bbox['x'],
                    'y': corrected_bbox['y'],
                    'w': corrected_bbox['w'],
                    'h': corrected_bbox['h'],
                    'frame_time': frame_time
                }), track_id))
            else:
                cur.execute("""
                    UPDATE clip_analysis_results
                    SET issue_reason = %s,
                        review_status = 'flagged'
                    WHERE id = %s
                """, (reason, track_id))

            if cur.rowcount == 0:
                return jsonify({'success': False, 'error': 'Track not found'}), 404

        return jsonify({'success': True})

    except Exception as e:
        logger.error(f"Error reporting track issue: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@clip_analysis_bp.route('/api/clip-analysis/<int:analysis_id>/clip', methods=['GET'])
def serve_analysis_clip(analysis_id):
    """Serve the video clip for an analysis."""
    try:
        with get_cursor() as cur:
            # Resolve video_id: try clip_analysis_results.id first, then treat as video_id
            cur.execute("""
                SELECT car.video_id
                FROM clip_analysis_results car
                WHERE car.id = %s
            """, (analysis_id,))
            row = cur.fetchone()
            video_id = row['video_id'] if row else analysis_id

            # Get video info including frigate_event_id for clip path resolution
            cur.execute("""
                SELECT filename, metadata->>'frigate_event_id' as frigate_event_id
                FROM videos WHERE id = %s
            """, (video_id,))
            vrow = cur.fetchone()

            if not vrow:
                return jsonify({'success': False, 'error': 'Video not found'}), 404

            # Try to find the MP4 clip: Frigate clips are stored as frigate_{event_id}.mp4
            video_path = None
            if vrow.get('frigate_event_id'):
                clip_file = CLIPS_DIR / f"frigate_{vrow['frigate_event_id']}.mp4"
                if clip_file.exists():
                    video_path = clip_file

            # Fall back to filename from videos table
            if not video_path:
                candidate = Path(vrow['filename'])
                if candidate.exists():
                    video_path = candidate
                # Try in clips/ and downloads/
                for search_dir in [CLIPS_DIR, Path('/opt/groundtruth-studio/downloads')]:
                    candidate = search_dir / vrow['filename']
                    if candidate.exists():
                        video_path = candidate
                        break

            if not video_path or not video_path.exists():
                return jsonify({'success': False, 'error': 'Video file not found'}), 404

            return send_from_directory(
                video_path.parent,
                video_path.name,
                mimetype='video/mp4'
            )

    except Exception as e:
        logger.error(f"Error serving clip: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@clip_analysis_bp.route('/api/clip-analysis/<int:analysis_id>/tracks/<int:track_id>/crop', methods=['GET'])
def serve_track_crop(analysis_id, track_id):
    """Serve the best crop image for a track."""
    try:
        with get_cursor() as cur:
            # track_id from URL is clip_analysis_results.id (what JS track.id holds)
            cur.execute("""
                SELECT vt.best_crop_path
                FROM clip_analysis_results car
                JOIN video_tracks vt ON car.video_track_id = vt.id
                WHERE car.id = %s
            """, (track_id,))

            row = cur.fetchone()
            if not row or not row['best_crop_path']:
                return jsonify({'success': False, 'error': 'Crop not found'}), 404

            crop_path = Path(row['best_crop_path'])

            if not crop_path.exists():
                return jsonify({'success': False, 'error': 'Crop file not found'}), 404

            return send_from_directory(
                crop_path.parent,
                crop_path.name,
                mimetype='image/jpeg'
            )

    except Exception as e:
        logger.error(f"Error serving crop: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@clip_analysis_bp.route('/api/clip-analysis/thumb/<path:filename>', methods=['GET'])
def serve_small_thumbnail(filename):
    """Serve a resized thumbnail (144px wide) with disk cache."""
    try:
        # Sanitize filename
        safe_name = Path(filename).name
        cached = THUMB_CACHE_DIR / safe_name

        if cached.exists():
            return send_from_directory(THUMB_CACHE_DIR, safe_name, mimetype='image/jpeg')

        original = THUMBNAILS_DIR / safe_name
        if not original.exists():
            return jsonify({'success': False, 'error': 'Thumbnail not found'}), 404

        # Resize and cache
        from PIL import Image
        img = Image.open(original)
        img.thumbnail((288, 192), Image.LANCZOS)
        img.save(str(cached), 'JPEG', quality=70, optimize=True)

        return send_from_directory(THUMB_CACHE_DIR, safe_name, mimetype='image/jpeg')

    except Exception as e:
        logger.error(f"Error serving small thumbnail: {e}", exc_info=True)
        # Fall back to original
        return send_from_directory(THUMBNAILS_DIR, safe_name, mimetype='image/jpeg')
