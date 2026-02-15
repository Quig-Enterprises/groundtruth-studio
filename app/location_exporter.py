"""
Location Classification Dataset Exporter

Exports frames with location labels for training scene/location recognition models.
Data sources:
1. Frames with location_context annotations + location_name tag (manually annotated)
2. All thumbnails/frames from cameras with registered locations (auto-labeled via MAC mapping)
"""

import os
import json
import shutil
import random
import logging
from pathlib import Path
from datetime import datetime

from db_connection import get_cursor, get_connection
from psycopg2 import extras

logger = logging.getLogger(__name__)


class LocationExporter:
    def __init__(self, db, export_dir, thumbnail_dir, download_dir):
        self.db = db
        self.export_dir = Path(export_dir)
        self.thumbnail_dir = Path(thumbnail_dir)
        self.download_dir = Path(download_dir)

    def get_export_stats(self):
        """Get statistics about available training data for location classification."""
        stats = {
            'locations': [],
            'total_frames': 0,
            'total_locations': 0,
            'sources': {
                'manual_annotations': 0,
                'camera_mappings': 0
            }
        }

        with get_cursor(commit=False) as cursor:
            # Source 1: Manual annotations with location_name in scenario data
            cursor.execute("""
                SELECT at2.tag_value, COUNT(DISTINCT ka.id) as frame_count
                FROM keyframe_annotations ka
                JOIN annotation_tags at2 ON ka.id = at2.annotation_id AND at2.annotation_type = 'keyframe'
                JOIN tag_groups tg ON at2.group_id = tg.id
                WHERE tg.group_name = '_scenario_data'
                AND at2.tag_value LIKE '%%location_name%%'
                GROUP BY at2.tag_value
            """)
            manual_rows = cursor.fetchall()

            manual_locations = {}
            for row in manual_rows:
                try:
                    data = json.loads(row['tag_value'])
                    loc_name = None
                    if 'tags' in data and 'location_name' in data['tags']:
                        loc_name = data['tags']['location_name']
                    elif 'location_name' in data:
                        loc_name = data['location_name']
                    if loc_name:
                        manual_locations[loc_name] = manual_locations.get(loc_name, 0) + row['frame_count']
                except (json.JSONDecodeError, KeyError):
                    pass

            stats['sources']['manual_annotations'] = sum(manual_locations.values())

            # Source 2: Camera-mapped frames (auto-labeled)
            cursor.execute("""
                SELECT cl.location_name, cl.camera_id, cl.camera_name,
                       COUNT(DISTINCT v.id) as frame_count
                FROM camera_locations cl
                JOIN videos v ON v.camera_id = cl.camera_id
                WHERE v.thumbnail_path IS NOT NULL
                GROUP BY cl.location_name, cl.camera_id, cl.camera_name
            """)
            camera_rows = cursor.fetchall()

            camera_locations = {}
            for row in camera_rows:
                loc_name = row['location_name']
                camera_locations[loc_name] = camera_locations.get(loc_name, 0) + row['frame_count']

            stats['sources']['camera_mappings'] = sum(camera_locations.values())

            # Merge location counts
            all_locations = {}
            for loc, count in manual_locations.items():
                all_locations[loc] = all_locations.get(loc, 0) + count
            for loc, count in camera_locations.items():
                all_locations[loc] = all_locations.get(loc, 0) + count

            stats['total_locations'] = len(all_locations)
            stats['total_frames'] = sum(all_locations.values())

            for loc_name, count in sorted(all_locations.items(), key=lambda x: -x[1]):
                manual_count = manual_locations.get(loc_name, 0)
                auto_count = camera_locations.get(loc_name, 0)
                stats['locations'].append({
                    'location_name': loc_name,
                    'total_frames': count,
                    'manual_frames': manual_count,
                    'auto_frames': auto_count
                })

        return stats

    def _collect_frames(self):
        """Collect all frames with location labels from both sources.

        Returns list of dicts: [{path, location_name, source, video_id}, ...]
        """
        frames = []
        seen_paths = set()

        with get_cursor(commit=False) as cursor:
            # Source 1: Manual annotations with location_name
            cursor.execute("""
                SELECT ka.id, ka.video_id, v.thumbnail_path, v.filename,
                       at2.tag_value as scenario_data
                FROM keyframe_annotations ka
                JOIN videos v ON ka.video_id = v.id
                JOIN annotation_tags at2 ON ka.id = at2.annotation_id AND at2.annotation_type = 'keyframe'
                JOIN tag_groups tg ON at2.group_id = tg.id
                WHERE tg.group_name = '_scenario_data'
                AND at2.tag_value LIKE '%%location_name%%'
            """)

            for row in cursor.fetchall():
                try:
                    data = json.loads(row['scenario_data'])
                    loc_name = None
                    if 'tags' in data and 'location_name' in data['tags']:
                        loc_name = data['tags']['location_name']
                    elif 'location_name' in data:
                        loc_name = data['location_name']

                    if loc_name and row['thumbnail_path']:
                        path = row['thumbnail_path']
                        if path not in seen_paths:
                            seen_paths.add(path)
                            frames.append({
                                'path': path,
                                'location_name': loc_name,
                                'source': 'manual',
                                'video_id': row['video_id']
                            })
                except (json.JSONDecodeError, KeyError):
                    pass

            # Source 2: Camera-mapped frames (auto-labeled via MAC)
            cursor.execute("""
                SELECT v.id as video_id, v.thumbnail_path, v.filename,
                       cl.location_name
                FROM videos v
                JOIN camera_locations cl ON v.camera_id = cl.camera_id
                WHERE v.thumbnail_path IS NOT NULL
            """)

            for row in cursor.fetchall():
                path = row['thumbnail_path']
                if path not in seen_paths:
                    seen_paths.add(path)
                    frames.append({
                        'path': path,
                        'location_name': row['location_name'],
                        'source': 'camera_mapping',
                        'video_id': row['video_id']
                    })

        return frames

    def export_dataset(self, output_dir=None, format='imagefolder', val_split=0.2, seed=42):
        """
        Export frames with location labels for training.

        Args:
            output_dir: Output directory path. Auto-generated if None.
            format: 'imagefolder' for PyTorch ImageFolder structure, 'csv' for CSV labels
            val_split: Fraction of data to use for validation (0.0-1.0)
            seed: Random seed for reproducible splits

        Returns:
            dict with export results
        """
        random.seed(seed)

        # Collect all labeled frames
        frames = self._collect_frames()

        if not frames:
            return {
                'success': False,
                'error': 'No frames with location labels found. Map cameras to locations or annotate frames with location_context scenario.'
            }

        # Create output directory
        if output_dir is None:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            output_dir = self.export_dir / f'location_dataset_{timestamp}'
        else:
            output_dir = Path(output_dir)

        output_dir.mkdir(parents=True, exist_ok=True)

        # Group frames by location
        by_location = {}
        for frame in frames:
            loc = frame['location_name']
            if loc not in by_location:
                by_location[loc] = []
            by_location[loc].append(frame)

        # Split into train/val
        train_frames = []
        val_frames = []
        for loc, loc_frames in by_location.items():
            random.shuffle(loc_frames)
            split_idx = max(1, int(len(loc_frames) * (1 - val_split)))
            train_frames.extend(loc_frames[:split_idx])
            val_frames.extend(loc_frames[split_idx:])

        if format == 'imagefolder':
            result = self._export_imagefolder(output_dir, train_frames, val_frames, by_location)
        elif format == 'csv':
            result = self._export_csv(output_dir, train_frames, val_frames)
        else:
            return {'success': False, 'error': f'Unknown format: {format}'}

        result['total_frames'] = len(frames)
        result['train_frames'] = len(train_frames)
        result['val_frames'] = len(val_frames)
        result['locations'] = len(by_location)
        result['location_counts'] = {loc: len(f) for loc, f in by_location.items()}
        result['export_path'] = str(output_dir)
        result['format'] = format

        # Save metadata
        meta_path = output_dir / 'dataset_meta.json'
        with open(meta_path, 'w') as f:
            json.dump({
                'created': datetime.now().isoformat(),
                'format': format,
                'val_split': val_split,
                'seed': seed,
                'total_frames': result['total_frames'],
                'train_frames': result['train_frames'],
                'val_frames': result['val_frames'],
                'locations': result['location_counts']
            }, f, indent=2)

        return result

    def _export_imagefolder(self, output_dir, train_frames, val_frames, by_location):
        """Export in PyTorch ImageFolder format: train/{location}/frame.jpg"""
        # Create directories
        for split_name, split_frames in [('train', train_frames), ('val', val_frames)]:
            for loc in by_location.keys():
                safe_loc = loc.replace(' ', '_').replace('/', '_')
                (output_dir / split_name / safe_loc).mkdir(parents=True, exist_ok=True)

        copied = 0
        errors = 0

        for split_name, split_frames in [('train', train_frames), ('val', val_frames)]:
            for i, frame in enumerate(split_frames):
                src_path = Path(frame['path'])
                if not src_path.is_absolute():
                    # Try thumbnail dir
                    src_path = self.thumbnail_dir / src_path.name

                if not src_path.exists():
                    # Try just the filename in thumbnail dir
                    src_path = self.thumbnail_dir / Path(frame['path']).name

                if not src_path.exists():
                    errors += 1
                    continue

                safe_loc = frame['location_name'].replace(' ', '_').replace('/', '_')
                ext = src_path.suffix or '.jpg'
                dst_path = output_dir / split_name / safe_loc / f'frame_{frame["video_id"]}_{i:04d}{ext}'

                try:
                    shutil.copy2(str(src_path), str(dst_path))
                    copied += 1
                except Exception as e:
                    logger.warning(f"Failed to copy {src_path}: {e}")
                    errors += 1

        return {
            'success': True,
            'copied_frames': copied,
            'copy_errors': errors
        }

    def _export_csv(self, output_dir, train_frames, val_frames):
        """Export as CSV with frame paths and labels."""
        import csv

        frames_dir = output_dir / 'frames'
        frames_dir.mkdir(parents=True, exist_ok=True)

        copied = 0
        errors = 0
        rows = []

        for split_name, split_frames in [('train', train_frames), ('val', val_frames)]:
            for i, frame in enumerate(split_frames):
                src_path = Path(frame['path'])
                if not src_path.is_absolute():
                    src_path = self.thumbnail_dir / src_path.name

                if not src_path.exists():
                    src_path = self.thumbnail_dir / Path(frame['path']).name

                if not src_path.exists():
                    errors += 1
                    continue

                ext = src_path.suffix or '.jpg'
                dst_filename = f'{split_name}_{frame["video_id"]}_{i:04d}{ext}'
                dst_path = frames_dir / dst_filename

                try:
                    shutil.copy2(str(src_path), str(dst_path))
                    copied += 1
                    rows.append({
                        'path': f'frames/{dst_filename}',
                        'location_name': frame['location_name'],
                        'split': split_name,
                        'source': frame['source'],
                        'video_id': frame['video_id']
                    })
                except Exception as e:
                    logger.warning(f"Failed to copy {src_path}: {e}")
                    errors += 1

        # Write CSV
        csv_path = output_dir / 'labels.csv'
        with open(csv_path, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=['path', 'location_name', 'split', 'source', 'video_id'])
            writer.writeheader()
            writer.writerows(rows)

        return {
            'success': True,
            'copied_frames': copied,
            'copy_errors': errors,
            'csv_path': str(csv_path)
        }

    def export_for_training(self, format='imagefolder', val_split=0.2, seed=42, output_name=None):
        """
        Export dataset and return path ready for training queue submission.

        This is a convenience wrapper around export_dataset that generates
        a named output directory suitable for S3 upload.

        Args:
            format: Export format ('imagefolder' or 'csv')
            val_split: Validation split fraction
            seed: Random seed for reproducibility
            output_name: Optional custom name for the export directory

        Returns:
            dict with export results including 'export_path' for training queue
        """
        if output_name:
            output_dir = self.export_dir / output_name
        else:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            output_dir = self.export_dir / f'location_training_{timestamp}'

        result = self.export_dataset(
            output_dir=str(output_dir),
            format=format,
            val_split=val_split,
            seed=seed
        )

        return result
