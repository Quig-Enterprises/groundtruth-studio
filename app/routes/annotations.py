from flask import Blueprint, request, jsonify, g
from db_connection import get_connection, get_cursor
from psycopg2 import extras
import services
from services import db
import json
import logging

annotations_bp = Blueprint('annotations', __name__)
logger = logging.getLogger(__name__)


@annotations_bp.route('/api/videos/<int:video_id>/time-range-tags', methods=['GET'])
def get_time_range_tags(video_id):
    """Get all time-range tags for a video"""
    tags = db.get_time_range_tags(video_id)
    return jsonify({'success': True, 'tags': tags})

@annotations_bp.route('/api/videos/<int:video_id>/time-range-tags', methods=['POST'])
def add_time_range_tag(video_id):
    """Add time-range tag"""
    data = request.get_json()

    tag_name = data.get('tag_name', '').strip()
    start_time = data.get('start_time')

    if not tag_name or start_time is None:
        return jsonify({'success': False, 'error': 'tag_name and start_time required'}), 400

    tag_id = db.add_time_range_tag(
        video_id=video_id,
        tag_name=tag_name,
        start_time=start_time,
        end_time=data.get('end_time'),
        is_negative=data.get('is_negative', False),
        comment=data.get('comment')
    )

    return jsonify({'success': True, 'tag_id': tag_id})

@annotations_bp.route('/api/time-range-tags/<int:tag_id>', methods=['PUT'])
def update_time_range_tag(tag_id):
    """Update time-range tag (close tag, add comment)"""
    data = request.get_json()

    success = db.update_time_range_tag(
        tag_id=tag_id,
        tag_name=data.get('tag_name'),
        end_time=data.get('end_time'),
        is_negative=data.get('is_negative'),
        comment=data.get('comment')
    )

    return jsonify({'success': success})

@annotations_bp.route('/api/time-range-tags/<int:tag_id>', methods=['GET'])
def get_time_range_tag(tag_id):
    """Get a single time-range tag by ID"""
    tag = db.get_time_range_tag_by_id(tag_id)
    if tag:
        return jsonify({'success': True, 'tag': tag})
    return jsonify({'success': False, 'error': 'Tag not found'}), 404

@annotations_bp.route('/api/time-range-tags/<int:tag_id>', methods=['DELETE'])
def delete_time_range_tag(tag_id):
    """Delete time-range tag"""
    success = db.delete_time_range_tag(tag_id)
    return jsonify({'success': success})

@annotations_bp.route('/api/videos/<int:video_id>/keyframe-annotations', methods=['GET'])
def get_keyframe_annotations(video_id):
    """Get all keyframe annotations for a video"""
    annotations = db.get_keyframe_annotations(video_id)
    return jsonify({'success': True, 'annotations': annotations})

@annotations_bp.route('/api/videos/<int:video_id>/keyframe-annotations', methods=['POST'])
def add_keyframe_annotation(video_id):
    """Add keyframe annotation with bounding box"""
    data = request.get_json()

    required = ['timestamp', 'bbox_x', 'bbox_y', 'bbox_width', 'bbox_height']
    if not all(k in data for k in required):
        return jsonify({'success': False, 'error': 'Missing required fields'}), 400

    annotation_id = db.add_keyframe_annotation(
        video_id=video_id,
        timestamp=data['timestamp'],
        bbox_x=data['bbox_x'],
        bbox_y=data['bbox_y'],
        bbox_width=data['bbox_width'],
        bbox_height=data['bbox_height'],
        activity_tag=data.get('activity_tag'),
        moment_tag=data.get('moment_tag'),
        is_negative=data.get('is_negative', False),
        comment=data.get('comment'),
        reviewed=data.get('reviewed', True),
        source=data.get('source'),
        source_prediction_id=data.get('source_prediction_id')
    )

    return jsonify({'success': True, 'annotation_id': annotation_id})

