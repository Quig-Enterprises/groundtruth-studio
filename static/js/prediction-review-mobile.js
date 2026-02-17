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
    speedMode: false,
    classifyIncludePending: false,
    skippedIds: new Set(),
    imageCache: {},
    activeFilter: 'all',
    _scenarioFilterMap: { 'plates': 'license_plate', 'boat_reg': 'boat_registration' },
    queueData: null,
    _maskIdCounter: 0,
    classifyMode: false,
    classifyQueue: [],
    classifySyncQueue: [],
    reclassifyClasses: [],
    cameraTopClasses: [],
    reclassifySearchText: '',
    classifySyncTimer: null,
    _queueFiltered: [],
    _queueRenderedCount: 0,
    _queueBatchSize: 20,
    _queueObserver: null,
    groupedMode: true,
    conflictMode: false,
    conflicts: [],
    currentConflictIndex: -1,
    selectedClassification: null,

    // Known detection classes for reclassification
    knownClasses: [
        'sedan', 'pickup truck', 'SUV', 'minivan', 'van',
        'tractor', 'ATV', 'UTV', 'snowmobile', 'golf cart', 'motorcycle', 'trailer',
        'bus', 'semi truck', 'dump truck',
        'rowboat', 'fishing boat', 'speed boat', 'pontoon boat', 'kayak', 'canoe', 'sailboat', 'jet ski',
        'person', 'animal', 'flag', 'tree', 'snow', 'roof'
    ],

    // Review guidance descriptions per scenario
    scenarioGuidance: {
        'person_detection': 'Is there a person inside the bounding box? The box should tightly wrap the full body or visible portion.',
        'face_detection': 'Is there a face inside the bounding box? The box should frame the face clearly, not empty space or other objects.',
        'person_identification': 'Is the identified person correct? Check if the face/body matches the labeled identity.',
        'vehicle_detection': 'Is there a vehicle inside the bounding box? The box should tightly enclose the entire vehicle.',
        'boat_detection': 'Is there a boat inside the bounding box? The box should enclose the full vessel including hull and cabin.',
        'license_plate': 'Is there a legible license plate inside the bounding box? The plate text should be readable.',
        'boat_registration': 'Is there a visible boat registration number/name inside the bounding box?',
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

    // Zoom state
    zoom: {
        scale: 1,
        translateX: 0,
        translateY: 0,
        isPinching: false,
        isPanning: false,
        initialDistance: 0,
        initialScale: 1,
        lastTapTime: 0,
        pointers: new Map(),
        panStartX: 0,
        panStartY: 0,
        panBaseX: 0,
        panBaseY: 0
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
            'review-menu', 'review-menu-dropdown', 'menu-hard-refresh', 'menu-reset-zoom', 'menu-copy-debug',
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

        // Three-dot menu
        if (this.els.reviewMenu) {
            this.els.reviewMenu.addEventListener('click', function(e) {
                e.stopPropagation();
                var dd = self.els.reviewMenuDropdown;
                if (dd) dd.classList.toggle('hidden');
            });
        }
        if (this.els.menuHardRefresh) {
            this.els.menuHardRefresh.addEventListener('click', function() {
                location.reload(true);
            });
        }
        if (this.els.menuResetZoom) {
            this.els.menuResetZoom.addEventListener('click', function() {
                self._resetZoom();
                var dd = self.els.reviewMenuDropdown;
                if (dd) dd.classList.add('hidden');
            });
        }
        if (this.els.menuCopyDebug) {
            this.els.menuCopyDebug.addEventListener('click', function() {
                self.copyDebugContext();
                var dd = self.els.reviewMenuDropdown;
                if (dd) dd.classList.add('hidden');
            });
        }
        // Speed mode toggle
        var speedToggle = document.getElementById('speed-mode-toggle');
        if (speedToggle) {
            speedToggle.addEventListener('click', function() {
                self.speedMode = !self.speedMode;
                speedToggle.classList.toggle('active', self.speedMode);
                var label = speedToggle.querySelector('.speed-label');
                if (label) label.textContent = self.speedMode ? 'Speed: ON' : 'Speed: OFF';
            });
        }
        // Grouped mode toggle
        var groupedToggle = document.getElementById('grouped-toggle');
        if (groupedToggle) {
            groupedToggle.addEventListener('click', function() {
                self.groupedMode = !self.groupedMode;
                groupedToggle.classList.toggle('active', self.groupedMode);
                // Reload queue with new mode
                if (self.screen === 'queue') {
                    if (self.classifyMode) {
                        self.loadClassifyQueueSummary();
                    } else {
                        self.loadQueueSummary();
                    }
                }
            });
        }
        // Include pending toggle for classify mode
        var pendingToggle = document.getElementById('pending-toggle');
        if (pendingToggle) {
            pendingToggle.addEventListener('click', function() {
                self.classifyIncludePending = !self.classifyIncludePending;
                pendingToggle.classList.toggle('active', self.classifyIncludePending);
                var label = pendingToggle.querySelector('.pending-label');
                if (label) label.textContent = self.classifyIncludePending ? 'Pending: ON' : 'Pending: OFF';
                // Reload queue with new filter
                if (self.classifyMode && self.screen === 'queue') {
                    self.loadClassifyQueueSummary();
                }
            });
        }
        // Close menu when tapping elsewhere
        document.addEventListener('click', function() {
            var dd = self.els.reviewMenuDropdown;
            if (dd && !dd.classList.contains('hidden')) {
                dd.classList.add('hidden');
            }
        });

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
                var filterValue = this.getAttribute('data-history-filter');

                // Show/hide classification subfilter dropdown
                var subfilters = document.getElementById('history-subfilters');
                if (subfilters) {
                    if (filterValue === 'classified' || filterValue === 'rejected') {
                        subfilters.style.display = 'block';
                    } else {
                        subfilters.style.display = 'none';
                    }
                }

                self.loadHistory(filterValue);
            });
        }

        // Classification select dropdown
        var classSelect = document.getElementById('history-classification-select');
        if (classSelect) {
            classSelect.addEventListener('change', function() {
                self.loadHistory(self.historyStatusFilter);
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
                        // Hide class picker if showing
                        var classPicker = document.getElementById('class-picker-container');
                        if (classPicker) classPicker.classList.add('hidden');
                    } else if (reason === 'wrong_class') {
                        chip.classList.toggle('chip-selected');
                        self.toggleClassPicker(chip.classList.contains('chip-selected'));
                    } else {
                        chip.classList.toggle('chip-selected');
                    }
                });
            })(chips[i]);
        }

        // Filter chips in queue
        var filterChips = document.querySelectorAll('.filter-chips .filter-chip');
        for (var i = 0; i < filterChips.length; i++) {
            (function(chip) {
                chip.addEventListener('click', function() {
                    filterChips.forEach(function(c) { c.classList.remove('active'); });
                    chip.classList.add('active');
                    self.activeFilter = chip.getAttribute('data-filter');
                    self.classifyMode = (self.activeFilter === 'classify');
                    self.conflictMode = (self.activeFilter === 'conflicts');
                    var pendingToggle = document.getElementById('pending-toggle');
                    if (pendingToggle) pendingToggle.style.display = self.classifyMode ? '' : 'none';
                    var speedToggle = document.getElementById('speed-mode-toggle');
                    if (speedToggle) speedToggle.style.display = self.classifyMode ? 'none' : '';
                    if (self.conflictMode) {
                        self.loadConflicts();
                    } else {
                        // Restore All Videos card visibility when leaving conflict mode
                        var allCard = document.getElementById('all-videos-card');
                        if (allCard) allCard.style.display = '';
                        // Remove conflict list if present
                        var oldConflictList = self.els.videoList ? self.els.videoList.querySelector('.conflict-list') : null;
                        if (oldConflictList) oldConflictList.remove();
                        // Restore queue summary spans if conflict mode destroyed them
                        if (!document.getElementById('pending-count')) {
                            var summaryEl = document.getElementById('queue-summary');
                            if (summaryEl) {
                                summaryEl.innerHTML = '<span id="pending-count">0</span> pending across <span id="video-count">0</span> videos';
                                self.els.pendingCount = document.getElementById('pending-count');
                                self.els.videoCount = document.getElementById('video-count');
                            }
                        }
                        if (self.classifyMode) {
                            self.loadClassifyQueueSummary();
                        } else if (self._scenarioFilterMap[self.activeFilter]) {
                            self.loadQueueSummary();
                        } else {
                            // Update summary counts from cached data
                            if (self.queueData) {
                                if (self.els.pendingCount) self.els.pendingCount.textContent = self.queueData.total_pending || 0;
                                if (self.els.videoCount) self.els.videoCount.textContent = self.queueData.video_count || 0;
                            }
                            self.renderQueueList();
                        }
                    }
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
        if (total > 0 && this.classifyMode && this.classifySyncQueue.length > 0) {
            this.flushClassifySync();
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
        var url = '/api/ai/predictions/review-queue/summary';
        if (this.groupedMode) url += '?grouped=1';
        // Add scenario filter if active
        if (this._scenarioFilterMap[this.activeFilter]) {
            url += (url.indexOf('?') >= 0 ? '&' : '?') + 'scenario=' + this._scenarioFilterMap[this.activeFilter];
        }
        fetch(url)
            .then(function(resp) { return resp.json(); })
            .then(function(data) {
                self.queueData = data;
                self.renderQueueSummary(data);
                // Also fetch conflict count for badge
                fetch('/api/ai/tracks/conflicts')
                    .then(function(r) { return r.json(); })
                    .then(function(cData) {
                        if (cData.success) {
                            var badge = document.getElementById('conflict-count-badge');
                            if (badge) badge.textContent = (cData.conflicts || []).length > 0 ? (cData.conflicts || []).length : '';
                        }
                    })
                    .catch(function() {});
            })
            .catch(function(err) {
                console.error('Failed to load queue summary:', err);
            });
    },

    loadClassifyQueueSummary: function() {
        var self = this;
        var summaryUrl = '/api/ai/predictions/classification-queue/summary';
        if (self.classifyIncludePending) summaryUrl += '?include_pending=true';
        fetch(summaryUrl)
            .then(function(resp) { return resp.json(); })
            .then(function(data) {
                if (!data.success) return;
                // Reformat to match queueData shape for renderQueueList
                self.queueData = {
                    total_pending: data.total_needing_classification || 0,
                    video_count: data.video_count || 0,
                    videos: (data.videos || []).map(function(v) {
                        return {
                            video_id: v.video_id,
                            video_title: v.video_title,
                            thumbnail_path: v.thumbnail_path,
                            pending_count: v.pending_classification,
                            total_count: v.pending_classification,
                            reviewed_count: 0,
                            avg_confidence: v.avg_confidence
                        };
                    })
                };
                self.renderQueueSummary(self.queueData);
            })
            .catch(function(err) {
                console.error('Failed to load classify queue:', err);
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
            if (this.groupedMode && data.total_predictions && data.total_predictions !== data.total_pending) {
                this.els.allVideosSubtitle.textContent = (data.total_pending || 0) + ' groups (' + (data.total_predictions || 0) + ' predictions)';
            } else {
                this.els.allVideosSubtitle.textContent = (data.total_pending || 0) + ' predictions pending';
            }
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

        // Remove old sentinel
        var oldSentinel = this.els.videoList.querySelector('.queue-sentinel');
        if (oldSentinel) oldSentinel.remove();

        // Disconnect old observer
        if (this._queueObserver) {
            this._queueObserver.disconnect();
            this._queueObserver = null;
        }

        if (!data.videos || data.videos.length === 0) return;

        var filtered = data.videos;
        if (this.activeFilter === 'high') {
            filtered = data.videos.filter(function(v) { return (v.avg_confidence || 0) >= 0.8; });
        } else if (this.activeFilter === 'low') {
            filtered = data.videos.filter(function(v) { return (v.avg_confidence || 0) < 0.8; });
        }

        this._queueFiltered = filtered;
        this._queueRenderedCount = 0;
        this._renderQueueBatch();
    },

    _renderQueueBatch: function() {
        if (!this.els.videoList || this._queueRenderedCount >= this._queueFiltered.length) return;

        var end = Math.min(this._queueRenderedCount + this._queueBatchSize, this._queueFiltered.length);

        // Remove old sentinel before appending new cards
        var oldSentinel = this.els.videoList.querySelector('.queue-sentinel');
        if (oldSentinel) oldSentinel.remove();

        for (var i = this._queueRenderedCount; i < end; i++) {
            var card = this.createVideoCard(this._queueFiltered[i]);
            this.els.videoList.appendChild(card);
        }
        this._queueRenderedCount = end;

        // Add sentinel for next batch if more items remain
        if (this._queueRenderedCount < this._queueFiltered.length) {
            var sentinel = document.createElement('div');
            sentinel.className = 'queue-sentinel';
            sentinel.style.height = '1px';
            this.els.videoList.appendChild(sentinel);
            this._observeSentinel(sentinel);
        }
    },

    _observeSentinel: function(sentinel) {
        var self = this;
        if (this._queueObserver) {
            this._queueObserver.disconnect();
        }
        this._queueObserver = new IntersectionObserver(function(entries) {
            if (entries[0].isIntersecting) {
                self._queueObserver.disconnect();
                self._renderQueueBatch();
            }
        }, {
            root: self.els.videoList,
            rootMargin: '200px'
        });
        this._queueObserver.observe(sentinel);
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
        if (this.groupedMode && v.total_predictions && v.total_predictions !== v.pending_count) {
            subtitleDiv.textContent = (v.pending_count || 0) + ' groups (' + (v.total_predictions || 0) + ' predictions)';
        } else {
            subtitleDiv.textContent = (v.pending_count || 0) + ' pending';
        }

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

        // Fetch reclassification classes for reject sheet (initially without camera, will re-fetch with camera once predictions load)
        this.fetchReclassifyClasses('');

        var url;
        if (this.classifyMode) {
            url = '/api/ai/predictions/classification-queue?limit=200';
            if (this.classifyIncludePending) url += '&include_pending=true';
            if (this.groupedMode) url += '&grouped=1';
        } else {
            url = '/api/ai/predictions/review-queue?limit=200';
            if (this.groupedMode) url += '&grouped=1';
        }
        if (videoId) {
            url += '&video_id=' + encodeURIComponent(videoId);
        }
        // Add scenario filter if active
        if (this._scenarioFilterMap[this.activeFilter]) {
            url += '&scenario=' + this._scenarioFilterMap[this.activeFilter];
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

                // Re-fetch reclassification classes with camera context
                if (self.predictions && self.predictions.length > 0 && self.predictions[0].camera_id) {
                    self.fetchReclassifyClasses(self.predictions[0].camera_id);
                }
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

        // Reset zoom state immediately when card changes
        this._resetZoomImmediate();

        var pred = this.predictions[this.currentIndex];
        this.els.cardContainer.textContent = '';

        var card = document.createElement('div');
        card.className = 'review-card';
        card.style.willChange = 'transform';

        // Image container
        var imgWrap = document.createElement('div');
        imgWrap.className = 'card-image-container';

        var clipUrl = '/api/ai/predictions/' + pred.id + '/clip';
        var thumbUrl = this.getThumbUrl(pred.thumbnail_path);
        var self = this;

        // Show thumbnail first, lazy-load video on play tap
        var img = document.createElement('img');
        img.className = 'card-image';
        img.alt = 'Prediction frame';
        img.src = thumbUrl;
        img.onerror = function() { this.style.display = 'none'; };
        imgWrap.appendChild(img);

        // Play button — in metadata strip, not overlaying the image
        var metaStrip = document.getElementById('metadata-strip');
        var oldPlayBtn = document.getElementById('metadata-play-btn');
        if (oldPlayBtn) oldPlayBtn.parentNode.removeChild(oldPlayBtn);

        if (metaStrip) {
            var playBtn = document.createElement('button');
            playBtn.className = 'metadata-play-btn';
            playBtn.id = 'metadata-play-btn';

            var makePlayIcon = function() {
                var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
                svg.setAttribute('width', '14');
                svg.setAttribute('height', '14');
                svg.setAttribute('viewBox', '0 0 24 24');
                svg.setAttribute('fill', 'currentColor');
                var poly = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
                poly.setAttribute('points', '6,3 20,12 6,21');
                svg.appendChild(poly);
                return svg;
            };

            var makeStopIcon = function() {
                var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
                svg.setAttribute('width', '14');
                svg.setAttribute('height', '14');
                svg.setAttribute('viewBox', '0 0 24 24');
                svg.setAttribute('fill', 'currentColor');
                var r1 = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
                r1.setAttribute('x', '5'); r1.setAttribute('y', '4');
                r1.setAttribute('width', '4'); r1.setAttribute('height', '16');
                var r2 = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
                r2.setAttribute('x', '15'); r2.setAttribute('y', '4');
                r2.setAttribute('width', '4'); r2.setAttribute('height', '16');
                svg.appendChild(r1);
                svg.appendChild(r2);
                return svg;
            };

            var setPlayState = function() {
                playBtn.textContent = '';
                playBtn.appendChild(makePlayIcon());
                playBtn.appendChild(document.createTextNode(' Clip'));
            };
            setPlayState();
            metaStrip.appendChild(playBtn);

            var activeVideo = null;
            playBtn.addEventListener('click', function(e) {
                e.stopPropagation();
                if (activeVideo) {
                    if (activeVideo.parentNode) activeVideo.parentNode.removeChild(activeVideo);
                    activeVideo = null;
                    img.style.display = '';
                    var bboxOvl = imgWrap.querySelector('.bbox-overlay');
                    if (bboxOvl) bboxOvl.style.display = '';
                    setPlayState();
                    return;
                }
                var video = document.createElement('video');
                video.className = 'card-image';
                video.autoplay = true;
                video.loop = true;
                video.muted = true;
                video.playsInline = true;
                video.setAttribute('playsinline', '');
                video.setAttribute('webkit-playsinline', '');
                video.preload = 'auto';
                video.src = clipUrl;
                video.onerror = function() {
                    if (video.parentNode) video.parentNode.removeChild(video);
                    activeVideo = null;
                    img.style.display = '';
                    var bboxOvl = imgWrap.querySelector('.bbox-overlay');
                    if (bboxOvl) bboxOvl.style.display = '';
                    playBtn.textContent = 'No clip';
                    playBtn.disabled = true;
                };
                imgWrap.insertBefore(video, img);
                img.style.display = 'none';
                // Hide bbox overlay while video plays
                var bboxOvl = imgWrap.querySelector('.bbox-overlay');
                if (bboxOvl) bboxOvl.style.display = 'none';
                activeVideo = video;
                playBtn.textContent = '';
                playBtn.appendChild(makeStopIcon());
                playBtn.appendChild(document.createTextNode(' Stop'));
            });
        }

        // SVG bbox overlay — use known dimensions or wait for image load
        if (pred.bbox_x != null && pred.bbox_width > 0) {
            var addBbox = function(vw, vh) {
                var bboxSvg = self.createBboxOverlay(pred, vw, vh);
                imgWrap.appendChild(bboxSvg);
                // Auto-zoom to bbox after DOM settles
                requestAnimationFrame(function() {
                    self._autoZoomToBbox(pred);
                });
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

        // Group badge
        if (pred.group_id && pred.member_count > 1) {
            var badge = document.createElement('div');
            badge.className = 'group-badge';
            badge.textContent = '\u00D7' + pred.member_count;
            card.appendChild(badge);
        }

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
        // Toggle action zone for classify mode
        this.updateActionZone();
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
        dimRect.setAttribute('data-bbox', 'dim');
        svg.appendChild(dimRect);

        // Determine color by scenario category
        var classColor = this.getScenarioColor(pred.scenario);

        // Bbox outline
        var bboxRect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
        bboxRect.setAttribute('x', String(bx));
        bboxRect.setAttribute('y', String(by));
        bboxRect.setAttribute('width', String(bw));
        bboxRect.setAttribute('height', String(bh));
        bboxRect.setAttribute('fill', 'none');
        bboxRect.setAttribute('stroke', classColor);
        bboxRect.setAttribute('stroke-width', '2.5');
        bboxRect.setAttribute('data-bbox', 'outline');
        // No vector-effect: non-scaling-stroke — we manually scale stroke in _applyZoomTransform
        svg.appendChild(bboxRect);

        return svg;
    },

    // Color palette by detection scenario
    _scenarioColors: {
        'vehicle_detection':      '#F59E0B', // amber
        'person_detection':       '#06B6D4', // cyan
        'person_identification':  '#8B5CF6', // violet
        'face_detection':         '#EC4899', // pink
        'prescreen_scan':         '#6366F1', // indigo
        'animal_detection':       '#10B981', // green
        'flag_detection':         '#6B7280', // gray
        'license_plate':          '#EF4444', // red
        'boat_registration':      '#0EA5E9', // sky blue
    },

    getScenarioColor: function(scenario) {
        if (!scenario) return '#06B6D4';
        return this._scenarioColors[scenario] || '#06B6D4';
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
            var color = this.getScenarioColor(pred.scenario);
            this.els.predClass.style.backgroundColor = color;
            this.els.predClass.style.color = '#fff';
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
        // Group member info
        if (pred.group_id && pred.member_count > 1) {
            if (this.els.predModel) {
                this.els.predModel.textContent += ' | ' + pred.member_count + ' detections';
            }
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
            if (this.classifyMode) {
                var ctags = pred.predicted_tags || {};
                if (typeof ctags === 'string') { try { ctags = JSON.parse(ctags); } catch(e) { ctags = {}; } }
                var detected = ctags.class || ctags.vehicle_type || 'vehicle';
                guidance = 'Detected as "' + detected + '". Select the correct vehicle type below.';
            }
            if (this.conflictMode && pred.conflict_id) {
                if (pred.classification_conflict) {
                    guidance = pred.member_count + ' frames with mixed classifications. Select the correct type.';
                } else {
                    guidance = pred.member_count + ' frames: ' + (pred.approved_count || 0) + ' approved, ' + (pred.rejected_count || 0) + ' rejected. Choose for all.';
                }
            }
            this.els.reviewGuidance.textContent = guidance;
        }
    },

    updateActionZone: function() {
        var actionButtons = document.querySelector('.action-buttons');
        var classifyChips = document.getElementById('classify-chips');
        var conflictChips = document.getElementById('conflict-chips');

        // Conflict mode with classification conflict - show conflict option chips
        if (this.conflictMode && this.currentIndex < this.predictions.length) {
            var pred = this.predictions[this.currentIndex];
            if (pred.conflict_id && pred.classification_conflict && pred.conflict_options && pred.conflict_options.length > 0) {
                if (actionButtons) actionButtons.style.display = 'none';
                if (classifyChips) classifyChips.style.display = 'none';
                if (conflictChips) conflictChips.parentNode.removeChild(conflictChips);
                conflictChips = this.createConflictChips(pred);
                var actionZone = document.querySelector('.action-zone');
                if (actionZone) {
                    actionZone.insertBefore(conflictChips, actionZone.querySelector('.progress-bar'));
                }
                return;
            }
        }

        // Clean up conflict chips when not needed
        if (conflictChips) { conflictChips.parentNode.removeChild(conflictChips); }

        if (this.classifyMode) {
            // Hide approve/reject/skip, show classify chips
            if (actionButtons) actionButtons.style.display = 'none';
            if (!classifyChips) {
                classifyChips = this.createClassifyChips();
                var actionZone = document.querySelector('.action-zone');
                if (actionZone) {
                    actionZone.insertBefore(classifyChips, actionZone.querySelector('.progress-bar'));
                }
            }
            classifyChips.style.display = '';

            // Highlight the matching chip for the auto-detected class
            if (classifyChips && this.currentIndex < this.predictions.length) {
                var pred = this.predictions[this.currentIndex];
                var tags = pred.predicted_tags || {};
                if (typeof tags === 'string') { try { tags = JSON.parse(tags); } catch(e) { tags = {}; } }
                var detected = (tags.class || tags.vehicle_type || '').toLowerCase();
                var allChips = classifyChips.querySelectorAll('.classify-chip[data-subtype]');
                for (var i = 0; i < allChips.length; i++) {
                    if (allChips[i].getAttribute('data-subtype') === detected) {
                        allChips[i].classList.add('classify-match');
                    } else {
                        allChips[i].classList.remove('classify-match');
                    }
                }
            }
        } else {
            // Show approve/reject/skip, hide classify chips
            if (actionButtons) actionButtons.style.display = '';
            if (classifyChips) classifyChips.style.display = 'none';
        }
    },

    createClassifyChips: function() {
        var self = this;
        var container = document.createElement('div');
        container.id = 'classify-chips';
        container.className = 'classify-chips';

        // Rule-out chips — reject false positives
        var ruleOuts = [
            { label: 'NOT a vehicle', value: 'not_vehicle' },
            { label: 'Person / hand', value: 'person_or_hand' },
            { label: 'Furniture', value: 'furniture' },
            { label: 'Background', value: 'background_object' },
        ];

        var ruleOutRow = document.createElement('div');
        ruleOutRow.className = 'classify-ruleout-row';
        for (var r = 0; r < ruleOuts.length; r++) {
            (function(ro) {
                var chip = document.createElement('button');
                chip.className = 'classify-chip classify-ruleout';
                chip.textContent = '\u2718 ' + ro.label;
                chip.addEventListener('click', function() {
                    self.commitRuleOut(ro.value);
                });
                ruleOutRow.appendChild(chip);
            })(ruleOuts[r]);
        }
        container.appendChild(ruleOutRow);

        // Divider
        var divider = document.createElement('div');
        divider.className = 'classify-divider';
        divider.textContent = 'Classify as:';
        container.appendChild(divider);

        // Vehicle subtype chips
        var subtypes = [
            'sedan', 'SUV', 'pickup truck', 'van', 'minivan',
            'semi truck', 'dump truck', 'tractor',
            'ATV', 'UTV', 'motorcycle', 'snowmobile',
            'golf cart', 'bus', 'trailer',
            'ambulance', 'fire truck', 'other'
        ];

        for (var i = 0; i < subtypes.length; i++) {
            (function(subtype) {
                var chip = document.createElement('button');
                chip.className = 'classify-chip';
                chip.setAttribute('data-subtype', subtype.toLowerCase());
                chip.textContent = subtype;
                chip.addEventListener('click', function() {
                    self.commitClassification(subtype);
                });
                container.appendChild(chip);
            })(subtypes[i]);
        }

        return container;
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
        var self = this;
        this.showScreen('history');
        this.historyStatusFilter = 'all';
        this.historyClassFilter = '';

        // Fetch classification values and populate dropdown
        fetch('/api/ai/predictions/classification-values')
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.success) {
                    var select = document.getElementById('history-classification-select');
                    if (select) {
                        select.innerHTML = '<option value="">All classifications</option>';
                        // Add vehicle subtypes
                        var subtypes = data.vehicle_subtypes || [];
                        for (var i = 0; i < subtypes.length; i++) {
                            var opt = document.createElement('option');
                            opt.value = subtypes[i];
                            opt.textContent = subtypes[i];
                            select.appendChild(opt);
                        }
                        // Add actual_class values (rejected reclassifications)
                        if (data.actual_classes && data.actual_classes.length > 0) {
                            var sep = document.createElement('option');
                            sep.disabled = true;
                            sep.textContent = '── Rejected as ──';
                            select.appendChild(sep);
                            for (var j = 0; j < data.actual_classes.length; j++) {
                                var opt2 = document.createElement('option');
                                opt2.value = 'reject:' + data.actual_classes[j].value;
                                opt2.textContent = data.actual_classes[j].value + ' (' + data.actual_classes[j].count + ')';
                                select.appendChild(opt2);
                            }
                        }
                    }
                }
            })
            .catch(function(err) {
                console.error('Failed to load classification values:', err);
            });

        this.loadHistory('all');
    },

    loadHistory: function(filter) {
        var self = this;
        this.historyStatusFilter = filter;

        var url = '/api/ai/predictions/review-history?limit=100';
        if (filter && filter !== 'all') {
            url += '&status=' + filter;
        }

        // Add classification filter
        var classSelect = document.getElementById('history-classification-select');
        if (classSelect && classSelect.value) {
            this.historyClassFilter = classSelect.value;
            if (classSelect.value.indexOf('reject:') === 0) {
                // Filter by actual_class (rejected reclassifications)
                url += '&actual_class=' + encodeURIComponent(classSelect.value.substring(7));
                if (!filter || filter === 'all') {
                    url += '&status=rejected';
                }
            } else {
                url += '&classification=' + encodeURIComponent(classSelect.value);
            }
        } else {
            this.historyClassFilter = '';
        }

        // Add video_id if one is selected
        if (this.videoId && this.videoId !== 'all') {
            url += '&video_id=' + encodeURIComponent(this.videoId);
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

        // Show classification/reclassification info
        var correctedTags = pred.corrected_tags || {};
        if (typeof correctedTags === 'string') { try { correctedTags = JSON.parse(correctedTags); } catch(e) { correctedTags = {}; } }

        // Vehicle subtype classification badge
        if (correctedTags.vehicle_subtype) {
            var subtypeBadge = document.createElement('div');
            subtypeBadge.className = 'history-classification-badge subtype';
            subtypeBadge.textContent = correctedTags.vehicle_subtype;
            info.appendChild(subtypeBadge);
        }

        // Actual class (reclassification) badge
        if (correctedTags.actual_class) {
            var actualClassBadge = document.createElement('div');
            actualClassBadge.className = 'history-classification-badge actual-class';
            actualClassBadge.textContent = '→ ' + correctedTags.actual_class;
            info.appendChild(actualClassBadge);
        }

        // Scenario type label
        if (pred.scenario) {
            var scenarioLabel = document.createElement('div');
            scenarioLabel.className = 'history-scenario-label';
            scenarioLabel.textContent = pred.scenario.replace(/_/g, ' ');
            info.appendChild(scenarioLabel);
        }

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

        // Track pointer in map
        this.zoom.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });

        // If 2 pointers, start pinch mode
        if (this.zoom.pointers.size === 2) {
            this.touch.isDragging = false;
            this.zoom.isPinching = true;
            this.zoom.initialDistance = this._getPointerDistance();
            this.zoom.initialScale = this.zoom.scale;
            // Release pointer capture from first finger so both pointers deliver events
            try {
                var container = this.els.cardContainer;
                var pts = Array.from(this.zoom.pointers.keys());
                for (var i = 0; i < pts.length; i++) {
                    if (container.hasPointerCapture && container.hasPointerCapture(pts[i])) {
                        container.releasePointerCapture(pts[i]);
                    }
                }
            } catch (ex) { /* ignore */ }
            e.preventDefault();
            return;
        }

        // Double-tap detection
        var now = Date.now();
        if (now - this.zoom.lastTapTime < 300) {
            this._handleDoubleTap(e);
            this.zoom.lastTapTime = 0;
            e.preventDefault();
            return;
        }
        this.zoom.lastTapTime = now;

        // If zoomed, single finger = pan (not swipe)
        if (this.zoom.scale > 1.05) {
            this.zoom.isPanning = true;
            this.zoom.panStartX = e.clientX;
            this.zoom.panStartY = e.clientY;
            this.zoom.panBaseX = this.zoom.translateX;
            this.zoom.panBaseY = this.zoom.translateY;
            e.preventDefault();
            return;
        }

        // Normal swipe behavior
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
        // Update pointer position in map
        if (this.zoom.pointers.has(e.pointerId)) {
            this.zoom.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
        }

        // Pinch zoom
        if (this.zoom.isPinching && this.zoom.pointers.size === 2) {
            e.preventDefault();
            var dist = this._getPointerDistance();
            var newScale = this.zoom.initialScale * (dist / this.zoom.initialDistance);
            this.zoom.scale = Math.max(1, Math.min(4, newScale));
            this._applyZoomTransform();
            return;
        }

        // Pan while zoomed — but transition to swipe if drag is clearly horizontal
        if (this.zoom.isPanning && this.zoom.scale > 1.05) {
            e.preventDefault();
            var panDX = e.clientX - this.zoom.panStartX;
            var panDY = e.clientY - this.zoom.panStartY;

            // If horizontal drag > 80px and mostly horizontal, switch to swipe
            if (Math.abs(panDX) > 80 && Math.abs(panDX) > Math.abs(panDY) * 2) {
                // Reset zoom immediately and transition to swipe mode
                this.zoom.isPanning = false;
                this._resetZoomImmediate();
                var container = this.els.cardContainer;
                var imgContainer = container ? container.querySelector('.card-image-container') : null;
                if (imgContainer) imgContainer.style.transform = '';

                // Set up swipe state
                var card = container ? container.querySelector('.review-card') : null;
                if (card) {
                    this.touch.startX = this.zoom.panStartX;
                    this.touch.startY = this.zoom.panStartY;
                    this.touch.currentX = e.clientX;
                    this.touch.currentY = e.clientY;
                    this.touch.isDragging = true;
                    this.touch.direction = 'horizontal';
                    this.touch.startTime = Date.now();
                    this.touch._hapticFired = false;
                    this.touch._hapticFiredV = false;
                    card.style.transition = 'none';
                    card.style.willChange = 'transform';
                }
                return;
            }

            this.zoom.translateX = this.zoom.panBaseX + panDX;
            this.zoom.translateY = this.zoom.panBaseY + panDY;
            this._clampPan();
            this._applyZoomTransform();
            return;
        }

        // Normal swipe handling
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
        // Remove pointer from map
        this.zoom.pointers.delete(e.pointerId);

        // End pinch mode when we drop below 2 pointers
        if (this.zoom.isPinching) {
            if (this.zoom.pointers.size < 2) {
                this.zoom.isPinching = false;
                // If zoomed back to ~1x, reset fully
                if (this.zoom.scale < 1.05) {
                    this._resetZoom();
                }
            }
            return;
        }

        // End pan mode
        if (this.zoom.isPanning) {
            this.zoom.isPanning = false;
            return;
        }

        // Normal swipe handling
        if (!this.touch.isDragging) return;
        this.touch.isDragging = false;

        var deltaX = this.touch.currentX - this.touch.startX;
        var deltaY = this.touch.currentY - this.touch.startY;

        // Reset glows
        if (this.els.glowLeft) this.els.glowLeft.style.opacity = '0';
        if (this.els.glowRight) this.els.glowRight.style.opacity = '0';
        this.scaleButton(this.els.approveButton, 1);
        this.scaleButton(this.els.rejectButton, 1);

        // Block all swipe actions for classification conflicts - must use chips
        var _swipeBlocked = false;
        if (this.conflictMode && this.currentIndex < this.predictions.length) {
            var _pred = this.predictions[this.currentIndex];
            if (_pred.conflict_id && _pred.classification_conflict) _swipeBlocked = true;
        }

        if (_swipeBlocked) {
            // Spring back - don't allow swipe actions
            var card = this.els.cardContainer.querySelector('.review-card');
            if (card) {
                card.style.transition = 'transform 0.3s cubic-bezier(0.175, 0.885, 0.32, 1.275)';
                card.style.transform = 'translateX(0) rotate(0deg)';
                var labels = card.querySelectorAll('.swipe-label');
                for (var i = 0; i < labels.length; i++) {
                    labels[i].style.opacity = '0';
                }
            }
        } else if (this.touch.direction === 'horizontal' && Math.abs(deltaX) > 80) {
            if (deltaX > 0) {
                this.animateCardExit('right');
                this.commitAction('approve');
            } else {
                this.animateCardExit('left');
                if (this.speedMode) {
                    this.commitAction('reject');
                } else {
                    this.commitAction('reject', null, true);
                    this.showRejectSheet();
                }
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
        // In conflict mode, handle all conflicts directly (no reclassify sheet)
        if (this.conflictMode && this.currentIndex < this.predictions.length) {
            var pred = this.predictions[this.currentIndex];
            if (pred.conflict_id) {
                if (pred.classification_conflict) {
                    // Classification conflicts must use chips - block reject button
                    return;
                }
                // Review conflicts: reject directly via resolve API
                this.resolveConflictAction(pred, 'reject');
                return;
            }
        }
        var self = this;
        this.animateCardExit('left');
        if (this.speedMode) {
            this.commitAction('reject');
        } else {
            this.commitAction('reject', null, true);
            setTimeout(function() {
                self.showRejectSheet();
            }, 200);
        }
    },

    commitAction: function(action, notes, skipAdvance) {
        if (this.screen !== 'review') return;
        if (this.currentIndex >= this.predictions.length) return;

        var pred = this.predictions[this.currentIndex];

        // In conflict mode: classification conflicts must use the chips, not swipe/buttons
        if (this.conflictMode && pred.conflict_id && pred.classification_conflict) {
            return;  // Block swipe/button actions - user must pick a classification chip
        }

        // Handle review conflict (non-classification) via resolve API
        if (this.conflictMode && pred.conflict_id && !pred.classification_conflict) {
            this.resolveConflictAction(pred, action);
            return;
        }

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
        var countIncrement = (pred.group_id && pred.member_count > 1) ? pred.member_count : 1;
        if (action === 'approve') {
            this.sessionStats.approved += countIncrement;
        } else if (action === 'reject') {
            this.sessionStats.rejected += countIncrement;
        } else {
            this.sessionStats.skipped += countIncrement;
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
            var syncItem = {
                prediction_id: pred.id,
                action: action,
                notes: notes || null
            };
            if (pred.group_id) {
                syncItem.group_id = pred.group_id;
            }
            this.syncQueue.push(syncItem);
            // Don't flush sync queue yet if reject sheet will open —
            // actual_class hasn't been set yet. finishRejectWithReclassify
            // will trigger the sync after the user picks a class.
            if (!skipAdvance) {
                this.scheduleSyncReviews();
            }
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

    fetchReclassifyClasses: function(cameraId) {
        var self = this;
        var url = '/api/ai/reclassification-classes';
        if (cameraId) {
            url += '?camera=' + encodeURIComponent(cameraId);
        }
        fetch(url)
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.success) {
                    var dbClasses = (data.classes || []).map(function(c) {
                        return typeof c === 'string' ? c : c.class_name;
                    });
                    // Merge knownClasses into the list, deduplicating
                    var seen = {};
                    var merged = [];
                    for (var i = 0; i < dbClasses.length; i++) {
                        var key = dbClasses[i].toLowerCase();
                        if (!seen[key]) { seen[key] = true; merged.push(dbClasses[i]); }
                    }
                    for (var i = 0; i < self.knownClasses.length; i++) {
                        var key = self.knownClasses[i].toLowerCase();
                        if (!seen[key]) { seen[key] = true; merged.push(self.knownClasses[i]); }
                    }
                    self.reclassifyClasses = merged;
                    self.cameraTopClasses = (data.camera_top || []).map(function(c) {
                        return typeof c === 'string' ? c : c.class_name;
                    });
                }
            })
            .catch(function(err) {
                console.error('Failed to load reclassification classes:', err);
            });
    },

    // --------------- Reject Sheet ---------------
    showRejectSheet: function() {
        if (!this.els.rejectSheet) return;
        var self = this;

        // Build reclassification UI
        this.els.rejectSheet.innerHTML = '<div class="sheet-grabber"></div>';

        var container = document.createElement('div');
        container.className = 'reclassify-sheet';

        // Header
        var header = document.createElement('div');
        header.className = 'reclassify-header';
        header.textContent = 'What is this actually?';
        container.appendChild(header);

        // Camera-specific frequent classes (if available)
        if (this.cameraTopClasses && this.cameraTopClasses.length > 0) {
            var frequentSection = document.createElement('div');
            frequentSection.className = 'reclassify-section';

            var frequentLabel = document.createElement('div');
            frequentLabel.className = 'reclassify-section-label';
            frequentLabel.textContent = 'Common for this camera';
            frequentSection.appendChild(frequentLabel);

            var frequentChips = document.createElement('div');
            frequentChips.className = 'reclassify-chips';
            for (var i = 0; i < Math.min(6, this.cameraTopClasses.length); i++) {
                var chip = this.createReclassifyChip(this.cameraTopClasses[i], true);
                frequentChips.appendChild(chip);
            }
            frequentSection.appendChild(frequentChips);
            container.appendChild(frequentSection);
        }

        // Search input
        var searchSection = document.createElement('div');
        searchSection.className = 'reclassify-section';
        var searchInput = document.createElement('input');
        searchInput.type = 'text';
        searchInput.className = 'reclassify-search';
        searchInput.placeholder = 'Search classes...';
        searchInput.addEventListener('input', function(e) {
            self.reclassifySearchText = e.target.value.toLowerCase();
            self.updateReclassifyResults();
        });
        searchInput.addEventListener('focus', function() {
            // When virtual keyboard opens, adjust sheet to stay visible
            var sheet = self.els.rejectSheet;
            if (sheet && window.visualViewport) {
                var onResize = function() {
                    var offsetY = window.innerHeight - window.visualViewport.height - window.visualViewport.offsetTop;
                    sheet.style.bottom = offsetY + 'px';
                    setTimeout(function() { searchInput.scrollIntoView({ block: 'center', behavior: 'smooth' }); }, 100);
                };
                window.visualViewport.addEventListener('resize', onResize);
                searchInput._vvCleanup = function() {
                    window.visualViewport.removeEventListener('resize', onResize);
                    sheet.style.bottom = '';
                };
            }
        });
        searchInput.addEventListener('blur', function() {
            if (searchInput._vvCleanup) {
                searchInput._vvCleanup();
                searchInput._vvCleanup = null;
            }
        });
        searchSection.appendChild(searchInput);
        container.appendChild(searchSection);

        // All classes container (filtered)
        var allSection = document.createElement('div');
        allSection.className = 'reclassify-section';
        allSection.id = 'reclassify-all-section';

        var allLabel = document.createElement('div');
        allLabel.className = 'reclassify-section-label';
        allLabel.textContent = 'All classes';
        allSection.appendChild(allLabel);

        var allChipsContainer = document.createElement('div');
        allChipsContainer.className = 'reclassify-all-classes';
        allChipsContainer.id = 'reclassify-all-chips';
        allSection.appendChild(allChipsContainer);

        container.appendChild(allSection);

        // Add new class button (shown when no matches)
        var addNewBtn = document.createElement('button');
        addNewBtn.className = 'reclassify-add-new';
        addNewBtn.id = 'reclassify-add-new';
        addNewBtn.style.display = 'none';
        addNewBtn.innerHTML = '<span>+</span> Add as new class';
        addNewBtn.addEventListener('mousedown', function(e) { e.preventDefault(); });
        addNewBtn.addEventListener('click', function() {
            self.finishRejectWithReclassify(self.reclassifySearchText.trim());
        });
        container.appendChild(addNewBtn);

        // Quick reject buttons (always visible)
        var quickBtns = document.createElement('div');
        quickBtns.className = 'reclassify-quick-actions';

        // Show scenario-appropriate quick actions
        var pred = this.predictions[this.currentIndex];
        var scenario = pred ? pred.scenario : '';
        var isVehicle = scenario === 'vehicle_detection' || !scenario;

        var otherBtn = document.createElement('button');
        otherBtn.className = 'reclassify-quick-btn';
        otherBtn.textContent = isVehicle ? 'Not a vehicle' : 'False positive';
        otherBtn.addEventListener('mousedown', function(e) { e.preventDefault(); });
        otherBtn.addEventListener('click', function() {
            self.finishRejectWithReclassify('other');
        });
        var unknownBtn = document.createElement('button');
        unknownBtn.className = 'reclassify-quick-btn';
        unknownBtn.textContent = 'Not sure';
        unknownBtn.addEventListener('mousedown', function(e) { e.preventDefault(); });
        unknownBtn.addEventListener('click', function() {
            self.finishRejectWithReclassify('unknown');
        });
        quickBtns.appendChild(otherBtn);
        quickBtns.appendChild(unknownBtn);
        container.appendChild(quickBtns);

        this.els.rejectSheet.appendChild(container);

        // Initial render
        this.reclassifySearchText = '';
        this.updateReclassifyResults();

        this.els.rejectSheet.classList.add('sheet-visible');
    },

    createReclassifyChip: function(className, isFrequent) {
        var self = this;
        var chip = document.createElement('button');
        chip.className = 'reclassify-chip' + (isFrequent ? ' frequent' : '');
        chip.textContent = className;
        chip.addEventListener('mousedown', function(e) { e.preventDefault(); });
        chip.addEventListener('click', function() {
            self.finishRejectWithReclassify(className);
        });
        return chip;
    },

    updateReclassifyResults: function() {
        var allChipsContainer = document.getElementById('reclassify-all-chips');
        var addNewBtn = document.getElementById('reclassify-add-new');
        if (!allChipsContainer) return;

        allChipsContainer.innerHTML = '';

        var searchText = this.reclassifySearchText.toLowerCase().trim();
        var filtered = this.reclassifyClasses;

        if (searchText) {
            filtered = this.reclassifyClasses.filter(function(cls) {
                return cls.toLowerCase().indexOf(searchText) !== -1;
            });
        }

        // Render filtered chips
        var chipsDiv = document.createElement('div');
        chipsDiv.className = 'reclassify-chips';
        for (var i = 0; i < filtered.length; i++) {
            var chip = this.createReclassifyChip(filtered[i], false);
            chipsDiv.appendChild(chip);
        }
        allChipsContainer.appendChild(chipsDiv);

        // Show "add new" button if search text exists and no exact match
        if (addNewBtn) {
            if (searchText && filtered.length === 0) {
                addNewBtn.style.display = 'flex';
                addNewBtn.innerHTML = '<span>+</span> Add "' + this.reclassifySearchText.trim() + '" as new class';
            } else {
                addNewBtn.style.display = 'none';
            }
        }
    },

    finishRejectWithReclassify: function(actualClass) {
        this.hideRejectSheet();

        // Add new class to local list so it appears in subsequent reviews
        if (actualClass && this.reclassifyClasses.indexOf(actualClass) === -1) {
            this.reclassifyClasses.unshift(actualClass);
        }

        // Handle pending conflict resolve (conflict mode reject)
        if (this._pendingConflictResolve) {
            var pcr = this._pendingConflictResolve;
            this._pendingConflictResolve = null;
            this._sendConflictResolve(pcr.pred, pcr.decision, actualClass || 'unknown');
            this.advanceToNextCard();
            return;
        }

        // Update the already-committed rejection with actual_class
        if (this.undoStack) {
            this.undoStack.actual_class = actualClass || 'unknown';
            for (var i = this.syncQueue.length - 1; i >= 0; i--) {
                if (this.syncQueue[i].prediction_id === this.undoStack.prediction.id) {
                    this.syncQueue[i].actual_class = actualClass || 'unknown';
                    break;
                }
            }
        }

        // Now that actual_class is set, flush the sync queue
        this.scheduleSyncReviews();

        this.advanceToNextCard();
    },

    hideRejectSheet: function() {
        // Dismiss keyboard by blurring any focused input
        if (document.activeElement && document.activeElement.tagName === 'INPUT') {
            document.activeElement.blur();
        }
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
        // Include correct class if "wrong class" was selected
        var correctClass = this.getSelectedCorrectClass();
        if (correctClass) {
            reasons.push('correct_class:' + correctClass);
        }
        var notes = reasons.length > 0 ? reasons.join(', ') : null;
        this.hideRejectSheet();
        // Handle pending conflict resolve
        if (this._pendingConflictResolve) {
            var pcr = this._pendingConflictResolve;
            this._pendingConflictResolve = null;
            this._sendConflictResolve(pcr.pred, pcr.decision);
            this.advanceToNextCard();
            return;
        }
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
        this.scheduleSyncReviews();
        this.advanceToNextCard();
    },

    finishReject: function(notes) {
        this.hideRejectSheet();
        // Handle pending conflict resolve
        if (this._pendingConflictResolve) {
            var pcr = this._pendingConflictResolve;
            this._pendingConflictResolve = null;
            this._sendConflictResolve(pcr.pred, pcr.decision);
            this.advanceToNextCard();
            return;
        }
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
        this.scheduleSyncReviews();
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
            if (this._pendingConflictResolve) {
                var pcr = this._pendingConflictResolve;
                this._pendingConflictResolve = null;
                this._sendConflictResolve(pcr.pred, pcr.decision);
            } else {
                this.scheduleSyncReviews();
            }
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

        // Separate group actions from individual actions
        var groupActions = [];
        var individualReviews = [];
        for (var i = 0; i < batch.length; i++) {
            if (batch[i].group_id) {
                groupActions.push(batch[i]);
            } else {
                var review = {
                    prediction_id: batch[i].prediction_id,
                    action: batch[i].action,
                    notes: batch[i].notes
                };
                if (batch[i].actual_class) {
                    review.actual_class = batch[i].actual_class;
                }
                individualReviews.push(review);
            }
        }

        var promises = [];

        // Check if any reviews have reclassifications
        var hasReclassifications = false;
        for (var r = 0; r < individualReviews.length; r++) {
            if (individualReviews[r].actual_class) { hasReclassifications = true; break; }
        }

        // Sync individual reviews
        if (individualReviews.length > 0) {
            promises.push(
                fetch('/api/ai/predictions/batch-review', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        reviews: individualReviews,
                        reviewer: 'mobile_reviewer'
                    })
                })
                .then(function(resp) { return resp.json(); })
                .then(function(data) {
                    if (data.failed && data.failed > 0) {
                        console.warn('Some reviews failed:', data.failed);
                    }
                    // Re-fetch class list so usage ranking updates
                    if (hasReclassifications) {
                        var cameraId = (self.predictions && self.predictions[self.currentIndex])
                            ? self.predictions[self.currentIndex].camera_id : '';
                        self.fetchReclassifyClasses(cameraId);
                    }
                })
            );
        }

        // Sync group actions (one request per group)
        for (var g = 0; g < groupActions.length; g++) {
            var ga = groupActions[g];
            var payload = {
                group_id: ga.group_id,
                action: ga.action,
                notes: ga.notes,
                reviewer: 'mobile_reviewer'
            };
            if (ga.actual_class) {
                payload.actual_class = ga.actual_class;
            }
            promises.push(
                fetch('/api/ai/predictions/group-review', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                })
                .then(function(resp) { return resp.json(); })
            );
        }

        if (promises.length === 0) return Promise.resolve();

        return Promise.all(promises)
            .catch(function(err) {
                console.error('Sync failed, re-queuing:', err);
                self.syncQueue = batch.concat(self.syncQueue);
                self.showSyncError();
                if (!self.syncTimer) {
                    self.syncTimer = setTimeout(function() {
                        self.syncReviews();
                    }, 10000);
                }
            });
    },

    commitClassification: function(vehicleSubtype) {
        if (this.screen !== 'review') return;
        if (this.currentIndex >= this.predictions.length) return;

        var pred = this.predictions[this.currentIndex];

        // Animate exit right
        this.animateCardExit('right');
        this.vibrate([10]);

        // Update stats (reuse approved counter for classified)
        this.sessionStats.approved += (pred.group_id && pred.member_count > 1) ? pred.member_count : 1;

        // Review log
        this.reviewLog.push({
            prediction_id: pred.id,
            action: 'classify',
            vehicle_subtype: vehicleSubtype,
            timestamp: Date.now()
        });

        // Sync queue for classify
        if (pred.group_id) {
            // Group classify - send immediately
            fetch('/api/ai/predictions/group-classify', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    group_id: pred.group_id,
                    vehicle_subtype: vehicleSubtype,
                    classifier: 'mobile_reviewer'
                })
            }).catch(function(err) { console.error('Group classify failed:', err); });
        } else {
            this.classifySyncQueue.push({
                prediction_id: pred.id,
                vehicle_subtype: vehicleSubtype
            });
        }
        this.scheduleClassifySync();

        // Undo stack
        this.undoStack = {
            prediction: pred,
            index: this.currentIndex,
            action: 'classify',
            vehicle_subtype: vehicleSubtype
        };

        this.advanceToNextCard();
    },

    commitRuleOut: function(reason) {
        if (this.screen !== 'review') return;
        if (this.currentIndex >= this.predictions.length) return;

        var pred = this.predictions[this.currentIndex];

        // Animate exit left (like a reject)
        this.animateCardExit('left');
        this.vibrate([10, 30, 10]);

        // Count as rejected
        this.sessionStats.rejected += (pred.group_id && pred.member_count > 1) ? pred.member_count : 1;

        // Review log
        this.reviewLog.push({
            prediction_id: pred.id,
            action: 'ruleout',
            reason: reason,
            timestamp: Date.now()
        });

        // Add to sync queue - this will reject the prediction
        var ruleOutItem = {
            prediction_id: pred.id,
            action: 'reject',
            notes: 'classify_ruleout: ' + reason
        };
        if (pred.group_id) {
            ruleOutItem.group_id = pred.group_id;
        }
        this.syncQueue.push(ruleOutItem);
        this.scheduleSyncReviews();

        // Undo stack
        this.undoStack = {
            prediction: pred,
            index: this.currentIndex,
            action: 'ruleout',
            reason: reason
        };

        this.advanceToNextCard();
    },

    scheduleClassifySync: function() {
        var self = this;
        if (this.classifySyncTimer) clearTimeout(this.classifySyncTimer);
        if (this.classifySyncQueue.length >= 10) {
            this.syncClassifications();
        } else {
            this.classifySyncTimer = setTimeout(function() {
                self.syncClassifications();
            }, 3000);
        }
    },

    syncClassifications: function() {
        if (this.classifySyncQueue.length === 0) return Promise.resolve();
        var batch = this.classifySyncQueue.splice(0);

        var self = this;
        return fetch('/api/ai/predictions/batch-classify', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                classifications: batch,
                classifier: 'mobile_reviewer'
            })
        })
        .then(function(resp) { return resp.json(); })
        .then(function(data) {
            if (data.failed && data.failed > 0) {
                console.warn('Some classifications failed:', data.failed);
            }
        })
        .catch(function(err) {
            console.error('Classify sync failed, re-queuing:', err);
            self.classifySyncQueue = batch.concat(self.classifySyncQueue);
        });
    },

    flushClassifySync: function() {
        if (this.classifySyncTimer) {
            clearTimeout(this.classifySyncTimer);
            this.classifySyncTimer = null;
        }
        if (this.classifySyncQueue.length > 0) {
            return this.syncClassifications();
        }
    },

    flushSync: function() {
        if (this.syncTimer) {
            clearTimeout(this.syncTimer);
            this.syncTimer = null;
        }
        if (this.classifySyncTimer) {
            clearTimeout(this.classifySyncTimer);
            this.classifySyncTimer = null;
        }
        var p1 = this.syncReviews();
        var p2 = this.syncClassifications();
        if (p1 && p2) return Promise.all([p1, p2]);
        return p1 || p2;
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

        // Conflict undo - go back to the card so user can re-resolve
        if (undoItem.conflict_id) {
            var undoCount = (undoItem.prediction && undoItem.prediction.member_count > 1) ? undoItem.prediction.member_count : 1;
            if (undoItem.action === 'conflict_classify' || undoItem.action === 'conflict_approve') {
                this.sessionStats.approved = Math.max(0, this.sessionStats.approved - undoCount);
            } else if (undoItem.action === 'conflict_reject') {
                this.sessionStats.rejected = Math.max(0, this.sessionStats.rejected - undoCount);
            }
            // Remove from review log
            for (var i = this.reviewLog.length - 1; i >= 0; i--) {
                if (this.reviewLog[i].conflict_id === undoItem.conflict_id) {
                    this.reviewLog.splice(i, 1);
                    break;
                }
            }
            // Go back to the card - re-resolving will overwrite the previous resolution
            this.currentIndex = undoItem.index;
            this.updateUndoButton();
            this.renderCurrentCard();
            return;
        }

        // Reverse stats
        var undoCount = (undoItem.prediction && undoItem.prediction.group_id && undoItem.prediction.member_count > 1) ? undoItem.prediction.member_count : 1;
        if (undoItem.action === 'approve' || undoItem.action === 'classify') {
            this.sessionStats.approved = Math.max(0, this.sessionStats.approved - undoCount);
        } else if (undoItem.action === 'reject' || undoItem.action === 'ruleout') {
            this.sessionStats.rejected = Math.max(0, this.sessionStats.rejected - undoCount);
        } else {
            this.sessionStats.skipped = Math.max(0, this.sessionStats.skipped - undoCount);
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
            if (undoItem.prediction && undoItem.prediction.group_id) {
                fetch('/api/ai/predictions/group-undo', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ group_id: undoItem.prediction.group_id })
                }).catch(function(err) {
                    console.error('Group undo failed:', err);
                });
            } else {
                fetch('/api/ai/predictions/' + predId + '/undo', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' }
                }).catch(function(err) {
                    console.error('Server undo failed:', err);
                });
            }
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
            // Prefetch video clip via link hint
            var clipUrl = '/api/ai/predictions/' + pred.id + '/clip';
            if (!this.imageCache[clipUrl]) {
                var link = document.createElement('link');
                link.rel = 'prefetch';
                link.as = 'video';
                link.href = clipUrl;
                document.head.appendChild(link);
                this.imageCache[clipUrl] = link;
            }
        }
    },

    // --------------- S2: Summary Screen ---------------
    showSummary: function() {
        this.flushSync();
        if (this.classifyMode) {
            this.flushClassifySync();
        }
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

        // Adapt labels for classify mode
        var subtitleEl = document.querySelector('.completion-subtitle');
        var approvedLabel = document.querySelector('.stat-approved .stat-label');
        var rejectedCard = document.querySelector('.stat-rejected');

        if (this.classifyMode) {
            if (subtitleEl) subtitleEl.textContent = 'vehicles classified';
            if (approvedLabel) approvedLabel.textContent = 'Classified';
            if (rejectedCard) rejectedCard.style.display = 'none';
        } else {
            if (subtitleEl) subtitleEl.textContent = 'predictions reviewed';
            if (approvedLabel) approvedLabel.textContent = 'Approved';
            if (rejectedCard) rejectedCard.style.display = '';
        }

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
        var loadFn = this.classifyMode
            ? function() { self.loadClassifyQueueSummary(); }
            : function() { self.loadQueueSummary(); };
        if (pending && pending.then) {
            pending.then(loadFn);
        } else {
            loadFn();
        }
        this.checkTrainingStatus();
    },

    checkTrainingStatus: function() {
        fetch('/api/training/jobs?limit=5')
            .then(function(resp) { return resp.json(); })
            .then(function(data) {
                if (!data.success || !data.jobs) return;
                var active = data.jobs.filter(function(j) {
                    return j.status === 'processing' || j.status === 'queued';
                });
                ReviewApp.renderTrainingIndicator(active);
            })
            .catch(function() { /* silent fail */ });
    },

    renderTrainingIndicator: function(activeJobs) {
        var existing = document.getElementById('training-indicator');
        if (activeJobs.length === 0) {
            if (existing) existing.remove();
            return;
        }
        if (!existing) {
            existing = document.createElement('div');
            existing.id = 'training-indicator';
            existing.className = 'training-indicator';
            var dot = document.createElement('span');
            dot.className = 'training-dot';
            existing.appendChild(dot);
            var topBar = document.querySelector('.queue-top-bar');
            var histBtn = document.getElementById('history-button');
            if (topBar && histBtn) {
                topBar.insertBefore(existing, histBtn);
            }
        }
        var label = activeJobs.length === 1
            ? 'Training in progress'
            : activeJobs.length + ' training jobs active';
        existing.title = label;
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
    },

    // --------------- Zoom helper methods ---------------
    _getPointerDistance: function() {
        var pts = Array.from(this.zoom.pointers.values());
        if (pts.length < 2) return 0;
        var dx = pts[1].x - pts[0].x;
        var dy = pts[1].y - pts[0].y;
        return Math.sqrt(dx * dx + dy * dy);
    },

    _autoZoomToBbox: function(pred) {
        // Need bbox data
        if (pred.bbox_x == null || !pred.bbox_width) return;

        var container = this.els.cardContainer;
        if (!container) return;
        var imgContainer = container.querySelector('.card-image-container');
        if (!imgContainer) return;

        var containerW = imgContainer.offsetWidth;
        var containerH = imgContainer.offsetHeight;
        if (!containerW || !containerH) return;

        // Get video/image dimensions from the prediction or the media element
        var videoW = pred.video_width;
        var videoH = pred.video_height;
        if (!videoW || !videoH) {
            var vid = imgContainer.querySelector('video');
            if (vid && vid.videoWidth) {
                videoW = vid.videoWidth;
                videoH = vid.videoHeight;
            } else {
                var img = imgContainer.querySelector('img');
                if (img && img.naturalWidth) {
                    videoW = img.naturalWidth;
                    videoH = img.naturalHeight;
                } else {
                    return; // can't calculate without dimensions
                }
            }
        }

        // Bbox center as fraction of image
        var fracX = (pred.bbox_x + pred.bbox_width / 2) / videoW;
        var fracY = (pred.bbox_y + pred.bbox_height / 2) / videoH;

        // Calculate scale to make bbox fill ~50% of visible area
        var bboxFracW = pred.bbox_width / videoW;
        var bboxFracH = pred.bbox_height / videoH;
        var maxFrac = Math.max(bboxFracW, bboxFracH);
        var targetScale = 0.5 / maxFrac;

        // Clamp scale: minimum 1.5x (subtle zoom), maximum 3.5x
        targetScale = Math.max(1.5, Math.min(3.5, targetScale));

        // If bbox is already large enough (>40% of image), don't auto-zoom
        if (maxFrac > 0.4) return;

        // Account for object-fit: contain — the image may not fill the container
        var imgAspect = videoW / videoH;
        var containerAspect = containerW / containerH;
        var renderedW, renderedH;
        if (imgAspect > containerAspect) {
            // Image is wider than container — width fills, height has letterbox
            renderedW = containerW;
            renderedH = containerW / imgAspect;
        } else {
            // Image is taller — height fills, width has pillarbox
            renderedH = containerH;
            renderedW = containerH * imgAspect;
        }

        // Calculate translate to center the bbox using rendered image dimensions
        var targetX = renderedW * (fracX - 0.5);
        var targetY = renderedH * (fracY - 0.5);

        // To center target at origin: TX = -scale * targetX, TY = -scale * targetY
        var tx = -targetScale * targetX;
        var ty = -targetScale * targetY;

        // Review card - use zoom state + clamp
        this.zoom.scale = targetScale;
        this.zoom.translateX = tx;
        this.zoom.translateY = ty;
        this._clampPan();

        imgContainer.style.transition = 'transform 0.5s ease-out';
        this._applyZoomTransform();

        var self = this;
        setTimeout(function() {
            imgContainer.style.transition = '';
        }, 500);
    },

    _applyZoomTransform: function() {
        var container = this.els.cardContainer;
        if (!container) return;
        var imgContainer = container.querySelector('.card-image-container');
        if (!imgContainer) return;
        imgContainer.style.transform = 'scale(' + this.zoom.scale + ') translate(' +
            (this.zoom.translateX / this.zoom.scale) + 'px, ' +
            (this.zoom.translateY / this.zoom.scale) + 'px)';
        imgContainer.style.transformOrigin = 'center center';

        // Scale down bbox strokes/labels inversely so they don't cover content
        var svg = imgContainer.querySelector('.bbox-overlay');
        if (svg) {
            var invScale = 1 / this.zoom.scale;
            // Scale bbox outline stroke using CSS style (not attribute) to force repaint
            var bboxRect = svg.querySelector('[data-bbox="outline"]');
            if (bboxRect) {
                bboxRect.style.strokeWidth = (2.5 * invScale);
            }
            // Scale dim overlay opacity
            var dimRect = svg.querySelector('[data-bbox="dim"]');
            if (dimRect) {
                dimRect.style.fill = 'rgba(0,0,0,' + (0.4 * invScale) + ')';
            }
            // Scale label text
            var labels = svg.querySelectorAll('text');
            for (var i = 0; i < labels.length; i++) {
                var origSize = parseFloat(labels[i].getAttribute('data-orig-size') || labels[i].getAttribute('font-size'));
                if (!labels[i].getAttribute('data-orig-size')) {
                    labels[i].setAttribute('data-orig-size', String(origSize));
                }
                labels[i].style.fontSize = (origSize * invScale) + 'px';
            }
            // Scale label backgrounds
            var labelBgs = svg.querySelectorAll('[data-bbox="label-bg"]');
            for (var j = 0; j < labelBgs.length; j++) {
                var origH = parseFloat(labelBgs[j].getAttribute('data-orig-h') || labelBgs[j].getAttribute('height'));
                var origW = parseFloat(labelBgs[j].getAttribute('data-orig-w') || labelBgs[j].getAttribute('width'));
                if (!labelBgs[j].getAttribute('data-orig-h')) {
                    labelBgs[j].setAttribute('data-orig-h', String(origH));
                    labelBgs[j].setAttribute('data-orig-w', String(origW));
                }
                labelBgs[j].setAttribute('height', String(origH * invScale));
                labelBgs[j].setAttribute('width', String(origW * invScale));
            }
        }
    },

    _clampPan: function() {
        // Prevent panning too far outside the image bounds using actual container size
        var container = this.els.cardContainer;
        var w = 300, h = 225; // fallback
        if (container) {
            var imgC = container.querySelector('.card-image-container');
            if (imgC && imgC.offsetWidth) {
                w = imgC.offsetWidth;
                h = imgC.offsetHeight;
            }
        }
        var maxPanX = (this.zoom.scale - 1) * w * 0.5;
        var maxPanY = (this.zoom.scale - 1) * h * 0.5;
        this.zoom.translateX = Math.max(-maxPanX, Math.min(maxPanX, this.zoom.translateX));
        this.zoom.translateY = Math.max(-maxPanY, Math.min(maxPanY, this.zoom.translateY));
    },

    _resetZoom: function() {
        this.zoom.scale = 1;
        this.zoom.translateX = 0;
        this.zoom.translateY = 0;
        this.zoom.isPinching = false;
        this.zoom.isPanning = false;
        var container = this.els.cardContainer;
        if (container) {
            var imgContainer = container.querySelector('.card-image-container');
            if (imgContainer) {
                imgContainer.style.transition = 'transform 0.3s ease';
                imgContainer.style.transform = '';
                // Reset bbox overlay attributes to defaults
                var svg = imgContainer.querySelector('.bbox-overlay');
                if (svg) {
                    var bboxRect = svg.querySelector('[data-bbox="outline"]');
                    if (bboxRect) {
                        bboxRect.style.strokeWidth = '';
                    }
                    var dimRect = svg.querySelector('[data-bbox="dim"]');
                    if (dimRect) {
                        dimRect.style.fill = '';
                    }
                    var labels = svg.querySelectorAll('text');
                    for (var k = 0; k < labels.length; k++) {
                        labels[k].style.fontSize = '';
                    }
                    var labelBgs = svg.querySelectorAll('[data-bbox="label-bg"]');
                    for (var k = 0; k < labelBgs.length; k++) {
                        var origH = labelBgs[k].getAttribute('data-orig-h');
                        var origW = labelBgs[k].getAttribute('data-orig-w');
                        if (origH) labelBgs[k].setAttribute('height', origH);
                        if (origW) labelBgs[k].setAttribute('width', origW);
                    }
                }
                setTimeout(function() {
                    imgContainer.style.transition = '';
                }, 300);
            }
        }
    },

    copyDebugContext: function() {
        var pred = this.predictions[this.currentIndex];
        if (!pred) {
            this._showToast('No prediction loaded');
            return;
        }
        var tags = pred.predicted_tags || {};
        var corrected = pred.corrected_tags || {};
        var lines = [
            'Prediction ID: ' + pred.id,
            'Video ID: ' + pred.video_id,
            'Camera: ' + (pred.camera_id || 'N/A'),
            'Scenario: ' + (pred.scenario || 'N/A'),
            'Model: ' + (pred.model_name || '?') + ' v' + (pred.model_version || '?'),
            'Confidence: ' + (pred.confidence || 0),
            'Class: ' + (tags.class || tags.vehicle_type || 'N/A'),
            'BBox: ' + pred.bbox_x + ',' + pred.bbox_y + ' ' + pred.bbox_width + 'x' + pred.bbox_height,
            'Timestamp: ' + (pred.timestamp || 0),
            'Review: ' + (pred.review_status || 'N/A')
        ];
        if (pred.group_id) lines.push('Group ID: ' + pred.group_id + ' (' + (pred.member_count || 1) + ' members)');
        if (pred.conflict_id) lines.push('Conflict ID: ' + pred.conflict_id);
        if (corrected.vehicle_subtype) lines.push('Classified as: ' + corrected.vehicle_subtype);
        if (corrected.actual_class) lines.push('Reclassified to: ' + corrected.actual_class);
        if (pred.thumbnail_path) lines.push('Thumbnail: ' + pred.thumbnail_path);
        var text = lines.join('\n');
        var self = this;
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(function() {
                self._showToast('Debug info copied');
            }).catch(function() {
                self._fallbackCopy(text);
            });
        } else {
            self._fallbackCopy(text);
        }
    },

    _fallbackCopy: function(text) {
        var ta = document.createElement('textarea');
        ta.value = text;
        ta.style.cssText = 'position:fixed;left:-9999px';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
        this._showToast('Debug info copied');
    },

    _showToast: function(msg) {
        var existing = document.querySelector('.debug-toast');
        if (existing) existing.remove();
        var toast = document.createElement('div');
        toast.className = 'debug-toast';
        toast.textContent = msg;
        toast.style.cssText = 'position:fixed;bottom:100px;left:50%;transform:translateX(-50%);background:#333;color:#fff;padding:8px 16px;border-radius:20px;font-size:14px;z-index:10000;opacity:0;transition:opacity 0.3s;';
        document.body.appendChild(toast);
        requestAnimationFrame(function() { toast.style.opacity = '1'; });
        setTimeout(function() {
            toast.style.opacity = '0';
            setTimeout(function() { toast.remove(); }, 300);
        }, 2000);
    },

    _resetZoomImmediate: function() {
        this.zoom.scale = 1;
        this.zoom.translateX = 0;
        this.zoom.translateY = 0;
        this.zoom.isPinching = false;
        this.zoom.isPanning = false;
        this.zoom.pointers.clear();
        var container = this.els.cardContainer;
        if (container) {
            var imgContainer = container.querySelector('.card-image-container');
            if (imgContainer) imgContainer.style.transition = '';
        }
    },

    _handleDoubleTap: function(e) {
        if (this.zoom.scale > 1.05) {
            this._resetZoom();
        } else {
            this.zoom.scale = 2;
            // Center zoom on tap point relative to card
            var container = this.els.cardContainer;
            var rect = container.getBoundingClientRect();
            var tapX = e.clientX - rect.left - rect.width / 2;
            var tapY = e.clientY - rect.top - rect.height / 2;
            this.zoom.translateX = -tapX;
            this.zoom.translateY = -tapY;
            this._clampPan();
            this._applyZoomTransform();
        }
    }
    ,

    // --------------- Wrong Class Reclassification ---------------
    toggleClassPicker: function(show) {
        var container = document.getElementById('class-picker-container');
        if (!container) {
            this.buildClassPicker();
            container = document.getElementById('class-picker-container');
        }
        if (container) {
            container.classList.toggle('hidden', !show);
        }
        // Hide "other" input when showing class picker
        if (show && this.els.otherInputContainer) {
            this.els.otherInputContainer.classList.add('hidden');
            var otherChip = document.querySelector('.reason-chip[data-reason="other"]');
            if (otherChip) otherChip.classList.remove('chip-selected');
        }
    },

    buildClassPicker: function() {
        var sheet = this.els.rejectSheet;
        if (!sheet) return;

        var container = document.createElement('div');
        container.id = 'class-picker-container';
        container.className = 'class-picker-container';

        var label = document.createElement('div');
        label.className = 'class-picker-label';
        label.textContent = 'What is the correct class?';
        container.appendChild(label);

        var grid = document.createElement('div');
        grid.className = 'class-picker-grid';

        var self = this;
        for (var i = 0; i < this.knownClasses.length; i++) {
            var cls = this.knownClasses[i];
            (function(className) {
                var btn = document.createElement('button');
                btn.className = 'class-pick-chip';
                btn.textContent = className;
                btn.addEventListener('click', function() {
                    // Deselect other class chips
                    var siblings = grid.querySelectorAll('.class-pick-chip');
                    for (var j = 0; j < siblings.length; j++) {
                        siblings[j].classList.remove('chip-selected');
                    }
                    btn.classList.add('chip-selected');
                    // Hide "other class" input
                    var otherClassInput = document.getElementById('other-class-input-container');
                    if (otherClassInput) otherClassInput.classList.add('hidden');
                });
                grid.appendChild(btn);
            })(cls);
        }

        // "Other" option with text input
        var otherBtn = document.createElement('button');
        otherBtn.className = 'class-pick-chip class-pick-other';
        otherBtn.textContent = 'Other...';
        otherBtn.addEventListener('click', function() {
            var siblings = grid.querySelectorAll('.class-pick-chip');
            for (var j = 0; j < siblings.length; j++) {
                siblings[j].classList.remove('chip-selected');
            }
            otherBtn.classList.add('chip-selected');
            var otherClassInput = document.getElementById('other-class-input-container');
            if (otherClassInput) {
                otherClassInput.classList.remove('hidden');
                var input = otherClassInput.querySelector('input');
                if (input) input.focus();
            }
        });
        grid.appendChild(otherBtn);

        container.appendChild(grid);

        // Other class text input
        var otherInputDiv = document.createElement('div');
        otherInputDiv.id = 'other-class-input-container';
        otherInputDiv.className = 'other-input-container hidden';
        var otherInput = document.createElement('input');
        otherInput.type = 'text';
        otherInput.className = 'other-input';
        otherInput.id = 'other-class-input';
        otherInput.placeholder = 'Type the correct class...';
        otherInput.maxLength = 50;
        otherInputDiv.appendChild(otherInput);
        container.appendChild(otherInputDiv);

        // Insert before the sheet-actions div
        var actionsDiv = sheet.querySelector('.sheet-actions');
        if (actionsDiv) {
            sheet.insertBefore(container, actionsDiv);
        } else {
            sheet.appendChild(container);
        }
    },

    getSelectedCorrectClass: function() {
        var container = document.getElementById('class-picker-container');
        if (!container || container.classList.contains('hidden')) return null;
        var selected = container.querySelector('.class-pick-chip.chip-selected');
        if (!selected) return null;
        if (selected.classList.contains('class-pick-other')) {
            var input = document.getElementById('other-class-input');
            return input ? input.value.trim() || null : null;
        }
        return selected.textContent;
    },

    // --------------- Conflict Resolution ---------------
    loadConflicts: function() {
        var self = this;
        fetch('/api/ai/tracks/conflicts')
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (!data.success) return;
                self.conflicts = data.conflicts || [];
                self.renderConflictList();
                // Update badge
                var badge = document.getElementById('conflict-count-badge');
                if (badge) badge.textContent = self.conflicts.length > 0 ? self.conflicts.length : '';
            })
            .catch(function(err) { console.error('Failed to load conflicts:', err); });
    },

    renderConflictList: function() {
        var list = this.els.videoList;
        if (!list) return;

        // Hide the All Videos card when in conflict mode
        var allCard = document.getElementById('all-videos-card');
        if (allCard) allCard.style.display = 'none';

        // Remove existing video cards
        var existing = list.querySelectorAll('.video-card:not(#all-videos-card)');
        for (var i = 0; i < existing.length; i++) {
            existing[i].remove();
        }
        // Remove old conflict list
        var oldList = list.querySelector('.conflict-list');
        if (oldList) oldList.remove();

        if (this.conflicts.length === 0) {
            var empty = document.createElement('div');
            empty.style.cssText = 'text-align:center;padding:40px 20px;color:#666;';
            empty.textContent = 'No conflicts to resolve';
            empty.className = 'conflict-list';
            list.appendChild(empty);
            // Update summary text
            var summaryEl = document.getElementById('queue-summary');
            if (summaryEl) summaryEl.textContent = 'No conflicts to resolve';
            return;
        }

        var container = document.createElement('div');
        container.className = 'conflict-list';

        // "Review All" card at top
        var self = this;
        var allCard = document.createElement('div');
        allCard.className = 'conflict-card';
        allCard.style.borderColor = '#0D9488';
        var allInfo = document.createElement('div');
        allInfo.className = 'conflict-card-info';
        var allTitle = document.createElement('div');
        allTitle.className = 'conflict-card-title';
        allTitle.textContent = 'Review All Conflicts';
        allTitle.style.color = '#2DD4BF';
        var allMeta = document.createElement('div');
        allMeta.className = 'conflict-card-meta';
        allMeta.textContent = this.conflicts.length + ' conflicts to resolve';
        allInfo.appendChild(allTitle);
        allInfo.appendChild(allMeta);
        allCard.appendChild(allInfo);
        allCard.addEventListener('click', function() { self.startConflictReview(0); });
        container.appendChild(allCard);

        for (var i = 0; i < this.conflicts.length; i++) {
            var c = this.conflicts[i];
            var card = document.createElement('div');
            card.className = 'conflict-card';
            card.setAttribute('data-conflict-index', i);

            var isClassConflict = c.classification_conflict;
            var typeLabel = isClassConflict ? 'Classification' : 'Review';
            var typeCls = isClassConflict ? 'classification' : 'review';

            // Build thumbnail URL
            var thumbSrc = c.thumbnail_path || '';
            if (thumbSrc && !thumbSrc.startsWith('http')) {
                thumbSrc = '/thumbnails/' + thumbSrc.split('/').pop();
            }

            var statsHtml = '';
            if (c.approved_count > 0) statsHtml += '<span class="conflict-stat approved">' + c.approved_count + ' approved</span>';
            if (c.rejected_count > 0) statsHtml += '<span class="conflict-stat rejected">' + c.rejected_count + ' rejected</span>';
            if (c.pending_count > 0) statsHtml += '<span class="conflict-stat pending">' + c.pending_count + ' pending</span>';

            var thumbImg = document.createElement('img');
            thumbImg.className = 'conflict-card-thumb';
            thumbImg.src = thumbSrc;
            thumbImg.loading = 'lazy';
            thumbImg.onerror = function() { this.style.display = 'none'; };

            var infoDiv = document.createElement('div');
            infoDiv.className = 'conflict-card-info';

            var titleDiv = document.createElement('div');
            titleDiv.className = 'conflict-card-title';
            titleDiv.textContent = c.camera_id || 'Unknown';

            var metaDiv = document.createElement('div');
            metaDiv.className = 'conflict-card-meta';
            metaDiv.textContent = c.member_count + ' frames \u00B7 ' + (c.scenario || '').replace(/_/g, ' ');

            var statsDiv = document.createElement('div');
            statsDiv.className = 'conflict-card-stats';
            if (statsHtml) statsDiv.innerHTML = statsHtml;

            infoDiv.appendChild(titleDiv);
            infoDiv.appendChild(metaDiv);
            infoDiv.appendChild(statsDiv);

            var badge = document.createElement('span');
            badge.className = 'conflict-type-badge ' + typeCls;
            badge.textContent = typeLabel;

            card.appendChild(thumbImg);
            card.appendChild(infoDiv);
            card.appendChild(badge);

            var self = this;
            (function(idx) {
                card.addEventListener('click', function() { self.startConflictReview(idx); });
            })(i);

            container.appendChild(card);
        }

        list.appendChild(container);

        // Update summary text
        var summaryEl = document.getElementById('queue-summary');
        if (summaryEl) {
            summaryEl.textContent = this.conflicts.length + ' conflict' + (this.conflicts.length !== 1 ? 's' : '') + ' to resolve';
        }
    },

    startConflictReview: function(startIndex) {
        // Map conflicts to prediction-like objects for the card renderer
        this.predictions = [];
        for (var i = 0; i < this.conflicts.length; i++) {
            var c = this.conflicts[i];
            this.predictions.push({
                id: c.representative_prediction_id || c.id,
                conflict_id: c.id,
                video_id: c.video_id,
                bbox_x: c.bbox_x,
                bbox_y: c.bbox_y,
                bbox_width: c.bbox_width || c.avg_bbox_width,
                bbox_height: c.bbox_height || c.avg_bbox_height,
                confidence: c.confidence || c.avg_confidence,
                timestamp: c.timestamp,
                predicted_tags: c.predicted_tags,
                scenario: c.scenario,
                camera_id: c.camera_id,
                video_title: c.video_title,
                video_width: c.video_width,
                video_height: c.video_height,
                thumbnail_path: c.thumbnail_path,
                member_count: c.member_count,
                approved_count: c.approved_count,
                rejected_count: c.rejected_count,
                pending_count: c.pending_count,
                classification_conflict: c.classification_conflict,
                conflict_options: (c.anchor_classification && c.anchor_classification.conflict_options) || [],
                review_status: 'conflict'
            });
        }
        this.currentIndex = startIndex || 0;
        this.sessionStats = { approved: 0, rejected: 0, skipped: 0 };
        this.reviewLog = [];
        this.syncQueue = [];
        this.classifySyncQueue = [];
        this.undoStack = null;
        this.showScreen('review');
        if (this.els.reviewVideoTitle) {
            this.els.reviewVideoTitle.textContent = 'Resolve Conflicts';
        }
        this.renderCurrentCard();
        this.updateProgress();
        this.updateDots();
        this.updateUndoButton();
    },

    createConflictChips: function(pred) {
        var self = this;
        var container = document.createElement('div');
        container.id = 'conflict-chips';
        container.className = 'classify-chips';

        // Header
        var header = document.createElement('div');
        header.className = 'classify-divider';
        header.textContent = 'Choose correct type (' + pred.member_count + ' frames):';
        container.appendChild(header);

        // Conflict option chips
        var options = pred.conflict_options;
        for (var i = 0; i < options.length; i++) {
            (function(option) {
                var chip = document.createElement('button');
                chip.className = 'classify-chip';
                chip.setAttribute('data-subtype', option.toLowerCase());
                chip.textContent = option;
                chip.addEventListener('click', function() {
                    self.resolveConflictAsClassification(pred, option);
                });
                container.appendChild(chip);
            })(options[i]);
        }

        // Reject all button
        var rejectBtn = document.createElement('button');
        rejectBtn.className = 'classify-chip classify-ruleout';
        rejectBtn.textContent = '\u2718 Reject all ' + pred.member_count + ' frames';
        rejectBtn.style.marginTop = '8px';
        rejectBtn.addEventListener('click', function() {
            self.resolveConflictAction(pred, 'reject');
        });
        container.appendChild(rejectBtn);

        return container;
    },

    resolveConflictAsClassification: function(pred, vehicleSubtype) {
        if (this.screen !== 'review') return;
        var self = this;

        this.animateCardExit('right');
        this.vibrate([10]);
        this.sessionStats.approved += pred.member_count || 1;

        this.reviewLog.push({
            conflict_id: pred.conflict_id,
            action: 'classify',
            vehicle_subtype: vehicleSubtype,
            timestamp: Date.now()
        });

        fetch('/api/ai/tracks/' + pred.conflict_id + '/resolve', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Auth-Role': 'user' },
            body: JSON.stringify({
                decision: 'approve',
                vehicle_subtype: vehicleSubtype,
                reviewer: 'mobile_reviewer'
            })
        })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.success) {
                self.showConflictToast('Classified ' + (pred.member_count || 1) + ' frames as ' + vehicleSubtype);
            }
        })
        .catch(function(err) { console.error('Conflict resolve failed:', err); });

        // Update conflict count badge
        var badge = document.getElementById('conflict-count-badge');
        var remaining = this.predictions.length - this.currentIndex - 1;
        if (badge) badge.textContent = remaining > 0 ? remaining : '';

        this.undoStack = {
            prediction: pred,
            index: this.currentIndex,
            action: 'conflict_classify',
            vehicle_subtype: vehicleSubtype,
            conflict_id: pred.conflict_id
        };
        this.updateUndoButton();
        this.advanceToNextCard();
    },

    resolveConflictAction: function(pred, decision) {
        if (this.screen !== 'review') return;
        var self = this;

        this.animateCardExit(decision === 'approve' ? 'right' : 'left');
        this.vibrate(decision === 'approve' ? [10] : [10, 30, 10]);

        if (decision === 'approve') {
            this.sessionStats.approved += pred.member_count || 1;
        } else {
            this.sessionStats.rejected += pred.member_count || 1;
        }

        this.reviewLog.push({
            conflict_id: pred.conflict_id,
            action: decision,
            timestamp: Date.now()
        });

        var badge = document.getElementById('conflict-count-badge');
        var remaining = this.predictions.length - this.currentIndex - 1;
        if (badge) badge.textContent = remaining > 0 ? remaining : '';

        this.undoStack = {
            prediction: pred,
            index: this.currentIndex,
            action: decision === 'approve' ? 'conflict_approve' : 'conflict_reject',
            conflict_id: pred.conflict_id
        };
        this.updateUndoButton();

        // For rejections with speed mode off, show reclassify sheet
        // and defer the API call until user picks a class
        if (decision === 'reject' && !this.speedMode) {
            this._pendingConflictResolve = {
                pred: pred,
                decision: decision
            };
            setTimeout(function() {
                self.showRejectSheet();
            }, 200);
            return;
        }

        // Approve or speed-mode reject: resolve immediately
        this._sendConflictResolve(pred, decision);
        this.advanceToNextCard();
    },

    _sendConflictResolve: function(pred, decision, actualClass) {
        var self = this;
        var payload = {
            decision: decision,
            reviewer: 'mobile_reviewer'
        };
        if (actualClass) {
            payload.actual_class = actualClass;
        }
        fetch('/api/ai/tracks/' + pred.conflict_id + '/resolve', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Auth-Role': 'user' },
            body: JSON.stringify(payload)
        })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.success) {
                var label = decision === 'approve' ? 'Approved' : 'Rejected';
                self.showConflictToast(label + ' ' + (pred.member_count || 1) + ' frames');
            }
            // Re-fetch class list if reclassified
            if (actualClass) {
                var cameraId = (self.predictions && self.predictions[self.currentIndex])
                    ? self.predictions[self.currentIndex].camera_id : '';
                self.fetchReclassifyClasses(cameraId);
            }
        })
        .catch(function(err) { console.error('Conflict resolve failed:', err); });
    },

    showConflictToast: function(message) {
        var existing = document.querySelector('.conflict-toast');
        if (existing) existing.parentNode.removeChild(existing);

        var toast = document.createElement('div');
        toast.className = 'conflict-toast';
        toast.textContent = message;
        document.body.appendChild(toast);

        requestAnimationFrame(function() { toast.classList.add('visible'); });
        setTimeout(function() {
            toast.classList.remove('visible');
            setTimeout(function() { if (toast.parentNode) toast.parentNode.removeChild(toast); }, 300);
        }, 2500);
    }
};

// Inject video indicator and zoom styles
(function() {
    var style = document.createElement('style');
    style.textContent = '' +
        '.media-badge { position: absolute; top: 8px; left: 8px; padding: 3px 8px; border-radius: 4px; font-size: 10px; font-weight: 700; letter-spacing: 0.5px; z-index: 5; pointer-events: none; }' +
        '.media-badge-video { background: rgba(220, 53, 69, 0.85); color: white; }' +
        '.media-badge-photo { background: rgba(108, 117, 125, 0.7); color: white; }' +
        '.metadata-play-btn { background: rgba(255,255,255,0.15); border: 1px solid rgba(255,255,255,0.3); color: #fff; font-size: 11px; padding: 2px 8px; border-radius: 10px; cursor: pointer; display: inline-flex; align-items: center; gap: 3px; margin-left: auto; vertical-align: middle; }' +
        '.metadata-play-btn:disabled { opacity: 0.4; }' +
        '.card-image-container { overflow: hidden; position: relative; }';
    document.head.appendChild(style);
})();

// Boot on DOMContentLoaded
document.addEventListener('DOMContentLoaded', function() {
    ReviewApp.init();
});
