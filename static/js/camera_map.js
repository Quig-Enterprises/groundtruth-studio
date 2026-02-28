/**
 * camera_map.js — GroundTruth Studio Camera Map
 *
 * Interactive Leaflet map for placing cameras, setting bearing/FOV/range,
 * and visualizing field-of-view cones.
 *
 * Depends on: Leaflet 1.9.4 (CDN), CAN_WRITE global from template.
 * XSS note: all user-supplied strings are inserted via textContent or
 * DOM construction — never via innerHTML.
 */

(function () {
    'use strict';

    // ================================================================
    // CONSTANTS
    // ================================================================

    var DEFAULT_COLOR      = '#4CAF50';
    var FOV_FILL_OPACITY   = 0.18;
    var FOV_STROKE_OPACITY = 0.55;
    var FOV_CONE_POINTS    = 32;
    var ROTATION_HANDLE_FRAC = 0.85;
    var SAVE_DEBOUNCE_MS   = 800;
    var DEFAULT_CENTER     = [39.5, -98.35];
    var DEFAULT_ZOOM       = 4;

    // ================================================================
    // STATE
    // ================================================================

    var map             = null;
    var cameras         = new Map();   // id → { data, marker, cone, rotationHandle }
    var unplacedCameras = [];
    var selectedId      = null;
    var placingCamera   = null;
    var contextMenuLatLng = null;
    var saveDebounceTimer = null;

    // ================================================================
    // BOOT
    // ================================================================

    document.addEventListener('DOMContentLoaded', function () {
        initMap();
        initSidebar();
        initDetailPanel();
        initContextMenu();
        loadAllData();

        if (!CAN_WRITE) {
            document.getElementById('readonly-badge').style.display = 'block';
        }

        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape') {
                cancelPlacementMode();
                closeContextMenu();
            }
        });
    });

    // ================================================================
    // MAP
    // ================================================================

    function initMap() {
        map = L.map('map', {
            center: DEFAULT_CENTER,
            zoom: DEFAULT_ZOOM,
            zoomControl: true,
            attributionControl: true
        });

        var tileRoad = L.tileLayer(
            'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
            {
                attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>',
                subdomains: 'abcd',
                maxZoom: 20
            }
        );

        var tileSatellite = L.tileLayer(
            'https://mt{s}.google.com/vt/lyrs=s&x={x}&y={y}&z={z}',
            {
                subdomains: '0123',
                attribution: 'Imagery &copy; Google',
                maxZoom: 21
            }
        );

        tileRoad.addTo(map);
        L.control.layers(
            { 'Map (Dark)': tileRoad, 'Satellite': tileSatellite },
            {},
            { position: 'topright' }
        ).addTo(map);

        map.on('click', function (e) {
            if (placingCamera) { commitPlacement(e.latlng); return; }
            closeContextMenu();
        });

        map.on('contextmenu', function (e) {
            if (!CAN_WRITE) return;
            contextMenuLatLng = e.latlng;
            showContextMenu(e.originalEvent.clientX, e.originalEvent.clientY);
            L.DomEvent.preventDefault(e);
        });
    }

    // ================================================================
    // DATA
    // ================================================================

    function loadAllData() {
        showMapLoading(true);
        Promise.all([fetchPlacements(), fetchUnplaced()])
            .then(function () { showMapLoading(false); })
            .catch(function (err) {
                console.error('[CameraMap] load error:', err);
                showMapLoading(false);
            });
    }

    function fetchPlacements() {
        return fetch('/api/camera-map/placements')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data.success && Array.isArray(data.placements)) {
                    data.placements.forEach(addOrUpdateCamera);
                    fitBoundsToAll();
                    renderPlacedList();
                }
            });
    }

    function fetchUnplaced() {
        return fetch('/api/camera-map/unplaced')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data.success && Array.isArray(data.cameras)) {
                    unplacedCameras = data.cameras;
                    renderUnplacedList();
                }
            });
    }

    function fitBoundsToAll() {
        if (cameras.size === 0) return;
        var pts = [];
        cameras.forEach(function (e) { pts.push([e.data.latitude, e.data.longitude]); });
        if (pts.length === 1) { map.setView(pts[0], 16); }
        else { map.fitBounds(L.latLngBounds(pts), { padding: [60, 60] }); }
    }

    // ================================================================
    // CAMERA LIFECYCLE
    // ================================================================

    function addOrUpdateCamera(placement) {
        var id = String(placement.id);

        if (cameras.has(id)) {
            var ex = cameras.get(id);
            ex.data = placement;
            updateMarkerColor(ex, placement.map_color || DEFAULT_COLOR);
            updateCone(ex);
            return ex;
        }

        var color  = placement.map_color || DEFAULT_COLOR;
        var marker = createCameraMarker(placement.latitude, placement.longitude, color, id);

        var cone = L.polygon(
            computeFovCone(placement.latitude, placement.longitude,
                           placement.bearing  || 0,
                           placement.fov_angle || 60,
                           placement.fov_range  || 50),
            { color: color, fillColor: color,
              fillOpacity: FOV_FILL_OPACITY, opacity: FOV_STROKE_OPACITY,
              weight: 1.5, interactive: false }
        ).addTo(map);

        var ptzCone = null;
        if (placement.is_ptz) {
            var ptzPoints = computeFovCone(
                placement.latitude, placement.longitude,
                placement.bearing || 0, placement.ptz_pan_range || 180, placement.fov_range || 50, 48
            );
            ptzCone = L.polygon(ptzPoints, {
                color: color, fillColor: color,
                fillOpacity: 0.08, weight: 1, opacity: 0.25,
                dashArray: '6,4', interactive: false
            }).addTo(map);
        }

        var entry = { data: placement, marker: marker, cone: cone, ptzCone: ptzCone, rotationHandle: null };
        cameras.set(id, entry);

        marker.on('click', function (e) {
            L.DomEvent.stopPropagation(e);
            selectCamera(id);
        });

        if (CAN_WRITE) {
            marker.on('drag', function () {
                var ll = marker.getLatLng();
                entry.data.latitude  = ll.lat;
                entry.data.longitude = ll.lng;
                updateCone(entry);
                if (selectedId === id) {
                    document.getElementById('detail-lat').textContent = ll.lat.toFixed(6);
                    document.getElementById('detail-lng').textContent = ll.lng.toFixed(6);
                    if (entry.rotationHandle) updateRotationHandlePosition(entry);
                }
            });
            marker.on('dragend', function () {
                var ll = marker.getLatLng();
                doPut(id, { latitude: ll.lat, longitude: ll.lng }, null);
            });
        }

        return entry;
    }

    function removeCamera(id) {
        var e = cameras.get(id);
        if (!e) return;
        if (e.marker)         map.removeLayer(e.marker);
        if (e.cone)           map.removeLayer(e.cone);
        if (e.ptzCone)        map.removeLayer(e.ptzCone);
        if (e.rotationHandle) map.removeLayer(e.rotationHandle);
        cameras.delete(id);
    }

    // ================================================================
    // MARKER
    // ================================================================

    function createCameraMarker(lat, lng, color, id) {
        var icon = L.divIcon({
            className:  'camera-marker-icon',
            iconSize:   [28, 28],
            iconAnchor: [14, 14],
            html: buildMarkerSvg(color, false)  // static SVG, no user data
        });

        var marker = L.marker([lat, lng], {
            icon:     icon,
            draggable: !!CAN_WRITE,
            zIndexOffset: 100
        }).addTo(map);

        marker._cameraId = id;
        return marker;
    }

    // buildMarkerSvg produces markup from trusted color constants only
    function buildMarkerSvg(color, selected) {
        var pulse = selected
            ? '<circle class="pulse-ring" cx="14" cy="14" r="10" fill="none" stroke="' + color + '" stroke-width="1.5" opacity="0"/>'
            : '';
        return (
            '<svg class="camera-marker-svg' + (selected ? ' selected' : '') +
            '" width="28" height="28" viewBox="0 0 28 28" xmlns="http://www.w3.org/2000/svg" overflow="visible">' +
            pulse +
            '<circle cx="14" cy="14" r="9" fill="#1a1a1a" stroke="' + color + '" stroke-width="2"/>' +
            '<circle cx="14" cy="14" r="4" fill="' + color + '" opacity="' + (selected ? '1' : '0.8') + '"/>' +
            '</svg>'
        );
    }

    function updateMarkerColor(entry, color) {
        var sel = (selectedId === String(entry.data.id));
        entry.marker.setIcon(L.divIcon({
            className: 'camera-marker-icon', iconSize: [28, 28], iconAnchor: [14, 14],
            html: buildMarkerSvg(color, sel)
        }));
    }

    function setMarkerSelected(entry, selected) {
        entry.marker.setIcon(L.divIcon({
            className: 'camera-marker-icon', iconSize: [28, 28], iconAnchor: [14, 14],
            html: buildMarkerSvg(entry.data.map_color || DEFAULT_COLOR, selected)
        }));
    }

    // ================================================================
    // FOV CONE MATH
    // ================================================================

    function destinationPoint(lat, lng, distMeters, bearingDeg) {
        var R    = 6371000;
        var d    = distMeters / R;
        var brng = bearingDeg * Math.PI / 180;
        var φ1   = lat * Math.PI / 180;
        var λ1   = lng * Math.PI / 180;
        var φ2   = Math.asin(Math.sin(φ1) * Math.cos(d) +
                              Math.cos(φ1) * Math.sin(d) * Math.cos(brng));
        var λ2   = λ1 + Math.atan2(
            Math.sin(brng) * Math.sin(d) * Math.cos(φ1),
            Math.cos(d) - Math.sin(φ1) * Math.sin(φ2)
        );
        return { lat: φ2 * 180 / Math.PI, lng: ((λ2 * 180 / Math.PI) + 540) % 360 - 180 };
    }

    function computeFovCone(lat, lng, bearing, fovAngle, rangeMeters, nPts) {
        nPts = nPts || FOV_CONE_POINTS;
        var pts   = [[lat, lng]];
        var start = bearing - fovAngle / 2;
        var end   = bearing + fovAngle / 2;
        for (var i = 0; i <= nPts; i++) {
            var angle = start + (end - start) * (i / nPts);
            var dest  = destinationPoint(lat, lng, rangeMeters, angle);
            pts.push([dest.lat, dest.lng]);
        }
        pts.push([lat, lng]);
        return pts;
    }

    function updateCone(entry) {
        var d = entry.data;
        var color = d.map_color || DEFAULT_COLOR;
        entry.cone.setLatLngs(computeFovCone(d.latitude, d.longitude,
            d.bearing || 0, d.fov_angle || 60, d.fov_range || 50));
        entry.cone.setStyle({ color: color, fillColor: color });

        // Update or create/remove PTZ sweep cone
        if (d.is_ptz) {
            var ptzPoints = computeFovCone(
                d.latitude, d.longitude,
                d.bearing || 0, d.ptz_pan_range || 180, d.fov_range || 50, 48
            );
            if (entry.ptzCone) {
                entry.ptzCone.setLatLngs(ptzPoints);
                entry.ptzCone.setStyle({ color: color, fillColor: color });
            } else {
                entry.ptzCone = L.polygon(ptzPoints, {
                    color: color, fillColor: color,
                    fillOpacity: 0.08, weight: 1, opacity: 0.25,
                    dashArray: '6,4', interactive: false
                }).addTo(map);
            }
        } else {
            if (entry.ptzCone) {
                map.removeLayer(entry.ptzCone);
                entry.ptzCone = null;
            }
        }
    }

    // ================================================================
    // ROTATION HANDLE
    // ================================================================

    function showRotationHandle(entry) {
        removeRotationHandle(entry);
        var d    = entry.data;
        var dist = (d.fov_range || 50) * ROTATION_HANDLE_FRAC;
        var pt   = destinationPoint(d.latitude, d.longitude, dist, d.bearing || 0);

        var handle = L.circleMarker([pt.lat, pt.lng], {
            radius: 7, color: d.map_color || DEFAULT_COLOR,
            fillColor: '#1a1a1a', fillOpacity: 1, weight: 2,
            className: 'rotation-handle', interactive: true
        }).addTo(map);

        handle._isDragging = false;

        handle.on('mousedown touchstart', function (e) {
            L.DomEvent.stopPropagation(e);
            handle._isDragging = true;
            map.dragging.disable();

            function onMove(moveEvt) {
                if (!handle._isDragging) return;
                var ll  = moveEvt.latlng || map.mouseEventToLatLng(moveEvt);
                var brg = bearingBetween(entry.data.latitude, entry.data.longitude, ll.lat, ll.lng);
                entry.data.bearing = brg;
                updateCone(entry);
                updateRotationHandlePosition(entry);
                if (selectedId === String(entry.data.id)) {
                    document.getElementById('detail-bearing').value = Math.round(brg);
                    document.getElementById('detail-bearing-val').textContent = Math.round(brg);
                }
            }

            function onUp() {
                if (!handle._isDragging) return;
                handle._isDragging = false;
                map.dragging.enable();
                map.off('mousemove', onMove);
                map.off('mouseup', onUp);
                document.removeEventListener('touchmove', onTouchMove);
                document.removeEventListener('touchend', onUp);
                scheduleSave(String(entry.data.id));
            }

            function onTouchMove(te) {
                if (!handle._isDragging) return;
                var t  = te.touches[0];
                var ll = map.containerPointToLatLng(
                    map.mouseEventToContainerPoint({ clientX: t.clientX, clientY: t.clientY })
                );
                onMove({ latlng: ll });
            }

            map.on('mousemove', onMove);
            map.on('mouseup', onUp);
            document.addEventListener('touchmove', onTouchMove, { passive: false });
            document.addEventListener('touchend', onUp);
        });

        entry.rotationHandle = handle;
    }

    function removeRotationHandle(entry) {
        if (entry.rotationHandle) {
            map.removeLayer(entry.rotationHandle);
            entry.rotationHandle = null;
        }
    }

    function updateRotationHandlePosition(entry) {
        if (!entry.rotationHandle) return;
        var d  = entry.data;
        var pt = destinationPoint(d.latitude, d.longitude,
                                  (d.fov_range || 50) * ROTATION_HANDLE_FRAC,
                                  d.bearing || 0);
        entry.rotationHandle.setLatLng([pt.lat, pt.lng]);
    }

    function bearingBetween(lat1, lng1, lat2, lng2) {
        var dL = (lng2 - lng1) * Math.PI / 180;
        var r1 = lat1 * Math.PI / 180;
        var r2 = lat2 * Math.PI / 180;
        var y  = Math.sin(dL) * Math.cos(r2);
        var x  = Math.cos(r1) * Math.sin(r2) - Math.sin(r1) * Math.cos(r2) * Math.cos(dL);
        return (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
    }

    // ================================================================
    // SELECTION
    // ================================================================

    function selectCamera(id) {
        if (selectedId && selectedId !== id) {
            var prev = cameras.get(selectedId);
            if (prev) { setMarkerSelected(prev, false); removeRotationHandle(prev); }
        }

        selectedId = id;
        var entry  = cameras.get(id);
        if (!entry) return;

        setMarkerSelected(entry, true);

        var d = entry.data;
        document.getElementById('detail-title').textContent       = d.camera_name || d.camera_id || 'Camera';
        document.getElementById('detail-name').value              = d.camera_name || '';
        document.getElementById('detail-bearing').value           = d.bearing     || 0;
        document.getElementById('detail-bearing-val').textContent = Math.round(d.bearing || 0);
        document.getElementById('detail-fov').value               = d.fov_angle   || 60;
        document.getElementById('detail-fov-val').textContent     = d.fov_angle   || 60;
        document.getElementById('detail-range').value             = d.fov_range   || 50;
        document.getElementById('detail-range-val').textContent   = d.fov_range   || 50;
        document.getElementById('detail-lat').textContent         = d.latitude  ? d.latitude.toFixed(6)  : '—';
        document.getElementById('detail-lng').textContent         = d.longitude ? d.longitude.toFixed(6) : '—';
        document.getElementById('detail-notes').value             = d.location_description || '';

        setActiveColorPreset(d.map_color || DEFAULT_COLOR);

        // PTZ controls
        var ptzCheckbox  = document.getElementById('input-ptz');
        var ptzRangeGrp  = document.getElementById('ptz-range-group');
        var ptzRangeInp  = document.getElementById('input-ptz-range');
        var ptzRangeVal  = document.getElementById('ptz-range-val');
        if (ptzCheckbox) {
            ptzCheckbox.checked = !!d.is_ptz;
            ptzRangeGrp.style.display = d.is_ptz ? '' : 'none';
            ptzRangeInp.value = d.ptz_pan_range || 180;
            ptzRangeVal.textContent = (d.ptz_pan_range || 180);
        }

        // Indoor control
        var indoorCheckbox = document.getElementById('input-indoor');
        if (indoorCheckbox) {
            indoorCheckbox.checked = !!d.is_indoor;
        }

        var editable = !!CAN_WRITE;
        ['detail-name', 'detail-bearing', 'detail-fov', 'detail-range', 'detail-notes', 'input-ptz', 'input-ptz-range', 'input-indoor'].forEach(function (id) {
            var el = document.getElementById(id);
            if (el) el.disabled = !editable;
        });
        document.getElementById('detail-actions').style.display = editable ? 'flex' : 'none';
        document.querySelectorAll('.color-preset').forEach(function (el) {
            el.style.pointerEvents = editable ? '' : 'none';
            el.style.opacity       = editable ? '' : '0.4';
        });

        if (CAN_WRITE) showRotationHandle(entry);

        // Live feed link
        var feedLinkDiv = document.getElementById('detail-feed-link');
        var feedLink = document.getElementById('feed-link');
        if (feedLinkDiv && d.camera_id && !d.camera_id.startsWith('manual-')) {
            feedLink.href = 'https://frigate.ecoeyetech.com/#/cameras/' + encodeURIComponent(d.camera_id);
            feedLinkDiv.style.display = 'block';
        } else if (feedLinkDiv) {
            feedLinkDiv.style.display = 'none';
        }

        // Fetch camera preview
        var previewDiv = document.getElementById('detail-preview');
        var previewImg = document.getElementById('preview-img');
        var previewCaption = document.getElementById('preview-caption');
        if (previewDiv && d.camera_id) {
            fetch('/api/camera-map/preview/' + encodeURIComponent(d.camera_id))
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    if (data.success && data.preview_url) {
                        previewImg.src = data.preview_url;
                        previewDiv.style.display = 'block';
                        if (data.captured_at) {
                            previewCaption.textContent = 'Latest capture: ' + new Date(data.captured_at).toLocaleString();
                        }
                    } else {
                        previewDiv.style.display = 'none';
                    }
                })
                .catch(function() { previewDiv.style.display = 'none'; });
        } else if (previewDiv) {
            previewDiv.style.display = 'none';
        }

        document.getElementById('detail-panel').classList.add('open');
        hideSaveFeedback();
        renderPlacedList();
    }

    function deselectCamera() {
        if (selectedId) {
            var entry = cameras.get(selectedId);
            if (entry) { setMarkerSelected(entry, false); removeRotationHandle(entry); }
        }
        selectedId = null;
        document.getElementById('detail-panel').classList.remove('open');
        var previewDiv = document.getElementById('detail-preview');
        if (previewDiv) previewDiv.style.display = 'none';
        renderPlacedList();
    }

    // ================================================================
    // DETAIL PANEL
    // ================================================================

    function initDetailPanel() {
        document.getElementById('detail-close').addEventListener('click', deselectCamera);

        document.getElementById('detail-bearing').addEventListener('input', function () {
            var val = parseInt(this.value, 10);
            document.getElementById('detail-bearing-val').textContent = val;
            if (!selectedId) return;
            var e = cameras.get(selectedId);
            if (!e) return;
            e.data.bearing = val;
            updateCone(e);
            updateRotationHandlePosition(e);
        });

        document.getElementById('detail-fov').addEventListener('input', function () {
            var val = parseInt(this.value, 10);
            document.getElementById('detail-fov-val').textContent = val;
            if (!selectedId) return;
            var e = cameras.get(selectedId);
            if (!e) return;
            e.data.fov_angle = val;
            updateCone(e);
        });

        document.getElementById('detail-range').addEventListener('input', function () {
            var val = parseInt(this.value, 10);
            document.getElementById('detail-range-val').textContent = val;
            if (!selectedId) return;
            var e = cameras.get(selectedId);
            if (!e) return;
            e.data.fov_range = val;
            updateCone(e);
            updateRotationHandlePosition(e);
        });

        document.getElementById('input-ptz').addEventListener('change', function () {
            if (!selectedId) return;
            var e = cameras.get(selectedId);
            if (!e) return;
            e.data.is_ptz = this.checked;
            document.getElementById('ptz-range-group').style.display = this.checked ? '' : 'none';
            updateCone(e);
            scheduleSave(selectedId);
        });

        document.getElementById('input-ptz-range').addEventListener('input', function () {
            var val = parseFloat(this.value);
            document.getElementById('ptz-range-val').textContent = Math.round(val);
            if (!selectedId) return;
            var e = cameras.get(selectedId);
            if (!e) return;
            e.data.ptz_pan_range = val;
            updateCone(e);
            scheduleSave(selectedId);
        });

        document.getElementById('input-indoor').addEventListener('change', function () {
            if (!selectedId) return;
            var e = cameras.get(selectedId);
            if (!e) return;
            e.data.is_indoor = this.checked;
            scheduleSave(selectedId);
        });

        document.getElementById('color-presets').addEventListener('click', function (evt) {
            var preset = evt.target.closest('.color-preset');
            if (!preset) return;
            var color = preset.dataset.color;
            setActiveColorPreset(color);
            if (!selectedId) return;
            var e = cameras.get(selectedId);
            if (!e) return;
            e.data.map_color = color;
            updateCone(e);
            updateMarkerColor(e, color);
            setMarkerSelected(e, true);
            if (e.rotationHandle) e.rotationHandle.setStyle({ color: color });
        });

        document.getElementById('btn-save').addEventListener('click', function () {
            if (selectedId) doSave(selectedId, false);
        });

        document.getElementById('btn-delete').addEventListener('click', function () {
            if (!selectedId) return;
            var e    = cameras.get(selectedId);
            var name = e ? (e.data.camera_name || e.data.camera_id || 'this camera') : 'this camera';
            if (!confirm('Remove "' + name + '" from the map?')) return;
            doDelete(selectedId);
        });

    }

    function setActiveColorPreset(color) {
        document.querySelectorAll('.color-preset').forEach(function (el) {
            el.classList.toggle('active', el.dataset.color.toLowerCase() === color.toLowerCase());
        });
    }

    function scheduleSave(id) {
        clearTimeout(saveDebounceTimer);
        saveDebounceTimer = setTimeout(function () { doSave(id, true); }, SAVE_DEBOUNCE_MS);
    }

    function doSave(id, silent) {
        var entry = cameras.get(id);
        if (!entry) return;
        var d = entry.data;

        var payload = {
            camera_name:          (document.getElementById('detail-name').value.trim() || d.camera_name),
            latitude:             d.latitude,
            longitude:            d.longitude,
            bearing:              d.bearing       || 0,
            fov_angle:            d.fov_angle     || 60,
            fov_range:            d.fov_range     || 50,
            map_color:            d.map_color     || DEFAULT_COLOR,
            is_ptz:               d.is_ptz        || false,
            ptz_pan_range:        d.ptz_pan_range || 180,
            is_indoor:            d.is_indoor     || false,
            location_description: document.getElementById('detail-notes').value.trim()
        };

        var btn = document.getElementById('btn-save');
        if (btn) { btn.disabled = true; btn.textContent = 'Saving…'; }

        doPut(id, payload, function (updated) {
            if (updated) {
                Object.assign(d, payload);
                document.getElementById('detail-title').textContent = d.camera_name || d.camera_id || 'Camera';
                renderPlacedList();
            }
            if (!silent) showSaveFeedback();
            if (btn) { btn.disabled = false; btn.textContent = 'Save'; }
        });
    }

    function doDelete(id) {
        fetch('/api/camera-map/placements/' + encodeURIComponent(id), { method: 'DELETE' })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!data.success) { alert('Delete failed.'); return; }
                var entry = cameras.get(id);
                if (entry && entry.data.camera_id) {
                    unplacedCameras.push({
                        camera_id:    entry.data.camera_id,
                        camera_name:  entry.data.camera_name,
                        location_name: entry.data.location_name,
                        source:       entry.data.source
                    });
                }
                removeCamera(id);
                deselectCamera();
                renderUnplacedList();
                renderPlacedList();
            })
            .catch(function (err) {
                console.error('[CameraMap] delete error:', err);
                alert('Error deleting camera placement.');
            });
    }

    function showSaveFeedback() {
        var el = document.getElementById('save-feedback');
        el.classList.add('visible');
        setTimeout(function () { el.classList.remove('visible'); }, 2000);
    }

    function hideSaveFeedback() {
        document.getElementById('save-feedback').classList.remove('visible');
    }

    // ================================================================
    // API
    // ================================================================

    function doPut(id, payload, callback) {
        fetch('/api/camera-map/placements/' + encodeURIComponent(id), {
            method:  'PUT',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify(payload)
        })
            .then(function (r) { return r.json(); })
            .then(function (data) { if (callback) callback(data.success ? data.placement : null); })
            .catch(function (err) {
                console.error('[CameraMap] PUT error:', err);
                if (callback) callback(null);
            });
    }

    function doPost(payload, callback) {
        fetch('/api/camera-map/placements', {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify(payload)
        })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (callback) callback(data.success ? data.placement : null, data.error || null);
            })
            .catch(function (err) {
                console.error('[CameraMap] POST error:', err);
                if (callback) callback(null, err.message);
            });
    }

    // ================================================================
    // PLACEMENT MODE
    // ================================================================

    function enterPlacementMode(cam) {
        placingCamera = cam;
        document.getElementById('map').classList.add('placing-mode');
        document.getElementById('placement-banner').classList.add('visible');
        var btn = document.querySelector('.btn-place[data-camera-id="' + cam.camera_id + '"]');
        if (btn) {
            document.querySelectorAll('.btn-place.placing').forEach(function (b) {
                b.classList.remove('placing');
                b.textContent = 'Place';
            });
            btn.classList.add('placing');
            btn.textContent = 'Cancel';
        }
    }

    function cancelPlacementMode() {
        if (!placingCamera) return;
        placingCamera = null;
        document.getElementById('map').classList.remove('placing-mode');
        document.getElementById('placement-banner').classList.remove('visible');
        document.querySelectorAll('.btn-place.placing').forEach(function (b) {
            b.classList.remove('placing');
            b.textContent = 'Place';
        });
    }

    function commitPlacement(latlng) {
        if (!placingCamera) return;
        var cam = placingCamera;
        cancelPlacementMode();

        doPost({
            camera_id:   cam.camera_id,
            camera_name: cam.camera_name || cam.camera_id,
            latitude:    latlng.lat,
            longitude:   latlng.lng,
            bearing:     0,
            fov_angle:   60,
            fov_range:   50,
            map_color:   DEFAULT_COLOR
        }, function (placement, err) {
            if (!placement) { alert('Error placing camera: ' + (err || 'Unknown error')); return; }
            unplacedCameras = unplacedCameras.filter(function (c) { return c.camera_id !== cam.camera_id; });
            addOrUpdateCamera(placement);
            renderUnplacedList();
            renderPlacedList();
            selectCamera(String(placement.id));
            map.setView(latlng, Math.max(map.getZoom(), 16));
        });
    }

    // ================================================================
    // CONTEXT MENU
    // ================================================================

    function initContextMenu() {
        document.getElementById('ctx-add-camera').addEventListener('click', function () {
            closeContextMenu();
            if (!contextMenuLatLng || !CAN_WRITE) return;
            var name = prompt('Camera name (or ID):');
            if (!name || !name.trim()) return;

            doPost({
                camera_id:   'manual-' + Date.now(),
                camera_name: name.trim(),
                latitude:    contextMenuLatLng.lat,
                longitude:   contextMenuLatLng.lng,
                bearing:     0,
                fov_angle:   60,
                fov_range:   50,
                map_color:   DEFAULT_COLOR
            }, function (placement, err) {
                if (!placement) { alert('Error adding camera: ' + (err || 'Unknown error')); return; }
                addOrUpdateCamera(placement);
                renderPlacedList();
                selectCamera(String(placement.id));
                map.setView(contextMenuLatLng, Math.max(map.getZoom(), 16));
            });
        });

        map.on('movestart', closeContextMenu);
        document.addEventListener('click', function (e) {
            if (!e.target.closest('#context-menu')) closeContextMenu();
        });
    }

    function showContextMenu(x, y) {
        var menu = document.getElementById('context-menu');
        menu.style.left = x + 'px';
        menu.style.top  = y + 'px';
        menu.classList.add('visible');
    }

    function closeContextMenu() {
        document.getElementById('context-menu').classList.remove('visible');
    }

    // ================================================================
    // SIDEBAR (DOM-constructed lists — no innerHTML for user data)
    // ================================================================

    function initSidebar() {
        var sidebar   = document.getElementById('sidebar');
        var toggle    = document.getElementById('sidebar-toggle');
        var collapsed = false;

        toggle.addEventListener('click', function () {
            collapsed = !collapsed;
            sidebar.classList.toggle('collapsed', collapsed);
            toggle.textContent = collapsed ? '\u2039' : '\u203A';
            toggle.title       = collapsed ? 'Expand sidebar' : 'Collapse sidebar';
        });

        document.getElementById('camera-search').addEventListener('input', function () {
            var q = this.value.trim().toLowerCase();
            renderUnplacedList(q);
            renderPlacedList(q);
        });
    }

    /* Build the "Unplaced" camera list with pure DOM construction */
    function renderUnplacedList(filter) {
        var container = document.getElementById('unplaced-list');
        var countEl   = document.getElementById('unplaced-count');

        var list = filter
            ? unplacedCameras.filter(function (c) { return matchesFilter(c, filter); })
            : unplacedCameras;

        countEl.textContent = list.length;
        clearChildren(container);

        if (list.length === 0) {
            var empty = document.createElement('div');
            empty.className   = 'camera-item-empty';
            empty.textContent = filter ? 'No matches' : 'All cameras placed';
            container.appendChild(empty);
            return;
        }

        list.forEach(function (cam) {
            var row = document.createElement('div');
            row.className = 'camera-item';

            // Preview thumbnail
            var thumb = document.createElement('img');
            thumb.className = 'camera-item-thumb';
            thumb.alt = '';
            thumb.style.display = 'none';
            row.appendChild(thumb);
            (function(imgEl, cid) {
                fetch('/api/camera-map/preview/' + encodeURIComponent(cid))
                    .then(function(r) { return r.json(); })
                    .then(function(d) {
                        if (d.success && d.preview_url) {
                            imgEl.src = d.preview_url;
                            imgEl.style.display = 'block';
                        }
                    })
                    .catch(function() {});
            })(thumb, cam.camera_id);

            var info = document.createElement('div');
            info.className = 'camera-item-info';

            var nameEl = document.createElement('div');
            nameEl.className   = 'camera-item-name';
            nameEl.textContent = cam.camera_name || cam.camera_id || 'Unnamed';
            nameEl.title       = cam.camera_name || cam.camera_id || '';
            info.appendChild(nameEl);

            if (cam.location_name || cam.source) {
                var sub = document.createElement('div');
                sub.className   = 'camera-item-sub';
                sub.textContent = cam.location_name || cam.source;
                info.appendChild(sub);
            }

            row.appendChild(info);

            if (CAN_WRITE) {
                var action = document.createElement('div');
                action.className = 'camera-item-action';

                var btn = document.createElement('button');
                btn.className         = 'btn-place';
                btn.textContent       = 'Place';
                btn.dataset.cameraId  = cam.camera_id;
                btn.addEventListener('click', function () {
                    if (placingCamera && placingCamera.camera_id === cam.camera_id) {
                        cancelPlacementMode();
                    } else {
                        cancelPlacementMode();
                        enterPlacementMode(cam);
                    }
                });

                action.appendChild(btn);
                row.appendChild(action);
            }

            container.appendChild(row);
        });
    }

    /* Build the "Placed" camera list with pure DOM construction */
    function renderPlacedList(filter) {
        var container = document.getElementById('placed-list');
        var countEl   = document.getElementById('placed-count');

        var entries = [];
        cameras.forEach(function (e) { entries.push(e); });

        if (filter) {
            entries = entries.filter(function (e) { return matchesFilter(e.data, filter); });
        }

        entries.sort(function (a, b) {
            var na = (a.data.camera_name || a.data.camera_id || '').toLowerCase();
            var nb = (b.data.camera_name || b.data.camera_id || '').toLowerCase();
            return na < nb ? -1 : na > nb ? 1 : 0;
        });

        countEl.textContent = entries.length;
        clearChildren(container);

        if (entries.length === 0) {
            var empty = document.createElement('div');
            empty.className   = 'camera-item-empty';
            empty.textContent = filter ? 'No matches' : 'No cameras placed yet';
            container.appendChild(empty);
            return;
        }

        entries.forEach(function (entry) {
            var d     = entry.data;
            var id    = String(d.id);
            var color = d.map_color || DEFAULT_COLOR;

            var row = document.createElement('div');
            row.className = 'camera-item' + (selectedId === id ? ' active' : '');
            row.addEventListener('click', function () {
                var e = cameras.get(id);
                if (!e) return;
                map.flyTo([e.data.latitude, e.data.longitude],
                           Math.max(map.getZoom(), 17),
                           { animate: true, duration: 0.8 });
                selectCamera(id);
            });

            // Preview thumbnail
            var thumb = document.createElement('img');
            thumb.className = 'camera-item-thumb';
            thumb.alt = '';
            thumb.style.display = 'none';
            row.appendChild(thumb);
            (function(imgEl, cid) {
                fetch('/api/camera-map/preview/' + encodeURIComponent(cid))
                    .then(function(r) { return r.json(); })
                    .then(function(data) {
                        if (data.success && data.preview_url) {
                            imgEl.src = data.preview_url;
                            imgEl.style.display = 'block';
                        }
                    })
                    .catch(function() {});
            })(thumb, d.camera_id);

            var dot = document.createElement('div');
            dot.className        = 'camera-color-dot';
            dot.style.background = color;

            var info = document.createElement('div');
            info.className = 'camera-item-info';

            var nameEl = document.createElement('div');
            nameEl.className   = 'camera-item-name';
            nameEl.textContent = d.camera_name || d.camera_id || 'Unnamed';
            nameEl.title       = d.camera_name || d.camera_id || '';
            info.appendChild(nameEl);

            if (d.location_name) {
                var sub = document.createElement('div');
                sub.className   = 'camera-item-sub';
                sub.textContent = d.location_name;
                info.appendChild(sub);
            }

            row.appendChild(dot);
            row.appendChild(info);
            container.appendChild(row);
        });
    }

    function matchesFilter(obj, filter) {
        var f = filter.toLowerCase();
        return (
            (obj.camera_id    && obj.camera_id.toLowerCase().includes(f))    ||
            (obj.camera_name  && obj.camera_name.toLowerCase().includes(f))  ||
            (obj.location_name && obj.location_name.toLowerCase().includes(f))
        );
    }

    // ================================================================
    // UTILITIES
    // ================================================================

    function clearChildren(el) {
        while (el.firstChild) el.removeChild(el.firstChild);
    }

    function showMapLoading(show) {
        document.getElementById('map-loading').style.display = show ? 'flex' : 'none';
    }

})();
