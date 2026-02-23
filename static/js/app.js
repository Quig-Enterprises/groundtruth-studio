let allVideos = [];
let allTags = [];
let activeSource = 'all';
let activeLibrary = '';
let allLibraries = [];
let selectedVideoIds = new Set();
let multiSelectMode = false;
let showUnannotatedOnly = false;

// EcoEye state
let eeCurrentPage = 1;
let eeTotalPages = 1;
const eePageSize = 50;
let eeSelectedEvents = new Set();
let eeSelectedTags = new Set();
let eeAllEvents = [];
let eeAvailableTags = [];
let eeEventTagsMap = {};
let eeInitialized = false;

async function checkSystemStatus() {
    const statusEl = document.getElementById('status-indicator');
    if (!statusEl) return;
    try {
        const response = await fetch('/api/system/status');
        const data = await response.json();
        if (data.yt_dlp_installed && data.ffmpeg_installed) {
            statusEl.textContent = 'System Ready';
            statusEl.className = 'status-ok';
        } else {
            let missing = [];
            if (!data.yt_dlp_installed) missing.push('yt-dlp');
            if (!data.ffmpeg_installed) missing.push('FFmpeg');
            statusEl.textContent = `Missing: ${missing.join(', ')}`;
            statusEl.className = 'status-error';
        }
    } catch (error) {
        const statusEl = document.getElementById('status-indicator');
        statusEl.textContent = 'System Error';
        statusEl.className = 'status-error';
    }
}

async function loadVideos() {
    const grid = document.getElementById('videos-grid');
    if (!grid) return;
    grid.textContent = '';
    const loadingDiv = document.createElement('div');
    loadingDiv.className = 'loading';
    loadingDiv.textContent = 'Loading videos...';
    grid.appendChild(loadingDiv);

    try {
        let url = '/api/videos';
        if (activeLibrary) url += `?library=${activeLibrary}`;
        const response = await fetch(url);
        const data = await response.json();

        if (data.success) {
            allVideos = data.videos;
            filterBySource(activeSource);
        }
    } catch (error) {
        grid.innerHTML = '<div class="loading">Error loading videos</div>';
    }
}

async function loadLibraries() {
    try {
        const response = await fetch('/api/libraries');
        const data = await response.json();
        if (data.success) {
            allLibraries = data.libraries;
            const select = document.getElementById('library-select');
            if (!select) return;
            const currentValue = select.value;
            select.innerHTML = '<option value="">All Libraries</option>';
            data.libraries.forEach(lib => {
                const opt = document.createElement('option');
                opt.value = lib.id;
                opt.textContent = `${lib.name} (${lib.item_count})`;
                select.appendChild(opt);
            });
            select.value = currentValue;
            // Also populate bulk assign dropdown
            const bulkSelect = document.getElementById('bulk-library-select');
            if (bulkSelect) {
                bulkSelect.innerHTML = '<option value="">Choose library...</option>';
                data.libraries.filter(l => !l.is_default).forEach(lib => {
                    const opt = document.createElement('option');
                    opt.value = lib.id;
                    opt.textContent = lib.name;
                    bulkSelect.appendChild(opt);
                });
            }
        }
    } catch (e) {
        console.error('Error loading libraries:', e);
    }
}

function filterByLibrary(libraryId) {
    activeLibrary = libraryId;
    loadVideos();
}

function displayVideos(videos) {
    const grid = document.getElementById('videos-grid');

    if (videos.length === 0) {
        grid.innerHTML = '<div class="loading">No videos found</div>';
        return;
    }

    grid.innerHTML = videos.map(video => {
        // Determine card type based on source
        const isEcoEye = video.is_ecoeye_import;
        const hasVideoFile = video.has_video_file;

        // Build thumbnail HTML with bbox data for overlay
        let thumbnailHtml = '';
        const bboxAttr = video.bboxes && video.bboxes.length > 0
            ? ` data-bboxes='${JSON.stringify(video.bboxes).replace(/'/g, "&#39;")}'`
            : '';
        const dimsAttr = video.width && video.height
            ? ` data-vw="${video.width}" data-vh="${video.height}"`
            : '';
        if (video.thumbnail_path) {
            const thumbName = video.thumbnail_path.split('/').pop();
            thumbnailHtml = `<div class="thumbnail-container"${bboxAttr}${dimsAttr}><img src="/thumbnails/${encodeURIComponent(thumbName)}" class="video-thumbnail" alt="${escapeHtml(video.title)}" loading="lazy"></div>`;
        } else {
            thumbnailHtml = '<div class="thumbnail-container"><div class="video-thumbnail"></div></div>';
        }

        // Build source badge
        let sourceBadge = '';
        if (isEcoEye) {
            if (hasVideoFile) {
                sourceBadge = '<div class="source-badge ecoeye-video" title="EcoEye Alert - Video Available">👁️ EcoEye</div>';
            } else {
                sourceBadge = '<div class="source-badge ecoeye-meta" title="EcoEye Alert - Metadata Only">👁️ Meta Only</div>';
            }
        }

        // Build video meta info
        let metaInfo = '';
        if (isEcoEye && !hasVideoFile) {
            // Metadata-only EcoEye import - show different info
            metaInfo = `
                <div class="video-meta" style="color: #3498db;">
                    📋 Metadata imported from EcoEye
                    ${video.ecoeye_camera ? `<br>📹 ${escapeHtml(video.ecoeye_camera)}` : ''}
                </div>
            `;
        } else {
            // Regular video or EcoEye with video
            metaInfo = `
                <div class="video-meta">
                    ${formatDuration(video.duration)} |
                    ${video.width && video.height ? `${video.width}x${video.height}` : 'Unknown resolution'} |
                    ${formatFileSize(video.file_size)}
                </div>
            `;
        }

        // Build action button
        let actionButton = '';
        if (hasVideoFile) {
            actionButton = `
                <button onclick="openAnnotate(${video.id})" class="btn-primary" style="width: 100%; font-size: 13px;">
                    Annotate Video
                </button>
            `;
        } else if (isEcoEye) {
            actionButton = `
                <div style="display: flex; gap: 6px;">
                    <button onclick="showVideoDetails(${video.id})" class="btn-secondary" style="flex: 1; font-size: 13px;">
                        View Details
                    </button>
                    ${video.thumbnail_path ? `
                        <button onclick="openAnnotate(${video.id})" class="btn-primary" style="flex: 1; font-size: 13px;">
                            Annotate
                        </button>
                    ` : ''}
                </div>
            `;
        } else {
            actionButton = `
                <button onclick="openAnnotate(${video.id})" class="btn-primary" style="width: 100%; font-size: 13px;">
                    Annotate Video
                </button>
            `;
        }

        // Build card classes
        const cardClasses = ['video-card'];
        if (isEcoEye) cardClasses.push('ecoeye-import');
        if (!hasVideoFile) cardClasses.push('metadata-only');

        return `
            <div class="${cardClasses.join(' ')}" data-video-id="${video.id}" onclick="toggleVideoSelect(${video.id}, this, event)">
                <div onclick="if(!multiSelectMode) showVideoDetails(${video.id})" style="cursor: pointer; position: relative;">
                    ${thumbnailHtml}
                    <div class="annotation-badge ${video.annotation_count > 0 ? 'has-annotations' : 'no-annotations'}">
                        ${video.annotation_count || 0}
                    </div>
                    ${sourceBadge}
                    <div class="video-info">
                        <div class="video-title">${escapeHtml(video.title || video.filename)}</div>
                        ${metaInfo}
                        ${video.tags ? `
                            <div class="video-tags">
                                ${video.tags.split(', ').map(tag =>
                                    `<span class="video-tag">${escapeHtml(tag)}</span>`
                                ).join('')}
                            </div>
                        ` : ''}
                        ${video.libraries && video.libraries.length > 0 ? `
                            <div class="library-badges">
                                ${video.libraries.map(lib =>
                                    `<span class="library-badge">${escapeHtml(lib.name)}</span>`
                                ).join('')}
                            </div>
                        ` : ''}
                    </div>
                </div>
                <div style="padding: 10px; border-top: 1px solid #ecf0f1;">
                    ${actionButton}
                </div>
            </div>
        `;
    }).join('');

    // Render bbox overlays after thumbnails load
    renderBboxOverlays();
}

