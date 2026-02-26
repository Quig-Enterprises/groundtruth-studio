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

# Job types that carry their data in config rather than an export_path directory
CONFIG_ONLY_JOB_TYPES = {'face_detect_embed'}

@training_bp.route('/api/training/submit', methods=['POST'])
def submit_training_job():
    """Submit a training job: export data, upload to S3, queue via SQS"""
    try:
        data = request.get_json()
        job_type = data.get('job_type', 'yolo-training')
        config = data.get('config', {})

        export_config_id = data.get('export_config_id')
        export_path = data.get('export_path')

        if job_type in CONFIG_ONLY_JOB_TYPES:
            # These job types carry all data in config; no export directory needed.
            # Use a sentinel path so submit_job stays consistent.
            export_path = export_path or ''
        else:
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
    """Atomically claim the next queued training job for a worker.

    Optional query param ?job_type= restricts to a specific job type.
    """
    try:
        job_type_filter = request.args.get('job_type')
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
            if job_type_filter:
                cursor.execute('''
                    UPDATE training_jobs SET status = 'processing'
                    WHERE id = (
                        SELECT id FROM training_jobs
                        WHERE status = 'queued' AND job_type = %s
                        ORDER BY submitted_at
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED
                    )
                    RETURNING *
                ''', (job_type_filter,))
            else:
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

        # Handle clip_analysis job completion — update in-memory status cache
        try:
            job = training_queue.get_job(job_id)
            if job and job.get('job_type') == 'clip_analysis':
                config = job.get('config') or {}
                if not config and job.get('config_json'):
                    config = json.loads(job['config_json'])
                vid = config.get('video_id')
                if vid:
                    from routes.clip_analysis import _analysis_status
                    _analysis_status[int(vid)] = {'status': 'completed'}
                    logger.info(f'Updated clip analysis status for video {vid} (job {job_id})')
        except Exception as e:
            logger.warning(f'Failed to update clip analysis status for job {job_id}: {e}')

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

        # Handle clip_analysis failure — update in-memory status cache
        try:
            job = training_queue.get_job(job_id)
            if job and job.get('job_type') == 'clip_analysis':
                config = job.get('config') or {}
                if not config and job.get('config_json'):
                    config = json.loads(job['config_json'])
                vid = config.get('video_id')
                if vid:
                    from routes.clip_analysis import _analysis_status
                    _analysis_status[int(vid)] = {'status': 'failed', 'error': error_message}
        except Exception as exc:
            logger.warning(f'Failed to update clip analysis status for job {job_id}: {exc}')

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


# ==================== Model Deployment Endpoints ====================

@training_bp.route('/api/training/jobs/<job_id>/validation', methods=['POST'])
def report_training_validation(job_id):
    """Receive post-training validation results from worker."""
    try:
        data = request.get_json() or {}
        validation_map = data.get('validation_map')
        validation_results = data.get('validation_results')
        deploy_status = data.get('deploy_status', 'none')

        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
            cursor.execute('''
                UPDATE training_jobs
                SET validation_map = %s,
                    validation_results = %s,
                    deploy_status = %s
                WHERE job_id = %s
                RETURNING id
            ''', (validation_map, json.dumps(validation_results) if validation_results else None,
                  deploy_status, job_id))
            row = cursor.fetchone()
            conn.commit()

        if not row:
            return jsonify({'success': False, 'error': 'Job not found'}), 404

        return jsonify({'success': True, 'job_id': job_id, 'deploy_status': deploy_status})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@training_bp.route('/api/training/deploy/<job_id>', methods=['POST'])
