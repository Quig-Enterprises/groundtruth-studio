import os
from datetime import datetime
from typing import List, Dict, Optional

import psycopg2
from psycopg2 import extras

from db_connection import get_connection, get_cursor


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
                  file_size: int = None, thumbnail_path: str = None, notes: str = None) -> int:
        with get_cursor() as cursor:
            cursor.execute('''
                INSERT INTO videos (filename, original_url, title, duration, width, height,
                                  file_size, thumbnail_path, notes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            ''', (filename, original_url, title, duration, width, height, file_size, thumbnail_path, notes))
            result = cursor.fetchone()
            return result['id']

    def get_video(self, video_id: int) -> Optional[Dict]:
        with get_cursor(commit=False) as cursor:
            cursor.execute('SELECT * FROM videos WHERE id = %s', (video_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_all_videos(self, limit: int = 100, offset: int = 0) -> List[Dict]:
        with get_cursor(commit=False) as cursor:
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
                         'file_size', 'thumbnail_path', 'notes']

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
