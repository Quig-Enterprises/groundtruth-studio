#!/usr/bin/env python3
"""
Auto-detect persons and faces in GroundTruth Studio thumbnails.

Uses the person-face-v1 YOLOv11m model to detect persons and faces,
then submits predictions through GT Studio's AI prediction pipeline
for confidence-based routing (auto-approve / human review / auto-reject).
"""

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Tuple

import psycopg2
import psycopg2.extras
import requests
from PIL import Image
from ultralytics import YOLO


# Configuration
MODEL_PATH = "/models/custom/people-vehicles-objects/models/person-face-v1-best.pt"
API_BASE_URL = "http://localhost:5050"
THUMBNAILS_DIR = "/opt/groundtruth-studio/thumbnails/"
DATABASE_URL = os.environ.get(
    'DATABASE_URL',
    'postgresql://groundtruth:bZv6QbJ8KCAQubJFb+frmbGNKUiPm7lBUg0XgMvEzNQ=@localhost:5432/groundtruth_studio'
)

MODEL_NAME = "person-face-v1"
MODEL_VERSION = "1.0"
MODEL_TYPE = "yolo"

CLASS_NAMES = {
    0: "person",
    1: "face"
}

SCENARIO_MAP = {
    0: "person_detection",
    1: "face_detection"
}