@annotations_bp.route('/api/keyframe-annotations/<int:annotation_id>', methods=['GET'])
def get_keyframe_annotation(annotation_id):
    """Get a single keyframe annotation by ID"""
    annotation = db.get_keyframe_annotation_by_id(annotation_id)
    if annotation:
        return jsonify({'success': True, 'annotation': annotation})
    return jsonify({'success': False, 'error': 'Annotation not found'}), 404

@annotations_bp.route('/api/keyframe-annotations/<int:annotation_id>', methods=['PUT'])
def update_keyframe_annotation(annotation_id):
    """Update keyframe annotation"""
    data = request.get_json()

    success = db.update_keyframe_annotation(
        annotation_id=annotation_id,
        bbox_x=data.get('bbox_x'),
        bbox_y=data.get('bbox_y'),
        bbox_width=data.get('bbox_width'),
        bbox_height=data.get('bbox_height'),
        activity_tag=data.get('activity_tag'),
        moment_tag=data.get('moment_tag'),
        is_negative=data.get('is_negative'),
        comment=data.get('comment'),
        reviewed=data.get('reviewed')
    )

    return jsonify({'success': success})

@annotations_bp.route('/api/keyframe-annotations/<int:annotation_id>', methods=['DELETE'])
def delete_keyframe_annotation(annotation_id):
    """Delete keyframe annotation"""
    success = db.delete_keyframe_annotation(annotation_id)
    return jsonify({'success': success})

@annotations_bp.route('/api/activity-tags', methods=['GET'])
def get_activity_tags():
    """Get all unique activity tags for auto-suggest"""
    tags = db.get_all_activity_tags()
    return jsonify({'success': True, 'tags': tags})

@annotations_bp.route('/api/moment-tags', methods=['GET'])
def get_moment_tags():
    """Get all unique moment tags for auto-suggest"""
    tags = db.get_all_moment_tags()
    return jsonify({'success': True, 'tags': tags})

@annotations_bp.route('/api/tag-suggestions', methods=['GET'])
def get_tag_suggestions():
    """Get tag suggestions, optionally filtered by category"""
    category = request.args.get('category')
    suggestions = db.get_tag_suggestions_by_category(category)
    return jsonify({'success': True, 'suggestions': suggestions})

@annotations_bp.route('/api/tag-suggestions/categories', methods=['GET'])
def get_suggestion_categories():
    """Get all tag suggestion categories"""
    categories = db.get_all_suggestion_categories()
    return jsonify({'success': True, 'categories': categories})

@annotations_bp.route('/api/tag-suggestions', methods=['POST'])
def add_tag_suggestion():
    """Add new tag suggestion"""
    data = request.get_json()

    category = data.get('category', '').strip()
    tag_text = data.get('tag_text', '').strip()

    if not category or not tag_text:
        return jsonify({'success': False, 'error': 'category and tag_text required'}), 400

    suggestion_id = db.add_tag_suggestion(
        category=category,
        tag_text=tag_text,
        is_negative=data.get('is_negative', False),
        description=data.get('description'),
        sort_order=data.get('sort_order', 0)
    )

    return jsonify({'success': True, 'suggestion_id': suggestion_id})

@annotations_bp.route('/api/tag-suggestions/<int:suggestion_id>', methods=['PUT'])
def update_tag_suggestion(suggestion_id):
    """Update tag suggestion"""
    data = request.get_json()

    success = db.update_tag_suggestion(
        suggestion_id=suggestion_id,
        category=data.get('category'),
        tag_text=data.get('tag_text'),
        is_negative=data.get('is_negative'),
        description=data.get('description'),
        sort_order=data.get('sort_order')
    )

    return jsonify({'success': success})

@annotations_bp.route('/api/tag-suggestions/<int:suggestion_id>', methods=['DELETE'])
def delete_tag_suggestion(suggestion_id):
    """Delete tag suggestion"""
    success = db.delete_tag_suggestion(suggestion_id)
    return jsonify({'success': success})

@annotations_bp.route('/api/tag-suggestions/seed', methods=['POST'])
def seed_tag_suggestions():
    """Seed database with default tag suggestions"""
    db.seed_default_tag_suggestions()
    return jsonify({'success': True, 'message': 'Default tag suggestions added'})

