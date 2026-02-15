/**
 * Model Training - Unified Training Interface
 * Vanilla JavaScript implementation for dataset health, export, and training management
 */

// ============================================================================
// GLOBAL STATE
// ============================================================================

let currentConfigId = null;
let pollInterval = null;
let pollSpeed = 30000; // idle: 30s
const FAST_POLL = 5000; // active: 5s
const IDLE_POLL = 30000; // idle: 30s

// ============================================================================
// TOAST SYSTEM
// ============================================================================

/**
 * Show a toast notification
 * @param {string} message - Message to display
 * @param {string} type - 'success', 'error', 'warning', 'info'
 * @param {number} duration - Auto-dismiss after ms (0 for persistent)
 */
function showToast(message, type = 'info', duration = 5000) {
    const container = document.getElementById('toast-container');
    if (!container) return;

    // Create toast element
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;

    // Message text
    const messageSpan = document.createElement('span');
    messageSpan.textContent = message;
    toast.appendChild(messageSpan);

    // Close button
    const closeBtn = document.createElement('button');
    closeBtn.className = 'toast-close';
    closeBtn.textContent = '×';
    closeBtn.onclick = () => removeToast(toast);
    toast.appendChild(closeBtn);

    // Add to container
    container.appendChild(toast);

    // Limit to 5 toasts
    const toasts = container.querySelectorAll('.toast');
    if (toasts.length > 5) {
        removeToast(toasts[0]);
    }

    // Trigger animation
    setTimeout(() => toast.classList.add('show'), 10);

    // Auto-dismiss (except errors)
    if (type !== 'error' && duration > 0) {
        setTimeout(() => removeToast(toast), duration);
    }
}

/**
 * Remove a toast with animation
 */
function removeToast(toast) {
    toast.classList.remove('show');
    setTimeout(() => {
        if (toast.parentNode) {
            toast.parentNode.removeChild(toast);
        }
    }, 300);
}

/**
 * Show confirmation modal
 * @param {string} message - Confirmation message
 * @param {Function} onConfirm - Callback on confirm
 * @param {Function} onCancel - Optional callback on cancel
 */
function showConfirm(message, onConfirm, onCancel = null) {
    // Create overlay
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';

    // Create modal
    const modal = document.createElement('div');
    modal.className = 'modal-confirm';

    // Message
    const messageP = document.createElement('p');
    messageP.textContent = message;
    modal.appendChild(messageP);

    // Button container
    const btnContainer = document.createElement('div');
    btnContainer.className = 'modal-buttons';

    // Cancel button
    const cancelBtn = document.createElement('button');
    cancelBtn.className = 'btn btn-secondary';
    cancelBtn.textContent = 'Cancel';
    cancelBtn.onclick = () => {
        document.body.removeChild(overlay);
        if (onCancel) onCancel();
    };
    btnContainer.appendChild(cancelBtn);

    // Confirm button
    const confirmBtn = document.createElement('button');
    confirmBtn.className = 'btn btn-primary';
    confirmBtn.textContent = 'Confirm';
    confirmBtn.onclick = () => {
        document.body.removeChild(overlay);
        onConfirm();
    };
    btnContainer.appendChild(confirmBtn);

    modal.appendChild(btnContainer);
    overlay.appendChild(modal);
    document.body.appendChild(overlay);

    // Show with animation
    setTimeout(() => overlay.classList.add('show'), 10);
}

// ============================================================================
// UTILITY FUNCTIONS
// ============================================================================

/**
 * Fetch JSON wrapper with error handling
 */
async function fetchJSON(url, options = {}) {
    try {
        const response = await fetch(url, {
            ...options,
            headers: {
                'Content-Type': 'application/json',
                ...options.headers
            }
        });

        if (!response.ok) {
            const error = await response.json().catch(() => ({ error: response.statusText }));
            throw new Error(error.error || error.message || `HTTP ${response.status}`);
        }

        return await response.json();
    } catch (error) {
        console.error('Fetch error:', error);
        throw error;
    }
}

/**
 * Format ISO date to locale string
 */
function formatDate(isoStr) {
    if (!isoStr) return '-';
    try {
        // Postgres timestamps don't have timezone - treat as UTC
        const str = isoStr.endsWith('Z') ? isoStr : isoStr + 'Z';
        const date = new Date(str);
        if (isNaN(date.getTime())) return isoStr;
        return date.toLocaleString();
    } catch {
        return isoStr;
    }
}

/**
 * Format duration in seconds to human-readable
 */
