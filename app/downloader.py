import os
import subprocess
import json
from typing import Dict, Optional
from pathlib import Path

class VideoDownloader:
    def __init__(self, download_dir='downloads'):
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(exist_ok=True)

        # Try to find yt-dlp in venv first, then system
        venv_yt_dlp = Path(__file__).parent.parent / 'venv' / 'bin' / 'yt-dlp'
        if venv_yt_dlp.exists():
            self.yt_dlp_cmd = str(venv_yt_dlp)
        else:
            self.yt_dlp_cmd = 'yt-dlp'

    def download_video(self, url: str, output_filename: Optional[str] = None) -> Dict:
        """
        Download video from URL using yt-dlp

        Returns dict with:
        - success: bool
        - filename: str (actual saved filename)
        - metadata: dict (title, duration, resolution, etc.)
        - error: str (if failed)
        """
        try:
            if output_filename:
                output_template = str(self.download_dir / output_filename)
            else:
                output_template = str(self.download_dir / '%(title)s_%(id)s.%(ext)s')

            cmd = [
                self.yt_dlp_cmd,
                '--format', 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
                '--merge-output-format', 'mp4',
                '--output', output_template,
                '--print-json',
                '--no-playlist',
                url
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

            if result.returncode != 0:
                return {
                    'success': False,
                    'error': result.stderr or 'Download failed'
                }

            metadata = {}
            for line in result.stdout.strip().split('\n'):
                if line.strip().startswith('{'):
                    try:
                        metadata = json.loads(line)
                        break
                    except json.JSONDecodeError:
                        continue

            if not metadata:
                downloaded_files = list(self.download_dir.glob('*.mp4'))
                if downloaded_files:
                    latest_file = max(downloaded_files, key=lambda p: p.stat().st_mtime)
                    metadata = {'_filename': latest_file.name}

            filename = metadata.get('_filename') or metadata.get('filename', '')
            if not filename:
                potential_files = list(self.download_dir.glob('*.mp4'))
                if potential_files:
                    filename = max(potential_files, key=lambda p: p.stat().st_mtime).name

            return {
                'success': True,
                'filename': filename,
                'metadata': {
                    'title': metadata.get('title', ''),
                    'duration': metadata.get('duration'),
                    'width': metadata.get('width'),
                    'height': metadata.get('height'),
                    'file_size': metadata.get('filesize') or metadata.get('filesize_approx'),
                    'original_url': url,
                    'uploader': metadata.get('uploader'),
                    'upload_date': metadata.get('upload_date'),
                    'description': metadata.get('description')
                }
            }

        except subprocess.TimeoutExpired:
            return {
                'success': False,
                'error': 'Download timeout (10 minutes exceeded)'
            }
        except Exception as e:
            return {
                'success': False,
                'error': f'Download error: {str(e)}'
            }

    def get_video_info(self, url: str) -> Dict:
        """
        Get video information without downloading
        """
        try:
            cmd = [
                'yt-dlp',
                '--dump-json',
                '--no-playlist',
                url
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

            if result.returncode != 0:
                return {
                    'success': False,
                    'error': result.stderr or 'Failed to fetch video info'
                }

            metadata = json.loads(result.stdout)

            return {
                'success': True,
                'info': {
                    'title': metadata.get('title', ''),
                    'duration': metadata.get('duration'),
                    'width': metadata.get('width'),
                    'height': metadata.get('height'),
                    'file_size': metadata.get('filesize') or metadata.get('filesize_approx'),
                    'uploader': metadata.get('uploader'),
                    'thumbnail': metadata.get('thumbnail'),
                    'description': metadata.get('description', '')[:500]
                }
            }

        except subprocess.TimeoutExpired:
            return {
                'success': False,
                'error': 'Request timeout'
            }
        except Exception as e:
            return {
                'success': False,
                'error': f'Error: {str(e)}'
            }

    def check_yt_dlp_installed(self) -> bool:
        """
        Check if yt-dlp is installed
        """
        try:
            subprocess.run([self.yt_dlp_cmd, '--version'], capture_output=True, timeout=5)
            return True
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False
