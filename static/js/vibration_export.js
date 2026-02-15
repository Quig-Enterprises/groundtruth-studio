// Vibration Export page logic
(function() {
    let availableTags = [];
    let selectedTag = null;

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

    async function loadTags() {
        try {
            const data = await fetchJSON('/api/vibration/tags');
            if (!data.success) return;
            availableTags = data.tags || [];
            renderTags();
        } catch (e) {
            console.error('Failed to load tags', e);
        }
    }

    function renderTags() {
        const container = document.getElementById('tags-container');
        if (!container) return;

        if (!availableTags.length) {
            container.innerHTML = '<div class="empty-state"><p>No vibration annotations yet</p><small>Add time-range annotations to videos to create training data</small></div>';
            return;
        }

        let totalCount = 0, totalNeg = 0;
        availableTags.forEach(t => { totalCount += t.count; totalNeg += t.negative_count; });

        let html = '<div class="tag-stats-summary">' +
            '<div class="summary-stat"><span class="summary-value">' + availableTags.length + '</span><span class="summary-label">Tag Types</span></div>' +
            '<div class="summary-stat"><span class="summary-value">' + totalCount + '</span><span class="summary-label">Total Annotations</span></div>' +
            '<div class="summary-stat"><span class="summary-value">' + totalNeg + '</span><span class="summary-label">Negative Samples</span></div>' +
            '</div>';

        html += '<div class="tags-grid">';
        for (const tag of availableTags) {
            const isSelected = selectedTag === tag.tag_name;
            html += '<div class="tag-card ' + (isSelected ? 'selected' : '') + '" onclick="vibExport.selectTag(\'' + escapeHtml(tag.tag_name) + '\')">' +
                '<div class="tag-card-header">' +
                '<span class="tag-name">' + escapeHtml(tag.tag_name) + '</span>' +
                '<span class="tag-count">' + tag.count + ' samples</span>' +
                '</div>' +
                '<div class="tag-card-stats">' +
                '<span>Positive: ' + (tag.count - tag.negative_count) + '</span>' +
                '<span>Negative: ' + tag.negative_count + '</span>' +
                '</div>';

            if (canWrite) {
                html += '<div class="tag-card-actions">' +
                    '<button class="btn-primary btn-sm" onclick="event.stopPropagation(); vibExport.exportTag(\'' + escapeHtml(tag.tag_name) + '\')">Export</button>' +
                    '<button class="btn-success btn-sm" onclick="event.stopPropagation(); vibExport.exportAndTrain(\'' + escapeHtml(tag.tag_name) + '\')">Export &amp; Train</button>' +
                    '</div>';
            }
            html += '</div>';
        }
        html += '</div>';
        container.innerHTML = html;
    }

    function selectTag(tagName) {
        selectedTag = selectedTag === tagName ? null : tagName;
        renderTags();
    }

    async function exportTag(tagName) {
        const formats = [];
        const csvCheck = document.getElementById('format-csv');
        const parquetCheck = document.getElementById('format-parquet');
        if (csvCheck && csvCheck.checked) formats.push('csv');
        if (parquetCheck && parquetCheck.checked) formats.push('parquet');
        if (formats.length === 0) formats.push('csv');

        const valSplit = parseFloat(document.getElementById('val-split')?.value || '0.2');
        const seed = parseInt(document.getElementById('random-seed')?.value || '42');

        const statusEl = document.getElementById('export-status');
        if (statusEl) { statusEl.style.display = 'block'; statusEl.textContent = 'Exporting...'; statusEl.style.color = '#f39c12'; }

        try {
            const data = await fetchJSON('/api/vibration/export', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ tag_filter: tagName || null, formats, val_split: valSplit, seed })
            });

            if (data.success) {
                if (statusEl) {
                    statusEl.textContent = 'Exported ' + data.total_samples + ' samples (' + data.train_count + ' train, ' + data.val_count + ' val) to ' + data.export_path;
                    statusEl.style.color = '#27ae60';
                }
            } else {
                if (statusEl) { statusEl.textContent = 'Error: ' + (data.error || 'Export failed'); statusEl.style.color = '#e74c3c'; }
            }
        } catch (e) {
            if (statusEl) { statusEl.textContent = 'Error: ' + e.message; statusEl.style.color = '#e74c3c'; }
        }
    }

    async function exportAndTrain(tagName) {
        const statusEl = document.getElementById('export-status');
        if (statusEl) { statusEl.style.display = 'block'; statusEl.textContent = 'Exporting & submitting training job...'; statusEl.style.color = '#f39c12'; }

        const modelType = document.getElementById('model-type')?.value || 'autoencoder';
        const epochs = parseInt(document.getElementById('epochs')?.value || '100');

        try {
            const data = await fetchJSON('/api/training/export-and-train', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    job_type: 'bearing-fault',
                    tag_filter: tagName || null,
                    model_type: modelType,
                    epochs: epochs
                })
            });

            if (data.success) {
                const jobId = data.job?.job_id || '';
                if (statusEl) {
                    statusEl.textContent = data.message + ' (Job: ' + jobId.substring(0, 8) + '...)';
                    statusEl.style.color = '#27ae60';
                }
            } else {
                if (statusEl) { statusEl.textContent = 'Error: ' + data.error; statusEl.style.color = '#e74c3c'; }
            }
        } catch (e) {
            if (statusEl) { statusEl.textContent = 'Error: ' + e.message; statusEl.style.color = '#e74c3c'; }
        }
    }

    async function exportAll() { await exportTag(null); }
    async function exportAndTrainAll() { await exportAndTrain(null); }

    window.vibExport = { loadTags, selectTag, exportTag, exportAndTrain, exportAll, exportAndTrainAll };
    loadTags();
})();