class AutoDetector:
    """Automated detection pipeline for GroundTruth Studio."""

    def __init__(self, conf_threshold: float = 0.25, device: str = "0", dry_run: bool = False, force: bool = False):
        """
        Initialize the auto-detector.

        Args:
            conf_threshold: Minimum confidence threshold for detections
            device: Device to run inference on (0 for GPU, cpu for CPU)
            dry_run: If True, don't submit predictions to API
            force: If True, re-process videos that already have predictions
        """
        self.conf_threshold = conf_threshold
        self.device = device
        self.dry_run = dry_run
        self.force = force
        self.model: Optional[YOLO] = None
        self.stats = {
            'processed': 0,
            'skipped': 0,
            'errors': 0,
            'total_persons': 0,
            'total_faces': 0
        }

    def load_model(self) -> None:
        """Load the YOLO model."""
        if not Path(MODEL_PATH).exists():
            raise FileNotFoundError(f"Model not found: {MODEL_PATH}")

        print(f"Loading model from {MODEL_PATH}...")
        self.model = YOLO(MODEL_PATH)
        print(f"Model loaded. Using device: {self.device}")

    def get_db_connection(self):
        """Get database connection."""
        return psycopg2.connect(DATABASE_URL)

    def get_videos_with_thumbnails(self) -> List[Dict]:
        """
        Query database for all videos with thumbnails.

        Returns:
            List of video records with id, title, and thumbnail_path
        """
        conn = self.get_db_connection()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, title, thumbnail_path
                    FROM videos
                    WHERE thumbnail_path IS NOT NULL
                    ORDER BY id
                """)
                return cur.fetchall()
        finally:
            conn.close()

    def get_video_by_id(self, video_id: int) -> Optional[Dict]:
        """
        Get a single video by ID.

        Args:
            video_id: Video ID to fetch

        Returns:
            Video record or None if not found
        """
        conn = self.get_db_connection()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, title, thumbnail_path
                    FROM videos
                    WHERE id = %s
                """, (video_id,))
                return cur.fetchone()
        finally:
            conn.close()

    def has_existing_predictions(self, video_id: int) -> bool:
        """
        Check if video already has predictions from this model.

        Args:
            video_id: Video ID to check

        Returns:
            True if predictions exist
        """
        conn = self.get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*) FROM ai_predictions
                    WHERE video_id = %s
                    AND model_name = %s
                    AND model_version = %s
                """, (video_id, MODEL_NAME, MODEL_VERSION))
                count = cur.fetchone()[0]
                return count > 0
        finally:
            conn.close()

    def get_existing_annotations(self, video_id: int) -> List[Dict]:
        """
        Get all existing keyframe annotation bboxes for a video (both human and AI).

        Args:
            video_id: Video ID

        Returns:
            List of bbox dicts with x, y, width, height, activity_tag
        """
        conn = self.get_db_connection()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT bbox_x, bbox_y, bbox_width, bbox_height, activity_tag
                    FROM keyframe_annotations
                    WHERE video_id = %s
                    AND bbox_x IS NOT NULL AND bbox_y IS NOT NULL
                """, (video_id,))
                return cur.fetchall()
        finally:
            conn.close()

    @staticmethod
    def compute_iou(box_a: Dict, box_b: Dict) -> float:
        """Compute Intersection over Union between two bboxes (x, y, width, height)."""
        ax1, ay1 = box_a['x'], box_a['y']
        ax2, ay2 = ax1 + box_a['width'], ay1 + box_a['height']
        bx1, by1 = box_b['x'], box_b['y']
        bx2, by2 = bx1 + box_b['width'], by1 + box_b['height']

        ix1, iy1 = max(ax1, bx1), max(ay1, by1)
        ix2, iy2 = min(ax2, bx2), min(ay2, by2)
        inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
        if inter == 0:
            return 0.0

        area_a = box_a['width'] * box_a['height']
        area_b = box_b['width'] * box_b['height']
        return inter / (area_a + area_b - inter)

    def filter_duplicate_detections(self, video_id: int, predictions: List[Dict], iou_threshold: float = 0.5) -> List[Dict]:
        """
        Remove predictions that overlap with existing annotations for this video.

        Args:
            video_id: Video ID
            predictions: New predictions to filter
            iou_threshold: IoU threshold above which a detection is considered duplicate

        Returns:
            Filtered list of predictions (duplicates removed)
        """
        existing = self.get_existing_annotations(video_id)
        if not existing:
            return predictions

        filtered = []
        for pred in predictions:
            pred_box = pred['bbox']
            is_dup = False
            for ann in existing:
                ann_box = {
                    'x': ann['bbox_x'], 'y': ann['bbox_y'],
                    'width': ann['bbox_width'], 'height': ann['bbox_height']
                }
                # Any significantly overlapping bbox is a duplicate regardless of label
                if self.compute_iou(pred_box, ann_box) >= iou_threshold:
                    is_dup = True
                    break
            if not is_dup:
                filtered.append(pred)
            else:
                print(f"    Skipping duplicate {pred['scenario']} detection (IoU >= {iou_threshold} with existing annotation)")

        return filtered

    def run_detection(self, image_path: str) -> Tuple[List[Dict], float]:
        """
        Run YOLO detection on an image.

        Args:
            image_path: Path to image file

        Returns:
            Tuple of (predictions list, inference time in ms)
        """
        if self.model is None:
            raise RuntimeError("Model not loaded. Call load_model() first.")

        # Get image dimensions for validation
        img = Image.open(image_path)
        img_width, img_height = img.size

        # Run inference
        start_time = time.time()
        results = self.model.predict(
            source=image_path,
            conf=self.conf_threshold,
            device=self.device,
            verbose=False
        )
        inference_time_ms = (time.time() - start_time) * 1000

        predictions = []
        result = results[0]  # Single image

        if result.boxes is not None and len(result.boxes) > 0:
            for box in result.boxes:
                # Extract box coordinates (xyxy format)
                x1, y1, x2, y2 = box.xyxy[0].tolist()
                confidence = float(box.conf[0])
                class_id = int(box.cls[0])

                # Convert to GT Studio format
                bbox = {
                    'x': int(x1),
                    'y': int(y1),
                    'width': int(x2 - x1),
                    'height': int(y2 - y1)
                }

                # Validate coordinates
                if (bbox['x'] < 0 or bbox['y'] < 0 or
                    bbox['x'] + bbox['width'] > img_width or
                    bbox['y'] + bbox['height'] > img_height):
                    print(f"Warning: Skipping out-of-bounds detection: {bbox}")
                    continue

                prediction = {
                    'prediction_type': 'keyframe',
                    'confidence': round(confidence, 4),
                    'timestamp': 0.0,  # Thumbnail is at t=0
                    'scenario': SCENARIO_MAP.get(class_id, 'unknown'),
                    'tags': {
                        'class': CLASS_NAMES.get(class_id, f'class_{class_id}'),
                        'class_id': class_id
                    },
                    'bbox': bbox,
                    'inference_time_ms': round(inference_time_ms, 2)
                }
                predictions.append(prediction)

        # Post-process: ensure every face has a containing person bbox
        predictions = self.ensure_person_for_faces(predictions, img_width, img_height)

        return predictions, inference_time_ms

    def ensure_person_for_faces(self, predictions: List[Dict], img_width: int, img_height: int) -> List[Dict]:
        """
        For every face detection, ensure a person bbox exists that contains it.
        If no person bbox contains the face, synthesize one by expanding the face bbox.
        """
        persons = [p for p in predictions if p['tags']['class_id'] == 0]
        faces = [p for p in predictions if p['tags']['class_id'] == 1]

        if not faces:
            return predictions

        new_persons = []
        for face in faces:
            fb = face['bbox']
            face_cx = fb['x'] + fb['width'] / 2
            face_cy = fb['y'] + fb['height'] / 2

            # Check if any existing person bbox contains this face center
            contained = False
            for person in persons:
                pb = person['bbox']
                if (pb['x'] <= face_cx <= pb['x'] + pb['width'] and
                        pb['y'] <= face_cy <= pb['y'] + pb['height']):
                    contained = True
                    break

            if not contained:
                # Synthesize a person bbox: expand face bbox downward (body below face)
                # Typical person is ~7x face height, face at top ~15%
                body_height = int(fb['height'] * 6)
                body_width = int(fb['width'] * 2.5)
                px = max(0, int(face_cx - body_width / 2))
                py = max(0, fb['y'])
                pw = min(body_width, img_width - px)
                ph = min(body_height, img_height - py)

                new_persons.append({
                    'prediction_type': 'keyframe',
                    'confidence': round(face['confidence'] * 0.9, 4),  # Slightly lower confidence
                    'timestamp': 0.0,
                    'scenario': 'person_detection',
                    'tags': {
                        'class': 'person',
                        'class_id': 0
                    },
                    'bbox': {'x': px, 'y': py, 'width': pw, 'height': ph},
                    'inference_time_ms': face['inference_time_ms']
                })
                print(f"    Synthesized person bbox for uncontained face at ({fb['x']},{fb['y']})")

        return predictions + new_persons

    def submit_predictions(self, video_id: int, predictions: List[Dict]) -> bool:
        """
        Submit predictions to GT Studio API.

        Args:
            video_id: Video ID
            predictions: List of prediction dictionaries

        Returns:
            True if submission succeeded
        """
        if self.dry_run:
            print(f"  [DRY RUN] Would submit {len(predictions)} predictions")
            return True

        batch_id = f"auto-detect-{int(time.time())}"

        payload = {
            'video_id': video_id,
            'model_name': MODEL_NAME,
            'model_version': MODEL_VERSION,
            'model_type': MODEL_TYPE,
            'batch_id': batch_id,
            'predictions': predictions
        }

        try:
            response = requests.post(
                f"{API_BASE_URL}/api/ai/predictions/batch",
                json=payload,
                headers={'X-Auth-Role': 'admin'},
                timeout=30
            )
            response.raise_for_status()
            result = response.json()

            # Print routing summary
            if 'summary' in result:
                summary = result['summary']
                print(f"  Submitted {summary.get('total', 0)} predictions:")
                print(f"    Auto-approved: {summary.get('auto_approved', 0)}")
                print(f"    Human review: {summary.get('human_review', 0)}")
                print(f"    Auto-rejected: {summary.get('auto_rejected', 0)}")

            return True

        except requests.exceptions.RequestException as e:
            print(f"  Error submitting predictions: {e}")
            self.stats['errors'] += 1
            return False

    def process_video(self, video: Dict) -> Optional[Dict]:
        """
        Process a single video's thumbnail.

        Args:
            video: Video record dictionary

        Returns:
            Processing result dictionary or None if skipped
        """
        video_id = video['id']
        title = video['title']
        thumbnail_path = video['thumbnail_path']

        print(f"\nProcessing video {video_id}: {title}")

        # Check if already processed
        if not self.dry_run and not self.force and self.has_existing_predictions(video_id):
            print(f"  Skipping: Already has predictions from {MODEL_NAME}")
            self.stats['skipped'] += 1
            return None

        # Construct full path and handle legacy path remapping
        if not thumbnail_path.startswith('/'):
            thumbnail_path = os.path.join(THUMBNAILS_DIR, thumbnail_path)
        elif not os.path.exists(thumbnail_path):
            # Remap legacy paths (e.g. /var/www/html/groundtruth-studio/thumbnails/ -> /opt/groundtruth-studio/thumbnails/)
            basename = os.path.basename(thumbnail_path)
            remapped = os.path.join(THUMBNAILS_DIR, basename)
            if os.path.exists(remapped):
                thumbnail_path = remapped

        # Check if file exists
        if not os.path.exists(thumbnail_path):
            print(f"  Error: Thumbnail not found: {thumbnail_path}")
            self.stats['errors'] += 1
            return None

        try:
            # Run detection
            predictions, inference_time = self.run_detection(thumbnail_path)

            # Count detections by class
            person_count = sum(1 for p in predictions if p['tags']['class_id'] == 0)
            face_count = sum(1 for p in predictions if p['tags']['class_id'] == 1)

            print(f"  Detected: {person_count} persons, {face_count} faces")
            print(f"  Inference time: {inference_time:.2f}ms")

            # Filter out detections that duplicate existing annotations (human or AI)
            if predictions and not self.dry_run:
                predictions = self.filter_duplicate_detections(video_id, predictions)
                person_count = sum(1 for p in predictions if p['tags']['class_id'] == 0)
                face_count = sum(1 for p in predictions if p['tags']['class_id'] == 1)

            # Submit to API
            if predictions:
                success = self.submit_predictions(video_id, predictions)
                if success:
                    self.stats['processed'] += 1
                    self.stats['total_persons'] += person_count
                    self.stats['total_faces'] += face_count
            else:
                print("  No detections above threshold")
                self.stats['processed'] += 1

            return {
                'video_id': video_id,
                'title': title,
                'persons': person_count,
                'faces': face_count,
                'total': len(predictions)
            }

        except Exception as e:
            print(f"  Error processing video: {e}")
            self.stats['errors'] += 1
            return None

    def process_all_videos(self) -> None:
        """Process all videos with thumbnails."""
        print("Fetching videos with thumbnails...")
        videos = self.get_videos_with_thumbnails()
        print(f"Found {len(videos)} videos with thumbnails\n")

        results = []
        for video in videos:
            result = self.process_video(video)
            if result:
                results.append(result)

        self.print_summary(results)

    def process_single_video(self, video_id: int) -> None:
        """Process a single video by ID."""
        video = self.get_video_by_id(video_id)
        if not video:
            print(f"Error: Video {video_id} not found")
            sys.exit(1)

        if not video['thumbnail_path']:
            print(f"Error: Video {video_id} has no thumbnail")
            sys.exit(1)

        result = self.process_video(video)
        if result:
            self.print_summary([result])

    def print_summary(self, results: List[Dict]) -> None:
        """Print summary table of results."""
        print("\n" + "=" * 80)
        print("DETECTION SUMMARY")
        print("=" * 80)

        if results:
            # Print table header
            print(f"{'Video ID':<10} {'Title':<40} {'Persons':<8} {'Faces':<8} {'Total':<8}")
            print("-" * 80)

            # Print rows
            for r in results:
                title = r['title'][:38] + '..' if len(r['title']) > 40 else r['title']
                print(f"{r['video_id']:<10} {title:<40} {r['persons']:<8} {r['faces']:<8} {r['total']:<8}")

            print("-" * 80)

        # Print statistics
        print(f"\nStatistics:")
        print(f"  Videos processed: {self.stats['processed']}")
        print(f"  Videos skipped: {self.stats['skipped']}")
        print(f"  Errors: {self.stats['errors']}")
        print(f"  Total persons detected: {self.stats['total_persons']}")
        print(f"  Total faces detected: {self.stats['total_faces']}")

        if self.dry_run:
            print("\n[DRY RUN MODE - No predictions were submitted]")

        print("=" * 80)


