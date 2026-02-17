/**
 * Crossing Line Configuration UI
 * Handles drawing, saving, and pairing of crossing lines across cameras
 */

// Application state
let state = {
    selectedCamera: null,
    lines: [],              // all crossing lines for selected camera
    allLines: [],           // all lines across all cameras
    pairs: [],              // all line pairs
    drawMode: false,
    drawStart: null,        // {x, y} first click point
    currentLine: null,      // line being drawn {x1, y1, x2, y2}
    frameImage: null,       // loaded Image object
    canvasScale: 1.0,       // scale factor for frame to canvas
    frameWidth: 1920,       // actual frame width
    frameHeight: 1080,      // actual frame height
    direction: null,        // selected direction {dx, dy}
};

// DOM elements
let elements = {};

/**
 * Initialize the application
 */
function init() {
    // Cache DOM elements
    elements = {
        cameraSelect: document.getElementById('camera-select'),
        canvas: document.getElementById('line-canvas'),
        canvasWrapper: document.getElementById('canvas-wrapper'),
        lineNameInput: document.getElementById('line-name'),
        drawModeBtn: document.getElementById('draw-mode-btn'),
        directionSelector: document.getElementById('direction-selector'),
        directionABtn: document.getElementById('direction-a-btn'),
        directionBBtn: document.getElementById('direction-b-btn'),
        saveLineBtn: document.getElementById('save-line-btn'),
        linesList: document.getElementById('lines-list'),
        pairLineA: document.getElementById('pair-line-a'),
        pairLineB: document.getElementById('pair-line-b'),
        pairReversed: document.getElementById('pair-reversed'),
        pairLinesBtn: document.getElementById('pair-lines-btn'),
        pairsList: document.getElementById('pairs-list'),
        runSpatialMatchBtn: document.getElementById('run-spatial-match-btn'),
    };

    elements.ctx = elements.canvas.getContext('2d');

    // Event listeners
    elements.cameraSelect.addEventListener('change', onCameraChange);
    elements.drawModeBtn.addEventListener('click', toggleDrawMode);
    elements.canvas.addEventListener('click', handleCanvasClick);
    elements.directionABtn.addEventListener('click', () => selectDirection('A'));
    elements.directionBBtn.addEventListener('click', () => selectDirection('B'));
    elements.saveLineBtn.addEventListener('click', saveLine);
    elements.pairLinesBtn.addEventListener('click', pairLines);
    elements.runSpatialMatchBtn.addEventListener('click', runSpatialMatch);

    // Initial load
    loadCameras();
    loadAllLines();
    loadPairs();

    // Handle window resize
    window.addEventListener('resize', debounce(onResize, 250));
}

/**
 * Load available cameras
 */
async function loadCameras() {
    try {
        // Fetch cameras from crossing lines
        const linesResponse = await fetch('/api/ai/crossing-lines');
        const linesData = await linesResponse.json();
        const camerasFromLines = new Set();

        if (linesData.lines) {
            linesData.lines.forEach(line => {
                if (line.camera_id) {
                    camerasFromLines.add(line.camera_id);
                }
            });
        }

        // Fetch cameras from tracks
        const tracksResponse = await fetch('/api/ai/tracks/summary');
        const tracksData = await tracksResponse.json();
        const camerasFromTracks = new Set();

        if (tracksData.cameras) {
            tracksData.cameras.forEach(cam => {
                camerasFromTracks.add(cam.camera_id);
            });
        }

        // Combine all cameras
        const allCameras = new Set([...camerasFromLines, ...camerasFromTracks]);
        const sortedCameras = Array.from(allCameras).sort();

        // Populate dropdown - clear first
        while (elements.cameraSelect.firstChild) {
            elements.cameraSelect.removeChild(elements.cameraSelect.firstChild);
        }

        const defaultOption = document.createElement('option');
        defaultOption.value = '';
        defaultOption.textContent = 'Select a camera...';
        elements.cameraSelect.appendChild(defaultOption);

        sortedCameras.forEach(cameraId => {
            const option = document.createElement('option');
            option.value = cameraId;
            option.textContent = cameraId;
            elements.cameraSelect.appendChild(option);
        });

        console.log(`Loaded ${sortedCameras.length} cameras`);
    } catch (error) {
        console.error('Error loading cameras:', error);
        showNotification('Failed to load cameras', 'error');
    }
}

/**
 * Handle camera selection change
 */
