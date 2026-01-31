let allVideos = [];
let allTags = [];

async function checkSystemStatus() {
    try {
        const response = await fetch('/api/system/status');
        const data = await response.json();

        const statusEl = document.getElementById('status-indicator');
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
    grid.innerHTML = '<div class="loading">Loading videos...</div>';

    try {
        const response = await fetch('/api/videos');
        const data = await response.json();

        if (data.success) {
            allVideos = data.videos;
            displayVideos(allVideos);
        }
    } catch (error) {
        grid.innerHTML = '<div class="loading">Error loading videos</div>';
    }
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

        // Build thumbnail HTML
        let thumbnailHtml = '';
        if (video.thumbnail_path) {
            const thumbName = video.thumbnail_path.split('/').pop();
            thumbnailHtml = `<img src="/thumbnails/${encodeURIComponent(thumbName)}" class="video-thumbnail" alt="${escapeHtml(video.title)}">`;
        } else {
            thumbnailHtml = '<div class="video-thumbnail"></div>';
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
                <button onclick="showVideoDetails(${video.id})" class="btn-secondary" style="width: 100%; font-size: 13px;">
                    View Details
                </button>
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
            <div class="${cardClasses.join(' ')}">
                <div onclick="showVideoDetails(${video.id})" style="cursor: pointer; position: relative;">
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
                    </div>
                </div>
                <div style="padding: 10px; border-top: 1px solid #ecf0f1;">
                    ${actionButton}
                </div>
            </div>
        `;
    }).join('');
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
document.getElementById('video-files').addEventListener('change', (e) => {
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
document.getElementById('upload-form').addEventListener('submit', async (e) => {
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
        const response = await fetch(`/api/videos?search=${encodeURIComponent(query)}`);
        const data = await response.json();

        if (data.success) {
            displayVideos(data.videos);
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

        document.getElementById('video-modal').style.display = 'flex';
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
    window.location.href = `/annotate?id=${videoId}`;
}

document.getElementById('search-input').addEventListener('keypress', (e) => {
    if (e.key === 'Enter') {
        searchVideos();
    }
});

window.onclick = function(event) {
    const videoModal = document.getElementById('video-modal');
    const uploadModal = document.getElementById('upload-modal');

    if (event.target === videoModal) {
        closeModal();
    }
    if (event.target === uploadModal) {
        closeUploadModal();
    }
}

// Download Queue Status
async function loadDownloadQueueStatus() {
    try {
        const response = await fetch('/api/download/status');
        const data = await response.json();
        const statusDiv = document.getElementById('download-queue-status');

        if (data.queue_size === 0 && Object.keys(data.active_downloads).length === 0) {
            statusDiv.innerHTML = '<div style="color: #95a5a6; font-size: 14px;">Queue is empty</div>';
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
        document.getElementById('download-queue-status').innerHTML =
            '<div style="color: #e74c3c;">Error loading queue status</div>';
    }
}

// Refresh queue status every 3 seconds
setInterval(loadDownloadQueueStatus, 3000);

checkSystemStatus();
loadVideos();
loadDownloadQueueStatus();