# Tag Group API Endpoints
@annotations_bp.route('/api/tag-groups', methods=['GET'])
def get_tag_groups():
    """Get all tag groups, optionally filtered by annotation type"""
    annotation_type = request.args.get('annotation_type')
    groups = db.get_tag_groups(annotation_type)

    # Add options for each group
    for group in groups:
        group['options'] = db.get_tag_options(group['id'])

    return jsonify({'success': True, 'groups': groups})

@annotations_bp.route('/api/tag-groups/<group_name>', methods=['GET'])
def get_tag_group_by_name(group_name):
    """Get a specific tag group with its options"""
    group = db.get_tag_group_by_name(group_name)
    if not group:
        return jsonify({'success': False, 'error': 'Tag group not found'}), 404

    group['options'] = db.get_tag_options(group['id'])
    return jsonify({'success': True, 'group': group})

@annotations_bp.route('/api/tag-schema', methods=['GET'])
def get_tag_schema():
    """Get complete tag schema for dynamic form generation

    Query params:
    - annotation_type: 'time_range' or 'keyframe'
    - ground_truth: optional filter by ground truth value
    - is_negative: optional filter for negative examples
    """
    annotation_type = request.args.get('annotation_type')
    ground_truth = request.args.get('ground_truth')
    is_negative = request.args.get('is_negative') == 'true'

    groups = db.get_tag_groups(annotation_type)

    # Add options for each group
    result_groups = []
    for group in groups:
        group['options'] = db.get_tag_options(group['id'])

        # Apply conditional display logic
        # This is a simplified version - full logic would be more complex
        include_group = True

        # False positive groups only show when is_negative is true
        if group['group_name'].startswith('false_positive_') and not is_negative:
            include_group = False

        # Present/Absent indicators conditional on ground truth and negative flag
        if group['group_name'] == 'present_indicators' and (ground_truth != 'power_loading' or is_negative):
            include_group = False
        if group['group_name'] == 'absent_indicators' and (ground_truth != 'power_loading' or not is_negative):
            include_group = False

        # Power loading specific groups
        power_loading_groups = ['violation_context', 'motor_state', 'boat_motion']
        if group['group_name'] in power_loading_groups and ground_truth != 'power_loading':
            include_group = False

        # License plate specific groups
        license_plate_groups = ['vehicle_type', 'plate_state']
        if group['group_name'] in license_plate_groups and ground_truth != 'license_plate':
            include_group = False

        # Face detection specific groups
        face_groups = ['face_angle', 'face_obstruction']
        if group['group_name'] in face_groups and ground_truth != 'face_detected':
            include_group = False

        if include_group:
            result_groups.append(group)

    return jsonify({'success': True, 'groups': result_groups})

