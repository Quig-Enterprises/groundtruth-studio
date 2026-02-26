# Alfred Photo Catalog Identity Sync

**Date:** 2026-02-25
**Status:** Proposed
**Goal:** Use face identities built from Alfred's photo catalog to recognize people on security camera feeds processed by GroundTruth Studio, without storing any photos from Alfred on Artemis.

---

## Problem

GroundTruth Studio's person recognizer maintains a reference gallery of face embeddings linked to named identities. Currently, this gallery is built only from manually tagged video annotations within Studio. Meanwhile, Alfred's photo catalog (`http://alfred:8082`) has AI face detection running across a large photo collection with many labeled identities. These two identity pools are disconnected.

## Solution

Sync identity metadata and face embeddings (not photos) from Alfred to Studio, making Alfred's labeled faces available as reference identities for camera feed recognition.

**Privacy constraint:** No photos from Alfred are stored or cached on Artemis. Only 512-float embedding vectors, identity names, and metadata cross the wire.

---

## Architecture

```
Alfred Photo Catalog                    GroundTruth Studio (Artemis)
┌──────────────────┐                    ┌──────────────────────────┐
│                  │                    │                          │
│  Photos (stays)  │                    │  identities table        │
│  Person DB IDs   │───identity sync───▶│    external_id           │
│  Person names    │   (names + keys)   │    source_system         │
│                  │                    │    name                  │
└────────┬─────────┘                    │                          │
         │                              │  embeddings table        │
         │ photos fetched               │    vector (512 floats)   │
         │ temporarily by worker        │    is_reference = true   │
         ▼                              │    source_image_path=NULL│
┌──────────────────┐                    │                          │
│ face_embed_worker│                    │  person_recognizer.py    │
│ (GPU machine)    │───embeddings──────▶│    reference gallery     │
│                  │   (vectors only)   │    cosine matching       │
│ InsightFace      │                    │    camera feed IDs       │
│ buffalo_l        │                    └──────────────────────────┘
└──────────────────┘
     Photos are loaded into memory,
     processed, and discarded.
     Never sent to Artemis.
```

---

## Data Model Changes

### identities table — new columns

```sql
ALTER TABLE identities ADD COLUMN IF NOT EXISTS external_id VARCHAR(255);
ALTER TABLE identities ADD COLUMN IF NOT EXISTS source_system VARCHAR(50);

CREATE UNIQUE INDEX IF NOT EXISTS idx_identities_external
  ON identities(source_system, external_id)
  WHERE external_id IS NOT NULL;
```

