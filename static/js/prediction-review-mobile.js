/**
 * Prediction Review Mobile - Tinder-style card swiping for AI prediction review.
 * Self-contained vanilla JS. No jQuery, no frameworks.
 *
 * Screens:
 *   S0 (queue)   - Video selection with pending counts
 *   S1 (review)  - Card swiping with bbox overlay
 *   S2 (summary) - Session results
 */
var ReviewApp = {
    // --------------- State ---------------
    screen: 'queue',
    predictions: [],
    currentIndex: 0,
    videoId: null,
    videoTitle: '',
    reviewLog: [],
    undoStack: null,
    syncQueue: [],
    syncTimer: null,
    sessionStats: { approved: 0, rejected: 0, skipped: 0 },
    skippedIds: new Set(),
    imageCache: {},
    activeFilter: 'all',
    queueData: null,
    _maskIdCounter: 0,

    // Review guidance descriptions per scenario
    scenarioGuidance: {
        'person_detection': 'Is there a person inside the bounding box? The box should tightly wrap the full body or visible portion.',
        'face_detection': 'Is there a face inside the bounding box? The box should frame the face clearly, not empty space or other objects.',
        'person_identification': 'Is the identified person correct? Check if the face/body matches the labeled identity.',
        'vehicle_detection': 'Is there a vehicle inside the bounding box? The box should tightly enclose the entire vehicle.',
        'boat_detection': 'Is there a boat inside the bounding box? The box should enclose the full vessel including hull and cabin.',
        'license_plate': 'Is there a legible license plate inside the bounding box? The plate text should be readable.',
        'animal_detection': 'Is there an animal inside the bounding box? The box should tightly wrap the animal.',
        'object_detection': 'Is there a recognizable object inside the bounding box? The box should tightly wrap the detected item.'
    },

    // Touch state
    touch: {
        startX: 0,
        startY: 0,
        currentX: 0,
        currentY: 0,
        isDragging: false,
        direction: null,
        startTime: 0,
        _hapticFired: false,
        _hapticFiredV: false
    },

    // Sheet drag state
    sheetTouch: {
        startY: 0,
        currentY: 0,
        isDragging: false
    },

    // --------------- DOM refs (cached on init) ---------------
    els: {},

    // --------------- Init ---------------
    init: function() {
        this.cacheElements();
        this.bindEvents();

        var params = new URLSearchParams(window.location.search);
        var vid = params.get('video_id');
        if (vid) {
            this.startReview(vid, '');
        } else {
            this.showScreen('queue');
            this.loadQueueSummary();
        }
    },

    cacheElements: function() {
        var ids = [
            'queue-screen', 'review-screen', 'summary-screen', 'history-screen',
            'video-list', 'queue-summary', 'pending-count', 'video-count',
            'all-videos-card', 'all-videos-subtitle',
            'card-container', 'reject-sheet',
            'approve-button', 'reject-button', 'skip-button', 'undo-button',
            'review-back', 'queue-back', 'history-back', 'history-button',
            'metadata-strip', 'pred-class', 'pred-confidence', 'pred-model', 'review-guidance',
            'progress-fill', 'review-count', 'position-dots',
            'glow-left', 'glow-right', 'review-video-title',
            'other-input-container', 'other-input',
            'skip-feedback', 'done-feedback',
            'completion-ring-fill', 'completion-count',
            'approved-count', 'rejected-count', 'skipped-count', 'skipped-line',
            'back-to-queue', 'review-skipped',
            'sheet-undo-button', 'summary-undo-button', 'history-list'
        ];
        for (var i = 0; i < ids.length; i++) {
            var id = ids[i];
            var key = id.replace(/-([a-z])/g, function(_, c) { return c.toUpperCase(); });
            this.els[key] = document.getElementById(id);
        }
    },

    bindEvents: function() {
        var self = this;

        // Action buttons
        if (this.els.approveButton) {
            this.els.approveButton.addEventListener('click', function() {
                self.commitAction('approve');
            });
        }
        if (this.els.rejectButton) {
            this.els.rejectButton.addEventListener('click', function() {
                self.handleRejectButton();
            });
        }
        if (this.els.skipButton) {
            this.els.skipButton.addEventListener('click', function() {
                self.commitAction('skip');
            });
        }
        if (this.els.undoButton) {
            this.els.undoButton.addEventListener('click', function() {
                self.undo();
            });
        }

        // Back buttons
        if (this.els.reviewBack) {
            this.els.reviewBack.addEventListener('click', function() {
                self.confirmBack();
            });
        }

        // Summary buttons
        if (this.els.backToQueue) {
            this.els.backToQueue.addEventListener('click', function() {
                self.showQueue();
            });
        }
        if (this.els.reviewSkipped) {
            this.els.reviewSkipped.addEventListener('click', function() {
                self.reviewSkippedItems();
            });
        }
        if (this.els.summaryUndoButton) {
            this.els.summaryUndoButton.addEventListener('click', function() {
                self.undo();
                self.showScreen('review');
                self.renderCurrentCard();
            });
        }

        // History buttons
        if (this.els.historyButton) {
            this.els.historyButton.addEventListener('click', function() {
                self.showHistory();
            });
        }
        if (this.els.historyBack) {
            this.els.historyBack.addEventListener('click', function() {
                self.showQueue();
            });
        }

        // History filter chips
        var historyChips = document.querySelectorAll('[data-history-filter]');
        for (var i = 0; i < historyChips.length; i++) {
            historyChips[i].addEventListener('click', function() {
                for (var j = 0; j < historyChips.length; j++) historyChips[j].classList.remove('active');
                this.classList.add('active');
                self.loadHistory(this.getAttribute('data-history-filter'));
            });
        }

        // Reject sheet undo button
        if (this.els.sheetUndoButton) {
            this.els.sheetUndoButton.addEventListener('click', function() {
                self.hideRejectSheet();
                self.undo();
            });
        }

        // Reject sheet buttons
        if (this.els.skipFeedback) {
            this.els.skipFeedback.addEventListener('click', function() {
                self.finishReject(null);
            });
        }
        if (this.els.doneFeedback) {
            this.els.doneFeedback.addEventListener('click', function() {
                self.finishRejectWithReasons();
            });
        }

        // Reject reason chips
        var chips = document.querySelectorAll('.reason-chip');
        for (var i = 0; i < chips.length; i++) {
            (function(chip) {
                chip.addEventListener('click', function() {
                    var reason = chip.getAttribute('data-reason');
                    if (reason === 'other') {
                        chip.classList.toggle('chip-selected');
                        if (self.els.otherInputContainer) {
                            self.els.otherInputContainer.classList.toggle('hidden', !chip.classList.contains('chip-selected'));
                        }
                        if (chip.classList.contains('chip-selected') && self.els.otherInput) {
                            self.els.otherInput.focus();
                        }
                    } else {
                        chip.classList.toggle('chip-selected');
                    }
                });
            })(chips[i]);
        }

        // Filter chips in queue
        var filterChips = document.querySelectorAll('.filter-chip');
        for (var i = 0; i < filterChips.length; i++) {
            (function(chip) {
                chip.addEventListener('click', function() {
                    filterChips.forEach(function(c) { c.classList.remove('active'); });
                    chip.classList.add('active');
                    self.activeFilter = chip.getAttribute('data-filter');
                    self.renderQueueList();
                });
            })(filterChips[i]);
        }

        // Touch/pointer events on card container
        if (this.els.cardContainer) {
            var container = this.els.cardContainer;
            container.addEventListener('pointerdown', function(e) { self.onPointerDown(e); });
            container.addEventListener('pointermove', function(e) { self.onPointerMove(e); });
            container.addEventListener('pointerup', function(e) { self.onPointerUp(e); });
            container.addEventListener('pointercancel', function(e) { self.onPointerUp(e); });
            container.addEventListener('contextmenu', function(e) { e.preventDefault(); });
        }

        // Sheet swipe-down-to-dismiss
        var sheet = this.els.rejectSheet;
        if (sheet) {
            var grabber = sheet.querySelector('.sheet-grabber');
            if (grabber) {
                grabber.addEventListener('pointerdown', function(e) { self.onSheetPointerDown(e); });
            }
            sheet.addEventListener('pointermove', function(e) { self.onSheetPointerMove(e); });
            sheet.addEventListener('pointerup', function(e) { self.onSheetPointerUp(e); });
        }

        // Keyboard shortcuts
        document.addEventListener('keydown', function(e) {
            if (self.screen !== 'review') return;
            if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
            if (e.key === 'ArrowRight' || e.key === 'a') {
                e.preventDefault();
                self.commitAction('approve');
            } else if (e.key === 'ArrowLeft' || e.key === 'r') {
                e.preventDefault();
                self.handleRejectButton();
            } else if (e.key === 'ArrowUp' || e.key === 's') {
                e.preventDefault();
                self.commitAction('skip');
            } else if (e.key === 'z' && (e.ctrlKey || e.metaKey)) {
                e.preventDefault();
                self.undo();
            }
        });
    },

    // --------------- Screen management ---------------
    showScreen: function(name) {
        this.screen = name;
        var screens = ['queue', 'review', 'summary', 'history'];
        for (var i = 0; i < screens.length; i++) {
            var el = document.getElementById(screens[i] + '-screen');
            if (el) {
                if (screens[i] === name) {
                    el.classList.remove('hidden');
                } else {
                    el.classList.add('hidden');
                }
            }
        }
    },

    confirmBack: function() {
        var total = this.sessionStats.approved + this.sessionStats.rejected + this.sessionStats.skipped;
        if (total > 0 && this.syncQueue.length > 0) {
            this.flushSync();
        }
        if (total > 0) {
            this.showSummary();
        } else {
            this.showQueue();
        }
    },

    // --------------- S0: Queue Screen ---------------
    loadQueueSummary: function() {
        var self = this;
        fetch('/api/ai/predictions/review-queue/summary')
            .then(function(resp) { return resp.json(); })
            .then(function(data) {
                self.queueData = data;
                self.renderQueueSummary(data);
            })
            .catch(function(err) {
                console.error('Failed to load queue summary:', err);
            });
    },

    renderQueueSummary: function(data) {
        if (this.els.pendingCount) {
            this.els.pendingCount.textContent = data.total_pending || 0;
        }
        if (this.els.videoCount) {
            this.els.videoCount.textContent = data.video_count || 0;
        }

        // Update "All Videos" card
        if (this.els.allVideosSubtitle) {
            this.els.allVideosSubtitle.textContent = (data.total_pending || 0) + ' predictions pending';
        }

        var allCard = this.els.allVideosCard;
        if (allCard) {
            var totalAll = 0;
            var reviewedAll = 0;
            if (data.videos) {
                for (var i = 0; i < data.videos.length; i++) {
                    totalAll += data.videos[i].total_count || 0;
                    reviewedAll += data.videos[i].reviewed_count || 0;
                }
            }
            var pctAll = totalAll > 0 ? reviewedAll / totalAll : 0;
            var ringFill = allCard.querySelector('.progress-ring-fill');
            if (ringFill) {
                var circumference = 2 * Math.PI * 14;
                ringFill.setAttribute('stroke-dashoffset', String(circumference * (1 - pctAll)));
            }
            allCard.onclick = function() { ReviewApp.startReview(null, 'All Videos'); };
        }

        this.renderQueueList();
    },

    renderQueueList: function() {
        if (!this.queueData || !this.els.videoList) return;
        var data = this.queueData;

        // Remove existing video cards (keep the All Videos card)
        var existing = this.els.videoList.querySelectorAll('.video-card:not(#all-videos-card)');
        for (var i = 0; i < existing.length; i++) {
            existing[i].remove();
        }

        if (!data.videos || data.videos.length === 0) return;

        var filtered = data.videos;
        if (this.activeFilter === 'high') {
            filtered = data.videos.filter(function(v) { return (v.avg_confidence || 0) >= 0.8; });
        } else if (this.activeFilter === 'low') {
            filtered = data.videos.filter(function(v) { return (v.avg_confidence || 0) < 0.8; });
        }

        for (var i = 0; i < filtered.length; i++) {
            var card = this.createVideoCard(filtered[i]);
            this.els.videoList.appendChild(card);
        }
    },

    createVideoCard: function(v) {
        var self = this;
        var card = document.createElement('div');
        card.className = 'video-card';
        card.setAttribute('data-video-id', v.video_id);

        // Thumbnail
        var thumbDiv = document.createElement('div');
        thumbDiv.className = 'video-card-thumbnail';
        if (v.thumbnail_path) {
            var img = document.createElement('img');
            img.src = this.getThumbUrl(v.thumbnail_path);
            img.alt = '';
            img.loading = 'lazy';
            img.onerror = function() { this.style.display = 'none'; };
            thumbDiv.appendChild(img);
        } else {
            var placeholderSvg = this._createPlaceholderSvg();
            thumbDiv.appendChild(placeholderSvg);
        }

        // Info
        var infoDiv = document.createElement('div');
        infoDiv.className = 'video-card-info';

        var titleDiv = document.createElement('div');
        titleDiv.className = 'video-card-title';
        titleDiv.textContent = v.video_title || 'Video #' + v.video_id;

        var subtitleDiv = document.createElement('div');
        subtitleDiv.className = 'video-card-subtitle';
        subtitleDiv.textContent = (v.pending_count || 0) + ' pending';

        infoDiv.appendChild(titleDiv);
        infoDiv.appendChild(subtitleDiv);

        // Progress ring
        var total = v.total_count || 0;
        var reviewed = v.reviewed_count || 0;
        var pct = total > 0 ? reviewed / total : 0;
        var circumference = 2 * Math.PI * 14;
        var offset = circumference * (1 - pct);

        var ringSvg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        ringSvg.setAttribute('class', 'progress-ring');
        ringSvg.setAttribute('width', '32');
        ringSvg.setAttribute('height', '32');

        var bgCircle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
        bgCircle.setAttribute('cx', '16');
        bgCircle.setAttribute('cy', '16');
        bgCircle.setAttribute('r', '14');
        bgCircle.setAttribute('stroke', '#262626');
        bgCircle.setAttribute('stroke-width', '2.5');
        bgCircle.setAttribute('fill', 'none');

        var fillCircle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
        fillCircle.setAttribute('cx', '16');
        fillCircle.setAttribute('cy', '16');
        fillCircle.setAttribute('r', '14');
        fillCircle.setAttribute('stroke', '#0D9488');
        fillCircle.setAttribute('stroke-width', '2.5');
        fillCircle.setAttribute('fill', 'none');
        fillCircle.setAttribute('stroke-dasharray', String(circumference));
        fillCircle.setAttribute('stroke-dashoffset', String(offset));
        fillCircle.setAttribute('stroke-linecap', 'round');
        fillCircle.setAttribute('transform', 'rotate(-90 16 16)');

        ringSvg.appendChild(bgCircle);
        ringSvg.appendChild(fillCircle);

        card.appendChild(thumbDiv);
        card.appendChild(infoDiv);
        card.appendChild(ringSvg);

        card.addEventListener('click', function() {
            self.startReview(v.video_id, v.video_title || 'Video #' + v.video_id);
        });

        return card;
    },

    /** Create a placeholder SVG icon for videos without thumbnails */
    _createPlaceholderSvg: function() {
        var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.setAttribute('width', '56');
        svg.setAttribute('height', '56');
        svg.setAttribute('viewBox', '0 0 56 56');
        svg.setAttribute('fill', 'none');

        var rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
        rect.setAttribute('width', '56');
        rect.setAttribute('height', '56');
        rect.setAttribute('rx', '8');
        rect.setAttribute('fill', '#262626');

        var path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        path.setAttribute('d', 'M20 18v20l16-10z');
        path.setAttribute('fill', '#525252');

        svg.appendChild(rect);
        svg.appendChild(path);
        return svg;
    },

    // --------------- S1: Review Screen ---------------
    startReview: function(videoId, videoTitle) {
        var self = this;
        this.videoId = videoId;
        this.videoTitle = videoTitle || '';
        this.currentIndex = 0;
        this.sessionStats = { approved: 0, rejected: 0, skipped: 0 };
        this.reviewLog = [];
        this.undoStack = null;
        this.skippedIds = new Set();
        this.syncQueue = [];

        if (this.els.reviewVideoTitle) {
            this.els.reviewVideoTitle.textContent = videoTitle || 'All Videos';
        }

        var url = '/api/ai/predictions/review-queue?limit=200';
        if (videoId) {
            url += '&video_id=' + encodeURIComponent(videoId);
        }

        fetch(url)
            .then(function(resp) { return resp.json(); })
            .then(function(data) {
                self.predictions = data.predictions || [];
                if (self.predictions.length === 0) {
                    self.showScreen('review');
                    self.renderEmptyState();
                    return;
                }
                self.showScreen('review');
                self.renderCurrentCard();
                self.preloadImages(3);
                self.updateProgress();
                self.updateDots();
                self.updateUndoButton();
            })
            .catch(function(err) {
                console.error('Failed to load predictions:', err);
            });
    },

    renderEmptyState: function() {
        if (!this.els.cardContainer) return;
        this.els.cardContainer.textContent = '';

        var empty = document.createElement('div');
        empty.className = 'empty-card';

        var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.setAttribute('width', '48');
        svg.setAttribute('height', '48');
        svg.setAttribute('viewBox', '0 0 24 24');
        svg.setAttribute('fill', 'none');
        svg.setAttribute('stroke', '#525252');
        svg.setAttribute('stroke-width', '1.5');
        var svgPath = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        svgPath.setAttribute('d', 'M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z');
        svg.appendChild(svgPath);
        empty.appendChild(svg);

        var p = document.createElement('p');
        p.style.cssText = 'color:#a3a3a3;margin-top:12px;';
        p.textContent = 'No predictions to review';
        empty.appendChild(p);

        this.els.cardContainer.appendChild(empty);
        this.updateProgress();
    },

    renderCurrentCard: function() {
        if (!this.els.cardContainer) return;
        if (this.currentIndex >= this.predictions.length) {
            this.showSummary();
            return;
        }

        var pred = this.predictions[this.currentIndex];
        this.els.cardContainer.textContent = '';

        var card = document.createElement('div');
        card.className = 'review-card';
        card.style.willChange = 'transform';

        // Image container
        var imgWrap = document.createElement('div');
        imgWrap.className = 'card-image-container';

        var img = document.createElement('img');
        img.className = 'card-image';
        img.alt = 'Prediction frame';
        img.src = this.getThumbUrl(pred.thumbnail_path);
        img.onerror = function() { this.style.display = 'none'; };
        imgWrap.appendChild(img);

        // SVG bbox overlay — use image natural dimensions for accurate placement
        if (pred.bbox_x != null && pred.bbox_width > 0) {
            var self = this;
            var addBbox = function(vw, vh) {
                var bboxSvg = self.createBboxOverlay(pred, vw, vh);
                imgWrap.appendChild(bboxSvg);
            };
            if (pred.video_width && pred.video_height) {
                addBbox(pred.video_width, pred.video_height);
            } else {
                img.addEventListener('load', function() {
                    addBbox(this.naturalWidth, this.naturalHeight);
                });
            }
        }

        card.appendChild(imgWrap);

        // Swipe label overlays
        var approveLabel = document.createElement('div');
        approveLabel.className = 'swipe-label swipe-label-approve';
        approveLabel.textContent = 'APPROVE';
        card.appendChild(approveLabel);

        var rejectLabel = document.createElement('div');
        rejectLabel.className = 'swipe-label swipe-label-reject';
        rejectLabel.textContent = 'REJECT';
        card.appendChild(rejectLabel);

        var skipLabel = document.createElement('div');
        skipLabel.className = 'swipe-label swipe-label-skip';
        skipLabel.textContent = 'SKIP';
        card.appendChild(skipLabel);

        this.els.cardContainer.appendChild(card);

        // Entry animation
        card.style.opacity = '0';
        card.style.transform = 'scale(0.95)';
        requestAnimationFrame(function() {
            requestAnimationFrame(function() {
                card.style.transition = 'opacity 0.2s ease, transform 0.2s ease';
                card.style.opacity = '1';
                card.style.transform = 'translateX(0) rotate(0deg)';
            });
        });

        // Update metadata
        this.updateMetadata(pred);
        this.updateProgress();
        this.updateDots();
    },

    createBboxOverlay: function(pred, videoWidth, videoHeight) {
        var bx = pred.bbox_x;
        var by = pred.bbox_y;
        var bw = pred.bbox_width;
        var bh = pred.bbox_height;
        var maskId = 'dim-mask-' + (this._maskIdCounter++);

        var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.setAttribute('class', 'bbox-overlay');
        svg.setAttribute('viewBox', '0 0 ' + videoWidth + ' ' + videoHeight);
        svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');

        // Defs with mask
        var defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
        var mask = document.createElementNS('http://www.w3.org/2000/svg', 'mask');
        mask.setAttribute('id', maskId);

        var maskBg = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
        maskBg.setAttribute('width', '100%');
        maskBg.setAttribute('height', '100%');
        maskBg.setAttribute('fill', 'white');

        var maskCutout = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
        maskCutout.setAttribute('x', String(bx));
        maskCutout.setAttribute('y', String(by));
        maskCutout.setAttribute('width', String(bw));
        maskCutout.setAttribute('height', String(bh));
        maskCutout.setAttribute('fill', 'black');

        mask.appendChild(maskBg);
        mask.appendChild(maskCutout);
        defs.appendChild(mask);
        svg.appendChild(defs);

        // Dim overlay with cutout
        var dimRect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
        dimRect.setAttribute('width', '100%');
        dimRect.setAttribute('height', '100%');
        dimRect.setAttribute('fill', 'rgba(0,0,0,0.4)');
        dimRect.setAttribute('mask', 'url(#' + maskId + ')');
        svg.appendChild(dimRect);

        // Bbox outline
        var bboxRect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
        bboxRect.setAttribute('x', String(bx));
        bboxRect.setAttribute('y', String(by));
        bboxRect.setAttribute('width', String(bw));
        bboxRect.setAttribute('height', String(bh));
        bboxRect.setAttribute('fill', 'none');
        bboxRect.setAttribute('stroke', '#06B6D4');
        bboxRect.setAttribute('stroke-width', '2.5');
        bboxRect.setAttribute('vector-effect', 'non-scaling-stroke');
        svg.appendChild(bboxRect);

        // Label
        var className = this.extractClassName(pred);
        if (className) {
            var labelFontSize = Math.max(14, Math.min(videoHeight * 0.02, 24));
            var labelPadH = 6;
            var labelPadV = 4;
            var labelY = by - labelFontSize - labelPadV * 2 - 2;
            // If label would be above the image, put it inside the bbox top
            if (labelY < 0) {
                labelY = by + 2;
            }
            var approxWidth = className.length * labelFontSize * 0.6 + labelPadH * 2;

            var labelBg = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
            labelBg.setAttribute('x', String(bx));
            labelBg.setAttribute('y', String(labelY));
            labelBg.setAttribute('width', String(approxWidth));
            labelBg.setAttribute('height', String(labelFontSize + labelPadV * 2));
            labelBg.setAttribute('fill', 'rgba(0,0,0,0.7)');
            labelBg.setAttribute('rx', '3');
            svg.appendChild(labelBg);

            var text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
            text.setAttribute('x', String(bx + labelPadH));
            text.setAttribute('y', String(labelY + labelFontSize + labelPadV - 2));
            text.setAttribute('fill', '#fff');
            text.setAttribute('font-size', String(labelFontSize));
            text.setAttribute('font-family', 'system-ui, -apple-system, sans-serif');
            text.textContent = className;
            svg.appendChild(text);
        }

        return svg;
    },

    extractClassName: function(pred) {
        // For identification scenarios, show the identified name prominently
        if (pred.predicted_tags) {
            var tags = pred.predicted_tags;
            if (typeof tags === 'string') {
                try { tags = JSON.parse(tags); } catch (e) { tags = {}; }
            }
            if (tags && typeof tags === 'object') {
                if (tags.person_name) return tags.person_name;
                if (tags.class_name) return tags.class_name;
                if (tags.label) return tags.label;
            }
        }
        if (pred.scenario) return pred.scenario.replace(/_/g, ' ');
        return '';
    },

    updateMetadata: function(pred) {
        if (this.els.predClass) {
            var cls = this.extractClassName(pred);
            this.els.predClass.textContent = cls || 'Detection';
        }
        if (this.els.predConfidence) {
            this.els.predConfidence.textContent = this.formatConfidence(pred.confidence);
            this.els.predConfidence.className = 'metadata-confidence ' + this.getConfidenceClass(pred.confidence);
        }
        if (this.els.predModel) {
            var modelStr = pred.model_name || 'unknown';
            if (pred.model_version) modelStr += ' v' + pred.model_version;
            this.els.predModel.textContent = modelStr;
        }
        // Show review guidance for this scenario
        if (this.els.reviewGuidance) {
            var scenario = pred.scenario || '';
            var guidance = this.scenarioGuidance[scenario] || 'Does the bounding box correctly identify the detected object?';
            // For person identification, include the identified name
            if (scenario === 'person_identification') {
                var tags = pred.predicted_tags || {};
                if (typeof tags === 'string') { try { tags = JSON.parse(tags); } catch(e) { tags = {}; } }
                var name = tags.person_name || 'unknown';
                var similarity = tags.match_similarity ? Math.round(tags.match_similarity * 100) + '%' : '';
                guidance = 'The system thinks this is "' + name + '"' + (similarity ? ' (' + similarity + ' match)' : '') + '. Is this the correct person?';
            }
            this.els.reviewGuidance.textContent = guidance;
        }
    },

    updateProgress: function() {
        var total = this.predictions.length;
        var current = this.currentIndex;
        var pct = total > 0 ? (current / total) * 100 : 0;

        if (this.els.progressFill) {
            this.els.progressFill.style.width = pct + '%';
        }
        if (this.els.reviewCount) {
            this.els.reviewCount.textContent = current + '/' + total;
        }
    },

    updateDots: function() {
        if (!this.els.positionDots) return;
        this.els.positionDots.textContent = '';

        var total = this.predictions.length;
        var maxDots = 7;
        var start = Math.max(0, this.currentIndex - Math.floor(maxDots / 2));
        var end = Math.min(total, start + maxDots);
        if (end - start < maxDots) {
            start = Math.max(0, end - maxDots);
        }

        for (var i = start; i < end; i++) {
            var dot = document.createElement('div');
            dot.className = 'dot';
            if (i === this.currentIndex) dot.classList.add('active');
            if (i < this.currentIndex) dot.classList.add('done');
            this.els.positionDots.appendChild(dot);
        }
    },

    updateUndoButton: function() {
        if (this.els.undoButton) {
            this.els.undoButton.disabled = !this.undoStack;
            this.els.undoButton.style.opacity = this.undoStack ? '1' : '0.3';
        }
    },

    // --------------- S3: History Screen ---------------
    showHistory: function() {
        this.showScreen('history');
        this.loadHistory('all');
    },

    loadHistory: function(filter) {
        var self = this;
        var url = '/api/ai/predictions/review-history?limit=100';
        if (filter && filter !== 'all') {
            url += '&status=' + filter;
        }

        fetch(url)
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.success) {
                    self.renderHistory(data.predictions);
                }
            })
            .catch(function(err) {
                console.error('Failed to load history:', err);
            });
    },

    renderHistory: function(predictions) {
        var list = this.els.historyList;
        if (!list) return;
        list.textContent = '';

        if (predictions.length === 0) {
            var empty = document.createElement('div');
            empty.className = 'history-empty';
            empty.textContent = 'No reviewed predictions yet';
            list.appendChild(empty);
            return;
        }

        var self = this;
        for (var i = 0; i < predictions.length; i++) {
            var pred = predictions[i];
            var item = this.createHistoryItem(pred);
            list.appendChild(item);
        }
    },

    createHistoryItem: function(pred) {
        var self = this;
        var item = document.createElement('div');
        item.className = 'history-item';
        item.setAttribute('data-prediction-id', pred.id);

        // Thumbnail
        var img = document.createElement('img');
        img.className = 'history-thumb';
        img.src = this.getThumbUrl(pred.thumbnail_path);
        img.alt = '';
        img.onerror = function() { this.style.display = 'none'; };
        item.appendChild(img);

        // Info
        var info = document.createElement('div');
        info.className = 'history-info';

        var name = document.createElement('div');
        name.className = 'history-info-name';
        var tags = pred.predicted_tags || {};
        if (typeof tags === 'string') { try { tags = JSON.parse(tags); } catch(e) { tags = {}; } }
        name.textContent = tags.person_name || tags.class_name || tags.label || (pred.scenario || '').replace(/_/g, ' ') || 'Detection';
        info.appendChild(name);

        var detail = document.createElement('div');
        detail.className = 'history-info-detail';
        var conf = pred.confidence ? Math.round(pred.confidence * 100) + '%' : '';
        var reviewer = pred.reviewed_by || '';
        var timeStr = '';
        if (pred.reviewed_at) {
            var d = new Date(pred.reviewed_at);
            timeStr = d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
        }
        detail.textContent = [conf, reviewer, timeStr].filter(Boolean).join(' · ');
        info.appendChild(detail);

        item.appendChild(info);

        // Status badge
        var status = document.createElement('span');
        status.className = 'history-status ' + pred.review_status;
        status.textContent = pred.review_status;
        item.appendChild(status);

        // Revert button
        var revert = document.createElement('button');
        revert.className = 'history-revert-button';
        revert.textContent = 'Revert';
        revert.addEventListener('click', function() {
            self.revertPrediction(pred.id, item);
        });
        item.appendChild(revert);

        return item;
    },

    revertPrediction: function(predictionId, itemElement) {
        fetch('/api/ai/predictions/' + predictionId + '/undo', { method: 'POST' })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.success) {
                    itemElement.style.transition = 'opacity 0.3s ease, transform 0.3s ease';
                    itemElement.style.opacity = '0';
                    itemElement.style.transform = 'translateX(-100%)';
                    setTimeout(function() { itemElement.remove(); }, 300);
                } else {
                    alert('Failed to revert: ' + (data.error || 'Unknown error'));
                }
            })
            .catch(function(err) {
                console.error('Revert failed:', err);
            });
    },

    // --------------- Touch / Swipe handling ---------------
    onPointerDown: function(e) {
        if (this.screen !== 'review') return;
        if (e.pointerType === 'mouse' && e.button !== 0) return;

        var card = this.els.cardContainer.querySelector('.review-card');
        if (!card) return;

        this.touch.startX = e.clientX;
        this.touch.startY = e.clientY;
        this.touch.currentX = e.clientX;
        this.touch.currentY = e.clientY;
        this.touch.isDragging = true;
        this.touch.direction = null;
        this.touch.startTime = Date.now();
        this.touch._hapticFired = false;
        this.touch._hapticFiredV = false;

        card.style.transition = 'none';
        card.style.willChange = 'transform';

        if (this.els.cardContainer.setPointerCapture) {
            this.els.cardContainer.setPointerCapture(e.pointerId);
        }
    },

    onPointerMove: function(e) {
        if (!this.touch.isDragging || this.screen !== 'review') return;
        e.preventDefault();

        this.touch.currentX = e.clientX;
        this.touch.currentY = e.clientY;

        var deltaX = this.touch.currentX - this.touch.startX;
        var deltaY = this.touch.currentY - this.touch.startY;

        // Determine direction on first significant movement
        if (!this.touch.direction) {
            var absX = Math.abs(deltaX);
            var absY = Math.abs(deltaY);
            if (absX > 10 || absY > 10) {
                this.touch.direction = absX > absY ? 'horizontal' : 'vertical';
            } else {
                return;
            }
        }

        var card = this.els.cardContainer.querySelector('.review-card');
        if (!card) return;

        if (this.touch.direction === 'horizontal') {
            var rotation = deltaX * 0.03;
            card.style.transform = 'translateX(' + deltaX + 'px) rotate(' + rotation + 'deg)';

            // Edge glow
            if (this.els.glowRight) {
                this.els.glowRight.style.opacity = deltaX > 30 ? String(Math.min((deltaX - 30) / 80, 1)) : '0';
            }
            if (this.els.glowLeft) {
                this.els.glowLeft.style.opacity = deltaX < -30 ? String(Math.min((-deltaX - 30) / 80, 1)) : '0';
            }

            // Scale action buttons past 30px threshold
            if (deltaX > 30) {
                this.scaleButton(this.els.approveButton, Math.min(1 + (deltaX - 30) / 200, 1.3));
                this.scaleButton(this.els.rejectButton, 1);
            } else if (deltaX < -30) {
                this.scaleButton(this.els.rejectButton, Math.min(1 + (-deltaX - 30) / 200, 1.3));
                this.scaleButton(this.els.approveButton, 1);
            } else {
                this.scaleButton(this.els.approveButton, 1);
                this.scaleButton(this.els.rejectButton, 1);
            }

            // Show swipe labels past 80px
            var approveLabel = card.querySelector('.swipe-label-approve');
            var rejectLabel = card.querySelector('.swipe-label-reject');
            if (approveLabel) approveLabel.style.opacity = deltaX > 80 ? '1' : '0';
            if (rejectLabel) rejectLabel.style.opacity = deltaX < -80 ? '1' : '0';

            // Haptic feedback at threshold crossing
            if (Math.abs(deltaX) > 80 && !this.touch._hapticFired) {
                this.touch._hapticFired = true;
                this.vibrate([10]);
            }
            if (Math.abs(deltaX) <= 80) {
                this.touch._hapticFired = false;
            }

        } else if (this.touch.direction === 'vertical' && deltaY < 0) {
            card.style.transform = 'translateY(' + deltaY + 'px)';

            var skipLabel = card.querySelector('.swipe-label-skip');
            if (skipLabel) skipLabel.style.opacity = deltaY < -100 ? '1' : '0';

            if (deltaY < -100 && !this.touch._hapticFiredV) {
                this.touch._hapticFiredV = true;
                this.vibrate([10]);
            }
            if (deltaY >= -100) {
                this.touch._hapticFiredV = false;
            }
        }
    },

    onPointerUp: function(e) {
        if (!this.touch.isDragging) return;
        this.touch.isDragging = false;

        var deltaX = this.touch.currentX - this.touch.startX;
        var deltaY = this.touch.currentY - this.touch.startY;

        // Reset glows
        if (this.els.glowLeft) this.els.glowLeft.style.opacity = '0';
        if (this.els.glowRight) this.els.glowRight.style.opacity = '0';
        this.scaleButton(this.els.approveButton, 1);
        this.scaleButton(this.els.rejectButton, 1);

        if (this.touch.direction === 'horizontal' && Math.abs(deltaX) > 80) {
            if (deltaX > 0) {
                this.animateCardExit('right');
                this.commitAction('approve');
            } else {
                this.animateCardExit('left');
                this.commitAction('reject', null, true);
                this.showRejectSheet();
            }
        } else if (this.touch.direction === 'vertical' && deltaY < -100) {
            this.animateCardExit('up');
            this.commitAction('skip');
        } else {
            // Spring back
            var card = this.els.cardContainer.querySelector('.review-card');
            if (card) {
                card.style.transition = 'transform 0.3s cubic-bezier(0.175, 0.885, 0.32, 1.275)';
                card.style.transform = 'translateX(0) rotate(0deg)';
                var labels = card.querySelectorAll('.swipe-label');
                for (var i = 0; i < labels.length; i++) {
                    labels[i].style.opacity = '0';
                }
            }
        }

        this.touch.direction = null;
        this.touch._hapticFired = false;
        this.touch._hapticFiredV = false;
    },

    animateCardExit: function(direction) {
        var card = this.els.cardContainer.querySelector('.review-card');
        if (!card) return;

        var transform;
        if (direction === 'right') {
            transform = 'translateX(120vw) rotate(30deg)';
        } else if (direction === 'left') {
            transform = 'translateX(-120vw) rotate(-30deg)';
        } else {
            transform = 'translateY(-120vh)';
        }

        card.style.transition = 'transform 0.35s cubic-bezier(0.4, 0, 0.2, 1), opacity 0.35s ease';
        card.style.transform = transform;
        card.style.opacity = '0';
    },

    scaleButton: function(btn, scale) {
        if (btn) btn.style.transform = 'scale(' + scale + ')';
    },

    vibrate: function(pattern) {
        if (navigator.vibrate) {
            try { navigator.vibrate(pattern); } catch (e) { /* ignore */ }
        }
    },

    // --------------- Actions ---------------
    handleRejectButton: function() {
        var self = this;
        this.animateCardExit('left');
        this.commitAction('reject', null, true);
        setTimeout(function() {
            self.showRejectSheet();
        }, 200);
    },

    commitAction: function(action, notes, skipAdvance) {
        if (this.screen !== 'review') return;
        if (this.currentIndex >= this.predictions.length) return;

        var pred = this.predictions[this.currentIndex];

        // Animate exit if not already animating (button-triggered)
        var card = this.els.cardContainer.querySelector('.review-card');
        if (card && card.style.opacity !== '0') {
            if (action === 'approve') {
                this.animateCardExit('right');
            } else if (action === 'reject') {
                this.animateCardExit('left');
            } else {
                this.animateCardExit('up');
            }
        }

        // Haptic
        if (action === 'approve') {
            this.vibrate([10]);
        } else if (action === 'reject') {
            this.vibrate([10, 30, 10]);
        } else {
            this.vibrate([5]);
        }

        // Update stats
        if (action === 'approve') {
            this.sessionStats.approved++;
        } else if (action === 'reject') {
            this.sessionStats.rejected++;
        } else {
            this.sessionStats.skipped++;
        }

        if (action === 'skip') {
            this.skippedIds.add(pred.id);
        }

        // Review log
        this.reviewLog.push({
            prediction_id: pred.id,
            action: action,
            notes: notes || null,
            timestamp: Date.now()
        });

        // Sync queue (skip actions don't need server sync)
        if (action !== 'skip') {
            this.syncQueue.push({
                prediction_id: pred.id,
                action: action,
                notes: notes || null
            });
            this.scheduleSyncReviews();
        }

        // Undo stack (single level)
        this.undoStack = {
            prediction: pred,
            index: this.currentIndex,
            action: action,
            notes: notes || null
        };

        // Advance (unless deferred for reject sheet)
        if (!skipAdvance) {
            this.advanceToNextCard();
        }
    },

    advanceToNextCard: function() {
        this.currentIndex++;
        this.updateUndoButton();

        var self = this;
        setTimeout(function() {
            if (self.currentIndex < self.predictions.length) {
                self.renderCurrentCard();
                self.preloadImages(3);
            } else {
                self.showSummary();
            }
        }, 350);
    },

    // --------------- Reject Sheet ---------------
    showRejectSheet: function() {
        if (!this.els.rejectSheet) return;

        // Reset chip state
        var chips = this.els.rejectSheet.querySelectorAll('.reason-chip');
        for (var i = 0; i < chips.length; i++) {
            chips[i].classList.remove('chip-selected');
        }
        if (this.els.otherInputContainer) {
            this.els.otherInputContainer.classList.add('hidden');
        }
        if (this.els.otherInput) {
            this.els.otherInput.value = '';
        }

        this.els.rejectSheet.classList.add('sheet-visible');
    },

    hideRejectSheet: function() {
        if (this.els.rejectSheet) {
            this.els.rejectSheet.classList.remove('sheet-visible');
            this.els.rejectSheet.style.transform = '';
        }
    },

    finishRejectWithReasons: function() {
        var selectedChips = this.els.rejectSheet.querySelectorAll('.reason-chip.chip-selected');
        var reasons = [];
        for (var i = 0; i < selectedChips.length; i++) {
            var reason = selectedChips[i].getAttribute('data-reason');
            if (reason === 'other') {
                var otherText = this.els.otherInput ? this.els.otherInput.value.trim() : '';
                if (otherText) reasons.push(otherText);
            } else {
                reasons.push(reason);
            }
        }
        var notes = reasons.length > 0 ? reasons.join(', ') : null;
        this.hideRejectSheet();
        // Update notes on the already-committed rejection in the sync queue
        if (notes && this.undoStack) {
            this.undoStack.notes = notes;
            for (var i = this.syncQueue.length - 1; i >= 0; i--) {
                if (this.syncQueue[i].prediction_id === this.undoStack.prediction.id) {
                    this.syncQueue[i].notes = notes;
                    break;
                }
            }
        }
        this.advanceToNextCard();
    },

    finishReject: function(notes) {
        this.hideRejectSheet();
        // Update notes on the already-committed rejection if provided
        if (notes && this.undoStack) {
            this.undoStack.notes = notes;
            for (var i = this.syncQueue.length - 1; i >= 0; i--) {
                if (this.syncQueue[i].prediction_id === this.undoStack.prediction.id) {
                    this.syncQueue[i].notes = notes;
                    break;
                }
            }
        }
        this.advanceToNextCard();
    },

    // Sheet swipe-to-dismiss
    onSheetPointerDown: function(e) {
        this.sheetTouch.startY = e.clientY;
        this.sheetTouch.currentY = e.clientY;
        this.sheetTouch.isDragging = true;
        if (this.els.rejectSheet) {
            this.els.rejectSheet.style.transition = 'none';
        }
    },

    onSheetPointerMove: function(e) {
        if (!this.sheetTouch.isDragging) return;
        this.sheetTouch.currentY = e.clientY;
        var deltaY = this.sheetTouch.currentY - this.sheetTouch.startY;
        if (deltaY > 0 && this.els.rejectSheet) {
            this.els.rejectSheet.style.transform = 'translateY(' + deltaY + 'px)';
        }
    },

    onSheetPointerUp: function(e) {
        if (!this.sheetTouch.isDragging) return;
        this.sheetTouch.isDragging = false;
        var deltaY = this.sheetTouch.currentY - this.sheetTouch.startY;

        if (this.els.rejectSheet) {
            this.els.rejectSheet.style.transition = 'transform 0.3s ease';
        }

        if (deltaY > 100) {
            // Dismiss without feedback (reject already committed)
            this.hideRejectSheet();
            this.advanceToNextCard();
        } else {
            // Snap back
            if (this.els.rejectSheet) {
                this.els.rejectSheet.style.transform = '';
            }
        }
    },

    // --------------- Sync ---------------
    scheduleSyncReviews: function() {
        var self = this;
        if (this.syncTimer) clearTimeout(this.syncTimer);
        if (this.syncQueue.length >= 10) {
            this.syncReviews();
        } else {
            this.syncTimer = setTimeout(function() {
                self.syncReviews();
            }, 3000);
        }
    },

    syncReviews: function() {
        if (this.syncQueue.length === 0) return Promise.resolve();

        var batch = this.syncQueue.splice(0);
        var self = this;

        var reviews = [];
        for (var i = 0; i < batch.length; i++) {
            reviews.push({
                prediction_id: batch[i].prediction_id,
                action: batch[i].action,
                notes: batch[i].notes
            });
        }

        if (reviews.length === 0) return Promise.resolve();

        return fetch('/api/ai/predictions/batch-review', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                reviews: reviews,
                reviewer: 'mobile_reviewer'
            })
        })
        .then(function(resp) { return resp.json(); })
        .then(function(data) {
            if (data.failed && data.failed > 0) {
                console.warn('Some reviews failed:', data.failed);
            }
        })
        .catch(function(err) {
            console.error('Sync failed, re-queuing:', err);
            // Put failed items back at front
            self.syncQueue = batch.concat(self.syncQueue);
            self.showSyncError();
            // Retry after 10 seconds
            if (!self.syncTimer) {
                self.syncTimer = setTimeout(function() {
                    self.syncReviews();
                }, 10000);
            }
        });
    },

    flushSync: function() {
        if (this.syncTimer) {
            clearTimeout(this.syncTimer);
            this.syncTimer = null;
        }
        return this.syncReviews();
    },

    showSyncError: function() {
        var toast = document.createElement('div');
        toast.className = 'sync-toast';
        toast.textContent = 'Sync paused - retrying...';
        toast.style.cssText = 'position:fixed;bottom:100px;left:50%;transform:translateX(-50%);' +
            'background:rgba(239,68,68,0.9);color:#fff;padding:8px 16px;border-radius:20px;' +
            'font-size:13px;z-index:1000;opacity:0;transition:opacity 0.3s;';
        document.body.appendChild(toast);
        requestAnimationFrame(function() {
            toast.style.opacity = '1';
        });
        setTimeout(function() {
            toast.style.opacity = '0';
            setTimeout(function() {
                if (toast.parentNode) toast.parentNode.removeChild(toast);
            }, 300);
        }, 3000);
    },

    // --------------- Undo ---------------
    undo: function() {
        if (!this.undoStack) return;
        var undoItem = this.undoStack;
        this.undoStack = null;

        // Reverse stats
        if (undoItem.action === 'approve') {
            this.sessionStats.approved = Math.max(0, this.sessionStats.approved - 1);
        } else if (undoItem.action === 'reject') {
            this.sessionStats.rejected = Math.max(0, this.sessionStats.rejected - 1);
        } else {
            this.sessionStats.skipped = Math.max(0, this.sessionStats.skipped - 1);
        }

        if (undoItem.action === 'skip') {
            this.skippedIds.delete(undoItem.prediction.id);
        }

        // Remove from sync queue if not yet synced
        var predId = undoItem.prediction.id;
        this.syncQueue = this.syncQueue.filter(function(item) {
            return item.prediction_id !== predId;
        });

        // Remove from review log
        for (var i = this.reviewLog.length - 1; i >= 0; i--) {
            if (this.reviewLog[i].prediction_id === predId) {
                this.reviewLog.splice(i, 1);
                break;
            }
        }

        // Server-side undo for non-skip actions
        if (undoItem.action !== 'skip') {
            fetch('/api/ai/predictions/' + predId + '/undo', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' }
            }).catch(function(err) {
                console.error('Server undo failed:', err);
            });
        }

        // Go back
        this.currentIndex = undoItem.index;
        this.updateUndoButton();
        this.renderCurrentCard();
    },

    // --------------- Preload ---------------
    preloadImages: function(count) {
        for (var i = 1; i <= count; i++) {
            var idx = this.currentIndex + i;
            if (idx >= this.predictions.length) break;
            var pred = this.predictions[idx];
            var url = this.getThumbUrl(pred.thumbnail_path);
            if (url && !this.imageCache[url]) {
                var img = new Image();
                img.src = url;
                this.imageCache[url] = img;
            }
        }
    },

    // --------------- S2: Summary Screen ---------------
    showSummary: function() {
        this.flushSync();
        this.showScreen('summary');

        var total = this.sessionStats.approved + this.sessionStats.rejected + this.sessionStats.skipped;

        // Animate completion ring
        if (this.els.completionRingFill) {
            var circumference = 2 * Math.PI * 70; // r=70 from the HTML SVG
            this.els.completionRingFill.setAttribute('stroke-dashoffset', String(circumference));
            var fillEl = this.els.completionRingFill;
            requestAnimationFrame(function() {
                requestAnimationFrame(function() {
                    fillEl.style.transition = 'stroke-dashoffset 1s cubic-bezier(0.4, 0, 0.2, 1)';
                    fillEl.setAttribute('stroke-dashoffset', '0');
                });
            });
        }

        // Animate count number
        this.animateCount(this.els.completionCount, total, 800);

        // Stats
        if (this.els.approvedCount) this.els.approvedCount.textContent = String(this.sessionStats.approved);
        if (this.els.rejectedCount) this.els.rejectedCount.textContent = String(this.sessionStats.rejected);
        if (this.els.skippedCount) this.els.skippedCount.textContent = String(this.sessionStats.skipped);

        // Show/hide skipped line and button
        if (this.els.skippedLine) {
            this.els.skippedLine.style.display = this.sessionStats.skipped > 0 ? '' : 'none';
        }
        if (this.els.reviewSkipped) {
            this.els.reviewSkipped.style.display = this.sessionStats.skipped > 0 ? '' : 'none';
        }
    },

    animateCount: function(el, target, duration) {
        if (!el) return;
        var startTime = null;

        function step(timestamp) {
            if (!startTime) startTime = timestamp;
            var progress = Math.min((timestamp - startTime) / duration, 1);
            // Ease out cubic
            var eased = 1 - Math.pow(1 - progress, 3);
            el.textContent = String(Math.round(eased * target));
            if (progress < 1) {
                requestAnimationFrame(step);
            }
        }

        requestAnimationFrame(step);
    },

    // --------------- Navigation ---------------
    showQueue: function() {
        this.showScreen('queue');
        var self = this;
        var pending = this.flushSync();
        if (pending && pending.then) {
            pending.then(function() { self.loadQueueSummary(); });
        } else {
            this.loadQueueSummary();
        }
    },

    reviewSkippedItems: function() {
        if (this.skippedIds.size === 0) return;

        var skippedIdSet = this.skippedIds;
        var skippedPreds = this.predictions.filter(function(p) {
            return skippedIdSet.has(p.id);
        });

        if (skippedPreds.length === 0) {
            this.showQueue();
            return;
        }

        this.predictions = skippedPreds;
        this.currentIndex = 0;
        this.sessionStats = { approved: 0, rejected: 0, skipped: 0 };
        this.skippedIds = new Set();
        this.undoStack = null;
        this.reviewLog = [];

        this.showScreen('review');
        this.renderCurrentCard();
        this.preloadImages(3);
        this.updateProgress();
        this.updateDots();
        this.updateUndoButton();
    },

    // --------------- Utility ---------------
    formatConfidence: function(conf) {
        if (conf == null) return '?';
        return Math.round(conf * 100) + '%';
    },

    getConfidenceClass: function(conf) {
        if (conf >= 0.9) return 'conf-high';
        if (conf >= 0.7) return 'conf-medium';
        return 'conf-low';
    },

    escapeHtml: function(str) {
        if (!str) return '';
        var div = document.createElement('div');
        div.textContent = str;
        return div.textContent;
    },

    getThumbUrl: function(path) {
        if (!path) return '';
        var parts = path.replace(/\\/g, '/').split('/');
        var filename = parts[parts.length - 1];
        return '/thumbnails/' + encodeURIComponent(filename);
    }
};

// Boot on DOMContentLoaded
document.addEventListener('DOMContentLoaded', function() {
    ReviewApp.init();
});
