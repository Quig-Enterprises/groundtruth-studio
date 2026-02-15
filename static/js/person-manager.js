/**
 * Person Name Manager
 * Manage person identifications across all annotations
 */

let allDetections = [];
let selectedDetections = new Set();
let currentFilter = null; // null = all, 'unknown' = unknown, or person name
let recentNames = [];

// Initialize on page load
document.addEventListener('DOMContentLoaded', () => {
    loadPersonDetections();
    loadRecentNames();
    setupEventListeners();
});

function setupEventListeners() {
    // Search filter
    document.getElementById('search-filter').addEventListener('input', (e) => {
        filterDetections(e.target.value);
    });

    // Sort filter
    document.getElementById('sort-filter').addEventListener('change', (e) => {
        sortDetections(e.target.value);
    });

    // Bulk actions
    document.getElementById('select-all-btn').addEventListener('click', toggleSelectAll);
    document.getElementById('bulk-assign-btn').addEventListener('click', openBulkAssignModal);
    document.getElementById('bulk-unassign-btn').addEventListener('click', bulkUnassign);

    // Name input autocomplete
    const nameInput = document.getElementById('assign-name-input');
    nameInput.addEventListener('input', (e) => {
        showNameSuggestions(e.target.value);
    });
    nameInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            confirmAssignName();
        }
    });

    // Close modal on background click
    document.getElementById('assign-modal').addEventListener('click', (e) => {
        if (e.target.id === 'assign-modal') {
            closeAssignModal();
        }
    });
}

async function loadPersonDetections() {
    try {
        const response = await fetch('/api/person-detections');
        const data = await response.json();

        if (data.success) {
            allDetections = data.detections;
            updateStats(data.stats);
            renderPersonNames(data.people);
            renderDetections(allDetections);
        } else {
            showError('Failed to load person detections');
        }
    } catch (error) {
        console.error('Error loading detections:', error);
        showError('Error loading person detections');
    }
}

async function loadRecentNames() {
    try {
        const response = await fetch('/api/person-names/recent?limit=10');
        const data = await response.json();

        if (data.success) {
            recentNames = data.names;
        }
    } catch (error) {
        console.error('Error loading recent names:', error);
    }
}

function updateStats(stats) {
    document.getElementById('stat-total-detections').textContent = stats.total_detections;
    document.getElementById('stat-named-people').textContent = stats.named_people;
    document.getElementById('stat-unknown').textContent = stats.unknown;
    document.getElementById('stat-videos').textContent = stats.videos_with_people;
}

function renderPersonNames(people) {
    const container = document.getElementById('person-names-list');

    if (!people || people.length === 0) {
        container.innerHTML = '<div class="empty-state">No people detected yet</div>';
        return;
    }

    let html = '';

    // Unknown detections
    const unknownCount = people.find(p => p.name === null || p.name === 'Unknown')?.count || 0;
    if (unknownCount > 0) {
        html += `
            <div class="person-name-item ${currentFilter === 'unknown' ? 'active' : ''}" onclick="filterByPerson('unknown')">
                <span class="name">Unknown</span>
                <span class="count">${unknownCount}</span>
            </div>
        `;
    }

    // Named people
    people.filter(p => p.name && p.name !== 'Unknown').forEach(person => {
        const isActive = currentFilter === person.name;
        html += `
            <div class="person-name-item ${isActive ? 'active' : ''}" onclick="filterByPerson('${escapeHtml(person.name)}')">
                <span class="name">${escapeHtml(person.name)}</span>
                <span class="count">${person.count}</span>
            </div>
        `;
    });

    container.innerHTML = html;
}

