# AI Prediction & Human Review System

## Overview

This system enables AI models to submit predictions for human review, creating a feedback loop that improves model accuracy over time. Predictions are stored separately from human annotations until approved, then merged into the training dataset with equal weight.

## Architecture

```
┌─────────────────┐         ┌──────────────────┐         ┌─────────────────┐
│   AI Training   │────────▶│  Groundtruth     │────────▶│  Human          │
│   Server        │  POST   │  Studio API      │  Review │  Reviewer       │
│                 │         │                  │         │                 │
└─────────────────┘         └──────────────────┘         └─────────────────┘
                                     │                            │
                                     ▼                            ▼
                            ┌──────────────────┐       ┌──────────────────┐
                            │  ai_predictions  │       │  Approved =      │
                            │  (pending)       │       │  Training Data   │
                            └──────────────────┘       └──────────────────┘
```

## Database Schema

### New Table: `ai_predictions`

```sql
CREATE TABLE ai_predictions (
    id SERIAL PRIMARY KEY,
    video_id INTEGER NOT NULL,
    model_name TEXT NOT NULL,           -- e.g., "yolov8-boat-detector-v1.2"
    model_version TEXT NOT NULL,        -- e.g., "1.2.0"
    prediction_type TEXT NOT NULL,      -- "keyframe" or "time_range"
    confidence REAL NOT NULL,           -- 0.0 to 1.0

    -- Temporal data
    timestamp REAL,                     -- For keyframe predictions
    start_time REAL,                    -- For time range predictions
    end_time REAL,                      -- For time range predictions

    -- Spatial data (for keyframe predictions)
    bbox_x INTEGER,
    bbox_y INTEGER,
    bbox_width INTEGER,
    bbox_height INTEGER,

    -- Classification data
    scenario TEXT NOT NULL,             -- Scenario ID from annotation-scenarios.js
    predicted_tags TEXT NOT NULL,       -- JSON blob of predicted tags

    -- Review status
    review_status TEXT DEFAULT 'pending', -- pending|approved|rejected|needs_correction
    reviewed_by TEXT,                   -- Username/ID of reviewer
    reviewed_at TIMESTAMP,
    review_notes TEXT,

    -- Correction data (if human modifies prediction)
    corrected_tags TEXT,                -- JSON blob of human corrections
    correction_type TEXT,               -- bbox_adjusted|tags_corrected|both

    -- Metadata
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    batch_id TEXT,                      -- For batch processing tracking
    inference_time_ms INTEGER,          -- How long inference took

    FOREIGN KEY (video_id) REFERENCES videos(id)
);

CREATE INDEX idx_predictions_video ON ai_predictions(video_id);
CREATE INDEX idx_predictions_status ON ai_predictions(review_status);
CREATE INDEX idx_predictions_model ON ai_predictions(model_name, model_version);
CREATE INDEX idx_predictions_confidence ON ai_predictions(confidence);
```

## API Endpoints

### 1. Submit Predictions (AI Server → Groundtruth Studio)

**POST** `/api/ai/predictions/batch`

Submit a batch of predictions for a video.

**Request Body:**
```json
{
  "video_id": 123,
  "model_name": "yolov8-boat-detector",
  "model_version": "1.2.0",
  "batch_id": "batch_2026-01-19_001",
  "predictions": [
    {
      "prediction_type": "keyframe",
      "timestamp": 45.2,
      "confidence": 0.92,
      "bbox": {
        "x": 100,
        "y": 50,
        "width": 300,
        "height": 200
      },
      "scenario": "boat_operating_water",
      "tags": {
        "wake_type": "enhanced_wake",
        "boat_activity": "cruising",
        "speed_estimate": "fast"
      },
      "inference_time_ms": 45
    },
    {
      "prediction_type": "time_range",
      "start_time": 10.0,
      "end_time": 25.5,
      "confidence": 0.87,
      "scenario": "loading_boat_trailer",
      "tags": {
        "loading_direction": "loading_onto_trailer",
        "ramp_angle": "moderate"
      },
      "inference_time_ms": 120
    }
  ]
}
```

