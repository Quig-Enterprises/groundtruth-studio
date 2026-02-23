#!/usr/bin/env python3
"""
Face Embedding Worker for Photo Catalog Pipeline

Polls GroundTruth Studio for face_detect_embed jobs, fetches photos
from the catalog service, runs InsightFace buffalo_l on GPU, and
posts face embeddings back to the Studio.

Usage:
    python face_embed_worker.py
    STUDIO_URL=http://192.168.50.20:5050 CATALOG_URL=http://alfred:8082 python face_embed_worker.py

Environment variables:
    STUDIO_URL      Studio API URL (default: http://192.168.50.20:5050)
    CATALOG_URL     Photo catalog URL (default: http://alfred:8082)
    POLL_INTERVAL   Seconds between polls (default: 10)
    BATCH_SIZE      Photos per processing batch (default: 50)
    GPU_ID          CUDA device ID (default: 0)
"""

import argparse
import cv2
import json
import logging
import numpy as np
import os
import requests
import signal
import sys
import time

from insightface.app import FaceAnalysis

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('face-embed-worker')

DEFAULT_STUDIO_URL = 'http://192.168.50.20:5050'
DEFAULT_CATALOG_URL = 'http://alfred:8082'
DEFAULT_POLL_INTERVAL = 10
DEFAULT_BATCH_SIZE = 50
DEFAULT_GPU_ID = 0


