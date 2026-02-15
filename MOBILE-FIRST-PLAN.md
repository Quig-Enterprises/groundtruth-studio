# Groundtruth Studio: Mobile-First Redesign Plan

**Created**: 2026-02-13
**Status**: Planning (do not implement until other changes land)

---

## Executive Summary

Groundtruth Studio already has reasonable responsive behavior with 3 breakpoints (1024px, 768px, 480px) and touch optimizations. However, the CSS is written desktop-first (wide styles as defaults, narrowed via `max-width` queries). This plan converts to a **mobile-first** approach: base styles target small screens, with `min-width` queries progressively enhancing for larger displays. It also addresses specific mobile UX gaps identified in the audit.

---

## Current State

### What Already Works on Mobile
- Viewport meta tag present on all pages
- Grid layouts collapse to single column
- Touch target sizes (44px minimum) on buttons
- Font size bumps on small screens
- Hover effects removed on touch devices
- Landscape orientation handling

### What Needs Improvement
| Issue | Impact | Priority |
|-------|--------|----------|
| No hamburger menu - nav links stack vertically taking excessive space | High | P0 |
| Annotation page 2-panel layout awkward on phones | High | P0 |
| Tables don't scroll well horizontally | Medium | P1 |
| Modals aren't full-screen on mobile | Medium | P1 |
| D3 camera topology graph unusable on small screens | Medium | P1 |
| Some action button groups overflow | Medium | P1 |
| Filter bars wrap poorly | Low | P2 |
| Stats grids could be more compact | Low | P2 |
| No pull-to-refresh or swipe gestures | Low | P3 |

---

## Architecture Decisions

### 1. No Framework Introduction
Keep pure custom CSS. The project has ~2,400 lines of CSS across 6 files - small enough to manage without Tailwind/Bootstrap. Adding a framework mid-project creates churn and learning curve for no benefit at this scale.

### 2. CSS Rewrite Strategy: Incremental
Do **not** rewrite all CSS at once. Convert file-by-file:
1. `style.css` (main) - highest impact
2. `annotate.css` - most complex mobile challenge
3. `scenario-workflow.css` - workflow UX
4. `tag-form.css`, `tag_manager.css`, `style-additions.css` - smaller files

### 3. Breakpoint System (Mobile-First)
```css
/* Base: 0-479px (small phones) - DEFAULT styles */

/* Small tablets / large phones */
@media (min-width: 480px) { }

/* Tablets */
@media (min-width: 768px) { }

/* Desktop */
@media (min-width: 1024px) { }

/* Wide desktop */
@media (min-width: 1400px) { }
```

### 4. CSS Custom Properties for Spacing
Introduce a small set of variables to make responsive spacing consistent:
```css
:root {
  --spacing-xs: 4px;
  --spacing-sm: 8px;
  --spacing-md: 16px;
  --spacing-lg: 24px;
  --spacing-xl: 32px;
  --container-padding: var(--spacing-md);
  --card-padding: var(--spacing-md);
  --nav-height: 56px;
}

@media (min-width: 768px) {
  :root {
    --container-padding: var(--spacing-lg);
    --card-padding: var(--spacing-lg);
  }
}

@media (min-width: 1024px) {
  :root {
    --container-padding: var(--spacing-xl);
  }
}
```

---

## Component-by-Component Plan

### Phase 1: Global Layout & Navigation (P0)

#### 1A. Hamburger Navigation Menu
**Current**: Horizontal links that stack vertically on mobile, consuming screen real estate.
**Target**: Collapsible hamburger menu on screens < 768px.

**Changes**:
- **`style.css`**: Replace the header link layout with a mobile nav pattern
  - Base (mobile): Nav links hidden by default, hamburger icon visible
  - 768px+: Nav links displayed inline, hamburger hidden
- **`templates/*.html`**: Add hamburger button element to all page headers
- **`static/js/app.js`**: Add toggle handler for hamburger menu (small addition)

