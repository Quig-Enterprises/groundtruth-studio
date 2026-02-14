/**
 * Scenario-based Annotation Workflow Controller
 * Manages multi-step annotation processes
 */

class ScenarioWorkflow {
    constructor() {
        this.currentScenario = null;
        this.currentStep = 0;
        this.annotationType = null; // 'keyframe' or 'time_range'
        this.collectedData = {
            bboxes: {},      // { step_id: {x, y, width, height} }
            tags: {},        // { tag_id: value }
            notVisible: [],  // Array of step_ids marked as "not visible"
            notPresent: [],  // Array of step_ids marked as "not present"
            skipped: []      // Array of step_ids that were skipped
        };
        this.startTime = null;
    }

    /**
     * Start a new annotation workflow
     */
    startWorkflow() {
        const currentTime = videoPlayer ? videoPlayer.currentTime : 0;
        console.log('[Scenario Workflow] Starting workflow at time:', currentTime);

        // Pause video to prevent it from moving
        if (videoPlayer) videoPlayer.pause();

        this.reset();

        // Calculate frame-precise timestamp (assuming 30fps, or use video metadata)
        // For most videos, 30fps = 0.0333s per frame, 60fps = 0.0167s per frame
        // We'll use a precision of 0.001s (1ms) which is more than sufficient for any frame rate
        this.startTime = Math.round(currentTime * 1000) / 1000;

        // Extract current frame as image
        this.extractCurrentFrame();

        // Hide manual Draw BBox button during scenario workflow
        const drawBtn = document.getElementById('draw-mode-btn');
        if (drawBtn) {
            drawBtn.style.display = 'none';
        }

        // Show scenario selection screen
        this.showScenarioSelection();
    }

    /**
     * Extract current video frame as image
     */
    extractCurrentFrame() {
        console.log('[Scenario Workflow] Extracting current frame');

        // In image mode, use the thumbnail image element directly
        const sourceElement = window.isImageMode
            ? document.getElementById('thumbnail-image')
            : videoPlayer;

        if (!sourceElement) {
            console.error('[Scenario Workflow] Source element not found');
            return;
        }

        // Create a temporary canvas to capture the frame
        const tempCanvas = document.createElement('canvas');
        if (window.isImageMode) {
            tempCanvas.width = sourceElement.naturalWidth || sourceElement.offsetWidth;
            tempCanvas.height = sourceElement.naturalHeight || sourceElement.offsetHeight;
        } else {
            tempCanvas.width = videoPlayer.videoWidth || videoPlayer.offsetWidth;
            tempCanvas.height = videoPlayer.videoHeight || videoPlayer.offsetHeight;
        }
        const tempCtx = tempCanvas.getContext('2d');

        // Draw current frame (works for both img and video elements)
        tempCtx.drawImage(sourceElement, 0, 0, tempCanvas.width, tempCanvas.height);

        // Convert to data URL
        this.extractedFrameData = tempCanvas.toDataURL('image/jpeg', 0.95);

        console.log('[Scenario Workflow] Frame extracted:', tempCanvas.width, 'x', tempCanvas.height);
    }

    /**
     * Show extracted frame image (hide video)
     */
    showExtractedFrame() {
        if (!this.extractedFrameData) {
            console.warn('[Scenario Workflow] No extracted frame data');
            return;
        }

        const videoWrapper = document.querySelector('.video-wrapper');
        if (!videoWrapper) return;

        console.log('[Scenario Workflow] Video currentTime before hiding:', videoPlayer.currentTime);

        // Create or update frame image element
        let frameImg = document.getElementById('scenario-frame-image');
        if (!frameImg) {
            frameImg = document.createElement('img');
            frameImg.id = 'scenario-frame-image';
            if (window.isImageMode) {
                frameImg.style.cssText = 'width: 100%; height: 100%; object-fit: contain; display: none;';
            } else {
                frameImg.style.cssText = 'position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); max-width: 100%; max-height: 100%; display: none;';
            }
            videoWrapper.appendChild(frameImg);
        }

        frameImg.src = this.extractedFrameData;
        frameImg.style.display = 'block';

        // Hide the source element
        if (window.isImageMode) {
            const thumbImg = document.getElementById('thumbnail-image');
            if (thumbImg) thumbImg.style.display = 'none';
        }
        videoPlayer.style.display = 'none';

        console.log('[Scenario Workflow] Showing extracted frame, hiding video');
        console.log('[Scenario Workflow] Video currentTime after hiding:', videoPlayer.currentTime);
    }

    /**
     * Restore video display (hide extracted frame)
     */
    restoreVideoDisplay() {
        const frameImg = document.getElementById('scenario-frame-image');
        if (frameImg) {
            frameImg.style.display = 'none';
        }

        if (window.isImageMode) {
            const thumbImg = document.getElementById('thumbnail-image');
            if (thumbImg) thumbImg.style.display = 'block';
        }

        if (videoPlayer) {
            videoPlayer.style.display = 'block';
        }

        console.log('[Scenario Workflow] Restored video display');
    }

    /**
     * Reset workflow state
     */
    reset() {
        this.currentScenario = null;
        this.currentStep = 0;
        this.annotationType = null;
        this.collectedData = {
            bboxes: {},
            tags: {},
            notVisible: [],
            notPresent: [],
            skipped: []
        };
        this.startTime = null;
        this.annotationIdBeingEdited = null;
        clearBBoxCanvas();
        if (window.isDrawingMode) {
            window.isDrawingMode = false;
            document.getElementById('draw-mode-btn')?.classList.remove('active');
            bboxCanvas?.classList.remove('drawing');
        }
    }

    /**
     * Show scenario selection screen
     */
    showScenarioSelection() {
        const panel = document.querySelector('.annotation-panel');

        // Save original content if not already saved
        if (!originalPanelContent) {
            originalPanelContent = panel.innerHTML;
        }

        // Get scenarios organized by category, sorted by recent usage
        const categorized = scenarioTracker.getSortedByCategory();

        let scenariosHTML = '';
        for (const [category, scenarioIds] of Object.entries(categorized)) {
            scenariosHTML += `<div class="scenario-category">
                <h3 class="category-title">${category}</h3>
                <div class="scenario-grid">`;

            scenarioIds.forEach(scenarioId => {
                const scenario = annotationScenarios[scenarioId];
                const iconClass = scenario.requiresBoundingBox ? 'bbox-icon' :
                                 scenario.appliesToEntireClip ? 'clip-icon' : 'event-icon';

                scenariosHTML += `
                    <button class="scenario-card ${iconClass}" onclick="scenarioWorkflow.selectScenario('${scenarioId}')">
                        <div class="scenario-title">${scenario.label}</div>
                        <div class="scenario-description">${scenario.description}</div>
                    </button>
                `;
            });

            scenariosHTML += `</div></div>`;
        }

        panel.innerHTML = `
            <div class="inline-annotation-form">
                <div class="form-header">
                    <h2>New Annotation</h2>
                    <button onclick="scenarioWorkflow.cancel()" class="btn-secondary">Cancel</button>
                </div>

                <div class="scenario-selection">
                    <p class="help-text-small">Select the type of annotation you want to create:</p>
                    ${scenariosHTML}
                </div>
            </div>
        `;

        panel.scrollTop = 0;
    }

    /**
     * User selects a scenario
     */
    selectScenario(scenarioId) {
        console.log('[Scenario Workflow] Selected scenario:', scenarioId);
        this.currentScenario = JSON.parse(JSON.stringify(annotationScenarios[scenarioId]));
        this.currentScenario.id = scenarioId;

        // Track usage
        scenarioTracker.trackUsage(scenarioId);

        // Set annotation type based on scenario configuration
        // Scenarios with bboxes are keyframe annotations (single moment)
        // Scenarios without bboxes might be time-range or whole-clip
        if (this.currentScenario.requiresBoundingBox) {
            this.annotationType = 'keyframe';
        } else if (this.currentScenario.appliesToEntireClip) {
            this.annotationType = 'time_range'; // Whole clip is a time range
        } else {
            this.annotationType = 'keyframe'; // Default to keyframe for moment annotations
        }

        console.log('[Scenario Workflow] Annotation type:', this.annotationType);

        // Show extracted frame instead of video
        this.showExtractedFrame();

        // Determine workflow based on scenario configuration
        if (this.currentScenario.requiresBoundingBox) {
            // Start multi-step bbox workflow
            this.currentStep = 0;
            this.showBBoxStep();
        } else {
            // Whole-clip annotation - go straight to tags (skip event boundaries)
            this.showTagForm();
        }
    }

    /**
     * Capture current tag form values from DOM into collectedData.tags
     * so they survive panel re-renders.
     */
    captureCurrentTagValues() {
        if (!this.currentScenario?.tags) return;
        const tags = this.currentScenario.tags;
        for (const [tagId, tagConfig] of Object.entries(tags)) {
            if (tagConfig.type === 'checkbox') {
                const checkboxes = document.querySelectorAll(`input[name="tag-${tagId}"]:checked`);
                if (checkboxes.length > 0) {
                    this.collectedData.tags[tagId] = Array.from(checkboxes).map(cb => cb.value);
                }
            } else if (tagConfig.type === 'configurable_dropdown') {
                const select = document.getElementById(`tag-${tagId}`);
                const customInput = document.getElementById(`tag-${tagId}-custom`);
                if (select && select.value === '__custom__' && customInput && customInput.value) {
                    this.collectedData.tags[tagId] = customInput.value;
                } else if (select && select.value) {
                    this.collectedData.tags[tagId] = select.value;
                }
            } else if (tagConfig.type === 'dropdown' && tagConfig.allowCustom) {
                const select = document.getElementById(`tag-${tagId}`);
                const customInput = document.getElementById(`tag-${tagId}-custom`);
                if (select && select.value === '__custom__' && customInput && customInput.value) {
                    this.collectedData.tags[tagId] = customInput.value;
                } else if (select && select.value) {
                    this.collectedData.tags[tagId] = select.value;
                }
            } else {
                const element = document.getElementById(`tag-${tagId}`);
                if (element && element.value) {
                    this.collectedData.tags[tagId] = element.value;
                }
            }
        }
        // Capture notes too
        const notesEl = document.getElementById('scenario-notes');
        if (notesEl && notesEl.value) {
            this.collectedData.notes = notesEl.value;
        }
    }

    /**
     * Restore tag form values from collectedData.tags after panel re-render.
     */
    restoreTagValues() {
        if (!this.currentScenario?.tags) return;
        const tags = this.currentScenario.tags;
        for (const [tagId, tagConfig] of Object.entries(tags)) {
            const value = this.collectedData.tags[tagId];
            if (value === undefined || value === null || value === '') continue;

            if (tagConfig.type === 'checkbox') {
                if (Array.isArray(value)) {
                    value.forEach(v => {
                        const cb = document.querySelector(`input[name="tag-${tagId}"][value="${v}"]`);
                        if (cb) cb.checked = true;
                    });
                }
            } else if (tagConfig.type === 'configurable_dropdown' || (tagConfig.type === 'dropdown' && tagConfig.allowCustom)) {
                const select = document.getElementById(`tag-${tagId}`);
                if (select) {
                    // Check if value is one of the options
                    const optionExists = Array.from(select.options).some(o => o.value === value);
                    if (optionExists) {
                        select.value = value;
                    } else {
                        // Set to custom
                        select.value = '__custom__';
                        const customInput = document.getElementById(`tag-${tagId}-custom`);
                        if (customInput) {
                            customInput.value = value;
                            customInput.style.display = 'block';
                        }
                    }
                }
            } else {
                const element = document.getElementById(`tag-${tagId}`);
                if (element) {
                    element.value = value;
                }
            }
        }
    }