**Response:**
```json
{
  "success": true,
  "batch_id": "batch_2026-01-19_001",
  "predictions_submitted": 2,
  "prediction_ids": [456, 457]
}
```

### 2. Get Pending Predictions (Groundtruth Studio → Display)

**GET** `/api/ai/predictions/pending?video_id=123`

Get all pending predictions for review.

**Response:**
```json
{
  "success": true,
  "predictions": [
    {
      "id": 456,
      "video_id": 123,
      "model_name": "yolov8-boat-detector",
      "model_version": "1.2.0",
      "prediction_type": "keyframe",
      "timestamp": 45.2,
      "confidence": 0.92,
      "bbox": {...},
      "scenario": "boat_operating_water",
      "predicted_tags": {...},
      "created_at": "2026-01-19T10:30:00Z"
    }
  ]
}
```

### 3. Review Prediction (Human Reviewer)

**POST** `/api/ai/predictions/{prediction_id}/review`

Approve, reject, or correct a prediction.

**Request Body (Approve):**
```json
{
  "action": "approve",
  "reviewer": "user123",
  "notes": "Looks good"
}
```

**Request Body (Reject):**
```json
{
  "action": "reject",
  "reviewer": "user123",
  "notes": "False positive - not a boat"
}
```

**Request Body (Correct):**
```json
{
  "action": "correct",
  "reviewer": "user123",
  "corrections": {
    "bbox": {
      "x": 105,
      "y": 55,
      "width": 295,
      "height": 195
    },
    "tags": {
      "wake_type": "normal_wake",  // Corrected from "enhanced_wake"
      "boat_activity": "cruising"
    }
  },
  "correction_type": "both",
  "notes": "Adjusted bbox and wake type"
}
```

**Response:**
```json
{
  "success": true,
  "prediction_id": 456,
  "review_status": "approved",
  "annotation_id": 789  // If approved, ID of created training annotation
}
```

### 4. Get Model Performance Stats

**GET** `/api/ai/models/{model_name}/stats`

Get accuracy statistics for a model.

**Response:**
```json
{
  "success": true,
  "model_name": "yolov8-boat-detector",
  "model_version": "1.2.0",
  "stats": {
    "total_predictions": 1000,
    "approved": 850,
    "rejected": 100,
    "corrected": 50,
    "pending": 0,
    "approval_rate": 0.85,
    "avg_confidence_approved": 0.91,
    "avg_confidence_rejected": 0.65,
    "scenarios": {
      "boat_operating_water": {
        "total": 600,
        "approved": 550,
        "approval_rate": 0.92
      },
      "loading_boat_trailer": {
        "total": 400,
        "approved": 300,
        "approval_rate": 0.75
      }
    }
  }
}
```

## Communication Protocol

### Method 1: REST API (Recommended)

**Advantages:**
- Simple, well-understood
- Language-agnostic (Python, Go, Rust, etc.)
- Easy to test with curl/Postman
- Built-in error handling

**Implementation:**
```python
# AI Server submits predictions
import requests

predictions = generate_predictions(video_path)

response = requests.post(
    'http://groundtruth-studio:5000/api/ai/predictions/batch',
    json={
        'video_id': video_id,
        'model_name': 'yolov8-boat-detector',
        'model_version': '1.2.0',
        'predictions': predictions
    },
    headers={'Authorization': 'Bearer YOUR_API_KEY'}
)
```

### Method 2: Message Queue (For High Volume)

**Advantages:**
- Asynchronous processing
- Built-in retry logic
- Handles network failures gracefully
- Decouples systems

**Implementation Options:**
- **RabbitMQ**: Reliable, mature, easy to deploy
- **Redis Pub/Sub**: Simple, if you already use Redis
- **Apache Kafka**: Overkill unless processing millions of predictions

