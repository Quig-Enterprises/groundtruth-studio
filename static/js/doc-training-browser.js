/* Document Training Data Browser — vanilla JS module */
var DocTrainingBrowser = {
    // State
    items: [],
    page: 1,
    perPage: 60,
    totalPages: 0,
    loading: false,
    filters: { split: 'train', class_id: '', source: '', issuer: '' },
    observer: null,
    imageObserver: null,
    stats: {},

    // ── Initialization ───────────────────────────────────────────────────

    init() {
        this.loadStats();
        this.loadFiltersFromURL();
        this.loadClassOptions();
        this.loadFilterOptions();
        this.setupInfiniteScroll();
        this.bindEvents();
        this.loadPage();
    },

    // ── Class Options Loading ─────────────────────────────────────────────

    loadClassOptions() {
        fetch('/api/doc-training/classes')
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.success && data.classes) {
                    var sel = document.getElementById('class-filter');
                    data.classes.forEach(function(cls) {
                        var opt = document.createElement('option');
                        opt.value = cls.id;
                        opt.textContent = cls.name;
                        sel.appendChild(opt);
                    });
                    // Restore filter from URL if present
                    var params = new URLSearchParams(window.location.search);
                    if (params.has('class_id')) {
                        sel.value = params.get('class_id');
                    }
                }
            });
    },

    // ── Stats Loading ────────────────────────────────────────────────────

    async loadStats() {
        try {
            var resp = await fetch('/api/doc-training/stats');
            var data = await resp.json();
            if (data.success) {
                this.stats = data;
            }
        } catch (e) {
            console.error('Failed to load stats:', e);
        }
    },

    // ── Cascading Filter Options ──────────────────────────────────────────

    async loadFilterOptions() {
        try {
            var params = new URLSearchParams({ split: this.filters.split });
            if (this.filters.class_id !== '') params.set('class_id', this.filters.class_id);
            if (this.filters.source) params.set('source', this.filters.source);

            var resp = await fetch('/api/doc-training/filter-options?' + params);
            var data = await resp.json();
            if (!data.success) return;

            // Update source dropdown
            var sourceSel = document.getElementById('source-filter');
            var currentSource = sourceSel.value;
            while (sourceSel.options.length > 1) sourceSel.remove(1);
            data.sources.forEach(function(src) {
                var opt = document.createElement('option');
                opt.value = src.code;
                opt.textContent = src.code.toUpperCase() + ' (' + src.count + ')';
                sourceSel.appendChild(opt);
            });
            // Restore selection if still valid
            if (currentSource && Array.from(sourceSel.options).some(function(o) { return o.value === currentSource; })) {
                sourceSel.value = currentSource;
            } else if (currentSource) {
                sourceSel.value = '';
                this.filters.source = '';
            }

            // Update issuer dropdown
            var issuerSel = document.getElementById('issuer-filter');
            var currentIssuer = issuerSel.value;
            while (issuerSel.options.length > 1) issuerSel.remove(1);
            data.issuers.forEach(function(iss) {
                var opt = document.createElement('option');
                opt.value = iss.code;
                opt.textContent = iss.code + ' (' + iss.count + ')';
                issuerSel.appendChild(opt);
            });
            if (currentIssuer && Array.from(issuerSel.options).some(function(o) { return o.value === currentIssuer; })) {
                issuerSel.value = currentIssuer;
            } else if (currentIssuer) {
                issuerSel.value = '';
                this.filters.issuer = '';
            }
        } catch (e) {
            console.error('Failed to load filter options:', e);
        }
    },

    // ── Page Loading ─────────────────────────────────────────────────────

    async loadPage() {
        if (this.loading) return;
        this.loading = true;

        var params = new URLSearchParams({
            split: this.filters.split,
            page: this.page,
            per_page: this.perPage
        });
        if (this.filters.class_id !== '') params.set('class_id', this.filters.class_id);
        if (this.filters.source) params.set('source', this.filters.source);
        if (this.filters.issuer) params.set('issuer', this.filters.issuer);

        try {
            var resp = await fetch('/api/doc-training/items?' + params);
            var data = await resp.json();

            if (this.page === 1) {
                var grid = document.getElementById('gallery-grid');
                grid.textContent = '';
                var initLoading = document.getElementById('initial-loading');
                if (initLoading) initLoading.style.display = 'none';
            }

            this.totalPages = data.pages;
            this.renderItems(data.items);
            this.updateStats(data.total);

            if (data.items.length === 0 && this.page === 1) {
                var empty = document.createElement('div');
                empty.className = 'empty-state';
                empty.textContent = 'No training images match the current filters.';
                document.getElementById('gallery-grid').appendChild(empty);
            }
        } catch (e) {
            console.error('Failed to load page:', e);
            if (this.page === 1) {
                var errEl = document.createElement('div');
                errEl.className = 'empty-state error';
                errEl.textContent = 'Failed to load training images.';
                document.getElementById('gallery-grid').appendChild(errEl);
            }
        }

        this.loading = false;
    },

    // ── Rendering ────────────────────────────────────────────────────────

    renderItems(items) {
        var grid = document.getElementById('gallery-grid');
        var self = this;
        items.forEach(function(item) {
            grid.appendChild(self.createCard(item));
            self.items.push(item);
        });
    },

    createCard(item) {
        var card = document.createElement('div');
        card.className = 'gallery-card';

        var imgWrap = document.createElement('div');
        imgWrap.className = 'card-image';

        // Use canvas for bbox overlay
        var canvas = document.createElement('canvas');
        canvas.className = 'bbox-canvas';

        var img = new Image();
        img.onload = function() {
            // Set canvas size to match display container
            var displayW = imgWrap.clientWidth || 200;
            var displayH = displayW; // square aspect
            canvas.width = displayW;
            canvas.height = displayH;

            var ctx = canvas.getContext('2d');

            // Draw image (contain fit)
            var scale = Math.min(displayW / img.width, displayH / img.height);
            var drawW = img.width * scale;
            var drawH = img.height * scale;
            var offsetX = (displayW - drawW) / 2;
            var offsetY = (displayH - drawH) / 2;
            ctx.drawImage(img, offsetX, offsetY, drawW, drawH);

            // Draw bboxes
            item.bboxes.forEach(function(bbox) {
                var bx = offsetX + (bbox.x_center - bbox.w / 2) * drawW;
                var by = offsetY + (bbox.y_center - bbox.h / 2) * drawH;
                var bw = bbox.w * drawW;
                var bh = bbox.h * drawH;

                ctx.strokeStyle = bbox.color;
                ctx.lineWidth = 2;
                ctx.strokeRect(bx, by, bw, bh);

                // Label background
                ctx.fillStyle = bbox.color;
                var labelText = bbox.class_name;
                ctx.font = '11px system-ui';
                var textW = ctx.measureText(labelText).width;
                ctx.fillRect(bx, by - 16, textW + 8, 16);
                ctx.fillStyle = '#fff';
                ctx.fillText(labelText, bx + 4, by - 4);
            });
        };

        // Store thumbnail src for lazy loading (grid uses smaller cached thumbnails)
        var thumbUrl = item.image_url.replace(
            '/api/doc-training/image/',
            '/api/doc-training/thumbnail/'
        );
        img.dataset.src = thumbUrl;
        // Attach img reference to canvas for lazy load observer
        canvas._lazyImg = img;

        imgWrap.appendChild(canvas);
        card.appendChild(imgWrap);

        // Observe canvas for lazy loading
        if (this.imageObserver) {
            this.imageObserver.observe(canvas);
        } else {
            // Fallback: load immediately
            img.src = img.dataset.src;
        }

        // Card info: filename + class labels
        var info = document.createElement('div');
        info.className = 'card-info';

        var labelSpan = document.createElement('span');
        labelSpan.className = 'card-class';
        var classNames = item.bboxes.map(function(b) { return b.class_name; });
        // Deduplicate
        var unique = classNames.filter(function(v, i, a) { return a.indexOf(v) === i; });
        labelSpan.textContent = unique.join(', ') || '\u2014';

        var fileSpan = document.createElement('span');
        fileSpan.className = 'card-confidence';
        fileSpan.textContent = item.filename.length > 22
            ? item.filename.substring(0, 20) + '\u2026'
            : item.filename;
        fileSpan.title = item.filename;

        info.appendChild(labelSpan);
        info.appendChild(fileSpan);
        card.appendChild(info);

        // Click to open detail modal
        var self = this;
        card.style.cursor = 'pointer';
        card.addEventListener('click', function() {
            self.openModal(item);
        });

        return card;
    },

    // ── Detail Modal ──────────────────────────────────────────────────────

    openModal(item) {
        var modal = document.getElementById('image-modal');
        var canvas = document.getElementById('modal-canvas');
        var tableEl = document.getElementById('modal-bbox-table');

        document.getElementById('modal-filename').textContent = item.filename;
        document.getElementById('modal-dimensions').textContent =
            item.width + ' \u00d7 ' + item.height + 'px';

        modal.style.display = 'flex';

        // Draw image + bboxes on modal canvas
        var img = new Image();
        img.onload = function() {
            var wrap = canvas.parentElement;
            var maxW = Math.min(wrap.clientWidth || 800, 900);
            var maxH = Math.min(window.innerHeight * 0.55, 600);

            var scale = Math.min(maxW / img.width, maxH / img.height, 1);
            var drawW = Math.round(img.width * scale);
            var drawH = Math.round(img.height * scale);

            canvas.width = drawW;
            canvas.height = drawH;

            var ctx = canvas.getContext('2d');
            ctx.drawImage(img, 0, 0, drawW, drawH);

            // Draw all bboxes
            item.bboxes.forEach(function(bbox) {
                var bx = (bbox.x_center - bbox.w / 2) * drawW;
                var by = (bbox.y_center - bbox.h / 2) * drawH;
                var bw = bbox.w * drawW;
                var bh = bbox.h * drawH;

                ctx.strokeStyle = bbox.color;
                ctx.lineWidth = 3;
                ctx.strokeRect(bx, by, bw, bh);

                // Label with background
                var label = bbox.class_name;
                ctx.font = 'bold 13px system-ui';
                var tw = ctx.measureText(label).width;
                ctx.fillStyle = bbox.color;
                ctx.fillRect(bx, by - 20, tw + 10, 20);
                ctx.fillStyle = '#fff';
                ctx.fillText(label, bx + 5, by - 5);
            });
        };
        img.src = item.image_url;

        // Fetch and display metadata
        var self = this;
        tableEl.textContent = '';
        var loadingMeta = document.createElement('div');
        loadingMeta.className = 'meta-loading';
        loadingMeta.textContent = 'Loading metadata...';
        tableEl.appendChild(loadingMeta);

        fetch('/api/doc-training/metadata/' + this.filters.split + '/' + encodeURIComponent(item.filename))
            .then(function(r) { return r.json(); })
            .then(function(data) {
                tableEl.textContent = '';
                if (!data.success || !data.metadata) {
                    var noData = document.createElement('div');
                    noData.className = 'bbox-empty';
                    noData.textContent = 'No metadata available.';
                    tableEl.appendChild(noData);
                    return;
                }
                var meta = data.metadata;

                // Header chips row
                var headerRow = document.createElement('div');
                headerRow.className = 'meta-header-row';

                var chips = [];
                if (meta.source) chips.push(meta.source);
                if (meta.document_type) chips.push(meta.document_type);
                if (meta.issuer_name) chips.push(meta.issuer_name);
                if (meta.capture_mode) chips.push(meta.capture_mode);

                chips.forEach(function(text) {
                    var chip = document.createElement('span');
                    chip.className = 'meta-chip';
                    chip.textContent = text;
                    headerRow.appendChild(chip);
                });
                tableEl.appendChild(headerRow);

                // Field values table
                var fields = meta.fields || {};
                var fieldKeys = Object.keys(fields);
                if (fieldKeys.length > 0) {
                    var table = document.createElement('table');
                    table.className = 'bbox-detail-table';

                    var tbody = document.createElement('tbody');
                    fieldKeys.forEach(function(key) {
                        var row = document.createElement('tr');

                        var tdLabel = document.createElement('td');
                        tdLabel.className = 'meta-field-label';
                        tdLabel.textContent = key;
                        row.appendChild(tdLabel);

                        var tdValue = document.createElement('td');
                        tdValue.className = 'meta-field-value';
                        tdValue.textContent = fields[key];
                        row.appendChild(tdValue);

                        tbody.appendChild(row);
                    });
                    table.appendChild(tbody);
                    tableEl.appendChild(table);
                } else {
                    var noFields = document.createElement('div');
                    noFields.className = 'bbox-empty';
                    noFields.textContent = 'No field-level annotations available for this dataset.';
                    tableEl.appendChild(noFields);
                }

                // Bbox summary (compact, below metadata)
                if (item.bboxes && item.bboxes.length > 0) {
                    var bboxSummary = document.createElement('div');
                    bboxSummary.className = 'bbox-summary';
                    bboxSummary.textContent = item.bboxes.length + ' bounding box' +
                        (item.bboxes.length !== 1 ? 'es' : '') + ': ' +
                        item.bboxes.map(function(b) { return b.class_name; }).join(', ');
                    tableEl.appendChild(bboxSummary);
                }
            })
            .catch(function(err) {
                tableEl.textContent = '';
                var errEl = document.createElement('div');
                errEl.className = 'bbox-empty';
                errEl.textContent = 'Failed to load metadata.';
                tableEl.appendChild(errEl);
            });
    },

    closeModal() {
        document.getElementById('image-modal').style.display = 'none';
    },

    // ── Infinite Scroll + Lazy Images ────────────────────────────────────

    setupInfiniteScroll() {
        var self = this;

        // Lazy load observer for canvas elements
        this.imageObserver = new IntersectionObserver(function(entries) {
            entries.forEach(function(entry) {
                if (entry.isIntersecting) {
                    var canvas = entry.target;
                    var img = canvas._lazyImg;
                    if (img && img.dataset.src) {
                        img.src = img.dataset.src;
                        delete img.dataset.src;
                    }
                    self.imageObserver.unobserve(canvas);
                }
            });
        }, { rootMargin: '200px' });

        // Infinite scroll observer
        this.observer = new IntersectionObserver(function(entries) {
            if (entries[0].isIntersecting && !self.loading && self.page < self.totalPages) {
                self.page++;
                self.loadPage();
            }
        }, { rootMargin: '400px' });

        var sentinel = document.getElementById('scroll-sentinel');
        if (sentinel) this.observer.observe(sentinel);
    },

    // ── Stats ────────────────────────────────────────────────────────────

    updateStats(total) {
        var el = document.getElementById('filter-stats');
        if (el) el.textContent = total.toLocaleString() + ' images';
    },

    // ── URL State Sync ───────────────────────────────────────────────────

    syncFiltersToURL() {
        var params = new URLSearchParams();
        // Only include non-default values
        if (this.filters.split && this.filters.split !== 'train') {
            params.set('split', this.filters.split);
        }
        if (this.filters.class_id !== '') {
            params.set('class_id', this.filters.class_id);
        }
        if (this.filters.source) {
            params.set('source', this.filters.source);
        }
        if (this.filters.issuer) {
            params.set('issuer', this.filters.issuer);
        }
        var search = params.toString();
        var newUrl = window.location.pathname + (search ? '?' + search : '');
        history.replaceState(null, '', newUrl);
    },

    loadFiltersFromURL() {
        var params = new URLSearchParams(window.location.search);

        if (params.has('split')) {
            this.filters.split = params.get('split');
            // Update split toggle buttons
            var self = this;
            document.querySelectorAll('#split-toggle .toggle-btn').forEach(function(btn) {
                btn.classList.toggle('active', btn.dataset.split === self.filters.split);
            });
        }

        if (params.has('class_id')) {
            this.filters.class_id = params.get('class_id');
            var classEl = document.getElementById('class-filter');
            if (classEl) classEl.value = this.filters.class_id;
        }

        if (params.has('source')) {
            this.filters.source = params.get('source');
            // Source options are populated dynamically; value will be restored
            // by loadFilterOptions() which preserves currentSource
            var sourceEl = document.getElementById('source-filter');
            if (sourceEl) sourceEl.value = this.filters.source;
        }

        if (params.has('issuer')) {
            this.filters.issuer = params.get('issuer');
            var issuerEl = document.getElementById('issuer-filter');
            if (issuerEl) issuerEl.value = this.filters.issuer;
        }
    },

    // ── Event Binding ────────────────────────────────────────────────────

    bindEvents() {
        var self = this;

        // Split toggle
        document.querySelectorAll('#split-toggle .toggle-btn').forEach(function(btn) {
            btn.addEventListener('click', function() {
                document.querySelectorAll('#split-toggle .toggle-btn').forEach(function(b) {
                    b.classList.remove('active');
                });
                btn.classList.add('active');
                self.filters.split = btn.dataset.split;
                // Reset downstream filters
                self.filters.source = '';
                self.filters.issuer = '';
                document.getElementById('source-filter').value = '';
                document.getElementById('issuer-filter').value = '';
                self.syncFiltersToURL();
                self.loadFilterOptions();
                self.resetAndReload();
            });
        });

        // Class filter
        document.getElementById('class-filter').addEventListener('change', function(e) {
            self.filters.class_id = e.target.value;
            // Reset downstream filters
            self.filters.source = '';
            self.filters.issuer = '';
            document.getElementById('source-filter').value = '';
            document.getElementById('issuer-filter').value = '';
            self.syncFiltersToURL();
            self.loadFilterOptions();
            self.resetAndReload();
        });

        // Source filter
        document.getElementById('source-filter').addEventListener('change', function(e) {
            self.filters.source = e.target.value;
            // Reset issuer (downstream)
            self.filters.issuer = '';
            document.getElementById('issuer-filter').value = '';
            self.syncFiltersToURL();
            self.loadFilterOptions();
            self.resetAndReload();
        });

        // Issuer filter
        document.getElementById('issuer-filter').addEventListener('change', function(e) {
            self.filters.issuer = e.target.value;
            self.syncFiltersToURL();
            self.resetAndReload();
        });

        // Modal close
        document.getElementById('modal-close').addEventListener('click', function() {
            self.closeModal();
        });
        document.getElementById('image-modal').addEventListener('click', function(e) {
            if (e.target === document.getElementById('image-modal')) self.closeModal();
        });
        document.addEventListener('keydown', function(e) {
            if (e.key === 'Escape') self.closeModal();
        });
    },

    // ── Utilities ────────────────────────────────────────────────────────

    resetAndReload() {
        this.page = 1;
        this.items = [];

        var grid = document.getElementById('gallery-grid');
        grid.textContent = '';
        var loadingEl = document.createElement('div');
        loadingEl.className = 'loading-state';
        loadingEl.textContent = 'Loading training images...';
        grid.appendChild(loadingEl);

        this.loadPage();
    }
};

document.addEventListener('DOMContentLoaded', function() { DocTrainingBrowser.init(); });
