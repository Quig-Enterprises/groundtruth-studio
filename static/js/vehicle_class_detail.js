// Vehicle Class Detail Page JavaScript

const classDetail = {
    className: CLASS_NAME,
    predictions: [],
    selected: new Set(),
    lastClickedIndex: null,
    currentStatus: 'all',
    limit: 200,
    offset: 0,
    total: 0,
    reclassClasses: [],

    init() {
        this.loadPredictions();
        this.loadReclassificationClasses();
        this.bindEvents();
    },

    bindEvents() {
        // Filter tabs
        document.querySelectorAll('.filter-tab').forEach(tab => {
            tab.addEventListener('click', () => {
                document.querySelector('.filter-tab.active').classList.remove('active');
                tab.classList.add('active');
                this.currentStatus = tab.dataset.status;
                this.offset = 0;
                this.selected.clear();
                this.updateToolbar();
                this.loadPredictions();
            });
        });

        // Select all / deselect all
        document.getElementById('btn-select-all').addEventListener('click', () => this.selectAll());
        document.getElementById('btn-deselect-all').addEventListener('click', () => this.deselectAll());
        document.getElementById('toolbar-deselect').addEventListener('click', () => this.deselectAll());

        // Reclassify dropdown
        document.getElementById('btn-reclassify').addEventListener('click', (e) => {
            e.stopPropagation();
            const dropdown = document.getElementById('reclassify-dropdown');
            dropdown.classList.toggle('open');
            if (dropdown.classList.contains('open')) {
                document.getElementById('dropdown-search').value = '';
                this.renderDropdownList('');
                document.getElementById('dropdown-search').focus();
            }
        });

        document.getElementById('dropdown-search').addEventListener('input', (e) => {
            this.renderDropdownList(e.target.value);
        });

        // Close dropdown on outside click
        document.addEventListener('click', (e) => {
            const dropdown = document.getElementById('reclassify-dropdown');
            if (!e.target.closest('.reclassify-wrapper')) {
                dropdown.classList.remove('open');
            }
        });

        // Requeue button
        document.getElementById('btn-requeue').addEventListener('click', () => this.batchRequeue());

        // Pagination
        document.getElementById('btn-prev').addEventListener('click', () => {
            if (this.offset > 0) {
                this.offset = Math.max(0, this.offset - this.limit);
                this.selected.clear();
                this.updateToolbar();
                this.loadPredictions();
            }
        });

        document.getElementById('btn-next').addEventListener('click', () => {
            if (this.offset + this.limit < this.total) {
                this.offset += this.limit;
                this.selected.clear();
                this.updateToolbar();
                this.loadPredictions();
            }
        });
    },

    async loadPredictions() {
        const container = document.getElementById('grid-container');
        container.className = 'loading-state';
        container.textContent = 'Loading detections...';

        try {
            const params = new URLSearchParams({
                class: this.className,
                status: this.currentStatus,
                limit: this.limit,
                offset: this.offset
            });

            const response = await fetch('/api/ai/predictions/by-class?' + params);
            const result = await response.json();

            if (!result.success) {
                this.showEmptyState(container, result.error || 'Failed to load');
                return;
            }

            this.predictions = result.predictions;
            this.total = result.total;

            document.getElementById('total-count').textContent = this.total + ' detection' + (this.total !== 1 ? 's' : '');

            this.renderGrid();
            this.updatePagination();
        } catch (error) {
            console.error('Error loading predictions:', error);
            this.showEmptyState(container, 'Failed to load detections. Please try again.');
        }
    },

    showEmptyState(container, message, submessage) {
        container.className = 'empty-state';
        container.textContent = '';
        const p = document.createElement('p');
        p.textContent = message;
        container.appendChild(p);
        if (submessage) {
            const small = document.createElement('small');
            small.textContent = submessage;
            container.appendChild(small);
        }
    },

    async loadReclassificationClasses() {
        try {
            const response = await fetch('/api/ai/reclassification-classes');
            const result = await response.json();
            if (result.success && result.classes) {
                this.reclassClasses = result.classes.map(c => c.class_name || c);
            }
        } catch (e) {
            console.error('Error loading reclassification classes:', e);
        }
    },

    renderGrid() {
        const container = document.getElementById('grid-container');

        if (this.predictions.length === 0) {
            this.showEmptyState(container, 'No detections found',
                'Try a different filter or check if detections exist for this class');
            return;
        }

        container.className = 'thumb-grid';
        container.textContent = '';

        this.predictions.forEach((pred, index) => {
            const card = document.createElement('div');
            card.className = 'thumb-card status-' + pred.review_status;
            card.dataset.id = pred.id;
            card.dataset.index = index;

            if (this.selected.has(pred.id)) {
                card.classList.add('selected');
            }

            // Checkbox overlay
            const checkbox = document.createElement('div');
            checkbox.className = 'thumb-checkbox';
            card.appendChild(checkbox);

            // Status dot
            const statusDot = document.createElement('div');
            statusDot.className = 'thumb-status-dot ' + pred.review_status;
            card.appendChild(statusDot);

            // Image (lazy loaded)
            const img = document.createElement('img');
            img.loading = 'lazy';
            img.src = '/thumbnails/crop/' + pred.id;
            img.alt = this.className + ' detection';
            img.onerror = function() {
                this.style.background = '#dee2e6';
                this.alt = 'Image unavailable';
            };
            card.appendChild(img);

            // Confidence badge
            const confBadge = document.createElement('div');
            confBadge.className = 'thumb-confidence';
            confBadge.textContent = (pred.confidence * 100).toFixed(0) + '%';
            card.appendChild(confBadge);

            // Click handler
            card.addEventListener('click', (e) => {
                this.handleCardClick(pred.id, index, e);
            });

            container.appendChild(card);
        });
    },

    handleCardClick(predId, index, event) {
        if (event.shiftKey && this.lastClickedIndex !== null) {
            // Range select
            const start = Math.min(this.lastClickedIndex, index);
            const end = Math.max(this.lastClickedIndex, index);
            for (let i = start; i <= end; i++) {
                const id = this.predictions[i].id;
                this.selected.add(id);
                const card = document.querySelector('[data-index="' + i + '"]');
                if (card) card.classList.add('selected');
            }
        } else {
            // Toggle single
            if (this.selected.has(predId)) {
                this.selected.delete(predId);
                const card = document.querySelector('[data-id="' + predId + '"]');
                if (card) card.classList.remove('selected');
            } else {
                this.selected.add(predId);
                const card = document.querySelector('[data-id="' + predId + '"]');
                if (card) card.classList.add('selected');
            }
        }

        this.lastClickedIndex = index;
        this.updateToolbar();
    },

    selectAll() {
        this.predictions.forEach((pred, index) => {
            this.selected.add(pred.id);
            const card = document.querySelector('[data-index="' + index + '"]');
            if (card) card.classList.add('selected');
        });
        this.updateToolbar();
    },

    deselectAll() {
        this.selected.clear();
        document.querySelectorAll('.thumb-card.selected').forEach(card => {
            card.classList.remove('selected');
        });
        this.lastClickedIndex = null;
        this.updateToolbar();
    },

    updateToolbar() {
        const toolbar = document.getElementById('batch-toolbar');
        const count = this.selected.size;

        if (count > 0 && canWrite) {
            toolbar.classList.add('visible');
            document.getElementById('selection-count').textContent = count + ' selected';
        } else {
            toolbar.classList.remove('visible');
        }
    },

    updatePagination() {
        const bar = document.getElementById('pagination-bar');
        const info = document.getElementById('pagination-info');
        const prevBtn = document.getElementById('btn-prev');
        const nextBtn = document.getElementById('btn-next');

        if (this.total <= this.limit) {
            bar.style.display = 'none';
            return;
        }

        bar.style.display = 'flex';
        const start = this.offset + 1;
        const end = Math.min(this.offset + this.limit, this.total);
        info.textContent = start + '-' + end + ' of ' + this.total;
        prevBtn.disabled = this.offset === 0;
        nextBtn.disabled = this.offset + this.limit >= this.total;
    },

    renderDropdownList(filter) {
        const list = document.getElementById('dropdown-list');
        list.textContent = '';

        // Combine known classes with reclassification classes
        const knownClasses = [
            'sedan', 'pickup truck', 'SUV', 'minivan', 'van',
            'tractor', 'ATV', 'UTV', 'snowmobile', 'golf cart',
            'motorcycle', 'trailer', 'bus', 'semi truck', 'dump truck',
            'rowboat', 'fishing boat', 'speed boat', 'pontoon boat',
            'kayak', 'canoe', 'sailboat', 'jet ski'
        ];

        const allClasses = [...new Set([...knownClasses, ...this.reclassClasses])];
        const filterLower = filter.toLowerCase();
        const filtered = allClasses
            .filter(c => c.toLowerCase().includes(filterLower))
            .sort();

        if (filtered.length === 0) {
            const empty = document.createElement('div');
            empty.className = 'dropdown-item';
            empty.textContent = 'No matching classes';
            empty.style.color = '#999';
            list.appendChild(empty);
            return;
        }

        filtered.forEach(cls => {
            const item = document.createElement('div');
            item.className = 'dropdown-item';
            item.textContent = cls;
            item.addEventListener('click', () => {
                this.batchReclassify(cls);
                document.getElementById('reclassify-dropdown').classList.remove('open');
            });
            list.appendChild(item);
        });
    },

    async batchReclassify(targetClass) {
        if (this.selected.size === 0) return;

        const ids = Array.from(this.selected);
        const confirmMsg = 'Reclassify ' + ids.length + ' detection' + (ids.length > 1 ? 's' : '') + ' to "' + targetClass + '"?';
        if (!confirm(confirmMsg)) return;

        try {
            const response = await fetch('/api/ai/predictions/batch-update-class', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    prediction_ids: ids,
                    vehicle_subtype: targetClass
                })
            });

            const result = await response.json();

            if (result.success) {
                this.selected.clear();
                this.updateToolbar();
                this.loadPredictions();
            } else {
                alert('Reclassification failed: ' + (result.error || 'Unknown error'));
            }
        } catch (error) {
            console.error('Batch reclassify error:', error);
            alert('Failed to reclassify. Please try again.');
        }
    },

    async batchRequeue() {
        if (this.selected.size === 0) return;

        const ids = Array.from(this.selected);
        const confirmMsg = 'Requeue ' + ids.length + ' detection' + (ids.length > 1 ? 's' : '') + ' for review?';
        if (!confirm(confirmMsg)) return;

        try {
            const response = await fetch('/api/ai/predictions/batch-update-class', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    prediction_ids: ids,
                    requeue: true
                })
            });

            const result = await response.json();

            if (result.success) {
                this.selected.clear();
                this.updateToolbar();
                this.loadPredictions();
            } else {
                alert('Requeue failed: ' + (result.error || 'Unknown error'));
            }
        } catch (error) {
            console.error('Batch requeue error:', error);
            alert('Failed to requeue. Please try again.');
        }
    }
};

document.addEventListener('DOMContentLoaded', () => classDetail.init());
