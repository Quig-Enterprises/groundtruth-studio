let currentVideoId = null;
let videoPlayer = null;
let drawingStartPos = null;

// Global variables (accessible from scenario-workflow.js)
window.bboxCanvas = null;
window.bboxContext = null;
window.currentBBox = null;
window.isDrawingMode = false;

// Local aliases
let bboxCanvas = null;
let bboxContext = null;
let currentBBox = null;
let isDrawingMode = false;
let allTagSuggestions = [];
let allActivityTags = [];
let allMomentTags = [];
let quickSelectSuggestions = {};
let activeAnnotateLibrary = null;

function getVideoIdFromUrl() {
    const params = new URLSearchParams(window.location.search);
    return params.get('id');
}

async function loadVideo() {
    currentVideoId = getVideoIdFromUrl();
    if (!currentVideoId) {
        alert('No video ID provided');
        window.location.href = '/';
        return;
    }

    // Track which library was active when opening
    const params = new URLSearchParams(window.location.search);
    activeAnnotateLibrary = params.get('library') || null;

    // Restore navigation history from session
    try {
        navHistory = JSON.parse(sessionStorage.getItem('annotateNavHistory') || '[]');
    } catch (e) { navHistory = []; }
    // Update back button state
    const backBtn = document.getElementById('nav-back-btn');
    if (backBtn && navHistory.length === 0) {
        backBtn.title = 'Return to video library';
    }

    try {
        const response = await fetch(`/api/videos/${currentVideoId}`);
        const data = await response.json();

        if (!data.success) {
            alert('Video not found');
            window.location.href = '/';
            return;
        }

        const video = data.video;
        document.getElementById('video-title').textContent = video.title || video.filename;

        // Store camera_id for location auto-detection
        window.currentCameraId = video.camera_id || null;
        window.currentLocation = null;

        // If camera_id exists, lookup location mapping
        if (window.currentCameraId) {
            try {
                const locResponse = await fetch(`/api/camera-locations/lookup/${encodeURIComponent(window.currentCameraId)}`);
                const locData = await locResponse.json();
                if (locData.success && locData.location) {
                    window.currentLocation = locData.location;
                    console.log('[Annotate] Camera location detected:', locData.location.location_name);
                }
            } catch (e) {
                console.log('[Annotate] No location mapping for camera:', window.currentCameraId);
            }
        }

        // Detect image-only mode: no video file but has a thumbnail
        const hasVideoFile = video.has_video_file;
        const thumbnailPath = video.thumbnail_path;

        if (!hasVideoFile && thumbnailPath) {
            // IMAGE MODE: Use thumbnail for annotation
            console.log('[Annotate] Image mode: no video file, using thumbnail:', thumbnailPath);

            const thumbName = thumbnailPath.split('/').pop();
            const thumbnailUrl = `/thumbnails/${encodeURIComponent(thumbName)}`;

            // Initialize image mode shim
            const shim = initImageMode(thumbnailUrl);
            if (shim) {
                videoPlayer = shim;

                // Wait for image to load before setting up canvas
                const imgEl = document.getElementById('thumbnail-image');
                const onImageReady = () => {
                    console.log('[Annotate] Image loaded, dimensions:', shim.videoWidth, 'x', shim.videoHeight);
                    // Delay to ensure layout reflow after image-mode class is applied
                    requestAnimationFrame(() => {
                        requestAnimationFrame(() => {
                            resizeCanvas();
                        });
                    });

                    // Update metadata display
                    document.getElementById('meta-duration').textContent = 'Static Image';
                    document.getElementById('meta-resolution').textContent = `${shim.videoWidth}x${shim.videoHeight}`;
                };

                if (imgEl.naturalWidth > 0) {
                    onImageReady();
                } else {
                    imgEl.addEventListener('load', onImageReady, { once: true });
                }
            }

            // Still load existing annotations
            loadTimeRangeTags();
            loadKeyframeAnnotations();

        } else {
            // VIDEO MODE: Normal video loading
            document.getElementById('video-source').src = `/downloads/${video.filename}`;

            document.getElementById('meta-duration').textContent = formatDuration(video.duration);
            document.getElementById('meta-resolution').textContent = `${video.width}x${video.height}`;

            videoPlayer.load();

            loadTimeRangeTags();
            loadKeyframeAnnotations();
        }

        // Load library assignment bar
        loadAnnotateLibraryBar();

    } catch (error) {
        alert('Error loading video: ' + error.message);
        window.location.href = '/';
    }
}

async function loadAnnotateLibraryBar() {
    const bar = document.getElementById('annotation-library-bar');
    if (!bar) return;
    try {
        const [libRes, vidRes] = await Promise.all([
            fetch('/api/libraries'),
            fetch(`/api/videos/${currentVideoId}`)
        ]);
        const libData = await libRes.json();
        const vidData = await vidRes.json();
        const videoLibIds = (vidData.video?.libraries || []).map(l => l.id);
        // Video is "uncategorized" if it's not in any non-default library
        const inAnyCustomLib = videoLibIds.length > 0;

        // Only show non-default libraries as toggleable chips
        const nonDefault = libData.libraries.filter(l => !l.is_default);
        let html = '<span class="lib-label">Libraries:</span>';
        if (!inAnyCustomLib) {
            html += '<span class="lib-chip active" style="cursor: default; opacity: 0.7;">Uncategorized</span>';
        }
        html += nonDefault.map(lib => {
            const isActive = videoLibIds.includes(lib.id);
            return `<span class="lib-chip ${isActive ? 'active' : ''}" onclick="toggleAnnotateLibrary(this, ${lib.id})">${lib.name}</span>`;
        }).join('');
        bar.innerHTML = html;
    } catch (e) {
        console.error('Error loading library bar:', e);
    }
}

