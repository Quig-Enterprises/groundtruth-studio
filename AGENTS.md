# GroundTruth Studio - AI Development Context

> Multi-purpose ML training platform: video annotation, document synthesis, prediction review, cross-camera tracking, and YOLO dataset export. Integrates with EcoEye and UniFi Protect cameras.

## Quick Reference

| Item | Value |
|------|-------|
| **Location** | `/opt/groundtruth-studio` |
| **URL** | `https://studio.ecoeyetech.com` |
| **Port** | 5050 (Flask behind nginx) |
| **Service** | `sudo systemctl restart groundtruth-studio` |
| **Logs** | `tail -f /opt/groundtruth-studio/flask.log` or `server.log` |
| **Database** | PostgreSQL (`groundtruth_studio`) — 46 tables |
| **DB Connection** | `app/db_connection.py` + `DATABASE_URL` env var |
| **Max Upload** | 2 GB |

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                        nginx (443)                                │
│                   studio.ecoeyetech.com                           │
│          SSL via Cloudflare · RBAC via X-Auth-Role                │
└──────────────────────┬───────────────────────────────────────────┘
                       │ proxy_pass
                       ▼
┌──────────────────────────────────────────────────────────────────┐
│                   Flask App (port 5050)                            │
│                      app/api.py                                    │
│           22 Blueprints registered in app/routes/                  │
├──────────────────────────────────────────────────────────────────┤
│  RBAC: X-Auth-Role header (viewer/user/admin/super)               │
│  Viewers: read-only. Users+: can POST/PUT/DELETE.                 │
└──────────────────────────────────────────────────────────────────┘
        │              │              │              │
  ┌─────┘       ┌──────┘       ┌─────┘       ┌─────┘
  ▼             ▼              ▼              ▼
┌──────┐  ┌──────────┐  ┌──────────┐  ┌────────────────────────┐
│Postgr│  │ EcoEye   │  │  UniFi   │  │ /mnt/storage/          │
│SQL DB│  │  API     │  │ Protect  │  │  training-material/    │
│46 tbl│  │alert.eco │  │  NVR     │  │  documents/synthesizer │
└──────┘  └──────────┘  └──────────┘  └────────────────────────┘
```

## Navigation Structure

```
Groundtruth Studio
├── Video Library                     → /
├── Review
│   ├── AI Review                    → /prediction-review
│   ├── Quick Review                 → /review
│   └── Track Review                 → /interpolation-review
├── AI & Training
│   ├── Model Training               → /model-training
│   ├── Vehicle Metrics              → /vehicle-metrics
│   ├── Crossing Lines               → /crossing-line-config
│   ├── Clip Analysis                → /clip-analysis
│   ├── Training Gallery             → /training-gallery
│   ├── Doc Training Data            → /doc-training-data
│   ├── Document Synthesizer         → /doc-template-annotator
│   └── Synthesized Identity Manager → /face-photo-manager
├── Content & Cameras
│   ├── Add Content                  → /add-content
│   ├── Camera Management            → /camera-management
│   ├── Camera Map                   → /camera-map
│   ├── Camera Sync                  → /camera-sync
│   ├── Person Manager               → /person-manager
│   ├── EcoEye Preview               → /ecoeye-preview
│   └── Document Upload              → /document-upload
└── Export & Settings
    ├── Vibration Export             → /vibration-export
    ├── Location Export              → /location-export
    └── Sync Settings                → /sync-settings
