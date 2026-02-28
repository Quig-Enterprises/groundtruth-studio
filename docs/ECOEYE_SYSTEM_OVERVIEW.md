# EcoEye System Overview

**Last Updated:** 2026-02-26

## Overview

EcoEye is an integrated multi-camera surveillance, detection, and recognition platform. It combines real-time video analytics, face recognition, vehicle tracking, cross-camera entity matching, violation detection, PTZ camera tasking, and cloud alert integration into a unified system managed through Groundtruth Studio.

---

## System Architecture

### Infrastructure

| Component | Runtime |
|-----------|---------|
| **Groundtruth Studio** | Native Python (Flask) — web UI, API, pipeline orchestration |
| **InsightFace API** | Native Python (systemd) — face detection and embedding service |
| **Frigate NVR** | Docker (GPU-accelerated) — multi-camera recording and detection |
| **CompreFace** | Docker (GPU-accelerated) — alternative face recognition backend |
| **FastReID API** | Native Python — body and vehicle re-identification embeddings |
| **MQTT Broker** | Native — event message bus between all services |
| **PostgreSQL 17** | Native — primary data store |
| **Ollama** | Native — LLM/VLM inference for detection review |
| **Nginx** | Native — reverse proxy and TLS termination |

### GPU Allocation

| GPU | Workload |
|-----|----------|
| GPU 0 | Frigate NVR, CompreFace, InsightFace (face detection + embedding) |
| GPU 1 | YOLO-World vehicle detection, ByteTrack clip tracking, FastReID |

### Database

- **Engine:** PostgreSQL 17
- **Key tables:** videos, predictions, embeddings, identities, tracks, visits, camera_topology_learned, violations, annotations, keyframe_annotations

### MQTT Message Bus

| Topic Pattern | Publisher | Subscriber | Purpose |
|---------------|-----------|------------|---------|
| `frigate/events` | Frigate | Frigate Ingester | Camera detection events |
| `tracker/tracks/+` | Pipeline Worker | InsightFace API | Person track notifications |
| `identity/face/+` | InsightFace API | Pipeline Worker | Face identity results |

---

## Models & Capabilities

### Face Recognition

| Model | Purpose | Details |
|-------|---------|---------|
| **RetinaFace** | Face detection | High-accuracy multi-face detector, 640x640 input |
| **ArcFace** | Face embedding/recognition | 512-dim float32 embeddings, ResNet50, trained on WebFace600K |
| 3D Alignment | 68-point 3D face landmarks | |
| 2D Alignment | 106-point 2D face landmarks | |
| Gender/Age | Demographic estimation | |

**Model Pack:** InsightFace `buffalo_l` (v0.7.3) via ONNX Runtime GPU

**Capabilities:**
- REST API face embedding extraction (multipart upload and base64 endpoints)
- MQTT-driven automatic embedding on person track events
- Embedding storage in PostgreSQL with identity linkage
- Reference gallery building from manually tagged annotations
- Cosine similarity matching against known identities
- HDBSCAN-based clustering of unknown faces into recurring identity groups

### Person Detection

| Model | Purpose |
|-------|---------|
| **person-face-v1** | YOLOv11m person + face detector (custom trained) |

**Pipeline:** YOLO-World pre-screen detects person presence -> person-face-v1 runs precise person/face detection -> InsightFace extracts face embedding -> match against gallery or cluster

### Vehicle Detection & Tracking

| Model | Purpose |
|-------|---------|
| **YOLO-World v2** | Open-vocabulary vehicle + person pre-screening |

**Vehicle Classes:** sedan, pickup truck, SUV, minivan, van, ATV, UTV, box truck, delivery truck, motorcycle, boat, bus, car, truck, and more

**Capabilities:**
- Per-class confidence thresholds for precision tuning
- Multi-object tracking via ByteTrack on video clips
- Real timestamps, per-frame trajectories, best-crop extraction
- Direction-of-travel analysis from trajectory data
- Vehicle ReID embeddings via FastReID

### Vision Language Model (VLM)

| Model | Purpose |
|-------|---------|
| **llama3.2-vision** | AI-assisted reclassification of ambiguous detections |

**Capabilities:**
- Pre-analyzes YOLO detections to filter false positives (trees, shadows, signs)
- Suggests reclassifications with confidence scoring
- Skips high-confidence detections to save compute

### Document Detection

| Model | Purpose |
|-------|---------|
| **doc-detect-v1** | Document detection in images |

---

## Core Capabilities

### 1. Real-Time Multi-Camera Surveillance

- Frigate NVR processes multiple RTSP camera feeds (static and PTZ)
- Object tracking: person, car, truck, motorcycle, boat, bus
- Motion-based recording with configurable retention
- Alert clips with pre/post capture buffers

### 2. Face Recognition Pipeline

