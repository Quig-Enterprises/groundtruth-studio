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
        with self.db.get_connection() as conn:
            cursor = conn.cursor()

            cursor.execute('''
                INSERT INTO yolo_export_configs
                (config_name, description, class_mapping, include_reviewed_only,
                 include_ai_generated, include_negative_examples, min_confidence, export_format)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ''', (
                config_name,
                description,
                json.dumps(class_mapping),
                options.get('include_reviewed_only', 0),
                options.get('include_ai_generated', 1),
                options.get('include_negative_examples', 1),
                options.get('min_confidence', 0.0),
                options.get('export_format', 'yolov8')
            ))

            config_id = cursor.lastrowid
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
        with self.db.get_connection() as conn:
            cursor = conn.cursor()

            cursor.execute('''
                INSERT INTO yolo_export_filters (export_config_id, filter_type, filter_value, is_exclusion)
                VALUES (%s, %s, %s, %s)
            ''', (config_id, filter_type, filter_value, int(is_exclusion)))

            conn.commit()

    def get_export_configs(self) -> List[Dict]:
        """Get all export configurations"""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()

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
        with self.db.get_connection() as conn:
            cursor = conn.cursor()

            # Get filters
            cursor.execute('SELECT * FROM yolo_export_filters WHERE export_config_id = %s', (config_id,))
            filters = [dict(row) for row in cursor.fetchall()]

            if not filters:
                # No filters - return all videos with annotations
                cursor.execute('''
                    SELECT DISTINCT video_id FROM keyframe_annotations
                    WHERE bbox_x IS NOT NULL AND bbox_width > 0
                ''')
                video_ids = [row[0] for row in cursor.fetchall()]
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

                    filter_results = set(row[0] for row in cursor.fetchall())

                    if is_exclusion:
                        video_ids -= filter_results
                    else:
                        if not video_ids:
                            video_ids = filter_results
                        else:
                            video_ids &= filter_results

        return list(video_ids)

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
        with self.db.get_connection() as conn:
            cursor = conn.cursor()

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

        export_dir.mkdir(parents=True, exist_ok=True)
        # Create train/val split directories
        for split in ('train', 'val'):
            (export_dir / split / 'images').mkdir(parents=True, exist_ok=True)
            (export_dir / split / 'labels').mkdir(parents=True, exist_ok=True)

        # Get videos to export
        video_ids = self.get_filtered_videos(config_id)

        total_frames = 0
        total_annotations = 0
        train_count = 0
        val_count = 0

        # Collect all frame entries first for splitting
        all_frame_entries = []

            for video_id in video_ids:
                # Get video info
                cursor.execute('SELECT * FROM videos WHERE id = %s', (video_id,))
                video = dict(cursor.fetchone())

                # Get keyframe annotations
                cursor.execute('''
                    SELECT * FROM keyframe_annotations
                    WHERE video_id = %s
                    AND bbox_x IS NOT NULL
                    AND bbox_width > 0
                    ORDER BY timestamp
                ''', (video_id,))

                annotations = [dict(row) for row in cursor.fetchall()]

                # Filter based on configuration
                filtered_annotations = []
                for ann in annotations:
                    if config['include_reviewed_only'] and not ann.get('reviewed'):
                        continue
                    if not config['include_ai_generated'] and ann.get('reviewed') == 0:
                        continue
                    if not config['include_negative_examples'] and ann.get('is_negative'):
                        continue
                    if ann['activity_tag'] not in config['class_mapping']:
                        continue
                    filtered_annotations.append(ann)

                if not filtered_annotations:
                    continue

                for ann in filtered_annotations:
                    all_frame_entries.append((video_id, video, ann))

        # Shuffle and split into train/val
        rng = random.Random(seed)
        rng.shuffle(all_frame_entries)
        val_size = max(1, int(len(all_frame_entries) * val_split)) if all_frame_entries else 0
        val_set = set(range(val_size))

        # Process each frame
        open_caps = {}
        for idx, (video_id, video, ann) in enumerate(all_frame_entries):
            split = 'val' if idx in val_set else 'train'
            images_dir = export_dir / split / 'images'
            labels_dir = export_dir / split / 'labels'

            video_path = self.videos_dir / video['filename']
            if not video_path.exists():
                print(f"Warning: Video file not found: {video_path}")
                continue

            # Cache video captures
            if video_id not in open_caps:
                cap = cv2.VideoCapture(str(video_path))
                open_caps[video_id] = {
                    'cap': cap,
                    'width': int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                    'height': int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                    'fps': cap.get(cv2.CAP_PROP_FPS),
                }
            vc = open_caps[video_id]
            cap = vc['cap']

            frame_number = int(ann['timestamp'] * vc['fps'])
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_number)
            ret, frame = cap.read()

            if not ret:
                continue

            frame_filename = f"video_{video_id}_frame_{frame_number}.jpg"
            cv2.imwrite(str(images_dir / frame_filename), frame)

            label_filename = frame_filename.replace('.jpg', '.txt')
            class_id = config['class_mapping'][ann['activity_tag']]
            x_center = (ann['bbox_x'] + ann['bbox_width'] / 2) / vc['width']
            y_center = (ann['bbox_y'] + ann['bbox_height'] / 2) / vc['height']
            width = ann['bbox_width'] / vc['width']
            height = ann['bbox_height'] / vc['height']

            with open(labels_dir / label_filename, 'w') as f:
                f.write(f"{class_id} {x_center:.6f} {y_center:.6f} {width:.6f} {height:.6f}\n")

            total_frames += 1
            total_annotations += 1
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
**Export Date:** {cursor.execute('SELECT datetime()').fetchone()[0]}
**Description:** {config.get('description', 'N/A')}

## Statistics

- Total Videos: {len(video_ids)}
- Total Frames: {total_frames} (train: {train_count}, val: {val_count})
- Total Annotations: {total_annotations}

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
            cursor.execute('''
                INSERT INTO yolo_export_logs
                (export_config_id, export_path, video_count, annotation_count, export_format)
                VALUES (%s, %s, %s, %s, %s)
            ''', (config_id, str(export_dir), len(video_ids), total_annotations, config['export_format']))

            # Update config
            cursor.execute('''
                UPDATE yolo_export_configs
                SET last_export_date = datetime(), last_export_count = %s
                WHERE id = %s
            ''', (total_annotations, config_id))

            conn.commit()

        return {
            'success': True,
            'export_path': str(export_dir),
            'video_count': len(video_ids),
            'frame_count': total_frames,
            'annotation_count': total_annotations,
            'train_count': train_count,
            'val_count': val_count,
            'class_mapping': config['class_mapping']
        }

    def get_export_preview(self, config_id: int) -> Dict:
        """
        Preview what would be exported without actually exporting

        Returns statistics about what would be exported
        """
        video_ids = self.get_filtered_videos(config_id)

        with self.db.get_connection() as conn:
            cursor = conn.cursor()

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
                    activity_tag = row[0]
                    count = row[1]
                    if activity_tag in class_counts:
                        class_counts[activity_tag] += count
                        total_annotations += count

        return {
            'video_count': len(video_ids),
            'total_annotations': total_annotations,
            'class_distribution': class_counts,
            'config': config
        }