function renderDetections(detections) {
    const container = document.getElementById('detections-grid');

    if (!detections || detections.length === 0) {
        container.innerHTML = `
            <div class="empty-state">
                <div class="empty-state-icon">🔍</div>
                <p>No detections found</p>
            </div>
        `;
        return;
    }

    container.textContent = '';
    detections.forEach(detection => {
        const isSelected = selectedDetections.has(detection.id);
        const personName = detection.person_name || 'Unknown';

        const card = document.createElement('div');
        card.className = 'detection-card' + (isSelected ? ' selected' : '');
        card.setAttribute('data-id', detection.id);

        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.className = 'detection-checkbox';
        checkbox.checked = isSelected;
        checkbox.onchange = () => toggleDetectionSelect(detection.id);

        const thumbDiv = document.createElement('div');
        thumbDiv.className = 'detection-thumbnail';
        thumbDiv.setAttribute('data-mode', 'crop');

        const img = document.createElement('img');
        img.alt = 'Detection';

        thumbDiv.onclick = (e) => {
            e.stopPropagation();
            toggleThumbnailView(thumbDiv, img, detection);
        };
        thumbDiv.ondblclick = (e) => {
            e.stopPropagation();
            viewDetection(detection.video_id, detection.timestamp);
        };

        thumbDiv.appendChild(img);

        // Set src, then crop once fully decoded
        img.src = detection.thumbnail_path;
        if (detection.bbox_x != null && detection.bbox_width > 0) {
            img.decode().then(() => {
                applyCropView(thumbDiv, img, detection);
            }).catch(() => {});
        }

        const info = document.createElement('div');
        info.className = 'detection-info';

        const titleDiv = document.createElement('div');
        titleDiv.className = 'video-title';
        titleDiv.textContent = detection.video_title;

        const tsDiv = document.createElement('div');
        tsDiv.className = 'timestamp';
        tsDiv.textContent = '@ ' + formatTimestamp(detection.timestamp);

        const tagsDiv = document.createElement('div');
        tagsDiv.className = 'tags';
        const nameTag = document.createElement('span');
        nameTag.className = 'tag';
        nameTag.textContent = personName;
        tagsDiv.appendChild(nameTag);

        if (detection.pose) {
            const poseTag = document.createElement('span');
            poseTag.className = 'tag';
            poseTag.textContent = detection.pose;
            tagsDiv.appendChild(poseTag);
        }
        if (detection.distance_category) {
            const distTag = document.createElement('span');
            distTag.className = 'tag';
            distTag.textContent = detection.distance_category;
            tagsDiv.appendChild(distTag);
        }

        info.appendChild(titleDiv);
        info.appendChild(tsDiv);
        info.appendChild(tagsDiv);

        card.appendChild(checkbox);
        card.appendChild(thumbDiv);
        card.appendChild(info);
        container.appendChild(card);
    });
    updateBulkActionButtons();
}

function filterByPerson(personName) {
    currentFilter = personName;

    let filtered;
    if (personName === 'unknown') {
        filtered = allDetections.filter(d => !d.person_name || d.person_name === 'Unknown');
        document.getElementById('detections-title').textContent = 'Unknown People';
    } else if (personName === null) {
        filtered = allDetections;
        document.getElementById('detections-title').textContent = 'All Detections';
    } else {
        filtered = allDetections.filter(d => d.person_name === personName);
        document.getElementById('detections-title').textContent = `Detections: ${personName}`;
    }

    document.getElementById('detections-subtitle').textContent = `${filtered.length} detection(s)`;
    renderDetections(filtered);

    // Update active state in person list
    document.querySelectorAll('.person-name-item').forEach(item => {
        item.classList.remove('active');
    });
    event.target.classList.add('active');
}

function toggleDetectionSelect(detectionId) {
    if (selectedDetections.has(detectionId)) {
        selectedDetections.delete(detectionId);
    } else {
        selectedDetections.add(detectionId);
    }

    renderDetections(getCurrentFilteredDetections());
    updateBulkActionButtons();
}

function toggleSelectAll() {
    const filtered = getCurrentFilteredDetections();
    const allSelected = filtered.every(d => selectedDetections.has(d.id));

    if (allSelected) {
        // Deselect all
        filtered.forEach(d => selectedDetections.delete(d.id));
    } else {
        // Select all
        filtered.forEach(d => selectedDetections.add(d.id));
    }

    renderDetections(filtered);
    updateBulkActionButtons();
}

function getCurrentFilteredDetections() {
    if (currentFilter === 'unknown') {
        return allDetections.filter(d => !d.person_name || d.person_name === 'Unknown');
    } else if (currentFilter) {
        return allDetections.filter(d => d.person_name === currentFilter);
    }
    return allDetections;
}

