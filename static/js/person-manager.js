/**
 * Person Name Manager
 * Manage person identifications across all annotations
 */

let allDetections = [];
let selectedDetections = new Set();
let currentFilter = null; // null = all, 'unidentified', 'ambiguous', 'anonymous:GroupName', or person name
let currentView = 'detections'; // 'detections' = individual cards, 'groups' = one card per person
let currentStatFilter = null; // which stat card is active: null, 'named', 'unidentified', 'ambiguous', 'anonymous'
let recentNames = [];
let cachedPeople = null; // store people data for grouped rendering

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

    // Bulk actions (may not exist if read-only)
    const selectAllBtn = document.getElementById('select-all-btn');
    if (selectAllBtn) selectAllBtn.addEventListener('click', toggleSelectAll);
    const bulkAssignBtn = document.getElementById('bulk-assign-btn');
    if (bulkAssignBtn) bulkAssignBtn.addEventListener('click', openBulkAssignModal);
    const bulkUnassignBtn = document.getElementById('bulk-unassign-btn');
    if (bulkUnassignBtn) bulkUnassignBtn.addEventListener('click', bulkUnassign);

    // New category action buttons (only exist if can_write)
    const groupAnonBtn = document.getElementById('bulk-group-anonymous-btn');
    if (groupAnonBtn) groupAnonBtn.addEventListener('click', bulkGroupAnonymous);
    const markAmbigBtn = document.getElementById('bulk-mark-ambiguous-btn');
    if (markAmbigBtn) markAmbigBtn.addEventListener('click', bulkMarkAmbiguous);
    const autoClusterBtn = document.getElementById('auto-cluster-btn');
    if (autoClusterBtn) autoClusterBtn.addEventListener('click', autoClusterUnidentified);

    // Name input autocomplete (may not exist if read-only)
    const nameInput = document.getElementById('assign-name-input');
    if (nameInput) {
        nameInput.addEventListener('input', (e) => {
            showNameSuggestions(e.target.value);
        });
        nameInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                confirmAssignName();
            }
        });
    }

    // Close modal on background click
    const assignModal = document.getElementById('assign-modal');
    if (assignModal) {
        assignModal.addEventListener('click', (e) => {
            if (e.target.id === 'assign-modal') {
                closeAssignModal();
            }
        });
    }
}

