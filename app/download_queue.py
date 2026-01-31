"""
Asynchronous video download queue manager
Handles queuing, duplicate detection, and background downloading
"""

import threading
import queue
import time
from typing import Dict, Optional
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from pathlib import Path
from downloader import VideoDownloader
from video_utils import VideoProcessor
from database import VideoDatabase

class DownloadQueue:
    def __init__(self, download_dir: Path, thumbnail_dir: Path, db: VideoDatabase):
        self.download_dir = download_dir
        self.thumbnail_dir = thumbnail_dir
        self.db = db
        self.downloader = VideoDownloader(download_dir)
        self.processor = VideoProcessor(thumbnail_dir)

        self.queue = queue.Queue()
        self.queued_items = []  # List to track queued items in order
        self.active_downloads = {}  # {url: status}
        self.completed_downloads = {}  # {normalized_url: video_id}
        self.lock = threading.Lock()

        # Start worker thread
        self.worker_thread = threading.Thread(target=self._worker, daemon=True)
        self.worker_thread.start()

    def normalize_url(self, url: str) -> str:
        """
        Normalize URL by removing tracking parameters and standardizing format
        """
        parsed = urlparse(url)

        # Remove common tracking parameters
        tracking_params = {
            'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
            'fbclid', 'gclid', 'msclkid', '_ga', 'mc_cid', 'mc_eid',
            'ref', 'referrer', 'source', 'campaign'
        }

        # Parse query parameters
        query_params = parse_qs(parsed.query)

        # Filter out tracking parameters
        clean_params = {k: v for k, v in query_params.items() if k not in tracking_params}

        # For YouTube, keep only essential params (v for video ID)
        if 'youtube.com' in parsed.netloc or 'youtu.be' in parsed.netloc:
            if 'v' in clean_params:
                clean_params = {'v': clean_params['v']}
            # Handle youtu.be shortened URLs
            if 'youtu.be' in parsed.netloc:
                # Extract video ID from path
                video_id = parsed.path.lstrip('/')
                return f'https://www.youtube.com/watch?v={video_id}'

        # Rebuild query string
        clean_query = urlencode(clean_params, doseq=True) if clean_params else ''

        # Rebuild URL
        normalized = urlunparse((
            parsed.scheme or 'https',
            parsed.netloc,
            parsed.path,
            '',  # params
            clean_query,
            ''   # fragment
        ))

        return normalized.lower()

    def check_duplicate(self, url: str) -> Optional[int]:
        """
        Check if video already exists in database
        Returns video_id if duplicate found, None otherwise
        """
        normalized_url = self.normalize_url(url)

        # Check in-memory cache first
        if normalized_url in self.completed_downloads:
            return self.completed_downloads[normalized_url]

        # Check database
        videos = self.db.get_all_videos(limit=10000)
        for video in videos:
            if video.get('original_url'):
                existing_normalized = self.normalize_url(video['original_url'])
                if existing_normalized == normalized_url:
                    # Cache for future lookups
                    self.completed_downloads[normalized_url] = video['id']
                    return video['id']

        return None

    def add_to_queue(self, url: str) -> Dict:
        """
        Add video URL to download queue
        Returns status dict with queue position or duplicate info
        """
        normalized_url = self.normalize_url(url)

        # Check for duplicates
        existing_video_id = self.check_duplicate(url)
        if existing_video_id:
            return {
                'success': False,
                'duplicate': True,
                'video_id': existing_video_id,
                'message': 'Video already downloaded'
            }

        # Check if already in queue or downloading
        with self.lock:
            if normalized_url in self.active_downloads:
                status = self.active_downloads[normalized_url]
                return {
                    'success': False,
                    'in_progress': True,
                    'status': status,
                    'message': f'Already {status}'
                }

            # Add to queue
            item = {
                'url': url,
                'normalized_url': normalized_url,
                'queued_at': time.time()
            }
            self.active_downloads[normalized_url] = 'queued'
            self.queued_items.append(item)
            self.queue.put(item)

            queue_position = self.queue.qsize()

        return {
            'success': True,
            'queued': True,
            'queue_position': queue_position,
            'message': f'Added to download queue (position {queue_position})'
        }

    def _worker(self):
        """
        Background worker that processes download queue
        """
        while True:
            try:
                # Get next item from queue (blocking)
                item = self.queue.get()
                url = item['url']
                normalized_url = item['normalized_url']

                # Update status and remove from queued list
                with self.lock:
                    self.active_downloads[normalized_url] = 'downloading'
                    # Remove from queued items list
                    self.queued_items = [i for i in self.queued_items if i['normalized_url'] != normalized_url]

                # Download video
                result = self.downloader.download_video(url)

                if result['success']:
                    # Process video
                    video_path = self.download_dir / result['filename']

                    # Get metadata
                    metadata_result = self.processor.get_video_metadata(str(video_path))
                    if metadata_result['success']:
                        metadata = metadata_result['metadata']
                    else:
                        metadata = result.get('metadata', {})

                    # Extract thumbnail
                    thumb_result = self.processor.extract_thumbnail(str(video_path))
                    thumbnail_path = thumb_result.get('thumbnail_path') if thumb_result['success'] else None

                    # Add to database
                    video_id = self.db.add_video(
                        filename=result['filename'],
                        original_url=url,
                        title=result['metadata'].get('title', result['filename']),
                        duration=metadata.get('duration'),
                        width=metadata.get('width'),
                        height=metadata.get('height'),
                        file_size=metadata.get('file_size'),
                        thumbnail_path=thumbnail_path
                    )

                    # Update cache
                    with self.lock:
                        self.completed_downloads[normalized_url] = video_id
                        del self.active_downloads[normalized_url]

                    print(f'[Download Queue] Successfully downloaded: {url} -> video_id={video_id}')
                else:
                    # Download failed
                    with self.lock:
                        del self.active_downloads[normalized_url]
                    print(f'[Download Queue] Failed to download: {url} - {result.get("error")}')

                # Mark task as done
                self.queue.task_done()

            except Exception as e:
                print(f'[Download Queue] Worker error: {e}')
                if 'normalized_url' in locals():
                    with self.lock:
                        if normalized_url in self.active_downloads:
                            del self.active_downloads[normalized_url]
                self.queue.task_done()

    def get_queue_status(self) -> Dict:
        """
        Get current queue status
        """
        with self.lock:
            return {
                'queue_size': self.queue.qsize(),
                'queued_items': list(self.queued_items),  # Return copy of list
                'active_downloads': dict(self.active_downloads),
                'completed_count': len(self.completed_downloads)
            }