function renderBboxOverlays() {
    document.querySelectorAll('.thumbnail-container[data-bboxes]').forEach(container => {
        const img = container.querySelector('img.video-thumbnail');
        if (!img) return;

        const bboxes = JSON.parse(container.dataset.bboxes);
        if (!bboxes.length) return;

        function createOverlay() {
            // Use video dimensions if available, else image natural dimensions
            const vw = container.dataset.vw ? parseInt(container.dataset.vw) : img.naturalWidth;
            const vh = container.dataset.vh ? parseInt(container.dataset.vh) : img.naturalHeight;
            if (!vw || !vh) return;

            const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
            svg.setAttribute('class', 'bbox-overlay');
            svg.setAttribute('viewBox', `0 0 ${vw} ${vh}`);
            svg.setAttribute('preserveAspectRatio', 'xMidYMid slice');

            bboxes.forEach(b => {
                const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
                rect.setAttribute('x', b.x);
                rect.setAttribute('y', b.y);
                rect.setAttribute('width', b.w);
                rect.setAttribute('height', b.h);
                rect.setAttribute('class', b.reviewed ? 'bbox-validated' : 'bbox-unvalidated');
                rect.setAttribute('vector-effect', 'non-scaling-stroke');
                svg.appendChild(rect);
            });

            container.appendChild(svg);
        }

        if (img.complete && img.naturalWidth > 0) {
            createOverlay();
        } else {
            img.addEventListener('load', createOverlay, { once: true });
        }
    });
}

function filterBySource(source) {
    activeSource = source;

    // Update tab active state
    document.querySelectorAll('.source-tab').forEach(tab => {
        tab.classList.toggle('active', tab.dataset.source === source);
    });

    const searchPanel = document.querySelector('.search-panel');
    const eeControls = document.getElementById('ecoeye-controls');
    const eeToolbar = document.getElementById('ecoeye-toolbar');
    const countEl = document.getElementById('source-tab-count');

    if (source === 'ecoeye') {
        // Show EcoEye UI, hide search panel
        searchPanel.style.display = 'none';
        eeControls.style.display = '';
        eeToolbar.style.display = '';

        // Initialize EcoEye data on first visit
        if (!eeInitialized) {
            eeInitialized = true;
            eeInit();
        } else {
            eeLoadEvents();
        }

        if (countEl) countEl.textContent = '';
    } else {
        // Show search panel, hide EcoEye UI
        searchPanel.style.display = '';
        eeControls.style.display = 'none';
        eeToolbar.style.display = 'none';

        // Filter videos
        let filtered;
        if (source === 'manual') {
            filtered = allVideos.filter(v => !v.is_ecoeye_import);
        } else {
            filtered = allVideos;
        }

        if (showUnannotatedOnly) {
            filtered = filtered.filter(v => (v.annotation_count || 0) === 0);
        }

        if (countEl) {
            countEl.textContent = `Showing ${filtered.length} of ${allVideos.length}`;
        }

        displayVideos(filtered);
    }
}

function toggleUnannotatedFilter(checked) {
    showUnannotatedOnly = checked;
    filterBySource(activeSource);
}

// Old tag system functions removed - now using annotation-based workflow

async function previewVideo() {
    const url = document.getElementById('video-url').value.trim();
    const previewDiv = document.getElementById('preview-info');

    if (!url) {
        alert('Please enter a URL');
        return;
    }

    previewDiv.innerHTML = '<p>Loading preview...</p>';
    previewDiv.style.display = 'block';

    try {
        const response = await fetch('/api/video-info', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({url})
        });

        const data = await response.json();

        if (data.success) {
            const info = data.info;
            previewDiv.innerHTML = `
                <h3>${escapeHtml(info.title)}</h3>
                <p><strong>Duration:</strong> ${formatDuration(info.duration)}</p>
                <p><strong>Resolution:</strong> ${info.width}x${info.height}</p>
                <p><strong>Size:</strong> ~${formatFileSize(info.file_size)}</p>
                <p><strong>Uploader:</strong> ${escapeHtml(info.uploader || 'Unknown')}</p>
                ${info.description ? `<p><strong>Description:</strong> ${escapeHtml(info.description)}</p>` : ''}
            `;
        } else {
            previewDiv.innerHTML = `<p style="color: #e74c3c;">Error: ${escapeHtml(data.error)}</p>`;
        }
    } catch (error) {
        previewDiv.innerHTML = `<p style="color: #e74c3c;">Error: ${error.message}</p>`;
    }
}

async function downloadVideo() {
    const url = document.getElementById('video-url').value.trim();
    const statusDiv = document.getElementById('download-status');

    if (!url) {
        alert('Please enter a URL');
        return;
    }

    statusDiv.innerHTML = 'Downloading video... This may take several minutes.';
    statusDiv.className = 'status-message';

    try {
        const response = await fetch('/api/download', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({url})
        });

        const data = await response.json();

        if (data.success && data.queued) {
            statusDiv.innerHTML = `Video added to download queue (position ${data.queue_position}). Download will process in background.`;
            statusDiv.className = 'status-message success';
            document.getElementById('video-url').value = '';
            document.getElementById('preview-info').style.display = 'none';
        } else if (data.duplicate) {
            statusDiv.innerHTML = `Video already downloaded (Video ID: ${data.video_id})`;
            statusDiv.className = 'status-message error';
        } else if (data.in_progress) {
            statusDiv.innerHTML = `Video is already ${data.status}`;
            statusDiv.className = 'status-message error';
        } else {
            statusDiv.innerHTML = `Error: ${escapeHtml(data.error || data.message || 'Unknown error')}`;
            statusDiv.className = 'status-message error';
        }
    } catch (error) {
        statusDiv.innerHTML = `Error: ${error.message}`;
        statusDiv.className = 'status-message error';
    }
}

// Upload modal functions
function openUploadModal() {
    document.getElementById('upload-modal').style.display = 'block';
    document.getElementById('upload-status').innerHTML = '';
    document.getElementById('upload-progress').innerHTML = '';
}

function closeUploadModal() {
    document.getElementById('upload-modal').style.display = 'none';
    document.getElementById('video-files').value = '';
    document.getElementById('upload-notes').value = '';
    document.getElementById('file-list').innerHTML = '';
}

// Show selected files
const _videoFilesInput = document.getElementById('video-files');
if (_videoFilesInput) _videoFilesInput.addEventListener('change', (e) => {
    const fileList = document.getElementById('file-list');
    const files = e.target.files;

    if (files.length === 0) {
        fileList.innerHTML = '';
        return;
    }

    fileList.innerHTML = `<strong>Selected ${files.length} file(s):</strong><br>` +
        Array.from(files).map(f => `• ${escapeHtml(f.name)} (${formatFileSize(f.size)})`).join('<br>');
});

// Handle multiple file uploads
const _uploadForm = document.getElementById('upload-form');
if (_uploadForm) _uploadForm.addEventListener('submit', async (e) => {
    e.preventDefault();

    const fileInput = document.getElementById('video-files');
    const notesInput = document.getElementById('upload-notes');
    const statusDiv = document.getElementById('upload-status');
    const progressDiv = document.getElementById('upload-progress');

    if (!fileInput.files || fileInput.files.length === 0) {
        alert('Please select at least one file');
        return;
    }

    const files = Array.from(fileInput.files);
    const notes = notesInput.value;

    statusDiv.innerHTML = `Uploading ${files.length} file(s)...`;
    statusDiv.className = 'status-message';
    progressDiv.innerHTML = '';

    let successCount = 0;
    let errorCount = 0;

    for (let i = 0; i < files.length; i++) {
        const file = files[i];
        progressDiv.innerHTML = `Processing ${i + 1} of ${files.length}: ${escapeHtml(file.name)}...`;

        const formData = new FormData();
        formData.append('file', file);
        formData.append('notes', notes);

        try {
            const response = await fetch('/api/upload', {
                method: 'POST',
                body: formData
            });

            const data = await response.json();

            if (data.success) {
                successCount++;
            } else {
                errorCount++;
                console.error(`Failed to upload ${file.name}:`, data.error);
            }
        } catch (error) {
            errorCount++;
            console.error(`Error uploading ${file.name}:`, error);
        }
    }

    progressDiv.innerHTML = '';

    if (errorCount === 0) {
        statusDiv.innerHTML = `Success! Uploaded ${successCount} video(s).`;
        statusDiv.className = 'status-message success';

        setTimeout(() => {
            closeUploadModal();
            loadVideos();
        }, 2000);
    } else {
        statusDiv.innerHTML = `Uploaded ${successCount} video(s). ${errorCount} failed (check console for details).`;
        statusDiv.className = 'status-message error';
    }
});