function formatDuration(seconds) {
    if (!seconds || seconds < 0) return 'N/A';
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    if (mins > 0) {
        return `${mins}m ${secs}s`;
    }
    return `${secs}s`;
}

/**
 * Clear all children from an element
 */
function clearElement(element) {
    while (element.firstChild) {
        element.removeChild(element.firstChild);
    }
}

// ============================================================================
// CONFIG MANAGEMENT
// ============================================================================

/**
 * Load all configs and populate dropdown
 */
async function loadConfigs() {
    try {
        const data = await fetchJSON('/api/yolo/configs');

        if (!data.success) {
            throw new Error(data.error || 'Failed to load configs');
        }

        const select = document.getElementById('config-select');
        clearElement(select);

        // Add "Create New" option
        const createOption = document.createElement('option');
        createOption.value = '';
        createOption.textContent = '-- Create New --';
        select.appendChild(createOption);

        // Add configs
        data.configs.forEach(config => {
            const option = document.createElement('option');
            option.value = config.id;
            option.textContent = config.config_name;
            select.appendChild(option);
        });

        // Select first config if available
        if (data.configs.length > 0) {
            select.value = data.configs[0].id;
            await selectConfig(data.configs[0].id);
        } else {
            clearConfigEditor();
        }

    } catch (error) {
        showToast(`Failed to load configs: ${error.message}`, 'error');
    }
}

/**
 * Select and load a config
 */
async function selectConfig(configId) {
    if (!configId) {
        clearConfigEditor();
        currentConfigId = null;
        return;
    }

    try {
        const data = await fetchJSON(`/api/yolo/configs/${configId}`);

        if (!data.success) {
            throw new Error(data.error || 'Failed to load config');
        }

        currentConfigId = configId;
        populateConfigEditor(data.config);

        // Refresh related sections
        await Promise.all([
            refreshHealth(),
            loadJobs()
        ]);

    } catch (error) {
        showToast(`Failed to load config: ${error.message}`, 'error');
    }
}

/**
 * Clear config editor for new config
 */
function clearConfigEditor() {
    document.getElementById('config-name').value = '';
    document.getElementById('config-description').value = '';
    document.getElementById('include-reviewed-only').checked = false;
    document.getElementById('include-ai-generated').checked = true;
    document.getElementById('include-negative').checked = false;

    const mappingEditor = document.getElementById('class-mapping-editor');
    clearElement(mappingEditor);
    addClassMapping(); // Start with one empty row

    // Clear health section
    const healthStats = document.getElementById('health-stats');
    clearElement(healthStats);
    const msg = document.createElement('p');
    msg.textContent = 'Create or select a config to view health';
    healthStats.appendChild(msg);

    clearElement(document.getElementById('health-bars'));
    clearElement(document.getElementById('health-warnings'));

    // Clear jobs
    const jobsBody = document.getElementById('jobs-table-body');
    clearElement(jobsBody);
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 8;
    td.style.textAlign = 'center';
    td.textContent = 'No config selected';
    tr.appendChild(td);
    jobsBody.appendChild(tr);
}

/**
 * Populate config editor with existing config
 */
function populateConfigEditor(config) {
    document.getElementById('config-name').value = config.config_name || '';
    document.getElementById('config-description').value = config.description || '';
    document.getElementById('include-reviewed-only').checked = config.include_reviewed_only || false;
    document.getElementById('include-ai-generated').checked = config.include_ai_generated !== false;
    document.getElementById('include-negative').checked = config.include_negative_examples || false;

    // Populate class mappings
    const mappingEditor = document.getElementById('class-mapping-editor');
    clearElement(mappingEditor);

    if (config.class_mapping && Object.keys(config.class_mapping).length > 0) {
        Object.entries(config.class_mapping).forEach(([activityTag, classId]) => {
            addClassMapping(activityTag, classId);
        });
    } else {
        addClassMapping(); // Add one empty row
    }
}

/**
 * Add a class mapping row
 */
