/* Face Photo Manager — vanilla JS module */
var FacePhotoManager = {
    // ── State ─────────────────────────────────────────────────────────────
    items: [],
    page: 1,
    perPage: 60,
    totalPages: 0,
    loading: false,
    filters: { variant: 'original' },
    selectedItems: new Set(),
    imageObserver: null,
    scrollObserver: null,
    preprocessPollTimer: null,
    genderPollTimer: null,
    currentPreviewItem: null,

    // ── Initialization ────────────────────────────────────────────────────
    init: function() {
        this.setupInfiniteScroll();
        this.bindEvents();
        this.loadStats();
        this.loadPage();
    },

    // ── Stats ─────────────────────────────────────────────────────────────
    loadStats: async function() {
        try {
            var resp = await fetch('/api/faces/stats');
            var data = await resp.json();
            if (data.success) {
                document.getElementById('stat-originals').textContent =
                    (data.originals || 0).toLocaleString();
                document.getElementById('stat-preprocessed').textContent =
                    (data.preprocessed || 0).toLocaleString();
                document.getElementById('stat-unprocessed').textContent =
                    (data.unprocessed || 0).toLocaleString();
                document.getElementById('stat-profiles').textContent =
                    (data.profiles || 0).toLocaleString();
            }
        } catch (e) {
            console.error('Failed to load face stats:', e);
        }
    },

    // ── Page Loading ──────────────────────────────────────────────────────
    loadPage: async function() {
        if (this.loading) return;
        this.loading = true;

        var params = new URLSearchParams({
            page: this.page,
            per_page: this.perPage,
            variant: this.filters.variant
        });

        try {
            var resp = await fetch('/api/faces/items?' + params);
            var data = await resp.json();

            if (this.page === 1) {
                var grid = document.getElementById('fpm-grid');
                grid.textContent = '';
                var initLoading = document.getElementById('initial-loading');
                if (initLoading) initLoading.style.display = 'none';
            }

            this.totalPages = data.pages || 1;
            this.renderGrid(data.items || []);
            this.updateFilterStats(data.total || 0);

            if ((data.items || []).length === 0 && this.page === 1) {
                var empty = document.createElement('div');
                empty.className = 'empty-state';
                empty.textContent = 'No face photos found. Upload some to get started.';
                document.getElementById('fpm-grid').appendChild(empty);
            }
        } catch (e) {
            console.error('Failed to load face photos:', e);
            if (this.page === 1) {
                var errEl = document.createElement('div');
                errEl.className = 'empty-state error';
                errEl.textContent = 'Failed to load face photos.';
                document.getElementById('fpm-grid').appendChild(errEl);
            }
        }

        this.loading = false;
    },

    // ── Rendering ─────────────────────────────────────────────────────────
    renderGrid: function(items) {
        var grid = document.getElementById('fpm-grid');
        var self = this;
        items.forEach(function(item) {
            self.items.push(item);
            grid.appendChild(self.createCard(item));
        });
    },

    createCard: function(item) {
        var self = this;
        var isTransparent = this.filters.variant === 'white' || (this.filters.variant === 'both' && item.has_white);
        var isSelected = this.selectedItems.has(item.id);
        var showBoth = this.filters.variant === 'both';

        var card = document.createElement('div');
        card.className = 'face-card' +
            (isTransparent ? ' variant-preprocessed' : '') +
            (isSelected ? ' selected' : '');
        card.dataset.itemId = item.id;
        card.dataset.variant = item.variant || 'original';

        // Checkbox
        var checkWrap = document.createElement('div');
        checkWrap.className = 'card-checkbox';
        var checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.checked = isSelected;
        checkbox.dataset.id = item.id;
        checkWrap.appendChild(checkbox);
        card.appendChild(checkWrap);

        // Thumbnail
        var thumb = document.createElement('div');
        thumb.className = 'card-thumb' + (isTransparent ? ' transparent-bg' : '');

        var img = document.createElement('img');
        img.alt = '';
        img.dataset.src = item.image_url;
        img.dataset.retries = '0';
        img.onerror = function() {
            var r = parseInt(this.dataset.retries || '0');
            if (r < 3) {
                this.dataset.retries = r + 1;
                var base = this.src.split('?')[0];
                var self2 = this;
                setTimeout(function() { self2.src = base + '?r=' + (r + 1); }, 500 * (r + 1));
            }
        };

        if (this.imageObserver) {
            this.imageObserver.observe(img);
        } else {
            img.src = img.dataset.src;
        }

        thumb.appendChild(img);

        // Variant badge (only in "both" view)
        if (showBoth) {
            var badge = document.createElement('div');
            badge.className = 'variant-badge ' +
                (isTransparent ? 'variant-badge-preprocessed' : 'variant-badge-original');
            badge.textContent = isTransparent ? 'PP' : 'Orig';
            thumb.appendChild(badge);
        }

        card.appendChild(thumb);

        // Info row
        var info = document.createElement('div');
        info.className = 'card-info';
        var fname = document.createElement('div');
        fname.className = 'card-filename';
        fname.textContent = item.filename || item.id;
        info.appendChild(fname);
        card.appendChild(info);

        // Profile badge
        if (item.has_profile && item.profile_name) {
            var badge = document.createElement('div');
            badge.className = 'card-profile-badge';
            badge.textContent = item.profile_name;
            thumb.appendChild(badge);
        }

        // Gender badge
        if (item.detected_gender) {
            var gBadge = document.createElement('div');
            gBadge.className = 'card-gender-badge gender-' + item.detected_gender.toLowerCase();
            gBadge.textContent = item.detected_gender === 'M' ? 'M' : 'F';
            gBadge.title = item.detected_gender === 'M' ? 'Male (detected)' : 'Female (detected)';
            thumb.appendChild(gBadge);
        }

        // Checkbox toggle
        checkbox.addEventListener('change', function(e) {
            e.stopPropagation();
            self.toggleItemSelection(item.id, e.target.checked, card);
        });

        // Click to preview or multi-select
        card.addEventListener('click', function(e) {
            if (e.target === checkbox) return;

            if (e.shiftKey || e.ctrlKey || e.metaKey) {
                // Multi-select modifier
                var newChecked = !checkbox.checked;
                checkbox.checked = newChecked;
                self.toggleItemSelection(item.id, newChecked, card);
                return;
            }

            // Normal click: open preview
            self.showPreview(item);
        });

        return card;
    },

    // ── Selection ─────────────────────────────────────────────────────────
    toggleItemSelection: function(id, checked, card) {
        if (checked) {
            this.selectedItems.add(id);
            card.classList.add('selected');
        } else {
            this.selectedItems.delete(id);
            card.classList.remove('selected');
        }
        this.updateActionBar();
    },

    selectAll: function() {
        var self = this;
        this.items.forEach(function(item) {
            self.selectedItems.add(item.id);
        });
        document.querySelectorAll('.face-card').forEach(function(card) {
            card.classList.add('selected');
            var cb = card.querySelector('input[type=checkbox]');
            if (cb) cb.checked = true;
        });
        this.updateActionBar();
    },

    deselectAll: function() {
        this.selectedItems.clear();
        document.querySelectorAll('.face-card').forEach(function(card) {
            card.classList.remove('selected');
            var cb = card.querySelector('input[type=checkbox]');
            if (cb) cb.checked = false;
        });
        this.updateActionBar();
    },

    updateActionBar: function() {
        var bar = document.getElementById('action-bar');
        var count = this.selectedItems.size;
        if (count > 0) {
            bar.style.display = 'flex';
            document.getElementById('selection-count').textContent =
                count.toLocaleString() + ' selected';
        } else {
            bar.style.display = 'none';
        }
    },

    updateFilterStats: function(total) {
        var el = document.getElementById('filter-stats');
        if (el) {
            el.textContent = total.toLocaleString() + ' photo' + (total !== 1 ? 's' : '');
        }
    },

    // ── Preview Modal ──────────────────────────────────────────────────────
    showPreview: function(item) {
        var modal = document.getElementById('preview-modal');
        var img = document.getElementById('preview-img');
        var wrap = document.getElementById('preview-image-wrap');
        var meta = document.getElementById('preview-meta');

        this.currentPreviewItem = item;

        var isTransparent = this.filters.variant === 'white' || (this.filters.variant === 'both' && item.has_white);

        img.src = item.image_url || '';
        img.alt = item.filename || '';

        wrap.className = 'preview-image-wrap' + (isTransparent ? ' transparent-bg' : '');

        var parts = [];
        if (item.filename) parts.push(item.filename);
        if (item.detected_gender) parts.push('Gender: ' + (item.detected_gender === 'M' ? 'Male' : 'Female'));
        if (item.variant) parts.push(item.variant === 'white' || item.variant === 'preprocessed'
            ? 'Preprocessed' : 'Original');
        meta.textContent = parts.join(' \u2014 ');

        modal.style.display = 'flex';

        // Load profile
        this.loadPreviewProfile(item.filename);
    },

    hidePreview: function() {
        var modal = document.getElementById('preview-modal');
        modal.style.display = 'none';
        document.getElementById('preview-img').src = '';
    },

    loadPreviewProfile: async function(filename) {
        var panel = document.getElementById('profile-panel');
        var fields = document.getElementById('profile-fields');
        var docs = document.getElementById('profile-docs');
        var metaInfo = document.getElementById('profile-meta-info');
        var editForm = document.getElementById('profile-edit-form');

        panel.style.display = 'none';
        editForm.style.display = 'none';
        fields.textContent = '';
        docs.textContent = '';
        metaInfo.textContent = '';

        try {
            var resp = await fetch('/api/faces/' + encodeURIComponent(filename) + '/profile');
            var data = await resp.json();

            if (!data.success || !data.profile) {
                panel.style.display = 'block';
                fields.innerHTML = '<div class="profile-empty">No profile yet. One will be created when this face is used in document generation.</div>';
                document.getElementById('btn-profile-edit').style.display = 'none';
                document.getElementById('btn-profile-reset').style.display = 'none';
                return;
            }

            var profile = data.profile;
            var identity = profile.identity || {};

            document.getElementById('btn-profile-edit').style.display = '';
            document.getElementById('btn-profile-reset').style.display = '';

            var rows = [
                ['Name', (identity.first_name || '') + ' ' + (identity.last_name || '')],
                ['Gender', identity.gender === 'M' ? 'Male' : 'Female'],
                ['DOB', identity.dob || '\u2014'],
                ['Address', identity.address || '\u2014'],
                ['Height', identity.height_inches ? Math.floor(identity.height_inches/12) + "'" + (identity.height_inches%12) + '"' : '\u2014'],
                ['Weight', identity.weight ? identity.weight + ' lbs' : '\u2014'],
                ['Eyes', identity.eye_color || '\u2014'],
                ['Hair', identity.hair_color || '\u2014'],
                ['Place of Birth', identity.place_of_birth || '\u2014'],
            ];

            var html = '<table class="profile-table">';
            rows.forEach(function(row) {
                html += '<tr><td class="profile-label">' + row[0] + '</td><td class="profile-value">' + row[1] + '</td></tr>';
            });
            html += '</table>';
            fields.innerHTML = html;

            var documents = profile.documents || {};
            var docTypes = Object.keys(documents);
            if (docTypes.length > 0) {
                var docHtml = '<div class="profile-section-title">Documents</div>';
                docTypes.forEach(function(dt) {
                    var doc = documents[dt];
                    var count = doc.generated_count || 0;
                    docHtml += '<div class="profile-doc-item">';
                    docHtml += '<span class="profile-doc-type">' + dt.toUpperCase() + '</span>';
                    docHtml += '<span class="profile-doc-count">' + count + ' generated</span>';
                    docHtml += '</div>';
                });
                docs.innerHTML = docHtml;
            }

            metaInfo.innerHTML = '<span>Total generations: ' + (profile.generation_count || 0) + '</span>' +
                (profile.last_used ? '<span>Last used: ' + profile.last_used.split('T')[0] + '</span>' : '');

            panel.style.display = 'block';

        } catch (e) {
            console.error('Failed to load profile:', e);
        }
    },

    showProfileEdit: function() {
        var item = this.currentPreviewItem;
        if (!item) return;

        var self = this;
        fetch('/api/faces/' + encodeURIComponent(item.filename) + '/profile')
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (!data.success || !data.profile) return;
                var identity = data.profile.identity || {};

                document.getElementById('edit-first-name').value = identity.first_name || '';
                document.getElementById('edit-last-name').value = identity.last_name || '';
                document.getElementById('edit-dob').value = identity.dob || '';
                document.getElementById('edit-gender').value = identity.gender || 'M';
                document.getElementById('edit-eye-color').value = identity.eye_color || 'BRO';
                document.getElementById('edit-hair-color').value = identity.hair_color || 'BRO';

                document.getElementById('profile-fields').style.display = 'none';
                document.getElementById('profile-edit-form').style.display = 'block';
            });
    },

    cancelProfileEdit: function() {
        document.getElementById('profile-fields').style.display = '';
        document.getElementById('profile-edit-form').style.display = 'none';
    },

    saveProfileEdit: async function() {
        var item = this.currentPreviewItem;
        if (!item) return;

        var updates = {
            first_name: document.getElementById('edit-first-name').value.toUpperCase(),
            last_name: document.getElementById('edit-last-name').value.toUpperCase(),
            dob: document.getElementById('edit-dob').value,
            gender: document.getElementById('edit-gender').value,
            eye_color: document.getElementById('edit-eye-color').value,
            hair_color: document.getElementById('edit-hair-color').value,
        };

        try {
            var resp = await fetch('/api/faces/' + encodeURIComponent(item.filename) + '/profile', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ identity: updates })
            });
            var data = await resp.json();

            if (data.success) {
                this.showToast('Profile updated', false);
                this.cancelProfileEdit();
                this.loadPreviewProfile(item.filename);
                this.updateCardBadge(item.filename, updates.first_name + ' ' + updates.last_name);
            } else {
                this.showToast(data.error || 'Update failed', true);
            }
        } catch (e) {
            console.error('Profile save failed:', e);
            this.showToast('Failed to save profile', true);
        }
    },

    resetProfile: function() {
        var item = this.currentPreviewItem;
        if (!item) return;
        var self = this;

        this.showConfirm('Reset profile for ' + item.filename + '? A new identity will be assigned on next generation.', async function() {
            try {
                var resp = await fetch('/api/faces/' + encodeURIComponent(item.filename) + '/profile', {
                    method: 'DELETE'
                });
                var data = await resp.json();

                if (data.success) {
                    self.showToast('Profile reset', false);
                    self.loadPreviewProfile(item.filename);
                    self.updateCardBadge(item.filename, null);
                    self.loadStats();
                } else {
                    self.showToast(data.error || 'Reset failed', true);
                }
            } catch (e) {
                console.error('Profile reset failed:', e);
                self.showToast('Failed to reset profile', true);
            }
        });
    },

    updateCardBadge: function(filename, name) {
        var cards = document.querySelectorAll('.face-card');
        cards.forEach(function(card) {
            if (card.dataset.itemId === filename) {
                var existing = card.querySelector('.card-profile-badge');
                if (name) {
                    if (existing) {
                        existing.textContent = name;
                    } else {
                        var badge = document.createElement('div');
                        badge.className = 'card-profile-badge';
                        badge.textContent = name;
                        card.querySelector('.card-thumb').appendChild(badge);
                    }
                } else {
                    if (existing) existing.remove();
                }
            }
        });
    },

    // ── Upload Modal ───────────────────────────────────────────────────────
    showUploadModal: function() {
        var modal = document.getElementById('upload-modal');
        document.getElementById('upload-file-list').textContent = '';
        document.getElementById('file-input').value = '';
        document.getElementById('btn-upload-start').disabled = true;
        document.getElementById('upload-btn-label').textContent = 'Upload';
        modal.style.display = 'flex';
    },

    hideUploadModal: function() {
        document.getElementById('upload-modal').style.display = 'none';
    },

    addFilesToUploadList: function(files) {
        var list = document.getElementById('upload-file-list');
        var btn = document.getElementById('btn-upload-start');

        for (var i = 0; i < files.length; i++) {
            var file = files[i];
            if (!file.type.startsWith('image/')) continue;

            var item = document.createElement('div');
            item.className = 'file-item';
            item.dataset.filename = file.name;

            var nameEl = document.createElement('span');
            nameEl.className = 'file-item-name';
            nameEl.textContent = file.name;

            var sizeEl = document.createElement('span');
            sizeEl.className = 'file-item-size';
            sizeEl.textContent = this.formatBytes(file.size);

            var statusEl = document.createElement('span');
            statusEl.className = 'file-item-status pending';
            statusEl.textContent = 'Ready';

            item.appendChild(nameEl);
            item.appendChild(sizeEl);
            item.appendChild(statusEl);
            list.appendChild(item);
        }

        if (list.children.length > 0) {
            btn.disabled = false;
        }
    },

    uploadFiles: async function() {
        var fileInput = document.getElementById('file-input');
        var files = fileInput.files;
        if (!files || files.length === 0) return;

        var btn = document.getElementById('btn-upload-start');
        var btnLabel = document.getElementById('upload-btn-label');
        btn.disabled = true;
        btnLabel.textContent = 'Uploading...';

        var list = document.getElementById('upload-file-list');
        var items = list.querySelectorAll('.file-item');
        var successCount = 0;
        var failCount = 0;

        for (var i = 0; i < files.length; i++) {
            var file = files[i];
            var fileItem = items[i];
            if (!fileItem) continue;
            var statusEl = fileItem.querySelector('.file-item-status');

            statusEl.className = 'file-item-status uploading';
            statusEl.textContent = 'Uploading';

            var formData = new FormData();
            formData.append('file', file);

            try {
                var resp = await fetch('/api/faces/upload', {
                    method: 'POST',
                    body: formData
                });
                var data = await resp.json();

                if (data.success) {
                    statusEl.className = 'file-item-status done';
                    statusEl.textContent = 'Done';
                    successCount++;
                } else {
                    statusEl.className = 'file-item-status error';
                    statusEl.textContent = data.error || 'Error';
                    failCount++;
                }
            } catch (e) {
                statusEl.className = 'file-item-status error';
                statusEl.textContent = 'Failed';
                failCount++;
            }
        }

        btnLabel.textContent = 'Done';

        if (successCount > 0) {
            this.showToast(successCount + ' photo' + (successCount !== 1 ? 's' : '') + ' uploaded', false);
            var self = this;
            setTimeout(function() {
                self.hideUploadModal();
                self.resetAndReload();
                self.loadStats();
            }, 800);
        } else {
            this.showToast('Upload failed', true);
            btn.disabled = false;
            btnLabel.textContent = 'Retry';
        }
    },

    // ── Delete ─────────────────────────────────────────────────────────────
    deleteSelected: async function() {
        var ids = Array.from(this.selectedItems);
        if (ids.length === 0) return;

        var self = this;
        var noun = ids.length !== 1 ? 'photos' : 'photo';
        this.showConfirm(
            'Delete ' + ids.length.toLocaleString() + ' ' + noun + '? This cannot be undone.',
            async function() {
                try {
                    var resp = await fetch('/api/faces/delete', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ ids: ids })
                    });
                    var data = await resp.json();

                    if (data.success) {
                        var idSet = new Set(ids);
                        document.querySelectorAll('.face-card').forEach(function(card) {
                            if (idSet.has(card.dataset.itemId)) card.remove();
                        });
                        self.items = self.items.filter(function(item) {
                            return !idSet.has(item.id);
                        });
                        self.selectedItems.clear();
                        self.updateActionBar();
                        self.loadStats();
                        self.showToast(
                            (data.deleted || ids.length) + ' ' + noun + ' deleted', false
                        );
                    } else {
                        self.showToast(data.error || 'Delete failed', true);
                    }
                } catch (e) {
                    console.error('Delete failed:', e);
                    self.showToast('Delete failed \u2014 check connection', true);
                }
            }
        );
    },

    // ── Preprocess ────────────────────────────────────────────────────────
    triggerPreprocess: async function() {
        var btn = document.getElementById('btn-preprocess');
        btn.disabled = true;

        try {
            var resp = await fetch('/api/faces/preprocess', { method: 'POST' });
            var data = await resp.json();

            if (data.success) {
                this.showPreprocessBanner('Preprocessing faces...');
                this.pollPreprocessStatus();
            } else {
                this.showToast(data.error || 'Preprocess failed', true);
                btn.disabled = false;
            }
        } catch (e) {
            console.error('Preprocess trigger failed:', e);
            this.showToast('Failed to start preprocessing', true);
            btn.disabled = false;
        }
    },

    pollPreprocessStatus: function() {
        var self = this;
        if (this.preprocessPollTimer) clearInterval(this.preprocessPollTimer);

        this.preprocessPollTimer = setInterval(async function() {
            try {
                var resp = await fetch('/api/faces/preprocess/status');
                var data = await resp.json();

                if (data.running) {
                    var msg = 'Preprocessing';
                    if (data.progress != null && data.total != null) {
                        msg += ' \u2014 ' + data.progress + ' / ' + data.total;
                    }
                    self.showPreprocessBanner(msg);
                } else {
                    clearInterval(self.preprocessPollTimer);
                    self.preprocessPollTimer = null;
                    self.hidePreprocessBanner();
                    document.getElementById('btn-preprocess').disabled = false;
                    self.showToast('Preprocessing complete', false);
                    self.resetAndReload();
                    self.loadStats();
                }
            } catch (e) {
                console.error('Preprocess status poll failed:', e);
            }
        }, 2000);
    },

    showPreprocessBanner: function(msg) {
        var banner = document.getElementById('preprocess-banner');
        document.getElementById('preprocess-status-text').textContent = msg;
        banner.style.display = 'block';
    },

    hidePreprocessBanner: function() {
        document.getElementById('preprocess-banner').style.display = 'none';
    },

    // ── Gender Detection ─────────────────────────────────────────────────
    triggerGenderDetection: async function() {
        var btn = document.getElementById('btn-detect-gender');
        btn.disabled = true;

        try {
            var resp = await fetch('/api/faces/detect-gender', { method: 'POST' });
            var data = await resp.json();

            if (data.success) {
                if (data.count === 0) {
                    this.showToast('All faces already have gender metadata', false);
                    btn.disabled = false;
                } else {
                    this.showGenderBanner('Detecting gender...');
                    this.pollGenderStatus();
                }
            } else {
                this.showToast(data.error || 'Gender detection failed', true);
                btn.disabled = false;
            }
        } catch (e) {
            console.error('Gender detection trigger failed:', e);
            this.showToast('Failed to start gender detection', true);
            btn.disabled = false;
        }
    },

    pollGenderStatus: function() {
        var self = this;
        if (this.genderPollTimer) clearInterval(this.genderPollTimer);

        this.genderPollTimer = setInterval(async function() {
            try {
                var resp = await fetch('/api/faces/detect-gender/status');
                var data = await resp.json();

                if (data.running) {
                    var msg = 'Detecting gender';
                    if (data.progress != null && data.total != null) {
                        msg += ' \u2014 ' + data.progress + ' / ' + data.total;
                    }
                    self.showGenderBanner(msg);
                } else {
                    clearInterval(self.genderPollTimer);
                    self.genderPollTimer = null;
                    self.hideGenderBanner();
                    document.getElementById('btn-detect-gender').disabled = false;
                    self.showToast('Gender detection complete', false);
                    self.resetAndReload();
                }
            } catch (e) {
                console.error('Gender status poll failed:', e);
            }
        }, 2000);
    },

    showGenderBanner: function(msg) {
        var banner = document.getElementById('gender-banner');
        document.getElementById('gender-status-text').textContent = msg;
        banner.style.display = 'block';
    },

    hideGenderBanner: function() {
        document.getElementById('gender-banner').style.display = 'none';
    },

    // ── Infinite Scroll + Lazy Images ─────────────────────────────────────
    setupInfiniteScroll: function() {
        var self = this;

        this.imageObserver = new IntersectionObserver(function(entries) {
            entries.forEach(function(entry) {
                if (entry.isIntersecting) {
                    var img = entry.target;
                    if (img.dataset.src) {
                        img.src = img.dataset.src;
                        delete img.dataset.src;
                    }
                    self.imageObserver.unobserve(img);
                }
            });
        }, { rootMargin: '200px' });

        this.scrollObserver = new IntersectionObserver(function(entries) {
            if (entries[0].isIntersecting && !self.loading && self.page < self.totalPages) {
                self.page++;
                self.loadPage();
            }
        }, { rootMargin: '400px' });

        var sentinel = document.getElementById('scroll-sentinel');
        if (sentinel) this.scrollObserver.observe(sentinel);
    },

    // ── Dialogs ────────────────────────────────────────────────────────────
    showConfirm: function(message, onConfirm) {
        var dialog = document.getElementById('confirm-dialog');
        document.getElementById('confirm-message').textContent = message;
        dialog.style.display = 'flex';

        var okBtn = document.getElementById('confirm-ok');
        var cancelBtn = document.getElementById('confirm-cancel');
        var newOk = okBtn.cloneNode(true);
        var newCancel = cancelBtn.cloneNode(true);
        okBtn.replaceWith(newOk);
        cancelBtn.replaceWith(newCancel);

        var close = function() { dialog.style.display = 'none'; };
        newOk.addEventListener('click', function() { close(); onConfirm(); });
        newCancel.addEventListener('click', close);
    },

    showToast: function(msg, isError) {
        var toast = document.createElement('div');
        toast.className = 'toast' + (isError ? ' toast-error' : ' toast-success');
        toast.textContent = msg;
        document.body.appendChild(toast);
        var t = toast;
        setTimeout(function() { if (t.parentNode) t.remove(); }, 3200);
    },

    // ── Utilities ─────────────────────────────────────────────────────────
    resetAndReload: function() {
        this.page = 1;
        this.items = [];
        this.selectedItems.clear();
        this.updateActionBar();

        var grid = document.getElementById('fpm-grid');
        grid.textContent = '';
        var loadingEl = document.createElement('div');
        loadingEl.className = 'loading-state';
        loadingEl.textContent = 'Loading face photos...';
        grid.appendChild(loadingEl);

        this.loadPage();
    },

    formatBytes: function(bytes) {
        if (bytes < 1024) return bytes + ' B';
        if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
        return (bytes / 1048576).toFixed(1) + ' MB';
    },

    // ── Event Binding ─────────────────────────────────────────────────────
    bindEvents: function() {
        var self = this;

        // Variant toggle
        document.querySelectorAll('#variant-toggle .toggle-btn').forEach(function(btn) {
            btn.addEventListener('click', function() {
                document.querySelectorAll('#variant-toggle .toggle-btn').forEach(function(b) {
                    b.classList.remove('active');
                });
                btn.classList.add('active');
                self.filters.variant = btn.dataset.variant;
                self.resetAndReload();
            });
        });

        // Upload button
        document.getElementById('btn-upload').addEventListener('click', function() {
            self.showUploadModal();
        });

        // Preprocess button
        document.getElementById('btn-preprocess').addEventListener('click', function() {
            self.triggerPreprocess();
        });

        // Gender detection button
        document.getElementById('btn-detect-gender').addEventListener('click', function() {
            self.triggerGenderDetection();
        });

        // Action bar
        document.getElementById('btn-select-all').addEventListener('click', function() {
            self.selectAll();
        });
        document.getElementById('btn-deselect-all').addEventListener('click', function() {
            self.deselectAll();
        });
        document.getElementById('btn-delete-selected').addEventListener('click', function() {
            self.deleteSelected();
        });

        // Preview modal close
        document.getElementById('preview-close').addEventListener('click', function() {
            self.hidePreview();
        });
        document.getElementById('preview-modal').addEventListener('click', function(e) {
            if (e.target === document.getElementById('preview-modal')) {
                self.hidePreview();
            }
        });

        // Upload modal
        document.getElementById('upload-close').addEventListener('click', function() {
            self.hideUploadModal();
        });
        document.getElementById('btn-upload-cancel').addEventListener('click', function() {
            self.hideUploadModal();
        });
        document.getElementById('upload-modal').addEventListener('click', function(e) {
            if (e.target === document.getElementById('upload-modal')) {
                self.hideUploadModal();
            }
        });

        // Browse files button
        document.getElementById('browse-btn').addEventListener('click', function(e) {
            e.stopPropagation();
            document.getElementById('file-input').click();
        });

        // Drop zone click
        document.getElementById('drop-zone').addEventListener('click', function() {
            document.getElementById('file-input').click();
        });

        // File input change
        document.getElementById('file-input').addEventListener('change', function(e) {
            self.addFilesToUploadList(e.target.files);
        });

        // Drag and drop
        var dropZone = document.getElementById('drop-zone');
        dropZone.addEventListener('dragover', function(e) {
            e.preventDefault();
            dropZone.classList.add('drag-over');
        });
        dropZone.addEventListener('dragleave', function(e) {
            if (!dropZone.contains(e.relatedTarget)) {
                dropZone.classList.remove('drag-over');
            }
        });
        dropZone.addEventListener('drop', function(e) {
            e.preventDefault();
            dropZone.classList.remove('drag-over');
            var files = e.dataTransfer.files;
            if (files.length) {
                // Sync dropped files into the hidden input
                var dt = new DataTransfer();
                for (var i = 0; i < files.length; i++) dt.items.add(files[i]);
                document.getElementById('file-input').files = dt.files;
                self.addFilesToUploadList(files);
            }
        });

        // Upload start
        document.getElementById('btn-upload-start').addEventListener('click', function() {
            self.uploadFiles();
        });

        // Profile panel buttons
        document.getElementById('btn-profile-edit').addEventListener('click', function() {
            self.showProfileEdit();
        });
        document.getElementById('btn-profile-reset').addEventListener('click', function() {
            self.resetProfile();
        });
        document.getElementById('btn-edit-cancel').addEventListener('click', function() {
            self.cancelProfileEdit();
        });
        document.getElementById('btn-edit-save').addEventListener('click', function() {
            self.saveProfileEdit();
        });

        // Keyboard shortcuts
        document.addEventListener('keydown', function(e) {
            if (e.key === 'Escape') {
                var previewModal = document.getElementById('preview-modal');
                var uploadModal  = document.getElementById('upload-modal');
                var confirmDialog = document.getElementById('confirm-dialog');
                if (previewModal.style.display !== 'none') {
                    self.hidePreview();
                } else if (uploadModal.style.display !== 'none') {
                    self.hideUploadModal();
                } else if (confirmDialog && confirmDialog.style.display !== 'none') {
                    confirmDialog.style.display = 'none';
                }
            }
        });
    }
};

document.addEventListener('DOMContentLoaded', function() {
    FacePhotoManager.init();
});
