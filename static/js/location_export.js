// Location Export page logic
(function() {
    let stats = null;

    async function fetchJSON(url, options) {
        const resp = await fetch(url, options);
        return resp.json();
    }

    function escapeHtml(str) {
        if (!str) return '';
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    async function loadStats() {
        try {
            const data = await fetchJSON('/api/location-export/stats');
            if (!data.success) return;
            stats = data;
            renderStats();
        } catch (e) {
            console.error('Failed to load location stats', e);
            document.getElementById('locations-container').innerHTML = '<div class="empty-state"><p>Failed to load stats</p></div>';
        }
    }

    function renderStats() {
        if (!stats) return;

        const summaryEl = document.getElementById('summary-stats');
        if (summaryEl) {
            summaryEl.innerHTML =
                '<div class="stat-card"><div class="stat-value">' + (stats.total_frames || 0) + '</div><div class="stat-label">Total Frames</div></div>' +
                '<div class="stat-card"><div class="stat-value">' + (stats.total_locations || 0) + '</div><div class="stat-label">Locations</div></div>' +
                '<div class="stat-card"><div class="stat-value">' + (stats.sources?.manual_annotations || 0) + '</div><div class="stat-label">Manual Annotations</div></div>' +
                '<div class="stat-card"><div class="stat-value">' + (stats.sources?.camera_mappings || 0) + '</div><div class="stat-label">Camera Mapped</div></div>';
        }

        const container = document.getElementById('locations-container');
        if (!stats.locations || stats.locations.length === 0) {
            container.innerHTML = '<div class="empty-state"><p>No location data available</p><small>Map cameras to locations or annotate frames with location_context scenario</small></div>';
            return;
        }

        let html = '<div class="locations-grid">';
        for (const loc of stats.locations) {
            const manualPct = loc.total_frames > 0 ? ((loc.manual_frames / loc.total_frames) * 100).toFixed(0) : 0;
            const autoPct = loc.total_frames > 0 ? ((loc.auto_frames / loc.total_frames) * 100).toFixed(0) : 0;

            html += '<div class="location-card">' +
                '<div class="location-name">' + escapeHtml(loc.location_name) + '</div>' +
                '<div class="location-frame-count">' + loc.total_frames + ' frames</div>' +
                '<div class="location-source-bar">' +
                '<div class="source-bar-fill manual" style="width:' + manualPct + '%" title="Manual: ' + loc.manual_frames + '"></div>' +
                '<div class="source-bar-fill auto" style="width:' + autoPct + '%" title="Auto: ' + loc.auto_frames + '"></div>' +
                '</div>' +
                '<div class="location-source-labels">' +
                '<span>Manual: ' + loc.manual_frames + '</span>' +
                '<span>Auto: ' + loc.auto_frames + '</span>' +
                '</div></div>';
        }
        html += '</div>';
        container.innerHTML = html;
    }

    async function exportDataset() {
        const statusEl = document.getElementById('export-status');
        statusEl.textContent = 'Exporting...';
        statusEl.style.color = '#f39c12';

        const format = document.getElementById('export-format')?.value || 'imagefolder';
        const valSplit = parseFloat(document.getElementById('val-split')?.value || '0.2');
        const seed = parseInt(document.getElementById('random-seed')?.value || '42');

        try {
            const data = await fetchJSON('/api/location-export/export', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ format, val_split: valSplit, seed })
            });

            if (data.success) {
                statusEl.textContent = 'Exported ' + data.total_frames + ' frames (' + data.train_frames + ' train, ' + data.val_frames + ' val) across ' + data.locations + ' locations';
                statusEl.style.color = '#27ae60';
            } else {
                statusEl.textContent = 'Error: ' + (data.error || 'Export failed');
                statusEl.style.color = '#e74c3c';
            }
        } catch (e) {
            statusEl.textContent = 'Error: ' + e.message;
            statusEl.style.color = '#e74c3c';
        }
    }

    async function exportAndTrain() {
        const statusEl = document.getElementById('export-status');
        statusEl.textContent = 'Exporting & submitting training job...';
        statusEl.style.color = '#f39c12';

        const format = document.getElementById('export-format')?.value || 'imagefolder';
        const valSplit = parseFloat(document.getElementById('val-split')?.value || '0.2');
        const seed = parseInt(document.getElementById('random-seed')?.value || '42');

        try {
            const data = await fetchJSON('/api/location-export/export-and-train', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ format, val_split: valSplit, seed })
            });

            if (data.success) {
                const jobId = data.job?.job_id || '';
                statusEl.textContent = data.message + (jobId ? ' (Job: ' + jobId.substring(0, 8) + '...)' : '');
                statusEl.style.color = '#27ae60';
            } else {
                statusEl.textContent = 'Error: ' + data.error;
                statusEl.style.color = '#e74c3c';
            }
        } catch (e) {
            statusEl.textContent = 'Error: ' + e.message;
            statusEl.style.color = '#e74c3c';
        }
    }

    window.locExport = { loadStats, exportDataset, exportAndTrain };
    loadStats();
})();