async function addClassMapping(activityTag = '', classId = '') {
    const mappingEditor = document.getElementById('class-mapping-editor');

    // Auto-increment class ID if not provided
    if (!classId) {
        const existingRows = mappingEditor.querySelectorAll('.mapping-row');
        const maxId = Array.from(existingRows).reduce((max, row) => {
            const input = row.querySelector('.class-id-input');
            const id = parseInt(input.value) || 0;
            return Math.max(max, id);
        }, -1);
        classId = maxId + 1;
    }

    const row = document.createElement('div');
    row.className = 'mapping-row';

    // Activity tag dropdown
    const tagSelect = document.createElement('select');
    tagSelect.className = 'activity-tag-select';
    tagSelect.required = true;

    // Load activity tags if not cached
    if (!window.cachedActivityTags) {
        await loadActivityTags();
    }

    const emptyOption = document.createElement('option');
    emptyOption.value = '';
    emptyOption.textContent = '-- Select Activity --';
    tagSelect.appendChild(emptyOption);

    (window.cachedActivityTags || []).forEach(tag => {
        const option = document.createElement('option');
        option.value = tag.name;
        option.textContent = `${tag.name} (${tag.count})`;
        if (tag.name === activityTag) option.selected = true;
        tagSelect.appendChild(option);
    });

    // Class ID input
    const classInput = document.createElement('input');
    classInput.type = 'number';
    classInput.className = 'class-id-input';
    classInput.value = classId;
    classInput.min = '0';
    classInput.required = true;

    // Delete button
    const deleteBtn = document.createElement('button');
    deleteBtn.type = 'button';
    deleteBtn.className = 'btn btn-danger btn-sm';
    deleteBtn.textContent = 'Delete';
    deleteBtn.onclick = () => deleteMapping(deleteBtn);

    row.appendChild(tagSelect);
    row.appendChild(classInput);
    row.appendChild(deleteBtn);
    mappingEditor.appendChild(row);
}

/**
 * Delete a mapping row
 */
function deleteMapping(btn) {
    const row = btn.closest('.mapping-row');
    const mappingEditor = document.getElementById('class-mapping-editor');

    // Ensure at least one row remains
    if (mappingEditor.querySelectorAll('.mapping-row').length > 1) {
        row.remove();
    } else {
        showToast('At least one class mapping is required', 'warning');
    }
}

/**
 * Load activity tags from API
 */
async function loadActivityTags() {
    try {
        const data = await fetchJSON('/api/yolo/activity-tags');

        if (data.success) {
            window.cachedActivityTags = data.tags;
        }
    } catch (error) {
        console.error('Failed to load activity tags:', error);
        window.cachedActivityTags = [];
    }
}

/**
 * Save current config
 */
async function saveConfig() {
    try {
        // Validate inputs
        const configName = document.getElementById('config-name').value.trim();
        if (!configName) {
            showToast('Config name is required', 'warning');
            return;
        }

        // Collect class mappings
        const mappingRows = document.querySelectorAll('.mapping-row');
        const classMapping = {};

        for (const row of mappingRows) {
            const tag = row.querySelector('.activity-tag-select').value;
            const classId = parseInt(row.querySelector('.class-id-input').value);

            if (!tag) {
                showToast('All activity tags must be selected', 'warning');
                return;
            }

            if (isNaN(classId) || classId < 0) {
                showToast('All class IDs must be valid numbers', 'warning');
                return;
            }

            if (classMapping[tag]) {
                showToast(`Duplicate activity tag: ${tag}`, 'warning');
                return;
            }

            classMapping[tag] = classId;
        }

        if (Object.keys(classMapping).length === 0) {
            showToast('At least one class mapping is required', 'warning');
            return;
        }

        // Build payload
        const payload = {
            config_name: configName,
            description: document.getElementById('config-description').value.trim(),
            class_mapping: classMapping,
            include_reviewed_only: document.getElementById('include-reviewed-only').checked,
            include_ai_generated: document.getElementById('include-ai-generated').checked,
            include_negative_examples: document.getElementById('include-negative').checked
        };

        // Save (create or update)
        let data;
        if (currentConfigId) {
            data = await fetchJSON(`/api/yolo/configs/${currentConfigId}`, {
                method: 'PUT',
                body: JSON.stringify(payload)
            });
        } else {
            data = await fetchJSON('/api/yolo/configs', {
                method: 'POST',
                body: JSON.stringify(payload)
            });
        }

        if (data.success) {
            showToast('Config saved successfully', 'success');

            // Reload configs and select the saved one
            await loadConfigs();
            const savedId = currentConfigId || data.config_id;
            if (savedId) {
                document.getElementById('config-select').value = savedId;
                await selectConfig(savedId);
            }
        } else {
            throw new Error(data.error || 'Failed to save config');
        }

    } catch (error) {
        showToast(`Failed to save config: ${error.message}`, 'error');
    }
}

// ============================================================================
// DATASET HEALTH
// ============================================================================

/**
 * Refresh dataset health
 */
