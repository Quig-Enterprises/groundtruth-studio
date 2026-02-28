/**
 * Cross-Camera Review - Swipe-based validation UI
 * Validates cross-camera vehicle matches with confirm/reject/skip actions.
 */
(function() {
    'use strict';

    // ── State ──────────────────────────────────────────────────────────
    const state = {
        links: [],
        filtered: [],
        currentIndex: 0,
        history: [],
        stats: { confirmed: 0, rejected: 0, skipped: 0 },
        activeFilter: 'all',
        pendingRejectId: null,
        isDragging: false,
        dragStartX: 0,
        dragCurrentX: 0,
        isAnimating: false
    };

    // ── DOM Refs ───────────────────────────────────────────────────────
    const $ = id => document.getElementById(id);
    const dom = {};

    function cacheDom() {
        dom.queueScreen    = $('queue-screen');
        dom.reviewScreen   = $('review-screen');
        dom.summaryScreen  = $('summary-screen');
        dom.pendingCount   = $('pending-count');
        dom.startBtn       = $('start-review');
        dom.reviewCount    = $('review-count');
        dom.progressBar    = $('progress-bar');
        dom.cardStage      = $('card-stage');
        dom.card           = $('current-card');
        dom.imgA           = $('img-a');
        dom.imgB           = $('img-b');
        dom.badgeCamA      = $('badge-camera-a');
        dom.badgeCamB      = $('badge-camera-b');
        dom.badgeTimeA     = $('badge-time-a');
        dom.badgeTimeB     = $('badge-time-b');
        dom.badgeClassA    = $('badge-class-a');
        dom.badgeClassB    = $('badge-class-b');
        dom.confidenceBadge = $('confidence-badge');
        dom.glowLeft       = $('glow-left');
        dom.glowRight      = $('glow-right');
        dom.btnReject      = $('btn-reject');
        dom.btnSkip        = $('btn-skip');
        dom.btnConfirm     = $('btn-confirm');
        dom.undoBar        = $('undo-bar');
        dom.btnUndo        = $('btn-undo');
        dom.btnCopyDebug   = $('btn-copy-debug');
        dom.rejectSheet    = $('reject-sheet');
        dom.sheetBackdrop  = $('sheet-backdrop');
        dom.otherContainer = $('other-input-container');
        dom.otherInput     = $('other-input');
        dom.skipFeedback   = $('skip-feedback');
        dom.doneFeedback   = $('done-feedback');
        dom.completionCount    = $('completion-count');
        dom.confirmedCount     = $('confirmed-count');
        dom.rejectedCount      = $('rejected-count');
        dom.skippedCount       = $('skipped-count');
        dom.completionRingFill = $('completion-ring-fill');
        dom.backToQueue        = $('back-to-queue');
        dom.reviewSkipped      = $('review-skipped');
    }

    // ── API ────────────────────────────────────────────────────────────
    async function fetchQueue() {
        try {
            const resp = await fetch('/api/ai/cross-camera/review-queue?status=auto');
            const data = await resp.json();
            if (data.success) return data.links;
        } catch (e) {
            console.error('Failed to fetch review queue:', e);
        }
        return [];
    }

    async function apiConfirm(linkId) {
        const resp = await fetch(`/api/ai/cross-camera/links/${linkId}/confirm`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ reject: false })
        });
        return resp.json();
    }

    async function apiReject(linkId, reason) {
        const resp = await fetch(`/api/ai/cross-camera/links/${linkId}/confirm`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ reject: true, rejection_reason: reason || null })
        });
        return resp.json();
    }

    async function apiUndo(linkId) {
        // Reset link back to 'auto' status
        const resp = await fetch(`/api/ai/cross-camera/links/${linkId}/confirm`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ reject: false })
        });
        // We reuse confirm endpoint but we actually need to set back to auto
        // Since the API only supports confirmed/rejected, we'll handle undo client-side
        // by tracking the link and re-showing it
        return resp.json();
    }

    // ── Screen Navigation ──────────────────────────────────────────────
    function showScreen(screen) {
        [dom.queueScreen, dom.reviewScreen, dom.summaryScreen].forEach(s => {
            s.classList.add('hidden');
        });
        screen.classList.remove('hidden');
    }

    // ── Queue ──────────────────────────────────────────────────────────
    async function loadQueue() {
        state.links = await fetchQueue();
        applyFilter();
        dom.pendingCount.textContent = state.filtered.length;
        dom.startBtn.disabled = state.filtered.length === 0;
    }

    function applyFilter() {
        const f = state.activeFilter;
        if (f === 'all') {
            state.filtered = [...state.links];
        } else if (f === 'high') {
            state.filtered = state.links.filter(l => l.match_confidence >= 0.7);
        } else if (f === 'low') {
            state.filtered = state.links.filter(l => l.match_confidence < 0.7);
        }
    }

    // ── Card Rendering ─────────────────────────────────────────────────
    function formatTime(val) {
        if (!val) return '—';
        try {
            // Handle epoch seconds (numeric strings like "1771162000.0") and ISO strings
            const num = parseFloat(val);
            const d = (!isNaN(num) && num > 1e9) ? new Date(num * 1000) : new Date(val);
            if (isNaN(d.getTime())) return '—';
            return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        } catch { return '—'; }
    }

    function formatClassification(cls) {
        if (!cls) return null;
        if (typeof cls === 'string') return cls;
        // cls is a JSON object like {"vehicle_subtype": "trailer"}
        return cls.vehicle_subtype || cls.class || cls.label || null;
    }

    function cropUrl(predId) {
        if (!predId) return '';
        return `/thumbnails/crop/${predId}`;
    }

    function linkCropUrl(link, side) {
        // For video_track links, use the crop endpoint; for camera_object, use prediction crop
        if (link.source_track_type === 'video_track') {
            const trackId = side === 'a' ? link.track_a_id : link.track_b_id;
            return trackId ? `/api/ai/video-tracks/${trackId}/crop` : '';
        }
        const predId = side === 'a' ? link.pred_id_a : link.pred_id_b;
        return cropUrl(predId);
    }

    function renderCard(link) {
        if (!link) return;

        // Camera A (top)
        const urlA = linkCropUrl(link, 'a');
        if (urlA) {
            dom.imgA.src = urlA;
            dom.imgA.style.display = '';
        } else {
            dom.imgA.src = '';
            dom.imgA.alt = 'No image available';
        }
        dom.badgeCamA.textContent = link.camera_a || 'Camera A';
        dom.badgeTimeA.textContent = formatTime(link.first_seen_a);
        const clsTextA = formatClassification(link.cls_a);
        if (clsTextA) {
            dom.badgeClassA.textContent = clsTextA;
            dom.badgeClassA.style.display = '';
        } else {
            dom.badgeClassA.style.display = 'none';
        }

        // Camera B (bottom)
        const urlB = linkCropUrl(link, 'b');
        if (urlB) {
            dom.imgB.src = urlB;
            dom.imgB.style.display = '';
        } else {
            dom.imgB.src = '';
            dom.imgB.alt = 'No image available';
        }
        dom.badgeCamB.textContent = link.camera_b || 'Camera B';
        dom.badgeTimeB.textContent = formatTime(link.first_seen_b);
        const clsTextB = formatClassification(link.cls_b);
        if (clsTextB) {
            dom.badgeClassB.textContent = clsTextB;
            dom.badgeClassB.style.display = '';
        } else {
            dom.badgeClassB.style.display = 'none';
        }

        // Confidence
        const conf = Math.round((link.match_confidence || 0) * 100);
        dom.confidenceBadge.textContent = `${conf}% match`;
        dom.confidenceBadge.className = 'confidence-badge';
        if (conf >= 70) dom.confidenceBadge.classList.add('high');
        else if (conf >= 40) dom.confidenceBadge.classList.add('medium');
        else dom.confidenceBadge.classList.add('low');

        // Reset card position
        dom.card.className = 'card';
        dom.card.style.transform = '';
        dom.card.style.opacity = '';

        updateProgress();
    }

    function updateProgress() {
        const total = state.filtered.length;
        const current = state.currentIndex;
        dom.reviewCount.textContent = `${current + 1}/${total}`;
        const pct = total > 0 ? ((current) / total) * 100 : 0;
        dom.progressBar.style.width = `${pct}%`;
    }

    function preloadNext() {
        const nextIdx = state.currentIndex + 1;
        if (nextIdx < state.filtered.length) {
            const next = state.filtered[nextIdx];
            const urlA = linkCropUrl(next, 'a');
            const urlB = linkCropUrl(next, 'b');
            if (urlA) new Image().src = urlA;
            if (urlB) new Image().src = urlB;
        }
    }

    // ── Actions ────────────────────────────────────────────────────────
    function currentLink() {
        return state.filtered[state.currentIndex];
    }

    async function confirmCurrent() {
        if (state.isAnimating) return;
        const link = currentLink();
        if (!link) return;

        state.isAnimating = true;
        state.history.push({ linkId: link.id, action: 'confirmed', index: state.currentIndex });
        state.stats.confirmed++;

        // Animate out right
        dom.card.classList.add('exit-right');
        apiConfirm(link.id).catch(e => console.error('Confirm failed:', e));

        setTimeout(() => advanceCard(), 400);
    }

    function rejectCurrent() {
        if (state.isAnimating) return;
        const link = currentLink();
        if (!link) return;

        state.pendingRejectId = link.id;
        openRejectSheet();
    }

    function completeRejection(reason) {
        const linkId = state.pendingRejectId;
        if (!linkId) return;

        state.isAnimating = true;
        state.history.push({ linkId, action: 'rejected', reason, index: state.currentIndex });
        state.stats.rejected++;

        closeRejectSheet();

        dom.card.classList.add('exit-left');
        apiReject(linkId, reason).catch(e => console.error('Reject failed:', e));

        setTimeout(() => advanceCard(), 400);
        state.pendingRejectId = null;
    }

    function skipCurrent() {
        if (state.isAnimating) return;
        const link = currentLink();
        if (!link) return;

        state.isAnimating = true;
        state.history.push({ linkId: link.id, action: 'skipped', index: state.currentIndex });
        state.stats.skipped++;

        dom.card.style.transition = 'opacity 0.3s ease';
        dom.card.style.opacity = '0';

        setTimeout(() => advanceCard(), 300);
    }

    function advanceCard() {
        state.currentIndex++;
        showUndoBar();

        if (state.currentIndex >= state.filtered.length) {
            showSummary();
            return;
        }

        // Animate in new card
        dom.card.className = 'card enter';
        renderCard(state.filtered[state.currentIndex]);

        requestAnimationFrame(() => {
            requestAnimationFrame(() => {
                dom.card.className = 'card';
                state.isAnimating = false;
            });
        });

        preloadNext();
    }

    function undoLast() {
        if (state.history.length === 0) return;

        const last = state.history.pop();

        // Undo stats
        if (last.action === 'confirmed') state.stats.confirmed--;
        else if (last.action === 'rejected') state.stats.rejected--;
        else if (last.action === 'skipped') state.stats.skipped--;

        // Go back to that card
        state.currentIndex = last.index;

        // If we were on summary, go back to review
        if (!dom.summaryScreen.classList.contains('hidden')) {
            showScreen(dom.reviewScreen);
        }

        renderCard(state.filtered[state.currentIndex]);
        state.isAnimating = false;

        if (state.history.length === 0) hideUndoBar();
    }

    function showUndoBar() {
        dom.undoBar.classList.remove('hidden');
        // Auto-hide after 5s
        clearTimeout(state.undoTimer);
        state.undoTimer = setTimeout(() => hideUndoBar(), 5000);
    }

    function hideUndoBar() {
        dom.undoBar.classList.add('hidden');
    }

    // ── Debug Copy ──────────────────────────────────────────────────────
    function copyDebugInfo() {
        const link = currentLink();
        if (!link) { showToast('No link loaded'); return; }

        const clsA = formatClassification(link.cls_a);
        const clsB = formatClassification(link.cls_b);
        const lines = [
            'Link ID: ' + link.id,
            'Track A: ' + link.track_a_id + ' (' + link.camera_a + ')',
            'Track B: ' + link.track_b_id + ' (' + link.camera_b + ')',
            'Match Confidence: ' + link.match_confidence,
            'ReID Similarity: ' + (link.reid_similarity || 'N/A'),
            'Temporal Gap: ' + (link.temporal_gap_seconds || 0) + 's',
            'Match Method: ' + (link.match_method || 'N/A'),
            'Entity Type: ' + (link.entity_type || 'N/A'),
            'Classification A: ' + (clsA || 'N/A'),
            'Classification B: ' + (clsB || 'N/A'),
            'Classification Match: ' + link.classification_match,
            'First Seen A: ' + (link.first_seen_a || 'N/A'),
            'First Seen B: ' + (link.first_seen_b || 'N/A'),
            'Last Seen A: ' + (link.last_seen_a || 'N/A'),
            'Last Seen B: ' + (link.last_seen_b || 'N/A'),
            'Members A: ' + (link.members_a || 'N/A'),
            'Members B: ' + (link.members_b || 'N/A'),
            'Pred ID A: ' + (link.pred_id_a || 'N/A'),
            'Pred ID B: ' + (link.pred_id_b || 'N/A'),
            'Status: ' + (link.status || 'N/A'),
            'Crop A: ' + cropUrl(link.pred_id_a),
            'Crop B: ' + cropUrl(link.pred_id_b),
        ];
        const text = lines.join('\n');
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(() => showToast('Debug info copied'))
                .catch(() => fallbackCopy(text));
        } else {
            fallbackCopy(text);
        }
    }

    function fallbackCopy(text) {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.cssText = 'position:fixed;left:-9999px';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
        showToast('Debug info copied');
    }

    function showToast(msg) {
        const existing = document.querySelector('.debug-toast');
        if (existing) existing.remove();
        const toast = document.createElement('div');
        toast.className = 'debug-toast';
        toast.textContent = msg;
        toast.style.cssText = 'position:fixed;bottom:100px;left:50%;transform:translateX(-50%);background:#333;color:#fff;padding:8px 16px;border-radius:20px;font-size:14px;z-index:10000;opacity:0;transition:opacity 0.3s;';
        document.body.appendChild(toast);
        requestAnimationFrame(() => { toast.style.opacity = '1'; });
        setTimeout(() => {
            toast.style.opacity = '0';
            setTimeout(() => toast.remove(), 300);
        }, 2000);
    }

    // ── Rejection Sheet ────────────────────────────────────────────────
    function openRejectSheet() {
        dom.rejectSheet.classList.add('open');
        dom.sheetBackdrop.classList.remove('hidden');
        dom.sheetBackdrop.classList.add('visible');
        // Reset chips
        dom.rejectSheet.querySelectorAll('.reason-chip').forEach(c => c.classList.remove('selected'));
        dom.otherContainer.classList.add('hidden');
        dom.otherInput.value = '';
    }

    function closeRejectSheet() {
        dom.rejectSheet.classList.remove('open');
        dom.sheetBackdrop.classList.add('hidden');
        dom.sheetBackdrop.classList.remove('visible');
        state.pendingRejectId = null;
    }

    function getSelectedReason() {
        const selected = dom.rejectSheet.querySelector('.reason-chip.selected');
        if (!selected) return null;
        const reason = selected.dataset.reason;
        if (reason === 'other') return dom.otherInput.value.trim() || 'other';
        return reason;
    }

    // ── Summary Screen ─────────────────────────────────────────────────
    function showSummary() {
        state.isAnimating = false;
        showScreen(dom.summaryScreen);

        const total = state.stats.confirmed + state.stats.rejected + state.stats.skipped;
        dom.completionCount.textContent = total;
        dom.confirmedCount.textContent = state.stats.confirmed;
        dom.rejectedCount.textContent = state.stats.rejected;
        dom.skippedCount.textContent = state.stats.skipped;

        // Animate ring
        const circumference = 439.82;
        const reviewed = state.stats.confirmed + state.stats.rejected;
        const pct = total > 0 ? reviewed / total : 0;
        const offset = circumference * (1 - pct);
        requestAnimationFrame(() => {
            dom.completionRingFill.style.transition = 'stroke-dashoffset 1s ease';
            dom.completionRingFill.style.strokeDashoffset = offset;
        });
    }

    // ── Swipe Gesture Engine ───────────────────────────────────────────
    const SWIPE_THRESHOLD = 80;

    function onPointerDown(e) {
        if (state.isAnimating) return;
        state.isDragging = true;
        state.dragStartX = e.clientX;
        state.dragCurrentX = e.clientX;
        dom.card.classList.add('dragging');
    }

    function onPointerMove(e) {
        if (!state.isDragging) return;
        state.dragCurrentX = e.clientX;
        const dx = state.dragCurrentX - state.dragStartX;
        const rotation = dx * 0.05;
        const maxRotation = 12;
        const clampedRotation = Math.max(-maxRotation, Math.min(maxRotation, rotation));

        dom.card.style.transform = `translateX(${dx}px) rotate(${clampedRotation}deg)`;

        // Edge glows
        const progress = Math.min(Math.abs(dx) / SWIPE_THRESHOLD, 1);
        if (dx < 0) {
            dom.glowLeft.style.opacity = progress;
            dom.glowRight.style.opacity = 0;
        } else if (dx > 0) {
            dom.glowRight.style.opacity = progress;
            dom.glowLeft.style.opacity = 0;
        }
    }

    function onPointerUp() {
        if (!state.isDragging) return;
        state.isDragging = false;
        dom.card.classList.remove('dragging');

        const dx = state.dragCurrentX - state.dragStartX;

        // Reset glows
        dom.glowLeft.style.opacity = 0;
        dom.glowRight.style.opacity = 0;

        if (dx > SWIPE_THRESHOLD) {
            confirmCurrent();
        } else if (dx < -SWIPE_THRESHOLD) {
            rejectCurrent();
        } else {
            // Snap back
            dom.card.style.transition = 'transform 0.3s ease';
            dom.card.style.transform = '';
            setTimeout(() => { dom.card.style.transition = ''; }, 300);
        }
    }

    // ── Keyboard Shortcuts ─────────────────────────────────────────────
    function onKeyDown(e) {
        // Don't handle if typing in input
        if (e.target.tagName === 'INPUT') return;
        // Don't handle if reject sheet is open (except Escape)
        const sheetOpen = dom.rejectSheet.classList.contains('open');

        if (e.key === 'Escape' && sheetOpen) {
            closeRejectSheet();
            return;
        }

        if (sheetOpen) return;
        if (dom.reviewScreen.classList.contains('hidden')) return;

        switch (e.key) {
            case 'ArrowRight':
            case 'd':
            case 'D':
                e.preventDefault();
                confirmCurrent();
                break;
            case 'ArrowLeft':
            case 'a':
            case 'A':
                e.preventDefault();
                rejectCurrent();
                break;
            case 'ArrowDown':
            case 's':
            case 'S':
                e.preventDefault();
                skipCurrent();
                break;
            case 'z':
            case 'Z':
                if (e.ctrlKey || e.metaKey || e.key === 'z') {
                    e.preventDefault();
                    undoLast();
                }
                break;
        }
    }

    // ── Event Binding ──────────────────────────────────────────────────
    function bindEvents() {
        // Queue screen
        dom.startBtn.addEventListener('click', () => {
            state.currentIndex = 0;
            state.history = [];
            state.stats = { confirmed: 0, rejected: 0, skipped: 0 };
            showScreen(dom.reviewScreen);
            renderCard(state.filtered[0]);
            preloadNext();
        });

        // Filter chips
        document.querySelectorAll('.filter-chip[data-filter]').forEach(chip => {
            chip.addEventListener('click', () => {
                document.querySelectorAll('.filter-chip[data-filter]').forEach(c => c.classList.remove('active'));
                chip.classList.add('active');
                state.activeFilter = chip.dataset.filter;
                applyFilter();
                dom.pendingCount.textContent = state.filtered.length;
                dom.startBtn.disabled = state.filtered.length === 0;
            });
        });

        // Review screen back
        $('review-back').addEventListener('click', () => showScreen(dom.queueScreen));
        $('queue-back').addEventListener('click', () => { window.location.href = '/'; });

        // Action buttons
        dom.btnConfirm.addEventListener('click', confirmCurrent);
        dom.btnReject.addEventListener('click', rejectCurrent);
        dom.btnSkip.addEventListener('click', skipCurrent);
        dom.btnUndo.addEventListener('click', undoLast);
        dom.btnCopyDebug.addEventListener('click', copyDebugInfo);

        // Swipe gestures
        dom.card.addEventListener('pointerdown', onPointerDown);
        document.addEventListener('pointermove', onPointerMove);
        document.addEventListener('pointerup', onPointerUp);

        // Keyboard
        document.addEventListener('keydown', onKeyDown);

        // Populate rejection reason chips dynamically
        var ccReasonsContainer = dom.rejectSheet.querySelector('.rejection-reasons');
        if (ccReasonsContainer) {
            ccReasonsContainer.appendChild(RejectReasons.buildChips('cross_camera'));
        }

        // Rejection sheet
        dom.rejectSheet.querySelectorAll('.reason-chip').forEach(chip => {
            chip.addEventListener('click', () => {
                dom.rejectSheet.querySelectorAll('.reason-chip').forEach(c => c.classList.remove('selected'));
                chip.classList.add('selected');
                if (chip.dataset.reason === 'other') {
                    dom.otherContainer.classList.remove('hidden');
                    dom.otherInput.focus();
                } else {
                    dom.otherContainer.classList.add('hidden');
                }
            });
        });

        dom.skipFeedback.addEventListener('click', () => {
            completeRejection(null);
        });

        dom.doneFeedback.addEventListener('click', () => {
            const reason = getSelectedReason();
            completeRejection(reason);
        });

        dom.sheetBackdrop.addEventListener('click', closeRejectSheet);

        // Summary screen
        dom.backToQueue.addEventListener('click', () => {
            showScreen(dom.queueScreen);
            loadQueue();
        });

        dom.reviewSkipped.addEventListener('click', () => {
            // Filter to only skipped items
            const skippedIds = state.history
                .filter(h => h.action === 'skipped')
                .map(h => h.linkId);
            state.filtered = state.links.filter(l => skippedIds.includes(l.id));
            state.currentIndex = 0;
            state.history = [];
            state.stats = { confirmed: 0, rejected: 0, skipped: 0 };
            if (state.filtered.length > 0) {
                showScreen(dom.reviewScreen);
                renderCard(state.filtered[0]);
                preloadNext();
            }
        });
    }

    // ── Init ───────────────────────────────────────────────────────────
    function init() {
        cacheDom();
        bindEvents();
        loadQueue();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
