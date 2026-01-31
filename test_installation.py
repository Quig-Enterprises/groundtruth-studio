#!/usr/bin/env python3
"""
Installation test script - verifies all dependencies are installed correctly
"""
import sys
from pathlib import Path

print("Video Archive System - Installation Test")
print("=" * 60)

errors = []
warnings = []

# Test Python version
print("\n1. Checking Python version...")
if sys.version_info >= (3, 8):
    print(f"   ✓ Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
else:
    errors.append(f"Python 3.8+ required (found {sys.version_info.major}.{sys.version_info.minor})")
    print(f"   ❌ Python version too old")

# Test Flask
print("\n2. Checking Flask...")
try:
    import flask
    print(f"   ✓ Flask {flask.__version__}")
except ImportError:
    errors.append("Flask not installed - run: pip install Flask")
    print("   ❌ Flask not found")

# Test yt-dlp
print("\n3. Checking yt-dlp...")
sys.path.insert(0, str(Path(__file__).parent / 'app'))
try:
    from downloader import VideoDownloader
    downloader = VideoDownloader()
    if downloader.check_yt_dlp_installed():
        print("   ✓ yt-dlp installed")
    else:
        warnings.append("yt-dlp not installed - video downloads will not work")
        print("   ⚠ yt-dlp not found (install with: pip install yt-dlp)")
except Exception as e:
    errors.append(f"Error checking yt-dlp: {e}")
    print(f"   ❌ Error: {e}")

# Test FFmpeg
print("\n4. Checking FFmpeg...")
try:
    from video_utils import VideoProcessor
    processor = VideoProcessor()
    if processor.check_ffmpeg_installed():
        print("   ✓ FFmpeg installed")
    else:
        warnings.append("FFmpeg not installed - thumbnails will not work")
        print("   ⚠ FFmpeg not found (install with: sudo apt install ffmpeg)")
except Exception as e:
    errors.append(f"Error checking FFmpeg: {e}")
    print(f"   ❌ Error: {e}")

# Test directory structure
print("\n5. Checking directory structure...")
required_dirs = ['app', 'downloads', 'thumbnails', 'static', 'templates']
base_dir = Path(__file__).parent

for dir_name in required_dirs:
    dir_path = base_dir / dir_name
    if dir_path.exists():
        print(f"   ✓ {dir_name}/")
    else:
        dir_path.mkdir(exist_ok=True)
        print(f"   ✓ {dir_name}/ (created)")

# Test database initialization
print("\n6. Testing database initialization...")
try:
    from database import VideoDatabase
    db = VideoDatabase(':memory:')
    print("   ✓ Database module working")
except Exception as e:
    errors.append(f"Database error: {e}")
    print(f"   ❌ Error: {e}")

# Summary
print("\n" + "=" * 60)
if errors:
    print("❌ INSTALLATION INCOMPLETE")
    print("\nErrors:")
    for error in errors:
        print(f"  - {error}")
else:
    print("✓ INSTALLATION SUCCESSFUL")

if warnings:
    print("\nWarnings:")
    for warning in warnings:
        print(f"  - {warning}")

if not errors:
    print("\nYou can start the system with:")
    print("  ./start.sh")
    print("\nOr manually:")
    print("  python3 app/api.py")
    print("\nAccess web interface at: http://localhost:5000")

print("=" * 60)

sys.exit(0 if not errors else 1)
