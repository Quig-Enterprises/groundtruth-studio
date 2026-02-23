"""
EcoEye Alerts Integration Module

Provides synchronization with alert.ecoeyetech.com (EcoEye Alert Relay) for:
1. Syncing alert data from EcoEye Alert Relay API v1
2. Downloading videos via authenticated API

EcoEye Alert Relay API:
- Base URL: https://alert.ecoeyetech.com
- Authentication: HMAC-SHA256 request signing (X-API-Key, X-Signature, X-Timestamp)
- Endpoints:
  - GET /api/health - Health/status check
  - GET /api/events - List events with filtering
  - GET /api/events/{event_id} - Get event details and video info
  - POST /api/events/{event_id}/retry - Trigger video download from UniFi
  - GET /videos/{path} - Download video file (served via relay)

Field Mapping (API → local):
- event_id → alert_id
- event_type → alert_type
- confidence → confidence
- event_id → video_id
- status (completed) → video_available
- video_path → local download path construction

Security features:
- HMAC-SHA256 request signing
- HTTPS-only connections
- Rate limiting (100 req/min, 10 req/sec burst)
- 5-minute timestamp window for replay protection
"""

import requests
import time
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
import logging
import hmac
import hashlib
from db_connection import get_connection, get_cursor

logger = logging.getLogger(__name__)