async function toggleAnnotateLibrary(chip, libraryId) {
    const isActive = chip.classList.contains('active');
    try {
        if (isActive) {
            await fetch(`/api/libraries/${libraryId}/items/${currentVideoId}`, { method: 'DELETE' });
            chip.classList.remove('active');
        } else {
            await fetch(`/api/libraries/${libraryId}/items`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ video_ids: [parseInt(currentVideoId)] })
            });
            chip.classList.add('active');
        }
        // Refresh to update Uncategorized state
        loadAnnotateLibraryBar();
    } catch (e) {
        console.error('Error toggling library:', e);
    }
}

// Navigation history
let navHistory = [];

function navBack() {
    if (navHistory.length > 0) {
        const prevId = navHistory.pop();
        sessionStorage.setItem('annotateNavHistory', JSON.stringify(navHistory));
        const libParam = activeAnnotateLibrary ? `&library=${activeAnnotateLibrary}` : '';
        window.location.href = `/annotate?id=${prevId}${libParam}`;
    } else if (activeAnnotateLibrary) {
        window.location.href = `/?library=${activeAnnotateLibrary}`;
    } else {
        window.location.href = '/';
    }
}

async function navNext() {
    try {
        let res, data;
        const pushHistory = () => {
            const hist = JSON.parse(sessionStorage.getItem('annotateNavHistory') || '[]');
            hist.push(currentVideoId);
            sessionStorage.setItem('annotateNavHistory', JSON.stringify(hist));
        };
        if (activeAnnotateLibrary) {
            res = await fetch(`/api/libraries/${activeAnnotateLibrary}/next-unannotated?current=${currentVideoId}`);
            data = await res.json();
            if (data.success && data.video) {
                pushHistory();
                window.location.href = `/annotate?id=${data.video.id}&library=${activeAnnotateLibrary}`;
                return;
            }
            alert('No more unannotated videos in this library.');
            return;
        }
        // No library context - use global
        res = await fetch(`/api/next-unannotated?current=${currentVideoId}`);
        data = await res.json();
        if (data.success && data.video) {
            pushHistory();
            window.location.href = `/annotate?id=${data.video.id}`;
            return;
        }
        alert('All videos have been annotated!');
    } catch (e) {
        console.error('Error finding next video:', e);
    }
}

function saveAndClose() {
    window._saveAction = 'close';
    scenarioWorkflow.saveFromEditor();
}

function saveAndNext() {
    window._saveAction = 'next';
    scenarioWorkflow.saveFromEditor();
}

async function navigateAfterSave() {
    const action = window._saveAction || 'close';
    window._saveAction = null;

    if (action === 'next') {
        try {
            let res, data;
            const pushHistory = () => {
                const hist = JSON.parse(sessionStorage.getItem('annotateNavHistory') || '[]');
                hist.push(currentVideoId);
                sessionStorage.setItem('annotateNavHistory', JSON.stringify(hist));
            };
            if (activeAnnotateLibrary) {
                // Stay within the active library
                res = await fetch(`/api/libraries/${activeAnnotateLibrary}/next-unannotated?current=${currentVideoId}`);
                data = await res.json();
                if (data.success && data.video) {
                    pushHistory();
                    window.location.href = `/annotate?id=${data.video.id}&library=${activeAnnotateLibrary}`;
                    return;
                }
                // No more unannotated in this library - go back to library view
                alert('All videos in this library have been annotated!');
                window.location.href = `/?library=${activeAnnotateLibrary}`;
                return;
            }
            // No library context - use global search
            res = await fetch(`/api/next-unannotated?current=${currentVideoId}`);
            data = await res.json();
            if (data.success && data.video) {
                pushHistory();
                window.location.href = `/annotate?id=${data.video.id}`;
                return;
            }
            alert('All videos have been annotated!');
        } catch (e) {
            console.error('Error finding next video:', e);
        }
        // After alert or error, go home
        if (activeAnnotateLibrary) {
            window.location.href = `/?library=${activeAnnotateLibrary}`;
        } else {
            window.location.href = '/';
        }
        return;
    }

    // Close: go home with library filter
    if (activeAnnotateLibrary) {
        window.location.href = `/?library=${activeAnnotateLibrary}`;
    } else {
        window.location.href = '/';
    }
}

// Old tag suggestion system removed - now using scenario-based workflow

function selectQuickTag(tagText) {
    document.getElementById('tag-name-input').value = tagText;
}

function selectActivityTag(tagText) {
    document.getElementById('keyframe-activity-tag').value = tagText;
}

function selectMomentTag(tagText) {
    document.getElementById('keyframe-moment-tag').value = tagText;
}

async function loadTimeRangeTags() {
    try {
        const response = await fetch(`/api/videos/${currentVideoId}/time-range-tags`);
        const data = await response.json();

        if (data.success) {
            displayTimeRangeTags(data.tags);
        }
    } catch (error) {
        console.error('Error loading time range tags:', error);
    }
}

