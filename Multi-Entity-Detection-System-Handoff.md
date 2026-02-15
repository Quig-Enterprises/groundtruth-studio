# Multi-Entity Detection, Tracking & Identification System Architecture

**Technical Handoff Document — RampGuard + Argus + Groundtruth Studio**
**February 2026**
**Infrastructure: Artemis Server (192.168.50.20) · Dual RTX 4090**

---

## 1. System Overview

This document describes the architecture for a multi-entity detection, tracking, and identification system. The system identifies and tracks people, vehicles, boats, and their associations across multiple camera feeds, enabling comprehensive visit-level intelligence for marina enforcement and beyond.

### 1.1 Model Architecture

| Model | Type | Purpose | Runs When |
|-------|------|---------|-----------|
| **Argus** | YOLO Object Detection | Detect people, vehicles, boats, trailers | Every frame (5–10 fps) |
| **InsightFace** | Face Embedding | Generate 512-d face vectors for identity matching | When face detected in person crop |
| **OSNet** | Person ReID | Body appearance embedding for tracking through occlusions | Every tracked person, every frame |
| **BoT-SORT** | Multi-Object Tracker | Frame-to-frame track continuity via motion + IoU | Every frame |
| **Boat ReID** | Custom ReID (fine-tuned OSNet) | Visual boat appearance embedding | Every tracked boat |
| **Vehicle Classifier** | Fine-tuned ResNet/EfficientNet | Make/model/year identification | On vehicle crops from Argus |
| **ALPR** | OCR Pipeline | License plate text extraction | On plate crops |
| **Boat Registration OCR** | Custom OCR | Hull registration number extraction | On boat hull crops |
| **Power Loading** | Custom YOLO Classifier | Detect propeller spray, violation behavior | When boat+trailer co-detected at ramp |

---

## 2. Frame Processing Pipeline

Each frame from each camera follows this processing sequence. Expensive operations (face embedding, OCR) only run when triggered by cheaper upstream detections.

### 2.1 Per-Frame Flow

```
Frame Ingress (5-10 fps per camera)
  │
  ├── Argus (YOLO) → Bounding boxes: person, vehicle, boat, trailer
  │
  ├── BoT-SORT → Assign/continue track IDs via motion + IoU
  │
  ├── Per tracked PERSON:
  │     ├── OSNet ReID embedding (always, any angle)
  │     ├── Face visible? → InsightFace embedding → identity match
  │     └── No face? → maintain identity via ReID + track continuity
  │
  ├── Per tracked VEHICLE:
  │     ├── Vehicle Classifier (make/model/year)
  │     └── ALPR (license plate OCR when visible)
  │
  ├── Per tracked BOAT:
  │     ├── Boat ReID embedding (visual appearance)
  │     └── Registration OCR (hull number when visible)
  │
  └── Context Engine: Link person ↔ vehicle ↔ trailer ↔ boat
```

### 2.2 Cross-Camera Handoff

When a tracked entity leaves one camera's field of view, the system re-acquires them on adjacent cameras using ReID embeddings, face/plate identifiers, and spatial-temporal constraints.

```
Person leaves Camera A → track ends, identity + embeddings persist
Person enters Camera B → new track, ReID compared to recent departures
   ├── Spatial filter: only compare against adjacent camera departures
   ├── Temporal filter: within expected transit time window
   ├── ReID match? → same identity linked to new track
   └── Face visible later? → confirms/corrects the match
```

---

## 3. Data Schema

The schema is designed around entities (people, vehicles, boats) and their relationships, with sightings providing the temporal/spatial record. All embeddings are stored as arrays of floats for cosine similarity matching.

### 3.1 `identities`

Long-term identity records. An identity may be unnamed (auto-clustered) until a human labels it.

| Column | Type | Nullable | Description |
|--------|------|----------|-------------|
| **identity_id** | UUID (PK) | No | Unique identifier |
| name | VARCHAR(255) | Yes | Human-assigned name; null = "Unknown #N" |
| identity_type | ENUM | No | `'person'`, `'vehicle'`, `'boat'`, `'trailer'` |
| first_seen | TIMESTAMP | No | First detection timestamp |
| last_seen | TIMESTAMP | No | Most recent detection timestamp |
| metadata | JSONB | Yes | Flexible attributes (see below) |
| is_flagged | BOOLEAN | No | Flagged for enforcement interest |
| notes | TEXT | Yes | Admin notes about this identity |
| created_at | TIMESTAMP | No | Record creation time |