function updateBulkActionButtons() {
    const hasSelection = selectedDetections.size > 0;
    document.getElementById('bulk-assign-btn').disabled = !hasSelection;
    document.getElementById('bulk-unassign-btn').disabled = !hasSelection;
}

function openBulkAssignModal() {
    if (selectedDetections.size === 0) return;

    document.getElementById('assign-count').textContent = selectedDetections.size;
    document.getElementById('assign-name-input').value = '';
    document.getElementById('assign-modal').classList.add('show');
    document.getElementById('assign-name-input').focus();
}

function closeAssignModal() {
    document.getElementById('assign-modal').classList.remove('show');
}

function showNameSuggestions(query) {
    const suggestionsDiv = document.getElementById('assign-name-suggestions');

    if (!query || query.length < 1) {
        suggestionsDiv.classList.remove('show');
        return;
    }

    // Filter recent names by query
    const matches = recentNames.filter(name =>
        name.toLowerCase().includes(query.toLowerCase())
    );

    if (matches.length === 0) {
        suggestionsDiv.classList.remove('show');
        return;
    }

    let html = '';
    matches.forEach(name => {
        const count = allDetections.filter(d => d.person_name === name).length;
        html += `
            <div class="autocomplete-item" onclick="selectNameSuggestion('${escapeHtml(name)}')">
                ${escapeHtml(name)}
                <span class="count">${count}</span>
            </div>
        `;
    });

    suggestionsDiv.innerHTML = html;
    suggestionsDiv.classList.add('show');
}

function selectNameSuggestion(name) {
    document.getElementById('assign-name-input').value = name;
    document.getElementById('assign-name-suggestions').classList.remove('show');
}

async function confirmAssignName() {
    const name = document.getElementById('assign-name-input').value.trim();

    if (!name) {
        alert('Please enter a name');
        return;
    }

    if (selectedDetections.size === 0) {
        return;
    }

    try {
        const response = await fetch('/api/person-detections/assign-name', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                detection_ids: Array.from(selectedDetections),
                person_name: name
            })
        });

        const data = await response.json();

        if (data.success) {
            closeAssignModal();
            selectedDetections.clear();
            await loadPersonDetections();
            await loadRecentNames();
            showSuccess(`Assigned ${data.updated_count} detection(s) to "${name}"`);
        } else {
            showError('Failed to assign name: ' + data.error);
        }
    } catch (error) {
        console.error('Error assigning name:', error);
        showError('Error assigning name');
    }
}

async function bulkUnassign() {
    if (selectedDetections.size === 0) return;

    if (!confirm(`Remove name assignment from ${selectedDetections.size} detection(s)?`)) {
        return;
    }

    try {
        const response = await fetch('/api/person-detections/unassign-name', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                detection_ids: Array.from(selectedDetections)
            })
        });

        const data = await response.json();

        if (data.success) {
            selectedDetections.clear();
            await loadPersonDetections();
            showSuccess(`Unassigned ${data.updated_count} detection(s)`);
        } else {
            showError('Failed to unassign: ' + data.error);
        }
    } catch (error) {
        console.error('Error unassigning:', error);
        showError('Error unassigning name');
    }
}

function applyCropView(thumbDiv, img, detection) {
    // Remove any existing bbox overlay
    const existing = thumbDiv.querySelector('.bbox-overlay');
    if (existing) existing.remove();

    if (!img.naturalWidth || detection.bbox_x == null) return;

    const natW = img.naturalWidth;
    const natH = img.naturalHeight;
    const bx = detection.bbox_x;
    const by = detection.bbox_y;
    const bw = detection.bbox_width;
    const bh = detection.bbox_height;

    // Padded crop region (1% padding)
    const padX = bw * 0.01;
    const padY = bh * 0.01;
    const cropX = Math.max(0, bx - padX);
    const cropY = Math.max(0, by - padY);
    const cropR = Math.min(natW, bx + bw + padX);
    const cropB = Math.min(natH, by + bh + padY);
    const cropW = cropR - cropX;
    const cropH = cropB - cropY;

    // Draw cropped region to canvas and replace img src with data URL
    try {
        const canvas = document.createElement('canvas');
        // Output at 2x for retina sharpness, capped at reasonable size
        const outW = Math.min(cropW * 2, 600);
        const outH = Math.min(cropH * 2, 600);
        const scale = Math.min(outW / cropW, outH / cropH);
        canvas.width = Math.round(cropW * scale);
        canvas.height = Math.round(cropH * scale);

        const ctx = canvas.getContext('2d');
        ctx.drawImage(img, cropX, cropY, cropW, cropH, 0, 0, canvas.width, canvas.height);

        // Store original src for full view toggle
        if (!img.dataset.originalSrc) {
            img.dataset.originalSrc = img.src;
        }
        img.src = canvas.toDataURL('image/jpeg', 0.9);
    } catch (e) {
        console.error('Crop failed:', e);
        return;
    }

    // Reset all styles to default
    img.style.cssText = 'width:100%;height:100%;object-fit:cover;';

    thumbDiv.setAttribute('data-mode', 'crop');
}