function displayTimeRangeTags(tags) {
    const container = document.getElementById('time-range-tags-list');

    if (tags.length === 0) {
        container.innerHTML = '<div class="empty-state">No time range tags yet. Click "+ Tag" to add one.</div>';
        return;
    }

    container.innerHTML = tags.map(tag => {
        const isOpen = tag.end_time === null || tag.end_time === undefined;
        const isNegative = tag.is_negative === 1 || tag.is_negative === true;

        return `
            <div class="time-range-tag-item ${isOpen ? 'open' : ''} ${isNegative ? 'negative' : ''}">
                <div class="tag-item-header">
                    <span class="tag-name">${escapeHtml(tag.tag_name)}</span>
                    <span class="tag-time-range">
                        ${formatDuration(tag.start_time)}
                        ${!isOpen ? ` - ${formatDuration(tag.end_time)}` : ' (open)'}
                    </span>
                </div>
                ${tag.comment ? `<div class="tag-comment">"${escapeHtml(tag.comment)}"</div>` : ''}
                <div class="tag-actions">
                    <button onclick="seekToTime(${tag.start_time})" class="btn-small btn-seek">Seek Start</button>
                    ${isOpen ? `<button onclick="closeTimeRangeTag(${tag.id})" class="btn-small btn-close-tag">Close at Current Time</button>` : ''}
                    <button onclick="deleteTimeRangeTag(${tag.id})" class="btn-small btn-delete-tag">Delete</button>
                </div>
            </div>
        `;
    }).join('');
}

async function loadKeyframeAnnotations() {
    try {
        const response = await fetch(`/api/videos/${currentVideoId}/keyframe-annotations`);
        const data = await response.json();

        if (data.success) {
            // Store annotations globally for waveform markers
            window.currentKeyframeAnnotations = data.annotations;

            displayKeyframeAnnotations(data.annotations);

            // Redraw waveform with markers
            if (typeof redrawWaveformMarkers === 'function') {
                redrawWaveformMarkers();
            }
        }
    } catch (error) {
        console.error('Error loading keyframe annotations:', error);
    }
}

async function displayKeyframeAnnotations(annotations) {
    const container = document.getElementById('keyframe-annotations-list');

    if (annotations.length === 0) {
        container.innerHTML = '<div class="empty-state">No keyframe annotations yet. Click "+ Keyframe" to add one.</div>';
        return;
    }

    // Fetch tags for all annotations to get scenario data
    const annotationsWithTags = await Promise.all(annotations.map(async anno => {
        try {
            const response = await fetch(`/api/annotations/${anno.id}/tags?annotation_type=keyframe`);
            const data = await response.json();
            return { ...anno, tags: data.success ? data.tags : {} };
        } catch (error) {
            console.error('Error loading tags for annotation', anno.id, error);
            return { ...anno, tags: {} };
        }
    }));

    container.innerHTML = annotationsWithTags.map(anno => { // eslint-disable-line -- innerHTML is safe here: all values are escaped via escapeHtml()
        const isNegative = anno.is_negative === 1 || anno.is_negative === true;
        const isAI = anno.reviewed === 0 || anno.reviewed === false;
        const tags = anno.tags || {};
        const scenarioId = tags.scenario || anno.activity_tag;

        // Get notes from tags or comment
        const notes = tags.comment || anno.comment || '';

        // Extract person name from tags (stored as JSON string '{"person_name":"John"}')
        let personName = '';
        if (tags.person_name) {
            try {
                const parsed = typeof tags.person_name === 'string' ? JSON.parse(tags.person_name) : tags.person_name;
                personName = parsed.person_name || '';
            } catch (e) {
                personName = String(tags.person_name);
            }
        }

        // Bbox info
        const hasBBox = anno.bbox_x != null && anno.bbox_y != null;

        return `
        <div class="keyframe-item ${isNegative ? 'negative' : ''} ${isAI ? 'ai-generated' : ''}" onclick="showAnnotationBBoxes(${Number(anno.id)}, ${Number(anno.timestamp)})" style="cursor: pointer;${isAI ? ' border-left: 3px dashed #f39c12;' : ''}" data-annotation-id="${Number(anno.id)}">
            <div class="keyframe-header">
                <span class="keyframe-timestamp">${formatDuration(anno.timestamp)}</span>
                ${isAI ? '<span class="ai-badge" style="font-size:10px;background:#f39c12;color:#fff;padding:1px 5px;border-radius:3px;margin-left:6px;">AI</span>' : ''}
                <div class="keyframe-header-actions">
                    <button onclick="event.stopPropagation(); editKeyframeAnnotation(${Number(anno.id)}, ${Number(anno.timestamp)})" class="btn-small">Edit</button>
                    <button onclick="event.stopPropagation(); deleteKeyframeAnnotation(${Number(anno.id)})" class="btn-small btn-delete-tag">Delete</button>
                </div>
            </div>
            ${scenarioId ? `
                <div class="keyframe-tags">
                    <span class="activity-tag">${escapeHtml(scenarioId.replace(/_/g, ' '))}</span>
                    ${personName ? `<span class="moment-tag" style="background: #27ae60;">${escapeHtml(personName)}</span>` : ''}
                </div>
            ` : ''}
            ${hasBBox ? `<div class="keyframe-bbox">BBox: ${Math.round(anno.bbox_x)},${Math.round(anno.bbox_y)} ${Math.round(anno.bbox_width)}\u00D7${Math.round(anno.bbox_height)}</div>` : ''}
            ${notes ? `<div class="keyframe-comment">${escapeHtml(notes)}</div>` : ''}
            <div class="keyframe-thumbnail" id="thumbnail-${Number(anno.id)}"></div>
        </div>
        `;
    }).join('');

    // Generate thumbnails for each annotation (with bbox overlay)
    annotationsWithTags.forEach(anno => {
        generateThumbnail(anno.id, anno.timestamp, anno);
    });
}

function addNewTimeRangeTag() {
    const currentTime = videoPlayer.currentTime;
    document.getElementById('tag-start-time').value = currentTime.toFixed(2);
    document.getElementById('tag-end-time').value = '';
    document.getElementById('tag-name-input').value = '';
    document.getElementById('tag-is-negative').checked = false;
    document.getElementById('tag-comment').value = '';
    updateTagCommentLabel();
    displayQuickSelectForTimeRange();

    document.getElementById('tag-modal').style.display = 'flex';
    document.getElementById('tag-name-input').focus();
}