**Metadata JSONB examples by identity_type:**

```json
// Person
{"face_cluster_id": "...", "known_associates": [...]}

// Vehicle
{"make": "Ford", "model": "F-150", "year": 2022, "color": "silver", "plate_state": "FL", "plate_number": "ABC1234"}

// Boat
{"type": "center_console", "length_ft": 19, "color": "blue", "registration": "FL1234AB", "motor_config": "single"}

// Trailer
{"type": "single_axle", "color": "white", "fits_boat_length": 21}
```

### 3.2 `embeddings`

Stores face, body (ReID), and visual appearance embeddings. Multiple embeddings per identity capture different angles, lighting, and appearances.

| Column | Type | Nullable | Description |
|--------|------|----------|-------------|
| **embedding_id** | UUID (PK) | No | Unique identifier |
| identity_id | UUID (FK) | No | References `identities.identity_id` |
| embedding_type | ENUM | No | `'face'`, `'body_reid'`, `'boat_reid'`, `'vehicle_appearance'` |
| vector | FLOAT[512] | No | Embedding vector (cosine similarity) |
| confidence | FLOAT | No | Quality/confidence score 0.0–1.0 |
| source_image_path | VARCHAR(500) | Yes | Path to cropped source image |
| camera_id | VARCHAR(100) | Yes | Camera that captured this embedding |
| is_reference | BOOLEAN | No | True = manually uploaded reference photo |
| session_date | DATE | Yes | For ReID: date of clothing appearance (expires daily) |
| created_at | TIMESTAMP | No | Capture/upload time |

### 3.3 `associations`

Links entities together: person↔vehicle, vehicle↔trailer, trailer↔boat. Associations are probabilistic and build confidence over repeated co-observations.

| Column | Type | Nullable | Description |
|--------|------|----------|-------------|
| **association_id** | UUID (PK) | No | Unique identifier |
| identity_a | UUID (FK) | No | First entity in the association |
| identity_b | UUID (FK) | No | Second entity in the association |
| association_type | ENUM | No | `'person_vehicle'`, `'vehicle_trailer'`, `'trailer_boat'`, `'person_boat'` |
| confidence | FLOAT | No | Association strength 0.0–1.0; increases with repeated co-sightings |
| observation_count | INTEGER | No | Number of times seen together |
| first_observed | TIMESTAMP | No | First co-sighting |
| last_observed | TIMESTAMP | No | Most recent co-sighting |

### 3.4 `tracks`

A track represents a continuous observation of a single entity within one camera. Tracks are linked to identities via embeddings and direct identification (face, plate).

| Column | Type | Nullable | Description |
|--------|------|----------|-------------|
| **track_id** | UUID (PK) | No | Unique identifier |
| identity_id | UUID (FK) | Yes | Linked identity; null if unresolved |
| camera_id | VARCHAR(100) | No | Source camera identifier |
| entity_type | ENUM | No | `'person'`, `'vehicle'`, `'boat'`, `'trailer'` |
| start_time | TIMESTAMP | No | Track start (first frame) |
| end_time | TIMESTAMP | Yes | Track end (last frame); null if active |
| identity_method | ENUM | Yes | `'face'`, `'reid'`, `'plate'`, `'registration'`, `'manual'`, `'association'` |
| identity_confidence | FLOAT | Yes | Confidence in identity link 0.0–1.0 |

### 3.5 `sightings`

Per-frame detections within a track. Stored as time-series data for replay and analysis.

| Column | Type | Nullable | Description |
|--------|------|----------|-------------|
| **sighting_id** | BIGSERIAL (PK) | No | Auto-increment for time-series performance |
| track_id | UUID (FK) | No | Parent track |
| timestamp | TIMESTAMP | No | Frame timestamp |
| bbox | FLOAT[4] | No | [x, y, width, height] normalized 0–1 |
| confidence | FLOAT | No | Detection confidence |
| face_visible | BOOLEAN | No | Was face detected in this frame (person only) |

### 3.6 `camera_topology`

Defines spatial relationships between cameras for cross-camera handoff. Transit times are learned over time from observed handoffs.