@annotations_bp.route('/api/annotations/<int:annotation_id>/tags', methods=['POST'])
def add_annotation_tags(annotation_id):
    """Add tags to an annotation

    Request body:
    {
        "annotation_type": "time_range" or "keyframe",
        "tags": {
            "ground_truth": "power_loading",
            "confidence_level": "certain",
            "lighting_conditions": ["sun_glare", "bright_overexposed"],
            ...
        }
    }
    """
    data = request.get_json()
    annotation_type = data.get('annotation_type')
    tags = data.get('tags', {})

    if not annotation_type or annotation_type not in ['time_range', 'keyframe']:
        return jsonify({'success': False, 'error': 'Invalid annotation_type'}), 400

    # Delete existing tags first
    db.delete_annotation_tags(annotation_id, annotation_type)

    # Handle structured scenario data (scenario, bboxes, notVisible, notPresent, skipped)
    # Store these as JSON in a special "_scenario_data" tag group
    scenario_data_keys = ['scenario', 'bboxes', 'notVisible', 'notPresent', 'skipped']
    scenario_data = {k: v for k, v in tags.items() if k in scenario_data_keys}

    print(f"[API] add_annotation_tags called for annotation_id={annotation_id}, type={annotation_type}")
    print(f"[API] scenario_data keys found: {list(scenario_data.keys())}")
    print(f"[API] scenario_data['bboxes'] has {len(scenario_data.get('bboxes', {}))} items")

    if scenario_data:
        # Ensure the _scenario_data tag group exists
        scenario_group = db.get_tag_group_by_name('_scenario_data')
        if not scenario_group:
            # Create it if it doesn't exist
            with get_connection() as conn:
                cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
                cursor.execute('''
                    INSERT INTO tag_groups (group_name, display_name, group_type, description, is_required, applies_to, sort_order)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (group_name) DO NOTHING
                ''', ('_scenario_data', 'Scenario Data', 'text', 'Structured scenario data (JSON)', 0, 'both', 999))
                conn.commit()
            scenario_group = db.get_tag_group_by_name('_scenario_data')
            print(f"[API] Created _scenario_data tag group with id={scenario_group['id']}")

        if scenario_group:
            json_data = json.dumps(scenario_data)
            print(f"[API] Saving JSON data ({len(json_data)} bytes): {json_data[:200]}...")
            db.add_annotation_tag(annotation_id, annotation_type, scenario_group['id'], json_data)

    # Add regular tags
    for group_name, tag_value in tags.items():
        # Skip scenario data keys (already handled above)
        if group_name in scenario_data_keys:
            continue

        group = db.get_tag_group_by_name(group_name)
        if not group:
            continue

        # For checkbox groups, tag_value is a list
        if group['group_type'] == 'checkbox' and isinstance(tag_value, list):
            tag_value_str = ','.join(tag_value)
        else:
            tag_value_str = str(tag_value) if tag_value else ''

        if tag_value_str:
            db.add_annotation_tag(annotation_id, annotation_type, group['id'], tag_value_str)

    return jsonify({'success': True})

@annotations_bp.route('/api/annotations/<int:annotation_id>/tags', methods=['GET'])
def get_annotation_tags_api(annotation_id):
    """Get all tags for an annotation"""
    annotation_type = request.args.get('annotation_type')
    if not annotation_type:
        return jsonify({'success': False, 'error': 'annotation_type required'}), 400

    tags = db.get_annotation_tags(annotation_id, annotation_type)
    print(f"[API] get_annotation_tags for annotation_id={annotation_id}, type={annotation_type}")
    print(f"[API] Retrieved {len(tags)} tag records from database")

    # Format tags into a dictionary grouped by tag group
    tags_dict = {}
    for tag in tags:
        group_name = tag['group_name']
        tag_value = tag['tag_value']

        # Parse JSON scenario data
        if group_name == '_scenario_data':
            try:
                print(f"[API] Found _scenario_data tag, parsing JSON ({len(tag_value)} bytes)")
                scenario_data = json.loads(tag_value)
                print(f"[API] Parsed scenario_data keys: {list(scenario_data.keys())}")
                if 'bboxes' in scenario_data:
                    print(f"[API] scenario_data['bboxes'] has {len(scenario_data['bboxes'])} items")
                    print(f"[API] bbox keys: {list(scenario_data['bboxes'].keys())}")
                # Merge scenario data into tags_dict at top level
                tags_dict.update(scenario_data)
            except Exception as e:
                print(f"[API] Error parsing JSON scenario data: {e}")
                pass  # Skip if JSON parsing fails
            continue

        # For checkbox groups, split comma-separated values back into list
        if tag['group_type'] == 'checkbox' and ',' in tag_value:
            tags_dict[group_name] = tag_value.split(',')
        else:
            tags_dict[group_name] = tag_value

    print(f"[API] Returning tags_dict with keys: {list(tags_dict.keys())}")
    return jsonify({'success': True, 'tags': tags_dict})

@annotations_bp.route('/api/tag-taxonomy/seed', methods=['POST'])
def seed_tag_taxonomy():
    """Seed database with comprehensive tag taxonomy"""
    try:
        db.seed_comprehensive_tag_taxonomy()
        return jsonify({'success': True, 'message': '29 tag groups seeded successfully'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
