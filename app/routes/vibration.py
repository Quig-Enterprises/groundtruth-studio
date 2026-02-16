from flask import Blueprint, request, jsonify, render_template, g
import services
from services import db, vibration_exporter
import json
import logging

vibration_bp = Blueprint('vibration', __name__)
logger = logging.getLogger(__name__)


@vibration_bp.route('/vibration-export')
def vibration_export_page():
    return render_template('vibration_export.html')


@vibration_bp.route('/api/vibration/tags', methods=['GET'])
def get_vibration_tags():
    """Get all unique time-range tag names with counts"""
    try:
        tags = vibration_exporter.get_available_tags()
        return jsonify({'success': True, 'tags': tags})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@vibration_bp.route('/api/vibration/export', methods=['POST'])
def export_vibration_data():
    """Export vibration/time-range annotations as CSV/Parquet"""
    try:
        data = request.get_json()
        result = vibration_exporter.export_dataset(
            output_name=data.get('output_name'),
            tag_filter=data.get('tag_filter'),
            formats=data.get('formats'),
            val_split=data.get('val_split', 0.2),
            seed=data.get('seed', 42)
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
