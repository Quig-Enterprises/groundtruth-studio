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
- **Database**: PostgreSQL
- **Video Download**: yt-dlp
- **Video Processing**: FFmpeg
- **Frontend**: Vanilla JavaScript, no framework dependencies
- **Storage**: File-based with organized directory structure

## Pipeline Worker (MQTT Intelligence Bridge)

The pipeline worker is a standalone service that bridges real-time MQTT events from the detection pipeline into Groundtruth Studio's intelligence layer.

### Architecture

```
tracker/tracks/{camera_id}  ──┐
identity/face/{camera_id}   ──┼──► Pipeline Worker ──► Context Engine (associations)
                               │                   ──► Violation Detector
                               │                   ──► Visit Builder (periodic)
                               └──────────────────────► Face Clustering (periodic)
```

### MQTT Subscriptions

| Topic | Source | Purpose |
|-------|--------|---------|
| `tracker/tracks/+` | Tracker Service | Track positions per frame — fed to context engine and violation detector |
| `identity/face/+` | InsightFace API | Face detection events for identity linkage |

### Periodic Jobs

| Job | Interval | Description |
|-----|----------|-------------|
| Visit aggregation | 60s | Groups tracks into visit records via `VisitBuilder.build_visits()` |
| Face clustering | 300s | Clusters unassigned face embeddings via `FaceClusterer.run_clustering()` |

### Configuration

Edit `app/pipeline_config.yml`:

```yaml
mqtt:
  host: "127.0.0.1"
  port: 1883
  username: "pipeline"
  password: "pipeline_worker_2026"
  client_id: "pipeline-worker"
  subscriptions:
    - "tracker/tracks/+"
    - "identity/face/+"

periodic:
  visit_interval_seconds: 60
  clustering_interval_seconds: 300

violation:
  ramp_cameras: []    # Empty = check all cameras
```

### Service Management

```bash
# Start / stop / restart
sudo systemctl start pipeline-worker
sudo systemctl stop pipeline-worker
sudo systemctl restart pipeline-worker

# Check status and logs
systemctl is-active pipeline-worker
tail -f /opt/groundtruth-studio/logs/pipeline-worker.log
```

The service is managed by systemd (`/etc/systemd/system/pipeline-worker.service`), starts on boot, and auto-restarts on failure.

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

1. **Database**: Uses PostgreSQL via db_connection.py module
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

## Local LAN Training Queue

The training pipeline supports a local mode that replaces AWS (S3/SQS) with HTTP-based job queuing and data transfer. Any machine on the LAN can act as a training worker by polling the Studio API.

### How It Works

```
Submit job (UI/API) → DB status='queued'
                           ↓
Worker polls GET /api/training/jobs/next → claims job (status='processing')
                           ↓
Worker downloads GET /api/training/jobs/{id}/download → tar.gz of export data
                           ↓
Worker runs training locally (YOLO, vibration, location, custom)
                           ↓
Worker reports POST /api/training/jobs/{id}/complete with metrics
```

Local mode activates automatically when AWS credentials are not configured. The Studio server at `192.168.50.20:5050` becomes the queue coordinator.

### Worker API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/training/jobs/next` | GET | Atomically claim next queued job (race-condition safe) |
| `/api/training/jobs/<id>/download` | GET | Download export data as tar.gz |
| `/api/training/jobs/<id>/complete` | POST | Report completion with metrics |
| `/api/training/jobs/<id>/fail` | POST | Report failure with error message |
| `/api/training/queue-status` | GET | Queue depth (DB-based in local mode) |

### Running a Local Training Worker

The local worker requires only Python 3 and `requests`. No AWS SDK needed.

```bash
# On the Studio server itself
python3 /opt/groundtruth-studio/worker/training_worker_local.py

# On another LAN machine (point to Studio)
python3 training_worker_local.py --studio-url http://192.168.50.20:5050

# Custom data directory and poll interval
python3 training_worker_local.py --data-dir /data/training-jobs --poll-interval 60

# Process one job and exit (for testing or cron)
python3 training_worker_local.py --once
```

**Environment variables** (alternative to CLI flags):

| Variable | Default | Description |
|----------|---------|-------------|
| `STUDIO_URL` | `http://192.168.50.20:5050` | Studio API base URL |
| `DATA_DIR` | `/tmp/training-jobs` | Where to download and extract training data |
| `POLL_INTERVAL` | `30` | Seconds between job polls |
| `TRAINING_COMMANDS` | (built-in) | JSON override for training command templates |

### Supported Job Types

| Job Type | Command Template |
|----------|-----------------|
| `yolo-training` | `yolo train data={data_dir}/data.yaml model={model_type} epochs={epochs} imgsz=640` |
| `bearing-fault` | `python3 train_bearing_fault.py --train {train_file} --val {val_file} ...` |
| `vibration` | Same as bearing-fault |
| `location` | `python3 -m torchvision.models --data {data_dir} ...` |
| `custom` | `echo "Custom job {job_id}: data at {data_dir}"` |

### Multiple Workers

Multiple workers can run simultaneously on different machines. The `/api/training/jobs/next` endpoint uses `FOR UPDATE SKIP LOCKED` to ensure each job is claimed by exactly one worker. Workers are stateless and can be started/stopped freely.

### Verifying Local Mode

```bash
# Check that local mode is active
curl http://192.168.50.20:5050/api/training/queue-status
# Should return: {"local_mode": true, "queue_messages": N, ...}

# Submit a test job
curl -X POST http://192.168.50.20:5050/api/training/submit \
  -H 'Content-Type: application/json' \
  -d '{"export_path": "/path/to/export", "job_type": "yolo-training", "config": {"epochs": 10}}'
# Should return: {"status": "queued", ...}  (not "uploading")
```

### Files

| File | Description |
|------|-------------|
| `app/training_queue.py` | Queue client with local/AWS mode detection |
| `app/api.py` | REST API including worker endpoints |
| `worker/training_worker_local.py` | HTTP-based local worker (no AWS dependency) |
| `worker/training_worker.py` | Original SQS/S3-based worker (requires AWS) |

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

### Database connection errors

PostgreSQL handles concurrency natively and supports multiple simultaneous connections. If you encounter connection errors, check that the DATABASE_URL environment variable is set correctly and the PostgreSQL service is running.

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