- **Detection:** RetinaFace for multi-face detection
- **Embedding:** ArcFace producing 512-dim float32 normalized embeddings
- **Storage:** PostgreSQL embeddings table with identity linkage
- **Recognition:** Cosine similarity matching against reference gallery
- **Clustering:** HDBSCAN groups unknown faces into recurring identity clusters
- **API:** REST endpoints for on-demand embedding + MQTT-driven automatic processing
- **Gallery:** Built from manually tagged person annotations in Groundtruth Studio

### 3. Person Re-Identification (ReID)

- Body appearance embeddings via FastReID
- Face embeddings via InsightFace
- Dual-modality matching (body + face)
- Batch processing of person detections from camera feeds
- Track linking across time windows

### 4. Cross-Camera Entity Matching

- Multi-signal matching: visual appearance (ReID), temporal proximity, classification consistency, bounding box similarity
- Learned camera topology for transit time constraints
- Direction-of-travel compatibility scoring
- Vehicle class compatibility groups (e.g., ATV/UTV/pickup treated as related)
- Separate thresholds for snapshot matches vs. video track matches

### 5. Association Chain Building

Spatial co-occurrence analysis links entities:
- Person exits/enters vehicle -> person_vehicle association
- Vehicle tows trailer -> vehicle_trailer association
- Trailer carries boat -> trailer_boat association
- Person near boat -> person_boat association

### 6. Visit Aggregation

- Groups related tracks into visit records (primary enforcement and reporting unit)
- Captures: arrival, activities across cameras, violations, departure
- Configurable timeout for visit session boundaries
- Periodic aggregation cycle

### 7. Violation Detection

Rule-based enforcement engine detecting:
- **Power loading:** Boat motor running while on trailer at ramp
- **Unauthorized docking:** Vessel docked at restricted location
- Configurable per-camera zone rules
- Minimum confidence and duration thresholds

### 8. EcoEye Cloud Integration

- **Alert Relay:** Syncs with EcoEye Alert Relay API
- **Security:** HMAC-SHA256 request signing, HTTPS-only, rate limiting, replay protection
- **Auto-sync daemon:** Periodic polling, automatic video download
- **Pipeline integration:** Downloaded clips auto-processed through vehicle detection + clip tracking
- **Retention management:** Automatic cleanup of expired alert videos
- **Disk safety:** Skips downloads when free disk is low

### 9. Camera Topology Learning

- Automatically learns spatial relationships between cameras
- Analyzes entity transition patterns across camera feeds
- Builds transit time models for cross-camera matching constraints
- Direction-of-travel inference from learned topology

### 10. Correction-Based Calibration

- Learns per-camera velocity correction factors from human bounding box corrections
- Computes position error, velocity error, and velocity multipliers per camera
- Minimum correction count required before trusting calibration
- Improves motion projection accuracy over time
- Calibration factors stored and consumed by motion projection system

### 11. Image Quality Assessment

- Crop quality scoring for detection thumbnails
- Ensures high-quality crops are selected for ReID embedding extraction
- Filters poor-quality detections before downstream processing

### 12. PTZ Camera Tasking (ONVIF)

Intelligent PTZ (Pan-Tilt-Zoom) camera control that supplements static camera coverage by capturing high-resolution images of identifying features on demand.

**Concept of Operations:**
- Static cameras monitor scenes continuously (e.g., boat ramp, parking lot)
- When the detection pipeline identifies a target of interest, the PTZ is tasked to acquire specific shots
- The PTZ works through a capture task list while the static camera continues uninterrupted monitoring
- Results feed back into the recognition and evidence pipelines

**ONVIF PTZ Control:**
- Industry-standard ONVIF protocol for pan, tilt, zoom, and preset commands
- Compatible with ONVIF Profile S cameras
- Absolute and relative positioning for precise framing
- Preset positions for known areas of interest (ramp entry, dock, parking)
- Auto-return to home/patrol position after task completion

**Automated Target Acquisition:**
- **Person tracking:** Slew PTZ to track a detected person and capture close-up face images for higher-quality face embedding
- **Vehicle tracking:** Zoom to capture license plates that are illegible in static camera wide shots
- **Boat registration:** Capture hull registration numbers, transom markings, and identifying features
- **General identifying features:** Any detail the static camera cannot resolve — permits, trailer tags, vessel names

**Task Queue Model:**
- Detection pipeline generates a prioritized capture task list per scene event
- Each task specifies: target type, estimated location (from static camera bbox + camera geometry), required zoom level, capture count
- PTZ controller works through the list sequentially, capturing images at each target
- Captured images are stored and linked back to the originating track/visit/entity
- If the target moves (tracked via static camera updates), PTZ re-aims before capture

**Use Case: Boat Ramp Scene**
```
Static camera detects:
  - Vehicle + trailer arriving
  - Person(s) exiting vehicle
  - Boat on trailer

PTZ is tasked to capture:
  1. Vehicle license plate (rear, zoomed)
  2. Trailer registration tag
  3. Boat hull registration number
  4. Boat transom (name/port)
  5. Person face (close-up for recognition)
  6. Any permits/stickers on vehicle windshield

PTZ returns to patrol position when task list is complete.
Static camera never stops monitoring.
```

