#!/usr/bin/env python3
"""
Training Worker
Polls SQS for training jobs, syncs data from S3 to local storage,
and executes configurable training commands.

Usage:
    python training_worker.py
    python training_worker.py --nas-path /mnt/nas/training-jobs
    python training_worker.py --once  # Process one job and exit

Environment variables:
    AWS_REGION          (default: us-east-2)
    SQS_QUEUE_URL       (default: groundtruth-studio-queue)
    S3_BUCKET           (default: groundtruth-studio)
    NAS_PATH            (default: /mnt/nas/training-jobs)
    STUDIO_URL          (default: http://localhost:5000)
    POLL_INTERVAL       (default: 20, SQS long poll seconds)
    MAX_RETRIES         (default: 3, retries for S3 downloads)
"""

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import boto3
import requests
from botocore.exceptions import ClientError

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('training-worker')

DEFAULT_REGION = 'us-east-2'
DEFAULT_QUEUE_URL = 'https://sqs.us-east-2.amazonaws.com/051951709252/groundtruth-studio-queue'
DEFAULT_BUCKET = 'groundtruth-studio'
DEFAULT_NAS_PATH = '/mnt/nas/training-jobs'
DEFAULT_STUDIO_URL = 'http://localhost:5000'
DEFAULT_POLL_INTERVAL = 20
DEFAULT_MAX_RETRIES = 3

# Training commands per job type. Substitution variables:
#   {job_id}, {data_dir}, {model_type}, {epochs}, {labels}
#   For vibration jobs: {train_file}, {val_file}, {manifest}, {format}
# Override with TRAINING_COMMANDS env var (JSON object).
DEFAULT_TRAINING_COMMANDS = {
    'yolo-training': 'yolo train data={data_dir}/data.yaml model={model_type} epochs={epochs} imgsz=640',
    'bearing-fault': 'python3 train_bearing_fault.py --train {train_file} --val {val_file} --model {model_type} --epochs {epochs} --labels "{labels}"',
    'bearing-fault-training': 'python3 train_bearing_fault.py --train {train_file} --val {val_file} --model {model_type} --epochs {epochs} --labels "{labels}"',
    'vibration': 'python3 train_bearing_fault.py --train {train_file} --val {val_file} --model {model_type} --epochs {epochs} --labels "{labels}"',
    'custom': 'echo "Custom job {job_id}: data at {data_dir}"',
}


