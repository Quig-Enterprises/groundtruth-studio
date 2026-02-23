# GroundTruth Studio — Detection Pipeline

Living document. Last updated: 2026-02-19.

---

## 1. Pipeline Architecture

Full flow from Frigate event to human review:

```
Frigate MQTT event
  |
  v
frigate_ingester.py  ─────────────────────────────────────────────────────┐
  - Subscribes to MQTT                                                     |
  - Captures snapshot from Frigate                                         |
  - Creates video record in DB                                             |
  |                                                                        |
  v                                                                        |
vehicle_detect_runner.py  (Stage 1: YOLO-World pre-screen, GPU)            |
  - Runs YOLO-World on snapshot                                            |
  - Detects vehicles, persons, other objects                               |
  |                                                                        |
  ├─ Vehicles found ──> POST /api/ai/predictions/batch                     |
  |                       - Routes to review queue                         |
  |                       - review_status = 'pending'                      |
  |                                                                        |
  └─ Persons found ──> auto_detect_runner.py  (Stage 2: person-face-v1, CPU)
                          - Runs person/face detection model               |
                          |                                                |
                          └─ Faces found ──> person_recognizer.py  (Stage 3)
                                               - Sends crops to InsightFace API
                                               - Compares embeddings to gallery
                                               - Match? → person_identification prediction

Background jobs (pipeline_worker.py, every 5 min):
  - face_clustering.py    — HDBSCAN clustering of unknown face embeddings
  - visit_builder.py      — Aggregates predictions into visits

Post-ingestion:
  - prediction_grouper.py — Spatial/temporal grouping of predictions
  - vlm_reviewer.py       — LLaMA 3.2 Vision false-positive filtering
  - track_builder.py      — Track matching for auto-approve/reject
```

---

## 2. Key Files

| File | Purpose |
|------|---------|
| `app/frigate_ingester.py` | MQTT subscriber, snapshot capture, pipeline entry point |
| `app/vehicle_detect_runner.py` | YOLO-World pre-screen + vehicle detection (GPU) |
| `app/auto_detect_runner.py` | Person/face detection using person-face-v1 model (CPU) |
| `app/person_recognizer.py` | Face recognition via InsightFace embedding API |
| `app/face_clustering.py` | HDBSCAN clustering of unknown face embeddings |
| `app/pipeline_worker.py` | Background periodic jobs (clustering, visits, etc.) |
| `app/visit_builder.py` | Visit aggregation from predictions |
| `app/prediction_grouper.py` | Spatial/temporal grouping of predictions |
| `app/vlm_reviewer.py` | VLM-based false positive filtering (llama3.2-vision) |
| `app/track_builder.py` | Track matching for auto-approve/reject |
| `app/routes/predictions.py` | API endpoints for predictions, batch submission |
| `app/repos/prediction_mixin.py` | Database operations for predictions |
| `app/db_connection.py` | PostgreSQL connection pool |
| `app/schema.py` | Database schema and migrations |
| `app/ecoeye_sync.py` | EcoEye Alert Relay integration (manual import) |
| `app/recover_detections.py` | Recovery script for failed detections |

---

## 3. Database Dependencies

Critical columns and constraints the pipeline depends on.

### ai_predictions table

- `parent_prediction_id` — Links sub-detections (faces, persons) to the source vehicle/person prediction.
- `review_status` CHECK constraint — Must include: `'pending'`, `'approved'`, `'rejected'`, `'processing'`. The `'processing'` value is used as an intermediate state during batch submission before predictions land in the review queue.

### Supporting tables

| Table | Purpose |
|-------|---------|
| `prediction_groups` | Spatial grouping of related predictions |
| `cross_camera_links` | Cross-camera matching of the same object |
| `person_embeddings` | Face recognition gallery (known face embeddings) |

---

## 4. Services

| Service | Entry Point | Port | Notes |
|---------|-------------|------|-------|
| `groundtruth-studio` | `api.py` | 5050 | Main API server |
| `groundtruth-frigate` | `frigate_ingester.py` | — | MQTT subscriber, long-running |
| `groundtruth-pipeline` | `pipeline_worker.py` | — | Periodic background jobs |
| `insightface-api` | (separate project) | 5060 | Face embedding service |

All managed via systemd.

---

## 5. Regression Testing Checklist

### Schema & Migrations

- [ ] All columns referenced in INSERT/UPDATE statements exist in the database
- [ ] All CHECK constraints include all values used by the application code
- [ ] Run `schema.py` migrations against a test database after schema changes
- [ ] Test the `/api/ai/predictions/batch` endpoint with a sample prediction payload after any schema change