function updateTagCommentLabel() {
    const isNegative = document.getElementById('tag-is-negative').checked;
    const label = document.getElementById('tag-comment-label');
    const textarea = document.getElementById('tag-comment');

    if (isNegative) {
        label.textContent = 'What makes this confusing?';
        textarea.placeholder = 'Describe what makes this look like the behavior but is not...';
    } else {
        label.textContent = 'Comment';
        textarea.placeholder = 'Optional comment...';
    }

    displayQuickSelectForTimeRange();
}

// Old modal functions removed - now using scenario workflow system

async function closeTimeRangeTag(tagId) {
    const currentTime = videoPlayer.currentTime;

    try {
        const response = await fetch(`/api/time-range-tags/${tagId}`, {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({end_time: currentTime})
        });

        const data = await response.json();

        if (data.success) {
            loadTimeRangeTags();
        } else {
            alert('Error closing tag');
        }
    } catch (error) {
        alert('Error: ' + error.message);
    }
}

async function deleteTimeRangeTag(tagId) {
    if (!confirm('Delete this time range tag?')) return;

    try {
        const response = await fetch(`/api/time-range-tags/${tagId}`, {
            method: 'DELETE'
        });

        const data = await response.json();

        if (data.success) {
            loadTimeRangeTags();
        }
    } catch (error) {
        alert('Error: ' + error.message);
    }
}

function startKeyframeAnnotation() {
    videoPlayer.pause();
    toggleDrawMode();
    document.getElementById('keyframe-timestamp').value = videoPlayer.currentTime.toFixed(2);
}

function startOtherAnnotation() {
    // Open the scenario workflow directly to "Other Annotation" scenario
    if (window.scenarioWorkflow) {
        window.scenarioWorkflow.startWorkflow();
        // Wait a moment for the scenario selection to render, then select "other"
        setTimeout(() => {
            window.scenarioWorkflow.selectScenario('other');
        }, 100);
    }
}

function toggleDrawMode() {
    isDrawingMode = !isDrawingMode;
    window.isDrawingMode = isDrawingMode; // Keep global in sync
    const btn = document.getElementById('draw-mode-btn');
    const container = document.querySelector('.annotation-container');

    if (isDrawingMode) {
        btn.classList.add('active');
        btn.textContent = 'Cancel Drawing';
        bboxCanvas.classList.add('drawing');
        if (container) container.classList.add('bbox-drawing');
    } else {
        btn.classList.remove('active');
        btn.textContent = 'Draw BBox';
        bboxCanvas.classList.remove('drawing');
        if (container) container.classList.remove('bbox-drawing');
        clearCanvas();
    }
}

function initializeCanvas() {
    bboxCanvas = document.getElementById('bbox-canvas');
    bboxContext = bboxCanvas.getContext('2d');

    // Also set global references
    window.bboxCanvas = bboxCanvas;
    window.bboxContext = bboxContext;

    bboxCanvas.addEventListener('mousedown', handleCanvasMouseDown);
    bboxCanvas.addEventListener('mousemove', handleCanvasMouseMove);
    bboxCanvas.addEventListener('mouseup', handleCanvasMouseUp);
    bboxCanvas.addEventListener('mouseleave', handleCanvasMouseLeave);

    // ===== Touch Event Handlers for BBox Drawing =====

    function getTouchCanvasCoords(e) {
        const touch = e.touches[0] || e.changedTouches[0];
        const rect = bboxCanvas.getBoundingClientRect();
        return {
            x: touch.clientX - rect.left,
            y: touch.clientY - rect.top
        };
    }

    bboxCanvas.addEventListener('touchstart', function(e) {
        if (!window.isDrawingMode) return;
        e.preventDefault();
        const coords = getTouchCanvasCoords(e);
        handleCanvasMouseDown({ clientX: coords.x + bboxCanvas.getBoundingClientRect().left, clientY: coords.y + bboxCanvas.getBoundingClientRect().top, preventDefault: function(){} });
    }, { passive: false });

    bboxCanvas.addEventListener('touchmove', function(e) {
        if (!window.isDrawingMode) return;
        e.preventDefault();
        const coords = getTouchCanvasCoords(e);
        handleCanvasMouseMove({ clientX: coords.x + bboxCanvas.getBoundingClientRect().left, clientY: coords.y + bboxCanvas.getBoundingClientRect().top });
    }, { passive: false });

    bboxCanvas.addEventListener('touchend', function(e) {
        if (!window.isDrawingMode) return;
        e.preventDefault();
        const coords = getTouchCanvasCoords(e);
        handleCanvasMouseUp({ clientX: coords.x + bboxCanvas.getBoundingClientRect().left, clientY: coords.y + bboxCanvas.getBoundingClientRect().top });
    }, { passive: false });
}

