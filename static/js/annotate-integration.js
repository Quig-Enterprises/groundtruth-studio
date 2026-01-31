/**
 * Integration between old annotate.js and new tag-form-generator.js
 * Uses inline panel editing instead of modals
 */

// Store original panel content
let originalPanelContent = null;
let currentEditMode = null; // 'time_range' or 'keyframe'
let currentEditingId = null; // ID of annotation being edited (null for new)

function addNewTimeRangeTag() {
    // Redirect to unified workflow
    scenarioWorkflow.startWorkflow();
}

function startKeyframeAnnotation() {
    // Redirect to unified workflow
    scenarioWorkflow.startWorkflow();
}

async function editTimeRangeTag(tagId) {
    // Fetch the tag data
    const response = await fetch(`/api/time-range-tags/${tagId}`);
    const data = await response.json();

    if (!data.success || !data.tag) {
        alert('Error loading tag for editing');
        return;
    }

    const tag = data.tag;
    currentEditingId = tagId;
    currentEditMode = 'time_range';
    const panel = document.querySelector('.annotation-panel');

    // Save original content
    if (!originalPanelContent) {
        originalPanelContent = panel.innerHTML;
    }

    // Seek to tag start time
    seekToTime(tag.start_time);

    // Create inline form with existing data
    panel.innerHTML = `
        <div class="inline-annotation-form">
            <div class="form-header">
                <h2>Edit Time Range Tag</h2>
                <button onclick="cancelInlineForm()" class="btn-secondary">Cancel</button>
            </div>

            <div class="form-group">
                <label>Start Time (seconds)</label>
                <input type="number" id="tag-start-time" step="0.1" value="${tag.start_time.toFixed(2)}" readonly>
            </div>

            <div class="form-group">
                <label>End Time (seconds) - Optional</label>
                <input type="number" id="tag-end-time" step="0.1" value="${tag.end_time ? tag.end_time.toFixed(2) : ''}" placeholder="Leave empty to close later">
            </div>

            <div class="form-group">
                <label>
                    <input type="checkbox" id="tag-is-negative" ${tag.is_negative ? 'checked' : ''} onchange="handleNegativeCheckboxChange()">
                    Negative Example (mark as "NOT [classification]")
                </label>
                <p class="help-text-small">Use this to tag periods where the behavior explicitly does NOT occur.</p>
            </div>

            <div id="time-range-tag-form"></div>

            <div class="form-actions">
                <button onclick="saveTimeRangeTag()" class="btn-primary">Update Tag</button>
                <button onclick="cancelInlineForm()" class="btn-secondary">Cancel</button>
            </div>
        </div>
    `;

    // Fetch the structured tags for this annotation
    const tagsResponse = await fetch(`/api/annotations/${tagId}/tags?annotation_type=time_range`);
    const tagsData = await tagsResponse.json();

    // Generate dynamic form with existing tag data
    const existingTags = tagsData.success ? tagsData.tags : {};
    tagFormGenerator.generateForm('time_range', 'time-range-tag-form', existingTags.ground_truth || null, tag.is_negative || false);

    // Populate form fields with existing values
    if (tagsData.success && tagsData.tags) {
        setTimeout(() => {
            Object.keys(tagsData.tags).forEach(key => {
                const field = document.getElementById(`tag-${key}`);
                if (field) {
                    if (field.type === 'checkbox') {
                        field.checked = tagsData.tags[key] ? true : false;
                    } else {
                        field.value = tagsData.tags[key] || '';
                    }
                }
            });
        }, 100);
    }

    // Scroll to top of panel
    panel.scrollTop = 0;
}

function handleNegativeCheckboxChange() {
    const isNegative = document.getElementById('tag-is-negative').checked;
    const groundTruthSelect = document.getElementById('tag-ground_truth');
    const groundTruth = groundTruthSelect ? groundTruthSelect.value : null;

    const formId = currentEditMode === 'time_range' ? 'time-range-tag-form' : 'keyframe-tag-form';
    tagFormGenerator.generateForm(currentEditMode, formId, groundTruth, isNegative);
}

