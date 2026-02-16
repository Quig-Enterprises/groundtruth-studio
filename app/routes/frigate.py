from flask import Blueprint, request, jsonify
from frigate_ingester import get_ingester, start_background_ingester, stop_background_ingester
import logging

frigate_bp = Blueprint('frigate', __name__)
logger = logging.getLogger(__name__)

# ==================== Frigate Ingester Endpoints ====================

@frigate_bp.route('/api/frigate/start', methods=['POST'])
def frigate_start():
    """Start the Frigate background ingester."""
    role = request.headers.get('X-Auth-Role', '')
    if role != 'admin':
        return jsonify({'success': False, 'error': 'Admin access required'}), 403

    try:
        data = request.get_json() or {}
        interval = data.get('interval', 60)

        start_background_ingester(interval)

        return jsonify({
            'success': True,
            'message': 'Frigate ingester started',
            'interval': interval
        })
    except Exception as e:
        logger.error(f"Failed to start Frigate ingester: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@frigate_bp.route('/api/frigate/stop', methods=['POST'])
def frigate_stop():
    """Stop the Frigate background ingester."""
    role = request.headers.get('X-Auth-Role', '')
    if role != 'admin':
        return jsonify({'success': False, 'error': 'Admin access required'}), 403

    try:
        stop_background_ingester()

        return jsonify({
            'success': True,
            'message': 'Frigate ingester stopped'
        })
    except Exception as e:
        logger.error(f"Failed to stop Frigate ingester: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@frigate_bp.route('/api/frigate/status', methods=['GET'])
def frigate_status():
    """Get the status of the Frigate ingester."""
    role = request.headers.get('X-Auth-Role', '')
    if role != 'admin':
        return jsonify({'success': False, 'error': 'Admin access required'}), 403

    try:
        ingester = get_ingester()

        try:
            cameras = ingester.get_cameras()
        except Exception as e:
            logger.warning(f"Failed to get cameras (Frigate may not be reachable): {e}")
            cameras = []

        return jsonify({
            'success': True,
            'running': not ingester._stop_flag.is_set(),
            'interval': ingester.interval,
            'cameras': cameras
        })
    except Exception as e:
        logger.error(f"Failed to get Frigate ingester status: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@frigate_bp.route('/api/frigate/capture', methods=['POST'])
def frigate_capture():
    """Run a single Frigate capture cycle."""
    role = request.headers.get('X-Auth-Role', '')
    if role != 'admin':
        return jsonify({'success': False, 'error': 'Admin access required'}), 403

    try:
        ingester = get_ingester()
        cycle_result = ingester.run_cycle()

        return jsonify({
            'success': True,
            **cycle_result
        })
    except Exception as e:
        logger.error(f"Failed to run Frigate capture cycle: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