async function searchVideos() {
    const query = document.getElementById('search-input').value.trim();
    const grid = document.getElementById('videos-grid');

    grid.innerHTML = '<div class="loading">Searching...</div>';

    try {
        let searchUrl = `/api/videos?search=${encodeURIComponent(query)}`;
        if (activeLibrary) searchUrl += `&library=${activeLibrary}`;
        const response = await fetch(searchUrl);
        const data = await response.json();

        if (data.success) {
            allVideos = data.videos;
            filterBySource(activeSource);
        }
    } catch (error) {
        grid.innerHTML = '<div class="loading">Error searching videos</div>';
    }
}

function searchByTag(tagName) {
    document.getElementById('search-input').value = tagName;
    searchVideos();
}

async function showVideoDetails(videoId) {
    try {
        const response = await fetch(`/api/videos/${videoId}`);
        const data = await response.json();

        if (!data.success) {
            alert('Error loading video details');
            return;
        }

        const video = data.video;
        const behaviors = data.behaviors || [];
        const isEcoEye = video.original_url && video.original_url.startsWith('ecoeye://');
        const hasVideoFile = video.filename && !video.filename.endsWith('.placeholder');

        const modalBody = document.getElementById('modal-body');
        const videoTags = video.tags ? video.tags.split(', ') : [];

        // Build video player or placeholder
        let videoPlayerHtml = '';
        if (hasVideoFile) {
            videoPlayerHtml = `<video controls class="modal-video" src="/downloads/${video.filename}"></video>`;
        } else if (video.thumbnail_path) {
            const thumbName = video.thumbnail_path.split('/').pop();
            videoPlayerHtml = `
                <div class="modal-video" style="background: #2c3e50; display: flex; align-items: center; justify-content: center; flex-direction: column; min-height: 300px;">
                    <img src="/thumbnails/${encodeURIComponent(thumbName)}" style="max-width: 100%; max-height: 250px; border-radius: 4px;">
                    <div style="color: white; margin-top: 15px; font-size: 14px;">
                        📋 Metadata only - no video file
                    </div>
                </div>
            `;
        } else {
            videoPlayerHtml = `
                <div class="modal-video" style="background: #2c3e50; display: flex; align-items: center; justify-content: center; min-height: 300px; color: white;">
                    📋 Metadata only - no video or thumbnail
                </div>
            `;
        }

        // Build source info for EcoEye imports
        let sourceInfo = '';
        if (isEcoEye) {
            const eventId = video.original_url.replace('ecoeye://', '');
            sourceInfo = `
                <div class="modal-section" style="background: #eaf2f8; padding: 15px; border-radius: 4px; margin-bottom: 15px;">
                    <h4 style="margin: 0 0 10px 0; color: #2980b9;">👁️ EcoEye Import</h4>
                    <p style="margin: 0; font-size: 14px;">
                        <strong>Event ID:</strong> ${escapeHtml(eventId)}<br>
                        <strong>Status:</strong> ${hasVideoFile ? '✅ Video downloaded' : '📋 Metadata only'}
                    </p>
                    ${!hasVideoFile ? `
                        <button onclick="requestEcoEyeVideo('${eventId}')" class="btn-warning" style="margin-top: 10px;">
                            Request Video Download
                        </button>
                    ` : ''}
                </div>
            `;
        }

        modalBody.innerHTML = `
            ${videoPlayerHtml}

            ${sourceInfo}

            <div class="modal-section">
                <h3>${escapeHtml(video.title || video.filename)}</h3>
                ${hasVideoFile ? `
                    <p><strong>Duration:</strong> ${formatDuration(video.duration)}</p>
                    <p><strong>Resolution:</strong> ${video.width}x${video.height}</p>
                    <p><strong>File Size:</strong> ${formatFileSize(video.file_size)}</p>
                ` : ''}
                <p><strong>Uploaded:</strong> ${new Date(video.upload_date).toLocaleString()}</p>
                ${video.original_url && !isEcoEye ? `<p><strong>Source:</strong> <a href="${video.original_url}" target="_blank">${video.original_url}</a></p>` : ''}
                ${video.notes ? `<p><strong>Notes:</strong> ${escapeHtml(video.notes)}</p>` : ''}
            </div>

            <div class="modal-section">
                <h3>Tags</h3>
                <div class="video-tags">
                    ${videoTags.map(tag => `
                        <span class="video-tag">
                            ${escapeHtml(tag)}
                            <span onclick="removeTag(${videoId}, '${escapeHtml(tag)}')" style="cursor: pointer; margin-left: 4px; color: #e74c3c;">&times;</span>
                        </span>
                    `).join('')}
                </div>
                <div class="tag-input-group">
                    <input type="text" id="new-tag-input" placeholder="Add new tag" />
                    <button onclick="addTag(${videoId})" class="btn-primary">Add Tag</button>
                </div>
            </div>

            <div class="modal-section">
                <h3>Libraries</h3>
                <div class="library-checklist" id="video-library-checklist" data-video-id="${videoId}"></div>
            </div>

            <div class="modal-section">
                <h3>Behaviors (${behaviors.length})</h3>
                ${behaviors.length > 0 ? `
                    <div>
                        ${behaviors.map(b => `
                            <div style="background: #ecf0f1; padding: 10px; margin-bottom: 8px; border-radius: 4px;">
                                <strong>${escapeHtml(b.behavior_type)}</strong>
                                ${b.start_time ? ` | ${formatDuration(b.start_time)} - ${formatDuration(b.end_time)}` : ''}
                                ${b.confidence ? ` | Confidence: ${(b.confidence * 100).toFixed(1)}%` : ''}
                                ${b.notes ? `<br><small>${escapeHtml(b.notes)}</small>` : ''}
                            </div>
                        `).join('')}
                    </div>
                ` : '<p>No behavior annotations yet</p>'}
            </div>

            <div class="modal-section" style="display: flex; gap: 10px;">
                ${hasVideoFile ? `<button onclick="openAnnotate(${videoId})" class="btn-primary">Open Annotator</button>` : ''}
                <button onclick="deleteVideo(${videoId})" class="btn-danger">Delete</button>
            </div>
        `;

        // Add floating Annotate button if video or thumbnail exists
        const canAnnotate = hasVideoFile || video.thumbnail_path;
        if (canAnnotate) {
            const annotateBtn = document.createElement('button');
            annotateBtn.className = 'modal-annotate-btn';
            annotateBtn.textContent = 'Annotate';
            annotateBtn.addEventListener('click', function() { openAnnotate(videoId); });
            modalBody.insertBefore(annotateBtn, modalBody.firstChild);
        }

        document.getElementById('video-modal').style.display = 'flex';

        // Populate library checkboxes
        loadVideoLibraryChecklist(videoId);
    } catch (error) {
        alert('Error loading video: ' + error.message);
    }
}

// Request video download from EcoEye
async function requestEcoEyeVideo(eventId) {
    if (!confirm('Request video download from EcoEye relay?')) {
        return;
    }

    try {
        const response = await fetch('/api/ecoeye/request-download', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ event_id: eventId })
        });

        const data = await response.json();

        if (data.success) {
            alert('Download request sent! The video will be available soon.');
        } else {
            alert('Failed to request download: ' + (data.error || 'Unknown error'));
        }
    } catch (error) {
        alert('Error: ' + error.message);
    }
}

async function addTag(videoId) {
    const input = document.getElementById('new-tag-input');
    const tag = input.value.trim();

    if (!tag) return;

    try {
        const response = await fetch(`/api/videos/${videoId}/tags`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({tag})
        });

        const data = await response.json();

        if (data.success) {
            input.value = '';
            showVideoDetails(videoId);
        } else {
            alert('Failed to add tag');
        }
    } catch (error) {
        alert('Error: ' + error.message);
    }
}