class EcoEyeSyncClient:
    """Client for syncing with alert.ecoeyetech.com (EcoEye Alert Relay)"""

    def __init__(self, download_dir: Path, api_key: str = None, api_secret: str = None, base_url: str = None):
        """
        Initialize EcoEye sync client

        Args:
            download_dir: Directory for downloaded videos
            api_key: API key for HMAC authentication
            api_secret: API secret for HMAC-SHA256 request signing
            base_url: Base URL for EcoEye Alert Relay API

        Note: API uses HMAC-SHA256 signing for authentication.
              Database connection uses PostgreSQL via db_connection module.
        """
        self.download_dir = Path(download_dir)
        self.base_url = base_url or "https://alert.ecoeyetech.com"
        self.api_key = api_key
        self.api_secret = api_secret
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'GroundtruthStudio/1.0',
            'Accept': 'application/json'
        })

        # Rate limiting
        self.last_request_time = 0
        self.min_request_interval = 0.5  # 500ms between requests

        # Create downloads directory if needed
        self.download_dir.mkdir(parents=True, exist_ok=True)

    def set_credentials(self, api_key: str, api_secret: str):
        """
        Set API credentials for HMAC-SHA256 request signing

        Args:
            api_key: API key (sent in X-API-Key header)
            api_secret: API secret for generating HMAC signatures
        """
        self.api_key = api_key
        self.api_secret = api_secret

    def _rate_limit(self):
        """Enforce rate limiting between requests"""
        elapsed = time.time() - self.last_request_time
        if elapsed < self.min_request_interval:
            time.sleep(self.min_request_interval - elapsed)
        self.last_request_time = time.time()

    def _make_request(self, method: str, endpoint: str, params: Dict = None,
                     json_data: Dict = None, stream: bool = False) -> requests.Response:
        """
        Make request to EcoEye Alert Relay API with HMAC-SHA256 signing

        Args:
            method: HTTP method
            endpoint: API endpoint
            params: Query parameters
            json_data: JSON body
            stream: Whether to stream response (for large downloads)

        Returns:
            Response object

        Raises:
            requests.exceptions.RequestException: On request failure
        """
        self._rate_limit()

        url = f"{self.base_url}{endpoint}"

        # Build authentication headers
        auth_headers = {
            'X-API-Key': self.api_key or '',
            'X-Timestamp': str(int(time.time()))
        }

        # HMAC-SHA256 signing if secret is available
        if self.api_secret:
            canonical = f"{method.upper()}\n{endpoint}\n"
            if params:
                param_str = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
                canonical += param_str + "\n"
            else:
                canonical += "\n"
            if json_data:
                body_json = json.dumps(json_data, sort_keys=True)
                body_hash = hashlib.sha256(body_json.encode()).hexdigest()
                canonical += body_hash

            signature = hmac.new(
                self.api_secret.encode(),
                canonical.encode(),
                hashlib.sha256
            ).hexdigest()
            auth_headers['X-Signature'] = signature

        self.session.headers.update(auth_headers)

        # Make request with retries
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = self.session.request(
                    method=method,
                    url=url,
                    params=params,
                    json=json_data,
                    stream=stream,
                    timeout=(10, 300) if stream else 30
                )
                response.raise_for_status()
                return response

            except requests.exceptions.RequestException as e:
                if attempt == max_retries - 1:
                    logger.error(f"Request failed after {max_retries} attempts: {e}")
                    raise

                # Exponential backoff
                wait_time = 2 ** attempt
                logger.warning(f"Request failed (attempt {attempt + 1}/{max_retries}), "
                             f"retrying in {wait_time}s: {e}")
                time.sleep(wait_time)

    def test_connection(self) -> Dict:
        """
        Test connection to EcoEye Alert Relay API

        Returns:
            Dict with connection status and server info
        """
        try:
            response = self._make_request('GET', '/api/health')
            data = response.json()

            return {
                'success': True,
                'status': 'connected',
                'server_time': data.get('timestamp'),
                'api_version': data.get('version', 'unknown'),
                'message': 'Successfully connected to alert.ecoeyetech.com'
            }

        except Exception as e:
            return {
                'success': False,
                'status': 'error',
                'error': str(e),
                'message': f'Failed to connect: {e}'
            }

    def get_alerts(self, start_time: datetime = None, end_time: datetime = None,
                   limit: int = 100, offset: int = 0) -> Dict:
        """
        Get alerts from EcoEye Alert Relay API

        Args:
            start_time: Filter alerts from this time (client-side filter)
            end_time: Filter alerts until this time (client-side filter)
            limit: Maximum number of alerts to return
            offset: Pagination offset (client-side)

        Returns:
            Dict with alerts list and metadata
        """
        params = {
            'limit': limit,
            'offset': offset
        }

        try:
            response = self._make_request('GET', '/api/events', params=params)
            data = response.json()

            # Extract events array from response
            events = data.get('events', [])

            # Convert events to alerts format (field mapping for deployed API)
            alerts = []
            for event in events:
                # Parse timestamp for filtering
                event_timestamp = None
                if 'timestamp' in event:
                    try:
                        event_timestamp = datetime.fromisoformat(event['timestamp'].replace('Z', '+00:00'))
                    except Exception:
                        pass

                # Apply time filtering (client-side)
                # Ensure both sides are timezone-aware or both naive for comparison
                if start_time and event_timestamp:
                    st = start_time.replace(tzinfo=event_timestamp.tzinfo) if start_time.tzinfo is None and event_timestamp.tzinfo else start_time
                    if event_timestamp < st:
                        continue
                if end_time and event_timestamp:
                    et = end_time.replace(tzinfo=event_timestamp.tzinfo) if end_time.tzinfo is None and event_timestamp.tzinfo else end_time
                    if event_timestamp > et:
                        continue

                # Map fields from API response (with fallbacks for old/new field names)
                alert = {
                    'id': event.get('event_id', event.get('id')),
                    'camera_id': event.get('camera_id'),
                    'timestamp': event.get('timestamp'),
                    'type': event.get('event_type', event.get('type')),
                    'confidence': event.get('confidence', 1.0),
                    'video_available': event.get('status') == 'completed' or event.get('video_available', False),
                    'video_id': event.get('event_id', event.get('id')),
                    'video_path': event.get('video_path'),
                    'metadata': event.get('metadata')
                }
                alerts.append(alert)

            # Apply client-side offset
            total_before_offset = len(alerts)
            alerts = alerts[offset:]

            # Apply limit
            has_more = len(alerts) > limit
            alerts = alerts[:limit]

            return {
                'success': True,
                'alerts': alerts,
                'total': total_before_offset,
                'limit': limit,
                'offset': offset,
                'has_more': has_more
            }

        except Exception as e:
            logger.error(f"Failed to get alerts: {e}")
            return {
                'success': False,
                'error': str(e),
                'alerts': []
            }

    def get_alert_video(self, alert_id: str) -> Optional[Dict]:
        """
        Get video information for a specific alert from API

        Args:
            alert_id: Alert ID

        Returns:
            Dict with video metadata or None if not available
        """
        try:
            response = self._make_request('GET', f'/api/events/{alert_id}')
            data = response.json()

            video_available = data.get('status') == 'completed' or data.get('video_available', False)

            if video_available:
                video_path = data.get('video_path', '')
                return {
                    'success': True,
                    'alert_id': alert_id,
                    'video_id': data.get('event_id', alert_id),
                    'camera_id': data.get('camera_id'),
                    'timestamp': data.get('timestamp'),
                    'duration': data.get('duration'),
                    'file_size': data.get('file_size'),
                    'video_path': video_path,
                    'download_url': f"{self.base_url}{data.get('download_url')}" if data.get('download_url') else None
                }
            else:
                return {
                    'success': False,
                    'alert_id': alert_id,
                    'error': 'Video not available or not yet processed'
                }

        except Exception as e:
            logger.error(f"Failed to get video for alert {alert_id}: {e}")
            return None

    def download_alert_video(self, alert_id: str, video_info: Dict = None) -> Tuple[bool, str]:
        """
        Download video for an alert from EcoEye Alert Relay API

        Args:
            alert_id: Alert ID
            video_info: Optional video info (if already fetched)

        Returns:
            Tuple of (success, file_path or error_message)
        """
        try:
            # Get video info if not provided
            if not video_info:
                video_info = self.get_alert_video(alert_id)
                if not video_info or not video_info.get('success'):
                    return False, "Video not available for this alert"

            # Generate filename from metadata
            camera_id = video_info.get('camera_id', 'unknown')
            timestamp = video_info.get('timestamp', datetime.now().isoformat())
            filename = f"ecoeye_{alert_id}_{camera_id}_{timestamp.replace(':', '-')}.mp4"

            filepath = self.download_dir / filename

            # First, trigger download from UniFi via retry endpoint
            logger.info(f"Requesting video download for alert {alert_id}")
            try:
                self._make_request('POST', f'/api/events/{alert_id}/retry')
            except Exception as e:
                logger.warning(f"Retry request failed (may already be downloaded): {e}")

            # Construct download URL from video_path
            video_path = video_info.get('video_path')
            if not video_path:
                return False, "No video path available"

            # Extract relative path from video_path
            if '/videos/' in video_path:
                relative_path = video_path.split('/videos/')[-1]
                download_url = f"{self.base_url}/videos/{relative_path}"
            else:
                return False, f"Invalid video path format: {video_path}"

            # Download video with streaming
            logger.info(f"Downloading video from {download_url}")

            response = self._make_request('GET', f'/videos/{relative_path}', stream=True)

            # Stream to file
            total_size = int(response.headers.get('content-length', 0))
            downloaded = 0

            with open(filepath, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)

                        # Log progress for large files
                        if total_size > 0 and downloaded % (1024 * 1024) == 0:
                            progress = (downloaded / total_size) * 100
                            logger.debug(f"Download progress: {progress:.1f}%")

            # Verify download integrity
            if total_size > 0 and downloaded != total_size:
                logger.error(
                    f"Download truncated for alert {alert_id}: "
                    f"expected {total_size} bytes, got {downloaded} bytes"
                )
                filepath.unlink(missing_ok=True)
                return False, f"Download truncated: {downloaded}/{total_size} bytes"

            logger.info(f"Successfully downloaded video to {filepath} ({downloaded} bytes)")
            return True, str(filepath)

        except Exception as e:
            logger.error(f"Failed to download video for alert {alert_id}: {e}")
            return False, str(e)

    def acknowledge_download(self, alert_id: str) -> bool:
        """
        Acknowledge successful video download to relay for retention management

        Args:
            alert_id: The alert/event ID to acknowledge

        Returns:
            True if acknowledged successfully, False otherwise
        """
        try:
            resp = self._make_request('POST', f'/api/events/{alert_id}/acknowledge')
            logger.info(f"Acknowledged download for {alert_id}: {resp.json()}")
            return True
        except Exception as e:
            # Non-fatal - log warning but don't fail the download
            logger.warning(f"Failed to acknowledge download for {alert_id}: {e}")
            return False

    def sync_alerts_to_database(self, start_time: datetime = None,
                                end_time: datetime = None) -> Dict:
        """
        Sync alerts (events) from EcoEye Alert Relay to local database

        Args:
            start_time: Sync alerts from this time (client-side filter)
            end_time: Sync alerts until this time (client-side filter)

        Returns:
            Dict with sync results
        """
        if not start_time:
            # Default: last 24 hours
            start_time = datetime.now() - timedelta(days=1)

        try:
            with get_connection() as conn:
                cursor = conn.cursor()

                # Fetch alerts from API
                offset = 0
                limit = 100
                total_synced = 0
                new_alerts = 0
                updated_alerts = 0

                while True:
                    result = self.get_alerts(start_time, end_time, limit, offset)

                    if not result['success']:
                        break

                    alerts = result['alerts']
                    if not alerts:
                        break

                    for alert in alerts:
                        # Extract fields from API response
                        alert_id = alert.get('id')
                        camera_id = alert.get('camera_id')
                        timestamp = alert.get('timestamp')
                        alert_type = alert.get('type')
                        confidence = alert.get('confidence', 1.0)
                        video_available = alert.get('video_available', False)
                        video_id = alert.get('video_id')
                        video_path = alert.get('video_path')
                        metadata = json.dumps(alert)

                        # Check if alert exists
                        cursor.execute('SELECT id FROM ecoeye_alerts WHERE alert_id = %s', (alert_id,))
                        existing = cursor.fetchone()

                        if existing:
                            # Update existing alert
                            cursor.execute('''
                                UPDATE ecoeye_alerts
                                SET camera_id = %s, timestamp = %s, alert_type = %s,
                                    confidence = %s, video_available = %s, video_id = %s,
                                    video_path = %s, metadata = %s, synced_at = CURRENT_TIMESTAMP
                                WHERE alert_id = %s
                            ''', (camera_id, timestamp, alert_type, confidence,
                                 video_available, video_id, video_path, metadata, alert_id))
                            updated_alerts += 1
                        else:
                            # Insert new alert
                            cursor.execute('''
                                INSERT INTO ecoeye_alerts
                                (alert_id, camera_id, timestamp, alert_type, confidence,
                                 video_available, video_id, video_path, metadata)
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ''', (alert_id, camera_id, timestamp, alert_type, confidence,
                                 video_available, video_id, video_path, metadata))
                            new_alerts += 1

                        total_synced += 1

                    # Check if more alerts to fetch
                    if not result.get('has_more'):
                        break

                    offset += limit

                # Second pass: sync completed events with video (may not be in recent pages)
                logger.info("Syncing completed events with video...")
                try:
                    response = self._make_request('GET', '/api/events', params={
                        'status': 'completed',
                        'limit': 100,
                        'sort': 'desc'
                    })
                    data = response.json()
                    completed_events = data.get('events', [])

                    for event in completed_events:
                        alert_id = event.get('event_id', event.get('id'))
                        video_path = event.get('video_path')
                        video_available = bool(video_path)
                        camera_id = event.get('camera_id')
                        timestamp = event.get('timestamp')
                        alert_type = event.get('event_type', event.get('type'))
                        confidence = event.get('confidence', 1.0)
                        video_id = event.get('event_id', event.get('id'))
                        metadata = json.dumps(event)

                        cursor.execute('SELECT id, video_available FROM ecoeye_alerts WHERE alert_id = %s', (alert_id,))
                        existing = cursor.fetchone()

                        if existing:
                            cursor.execute('''
                                UPDATE ecoeye_alerts
                                SET video_available = %s, video_path = %s, video_id = %s,
                                    metadata = %s, synced_at = CURRENT_TIMESTAMP
                                WHERE alert_id = %s
                            ''', (video_available, video_path, video_id, metadata, alert_id))
                            updated_alerts += 1
                        else:
                            cursor.execute('''
                                INSERT INTO ecoeye_alerts
                                (alert_id, camera_id, timestamp, alert_type, confidence,
                                 video_available, video_id, video_path, metadata)
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ''', (alert_id, camera_id, timestamp, alert_type, confidence,
                                 video_available, video_id, video_path, metadata))
                            new_alerts += 1
                        total_synced += 1

                    logger.info(f"Synced {len(completed_events)} completed events")
                except Exception as e:
                    logger.warning(f"Failed to sync completed events: {e}")

                conn.commit()

            return {
                'success': True,
                'total_synced': total_synced,
                'new_alerts': new_alerts,
                'updated_alerts': updated_alerts,
                'start_time': start_time.isoformat(),
                'end_time': end_time.isoformat() if end_time else 'now'
            }

        except Exception as e:
            logger.error(f"Failed to sync alerts: {e}")
            return {
                'success': False,
                'error': str(e)
            }

    def download_pending_videos(self, limit: int = 10) -> Dict:
        """
        Download videos for alerts that have videos available but not yet downloaded

        Args:
            limit: Maximum number of videos to download

        Returns:
            Dict with download results
        """
        try:
            with get_connection() as conn:
                cursor = conn.cursor()

                # Find alerts with videos not yet downloaded
                cursor.execute('''
                    SELECT alert_id, camera_id, timestamp
                    FROM ecoeye_alerts
                    WHERE video_available = true
                      AND video_downloaded = false
                    ORDER BY timestamp DESC
                    LIMIT %s
                ''', (limit,))

                pending = cursor.fetchall()

                downloaded = 0
                failed = 0
                results = []

                for alert_id, camera_id, timestamp in pending:
                    logger.info(f"Downloading video for alert {alert_id}")

                    success, result = self.download_alert_video(alert_id)

                    if success:
                        # Update database
                        cursor.execute('''
                            UPDATE ecoeye_alerts
                            SET video_downloaded = true, local_video_path = %s
                            WHERE alert_id = %s
                        ''', (result, alert_id))
                        conn.commit()

                        downloaded += 1

                        # Acknowledge download to relay for retention management
                        self.acknowledge_download(alert_id)

                        # Create videos record + trigger detection pipeline
                        try:
                            from services import db as _db, processor as _proc
                            from vehicle_detect_runner import trigger_vehicle_detect as _tvd
                            from clip_tracker import run_clip_tracking
                            import threading

                            vid_id = _db.add_video(
                                filename=Path(result).name,
                                original_url=f'ecoeye://{alert_id}',
                                title=f'{camera_id or "unknown"} - ecoeye alert',
                                camera_id=camera_id,
                            )

                            thumb = _proc.extract_thumbnail(result)
                            if thumb.get('success'):
                                _db.update_video(vid_id, thumbnail_path=thumb['thumbnail_path'])
                                _tvd(vid_id, thumb['thumbnail_path'])

                            # Clip tracking
                            if vid_id:
                                threading.Thread(
                                    target=run_clip_tracking,
                                    args=(vid_id, camera_id or ''),
                                    kwargs={'clip_path': result},
                                    daemon=True,
                                    name=f"clip-track-ecoeye-{alert_id}"
                                ).start()
                        except Exception as proc_err:
                            logger.warning(f"Post-processing failed for EcoEye {alert_id}: {proc_err}")

                        results.append({
                            'alert_id': alert_id,
                            'status': 'success',
                            'path': result
                        })
                    else:
                        failed += 1
                        results.append({
                            'alert_id': alert_id,
                            'status': 'failed',
                            'error': result
                        })

            return {
                'success': True,
                'total_pending': len(pending),
                'downloaded': downloaded,
                'failed': failed,
                'results': results
            }

        except Exception as e:
            logger.error(f"Failed to download pending videos: {e}")
            return {
                'success': False,
                'error': str(e)
            }

    def get_sync_status(self) -> Dict:
        """
        Get current sync status

        Returns:
            Dict with status information
        """
        try:
            with get_connection() as conn:
                cursor = conn.cursor()

                # Total alerts
                cursor.execute('SELECT COUNT(*) FROM ecoeye_alerts')
                total_alerts = cursor.fetchone()[0]

                # Alerts with videos
                cursor.execute('SELECT COUNT(*) FROM ecoeye_alerts WHERE video_available = true')
                alerts_with_video = cursor.fetchone()[0]

                # Downloaded videos
                cursor.execute('SELECT COUNT(*) FROM ecoeye_alerts WHERE video_downloaded = true')
                videos_downloaded = cursor.fetchone()[0]

                # Pending downloads
                pending_downloads = alerts_with_video - videos_downloaded

                # Last sync time
                cursor.execute('SELECT MAX(synced_at) FROM ecoeye_alerts')
                last_sync = cursor.fetchone()[0]

            return {
                'success': True,
                'total_alerts': total_alerts,
                'alerts_with_video': alerts_with_video,
                'videos_downloaded': videos_downloaded,
                'pending_downloads': pending_downloads,
                'last_sync': last_sync
            }

        except Exception as e:
            logger.error(f"Failed to get sync status: {e}")
            return {
                'success': False,
                'error': str(e)
            }