async function refreshHealth() {
    if (!currentConfigId) {
        const healthStats = document.getElementById('health-stats');
        clearElement(healthStats);
        const msg = document.createElement('p');
        msg.textContent = 'No config selected';
        healthStats.appendChild(msg);

        clearElement(document.getElementById('health-bars'));
        clearElement(document.getElementById('health-warnings'));
        return;
    }

    try {
        const data = await fetchJSON(`/api/yolo/configs/${currentConfigId}/health`);

        if (!data.success) {
            throw new Error(data.error || 'Failed to load health');
        }

        renderHealthStats(data.health);
        renderClassDistribution(data.health.class_counts);
        renderHealthWarnings(data.health.warnings, data.health.recommendations);

        // Update action bar based on critical warnings
        const criticalWarnings = (data.health.warnings || []).filter(w => w.level === 'critical');
        updateActionBar(criticalWarnings);

    } catch (error) {
        showToast(`Failed to load health: ${error.message}`, 'error');
        const healthStats = document.getElementById('health-stats');
        clearElement(healthStats);
        const msg = document.createElement('p');
        msg.className = 'error';
        msg.textContent = 'Failed to load health data';
        healthStats.appendChild(msg);
    }
}

/**
 * Render health statistics
 */
function renderHealthStats(health) {
    const statsDiv = document.getElementById('health-stats');
    clearElement(statsDiv);

    const stats = [
        { label: 'Total Annotations', value: health.total_annotations || 0 },
        { label: 'Total Frames', value: health.total_frames || 0 },
        { label: 'Total Videos', value: health.total_videos || 0 },
        { label: 'Classes', value: Object.keys(health.class_counts || {}).length }
    ];

    stats.forEach(stat => {
        const card = document.createElement('div');
        card.className = 'stat-card';

        const label = document.createElement('div');
        label.className = 'stat-label';
        label.textContent = stat.label;

        const value = document.createElement('div');
        value.className = 'stat-value';
        value.textContent = stat.value;

        card.appendChild(label);
        card.appendChild(value);
        statsDiv.appendChild(card);
    });
}

/**
 * Render class distribution bars
 */
function renderClassDistribution(classCounts) {
    const barsDiv = document.getElementById('health-bars');
    clearElement(barsDiv);

    if (!classCounts || Object.keys(classCounts).length === 0) {
        const msg = document.createElement('p');
        msg.textContent = 'No class data available';
        barsDiv.appendChild(msg);
        return;
    }

    // Find max count for scaling
    const maxCount = Math.max(...Object.values(classCounts));

    // Sort by count descending
    const sortedClasses = Object.entries(classCounts).sort((a, b) => b[1] - a[1]);

    sortedClasses.forEach(([className, count]) => {
        const percentage = (count / maxCount * 100).toFixed(1);

        const classBar = document.createElement('div');
        classBar.className = 'class-bar';

        const classLabel = document.createElement('div');
        classLabel.className = 'class-label';
        classLabel.textContent = className;

        const barContainer = document.createElement('div');
        barContainer.className = 'class-bar-container';

        const barFill = document.createElement('div');
        barFill.className = 'class-bar-fill';
        barFill.style.width = `${percentage}%`;

        const countSpan = document.createElement('span');
        countSpan.className = 'class-count';
        countSpan.textContent = count;

        barContainer.appendChild(barFill);
        barContainer.appendChild(countSpan);
        classBar.appendChild(classLabel);
        classBar.appendChild(barContainer);
        barsDiv.appendChild(classBar);
    });
}

/**
 * Render health warnings and recommendations
 */
function renderHealthWarnings(warnings, recommendations) {
    const warningsDiv = document.getElementById('health-warnings');
    clearElement(warningsDiv);

    if ((!warnings || warnings.length === 0) && (!recommendations || recommendations.length === 0)) {
        const msg = document.createElement('p');
        msg.className = 'success';
        msg.textContent = 'No issues detected';
        warningsDiv.appendChild(msg);
        return;
    }

    // Render warnings
    if (warnings && warnings.length > 0) {
        warnings.forEach(warning => {
            const card = document.createElement('div');
            const severityClass = warning.level === 'critical' ? 'critical' : 'warning';
            card.className = `warning-card ${severityClass}`;

            const strong = document.createElement('strong');
            strong.textContent = warning.level === 'critical' ? 'Critical: ' : 'Warning: ';

            const text = document.createTextNode(warning.message);

            card.appendChild(strong);
            card.appendChild(text);
            warningsDiv.appendChild(card);
        });
    }

    // Render recommendations
    if (recommendations && recommendations.length > 0) {
        recommendations.forEach(rec => {
            const card = document.createElement('div');
            card.className = 'warning-card info';

            const strong = document.createElement('strong');
            strong.textContent = 'Recommendation: ';

            const text = document.createTextNode(rec);

            card.appendChild(strong);
            card.appendChild(text);
            warningsDiv.appendChild(card);
        });
    }
}