    /**
     * Show bbox collection with checklist interface
     */
    showBBoxStep() {
        // Capture current form values before re-rendering
        this.captureCurrentTagValues();

        const panel = document.querySelector('.annotation-panel');
        const currentStep = this.currentScenario.steps[this.currentStep];

        // Build checklist HTML
        let checklistHTML = '';
        for (let i = 0; i < this.currentScenario.steps.length; i++) {
            const step = this.currentScenario.steps[i];
            const hasBBox = this.collectedData.bboxes[step.id];
            const isNotVisible = this.collectedData.notVisible.includes(step.id);
            const isNotPresent = this.collectedData.notPresent?.includes(step.id);
            const isSkipped = this.collectedData.skipped?.includes(step.id);
            const isCurrent = i === this.currentStep;

            let statusIcon = '☐';
            let statusClass = 'pending';
            let statusText = '';

            if (hasBBox) {
                statusIcon = '☑';
                statusClass = 'completed';
                statusText = `(${hasBBox.width}x${hasBBox.height})`;
            } else if (isNotVisible) {
                statusIcon = '☑';
                statusClass = 'not-visible';
                statusText = '(not visible)';
            } else if (isNotPresent) {
                statusIcon = '☑';
                statusClass = 'not-present';
                statusText = '(not present)';
            } else if (isSkipped) {
                statusIcon = '☑';
                statusClass = 'skipped';
                statusText = '(skipped)';
            }

            checklistHTML += `
                <div class="checklist-item ${statusClass} ${isCurrent ? 'current' : ''}"
                     data-step-index="${i}"
                     onclick="scenarioWorkflow.switchToStep(${i})"
                     style="cursor: pointer;">
                    <span class="checklist-icon">${statusIcon}</span>
                    <span class="checklist-label">${step.label}</span>
                    <span class="checklist-status">${statusText}</span>
                    ${(hasBBox || isNotVisible || isNotPresent || isSkipped) ? `
                        <button onclick="event.stopPropagation(); scenarioWorkflow.deleteBBoxStep(${i})" class="btn-tiny btn-delete-tag">Delete</button>
                    ` : ''}
                </div>
            `;
        }

        panel.innerHTML = `
            <div class="inline-annotation-form">
                <div class="form-header">
                    <h2>${this.currentScenario.label}</h2>
                    <button onclick="scenarioWorkflow.cancel()" class="btn-secondary">Cancel</button>
                </div>

                <div class="bbox-checklist">
                    <h3>Bounding Boxes</h3>
                    ${checklistHTML}
                    ${this.currentScenario.allowDynamicSteps ? `
                        <button onclick="scenarioWorkflow.addDynamicStep()" class="btn-small" style="margin-top: 8px; width: 100%;">+ Add ${this.currentScenario.dynamicStepTemplate?.label || 'Item'}</button>
                    ` : ''}
                </div>

                <div class="bbox-current-step">
                    <h3>${currentStep.label}</h3>
                    <p class="step-prompt">${currentStep.prompt}</p>

                    ${this.collectedData.bboxes[currentStep.id] ? `
                        <div class="bbox-status-message">
                            ✓ Bounding box drawn
                        </div>
                    ` : this.collectedData.notVisible.includes(currentStep.id) ? `
                        <div class="bbox-status-message not-visible">
                            ⊘ Marked as not visible
                        </div>
                    ` : this.collectedData.notPresent?.includes(currentStep.id) ? `
                        <div class="bbox-status-message not-visible">
                            ⊘ Marked as not present
                        </div>
                    ` : this.collectedData.skipped?.includes(currentStep.id) ? `
                        <div class="bbox-status-message not-visible">
                            ⊘ Skipped
                        </div>
                    ` : `
                        <div class="step-instructions">
                            <p><strong>Click and drag</strong> on the video frame to draw the bounding box.</p>
                            ${currentStep.optional ? '<p class="help-text-small"><em>This step is optional.</em></p>' : ''}
                        </div>
                    `}

                    <div class="step-actions">
                        ${currentStep.notVisibleOption ? `<button onclick="scenarioWorkflow.markNotVisible()" class="btn-secondary">Not Visible</button>` : ''}
                        ${currentStep.notPresentOption ? `<button onclick="scenarioWorkflow.markNotPresent()" class="btn-secondary">Not Present</button>` : ''}
                        ${currentStep.optional ? `<button onclick="scenarioWorkflow.skipStep()" class="btn-secondary">Skip</button>` : ''}
                        ${this.collectedData.bboxes[currentStep.id] ? `<button onclick="scenarioWorkflow.redrawBBox()" class="btn-secondary">Redraw</button>` : ''}
                    </div>
                </div>

                ${this.buildTagsHTML()}

                <div class="form-group">
                    <label>Notes (optional)</label>
                    <textarea id="scenario-notes" rows="3" placeholder="Add any observations or notes about this annotation...">${this.collectedData.notes || ''}</textarea>
                </div>

                <div class="form-actions">
                    <button onclick="saveAndClose()" class="btn-primary" id="save-annotation-btn">Save & Close</button>
                    <button onclick="saveAndNext()" class="btn-primary" id="save-next-btn" style="background: #27ae60;">Save & Next</button>
                    <button onclick="scenarioWorkflow.cancel()" class="btn-secondary">Cancel</button>
                </div>
            </div>
        `;

        // Restore tag values that were captured before re-render
        this.restoreTagValues();

        panel.scrollTop = 0;

        // Redraw all bboxes on canvas
        this.drawAllBBoxes();

        // Enable drawing mode if current step doesn't have data
        const hasData = this.collectedData.bboxes[currentStep.id] ||
                       this.collectedData.notVisible.includes(currentStep.id) ||
                       this.collectedData.notPresent?.includes(currentStep.id) ||
                       this.collectedData.skipped?.includes(currentStep.id);

        if (!hasData) {
            console.log('[Scenario Workflow] Enabling bbox drawing mode for step:', currentStep.id);
            window.isDrawingMode = true;
            if (window.bboxCanvas) {
                window.bboxCanvas.classList.add('drawing');
            }
            this.waitForBBox(currentStep.id);
        } else {
            window.isDrawingMode = false;
            if (window.bboxCanvas) {
                window.bboxCanvas.classList.remove('drawing');
            }
        }

        // Check if all required steps are complete
        this.updateSaveButtonState();
    }

    /**
     * Update save button enabled/disabled state
     */
    updateSaveButtonState() {
        const saveBtn = document.getElementById('save-annotation-btn');
        if (!saveBtn) return;

        // Check if all required (non-optional) steps have data
        let allRequiredComplete = true;
        for (const step of this.currentScenario.steps) {
            const hasData = this.collectedData.bboxes[step.id] ||
                           this.collectedData.notVisible.includes(step.id) ||
                           this.collectedData.notPresent?.includes(step.id) ||
                           this.collectedData.skipped?.includes(step.id);

            // If required step has no data, mark as incomplete
            if (!step.optional && !hasData) {
                allRequiredComplete = false;
                break;
            }
        }

        saveBtn.disabled = !allRequiredComplete;
        const saveNextBtn = document.getElementById('save-next-btn');
        if (saveNextBtn) saveNextBtn.disabled = !allRequiredComplete;
        if (!allRequiredComplete) {
            saveBtn.title = 'Complete all required bounding boxes first';
            if (saveNextBtn) saveNextBtn.title = 'Complete all required bounding boxes first';
        } else {
            saveBtn.title = '';
            if (saveNextBtn) saveNextBtn.title = '';
        }
    }

    /**
     * Wait for user to draw bounding box
     */
    waitForBBox(stepId) {
        console.log('[Scenario Workflow] Waiting for bbox to be drawn for step:', stepId);

        // Clean up any previous polling interval (backward compat)
        if (this.bboxWaitInterval) {
            clearInterval(this.bboxWaitInterval);
            this.bboxWaitInterval = null;
        }

        // Use direct callback instead of polling to avoid race condition
        // where drawing mode gets disabled between mouseUp and poll tick
        window.onBBoxDrawn = (bbox) => {
            if (!bbox || bbox.width <= 5 || bbox.height <= 5) return;

            window.onBBoxDrawn = null; // One-shot callback

            console.log('[Scenario Workflow] BBox drawn (callback):', bbox);

            // Save bbox
            this.collectedData.bboxes[stepId] = {
                x: bbox.x,
                y: bbox.y,
                width: bbox.width,
                height: bbox.height
            };
            console.log('[Scenario Workflow] BBox saved for step:', stepId, this.collectedData.bboxes[stepId]);
            console.log('[Scenario Workflow] Total bboxes collected:', Object.keys(this.collectedData.bboxes).length, Object.keys(this.collectedData.bboxes));

            // Disable drawing mode
            window.isDrawingMode = false;
            const drawBtn = document.getElementById('draw-mode-btn');
            if (drawBtn) {
                drawBtn.classList.remove('active');
            }
            if (window.bboxCanvas) {
                window.bboxCanvas.classList.remove('drawing');
            }
            window.currentBBox = null;

            // Auto-advance to next pending step
            console.log('[Scenario Workflow] Auto-advancing to next pending step after bbox drawn');
            this.autoAdvanceToNextPending();
        };
    }

    /**
     * Mark current step as "not visible"
     */
    markNotVisible() {
        const step = this.currentScenario.steps[this.currentStep];
        console.log('[Scenario Workflow] Marking step as not visible:', step.id);

        // Delete any existing bbox for this step
        delete this.collectedData.bboxes[step.id];

        // Add to not visible list if not already there
        if (!this.collectedData.notVisible.includes(step.id)) {
            this.collectedData.notVisible.push(step.id);
        }
        console.log('[Scenario Workflow] Total not visible items:', this.collectedData.notVisible.length, this.collectedData.notVisible);

        // Cancel bbox waiting
        if (this.bboxWaitInterval) {
            clearInterval(this.bboxWaitInterval);
            this.bboxWaitInterval = null;
        }
        window.onBBoxDrawn = null;

        // Clear current bbox being drawn
        window.currentBBox = null;
        if (typeof clearBBoxCanvas === 'function') {
            clearBBoxCanvas();
        }

        // Disable drawing mode
        window.isDrawingMode = false;
        if (window.bboxCanvas) {
            window.bboxCanvas.classList.remove('drawing');
        }

        // Auto-advance to next pending step
        console.log('[Scenario Workflow] Auto-advancing after marking not visible');
        this.autoAdvanceToNextPending();
    }

    /**
     * Mark step as not present
     */
    markNotPresent() {
        const step = this.currentScenario.steps[this.currentStep];
        console.log('[Scenario Workflow] Marking step as not present:', step.id);

        // Delete any existing bbox for this step
        delete this.collectedData.bboxes[step.id];

        // Add to not present list if not already there
        if (!this.collectedData.notPresent.includes(step.id)) {
            this.collectedData.notPresent.push(step.id);
        }

        // Cancel bbox waiting
        if (this.bboxWaitInterval) {
            clearInterval(this.bboxWaitInterval);
            this.bboxWaitInterval = null;
        }
        window.onBBoxDrawn = null;

        // Clear current bbox being drawn
        window.currentBBox = null;
        if (typeof clearBBoxCanvas === 'function') {
            clearBBoxCanvas();
        }

        // Disable drawing mode
        window.isDrawingMode = false;
        if (window.bboxCanvas) {
            window.bboxCanvas.classList.remove('drawing');
        }

        // Auto-advance to next pending step
        this.autoAdvanceToNextPending();
    }