function resizeCanvas() {
    if (window.isImageMode) {
        const imgEl = document.getElementById('thumbnail-image');
        const wrapper = document.querySelector('.video-wrapper');
        if (imgEl && wrapper) {
            const natW = imgEl.naturalWidth;
            const natH = imgEl.naturalHeight;
            if (natW && natH) {
                // Image uses object-fit: contain, so compute the rendered content area
                const elemW = imgEl.clientWidth;
                const elemH = imgEl.clientHeight;
                const scale = Math.min(elemW / natW, elemH / natH);
                const renderedW = Math.floor(natW * scale);
                const renderedH = Math.floor(natH * scale);
                const offsetX = Math.floor((elemW - renderedW) / 2);
                const offsetY = Math.floor((elemH - renderedH) / 2);

                // Position canvas precisely over the rendered image content
                bboxCanvas.width = natW;
                bboxCanvas.height = natH;
                bboxCanvas.style.width = renderedW + 'px';
                bboxCanvas.style.height = renderedH + 'px';
                bboxCanvas.style.top = offsetY + 'px';
                bboxCanvas.style.left = offsetX + 'px';
                bboxCanvas.style.transform = 'none';
            }
        }
    } else {
        const video = videoPlayer;
        bboxCanvas.width = video.videoWidth || video.offsetWidth;
        bboxCanvas.height = video.videoHeight || video.offsetHeight;
        bboxCanvas.style.width = video.offsetWidth + 'px';
        bboxCanvas.style.height = video.offsetHeight + 'px';
    }
}

function handleCanvasMouseDown(e) {
    console.log('[BBox] Mouse down event received - isDrawingMode:', window.isDrawingMode, 'canvas:', bboxCanvas ? 'present' : 'null');

    if (!window.isDrawingMode) {
        console.log('[BBox] Mouse down but drawing mode not active');
        return;
    }

    console.log('[BBox] Starting bbox drawing');
    const rect = bboxCanvas.getBoundingClientRect();
    const scaleX = bboxCanvas.width / rect.width;
    const scaleY = bboxCanvas.height / rect.height;

    // Clamp start position to canvas bounds
    let startX = (e.clientX - rect.left) * scaleX;
    let startY = (e.clientY - rect.top) * scaleY;
    startX = Math.max(0, Math.min(bboxCanvas.width, startX));
    startY = Math.max(0, Math.min(bboxCanvas.height, startY));

    drawingStartPos = { x: startX, y: startY };
    console.log('[BBox] Drawing start position:', drawingStartPos);
}

function handleCanvasMouseMove(e) {
    if (!window.isDrawingMode) return;

    const rect = bboxCanvas.getBoundingClientRect();
    const scaleX = bboxCanvas.width / rect.width;
    const scaleY = bboxCanvas.height / rect.height;

    let currentX = (e.clientX - rect.left) * scaleX;
    let currentY = (e.clientY - rect.top) * scaleY;
    currentX = Math.max(0, Math.min(bboxCanvas.width, currentX));
    currentY = Math.max(0, Math.min(bboxCanvas.height, currentY));

    clearCanvas();

    // Draw crosshairs extending to edges of the image
    drawCrosshairs(currentX, currentY);

    // Draw bbox preview if actively dragging
    if (drawingStartPos) {
        drawBBox(drawingStartPos.x, drawingStartPos.y, currentX - drawingStartPos.x, currentY - drawingStartPos.y);
    }
}

function handleCanvasMouseLeave() {
    if (!window.isDrawingMode) return;
    if (!drawingStartPos) {
        clearCanvas();
    }
}

function drawCrosshairs(x, y) {
    bboxContext.save();
    bboxContext.strokeStyle = 'rgba(255, 255, 255, 0.5)';
    bboxContext.lineWidth = 0.5;
    bboxContext.setLineDash([4, 4]);

    // Vertical line (top to bottom)
    bboxContext.beginPath();
    bboxContext.moveTo(x, 0);
    bboxContext.lineTo(x, bboxCanvas.height);
    bboxContext.stroke();

    // Horizontal line (left to right)
    bboxContext.beginPath();
    bboxContext.moveTo(0, y);
    bboxContext.lineTo(bboxCanvas.width, y);
    bboxContext.stroke();

    bboxContext.restore();
}

function handleCanvasMouseUp(e) {
    if (!window.isDrawingMode || !drawingStartPos) return;

    const rect = bboxCanvas.getBoundingClientRect();
    const scaleX = bboxCanvas.width / rect.width;
    const scaleY = bboxCanvas.height / rect.height;

    // Get end position and clamp to canvas boundaries
    let endX = (e.clientX - rect.left) * scaleX;
    let endY = (e.clientY - rect.top) * scaleY;

    // Clamp end coordinates to canvas bounds
    endX = Math.max(0, Math.min(bboxCanvas.width, endX));
    endY = Math.max(0, Math.min(bboxCanvas.height, endY));

    const x = Math.min(drawingStartPos.x, endX);
    const y = Math.min(drawingStartPos.y, endY);
    const width = Math.abs(endX - drawingStartPos.x);
    const height = Math.abs(endY - drawingStartPos.y);

    // Create bbox with any size (removed minimum size constraint)
    if (width > 0 && height > 0) {
        currentBBox = {
            x: Math.round(x),
            y: Math.round(y),
            width: Math.round(width),
            height: Math.round(height)
        };
        window.currentBBox = currentBBox; // Set global reference

        console.log('[BBox] BBox completed:', currentBBox);

        // Notify scenario workflow directly instead of relying on polling
        if (typeof window.onBBoxDrawn === 'function') {
            window.onBBoxDrawn(window.currentBBox);
        }

        // Only open modal if scenario workflow is NOT active
        // Scenario workflow will detect currentBBox via its waitForBBox() method
        if (!window.scenarioWorkflow || !window.scenarioWorkflow.currentScenario) {
            console.log('[BBox] No scenario workflow active - would open old modal (but removed)');
            toggleDrawMode();
        } else {
            console.log('[BBox] Scenario workflow active - bbox will be picked up by waitForBBox()');
        }
    }

    drawingStartPos = null;
}