/**
 * Update action bar based on health
 * @param {Array} criticalWarnings - Array of critical warning objects (or empty array)
 */
function updateActionBar(criticalWarnings) {
    const exportTrainBtn = document.getElementById('btn-export-train');
    const exportOnlyBtn = document.getElementById('btn-export-only');
    const retrainBtn = document.getElementById('btn-retrain');

    // Helper to set disabled state with tooltip
    function setButtonState(btn, disabled, reason) {
        if (!btn) return;
        btn.disabled = disabled;
        btn.title = disabled ? reason : '';
    }

    // Build specific tooltip from critical warnings
    const criticalFixMap = {
        'NO_ANNOTATIONS': 'Add annotations that match the class mapping in this config',
        'INSUFFICIENT_DATA': 'Add more annotated samples (minimum 10 required for train/val split)',
        'VERY_SMALL_DATASET': 'Add more annotations (50+ recommended to avoid overfitting)',
        'EMPTY_CLASS': 'Add annotations for empty classes or remove them from the config'
    };

    let criticalTooltip = '';
    if (criticalWarnings && criticalWarnings.length > 0) {
        const fixes = criticalWarnings.map(w => criticalFixMap[w.code] || w.message);
        const unique = [...new Set(fixes)];
        criticalTooltip = 'Blocked: ' + unique.join('; ');
    }

    // Determine reasons
    const noWrite = !window.canWrite;
    const noConfig = !currentConfigId;
    const hasCritical = criticalWarnings && criticalWarnings.length > 0;

    // Export Only - not blocked by critical health issues
    if (noWrite) {
        setButtonState(exportOnlyBtn, true, 'Read-only mode');
    } else if (noConfig) {
        setButtonState(exportOnlyBtn, true, 'Select a training config first');
    } else {
        setButtonState(exportOnlyBtn, false, '');
    }

    // Export & Train and Retrain - blocked by critical health issues
    if (hasCritical) {
        setButtonState(exportTrainBtn, true, criticalTooltip);
        setButtonState(retrainBtn, true, criticalTooltip);
    } else if (noWrite) {
        setButtonState(exportTrainBtn, true, 'Read-only mode');
        setButtonState(retrainBtn, true, 'Read-only mode');
    } else if (noConfig) {
        setButtonState(exportTrainBtn, true, 'Select a training config first');
        setButtonState(retrainBtn, true, 'Select a training config first');
    } else {
        setButtonState(exportTrainBtn, false, '');
        setButtonState(retrainBtn, false, '');
    }
}

// ============================================================================
// ACTIONS
// ============================================================================

/**
 * Export dataset only (no training)
 */
async function exportOnly() {
    if (!currentConfigId) {
        showToast('No config selected', 'warning');
        return;
    }

    showConfirm('Export dataset without training?', async () => {
        try {
            setActionProgress('Exporting dataset...', true);

            const data = await fetchJSON(`/api/yolo/configs/${currentConfigId}/export`, {
                method: 'POST'
            });

            if (data.success) {
                const exportInfo = data.export_path || '';
                const count = data.annotation_count || data.frame_count || '';
                showToast(`Export complete! ${count ? count + ' annotations. ' : ''}${exportInfo}`, 'success', 8000);
            } else {
                throw new Error(data.error || 'Export failed');
            }

        } catch (error) {
            showToast(`Export failed: ${error.message}`, 'error');
        } finally {
            setActionProgress('', false);
        }
    });
}

/**
 * Export and train
 */
async function exportAndTrain() {
    if (!currentConfigId) {
        showToast('No config selected', 'warning');
        return;
    }

    showConfirm('Export dataset and start training?', async () => {
        try {
            setActionProgress('Starting export and training...', true);

            const data = await fetchJSON('/api/training/export-and-train', {
                method: 'POST',
                body: JSON.stringify({
                    job_type: 'yolo',
                    export_config_id: currentConfigId,
                    model_type: 'yolov8n',
                    epochs: 100
                })
            });

            if (data.success) {
                const jobId = (data.job && data.job.job_id) || '';
                showToast(`Training job ${jobId.substring(0, 8)}... started successfully`, 'success');
                setFastPolling();
                pollNow();
            } else {
                throw new Error(data.error || 'Failed to start training');
            }

        } catch (error) {
            showToast(`Failed to start training: ${error.message}`, 'error');
        } finally {
            setActionProgress('', false);
        }
    });
}