```

Hidden (URL-only): `/yolo-export`, `/training-queue`, `/camera-topology`, `/cross-camera-review`, `/vehicle-metrics/<class>`, `/batch-cluster-review`

---

## Backend — Route Blueprints (`app/routes/`)

| File | Blueprint | Page Routes | Description |
|------|-----------|-------------|-------------|
| `annotations.py` | `annotations_bp` | — | CRUD for time-range tags, keyframe annotations, tag suggestions, tag groups |
| `camera_map.py` | `camera_map_bp` | `/camera-map` | Camera map placements, FOV previews, unplaced cameras |
| `camera_sync.py` | `camera_sync_bp` | `/camera-sync` | Camera overlap groups, WebRTC sync, PTZ controls and calibration |
| `clip_analysis.py` | `clip_analysis_bp` | `/clip-analysis` | Clip analysis jobs, track review, reclassification, training export |
| `doc_template_annotator.py` | `doc_template_annotator_bp` | `/doc-template-annotator` | Document template CRUD, preview, batch synthesis, font listing, overlays, scene backgrounds |
| `doc_training_browser.py` | `doc_training_browser_bp` | `/doc-training-data` | Browse/filter synthesized document training data with stats and thumbnails |
| `documents.py` | `documents_bp` | `/document-upload` | Document upload, OCR scan pipeline, identity linking |
| `ecoeye.py` | `ecoeye_bp` | `/ecoeye-preview`, `/sync-settings` | EcoEye alert browser, camera/site/tag CRUD, auto-sync config |
| `face_photo_manager.py` | `face_photo_manager_bp` | `/face-photo-manager` | Synthesized face photo management, profile CRUD, gender detection |
| `frigate.py` | `frigate_bp` | — | Frigate NVR start/stop/status, frame capture |
| `identities.py` | `identities_bp` | — | Identity sync, external embedding ingest |
| `locations.py` | `locations_bp` | `/location-export` | Camera location CRUD, reference images, export-and-train |
| `models.py` | `models_bp` | — | AI model registry, confidence thresholds, metrics |
| `persons.py` | `persons_bp` | `/person-manager`, `/camera-topology`, `/camera-management` | Person detection, name assignment, clustering, camera topology graph |
| `predictions.py` | `predictions_bp` | `/prediction-review`, `/review`, `/cross-camera-review`, `/vehicle-metrics` | Prediction serving, auto-detect, review workflow, stats |
| `tracks.py` | `tracks_bp` | `/crossing-line-config` | Track building, crossing line CRUD, cross-camera spatial match, ReID links |
| `training.py` | `training_bp` | `/training-queue`, `/model-training` | Training job queue, worker status, export-and-train, auto-retrain, model deploy |
| `training_gallery.py` | `training_gallery_bp` | `/training-gallery` | Training sample browser with filtering, clusters, bulk actions |
| `unifi.py` | `unifi_bp` | — | Unifi Protect sync config, connection test |
| `vibration.py` | `vibration_bp` | `/vibration-export` | Vibration sensor tag listing and export |
| `videos.py` | `videos_bp` | `/add-content`, `/annotate` | Video library CRUD, upload, URL download queue |
| `yolo_export.py` | `yolo_export_bp` | `/yolo-export` | YOLO export config CRUD, preview, export trigger |

## Backend — Core Modules (`app/`)

| File | Description |
|------|-------------|
| `api.py` | Flask entry point — blueprint registration, RBAC middleware, CORS |
| `schema.py` | Full PostgreSQL DDL (46 tables + indexes), migrations |
| `db_connection.py` | PostgreSQL connection pool (`psycopg2`) |
| `database.py` | VideoDatabase wrapper shim |
| `services.py` | Shared service singletons, background daemon startup |
| `clip_analysis.py` | Core clip analysis engine (ByteTrack MOT, YOLO detection) |
| `clip_tracker.py` | ByteTrack multi-object tracker |
| `cross_camera_matcher.py` | ReID-based cross-camera entity matching |
| `crossing_line_matcher.py` | Spatial-temporal matching using crossing lines |
| `camera_topology.py` | Learns inter-camera transit times |
| `auto_detect.py` | YOLO auto-detection runner per video |
| `auto_detect_runner.py` | Subprocess wrapper for auto-detection |
| `auto_retrain.py` | Background auto-retrain daemon |
| `calibration.py` | PTZ camera calibration math |
| `context_engine.py` | Scene context builder for classification |
| `doc_detect_runner.py` | YOLO document detection runner |
| `doc_ocr_runner.py` | OCR pipeline for extracting text from documents |
| `download_queue.py` | Async video download queue |
| `downloader.py` | HTTP video downloader |
| `ecoeye_auto_sync.py` | Background EcoEye alert sync daemon |
| `ecoeye_sync.py` | Full EcoEye API client |
| `face_clustering.py` | Face embedding clustering |
| `frigate_ingester.py` | Frigate NVR MQTT/API ingest |
| `gpu_manager.py` | GPU resource allocation |
| `image_quality.py` | Image quality scoring (blur, exposure) |
| `interpolation_runner.py` | Keyframe interpolation between approved predictions |
| `location_exporter.py` | Camera location training dataset export |
| `ocr_exporter.py` | OCR field export for training |
| `person_recognizer.py` | Face recognition (ArcFace embeddings) |
| `person_reid_processor.py` | Body ReID embedding extraction |
| `pipeline_worker.py` | Multi-stage processing pipeline orchestrator |
| `prediction_grouper.py` | Groups predictions for batch review |
| `sample_router.py` | Routes predictions to review queue by confidence |
| `sync_config.py` | EcoEye/Unifi sync settings manager |
| `track_builder.py` | Long-term identity track builder |
| `training_queue.py` | Training job queue client |
| `unifi_protect_client.py` | Unifi Protect API client |
| `vehicle_detect_runner.py` | Vehicle detection, classification, and attribute pipeline |
| `vibration_exporter.py` | Vibration sensor data export |
| `video_utils.py` | FFmpeg wrapper — thumbnails, clip extraction |
| `violation_detector.py` | Policy violation detection |
| `visit_builder.py` | Aggregates tracks into visit records |
| `vlm_reviewer.py` | VLM-based auto-review for predictions |
| `yolo_exporter.py` | YOLO dataset assembly and export |

---

## Document Synthesizer Subsystem

Located at `/mnt/storage/training-material/documents/synthesizer/`. Generates synthetic document images with randomized PII for YOLO training.

### Core Files

| File | Description |
|------|-------------|
| `doc_synthesizer.py` | Synthesis engine — renders templates with randomized identities, photos, barcodes. Supports photocopy simulation (washout/oversaturated/grayscale), artifact overlays, color temperature, scene compositing, fold cropping. |
| `profile_store.py` | Persistent identity profiles — links face photos to consistent demographics across document types. Gender-aware name generation. |
| `gender_detector.py` | Gender detection from face photos using OpenCV DNN (Caffe model) |
| `preprocess_faces.py` | Face photo alignment, cropping, quality filtering |
| `extract_faces.py` | Face region extraction from source images |

### Document Types

| Class ID | Class Name | Persona Fields |
|----------|------------|----------------|
| 0 | `us_passport` | passport_no, nationality, place_of_birth |
| 1 | `twic` | twic_number, issuer="TSA" |
| 2 | `mmc` | mmc_number, mariner_type, reference_number |
| 3 | `wi21dl` | license_number, dl_class, restrictions |
| 5 | `uscg_medical_cert` | medical_cert_number, issuer="USCG", STCW fields |

### Generation Features

- **Photocopy simulation**: Washout (faded/low toner) and oversaturated (heavy toner with shadow bands, speckle). Output is grayscale.
- **Photocopy overlays**: Real scanned artifact images (ghost bleed-through, vertical streaks, edge smears) composited via multiply blend with random flip/opacity.
- **Scene compositing**: Places document on textured background with perspective warp and rotation.
- **Color temperature**: Simulates warm (2700K) to cool (6500K) lighting.
- **Fold cropping**: Fold lines split document into partial variants (simulates folded documents).
- **Print degradation**: Content layer degradation (faded opacity, uneven ink, speckle dropout, streaks).

### Assets

| Path | Contents |
|------|----------|
| `assets/names/` | Name lists: `first_names.txt`, `first_names_m.txt`, `first_names_f.txt`, `last_names.txt` |
| `assets/addresses/` | `us_addresses.txt` |
| `assets/scene_backgrounds/` | Background scene photos for compositing |
| `faces/` | Preprocessed face photo pool (diverse) |
| `faces_white/` | Preprocessed face photo pool (lighter skin tones) |
| `templates/` | Document templates indexed by `index.json` |
| `output/` | Generated images + YOLO labels |
| `output/metadata/` | Per-image metadata JSON |

### Overlay Assets

| Path | Contents |
|------|----------|
| `/opt/groundtruth-studio/static/overlays/photocopy/ghost_bleedthrough.jpg` | Faint text from previous page showing through |
| `/opt/groundtruth-studio/static/overlays/photocopy/vertical_streaks.jpg` | Dirty scanner glass artifact |
| `/opt/groundtruth-studio/static/overlays/photocopy/edge_smear_right.jpg` | Heavy toner/roller artifact (randomly mirrored for left/right) |

---

## Worker Subsystem (`worker/`)

Separate virtualenv with PyTorch, InsightFace, ONNX Runtime.

| File | Description |
|------|-------------|
| `training_worker.py` | YOLO training worker — polls job queue, runs YOLOv8/v11 training, uploads to S3 |
| `training_worker_local.py` | Local (non-S3) training worker for development |
| `face_embed_worker.py` | Face embedding extraction (ArcFace) |
| `clip_analysis_worker.py` | Clip analysis job trigger |
| `train_bearing_fault.py` | Bearing fault detection model trainer |

---

## Frontend

### JavaScript (`static/js/`)

| File | Description |
|------|-------------|
| `app.js` | Video library home — grid, filtering, library management |
| `annotate.js` | Annotation tool — video playback, bbox drawing, time-range tags |
| `annotate-integration.js` | Integration layer for tag groups/suggestions API |
| `annotation-scenarios.js` | Scenario-based annotation workflow definitions |
| `camera-sync.js` | WebRTC stream sync, overlap groups |
| `camera_map.js` | Leaflet-based camera map with FOV visualization |
| `clip-analysis.js` | Clip analysis — track review, reclassification |
| `cross-camera-review.js` | Cross-camera ReID match review |
| `crossing_line_config.js` | Crossing line editor with canvas overlay |
| `doc-template-annotator.js` | Document synthesizer UI — template editor, field placement, generation |
| `doc-training-browser.js` | Document training data browser |
| `face-photo-manager.js` | Face photo management, profiles, gender detection |
| `model_training.js` | Training dashboard — job queue, metrics, deploy |
| `person-manager.js` | Person detection browser, clustering |
| `prediction-review.js` | Desktop prediction review |
| `prediction-review-mobile.js` | Mobile prediction review (swipe UI, offline-capable) |
| `prediction_review_page.js` | Review page routing |
| `scenario-workflow.js` | Multi-step annotation scenario engine |
| `training-gallery.js` | Training sample gallery with bulk actions |
| `vehicle_class_detail.js` | Vehicle class detail stats |
| `vehicle_metrics_page.js` | Vehicle metrics dashboard |
| `yolo_export.js` | YOLO export config editor |
| `location_export.js` | Location export trigger |
| `vibration_export.js` | Vibration data export |
| `tag_manager.js` | Tag group management |
| `interpolation_review.js` | Interpolation track review |
| `gt-utils.js` | Shared utility functions |
| `responsive-utils.js` | Responsive layout helpers |
| `shared-class-search.js` | Class/tag search autocomplete |
| `tag-form-generator.js` | Dynamic tag form generator |
| `image-player-shim.js` | Image-based "video" playback compat |
| `movement-tracking.js` | Trajectory visualization helpers |
| `waveform.js` | Vibration waveform visualization |

### CSS (`static/css/`)

| File | Description |
|------|-------------|
| `style.css` | Global styles — layout, nav, cards, modals, base components |
| `style-additions.css` | Minor overrides |
| `annotate.css` | Annotation tool styles |
| `camera-sync.css` | Camera sync styles |
| `clip-analysis.css` | Clip analysis styles |
| `cross-camera-review.css` | Cross-camera review layout |
| `doc-template-annotator.css` | Document synthesizer UI styles |
| `doc-training-browser.css` | Document training browser styles |
| `face-photo-manager.css` | Face photo manager styles |
| `interpolation_review.css` | Interpolation review styles |
| `prediction-review-mobile.css` | Mobile review styles |
| `scenario-workflow.css` | Scenario workflow styles |
| `tag-form.css` | Tag form styles |
| `tag_manager.css` | Tag manager styles |
| `training-gallery.css` | Training gallery styles |

---

## Database Schema (46 tables)

### Core Video & Annotation
`videos`, `tags`, `video_tags`, `behaviors`, `time_range_tags`, `keyframe_annotations`, `tag_groups`, `tag_options`, `annotation_tags`, `tag_suggestions`

### YOLO Export
`yolo_export_configs`, `yolo_export_videos`, `yolo_export_filters`, `yolo_export_logs`

### Fleet / Vehicles
`fleet_vehicles`, `vehicle_person_links`, `trailers`, `vehicle_trailer_links`

### Sync & External
`sync_config`, `sync_history`, `ecoeye_alerts`

### Camera System
`camera_locations`, `camera_aliases`, `camera_topology_learned`, `camera_crossing_lines`, `camera_object_tracks`, `ptz_calibration_points`

### Training & AI
`training_jobs`, `ai_predictions`, `model_registry`, `training_metrics`, `interpolation_tracks`, `prediction_groups`

### Content Libraries
`content_libraries`, `content_library_items`

### Multi-Entity Detection (MEDS)
`identities`, `embeddings`, `associations`, `tracks`, `sightings`, `violations`, `visits`

### Cross-Camera Tracking
`cross_camera_links`, `video_tracks`

### Document Intelligence
`document_scans`, `identity_documents`

---

## External Storage

```
/mnt/storage/training-material/
├── documents/
│   ├── synthesizer/              ← Document synthesis system
│   ├── yolo-doc-detect/merged/   ← Merged YOLO doc-detect training set
│   ├── datasets/idnet/           ← IDNet document dataset
│   ├── datasets/midv-2020/       ← MIDV-2020 document dataset
│   ├── datasets/midv-500/        ← MIDV-500 document dataset
│   ├── bearing/                  ← Bearing fault vibration data
│   └── shipping/                 ← Shipping document samples
├── processed/people-vehicles-objects/
├── sensor/                       ← Bearing/motor fault datasets (CWRU, IMS, MFPT, etc.)
└── vision/                       ← COCO, CrowdHuman, WIDER datasets
```

---

## External Integrations

### EcoEye API
- **Base URL**: `https://alert.ecoeyetech.com`
- **Auth**: API key in header `X-API-KEY`
- **Purpose**: Pull alert videos for annotation and training

