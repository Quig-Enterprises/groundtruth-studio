from flask import Blueprint, request, jsonify, render_template
from psycopg2 import extras
from db_connection import get_connection
from services import yolo_exporter
import json
import logging

yolo_export_bp = Blueprint('yolo_export', __name__)
logger = logging.getLogger(__name__)

@yolo_export_bp.route('/yolo-export')
def yolo_export_page():
    """YOLO export configuration page"""
    return render_template('yolo_export.html')

@yolo_export_bp.route('/api/yolo/configs', methods=['GET'])
def get_yolo_configs():
    """Get all YOLO export configurations"""
    try:
        configs = yolo_exporter.get_export_configs()
        return jsonify({'success': True, 'configs': configs})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@yolo_export_bp.route('/api/yolo/configs', methods=['POST'])
def create_yolo_config():
    """Create new YOLO export configuration"""
    try:
        data = request.get_json()
        config_name = data.get('config_name')
        class_mapping = data.get('class_mapping')  # Dict: {activity_tag: class_id}
        description = data.get('description', '')

        if not config_name or not class_mapping:
            return jsonify({'success': False, 'error': 'Missing required fields'}), 400

        config_id = yolo_exporter.create_export_config(
            config_name=config_name,
            class_mapping=class_mapping,
            description=description,
            include_reviewed_only=bool(data.get('include_reviewed_only', False)),
            include_ai_generated=bool(data.get('include_ai_generated', True)),
            include_negative_examples=bool(data.get('include_negative_examples', True)),
            min_confidence=data.get('min_confidence', 0.0)
        )

        return jsonify({'success': True, 'config_id': config_id})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@yolo_export_bp.route('/api/yolo/configs/<int:config_id>', methods=['GET'])
def get_yolo_config(config_id):
    """Get a single YOLO export configuration"""
    try:
        configs = yolo_exporter.get_export_configs()
        config = next((c for c in configs if c['id'] == config_id), None)
        if not config:
            return jsonify({'success': False, 'error': 'Config not found'}), 404
        return jsonify({'success': True, 'config': config})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@yolo_export_bp.route('/api/yolo/configs/<int:config_id>', methods=['PUT'])