/**
 * Retrain with latest dataset
 */
async function retrainWithLatest() {
    // Same as exportAndTrain
    await exportAndTrain();
}

/**
 * Set action progress indicator
 */
function setActionProgress(message, isLoading) {
    const progressDiv = document.getElementById('action-progress');
    clearElement(progressDiv);

    if (isLoading) {
        const spinner = document.createElement('span');
        spinner.className = 'spinner';

        const text = document.createTextNode(' ' + message);

        progressDiv.appendChild(spinner);
        progressDiv.appendChild(text);
        progressDiv.style.display = 'block';
    } else {
        progressDiv.style.display = 'none';
    }
}

// ============================================================================
// POLLING
// ============================================================================

/**
 * Start adaptive polling
 */
function startPolling() {
    if (pollInterval) {
        clearInterval(pollInterval);
    }

    pollNow(); // Initial poll
    pollInterval = setInterval(pollNow, pollSpeed);
}

/**
 * Switch to fast polling (5s)
 */
function setFastPolling() {
    if (pollSpeed === FAST_POLL) return;

    pollSpeed = FAST_POLL;
    startPolling();
}

/**
 * Switch to idle polling (30s)
 */
function setIdlePolling() {
    if (pollSpeed === IDLE_POLL) return;

    pollSpeed = IDLE_POLL;
    startPolling();
}

/**
 * Poll now
 */
async function pollNow() {
    await Promise.all([
        loadJobs(),
        loadWorkerStatus(),
        loadQueueStatus()
    ]);
}

// ============================================================================
// JOB MONITOR
// ============================================================================

/**
 * Load training jobs
 */
async function loadJobs() {
    try {
        const showAll = document.getElementById('show-all-configs').checked;

        let url = '/api/training/jobs?limit=100';
        if (!showAll && currentConfigId) {
            url += `&export_config_id=${currentConfigId}`;
        }

        const data = await fetchJSON(url);

        if (data.success) {
            renderJobs(data.jobs || []);

            // Check if all jobs are terminal
            const hasActive = data.jobs.some(job =>
                ['pending', 'uploading', 'queued', 'processing'].includes(job.status)
            );

            if (hasActive) {
                setFastPolling();
            } else {
                setIdlePolling();
            }
        }

    } catch (error) {
        console.error('Failed to load jobs:', error);
    }
}

/**
 * Render jobs table
 */
function renderJobs(jobs) {
    const tbody = document.getElementById('jobs-table-body');
    clearElement(tbody);

    if (jobs.length === 0) {
        const tr = document.createElement('tr');
        const td = document.createElement('td');
        td.colSpan = 6;
        td.style.textAlign = 'center';
        td.textContent = 'No jobs found';
        tr.appendChild(td);
        tbody.appendChild(tr);
        return;
    }

    jobs.forEach(job => {
        const tr = document.createElement('tr');

        // Job ID (truncated)
        const tdId = document.createElement('td');
        const shortId = (job.job_id || '').substring(0, 8);
        tdId.textContent = shortId + '...';
        tdId.title = job.job_id;
        tdId.style.fontFamily = 'monospace';
        tdId.style.fontSize = '12px';
        tr.appendChild(tdId);

        // Job Type
        const tdType = document.createElement('td');
        tdType.textContent = job.job_type || 'yolo';
        tr.appendChild(tdType);

        // Status
        const tdStatus = document.createElement('td');
        const statusBadge = document.createElement('span');
        statusBadge.className = `badge status-${job.status}`;
        statusBadge.textContent = job.status;
        tdStatus.appendChild(statusBadge);
        if (job.error_message) {
            const errSpan = document.createElement('small');
            errSpan.style.cssText = 'display:block;color:#e74c3c;margin-top:4px;font-size:11px;';
            errSpan.textContent = job.error_message;
            tdStatus.appendChild(errSpan);
        }
        tr.appendChild(tdStatus);

        // Submitted At
        const tdCreated = document.createElement('td');
        tdCreated.textContent = formatDate(job.submitted_at);
        tr.appendChild(tdCreated);

        // Completed At
        const tdCompleted = document.createElement('td');
        tdCompleted.textContent = formatDate(job.completed_at);
        tr.appendChild(tdCompleted);

        // Actions
        const tdActions = document.createElement('td');
        tdActions.className = 'job-actions';

        const canCancel = ['pending', 'uploading', 'queued', 'processing'].includes(job.status);
        const canRetry = ['failed', 'cancelled'].includes(job.status);
        const canDelete = ['completed', 'failed', 'cancelled'].includes(job.status);

        if (canCancel) {
            const cancelBtn = document.createElement('button');
            cancelBtn.className = 'btn btn-sm btn-warning';
            cancelBtn.textContent = 'Cancel';
            cancelBtn.onclick = () => cancelJob(job.job_id);
            tdActions.appendChild(cancelBtn);
        }

        if (canRetry) {
            const retryBtn = document.createElement('button');
            retryBtn.className = 'btn btn-sm btn-primary';
            retryBtn.textContent = 'Retry';
            retryBtn.onclick = () => retryJob(job.job_id);
            tdActions.appendChild(retryBtn);
        }

        if (canDelete) {
            const deleteBtn = document.createElement('button');
            deleteBtn.className = 'btn btn-sm btn-danger';
            deleteBtn.textContent = 'Delete';
            deleteBtn.onclick = () => deleteJob(job.job_id);
            tdActions.appendChild(deleteBtn);
        }

        tr.appendChild(tdActions);
        tbody.appendChild(tr);
    });
}