    /**
     * Skip optional step
     */
    skipStep() {
        const step = this.currentScenario.steps[this.currentStep];
        console.log('[Scenario Workflow] Skipping optional step:', step.id);

        // Delete any existing bbox for this step
        delete this.collectedData.bboxes[step.id];

        // Add to skipped list if not already there
        if (!this.collectedData.skipped.includes(step.id)) {
            this.collectedData.skipped.push(step.id);
        }

        // Cancel bbox waiting
        if (this.bboxWaitInterval) {
            clearInterval(this.bboxWaitInterval);
            this.bboxWaitInterval = null;
        }
        window.onBBoxDrawn = null;

        // Clear current bbox being drawn
        window.currentBBox = null;
        if (typeof clearBBoxCanvas === 'function') {
            clearBBoxCanvas();
        }

        // Disable drawing mode
        window.isDrawingMode = false;
        if (window.bboxCanvas) {
            window.bboxCanvas.classList.remove('drawing');
        }

        // Auto-advance to next pending step
        this.autoAdvanceToNextPending();
    }

    /**
     * Move to next step
     */
    nextStep() {
        console.log('[Scenario Workflow] Moving to next step from:', this.currentStep);
        this.currentStep++;

        if (this.currentStep < this.currentScenario.steps.length) {
            // More bbox steps
            this.showBBoxStep();
        } else {
            // All bbox steps done, stay on checklist (don't switch to tag form)
            console.log('[Scenario Workflow] All bbox steps complete, staying on checklist');
            this.currentStep = this.currentScenario.steps.length - 1; // Stay on last step
            this.showBBoxStep();
        }
    }

    /**
     * Move to previous step
     */
    previousStep() {
        console.log('[Scenario Workflow] Moving to previous step from:', this.currentStep);
        if (this.currentStep > 0) {
            this.currentStep--;
            this.showBBoxStep();
        }
    }

    /**
     * Redraw bbox for current step
     */
    redrawBBox() {
        const step = this.currentScenario.steps[this.currentStep];
        console.log('[Scenario Workflow] Redrawing bbox for step:', step.id);

        // Remove existing bbox data for this step
        delete this.collectedData.bboxes[step.id];

        // Remove from notVisible list if it was there
        const notVisibleIndex = this.collectedData.notVisible.indexOf(step.id);
        if (notVisibleIndex > -1) {
            this.collectedData.notVisible.splice(notVisibleIndex, 1);
        }

        // Remove from skipped list if it was there
        const skippedIndex = this.collectedData.skipped?.indexOf(step.id);
        if (skippedIndex > -1) {
            this.collectedData.skipped.splice(skippedIndex, 1);
        }

        // Refresh the step to enable drawing mode
        this.showBBoxStep();
    }

    /**
     * Switch to a different step in the checklist
     */
    switchToStep(stepIndex) {
        console.log('[Scenario Workflow] Switching to step:', stepIndex);

        // Cancel any bbox waiting from previous step
        if (this.bboxWaitInterval) {
            clearInterval(this.bboxWaitInterval);
            this.bboxWaitInterval = null;
        }
        window.onBBoxDrawn = null;

        // Clear any partially drawn bbox
        window.currentBBox = null;

        // Disable drawing mode
        window.isDrawingMode = false;
        if (window.bboxCanvas) {
            window.bboxCanvas.classList.remove('drawing');
        }

        // Switch to new step
        this.currentStep = stepIndex;
        this.showBBoxStep();
    }

    /**
     * Edit a bbox step from the checklist (alias for switchToStep)
     */
    editBBoxStep(stepIndex) {
        this.switchToStep(stepIndex);
    }

    /**
     * Delete a bbox step from the checklist
     */
    deleteBBoxStep(stepIndex) {
        const step = this.currentScenario.steps[stepIndex];
        console.log('[Scenario Workflow] Deleting bbox step:', step.id);

        // Remove from all collections
        delete this.collectedData.bboxes[step.id];

        const notVisibleIndex = this.collectedData.notVisible.indexOf(step.id);
        if (notVisibleIndex > -1) {
            this.collectedData.notVisible.splice(notVisibleIndex, 1);
        }

        const notPresentIndex = this.collectedData.notPresent?.indexOf(step.id);
        if (notPresentIndex > -1) {
            this.collectedData.notPresent.splice(notPresentIndex, 1);
        }

        const skippedIndex = this.collectedData.skipped?.indexOf(step.id);
        if (skippedIndex > -1) {
            this.collectedData.skipped.splice(skippedIndex, 1);
        }

        // Refresh display
        this.showBBoxStep();
    }

    /**
     * Add a dynamic step (for scenarios that allow arbitrary additional items)
     */
    addDynamicStep() {
        if (!this.currentScenario.allowDynamicSteps || !this.currentScenario.dynamicStepTemplate) {
            console.error('[Scenario Workflow] This scenario does not support dynamic steps');
            return;
        }

        const template = this.currentScenario.dynamicStepTemplate;

        // Count existing dynamic steps to generate unique ID
        const existingDynamicSteps = this.currentScenario.steps.filter(s =>
            s.id.startsWith(template.idPrefix)
        );
        const nextNumber = existingDynamicSteps.length + 1;

        // Create new step
        const newStep = {
            id: `${template.idPrefix}${nextNumber}`,
            label: `${template.label} ${nextNumber}`,
            prompt: template.prompt,
            optional: template.optional,
            notVisibleOption: template.notVisibleOption,
            isDynamic: true
        };

        // Add to steps array
        this.currentScenario.steps.push(newStep);

        // Switch to the new step
        this.currentStep = this.currentScenario.steps.length - 1;
        this.showBBoxStep();

        console.log('[Scenario Workflow] Added dynamic step:', newStep);
    }

    /**
     * Auto-advance to next pending step
     */
    autoAdvanceToNextPending() {
        // Find next step that doesn't have data
        for (let i = this.currentStep + 1; i < this.currentScenario.steps.length; i++) {
            const step = this.currentScenario.steps[i];
            const hasData = this.collectedData.bboxes[step.id] ||
                           this.collectedData.notVisible.includes(step.id) ||
                           this.collectedData.notPresent?.includes(step.id) ||
                           this.collectedData.skipped?.includes(step.id);

            if (!hasData) {
                this.currentStep = i;
                this.showBBoxStep();
                return;
            }
        }

        // No pending steps found, stay on current or just refresh
        this.showBBoxStep();
    }

    /**
     * Finish bbox collection and go to review
     */
    finishBBoxCollection() {
        console.log('[Scenario Workflow] Finishing bbox collection');
        this.showTagForm();
    }

    /**
     * Draw all collected bboxes on canvas with labels
     */
    drawAllBBoxes() {
        if (!window.bboxCanvas || !window.bboxContext) {
            console.warn('[Scenario Workflow] Canvas not available for drawing');
            return;
        }

        // Clear canvas first
        if (typeof clearBBoxCanvas === 'function') {
            clearBBoxCanvas();
        }

        const ctx = window.bboxContext;
        const bboxCount = Object.keys(this.collectedData.bboxes).length;
        console.log('[Scenario Workflow] Drawing', bboxCount, 'bboxes on canvas',
            window.bboxCanvas.width, 'x', window.bboxCanvas.height,
            'visible:', window.bboxCanvas.style.display,
            'pointerEvents:', window.bboxCanvas.style.pointerEvents);

        // Draw each bbox with label
        for (const [stepId, bbox] of Object.entries(this.collectedData.bboxes)) {
            // Find step label
            const step = this.currentScenario.steps.find(s => s.id === stepId);
            const label = step ? step.label : stepId;

            console.log('[Scenario Workflow] Drawing bbox:', label, bbox);

            // Draw bbox
            ctx.strokeStyle = '#3498db';
            ctx.lineWidth = 2;
            ctx.strokeRect(bbox.x, bbox.y, bbox.width, bbox.height);

            // Draw label background
            ctx.font = '12px Arial';
            const textMetrics = ctx.measureText(label);
            const textWidth = textMetrics.width;
            const padding = 4;

            ctx.fillStyle = '#3498db';
            ctx.fillRect(bbox.x, bbox.y - 20, textWidth + padding * 2, 18);

            // Draw label text
            ctx.fillStyle = '#ffffff';
            ctx.fillText(label, bbox.x + padding, bbox.y - 6);
        }
    }

    /**
     * Show event boundary selection
     */
    showEventBoundarySelection() {
        const panel = document.querySelector('.annotation-panel');

        panel.innerHTML = `
            <div class="inline-annotation-form">
                <div class="form-header">
                    <h2>${this.currentScenario.label} - Event Timing</h2>
                    <button onclick="scenarioWorkflow.cancel()" class="btn-secondary">Cancel</button>
                </div>

                <div class="event-boundary-content">
                    <p class="help-text-small">${this.currentScenario.eventBoundaryPrompt || 'Define when this event occurs:'}</p>

                    <div class="boundary-options">
                        <button class="boundary-option-card" onclick="scenarioWorkflow.setWholeClip()">
                            <div class="option-title">Entire Clip</div>
                            <div class="option-description">This applies to the whole video</div>
                        </button>

                        <button class="boundary-option-card" onclick="scenarioWorkflow.setCurrentMoment()">
                            <div class="option-title">Current Moment</div>
                            <div class="option-description">Just this point in time (${videoPlayer.currentTime.toFixed(1)}s)</div>
                        </button>

                        <button class="boundary-option-card" onclick="scenarioWorkflow.defineTimeRange()">
                            <div class="option-title">Time Range</div>
                            <div class="option-description">Mark start and end times</div>
                        </button>
                    </div>
                </div>
            </div>
        `;

        panel.scrollTop = 0;
    }

    /**
     * Set annotation to apply to whole clip
     */
    setWholeClip() {
        this.collectedData.startTime = 0;
        this.collectedData.endTime = videoPlayer.duration;
        this.collectedData.boundaryType = 'whole_clip';
        this.showTagForm();
    }

    /**
     * Set annotation to current moment only
     */
    setCurrentMoment() {
        this.collectedData.startTime = videoPlayer.currentTime;
        this.collectedData.endTime = null;
        this.collectedData.boundaryType = 'moment';
        this.showTagForm();
    }

    /**
     * Show time range selection
     */
    defineTimeRange() {
        const panel = document.querySelector('.annotation-panel');

        panel.innerHTML = `
            <div class="inline-annotation-form">
                <div class="form-header">
                    <h2>${this.currentScenario.label} - Time Range</h2>
                    <button onclick="scenarioWorkflow.cancel()" class="btn-secondary">Cancel</button>
                </div>

                <div class="time-range-content">
                    <div class="form-group">
                        <label>Start Time (seconds)</label>
                        <input type="number" id="range-start-time" step="0.1" value="${videoPlayer.currentTime.toFixed(2)}">
                        <button type="button" onclick="document.getElementById('range-start-time').value = videoPlayer.currentTime.toFixed(2)" class="btn-secondary" style="margin-top: 5px; font-size: 12px;">Set to Current Time</button>
                    </div>

                    <div class="form-group">
                        <label>End Time (seconds)</label>
                        <input type="number" id="range-end-time" step="0.1" placeholder="Leave empty to set later">
                        <button type="button" onclick="document.getElementById('range-end-time').value = videoPlayer.currentTime.toFixed(2)" class="btn-secondary" style="margin-top: 5px; font-size: 12px;">Set to Current Time</button>
                    </div>

                    <div class="form-actions">
                        <button onclick="scenarioWorkflow.saveTimeRange()" class="btn-primary">Continue</button>
                        <button onclick="scenarioWorkflow.showEventBoundarySelection()" class="btn-secondary">Back</button>
                    </div>
                </div>
            </div>
        `;

        panel.scrollTop = 0;
    }