async function onCameraChange() {
    const cameraId = elements.cameraSelect.value;
    if (!cameraId) {
        state.selectedCamera = null;
        clearCanvas();
        return;
    }

    state.selectedCamera = cameraId;
    resetDrawingState();

    await Promise.all([
        loadFrame(cameraId),
        loadLines(cameraId),
    ]);
}

/**
 * Load frame image for camera
 */
async function loadFrame(cameraId) {
    try {
        const response = await fetch(`/api/ai/crossing-lines/${cameraId}/frame`);

        if (response.status === 404) {
            console.warn(`No frame available for camera ${cameraId}`);
            state.frameImage = null;
            drawCanvas();
            return;
        }

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }

        const blob = await response.blob();
        const imageUrl = URL.createObjectURL(blob);

        const img = new Image();
        img.onload = () => {
            state.frameImage = img;
            state.frameWidth = img.width;
            state.frameHeight = img.height;
            calculateCanvasScale();
            drawCanvas();
            URL.revokeObjectURL(imageUrl);
        };
        img.onerror = () => {
            console.error('Failed to load frame image');
            state.frameImage = null;
            drawCanvas();
            URL.revokeObjectURL(imageUrl);
        };
        img.src = imageUrl;

    } catch (error) {
        console.error('Error loading frame:', error);
        state.frameImage = null;
        drawCanvas();
    }
}

/**
 * Load crossing lines for camera
 */
async function loadLines(cameraId) {
    try {
        const response = await fetch(`/api/ai/crossing-lines?camera_id=${cameraId}`);
        const data = await response.json();

        state.lines = data.lines || [];
        renderLinesList();
        drawCanvas();

        console.log(`Loaded ${state.lines.length} lines for camera ${cameraId}`);
    } catch (error) {
        console.error('Error loading lines:', error);
        showNotification('Failed to load lines', 'error');
    }
}

/**
 * Load all crossing lines (for pairing)
 */
async function loadAllLines() {
    try {
        const response = await fetch('/api/ai/crossing-lines');
        const data = await response.json();

        state.allLines = data.lines || [];
        updatePairDropdowns();

    } catch (error) {
        console.error('Error loading all lines:', error);
    }
}

/**
 * Load existing pairs (derived from allLines with paired_line_id set)
 */
async function loadPairs() {
    try {
        // Ensure allLines is loaded
        if (!state.allLines || state.allLines.length === 0) {
            await loadAllLines();
        }

        // Build pairs from lines that have paired_line_id, deduplicate by smaller ID first
        const seen = new Set();
        state.pairs = [];
        const lineMap = {};
        state.allLines.forEach(l => { lineMap[l.id] = l; });

        state.allLines.forEach(line => {
            if (!line.paired_line_id) return;
            const pairKey = Math.min(line.id, line.paired_line_id) + '-' + Math.max(line.id, line.paired_line_id);
            if (seen.has(pairKey)) return;
            seen.add(pairKey);

            const paired = lineMap[line.paired_line_id];
            if (!paired) return;

            state.pairs.push({
                line_a_id: line.id,
                line_b_id: paired.id,
                camera_a: line.camera_id,
                camera_b: paired.camera_id,
                line_a_name: line.line_name,
                line_b_name: paired.line_name,
                reversed: line.lane_mapping_reversed
            });
        });

        renderPairsList();

    } catch (error) {
        console.error('Error loading pairs:', error);
    }
}

/**
 * Calculate canvas scale factor
 */
function calculateCanvasScale() {
    const maxWidth = elements.canvasWrapper.clientWidth - 40;
    const maxHeight = elements.canvasWrapper.clientHeight - 40;

    const scaleX = maxWidth / state.frameWidth;
    const scaleY = maxHeight / state.frameHeight;

    state.canvasScale = Math.min(scaleX, scaleY, 1.0);

    elements.canvas.width = Math.floor(state.frameWidth * state.canvasScale);
    elements.canvas.height = Math.floor(state.frameHeight * state.canvasScale);
}

/**
 * Convert canvas coordinates to frame coordinates
 */
function canvasToFrame(canvasX, canvasY) {
    const rect = elements.canvas.getBoundingClientRect();
    const x = canvasX - rect.left;
    const y = canvasY - rect.top;

    return {
        x: Math.round(x / state.canvasScale),
        y: Math.round(y / state.canvasScale)
    };
}

/**
 * Convert frame coordinates to canvas coordinates
 */