**Integration Points:**
- Receives target coordinates from cross-camera matching and track builder
- Captured images fed into face recognition, OCR (plates/registrations), and evidence storage
- Results linked to visits and association chains for complete enforcement records
- Camera topology learner includes PTZ coverage zones in spatial model

### 13. CLIP Analysis

- Semantic video clip analysis
- Worker-based asynchronous processing

---

## Groundtruth Studio UI

| Page | Purpose |
|------|---------|
| EcoEye Preview | Alert preview and management |
| Persons | Person identity management |
| Identities | Identity database browser |
| Face Photo Manager | Face photo tagging and gallery management |
| Tracks | Track browser and review |
| Predictions | AI prediction review (approve/reject/reclassify) |
| Training | Model training management |
| Training Gallery | Training data gallery browser |
| Videos | Video library management |
| Frigate | NVR integration and event browser |
| Camera Map | Camera topology visualization |
| Annotations | Annotation tools |
| Locations | Location management |
| CLIP Analysis | Semantic analysis viewer |
| Documents | Document detection results |
| Vibration | Bearing vibration monitoring |
| UniFi | UniFi Protect integration |
| Models | Model management |
| YOLO Export | Training data export |

---

## Service Endpoints

| Service | Protocol | Purpose |
|---------|----------|---------|
| Groundtruth Studio | HTTP | Main web UI and API |
| InsightFace API | HTTP | Face embedding extraction |
| FastReID API | HTTP | Body/vehicle ReID embeddings |
| Frigate NVR | HTTP | NVR API and clips |
| Frigate Web UI | HTTP | Authenticated camera UI |
| Frigate RTSP | RTSP | Re-streamed camera feeds |
| Frigate WebRTC | WebRTC | Low-latency live view |
| CompreFace API | HTTP | Alternative face recognition |
| Ollama | HTTP | LLM/VLM inference |
| MQTT Broker | MQTT | Event message bus |
| PostgreSQL | TCP | Primary database |

---

## Data Flow

```
Camera RTSP Feeds (Static + PTZ)
       |
       v
  Frigate NVR (detection + recording)
       |
       v
  MQTT Event Bus
       |
       v
  Event Ingester -> Database (videos, thumbnails)
       |
       v
  YOLO-World Pre-Screen (GPU)
       |
       +--[person detected]--> Person/Face Model -> InsightFace (face embed)
       |                                              |
       |                                              v
       |                                    Face Gallery Match / HDBSCAN Cluster
       |                                              |
       |                                              v
       |                                    Person Recognition + ReID
       |
       +--[vehicle detected]--> Prediction Queue -> VLM Review -> Human Review
       |                                              |
       |                                              v
       |                                    ByteTrack Clip Tracking
       |                                              |
       |                                              v
       |                                    FastReID Vehicle Embeddings
       |
       v
  Cross-Camera Entity Matching
       |
       +--[target of interest]--> PTZ Task Queue (ONVIF)
       |                                |
       |                                v
       |                     PTZ Capture: plates, faces,
       |                     registrations, permits
       |                                |
       |                                v
       |                     OCR / Face Recognition / Evidence Store
       |                                |
       |                                +---> Link to track/visit/entity
       |
       v
  Association Chain Builder (person<->vehicle<->trailer<->boat)
       |
       v
  Visit Builder (session aggregation)
       |
       v
  Violation Detector (rule-based enforcement)
```

**Cloud Integration Path:**
```
EcoEye Alert Relay (cloud)
       |
       v
  Auto-Sync Daemon (periodic poll, HMAC-SHA256 auth)
       |
       v
  Download Video Clips -> Vehicle Detection Pipeline -> Clip Tracking
```

---

## Status Summary

| Component | Status | Notes |
|-----------|--------|-------|
| Frigate NVR | Running | Multiple cameras active |
| InsightFace API | Running | RetinaFace + ArcFace on GPU |
| CompreFace | Configured | Available as alternative face recognition backend |
| Groundtruth Studio | Running | Full web UI operational |
| Event Ingester | Running | MQTT event pipeline active |
| Person Detection | Operational | Custom-trained person-face model |
| Vehicle Detection | Operational | YOLO-World v2 open-vocabulary pre-screening |
| Face Recognition | Operational | 512-dim ArcFace embeddings, gallery matching, HDBSCAN clustering |
| Person ReID | Operational | Dual-modality (face + body) |
| Cross-Camera Matching | Operational | Multi-signal with learned topology |
| Visit Builder | Operational | Periodic aggregation cycle |
| Violation Detection | Operational | Rule-based enforcement engine |
| EcoEye Cloud Sync | Operational | Auto-sync daemon with HMAC auth |
| VLM Review | Operational | Vision language model for detection filtering |
| Camera Topology Learning | Operational | Auto-learns from entity transitions |
| Calibration | Operational | Correction-based per-camera tuning |
| PTZ Camera Tasking | In Progress | ONVIF control demonstrated; feedback loop integration underway |
