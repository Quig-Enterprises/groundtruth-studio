/* Training Gallery — vanilla JS module */
var TrainingGallery = {
    // State
    items: [],
    page: 1,
    perPage: 60,
    totalPages: 0,
    loading: false,
    filters: { scenario: '', classification: '', sort: 'confidence' },
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

    // ── Initialization ───────────────────────────────────────────────────

    init() {
        this.loadFilters();
        this.setupInfiniteScroll();
        this.bindEvents();
    },

    // ── Filter Loading ───────────────────────────────────────────────────

    async loadFilters() {
        try {
            const resp = await fetch('/api/training-gallery/filters');
            const data = await resp.json();

            const scenarioSel = document.getElementById('scenario-filter');
            data.scenarios.forEach(s => {
                const opt = document.createElement('option');
                opt.value = s.scenario;
                opt.textContent = s.scenario + ' (' + s.count + ')';
                scenarioSel.appendChild(opt);
            });

            this.allClasses = data.classifications;
            this.updateClassificationDropdown(data.classifications);
            this.loadPage();
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
        ['reclassify-list', 'modal-reclassify-list'].forEach(id => {
            const dl = document.getElementById(id);
            if (!dl) return;
            dl.innerHTML = '';
            this.allClasses.forEach(c => {
                const opt = document.createElement('option');
                opt.value = c.name;
                dl.appendChild(opt);
            });
        });
    },

    // ── Page Loading ─────────────────────────────────────────────────────

    async loadPage() {
        if (this.loading) return;
        this.loading = true;

        const params = new URLSearchParams({
            page: this.page,
            per_page: this.perPage,
            sort: this.filters.sort
        });
        if (this.filters.scenario) params.set('scenario', this.filters.scenario);
        if (this.filters.classification) params.set('classification', this.filters.classification);

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

        const img = document.createElement('img');
        img.alt = '';
        img.dataset.src = '/api/training-gallery/crop/' + item.id;
        if (this.imageObserver) {
            this.imageObserver.observe(img);
        } else {
            img.src = img.dataset.src;
        }
        imgWrap.appendChild(img);

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

        // Checkbox change handler
        checkbox.addEventListener('change', (e) => {
            e.stopPropagation();
            this.toggleSelection(item, e.target.checked);
            card.classList.toggle('selected', e.target.checked);
        });

        return card;
    },

    // ── Infinite Scroll + Lazy Images ────────────────────────────────────

    setupInfiniteScroll() {
        this.imageObserver = new IntersectionObserver((entries) => {
            entries.forEach(entry => {
                if (entry.isIntersecting) {
                    const img = entry.target;
                    if (img.dataset.src) {
                        img.src = img.dataset.src;
                        delete img.dataset.src;
                    }
                    this.imageObserver.unobserve(img);
                }
            });
        }, { rootMargin: '200px' });

        this.observer = new IntersectionObserver((entries) => {
            if (entries[0].isIntersecting && !this.loading && this.page < this.totalPages) {
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
            const resp = await fetch('/api/training-gallery/cluster/' + clusterType + '/' + clusterId);
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

    async executeBulkAction(action, predictionIds, newClassification) {
        if (!predictionIds.length) return;

        const body = { action: action, prediction_ids: predictionIds };
        if (newClassification) body.new_classification = newClassification;

        try {
            const resp = await fetch('/api/training-gallery/bulk-action', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body)
            });
            const data = await resp.json();

            if (data.success) {
                predictionIds.forEach(id => {
                    const card = document.querySelector(
                        '.gallery-card[data-prediction-id="' + id + '"]'
                    );
                    if (card) card.remove();
                    this.selected.delete(id);
                });

                this.items = this.items.filter(item => !predictionIds.includes(item.id));
                this.selectedClusters.clear();
                this.updateActionBar();

                if (this.modalOpen) this.closeModal();

                const affected = data.affected || predictionIds.length;
                const noun = affected !== 1 ? 'images' : 'image';
                const verb = action === 'remove' ? 'removed'
                    : action === 'requeue' ? 'requeued for review'
                    : 'reclassified';
                this.showToast(affected + ' ' + noun + ' ' + verb, false);
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

    // ── Stats ────────────────────────────────────────────────────────────

    updateStats(total) {
        const el = document.getElementById('filter-stats');
        if (el) el.textContent = total.toLocaleString() + ' training images';
    },

    // ── Event Binding ────────────────────────────────────────────────────

    bindEvents() {
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

        document.getElementById('sort-select').addEventListener('change', (e) => {
            this.filters.sort = e.target.value;
            this.resetAndReload();
        });

        // Selection
        document.getElementById('btn-select-all').addEventListener('click', () => this.selectAll());
        document.getElementById('btn-deselect-all').addEventListener('click', () => this.deselectAll());

        // Action bar — Reclassify
        document.getElementById('btn-reclassify').addEventListener('click', () => {
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

        // Action bar — Requeue
        document.getElementById('btn-requeue').addEventListener('click', async () => {
            const ids = await this.getSelectedPredictionIds();
            const n = ids.length;
            this.showConfirm(
                'Requeue ' + n.toLocaleString() + ' image' + (n !== 1 ? 's' : '') + ' for review?',
                () => this.executeBulkAction('requeue', ids)
            );
        });

        // Action bar — Remove
        document.getElementById('btn-remove').addEventListener('click', async () => {
            const ids = await this.getSelectedPredictionIds();
            const n = ids.length;
            this.showConfirm(
                'Permanently remove ' + n.toLocaleString() + ' image' + (n !== 1 ? 's' : '') + ' from training data?',
                () => this.executeBulkAction('remove', ids)
            );
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

        // Modal — Requeue
        document.getElementById('modal-btn-requeue').addEventListener('click', () => {
            const ids = Array.from(this.modalSelected);
            const n = ids.length;
            this.showConfirm(
                'Requeue ' + n + ' image' + (n !== 1 ? 's' : '') + ' for review?',
                () => this.executeBulkAction('requeue', ids)
            );
        });

        // Modal — Remove
        document.getElementById('modal-btn-remove').addEventListener('click', () => {
            const ids = Array.from(this.modalSelected);
            const n = ids.length;
            this.showConfirm(
                'Permanently remove ' + n + ' image' + (n !== 1 ? 's' : '') + ' from training data?',
                () => this.executeBulkAction('remove', ids)
            );
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