function frameToCanvas(frameX, frameY) {
    return {
        x: Math.round(frameX * state.canvasScale),
        y: Math.round(frameY * state.canvasScale)
    };
}

/**
 * Toggle draw mode
 */
function toggleDrawMode() {
    state.drawMode = !state.drawMode;

    if (state.drawMode) {
        elements.drawModeBtn.textContent = 'Disable Draw Mode';
        elements.drawModeBtn.classList.add('active');
        elements.canvas.classList.add('draw-mode-active');
    } else {
        elements.drawModeBtn.textContent = 'Enable Draw Mode';
        elements.drawModeBtn.classList.remove('active');
        elements.canvas.classList.remove('draw-mode-active');
        resetDrawingState();
    }
}

/**
 * Reset drawing state
 */
function resetDrawingState() {
    state.drawStart = null;
    state.currentLine = null;
    state.direction = null;
    elements.directionSelector.style.display = 'none';
    elements.saveLineBtn.disabled = true;
    drawCanvas();
}

/**
 * Handle canvas click
 */
function handleCanvasClick(e) {
    if (!state.drawMode || !state.selectedCamera) return;

    const frameCoords = canvasToFrame(e.clientX, e.clientY);

    if (!state.drawStart) {
        // First click: set start point
        state.drawStart = frameCoords;
        drawCanvas();
    } else {
        // Second click: complete line
        state.currentLine = {
            x1: state.drawStart.x,
            y1: state.drawStart.y,
            x2: frameCoords.x,
            y2: frameCoords.y
        };

        state.drawStart = null;
        elements.directionSelector.style.display = 'block';
        drawCanvas();
    }
}

/**
 * Select direction for line
 */
function selectDirection(option) {
    if (!state.currentLine) return;

    const dx = state.currentLine.x2 - state.currentLine.x1;
    const dy = state.currentLine.y2 - state.currentLine.y1;
    const length = Math.sqrt(dx * dx + dy * dy);

    // Perpendicular to line (normalized)
    const perpX = -dy / length;
    const perpY = dx / length;

    if (option === 'A') {
        state.direction = { dx: perpX, dy: perpY };
        elements.directionABtn.classList.add('btn-primary');
        elements.directionABtn.classList.remove('btn-secondary');
        elements.directionBBtn.classList.remove('btn-primary');
        elements.directionBBtn.classList.add('btn-secondary');
    } else {
        state.direction = { dx: -perpX, dy: -perpY };
        elements.directionBBtn.classList.add('btn-primary');
        elements.directionBBtn.classList.remove('btn-secondary');
        elements.directionABtn.classList.remove('btn-primary');
        elements.directionABtn.classList.add('btn-secondary');
    }

    elements.saveLineBtn.disabled = false;
    drawCanvas();
}

/**
 * Save the current line
 */
async function saveLine() {
    const name = elements.lineNameInput.value.trim();
    if (!name) {
        showNotification('Please enter a line name', 'error');
        return;
    }

    if (!state.currentLine || !state.direction) {
        showNotification('Please draw a line and select direction', 'error');
        return;
    }

    const payload = {
        camera_id: state.selectedCamera,
        line_name: name,
        x1: state.currentLine.x1,
        y1: state.currentLine.y1,
        x2: state.currentLine.x2,
        y2: state.currentLine.y2,
        forward_dx: state.direction.dx,
        forward_dy: state.direction.dy
    };

    try {
        const response = await fetch('/api/ai/crossing-lines', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }

        showNotification('Line saved successfully', 'success');
        elements.lineNameInput.value = '';
        resetDrawingState();

        await Promise.all([
            loadLines(state.selectedCamera),
            loadAllLines()
        ]);

    } catch (error) {
        console.error('Error saving line:', error);
        showNotification('Failed to save line', 'error');
    }
}

/**
 * Delete a line
 */
async function deleteLine(lineId) {
    if (!confirm('Are you sure you want to delete this line?')) {
        return;
    }

    try {
        const response = await fetch(`/api/ai/crossing-lines/${lineId}`, {
            method: 'DELETE'
        });

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }

        showNotification('Line deleted successfully', 'success');

        await Promise.all([
            loadLines(state.selectedCamera),
            loadAllLines()
        ]);

    } catch (error) {
        console.error('Error deleting line:', error);
        showNotification('Failed to delete line', 'error');
    }
}

/**
 * Pair two lines
 */