def update_yolo_config(config_id):
    """Update an existing YOLO export configuration"""
    try:
        data = request.get_json()

        with get_connection() as conn:
            cursor = conn.cursor()

            # Build update fields
            updates = []
            values = []

            if 'config_name' in data:
                updates.append('config_name = %s')
                values.append(data['config_name'])
            if 'description' in data:
                updates.append('description = %s')
                values.append(data['description'])
            if 'class_mapping' in data:
                updates.append('class_mapping = %s')
                values.append(json.dumps(data['class_mapping']))
            if 'include_reviewed_only' in data:
                updates.append('include_reviewed_only = %s')
                values.append(bool(data['include_reviewed_only']))
            if 'include_ai_generated' in data:
                updates.append('include_ai_generated = %s')
                values.append(bool(data['include_ai_generated']))
            if 'include_negative_examples' in data:
                updates.append('include_negative_examples = %s')
                values.append(bool(data['include_negative_examples']))

            if not updates:
                return jsonify({'success': False, 'error': 'No fields to update'}), 400

            values.append(config_id)
            cursor.execute(
                f'UPDATE yolo_export_configs SET {", ".join(updates)} WHERE id = %s',
                values
            )

            if cursor.rowcount == 0:
                return jsonify({'success': False, 'error': 'Config not found'}), 404

            conn.commit()

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@yolo_export_bp.route('/api/yolo/configs/<int:config_id>/health', methods=['GET'])
def get_yolo_config_health(config_id):
    """Dataset health analysis for a YOLO export config"""
    try:
        preview = yolo_exporter.get_export_preview(config_id)

        class_counts = preview.get('class_distribution', {})
        total_annotations = preview.get('total_annotations', 0)
        total_videos = preview.get('video_count', 0)

        # Count frames (distinct timestamps across videos)
        total_frames = 0
        with get_connection() as conn:
            cursor = conn.cursor()
            config_data = None
            cursor.execute('SELECT class_mapping FROM yolo_export_configs WHERE id = %s', (config_id,))
            row = cursor.fetchone()
            if row:
                config_data = json.loads(row[0]) if row[0] else {}

            # Count distinct frames that have matching annotations
            activity_tags = list((config_data or {}).keys())
            if activity_tags:
                placeholders = ','.join(['%s'] * len(activity_tags))
                cursor.execute(f'''
                    SELECT COUNT(DISTINCT (video_id, timestamp))
                    FROM keyframe_annotations
                    WHERE activity_tag IN ({placeholders}) AND bbox_x IS NOT NULL
                ''', activity_tags)
                result = cursor.fetchone()
                total_frames = result[0] if result else 0

        # Generate warnings
        warnings = []
        recommendations = []

        if total_annotations == 0:
            warnings.append({
                'level': 'critical',
                'code': 'NO_ANNOTATIONS',
                'message': 'No annotations match this configuration. Ensure the class mapping includes activity tags that have annotations.'
            })
            recommendations.append('Check that your class mapping includes activity tags with annotations')

        if 0 < total_annotations < 10:
            warnings.append({
                'level': 'critical',
                'code': 'INSUFFICIENT_DATA',
                'message': f'Only {total_annotations} samples found — need at least 10 for train/val split. Add {10 - total_annotations} more annotated samples to proceed.'
            })

        if 10 <= total_annotations < 50:
            warnings.append({
                'level': 'critical',
                'code': 'VERY_SMALL_DATASET',
                'message': f'Only {total_annotations}/50 samples — need at least 50 to avoid overfitting. Add {50 - total_annotations} more annotated samples to proceed.'
            })
            recommendations.append('Add more annotated data for better training results')

        # Per-class warnings
        for class_name, count in class_counts.items():
            if count == 0:
                warnings.append({
                    'level': 'critical',
                    'code': 'EMPTY_CLASS',
                    'message': f'Class "{class_name}" has 0 annotations. Add annotations for this class or remove it from the config.'
                })
            elif count < 100:
                warnings.append({
                    'level': 'warning',
                    'code': 'LOW_SAMPLES_CLASS',
                    'message': f'Class "{class_name}" has only {count} samples (recommend 100+)'
                })
                recommendations.append(f'Add more "{class_name}" annotations for better accuracy')

        # Class imbalance
        if class_counts and len(class_counts) > 1:
            counts = [c for c in class_counts.values() if c > 0]
            if counts:
                max_count = max(counts)
                min_count = min(counts)
                if min_count > 0 and max_count / min_count > 5:
                    warnings.append({
                        'level': 'warning',
                        'code': 'CLASS_IMBALANCE',
                        'message': f'Significant class imbalance (ratio {max_count}:{min_count})'
                    })
                    recommendations.append('Consider balancing classes by adding more annotations to underrepresented classes')

        # Low diversity
        if total_videos == 1:
            warnings.append({
                'level': 'warning',
                'code': 'LOW_DIVERSITY',
                'message': 'All data from a single video source - low diversity'
            })
            recommendations.append('Add annotations from different video sources for better generalization')

        return jsonify({
            'success': True,
            'health': {
                'total_annotations': total_annotations,
                'total_frames': total_frames,
                'total_videos': total_videos,
                'class_counts': class_counts,
                'warnings': warnings,
                'recommendations': list(set(recommendations))
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@yolo_export_bp.route('/api/yolo/configs/<int:config_id>/filters', methods=['POST'])
def add_yolo_filter(config_id):
    """Add filter to YOLO export configuration"""
    try:
        data = request.get_json()
        filter_type = data.get('filter_type')
        filter_value = data.get('filter_value')
        is_exclusion = data.get('is_exclusion', False)

        if not filter_type or not filter_value:
            return jsonify({'success': False, 'error': 'Missing required fields'}), 400

        yolo_exporter.add_filter(config_id, filter_type, filter_value, is_exclusion)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@yolo_export_bp.route('/api/yolo/configs/<int:config_id>/preview', methods=['GET'])
def preview_yolo_export(config_id):
    """Preview YOLO export without actually exporting"""
    try:
        preview = yolo_exporter.get_export_preview(config_id)
        return jsonify({'success': True, 'preview': preview})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@yolo_export_bp.route('/api/yolo/configs/<int:config_id>/export', methods=['POST'])
def export_yolo_dataset(config_id):
    """Export YOLO dataset"""
    try:
        data = request.get_json() or {}
        output_name = data.get('output_name')

        result = yolo_exporter.export_dataset(config_id, output_name)
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@yolo_export_bp.route('/api/yolo/activity-tags', methods=['GET'])
def get_yolo_activity_tags():
    """Get all unique activity tags from annotations for YOLO export"""
    try:
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
            cursor.execute('''
                SELECT DISTINCT activity_tag, COUNT(*) as count
                FROM keyframe_annotations
                WHERE activity_tag IS NOT NULL AND activity_tag != ''
                GROUP BY activity_tag
                ORDER BY count DESC
            ''')
            tags = [{'name': row['activity_tag'], 'count': row['count']} for row in cursor.fetchall()]
        return jsonify({'success': True, 'tags': tags})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
