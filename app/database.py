import json
import math
import os
import uuid
from datetime import datetime
from typing import List, Dict, Optional

import psycopg2
from psycopg2 import extras

from db_connection import get_connection, get_cursor

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False


def _cosine_similarity(vec_a, vec_b):
    """Compute cosine similarity between two vectors."""
    if _HAS_NUMPY:
        a = np.array(vec_a, dtype=np.float32)
        b = np.array(vec_b, dtype=np.float32)
        dot = np.dot(a, b)
        norm = np.linalg.norm(a) * np.linalg.norm(b)
        return float(dot / norm) if norm > 0 else 0.0
    else:
        dot = sum(a * b for a, b in zip(vec_a, vec_b))
        norm_a = math.sqrt(sum(a * a for a in vec_a))
        norm_b = math.sqrt(sum(b * b for b in vec_b))
        norm = norm_a * norm_b
        return dot / norm if norm > 0 else 0.0


class VideoDatabase:
    def __init__(self, db_path=None):
        """
        Initialize VideoDatabase.

        Args:
            db_path: Ignored (kept for backwards compatibility).
                     PostgreSQL connection is configured via DATABASE_URL env var.
        """
        # db_path parameter kept for API compatibility but is ignored
        # Schema initialization is handled by app.schema module
        pass

    def add_video(self, filename: str, original_url: str = None, title: str = None,
                  duration: float = None, width: int = None, height: int = None,
                  file_size: int = None, thumbnail_path: str = None, notes: str = None,
                  camera_id: str = None) -> int:
        with get_cursor() as cursor:
            cursor.execute('''
                INSERT INTO videos (filename, original_url, title, duration, width, height,
                                  file_size, thumbnail_path, notes, camera_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (filename) DO NOTHING
                RETURNING id
            ''', (filename, original_url, title, duration, width, height, file_size, thumbnail_path, notes, camera_id))
            result = cursor.fetchone()
            if result:
                return result['id']
            # Row already existed — look it up
            cursor.execute('SELECT id FROM videos WHERE filename = %s', (filename,))
            existing = cursor.fetchone()
            return existing['id']

    def get_video(self, video_id: int) -> Optional[Dict]:
        with get_cursor(commit=False) as cursor:
            cursor.execute('SELECT * FROM videos WHERE id = %s', (video_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def _is_default_library(self, library_id: int) -> bool:
        """Check if a library is the default (Uncategorized) library."""
        with get_cursor(commit=False) as cursor:
            cursor.execute('SELECT is_default FROM content_libraries WHERE id = %s', (library_id,))
            row = cursor.fetchone()
            return bool(row and row['is_default'])

    def get_all_videos(self, limit: int = 100, offset: int = 0, library_id: int = None) -> List[Dict]:
        with get_cursor(commit=False) as cursor:
            if library_id is not None and self._is_default_library(library_id):
                # Default library: show videos NOT in any non-default library
                cursor.execute('''
                    SELECT v.*,
                           STRING_AGG(DISTINCT t.name, ', ') as tags,
                           COUNT(DISTINCT ka.id) as annotation_count
                    FROM videos v
                    LEFT JOIN video_tags vt ON v.id = vt.video_id
                    LEFT JOIN tags t ON vt.tag_id = t.id
                    LEFT JOIN keyframe_annotations ka ON v.id = ka.video_id
                    WHERE NOT EXISTS (
                        SELECT 1 FROM content_library_items cli2
                        INNER JOIN content_libraries cl2 ON cli2.library_id = cl2.id
                        WHERE cli2.video_id = v.id AND cl2.is_default = FALSE
                    )
                    GROUP BY v.id
                    ORDER BY v.upload_date DESC
                    LIMIT %s OFFSET %s
                ''', (limit, offset))
            elif library_id is not None:
                cursor.execute('''
                    SELECT v.*,
                           STRING_AGG(DISTINCT t.name, ', ') as tags,
                           COUNT(DISTINCT ka.id) as annotation_count
                    FROM videos v
                    INNER JOIN content_library_items cli ON v.id = cli.video_id AND cli.library_id = %s
                    LEFT JOIN video_tags vt ON v.id = vt.video_id
                    LEFT JOIN tags t ON vt.tag_id = t.id
                    LEFT JOIN keyframe_annotations ka ON v.id = ka.video_id
                    GROUP BY v.id
                    ORDER BY v.upload_date DESC
                    LIMIT %s OFFSET %s
                ''', (library_id, limit, offset))
            else:
                cursor.execute('''
                    SELECT v.*,
                           STRING_AGG(DISTINCT t.name, ', ') as tags,
                           COUNT(DISTINCT ka.id) as annotation_count
                    FROM videos v
                    LEFT JOIN video_tags vt ON v.id = vt.video_id
                    LEFT JOIN tags t ON vt.tag_id = t.id
                    LEFT JOIN keyframe_annotations ka ON v.id = ka.video_id
                    GROUP BY v.id
                    ORDER BY v.upload_date DESC
                    LIMIT %s OFFSET %s
                ''', (limit, offset))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def search_videos(self, query: str) -> List[Dict]:
        with get_cursor(commit=False) as cursor:
            search_term = f'%{query}%'
            cursor.execute('''
                SELECT v.*,
                       (SELECT STRING_AGG(DISTINCT t2.name, ', ')
                        FROM video_tags vt2
                        JOIN tags t2 ON vt2.tag_id = t2.id
                        WHERE vt2.video_id = v.id) as tags,
                       (SELECT COUNT(*)
                        FROM keyframe_annotations ka2
                        WHERE ka2.video_id = v.id) as annotation_count
                FROM videos v
                LEFT JOIN video_tags vt ON v.id = vt.video_id
                LEFT JOIN tags t ON vt.tag_id = t.id
                LEFT JOIN keyframe_annotations ka ON v.id = ka.video_id
                LEFT JOIN annotation_tags at ON ka.id = at.annotation_id AND at.annotation_type = 'keyframe'
                LEFT JOIN tag_groups tg ON at.group_id = tg.id
                WHERE v.title LIKE %s OR v.filename LIKE %s OR v.notes LIKE %s OR t.name LIKE %s
                   OR ka.activity_tag LIKE %s OR ka.comment LIKE %s
                   OR tg.group_name LIKE %s OR at.tag_value LIKE %s
                GROUP BY v.id
                ORDER BY v.upload_date DESC
            ''', (search_term, search_term, search_term, search_term,
                  search_term, search_term, search_term, search_term))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def add_tag(self, name: str, category: str = None) -> int:
        with get_cursor() as cursor:
            try:
                cursor.execute('''
                    INSERT INTO tags (name, category) VALUES (%s, %s)
                    RETURNING id
                ''', (name, category))
                result = cursor.fetchone()
                return result['id']
            except psycopg2.IntegrityError:
                # Tag already exists, fetch its id
                cursor.connection.rollback()
                cursor.execute('SELECT id FROM tags WHERE name = %s', (name,))
                result = cursor.fetchone()
                return result['id']

    def tag_video(self, video_id: int, tag_name: str) -> bool:
        tag_id = self.add_tag(tag_name)
        with get_cursor() as cursor:
            try:
                cursor.execute('INSERT INTO video_tags (video_id, tag_id) VALUES (%s, %s)',
                             (video_id, tag_id))
                return True
            except psycopg2.IntegrityError:
                return False

    def untag_video(self, video_id: int, tag_name: str) -> bool:
        with get_cursor() as cursor:
            cursor.execute('''
                DELETE FROM video_tags
                WHERE video_id = %s AND tag_id = (SELECT id FROM tags WHERE name = %s)
            ''', (video_id, tag_name))
            return cursor.rowcount > 0

    def get_all_tags(self) -> List[Dict]:
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT t.*, COUNT(vt.video_id) as video_count
                FROM tags t
                LEFT JOIN video_tags vt ON t.id = vt.tag_id
                GROUP BY t.id
                ORDER BY t.name
            ''')
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def add_behavior_annotation(self, video_id: int, behavior_type: str,
                               start_time: float = None, end_time: float = None,
                               confidence: float = None, notes: str = None) -> int:
        with get_cursor() as cursor:
            cursor.execute('''
                INSERT INTO behaviors (video_id, behavior_type, start_time, end_time, confidence, notes)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
            ''', (video_id, behavior_type, start_time, end_time, confidence, notes))
            result = cursor.fetchone()
            return result['id']

    def get_video_behaviors(self, video_id: int) -> List[Dict]:
        with get_cursor(commit=False) as cursor:
            cursor.execute('SELECT * FROM behaviors WHERE video_id = %s ORDER BY start_time', (video_id,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def delete_video(self, video_id: int) -> bool:
        with get_cursor() as cursor:
            cursor.execute('DELETE FROM videos WHERE id = %s', (video_id,))
            return cursor.rowcount > 0

    def add_time_range_tag(self, video_id: int, tag_name: str, start_time: float,
                          end_time: float = None, is_negative: bool = False, comment: str = None) -> int:
        with get_cursor() as cursor:
            cursor.execute('''
                INSERT INTO time_range_tags (video_id, tag_name, start_time, end_time, is_negative, comment)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
            ''', (video_id, tag_name, start_time, end_time, is_negative, comment))
            result = cursor.fetchone()
            return result['id']

    def update_time_range_tag(self, tag_id: int, tag_name: str = None, end_time: float = None,
                             is_negative: bool = None, comment: str = None) -> bool:
        updates = []
        values = []
        if tag_name is not None:
            updates.append('tag_name = %s')
            values.append(tag_name)
        if end_time is not None:
            updates.append('end_time = %s')
            values.append(end_time)
        if is_negative is not None:
            updates.append('is_negative = %s')
            values.append(is_negative)
        if comment is not None:
            updates.append('comment = %s')
            values.append(comment)

        if not updates:
            return False

        values.append(tag_id)
        with get_cursor() as cursor:
            cursor.execute(f'''
                UPDATE time_range_tags SET {', '.join(updates)}
                WHERE id = %s
            ''', values)
            return cursor.rowcount > 0

    def get_time_range_tags(self, video_id: int) -> List[Dict]:
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT * FROM time_range_tags
                WHERE video_id = %s
                ORDER BY start_time
            ''', (video_id,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_time_range_tag_by_id(self, tag_id: int) -> Dict:
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT * FROM time_range_tags
                WHERE id = %s
            ''', (tag_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def delete_time_range_tag(self, tag_id: int) -> bool:
        with get_cursor() as cursor:
            cursor.execute('DELETE FROM time_range_tags WHERE id = %s', (tag_id,))
            return cursor.rowcount > 0

    def get_all_time_range_tag_names(self) -> List[str]:
        with get_cursor(commit=False) as cursor:
            cursor.execute('SELECT DISTINCT tag_name FROM time_range_tags ORDER BY tag_name')
            rows = cursor.fetchall()
            return [row['tag_name'] for row in rows]

    def add_keyframe_annotation(self, video_id: int, timestamp: float,
                               bbox_x: int, bbox_y: int, bbox_width: int, bbox_height: int,
                               activity_tag: str = None, moment_tag: str = None,
                               is_negative: bool = False, comment: str = None, reviewed: bool = True) -> int:
        with get_cursor() as cursor:
            cursor.execute('''
                INSERT INTO keyframe_annotations
                (video_id, timestamp, bbox_x, bbox_y, bbox_width, bbox_height,
                 activity_tag, moment_tag, is_negative, comment, reviewed)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            ''', (video_id, timestamp, bbox_x, bbox_y, bbox_width, bbox_height,
                  activity_tag, moment_tag, is_negative, comment, reviewed))
            result = cursor.fetchone()
            return result['id']

    def get_keyframe_annotations(self, video_id: int) -> List[Dict]:
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT * FROM keyframe_annotations
                WHERE video_id = %s
                ORDER BY timestamp
            ''', (video_id,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_annotation_count(self, video_id: int) -> int:
        """Get total count of keyframe annotations for a video"""
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT COUNT(*) as count FROM keyframe_annotations
                WHERE video_id = %s
            ''', (video_id,))
            result = cursor.fetchone()
            return result['count'] if result else 0

    def get_bboxes_for_video_ids(self, video_ids: list) -> dict:
        """Get bboxes from keyframe annotations and AI predictions for multiple videos"""
        if not video_ids:
            return {}
        with get_cursor(commit=False) as cursor:
            placeholders = ','.join(['%s'] * len(video_ids))
            # Get keyframe annotations
            cursor.execute(f'''
                SELECT video_id, bbox_x, bbox_y, bbox_width, bbox_height, reviewed
                FROM keyframe_annotations
                WHERE video_id IN ({placeholders})
                AND bbox_x IS NOT NULL AND bbox_width > 0
            ''', video_ids)
            rows = cursor.fetchall()
            result = {}
            for row in rows:
                vid = row['video_id']
                if vid not in result:
                    result[vid] = []
                result[vid].append({
                    'x': row['bbox_x'],
                    'y': row['bbox_y'],
                    'w': row['bbox_width'],
                    'h': row['bbox_height'],
                    'reviewed': bool(row['reviewed'])
                })
            # Get AI predictions (always shown as unvalidated)
            cursor.execute(f'''
                SELECT video_id, bbox_x, bbox_y, bbox_width, bbox_height
                FROM ai_predictions
                WHERE video_id IN ({placeholders})
                AND bbox_x IS NOT NULL AND bbox_width > 0
                AND review_status IN ('pending', 'needs_correction')
            ''', video_ids)
            rows = cursor.fetchall()
            for row in rows:
                vid = row['video_id']
                if vid not in result:
                    result[vid] = []
                result[vid].append({
                    'x': row['bbox_x'],
                    'y': row['bbox_y'],
                    'w': row['bbox_width'],
                    'h': row['bbox_height'],
                    'reviewed': False
                })
            return result

    def get_keyframe_annotation_by_id(self, annotation_id: int) -> Dict:
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT * FROM keyframe_annotations
                WHERE id = %s
            ''', (annotation_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def update_keyframe_annotation(self, annotation_id: int, bbox_x: int = None, bbox_y: int = None,
                                  bbox_width: int = None, bbox_height: int = None,
                                  activity_tag: str = None, moment_tag: str = None,
                                  is_negative: bool = None, comment: str = None, reviewed: bool = None) -> bool:
        updates = []
        values = []
        if bbox_x is not None:
            updates.append('bbox_x = %s')
            values.append(bbox_x)
        if bbox_y is not None:
            updates.append('bbox_y = %s')
            values.append(bbox_y)
        if bbox_width is not None:
            updates.append('bbox_width = %s')
            values.append(bbox_width)
        if bbox_height is not None:
            updates.append('bbox_height = %s')
            values.append(bbox_height)
        if activity_tag is not None:
            updates.append('activity_tag = %s')
            values.append(activity_tag)
        if moment_tag is not None:
            updates.append('moment_tag = %s')
            values.append(moment_tag)
        if is_negative is not None:
            updates.append('is_negative = %s')
            values.append(is_negative)
        if comment is not None:
            updates.append('comment = %s')
            values.append(comment)
        if reviewed is not None:
            updates.append('reviewed = %s')
            values.append(reviewed)

        if not updates:
            return False

        values.append(annotation_id)
        with get_cursor() as cursor:
            cursor.execute(f'''
                UPDATE keyframe_annotations SET {', '.join(updates)}
                WHERE id = %s
            ''', values)
            return cursor.rowcount > 0

    def delete_keyframe_annotation(self, annotation_id: int) -> bool:
        with get_cursor() as cursor:
            cursor.execute('DELETE FROM keyframe_annotations WHERE id = %s', (annotation_id,))
            return cursor.rowcount > 0

    def get_all_activity_tags(self) -> List[str]:
        with get_cursor(commit=False) as cursor:
            cursor.execute('SELECT DISTINCT activity_tag FROM keyframe_annotations WHERE activity_tag IS NOT NULL ORDER BY activity_tag')
            rows = cursor.fetchall()
            return [row['activity_tag'] for row in rows]

    def get_all_moment_tags(self) -> List[str]:
        with get_cursor(commit=False) as cursor:
            cursor.execute('SELECT DISTINCT moment_tag FROM keyframe_annotations WHERE moment_tag IS NOT NULL ORDER BY moment_tag')
            rows = cursor.fetchall()
            return [row['moment_tag'] for row in rows]

    def add_tag_suggestion(self, category: str, tag_text: str, is_negative: bool = False,
                          description: str = None, sort_order: int = 0) -> int:
        with get_cursor() as cursor:
            cursor.execute('''
                INSERT INTO tag_suggestions (category, tag_text, is_negative, description, sort_order)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
            ''', (category, tag_text, is_negative, description, sort_order))
            result = cursor.fetchone()
            return result['id']

    def get_tag_suggestions_by_category(self, category: str = None) -> List[Dict]:
        with get_cursor(commit=False) as cursor:
            if category:
                cursor.execute('''
                    SELECT * FROM tag_suggestions
                    WHERE category = %s
                    ORDER BY sort_order, tag_text
                ''', (category,))
            else:
                cursor.execute('SELECT * FROM tag_suggestions ORDER BY category, sort_order, tag_text')
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_all_suggestion_categories(self) -> List[str]:
        with get_cursor(commit=False) as cursor:
            cursor.execute('SELECT DISTINCT category FROM tag_suggestions ORDER BY category')
            rows = cursor.fetchall()
            return [row['category'] for row in rows]

    def update_tag_suggestion(self, suggestion_id: int, category: str = None, tag_text: str = None,
                             is_negative: bool = None, description: str = None, sort_order: int = None) -> bool:
        updates = []
        values = []

        if category is not None:
            updates.append('category = %s')
            values.append(category)
        if tag_text is not None:
            updates.append('tag_text = %s')
            values.append(tag_text)
        if is_negative is not None:
            updates.append('is_negative = %s')
            values.append(is_negative)
        if description is not None:
            updates.append('description = %s')
            values.append(description)
        if sort_order is not None:
            updates.append('sort_order = %s')
            values.append(sort_order)

        if not updates:
            return False

        values.append(suggestion_id)
        with get_cursor() as cursor:
            cursor.execute(f'''
                UPDATE tag_suggestions SET {', '.join(updates)}
                WHERE id = %s
            ''', values)
            return cursor.rowcount > 0

    def delete_tag_suggestion(self, suggestion_id: int) -> bool:
        with get_cursor() as cursor:
            cursor.execute('DELETE FROM tag_suggestions WHERE id = %s', (suggestion_id,))
            return cursor.rowcount > 0

    def seed_default_tag_suggestions(self):
        """Seed database with default tag suggestions"""
        default_suggestions = [
            # Power Loading
            ('power_loading', 'Visually similar to real violation', True, 'Equipment in similar position but not actually loading', 1),
            ('power_loading', 'Equipment idle', True, 'Equipment present but not in operation', 2),
            ('power_loading', 'Different operation type', True, 'Different type of operation that looks similar', 3),

            # License Plate
            ('license_plate', 'Dealer advertisement frame', True, 'Frame text that looks like plate text', 1),
            ('license_plate', 'Obstructed by trailer equipment', True, 'Plate blocked by trailer hitch, bike rack, etc.', 2),
            ('license_plate', 'Aftermarket frame with text', True, 'Custom frame with large text', 3),
            ('license_plate', 'Vehicle text/graphics (not plate)', True, 'Company logos, decals, or vehicle body text', 4),
            ('license_plate', 'Plate clearly visible', False, 'Full plate readable', 5),
            ('license_plate', 'Partial plate visible', False, 'Some characters readable', 6),

            # Person/Face
            ('person_face', 'Face partially visible', True, 'Only part of face shown', 1),
            ('person_face', 'Side profile only', True, 'No frontal view available', 2),
            ('person_face', 'Hat/sunglasses obscuring', True, 'Accessories blocking facial features', 3),
            ('person_face', 'Inside vehicle (through windshield)', True, 'Face visible through glass with reflections', 4),
            ('person_face', 'Distance too far for ID', True, 'Person too far from camera', 5),
            ('person_face', 'Motion blur', True, 'Subject moving causing blur', 6),
            ('person_face', 'Reflection/glare on face', True, 'Lighting issues preventing clear view', 7),
            ('person_face', 'Clear frontal view', False, 'Full face visible and in focus', 8),
        ]

        for category, tag_text, is_negative, description, sort_order in default_suggestions:
            self.add_tag_suggestion(category, tag_text, is_negative, description, sort_order)

    # Tag Group Management
    def add_tag_group(self, group_name: str, display_name: str, group_type: str,
                      description: str = None, is_required: bool = False,
                      applies_to: str = 'both', sort_order: int = 0) -> int:
        """Add a new tag group"""
        with get_cursor() as cursor:
            cursor.execute('''
                INSERT INTO tag_groups (group_name, display_name, group_type, description, is_required, applies_to, sort_order)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            ''', (group_name, display_name, group_type, description, is_required, applies_to, sort_order))
            result = cursor.fetchone()
            return result['id']

    def get_tag_groups(self, annotation_type: str = None) -> List[Dict]:
        """Get all tag groups, optionally filtered by annotation type"""
        with get_cursor(commit=False) as cursor:
            if annotation_type:
                cursor.execute('''
                    SELECT * FROM tag_groups
                    WHERE applies_to IN (%s, 'both')
                    ORDER BY sort_order, display_name
                ''', (annotation_type,))
            else:
                cursor.execute('SELECT * FROM tag_groups ORDER BY sort_order, display_name')
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_tag_group_by_name(self, group_name: str) -> Dict:
        """Get a specific tag group by name"""
        with get_cursor(commit=False) as cursor:
            cursor.execute('SELECT * FROM tag_groups WHERE group_name = %s', (group_name,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def add_tag_option(self, group_id: int, option_value: str, display_text: str,
                       is_negative: bool = False, description: str = None, sort_order: int = 0) -> int:
        """Add an option to a tag group"""
        with get_cursor() as cursor:
            cursor.execute('''
                INSERT INTO tag_options (group_id, option_value, display_text, is_negative, description, sort_order)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
            ''', (group_id, option_value, display_text, is_negative, description, sort_order))
            result = cursor.fetchone()
            return result['id']

    def get_tag_options(self, group_id: int) -> List[Dict]:
        """Get all options for a tag group"""
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT * FROM tag_options
                WHERE group_id = %s
                ORDER BY sort_order, display_text
            ''', (group_id,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def add_annotation_tag(self, annotation_id: int, annotation_type: str,
                           group_id: int, tag_value: str) -> int:
        """Add a tag to an annotation"""
        with get_cursor() as cursor:
            cursor.execute('''
                INSERT INTO annotation_tags (annotation_id, annotation_type, group_id, tag_value)
                VALUES (%s, %s, %s, %s)
                RETURNING id
            ''', (annotation_id, annotation_type, group_id, tag_value))
            result = cursor.fetchone()
            return result['id']

    def get_annotation_tags(self, annotation_id: int, annotation_type: str) -> List[Dict]:
        """Get all tags for an annotation"""
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT at.*, tg.group_name, tg.display_name, tg.group_type
                FROM annotation_tags at
                JOIN tag_groups tg ON at.group_id = tg.id
                WHERE at.annotation_id = %s AND at.annotation_type = %s
                ORDER BY tg.sort_order
            ''', (annotation_id, annotation_type))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def delete_annotation_tags(self, annotation_id: int, annotation_type: str) -> bool:
        """Delete all tags for an annotation"""
        with get_cursor() as cursor:
            cursor.execute('''
                DELETE FROM annotation_tags
                WHERE annotation_id = %s AND annotation_type = %s
            ''', (annotation_id, annotation_type))
            return cursor.rowcount > 0

    def seed_comprehensive_tag_taxonomy(self):
        """Seed database with comprehensive multi-type tag taxonomy from ADVANCED_TAGGING_SPEC.md"""

        # Define tag groups and their options
        taxonomy = [
            # 1. Ground Truth (Required Dropdown)
            {
                'group': ('ground_truth', 'Ground Truth', 'dropdown', 'Select the primary classification', True, 'both', 1),
                'options': [
                    ('power_loading', 'Power Loading', False, 'Boat being loaded using motor power', 1),
                    ('normal_loading', 'Normal Loading', False, 'Boat being loaded using winch only', 2),
                    ('normal_approach', 'Normal Approach', False, 'Boat approaching ramp normally', 3),
                    ('license_plate', 'License Plate', False, 'Vehicle license plate detected', 4),
                    ('boat_registration', 'Boat Registration', False, 'Boat registration number detected', 5),
                    ('face_detected', 'Face Detected', False, 'Human face detected', 6),
                ]
            },

            # 2. Confidence Level (Required Dropdown)
            {
                'group': ('confidence_level', 'Confidence Level', 'dropdown', 'How confident are you in this classification?', True, 'both', 2),
                'options': [
                    ('certain', 'Certain', False, 'Absolutely sure of classification', 1),
                    ('likely', 'Likely', False, 'Probably correct but small doubt', 2),
                    ('unsure', 'Unsure', False, 'Uncertain classification', 3),
                    ('needs_expert_review', 'Needs Expert Review', False, 'Requires additional expertise', 4),
                    ('ambiguous_case', 'Ambiguous Case', False, 'Cannot determine definitively', 5),
                ]
            },

            # 3. False Positive Type - Power Loading (Checkbox)
            {
                'group': ('false_positive_power_loading', 'False Positive Type - Power Loading', 'checkbox',
                         'Why does this look like power loading but is not?', False, 'both', 3),
                'options': [
                    ('motor_running_legitimately', 'Motor Running Legitimately', True, 'Motor on but not for loading', 1),
                    ('natural_water_movement', 'Natural Water Movement', True, 'Water movement not from motor', 2),
                    ('visual_confusion', 'Visual Confusion', True, 'Angle or lighting creates confusion', 3),
                    ('similar_activity', 'Similar Activity', True, 'Different activity that looks similar', 4),
                ]
            },

            # 4. False Positive Type - License Plate (Checkbox)
            {
                'group': ('false_positive_license_plate', 'False Positive Type - License Plate', 'checkbox',
                         'Why does this look like a license plate but is not?', False, 'both', 4),
                'options': [
                    ('vehicle_text_graphics', 'Vehicle Text/Graphics', True, 'Company logos or decals', 1),
                    ('plate_lookalike_object', 'Plate Lookalike Object', True, 'Object resembling a plate', 2),
                    ('poor_plate_visibility', 'Poor Plate Visibility', True, 'Plate too obscured to confirm', 3),
                ]
            },

            # 5. Lighting Conditions (Checkbox)
            {
                'group': ('lighting_conditions', 'Lighting Conditions', 'checkbox',
                         'Environmental lighting factors', False, 'both', 10),
                'options': [
                    ('bright_overexposed', 'Bright/Overexposed', False, 'Very bright, possibly overexposed', 1),
                    ('low_light_dusk', 'Low Light/Dusk', False, 'Poor lighting conditions', 2),
                    ('night_conditions', 'Night Conditions', False, 'Dark/nighttime', 3),
                    ('sun_glare', 'Sun Glare', False, 'Direct sunlight causing glare', 4),
                    ('shadows', 'Shadows', False, 'Significant shadows present', 5),
                ]
            },

            # 6. Weather Conditions (Checkbox)
            {
                'group': ('weather_conditions', 'Weather Conditions', 'checkbox',
                         'Weather factors affecting visibility or behavior', False, 'both', 11),
                'options': [
                    ('rain', 'Rain', False, 'Raining during capture', 1),
                    ('snow', 'Snow', False, 'Snowing during capture', 2),
                    ('fog', 'Fog', False, 'Foggy conditions', 3),
                    ('ice_on_ramp', 'Ice on Ramp', False, 'Icy conditions on boat ramp', 4),
                    ('wind_driven_water', 'Wind-Driven Water', False, 'Strong wind affecting water', 5),
                ]
            },

            # 7. Water Conditions (Checkbox)
            {
                'group': ('water_conditions', 'Water Conditions', 'checkbox',
                         'Water state affecting behavior or visibility', False, 'both', 12),
                'options': [
                    ('rough_water', 'Rough Water', False, 'Choppy or rough water conditions', 1),
                    ('strong_current', 'Strong Current', False, 'Visible water current', 2),
                    ('wave_action', 'Wave Action', False, 'Significant wave activity', 3),
                    ('calm_water', 'Calm Water', False, 'Still, calm water', 4),
                ]
            },

            # 8. Camera Issues (Checkbox)
            {
                'group': ('camera_issues', 'Camera Issues', 'checkbox',
                         'Technical camera problems affecting quality', False, 'both', 20),
                'options': [
                    ('camera_angle_suboptimal', 'Camera Angle Suboptimal', False, 'Poor viewing angle', 1),
                    ('ptz_camera_moving', 'PTZ Camera Moving', False, 'Camera in motion during capture', 2),
                    ('out_of_focus', 'Out of Focus', False, 'Image not in focus', 3),
                    ('motion_blur', 'Motion Blur', False, 'Blur from subject or camera movement', 4),
                    ('compression_artifacts', 'Compression Artifacts', False, 'Video compression degradation', 5),
                    ('frame_rate_insufficient', 'Frame Rate Insufficient', False, 'Too few frames per second', 6),
                ]
            },

            # 9. Visibility Issues (Checkbox)
            {
                'group': ('visibility_issues', 'Visibility Issues', 'checkbox',
                         'Factors reducing visibility of subject', False, 'both', 21),
                'options': [
                    ('obstructed_view', 'Obstructed View', False, 'Object blocking view', 1),
                    ('distance_too_far', 'Distance Too Far', False, 'Subject too far from camera', 2),
                    ('partial_view_only', 'Partial View Only', False, 'Only part of subject visible', 3),
                    ('multiple_subjects_overlapping', 'Multiple Subjects Overlapping', False, 'Subjects obscuring each other', 4),
                ]
            },

            # 10. Violation Context (Checkbox - Time Range Only, Power Loading)
            {
                'group': ('violation_context', 'Violation Context', 'checkbox',
                         'Stage and nature of the violation', False, 'time_range', 30),
                'options': [
                    ('pre_violation_positioning', 'Pre-Violation Positioning', False, 'Setting up for violation', 1),
                    ('violation_in_progress', 'Violation in Progress', False, 'Actively violating', 2),
                    ('post_violation_departure', 'Post-Violation Departure', False, 'Leaving after violation', 3),
                    ('brief_momentary_contact', 'Brief Momentary Contact', False, 'Very short violation', 4),
                    ('extended_violation', 'Extended Violation', False, 'Long duration violation', 5),
                    ('repeated_attempts', 'Repeated Attempts', False, 'Multiple violation attempts', 6),
                ]
            },

            # 11. Motor State (Dropdown - Power Loading)
            {
                'group': ('motor_state', 'Motor State', 'dropdown',
                         'State of the boat motor', False, 'both', 31),
                'options': [
                    ('motor_off', 'Motor Off', False, 'Motor not running', 1),
                    ('motor_idling', 'Motor Idling', False, 'Motor running but not propelling', 2),
                    ('motor_propelling', 'Motor Propelling', False, 'Motor actively propelling boat', 3),
                    ('motor_trimming', 'Motor Trimming', False, 'Motor trimmed up/down', 4),
                ]
            },

            # 12. Boat Motion (Dropdown - Power Loading)
            {
                'group': ('boat_motion', 'Boat Motion', 'dropdown',
                         'Direction and type of boat movement', False, 'both', 32),
                'options': [
                    ('stationary', 'Stationary', False, 'Boat not moving', 1),
                    ('backing', 'Backing', False, 'Boat moving backward', 2),
                    ('forward_motion', 'Forward Motion', False, 'Boat moving forward', 3),
                    ('lateral_movement', 'Lateral Movement', False, 'Boat moving sideways', 4),
                ]
            },

            # 13. Training Priority (Dropdown)
            {
                'group': ('training_priority', 'Training Priority', 'dropdown',
                         'Importance of this example for training', False, 'both', 50),
                'options': [
                    ('critical_edge_case', 'Critical Edge Case', False, 'Rare but important scenario', 1),
                    ('common_false_positive', 'Common False Positive', False, 'Frequently misclassified', 2),
                    ('rare_but_important', 'Rare But Important', False, 'Uncommon but valuable', 3),
                    ('typical_example', 'Typical Example', False, 'Standard representative case', 4),
                    ('redundant_frame', 'Redundant Frame', False, 'Similar to many existing examples', 5),
                ]
            },

            # 14. Dataset Usage (Dropdown)
            {
                'group': ('dataset_usage', 'Dataset Usage', 'dropdown',
                         'How this example should be used in training', False, 'both', 51),
                'options': [
                    ('include_training', 'Include in Training', False, 'Use for model training', 1),
                    ('validation_only', 'Validation Only', False, 'Use only for validation', 2),
                    ('exclude_low_quality', 'Exclude - Low Quality', False, 'Do not use, quality issues', 3),
                    ('gold_standard_example', 'Gold Standard Example', False, 'Perfect example for testing', 4),
                ]
            },

            # 15. Boat Type (Dropdown - Keyframe Only)
            {
                'group': ('boat_type', 'Boat Type', 'dropdown',
                         'Type of watercraft', False, 'keyframe', 40),
                'options': [
                    ('pontoon', 'Pontoon', False, 'Pontoon boat', 1),
                    ('bowrider', 'Bowrider', False, 'Bowrider boat', 2),
                    ('fishing', 'Fishing Boat', False, 'Fishing vessel', 3),
                    ('jetski', 'Jet Ski', False, 'Personal watercraft', 4),
                    ('sailboat', 'Sailboat', False, 'Sail-powered boat', 5),
                    ('kayak_canoe', 'Kayak/Canoe', False, 'Small paddle craft', 6),
                ]
            },

            # 16. Boat Size (Dropdown - Keyframe)
            {
                'group': ('boat_size', 'Boat Size', 'dropdown',
                         'Relative size of watercraft', False, 'keyframe', 41),
                'options': [
                    ('small', 'Small', False, 'Under 16 feet', 1),
                    ('medium', 'Medium', False, '16-25 feet', 2),
                    ('large', 'Large', False, 'Over 25 feet', 3),
                ]
            },

            # 17. Propeller Visible (Dropdown - Keyframe)
            {
                'group': ('propeller_visible', 'Propeller Visible', 'dropdown',
                         'Can the propeller be seen?', False, 'keyframe', 42),
                'options': [
                    ('yes', 'Yes', False, 'Propeller clearly visible', 1),
                    ('no', 'No', False, 'Propeller not visible', 2),
                    ('uncertain', 'Uncertain', False, 'Cannot determine', 3),
                ]
            },

            # 18. Registration Visible (Dropdown - Keyframe)
            {
                'group': ('registration_visible', 'Registration Visible', 'dropdown',
                         'Can boat registration number be seen?', False, 'keyframe', 43),
                'options': [
                    ('yes_clearly', 'Yes - Clearly', False, 'Registration clearly readable', 1),
                    ('yes_partially', 'Yes - Partially', False, 'Registration partially visible', 2),
                    ('no', 'No', False, 'Registration not visible', 3),
                    ('uncertain', 'Uncertain', False, 'Cannot determine', 4),
                ]
            },

            # 19. Vehicle Type (Dropdown - Keyframe, License Plate)
            {
                'group': ('vehicle_type', 'Vehicle Type', 'dropdown',
                         'Type of vehicle', False, 'keyframe', 44),
                'options': [
                    ('truck', 'Truck', False, 'Pickup truck', 1),
                    ('suv', 'SUV', False, 'Sport utility vehicle', 2),
                    ('car', 'Car', False, 'Passenger car', 3),
                    ('trailer_only', 'Trailer Only', False, 'Boat trailer without vehicle', 4),
                    ('motorcycle', 'Motorcycle', False, 'Motorcycle or ATV', 5),
                ]
            },

            # 20. Plate State (Dropdown - Keyframe, License Plate)
            {
                'group': ('plate_state', 'Plate State', 'dropdown',
                         'Condition of license plate', False, 'keyframe', 45),
                'options': [
                    ('visible', 'Visible', False, 'Plate clearly visible', 1),
                    ('obstructed', 'Obstructed', False, 'Plate partially blocked', 2),
                    ('missing', 'Missing', False, 'No plate present', 3),
                    ('uncertain', 'Uncertain', False, 'Cannot determine', 4),
                ]
            },

            # 21. Commercial Vehicle (Dropdown - Keyframe)
            {
                'group': ('commercial_vehicle', 'Commercial Vehicle', 'dropdown',
                         'Is this a commercial vehicle?', False, 'keyframe', 46),
                'options': [
                    ('yes', 'Yes', False, 'Commercial vehicle', 1),
                    ('no', 'No', False, 'Personal vehicle', 2),
                    ('uncertain', 'Uncertain', False, 'Cannot determine', 3),
                ]
            },

            # 22. Face Angle (Dropdown - Keyframe, Face Detected)
            {
                'group': ('face_angle', 'Face Angle', 'dropdown',
                         'Angle of detected face', False, 'keyframe', 47),
                'options': [
                    ('front', 'Front', False, 'Frontal view', 1),
                    ('side', 'Side', False, 'Side profile', 2),
                    ('back', 'Back', False, 'Back of head', 3),
                    ('three_quarter', 'Three-Quarter', False, '3/4 view', 4),
                ]
            },

            # 23. Face Obstruction (Checkbox - Keyframe, Face Detected)
            {
                'group': ('face_obstruction', 'Face Obstruction', 'checkbox',
                         'What is obscuring the face?', False, 'keyframe', 48),
                'options': [
                    ('hat', 'Hat', False, 'Hat covering part of face', 1),
                    ('glasses', 'Glasses', False, 'Sunglasses or eyeglasses', 2),
                    ('mask', 'Mask', False, 'Face mask or covering', 3),
                    ('hand', 'Hand', False, 'Hand in front of face', 4),
                    ('hair', 'Hair', False, 'Hair covering face', 5),
                ]
            },

            # 24. Number of People (Dropdown - Keyframe)
            {
                'group': ('number_of_people', 'Number of People', 'dropdown',
                         'How many people are visible?', False, 'keyframe', 49),
                'options': [
                    ('one', 'One', False, 'Single person', 1),
                    ('two', 'Two', False, 'Two people', 2),
                    ('three_plus', 'Three or More', False, 'Three or more people', 3),
                ]
            },

            # 25. Extenuating Circumstances (Checkbox - Time Range)
            {
                'group': ('extenuating_circumstances', 'Extenuating Circumstances', 'checkbox',
                         'Factors that may excuse or explain behavior', False, 'time_range', 35),
                'options': [
                    ('elderly_disabled_operator', 'Elderly/Disabled Operator', False, 'Physical limitations evident', 1),
                    ('mechanical_issue_visible', 'Mechanical Issue Visible', False, 'Equipment malfunction', 2),
                    ('emergency_situation', 'Emergency Situation', False, 'Emergency circumstances', 3),
                    ('assisting_another_boater', 'Assisting Another Boater', False, 'Helping another person', 4),
                    ('instructional_situation', 'Instructional Situation', False, 'Teaching/learning scenario', 5),
                    ('first_time_user_evident', 'First-Time User Evident', False, 'Inexperience visible', 6),
                    ('ramp_conditions_difficult', 'Ramp Conditions Difficult', False, 'Challenging ramp conditions', 7),
                ]
            },

            # 26. Present Indicators (Checkbox - Power Loading Positive)
            {
                'group': ('present_indicators', 'Present Indicators', 'checkbox',
                         'Visual evidence that power loading IS occurring', False, 'both', 33),
                'options': [
                    ('propeller_spray_visible', 'Propeller Spray Visible', False, 'Water spray from propeller', 1),
                    ('forward_thrust_evident', 'Forward Thrust Evident', False, 'Boat pushing forward', 2),
                    ('boat_climbing_trailer', 'Boat Climbing Trailer', False, 'Boat rising onto trailer', 3),
                    ('motor_sound_audible', 'Motor Sound Audible', False, 'Engine noise in audio', 4),
                ]
            },

            # 27. Absent Indicators (Checkbox - Power Loading Negative)
            {
                'group': ('absent_indicators', 'Absent Indicators', 'checkbox',
                         'Visual evidence that power loading is NOT occurring', False, 'both', 34),
                'options': [
                    ('no_propeller_spray', 'No Propeller Spray', False, 'No water spray visible', 1),
                    ('no_forward_motion', 'No Forward Motion', False, 'Boat not moving forward', 2),
                    ('boat_stationary', 'Boat Stationary', False, 'Boat completely still', 3),
                    ('winch_only', 'Winch Only', False, 'Only winch being used', 4),
                ]
            },

            # 28. Reviewer Notes (Textarea)
            {
                'group': ('reviewer_notes', 'Reviewer Notes', 'textarea',
                         'Free-form notes and observations', False, 'both', 60),
                'options': []
            },

            # 29. Flags (Checkbox)
            {
                'group': ('flags', 'Flags for Review', 'checkbox',
                         'Mark for additional review or discussion', False, 'both', 61),
                'options': [
                    ('flagged_for_discussion', 'Flagged for Discussion', False, 'Needs team discussion', 1),
                    ('consensus_needed', 'Consensus Needed', False, 'Requires multiple reviewers', 2),
                    ('expert_review_required', 'Expert Review Required', False, 'Needs domain expert', 3),
                ]
            },
        ]

        # Insert groups and options
        for item in taxonomy:
            group_data = item['group']
            group_id = self.add_tag_group(*group_data)

            for option_data in item['options']:
                self.add_tag_option(group_id, *option_data)

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

    # Fleet Vehicle Management
    def add_or_update_fleet_vehicle(self, fleet_id: str, fleet_type: str = None,
                                     vehicle_type: str = None, vehicle_make: str = None,
                                     vehicle_model: str = None, primary_color: str = None,
                                     secondary_color: str = None, agency_name: str = None,
                                     plate_number: str = None, plate_state: str = None,
                                     notes: str = None) -> int:
        """Add or update a fleet vehicle"""
        with get_cursor() as cursor:
            # Try to update first
            cursor.execute('''
                UPDATE fleet_vehicles
                SET fleet_type = COALESCE(%s, fleet_type),
                    vehicle_type = COALESCE(%s, vehicle_type),
                    vehicle_make = COALESCE(%s, vehicle_make),
                    vehicle_model = COALESCE(%s, vehicle_model),
                    primary_color = COALESCE(%s, primary_color),
                    secondary_color = COALESCE(%s, secondary_color),
                    agency_name = COALESCE(%s, agency_name),
                    plate_number = COALESCE(%s, plate_number),
                    plate_state = COALESCE(%s, plate_state),
                    notes = COALESCE(%s, notes),
                    last_seen_date = CURRENT_TIMESTAMP,
                    total_detections = total_detections + 1
                WHERE fleet_id = %s
                RETURNING id
            ''', (fleet_type, vehicle_type, vehicle_make, vehicle_model,
                  primary_color, secondary_color, agency_name, plate_number,
                  plate_state, notes, fleet_id))

            result = cursor.fetchone()
            if result:
                return result['id']

            # Insert new record
            cursor.execute('''
                INSERT INTO fleet_vehicles
                (fleet_id, fleet_type, vehicle_type, vehicle_make, vehicle_model,
                 primary_color, secondary_color, agency_name, plate_number, plate_state, notes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            ''', (fleet_id, fleet_type, vehicle_type, vehicle_make, vehicle_model,
                  primary_color, secondary_color, agency_name, plate_number, plate_state, notes))
            result = cursor.fetchone()
            return result['id']

    def get_fleet_vehicle(self, fleet_id: str) -> Optional[Dict]:
        """Get a fleet vehicle by fleet ID"""
        with get_cursor(commit=False) as cursor:
            cursor.execute('SELECT * FROM fleet_vehicles WHERE fleet_id = %s', (fleet_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_all_fleet_vehicles(self, fleet_type: str = None, limit: int = 100) -> List[Dict]:
        """Get all fleet vehicles, optionally filtered by type"""
        with get_cursor(commit=False) as cursor:
            if fleet_type:
                cursor.execute('''
                    SELECT * FROM fleet_vehicles
                    WHERE fleet_type = %s
                    ORDER BY last_seen_date DESC
                    LIMIT %s
                ''', (fleet_type, limit))
            else:
                cursor.execute('''
                    SELECT * FROM fleet_vehicles
                    ORDER BY last_seen_date DESC
                    LIMIT %s
                ''', (limit,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def search_fleet_vehicles(self, query: str) -> List[Dict]:
        """Search fleet vehicles by various fields"""
        with get_cursor(commit=False) as cursor:
            search_term = f'%{query}%'
            cursor.execute('''
                SELECT * FROM fleet_vehicles
                WHERE fleet_id LIKE %s OR plate_number LIKE %s
                   OR vehicle_make LIKE %s OR vehicle_model LIKE %s
                   OR agency_name LIKE %s OR notes LIKE %s
                ORDER BY last_seen_date DESC
            ''', (search_term, search_term, search_term, search_term, search_term, search_term))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def link_person_to_vehicle(self, fleet_id: str, person_name: str) -> int:
        """Link a person to a fleet vehicle"""
        with get_cursor() as cursor:
            # Try to update existing link
            cursor.execute('''
                UPDATE vehicle_person_links
                SET last_seen_together = CURRENT_TIMESTAMP,
                    times_seen_together = times_seen_together + 1
                WHERE vehicle_fleet_id = %s AND person_name = %s
                RETURNING id
            ''', (fleet_id, person_name))

            result = cursor.fetchone()
            if result:
                return result['id']

            # Create new link
            cursor.execute('''
                INSERT INTO vehicle_person_links (vehicle_fleet_id, person_name)
                VALUES (%s, %s)
                RETURNING id
            ''', (fleet_id, person_name))
            result = cursor.fetchone()
            return result['id']

    def get_vehicle_persons(self, fleet_id: str) -> List[Dict]:
        """Get all persons linked to a vehicle"""
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT * FROM vehicle_person_links
                WHERE vehicle_fleet_id = %s
                ORDER BY times_seen_together DESC
            ''', (fleet_id,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_person_vehicles(self, person_name: str) -> List[Dict]:
        """Get all vehicles linked to a person"""
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT fv.*, vpl.first_seen_together, vpl.last_seen_together, vpl.times_seen_together
                FROM fleet_vehicles fv
                JOIN vehicle_person_links vpl ON fv.fleet_id = vpl.vehicle_fleet_id
                WHERE vpl.person_name = %s
                ORDER BY vpl.times_seen_together DESC
            ''', (person_name,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    # Trailer Management
    def add_or_update_trailer(self, trailer_id: str, trailer_type: str = None,
                              trailer_color: str = None, plate_number: str = None,
                              plate_state: str = None, notes: str = None) -> int:
        """Add or update a trailer"""
        with get_cursor() as cursor:
            # Try to update first
            cursor.execute('''
                UPDATE trailers
                SET trailer_type = COALESCE(%s, trailer_type),
                    trailer_color = COALESCE(%s, trailer_color),
                    plate_number = COALESCE(%s, plate_number),
                    plate_state = COALESCE(%s, plate_state),
                    notes = COALESCE(%s, notes),
                    last_seen_date = CURRENT_TIMESTAMP,
                    total_detections = total_detections + 1
                WHERE trailer_id = %s
                RETURNING id
            ''', (trailer_type, trailer_color, plate_number, plate_state, notes, trailer_id))

            result = cursor.fetchone()
            if result:
                return result['id']

            # Insert new record
            cursor.execute('''
                INSERT INTO trailers
                (trailer_id, trailer_type, trailer_color, plate_number, plate_state, notes)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
            ''', (trailer_id, trailer_type, trailer_color, plate_number, plate_state, notes))
            result = cursor.fetchone()
            return result['id']

    def get_trailer(self, trailer_id: str) -> Optional[Dict]:
        """Get a trailer by trailer ID"""
        with get_cursor(commit=False) as cursor:
            cursor.execute('SELECT * FROM trailers WHERE trailer_id = %s', (trailer_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_all_trailers(self, limit: int = 100) -> List[Dict]:
        """Get all trailers"""
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT * FROM trailers
                ORDER BY last_seen_date DESC
                LIMIT %s
            ''', (limit,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def link_trailer_to_vehicle(self, fleet_id: str, trailer_id: str) -> int:
        """Link a trailer to a fleet vehicle"""
        with get_cursor() as cursor:
            # Try to update existing link
            cursor.execute('''
                UPDATE vehicle_trailer_links
                SET last_seen_together = CURRENT_TIMESTAMP,
                    times_seen_together = times_seen_together + 1
                WHERE vehicle_fleet_id = %s AND trailer_id = %s
                RETURNING id
            ''', (fleet_id, trailer_id))

            result = cursor.fetchone()
            if result:
                return result['id']

            # Create new link
            cursor.execute('''
                INSERT INTO vehicle_trailer_links (vehicle_fleet_id, trailer_id)
                VALUES (%s, %s)
                RETURNING id
            ''', (fleet_id, trailer_id))
            result = cursor.fetchone()
            return result['id']

    def get_vehicle_trailers(self, fleet_id: str) -> List[Dict]:
        """Get all trailers linked to a vehicle"""
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT t.*, vtl.first_seen_together, vtl.last_seen_together, vtl.times_seen_together
                FROM trailers t
                JOIN vehicle_trailer_links vtl ON t.trailer_id = vtl.trailer_id
                WHERE vtl.vehicle_fleet_id = %s
                ORDER BY vtl.times_seen_together DESC
            ''', (fleet_id,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_trailer_vehicles(self, trailer_id: str) -> List[Dict]:
        """Get all vehicles linked to a trailer"""
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT fv.*, vtl.first_seen_together, vtl.last_seen_together, vtl.times_seen_together
                FROM fleet_vehicles fv
                JOIN vehicle_trailer_links vtl ON fv.fleet_id = vtl.vehicle_fleet_id
                WHERE vtl.trailer_id = %s
                ORDER BY vtl.times_seen_together DESC
            ''', (trailer_id,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def update_video(self, video_id: int, **kwargs) -> bool:
        """Update video fields"""
        allowed_fields = ['filename', 'original_url', 'title', 'duration', 'width', 'height',
                         'file_size', 'thumbnail_path', 'notes', 'camera_id']

        updates = []
        values = []
        for field, value in kwargs.items():
            if field in allowed_fields:
                updates.append(f'{field} = %s')
                values.append(value)

        if not updates:
            return False

        values.append(video_id)
        with get_cursor() as cursor:
            cursor.execute(f'''
                UPDATE videos SET {', '.join(updates)}
                WHERE id = %s
            ''', values)
            return cursor.rowcount > 0

    def get_video_by_filename(self, filename: str) -> Optional[Dict]:
        """Get video by filename"""
        with get_cursor(commit=False) as cursor:
            cursor.execute('SELECT * FROM videos WHERE filename = %s', (filename,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_video_tags(self, video_id: int) -> List[Dict]:
        """Get all tags for a specific video"""
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT t.*
                FROM tags t
                JOIN video_tags vt ON t.id = vt.tag_id
                WHERE vt.video_id = %s
                ORDER BY t.name
            ''', (video_id,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_videos_by_tag(self, tag_name: str) -> List[Dict]:
        """Get all videos with a specific tag"""
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT v.*
                FROM videos v
                JOIN video_tags vt ON v.id = vt.video_id
                JOIN tags t ON vt.tag_id = t.id
                WHERE t.name = %s
                ORDER BY v.upload_date DESC
            ''', (tag_name,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_total_video_count(self) -> int:
        """Get total number of videos"""
        with get_cursor(commit=False) as cursor:
            cursor.execute('SELECT COUNT(*) as count FROM videos')
            result = cursor.fetchone()
            return result['count'] if result else 0

    def get_total_annotation_count(self) -> int:
        """Get total number of keyframe annotations"""
        with get_cursor(commit=False) as cursor:
            cursor.execute('SELECT COUNT(*) as count FROM keyframe_annotations')
            result = cursor.fetchone()
            return result['count'] if result else 0

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

    def bulk_add_keyframe_annotations(self, annotations: List[Dict]) -> List[int]:
        """Bulk insert keyframe annotations for better performance"""
        if not annotations:
            return []

        with get_cursor() as cursor:
            ids = []
            for ann in annotations:
                cursor.execute('''
                    INSERT INTO keyframe_annotations
                    (video_id, timestamp, bbox_x, bbox_y, bbox_width, bbox_height,
                     activity_tag, moment_tag, is_negative, comment, reviewed)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                ''', (
                    ann['video_id'], ann['timestamp'],
                    ann['bbox_x'], ann['bbox_y'], ann['bbox_width'], ann['bbox_height'],
                    ann.get('activity_tag'), ann.get('moment_tag'),
                    ann.get('is_negative', False), ann.get('comment'),
                    ann.get('reviewed', False)
                ))
                result = cursor.fetchone()
                ids.append(result['id'])
            return ids

    def delete_all_keyframe_annotations_for_video(self, video_id: int) -> int:
        """Delete all keyframe annotations for a video, return count deleted"""
        with get_cursor() as cursor:
            cursor.execute('DELETE FROM keyframe_annotations WHERE video_id = %s', (video_id,))
            return cursor.rowcount

    def get_unreviewed_annotations(self, video_id: int = None, limit: int = 100) -> List[Dict]:
        """Get unreviewed annotations, optionally filtered by video"""
        with get_cursor(commit=False) as cursor:
            if video_id:
                cursor.execute('''
                    SELECT ka.*, v.filename
                    FROM keyframe_annotations ka
                    JOIN videos v ON ka.video_id = v.id
                    WHERE ka.reviewed = false AND ka.video_id = %s
                    ORDER BY ka.timestamp
                    LIMIT %s
                ''', (video_id, limit))
            else:
                cursor.execute('''
                    SELECT ka.*, v.filename
                    FROM keyframe_annotations ka
                    JOIN videos v ON ka.video_id = v.id
                    WHERE ka.reviewed = false
                    ORDER BY v.filename, ka.timestamp
                    LIMIT %s
                ''', (limit,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def mark_annotations_reviewed(self, annotation_ids: List[int]) -> int:
        """Mark multiple annotations as reviewed"""
        if not annotation_ids:
            return 0

        with get_cursor() as cursor:
            placeholders = ','.join(['%s'] * len(annotation_ids))
            cursor.execute(f'''
                UPDATE keyframe_annotations
                SET reviewed = true
                WHERE id IN ({placeholders})
            ''', annotation_ids)
            return cursor.rowcount

    def get_statistics(self) -> Dict:
        """Get database statistics"""
        with get_cursor(commit=False) as cursor:
            stats = {}

            cursor.execute('SELECT COUNT(*) as count FROM videos')
            stats['total_videos'] = cursor.fetchone()['count']

            cursor.execute('SELECT COUNT(*) as count FROM keyframe_annotations')
            stats['total_annotations'] = cursor.fetchone()['count']

            cursor.execute('SELECT COUNT(*) as count FROM keyframe_annotations WHERE reviewed = true')
            stats['reviewed_annotations'] = cursor.fetchone()['count']

            cursor.execute('SELECT COUNT(*) as count FROM tags')
            stats['total_tags'] = cursor.fetchone()['count']

            cursor.execute('SELECT COUNT(*) as count FROM fleet_vehicles')
            stats['total_fleet_vehicles'] = cursor.fetchone()['count']

            cursor.execute('SELECT COUNT(*) as count FROM trailers')
            stats['total_trailers'] = cursor.fetchone()['count']

            cursor.execute('SELECT COUNT(*) as count FROM yolo_export_configs')
            stats['total_export_configs'] = cursor.fetchone()['count']

            return stats

    # ==================== AI Predictions ====================

    def count_predictions_for_video(self, video_id: int, model_name: str, model_version: str) -> int:
        """Count existing predictions for a video from a specific model."""
        with get_cursor() as cursor:
            cursor.execute('''
                SELECT COUNT(*) FROM ai_predictions
                WHERE video_id = %s AND model_name = %s AND model_version = %s
            ''', (video_id, model_name, model_version))
            return cursor.fetchone()['count']

    def insert_predictions_batch(self, video_id: int, model_name: str, model_version: str,
                                  batch_id: str, predictions: List[Dict]) -> List[int]:
        """Insert a batch of AI predictions. Returns list of prediction IDs."""
        ids = []
        with get_cursor() as cursor:
            for pred in predictions:
                cursor.execute('''
                    INSERT INTO ai_predictions
                    (video_id, model_name, model_version, prediction_type, confidence,
                     timestamp, start_time, end_time, bbox_x, bbox_y, bbox_width, bbox_height,
                     scenario, predicted_tags, batch_id, inference_time_ms)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                ''', (
                    video_id, model_name, model_version,
                    pred['prediction_type'], pred['confidence'],
                    pred.get('timestamp'), pred.get('start_time'), pred.get('end_time'),
                    pred.get('bbox', {}).get('x'), pred.get('bbox', {}).get('y'),
                    pred.get('bbox', {}).get('width'), pred.get('bbox', {}).get('height'),
                    pred['scenario'],
                    extras.Json(pred.get('tags', {})),
                    batch_id,
                    pred.get('inference_time_ms')
                ))
                result = cursor.fetchone()
                ids.append(result['id'])
        return ids

    def get_pending_predictions(self, video_id: int = None, model_name: str = None,
                                 limit: int = 100, offset: int = 0) -> List[Dict]:
        """Get pending predictions for review, optionally filtered."""
        with get_cursor(commit=False) as cursor:
            conditions = ["review_status = 'pending'"]
            params = []
            if video_id:
                conditions.append("video_id = %s")
                params.append(video_id)
            if model_name:
                conditions.append("model_name = %s")
                params.append(model_name)
            where = " AND ".join(conditions)
            params.extend([limit, offset])
            cursor.execute(f'''
                SELECT p.*, v.filename as video_filename, v.title as video_title
                FROM ai_predictions p
                JOIN videos v ON p.video_id = v.id
                WHERE {where}
                ORDER BY p.confidence DESC, p.created_at DESC
                LIMIT %s OFFSET %s
            ''', params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_predictions_for_video(self, video_id: int, limit: int = 200, offset: int = 0) -> List[Dict]:
        """Get all predictions for a video regardless of review status."""
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT p.*, v.filename as video_filename, v.title as video_title
                FROM ai_predictions p
                JOIN videos v ON p.video_id = v.id
                WHERE p.video_id = %s
                ORDER BY p.confidence DESC, p.created_at DESC
                LIMIT %s OFFSET %s
            ''', (video_id, limit, offset))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_prediction_by_id(self, prediction_id: int) -> Optional[Dict]:
        """Get a single prediction by ID."""
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT p.*, v.filename as video_filename, v.title as video_title
                FROM ai_predictions p
                JOIN videos v ON p.video_id = v.id
                WHERE p.id = %s
            ''', (prediction_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_prediction_counts(self, video_id: int = None) -> Dict:
        """Get count of predictions by status, optionally for a specific video."""
        with get_cursor(commit=False) as cursor:
            if video_id:
                cursor.execute('''
                    SELECT review_status, COUNT(*) as count
                    FROM ai_predictions
                    WHERE video_id = %s
                    GROUP BY review_status
                ''', (video_id,))
            else:
                cursor.execute('''
                    SELECT review_status, COUNT(*) as count
                    FROM ai_predictions
                    GROUP BY review_status
                ''')
            rows = cursor.fetchall()
            counts = {row['review_status']: row['count'] for row in rows}
            counts['total'] = sum(counts.values())
            return counts

    def review_prediction(self, prediction_id: int, action: str, reviewer: str,
                           notes: str = None, corrections: Dict = None) -> Optional[Dict]:
        """Review a prediction: approve, reject, or correct."""
        with get_cursor() as cursor:
            if action == 'approve':
                status = 'approved'
            elif action == 'reject':
                status = 'rejected'
            elif action == 'correct':
                status = 'approved'  # corrections are approved with modified data
            else:
                return None

            update_fields = [
                "review_status = %s",
                "reviewed_by = %s",
                "reviewed_at = NOW()",
                "review_notes = %s"
            ]
            params = [status, reviewer, notes]

            if corrections:
                if corrections.get('tags'):
                    update_fields.append("corrected_tags = %s")
                    params.append(extras.Json(corrections['tags']))
                if corrections.get('bbox'):
                    update_fields.append("corrected_bbox = %s")
                    params.append(extras.Json(corrections['bbox']))
                if corrections.get('correction_type'):
                    update_fields.append("correction_type = %s")
                    params.append(corrections['correction_type'])

            params.append(prediction_id)
            cursor.execute(f'''
                UPDATE ai_predictions
                SET {", ".join(update_fields)}
                WHERE id = %s
                RETURNING *
            ''', params)
            row = cursor.fetchone()
            return dict(row) if row else None

    def approve_prediction_to_annotation(self, prediction_id: int) -> Optional[int]:
        """Convert an approved prediction into a training annotation. Returns annotation ID."""
        pred = self.get_prediction_by_id(prediction_id)
        if not pred or pred['review_status'] not in ('approved', 'auto_approved'):
            return None

        # Use corrected data if available, otherwise use predicted data
        tags = pred.get('corrected_tags') or pred['predicted_tags']
        bbox = pred.get('corrected_bbox')

        with get_cursor() as cursor:
            if pred['prediction_type'] == 'keyframe':
                bx = bbox['x'] if bbox else pred['bbox_x']
                by = bbox['y'] if bbox else pred['bbox_y']
                bw = bbox['width'] if bbox else pred['bbox_width']
                bh = bbox['height'] if bbox else pred['bbox_height']

                source = 'ai_auto_approved' if pred['review_status'] == 'auto_approved' else 'ai_prediction'
                # Human-reviewed predictions are already verified; auto-approved need review
                is_reviewed = pred['review_status'] == 'approved'
                cursor.execute('''
                    INSERT INTO keyframe_annotations
                    (video_id, timestamp, bbox_x, bbox_y, bbox_width, bbox_height,
                     activity_tag, comment, reviewed, source, source_prediction_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                ''', (
                    pred['video_id'], pred['timestamp'],
                    bx, by, bw, bh,
                    pred['scenario'],
                    f"AI prediction (model={pred['model_name']} v{pred['model_version']}, confidence={pred['confidence']:.2f})",
                    is_reviewed, source, prediction_id
                ))
                result = cursor.fetchone()
                annotation_id = result['id']

            elif pred['prediction_type'] == 'time_range':
                cursor.execute('''
                    INSERT INTO time_range_tags
                    (video_id, tag_name, start_time, end_time, comment)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING id
                ''', (
                    pred['video_id'], pred['scenario'],
                    pred['start_time'], pred['end_time'],
                    f"AI prediction (model={pred['model_name']} v{pred['model_version']}, confidence={pred['confidence']:.2f})"
                ))
                result = cursor.fetchone()
                annotation_id = result['id']
            else:
                return None

            # Link prediction to the created annotation
            cursor.execute('''
                UPDATE ai_predictions SET created_annotation_id = %s WHERE id = %s
            ''', (annotation_id, prediction_id))

            return annotation_id

    def update_prediction_routing(self, prediction_id: int, review_status: str,
                                    routed_by: str, threshold_used: Dict = None) -> bool:
        """Update a prediction's routing status (for auto-approve/auto-reject)."""
        with get_cursor() as cursor:
            cursor.execute('''
                UPDATE ai_predictions
                SET review_status = %s, routed_by = %s, routing_threshold_used = %s
                WHERE id = %s
            ''', (review_status, routed_by, extras.Json(threshold_used) if threshold_used else None, prediction_id))
            return cursor.rowcount > 0

    def get_review_queue(self, video_id=None, model_name=None, min_confidence=None,
                         max_confidence=None, limit=50, offset=0):
        """Get pending predictions for mobile review queue, with thumbnail paths."""
        with get_cursor(commit=False) as cursor:
            conditions = ["p.review_status = 'pending'"]
            params = []
            if video_id:
                conditions.append("p.video_id = %s")
                params.append(video_id)
            if model_name:
                conditions.append("p.model_name = %s")
                params.append(model_name)
            if min_confidence is not None:
                conditions.append("p.confidence >= %s")
                params.append(min_confidence)
            if max_confidence is not None:
                conditions.append("p.confidence <= %s")
                params.append(max_confidence)
            where = " AND ".join(conditions)
            params.extend([limit, offset])
            cursor.execute(f'''
                SELECT p.id, p.video_id, p.model_name, p.model_version, p.prediction_type,
                       p.confidence, p.timestamp, p.start_time, p.end_time,
                       p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
                       p.scenario, p.predicted_tags, p.inference_time_ms,
                       v.title as video_title, v.thumbnail_path, v.width as video_width, v.height as video_height
                FROM ai_predictions p
                JOIN videos v ON p.video_id = v.id
                WHERE {where}
                ORDER BY p.confidence ASC, p.created_at DESC
                LIMIT %s OFFSET %s
            ''', params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_review_queue_summary(self):
        """Get summary of pending predictions grouped by video for review queue entry screen."""
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT v.id as video_id, v.title as video_title, v.thumbnail_path,
                       COUNT(*) as pending_count,
                       COUNT(*) FILTER (WHERE p.review_status IN ('approved', 'rejected')) as reviewed_count,
                       COUNT(*) FILTER (WHERE p.review_status = 'pending') +
                       COUNT(*) FILTER (WHERE p.review_status IN ('approved', 'rejected')) as total_count,
                       ROUND(AVG(p.confidence)::numeric, 3) as avg_confidence,
                       MIN(p.confidence) as min_confidence
                FROM ai_predictions p
                JOIN videos v ON p.video_id = v.id
                WHERE p.review_status = 'pending'
                GROUP BY v.id, v.title, v.thumbnail_path
                ORDER BY pending_count DESC
            ''')
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def batch_review_predictions(self, reviews, reviewer='studio_user'):
        """Batch review multiple predictions. Returns summary."""
        results = {'approved': 0, 'rejected': 0, 'failed': 0, 'annotation_ids': []}
        with get_cursor() as cursor:
            for review in reviews:
                pred_id = review.get('prediction_id')
                action = review.get('action')
                notes = review.get('notes')
                if action not in ('approve', 'reject'):
                    results['failed'] += 1
                    continue
                status = 'approved' if action == 'approve' else 'rejected'
                cursor.execute('''
                    UPDATE ai_predictions
                    SET review_status = %s, reviewed_by = %s, reviewed_at = NOW(), review_notes = %s
                    WHERE id = %s AND review_status = 'pending'
                    RETURNING id, model_name, model_version
                ''', (status, reviewer, notes, pred_id))
                row = cursor.fetchone()
                if row:
                    results[status] += 1
                else:
                    results['failed'] += 1
        # Create annotations for approved predictions (outside the batch cursor)
        # We do this separately to avoid nested cursor issues
        for review in reviews:
            if review.get('action') == 'approve':
                ann_id = self.approve_prediction_to_annotation(review['prediction_id'])
                if ann_id:
                    results['annotation_ids'].append(ann_id)
        return results

    def unreview_prediction(self, prediction_id):
        """Revert a prediction back to pending (for undo). Also removes created annotation."""
        with get_cursor() as cursor:
            # Get current state
            cursor.execute('SELECT created_annotation_id, review_status FROM ai_predictions WHERE id = %s', (prediction_id,))
            row = cursor.fetchone()
            if not row or row['review_status'] == 'pending':
                return False
            # Remove created annotation if any
            if row['created_annotation_id']:
                cursor.execute('DELETE FROM keyframe_annotations WHERE id = %s', (row['created_annotation_id'],))
            # Reset prediction to pending
            cursor.execute('''
                UPDATE ai_predictions
                SET review_status = 'pending', reviewed_by = NULL, reviewed_at = NULL,
                    review_notes = NULL, created_annotation_id = NULL
                WHERE id = %s
            ''', (prediction_id,))
            return cursor.rowcount > 0

    def get_review_history(self, status_filter=None, reviewer=None, limit=50, offset=0):
        """Get reviewed predictions for history view, most recent first"""
        with self.get_cursor() as cur:
            conditions = ["review_status IN ('approved', 'rejected')"]
            params = []

            if status_filter:
                conditions.append("review_status = %s")
                params.append(status_filter)
            if reviewer:
                conditions.append("reviewed_by = %s")
                params.append(reviewer)

            where = " AND ".join(conditions)
            params.extend([limit, offset])

            cur.execute(f"""
                SELECT p.id, p.video_id, v.title as video_title, p.prediction_type, p.scenario,
                       p.predicted_tags, p.confidence, p.review_status, p.reviewed_by, p.reviewed_at,
                       p.review_notes, p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
                       p.thumbnail_path
                FROM ai_predictions p
                LEFT JOIN videos v ON p.video_id = v.id
                WHERE {where}
                ORDER BY p.reviewed_at DESC NULLS LAST
                LIMIT %s OFFSET %s
            """, params)
            return [dict(row) for row in cur.fetchall()]

    # ==================== Model Registry ====================

    def get_or_create_model_registry(self, model_name: str, model_version: str,
                                      model_type: str = 'yolo') -> Dict:
        """Get or create a model registry entry."""
        with get_cursor() as cursor:
            cursor.execute('''
                SELECT * FROM model_registry WHERE model_name = %s AND model_version = %s
            ''', (model_name, model_version))
            row = cursor.fetchone()
            if row:
                return dict(row)

            cursor.execute('''
                INSERT INTO model_registry (model_name, model_version, model_type)
                VALUES (%s, %s, %s)
                RETURNING *
            ''', (model_name, model_version, model_type))
            row = cursor.fetchone()
            return dict(row)

    def get_model_registry(self, model_name: str = None, active_only: bool = True) -> List[Dict]:
        """Get model registry entries, optionally filtered."""
        with get_cursor(commit=False) as cursor:
            conditions = []
            params = []
            if model_name:
                conditions.append("model_name = %s")
                params.append(model_name)
            if active_only:
                conditions.append("is_active = true")
            where = "WHERE " + " AND ".join(conditions) if conditions else ""
            cursor.execute(f'''
                SELECT * FROM model_registry {where}
                ORDER BY model_name, model_version DESC
            ''', params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def update_model_thresholds(self, model_name: str, model_version: str,
                                 thresholds: Dict) -> Optional[Dict]:
        """Update confidence thresholds for a model."""
        with get_cursor() as cursor:
            cursor.execute('''
                UPDATE model_registry
                SET confidence_thresholds = %s, updated_at = NOW()
                WHERE model_name = %s AND model_version = %s
                RETURNING *
            ''', (extras.Json(thresholds), model_name, model_version))
            row = cursor.fetchone()
            return dict(row) if row else None

    def update_model_approval_stats(self, model_name: str, model_version: str) -> Optional[Dict]:
        """Recalculate and update approval stats for a model from actual prediction data."""
        with get_cursor() as cursor:
            cursor.execute('''
                SELECT
                    COUNT(*) as total,
                    COUNT(*) FILTER (WHERE review_status IN ('approved', 'auto_approved')) as approved,
                    COUNT(*) FILTER (WHERE review_status IN ('rejected', 'auto_rejected')) as rejected
                FROM ai_predictions
                WHERE model_name = %s AND model_version = %s
            ''', (model_name, model_version))
            stats = cursor.fetchone()

            total = stats['total']
            approved = stats['approved']
            rejected = stats['rejected']
            approval_rate = approved / total if total > 0 else None

            cursor.execute('''
                UPDATE model_registry
                SET total_predictions = %s, total_approved = %s, total_rejected = %s,
                    approval_rate = %s, updated_at = NOW()
                WHERE model_name = %s AND model_version = %s
                RETURNING *
            ''', (total, approved, rejected, approval_rate, model_name, model_version))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_model_stats(self, model_name: str, model_version: str = None) -> Dict:
        """Get comprehensive model performance stats."""
        with get_cursor(commit=False) as cursor:
            conditions = ["model_name = %s"]
            params = [model_name]
            if model_version:
                conditions.append("model_version = %s")
                params.append(model_version)
            where = " AND ".join(conditions)

            # Overall stats
            cursor.execute(f'''
                SELECT
                    COUNT(*) as total_predictions,
                    COUNT(*) FILTER (WHERE review_status IN ('approved', 'auto_approved')) as approved,
                    COUNT(*) FILTER (WHERE review_status IN ('rejected', 'auto_rejected')) as rejected,
                    COUNT(*) FILTER (WHERE review_status = 'pending') as pending,
                    COUNT(*) FILTER (WHERE review_status = 'needs_correction') as needs_correction,
                    AVG(confidence) FILTER (WHERE review_status IN ('approved', 'auto_approved')) as avg_confidence_approved,
                    AVG(confidence) FILTER (WHERE review_status IN ('rejected', 'auto_rejected')) as avg_confidence_rejected
                FROM ai_predictions
                WHERE {where}
            ''', params)
            overall = dict(cursor.fetchone())

            total = overall['total_predictions']
            overall['approval_rate'] = overall['approved'] / total if total > 0 else None

            # Per-scenario stats
            cursor.execute(f'''
                SELECT scenario,
                    COUNT(*) as total,
                    COUNT(*) FILTER (WHERE review_status IN ('approved', 'auto_approved')) as approved,
                    COUNT(*) FILTER (WHERE review_status IN ('rejected', 'auto_rejected')) as rejected
                FROM ai_predictions
                WHERE {where}
                GROUP BY scenario
                ORDER BY COUNT(*) DESC
            ''', params)
            scenarios = {}
            for row in cursor.fetchall():
                row = dict(row)
                stotal = row['total']
                scenarios[row['scenario']] = {
                    'total': stotal,
                    'approved': row['approved'],
                    'rejected': row['rejected'],
                    'approval_rate': row['approved'] / stotal if stotal > 0 else None
                }

            overall['scenarios'] = scenarios
            return overall

    # ==================== Training Metrics ====================

    def insert_training_metrics(self, training_job_id: int, model_name: str,
                                 model_version: str, metrics: Dict) -> int:
        """Insert training metrics for a completed job."""
        with get_cursor() as cursor:
            cursor.execute('''
                INSERT INTO training_metrics
                (training_job_id, model_name, model_version, accuracy, loss,
                 val_accuracy, val_loss, class_metrics, confusion_matrix,
                 epochs, training_duration_seconds, dataset_size, dataset_hash)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            ''', (
                training_job_id, model_name, model_version,
                metrics.get('accuracy'), metrics.get('loss'),
                metrics.get('val_accuracy'), metrics.get('val_loss'),
                extras.Json(metrics.get('class_metrics')) if metrics.get('class_metrics') else None,
                extras.Json(metrics.get('confusion_matrix')) if metrics.get('confusion_matrix') else None,
                metrics.get('epochs'), metrics.get('training_duration_seconds'),
                metrics.get('dataset_size'), metrics.get('dataset_hash')
            ))
            result = cursor.fetchone()

            # Also update the model registry with latest metrics
            cursor.execute('''
                UPDATE model_registry
                SET latest_metrics = %s, updated_at = NOW()
                WHERE model_name = %s AND model_version = %s
            ''', (extras.Json(metrics), model_name, model_version))

            return result['id']

    def get_training_metrics_history(self, model_name: str, model_version: str = None,
                                      limit: int = 20) -> List[Dict]:
        """Get training metrics history for a model."""
        with get_cursor(commit=False) as cursor:
            if model_version:
                cursor.execute('''
                    SELECT tm.*, tj.job_type, tj.status as job_status
                    FROM training_metrics tm
                    LEFT JOIN training_jobs tj ON tm.training_job_id = tj.id
                    WHERE tm.model_name = %s AND tm.model_version = %s
                    ORDER BY tm.created_at DESC
                    LIMIT %s
                ''', (model_name, model_version, limit))
            else:
                cursor.execute('''
                    SELECT tm.*, tj.job_type, tj.status as job_status
                    FROM training_metrics tm
                    LEFT JOIN training_jobs tj ON tm.training_job_id = tj.id
                    WHERE tm.model_name = %s
                    ORDER BY tm.created_at DESC
                    LIMIT %s
                ''', (model_name, limit))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    # ── Content Libraries ──────────────────────────────────────────────

    def get_all_libraries(self) -> List[Dict]:
        """Get all content libraries with item counts.
        The default (Uncategorized) library count = videos not in any other library."""
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT cl.*,
                       COUNT(cli.video_id) as item_count
                FROM content_libraries cl
                LEFT JOIN content_library_items cli ON cl.id = cli.library_id
                WHERE cl.is_default = FALSE
                GROUP BY cl.id
                ORDER BY cl.name ASC
            ''')
            non_default = [dict(row) for row in cursor.fetchall()]

            # Count videos not in any non-default library for Uncategorized
            cursor.execute('''
                SELECT cl.*, (
                    SELECT COUNT(*) FROM videos v
                    WHERE NOT EXISTS (
                        SELECT 1 FROM content_library_items cli2
                        INNER JOIN content_libraries cl2 ON cli2.library_id = cl2.id
                        WHERE cli2.video_id = v.id AND cl2.is_default = FALSE
                    )
                ) as item_count
                FROM content_libraries cl
                WHERE cl.is_default = TRUE
            ''')
            default_row = cursor.fetchone()
            result = []
            if default_row:
                result.append(dict(default_row))
            result.extend(non_default)
            return result

    def create_library(self, name: str) -> int:
        """Create a new content library. Returns its id."""
        with get_cursor() as cursor:
            cursor.execute('''
                INSERT INTO content_libraries (name)
                VALUES (%s)
                RETURNING id
            ''', (name,))
            return cursor.fetchone()['id']

    def rename_library(self, library_id: int, name: str) -> bool:
        """Rename a library. Cannot rename the default."""
        with get_cursor() as cursor:
            cursor.execute('''
                UPDATE content_libraries
                SET name = %s
                WHERE id = %s AND is_default = FALSE
            ''', (name, library_id))
            return cursor.rowcount > 0

    def delete_library(self, library_id: int) -> bool:
        """Delete a library. Cannot delete the default. Items become unassigned."""
        with get_cursor() as cursor:
            cursor.execute('''
                DELETE FROM content_libraries
                WHERE id = %s AND is_default = FALSE
            ''', (library_id,))
            return cursor.rowcount > 0

    def add_to_library(self, library_id: int, video_ids: List[int]) -> int:
        """Add videos to a library. Returns count of newly added."""
        added = 0
        with get_cursor() as cursor:
            for vid in video_ids:
                try:
                    cursor.execute('''
                        INSERT INTO content_library_items (library_id, video_id)
                        VALUES (%s, %s)
                        ON CONFLICT DO NOTHING
                    ''', (library_id, vid))
                    added += cursor.rowcount
                except Exception:
                    pass
        return added

    def remove_from_library(self, library_id: int, video_id: int) -> bool:
        """Remove a video from a library."""
        with get_cursor() as cursor:
            cursor.execute('''
                DELETE FROM content_library_items
                WHERE library_id = %s AND video_id = %s
            ''', (library_id, video_id))
            return cursor.rowcount > 0

    def get_video_libraries(self, video_id: int) -> List[Dict]:
        """Get all libraries a video belongs to."""
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT cl.id, cl.name, cl.is_default
                FROM content_libraries cl
                INNER JOIN content_library_items cli ON cl.id = cli.library_id
                WHERE cli.video_id = %s
                ORDER BY cl.name
            ''', (video_id,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_next_unannotated_in_library(self, library_id: int, current_video_id: int = None) -> Optional[Dict]:
        """Get the next video in a library that has zero keyframe annotations.
        For the default library, finds videos not in any non-default library.
        Skips the current video if provided. Returns None if all are annotated."""
        with get_cursor(commit=False) as cursor:
            if self._is_default_library(library_id):
                # Default library: videos not in any non-default library
                cursor.execute('''
                    SELECT v.id, v.filename, v.title
                    FROM videos v
                    LEFT JOIN keyframe_annotations ka ON v.id = ka.video_id
                    WHERE v.id != COALESCE(%s, -1)
                      AND v.filename NOT LIKE '%%.placeholder'
                      AND NOT EXISTS (
                          SELECT 1 FROM content_library_items cli2
                          INNER JOIN content_libraries cl2 ON cli2.library_id = cl2.id
                          WHERE cli2.video_id = v.id AND cl2.is_default = FALSE
                      )
                    GROUP BY v.id
                    HAVING COUNT(ka.id) = 0
                    ORDER BY v.upload_date ASC
                    LIMIT 1
                ''', (current_video_id,))
            else:
                cursor.execute('''
                    SELECT v.id, v.filename, v.title
                    FROM videos v
                    INNER JOIN content_library_items cli ON v.id = cli.video_id AND cli.library_id = %s
                    LEFT JOIN keyframe_annotations ka ON v.id = ka.video_id
                    WHERE v.id != COALESCE(%s, -1)
                    GROUP BY v.id
                    HAVING COUNT(ka.id) = 0
                    ORDER BY v.upload_date ASC
                    LIMIT 1
                ''', (library_id, current_video_id))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_next_unannotated(self, current_video_id: int = None) -> Optional[Dict]:
        """Get the next video (globally) that has zero keyframe annotations.
        Skips the current video if provided. Returns None if all are annotated."""
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT v.id, v.filename, v.title
                FROM videos v
                LEFT JOIN keyframe_annotations ka ON v.id = ka.video_id
                WHERE v.id != COALESCE(%s, -1)
                  AND v.filename NOT LIKE '%%.placeholder'
                GROUP BY v.id
                HAVING COUNT(ka.id) = 0
                ORDER BY v.upload_date ASC
                LIMIT 1
            ''', (current_video_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    # ==================== Identities ====================

    def create_identity(self, identity_type: str, name: str = None,
                        metadata: dict = None, is_flagged: bool = False,
                        notes: str = None) -> Dict:
        """Create a new identity record."""
        with get_cursor() as cursor:
            cursor.execute('''
                INSERT INTO identities (identity_type, name, metadata, is_flagged, notes)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
            ''', (identity_type, name,
                  extras.Json(metadata) if metadata else extras.Json({}),
                  is_flagged, notes))
            row = cursor.fetchone()
            return dict(row)

    def get_identity(self, identity_id: str) -> Optional[Dict]:
        """Get an identity by ID."""
        with get_cursor(commit=False) as cursor:
            cursor.execute('SELECT * FROM identities WHERE identity_id = %s',
                           (identity_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_identities(self, identity_type: str = None, is_flagged: bool = None,
                       search: str = None, limit: int = 100, offset: int = 0) -> List[Dict]:
        """Get identities with optional filters. Search matches name or notes."""
        conditions = []
        params = []
        if identity_type is not None:
            conditions.append('identity_type = %s')
            params.append(identity_type)
        if is_flagged is not None:
            conditions.append('is_flagged = %s')
            params.append(is_flagged)
        if search is not None:
            conditions.append('(name ILIKE %s OR notes ILIKE %s)')
            params.extend([f'%{search}%', f'%{search}%'])

        where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
        params.extend([limit, offset])

        with get_cursor(commit=False) as cursor:
            cursor.execute(f'''
                SELECT * FROM identities
                {where}
                ORDER BY last_seen DESC
                LIMIT %s OFFSET %s
            ''', params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def update_identity(self, identity_id: str, **kwargs) -> Optional[Dict]:
        """Update an identity. Allowed fields: name, metadata, is_flagged, notes, last_seen."""
        allowed = {'name', 'metadata', 'is_flagged', 'notes', 'last_seen'}
        updates = []
        values = []
        for key, value in kwargs.items():
            if key not in allowed:
                continue
            if key == 'metadata':
                updates.append('metadata = %s')
                values.append(extras.Json(value))
            else:
                updates.append(f'{key} = %s')
                values.append(value)

        if not updates:
            return None

        values.append(identity_id)
        with get_cursor() as cursor:
            cursor.execute(f'''
                UPDATE identities SET {', '.join(updates)}
                WHERE identity_id = %s
                RETURNING *
            ''', values)
            row = cursor.fetchone()
            return dict(row) if row else None

    def delete_identity(self, identity_id: str) -> bool:
        """Delete an identity by ID."""
        with get_cursor() as cursor:
            cursor.execute('DELETE FROM identities WHERE identity_id = %s',
                           (identity_id,))
            return cursor.rowcount > 0

    # ==================== Embeddings ====================

    def insert_embedding(self, identity_id: str, embedding_type: str,
                         vector: list, confidence: float,
                         source_image_path: str = None, camera_id: str = None,
                         is_reference: bool = False, session_date=None) -> Dict:
        """Insert a new embedding vector."""
        with get_cursor() as cursor:
            cursor.execute('''
                INSERT INTO embeddings
                (identity_id, embedding_type, vector, confidence,
                 source_image_path, camera_id, is_reference, session_date)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
            ''', (identity_id, embedding_type, vector, confidence,
                  source_image_path, camera_id, is_reference, session_date))
            row = cursor.fetchone()
            return dict(row)

    def get_embeddings(self, identity_id: str = None, embedding_type: str = None,
                       is_reference: bool = None, limit: int = 100) -> List[Dict]:
        """Get embeddings with optional filters."""
        conditions = []
        params = []
        if identity_id is not None:
            conditions.append('identity_id = %s')
            params.append(identity_id)
        if embedding_type is not None:
            conditions.append('embedding_type = %s')
            params.append(embedding_type)
        if is_reference is not None:
            conditions.append('is_reference = %s')
            params.append(is_reference)

        where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
        params.append(limit)

        with get_cursor(commit=False) as cursor:
            cursor.execute(f'''
                SELECT * FROM embeddings
                {where}
                ORDER BY created_at DESC
                LIMIT %s
            ''', params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def find_similar_embeddings(self, vector: list, embedding_type: str,
                                threshold: float = 0.6, limit: int = 10,
                                session_date=None) -> List[Dict]:
        """Find similar embeddings by cosine similarity. Fetches candidates then
        computes similarity in Python."""
        conditions = ['embedding_type = %s']
        params = [embedding_type]
        if session_date is not None:
            conditions.append('session_date = %s')
            params.append(session_date)

        where = 'WHERE ' + ' AND '.join(conditions)

        with get_cursor(commit=False) as cursor:
            cursor.execute(f'''
                SELECT * FROM embeddings
                {where}
                ORDER BY created_at DESC
            ''', params)
            rows = cursor.fetchall()

        results = []
        for row in rows:
            row_dict = dict(row)
            candidate_vector = row_dict['vector']
            similarity = _cosine_similarity(vector, candidate_vector)
            if similarity >= threshold:
                row_dict['similarity'] = round(similarity, 6)
                results.append(row_dict)

        results.sort(key=lambda x: x['similarity'], reverse=True)
        return results[:limit]

    def delete_embedding(self, embedding_id: str) -> bool:
        """Delete an embedding by ID."""
        with get_cursor() as cursor:
            cursor.execute('DELETE FROM embeddings WHERE embedding_id = %s',
                           (embedding_id,))
            return cursor.rowcount > 0

    # ==================== Associations ====================

    def upsert_association(self, identity_a: str, identity_b: str,
                           association_type: str,
                           confidence_delta: float = 0.1) -> Dict:
        """Create or update an association between two identities.
        On conflict, increments observation_count and adds confidence_delta (capped at 1.0)."""
        with get_cursor() as cursor:
            cursor.execute('''
                INSERT INTO associations (identity_a, identity_b, association_type,
                                          confidence, observation_count)
                VALUES (%s, %s, %s, %s, 1)
                ON CONFLICT (identity_a, identity_b, association_type) DO UPDATE SET
                    observation_count = associations.observation_count + 1,
                    confidence = LEAST(associations.confidence + %s, 1.0),
                    last_observed = NOW()
                RETURNING *
            ''', (identity_a, identity_b, association_type,
                  confidence_delta, confidence_delta))
            row = cursor.fetchone()
            return dict(row)

    def get_associations(self, identity_id: str,
                         association_type: str = None) -> List[Dict]:
        """Get all associations involving an identity (as either side)."""
        conditions = ['(identity_a = %s OR identity_b = %s)']
        params = [identity_id, identity_id]
        if association_type is not None:
            conditions.append('association_type = %s')
            params.append(association_type)

        where = 'WHERE ' + ' AND '.join(conditions)

        with get_cursor(commit=False) as cursor:
            cursor.execute(f'''
                SELECT a.*,
                       ia.name AS identity_a_name, ia.identity_type AS identity_a_type,
                       ib.name AS identity_b_name, ib.identity_type AS identity_b_type
                FROM associations a
                LEFT JOIN identities ia ON a.identity_a = ia.identity_id
                LEFT JOIN identities ib ON a.identity_b = ib.identity_id
                {where}
                ORDER BY a.confidence DESC
            ''', params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_association_chain(self, identity_id: str) -> Dict:
        """Get the full association chain for an identity.
        Returns dict with keys: persons, vehicles, trailers, boats containing
        associated identities found by walking the association graph."""
        result = {
            'persons': [],
            'vehicles': [],
            'trailers': [],
            'boats': []
        }
        visited = set()
        queue = [identity_id]

        while queue:
            current_id = queue.pop(0)
            if current_id in visited:
                continue
            visited.add(current_id)

            associations = self.get_associations(current_id)
            for assoc in associations:
                # Determine the other identity in the association
                other_id = assoc['identity_b'] if str(assoc['identity_a']) == str(current_id) else assoc['identity_a']
                other_id_str = str(other_id)
                if other_id_str not in visited:
                    queue.append(other_id_str)

        # Now fetch all visited identities (except the original)
        visited.discard(identity_id)
        if not visited:
            return result

        with get_cursor(commit=False) as cursor:
            placeholders = ','.join(['%s'] * len(visited))
            cursor.execute(f'''
                SELECT * FROM identities
                WHERE identity_id IN ({placeholders})
            ''', list(visited))
            rows = cursor.fetchall()

        type_map = {
            'person': 'persons',
            'vehicle': 'vehicles',
            'trailer': 'trailers',
            'boat': 'boats'
        }
        for row in rows:
            row_dict = dict(row)
            key = type_map.get(row_dict['identity_type'])
            if key:
                result[key].append(row_dict)

        return result

    # ==================== Tracks ====================

    def create_track(self, camera_id: str, entity_type: str,
                     identity_id: str = None, identity_method: str = None,
                     identity_confidence: float = None) -> Dict:
        """Create a new track for entity observation within a camera."""
        with get_cursor() as cursor:
            cursor.execute('''
                INSERT INTO tracks (camera_id, entity_type, identity_id,
                                    identity_method, identity_confidence)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
            ''', (camera_id, entity_type, identity_id,
                  identity_method, identity_confidence))
            row = cursor.fetchone()
            return dict(row)

    def end_track(self, track_id: str) -> bool:
        """End a track by setting end_time to NOW()."""
        with get_cursor() as cursor:
            cursor.execute('''
                UPDATE tracks SET end_time = NOW()
                WHERE track_id = %s AND end_time IS NULL
            ''', (track_id,))
            return cursor.rowcount > 0

    def get_track(self, track_id: str) -> Optional[Dict]:
        """Get a track by ID."""
        with get_cursor(commit=False) as cursor:
            cursor.execute('SELECT * FROM tracks WHERE track_id = %s',
                           (track_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_tracks(self, camera_id: str = None, entity_type: str = None,
                   identity_id: str = None, start_after=None, start_before=None,
                   active_only: bool = False, limit: int = 100,
                   offset: int = 0) -> List[Dict]:
        """Get tracks with optional filters."""
        conditions = []
        params = []
        if camera_id is not None:
            conditions.append('camera_id = %s')
            params.append(camera_id)
        if entity_type is not None:
            conditions.append('entity_type = %s')
            params.append(entity_type)
        if identity_id is not None:
            conditions.append('identity_id = %s')
            params.append(identity_id)
        if start_after is not None:
            conditions.append('start_time >= %s')
            params.append(start_after)
        if start_before is not None:
            conditions.append('start_time <= %s')
            params.append(start_before)
        if active_only:
            conditions.append('end_time IS NULL')

        where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
        params.extend([limit, offset])

        with get_cursor(commit=False) as cursor:
            cursor.execute(f'''
                SELECT * FROM tracks
                {where}
                ORDER BY start_time DESC
                LIMIT %s OFFSET %s
            ''', params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def link_track_to_identity(self, track_id: str, identity_id: str,
                               method: str, confidence: float) -> bool:
        """Link a track to an identity with identification method and confidence."""
        with get_cursor() as cursor:
            cursor.execute('''
                UPDATE tracks
                SET identity_id = %s, identity_method = %s, identity_confidence = %s
                WHERE track_id = %s
            ''', (identity_id, method, confidence, track_id))
            return cursor.rowcount > 0

    def get_active_tracks(self, camera_id: str = None) -> List[Dict]:
        """Get all active tracks (end_time IS NULL), optionally filtered by camera."""
        conditions = ['end_time IS NULL']
        params = []
        if camera_id is not None:
            conditions.append('camera_id = %s')
            params.append(camera_id)

        where = 'WHERE ' + ' AND '.join(conditions)

        with get_cursor(commit=False) as cursor:
            cursor.execute(f'''
                SELECT * FROM tracks
                {where}
                ORDER BY start_time DESC
            ''', params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    # ==================== Sightings ====================

    def batch_insert_sightings(self, sightings: List[Dict]) -> int:
        """Batch insert sightings using execute_values for performance.
        Each sighting dict should have: track_id, timestamp, bbox, confidence, face_visible."""
        if not sightings:
            return 0

        values = [
            (s['track_id'], s['timestamp'], s['bbox'],
             s['confidence'], s.get('face_visible', False))
            for s in sightings
        ]

        with get_cursor() as cursor:
            extras.execute_values(
                cursor,
                '''INSERT INTO sightings (track_id, timestamp, bbox, confidence, face_visible)
                   VALUES %s''',
                values,
                template='(%s, %s, %s, %s, %s)'
            )
            return cursor.rowcount

    def get_track_sightings(self, track_id: str,
                            limit: int = None) -> List[Dict]:
        """Get all sightings for a track, ordered by timestamp ASC."""
        params = [track_id]
        limit_clause = ''
        if limit is not None:
            limit_clause = 'LIMIT %s'
            params.append(limit)

        with get_cursor(commit=False) as cursor:
            cursor.execute(f'''
                SELECT * FROM sightings
                WHERE track_id = %s
                ORDER BY timestamp ASC
                {limit_clause}
            ''', params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    # ==================== Camera Topology Learned ====================

    def upsert_camera_transit(self, camera_a: str, camera_b: str,
                              transit_seconds: int) -> Dict:
        """Insert or update camera transit time observation.
        On insert: sets min=max=avg=transit_seconds, count=1.
        On conflict: updates min/max, recalculates running avg, increments count."""
        with get_cursor() as cursor:
            cursor.execute('''
                INSERT INTO camera_topology_learned
                (camera_a, camera_b, min_transit_seconds, max_transit_seconds,
                 avg_transit_seconds, observation_count)
                VALUES (%s, %s, %s, %s, %s, 1)
                ON CONFLICT (camera_a, camera_b) DO UPDATE SET
                    min_transit_seconds = LEAST(camera_topology_learned.min_transit_seconds, EXCLUDED.min_transit_seconds),
                    max_transit_seconds = GREATEST(camera_topology_learned.max_transit_seconds, EXCLUDED.max_transit_seconds),
                    avg_transit_seconds = (
                        camera_topology_learned.avg_transit_seconds * camera_topology_learned.observation_count
                        + EXCLUDED.avg_transit_seconds
                    ) / (camera_topology_learned.observation_count + 1),
                    observation_count = camera_topology_learned.observation_count + 1
                RETURNING *
            ''', (camera_a, camera_b, transit_seconds, transit_seconds,
                  float(transit_seconds)))
            row = cursor.fetchone()
            return dict(row)

    def get_adjacent_cameras(self, camera_id: str) -> List[Dict]:
        """Get all cameras adjacent to the given camera (in either direction)."""
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT * FROM camera_topology_learned
                WHERE camera_a = %s OR camera_b = %s
                ORDER BY avg_transit_seconds ASC
            ''', (camera_id, camera_id))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    # ==================== Violations ====================

    def create_violation(self, violation_type: str, camera_id: str,
                         confidence: float, person_identity_id: str = None,
                         vehicle_identity_id: str = None,
                         boat_identity_id: str = None,
                         trailer_identity_id: str = None,
                         evidence_paths: list = None) -> Dict:
        """Create a new violation record."""
        with get_cursor() as cursor:
            cursor.execute('''
                INSERT INTO violations
                (violation_type, camera_id, confidence, person_identity_id,
                 vehicle_identity_id, boat_identity_id, trailer_identity_id,
                 evidence_paths)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
            ''', (violation_type, camera_id, confidence,
                  person_identity_id, vehicle_identity_id,
                  boat_identity_id, trailer_identity_id,
                  evidence_paths or []))
            row = cursor.fetchone()
            return dict(row)

    def get_violations(self, status: str = None, camera_id: str = None,
                       violation_type: str = None, limit: int = 100,
                       offset: int = 0) -> List[Dict]:
        """Get violations with optional filters. Includes identity names via LEFT JOINs."""
        conditions = []
        params = []
        if status is not None:
            conditions.append('v.status = %s')
            params.append(status)
        if camera_id is not None:
            conditions.append('v.camera_id = %s')
            params.append(camera_id)
        if violation_type is not None:
            conditions.append('v.violation_type = %s')
            params.append(violation_type)

        where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
        params.extend([limit, offset])

        with get_cursor(commit=False) as cursor:
            cursor.execute(f'''
                SELECT v.*,
                       ip.name AS person_name,
                       iv.name AS vehicle_name,
                       ib.name AS boat_name,
                       it.name AS trailer_name
                FROM violations v
                LEFT JOIN identities ip ON v.person_identity_id = ip.identity_id
                LEFT JOIN identities iv ON v.vehicle_identity_id = iv.identity_id
                LEFT JOIN identities ib ON v.boat_identity_id = ib.identity_id
                LEFT JOIN identities it ON v.trailer_identity_id = it.identity_id
                {where}
                ORDER BY v.timestamp DESC
                LIMIT %s OFFSET %s
            ''', params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def review_violation(self, violation_id: str, status: str,
                         reviewed_by: str, notes: str = None) -> Optional[Dict]:
        """Review a violation - update its status, reviewer, and notes."""
        with get_cursor() as cursor:
            cursor.execute('''
                UPDATE violations
                SET status = %s, reviewed_by = %s, notes = %s
                WHERE violation_id = %s
                RETURNING *
            ''', (status, reviewed_by, notes, violation_id))
            row = cursor.fetchone()
            return dict(row) if row else None

    # ==================== Visits ====================

    def create_visit(self, person_identity_id: str = None,
                     vehicle_identity_id: str = None,
                     boat_identity_id: str = None,
                     track_ids: list = None,
                     camera_timeline: list = None) -> Dict:
        """Create a new visit record."""
        with get_cursor() as cursor:
            cursor.execute('''
                INSERT INTO visits
                (person_identity_id, vehicle_identity_id, boat_identity_id,
                 track_ids, camera_timeline)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
            ''', (person_identity_id, vehicle_identity_id, boat_identity_id,
                  track_ids or [],
                  extras.Json(camera_timeline) if camera_timeline else extras.Json([])))
            row = cursor.fetchone()
            return dict(row)

    def get_visit(self, visit_id: str) -> Optional[Dict]:
        """Get a visit by ID with identity names via LEFT JOINs."""
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT v.*,
                       ip.name AS person_name,
                       iv.name AS vehicle_name,
                       ib.name AS boat_name
                FROM visits v
                LEFT JOIN identities ip ON v.person_identity_id = ip.identity_id
                LEFT JOIN identities iv ON v.vehicle_identity_id = iv.identity_id
                LEFT JOIN identities ib ON v.boat_identity_id = ib.identity_id
                WHERE v.visit_id = %s
            ''', (visit_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_visits(self, person_identity_id: str = None, date_start=None,
                   date_end=None, limit: int = 100, offset: int = 0) -> List[Dict]:
        """Get visits with optional filters, ordered by arrival_time DESC."""
        conditions = []
        params = []
        if person_identity_id is not None:
            conditions.append('v.person_identity_id = %s')
            params.append(person_identity_id)
        if date_start is not None:
            conditions.append('v.arrival_time >= %s')
            params.append(date_start)
        if date_end is not None:
            conditions.append('v.arrival_time <= %s')
            params.append(date_end)

        where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
        params.extend([limit, offset])

        with get_cursor(commit=False) as cursor:
            cursor.execute(f'''
                SELECT v.*,
                       ip.name AS person_name,
                       iv.name AS vehicle_name,
                       ib.name AS boat_name
                FROM visits v
                LEFT JOIN identities ip ON v.person_identity_id = ip.identity_id
                LEFT JOIN identities iv ON v.vehicle_identity_id = iv.identity_id
                LEFT JOIN identities ib ON v.boat_identity_id = ib.identity_id
                {where}
                ORDER BY v.arrival_time DESC
                LIMIT %s OFFSET %s
            ''', params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def end_visit(self, visit_id: str, departure_time=None) -> bool:
        """End a visit by setting departure_time (defaults to NOW())."""
        with get_cursor() as cursor:
            if departure_time is not None:
                cursor.execute('''
                    UPDATE visits SET departure_time = %s
                    WHERE visit_id = %s AND departure_time IS NULL
                ''', (departure_time, visit_id))
            else:
                cursor.execute('''
                    UPDATE visits SET departure_time = NOW()
                    WHERE visit_id = %s AND departure_time IS NULL
                ''', (visit_id,))
            return cursor.rowcount > 0

    def add_violation_to_visit(self, visit_id: str, violation_id: str) -> bool:
        """Append a violation_id to a visit's violation_ids array."""
        with get_cursor() as cursor:
            cursor.execute('''
                UPDATE visits
                SET violation_ids = array_append(violation_ids, %s::uuid)
                WHERE visit_id = %s
            ''', (violation_id, visit_id))
            return cursor.rowcount > 0

    # ==================== Interpolation Tracks ====================

    def create_interpolation_track(self, video_id: int, class_name: str,
                                    start_pred_id: int, end_pred_id: int,
                                    start_ts: float, end_ts: float,
                                    batch_id: str = None) -> int:
        """Create a new interpolation track record. Returns track ID."""
        with get_cursor() as cursor:
            cursor.execute('''
                INSERT INTO interpolation_tracks
                (video_id, class_name, start_prediction_id, end_prediction_id,
                 start_timestamp, end_timestamp, batch_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            ''', (video_id, class_name, start_pred_id, end_pred_id,
                  start_ts, end_ts, batch_id))
            return cursor.fetchone()['id']

    def update_interpolation_track(self, track_id: int, status: str = None,
                                    frames_generated: int = None,
                                    frames_detected: int = None,
                                    reviewed_by: str = None) -> bool:
        """Update an interpolation track's status and/or counts."""
        updates = []
        values = []
        if status is not None:
            updates.append('status = %s')
            values.append(status)
        if frames_generated is not None:
            updates.append('frames_generated = %s')
            values.append(frames_generated)
        if frames_detected is not None:
            updates.append('frames_detected = %s')
            values.append(frames_detected)
        if reviewed_by is not None:
            updates.append('reviewed_by = %s')
            values.append(reviewed_by)
            updates.append('reviewed_at = NOW()')

        if not updates:
            return False

        values.append(track_id)
        with get_cursor() as cursor:
            cursor.execute(f'''
                UPDATE interpolation_tracks SET {', '.join(updates)}
                WHERE id = %s
            ''', values)
            return cursor.rowcount > 0

    def get_interpolation_tracks(self, video_id: int = None, status: str = None) -> List[Dict]:
        """Get interpolation tracks, optionally filtered by video_id and/or status."""
        with get_cursor(commit=False) as cursor:
            conditions = []
            params = []
            if video_id is not None:
                conditions.append('t.video_id = %s')
                params.append(video_id)
            if status is not None:
                conditions.append('t.status = %s')
                params.append(status)

            where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
            cursor.execute(f'''
                SELECT t.*, v.filename as video_filename, v.title as video_title
                FROM interpolation_tracks t
                JOIN videos v ON t.video_id = v.id
                {where}
                ORDER BY t.created_at DESC
            ''', params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_interpolation_track(self, track_id: int) -> Optional[Dict]:
        """Get a single interpolation track with anchor prediction details."""
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT t.*,
                       v.filename as video_filename, v.title as video_title,
                       v.width as video_width, v.height as video_height,
                       sp.timestamp as start_pred_timestamp,
                       sp.bbox_x as start_bbox_x, sp.bbox_y as start_bbox_y,
                       sp.bbox_width as start_bbox_width, sp.bbox_height as start_bbox_height,
                       sp.confidence as start_confidence,
                       sp.predicted_tags as start_predicted_tags,
                       sp.corrected_tags as start_corrected_tags,
                       sp.corrected_bbox as start_corrected_bbox,
                       ep.timestamp as end_pred_timestamp,
                       ep.bbox_x as end_bbox_x, ep.bbox_y as end_bbox_y,
                       ep.bbox_width as end_bbox_width, ep.bbox_height as end_bbox_height,
                       ep.confidence as end_confidence,
                       ep.predicted_tags as end_predicted_tags,
                       ep.corrected_tags as end_corrected_tags,
                       ep.corrected_bbox as end_corrected_bbox
                FROM interpolation_tracks t
                JOIN videos v ON t.video_id = v.id
                LEFT JOIN ai_predictions sp ON t.start_prediction_id = sp.id
                LEFT JOIN ai_predictions ep ON t.end_prediction_id = ep.id
                WHERE t.id = %s
            ''', (track_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_track_predictions(self, batch_id: str) -> List[Dict]:
        """Get all predictions belonging to an interpolation track batch."""
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT * FROM ai_predictions
                WHERE batch_id = %s
                ORDER BY timestamp ASC
            ''', (batch_id,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_approved_predictions_for_class(self, video_id: int, class_name: str,
                                            model_name: str) -> List[Dict]:
        """Get approved predictions matching a class for a video."""
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT * FROM ai_predictions
                WHERE video_id = %s
                AND model_name = %s
                AND review_status IN ('approved', 'auto_approved')
                AND (
                    predicted_tags->>'class' = %s
                    OR corrected_tags->>'class' = %s
                )
                ORDER BY timestamp ASC
            ''', (video_id, model_name, class_name, class_name))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def interpolation_track_exists(self, pred_id_a: int, pred_id_b: int) -> bool:
        """Check if an interpolation track already exists for a pair of predictions."""
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT COUNT(*) as count FROM interpolation_tracks
                WHERE (start_prediction_id = %s AND end_prediction_id = %s)
                   OR (start_prediction_id = %s AND end_prediction_id = %s)
            ''', (pred_id_a, pred_id_b, pred_id_b, pred_id_a))
            return cursor.fetchone()['count'] > 0