**Design**:
```
Mobile (< 768px):
┌─────────────────────────┐
│ ☰ Groundtruth Studio    │  <- Fixed top bar, 56px height
├─────────────────────────┤
│ (content below)         │

When ☰ tapped:
┌─────────────────────────┐
│ ✕ Groundtruth Studio    │
├─────────────────────────┤
│ 📹 Video Library        │  <- Full-width slide-down menu
│ 🏷️ Tag Manager          │
│ 👤 Person Manager       │
│ 🔗 Camera Topology      │
│ 🤖 Training Queue       │
│ 📊 Prediction Review    │
│ ⚙️ Settings             │
│ 📦 Exports ▸            │  <- Expandable submenu for exports
└─────────────────────────┘

Desktop (≥ 768px):
┌──────────────────────────────────────────────┐
│ Groundtruth Studio  [Library] [Tags] [People]│
│                     [Topology] [Training] ... │
└──────────────────────────────────────────────┘
```

**Estimated scope**: ~80 lines CSS, ~30 lines JS, ~15 lines HTML per template

#### 1B. Container & Spacing
**Current**: `max-width: 1400px` container with fixed padding.
**Target**: Fluid container with responsive spacing via CSS variables.

**Changes**:
- **`style.css`**: Replace fixed padding with `var(--container-padding)`
- Base: 16px padding, no max-width constraint
- 1024px+: 24px padding
- 1400px+: `max-width: 1400px`, auto margins

---

### Phase 2: Index / Library Page (P0)

#### 2A. Video Card Grid
**Current**: `grid-template-columns: repeat(auto-fill, minmax(300px, 1fr))` - works but cards are wide on phones.
**Target**: Mobile-first grid.

```css
/* Base: single column, full width cards */
.video-grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: var(--spacing-md);
}

@media (min-width: 480px) {
  .video-grid {
    grid-template-columns: repeat(2, 1fr);
  }
}

@media (min-width: 1024px) {
  .video-grid {
    grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  }
}
```

#### 2B. Filter & Action Bars
**Current**: Horizontal flex with wrapping.
**Target**:
- Mobile: Stacked vertically, search full-width on top, filter chips scrollable horizontal
- Tablet+: Current horizontal layout

#### 2C. Source Tabs (All / Manual / EcoEye)
**Current**: Tab buttons inline.
**Target**: Full-width segmented control on mobile (equal-width buttons in a row), standard tabs on desktop.

---

### Phase 3: Annotation Page (P0)

This is the most complex page - a full-screen two-panel layout with video player, canvas overlay, waveform, and annotation panel.

#### 3A. Panel Layout
**Current**: Side-by-side (video flex:2, annotations flex:1). Stacks on mobile but awkwardly.
**Target**: Tabbed interface on mobile.

```
Mobile (< 768px):
┌─────────────────────────┐
│ ← Back                  │
├─────────────────────────┤
│                         │
│   Video Player          │  <- 16:9 aspect ratio, full width
│   (with canvas overlay) │
│                         │
├─────────────────────────┤
│ [▶ Controls] [Timeline] │  <- Compact single-row controls
├─────────────────────────┤
│ [Video] [Annotations]   │  <- Tab switcher
├─────────────────────────┤
│ Annotation list /       │  <- Scrollable panel below
│ AI Predictions          │
│                         │
└─────────────────────────┘

Desktop (≥ 1024px):
┌──────────────────────────────────────────────┐
│ ← Back to Library                            │
├────────────────────────────┬─────────────────┤
│                            │ Annotations     │
│   Video Player             │ - bbox list     │
│   (canvas overlay)         │ - tags          │
│                            │ - actions       │
├────────────────────────────┤                 │
│   Waveform Timeline        │                 │
├────────────────────────────┤                 │
│   Player Controls          │                 │
└────────────────────────────┴─────────────────┘
```

**Changes**:
- **`annotate.css`**: Rewrite layout as mobile-first column, enhance to side-by-side at 1024px
- **`annotate.html`**: Add tab switcher markup for mobile view
- **`annotate.js`**: Add tab switching logic for mobile, handle resize events to switch between layouts

#### 3B. Bounding Box Drawing on Touch (CRITICAL)
**Current**: Mouse-only drawing (mousedown, mousemove, mouseup). Completely non-functional on touch devices.
**Target**: Full touch support for bbox creation, resizing, and repositioning.

