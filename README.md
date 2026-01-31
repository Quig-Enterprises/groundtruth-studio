# Groundtruth Studio

A professional video annotation system for creating high-quality training datasets for machine learning models. Features a sophisticated multi-type tagging system with 29 tag groups, conditional display logic, and support for complex classification tasks.

Download videos from 1000+ sites, organize with structured tags, and create ML-ready annotations with time-range tags and frame-precise keyframe annotations.

## Features

### Video Management
- **Multi-Source Download**: Download videos from YouTube, Vimeo, and 1000+ other sites using yt-dlp
- **Manual Upload**: Support for direct MP4 file uploads
- **Automatic Thumbnails**: Generate thumbnails automatically for quick preview
- **Metadata Extraction**: Capture duration, resolution, file size, and more
- **Search**: Full-text search across titles, tags, and notes
- **CLI Tool**: Command-line interface for batch downloads

### Advanced Annotation System
- **Time Range Tags**: Mark video periods with behavior labels (supports overlapping tags)
- **Keyframe Annotations**: Frame-precise annotation with bounding boxes
- **Auto-Suggest Tags**: Intelligent tag suggestions from existing annotations
- **Activity + Moment Tagging**: Hierarchical tagging (e.g., activity: power_loading, moment: motor_starts)
- **Real-Time Annotation**: Add/close tags while watching video
- **Bounding Box Drawing**: Click-and-drag interface for spatial annotation
- **Comment Support**: Add contextual notes to any annotation

### Web Interface
- **Video Library**: Clean, modern UI for browsing your archive
- **Professional Annotation UI**: Split-screen video player with annotation panel
- **Playback Controls**: Frame-accurate seeking (±1s, ±5s buttons)
- **Tag Management**: Organize videos with custom tags and categories

## Technology Stack

- **Backend**: Python 3.8+ with Flask
- **Database**: SQLite (easily upgradeable to PostgreSQL)
- **Video Download**: yt-dlp
- **Video Processing**: FFmpeg
- **Frontend**: Vanilla JavaScript, no framework dependencies
- **Storage**: File-based with organized directory structure

## Installation

### Prerequisites

```bash
# Install system dependencies (Ubuntu/Debian)
sudo apt update
sudo apt install python3 python3-pip ffmpeg

# Install yt-dlp
pip install yt-dlp

# Or on Ubuntu 22.04+:
sudo apt install yt-dlp
```

### Setup

```bash
# Clone or navigate to project directory
cd /var/www/html/groundtruth-studio

# Install Python dependencies
pip install -r requirements.txt

# Make CLI tool executable
chmod +x download_cli.py

# Verify dependencies
python3 -c "from app.downloader import VideoDownloader; from app.video_utils import VideoProcessor; d = VideoDownloader(); p = VideoProcessor(); print('yt-dlp:', d.check_yt_dlp_installed()); print('FFmpeg:', p.check_ffmpeg_installed())"
```

## Usage

### Web Interface

Start the Flask server:

```bash
cd /var/www/html/groundtruth-studio
python3 app/api.py
```

Access the web interface at: `http://localhost:5000`

**Main Interface:**
- **Download Tab**: Paste video URL, preview info, and download
- **Upload Tab**: Manually upload MP4 files
- **Search**: Search videos by title, tags, or notes
- **Tags Panel**: View all tags with video counts, click to filter
- **Video Cards**: Click any video to view details or click "Annotate Video" button

**Annotation Interface** (`/annotate?id=VIDEO_ID`):

Click "Annotate Video" on any video card to open the professional annotation interface.

**Time Range Tags:**
1. Navigate to start of behavior
2. Click "+ Tag" button
3. Enter tag name (auto-suggest from existing tags)
4. Optionally set end time, or leave open
5. Click "Close at Current Time" button later to finish tag
6. Add comments for context

**Keyframe Annotations:**
1. Navigate to target frame
2. Click "+ Keyframe" button
3. Draw bounding box on video by clicking and dragging
4. Add Activity Tag (e.g., `power_loading`)
5. Add Moment Tag (e.g., `motor_starts`, `spray_visible`)
6. Optionally add comment
7. Save annotation

See [ANNOTATION_GUIDE.md](ANNOTATION_GUIDE.md) for complete annotation workflow documentation.

### CLI Tool

Download single video:

```bash
./download_cli.py "https://www.youtube.com/watch?v=VIDEO_ID"
```

Download with tags:

```bash
./download_cli.py "https://www.youtube.com/watch?v=VIDEO_ID" \
    --tags "dog" "running" "outdoor" \
    --notes "Training sample for running behavior detection"
```

Skip thumbnail generation:

