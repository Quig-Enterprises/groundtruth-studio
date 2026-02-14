/**
 * AI Prediction Review Module
 * Handles fetching, displaying, and reviewing AI predictions on the annotation page.
 */
const predictionReview = {
    predictions: [],
    selectedId: null,
    videoId: null,
    bboxSvgOverlay: null,

    init(videoId) {
        this.videoId = videoId;
        this.loadPredictions();
    },

    async loadPredictions() {
        if (!this.videoId) return;
        try {
            const resp = await fetch(`/api/ai/predictions/pending?video_id=${this.videoId}`);
            const data = await resp.json();
            if (data.success) {
                this.predictions = data.predictions;
                this.render();
                this.updateBadges();
                this.populateModelFilter();
            }
        } catch (e) {
            console.error('Failed to load predictions:', e);
        }
    },

    updateBadges() {
        const count = this.predictions.length;
        const badge = document.getElementById('tab-prediction-badge');
        const sectionBadge = document.getElementById('prediction-badge');
        const countDisplay = document.getElementById('prediction-count-display');

        if (badge) {
            badge.textContent = count;
            badge.style.display = count > 0 ? 'inline-block' : 'none';
        }
        if (sectionBadge) {
            sectionBadge.textContent = count;
            sectionBadge.style.display = count > 0 ? 'inline-block' : 'none';
        }
        if (countDisplay) {
            countDisplay.textContent = count + ' pending';
        }
    },

    populateModelFilter() {
        const select = document.getElementById('prediction-model-filter');
        if (!select) return;
        const models = [...new Set(this.predictions.map(p => p.model_name + ' v' + p.model_version))];
        // Keep the "All Models" option, clear the rest
        select.innerHTML = '<option value="">All Models</option>';
        models.forEach(m => {
            const opt = document.createElement('option');
            opt.value = m;
            opt.textContent = m;
            select.appendChild(opt);
        });
    },

    filterByModel(modelFilter) {
        this.render(modelFilter);
    },

    render(modelFilter) {
        const container = document.getElementById('predictions-list');
        if (!container) return;

        let filtered = this.predictions;
        if (modelFilter) {
            filtered = filtered.filter(p => (p.model_name + ' v' + p.model_version) === modelFilter);
        }

        if (filtered.length === 0) {
            container.innerHTML = '<div class="empty-state" style="padding:30px;"><p style="color:#95a5a6;font-size:14px;">No pending predictions</p></div>';
            return;
        }

        // Clear container and rebuild with safe DOM methods
        container.innerHTML = '';
        filtered.forEach(p => {
            const card = this.createPredictionCard(p);
            container.appendChild(card);
        });
    },

    createPredictionCard(p) {
        const card = document.createElement('div');
        card.className = 'prediction-card' + (p.id === this.selectedId ? ' selected' : '');
        card.setAttribute('data-id', p.id);
        card.onclick = () => this.selectPrediction(p.id);

        // Header
        const header = document.createElement('div');
        header.className = 'prediction-card-header';

        const scenario = document.createElement('span');
        scenario.className = 'prediction-scenario';
        scenario.textContent = p.scenario;

        const confClass = p.confidence >= 0.9 ? 'conf-high' : p.confidence >= 0.7 ? 'conf-medium' : 'conf-low';
        const confPill = document.createElement('span');
        confPill.className = 'confidence-pill ' + confClass;
        confPill.textContent = (p.confidence * 100).toFixed(1) + '%';

        header.appendChild(scenario);
        header.appendChild(confPill);

        // Meta
        const meta = document.createElement('div');
        meta.className = 'prediction-meta';
        const typeLabel = p.prediction_type === 'keyframe' ? 'Keyframe' : 'Time Range';
        const timeStr = p.prediction_type === 'keyframe'
            ? this.formatTime(p.timestamp)
            : this.formatTime(p.start_time) + ' - ' + this.formatTime(p.end_time);
        meta.textContent = `${typeLabel} at ${timeStr} · ${p.model_name} v${p.model_version}`;
        if (p.inference_time_ms) {
            meta.textContent += ` · ${p.inference_time_ms}ms`;
        }

        // Actions
        const actions = document.createElement('div');
        actions.className = 'prediction-review-actions';

        const btnApprove = document.createElement('button');
        btnApprove.className = 'btn-approve';
        btnApprove.textContent = 'Approve';
        btnApprove.onclick = (e) => {
            e.stopPropagation();
            this.reviewPrediction(p.id, 'approve');
        };

        const btnReject = document.createElement('button');
        btnReject.className = 'btn-reject';
        btnReject.textContent = 'Reject';
        btnReject.onclick = (e) => {
            e.stopPropagation();
            this.reviewPrediction(p.id, 'reject');
        };

        const btnCorrect = document.createElement('button');
        btnCorrect.className = 'btn-correct';
        btnCorrect.textContent = 'Correct';
        btnCorrect.onclick = (e) => {
            e.stopPropagation();
            this.reviewPrediction(p.id, 'correct');
        };

        actions.appendChild(btnApprove);
        actions.appendChild(btnReject);
        actions.appendChild(btnCorrect);

        card.appendChild(header);
        card.appendChild(meta);
        card.appendChild(actions);

        return card;
    },

    selectPrediction(id) {
        this.selectedId = id;
        const pred = this.predictions.find(p => p.id === id);
        if (!pred) return;

        // Seek video to prediction timestamp
        const video = document.getElementById('video-player');
        if (video && pred.prediction_type === 'keyframe' && pred.timestamp != null) {
            video.currentTime = pred.timestamp;
        } else if (video && pred.start_time != null) {
            video.currentTime = pred.start_time;
        }

        // Update highlight on SVG overlay
        this.updateBboxHighlight();

        this.render(document.getElementById('prediction-model-filter')?.value || '');
    },

    /** Draw all prediction bboxes as an SVG overlay on the video/image preview */
    drawAllBboxOverlays() {
        this.clearAllBboxOverlays();

        const wrapper = document.querySelector('.video-wrapper');
        if (!wrapper) return;

        const predsWithBbox = this.predictions.filter(p => p.bbox_x != null && p.bbox_width > 0);
        if (!predsWithBbox.length) return;

        const canvas = document.getElementById('bbox-canvas');
        if (!canvas) return;

        let natW, natH;
        if (window.isImageMode) {
            const img = document.getElementById('thumbnail-image');
            if (!img || !img.naturalWidth) return;
            natW = img.naturalWidth;
            natH = img.naturalHeight;
        } else {
            const video = document.getElementById('video-player');
            if (!video || !video.videoWidth) return;
            natW = video.videoWidth;
            natH = video.videoHeight;
        }

        const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.setAttribute('class', 'prediction-bbox-svg');
        svg.setAttribute('viewBox', `0 0 ${natW} ${natH}`);
        // Match canvas positioning exactly
        svg.style.position = 'absolute';
        svg.style.width = canvas.style.width;
        svg.style.height = canvas.style.height;
        svg.style.top = canvas.style.top || '50%';
        svg.style.left = canvas.style.left || '50%';
        svg.style.transform = canvas.style.transform || 'translate(-50%, -50%)';
        svg.style.pointerEvents = 'none';
        svg.style.zIndex = '10';

        predsWithBbox.forEach(p => {
            const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
            rect.setAttribute('x', p.bbox_x);
            rect.setAttribute('y', p.bbox_y);
            rect.setAttribute('width', p.bbox_width);
            rect.setAttribute('height', p.bbox_height);
            rect.setAttribute('data-pred-id', p.id);
            rect.setAttribute('class', p.id === this.selectedId ? 'pred-bbox pred-bbox-selected' : 'pred-bbox');
            rect.setAttribute('vector-effect', 'non-scaling-stroke');
            svg.appendChild(rect);
        });

        wrapper.appendChild(svg);
        this.bboxSvgOverlay = svg;
    },

    /** Update which bbox is highlighted without redrawing all */
    updateBboxHighlight() {
        if (!this.bboxSvgOverlay) return;
        this.bboxSvgOverlay.querySelectorAll('.pred-bbox').forEach(rect => {
            const id = parseInt(rect.getAttribute('data-pred-id'));
            if (id === this.selectedId) {
                rect.setAttribute('class', 'pred-bbox pred-bbox-selected');
            } else {
                rect.setAttribute('class', 'pred-bbox');
            }
        });
    },

    clearAllBboxOverlays() {
        if (this.bboxSvgOverlay) {
            this.bboxSvgOverlay.remove();
            this.bboxSvgOverlay = null;
        }
    },

    /** Called from switchTab to show/hide prediction overlays */
    onTabSwitch(tab) {
        if (tab === 'predictions') {
            this.drawAllBboxOverlays();
        } else {
            this.clearAllBboxOverlays();
        }
    },

    async reviewPrediction(id, action) {
        // Grab prediction before removing from list (need scenario/bbox for person identify)
        const pred = this.predictions.find(p => p.id === id);
        try {
            const resp = await fetch(`/api/ai/predictions/${id}/review`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    action: action,
                    reviewer: 'studio_user'
                })
            });
            const data = await resp.json();
            if (data.success) {
                this.predictions = this.predictions.filter(p => p.id !== id);
                if (this.selectedId === id) {
                    this.selectedId = null;
                }
                this.drawAllBboxOverlays();
                this.render(document.getElementById('prediction-model-filter')?.value || '');
                this.updateBadges();
                // Refresh annotations list when a prediction is approved/corrected
                if ((action === 'approve' || action === 'correct') && typeof loadKeyframeAnnotations === 'function') {
                    loadKeyframeAnnotations();
                }
                // Trigger person identification for person-related predictions
                if ((action === 'approve' || action === 'correct') && data.annotation_id && pred) {
                    this.triggerPersonIdentify(pred, data.annotation_id);
                }
            } else {
                alert('Review failed: ' + (data.error || 'Unknown error'));
            }
        } catch (e) {
            console.error('Failed to review prediction:', e);
            alert('Failed to review prediction: ' + e.message);
        }
    },

    async approveAllHighConfidence() {
        const highConf = this.predictions.filter(p => p.confidence >= 0.9);
        if (highConf.length === 0) {
            alert('No high-confidence predictions to approve.');
            return;
        }
        if (!confirm(`Approve ${highConf.length} predictions with confidence >= 90%?`)) return;

        let approved = 0;
        for (const pred of highConf) {
            try {
                const resp = await fetch(`/api/ai/predictions/${pred.id}/review`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ action: 'approve', reviewer: 'studio_user' })
                });
                const data = await resp.json();
                if (data.success) approved++;
            } catch (e) {
                console.error(`Failed to approve prediction ${pred.id}:`, e);
            }
        }

        alert(`Approved ${approved} of ${highConf.length} predictions.`);
        this.loadPredictions();
        if (approved > 0 && typeof loadKeyframeAnnotations === 'function') {
            loadKeyframeAnnotations();
        }
    },

    /** Check if a prediction scenario is person-related and trigger identify modal */
    triggerPersonIdentify(pred, annotationId) {
        const personScenarios = ['person_identification', 'person_detection', 'face_detection', 'person'];
        const scenario = (pred.scenario || '').toLowerCase();
        if (!personScenarios.some(s => scenario.includes(s))) return;
        if (!pred.bbox_x || !pred.bbox_width) return;

        // Capture current frame from video or thumbnail
        const frameData = this.captureFrame();
        if (!frameData) return;

        // Use the scenario workflow's person identify modal if available
        if (typeof scenarioWorkflow !== 'undefined' && scenarioWorkflow.showPersonIdentifyModal) {
            scenarioWorkflow._savedFrameData = frameData;
            scenarioWorkflow._savedBBox = {
                x: pred.bbox_x,
                y: pred.bbox_y,
                width: pred.bbox_width,
                height: pred.bbox_height
            };
            scenarioWorkflow._personQueue = [{ annotationId: annotationId, bbox: scenarioWorkflow._savedBBox }];
            scenarioWorkflow._personQueueIndex = 0;
            scenarioWorkflow.showNextPersonIdentify();
        }
    },

    /** Capture current video frame or thumbnail as data URL */
    captureFrame() {
        const canvas = document.createElement('canvas');
        let source;
        if (window.isImageMode) {
            source = document.getElementById('thumbnail-image');
            if (!source || !source.naturalWidth) return null;
            canvas.width = source.naturalWidth;
            canvas.height = source.naturalHeight;
        } else {
            source = document.getElementById('video-player');
            if (!source || !source.videoWidth) return null;
            canvas.width = source.videoWidth;
            canvas.height = source.videoHeight;
        }
        const ctx = canvas.getContext('2d');
        ctx.drawImage(source, 0, 0, canvas.width, canvas.height);
        return canvas.toDataURL('image/jpeg', 0.95);
    },

    formatTime(seconds) {
        if (seconds == null) return '-';
        const m = Math.floor(seconds / 60);
        const s = Math.floor(seconds % 60);
        return m + ':' + String(s).padStart(2, '0');
    }
};