**Touch Event Mapping**:
| Mouse Event | Touch Equivalent | Notes |
|-------------|-----------------|-------|
| `mousedown` | `touchstart` | Extract coords from `e.touches[0]` |
| `mousemove` | `touchmove` | Extract coords from `e.touches[0]`, call `e.preventDefault()` |
| `mouseup` | `touchend` | Use `e.changedTouches[0]` (touches array is empty on end) |
| hover (resize handles) | N/A | Use larger hit areas instead |

**Changes**:
- **`annotate.js`**: Add parallel touch handlers for canvas bbox drawing
  - Convert touch coords to canvas-relative coords (same transform as mouse)
  - Single-touch only - ignore multi-touch (reserve for pinch-to-zoom)
  - Call `e.preventDefault()` in `touchmove` to prevent scroll while drawing
- **`scenario-workflow.js`**: Same touch handler additions for scenario-driven bbox workflows
- **`annotate.css`**: Add `touch-action: none` on the canvas element when in draw mode to prevent browser zoom/scroll interference
- **Canvas hit areas**: Increase bbox corner handle size from current ~6px to 20px on touch devices for reliable finger targeting
- **Visual feedback**: Show a crosshair or dot at touch point during draw to compensate for finger occlusion
- **Pinch-to-zoom canvas**: Optional (P3) - allow two-finger zoom on the canvas for precision drawing on small screens, then single-finger to draw at zoomed level

**Edge cases to handle**:
- Accidental touch while scrolling (require brief hold before entering draw mode, or use explicit "Draw" toggle button)
- Palm rejection - ignore touches with large contact area if `TouchEvent.radiusX/radiusY` available
- Prevent double-tap zoom on canvas: `touch-action: manipulation` as fallback

#### 3C. Player Controls
**Current**: Custom controls with play/pause, seek, speed, frame-step.
**Target**:
- Mobile: Minimal controls (play/pause, seek bar, fullscreen). Advanced controls behind a "..." menu
- Desktop: Full controls visible

#### 3D. Waveform Timeline
**Current**: Canvas below video.
**Target**:
- Mobile: Collapsed by default, expandable via tap. When visible, full-width below video
- Desktop: Always visible

---

### Phase 4: Data Tables (P1)

Affects: `training_queue.html`, `prediction_review.html`

#### 4A. Responsive Table Pattern
**Current**: Standard `<table>` with horizontal scroll.
**Target**: Card-based layout on mobile, table on desktop.

```
Mobile (< 768px):
┌─────────────────────────┐
│ Job: person-detection    │
│ Status: ● Running        │
│ Progress: ████░░ 67%     │
│ Started: 2 hours ago     │
│ [View Details]           │
├─────────────────────────┤
│ Job: activity-classify   │
│ Status: ○ Queued         │
│ ...                      │
└─────────────────────────┘

Desktop (≥ 768px):
Standard table view (unchanged)
```

**Changes**:
- **`style.css`**: Add `.responsive-table` utility class
- **Templates**: Add `data-label` attributes to `<td>` elements for mobile card labels
- CSS-only solution using `display: block` on `<tr>` at mobile breakpoint

---

### Phase 5: Modals (P1)

#### 5A. Full-Screen Modals on Mobile
**Current**: Centered overlay with max-width.
**Target**:

```css
/* Base: full screen */
.modal-content {
  width: 100%;
  height: 100%;
  max-width: none;
  border-radius: 0;
  overflow-y: auto;
}

/* Desktop: centered dialog */
@media (min-width: 768px) {
  .modal-content {
    width: auto;
    height: auto;
    max-width: 600px;
    border-radius: 8px;
  }
}
```

#### 5B. Person Identification Modal
**Current**: Grid of person thumbnails.
**Target**: Scrollable vertical list on mobile with larger tap targets. Grid on desktop.

---

### Phase 6: Camera Topology (P1)

#### 6A. D3 Graph on Mobile
**Current**: Force-directed graph in a panel. Nearly unusable on small screens.
**Target**:
- Mobile: Simplified list view of camera connections with expandable details. Graph available via "View Graph" button that opens full-screen
- Desktop: Current side-by-side layout