function drawBBox(x, y, width, height, color = '#e74c3c', label = null, isDotted = false) {
    bboxContext.strokeStyle = color;
    bboxContext.lineWidth = 3;
    if (isDotted) {
        bboxContext.setLineDash([8, 6]);
    } else {
        bboxContext.setLineDash([]);
    }
    bboxContext.strokeRect(x, y, width, height);
    bboxContext.setLineDash([]);

    // Draw label if provided
    if (label) {
        bboxContext.font = '14px Arial';
        bboxContext.fillStyle = color;

        // Measure text width for background
        const textMetrics = bboxContext.measureText(label);
        const textWidth = textMetrics.width;
        const textHeight = 16;
        const padding = 4;

        // Draw background rectangle
        bboxContext.fillStyle = color;
        bboxContext.fillRect(x, y - textHeight - padding, textWidth + padding * 2, textHeight + padding);

        // Draw text
        bboxContext.fillStyle = 'white';
        bboxContext.fillText(label, x + padding, y - padding);
    }
}

function clearCanvas() {
    bboxContext.clearRect(0, 0, bboxCanvas.width, bboxCanvas.height);
}

// Removed: openKeyframeModal, updateKeyframeCommentLabel, saveKeyframeAnnotation
// Now using scenario workflow system

async function deleteKeyframeAnnotation(annotationId) {
    if (!confirm('Delete this keyframe annotation?')) return;

    try {
        const response = await fetch(`/api/keyframe-annotations/${annotationId}`, {
            method: 'DELETE'
        });

        const data = await response.json();

        if (data.success) {
            loadKeyframeAnnotations();
        }
    } catch (error) {
        alert('Error: ' + error.message);
    }
}

function showKeyframeBBox(id, timestamp, x, y, width, height) {
    seekToTime(timestamp);
    setTimeout(() => {
        clearCanvas();
        drawBBox(x, y, width, height, '#27ae60');
        setTimeout(() => clearCanvas(), 3000);
    }, 100);
}

function editKeyframeAnnotation(annotationId, timestamp) {
    console.log('[Annotate] Edit annotation requested:', annotationId);
    if (window.scenarioWorkflow) {
        window.scenarioWorkflow.editAnnotation(annotationId, timestamp);
    } else {
        alert('Scenario workflow not available');
    }
}

/**
 * Core function to render annotation bboxes
 * Used by both click handlers and auto-display during playback
 */
async function renderAnnotationBBoxes(annotationId) {
    try {
        // First get the annotation to check if it's reviewed
        const annoResponse = await fetch(`/api/keyframe-annotations/${annotationId}`);
        const annoData = await annoResponse.json();

        if (!annoData.success || !annoData.annotation) {
            console.error('Failed to load annotation');
            return;
        }

        const isReviewed = annoData.annotation.reviewed === 1 || annoData.annotation.reviewed === true;
        const isDotted = !isReviewed; // Dotted if NOT reviewed (AI-generated)

        const response = await fetch(`/api/annotations/${annotationId}/tags?annotation_type=keyframe`);
        const data = await response.json();

        if (!data.success) {
            console.error('Failed to load annotation tags');
            return;
        }

        const tags = data.tags || {};
        const bboxes = tags.bboxes || {};

        // Clear canvas first
        clearCanvas();

        // If no structured bboxes, fall back to main annotation bbox
        if (Object.keys(bboxes).length === 0) {
            const anno = annoData.annotation;
            drawBBox(anno.bbox_x, anno.bbox_y, anno.bbox_width, anno.bbox_height, '#27ae60', 'Annotation', isDotted);
            return;
        }

        // Draw all bboxes with labels
        const colors = ['#27ae60', '#3498db', '#e74c3c', '#f39c12', '#9b59b6', '#1abc9c'];
        let colorIndex = 0;

        for (const [stepId, bbox] of Object.entries(bboxes)) {
            const color = colors[colorIndex % colors.length];
            const label = stepId.replace(/_/g, ' ');
            drawBBox(bbox.x, bbox.y, bbox.width, bbox.height, color, label, isDotted);
            colorIndex++;
        }

    } catch (error) {
        console.error('Error rendering annotation bboxes:', error);
    }
}

async function showAnnotationBBoxes(annotationId, timestamp) {
    console.log('[Annotate] Showing all bboxes for annotation:', annotationId);

    // Seek to the timestamp
    seekToTime(timestamp);

    // Wait for video to seek
    await new Promise(resolve => setTimeout(resolve, 100));

    // Use shared rendering function
    await renderAnnotationBBoxes(annotationId);
}