```bash
./download_cli.py "URL" --no-thumbnail
```

## API Reference

### GET `/api/videos`

Get all videos or search results.

**Query Parameters:**
- `search` (optional): Search query
- `limit` (optional): Results per page (default: 100)
- `offset` (optional): Pagination offset (default: 0)

**Response:**
```json
{
  "success": true,
  "videos": [
    {
      "id": 1,
      "filename": "video.mp4",
      "title": "Sample Video",
      "duration": 120.5,
      "width": 1920,
      "height": 1080,
      "file_size": 52428800,
      "thumbnail_path": "thumbnails/video.jpg",
      "upload_date": "2026-01-19 10:30:00",
      "tags": "dog, running, outdoor"
    }
  ]
}
```

### GET `/api/videos/<id>`

Get single video with behavior annotations.

### POST `/api/download`

Download video from URL.

**Request:**
```json
{
  "url": "https://www.youtube.com/watch?v=VIDEO_ID"
}
```

### POST `/api/upload`

Upload video file (multipart/form-data).

**Form Fields:**
- `file`: Video file
- `title` (optional): Custom title
- `notes` (optional): Notes

### POST `/api/videos/<id>/tags`

Add tag to video.

**Request:**
```json
{
  "tag": "running"
}
```

### DELETE `/api/videos/<id>/tags/<tag_name>`

Remove tag from video.

### GET `/api/tags`

Get all tags with video counts.

### POST `/api/videos/<id>/behaviors`

Add behavior annotation (legacy).

**Request:**
```json
{
  "behavior_type": "running",
  "start_time": 5.2,
  "end_time": 12.8,
  "confidence": 0.95,
  "notes": "Dog running in park"
}
```

### GET `/api/videos/<id>/time-range-tags`

Get all time-range tags for a video.

### POST `/api/videos/<id>/time-range-tags`

Add time-range tag.

**Request:**
```json
{
  "tag_name": "power_loading",
  "start_time": 0.0,
  "end_time": 45.5,
  "comment": "Full operation cycle"
}
```

### PUT `/api/time-range-tags/<tag_id>`

Update time-range tag (close tag, update comment).

**Request:**
```json
{
  "end_time": 45.5,
  "comment": "Updated comment"
}
```

### DELETE `/api/time-range-tags/<tag_id>`

Delete time-range tag.

### GET `/api/tag-suggestions`

Get all unique tag names for auto-suggest.

### GET `/api/videos/<id>/keyframe-annotations`

Get all keyframe annotations for a video.

### POST `/api/videos/<id>/keyframe-annotations`

Add keyframe annotation with bounding box.

**Request:**
```json
{
  "timestamp": 8.2,
  "bbox_x": 150,
  "bbox_y": 200,
  "bbox_width": 300,
  "bbox_height": 250,
  "activity_tag": "power_loading",
  "moment_tag": "motor_starts",
  "comment": "Motor startup sequence"
}
```

### DELETE `/api/keyframe-annotations/<annotation_id>`

Delete keyframe annotation.

### GET `/api/activity-tags`

Get all unique activity tags for auto-suggest.

### GET `/api/moment-tags`

Get all unique moment tags for auto-suggest.

## Database Schema

### Videos Table

- `id`: Primary key
- `filename`: Unique filename
- `original_url`: Source URL (if downloaded)
- `title`: Display title
- `duration`: Video duration in seconds
- `width`, `height`: Resolution
- `file_size`: File size in bytes
- `thumbnail_path`: Path to thumbnail image
- `upload_date`: Timestamp
- `notes`: User notes

### Tags Table

- `id`: Primary key
- `name`: Tag name (unique)
- `category`: Optional category
- `created_date`: Timestamp

### Video_Tags Table

- `video_id`, `tag_id`: Composite primary key
- `added_date`: Timestamp

### Behaviors Table (Legacy)

- `id`: Primary key
- `video_id`: Foreign key to videos
- `behavior_type`: Type of behavior
- `start_time`, `end_time`: Time range in seconds
- `confidence`: Confidence score (0-1)
- `notes`: Annotation notes
- `annotated_date`: Timestamp

### Time_Range_Tags Table

- `id`: Primary key
- `video_id`: Foreign key to videos
- `tag_name`: Behavior/activity label
- `start_time`: Start timestamp in seconds
- `end_time`: End timestamp in seconds (nullable for open tags)
- `comment`: Optional contextual notes
- `created_date`: Timestamp

### Keyframe_Annotations Table