    /**
     * Save time range and continue to tags
     */
    saveTimeRange() {
        const startTime = parseFloat(document.getElementById('range-start-time').value);
        const endTimeInput = document.getElementById('range-end-time').value.trim();
        const endTime = endTimeInput ? parseFloat(endTimeInput) : null;

        if (isNaN(startTime) || startTime < 0) {
            alert('Please enter a valid start time');
            return;
        }

        this.collectedData.startTime = startTime;
        this.collectedData.endTime = endTime;
        this.collectedData.boundaryType = 'time_range';
        this.showTagForm();
    }

    /**
     * Show tag form for additional metadata
     */
    showTagForm() {
        console.log('[Scenario Workflow] Showing scenario tag editor');
        this.showScenarioEditor();
    }

    /**
     * Show scenario editor with all bboxes and notes field
     */
    showScenarioEditor() {
        console.log('[Scenario Workflow] === SHOWING SCENARIO EDITOR (redirecting to checklist) ===');
        console.log('[Scenario Workflow] Total bboxes in collectedData:', Object.keys(this.collectedData.bboxes).length);
        console.log('[Scenario Workflow] Bboxes:', this.collectedData.bboxes);
        console.log('[Scenario Workflow] Not visible:', this.collectedData.notVisible);

        // Instead of showing the old review page, show the checklist
        // Set current step to first step or first incomplete step
        this.currentStep = 0;
        const steps = this.currentScenario.steps || [];
        for (let i = 0; i < steps.length; i++) {
            const step = steps[i];
            const hasData = this.collectedData.bboxes[step.id] ||
                           this.collectedData.notVisible.includes(step.id) ||
                           this.collectedData.notPresent?.includes(step.id) ||
                           this.collectedData.skipped?.includes(step.id);
            if (!hasData) {
                this.currentStep = i;
                break;
            }
        }

        // Only show bbox step if this scenario has bbox steps
        if (steps.length > 0) {
            this.showBBoxStep();
        } else {
            // Non-bbox scenario (e.g., location_context) - show simplified editor
            this.showNoBBoxEditor();
        }
    }

    /**
     * Show simplified editor for scenarios without bbox steps (e.g., location_context)
     */
    showNoBBoxEditor() {
        const panel = document.querySelector('.annotation-panel');

        // Build UI using DOM methods for safety
        panel.textContent = '';
        const form = document.createElement('div');
        form.className = 'inline-annotation-form';

        const header = document.createElement('div');
        header.className = 'form-header';
        const h2 = document.createElement('h2');
        h2.textContent = this.currentScenario.label;
        const cancelBtn1 = document.createElement('button');
        cancelBtn1.textContent = 'Cancel';
        cancelBtn1.className = 'btn-secondary';
        cancelBtn1.addEventListener('click', () => scenarioWorkflow.cancel());
        header.appendChild(h2);
        header.appendChild(cancelBtn1);

        // Add tag fields
        const tagsWrapper = document.createElement('div');
        tagsWrapper.innerHTML = this.buildTagsHTML();

        const notesGroup = document.createElement('div');
        notesGroup.className = 'form-group';
        const notesLabel = document.createElement('label');
        notesLabel.textContent = 'Notes (optional)';
        const notesArea = document.createElement('textarea');
        notesArea.id = 'scenario-notes';
        notesArea.rows = 3;
        notesArea.placeholder = 'Add any observations or notes about this annotation...';
        notesArea.value = this.collectedData.notes || '';
        notesGroup.appendChild(notesLabel);
        notesGroup.appendChild(notesArea);

        const actions = document.createElement('div');
        actions.className = 'form-actions';
        const saveBtn = document.createElement('button');
        saveBtn.textContent = 'Save & Close';
        saveBtn.className = 'btn-primary';
        saveBtn.id = 'save-annotation-btn';
        saveBtn.addEventListener('click', () => saveAndClose());
        const saveNextBtn = document.createElement('button');
        saveNextBtn.textContent = 'Save & Next';
        saveNextBtn.className = 'btn-primary';
        saveNextBtn.id = 'save-next-btn';
        saveNextBtn.style.background = '#27ae60';
        saveNextBtn.addEventListener('click', () => saveAndNext());
        const cancelBtn2 = document.createElement('button');
        cancelBtn2.textContent = 'Cancel';
        cancelBtn2.className = 'btn-secondary';
        cancelBtn2.addEventListener('click', () => scenarioWorkflow.cancel());
        actions.appendChild(saveBtn);
        actions.appendChild(saveNextBtn);
        actions.appendChild(cancelBtn2);

        form.appendChild(header);
        form.appendChild(tagsWrapper);
        form.appendChild(notesGroup);
        form.appendChild(actions);
        panel.appendChild(form);

        panel.scrollTop = 0;
    }

    /**
     * Enable interactive bbox editing
     */
    enableBBoxEditing() {
        console.log('[Scenario Workflow] Enabling bbox editing mode');

        if (!window.bboxCanvas) return;

        this.editMode = {
            isDragging: false,
            isResizing: false,
            selectedBBox: null,
            dragStartX: 0,
            dragStartY: 0,
            resizeHandle: null // 'nw', 'ne', 'sw', 'se', 'n', 's', 'e', 'w'
        };

        // Remove old event listeners if they exist
        if (this.canvasMouseDown) {
            window.bboxCanvas.removeEventListener('mousedown', this.canvasMouseDown);
            window.bboxCanvas.removeEventListener('mousemove', this.canvasMouseMove);
            window.bboxCanvas.removeEventListener('mouseup', this.canvasMouseUp);
        }
        if (this.canvasTouchStart) {
            window.bboxCanvas.removeEventListener('touchstart', this.canvasTouchStart);
            window.bboxCanvas.removeEventListener('touchmove', this.canvasTouchMove);
            window.bboxCanvas.removeEventListener('touchend', this.canvasTouchEnd);
        }

        // Create bound event handlers
        this.canvasMouseDown = this.handleEditMouseDown.bind(this);
        this.canvasMouseMove = this.handleEditMouseMove.bind(this);
        this.canvasMouseUp = this.handleEditMouseUp.bind(this);

        window.bboxCanvas.addEventListener('mousedown', this.canvasMouseDown);
        window.bboxCanvas.addEventListener('mousemove', this.canvasMouseMove);
        window.bboxCanvas.addEventListener('mouseup', this.canvasMouseUp);

        // Touch event handlers for bbox editing on mobile
        if (this.canvasTouchStart) {
            window.bboxCanvas.removeEventListener('touchstart', this.canvasTouchStart);
            window.bboxCanvas.removeEventListener('touchmove', this.canvasTouchMove);
            window.bboxCanvas.removeEventListener('touchend', this.canvasTouchEnd);
        }

        this.canvasTouchStart = ((e) => {
            e.preventDefault();
            const touch = e.touches[0];
            this.handleEditMouseDown({ clientX: touch.clientX, clientY: touch.clientY });
        }).bind(this);

        this.canvasTouchMove = ((e) => {
            e.preventDefault();
            const touch = e.touches[0];
            this.handleEditMouseMove({ clientX: touch.clientX, clientY: touch.clientY });
        }).bind(this);

        this.canvasTouchEnd = ((e) => {
            e.preventDefault();
            const touch = e.changedTouches[0];
            this.handleEditMouseUp({ clientX: touch.clientX, clientY: touch.clientY });
        }).bind(this);

        window.bboxCanvas.addEventListener('touchstart', this.canvasTouchStart, { passive: false });
        window.bboxCanvas.addEventListener('touchmove', this.canvasTouchMove, { passive: false });
        window.bboxCanvas.addEventListener('touchend', this.canvasTouchEnd, { passive: false });

        // Ensure canvas is visible and interactive
        window.bboxCanvas.style.pointerEvents = 'all';
        window.bboxCanvas.style.cursor = 'default';
        window.bboxCanvas.style.display = 'block';
        window.bboxCanvas.classList.add('drawing');

        console.log('[Scenario Workflow] Canvas ready for editing - dimensions:',
            window.bboxCanvas.width, 'x', window.bboxCanvas.height,
            'style:', window.bboxCanvas.style.width, 'x', window.bboxCanvas.style.height);
    }

    /**
     * Handle mouse down in edit mode
     */
    handleEditMouseDown(e) {
        const rect = window.bboxCanvas.getBoundingClientRect();
        const scaleX = window.bboxCanvas.width / rect.width;
        const scaleY = window.bboxCanvas.height / rect.height;
        const x = (e.clientX - rect.left) * scaleX;
        const y = (e.clientY - rect.top) * scaleY;

        // Check if clicking on a bbox or resize handle
        for (const [stepId, bbox] of Object.entries(this.collectedData.bboxes)) {
            const handleSize = 10;
            const handle = this.getResizeHandle(x, y, bbox, handleSize);

            if (handle) {
                this.editMode.isResizing = true;
                this.editMode.selectedBBox = stepId;
                this.editMode.resizeHandle = handle;
                this.editMode.dragStartX = x;
                this.editMode.dragStartY = y;
                this.editMode.originalBBox = { ...bbox };
                console.log('[Scenario Workflow] Started resizing bbox:', stepId, 'handle:', handle);
                return;
            } else if (this.isPointInBBox(x, y, bbox)) {
                this.editMode.isDragging = true;
                this.editMode.selectedBBox = stepId;
                this.editMode.dragStartX = x - bbox.x;
                this.editMode.dragStartY = y - bbox.y;
                console.log('[Scenario Workflow] Started dragging bbox:', stepId);
                return;
            }
        }
    }

    /**
     * Handle mouse move in edit mode
     */
    handleEditMouseMove(e) {
        const rect = window.bboxCanvas.getBoundingClientRect();
        const scaleX = window.bboxCanvas.width / rect.width;
        const scaleY = window.bboxCanvas.height / rect.height;
        const x = (e.clientX - rect.left) * scaleX;
        const y = (e.clientY - rect.top) * scaleY;

        if (this.editMode.isDragging && this.editMode.selectedBBox) {
            // Move bbox
            const bbox = this.collectedData.bboxes[this.editMode.selectedBBox];
            bbox.x = Math.round(x - this.editMode.dragStartX);
            bbox.y = Math.round(y - this.editMode.dragStartY);
            this.drawAllBBoxes();
            this.highlightSelectedBBox();
        } else if (this.editMode.isResizing && this.editMode.selectedBBox) {
            // Resize bbox
            const bbox = this.collectedData.bboxes[this.editMode.selectedBBox];
            const orig = this.editMode.originalBBox;
            const dx = x - this.editMode.dragStartX;
            const dy = y - this.editMode.dragStartY;

            switch (this.editMode.resizeHandle) {
                case 'nw':
                    bbox.x = orig.x + dx;
                    bbox.y = orig.y + dy;
                    bbox.width = orig.width - dx;
                    bbox.height = orig.height - dy;
                    break;
                case 'ne':
                    bbox.y = orig.y + dy;
                    bbox.width = orig.width + dx;
                    bbox.height = orig.height - dy;
                    break;
                case 'sw':
                    bbox.x = orig.x + dx;
                    bbox.width = orig.width - dx;
                    bbox.height = orig.height + dy;
                    break;
                case 'se':
                    bbox.width = orig.width + dx;
                    bbox.height = orig.height + dy;
                    break;
            }

            // Ensure minimum size
            if (bbox.width < 10) bbox.width = 10;
            if (bbox.height < 10) bbox.height = 10;

            this.drawAllBBoxes();
            this.highlightSelectedBBox();
        } else {
            // Update cursor based on what's under mouse
            let cursor = 'default';
            for (const [stepId, bbox] of Object.entries(this.collectedData.bboxes)) {
                const handle = this.getResizeHandle(x, y, bbox, 10);
                if (handle) {
                    cursor = this.getResizeCursor(handle);
                    break;
                } else if (this.isPointInBBox(x, y, bbox)) {
                    cursor = 'move';
                    break;
                }
            }
            window.bboxCanvas.style.cursor = cursor;
        }
    }

