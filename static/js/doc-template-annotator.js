/* ═══════════════════════════════════════════════════════════════════════
   Document Template Annotator — vanilla JS module
   Canvas-based region annotation for synthetic document generation.
   ═══════════════════════════════════════════════════════════════════════ */
var TemplateAnnotator = {

    // ── State ────────────────────────────────────────────────────────────
    templates: [],
    currentTemplate: null,
    regions: [],
    selectedRegionIndex: -1,
    canvas: null,
    ctx: null,
    bgImage: null,
    zoom: 1.0,
    panX: 0,
    panY: 0,
    mode: 'select',       // 'select' | 'draw' | 'move' | 'resize'
    dragStart: null,
    drawStart: null,
    resizeHandle: null,
    isPanning: false,
    panStart: null,
    fonts: [],
    _dirty: false,
    _autosaveTimer: null,
    _toastTimer: null,
    _overlayImages: {},

    // ── Class name → class_id mapping (mirrors backend) ────────────────
    CLASS_IDS: {passport: 0, drivers_license: 1, twic_card: 2, merchant_mariner_credential: 3, id_card_generic: 4, uscg_medical_cert: 5},

    // ── Region type → color mapping ─────────────────────────────────────
    TYPE_COLORS: {
        photo:        '#3b82f6',
        text_name:    '#22c55e',
        text_date:    '#f97316',
        text_number:  '#a855f7',
        text_static:  '#6b7280',
        text_address: '#14b8a6',
        text_field:   '#eab308',
        mrz:          '#ef4444',
        barcode:      '#92400e',
        qrcode:       '#d946ef',
        chip:         '#d97706',
        static_image: '#06b6d4',
        line:         '#ec4899',
        box:          '#64748b'
    },

    TYPE_LABELS: {
        photo:        'Photo',
        text_name:    'Name',
        text_date:    'Date',
        text_number:  'Number',
        text_static:  'Static Text',
        text_address: 'Address',
        text_field:   'Field',
        mrz:          'MRZ',
        barcode:      'Barcode',
        qrcode:       'QR Code',
        chip:         'Chip',
        static_image: 'Static Image',
        line:         'Line',
        box:          'Box'
    },

    // ═════════════════════════════════════════════════════════════════════
    // Initialization
    // ═════════════════════════════════════════════════════════════════════

    init: function() {
        console.log('TemplateAnnotator init');
        this.bindGlobalEvents();
        this.loadTemplates();
        this.loadFonts();
        this.initSceneBackgrounds();

        // Restore template from URL hash (e.g., #edit/twic)
        var self = this;
        var hash = window.location.hash;
        if (hash && hash.indexOf('#edit/') === 0) {
            var tplId = hash.substring(6);
            // Wait for templates to load then open
            setTimeout(function() { self.openTemplate(tplId); }, 500);
        }
    },

    bindGlobalEvents: function() {
        var self = this;

        // New template button
        document.getElementById('btn-new-template').addEventListener('click', function() {
            self.showNewTemplateModal();
        });

        // Back to list
        document.getElementById('btn-back').addEventListener('click', function() {
            self.backToList();
        });

        // Toolbar actions
        document.getElementById('btn-save').addEventListener('click', function() {
            self.saveTemplate();
        });
        document.getElementById('btn-preview').addEventListener('click', function() {
            self.previewTemplate();
        });
        document.getElementById('btn-generate').addEventListener('click', function() {
            self.showGenerateModal();
        });

        // Create template
        document.getElementById('btn-create-template').addEventListener('click', function() {
            self.createTemplate();
        });

        // Generate start
        document.getElementById('btn-start-generate').addEventListener('click', function() {
            self.startGeneration();
        });

        // Scene percentage slider
        var scenePct = document.getElementById('gen-scene-pct');
        if (scenePct) {
            scenePct.addEventListener('input', function() {
                document.getElementById('gen-scene-pct-val').textContent = this.value + '%';
            });
        }

        // Max perspective slider
        document.getElementById('gen-max-perspective').addEventListener('input', function() {
            document.getElementById('gen-max-perspective-val').textContent = this.value + '%';
        });
        // Max rotation slider
        document.getElementById('gen-max-rotation').addEventListener('input', function() {
            document.getElementById('gen-max-rotation-val').textContent = this.value + '°';
        });

        // Photocopy percentage slider
        document.getElementById('gen-photocopy-pct').addEventListener('input', function() {
            document.getElementById('gen-photocopy-pct-val').textContent = this.value + '%';
        });
        // Washout min slider
        document.getElementById('gen-washout-min').addEventListener('input', function() {
            document.getElementById('gen-washout-min-val').textContent = this.value + '%';
            // Ensure min <= max
            var maxEl = document.getElementById('gen-washout-max');
            if (parseInt(this.value) > parseInt(maxEl.value)) {
                maxEl.value = this.value;
                document.getElementById('gen-washout-max-val').textContent = this.value + '%';
            }
        });
        // Washout max slider
        document.getElementById('gen-washout-max').addEventListener('input', function() {
            document.getElementById('gen-washout-max-val').textContent = this.value + '%';
            var minEl = document.getElementById('gen-washout-min');
            if (parseInt(this.value) < parseInt(minEl.value)) {
                minEl.value = this.value;
                document.getElementById('gen-washout-min-val').textContent = this.value + '%';
            }
        });
        // Oversaturated percentage slider
        document.getElementById('gen-oversaturated-pct').addEventListener('input', function() {
            document.getElementById('gen-oversaturated-pct-val').textContent = this.value + '%';
        });
        // Oversaturation min slider
        document.getElementById('gen-oversat-min').addEventListener('input', function() {
            document.getElementById('gen-oversat-min-val').textContent = this.value + '%';
            var maxEl = document.getElementById('gen-oversat-max');
            if (parseInt(this.value) > parseInt(maxEl.value)) {
                maxEl.value = this.value;
                document.getElementById('gen-oversat-max-val').textContent = this.value + '%';
            }
        });
        // Oversaturation max slider
        document.getElementById('gen-oversat-max').addEventListener('input', function() {
            document.getElementById('gen-oversat-max-val').textContent = this.value + '%';
            var minEl = document.getElementById('gen-oversat-min');
            if (parseInt(this.value) < parseInt(minEl.value)) {
                minEl.value = this.value;
                document.getElementById('gen-oversat-min-val').textContent = this.value + '%';
            }
        });

        // Modal dismiss buttons
        document.querySelectorAll('[data-dismiss="modal"]').forEach(function(btn) {
            btn.addEventListener('click', function() {
                var overlay = btn.closest('.modal-overlay');
                if (overlay) overlay.style.display = 'none';
            });
        });

        // Close modals on backdrop click — only for display-only modals (no action footer)
        document.querySelectorAll('.modal-overlay').forEach(function(overlay) {
            overlay.addEventListener('click', function(e) {
                if (e.target === overlay && !overlay.querySelector('.modal-footer')) {
                    overlay.style.display = 'none';
                }
            });
        });

        // Escape key — only close display-only modals (no action footer)
        document.addEventListener('keydown', function(e) {
            if (e.key === 'Escape') {
                document.querySelectorAll('.modal-overlay').forEach(function(m) {
                    if (m.style.display !== 'none' && !m.querySelector('.modal-footer')) {
                        m.style.display = 'none';
                    }
                });
            }
        });

        // Overlay add button
        document.getElementById('btn-add-overlay').addEventListener('click', function() {
            document.getElementById('overlay-file-input').click();
        });
        document.getElementById('overlay-file-input').addEventListener('change', function() {
            if (this.files && this.files[0]) {
                self.uploadOverlay(this.files[0]);
                this.value = '';
            }
        });

        // Zoom controls
        document.getElementById('btn-zoom-in').addEventListener('click', function() {
            self.setZoom(self.zoom * 1.25);
        });
        document.getElementById('btn-zoom-out').addEventListener('click', function() {
            self.setZoom(self.zoom / 1.25);
        });
        document.getElementById('btn-zoom-fit').addEventListener('click', function() {
            self.fitToView();
        });

        // Keyboard shortcuts in edit view
        document.addEventListener('keydown', function(e) {
            if (document.getElementById('edit-view').style.display === 'none') return;
            // Do not capture if focus is in an input
            var tag = (e.target.tagName || '').toLowerCase();
            if (tag === 'input' || tag === 'select' || tag === 'textarea') return;

            if (e.key === 'Delete' || e.key === 'Backspace') {
                self.deleteSelectedRegion();
                e.preventDefault();
            }
            if (e.key === 'f' || e.key === 'F') {
                self.addFoldLine();
                e.preventDefault();
            }
        });
        console.log('bindGlobalEvents complete');
    },

    // ═════════════════════════════════════════════════════════════════════
    // LIST VIEW — Load, render, create templates
    // ═════════════════════════════════════════════════════════════════════

    loadTemplates: async function() {
        try {
            var resp = await fetch('/api/doc-templates/');
            var data = await resp.json();
            if (data.success) {
                this.templates = data.templates || [];
            } else {
                this.templates = [];
            }
        } catch (e) {
            console.error('Failed to load templates:', e);
            this.templates = [];
        }
        this.renderTemplateGrid();
    },

    renderTemplateGrid: function() {
        var grid = document.getElementById('template-grid');
        grid.textContent = '';

        if (this.templates.length === 0) {
            var empty = document.createElement('div');
            empty.className = 'empty-state';

            var iconWrap = document.createElement('div');
            iconWrap.className = 'empty-icon';
            var iconSvg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
            iconSvg.setAttribute('width', '28');
            iconSvg.setAttribute('height', '28');
            iconSvg.setAttribute('viewBox', '0 0 24 24');
            iconSvg.setAttribute('fill', 'none');
            iconSvg.setAttribute('stroke', 'currentColor');
            iconSvg.setAttribute('stroke-width', '1.5');
            var rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
            rect.setAttribute('x', '3'); rect.setAttribute('y', '3');
            rect.setAttribute('width', '18'); rect.setAttribute('height', '18');
            rect.setAttribute('rx', '2');
            iconSvg.appendChild(rect);
            var l1 = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            l1.setAttribute('x1', '12'); l1.setAttribute('y1', '8');
            l1.setAttribute('x2', '12'); l1.setAttribute('y2', '16');
            iconSvg.appendChild(l1);
            var l2 = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            l2.setAttribute('x1', '8'); l2.setAttribute('y1', '12');
            l2.setAttribute('x2', '16'); l2.setAttribute('y2', '12');
            iconSvg.appendChild(l2);
            iconWrap.appendChild(iconSvg);
            empty.appendChild(iconWrap);

            var msg = document.createElement('span');
            msg.textContent = 'No templates yet. Create one to get started.';
            empty.appendChild(msg);

            grid.appendChild(empty);
            return;
        }

        var self = this;
        this.templates.forEach(function(tpl) {
            grid.appendChild(self.createTemplateCard(tpl));
        });
    },

    createTemplateCard: function(tpl) {
        var card = document.createElement('div');
        card.className = 'template-card';

        // Thumbnail
        var thumb = document.createElement('div');
        thumb.className = 'template-card-thumb';
        if (tpl.thumbnail) {
            var img = document.createElement('img');
            img.src = tpl.thumbnail;
            img.alt = tpl.name;
            img.loading = 'lazy';
            thumb.appendChild(img);
        } else {
            var ph = document.createElement('span');
            ph.className = 'thumb-placeholder';
            ph.textContent = 'No preview';
            thumb.appendChild(ph);
        }
        card.appendChild(thumb);

        // Info
        var info = document.createElement('div');
        info.className = 'template-card-info';

        var name = document.createElement('div');
        name.className = 'template-card-name';
        name.textContent = tpl.name;
        name.title = tpl.name;
        info.appendChild(name);

        var meta = document.createElement('div');
        meta.className = 'template-card-meta';

        var cls = document.createElement('span');
        cls.className = 'template-card-class';
        cls.textContent = (tpl.class_name || '').replace(/_/g, ' ');
        meta.appendChild(cls);

        var rc = document.createElement('span');
        rc.className = 'template-card-regions';
        var count = (tpl.regions || []).length;
        rc.textContent = count + ' region' + (count !== 1 ? 's' : '');
        meta.appendChild(rc);

        info.appendChild(meta);
        card.appendChild(info);

        // Click to open
        var self = this;
        card.addEventListener('click', function() {
            self.openTemplate(tpl.id);
        });

        return card;
    },

    showNewTemplateModal: function() {
        console.log('showNewTemplateModal called');
        document.getElementById('new-tpl-name').value = '';
        document.getElementById('new-tpl-class').value = 'passport';
        document.getElementById('new-tpl-bg').value = '';
        // Clear any previous inline error
        var errDiv = document.getElementById('create-template-error');
        if (errDiv) errDiv.textContent = '';
        document.getElementById('modal-new-template').style.display = 'flex';
        setTimeout(function() {
            document.getElementById('new-tpl-name').focus();
        }, 100);
    },

    createTemplate: async function() {
        console.log('createTemplate called');

        // Helper: show/clear inline error
        var showInlineError = function(msg) {
            var errDiv = document.getElementById('create-template-error');
            if (!errDiv) {
                // Create it if missing (before form-actions)
                errDiv = document.createElement('div');
                errDiv.id = 'create-template-error';
                errDiv.className = 'form-error';
                var formActions = document.querySelector('#modal-new-template .form-actions');
                if (formActions) {
                    formActions.parentNode.insertBefore(errDiv, formActions);
                }
            }
            if (errDiv) errDiv.textContent = msg;
        };
        var clearInlineError = function() {
            var errDiv = document.getElementById('create-template-error');
            if (errDiv) errDiv.textContent = '';
        };

        clearInlineError();

        var name = document.getElementById('new-tpl-name').value.trim();
        var docClass = document.getElementById('new-tpl-class').value;
        var fileInput = document.getElementById('new-tpl-bg');
        console.log('createTemplate: name=', name, 'docClass=', docClass, 'files=', fileInput.files ? fileInput.files.length : 'none');

        if (!name) {
            console.log('createTemplate: validation failed — no name');
            this.showToast('Please enter a template name.', 'error');
            showInlineError('Please enter a template name.');
            return;
        }
        if (!fileInput.files || fileInput.files.length === 0) {
            console.log('createTemplate: validation failed — no background image');
            this.showToast('Please select a background image.', 'error');
            showInlineError('Please select a background image.');
            return;
        }

        var btn = document.getElementById('btn-create-template');
        var origText = btn ? btn.textContent : null;
        if (btn) {
            btn.disabled = true;
            btn.textContent = 'Creating...';
        }

        var formData = new FormData();
        formData.append('name', name);
        formData.append('class_id', this.CLASS_IDS[docClass] !== undefined ? this.CLASS_IDS[docClass] : 0);
        formData.append('background', fileInput.files[0]);
        console.log('createTemplate: sending POST /api/doc-templates/');

        try {
            var resp = await fetch('/api/doc-templates/', {
                method: 'POST',
                body: formData
            });
            console.log('createTemplate: response status=', resp.status);
            if (!resp.ok) {
                var errMsg = 'Server error (' + resp.status + ')';
                try {
                    var errData = await resp.json();
                    if (errData.error) errMsg = errData.error;
                    else if (errData.detail) errMsg = errData.detail;
                } catch (_) {}
                console.log('createTemplate: request failed —', errMsg);
                this.showToast(errMsg, 'error');
                showInlineError(errMsg);
                if (btn) { btn.disabled = false; btn.textContent = origText; }
                return;
            }
            var data = await resp.json();
            if (data.success) {
                document.getElementById('modal-new-template').style.display = 'none';
                this.showToast('Template created.', 'success');
                if (btn) { btn.disabled = false; btn.textContent = origText; }
                await this.loadTemplates();
                if (data.template && data.template.id) {
                    this.openTemplate(data.template.id);
                }
            } else {
                var errMsg2 = data.error || 'Failed to create template.';
                console.log('createTemplate: API returned failure —', errMsg2);
                this.showToast(errMsg2, 'error');
                showInlineError(errMsg2);
                if (btn) { btn.disabled = false; btn.textContent = origText; }
            }
        } catch (e) {
            console.error('Create template error:', e);
            this.showToast('Network error creating template.', 'error');
            showInlineError('Network error creating template.');
            if (btn) { btn.disabled = false; btn.textContent = origText; }
        }
    },

    // ═════════════════════════════════════════════════════════════════════
    // EDIT VIEW — Open template, switch views
    // ═════════════════════════════════════════════════════════════════════

    openTemplate: async function(id) {
        try {
            var resp = await fetch('/api/doc-templates/' + id);
            var data = await resp.json();
            if (!data.success) {
                this.showToast(data.error || 'Failed to load template.', 'error');
                return;
            }
            this.currentTemplate = data.template;
            this.regions = (data.template.regions || []).map(function(r, i) {
                r._index = i;
                return r;
            });
            this.selectedRegionIndex = -1;
            this._dirty = false;
            this._overlayImages = {};
            if (!this.currentTemplate.overlays) {
                this.currentTemplate.overlays = [];
            }
        } catch (e) {
            console.error('Open template error:', e);
            this.showToast('Network error loading template.', 'error');
            return;
        }

        // Populate toolbar
        document.getElementById('template-name-input').value = this.currentTemplate.name || '';
        document.getElementById('template-class-select').value = this.currentTemplate.class_name || 'passport';

        // Switch views
        document.getElementById('list-view').style.display = 'none';
        document.getElementById('edit-view').style.display = 'block';

        // Update URL so refresh stays on this template
        history.replaceState(null, '', '#edit/' + id);

        // Load background image then setup canvas
        this.loadBackgroundImage();
    },

    backToList: function() {
        if (this._dirty) {
            if (!confirm('You have unsaved changes. Leave without saving?')) return;
        }
        document.getElementById('edit-view').style.display = 'none';
        document.getElementById('list-view').style.display = 'block';
        history.replaceState(null, '', window.location.pathname);
        this.currentTemplate = null;
        this.regions = [];
        this.selectedRegionIndex = -1;
        this.bgImage = null;
        this._dirty = false;
        if (this._autosaveTimer) { clearTimeout(this._autosaveTimer); this._autosaveTimer = null; }
        this._overlayImages = {};
        this.loadTemplates();
    },

    loadBackgroundImage: function() {
        var self = this;
        if (!this.currentTemplate) {
            this.setupCanvas();
            return;
        }
        var bgUrl = '/api/doc-templates/' + this.currentTemplate.id + '/background';
        var img = new Image();
        img.onload = function() {
            self.bgImage = img;
            self.setupCanvas();
            self.fitToView();
        };
        img.onerror = function() {
            console.error('Failed to load background image');
            self.bgImage = null;
            self.setupCanvas();
        };
        img.src = bgUrl;
    },

    // ═════════════════════════════════════════════════════════════════════
    // CANVAS — Setup, rendering, coordinate transforms
    // ═════════════════════════════════════════════════════════════════════

    setupCanvas: function() {
        this.canvas = document.getElementById('annotator-canvas');
        this.ctx = this.canvas.getContext('2d');

        var viewport = document.getElementById('canvas-viewport');
        this.canvas.width = viewport.clientWidth;
        this.canvas.height = viewport.clientHeight;

        var self = this;

        // Use named handler references for clean binding
        this._onMouseDown = function(e) { self.onMouseDown(e); };
        this._onMouseMove = function(e) { self.onMouseMove(e); };
        this._onMouseUp   = function(e) { self.onMouseUp(e); };
        this._onWheel     = function(e) { self.onWheel(e); };
        this._onResize    = function()  { self.onResize(); };

        // Remove old if any
        this.canvas.removeEventListener('mousedown', this.__md);
        this.canvas.removeEventListener('mousemove', this.__mm);
        this.canvas.removeEventListener('mouseup',   this.__mu);
        this.canvas.removeEventListener('wheel',     this.__wh);
        window.removeEventListener('resize', this.__rs);

        this.canvas.addEventListener('mousedown', this._onMouseDown);
        this.canvas.addEventListener('mousemove', this._onMouseMove);
        this.canvas.addEventListener('mouseup',   this._onMouseUp);
        this.canvas.addEventListener('wheel',     this._onWheel);
        window.addEventListener('resize', this._onResize);

        // Store references for removal
        this.__md = this._onMouseDown;
        this.__mm = this._onMouseMove;
        this.__mu = this._onMouseUp;
        this.__wh = this._onWheel;
        this.__rs = this._onResize;

        // Prevent context menu on canvas
        this.canvas.addEventListener('contextmenu', function(e) { e.preventDefault(); });

        this.renderRegionList();
        this.renderRegionProperties();
        this.renderOverlayList();
        this.render();
    },

    onResize: function() {
        if (!this.canvas) return;
        var viewport = document.getElementById('canvas-viewport');
        this.canvas.width = viewport.clientWidth;
        this.canvas.height = viewport.clientHeight;
        this.render();
    },

    setZoom: function(z) {
        var oldZoom = this.zoom;
        this.zoom = Math.max(0.1, Math.min(z, 10));
        // Keep center stable
        var cx = this.canvas.width / 2;
        var cy = this.canvas.height / 2;
        this.panX = cx - (cx - this.panX) * (this.zoom / oldZoom);
        this.panY = cy - (cy - this.panY) * (this.zoom / oldZoom);
        document.getElementById('zoom-label').textContent = Math.round(this.zoom * 100) + '%';
        this.render();
    },

    fitToView: function() {
        if (!this.bgImage || !this.canvas) return;
        var vw = this.canvas.width;
        var vh = this.canvas.height;
        var iw = this.bgImage.width;
        var ih = this.bgImage.height;
        var padding = 40;
        this.zoom = Math.min((vw - padding * 2) / iw, (vh - padding * 2) / ih, 2);
        this.panX = (vw - iw * this.zoom) / 2;
        this.panY = (vh - ih * this.zoom) / 2;
        document.getElementById('zoom-label').textContent = Math.round(this.zoom * 100) + '%';
        this.render();
    },

    // ── Coordinate transforms ────────────────────────────────────────────

    canvasToImage: function(cx, cy) {
        return {
            x: (cx - this.panX) / this.zoom,
            y: (cy - this.panY) / this.zoom
        };
    },

    imageToCanvas: function(ix, iy) {
        return {
            x: ix * this.zoom + this.panX,
            y: iy * this.zoom + this.panY
        };
    },

    imageToNorm: function(ix, iy) {
        if (!this.bgImage) return { x: 0, y: 0 };
        return {
            x: ix / this.bgImage.width,
            y: iy / this.bgImage.height
        };
    },

    normToImage: function(nx, ny) {
        if (!this.bgImage) return { x: 0, y: 0 };
        return {
            x: nx * this.bgImage.width,
            y: ny * this.bgImage.height
        };
    },

    // ── Main render loop ─────────────────────────────────────────────────

    render: function() {
        if (!this.ctx || !this.canvas) return;
        if (this._dirty && this.currentTemplate) {
            this._scheduleAutosave();
        }
        var ctx = this.ctx;
        var w = this.canvas.width;
        var h = this.canvas.height;

        // Clear
        ctx.clearRect(0, 0, w, h);

        // Canvas background
        ctx.fillStyle = '#0a0a1a';
        ctx.fillRect(0, 0, w, h);

        if (this.bgImage) {
            ctx.save();
            ctx.translate(this.panX, this.panY);
            ctx.scale(this.zoom, this.zoom);

            // Subtle shadow under document
            ctx.shadowColor = 'rgba(0,0,0,0.5)';
            ctx.shadowBlur = 20 / this.zoom;
            ctx.shadowOffsetX = 4 / this.zoom;
            ctx.shadowOffsetY = 4 / this.zoom;
            ctx.fillStyle = '#111';
            ctx.fillRect(0, 0, this.bgImage.width, this.bgImage.height);
            ctx.shadowColor = 'transparent';

            // Draw image
            ctx.drawImage(this.bgImage, 0, 0);
            ctx.restore();
        }

        // Draw regions: boxes first (underneath), then everything else
        var self = this;
        this.regions.forEach(function(region, index) {
            if (region.type === 'box') self.drawRegion(region, index);
        });
        this.regions.forEach(function(region, index) {
            if (region.type !== 'box') self.drawRegion(region, index);
        });

        // Draw active drawing rect
        if (this.mode === 'draw' && this.drawStart && this.drawCurrent) {
            var ds = this.drawStart;
            var dc = this.drawCurrent;
            var rx = Math.min(ds.x, dc.x);
            var ry = Math.min(ds.y, dc.y);
            var rw = Math.abs(dc.x - ds.x);
            var rh = Math.abs(dc.y - ds.y);
            var tl = this.imageToCanvas(rx, ry);
            ctx.save();
            ctx.strokeStyle = '#fff';
            ctx.lineWidth = 1.5;
            ctx.setLineDash([6, 4]);
            ctx.strokeRect(tl.x, tl.y, rw * this.zoom, rh * this.zoom);
            // Also show line preview for thin drags
            if (rw < 8 || rh < 8) {
                var tlStart = this.imageToCanvas(ds.x, ds.y);
                var tlEnd = this.imageToCanvas(dc.x, dc.y);
                ctx.strokeStyle = '#ec4899';
                ctx.lineWidth = 2;
                ctx.beginPath();
                ctx.moveTo(tlStart.x, tlStart.y);
                ctx.lineTo(tlEnd.x, tlEnd.y);
                ctx.stroke();
            }
            ctx.setLineDash([]);
            ctx.restore();
        }

        // Draw document-level overlays on top of everything
        if (this.bgImage && this.currentTemplate && this.currentTemplate.overlays) {
            var overlays = this.currentTemplate.overlays;
            for (var oi = 0; oi < overlays.length; oi++) {
                var ov = overlays[oi];
                var ovImg = this._overlayImages[ov.filename];
                if (!ovImg) {
                    this._loadOverlayImage(ov.filename);
                    continue;
                }
                if (!ovImg.complete || !ovImg.naturalWidth) continue;

                ctx.save();
                ctx.translate(this.panX, this.panY);
                ctx.scale(this.zoom, this.zoom);

                ctx.globalAlpha = (ov.opacity !== undefined) ? ov.opacity : 0.3;

                var blendMap = {
                    'screen': 'screen',
                    'overlay': 'overlay',
                    'multiply': 'multiply',
                    'soft_light': 'soft-light',
                    'normal': 'source-over'
                };
                ctx.globalCompositeOperation = blendMap[ov.blend_mode] || 'screen';

                ctx.drawImage(ovImg, 0, 0, this.bgImage.width, this.bgImage.height);
                ctx.restore();
            }
        }
    },

    drawRegion: function(region, index) {
        if (!this.bgImage) return;
        var ctx = this.ctx;
        var color = this.TYPE_COLORS[region.type] || '#888';
        var isSelected = (index === this.selectedRegionIndex);

        // Handle line regions differently
        if (region.type === 'line') {
            this.drawLineRegion(region, index, isSelected);
            return;
        }

        // Convert normalized coords to image coords, then to canvas
        var imgPos = this.normToImage(region.x, region.y);
        var imgSize = {
            x: region.w * this.bgImage.width,
            y: region.h * this.bgImage.height
        };
        var tl = this.imageToCanvas(imgPos.x, imgPos.y);
        var w = imgSize.x * this.zoom;
        var h = imgSize.y * this.zoom;

        // Fill
        ctx.save();

        if (region.type === 'box') {
            // Box region: use configured fill and border
            if (region.fill_color && region.fill_enabled !== false) {
                ctx.fillStyle = region.fill_color;
                ctx.globalAlpha = region.fill_opacity != null ? region.fill_opacity : 1;
                ctx.fillRect(tl.x, tl.y, w, h);
                ctx.globalAlpha = 1;
            }
            if (isSelected) {
                // Show selection highlight
                ctx.fillStyle = color + '20';
                ctx.fillRect(tl.x, tl.y, w, h);
            }
            // Border (skip if width is 0)
            var bw = region.border_width != null ? region.border_width : 1;
            if (bw > 0 || isSelected) {
                ctx.strokeStyle = region.border_color || '#000000';
                ctx.lineWidth = bw > 0 ? bw * this.zoom : 1;
                if (isSelected) {
                    ctx.setLineDash([4, 3]);
                    ctx.strokeStyle = color;
                    ctx.lineWidth = 2;
                }
                ctx.strokeRect(tl.x, tl.y, w, h);
                ctx.setLineDash([]);
            }
        } else {
            ctx.fillStyle = color + (isSelected ? '30' : '18');
            ctx.fillRect(tl.x, tl.y, w, h);

            // Border
            ctx.strokeStyle = color;
            ctx.lineWidth = isSelected ? 2.5 : 1.5;
            if (!isSelected) {
                ctx.setLineDash([5, 3]);
            }
            ctx.strokeRect(tl.x, tl.y, w, h);
            ctx.setLineDash([]);
        }

        // Label — for static text, use the content as the label
        var label = (region.type === 'text_static' && region.text_value) ? region.text_value : (region.label || this.TYPE_LABELS[region.type] || region.type);
        ctx.font = (isSelected ? 'bold ' : '') + '11px system-ui';
        var textW = ctx.measureText(label).width;
        var labelH = 16;
        var labelY = tl.y - labelH - 2;
        if (labelY < 0) labelY = tl.y + 2; // flip below if at top edge

        ctx.fillStyle = color;
        ctx.globalAlpha = isSelected ? 0.95 : 0.8;
        ctx.fillRect(tl.x, labelY, textW + 10, labelH);
        ctx.globalAlpha = 1;

        ctx.fillStyle = '#fff';
        ctx.fillText(label, tl.x + 5, labelY + 12);

        // ── Sample/placeholder text inside the region ──
        var sampleText = '';
        var rtype = region.type;
        if (rtype === 'text_static') {
            sampleText = region.text_value || 'Static text';
        } else if (rtype === 'text_name') {
            var nf = region.format || 'full';
            var nameExamples = {
                'full': 'JOHN DOE',
                'first_last': 'JOHN DOE',
                'last_comma_first': 'DOE, JOHN',
                'last_comma_first_middle': 'DOE, JOHN MICHAEL',
                'last_comma_first_mi': 'DOE, JOHN A.',
                'last_comma': 'DOE,',
                'first_mi': 'JOHN A.'
            };
            sampleText = nameExamples[nf] || 'JOHN DOE';
        } else if (rtype === 'text_date') {
            var df = region.format || '';
            var dateExamples = {
                'DD MMM YYYY': '15 JAN 2030',
                'DD-MMM-YYYY': '15-JAN-2030',
                'MM/DD/YYYY': '01/15/2030',
                'YYYY-MM-DD': '2030-01-15',
                'DD/MM/YYYY': '15/01/2030',
                'MMM DD, YYYY': 'JAN 15, 2030',
                'MMMDD': 'JAN15',
                'YYYY': '2030'
            };
            sampleText = dateExamples[df] || '01/15/2030';
        } else if (rtype === 'text_number') {
            sampleText = '12345678' + (region.suffix || '');
        } else if (rtype === 'text_address') {
            sampleText = '123 MAIN ST';
        } else if (rtype === 'text_field') {
            sampleText = 'FIELD';
        } else if (rtype === 'photo') {
            sampleText = 'PHOTO';
        } else if (rtype && rtype !== 'box') {
            sampleText = (this.TYPE_LABELS[rtype] || rtype).toUpperCase();
        }

        // Apply uppercase transform only when ALL CAPS is checked
        if (sampleText && region.uppercase !== false) {
            sampleText = sampleText.toUpperCase();
        }

        if (sampleText && w > 10 && h > 10) {
            // Apply rotation only to text content, not the bbox/handles
            if (region.rotation) {
                var rcx = tl.x + w / 2;
                var rcy = tl.y + h / 2;
                ctx.save();
                ctx.translate(rcx, rcy);
                ctx.rotate(region.rotation * Math.PI / 180);
                ctx.translate(-rcx, -rcy);
            }
            // For ±90/270° rotation, swap layout dimensions so alignment axes match visual axes
            var absRot = region.rotation ? Math.abs(region.rotation % 360) : 0;
            var swapDims = (absRot === 90 || absRot === 270);
            var lw = swapDims ? h : w;
            var lh = swapDims ? w : h;
            var lx = tl.x + w / 2 - lw / 2;
            var ly = tl.y + h / 2 - lh / 2;

            if (rtype === 'photo') {
                // Photo regions: simple centered label, no text formatting properties
                var photoFontSize = Math.max(10, Math.round(Math.min(lh * 0.3, lw * 0.25, 36)));
                ctx.font = photoFontSize + 'px system-ui';
                ctx.textAlign = 'center';
                ctx.textBaseline = 'middle';
                ctx.fillStyle = color;
                ctx.globalAlpha = isSelected ? 0.7 : 0.5;
                ctx.fillText(sampleText, lx + lw / 2, ly + lh / 2, lw - 4);
                ctx.globalAlpha = 1;
                ctx.textAlign = 'start';
                ctx.textBaseline = 'alphabetic';
            } else {
                // Use actual region text formatting properties
                var fontFamily = (region.font_family && region.font_family.trim()) ? region.font_family : 'system-ui';
                this.ensureFontLoaded(fontFamily);
                var boldPrefix = region.bold ? 'bold ' : '';
                var fontSize = Math.max(6, Math.round((region.font_size || 12) * this.zoom));
                // Add fallback fonts for symbols (e.g. ⚓) not in primary font
                var fontStack = "'" + fontFamily + "', 'DejaVu Sans', sans-serif";

                // Let text overflow — placeholder text doesn't reflect actual content
                ctx.font = boldPrefix + fontSize + 'px ' + fontStack;

                // Alignment — no padding, text fills entire region box
                var textX;
                var alignment = region.alignment || 'left';
                if (alignment === 'center') {
                    ctx.textAlign = 'center';
                    textX = lx + lw / 2;
                } else if (alignment === 'right') {
                    ctx.textAlign = 'right';
                    textX = lx + lw;
                } else {
                    ctx.textAlign = 'left';
                    textX = lx;
                }

                // Vertical alignment
                var valign = region.valign || 'middle';
                var textY;
                if (valign === 'top') {
                    ctx.textBaseline = 'top';
                    textY = ly;
                } else if (valign === 'bottom') {
                    ctx.textBaseline = 'bottom';
                    textY = ly + lh;
                } else {
                    ctx.textBaseline = 'middle';
                    textY = ly + lh / 2;
                }

                // Text outline
                var outlineSize = parseFloat(region.outline_size) || 0;
                var outlineColor = region.outline_color || '#000000';
                if (outlineSize > 0) {
                    ctx.globalAlpha = region.opacity != null ? region.opacity : 1;
                    ctx.strokeStyle = outlineColor;
                    ctx.lineWidth = outlineSize * this.zoom;
                    ctx.lineJoin = 'round';
                    if (region.letter_spacing > 0) {
                        var spacing = region.letter_spacing * this.zoom;
                        var chars = sampleText.split('');
                        // Measure total width for alignment offset
                        var totalW = 0;
                        for (var si = 0; si < chars.length; si++) {
                            totalW += ctx.measureText(chars[si]).width + (si < chars.length - 1 ? spacing : 0);
                        }
                        var charX;
                        if (alignment === 'center') {
                            charX = textX - totalW / 2;
                        } else if (alignment === 'right') {
                            charX = textX - totalW;
                        } else {
                            charX = textX;
                        }
                        ctx.textAlign = 'left';
                        for (var si = 0; si < chars.length; si++) {
                            ctx.strokeText(chars[si], charX, textY);
                            charX += ctx.measureText(chars[si]).width + spacing;
                        }
                        ctx.textAlign = alignment === 'center' ? 'center' : (alignment === 'right' ? 'right' : 'left');
                    } else {
                        ctx.strokeText(sampleText, textX, textY);
                    }
                }

                // Fill text
                ctx.fillStyle = region.color || color;
                ctx.globalAlpha = region.opacity != null ? region.opacity : 1;

                if (region.letter_spacing > 0) {
                    var spacing = region.letter_spacing * this.zoom;
                    var chars = sampleText.split('');
                    var totalW = 0;
                    for (var fi = 0; fi < chars.length; fi++) {
                        totalW += ctx.measureText(chars[fi]).width + (fi < chars.length - 1 ? spacing : 0);
                    }
                    var charX;
                    if (alignment === 'center') {
                        charX = textX - totalW / 2;
                    } else if (alignment === 'right') {
                        charX = textX - totalW;
                    } else {
                        charX = textX;
                    }
                    ctx.textAlign = 'left';
                    for (var fi = 0; fi < chars.length; fi++) {
                        ctx.fillText(chars[fi], charX, textY);
                        charX += ctx.measureText(chars[fi]).width + spacing;
                    }
                } else {
                    ctx.fillText(sampleText, textX, textY);
                }

                ctx.textAlign = 'start';
                ctx.textBaseline = 'alphabetic';
            }

            // Close rotation context for text
            if (region.rotation) {
                ctx.restore();
            }
        }

        ctx.restore();

        // Resize handles for selected region (outside rotation — handles stay axis-aligned)
        if (isSelected) {
            this.drawHandles(tl.x, tl.y, w, h);
        }
    },

    drawLineRegion: function(region, index, isSelected) {
        if (!this.bgImage) return;
        var ctx = this.ctx;
        var color = this.TYPE_COLORS.line;

        var p1 = this.imageToCanvas(
            (region.x1 || 0) * this.bgImage.width,
            (region.y1 || 0) * this.bgImage.height
        );
        var p2 = this.imageToCanvas(
            (region.x2 || 1) * this.bgImage.width,
            (region.y2 || 0.5) * this.bgImage.height
        );

        ctx.save();
        ctx.strokeStyle = color;
        ctx.lineWidth = (isSelected ? 3 : 2) * (region.line_width || 2) / 2;

        var style = region.line_style || 'solid';
        if (style === 'dotted') {
            ctx.setLineDash([3, 5]);
        } else if (style === 'dashed') {
            ctx.setLineDash([10, 5]);
        } else {
            ctx.setLineDash([]);
        }

        ctx.beginPath();
        ctx.moveTo(p1.x, p1.y);
        ctx.lineTo(p2.x, p2.y);
        ctx.stroke();
        ctx.setLineDash([]);

        // Draw endpoint handles
        var handleRadius = isSelected ? 6 : 4;
        [p1, p2].forEach(function(pt) {
            ctx.beginPath();
            ctx.arc(pt.x, pt.y, handleRadius, 0, Math.PI * 2);
            ctx.fillStyle = isSelected ? color : color + '80';
            ctx.fill();
            ctx.strokeStyle = '#fff';
            ctx.lineWidth = 1.5;
            ctx.stroke();
        });

        // Label
        var midX = (p1.x + p2.x) / 2;
        var midY = (p1.y + p2.y) / 2;
        var label = region.label || 'Line';
        if (region.fold_line) label = 'Fold: ' + label;
        ctx.font = (isSelected ? 'bold ' : '') + '11px system-ui';
        var textW = ctx.measureText(label).width;
        var labelH = 16;
        ctx.fillStyle = color;
        ctx.globalAlpha = isSelected ? 0.95 : 0.8;
        ctx.fillRect(midX - textW / 2 - 5, midY - labelH - 4, textW + 10, labelH);
        ctx.globalAlpha = 1;
        ctx.fillStyle = '#fff';
        ctx.fillText(label, midX - textW / 2, midY - labelH + 8);

        ctx.restore();
    },

    drawHandles: function(x, y, w, h) {
        var ctx = this.ctx;
        var size = 7;
        var half = size / 2;
        var handles = this.getHandlePositions(x, y, w, h);

        handles.forEach(function(hp) {
            ctx.fillStyle = '#fff';
            ctx.strokeStyle = '#333';
            ctx.lineWidth = 1;
            ctx.fillRect(hp.x - half, hp.y - half, size, size);
            ctx.strokeRect(hp.x - half, hp.y - half, size, size);
        });
    },

    getHandlePositions: function(x, y, w, h) {
        return [
            { x: x,         y: y,         cursor: 'nw-resize', id: 'tl' },
            { x: x + w / 2, y: y,         cursor: 'n-resize',  id: 'tm' },
            { x: x + w,     y: y,         cursor: 'ne-resize', id: 'tr' },
            { x: x + w,     y: y + h / 2, cursor: 'e-resize',  id: 'mr' },
            { x: x + w,     y: y + h,     cursor: 'se-resize', id: 'br' },
            { x: x + w / 2, y: y + h,     cursor: 's-resize',  id: 'bm' },
            { x: x,         y: y + h,     cursor: 'sw-resize', id: 'bl' },
            { x: x,         y: y + h / 2, cursor: 'w-resize',  id: 'ml' }
        ];
    },

    // ═════════════════════════════════════════════════════════════════════
    // MOUSE HANDLERS
    // ═════════════════════════════════════════════════════════════════════

    getCanvasXY: function(e) {
        var rect = this.canvas.getBoundingClientRect();
        return {
            x: e.clientX - rect.left,
            y: e.clientY - rect.top
        };
    },

    onMouseDown: function(e) {
        var pos = this.getCanvasXY(e);

        // Middle-click or Alt+click = pan
        if (e.button === 1 || (e.button === 0 && e.altKey)) {
            this.isPanning = true;
            this.panStart = { x: pos.x - this.panX, y: pos.y - this.panY };
            this.canvas.style.cursor = 'grabbing';
            e.preventDefault();
            return;
        }

        if (e.button !== 0) return;

        // Check resize handles first (only if a region is selected)
        if (this.selectedRegionIndex >= 0) {
            var handle = this.hitTestHandle(pos.x, pos.y);
            if (handle) {
                this.mode = 'resize';
                this.resizeHandle = handle;
                this.dragStart = this.canvasToImage(pos.x, pos.y);
                this._resizeOriginal = this.getRegionRect(this.regions[this.selectedRegionIndex]);
                this.canvas.style.cursor = handle.cursor;
                return;
            }
        }

        // Check if clicking on a region
        var hitIdx = this.hitTestRegion(pos.x, pos.y);
        if (hitIdx >= 0) {
            this.selectRegion(hitIdx);
            var hitRegion = this.regions[hitIdx];
            if (hitRegion.type === 'line') {
                // Check if clicking near an endpoint for endpoint drag
                var imgPtHit = this.canvasToImage(pos.x, pos.y);
                var ep1x = (hitRegion.x1 || 0) * this.bgImage.width;
                var ep1y = (hitRegion.y1 || 0) * this.bgImage.height;
                var ep2x = (hitRegion.x2 || 1) * this.bgImage.width;
                var ep2y = (hitRegion.y2 || 0.5) * this.bgImage.height;
                var d1 = Math.sqrt((imgPtHit.x - ep1x) * (imgPtHit.x - ep1x) + (imgPtHit.y - ep1y) * (imgPtHit.y - ep1y));
                var d2 = Math.sqrt((imgPtHit.x - ep2x) * (imgPtHit.x - ep2x) + (imgPtHit.y - ep2y) * (imgPtHit.y - ep2y));
                var threshold = 15 / this.zoom;
                if (d1 < threshold) {
                    this.mode = 'line_endpoint';
                    this._lineEndpoint = 1;
                    this.dragStart = imgPtHit;
                    this.canvas.style.cursor = 'move';
                    return;
                } else if (d2 < threshold) {
                    this.mode = 'line_endpoint';
                    this._lineEndpoint = 2;
                    this.dragStart = imgPtHit;
                    this.canvas.style.cursor = 'move';
                    return;
                }
                // Whole line move
                this.mode = 'move';
                this.dragStart = this.canvasToImage(pos.x, pos.y);
                this._moveOriginal = { x1: hitRegion.x1, y1: hitRegion.y1, x2: hitRegion.x2, y2: hitRegion.y2 };
                this.canvas.style.cursor = 'move';
                return;
            }
            this.mode = 'move';
            this.dragStart = this.canvasToImage(pos.x, pos.y);
            this._moveOriginal = { x: hitRegion.x, y: hitRegion.y };
            this.canvas.style.cursor = 'move';
            return;
        }

        // Click on empty area: deselect and start drawing
        this.selectRegion(-1);
        this.mode = 'draw';
        var imgPt = this.canvasToImage(pos.x, pos.y);
        this.drawStart = imgPt;
        this.drawCurrent = imgPt;
        this.canvas.style.cursor = 'crosshair';
    },

    onMouseMove: function(e) {
        var pos = this.getCanvasXY(e);

        // Panning
        if (this.isPanning && this.panStart) {
            this.panX = pos.x - this.panStart.x;
            this.panY = pos.y - this.panStart.y;
            this.render();
            return;
        }

        // Drawing
        if (this.mode === 'draw' && this.drawStart) {
            this.drawCurrent = this.canvasToImage(pos.x, pos.y);
            this.render();
            return;
        }

        // Moving
        if (this.mode === 'move' && this.dragStart && this.selectedRegionIndex >= 0) {
            var imgPt = this.canvasToImage(pos.x, pos.y);
            var dx = imgPt.x - this.dragStart.x;
            var dy = imgPt.y - this.dragStart.y;
            var region = this.regions[this.selectedRegionIndex];
            if (this.bgImage) {
                if (region.type === 'line' && this._moveOriginal.x1 !== undefined) {
                    var ddx = dx / this.bgImage.width;
                    var ddy = dy / this.bgImage.height;
                    region.x1 = Math.max(0, Math.min(1, this._moveOriginal.x1 + ddx));
                    region.y1 = Math.max(0, Math.min(1, this._moveOriginal.y1 + ddy));
                    region.x2 = Math.max(0, Math.min(1, this._moveOriginal.x2 + ddx));
                    region.y2 = Math.max(0, Math.min(1, this._moveOriginal.y2 + ddy));
                } else {
                    region.x = Math.max(0, Math.min(this._moveOriginal.x + dx / this.bgImage.width, 1 - region.w));
                    region.y = Math.max(0, Math.min(this._moveOriginal.y + dy / this.bgImage.height, 1 - region.h));
                }
            }
            this._dirty = true;
            this.render();
            this.updatePositionInputs(region);
            return;
        }

        // Line endpoint drag
        if (this.mode === 'line_endpoint' && this.dragStart && this.selectedRegionIndex >= 0) {
            var imgPt = this.canvasToImage(pos.x, pos.y);
            var region = this.regions[this.selectedRegionIndex];
            if (this.bgImage && region.type === 'line') {
                var nx = imgPt.x / this.bgImage.width;
                var ny = imgPt.y / this.bgImage.height;
                nx = Math.max(0, Math.min(1, nx));
                ny = Math.max(0, Math.min(1, ny));
                if (this._lineEndpoint === 1) {
                    region.x1 = nx;
                    region.y1 = ny;
                } else {
                    region.x2 = nx;
                    region.y2 = ny;
                }
                this._dirty = true;
                this.render();
            }
            return;
        }

        // Resizing
        if (this.mode === 'resize' && this.resizeHandle && this.selectedRegionIndex >= 0) {
            this.performResize(pos);
            return;
        }

        // Hover cursor
        this.updateHoverCursor(pos);
    },

    onMouseUp: function(e) {
        // Finish panning
        if (this.isPanning) {
            this.isPanning = false;
            this.panStart = null;
            this.canvas.style.cursor = 'crosshair';
            return;
        }

        // Finish drawing
        if (this.mode === 'draw' && this.drawStart && this.drawCurrent) {
            var ds = this.drawStart;
            var dc = this.drawCurrent;
            var rx = Math.min(ds.x, dc.x);
            var ry = Math.min(ds.y, dc.y);
            var rw = Math.abs(dc.x - ds.x);
            var rh = Math.abs(dc.y - ds.y);

            // Create region from drawn area
            if (this.bgImage) {
                // Check if this should be a line (very thin drag = line)
                var isLineDrag = (rw < 8 || rh < 8) && (rw > 3 || rh > 3);
                var dist = Math.sqrt(rw * rw + rh * rh);

                if (isLineDrag && dist > 10) {
                    // Create a line region
                    var norm1 = this.imageToNorm(ds.x, ds.y);
                    var norm2 = this.imageToNorm(dc.x, dc.y);
                    this.addLineRegion({
                        x1: Math.max(0, Math.min(norm1.x, 1)),
                        y1: Math.max(0, Math.min(norm1.y, 1)),
                        x2: Math.max(0, Math.min(norm2.x, 1)),
                        y2: Math.max(0, Math.min(norm2.y, 1))
                    });
                } else if (rw > 5 && rh > 5) {
                    var norm = this.imageToNorm(rx, ry);
                    var normW = rw / this.bgImage.width;
                    var normH = rh / this.bgImage.height;
                    this.addRegion({
                        x: Math.max(0, Math.min(norm.x, 1)),
                        y: Math.max(0, Math.min(norm.y, 1)),
                        w: Math.min(normW, 1 - norm.x),
                        h: Math.min(normH, 1 - norm.y)
                    });
                }
            }

            this.drawStart = null;
            this.drawCurrent = null;
        }

        // Finish move/resize/line_endpoint
        if (this.mode === 'move' || this.mode === 'resize' || this.mode === 'line_endpoint') {
            this.renderRegionProperties();
        }

        this.mode = 'select';
        this.dragStart = null;
        this.resizeHandle = null;
        this._resizeOriginal = null;
        this._moveOriginal = null;
        this._lineEndpoint = null;
        this.canvas.style.cursor = 'crosshair';
        this.render();
    },

    onWheel: function(e) {
        e.preventDefault();
        var pos = this.getCanvasXY(e);
        var oldZoom = this.zoom;
        var factor = e.deltaY < 0 ? 1.12 : 1 / 1.12;
        this.zoom = Math.max(0.1, Math.min(this.zoom * factor, 10));

        // Zoom centered on cursor
        this.panX = pos.x - (pos.x - this.panX) * (this.zoom / oldZoom);
        this.panY = pos.y - (pos.y - this.panY) * (this.zoom / oldZoom);

        document.getElementById('zoom-label').textContent = Math.round(this.zoom * 100) + '%';
        this.render();
    },

    // ── Resize logic ─────────────────────────────────────────────────────

    performResize: function(pos) {
        var imgPt = this.canvasToImage(pos.x, pos.y);
        var region = this.regions[this.selectedRegionIndex];
        var orig = this._resizeOriginal;
        if (!orig || !this.bgImage) return;

        var norm = this.imageToNorm(imgPt.x, imgPt.y);
        var hid = this.resizeHandle.id;

        var newX = orig.x, newY = orig.y, newW = orig.w, newH = orig.h;

        if (hid === 'tl' || hid === 'ml' || hid === 'bl') {
            newX = Math.min(norm.x, orig.x + orig.w - 0.01);
            newW = (orig.x + orig.w) - newX;
        }
        if (hid === 'tr' || hid === 'mr' || hid === 'br') {
            newW = Math.max(0.01, norm.x - orig.x);
        }
        if (hid === 'tl' || hid === 'tm' || hid === 'tr') {
            newY = Math.min(norm.y, orig.y + orig.h - 0.01);
            newH = (orig.y + orig.h) - newY;
        }
        if (hid === 'bl' || hid === 'bm' || hid === 'br') {
            newH = Math.max(0.01, norm.y - orig.y);
        }

        // Clamp to image bounds
        region.x = Math.max(0, Math.min(newX, 1));
        region.y = Math.max(0, Math.min(newY, 1));
        region.w = Math.min(newW, 1 - region.x);
        region.h = Math.min(newH, 1 - region.y);

        this._dirty = true;
        this.render();
        this.updatePositionInputs(region);
    },

    getRegionRect: function(r) {
        return { x: r.x, y: r.y, w: r.w, h: r.h };
    },

    // ── Hit testing ──────────────────────────────────────────────────────

    hitTestRegion: function(cx, cy) {
        if (!this.bgImage) return -1;
        for (var i = this.regions.length - 1; i >= 0; i--) {
            var r = this.regions[i];
            if (r.type === 'line') {
                // Point-to-line-segment distance check
                var p1 = this.imageToCanvas(
                    (r.x1 || 0) * this.bgImage.width,
                    (r.y1 || 0) * this.bgImage.height
                );
                var p2 = this.imageToCanvas(
                    (r.x2 || 1) * this.bgImage.width,
                    (r.y2 || 0.5) * this.bgImage.height
                );
                var dist = this._pointToSegmentDist(cx, cy, p1.x, p1.y, p2.x, p2.y);
                if (dist < 10) return i;
                continue;
            }
            var imgPos = this.normToImage(r.x, r.y);
            var tl = this.imageToCanvas(imgPos.x, imgPos.y);
            var w = r.w * this.bgImage.width * this.zoom;
            var h = r.h * this.bgImage.height * this.zoom;
            if (cx >= tl.x && cx <= tl.x + w && cy >= tl.y && cy <= tl.y + h) {
                return i;
            }
        }
        return -1;
    },

    _pointToSegmentDist: function(px, py, x1, y1, x2, y2) {
        var dx = x2 - x1, dy = y2 - y1;
        var lenSq = dx * dx + dy * dy;
        if (lenSq === 0) return Math.sqrt((px - x1) * (px - x1) + (py - y1) * (py - y1));
        var t = Math.max(0, Math.min(1, ((px - x1) * dx + (py - y1) * dy) / lenSq));
        var projX = x1 + t * dx, projY = y1 + t * dy;
        return Math.sqrt((px - projX) * (px - projX) + (py - projY) * (py - projY));
    },

    hitTestHandle: function(cx, cy) {
        if (this.selectedRegionIndex < 0 || !this.bgImage) return null;
        var r = this.regions[this.selectedRegionIndex];
        var imgPos = this.normToImage(r.x, r.y);
        var tl = this.imageToCanvas(imgPos.x, imgPos.y);
        var w = r.w * this.bgImage.width * this.zoom;
        var h = r.h * this.bgImage.height * this.zoom;
        var handles = this.getHandlePositions(tl.x, tl.y, w, h);
        var hitDist = 8;

        for (var i = 0; i < handles.length; i++) {
            var hp = handles[i];
            if (Math.abs(cx - hp.x) <= hitDist && Math.abs(cy - hp.y) <= hitDist) {
                return hp;
            }
        }
        return null;
    },

    updateHoverCursor: function(pos) {
        if (this.selectedRegionIndex >= 0) {
            var handle = this.hitTestHandle(pos.x, pos.y);
            if (handle) {
                this.canvas.style.cursor = handle.cursor;
                return;
            }
        }
        var hitIdx = this.hitTestRegion(pos.x, pos.y);
        if (hitIdx >= 0) {
            this.canvas.style.cursor = (hitIdx === this.selectedRegionIndex) ? 'move' : 'pointer';
        } else {
            this.canvas.style.cursor = 'crosshair';
        }
    },

    // ═════════════════════════════════════════════════════════════════════
    // REGION CRUD
    // ═════════════════════════════════════════════════════════════════════

    addRegion: function(rect) {
        var newRegion = {
            label: 'Region ' + (this.regions.length + 1),
            type: 'text_static',
            x: rect.x,
            y: rect.y,
            w: rect.w,
            h: rect.h,
            // Text defaults
            font_family: this.fonts.length > 0 ? this.fonts[0] : 'Helvetica',
            font_size: 14,
            color: '#000000',
            outline_color: '#000000',
            outline_size: 0,
            alignment: 'left',
            bold: false,
            letter_spacing: 0,
            rotation: 0,
            valign: 'middle',
            opacity: 1.0,
            // Type-specific defaults
            text_value: '',
            field_name: '',
            format: ''
        };
        this.regions.push(newRegion);
        this._dirty = true;
        this.selectRegion(this.regions.length - 1);
    },

    addLineRegion: function(coords) {
        var newRegion = {
            label: 'Line ' + (this.regions.length + 1),
            type: 'line',
            x1: coords.x1,
            y1: coords.y1,
            x2: coords.x2,
            y2: coords.y2,
            // Line-specific defaults
            line_width: 2,
            line_style: 'solid',
            line_color: '#888888',
            fold_line: false,
            fold_pct: 0.3
        };
        this.regions.push(newRegion);
        this._dirty = true;
        this.selectRegion(this.regions.length - 1);
    },

    addFoldLine: function() {
        this.addLineRegion({
            x1: 0.0, y1: 0.5, x2: 1.0, y2: 0.5
        });
        var region = this.regions[this.regions.length - 1];
        region.label = 'Fold Line';
        region.line_style = 'dotted';
        region.fold_line = true;
        region.fold_pct = 0.3;
        this.renderRegionList();
        this.renderRegionProperties();
        this.render();
    },

    selectRegion: function(index) {
        this.selectedRegionIndex = index;
        this.renderRegionList();
        this.renderRegionProperties();
        this.render();

        // Update hint
        var hint = document.getElementById('canvas-hint');
        if (index >= 0) {
            hint.textContent = 'Drag to move \u00b7 Handles to resize \u00b7 Del to delete';
        } else {
            hint.textContent = 'Click and drag to draw a region';
        }
    },

    deleteSelectedRegion: function() {
        if (this.selectedRegionIndex < 0) return;
        this.regions.splice(this.selectedRegionIndex, 1);
        this._dirty = true;
        this.selectRegion(-1);
    },

    // ═════════════════════════════════════════════════════════════════════
    // PROPERTIES PANEL — Region list + property editor
    // ═════════════════════════════════════════════════════════════════════

    renderRegionList: function() {
        var list = document.getElementById('region-list');
        var countEl = document.getElementById('region-count');
        list.textContent = '';
        countEl.textContent = this.regions.length;

        if (this.regions.length === 0) {
            var empty = document.createElement('div');
            empty.className = 'empty-regions';
            empty.textContent = 'No regions defined. Draw on the canvas to add one.';
            list.appendChild(empty);
            return;
        }

        var self = this;
        this.regions.forEach(function(region, index) {
            var item = document.createElement('div');
            item.className = 'region-item' + (index === self.selectedRegionIndex ? ' selected' : '');

            var dot = document.createElement('span');
            dot.className = 'region-color-dot';
            dot.style.backgroundColor = self.TYPE_COLORS[region.type] || '#888';
            item.appendChild(dot);

            var label = document.createElement('span');
            label.className = 'region-item-label';
            label.textContent = (region.type === 'text_static' && region.text_value) ? region.text_value : (region.label || 'Region ' + (index + 1));
            item.appendChild(label);

            var typeSpan = document.createElement('span');
            typeSpan.className = 'region-item-type';
            typeSpan.textContent = self.TYPE_LABELS[region.type] || region.type;
            item.appendChild(typeSpan);

            var dupBtn = document.createElement('button');
            dupBtn.className = 'btn-ghost region-item-dup';
            dupBtn.textContent = '\u2398';
            dupBtn.title = 'Duplicate region';
            dupBtn.addEventListener('click', function(e) {
                e.stopPropagation();
                var copy = JSON.parse(JSON.stringify(self.regions[index]));
                copy.label = (copy.label || 'Region') + ' copy';
                // Offset the duplicate slightly so it's visible
                if (copy.x != null) { copy.x = Math.min(copy.x + 0.02, 0.95); copy.y = Math.min(copy.y + 0.02, 0.95); }
                if (copy.x1 != null) { copy.x1 = Math.min(copy.x1 + 0.02, 0.98); copy.y1 = Math.min(copy.y1 + 0.02, 0.98); copy.x2 = Math.min(copy.x2 + 0.02, 0.98); copy.y2 = Math.min(copy.y2 + 0.02, 0.98); }
                self.regions.splice(index + 1, 0, copy);
                self._dirty = true;
                self.selectRegion(index + 1);
            });
            item.appendChild(dupBtn);

            var delBtn = document.createElement('button');
            delBtn.className = 'btn-danger-ghost region-item-delete';
            delBtn.textContent = '\u00d7';
            delBtn.title = 'Delete region';
            delBtn.addEventListener('click', function(e) {
                e.stopPropagation();
                self.regions.splice(index, 1);
                self._dirty = true;
                if (self.selectedRegionIndex === index) {
                    self.selectRegion(-1);
                } else if (self.selectedRegionIndex > index) {
                    self.selectedRegionIndex--;
                    self.renderRegionList();
                    self.render();
                } else {
                    self.renderRegionList();
                    self.render();
                }
            });
            item.appendChild(delBtn);

            item.addEventListener('click', function() {
                self.selectRegion(index);
            });

            list.appendChild(item);
        });

        // Scroll selected into view
        if (this.selectedRegionIndex >= 0) {
            var items = list.querySelectorAll('.region-item');
            var targetItem = items[this.selectedRegionIndex];
            if (targetItem) {
                // Use setTimeout to ensure DOM layout is complete
                setTimeout(function() {
                    // Scroll the list container, not the page
                    var itemTop = targetItem.offsetTop;
                    var itemBottom = itemTop + targetItem.offsetHeight;
                    var listScrollTop = list.scrollTop;
                    var listHeight = list.clientHeight;
                    if (itemBottom > listScrollTop + listHeight) {
                        list.scrollTop = itemBottom - listHeight;
                    } else if (itemTop < listScrollTop) {
                        list.scrollTop = itemTop;
                    }
                }, 0);
            }
        }
    },

    renderRegionProperties: function() {
        var container = document.getElementById('region-properties');
        container.textContent = '';

        if (this.selectedRegionIndex < 0 || !this.regions[this.selectedRegionIndex]) {
            // Show template-level settings when no region is selected
            if (this.currentTemplate) {
                this.buildTemplateSettingsDOM(container);
                this.bindTemplateSettingsEvents();
            } else {
                var empty = document.createElement('div');
                empty.className = 'empty-props';
                empty.textContent = 'Select a region to edit its properties.';
                container.appendChild(empty);
            }
            return;
        }

        var region = this.regions[this.selectedRegionIndex];
        this.buildPropertyEditorDOM(container, region);
        this.bindPropertyEvents();
    },

    // ── DOM-based property editor (no innerHTML) ─────────────────────────

    buildPropertyEditorDOM: function(container, region) {
        var self = this;

        // Helper: create a prop-group with label + child element
        function makeGroup(labelText, child, tooltip) {
            var g = document.createElement('div');
            g.className = 'prop-group';
            var lbl = document.createElement('label');
            lbl.className = 'prop-label';
            lbl.textContent = labelText;
            if (tooltip) {
                lbl.title = tooltip;
                child.title = tooltip;
            }
            g.appendChild(lbl);
            g.appendChild(child);
            return g;
        }

        // Helper: text input
        function makeInput(id, value, type, attrs) {
            var inp = document.createElement('input');
            inp.type = type || 'text';
            inp.className = 'prop-input';
            inp.id = id;
            inp.value = (value !== undefined && value !== null) ? value : '';
            if (type === 'text') inp.spellcheck = false;
            if (attrs) {
                for (var k in attrs) inp.setAttribute(k, attrs[k]);
            }
            return inp;
        }

        // Helper: select
        function makeSelect(id, options, selectedValue) {
            var sel = document.createElement('select');
            sel.className = 'prop-select';
            sel.id = id;
            options.forEach(function(opt) {
                var o = document.createElement('option');
                o.value = opt.value;
                o.textContent = opt.label;
                if (opt.value === selectedValue) o.selected = true;
                sel.appendChild(o);
            });
            return sel;
        }

        // Helper: divider
        function makeDivider() {
            var d = document.createElement('div');
            d.className = 'prop-divider';
            return d;
        }

        // Helper: section label
        function makeSectionLabel(text) {
            var s = document.createElement('div');
            s.className = 'prop-section-label';
            s.textContent = text;
            return s;
        }

        // Helper: prop-row (flex row)
        function makeRow(children) {
            var row = document.createElement('div');
            row.className = 'prop-row';
            children.forEach(function(c) { row.appendChild(c); });
            return row;
        }

        // Helper: checkbox row
        function makeCheckbox(id, labelText, checked, tooltip) {
            var row = document.createElement('div');
            row.className = 'prop-checkbox-row';
            var cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.id = id;
            cb.checked = !!checked;
            if (tooltip) cb.title = tooltip;
            var lbl = document.createElement('label');
            lbl.setAttribute('for', id);
            lbl.textContent = labelText;
            if (tooltip) lbl.title = tooltip;
            row.appendChild(cb);
            row.appendChild(lbl);
            return row;
        }

        // ── Build type options for the type select ──
        var typeOptions = Object.keys(self.TYPE_COLORS).map(function(t) {
            return { value: t, label: self.TYPE_LABELS[t] || t };
        });

        // Label
        container.appendChild(makeGroup('Label', makeInput('prop-label', region.label, 'text'), 'Display name for this region in the region list'));

        // Type
        container.appendChild(makeGroup('Type', makeSelect('prop-type', typeOptions, region.type), 'Region type: determines what content is rendered (text, photo, barcode, etc.)'));

        var t = region.type;

        // Position & Size (not for line regions - they use endpoints)
        if (t !== 'line') {
        container.appendChild(makeDivider());
        container.appendChild(makeSectionLabel('Position & Size'));
        container.appendChild(makeRow([
            makeGroup('X', makeInput('prop-x', self.round4(region.x), 'number', { step: '0.001', min: '0', max: '1' }), 'Horizontal position of the region\'s left edge (0 = left, 1 = right, normalized)'),
            makeGroup('Y', makeInput('prop-y', self.round4(region.y), 'number', { step: '0.001', min: '0', max: '1' }), 'Vertical position of the region\'s top edge (0 = top, 1 = bottom, normalized)')
        ]));
        container.appendChild(makeRow([
            makeGroup('W', makeInput('prop-w', self.round4(region.w), 'number', { step: '0.001', min: '0.001', max: '1' }), 'Width of the region (0 to 1, fraction of document width)'),
            makeGroup('H', makeInput('prop-h', self.round4(region.h), 'number', { step: '0.001', min: '0.001', max: '1' }), 'Height of the region (0 to 1, fraction of document height)')
        ]));
        }

        // Typography section (all text types and mrz)
        if (t.indexOf('text_') === 0 || t === 'mrz') {
            container.appendChild(makeDivider());
            container.appendChild(makeSectionLabel('Typography'));

            // Font family
            var fontOpts = self.fonts.map(function(f) { return { value: f, label: f }; });
            container.appendChild(makeGroup('Font', makeSelect('prop-font-family', fontOpts, region.font_family), 'Font used to render text in this region'));

            // Font size + color row
            container.appendChild(makeRow([
                makeGroup('Size', makeInput('prop-font-size', region.font_size || 14, 'number', { min: '4', max: '200' }), 'Font size in points (pt)'),
                makeGroup('Color', makeInput('prop-color', region.color || '#000000', 'color'), 'Text color')
            ]));

            // Text outline row
            container.appendChild(makeRow([
                makeGroup('Outline', makeInput('prop-outline-color', region.outline_color || '#000000', 'color'), 'Color of text outline/stroke'),
                makeGroup('Width', makeInput('prop-outline-size', region.outline_size || 0, 'number', { min: '0', max: '10', step: '0.5' }), 'Thickness of text outline in pixels (px). 0 = no outline')
            ]));

            // Rotation & Opacity
            container.appendChild(makeRow([
                makeGroup('Rotation', makeInput('prop-rotation', region.rotation || 0, 'number', { min: '-360', max: '360', step: '1' }), 'Rotate text content in degrees (\u00b0). Positive = clockwise'),
                makeGroup('Opacity', makeInput('prop-opacity', region.opacity != null ? region.opacity : 1.0, 'number', { min: '0', max: '1', step: '0.05' }), 'Text transparency (0 = invisible, 1 = fully opaque)')
            ]));

            // Alignment
            var alignGroup = document.createElement('div');
            alignGroup.className = 'prop-group';
            var alignLabel = document.createElement('label');
            alignLabel.className = 'prop-label';
            alignLabel.textContent = 'Alignment';
            alignLabel.title = 'Horizontal text alignment within the region';
            alignGroup.appendChild(alignLabel);
            var alignBtns = document.createElement('div');
            alignBtns.className = 'alignment-group';
            alignBtns.title = 'Horizontal text alignment within the region';
            ['left', 'center', 'right'].forEach(function(a) {
                var btn = document.createElement('button');
                btn.className = 'align-btn' + (region.alignment === a ? ' active' : '');
                btn.dataset.align = a;
                btn.textContent = a.charAt(0).toUpperCase();
                alignBtns.appendChild(btn);
            });
            alignGroup.appendChild(alignBtns);
            container.appendChild(alignGroup);

            // Vertical alignment
            var valignGroup = document.createElement('div');
            valignGroup.className = 'prop-group';
            var valignLabel = document.createElement('label');
            valignLabel.className = 'prop-label';
            valignLabel.textContent = 'V-Align';
            valignLabel.title = 'Vertical text alignment within the region';
            valignGroup.appendChild(valignLabel);
            var valignBtns = document.createElement('div');
            valignBtns.className = 'alignment-group';
            valignBtns.title = 'Vertical text alignment within the region';
            ['top', 'middle', 'bottom'].forEach(function(a) {
                var btn = document.createElement('button');
                btn.className = 'valign-btn' + ((region.valign || 'middle') === a ? ' active' : '');
                btn.dataset.valign = a;
                btn.textContent = a.charAt(0).toUpperCase();
                valignBtns.appendChild(btn);
            });
            valignGroup.appendChild(valignBtns);
            container.appendChild(valignGroup);

            // Bold + Uppercase + letter spacing row
            var boldGroup = document.createElement('div');
            boldGroup.className = 'prop-group';
            boldGroup.appendChild(makeCheckbox('prop-bold', 'Bold', region.bold, 'Render text in bold weight'));
            var uppercaseGroup = document.createElement('div');
            uppercaseGroup.className = 'prop-group';
            uppercaseGroup.appendChild(makeCheckbox('prop-uppercase', 'ALL CAPS', region.uppercase !== false, 'Force all text to uppercase. When unchecked, original case is preserved'));
            container.appendChild(makeRow([
                boldGroup,
                uppercaseGroup,
                makeGroup('Spacing', makeInput('prop-letter-spacing', region.letter_spacing || 0, 'number', { step: '0.5', min: '-5', max: '20' }), 'Extra space between characters in pixels (px). 0 = normal spacing')
            ]));
        }

        // text_name specifics
        if (t === 'text_name') {
            container.appendChild(makeDivider());
            container.appendChild(makeSectionLabel('Name Format'));
            var nameFormats = [
                { value: 'full', label: 'full' },
                { value: 'first_last', label: 'first last' },
                { value: 'last_comma_first', label: 'last, first' },
                { value: 'last_comma_first_middle', label: 'last, first middle' },
                { value: 'last_comma_first_mi', label: 'last, first mi' },
                { value: 'last_comma', label: 'LAST,' },
                { value: 'first_mi', label: 'First MI' }
            ];
            container.appendChild(makeGroup('Format', makeSelect('prop-format', nameFormats, region.format), 'How the person\'s name is formatted in this region'));
        }

        // text_date specifics
        if (t === 'text_date') {
            container.appendChild(makeDivider());
            container.appendChild(makeSectionLabel('Date Options'));
            var dateFields = ['dob', 'issue', 'expiry', 'date_of_examination', 'stcw_expiry_date', 'national_expiry_date', 'pilot_expiry_date', 'last_color_vision_test_date'].map(function(f) { return { value: f, label: f.replace(/_/g, ' ').toUpperCase() }; });
            container.appendChild(makeGroup('Field', makeSelect('prop-field-name', dateFields, region.field_name), 'Which date from the persona profile to use'));
            var dateFmts = ['DD MMM YYYY', 'DD-MMM-YYYY', 'MM/DD/YYYY', 'YYYY-MM-DD', 'DD/MM/YYYY', 'MMM DD, YYYY', 'MMMDD', 'YYYY'].map(function(f) { return { value: f, label: f }; });
            container.appendChild(makeGroup('Format', makeSelect('prop-format', dateFmts, region.format), 'Date display format (e.g., MM/DD/YYYY, DD-MMM-YYYY)'));
        }

        // text_number specifics
        if (t === 'text_number') {
            container.appendChild(makeDivider());
            container.appendChild(makeSectionLabel('Number Field'));
            var numFields = ['passport_no', 'twic_number', 'mmc_number', 'license_number', 'medical_cert_number', 'reference_number'].map(function(f) {
                return { value: f, label: f.replace(/_/g, ' ') };
            });
            container.appendChild(makeGroup('Field', makeSelect('prop-field-name', numFields, region.field_name), 'Which numeric field from the persona profile to display'));
            var suffixInp = makeInput('prop-suffix', region.suffix || '', 'text');
            suffixInp.placeholder = 'e.g. ⚓';
            container.appendChild(makeGroup('Suffix', suffixInp, 'Text appended after the number value'));
        }

        // text_field specifics
        if (t === 'text_field') {
            container.appendChild(makeDivider());
            container.appendChild(makeSectionLabel('Data Field'));
            var dataFields = ['gender', 'gender_full', 'height', 'weight', 'eye_color', 'hair_color', 'nationality', 'place_of_birth', 'dl_class', 'restrictions', 'citizenship', 'medical_cert_number', 'hearing_stcw', 'visual_acuity_stcw', 'color_vision_stcw', 'fit_for_lookout', 'unaided_hearing', 'id_checks_at_exam', 'no_limitations'].map(function(f) {
                return { value: f, label: f.replace(/_/g, ' ') };
            });
            container.appendChild(makeGroup('Field', makeSelect('prop-field-name', dataFields, region.field_name), 'Which persona data field to display (e.g., sex, height, eye_color)'));
        }

        // text_static specifics
        if (t === 'text_static') {
            container.appendChild(makeDivider());
            container.appendChild(makeSectionLabel('Static Text'));
            var textInp = makeInput('prop-text-value', region.text_value || '', 'text');
            textInp.placeholder = 'Enter static text';
            container.appendChild(makeGroup('Text Value', textInp, 'The literal text string to render in this region'));
        }

        // photo specifics
        if (t === 'photo') {
            container.appendChild(makeDivider());
            container.appendChild(makeSectionLabel('Photo Options'));
            container.appendChild(makeCheckbox('prop-grayscale', 'Grayscale', region.grayscale, 'Convert the face photo to grayscale/black-and-white'));
        }

        // mrz specifics
        if (t === 'mrz') {
            container.appendChild(makeDivider());
            container.appendChild(makeSectionLabel('MRZ Options'));
            var lineOpts = [{ value: '1', label: '1' }, { value: '2', label: '2' }];
            var charOpts = [{ value: '44', label: '44' }, { value: '36', label: '36' }];
            container.appendChild(makeRow([
                makeGroup('Lines', makeSelect('prop-num-lines', lineOpts, String(region.num_lines || 2)), 'Number of MRZ lines (1 or 2)'),
                makeGroup('Chars/Line', makeSelect('prop-chars-per-line', charOpts, String(region.chars_per_line || 44)), 'Characters per MRZ line (44 for TD3/passport, 36 for TD2)')
            ]));
        }

        // static_image specifics
        if (t === 'static_image') {
            container.appendChild(makeDivider());
            container.appendChild(makeSectionLabel('Static Image'));
            var fileGroup = document.createElement('div');
            fileGroup.className = 'prop-group';
            var fileLabel = document.createElement('label');
            fileLabel.className = 'prop-file-btn';
            fileLabel.setAttribute('for', 'prop-static-image-file');
            var fileSvg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
            fileSvg.setAttribute('width', '14');
            fileSvg.setAttribute('height', '14');
            fileSvg.setAttribute('viewBox', '0 0 14 14');
            fileSvg.setAttribute('fill', 'none');
            fileSvg.setAttribute('stroke', 'currentColor');
            fileSvg.setAttribute('stroke-width', '1.5');
            var filePath = document.createElementNS('http://www.w3.org/2000/svg', 'path');
            filePath.setAttribute('d', 'M7 2v10M2 7h10');
            fileSvg.appendChild(filePath);
            fileLabel.appendChild(fileSvg);
            var fileLabelText = document.createTextNode(' Upload Image');
            fileLabel.appendChild(fileLabelText);
            fileGroup.appendChild(fileLabel);
            var fileInp = document.createElement('input');
            fileInp.type = 'file';
            fileInp.id = 'prop-static-image-file';
            fileInp.accept = 'image/*';
            fileInp.style.display = 'none';
            fileGroup.appendChild(fileInp);
            container.appendChild(fileGroup);
        }

        // Box properties
        if (t === 'box') {
            container.appendChild(makeDivider());
            container.appendChild(makeSectionLabel('Box Options'));
            container.appendChild(makeRow([
                makeGroup('Border Color', makeInput('prop-border-color', region.border_color || '#000000', 'color'), 'Color of the box outline'),
                makeGroup('Border Width', makeInput('prop-border-width', region.border_width || 1, 'number', { min: '0', max: '20', step: '1' }), 'Thickness of the box border in pixels (px). 0 = no border')
            ]));
            var fillGroup = document.createElement('div');
            fillGroup.className = 'prop-group';
            fillGroup.appendChild(makeCheckbox('prop-fill-enabled', 'Fill', region.fill_enabled !== false && !!region.fill_color, 'Enable a solid fill color inside the box'));
            container.appendChild(makeRow([
                fillGroup,
                makeGroup('Fill Color', makeInput('prop-fill-color', region.fill_color || '#ffffff', 'color'), 'Background fill color of the box'),
                makeGroup('Opacity', makeInput('prop-fill-opacity', region.fill_opacity != null ? region.fill_opacity : 1, 'number', { min: '0', max: '1', step: '0.05' }), 'Fill transparency (0 = invisible, 1 = fully opaque)')
            ]));
        }

        // line specifics
        if (t === 'line') {
            container.appendChild(makeDivider());
            container.appendChild(makeSectionLabel('Line Properties'));
            var lineStyles = [
                { value: 'solid', label: 'Solid' },
                { value: 'dashed', label: 'Dashed' },
                { value: 'dotted', label: 'Dotted' }
            ];
            container.appendChild(makeGroup('Style', makeSelect('prop-line-style', lineStyles, region.line_style || 'solid'), 'Line appearance: solid, dashed, or dotted'));
            container.appendChild(makeRow([
                makeGroup('Width', makeInput('prop-line-width', region.line_width || 2, 'number', { min: '1', max: '10' }), 'Thickness of the line in pixels (px)'),
                makeGroup('Color', makeInput('prop-line-color', region.line_color || '#888888', 'color'), 'Color of the line')
            ]));

            container.appendChild(makeDivider());
            container.appendChild(makeSectionLabel('Fold Options'));
            container.appendChild(makeCheckbox('prop-fold-line', 'Fold Line', region.fold_line, 'Mark this line as a fold line for partial document generation'));
            container.appendChild(makeGroup('Fold %', makeInput('prop-fold-pct', region.fold_pct != null ? region.fold_pct : 0.3, 'number', { min: '0', max: '1', step: '0.05' }), 'Percentage of generated documents that will be folded at this line (e.g., 0.05 = 5%)'));

            // Endpoint coordinates
            container.appendChild(makeDivider());
            container.appendChild(makeSectionLabel('Endpoints'));
            container.appendChild(makeRow([
                makeGroup('X1', makeInput('prop-x1', self.round4(region.x1), 'number', { step: '0.001', min: '0', max: '1' }), 'Start point X coordinate (normalized, 0 = left, 1 = right)'),
                makeGroup('Y1', makeInput('prop-y1', self.round4(region.y1), 'number', { step: '0.001', min: '0', max: '1' }), 'Start point Y coordinate (normalized, 0 = top, 1 = bottom)')
            ]));
            container.appendChild(makeRow([
                makeGroup('X2', makeInput('prop-x2', self.round4(region.x2), 'number', { step: '0.001', min: '0', max: '1' }), 'End point X coordinate (normalized, 0 = left, 1 = right)'),
                makeGroup('Y2', makeInput('prop-y2', self.round4(region.y2), 'number', { step: '0.001', min: '0', max: '1' }), 'End point Y coordinate (normalized, 0 = top, 1 = bottom)')
            ]));
        }
    },

    bindPropertyEvents: function() {
        var self = this;
        var region = this.regions[this.selectedRegionIndex];
        if (!region) return;

        // Helper to bind input changes
        function bindInput(id, key, parser) {
            var el = document.getElementById(id);
            if (!el) return;
            el.addEventListener('input', function() {
                region[key] = parser ? parser(el.value) : el.value;
                self._dirty = true;
                self.render();
                if (key === 'label' || key === 'type') {
                    self.renderRegionList();
                }
            });
        }

        function bindSelect(id, key, parser) {
            var el = document.getElementById(id);
            if (!el) return;
            el.addEventListener('change', function() {
                region[key] = parser ? parser(el.value) : el.value;
                self._dirty = true;
                self.render();
                if (key === 'type') {
                    self.renderRegionList();
                    self.renderRegionProperties();
                }
            });
        }

        function bindCheckbox(id, key) {
            var el = document.getElementById(id);
            if (!el) return;
            el.addEventListener('change', function() {
                region[key] = el.checked;
                self._dirty = true;
                self.render();
            });
        }

        bindInput('prop-label', 'label');
        bindSelect('prop-type', 'type');

        bindInput('prop-x', 'x', parseFloat);
        bindInput('prop-y', 'y', parseFloat);
        bindInput('prop-w', 'w', parseFloat);
        bindInput('prop-h', 'h', parseFloat);

        // Typography
        bindSelect('prop-font-family', 'font_family');
        bindInput('prop-font-size', 'font_size', parseInt);
        bindInput('prop-color', 'color');
        bindCheckbox('prop-bold', 'bold');
        bindCheckbox('prop-uppercase', 'uppercase');
        bindInput('prop-letter-spacing', 'letter_spacing', parseFloat);
        bindInput('prop-outline-color', 'outline_color');
        bindInput('prop-outline-size', 'outline_size', parseFloat);
        bindInput('prop-rotation', 'rotation', parseFloat);
        bindInput('prop-opacity', 'opacity', parseFloat);

        // Alignment buttons
        document.querySelectorAll('.align-btn[data-align]').forEach(function(btn) {
            btn.addEventListener('click', function() {
                region.alignment = btn.dataset.align;
                self._dirty = true;
                document.querySelectorAll('.align-btn').forEach(function(b) { b.classList.remove('active'); });
                btn.classList.add('active');
                self.render();
            });
        });

        // Vertical alignment buttons
        document.querySelectorAll('.valign-btn[data-valign]').forEach(function(btn) {
            btn.addEventListener('click', function() {
                region.valign = btn.dataset.valign;
                self._dirty = true;
                document.querySelectorAll('.valign-btn').forEach(function(b) { b.classList.remove('active'); });
                btn.classList.add('active');
                self.render();
            });
        });

        // Type-specific — for static text, sync label to content
        bindInput('prop-text-value', 'text_value');
        (function() {
            var tvEl = document.getElementById('prop-text-value');
            if (tvEl && region.type === 'text_static') {
                tvEl.addEventListener('input', function() {
                    region.label = tvEl.value || 'Static Text';
                    self.renderRegionList();
                });
            }
        })();
        bindInput('prop-suffix', 'suffix');
        bindSelect('prop-field-name', 'field_name');
        bindSelect('prop-format', 'format');
        bindCheckbox('prop-grayscale', 'grayscale');
        bindSelect('prop-num-lines', 'num_lines', parseInt);
        bindSelect('prop-chars-per-line', 'chars_per_line', parseInt);

        // Static image upload
        var fileInput = document.getElementById('prop-static-image-file');
        if (fileInput) {
            fileInput.addEventListener('change', function() {
                if (fileInput.files && fileInput.files[0]) {
                    region._pendingImage = fileInput.files[0];
                    self._dirty = true;
                    self.showToast('Image attached. Save to upload.', 'success');
                }
            });
        }

        // Box properties
        bindInput('prop-border-color', 'border_color');
        bindInput('prop-border-width', 'border_width', parseInt);
        bindCheckbox('prop-fill-enabled', 'fill_enabled');
        bindInput('prop-fill-color', 'fill_color');
        bindInput('prop-fill-opacity', 'fill_opacity', parseFloat);

        // Line properties
        bindSelect('prop-line-style', 'line_style');
        bindInput('prop-line-width', 'line_width', parseInt);
        bindInput('prop-line-color', 'line_color');
        bindCheckbox('prop-fold-line', 'fold_line');
        bindInput('prop-fold-pct', 'fold_pct', parseFloat);
        bindInput('prop-x1', 'x1', parseFloat);
        bindInput('prop-y1', 'y1', parseFloat);
        bindInput('prop-x2', 'x2', parseFloat);
        bindInput('prop-y2', 'y2', parseFloat);
    },

    buildTemplateSettingsDOM: function(container) {
        var self = this;
        var tpl = this.currentTemplate;

        function makeGroup(labelText, child, tooltip) {
            var g = document.createElement('div');
            g.className = 'prop-group';
            var lbl = document.createElement('label');
            lbl.className = 'prop-label';
            lbl.textContent = labelText;
            if (tooltip) {
                lbl.title = tooltip;
                child.title = tooltip;
            }
            g.appendChild(lbl);
            g.appendChild(child);
            return g;
        }
        function makeInput(id, value, type, attrs) {
            var inp = document.createElement('input');
            inp.type = type || 'text';
            inp.className = 'prop-input';
            inp.id = id;
            inp.value = (value !== undefined && value !== null) ? value : '';
            if (attrs) { for (var k in attrs) inp.setAttribute(k, attrs[k]); }
            return inp;
        }
        function makeCheckbox(id, labelText, checked, tooltip) {
            var row = document.createElement('div');
            row.className = 'prop-checkbox-row';
            var cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.id = id;
            cb.checked = !!checked;
            if (tooltip) cb.title = tooltip;
            var lbl = document.createElement('label');
            lbl.setAttribute('for', id);
            lbl.textContent = labelText;
            if (tooltip) lbl.title = tooltip;
            row.appendChild(cb);
            row.appendChild(lbl);
            return row;
        }
        function makeRow(children) {
            var row = document.createElement('div');
            row.className = 'prop-row';
            children.forEach(function(c) { row.appendChild(c); });
            return row;
        }
        function makeSectionLabel(text) {
            var s = document.createElement('div');
            s.className = 'prop-section-label';
            s.textContent = text;
            return s;
        }
        function makeDivider() {
            var d = document.createElement('div');
            d.className = 'prop-divider';
            return d;
        }

        // Header
        var header = document.createElement('div');
        header.className = 'prop-section-label';
        header.textContent = 'Template Settings';
        header.style.fontSize = '13px';
        header.style.fontWeight = 'bold';
        container.appendChild(header);

        var hint = document.createElement('div');
        hint.className = 'empty-props';
        hint.textContent = 'Select a region to edit its properties, or configure template settings below.';
        hint.style.marginBottom = '12px';
        container.appendChild(hint);

        // ── Print Quality ──
        var pq = tpl.print_quality || {};
        container.appendChild(makeDivider());
        container.appendChild(makeSectionLabel('Print Quality'));
        container.appendChild(makeGroup('Degraded %', makeInput('tpl-pq-degraded-pct', pq.degraded_pct != null ? pq.degraded_pct : 0, 'number', { min: '0', max: '1', step: '0.05' }), 'Percentage of generated documents that receive print degradation effects (0-1)'));
        container.appendChild(makeRow([
            makeGroup('Min Opacity', makeInput('tpl-pq-min-opacity', pq.min_opacity != null ? pq.min_opacity : 0.5, 'number', { min: '0', max: '1', step: '0.05' }), 'Minimum content opacity when degraded (0 = invisible, 1 = full)'),
            makeGroup('Max Opacity', makeInput('tpl-pq-max-opacity', pq.max_opacity != null ? pq.max_opacity : 0.9, 'number', { min: '0', max: '1', step: '0.05' }), 'Maximum content opacity when degraded (0 = invisible, 1 = full)')
        ]));
        container.appendChild(makeCheckbox('tpl-pq-streaks', 'Streaks', pq.streaks, 'Add horizontal photocopier roller streak artifacts to degraded prints'));

        // ── Lighting ──
        var lt = tpl.lighting || {};
        container.appendChild(makeDivider());
        container.appendChild(makeSectionLabel('Lighting'));
        container.appendChild(makeCheckbox('tpl-lt-enabled', 'Enable Lighting Variation', lt.enabled, 'Apply random color temperature shifts to simulate different lighting conditions'));
        container.appendChild(makeRow([
            makeGroup('Temp Min', makeInput('tpl-lt-temp-min', lt.temp_min != null ? lt.temp_min : 2700, 'number', { min: '2700', max: '6500', step: '100' }), 'Lowest color temperature in Kelvin (K). 2700K = warm/yellow, 5500K = daylight'),
            makeGroup('Temp Max', makeInput('tpl-lt-temp-max', lt.temp_max != null ? lt.temp_max : 6500, 'number', { min: '2700', max: '6500', step: '100' }), 'Highest color temperature in Kelvin (K). 5500K = daylight, 6500K = cool/blue')
        ]));

    },

    bindTemplateSettingsEvents: function() {
        var self = this;
        var tpl = this.currentTemplate;
        if (!tpl) return;

        function bindTplInput(id, group, key, parser) {
            var el = document.getElementById(id);
            if (!el) return;
            el.addEventListener('input', function() {
                if (!tpl[group]) tpl[group] = {};
                tpl[group][key] = parser ? parser(el.value) : el.value;
                self._dirty = true;
            });
        }
        function bindTplCheckbox(id, group, key) {
            var el = document.getElementById(id);
            if (!el) return;
            el.addEventListener('change', function() {
                if (!tpl[group]) tpl[group] = {};
                tpl[group][key] = el.checked;
                self._dirty = true;
            });
        }

        // Print Quality
        bindTplInput('tpl-pq-degraded-pct', 'print_quality', 'degraded_pct', parseFloat);
        bindTplInput('tpl-pq-min-opacity', 'print_quality', 'min_opacity', parseFloat);
        bindTplInput('tpl-pq-max-opacity', 'print_quality', 'max_opacity', parseFloat);
        bindTplCheckbox('tpl-pq-streaks', 'print_quality', 'streaks');

        // Lighting
        bindTplCheckbox('tpl-lt-enabled', 'lighting', 'enabled');
        bindTplInput('tpl-lt-temp-min', 'lighting', 'temp_min', parseInt);
        bindTplInput('tpl-lt-temp-max', 'lighting', 'temp_max', parseInt);

    },

    updatePositionInputs: function(region) {
        var xEl = document.getElementById('prop-x');
        var yEl = document.getElementById('prop-y');
        var wEl = document.getElementById('prop-w');
        var hEl = document.getElementById('prop-h');
        if (xEl) xEl.value = this.round4(region.x);
        if (yEl) yEl.value = this.round4(region.y);
        if (wEl) wEl.value = this.round4(region.w);
        if (hEl) hEl.value = this.round4(region.h);
    },

    // ═════════════════════════════════════════════════════════════════════
    // OVERLAYS — Upload, render list, remove, lazy image loading
    // ═════════════════════════════════════════════════════════════════════

    _loadOverlayImage: function(filename) {
        if (this._overlayImages[filename]) return;
        if (!this.currentTemplate) return;
        var self = this;
        var img = new Image();
        img.crossOrigin = 'anonymous';  // needed for getImageData
        img.onload = function() {
            // Pre-process: make white/near-white pixels transparent
            var offscreen = document.createElement('canvas');
            offscreen.width = img.naturalWidth;
            offscreen.height = img.naturalHeight;
            var offCtx = offscreen.getContext('2d');
            offCtx.drawImage(img, 0, 0);

            var imageData = offCtx.getImageData(0, 0, offscreen.width, offscreen.height);
            var data = imageData.data;
            for (var i = 0; i < data.length; i += 4) {
                var r = data[i], g = data[i+1], b = data[i+2];
                var brightness = (r + g + b) / 3;
                if (brightness > 240) {
                    // Fade: 240 = full opacity, 255 = fully transparent
                    var fade = Math.min((brightness - 240) / 15, 1);
                    data[i+3] = Math.round(data[i+3] * (1 - fade));
                }
            }
            offCtx.putImageData(imageData, 0, 0);

            // Create a new image from the processed canvas
            var processedImg = new Image();
            processedImg.onload = function() {
                self._overlayImages[filename] = processedImg;
                self.render();
            };
            processedImg.src = offscreen.toDataURL('image/png');
        };
        img.onerror = function() {
            console.error('Failed to load overlay image:', filename);
        };
        // Set a placeholder to prevent duplicate loads while loading
        this._overlayImages[filename] = img;
        img.src = '/api/doc-templates/' + this.currentTemplate.id + '/overlays/' + encodeURIComponent(filename);
    },

    uploadOverlay: async function(file) {
        if (!this.currentTemplate) return;

        var formData = new FormData();
        formData.append('overlay', file);

        try {
            var resp = await fetch('/api/doc-templates/' + this.currentTemplate.id + '/upload-overlay', {
                method: 'POST',
                body: formData
            });
            var data = await resp.json();
            if (data.success) {
                if (!this.currentTemplate.overlays) {
                    this.currentTemplate.overlays = [];
                }
                this.currentTemplate.overlays.push({
                    filename: data.name,
                    opacity: 0.3,
                    blend_mode: 'screen'
                });
                this._dirty = true;
                this.renderOverlayList();
                this.render();
                this.showToast('Overlay added.', 'success');
            } else {
                this.showToast(data.error || 'Failed to upload overlay.', 'error');
            }
        } catch (e) {
            console.error('Upload overlay error:', e);
            this.showToast('Network error uploading overlay.', 'error');
        }
    },

    renderOverlayList: function() {
        var list = document.getElementById('overlay-list');
        if (!list) return;
        list.textContent = '';

        var overlays = (this.currentTemplate && this.currentTemplate.overlays) ? this.currentTemplate.overlays : [];

        if (overlays.length === 0) {
            var empty = document.createElement('div');
            empty.className = 'empty-overlays';
            empty.textContent = 'No overlays. Add hologram or reflection images.';
            list.appendChild(empty);
            return;
        }

        var self = this;
        overlays.forEach(function(ov, index) {
            var item = document.createElement('div');
            item.className = 'overlay-item';

            // Header row: thumbnail + name + delete button
            var header = document.createElement('div');
            header.className = 'overlay-item-header';

            var thumb = document.createElement('img');
            thumb.className = 'overlay-thumb';
            thumb.alt = ov.filename;
            if (self.currentTemplate) {
                thumb.src = '/api/doc-templates/' + self.currentTemplate.id + '/overlays/' + encodeURIComponent(ov.filename);
            }
            header.appendChild(thumb);

            var info = document.createElement('div');
            info.className = 'overlay-info';
            var name = document.createElement('div');
            name.className = 'overlay-name';
            name.textContent = ov.filename;
            name.title = ov.filename;
            info.appendChild(name);
            header.appendChild(info);

            var delBtn = document.createElement('button');
            delBtn.className = 'btn-danger-ghost';
            delBtn.textContent = '\u00d7';
            delBtn.title = 'Remove overlay';
            delBtn.addEventListener('click', function() {
                self.removeOverlay(index);
            });
            header.appendChild(delBtn);

            item.appendChild(header);

            // Controls: sliders + blend mode
            var controls = document.createElement('div');
            controls.className = 'overlay-controls';

            // Opacity range — two separate sliders (min/max)
            var defaultOp = ov.opacity !== undefined ? ov.opacity : 0.3;
            if (ov.opacity_min == null) ov.opacity_min = defaultOp;
            if (ov.opacity_max == null) ov.opacity_max = defaultOp;
            ov.opacity = (ov.opacity_min + ov.opacity_max) / 2;

            var makeSliderRow = function(label, prop, ov) {
                var row = document.createElement('div');
                row.style.cssText = 'display:flex;align-items:center;gap:6px;font-size:11px;color:#aaa;';
                var lbl = document.createElement('span');
                lbl.textContent = label;
                lbl.style.cssText = 'width:28px;flex-shrink:0;';
                row.appendChild(lbl);
                var slider = document.createElement('input');
                slider.type = 'range';
                slider.className = 'overlay-opacity';
                slider.min = '0'; slider.max = '100'; slider.step = '1';
                slider.value = String(Math.round(ov[prop] * 100));
                row.appendChild(slider);
                var val = document.createElement('span');
                val.textContent = slider.value + '%';
                val.style.cssText = 'width:32px;text-align:right;flex-shrink:0;';
                row.appendChild(val);
                slider.addEventListener('input', function() {
                    ov[prop] = parseInt(this.value) / 100;
                    val.textContent = this.value + '%';
                    ov.opacity = ov[prop];
                    self._dirty = true;
                    self.render();
                });
                slider.addEventListener('change', function() {
                    ov.opacity = (ov.opacity_min + ov.opacity_max) / 2;
                    self._dirty = true;
                    self.render();
                });
                return row;
            };

            controls.appendChild(makeSliderRow('Min', 'opacity_min', ov));
            controls.appendChild(makeSliderRow('Max', 'opacity_max', ov));

            // Blend mode row
            var blendRow = document.createElement('div');
            blendRow.className = 'overlay-controls-row';
            var blendLabel = document.createElement('span');
            blendLabel.textContent = 'Blend';
            blendLabel.style.cssText = 'width:28px;flex-shrink:0;font-size:11px;color:#aaa;';
            blendRow.appendChild(blendLabel);

            var blendSelect = document.createElement('select');
            blendSelect.className = 'overlay-blend';
            var blendModes = [
                { value: 'screen', label: 'Screen' },
                { value: 'overlay', label: 'Overlay' },
                { value: 'multiply', label: 'Multiply' },
                { value: 'soft_light', label: 'Soft Light' },
                { value: 'normal', label: 'Normal' }
            ];
            blendModes.forEach(function(bm) {
                var opt = document.createElement('option');
                opt.value = bm.value;
                opt.textContent = bm.label;
                if (bm.value === (ov.blend_mode || 'screen')) opt.selected = true;
                blendSelect.appendChild(opt);
            });
            blendSelect.addEventListener('change', function() {
                ov.blend_mode = this.value;
                self._dirty = true;
                self.render();
            });
            blendRow.appendChild(blendSelect);
            controls.appendChild(blendRow);

            item.appendChild(controls);
            list.appendChild(item);
        });
    },

    removeOverlay: function(index) {
        if (!this.currentTemplate || !this.currentTemplate.overlays) return;
        var removed = this.currentTemplate.overlays.splice(index, 1);
        if (removed.length > 0 && removed[0].filename) {
            delete this._overlayImages[removed[0].filename];
        }
        this._dirty = true;
        this.renderOverlayList();
        this.render();
    },

    // ═════════════════════════════════════════════════════════════════════
    // API CALLS — Save, Preview, Generate
    // ═════════════════════════════════════════════════════════════════════

    _scheduleAutosave: function() {
        if (this._autosaveTimer) clearTimeout(this._autosaveTimer);
        var self = this;
        this._autosaveTimer = setTimeout(function() {
            self._autosaveDraft();
        }, 3000);
    },

    _autosaveDraft: async function() {
        if (!this.currentTemplate || !this._dirty) return;

        var classSelect = document.getElementById('template-class-select');
        var classSelectVal = classSelect ? classSelect.value : '';
        var nameInput = document.getElementById('template-name-input');
        var payload = {
            name: nameInput ? nameInput.value.trim() : (this.currentTemplate.name || ''),
            class_id: this.CLASS_IDS[classSelectVal] !== undefined ? this.CLASS_IDS[classSelectVal] : (this.currentTemplate.class_id || 0),
            regions: this.regions.map(function(r) {
                var clean = {};
                for (var k in r) {
                    if (k.charAt(0) !== '_') {
                        clean[k] = r[k];
                    }
                }
                return clean;
            }),
            overlays: (this.currentTemplate.overlays || []).map(function(o) {
                var oMin = o.opacity_min != null ? o.opacity_min : (o.opacity || 0.3);
                var oMax = o.opacity_max != null ? o.opacity_max : (o.opacity || 0.3);
                return {
                    filename: o.filename,
                    opacity: (oMin + oMax) / 2,
                    opacity_min: oMin,
                    opacity_max: oMax,
                    blend_mode: o.blend_mode
                };
            }),
            print_quality: this.currentTemplate.print_quality || null,
            lighting: this.currentTemplate.lighting || null,
            scene: this.currentTemplate.scene || null
        };

        try {
            var resp = await fetch('/api/doc-templates/' + this.currentTemplate.id, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            var data = await resp.json();
            if (data.success) {
                this._dirty = false;
                this.showToast('Draft saved', 'success');
            }
        } catch (e) {
            console.error('Autosave error:', e);
        }
    },

    saveTemplate: async function() {
        if (!this.currentTemplate) return;
        if (this._autosaveTimer) { clearTimeout(this._autosaveTimer); this._autosaveTimer = null; }

        var classSelectVal = document.getElementById('template-class-select').value;
        var payload = {
            name: document.getElementById('template-name-input').value.trim(),
            class_id: this.CLASS_IDS[classSelectVal] !== undefined ? this.CLASS_IDS[classSelectVal] : 0,
            regions: this.regions.map(function(r) {
                // Strip internal keys
                var clean = {};
                for (var k in r) {
                    if (k.charAt(0) !== '_') {
                        clean[k] = r[k];
                    }
                }
                return clean;
            }),
            overlays: (this.currentTemplate.overlays || []).map(function(o) {
                var oMin = o.opacity_min != null ? o.opacity_min : (o.opacity || 0.3);
                var oMax = o.opacity_max != null ? o.opacity_max : (o.opacity || 0.3);
                return {
                    filename: o.filename,
                    opacity: (oMin + oMax) / 2,
                    opacity_min: oMin,
                    opacity_max: oMax,
                    blend_mode: o.blend_mode
                };
            }),
            // Template-level settings
            print_quality: this.currentTemplate.print_quality || null,
            lighting: this.currentTemplate.lighting || null
        };

        try {
            var resp = await fetch('/api/doc-templates/' + this.currentTemplate.id, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            var data = await resp.json();
            if (data.success) {
                this._dirty = false;
                this.currentTemplate.name = payload.name;
                this.currentTemplate.class_name = classSelectVal;
                this.showToast('Template saved.', 'success');
            } else {
                this.showToast(data.error || 'Failed to save.', 'error');
            }
        } catch (e) {
            console.error('Save error:', e);
            this.showToast('Network error saving template.', 'error');
        }
    },

    previewTemplate: async function() {
        if (!this.currentTemplate) return;

        var modal = document.getElementById('modal-preview');
        var loading = document.getElementById('preview-loading');
        var img = document.getElementById('preview-image');

        modal.style.display = 'flex';
        loading.style.display = 'block';
        loading.textContent = 'Generating preview...';
        img.style.display = 'none';

        try {
            var resp = await fetch('/api/doc-templates/' + this.currentTemplate.id + '/preview', {
                method: 'POST'
            });
            if (!resp.ok) {
                throw new Error('Preview request failed');
            }
            var blob = await resp.blob();
            var url = URL.createObjectURL(blob);
            img.onload = function() {
                loading.style.display = 'none';
                img.style.display = 'block';
            };
            img.src = url;
        } catch (e) {
            console.error('Preview error:', e);
            loading.textContent = 'Preview generation failed.';
        }
    },

    showGenerateModal: function() {
        document.getElementById('gen-count').value = '50';
        document.getElementById('gen-scene-pct').value = '30';
        document.getElementById('gen-scene-pct-val').textContent = '30%';
        document.getElementById('gen-max-perspective').value = '8';
        document.getElementById('gen-max-perspective-val').textContent = '8%';
        document.getElementById('gen-max-rotation').value = '3';
        document.getElementById('gen-max-rotation-val').textContent = '3°';
        document.getElementById('gen-photocopy-pct').value = '0';
        document.getElementById('gen-photocopy-pct-val').textContent = '0%';
        document.getElementById('gen-oversaturated-pct').value = '30';
        document.getElementById('gen-oversaturated-pct-val').textContent = '30%';
        document.getElementById('gen-washout-min').value = '20';
        document.getElementById('gen-washout-min-val').textContent = '20%';
        document.getElementById('gen-washout-max').value = '70';
        document.getElementById('gen-washout-max-val').textContent = '70%';
        document.getElementById('gen-oversat-min').value = '20';
        document.getElementById('gen-oversat-min-val').textContent = '20%';
        document.getElementById('gen-oversat-max').value = '70';
        document.getElementById('gen-oversat-max-val').textContent = '70%';
        document.getElementById('gen-progress-section').style.display = 'none';
        document.getElementById('btn-start-generate').disabled = false;
        document.getElementById('modal-generate').style.display = 'flex';
    },

    startGeneration: async function() {
        if (!this.currentTemplate) return;

        var count = parseInt(document.getElementById('gen-count').value) || 50;
        var scenePct = parseInt(document.getElementById('gen-scene-pct').value) || 0;
        var maxPerspective = parseInt(document.getElementById('gen-max-perspective').value) || 8;
        var maxRotation = parseInt(document.getElementById('gen-max-rotation').value) || 3;
        var photocopyPct = parseInt(document.getElementById('gen-photocopy-pct').value) || 0;
        var oversaturatedPct = parseInt(document.getElementById('gen-oversaturated-pct').value) || 30;
        var washoutMin = parseInt(document.getElementById('gen-washout-min').value) || 20;
        var washoutMax = parseInt(document.getElementById('gen-washout-max').value) || 70;
        var oversatMin = parseInt(document.getElementById('gen-oversat-min').value) || 20;
        var oversatMax = parseInt(document.getElementById('gen-oversat-max').value) || 70;

        document.getElementById('gen-progress-section').style.display = 'block';
        document.getElementById('gen-progress-fill').style.width = '0%';
        document.getElementById('gen-progress-text').textContent = '0 / ' + count;
        document.getElementById('btn-start-generate').disabled = true;

        try {
            var resp = await fetch('/api/doc-templates/' + this.currentTemplate.id + '/generate', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    count: count, scene_pct: scenePct,
                    max_perspective: maxPerspective, max_rotation: maxRotation,
                    photocopy_pct: photocopyPct, oversaturated_pct: oversaturatedPct,
                    washout_min: washoutMin, washout_max: washoutMax,
                    oversat_min: oversatMin, oversat_max: oversatMax
                })
            });
            var data = await resp.json();
            if (data.success) {
                this.pollGenerationStatus(this.currentTemplate.id, count);
            } else {
                this.showToast(data.error || 'Failed to start generation.', 'error');
                document.getElementById('btn-start-generate').disabled = false;
            }
        } catch (e) {
            console.error('Generate error:', e);
            this.showToast('Network error starting generation.', 'error');
            document.getElementById('btn-start-generate').disabled = false;
        }
    },

    pollGenerationStatus: function(templateId, total) {
        var self = this;
        var interval = setInterval(async function() {
            try {
                var resp = await fetch('/api/doc-templates/' + templateId + '/generate/status');
                var data = await resp.json();
                if (!data.success) {
                    clearInterval(interval);
                    self.showToast('Generation status error.', 'error');
                    document.getElementById('btn-start-generate').disabled = false;
                    return;
                }

                var completed = data.completed || 0;
                var pct = total > 0 ? Math.round((completed / total) * 100) : 0;
                document.getElementById('gen-progress-fill').style.width = pct + '%';
                document.getElementById('gen-progress-text').textContent = completed + ' / ' + total;

                if (data.status === 'completed') {
                    clearInterval(interval);
                    document.getElementById('gen-progress-fill').style.width = '100%';
                    document.getElementById('gen-progress-text').textContent = total + ' / ' + total + ' \u2014 Complete';
                    self.showToast('Generation complete. ' + total + ' documents created.', 'success');
                    document.getElementById('btn-start-generate').disabled = false;
                } else if (data.status === 'failed') {
                    clearInterval(interval);
                    self.showToast('Generation failed: ' + (data.error || 'Unknown error'), 'error');
                    document.getElementById('btn-start-generate').disabled = false;
                }
            } catch (e) {
                clearInterval(interval);
                console.error('Poll error:', e);
                document.getElementById('btn-start-generate').disabled = false;
            }
        }, 1000);
    },

    // ═════════════════════════════════════════════════════════════════════
    // FONTS
    // ═════════════════════════════════════════════════════════════════════

    _fontData: [],       // Raw font list from API [{name, path}]
    _loadedFonts: {},    // Track which fonts have been loaded into browser

    loadFonts: async function() {
        try {
            var resp = await fetch('/api/doc-templates/fonts');
            var data = await resp.json();
            if (data.success && data.fonts) {
                this._fontData = data.fonts;
                this.fonts = data.fonts.map(function(f) { return f.name; });
            }
        } catch (e) {
            console.warn('Failed to load font list:', e);
            this.fonts = ['LiberationSans-Regular', 'LiberationSansNarrow-Regular', 'LiberationMono-Regular', 'NimbusSans-Regular', 'NimbusSansNarrow-Regular'];
        }
    },

    // Lazy-load a single font into the browser when needed for canvas rendering
    ensureFontLoaded: function(fontName) {
        if (!fontName || this._loadedFonts[fontName]) return;
        this._loadedFonts[fontName] = 'loading';
        var fontInfo = this._fontData.find(function(f) { return f.name === fontName; });
        if (!fontInfo) { this._loadedFonts[fontName] = 'missing'; return; }
        var url = '/api/doc-templates/fonts/' + encodeURIComponent(fontName);
        var ext = fontInfo.path.endsWith('.otf') ? 'opentype' : 'truetype';
        var self = this;
        var face = new FontFace(fontName, 'url(' + url + ') format("' + ext + '")');
        face.load().then(function(loaded) {
            document.fonts.add(loaded);
            self._loadedFonts[fontName] = 'loaded';
            self.render(); // Re-render canvas with the newly loaded font
        }).catch(function() {
            self._loadedFonts[fontName] = 'failed';
        });
    },

    // ═════════════════════════════════════════════════════════════════════
    // SCENE BACKGROUNDS — Library manager
    // ═════════════════════════════════════════════════════════════════════

    initSceneBackgrounds: function() {
        var self = this;

        // Collapsible panel toggle
        var header = document.getElementById('scene-bg-panel-header');
        if (header) {
            header.addEventListener('click', function() {
                var panel = document.getElementById('scene-bg-panel');
                panel.classList.toggle('open');
                // Load on first open
                if (panel.classList.contains('open')) {
                    self.loadSceneBackgrounds();
                }
            });
        }

        // File input (browse button)
        var fileInput = document.getElementById('scene-bg-file-input');
        var uploadBtn = document.getElementById('scene-bg-upload-btn');
        if (uploadBtn && fileInput) {
            uploadBtn.addEventListener('click', function(e) {
                e.stopPropagation();
                fileInput.click();
            });
        }
        if (fileInput) {
            fileInput.addEventListener('change', function() {
                if (fileInput.files && fileInput.files.length > 0) {
                    self.uploadSceneBackgrounds(fileInput.files);
                    fileInput.value = '';
                }
            });
        }

        // Dropzone click → open file picker
        var dropzone = document.getElementById('scene-bg-dropzone');
        if (dropzone) {
            dropzone.addEventListener('click', function(e) {
                // Don't trigger if the button inside was clicked
                if (e.target.id === 'scene-bg-upload-btn') return;
                if (fileInput) fileInput.click();
            });

            // Drag and drop events
            dropzone.addEventListener('dragover', function(e) {
                e.preventDefault();
                e.stopPropagation();
                dropzone.classList.add('drag-over');
            });
            dropzone.addEventListener('dragleave', function(e) {
                e.preventDefault();
                e.stopPropagation();
                dropzone.classList.remove('drag-over');
            });
            dropzone.addEventListener('drop', function(e) {
                e.preventDefault();
                e.stopPropagation();
                dropzone.classList.remove('drag-over');
                var files = e.dataTransfer && e.dataTransfer.files;
                if (files && files.length > 0) {
                    // Filter to only images
                    var imageFiles = [];
                    for (var i = 0; i < files.length; i++) {
                        if (files[i].type === 'image/jpeg' || files[i].type === 'image/png') {
                            imageFiles.push(files[i]);
                        }
                    }
                    if (imageFiles.length > 0) {
                        self.uploadSceneBackgrounds(imageFiles);
                    } else {
                        self.showToast('Please drop JPG or PNG images.', 'error');
                    }
                }
            });
        }
    },

    loadSceneBackgrounds: async function() {
        try {
            var resp = await fetch('/api/doc-templates/scene-backgrounds');
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            var data = await resp.json();
            var backgrounds = Array.isArray(data) ? data : (data.scene_backgrounds || data.backgrounds || []);
            this._renderSceneBackgroundGrid(backgrounds);
        } catch (e) {
            console.error('Failed to load scene backgrounds:', e);
            this._renderSceneBackgroundGrid([]);
        }
    },

    uploadSceneBackgrounds: async function(files) {
        var self = this;
        var uploading = document.getElementById('scene-bg-uploading');
        if (uploading) uploading.classList.add('active');

        var formData = new FormData();
        var fileArray = Array.prototype.slice.call(files);
        fileArray.forEach(function(f) {
            formData.append('files', f);
        });

        try {
            var resp = await fetch('/api/doc-templates/scene-backgrounds', {
                method: 'POST',
                body: formData
            });
            if (!resp.ok) {
                var errData = {};
                try { errData = await resp.json(); } catch (_) {}
                throw new Error(errData.error || errData.detail || 'Upload failed (' + resp.status + ')');
            }
            var data = await resp.json();
            self.showToast(
                (fileArray.length === 1 ? '1 background' : fileArray.length + ' backgrounds') + ' uploaded.',
                'success'
            );
            await self.loadSceneBackgrounds();
        } catch (e) {
            console.error('Scene background upload error:', e);
            self.showToast(e.message || 'Upload failed.', 'error');
        } finally {
            if (uploading) uploading.classList.remove('active');
        }
    },

    deleteSceneBackground: async function(filename) {
        var self = this;
        try {
            var resp = await fetch('/api/doc-templates/scene-backgrounds/' + encodeURIComponent(filename), {
                method: 'DELETE'
            });
            if (!resp.ok) {
                var errData = {};
                try { errData = await resp.json(); } catch (_) {}
                throw new Error(errData.error || errData.detail || 'Delete failed (' + resp.status + ')');
            }
            self.showToast('Background deleted.', 'success');
            await self.loadSceneBackgrounds();
        } catch (e) {
            console.error('Delete scene background error:', e);
            self.showToast(e.message || 'Delete failed.', 'error');
        }
    },

    _renderSceneBackgroundGrid: function(backgrounds) {
        var self = this;
        var grid = document.getElementById('scene-bg-grid');
        var countEl = document.getElementById('scene-bg-count');

        if (!grid) return;

        // Update count badge
        if (countEl) {
            countEl.textContent = backgrounds.length + ' loaded';
        }

        grid.textContent = '';

        if (!backgrounds || backgrounds.length === 0) {
            var empty = document.createElement('div');
            empty.className = 'scene-bg-empty';
            empty.textContent = 'No backgrounds loaded yet. Upload JPG/PNG images above.';
            grid.appendChild(empty);
            return;
        }

        backgrounds.forEach(function(bg) {
            var filename = bg.filename || bg.name || '';
            var url = bg.url || ('/api/doc-templates/scene-backgrounds/' + encodeURIComponent(filename));

            var card = document.createElement('div');
            card.className = 'scene-bg-card';

            // Thumbnail image
            var img = document.createElement('img');
            img.className = 'scene-bg-thumb';
            img.alt = filename;
            img.loading = 'lazy';
            img.src = url;
            img.onerror = function() {
                img.style.display = 'none';
                var ph = document.createElement('div');
                ph.className = 'scene-bg-thumb-placeholder';
                ph.textContent = 'No preview';
                card.insertBefore(ph, card.firstChild);
            };
            card.appendChild(img);

            // Filename label
            var info = document.createElement('div');
            info.className = 'scene-bg-card-info';
            info.textContent = filename;
            info.title = filename;
            card.appendChild(info);

            // Delete button
            var delBtn = document.createElement('button');
            delBtn.className = 'scene-bg-delete-btn';
            delBtn.title = 'Delete ' + filename;
            delBtn.textContent = '\u00d7';
            delBtn.addEventListener('click', function(e) {
                e.stopPropagation();
                if (confirm('Delete "' + filename + '"?')) {
                    self.deleteSceneBackground(filename);
                }
            });
            card.appendChild(delBtn);

            grid.appendChild(card);
        });
    },

    // ═════════════════════════════════════════════════════════════════════
    // UTILITIES
    // ═════════════════════════════════════════════════════════════════════

    round4: function(n) {
        return Math.round((n || 0) * 10000) / 10000;
    },

    showToast: function(msg, type) {
        var existing = document.querySelector('.toast');
        if (existing) existing.remove();

        var toast = document.createElement('div');
        toast.className = 'toast' + (type ? ' toast-' + type : '');
        toast.textContent = msg;
        document.body.appendChild(toast);

        // Trigger reflow for animation
        toast.offsetHeight; // eslint-disable-line no-unused-expressions
        toast.classList.add('show');

        clearTimeout(this._toastTimer);
        this._toastTimer = setTimeout(function() {
            toast.classList.remove('show');
            setTimeout(function() { toast.remove(); }, 300);
        }, 3000);
    }
};

document.addEventListener('DOMContentLoaded', function() { TemplateAnnotator.init(); });