function handleKeyframeNegativeCheckboxChange() {
    handleNegativeCheckboxChange();
}

async function saveTimeRangeTag() {
    const startTime = parseFloat(document.getElementById('tag-start-time').value);
    const endTimeInput = document.getElementById('tag-end-time').value.trim();
    const endTime = endTimeInput ? parseFloat(endTimeInput) : null;
    const isNegative = document.getElementById('tag-is-negative').checked;

    const validation = tagFormGenerator.validateForm();
    if (!validation.valid) {
        alert('Please fill in required fields: ' + validation.missingFields.join(', '));
        return;
    }

    const tags = tagFormGenerator.getFormValues();
    const tagName = tags.ground_truth || 'unclassified';

    try {
        let tagId;

        if (currentEditingId) {
            // Update existing tag
            const response = await fetch(`/api/time-range-tags/${currentEditingId}`, {
                method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    tag_name: tagName,
                    end_time: endTime,
                    is_negative: isNegative,
                    comment: tags.reviewer_notes || ''
                })
            });

            const data = await response.json();

            if (!data.success) {
                alert('Error updating tag: ' + (data.error || 'Unknown error'));
                return;
            }

            tagId = currentEditingId;
        } else {
            // Create new tag
            const response = await fetch(`/api/videos/${currentVideoId}/time-range-tags`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    tag_name: tagName,
                    start_time: startTime,
                    end_time: endTime,
                    is_negative: isNegative,
                    comment: tags.reviewer_notes || ''
                })
            });

            const data = await response.json();

            if (!data.success) {
                alert('Error saving tag: ' + (data.error || 'Unknown error'));
                return;
            }

            tagId = data.tag_id;
        }

        // Save/update structured tags
        const tagsResponse = await fetch(`/api/annotations/${tagId}/tags`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                annotation_type: 'time_range',
                tags: tags
            })
        });

        if (tagsResponse.ok) {
            cancelInlineForm();
            loadTimeRangeTags();
        } else {
            alert('Tag saved but structured tags failed to save');
        }
    } catch (error) {
        alert('Error saving tag: ' + error.message);
    }
}

// Old editKeyframeAnnotation function removed - now using scenario workflow editor

async function saveKeyframeAnnotation() {
    if (!currentBBox) {
        alert('No bounding box drawn');
        return;
    }

    const timestamp = parseFloat(document.getElementById('keyframe-timestamp').value);
    const isNegative = document.getElementById('keyframe-is-negative').checked;

    const validation = tagFormGenerator.validateForm();
    if (!validation.valid) {
        alert('Please fill in required fields: ' + validation.missingFields.join(', '));
        return;
    }

    const tags = tagFormGenerator.getFormValues();
    const activityTag = tags.ground_truth || 'unclassified';

    try {
        let annotationId;

        if (currentEditingId) {
            // Update existing annotation (including bbox if it was redrawn)
            const response = await fetch(`/api/keyframe-annotations/${currentEditingId}`, {
                method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    bbox_x: currentBBox.x,
                    bbox_y: currentBBox.y,
                    bbox_width: currentBBox.width,
                    bbox_height: currentBBox.height,
                    activity_tag: activityTag,
                    moment_tag: null,
                    is_negative: isNegative,
                    comment: tags.reviewer_notes || ''
                })
            });

            const data = await response.json();

            if (!data.success) {
                alert('Error updating annotation: ' + (data.error || 'Unknown error'));
                return;
            }

            annotationId = currentEditingId;
        } else {
            // Create new annotation
            const response = await fetch(`/api/videos/${currentVideoId}/keyframe-annotations`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    timestamp: timestamp,
                    bbox_x: currentBBox.x,
                    bbox_y: currentBBox.y,
                    bbox_width: currentBBox.width,
                    bbox_height: currentBBox.height,
                    activity_tag: activityTag,
                    moment_tag: null,
                    is_negative: isNegative,
                    comment: tags.reviewer_notes || ''
                })
            });

            const data = await response.json();

            if (!data.success) {
                alert('Error saving annotation: ' + (data.error || 'Unknown error'));
                return;
            }

            annotationId = data.annotation_id;
        }

        // Save/update structured tags
        const tagsResponse = await fetch(`/api/annotations/${annotationId}/tags`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                annotation_type: 'keyframe',
                tags: tags
            })
        });

        if (tagsResponse.ok) {
            cancelInlineForm();
            loadKeyframeAnnotations();
            currentBBox = null;
            clearBBoxCanvas();
        } else {
            alert('Annotation saved but structured tags failed to save');
        }
    } catch (error) {
        alert('Error saving annotation: ' + error.message);
    }
}