    /**
     * Handle mouse up in edit mode
     */
    handleEditMouseUp(e) {
        if (this.editMode.isDragging || this.editMode.isResizing) {
            console.log('[Scenario Workflow] Finished editing bbox');
            this.editMode.isDragging = false;
            this.editMode.isResizing = false;
            this.editMode.originalBBox = null;
        }
    }

    /**
     * Check if point is inside bbox
     */
    isPointInBBox(x, y, bbox) {
        return x >= bbox.x && x <= bbox.x + bbox.width &&
               y >= bbox.y && y <= bbox.y + bbox.height;
    }

    /**
     * Get resize handle at position
     */
    getResizeHandle(x, y, bbox, handleSize) {
        const corners = {
            nw: { x: bbox.x, y: bbox.y },
            ne: { x: bbox.x + bbox.width, y: bbox.y },
            sw: { x: bbox.x, y: bbox.y + bbox.height },
            se: { x: bbox.x + bbox.width, y: bbox.y + bbox.height }
        };

        for (const [handle, pos] of Object.entries(corners)) {
            if (Math.abs(x - pos.x) <= handleSize && Math.abs(y - pos.y) <= handleSize) {
                return handle;
            }
        }
        return null;
    }

    /**
     * Get cursor style for resize handle
     */
    getResizeCursor(handle) {
        const cursors = {
            nw: 'nw-resize',
            ne: 'ne-resize',
            sw: 'sw-resize',
            se: 'se-resize'
        };
        return cursors[handle] || 'default';
    }

    /**
     * Highlight selected bbox
     */
    highlightSelectedBBox() {
        if (this.editMode.selectedBBox) {
            const bbox = this.collectedData.bboxes[this.editMode.selectedBBox];
            const ctx = window.bboxContext;
            ctx.strokeStyle = '#e74c3c';
            ctx.lineWidth = 3;
            ctx.strokeRect(bbox.x, bbox.y, bbox.width, bbox.height);

            // Draw resize handles
            const handleSize = 6;
            ctx.fillStyle = '#e74c3c';
            const corners = [
                { x: bbox.x, y: bbox.y },
                { x: bbox.x + bbox.width, y: bbox.y },
                { x: bbox.x, y: bbox.y + bbox.height },
                { x: bbox.x + bbox.width, y: bbox.y + bbox.height }
            ];
            corners.forEach(corner => {
                ctx.fillRect(corner.x - handleSize/2, corner.y - handleSize/2, handleSize, handleSize);
            });
        }
    }

    /**
     * Select a bbox for editing
     */
    selectBBoxForEdit(stepId) {
        console.log('[Scenario Workflow] Selecting bbox for edit:', stepId);
        this.selectedBBoxForEdit = stepId;

        // Highlight the selected bbox
        this.drawAllBBoxes();

        const bbox = this.collectedData.bboxes[stepId];
        if (bbox && window.bboxContext) {
            const ctx = window.bboxContext;
            ctx.strokeStyle = '#e74c3c';
            ctx.lineWidth = 4;
            ctx.strokeRect(bbox.x, bbox.y, bbox.width, bbox.height);
        }
    }

    /**
     * Delete a bbox
     */
    deleteBBox(stepId) {
        console.log('[Scenario Workflow] Deleting bbox:', stepId);
        if (confirm('Delete this bounding box?')) {
            delete this.collectedData.bboxes[stepId];
            this.showScenarioEditor();
        }
    }

    /**
     * Save from editor (with notes)
     */
    saveFromEditor() {
        const notes = document.getElementById('scenario-notes')?.value || '';
        this.collectedData.notes = notes;
        console.log('[Scenario Workflow] Saving from editor with notes');
        this.save();
    }

    /**
     * Build HTML for tag form fields
     * Returns HTML string for all tag form fields defined in this.currentScenario.tags
     */
    buildTagsHTML() {
        const tags = this.currentScenario.tags;
        if (!tags || Object.keys(tags).length === 0) return '';

        let tagsHTML = '<div class="scenario-tags"><h3>Details</h3>';
        for (const [tagId, tagConfig] of Object.entries(tags)) {
            const required = tagConfig.required ? ' <span class="required">*</span>' : '';

            if (tagConfig.type === 'dropdown') {
                let optionsHTML = '<option value="">-- Select --</option>';

                // Handle dynamic options
                if (tagConfig.dynamicOptions) {
                    // Options will be loaded asynchronously
                    tagsHTML += `
                        <div class="form-group">
                            <label>${tagConfig.label}${required}</label>
                            <select id="tag-${tagId}" ${tagConfig.required ? 'required' : ''} data-dynamic-options="${tagConfig.dynamicOptions}" data-tag-id="${tagId}">
                                ${optionsHTML}
                            </select>
                        </div>
                    `;

                    // Load dynamic options after rendering
                    setTimeout(() => this.loadDynamicOptionsForTag(tagId, tagConfig), 0);
                } else {
                    // Static options
                    tagConfig.options.forEach(opt => {
                        optionsHTML += `<option value="${opt}">${opt.replace(/_/g, ' ')}</option>`;
                    });

                    // Handle allowCustom for static dropdowns
                    if (tagConfig.allowCustom) {
                        optionsHTML += `<option value="__custom__">Other...</option>`;
                    }

                    tagsHTML += `
                        <div class="form-group">
                            <label>${tagConfig.label}${required}</label>
                            <select id="tag-${tagId}" ${tagConfig.required ? 'required' : ''} ${tagConfig.allowCustom ? `onchange="scenarioWorkflow.handleCustomDropdown(this, '${tagId}')"` : ''}>
                                ${optionsHTML}
                            </select>
                            ${tagConfig.allowCustom ? `<input type="text" id="tag-${tagId}-custom" style="display:none; margin-top:5px;" placeholder="Enter custom value">` : ''}
                        </div>
                    `;
                }
            } else if (tagConfig.type === 'checkbox') {
                let checkboxesHTML = '';
                tagConfig.options.forEach(opt => {
                    checkboxesHTML += `
                        <label style="display: block; margin: 5px 0;">
                            <input type="checkbox" name="tag-${tagId}" value="${opt}">
                            ${opt.replace(/_/g, ' ')}
                        </label>
                    `;
                });

                tagsHTML += `
                    <div class="form-group">
                        <label>${tagConfig.label}${required}</label>
                        <div class="checkbox-group">
                            ${checkboxesHTML}
                        </div>
                    </div>
                `;
            } else if (tagConfig.type === 'name_autocomplete') {
                tagsHTML += `
                    <div class="form-group">
                        <label>${tagConfig.label}${required}</label>
                        <div class="name-autocomplete-wrapper">
                            <input type="text" id="tag-${tagId}"
                                   placeholder="${tagConfig.placeholder || 'Enter name...'}"
                                   ${tagConfig.required ? 'required' : ''}
                                   autocomplete="off"
                                   oninput="scenarioWorkflow.showNameSuggestions(this, '${tagId}')">
                            <div id="suggestions-${tagId}" class="name-suggestions"></div>
                            ${tagConfig.showRecentNames ? `<div id="recent-${tagId}" class="recent-names"></div>` : ''}
                        </div>
                    </div>
                `;
            } else if (tagConfig.type === 'text_autocomplete') {
                tagsHTML += `
                    <div class="form-group">
                        <label>${tagConfig.label}${required}</label>
                        <div class="text-autocomplete-wrapper">
                            <input type="text" id="tag-${tagId}"
                                   placeholder="${tagConfig.placeholder || ''}"
                                   ${tagConfig.required ? 'required' : ''}
                                   autocomplete="off"
                                   oninput="scenarioWorkflow.showTextSuggestions(this, '${tagId}', '${tagConfig.recentCount || 10}')">
                            <div id="suggestions-${tagId}" class="text-suggestions"></div>
                            ${tagConfig.showRecentValues ? `<div id="recent-${tagId}" class="recent-values"></div>` : ''}
                        </div>
                    </div>
                `;
            } else if (tagConfig.type === 'configurable_dropdown') {
                let optionsHTML = '<option value="">-- Select --</option>';
                tagConfig.defaultOptions.forEach(opt => {
                    optionsHTML += `<option value="${opt}">${opt.replace(/_/g, ' ')}</option>`;
                });
                if (tagConfig.allowCustom) {
                    optionsHTML += `<option value="__custom__">+ Add New...</option>`;
                }

                tagsHTML += `
                    <div class="form-group">
                        <label>${tagConfig.label}${required}</label>
                        <select id="tag-${tagId}" ${tagConfig.required ? 'required' : ''}
                                onchange="scenarioWorkflow.handleConfigurableDropdown(this, '${tagId}', '${tagConfig.customPrompt || 'Enter custom value'}')">
                            ${optionsHTML}
                        </select>
                        <input type="text" id="tag-${tagId}-custom" style="display:none; margin-top:5px;"
                               placeholder="${tagConfig.customPrompt || 'Enter custom value'}">
                    </div>
                `;
            } else if (tagConfig.type === 'textarea') {
                tagsHTML += `
                    <div class="form-group">
                        <label>${tagConfig.label}${required}</label>
                        <textarea id="tag-${tagId}" rows="4" placeholder="${tagConfig.placeholder || ''}" ${tagConfig.required ? 'required' : ''}></textarea>
                    </div>
                `;
            } else {
                // Default: text input
                tagsHTML += `
                    <div class="form-group">
                        <label>${tagConfig.label}${required}</label>
                        <input type="text" id="tag-${tagId}" placeholder="${tagConfig.placeholder || ''}" ${tagConfig.required ? 'required' : ''}>
                    </div>
                `;
            }
        }
        tagsHTML += '</div>';
        return tagsHTML;
    }

    /**
     * Show time range tag form
     */
    showTimeRangeForm() {
        const panel = document.querySelector('.annotation-panel');
        const tagsHTML = this.buildTagsHTML();

        panel.innerHTML = `
            <div class="inline-annotation-form">
                <div class="form-header">
                    <h2>${this.currentScenario.label}</h2>
                    <button onclick="scenarioWorkflow.cancel()" class="btn-secondary">Cancel</button>
                </div>

                <div class="tag-form-content">
                    <div class="form-group">
                        <label>Start Time (seconds)</label>
                        <input type="number" id="tag-start-time" step="0.1" value="${this.startTime.toFixed(2)}" readonly>
                    </div>

                    <div class="form-group">
                        <label>End Time (seconds) - Optional</label>
                        <input type="number" id="tag-end-time" step="0.1" placeholder="Leave empty to close later">
                    </div>

                    ${tagsHTML}

                    <div class="form-group">
                        <label>Notes (optional)</label>
                        <textarea id="tag-notes" rows="3" placeholder="Add any additional notes or observations..."></textarea>
                    </div>
                </div>

                <div class="form-actions">
                    <button onclick="scenarioWorkflow.save()" class="btn-primary">Save Tag</button>
                    <button onclick="scenarioWorkflow.cancel()" class="btn-secondary">Cancel</button>
                </div>
            </div>
        `;

        panel.scrollTop = 0;
    }