/**
 * Cancel a job
 */
async function cancelJob(jobId) {
    showConfirm(`Cancel job ${jobId}?`, async () => {
        try {
            const data = await fetchJSON(`/api/training/jobs/${jobId}/cancel`, {
                method: 'POST'
            });

            if (data.success) {
                showToast('Job cancelled successfully', 'success');
                pollNow();
            } else {
                throw new Error(data.error || 'Failed to cancel job');
            }

        } catch (error) {
            showToast(`Failed to cancel job: ${error.message}`, 'error');
        }
    });
}

/**
 * Retry a job
 */
async function retryJob(jobId) {
    showConfirm(`Retry job ${jobId}?`, async () => {
        try {
            const data = await fetchJSON(`/api/training/jobs/${jobId}/retry`, {
                method: 'POST'
            });

            if (data.success) {
                showToast(`New job ${data.new_job_id} created`, 'success');
                setFastPolling();
                pollNow();
            } else {
                throw new Error(data.error || 'Failed to retry job');
            }

        } catch (error) {
            showToast(`Failed to retry job: ${error.message}`, 'error');
        }
    });
}

/**
 * Delete a job
 */
async function deleteJob(jobId) {
    showConfirm(`Delete job ${jobId}? This cannot be undone.`, async () => {
        try {
            const data = await fetchJSON(`/api/training/jobs/${jobId}`, {
                method: 'DELETE'
            });

            if (data.success) {
                showToast('Job deleted successfully', 'success');
                pollNow();
            } else {
                throw new Error(data.error || 'Failed to delete job');
            }

        } catch (error) {
            showToast(`Failed to delete job: ${error.message}`, 'error');
        }
    });
}

// ============================================================================
// WORKER STATUS
// ============================================================================

/**
 * Load worker status
 */
async function loadWorkerStatus() {
    try {
        const data = await fetchJSON('/api/worker/status');

        const badge = document.getElementById('worker-status');
        if (!badge) return;

        if (data.active) {
            badge.textContent = 'Active';
            badge.className = 'badge status-completed';
        } else {
            badge.textContent = 'Inactive';
            badge.className = 'badge status-failed';
        }

    } catch (error) {
        console.error('Failed to load worker status:', error);
    }
}

/**
 * Load queue status
 */
async function loadQueueStatus() {
    // Currently not displayed in UI but could be added later
    try {
        const data = await fetchJSON('/api/training/queue-status');
        // Store for potential use
        window.queueStatus = data;
    } catch (error) {
        console.error('Failed to load queue status:', error);
    }
}

// ============================================================================
// MODEL RESULTS
// ============================================================================

/**
 * Load trained models
 */
async function loadModels() {
    try {
        const data = await fetchJSON('/api/ai/models?active_only=false');

        if (data.success) {
            renderModels(data.models || []);
        }

    } catch (error) {
        console.error('Failed to load models:', error);
    }
}

/**
 * Render model cards
 */