async function removeTag(videoId, tagName) {
    try {
        const response = await fetch(`/api/videos/${videoId}/tags/${encodeURIComponent(tagName)}`, {
            method: 'DELETE'
        });

        const data = await response.json();

        if (data.success) {
            showVideoDetails(videoId);
        }
    } catch (error) {
        alert('Error: ' + error.message);
    }
}

async function deleteVideo(videoId) {
    if (!confirm('Are you sure you want to delete this video? This cannot be undone.')) {
        return;
    }

    try {
        const response = await fetch(`/api/videos/${videoId}`, {
            method: 'DELETE'
        });

        const data = await response.json();

        if (data.success) {
            closeModal();
            loadVideos();
        } else {
            alert('Failed to delete video');
        }
    } catch (error) {
        alert('Error: ' + error.message);
    }
}

function closeModal() {
    document.getElementById('video-modal').style.display = 'none';
}

function formatDuration(seconds) {
    if (!seconds) return 'Unknown';
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    return `${mins}:${secs.toString().padStart(2, '0')}`;
}

function formatFileSize(bytes) {
    if (!bytes) return 'Unknown';
    const mb = bytes / (1024 * 1024);
    if (mb < 1024) return `${mb.toFixed(1)} MB`;
    return `${(mb / 1024).toFixed(2)} GB`;
}

function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function openAnnotate(videoId) {
    let url = `/annotate?id=${videoId}`;
    if (activeLibrary) url += `&library=${activeLibrary}`;
    window.location.href = url;
}

document.getElementById('search-input').addEventListener('keypress', (e) => {
    if (e.key === 'Enter') {
        searchVideos();
    }
});

window.onclick = function(event) {
    const videoModal = document.getElementById('video-modal');
    const uploadModal = document.getElementById('upload-modal');
    const eeTagModal = document.getElementById('ee-tag-manager-modal');
    const eeVidModal = document.getElementById('ee-video-modal');
    const libModal = document.getElementById('library-manager-modal');

    if (event.target === videoModal) {
        closeModal();
    }
    if (event.target === uploadModal) {
        closeUploadModal();
    }
    if (event.target === eeTagModal) {
        eeCloseTagManager();
    }
    if (event.target === eeVidModal) {
        eeCloseVideoPlayer();
    }
    if (event.target === libModal) {
        closeLibraryManager();
    }
}

// ==========================================
// EcoEye Events Tab
// ==========================================

function eeFetchWithTimeout(url, options, timeoutMs) {
    timeoutMs = timeoutMs || 20000;
    var controller = new AbortController();
    var timer = setTimeout(function() { controller.abort(); }, timeoutMs);
    var opts = Object.assign({}, options || {}, { signal: controller.signal });
    return fetch(url, opts).finally(function() { clearTimeout(timer); });
}

async function eeInit() {
    const grid = document.getElementById('videos-grid');
    grid.textContent = '';
    var loadingDiv = document.createElement('div');
    loadingDiv.className = 'loading';
    loadingDiv.textContent = 'Connecting to EcoEye...';
    grid.appendChild(loadingDiv);

    try {
        await Promise.all([eeLoadTags(), eeLoadCameras(), eeLoadSites()]);
    } catch (e) {
        console.error('EcoEye init partial failure:', e);
    }
    eeLoadEvents();
}

// --- Tags ---

async function eeLoadTags() {
    try {
        const response = await eeFetchWithTimeout('/api/ecoeye/tags');
        const data = await response.json();
        if (data.success) {
            eeAvailableTags = data.tags || [];
            eeRenderTagPills();
            eePopulateTagFilter();
        }
    } catch (error) {
        console.error('Failed to load EcoEye tags:', error);
    }
}

function eePopulateTagFilter() {
    const select = document.getElementById('ee-tag-filter');
    while (select.options.length > 2) select.remove(2);
    eeAvailableTags.forEach(tag => {
        const option = document.createElement('option');
        option.value = 'tag:' + tag.id;
        option.textContent = 'Tag: ' + tag.name + ' (' + (tag.usage_count || 0) + ')';
        select.appendChild(option);
    });
}

function eeRenderTagPills() {
    const container = document.getElementById('ee-tag-pills');
    const pills = eeAvailableTags.map(tag => {
        const pill = document.createElement('div');
        pill.className = 'ecoeye-tag-pill' + (eeSelectedTags.has(tag.id) ? ' selected' : '');
        pill.style.background = tag.color;
        pill.title = tag.description || tag.name;
        pill.onclick = function() { eeToggleTagSelection(tag.id); };
        pill.textContent = tag.name + ' ';
        const countSpan = document.createElement('span');
        countSpan.className = 'ee-tag-count';
        countSpan.textContent = tag.usage_count || 0;
        pill.appendChild(countSpan);
        return pill;
    });
    container.replaceChildren(...pills);
}

function eeToggleTagSelection(tagId) {
    if (eeSelectedTags.has(tagId)) {
        eeSelectedTags.delete(tagId);
    } else {
        eeSelectedTags.add(tagId);
    }
    eeRenderTagPills();
    eeUpdateTagButtons();
}

function eeUpdateTagButtons() {
    const applyBtn = document.getElementById('ee-apply-tags-btn');
    const removeBtn = document.getElementById('ee-remove-tags-btn');
    const count = eeSelectedEvents.size;
    document.getElementById('ee-tag-apply-count').textContent = count;
    applyBtn.disabled = eeSelectedTags.size === 0 || count === 0;
    removeBtn.disabled = eeSelectedTags.size === 0 || count === 0;
}

async function eeApplySelectedTags() {
    if (eeSelectedTags.size === 0 || eeSelectedEvents.size === 0) {
        alert('Please select both events (checkboxes) and tags (colored pills) first.');
        return;
    }

    const eventIds = Array.from(eeSelectedEvents);
    const tagIds = Array.from(eeSelectedTags);

    // Auto-import unimported events
    const unimported = eventIds.filter(function(id) {
        const ev = eeAllEvents.find(function(e) { return (e.event_id || e.id) === id; });
        return ev && !ev.imported_to_studio && !ev.has_local_video;
    });

    if (unimported.length > 0) {
        eeShowLoading('Importing ' + unimported.length + ' event(s)...');
        for (const id of unimported) {
            try {
                await fetch('/api/ecoeye/sync-sample', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ event_id: id, include_video: false })
                });
            } catch (err) { console.error('Auto-import failed for', id, err); }
        }
    }

    eeShowLoading('Applying ' + tagIds.length + ' tag(s) to ' + eventIds.length + ' event(s)...');

    try {
        const response = await fetch('/api/ecoeye/tags/assign', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ event_ids: eventIds, tag_ids: tagIds })
        });
        const data = await response.json();
        eeHideLoading();

        if (data.success) {
            eeSelectedEvents.clear();
            eeSelectedTags.clear();
            eeUpdateSelectedCount();
            eeUpdateTagButtons();
            eeRenderTagPills();
            await eeLoadTags();
            await eeLoadEventTags();
            await eeLoadEvents();
        } else {
            alert('Failed to apply tags: ' + (data.error || 'Unknown error'));
        }
    } catch (error) {
        eeHideLoading();
        alert('Failed to apply tags: ' + error.message);
    }
}

async function eeRemoveSelectedTags() {
    if (eeSelectedTags.size === 0 || eeSelectedEvents.size === 0) return;
    if (!confirm('Remove ' + eeSelectedTags.size + ' tag(s) from ' + eeSelectedEvents.size + ' event(s)?')) return;

    eeShowLoading('Removing tags...');

    try {
        const response = await fetch('/api/ecoeye/tags/remove', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                event_ids: Array.from(eeSelectedEvents),
                tag_ids: Array.from(eeSelectedTags)
            })
        });
        const data = await response.json();
        eeHideLoading();

        if (data.success) {
            await eeLoadTags();
            await eeLoadEventTags();
            eeRenderEvents(eeAllEvents);
        } else {
            alert('Failed to remove tags: ' + (data.error || 'Unknown error'));
        }
    } catch (error) {
        eeHideLoading();
        alert('Failed to remove tags: ' + error.message);
    }
}

