import json
from datetime import datetime
from typing import List, Dict, Optional

import psycopg2
from psycopg2 import extras

from db_connection import get_cursor


class AnnotationMixin:
    """Keyframe annotations, time-range tags, taxonomy, and tag suggestions."""

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