// Keyboard shortcuts for prediction review
document.addEventListener('keydown', function(e) {
    if (!predictionReview.selectedId) return;
    // Only if predictions tab is active
    const predTab = document.getElementById('tab-predictions');
    if (!predTab || !predTab.classList.contains('active')) return;
    // Don't intercept if user is typing in an input
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT') return;

    if (e.key === 'a' || e.key === 'A') {
        e.preventDefault();
        predictionReview.reviewPrediction(predictionReview.selectedId, 'approve');
    } else if (e.key === 'r' || e.key === 'R') {
        e.preventDefault();
        predictionReview.reviewPrediction(predictionReview.selectedId, 'reject');
    } else if (e.key === 'n' || e.key === 'N') {
        e.preventDefault();
        // Select next prediction
        const idx = predictionReview.predictions.findIndex(p => p.id === predictionReview.selectedId);
        if (idx >= 0 && idx < predictionReview.predictions.length - 1) {
            predictionReview.selectPrediction(predictionReview.predictions[idx + 1].id);
        }
    }
});

// Auto-initialize when video ID is available
document.addEventListener('DOMContentLoaded', function() {
    const urlParams = new URLSearchParams(window.location.search);
    const videoId = urlParams.get('id') || window.location.pathname.split('/').pop();
    if (videoId && !isNaN(videoId)) {
        predictionReview.init(parseInt(videoId));
    }
});