async function eeLoadEventTags() {
    if (eeAllEvents.length === 0) return;
    const eventIds = eeAllEvents.map(function(e) { return e.event_id || e.id; }).join(',');
    try {
        const response = await fetch('/api/ecoeye/tags?event_ids=' + encodeURIComponent(eventIds));
        const data = await response.json();
        if (data.success) {
            eeEventTagsMap = data.events || {};
        }
    } catch (error) {
        console.error('Failed to load event tags:', error);
    }
}

// --- Tag Manager ---

function eeOpenTagManager() {
    document.getElementById('ee-tag-manager-modal').style.display = 'flex';
    eeRenderTagManagerList();
}

function eeCloseTagManager() {
    document.getElementById('ee-tag-manager-modal').style.display = 'none';
}

function eeRenderTagManagerList() {
    const container = document.getElementById('ee-tag-manager-list');
    var items = eeAvailableTags.map(function(tag) {
        var item = document.createElement('div');
        item.className = 'ee-tag-list-item';

        var colorDiv = document.createElement('div');
        colorDiv.className = 'ee-tag-color';
        colorDiv.style.background = tag.color;
        item.appendChild(colorDiv);

        var nameSpan = document.createElement('span');
        nameSpan.className = 'ee-tag-name';
        nameSpan.textContent = tag.name;
        item.appendChild(nameSpan);

        var usageSpan = document.createElement('span');
        usageSpan.className = 'ee-tag-usage';
        usageSpan.textContent = (tag.usage_count || 0) + ' uses';
        item.appendChild(usageSpan);

        var delBtn = document.createElement('button');
        delBtn.className = 'btn-danger';
        delBtn.style.padding = '4px 8px';
        delBtn.style.fontSize = '12px';
        delBtn.textContent = 'Delete';
        delBtn.onclick = function() { eeDeleteTag(tag.id, tag.name); };
        item.appendChild(delBtn);

        return item;
    });
    container.replaceChildren.apply(container, items);
}

async function eeCreateTag() {
    const nameInput = document.getElementById('ee-new-tag-name');
    const colorInput = document.getElementById('ee-new-tag-color');
    const name = nameInput.value.trim();
    const color = colorInput.value;

    if (!name) { alert('Please enter a tag name'); return; }

    try {
        const response = await fetch('/api/ecoeye/tags', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: name, color: color })
        });
        const data = await response.json();
        if (data.success) {
            nameInput.value = '';
            await eeLoadTags();
            eeRenderTagManagerList();
        } else {
            alert('Failed to create tag: ' + (data.error || 'Unknown error'));
        }
    } catch (error) {
        alert('Failed to create tag: ' + error.message);
    }
}

async function eeDeleteTag(tagId, tagName) {
    if (!confirm('Delete tag "' + tagName + '"? This will remove it from all events.')) return;
    try {
        const response = await fetch('/api/ecoeye/tags/' + tagId, { method: 'DELETE' });
        const data = await response.json();
        if (data.success) {
            await eeLoadTags();
            eeRenderTagManagerList();
            await eeLoadEventTags();
            eeRenderEvents(eeAllEvents);
        } else {
            alert('Failed to delete tag: ' + (data.error || 'Unknown error'));
        }
    } catch (error) {
        alert('Failed to delete tag: ' + error.message);
    }
}

// --- Filters ---

async function eeLoadCameras() {
    try {
        const response = await eeFetchWithTimeout('/api/ecoeye/cameras');
        const data = await response.json();
        if (data.success && data.cameras) {
            const select = document.getElementById('ee-camera-filter');
            data.cameras.forEach(function(camera) {
                const option = document.createElement('option');
                option.value = camera;
                option.textContent = camera;
                select.appendChild(option);
            });
        }
    } catch (error) {
        console.error('Failed to load cameras:', error);
    }
}

async function eeLoadSites() {
    try {
        const response = await eeFetchWithTimeout('/api/ecoeye/sites');
        const data = await response.json();
        if (data.success && data.sites) {
            const select = document.getElementById('ee-site-filter');
            data.sites.forEach(function(site) {
                const option = document.createElement('option');
                option.value = site.name;
                option.textContent = site.name;
                select.appendChild(option);
            });
        }
    } catch (error) {
        console.error('Failed to load sites:', error);
    }
}

function eeSearchEvents() {
    eeCurrentPage = 1;
    eeLoadEvents();
}

function eeResetFilters() {
    document.getElementById('ee-camera-filter').value = '';
    document.getElementById('ee-type-filter').value = '';
    document.getElementById('ee-date-filter').value = '';
    document.getElementById('ee-sort-filter').value = 'desc';
    document.getElementById('ee-site-filter').value = '';
    document.getElementById('ee-tag-filter').value = '';
    eeCurrentPage = 1;
    eeLoadEvents();
}

// --- Load & Render Events ---

async function eeLoadEvents() {
    const grid = document.getElementById('videos-grid');
    grid.textContent = '';
    var loadingDiv = document.createElement('div');
    loadingDiv.className = 'loading';
    loadingDiv.textContent = 'Loading EcoEye events...';
    grid.appendChild(loadingDiv);

    const camera = document.getElementById('ee-camera-filter').value;
    const eventType = document.getElementById('ee-type-filter').value;
    const dateValue = document.getElementById('ee-date-filter').value;
    const sortOrder = document.getElementById('ee-sort-filter').value;
    const site = document.getElementById('ee-site-filter').value;
    const tagFilter = document.getElementById('ee-tag-filter').value;

    try {
        const params = new URLSearchParams({
            limit: eePageSize,
            offset: (eeCurrentPage - 1) * eePageSize,
            has_thumbnail: '1',
            sort: sortOrder
        });

        if (camera) params.append('camera', camera);
        if (eventType) params.append('event_type', eventType);
        if (dateValue) params.append('since', dateValue);
        if (site) params.append('site', site);
        if (tagFilter === 'untagged') {
            params.append('untagged', '1');
        } else if (tagFilter.startsWith('tag:')) {
            params.append('tag_id', tagFilter.replace('tag:', ''));
        }

        const response = await eeFetchWithTimeout('/api/ecoeye/events?' + params, null, 30000);
        const data = await response.json();

        if (data.success) {
            eeAllEvents = data.events || [];
            await eeLoadEventTags();
            eeRenderEvents(eeAllEvents);
            eeUpdatePagination(data.total || eeAllEvents.length);
        } else {
            grid.textContent = '';
            var errDiv = document.createElement('div');
            errDiv.className = 'loading';
            errDiv.textContent = 'Failed to load events: ' + (data.error || 'Unknown error');
            grid.appendChild(errDiv);
        }
    } catch (error) {
        grid.textContent = '';
        var errDiv2 = document.createElement('div');
        errDiv2.className = 'loading';
        var msg = error.name === 'AbortError'
            ? 'Request timed out. The EcoEye relay may be slow. Click Search to retry.'
            : 'Error loading events: ' + error.message;
        errDiv2.textContent = msg;
        grid.appendChild(errDiv2);
    }
}