def main():
    parser = argparse.ArgumentParser(
        description="Auto-detect persons and faces in GroundTruth Studio thumbnails",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Process all videos
  %(prog)s --all

  # Process a single video
  %(prog)s --video-id 123

  # Dry run to see what would be detected
  %(prog)s --all --dry-run

  # Use CPU instead of GPU
  %(prog)s --all --device cpu

  # Use higher confidence threshold
  %(prog)s --all --conf 0.5
        """
    )

    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        '--all',
        action='store_true',
        help='Process all videos with thumbnails'
    )
    mode_group.add_argument(
        '--video-id',
        type=int,
        metavar='N',
        help='Process a single video by ID'
    )

    parser.add_argument(
        '--conf',
        type=float,
        default=0.25,
        metavar='THRESHOLD',
        help='Confidence threshold (default: 0.25)'
    )
    parser.add_argument(
        '--device',
        type=str,
        default='0',
        help='Device to use: 0 for GPU, cpu for CPU (default: 0)'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show detections without submitting to API'
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='Re-process videos that already have predictions'
    )

    args = parser.parse_args()

    # Initialize detector
    detector = AutoDetector(
        conf_threshold=args.conf,
        device=args.device,
        dry_run=args.dry_run,
        force=args.force
    )

    # Load model
    try:
        detector.load_model()
    except Exception as e:
        print(f"Error loading model: {e}")
        sys.exit(1)

    # Run detection
    try:
        if args.all:
            detector.process_all_videos()
        else:
            detector.process_single_video(args.video_id)
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
        detector.print_summary([])
        sys.exit(1)
    except Exception as e:
        print(f"\nFatal error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
