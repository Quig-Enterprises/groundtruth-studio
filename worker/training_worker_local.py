#!/usr/bin/env python3
"""
Local LAN Training Worker
Polls Groundtruth Studio for training jobs over HTTP, downloads data,
runs training commands, and reports results back. No AWS dependency.

Usage:
    python training_worker_local.py
    python training_worker_local.py --studio-url http://192.168.50.20:5050
    python training_worker_local.py --data-dir /tmp/training-jobs --once

Environment variables:
    STUDIO_URL      (default: http://192.168.50.20:5050)
    DATA_DIR        (default: /tmp/training-jobs)
    POLL_INTERVAL   (default: 30, seconds between polls)
"""

import argparse
import csv
import io
import json
import logging
import os
import re
import signal
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('training-worker-local')

DEFAULT_STUDIO_URL = 'http://192.168.50.20:5050'
DEFAULT_DATA_DIR = '/tmp/training-jobs'
DEFAULT_POLL_INTERVAL = 30

# Training commands per job type. Substitution variables:
#   {job_id}, {data_dir}, {model_type}, {epochs}, {labels}
#   For vibration jobs: {train_file}, {val_file}, {manifest}, {format}
DEFAULT_TRAINING_COMMANDS = {
    'yolo-training': 'yolo train data={data_dir}/data.yaml model={model_type} epochs={epochs} imgsz=640',
    'bearing-fault': 'python3 train_bearing_fault.py --train {train_file} --val {val_file} --model {model_type} --epochs {epochs} --labels "{labels}"',
    'bearing-fault-training': 'python3 train_bearing_fault.py --train {train_file} --val {val_file} --model {model_type} --epochs {epochs} --labels "{labels}"',
    'vibration': 'python3 train_bearing_fault.py --train {train_file} --val {val_file} --model {model_type} --epochs {epochs} --labels "{labels}"',
    'location': 'python3 -m torchvision.models --data {data_dir} --model {model_type} --epochs {epochs}',
    'custom': 'echo "Custom job {job_id}: data at {data_dir}"',
}


