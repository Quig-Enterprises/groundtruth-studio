#!/usr/bin/env python3
"""
CLI tool for downloading videos and adding them to the archive
"""
import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / 'app'))

from database import VideoDatabase
from downloader import VideoDownloader
from video_utils import VideoProcessor

def main():
    parser = argparse.ArgumentParser(description='Download videos and add to archive')
    parser.add_argument('url', help='Video URL to download')
    parser.add_argument('-t', '--tags', nargs='+', help='Tags to add to the video')
    parser.add_argument('-n', '--notes', help='Notes about the video')
    parser.add_argument('--no-thumbnail', action='store_true', help='Skip thumbnail generation')

    args = parser.parse_args()

    base_dir = Path(__file__).parent
    db = VideoDatabase(str(base_dir / 'video_archive.db'))
    downloader = VideoDownloader(str(base_dir / 'downloads'))
    processor = VideoProcessor(str(base_dir / 'thumbnails'))

    print(f"Downloading video from: {args.url}")

    result = downloader.download_video(args.url)

    if not result['success']:
        print(f"Error: {result['error']}", file=sys.stderr)
        sys.exit(1)

    print(f"Downloaded: {result['filename']}")

    video_path = base_dir / 'downloads' / result['filename']

    print("Extracting metadata...")
    metadata_result = processor.get_video_metadata(str(video_path))

    if metadata_result['success']:
        metadata = metadata_result['metadata']
    else:
        metadata = result.get('metadata', {})
        print(f"Warning: Could not extract metadata: {metadata_result.get('error')}")

    thumbnail_path = None
    if not args.no_thumbnail:
        print("Generating thumbnail...")
        thumb_result = processor.extract_thumbnail(str(video_path))
        if thumb_result['success']:
            thumbnail_path = thumb_result['thumbnail_path']
            print(f"Thumbnail saved: {thumbnail_path}")
        else:
            print(f"Warning: Could not generate thumbnail: {thumb_result.get('error')}")

    print("Adding to database...")
    video_id = db.add_video(
        filename=result['filename'],
        original_url=args.url,
        title=result['metadata'].get('title', result['filename']),
        duration=metadata.get('duration'),
        width=metadata.get('width'),
        height=metadata.get('height'),
        file_size=metadata.get('file_size'),
        thumbnail_path=thumbnail_path,
        notes=args.notes
    )

    print(f"Video added with ID: {video_id}")

    if args.tags:
        print("Adding tags...")
        for tag in args.tags:
            db.tag_video(video_id, tag)
            print(f"  - {tag}")

    print("\nSuccess!")
    print(f"Title: {result['metadata'].get('title', result['filename'])}")
    print(f"Duration: {metadata.get('duration', 'Unknown')} seconds")
    print(f"Resolution: {metadata.get('width')}x{metadata.get('height')}")

if __name__ == '__main__':
    main()
