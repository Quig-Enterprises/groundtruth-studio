from flask import Blueprint, request, jsonify, render_template, send_from_directory, g, Response
from psycopg2 import extras
from db_connection import get_connection
from auto_retrain import get_auto_retrain_status
import services
from services import db, yolo_exporter, training_queue, EXPORT_DIR, vibration_exporter
import os
import json
import logging
import threading
import tarfile
import io

training_bp = Blueprint('training', __name__)
logger = logging.getLogger(__name__)


@training_bp.route('/training-queue')
def training_queue_page():
    """Training queue management interface"""
    return render_template('training_queue.html')

@training_bp.route('/model-training')
def model_training_page():
    """Unified model training page"""
    return render_template('model_training.html')

# ===== Training Job Queue Endpoints =====

@training_bp.route('/api/training/submit', methods=['POST'])
def submit_training_job():
    """Submit a training job: export data, upload to S3, queue via SQS"""
    try:
        data = request.get_json()
        job_type = data.get('job_type', 'yolo-training')
        config = data.get('config', {})

        export_config_id = data.get('export_config_id')
        export_path = data.get('export_path')

        if export_config_id and not export_path:
            output_name = data.get('output_name')
            result = yolo_exporter.export_dataset(export_config_id, output_name)
            if not result.get('success'):
                return jsonify({'success': False, 'error': 'Export failed', 'details': result}), 500
            export_path = result['export_path']
            config['export_stats'] = {
                'video_count': result.get('video_count'),
                'frame_count': result.get('frame_count'),
                'annotation_count': result.get('annotation_count'),
                'class_mapping': result.get('class_mapping'),
            }

        if not export_path:
            return jsonify({'success': False, 'error': 'Either export_config_id or export_path required'}), 400

        if not os.path.isdir(export_path):
            return jsonify({'success': False, 'error': f'Export path not found: {export_path}'}), 400

        result = training_queue.submit_job(
            export_path=export_path,
            job_type=job_type,
            config=config,
            export_config_id=export_config_id
        )

        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@training_bp.route('/api/training/jobs', methods=['GET'])
def get_training_jobs():
    """List all training jobs"""
    try:
        limit = int(request.args.get('limit', 50))
        offset = int(request.args.get('offset', 0))
        export_config_id = request.args.get('export_config_id', type=int)
        jobs = training_queue.get_jobs(limit, offset, export_config_id=export_config_id)
        return jsonify({'success': True, 'jobs': jobs})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@training_bp.route('/api/training/jobs/next', methods=['GET'])
