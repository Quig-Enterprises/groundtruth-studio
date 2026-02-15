import subprocess
import os
import json
from pathlib import Path
from typing import Dict, Optional
import requests

class VideoProcessor:
    def __init__(self, thumbnail_dir='thumbnails'):
        self.thumbnail_dir = Path(thumbnail_dir)
        self.thumbnail_dir.mkdir(exist_ok=True)

    def extract_thumbnail(self, video_path: str, output_path: Optional[str] = None,
                         timestamp: str = '00:00:01') -> Dict:
        """
        Extract thumbnail from video using FFmpeg

        Args:
            video_path: Path to video file
            output_path: Optional custom output path
            timestamp: Timestamp for thumbnail (default 1 second)

        Returns:
            Dict with success status and thumbnail path
        """
        try:
            video_file = Path(video_path)
            if not video_file.exists():
                return {'success': False, 'error': 'Video file not found'}

            if output_path:
                thumb_path = Path(output_path)
            else:
                thumb_name = video_file.stem + '.jpg'
                thumb_path = self.thumbnail_dir / thumb_name

            cmd = [
                'ffmpeg',
                '-i', str(video_file),
                '-ss', timestamp,
                '-vframes', '1',
                '-q:v', '2',
                '-y',
                str(thumb_path)
            ]

            result = subprocess.run(cmd, capture_output=True, timeout=30)

            if result.returncode != 0:
                return {
                    'success': False,
                    'error': 'Thumbnail extraction failed'
                }

            return {
                'success': True,
                'thumbnail_path': str(thumb_path)
            }

        except subprocess.TimeoutExpired:
            return {'success': False, 'error': 'Thumbnail extraction timeout'}
        except Exception as e:
            return {'success': False, 'error': f'Error: {str(e)}'}

    def get_video_metadata(self, video_path: str) -> Dict:
        """
        Extract video metadata using FFprobe

        Returns:
            Dict with duration, width, height, file_size, codec info
        """
        try:
            video_file = Path(video_path)
            if not video_file.exists():
                return {'success': False, 'error': 'Video file not found'}

            cmd = [
                'ffprobe',
                '-v', 'quiet',
                '-print_format', 'json',
                '-show_format',
                '-show_streams',
                str(video_file)
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

            if result.returncode != 0:
                return {'success': False, 'error': 'Failed to extract metadata'}

            data = json.loads(result.stdout)

            video_stream = None
            for stream in data.get('streams', []):
                if stream.get('codec_type') == 'video':
                    video_stream = stream
                    break

            format_info = data.get('format', {})

            return {
                'success': True,
                'metadata': {
                    'duration': float(format_info.get('duration', 0)),
                    'file_size': int(format_info.get('size', 0)),
                    'width': video_stream.get('width') if video_stream else None,
                    'height': video_stream.get('height') if video_stream else None,
                    'codec': video_stream.get('codec_name') if video_stream else None,
                    'bit_rate': int(format_info.get('bit_rate', 0)),
                    'format_name': format_info.get('format_name', '')
                }
            }

        except subprocess.TimeoutExpired:
            return {'success': False, 'error': 'Metadata extraction timeout'}
        except Exception as e:
            return {'success': False, 'error': f'Error: {str(e)}'}

    def extract_clip(self, video_path: str, timestamp: float,
                     duration: float = 5.0, output_path: Optional[str] = None) -> Dict:
        """
        Extract a short video clip around a timestamp using FFmpeg.

        Args:
            video_path: Path to source video file
            timestamp: Center timestamp in seconds
            duration: Clip duration in seconds (default 5.0)
            output_path: Optional custom output path

        Returns:
            Dict with success status and clip_path
        """
        try:
            video_file = Path(video_path)
            if not video_file.exists():
                return {'success': False, 'error': 'Video file not found'}

            # Skip non-video files (Frigate snapshots are .jpg)
            if video_file.suffix.lower() in ('.jpg', '.jpeg', '.png', '.bmp', '.webp'):
                return {'success': False, 'error': 'Source is an image, not a video'}

            # Calculate start time (center clip around timestamp)
            half = duration / 2.0
            start = max(0, timestamp - half)

            if output_path:
                clip_path = Path(output_path)
            else:
                clips_dir = Path('/opt/groundtruth-studio/clips')
                clips_dir.mkdir(exist_ok=True)
                clip_name = f"{video_file.stem}_t{int(timestamp*10)}.mp4"
                clip_path = clips_dir / clip_name

            # Return cached clip if it exists
            if clip_path.exists() and clip_path.stat().st_size > 0:
                return {'success': True, 'clip_path': str(clip_path)}

            # Use stream copy for speed when possible, re-encode for precision
            cmd = [
                'ffmpeg',
                '-ss', f'{start:.2f}',
                '-i', str(video_file),
                '-t', f'{duration:.2f}',
                '-c:v', 'libx264',
                '-preset', 'ultrafast',
                '-crf', '23',
                '-an',              # No audio needed for review
                '-movflags', '+faststart',  # Web-friendly MP4
                '-y',
                str(clip_path)
            ]

            result = subprocess.run(cmd, capture_output=True, timeout=30)

            if result.returncode != 0:
                stderr = result.stderr.decode('utf-8', errors='replace')[-200:]
                return {'success': False, 'error': f'Clip extraction failed: {stderr}'}

            if not clip_path.exists() or clip_path.stat().st_size == 0:
                return {'success': False, 'error': 'Clip file was not created'}

            return {'success': True, 'clip_path': str(clip_path)}

        except subprocess.TimeoutExpired:
            return {'success': False, 'error': 'Clip extraction timeout'}
        except Exception as e:
            return {'success': False, 'error': f'Error: {str(e)}'}

    def fetch_frigate_clip(self, frigate_url: str, event_id: str, camera: str,
                           duration: float = 5.0,
                           clips_dir: str = '/opt/groundtruth-studio/clips') -> Dict:
        """
        Fetch a video clip from Frigate's recording API for a specific event.

        Args:
            frigate_url: Base URL of the Frigate instance
            event_id: Full Frigate event UUID
            camera: Camera name
            duration: Desired clip duration (unused, Frigate returns event-scoped clip)
            clips_dir: Directory to cache downloaded clips

        Returns:
            Dict with success status and clip_path
        """
        try:
            clips_path = Path(clips_dir)
            clips_path.mkdir(exist_ok=True)

            # Cache key based on event_id
            clip_name = f"frigate_{event_id}.mp4"
            clip_path = clips_path / clip_name

            # Return cached clip if already fetched
            if clip_path.exists() and clip_path.stat().st_size > 0:
                return {'success': True, 'clip_path': str(clip_path)}

            # Fetch event clip from Frigate API
            url = f"{frigate_url.rstrip('/')}/api/events/{event_id}/clip.mp4"
            resp = requests.get(url, timeout=30, stream=True)

            if resp.status_code != 200:
                return {'success': False, 'error': f'Frigate clip API returned {resp.status_code}'}

            # Stream to file
            with open(clip_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)

            if clip_path.stat().st_size == 0:
                clip_path.unlink(missing_ok=True)
                return {'success': False, 'error': 'Frigate returned empty clip'}

            return {'success': True, 'clip_path': str(clip_path)}

        except requests.Timeout:
            return {'success': False, 'error': 'Frigate clip request timed out'}
        except Exception as e:
            return {'success': False, 'error': f'Failed to fetch Frigate clip: {str(e)}'}

    def cleanup_clips(self, max_age_days: int = 7, max_size_mb: int = 500,
                      clips_dir: str = '/opt/groundtruth-studio/clips') -> Dict:
        """
        Clean up cached video clips based on age and total size.

        Args:
            max_age_days: Remove clips older than this many days
            max_size_mb: Maximum total size of clips directory in MB

        Returns:
            Dict with cleanup stats
        """
        try:
            clips_path = Path(clips_dir)
            if not clips_path.exists():
                return {'success': True, 'removed': 0, 'freed_mb': 0}

            import time
            now = time.time()
            max_age_secs = max_age_days * 86400
            max_size_bytes = max_size_mb * 1024 * 1024

            removed = 0
            freed = 0

            # Phase 1: Remove clips older than max_age_days
            clips = sorted(clips_path.glob('*.mp4'), key=lambda f: f.stat().st_mtime)
            for clip in clips:
                try:
                    age = now - clip.stat().st_mtime
                    if age > max_age_secs:
                        size = clip.stat().st_size
                        clip.unlink()
                        removed += 1
                        freed += size
                except OSError:
                    continue

            # Phase 2: If still over max size, remove oldest first
            remaining = sorted(clips_path.glob('*.mp4'), key=lambda f: f.stat().st_mtime)
            total_size = sum(f.stat().st_size for f in remaining)

            while total_size > max_size_bytes and remaining:
                oldest = remaining.pop(0)
                try:
                    size = oldest.stat().st_size
                    oldest.unlink()
                    removed += 1
                    freed += size
                    total_size -= size
                except OSError:
                    continue

            return {
                'success': True,
                'removed': removed,
                'freed_mb': round(freed / (1024 * 1024), 1),
                'remaining_mb': round(total_size / (1024 * 1024), 1),
                'remaining_count': len(list(clips_path.glob('*.mp4')))
            }
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def check_ffmpeg_installed(self) -> bool:
        """
        Check if FFmpeg is installed
        """
        try:
            subprocess.run(['ffmpeg', '-version'], capture_output=True, timeout=5)
            subprocess.run(['ffprobe', '-version'], capture_output=True, timeout=5)
            return True
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False
