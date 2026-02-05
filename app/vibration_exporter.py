"""
Vibration Data Exporter
Exports time-range tag annotations as Parquet and/or CSV
for bearing fault and other vibration-based training jobs.
"""

import csv
import json
import random
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import psycopg2.extras
from database import VideoDatabase
from db_connection import get_connection

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
    HAS_PARQUET = True
except ImportError:
    HAS_PARQUET = False


class VibrationExporter:
    def __init__(self, db: VideoDatabase, export_base_dir: Path):
        self.db = db
        self.export_base_dir = export_base_dir
        self.export_base_dir.mkdir(parents=True, exist_ok=True)

    def export_dataset(self, output_name: str = None, tag_filter: str = None,
                       formats: List[str] = None, val_split: float = 0.2,
                       seed: int = 42) -> Dict:
        """
        Export time-range annotations as structured training data.

        Args:
            output_name: Output directory name (auto-generated if None)
            tag_filter: Only include time-range tags matching this name (None = all)
            formats: List of output formats ('csv', 'parquet', or both). Default: both.
            val_split: Fraction of data for validation (default 0.2)
            seed: Random seed for reproducible splits

        Returns:
            Dict with export statistics and paths
        """
        if formats is None:
            formats = ['csv', 'parquet'] if HAS_PARQUET else ['csv']

        if 'parquet' in formats and not HAS_PARQUET:
            formats = [f for f in formats if f != 'parquet']
            formats.append('csv')

        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

            # Query all time-range tags with video metadata
            if tag_filter:
                cursor.execute('''
                    SELECT trt.*, v.title as video_title, v.filename, v.duration as video_duration
                    FROM time_range_tags trt
                    JOIN videos v ON trt.video_id = v.id
                    WHERE trt.tag_name = %s
                    ORDER BY v.id, trt.start_time
                ''', (tag_filter,))
            else:
                cursor.execute('''
                    SELECT trt.*, v.title as video_title, v.filename, v.duration as video_duration
                    FROM time_range_tags trt
                    JOIN videos v ON trt.video_id = v.id
                    ORDER BY v.id, trt.start_time
                ''')

            rows = [dict(r) for r in cursor.fetchall()]

            # Also grab annotation tags for each time-range tag
            for row in rows:
                cursor.execute('''
                    SELECT tg.group_name, at.tag_value
                    FROM annotation_tags at
                    JOIN tag_groups tg ON at.group_id = tg.id
                    WHERE at.annotation_id = %s AND at.annotation_type = 'time_range'
                ''', (row['id'],))
                row['tags'] = {r['group_name']: r['tag_value'] for r in cursor.fetchall()}

        if not rows:
            return {
                'success': True,
                'export_path': None,
                'total_samples': 0,
                'message': 'No time-range annotations found'
            }

        # Create export directory
        if not output_name:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            output_name = f'vibration_export_{timestamp}'

        export_dir = self.export_base_dir / output_name
        export_dir.mkdir(parents=True, exist_ok=True)

        # Shuffle and split
        rng = random.Random(seed)
        rng.shuffle(rows)
        val_size = max(1, int(len(rows) * val_split)) if len(rows) > 1 else 0

        train_rows = rows[val_size:]
        val_rows = rows[:val_size]

        # Build sample records
        fields = [
            'sample_id', 'video_id', 'video_title', 'filename',
            'tag_name', 'start_time', 'end_time', 'duration',
            'is_negative', 'comment', 'label', 'split'
        ]

        samples = []
        for split_name, split_rows in [('train', train_rows), ('val', val_rows)]:
            for i, row in enumerate(split_rows):
                duration = (row['end_time'] - row['start_time']) if row['end_time'] else None
                label = row['tag_name']

                # Use ground_truth tag if available
                if 'ground_truth' in row.get('tags', {}):
                    label = row['tags']['ground_truth']

                sample = {
                    'sample_id': f'{split_name}_{i:05d}',
                    'video_id': row['video_id'],
                    'video_title': row['video_title'],
                    'filename': row['filename'],
                    'tag_name': row['tag_name'],
                    'start_time': row['start_time'],
                    'end_time': row['end_time'],
                    'duration': duration,
                    'is_negative': bool(row['is_negative']),
                    'comment': row.get('comment') or '',
                    'label': label,
                    'split': split_name,
                }

                # Add all structured tags as extra columns
                for tag_key, tag_val in row.get('tags', {}).items():
                    if tag_key not in sample:
                        sample[tag_key] = tag_val
                        if tag_key not in fields:
                            fields.append(tag_key)

                samples.append(sample)

        # Write CSV
        if 'csv' in formats:
            for split_name in ('train', 'val'):
                split_samples = [s for s in samples if s['split'] == split_name]
                if not split_samples:
                    continue
                csv_path = export_dir / f'{split_name}_samples.csv'
                with open(csv_path, 'w', newline='') as f:
                    writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
                    writer.writeheader()
                    writer.writerows(split_samples)

            # Also write combined
            csv_all_path = export_dir / 'all_samples.csv'
            with open(csv_all_path, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
                writer.writeheader()
                writer.writerows(samples)

        # Write Parquet
        if 'parquet' in formats and HAS_PARQUET:
            for split_name in ('train', 'val'):
                split_samples = [s for s in samples if s['split'] == split_name]
                if not split_samples:
                    continue
                table = pa.Table.from_pylist(split_samples)
                pq.write_table(table, export_dir / f'{split_name}_samples.parquet')

            table_all = pa.Table.from_pylist(samples)
            pq.write_table(table_all, export_dir / 'all_samples.parquet')

        # Write manifest
        unique_labels = sorted(set(s['label'] for s in samples))
        label_counts = {}
        for s in samples:
            label_counts[s['label']] = label_counts.get(s['label'], 0) + 1

        manifest = {
            'export_date': datetime.now().isoformat(),
            'total_samples': len(samples),
            'train_samples': len(train_rows),
            'val_samples': len(val_rows),
            'labels': unique_labels,
            'label_distribution': label_counts,
            'formats': formats,
            'tag_filter': tag_filter,
            'val_split': val_split,
            'fields': fields,
        }

        with open(export_dir / 'manifest.json', 'w') as f:
            json.dump(manifest, f, indent=2)

        return {
            'success': True,
            'export_path': str(export_dir),
            'total_samples': len(samples),
            'train_count': len(train_rows),
            'val_count': len(val_rows),
            'labels': unique_labels,
            'label_distribution': label_counts,
            'formats': formats,
        }

    def get_available_tags(self) -> List[Dict]:
        """Get all unique time-range tag names with counts."""
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cursor.execute('''
                SELECT tag_name, COUNT(*) as count,
                       SUM(CASE WHEN is_negative = true THEN 1 ELSE 0 END) as negative_count
                FROM time_range_tags
                GROUP BY tag_name
                ORDER BY count DESC
            ''')
            rows = [dict(r) for r in cursor.fetchall()]
        return rows
