(function () {
    'use strict';

    // ================================================================
    // STATE
    // ================================================================

    var currentGroupId = null;
    var groups = [];
    var allCameras = [];   // all camera_locations for checklist
    var activeStreams = new Map(); // camera_id → { pc: RTCPeerConnection, video: HTMLVideoElement }
    var isDrawingMode = false;
    var selections = [];
    var drawingState = { active: false, canvas: null, ctx: null, startX: 0, startY: 0, cameraId: null };
    var editingGroupId = null; // null = creating new, integer = editing existing
    var ptzTargetState = null; // { sourceCameraId, targetCameraId, bboxX, bboxY, estimatedPan, estimatedTilt, method }
    var _ptzHudTimers = {};   // camera_id → interval id for HUD position polling

    // ================================================================
    // BOOT
    // ================================================================

    var camerasReady = null; // promise that resolves when allCameras is loaded

    document.addEventListener('DOMContentLoaded', function () {
        initToolbar();
        initModal();
        initSelectionsPanel();
        camerasReady = loadAllCameras();
        loadGroups();

        // Resize bearing bar canvases on window resize
        window.addEventListener('resize', function () {
            document.querySelectorAll('.bearing-bar').forEach(function (bar) {
                var wrapper = bar.closest('.video-wrapper');
                if (wrapper) bar.width = wrapper.offsetWidth;
            });
        });
    });

    // ================================================================
    // TOOLBAR
    // ================================================================

    function initToolbar() {
        var sel = document.getElementById('group-select');
        sel.addEventListener('change', function () {
            selectGroup(this.value ? parseInt(this.value) : null);
        });

        document.getElementById('btn-recompute').addEventListener('click', recomputeGroups);
        document.getElementById('btn-manage-groups').addEventListener('click', openGroupModal);

        var drawBtn = document.getElementById('btn-draw-bbox');
        drawBtn.addEventListener('click', function () {
            isDrawingMode = !isDrawingMode;
            drawBtn.classList.toggle('active', isDrawingMode);
            drawBtn.textContent = isDrawingMode ? 'Cancel Drawing' : 'Draw BBox';
            // Toggle pointer-events on all canvases
            document.querySelectorAll('.bbox-overlay').forEach(function (c) {
                c.classList.toggle('drawing', isDrawingMode);
            });
        });

        var calBtn = document.getElementById('btn-calibration');
        if (calBtn) {
            calBtn.addEventListener('click', openCalibrationModal);
        }

        if (!CAN_WRITE) {
            drawBtn.style.display = 'none';
            document.getElementById('btn-recompute').style.display = 'none';
            if (calBtn) calBtn.style.display = 'none';
        }
    }

    // ================================================================
    // GROUP LOADING
    // ================================================================

    function loadGroups() {
        fetch('/api/camera-sync/groups')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!data.success) return;
                groups = data.groups || [];
                var sel = document.getElementById('group-select');
                // Keep first option
                while (sel.options.length > 1) sel.remove(1);
                groups.forEach(function (g) {
                    var opt = document.createElement('option');
                    opt.value = g.id;
                    opt.textContent = g.group_name + ' (' + (g.camera_ids || []).length + ' cameras)';
                    sel.appendChild(opt);
                });
                // Wait for allCameras to load before auto-selecting a group
                // (selectGroup needs camInfo for PTZ detection)
                return camerasReady.then(function () {
                    var urlGroup = new URLSearchParams(window.location.search).get('group');
                    if (urlGroup && groups.find(function (g) { return g.id === parseInt(urlGroup); })) {
                        sel.value = urlGroup;
                        selectGroup(parseInt(urlGroup), true);
                    } else if (groups.length === 1) {
                        sel.value = groups[0].id;
                        selectGroup(groups[0].id);
                    }
                });
            })
            .catch(function (e) { console.error('Failed to load groups:', e); });
    }

    function loadAllCameras() {
        return fetch('/api/camera-map/placements')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data.success) allCameras = data.placements || [];
            })
            .catch(function (e) { console.error('Failed to load cameras:', e); });
    }

    function makeEmptyState(title, msg) {
        var div = document.createElement('div');
        div.className = 'empty-state';
        div.innerHTML = '<h3>' + title + '</h3><p>' + msg + '</p>';
        return div;
    }

    function selectGroup(groupId, skipUrlUpdate) {
        stopAllStreams();
        currentGroupId = groupId;

        // Update URL with selected group
        if (!skipUrlUpdate) {
            var url = new URL(window.location);
            if (groupId) {
                url.searchParams.set('group', groupId);
            } else {
                url.searchParams.delete('group');
            }
            history.replaceState(null, '', url);
        }
        var grid = document.getElementById('camera-grid');
        var drawBtn = document.getElementById('btn-draw-bbox');

        if (!groupId) {
            grid.innerHTML = '';
            grid.appendChild(makeEmptyState('No Group Selected',
                'Select an overlap group from the toolbar to view cameras with overlapping fields of view, or click "Recompute" to auto-detect groups.'));
            drawBtn.disabled = true;
            return;
        }

        var group = groups.find(function (g) { return g.id === groupId; });
        if (!group) return;

        var cameraIds = group.camera_ids || [];
        if (cameraIds.length === 0) {
            grid.innerHTML = '';
            grid.appendChild(makeEmptyState('No Cameras in Group', 'This group has no cameras assigned.'));
            drawBtn.disabled = true;
            return;
        }

        drawBtn.disabled = false;

        // Set grid columns
        grid.className = 'camera-grid';
        if (cameraIds.length === 1) grid.classList.add('cols-1');
        else if (cameraIds.length <= 4) grid.classList.add('cols-2');
        else grid.classList.add('cols-3');

        grid.innerHTML = '';

        cameraIds.forEach(function (camId) {
            var camInfo = allCameras.find(function (c) { return c.camera_id === camId; });
            var cell = buildCameraCell(camId, camInfo);
            grid.appendChild(cell);
            startStream(camId, cell.querySelector('video'), cell.querySelector('.stream-status'));
        });

        loadSelections();

        // Load calibration badges for PTZ cameras
        setTimeout(loadAllCalibrationBadges, 500);
    }

    // ================================================================
    // CAMERA GRID BUILDER
    // ================================================================

    function buildCameraCell(cameraId, camInfo) {
        var cell = document.createElement('div');
        cell.className = 'camera-cell';
        cell.dataset.cameraId = cameraId;

        // Header
        var header = document.createElement('div');
        header.className = 'camera-header';

        var name = document.createElement('span');
        name.className = 'camera-name';
        name.textContent = (camInfo && camInfo.camera_name) || cameraId;
        header.appendChild(name);

        var isPtz = camInfo && camInfo.is_ptz;
        if (isPtz) {
            var badge = document.createElement('span');
            badge.className = 'ptz-badge';
            badge.textContent = 'PTZ';
            header.appendChild(badge);
        }

        if (isPtz) {
            var calBadge = document.createElement('span');
            calBadge.className = 'cal-badge loading';
            calBadge.id = 'cal-badge-' + cameraId;
            calBadge.textContent = 'Cal: ...';
            header.appendChild(calBadge);
        }

        var status = document.createElement('span');
        status.className = 'stream-status';
        header.appendChild(status);

        cell.appendChild(header);

        // Video wrapper with canvas overlay
        var wrapper = document.createElement('div');
        wrapper.className = 'video-wrapper';

        var video = document.createElement('video');
        video.autoplay = true;
        video.muted = true;
        video.playsInline = true;
        wrapper.appendChild(video);

        var canvas = document.createElement('canvas');
        canvas.className = 'bbox-overlay';
        canvas.dataset.cameraId = cameraId;
        wrapper.appendChild(canvas);

        // PTZ directional pad for PTZ cameras
        if (isPtz) {
            var pad = buildPtzPad(cameraId);
            wrapper.appendChild(pad);

            // PTZ HUD: bearing bar + numeric readout
            var hud = document.createElement('div');
            hud.className = 'ptz-hud';
            hud.id = 'ptz-hud-' + cameraId;

            var bar = document.createElement('canvas');
            bar.className = 'bearing-bar';
            bar.id = 'bearing-bar-' + cameraId;
            bar.height = 28;
            hud.appendChild(bar);

            var readout = document.createElement('div');
            readout.className = 'ptz-readout';
            readout.id = 'ptz-readout-' + cameraId;
            readout.textContent = '---';
            hud.appendChild(readout);

            // Compass calibrate button (bottom-left, above PTZ pad level)
            var calBtn = document.createElement('button');
            calBtn.className = 'ptz-compass-btn';
            calBtn.id = 'ptz-compass-btn-' + cameraId;
            calBtn.textContent = '\u2316'; // crosshair icon
            calBtn.title = 'Calibrate compass';
            calBtn.addEventListener('click', function () {
                openCompassCalibrationDialog(cameraId);
            });
            hud.appendChild(calBtn);

            wrapper.appendChild(hud);

            // Start polling position once stream connects
            startPtzHudPolling(cameraId, camInfo);
        }

        cell.appendChild(wrapper);

        // Size canvas when video metadata loads
        video.addEventListener('loadedmetadata', function () {
            canvas.width = video.videoWidth || video.offsetWidth;
            canvas.height = video.videoHeight || video.offsetHeight;
        });

        // Bbox drawing events on canvas
        initCanvasDrawing(canvas, cameraId);

        return cell;
    }

    // ================================================================
    // STREAMING (WebRTC with MSE fallback)
    // ================================================================

    function startStream(cameraId, videoEl, statusEl) {
        // WebRTC fails to decode H.264 High Profile from these cameras (videoWidth stays 0).
        // Use MSE (MP4 via go2rtc) directly — reliable, works with any H.264 profile.
        // PTZ cameras use the sub-stream for lower latency.
        var camInfo = allCameras.find(function (c) { return c.camera_id === cameraId; });
        var streamId = (camInfo && camInfo.is_ptz) ? cameraId + '_sub' : cameraId;
        startStreamMSE(streamId, videoEl, statusEl);
    }

    function startStreamWebRTC(cameraId, videoEl, statusEl) {
        console.log('[STREAM]', cameraId, 'starting WebRTC');
        statusEl.className = 'stream-status connecting';

        var pc = new RTCPeerConnection({
            iceServers: [{ urls: 'stun:stun.l.google.com:19302' }]
        });

        pc.addTransceiver('video', { direction: 'recvonly' });
        pc.addTransceiver('audio', { direction: 'recvonly' });

        pc.ontrack = function (ev) {
            console.log('[STREAM]', cameraId, 'WebRTC ontrack:', ev.track.kind);
            if (ev.track.kind === 'video') {
                videoEl.srcObject = ev.streams[0];
                videoEl.play().catch(function (e) {
                    console.warn('[STREAM]', cameraId, 'autoplay blocked, retrying muted:', e);
                    videoEl.muted = true;
                    videoEl.play().catch(function () {});
                });
                statusEl.className = 'stream-status connected';
            }
        };

        // Check if video actually decodes; wait up to 15s for keyframe on high-res cameras
        var checkCount = 0;
        var decodeCheck = setInterval(function () {
            checkCount++;
            console.log('[STREAM]', cameraId, 'decode check #' + checkCount + ': videoWidth=', videoEl.videoWidth, 'paused=', videoEl.paused, 'readyState=', videoEl.readyState);
            if (videoEl.videoWidth > 0 && !videoEl.paused) {
                console.log('[STREAM]', cameraId, 'WebRTC decode OK:', videoEl.videoWidth + 'x' + videoEl.videoHeight);
                clearInterval(decodeCheck);
            } else if (checkCount >= 5) {
                // 5 checks x 3s = 15 seconds — give up and fall back
                console.warn('[STREAM]', cameraId, 'WebRTC no decode after 15s — falling back to MP4 stream');
                clearInterval(decodeCheck);
                stopStream(cameraId);
                startStreamMSE(cameraId, videoEl, statusEl);
            }
        }, 3000);

        pc.oniceconnectionstatechange = function () {
            console.log('[STREAM]', cameraId, 'ICE state:', pc.iceConnectionState);
            if (pc.iceConnectionState === 'disconnected' || pc.iceConnectionState === 'failed') {
                statusEl.className = 'stream-status error';
                clearInterval(decodeCheck);
                setTimeout(function () {
                    if (currentGroupId && activeStreams.has(cameraId)) {
                        stopStream(cameraId);
                        var cell = document.querySelector('[data-camera-id="' + cameraId + '"]');
                        if (cell) {
                            startStream(cameraId, cell.querySelector('video'), cell.querySelector('.stream-status'));
                        }
                    }
                }, 3000);
            }
        };

        pc.createOffer().then(function (offer) {
            console.log('[STREAM]', cameraId, 'SDP offer created');
            return pc.setLocalDescription(offer);
        }).then(function () {
            return new Promise(function (resolve) {
                if (pc.iceGatheringState === 'complete') {
                    resolve();
                } else {
                    var timeout = setTimeout(resolve, 2000);
                    pc.onicegatheringstatechange = function () {
                        if (pc.iceGatheringState === 'complete') {
                            clearTimeout(timeout);
                            resolve();
                        }
                    };
                }
            });
        }).then(function () {
            console.log('[STREAM]', cameraId, 'sending SDP offer to server');
            return fetch('/api/camera-sync/webrtc-offer', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    camera: cameraId,
                    sdp: pc.localDescription.sdp
                })
            });
        }).then(function (resp) {
            console.log('[STREAM]', cameraId, 'signaling response:', resp.status);
            if (!resp.ok) throw new Error('Signaling failed: ' + resp.status);
            return resp.text();
        }).then(function (sdpAnswer) {
            console.log('[STREAM]', cameraId, 'got SDP answer, setting remote description');
            return pc.setRemoteDescription(new RTCSessionDescription({
                type: 'answer',
                sdp: sdpAnswer
            }));
        }).then(function () {
            console.log('[STREAM]', cameraId, 'WebRTC setup complete, waiting for media');
        }).catch(function (e) {
            console.error('[STREAM]', cameraId, 'WebRTC error:', e);
            statusEl.className = 'stream-status error';
            clearInterval(decodeCheck);
            console.log('[STREAM]', cameraId, 'falling back to MSE after WebRTC error');
            startStreamMSE(cameraId, videoEl, statusEl);
        });

        activeStreams.set(cameraId, { pc: pc, video: videoEl, decodeCheck: decodeCheck });
    }

    // Fallback 1: MP4 stream via go2rtc (reliable, handles any codec go2rtc can transcode)
    function startStreamMSE(cameraId, videoEl, statusEl) {
        console.log('[MP4]', cameraId, 'starting MP4 stream via go2rtc');
        statusEl.className = 'stream-status connecting';

        // go2rtc serves fragmented MP4 over HTTP — browser plays it natively
        var mp4Url = '/api/go2rtc/stream.mp4?src=' + encodeURIComponent(cameraId);
        videoEl.src = mp4Url;
        videoEl.muted = true;
        videoEl.play().catch(function (e) {
            console.warn('[MP4]', cameraId, 'play failed:', e);
        });

        // Monitor for successful playback
        var checkCount = 0;
        var mp4Check = setInterval(function () {
            checkCount++;
            if (videoEl.videoWidth > 0 && !videoEl.paused) {
                console.log('[MP4]', cameraId, 'playing:', videoEl.videoWidth + 'x' + videoEl.videoHeight);
                statusEl.className = 'stream-status connected';
                clearInterval(mp4Check);
            } else if (checkCount >= 10) {
                console.warn('[MP4]', cameraId, 'no playback after 10s — falling back to snapshots');
                clearInterval(mp4Check);
                stopStream(cameraId);
                startStreamSnap(cameraId, videoEl, statusEl);
            }
        }, 1000);

        // Handle stream errors
        videoEl.onerror = function () {
            console.error('[MP4]', cameraId, 'stream error');
            clearInterval(mp4Check);
            statusEl.className = 'stream-status connecting';
            // Retry after delay
            setTimeout(function () {
                if (currentGroupId && activeStreams.has(cameraId)) {
                    stopStream(cameraId);
                    startStreamSnap(cameraId, videoEl, statusEl);
                }
            }, 3000);
        };

        activeStreams.set(cameraId, { video: videoEl, mp4Check: mp4Check });
    }

    // Fallback 2: JPEG snapshot polling (last resort)
    function startStreamSnap(cameraId, videoEl, statusEl) {
        console.log('[SNAP]', cameraId, 'starting JPEG snapshot fallback');
        statusEl.className = 'stream-status connecting';

        // Replace video element with an img element for snapshot polling
        var img = document.createElement('img');
        img.style.width = '100%';
        img.style.height = '100%';
        img.style.objectFit = 'contain';
        img.style.background = '#000';
        videoEl.parentNode.replaceChild(img, videoEl);

        var running = true;
        var frameUrl = '/api/camera-sync/snapshot/' + encodeURIComponent(cameraId);

        function fetchFrame() {
            if (!running) return;
            img.src = frameUrl + '?t=' + Date.now();
        }

        img.onload = function () {
            statusEl.className = 'stream-status connected';
            if (running) setTimeout(fetchFrame, 2000);
        };

        img.onerror = function () {
            statusEl.className = 'stream-status connecting';
            if (running) setTimeout(fetchFrame, 5000);
        };

        fetchFrame();
        activeStreams.set(cameraId, { img: img, stopSnap: function () { running = false; } });
    }

    function stopStream(cameraId) {
        var stream = activeStreams.get(cameraId);
        if (stream) {
            if (stream.decodeCheck) clearInterval(stream.decodeCheck);
            if (stream.mp4Check) clearInterval(stream.mp4Check);
            if (stream.stopSnap) stream.stopSnap();
            if (stream.pc) stream.pc.close();
            if (stream.ws) { try { stream.ws.close(); } catch (e) {} }
            if (stream.video) {
                if (stream.video.srcObject) {
                    stream.video.srcObject.getTracks().forEach(function (t) { t.stop(); });
                    stream.video.srcObject = null;
                }
                // Clear MP4 stream src
                if (stream.video.src && !stream.video.srcObject) {
                    stream.video.removeAttribute('src');
                    stream.video.load();
                }
            }
            activeStreams.delete(cameraId);
        }
    }

    function stopAllStreams() {
        activeStreams.forEach(function (_, camId) { stopStream(camId); });
        stopAllPtzHudPolling();
    }

    // ================================================================
    // PTZ CONTROLS
    // ================================================================

    function buildPtzPad(cameraId) {
        var pad = document.createElement('div');
        pad.className = 'ptz-pad';

        // 3x3 grid: directional arrows + center stop
        var dirs = [
            ['up-left',  '\u2196', 1], ['up',    '\u25B2', 2], ['up-right',  '\u2197', 3],
            ['left',     '\u25C0', 4], [null,     null,     5], ['right',     '\u25B6', 6],
            ['down-left','\u2199', 7], ['down',  '\u25BC', 8], ['down-right','\u2198', 9]
        ];

        dirs.forEach(function (d) {
            var btn = document.createElement('button');
            btn.className = 'ptz-btn';
            if (d[0]) {
                btn.textContent = d[1];
                btn.title = d[0];
                btn.dataset.direction = d[0];
                // Start moving on press, stop on release
                btn.addEventListener('mousedown', function (e) {
                    e.preventDefault();
                    ptzMove(cameraId, d[0]);
                });
                btn.addEventListener('mouseup', function (e) {
                    e.preventDefault();
                    ptzStop(cameraId);
                });
                btn.addEventListener('mouseleave', function (e) {
                    ptzStop(cameraId);
                });
                // Touch support
                btn.addEventListener('touchstart', function (e) {
                    e.preventDefault();
                    ptzMove(cameraId, d[0]);
                }, { passive: false });
                btn.addEventListener('touchend', function (e) {
                    e.preventDefault();
                    ptzStop(cameraId);
                }, { passive: false });
            } else {
                // Center cell — zoom controls
                btn.className = 'ptz-btn ptz-zoom-group';
                btn.innerHTML = '<span class="ptz-zoom-in" title="Zoom in">+</span><span class="ptz-zoom-out" title="Zoom out">&minus;</span>';
                btn.querySelector('.ptz-zoom-in').addEventListener('mousedown', function (e) {
                    e.stopPropagation(); ptzZoom(cameraId, 'in');
                });
                btn.querySelector('.ptz-zoom-in').addEventListener('mouseup', function (e) {
                    e.stopPropagation(); ptzStop(cameraId);
                });
                btn.querySelector('.ptz-zoom-in').addEventListener('mouseleave', function () {
                    ptzStop(cameraId);
                });
                btn.querySelector('.ptz-zoom-out').addEventListener('mousedown', function (e) {
                    e.stopPropagation(); ptzZoom(cameraId, 'out');
                });
                btn.querySelector('.ptz-zoom-out').addEventListener('mouseup', function (e) {
                    e.stopPropagation(); ptzStop(cameraId);
                });
                btn.querySelector('.ptz-zoom-out').addEventListener('mouseleave', function () {
                    ptzStop(cameraId);
                });
            }
            pad.appendChild(btn);
        });

        return pad;
    }

    var _ptzActive = null; // camera_id of active PTZ command, or null
    var _ptzStopTimer = null;
    var LIMIT_MARGIN = 0.03; // how close to limit before warning

    function ptzMove(cameraId, direction) {
        if (_ptzStopTimer) { clearTimeout(_ptzStopTimer); _ptzStopTimer = null; }
        _ptzActive = cameraId;

        // Check travel limits instantly
        checkTravelLimit(cameraId, direction);

        fetch('/api/camera-sync/ptz/move', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ camera_id: cameraId, direction: direction })
        }).then(function (r) { return r.json(); }).then(function (data) {
            if (!data.success) console.error('[PTZ] move error:', data.error);
        }).catch(function (e) {
            console.error('[PTZ] move failed:', e);
        });
    }

    function ptzStop(cameraId) {
        if (!_ptzActive) return;
        // Debounce: only send one stop per gesture (mouseup + mouseleave both fire)
        if (_ptzStopTimer) return;
        _ptzStopTimer = setTimeout(function () {
            _ptzActive = null;
            _ptzStopTimer = null;
            fetch('/api/camera-sync/ptz/stop', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ camera_id: cameraId })
            }).then(function (r) { return r.json(); }).then(function (data) {
                if (!data.success) console.error('[PTZ] stop error:', data.error);
            }).catch(function (e) {
                console.error('[PTZ] stop failed:', e);
            });
        }, 100);
    }

    function checkTravelLimit(cameraId, direction) {
        var st = _ptzHudState[cameraId];
        if (!st || !st.limits || st.lastPan == null) return;
        var limits = st.limits;
        var pan = st.lastPan;
        var tilt = st.lastTilt;
        var zoom = st.lastZoom;
        var warned = false;

        // Check pan limits
        if (direction.indexOf('left') >= 0 && limits.pan) {
            if (pan <= limits.pan.min + LIMIT_MARGIN) {
                showPtzLimitWarning(cameraId, 'left');
                warned = true;
            }
        }
        if (direction.indexOf('right') >= 0 && limits.pan) {
            if (pan >= limits.pan.max - LIMIT_MARGIN) {
                showPtzLimitWarning(cameraId, 'right');
                warned = true;
            }
        }
        // Check tilt limits
        if (direction.indexOf('up') >= 0 && limits.tilt) {
            if (tilt >= limits.tilt.max - LIMIT_MARGIN) {
                showPtzLimitWarning(cameraId, 'up');
                warned = true;
            }
        }
        if (direction.indexOf('down') >= 0 && limits.tilt) {
            if (tilt <= limits.tilt.min + LIMIT_MARGIN) {
                showPtzLimitWarning(cameraId, 'down');
                warned = true;
            }
        }
    }

    function showPtzLimitWarning(cameraId, direction) {
        var dirLabels = {
            'left': 'Left pan limit reached',
            'right': 'Right pan limit reached',
            'up': 'Upper tilt limit reached',
            'down': 'Lower tilt limit reached',
            'up-left': 'Upper-left movement limit reached',
            'up-right': 'Upper-right movement limit reached',
            'down-left': 'Lower-left movement limit reached',
            'down-right': 'Lower-right movement limit reached',
            'zoom-in': 'Maximum zoom reached',
            'zoom-out': 'Minimum zoom reached'
        };
        var msg = dirLabels[direction] || 'Movement limit reached';
        showToast(msg, 'error');
    }

    function dismissPtzLimitWarning() {
        // No-op now — toast auto-dismisses
    }

    function ptzZoom(cameraId, direction) {
        if (_ptzStopTimer) { clearTimeout(_ptzStopTimer); _ptzStopTimer = null; }
        _ptzActive = cameraId;

        // Check zoom travel limits instantly
        var st = _ptzHudState[cameraId];
        if (st && st.limits && st.limits.zoom && st.lastZoom != null) {
            if (direction === 'in' && st.lastZoom >= st.limits.zoom.max - LIMIT_MARGIN) {
                showPtzLimitWarning(cameraId, 'zoom-in');
            } else if (direction === 'out' && st.lastZoom <= st.limits.zoom.min + LIMIT_MARGIN) {
                showPtzLimitWarning(cameraId, 'zoom-out');
            }
        }

        fetch('/api/camera-sync/ptz/zoom', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ camera_id: cameraId, direction: direction })
        }).then(function (r) { return r.json(); }).then(function (data) {
            if (!data.success) console.error('[PTZ] zoom error:', data.error);
        }).catch(function (e) {
            console.error('[PTZ] zoom failed:', e);
        });
    }

    // ================================================================
    // BBOX DRAWING
    // ================================================================

    function initCanvasDrawing(canvas, cameraId) {
        var ctx = canvas.getContext('2d');
        var startX, startY, dragging = false;

        canvas.addEventListener('mousedown', function (e) {
            if (!isDrawingMode) return;
            e.preventDefault();
            dragging = true;
            var rect = canvas.getBoundingClientRect();
            var scaleX = canvas.width / rect.width;
            var scaleY = canvas.height / rect.height;
            startX = (e.clientX - rect.left) * scaleX;
            startY = (e.clientY - rect.top) * scaleY;
            drawingState = { active: true, canvas: canvas, ctx: ctx, startX: startX, startY: startY, cameraId: cameraId };
        });

        canvas.addEventListener('mousemove', function (e) {
            if (!dragging) return;
            var rect = canvas.getBoundingClientRect();
            var scaleX = canvas.width / rect.width;
            var scaleY = canvas.height / rect.height;
            var curX = (e.clientX - rect.left) * scaleX;
            var curY = (e.clientY - rect.top) * scaleY;

            ctx.clearRect(0, 0, canvas.width, canvas.height);

            // Draw selection rectangle
            ctx.strokeStyle = '#4CAF50';
            ctx.lineWidth = 2;
            ctx.setLineDash([6, 3]);
            ctx.strokeRect(startX, startY, curX - startX, curY - startY);
            ctx.setLineDash([]);

            // Crosshair lines
            ctx.strokeStyle = 'rgba(255,255,255,0.3)';
            ctx.lineWidth = 0.5;
            ctx.beginPath();
            ctx.moveTo(curX, 0); ctx.lineTo(curX, canvas.height);
            ctx.moveTo(0, curY); ctx.lineTo(canvas.width, curY);
            ctx.stroke();
        });

        canvas.addEventListener('mouseup', function (e) {
            if (!dragging) return;
            dragging = false;
            var rect = canvas.getBoundingClientRect();
            var scaleX = canvas.width / rect.width;
            var scaleY = canvas.height / rect.height;
            var endX = (e.clientX - rect.left) * scaleX;
            var endY = (e.clientY - rect.top) * scaleY;

            var x = Math.min(startX, endX);
            var y = Math.min(startY, endY);
            var w = Math.abs(endX - startX);
            var h = Math.abs(endY - startY);

            // Minimum size check
            if (w < 5 || h < 5) {
                ctx.clearRect(0, 0, canvas.width, canvas.height);
                return;
            }

            // Draw final rectangle
            ctx.clearRect(0, 0, canvas.width, canvas.height);
            ctx.strokeStyle = '#4CAF50';
            ctx.lineWidth = 2;
            ctx.setLineDash([]);
            ctx.strokeRect(x, y, w, h);

            // Normalize to 0.0-1.0
            var normX = x / canvas.width;
            var normY = y / canvas.height;
            var normW = w / canvas.width;
            var normH = h / canvas.height;

            saveSelection(cameraId, normX, normY, normW, normH, canvas.width, canvas.height);

            drawingState.active = false;
        });

        canvas.addEventListener('mouseleave', function () {
            if (dragging) {
                dragging = false;
                ctx.clearRect(0, 0, canvas.width, canvas.height);
            }
        });

        // Touch support
        function getTouchCoords(e) {
            var touch = e.touches[0] || e.changedTouches[0];
            var rect = canvas.getBoundingClientRect();
            return { clientX: touch.clientX, clientY: touch.clientY };
        }

        canvas.addEventListener('touchstart', function (e) {
            if (!isDrawingMode) return;
            e.preventDefault();
            var t = getTouchCoords(e);
            canvas.dispatchEvent(new MouseEvent('mousedown', { clientX: t.clientX, clientY: t.clientY }));
        }, { passive: false });

        canvas.addEventListener('touchmove', function (e) {
            if (!isDrawingMode) return;
            e.preventDefault();
            var t = getTouchCoords(e);
            canvas.dispatchEvent(new MouseEvent('mousemove', { clientX: t.clientX, clientY: t.clientY }));
        }, { passive: false });

        canvas.addEventListener('touchend', function (e) {
            if (!isDrawingMode) return;
            e.preventDefault();
            var t = getTouchCoords(e);
            canvas.dispatchEvent(new MouseEvent('mouseup', { clientX: t.clientX, clientY: t.clientY }));
        }, { passive: false });
    }

    // ================================================================
    // SELECTIONS
    // ================================================================

    function saveSelection(cameraId, bboxX, bboxY, bboxW, bboxH, frameW, frameH) {
        // If bbox drawn on a PTZ camera, focus the camera on that area
        var camInfo = allCameras.find(function (c) { return c.camera_id === cameraId; });
        console.log('[BBOX] saveSelection camera:', cameraId, 'is_ptz:', camInfo && camInfo.is_ptz, 'allCameras:', allCameras.length);
        if (camInfo && camInfo.is_ptz) {
            ptzFocusBbox(cameraId, bboxX, bboxY, bboxW, bboxH);
            return;
        }

        fetch('/api/camera-sync/selections', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                source_camera_id: cameraId,
                group_id: currentGroupId,
                bbox_x: bboxX,
                bbox_y: bboxY,
                bbox_width: bboxW,
                bbox_height: bboxH,
                frame_width: frameW,
                frame_height: frameH
            })
        })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (data.success) {
                showToast('Selection saved', 'success');
                loadSelections();
                // Check if group has a PTZ camera to offer targeting
                checkPtzTargeting(cameraId, bboxX + bboxW / 2, bboxY + bboxH / 2);
            } else {
                showToast('Failed to save: ' + (data.error || 'unknown'), 'error');
            }
        })
        .catch(function (e) {
            showToast('Network error', 'error');
        });
    }

    function ptzFocusBbox(cameraId, bboxX, bboxY, bboxW, bboxH) {
        console.log('[PTZ-FOCUS] requesting focus-bbox:', cameraId, 'bbox:', bboxX.toFixed(3), bboxY.toFixed(3), bboxW.toFixed(3), bboxH.toFixed(3));
        showToast('Focusing PTZ...', 'info');
        fetch('/api/camera-sync/ptz/focus-bbox', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                camera_id: cameraId,
                bbox_x: bboxX,
                bbox_y: bboxY,
                bbox_w: bboxW,
                bbox_h: bboxH
            })
        })
        .then(function (r) {
            console.log('[PTZ-FOCUS] response status:', r.status);
            return r.json();
        })
        .then(function (data) {
            console.log('[PTZ-FOCUS] response data:', JSON.stringify(data));
            if (data.success) {
                var to = data.to;
                showToast('PTZ focused (pan=' + to.pan.toFixed(2) + ' tilt=' + to.tilt.toFixed(2) + ' zoom=' + to.zoom.toFixed(2) + ')', 'success');
            } else {
                showToast('Focus failed: ' + (data.error || 'unknown'), 'error');
            }
            // Clear the bbox overlay
            var cell = document.querySelector('[data-camera-id="' + cameraId + '"]');
            if (cell) {
                var canvas = cell.querySelector('.bbox-overlay');
                if (canvas) canvas.getContext('2d').clearRect(0, 0, canvas.width, canvas.height);
            }
        })
        .catch(function (e) {
            showToast('Network error', 'error');
        });
    }

    function loadSelections() {
        var url = '/api/camera-sync/selections?limit=50';
        if (currentGroupId) url += '&group_id=' + currentGroupId;

        fetch(url)
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!data.success) return;
                selections = data.selections || [];
                renderSelections();
            });
    }

    function renderSelections() {
        var list = document.getElementById('selections-list');
        var count = document.getElementById('selections-count');
        count.textContent = selections.length;

        if (selections.length === 0) {
            list.innerHTML = '<div style="padding:12px; color:#555; font-size:13px; text-align:center;">No selections yet. Use Draw BBox to create one.</div>';
            return;
        }

        list.innerHTML = '';
        selections.forEach(function (s) {
            var item = document.createElement('div');
            item.className = 'selection-item';
            item.innerHTML =
                '<span class="selection-camera">' + escapeHtml(s.source_camera_id) + '</span>' +
                '<span class="selection-bbox">' +
                    (s.bbox_x * 100).toFixed(1) + '%, ' +
                    (s.bbox_y * 100).toFixed(1) + '% — ' +
                    (s.bbox_width * 100).toFixed(1) + '% x ' +
                    (s.bbox_height * 100).toFixed(1) + '%' +
                '</span>' +
                '<span class="selection-time">' + formatTime(s.created_date) + '</span>';
            list.appendChild(item);
        });
    }

    function initSelectionsPanel() {
        var header = document.getElementById('selections-header');
        var panel = document.getElementById('selections-panel');
        header.addEventListener('click', function () {
            panel.classList.toggle('open');
        });
    }

    // ================================================================
    // RECOMPUTE GROUPS
    // ================================================================

    function recomputeGroups() {
        var btn = document.getElementById('btn-recompute');
        btn.disabled = true;
        btn.textContent = 'Computing...';

        fetch('/api/camera-sync/groups/compute', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                btn.disabled = false;
                btn.textContent = 'Recompute';
                if (data.success) {
                    showToast('Found ' + (data.groups || []).length + ' overlap group(s)', 'success');
                    loadGroups();
                } else {
                    showToast('Error: ' + (data.error || 'unknown'), 'error');
                }
            })
            .catch(function (e) {
                btn.disabled = false;
                btn.textContent = 'Recompute';
                showToast('Network error', 'error');
            });
    }

    // ================================================================
    // GROUP MODAL
    // ================================================================

    function initModal() {
        document.getElementById('btn-modal-cancel').addEventListener('click', closeGroupModal);
        document.getElementById('modal-close').addEventListener('click', closeGroupModal);
        document.getElementById('btn-modal-save').addEventListener('click', saveGroup);

        document.getElementById('group-modal').addEventListener('click', function (e) {
            if (e.target === this) closeGroupModal();
        });
    }

    function openGroupModal() {
        var modal = document.getElementById('group-modal');
        modal.classList.add('visible');

        // Populate existing groups
        var listEl = document.getElementById('modal-groups-list');
        listEl.innerHTML = '';
        groups.forEach(function (g) {
            var item = document.createElement('div');
            item.className = 'group-list-item';
            item.innerHTML =
                '<span class="group-list-name">' + escapeHtml(g.group_name) + '</span>' +
                '<span class="group-list-count">' + (g.camera_ids || []).length + ' cameras</span>' +
                (g.is_auto_computed ? '<span class="group-list-auto">auto</span>' : '') +
                (CAN_WRITE ? '<button class="btn-edit-sm" data-id="' + g.id + '">Edit</button>' +
                             '<button class="btn-delete-sm" data-id="' + g.id + '">Delete</button>' : '');
            listEl.appendChild(item);
        });

        // Edit handlers
        listEl.querySelectorAll('.btn-edit-sm').forEach(function (btn) {
            btn.addEventListener('click', function () {
                editGroup(parseInt(this.dataset.id));
            });
        });

        // Delete handlers
        listEl.querySelectorAll('.btn-delete-sm').forEach(function (btn) {
            btn.addEventListener('click', function () {
                deleteGroup(parseInt(this.dataset.id));
            });
        });

        // Populate camera checklist
        var checklist = document.getElementById('camera-checklist');
        checklist.innerHTML = '';
        allCameras.forEach(function (cam) {
            var item = document.createElement('label');
            item.className = 'camera-check-item';
            item.innerHTML =
                '<input type="checkbox" value="' + escapeHtml(cam.camera_id) + '"> ' +
                escapeHtml(cam.camera_name || cam.camera_id);
            checklist.appendChild(item);
        });

        // Reset form to create mode
        resetGroupForm();
    }

    function resetGroupForm() {
        editingGroupId = null;
        document.getElementById('new-group-name').value = '';
        document.getElementById('new-group-desc').value = '';
        document.querySelectorAll('#camera-checklist input[type="checkbox"]').forEach(function (cb) {
            cb.checked = false;
        });
        document.getElementById('btn-modal-save').textContent = 'Create Group';
    }

    function editGroup(id) {
        var group = groups.find(function (g) { return g.id === id; });
        if (!group) return;

        editingGroupId = id;
        document.getElementById('new-group-name').value = group.group_name || '';
        document.getElementById('new-group-desc').value = group.description || '';
        document.getElementById('btn-modal-save').textContent = 'Update Group';

        // Check the cameras that belong to this group
        var camIds = group.camera_ids || [];
        document.querySelectorAll('#camera-checklist input[type="checkbox"]').forEach(function (cb) {
            cb.checked = camIds.indexOf(cb.value) !== -1;
        });

        // Scroll form into view
        document.getElementById('new-group-name').scrollIntoView({ behavior: 'smooth', block: 'center' });
    }

    function closeGroupModal() {
        document.getElementById('group-modal').classList.remove('visible');
        editingGroupId = null;
    }

    function saveGroup() {
        var name = document.getElementById('new-group-name').value.trim();
        if (!name) { showToast('Group name required', 'error'); return; }

        var checked = [];
        document.querySelectorAll('#camera-checklist input[type="checkbox"]:checked').forEach(function (cb) {
            checked.push(cb.value);
        });
        if (checked.length < 2) { showToast('Select at least 2 cameras', 'error'); return; }

        var payload = {
            group_name: name,
            description: document.getElementById('new-group-desc').value.trim(),
            camera_ids: checked
        };
        if (editingGroupId) {
            payload.id = editingGroupId;
        }

        fetch('/api/camera-sync/groups', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (data.success) {
                showToast(editingGroupId ? 'Group updated' : 'Group created', 'success');
                closeGroupModal();
                loadGroups();
            } else {
                showToast('Error: ' + (data.error || 'unknown'), 'error');
            }
        });
    }

    function deleteGroup(id) {
        if (!confirm('Delete this group?')) return;
        fetch('/api/camera-sync/groups/' + id, { method: 'DELETE' })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data.success) {
                    showToast('Group deleted', 'success');
                    if (currentGroupId === id) selectGroup(null);
                    loadGroups();
                    openGroupModal(); // refresh list
                }
            });
    }

    // ================================================================
    // PTZ TARGETING
    // ================================================================

    function checkPtzTargeting(sourceCameraId, bboxCenterX, bboxCenterY) {
        if (!currentGroupId) return;
        var group = groups.find(function (g) { return g.id === currentGroupId; });
        if (!group) return;

        // Find a PTZ camera in this group that isn't the source
        var ptzCamId = null;
        (group.camera_ids || []).forEach(function (camId) {
            if (camId === sourceCameraId) return;
            var camInfo = allCameras.find(function (c) { return c.camera_id === camId; });
            if (camInfo && camInfo.is_ptz) ptzCamId = camId;
        });

        if (!ptzCamId) return;

        // Show "Target PTZ" button on the source camera cell
        var cell = document.querySelector('.camera-cell[data-camera-id="' + sourceCameraId + '"]');
        if (!cell) return;

        // Remove any existing target button
        var existing = cell.querySelector('.btn-target-ptz');
        if (existing) existing.remove();

        var btn = document.createElement('button');
        btn.className = 'btn-target-ptz';
        btn.textContent = 'Target PTZ \u2192';
        btn.addEventListener('click', function () {
            btn.remove();
            openTargetingPanel(sourceCameraId, ptzCamId, bboxCenterX, bboxCenterY);
        });
        cell.querySelector('.video-wrapper').appendChild(btn);

        // Auto-dismiss after 15 seconds
        setTimeout(function () { if (btn.parentNode) btn.remove(); }, 15000);
    }

    function openTargetingPanel(sourceCameraId, targetCameraId, bboxX, bboxY) {
        // Remove any existing targeting panel
        closeTargetingPanel();

        var panel = document.createElement('div');
        panel.id = 'targeting-panel';
        panel.className = 'targeting-panel';

        var panelHtml =
            '<div class="targeting-header">' +
                '<span class="targeting-title">PTZ Targeting</span>' +
                '<span class="targeting-method" id="targeting-method">Computing...</span>' +
                '<button class="targeting-close" id="targeting-close">\u2715</button>' +
            '</div>' +
            '<div class="targeting-body">' +
                '<div class="targeting-info">' +
                    '<div class="targeting-detail">' +
                        '<span class="targeting-label">Source</span>' +
                        '<span class="targeting-value">' + escapeHtml(sourceCameraId) + '</span>' +
                    '</div>' +
                    '<div class="targeting-detail">' +
                        '<span class="targeting-label">Target PTZ</span>' +
                        '<span class="targeting-value">' + escapeHtml(targetCameraId) + '</span>' +
                    '</div>' +
                    '<div class="targeting-detail">' +
                        '<span class="targeting-label">BBox Center</span>' +
                        '<span class="targeting-value">' + (bboxX * 100).toFixed(1) + '%, ' + (bboxY * 100).toFixed(1) + '%</span>' +
                    '</div>' +
                    '<div class="targeting-detail" id="targeting-pan-tilt">' +
                        '<span class="targeting-label">Pan / Tilt</span>' +
                        '<span class="targeting-value">...</span>' +
                    '</div>' +
                '</div>' +
                '<div class="targeting-actions">' +
                    '<span class="targeting-hint">Fine-tune with PTZ pad, then confirm or discard</span>' +
                    '<button class="btn-toolbar primary" id="btn-confirm-match">Confirm Match</button>' +
                    '<button class="btn-toolbar" id="btn-discard-match" style="border-color:#e57373;color:#e57373;">Not a Match</button>' +
                '</div>' +
            '</div>';
        panel.innerHTML = panelHtml;

        // Insert between camera grid and selections panel
        var grid = document.getElementById('camera-grid');
        grid.parentNode.insertBefore(panel, grid.nextSibling);

        document.getElementById('targeting-close').addEventListener('click', closeTargetingPanel);
        document.getElementById('btn-discard-match').addEventListener('click', closeTargetingPanel);
        document.getElementById('btn-confirm-match').addEventListener('click', function () {
            openConfirmDialog();
        });

        // Send targeting request
        ptzTargetState = {
            sourceCameraId: sourceCameraId,
            targetCameraId: targetCameraId,
            bboxX: bboxX,
            bboxY: bboxY,
            estimatedPan: null,
            estimatedTilt: null,
            method: 'geometry'
        };

        fetch('/api/camera-sync/ptz/target', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                source_camera_id: sourceCameraId,
                target_camera_id: targetCameraId,
                bbox_x: bboxX,
                bbox_y: bboxY
            })
        })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (data.success) {
                var methodEl = document.getElementById('targeting-method');
                var ptEl = document.getElementById('targeting-pan-tilt');
                if (methodEl) {
                    var methodText = data.method === 'calibrated'
                        ? 'Calibrated (' + data.calibration_points + ' pts)'
                        : 'Geometry estimate';
                    methodEl.textContent = methodText;
                    methodEl.className = 'targeting-method ' + data.method;
                }
                if (ptEl) {
                    ptEl.querySelector('.targeting-value').textContent =
                        data.estimated_pan.toFixed(3) + ' / ' + data.estimated_tilt.toFixed(3);
                }
                ptzTargetState.estimatedPan = data.geo_pan;
                ptzTargetState.estimatedTilt = data.geo_tilt;
                ptzTargetState.method = data.method;
                if (data.move_error) {
                    showToast('PTZ move failed: ' + data.move_error, 'error');
                }
            } else {
                showToast('Targeting failed: ' + (data.error || 'unknown'), 'error');
                closeTargetingPanel();
            }
        })
        .catch(function (e) {
            showToast('Targeting network error', 'error');
            closeTargetingPanel();
        });
    }

    function closeTargetingPanel() {
        var panel = document.getElementById('targeting-panel');
        if (panel) panel.remove();
        ptzTargetState = null;
        // Remove any confirm dialog too
        var dialog = document.getElementById('confirm-dialog-overlay');
        if (dialog) dialog.remove();
    }

    function openConfirmDialog() {
        if (!ptzTargetState) return;

        // Remove existing dialog
        var existing = document.getElementById('confirm-dialog-overlay');
        if (existing) existing.remove();

        var overlay = document.createElement('div');
        overlay.id = 'confirm-dialog-overlay';
        overlay.className = 'modal-overlay visible';

        var dialogHtml =
            '<div class="modal-content" style="max-width:400px;">' +
                '<div class="modal-header">' +
                    '<span class="modal-title">Confirm Reference Point</span>' +
                    '<button class="modal-close" id="confirm-dialog-close">\u2715</button>' +
                '</div>' +
                '<div class="modal-body">' +
                    '<div class="form-field">' +
                        '<label>What is this landmark?</label>' +
                        '<input type="text" id="cal-label-input" placeholder="e.g. tall pine tree, white fence post, dock piling" autocomplete="off">' +
                    '</div>' +
                    '<p style="font-size:12px; color:#888; margin-top:8px;">This saves the current PTZ position as a calibration reference point. Over time, this improves targeting accuracy.</p>' +
                '</div>' +
                '<div class="modal-footer">' +
                    '<button class="btn-toolbar" id="btn-cal-cancel">Cancel</button>' +
                    '<button class="btn-toolbar primary" id="btn-cal-save">Save Reference Point</button>' +
                '</div>' +
            '</div>';
        overlay.innerHTML = dialogHtml;

        document.body.appendChild(overlay);

        document.getElementById('confirm-dialog-close').addEventListener('click', function () {
            overlay.remove();
        });
        document.getElementById('btn-cal-cancel').addEventListener('click', function () {
            overlay.remove();
        });
        overlay.addEventListener('click', function (e) {
            if (e.target === overlay) overlay.remove();
        });

        document.getElementById('btn-cal-save').addEventListener('click', function () {
            var label = document.getElementById('cal-label-input').value.trim();
            saveCalibrationPoint(label);
            overlay.remove();
        });

        // Focus input
        setTimeout(function () {
            document.getElementById('cal-label-input').focus();
        }, 100);
    }

    function saveCalibrationPoint(label) {
        if (!ptzTargetState) return;

        fetch('/api/camera-sync/ptz/calibrate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                source_camera_id: ptzTargetState.sourceCameraId,
                target_camera_id: ptzTargetState.targetCameraId,
                source_bbox_x: ptzTargetState.bboxX,
                source_bbox_y: ptzTargetState.bboxY,
                estimated_pan: ptzTargetState.estimatedPan,
                estimated_tilt: ptzTargetState.estimatedTilt,
                label: label
            })
        })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (data.success) {
                showToast('Reference point saved', 'success');
                // Refresh calibration badge
                loadCalibrationBadge(ptzTargetState.sourceCameraId, ptzTargetState.targetCameraId);
                closeTargetingPanel();
            } else {
                showToast('Failed to save: ' + (data.error || 'unknown'), 'error');
            }
        })
        .catch(function (e) {
            showToast('Network error saving calibration', 'error');
        });
    }

    // ================================================================
    // CALIBRATION BADGE
    // ================================================================

    function loadCalibrationBadge(sourceCameraId, targetCameraId) {
        fetch('/api/camera-sync/ptz/calibration?source_camera_id=' +
              encodeURIComponent(sourceCameraId) + '&target_camera_id=' +
              encodeURIComponent(targetCameraId))
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (!data.success) return;
            updateCalBadge(targetCameraId, data.count);
        });
    }

    function updateCalBadge(cameraId, count) {
        var badge = document.getElementById('cal-badge-' + cameraId);
        if (!badge) return;

        badge.classList.remove('loading', 'uncalibrated', 'basic', 'calibrated');
        if (count < 3) {
            badge.className = 'cal-badge uncalibrated';
            badge.textContent = 'Cal: ' + count + ' (Uncalibrated)';
        } else if (count < 6) {
            badge.className = 'cal-badge basic';
            badge.textContent = 'Cal: ' + count + ' (Basic)';
        } else {
            badge.className = 'cal-badge calibrated';
            badge.textContent = 'Cal: ' + count + ' (Calibrated)';
        }
    }

    function loadAllCalibrationBadges() {
        if (!currentGroupId) return;
        var group = groups.find(function (g) { return g.id === currentGroupId; });
        if (!group) return;

        var cameraIds = group.camera_ids || [];
        var ptzCams = [];
        var nonPtzCams = [];

        cameraIds.forEach(function (camId) {
            var camInfo = allCameras.find(function (c) { return c.camera_id === camId; });
            if (camInfo && camInfo.is_ptz) {
                ptzCams.push(camId);
            } else {
                nonPtzCams.push(camId);
            }
        });

        // For each PTZ cam, load calibration count from each non-PTZ source
        ptzCams.forEach(function (ptzId) {
            var totalCount = 0;
            var pending = nonPtzCams.length;
            if (pending === 0) {
                updateCalBadge(ptzId, 0);
                return;
            }
            nonPtzCams.forEach(function (srcId) {
                fetch('/api/camera-sync/ptz/calibration?source_camera_id=' +
                      encodeURIComponent(srcId) + '&target_camera_id=' +
                      encodeURIComponent(ptzId))
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    if (data.success) totalCount += data.count;
                    pending--;
                    if (pending === 0) updateCalBadge(ptzId, totalCount);
                })
                .catch(function () {
                    pending--;
                    if (pending === 0) updateCalBadge(ptzId, totalCount);
                });
            });
        });
    }

    // ================================================================
    // CALIBRATION VIEWER MODAL
    // ================================================================

    function openCalibrationModal() {
        if (!currentGroupId) {
            showToast('Select a group first', 'error');
            return;
        }

        var group = groups.find(function (g) { return g.id === currentGroupId; });
        if (!group) return;

        // Find camera pairs (non-PTZ to PTZ)
        var cameraIds = group.camera_ids || [];
        var ptzCams = [];
        var nonPtzCams = [];
        cameraIds.forEach(function (camId) {
            var camInfo = allCameras.find(function (c) { return c.camera_id === camId; });
            if (camInfo && camInfo.is_ptz) ptzCams.push(camId);
            else nonPtzCams.push(camId);
        });

        if (ptzCams.length === 0) {
            showToast('No PTZ cameras in this group', 'error');
            return;
        }

        // Remove existing modal
        var existingModal = document.getElementById('calibration-modal-overlay');
        if (existingModal) existingModal.remove();

        var overlay = document.createElement('div');
        overlay.id = 'calibration-modal-overlay';
        overlay.className = 'modal-overlay visible';
        var modalHtml =
            '<div class="modal-content" style="max-width:700px;">' +
                '<div class="modal-header">' +
                    '<span class="modal-title">Calibration</span>' +
                    '<button class="modal-close" id="cal-modal-close">\u2715</button>' +
                '</div>' +
                '<div class="modal-body" id="cal-modal-body">' +
                    '<div class="cal-section">' +
                        '<h4 style="color:#aaa; font-size:13px; margin:0 0 8px;">Movement Calibration</h4>' +
                        '<p style="color:#666; font-size:12px; margin:0 0 12px;">Run auto-calibration to measure how the PTZ responds to movement commands. This takes ~20 seconds and moves the camera through test patterns.</p>' +
                        '<div id="cal-auto-section"></div>' +
                    '</div>' +
                    '<hr style="border-color:#2e2e2e; margin:16px 0;">' +
                    '<div class="cal-section">' +
                        '<h4 style="color:#aaa; font-size:13px; margin:0 0 8px;">Reference Points</h4>' +
                        '<div id="cal-points-section"><p style="color:#888; font-size:13px;">Loading...</p></div>' +
                    '</div>' +
                '</div>' +
            '</div>';
        overlay.innerHTML = modalHtml;

        document.body.appendChild(overlay);

        document.getElementById('cal-modal-close').addEventListener('click', function () {
            overlay.remove();
        });
        overlay.addEventListener('click', function (e) {
            if (e.target === overlay) overlay.remove();
        });

        // Populate auto-calibrate section
        var autoSection = document.getElementById('cal-auto-section');
        ptzCams.forEach(function (ptzId) {
            var row = document.createElement('div');
            row.style.cssText = 'display:flex; align-items:center; gap:10px; margin-bottom:8px;';
            var label = document.createElement('span');
            label.style.cssText = 'color:#ccc; font-size:13px; flex:1;';
            label.textContent = ptzId;
            var statusSpan = document.createElement('span');
            statusSpan.id = 'cal-auto-status-' + ptzId;
            statusSpan.style.cssText = 'color:#666; font-size:12px;';
            statusSpan.textContent = 'Not calibrated';
            var btn = document.createElement('button');
            btn.className = 'btn-toolbar primary';
            btn.style.cssText = 'font-size:12px; padding:4px 12px;';
            btn.textContent = 'Speed Test';
            btn.addEventListener('click', function () {
                runAutoCalibrate(ptzId, btn, statusSpan);
            });
            var visualBtn = document.createElement('button');
            visualBtn.className = 'btn-toolbar';
            visualBtn.style.cssText = 'font-size:12px; padding:4px 12px;';
            visualBtn.textContent = 'Visual Calibrate';
            visualBtn.addEventListener('click', function () {
                runVisualCalibrate(ptzId, visualBtn, statusSpan);
            });
            var zoomBtn = document.createElement('button');
            zoomBtn.className = 'btn-toolbar';
            zoomBtn.style.cssText = 'font-size:12px; padding:4px 12px;';
            zoomBtn.textContent = 'Zoom Calibrate';
            zoomBtn.addEventListener('click', function () {
                runZoomCalibrate(ptzId, zoomBtn, statusSpan);
            });
            row.appendChild(label);
            row.appendChild(statusSpan);
            row.appendChild(btn);
            row.appendChild(zoomBtn);
            row.appendChild(visualBtn);
            autoSection.appendChild(row);
        });

        // Load calibration reference points
        var pointsSection = document.getElementById('cal-points-section');
        var pairs = [];
        nonPtzCams.forEach(function (src) {
            ptzCams.forEach(function (tgt) {
                pairs.push({ source: src, target: tgt });
            });
        });

        if (pairs.length === 0) {
            pointsSection.innerHTML = '<p style="color:#666; font-size:12px;">No source-to-PTZ pairs in this group.</p>';
            return;
        }

        pointsSection.textContent = '';
        pairs.forEach(function (pair) {
            var section = document.createElement('div');
            section.className = 'cal-pair-section';

            var title = document.createElement('h4');
            title.className = 'cal-pair-title';
            title.textContent = pair.source + ' \u2192 ' + pair.target;
            section.appendChild(title);

            var pairBody = document.createElement('div');
            pairBody.className = 'cal-pair-body';
            pairBody.id = 'cal-pair-' + pair.source + '-' + pair.target;
            pairBody.textContent = 'Loading...';
            section.appendChild(pairBody);

            pointsSection.appendChild(section);

            fetch('/api/camera-sync/ptz/calibration?source_camera_id=' +
                  encodeURIComponent(pair.source) + '&target_camera_id=' +
                  encodeURIComponent(pair.target))
            .then(function (r) { return r.json(); })
            .then(function (data) {
                var container = document.getElementById('cal-pair-' + pair.source + '-' + pair.target);
                if (!container) return;

                if (!data.success || data.count === 0) {
                    container.textContent = 'No calibration points yet.';
                    container.style.cssText = 'color:#666; font-size:12px; margin:4px 0;';
                    return;
                }

                // Build summary
                var summary = document.createElement('div');
                summary.className = 'cal-summary';
                var s1 = document.createElement('span');
                s1.textContent = data.count + ' points';
                var s2 = document.createElement('span');
                s2.textContent = 'Avg error: pan=' + data.avg_error_pan.toFixed(4) + ', tilt=' + data.avg_error_tilt.toFixed(4);
                var s3 = document.createElement('span');
                s3.textContent = 'Coverage: ' + (data.coverage * 100).toFixed(0) + '%';
                summary.appendChild(s1);
                summary.appendChild(s2);
                summary.appendChild(s3);

                // Build table
                var table = document.createElement('table');
                table.className = 'cal-table';
                var thead = document.createElement('thead');
                var headRow = document.createElement('tr');
                ['Label', 'Position', 'Est. Pan', 'Actual Pan', 'Error', 'Time', ''].forEach(function (h) {
                    var th = document.createElement('th');
                    th.textContent = h;
                    headRow.appendChild(th);
                });
                thead.appendChild(headRow);
                table.appendChild(thead);

                var tbody = document.createElement('tbody');
                data.points.forEach(function (pt) {
                    var tr = document.createElement('tr');

                    var td1 = document.createElement('td');
                    td1.textContent = pt.label || '\u2014';
                    tr.appendChild(td1);

                    var td2 = document.createElement('td');
                    td2.textContent = (pt.source_bbox_x * 100).toFixed(1) + '%, ' + (pt.source_bbox_y * 100).toFixed(1) + '%';
                    tr.appendChild(td2);

                    var td3 = document.createElement('td');
                    td3.textContent = pt.estimated_pan != null ? pt.estimated_pan.toFixed(3) : '\u2014';
                    tr.appendChild(td3);

                    var td4 = document.createElement('td');
                    td4.textContent = pt.actual_pan.toFixed(3);
                    tr.appendChild(td4);

                    var td5 = document.createElement('td');
                    if (Math.abs(pt.error_pan || 0) > 0.1) td5.className = 'cal-error-high';
                    td5.textContent = pt.error_pan != null ? pt.error_pan.toFixed(4) : '\u2014';
                    tr.appendChild(td5);

                    var td6 = document.createElement('td');
                    td6.textContent = formatTime(pt.created_date);
                    tr.appendChild(td6);

                    var td7 = document.createElement('td');
                    if (CAN_WRITE) {
                        var delBtn = document.createElement('button');
                        delBtn.className = 'btn-delete-sm btn-cal-delete';
                        delBtn.dataset.id = pt.id;
                        delBtn.textContent = 'Delete';
                        delBtn.addEventListener('click', function () {
                            var ptId = parseInt(this.dataset.id);
                            deleteCalibrationPoint(ptId, pair.source, pair.target);
                        });
                        td7.appendChild(delBtn);
                    }
                    tr.appendChild(td7);

                    tbody.appendChild(tr);
                });
                table.appendChild(tbody);

                container.textContent = '';
                container.appendChild(summary);
                container.appendChild(table);
            });
        });
    }

    function runAutoCalibrate(cameraId, btn, statusSpan) {
        btn.disabled = true;
        btn.textContent = 'Calibrating...';
        statusSpan.style.color = '#FFC107';
        statusSpan.textContent = 'Running tests (~20s)...';

        fetch('/api/camera-sync/ptz/auto-calibrate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ camera_id: cameraId })
        })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            btn.disabled = false;
            btn.textContent = 'Run Auto-Calibrate';

            if (data.success) {
                var ms = data.max_speeds || {};
                statusSpan.style.color = '#4CAF50';
                statusSpan.textContent = 'Complete';

                var detailsId = 'cal-auto-details-' + cameraId;
                var existing = document.getElementById(detailsId);
                if (existing) existing.remove();

                var details = document.createElement('div');
                details.id = detailsId;
                details.style.cssText = 'margin:4px 0 12px 0; padding:8px; background:#1a1a1a; border-radius:4px; font-size:11px; color:#888; max-height:250px; overflow-y:auto;';

                var lines = [];
                // Ranges
                var r = data.ranges || {};
                if (r.pan) lines.push('Pan range: ' + r.pan.min + ' to ' + r.pan.max + ' (' + (r.pan.max - r.pan.min).toFixed(3) + ' total)');
                if (r.tilt) lines.push('Tilt range: ' + r.tilt.min + ' to ' + r.tilt.max + ' (' + (r.tilt.max - r.tilt.min).toFixed(3) + ' total)');
                if (r.zoom) lines.push('Zoom range: ' + r.zoom.min + ' to ' + r.zoom.max);
                lines.push('');

                // Max speeds
                ['pan', 'tilt', 'zoom'].forEach(function (axis) {
                    var m = ms[axis];
                    if (m) lines.push(axis.toUpperCase() + ' max: ' + m.max_units_per_sec.toFixed(4) + '/s @speed=' + m.max_speed_setting +
                        (m['units_per_sec_at_0.5'] ? ', @0.5=' + m['units_per_sec_at_0.5'].toFixed(4) + '/s' : ''));
                });
                lines.push('');

                // Speed test details
                lines.push('--- Speed tests ---');
                (data.speed_tests || []).forEach(function (t) {
                    lines.push(t.axis + ' ' + t.direction + ' @' + t.speed + ': ' + t.units_per_sec.toFixed(4) + '/s (d=' + t.displacement.toFixed(4) + ')');
                });

                details.innerHTML = lines.join('<br>');
                btn.parentNode.parentNode.appendChild(details);

                showToast('Auto-calibration complete for ' + cameraId, 'success');
            } else {
                statusSpan.style.color = '#F44336';
                statusSpan.textContent = 'Failed: ' + (data.error || 'unknown');
                showToast('Auto-calibration failed', 'error');
            }
        })
        .catch(function (e) {
            btn.disabled = false;
            btn.textContent = 'Run Auto-Calibrate';
            statusSpan.style.color = '#F44336';
            statusSpan.textContent = 'Network error';
            showToast('Auto-calibration network error', 'error');
        });
    }

    function runVisualCalibrate(cameraId, btn, statusSpan) {
        btn.disabled = true;
        btn.textContent = 'Running...';
        statusSpan.style.color = '#FFC107';
        statusSpan.textContent = 'Visual calibration (~2 min)...';

        fetch('/api/camera-sync/ptz/calibrate-visual', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ camera_id: cameraId, num_positions: 9, centering_attempts: 3 })
        })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            btn.disabled = false;
            btn.textContent = 'Visual Calibrate';

            if (data.success) {
                var s = data.summary || {};
                statusSpan.style.color = '#4CAF50';
                statusSpan.textContent = s.successful_centerings + '/' + data.positions_tested + ' centered, ' +
                    s.roundtrips_confirmed + '/' + s.roundtrips_tested + ' return confirmed, ' +
                    'drift=' + (s.avg_position_drift || 0).toFixed(5);

                // Show per-position details
                var detailsId = 'cal-visual-details-' + cameraId;
                var existing = document.getElementById(detailsId);
                if (existing) existing.remove();

                var details = document.createElement('div');
                details.id = detailsId;
                details.style.cssText = 'margin:4px 0 12px 0; padding:8px; background:#1a1a1a; border-radius:4px; font-size:11px; color:#888; max-height:200px; overflow-y:auto;';

                var rows = (data.results || []).map(function (r) {
                    var line = 'Grid ' + r.grid_index + ' [' + r.target_pan + ',' + r.target_tilt + ']: ' + r.status;
                    if (r.centering_error !== null) line += ' err=' + r.centering_error.toFixed(4);
                    if (r.match_confidence !== null) line += ' conf=' + r.match_confidence.toFixed(2);
                    if (r.roundtrip) {
                        line += ' | return: drift=' + r.roundtrip.position_drift.toFixed(5);
                        if (r.roundtrip.visual_return_error !== null) {
                            line += ' visual_err=' + r.roundtrip.visual_return_error.toFixed(4);
                        }
                        line += r.roundtrip.visual_confirmed ? ' [OK]' : ' [MISS]';
                    }
                    return line;
                });
                details.innerHTML = rows.join('<br>');
                btn.parentNode.parentNode.appendChild(details);

                showToast('Visual calibration: ' + s.successful_centerings + ' positions centered, ' +
                    s.roundtrips_confirmed + ' return trips confirmed', 'success');
            } else {
                statusSpan.style.color = '#F44336';
                statusSpan.textContent = 'Failed: ' + (data.error || 'unknown');
                showToast('Visual calibration failed', 'error');
            }
        })
        .catch(function (e) {
            btn.disabled = false;
            btn.textContent = 'Visual Calibrate';
            statusSpan.style.color = '#F44336';
            statusSpan.textContent = 'Network error';
            showToast('Visual calibration network error', 'error');
        });
    }

    function runZoomCalibrate(cameraId, btn, statusSpan) {
        btn.disabled = true;
        btn.textContent = 'Running...';
        statusSpan.style.color = '#FFC107';
        statusSpan.textContent = 'Zoom calibration (~45s)...';

        fetch('/api/camera-sync/ptz/calibrate-zoom', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ camera_id: cameraId })
        })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            btn.disabled = false;
            btn.textContent = 'Zoom Calibrate';

            if (data.success) {
                var s = data.summary || {};
                var range = data.zoom_range || {};
                statusSpan.style.color = '#4CAF50';
                statusSpan.textContent = 'Zoom: ' + (range.min || 0).toFixed(2) + '-' +
                    (range.max || 0).toFixed(2) + ', speed@0.5=' +
                    (s['zoom_speed_at_0.5'] || 0).toFixed(4) + '/s';

                var detailsId = 'cal-zoom-details-' + cameraId;
                var existing = document.getElementById(detailsId);
                if (existing) existing.remove();

                var details = document.createElement('div');
                details.id = detailsId;
                details.style.cssText = 'margin:4px 0 12px 0; padding:8px; background:#1a1a1a; border-radius:4px; font-size:11px; color:#888; max-height:200px; overflow-y:auto;';

                var lines = [];
                lines.push('Range: ' + (range.min || 0).toFixed(4) + ' to ' + (range.max || 0).toFixed(4));
                (data.speed_tests || []).forEach(function (t) {
                    lines.push('Speed ' + t.speed + ': ' + t.units_per_sec.toFixed(4) + '/s (displacement=' + t.displacement.toFixed(4) + ')');
                });
                lines.push('--- FOV samples ---');
                (data.fov_samples || []).forEach(function (f) {
                    var line = 'zoom=' + f.target_zoom.toFixed(1) + ' actual=' + f.actual_zoom.toFixed(3);
                    if (f.fov_ratio !== null) line += ' fov_ratio=' + f.fov_ratio.toFixed(3);
                    line += ' features=' + f.feature_count;
                    if (f.avg_brightness !== null) line += ' bright=' + f.avg_brightness;
                    lines.push(line);
                });
                details.innerHTML = lines.join('<br>');
                btn.parentNode.parentNode.appendChild(details);

                showToast('Zoom calibration complete for ' + cameraId, 'success');
            } else {
                statusSpan.style.color = '#F44336';
                statusSpan.textContent = 'Failed: ' + (data.error || 'unknown');
                showToast('Zoom calibration failed', 'error');
            }
        })
        .catch(function (e) {
            btn.disabled = false;
            btn.textContent = 'Zoom Calibrate';
            statusSpan.style.color = '#F44336';
            statusSpan.textContent = 'Network error';
            showToast('Zoom calibration network error', 'error');
        });
    }

    function deleteCalibrationPoint(pointId, sourceCameraId, targetCameraId) {
        if (!confirm('Delete this calibration point?')) return;
        fetch('/api/camera-sync/ptz/calibration/' + pointId, { method: 'DELETE' })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (data.success) {
                showToast('Calibration point deleted', 'success');
                openCalibrationModal(); // refresh
                loadAllCalibrationBadges();
            } else {
                showToast('Delete failed: ' + (data.error || 'unknown'), 'error');
            }
        });
    }

    // ================================================================
    // HELPERS
    // ================================================================

    function showToast(msg, type) {
        var toast = document.getElementById('toast');
        toast.textContent = msg;
        toast.className = 'toast visible ' + (type || '');
        setTimeout(function () { toast.className = 'toast'; }, 3000);
    }

    function escapeHtml(str) {
        var div = document.createElement('div');
        div.textContent = str || '';
        return div.innerHTML;
    }

    function formatTime(dateStr) {
        if (!dateStr) return '';
        var d = new Date(dateStr);
        return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }

    // ================================================================
    // PTZ HUD — Bearing Bar + Readout
    // ================================================================

    var CARDINAL = [
        { deg: 0,   label: 'N'  }, { deg: 45,  label: 'NE' },
        { deg: 90,  label: 'E'  }, { deg: 135, label: 'SE' },
        { deg: 180, label: 'S'  }, { deg: 225, label: 'SW' },
        { deg: 270, label: 'W'  }, { deg: 315, label: 'NW' }
    ];

    // Per-camera HUD state: { homeBearing, panRange, isCalibrated }
    var _ptzHudState = {};

    function startPtzHudPolling(cameraId, camInfo) {
        if (_ptzHudTimers[cameraId]) return;
        var homeBearing = (camInfo && camInfo.ptz_home_bearing != null) ? parseFloat(camInfo.ptz_home_bearing) : null;
        var panRange = (camInfo && camInfo.ptz_pan_range) ? parseFloat(camInfo.ptz_pan_range) : 360;
        var isCalibrated = (homeBearing !== null && !isNaN(homeBearing));

        var travelLimits = (camInfo && camInfo.ptz_travel_limits) ? camInfo.ptz_travel_limits : null;
        _ptzHudState[cameraId] = { homeBearing: homeBearing, panRange: panRange, isCalibrated: isCalibrated, limits: travelLimits, lastPan: null, lastTilt: null, lastZoom: null };

        // Size the canvas once wrapper is laid out
        setTimeout(function () {
            var bar = document.getElementById('bearing-bar-' + cameraId);
            if (bar) {
                var wrapper = bar.closest('.video-wrapper');
                if (wrapper) {
                    bar.width = wrapper.offsetWidth;
                }
            }
        }, 200);

        // Initial fetch + poll every 2s
        fetchAndUpdateHud(cameraId);
        _ptzHudTimers[cameraId] = setInterval(function () {
            fetchAndUpdateHud(cameraId);
        }, 2000);
    }

    function openCompassCalibrationDialog(cameraId) {
        var st = _ptzHudState[cameraId];
        var isCalibrated = st && st.isCalibrated;

        // Build a simple modal overlay
        var overlay = document.createElement('div');
        overlay.className = 'modal-overlay visible';
        overlay.style.zIndex = '1100';

        var modal = document.createElement('div');
        modal.className = 'modal-content';
        modal.style.maxWidth = '400px';

        // Header
        var header = document.createElement('div');
        header.className = 'modal-header';
        var title = document.createElement('span');
        title.className = 'modal-title';
        title.textContent = 'Compass Calibration';
        header.appendChild(title);
        var closeBtn = document.createElement('button');
        closeBtn.className = 'modal-close';
        closeBtn.textContent = '\u00D7';
        closeBtn.addEventListener('click', function () { overlay.remove(); });
        header.appendChild(closeBtn);
        modal.appendChild(header);

        // Body
        var body = document.createElement('div');
        body.className = 'modal-body';

        var desc = document.createElement('p');
        desc.style.cssText = 'font-size:13px;color:#aaa;margin:0 0 16px';
        desc.textContent = 'Point the camera at a known target, then enter the compass bearing (0\u00B0\u2013360\u00B0) from the camera to that target.';
        body.appendChild(desc);

        var field = document.createElement('div');
        field.className = 'form-field';
        var label = document.createElement('label');
        label.textContent = 'Bearing to target';
        field.appendChild(label);
        var input = document.createElement('input');
        input.type = 'number';
        input.min = '0';
        input.max = '360';
        input.step = '0.1';
        input.placeholder = 'e.g. 135';
        input.style.cssText = 'width:100%;padding:8px 12px;background:#111;border:1px solid #333;border-radius:5px;color:#ddd;font-size:14px;outline:none;';
        field.appendChild(input);
        body.appendChild(field);

        if (isCalibrated) {
            var currentInfo = document.createElement('p');
            currentInfo.style.cssText = 'font-size:12px;color:#666;margin:12px 0 0';
            currentInfo.textContent = 'Currently calibrated: home bearing = ' + st.homeBearing.toFixed(1) + '\u00B0';
            body.appendChild(currentInfo);
        }

        modal.appendChild(body);

        // Footer
        var footer = document.createElement('div');
        footer.className = 'modal-footer';

        if (isCalibrated) {
            var clearBtn = document.createElement('button');
            clearBtn.className = 'btn-toolbar';
            clearBtn.textContent = 'Clear';
            clearBtn.style.marginRight = 'auto';
            clearBtn.addEventListener('click', function () {
                fetch('/api/camera-sync/ptz/compass-calibrate?camera_id=' + encodeURIComponent(cameraId), {
                    method: 'DELETE'
                }).then(function (r) { return r.json(); }).then(function (data) {
                    if (data.success) {
                        _ptzHudState[cameraId].homeBearing = null;
                        _ptzHudState[cameraId].isCalibrated = false;
                        showToast('Compass calibration cleared', 'success');
                        overlay.remove();
                    } else {
                        showToast('Error: ' + (data.error || 'unknown'), 'error');
                    }
                });
            });
            footer.appendChild(clearBtn);
        }

        var cancelBtn = document.createElement('button');
        cancelBtn.className = 'btn-toolbar';
        cancelBtn.textContent = 'Cancel';
        cancelBtn.addEventListener('click', function () { overlay.remove(); });
        footer.appendChild(cancelBtn);

        var saveBtn = document.createElement('button');
        saveBtn.className = 'btn-toolbar primary';
        saveBtn.textContent = 'Calibrate';
        saveBtn.addEventListener('click', function () {
            var val = parseFloat(input.value);
            if (isNaN(val) || val < 0 || val > 360) {
                input.style.borderColor = '#f44336';
                return;
            }
            saveBtn.disabled = true;
            saveBtn.textContent = 'Calibrating...';
            fetch('/api/camera-sync/ptz/compass-calibrate', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ camera_id: cameraId, target_bearing: val })
            }).then(function (r) { return r.json(); }).then(function (data) {
                if (data.success) {
                    _ptzHudState[cameraId].homeBearing = data.home_bearing;
                    _ptzHudState[cameraId].isCalibrated = true;
                    showToast('Compass calibrated: home bearing = ' + data.home_bearing + '\u00B0', 'success');
                    overlay.remove();
                } else {
                    showToast('Error: ' + (data.error || 'unknown'), 'error');
                    saveBtn.disabled = false;
                    saveBtn.textContent = 'Calibrate';
                }
            }).catch(function (e) {
                showToast('Network error', 'error');
                saveBtn.disabled = false;
                saveBtn.textContent = 'Calibrate';
            });
        });
        footer.appendChild(saveBtn);
        modal.appendChild(footer);

        overlay.appendChild(modal);
        overlay.addEventListener('click', function (e) {
            if (e.target === overlay) overlay.remove();
        });
        document.body.appendChild(overlay);
        input.focus();
    }

    function stopAllPtzHudPolling() {
        Object.keys(_ptzHudTimers).forEach(function (camId) {
            clearInterval(_ptzHudTimers[camId]);
        });
        _ptzHudTimers = {};
    }

    function fetchAndUpdateHud(cameraId) {
        var st = _ptzHudState[cameraId];
        if (!st) return;
        fetch('/api/camera-sync/ptz/position?camera_id=' + encodeURIComponent(cameraId))
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!data.success) return;
                updatePtzHud(cameraId, data.pan, data.tilt, data.zoom, st.homeBearing, st.panRange, st.isCalibrated);
            })
            .catch(function () { /* silent */ });
    }

    function buildReadoutItem(label, value) {
        var span = document.createElement('span');
        span.className = 'ptz-readout-item';
        span.appendChild(document.createTextNode(label + ' '));
        var b = document.createElement('b');
        b.textContent = value;
        span.appendChild(b);
        return span;
    }

    function updatePtzHud(cameraId, pan, tilt, zoom, bearing, panRange, isCalibrated) {
        // Store last known position for limit detection
        var st = _ptzHudState[cameraId];
        if (st) { st.lastPan = pan; st.lastTilt = tilt; st.lastZoom = zoom; }

        // Update readout
        var readout = document.getElementById('ptz-readout-' + cameraId);
        if (readout) {
            readout.textContent = '';
            if (isCalibrated) {
                var heading = ((bearing + pan * (panRange / 2)) % 360 + 360) % 360;
                var cardinalLabel = nearestCardinal(heading);
                readout.appendChild(buildReadoutItem('HDG', Math.round(heading) + '\u00B0 ' + cardinalLabel));
                readout.appendChild(buildReadoutItem('TILT', (tilt >= 0 ? '+' : '') + (tilt * 45).toFixed(1) + '\u00B0'));
                readout.appendChild(buildReadoutItem('ZOOM', zoom.toFixed(2)));
            } else {
                readout.appendChild(buildReadoutItem('P', pan.toFixed(3)));
                readout.appendChild(buildReadoutItem('T', tilt.toFixed(3)));
                readout.appendChild(buildReadoutItem('Z', zoom.toFixed(3)));
            }
        }

        // Update bearing bar
        var bar = document.getElementById('bearing-bar-' + cameraId);
        if (bar) {
            if (isCalibrated) {
                var heading = ((bearing + pan * (panRange / 2)) % 360 + 360) % 360;
                renderBearingBarCalibrated(bar, heading);
            } else {
                renderBearingBarRaw(bar, pan);
            }
        }
    }

    function nearestCardinal(deg) {
        var best = '';
        var bestDist = 999;
        for (var i = 0; i < CARDINAL.length; i++) {
            var d = Math.abs(deg - CARDINAL[i].deg);
            if (d > 180) d = 360 - d;
            if (d < bestDist) { bestDist = d; best = CARDINAL[i].label; }
        }
        return best;
    }

    function renderBearingBarCalibrated(canvas, headingDeg) {
        var ctx = canvas.getContext('2d');
        var w = canvas.width;
        var h = canvas.height;
        if (w === 0) return;
        ctx.clearRect(0, 0, w, h);

        // Background
        ctx.fillStyle = 'rgba(0, 0, 0, 0.55)';
        ctx.fillRect(0, 0, w, h);

        var spread = 120; // degrees visible across bar width
        var pixPerDeg = w / spread;
        var startDeg = headingDeg - spread / 2;
        var endDeg = headingDeg + spread / 2;

        // Draw ticks
        for (var deg = Math.floor(startDeg / 5) * 5; deg <= endDeg; deg += 5) {
            var normDeg = ((deg % 360) + 360) % 360;
            var x = (deg - startDeg) * pixPerDeg;

            var isCardinal = (normDeg % 90 === 0);
            var isIntercardinal = (!isCardinal && normDeg % 45 === 0);
            var is30 = (!isCardinal && !isIntercardinal && normDeg % 30 === 0);
            var is10 = (normDeg % 10 === 0);

            if (isCardinal) {
                ctx.strokeStyle = '#fff';
                ctx.lineWidth = 2;
                ctx.beginPath();
                ctx.moveTo(x, h);
                ctx.lineTo(x, h * 0.38);
                ctx.stroke();
                var cLabels = { 0: 'N', 90: 'E', 180: 'S', 270: 'W' };
                ctx.fillStyle = normDeg === 0 ? '#f44336' : '#fff';
                ctx.font = 'bold 11px -apple-system, sans-serif';
                ctx.textAlign = 'center';
                ctx.fillText(cLabels[normDeg], x, h * 0.28);
            } else if (isIntercardinal) {
                ctx.strokeStyle = '#bbb';
                ctx.lineWidth = 1.5;
                ctx.beginPath();
                ctx.moveTo(x, h);
                ctx.lineTo(x, h * 0.45);
                ctx.stroke();
                var icLabels = { 45: 'NE', 135: 'SE', 225: 'SW', 315: 'NW' };
                ctx.fillStyle = '#aaa';
                ctx.font = '10px -apple-system, sans-serif';
                ctx.textAlign = 'center';
                ctx.fillText(icLabels[normDeg], x, h * 0.32);
            } else if (is30) {
                ctx.strokeStyle = '#888';
                ctx.lineWidth = 1;
                ctx.beginPath();
                ctx.moveTo(x, h);
                ctx.lineTo(x, h * 0.55);
                ctx.stroke();
                ctx.fillStyle = '#777';
                ctx.font = '9px -apple-system, sans-serif';
                ctx.textAlign = 'center';
                ctx.fillText(String(normDeg), x, h * 0.42);
            } else if (is10) {
                ctx.strokeStyle = '#555';
                ctx.lineWidth = 1;
                ctx.beginPath();
                ctx.moveTo(x, h);
                ctx.lineTo(x, h * 0.65);
                ctx.stroke();
            } else {
                ctx.strokeStyle = '#333';
                ctx.lineWidth = 0.5;
                ctx.beginPath();
                ctx.moveTo(x, h);
                ctx.lineTo(x, h * 0.75);
                ctx.stroke();
            }
        }

        // Center indicator triangle
        ctx.fillStyle = '#4CAF50';
        ctx.beginPath();
        ctx.moveTo(w / 2, h);
        ctx.lineTo(w / 2 - 5, h - 7);
        ctx.lineTo(w / 2 + 5, h - 7);
        ctx.closePath();
        ctx.fill();

        // Center line
        ctx.strokeStyle = '#4CAF50';
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.moveTo(w / 2, h - 7);
        ctx.lineTo(w / 2, 0);
        ctx.stroke();
    }

    function renderBearingBarRaw(canvas, pan) {
        var ctx = canvas.getContext('2d');
        var w = canvas.width;
        var h = canvas.height;
        if (w === 0) return;
        ctx.clearRect(0, 0, w, h);

        // Background
        ctx.fillStyle = 'rgba(0, 0, 0, 0.55)';
        ctx.fillRect(0, 0, w, h);

        // Show pan range -1 to +1, with current pan centered
        var spread = 1.0; // +/- 0.5 visible
        var pixPerUnit = w / spread;
        var startVal = pan - spread / 2;
        var endVal = pan + spread / 2;

        // Draw ticks at 0.1 intervals
        for (var v = Math.floor(startVal * 10) / 10; v <= endVal + 0.001; v += 0.1) {
            var x = (v - startVal) * pixPerUnit;
            var rounded = Math.round(v * 100) / 100;
            var isMajor = (Math.abs(rounded % 0.5) < 0.01);
            var isZero = (Math.abs(rounded) < 0.01);

            if (isZero) {
                ctx.strokeStyle = '#fff';
                ctx.lineWidth = 2;
                ctx.beginPath();
                ctx.moveTo(x, h);
                ctx.lineTo(x, h * 0.35);
                ctx.stroke();
                ctx.fillStyle = '#fff';
                ctx.font = 'bold 11px -apple-system, sans-serif';
                ctx.textAlign = 'center';
                ctx.fillText('0', x, h * 0.25);
            } else if (isMajor) {
                ctx.strokeStyle = '#bbb';
                ctx.lineWidth = 1.5;
                ctx.beginPath();
                ctx.moveTo(x, h);
                ctx.lineTo(x, h * 0.45);
                ctx.stroke();
                ctx.fillStyle = '#aaa';
                ctx.font = '10px -apple-system, sans-serif';
                ctx.textAlign = 'center';
                ctx.fillText(rounded.toFixed(1), x, h * 0.32);
            } else {
                ctx.strokeStyle = '#555';
                ctx.lineWidth = 1;
                ctx.beginPath();
                ctx.moveTo(x, h);
                ctx.lineTo(x, h * 0.65);
                ctx.stroke();
            }
        }

        // Center indicator
        ctx.fillStyle = '#FF9800';
        ctx.beginPath();
        ctx.moveTo(w / 2, h);
        ctx.lineTo(w / 2 - 5, h - 7);
        ctx.lineTo(w / 2 + 5, h - 7);
        ctx.closePath();
        ctx.fill();

        ctx.strokeStyle = '#FF9800';
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.moveTo(w / 2, h - 7);
        ctx.lineTo(w / 2, 0);
        ctx.stroke();
    }

    // ================================================================
    // EXTENSIBILITY HOOKS (future use)
    // ================================================================
    // function onBboxComplete(cameraId, bbox) {
    //     // Future: highlightInOtherCameras(cameraId, bbox);
    //     // Future: computePtzTarget(cameraId, bbox);
    // }

})();
