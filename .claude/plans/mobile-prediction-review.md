# Mobile-First Prediction Review — Implementation Plan

## Vision
A Tinder-style card swiping interface for rapidly triaging AI predictions on mobile. Swipe right to approve, left to reject, up to skip. Full-screen keyframe with bbox overlay, rejection feedback bottom sheet, progress tracking, and undo.

## Design Summary (from UX Spec)

**Core interaction:** Full-screen keyframe image on black background, bbox overlay with cyan stroke + dim-outside-spotlight effect. Swipe right = approve, left = reject, up = skip. Buttons as fallback. Bottom sheet for optional rejection feedback. Single-level undo. Progress bar. Session summary on completion.

**Color palette:** Black (#000) background, cyan (#06B6D4) bbox, green (#16A34A) approve, red (#DC2626) reject, teal (#0D9488) accent/progress.

---

## Current State

- **Frontend:** `prediction-review.js` — card-based list in annotation page sidebar, individual approve/reject/correct buttons, keyboard shortcuts (A/R/N)
- **Backend:** `POST /api/ai/predictions/<id>/review` — individual review endpoint (no batch)
- **Data model:** `ai_predictions` table has `review_status`, `reviewed_by`, `reviewed_at`, `review_notes`, `corrected_tags`, `corrected_bbox` — all fields needed already exist
- **Mobile CSS:** Good responsive foundation in `annotate.css` with touch targets, landscape mode, panel switching
- **No batch review endpoint exists**

---

## Implementation Phases

### Phase 1: Backend — Batch Review Endpoint
**File:** `app/api.py`

Add `POST /api/ai/predictions/batch-review`:
```
Request: {
  "reviews": [
    { "prediction_id": 123, "action": "approve" },
    { "prediction_id": 456, "action": "reject", "notes": "false_positive" },
    ...
  ],
  "reviewer": "studio_user"
}
Response: { "success": true, "reviewed": 5, "annotations_created": 3 }
```

Also add `GET /api/ai/predictions/review-queue`:
- Returns predictions across all videos (or filtered by video/model/confidence)
- Includes full thumbnail URL and bbox data
- Sorted by confidence ascending (review lowest confidence first)
- Paginated with cursor-based pagination for infinite scroll / preloading

**File:** `app/database.py`
- Add `batch_review_predictions(reviews)` method
- Add `get_review_queue(filters, limit, offset)` method

### Phase 2: New Mobile Review Page
**New file:** `templates/prediction_review_mobile.html`
**New file:** `static/js/prediction-review-mobile.js`
**New file:** `static/css/prediction-review-mobile.css`

New route: `/review` (or `/review?video_id=X` for single-video review)

#### 2a: Page Shell & Entry Screen (S0)
- Video selection list with review progress rings
- Filter chips: All Models, High Confidence, Low Confidence
- Summary: "147 pending across 12 videos"
- Tap video to enter review flow

#### 2b: Card Review Screen (S1)
- Full-screen black background
- Keyframe image edge-to-edge, top 65% of viewport
- SVG bbox overlay: cyan stroke, dim outside region
- Metadata strip: class name, confidence %, model name
- Action buttons in thumb zone (bottom 25%): Reject (X) / Skip (>>) / Approve (check)
- Progress bar with count
- Minimal top overlay: back arrow, video name

#### 2c: Swipe Gesture Engine
- Touch/pointer event handling for card drag
- Horizontal threshold: 80px for commit
- Rotation tilt effect (max 3 degrees)
- Color glow on screen edges (green right, red left)
- Spring physics exit animation (CSS transitions or requestAnimationFrame)
- Haptic feedback via `navigator.vibrate()` where supported
- Preload next 3 keyframe images

#### 2d: Rejection Feedback Sheet
- Bottom sheet sliding up after reject swipe
- Quick-select chips (2-column grid):
  - Wrong location, Bad size, Wrong class, False positive, Missed detection, Other
- Optional free-text note for "Other"
- "Skip feedback" (dismiss) + "Done" buttons
- Swipe down to dismiss (= skip feedback)

#### 2e: Undo System
- Single-level undo stack
- Reverse animation of last card
- Undo button appears after first action, bottom-left
- Calls review API to revert (set back to pending)

#### 2f: Session Summary (S2)
- Animated completion ring
- Approved/Rejected/Skipped breakdown cards
- "Back to Videos" + "Review Skipped" buttons

### Phase 3: Optimistic Updates & Offline Queue
- Local state for immediate UI response
- Batch sync to server every 5 reviews or 10 seconds
- Queue reviews in localStorage if offline
- Non-intrusive "Sync paused" banner if offline
- Retry queue on reconnect

### Phase 4: Navigation & Filtering
- Filter modal: model, confidence range slider, class, review status
- Cross-video navigation via progress bar swipe or overflow menu
- Deep links: `/review?video_id=X&model=person-face-v1`

### Phase 5: Polish
- Pinch-to-zoom on keyframe for bbox inspection
- Double-tap to 2x zoom centered on bbox
- Auto-zoom for small bboxes (<5% of image area)
- Reduced-motion mode (crossfade instead of spring animations)
- Landscape optimization
- Overlapping bbox detection (show grouped when IoU > 0.5)

---

## File Manifest

| File | Action | Description |
|------|--------|-------------|
| `app/api.py` | Modify | Add batch-review + review-queue endpoints |
| `app/database.py` | Modify | Add batch review + queue query methods |
| `templates/prediction_review_mobile.html` | Create | New mobile review page template |
| `static/js/prediction-review-mobile.js` | Create | Swipe engine, card management, review logic |
| `static/css/prediction-review-mobile.css` | Create | Full-screen dark UI, animations, responsive layout |

---

## Estimated Scope
- Phase 1 (Backend): ~150 lines
- Phase 2 (Core UI): ~800 lines JS, ~400 lines CSS, ~100 lines HTML
- Phase 3 (Offline): ~100 lines
- Phase 4 (Filters): ~200 lines
- Phase 5 (Polish): ~200 lines

## Dependencies
- No new libraries needed — pure vanilla JS with touch events
- Existing thumbnail infrastructure serves keyframe images
- Existing bbox data in predictions table