| Column | Type | Nullable | Description |
|--------|------|----------|-------------|
| **camera_a** | VARCHAR(100) | No | Source camera |
| **camera_b** | VARCHAR(100) | No | Adjacent camera |
| min_transit_seconds | INTEGER | No | Minimum expected transit time |
| max_transit_seconds | INTEGER | No | Maximum expected transit time |
| avg_transit_seconds | FLOAT | Yes | Learned average from observed handoffs |
| observation_count | INTEGER | No | Number of observed transitions |

### 3.7 `violations`

Records detected violations with full association chain linking the violator, their vehicle, trailer, and boat.

| Column | Type | Nullable | Description |
|--------|------|----------|-------------|
| **violation_id** | UUID (PK) | No | Unique identifier |
| violation_type | ENUM | No | `'power_loading'`, `'unauthorized_dock'`, etc. |
| camera_id | VARCHAR(100) | No | Camera that captured violation |
| timestamp | TIMESTAMP | No | Violation detection time |
| person_identity_id | UUID (FK) | Yes | Identified violator (if resolved) |
| vehicle_identity_id | UUID (FK) | Yes | Associated vehicle |
| boat_identity_id | UUID (FK) | Yes | Associated boat |
| trailer_identity_id | UUID (FK) | Yes | Associated trailer |
| evidence_paths | TEXT[] | No | Array of video clip / snapshot paths |
| confidence | FLOAT | No | Violation detection confidence |
| status | ENUM | No | `'detected'`, `'confirmed'`, `'false_positive'`, `'actioned'` |
| reviewed_by | VARCHAR(100) | Yes | Human reviewer |
| notes | TEXT | Yes | Review notes |

### 3.8 `visits`

Aggregates all entity activity into a single visit record — the primary enforcement and reporting unit.

| Column | Type | Nullable | Description |
|--------|------|----------|-------------|
| **visit_id** | UUID (PK) | No | Unique identifier |
| person_identity_id | UUID (FK) | Yes | Primary person |
| vehicle_identity_id | UUID (FK) | Yes | Vehicle used |
| boat_identity_id | UUID (FK) | Yes | Boat launched/retrieved |
| arrival_time | TIMESTAMP | No | First detection (typically parking cam) |
| departure_time | TIMESTAMP | Yes | Last detection; null if still on-site |
| violation_ids | UUID[] | Yes | Array of violation IDs during this visit |
| track_ids | UUID[] | No | All tracks comprising this visit |
| camera_timeline | JSONB | No | `[{camera, enter_time, exit_time}, ...]` |

---

## 4. Identity Resolution Strategy

Identity resolution operates on three timescales, each using different signals.

### 4.1 Timescale Layers

| Timescale | Mechanism | Signal | Limitation |
|-----------|-----------|--------|------------|
| **Frame-to-frame** | BoT-SORT tracker | Motion prediction + IoU overlap | Fails on occlusion, camera exit |
| **Within session** | OSNet / Boat ReID | Body/boat appearance embedding | Fails when clothing/appearance changes |
| **Long-term** | InsightFace / ALPR / OCR | Face, license plate, registration | Requires visible face/plate/hull number |

### 4.2 Association Chain

The most powerful identification comes from building association chains across entity types:

```
Person (face) ↔ Vehicle (plate: FL-ABC1234) ↔ Trailer (visual) ↔ Boat (reg: FL1234AB)
```

If the face is seen once linking a person to a vehicle, every subsequent appearance of that plate implicitly identifies the person — even if their face is never visible again.

### 4.3 HDBSCAN Auto-Clustering

Unknown faces are automatically clustered by visual similarity using HDBSCAN on the 512-dimensional face embedding space. Each cluster becomes an "Unknown #N" identity that can be reviewed and labeled in Groundtruth Studio. HDBSCAN is preferred over DBSCAN because it handles varying density and does not require a fixed epsilon parameter.

---

## 5. Container Architecture

All services run as Docker containers on Artemis (192.168.50.20) with GPU access via NVIDIA Container Toolkit. Local-only access, no external exposure.