class LocalTrainingWorker:
    def __init__(self, studio_url=None, data_dir=None, poll_interval=None):
        self.studio_url = (studio_url or os.environ.get('STUDIO_URL', DEFAULT_STUDIO_URL)).rstrip('/')
        self.data_dir = Path(data_dir or os.environ.get('DATA_DIR', DEFAULT_DATA_DIR))
        self.poll_interval = int(poll_interval or os.environ.get('POLL_INTERVAL', DEFAULT_POLL_INTERVAL))

        # Load training commands (allow override via env)
        commands_env = os.environ.get('TRAINING_COMMANDS')
        if commands_env:
            self.training_commands = json.loads(commands_env)
        else:
            self.training_commands = DEFAULT_TRAINING_COMMANDS.copy()

        self.running = True
        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _shutdown(self, signum, frame):
        logger.info('Shutdown signal received, finishing current job...')
        self.running = False

    def run(self, once=False):
        """Main loop: poll Studio API for jobs and process them."""
        logger.info('Local training worker started')
        logger.info(f'  Studio URL:    {self.studio_url}')
        logger.info(f'  Data dir:      {self.data_dir}')
        logger.info(f'  Poll interval: {self.poll_interval}s')

        while self.running:
            try:
                job = self._poll_job()
                if job:
                    self._process_job(job)
                    if once:
                        break
                elif once:
                    logger.info('No jobs available, exiting (--once mode)')
                    break
                else:
                    logger.debug(f'No jobs available, sleeping {self.poll_interval}s...')
                    # Sleep in small increments so we can respond to shutdown signals
                    for _ in range(self.poll_interval):
                        if not self.running:
                            break
                        time.sleep(1)
            except Exception as e:
                logger.error(f'Unexpected error in main loop: {e}', exc_info=True)
                time.sleep(5)

        logger.info('Local training worker stopped')

    def _poll_job(self):
        """Poll Studio API for the next available job."""
        try:
            resp = requests.get(
                f'{self.studio_url}/api/training/jobs/next',
                timeout=10
            )
            resp.raise_for_status()
            data = resp.json()
            job = data.get('job')
            if job:
                logger.info(f'Claimed job {job["job_id"]} (type: {job["job_type"]})')
            return job
        except requests.ConnectionError:
            logger.warning(f'Cannot reach Studio at {self.studio_url}')
            return None
        except Exception as e:
            logger.error(f'Poll error: {e}')
            return None

    def _process_job(self, job):
        """Download data, run training, report results."""
        job_id = job['job_id']
        job_type = job.get('job_type', 'custom')
        config = job.get('config') or {}
        if not config and job.get('config_json'):
            try:
                config = json.loads(job['config_json'])
            except (json.JSONDecodeError, TypeError):
                config = {}

        try:
            # Download training data
            job_dir = self._download_data(job)

            # Run training command
            stdout, stderr = self._run_training(job_id, job_type, job_dir, config)

            # Parse training metrics
            metrics = self._parse_training_metrics(job_type, job_dir, stdout, stderr)

            # Report success
            self._report_complete(job_id, config=config, metrics=metrics)
            logger.info(f'Job {job_id} completed successfully')

        except Exception as e:
            logger.error(f'Job {job_id} failed: {e}', exc_info=True)
            self._report_failure(job_id, str(e))

    def _download_data(self, job):
        """Download training data from Studio via HTTP."""
        job_id = job['job_id']
        job_dir = self.data_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        download_url = job.get('download_url', f'/api/training/jobs/{job_id}/download')
        if download_url.startswith('/'):
            download_url = self.studio_url + download_url

        logger.info(f'Downloading training data from {download_url}')

        resp = requests.get(download_url, stream=True, timeout=300)
        resp.raise_for_status()

        # Extract tar.gz to job directory
        tar_bytes = io.BytesIO(resp.content)
        with tarfile.open(fileobj=tar_bytes, mode='r:gz') as tar:
            # Extract with safety check - strip leading directory
            members = tar.getmembers()
            if not members:
                raise ValueError('Empty archive received')

            # Find common prefix to strip (the archive wraps in a directory)
            top_dir = members[0].name.split('/')[0] if '/' in members[0].name else members[0].name

            for member in members:
                # Security: prevent path traversal
                member_path = Path(member.name)
                if member_path.is_absolute() or '..' in member_path.parts:
                    logger.warning(f'Skipping suspicious path in archive: {member.name}')
                    continue
                tar.extract(member, path=str(job_dir))

        # If contents are nested in a subdirectory, find the actual data dir
        extracted_dirs = [d for d in job_dir.iterdir() if d.is_dir()]
        if len(extracted_dirs) == 1:
            # Data was wrapped in a single directory - use that as the job dir
            actual_dir = extracted_dirs[0]
            logger.info(f'Using extracted directory: {actual_dir}')
            return actual_dir

        logger.info(f'Downloaded and extracted training data to {job_dir}')
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

        return result.stdout or '', result.stderr or ''

    def _parse_training_metrics(self, job_type, job_dir, stdout, stderr):
        """Parse training metrics from output files and stdout."""
        metrics = {}
        job_dir = Path(job_dir)

        try:
            if job_type in ('yolo-training', 'yolo'):
                for results_path in [
                    job_dir / 'runs' / 'detect' / 'train' / 'results.csv',
                    job_dir / 'train' / 'results.csv',
                    *job_dir.rglob('results.csv')
                ]:
                    if results_path.exists():
                        with open(results_path) as f:
                            reader = csv.DictReader(f)
                            rows = list(reader)
                        if rows:
                            last = rows[-1]
                            metrics['epochs'] = len(rows)
                            for key_map in [
                                ('metrics/mAP50(B)', 'accuracy'),
                                ('metrics/mAP50-95(B)', 'val_accuracy'),
                                ('train/box_loss', 'loss'),
                                ('val/box_loss', 'val_loss'),
                                ('      metrics/mAP50(B)', 'accuracy'),
                                ('      metrics/mAP50-95(B)', 'val_accuracy'),
                            ]:
                                src, dst = key_map
                                val = last.get(src.strip())
                                if val is not None:
                                    try:
                                        metrics[dst] = float(val.strip())
                                    except (ValueError, AttributeError):
                                        pass
                        break

            elif job_type in ('bearing-fault', 'bearing-fault-training', 'vibration'):
                if stdout:
                    acc_match = re.search(r'(?:accuracy|Accuracy)[:\s]+([0-9.]+)', stdout)
                    if acc_match:
                        metrics['accuracy'] = float(acc_match.group(1))
                    f1_match = re.search(r'(?:f1[_-]?score|F1)[:\s]+([0-9.]+)', stdout)
                    if f1_match:
                        metrics['val_accuracy'] = float(f1_match.group(1))
                    loss_match = re.search(r'(?:loss|Loss)[:\s]+([0-9.]+)', stdout)
                    if loss_match:
                        metrics['loss'] = float(loss_match.group(1))

            elif job_type == 'location':
                if stdout:
                    acc_match = re.search(r'(?:accuracy|val_acc|test_acc)[:\s]+([0-9.]+)', stdout)
                    if acc_match:
                        metrics['accuracy'] = float(acc_match.group(1))
                    loss_match = re.search(r'(?:val_loss|test_loss)[:\s]+([0-9.]+)', stdout)
                    if loss_match:
                        metrics['val_loss'] = float(loss_match.group(1))

        except Exception as e:
            logger.warning(f'Failed to parse training metrics: {e}')

        return metrics if metrics else None

    def _report_complete(self, job_id, config=None, metrics=None):
        """Report job completion to Studio."""
        try:
            payload = {'result': {'status': 'completed'}}
            if metrics:
                payload['metrics'] = metrics
            if config:
                payload['model_name'] = config.get('model_name', config.get('model_type', ''))
                payload['model_version'] = config.get('model_version', '1.0.0')
            requests.post(
                f'{self.studio_url}/api/training/jobs/{job_id}/complete',
                json=payload,
                timeout=30
            )
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


def main():
    parser = argparse.ArgumentParser(description='Groundtruth Studio Local Training Worker')
    parser.add_argument('--studio-url', default=None,
                        help=f'Studio API URL (default: {DEFAULT_STUDIO_URL})')
    parser.add_argument('--data-dir', default=None,
                        help=f'Local data directory (default: {DEFAULT_DATA_DIR})')
    parser.add_argument('--poll-interval', default=None, type=int,
                        help=f'Seconds between polls (default: {DEFAULT_POLL_INTERVAL})')
    parser.add_argument('--once', action='store_true',
                        help='Process one job and exit')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Debug logging')

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    worker = LocalTrainingWorker(
        studio_url=args.studio_url,
        data_dir=args.data_dir,
        poll_interval=args.poll_interval,
    )

    worker.run(once=args.once)


if __name__ == '__main__':
    main()