    /**
     * Save the annotation
     */
    async save() {
        // Collect tag values
        const tags = this.currentScenario.tags;
        for (const [tagId, tagConfig] of Object.entries(tags)) {
            if (tagConfig.type === 'checkbox') {
                // Collect all checked values
                const checkboxes = document.querySelectorAll(`input[name="tag-${tagId}"]:checked`);
                const values = Array.from(checkboxes).map(cb => cb.value);
                this.collectedData.tags[tagId] = values;
            } else if (tagConfig.type === 'configurable_dropdown') {
                // Check if custom value was entered
                const select = document.getElementById(`tag-${tagId}`);
                const customInput = document.getElementById(`tag-${tagId}-custom`);
                if (select && select.value === '__custom__' && customInput) {
                    this.collectedData.tags[tagId] = customInput.value;
                } else if (select) {
                    this.collectedData.tags[tagId] = select.value;
                }
            } else if (tagConfig.type === 'dropdown' && tagConfig.allowCustom) {
                // Handle dropdown with allowCustom (like location_name)
                const select = document.getElementById(`tag-${tagId}`);
                const customInput = document.getElementById(`tag-${tagId}-custom`);
                if (select && select.value === '__custom__' && customInput) {
                    this.collectedData.tags[tagId] = customInput.value;
                } else if (select) {
                    this.collectedData.tags[tagId] = select.value;
                }
            } else {
                const element = document.getElementById(`tag-${tagId}`);
                if (element) {
                    this.collectedData.tags[tagId] = element.value;
                }
            }
        }

        // Get notes
        const notes = document.getElementById('tag-notes')?.value || '';

        // Validate required fields
        for (const [tagId, tagConfig] of Object.entries(tags)) {
            if (tagConfig.required) {
                const value = this.collectedData.tags[tagId];
                const isEmpty = Array.isArray(value) ? value.length === 0 : !value;
                if (isEmpty) {
                    alert(`Please fill in required field: ${tagConfig.label}`);
                    return;
                }
            }
        }

        if (this.annotationType === 'keyframe') {
            await this.saveKeyframeAnnotation(notes);
        } else {
            await this.saveTimeRangeTag(notes);
        }
    }