async function pairLines() {
    const lineAId = elements.pairLineA.value;
    const lineBId = elements.pairLineB.value;
    const reversed = elements.pairReversed.checked;

    if (!lineAId || !lineBId) {
        showNotification('Please select both lines', 'error');
        return;
    }

    if (lineAId === lineBId) {
        showNotification('Cannot pair a line with itself', 'error');
        return;
    }

    // Check if lines are from different cameras
    const lineA = state.allLines.find(l => l.id === parseInt(lineAId));
    const lineB = state.allLines.find(l => l.id === parseInt(lineBId));

    if (lineA.camera_id === lineB.camera_id) {
        showNotification('Lines must be from different cameras', 'error');
        return;
    }

    try {
        const response = await fetch(`/api/ai/crossing-lines/${lineAId}/pair`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                paired_line_id: parseInt(lineBId),
                lane_mapping_reversed: reversed
            })
        });

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }

        showNotification('Lines paired successfully', 'success');
        elements.pairReversed.checked = false;

        await loadPairs();

    } catch (error) {
        console.error('Error pairing lines:', error);
        showNotification('Failed to pair lines', 'error');
    }
}

/**
 * Delete a pair
 */
async function deletePair(lineId) {
    if (!confirm('Are you sure you want to unpair these lines?')) {
        return;
    }

    try {
        const response = await fetch(`/api/ai/crossing-lines/${lineId}/pair`, {
            method: 'DELETE'
        });

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }

        showNotification('Lines unpaired successfully', 'success');
        await loadAllLines();
        await loadPairs();

    } catch (error) {
        console.error('Error unpairing lines:', error);
        showNotification('Failed to unpair lines', 'error');
    }
}

/**
 * Run spatial matching
 */
async function runSpatialMatch() {
    try {
        elements.runSpatialMatchBtn.disabled = true;
        elements.runSpatialMatchBtn.textContent = 'Running...';

        const response = await fetch('/api/ai/cross-camera/match-spatial', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({})
        });

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }

        const result = await response.json();
        showNotification(`Spatial matching complete: ${result.matches_found || 0} matches found`, 'success');

    } catch (error) {
        console.error('Error running spatial match:', error);
        showNotification('Failed to run spatial matching', 'error');
    } finally {
        elements.runSpatialMatchBtn.disabled = false;
        elements.runSpatialMatchBtn.textContent = 'Run Spatial Match';
    }
}

/**
 * Update pair dropdowns
 */
function updatePairDropdowns() {
    // Clear dropdowns
    while (elements.pairLineA.firstChild) {
        elements.pairLineA.removeChild(elements.pairLineA.firstChild);
    }
    while (elements.pairLineB.firstChild) {
        elements.pairLineB.removeChild(elements.pairLineB.firstChild);
    }

    // Add default options
    const defaultA = document.createElement('option');
    defaultA.value = '';
    defaultA.textContent = 'Line A...';
    elements.pairLineA.appendChild(defaultA);

    const defaultB = document.createElement('option');
    defaultB.value = '';
    defaultB.textContent = 'Line B...';
    elements.pairLineB.appendChild(defaultB);

    // Add line options
    state.allLines.forEach(line => {
        const optionA = document.createElement('option');
        optionA.value = line.id;
        optionA.textContent = `${line.camera_id} - ${line.line_name}`;
        elements.pairLineA.appendChild(optionA);

        const optionB = document.createElement('option');
        optionB.value = line.id;
        optionB.textContent = `${line.camera_id} - ${line.line_name}`;
        elements.pairLineB.appendChild(optionB);
    });
}

/**
 * Render lines list
 */