function eeRenderEvents(events) {
    const grid = document.getElementById('videos-grid');

    if (events.length === 0) {
        grid.textContent = '';
        var emptyDiv = document.createElement('div');
        emptyDiv.className = 'loading';
        emptyDiv.textContent = 'No EcoEye events found. Try adjusting your filters.';
        grid.appendChild(emptyDiv);
        return;
    }

    var cards = events.map(function(event) {
        const eventId = event.event_id || event.id;
        const isSelected = eeSelectedEvents.has(eventId);
        const badgeClass = eeGetBadgeClass(event.event_type);
        const eventTags = eeEventTagsMap[eventId] || [];
        const thumbnailSrc = event.local_thumbnail || event.thumbnail;

        var card = document.createElement('div');
        card.className = 'ee-event-card' + (isSelected ? ' selected' : '');
        card.dataset.eventId = eventId;

        // Thumbnail container
        var thumbDiv = document.createElement('div');
        thumbDiv.className = 'ee-event-thumbnail';

        if (thumbnailSrc) {
            var img = document.createElement('img');
            img.src = thumbnailSrc;
            img.alt = 'Event thumbnail';
            img.onerror = function() {
                thumbDiv.textContent = '';
                var noThumb = document.createElement('div');
                noThumb.className = 'ee-no-thumb';
                noThumb.textContent = '\uD83D\uDCF9';
                thumbDiv.appendChild(noThumb);
            };
            thumbDiv.appendChild(img);
        } else {
            var noThumb = document.createElement('div');
            noThumb.className = 'ee-no-thumb';
            noThumb.textContent = '\uD83D\uDCF9';
            thumbDiv.appendChild(noThumb);
        }

        // Play overlay for local videos
        if (event.has_local_video && event.local_video_path) {
            var overlay = document.createElement('div');
            overlay.className = 'ee-play-overlay ee-always-visible';
            overlay.onclick = (function(path, name) {
                return function() { eePlayVideo(path, name); };
            })(event.local_video_path, event.camera_name || 'Video');
            var playBtn = document.createElement('div');
            playBtn.className = 'ee-play-btn';
            playBtn.textContent = '\u25B6';
            overlay.appendChild(playBtn);
            thumbDiv.appendChild(overlay);
        }

        // Event type badge
        var badge = document.createElement('div');
        badge.className = 'ee-event-badge ee-' + badgeClass;
        badge.textContent = event.event_type || 'Event';
        thumbDiv.appendChild(badge);

        // Imported badge
        if (event.has_local_video) {
            var impBadge = document.createElement('div');
            impBadge.className = 'ee-imported-badge ee-has-video';
            impBadge.textContent = 'In Studio';
            thumbDiv.appendChild(impBadge);
        } else if (event.imported_to_studio) {
            var impBadge2 = document.createElement('div');
            impBadge2.className = 'ee-imported-badge ee-metadata-only';
            impBadge2.textContent = 'Imported';
            thumbDiv.appendChild(impBadge2);
        }

        card.appendChild(thumbDiv);

        // Info section
        var infoDiv = document.createElement('div');
        infoDiv.className = 'ee-event-info';

        var cameraDiv = document.createElement('div');
        cameraDiv.className = 'ee-event-camera';
        cameraDiv.textContent = event.camera_name || 'Unknown Camera';
        infoDiv.appendChild(cameraDiv);

        var metaDiv = document.createElement('div');
        metaDiv.className = 'ee-event-meta';
        var tsDiv = document.createElement('div');
        tsDiv.textContent = eeFormatTimestamp(event.timestamp);
        metaDiv.appendChild(tsDiv);
        if (event.site_name) {
            var siteDiv = document.createElement('div');
            siteDiv.textContent = '\uD83D\uDCCD ' + event.site_name;
            metaDiv.appendChild(siteDiv);
        }
        if (event.alarm_name) {
            var alarmDiv = document.createElement('div');
            alarmDiv.textContent = '\uD83D\uDD14 ' + event.alarm_name;
            metaDiv.appendChild(alarmDiv);
        }
        infoDiv.appendChild(metaDiv);

        // Status bar
        var statusBar = document.createElement('div');
        statusBar.className = 'ee-event-status-bar';
        if (event.has_local_video) {
            statusBar.classList.add('ee-status-local-video');
            statusBar.textContent = '\u2713 Video in Studio';
        } else if (event.imported_to_studio) {
            statusBar.classList.add('ee-status-imported');
            statusBar.textContent = '\uD83D\uDCCB Metadata Imported';
        } else if (event.has_video) {
            statusBar.classList.add('ee-status-available');
            statusBar.textContent = '\uD83D\uDCF9 Video Available';
        } else {
            statusBar.classList.add('ee-status-pending');
            statusBar.textContent = '\u23F3 Not Imported';
        }
        infoDiv.appendChild(statusBar);

        // Tags
        var tagsDiv = document.createElement('div');
        tagsDiv.className = 'ee-event-tags';
        eventTags.forEach(function(t) {
            var tagSpan = document.createElement('span');
            tagSpan.className = 'ee-event-tag';
            tagSpan.style.background = t.color;
            tagSpan.textContent = t.name;
            tagsDiv.appendChild(tagSpan);
        });
        infoDiv.appendChild(tagsDiv);

        // Actions
        var actionsDiv = document.createElement('div');
        actionsDiv.className = 'ee-event-actions';

        var checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.className = 'ee-event-checkbox';
        checkbox.checked = isSelected;
        checkbox.onchange = (function(eid) {
            return function() { eeToggleSelection(eid, this.checked); };
        })(eventId);
        actionsDiv.appendChild(checkbox);

        if (event.has_local_video) {
            var annotateBtn = document.createElement('button');
            annotateBtn.className = 'btn-primary';
            annotateBtn.textContent = 'Open in Annotator';
            annotateBtn.onclick = (function(vid) {
                return function() { openAnnotate(vid); };
            })(event.local_video_id);
            actionsDiv.appendChild(annotateBtn);
        } else if (event.imported_to_studio) {
            var reqVidBtn = document.createElement('button');
            reqVidBtn.className = 'btn-secondary';
            reqVidBtn.style.background = '#f39c12';
            reqVidBtn.style.color = 'white';
            reqVidBtn.style.fontSize = '12px';
            reqVidBtn.textContent = 'Request Video';
            reqVidBtn.onclick = (function(eid) {
                return function() { eeRequestVideoDownload(eid); };
            })(eventId);
            actionsDiv.appendChild(reqVidBtn);
            if (event.local_video_id) {
                var annBtn2 = document.createElement('button');
                annBtn2.className = 'btn-primary';
                annBtn2.style.fontSize = '12px';
                annBtn2.textContent = 'Annotate';
                annBtn2.onclick = (function(vid) {
                    return function() { openAnnotate(vid); };
                })(event.local_video_id);
                actionsDiv.appendChild(annBtn2);
            }
        } else if (event.has_video) {
            var syncBtn = document.createElement('button');
            syncBtn.className = 'btn-primary';
            syncBtn.textContent = 'Sync with Video';
            syncBtn.onclick = (function(eid) {
                return function() { eeSyncSingleEvent(eid); };
            })(eventId);
            actionsDiv.appendChild(syncBtn);
        } else {
            var metaBtn = document.createElement('button');
            metaBtn.className = 'btn-secondary';
            metaBtn.style.fontSize = '12px';
            metaBtn.textContent = 'Import Metadata';
            metaBtn.onclick = (function(eid) {
                return function() { eeImportMetadataOnly(eid); };
            })(eventId);
            actionsDiv.appendChild(metaBtn);

            var reqBtn = document.createElement('button');
            reqBtn.className = 'btn-secondary';
            reqBtn.style.background = '#f39c12';
            reqBtn.style.color = 'white';
            reqBtn.style.fontSize = '12px';
            reqBtn.textContent = 'Request Video';
            reqBtn.onclick = (function(eid) {
                return function() { eeRequestVideoDownload(eid); };
            })(eventId);
            actionsDiv.appendChild(reqBtn);
        }

        infoDiv.appendChild(actionsDiv);
        card.appendChild(infoDiv);

        return card;
    });

    grid.replaceChildren.apply(grid, cards);
    eeUpdateSelectedCount();
}

function eeGetBadgeClass(eventType) {
    if (!eventType) return 'default';
    const type = eventType.toLowerCase();
    return ['motion', 'person', 'vehicle', 'animal', 'package'].includes(type) ? type : 'default';
}

function eeFormatTimestamp(timestamp) {
    if (!timestamp) return 'Unknown time';
    return new Date(timestamp).toLocaleString();
}

// --- Selection ---

function eeToggleSelection(eventId, isChecked) {
    if (isChecked) { eeSelectedEvents.add(eventId); } else { eeSelectedEvents.delete(eventId); }
    eeUpdateSelectedCount();
    eeUpdateTagButtons();
    var card = document.querySelector('.ee-event-card[data-event-id="' + eventId + '"]');
    if (card) card.classList.toggle('selected', isChecked);
}

function eeSelectAll() {
    eeAllEvents.forEach(function(event) { eeSelectedEvents.add(event.event_id || event.id); });
    document.querySelectorAll('.ee-event-checkbox').forEach(function(cb) { cb.checked = true; });
    document.querySelectorAll('.ee-event-card').forEach(function(card) { card.classList.add('selected'); });
    eeUpdateSelectedCount();
    eeUpdateTagButtons();
}

function eeDeselectAll() {
    eeSelectedEvents.clear();
    document.querySelectorAll('.ee-event-checkbox').forEach(function(cb) { cb.checked = false; });
    document.querySelectorAll('.ee-event-card').forEach(function(card) { card.classList.remove('selected'); });
    eeUpdateSelectedCount();
    eeUpdateTagButtons();
}

