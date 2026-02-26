"""
YOLO Training Data Exporter
Exports annotated video frames in YOLO format for model training
"""

import json
import random
import shutil
from pathlib import Path
from typing import Dict, List, Optional
import cv2
from database import VideoDatabase
from psycopg2 import extras
from db_connection import get_connection
import logging
logger = logging.getLogger(__name__)


class YOLOExporter:
    def __init__(self, db: VideoDatabase, videos_dir: Path, export_base_dir: Path):
        self.db = db
        self.videos_dir = videos_dir
        self.export_base_dir = export_base_dir
        self.export_base_dir.mkdir(parents=True, exist_ok=True)

    def create_export_config(self, config_name: str, class_mapping: Dict[str, int],
                           description: str = None, **options) -> int:
        """
        Create a new YOLO export configuration

        Args:
            config_name: Unique name for this configuration
            class_mapping: Dict mapping activity_tag to class_id (e.g., {"boat": 0, "person": 1})
            description: Optional description
            **options: Additional config options (include_reviewed_only, include_ai_generated, etc.)

        Returns:
            config_id
        """
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)

            cursor.execute('''
                INSERT INTO yolo_export_configs
                (config_name, description, class_mapping, include_reviewed_only,
                 include_ai_generated, include_negative_examples, min_confidence, export_format)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            ''', (
                config_name,
                description,
                json.dumps(class_mapping),
                bool(options.get('include_reviewed_only', False)),
                bool(options.get('include_ai_generated', True)),
                bool(options.get('include_negative_examples', True)),
                options.get('min_confidence', 0.0),
                options.get('export_format', 'yolov8')
            ))

            config_id = cursor.fetchone()['id']
            conn.commit()
        return config_id

    def add_filter(self, config_id: int, filter_type: str, filter_value: str, is_exclusion: bool = False):
        """
        Add a filter rule to an export configuration

        Filter types:
        - tag: Filter by video tag
        - activity_tag: Filter by keyframe activity_tag
        - annotation_tag: Filter by annotation tag (group_name:tag_value)
        - date_range: Filter by date (start:end)
        - video_id: Specific video ID
        """
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)

            cursor.execute('''
                INSERT INTO yolo_export_filters (export_config_id, filter_type, filter_value, is_exclusion)
                VALUES (%s, %s, %s, %s)
            ''', (config_id, filter_type, filter_value, int(is_exclusion)))

            conn.commit()

    def get_export_configs(self) -> List[Dict]:
        """Get all export configurations"""
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)

            cursor.execute('SELECT * FROM yolo_export_configs ORDER BY created_date DESC')
            rows = cursor.fetchall()

        configs = []
        for row in rows:
            config = dict(row)
            config['class_mapping'] = json.loads(config['class_mapping'])
            configs.append(config)

        return configs

    def get_filtered_videos(self, config_id: int) -> List[int]:
        """
        Get list of video IDs based on export configuration filters

        Returns:
            List of video_ids
        """
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)

            # Get filters
            cursor.execute('SELECT * FROM yolo_export_filters WHERE export_config_id = %s', (config_id,))
            filters = [dict(row) for row in cursor.fetchall()]

            if not filters:
                # No filters - return all videos with annotations
                cursor.execute('''
                    SELECT DISTINCT video_id FROM keyframe_annotations
                    WHERE bbox_x IS NOT NULL AND bbox_width > 0
                ''')
                video_ids = [row['video_id'] for row in cursor.fetchall()]
            else:
                # Apply filters
                video_ids = set()

                for filter_rule in filters:
                    filter_type = filter_rule['filter_type']
                    filter_value = filter_rule['filter_value']
                    is_exclusion = filter_rule['is_exclusion']

                    if filter_type == 'tag':
                        # Filter by video tag
                        cursor.execute('''
                            SELECT DISTINCT v.id FROM videos v
                            JOIN video_tags vt ON v.id = vt.video_id
                            JOIN tags t ON vt.tag_id = t.id
                            WHERE t.name = %s
                        ''', (filter_value,))

                    elif filter_type == 'activity_tag':
                        # Filter by keyframe activity_tag
                        cursor.execute('''
                            SELECT DISTINCT video_id FROM keyframe_annotations
                            WHERE activity_tag = %s
                        ''', (filter_value,))

                    elif filter_type == 'annotation_tag':
                        # Filter by annotation tag (group_name:tag_value)
                        group_name, tag_value = filter_value.split(':', 1)
                        cursor.execute('''
                            SELECT DISTINCT ka.video_id FROM keyframe_annotations ka
                            JOIN annotation_tags at ON ka.id = at.annotation_id
                            JOIN tag_groups tg ON at.group_id = tg.id
                            WHERE tg.group_name = %s AND at.tag_value = %s
                            AND at.annotation_type = 'keyframe'
                        ''', (group_name, tag_value))

                    elif filter_type == 'video_id':
                        cursor.execute('SELECT id FROM videos WHERE id = %s', (int(filter_value),))

                    filter_results = set(list(row.values())[0] for row in cursor.fetchall())

                    if is_exclusion:
                        video_ids -= filter_results
                    else:
                        if not video_ids:
                            video_ids = filter_results
                        else:
                            video_ids &= filter_results

        return list(video_ids)

    def _try_upgrade_placeholder(self, video: dict) -> Optional[Path]:
        """
        If video is an EcoEye placeholder, attempt to download the full video
        clip from the relay, upgrade the DB record, and return the path to the
        downloaded file.  Returns None on failure (caller falls through to thumbnail).
        """
        if not video['filename'].endswith('.placeholder'):
            return None
        original_url = video.get('original_url') or ''
        if not original_url.startswith('ecoeye://'):
            return None

        event_id = original_url.replace('ecoeye://', '', 1)
        video_id = video['id']

        try:
            # Lazy imports to avoid circular dependency (exporter is created in services.py)
            from services import ecoeye_request, ECOEYE_API_BASE, downloader, processor, DOWNLOAD_DIR

            # Query EcoEye for event details
            resp = ecoeye_request('GET', 'api-thumbnails.php',
                params={'event_id': event_id},
                timeout=30
            )
            result = resp.json()
            events = result.get('events', [])
            if not events:
                logger.warning("EcoEye upgrade: event %s not found on relay", event_id)
                return None

            event = events[0]
            video_path_remote = event.get('video_path')
            if not video_path_remote:
                logger.warning("EcoEye upgrade: no video_path for event %s", event_id)
                return None

            # Build download URL (same logic as routes/ecoeye.py line 223-224)
            video_filename = video_path_remote.split('/videos/')[-1] if '/videos/' in video_path_remote else video_path_remote
            video_url = f'{ECOEYE_API_BASE}/videos/{video_filename}'

            # Download the video
            dl_result = downloader.download_video(video_url)
            if not dl_result.get('success'):
                logger.warning("EcoEye upgrade: download failed for event %s: %s",
                    event_id, dl_result.get('error', 'unknown'))
                return None

            # Upgrade the database record
            self.db.update_video(video_id, filename=dl_result['filename'])

            # Extract and update thumbnail
            thumb = processor.extract_thumbnail(str(DOWNLOAD_DIR / dl_result['filename']))
            if thumb.get('success'):
                self.db.update_video(video_id, thumbnail_path=thumb['thumbnail_path'])

            # Stamp upgrade metadata
            from db_connection import get_connection
            from datetime import datetime
            with get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE videos SET metadata = COALESCE(metadata, '{}'::jsonb) || %s WHERE id = %s",
                    (json.dumps({"upgraded_at": datetime.now().isoformat(), "upgraded_by": "yolo_export"}), video_id)
                )
                conn.commit()

            logger.info("EcoEye upgrade: placeholder %s upgraded to %s", event_id, dl_result['filename'])
            return self.videos_dir / dl_result['filename']

        except Exception:
            logger.warning("EcoEye upgrade: failed for event %s", event_id, exc_info=True)
            return None

    def export_dataset(self, config_id: int, output_name: Optional[str] = None,
                       val_split: float = 0.2, seed: int = 42) -> Dict:
        """
        Export complete YOLO dataset for a configuration

        Args:
            config_id: Export configuration ID
            output_name: Optional custom output directory name

        Returns:
            Dict with export statistics and paths
        """
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)

            # Get configuration
            cursor.execute('SELECT * FROM yolo_export_configs WHERE id = %s', (config_id,))
            config_row = cursor.fetchone()
            if not config_row:
                raise ValueError(f"Export configuration {config_id} not found")

            config = dict(config_row)
            config['class_mapping'] = json.loads(config['class_mapping'])

        # Create export directory
        if output_name:
            export_dir = self.export_base_dir / output_name
        else:
            export_dir = self.export_base_dir / f"{config['config_name']}_{config_id}"

        # Clean and recreate export directory
        if export_dir.exists():
            shutil.rmtree(export_dir)
        export_dir.mkdir(parents=True, exist_ok=True)
        for split in ('train', 'val'):
            (export_dir / split / 'images').mkdir(parents=True, exist_ok=True)
            (export_dir / split / 'labels').mkdir(parents=True, exist_ok=True)

        # Get videos to export
        video_ids = self.get_filtered_videos(config_id)

        total_frames = 0
        total_annotations = 0
        train_count = 0
        val_count = 0
        negative_frame_count = 0
        upgraded_count = 0

        # Collect annotations grouped by frame (video_id + timestamp)
        # Each unique frame gets one image file with all its bboxes in one label file
        frame_groups = {}  # key: (video_id, timestamp) -> {video: dict, positive: [list], negative: [list]}

        # Get min_quality_score from config (default 0.0 = no filtering)
        min_quality_score = config.get('min_quality_score', 0.0) or 0.0
        quality_filtered_count = 0
        quality_scores = []

        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
            for video_id in video_ids:
                cursor.execute('SELECT * FROM videos WHERE id = %s', (video_id,))
                video = dict(cursor.fetchone())

                cursor.execute('''
                    SELECT ka.*, ap.quality_score as pred_quality_score
                    FROM keyframe_annotations ka
                    LEFT JOIN ai_predictions ap ON ap.id = ka.source_prediction_id
                    WHERE ka.video_id = %s
                    AND ka.bbox_x IS NOT NULL
                    AND ka.bbox_width > 0
                    ORDER BY ka.timestamp
                ''', (video_id,))

                raw_annotations = [dict(row) for row in cursor.fetchall()]

                # Quality gate: filter out annotations from low-quality source predictions
                annotations = []
                for ann in raw_annotations:
                    pred_qs = ann.get('pred_quality_score')
                    if pred_qs is not None:
                        quality_scores.append(pred_qs)
                    # Apply quality filter if threshold is set
                    if min_quality_score > 0 and pred_qs is not None and pred_qs < min_quality_score:
                        quality_filtered_count += 1
                        continue
                    annotations.append(ann)

                for ann in annotations:
                    if config['include_reviewed_only'] and not ann.get('reviewed'):
                        continue
                    if not config['include_ai_generated'] and ann.get('reviewed') == 0:
                        continue

                    is_neg = ann.get('is_negative', False)

                    # Skip negatives if config says not to include them
                    if not config['include_negative_examples'] and is_neg:
                        continue

                    # Positive annotations must have a known class; negatives don't need one
                    if not is_neg and ann['activity_tag'] not in config['class_mapping']:
                        continue

                    # Skip tiny bboxes that are too small for useful training data
                    min_bbox_dim = config.get('min_bbox_dim', 32)
                    if not is_neg and min_bbox_dim > 0:
                        if (ann.get('bbox_width') or 0) < min_bbox_dim or (ann.get('bbox_height') or 0) < min_bbox_dim:
                            continue

                    key = (video_id, ann['timestamp'])
                    if key not in frame_groups:
                        frame_groups[key] = {'video': video, 'positive': [], 'negative': []}
                    if is_neg:
                        frame_groups[key]['negative'].append(ann)
                    else:
                        frame_groups[key]['positive'].append(ann)

        # Shuffle frames and split into train/val
        all_frames = list(frame_groups.items())
        rng = random.Random(seed)
        rng.shuffle(all_frames)
        val_size = max(1, int(len(all_frames) * val_split)) if all_frames else 0
        val_set = set(range(val_size))

        # Process each frame
        image_exts = {'.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.webp'}
        open_caps = {}
        upgraded_videos = {}  # video_id -> refreshed video dict (avoid duplicate upgrades)
        for idx, ((video_id, timestamp), group) in enumerate(all_frames):
            split = 'val' if idx in val_set else 'train'
            images_dir = export_dir / split / 'images'
            labels_dir = export_dir / split / 'labels'

            video = group['video']
            video_path = self.videos_dir / video['filename']
            # Try to upgrade EcoEye placeholder to full video
            if video['filename'].endswith('.placeholder'):
                if video_id in upgraded_videos:
                    # Already upgraded in a previous frame iteration
                    video = upgraded_videos[video_id]
                    video_path = self.videos_dir / video['filename']
                else:
                    upgraded_path = self._try_upgrade_placeholder(video)
                    if upgraded_path:
                        video_path = upgraded_path
                        # Re-fetch video record with updated filename/thumbnail
                        with get_connection() as conn2:
                            up_cursor = conn2.cursor(cursor_factory=extras.RealDictCursor)
                            up_cursor.execute('SELECT * FROM videos WHERE id = %s', (video_id,))
                            video = dict(up_cursor.fetchone())
                        upgraded_videos[video_id] = video
                        upgraded_count += 1
            thumbnail_path = Path(video['thumbnail_path']) if video.get('thumbnail_path') else None
            frame = None
            img_width = None
            img_height = None
            frame_filename = None

            # Source 1: Direct image file (uploaded JPG/PNG stored as video entry)
            if video_path.exists() and video_path.suffix.lower() in image_exts:
                frame = cv2.imread(str(video_path))
                if frame is not None:
                    img_height, img_width = frame.shape[:2]
                    frame_filename = f"video_{video_id}_img_{int(timestamp * 1000)}.jpg"

            # Source 2: Video file - extract frame at timestamp
            if frame is None and video_path.exists() and not video['filename'].endswith('.placeholder'):
                if video_id not in open_caps:
                    cap = cv2.VideoCapture(str(video_path))
                    open_caps[video_id] = {
                        'cap': cap,
                        'width': int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                        'height': int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                        'fps': cap.get(cv2.CAP_PROP_FPS),
                    }
                vc = open_caps[video_id]
                frame_number = int(timestamp * vc['fps'])
                vc['cap'].set(cv2.CAP_PROP_POS_FRAMES, frame_number)
                ret, frame = vc['cap'].read()
                if ret:
                    img_width = vc['width']
                    img_height = vc['height']
                    frame_filename = f"video_{video_id}_frame_{frame_number}.jpg"
                else:
                    frame = None

            # Source 3: Thumbnail
            if frame is None and thumbnail_path and thumbnail_path.exists():
                frame = cv2.imread(str(thumbnail_path))
                if frame is not None:
                    img_height, img_width = frame.shape[:2]
                    frame_filename = f"video_{video_id}_thumb_{int(timestamp * 1000)}.jpg"

            if frame is None:
                print(f"Warning: No usable image source for video {video_id}")
                continue

            cv2.imwrite(str(images_dir / frame_filename), frame)

            # Write label file: positive bboxes only; empty file for negative-only frames
            label_filename = frame_filename.replace('.jpg', '.txt')
            positive_anns = group['positive']
            with open(labels_dir / label_filename, 'w') as f:
                for ann in positive_anns:
                    class_id = config['class_mapping'][ann['activity_tag']]
                    x_center = (ann['bbox_x'] + ann['bbox_width'] / 2) / img_width
                    y_center = (ann['bbox_y'] + ann['bbox_height'] / 2) / img_height
                    w_norm = ann['bbox_width'] / img_width
                    h_norm = ann['bbox_height'] / img_height
                    f.write(f"{class_id} {x_center:.6f} {y_center:.6f} {w_norm:.6f} {h_norm:.6f}\n")
                    total_annotations += 1
            if not positive_anns:
                negative_frame_count += 1

            total_frames += 1
            if split == 'val':
                val_count += 1
            else:
                train_count += 1

        for vc in open_caps.values():
            vc['cap'].release()

        # Create data.yaml for YOLO
        yaml_content = f"""# YOLO Dataset Configuration
# Generated by Groundtruth Studio

path: {export_dir.absolute()}
train: train/images
val: val/images

# Classes
nc: {len(config['class_mapping'])}
names:
"""

        # Sort by class_id
        sorted_classes = sorted(config['class_mapping'].items(), key=lambda x: x[1])
        for class_name, class_id in sorted_classes:
            yaml_content += f"  {class_id}: {class_name}\n"

        yaml_path = export_dir / 'data.yaml'
        with open(yaml_path, 'w') as f:
            f.write(yaml_content)

        # Create README
        readme_content = f"""# YOLO Training Dataset Export

**Configuration:** {config['config_name']}
**Export Date:** {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
**Description:** {config.get('description', 'N/A')}

## Statistics

- Total Videos: {len(video_ids)}
- Total Frames: {total_frames} (train: {train_count}, val: {val_count})
- Total Annotations: {total_annotations}
- Negative Frames (hard negatives): {negative_frame_count}
- Upgraded Placeholders: {upgraded_count}

## Class Mapping

{chr(10).join(f'- {name} (ID: {id})' for name, id in sorted_classes)}

## Configuration Settings

- Include Reviewed Only: {bool(config['include_reviewed_only'])}
- Include AI Generated: {bool(config['include_ai_generated'])}
- Include Negative Examples: {bool(config['include_negative_examples'])}
- Min Confidence: {config['min_confidence']}

## Directory Structure

```
{export_dir.name}/
├── train/
│   ├── images/      # Training images
│   └── labels/      # Training labels
├── val/
│   ├── images/      # Validation images
│   └── labels/      # Validation labels
├── data.yaml        # YOLO configuration
└── README.md        # This file
```

## Usage

Train with YOLOv8:
```bash
yolo train data={yaml_path.absolute()} model=yolov8n.pt epochs=100 imgsz=640
```

Train with YOLOv5:
```bash
python train.py --data {yaml_path.absolute()} --weights yolov5s.pt --epochs 100
```
"""

        readme_path = export_dir / 'README.md'
        with open(readme_path, 'w') as f:
            f.write(readme_content)

        # Log export
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
            cursor.execute('''
                INSERT INTO yolo_export_logs
                (export_config_id, export_path, video_count, annotation_count, export_format)
                VALUES (%s, %s, %s, %s, %s)
            ''', (config_id, str(export_dir), len(video_ids), total_annotations, config['export_format']))

            # Update config
            cursor.execute('''
                UPDATE yolo_export_configs
                SET last_export_date = NOW(), last_export_count = %s
                WHERE id = %s
            ''', (total_annotations, config_id))

            conn.commit()

        # Compute quality score distribution for export stats
        quality_distribution = {}
        if quality_scores:
            import numpy as np
            bins = [0, 0.1, 0.25, 0.5, 0.75, 1.0]
            hist, _ = np.histogram(quality_scores, bins=bins)
            quality_distribution = {
                f'{bins[i]:.2f}-{bins[i+1]:.2f}': int(hist[i])
                for i in range(len(hist))
            }

        return {
            'success': True,
            'export_path': str(export_dir),
            'video_count': len(video_ids),
            'frame_count': total_frames,
            'annotation_count': total_annotations,
            'train_count': train_count,
            'val_count': val_count,
            'negative_frame_count': negative_frame_count,
            'upgraded_count': upgraded_count,
            'class_mapping': config['class_mapping'],
            'quality_filtered_count': quality_filtered_count,
            'quality_score_distribution': quality_distribution,
            'min_quality_score': min_quality_score
        }

    def get_export_preview(self, config_id: int) -> Dict:
        """
        Preview what would be exported without actually exporting

        Returns statistics about what would be exported
        """
        video_ids = self.get_filtered_videos(config_id)

        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)

            # Get config
            cursor.execute('SELECT * FROM yolo_export_configs WHERE id = %s', (config_id,))
            config = dict(cursor.fetchone())
            config['class_mapping'] = json.loads(config['class_mapping'])

            total_annotations = 0
            class_counts = {name: 0 for name in config['class_mapping'].keys()}

            for video_id in video_ids:
                cursor.execute('''
                    SELECT activity_tag, COUNT(*) as count
                    FROM keyframe_annotations
                    WHERE video_id = %s AND bbox_x IS NOT NULL
                    GROUP BY activity_tag
                ''', (video_id,))

                for row in cursor.fetchall():
                    activity_tag = row['activity_tag']
                    count = row['count']
                    if activity_tag in class_counts:
                        class_counts[activity_tag] += count
                        total_annotations += count

        return {
            'video_count': len(video_ids),
            'total_annotations': total_annotations,
            'class_distribution': class_counts,
            'config': config
        }