function renderLinesList() {
    // Clear list
    while (elements.linesList.firstChild) {
        elements.linesList.removeChild(elements.linesList.firstChild);
    }

    if (state.lines.length === 0) {
        const emptyState = document.createElement('div');
        emptyState.className = 'empty-state';
        emptyState.textContent = 'No lines defined yet';
        elements.linesList.appendChild(emptyState);
        return;
    }

    state.lines.forEach(line => {
        const isPaired = line.paired_line_id != null;

        const lineItem = document.createElement('div');
        lineItem.className = 'line-item';

        const lineInfo = document.createElement('div');
        lineInfo.className = 'line-info';

        const lineName = document.createElement('div');
        lineName.className = 'line-name';

        const statusDot = document.createElement('span');
        statusDot.className = `line-status ${isPaired ? 'paired' : 'unpaired'}`;
        lineName.appendChild(statusDot);

        const nameText = document.createTextNode(line.line_name);
        lineName.appendChild(nameText);

        const lineCoords = document.createElement('div');
        lineCoords.className = 'line-coords';
        lineCoords.textContent = `(${line.x1}, ${line.y1}) → (${line.x2}, ${line.y2})`;

        lineInfo.appendChild(lineName);
        lineInfo.appendChild(lineCoords);

        const lineActions = document.createElement('div');
        lineActions.className = 'line-actions';

        const deleteBtn = document.createElement('button');
        deleteBtn.className = 'btn btn-danger btn-small';
        deleteBtn.textContent = 'Delete';
        deleteBtn.onclick = () => deleteLine(line.id);

        lineActions.appendChild(deleteBtn);

        lineItem.appendChild(lineInfo);
        lineItem.appendChild(lineActions);

        elements.linesList.appendChild(lineItem);
    });
}

/**
 * Render pairs list
 */
function renderPairsList() {
    // Clear list
    while (elements.pairsList.firstChild) {
        elements.pairsList.removeChild(elements.pairsList.firstChild);
    }

    if (state.pairs.length === 0) {
        const emptyState = document.createElement('div');
        emptyState.className = 'empty-state';
        emptyState.textContent = 'No line pairs defined yet';
        elements.pairsList.appendChild(emptyState);
        return;
    }

    state.pairs.forEach(pair => {
        const pairItem = document.createElement('div');
        pairItem.className = 'pair-item';

        const pairInfo = document.createElement('div');
        pairInfo.className = 'pair-info';

        const cameraA = document.createElement('span');
        cameraA.className = 'pair-cameras';
        cameraA.textContent = pair.camera_a;

        const arrow1 = document.createTextNode(' ↔ ');

        const cameraB = document.createElement('span');
        cameraB.className = 'pair-cameras';
        cameraB.textContent = pair.camera_b;

        pairInfo.appendChild(cameraA);
        pairInfo.appendChild(arrow1);
        pairInfo.appendChild(cameraB);
        pairInfo.appendChild(document.createElement('br'));

        const lineNames = document.createTextNode(`${pair.line_a_name} ↔ ${pair.line_b_name}`);
        pairInfo.appendChild(lineNames);

        if (pair.reversed) {
            const reversedSpan = document.createElement('span');
            reversedSpan.className = 'pair-reversed';
            reversedSpan.textContent = '(reversed)';
            pairInfo.appendChild(reversedSpan);
        }

        const pairActions = document.createElement('div');
        pairActions.className = 'line-actions';

        const unpairBtn = document.createElement('button');
        unpairBtn.className = 'btn btn-danger btn-small';
        unpairBtn.textContent = 'Unpair';
        unpairBtn.onclick = () => deletePair(pair.line_a_id);

        pairActions.appendChild(unpairBtn);

        pairItem.appendChild(pairInfo);
        pairItem.appendChild(pairActions);

        elements.pairsList.appendChild(pairItem);
    });
}

/**
 * Draw canvas
 */
function drawCanvas() {
    const ctx = elements.ctx;
    const width = elements.canvas.width;
    const height = elements.canvas.height;

    // Clear canvas
    ctx.clearRect(0, 0, width, height);

    if (!state.selectedCamera) {
        drawPlaceholder(ctx, width, height, 'Select a camera to begin');
        return;
    }

    // Draw background image or placeholder
    if (state.frameImage) {
        ctx.drawImage(state.frameImage, 0, 0, width, height);
    } else {
        drawPlaceholder(ctx, width, height, 'No frame available for this camera');
    }

    // Draw saved lines (green)
    state.lines.forEach(line => {
        const p1 = frameToCanvas(line.x1, line.y1);
        const p2 = frameToCanvas(line.x2, line.y2);

        drawLine(ctx, p1.x, p1.y, p2.x, p2.y, '#4CAF50', 3);
        drawArrow(ctx, p1.x, p1.y, p2.x, p2.y, line.forward_dx, line.forward_dy, '#4CAF50');
        drawLabel(ctx, line.line_name, (p1.x + p2.x) / 2, (p1.y + p2.y) / 2 - 15, '#4CAF50');
    });

    // Draw current drawing line (yellow)
    if (state.currentLine) {
        const p1 = frameToCanvas(state.currentLine.x1, state.currentLine.y1);
        const p2 = frameToCanvas(state.currentLine.x2, state.currentLine.y2);

        drawLine(ctx, p1.x, p1.y, p2.x, p2.y, '#f39c12', 3);

        if (state.direction) {
            drawArrow(ctx, p1.x, p1.y, p2.x, p2.y, state.direction.dx, state.direction.dy, '#f39c12');
        }
    }

    // Draw start point (red)
    if (state.drawStart) {
        const p = frameToCanvas(state.drawStart.x, state.drawStart.y);
        ctx.fillStyle = '#e74c3c';
        ctx.beginPath();
        ctx.arc(p.x, p.y, 5, 0, 2 * Math.PI);
        ctx.fill();
    }
}