**Example with RabbitMQ:**
```python
# AI Server publishes predictions
import pika

connection = pika.BlockingConnection(pika.ConnectionParameters('localhost'))
channel = connection.channel()
channel.queue_declare(queue='predictions')

channel.basic_publish(
    exchange='',
    routing_key='predictions',
    body=json.dumps(prediction_data)
)
```

### Method 3: Shared Database

**Advantages:**
- No API to maintain
- Direct data access
- Simple for same-network deployments

**Disadvantages:**
- Tight coupling
- Security concerns
- Version compatibility issues

## Video Library Access

### Option 1: Shared Network Storage (NFS/SMB)

Both servers mount the same video directory:
- Groundtruth Studio: `/var/www/html/groundtruth-studio/downloads/`
- AI Server: `/mnt/groundtruth-videos/` (same physical storage)

### Option 2: Video API Endpoint

AI server requests videos via API:

**GET** `/api/videos/{video_id}/download`

Returns video file for processing.

### Option 3: Object Storage (S3-compatible)

Both systems read/write to shared MinIO/S3 bucket:
- Groundtruth uploads: `s3://groundtruth-videos/video-123.mp4`
- AI server reads: Same URL

## Review Workflow UI

### Prediction Review Mode

When predictions exist for a video:

1. **Prediction Badge**: Show count of pending predictions
2. **Review Mode Button**: "Review AI Predictions (12 pending)"
3. **Side-by-side View**:
   - Left: AI prediction (highlighted in yellow)
   - Right: Actions (Approve / Reject / Edit)
4. **Quick Navigation**: Jump between predictions
5. **Confidence Display**: Show model confidence for each prediction

### Visual Indicators

- **Green border**: High confidence (>0.9)
- **Yellow border**: Medium confidence (0.7-0.9)
- **Orange border**: Low confidence (<0.7)
- **Dashed border**: Indicates it's a prediction, not human annotation

## Data Flow Example

1. **AI Server processes video**:
   - Downloads video or accesses via NFS
   - Runs inference (YOLOv8, etc.)
   - Generates predictions with bboxes and classifications

2. **AI Server submits batch**:
   - POSTs to `/api/ai/predictions/batch`
   - Includes confidence scores, bboxes, predicted tags

3. **Groundtruth Studio stores predictions**:
   - Saves to `ai_predictions` table with status='pending'
   - Returns acknowledgment with prediction IDs

4. **Human reviewer opens video**:
   - Sees "12 AI predictions pending review"
   - Enters review mode

5. **Human reviews each prediction**:
   - Approves good predictions → Converted to training annotations
   - Rejects bad predictions → Marked for model retraining feedback
   - Corrects close predictions → Saves corrections for fine-tuning

6. **Approved predictions become training data**:
   - Inserted into `keyframe_annotations` or `time_range_tags`
   - Tagged with `source='ai_approved'` for tracking
   - Used in next training iteration with full weight

## Security Considerations

1. **API Authentication**: Require API keys for prediction submission
2. **Rate Limiting**: Prevent abuse with rate limits
3. **Input Validation**: Validate all prediction data
4. **Reviewer Authentication**: Track who approves what
5. **Audit Log**: Log all review actions

## Metrics to Track

- **Model accuracy over time**: Approval rate by version
- **Confidence calibration**: Are high-confidence predictions actually more accurate?
- **Scenario difficulty**: Which scenarios need more training data?
- **Reviewer agreement**: Do different reviewers agree on corrections?
- **Time savings**: How much faster vs manual annotation?

## Recommended Communication Method

**For your use case**: **REST API** is recommended because:
- ✅ Simple to implement on both ends
- ✅ Shared video library via NFS handles large files
- ✅ Synchronous feedback (know immediately if submission succeeded)
- ✅ Easy to add authentication
- ✅ Can batch predictions to reduce API calls

**Upgrade to Message Queue** only if you:
- Process hundreds of videos per hour
- Need guaranteed delivery across network failures
- Want to decouple AI server availability from Groundtruth Studio
