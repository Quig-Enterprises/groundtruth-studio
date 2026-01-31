/**
 * Audio Waveform Visualization
 * Displays audio waveform behind timeline to help identify key audio moments
 */

let waveformCanvas, waveformContext;
let waveformMarkersCanvas, waveformMarkersContext;
let waveformProgress;
let waveformData = null;
let audioContext = null;

/**
 * Initialize waveform on page load
 */
function initWaveform() {
    waveformCanvas = document.getElementById('waveform-canvas');
    waveformMarkersCanvas = document.getElementById('waveform-markers-canvas');
    waveformProgress = document.getElementById('waveform-progress');

    if (!waveformCanvas || !waveformMarkersCanvas) return;

    waveformContext = waveformCanvas.getContext('2d');
    waveformMarkersContext = waveformMarkersCanvas.getContext('2d');

    // Make canvas clickable for seeking
    waveformCanvas.addEventListener('click', handleWaveformClick);

    // Update progress indicator on video timeupdate
    const videoPlayer = document.getElementById('video-player');
    if (videoPlayer) {
        videoPlayer.addEventListener('timeupdate', updateWaveformProgress);
    }
}

/**
 * Load and visualize audio from video
 */
async function loadAudioWaveform(videoElement) {
    if (!videoElement || !videoElement.src) return;

    const container = waveformCanvas?.parentElement;
    if (!container) return;

    try {
        // Show loading state
        const loading = document.createElement('div');
        loading.className = 'waveform-loading';
        loading.textContent = 'Loading audio waveform...';
        loading.id = 'waveform-loading-msg';
        container.appendChild(loading);

        // Create audio context if not exists
        if (!audioContext) {
            audioContext = new (window.AudioContext || window.webkitAudioContext)();
        }

        // Fetch video file
        const response = await fetch(videoElement.src);
        if (!response.ok) {
            throw new Error(`Failed to fetch video: ${response.status}`);
        }

        const arrayBuffer = await response.arrayBuffer();

        // Decode audio data
        const audioBuffer = await audioContext.decodeAudioData(arrayBuffer);

        // Extract waveform data
        waveformData = extractWaveformData(audioBuffer);

        // Draw waveform
        drawWaveform(waveformData);

        // Remove loading state
        const loadingMsg = document.getElementById('waveform-loading-msg');
        if (loadingMsg && loadingMsg.parentElement) {
            loadingMsg.remove();
        }

    } catch (error) {
        console.warn('Waveform visualization unavailable:', error.message);

        // Show a simple placeholder instead
        const loadingMsg = document.getElementById('waveform-loading-msg');
        if (loadingMsg && loadingMsg.parentElement) {
            loadingMsg.textContent = 'Waveform unavailable';
            loadingMsg.style.color = '#7f8c8d';
            loadingMsg.style.fontSize = '12px';
        }

        // Draw a simple placeholder waveform
        drawPlaceholderWaveform();
    }
}

/**
 * Draw a simple placeholder waveform when audio extraction fails
 */
function drawPlaceholderWaveform() {
    if (!waveformCanvas) return;

    const container = waveformCanvas.parentElement;
    const containerRect = container.getBoundingClientRect();
    const width = containerRect.width - 20;
    const height = 60;
    const dpr = window.devicePixelRatio || 1;

    // Set canvas internal resolution
    waveformCanvas.width = width * dpr;
    waveformCanvas.height = height * dpr;

    // Set canvas CSS size to match
    waveformCanvas.style.width = width + 'px';
    waveformCanvas.style.height = height + 'px';

    // Reset scale after setting dimensions
    waveformContext.setTransform(1, 0, 0, 1, 0, 0);
    waveformContext.scale(dpr, dpr);

    // Draw simple bars
    const bars = 500; // Increased for higher resolution placeholder
    const barWidth = width / bars;
    const barGap = barWidth * 0.2;
    const actualBarWidth = barWidth - barGap;

    waveformContext.fillStyle = '#7f8c8d';

    for (let i = 0; i < bars; i++) {
        const barHeight = Math.random() * height * 0.5 + height * 0.1;
        const x = i * barWidth;
        const y = (height - barHeight) / 2;
        waveformContext.fillRect(x, y, actualBarWidth, barHeight);
    }

    // Draw keyframe markers on separate canvas (once)
    drawKeyframeMarkers();
}

/**
 * Extract waveform data from audio buffer
 */
function extractWaveformData(audioBuffer) {
    const rawData = audioBuffer.getChannelData(0); // Use first channel
    const samples = 2000; // Number of bars to display (increased for higher resolution)
    const blockSize = Math.floor(rawData.length / samples);
    const filteredData = [];

    for (let i = 0; i < samples; i++) {
        let blockStart = blockSize * i;
        let max = 0;

        // Find peak amplitude in this block (instead of average)
        for (let j = 0; j < blockSize; j++) {
            const absValue = Math.abs(rawData[blockStart + j]);
            if (absValue > max) {
                max = absValue;
            }
        }
        filteredData.push(max);
    }

    // Normalize data
    const maxValue = Math.max(...filteredData);
    return filteredData.map(n => n / maxValue);
}

/**
 * Draw waveform on canvas
 */
