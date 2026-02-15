/**
 * ImagePlayerShim - Makes an <img> element behave like an HTMLVideoElement
 *
 * This allows the annotation system (canvas overlay, bbox drawing, scenario workflow)
 * to work with static thumbnail images instead of video files.
 *
 * Canvas drawImage() accepts both HTMLVideoElement and HTMLImageElement,
 * so the shim just needs to provide video-like properties/methods.
 */

class ImagePlayerShim {
    constructor(imgElement) {
        this.element = imgElement;
        this._listeners = {};
        this._loaded = false;

        // If image is already loaded, mark as ready
        if (imgElement.naturalWidth > 0) {
            this._loaded = true;
        }

        // Listen for image load
        imgElement.addEventListener('load', () => {
            this._loaded = true;
            this._fireEvent('loadedmetadata');
            this._fireEvent('loadeddata');
            this._fireEvent('canplay');
        });
    }

    // Video-like properties (all static since it's a single image)
    get currentTime() { return 0; }
    set currentTime(val) {
        // Immediately fire seeked since there's nothing to seek to
        setTimeout(() => this._fireEvent('seeked'), 0);
    }

    get duration() { return 0; }
    get paused() { return true; }
    get ended() { return false; }
    get readyState() { return this._loaded ? 4 : 0; }
    get playbackRate() { return 1; }
    set playbackRate(val) { /* noop */ }

    // Dimensions from the image
    get videoWidth() { return this.element.naturalWidth || this.element.width || 0; }
    get videoHeight() { return this.element.naturalHeight || this.element.height || 0; }
    get offsetWidth() { return this.element.offsetWidth; }
    get offsetHeight() { return this.element.offsetHeight; }

    // Style proxy
    get style() { return this.element.style; }

    // Noop methods
    play() { return Promise.resolve(); }
    pause() { /* noop */ }
    load() {
        if (this._loaded) {
            setTimeout(() => this._fireEvent('loadedmetadata'), 0);
        }
    }

    // Event system
    addEventListener(event, callback, options) {
        if (!this._listeners[event]) {
            this._listeners[event] = [];
        }

        const entry = { callback, once: options?.once || false };
        this._listeners[event].push(entry);

        // If image is already loaded and they're listening for load events, fire immediately
        if (this._loaded && ['loadedmetadata', 'loadeddata', 'canplay'].includes(event)) {
            setTimeout(() => callback(), 0);
        }
    }

    removeEventListener(event, callback) {
        if (this._listeners[event]) {
            this._listeners[event] = this._listeners[event].filter(e => e.callback !== callback);
        }
    }

    _fireEvent(event) {
        const listeners = this._listeners[event] || [];
        const remaining = [];
        for (const entry of listeners) {
            entry.callback();
            if (!entry.once) {
                remaining.push(entry);
            }
        }
        this._listeners[event] = remaining;
    }
}

/**
 * Initialize image mode for the annotation page.
 *
 * @param {string} thumbnailUrl - URL of the thumbnail image
 * @returns {ImagePlayerShim} - The shim that replaces videoPlayer
 */
function initImageMode(thumbnailUrl) {
    console.log('[ImageMode] Initializing with thumbnail:', thumbnailUrl);

    const imgElement = document.getElementById('thumbnail-image');
    if (!imgElement) {
        console.error('[ImageMode] thumbnail-image element not found');
        return null;
    }

    // Set the image source
    imgElement.src = thumbnailUrl;
    imgElement.style.display = 'block';

    // Hide the video element
    const videoEl = document.getElementById('video-player');
    if (videoEl) {
        videoEl.style.display = 'none';
    }

    // Add image-mode class to the container
    const container = document.querySelector('.annotation-container');
    if (container) {
        container.classList.add('image-mode');
    }

    // Create and return the shim
    const shim = new ImagePlayerShim(imgElement);

    console.log('[ImageMode] Shim created, image dimensions:', shim.videoWidth, 'x', shim.videoHeight);

    // Flag for other code to detect image mode
    window.isImageMode = true;

    return shim;
}
