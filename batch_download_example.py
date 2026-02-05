#!/usr/bin/env python3
"""
Example batch download script for processing multiple videos

Usage:
    1. Create a CSV file with columns: url, tags (comma-separated), notes
    2. Run: ./batch_download_example.py videos.csv

Or modify the urls_data list below and run directly.
"""
import sys
import csv
from pathlib import Path
import time

sys.path.insert(0, str(Path(__file__).parent / 'app'))

from database import VideoDatabase
from downloader import VideoDownloader
from video_utils import VideoProcessor

def process_video(url, tags=None, notes=None, db=None, downloader=None, processor=None):
    """Process a single video"""
    print(f"\n{'='*60}")
    print(f"Processing: {url}")
    print(f"{'='*60}")

    result = downloader.download_video(url)

    if not result['success']:
        print(f"❌ Download failed: {result['error']}")
        return False

    print(f"✓ Downloaded: {result['filename']}")

    video_path = Path(__file__).parent / 'downloads' / result['filename']

    print("Extracting metadata...")
    metadata_result = processor.get_video_metadata(str(video_path))

    if metadata_result['success']:
        metadata = metadata_result['metadata']
        print(f"✓ Duration: {metadata.get('duration', 'Unknown')} seconds")
        print(f"✓ Resolution: {metadata.get('width')}x{metadata.get('height')}")
    else:
        metadata = result.get('metadata', {})
        print(f"⚠ Could not extract metadata: {metadata_result.get('error')}")

    print("Generating thumbnail...")
    thumb_result = processor.extract_thumbnail(str(video_path))
    thumbnail_path = None
    if thumb_result['success']:
        thumbnail_path = thumb_result['thumbnail_path']
        print(f"✓ Thumbnail saved")
    else:
        print(f"⚠ Could not generate thumbnail: {thumb_result.get('error')}")

    print("Adding to database...")
    video_id = db.add_video(
        filename=result['filename'],
        original_url=url,
        title=result['metadata'].get('title', result['filename']),
        duration=metadata.get('duration'),
        width=metadata.get('width'),
        height=metadata.get('height'),
        file_size=metadata.get('file_size'),
        thumbnail_path=thumbnail_path,
        notes=notes
    )

    print(f"✓ Video ID: {video_id}")

    if tags:
        tag_list = [t.strip() for t in tags.split(',')] if isinstance(tags, str) else tags
        print(f"Adding {len(tag_list)} tags...")
        for tag in tag_list:
            db.tag_video(video_id, tag)
            print(f"  ✓ {tag}")

    print(f"✓ Success! Video '{result['metadata'].get('title', result['filename'])}' added to archive")
    return True

def batch_download_from_csv(csv_file):
    """Download videos from CSV file"""
    base_dir = Path(__file__).parent
    db = VideoDatabase()
    downloader = VideoDownloader(str(base_dir / 'downloads'))
    processor = VideoProcessor(str(base_dir / 'thumbnails'))

    success_count = 0
    fail_count = 0

    with open(csv_file, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            url = row.get('url', '').strip()
            if not url:
                continue

            tags = row.get('tags', '').strip()
            notes = row.get('notes', '').strip()

            success = process_video(url, tags, notes, db, downloader, processor)
            if success:
                success_count += 1
            else:
                fail_count += 1

            time.sleep(2)

    print(f"\n{'='*60}")
    print(f"Batch download complete!")
    print(f"Success: {success_count}")
    print(f"Failed: {fail_count}")
    print(f"{'='*60}")

def batch_download_from_list():
    """Download videos from hardcoded list"""

    urls_data = [
        {
            'url': 'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
            'tags': 'example, test, music',
            'notes': 'Example video for testing'
        },
    ]

    base_dir = Path(__file__).parent
    db = VideoDatabase()
    downloader = VideoDownloader(str(base_dir / 'downloads'))
    processor = VideoProcessor(str(base_dir / 'thumbnails'))

    success_count = 0
    fail_count = 0

    for item in urls_data:
        success = process_video(
            item['url'],
            item.get('tags'),
            item.get('notes'),
            db, downloader, processor
        )
        if success:
            success_count += 1
        else:
            fail_count += 1

        time.sleep(2)

    print(f"\n{'='*60}")
    print(f"Batch download complete!")
    print(f"Success: {success_count}")
    print(f"Failed: {fail_count}")
    print(f"{'='*60}")

if __name__ == '__main__':
    if len(sys.argv) > 1:
        csv_file = sys.argv[1]
        if not Path(csv_file).exists():
            print(f"Error: File '{csv_file}' not found")
            sys.exit(1)
        batch_download_from_csv(csv_file)
    else:
        print("No CSV file provided. Using hardcoded example list.")
        print("Modify urls_data in this script or provide a CSV file:")
        print("  ./batch_download_example.py videos.csv")
        print()
        batch_download_from_list()