function drawWaveform(data) {
    if (!waveformCanvas || !data) return;

    // Set canvas resolution
    const container = waveformCanvas.parentElement;
    const dpr = window.devicePixelRatio || 1;

    // Get actual display size from parent container
    const containerRect = container.getBoundingClientRect();
    const width = containerRect.width - 20; // Account for padding
    const height = 60; // Fixed height as per CSS

    // Set canvas internal resolution
    waveformCanvas.width = width * dpr;
    waveformCanvas.height = height * dpr;

    // Set canvas CSS size to match
    waveformCanvas.style.width = width + 'px';
    waveformCanvas.style.height = height + 'px';

    // Reset scale after setting dimensions
    waveformContext.setTransform(1, 0, 0, 1, 0, 0);
    waveformContext.scale(dpr, dpr);

    const rect = { width, height };

    // Clear canvas
    waveformContext.clearRect(0, 0, rect.width, rect.height);

    // Draw waveform bars
    const barWidth = rect.width / data.length;
    const barGap = barWidth * 0.2;
    const actualBarWidth = barWidth - barGap;

    waveformContext.fillStyle = '#3498db';

    data.forEach((amplitude, i) => {
        const barHeight = amplitude * rect.height * 0.8;
        const x = i * barWidth;
        const y = (rect.height - barHeight) / 2;

        waveformContext.fillRect(x, y, actualBarWidth, barHeight);
    });

    // Draw keyframe markers on separate canvas (once)
    drawKeyframeMarkers();
}

/**
 * Update progress indicator position
 */
function updateWaveformProgress() {
    if (!waveformProgress || !videoPlayer) return;

    const progress = videoPlayer.currentTime / videoPlayer.duration;

    // Canvas width matches the actual waveform width (container - 20px padding)
    const canvasWidth = waveformCanvas.getBoundingClientRect().width;

    // Position indicator: 10px left padding + progress through the canvas
    waveformProgress.style.left = (10 + progress * canvasWidth) + 'px';

    // Only redraw markers (not entire waveform) to update active state
    updateKeyframeMarkers();
}

/**
 * Handle click on waveform to seek video
 */
function handleWaveformClick(e) {
    if (!videoPlayer) return;

    const rect = waveformCanvas.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const progress = x / rect.width;

    videoPlayer.currentTime = progress * videoPlayer.duration;
}

/**
 * Draw keyframe annotation markers on waveform (initial setup)
 */
function drawKeyframeMarkers() {
    if (!waveformMarkersCanvas || !videoPlayer) return;

    const container = waveformMarkersCanvas.parentElement;
    const containerRect = container.getBoundingClientRect();
    const width = containerRect.width - 20;
    const height = 60;
    const dpr = window.devicePixelRatio || 1;

    // Set canvas internal resolution
    waveformMarkersCanvas.width = width * dpr;
    waveformMarkersCanvas.height = height * dpr;

    // Set canvas CSS size
    waveformMarkersCanvas.style.width = width + 'px';
    waveformMarkersCanvas.style.height = height + 'px';

    // Reset scale
    waveformMarkersContext.setTransform(1, 0, 0, 1, 0, 0);
    waveformMarkersContext.scale(dpr, dpr);

    // Draw markers
    updateKeyframeMarkers();
}

/**
 * Update keyframe markers (called during playback)
 */
function updateKeyframeMarkers() {
    if (!waveformMarkersCanvas || !videoPlayer) return;

    const duration = videoPlayer.duration;
    if (!duration) return;

    const canvasWidth = waveformMarkersCanvas.getBoundingClientRect().width;
    const canvasHeight = 60;

    // Clear the markers canvas
    waveformMarkersContext.clearRect(0, 0, canvasWidth, canvasHeight);

    // Get keyframe annotations from global state (if available)
    const annotations = window.currentKeyframeAnnotations || [];
    const currentTime = videoPlayer.currentTime;
    const tolerance = 0.5; // Grace period for highlighting

    annotations.forEach(anno => {
        const position = (anno.timestamp / duration) * canvasWidth;
        const isActive = Math.abs(anno.timestamp - currentTime) < tolerance;

        // Use different colors for active vs inactive keyframes
        const color = isActive ? '#f39c12' : '#e74c3c'; // Orange for active, red for inactive
        const lineWidth = isActive ? 3 : 2;

        // Draw vertical line marker
        waveformMarkersContext.strokeStyle = color;
        waveformMarkersContext.lineWidth = lineWidth;
        waveformMarkersContext.beginPath();
        waveformMarkersContext.moveTo(position, 0);
        waveformMarkersContext.lineTo(position, canvasHeight);
        waveformMarkersContext.stroke();

        // Draw small triangle at top (larger if active)
        const triangleSize = isActive ? 6 : 4;
        waveformMarkersContext.fillStyle = color;
        waveformMarkersContext.beginPath();
        waveformMarkersContext.moveTo(position, 0);
        waveformMarkersContext.lineTo(position - triangleSize, triangleSize * 2);
        waveformMarkersContext.lineTo(position + triangleSize, triangleSize * 2);
        waveformMarkersContext.closePath();
        waveformMarkersContext.fill();

        // Add glow effect for active keyframe
        if (isActive) {
            waveformMarkersContext.shadowColor = '#f39c12';
            waveformMarkersContext.shadowBlur = 10;
            waveformMarkersContext.strokeStyle = color;
            waveformMarkersContext.lineWidth = lineWidth;
            waveformMarkersContext.beginPath();
            waveformMarkersContext.moveTo(position, 0);
            waveformMarkersContext.lineTo(position, canvasHeight);
            waveformMarkersContext.stroke();
            waveformMarkersContext.shadowBlur = 0; // Reset shadow
        }
    });
}

/**
 * Redraw waveform with updated keyframe markers
 */
function redrawWaveformMarkers() {
    if (waveformData) {
        drawWaveform(waveformData);
    } else {
        drawPlaceholderWaveform();
    }
}

/**
 * Resize waveform canvas on window resize
 */
function resizeWaveform() {
    if (waveformData) {
        drawWaveform(waveformData);
    }
}

// Initialize on DOM load
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initWaveform);
} else {
    initWaveform();
}

// Handle window resize
window.addEventListener('resize', resizeWaveform);