### Pipeline End-to-End

- [ ] Frigate MQTT -> ingester -> vehicle detection -> batch submission -> review queue (full chain)
- [ ] Person detection -> face detection -> face recognition chain fires when person detected
- [ ] Predictions appear in review queue with correct review_status ('pending', not stuck in 'processing')
- [ ] VLM reviewer runs and tags predictions with vlm_* corrected_tags
- [ ] Prediction grouping creates groups for spatially similar detections
- [ ] Track matching auto-approves/rejects known objects

### Review Queue

- [ ] All review filters show correct counts (All, Classify, Conflicts, Cross-Camera, Clusters)
- [ ] Classify queue shows ALL pending items (not just ungrouped subset)
- [ ] "Not a vehicle" rule-out actually removes from classify queue (review_status updated from 'approved')
- [ ] Classification sync fires immediately (not lost on page navigation)
- [ ] Approve/reject works for predictions with review_status='approved' (not just 'pending')
- [ ] Confidence dropdown filters counts and items correctly

### Services Health

- [ ] groundtruth-studio (api.py) is running on port 5050
- [ ] groundtruth-frigate (frigate_ingester.py) is running and connected to MQTT
- [ ] groundtruth-pipeline (pipeline_worker.py) is running periodic jobs
- [ ] insightface-api is running on port 5060
- [ ] No recurring errors in frigate-ingester.log
- [ ] No recurring errors in pipeline_worker.log

### Database Integrity

- [ ] visit_builder.py uses get_cursor() without cursor_factory (it's built-in)
- [ ] All batch_review_predictions queries use review_status IN ('pending', 'approved') not just 'pending'
- [ ] beforeunload handler flushes pending sync queues

### Recovery

- [ ] recover_detections.py can re-process videos with no predictions
- [ ] Dedup in batch endpoint prevents duplicate predictions on re-run

---

## 6. Known Issues & Fixes History

| Date | Issue | Root Cause | Fix |
|------|-------|------------|-----|
| 2026-02-19 | No new detections generated | `parent_prediction_id` column missing from ai_predictions; `review_status` CHECK constraint missing 'processing' | Applied ALTER TABLE + updated CHECK constraint |
| 2026-02-19 | visit_builder recurring error | `get_cursor()` called with `cursor_factory` kwarg it doesn't accept | Removed kwarg, get_cursor() already uses RealDictCursor |
| 2026-02-18 | "Not a vehicle" doesn't remove from classify queue | batch_review_predictions WHERE clause only matched review_status='pending', classify items are 'approved' | Changed to IN ('pending', 'approved') |
| 2026-02-18 | Classify queue shows only 2 of 13 items | Grouped mode query excludes predictions with group_id from ungrouped UNION | Always use ungrouped query for classify mode |
| 2026-02-18 | VLM text overrides classify guidance | VLM suggested class text replaced guidance entirely in classify mode | Made VLM info additive in classify mode |
| 2026-02-18 | Classifications lost on page navigation | 3-second batching timer, no beforeunload handler | Added beforeunload+visibilitychange handlers, immediate sync |

---

## 7. Quick Health Check Commands

```bash
# Check all services
systemctl is-active groundtruth-studio groundtruth-frigate groundtruth-pipeline insightface-api

# Check for pipeline errors (last hour)
grep -c "ERROR" /opt/groundtruth-studio/frigate-ingester.log | tail -1
tail -20 /opt/groundtruth-studio/pipeline-worker.log

# Test batch endpoint
curl -s -X POST 'http://localhost:5050/api/ai/predictions/batch' \
  -H 'Content-Type: application/json' -H 'X-Auth-Role: admin' \
  -d '{"video_id": 1, "model_name": "test", "model_version": "1.0", "batch_id": "healthcheck", "predictions": [{"prediction_type": "keyframe", "confidence": 0.5, "timestamp": 0.0, "scenario": "vehicle_detection", "tags": {}, "bbox": {"x": 0, "y": 0, "width": 10, "height": 10}, "inference_time_ms": 1}]}'

# Count videos without predictions
sudo -u postgres psql groundtruth_studio -c "SELECT COUNT(*) FROM videos WHERE id NOT IN (SELECT DISTINCT video_id FROM ai_predictions)"

# Check review queue counts
curl -s 'http://localhost:5050/api/ai/predictions/review-filter-counts' | python3 -m json.tool
```
