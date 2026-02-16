"""Shared service instances used by all route blueprints."""
import os
import logging
import atexit
from pathlib import Path

from database import VideoDatabase
from db_connection import init_connection_pool, close_connection_pool
from downloader import VideoDownloader
from video_utils import VideoProcessor
from download_queue import DownloadQueue
from yolo_exporter import YOLOExporter
from camera_topology import CameraTopologyLearner
from vibration_exporter import VibrationExporter
from location_exporter import LocationExporter
from sample_router import SampleRouter
from face_clustering import FaceClusterer
from training_queue import TrainingQueueClient, init_training_jobs_table
from sync_config import SyncConfigManager
from auto_retrain import start_auto_retrain_checker, stop_auto_retrain_checker

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
DOWNLOAD_DIR = BASE_DIR / 'downloads'
THUMBNAIL_DIR = BASE_DIR / 'thumbnails'
EXPORT_DIR = BASE_DIR / 'exports'

ALLOWED_EXTENSIONS = {'mp4', 'avi', 'mov', 'mkv', 'webm'}

# EcoEye API Configuration
ECOEYE_API_BASE = 'https://alert.ecoeyetech.com'
ECOEYE_PHP_API_KEY = '-3tsV7gFLF-nxAAUt-zRETAJLWEyxEWszwdT4fCKpeI'
ECOEYE_API_KEY = '2cVrlQ2XW3wxDwZmVzQ3lOCi96jnqKnH8v1wyU97lM0'
ECOEYE_API_SECRET = os.environ.get('ECOEYE_API_SECRET', '8SyPU2FW05yjtaNVOlCGPoyfqFSXJiGp36SEiiKqT-c0dSZDTBr89M8RsMTsD7_pyDHW2b6MxfPxuVUVlzpb8g')

# Service instances — initialized by init_services()
db = None
downloader = None
processor = None
download_queue = None
yolo_exporter = None
vibration_exporter = None
location_exporter = None
topology_learner = None
sample_router = None
face_clusterer = None
sync_config = None
ecoeye_client = None
unifi_client = None
training_queue = None


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def ecoeye_request(method, endpoint, **kwargs):
    """Make authenticated request to EcoEye API with HMAC-SHA256 signing"""
    import requests
    import hmac
    import hashlib
    import time as _time
    import json as _json

    headers = kwargs.pop('headers', {})
    is_legacy = '.php' in endpoint

    if is_legacy:
        # Legacy PHP endpoints use simple API key auth
        headers['X-API-Key'] = ECOEYE_PHP_API_KEY
    else:
        # New /api/ endpoints use HMAC-SHA256 signing
        headers['X-API-Key'] = ECOEYE_API_KEY
        headers['X-Timestamp'] = str(int(_time.time()))

        if ECOEYE_API_SECRET:
            canonical = f"{method.upper()}\n/{endpoint}\n"

            params = kwargs.get('params')
            if params:
                param_str = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
                canonical += param_str + "\n"
            else:
                canonical += "\n"

            json_data = kwargs.get('json')
            if json_data:
                body_json = _json.dumps(json_data, sort_keys=True)
                body_hash = hashlib.sha256(body_json.encode()).hexdigest()
                canonical += body_hash

            signature = hmac.new(
                ECOEYE_API_SECRET.encode(),
                canonical.encode(),
                hashlib.sha256
            ).hexdigest()
            headers['X-Signature'] = signature

    url = f"{ECOEYE_API_BASE}/{endpoint}"
    return requests.request(method, url, headers=headers, **kwargs)


def init_services():
    """Initialize all shared services. Called once at app startup."""
    global db, downloader, processor, download_queue, yolo_exporter
    global vibration_exporter, location_exporter, topology_learner
    global sample_router, face_clusterer, sync_config, training_queue

    # Initialize PostgreSQL connection pool
    init_connection_pool()
    atexit.register(close_connection_pool)

    db = VideoDatabase()
    downloader = VideoDownloader(str(DOWNLOAD_DIR))
    processor = VideoProcessor(str(THUMBNAIL_DIR))
    download_queue = DownloadQueue(DOWNLOAD_DIR, THUMBNAIL_DIR, db)
    yolo_exporter = YOLOExporter(db, DOWNLOAD_DIR, EXPORT_DIR)
    vibration_exporter = VibrationExporter(db, EXPORT_DIR)
    location_exporter = LocationExporter(db, EXPORT_DIR, THUMBNAIL_DIR, DOWNLOAD_DIR)
    topology_learner = CameraTopologyLearner()
    sample_router = SampleRouter(db)
    face_clusterer = FaceClusterer()

    # Sync components
    sync_config = SyncConfigManager()

    # Training queue
    init_training_jobs_table(db)
    training_queue = TrainingQueueClient(db)

    # Startup clip cleanup
    try:
        _startup_cleanup = processor.cleanup_clips(max_age_days=7, max_size_mb=500)
        if _startup_cleanup.get('removed', 0) > 0:
            logger.info(f"Startup clip cleanup: removed {_startup_cleanup['removed']} clips, freed {_startup_cleanup['freed_mb']}MB")
    except Exception as e:
        logger.warning(f"Startup clip cleanup failed: {e}")

    # Start auto-retrain background checker
    start_auto_retrain_checker(db, yolo_exporter, training_queue)

    # Run schema migrations
    from schema import run_migrations
    try:
        run_migrations()
    except Exception as e:
        print(f"[Migrations] Warning: {e}")


# Initialize on import (matches original api.py module-level behavior)
init_services()
