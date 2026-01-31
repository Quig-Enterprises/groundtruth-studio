import subprocess
import os
import json
from pathlib import Path
from typing import Dict, Optional

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
