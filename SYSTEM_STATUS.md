# Groundtruth Studio - System Status

**Installation Date:** 2026-01-19
**Version:** 1.0.0
**Location:** `/var/www/html/groundtruth-studio/`

## ✅ Completed Setup

### 1. Database Schema ✓
- **Core tables**: videos, time_range_tags, keyframe_annotations
- **Tag system tables**: tag_groups, tag_options, annotation_tags
- **Database location**: `/var/www/html/groundtruth-studio/video_archive.db`

### 2. Tag Taxonomy ✓
- **29 tag groups** seeded successfully
- **Required groups**: Ground Truth, Confidence Level
- **Optional groups**: 27 additional groups organized into 7 sections
- **Total tag options**: 150+ predefined options

### 3. Application Files ✓
- **Backend**: Flask REST API (`app/api.py`)
- **Frontend**: Vanilla JavaScript with dynamic form generation
- **Templates**: index.html, annotate.html, tag_manager.html
- **Static assets**: CSS and JavaScript files
- **Documentation**: README.md, INSTALL.md, APACHE_SETUP.md

### 4. Dynamic Form Generator ✓
- **JavaScript class**: TagFormGenerator
- **Supports**: Dropdowns, checkboxes, text inputs, textareas
- **Conditional logic**: Forms update based on Ground Truth selection
- **Collapsible sections**: Organized tag groups for better UX

### 5. API Endpoints ✓
- **Videos**: GET /api/videos, POST /api/videos/download, POST /api/videos/upload
- **Annotations**: Time-range tags and keyframe annotations
- **Tag System**: GET /api/tag-schema, POST /api/annotations/<id>/tags
- **Utility**: GET /api/system/status

## ⚠️ Pending Setup

### 1. Install Python Dependencies
```bash
cd /var/www/html/groundtruth-studio
pip3 install -r requirements.txt
```

**Required packages:**
- Flask==3.0.0
- Werkzeug==3.0.1
- yt-dlp (for video downloads)

### 2. Install System Dependencies
```bash
sudo apt install ffmpeg python3-pip
pip3 install yt-dlp
```

### 3. Configure Web Access

**Option A: Development Mode (Port 5000)**
```bash
cd /var/www/html/groundtruth-studio
./start_server.sh
```

Access at:
- http://10.153.2.6:5000
- http://10.100.2.18:5000
- http://localhost:5000

**Option B: Production Mode (Apache Reverse Proxy)**

See [APACHE_SETUP.md](APACHE_SETUP.md) for complete instructions to configure:
- http://10.153.2.6/groundtruth-studio/
- http://10.100.2.18/groundtruth-studio/

Required steps:
1. Enable Apache proxy modules: `sudo a2enmod proxy proxy_http`
2. Update Apache config: `/etc/apache2/sites-available/wordpress.conf`
3. Create systemd service: `/etc/systemd/system/groundtruth-studio.service`
4. Start service and restart Apache

## 📊 Tag Taxonomy Overview

### Required Fields (2 groups)
1. **Ground Truth** (dropdown) - 6 options: power_loading, normal_loading, normal_approach, license_plate, boat_registration, face_detected
2. **Confidence Level** (dropdown) - 5 options: certain, likely, unsure, needs_expert_review, ambiguous_case

### Environmental Factors (3 groups)
3. **Lighting Conditions** (checkbox) - 5 options
4. **Weather Conditions** (checkbox) - 5 options
5. **Water Conditions** (checkbox) - 4 options

### Technical/Quality Issues (2 groups)
6. **Camera Issues** (checkbox) - 6 options
7. **Visibility Issues** (checkbox) - 4 options

### Behavioral Context (4 groups)
8. **Violation Context** (checkbox, time_range only) - 6 options
9. **Motor State** (dropdown) - 4 options
10. **Boat Motion** (dropdown) - 4 options
11. **Extenuating Circumstances** (checkbox, time_range only) - 7 options

