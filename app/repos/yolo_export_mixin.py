import json
from datetime import datetime
from typing import List, Dict, Optional

import psycopg2
from psycopg2 import extras

from db_connection import get_cursor


class YoloExportMixin:
    """YOLO export configuration, filters, and logs."""

    # YOLO Export Configuration Management
    def create_yolo_export_config(self, config_name: str, class_mapping: str,
                                  description: str = None,
                                  include_reviewed_only: bool = False,
                                  include_ai_generated: bool = True,
                                  include_negative_examples: bool = True,
                                  min_confidence: float = 0.0,
                                  export_format: str = 'yolov8') -> int:
        """Create a new YOLO export configuration"""
        with get_cursor() as cursor:
            cursor.execute('''
                INSERT INTO yolo_export_configs
                (config_name, description, class_mapping, include_reviewed_only,
                 include_ai_generated, include_negative_examples, min_confidence, export_format)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            ''', (config_name, description, class_mapping, include_reviewed_only,
                  include_ai_generated, include_negative_examples, min_confidence, export_format))
            result = cursor.fetchone()
            return result['id']

    def get_yolo_export_config(self, config_id: int) -> Optional[Dict]:
        """Get a YOLO export config by ID"""
        with get_cursor(commit=False) as cursor:
            cursor.execute('SELECT * FROM yolo_export_configs WHERE id = %s', (config_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_all_yolo_export_configs(self) -> List[Dict]:
        """Get all YOLO export configurations"""
        with get_cursor(commit=False) as cursor:
            cursor.execute('SELECT * FROM yolo_export_configs ORDER BY config_name')
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def update_yolo_export_config(self, config_id: int, **kwargs) -> bool:
        """Update a YOLO export configuration"""
        allowed_fields = ['config_name', 'description', 'class_mapping', 'include_reviewed_only',
                         'include_ai_generated', 'include_negative_examples', 'min_confidence',
                         'export_format', 'last_export_date', 'last_export_count']

        updates = []
        values = []
        for field, value in kwargs.items():
            if field in allowed_fields:
                updates.append(f'{field} = %s')
                values.append(value)

        if not updates:
            return False

        values.append(config_id)
        with get_cursor() as cursor:
            cursor.execute(f'''
                UPDATE yolo_export_configs SET {', '.join(updates)}
                WHERE id = %s
            ''', values)
            return cursor.rowcount > 0

    def delete_yolo_export_config(self, config_id: int) -> bool:
        """Delete a YOLO export configuration"""
        with get_cursor() as cursor:
            cursor.execute('DELETE FROM yolo_export_configs WHERE id = %s', (config_id,))
            return cursor.rowcount > 0

    def add_video_to_export_config(self, config_id: int, video_id: int,
                                   included: bool = True, notes: str = None) -> int:
        """Add a video to an export configuration"""
        with get_cursor() as cursor:
            try:
                cursor.execute('''
                    INSERT INTO yolo_export_videos (export_config_id, video_id, included, notes)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id
                ''', (config_id, video_id, included, notes))
                result = cursor.fetchone()
                return result['id']
            except psycopg2.IntegrityError:
                # Already exists, update instead
                cursor.connection.rollback()
                cursor.execute('''
                    UPDATE yolo_export_videos SET included = %s, notes = %s
                    WHERE export_config_id = %s AND video_id = %s
                ''', (included, notes, config_id, video_id))
                return -1  # Indicate update instead of insert

    def get_export_config_videos(self, config_id: int, included_only: bool = True) -> List[Dict]:
        """Get all videos for an export configuration"""
        with get_cursor(commit=False) as cursor:
            if included_only:
                cursor.execute('''
                    SELECT v.*, ev.included, ev.notes as export_notes
                    FROM videos v
                    JOIN yolo_export_videos ev ON v.id = ev.video_id
                    WHERE ev.export_config_id = %s AND ev.included = true
                    ORDER BY v.filename
                ''', (config_id,))
            else:
                cursor.execute('''
                    SELECT v.*, ev.included, ev.notes as export_notes
                    FROM videos v
                    JOIN yolo_export_videos ev ON v.id = ev.video_id
                    WHERE ev.export_config_id = %s
                    ORDER BY v.filename
                ''', (config_id,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def add_export_filter(self, config_id: int, filter_type: str,
                         filter_value: str, is_exclusion: bool = False) -> int:
        """Add a filter rule to an export configuration"""
        with get_cursor() as cursor:
            cursor.execute('''
                INSERT INTO yolo_export_filters (export_config_id, filter_type, filter_value, is_exclusion)
                VALUES (%s, %s, %s, %s)
                RETURNING id
            ''', (config_id, filter_type, filter_value, is_exclusion))
            result = cursor.fetchone()
            return result['id']

    def get_export_filters(self, config_id: int) -> List[Dict]:
        """Get all filters for an export configuration"""
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT * FROM yolo_export_filters
                WHERE export_config_id = %s
                ORDER BY filter_type
            ''', (config_id,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def log_export(self, config_id: int, export_path: str, video_count: int,
                   annotation_count: int, export_format: str, notes: str = None) -> int:
        """Log an export operation"""
        with get_cursor() as cursor:
            cursor.execute('''
                INSERT INTO yolo_export_logs
                (export_config_id, export_path, video_count, annotation_count, export_format, notes)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
            ''', (config_id, export_path, video_count, annotation_count, export_format, notes))
            result = cursor.fetchone()

            # Update the config's last export info
            cursor.execute('''
                UPDATE yolo_export_configs
                SET last_export_date = CURRENT_TIMESTAMP, last_export_count = %s
                WHERE id = %s
            ''', (annotation_count, config_id))

            return result['id']

    def get_export_logs(self, config_id: int = None, limit: int = 50) -> List[Dict]:
        """Get export logs, optionally filtered by config"""
        with get_cursor(commit=False) as cursor:
            if config_id:
                cursor.execute('''
                    SELECT el.*, ec.config_name
                    FROM yolo_export_logs el
                    JOIN yolo_export_configs ec ON el.export_config_id = ec.id
                    WHERE el.export_config_id = %s
                    ORDER BY el.export_date DESC
                    LIMIT %s
                ''', (config_id, limit))
            else:
                cursor.execute('''
                    SELECT el.*, ec.config_name
                    FROM yolo_export_logs el
                    JOIN yolo_export_configs ec ON el.export_config_id = ec.id
                    ORDER BY el.export_date DESC
                    LIMIT %s
                ''', (limit,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_annotations_for_export(self, config_id: int) -> List[Dict]:
        """Get all annotations for videos in an export configuration"""
        with get_cursor(commit=False) as cursor:
            config = self.get_yolo_export_config(config_id)
            if not config:
                return []

            query = '''
                SELECT ka.*, v.filename, v.width as video_width, v.height as video_height
                FROM keyframe_annotations ka
                JOIN videos v ON ka.video_id = v.id
                JOIN yolo_export_videos ev ON v.id = ev.video_id
                WHERE ev.export_config_id = %s AND ev.included = true
            '''
            params = [config_id]

            if config.get('include_reviewed_only'):
                query += ' AND ka.reviewed = true'

            if config.get('min_confidence', 0) > 0:
                # Note: confidence filtering would require an additional column
                pass

            query += ' ORDER BY v.filename, ka.timestamp'

            cursor.execute(query, params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
