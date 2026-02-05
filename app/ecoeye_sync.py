"""
EcoEye Alerts Integration Module

Provides synchronization with alert.ecoeyetech.com for:
1. Syncing alert data from EcoEye Alert Relay
2. Downloading videos already retrieved from UniFi Protect

EcoEye Alert Relay API Integration:
- Base URL: https://alert.ecoeyetech.com (no /api/v1 prefix)
- Authentication: Currently no auth required (IP whitelist will be added later)
- Endpoints:
  - GET /health - Health check
  - GET /events?limit=N&status=completed - List events
  - GET /events/{event_id} - Get single event with video_path
  - GET /videos/{path} - Download video file

Field Mapping:
- event_id → alert_id
- event_type → alert_type
- status == 'completed' → video_available
- video_path → used to construct download_url
- confidence: not available, defaults to 1.0

Security features:
- HTTPS-only connections
- Rate limiting
- Error handling and retry logic
"""

import requests
import time
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
import logging
from db_connection import get_connection, get_cursor

logger = logging.getLogger(__name__)


class EcoEyeSyncClient:
    """Client for syncing with alert.ecoeyetech.com (EcoEye Alert Relay)"""

    def __init__(self, db_path: str, download_dir: Path, api_key: str = None, api_secret: str = None):
        """
        Initialize EcoEye sync client

        Args:
            db_path: Path to video database (kept for backwards compatibility, ignored)
            download_dir: Directory for downloaded videos
            api_key: API key for authentication (optional, reserved for future use)
            api_secret: API secret for request signing (optional, reserved for future use)

        Note: Authentication is currently not required (no auth on Alert Relay API).
              IP whitelist will be added later.
              Database connection now uses PostgreSQL via db_connection module.
        """
        # db_path kept for backwards compatibility but not used
        self.download_dir = Path(download_dir)
        self.base_url = "https://alert.ecoeyetech.com"  # No /api/v1 prefix
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
        Set API credentials (reserved for future use)

        Note: Currently not required as API doesn't use authentication yet
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
        Make request to EcoEye Alert Relay API

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

        # Note: No authentication headers needed currently
        # Future enhancement: Add X-API-Key when IP whitelist is not sufficient

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
                    timeout=30
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
            response = self._make_request('GET', '/health')
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
        Get alerts (events) from EcoEye Alert Relay API

        Args:
            start_time: Filter alerts from this time (client-side filter)
            end_time: Filter alerts until this time (client-side filter)
            limit: Maximum number of alerts to return
            offset: Pagination offset (not supported by API yet, client-side)

        Returns:
            Dict with alerts list and metadata

        Note: API doesn't support time filtering yet, so we fetch completed events
              and filter client-side. Offset is also implemented client-side.
        """
        params = {
            'limit': limit,
            'status': 'completed'  # Only fetch events with completed video processing
        }

        try:
            response = self._make_request('GET', '/events', params=params)
            data = response.json()

            # Extract events array from response
            events = data.get('events', [])

            # Convert events to alerts format (field mapping)
            alerts = []
            for event in events:
                # Parse timestamp for filtering
                event_timestamp = None
                if 'timestamp' in event:
                    try:
                        event_timestamp = datetime.fromisoformat(event['timestamp'].replace('Z', '+00:00'))
                    except Exception:
                        pass

                # Apply time filtering (client-side since API doesn't support it)
                if start_time and event_timestamp and event_timestamp < start_time:
                    continue
                if end_time and event_timestamp and event_timestamp > end_time:
                    continue

                # Map fields from event to alert format
                alert = {
                    'id': event.get('event_id'),  # event_id → alert_id
                    'camera_id': event.get('camera_id'),
                    'timestamp': event.get('timestamp'),
                    'type': event.get('event_type'),  # event_type → alert_type
                    'confidence': 1.0,  # Not available in API, default to 1.0
                    'video_available': event.get('status') == 'completed',  # completed = video available
                    'video_id': event.get('event_id'),  # Use event_id as video_id
                    'video_path': event.get('video_path')  # Store for download URL construction
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
        Get video information for a specific alert (event)

        Args:
            alert_id: Alert ID (event_id)

        Returns:
            Dict with video metadata or None if not available
        """
        try:
            response = self._make_request('GET', f'/events/{alert_id}')
            data = response.json()

            # Check if video is available (status == completed and has video_path)
            video_available = data.get('status') == 'completed' and data.get('video_path')

            if video_available:
                video_path = data.get('video_path')

                # Construct download URL from video_path
                # video_path format: /home/brandon/web/alert.ecoeyetech.com/videos/Front_Door/2025-01-29/14-30-45_person.mp4
                # Extract everything after /videos/
                download_url = None
                if video_path and '/videos/' in video_path:
                    relative_path = video_path.split('/videos/')[-1]
                    download_url = f"{self.base_url}/videos/{relative_path}"

                return {
                    'success': True,
                    'alert_id': alert_id,
                    'video_id': alert_id,
                    'camera_id': data.get('camera_id'),
                    'timestamp': data.get('timestamp'),
                    'duration': data.get('duration'),  # May not be available
                    'file_size': data.get('file_size'),  # May not be available
                    'download_url': download_url,
                    'video_path': video_path
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
        Download video for an alert from EcoEye Alert Relay

        Args:
            alert_id: Alert ID (event_id)
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

            download_url = video_info.get('download_url')
            if not download_url:
                return False, "No download URL provided"

            # Generate filename from video path or construct from metadata
            video_path = video_info.get('video_path', '')
            if video_path:
                # Use original filename from path
                filename = Path(video_path).name
            else:
                # Fallback: construct filename
                camera_id = video_info.get('camera_id', 'unknown')
                timestamp = video_info.get('timestamp', datetime.now().isoformat())
                filename = f"ecoeye_{alert_id}_{camera_id}_{timestamp.replace(':', '-')}.mp4"

            filepath = self.download_dir / filename

            # Download video with streaming
            logger.info(f"Downloading video for alert {alert_id} from {download_url}")

            # Note: For video downloads, we use a direct HTTP request to the video URL
            # The download_url is already a full URL: https://alert.ecoeyetech.com/videos/...
            response = self.session.get(download_url, stream=True, timeout=300)
            response.raise_for_status()

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

            logger.info(f"Successfully downloaded video to {filepath} ({downloaded} bytes)")
            return True, str(filepath)

        except Exception as e:
            logger.error(f"Failed to download video for alert {alert_id}: {e}")
            return False, str(e)

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
                        # Extract fields with new mapping
                        alert_id = alert.get('id')  # event_id
                        camera_id = alert.get('camera_id')
                        timestamp = alert.get('timestamp')
                        alert_type = alert.get('type')  # event_type
                        confidence = alert.get('confidence', 1.0)  # Default 1.0 since not in API
                        video_available = alert.get('video_available', False)  # status == completed
                        video_id = alert.get('video_id')  # event_id
                        video_path = alert.get('video_path')  # Store for download URL
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
