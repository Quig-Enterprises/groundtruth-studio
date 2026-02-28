/* Training Gallery — vanilla JS module */
var TrainingGallery = {
    // State
    items: [],
    page: 1,
    perPage: 60,
    totalPages: 0,
    loading: false,
    filters: { scenario: '', classification: '', sort: 'confidence', status: 'approved', camera: '', reject_reasons: [] },
    selected: new Set(),         // individual prediction IDs
    selectedClusters: new Set(), // "type:id" strings
    clusterMembers: {},          // cache: "type:id" -> [items]
    allClasses: [],              // for reclassify dropdowns
    observer: null,
    imageObserver: null,
    modalOpen: false,
    modalClusterType: null,
    modalClusterId: null,
    modalItems: [],
    modalSelected: new Set(),
    similarityMode: false,
    similaritySeedId: null,

    // ── Initialization ───────────────────────────────────────────────────

    init() {
        this.readUrlParams();
        this.loadFilters();
        this.setupInfiniteScroll();
        this.bindEvents();
    },

    readUrlParams() {
        var params = new URLSearchParams(window.location.search);
        if (params.get('status')) this.filters.status = params.get('status');
        if (params.get('scenario')) this.filters.scenario = params.get('scenario');
        if (params.get('classification')) this.filters.classification = params.get('classification');
        if (params.get('sort')) this.filters.sort = params.get('sort');
        if (params.get('camera')) this.filters.camera = params.get('camera');
        if (params.get('reject_reasons')) this.filters.reject_reasons = params.get('reject_reasons').split(',');
        if (params.get('page')) this.page = parseInt(params.get('page'), 10) || 1;
        // Sync UI controls
        var statusBtn = document.querySelector('#status-toggle .toggle-btn[data-status="' + this.filters.status + '"]');
        if (statusBtn) {
            document.querySelectorAll('#status-toggle .toggle-btn').forEach(function(b) { b.classList.remove('active'); });
            statusBtn.classList.add('active');
        }
        var sortSel = document.getElementById('sort-select');
        if (sortSel) sortSel.value = this.filters.sort;
        this.updateModeButtons();
    },

    syncUrlParams() {
        var params = new URLSearchParams();
        params.set('status', this.filters.status);
        if (this.filters.scenario) params.set('scenario', this.filters.scenario);
        if (this.filters.classification) params.set('classification', this.filters.classification);
        if (this.filters.sort && this.filters.sort !== 'confidence') params.set('sort', this.filters.sort);
        if (this.filters.camera) params.set('camera', this.filters.camera);
        if (this.filters.reject_reasons.length) params.set('reject_reasons', this.filters.reject_reasons.join(','));
        if (this.page > 1) params.set('page', this.page);
        var newUrl = window.location.pathname + '?' + params.toString();
        history.replaceState(null, '', newUrl);
    },

    // ── Filter Loading ───────────────────────────────────────────────────

    async loadFilters() {
        try {
            const filterParams = new URLSearchParams({ status: this.filters.status });
            if (this.filters.camera) filterParams.set('camera', this.filters.camera);
            const resp = await fetch('/api/training-gallery/filters?' + filterParams);
            const data = await resp.json();

            const scenarioSel = document.getElementById('scenario-filter');
            while (scenarioSel.options.length > 1) scenarioSel.remove(1);
            data.scenarios.forEach(s => {
                const opt = document.createElement('option');
                opt.value = s.scenario;
                opt.textContent = s.scenario + ' (' + s.count + ')';
                scenarioSel.appendChild(opt);
            });

            this.allClasses = data.classifications;
            this.reclassifyClasses = data.all_classes || data.classifications;
            this.updateClassificationDropdown(data.classifications);

            // Restore dropdown values from URL params
            if (this.filters.scenario) scenarioSel.value = this.filters.scenario;
            if (this.filters.classification) {
                document.getElementById('classification-filter').value = this.filters.classification;
            }
            this.loadPage();

            // Update pending badge
            var pendingBtn = document.querySelector('.toggle-btn[data-status="pending"]');
            if (pendingBtn && data.pending_count > 0) {
                var badge = pendingBtn.querySelector('.count-badge');
                if (!badge) {
                    badge = document.createElement('span');
                    badge.className = 'count-badge';
                    pendingBtn.appendChild(badge);
                }
                badge.textContent = data.pending_count.toLocaleString();
            } else if (pendingBtn) {
                var oldBadge = pendingBtn.querySelector('.count-badge');
                if (oldBadge) oldBadge.remove();
            }

            // Update rejected badge
            var rejectedBtn = document.querySelector('.toggle-btn[data-status="rejected"]');
            if (rejectedBtn && data.rejected_count > 0) {
                var rBadge = rejectedBtn.querySelector('.count-badge');
                if (!rBadge) {
                    rBadge = document.createElement('span');
                    rBadge.className = 'count-badge';
                    rejectedBtn.appendChild(rBadge);
                }
                rBadge.textContent = data.rejected_count.toLocaleString();
            } else if (rejectedBtn) {
                var oldRBadge = rejectedBtn.querySelector('.count-badge');
                if (oldRBadge) oldRBadge.remove();
            }

            // Show/hide and populate reason filter
            var reasonWrap = document.getElementById('reason-filter-wrap');
            if (this.filters.status === 'rejected' && data.reject_reasons && data.reject_reasons.length) {
                reasonWrap.style.display = '';
                this.populateReasonFilter(data.reject_reasons);
            } else {
                reasonWrap.style.display = 'none';
            }

            // Load camera list
            try {
                const camResp = await fetch('/api/training-gallery/cameras?status=' + this.filters.status);
                const camData = await camResp.json();
                if (camData.success) {
                    const camSel = document.getElementById('camera-filter');
                    if (camSel) {
                        while (camSel.options.length > 1) camSel.remove(1);
                        camData.cameras.forEach(c => {
                            const opt = document.createElement('option');
                            opt.value = c.name;
                            opt.textContent = c.name + ' (' + c.count + ')';
                            camSel.appendChild(opt);
                        });
                        camSel.value = this.filters.camera;
                    }
                }
            } catch (e) {
                console.error('Failed to load cameras:', e);
            }
        } catch (e) {
            console.error('Failed to load filters:', e);
            this.loadPage();
        }
    },

    updateClassificationDropdown(classes) {
        const sel = document.getElementById('classification-filter');
        while (sel.options.length > 1) sel.remove(1);
        classes.forEach(c => {
            const opt = document.createElement('option');
            opt.value = c.name;
            opt.textContent = (c.display_name || c.name) + ' (' + c.count + ')';
            sel.appendChild(opt);
        });
        this.populateReclassifyDropdowns();
    },

    populateReclassifyDropdowns() {
        const classes = this.reclassifyClasses || this.allClasses;
        populateClassDatalist('reclassify-list', classes);
        populateClassDatalist('modal-reclassify-list', classes);
    },

    // ── Page Loading ─────────────────────────────────────────────────────

    async loadPage() {
        if (this.loading) return;
        this.loading = true;

        const params = new URLSearchParams({
            page: this.page,
            per_page: this.perPage,
            sort: this.filters.sort,
            status: this.filters.status
        });
        if (this.filters.scenario) params.set('scenario', this.filters.scenario);
        if (this.filters.classification) params.set('classification', this.filters.classification);
        if (this.filters.camera) params.set('camera', this.filters.camera);
        if (this.filters.reject_reasons.length) params.set('reject_reasons', this.filters.reject_reasons.join(','));

        try {
            const resp = await fetch('/api/training-gallery/items?' + params);
            const data = await resp.json();

            if (this.page === 1) {
                const grid = document.getElementById('gallery-grid');
                grid.textContent = '';
                const initLoading = document.getElementById('initial-loading');
                if (initLoading) initLoading.style.display = 'none';
            }

            this.totalPages = data.pages;
            this.renderItems(data.items);
            this.updateStats(data.total);

            if (data.items.length === 0 && this.page === 1) {
                const empty = document.createElement('div');
                empty.className = 'empty-state';
                empty.textContent = 'No training images match the current filters.';
                document.getElementById('gallery-grid').appendChild(empty);
            }
        } catch (e) {
            console.error('Failed to load gallery page:', e);
            if (this.page === 1) {
                const errEl = document.createElement('div');
                errEl.className = 'empty-state error';
                errEl.textContent = 'Failed to load training images.';
                document.getElementById('gallery-grid').appendChild(errEl);
            }
        }

        this.loading = false;
    },

    // ── Rendering ────────────────────────────────────────────────────────

    renderItems(items) {
        const grid = document.getElementById('gallery-grid');
        items.forEach(item => {
            grid.appendChild(this.createCard(item));
            this.items.push(item);
        });
    },

    createCard(item) {
        const isCluster = !!(item.cluster_type && item.cluster_count > 1);
        const isSelected = this.selected.has(item.id);

        // Root card
        const card = document.createElement('div');
        card.className = 'gallery-card' + (isCluster ? ' cluster-card' : '') + (isSelected ? ' selected' : '');
        card.dataset.predictionId = item.id;
        if (item.cluster_type) {
            card.dataset.clusterType = item.cluster_type;
            card.dataset.clusterId = item.cluster_id;
        }
        if (item.scenario) card.dataset.scenario = item.scenario;

        // Checkbox wrapper
        const checkWrap = document.createElement('div');
        checkWrap.className = 'card-checkbox';
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.checked = isSelected;
        checkbox.dataset.id = item.id;
        if (isCluster) {
            checkbox.dataset.clusterType = item.cluster_type;
            checkbox.dataset.clusterId = item.cluster_id;
        }
        checkWrap.appendChild(checkbox);
        card.appendChild(checkWrap);

        // Image wrapper
        const imgWrap = document.createElement('div');
        imgWrap.className = 'card-image';

        // Check if this is a document prediction that should show full image + bbox
        var isDocScenario = item.scenario && (item.scenario.indexOf('document') === 0);

        if (isDocScenario) {
            // Use canvas for bbox overlay on full image
            var canvas = document.createElement('canvas');
            canvas.className = 'bbox-canvas';
            var loadImg = new Image();

            var drawBbox = function() {
                var displayW = imgWrap.clientWidth || 200;
                var displayH = displayW;
                canvas.width = displayW;
                canvas.height = displayH;

                var ctx = canvas.getContext('2d');
                var scale = Math.min(displayW / loadImg.width, displayH / loadImg.height);
                var drawW = loadImg.width * scale;
                var drawH = loadImg.height * scale;
                var offX = (displayW - drawW) / 2;
                var offY = (displayH - drawH) / 2;
                ctx.drawImage(loadImg, offX, offY, drawW, drawH);

                // Draw bbox
                if (item.bbox_x != null && item.bbox_width != null) {
                    var vidW = item.video_width || loadImg.width;
                    var vidH = item.video_height || loadImg.height;
                    // bbox coords are in video pixel space
                    var bx = offX + (item.bbox_x / vidW) * drawW;
                    var by = offY + (item.bbox_y / vidH) * drawH;
                    var bw = (item.bbox_width / vidW) * drawW;
                    var bh = (item.bbox_height / vidH) * drawH;

                    ctx.strokeStyle = '#7C3AED';
                    ctx.lineWidth = 2;
                    ctx.strokeRect(bx, by, bw, bh);

                    // Label
                    var label = item.classification || 'document';
                    ctx.font = '11px system-ui';
                    var tw = ctx.measureText(label).width;
                    ctx.fillStyle = '#7C3AED';
                    ctx.fillRect(bx, by - 16, tw + 8, 16);
                    ctx.fillStyle = '#fff';
                    ctx.fillText(label, bx + 4, by - 4);
                }
            };

            loadImg.onload = drawBbox;
            // Use data-src for lazy loading
            loadImg.dataset.src = '/api/training-gallery/full-image/' + item.id;
            canvas.dataset.loadImg = 'pending';

            imgWrap.appendChild(canvas);

            // Store reference for lazy loading
            canvas._loadImg = loadImg;

            if (this.imageObserver) {
                this.imageObserver.observe(canvas);
            } else {
                loadImg.src = loadImg.dataset.src;
            }
        } else {
            // Existing img element code for non-document predictions
            var img = document.createElement('img');
            img.alt = '';
            img.dataset.src = '/api/training-gallery/crop/' + item.id;
            img.dataset.retries = '0';
            img.onerror = function() {
                var r = parseInt(this.dataset.retries || '0');
                if (r < 3) {
                    this.dataset.retries = r + 1;
                    setTimeout(() => { this.src = this.src.split('?')[0] + '?r=' + (r + 1); }, 500 * (r + 1));
                }
            };
            if (this.imageObserver) {
                this.imageObserver.observe(img);
            } else {
                img.src = img.dataset.src;
            }
            imgWrap.appendChild(img);
        }

        // Cluster badge (static SVG + count text — no user data in markup)
        if (isCluster) {
            const badge = document.createElement('div');
            badge.className = 'cluster-badge';

            const svgNS = 'http://www.w3.org/2000/svg';
            const svg = document.createElementNS(svgNS, 'svg');
            svg.setAttribute('width', '12');
            svg.setAttribute('height', '12');
            svg.setAttribute('viewBox', '0 0 24 24');
            svg.setAttribute('fill', 'currentColor');
            const path = document.createElementNS(svgNS, 'path');
            path.setAttribute('d', 'M4 6H2v14c0 1.1.9 2 2 2h14v-2H4V6zm16-4H8c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2z');
            svg.appendChild(path);
            badge.appendChild(svg);

            const countSpan = document.createElement('span');
            countSpan.textContent = item.cluster_count;
            badge.appendChild(countSpan);
            imgWrap.appendChild(badge);

            imgWrap.style.cursor = 'pointer';
            imgWrap.addEventListener('click', () => {
                this.openCluster(item.cluster_type, item.cluster_id, item.classification);
            });
        }

        card.appendChild(imgWrap);

        // Card info row
        const info = document.createElement('div');
        info.className = 'card-info';

        const labelSpan = document.createElement('span');
        labelSpan.className = 'card-class';
        labelSpan.textContent = item.classification || item.scenario || '\u2014';

        const confSpan = document.createElement('span');
        confSpan.className = 'card-confidence';
        confSpan.textContent = item.confidence != null
            ? Math.round(item.confidence * 100) + '%'
            : '\u2014';

        info.appendChild(labelSpan);
        info.appendChild(confSpan);
        card.appendChild(info);

        // Reject reason badge (rejected mode)
        if (this.filters.status === 'rejected') {
            var reasonBadge = document.createElement('div');
            reasonBadge.className = 'card-reason-badge';
            if (item.reject_reason) {
                reasonBadge.textContent = RejectReasons.labelFor(item.reject_reason) || item.reject_reason;
            } else {
                reasonBadge.textContent = 'Rejected';
                reasonBadge.style.opacity = '0.6';
            }
            card.appendChild(reasonBadge);
        }

        // Similarity score badge / seed badge
        if (item.is_seed) {
            var seedBadge = document.createElement('div');
            seedBadge.className = 'card-similarity-badge card-seed-badge';
            seedBadge.textContent = 'Seed';
            card.appendChild(seedBadge);
        } else if (item.similarity != null) {
            var simBadge = document.createElement('div');
            simBadge.className = 'card-similarity-badge';
            simBadge.textContent = Math.round(item.similarity * 100) + '% match';
            card.appendChild(simBadge);
        }

        // Checkbox change handler
        checkbox.addEventListener('change', (e) => {
            e.stopPropagation();
            this.toggleSelection(item, e.target.checked);
            card.classList.toggle('selected', e.target.checked);
        });

        // Click anywhere on card to toggle checkbox (clusters open modal via imgWrap)
        card.addEventListener('click', (e) => {
            if (e.target === checkbox || e.target.closest('.cluster-badge')) return;
            if (isCluster && e.target.closest('.card-image')) return;
            checkbox.checked = !checkbox.checked;
            this.toggleSelection(item, checkbox.checked);
            card.classList.toggle('selected', checkbox.checked);
        });
        card.style.cursor = 'pointer';

        // Right-click context menu for Find Similar
        card.addEventListener('contextmenu', (e) => {
            if (item.id) this.showContextMenu(e, item);
        });

        return card;
    },

    // ── Infinite Scroll + Lazy Images ────────────────────────────────────

    setupInfiniteScroll() {
        this.imageObserver = new IntersectionObserver((entries) => {
            entries.forEach(entry => {
                if (entry.isIntersecting) {
                    var el = entry.target;
                    if (el.tagName === 'IMG' && el.dataset.src) {
                        el.src = el.dataset.src;
                        delete el.dataset.src;
                    } else if (el.tagName === 'CANVAS' && el._loadImg && el._loadImg.dataset.src) {
                        el._loadImg.src = el._loadImg.dataset.src;
                        delete el._loadImg.dataset.src;
                    }
                    this.imageObserver.unobserve(el);
                }
            });
        }, { rootMargin: '200px' });

        this.observer = new IntersectionObserver((entries) => {
            if (entries[0].isIntersecting && !this.loading && this.page < this.totalPages && !this.similarityMode) {
                this.page++;
                this.loadPage();
            }
        }, { rootMargin: '400px' });

        const sentinel = document.getElementById('scroll-sentinel');
        if (sentinel) this.observer.observe(sentinel);
    },

    // ── Selection Management ─────────────────────────────────────────────

    toggleSelection(item, checked) {
        if (checked) {
            this.selected.add(item.id);
            if (item.cluster_type && item.cluster_count > 1) {
                this.selectedClusters.add(item.cluster_type + ':' + item.cluster_id);
            }
        } else {
            this.selected.delete(item.id);
            if (item.cluster_type) {
                this.selectedClusters.delete(item.cluster_type + ':' + item.cluster_id);
            }
        }
        this.updateActionBar();
    },

    updateActionBar() {
        const bar = document.getElementById('action-bar');
        const total = this.selected.size + this.getClusterExpandedCount();
        if (total > 0) {
            bar.style.display = 'flex';
            document.getElementById('selection-count').textContent =
                total.toLocaleString() + ' selected';
        } else {
            bar.style.display = 'none';
            document.getElementById('reclassify-group').style.display = 'none';
            document.getElementById('btn-reclassify').style.display = '';
        }
    },

    getClusterExpandedCount() {
        let extra = 0;
        this.items.forEach(item => {
            if (item.cluster_type &&
                this.selectedClusters.has(item.cluster_type + ':' + item.cluster_id)) {
                extra += (item.cluster_count - 1);
            }
        });
        return extra;
    },

    async getSelectedPredictionIds() {
        const ids = new Set(this.selected);
        for (const clusterKey of this.selectedClusters) {
            const sepIdx = clusterKey.indexOf(':');
            const type = clusterKey.slice(0, sepIdx);
            const id = clusterKey.slice(sepIdx + 1);
            if (!this.clusterMembers[clusterKey]) {
                try {
                    const resp = await fetch('/api/training-gallery/cluster/' + type + '/' + id);
                    const data = await resp.json();
                    this.clusterMembers[clusterKey] = data.items;
                } catch (e) {
                    console.error('Failed to fetch cluster members for', clusterKey, e);
                }
            }
            if (this.clusterMembers[clusterKey]) {
                this.clusterMembers[clusterKey].forEach(m => ids.add(m.id));
            }
        }
        return Array.from(ids);
    },

    selectAll() {
        this.items.forEach(item => {
            this.selected.add(item.id);
            if (item.cluster_type && item.cluster_count > 1) {
                this.selectedClusters.add(item.cluster_type + ':' + item.cluster_id);
            }
        });
        document.querySelectorAll('.gallery-card .card-checkbox input').forEach(cb => {
            cb.checked = true;
            cb.closest('.gallery-card').classList.add('selected');
        });
        this.updateActionBar();
    },

    deselectAll() {
        this.selected.clear();
        this.selectedClusters.clear();
        document.querySelectorAll('.gallery-card .card-checkbox input').forEach(cb => {
            cb.checked = false;
            cb.closest('.gallery-card').classList.remove('selected');
        });
        this.updateActionBar();
    },

    // ── Cluster Modal ────────────────────────────────────────────────────

    async openCluster(clusterType, clusterId, classification) {
        this.modalOpen = true;
        this.modalClusterType = clusterType;
        this.modalClusterId = clusterId;
        this.modalSelected.clear();

        const modal = document.getElementById('cluster-modal');
        const grid = document.getElementById('modal-grid');
        modal.style.display = 'flex';

        // Reset modal UI
        const loadingEl = document.createElement('div');
        loadingEl.className = 'loading-state';
        loadingEl.textContent = 'Loading cluster...';
        grid.textContent = '';
        grid.appendChild(loadingEl);

        document.getElementById('modal-cluster-title').textContent = classification || 'Cluster';
        document.getElementById('modal-cluster-ref').textContent = clusterType + ':' + clusterId;
        document.getElementById('modal-cluster-count').textContent = '';
        document.getElementById('modal-action-bar').style.display = 'none';
        document.getElementById('modal-reclassify-group').style.display = 'none';
        document.getElementById('modal-btn-reclassify').style.display = '';

        try {
            const resp = await fetch('/api/training-gallery/cluster/' + clusterType + '/' + clusterId + '?status=' + this.filters.status);
            const data = await resp.json();
            this.modalItems = data.items;

            const cacheKey = clusterType + ':' + clusterId;
            this.clusterMembers[cacheKey] = data.items;

            document.getElementById('modal-cluster-count').textContent =
                data.items.length + ' image' + (data.items.length !== 1 ? 's' : '');

            grid.textContent = '';
            data.items.forEach(item => {
                grid.appendChild(this.createModalCard(item));
            });
        } catch (e) {
            console.error('Failed to load cluster:', e);
            grid.textContent = '';
            const errEl = document.createElement('div');
            errEl.className = 'empty-state';
            errEl.textContent = 'Failed to load cluster images.';
            grid.appendChild(errEl);
        }
    },

    createModalCard(item) {
        const card = document.createElement('div');
        card.className = 'modal-card';
        card.dataset.id = item.id;

        const checkWrap = document.createElement('div');
        checkWrap.className = 'card-checkbox';
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.dataset.id = item.id;
        checkWrap.appendChild(checkbox);
        card.appendChild(checkWrap);

        const img = document.createElement('img');
        img.src = '/api/training-gallery/crop/' + item.id;
        img.alt = '';
        img.loading = 'lazy';
        img.dataset.retries = '0';
        img.onerror = function() {
            var r = parseInt(this.dataset.retries || '0');
            if (r < 3) {
                this.dataset.retries = r + 1;
                setTimeout(() => { this.src = this.src.split('?')[0] + '?r=' + (r + 1); }, 500 * (r + 1));
            }
        };
        card.appendChild(img);

        const info = document.createElement('div');
        info.className = 'card-info';
        const confSpan = document.createElement('span');
        confSpan.className = 'card-confidence';
        confSpan.textContent = item.confidence != null
            ? Math.round(item.confidence * 100) + '%'
            : '\u2014';
        info.appendChild(confSpan);
        card.appendChild(info);

        // Reject reason badge (rejected mode)
        if (item.reject_reason) {
            var reasonBadge = document.createElement('div');
            reasonBadge.className = 'card-reason-badge';
            reasonBadge.textContent = RejectReasons.labelFor(item.reject_reason) || item.reject_reason;
            reasonBadge.style.margin = '0 8px 5px';
            card.appendChild(reasonBadge);
        }

        checkbox.addEventListener('change', (e) => {
            if (e.target.checked) {
                this.modalSelected.add(item.id);
                card.classList.add('selected');
            } else {
                this.modalSelected.delete(item.id);
                card.classList.remove('selected');
            }
            this.updateModalActionBar();
        });

        card.addEventListener('click', (e) => {
            if (e.target === checkbox) return;
            checkbox.checked = !checkbox.checked;
            if (checkbox.checked) {
                this.modalSelected.add(item.id);
                card.classList.add('selected');
            } else {
                this.modalSelected.delete(item.id);
                card.classList.remove('selected');
            }
            this.updateModalActionBar();
        });
        card.style.cursor = 'pointer';

        return card;
    },

    closeModal() {
        this.modalOpen = false;
        document.getElementById('cluster-modal').style.display = 'none';
        this.modalSelected.clear();
        this.modalItems = [];
        document.getElementById('modal-action-bar').style.display = 'none';
        document.getElementById('modal-reclassify-group').style.display = 'none';
        document.getElementById('modal-btn-reclassify').style.display = '';
    },

    updateModalActionBar() {
        const bar = document.getElementById('modal-action-bar');
        if (this.modalSelected.size > 0) {
            bar.style.display = 'flex';
            document.getElementById('modal-selection-count').textContent =
                this.modalSelected.size + ' selected';
        } else {
            bar.style.display = 'none';
        }
    },

    // ── Bulk Actions ─────────────────────────────────────────────────────

    async executeBulkAction(action, predictionIds, newClassification, actualClass, newReason) {
        if (!predictionIds.length) return;

        const body = { action: action, prediction_ids: predictionIds };
        if (newClassification) body.new_classification = newClassification;
        if (actualClass) body.actual_class = actualClass;
        if (newReason) body.new_reason = newReason;

        try {
            const resp = await fetch('/api/training-gallery/bulk-action', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body)
            });
            const data = await resp.json();

            if (data.success) {
                const idSet = new Set(predictionIds);

                // update_reason: update badges in-place, don't remove cards
                if (action === 'update_reason' && newReason) {
                    var reasonLabel = RejectReasons.labelFor(newReason) || newReason;
                    idSet.forEach(id => {
                        // Update main grid cards
                        var mainCard = document.querySelector('.gallery-card[data-prediction-id="' + id + '"]');
                        if (mainCard) {
                            var badge = mainCard.querySelector('.card-reason-badge');
                            if (badge) {
                                badge.textContent = reasonLabel;
                            } else {
                                badge = document.createElement('div');
                                badge.className = 'card-reason-badge';
                                badge.textContent = reasonLabel;
                                mainCard.appendChild(badge);
                            }
                        }
                        // Update modal cards
                        var modalCard = document.querySelector('#modal-grid .modal-card[data-id="' + id + '"]');
                        if (modalCard) {
                            var mBadge = modalCard.querySelector('.card-reason-badge');
                            if (mBadge) {
                                mBadge.textContent = reasonLabel;
                            } else {
                                mBadge = document.createElement('div');
                                mBadge.className = 'card-reason-badge';
                                mBadge.textContent = reasonLabel;
                                mBadge.style.margin = '0 8px 5px';
                                modalCard.appendChild(mBadge);
                            }
                        }
                    });
                    // Update item data
                    this.items.forEach(item => {
                        if (idSet.has(item.id)) item.reject_reason = newReason;
                    });
                    this.modalItems.forEach(item => {
                        if (idSet.has(item.id)) item.reject_reason = newReason;
                    });
                    // Deselect
                    this.selected.clear();
                    this.selectedClusters.clear();
                    this.modalSelected.clear();
                    this.updateActionBar();
                    this.updateModalActionBar();
                    // Hide change reason UI
                    document.getElementById('change-reason-group').style.display = 'none';
                    document.getElementById('btn-change-reason').style.display = '';
                    document.getElementById('modal-change-reason-group').style.display = 'none';
                    document.getElementById('modal-btn-change-reason').style.display = '';
                    document.querySelectorAll('.gallery-card .card-checkbox input, #modal-grid input[type=checkbox]').forEach(cb => {
                        cb.checked = false;
                        var parentCard = cb.closest('.gallery-card') || cb.closest('.modal-card');
                        if (parentCard) parentCard.classList.remove('selected');
                    });

                    const affected = data.affected || predictionIds.length;
                    this.showToast(affected + ' ' + (affected !== 1 ? 'images' : 'image') + ' reason updated', false);
                    return;
                }

                // Remove from modal if open
                if (this.modalOpen) {
                    // Remove affected cards from modal DOM
                    idSet.forEach(id => {
                        const modalCard = document.querySelector(
                            '#modal-grid .modal-card[data-id="' + id + '"]'
                        );
                        if (modalCard) modalCard.remove();
                        this.modalSelected.delete(id);
                    });
                    this.modalItems = this.modalItems.filter(item => !idSet.has(item.id));

                    if (this.modalItems.length === 0) {
                        // Cluster is empty — close modal and remove cluster card from main grid
                        const clusterCard = document.querySelector(
                            '.gallery-card[data-cluster-type="' + this.modalClusterType +
                            '"][data-cluster-id="' + this.modalClusterId + '"]'
                        );
                        if (clusterCard) {
                            const cId = parseInt(clusterCard.dataset.predictionId);
                            this.items = this.items.filter(item => item.id !== cId);
                            clusterCard.remove();
                        }
                        this.closeModal();
                    } else {
                        // Update modal count and action bar
                        document.getElementById('modal-cluster-count').textContent =
                            this.modalItems.length + ' image' + (this.modalItems.length !== 1 ? 's' : '');
                        this.updateModalActionBar();
                        // Reset reclassify UI
                        document.getElementById('modal-reclassify-group').style.display = 'none';
                        document.getElementById('modal-btn-reclassify').style.display = '';
                        // Update cluster badge count in main grid
                        const clusterCard = document.querySelector(
                            '.gallery-card[data-cluster-type="' + this.modalClusterType +
                            '"][data-cluster-id="' + this.modalClusterId + '"]'
                        );
                        if (clusterCard) {
                            const badgeSpan = clusterCard.querySelector('.cluster-badge span');
                            if (badgeSpan) badgeSpan.textContent = this.modalItems.length;
                        }
                    }
                }

                // Remove affected cards from main grid
                idSet.forEach(id => {
                    const card = document.querySelector(
                        '.gallery-card[data-prediction-id="' + id + '"]'
                    );
                    if (card) card.remove();
                    this.selected.delete(id);
                });
                this.items = this.items.filter(item => !idSet.has(item.id));
                this.selectedClusters.clear();
                this.updateActionBar();

                const affected = data.affected || predictionIds.length;
                const noun = affected !== 1 ? 'images' : 'image';
                const verb = action === 'remove' ? 'removed'
                    : action === 'requeue' ? 'requeued for review'
                    : action === 'approve' ? 'approved'
                    : 'reclassified';
                this.showToast(affected + ' ' + noun + ' ' + verb, false);

                // Force full reload after actions that change status to ensure view consistency
                if (this.filters.status === 'pending' && (action === 'reclassify' || action === 'approve')) {
                    this.resetAndReload();
                }
                if (this.filters.status === 'rejected' && action === 'approve') {
                    this.resetAndReload();
                }
            } else {
                this.showToast(data.error || 'Action failed', true);
            }
        } catch (e) {
            console.error('Bulk action failed:', e);
            this.showToast('Action failed \u2014 check connection', true);
        }
    },

    // ── Dialogs & Toasts ─────────────────────────────────────────────────

    showConfirm(message, onConfirm) {
        const dialog = document.getElementById('confirm-dialog');
        document.getElementById('confirm-message').textContent = message;
        dialog.style.display = 'flex';

        const okBtn = document.getElementById('confirm-ok');
        const cancelBtn = document.getElementById('confirm-cancel');
        const newOk = okBtn.cloneNode(true);
        const newCancel = cancelBtn.cloneNode(true);
        okBtn.replaceWith(newOk);
        cancelBtn.replaceWith(newCancel);

        const close = () => { dialog.style.display = 'none'; };
        newOk.addEventListener('click', () => { close(); onConfirm(); });
        newCancel.addEventListener('click', close);
    },

    showToast(msg, isError) {
        const toast = document.createElement('div');
        toast.className = 'toast' + (isError ? ' toast-error' : ' toast-success');
        toast.textContent = msg;
        document.body.appendChild(toast);
        setTimeout(() => { if (toast.parentNode) toast.remove(); }, 3200);
    },

    showRejectPrompt(count, onConfirm) {
        var noun = count !== 1 ? 'images' : 'image';
        var dialog = document.getElementById('confirm-dialog');
        document.getElementById('confirm-message').textContent =
            'Remove ' + count.toLocaleString() + ' ' + noun + ' from training data?';
        dialog.style.display = 'flex';

        // Add reject reason UI if not already present
        var reasonWrap = document.getElementById('reject-reason-wrap');
        if (!reasonWrap) {
            reasonWrap = document.createElement('div');
            reasonWrap.id = 'reject-reason-wrap';
            reasonWrap.style.cssText = 'margin-top:12px;text-align:left';

            var label = document.createElement('label');
            label.textContent = 'Reason for removal (optional)';
            label.style.cssText = 'display:block;font-size:13px;color:#94a3b8;margin-bottom:4px';
            reasonWrap.appendChild(label);

            var select = document.createElement('select');
            select.id = 'reject-reason-select';
            select.style.cssText = 'width:100%;padding:6px 8px;border-radius:6px;border:1px solid #334155;background:#1e293b;color:#e2e8f0;font-size:14px';
            select.appendChild(RejectReasons.buildOptions('training'));
            reasonWrap.appendChild(select);

            var msgEl = document.getElementById('confirm-message');
            msgEl.parentNode.insertBefore(reasonWrap, msgEl.nextSibling);
        } else {
            reasonWrap.style.display = '';
            document.getElementById('reject-reason-select').value = '';
        }

        var okBtn = document.getElementById('confirm-ok');
        var cancelBtn = document.getElementById('confirm-cancel');
        var newOk = okBtn.cloneNode(true);
        var newCancel = cancelBtn.cloneNode(true);
        okBtn.replaceWith(newOk);
        cancelBtn.replaceWith(newCancel);

        var close = function() {
            dialog.style.display = 'none';
            if (reasonWrap) reasonWrap.style.display = 'none';
        };
        newOk.addEventListener('click', function() {
            var reason = document.getElementById('reject-reason-select').value || null;
            close();
            onConfirm(reason);
        });
        newCancel.addEventListener('click', close);
    },

    showSubtypePrompt(count, onConfirm) {
        var noun = count !== 1 ? 'images' : 'image';
        var dialog = document.getElementById('confirm-dialog');
        document.getElementById('confirm-message').textContent =
            'Approve ' + count.toLocaleString() + ' ' + noun + '. Classify vehicle type?';
        dialog.style.display = 'flex';

        // Add subtype picker UI
        var subtypeWrap = document.getElementById('subtype-picker-wrap');
        if (!subtypeWrap) {
            subtypeWrap = document.createElement('div');
            subtypeWrap.id = 'subtype-picker-wrap';
            subtypeWrap.style.cssText = 'margin-top:12px;text-align:left';

            var label = document.createElement('label');
            label.textContent = 'Vehicle subtype (optional, skip to approve as-is)';
            label.style.cssText = 'display:block;font-size:13px;color:#94a3b8;margin-bottom:4px';
            subtypeWrap.appendChild(label);

            var select = document.createElement('select');
            select.id = 'subtype-picker-select';
            select.style.cssText = 'width:100%;padding:6px 8px;border-radius:6px;border:1px solid #334155;background:#1e293b;color:#e2e8f0;font-size:14px';
            var options = [
                ['', '— Skip (approve without classifying) —'],
                ['sedan', 'Sedan'],
                ['pickup truck', 'Pickup Truck'],
                ['suv', 'SUV'],
                ['minivan', 'Minivan'],
                ['van', 'Van'],
                ['semi truck', 'Semi Truck'],
                ['dump truck', 'Dump Truck'],
                ['bus', 'Bus'],
                ['motorcycle', 'Motorcycle'],
                ['atv', 'ATV'],
                ['utv', 'UTV'],
                ['tractor', 'Tractor'],
                ['trailer', 'Trailer'],
                ['golf cart', 'Golf Cart'],
                ['skid loader', 'Skid Loader'],
                ['boat', 'Boat'],
                ['person', 'Person']
            ];
            options.forEach(function(o) {
                var opt = document.createElement('option');
                opt.value = o[0];
                opt.textContent = o[1];
                select.appendChild(opt);
            });
            subtypeWrap.appendChild(select);

            var msgEl = document.getElementById('confirm-message');
            msgEl.parentNode.insertBefore(subtypeWrap, msgEl.nextSibling);
        } else {
            subtypeWrap.style.display = '';
            document.getElementById('subtype-picker-select').value = '';
        }

        // Hide reject reason if visible
        var rejectWrap = document.getElementById('reject-reason-wrap');
        if (rejectWrap) rejectWrap.style.display = 'none';

        var okBtn = document.getElementById('confirm-ok');
        var cancelBtn = document.getElementById('confirm-cancel');
        var newOk = okBtn.cloneNode(true);
        var newCancel = cancelBtn.cloneNode(true);
        newOk.textContent = 'Approve';
        newOk.className = 'btn btn-primary';
        okBtn.replaceWith(newOk);
        cancelBtn.replaceWith(newCancel);

        var close = function() {
            dialog.style.display = 'none';
            if (subtypeWrap) subtypeWrap.style.display = 'none';
        };
        newOk.addEventListener('click', function() {
            var subtype = document.getElementById('subtype-picker-select').value || null;
            close();
            onConfirm(subtype);
        });
        newCancel.addEventListener('click', close);
    },

    // ── Reason Filter (Multi-Select) ────────────────────────────────────

    populateReasonFilter(reasonData) {
        var dropdown = document.getElementById('reason-filter-dropdown');
        var btn = document.getElementById('reason-filter-btn');
        dropdown.textContent = '';
        var self = this;
        var selected = this.filters.reject_reasons;

        reasonData.forEach(function(r) {
            var label = document.createElement('label');
            var cb = document.createElement('input');
            cb.type = 'checkbox';
            var filterValue = r.reason || '__none__';
            cb.value = filterValue;
            cb.checked = selected.indexOf(filterValue) !== -1;

            var text = document.createElement('span');
            var displayLabel = r.reason ? (RejectReasons.labelFor(r.reason) || r.reason) : '(No reason)';
            text.textContent = displayLabel;

            var count = document.createElement('span');
            count.className = 'ms-count';
            count.textContent = r.count.toLocaleString();

            label.appendChild(cb);
            label.appendChild(text);
            label.appendChild(count);
            dropdown.appendChild(label);

            cb.addEventListener('change', function() {
                self.onReasonFilterChange();
            });
        });

        this.updateReasonBtnLabel();
    },

    onReasonFilterChange() {
        var checkboxes = document.querySelectorAll('#reason-filter-dropdown input[type=checkbox]');
        var selected = [];
        checkboxes.forEach(function(cb) {
            if (cb.checked) selected.push(cb.value);
        });
        this.filters.reject_reasons = selected;
        this.updateReasonBtnLabel();
        this.resetAndReload();
    },

    updateReasonBtnLabel() {
        var btn = document.getElementById('reason-filter-btn');
        var n = this.filters.reject_reasons.length;
        if (n === 0) {
            btn.textContent = 'All Reasons';
        } else if (n === 1) {
            var val = this.filters.reject_reasons[0];
            btn.textContent = val === '__none__' ? '(No reason)' : (RejectReasons.labelFor(val) || val);
        } else {
            btn.textContent = n + ' Reasons';
        }
    },

    // ── Stats ────────────────────────────────────────────────────────────

    updateModeButtons() {
        const isPending = this.filters.status === 'pending';
        const isRejected = this.filters.status === 'rejected';

        // Main action bar buttons
        const requeueBtn = document.getElementById('btn-requeue');
        const removeBtn = document.getElementById('btn-remove');
        const reclassifyBtn = document.getElementById('btn-reclassify');
        const changeReasonBtn = document.getElementById('btn-change-reason');
        const restoreBtn = document.getElementById('btn-restore');

        // Modal action bar buttons
        const modalRequeueBtn = document.getElementById('modal-btn-requeue');
        const modalRemoveBtn = document.getElementById('modal-btn-remove');
        const modalReclassifyBtn = document.getElementById('modal-btn-reclassify');
        const modalChangeReasonBtn = document.getElementById('modal-btn-change-reason');
        const modalRestoreBtn = document.getElementById('modal-btn-restore');

        if (isRejected) {
            // Rejected mode: show Reclassify + Change Reason + Restore, hide Requeue/Remove
            reclassifyBtn.style.display = '';
            requeueBtn.style.display = 'none';
            removeBtn.style.display = 'none';
            changeReasonBtn.style.display = '';
            restoreBtn.style.display = '';

            modalReclassifyBtn.style.display = '';
            modalRequeueBtn.style.display = 'none';
            modalRemoveBtn.style.display = 'none';
            modalChangeReasonBtn.style.display = '';
            modalRestoreBtn.style.display = '';
        } else {
            // Approved/Pending mode: show normal buttons, hide rejected-mode buttons
            reclassifyBtn.style.display = '';
            requeueBtn.style.display = '';
            removeBtn.style.display = '';
            changeReasonBtn.style.display = 'none';
            restoreBtn.style.display = 'none';

            modalReclassifyBtn.style.display = '';
            modalRequeueBtn.style.display = '';
            modalRemoveBtn.style.display = '';
            modalChangeReasonBtn.style.display = 'none';
            modalRestoreBtn.style.display = 'none';

            requeueBtn.textContent = isPending ? 'Approve' : 'Requeue for Review';
            requeueBtn.className = isPending ? 'btn btn-primary' : 'btn btn-warning';
            modalRequeueBtn.textContent = isPending ? 'Approve' : 'Requeue';
            modalRequeueBtn.className = isPending ? 'btn btn-primary btn-sm' : 'btn btn-warning btn-sm';
        }
    },

    updateStats(total) {
        const el = document.getElementById('filter-stats');
        if (el) el.textContent = total.toLocaleString() + ' training images';
    },

    // ── Similarity Mode ─────────────────────────────────────────────────

    showContextMenu(e, item) {
        e.preventDefault();
        // Remove existing menu
        var old = document.querySelector('.gallery-context-menu');
        if (old) old.remove();

        var menu = document.createElement('div');
        menu.className = 'gallery-context-menu';

        var findBtn = document.createElement('button');
        findBtn.textContent = 'Find Similar';
        findBtn.addEventListener('click', () => {
            menu.remove();
            this.loadSimilar(item.id);
        });
        menu.appendChild(findBtn);

        menu.style.left = e.clientX + 'px';
        menu.style.top = e.clientY + 'px';
        document.body.appendChild(menu);

        // Close on next click
        var closeMenu = (ev) => {
            if (!menu.contains(ev.target)) {
                menu.remove();
                document.removeEventListener('click', closeMenu);
            }
        };
        setTimeout(() => document.addEventListener('click', closeMenu), 0);
    },

    async loadSimilar(seedId) {
        this.similarityMode = true;
        this.similaritySeedId = seedId;
        this.selected.clear();
        this.selectedClusters.clear();
        document.getElementById('action-bar').style.display = 'none';

        // Add "Most Similar" sort option
        var sortSel = document.getElementById('sort-select');
        if (!sortSel.querySelector('option[value="similarity"]')) {
            var opt = document.createElement('option');
            opt.value = 'similarity';
            opt.textContent = 'Most Similar';
            sortSel.insertBefore(opt, sortSel.firstChild);
        }
        sortSel.value = 'similarity';
        this.filters.sort = 'similarity';

        // Show loading
        var grid = document.getElementById('gallery-grid');
        grid.textContent = '';
        var loadingEl = document.createElement('div');
        loadingEl.className = 'loading-state';
        loadingEl.textContent = 'Finding similar images...';
        grid.appendChild(loadingEl);

        // Build params
        var params = new URLSearchParams({ status: this.filters.status });
        if (this.filters.scenario) params.set('scenario', this.filters.scenario);
        if (this.filters.classification) params.set('classification', this.filters.classification);
        if (this.filters.camera) params.set('camera', this.filters.camera);
        if (this.filters.reject_reasons.length) params.set('reject_reasons', this.filters.reject_reasons.join(','));

        try {
            var resp = await fetch('/api/training-gallery/similar/' + seedId + '?' + params);
            var data = await resp.json();

            grid.textContent = '';
            this.items = [];

            if (data.success && (data.seed_item || data.items.length > 0)) {
                this.showSimilarityBanner(seedId, data.seed_classification);

                // Sort remaining items by current sort selection
                var sortVal = this.filters.sort;
                data.items.sort(function(a, b) {
                    if (sortVal === 'confidence') return (b.confidence || 0) - (a.confidence || 0);
                    if (sortVal === 'date') return (b.created_at || '').localeCompare(a.created_at || '');
                    if (sortVal === 'similarity') return (b.similarity || 0) - (a.similarity || 0);
                    return (b.similarity || 0) - (a.similarity || 0);
                });

                // Seed item first, then sorted results
                var allItems = [];
                if (data.seed_item) {
                    data.seed_item.is_seed = true;
                    allItems.push(data.seed_item);
                }
                allItems = allItems.concat(data.items);
                this.renderItems(allItems);
                this.updateStats(allItems.length);
            } else {
                var empty = document.createElement('div');
                empty.className = 'empty-state';
                empty.textContent = data.error || 'No similar images found.';
                grid.appendChild(empty);
            }
        } catch (e) {
            console.error('Similarity search failed:', e);
            grid.textContent = '';
            var errEl = document.createElement('div');
            errEl.className = 'empty-state error';
            errEl.textContent = 'Similarity search failed.';
            grid.appendChild(errEl);
        }
    },

    resortSimilarityItems() {
        // Separate seed from rest
        var seed = null;
        var rest = [];
        for (var i = 0; i < this.items.length; i++) {
            if (this.items[i].is_seed) {
                seed = this.items[i];
            } else {
                rest.push(this.items[i]);
            }
        }
        var sortVal = this.filters.sort;
        rest.sort(function(a, b) {
            if (sortVal === 'confidence') return (b.confidence || 0) - (a.confidence || 0);
            if (sortVal === 'date') return (b.created_at || '').localeCompare(a.created_at || '');
            return (b.similarity || 0) - (a.similarity || 0);
        });
        var sorted = seed ? [seed].concat(rest) : rest;
        this.items = [];
        var grid = document.getElementById('gallery-grid');
        grid.textContent = '';
        this.renderItems(sorted);
    },

    showSimilarityBanner(seedId, seedClass) {
        var existing = document.getElementById('similarity-banner');
        if (existing) existing.remove();

        var banner = document.createElement('div');
        banner.className = 'similarity-banner';
        banner.id = 'similarity-banner';

        var img = document.createElement('img');
        img.className = 'seed-thumb';
        img.src = '/api/training-gallery/crop/' + seedId;
        banner.appendChild(img);

        var text = document.createElement('span');
        text.textContent = 'Visually similar to #' + seedId + (seedClass ? ' (' + seedClass + ')' : '');
        banner.appendChild(text);

        var clearBtn = document.createElement('button');
        clearBtn.className = 'btn btn-ghost btn-sm';
        clearBtn.textContent = 'Clear';
        clearBtn.style.marginLeft = 'auto';
        clearBtn.addEventListener('click', () => this.clearSimilarity());
        banner.appendChild(clearBtn);

        var filterBar = document.getElementById('filter-bar');
        filterBar.parentNode.insertBefore(banner, filterBar.nextSibling);
    },

    clearSimilarity() {
        this.similarityMode = false;
        this.similaritySeedId = null;
        var banner = document.getElementById('similarity-banner');
        if (banner) banner.remove();

        // Remove "Most Similar" sort option and reset to confidence
        var sortSel = document.getElementById('sort-select');
        var simOpt = sortSel.querySelector('option[value="similarity"]');
        if (simOpt) simOpt.remove();
        if (this.filters.sort === 'similarity') {
            this.filters.sort = 'confidence';
            sortSel.value = 'confidence';
        }

        this.resetAndReload();
    },

    // ── Event Binding ────────────────────────────────────────────────────

    bindEvents() {
        // Status toggle
        document.querySelectorAll('#status-toggle .toggle-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                document.querySelectorAll('#status-toggle .toggle-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                this.filters.status = btn.dataset.status;
                // Clear similarity mode if active
                if (this.similarityMode) {
                    this.similarityMode = false;
                    this.similaritySeedId = null;
                    var banner = document.getElementById('similarity-banner');
                    if (banner) banner.remove();
                }
                // Reset state and reload filters (which calls loadPage)
                this.filters.scenario = '';
                this.filters.classification = '';
                document.getElementById('scenario-filter').value = '';
                document.getElementById('classification-filter').value = '';
                this.filters.camera = '';
                this.filters.reject_reasons = [];
                var cameraFilter = document.getElementById('camera-filter');
                if (cameraFilter) cameraFilter.value = '';
                this.page = 1;
                this.items = [];
                this.selected.clear();
                this.selectedClusters.clear();
                document.getElementById('action-bar').style.display = 'none';
                this.updateModeButtons();
                this.syncUrlParams();
                this.loadFilters();
            });
        });

        // Filters
        document.getElementById('scenario-filter').addEventListener('change', (e) => {
            this.filters.scenario = e.target.value;
            const filtered = e.target.value
                ? this.allClasses.filter(c => c.scenario === e.target.value)
                : this.allClasses;
            this.updateClassificationDropdown(filtered);
            this.filters.classification = '';
            document.getElementById('classification-filter').value = '';
            this.resetAndReload();
        });

        document.getElementById('classification-filter').addEventListener('change', (e) => {
            this.filters.classification = e.target.value;
            this.resetAndReload();
        });

        // Camera filter
        var cameraFilter = document.getElementById('camera-filter');
        if (cameraFilter) {
            cameraFilter.addEventListener('change', (e) => {
                this.filters.camera = e.target.value;
                this.resetAndReload();
            });
        }

        document.getElementById('sort-select').addEventListener('change', (e) => {
            this.filters.sort = e.target.value;
            if (this.similarityMode) {
                // Re-sort current items in-place instead of reloading
                this.resortSimilarityItems();
            } else {
                this.resetAndReload();
            }
        });

        // Reason filter multi-select dropdown toggle
        document.getElementById('reason-filter-btn').addEventListener('click', (e) => {
            e.stopPropagation();
            var dd = document.getElementById('reason-filter-dropdown');
            dd.style.display = dd.style.display === 'none' ? '' : 'none';
        });
        // Close dropdown on outside click
        document.addEventListener('click', (e) => {
            var wrap = document.getElementById('reason-filter-wrap');
            var dd = document.getElementById('reason-filter-dropdown');
            if (dd && dd.style.display !== 'none' && !wrap.contains(e.target)) {
                dd.style.display = 'none';
            }
        });

        // Selection
        document.getElementById('btn-select-all').addEventListener('click', () => this.selectAll());
        document.getElementById('btn-deselect-all').addEventListener('click', () => this.deselectAll());

        // Action bar — Reclassify
        document.getElementById('btn-reclassify').addEventListener('click', () => {
            document.getElementById('reclassify-input').value = '';
            document.getElementById('reclassify-group').style.display = 'flex';
            document.getElementById('btn-reclassify').style.display = 'none';
        });
        document.getElementById('btn-cancel-reclassify').addEventListener('click', () => {
            document.getElementById('reclassify-group').style.display = 'none';
            document.getElementById('btn-reclassify').style.display = '';
            document.getElementById('reclassify-input').value = '';
        });
        document.getElementById('btn-apply-reclassify').addEventListener('click', async () => {
            const newClass = document.getElementById('reclassify-input').value.trim();
            if (!newClass) return;
            const ids = await this.getSelectedPredictionIds();
            this.executeBulkAction('reclassify', ids, newClass);
        });

        // Action bar — Requeue / Approve
        document.getElementById('btn-requeue').addEventListener('click', async () => {
            const ids = await this.getSelectedPredictionIds();
            const n = ids.length;
            const isPending = this.filters.status === 'pending';
            if (isPending) {
                this.executeBulkAction('approve', ids);
            } else {
                var msg = 'Requeue ' + n.toLocaleString() + ' image' + (n !== 1 ? 's' : '') + ' for review?';
                this.showConfirm(msg, () => this.executeBulkAction('requeue', ids));
            }
        });

        // Action bar — Remove (with optional reject reason)
        document.getElementById('btn-remove').addEventListener('click', async () => {
            const ids = await this.getSelectedPredictionIds();
            const n = ids.length;
            this.showRejectPrompt(n, (actualClass) => {
                this.executeBulkAction('remove', ids, null, actualClass);
            });
        });

        // Action bar — Change Reason (rejected mode)
        document.getElementById('btn-change-reason').addEventListener('click', () => {
            var sel = document.getElementById('change-reason-select');
            sel.textContent = '';
            sel.appendChild(RejectReasons.buildOptions('training', '\u2014 Select reason \u2014'));
            document.getElementById('change-reason-group').style.display = 'flex';
            document.getElementById('btn-change-reason').style.display = 'none';
        });
        document.getElementById('btn-cancel-reason').addEventListener('click', () => {
            document.getElementById('change-reason-group').style.display = 'none';
            document.getElementById('btn-change-reason').style.display = '';
        });
        document.getElementById('btn-apply-reason').addEventListener('click', async () => {
            var reason = document.getElementById('change-reason-select').value;
            if (!reason) return;
            var ids = await this.getSelectedPredictionIds();
            this.executeBulkAction('update_reason', ids, null, null, reason);
        });

        // Action bar — Restore (rejected mode → approve)
        document.getElementById('btn-restore').addEventListener('click', async () => {
            var ids = await this.getSelectedPredictionIds();
            var n = ids.length;
            var msg = 'Restore ' + n.toLocaleString() + ' image' + (n !== 1 ? 's' : '') + ' to approved?';
            this.showConfirm(msg, () => this.executeBulkAction('approve', ids));
        });

        // Modal — close
        document.getElementById('modal-close').addEventListener('click', () => this.closeModal());
        document.getElementById('cluster-modal').addEventListener('click', (e) => {
            if (e.target === document.getElementById('cluster-modal')) this.closeModal();
        });

        // Modal — select all
        document.getElementById('modal-select-all').addEventListener('click', () => {
            this.modalItems.forEach(item => this.modalSelected.add(item.id));
            document.querySelectorAll('#modal-grid input[type=checkbox]').forEach(cb => {
                cb.checked = true;
                cb.closest('.modal-card').classList.add('selected');
            });
            this.updateModalActionBar();
        });

        // Modal — Reclassify
        document.getElementById('modal-btn-reclassify').addEventListener('click', () => {
            document.getElementById('modal-reclassify-input').value = '';
            document.getElementById('modal-reclassify-group').style.display = 'flex';
            document.getElementById('modal-btn-reclassify').style.display = 'none';
        });
        document.getElementById('modal-cancel-reclassify').addEventListener('click', () => {
            document.getElementById('modal-reclassify-group').style.display = 'none';
            document.getElementById('modal-btn-reclassify').style.display = '';
            document.getElementById('modal-reclassify-input').value = '';
        });
        document.getElementById('modal-apply-reclassify').addEventListener('click', () => {
            const newClass = document.getElementById('modal-reclassify-input').value.trim();
            if (!newClass) return;
            this.executeBulkAction('reclassify', Array.from(this.modalSelected), newClass);
        });

        // Modal — Requeue / Approve
        document.getElementById('modal-btn-requeue').addEventListener('click', () => {
            const ids = Array.from(this.modalSelected);
            const n = ids.length;
            const isPending = this.filters.status === 'pending';
            if (isPending) {
                this.executeBulkAction('approve', ids);
            } else {
                var msg = 'Requeue ' + n + ' image' + (n !== 1 ? 's' : '') + ' for review?';
                this.showConfirm(msg, () => this.executeBulkAction('requeue', ids));
            }
        });

        // Modal — Remove (with optional reject reason)
        document.getElementById('modal-btn-remove').addEventListener('click', () => {
            const ids = Array.from(this.modalSelected);
            const n = ids.length;
            this.showRejectPrompt(n, (actualClass) => {
                this.executeBulkAction('remove', ids, null, actualClass);
            });
        });

        // Modal — Change Reason (rejected mode)
        document.getElementById('modal-btn-change-reason').addEventListener('click', () => {
            var sel = document.getElementById('modal-change-reason-select');
            sel.textContent = '';
            sel.appendChild(RejectReasons.buildOptions('training', '\u2014 Select reason \u2014'));
            document.getElementById('modal-change-reason-group').style.display = 'flex';
            document.getElementById('modal-btn-change-reason').style.display = 'none';
        });
        document.getElementById('modal-cancel-reason').addEventListener('click', () => {
            document.getElementById('modal-change-reason-group').style.display = 'none';
            document.getElementById('modal-btn-change-reason').style.display = '';
        });
        document.getElementById('modal-apply-reason').addEventListener('click', () => {
            var reason = document.getElementById('modal-change-reason-select').value;
            if (!reason) return;
            var ids = Array.from(this.modalSelected);
            this.executeBulkAction('update_reason', ids, null, null, reason);
        });

        // Modal — Restore (rejected mode → approve)
        document.getElementById('modal-btn-restore').addEventListener('click', () => {
            var ids = Array.from(this.modalSelected);
            var n = ids.length;
            var msg = 'Restore ' + n + ' image' + (n !== 1 ? 's' : '') + ' to approved?';
            this.showConfirm(msg, () => this.executeBulkAction('approve', ids));
        });

        // Keyboard
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                if (this.modalOpen) {
                    this.closeModal();
                } else {
                    const confirmDialog = document.getElementById('confirm-dialog');
                    if (confirmDialog && confirmDialog.style.display !== 'none') {
                        confirmDialog.style.display = 'none';
                    }
                }
            }
        });
    },

    // ── Utilities ────────────────────────────────────────────────────────

    resetAndReload() {
        this.page = 1;
        this.items = [];
        this.selected.clear();
        this.selectedClusters.clear();
        // Clear similarity mode if active
        if (this.similarityMode) {
            this.similarityMode = false;
            this.similaritySeedId = null;
            var simBanner = document.getElementById('similarity-banner');
            if (simBanner) simBanner.remove();
        }
        this.syncUrlParams();

        const grid = document.getElementById('gallery-grid');
        grid.textContent = '';
        const loadingEl = document.createElement('div');
        loadingEl.className = 'loading-state';
        loadingEl.textContent = 'Loading training images...';
        grid.appendChild(loadingEl);

        document.getElementById('action-bar').style.display = 'none';
        this.loadPage();
    }
};

document.addEventListener('DOMContentLoaded', () => TrainingGallery.init());
