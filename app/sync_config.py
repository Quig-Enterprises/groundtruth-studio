"""
Sync Configuration Management

Securely stores and manages credentials for:
- EcoEye alerts.ecoeyetech.com
- UniFi Protect instances

Credentials are stored encrypted in the database.
"""

import json
import base64
from pathlib import Path
from typing import Dict, Optional
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import logging
import os
from app.db_connection import get_connection, get_cursor

logger = logging.getLogger(__name__)


class SyncConfigManager:
    """Manages sync configuration and encrypted credentials"""

    def __init__(self, db_path: str = None, encryption_key: str = None):
        """
        Initialize config manager

        Args:
            db_path: Path to database (deprecated, kept for backwards compatibility)
            encryption_key: Master encryption key (will be generated if not provided)
        """
        # db_path parameter kept for backwards compatibility but ignored
        # Schema is now managed by schema.py
        self.encryption_key = encryption_key or self._generate_encryption_key()

    def _generate_encryption_key(self) -> str:
        """
        Generate encryption key from system-specific data

        Returns:
            Base64-encoded encryption key
        """
        # Use machine-specific data for key derivation
        # In production, this should be stored securely (e.g., environment variable)
        salt = b'groundtruth_studio_salt_v1'  # Fixed salt for deterministic key

        # Derive key using PBKDF2HMAC
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=100000,
        )

        # Use a combination of system identifiers
        # In production, use a secure secret from environment
        secret = os.environ.get('GROUNDTRUTH_ENCRYPTION_SECRET', 'default-secret-change-me')
        key = base64.urlsafe_b64encode(kdf.derive(secret.encode()))

        return key.decode()

    def _get_cipher(self) -> Fernet:
        """Get Fernet cipher instance"""
        return Fernet(self.encryption_key.encode())

    def _encrypt(self, data: str) -> str:
        """Encrypt data"""
        cipher = self._get_cipher()
        encrypted = cipher.encrypt(data.encode())
        return base64.urlsafe_b64encode(encrypted).decode()

    def _decrypt(self, encrypted_data: str) -> str:
        """Decrypt data"""
        try:
            cipher = self._get_cipher()
            encrypted_bytes = base64.urlsafe_b64decode(encrypted_data.encode())
            decrypted = cipher.decrypt(encrypted_bytes)
            return decrypted.decode()
        except Exception as e:
            logger.error(f"Failed to decrypt data: {e}")
            raise ValueError("Failed to decrypt credentials")


    def set_config(self, key: str, value: str, encrypt: bool = False):
        """
        Set configuration value

        Args:
            key: Configuration key
            value: Configuration value
            encrypt: Whether to encrypt the value
        """
        if encrypt:
            value = self._encrypt(value)

        with get_connection() as conn:
            cursor = conn.cursor()

            cursor.execute('''
                INSERT INTO sync_config (config_key, config_value, is_encrypted)
                VALUES (%s, %s, %s)
                ON CONFLICT(config_key) DO UPDATE SET
                    config_value = excluded.config_value,
                    is_encrypted = excluded.is_encrypted,
                    updated_at = CURRENT_TIMESTAMP
            ''', (key, value, encrypt))

            conn.commit()

    def get_config(self, key: str, default: str = None) -> Optional[str]:
        """
        Get configuration value

        Args:
            key: Configuration key
            default: Default value if not found

        Returns:
            Configuration value or default
        """
        with get_connection() as conn:
            cursor = conn.cursor()

            cursor.execute(
                'SELECT config_value, is_encrypted FROM sync_config WHERE config_key = %s',
                (key,)
            )
            result = cursor.fetchone()

        if not result:
            return default

        value, is_encrypted = result

        if is_encrypted:
            try:
                value = self._decrypt(value)
            except Exception as e:
                logger.error(f"Failed to decrypt config {key}: {e}")
                return default

        return value

    def delete_config(self, key: str):
        """Delete configuration value"""
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM sync_config WHERE config_key = %s', (key,))
            conn.commit()

    def get_all_config_keys(self) -> list:
        """Get all configuration keys"""
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT config_key FROM sync_config ORDER BY config_key')
            keys = [row[0] for row in cursor.fetchall()]
        return keys

    # EcoEye specific methods

    def set_ecoeye_credentials(self, api_key: str, api_secret: str):
        """Set EcoEye API credentials (encrypted)"""
        self.set_config('ecoeye.api_key', api_key, encrypt=True)
        self.set_config('ecoeye.api_secret', api_secret, encrypt=True)

    def get_ecoeye_credentials(self) -> Optional[Dict[str, str]]:
        """Get EcoEye API credentials"""
        api_key = self.get_config('ecoeye.api_key')
        api_secret = self.get_config('ecoeye.api_secret')

        if api_key and api_secret:
            return {
                'api_key': api_key,
                'api_secret': api_secret
            }
        return None

    def has_ecoeye_credentials(self) -> bool:
        """Check if EcoEye credentials are configured"""
        return self.get_ecoeye_credentials() is not None

    # UniFi Protect specific methods

    def set_unifi_credentials(self, host: str, username: str, password: str,
                             port: int = 443, verify_ssl: bool = True):
        """Set UniFi Protect credentials (encrypted)"""
        self.set_config('unifi.host', host, encrypt=False)
        self.set_config('unifi.port', str(port), encrypt=False)
        self.set_config('unifi.username', username, encrypt=True)
        self.set_config('unifi.password', password, encrypt=True)
        self.set_config('unifi.verify_ssl', str(verify_ssl), encrypt=False)

    def get_unifi_credentials(self) -> Optional[Dict]:
        """Get UniFi Protect credentials"""
        host = self.get_config('unifi.host')
        username = self.get_config('unifi.username')
        password = self.get_config('unifi.password')

        if host and username and password:
            return {
                'host': host,
                'port': int(self.get_config('unifi.port', '443')),
                'username': username,
                'password': password,
                'verify_ssl': self.get_config('unifi.verify_ssl', 'True') == 'True'
            }
        return None

    def has_unifi_credentials(self) -> bool:
        """Check if UniFi Protect credentials are configured"""
        return self.get_unifi_credentials() is not None

    # Sync history methods

    def log_sync_start(self, sync_type: str) -> int:
        """
        Log start of sync operation

        Args:
            sync_type: Type of sync (e.g., 'ecoeye_alerts', 'ecoeye_videos')

        Returns:
            Sync history ID
        """
        with get_connection() as conn:
            cursor = conn.cursor()

            cursor.execute('''
                INSERT INTO sync_history (sync_type, status)
                VALUES (%s, 'running')
            ''', (sync_type,))

            sync_id = cursor.lastrowid
            conn.commit()

        return sync_id

    def log_sync_complete(self, sync_id: int, items_processed: int,
                         items_succeeded: int, items_failed: int, details: Dict = None):
        """
        Log completion of sync operation

        Args:
            sync_id: Sync history ID
            items_processed: Total items processed
            items_succeeded: Items that succeeded
            items_failed: Items that failed
            details: Optional details dict
        """
        with get_connection() as conn:
            cursor = conn.cursor()

            details_json = json.dumps(details) if details else None

            cursor.execute('''
                UPDATE sync_history
                SET status = 'completed',
                    completed_at = CURRENT_TIMESTAMP,
                    items_processed = %s,
                    items_succeeded = %s,
                    items_failed = %s,
                    details = %s
                WHERE id = %s
            ''', (items_processed, items_succeeded, items_failed, details_json, sync_id))

            conn.commit()

    def log_sync_error(self, sync_id: int, error_message: str):
        """
        Log error in sync operation

        Args:
            sync_id: Sync history ID
            error_message: Error message
        """
        with get_connection() as conn:
            cursor = conn.cursor()

            cursor.execute('''
                UPDATE sync_history
                SET status = 'failed',
                    completed_at = CURRENT_TIMESTAMP,
                    error_message = %s
                WHERE id = %s
            ''', (error_message, sync_id))

            conn.commit()

    def get_sync_history(self, limit: int = 50) -> list:
        """
        Get sync history

        Args:
            limit: Maximum number of records to return

        Returns:
            List of sync history records
        """
        with get_connection() as conn:
            cursor = conn.cursor()

            cursor.execute('''
                SELECT id, sync_type, status, started_at, completed_at,
                       items_processed, items_succeeded, items_failed,
                       error_message
                FROM sync_history
                ORDER BY started_at DESC
                LIMIT %s
            ''', (limit,))

            columns = ['id', 'sync_type', 'status', 'started_at', 'completed_at',
                      'items_processed', 'items_succeeded', 'items_failed', 'error_message']

            history = []
            for row in cursor.fetchall():
                history.append(dict(zip(columns, row)))

        return history