**Changes**:
- **`camera_topology.html`**: Add list-view markup
- **`static/js/` (new or existing)**: List view rendering logic
- **`style.css`**: Mobile list styles
- D3 graph gets `width: 100vw; height: 70vh` in full-screen mode on mobile

#### 6B. Stats Bar
**Current**: 4-column grid.
**Target**: 2-column grid on mobile (2x2), 4-column on desktop.

---

### Phase 7: Forms & Filters (P2)

#### 7A. Tag Form Generator
**Current**: Multi-column form sections.
**Target**: Single-column on mobile, current layout on desktop. Already partially handled - just needs mobile-first rewrite.

#### 7B. Filter Bars
**Current**: Inline filters with wrapping.
**Target**:
- Mobile: "Filter" button that expands a drawer/sheet from bottom
- Desktop: Inline filters (current behavior)

#### 7C. Export Pages
**Current**: Form-based pages with settings.
**Target**: Full-width forms on mobile. These pages are simpler - mostly just need spacing adjustments.

---

### Phase 8: Micro-Interactions & Polish (P2-P3)

#### 8A. Bottom Action Bar
For pages with primary actions (annotate, prediction review), add a fixed bottom bar on mobile:
```
┌─────────────────────────┐
│ (page content)          │
│                         │
├─────────────────────────┤
│ [Reject] [Skip] [Save]  │  <- Fixed, 56px, safe-area-aware
└─────────────────────────┘
```

Use `env(safe-area-inset-bottom)` for phones with bottom bars (iPhone notch-era devices).

#### 8B. Swipe Gestures (P3 / Optional)
- Swipe between video cards in library
- Swipe left/right on prediction cards for reject/approve
- Requires a small gesture library or ~100 lines of touch JS

#### 8C. Safe Area Insets
Add to all pages:
```css
body {
  padding-top: env(safe-area-inset-top);
  padding-bottom: env(safe-area-inset-bottom);
  padding-left: env(safe-area-inset-left);
  padding-right: env(safe-area-inset-right);
}
```

---

## File Change Summary

### CSS Files (All Modified)

| File | Change Type | Estimated Lines Changed |
|------|-------------|------------------------|
| `style.css` | Major rewrite - flip all media queries, add hamburger, add responsive table, add bottom bar, add CSS variables | ~60% of file |
| `annotate.css` | Major rewrite - mobile-first panel layout, touch canvas, compact controls | ~70% of file |
| `scenario-workflow.css` | Moderate - mobile-first rewrite of wizard UI | ~40% of file |
| `tag-form.css` | Minor - flip queries, adjust spacing | ~25% of file |
| `tag_manager.css` | Minor - flip queries, adjust spacing | ~25% of file |
| `style-additions.css` | Minimal - minor spacing adjustments | ~10% of file |

### Templates (All Modified)

| File | Changes |
|------|---------|
| All 13 templates | Add hamburger menu markup to header |
| `index.html` | Restructure filter bar for mobile drawer pattern |
| `annotate.html` | Add mobile tab switcher, restructure panel markup |
| `camera_topology.html` | Add list-view alternative markup |
| `model_training.html` | Add hamburger nav, convert 1,350 lines inline CSS to mobile-first |
| `training_queue.html` | Add `data-label` attrs to table cells |
| `prediction_review.html` | Add `data-label` attrs, bottom action bar |

### JavaScript Files

| File | Changes |
|------|---------|
| `app.js` | Hamburger toggle (~30 lines), filter drawer toggle (~20 lines) |
| `annotate.js` | Touch event handlers (~80 lines), mobile tab switching (~40 lines), layout resize handler (~20 lines) |
| `scenario-workflow.js` | Touch bbox drawing (~60 lines) |
| New: `responsive-utils.js` | Shared utilities: hamburger, resize detection, bottom bar management (~80 lines) |

---

## Implementation Order

### Phase 0: Pre-Implementation Rescan (REQUIRED)

Before starting any implementation work, **rescan the entire codebase** to capture changes that landed while this plan was on hold. Other work is in progress and may have altered templates, CSS, JS, or added new pages/components.