- `id`: Primary key
- `video_id`: Foreign key to videos
- `timestamp`: Frame timestamp in seconds
- `bbox_x`, `bbox_y`: Bounding box top-left coordinates
- `bbox_width`, `bbox_height`: Bounding box dimensions
- `activity_tag`: High-level activity label (e.g., "power_loading")
- `moment_tag`: Specific event label (e.g., "motor_starts")
- `comment`: Optional contextual notes
- `created_date`: Timestamp

## Directory Structure

```
groundtruth-studio/
├── app/
│   ├── api.py              # Flask API server
│   ├── database.py         # Database models and queries
│   ├── downloader.py       # Video download functionality
│   └── video_utils.py      # Video processing utilities
├── downloads/              # Downloaded/uploaded videos
├── thumbnails/             # Generated thumbnails
├── static/
│   ├── css/
│   │   ├── style.css       # Main UI styles
│   │   └── annotate.css    # Annotation interface styles
│   └── js/
│       ├── app.js          # Main library interface
│       └── annotate.js     # Annotation interface
├── templates/
│   ├── index.html          # Main library interface
│   └── annotate.html       # Annotation interface
├── download_cli.py         # Command-line tool
├── batch_download_example.py  # Batch processing script
├── requirements.txt        # Python dependencies
├── video_archive.db        # SQLite database
├── README.md
└── ANNOTATION_GUIDE.md     # Complete annotation documentation
```

## Use Cases for AI Training

### Behavior Recognition Training

1. Download diverse video samples of target behaviors
2. Tag videos with behavior types (running, jumping, eating, etc.)
3. Use the behaviors table to annotate specific time ranges
4. Export dataset with timestamps for frame extraction

### Dataset Organization

```bash
# Download dog videos with automatic tagging
./download_cli.py "URL1" --tags "dog" "running" "outdoor"
./download_cli.py "URL2" --tags "dog" "sitting" "indoor"
./download_cli.py "URL3" --tags "dog" "jumping" "outdoor"

# Search and filter via web UI
# Tag additional attributes (breed, lighting, camera angle)
# Export metadata for training pipeline
```

### Quality Control

- Preview all videos in web interface
- Verify thumbnails for quick quality checks
- Add notes about video issues or特性
- Tag videos by quality level (high, medium, low)

## Advanced Configuration

### Production Deployment

For production use with larger datasets:

1. **Use PostgreSQL**: Update `database.py` to use PostgreSQL instead of SQLite
2. **Add authentication**: Implement user auth in Flask
3. **Use reverse proxy**: Deploy behind nginx or Apache
4. **Enable HTTPS**: Use SSL certificates
5. **Storage**: Mount large storage volume for videos

### Performance Optimization

- Enable Flask caching for video listings
- Use CDN for thumbnail delivery
- Implement pagination for large datasets
- Add database indexes for common queries

### Batch Processing

Create batch download script:

```python
#!/usr/bin/env python3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent / 'app'))

from database import VideoDatabase
from downloader import VideoDownloader
from video_utils import VideoProcessor

urls = [
    "https://www.youtube.com/watch?v=...",
    "https://vimeo.com/...",
]

db = VideoDatabase('video_archive.db')
downloader = VideoDownloader('downloads')
processor = VideoProcessor('thumbnails')

for url in urls:
    print(f"Processing: {url}")
    result = downloader.download_video(url)
    if result['success']:
        # Add to database with metadata
        print(f"Success: {result['filename']}")
    else:
        print(f"Failed: {result['error']}")
```

## Troubleshooting

### yt-dlp errors

Update yt-dlp to latest version:
```bash
pip install --upgrade yt-dlp
```

### FFmpeg not found

Install FFmpeg:
```bash
# Ubuntu/Debian
sudo apt install ffmpeg

# macOS
brew install ffmpeg
```

### Large file uploads failing

Increase Flask max upload size in `app/api.py`:
```python
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024 * 1024  # 5GB
```

### Database locked errors

SQLite has concurrency limitations. For multi-user access:
1. Add write-ahead logging: `PRAGMA journal_mode=WAL`
2. Or migrate to PostgreSQL for production

## Future Enhancements

- [ ] Frame extraction for ML training
- [ ] Integration with ML annotation tools (CVAT, Label Studio)
- [ ] Export to common dataset formats (COCO, YOLO)
- [ ] Video preprocessing (cropping, resizing)
- [ ] Duplicate detection
- [ ] Batch tag editing
- [ ] User authentication and permissions
- [ ] API rate limiting
- [ ] Webhook notifications for downloads
- [ ] Cloud storage integration (S3, GCS)

## License

This is a custom-built tool for AI training dataset management. Modify as needed for your use case.

## Support

For issues or questions:
1. Check system status indicator in web UI
2. Verify FFmpeg and yt-dlp installation
3. Check Flask logs for errors
4. Review database integrity