function generateThumbnail(annotationId, timestamp, anno) {
    const thumbnailContainer = document.getElementById(`thumbnail-${annotationId}`);
    if (!thumbnailContainer) return;

    // Helper: add SVG bbox overlay on the thumbnail
    function addBboxOverlay(container, natW, natH) {
        if (!anno || anno.bbox_x == null || anno.bbox_y == null) return;
        // Remove existing overlay
        const old = container.querySelector('.thumb-bbox-svg');
        if (old) old.remove();

        const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.setAttribute('class', 'thumb-bbox-svg');
        svg.setAttribute('viewBox', `0 0 ${natW} ${natH}`);
        svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');
        svg.style.cssText = 'position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;';

        const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
        rect.setAttribute('x', anno.bbox_x);
        rect.setAttribute('y', anno.bbox_y);
        rect.setAttribute('width', anno.bbox_width);
        rect.setAttribute('height', anno.bbox_height);
        rect.setAttribute('fill', 'none');
        rect.setAttribute('stroke', '#27ae60');
        rect.setAttribute('stroke-width', '3');
        rect.setAttribute('vector-effect', 'non-scaling-stroke');
        svg.appendChild(rect);

        // Add person name label if available
        const tags = anno.tags || {};
        let personName = '';
        if (tags.person_name) {
            try {
                const parsed = typeof tags.person_name === 'string' ? JSON.parse(tags.person_name) : tags.person_name;
                personName = parsed.person_name || '';
            } catch (e) {
                personName = String(tags.person_name);
            }
        }
        if (personName) {
            // Scale font size relative to natural dimensions for readability
            const fontSize = Math.max(natH * 0.04, 14);
            const bg = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
            bg.setAttribute('x', anno.bbox_x);
            bg.setAttribute('y', anno.bbox_y - fontSize - 4);
            bg.setAttribute('width', personName.length * fontSize * 0.65 + 8);
            bg.setAttribute('height', fontSize + 4);
            bg.setAttribute('fill', '#27ae60');
            bg.setAttribute('rx', '3');
            svg.appendChild(bg);

            const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
            text.setAttribute('x', anno.bbox_x + 4);
            text.setAttribute('y', anno.bbox_y - 4);
            text.setAttribute('fill', 'white');
            text.setAttribute('font-size', fontSize);
            text.setAttribute('font-family', 'Arial, sans-serif');
            text.setAttribute('font-weight', '600');
            text.textContent = personName;
            svg.appendChild(text);
        }

        container.appendChild(svg);
    }

    // In image mode, show the source thumbnail directly (no video seeking needed)
    if (window.isImageMode) {
        const imgEl = document.getElementById('thumbnail-image');
        if (imgEl && imgEl.src) {
            const thumbImg = document.createElement('img');
            thumbImg.src = imgEl.src;
            thumbImg.alt = 'Thumbnail';
            thumbImg.style.cssText = 'width: 100%; height: auto; border-radius: 4px;';
            thumbnailContainer.textContent = '';
            thumbnailContainer.appendChild(thumbImg);
            addBboxOverlay(thumbnailContainer, imgEl.naturalWidth, imgEl.naturalHeight);
        }
        return;
    }

    // Save current time
    const originalTime = videoPlayer.currentTime;

    // Create a hidden canvas for thumbnail generation
    const canvas = document.createElement('canvas');
    canvas.width = 160;
    canvas.height = 90;
    const ctx = canvas.getContext('2d');

    // Seek to timestamp and capture frame
    videoPlayer.currentTime = timestamp;

    const captureFrame = () => {
        const natW = videoPlayer.videoWidth || 160;
        const natH = videoPlayer.videoHeight || 90;

        // Draw video frame to canvas
        ctx.drawImage(videoPlayer, 0, 0, canvas.width, canvas.height);

        // Convert to data URL and create img element
        const dataUrl = canvas.toDataURL('image/jpeg', 0.7);
        const img = document.createElement('img');
        img.src = dataUrl;
        img.alt = 'Thumbnail';
        img.style.cssText = 'width: 100%; height: auto; border-radius: 4px;';
        thumbnailContainer.textContent = '';
        thumbnailContainer.appendChild(img);
        addBboxOverlay(thumbnailContainer, natW, natH);

        // Restore original time
        videoPlayer.currentTime = originalTime;
    };

    // Wait for seek to complete
    videoPlayer.addEventListener('seeked', captureFrame, { once: true });
}

function togglePlay() {
    if (videoPlayer.paused) {
        videoPlayer.play();
        document.getElementById('play-btn').textContent = 'Pause';
    } else {
        videoPlayer.pause();
        document.getElementById('play-btn').textContent = 'Play';
    }
}

function seekRelative(seconds) {
    videoPlayer.currentTime = Math.max(0, Math.min(videoPlayer.duration, videoPlayer.currentTime + seconds));
}

function seekToTime(seconds) {
    videoPlayer.currentTime = seconds;
}

function changePlaybackSpeed(speed) {
    const rate = parseFloat(speed);
    try {
        videoPlayer.playbackRate = rate;
    } catch (e) {
        // If browser doesn't support this rate, try capping at 16x
        console.warn(`Playback rate ${rate}x not supported, capping at 16x`);
        videoPlayer.playbackRate = Math.min(rate, 16);
    }
}

function updateTimeDisplay() {
    const current = videoPlayer.currentTime;
    const total = videoPlayer.duration;

    document.getElementById('current-time').textContent = formatDuration(current);
    document.getElementById('total-time').textContent = formatDuration(total);

    // Auto-display bboxes for keyframes at current time
    autoDisplayKeyframeBBoxes(current);

    // Update active keyframe highlighting in annotation list
    updateActiveKeyframeHighlight(current);
}

/**
 * Update active keyframe highlighting in annotation list
 */
function updateActiveKeyframeHighlight(currentTime) {
    if (!window.currentKeyframeAnnotations || window.currentKeyframeAnnotations.length === 0) {
        return;
    }

    const tolerance = 0.5;

    // Remove all existing active classes
    document.querySelectorAll('.keyframe-item').forEach(item => {
        item.classList.remove('active');
    });

    // Find and highlight active keyframes
    window.currentKeyframeAnnotations.forEach(anno => {
        const isActive = Math.abs(anno.timestamp - currentTime) < tolerance;
        if (isActive) {
            const item = document.querySelector(`.keyframe-item[data-annotation-id="${anno.id}"]`);
            if (item) {
                item.classList.add('active');
            }
        }
    });
}

/**
 * Automatically display bboxes for keyframes near current video time
 */
