// Global Prediction Review page logic
(function() {
    let predictions = [];
    let models = [];
    let total = 0;
    let currentOffset = 0;
    const PAGE_SIZE = 50;

    const VEHICLE_TYPES = [
        'sedan', 'pickup truck', 'SUV', 'minivan', 'van',
        'tractor', 'ATV', 'UTV', 'snowmobile', 'golf cart', 'skid loader', 'motorcycle', 'trailer',
        'bus', 'semi truck', 'dump truck',
        'rowboat', 'fishing boat', 'speed boat', 'pontoon boat', 'kayak', 'canoe', 'sailboat', 'jet ski'
    ];

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

    async function loadModels() {
        try {
            const data = await fetchJSON('/api/ai/models');
            if (data.success) {
                models = data.models || [];
                const select = document.getElementById('model-filter');
                if (select) {
                    select.innerHTML = '<option value="">All Models</option>';
                    models.forEach(m => {
                        const opt = document.createElement('option');
                        opt.value = m.model_name;
                        opt.textContent = m.model_name + ' v' + m.model_version;
                        select.appendChild(opt);
                    });
                }
            }
        } catch (e) {
            console.error('Failed to load models', e);
        }
    }

    async function loadPredictions() {
        const modelFilter = document.getElementById('model-filter')?.value || '';
        const scenarioFilter = document.getElementById('scenario-filter')?.value || '';
        const minConf = document.getElementById('min-confidence')?.value || '';
        const maxConf = document.getElementById('max-confidence')?.value || '';

        let url = '/api/ai/predictions/all-pending?limit=' + PAGE_SIZE + '&offset=' + currentOffset;
        if (modelFilter) url += '&model=' + encodeURIComponent(modelFilter);
        if (scenarioFilter) url += '&scenario=' + encodeURIComponent(scenarioFilter);
        if (minConf) url += '&min_confidence=' + minConf;
        if (maxConf) url += '&max_confidence=' + maxConf;

        try {
            const data = await fetchJSON(url);
            if (data.success) {
                predictions = data.predictions || [];
                total = data.total || 0;
                renderPredictions();
                renderPagination();
            }
        } catch (e) {
            console.error('Failed to load predictions', e);
        }
    }

    function getConfidenceClass(conf) {
        if (conf >= 0.9) return 'conf-high';
        if (conf >= 0.7) return 'conf-medium';
        return 'conf-low';
    }

    function formatTags(tags) {
        if (!tags) return '';
        if (typeof tags === 'string') {
            try { tags = JSON.parse(tags); } catch(e) { return escapeHtml(tags); }
        }
        if (typeof tags === 'object') {
            return Object.entries(tags).map(function(entry) {
                return '<span class="tag-pill">' + escapeHtml(entry[0]) + ': ' + escapeHtml(String(entry[1])) + '</span>';
            }).join(' ');
        }
        return escapeHtml(String(tags));
    }

    function renderPredictions() {
        const container = document.getElementById('predictions-container');
        const countEl = document.getElementById('total-count');
        if (countEl) countEl.textContent = total;

        if (!predictions.length) {
            container.innerHTML = '<div class="empty-state"><p>No pending predictions</p><small>Predictions appear here when AI models submit results</small></div>';
            return;
        }

        let html = '<div class="predictions-grid">';
        for (const pred of predictions) {
            const confClass = getConfidenceClass(pred.confidence);
            const confPct = (pred.confidence * 100).toFixed(1);
            const thumbUrl = pred.thumbnail_url || '';
            const timeAgo = pred.created_at ? new Date(pred.created_at).toLocaleString() : '';
            const thumbHtml = thumbUrl ? '<img class="pred-thumb" src="' + escapeHtml(thumbUrl) + '" alt="" onerror="this.style.display=\'none\'">' : '';

            html += '<div class="pred-card" id="pred-' + pred.id + '">' +
                '<div class="pred-card-top">' +
                thumbHtml +
                '<div class="pred-info">' +
                '<div class="pred-video-title">' + escapeHtml(pred.video_title || 'Video #' + pred.video_id) + '</div>' +
                '<div class="pred-type">' + escapeHtml(pred.prediction_type || 'classification') + '</div>' +
                '<div class="pred-tags">' + formatTags(pred.predicted_tags) + '</div>' +
                '</div>' +
                '<span class="confidence-pill ' + confClass + '">' + confPct + '%</span>' +
                '</div>' +
                '<div class="pred-card-meta">' +
                '<span>' + escapeHtml(pred.model_name || '') + ' v' + escapeHtml(pred.model_version || '') + '</span>' +
                '<span>' + timeAgo + '</span>' +
                '</div>';

            if (canWrite) {
                const tags = pred.predicted_tags || {};
                const isVehicle = tags.vehicle_type || (pred.scenario === 'vehicle_detection');

                html += '<div class="pred-card-actions">' +
                    '<button class="btn-approve" onclick="predReview.review(' + pred.id + ', \'approve\')">Approve</button>' +
                    '<button class="btn-reject" onclick="predReview.review(' + pred.id + ', \'reject\')">Reject</button>';

                if (isVehicle) {
                    const currentType = tags.vehicle_type || tags.class || '';
                    html += '<select id="reclassify-' + pred.id + '" style="flex:1;padding:8px;border:1px solid #ddd;border-radius:4px;font-size:13px;">';
                    html += '<option value="">Reclassify as...</option>';
                    for (const vt of VEHICLE_TYPES) {
                        const selected = (vt === currentType) ? ' selected' : '';
                        html += '<option value="' + escapeHtml(vt) + '"' + selected + '>' + escapeHtml(vt) + '</option>';
                    }
                    html += '</select>' +
                        '<button class="btn-correct" onclick="predReview.reclassifyVehicle(' + pred.id + ')">Reclassify</button>';
                } else {
                    html += '<button class="btn-correct" onclick="predReview.review(' + pred.id + ', \'correct\')">Correct</button>';
                }

                html += '</div>';
            }
            html += '</div>';
        }
        html += '</div>';
        container.innerHTML = html;
    }

    function renderPagination() {
        const el = document.getElementById('pagination-info');
        if (!el) return;
        const start = currentOffset + 1;
        const end = Math.min(currentOffset + PAGE_SIZE, total);
        el.textContent = 'Showing ' + start + '-' + end + ' of ' + total;

        const prevBtn = document.getElementById('prev-btn');
        const nextBtn = document.getElementById('next-btn');
        if (prevBtn) prevBtn.disabled = currentOffset === 0;
        if (nextBtn) nextBtn.disabled = currentOffset + PAGE_SIZE >= total;
    }

    function prevPage() {
        currentOffset = Math.max(0, currentOffset - PAGE_SIZE);
        loadPredictions();
    }

    function nextPage() {
        if (currentOffset + PAGE_SIZE < total) {
            currentOffset += PAGE_SIZE;
            loadPredictions();
        }
    }

    async function review(predictionId, action) {
        try {
            const data = await fetchJSON('/api/ai/predictions/' + predictionId + '/review', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: action })
            });

            if (data.success) {
                const card = document.getElementById('pred-' + predictionId);
                if (card) {
                    card.style.opacity = '0';
                    card.style.transform = 'scale(0.95)';
                    setTimeout(function() {
                        card.remove();
                        total--;
                        var countEl = document.getElementById('total-count');
                        if (countEl) countEl.textContent = total;
                    }, 300);
                }
            } else {
                alert('Error: ' + (data.error || 'Review failed'));
            }
        } catch (e) {
            alert('Error: ' + e.message);
        }
    }

    async function approveAllHighConfidence() {
        if (!confirm('Approve all predictions with confidence >= 95%?')) return;

        const highConf = predictions.filter(function(p) { return p.confidence >= 0.95; });
        let approved = 0;

        for (const pred of highConf) {
            try {
                const data = await fetchJSON('/api/ai/predictions/' + pred.id + '/review', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ action: 'approve' })
                });
                if (data.success) approved++;
            } catch (e) { console.error(e); }
        }

        alert('Approved ' + approved + ' of ' + highConf.length + ' high-confidence predictions');
        loadPredictions();
    }

    function applyFilters() {
        currentOffset = 0;
        loadPredictions();
    }

    async function reclassifyVehicle(predictionId) {
        const select = document.getElementById('reclassify-' + predictionId);
        if (!select || !select.value) {
            alert('Please select a vehicle type');
            return;
        }

        const newType = select.value;

        try {
            const data = await fetchJSON('/api/ai/predictions/' + predictionId + '/review', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    action: 'correct',
                    corrections: {
                        tags: {
                            class: newType,
                            vehicle_type: newType,
                            corrected: true
                        },
                        correction_type: 'vehicle_reclass'
                    },
                    notes: 'Reclassified vehicle type to: ' + newType
                })
            });

            if (data.success) {
                const card = document.getElementById('pred-' + predictionId);
                if (card) {
                    card.style.opacity = '0';
                    card.style.transform = 'scale(0.95)';
                    setTimeout(function() {
                        card.remove();
                        total--;
                        var countEl = document.getElementById('total-count');
                        if (countEl) countEl.textContent = total;
                    }, 300);
                }
            } else {
                alert('Error: ' + (data.error || 'Reclassification failed'));
            }
        } catch (e) {
            alert('Error: ' + e.message);
        }
    }

    window.predReview = { loadPredictions, review, reclassifyVehicle, approveAllHighConfidence, applyFilters, prevPage, nextPage };

    loadModels();
    loadPredictions();
})();
