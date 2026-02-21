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
    sessionStats: { approved: 0, rejected: 0, skipped: 0, reclassified: 0 },
    speedMode: false,
    classifyIncludePending: false,
    skippedIds: new Set(),
    imageCache: {},
    activeFilter: 'all',
    _scenarioFilterMap: { 'vehicles': 'vehicle_detection', 'people': 'person_detection,face_detection,person_identification', 'plates': 'license_plate', 'boat_reg': 'boat_registration', 'other': '_other' },
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
    crossCameraMode: false,
    clusterMode: false,
    confidenceFilter: 'all',
    cameraFilter: 'all',
    conflicts: [],
    currentConflictIndex: -1,
    selectedClassification: null,

    // Known detection classes for reclassification
    knownClasses: [
        'sedan', 'pickup truck', 'SUV', 'minivan', 'van',
        'tractor', 'ATV', 'UTV', 'snowmobile', 'golf cart', 'motorcycle', 'trailer',
        'bus', 'semi truck', 'dump truck', 'multiple_vehicles',
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
        this.loadCameraFilter();

        // Always load badge counts independently on page load
        this.loadFilterBadges();

        var params = new URLSearchParams(window.location.search);
        var vid = params.get('video_id');
        var filterParam = params.get('filter');
        if (vid) {
            this.startReview(vid, '');
        } else {
            // Auto-select chip from URL param
            if (filterParam) {
                this.activeFilter = filterParam;
                this.classifyMode = (filterParam === 'classify');
                this.conflictMode = (filterParam === 'conflicts');
                this.crossCameraMode = (filterParam === 'cross_camera');
                this.clusterMode = (filterParam === 'clusters');
                // Activate the matching chip
                var chips = document.querySelectorAll('.filter-chips .filter-chip');
                chips.forEach(function(c) {
                    c.classList.toggle('active', c.getAttribute('data-filter') === filterParam);
                });
            }
            // If hash contains a direct link (e.g. #cross_camera:4075), skip queue and go to review
            var hashDirect = window.location.hash;
            if (hashDirect && hashDirect.length > 2) {
                var hParts = hashDirect.substring(1).split(':');
                if (hParts.length === 2 && !isNaN(parseInt(hParts[1], 10))) {
                    this.startReview(null, '');
                    return;
                }
            }
            this.showScreen('queue');
            if (this.classifyMode) {
                this.loadClassifyQueueSummary();
            } else if (this.crossCameraMode) {
                this.loadCrossCameraQueueSummary();
            } else if (this.clusterMode) {
                this.loadClusterQueueSummary();
            } else if (this.conflictMode) {
                this.loadConflicts();
            } else {
                this.loadQueueSummary();
            }
        }
    },

    _confidenceParams: function() {
        if (this.confidenceFilter === 'certain') return 'min_confidence=0.99';
        if (this.confidenceFilter === 'very_high') return 'min_confidence=0.90';
        if (this.confidenceFilter === 'high') return 'min_confidence=0.80';
        if (this.confidenceFilter === 'moderate') return 'min_confidence=0.60';
        if (this.confidenceFilter === 'low') return 'max_confidence=0.60';
        return '';
    },

    _cameraParams: function() {
        if (this.cameraFilter && this.cameraFilter !== 'all') return 'camera_id=' + encodeURIComponent(this.cameraFilter);
        return '';
    },

    loadCameraFilter: function() {
        var self = this;
        fetch('/api/ai/predictions/cameras')
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (!data.success || !data.cameras) return;
                var sel = document.getElementById('camera-filter');
                if (!sel) return;
                // Group by location
                var groups = {};
                data.cameras.forEach(function(cam) {
                    var loc = cam.location_name || 'Unknown';
                    if (!groups[loc]) groups[loc] = [];
                    groups[loc].push(cam);
                });
                Object.keys(groups).sort().forEach(function(loc) {
                    var optgroup = document.createElement('optgroup');
                    optgroup.label = loc;
                    groups[loc].forEach(function(cam) {
                        var opt = document.createElement('option');
                        opt.value = cam.camera_id;
                        opt.textContent = cam.camera_name + ' (' + cam.prediction_count + ')';
                        optgroup.appendChild(opt);
                    });
                    sel.appendChild(optgroup);
                });
            })
            .catch(function(err) { console.error('Failed to load cameras:', err); });
    },

    loadFilterBadges: function() {
        var self = this;
        var fcUrl = '/api/ai/predictions/review-queue/filter-counts';
        var cp = self._confidenceParams();
        if (cp) fcUrl += '?' + cp;
        var cam = self._cameraParams();
        if (cam) fcUrl += (fcUrl.indexOf('?') >= 0 ? '&' : '?') + cam;
        fetch(fcUrl)
            .then(function(r) { return r.json(); })
            .then(function(fcData) {
                if (fcData.success && fcData.counts) {
                    var c = fcData.counts;
                    var map = {
                        'all': c.total || 0,
                        'vehicles': c.vehicles || 0,
                        'people': c.people || 0,
                        'classify': c.classify || 0,
                        'conflicts': c.conflicts || 0,
                        'cross_camera': c.cross_camera || 0,
                        'clusters': c.clusters || 0,
                        'plates': c.plates || 0,
                        'boat_reg': c.boat_reg || 0,
                        'other': c.other || 0,
                    };
                    var badges = document.querySelectorAll('.filter-count-badge');
                    for (var i = 0; i < badges.length; i++) {
                        var key = badges[i].getAttribute('data-count-filter');
                        if (key && map[key] !== undefined) {
                            badges[i].textContent = map[key] > 0 ? map[key] : '';
                        }
                    }
                }
            })
            .catch(function(err) { console.error('Failed to load filter badges:', err); });
    },

    cacheElements: function() {
        var ids = [
            'queue-screen', 'review-screen', 'summary-screen', 'history-screen',
            'video-list', 'queue-summary', 'pending-count', 'video-count',
            'all-videos-card', 'all-videos-subtitle',
            'card-container', 'reject-sheet',
            'approve-button', 'reject-button', 'skip-button', 'undo-button', 'bad-bbox-button',
            'review-back', 'queue-back', 'history-back', 'history-button',
            'review-menu', 'review-menu-dropdown', 'menu-hard-refresh', 'menu-reset-zoom', 'menu-copy-debug', 'menu-ai-feedback', 'ai-feedback-modal', 'feedback-context', 'feedback-text', 'feedback-close', 'feedback-cancel', 'feedback-submit',
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
        if (this.els.badBboxButton) {
            this.els.badBboxButton.addEventListener('click', function() {
                self.markBadBbox();
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
        // AI Feedback handler
        if (this.els.menuAiFeedback) {
            this.els.menuAiFeedback.addEventListener('click', function() {
                self.openFeedbackModal();
                var dd = self.els.reviewMenuDropdown;
                if (dd) dd.classList.add('hidden');
            });
        }
        if (this.els.feedbackClose) {
            this.els.feedbackClose.addEventListener('click', function() { self.closeFeedbackModal(); });
        }
        if (this.els.feedbackCancel) {
            this.els.feedbackCancel.addEventListener('click', function() { self.closeFeedbackModal(); });
        }
        if (this.els.feedbackSubmit) {
            this.els.feedbackSubmit.addEventListener('click', function() { self.submitFeedback(); });
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
                    self.crossCameraMode = (self.activeFilter === 'cross_camera');
                    self.clusterMode = (self.activeFilter === 'clusters');
                    // Persist filter in URL for refresh resilience
                    var url = new URL(window.location);
                    url.searchParams.set('filter', self.activeFilter);
                    url.hash = '';
                    history.replaceState(null, '', url);
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
                        } else if (self.crossCameraMode) {
                            self.loadCrossCameraQueueSummary();
                        } else if (self.clusterMode) {
                            self.loadClusterQueueSummary();
                        } else {
                            self.loadQueueSummary();
                        }
                    }
                });
            })(filterChips[i]);
        }

        // Confidence dropdown handler
        var confSelect = document.getElementById('confidence-filter');
        if (confSelect) {
            confSelect.addEventListener('change', function() {
                self.confidenceFilter = confSelect.value;
                if (self.classifyMode) {
                    self.loadClassifyQueueSummary();
                } else if (self.crossCameraMode) {
                    self.loadCrossCameraQueueSummary();
                } else if (self.clusterMode) {
                    self.loadClusterQueueSummary();
                } else {
                    self.loadQueueSummary();
                }
            });
        }

        // Camera dropdown handler
        var camSelect = document.getElementById('camera-filter');
        if (camSelect) {
            camSelect.addEventListener('change', function() {
                self.cameraFilter = camSelect.value;
                if (self.classifyMode) {
                    self.loadClassifyQueueSummary();
                } else if (self.crossCameraMode) {
                    self.loadCrossCameraQueueSummary();
                } else if (self.clusterMode) {
                    self.loadClusterQueueSummary();
                } else {
                    self.loadQueueSummary();
                }
            });
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

        // Flush pending syncs on page unload / visibility change
        window.addEventListener('beforeunload', function() {
            self.flushSync();
        });
        document.addEventListener('visibilitychange', function() {
            if (document.visibilityState === 'hidden') {
                self.flushSync();
            }
        });

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
        var self = this;
        var total = this.sessionStats.approved + this.sessionStats.rejected + this.sessionStats.skipped + this.sessionStats.reclassified;
        var flushPromise = null;
        if (total > 0 && (this.syncQueue.length > 0 || (this.classifyMode && this.classifySyncQueue.length > 0))) {
            flushPromise = this.flushSync();
        }
        var showNext = function() {
            if (total > 0) {
                self.showSummary();
            } else {
                self.showQueue();
            }
        };
        if (flushPromise && flushPromise.then) {
            flushPromise.then(showNext).catch(showNext);
        } else {
            showNext();
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
        var cp = this._confidenceParams();
        if (cp) url += (url.indexOf('?') >= 0 ? '&' : '?') + cp;
        var cam = this._cameraParams();
        if (cam) url += (url.indexOf('?') >= 0 ? '&' : '?') + cam;
        fetch(url)
            .then(function(resp) { return resp.json(); })
            .then(function(data) {
                self.queueData = data;
                self.renderQueueSummary(data);
                // Refresh badge counts
                self.loadFilterBadges();
            })
            .catch(function(err) {
                console.error('Failed to load queue summary:', err);
            });
    },

    loadCrossCameraQueueSummary: function() {
        var self = this;
        fetch('/api/ai/cross-camera/review-queue?status=auto')
            .then(function(resp) { return resp.json(); })
            .then(function(data) {
                var links = data.links || [];
                self.queueData = {
                    total_pending: links.length,
                    video_count: links.length,
                    videos: links.map(function(l) {
                        return {
                            video_id: '__cc__' + l.id,
                            video_title: (l.camera_a || 'Cam A') + ' \u2194 ' + (l.camera_b || 'Cam B') + ' (' + Math.round((l.match_confidence || l.confidence || 0) * 100) + '% match)',
                            thumbnail_path: l.pred_id_a ? '/thumbnails/annotated/' + l.pred_id_a : '',
                            pending_count: 1,
                            total_count: 1,
                            reviewed_count: 0,
                            avg_confidence: l.match_confidence || l.confidence,
                            _ccLink: l
                        };
                    })
                };
                self.renderQueueSummary(self.queueData);
                if (self.els.allVideosSubtitle) {
                    self.els.allVideosSubtitle.textContent = links.length + ' cross-camera matches pending';
                }
            })
            .catch(function(err) {
                console.error('Failed to load cross-camera queue:', err);
            });
    },

    loadClusterQueueSummary: function() {
        var self = this;
        fetch('/api/ai/predictions/static-clusters')
            .then(function(resp) { return resp.json(); })
            .then(function(data) {
                var clusters = data.clusters || [];
                self.queueData = {
                    total_pending: clusters.length,
                    video_count: clusters.length,
                    videos: clusters.map(function(c) {
                        return {
                            video_id: '__cluster__' + c.cluster_key,
                            video_title: c.camera_id + ' - ' + (c.yolo_classes || []).join(', ') + ' (' + c.count + ' detections)',
                            thumbnail_path: '/thumbnails/annotated/' + c.sample_id,
                            pending_count: c.count,
                            total_count: c.count,
                            reviewed_count: 0,
                            avg_confidence: c.avg_confidence,
                            _cluster: c
                        };
                    })
                };
                self.renderQueueSummary(self.queueData);
                if (self.els.allVideosSubtitle) {
                    self.els.allVideosSubtitle.textContent = clusters.length + ' clusters pending review';
                }
            })
            .catch(function(err) {
                console.error('Failed to load cluster queue:', err);
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
            if (this.activeFilter === 'all') {
                this.els.allVideosSubtitle.textContent = (data.total_pending || 0) + ' items pending review';
            } else if (this.groupedMode && data.total_predictions && data.total_predictions !== data.total_pending) {
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
            // Update card title based on active filter
            var cardTitle = allCard.querySelector('.video-card-title');
            if (cardTitle) {
                if (this.activeFilter === 'cross_camera') cardTitle.textContent = 'Start Review';
                else if (this.activeFilter === 'clusters') cardTitle.textContent = 'Start Review';
                else cardTitle.textContent = 'All Videos';
            }
            allCard.onclick = function() { ReviewApp.startReview(null, cardTitle ? cardTitle.textContent : 'All Videos'); };
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

        // Class + confidence line
        if (v.dominant_class || v.avg_confidence) {
            var metaDiv = document.createElement('div');
            metaDiv.className = 'video-card-meta';
            var parts = [];
            if (v.dominant_class) parts.push(v.dominant_class);
            if (v.avg_confidence) parts.push(Math.round(v.avg_confidence * 100) + '%');
            metaDiv.textContent = parts.join(' \u00b7 ');
            infoDiv.appendChild(titleDiv);
            infoDiv.appendChild(metaDiv);
            infoDiv.appendChild(subtitleDiv);
        } else {
            infoDiv.appendChild(titleDiv);
            infoDiv.appendChild(subtitleDiv);
        }

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
        this.sessionStats = { approved: 0, rejected: 0, skipped: 0, reclassified: 0 };
        this.reviewLog = [];
        this.undoStack = null;
        this.skippedIds = new Set();
        this.syncQueue = [];

        if (this.els.reviewVideoTitle) {
            this.els.reviewVideoTitle.textContent = videoTitle || 'All Videos';
        }

        // Fetch reclassification classes for reject sheet (initially without camera, will re-fetch with camera once predictions load)
        this.fetchReclassifyClasses('');

        // Mixed "All" queue: fetch all types in parallel when no specific video
        if (this.activeFilter === 'all' && !videoId) {
            var promises = [
                fetch('/api/ai/predictions/review-queue?limit=200' + (this.groupedMode ? '&grouped=1' : '') + (this._confidenceParams() ? '&' + this._confidenceParams() : '') + (this._cameraParams() ? '&' + this._cameraParams() : ''))
                    .then(function(r) { return r.json(); })
                    .then(function(data) {
                        return (data.predictions || []).map(function(p) {
                            p._reviewType = 'prediction';
                            return p;
                        });
                    })
                    .catch(function() { return []; }),
                fetch('/api/ai/cross-camera/review-queue?status=auto')
                    .then(function(r) { return r.json(); })
                    .then(function(data) {
                        return (data.links || []).map(function(link) {
                            return {
                                id: link.id,
                                _reviewType: 'cross_camera',
                                _link: link,
                                pred_id_a: link.pred_id_a,
                                pred_id_b: link.pred_id_b,
                                track_a_id: link.track_a_id,
                                track_b_id: link.track_b_id,
                                source_track_type: link.source_track_type,
                                camera_a: link.camera_a,
                                camera_b: link.camera_b,
                                match_confidence: link.match_confidence,
                                match_method: link.match_method,
                                entity_type: link.entity_type,
                                first_seen_a: link.first_seen_a,
                                first_seen_b: link.first_seen_b,
                                cls_a: link.cls_a,
                                cls_b: link.cls_b,
                                confidence: link.match_confidence
                            };
                        });
                    })
                    .catch(function() { return []; }),
                fetch('/api/ai/predictions/static-clusters')
                    .then(function(r) { return r.json(); })
                    .then(function(data) {
                        return (data.clusters || []).map(function(cluster) {
                            return {
                                id: cluster.cluster_key,
                                _reviewType: 'cluster',
                                _cluster: cluster,
                                cluster_key: cluster.cluster_key,
                                camera_id: cluster.camera_id,
                                count: cluster.count,
                                avg_confidence: cluster.avg_confidence,
                                yolo_classes: cluster.yolo_classes,
                                members: cluster.members || [],
                                confidence: cluster.avg_confidence
                            };
                        });
                    })
                    .catch(function() { return []; })
            ];
            Promise.all(promises).then(function(results) {
                var merged = [];
                for (var i = 0; i < results.length; i++) {
                    merged = merged.concat(results[i]);
                }
                // Sort by confidence ASC (low confidence = needs most attention first)
                merged.sort(function(a, b) {
                    return (a.confidence || 0) - (b.confidence || 0);
                });
                self.predictions = merged;
                if (self.predictions.length === 0) {
                    self.showQueue();
                    return;
                }
                self._restoreIndexFromHash();
                self.showScreen('review');
                self.renderCurrentCard();
                self.preloadImages(3);
                self.updateProgress();
                self.updateDots();
                self.updateUndoButton();
            }).catch(function(err) {
                console.error('Failed to load mixed queue:', err);
            });
            return;
        }

        var url;
        if (this.classifyMode) {
            // Always use ungrouped for classify — grouped query drops predictions
            // whose group representative is already classified
            url = '/api/ai/predictions/classification-queue?limit=200';
            if (this.classifyIncludePending) url += '&include_pending=true';
        } else if (this.crossCameraMode) {
            // Cross-camera mode: fetch links, map to prediction-like objects
            fetch('/api/ai/cross-camera/review-queue?status=auto')
                .then(function(resp) { return resp.json(); })
                .then(function(data) {
                    var links = data.links || [];
                    self.predictions = links.map(function(link) {
                        return {
                            id: link.id,
                            _reviewType: 'cross_camera',
                            _link: link,
                            pred_id_a: link.pred_id_a,
                            pred_id_b: link.pred_id_b,
                            track_a_id: link.track_a_id,
                            track_b_id: link.track_b_id,
                            source_track_type: link.source_track_type,
                            camera_a: link.camera_a,
                            camera_b: link.camera_b,
                            match_confidence: link.match_confidence,
                            match_method: link.match_method,
                            entity_type: link.entity_type,
                            first_seen_a: link.first_seen_a,
                            first_seen_b: link.first_seen_b,
                            cls_a: link.cls_a,
                            cls_b: link.cls_b,
                            confidence: link.match_confidence
                        };
                    });
                    if (self.predictions.length === 0) {
                        self.showQueue();
                        return;
                    }
                    self._restoreIndexFromHash();
                    self.showScreen('review');
                    self.renderCurrentCard();
                    self.updateProgress();
                    self.updateDots();
                    self.updateUndoButton();
                })
                .catch(function(err) {
                    console.error('Failed to load cross-camera queue:', err);
                });
            return;
        } else if (this.clusterMode) {
            // Cluster mode: fetch clusters, map to prediction-like objects
            fetch('/api/ai/predictions/static-clusters')
                .then(function(resp) { return resp.json(); })
                .then(function(data) {
                    var clusters = data.clusters || [];
                    self.predictions = clusters.map(function(cluster) {
                        return {
                            id: cluster.cluster_key,
                            _reviewType: 'cluster',
                            _cluster: cluster,
                            cluster_key: cluster.cluster_key,
                            camera_id: cluster.camera_id,
                            count: cluster.count,
                            avg_confidence: cluster.avg_confidence,
                            yolo_classes: cluster.yolo_classes,
                            members: cluster.members || [],
                            confidence: cluster.avg_confidence
                        };
                    });
                    if (self.predictions.length === 0) {
                        self.showQueue();
                        return;
                    }
                    self._restoreIndexFromHash();
                    self.showScreen('review');
                    self.renderCurrentCard();
                    self.updateProgress();
                    self.updateDots();
                    self.updateUndoButton();
                })
                .catch(function(err) {
                    console.error('Failed to load cluster queue:', err);
                });
            return;
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
        var cp = this._confidenceParams();
        if (cp) url += '&' + cp;
        var cam = this._cameraParams();
        if (cam) url += '&' + cam;

        fetch(url)
            .then(function(resp) { return resp.json(); })
            .then(function(data) {
                self.predictions = data.predictions || [];
                if (self.predictions.length === 0) {
                    // No predictions for this video — refresh queue list and go back
                    self.showQueue();
                    return;
                }
                self._restoreIndexFromHash();
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

    renderCrossCameraCard: function(pred) {
        if (!this.els.cardContainer) return;
        var card = document.createElement('div');
        card.className = 'review-card cross-camera-card';
        card.style.willChange = 'transform';

        // Camera A section (top)
        var sectionA = document.createElement('div');
        sectionA.className = 'cc-section';
        var imgA = document.createElement('img');
        imgA.className = 'cc-crop';
        imgA.src = (pred.source_track_type === 'video_track')
            ? '/api/ai/video-tracks/' + pred.track_a_id + '/crop'
            : '/thumbnails/crop/' + (pred.pred_id_a || '');
        imgA.alt = 'Camera A';
        imgA.onerror = function() { this.style.display = 'none'; };
        sectionA.appendChild(imgA);
        var badgeA = document.createElement('div');
        badgeA.className = 'cc-camera-badge';
        badgeA.textContent = pred.camera_a || 'Camera A';
        sectionA.appendChild(badgeA);
        // Time badge
        if (pred.first_seen_a) {
            var timeA = document.createElement('div');
            timeA.className = 'cc-time-badge';
            timeA.textContent = this._formatCCTime(pred.first_seen_a);
            sectionA.appendChild(timeA);
        }
        // Class badge
        var clsA = this._formatCCClass(pred.cls_a);
        if (clsA) {
            var clsBadgeA = document.createElement('div');
            clsBadgeA.className = 'cc-class-badge';
            clsBadgeA.textContent = clsA;
            sectionA.appendChild(clsBadgeA);
        }
        card.appendChild(sectionA);

        // Match confidence divider
        var divider = document.createElement('div');
        divider.className = 'cc-divider';
        var confPct = Math.round((pred.match_confidence || 0) * 100);
        var confBadge = document.createElement('span');
        confBadge.className = 'cc-confidence';
        if (confPct >= 70) confBadge.classList.add('cc-conf-high');
        else if (confPct >= 40) confBadge.classList.add('cc-conf-medium');
        else confBadge.classList.add('cc-conf-low');
        confBadge.textContent = confPct + '% match';
        divider.appendChild(confBadge);
        if (pred.match_method) {
            var methodBadge = document.createElement('span');
            methodBadge.className = 'cc-method';
            methodBadge.textContent = pred.match_method;
            divider.appendChild(methodBadge);
        }
        // Play Video button
        var playBtn = document.createElement('button');
        playBtn.className = 'cc-play-btn';
        playBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="white"><polygon points="5 3 19 12 5 21"/></svg> Play Video';
        var linkId = pred.id;
        playBtn.onclick = function(e) { e.stopPropagation(); console.log('Play Video clicked, linkId:', linkId, 'source_track_type:', pred.source_track_type, 'pred:', pred); ReviewApp.loadCrossCameraVideo(linkId, card, pred); };
        divider.appendChild(playBtn);
        card.appendChild(divider);

        // Camera B section (bottom)
        var sectionB = document.createElement('div');
        sectionB.className = 'cc-section';
        var imgB = document.createElement('img');
        imgB.className = 'cc-crop';
        imgB.src = (pred.source_track_type === 'video_track')
            ? '/api/ai/video-tracks/' + pred.track_b_id + '/crop'
            : '/thumbnails/crop/' + (pred.pred_id_b || '');
        imgB.alt = 'Camera B';
        imgB.onerror = function() { this.style.display = 'none'; };
        sectionB.appendChild(imgB);
        var badgeB = document.createElement('div');
        badgeB.className = 'cc-camera-badge';
        badgeB.textContent = pred.camera_b || 'Camera B';
        sectionB.appendChild(badgeB);
        if (pred.first_seen_b) {
            var timeB = document.createElement('div');
            timeB.className = 'cc-time-badge';
            timeB.textContent = this._formatCCTime(pred.first_seen_b);
            sectionB.appendChild(timeB);
        }
        var clsB = this._formatCCClass(pred.cls_b);
        if (clsB) {
            var clsBadgeB = document.createElement('div');
            clsBadgeB.className = 'cc-class-badge';
            clsBadgeB.textContent = clsB;
            sectionB.appendChild(clsBadgeB);
        }
        card.appendChild(sectionB);

        this.els.cardContainer.appendChild(card);

        // Update metadata strip
        if (this.els.predClass) this.els.predClass.textContent = 'Cross-Camera Match';
        if (this.els.predConfidence) this.els.predConfidence.textContent = confPct + '%';
        if (this.els.predModel) this.els.predModel.textContent = pred.match_method || 'ReID';
        if (this.els.reviewGuidance) this.els.reviewGuidance.textContent = 'Are these images of the same vehicle?';
    },

    _formatCCTime: function(val) {
        if (!val) return '';
        try {
            var num = parseFloat(val);
            var d = (!isNaN(num) && num > 1e9) ? new Date(num * 1000) : new Date(val);
            if (isNaN(d.getTime())) return '';
            return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        } catch(e) { return ''; }
    },

    _formatCCClass: function(cls) {
        if (!cls) return null;
        if (typeof cls === 'string') return cls;
        return cls.vehicle_subtype || cls.class || cls.label || null;
    },

    loadCrossCameraVideo: function(linkId, card, pred) {
        var self = this;

        // Fetch clip info
        fetch('/api/ai/cross-camera/links/' + linkId + '/clips')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (!data.success) {
                self._showToast('Video not available', '#FF9800');
                return;
            }
            self._renderDualVideoPlayer(data, card, pred);
        })
        .catch(function(err) {
            self._showToast('Failed to load video', '#f44336');
        });
    },

    _renderDualVideoPlayer: function(clipData, card, pred) {
        var self = this;

        // Store clip data for debug copy
        this._ccClipData = clipData;

        // Fetch calibration data if not already loaded
        if (!window._cameraCalibration) {
            fetch('/api/ai/feedback/calibration')
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.success) {
                    window._cameraCalibration = data.calibration;
                    console.log('Camera calibration loaded:', data.calibration);
                }
            })
            .catch(function(err) {
                console.warn('Failed to load camera calibration:', err);
            });
        }

        // Build video container
        var videoContainer = document.createElement('div');
        videoContainer.className = 'cc-video-container';

        // Camera A video
        var playerA = this._createVideoSection(clipData.camera_a, 'A');
        videoContainer.appendChild(playerA.wrapper);

        // Sync divider with controls
        var syncControls = document.createElement('div');
        syncControls.className = 'cc-sync-controls';
        syncControls.innerHTML = '<div class="cc-sync-row">' +
            '<button class="cc-sync-play-btn" id="cc-sync-play"><svg width="24" height="24" viewBox="0 0 24 24" fill="white"><polygon points="5 3 19 12 5 21"/></svg></button>' +
            '<span class="cc-sync-label">Synchronized</span>' +
            '<button class="cc-correct-bbox-btn" id="cc-correct-bbox" title="Correct bbox position"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2" stroke-dasharray="4 2"/><path d="M7 7l10 10M17 7l-10 10"/></svg></button>' +
            '<button class="cc-sync-close-btn" id="cc-sync-close">&times;</button>' +
            '</div>' +
            '<div class="cc-timeline-wrap" id="cc-timeline-wrap">' +
            '<div class="cc-timeline-bar">' +
            '<div class="cc-timeline-track-a" id="cc-tl-track-a"></div>' +
            '<div class="cc-timeline-track-b" id="cc-tl-track-b"></div>' +
            '<div class="cc-timeline-playhead" id="cc-tl-playhead"></div>' +
            '</div>' +
            '<div class="cc-timeline-time" id="cc-tl-time">0:00</div>' +
            '</div>';
        videoContainer.appendChild(syncControls);

        // Camera B video
        var playerB = this._createVideoSection(clipData.camera_b, 'B');
        videoContainer.appendChild(playerB.wrapper);

        // Replace card content
        card.innerHTML = '';
        card.appendChild(videoContainer);
        card.classList.add('cc-video-active');

        // Wire up sync controls
        var playPauseBtn = syncControls.querySelector('#cc-sync-play');
        var closeBtn = syncControls.querySelector('#cc-sync-close');
        var videoA = playerA.video;
        var videoB = playerB.video;
        var canvasA = playerA.canvas;
        var canvasB = playerB.canvas;
        var isPlaying = false;

        var MAX_PROJECT = 3.0; // Max seconds to project through occlusion

        // Calculate the occlusion threshold for a trajectory
        // Only flag as "projected" when the gap is large enough to be a real occlusion
        // (vehicle behind an object), not just a few dropped detection frames
        function calcNormalGap(traj) {
            if (!traj || traj.length < 2) return 0.4;
            // Sample up to 20 consecutive gaps and take the median
            var gaps = [];
            var limit = Math.min(traj.length - 1, 20);
            for (var i = 0; i < limit; i++) {
                var dt = (traj[i + 1].timestamp || traj[i + 1].ts || 0) - (traj[i].timestamp || traj[i].ts || 0);
                if (dt > 0) gaps.push(dt);
            }
            if (!gaps.length) return 0.4;
            gaps.sort(function(a, b) { return a - b; });
            var median = gaps[Math.floor(gaps.length / 2)];
            // Occlusion threshold: at least 0.4s, or 8x median frame gap
            // At 15fps (gap=0.069), threshold = 0.55s — any gap over that is occlusion
            // At 30fps (gap=0.033), threshold = 0.4s
            return Math.max(0.4, median * 8);
        }

        // Estimate velocity and acceleration using exponentially-weighted recent points
        // Returns { vx, vy, vw, vh, ax, ay, aw, ah } for quadratic projection
        function estimateMotion(traj, fromIdx) {
            if (fromIdx < 1) return { vx: 0, vy: 0, vw: 0, vh: 0, ax: 0, ay: 0, aw: 0, ah: 0 };

            // Use last 5 points with exponential weighting (recent points weighted ~3x more)
            var window = Math.min(5, fromIdx + 1);
            var startIdx = fromIdx - window + 1;

            // Compute weighted velocity using exponential weights
            var sumWt = 0, sumWtDt = 0;
            var sumWtDx = 0, sumWtDy = 0, sumWtDw = 0, sumWtDh = 0;

            for (var i = startIdx; i <= fromIdx; i++) {
                if (i === startIdx) continue; // Need pair for velocity
                var p0 = traj[i - 1], p1 = traj[i];
                var t0 = p0.timestamp || p0.ts || 0;
                var t1 = p1.timestamp || p1.ts || 0;
                var dt = t1 - t0;
                if (dt <= 0) continue;

                // Exponential weight: more recent = higher weight (decay factor ~0.5)
                var age = fromIdx - i;
                var weight = Math.exp(-0.7 * age); // Recent point ~2x, oldest ~0.25x

                sumWt += weight;
                sumWtDt += weight * dt;
                sumWtDx += weight * ((p1.x || 0) - (p0.x || 0));
                sumWtDy += weight * ((p1.y || 0) - (p0.y || 0));
                sumWtDw += weight * ((p1.w || 0) - (p0.w || 0));
                sumWtDh += weight * ((p1.h || 0) - (p0.h || 0));
            }

            var vx = 0, vy = 0, vw = 0, vh = 0;
            if (sumWtDt > 0) {
                vx = sumWtDx / sumWtDt;
                vy = sumWtDy / sumWtDt;
                vw = sumWtDw / sumWtDt;
                vh = sumWtDh / sumWtDt;
            }

            // Estimate acceleration using last 3 velocity samples
            var ax = 0, ay = 0, aw = 0, ah = 0;
            if (fromIdx >= 2) {
                var velocities = [];
                for (var j = Math.max(0, fromIdx - 2); j <= fromIdx; j++) {
                    if (j < 1) continue;
                    var pp = traj[j - 1], pc = traj[j];
                    var tp = pp.timestamp || pp.ts || 0;
                    var tc = pc.timestamp || pc.ts || 0;
                    var dtv = tc - tp;
                    if (dtv > 0) {
                        velocities.push({
                            t: tc,
                            vx: ((pc.x || 0) - (pp.x || 0)) / dtv,
                            vy: ((pc.y || 0) - (pp.y || 0)) / dtv,
                            vw: ((pc.w || 0) - (pp.w || 0)) / dtv,
                            vh: ((pc.h || 0) - (pp.h || 0)) / dtv
                        });
                    }
                }
                if (velocities.length >= 2) {
                    var v0 = velocities[0], v1 = velocities[velocities.length - 1];
                    var dtAcc = v1.t - v0.t;
                    if (dtAcc > 0) {
                        ax = (v1.vx - v0.vx) / dtAcc;
                        ay = (v1.vy - v0.vy) / dtAcc;
                        aw = (v1.vw - v0.vw) / dtAcc;
                        ah = (v1.vh - v0.vh) / dtAcc;
                    }
                }
            }

            // Detect approaching vehicle: if bbox area is growing, the object is getting closer
            // and perspective effects cause exponential apparent velocity growth
            var firstPt = traj[0];
            var lastPt = traj[fromIdx];
            var firstArea = (firstPt.w || 1) * (firstPt.h || 1);
            var lastArea = (lastPt.w || 1) * (lastPt.h || 1);
            var areaGrowthRate = (lastArea / firstArea);
            var trackDuration = (lastPt.timestamp || lastPt.ts || 0) - (firstPt.timestamp || firstPt.ts || 0);

            if (areaGrowthRate > 1.3 && trackDuration > 0.3) {
                // Vehicle is approaching - boost acceleration to account for perspective
                var perspectiveBoost = Math.min(areaGrowthRate, 5.0);
                ax = ax * perspectiveBoost;
                ay = ay * perspectiveBoost;
                // Also boost size change rates
                aw = aw * perspectiveBoost;
                ah = ah * perspectiveBoost;
            }

            return {
                vx: vx, vy: vy, vw: vw, vh: vh,
                ax: ax, ay: ay, aw: aw, ah: ah
            };
        }

        // Quadratic projection: pos = p0 + v*t + 0.5*a*t²
        function projectPosition(base, motion, elapsed) {
            return {
                x: Math.round((base.x || 0) + motion.vx * elapsed + 0.5 * motion.ax * elapsed * elapsed),
                y: Math.round((base.y || 0) + motion.vy * elapsed + 0.5 * motion.ay * elapsed * elapsed),
                width: Math.max(10, Math.round((base.w || 0) + motion.vw * elapsed + 0.5 * motion.aw * elapsed * elapsed)),
                height: Math.max(10, Math.round((base.h || 0) + motion.vh * elapsed + 0.5 * motion.ah * elapsed * elapsed)),
                projected: true
            };
        }

        // Cache normal gap per camera trajectory
        var normalGapA = calcNormalGap(clipData.camera_a && clipData.camera_a.trajectory);
        var normalGapB = calcNormalGap(clipData.camera_b && clipData.camera_b.trajectory);

        // Cross-camera bbox consensus: when one camera has a projected bbox,
        // check if the other camera has a real detection to confirm presence.
        var _consensusCache = { A: null, B: null, fetchedAtA: 0, fetchedAtB: 0 };
        var _consensusCacheTTL = 500; // ms — cache for 0.5s to avoid hammering API

        function fetchConsensusHint(linkId, cam, videoTime) {
            if (!linkId) return null;
            var cacheKey = cam.toUpperCase();
            var now = Date.now();
            // Return cached result if fresh
            if (_consensusCache[cacheKey] && (now - _consensusCache['fetchedAt' + cacheKey]) < _consensusCacheTTL) {
                return _consensusCache[cacheKey];
            }
            // Fire async fetch, return current cache (may be stale or null)
            var url = '/api/ai/cross-camera/' + linkId + '/consensus?camera=' + cacheKey + '&time=' + videoTime.toFixed(3);
            fetch(url)
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    _consensusCache[cacheKey] = data;
                    _consensusCache['fetchedAt' + cacheKey] = Date.now();
                })
                .catch(function() {
                    _consensusCache[cacheKey] = null;
                    _consensusCache['fetchedAt' + cacheKey] = Date.now();
                });
            return _consensusCache[cacheKey];
        }

        // Get bbox for current video time from trajectory with interpolation/projection
        // Returns { x, y, width, height, projected: bool } or null
        // Check if a bbox result has exited the frame
        function isOutOfFrame(bbox, frameW, frameH) {
            if (!bbox || !frameW || !frameH) return false;
            // Out-of-frame if right edge past frame or left edge past frame
            // (bbox fully outside or >70% outside)
            var overlapX = Math.min(bbox.x + bbox.width, frameW) - Math.max(bbox.x, 0);
            var overlapY = Math.min(bbox.y + bbox.height, frameH) - Math.max(bbox.y, 0);
            var visibleArea = Math.max(0, overlapX) * Math.max(0, overlapY);
            var totalArea = bbox.width * bbox.height;
            // If less than 30% of bbox is visible in frame, consider it gone
            return totalArea > 0 && visibleArea / totalArea < 0.3;
        }

        // Limit projection time based on track duration (don't project longer than you observed)
        function effectiveMaxProject(traj) {
            if (!traj || traj.length < 2) return 0.5;
            var firstTs = traj[0].timestamp || traj[0].ts || 0;
            var lastTs = traj[traj.length - 1].timestamp || traj[traj.length - 1].ts || 0;
            var duration = lastTs - firstTs;
            // For short tracks (<2s), allow more projection time to cover typical clip durations
            // For longer tracks, use 2x duration as before
            if (duration < 2.0) {
                return Math.max(1.0, Math.min(MAX_PROJECT, 4.0));
            }
            return Math.max(0.5, Math.min(MAX_PROJECT, duration * 2));
        }

        // Compute baseline bbox area from first N points of a trajectory
        // Used to detect when the tracker latches onto a different object (sign, etc.)
        function calcBaselineArea(traj) {
            if (!traj || traj.length < 2) return null;
            var n = Math.min(traj.length, 8);
            var sum = 0;
            for (var i = 0; i < n; i++) {
                sum += (traj[i].w || 0) * (traj[i].h || 0);
            }
            return sum / n;
        }

        function getBboxAtTime(cam, videoTime, normalGap, camLabel) {
            if (!cam) return null;
            var traj = cam.trajectory;
            if (!traj || !traj.length) {
                var fb = cam.bbox;
                return fb ? { x: fb.x, y: fb.y, width: fb.width, height: fb.height, projected: false } : null;
            }

            var frameW = cam.video_width || 1920;
            var frameH = cam.video_height || 1080;
            var occlusionThreshold = normalGap || 0.15;
            var maxProj = effectiveMaxProject(traj);
            var baseArea = calcBaselineArea(traj);

            // Find bracketing trajectory points
            var beforeIdx = -1, afterIdx = -1;
            for (var i = 0; i < traj.length; i++) {
                var ts = traj[i].timestamp || traj[i].ts || 0;
                if (ts <= videoTime) {
                    beforeIdx = i;
                } else {
                    afterIdx = i;
                    break;
                }
            }

            var before = beforeIdx >= 0 ? traj[beforeIdx] : null;
            var after = afterIdx >= 0 ? traj[afterIdx] : null;

            // Size anomaly detection: if the tracker latched onto a different object
            // (e.g., sign after vehicle left), the bbox area grows dramatically.
            // Walk back to find the last "good" point and treat everything after as invalid.
            if (baseArea && baseArea > 0) {
                var sizeLimit = baseArea * 3.0; // 3x baseline area = anomaly
                if (before && (before.w || 0) * (before.h || 0) > sizeLimit) {
                    // Find last good point before the anomaly
                    var lastGoodIdx = -1;
                    for (var gi = beforeIdx; gi >= 0; gi--) {
                        if ((traj[gi].w || 0) * (traj[gi].h || 0) <= sizeLimit) {
                            lastGoodIdx = gi;
                            break;
                        }
                    }
                    if (lastGoodIdx < 0) return null; // All points are anomalous
                    // Treat as track-end from last good point
                    before = traj[lastGoodIdx];
                    beforeIdx = lastGoodIdx;
                    after = null; // Ignore anomalous after-points
                    afterIdx = -1;
                }
                if (after && (after.w || 0) * (after.h || 0) > sizeLimit) {
                    after = null;
                    afterIdx = -1;
                }
            }

            // No points at all
            if (!before && !after) return null;

            // Before track starts
            if (!before && after) {
                var ats = after.timestamp || after.ts || 0;
                var lead = ats - videoTime;
                if (lead <= 0.5) {
                    return { x: after.x || 0, y: after.y || 0, width: after.w || 0, height: after.h || 0, projected: lead > occlusionThreshold };
                }
                return null;
            }

            // After track ends — project forward with velocity + acceleration
            if (before && !after) {
                var bts = before.timestamp || before.ts || 0;
                var elapsed = videoTime - bts;
                // Cross-camera consensus: extend projection if other camera confirms presence
                var endMaxProj = maxProj;
                var endConsensus = null;
                if (camLabel && elapsed > maxProj && elapsed <= maxProj + 1.5) {
                    endConsensus = fetchConsensusHint(clipData.link_id, camLabel, videoTime);
                    if (endConsensus && endConsensus.has_consensus) {
                        endMaxProj = maxProj + 1.5;
                    }
                }
                if (elapsed <= endMaxProj) {
                    var m = estimateMotion(traj, beforeIdx);

                    // Apply calibration if available
                    var cameraId = cam.camera_id;
                    var cal = window._cameraCalibration && window._cameraCalibration[cameraId];
                    if (cal && cal.velocity_multiplier_x) {
                        m.vx *= cal.velocity_multiplier_x;
                        m.vy *= cal.velocity_multiplier_y;
                    }

                    var proj = projectPosition(before, m, elapsed);
                    // Vehicle left the frame — stop showing bbox
                    if (isOutOfFrame(proj, frameW, frameH)) return null;
                    proj.projected = elapsed > occlusionThreshold;
                    // Add confidence decay indicator for projections
                    var projectionConfidence = Math.max(0, 1.0 - (elapsed / endMaxProj));
                    proj.projection_confidence = projectionConfidence;
                    // Tag with consensus confirmation when projected
                    if (camLabel && proj.projected) {
                        var ccHint = endConsensus || fetchConsensusHint(clipData.link_id, camLabel, videoTime);
                        if (ccHint && ccHint.has_consensus) {
                            proj.consensus_confirmed = true;
                        }
                    }
                    return proj;
                }
                return null;
            }

            // Both points exist
            var t0 = before.timestamp || before.ts || 0;
            var t1 = after.timestamp || after.ts || 0;
            var gap = t1 - t0;
            var isOcclusion = gap > occlusionThreshold;

            // If gap is enormous (> 10s), track is truly lost
            if (gap > 10) {
                var dBefore = videoTime - t0;
                if (dBefore <= maxProj) {
                    var mv = estimateMotion(traj, beforeIdx);

                    // Apply calibration if available
                    var cameraId = cam.camera_id;
                    var cal = window._cameraCalibration && window._cameraCalibration[cameraId];
                    if (cal && cal.velocity_multiplier_x) {
                        mv.vx *= cal.velocity_multiplier_x;
                        mv.vy *= cal.velocity_multiplier_y;
                    }

                    var proj = projectPosition(before, mv, dBefore);
                    if (isOutOfFrame(proj, frameW, frameH)) return null;
                    proj.projected = true;
                    // Cross-camera consensus tag for huge-gap projected bbox
                    if (camLabel) {
                        var gapHint = fetchConsensusHint(clipData.link_id, camLabel, videoTime);
                        if (gapHint && gapHint.has_consensus) proj.consensus_confirmed = true;
                    }
                    return proj;
                }
                var dAfter = t1 - videoTime;
                if (dAfter <= 0.5) {
                    return { x: after.x || 0, y: after.y || 0, width: after.w || 0, height: after.h || 0, projected: true };
                }
                return null;
            }

            // Occlusion: project forward from last-seen point (don't interpolate across gap)
            if (isOcclusion) {
                var dBefore = videoTime - t0;
                var dAfter = t1 - videoTime;
                // Close to re-appearing: snap to the "after" point
                if (dAfter <= 0.3) {
                    return { x: after.x || 0, y: after.y || 0, width: after.w || 0, height: after.h || 0, projected: true };
                }
                // Cross-camera consensus: extend occlusion projection if other camera confirms
                var occMaxProj = maxProj;
                var occConsensus = null;
                if (camLabel && dBefore > maxProj && dBefore <= maxProj + 1.5) {
                    occConsensus = fetchConsensusHint(clipData.link_id, camLabel, videoTime);
                    if (occConsensus && occConsensus.has_consensus) {
                        occMaxProj = maxProj + 1.5;
                    }
                }
                // Project forward from where vehicle was last seen
                if (dBefore <= occMaxProj) {
                    var mv = estimateMotion(traj, beforeIdx);

                    // Apply calibration if available
                    var cameraId = cam.camera_id;
                    var cal = window._cameraCalibration && window._cameraCalibration[cameraId];
                    if (cal && cal.velocity_multiplier_x) {
                        mv.vx *= cal.velocity_multiplier_x;
                        mv.vy *= cal.velocity_multiplier_y;
                    }

                    var proj = projectPosition(before, mv, dBefore);
                    if (isOutOfFrame(proj, frameW, frameH)) return null;
                    proj.projected = true;
                    // Tag with consensus confirmation
                    if (camLabel) {
                        var occHint = occConsensus || fetchConsensusHint(clipData.link_id, camLabel, videoTime);
                        if (occHint && occHint.has_consensus) proj.consensus_confirmed = true;
                    }
                    return proj;
                }
                // Deep in the gap, beyond projection range — hide bbox
                return null;
            }

            // Normal interpolation between close points (no occlusion)
            var t = gap > 0 ? (videoTime - t0) / gap : 0;
            return {
                x: Math.round((before.x || 0) + t * ((after.x || 0) - (before.x || 0))),
                y: Math.round((before.y || 0) + t * ((after.y || 0) - (before.y || 0))),
                width: Math.max(10, Math.round((before.w || 0) + t * ((after.w || 0) - (before.w || 0)))),
                height: Math.max(10, Math.round((before.h || 0) + t * ((after.h || 0) - (before.h || 0)))),
                projected: false
            };
        }

        // Draw bbox overlay accounting for object-fit: contain
        // bbox.projected = true → dotted line (occlusion/extrapolation)
        function drawBbox(canvas, cam, video, bbox, debug) {
            if (!canvas) return;
            var ctx = canvas.getContext('2d');

            // Match canvas pixel buffer to the element display size
            var elemW = video.clientWidth;
            var elemH = video.clientHeight;
            if (!elemW || !elemH) return;
            canvas.width = elemW;
            canvas.height = elemH;
            canvas.style.left = '0';
            canvas.style.top = '0';
            canvas.style.width = elemW + 'px';
            canvas.style.height = elemH + 'px';

            ctx.clearRect(0, 0, canvas.width, canvas.height);
            if (!cam || !bbox) return;

            // Calculate where the video is actually rendered (object-fit: contain)
            var vidW = video.videoWidth || (cam.video_width || 1920);
            var vidH = video.videoHeight || (cam.video_height || 1080);
            var videoAspect = vidW / vidH;
            var elemAspect = elemW / elemH;
            var renderW, renderH, offsetX, offsetY;
            if (videoAspect > elemAspect) {
                renderW = elemW;
                renderH = elemW / videoAspect;
                offsetX = 0;
                offsetY = (elemH - renderH) / 2;
            } else {
                renderH = elemH;
                renderW = elemH * videoAspect;
                offsetX = (elemW - renderW) / 2;
                offsetY = 0;
            }

            // Scale bbox from original resolution to rendered video area
            var srcW = cam.video_width || vidW;
            var srcH = cam.video_height || vidH;
            var scaleX = renderW / srcW;
            var scaleY = renderH / srcH;
            var bx = bbox.x * scaleX + offsetX;
            var by = bbox.y * scaleY + offsetY;
            var bw = bbox.width * scaleX;
            var bh = bbox.height * scaleY;

            // Projected (occlusion) → dashed orange; Observed → solid green
            if (bbox.projected) {
                ctx.strokeStyle = '#FFA500';
                ctx.lineWidth = 2;
                ctx.setLineDash([6, 4]);
                // Semi-transparent fill to indicate projected area
                ctx.fillStyle = 'rgba(255, 165, 0, 0.1)';
                ctx.fillRect(bx, by, bw, bh);
                // Low-confidence projection: use longer dashes and reduced opacity
                if (bbox.projection_confidence !== undefined && bbox.projection_confidence < 0.3) {
                    ctx.setLineDash([8, 4]);
                    ctx.globalAlpha = 0.4;
                }
            } else {
                ctx.strokeStyle = '#00FF00';
                ctx.lineWidth = 2;
                ctx.setLineDash([]);
            }
            ctx.strokeRect(bx, by, bw, bh);
            ctx.setLineDash([]);
            ctx.globalAlpha = 1.0;

            // Label
            var labelColor = bbox.projected ? 'rgba(255, 165, 0, 0.8)' : 'rgba(0, 255, 0, 0.7)';
            ctx.fillStyle = labelColor;
            ctx.font = '12px sans-serif';
            var label = cam.class_name || cam.camera_id || '';
            if (bbox.projected) label += ' (projected)';
            ctx.fillRect(bx, by - 16, ctx.measureText(label).width + 8, 16);
            ctx.fillStyle = '#000';
            ctx.fillText(label, bx + 4, by - 4);

            // Cross-camera consensus indicator: small "CC" badge when other camera confirms presence
            if (bbox.consensus_confirmed) {
                var ccX = bx + bw - 24;
                var ccY = by - 16;
                // Badge background — cyan to stand out from orange projected and green observed
                ctx.fillStyle = 'rgba(0, 200, 255, 0.9)';
                ctx.fillRect(ccX, ccY, 24, 16);
                // Badge border
                ctx.strokeStyle = 'rgba(0, 150, 200, 1)';
                ctx.lineWidth = 1;
                ctx.setLineDash([]);
                ctx.strokeRect(ccX, ccY, 24, 16);
                // Badge text
                ctx.fillStyle = '#000';
                ctx.font = 'bold 11px sans-serif';
                ctx.fillText('CC', ccX + 3, ccY + 12);
            }
        }

        // Animation loop for bbox overlay — uses trajectory for per-frame tracking
        var animFrame;
        function updateOverlays() {
            var bboxA = getBboxAtTime(clipData.camera_a, videoA.currentTime, normalGapA, 'A');
            var bboxB = getBboxAtTime(clipData.camera_b, videoB.currentTime, normalGapB, 'B');
            drawBbox(canvasA, clipData.camera_a, videoA, bboxA);
            drawBbox(canvasB, clipData.camera_b, videoB, bboxB);
            if (typeof updateTimeline === 'function') updateTimeline();
            if (isPlaying) animFrame = requestAnimationFrame(updateOverlays);
        }


        // Black overlay for "blank" state (before video starts / after it ends)
        var blankA = document.createElement('div');
        blankA.className = 'cc-blank-overlay';
        var wrapA = videoA.parentElement;
        if (wrapA) wrapA.appendChild(blankA);
        var blankB = document.createElement('div');
        blankB.className = 'cc-blank-overlay';
        var wrapB = videoB.parentElement;
        if (wrapB) wrapB.appendChild(blankB);

        function showBlank(el, show) {
            el.style.display = show ? 'block' : 'none';
        }
        showBlank(blankA, false);
        showBlank(blankB, false);

        // Synchronize videos based on absolute epoch timestamps
        var epochA = (clipData.camera_a && clipData.camera_a.first_seen_epoch) || 0;
        var epochB = (clipData.camera_b && clipData.camera_b.first_seen_epoch) || 0;
        var firstSeenA = (clipData.camera_a && clipData.camera_a.first_seen) || 0;
        var firstSeenB = (clipData.camera_b && clipData.camera_b.first_seen) || 0;

        // Calculate sync offset: align clip start times in real time
        var clipStartA = epochA > 0 ? epochA - firstSeenA : 0;
        var clipStartB = epochB > 0 ? epochB - firstSeenB : 0;
        // delayA/delayB: seconds to wait before starting each video
        // The clip that starts first in real time plays immediately; the other is delayed
        var delayA = 0, delayB = 0;
        if (epochA > 0 && epochB > 0) {
            var diff = clipStartA - clipStartB;
            if (diff > 0.1) {
                // A starts later in real time → delay A
                delayA = diff;
            } else if (diff < -0.1) {
                // B starts later in real time → delay B
                delayB = -diff;
            }
        }

        // Store delays on ReviewApp for feedback metadata access
        self._ccDelayA = delayA;
        self._ccDelayB = delayB;
        self._ccEpochA = clipStartA;
        self._ccEpochB = clipStartB;

        var delayTimerA = null, delayTimerB = null;
        var endedA = false, endedB = false;

        // ── Timeline scrubber ───────────────────────────────────
        var tlWrap = syncControls.querySelector('#cc-timeline-wrap');
        var tlTrackA = syncControls.querySelector('#cc-tl-track-a');
        var tlTrackB = syncControls.querySelector('#cc-tl-track-b');
        var tlPlayhead = syncControls.querySelector('#cc-tl-playhead');
        var tlTime = syncControls.querySelector('#cc-tl-time');

        // Compute total timeline duration in real-time seconds
        // Timeline starts at the earlier clip's real start, ends at the later clip's real end
        var durA = 0, durB = 0;
        function updateDurations() {
            if (videoA.duration && isFinite(videoA.duration)) durA = videoA.duration;
            if (videoB.duration && isFinite(videoB.duration)) durB = videoB.duration;
        }
        // Total timeline: max(delayA + durA, delayB + durB)
        function getTotalDuration() {
            updateDurations();
            return Math.max((delayA + durA) || 1, (delayB + durB) || 1);
        }

        // Position the track-A and track-B range indicators on the timeline
        function layoutTimeline() {
            updateDurations();
            var total = getTotalDuration();
            if (!total) return;
            // Track A: starts at delayA, lasts durA
            tlTrackA.style.left = ((delayA / total) * 100) + '%';
            tlTrackA.style.width = ((durA / total) * 100) + '%';
            // Track B: starts at delayB, lasts durB
            tlTrackB.style.left = ((delayB / total) * 100) + '%';
            tlTrackB.style.width = ((durB / total) * 100) + '%';
        }
        videoA.addEventListener('loadedmetadata', layoutTimeline);
        videoB.addEventListener('loadedmetadata', layoutTimeline);

        // Get current real-time position (seconds from timeline start)
        function getRealTime() {
            // The video that is NOT delayed gives us the baseline
            if (delayA > 0.1) {
                // B starts first; real time = videoB.currentTime + delayB(=0) = videoB.currentTime
                // But if B ended, use A: real time = videoA.currentTime + delayA
                if (endedB && !endedA) return videoA.currentTime + delayA;
                return videoB.currentTime;
            } else if (delayB > 0.1) {
                if (endedA && !endedB) return videoB.currentTime + delayB;
                return videoA.currentTime;
            }
            // No delay — both in sync
            return videoA.currentTime;
        }

        function formatTime(s) {
            var m = Math.floor(s / 60);
            var sec = Math.floor(s % 60);
            return m + ':' + (sec < 10 ? '0' : '') + sec;
        }

        // Update playhead position — called from animation loop
        function updateTimeline() {
            var total = getTotalDuration();
            var t = getRealTime();
            var pct = Math.min(100, Math.max(0, (t / total) * 100));
            tlPlayhead.style.left = pct + '%';
            tlTime.textContent = formatTime(t) + ' / ' + formatTime(total);
        }

        // Scrub: seek both videos to the tapped/dragged position
        function seekToFraction(frac) {
            var total = getTotalDuration();
            var targetTime = frac * total;

            // Compute each video's currentTime from the real-time target
            var tA = targetTime - delayA;
            var tB = targetTime - delayB;

            if (tA >= 0 && tA <= durA) {
                videoA.currentTime = tA;
                showBlank(blankA, false);
                endedA = false;
            } else if (tA < 0) {
                videoA.currentTime = 0;
                showBlank(blankA, true);
                endedA = false;
            } else {
                videoA.currentTime = durA;
                showBlank(blankA, true);
                endedA = true;
            }

            if (tB >= 0 && tB <= durB) {
                videoB.currentTime = tB;
                showBlank(blankB, false);
                endedB = false;
            } else if (tB < 0) {
                videoB.currentTime = 0;
                showBlank(blankB, true);
                endedB = false;
            } else {
                videoB.currentTime = durB;
                showBlank(blankB, true);
                endedB = true;
            }
            updateTimeline();
            // Update bbox overlays at new position
            var bboxA = getBboxAtTime(clipData.camera_a, videoA.currentTime, normalGapA);
            var bboxB = getBboxAtTime(clipData.camera_b, videoB.currentTime, normalGapB);
            drawBbox(canvasA, clipData.camera_a, videoA, bboxA);
            drawBbox(canvasB, clipData.camera_b, videoB, bboxB);
        }

        var scrubbing = false;
        function getTimelineFrac(e) {
            var rect = tlWrap.getBoundingClientRect();
            var x = (e.touches ? e.touches[0].clientX : e.clientX) - rect.left;
            return Math.min(1, Math.max(0, x / rect.width));
        }
        tlWrap.addEventListener('pointerdown', function(e) {
            scrubbing = true;
            var wasPlaying = isPlaying;
            if (isPlaying) { videoA.pause(); videoB.pause(); }
            seekToFraction(getTimelineFrac(e));
            function onMove(ev) { seekToFraction(getTimelineFrac(ev)); }
            function onUp() {
                scrubbing = false;
                document.removeEventListener('pointermove', onMove);
                document.removeEventListener('pointerup', onUp);
                if (wasPlaying) {
                    if (!endedA) { var p = videoA.play(); if (p && p.catch) p.catch(function(){}); }
                    if (!endedB) { var p = videoB.play(); if (p && p.catch) p.catch(function(){}); }
                    updateOverlays();
                }
            }
            document.addEventListener('pointermove', onMove);
            document.addEventListener('pointerup', onUp);
        });

        function setPauseIcon() {
            playPauseBtn.textContent = '';
            var pauseSvg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
            pauseSvg.setAttribute('width', '24'); pauseSvg.setAttribute('height', '24');
            pauseSvg.setAttribute('viewBox', '0 0 24 24'); pauseSvg.setAttribute('fill', 'white');
            var r1 = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
            r1.setAttribute('x', '6'); r1.setAttribute('y', '4');
            r1.setAttribute('width', '4'); r1.setAttribute('height', '16');
            var r2 = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
            r2.setAttribute('x', '14'); r2.setAttribute('y', '4');
            r2.setAttribute('width', '4'); r2.setAttribute('height', '16');
            pauseSvg.appendChild(r1); pauseSvg.appendChild(r2);
            playPauseBtn.appendChild(pauseSvg);
        }

        function setPlayIcon() {
            playPauseBtn.textContent = '';
            var playSvg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
            playSvg.setAttribute('width', '24'); playSvg.setAttribute('height', '24');
            playSvg.setAttribute('viewBox', '0 0 24 24'); playSvg.setAttribute('fill', 'white');
            var playPoly = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
            playPoly.setAttribute('points', '5 3 19 12 5 21');
            playSvg.appendChild(playPoly);
            playPauseBtn.appendChild(playSvg);
        }

        function startSyncedPlayback() {
            endedA = false;
            endedB = false;
            videoA.currentTime = 0;
            videoB.currentTime = 0;

            // Start the earlier video immediately, delay the later one
            if (delayA > 0.1) {
                showBlank(blankA, true);
                showBlank(blankB, false);
                var pB = videoB.play();
                if (pB && pB.catch) pB.catch(function() {});
                delayTimerA = setTimeout(function() {
                    showBlank(blankA, false);
                    var pA = videoA.play();
                    if (pA && pA.catch) pA.catch(function() {});
                }, delayA * 1000);
            } else if (delayB > 0.1) {
                showBlank(blankB, true);
                showBlank(blankA, false);
                var pA = videoA.play();
                if (pA && pA.catch) pA.catch(function() {});
                delayTimerB = setTimeout(function() {
                    showBlank(blankB, false);
                    var pB2 = videoB.play();
                    if (pB2 && pB2.catch) pB2.catch(function() {});
                }, delayB * 1000);
            } else {
                showBlank(blankA, false);
                showBlank(blankB, false);
                var pA = videoA.play();
                var pB = videoB.play();
                if (pA && pA.catch) pA.catch(function() {});
                if (pB && pB.catch) pB.catch(function() {});
            }
        }

        function stopPlayback() {
            if (delayTimerA) { clearTimeout(delayTimerA); delayTimerA = null; }
            if (delayTimerB) { clearTimeout(delayTimerB); delayTimerB = null; }
            videoA.pause();
            videoB.pause();
        }

        var hasStartedOnce = false;
        function resumePlayback() {
            // First play or both ended: use full synced start with delay logic
            if (!hasStartedOnce || (endedA && endedB)) {
                hasStartedOnce = true;
                startSyncedPlayback();
                return;
            }
            // Resume from current position without resetting to beginning
            if (!endedA) { var p = videoA.play(); if (p && p.catch) p.catch(function(){}); }
            if (!endedB) { var p = videoB.play(); if (p && p.catch) p.catch(function(){}); }
        }

        // Synchronized play/pause
        playPauseBtn.addEventListener('click', function() {
            if (isPlaying) {
                stopPlayback();
                isPlaying = false;
                setPlayIcon();
                cancelAnimationFrame(animFrame);
            } else {
                isPlaying = true;
                setPauseIcon();
                resumePlayback();
                updateOverlays();
            }
        });

        // When a video ends: show blank, check if both ended → restart cycle
        videoA.addEventListener('ended', function() {
            endedA = true;
            showBlank(blankA, true);
            // Clear canvas
            var ctxA = canvasA.getContext('2d');
            if (ctxA) ctxA.clearRect(0, 0, canvasA.width, canvasA.height);
            if (endedB) {
                // Both done — restart after brief pause
                setTimeout(function() {
                    if (isPlaying) startSyncedPlayback();
                }, 1000);
            }
        });
        videoB.addEventListener('ended', function() {
            endedB = true;
            showBlank(blankB, true);
            var ctxB = canvasB.getContext('2d');
            if (ctxB) ctxB.clearRect(0, 0, canvasB.width, canvasB.height);
            if (endedA) {
                setTimeout(function() {
                    if (isPlaying) startSyncedPlayback();
                }, 1000);
            }
        });

        // Close button — restore static view
        closeBtn.addEventListener('click', function() {
            cancelAnimationFrame(animFrame);
            stopPlayback();
            card.classList.remove('cc-video-active');
            self.renderCrossCameraCard(pred);
        });

        // ── BBox Correction Mode ────────────────────────────────
        var correctBtn = syncControls.querySelector('#cc-correct-bbox');
        var correctionMode = false;
        var correctionCam = null; // 'A' or 'B'
        var correctedBbox = null; // {cx, cy, w, h} in canvas coords (center + dims)
        var correctionOverlay = null;

        // Compute video layout metrics (object-fit: contain offsets + scale)
        function getVideoLayout(canvas, video, camData) {
            var elemW = video.clientWidth;
            var elemH = video.clientHeight;
            var vidW = video.videoWidth || (camData.video_width || 1920);
            var vidH = video.videoHeight || (camData.video_height || 1080);
            var videoAspect = vidW / vidH;
            var elemAspect = elemW / elemH;
            var renderW, renderH, offsetX, offsetY;
            if (videoAspect > elemAspect) {
                renderW = elemW; renderH = elemW / videoAspect; offsetX = 0; offsetY = (elemH - renderH) / 2;
            } else {
                renderH = elemH; renderW = elemH * videoAspect; offsetX = (elemW - renderW) / 2; offsetY = 0;
            }
            var srcW = camData.video_width || vidW;
            var srcH = camData.video_height || vidH;
            return { elemW: elemW, elemH: elemH, renderW: renderW, renderH: renderH,
                     offsetX: offsetX, offsetY: offsetY, srcW: srcW, srcH: srcH,
                     scaleX: renderW / srcW, scaleY: renderH / srcH };
        }

        function enterCorrectionMode() {
            if (correctionMode) return;
            correctionMode = true;
            if (isPlaying) {
                stopPlayback();
                isPlaying = false;
                setPlayIcon();
                cancelAnimationFrame(animFrame);
            }
            // Disable header so it doesn't block touches on the top of the video
            var hdr = document.querySelector('.review-header');
            if (hdr) hdr.style.pointerEvents = 'none';
            [playerA.wrapper, playerB.wrapper].forEach(function(wrap, idx) {
                var overlay = document.createElement('div');
                overlay.className = 'cc-correct-overlay';
                overlay.innerHTML = '<span class="cc-correct-hint">Tap to correct ' +
                    (idx === 0 ? clipData.camera_a.camera_id : clipData.camera_b.camera_id) + '</span>';
                overlay.dataset.cam = idx === 0 ? 'A' : 'B';
                overlay.addEventListener('click', function() {
                    if (correctionCam) return;
                    selectCorrectionCam(overlay.dataset.cam);
                });
                wrap.appendChild(overlay);
            });
            correctionOverlay = document.createElement('div');
            correctionOverlay.className = 'cc-correct-bar';
            correctionOverlay.innerHTML = '<span class="cc-correct-bar-label">Tap a video to correct</span>' +
                '<button class="cc-correct-wrongveh-btn" id="cc-correct-wrongveh" style="display:none">Not this vehicle</button>' +
                '<button class="cc-correct-save-btn" id="cc-correct-save" style="display:none">Save</button>' +
                '<button class="cc-correct-cancel-btn" id="cc-correct-cancel">Cancel</button>';
            syncControls.parentElement.insertBefore(correctionOverlay, syncControls.nextSibling);
            correctionOverlay.querySelector('#cc-correct-cancel').addEventListener('click', exitCorrectionMode);
            correctionOverlay.querySelector('#cc-correct-save').addEventListener('click', saveBboxCorrection);
            correctionOverlay.querySelector('#cc-correct-wrongveh').addEventListener('click', flagWrongVehicle);
            correctBtn.classList.add('active');
        }

        function selectCorrectionCam(cam) {
            correctionCam = cam;
            var overlays = document.querySelectorAll('.cc-correct-overlay');
            overlays.forEach(function(o) {
                if (o.dataset.cam === cam) {
                    o.style.display = 'none'; // Hide overlay in fullscreen
                } else {
                    o.style.display = 'none';
                }
            });
            var targetCanvas = cam === 'A' ? canvasA : canvasB;
            var targetVideo = cam === 'A' ? videoA : videoB;
            var targetCamData = cam === 'A' ? clipData.camera_a : clipData.camera_b;
            var targetPlayerWrap = targetCanvas.parentElement;

            // Go fullscreen — neutralize all ancestors that could create a
            // containing block (will-change, transform, overflow, max-width)
            // which would clip or constrain position:fixed
            var _fsAncestorRestores = [];
            var ancestor = targetPlayerWrap.parentElement;
            while (ancestor && ancestor !== document.body) {
                var saved = {
                    el: ancestor,
                    overflow: ancestor.style.overflow,
                    willChange: ancestor.style.willChange,
                    transform: ancestor.style.transform,
                    maxWidth: ancestor.style.maxWidth,
                    maxHeight: ancestor.style.maxHeight,
                    contain: ancestor.style.contain
                };
                _fsAncestorRestores.push(saved);
                ancestor.style.overflow = 'visible';
                ancestor.style.willChange = 'auto';
                ancestor.style.transform = 'none';
                ancestor.style.maxWidth = 'none';
                ancestor.style.maxHeight = 'none';
                ancestor.style.contain = 'none';
                ancestor = ancestor.parentElement;
            }
            targetPlayerWrap._fsAncestorRestores = _fsAncestorRestores;
            targetPlayerWrap.classList.add('cc-correction-fullscreen');
            if (correctionOverlay) correctionOverlay.classList.add('cc-fullscreen-bar');

            targetCanvas.style.pointerEvents = 'auto';
            targetCanvas.style.touchAction = 'none';
            targetCanvas.style.zIndex = '10001';

            // Defer bbox init until layout recalculates for fullscreen dimensions
            setTimeout(function() {
                var normalGap = cam === 'A' ? normalGapA : normalGapB;
                var aiBbox = getBboxAtTime(targetCamData, targetVideo.currentTime, normalGap);
                if (aiBbox) {
                    var layout = getVideoLayout(targetCanvas, targetVideo, targetCamData);
                    var cx = (aiBbox.x + aiBbox.width / 2) * layout.scaleX + layout.offsetX;
                    var cy = (aiBbox.y + aiBbox.height / 2) * layout.scaleY + layout.offsetY;
                    correctedBbox = { cx: cx, cy: cy, w: aiBbox.width * layout.scaleX, h: aiBbox.height * layout.scaleY };
                } else {
                    correctedBbox = { cx: targetVideo.clientWidth / 2, cy: targetVideo.clientHeight / 2, w: 80, h: 60 };
                }

                setupDragging(targetCanvas, targetVideo, targetCamData);
                drawCorrectionPreview(targetCanvas, targetVideo, targetCamData);

                if (correctionOverlay) {
                    correctionOverlay.querySelector('.cc-correct-bar-label').textContent =
                        'Drag bbox on ' + targetCamData.camera_id;
                    var saveBtn = correctionOverlay.querySelector('#cc-correct-save');
                    if (saveBtn) saveBtn.style.display = 'inline-block';
                    var wrongVehBtn = correctionOverlay.querySelector('#cc-correct-wrongveh');
                    if (wrongVehBtn) wrongVehBtn.style.display = 'inline-block';
                }
            }, 60);
        }

        function setupDragging(canvas, video, camData) {
            var mode = null; // 'move', 'resize-tl', 'resize-tr', 'resize-bl', 'resize-br', 'resize-l', 'resize-r', 'resize-t', 'resize-b'
            var dragOffX = 0, dragOffY = 0;
            var HANDLE = 18; // hit zone for corners/edges (px, generous for touch)

            function getPos(e) {
                var cr = canvas.getBoundingClientRect();
                var t = e.touches ? e.touches[0] : e;
                return { x: t.clientX - cr.left, y: t.clientY - cr.top };
            }

            function hitTest(pos) {
                if (!correctedBbox) return 'move';
                var l = correctedBbox.cx - correctedBbox.w / 2;
                var r = correctedBbox.cx + correctedBbox.w / 2;
                var t = correctedBbox.cy - correctedBbox.h / 2;
                var b = correctedBbox.cy + correctedBbox.h / 2;
                // Always use full HANDLE zone — for small bboxes handles extend outside
                var hx = HANDLE;
                var hy = HANDLE;
                var nearL = Math.abs(pos.x - l) < hx;
                var nearR = Math.abs(pos.x - r) < hx;
                var nearT = Math.abs(pos.y - t) < hy;
                var nearB = Math.abs(pos.y - b) < hy;
                // Corners first (priority) — always checked before move
                if (nearT && nearL) return 'resize-tl';
                if (nearT && nearR) return 'resize-tr';
                if (nearB && nearL) return 'resize-bl';
                if (nearB && nearR) return 'resize-br';
                // Edges
                if (nearL && pos.y > t - hy && pos.y < b + hy) return 'resize-l';
                if (nearR && pos.y > t - hy && pos.y < b + hy) return 'resize-r';
                if (nearT && pos.x > l - hx && pos.x < r + hx) return 'resize-t';
                if (nearB && pos.x > l - hx && pos.x < r + hx) return 'resize-b';
                return 'move';
            }

            function onStart(e) {
                if (!correctionMode || !correctionCam || !correctedBbox) return;
                e.preventDefault();
                e.stopPropagation();
                var pos = getPos(e);
                mode = hitTest(pos);
                dragOffX = pos.x - correctedBbox.cx;
                dragOffY = pos.y - correctedBbox.cy;
            }
            function onMove(e) {
                if (!mode || !correctedBbox) return;
                e.preventDefault();
                var pos = getPos(e);
                var l = correctedBbox.cx - correctedBbox.w / 2;
                var r = correctedBbox.cx + correctedBbox.w / 2;
                var t = correctedBbox.cy - correctedBbox.h / 2;
                var b = correctedBbox.cy + correctedBbox.h / 2;
                var MIN = 20; // minimum bbox dimension in canvas px

                if (mode === 'move') {
                    correctedBbox.cx = pos.x - dragOffX;
                    correctedBbox.cy = pos.y - dragOffY;
                } else if (mode === 'resize-tl') {
                    var newL = pos.x, newT = pos.y;
                    if (r - newL > MIN) { correctedBbox.w = r - newL; correctedBbox.cx = newL + correctedBbox.w / 2; }
                    if (b - newT > MIN) { correctedBbox.h = b - newT; correctedBbox.cy = newT + correctedBbox.h / 2; }
                } else if (mode === 'resize-tr') {
                    var newR = pos.x, newT2 = pos.y;
                    if (newR - l > MIN) { correctedBbox.w = newR - l; correctedBbox.cx = l + correctedBbox.w / 2; }
                    if (b - newT2 > MIN) { correctedBbox.h = b - newT2; correctedBbox.cy = newT2 + correctedBbox.h / 2; }
                } else if (mode === 'resize-bl') {
                    var newL2 = pos.x, newB = pos.y;
                    if (r - newL2 > MIN) { correctedBbox.w = r - newL2; correctedBbox.cx = newL2 + correctedBbox.w / 2; }
                    if (newB - t > MIN) { correctedBbox.h = newB - t; correctedBbox.cy = t + correctedBbox.h / 2; }
                } else if (mode === 'resize-br') {
                    var newR2 = pos.x, newB2 = pos.y;
                    if (newR2 - l > MIN) { correctedBbox.w = newR2 - l; correctedBbox.cx = l + correctedBbox.w / 2; }
                    if (newB2 - t > MIN) { correctedBbox.h = newB2 - t; correctedBbox.cy = t + correctedBbox.h / 2; }
                } else if (mode === 'resize-l') {
                    if (r - pos.x > MIN) { correctedBbox.w = r - pos.x; correctedBbox.cx = pos.x + correctedBbox.w / 2; }
                } else if (mode === 'resize-r') {
                    if (pos.x - l > MIN) { correctedBbox.w = pos.x - l; correctedBbox.cx = l + correctedBbox.w / 2; }
                } else if (mode === 'resize-t') {
                    if (b - pos.y > MIN) { correctedBbox.h = b - pos.y; correctedBbox.cy = pos.y + correctedBbox.h / 2; }
                } else if (mode === 'resize-b') {
                    if (pos.y - t > MIN) { correctedBbox.h = pos.y - t; correctedBbox.cy = t + correctedBbox.h / 2; }
                }
                drawCorrectionPreview(canvas, video, camData);
            }
            function onEnd() {
                mode = null;
            }

            canvas.addEventListener('pointerdown', onStart);
            canvas.addEventListener('pointermove', onMove);
            canvas.addEventListener('pointerup', onEnd);
            canvas.addEventListener('pointercancel', onEnd);
            canvas._correctionCleanup = function() {
                canvas.removeEventListener('pointerdown', onStart);
                canvas.removeEventListener('pointermove', onMove);
                canvas.removeEventListener('pointerup', onEnd);
                canvas.removeEventListener('pointercancel', onEnd);
            };
        }

        function drawCorrectionPreview(canvas, video, camData) {
            var ctx = canvas.getContext('2d');
            if (!ctx) return;
            // Match canvas exactly to video element (fixes top-portion issue)
            var elemW = video.clientWidth;
            var elemH = video.clientHeight;
            if (!elemW || !elemH) return;
            canvas.width = elemW;
            canvas.height = elemH;
            canvas.style.left = '0';
            canvas.style.top = '0';
            canvas.style.width = elemW + 'px';
            canvas.style.height = elemH + 'px';
            ctx.clearRect(0, 0, canvas.width, canvas.height);

            var layout = getVideoLayout(canvas, video, camData);
            var normalGap = correctionCam === 'A' ? normalGapA : normalGapB;
            var aiBbox = getBboxAtTime(camData, video.currentTime, normalGap);

            // Draw original AI bbox in red dashed (stays in place as reference)
            if (aiBbox) {
                var bx = aiBbox.x * layout.scaleX + layout.offsetX;
                var by = aiBbox.y * layout.scaleY + layout.offsetY;
                var bw = aiBbox.width * layout.scaleX;
                var bh = aiBbox.height * layout.scaleY;
                ctx.strokeStyle = 'rgba(255, 60, 60, 0.7)';
                ctx.lineWidth = 2;
                ctx.setLineDash([4, 4]);
                ctx.strokeRect(bx, by, bw, bh);
                ctx.setLineDash([]);
                ctx.fillStyle = 'rgba(255, 60, 60, 0.6)';
                ctx.font = '11px sans-serif';
                ctx.fillRect(bx, by - 14, ctx.measureText('AI bbox').width + 6, 14);
                ctx.fillStyle = '#fff';
                ctx.fillText('AI bbox', bx + 3, by - 3);
            }

            // Draw the draggable corrected bbox in cyan
            if (correctedBbox) {
                var rx = correctedBbox.cx - correctedBbox.w / 2;
                var ry = correctedBbox.cy - correctedBbox.h / 2;
                ctx.strokeStyle = '#00DDFF';
                ctx.lineWidth = 2.5;
                ctx.strokeRect(rx, ry, correctedBbox.w, correctedBbox.h);
                ctx.fillStyle = 'rgba(0, 221, 255, 0.15)';
                ctx.fillRect(rx, ry, correctedBbox.w, correctedBbox.h);
                ctx.fillStyle = 'rgba(0, 221, 255, 0.8)';
                ctx.font = '11px sans-serif';
                ctx.fillRect(rx, ry - 14, ctx.measureText('Corrected').width + 6, 14);
                ctx.fillStyle = '#000';
                ctx.fillText('Corrected', rx + 3, ry - 3);
                // Corner resize handles — larger for small bboxes
                var isSmall = correctedBbox.w < 60 || correctedBbox.h < 60;
                var hs = isSmall ? 8 : 5; // handle half-size
                ctx.fillStyle = '#00DDFF';
                var corners = [
                    [rx, ry], [rx + correctedBbox.w, ry],
                    [rx, ry + correctedBbox.h], [rx + correctedBbox.w, ry + correctedBbox.h]
                ];
                for (var ci = 0; ci < corners.length; ci++) {
                    ctx.fillRect(corners[ci][0] - hs, corners[ci][1] - hs, hs * 2, hs * 2);
                }
                // Edge midpoint handles (smaller)
                var ehs = 3;
                ctx.fillRect(rx + correctedBbox.w / 2 - ehs, ry - ehs, ehs * 2, ehs * 2);
                ctx.fillRect(rx + correctedBbox.w / 2 - ehs, ry + correctedBbox.h - ehs, ehs * 2, ehs * 2);
                ctx.fillRect(rx - ehs, ry + correctedBbox.h / 2 - ehs, ehs * 2, ehs * 2);
                ctx.fillRect(rx + correctedBbox.w - ehs, ry + correctedBbox.h / 2 - ehs, ehs * 2, ehs * 2);
                // Crosshair at center to indicate draggable
                ctx.strokeStyle = 'rgba(0, 221, 255, 0.4)';
                ctx.lineWidth = 1;
                ctx.beginPath();
                ctx.moveTo(correctedBbox.cx - 8, correctedBbox.cy);
                ctx.lineTo(correctedBbox.cx + 8, correctedBbox.cy);
                ctx.moveTo(correctedBbox.cx, correctedBbox.cy - 8);
                ctx.lineTo(correctedBbox.cx, correctedBbox.cy + 8);
                ctx.stroke();
            }
        }

        function saveBboxCorrection() {
            if (!correctedBbox || !correctionCam) return;
            var video = correctionCam === 'A' ? videoA : videoB;
            var camData = correctionCam === 'A' ? clipData.camera_a : clipData.camera_b;
            var canvas = correctionCam === 'A' ? canvasA : canvasB;
            var layout = getVideoLayout(canvas, video, camData);
            var normalGap = correctionCam === 'A' ? normalGapA : normalGapB;
            var aiBbox = getBboxAtTime(camData, video.currentTime, normalGap);

            // Convert corrected bbox from canvas coords back to video coords
            var correctedVideoX = Math.round((correctedBbox.cx - correctedBbox.w / 2 - layout.offsetX) / layout.scaleX);
            var correctedVideoY = Math.round((correctedBbox.cy - correctedBbox.h / 2 - layout.offsetY) / layout.scaleY);
            var correctedVideoW = Math.round(correctedBbox.w / layout.scaleX);
            var correctedVideoH = Math.round(correctedBbox.h / layout.scaleY);

            var payload = {
                type: 'bbox_correction',
                camera_id: camData.camera_id,
                video_track_id: camData.video_track_id,
                clip_url: camData.clip_url,
                video_time: video.currentTime,
                video_duration: video.duration || 0,
                video_width: layout.srcW,
                video_height: layout.srcH,
                original_bbox: aiBbox ? { x: aiBbox.x, y: aiBbox.y, w: aiBbox.width, h: aiBbox.height, projected: aiBbox.projected } : null,
                corrected_bbox: { x: correctedVideoX, y: correctedVideoY, w: correctedVideoW, h: correctedVideoH },
                class_name: camData.class_name,
                cross_camera_link_id: pred._ccLink ? pred._ccLink.id : pred.id,
                timestamp: new Date().toISOString()
            };

            fetch('/api/ai/feedback/bbox-correction', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-Auth-Role': 'user' },
                body: JSON.stringify(payload)
            }).then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.success) {
                    self._showToast('Correction saved', '#4CAF50');
                } else {
                    self._showToast('Error: ' + (data.error || 'unknown'), '#f44336');
                }
            }).catch(function(err) {
                self._showToast('Save failed', '#f44336');
            });

            exitCorrectionMode();
        }

        function flagWrongVehicle() {
            if (!correctionCam) return;
            var video = correctionCam === 'A' ? videoA : videoB;
            var camData = correctionCam === 'A' ? clipData.camera_a : clipData.camera_b;
            var canvas = correctionCam === 'A' ? canvasA : canvasB;
            var layout = getVideoLayout(canvas, video, camData);
            var normalGap = correctionCam === 'A' ? normalGapA : normalGapB;
            var aiBbox = getBboxAtTime(camData, video.currentTime, normalGap);

            var payload = {
                type: 'wrong_vehicle',
                camera_id: camData.camera_id,
                video_track_id: camData.video_track_id,
                clip_url: camData.clip_url,
                video_time: video.currentTime,
                video_duration: video.duration || 0,
                video_width: layout.srcW,
                video_height: layout.srcH,
                original_bbox: aiBbox ? { x: aiBbox.x, y: aiBbox.y, w: aiBbox.width, h: aiBbox.height, projected: aiBbox.projected } : null,
                corrected_bbox: null,
                class_name: camData.class_name,
                cross_camera_link_id: pred._ccLink ? pred._ccLink.id : pred.id,
                timestamp: new Date().toISOString()
            };

            fetch('/api/ai/feedback/bbox-correction', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-Auth-Role': 'user' },
                body: JSON.stringify(payload)
            }).then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.success) {
                    self._showToast('Flagged as wrong vehicle', '#FF9800');
                } else {
                    self._showToast('Error: ' + (data.error || 'unknown'), '#f44336');
                }
            }).catch(function(err) {
                self._showToast('Flag failed', '#f44336');
            });

            exitCorrectionMode();
        }

        function exitCorrectionMode() {
            correctionMode = false;
            correctionCam = null;
            correctedBbox = null;
            // Remove fullscreen and restore all ancestor styles
            document.querySelectorAll('.cc-correction-fullscreen').forEach(function(el) {
                if (el._fsAncestorRestores) {
                    el._fsAncestorRestores.forEach(function(saved) {
                        saved.el.style.overflow = saved.overflow;
                        saved.el.style.willChange = saved.willChange;
                        saved.el.style.transform = saved.transform;
                        saved.el.style.maxWidth = saved.maxWidth;
                        saved.el.style.maxHeight = saved.maxHeight;
                        saved.el.style.contain = saved.contain;
                    });
                    el._fsAncestorRestores = null;
                }
                el.classList.remove('cc-correction-fullscreen');
            });
            document.querySelectorAll('.cc-correct-overlay').forEach(function(o) { o.remove(); });
            if (correctionOverlay) {
                correctionOverlay.classList.remove('cc-fullscreen-bar');
                correctionOverlay.remove();
                correctionOverlay = null;
            }
            // Restore header pointer events
            var hdr = document.querySelector('.review-header');
            if (hdr) hdr.style.pointerEvents = '';
            [canvasA, canvasB].forEach(function(c) {
                if (c._correctionCleanup) { c._correctionCleanup(); c._correctionCleanup = null; }
                c.style.pointerEvents = 'none';
                c.style.touchAction = '';
                c.style.zIndex = '';
            });
            correctBtn.classList.remove('active');
            // Redraw normal bboxes after layout restores
            setTimeout(function() {
                var bboxA = getBboxAtTime(clipData.camera_a, videoA.currentTime, normalGapA);
                var bboxB = getBboxAtTime(clipData.camera_b, videoB.currentTime, normalGapB);
                drawBbox(canvasA, clipData.camera_a, videoA, bboxA);
                drawBbox(canvasB, clipData.camera_b, videoB, bboxB);
            }, 60);
        }

        if (correctBtn) {
            correctBtn.addEventListener('click', function() {
                if (correctionMode) {
                    exitCorrectionMode();
                } else {
                    enterCorrectionMode();
                }
            });
        }

        // Draw initial bbox when videos load metadata
        videoA.addEventListener('loadedmetadata', function() {
            var bbox = getBboxAtTime(clipData.camera_a, 0, normalGapA);
            drawBbox(canvasA, clipData.camera_a, videoA, bbox);
        });
        videoB.addEventListener('loadedmetadata', function() {
            var bbox = getBboxAtTime(clipData.camera_b, 0, normalGapB);
            drawBbox(canvasB, clipData.camera_b, videoB, bbox);
        });

        // Auto-play after both videos are ready (one-shot)
        var readyA = false, readyB = false;
        var autoStarted = false;
        function checkBothReady() {
            if (readyA && readyB && !autoStarted) {
                autoStarted = true;
                playPauseBtn.click();
            }
        }
        videoA.addEventListener('canplay', function onCanPlayA() {
            videoA.removeEventListener('canplay', onCanPlayA);
            readyA = true;
            checkBothReady();
        });
        videoB.addEventListener('canplay', function onCanPlayB() {
            videoB.removeEventListener('canplay', onCanPlayB);
            readyB = true;
            checkBothReady();
        });
    },

    _createVideoSection: function(camData, label) {
        var wrapper = document.createElement('div');
        wrapper.className = 'cc-video-section';

        // Camera label
        var badge = document.createElement('div');
        badge.className = 'cc-camera-badge';
        badge.textContent = (camData && camData.camera_id) || ('Camera ' + label);
        wrapper.appendChild(badge);

        var playerWrap = document.createElement('div');
        playerWrap.className = 'cc-player-wrap';

        if (camData && camData.has_clip && camData.clip_url) {
            // Loading progress bar
            var progressWrap = document.createElement('div');
            progressWrap.className = 'cc-loading-bar-wrap';
            var progressBar = document.createElement('div');
            progressBar.className = 'cc-loading-bar';
            var progressText = document.createElement('span');
            progressText.className = 'cc-loading-text';
            progressText.textContent = 'Loading clip...';
            progressWrap.appendChild(progressBar);
            progressWrap.appendChild(progressText);
            playerWrap.appendChild(progressWrap);

            // Video element
            var video = document.createElement('video');
            video.className = 'cc-video';
            video.src = camData.clip_url;
            video.preload = 'auto';
            video.playsInline = true;
            video.muted = true;
            video.loop = false;

            // Update progress bar as video buffers
            video.addEventListener('progress', function() {
                if (video.buffered.length > 0 && video.duration) {
                    var pct = (video.buffered.end(video.buffered.length - 1) / video.duration) * 100;
                    progressBar.style.width = pct + '%';
                    progressText.textContent = Math.round(pct) + '%';
                }
            });
            video.addEventListener('canplay', function() {
                progressWrap.style.display = 'none';
            });

            playerWrap.appendChild(video);

            // Canvas overlay for bbox
            var canvas = document.createElement('canvas');
            canvas.className = 'cc-bbox-canvas';
            playerWrap.appendChild(canvas);

            wrapper.appendChild(playerWrap);
            return { wrapper: wrapper, video: video, canvas: canvas };
        } else {
            // No clip available — show placeholder
            var placeholder = document.createElement('div');
            placeholder.className = 'cc-no-clip';
            placeholder.textContent = 'Video not available';
            playerWrap.appendChild(placeholder);
            wrapper.appendChild(playerWrap);
            // Create dummy video/canvas to prevent errors
            var dummyVideo = document.createElement('video');
            var dummyCanvas = document.createElement('canvas');
            return { wrapper: wrapper, video: dummyVideo, canvas: dummyCanvas };
        }
    },

    renderClusterCard: function(pred) {
        if (!this.els.cardContainer) return;
        var card = document.createElement('div');
        card.className = 'review-card cluster-card';
        card.style.willChange = 'transform';

        // Sample image (annotated frame from representative prediction)
        var sampleWrap = document.createElement('div');
        sampleWrap.className = 'cluster-sample-image';
        var sampleId = (pred.members && pred.members.length > 0) ? pred.members[0].id : null;
        if (sampleId) {
            var img = document.createElement('img');
            img.className = 'cluster-sample-img';
            img.src = '/thumbnails/annotated/' + sampleId;
            img.alt = 'Cluster sample';
            img.onerror = function() { this.src = '/thumbnails/crop/' + sampleId; };
            sampleWrap.appendChild(img);
        }

        // Badges overlay
        var badges = document.createElement('div');
        badges.className = 'cluster-badges';
        var camBadge = document.createElement('span');
        camBadge.className = 'cluster-camera-badge';
        camBadge.textContent = pred.camera_id || 'Unknown Camera';
        badges.appendChild(camBadge);
        var countBadge = document.createElement('span');
        countBadge.className = 'cluster-count-badge';
        countBadge.textContent = (pred.count || 0) + ' detections';
        badges.appendChild(countBadge);
        sampleWrap.appendChild(badges);

        // Class badges (bottom-left)
        if (pred.yolo_classes) {
            var classBadges = document.createElement('div');
            classBadges.className = 'cluster-class-badges';
            var classes = typeof pred.yolo_classes === 'string' ? pred.yolo_classes.split(',') : (Array.isArray(pred.yolo_classes) ? pred.yolo_classes : []);
            for (var i = 0; i < Math.min(classes.length, 3); i++) {
                var cChip = document.createElement('span');
                cChip.className = 'cluster-class-chip';
                cChip.textContent = classes[i].trim();
                classBadges.appendChild(cChip);
            }
            sampleWrap.appendChild(classBadges);
        }

        // Confidence badge (top-right)
        var confPct = Math.round((pred.avg_confidence || 0) * 100);
        var confBadge = document.createElement('span');
        confBadge.className = 'cluster-conf-badge';
        if (confPct >= 70) confBadge.style.cssText = 'background:rgba(22,163,74,0.7);color:#fff;';
        else if (confPct >= 40) confBadge.style.cssText = 'background:rgba(234,179,8,0.7);color:#fff;';
        else confBadge.style.cssText = 'background:rgba(220,38,38,0.7);color:#fff;';
        confBadge.textContent = confPct + '%';
        sampleWrap.appendChild(confBadge);

        // VLM hint if available
        if (pred.members && pred.members.length > 0 && pred.members[0].vlm_class) {
            var vlmHint = document.createElement('span');
            vlmHint.className = 'cluster-vlm-hint';
            vlmHint.textContent = 'VLM: ' + pred.members[0].vlm_class;
            sampleWrap.appendChild(vlmHint);
        }

        card.appendChild(sampleWrap);

        // Thumbnail strip (up to 6 member crops)
        if (pred.members && pred.members.length > 1) {
            var strip = document.createElement('div');
            strip.className = 'cluster-thumbnails';
            var maxThumbs = Math.min(pred.members.length, 6);
            for (var j = 0; j < maxThumbs; j++) {
                var thumb = document.createElement('img');
                thumb.className = 'cluster-thumb';
                thumb.src = '/thumbnails/crop/' + pred.members[j].id;
                thumb.alt = 'Member ' + (j + 1);
                thumb.onerror = function() { this.style.display = 'none'; };
                strip.appendChild(thumb);
            }
            if (pred.members.length > 6) {
                var more = document.createElement('div');
                more.className = 'cluster-thumb';
                more.style.cssText = 'display:flex;align-items:center;justify-content:center;background:var(--color-surface-elevated);color:var(--color-text-secondary);font-size:12px;font-weight:600;';
                more.textContent = '+' + (pred.members.length - 6);
                strip.appendChild(more);
            }
            card.appendChild(strip);
        }

        this.els.cardContainer.appendChild(card);

        // Update metadata strip
        if (this.els.predClass) this.els.predClass.textContent = 'Cluster';
        if (this.els.predConfidence) this.els.predConfidence.textContent = confPct + '%';
        if (this.els.predModel) this.els.predModel.textContent = (pred.count || 0) + ' items';
        if (this.els.reviewGuidance) this.els.reviewGuidance.textContent = 'Approve all detections in this cluster, or reject to reclassify.';
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

    _restoreIndexFromHash: function() {
        var hash = window.location.hash;
        if (!hash || hash.length < 2 || !this.predictions.length) return;
        // Format: #cross_camera:3767 or #prediction:1234
        var parts = hash.substring(1).split(':');
        if (parts.length !== 2) return;
        var targetId = parseInt(parts[1], 10);
        if (isNaN(targetId)) return;
        for (var i = 0; i < this.predictions.length; i++) {
            if (this.predictions[i].id === targetId) {
                this.currentIndex = i;
                return;
            }
        }
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

        // Persist current item in URL hash for refresh resilience
        if (pred && pred.id) {
            var hashType = pred._reviewType || 'prediction';
            history.replaceState(null, '', '#' + hashType + ':' + pred.id);
        }
        this.els.cardContainer.textContent = '';

        // Dispatch to type-specific renderer
        if (pred._reviewType === 'cross_camera') {
            this.renderCrossCameraCard(pred);
            this.updateActionZone();
            return;
        }
        if (pred._reviewType === 'cluster') {
            this.renderClusterCard(pred);
            this.updateActionZone();
            return;
        }

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

        // VLM review badge
        var _ct = pred.corrected_tags || {};
        if (typeof _ct === 'string') { try { _ct = JSON.parse(_ct); } catch(e) { _ct = {}; } }
        if (_ct.vlm_model) {
            var vlmBadge = document.createElement('div');
            vlmBadge.className = 'vlm-badge';
            var vlmConf = _ct.vlm_confidence ? Math.round(_ct.vlm_confidence * 100) + '%' : '';
            vlmBadge.textContent = 'AI' + (vlmConf ? ' ' + vlmConf : '');
            card.appendChild(vlmBadge);
        }

        // Swipe label overlays
        var approveLabel = document.createElement('div');
        approveLabel.className = 'swipe-label swipe-label-approve';
        var _ct = pred.corrected_tags || {};
        if (typeof _ct === 'string') { try { _ct = JSON.parse(_ct); } catch(e) { _ct = {}; } }
        if (_ct.needs_negative_review && _ct.actual_class) {
            approveLabel.textContent = _ct.actual_class.toUpperCase();
        } else {
            approveLabel.textContent = 'APPROVE';
        }
        card.appendChild(approveLabel);

        var rejectLabel = document.createElement('div');
        rejectLabel.className = 'swipe-label swipe-label-reject';
        var _isFaceScenario = pred.scenario === 'face_detection' || pred.scenario === 'person_identification';
        rejectLabel.textContent = _isFaceScenario ? 'RECLASSIFY' : 'REJECT';
        card.appendChild(rejectLabel);

        var skipLabel = document.createElement('div');
        skipLabel.className = 'swipe-label swipe-label-skip';
        skipLabel.textContent = 'RECLASSIFY';
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
                if (tags['class']) return tags['class'];
                if (tags.label) return tags.label;
            }
        }
        if (pred.scenario) return pred.scenario.replace(/_/g, ' ');
        return '';
    },

    updateMetadata: function(pred) {
        if (this.els.predClass) {
            var cls = this.extractClassName(pred);
            var originalClass = cls;
            this.els.predClass.textContent = cls || 'Detection';
            var color = this.getScenarioColor(pred.scenario);
            this.els.predClass.style.backgroundColor = color;
            this.els.predClass.style.color = '#fff';

            // Override: show the class being voted on (VLM suggestion or reclassification)
            var _vlm = pred.corrected_tags || {};
            if (typeof _vlm === 'string') { try { _vlm = JSON.parse(_vlm); } catch(e) { _vlm = {}; } }
            var votingClass = _vlm.actual_class || _vlm.vlm_suggested_class;
            if (votingClass && votingClass.toLowerCase() !== (cls || '').toLowerCase()) {
                this.els.predClass.textContent = votingClass;
                this.els.predClass.style.backgroundColor = '#D97706';
                // Show original YOLO class as small secondary label
                var origLabel = document.getElementById('orig-class-label');
                if (!origLabel) {
                    origLabel = document.createElement('span');
                    origLabel.id = 'orig-class-label';
                    origLabel.style.cssText = 'font-size:0.7em;opacity:0.6;margin-left:6px;text-decoration:line-through;';
                    this.els.predClass.parentNode.insertBefore(origLabel, this.els.predClass.nextSibling);
                }
                origLabel.textContent = originalClass;
                origLabel.style.display = '';
            } else {
                var origLabel = document.getElementById('orig-class-label');
                if (origLabel) origLabel.style.display = 'none';
            }
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
            // For vehicle detection, include the detected class
            if (scenario === 'vehicle_detection') {
                var vtags = pred.predicted_tags || {};
                if (typeof vtags === 'string') { try { vtags = JSON.parse(vtags); } catch(e) { vtags = {}; } }
                var vclass = vtags['class'] || vtags.vehicle_type;
                if (vclass) {
                    guidance = 'Detected as "' + vclass + '". Is this correct? Approve to confirm, reject if wrong.';
                }
            }
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
            // Reclassification guidance: show what we're voting on
            // In classify mode, keep the classify guidance and append VLM suggestion if available
            var _ctags = pred.corrected_tags || {};
            if (typeof _ctags === 'string') { try { _ctags = JSON.parse(_ctags); } catch(e) { _ctags = {}; } }
            var voteCls = _ctags.actual_class || _ctags.vlm_suggested_class;
            if (voteCls) {
                if (this.classifyMode) {
                    // Append VLM suggestion without replacing classify guidance
                    guidance += ' VLM suggests: "' + voteCls + '".';
                } else {
                    var origTags = pred.predicted_tags || {};
                    if (typeof origTags === 'string') { try { origTags = JSON.parse(origTags); } catch(e) { origTags = {}; } }
                    var origCls = origTags['class'] || origTags.class_name || '';
                    var reason = _ctags.vlm_reasoning || '';
                    guidance = 'Originally "' + origCls + '". Is this actually "' + voteCls + '"?';
                    if (reason) guidance += ' (' + reason + ')';
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

        // Don't intercept clicks on buttons (e.g. Play Video)
        if (e.target.closest && e.target.closest('button, a, .cc-play-btn, .cc-timeline-wrap, .cc-correct-overlay, .cc-correct-bar, .cc-bbox-canvas')) return;
        if (e.target.tagName === 'BUTTON' || e.target.tagName === 'A') return;

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
                var _swipePred = this.predictions[this.currentIndex];
                var _isFaceSwipe = _swipePred && (_swipePred.scenario === 'face_detection' || _swipePred.scenario === 'person_identification');
                if (this.speedMode && !_isFaceSwipe) {
                    this.commitAction('reject');
                } else {
                    this.commitAction('reject', null, true);
                    this.showRejectSheet();
                }
            }
        } else if (this.touch.direction === 'vertical' && deltaY < -100) {
            this.animateCardExit('up');
            this.commitAction('reclassify');
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
        var pred = this.predictions[this.currentIndex];

        // Cross-camera reviews: commitAction handles its own reject sheet (showCCRejectSheet)
        if (pred && pred._reviewType === 'cross_camera') {
            this.commitAction('reject');
            return;
        }

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

        // Cross-camera mode: immediate POST, not batched
        if (pred._reviewType === 'cross_camera') {
            this._commitCrossCamera(pred, action);
            return;
        }

        // Cluster mode: immediate POST, not batched
        if (pred._reviewType === 'cluster') {
            this._commitCluster(pred, action);
            return;
        }

        // In conflict mode: classification conflicts must use the chips, not swipe/buttons
        // BUT allow skip — it's client-only and doesn't resolve the conflict
        if (this.conflictMode && pred.conflict_id && pred.classification_conflict && action !== 'skip') {
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
        } else if (action === 'reclassify') {
            this.sessionStats.reclassified += countIncrement;
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

        // Sync queue (skip actions don't need server sync; reclassify and review actions do)
        if (action !== 'skip') {
            var syncItem = {
                prediction_id: pred.id,
                action: action,
                notes: notes || null
            };
            // For approve-as-alternate-class, include actual_class
            var _cTags = pred.corrected_tags || {};
            if (typeof _cTags === 'string') { try { _cTags = JSON.parse(_cTags); } catch(e) { _cTags = {}; } }
            if (action === 'approve' && _cTags.needs_negative_review && _cTags.actual_class) {
                syncItem.actual_class = _cTags.actual_class;
            }
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

    markBadBbox: function() {
        if (this.screen !== 'review') return;
        var pred = this.predictions[this.currentIndex];
        if (!pred) return;

        // Animate exit upward (neutral action — not approve/reject)
        var card = this.els.cardContainer && this.els.cardContainer.querySelector('.review-card');
        if (card) this.animateCardExit('up');

        this.vibrate([10, 20, 10]);

        // Build corrected_tags patch
        var existingTags = pred.corrected_tags || {};
        if (typeof existingTags === 'string') { try { existingTags = JSON.parse(existingTags); } catch(e) { existingTags = {}; } }
        var patchedTags = Object.assign({}, existingTags, {
            bad_bbox: true,
            exclude_from_training: true
        });

        // Queue as approved (valid detection, just bad bbox placement)
        this.syncQueue.push({
            prediction_id: pred.id,
            action: 'approve',
            notes: 'bad_bbox',
            corrected_tags: patchedTags
        });

        // Update stats (counts as an approval)
        var countIncrement = (pred.group_id && pred.member_count > 1) ? pred.member_count : 1;
        this.sessionStats.approved += countIncrement;

        // Review log
        this.reviewLog.push({
            prediction_id: pred.id,
            action: 'approve',
            notes: 'bad_bbox',
            timestamp: Date.now()
        });

        // Undo stack
        this.undoStack = {
            prediction: pred,
            index: this.currentIndex,
            action: 'approve',
            notes: 'bad_bbox'
        };

        this.scheduleSyncReviews();
        this._showToast('Bad BBox marked');
        this.advanceToNextCard();
    },

    _commitCrossCamera: function(pred, action) {
        var self = this;
        var linkId = pred.id;

        // Animate
        var card = this.els.cardContainer.querySelector('.review-card');
        if (card) {
            if (action === 'approve') this.animateCardExit('right');
            else if (action === 'reject') this.animateCardExit('left');
            else this.animateCardExit('up');
        }

        // Haptic
        if (action === 'approve') this.vibrate([10]);
        else if (action === 'reject') this.vibrate([10, 30, 10]);
        else this.vibrate([5]);

        // Stats
        if (action === 'approve') this.sessionStats.approved++;
        else if (action === 'reject') this.sessionStats.rejected++;
        else this.sessionStats.skipped++;

        // Log
        this.reviewLog.push({ prediction_id: linkId, action: action, timestamp: Date.now() });
        this.undoStack = { prediction: pred, index: this.currentIndex, action: action };

        // API call (immediate, not batched)
        if (action === 'approve') {
            fetch('/api/ai/cross-camera/links/' + linkId + '/confirm', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ reject: false })
            }).catch(function(e) { console.error('CC confirm failed:', e); });
            this.advanceToNextCard();
        } else if (action === 'reject') {
            // Show cross-camera rejection sheet
            this._pendingCCRejectPred = pred;
            this.showCCRejectSheet();
        } else {
            // Skip
            this.skippedIds.add(linkId);
            this.advanceToNextCard();
        }
    },

    showCCRejectSheet: function() {
        // Reuse the main reject sheet but with cross-camera reasons
        var sheet = this.els.rejectSheet;
        if (!sheet) return;
        var reasonsContainer = sheet.querySelector('.rejection-reasons');
        if (reasonsContainer) {
            reasonsContainer.innerHTML = '';
            var reasons = [
                { label: 'Not same vehicle', value: 'not_same_vehicle' },
                { label: 'Person / object', value: 'person_object' },
                { label: 'Bad quality', value: 'bad_quality' },
                { label: 'Wrong time window', value: 'wrong_time' },
                { label: 'Other', value: 'other' }
            ];
            var self = this;
            for (var i = 0; i < reasons.length; i++) {
                (function(r) {
                    var chip = document.createElement('button');
                    chip.className = 'reason-chip';
                    chip.setAttribute('data-reason', r.value);
                    chip.textContent = r.label;
                    chip.addEventListener('click', function() {
                        reasonsContainer.querySelectorAll('.reason-chip').forEach(function(c) { c.classList.remove('chip-selected'); });
                        chip.classList.add('chip-selected');
                        if (r.value === 'other') {
                            if (self.els.otherInputContainer) self.els.otherInputContainer.classList.remove('hidden');
                            if (self.els.otherInput) self.els.otherInput.focus();
                        } else {
                            if (self.els.otherInputContainer) self.els.otherInputContainer.classList.add('hidden');
                        }
                    });
                    reasonsContainer.appendChild(chip);
                })(reasons[i]);
            }
        }
        sheet.classList.add('open');
        // Store mode flag for done-feedback handler
        this._ccRejectMode = true;
    },

    _commitCluster: function(pred, action) {
        var self = this;

        // Animate
        var card = this.els.cardContainer.querySelector('.review-card');
        if (card) {
            if (action === 'approve') this.animateCardExit('right');
            else if (action === 'reject') this.animateCardExit('left');
            else this.animateCardExit('up');
        }

        // Haptic
        if (action === 'approve') this.vibrate([10]);
        else if (action === 'reject') this.vibrate([10, 30, 10]);
        else this.vibrate([5]);

        // Stats — count all members in the cluster
        var memberCount = pred.count || 1;
        if (action === 'approve') this.sessionStats.approved += memberCount;
        else if (action === 'reject') this.sessionStats.rejected += memberCount;
        else this.sessionStats.skipped += memberCount;

        // Log
        this.reviewLog.push({ prediction_id: pred.cluster_key, action: action, timestamp: Date.now() });
        this.undoStack = { prediction: pred, index: this.currentIndex, action: action };

        // API call (immediate)
        if (action === 'approve') {
            fetch('/api/ai/predictions/batch-cluster-review', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ cluster_key: pred.cluster_key, action: 'approve' })
            }).catch(function(e) { console.error('Cluster approve failed:', e); });
            this.advanceToNextCard();
        } else if (action === 'reject') {
            // Show reclassify sheet for clusters
            this._pendingClusterPred = pred;
            this.showClusterRejectSheet();
        } else {
            this.skippedIds.add(pred.cluster_key);
            this.advanceToNextCard();
        }
    },

    showClusterRejectSheet: function() {
        // Reuse the main reject sheet but with reclassify options
        var sheet = this.els.rejectSheet;
        if (!sheet) return;
        var heading = sheet.querySelector('.sheet-heading');
        if (heading) heading.textContent = 'Reclassify cluster as:';
        var reasonsContainer = sheet.querySelector('.rejection-reasons');
        if (reasonsContainer) {
            reasonsContainer.innerHTML = '';
            var quickClasses = ['tree', 'shadow', 'sign', 'mailbox', 'snow', 'building', 'fence', 'person', 'pole', 'rock'];
            var self = this;
            for (var i = 0; i < quickClasses.length; i++) {
                (function(cls) {
                    var chip = document.createElement('button');
                    chip.className = 'reason-chip';
                    chip.setAttribute('data-reason', cls);
                    chip.textContent = cls;
                    chip.addEventListener('click', function() {
                        reasonsContainer.querySelectorAll('.reason-chip').forEach(function(c) { c.classList.remove('chip-selected'); });
                        chip.classList.add('chip-selected');
                    });
                    reasonsContainer.appendChild(chip);
                })(quickClasses[i]);
            }
        }
        if (this.els.otherInputContainer) this.els.otherInputContainer.classList.remove('hidden');
        if (this.els.otherInput) {
            this.els.otherInput.placeholder = 'Or type a class name...';
            this.els.otherInput.value = '';
        }
        sheet.classList.add('open');
        this._clusterRejectMode = true;
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
        var _shPred = this.predictions[this.currentIndex];
        var _shScenario = _shPred ? _shPred.scenario : '';
        var _isFaceSheet = _shScenario === 'face_detection' || _shScenario === 'person_identification';
        header.textContent = _isFaceSheet ? 'What did the camera see?' : 'What is this actually?';
        container.appendChild(header);

        // Scenario-specific quick reclassify options
        var scenarioQuickClasses = {
            'face_detection': ['back of head', 'other body part'],
            'person_detection': ['animal', 'shadow', 'tree'],
            'person_identification': ['back of head', 'too blurry']
        };
        var quickClasses = scenarioQuickClasses[_shScenario];
        if (quickClasses && quickClasses.length > 0) {
            var quickSection = document.createElement('div');
            quickSection.className = 'reclassify-section';
            var quickLabel = document.createElement('div');
            quickLabel.className = 'reclassify-section-label';
            quickLabel.textContent = 'Quick options';
            quickSection.appendChild(quickLabel);
            var quickChips = document.createElement('div');
            quickChips.className = 'reclassify-chips';
            for (var qi = 0; qi < quickClasses.length; qi++) {
                quickChips.appendChild(this.createReclassifyChip(quickClasses[qi], true));
            }
            quickSection.appendChild(quickChips);
            container.appendChild(quickSection);
        }

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
        var isVehicle = _shScenario === 'vehicle_detection' || !_shScenario;

        var otherBtn = document.createElement('button');
        otherBtn.className = 'reclassify-quick-btn';
        otherBtn.textContent = isVehicle ? 'Not a vehicle' : (_isFaceSheet ? 'Not a face' : 'False positive');
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
        // Cross-camera reject completion
        if (this._ccRejectMode) {
            var ccSheet = this.els.rejectSheet;
            var selectedChip = ccSheet ? ccSheet.querySelector('.reason-chip.chip-selected') : null;
            var reason = selectedChip ? selectedChip.getAttribute('data-reason') : null;
            if (reason === 'other' && this.els.otherInput) reason = this.els.otherInput.value.trim() || 'other';
            var linkId = this._pendingCCRejectPred ? this._pendingCCRejectPred.id : null;
            if (linkId) {
                fetch('/api/ai/cross-camera/links/' + linkId + '/confirm', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ reject: true, rejection_reason: reason })
                }).catch(function(e) { console.error('CC reject failed:', e); });
            }
            this._ccRejectMode = false;
            this._pendingCCRejectPred = null;
            this.hideRejectSheet();
            this.advanceToNextCard();
            return;
        }
        // Cluster reclassify completion
        if (this._clusterRejectMode) {
            var clSheet = this.els.rejectSheet;
            var selectedChip = clSheet ? clSheet.querySelector('.reason-chip.chip-selected') : null;
            var actualClass = selectedChip ? selectedChip.getAttribute('data-reason') : null;
            if (this.els.otherInput && this.els.otherInput.value.trim()) {
                actualClass = this.els.otherInput.value.trim();
            }
            var clusterKey = this._pendingClusterPred ? this._pendingClusterPred.cluster_key : null;
            if (clusterKey && actualClass) {
                fetch('/api/ai/predictions/batch-cluster-review', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ cluster_key: clusterKey, action: 'reclassify', actual_class: actualClass })
                }).catch(function(e) { console.error('Cluster reclassify failed:', e); });
            }
            this._clusterRejectMode = false;
            this._pendingClusterPred = null;
            this.hideRejectSheet();
            if (this.els.otherInputContainer) this.els.otherInputContainer.classList.add('hidden');
            this.advanceToNextCard();
            return;
        }
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
        // Cross-camera: skip feedback = reject without reason
        if (this._ccRejectMode) {
            var linkId = this._pendingCCRejectPred ? this._pendingCCRejectPred.id : null;
            if (linkId) {
                fetch('/api/ai/cross-camera/links/' + linkId + '/confirm', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ reject: true, rejection_reason: null })
                }).catch(function(e) { console.error('CC reject failed:', e); });
            }
            this._ccRejectMode = false;
            this._pendingCCRejectPred = null;
            this.hideRejectSheet();
            this.advanceToNextCard();
            return;
        }
        // Cluster: skip feedback = reject without reclassify
        if (this._clusterRejectMode) {
            var clusterKey = this._pendingClusterPred ? this._pendingClusterPred.cluster_key : null;
            if (clusterKey) {
                fetch('/api/ai/predictions/batch-cluster-review', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ cluster_key: clusterKey, action: 'reject' })
                }).catch(function(e) { console.error('Cluster reject failed:', e); });
            }
            this._clusterRejectMode = false;
            this._pendingClusterPred = null;
            this.hideRejectSheet();
            if (this.els.otherInputContainer) this.els.otherInputContainer.classList.add('hidden');
            this.advanceToNextCard();
            return;
        }
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
                if (batch[i].corrected_tags) {
                    review.corrected_tags = batch[i].corrected_tags;
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
                        reviews: individualReviews
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
                notes: ga.notes
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
        if (this.classifySyncTimer) clearTimeout(this.classifySyncTimer);
        // Sync immediately — classifications are one-at-a-time, no benefit to batching
        this.syncClassifications();
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
        } else if (undoItem.action === 'reclassify') {
            this.sessionStats.reclassified = Math.max(0, this.sessionStats.reclassified - undoCount);
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
            // Clip prefetch skipped — many predictions are from snapshot JPEGs
            // and have no video to extract from (causes 404 noise)
        }
    },

    // --------------- S2: Summary Screen ---------------
    showSummary: function() {
        this.flushSync();
        if (this.classifyMode) {
            this.flushClassifySync();
        }
        this.showScreen('summary');

        var total = this.sessionStats.approved + this.sessionStats.rejected + this.sessionStats.skipped + this.sessionStats.reclassified;

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

        // Show/hide reclassified line
        var reclassifiedLine = document.querySelector('.stat-reclassified');
        if (reclassifiedLine) {
            reclassifiedLine.style.display = this.sessionStats.reclassified > 0 ? '' : 'none';
            var reclassifiedCount = reclassifiedLine.querySelector('.stat-count');
            if (reclassifiedCount) reclassifiedCount.textContent = String(this.sessionStats.reclassified);
        }

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
        var loadFn;
        if (this.classifyMode) {
            loadFn = function() { self.loadClassifyQueueSummary(); };
        } else if (this.crossCameraMode) {
            loadFn = function() { self.loadCrossCameraQueueSummary(); };
        } else if (this.clusterMode) {
            loadFn = function() { self.loadClusterQueueSummary(); };
        } else if (this.conflictMode) {
            loadFn = function() { self.loadConflicts(); };
        } else {
            loadFn = function() { self.loadQueueSummary(); };
        }
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
        this.sessionStats = { approved: 0, rejected: 0, skipped: 0, reclassified: 0 };
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
        // If already a URL path (e.g. /thumbnails/annotated/123), use as-is
        if (path.charAt(0) === '/' && path.indexOf('/thumbnails/') === 0) return path;
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

        var lines = [];

        // Cross-camera link debug info
        if (pred._reviewType === 'cross_camera') {
            var link = pred._link || pred;
            lines.push('=== Cross-Camera Link ===');
            lines.push('Link ID: ' + pred.id);
            lines.push('Source Track Type: ' + (pred.source_track_type || 'camera_object'));
            lines.push('Match Confidence: ' + (pred.match_confidence || pred.confidence || 0));
            lines.push('Match Method: ' + (pred.match_method || 'N/A'));
            lines.push('');
            lines.push('--- Camera A ---');
            lines.push('Camera: ' + (pred.camera_a || 'N/A'));
            lines.push('Track A ID: ' + (pred.track_a_id || 'N/A'));
            lines.push('Class A: ' + (pred.cls_a || 'N/A'));
            lines.push('First Seen A: ' + (pred.first_seen_a || 'N/A'));
            lines.push('Pred ID A: ' + (pred.pred_id_a || 'N/A'));
            lines.push('');
            lines.push('--- Camera B ---');
            lines.push('Camera: ' + (pred.camera_b || 'N/A'));
            lines.push('Track B ID: ' + (pred.track_b_id || 'N/A'));
            lines.push('Class B: ' + (pred.cls_b || 'N/A'));
            lines.push('First Seen B: ' + (pred.first_seen_b || 'N/A'));
            lines.push('Pred ID B: ' + (pred.pred_id_b || 'N/A'));
            lines.push('');

            // Include video player state if active
            if (this._ccClipData) {
                var cd = this._ccClipData;
                lines.push('=== Video Clip Data ===');
                if (cd.camera_a) {
                    lines.push('--- Clip A ---');
                    lines.push('Camera: ' + (cd.camera_a.camera_id || 'N/A'));
                    lines.push('Has Clip: ' + (cd.camera_a.has_clip || false));
                    lines.push('Clip URL: ' + (cd.camera_a.clip_url || 'N/A'));
                    lines.push('Video Size: ' + (cd.camera_a.video_width || 0) + 'x' + (cd.camera_a.video_height || 0));
                    lines.push('BBox: (' + (cd.camera_a.bbox ? cd.camera_a.bbox.x + ',' + cd.camera_a.bbox.y + ' ' + cd.camera_a.bbox.width + 'x' + cd.camera_a.bbox.height : 'N/A') + ')');
                    lines.push('First Seen (clip): ' + (cd.camera_a.first_seen || 0) + 's');
                    lines.push('First Seen Epoch: ' + (cd.camera_a.first_seen_epoch || 0));
                    lines.push('Trajectory Points: ' + (cd.camera_a.trajectory ? cd.camera_a.trajectory.length : 0));
                    if (cd.camera_a.trajectory && cd.camera_a.trajectory.length > 0) {
                        var ta = cd.camera_a.trajectory;
                        lines.push('Traj Range: ' + (ta[0].timestamp || ta[0].ts || 0).toFixed(3) + 's - ' + (ta[ta.length-1].timestamp || ta[ta.length-1].ts || 0).toFixed(3) + 's');
                        // Show a few sample points
                        var sampleIdxs = [0, Math.floor(ta.length/4), Math.floor(ta.length/2), Math.floor(ta.length*3/4), ta.length-1];
                        for (var si = 0; si < sampleIdxs.length; si++) {
                            var sp = ta[sampleIdxs[si]];
                            lines.push('  [' + sampleIdxs[si] + '] t=' + (sp.timestamp || sp.ts || 0).toFixed(3) + ' pos=(' + (sp.x||0) + ',' + (sp.y||0) + ') size=' + (sp.w||0) + 'x' + (sp.h||0) + ' conf=' + (sp.conf||0));
                        }
                    }
                }
                lines.push('');
                if (cd.camera_b) {
                    lines.push('--- Clip B ---');
                    lines.push('Camera: ' + (cd.camera_b.camera_id || 'N/A'));
                    lines.push('Has Clip: ' + (cd.camera_b.has_clip || false));
                    lines.push('Clip URL: ' + (cd.camera_b.clip_url || 'N/A'));
                    lines.push('Video Size: ' + (cd.camera_b.video_width || 0) + 'x' + (cd.camera_b.video_height || 0));
                    lines.push('BBox: (' + (cd.camera_b.bbox ? cd.camera_b.bbox.x + ',' + cd.camera_b.bbox.y + ' ' + cd.camera_b.bbox.width + 'x' + cd.camera_b.bbox.height : 'N/A') + ')');
                    lines.push('First Seen (clip): ' + (cd.camera_b.first_seen || 0) + 's');
                    lines.push('First Seen Epoch: ' + (cd.camera_b.first_seen_epoch || 0));
                    lines.push('Trajectory Points: ' + (cd.camera_b.trajectory ? cd.camera_b.trajectory.length : 0));
                    if (cd.camera_b.trajectory && cd.camera_b.trajectory.length > 0) {
                        var tb = cd.camera_b.trajectory;
                        lines.push('Traj Range: ' + (tb[0].timestamp || tb[0].ts || 0).toFixed(3) + 's - ' + (tb[tb.length-1].timestamp || tb[tb.length-1].ts || 0).toFixed(3) + 's');
                        var sIdxs = [0, Math.floor(tb.length/4), Math.floor(tb.length/2), Math.floor(tb.length*3/4), tb.length-1];
                        for (var sj = 0; sj < sIdxs.length; sj++) {
                            var sq = tb[sIdxs[sj]];
                            lines.push('  [' + sIdxs[sj] + '] t=' + (sq.timestamp || sq.ts || 0).toFixed(3) + ' pos=(' + (sq.x||0) + ',' + (sq.y||0) + ') size=' + (sq.w||0) + 'x' + (sq.h||0) + ' conf=' + (sq.conf||0));
                        }
                    }
                }
            }
        } else {
            // Standard prediction debug info
            var tags = pred.predicted_tags || {};
            var corrected = pred.corrected_tags || {};
            lines.push('Prediction ID: ' + pred.id);
            lines.push('Video ID: ' + pred.video_id);
            lines.push('Camera: ' + (pred.camera_id || 'N/A'));
            lines.push('Scenario: ' + (pred.scenario || 'N/A'));
            lines.push('Model: ' + (pred.model_name || '?') + ' v' + (pred.model_version || '?'));
            lines.push('Confidence: ' + (pred.confidence || 0));
            lines.push('Class: ' + (tags.class || tags.vehicle_type || 'N/A'));
            lines.push('BBox: ' + pred.bbox_x + ',' + pred.bbox_y + ' ' + pred.bbox_width + 'x' + pred.bbox_height);
            lines.push('Timestamp: ' + (pred.timestamp || 0));
            lines.push('Review: ' + (pred.review_status || 'N/A'));
            if (pred.group_id) lines.push('Group ID: ' + pred.group_id + ' (' + (pred.member_count || 1) + ' members)');
            if (pred.conflict_id) lines.push('Conflict ID: ' + pred.conflict_id);
            if (corrected.vehicle_subtype) lines.push('Classified as: ' + corrected.vehicle_subtype);
            if (corrected.actual_class) lines.push('Reclassified to: ' + corrected.actual_class);
            if (pred.thumbnail_path) lines.push('Thumbnail: ' + pred.thumbnail_path.replace(/^.*[\\\/]/, ''));
        }

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

    openFeedbackModal: function() {
        var modal = this.els.aiFeedbackModal;
        var ctx = this.els.feedbackContext;
        var txt = this.els.feedbackText;
        if (!modal) return;

        // Show context about current prediction
        var pred = this.predictions && this.predictions[this.currentIndex];
        if (pred && ctx) {
            var tags = pred.predicted_tags || {};
            if (typeof tags === 'string') try { tags = JSON.parse(tags); } catch(e) { tags = {}; }
            ctx.textContent = 'Prediction #' + pred.id + ' | ' + (tags['class'] || pred.scenario || 'unknown') + ' | Conf: ' + (pred.confidence ? (pred.confidence * 100).toFixed(0) + '%' : 'N/A');
        }
        if (txt) txt.value = '';
        modal.classList.remove('hidden');
        if (txt) setTimeout(function() { txt.focus(); }, 100);
    },

    closeFeedbackModal: function() {
        var modal = this.els.aiFeedbackModal;
        if (modal) modal.classList.add('hidden');
    },

    submitFeedback: function() {
        var txt = this.els.feedbackText;
        var feedback = txt ? txt.value.trim() : '';
        if (!feedback) {
            this._showToast('Please enter feedback');
            return;
        }

        var pred = this.predictions && this.predictions[this.currentIndex];
        var payload = {
            feedback: feedback,
            prediction_id: pred ? pred.id : null,
            video_id: pred ? pred.video_id : null,
            scenario: pred ? pred.scenario : null,
            predicted_class: null,
            confidence: pred ? pred.confidence : null,
            review_mode: this.classifyMode ? 'classify' : (this.conflictMode ? 'conflict' : (this.crossCameraMode ? 'cross_camera' : (this.clusterMode ? 'cluster' : 'prediction'))),
            active_filter: this.activeFilter,
            url: window.location.href
        };

        if (pred) {
            var tags = pred.predicted_tags || {};
            if (typeof tags === 'string') try { tags = JSON.parse(tags); } catch(e) { tags = {}; }
            payload.predicted_class = tags['class'] || tags.class_name || null;
        }

        // Capture current video timestamps for cross-camera clips
        var videoEls = document.querySelectorAll('.cc-video');
        if (videoEls.length >= 2) {
            payload.video_timestamps = [];
            for (var vi = 0; vi < videoEls.length; vi++) {
                var v = videoEls[vi];
                payload.video_timestamps.push({
                    camera: vi === 0 ? 'A' : 'B',
                    clip_url: v.src || v.currentSrc || '',
                    current_time: v.currentTime,
                    duration: v.duration || 0
                });
            }
        }
        // Include cross-camera clip data if available
        if (this._ccClipData) {
            payload.clip_data = {
                camera_a: this._ccClipData.camera_a ? {
                    camera_id: this._ccClipData.camera_a.camera_id,
                    clip_url: this._ccClipData.camera_a.clip_url,
                    video_track_id: this._ccClipData.camera_a.video_track_id,
                    class_name: this._ccClipData.camera_a.class_name,
                    first_seen: this._ccClipData.camera_a.first_seen,
                    first_seen_epoch: this._ccClipData.camera_a.first_seen_epoch
                } : null,
                camera_b: this._ccClipData.camera_b ? {
                    camera_id: this._ccClipData.camera_b.camera_id,
                    clip_url: this._ccClipData.camera_b.clip_url,
                    video_track_id: this._ccClipData.camera_b.video_track_id,
                    class_name: this._ccClipData.camera_b.class_name,
                    first_seen: this._ccClipData.camera_b.first_seen,
                    first_seen_epoch: this._ccClipData.camera_b.first_seen_epoch
                } : null
            };
            // Sync offsets: how video playback is offset in the timeline
            payload.sync_offsets = {
                delay_a: this._ccDelayA || 0,
                delay_b: this._ccDelayB || 0,
                epoch_a: this._ccEpochA || 0,
                epoch_b: this._ccEpochB || 0
            };
        }
        // Cross-camera link metadata
        if (pred && pred._ccLink) {
            payload.cross_camera_link_id = pred._ccLink.id || pred.id;
            payload.match_confidence = pred._ccLink.match_confidence || pred._ccLink.confidence;
            payload.match_factors = pred._ccLink.match_factors;
        }

        // POST to feedback endpoint
        fetch('/api/ai/feedback', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Auth-Role': 'admin' },
            body: JSON.stringify(payload)
        }).then(function(resp) { return resp.json(); })
        .then(function(data) {
            if (data.success) {
                ReviewApp._showToast('Feedback saved', '#4CAF50');
            } else {
                ReviewApp._showToast('Error: ' + (data.error || 'unknown'), '#f44336');
            }
        }).catch(function(err) {
            ReviewApp._showToast('Error saving feedback', '#f44336');
        });

        this.closeFeedbackModal();
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
        // Fetch known classes from API so the list stays current
        fetch('/api/training-gallery/filters?status=approved')
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.all_classes) {
                    self.knownClasses = data.all_classes.map(function(c) { return c.name; });
                }
            })
            .catch(function() { /* keep hardcoded fallback */ });

        fetch('/api/ai/tracks/conflicts')
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (!data.success) return;
                self.conflicts = data.conflicts || [];
                // Pre-cache thumbnails for all conflicts
                for (var i = 0; i < self.conflicts.length; i++) {
                    var thumbUrl = self.getThumbUrl(self.conflicts[i].thumbnail_path);
                    if (thumbUrl && !self.imageCache[thumbUrl]) {
                        var preImg = new Image();
                        preImg.src = thumbUrl;
                        self.imageCache[thumbUrl] = preImg;
                    }
                }
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
        this.sessionStats = { approved: 0, rejected: 0, skipped: 0, reclassified: 0 };
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

        // Conflict option chips - include the displayed class if not already present
        var options = pred.conflict_options.slice();
        var predTags = pred.predicted_tags || {};
        if (typeof predTags === 'string') { try { predTags = JSON.parse(predTags); } catch(e) { predTags = {}; } }
        var displayedClass = predTags['class'] || predTags.class_name || '';
        if (displayedClass) {
            var optionsLower = options.map(function(o) { return o.toLowerCase(); });
            if (optionsLower.indexOf(displayedClass.toLowerCase()) === -1) {
                options.unshift(displayedClass);
            }
        }
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

        // "Multiple Vehicles" suggested chip for mixed-classification conflicts
        var optionSet = {};
        for (var j = 0; j < options.length; j++) {
            optionSet[options[j].toLowerCase()] = true;
        }
        if (options.length >= 2 && !optionSet['multiple_vehicles']) {
            var mvDivider = document.createElement('div');
            mvDivider.className = 'classify-divider';
            mvDivider.style.marginTop = '8px';
            mvDivider.textContent = 'Suggested:';
            container.appendChild(mvDivider);

            var mvChip = document.createElement('button');
            mvChip.className = 'classify-chip';
            mvChip.setAttribute('data-subtype', 'multiple_vehicles');
            mvChip.textContent = 'Multiple Vehicles';
            mvChip.style.borderColor = '#F59E0B';
            mvChip.style.color = '#F59E0B';
            mvChip.addEventListener('click', function() {
                self.resolveConflictAsClassification(pred, 'multiple_vehicles');
            });
            container.appendChild(mvChip);
        }

        // Searchable class widget replacing flat chip list
        var searchWidget = createClassSearchWidget({
            classes: self.knownClasses.map(function(c) { return {name: c}; }),
            onSelect: function(cls) {
                self.resolveConflictAsClassification(pred, cls);
            },
            placeholder: 'Search all classes...',
            excludeClasses: options.map(function(o) { return o.toLowerCase(); })
        });
        container.appendChild(searchWidget);

        // Reject all button
        var rejectBtn = document.createElement('button');
        rejectBtn.className = 'classify-chip classify-ruleout';
        rejectBtn.textContent = '\u2718 Reject all ' + pred.member_count + ' frames';
        rejectBtn.style.marginTop = '8px';
        rejectBtn.addEventListener('click', function() {
            self.resolveConflictAction(pred, 'reject');
        });
        container.appendChild(rejectBtn);

        // Skip button
        var skipBtn = document.createElement('button');
        skipBtn.className = 'classify-chip';
        skipBtn.textContent = 'Skip';
        skipBtn.style.marginTop = '4px';
        skipBtn.style.color = '#a1a1aa';
        skipBtn.style.borderStyle = 'dashed';
        skipBtn.addEventListener('click', function() {
            self.commitAction('skip');
        });
        container.appendChild(skipBtn);

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
                vehicle_subtype: vehicleSubtype
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
            decision: decision
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
