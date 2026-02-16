from flask import Blueprint, request, jsonify, g
from psycopg2 import extras
from db_connection import get_connection
import services
from services import db
import json
import logging

models_bp = Blueprint('models', __name__)
logger = logging.getLogger(__name__)


@models_bp.route('/api/ai/models', methods=['GET'])
def get_registered_models():
    """List all registered models with stats"""
    try:
        model_name = request.args.get('model_name')
        active_only = request.args.get('active_only', 'true').lower() == 'true'
        models = db.get_model_registry(model_name, active_only)
        return jsonify({'success': True, 'models': models})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@models_bp.route('/api/ai/models/<model_name>/stats', methods=['GET'])
def get_model_performance_stats(model_name):
    """Get model performance and prediction stats"""
    try:
        model_version = request.args.get('model_version')
        stats = db.get_model_stats(model_name, model_version)
        return jsonify({'success': True, 'model_name': model_name, 'stats': stats})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@models_bp.route('/api/ai/models/<model_name>/thresholds', methods=['PUT'])
def update_model_thresholds(model_name):
    """Update routing confidence thresholds for a model"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'Request body required'}), 400

        model_version = data.get('model_version')
        thresholds = data.get('thresholds')

        if not model_version or not thresholds:
            return jsonify({'success': False, 'error': 'model_version and thresholds required'}), 400

        # Validate threshold values
        for key in ('auto_approve', 'review', 'auto_reject'):
            if key in thresholds:
                val = thresholds[key]
                if not isinstance(val, (int, float)) or val < 0 or val > 1:
                    return jsonify({'success': False, 'error': f'{key} must be between 0.0 and 1.0'}), 400

        updated = db.update_model_thresholds(model_name, model_version, thresholds)
        if not updated:
            return jsonify({'success': False, 'error': 'Model not found'}), 404

        return jsonify({'success': True, 'model': updated})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@models_bp.route('/api/ai/models/<model_name>/metrics', methods=['GET'])
def get_model_metrics_history(model_name):
    """Get training metrics history for a model"""
    try:
        model_version = request.args.get('model_version')
        limit = request.args.get('limit', 20, type=int)
        metrics = db.get_training_metrics_history(model_name, model_version, limit)
        return jsonify({'success': True, 'model_name': model_name, 'metrics': metrics})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@models_bp.route('/api/ai/models/<model_name>/toggle', methods=['POST'])
def toggle_model(model_name):
    """Activate or deactivate a model."""
    data = request.json or {}
    model_version = data.get('model_version')
    active = data.get('active')  # True/False

    if active is None:
        return jsonify({'success': False, 'error': 'active field required (true/false)'}), 400

    try:
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
            if model_version:
                cursor.execute('''
                    UPDATE model_registry SET is_active = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE model_name = %s AND model_version = %s
                ''', (active, model_name, model_version))
            else:
                cursor.execute('''
                    UPDATE model_registry SET is_active = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE model_name = %s
                ''', (active, model_name))

            if cursor.rowcount == 0:
                return jsonify({'success': False, 'error': 'Model not found'}), 404

            conn.commit()

        return jsonify({
            'success': True,
            'model_name': model_name,
            'active': active,
            'message': f'Model {"activated" if active else "deactivated"}'
        })
    except Exception as e:
        logger.error(f'Failed to toggle model {model_name}: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500