### UniFi Protect
- **Client**: `UniFiProtectClient` — camera listing and video sync

### Frigate NVR
- **Client**: `frigate_ingester.py` — MQTT/API detection ingest

---

## Common Development Tasks

### Adding a New API Endpoint
1. Create or edit a route file in `app/routes/`
2. Register blueprint in `app/routes/__init__.py` if new
3. Import and register in `app/api.py` if new
4. Restart: `sudo systemctl restart groundtruth-studio`

### Adding a New Page
1. Create template in `templates/your_page.html`
2. Add route in appropriate blueprint
3. Add nav link in `templates/partials/nav.html`

### Modifying Database Schema
1. Edit `app/schema.py` — add/update table DDL
2. Add migration to `run_migrations()` in `schema.py`
3. Restart service (migrations run on startup)

### Service Management
```bash
sudo systemctl restart groundtruth-studio   # Restart after changes
sudo systemctl status groundtruth-studio    # Check status
tail -f /opt/groundtruth-studio/flask.log   # View logs
tail -f /opt/groundtruth-studio/server.log  # View logs
```

## Nginx Config

Location: `/etc/nginx/sites-available/groundtruth-studio`
- SSL via Cloudflare origin certificates
- Proxies to `127.0.0.1:5050`
- Static files served directly from `/opt/groundtruth-studio/static/`
- RBAC: `auth_request` sets `X-Auth-Role` header
