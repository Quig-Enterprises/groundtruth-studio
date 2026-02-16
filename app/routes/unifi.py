from flask import Blueprint, request, jsonify, g
from db_connection import get_cursor
import services
from services import db, sync_config
from unifi_protect_client import UniFiProtectClient, UniFiProtectIntegration
import json
import logging

unifi_bp = Blueprint('unifi', __name__)
logger = logging.getLogger(__name__)

# Module-level client cache
unifi_client = None

# ===== UniFi Protect Endpoints =====

@unifi_bp.route('/api/sync/unifi/config', methods=['GET'])
def get_unifi_config():
    """Get UniFi Protect configuration status"""
    try:
        has_credentials = sync_config.has_unifi_credentials()
        credentials = sync_config.get_unifi_credentials() if has_credentials else None

        return jsonify({
            'success': True,
            'configured': has_credentials,
            'host': credentials['host'] if credentials else None,
            'port': credentials['port'] if credentials else None
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@unifi_bp.route('/api/sync/unifi/config', methods=['POST'])
def set_unifi_config():
    """Set UniFi Protect credentials"""
    try:
        data = request.json
        host = data.get('host')
        username = data.get('username')
        password = data.get('password')
        port = data.get('port', 443)
        verify_ssl = data.get('verify_ssl', True)

        if not host or not username or not password:
            return jsonify({'success': False, 'error': 'Host, username, and password required'}), 400

        sync_config.set_unifi_credentials(host, username, password, port, verify_ssl)

        return jsonify({'success': True, 'message': 'UniFi Protect credentials saved'})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@unifi_bp.route('/api/sync/unifi/test', methods=['POST'])
def test_unifi_connection():
    """Test connection to UniFi Protect"""
    try:
        global unifi_client

        credentials = sync_config.get_unifi_credentials()
        if not credentials:
            return jsonify({'success': False, 'error': 'UniFi Protect credentials not configured'}), 400

        # Initialize client
        unifi_client = UniFiProtectClient(
            host=credentials['host'],
            port=credentials['port'],
            username=credentials['username'],
            password=credentials['password'],
            verify_ssl=credentials['verify_ssl']
        )

        # Test authentication
        success = unifi_client.authenticate()

        return jsonify({
            'success': success,
            'message': 'UniFi Protect library not yet implemented' if not success else 'Connected successfully'
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ===== Sync History Endpoints =====

@unifi_bp.route('/api/sync/history', methods=['GET'])
def get_sync_history():
    """Get sync history"""
    try:
        limit = int(request.args.get('limit', 50))
        history = sync_config.get_sync_history(limit)

        return jsonify({
            'success': True,
            'history': history
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