**Rescan checklist:**
- [ ] Re-read all 6 CSS files - note any new classes, changed breakpoints, or added media queries
- [ ] Re-read all 12+ templates - note any new markup, changed structure, or added pages
- [ ] Re-read all JS files - note any new modules, changed DOM selectors, or event handlers
- [ ] Check for new files in `static/css/`, `static/js/`, and `templates/`
- [ ] Check `api.py` for new routes that may have added new pages
- [ ] Diff this plan against current state - update any phases that are invalidated or simplified by recent changes
- [ ] Update the file change summary if new files need to be covered

**Do not skip this step.** Implementing against a stale understanding of the codebase will cause merge conflicts and rework.

### Phase 0 Results (2026-02-13)

**New files discovered:**
- `templates/model_training.html` - New page with route `/model-training`, has 1,350 lines of inline CSS with its own responsive breakpoints (768px, 480px). Needs hamburger nav and mobile-first treatment.
- `static/js/gt-utils.js` - Utility script (50 lines), overrides window.alert for logging. No mobile impact.
- `static/js/model_training.js` - 1,319 lines, training job management. No touch events, no responsive handling.
- `static/js/prediction_review_page.js` - 214 lines, standalone prediction review page logic. No touch events.

**Orphaned template:**
- `tag_manager.html` has no corresponding Flask route in api.py. May be loaded via iframe or JavaScript. Will still receive hamburger nav treatment.

**Inline CSS sprawl:**
Several templates contain massive inline `<style>` blocks with their own responsive breakpoints:
| Template | Inline CSS Lines | Has Mobile Queries |
|----------|-----------------|-------------------|
| `camera_topology.html` | ~600 lines | Yes (4 breakpoints + touch) |
| `person_manager.html` | ~575 lines | Yes (4 breakpoints) |
| `model_training.html` | ~1,350 lines | Yes (2 breakpoints) |
| `ecoeye_preview.html` | ~705 lines | Yes (2 breakpoints) |
| `prediction_review.html` | ~350 lines | Minimal (1 breakpoint) |
| `training_queue.html` | ~340 lines | None |
| `annotate.html` | ~110 lines | None |
| Others | 0-160 lines | Varies |

These inline styles will need mobile-first conversion alongside the external CSS files.

**Confirmed zero mobile infrastructure:**
- 0 touch event handlers across all 17 JS files
- 0 `window.matchMedia()` calls
- 0 CSS custom properties
- 0 hamburger/mobile nav patterns

**Plan adjustments needed:**
- Phase 1 now covers 13 templates (not 12) for hamburger nav
- File change summary needs model_training.html added
- Inline CSS in templates needs mobile-first conversion (significant additional work for camera_topology, person_manager, model_training, ecoeye_preview)

---

```
Phase 1 ─── Global layout & nav ────────── 1-2 sessions
  1A. CSS custom properties
  1B. Container mobile-first rewrite
  1C. Hamburger navigation
  NOTE: Phase 1 now covers 13 templates (model_training.html added)

Phase 2 ─── Index page ─────────────────── 1 session
  2A. Video grid mobile-first
  2B. Filter/action bars
  2C. Source tabs

Phase 3 ─── Annotation page ────────────── 2-3 sessions (most complex)
  3A. Panel layout (tabbed mobile)
  3B. Touch bbox drawing
  3C. Compact controls
  3D. Waveform collapse

Phase 4 ─── Data tables ────────────────── 1 session
  4A. Responsive table → card pattern

Phase 5 ─── Modals ─────────────────────── 0.5 session
  5A. Full-screen on mobile
  5B. Person ID modal

Phase 6 ─── Camera topology ────────────── 1 session
  6A. List view alternative
  6B. Stats grid

Phase 7 ─── Forms & filters ────────────── 1 session
  7A. Tag forms
  7B. Filter drawers
  7C. Export pages

Phase 8 ─── Polish ─────────────────────── 1 session
  8A. Bottom action bars
  8B. Safe area insets
  8C. Swipe gestures (optional)
```

**Total estimated effort**: 8-10 focused sessions

---

## Testing Strategy

