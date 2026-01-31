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
        document.getElementById('video-source').src = `/downloads/${video.filename}`;

        document.getElementById('meta-duration').textContent = formatDuration(video.duration);
        document.getElementById('meta-resolution').textContent = `${video.width}x${video.height}`;

        videoPlayer.load();

        loadTimeRangeTags();
        loadKeyframeAnnotations();

    } catch (error) {
        alert('Error loading video: ' + error.message);
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

    container.innerHTML = annotationsWithTags.map(anno => {
        const isNegative = anno.is_negative === 1 || anno.is_negative === true;
        const tags = anno.tags || {};
        const scenarioId = tags.scenario || anno.activity_tag;

        // Get notes from tags or comment
        const notes = tags.comment || anno.comment || '';

        return `
        <div class="keyframe-item ${isNegative ? 'negative' : ''}" onclick="showAnnotationBBoxes(${anno.id}, ${anno.timestamp})" style="cursor: pointer;">
            <div class="keyframe-header">
                <span class="keyframe-timestamp">${formatDuration(anno.timestamp)}</span>
                <div class="keyframe-header-actions">
                    <button onclick="event.stopPropagation(); editKeyframeAnnotation(${anno.id}, ${anno.timestamp})" class="btn-small">Edit</button>
                    <button onclick="event.stopPropagation(); deleteKeyframeAnnotation(${anno.id})" class="btn-small btn-delete-tag">Delete</button>
                </div>
            </div>
            ${scenarioId ? `
                <div class="keyframe-tags">
                    <span class="activity-tag">${escapeHtml(scenarioId.replace(/_/g, ' '))}</span>
                </div>
            ` : ''}
            ${notes ? `<div class="keyframe-comment">${escapeHtml(notes)}</div>` : ''}
            <div class="keyframe-thumbnail" id="thumbnail-${anno.id}"></div>
        </div>
        `;
    }).join('');

    // Generate thumbnails for each annotation
    annotationsWithTags.forEach(anno => {
        generateThumbnail(anno.id, anno.timestamp);
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

    if (isDrawingMode) {
        btn.classList.add('active');
        btn.textContent = 'Cancel Drawing';
        bboxCanvas.classList.add('drawing');
    } else {
        btn.classList.remove('active');
        btn.textContent = 'Draw BBox';
        bboxCanvas.classList.remove('drawing');
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
}

function resizeCanvas() {
    const video = videoPlayer;
    bboxCanvas.width = video.videoWidth || video.offsetWidth;
    bboxCanvas.height = video.videoHeight || video.offsetHeight;
    bboxCanvas.style.width = video.offsetWidth + 'px';
    bboxCanvas.style.height = video.offsetHeight + 'px';
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
    if (!window.isDrawingMode || !drawingStartPos) return;

    const rect = bboxCanvas.getBoundingClientRect();
    const scaleX = bboxCanvas.width / rect.width;
    const scaleY = bboxCanvas.height / rect.height;

    // Clamp current position to canvas bounds for preview
    let currentX = (e.clientX - rect.left) * scaleX;
    let currentY = (e.clientY - rect.top) * scaleY;
    currentX = Math.max(0, Math.min(bboxCanvas.width, currentX));
    currentY = Math.max(0, Math.min(bboxCanvas.height, currentY));

    clearCanvas();
    drawBBox(drawingStartPos.x, drawingStartPos.y, currentX - drawingStartPos.x, currentY - drawingStartPos.y);
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

function drawBBox(x, y, width, height, color = '#e74c3c', label = null) {
    bboxContext.strokeStyle = color;
    bboxContext.lineWidth = 3;
    bboxContext.strokeRect(x, y, width, height);

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

function generateThumbnail(annotationId, timestamp) {
    const thumbnailContainer = document.getElementById(`thumbnail-${annotationId}`);
    if (!thumbnailContainer) return;

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
        // Draw video frame to canvas
        ctx.drawImage(videoPlayer, 0, 0, canvas.width, canvas.height);

        // Convert to data URL and create img element
        const dataUrl = canvas.toDataURL('image/jpeg', 0.7);
        thumbnailContainer.innerHTML = `<img src="${dataUrl}" alt="Thumbnail" style="width: 100%; height: auto; border-radius: 4px;">`;

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

    // Load audio waveform
    if (typeof loadAudioWaveform === 'function') {
        loadAudioWaveform(videoPlayer);
    }
});

videoPlayer.addEventListener('timeupdate', updateTimeDisplay);

videoPlayer.addEventListener('play', () => {
    document.getElementById('play-btn').textContent = 'Pause';
});

videoPlayer.addEventListener('pause', () => {
    document.getElementById('play-btn').textContent = 'Play';
});

initializeCanvas();
loadVideo();