| Column | Type | Purpose |
|--------|------|---------|
| `external_id` | `VARCHAR(255)` | Stable ID from the source system (e.g., Alfred's person DB ID) |
| `source_system` | `VARCHAR(50)` | Source identifier (e.g., `alfred_catalog`, `other_system`) |

- The `(source_system, external_id)` unique index ensures one Studio identity per source person.
- The same `external_id` value from different `source_system` values creates separate identities.
- Existing identities (video annotations, clustering) have `external_id = NULL` and are unaffected.
- If an identity is renamed on the source, Studio updates the `name` column on the matching row. Embeddings don't change.

---

## Studio API Endpoints

### 1. POST /api/identities/sync — Identity metadata sync

Pushes identity name/key updates from a source system. Handles creates, renames, and deletes in bulk.

**Request:**
```json
{
  "source_system": "alfred_catalog",
  "identities": [
    {"external_id": "42", "name": "John Smith", "action": "upsert"},
    {"external_id": "87", "name": "Jane Doe", "action": "upsert"},
    {"external_id": "15", "action": "delete"}
  ]
}
```

**Response:**
```json
{
  "success": true,
  "created": 1,
  "updated": 1,
  "deleted": 1,
  "errors": []
}
```

**Behavior:**

| Action | Logic |
|--------|-------|
| `upsert` | Find identity by `(source_system, external_id)`. If not found, create with `identity_type='person'`. If found, update `name` if changed. |
| `delete` | Delete the identity row. Cascades to embeddings via existing FK. |

**Validation:**
- `source_system` required, max 50 chars
- `external_id` required per identity, max 255 chars
- `name` required for `upsert` action
- Rejects any payload containing image data, URLs, or file paths

### 2. POST /api/identities/ingest-embeddings — Embedding ingestion

Called by the face_embed_worker after extracting faces from source photos. No photo data in the payload.

**Request:**
```json
{
  "source_system": "alfred_catalog",
  "results": [
    {
      "external_id": "42",
      "name": "John Smith",
      "faces": [
        {"embedding": [0.12, -0.34, ...], "confidence": 0.98},
        {"embedding": [0.11, -0.33, ...], "confidence": 0.95}
      ]
    },
    {
      "external_id": "87",
      "name": "Jane Doe",
      "faces": [
        {"embedding": [0.22, -0.45, ...], "confidence": 0.91}
      ]
    }
  ]
}
```

**Response:**
```json
{
  "success": true,
  "identities_resolved": 2,
  "embeddings_stored": 3
}
```

**Behavior:**
1. For each result, find-or-create identity by `(source_system, external_id)`.
2. If identity is new, set `name`, `identity_type='person'`.
3. If identity exists and name changed, update `name`.
4. Insert each face embedding with:
   - `identity_id` = resolved identity
   - `embedding_type` = `'face'`
   - `is_reference` = `true`
   - `source_image_path` = `NULL` (no photo reference)
   - `confidence` = from payload
5. Duplicate embeddings (same vector for same identity) are skipped.

**Validation:**
- Embedding must be a list of floats (expected: 512 elements for InsightFace buffalo_l)
- Confidence must be 0.0–1.0
- Rejects any field named `image`, `photo`, `thumbnail`, `url`, `path`, or `file`

### 3. GET /api/identities/external — List external identities (optional, for UI)

Returns identities that have an `external_id`, grouped by source system.

**Response:**
```json
{
  "success": true,
  "identities": [
    {
      "identity_id": "uuid-here",
      "external_id": "42",
      "source_system": "alfred_catalog",
      "name": "John Smith",
      "embedding_count": 15,
      "first_seen": "2026-02-20T10:00:00Z",
      "last_seen": "2026-02-25T14:30:00Z"
    }
  ]
}
```

---

## Face Embed Worker Changes

The `face_embed_worker.py` currently posts results to `POST /api/training/jobs/<id>/complete`. Changes needed:

1. **Accept identity metadata from Alfred's catalog API.** When fetching photos for a job, also fetch the person ID and name associated with each photo.

2. **Post to the new ingest-embeddings endpoint.** After extracting face embeddings, group results by person and post to `POST /api/identities/ingest-embeddings` with `external_id` and `name` from Alfred.

3. **No photo bytes in the payload.** The worker loads photos into memory for InsightFace processing, then discards them. Only the resulting 512-float vectors are sent to Studio.

### Modified flow

```
1. Worker polls Studio for face_detect_embed job
2. Job config contains batch_id or photo_ids
3. Worker fetches photo metadata from Alfred (includes person_id, person_name)
4. Worker fetches photo bytes from Alfred (into memory only)
5. Worker runs InsightFace buffalo_l → extracts face embeddings
6. Worker discards photo bytes from memory
7. Worker groups embeddings by person_id
8. Worker POSTs to Studio: /api/identities/ingest-embeddings
   {source_system: "alfred_catalog", results: [{external_id, name, faces: [{embedding, confidence}]}]}
9. Worker reports job complete to Studio (existing flow)
```

---

## Client-Side Integration Guide (Alfred)

### Prerequisites

- Alfred's photo catalog has a person database with stable numeric/UUID IDs per person
- Alfred can make HTTP requests to Studio at `http://192.168.50.20:5050`
- The face_embed_worker runs on a GPU machine with network access to both Alfred and Studio

### Option A: Identity Sync via API (for name changes, new people, deletions)

Call Studio's sync endpoint directly from Alfred whenever identity metadata changes. This is lightweight — no embeddings, just names and keys.

**Example: sync all labeled identities**

```python
import requests

STUDIO_URL = "http://192.168.50.20:5050"

# Fetch all labeled people from Alfred's database
people = get_all_labeled_people()  # your Alfred DB query

payload = {
    "source_system": "alfred_catalog",
    "identities": [
        {"external_id": str(p.id), "name": p.name, "action": "upsert"}
        for p in people
    ]
}

resp = requests.post(f"{STUDIO_URL}/api/identities/sync", json=payload, timeout=30)
print(resp.json())
# {"success": true, "created": 12, "updated": 3, "deleted": 0}
```

**Example: handle a rename**

```python
# When someone renames a person in Alfred's UI:
payload = {
    "source_system": "alfred_catalog",
    "identities": [
        {"external_id": "42", "name": "Jonathan Smith", "action": "upsert"}
    ]
}
requests.post(f"{STUDIO_URL}/api/identities/sync", json=payload)
```

**Example: handle a deletion**

```python
# When someone deletes a person from Alfred:
payload = {
    "source_system": "alfred_catalog",
    "identities": [
        {"external_id": "42", "action": "delete"}
    ]
}
requests.post(f"{STUDIO_URL}/api/identities/sync", json=payload)
```

### Option B: Embedding Ingestion via Worker (for face processing)

The face_embed_worker handles this automatically. To trigger processing:

1. **Submit a job to Studio** with the photo IDs or batch ID to process:

```python
payload = {
    "job_type": "face_detect_embed",
    "config": {
        "source_system": "alfred_catalog",
        "photo_ids": ["photo_001", "photo_002", ...],
        # OR
        "batch_id": "batch_2026_02_25"
    }
}
requests.post(f"{STUDIO_URL}/api/training/submit", json=payload)
```

2. **The worker picks up the job**, fetches photos from Alfred's catalog API, runs InsightFace, and posts embeddings to Studio with the person identity metadata.

3. **Alfred's catalog API must provide person metadata with photos.** The worker needs to know which person each photo belongs to. Alfred's API should return:

```json
// GET /api/photos/{photo_id}
{
  "photo_id": "photo_001",
  "person_id": 42,
  "person_name": "John Smith",
  "file_url": "/api/photos/photo_001/file"
}

// OR for batch:
// GET /api/batches/{batch_id}/photos
{
  "photos": [
    {"photo_id": "photo_001", "person_id": 42, "person_name": "John Smith"},
    {"photo_id": "photo_002", "person_id": 42, "person_name": "John Smith"},
    {"photo_id": "photo_003", "person_id": 87, "person_name": "Jane Doe"}
  ]
}
```

### Option C: Direct Embedding Push (no worker needed)

If Alfred already has its own face embedding pipeline (runs InsightFace locally), it can push embeddings directly to Studio without the worker:

```python
# Alfred already extracted embeddings from its photos
payload = {
    "source_system": "alfred_catalog",
    "results": [
        {
            "external_id": "42",
            "name": "John Smith",
            "faces": [
                {"embedding": embedding_vector.tolist(), "confidence": 0.98},
            ]
        }
    ]
}
requests.post(f"{STUDIO_URL}/api/identities/ingest-embeddings", json=payload)
```

This is useful if Alfred already computes embeddings and you want to skip the worker entirely.

### Recommended Integration Pattern

1. **Initial bulk sync:** Run the identity sync once to push all labeled people from Alfred to Studio.
2. **Initial embedding job:** Submit a face_detect_embed job covering all labeled photos. Worker processes them and populates reference embeddings.
3. **Ongoing — names:** Hook Alfred's person rename/delete events to call `/api/identities/sync`.
4. **Ongoing — new photos:** When new labeled photos are added to Alfred, submit a new face_detect_embed job for those photos.

### Cron-based sync (alternative to event hooks)

If hooking into Alfred's events isn't practical, run a periodic sync:

```bash
# crontab on Alfred
# Sync identity names every 15 minutes
*/15 * * * * /opt/alfred/scripts/sync_identities_to_studio.py

# Submit new photo batches daily
0 2 * * * /opt/alfred/scripts/submit_face_embed_batch.py
```

---

## Person Recognizer — No Changes Needed

The existing `person_recognizer.py` reference gallery query already consumes what this system produces:

```sql
SELECT e.identity_id, e.vector, i.name
FROM embeddings e
JOIN identities i ON e.identity_id = i.identity_id
WHERE e.is_reference = true AND e.embedding_type = 'face'
AND i.name IS NOT NULL AND i.name NOT LIKE 'Unknown%'
```

Once Alfred's embeddings land with `is_reference=true` under a named identity, they join the gallery on the next cache refresh (5-minute TTL).

---

## Privacy Guarantees

| Guarantee | Enforcement |
|-----------|-------------|
| No photos stored on Artemis | `source_image_path` = NULL for all Alfred embeddings |
| No photo bytes transmitted to Studio | Ingest endpoint rejects image/file fields |
| No thumbnail URLs stored | No URL/path fields in the schema for external images |
| Embeddings are one-way | 512-float vectors cannot be reversed to reconstruct faces |
| Worker discards photos after processing | Photos loaded into memory, processed by InsightFace, then garbage collected |

---

## What This Does NOT Do

- **No UI changes.** Existing Person Manager shows Alfred identities naturally.
- **No bidirectional sync.** Alfred is the source of truth for its identities.
- **No automatic cross-source merging.** If the same person exists from Alfred and from video annotations, they remain separate until manually merged.
- **No photo browsing.** Studio cannot display Alfred's photos — by design.

---

## Implementation Scope

| Component | File(s) | Change |
|-----------|---------|--------|
| DB migration | `app/schema.py` | Add `external_id`, `source_system` columns + index |
| Identity sync API | `app/routes/identities.py` (new) | `POST /api/identities/sync` |
| Embedding ingest API | `app/routes/identities.py` (new) | `POST /api/identities/ingest-embeddings` |
| External identity listing | `app/routes/identities.py` (new) | `GET /api/identities/external` |
| Blueprint registration | `app/api.py` | Register `identities_bp` |
| Worker enhancement | `worker/face_embed_worker.py` | Fetch person metadata, post to ingest endpoint |
| Person recognizer | `app/person_recognizer.py` | No changes |
