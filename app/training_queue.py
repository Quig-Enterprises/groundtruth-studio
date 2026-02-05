"""
Training Job Queue Client
Manages training job submission via AWS SQS and S3.
Uploads export data to S3, queues job notifications via SQS,
and tracks job status in the local database.
"""

import json
import logging
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

DEFAULT_REGION = 'us-east-2'
DEFAULT_BUCKET = 'groundtruth-studio'
DEFAULT_QUEUE_URL = 'https://sqs.us-east-2.amazonaws.com/051951709252/groundtruth-studio-queue'
DEFAULT_DLQ_URL = 'https://sqs.us-east-2.amazonaws.com/051951709252/groundtruth-studio-dlq'
S3_PREFIX = 'training-jobs'


def init_training_jobs_table(db):
    """Create the training_jobs table if it doesn't exist."""
    with db.get_connection() as conn:
        cursor = conn.cursor()

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS training_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL UNIQUE,
                job_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                s3_uri TEXT,
                config_json TEXT,
                result_json TEXT,
                error_message TEXT,
                submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                export_config_id INTEGER,
                FOREIGN KEY (export_config_id) REFERENCES yolo_export_configs(id)
            )
        ''')

        cursor.execute('CREATE INDEX IF NOT EXISTS idx_training_jobs_job_id ON training_jobs(job_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_training_jobs_status ON training_jobs(status)')

        conn.commit()


class TrainingQueueClient:
    def __init__(self, db, region=None, bucket=None, queue_url=None, dlq_url=None):
        self.db = db
        self.region = region or os.environ.get('AWS_REGION', DEFAULT_REGION)
        self.bucket = bucket or os.environ.get('S3_BUCKET', DEFAULT_BUCKET)
        self.queue_url = queue_url or os.environ.get('SQS_QUEUE_URL', DEFAULT_QUEUE_URL)
        self.dlq_url = dlq_url or os.environ.get('SQS_DLQ_URL', DEFAULT_DLQ_URL)

        self.sqs = boto3.client('sqs', region_name=self.region)
        self.s3 = boto3.client('s3', region_name=self.region)

    def submit_job(self, export_path: str, job_type: str, config: dict,
                   export_config_id: int = None) -> Dict:
        """
        Submit a training job: record in DB, then upload to S3 + queue SQS
        in a background thread.

        Returns immediately with job_id and status='uploading'.
        """
        job_id = str(uuid.uuid4())
        s3_uri = f's3://{self.bucket}/{S3_PREFIX}/{job_id}/'

        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO training_jobs (job_id, job_type, status, s3_uri, config_json, export_config_id)
                VALUES (%s, %s, 'uploading', %s, %s, %s)
            ''', (job_id, job_type, s3_uri, json.dumps(config), export_config_id))
            conn.commit()

        thread = threading.Thread(
            target=self._upload_and_queue,
            args=(job_id, export_path, job_type, config),
            daemon=True
        )
        thread.start()

        return {
            'success': True,
            'job_id': job_id,
            's3_uri': s3_uri,
            'status': 'uploading'
        }

    def _upload_and_queue(self, job_id: str, export_path: str, job_type: str, config: dict):
        """Background worker: upload files to S3, then send SQS message."""
        try:
            export_dir = Path(export_path)
            s3_prefix = f'{S3_PREFIX}/{job_id}'

            for file_path in export_dir.rglob('*'):
                if not file_path.is_file():
                    continue
                relative = file_path.relative_to(export_dir)
                s3_key = f'{s3_prefix}/{relative}'
                logger.info(f'Uploading {file_path} -> s3://{self.bucket}/{s3_key}')
                self.s3.upload_file(str(file_path), self.bucket, s3_key)

            message_body = json.dumps({
                'job_id': job_id,
                'type': job_type,
                'created_at': datetime.utcnow().isoformat() + 'Z',
                'data_uri': f's3://{self.bucket}/{s3_prefix}/',
                'config': config,
                'callback_url': f'/api/training/jobs/{job_id}/complete'
            })

            self.sqs.send_message(
                QueueUrl=self.queue_url,
                MessageBody=message_body
            )

            self._update_job_status(job_id, 'queued')
            logger.info(f'Training job {job_id} queued successfully')

        except ClientError as e:
            error_msg = f'AWS error: {e.response["Error"]["Message"]}'
            logger.error(f'Training job {job_id} failed: {error_msg}')
            self._update_job_status(job_id, 'failed', error_message=error_msg)
        except Exception as e:
            error_msg = str(e)
            logger.error(f'Training job {job_id} failed: {error_msg}')
            self._update_job_status(job_id, 'failed', error_message=error_msg)

    def _update_job_status(self, job_id: str, status: str, error_message: str = None):
        """Update job status in the database."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            if error_message:
                cursor.execute('''
                    UPDATE training_jobs SET status = %s, error_message = %s
                    WHERE job_id = %s
                ''', (status, error_message, job_id))
            else:
                cursor.execute('''
                    UPDATE training_jobs SET status = %s
                    WHERE job_id = %s
                ''', (status, job_id))
            conn.commit()

    def get_queue_status(self) -> Dict:
        """Get approximate message counts for the main queue and DLQ."""
        try:
            attrs = ['ApproximateNumberOfMessages', 'ApproximateNumberOfMessagesNotVisible']

            main_resp = self.sqs.get_queue_attributes(
                QueueUrl=self.queue_url,
                AttributeNames=attrs
            )
            main_attrs = main_resp.get('Attributes', {})

            dlq_resp = self.sqs.get_queue_attributes(
                QueueUrl=self.dlq_url,
                AttributeNames=['ApproximateNumberOfMessages']
            )
            dlq_attrs = dlq_resp.get('Attributes', {})

            return {
                'queue_messages': int(main_attrs.get('ApproximateNumberOfMessages', 0)),
                'queue_in_flight': int(main_attrs.get('ApproximateNumberOfMessagesNotVisible', 0)),
                'dlq_messages': int(dlq_attrs.get('ApproximateNumberOfMessages', 0)),
            }
        except ClientError as e:
            logger.error(f'Failed to get queue status: {e}')
            return {
                'error': f'AWS error: {e.response["Error"]["Message"]}',
                'queue_messages': None,
                'queue_in_flight': None,
                'dlq_messages': None,
            }

    def get_jobs(self, limit: int = 50, offset: int = 0) -> List[Dict]:
        """Get all training jobs ordered by submission time."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM training_jobs
                ORDER BY submitted_at DESC
                LIMIT %s OFFSET %s
            ''', (limit, offset))
            rows = cursor.fetchall()

        jobs = []
        for row in rows:
            job = dict(row)
            if job.get('config_json'):
                try:
                    job['config'] = json.loads(job['config_json'])
                except (json.JSONDecodeError, TypeError):
                    job['config'] = None
            if job.get('result_json'):
                try:
                    job['result'] = json.loads(job['result_json'])
                except (json.JSONDecodeError, TypeError):
                    job['result'] = None
            jobs.append(job)

        return jobs

    def get_job(self, job_id: str) -> Optional[Dict]:
        """Get a single training job by its UUID."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM training_jobs WHERE job_id = %s', (job_id,))
            row = cursor.fetchone()

        if not row:
            return None

        job = dict(row)
        if job.get('config_json'):
            try:
                job['config'] = json.loads(job['config_json'])
            except (json.JSONDecodeError, TypeError):
                job['config'] = None
        if job.get('result_json'):
            try:
                job['result'] = json.loads(job['result_json'])
            except (json.JSONDecodeError, TypeError):
                job['result'] = None

        return job

    def set_processing(self, job_id: str) -> bool:
        """Mark a job as processing. Called by worker when it picks up the message."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE training_jobs SET status = 'processing'
                WHERE job_id = %s AND status IN ('queued', 'uploading')
            ''', (job_id,))
            success = cursor.rowcount > 0
            conn.commit()
        return success

    def cancel_job(self, job_id: str) -> bool:
        """Cancel a job. Only jobs not yet completed can be cancelled."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE training_jobs
                SET status = 'cancelled', error_message = 'Cancelled by user', completed_at = CURRENT_TIMESTAMP
                WHERE job_id = %s AND status NOT IN ('completed', 'cancelled')
            ''', (job_id,))
            success = cursor.rowcount > 0
            conn.commit()
        return success

    def complete_job(self, job_id: str, result_data: dict = None) -> bool:
        """Mark a job as completed. Called by training worker callback."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE training_jobs
                SET status = 'completed', completed_at = CURRENT_TIMESTAMP, result_json = %s
                WHERE job_id = %s AND status NOT IN ('completed', 'failed', 'cancelled')
            ''', (json.dumps(result_data) if result_data else None, job_id))
            success = cursor.rowcount > 0
            conn.commit()
        return success

    def fail_job(self, job_id: str, error_message: str) -> bool:
        """Mark a job as failed."""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE training_jobs
                SET status = 'failed', error_message = %s, completed_at = CURRENT_TIMESTAMP
                WHERE job_id = %s
            ''', (error_message, job_id))
            success = cursor.rowcount > 0
            conn.commit()
        return success