    /**
     * Save keyframe annotation with multiple bboxes
     */
    async saveKeyframeAnnotation(notes) {
        try {
            console.log('[Scenario Workflow] saveKeyframeAnnotation called');
            console.log('[Scenario Workflow] collectedData.bboxes:', JSON.stringify(this.collectedData.bboxes, null, 2));
            console.log('[Scenario Workflow] collectedData.notVisible:', this.collectedData.notVisible);
            console.log('[Scenario Workflow] collectedData.skipped:', this.collectedData.skipped);

            // For now, save primary bbox (entire_boat or primary_subject) as main annotation
            const primaryBBoxKey = this.collectedData.bboxes.entire_boat ? 'entire_boat' :
                                    this.collectedData.bboxes.primary_subject ? 'primary_subject' :
                                    Object.keys(this.collectedData.bboxes)[0];

            const primaryBBox = this.collectedData.bboxes[primaryBBoxKey];

            if (!primaryBBox) {
                console.error('[Scenario Workflow] No bounding boxes in collectedData:', this.collectedData.bboxes);
                alert('No bounding boxes were drawn. Please draw at least one bounding box.');
                return;
            }

            console.log('[Scenario Workflow] Using primary bbox key:', primaryBBoxKey);
            console.log('[Scenario Workflow] Primary bbox:', primaryBBox);

            let annotationId;
            let response;

            // Check if we're editing an existing annotation
            if (this.annotationIdBeingEdited) {
                // Update existing annotation
                console.log('[Scenario Workflow] Updating annotation:', this.annotationIdBeingEdited);
                response = await fetch(`/api/keyframe-annotations/${this.annotationIdBeingEdited}`, {
                    method: 'PUT',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        timestamp: this.startTime,
                        bbox_x: primaryBBox.x,
                        bbox_y: primaryBBox.y,
                        bbox_width: primaryBBox.width,
                        bbox_height: primaryBBox.height,
                        activity_tag: this.currentScenario.id,
                        moment_tag: null,
                        is_negative: false,
                        comment: notes
                    })
                });
                annotationId = this.annotationIdBeingEdited;
            } else {
                // Create new annotation using the timestamp when workflow started
                console.log('[Scenario Workflow] Creating new annotation');
                response = await fetch(`/api/videos/${currentVideoId}/keyframe-annotations`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        timestamp: this.startTime,
                        bbox_x: primaryBBox.x,
                        bbox_y: primaryBBox.y,
                        bbox_width: primaryBBox.width,
                        bbox_height: primaryBBox.height,
                        activity_tag: this.currentScenario.id,
                        moment_tag: null,
                        is_negative: false,
                        comment: notes
                    })
                });
            }

            const data = await response.json();

            if (data.success) {
                if (!this.annotationIdBeingEdited) {
                    annotationId = data.annotation_id;
                }

                // Save all collected data as structured tags
                const tagData = {
                    scenario: this.currentScenario.id,
                    bboxes: this.collectedData.bboxes,
                    notVisible: this.collectedData.notVisible,
                    notPresent: this.collectedData.notPresent || [],
                    skipped: this.collectedData.skipped || [],
                    comment: this.collectedData.notes || notes,
                    ...this.collectedData.tags
                };

                console.log('[Scenario Workflow] Saving tag data:', tagData);
                console.log('[Scenario Workflow] Number of bboxes being saved:', Object.keys(tagData.bboxes).length);
                console.log('[Scenario Workflow] Bbox keys being saved:', Object.keys(tagData.bboxes));

                const tagsResponse = await fetch(`/api/annotations/${annotationId}/tags`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        annotation_type: 'keyframe',
                        tags: tagData
                    })
                });

                const tagsResult = await tagsResponse.json();
                console.log('[Scenario Workflow] Tags save response:', tagsResult);

                if (!tagsResult.success) {
                    console.error('[Scenario Workflow] Tags save failed:', tagsResult.error);
                    alert('Warning: Annotation saved but tags failed to save: ' + (tagsResult.error || 'Unknown error'));
                }

                // Success - reload and reset
                const savedScenarioId = this.currentScenario ? this.currentScenario.id : null;
                const savedFrameData = this.extractedFrameData;
                const allBboxes = JSON.parse(JSON.stringify(this.collectedData.bboxes));

                // For person_identification (new annotations only), create a separate annotation for each additional person
                const personQueue = [];
                const isNewAnnotation = !this.annotationIdBeingEdited;
                if (savedScenarioId === 'person_identification' && isNewAnnotation) {
                    // Primary person already saved as annotationId
                    personQueue.push({ annotationId: annotationId, bbox: allBboxes[primaryBBoxKey] });

                    // Create annotations for additional people
                    for (const [stepId, bbox] of Object.entries(allBboxes)) {
                        if (stepId === primaryBBoxKey) continue; // already saved
                        try {
                            const extraResp = await fetch(`/api/videos/${currentVideoId}/keyframe-annotations`, {
                                method: 'POST',
                                headers: {'Content-Type': 'application/json'},
                                body: JSON.stringify({
                                    timestamp: this.startTime,
                                    bbox_x: bbox.x,
                                    bbox_y: bbox.y,
                                    bbox_width: bbox.width,
                                    bbox_height: bbox.height,
                                    activity_tag: 'person_identification',
                                    moment_tag: null,
                                    is_negative: false,
                                    comment: notes || ''
                                })
                            });
                            const extraData = await extraResp.json();
                            if (extraData.success) {
                                personQueue.push({ annotationId: extraData.annotation_id, bbox: bbox });
                                console.log('[Scenario Workflow] Created annotation for', stepId, ':', extraData.annotation_id);
                            }
                        } catch (err) {
                            console.error('[Scenario Workflow] Failed to create annotation for', stepId, err);
                        }
                    }
                }

                this.cancel();
                loadKeyframeAnnotations();

                // If person_identification scenario (new only), iterate through identification queue
                if (savedScenarioId === 'person_identification' && isNewAnnotation && personQueue.length > 0) {
                    this._savedFrameData = savedFrameData;
                    this._personQueue = personQueue;
                    this._personQueueIndex = 0;
                    this.showNextPersonIdentify();
                } else if (window._saveAction) {
                    navigateAfterSave();
                }
            } else {
                alert('Error saving annotation: ' + (data.error || 'Unknown error'));
            }
        } catch (error) {
            alert('Error saving annotation: ' + error.message);
        }
    }

    /**
     * Save time range tag
     */
    async saveTimeRangeTag(notes) {
        try {
            let startTime, endTime;

            // For whole-clip annotations, use stored values
            if (this.currentScenario.appliesToEntireClip) {
                startTime = 0;
                endTime = videoPlayer.duration;
            } else if (this.startTime !== undefined && this.startTime !== null) {
                // Use the timestamp from when workflow started (frame extraction time)
                startTime = this.startTime;
                endTime = this.startTime; // Single moment annotation
            } else if (this.collectedData.startTime !== undefined) {
                // For event boundary annotations, use collected data
                startTime = this.collectedData.startTime;
                endTime = this.collectedData.endTime;
            } else {
                // Fallback to form elements (legacy)
                const startTimeElement = document.getElementById('tag-start-time');
                const endTimeElement = document.getElementById('tag-end-time');

                if (!startTimeElement) {
                    alert('Error: Unable to determine annotation start time');
                    return;
                }

                startTime = parseFloat(startTimeElement.value);
                const endTimeInput = endTimeElement?.value?.trim();
                endTime = endTimeInput ? parseFloat(endTimeInput) : null;
            }

            const response = await fetch(`/api/videos/${currentVideoId}/time-range-tags`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    tag_name: this.currentScenario.id,
                    start_time: startTime,
                    end_time: endTime,
                    is_negative: false,
                    comment: notes
                })
            });

            const data = await response.json();

            if (data.success) {
                const tagId = data.tag_id;

                // Save structured tags
                const tagData = {
                    scenario: this.currentScenario.id,
                    ...this.collectedData.tags
                };

                await fetch(`/api/annotations/${tagId}/tags`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        annotation_type: 'time_range',
                        tags: tagData
                    })
                });

                // Success - reload and reset
                this.cancel();
                loadTimeRangeTags();
                if (window._saveAction) {
                    navigateAfterSave();
                }
            } else {
                alert('Error saving tag: ' + (data.error || 'Unknown error'));
            }
        } catch (error) {
            alert('Error saving tag: ' + error.message);
        }
    }

    /**
     * Edit an existing annotation
     */
    async editAnnotation(annotationId, timestamp) {
        console.log('[Scenario Workflow] Loading annotation for editing:', annotationId);

        try {
            // Save original panel content if not already saved
            const panel = document.querySelector('.annotation-panel');
            if (!originalPanelContent) {
                originalPanelContent = panel.innerHTML;
            }

            // Fetch the main annotation data first
            const annoResponse = await fetch(`/api/keyframe-annotations/${annotationId}`);
            const annoData = await annoResponse.json();

            if (!annoData.success || !annoData.annotation) {
                console.error('[Scenario Workflow] Error loading annotation:', annoData);
                alert('Error loading annotation');
                return;
            }

            const anno = annoData.annotation;

            // Fetch annotation data including structured tags
            const response = await fetch(`/api/annotations/${annotationId}/tags?annotation_type=keyframe`);
            const data = await response.json();

            const tags = data.success ? data.tags : {};
            console.log('[Scenario Workflow] Loaded annotation data:', tags);
            console.log('[Scenario Workflow] Tags.bboxes:', tags.bboxes);
            console.log('[Scenario Workflow] Tags.scenario:', tags.scenario);

            // Reset workflow
            this.reset();
            this.startTime = timestamp;
            this.annotationIdBeingEdited = annotationId;

            // Extract frame at the annotation timestamp
            if (window.isImageMode) {
                this.extractCurrentFrame();
            } else {
                videoPlayer.currentTime = timestamp;
                await new Promise(resolve => {
                    videoPlayer.addEventListener('seeked', resolve, { once: true });
                });
                this.extractCurrentFrame();
            }

            // Load scenario configuration
            const scenarioId = tags.scenario || anno.activity_tag;
            console.log('[Scenario Workflow] Determined scenario ID:', scenarioId);

            if (!scenarioId || !annotationScenarios[scenarioId]) {
                const errorMsg = `Unknown scenario type for this annotation: ${scenarioId}`;
                console.error('[Scenario Workflow]', errorMsg);
                alert(errorMsg);
                return;
            }

            this.currentScenario = annotationScenarios[scenarioId];
            this.currentScenario.id = scenarioId;
            this.annotationType = 'keyframe';

            // Load collected data
            console.log('[Scenario Workflow] Raw tags from API:', JSON.stringify(tags, null, 2));
            console.log('[Scenario Workflow] tags.bboxes type:', typeof tags.bboxes);
            console.log('[Scenario Workflow] tags.bboxes value:', tags.bboxes);

            // Reconstruct dynamic steps if scenario supports them
            if (this.currentScenario.allowDynamicSteps && this.currentScenario.dynamicStepTemplate) {
                const template = this.currentScenario.dynamicStepTemplate;
                const bboxes = tags.bboxes || {};

                // Find all dynamic step IDs in saved bboxes
                const dynamicStepIds = Object.keys(bboxes).filter(id => id.startsWith(template.idPrefix));

                // Recreate dynamic steps
                dynamicStepIds.forEach(stepId => {
                    // Extract number from ID
                    const number = stepId.replace(template.idPrefix, '');

                    // Create step
                    const dynamicStep = {
                        id: stepId,
                        label: `${template.label} ${number}`,
                        prompt: template.prompt,
                        optional: template.optional,
                        notVisibleOption: template.notVisibleOption,
                        isDynamic: true
                    };

                    // Add to steps if not already there
                    if (!this.currentScenario.steps.find(s => s.id === stepId)) {
                        this.currentScenario.steps.push(dynamicStep);
                        console.log('[Scenario Workflow] Reconstructed dynamic step:', dynamicStep);
                    }
                });
            }

            // If no structured bboxes in tags, create from main annotation bbox
            let bboxes = tags.bboxes || {};
            if (Object.keys(bboxes).length === 0 && anno.bbox_x !== undefined) {
                console.log('[Scenario Workflow] No structured bboxes found, creating from main annotation bbox');
                // Create a bbox for the primary subject (use first step ID from scenario)
                const primaryKey = (this.currentScenario.steps[0] && this.currentScenario.steps[0].id) || 'primary_subject';
                bboxes[primaryKey] = {
                    x: anno.bbox_x,
                    y: anno.bbox_y,
                    width: anno.bbox_width,
                    height: anno.bbox_height
                };
                console.log('[Scenario Workflow] Created fallback bbox for key:', primaryKey);
            }

            this.collectedData = {
                bboxes: bboxes,
                tags: {},
                notVisible: tags.notVisible || [],
                notPresent: tags.notPresent || [],
                skipped: tags.skipped || [],
                notes: anno.comment || tags.comment || ''
            };

            console.log('[Scenario Workflow] Loaded bboxes count:', Object.keys(this.collectedData.bboxes).length);
            console.log('[Scenario Workflow] Loaded bboxes keys:', Object.keys(this.collectedData.bboxes));
            console.log('[Scenario Workflow] Loaded bboxes detail:', JSON.stringify(this.collectedData.bboxes, null, 2));
            console.log('[Scenario Workflow] Not visible items:', this.collectedData.notVisible);
            console.log('[Scenario Workflow] Not present items:', this.collectedData.notPresent);
            console.log('[Scenario Workflow] Skipped items:', this.collectedData.skipped);

            // Load tag values (exclude structural keys that are stored separately)
            const structuralKeys = ['scenario', 'bboxes', 'notVisible', 'notPresent', 'skipped', 'comment'];
            for (const [tagId, tagValue] of Object.entries(tags)) {
                if (!structuralKeys.includes(tagId)) {
                    this.collectedData.tags[tagId] = tagValue;
                }
            }

            // Hide Draw BBox button
            const drawBtn = document.getElementById('draw-mode-btn');
            if (drawBtn) {
                drawBtn.style.display = 'none';
            }

            // Show extracted frame
            this.showExtractedFrame();

            // Go directly to editor
            this.showScenarioEditor();

        } catch (error) {
            console.error('[Scenario Workflow] Exception while loading annotation:', error);
            alert('Error loading annotation: ' + error.message);
        }
    }

    /**
     * Show name suggestions for autocomplete
     */
    async showNameSuggestions(input, tagId) {
        const query = input.value.trim();
        const suggestionsDiv = document.getElementById(`suggestions-${tagId}`);
        const recentDiv = document.getElementById(`recent-${tagId}`);

        if (!query || query.length < 1) {
            if (suggestionsDiv) suggestionsDiv.innerHTML = '';
            // Load recent names
            if (recentDiv && !recentDiv.dataset.loaded) {
                this.loadRecentNames(tagId);
            }
            return;
        }

        try {
            const response = await fetch(`/api/person-names/recent?limit=20`);
            const data = await response.json();

            if (data.success) {
                const matches = data.names.filter(name =>
                    name.toLowerCase().includes(query.toLowerCase())
                );

                if (matches.length > 0 && suggestionsDiv) {
                    let html = '<div style="background: #1a1a1a; border: 1px solid #444; border-top: none; max-height: 150px; overflow-y: auto;">';
                    matches.forEach(name => {
                        html += `<div style="padding: 8px; cursor: pointer; border-bottom: 1px solid #333;"
                                      onclick="scenarioWorkflow.selectNameSuggestion('${tagId}', '${name.replace(/'/g, "\\'")}')">${name}</div>`;
                    });
                    html += '</div>';
                    suggestionsDiv.innerHTML = html;
                } else if (suggestionsDiv) {
                    suggestionsDiv.innerHTML = '';
                }
            }
        } catch (error) {
            console.error('Error loading name suggestions:', error);
        }
    }

    /**
     * Select a name suggestion
     */
    selectNameSuggestion(tagId, name) {
        const input = document.getElementById(`tag-${tagId}`);
        if (input) {
            input.value = name;
        }
        const suggestionsDiv = document.getElementById(`suggestions-${tagId}`);
        if (suggestionsDiv) {
            suggestionsDiv.innerHTML = '';
        }
    }

    /**
     * Load recent names for display
     */
    async loadRecentNames(tagId) {
        const recentDiv = document.getElementById(`recent-${tagId}`);
        if (!recentDiv) return;

        try {
            const response = await fetch('/api/person-names/recent?limit=10');
            const data = await response.json();

            if (data.success && data.names.length > 0) {
                let html = '<div style="margin-top: 10px;"><label style="font-size: 11px; color: #666;">Recent names:</label><div style="display: flex; flex-wrap: wrap; gap: 5px; margin-top: 5px;">';
                data.names.forEach(name => {
                    html += `<span style="background: #333; padding: 4px 8px; border-radius: 3px; cursor: pointer; font-size: 11px;"
                                   onclick="scenarioWorkflow.selectNameSuggestion('${tagId}', '${name.replace(/'/g, "\\'")}')">${name}</span>`;
                });
                html += '</div></div>';
                recentDiv.innerHTML = html;
                recentDiv.dataset.loaded = 'true';
            }
        } catch (error) {
            console.error('Error loading recent names:', error);
        }
    }

    /**
     * Show suggestions for text autocomplete fields (make, model, fleet ID, etc.)
     */
    async showTextSuggestions(input, tagId, limit = 10) {
        const query = input.value.trim();
        const suggestionsDiv = document.getElementById(`suggestions-${tagId}`);
        const recentDiv = document.getElementById(`recent-${tagId}`);

        if (!query || query.length < 1) {
            if (suggestionsDiv) suggestionsDiv.innerHTML = '';
            // Load recent values
            if (recentDiv && !recentDiv.dataset.loaded) {
                this.loadRecentValues(tagId, limit);
            }
            return;
        }

        try {
            const response = await fetch(`/api/tag-values/recent?tag_name=${encodeURIComponent(tagId)}&limit=20`);
            const data = await response.json();

            if (data.success && data.values) {
                const matches = data.values.filter(val =>
                    val.toLowerCase().includes(query.toLowerCase())
                );

                if (matches.length > 0 && suggestionsDiv) {
                    let html = '<div style="background: #1a1a1a; border: 1px solid #444; border-top: none; max-height: 150px; overflow-y: auto;">';
                    matches.forEach(value => {
                        html += `<div style="padding: 8px; cursor: pointer; border-bottom: 1px solid #333;"
                                      onclick="scenarioWorkflow.selectTextSuggestion('${tagId}', '${value.replace(/'/g, "\\'")}')">${value}</div>`;
                    });
                    html += '</div>';
                    suggestionsDiv.innerHTML = html;
                } else if (suggestionsDiv) {
                    suggestionsDiv.innerHTML = '';
                }
            }
        } catch (error) {
            console.error('Error loading text suggestions:', error);
        }
    }

    /**
     * Select a text suggestion
     */
    selectTextSuggestion(tagId, value) {
        const input = document.getElementById(`tag-${tagId}`);
        if (input) {
            input.value = value;
        }
        const suggestionsDiv = document.getElementById(`suggestions-${tagId}`);
        if (suggestionsDiv) {
            suggestionsDiv.innerHTML = '';
        }
    }

    /**
     * Load recent values for text autocomplete
     */
    async loadRecentValues(tagId, limit = 10) {
        const recentDiv = document.getElementById(`recent-${tagId}`);
        if (!recentDiv) return;

        try {
            const response = await fetch(`/api/tag-values/recent?tag_name=${encodeURIComponent(tagId)}&limit=${limit}`);
            const data = await response.json();

            if (data.success && data.values && data.values.length > 0) {
                let html = '<div style="margin-top: 10px;"><label style="font-size: 11px; color: #666;">Recent values:</label><div style="display: flex; flex-wrap: wrap; gap: 5px; margin-top: 5px;">';
                data.values.forEach(value => {
                    html += `<span style="background: #333; padding: 4px 8px; border-radius: 3px; cursor: pointer; font-size: 11px;"
                                   onclick="scenarioWorkflow.selectTextSuggestion('${tagId}', '${value.replace(/'/g, "\\'")}')">${value}</span>`;
                });
                html += '</div></div>';
                recentDiv.innerHTML = html;
                recentDiv.dataset.loaded = 'true';
            }
        } catch (error) {
            console.error('Error loading recent values:', error);
        }
    }

    /**
     * Handle configurable dropdown (add custom options)
     */
    handleConfigurableDropdown(select, tagId, customPrompt) {
        const customInput = document.getElementById(`tag-${tagId}-custom`);
        if (!customInput) return;

        if (select.value === '__custom__') {
            customInput.style.display = 'block';
            customInput.focus();
            customInput.required = select.required;
            select.required = false;
        } else {
            customInput.style.display = 'none';
            customInput.required = false;
            select.required = true;
        }
    }

    /**
     * Handle custom dropdown (show text input for "Other...")
     */
    handleCustomDropdown(select, tagId) {
        const customInput = document.getElementById(`tag-${tagId}-custom`);
        if (!customInput) return;

        if (select.value === '__custom__') {
            customInput.style.display = 'block';
            customInput.focus();
            customInput.required = select.required;
            select.required = false;
        } else {
            customInput.style.display = 'none';
            customInput.required = false;
            select.required = true;
        }
    }

    /**
     * Load dynamic options for a tag from API
     */
    async loadDynamicOptionsForTag(tagId, tagConfig) {
        const selectElement = document.getElementById(`tag-${tagId}`);
        if (!selectElement || !tagConfig.dynamicOptions) return;

        try {
            const response = await fetch(tagConfig.dynamicOptions);
            const data = await response.json();

            let options = [];
            if (tagConfig.dynamicOptionsMap && typeof tagConfig.dynamicOptionsMap === 'function') {
                options = tagConfig.dynamicOptionsMap(data);
            } else if (data.locations) {
                options = data.locations.map(loc => loc.location_name);
            }

            // Add fetched options to the select (preserve the "-- Select --" option)
            options.forEach(opt => {
                const optionEl = document.createElement('option');
                optionEl.value = opt;
                optionEl.textContent = opt;
                selectElement.appendChild(optionEl);
            });

            // Add "Other..." option if allowCustom is true
            if (tagConfig.allowCustom) {
                const otherOption = document.createElement('option');
                otherOption.value = '__custom__';
                otherOption.textContent = 'Other...';
                selectElement.appendChild(otherOption);

                // Add change handler for custom input
                selectElement.setAttribute('onchange', `scenarioWorkflow.handleCustomDropdown(this, '${tagId}')`);

                // Add hidden custom input
                const formGroup = selectElement.closest('.form-group');
                const customInput = document.createElement('input');
                customInput.type = 'text';
                customInput.id = `tag-${tagId}-custom`;
                customInput.style.cssText = 'display:none; margin-top:5px;';
                customInput.placeholder = 'Enter custom location name';
                formGroup.appendChild(customInput);
            }

            // Auto-populate if camera location is detected
            if (tagId === 'location_name' && window.currentLocation && window.currentLocation.location_name) {
                const locationName = window.currentLocation.location_name;
                // Check if this location exists in the options
                const matchingOption = Array.from(selectElement.options).find(opt => opt.value === locationName);
                if (matchingOption) {
                    selectElement.value = locationName;
                    console.log('[Scenario Workflow] Auto-populated location_name:', locationName);
                }
            }

        } catch (error) {
            console.error(`Failed to load dynamic options for ${tagId}:`, error);
        }
    }

    /**
     * Cancel workflow and restore panel
     */
    cancel() {
        console.log('[Scenario Workflow] Cancel called');

        // Cancel any bbox waiting
        if (this.bboxWaitInterval) {
            clearInterval(this.bboxWaitInterval);
            this.bboxWaitInterval = null;
        }
        window.onBBoxDrawn = null;

        // Restore video display
        this.restoreVideoDisplay();

        // Restore panel
        if (originalPanelContent) {
            document.querySelector('.annotation-panel').innerHTML = originalPanelContent;
            originalPanelContent = null;
        }

        // Show Draw BBox button again
        const drawBtn = document.getElementById('draw-mode-btn');
        if (drawBtn) {
            drawBtn.style.display = '';
        }

        this.reset();
    }

    /**
     * Iterate through person identification queue, showing modal for each unidentified person.
     */
    showNextPersonIdentify() {
        if (!this._personQueue || this._personQueueIndex >= this._personQueue.length) {
            // Done with all people
            this._personQueue = null;
            this._personQueueIndex = 0;
            this._savedFrameData = null;
            // Navigate if save action was set (Save & Close / Save & Next)
            if (window._saveAction) {
                navigateAfterSave();
            }
            return;
        }

        const current = this._personQueue[this._personQueueIndex];
        this._savedBBox = current.bbox;
        this.showPersonIdentifyModal(current.annotationId);
    }

    /**
     * Show person identification modal after saving a person_identification annotation.
     * Fetches known persons and lets the user quickly assign a name.
     */
    async showPersonIdentifyModal(annotationId) {
        const modal = document.getElementById('person-identify-modal');
        if (!modal) return;

        const cropContainer = document.getElementById('person-identify-crop');
        const knownContainer = document.getElementById('person-identify-known');
        const nameInput = document.getElementById('person-identify-input');
        const assignBtn = document.getElementById('person-identify-assign-btn');
        const skipBtn = document.getElementById('person-identify-skip');

        // Update header with person counter
        const headerTitle = modal.querySelector('.person-identify-header h3');
        if (headerTitle && this._personQueue && this._personQueue.length > 1) {
            headerTitle.textContent = `Identify Person (${this._personQueueIndex + 1} of ${this._personQueue.length})`;
        } else if (headerTitle) {
            headerTitle.textContent = 'Identify Person';
        }

        // Update skip button text based on queue position
        if (this._personQueue && this._personQueueIndex < this._personQueue.length - 1) {
            skipBtn.textContent = 'Skip';
        } else {
            skipBtn.textContent = 'Done';
        }

        // Build the crop display: full frame with bbox + cropped enlargement
        cropContainer.textContent = '';
        const frameData = this._savedFrameData || this.extractedFrameData;
        const bbox = this._savedBBox;

        if (frameData && bbox) {
            // Create a container with two views side by side
            const cropLayout = document.createElement('div');
            cropLayout.style.cssText = 'display: flex; gap: 12px; align-items: center; justify-content: center; flex-wrap: wrap;';

            // Full frame with bbox overlay
            const contextView = document.createElement('div');
            contextView.style.cssText = 'position: relative; flex: 1; min-width: 200px; max-width: 50%;';
            const contextCanvas = document.createElement('canvas');
            contextCanvas.style.cssText = 'width: 100%; border-radius: 6px; border: 2px solid #444;';
            contextView.appendChild(contextCanvas);
            cropLayout.appendChild(contextView);

            // Cropped enlargement
            const cropView = document.createElement('div');
            cropView.style.cssText = 'flex: 1; min-width: 120px; max-width: 50%; display: flex; align-items: center; justify-content: center;';
            const cropCanvas = document.createElement('canvas');
            cropCanvas.style.cssText = 'max-width: 100%; max-height: 240px; border-radius: 6px; border: 2px solid #4CAF50;';
            cropView.appendChild(cropCanvas);
            cropLayout.appendChild(cropView);

            cropContainer.appendChild(cropLayout);

            // Draw once the image loads
            const img = new Image();
            img.onload = () => {
                const imgW = img.naturalWidth;
                const imgH = img.naturalHeight;

                // Bbox is already in native pixel coords (same space as the frame)
                const bx = Math.round(bbox.x);
                const by = Math.round(bbox.y);
                const bw = Math.round(bbox.width);
                const bh = Math.round(bbox.height);

                // Context canvas: full frame with bbox
                contextCanvas.width = imgW;
                contextCanvas.height = imgH;
                const ctxCtx = contextCanvas.getContext('2d');
                ctxCtx.drawImage(img, 0, 0);
                // Draw bbox rectangle
                ctxCtx.strokeStyle = '#4CAF50';
                ctxCtx.lineWidth = Math.max(3, Math.round(imgW / 200));
                ctxCtx.strokeRect(bx, by, bw, bh);
                // Dim area outside bbox
                ctxCtx.fillStyle = 'rgba(0, 0, 0, 0.4)';
                ctxCtx.fillRect(0, 0, imgW, by); // top
                ctxCtx.fillRect(0, by + bh, imgW, imgH - by - bh); // bottom
                ctxCtx.fillRect(0, by, bx, bh); // left
                ctxCtx.fillRect(bx + bw, by, imgW - bx - bw, bh); // right

                // Crop canvas: just the person, enlarged
                cropCanvas.width = bw;
                cropCanvas.height = bh;
                const cropCtx = cropCanvas.getContext('2d');
                cropCtx.drawImage(img, bx, by, bw, bh, 0, 0, bw, bh);
            };
            img.src = frameData;
        } else if (frameData) {
            // Fallback: just show full frame if no bbox
            const img = document.createElement('img');
            img.src = frameData;
            img.alt = 'Captured frame';
            img.style.cssText = 'max-height: 180px; max-width: 100%; border-radius: 6px; border: 2px solid #444;';
            cropContainer.appendChild(img);
        }

        nameInput.value = '';
        modal.style.display = 'flex';

        // Close handler - advances to next person in queue
        const closeModal = () => {
            modal.style.display = 'none';
            this._savedBBox = null;
            // Advance to next person in queue
            if (this._personQueue) {
                this._personQueueIndex++;
                this.showNextPersonIdentify();
            }
        };

        skipBtn.onclick = closeModal;
        modal.addEventListener('click', (e) => {
            if (e.target === modal) closeModal();
        }, { once: true });

        // Assign handler
        const assignName = async (name) => {
            if (!name || !name.trim()) return;
            try {
                const resp = await fetch('/api/person-detections/assign-name', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        detection_ids: [annotationId],
                        person_name: name.trim()
                    })
                });
                const result = await resp.json();
                if (result.success) {
                    console.log('[Person Identify] Assigned name:', name.trim());
                    loadKeyframeAnnotations();
                } else {
                    console.error('[Person Identify] Assign failed:', result.error);
                }
            } catch (err) {
                console.error('[Person Identify] Error assigning name:', err);
            }
            closeModal();
        };

        assignBtn.onclick = () => assignName(nameInput.value);
        nameInput.onkeydown = (e) => {
            if (e.key === 'Enter') assignName(nameInput.value);
        };

        // Fetch known persons
        knownContainer.textContent = '';
        const loadingDiv = document.createElement('div');
        loadingDiv.className = 'person-identify-loading';
        loadingDiv.textContent = 'Loading known persons...';
        knownContainer.appendChild(loadingDiv);

        try {
            const resp = await fetch('/api/person-detections');
            const data = await resp.json();

            knownContainer.textContent = '';

            if (data.success && data.people && data.people.length > 0) {
                const knownPeople = data.people.filter(p => p.name && p.name !== 'Unknown');

                if (knownPeople.length === 0) {
                    const empty = document.createElement('div');
                    empty.className = 'person-identify-empty';
                    empty.textContent = 'No known persons yet. Type a name above.';
                    knownContainer.appendChild(empty);
                    return;
                }

                const grid = document.createElement('div');
                grid.className = 'person-identify-grid';

                for (const person of knownPeople) {
                    const card = document.createElement('div');
                    card.className = 'person-identify-card';
                    card.addEventListener('click', () => assignName(person.name));

                    const nameEl = document.createElement('div');
                    nameEl.className = 'person-name';
                    nameEl.textContent = person.name;
                    card.appendChild(nameEl);

                    const countEl = document.createElement('div');
                    countEl.className = 'person-count';
                    countEl.textContent = person.count + ' detection' + (person.count !== 1 ? 's' : '');
                    card.appendChild(countEl);

                    grid.appendChild(card);
                }

                knownContainer.appendChild(grid);
            } else {
                const empty = document.createElement('div');
                empty.className = 'person-identify-empty';
                empty.textContent = 'No known persons yet. Type a name above.';
                knownContainer.appendChild(empty);
            }
        } catch (err) {
            console.error('[Person Identify] Error loading persons:', err);
            knownContainer.textContent = '';
            const errDiv = document.createElement('div');
            errDiv.className = 'person-identify-empty';
            errDiv.textContent = 'Could not load known persons.';
            knownContainer.appendChild(errDiv);
        }

        nameInput.focus();
    }
}

// Global instance
window.scenarioWorkflow = new ScenarioWorkflow();