class TrainingWorker:
    def __init__(self, nas_path=None, studio_url=None, region=None,
                 queue_url=None, bucket=None, poll_interval=None, max_retries=None):
        self.nas_path = Path(nas_path or os.environ.get('NAS_PATH', DEFAULT_NAS_PATH))
        self.studio_url = (studio_url or os.environ.get('STUDIO_URL', DEFAULT_STUDIO_URL)).rstrip('/')
        self.region = region or os.environ.get('AWS_REGION', DEFAULT_REGION)
        self.queue_url = queue_url or os.environ.get('SQS_QUEUE_URL', DEFAULT_QUEUE_URL)
        self.bucket = bucket or os.environ.get('S3_BUCKET', DEFAULT_BUCKET)
        self.poll_interval = int(poll_interval or os.environ.get('POLL_INTERVAL', DEFAULT_POLL_INTERVAL))
        self.max_retries = int(max_retries or os.environ.get('MAX_RETRIES', DEFAULT_MAX_RETRIES))

        self.sqs = boto3.client('sqs', region_name=self.region)
        self.s3 = boto3.client('s3', region_name=self.region)

        # Load training commands (allow override via env)
        commands_env = os.environ.get('TRAINING_COMMANDS')
        if commands_env:
            self.training_commands = json.loads(commands_env)
        else:
            self.training_commands = DEFAULT_TRAINING_COMMANDS.copy()

        self.running = True
        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

        self.nas_path.mkdir(parents=True, exist_ok=True)

    def _shutdown(self, signum, frame):
        logger.info('Shutdown signal received, finishing current job...')
        self.running = False

    def run(self, once=False):
        """Main loop: poll SQS and process jobs."""
        logger.info('Training worker started')
        logger.info(f'  NAS path:    {self.nas_path}')
        logger.info(f'  Studio URL:  {self.studio_url}')
        logger.info(f'  Queue:       {self.queue_url}')
        logger.info(f'  Bucket:      {self.bucket}')

        while self.running:
            try:
                message = self._poll_message()
                if message:
                    self._process_message(message)
                    if once:
                        break
                elif once:
                    logger.info('No messages available, exiting (--once mode)')
                    break
            except Exception as e:
                logger.error(f'Unexpected error in main loop: {e}', exc_info=True)
                time.sleep(5)

        logger.info('Training worker stopped')

    def _poll_message(self):
        """Long-poll SQS for one message."""
        try:
            resp = self.sqs.receive_message(
                QueueUrl=self.queue_url,
                MaxNumberOfMessages=1,
                WaitTimeSeconds=self.poll_interval,
                MessageAttributeNames=['All']
            )
            messages = resp.get('Messages', [])
            return messages[0] if messages else None
        except ClientError as e:
            logger.error(f'SQS poll error: {e}')
            time.sleep(5)
            return None

    def _process_message(self, message):
        """Process a single SQS message."""
        receipt_handle = message['ReceiptHandle']
        try:
            body = json.loads(message['Body'])
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f'Invalid message body: {e}')
            self._delete_message(receipt_handle)
            return

        job_id = body.get('job_id')
        job_type = body.get('type', 'custom')
        data_uri = body.get('data_uri', '')
        config = body.get('config', {})
        callback_url = body.get('callback_url')

        logger.info(f'Received job {job_id} (type: {job_type})')

        # Check with Studio if job is cancelled, and mark as processing
        if not self._claim_job(job_id):
            logger.info(f'Job {job_id} is cancelled, skipping')
            self._delete_message(receipt_handle)
            return

        try:
            # Sync data from S3 to local NAS
            job_dir = self._sync_from_s3(job_id, data_uri)

            # Run training command
            self._run_training(job_id, job_type, job_dir, config)

            # Report success
            self._report_complete(job_id, callback_url)
            self._delete_message(receipt_handle)
            logger.info(f'Job {job_id} completed successfully')

        except Exception as e:
            logger.error(f'Job {job_id} failed: {e}', exc_info=True)
            self._report_failure(job_id, str(e))
            # Don't delete message — let visibility timeout expire so it can retry
            # Unless it's a permanent failure
            if self._is_permanent_failure(e):
                self._delete_message(receipt_handle)

    def _claim_job(self, job_id):
        """Tell Studio we're processing this job. Returns False if cancelled."""
        try:
            resp = requests.post(
                f'{self.studio_url}/api/training/jobs/{job_id}/processing',
                timeout=10
            )
            data = resp.json()
            if data.get('cancelled'):
                return False
            return True
        except Exception as e:
            # If Studio is unreachable, proceed anyway
            logger.warning(f'Could not reach Studio to claim job {job_id}: {e}')
            return True

    def _sync_from_s3(self, job_id, data_uri):
        """Download job data from S3 to local NAS directory."""
        job_dir = self.nas_path / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        # Parse S3 prefix from data_uri: s3://bucket/prefix/
        s3_prefix = data_uri.replace(f's3://{self.bucket}/', '').rstrip('/')

        logger.info(f'Syncing s3://{self.bucket}/{s3_prefix}/ -> {job_dir}')

        paginator = self.s3.get_paginator('list_objects_v2')
        pages = paginator.paginate(Bucket=self.bucket, Prefix=s3_prefix + '/')

        file_count = 0
        for page in pages:
            for obj in page.get('Contents', []):
                key = obj['Key']
                relative = key[len(s3_prefix) + 1:]  # strip prefix + /
                if not relative:
                    continue

                local_path = job_dir / relative
                local_path.parent.mkdir(parents=True, exist_ok=True)

                for attempt in range(self.max_retries):
                    try:
                        self.s3.download_file(self.bucket, key, str(local_path))
                        file_count += 1
                        break
                    except ClientError as e:
                        if attempt == self.max_retries - 1:
                            raise
                        logger.warning(f'Retry {attempt + 1} for {key}: {e}')
                        time.sleep(2 ** attempt)

        logger.info(f'Synced {file_count} files to {job_dir}')
        return job_dir

    def _detect_vibration_files(self, job_dir):
        """Auto-detect vibration/bearing-fault data files and return substitution dict."""
        job_dir = Path(job_dir)
        subs = {
            'train_file': '',
            'val_file': '',
            'manifest': '',
            'format': 'csv',
        }

        # Check for manifest
        manifest_path = job_dir / 'manifest.json'
        if manifest_path.exists():
            subs['manifest'] = str(manifest_path)
            # Read manifest to get format info
            try:
                with open(manifest_path) as f:
                    manifest = json.load(f)
                    if 'parquet' in manifest.get('formats', []):
                        subs['format'] = 'parquet'
            except Exception as e:
                logger.warning(f'Could not read manifest: {e}')

        # Prefer parquet if available, fallback to csv
        for fmt in ('parquet', 'csv'):
            train_path = job_dir / f'train_samples.{fmt}'
            val_path = job_dir / f'val_samples.{fmt}'
            all_path = job_dir / f'all_samples.{fmt}'

            if train_path.exists():
                subs['train_file'] = str(train_path)
                subs['format'] = fmt
                if val_path.exists():
                    subs['val_file'] = str(val_path)
                break
            elif all_path.exists():
                # No train/val split - use all_samples for both
                subs['train_file'] = str(all_path)
                subs['val_file'] = str(all_path)
                subs['format'] = fmt
                break

        if not subs['train_file']:
            logger.warning(f'No training data files found in {job_dir}')

        logger.info(f'Detected vibration data: format={subs["format"]}, train={subs["train_file"]}, val={subs["val_file"]}')
        return subs

    def _run_training(self, job_id, job_type, job_dir, config):
        """Execute the training command."""
        command_template = self.training_commands.get(job_type)
        if not command_template:
            raise ValueError(f'No training command configured for job type: {job_type}')

        # Build substitution values
        subs = {
            'job_id': job_id,
            'data_dir': str(job_dir),
            'model_type': config.get('model_type', 'yolov8n'),
            'epochs': str(config.get('epochs', 100)),
            'labels': config.get('labels', ''),
        }

        # Auto-detect vibration/bearing-fault data files
        if job_type in ('bearing-fault', 'bearing-fault-training', 'vibration'):
            subs.update(self._detect_vibration_files(job_dir))

        # Add any config values as available substitutions
        for k, v in config.items():
            if k not in subs and isinstance(v, (str, int, float)):
                subs[k] = str(v)

        command = command_template.format(**subs)
        logger.info(f'Running: {command}')

        result = subprocess.run(
            command,
            shell=True,
            cwd=str(job_dir),
            capture_output=True,
            text=True
        )

        if result.stdout:
            logger.info(f'STDOUT:\n{result.stdout[-2000:]}')
        if result.stderr:
            logger.warning(f'STDERR:\n{result.stderr[-2000:]}')

        if result.returncode != 0:
            raise RuntimeError(
                f'Training command exited with code {result.returncode}: '
                f'{result.stderr[-500:] if result.stderr else "no output"}'
            )

    def _report_complete(self, job_id, callback_url=None):
        """Report job completion to Studio."""
        url = callback_url or f'/api/training/jobs/{job_id}/complete'
        if url.startswith('/'):
            url = self.studio_url + url
        try:
            requests.post(url, json={'result': {'status': 'completed'}}, timeout=10)
        except Exception as e:
            logger.warning(f'Could not report completion for {job_id}: {e}')

    def _report_failure(self, job_id, error_message):
        """Report job failure to Studio."""
        try:
            requests.post(
                f'{self.studio_url}/api/training/jobs/{job_id}/fail',
                json={'error': error_message[:1000]},
                timeout=10
            )
        except Exception as e:
            logger.warning(f'Could not report failure for {job_id}: {e}')

    def _delete_message(self, receipt_handle):
        """Delete processed message from SQS."""
        try:
            self.sqs.delete_message(
                QueueUrl=self.queue_url,
                ReceiptHandle=receipt_handle
            )
        except ClientError as e:
            logger.error(f'Failed to delete SQS message: {e}')

    def _is_permanent_failure(self, error):
        """Determine if an error is permanent (no point retrying)."""
        msg = str(error).lower()
        permanent_patterns = [
            'no training command configured',
            'permission denied',
            'not found',
        ]
        return any(p in msg for p in permanent_patterns)


def main():
    parser = argparse.ArgumentParser(description='Groundtruth Studio Training Worker')
    parser.add_argument('--nas-path', default=None, help=f'Local data directory (default: {DEFAULT_NAS_PATH})')
    parser.add_argument('--studio-url', default=None, help=f'Studio API URL (default: {DEFAULT_STUDIO_URL})')
    parser.add_argument('--region', default=None, help=f'AWS region (default: {DEFAULT_REGION})')
    parser.add_argument('--queue-url', default=None, help='SQS queue URL')
    parser.add_argument('--bucket', default=None, help=f'S3 bucket (default: {DEFAULT_BUCKET})')
    parser.add_argument('--poll-interval', default=None, type=int, help=f'SQS long poll seconds (default: {DEFAULT_POLL_INTERVAL})')
    parser.add_argument('--once', action='store_true', help='Process one job and exit')
    parser.add_argument('--verbose', '-v', action='store_true', help='Debug logging')

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    worker = TrainingWorker(
        nas_path=args.nas_path,
        studio_url=args.studio_url,
        region=args.region,
        queue_url=args.queue_url,
        bucket=args.bucket,
        poll_interval=args.poll_interval,
    )

    worker.run(once=args.once)


if __name__ == '__main__':
    main()