function redrawBoundingBox() {
    // Clear current bbox and enter drawing mode
    clearBBoxCanvas();
    currentBBox = null;

    // Enable drawing mode
    isDrawingMode = true;
    bboxCanvas.classList.add('drawing');

    // Update button text to indicate drawing mode
    const redrawBtn = event.target;
    const originalText = redrawBtn.textContent;
    redrawBtn.textContent = 'Drawing... (click and drag on video)';
    redrawBtn.disabled = true;

    // Set up one-time listener for when bbox is drawn
    const checkForNewBBox = setInterval(() => {
        if (currentBBox) {
            clearInterval(checkForNewBBox);
            isDrawingMode = false;
            bboxCanvas.classList.remove('drawing');

            // Update the bbox input field
            document.getElementById('keyframe-bbox').value =
                `x:${currentBBox.x}, y:${currentBBox.y}, w:${currentBBox.width}, h:${currentBBox.height}`;

            redrawBtn.textContent = originalText;
            redrawBtn.disabled = false;
        }
    }, 100);
}

function cancelInlineForm() {
    if (originalPanelContent) {
        document.querySelector('.annotation-panel').innerHTML = originalPanelContent;
        originalPanelContent = null;
        currentEditMode = null;
        currentEditingId = null;
    }
    clearBBoxCanvas();
    isDrawingMode = false;
}

// Helper functions for bbox canvas
function clearBBoxCanvas() {
    if (bboxContext && bboxCanvas) {
        bboxContext.clearRect(0, 0, bboxCanvas.width, bboxCanvas.height);
    }
}

function drawBBox(x, y, width, height, color = '#e74c3c', label = null, isDotted = false) {
    if (bboxContext) {
        bboxContext.strokeStyle = color;
        bboxContext.lineWidth = 3;

        // Set line dash for AI-generated (unreviewed) annotations
        if (isDotted) {
            bboxContext.setLineDash([10, 5]); // 10px dash, 5px gap
        } else {
            bboxContext.setLineDash([]); // Solid line
        }

        bboxContext.strokeRect(x, y, width, height);

        // Reset line dash
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

            // Add "AI" indicator for unreviewed annotations
            if (isDotted) {
                bboxContext.fillStyle = color;
                bboxContext.fillRect(x + textWidth + padding * 3, y - textHeight - padding, 20, textHeight + padding);
                bboxContext.fillStyle = 'white';
                bboxContext.font = 'bold 10px Arial';
                bboxContext.fillText('AI', x + textWidth + padding * 4, y - padding);
            }
        }
    }
}