def claim_next_training_job():
    """Atomically claim the next queued training job for a worker."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
            cursor.execute('''
                UPDATE training_jobs SET status = 'processing'
                WHERE id = (
                    SELECT id FROM training_jobs
                    WHERE status = 'queued'
                    ORDER BY submitted_at
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING *
            ''')
            row = cursor.fetchone()
            conn.commit()

        if not row:
            return jsonify({'success': True, 'job': None})

        job = dict(row)
        if job.get('config_json'):
            try:
                job['config'] = json.loads(job['config_json'])
            except (json.JSONDecodeError, TypeError):
                job['config'] = {}

        job['download_url'] = f'/api/training/jobs/{job["job_id"]}/download'
        return jsonify({'success': True, 'job': job})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@training_bp.route('/api/training/jobs/<job_id>', methods=['GET'])
def get_training_job(job_id):
    """Get single training job details"""
    try:
        job = training_queue.get_job(job_id)
        if not job:
            return jsonify({'success': False, 'error': 'Job not found'}), 404
        return jsonify({'success': True, 'job': job})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@training_bp.route('/api/training/jobs/<job_id>/complete', methods=['POST'])
def complete_training_job(job_id):
    """Callback endpoint for training workers to report job completion with optional metrics"""
    try:
        data = request.get_json() or {}
        result_data = data.get('result')

        success = training_queue.complete_job(job_id, result_data)
        if not success:
            return jsonify({'success': False, 'error': 'Job not found or already completed'}), 404

        # Handle training metrics if provided
        metrics = data.get('metrics')
        if metrics:
            model_name = data.get('model_name', metrics.get('model_name', ''))
            model_version = data.get('model_version', metrics.get('model_version', ''))
            if model_name:
                try:
                    # Get the training job's DB id
                    job = training_queue.get_job(job_id)
                    job_db_id = job['id'] if job else None
                    db.insert_training_metrics(job_db_id, model_name, model_version, metrics)
                    # Auto-register model if not already in registry
                    job_type = job.get('config', {}).get('job_type', 'yolo') if job and job.get('config') else 'yolo'
                    db.get_or_create_model_registry(model_name, model_version, job_type)
                except Exception as e:
                    logger.warning(f'Failed to save training metrics for job {job_id}: {e}')

        return jsonify({'success': True, 'job_id': job_id, 'status': 'completed'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@training_bp.route('/api/training/jobs/<job_id>/fail', methods=['POST'])
def fail_training_job(job_id):
    """Report a training job failure"""
    try:
        data = request.get_json() or {}
        error_message = data.get('error', 'Unknown error')

        success = training_queue.fail_job(job_id, error_message)
        if not success:
            return jsonify({'success': False, 'error': 'Job not found'}), 404

        return jsonify({'success': True, 'job_id': job_id, 'status': 'failed'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@training_bp.route('/api/training/jobs/<job_id>/cancel', methods=['POST'])
def cancel_training_job(job_id):
    """Cancel a training job"""
    try:
        success = training_queue.cancel_job(job_id)
        if not success:
            return jsonify({'success': False, 'error': 'Job not found or already completed'}), 404

        return jsonify({'success': True, 'job_id': job_id, 'status': 'cancelled'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@training_bp.route('/api/training/jobs/<job_id>/processing', methods=['POST'])
def set_training_job_processing(job_id):
    """Worker calls this when it picks up a job. Returns cancelled=true if job was cancelled."""
    try:
        job = training_queue.get_job(job_id)
        if not job:
            return jsonify({'success': False, 'error': 'Job not found'}), 404

        if job['status'] == 'cancelled':
            return jsonify({'success': True, 'cancelled': True, 'job_id': job_id})

        success = training_queue.set_processing(job_id)
        return jsonify({'success': success, 'cancelled': False, 'job_id': job_id})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@training_bp.route('/api/training/jobs/<job_id>/retry', methods=['POST'])
def retry_training_job(job_id):
    """Retry a failed or cancelled training job by creating a new job with same config."""
    job = training_queue.get_job(job_id)
    if not job:
        return jsonify({'success': False, 'error': 'Job not found'}), 404

    if job['status'] not in ('failed', 'cancelled'):
        return jsonify({'success': False, 'error': f'Can only retry failed or cancelled jobs, current status: {job["status"]}'}), 400

    config = job.get('config') or {}
    if job.get('config_json'):
        try:
            config = json.loads(job['config_json'])
        except (json.JSONDecodeError, TypeError):
            pass

    export_path = config.get('export_path', '')
    if not export_path:
        # Try to reconstruct from S3 URI or re-export
        return jsonify({'success': False, 'error': 'Cannot retry: original export path not available. Please submit a new job.'}), 400

    result = training_queue.submit_job(
        export_path=export_path,
        job_type=job['job_type'],
        config=config,
        export_config_id=job.get('export_config_id')
    )

    return jsonify({
        'success': True,
        'new_job_id': result['job_id'],
        'message': f'Retry submitted as new job {result["job_id"][:8]}...',
        'original_job_id': job_id
    })


@training_bp.route('/api/training/jobs/<job_id>', methods=['DELETE'])
def delete_training_job(job_id):
    """Delete a training job record"""
    try:
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
            cursor.execute('DELETE FROM training_jobs WHERE job_id = %s', (job_id,))
            success = cursor.rowcount > 0
            conn.commit()
        if not success:
            return jsonify({'success': False, 'error': 'Job not found'}), 404
        return jsonify({'success': True, 'job_id': job_id})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@training_bp.route('/api/training/jobs/<job_id>/download', methods=['GET'])
def download_training_job_data(job_id):
    """Stream export directory as tar.gz for LAN training workers."""
    try:
        job = training_queue.get_job(job_id)
        if not job:
            return jsonify({'success': False, 'error': 'Job not found'}), 404

        # Get export path from config or s3_uri
        export_path = None
        config = job.get('config') or {}
        if not config and job.get('config_json'):
            try:
                config = json.loads(job['config_json'])
            except (json.JSONDecodeError, TypeError):
                config = {}

        export_path = config.get('export_path')
        if not export_path and job.get('s3_uri', '').startswith('local://'):
            export_path = job['s3_uri'][len('local://'):]

        if not export_path or not os.path.isdir(export_path):
            return jsonify({'success': False, 'error': f'Export data not available: {export_path}'}), 404

        def generate_tar():
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode='w:gz') as tar:
                tar.add(export_path, arcname=os.path.basename(export_path))
            buf.seek(0)
            return buf.read()

        tar_data = generate_tar()
        return Response(
            tar_data,
            mimetype='application/gzip',
            headers={
                'Content-Disposition': f'attachment; filename={job_id}.tar.gz',
                'Content-Length': str(len(tar_data)),
            }
        )
    except Exception as e:
        logger.error(f'Failed to download training data for {job_id}: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


@training_bp.route('/api/training/queue-status', methods=['GET'])
def get_training_queue_status():
    """Get SQS queue depth for main queue and DLQ"""
    try:
        status = training_queue.get_queue_status()
        return jsonify({'success': True, **status})
    except Exception as e:
        logger.warning(f"Queue status unavailable: {e}")
        return jsonify({'success': True, 'queue_messages': 0, 'queue_in_flight': 0, 'dlq_messages': 0, 'unavailable': True})


@training_bp.route('/api/worker/status', methods=['GET'])
def get_worker_status():
    """Get training worker status based on recent job activity."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)

            # Check for recently active jobs (processing in last 5 minutes)
            cursor.execute('''
                SELECT COUNT(*) as active_count
                FROM training_jobs
                WHERE status = 'processing'
            ''')
            active = cursor.fetchone()['active_count']

            # Check last completed job timestamp
            cursor.execute('''
                SELECT completed_at FROM training_jobs
                WHERE status IN ('completed', 'failed')
                ORDER BY completed_at DESC LIMIT 1
            ''')
            last_row = cursor.fetchone()
            last_activity = last_row['completed_at'].isoformat() if last_row and last_row['completed_at'] else None

            # Check queue status (degrade gracefully if AWS unavailable)
            try:
                queue_status = training_queue.get_queue_status()
            except Exception:
                queue_status = {'queue_messages': 0, 'queue_in_flight': 0, 'unavailable': True}

            return jsonify({
                'success': True,
                'worker': {
                    'active_jobs': active,
                    'last_activity': last_activity,
                    'status': 'busy' if active > 0 else 'idle',
                    'queue_messages': queue_status.get('queue_messages', 0),
                    'queue_in_flight': queue_status.get('queue_in_flight', 0)
                }
            })
    except Exception as e:
        logger.error(f'Failed to get worker status: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


# ==================== One-Click Export & Train ====================

@training_bp.route('/api/training/export-and-train', methods=['POST'])
def export_and_train():
    """One-click: export dataset then submit training job"""
    try:
        data = request.get_json()
        job_type = data.get('job_type', 'yolo')

        # Step 1: Export
        if job_type == 'yolo':
            config_id = data.get('export_config_id')
            if not config_id:
                return jsonify({'success': False, 'error': 'export_config_id required for YOLO jobs'}), 400
            export_result = yolo_exporter.export_dataset(
                config_id=int(config_id),
                output_name=data.get('output_name'),
                val_split=data.get('val_split', 0.2),
                seed=data.get('seed', 42)
            )
        elif job_type in ('bearing-fault', 'vibration'):
            export_result = vibration_exporter.export_dataset(
                output_name=data.get('output_name'),
                tag_filter=data.get('tag_filter'),
                formats=data.get('formats'),
                val_split=data.get('val_split', 0.2),
                seed=data.get('seed', 42)
            )
            job_type = 'bearing-fault'
        else:
            return jsonify({'success': False, 'error': f'Unknown job_type: {job_type}'}), 400

        if not export_result.get('success') or not export_result.get('export_path'):
            return jsonify({'success': False, 'error': 'Export produced no data',
                            'export_result': export_result}), 400

        export_path = export_result['export_path']

        # Step 2: Submit training job
        config = {
            'model_type': data.get('model_type', 'yolov8s' if job_type == 'yolo' else 'default'),
            'epochs': data.get('epochs', 100),
            'labels': data.get('labels', ''),
            'gpu_device': data.get('gpu_device', 1),
        }
        if job_type == 'yolo' and data.get('export_config_id'):
            config['export_config_id'] = int(data['export_config_id'])

        job = training_queue.submit_job(
            job_type=job_type,
            export_path=export_path,
            config=config,
            export_config_id=int(data['export_config_id']) if data.get('export_config_id') else None
        )

        return jsonify({
            'success': True,
            'job': job,
            'export_result': export_result,
            'message': f'Exported {export_result.get("total_samples") or export_result.get("annotation_count", 0)} samples and submitted training job'
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@training_bp.route('/api/training/auto-retrain/status', methods=['GET'])
def get_auto_retrain_status_endpoint():
    """Get auto-retrain checker status"""
    try:
        status = get_auto_retrain_status()
        if status is None:
            return jsonify({'success': False, 'error': 'Auto-retrain checker not running'}), 503
        return jsonify({'success': True, **status})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@training_bp.route('/api/training/auto-retrain/trigger', methods=['POST'])
def trigger_auto_retrain():
    """Manually trigger an auto-retrain check (bypasses rate limit)"""
    try:
        from auto_retrain import get_checker_instance
        checker = get_checker_instance()
        if checker is None:
            return jsonify({'success': False, 'error': 'Auto-retrain checker not running'}), 503
        result = checker.check_and_trigger(force=True)
        return jsonify({'success': True, 'result': result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