| Container | GPU | Purpose | Connects To |
|-----------|-----|---------|-------------|
| **frigate** | Yes | NVR + camera feeds | MQTT, double-take |
| **argus-detector** | Yes | YOLO object detection service | PostgreSQL, frigate events |
| **tracker-service** | Yes | BoT-SORT + OSNet ReID | argus-detector, PostgreSQL |
| **insightface-api** | Yes | Face detection + embedding API | PostgreSQL, tracker-service |
| **compreface-*** | Yes | Face recognition (managed stack) | double-take, groundtruth-studio |
| **double-take** | No | Frigate→CompreFace bridge | frigate, compreface |
| **groundtruth-studio** | No | Annotation, MLOps, QA | PostgreSQL, compreface, model registry |
| **ollama** | Yes | LLM inference | open-webui |
| **open-webui** | No | Chat interface | ollama, compreface API |
| **postgresql** | No | Shared database | All services |
| **nginx** | No | Reverse proxy + SSL | All web services |

---

## 6. Training Data Sources

### 6.1 Recommended Datasets

| Model | Dataset | Size | Notes |
|-------|---------|------|-------|
| **Argus** | COCO 2017 | 118K train images | Person, car, truck, bus, boat, motorcycle classes |
| | Open Images V7 | 9M images | Extended vehicle categories |
| **Face** | COCO-WholeBody | COCO extension | Face, hand, body, foot bounding boxes |
| | CrowdHuman | 15K train images | Head, visible body, full body boxes |
| | WIDER Face | 32K images | Gold standard face detection |
| **Person ReID** | Market-1501 | 32K images | Standard ReID benchmark |
| | DukeMTMC-reID | 36K images | Multi-camera person ReID |
| **Vehicle ID** | CompCars | 137K images | 1,716 make/model classes |
| | VMMRdb | 291K images | 9,170 make/model classes |
| **Boats** | ABOships | Surveillance images | Ship detection from fixed cameras |
| | Singapore Maritime (SMD) | Shore-based video | Similar vantage to marina cameras |
| **Boat ReID** | Custom (Groundtruth Studio) | Build from marina feeds | Fine-tune OSNet on annotated boat pairs |
| **Power Loading** | Custom (Groundtruth Studio) | Build from ramp cameras | Violation-specific with temporal context |

---

## 7. Groundtruth Studio Integration

Groundtruth Studio serves as the unified MLOps platform for the entire system.

### 7.1 Workflows

- **Video annotation:** Scenario-based bounding box annotation with temporal + keyframe hybrid approach
- **Face labeling:** Pull unknown clusters from CompreFace, assign/correct identities, push back via API
- **False positive review:** Flag misdetections from production for retraining
- **Model training:** Configure experiments, select base models, run training jobs on GPU
- **Evaluation:** Automated testing against curated test sets with A/B comparison
- **Deployment:** Versioned model rollout with canary testing and rollback
- **Feedback loop:** Production errors automatically queued for annotation and retraining

### 7.2 Boat ReID Data Collection

No large-scale boat ReID dataset exists, so Groundtruth Studio will build one from marina camera feeds. The workflow involves annotating the same boat across different cameras and timepoints, then training a fine-tuned OSNet model. Key distinguishing features: hull color/pattern, boat type, size, T-top/bimini presence, and motor configuration.

---

## 8. Implementation Phases

### Phase 1: Foundation
- Deploy Argus (YOLO) for person/vehicle/boat detection on camera feeds
- Integrate BoT-SORT for basic within-camera tracking
- Set up PostgreSQL schema (identities, tracks, sightings tables)
- Begin collecting COCO-filtered training data via FiftyOne

### Phase 2: Person Identification
- Deploy InsightFace for face embedding generation
- Deploy CompreFace + Double-Take for automated face capture/matching
- Implement HDBSCAN clustering for unknown face grouping
- Add OSNet ReID for person tracking through occlusions
- Build Groundtruth Studio face labeling workflow

### Phase 3: Vehicle & Boat Identification
- Train vehicle make/model classifier on CompCars/VMMRdb
- Integrate ALPR for license plate OCR
- Train boat registration OCR on custom annotated data
- Begin building boat ReID dataset from marina feeds
- Fine-tune OSNet for boat ReID

### Phase 4: Association & Intelligence
- Implement association chain engine (person↔vehicle↔trailer↔boat)
- Deploy cross-camera handoff with camera topology
- Build visit aggregation logic
- Integrate power loading detection with full association chain for enforcement
- Deploy violation reporting with complete evidence packages

### Phase 5: Continuous Improvement
- Production feedback loops via Groundtruth Studio
- Automated retraining schedules per model
- A/B testing for model versions
- Transit time learning for camera topology optimization
- Expand detection capabilities beyond power loading