function applyFullView(thumbDiv, img, detection) {
    // Restore original image src
    if (img.dataset.originalSrc) {
        img.src = img.dataset.originalSrc;
    }
    img.style.cssText = 'width:100%;height:100%;object-fit:contain;';

    // Add bbox overlay
    const existing = thumbDiv.querySelector('.bbox-overlay');
    if (existing) existing.remove();

    if (detection.bbox_x != null && detection.bbox_width > 0 && img.naturalWidth) {
        const containerW = thumbDiv.offsetWidth;
        const containerH = thumbDiv.offsetHeight;
        const natW = img.naturalWidth;
        const natH = img.naturalHeight;

        // Calculate how object-fit:contain positions the image
        const imgRatio = natW / natH;
        const contRatio = containerW / containerH;
        let dispW, dispH, offX, offY;
        if (imgRatio > contRatio) {
            dispW = containerW;
            dispH = containerW / imgRatio;
            offX = 0;
            offY = (containerH - dispH) / 2;
        } else {
            dispH = containerH;
            dispW = containerH * imgRatio;
            offX = (containerW - dispW) / 2;
            offY = 0;
        }

        const scaleX = dispW / natW;
        const scaleY = dispH / natH;

        const bbox = document.createElement('div');
        bbox.className = 'bbox-overlay';
        bbox.style.left = (offX + detection.bbox_x * scaleX) + 'px';
        bbox.style.top = (offY + detection.bbox_y * scaleY) + 'px';
        bbox.style.width = (detection.bbox_width * scaleX) + 'px';
        bbox.style.height = (detection.bbox_height * scaleY) + 'px';
        thumbDiv.appendChild(bbox);
    }

    thumbDiv.setAttribute('data-mode', 'full');
}

function toggleThumbnailView(thumbDiv, img, detection) {
    const mode = thumbDiv.getAttribute('data-mode');
    if (mode === 'crop') {
        applyFullView(thumbDiv, img, detection);
    } else {
        // Return to crop view; double-click full view navigates
        applyCropView(thumbDiv, img, detection);
    }
}

function viewDetection(videoId, timestamp) {
    // Open annotation page at specific timestamp
    window.open('/annotate?id=' + videoId + '&t=' + timestamp, '_blank');
}

function filterDetections(query) {
    const filtered = allDetections.filter(d => {
        const searchStr = `${d.video_title} ${d.person_name || 'unknown'}`.toLowerCase();
        return searchStr.includes(query.toLowerCase());
    });
    renderDetections(filtered);
}

function sortDetections(sortBy) {
    let sorted = [...allDetections];

    switch(sortBy) {
        case 'recent':
            sorted.sort((a, b) => new Date(b.created_date) - new Date(a.created_date));
            break;
        case 'oldest':
            sorted.sort((a, b) => new Date(a.created_date) - new Date(b.created_date));
            break;
        case 'video':
            sorted.sort((a, b) => a.video_title.localeCompare(b.video_title));
            break;
        case 'timestamp':
            sorted.sort((a, b) => a.timestamp - b.timestamp);
            break;
    }

    allDetections = sorted;
    renderDetections(getCurrentFilteredDetections());
}

function formatTimestamp(seconds) {
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    return `${mins}:${secs.toString().padStart(2, '0')}`;
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function showSuccess(message) {
    // Simple alert for now - could be enhanced with a toast notification
    alert(message);
}

function showError(message) {
    alert('Error: ' + message);
}