function eeClearAllSelections() {
    eeSelectedEvents.clear();
    eeSelectedTags.clear();
    document.querySelectorAll('.ee-event-checkbox').forEach(function(cb) { cb.checked = false; });
    document.querySelectorAll('.ee-event-card').forEach(function(card) { card.classList.remove('selected'); });
    eeRenderTagPills();
    eeUpdateSelectedCount();
    eeUpdateTagButtons();
}

function eeUpdateSelectedCount() {
    document.getElementById('ee-selected-count').textContent = eeSelectedEvents.size;
}

// --- Event Actions ---

async function eeSyncSingleEvent(eventId) {
    eeShowLoading('Importing event with video...');
    try {
        const response = await fetch('/api/ecoeye/sync-sample', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ event_id: eventId, include_video: true })
        });
        const data = await response.json();
        eeHideLoading();
        if (data.success) {
            alert('Event imported successfully! Video ID: ' + data.video_id);
            await eeLoadEvents();
        } else {
            alert('Failed to import event: ' + (data.error || 'Unknown error'));
        }
    } catch (error) {
        eeHideLoading();
        alert('Failed to import event: ' + error.message);
    }
}

async function eeImportMetadataOnly(eventId) {
    eeShowLoading('Importing metadata...');
    try {
        const response = await fetch('/api/ecoeye/sync-sample', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ event_id: eventId, include_video: false })
        });
        const data = await response.json();
        eeHideLoading();
        if (data.success) {
            alert('Metadata imported! Record ID: ' + data.record_id);
            await eeLoadEvents();
        } else {
            alert('Failed to import: ' + (data.error || 'Unknown error'));
        }
    } catch (error) {
        eeHideLoading();
        alert('Failed to import: ' + error.message);
    }
}

async function eeRequestVideoDownload(eventId) {
    if (!confirm('Request the EcoEye relay to download this video clip?')) return;
    eeShowLoading('Requesting video download...');
    try {
        const response = await fetch('/api/ecoeye/request-download', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ event_id: eventId })
        });
        const data = await response.json();
        eeHideLoading();
        if (data.success) {
            alert('Download request sent! The video will be available soon.');
            eeLoadEvents();
        } else {
            alert('Failed to request download: ' + (data.error || 'Unknown error'));
        }
    } catch (error) {
        eeHideLoading();
        alert('Failed to request download: ' + error.message);
    }
}

async function eeImportSelected() {
    if (eeSelectedEvents.size === 0) { alert('No events selected'); return; }

    const requestVideos = confirm(
        'Import ' + eeSelectedEvents.size + ' selected events.\n\nClick OK to also request video downloads.\nClick Cancel to import metadata/thumbnails only.'
    );

    eeShowLoading('Importing ' + eeSelectedEvents.size + ' events...');
    var imported = 0, failed = 0, videosRequested = 0;

    for (const eventId of eeSelectedEvents) {
        try {
            const response = await fetch('/api/ecoeye/sync-sample', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ event_id: eventId, include_video: false })
            });
            const data = await response.json();
            if (data.success) {
                imported++;
                if (requestVideos) {
                    const event = eeAllEvents.find(function(e) { return (e.event_id || e.id) === eventId; });
                    if (event && !event.has_video) {
                        try {
                            await fetch('/api/ecoeye/request-download', {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ event_id: eventId })
                            });
                            videosRequested++;
                        } catch (e) {}
                    }
                }
            } else { failed++; }
            document.getElementById('ee-loading-message').textContent = 'Importing... (' + (imported + failed) + '/' + eeSelectedEvents.size + ')';
        } catch (error) { failed++; }
    }

    eeHideLoading();
    var message = 'Import complete!\nSuccessful: ' + imported + '\nFailed: ' + failed;
    if (videosRequested > 0) message += '\nVideo downloads requested: ' + videosRequested;
    alert(message);
    eeDeselectAll();
    await eeLoadEvents();
}

// --- Pagination ---

function eeFirstPage() { if (eeCurrentPage !== 1) { eeCurrentPage = 1; eeLoadEvents(); } }
function eePreviousPage() { if (eeCurrentPage > 1) { eeCurrentPage--; eeLoadEvents(); } }
function eeNextPage() { if (eeCurrentPage < eeTotalPages) { eeCurrentPage++; eeLoadEvents(); } }
function eeLastPage() { if (eeCurrentPage !== eeTotalPages) { eeCurrentPage = eeTotalPages; eeLoadEvents(); } }

function eeUpdatePagination(total) {
    eeTotalPages = Math.ceil(total / eePageSize) || 1;
    document.getElementById('ee-page-info').textContent = 'Page ' + eeCurrentPage + ' of ' + eeTotalPages + ' (' + total + ' events)';
    document.getElementById('ee-first-btn').disabled = eeCurrentPage === 1;
    document.getElementById('ee-prev-btn').disabled = eeCurrentPage === 1;
    document.getElementById('ee-next-btn').disabled = eeCurrentPage >= eeTotalPages;
    document.getElementById('ee-last-btn').disabled = eeCurrentPage >= eeTotalPages;
}

// --- Loading ---

function eeShowLoading(message) {
    document.getElementById('ee-loading-message').textContent = message;
    document.getElementById('ee-loading-overlay').style.display = 'flex';
}

function eeHideLoading() {
    document.getElementById('ee-loading-overlay').style.display = 'none';
}

// --- Video Player ---

function eePlayVideo(videoPath, title) {
    const modal = document.getElementById('ee-video-modal');
    const video = document.getElementById('ee-video-player');
    const source = document.getElementById('ee-video-source');
    document.getElementById('ee-video-title').textContent = title;
    source.src = videoPath;
    video.load();
    modal.style.display = 'flex';
    video.play();
}

function eeCloseVideoPlayer() {
    const modal = document.getElementById('ee-video-modal');
    const video = document.getElementById('ee-video-player');
    video.pause();
    modal.style.display = 'none';
}

// Download Queue Status
async function loadDownloadQueueStatus() {
    const statusDiv = document.getElementById('download-queue-status');
    if (!statusDiv) return;
    try {
        const response = await fetch('/api/download/status');
        const data = await response.json();

        if (data.queue_size === 0 && Object.keys(data.active_downloads).length === 0) {
            statusDiv.textContent = 'Queue is empty';
        } else {
            let html = '<div style="font-size: 14px;">';

            // Show currently downloading
            const downloading = Object.entries(data.active_downloads).filter(([url, status]) => status === 'downloading');
            if (downloading.length > 0) {
                html += '<div style="margin-bottom: 10px;"><strong style="color: #3498db;">⬇ Downloading:</strong><ul style="margin: 5px 0; padding-left: 20px;">';
                for (const [url, status] of downloading) {
                    const displayUrl = url.length > 50 ? url.substring(0, 47) + '...' : url;
                    html += `<li style="font-size: 13px; margin: 3px 0;">${escapeHtml(displayUrl)}</li>`;
                }
                html += '</ul></div>';
            }

            // Show queued items in order (top = next to download)
            if (data.queued_items && data.queued_items.length > 0) {
                html += '<div style="margin-bottom: 10px;"><strong>📋 Queued (in order):</strong><ol style="margin: 5px 0; padding-left: 25px;">';
                data.queued_items.forEach((item, index) => {
                    const displayUrl = item.url.length > 50 ? item.url.substring(0, 47) + '...' : item.url;
                    const label = index === 0 ? ' <span style="color: #27ae60; font-weight: bold;">(Next)</span>' : '';
                    html += `<li style="font-size: 13px; margin: 3px 0;">${escapeHtml(displayUrl)}${label}</li>`;
                });
                html += '</ol></div>';
            }

            html += `<div style="font-size: 13px; color: #7f8c8d;">✅ Completed: ${data.completed_count}</div>`;
            html += '</div>';

            statusDiv.innerHTML = html;
        }
    } catch (error) {
        console.error('Error loading queue status:', error);
        if (statusDiv) statusDiv.textContent = 'Error loading queue status';
    }
}

// Refresh queue status every 3 seconds
setInterval(loadDownloadQueueStatus, 3000);