### Device Targets
| Device Class | Width | Test With |
|-------------|-------|-----------|
| Small phone | 320-375px | Chrome DevTools (iPhone SE) |
| Standard phone | 375-414px | Chrome DevTools (iPhone 14 / Pixel 7) |
| Large phone | 414-480px | Chrome DevTools (iPhone 14 Pro Max) |
| Small tablet | 768px | Chrome DevTools (iPad Mini) |
| Tablet | 1024px | Chrome DevTools (iPad Pro) |
| Desktop | 1280px+ | Native browser |

### Key Test Scenarios
1. **Library browsing**: Scroll through videos, tap to open, search, filter
2. **Video annotation**: Play video, draw bboxes via touch, add tags, navigate annotations
3. **Person management**: Browse detections, assign names, bulk operations
4. **Training queue**: Submit jobs, monitor progress
5. **Prediction review**: Filter predictions, approve/reject via tap or swipe
6. **Camera topology**: View network (list on mobile), assign locations
7. **Navigation**: Move between all pages via hamburger menu
8. **Model training**: Submit training jobs, monitor progress, view model results

### Accessibility Checks
- All touch targets ≥ 44x44px
- Focus indicators visible on all interactive elements
- Screen reader labels on hamburger and icon-only buttons
- Sufficient color contrast (already good, maintain)
- Logical tab order preserved after layout changes

---

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| CSS rewrite breaks desktop layout | Convert one file at a time, test both breakpoints after each |
| Touch bbox drawing is imprecise | Add zoom-to-draw mode: pinch to zoom canvas, then draw |
| D3 graph doesn't work in list mode | Keep graph as fallback, list view is primary on mobile |
| Cache-busted CSS not picked up | Update timestamp query params on all CSS includes after changes |
| Performance on mobile (large video grid) | Add lazy loading for thumbnails, limit initial grid to 20 items on mobile |

---

## Non-Goals (Explicitly Out of Scope)

- **PWA / Service Worker**: Not adding offline support in this pass
- **CSS Framework adoption**: Staying with custom CSS
- **Build toolchain**: No Sass/PostCSS/bundler introduction
- **Component library**: No refactor to web components or similar
- **Native mobile app**: This is a responsive web redesign only
- **Dark mode**: Separate effort if desired later
- **Backend language migration**: The Flask/Python backend stays as-is (see rationale below)

---

## Post-Implementation Changes

### Content Addition Separated from Dashboard (2026-02-13)

During implementation, the video download/upload functionality was moved from the main dashboard (`index.html`) to a dedicated `/add-content` page (`add_content.html`).

**What moved:**
- YouTube/URL video download form
- Download queue status display
- Video upload modal

**What changed:**
- `index.html`: Actions panel replaced with a single "Add Content" button
- `add_content.html`: New page with 2-column layout (upload + download), full-width queue
- `api.py`: New `/add-content` route added
- All 14 templates: "Add Content" nav link added to site navigation

**Rationale:** The dashboard should be focused on browsing and searching the video library. Content ingestion is a separate workflow that clutters the main view, especially on mobile where the actions panel consumed significant screen space.

---

## Rejected: Backend Migration to PHP

**Decision**: Keep Flask (Python). Do not migrate to PHP.

**Rationale**:

1. **Zero mobile-first benefit**: The mobile redesign is entirely CSS/HTML/JS. The backend language is invisible to the browser - it doesn't matter what generates the HTML.

2. **Python ML ecosystem dependency**: The training queue, YOLO export, AI prediction, and vibration analysis features all rely on Python libraries (PyTorch, OpenCV, NumPy, etc.). PHP has no equivalent ML ecosystem. A PHP backend would need to shell out to Python scripts anyway, adding complexity for no gain.

3. **3,000+ lines of working API code**: The `api.py` module has 50+ routes with EcoEye integration, UniFi sync, person tracking, and RBAC. Rewriting this in PHP would take weeks and deliver the exact same functionality.

4. **Deployment environment**: Groundtruth Studio runs on Artemis (local server), not shared PHP hosting. Flask's lightweight deployment is a natural fit.

5. **Cost/benefit**: Weeks of backend rewrite effort with zero user-facing improvement vs. the same time spent on the mobile CSS/HTML/JS work which directly improves the experience on every device.