async function loadPersonDetections() {
    try {
        const response = await fetch('/api/person-detections');
        const data = await response.json();

        if (data.success) {
            allDetections = data.detections;
            cachedPeople = data.people;
            updateStats(data.stats);
            renderPersonNames(data.people);
            // Re-apply current view mode after reload
            if (currentView === 'groups' && (currentStatFilter === 'named' || currentStatFilter === 'anonymous')) {
                renderGroupedView(currentStatFilter);
            } else {
                renderDetections(getCurrentFilteredDetections());
            }
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
    document.getElementById('stat-unidentified').textContent = stats.unidentified;
    document.getElementById('stat-ambiguous').textContent = stats.ambiguous;
    document.getElementById('stat-anonymous-groups').textContent = stats.anonymous_groups;
}

function renderPersonNames(people) {
    const container = document.getElementById('person-names-list');

    if (!people) {
        container.innerHTML = '<div class="empty-state">No people detected yet</div>';
        return;
    }

    let html = '';

    // Show All button at top
    html += `
        <div class="show-all-item ${currentFilter === null ? 'active' : ''}" onclick="filterByPerson(null)">
            Show All
        </div>
    `;

    // Fixed items: Unidentified and Ambiguous
    if (people.unidentified > 0) {
        html += `
            <div class="person-name-item category-unidentified ${currentFilter === 'unidentified' ? 'active' : ''}" onclick="filterByPerson('unidentified')">
                <span class="name">Unidentified</span>
                <span class="count">${people.unidentified}</span>
            </div>
        `;
    }
    if (people.ambiguous > 0) {
        html += `
            <div class="person-name-item category-ambiguous ${currentFilter === 'ambiguous' ? 'active' : ''}" onclick="filterByPerson('ambiguous')">
                <span class="name">Ambiguous</span>
                <span class="count">${people.ambiguous}</span>
            </div>
        `;
    }

    // Named People section
    if (people.named && people.named.length > 0) {
        html += '<div class="sidebar-section-label">Named People</div>';
        people.named.forEach(person => {
            const isActive = currentFilter === person.name;
            html += `
                <div class="person-name-item category-named ${isActive ? 'active' : ''}" onclick="filterByPerson('${escapeHtml(person.name)}')">
                    <span class="name">${escapeHtml(person.name)}</span>
                    <span class="count">${person.count}</span>
                </div>
            `;
        });
    }

    // Anonymous Groups section
    if (people.anonymous && people.anonymous.length > 0) {
        html += '<div class="sidebar-section-label">Anonymous Groups</div>';
        people.anonymous.forEach(person => {
            const isActive = currentFilter === 'anonymous:' + person.name;
            html += `
                <div class="person-name-item category-anonymous ${isActive ? 'active' : ''}" onclick="filterByPerson('anonymous:${escapeHtml(person.name)}')">
                    <span class="name">${escapeHtml(person.name)}</span>
                    <span class="count">${person.count}</span>
                </div>
            `;
        });
    }

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
        const personName = detection.display_name || 'Unidentified';
        const personStatus = detection.person_status || 'unidentified';

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
        img.src = detection.thumbnail_path ? detection.thumbnail_path + '?full=1' : '';
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
        nameTag.className = 'tag tag-' + personStatus;
        nameTag.textContent = personName;
        tagsDiv.appendChild(nameTag);

        if (detection.source === 'prediction') {
            const srcTag = document.createElement('span');
            srcTag.className = 'tag tag-source-prediction';
            srcTag.textContent = detection.review_status === 'approved' ? 'ML' : 'ML (pending)';
            tagsDiv.appendChild(srcTag);
        }
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

function filterByStat(statType) {
    currentStatFilter = statType;

    // Update active stat card
    document.querySelectorAll('.stat-clickable').forEach(c => c.classList.remove('active'));
    if (event && event.currentTarget) {
        event.currentTarget.classList.add('active');
    }

    if (statType === 'named') {
        // Show grouped view - one card per named person
        currentView = 'groups';
        currentFilter = null;
        document.getElementById('detections-title').textContent = 'Named People';
        const count = cachedPeople ? cachedPeople.named.length : 0;
        document.getElementById('detections-subtitle').textContent = `${count} person(s)`;
        renderGroupedView('named');
        updateSidebarActive(null);
    } else if (statType === 'anonymous') {
        // Show grouped view - one card per anonymous group
        currentView = 'groups';
        currentFilter = null;
        document.getElementById('detections-title').textContent = 'Anonymous Groups';
        const count = cachedPeople ? cachedPeople.anonymous.length : 0;
        document.getElementById('detections-subtitle').textContent = `${count} group(s)`;
        renderGroupedView('anonymous');
        updateSidebarActive(null);
    } else if (statType === 'unidentified') {
        currentView = 'detections';
        filterByPerson('unidentified');
    } else if (statType === 'ambiguous') {
        currentView = 'detections';
        filterByPerson('ambiguous');
    } else {
        // Total - show all detections
        currentView = 'detections';
        currentStatFilter = null;
        filterByPerson(null);
    }
}

function renderGroupedView(groupType) {
    const container = document.getElementById('detections-grid');

    if (!cachedPeople) {
        container.innerHTML = '<div class="empty-state"><p>No data available</p></div>';
        return;
    }

    const groups = groupType === 'named' ? cachedPeople.named : cachedPeople.anonymous;

    if (!groups || groups.length === 0) {
        container.innerHTML = `
            <div class="empty-state">
                <div class="empty-state-icon">🔍</div>
                <p>No ${groupType === 'named' ? 'named people' : 'anonymous groups'} found</p>
            </div>
        `;
        return;
    }

    container.textContent = '';
    groups.forEach(group => {
        // Get detections for this group
        let groupDetections;
        if (groupType === 'named') {
            groupDetections = allDetections.filter(d => d.person_status === 'named' && d.person_name === group.name);
        } else {
            groupDetections = allDetections.filter(d => d.person_status === 'anonymous' && d.person_name === group.name);
        }

        const card = document.createElement('div');
        card.className = 'group-card';
        card.onclick = () => {
            // Drill down into this person's detections
            currentView = 'detections';
            if (groupType === 'named') {
                filterByPerson(group.name);
            } else {
                filterByPerson('anonymous:' + group.name);
            }
        };

        // Thumbnail grid - show up to 6 sample images
        const thumbGrid = document.createElement('div');
        thumbGrid.className = 'group-card-thumbnails';
        const samples = groupDetections.slice(0, 6);
        for (let i = 0; i < 6; i++) {
            if (i < samples.length && samples[i].thumbnail_path) {
                const img = document.createElement('img');
                img.alt = group.name;
                img.src = samples[i].thumbnail_path ? samples[i].thumbnail_path + '?full=1' : '';
                // Apply crop if bbox available
                const det = samples[i];
                if (det.bbox_x != null && det.bbox_width > 0) {
                    img.decode().then(() => {
                        try {
                            const natW = img.naturalWidth;
                            const natH = img.naturalHeight;
                            const padX = det.bbox_width * 0.01;
                            const padY = det.bbox_height * 0.01;
                            const cx = Math.max(0, det.bbox_x - padX);
                            const cy = Math.max(0, det.bbox_y - padY);
                            const cw = Math.min(natW, det.bbox_x + det.bbox_width + padX) - cx;
                            const ch = Math.min(natH, det.bbox_y + det.bbox_height + padY) - cy;
                            const canvas = document.createElement('canvas');
                            const scale = Math.min(300 / cw, 300 / ch);
                            canvas.width = Math.round(cw * scale);
                            canvas.height = Math.round(ch * scale);
                            const ctx = canvas.getContext('2d');
                            ctx.drawImage(img, cx, cy, cw, ch, 0, 0, canvas.width, canvas.height);
                            img.src = canvas.toDataURL('image/jpeg', 0.85);
                        } catch(e) {}
                    }).catch(() => {});
                }
                thumbGrid.appendChild(img);
            } else {
                const slot = document.createElement('div');
                slot.className = 'thumb-slot';
                thumbGrid.appendChild(slot);
            }
        }

        // Info section
        const info = document.createElement('div');
        info.className = 'group-card-info';

        const nameDiv = document.createElement('div');
        nameDiv.className = 'group-name';
        const statusClass = groupType === 'named' ? 'category-named' : 'category-anonymous';
        nameDiv.innerHTML = `<span class="${statusClass}" style="border:none;padding:0;"><span class="name">${escapeHtml(group.name)}</span></span>`;

        const metaDiv = document.createElement('div');
        metaDiv.className = 'group-meta';
        metaDiv.innerHTML = `${group.count} detection${group.count !== 1 ? 's' : ''}`;
        // Show video count
        const videoSet = new Set(groupDetections.map(d => d.video_id));
        if (videoSet.size > 0) {
            metaDiv.innerHTML += ` &middot; ${videoSet.size} video${videoSet.size !== 1 ? 's' : ''}`;
        }

        info.appendChild(nameDiv);
        info.appendChild(metaDiv);

        card.appendChild(thumbGrid);
        card.appendChild(info);
        container.appendChild(card);
    });
}

function updateSidebarActive(filterValue) {
    document.querySelectorAll('.person-name-item, .show-all-item').forEach(item => {
        item.classList.remove('active');
    });
    if (filterValue === null) {
        const showAll = document.querySelector('.show-all-item');
        if (showAll) showAll.classList.add('active');
    }
}

function filterByPerson(personName) {
    currentFilter = personName;
    currentView = 'detections';

    // Clear stat card active state unless called from filterByStat
    if (!currentStatFilter || currentStatFilter === 'named' || currentStatFilter === 'anonymous') {
        // Only clear if drilling down from groups or clicking sidebar
        if (personName !== 'unidentified' && personName !== 'ambiguous') {
            document.querySelectorAll('.stat-clickable').forEach(c => c.classList.remove('active'));
            currentStatFilter = null;
        }
    }

    let filtered;
    if (personName === 'unidentified') {
        filtered = allDetections.filter(d => d.person_status === 'unidentified');
        document.getElementById('detections-title').textContent = 'Unidentified Detections';
    } else if (personName === 'ambiguous') {
        filtered = allDetections.filter(d => d.person_status === 'ambiguous');
        document.getElementById('detections-title').textContent = 'Ambiguous Detections';
    } else if (personName && personName.startsWith('anonymous:')) {
        const groupName = personName.substring('anonymous:'.length);
        filtered = allDetections.filter(d => d.person_status === 'anonymous' && d.person_name === groupName);
        document.getElementById('detections-title').textContent = `Detections: ${groupName}`;
    } else if (personName === null) {
        filtered = allDetections;
        document.getElementById('detections-title').textContent = 'All Detections';
    } else {
        filtered = allDetections.filter(d => d.person_name === personName);
        document.getElementById('detections-title').textContent = `Detections: ${personName}`;
    }

    document.getElementById('detections-subtitle').textContent = `${filtered.length} detection(s)`;

    // Load documents for named persons
    if (personName && personName !== 'unidentified' && personName !== 'ambiguous' && !personName.startsWith('anonymous:')) {
        loadPersonDocuments(personName).then(docs => renderDocumentsSection(docs));
    } else {
        const existingDocSection = document.getElementById('person-documents-section');
        if (existingDocSection) existingDocSection.remove();
    }

    renderDetections(filtered);

    // Update active state in person list
    document.querySelectorAll('.person-name-item, .show-all-item').forEach(item => {
        item.classList.remove('active');
    });
    if (event && event.target) {
        const clickedItem = event.target.closest('.person-name-item, .show-all-item');
        if (clickedItem) clickedItem.classList.add('active');
    }
}

function toggleDetectionSelect(detectionId) {
    if (selectedDetections.has(detectionId)) {
        selectedDetections.delete(detectionId);
    } else {
        selectedDetections.add(detectionId);
    }

    // Update just the toggled card instead of re-rendering the entire grid
    const card = document.querySelector(`.detection-card[data-id="${detectionId}"]`);
    if (card) {
        const isSelected = selectedDetections.has(detectionId);
        card.classList.toggle('selected', isSelected);
        const cb = card.querySelector('.detection-checkbox');
        if (cb) cb.checked = isSelected;
    }
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
    if (currentFilter === 'unidentified') {
        return allDetections.filter(d => d.person_status === 'unidentified');
    } else if (currentFilter === 'ambiguous') {
        return allDetections.filter(d => d.person_status === 'ambiguous');
    } else if (currentFilter && currentFilter.startsWith('anonymous:')) {
        const groupName = currentFilter.substring('anonymous:'.length);
        return allDetections.filter(d => d.person_status === 'anonymous' && d.person_name === groupName);
    } else if (currentFilter) {
        return allDetections.filter(d => d.person_name === currentFilter);
    }
    return allDetections;
}

function updateBulkActionButtons() {
    const hasSelection = selectedDetections.size > 0;
    const assignBtn = document.getElementById('bulk-assign-btn');
    if (assignBtn) assignBtn.disabled = !hasSelection;
    const unassignBtn = document.getElementById('bulk-unassign-btn');
    if (unassignBtn) unassignBtn.disabled = !hasSelection;
    const groupBtn = document.getElementById('bulk-group-anonymous-btn');
    if (groupBtn) groupBtn.disabled = !hasSelection;
    const ambigBtn = document.getElementById('bulk-mark-ambiguous-btn');
    if (ambigBtn) ambigBtn.disabled = !hasSelection;
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

async function bulkMarkAmbiguous() {
    if (selectedDetections.size === 0) return;

    if (!confirm(`Mark ${selectedDetections.size} detection(s) as ambiguous?`)) {
        return;
    }

    try {
        const response = await fetch('/api/person-detections/mark-ambiguous', {
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
            showSuccess(`Marked ${data.updated_count} detection(s) as ambiguous`);
        } else {
            showError('Failed to mark ambiguous: ' + data.error);
        }
    } catch (error) {
        console.error('Error marking ambiguous:', error);
        showError('Error marking detections as ambiguous');
    }
}

async function bulkGroupAnonymous() {
    if (selectedDetections.size === 0) return;

    if (!confirm(`Group ${selectedDetections.size} detection(s) as a new anonymous group?`)) {
        return;
    }

    try {
        const response = await fetch('/api/person-detections/group-anonymous', {
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
            showSuccess(`Grouped ${data.updated_count} detection(s) as "${data.group_name}"`);
        } else {
            showError('Failed to group anonymous: ' + data.error);
        }
    } catch (error) {
        console.error('Error grouping anonymous:', error);
        showError('Error creating anonymous group');
    }
}

async function autoClusterUnidentified() {
    if (!confirm('Run auto-clustering on face embeddings? This may take a moment.')) {
        return;
    }

    try {
        const btn = document.getElementById('auto-cluster-btn');
        if (btn) {
            btn.disabled = true;
            btn.textContent = 'Clustering...';
        }

        const response = await fetch('/api/person-detections/auto-cluster', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({})
        });

        const data = await response.json();

        if (data.success) {
            selectedDetections.clear();
            await loadPersonDetections();
            showSuccess(`${data.message} (${data.detections_assigned} detections assigned)`);
        } else {
            showError('Auto-cluster failed: ' + data.error);
        }
    } catch (error) {
        console.error('Error auto-clustering:', error);
        showError('Error running auto-cluster');
    } finally {
        const btn = document.getElementById('auto-cluster-btn');
        if (btn) {
            btn.disabled = false;
            btn.textContent = 'Auto-Cluster';
        }
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
        const searchStr = `${d.video_title} ${d.display_name || ''} ${d.person_name || ''} ${d.person_status || ''} ${d.source || ''}`.toLowerCase();
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

async function loadPersonDocuments(personName) {
    try {
        // Search for identity by person name, then fetch linked documents
        const searchResp = await fetch('/api/persons/search?q=' + encodeURIComponent(personName));
        const searchData = await searchResp.json();

        if (!searchData.success || !searchData.persons || searchData.persons.length === 0) {
            return [];
        }

        const identityId = searchData.persons[0].id;
        const docResp = await fetch('/api/documents/by-identity/' + identityId);
        const docData = await docResp.json();

        if (docData.success) {
            return docData.documents || [];
        }
        return [];
    } catch (error) {
        console.error('Error loading person documents:', error);
        return [];
    }
}

function renderDocumentsSection(documents) {
    let existing = document.getElementById('person-documents-section');
    if (existing) existing.remove();

    if (!documents || documents.length === 0) return;

    const section = document.createElement('div');
    section.id = 'person-documents-section';
    section.style.cssText = 'margin-top:16px;padding:12px;background:#1a1a2e;border-radius:8px;border:1px solid #2d2d44;';

    const heading = document.createElement('h4');
    heading.style.cssText = 'margin:0 0 8px 0;color:#9333EA;font-size:14px;';
    heading.textContent = 'Linked Documents (' + documents.length + ')';
    section.appendChild(heading);

    documents.forEach(doc => {
        const row = document.createElement('div');
        row.style.cssText = 'display:flex;align-items:center;gap:8px;padding:6px;margin-bottom:4px;background:#16213e;border-radius:4px;cursor:pointer;';

        if (doc.crop_image_path) {
            const thumb = document.createElement('img');
            thumb.src = doc.crop_image_path;
            thumb.style.cssText = 'width:40px;height:28px;object-fit:cover;border-radius:3px;';
            thumb.alt = doc.document_type || 'Document';
            row.appendChild(thumb);
        }

        const info = document.createElement('div');
        info.style.cssText = 'flex:1;min-width:0;';

        const typeLine = document.createElement('div');
        typeLine.style.cssText = 'font-size:12px;font-weight:600;color:#e0e0e0;';
        const typeStr = (doc.document_type || 'unknown').replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
        typeLine.textContent = typeStr;

        const detailLine = document.createElement('div');
        detailLine.style.cssText = 'font-size:11px;color:#95a5a6;';
        const parts = [];
        if (doc.document_number) parts.push('#' + doc.document_number);
        if (doc.expiry_date) parts.push('Exp: ' + doc.expiry_date);
        detailLine.textContent = parts.join(' · ') || 'No details';

        info.appendChild(typeLine);
        info.appendChild(detailLine);
        row.appendChild(info);

        const statusDot = document.createElement('span');
        statusDot.style.cssText = 'width:8px;height:8px;border-radius:50%;flex-shrink:0;background:' + (doc.ocr_completed ? '#2ecc71' : '#f39c12') + ';';
        statusDot.title = doc.ocr_completed ? 'OCR Complete' : 'OCR Pending';
        row.appendChild(statusDot);

        if (doc.prediction_id) {
            row.onclick = () => {
                window.location.href = '/review?highlight=' + doc.prediction_id;
            };
        }

        section.appendChild(row);
    });

    // Insert after detections grid or at end of main content
    const grid = document.getElementById('detections-grid');
    if (grid && grid.parentNode) {
        grid.parentNode.insertBefore(section, grid.nextSibling);
    }
}

function showError(message) {
    alert('Error: ' + message);
}
