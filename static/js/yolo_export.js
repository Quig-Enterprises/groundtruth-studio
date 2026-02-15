let activityTags = [];

// Load configurations on page load
async function loadConfigs() {
    try {
        const response = await fetch('/api/yolo/configs');
        const data = await response.json();

        if (data.success) {
            displayConfigs(data.configs);
        }
    } catch (error) {
        console.error('Error loading configs:', error);
    }
}

function displayConfigs(configs) {
    const container = document.getElementById('configs-list');

    if (configs.length === 0) {
        container.innerHTML = `
            <div style="text-align: center; padding: 60px; color: #95a5a6;">
                <h3>No Export Configurations Yet</h3>
                <p>Create your first YOLO export configuration to get started</p>
                <button onclick="openNewConfigModal()" class="btn-primary" style="margin-top: 20px;">+ Create Configuration</button>
            </div>
        `;
        return;
    }

    container.innerHTML = configs.map(config => `
        <div class="config-card">
            <div class="config-header">
                <div>
                    <div class="config-title">${escapeHtml(config.config_name)}</div>
                    <div class="config-meta">
                        ${config.description ? escapeHtml(config.description) : 'No description'}
                        <br>Created: ${new Date(config.created_date).toLocaleString()}
                    </div>
                </div>
                <div class="config-actions">
                    <button onclick="previewExport(${config.id})" class="btn-secondary">Preview</button>
                    <button onclick="exportDataset(${config.id})" class="btn-primary">Export</button>
                    <button onclick="exportAndTrainConfig(${config.id})" class="btn-primary" style="background-color: #27ae60;">Export &amp; Train</button>
                </div>
            </div>

            <div class="class-list">
                ${Object.entries(config.class_mapping).sort((a, b) => a[1] - b[1]).map(([name, id]) =>
                    `<span class="class-badge">${id}: ${escapeHtml(name)}</span>`
                ).join('')}
            </div>

            <div class="export-stats">
                <div class="stat-item">
                    <div class="stat-value">${config.last_export_count || 0}</div>
                    <div class="stat-label">Last Export</div>
                </div>
                <div class="stat-item">
                    <div class="stat-label" style="font-size: 11px;">Last Export Date</div>
                    <div style="font-size: 13px; color: #7f8c8d; margin-top: 3px;">
                        ${config.last_export_date ? new Date(config.last_export_date).toLocaleDateString() : 'Never'}
                    </div>
                </div>
                <div class="stat-item">
                    <div class="stat-label" style="font-size: 11px;">Settings</div>
                    <div style="font-size: 11px; color: #7f8c8d; margin-top: 3px;">
                        ${config.include_reviewed_only ? '✓ Reviewed Only' : ''}<br>
                        ${config.include_ai_generated ? '✓ AI Generated' : ''}<br>
                        ${config.include_negative_examples ? '✓ Negatives' : ''}
                    </div>
                </div>
            </div>
        </div>
    `).join('');
}

// New Config Modal
function openNewConfigModal() {
    document.getElementById('new-config-modal').style.display = 'block';
    loadActivityTags();
    initializeClassMapping();
}

function closeNewConfigModal() {
    document.getElementById('new-config-modal').style.display = 'none';
    document.getElementById('new-config-form').reset();
}

async function loadActivityTags() {
    try {
        const response = await fetch('/api/yolo/activity-tags');
        const data = await response.json();

        if (data.success) {
            activityTags = data.tags;
        }
    } catch (error) {
        console.error('Error loading activity tags:', error);
    }
}

function initializeClassMapping() {
    const editor = document.getElementById('class-mapping-editor');
    editor.innerHTML = `
        <div style="display: flex; gap: 10px; margin-bottom: 10px; font-weight: 600; font-size: 13px; color: #2c3e50;">
            <div style="flex: 2;">Activity Tag</div>
            <div style="flex: 1;">Class ID (auto)</div>
            <div style="width: 32px;"></div>
        </div>
    `;

    // Add first class automatically
    if (activityTags.length > 0) {
        addClassMapping();
    }
}

function addClassMapping() {
    const editor = document.getElementById('class-mapping-editor');

    // Calculate next available class ID by finding max of existing IDs + 1
    const existingRows = document.querySelectorAll('.class-row');
    let nextId = 0;

    existingRows.forEach(row => {
        const classId = parseInt(row.querySelector('.class-id').value);
        if (!isNaN(classId) && classId >= nextId) {
            nextId = classId + 1;
        }
    });

    const row = document.createElement('div');
    row.className = 'class-row';
    row.innerHTML = `
        <select class="class-activity-tag" style="flex: 2;">
            <option value="">Select activity tag...</option>
            ${activityTags.map(tag =>
                `<option value="${escapeHtml(tag.name)}">${escapeHtml(tag.name)} (${tag.count})</option>`
            ).join('')}
        </select>
        <input type="number" class="class-id" placeholder="Class ID" value="${nextId}" min="0" style="flex: 1; background-color: #ecf0f1; cursor: not-allowed;" readonly />
        <button type="button" onclick="this.parentElement.remove()" class="btn-danger btn-small">✕</button>
    `;
    editor.appendChild(row);
}