// ── Multi-Select ──────────────────────────────────────────────

function toggleMultiSelect() {
    multiSelectMode = !multiSelectMode;
    const btn = document.getElementById('multi-select-toggle');
    const bar = document.getElementById('bulk-action-bar');
    if (multiSelectMode) {
        btn.classList.add('active');
        btn.textContent = 'Cancel Select';
        bar.style.display = 'flex';
        document.getElementById('videos-grid').classList.add('multi-select-mode');
    } else {
        btn.classList.remove('active');
        btn.textContent = 'Select';
        bar.style.display = 'none';
        selectedVideoIds.clear();
        document.getElementById('videos-grid').classList.remove('multi-select-mode');
        document.querySelectorAll('.video-card.selected').forEach(c => c.classList.remove('selected'));
    }
    updateBulkCount();
}

function toggleVideoSelect(videoId, cardEl, event) {
    if (!multiSelectMode) return;
    event.stopPropagation();
    if (selectedVideoIds.has(videoId)) {
        selectedVideoIds.delete(videoId);
        cardEl.classList.remove('selected');
    } else {
        selectedVideoIds.add(videoId);
        cardEl.classList.add('selected');
    }
    updateBulkCount();
}

function selectAllVideos() {
    const cards = document.querySelectorAll('.video-card[data-video-id]');
    cards.forEach(card => {
        const id = parseInt(card.dataset.videoId);
        selectedVideoIds.add(id);
        card.classList.add('selected');
    });
    updateBulkCount();
}

function deselectAllVideos() {
    selectedVideoIds.clear();
    document.querySelectorAll('.video-card.selected').forEach(c => c.classList.remove('selected'));
    updateBulkCount();
}

function updateBulkCount() {
    const countEl = document.getElementById('bulk-selected-count');
    if (countEl) countEl.textContent = selectedVideoIds.size;
}

async function bulkAssignLibrary() {
    const select = document.getElementById('bulk-library-select');
    const libraryId = select.value;
    if (!libraryId) { alert('Select a library first'); return; }
    if (selectedVideoIds.size === 0) { alert('No videos selected'); return; }
    try {
        const res = await fetch(`/api/libraries/${libraryId}/items`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ video_ids: Array.from(selectedVideoIds) })
        });
        const data = await res.json();
        if (data.success) {
            alert(`Added ${data.added} video(s) to library`);
            toggleMultiSelect();
            loadLibraries();
            loadVideos();
        } else {
            alert(data.error || 'Failed to assign');
        }
    } catch (e) {
        alert('Error assigning to library');
    }
}

async function bulkRemoveLibrary() {
    if (!activeLibrary) { alert('Select a library view first'); return; }
    if (selectedVideoIds.size === 0) { alert('No videos selected'); return; }
    if (!confirm(`Remove ${selectedVideoIds.size} video(s) from this library?`)) return;
    try {
        for (const vid of selectedVideoIds) {
            await fetch(`/api/libraries/${activeLibrary}/items/${vid}`, { method: 'DELETE' });
        }
        toggleMultiSelect();
        loadLibraries();
        loadVideos();
    } catch (e) {
        alert('Error removing from library');
    }
}

// ── Content Libraries ──────────────────────────────────────────────

async function loadVideoLibraryChecklist(videoId) {
    const container = document.getElementById('video-library-checklist');
    if (!container) return;
    try {
        const [libRes, vidRes] = await Promise.all([
            fetch('/api/libraries'),
            fetch(`/api/videos/${videoId}`)
        ]);
        const libData = await libRes.json();
        const vidData = await vidRes.json();
        const videoLibIds = (vidData.video?.libraries || []).map(l => l.id);

        container.innerHTML = libData.libraries.map(lib => {
            const checked = videoLibIds.includes(lib.id);
            return `<label class="${checked ? 'checked' : ''}" onclick="toggleVideoLibrary(${videoId}, ${lib.id}, this)">
                <input type="checkbox" ${checked ? 'checked' : ''} style="display:none">
                ${escapeHtml(lib.name)}
            </label>`;
        }).join('');
    } catch (e) {
        container.textContent = 'Error loading libraries';
    }
}

async function toggleVideoLibrary(videoId, libraryId, labelEl) {
    const isChecked = labelEl.classList.contains('checked');
    try {
        if (isChecked) {
            await fetch(`/api/libraries/${libraryId}/items/${videoId}`, { method: 'DELETE' });
            labelEl.classList.remove('checked');
        } else {
            await fetch(`/api/libraries/${libraryId}/items`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ video_ids: [videoId] })
            });
            labelEl.classList.add('checked');
        }
        loadLibraries(); // refresh counts
    } catch (e) {
        console.error('Error toggling library:', e);
    }
}

function openLibraryManager() {
    document.getElementById('library-manager-modal').style.display = 'flex';
    renderLibraryManager();
}

function closeLibraryManager() {
    document.getElementById('library-manager-modal').style.display = 'none';
    loadLibraries(); // refresh dropdown
}

async function renderLibraryManager() {
    const list = document.getElementById('library-manager-list');
    list.innerHTML = '<div class="loading">Loading...</div>';
    try {
        const res = await fetch('/api/libraries');
        const data = await res.json();
        if (!data.success || !data.libraries.length) {
            list.innerHTML = '<p>No libraries found.</p>';
            return;
        }
        list.innerHTML = data.libraries.map(lib => `
            <div class="library-item">
                <span class="library-item-name" ${!lib.is_default ? `onclick="renameLibraryPrompt(${lib.id}, '${escapeHtml(lib.name)}')"` : ''}>${escapeHtml(lib.name)}</span>
                ${lib.is_default ? '<span class="library-item-default">Default</span>' : ''}
                <span class="library-item-count">${lib.item_count} item${lib.item_count !== 1 ? 's' : ''}</span>
                <div class="library-item-actions">
                    ${!lib.is_default ? `
                        <button onclick="renameLibraryPrompt(${lib.id}, '${escapeHtml(lib.name)}')">Rename</button>
                        <button class="btn-delete" onclick="deleteLibraryConfirm(${lib.id}, '${escapeHtml(lib.name)}')">Delete</button>
                    ` : ''}
                </div>
            </div>
        `).join('');
    } catch (e) {
        list.innerHTML = '<p>Error loading libraries.</p>';
    }
}

async function createLibrary() {
    const input = document.getElementById('new-library-name');
    const name = input.value.trim();
    if (!name) return;
    try {
        const res = await fetch('/api/libraries', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name })
        });
        const data = await res.json();
        if (data.success) {
            input.value = '';
            renderLibraryManager();
        } else {
            alert(data.error || 'Failed to create library');
        }
    } catch (e) {
        alert('Error creating library');
    }
}

async function renameLibraryPrompt(id, currentName) {
    const newName = prompt('Rename library:', currentName);
    if (!newName || newName.trim() === currentName) return;
    try {
        const res = await fetch(`/api/libraries/${id}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: newName.trim() })
        });
        const data = await res.json();
        if (data.success) {
            renderLibraryManager();
        } else {
            alert(data.error || 'Failed to rename library');
        }
    } catch (e) {
        alert('Error renaming library');
    }
}

async function deleteLibraryConfirm(id, name) {
    if (!confirm(`Delete library "${name}"? Videos will NOT be deleted, they just won't be in this library anymore.`)) return;
    try {
        const res = await fetch(`/api/libraries/${id}`, { method: 'DELETE' });
        const data = await res.json();
        if (data.success) {
            renderLibraryManager();
            if (activeLibrary == id) {
                activeLibrary = '';
                document.getElementById('library-select').value = '';
                loadVideos();
            }
        } else {
            alert(data.error || 'Failed to delete library');
        }
    } catch (e) {
        alert('Error deleting library');
    }
}

checkSystemStatus();

// Restore library filter from URL (e.g. returning from annotate page)
(function() {
    const params = new URLSearchParams(window.location.search);
    const libParam = params.get('library');
    if (libParam) {
        activeLibrary = libParam;
        // Set dropdown after libraries load
        const origLoad = loadLibraries;
        loadLibraries = async function() {
            await origLoad();
            const select = document.getElementById('library-select');
            if (select) select.value = libParam;
        };
    }
})();

loadLibraries();
loadVideos();
loadDownloadQueueStatus();
