/**
 * Movement Tracking Module
 * Manages object tracks across keyframes for guided interpolation.
 *
 * Tracks are auto-numbered (Track 1, Track 2, ...) with optional text labels.
 * Each track can have at most one bbox per keyframe timestamp.
 * When a track has 2+ keyframes, interpolation can be triggered.
 */
const movementTracker = {
    tracks: {},       // { trackNumber: { label, class, keyframes: [{annotationId, timestamp, bbox}] } }
    nextTrackNum: 1,
    videoId: null,
    panelVisible: false,

    async init(videoId) {
        this.videoId = videoId;
        await this.loadExistingTracks();
        this.renderPanel();
    },

    async loadExistingTracks() {
        if (!this.videoId) return;
        try {
            // Load all keyframe annotations with movement_tracking tag
            const resp = await fetch(`/api/videos/${this.videoId}/keyframe-annotations`);
            const data = await resp.json();
            if (!data.success) return;

            this.tracks = {};
            this.nextTrackNum = 1;

            // Filter to movement_tracking annotations and load their tags
            const trackAnnotations = (data.annotations || []).filter(
                a => a.activity_tag === 'movement_tracking'
            );

            for (const ann of trackAnnotations) {
                // Load tags for this annotation
                let tags = {};
                try {
                    const tagResp = await fetch(`/api/annotations/${ann.id}/tags?annotation_type=keyframe`);
                    const tagData = await tagResp.json();
                    if (tagData.success && tagData.tags) {
                        // Tags are stored as group entries, reconstruct
                        for (const t of tagData.tags) {
                            tags[t.tag_value.split(':')[0]] = t.tag_value.includes(':')
                                ? t.tag_value.split(':').slice(1).join(':')
                                : t.tag_value;
                        }
                    }
                } catch (e) {
                    // Tags may be stored differently, try the raw tag_data approach
                }

                // Also check if tags were stored as JSON in the annotation's comment
                // The scenario workflow stores tags via /api/annotations/{id}/tags
                // with tag_value = "key:value" format under a "scenario_data" group
                const trackNum = parseInt(tags.track_number || tags['scenario_data:track_number']) || 0;
                if (trackNum === 0) continue;

                const trackLabel = tags.track_label || tags['scenario_data:track_label'] || '';
                const className = tags['class'] || tags['scenario_data:class'] || '';

                if (!this.tracks[trackNum]) {
                    this.tracks[trackNum] = {
                        label: trackLabel,
                        className: className,
                        keyframes: []
                    };
                }

                this.tracks[trackNum].keyframes.push({
                    annotationId: ann.id,
                    timestamp: ann.timestamp,
                    bbox: {
                        x: ann.bbox_x,
                        y: ann.bbox_y,
                        width: ann.bbox_width,
                        height: ann.bbox_height
                    }
                });

                if (trackNum >= this.nextTrackNum) {
                    this.nextTrackNum = trackNum + 1;
                }
            }

            // Sort keyframes by timestamp within each track
            for (const track of Object.values(this.tracks)) {
                track.keyframes.sort((a, b) => a.timestamp - b.timestamp);
            }

            console.log('[MovementTracker] Loaded tracks:', Object.keys(this.tracks).length);
        } catch (e) {
            console.error('[MovementTracker] Failed to load tracks:', e);
        }
    },

    /**
     * Get track options for the scenario workflow dropdown.
     * Called dynamically when the movement_tracking scenario's track_number tag renders.
     */
    getTrackOptions() {
        const options = [];

        // Existing tracks
        for (const [num, track] of Object.entries(this.tracks)) {
            const label = track.label ? `Track ${num} - ${track.label}` : `Track ${num}`;
            const kfCount = track.keyframes.length;
            options.push(`${num}:${label} (${kfCount} keyframe${kfCount !== 1 ? 's' : ''})`);
        }

        // New track option
        options.push(`${this.nextTrackNum}:+ New Track ${this.nextTrackNum}`);

        return options;
    },

    /**
     * Check if the current timestamp already has a bbox for the selected track.
     */
    hasKeyframeAtTimestamp(trackNumber, timestamp) {
        const track = this.tracks[trackNumber];
        if (!track) return false;
        // Within 0.5s tolerance
        return track.keyframes.some(kf => Math.abs(kf.timestamp - timestamp) < 0.5);
    },

    /**
     * Sanitize text for safe HTML insertion
     */
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    },

    /**
     * Render the movement tracks panel in the annotation sidebar.
     */
    renderPanel() {
        let panel = document.getElementById('movement-tracks-panel');
        if (!panel) return;

        const trackEntries = Object.entries(this.tracks);

        if (trackEntries.length === 0) {
            panel.innerHTML = `
                <div class="tracks-empty">
                    <p>No movement tracks yet.</p>
                    <p class="help-text-small">Use the "Movement Tracking" scenario to tag objects across keyframes.</p>
                </div>
            `;
            return;
        }

        let html = '<div class="tracks-list">';

        for (const [num, track] of trackEntries) {
            const kfCount = track.keyframes.length;
            const canInterpolate = kfCount >= 2;
            const label = this.escapeHtml(track.label || `Track ${num}`);
            const className = this.escapeHtml(track.className || 'unclassified');
            const timeRange = kfCount > 0
                ? `${track.keyframes[0].timestamp.toFixed(1)}s - ${track.keyframes[kfCount - 1].timestamp.toFixed(1)}s`
                : '';

            html += `
                <div class="track-entry ${canInterpolate ? 'ready' : ''}">
                    <div class="track-entry-header">
                        <span class="track-name">${label}</span>
                        <span class="track-class-badge">${className}</span>
                    </div>
                    <div class="track-entry-meta">
                        <span>${kfCount} keyframe${kfCount !== 1 ? 's' : ''}</span>
                        <span>${timeRange}</span>
                    </div>
                    <div class="track-entry-keyframes">
                        ${track.keyframes.map(kf => `
                            <button class="kf-dot" title="${kf.timestamp.toFixed(1)}s"
                                    onclick="movementTracker.seekToKeyframe(${kf.timestamp})">
                                ${kf.timestamp.toFixed(1)}s
                            </button>
                        `).join('')}
                    </div>
                    ${canInterpolate ? `
                        <button class="btn-interpolate" onclick="movementTracker.triggerInterpolation(${num})">
                            Run Interpolation (${kfCount - 1} gap${kfCount - 1 !== 1 ? 's' : ''})
                        </button>
                    ` : `
                        <div class="track-hint">Add ${2 - kfCount} more keyframe${2 - kfCount !== 1 ? 's' : ''} to enable interpolation</div>
                    `}
                </div>
            `;
        }

        html += '</div>';
        panel.innerHTML = html;
    },

    seekToKeyframe(timestamp) {
        if (videoPlayer) {
            videoPlayer.currentTime = timestamp;
        }
    },

    async triggerInterpolation(trackNumber) {
        const track = this.tracks[trackNumber];
        if (!track || track.keyframes.length < 2) {
            alert('Track needs at least 2 keyframes for interpolation.');
            return;
        }

        const label = track.label || `Track ${trackNumber}`;
        if (!confirm(`Run interpolation for "${label}" (${track.className})?\n\nThis will detect the object on every 1-second frame between ${track.keyframes.length} keyframes.`)) {
            return;
        }

        // Build keyframe pairs from consecutive keyframes
        const keyframes = track.keyframes.map(kf => ({
            timestamp: kf.timestamp,
            bbox: kf.bbox
        }));

        try {
            const resp = await fetch('/api/interpolation/trigger', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    video_id: this.videoId,
                    class_name: track.className,
                    track_label: track.label || `Track ${trackNumber}`,
                    keyframes: keyframes
                })
            });
            const data = await resp.json();

            if (data.success) {
                const trackCount = data.tracks_created || 0;
                alert(`Interpolation started! ${trackCount} track segment${trackCount !== 1 ? 's' : ''} created.\n\nView results in Track Review.`);
                this.renderPanel();
            } else {
                alert('Interpolation failed: ' + (data.error || 'Unknown error'));
            }
        } catch (e) {
            console.error('[MovementTracker] Interpolation trigger failed:', e);
            alert('Failed to trigger interpolation. Check console for details.');
        }
    },

    /**
     * Toggle panel visibility.
     */
    togglePanel() {
        const panel = document.getElementById('movement-tracks-panel');
        if (!panel) return;
        this.panelVisible = !this.panelVisible;
        panel.style.display = this.panelVisible ? 'block' : 'none';

        const btn = document.getElementById('toggle-tracks-btn');
        if (btn) btn.classList.toggle('active', this.panelVisible);
    },

    /**
     * Refresh tracks after a new annotation is saved.
     */
    async refresh() {
        await this.loadExistingTracks();
        this.renderPanel();
    }
};

// Auto-init when annotator loads a video
document.addEventListener('DOMContentLoaded', () => {
    // Wait a tick for currentVideoId to be set
    setTimeout(() => {
        if (typeof currentVideoId !== 'undefined' && currentVideoId) {
            movementTracker.init(parseInt(currentVideoId));
        }
    }, 1500);
});

// Listen for annotation save events to refresh tracks
document.addEventListener('annotationSaved', () => {
    movementTracker.refresh();
});