function autoDisplayKeyframeBBoxes(currentTime) {
    if (!window.currentKeyframeAnnotations || window.currentKeyframeAnnotations.length === 0) {
        return;
    }

    // Find keyframe within 0.5 seconds of current time
    const tolerance = 0.5;
    const matchingAnnotation = window.currentKeyframeAnnotations.find(anno => {
        return Math.abs(anno.timestamp - currentTime) < tolerance;
    });

    if (matchingAnnotation) {
        // Show bboxes for this annotation if not already showing
        if (window.currentDisplayedAnnotationId !== matchingAnnotation.id) {
            window.currentDisplayedAnnotationId = matchingAnnotation.id;
            showAnnotationBBoxesQuiet(matchingAnnotation.id, matchingAnnotation.timestamp);
        }
    } else {
        // No matching annotation, clear bboxes
        if (window.currentDisplayedAnnotationId !== null) {
            window.currentDisplayedAnnotationId = null;
            if (typeof clearCanvas === 'function') {
                clearCanvas();
            }
        }
    }
}

/**
 * Show annotation bboxes without seeking (quiet mode for auto-display)
 */
async function showAnnotationBBoxesQuiet(annotationId, timestamp) {
    // Use shared rendering function without seeking
    await renderAnnotationBBoxes(annotationId);
}

// Removed: closeTagModal, closeKeyframeModal - modals no longer used

function formatDuration(seconds) {
    if (!seconds && seconds !== 0) return 'Unknown';
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    return `${mins}:${secs.toString().padStart(2, '0')}`;
}

function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

window.onclick = function(event) {
    const tagModal = document.getElementById('tag-modal');
    const keyframeModal = document.getElementById('keyframe-modal');

    if (event.target === tagModal) {
        closeTagModal();
    }
    if (event.target === keyframeModal) {
        closeKeyframeModal();
    }
}

videoPlayer = document.getElementById('video-player');

videoPlayer.addEventListener('loadedmetadata', () => {
    resizeCanvas();
    updateTimeDisplay();

    // Load audio waveform (skip in image mode)
    if (typeof loadAudioWaveform === 'function' && !window.isImageMode) {
        loadAudioWaveform(videoPlayer);
    }
});

videoPlayer.addEventListener('timeupdate', updateTimeDisplay);

// Resize canvas when window resizes
window.addEventListener('resize', () => {
    resizeCanvas();
    if (typeof predictionReview !== 'undefined') {
        predictionReview.repositionBboxOverlay();
    }
});

videoPlayer.addEventListener('play', () => {
    document.getElementById('play-btn').textContent = 'Pause';
});

videoPlayer.addEventListener('pause', () => {
    document.getElementById('play-btn').textContent = 'Play';
});

initializeCanvas();
loadVideo();

// ===== Mobile Panel Switching =====

function switchMobilePanel(panel) {
    const videoPanel = document.querySelector('.video-panel');
    const annotationPanel = document.querySelector('.annotation-panel');
    const tabs = document.querySelectorAll('.mobile-panel-tab');

    // Only apply on mobile
    if (window.matchMedia('(min-width: 768px)').matches) return;

    tabs.forEach(t => t.classList.remove('active'));
    document.querySelector(`[data-panel="${panel}"]`).classList.add('active');

    if (panel === 'video') {
        videoPanel.style.display = '';
        annotationPanel.style.display = 'none';
    } else {
        videoPanel.style.display = 'none';
        annotationPanel.style.display = '';
    }
}

// Reset panel visibility on resize to desktop
window.matchMedia('(min-width: 768px)').addEventListener('change', function(e) {
    if (e.matches) {
        const videoPanel = document.querySelector('.video-panel');
        const annotationPanel = document.querySelector('.annotation-panel');
        if (videoPanel) videoPanel.style.display = '';
        if (annotationPanel) annotationPanel.style.display = '';
    }
});

// ===== Landscape Mode: Auto-hide Controls =====

(function() {
    if (!window.matchMedia) return;

    const landscapeQuery = window.matchMedia('(max-width: 767px) and (orientation: landscape)');
    let controlsTimeout;

    function handleLandscape(e) {
        const controls = document.querySelector('.player-controls');
        if (!controls) return;

        if (e.matches) {
            // In landscape - auto-hide controls after 3 seconds
            showControlsBriefly();
            // Tap video wrapper to toggle controls
            const wrapper = document.querySelector('.video-wrapper');
            if (wrapper) {
                wrapper.addEventListener('click', toggleLandscapeControls);
                wrapper.addEventListener('touchend', toggleLandscapeControls);
            }
        } else {
            // Not landscape - ensure controls visible
            controls.classList.remove('controls-hidden');
            clearTimeout(controlsTimeout);
            const wrapper = document.querySelector('.video-wrapper');
            if (wrapper) {
                wrapper.removeEventListener('click', toggleLandscapeControls);
                wrapper.removeEventListener('touchend', toggleLandscapeControls);
            }
        }
    }

    function showControlsBriefly() {
        const controls = document.querySelector('.player-controls');
        if (!controls) return;
        controls.classList.remove('controls-hidden');
        clearTimeout(controlsTimeout);
        controlsTimeout = setTimeout(() => {
            controls.classList.add('controls-hidden');
        }, 3000);
    }

    function toggleLandscapeControls(e) {
        // Don't toggle if we're drawing
        const canvas = document.getElementById('bbox-canvas');
        if (canvas && canvas.classList.contains('drawing')) return;

        // Don't toggle if clicking on a control button
        if (e.target.closest('.player-controls')) return;

        const controls = document.querySelector('.player-controls');
        if (!controls) return;

        if (controls.classList.contains('controls-hidden')) {
            showControlsBriefly();
        } else {
            controls.classList.add('controls-hidden');
            clearTimeout(controlsTimeout);
        }
    }

    landscapeQuery.addEventListener('change', handleLandscape);
    // Check on load
    handleLandscape(landscapeQuery);
})();
