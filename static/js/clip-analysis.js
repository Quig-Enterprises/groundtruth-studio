/**
 * Clip Analysis - Full lifecycle: source selection, analysis review, export.
 * Vanilla JS, no framework dependencies.
 */
(function() {
    'use strict';

    // ── Constants ─────────────────────────────────────────────────────────
    const CLASS_COLORS = {
        'sedan':        '#4fc3f7',
        'SUV':          '#81c784',
        'pickup truck': '#ffb74d',
        'minivan':      '#ce93d8',
        'van':          '#a1887f',
        'tractor':      '#fff176',
        'ATV':          '#ef5350',
        'UTV':          '#ff7043',
        'motorcycle':   '#e91e63',
        'bus':          '#7e57c2',
        'semi truck':   '#5c6bc0',
        'dump truck':   '#8d6e63',
        'golf cart':    '#aed581',
        'skid loader':  '#ffab40',
        'snowmobile':   '#80deea',
        'trailer':      '#bcaaa4',
        'rowboat':      '#4dd0e1',
        'fishing boat': '#26c6da',
        'speed boat':   '#00bcd4',
        'pontoon boat': '#009688',
        'kayak':        '#26a69a',
        'canoe':        '#2bbbad',
        'sailboat':     '#00897b',
        'jet ski':      '#00acc1',
        'false positive': '#616161',
        'person':       '#ff8a80',
        'animal':       '#ffcc80',
        'snow pile':    '#e0e0e0',
        'sign':         '#ffab40',
        'building':     '#78909c',
        'vegetation':   '#66bb6a',
        'shadow':       '#424242',
        'unknown':      '#9e9e9e',
        'default':      '#90a4ae'
    };

    const NON_VEHICLE_CLASSES = [
        'person', 'animal', 'deer', 'bear', 'dog',
        'snow pile', 'dirt pile', 'rock',
        'sign', 'mailbox', 'fire hydrant', 'trash can', 'dumpster',
        'building', 'shed', 'fence', 'gate', 'pole',
        'tree', 'stump', 'bush', 'vegetation',
        'shadow', 'reflection', 'glare',
        'false positive', 'unknown'
    ];

    const FRAME_STEP = 1 / 30; // ~1 frame at 30fps
    const POLL_INTERVAL = 2000;

    // ── Helpers ───────────────────────────────────────────────────────────
    const $ = id => document.getElementById(id);
    const $$ = sel => document.querySelectorAll(sel);

    function classColor(cls) {
        return CLASS_COLORS[cls] || CLASS_COLORS['default'];
    }

    function formatTime(sec) {
        if (!sec || isNaN(sec)) return '0:00';
        const m = Math.floor(sec / 60);
        const s = Math.floor(sec % 60);
        return m + ':' + String(s).padStart(2, '0');
    }

    function formatCentralTime(isoString) {
        if (!isoString) return '';
        return new Date(isoString).toLocaleString('en-US', {
            timeZone: 'America/Chicago',
            month: 'short', day: 'numeric',
            hour: 'numeric', minute: '2-digit',
            hour12: true
        });
    }

    function showScreen(screenId) {
        $$('.screen').forEach(function(el) { el.classList.remove('active'); });
        var target = $(screenId);
        if (target) target.classList.add('active');
    }

    function escapeHtml(text) {
        var div = document.createElement('div');
        div.textContent = String(text || '');
        return div.innerHTML;
    }

    async function apiFetch(url, opts) {
        try {
            var resp = await fetch(url, opts);
            if (!resp.ok) {
                var text = await resp.text();
                var msg;
                try { msg = JSON.parse(text).error; } catch(_e) { msg = text; }
                throw new Error(msg || 'Request failed (' + resp.status + ')');
            }
            return await resp.json();
        } catch (err) {
            console.error('API error:', url, err);
            throw err;
        }
    }

    // ── State ─────────────────────────────────────────────────────────────
    var state = {
        selectedSource: null,   // { type, id, camera_id, ... }
        uploadedFile: null,
        analysisId: null,
        analysis: null,
        tracks: [],
        selectedTrackIds: new Set(),
        highlightedTrackId: null,
        pollTimer: null
    };

    // ── DOM cache ─────────────────────────────────────────────────────────
    var dom = {};

    function cacheDom() {
        dom.sourceScreen      = $('source-screen');
        dom.reviewScreen      = $('review-screen');
        dom.exportScreen      = $('export-screen');
        dom.frigateList       = $('frigate-events-list');
        dom.ecoeyelList       = $('ecoeye-alerts-list');
        dom.frigateFilter     = $('frigate-camera-filter');
        dom.ecoeyelFilter     = $('ecoeye-camera-filter');
        dom.btnRefresh        = $('btn-refresh-sources');
        dom.btnAnalyze        = $('btn-analyze');
        dom.uploadZone        = $('upload-zone');
        dom.fileInput         = $('file-input');
        dom.uploadCameraId    = $('upload-camera-id');
        dom.previousList      = $('previous-list');
        dom.btnBackSources    = $('btn-back-to-sources');
        dom.reviewTitle       = $('review-title');
        dom.reviewBadge       = $('review-status-badge');
        dom.video             = $('analysis-video');
        dom.overlayCanvas     = $('overlay-canvas');
        dom.btnPrev           = $('btn-prev-frame');
        dom.btnPlayPause      = $('btn-play-pause');
        dom.btnNext           = $('btn-next-frame');
        dom.timeDisplay       = $('time-display');
        dom.trackList         = $('track-list');
        dom.btnSelectAll      = $('btn-select-all-tracks');
        dom.btnExportSelected = $('btn-export-selected');
        dom.timelineCanvas    = $('timeline-canvas');
        dom.distributionBars  = $('distribution-bars');
        dom.btnBackReview     = $('btn-back-to-review');
        dom.sliderTopN        = $('slider-topn');
        dom.sliderQuality     = $('slider-quality');
        dom.topnValue         = $('topn-value');
        dom.qualityValue      = $('quality-value');
        dom.exportPreview     = $('export-preview');
        dom.btnConfirmExport  = $('btn-confirm-export');
        dom.exportSuccess     = $('export-success');
        dom.exportResultText  = $('export-result-text');
        dom.loadingOverlay    = $('loading-overlay');
        dom.loadingText       = $('loading-text');
        dom.chkRemoteWorker  = $('chk-remote-worker');
        dom.btnReanalyze     = $('btn-reanalyze');
    }

    // ══════════════════════════════════════════════════════════════════════
    //  SCREEN 1: SOURCE SELECTION
    // ══════════════════════════════════════════════════════════════════════

    function initSourceScreen() {
        // Tab switching
        $$('.tab-btn').forEach(function(btn) {
            btn.addEventListener('click', function() {
                $$('.tab-btn').forEach(function(b) { b.classList.remove('active'); });
                $$('.tab-content').forEach(function(c) { c.classList.remove('active'); });
                btn.classList.add('active');
                var tab = btn.getAttribute('data-tab');
                var content = $('tab-' + tab);
                if (content) content.classList.add('active');
                clearSelection();
            });
        });

        dom.btnRefresh.addEventListener('click', function() { loadSources(); });
        dom.btnAnalyze.addEventListener('click', function() {
            if (state.selectedSource && state.selectedSource.workflow_status &&
                (state.selectedSource.workflow_status === 'needs_review' ||
                 state.selectedSource.workflow_status === 'review_complete')) {
                openAnalysis(state.selectedSource.id);
            } else {
                startAnalysis();
            }
        });

        // Upload drag-drop
        dom.uploadZone.addEventListener('click', function() { dom.fileInput.click(); });
        dom.fileInput.addEventListener('change', handleFileSelect);
        dom.uploadZone.addEventListener('dragover', function(e) {
            e.preventDefault();
            dom.uploadZone.classList.add('dragover');
        });
        dom.uploadZone.addEventListener('dragleave', function() {
            dom.uploadZone.classList.remove('dragover');
        });
        dom.uploadZone.addEventListener('drop', function(e) {
            e.preventDefault();
            dom.uploadZone.classList.remove('dragover');
            if (e.dataTransfer.files.length > 0) {
                setUploadedFile(e.dataTransfer.files[0]);
            }
        });

        dom.uploadCameraId.addEventListener('input', updateAnalyzeButton);

        // Camera filter changes
        dom.frigateFilter.addEventListener('change', function() { loadSources('frigate'); });
        dom.ecoeyelFilter.addEventListener('change', function() { loadSources('ecoeye'); });

        loadSources();
        loadPreviousAnalyses();
    }

    function clearSelection() {
        state.selectedSource = null;
        state.uploadedFile = null;
        $$('.source-item.selected').forEach(function(el) { el.classList.remove('selected'); });
        updateAnalyzeButton();
    }

    function updateAnalyzeButton() {
        var enabled = false;
        var label = 'Analyze Clip';
        if (state.selectedSource) {
            enabled = true;
            var wf = state.selectedSource.workflow_status;
            if (wf === 'needs_review' || wf === 'review_complete') {
                label = 'Review Clip';
            } else if (wf === 'processing') {
                label = 'Processing...';
                enabled = false;
            }
        } else if (state.uploadedFile && dom.uploadCameraId.value.trim()) {
            enabled = true;
        }
        dom.btnAnalyze.disabled = !enabled;
        dom.btnAnalyze.textContent = label;
    }

    function handleFileSelect(e) {
        if (e.target.files.length > 0) {
            setUploadedFile(e.target.files[0]);
        }
    }

    function setUploadedFile(file) {
        state.uploadedFile = file;
        state.selectedSource = null;
        var sizeStr = (file.size / 1048576).toFixed(1);
        dom.uploadZone.querySelector('p').textContent = file.name + ' (' + sizeStr + ' MB)';
        dom.uploadZone.classList.add('has-file');
        updateAnalyzeButton();
    }

    async function loadSources(tabFilter) {
        var frigateCamera = dom.frigateFilter.value;
        var ecoeyelCamera = dom.ecoeyelFilter.value;

        if (!tabFilter || tabFilter === 'frigate') {
            dom.frigateList.textContent = '';
            appendEmptyState(dom.frigateList, 'Loading events...');
            try {
                var url = '/api/clip-analysis/sources?type=frigate';
                if (frigateCamera) url += '&camera_id=' + encodeURIComponent(frigateCamera);
                var data = await apiFetch(url);
                renderSourceList(dom.frigateList, data.sources || [], 'frigate');
                if (!frigateCamera) populateCameraFilter(dom.frigateFilter, data.cameras || []);
            } catch (err) {
                dom.frigateList.textContent = '';
                appendEmptyState(dom.frigateList, 'Failed to load Frigate events: ' + err.message, true);
            }
        }

        if (!tabFilter || tabFilter === 'ecoeye') {
            dom.ecoeyelList.textContent = '';
            appendEmptyState(dom.ecoeyelList, 'Loading alerts...');
            try {
                var url2 = '/api/clip-analysis/sources?type=ecoeye';
                if (ecoeyelCamera) url2 += '&camera_id=' + encodeURIComponent(ecoeyelCamera);
                var data2 = await apiFetch(url2);
                renderSourceList(dom.ecoeyelList, data2.sources || [], 'ecoeye');
                if (!ecoeyelCamera) populateCameraFilter(dom.ecoeyelFilter, data2.cameras || []);
            } catch (err2) {
                dom.ecoeyelList.textContent = '';
                appendEmptyState(dom.ecoeyelList, 'Failed to load EcoEye alerts: ' + err2.message, true);
            }
        }
    }

    function appendEmptyState(container, text, isError) {
        var div = document.createElement('div');
        div.className = 'empty-state' + (isError ? ' error' : '');
        div.textContent = text;
        container.appendChild(div);
    }

    function populateCameraFilter(selectEl, cameras) {
        var current = selectEl.value;
        var firstOption = selectEl.querySelector('option');
        selectEl.textContent = '';
        selectEl.appendChild(firstOption);
        cameras.forEach(function(cam) {
            var opt = document.createElement('option');
            opt.value = cam;
            opt.textContent = cam;
            if (cam === current) opt.selected = true;
            selectEl.appendChild(opt);
        });
    }

    function renderSourceList(container, sources, type) {
        container.textContent = '';
        if (sources.length === 0) {
            var label = type === 'frigate' ? 'Frigate events' : 'EcoEye alerts';
            appendEmptyState(container, 'No ' + label + ' found.');
            return;
        }

        sources.forEach(function(src) {
            var item = document.createElement('div');
            item.className = 'source-item';
            item.dataset.sourceType = type;
            item.dataset.sourceId = src.id;

            // Thumbnail
            if (src.thumbnail) {
                var img = document.createElement('img');
                img.className = 'source-thumb';
                img.src = src.thumbnail;
                img.alt = 'thumbnail';
                img.loading = 'lazy';
                img.decoding = 'async';
                img.width = 144;
                img.height = 96;
                item.appendChild(img);
            } else {
                var placeholder = document.createElement('div');
                placeholder.className = 'source-thumb placeholder';
                item.appendChild(placeholder);
            }

            // Info section
            var info = document.createElement('div');
            info.className = 'source-info';

            var labelEl = document.createElement('div');
            labelEl.className = 'source-label';
            labelEl.textContent = src.label || src.id;
            info.appendChild(labelEl);

            var meta = document.createElement('div');
            meta.className = 'source-meta';

            var camSpan = document.createElement('span');
            camSpan.className = 'meta-camera';
            camSpan.textContent = src.camera_id || '';
            meta.appendChild(camSpan);

            if (src.timestamp) {
                var ts = formatCentralTime(src.timestamp);
                var timeSpan = document.createElement('span');
                timeSpan.className = 'meta-time';
                timeSpan.textContent = ts;
                meta.appendChild(document.createTextNode(' '));
                meta.appendChild(timeSpan);
            }
            if (src.duration) {
                var durSpan = document.createElement('span');
                durSpan.className = 'meta-duration';
                durSpan.textContent = formatTime(src.duration);
                meta.appendChild(document.createTextNode(' '));
                meta.appendChild(durSpan);
            }

            info.appendChild(meta);
            item.appendChild(info);

            // Workflow status badge (if analysis exists)
            if (src.workflow_status) {
                var wfBadge = document.createElement('span');
                wfBadge.className = 'badge source-status';
                if (src.workflow_status === 'review_complete') {
                    wfBadge.classList.add('badge-complete');
                    wfBadge.textContent = 'Done';
                } else if (src.workflow_status === 'needs_review') {
                    wfBadge.classList.add('badge-needs-review');
                    var rText = 'Review';
                    if (src.track_count && src.reviewed_count != null) {
                        rText += ' ' + src.reviewed_count + '/' + src.track_count;
                    }
                    wfBadge.textContent = rText;
                } else if (src.workflow_status === 'processing') {
                    wfBadge.classList.add('badge-processing');
                    wfBadge.textContent = 'Running';
                }
                item.appendChild(wfBadge);
            }

            item.addEventListener('click', function() {
                $$('.source-item.selected').forEach(function(el) { el.classList.remove('selected'); });
                item.classList.add('selected');
                state.selectedSource = {
                    type: type, id: src.id, camera_id: src.camera_id,
                    label: src.label || src.id,
                    workflow_status: src.workflow_status || null
                };
                state.uploadedFile = null;
                updateAnalyzeButton();
            });

            container.appendChild(item);
        });
    }

    async function loadPreviousAnalyses() {
        try {
            var data = await apiFetch('/api/clip-analysis/list');
            renderPreviousList(data.analyses || []);
        } catch (_err) {
            dom.previousList.textContent = '';
            appendEmptyState(dom.previousList, 'Could not load previous analyses.');
        }
    }

    function renderPreviousList(analyses) {
        dom.previousList.textContent = '';
        if (analyses.length === 0) {
            appendEmptyState(dom.previousList, 'No previous analyses.');
            return;
        }
        analyses.forEach(function(a) {
            var item = document.createElement('div');
            item.className = 'analysis-item';

            var info = document.createElement('div');
            info.className = 'analysis-item-info';

            var lbl = document.createElement('span');
            lbl.className = 'analysis-item-label';
            lbl.textContent = a.label || a.id;
            info.appendChild(lbl);

            var meta = document.createElement('span');
            meta.className = 'analysis-item-meta';
            var metaText = a.camera_id || '';
            if (a.created_at) metaText += (metaText ? ' \u00B7 ' : '') + formatCentralTime(a.created_at);
            if (a.track_count) metaText += (metaText ? ' \u00B7 ' : '') + a.track_count + ' tracks';
            meta.textContent = metaText;
            info.appendChild(meta);

            item.appendChild(info);

            // Workflow status badge
            var wf = a.workflow_status || a.status || 'unknown';
            var badge = document.createElement('span');
            badge.className = 'badge';
            if (wf === 'review_complete') {
                badge.classList.add('badge-complete');
                badge.textContent = 'Review Complete';
            } else if (wf === 'needs_review') {
                badge.classList.add('badge-needs-review');
                badge.textContent = 'Needs Review';
                if (a.track_count && a.reviewed_count > 0) {
                    badge.textContent += ' (' + a.reviewed_count + '/' + a.track_count + ')';
                }
            } else if (wf === 'processing') {
                badge.classList.add('badge-processing');
                badge.textContent = 'Processing';
            } else if (wf === 'failed') {
                badge.classList.add('danger');
                badge.textContent = 'Failed';
            } else {
                badge.textContent = wf;
            }
            item.appendChild(badge);

            if (wf === 'needs_review' || wf === 'review_complete') {
                item.style.cursor = 'pointer';
                item.addEventListener('click', function() { openAnalysis(a.id); });
            }
            dom.previousList.appendChild(item);
        });
    }

    // ── Start Analysis ────────────────────────────────────────────────────

    async function startAnalysis() {
        var btn = dom.btnAnalyze;
        btn.disabled = true;
        btn.textContent = 'Submitting...';

        try {
            var body;
            var headers = {};

            if (state.uploadedFile) {
                body = new FormData();
                body.append('file', state.uploadedFile);
                body.append('camera_id', dom.uploadCameraId.value.trim());
            } else if (state.selectedSource) {
                headers['Content-Type'] = 'application/json';
                var payload = {
                    source_type: state.selectedSource.type,
                    source_id: state.selectedSource.id,
                    camera_id: state.selectedSource.camera_id
                };
                if (dom.chkRemoteWorker && dom.chkRemoteWorker.checked) {
                    payload.mode = 'remote';
                }
                body = JSON.stringify(payload);
            } else {
                btn.disabled = false;
                btn.textContent = 'Analyze Clip';
                return;
            }

            var data = await apiFetch('/api/clip-analysis/run', {
                method: 'POST',
                headers: headers,
                body: body
            });

            var queuedId = data.analysis_id || data.id;

            // Show toast and stay on source screen
            var modeLabel = data.mode === 'remote' ? 'Queued for remote worker: ' : 'Analysis queued for ';
            showToast(modeLabel + (state.selectedSource ? state.selectedSource.label : 'uploaded clip'));

            // Start background polling for this job
            startBackgroundPoll(queuedId);

            // Clear selection and refresh lists
            state.selectedSource = null;
            $$('.source-item.selected').forEach(function(el) { el.classList.remove('selected'); });
            btn.textContent = 'Analyze Clip';
            btn.disabled = true;

            // Refresh source list to show "Running" badge
            loadSources(state.activeTab || 'frigate');
            loadPreviousAnalyses();

        } catch (err) {
            btn.disabled = false;
            btn.textContent = 'Analyze Clip';
            alert('Failed to start analysis: ' + err.message);
        }
    }

    // Background poll trackers (multiple can run simultaneously)
    var _bgPolls = {};

    function startBackgroundPoll(analysisId) {
        if (_bgPolls[analysisId]) return;
        _bgPolls[analysisId] = setInterval(async function() {
            try {
                var data = await apiFetch('/api/clip-analysis/' + analysisId);
                if (data.status === 'completed') {
                    clearInterval(_bgPolls[analysisId]);
                    delete _bgPolls[analysisId];
                    showToast('Analysis complete \u2014 ready for review');
                    loadSources(state.activeTab || 'frigate');
                    loadPreviousAnalyses();
                } else if (data.status === 'failed') {
                    clearInterval(_bgPolls[analysisId]);
                    delete _bgPolls[analysisId];
                    showToast('Analysis failed: ' + (data.error || 'unknown error'));
                    loadSources(state.activeTab || 'frigate');
                    loadPreviousAnalyses();
                } else if (data.status === 'processing' || !data.status) {
                    // For remote jobs, also check job-status endpoint
                    try {
                        var jobData = await apiFetch('/api/clip-analysis/' + analysisId + '/job-status');
                        if (jobData.status === 'completed') {
                            clearInterval(_bgPolls[analysisId]);
                            delete _bgPolls[analysisId];
                            showToast('Remote analysis complete \u2014 ready for review');
                            loadSources(state.activeTab || 'frigate');
                            loadPreviousAnalyses();
                        } else if (jobData.status === 'failed') {
                            clearInterval(_bgPolls[analysisId]);
                            delete _bgPolls[analysisId];
                            showToast('Remote analysis failed: ' + (jobData.error || 'unknown error'));
                            loadSources(state.activeTab || 'frigate');
                            loadPreviousAnalyses();
                        }
                    } catch (_jobErr) {
                        // job-status endpoint may not exist for local jobs, ignore
                    }
                }
            } catch (_err) {
                // Silently retry on next interval
            }
        }, POLL_INTERVAL);
    }

    async function openAnalysis(id) {
        showLoading('Loading analysis...');
        try {
            state.analysisId = id;
            var results = await Promise.all([
                apiFetch('/api/clip-analysis/' + id),
                apiFetch('/api/clip-analysis/' + id + '/tracks')
            ]);
            state.analysis = results[0];
            state.tracks = results[1].tracks || [];
            state.selectedTrackIds.clear();
            state.highlightedTrackId = null;
            hideLoading();
            enterReviewScreen();
        } catch (err) {
            hideLoading();
            alert('Failed to load analysis: ' + err.message);
        }
    }

    async function reanalyzeClip() {
        if (!state.analysisId) return;
        if (!confirm('This will delete existing results and re-run analysis. Continue?')) return;

        dom.btnReanalyze.disabled = true;
        dom.btnReanalyze.textContent = 'Re-analyzing...';

        try {
            await apiFetch('/api/clip-analysis/' + state.analysisId + '/reanalyze', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({})
            });

            // Go back to source screen and start polling
            player.stop();
            showScreen('source-screen');
            showToast('Re-analysis started for #' + state.analysisId);
            startBackgroundPoll(state.analysisId);
            loadSources(state.activeTab || 'frigate');
            loadPreviousAnalyses();
        } catch (err) {
            alert('Failed to start re-analysis: ' + err.message);
        } finally {
            dom.btnReanalyze.disabled = false;
            dom.btnReanalyze.textContent = 'Re-analyze';
        }
    }

    // ══════════════════════════════════════════════════════════════════════
    //  SCREEN 2: ANALYSIS REVIEW
    // ══════════════════════════════════════════════════════════════════════

    function initReviewScreen() {
        dom.btnBackSources.addEventListener('click', function() {
            player.stop();
            showScreen('source-screen');
            loadPreviousAnalyses();
        });

        dom.btnReanalyze.addEventListener('click', function() { reanalyzeClip(); });

        dom.btnPrev.addEventListener('click', function() {
            dom.video.currentTime = Math.max(0, dom.video.currentTime - FRAME_STEP);
        });
        dom.btnNext.addEventListener('click', function() {
            dom.video.currentTime = Math.min(dom.video.duration || 0, dom.video.currentTime + FRAME_STEP);
        });
        dom.btnPlayPause.addEventListener('click', function() {
            if (dom.video.paused) dom.video.play();
            else dom.video.pause();
        });

        dom.video.addEventListener('play', function() {
            dom.btnPlayPause.textContent = '\u23F8';
        });
        dom.video.addEventListener('pause', function() {
            dom.btnPlayPause.textContent = '\u25B6';
        });
        dom.video.addEventListener('timeupdate', onVideoTimeUpdate);
        dom.video.addEventListener('loadedmetadata', onVideoLoaded);

        dom.btnSelectAll.addEventListener('click', toggleSelectAllTracks);
        dom.btnExportSelected.addEventListener('click', function() {
            if (state.selectedTrackIds.size > 0) {
                approveSelectedTracks();
            }
        });

        // Canvas click for track highlight
        dom.overlayCanvas.addEventListener('click', onOverlayClick);

        // Timeline click to seek
        dom.timelineCanvas.addEventListener('click', onTimelineClick);
    }

    function enterReviewScreen() {
        showScreen('review-screen');
        dom.reviewTitle.textContent = state.analysis.label || 'Analysis #' + state.analysisId;
        updateReviewBadge();

        // Load video
        dom.video.src = '/api/clip-analysis/' + state.analysisId + '/clip';
        dom.video.load();

        renderTrackList();
        renderDistribution();
        updateReviewProgress();
    }

    function getReviewCounts() {
        var total = state.tracks.length;
        var reviewed = state.tracks.filter(function(t) {
            return t.review_status === 'approved' || t.review_status === 'reviewed' || t.review_status === 'corrected' || t.review_status === 'flagged';
        }).length;
        return { total: total, reviewed: reviewed };
    }

    function updateReviewBadge() {
        var counts = getReviewCounts();

        if (counts.total === 0) {
            dom.reviewBadge.textContent = state.analysis.status || 'complete';
            dom.reviewBadge.className = 'badge';
            return;
        }

        if (counts.reviewed >= counts.total) {
            dom.reviewBadge.textContent = 'Review Complete';
            dom.reviewBadge.className = 'badge badge-complete';
        } else if (counts.reviewed > 0) {
            dom.reviewBadge.textContent = 'Needs Review (' + counts.reviewed + '/' + counts.total + ')';
            dom.reviewBadge.className = 'badge badge-needs-review';
        } else {
            dom.reviewBadge.textContent = 'Needs Review';
            dom.reviewBadge.className = 'badge badge-needs-review';
        }
    }

    function updateReviewProgress() {
        var existing = document.getElementById('review-progress');
        if (!existing) {
            existing = document.createElement('div');
            existing.id = 'review-progress';
            dom.trackList.parentNode.insertBefore(existing, dom.trackList);
        }

        var counts = getReviewCounts();
        if (counts.total === 0) {
            existing.style.display = 'none';
            return;
        }

        var pct = Math.round((counts.reviewed / counts.total) * 100);
        existing.style.display = '';
        existing.textContent = '';

        var labelRow = document.createElement('div');
        labelRow.className = 'review-progress-label';
        var labelLeft = document.createElement('span');
        labelLeft.textContent = 'Review Progress';
        var labelRight = document.createElement('span');
        labelRight.textContent = counts.reviewed + ' / ' + counts.total + ' tracks';
        labelRow.appendChild(labelLeft);
        labelRow.appendChild(labelRight);
        existing.appendChild(labelRow);

        var barOuter = document.createElement('div');
        barOuter.className = 'review-progress-bar';
        var barFill = document.createElement('div');
        barFill.className = 'review-progress-fill' + (pct >= 100 ? ' complete' : '');
        barFill.style.width = pct + '%';
        barOuter.appendChild(barFill);
        existing.appendChild(barOuter);
    }

    function onVideoLoaded() {
        resizeOverlayCanvas();
        player.start();
        timeline.draw();
    }

    function onVideoTimeUpdate() {
        var cur = dom.video.currentTime;
        var dur = dom.video.duration || 0;
        dom.timeDisplay.textContent = formatTime(cur) + ' / ' + formatTime(dur);
        timeline.drawPlayhead(cur, dur);
    }

    function resizeOverlayCanvas() {
        var rect = dom.video.getBoundingClientRect();
        dom.overlayCanvas.width = rect.width;
        dom.overlayCanvas.height = rect.height;
    }

    // ── Track List ────────────────────────────────────────────────────────

    function renderTrackList() {
        dom.trackList.textContent = '';
        if (state.tracks.length === 0) {
            appendEmptyState(dom.trackList, 'No tracks detected.');
            return;
        }
        state.tracks.forEach(function(track) {
            var item = document.createElement('div');
            item.className = 'track-item';
            item.dataset.trackId = track.id;
            if (state.highlightedTrackId === track.id) item.classList.add('highlighted');

            var color = classColor(track.consensus_class);
            var conf = track.confidence != null ? Math.round(track.confidence * 100) : 0;

            // Checkbox
            var cbLabel = document.createElement('label');
            cbLabel.className = 'track-checkbox';
            var cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.dataset.trackId = track.id;
            if (state.selectedTrackIds.has(track.id)) cb.checked = true;
            cbLabel.appendChild(cb);
            item.appendChild(cbLabel);

            // Crop image
            if (track.crop_url) {
                var cropImg = document.createElement('img');
                cropImg.className = 'track-crop';
                cropImg.src = track.crop_url;
                cropImg.alt = 'crop';
                item.appendChild(cropImg);
            } else {
                var cropPlaceholder = document.createElement('div');
                cropPlaceholder.className = 'track-crop placeholder';
                item.appendChild(cropPlaceholder);
            }

            // Info section
            var infoDiv = document.createElement('div');
            infoDiv.className = 'track-info';

            // Class badge + review status indicator
            var classDiv = document.createElement('div');
            classDiv.className = 'track-class';
            var classBadge = document.createElement('span');
            classBadge.className = 'badge';
            classBadge.style.background = color;
            classBadge.textContent = track.consensus_class || 'unknown';
            classDiv.appendChild(classBadge);

            if (track.review_status === 'approved') {
                var reviewInd = document.createElement('span');
                reviewInd.className = 'track-review-status approved';
                reviewInd.title = 'Approved';
                reviewInd.textContent = '\u2713';
                classDiv.appendChild(reviewInd);
            } else if (track.review_status === 'reviewed') {
                var reviewInd = document.createElement('span');
                reviewInd.className = 'track-review-status reviewed';
                reviewInd.title = 'Reclassified';
                reviewInd.textContent = '\u270E';
                classDiv.appendChild(reviewInd);
            }

            infoDiv.appendChild(classDiv);

            // Confidence bar
            var confDiv = document.createElement('div');
            confDiv.className = 'track-confidence';
            var barOuter = document.createElement('div');
            barOuter.className = 'confidence-bar';
            var barFill = document.createElement('div');
            barFill.className = 'confidence-fill';
            barFill.style.width = conf + '%';
            barFill.style.background = color;
            barOuter.appendChild(barFill);
            confDiv.appendChild(barOuter);
            var pctSpan = document.createElement('span');
            pctSpan.className = 'confidence-pct';
            pctSpan.textContent = conf + '%';
            confDiv.appendChild(pctSpan);
            infoDiv.appendChild(confDiv);

            // Frame count
            var metaDiv = document.createElement('div');
            metaDiv.className = 'track-meta';
            metaDiv.textContent = (track.frame_count || 0) + ' frames';
            infoDiv.appendChild(metaDiv);

            item.appendChild(infoDiv);

            // Reclassify select with optgroups + custom input
            var selWrap = document.createElement('div');
            selWrap.className = 'reclassify-wrap';

            var sel = document.createElement('select');
            sel.className = 'reclassify-select input-field';
            sel.dataset.trackId = track.id;

            var vehicleGroup = document.createElement('optgroup');
            vehicleGroup.label = 'Vehicles';
            var nvGroup = document.createElement('optgroup');
            nvGroup.label = 'Actually...';

            Object.keys(CLASS_COLORS).forEach(function(c) {
                if (c === 'default') return;
                var opt = document.createElement('option');
                opt.value = c;
                opt.textContent = c;
                if (c === track.consensus_class) opt.selected = true;
                if (NON_VEHICLE_CLASSES.indexOf(c) >= 0) {
                    nvGroup.appendChild(opt);
                } else {
                    vehicleGroup.appendChild(opt);
                }
            });

            // Add "Other..." option for custom labels
            var otherOpt = document.createElement('option');
            otherOpt.value = '__other__';
            otherOpt.textContent = 'Other (type below)';
            nvGroup.appendChild(otherOpt);

            sel.appendChild(vehicleGroup);
            sel.appendChild(nvGroup);

            // Check if current class is not in any list (custom label from before)
            var allKnown = Object.keys(CLASS_COLORS);
            if (track.consensus_class && allKnown.indexOf(track.consensus_class) < 0) {
                var customOpt = document.createElement('option');
                customOpt.value = track.consensus_class;
                customOpt.textContent = track.consensus_class;
                customOpt.selected = true;
                nvGroup.insertBefore(customOpt, otherOpt);
            }

            selWrap.appendChild(sel);

            // Custom text input (hidden by default)
            var customInput = document.createElement('input');
            customInput.type = 'text';
            customInput.className = 'reclassify-custom input-field';
            customInput.placeholder = 'e.g. snow pile';
            customInput.style.display = 'none';
            selWrap.appendChild(customInput);

            item.appendChild(selWrap);

            // Flag button
            var flagBtn = document.createElement('button');
            flagBtn.className = 'track-flag-btn' + (track.has_issue ? ' flagged' : '');
            flagBtn.title = 'Report issue with this track';
            flagBtn.textContent = '\u2691';
            flagBtn.addEventListener('click', function(e) {
                e.stopPropagation();
                openIssueSheet(track.id);
            });
            item.appendChild(flagBtn);

            // Events
            cb.addEventListener('change', function(e) {
                if (e.target.checked) state.selectedTrackIds.add(track.id);
                else state.selectedTrackIds.delete(track.id);
                updateExportButton();
            });

            sel.addEventListener('change', function(e) {
                if (e.target.value === '__other__') {
                    customInput.style.display = '';
                    customInput.focus();
                } else {
                    customInput.style.display = 'none';
                    reclassifyTrack(track.id, e.target.value);
                }
            });

            customInput.addEventListener('keydown', function(e) {
                if (e.key === 'Enter') {
                    var val = customInput.value.trim().toLowerCase();
                    if (val) {
                        reclassifyTrack(track.id, val);
                        customInput.style.display = 'none';
                    }
                }
            });
            customInput.addEventListener('blur', function() {
                var val = customInput.value.trim().toLowerCase();
                if (val) {
                    reclassifyTrack(track.id, val);
                    customInput.style.display = 'none';
                } else if (sel.value === '__other__') {
                    // Revert to original class if nothing typed
                    sel.value = track.consensus_class;
                    customInput.style.display = 'none';
                }
            });

            item.addEventListener('click', function(e) {
                if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT' || e.target.tagName === 'OPTION') return;
                state.highlightedTrackId = state.highlightedTrackId === track.id ? null : track.id;
                $$('.track-item').forEach(function(el) {
                    el.classList.toggle('highlighted', el.dataset.trackId == state.highlightedTrackId);
                });
            });

            dom.trackList.appendChild(item);
        });
    }

    function toggleSelectAllTracks() {
        var allSelected = state.selectedTrackIds.size === state.tracks.length;
        if (allSelected) {
            state.selectedTrackIds.clear();
        } else {
            state.tracks.forEach(function(t) { state.selectedTrackIds.add(t.id); });
        }
        dom.trackList.querySelectorAll('input[type=checkbox]').forEach(function(cb) {
            cb.checked = !allSelected;
        });
        dom.btnSelectAll.textContent = allSelected ? 'Select All' : 'Deselect All';
        updateExportButton();
    }

    function updateExportButton() {
        var count = state.selectedTrackIds.size;
        dom.btnExportSelected.disabled = count === 0;
        dom.btnExportSelected.textContent = count > 0
            ? 'Approve ' + count + ' Track' + (count > 1 ? 's' : '')
            : 'Approve Selected';
    }

    async function approveSelectedTracks() {
        var ids = Array.from(state.selectedTrackIds);
        if (ids.length === 0) return;

        dom.btnExportSelected.disabled = true;
        dom.btnExportSelected.textContent = 'Approving...';

        try {
            await apiFetch('/api/clip-analysis/' + state.analysisId + '/approve', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ track_ids: ids })
            });

            // Update local state
            ids.forEach(function(id) {
                var track = state.tracks.find(function(t) { return t.id === id; });
                if (track) track.review_status = 'approved';
            });

            state.selectedTrackIds.clear();
            renderTrackList();
            updateExportButton();

            // Update progress indicators
            updateReviewBadge();
            updateReviewProgress();

            // Show brief success feedback
            dom.btnExportSelected.textContent = 'Approved!';
            dom.btnExportSelected.disabled = true;
            setTimeout(function() { updateExportButton(); }, 2000);
        } catch (err) {
            alert('Failed to approve: ' + err.message);
            updateExportButton();
        }
    }

    // ── Bbox Correction Mode ────────────────────────────────────────────

    var correction = {
        active: false,
        trackId: null,
        bbox: null,       // {cx, cy, w, h} in canvas coords
        reason: null,
        dragMode: null,    // 'move', 'resize-tl', etc.
        dragOffX: 0,
        dragOffY: 0,
        HANDLE: 14,        // hit zone px
        MIN_DIM: 16        // minimum bbox dimension in canvas px
    };

    function enterCorrectionMode(trackId) {
        var track = state.tracks.find(function(t) { return t.id === trackId; });
        if (!track) return;

        // Pause video
        if (!dom.video.paused) dom.video.pause();

        correction.active = true;
        correction.trackId = trackId;
        correction.reason = null;
        state.highlightedTrackId = trackId;

        // Get current bbox in canvas coords
        var bbox = interpolateBbox(track, dom.video.currentTime);
        if (!bbox) {
            // Use first trajectory point
            var traj = track.trajectory;
            if (traj && traj.length > 0) {
                bbox = { x: traj[0].x, y: traj[0].y, w: traj[0].w, h: traj[0].h };
            } else {
                bbox = { x: 100, y: 100, w: 80, h: 60 };
            }
        }

        // Convert video coords to canvas coords
        var layout = getVideoLayout();
        correction.bbox = {
            cx: (bbox.x + bbox.w / 2) * layout.sx + layout.offsetX,
            cy: (bbox.y + bbox.h / 2) * layout.sy + layout.offsetY,
            w: bbox.w * layout.sx,
            h: bbox.h * layout.sy
        };

        // Show correction toolbar (no backdrop — it would block the canvas)
        var sheet = document.getElementById('issue-sheet');
        sheet.querySelectorAll('.reason-chip').forEach(function(c) { c.classList.remove('selected'); });
        var otherInput = document.getElementById('issue-other-input');
        otherInput.style.display = 'none';
        otherInput.value = '';
        sheet.classList.add('open');
        var submitBtn = document.getElementById('btn-submit-issue');
        submitBtn.disabled = false;
        submitBtn.textContent = 'Save Correction';

        // Update sheet heading
        var heading = sheet.querySelector('.sheet-heading');
        if (heading) heading.textContent = 'Correct Bbox \u2014 drag to reposition';

        // Elevate canvas above everything so drag works
        dom.overlayCanvas.style.zIndex = '200';
        dom.overlayCanvas.style.cursor = 'move';
        dom.overlayCanvas.addEventListener('mousedown', onCorrectionStart);
        dom.overlayCanvas.addEventListener('touchstart', onCorrectionStart, { passive: false });
        window.addEventListener('mousemove', onCorrectionMove);
        window.addEventListener('touchmove', onCorrectionMove, { passive: false });
        window.addEventListener('mouseup', onCorrectionEnd);
        window.addEventListener('touchend', onCorrectionEnd);

        renderTrackList();
    }

    function exitCorrectionMode() {
        correction.active = false;
        correction.trackId = null;
        correction.bbox = null;
        correction.reason = null;
        correction.dragMode = null;

        dom.overlayCanvas.style.cursor = '';
        dom.overlayCanvas.style.zIndex = '';
        dom.overlayCanvas.removeEventListener('mousedown', onCorrectionStart);
        dom.overlayCanvas.removeEventListener('touchstart', onCorrectionStart);
        window.removeEventListener('mousemove', onCorrectionMove);
        window.removeEventListener('touchmove', onCorrectionMove);
        window.removeEventListener('mouseup', onCorrectionEnd);
        window.removeEventListener('touchend', onCorrectionEnd);

        closeIssueSheet();
    }

    function getVideoLayout() {
        var vw = dom.video.videoWidth;
        var vh = dom.video.videoHeight;
        var canvas = dom.overlayCanvas;
        var displayW = canvas.width;
        var displayH = canvas.height;
        var videoAspect = vw / vh;
        var containerAspect = displayW / displayH;
        var renderW, renderH, offsetX, offsetY;
        if (videoAspect > containerAspect) {
            renderW = displayW; renderH = displayW / videoAspect; offsetX = 0; offsetY = (displayH - renderH) / 2;
        } else {
            renderH = displayH; renderW = displayH * videoAspect; offsetX = (displayW - renderW) / 2; offsetY = 0;
        }
        return { sx: renderW / vw, sy: renderH / vh, offsetX: offsetX, offsetY: offsetY, renderW: renderW, renderH: renderH, vw: vw, vh: vh };
    }

    function correctionHitTest(pos) {
        if (!correction.bbox) return 'move';
        var b = correction.bbox;
        var H = correction.HANDLE;
        var l = b.cx - b.w / 2, r = b.cx + b.w / 2;
        var t = b.cy - b.h / 2, bt = b.cy + b.h / 2;

        var nearL = Math.abs(pos.x - l) < H;
        var nearR = Math.abs(pos.x - r) < H;
        var nearT = Math.abs(pos.y - t) < H;
        var nearB = Math.abs(pos.y - bt) < H;

        if (nearT && nearL) return 'resize-tl';
        if (nearT && nearR) return 'resize-tr';
        if (nearB && nearL) return 'resize-bl';
        if (nearB && nearR) return 'resize-br';
        if (nearL && pos.y > t - H && pos.y < bt + H) return 'resize-l';
        if (nearR && pos.y > t - H && pos.y < bt + H) return 'resize-r';
        if (nearT && pos.x > l - H && pos.x < r + H) return 'resize-t';
        if (nearB && pos.x > l - H && pos.x < r + H) return 'resize-b';

        if (pos.x >= l && pos.x <= r && pos.y >= t && pos.y <= bt) return 'move';
        return null;
    }

    function getCanvasPos(e) {
        var cr = dom.overlayCanvas.getBoundingClientRect();
        var t = e.touches ? e.touches[0] : e;
        return { x: t.clientX - cr.left, y: t.clientY - cr.top };
    }

    function onCorrectionStart(e) {
        if (!correction.active || !correction.bbox) return;
        var pos = getCanvasPos(e);
        var mode = correctionHitTest(pos);
        if (!mode) return;
        e.preventDefault();
        e.stopPropagation();
        correction.dragMode = mode;
        correction.dragOffX = pos.x - correction.bbox.cx;
        correction.dragOffY = pos.y - correction.bbox.cy;
    }

    function onCorrectionMove(e) {
        if (!correction.dragMode || !correction.bbox) return;
        e.preventDefault();
        var pos = getCanvasPos(e);
        var b = correction.bbox;
        var MIN = correction.MIN_DIM;
        var l = b.cx - b.w / 2, r = b.cx + b.w / 2;
        var t = b.cy - b.h / 2, bt = b.cy + b.h / 2;

        if (correction.dragMode === 'move') {
            b.cx = pos.x - correction.dragOffX;
            b.cy = pos.y - correction.dragOffY;
        } else if (correction.dragMode === 'resize-tl') {
            if (r - pos.x > MIN) { b.w = r - pos.x; b.cx = pos.x + b.w / 2; }
            if (bt - pos.y > MIN) { b.h = bt - pos.y; b.cy = pos.y + b.h / 2; }
        } else if (correction.dragMode === 'resize-tr') {
            if (pos.x - l > MIN) { b.w = pos.x - l; b.cx = l + b.w / 2; }
            if (bt - pos.y > MIN) { b.h = bt - pos.y; b.cy = pos.y + b.h / 2; }
        } else if (correction.dragMode === 'resize-bl') {
            if (r - pos.x > MIN) { b.w = r - pos.x; b.cx = pos.x + b.w / 2; }
            if (pos.y - t > MIN) { b.h = pos.y - t; b.cy = t + b.h / 2; }
        } else if (correction.dragMode === 'resize-br') {
            if (pos.x - l > MIN) { b.w = pos.x - l; b.cx = l + b.w / 2; }
            if (pos.y - t > MIN) { b.h = pos.y - t; b.cy = t + b.h / 2; }
        } else if (correction.dragMode === 'resize-l') {
            if (r - pos.x > MIN) { b.w = r - pos.x; b.cx = pos.x + b.w / 2; }
        } else if (correction.dragMode === 'resize-r') {
            if (pos.x - l > MIN) { b.w = pos.x - l; b.cx = l + b.w / 2; }
        } else if (correction.dragMode === 'resize-t') {
            if (bt - pos.y > MIN) { b.h = bt - pos.y; b.cy = pos.y + b.h / 2; }
        } else if (correction.dragMode === 'resize-b') {
            if (pos.y - t > MIN) { b.h = pos.y - t; b.cy = t + b.h / 2; }
        }

        // Update cursor
        var cursorMap = {
            'resize-tl': 'nwse-resize', 'resize-br': 'nwse-resize',
            'resize-tr': 'nesw-resize', 'resize-bl': 'nesw-resize',
            'resize-l': 'ew-resize', 'resize-r': 'ew-resize',
            'resize-t': 'ns-resize', 'resize-b': 'ns-resize',
            'move': 'move'
        };
        dom.overlayCanvas.style.cursor = cursorMap[correction.dragMode] || 'move';
    }

    function onCorrectionEnd(e) {
        correction.dragMode = null;
    }

    function openIssueSheet(trackId) {
        enterCorrectionMode(trackId);
    }

    function closeIssueSheet() {
        var sheet = document.getElementById('issue-sheet');
        var backdrop = document.getElementById('issue-backdrop');
        sheet.classList.remove('open');
        backdrop.classList.remove('visible');
        backdrop.classList.add('hidden');
    }

    async function saveBboxCorrection() {
        if (!correction.active || !correction.bbox) return;
        var submitBtn = document.getElementById('btn-submit-issue');
        submitBtn.disabled = true;
        submitBtn.textContent = 'Saving...';

        // Convert canvas coords back to video coords
        var layout = getVideoLayout();
        var vidX = Math.round((correction.bbox.cx - correction.bbox.w / 2 - layout.offsetX) / layout.sx);
        var vidY = Math.round((correction.bbox.cy - correction.bbox.h / 2 - layout.offsetY) / layout.sy);
        var vidW = Math.round(correction.bbox.w / layout.sx);
        var vidH = Math.round(correction.bbox.h / layout.sy);

        var reason = correction.reason || 'bbox_corrected';
        var otherInput = document.getElementById('issue-other-input');
        if (correction.reason === 'other') {
            reason = otherInput.value.trim() || 'other';
        }

        try {
            await apiFetch('/api/clip-analysis/' + state.analysisId + '/tracks/' + correction.trackId + '/report-issue', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    reason: reason,
                    corrected_bbox: { x: vidX, y: vidY, w: vidW, h: vidH },
                    frame_time: dom.video.currentTime
                })
            });

            var track = state.tracks.find(function(t) { return t.id === correction.trackId; });
            if (track) {
                track.has_issue = true;
                track.corrected_bbox = { x: vidX, y: vidY, w: vidW, h: vidH };
            }

            exitCorrectionMode();
            renderTrackList();
            showToast('Bbox correction saved');
        } catch (err) {
            alert('Failed to save correction: ' + err.message);
            submitBtn.disabled = false;
            submitBtn.textContent = 'Save Correction';
        }
    }

    function initIssueSheet() {
        var sheet = document.getElementById('issue-sheet');
        var backdrop = document.getElementById('issue-backdrop');
        var otherInput = document.getElementById('issue-other-input');
        var submitBtn = document.getElementById('btn-submit-issue');
        var cancelBtn = document.getElementById('btn-cancel-correction');

        backdrop.addEventListener('click', function() {
            exitCorrectionMode();
        });
        if (cancelBtn) {
            cancelBtn.addEventListener('click', function() {
                exitCorrectionMode();
            });
        }

        sheet.querySelectorAll('.reason-chip').forEach(function(chip) {
            chip.addEventListener('click', function() {
                sheet.querySelectorAll('.reason-chip').forEach(function(c) { c.classList.remove('selected'); });
                chip.classList.add('selected');
                correction.reason = chip.dataset.reason;

                if (chip.dataset.reason === 'other') {
                    otherInput.style.display = '';
                    otherInput.focus();
                } else {
                    otherInput.style.display = 'none';
                }
            });
        });

        submitBtn.addEventListener('click', saveBboxCorrection);
    }

    async function reclassifyTrack(trackId, newClass) {
        try {
            await apiFetch('/api/clip-analysis/' + state.analysisId + '/tracks/' + trackId + '/reclassify', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ class_name: newClass })
            });
            var track = state.tracks.find(function(t) { return t.id === trackId; });
            if (track) {
                track.consensus_class = newClass;
                track.review_status = 'reviewed';
            }
            renderTrackList();
            renderDistribution();
            updateReviewBadge();
            updateReviewProgress();
        } catch (err) {
            alert('Failed to reclassify: ' + err.message);
        }
    }

    // ── Distribution Chart ────────────────────────────────────────────────

    function renderDistribution() {
        var counts = {};
        state.tracks.forEach(function(t) {
            var cls = t.consensus_class || 'unknown';
            counts[cls] = (counts[cls] || 0) + 1;
        });

        var entries = Object.entries(counts).sort(function(a, b) { return b[1] - a[1]; });
        var max = entries.length > 0 ? entries[0][1] : 1;

        dom.distributionBars.textContent = '';
        entries.forEach(function(entry) {
            var cls = entry[0];
            var count = entry[1];
            var row = document.createElement('div');
            row.className = 'distribution-row';

            var pct = Math.round((count / max) * 100);

            var label = document.createElement('span');
            label.className = 'dist-label';
            label.textContent = cls;
            row.appendChild(label);

            var barTrack = document.createElement('div');
            barTrack.className = 'dist-bar-track';
            var barFill = document.createElement('div');
            barFill.className = 'dist-bar-fill';
            barFill.style.width = pct + '%';
            barFill.style.background = classColor(cls);
            barTrack.appendChild(barFill);
            row.appendChild(barTrack);

            var countSpan = document.createElement('span');
            countSpan.className = 'dist-count';
            countSpan.textContent = count;
            row.appendChild(countSpan);

            dom.distributionBars.appendChild(row);
        });
    }

    // ── Overlay Player ────────────────────────────────────────────────────

    var player = {
        rafId: null,

        start: function() {
            this.stop();
            this.loop();
        },

        stop: function() {
            if (this.rafId) cancelAnimationFrame(this.rafId);
            this.rafId = null;
        },

        loop: function() {
            var self = this;
            this.drawOverlay();
            this.rafId = requestAnimationFrame(function() { self.loop(); });
        },

        drawOverlay: function() {
            var canvas = dom.overlayCanvas;
            var ctx = canvas.getContext('2d');
            var vw = dom.video.videoWidth;
            var vh = dom.video.videoHeight;
            if (!vw || !vh) return;

            // Ensure canvas matches display size
            var rect = dom.video.getBoundingClientRect();
            if (canvas.width !== Math.round(rect.width) || canvas.height !== Math.round(rect.height)) {
                canvas.width = Math.round(rect.width);
                canvas.height = Math.round(rect.height);
            }

            // Account for object-fit: contain letterboxing
            var displayW = canvas.width;
            var displayH = canvas.height;
            var videoAspect = vw / vh;
            var containerAspect = displayW / displayH;
            var renderW, renderH, offsetX, offsetY;

            if (videoAspect > containerAspect) {
                // Video is wider — letterboxed top/bottom
                renderW = displayW;
                renderH = displayW / videoAspect;
                offsetX = 0;
                offsetY = (displayH - renderH) / 2;
            } else {
                // Video is taller — pillarboxed left/right
                renderH = displayH;
                renderW = displayH * videoAspect;
                offsetX = (displayW - renderW) / 2;
                offsetY = 0;
            }

            var sx = renderW / vw;
            var sy = renderH / vh;
            var t = dom.video.currentTime;

            ctx.clearRect(0, 0, canvas.width, canvas.height);

            state.tracks.forEach(function(track) {
                var bbox = interpolateBbox(track, t);
                if (!bbox) return;

                var isHighlighted = state.highlightedTrackId === track.id;
                var color = classColor(track.consensus_class);
                var alpha = isHighlighted ? 1.0 : 0.5;

                var x = bbox.x * sx + offsetX;
                var y = bbox.y * sy + offsetY;
                var w = bbox.w * sx;
                var h = bbox.h * sy;

                ctx.strokeStyle = color;
                ctx.lineWidth = isHighlighted ? 3 : 1.5;
                ctx.globalAlpha = alpha;
                ctx.strokeRect(x, y, w, h);

                // Label
                var labelText = (track.consensus_class || '?');
                if (track.confidence != null) labelText += ' ' + Math.round(track.confidence * 100) + '%';
                ctx.font = (isHighlighted ? 'bold ' : '') + '12px system-ui, sans-serif';
                var tm = ctx.measureText(labelText);
                var lh = 16;
                var ly = y > lh + 2 ? y - 3 : y + h + lh;

                ctx.globalAlpha = alpha * 0.8;
                ctx.fillStyle = color;
                ctx.fillRect(x, ly - lh + 2, tm.width + 8, lh + 2);

                ctx.globalAlpha = alpha;
                ctx.fillStyle = '#000';
                ctx.fillText(labelText, x + 4, ly);
            });

            // Draw correction bbox if active
            if (correction.active && correction.bbox) {
                var cb = correction.bbox;
                var rx = cb.cx - cb.w / 2;
                var ry = cb.cy - cb.h / 2;

                // Cyan draggable box
                ctx.globalAlpha = 0.9;
                ctx.strokeStyle = '#00e5ff';
                ctx.lineWidth = 2.5;
                ctx.setLineDash([6, 3]);
                ctx.strokeRect(rx, ry, cb.w, cb.h);
                ctx.setLineDash([]);

                // Semi-transparent fill
                ctx.globalAlpha = 0.1;
                ctx.fillStyle = '#00e5ff';
                ctx.fillRect(rx, ry, cb.w, cb.h);

                // Corner handles
                ctx.globalAlpha = 0.9;
                ctx.fillStyle = '#00e5ff';
                var hs = 5;
                [[rx, ry], [rx + cb.w, ry], [rx, ry + cb.h], [rx + cb.w, ry + cb.h]].forEach(function(pt) {
                    ctx.fillRect(pt[0] - hs, pt[1] - hs, hs * 2, hs * 2);
                });

                // Edge midpoint handles
                ctx.fillRect(rx + cb.w / 2 - hs, ry - hs, hs * 2, hs * 2);
                ctx.fillRect(rx + cb.w / 2 - hs, ry + cb.h - hs, hs * 2, hs * 2);
                ctx.fillRect(rx - hs, ry + cb.h / 2 - hs, hs * 2, hs * 2);
                ctx.fillRect(rx + cb.w - hs, ry + cb.h / 2 - hs, hs * 2, hs * 2);

                // Crosshair at center
                ctx.strokeStyle = '#00e5ff';
                ctx.lineWidth = 1;
                ctx.globalAlpha = 0.6;
                ctx.beginPath();
                ctx.moveTo(cb.cx - 8, cb.cy);
                ctx.lineTo(cb.cx + 8, cb.cy);
                ctx.moveTo(cb.cx, cb.cy - 8);
                ctx.lineTo(cb.cx, cb.cy + 8);
                ctx.stroke();

                // Label
                ctx.globalAlpha = 0.9;
                ctx.font = 'bold 11px system-ui, sans-serif';
                ctx.fillStyle = '#00e5ff';
                ctx.fillText('CORRECTED', rx + 2, ry - 6);
            }

            ctx.globalAlpha = 1.0;
        }
    };

    function interpolateBbox(track, time) {
        var traj = track.trajectory;
        if (!traj || traj.length === 0) return null;

        var before = null;
        var after = null;
        for (var i = 0; i < traj.length; i++) {
            var pt = traj[i];
            if (pt.timestamp <= time) before = pt;
            if (pt.timestamp >= time && !after) after = pt;
        }

        if (!before && !after) return null;

        // Don't extrapolate beyond actual detections - only show near edges
        var SNAP_TOLERANCE = 0.15; // seconds
        if (!before) {
            return (after.timestamp - time) <= SNAP_TOLERANCE ? bboxOf(after) : null;
        }
        if (!after) {
            return (time - before.timestamp) <= SNAP_TOLERANCE ? bboxOf(before) : null;
        }

        if (before === after || before.timestamp === after.timestamp) return bboxOf(before);

        // Don't interpolate across large gaps (object wasn't detected)
        var gap = after.timestamp - before.timestamp;
        var MAX_GAP = 0.5; // seconds — at 30fps this is ~15 frames
        if (gap > MAX_GAP) return null;

        // Linearly interpolate between nearby detections
        var frac = (time - before.timestamp) / gap;
        return {
            x: before.x + (after.x - before.x) * frac,
            y: before.y + (after.y - before.y) * frac,
            w: before.w + (after.w - before.w) * frac,
            h: before.h + (after.h - before.h) * frac
        };
    }

    function bboxOf(pt) {
        return { x: pt.x, y: pt.y, w: pt.w, h: pt.h };
    }

    function onOverlayClick(e) {
        // Don't change highlight during correction mode
        if (correction.active) return;
        var canvas = dom.overlayCanvas;
        var rect = canvas.getBoundingClientRect();
        var cx = e.clientX - rect.left;
        var cy = e.clientY - rect.top;
        var vw = dom.video.videoWidth;
        var vh = dom.video.videoHeight;
        if (!vw || !vh) return;

        // Account for object-fit: contain letterboxing
        var displayW = canvas.width;
        var displayH = canvas.height;
        var videoAspect = vw / vh;
        var containerAspect = displayW / displayH;
        var renderW, renderH, offsetX, offsetY;
        if (videoAspect > containerAspect) {
            renderW = displayW;
            renderH = displayW / videoAspect;
            offsetX = 0;
            offsetY = (displayH - renderH) / 2;
        } else {
            renderH = displayH;
            renderW = displayH * videoAspect;
            offsetX = (displayW - renderW) / 2;
            offsetY = 0;
        }

        var sx = renderW / vw;
        var sy = renderH / vh;
        var t = dom.video.currentTime;

        var found = null;
        for (var i = state.tracks.length - 1; i >= 0; i--) {
            var track = state.tracks[i];
            var bbox = interpolateBbox(track, t);
            if (!bbox) continue;
            var x = bbox.x * sx + offsetX;
            var y = bbox.y * sy + offsetY;
            var w = bbox.w * sx;
            var h = bbox.h * sy;
            if (cx >= x && cx <= x + w && cy >= y && cy <= y + h) {
                found = track.id;
                break;
            }
        }

        state.highlightedTrackId = found === state.highlightedTrackId ? null : found;
        $$('.track-item').forEach(function(el) {
            el.classList.toggle('highlighted', el.dataset.trackId == state.highlightedTrackId);
        });
    }

    // ── Classification Timeline ───────────────────────────────────────────

    var timeline = {
        data: [],
        _lastRenderedSegments: null,

        draw: function() {
            this.loadData();
        },

        loadData: async function() {
            if (state.tracks.length === 0) return;
            var tid = state.highlightedTrackId || (state.tracks.length > 0 ? state.tracks[0].id : null);
            if (!tid) return;

            try {
                var data = await apiFetch('/api/clip-analysis/' + state.analysisId + '/tracks/' + tid + '/timeline');
                this.data = data.segments || [];
                this.render();
            } catch (err) {
                console.error('Failed to load timeline:', err);
                this.data = [];
                this.render();
            }
        },

        render: function() {
            var canvas = dom.timelineCanvas;
            var parent = canvas.parentElement;
            canvas.width = parent.clientWidth;
            canvas.height = 60;
            var ctx = canvas.getContext('2d');
            var w = canvas.width;
            var h = canvas.height;
            var dur = dom.video.duration || 1;

            ctx.clearRect(0, 0, w, h);

            // Background
            ctx.fillStyle = 'rgba(255,255,255,0.03)';
            ctx.fillRect(0, 0, w, h);

            // Draw segments
            var segments = this.data;
            for (var i = 0; i < segments.length; i++) {
                var seg = segments[i];
                var x0 = (seg.start / dur) * w;
                var x1 = (seg.end / dur) * w;
                var color = classColor(seg.class_name);
                var alpha = seg.weight != null ? Math.max(0.2, Math.min(1.0, seg.weight)) : 0.7;

                ctx.globalAlpha = alpha;
                ctx.fillStyle = color;
                ctx.fillRect(x0, 4, Math.max(x1 - x0, 2), h - 8);
            }

            ctx.globalAlpha = 1.0;

            // Border
            ctx.strokeStyle = 'rgba(255,255,255,0.1)';
            ctx.lineWidth = 1;
            ctx.strokeRect(0, 0, w, h);
        },

        drawPlayhead: function(currentTime, duration) {
            // Re-render base then overlay playhead
            this.render();

            var canvas = dom.timelineCanvas;
            var ctx = canvas.getContext('2d');
            var w = canvas.width;
            var h = canvas.height;
            var dur = duration || dom.video.duration || 1;

            var x = (currentTime / dur) * w;

            ctx.beginPath();
            ctx.strokeStyle = '#fff';
            ctx.lineWidth = 2;
            ctx.moveTo(x, 0);
            ctx.lineTo(x, h);
            ctx.stroke();

            // Small triangle at top
            ctx.fillStyle = '#fff';
            ctx.beginPath();
            ctx.moveTo(x - 5, 0);
            ctx.lineTo(x + 5, 0);
            ctx.lineTo(x, 6);
            ctx.closePath();
            ctx.fill();
        }
    };

    function onTimelineClick(e) {
        var canvas = dom.timelineCanvas;
        var rect = canvas.getBoundingClientRect();
        var x = e.clientX - rect.left;
        var frac = x / canvas.width;
        var dur = dom.video.duration || 0;
        if (dur > 0) {
            dom.video.currentTime = frac * dur;
        }
    }

    // Watch for highlighted track changes to reload timeline
    var lastHighlightedForTimeline = null;
    function checkTimelineTrackChange() {
        if (state.highlightedTrackId !== lastHighlightedForTimeline) {
            lastHighlightedForTimeline = state.highlightedTrackId;
            timeline.draw();
        }
    }

    // ══════════════════════════════════════════════════════════════════════
    //  SCREEN 3: EXPORT
    // ══════════════════════════════════════════════════════════════════════

    function initExportScreen() {
        dom.btnBackReview.addEventListener('click', function() {
            showScreen('review-screen');
            player.start();
        });

        dom.sliderTopN.addEventListener('input', function() {
            dom.topnValue.textContent = dom.sliderTopN.value;
            loadExportPreview();
        });
        dom.sliderQuality.addEventListener('input', function() {
            dom.qualityValue.textContent = (parseInt(dom.sliderQuality.value, 10) / 100).toFixed(2);
            loadExportPreview();
        });

        dom.btnConfirmExport.addEventListener('click', confirmExport);
    }

    function enterExportScreen() {
        showScreen('export-screen');
        player.stop();
        dom.exportSuccess.classList.add('hidden');
        dom.btnConfirmExport.disabled = false;
        dom.btnConfirmExport.style.display = '';
        dom.btnConfirmExport.textContent = 'Export as Pending Predictions';
        dom.exportPreview.textContent = '';
        appendEmptyState(dom.exportPreview, 'Loading preview...');
        loadExportPreview();
    }

    async function loadExportPreview() {
        var topN = parseInt(dom.sliderTopN.value, 10);
        var minQuality = parseInt(dom.sliderQuality.value, 10) / 100;
        var trackIds = Array.from(state.selectedTrackIds);

        try {
            var data = await apiFetch('/api/clip-analysis/' + state.analysisId + '/export-training?' +
                'top_n=' + topN +
                '&min_quality=' + minQuality +
                '&track_ids=' + encodeURIComponent(trackIds.join(',')));

            renderExportPreview(data.frames || []);
        } catch (err) {
            dom.exportPreview.textContent = '';
            appendEmptyState(dom.exportPreview, 'Preview failed: ' + err.message, true);
        }
    }

    function renderExportPreview(frames) {
        dom.exportPreview.textContent = '';
        if (frames.length === 0) {
            appendEmptyState(dom.exportPreview, 'No frames match the current criteria. Try lowering the quality threshold.');
            return;
        }
        frames.forEach(function(f) {
            var card = document.createElement('div');
            card.className = 'preview-frame';

            var img = document.createElement('img');
            img.src = f.image_url;
            img.alt = 'frame';
            card.appendChild(img);

            var info = document.createElement('div');
            info.className = 'preview-frame-info';

            var badge = document.createElement('span');
            badge.className = 'badge';
            badge.style.background = classColor(f.class_name);
            badge.textContent = f.class_name || '';
            info.appendChild(badge);

            var quality = document.createElement('span');
            quality.className = 'preview-quality';
            quality.textContent = 'Q: ' + (f.quality != null ? f.quality.toFixed(2) : '?');
            info.appendChild(quality);

            card.appendChild(info);
            dom.exportPreview.appendChild(card);
        });
    }

    async function confirmExport() {
        dom.btnConfirmExport.disabled = true;
        dom.btnConfirmExport.textContent = 'Exporting...';

        try {
            var data = await apiFetch('/api/clip-analysis/' + state.analysisId + '/export-training', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    track_ids: Array.from(state.selectedTrackIds),
                    top_n: parseInt(dom.sliderTopN.value, 10),
                    min_quality: parseInt(dom.sliderQuality.value, 10) / 100
                })
            });

            var count = data.exported_count || 0;
            dom.exportResultText.textContent = count + ' frame' + (count !== 1 ? 's' : '') + ' exported as pending predictions.';
            dom.exportSuccess.classList.remove('hidden');
            dom.btnConfirmExport.style.display = 'none';
        } catch (err) {
            dom.btnConfirmExport.disabled = false;
            dom.btnConfirmExport.textContent = 'Export as Pending Predictions';
            alert('Export failed: ' + err.message);
        }
    }

    // ── Loading Overlay ───────────────────────────────────────────────────

    function showLoading(text) {
        dom.loadingText.textContent = text || 'Loading...';
        dom.loadingOverlay.classList.remove('hidden');
    }

    function hideLoading() {
        dom.loadingOverlay.classList.add('hidden');
    }

    function showToast(message) {
        var toast = document.createElement('div');
        toast.className = 'toast-notification';
        toast.textContent = message;
        document.body.appendChild(toast);
        // Trigger reflow then add visible class for animation
        toast.offsetHeight;
        toast.classList.add('visible');
        setTimeout(function() {
            toast.classList.remove('visible');
            setTimeout(function() { toast.remove(); }, 300);
        }, 4000);
    }

    // ── Resize handling ───────────────────────────────────────────────────

    function onResize() {
        if (dom.overlayCanvas && dom.video.videoWidth) {
            resizeOverlayCanvas();
        }
        if (dom.timelineCanvas && state.analysisId) {
            timeline.render();
            timeline.drawPlayhead(dom.video.currentTime, dom.video.duration);
        }
    }

    // ── Periodic checks ───────────────────────────────────────────────────

    function tick() {
        checkTimelineTrackChange();
        requestAnimationFrame(tick);
    }

    // ── Init ──────────────────────────────────────────────────────────────

    function init() {
        cacheDom();
        initSourceScreen();
        initReviewScreen();
        initExportScreen();
        initIssueSheet();

        window.addEventListener('resize', onResize);
        tick();

        // Hamburger menu toggle (nav partial)
        var hamburger = document.getElementById('hamburger-btn');
        var navLinks = document.getElementById('nav-links');
        if (hamburger && navLinks) {
            hamburger.addEventListener('click', function() {
                var open = navLinks.classList.toggle('open');
                hamburger.setAttribute('aria-expanded', String(open));
            });
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