function renderModels(models) {
    const container = document.getElementById('models-container');
    clearElement(container);

    if (models.length === 0) {
        const msg = document.createElement('p');
        msg.textContent = 'No trained models found';
        container.appendChild(msg);
        return;
    }

    models.forEach(model => {
        const approvalRate = model.approval_rate || 0;
        const approvalClass = approvalRate > 85 ? 'high' : approvalRate > 70 ? 'medium' : 'low';

        const card = document.createElement('div');
        card.className = 'model-card';

        // Header
        const header = document.createElement('div');
        header.className = 'model-header';

        const h3 = document.createElement('h3');
        h3.textContent = model.model_name;

        const typeBadge = document.createElement('span');
        typeBadge.className = `badge badge-${model.model_type || 'yolo'}`;
        typeBadge.textContent = model.model_type || 'yolo';

        header.appendChild(h3);
        header.appendChild(typeBadge);

        // Stats
        const stats = document.createElement('div');
        stats.className = 'model-stats';

        const statsData = [
            { label: 'Version', value: model.version || 1 },
            { label: 'Accuracy', value: `${(model.accuracy || 0).toFixed(1)}%` },
            { label: 'Precision', value: `${(model.precision || 0).toFixed(1)}%` },
            { label: 'Recall', value: `${(model.recall || 0).toFixed(1)}%` }
        ];

        statsData.forEach(s => {
            const statDiv = document.createElement('div');
            statDiv.className = 'model-stat';

            const label = document.createElement('span');
            label.className = 'stat-label';
            label.textContent = s.label;

            const value = document.createElement('span');
            value.className = 'stat-value';
            value.textContent = s.value;

            statDiv.appendChild(label);
            statDiv.appendChild(value);
            stats.appendChild(statDiv);
        });

        // Approval bar
        const approvalBar = document.createElement('div');
        approvalBar.className = 'approval-bar';

        const approvalLabel = document.createElement('div');
        approvalLabel.className = 'approval-label';
        approvalLabel.textContent = 'Approval Rate';

        const approvalBarContainer = document.createElement('div');
        approvalBarContainer.className = 'approval-bar-container';

        const approvalBarFill = document.createElement('div');
        approvalBarFill.className = `approval-bar-fill ${approvalClass}`;
        approvalBarFill.style.width = `${approvalRate}%`;

        const approvalRateSpan = document.createElement('span');
        approvalRateSpan.className = 'approval-rate';
        approvalRateSpan.textContent = `${approvalRate.toFixed(1)}%`;

        approvalBarContainer.appendChild(approvalBarFill);
        approvalBarContainer.appendChild(approvalRateSpan);
        approvalBar.appendChild(approvalLabel);
        approvalBar.appendChild(approvalBarContainer);

        // Actions
        const actions = document.createElement('div');
        actions.className = 'model-actions';

        const retrainBtn = document.createElement('button');
        retrainBtn.className = 'btn btn-primary';
        retrainBtn.textContent = 'Retrain';
        retrainBtn.disabled = !window.canWrite;
        if (!window.canWrite) retrainBtn.title = 'Read-only mode';
        retrainBtn.onclick = retrainWithLatest;

        actions.appendChild(retrainBtn);

        // Assemble card
        card.appendChild(header);
        card.appendChild(stats);
        card.appendChild(approvalBar);
        card.appendChild(actions);
        container.appendChild(card);
    });
}

// ============================================================================
// SECTION COLLAPSE
// ============================================================================

/**
 * Toggle section visibility
 */
function toggleSection(sectionId) {
    const section = document.getElementById(sectionId);
    if (!section) return;

    const content = section.querySelector('.section-content');
    const chevron = section.querySelector('.chevron');

    if (!content) return;

    const isCollapsed = content.style.display === 'none';

    if (isCollapsed) {
        content.style.display = 'block';
        if (chevron) chevron.style.transform = 'rotate(0deg)';
    } else {
        content.style.display = 'none';
        if (chevron) chevron.style.transform = 'rotate(-90deg)';
    }
}

// ============================================================================
// INITIALIZATION
// ============================================================================

document.addEventListener('DOMContentLoaded', async () => {
    try {
        // Load initial data
        await loadActivityTags();
        await loadConfigs();
        await loadModels();

        // Start polling
        startPolling();

        // Setup event listeners
        document.getElementById('config-select').addEventListener('change', (e) => {
            selectConfig(e.target.value);
        });

        document.getElementById('show-all-configs').addEventListener('change', () => {
            loadJobs();
        });

        // Initial load
        showToast('Model training interface loaded', 'success', 3000);

    } catch (error) {
        showToast(`Initialization failed: ${error.message}`, 'error');
    }
});

// Make functions globally accessible for inline handlers
window.saveConfig = saveConfig;
window.addClassMapping = addClassMapping;
window.deleteMapping = deleteMapping;
window.exportOnly = exportOnly;
window.exportAndTrain = exportAndTrain;
window.retrainWithLatest = retrainWithLatest;
window.cancelJob = cancelJob;
window.retryJob = retryJob;
window.deleteJob = deleteJob;
window.toggleSection = toggleSection;
window.refreshHealth = refreshHealth;