// Override display functions to make annotations clickable for editing
window.displayTimeRangeTags = function(tags) {
    const container = document.getElementById('time-range-tags-list');

    if (tags.length === 0) {
        container.innerHTML = '<div class="empty-state">No time range tags yet. Click "+ Tag" to add one.</div>';
        return;
    }

    container.innerHTML = tags.map(tag => {
        const isOpen = tag.end_time === null || tag.end_time === undefined;
        const isNegative = tag.is_negative === 1 || tag.is_negative === true;

        return `
            <div class="time-range-tag-item ${isOpen ? 'open' : ''} ${isNegative ? 'negative' : ''}" onclick="editTimeRangeTag(${tag.id})" style="cursor: pointer;">
                <div class="tag-item-header">
                    <span class="tag-name">${escapeHtml(tag.tag_name)}</span>
                    <span class="tag-time-range">
                        ${formatDuration(tag.start_time)}
                        ${!isOpen ? ` - ${formatDuration(tag.end_time)}` : ' (open)'}
                    </span>
                </div>
                ${tag.comment ? `<div class="tag-comment">"${escapeHtml(tag.comment)}"</div>` : ''}
                <div class="tag-actions" onclick="event.stopPropagation();">
                    <button onclick="seekToTime(${tag.start_time})" class="btn-small btn-seek">Seek Start</button>
                    ${isOpen ? `<button onclick="closeTimeRangeTag(${tag.id})" class="btn-small btn-close-tag">Close at Current Time</button>` : ''}
                    <button onclick="deleteTimeRangeTag(${tag.id})" class="btn-small btn-delete-tag">Delete</button>
                </div>
            </div>
        `;
    }).join('');
};

window.displayKeyframeAnnotations = async function(annotations) {
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
        const isReviewed = anno.reviewed === 1 || anno.reviewed === true;
        const isAiGenerated = !isReviewed;
        const tags = anno.tags || {};
        const scenarioId = tags.scenario || anno.activity_tag;

        // Get notes from tags or comment
        const notes = tags.comment || anno.comment || '';

        return `
        <div class="keyframe-item ${isNegative ? 'negative' : ''} ${isAiGenerated ? 'ai-generated' : ''}" data-annotation-id="${anno.id}" onclick="showAnnotationBBoxes(${anno.id}, ${anno.timestamp})" style="cursor: pointer;">
            <div class="keyframe-header">
                <span class="keyframe-timestamp">${formatDuration(anno.timestamp)}</span>
                ${scenarioId ? `
                    <div class="keyframe-tags">
                        <span class="activity-tag">${escapeHtml(scenarioId.replace(/_/g, ' '))}</span>
                    </div>
                ` : ''}
                <div class="keyframe-header-actions">
                    <button onclick="event.stopPropagation(); editKeyframeAnnotation(${anno.id}, ${anno.timestamp})" class="btn-small">Edit</button>
                    <button onclick="event.stopPropagation(); deleteKeyframeAnnotation(${anno.id})" class="btn-small btn-delete-tag">Delete</button>
                </div>
            </div>
            ${notes ? `<div class="keyframe-comment">${escapeHtml(notes)}</div>` : ''}
            <div class="keyframe-thumbnail" id="thumbnail-${anno.id}"></div>
        </div>
        `;
    }).join('');

    // Generate thumbnails sequentially to avoid race conditions
    for (const anno of annotationsWithTags) {
        await new Promise(resolve => {
            const thumbnailContainer = document.getElementById(`thumbnail-${anno.id}`);
            if (!thumbnailContainer) {
                resolve();
                return;
            }

            const videoPlayer = document.getElementById('video-player');
            const originalTime = videoPlayer.currentTime;

            const canvas = document.createElement('canvas');
            canvas.width = 160;
            canvas.height = 90;
            const ctx = canvas.getContext('2d');

            videoPlayer.currentTime = anno.timestamp;

            const captureFrame = () => {
                ctx.drawImage(videoPlayer, 0, 0, canvas.width, canvas.height);
                const dataUrl = canvas.toDataURL('image/jpeg', 0.7);
                thumbnailContainer.innerHTML = `<img src="${dataUrl}" alt="Thumbnail" style="width: 100%; height: auto; border-radius: 4px;">`;
                videoPlayer.currentTime = originalTime;
                resolve();
            };

            videoPlayer.addEventListener('seeked', captureFrame, { once: true });
        });
    }
};

console.log('[Annotation Integration] Loaded - using scenario-based workflow system');
