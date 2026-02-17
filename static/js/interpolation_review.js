/**
 * Interpolation Track Review - Filmstrip Interface
 */
const interpReview = {
    tracks: [],
    currentTrack: null,
    currentFrames: [],
    selectedFrameIdx: -1,

    async init() {
        await this.loadTracks();
        this.setupKeyboardNav();
    },

    async loadTracks() {
        try {
            const status = document.getElementById('filter-status').value;
            const videoId = document.getElementById('filter-video').value;
            let url = '/api/interpolation/tracks?';
            if (status) url += `status=${status}&`;
            if (videoId) url += `video_id=${videoId}&`;

            const resp = await fetch(url);
            const data = await resp.json();
            if (data.success) {
                this.tracks = data.tracks;
                this.renderTrackList();
                this.populateVideoFilter();
            }
        } catch (e) {
            console.error('Failed to load tracks:', e);
        }
    },

    populateVideoFilter() {
        const select = document.getElementById('filter-video');
        const current = select.value;
        const videos = new Map();
        this.tracks.forEach(t => {
            if (!videos.has(t.video_id)) {
                videos.set(t.video_id, t.video_title || t.video_filename);
            }
        });
        select.innerHTML = '<option value="">All Videos</option>';
        videos.forEach((name, id) => {
            const opt = document.createElement('option');
            opt.value = id;
            opt.textContent = name;
            select.appendChild(opt);
        });
        if (current) select.value = current;
    },

    filterTracks() {
        this.loadTracks();
    },

    renderTrackList() {
        const container = document.getElementById('track-list');
        if (this.tracks.length === 0) {
            container.innerHTML = '<div class="empty-tracks">No tracks found</div>';
            return;
        }

        container.innerHTML = this.tracks.map(track => {
            const rate = track.frames_generated > 0
                ? Math.round((track.frames_detected / track.frames_generated) * 100)
                : 0;
            const isActive = this.currentTrack && this.currentTrack.id === track.id;
            return `
                <div class="track-item ${isActive ? 'active' : ''} status-${track.status}"
                     onclick="interpReview.selectTrack(${track.id})" data-track-id="${track.id}">
                    <div class="track-item-header">
                        <span class="track-class">${track.class_name}</span>
                        <span class="track-status status-${track.status}">${track.status}</span>
                    </div>
                    <div class="track-item-meta">
                        <span>${track.video_title || track.video_filename}</span>
                    </div>
                    <div class="track-item-details">
                        <span>${track.start_timestamp.toFixed(1)}s - ${track.end_timestamp.toFixed(1)}s</span>
                        <span class="detection-rate">${track.frames_detected}/${track.frames_generated} (${rate}%)</span>
                    </div>
                </div>
            `;
        }).join('');
    },

    async selectTrack(trackId) {
        try {
            const resp = await fetch(`/api/interpolation/track/${trackId}`);
            const data = await resp.json();
            if (!data.success) return;

            this.currentTrack = data.track;
            this.currentFrames = data.frames;
            this.selectedFrameIdx = -1;

            document.getElementById('no-track-msg').style.display = 'none';
            document.getElementById('filmstrip-content').style.display = 'block';

            this.renderTrackHeader();
            this.renderFilmstrip();
            this.renderTrackList();
        } catch (e) {
            console.error('Failed to load track:', e);
        }
    },

    renderTrackHeader() {
        const track = this.currentTrack;
        document.getElementById('track-title').textContent =
            `Track #${track.id}`;
        document.getElementById('track-class').textContent = track.class_name;
        document.getElementById('track-time').textContent =
            `${track.start_timestamp.toFixed(1)}s → ${track.end_timestamp.toFixed(1)}s`;

        const rate = track.frames_generated > 0
            ? Math.round((track.frames_detected / track.frames_generated) * 100)
            : 0;
        document.getElementById('track-detection-rate').textContent =
            `${track.frames_detected}/${track.frames_generated} matched (${rate}%)`;

        const canReview = track.status === 'ready';
        document.getElementById('btn-approve-track').style.display = canReview ? '' : 'none';
        document.getElementById('btn-reject-track').style.display = canReview ? '' : 'none';
    },

    renderFilmstrip() {
        const grid = document.getElementById('filmstrip-grid');
        const track = this.currentTrack;
        const videoWidth = track.video_width || 1920;
        const videoHeight = track.video_height || 1080;

        const cells = [];

        cells.push(this._renderAnchorCell(track, 'start', videoWidth, videoHeight));

        this.currentFrames.forEach((frame, idx) => {
            cells.push(this._renderFrameCell(frame, idx, videoWidth, videoHeight));
        });

        cells.push(this._renderAnchorCell(track, 'end', videoWidth, videoHeight));

        grid.innerHTML = cells.join('');

        this.currentFrames.forEach(frame => {
            if (frame.frame_cache) {
                const img = new Image();
                img.src = `/frame-cache/${track.video_id}/${frame.frame_cache}`;
            }
        });
    },

    _renderAnchorCell(track, position, videoWidth, videoHeight) {
        const bbox = position === 'start'
            ? { x: track.start_bbox_x, y: track.start_bbox_y,
                w: track.start_bbox_width, h: track.start_bbox_height }
            : { x: track.end_bbox_x, y: track.end_bbox_y,
                w: track.end_bbox_width, h: track.end_bbox_height };
        const ts = position === 'start' ? track.start_timestamp : track.end_timestamp;
        const conf = position === 'start' ? track.start_confidence : track.end_confidence;

        const corrected = position === 'start' ? track.start_corrected_bbox : track.end_corrected_bbox;
        if (corrected) {
            bbox.x = corrected.x; bbox.y = corrected.y;
            bbox.w = corrected.width; bbox.h = corrected.height;
        }

        return `
            <div class="filmstrip-cell anchor-cell">
                <div class="cell-image-wrapper">
                    <div class="cell-image placeholder-anchor">
                        <div class="anchor-label">ANCHOR</div>
                        <div class="anchor-time">${ts.toFixed(1)}s</div>
                    </div>
                    <svg class="bbox-overlay" viewBox="0 0 ${videoWidth} ${videoHeight}">
                        <rect x="${bbox.x}" y="${bbox.y}" width="${bbox.w}" height="${bbox.h}"
                              fill="none" stroke="#27ae60" stroke-width="3"/>
                    </svg>
                </div>
                <div class="cell-footer">
                    <span class="cell-timestamp">${ts.toFixed(1)}s</span>
                    <span class="cell-badge anchor-badge">Anchor</span>
                    ${conf ? `<span class="cell-confidence">${(conf * 100).toFixed(0)}%</span>` : ''}
                </div>
            </div>
        `;
    },

    _renderFrameCell(frame, idx, videoWidth, videoHeight) {
        const tags = frame.predicted_tags || {};
        const isUnmatched = tags.unmatched === true;
        const frameSrc = frame.frame_cache
            ? `/frame-cache/${this.currentTrack.video_id}/${frame.frame_cache}`
            : '';
        const cellClass = isUnmatched ? 'unmatched-cell' : 'detected-cell';
        const isSelected = idx === this.selectedFrameIdx;
        const isExcluded = frame.review_status === 'rejected';

        return `
            <div class="filmstrip-cell ${cellClass} ${isSelected ? 'selected' : ''} ${isExcluded ? 'excluded' : ''}"
                 onclick="interpReview.selectFrame(${idx})" data-frame-idx="${idx}">
                <div class="cell-image-wrapper">
                    ${frameSrc
                        ? `<img class="cell-image" src="${frameSrc}" alt="Frame at ${frame.timestamp}s" loading="lazy"/>`
                        : '<div class="cell-image placeholder">No image</div>'}
                    <svg class="bbox-overlay" viewBox="0 0 ${videoWidth} ${videoHeight}">
                        <rect x="${frame.bbox_x}" y="${frame.bbox_y}"
                              width="${frame.bbox_width}" height="${frame.bbox_height}"
                              fill="none"
                              stroke="${isUnmatched ? '#f39c12' : '#3498db'}"
                              stroke-width="3"
                              ${isUnmatched ? 'stroke-dasharray="8,4"' : ''}/>
                    </svg>
                    ${isUnmatched ? '<div class="unmatched-label">NO DETECTION</div>' : ''}
                    ${isExcluded ? '<div class="excluded-label">EXCLUDED</div>' : ''}
                </div>
                <div class="cell-footer">
                    <span class="cell-timestamp">${frame.timestamp.toFixed(1)}s</span>
                    ${!isUnmatched
                        ? `<span class="cell-confidence">${(frame.confidence * 100).toFixed(0)}%</span>`
                        : '<span class="cell-badge unmatched-badge">Unmatched</span>'}
                    ${!isExcluded && !isUnmatched && this.currentTrack.status === 'ready'
                        ? `<button class="cell-exclude-btn" onclick="event.stopPropagation(); interpReview.excludeFrame(${frame.id})" title="Exclude frame">&times;</button>`
                        : ''}
                </div>
            </div>
        `;
    },

    selectFrame(idx) {
        this.selectedFrameIdx = idx;
        document.querySelectorAll('.filmstrip-cell').forEach((el, i) => {
            el.classList.toggle('selected', i - 1 === idx);
        });
    },

    async excludeFrame(predictionId) {
        try {
            const resp = await fetch(`/api/ai/predictions/${predictionId}/review`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: 'reject', reviewer: 'track_review' })
            });
            const data = await resp.json();
            if (data.success) {
                this.selectTrack(this.currentTrack.id);
            }
        } catch (e) {
            console.error('Failed to exclude frame:', e);
        }
    },

    async approveTrack() {
        if (!this.currentTrack) return;
        if (!confirm(`Approve track #${this.currentTrack.id}? All detected frames will become annotations.`)) return;

        try {
            const resp = await fetch(`/api/interpolation/track/${this.currentTrack.id}/review`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: 'approve', reviewer: 'track_review' })
            });
            const data = await resp.json();
            if (data.success) {
                this.loadTracks();
                this.selectTrack(this.currentTrack.id);
            }
        } catch (e) {
            console.error('Failed to approve track:', e);
        }
    },

    async rejectTrack() {
        if (!this.currentTrack) return;
        if (!confirm(`Reject track #${this.currentTrack.id}? All predictions will be rejected.`)) return;

        try {
            const resp = await fetch(`/api/interpolation/track/${this.currentTrack.id}/review`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: 'reject', reviewer: 'track_review' })
            });
            const data = await resp.json();
            if (data.success) {
                this.loadTracks();
                this.selectTrack(this.currentTrack.id);
            }
        } catch (e) {
            console.error('Failed to reject track:', e);
        }
    },

    async scanAndTrigger() {
        const btn = document.getElementById('btn-scan');
        const origText = btn.textContent;
        btn.disabled = true;
        btn.textContent = 'Scanning...';

        try {
            const resp = await fetch('/api/interpolation/scan', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({})
            });
            const data = await resp.json();
            if (data.success) {
                const msg = data.triggered > 0
                    ? `Triggered ${data.triggered} interpolation(s). Refresh in a moment to see results.`
                    : 'No new eligible pairs found.';
                alert(msg);
                // Refresh track list after a short delay
                if (data.triggered > 0) {
                    setTimeout(() => this.loadTracks(), 3000);
                }
            } else {
                alert('Scan failed: ' + (data.error || 'Unknown error'));
            }
        } catch (err) {
            alert('Scan error: ' + err.message);
        } finally {
            btn.disabled = false;
            btn.textContent = origText;
        }
    },

    setupKeyboardNav() {
        document.addEventListener('keydown', (e) => {
            if (!this.currentTrack || !this.currentFrames.length) return;
            if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;

            switch (e.key) {
                case 'ArrowLeft':
                    e.preventDefault();
                    if (this.selectedFrameIdx > 0) {
                        this.selectFrame(this.selectedFrameIdx - 1);
                    }
                    break;
                case 'ArrowRight':
                    e.preventDefault();
                    if (this.selectedFrameIdx < this.currentFrames.length - 1) {
                        this.selectFrame(this.selectedFrameIdx + 1);
                    }
                    break;
                case 'Enter':
                    e.preventDefault();
                    this.approveTrack();
                    break;
                case 'Escape':
                    e.preventDefault();
                    this.rejectTrack();
                    break;
            }
        });
    },
};

document.addEventListener('DOMContentLoaded', () => interpReview.init());