// Form submission
document.getElementById('new-config-form').addEventListener('submit', async (e) => {
    e.preventDefault();

    const configName = document.getElementById('config-name').value.trim();
    const description = document.getElementById('config-description').value.trim();

    // Build class mapping
    const classMapping = {};
    const rows = document.querySelectorAll('.class-row');

    for (const row of rows) {
        const activityTag = row.querySelector('.class-activity-tag').value;
        const classId = parseInt(row.querySelector('.class-id').value);

        if (activityTag && !isNaN(classId)) {
            classMapping[activityTag] = classId;
        }
    }

    if (Object.keys(classMapping).length === 0) {
        alert('Please add at least one class mapping');
        return;
    }

    const requestData = {
        config_name: configName,
        description: description,
        class_mapping: classMapping,
        include_reviewed_only: document.getElementById('include-reviewed-only').checked,
        include_ai_generated: document.getElementById('include-ai-generated').checked,
        include_negative_examples: document.getElementById('include-negative').checked
    };

    try {
        const response = await fetch('/api/yolo/configs', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(requestData)
        });

        const data = await response.json();

        if (data.success) {
            closeNewConfigModal();
            loadConfigs();
        } else {
            alert('Error creating configuration: ' + data.error);
        }
    } catch (error) {
        alert('Error: ' + error.message);
    }
});

// Preview export
async function previewExport(configId) {
    try {
        const response = await fetch(`/api/yolo/configs/${configId}/preview`);
        const data = await response.json();

        if (data.success) {
            const preview = data.preview;
            const modal = document.getElementById('preview-modal');
            const content = document.getElementById('preview-content');

            const classDist = Object.entries(preview.class_distribution)
                .sort((a, b) => b[1] - a[1])
                .map(([name, count]) => `<li>${escapeHtml(name)}: <strong>${count}</strong> annotations</li>`)
                .join('');

            content.innerHTML = `
                <div class="export-stats">
                    <div class="stat-item">
                        <div class="stat-value">${preview.video_count}</div>
                        <div class="stat-label">Videos</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-value">${preview.total_annotations}</div>
                        <div class="stat-label">Annotations</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-value">${Object.keys(preview.class_distribution).length}</div>
                        <div class="stat-label">Classes</div>
                    </div>
                </div>

                <h3 style="margin-top: 30px;">Class Distribution</h3>
                <ul style="margin-top: 10px;">
                    ${classDist}
                </ul>

                <div style="margin-top: 30px; display: flex; gap: 10px;">
                    <button onclick="closePreviewModal()" class="btn-secondary">Close</button>
                    <button onclick="closePreviewModal(); exportDataset(${configId})" class="btn-primary">Export Dataset</button>
                </div>
            `;

            modal.style.display = 'block';
        }
    } catch (error) {
        alert('Error loading preview: ' + error.message);
    }
}

function closePreviewModal() {
    document.getElementById('preview-modal').style.display = 'none';
}

// Export and train dataset
async function exportAndTrainConfig(configId) {
    const statusDiv = document.createElement('div');
    statusDiv.className = 'status-message';
    statusDiv.textContent = 'Exporting & submitting training job...';
    document.getElementById('configs-list').prepend(statusDiv);

    try {
        const response = await fetch('/api/training/export-and-train', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                job_type: 'yolo',
                export_config_id: configId,
                model_type: 'yolov8n',
                epochs: 100
            })
        });
        const data = await response.json();

        if (data.success) {
            const jobId = data.job?.job_id || '';
            statusDiv.className = 'status-message success';
            statusDiv.textContent = 'Training job submitted: ' + jobId.substring(0, 8) + '...';
            setTimeout(function() { statusDiv.remove(); }, 5000);
        } else {
            statusDiv.className = 'status-message error';
            statusDiv.textContent = 'Error: ' + (data.error || 'Failed');
        }
    } catch (e) {
        statusDiv.className = 'status-message error';
        statusDiv.textContent = 'Error: ' + e.message;
    }
}

// Export dataset
async function exportDataset(configId) {
    if (!confirm('Export this dataset? This may take several minutes for large datasets.')) {
        return;
    }

    const statusDiv = document.createElement('div');
    statusDiv.className = 'status-message';
    statusDiv.textContent = 'Exporting dataset... This may take a while.';
    document.getElementById('configs-list').prepend(statusDiv);

    try {
        const response = await fetch(`/api/yolo/configs/${configId}/export`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({})
        });

        const data = await response.json();

        if (data.success) {
            statusDiv.className = 'status-message success';
            statusDiv.innerHTML = `
                <strong>Export Complete!</strong><br>
                Path: ${escapeHtml(data.export_path)}<br>
                Videos: ${data.video_count} | Frames: ${data.frame_count} | Annotations: ${data.annotation_count}
            `;

            setTimeout(() => {
                statusDiv.remove();
                loadConfigs();
            }, 5000);
        } else {
            statusDiv.className = 'status-message error';
            statusDiv.textContent = 'Error: ' + data.error;
        }
    } catch (error) {
        statusDiv.className = 'status-message error';
        statusDiv.textContent = 'Error: ' + error.message;
    }
}

// Modal click outside
window.onclick = function(event) {
    const newConfigModal = document.getElementById('new-config-modal');
    const previewModal = document.getElementById('preview-modal');

    if (event.target === newConfigModal) {
        closeNewConfigModal();
    }
    if (event.target === previewModal) {
        closePreviewModal();
    }
}

// Utility function
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Initialize
loadConfigs();
