# GroundTruth Studio - AI Development Context

> Video annotation system for ML training datasets with EcoEye and UniFi Protect integration.

## Quick Reference

| Item | Value |
|------|-------|
| **Location** | `/opt/groundtruth-studio` |
| **URL** | `https://studio.ecoeyetech.com` |
| **Port** | 5050 (Flask behind nginx) |
| **Service** | `sudo systemctl restart groundtruth-studio` |
| **Logs** | `tail -f /opt/groundtruth-studio/flask.log` or `server.log` |
| **Database** | `video_archive.db` (SQLite) |

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         nginx (443)                              │
│                    studio.ecoeyetech.com                         │
└─────────────────────┬───────────────────────────────────────────┘
                      │ proxy_pass
                      ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Flask App (port 5050)                         │
│                       app/api.py                                 │
├─────────────────────────────────────────────────────────────────┤
│  Routes:                                                         │
│  /              → Video library (index.html)                     │
│  /annotate      → Annotation interface                           │
│  /camera-topology → Camera relationship mapping                  │
│  /ecoeye-preview  → EcoEye alert video browser                   │
│  /person-manager  → Person/face management                       │
│  /training-queue  → ML training job queue                        │
│  /yolo-export     → YOLO dataset export                          │
│  /sync-settings   → EcoEye sync configuration                    │
│  /api/*           → REST API endpoints                           │
└─────────────────────────────────────────────────────────────────┘
                      │
        ┌─────────────┼─────────────┐
        ▼             ▼             ▼
┌───────────┐  ┌───────────┐  ┌───────────┐
│  SQLite   │  │  EcoEye   │  │  UniFi    │
│video_archive│ │   API     │  │ Protect   │
│    .db    │  │alert.eco..│  │  NVR      │
└───────────┘  └───────────┘  └───────────┘
```

## Key Files

### Backend (`app/`)

| File | Size | Purpose |
|------|------|---------|
| `api.py` | 79KB | **Main Flask app** - all routes and API endpoints |
| `database.py` | 58KB | SQLite models, queries for videos/annotations/tags |
| `ecoeye_sync.py` | 23KB | Sync videos from EcoEye alert system |
| `yolo_exporter.py` | 16KB | Export annotations to YOLO format for training |
| `unifi_protect_client.py` | 9KB | Download clips from UniFi Protect NVR |
| `training_queue.py` | 11KB | Queue and manage ML training jobs |
| `camera_topology.py` | 14KB | Learn camera spatial relationships |
| `downloader.py` | 6KB | yt-dlp wrapper for video downloads |
| `video_utils.py` | 4KB | FFmpeg wrapper for thumbnails |

### Frontend (`templates/`)

| File | Purpose |
|------|---------|
| `index.html` | Main video library grid |
| `annotate.html` | Video player + annotation tools |
| `ecoeye_preview.html` | Browse/import EcoEye alert videos |
| `training_queue.html` | View/manage training jobs |
| `yolo_export.html` | Configure and run YOLO exports |
| `camera_topology.html` | Visual camera relationship editor |
| `person_manager.html` | Manage person identities for face recognition |
| `sync_settings.html` | Configure EcoEye sync rules |

### Static Assets (`static/`)

- `css/style.css` - Main styles
- `css/annotate.css` - Annotation UI styles
- `js/app.js` - Library interface logic
- `js/annotate.js` - Annotation interface logic

## Database Schema (video_archive.db)

**Core Tables:**
- `videos` - Video metadata (filename, title, duration, resolution, etc.)
- `tags` - Tag definitions with categories
- `video_tags` - Video-tag associations
- `time_range_tags` - Time-based annotations (start_time, end_time, tag_name)
- `keyframe_annotations` - Frame-precise annotations with bounding boxes
- `behaviors` - Legacy behavior annotations

**Integration Tables:**
- `ecoeye_videos` - Synced EcoEye alert videos
- `camera_topology` - Camera spatial relationships
- `training_jobs` - ML training job queue
- `persons` - Known person identities
- `sync_rules` - EcoEye sync configuration

## External Integrations

### EcoEye API
- **Base URL**: `https://alert.ecoeyetech.com`
- **Auth**: API key in header `X-API-KEY`
- **Purpose**: Pull alert videos for annotation
- **Key endpoint**: `/api/alerts` - get alert events with video clips

### UniFi Protect
- **Purpose**: Download video clips from UniFi NVR cameras
- **Client**: `UniFiProtectClient` class handles auth and downloads

## Common Development Tasks

### Adding a New API Endpoint
1. Edit `app/api.py`
2. Add route with `@app.route('/api/your-endpoint')`
3. Restart service: `sudo systemctl restart groundtruth-studio`

### Adding a New Page
1. Create template in `templates/your_page.html`
2. Add route in `app/api.py`:
   ```python
   @app.route('/your-page')
   def your_page():
       return render_template('your_page.html')
   ```
3. Add navigation link in `templates/index.html` if needed

### Modifying Database Schema
1. Edit `app/database.py` - add table creation in `__init__`
2. For migrations, add `ALTER TABLE` in a new method
3. Database auto-creates tables on startup

### Testing Changes
1. Make code changes
2. Restart: `sudo systemctl restart groundtruth-studio`
3. Check logs: `tail -f /opt/groundtruth-studio/server.log`
4. Test in browser: `https://studio.ecoeyetech.com`

## Service Management

```bash
# Restart after code changes
sudo systemctl restart groundtruth-studio

# Check status
sudo systemctl status groundtruth-studio

# View logs
tail -f /opt/groundtruth-studio/flask.log
tail -f /opt/groundtruth-studio/server.log

# Manual run (for debugging)
cd /opt/groundtruth-studio
source venv/bin/activate
python app/api.py
```

## Nginx Config

Location: `/etc/nginx/sites-available/groundtruth-studio`

- SSL via Cloudflare origin certificates
- Proxies to `127.0.0.1:5050`
- Static files served directly from `/opt/groundtruth-studio/static/`
- Max upload: 500MB

## Project Purpose

GroundTruth Studio is used to:
1. **Collect videos** - Download from URLs, upload files, or sync from EcoEye/UniFi
2. **Annotate** - Add time-range tags and bounding box annotations
3. **Export** - Generate YOLO-format datasets for ML training
4. **Train** - Queue and monitor training jobs

The primary use case is creating training data for computer vision models that detect equipment behavior, people, and events in industrial/security camera footage.
