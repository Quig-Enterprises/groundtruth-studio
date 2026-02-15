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
