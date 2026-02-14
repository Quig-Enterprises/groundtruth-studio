"""
Pipeline Worker — MQTT to Intelligence Bridge

Subscribes to tracker and identity MQTT topics, then feeds events into
Groundtruth Studio's intelligence layer (context engine, violation detector).
Runs periodic visit aggregation and face clustering jobs.
"""

import sys
import json
import time
import signal
import logging
from typing import Dict, Optional, Any

import yaml
import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from db_connection import init_connection_pool, get_cursor, close_connection_pool
from context_engine import ContextEngine
from violation_detector import ViolationDetector
from visit_builder import VisitBuilder
from face_clustering import FaceClusterer

logger = logging.getLogger('pipeline_worker')


class PipelineWorker:
    """Bridges MQTT tracking events into the intelligence layer."""

    def __init__(self, config_path: str = 'pipeline_config.yml'):
        self.config = self._load_config(config_path)
        self.running = False
        self.mqtt_client: Optional[mqtt.Client] = None

        # Intelligence modules (initialised in run())
        self.context_engine: Optional[ContextEngine] = None
        self.violation_detector: Optional[ViolationDetector] = None
        self.visit_builder: Optional[VisitBuilder] = None
        self.face_clusterer: Optional[FaceClusterer] = None

        # Periodic job timers
        self.last_visit_run = 0.0
        self.last_clustering_run = 0.0

    # ── Configuration ─────────────────────────────────────────────

    @staticmethod
    def _load_config(path: str) -> Dict[str, Any]:
        try:
            with open(path, 'r') as f:
                cfg = yaml.safe_load(f)
            logger.info("Loaded config from %s", path)
            return cfg
        except FileNotFoundError:
            logger.error("Config file not found: %s", path)
            raise

    # ── MQTT setup & callbacks ────────────────────────────────────

    def _init_mqtt(self):
        mqtt_cfg = self.config['mqtt']
        self.mqtt_client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=mqtt_cfg.get('client_id', 'pipeline-worker'),
            clean_session=True,
        )
        self.mqtt_client.username_pw_set(
            mqtt_cfg['username'], mqtt_cfg['password']
        )
        self.mqtt_client.on_connect = self._on_connect
        self.mqtt_client.on_disconnect = self._on_disconnect
        self.mqtt_client.on_message = self._on_message
        self.mqtt_client.reconnect_delay_set(min_delay=1, max_delay=30)

        host = mqtt_cfg.get('host', '127.0.0.1')
        port = mqtt_cfg.get('port', 1883)
        logger.info("Connecting to MQTT broker at %s:%d", host, port)
        self.mqtt_client.connect(host, port, keepalive=60)
        self.mqtt_client.loop_start()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            logger.info("Connected to MQTT broker")
            for topic in self.config['mqtt'].get('subscriptions', []):
                client.subscribe(topic, qos=1)
                logger.info("Subscribed to %s", topic)
        else:
            logger.error("MQTT connect failed: %s", reason_code)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        if reason_code != 0:
            logger.warning("Unexpected MQTT disconnect (code %s)", reason_code)

    def _on_message(self, client, userdata, msg):
        try:
            topic = msg.topic
            if topic.startswith('tracker/tracks/'):
                self._handle_tracks(msg.payload)
            elif topic.startswith('identity/face/'):
                self._handle_face_identity(msg.payload)
            else:
                logger.debug("Unhandled topic: %s", topic)
        except Exception as e:
            logger.error("Error processing %s: %s", msg.topic, e, exc_info=True)

    # ── Message handlers ──────────────────────────────────────────

    def _handle_tracks(self, payload: bytes):
        """Process tracker/tracks/{camera_id} messages."""
        data = json.loads(payload)
        camera_id = data.get('camera_id')
        tracks = data.get('tracks', [])
        if not camera_id or not tracks:
            return

        logger.debug("Processing frame from %s with %d tracks", camera_id, len(tracks))

        # Batch identity lookup
        track_ids = [t.get('track_id') for t in tracks if t.get('track_id')]
        identity_map = self._batch_lookup_identities(track_ids)

        # Build tracked_objects list
        tracked_objects = []
        for t in tracks:
            tracked_objects.append({
                'track_id': t.get('track_id'),
                'entity_type': t.get('entity_type', 'unknown'),
                'bbox': t.get('bbox', []),
                'confidence': t.get('confidence', 0.0),
                'identity_id': identity_map.get(t.get('track_id')),
            })

        # Context engine — association building
        try:
            self.context_engine.process_tracked_frame(camera_id, tracked_objects)
        except Exception as e:
            logger.error("Context engine error: %s", e, exc_info=True)

        # Violation detection
        try:
            violation = self.violation_detector.check_power_loading(
                camera_id, tracked_objects
            )
            if violation:
                logger.info(
                    "Violation detected at %s: %s (confidence %.2f)",
                    camera_id, violation['violation_type'],
                    violation.get('confidence', 0),
                )
                self.violation_detector.record_violation(violation)
        except Exception as e:
            logger.error("Violation detector error: %s", e, exc_info=True)

    def _handle_face_identity(self, payload: bytes):
        """Process identity/face/{camera_id} messages."""
        data = json.loads(payload)
        if data.get('face_detected'):
            logger.debug(
                "Face identity event: track=%s camera=%s confidence=%.2f",
                data.get('track_id'), data.get('camera_id'),
                data.get('confidence', 0),
            )

    # ── Database helpers ──────────────────────────────────────────

    @staticmethod
    def _batch_lookup_identities(track_ids: list) -> dict:
        """Look up identity_id for a batch of track_ids in one query."""
        if not track_ids:
            return {}
        try:
            with get_cursor(commit=False) as cursor:
                cursor.execute(
                    "SELECT track_id, identity_id FROM tracks "
                    "WHERE track_id = ANY(%s)",
                    (track_ids,)
                )
                return {
                    row['track_id']: row['identity_id']
                    for row in cursor.fetchall()
                }
        except Exception as e:
            logger.debug("Batch identity lookup failed: %s", e)
            return {}

    # ── Periodic jobs ─────────────────────────────────────────────

    def _run_periodic_jobs(self):
        now = time.time()
        periodic = self.config.get('periodic', {})

        # Visit aggregation (build_visits already calls end_stale_visits)
        visit_interval = periodic.get('visit_interval_seconds', 60)
        if now - self.last_visit_run >= visit_interval:
            self.last_visit_run = now
            try:
                summary = self.visit_builder.build_visits()
                if summary.get('new_visits') or summary.get('updated_visits'):
                    logger.info("Visit aggregation: %s", summary)
            except Exception as e:
                logger.error("Visit aggregation error: %s", e, exc_info=True)

        # Face clustering
        clustering_interval = periodic.get('clustering_interval_seconds', 300)
        if now - self.last_clustering_run >= clustering_interval:
            self.last_clustering_run = now
            try:
                summary = self.face_clusterer.run_clustering()
                if summary.get('clusters_found'):
                    logger.info("Face clustering: %s", summary)
            except Exception as e:
                logger.error("Face clustering error: %s", e, exc_info=True)

    # ── Lifecycle ─────────────────────────────────────────────────

    def _signal_handler(self, signum, _frame):
        logger.info("Received %s, shutting down...", signal.Signals(signum).name)
        self.running = False

    def run(self):
        """Main entry point — start the pipeline worker."""
        logger.info("Pipeline Worker starting up")

        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

        # Database
        init_connection_pool(min_conn=2, max_conn=10)
        logger.info("Database connection pool ready")

        try:
            # Intelligence modules
            self.context_engine = ContextEngine()
            self.violation_detector = ViolationDetector(
                config={
                    'ramp_cameras': self.config.get('violation', {}).get(
                        'ramp_cameras', []
                    )
                }
            )
            self.visit_builder = VisitBuilder()
            self.face_clusterer = FaceClusterer(min_cluster_size=5, min_samples=3)
            logger.info("Intelligence modules initialised")

            # MQTT
            self._init_mqtt()

            # Main loop
            self.running = True
            self.last_visit_run = time.time()
            self.last_clustering_run = time.time()
            logger.info("Pipeline Worker running — waiting for messages")

            while self.running:
                self._run_periodic_jobs()
                time.sleep(1)

        except Exception as e:
            logger.error("Fatal error: %s", e, exc_info=True)
        finally:
            logger.info("Pipeline Worker shutting down")
            if self.mqtt_client:
                self.mqtt_client.loop_stop()
                self.mqtt_client.disconnect()
            close_connection_pool()
            logger.info("Pipeline Worker stopped")


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    )
    config_path = sys.argv[1] if len(sys.argv) > 1 else 'pipeline_config.yml'
    worker = PipelineWorker(config_path=config_path)
    worker.run()