### Distinguishing Features (4 groups)
12. **False Positive - Power Loading** (checkbox) - 4 options
13. **False Positive - License Plate** (checkbox) - 3 options
14. **Present Indicators** (checkbox) - 4 options
15. **Absent Indicators** (checkbox) - 4 options

### Object Attributes (10 groups)
16. **Boat Type** (dropdown, keyframe only) - 6 options
17. **Boat Size** (dropdown, keyframe only) - 3 options
18. **Propeller Visible** (dropdown, keyframe only) - 3 options
19. **Registration Visible** (dropdown, keyframe only) - 4 options
20. **Vehicle Type** (dropdown, keyframe only) - 5 options
21. **Plate State** (dropdown, keyframe only) - 4 options
22. **Commercial Vehicle** (dropdown, keyframe only) - 3 options
23. **Face Angle** (dropdown, keyframe only) - 4 options
24. **Face Obstruction** (checkbox, keyframe only) - 5 options
25. **Number of People** (dropdown, keyframe only) - 3 options

### Training Metadata (2 groups)
26. **Training Priority** (dropdown) - 5 options
27. **Dataset Usage** (dropdown) - 4 options

### Review & Notes (2 groups)
28. **Reviewer Notes** (textarea) - Free-form text
29. **Flags for Review** (checkbox) - 3 options

## 🔧 Quick Commands

### Check System Status
```bash
cd /var/www/html/groundtruth-studio

# Verify database
ls -lh video_archive.db

# Count tag groups
python3 -c "import sys; sys.path.insert(0, 'app'); from database import VideoDatabase; db = VideoDatabase(); print(f'Tag groups: {len(db.get_tag_groups())}')"

# Check dependencies
python3 -c "import flask; print(f'Flask {flask.__version__} installed')"
ffmpeg -version | head -1
yt-dlp --version
```

### Start Development Server
```bash
cd /var/www/html/groundtruth-studio
./start_server.sh
```

### Check Service Status (if systemd service is configured)
```bash
sudo systemctl status groundtruth-studio
sudo journalctl -u groundtruth-studio -f
```

## 📁 Directory Structure

```
/var/www/html/groundtruth-studio/
├── app/
│   ├── api.py                    # Flask REST API
│   ├── database.py               # Database operations (1019 lines)
│   ├── downloader.py             # yt-dlp integration
│   └── video_utils.py            # FFmpeg integration
├── templates/
│   ├── index.html                # Video library
│   ├── annotate.html             # Annotation interface
│   └── tag_manager.html          # Tag suggestion manager
├── static/
│   ├── css/
│   │   ├── style.css             # Global styles
│   │   ├── annotate.css          # Annotation interface
│   │   └── tag-form.css          # Dynamic form styles
│   └── js/
│       ├── app.js                # Video library logic
│       ├── annotate.js           # Annotation logic
│       └── tag-form-generator.js # Dynamic form generator (360 lines)
├── downloads/                    # Video storage
├── thumbnails/                   # Generated thumbnails
├── video_archive.db              # SQLite database
├── seed_taxonomy.py              # Database seeding script
├── start_server.sh               # Development server startup
├── requirements.txt              # Python dependencies
├── README.md                     # Complete documentation
├── INSTALL.md                    # Installation guide
├── APACHE_SETUP.md               # Apache configuration
└── SYSTEM_STATUS.md              # This file
```

## 🚀 Next Steps

1. **Install dependencies** (see INSTALL.md)
2. **Start the server** (development or production mode)
3. **Test accessibility** at configured URLs
4. **Add sample video** to test annotation system
5. **Create first annotation** with multi-type tags

## 📞 Support

For issues or questions:
1. Check [INSTALL.md](INSTALL.md) for setup instructions
2. Review [README.md](README.md) for complete documentation
3. See [APACHE_SETUP.md](APACHE_SETUP.md) for production deployment

---

**Groundtruth Studio** - Professional Video Annotation for Machine Learning
Last Updated: 2026-01-19