def deploy_trained_model(job_id):
    """Deploy a trained model from a completed training job.

    1. Copies best.pt to models directory
    2. Creates model_deployments row with status='active'
    3. Marks previous deployment as 'superseded'
    4. Signals vehicle_detect_runner to reload
    5. Keeps last 3 model versions on disk
    """
    try:
        import shutil
        from pathlib import Path

        # Get job details
        job = training_queue.get_job(job_id)
        if not job:
            return jsonify({'success': False, 'error': 'Job not found'}), 404

        if job['status'] != 'completed':
            return jsonify({'success': False, 'error': f'Job not completed (status: {job["status"]})'}), 400

        # Parse config
        config = job.get('config') or {}
        if not config and job.get('config_json'):
            try:
                config = json.loads(job['config_json'])
            except (json.JSONDecodeError, TypeError):
                config = {}

        # Find best.pt in the export directory
        export_path = config.get('export_path', '')
        if not export_path:
            s3_uri = job.get('s3_uri', '')
            if s3_uri.startswith('local://'):
                export_path = s3_uri[len('local://'):]

        if not export_path:
            return jsonify({'success': False, 'error': 'Cannot determine export path'}), 400

        # Look for best.pt in common ultralytics output locations
        best_pt = None
        for candidate in [
            Path(export_path) / 'train_run' / 'weights' / 'best.pt',
            Path(export_path) / 'runs' / 'detect' / 'train' / 'weights' / 'best.pt',
            Path(export_path) / 'train' / 'weights' / 'best.pt',
        ]:
            if candidate.exists():
                best_pt = candidate
                break

        # Also search recursively
        if not best_pt:
            for found in Path(export_path).rglob('best.pt'):
                best_pt = found
                break

        if not best_pt:
            return jsonify({'success': False, 'error': 'best.pt not found in training output'}), 404

        # Copy to models directory with versioned name
        models_dir = Path('/models/custom/people-vehicles-objects/models')
        models_dir.mkdir(parents=True, exist_ok=True)

        model_name = config.get('model_name', 'vehicle-world-v1')
        from datetime import datetime
        version_str = datetime.now().strftime('%Y%m%d_%H%M%S')
        model_version = f'finetune_{version_str}'
        dest_filename = f'{model_name}_{model_version}.pt'
        dest_path = models_dir / dest_filename

        shutil.copy2(str(best_pt), str(dest_path))
        logger.info(f'Copied {best_pt} -> {dest_path}')

        # Get validation info from job
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)

            # Get job DB id and validation data
            cursor.execute(
                'SELECT id, validation_map, validation_results FROM training_jobs WHERE job_id = %s',
                (job_id,))
            job_row = cursor.fetchone()
            job_db_id = job_row['id'] if job_row else None

            # Mark previous active deployments as superseded
            cursor.execute('''
                UPDATE model_deployments
                SET status = 'superseded'
                WHERE model_name = %s AND status = 'active'
            ''', (model_name,))
            superseded_count = cursor.rowcount

            # Create new deployment record
            cursor.execute('''
                INSERT INTO model_deployments
                (model_name, model_version, model_path, training_job_id,
                 validation_map, validation_results, status, deployed_at)
                VALUES (%s, %s, %s, %s, %s, %s, 'active', NOW())
                RETURNING id
            ''', (
                model_name, model_version, str(dest_path), job_db_id,
                job_row['validation_map'] if job_row else None,
                json.dumps(job_row['validation_results']) if job_row and job_row['validation_results'] else None
            ))
            deployment_id = cursor.fetchone()['id']

            # Update training job deploy status
            cursor.execute(
                "UPDATE training_jobs SET deploy_status = 'deployed' WHERE job_id = %s",
                (job_id,))

            conn.commit()

        # Signal vehicle_detect_runner to reload model
        try:
            from vehicle_detect_runner import reload_model
            reload_model()
            logger.info(f'Model reload triggered after deployment {deployment_id}')
        except Exception as e:
            logger.warning(f'Failed to signal model reload: {e}')

        # Clean up old model files (keep last 3)
        try:
            existing_models = sorted(
                models_dir.glob(f'{model_name}_finetune_*.pt'),
                key=lambda p: p.stat().st_mtime,
                reverse=True
            )
            for old_model in existing_models[3:]:
                old_model.unlink()
                logger.info(f'Cleaned up old model: {old_model}')
        except Exception as e:
            logger.warning(f'Failed to clean up old models: {e}')

        return jsonify({
            'success': True,
            'deployment_id': deployment_id,
            'model_path': str(dest_path),
            'model_version': model_version,
            'superseded_count': superseded_count
        })

    except Exception as e:
        logger.error(f'Failed to deploy model from job {job_id}: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


@training_bp.route('/api/training/rollback', methods=['POST'])
def rollback_model():
    """Roll back to the previous active model deployment.

    1. Marks current active deployment as 'rolled_back'
    2. Reactivates the most recent superseded deployment
    3. Signals vehicle_detect_runner to reload
    """
    try:
        data = request.get_json() or {}
        model_name = data.get('model_name', 'vehicle-world-v1')
        reason = data.get('reason', 'manual rollback')

        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)

            # Find current active deployment
            cursor.execute('''
                SELECT id, model_version FROM model_deployments
                WHERE model_name = %s AND status = 'active'
                ORDER BY deployed_at DESC LIMIT 1
            ''', (model_name,))
            current = cursor.fetchone()

            if not current:
                return jsonify({'success': False, 'error': 'No active deployment to roll back'}), 404

            # Mark as rolled back
            cursor.execute('''
                UPDATE model_deployments
                SET status = 'rolled_back', rolled_back_at = NOW(), rollback_reason = %s
                WHERE id = %s
            ''', (reason, current['id']))

            # Reactivate the most recent superseded deployment
            cursor.execute('''
                UPDATE model_deployments
                SET status = 'active', deployed_at = NOW()
                WHERE id = (
                    SELECT id FROM model_deployments
                    WHERE model_name = %s AND status = 'superseded'
                    ORDER BY deployed_at DESC LIMIT 1
                )
                RETURNING id, model_version, model_path
            ''', (model_name,))
            restored = cursor.fetchone()

            conn.commit()

        # Signal model reload
        try:
            from vehicle_detect_runner import reload_model
            reload_model()
        except Exception as e:
            logger.warning(f'Failed to signal model reload after rollback: {e}')

        result = {
            'success': True,
            'rolled_back_deployment': current['id'],
            'rolled_back_version': current['model_version'],
        }
        if restored:
            result['restored_deployment'] = restored['id']
            result['restored_version'] = restored['model_version']
            result['restored_path'] = restored['model_path']
        else:
            result['restored_deployment'] = None
            result['message'] = 'Rolled back to default model (no previous deployment found)'

        return jsonify(result)

    except Exception as e:
        logger.error(f'Failed to rollback model: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


@training_bp.route('/api/training/deployments', methods=['GET'])
def list_deployments():
    """List model deployments with their status."""
    try:
        model_name = request.args.get('model_name', 'vehicle-world-v1')
        limit = int(request.args.get('limit', 20))

        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
            cursor.execute('''
                SELECT md.*, tj.job_id as training_job_ref
                FROM model_deployments md
                LEFT JOIN training_jobs tj ON tj.id = md.training_job_id
                WHERE md.model_name = %s
                ORDER BY md.created_at DESC
                LIMIT %s
            ''', (model_name, limit))
            deployments = [dict(row) for row in cursor.fetchall()]

        return jsonify({'success': True, 'deployments': deployments})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