class FaceEmbedWorker:
    def __init__(
        self,
        studio_url=None,
        catalog_url=None,
        poll_interval=None,
        batch_size=None,
        gpu_id=None,
    ):
        self.studio_url = (studio_url or os.environ.get('STUDIO_URL', DEFAULT_STUDIO_URL)).rstrip('/')
        self.catalog_url = (catalog_url or os.environ.get('CATALOG_URL', DEFAULT_CATALOG_URL)).rstrip('/')
        self.poll_interval = int(poll_interval or os.environ.get('POLL_INTERVAL', DEFAULT_POLL_INTERVAL))
        self.batch_size = int(batch_size or os.environ.get('BATCH_SIZE', DEFAULT_BATCH_SIZE))
        self.gpu_id = int(gpu_id if gpu_id is not None else os.environ.get('GPU_ID', DEFAULT_GPU_ID))

        self.running = True
        self.face_app = None

        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

    def _shutdown(self, signum, frame):
        logger.info('Shutdown signal received, finishing current job...')
        self.running = False

    def _load_model(self):
        """Load InsightFace buffalo_l model on GPU."""
        providers = [
            ('CUDAExecutionProvider', {'device_id': str(self.gpu_id)}),
            'CPUExecutionProvider',
        ]
        logger.info('Loading InsightFace buffalo_l model on GPU %d...', self.gpu_id)
        self.face_app = FaceAnalysis(
            name='buffalo_l',
            providers=providers,
        )
        self.face_app.prepare(ctx_id=self.gpu_id, det_size=(640, 640))
        logger.info('InsightFace buffalo_l loaded successfully')

    def run(self, once=False):
        """Main loop: load model, then poll Studio for face_detect_embed jobs."""
        logger.info('Face embedding worker starting')
        logger.info('  Studio URL:    %s', self.studio_url)
        logger.info('  Catalog URL:   %s', self.catalog_url)
        logger.info('  Poll interval: %ds', self.poll_interval)
        logger.info('  Batch size:    %d', self.batch_size)
        logger.info('  GPU ID:        %d', self.gpu_id)

        self._load_model()

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
                    logger.debug('No jobs available, sleeping %ds...', self.poll_interval)
                    for _ in range(self.poll_interval):
                        if not self.running:
                            break
                        time.sleep(1)
            except Exception as e:
                logger.error('Unexpected error in main loop: %s', e, exc_info=True)
                time.sleep(5)

        logger.info('Face embedding worker stopped')

    def _poll_job(self):
        """Poll Studio API for the next available face_detect_embed job."""
        try:
            resp = requests.get(
                f'{self.studio_url}/api/training/jobs/next',
                params={'job_type': 'face_detect_embed'},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            job = data.get('job')
            if job:
                job_type = job.get('job_type', '')
                if job_type != 'face_detect_embed':
                    # Server returned a different job type - skip it
                    logger.debug('Ignoring job type %s (not face_detect_embed)', job_type)
                    return None
                logger.info('Claimed job %s (type: %s)', job.get('job_id'), job_type)
            return job
        except requests.ConnectionError:
            logger.warning('Cannot reach Studio at %s', self.studio_url)
            return None
        except Exception as e:
            logger.error('Poll error: %s', e)
            return None

    def _process_job(self, job):
        """Fetch photos, run InsightFace, and report results."""
        job_id = job['job_id']
        config = job.get('config') or {}
        if not config and job.get('config_json'):
            try:
                config = json.loads(job['config_json'])
            except (json.JSONDecodeError, TypeError):
                config = {}

        try:
            photo_results = []
            photos_processed = 0
            faces_detected = 0
            photos_no_face = 0

            # Determine photo source
            photo_ids = config.get('photo_ids') or []
            photo_paths = config.get('photo_paths') or []
            batch_id = config.get('batch_id')

            if not photo_ids and not photo_paths and batch_id:
                # Fetch photo IDs for this batch from catalog
                photo_ids = self._fetch_batch_photo_ids(batch_id)

            if photo_ids:
                logger.info('Processing %d photo IDs for job %s', len(photo_ids), job_id)
                # Process in batches to limit memory pressure
                for i in range(0, len(photo_ids), self.batch_size):
                    if not self.running:
                        raise RuntimeError('Worker shutdown during job processing')
                    chunk = photo_ids[i:i + self.batch_size]
                    for photo_id in chunk:
                        result = self._process_photo_id(photo_id)
                        photo_results.append(result)
                        photos_processed += 1
                        faces_detected += len(result.get('faces', []))
                        if not result.get('faces'):
                            photos_no_face += 1

            elif photo_paths:
                logger.info('Processing %d photo paths for job %s', len(photo_paths), job_id)
                for i in range(0, len(photo_paths), self.batch_size):
                    if not self.running:
                        raise RuntimeError('Worker shutdown during job processing')
                    chunk = photo_paths[i:i + self.batch_size]
                    for photo_path in chunk:
                        result = self._process_photo_path(photo_path)
                        photo_results.append(result)
                        photos_processed += 1
                        faces_detected += len(result.get('faces', []))
                        if not result.get('faces'):
                            photos_no_face += 1

            else:
                raise ValueError('Job config must contain photo_ids, photo_paths, or batch_id')

            metrics = {
                'photos_processed': photos_processed,
                'faces_detected': faces_detected,
                'photos_no_face': photos_no_face,
            }
            logger.info(
                'Job %s complete: processed=%d faces=%d no_face=%d',
                job_id, photos_processed, faces_detected, photos_no_face,
            )
            self._report_complete(job_id, photo_results, metrics)

        except Exception as e:
            logger.error('Job %s failed: %s', job_id, e, exc_info=True)
            self._report_failure(job_id, str(e))

    def _fetch_batch_photo_ids(self, batch_id):
        """Fetch the list of photo IDs for a batch from the catalog service."""
        try:
            resp = requests.get(
                f'{self.catalog_url}/api/batches/{batch_id}/photos',
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            ids = data.get('photo_ids') or [p['photo_id'] for p in data.get('photos', [])]
            logger.info('Fetched %d photo IDs for batch %s', len(ids), batch_id)
            return ids
        except Exception as e:
            logger.error('Failed to fetch batch %s photo IDs: %s', batch_id, e)
            raise

    def _fetch_photo(self, photo_id):
        """Fetch image bytes from the catalog service for a given photo ID."""
        resp = requests.get(
            f'{self.catalog_url}/api/photos/{photo_id}/file',
            timeout=30,
        )
        resp.raise_for_status()
        return resp.content

    def _process_photo_id(self, photo_id):
        """Fetch a photo by ID and run face detection/embedding."""
        try:
            image_bytes = self._fetch_photo(photo_id)
            faces = self._extract_faces_from_bytes(image_bytes)
            return {'photo_id': photo_id, 'faces': faces}
        except Exception as e:
            logger.warning('Failed to process photo_id %s: %s', photo_id, e)
            return {'photo_id': photo_id, 'faces': [], 'error': str(e)}

    def _process_photo_path(self, photo_path):
        """Read a photo from disk and run face detection/embedding."""
        try:
            with open(photo_path, 'rb') as f:
                image_bytes = f.read()
            faces = self._extract_faces_from_bytes(image_bytes)
            return {'photo_path': photo_path, 'faces': faces}
        except Exception as e:
            logger.warning('Failed to process photo_path %s: %s', photo_path, e)
            return {'photo_path': photo_path, 'faces': [], 'error': str(e)}

    def _extract_faces(self, image):
        """Run InsightFace on a decoded BGR image and return list of face dicts."""
        detected = self.face_app.get(image)
        faces = []
        for face in detected:
            bbox = face.bbox.astype(float).tolist()
            confidence = float(face.det_score)
            embedding = face.normed_embedding.tolist()
            faces.append({
                'bbox': bbox,
                'confidence': confidence,
                'embedding': embedding,
            })
        return faces

    def _extract_faces_from_bytes(self, image_bytes):
        """Decode raw image bytes and run InsightFace."""
        arr = np.frombuffer(image_bytes, dtype=np.uint8)
        image = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError('Failed to decode image bytes')
        return self._extract_faces(image)

    def _report_complete(self, job_id, embeddings, metrics):
        """Report job completion with embeddings to Studio."""
        try:
            payload = {
                'result': {'status': 'completed'},
                'metrics': metrics,
                'embeddings': embeddings,
            }
            resp = requests.post(
                f'{self.studio_url}/api/training/jobs/{job_id}/complete',
                json=payload,
                timeout=60,
            )
            resp.raise_for_status()
            logger.info('Reported completion for job %s', job_id)
        except Exception as e:
            logger.warning('Could not report completion for %s: %s', job_id, e)

    def _report_failure(self, job_id, error_message):
        """Report job failure to Studio."""
        try:
            requests.post(
                f'{self.studio_url}/api/training/jobs/{job_id}/fail',
                json={'error': error_message[:1000]},
                timeout=10,
            )
        except Exception as e:
            logger.warning('Could not report failure for %s: %s', job_id, e)


def main():
    parser = argparse.ArgumentParser(description='GroundTruth Studio Face Embedding Worker')
    parser.add_argument('--studio-url', default=None,
                        help=f'Studio API URL (default: {DEFAULT_STUDIO_URL})')
    parser.add_argument('--catalog-url', default=None,
                        help=f'Photo catalog URL (default: {DEFAULT_CATALOG_URL})')
    parser.add_argument('--poll-interval', default=None, type=int,
                        help=f'Seconds between polls (default: {DEFAULT_POLL_INTERVAL})')
    parser.add_argument('--batch-size', default=None, type=int,
                        help=f'Photos per processing batch (default: {DEFAULT_BATCH_SIZE})')
    parser.add_argument('--gpu-id', default=None, type=int,
                        help=f'CUDA device ID (default: {DEFAULT_GPU_ID})')
    parser.add_argument('--once', action='store_true',
                        help='Process one job and exit')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Debug logging')

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    worker = FaceEmbedWorker(
        studio_url=args.studio_url,
        catalog_url=args.catalog_url,
        poll_interval=args.poll_interval,
        batch_size=args.batch_size,
        gpu_id=args.gpu_id,
    )

    worker.run(once=args.once)


if __name__ == '__main__':
    main()