/**
 * Draw a line
 */
function drawLine(ctx, x1, y1, x2, y2, color, width) {
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.beginPath();
    ctx.moveTo(x1, y1);
    ctx.lineTo(x2, y2);
    ctx.stroke();
}

/**
 * Draw direction arrow
 */
function drawArrow(ctx, x1, y1, x2, y2, dirX, dirY, color) {
    const midX = (x1 + x2) / 2;
    const midY = (y1 + y2) / 2;

    const arrowLength = 30;
    const arrowHeadLength = 10;

    const endX = midX + dirX * arrowLength;
    const endY = midY + dirY * arrowLength;

    // Arrow shaft
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(midX, midY);
    ctx.lineTo(endX, endY);
    ctx.stroke();

    // Arrow head
    const angle = Math.atan2(dirY, dirX);
    const headAngle = Math.PI / 6;

    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.moveTo(endX, endY);
    ctx.lineTo(
        endX - arrowHeadLength * Math.cos(angle - headAngle),
        endY - arrowHeadLength * Math.sin(angle - headAngle)
    );
    ctx.lineTo(
        endX - arrowHeadLength * Math.cos(angle + headAngle),
        endY - arrowHeadLength * Math.sin(angle + headAngle)
    );
    ctx.closePath();
    ctx.fill();
}

/**
 * Draw text label
 */
function drawLabel(ctx, text, x, y, color) {
    ctx.font = '14px sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'bottom';

    // Background
    const metrics = ctx.measureText(text);
    const padding = 4;
    ctx.fillStyle = 'rgba(0, 0, 0, 0.7)';
    ctx.fillRect(
        x - metrics.width / 2 - padding,
        y - 16 - padding,
        metrics.width + padding * 2,
        16 + padding * 2
    );

    // Text
    ctx.fillStyle = color;
    ctx.fillText(text, x, y);
}

/**
 * Draw placeholder
 */
function drawPlaceholder(ctx, width, height, message) {
    // Grid background
    ctx.strokeStyle = '#222';
    ctx.lineWidth = 1;

    for (let x = 0; x < width; x += 20) {
        ctx.beginPath();
        ctx.moveTo(x, 0);
        ctx.lineTo(x, height);
        ctx.stroke();
    }

    for (let y = 0; y < height; y += 20) {
        ctx.beginPath();
        ctx.moveTo(0, y);
        ctx.lineTo(width, y);
        ctx.stroke();
    }

    // Message
    ctx.font = '18px sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillStyle = '#666';
    ctx.fillText(message, width / 2, height / 2);
}

/**
 * Clear canvas
 */
function clearCanvas() {
    const ctx = elements.ctx;
    ctx.clearRect(0, 0, elements.canvas.width, elements.canvas.height);
    drawPlaceholder(ctx, elements.canvas.width, elements.canvas.height, 'Select a camera to begin');
}

/**
 * Handle window resize
 */
function onResize() {
    if (state.selectedCamera && state.frameImage) {
        calculateCanvasScale();
        drawCanvas();
    }
}

/**
 * Show notification
 */
function showNotification(message, type = 'info') {
    // Use gt-utils if available
    if (typeof window.showNotification === 'function') {
        window.showNotification(message, type);
        return;
    }

    // Fallback to console
    console.log(`[${type.toUpperCase()}] ${message}`);
}

/**
 * Debounce utility
 */
function debounce(func, wait) {
    let timeout;
    return function executedFunction(...args) {
        const later = () => {
            clearTimeout(timeout);
            func(...args);
        };
        clearTimeout(timeout);
        timeout = setTimeout(later, wait);
    };
}

// Initialize when DOM is ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
} else {
    init();
}
