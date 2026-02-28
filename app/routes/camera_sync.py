"""Camera Sync — multi-camera overlap groups, FOV geometry, and synchronized viewing."""
from flask import Blueprint, request, jsonify, render_template
from db_connection import get_connection
from psycopg2 import extras
import requests as http_requests
import logging
import math
import os
import threading
import time

camera_sync_bp = Blueprint('camera_sync', __name__)
logger = logging.getLogger(__name__)

# go2rtc API inside Frigate container (direct, bypasses Frigate's nginx POST block)
GO2RTC_API = os.environ.get('GO2RTC_API_URL', 'http://172.200.1.6:1984')
FRIGATE_API = os.environ.get('FRIGATE_API_URL', 'https://172.200.1.6:8971')


# ---------------------------------------------------------------------------
# FOV Geometry Helpers (ported from camera_map.js)
# ---------------------------------------------------------------------------

def destination_point(lat, lng, dist_meters, bearing_deg):
    """Great-circle destination point given start, distance, and bearing."""
    R = 6371000
    d = dist_meters / R
    brng = math.radians(bearing_deg)
    phi1 = math.radians(lat)
    lam1 = math.radians(lng)
    phi2 = math.asin(math.sin(phi1) * math.cos(d) +
                      math.cos(phi1) * math.sin(d) * math.cos(brng))
    lam2 = lam1 + math.atan2(
        math.sin(brng) * math.sin(d) * math.cos(phi1),
        math.cos(d) - math.sin(phi1) * math.sin(phi2)
    )
    return (math.degrees(phi2), ((math.degrees(lam2) + 540) % 360) - 180)


def compute_fov_polygon(lat, lng, bearing, fov_angle, range_meters, n_pts=32):
    """Build a Shapely Polygon representing the camera's FOV cone."""
    from shapely.geometry import Polygon

    pts = [(lng, lat)]  # Shapely uses (x, y) = (lng, lat)
    start = bearing - fov_angle / 2
    end = bearing + fov_angle / 2
    for i in range(n_pts + 1):
        angle = start + (end - start) * (i / n_pts)
        dest_lat, dest_lng = destination_point(lat, lng, range_meters, angle)
        pts.append((dest_lng, dest_lat))
    pts.append((lng, lat))
    if len(pts) < 4:
        return None
    return Polygon(pts)


# ---------------------------------------------------------------------------
# Union-Find for overlap grouping
# ---------------------------------------------------------------------------

def _find(parent, i):
    if parent[i] != i:
        parent[i] = _find(parent, parent[i])
    return parent[i]


def _union(parent, rank, a, b):
    ra, rb = _find(parent, a), _find(parent, b)
    if ra == rb:
        return
    if rank[ra] < rank[rb]:
        ra, rb = rb, ra
    parent[rb] = ra
    if rank[ra] == rank[rb]:
        rank[ra] += 1


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@camera_sync_bp.route('/camera-sync')
def camera_sync_page():
    """Render the camera sync view."""
    return render_template('camera_sync.html')


@camera_sync_bp.route('/api/camera-sync/groups', methods=['GET'])
def get_groups():
    """List all overlap groups."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
            cursor.execute('''
                SELECT id, group_name, description, camera_ids,
                       is_auto_computed, manual_override, overlap_scores,
                       created_date, updated_date
                FROM camera_overlap_groups
                ORDER BY group_name
            ''')
            groups = [dict(row) for row in cursor.fetchall()]
        return jsonify({'success': True, 'groups': groups})
    except Exception as e:
        logger.error(f"Error fetching overlap groups: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@camera_sync_bp.route('/api/camera-sync/groups/compute', methods=['POST'])
def compute_groups():
    """Auto-compute overlap groups from FOV geometry."""
    try:
        data = request.json or {}
        threshold = data.get('threshold', 0.05)

        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)

            # 1. Fetch all cameras with coordinates
            cursor.execute('''
                SELECT id, camera_id, camera_name, latitude, longitude,
                       bearing, fov_angle, fov_range, is_ptz, ptz_pan_range
                FROM camera_locations
                WHERE latitude IS NOT NULL AND longitude IS NOT NULL
            ''')
            cameras = [dict(row) for row in cursor.fetchall()]

        if len(cameras) < 2:
            return jsonify({'success': True, 'groups': [],
                            'message': 'Need at least 2 placed cameras'})

        # 2. Build Shapely polygons
        polys = []
        valid_cameras = []
        for cam in cameras:
            fov = cam['ptz_pan_range'] if cam.get('is_ptz') else cam['fov_angle']
            fov = fov or 90
            rng = cam['fov_range'] or 30
            bearing = cam['bearing'] or 0
            poly = compute_fov_polygon(
                float(cam['latitude']), float(cam['longitude']),
                float(bearing), float(fov), float(rng)
            )
            if poly and poly.is_valid:
                polys.append(poly)
                valid_cameras.append(cam)

        n = len(valid_cameras)
        if n < 2:
            return jsonify({'success': True, 'groups': [],
                            'message': 'Need at least 2 cameras with valid FOV'})

        # 3. Pairwise IoU and union-find grouping
        parent = list(range(n))
        rank = [0] * n
        overlap_scores = {}

        for i in range(n):
            for j in range(i + 1, n):
                try:
                    intersection = polys[i].intersection(polys[j])
                    union_area = polys[i].union(polys[j]).area
                    iou = intersection.area / union_area if union_area > 0 else 0
                except Exception:
                    iou = 0
                if iou > threshold:
                    _union(parent, rank, i, j)
                    key = (min(i, j), max(i, j))
                    overlap_scores[key] = iou

        # 4. Collect groups (only groups with 2+ members)
        from collections import defaultdict
        group_map = defaultdict(list)
        for i in range(n):
            root = _find(parent, i)
            group_map[root].append(i)

        new_groups = []
        for members in group_map.values():
            if len(members) < 2:
                continue
            cam_ids = [valid_cameras[m]['camera_id'] for m in members]
            cam_names = [valid_cameras[m]['camera_name'] or valid_cameras[m]['camera_id']
                         for m in members]
            # Average overlap score for the group
            scores = []
            for a in range(len(members)):
                for b in range(a + 1, len(members)):
                    key = (min(members[a], members[b]), max(members[a], members[b]))
                    if key in overlap_scores:
                        scores.append(overlap_scores[key])
            avg_score = sum(scores) / len(scores) if scores else 0

            new_groups.append({
                'camera_ids': cam_ids,
                'group_name': ' + '.join(sorted(cam_names)),
                'description': f'Auto-computed overlap group ({len(cam_ids)} cameras, '
                               f'avg IoU {avg_score:.3f})',
                'overlap_score': round(avg_score, 4),
            })

        # 5. Replace auto-computed groups in DB
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)

            cursor.execute('''
                DELETE FROM camera_overlap_groups
                WHERE is_auto_computed = TRUE AND manual_override = FALSE
            ''')

            inserted = []
            for g in new_groups:
                cursor.execute('''
                    INSERT INTO camera_overlap_groups
                        (group_name, description, camera_ids,
                         is_auto_computed, manual_override, overlap_scores,
                         computed_at)
                    VALUES (%s, %s, %s, TRUE, FALSE, %s, CURRENT_TIMESTAMP)
                    RETURNING *
                ''', (
                    g['group_name'],
                    g['description'],
                    g['camera_ids'],
                    extras.Json({'avg_iou': g['overlap_score']}),
                ))
                inserted.append(dict(cursor.fetchone()))
            conn.commit()

        return jsonify({'success': True, 'groups': inserted,
                        'computed': len(inserted)})
    except Exception as e:
        logger.error(f"Error computing overlap groups: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@camera_sync_bp.route('/api/camera-sync/groups', methods=['POST'])
def create_or_update_group():
    """Create or update a manual overlap group."""
    try:
        data = request.json
        group_name = data.get('group_name', '').strip()
        if not group_name:
            return jsonify({'success': False, 'error': 'group_name is required'}), 400

        camera_ids = data.get('camera_ids', [])
        if not camera_ids:
            return jsonify({'success': False, 'error': 'camera_ids is required'}), 400

        description = data.get('description', '')
        group_id = data.get('id')

        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)

            if group_id:
                # Update existing group
                cursor.execute('''
                    UPDATE camera_overlap_groups
                    SET group_name = %s, description = %s, camera_ids = %s,
                        manual_override = TRUE, updated_date = CURRENT_TIMESTAMP
                    WHERE id = %s
                    RETURNING *
                ''', (group_name, description, camera_ids, group_id))
            else:
                # Insert new manual group
                cursor.execute('''
                    INSERT INTO camera_overlap_groups
                        (group_name, description, camera_ids,
                         is_auto_computed, manual_override)
                    VALUES (%s, %s, %s, FALSE, TRUE)
                    RETURNING *
                ''', (group_name, description, camera_ids))

            result = cursor.fetchone()
            conn.commit()

        if not result:
            return jsonify({'success': False, 'error': 'Group not found'}), 404
        return jsonify({'success': True, 'group': dict(result)})
    except Exception as e:
        logger.error(f"Error saving overlap group: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@camera_sync_bp.route('/api/camera-sync/groups/<int:group_id>', methods=['DELETE'])
def delete_group(group_id):
    """Delete an overlap group."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
            cursor.execute('DELETE FROM camera_overlap_groups WHERE id = %s', (group_id,))
            success = cursor.rowcount > 0
            conn.commit()
        return jsonify({'success': success})
    except Exception as e:
        logger.error(f"Error deleting overlap group: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@camera_sync_bp.route('/api/camera-sync/webrtc-offer', methods=['POST'])
def webrtc_offer():
    """Proxy WebRTC signaling to go2rtc inside Frigate container (avoids CORS)."""
    try:
        data = request.json
        camera = data.get('camera', '').strip()
        if not camera:
            return jsonify({'success': False, 'error': 'camera name required'}), 400

        sdp_offer = data.get('sdp', '')
        if not sdp_offer:
            return jsonify({'success': False, 'error': 'SDP offer required'}), 400

        # WebRTC SDP exchange with go2rtc (streams configured via Frigate's go2rtc: config)
        resp = http_requests.post(
            f'{GO2RTC_API}/api/webrtc?src={camera}',
            data=sdp_offer,
            headers={'Content-Type': 'application/sdp'},
            timeout=10
        )

        if resp.status_code in (200, 201):
            return resp.text, 200, {'Content-Type': 'application/sdp'}
        else:
            logger.error(f"go2rtc returned {resp.status_code}: {resp.text[:200]}")
            return jsonify({'success': False,
                            'error': f'go2rtc returned {resp.status_code}'}), 502
    except http_requests.exceptions.ConnectionError:
        return jsonify({'success': False, 'error': 'Cannot reach go2rtc'}), 502
    except Exception as e:
        logger.error(f"WebRTC proxy error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@camera_sync_bp.route('/api/camera-sync/snapshot/<camera_id>')
def camera_snapshot(camera_id):
    """Proxy snapshot — tries go2rtc frame.jpeg first, falls back to Frigate latest.jpg."""
    from flask import Response
    h = request.args.get('h', '720')

    # Try go2rtc first (faster, no auth)
    try:
        resp = http_requests.get(
            f'{GO2RTC_API}/api/frame.jpeg',
            timeout=5,
            params={'src': camera_id}
        )
        if resp.status_code == 200 and len(resp.content) > 100:
            return Response(resp.content, mimetype='image/jpeg',
                            headers={'Cache-Control': 'no-cache'})
    except Exception:
        pass

    # Fallback to Frigate snapshot
    try:
        resp = http_requests.get(
            f'{FRIGATE_API}/api/{camera_id}/latest.jpg',
            timeout=5,
            params={'h': h},
            verify=False
        )
        if resp.status_code == 200:
            return Response(resp.content, mimetype='image/jpeg',
                            headers={'Cache-Control': 'no-cache'})
    except Exception:
        pass

    return jsonify({'success': False, 'error': 'No snapshot source available'}), 502


@camera_sync_bp.route('/api/camera-sync/selections', methods=['POST'])
def save_selection():
    """Save a bbox selection for a camera within a group."""
    try:
        data = request.json
        source_camera_id = data.get('source_camera_id', '').strip()
        if not source_camera_id:
            return jsonify({'success': False,
                            'error': 'source_camera_id is required'}), 400

        group_id = data.get('group_id')
        if group_id is None:
            return jsonify({'success': False, 'error': 'group_id is required'}), 400

        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
            cursor.execute('''
                INSERT INTO camera_sync_selections
                    (source_camera_id, group_id, bbox_x, bbox_y,
                     bbox_width, bbox_height, frame_width, frame_height,
                     label, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
            ''', (
                source_camera_id,
                group_id,
                data.get('bbox_x', 0),
                data.get('bbox_y', 0),
                data.get('bbox_width', 0),
                data.get('bbox_height', 0),
                data.get('frame_width'),
                data.get('frame_height'),
                data.get('label', ''),
                extras.Json(data.get('metadata')) if data.get('metadata') else None,
            ))
            selection = dict(cursor.fetchone())
            conn.commit()
        return jsonify({'success': True, 'selection': selection})
    except Exception as e:
        logger.error(f"Error saving selection: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@camera_sync_bp.route('/api/camera-sync/selections', methods=['GET'])
def get_selections():
    """List recent bbox selections, optionally filtered by camera or group."""
    try:
        camera_id = request.args.get('camera_id')
        group_id = request.args.get('group_id')
        limit = request.args.get('limit', 50, type=int)

        conditions = []
        params = []
        if camera_id:
            conditions.append('source_camera_id = %s')
            params.append(camera_id)
        if group_id:
            conditions.append('group_id = %s')
            params.append(int(group_id))

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ''
        params.append(limit)

        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
            cursor.execute(f'''
                SELECT *
                FROM camera_sync_selections
                {where}
                ORDER BY created_date DESC
                LIMIT %s
            ''', params)
            selections = [dict(row) for row in cursor.fetchall()]
        return jsonify({'success': True, 'selections': selections})
    except Exception as e:
        logger.error(f"Error fetching selections: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ---------------------------------------------------------------------------
# PTZ Control via ONVIF (through relay at RTSP server)
# ---------------------------------------------------------------------------

# Cache: camera_id → { 'ptz_service', 'profile_token', 'camera' }
_onvif_cache = {}
_onvif_lock = threading.Lock()

# PTZ movement calibration cache: {camera_id: {pan_speed, tilt_speed, zoom_at, ...}}
_ptz_cal_cache = {}


def _get_onvif(camera_id):
    """Get or create an ONVIF PTZ service for a camera using per-camera DB credentials."""
    with _onvif_lock:
        entry = _onvif_cache.get(camera_id)
        if entry:
            # Refresh connection if older than 5 minutes
            age = time.time() - entry.get('_connected_at', 0)
            if age < 300:
                return entry
            else:
                logger.info(f"ONVIF connection for {camera_id} stale ({age:.0f}s), reconnecting")
                del _onvif_cache[camera_id]

    # Look up ONVIF credentials from DB
    with get_connection() as conn:
        cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
        cursor.execute('''
            SELECT onvif_host, onvif_port, onvif_username, onvif_password
            FROM camera_locations
            WHERE camera_id = %s
        ''', (camera_id,))
        row = cursor.fetchone()

    if not row or not row.get('onvif_host'):
        raise RuntimeError(f'No ONVIF credentials configured for camera {camera_id}')

    host = row['onvif_host']
    port = int(row.get('onvif_port') or 80)
    user = row.get('onvif_username') or ''
    passwd = row.get('onvif_password') or ''

    try:
        from onvif import ONVIFCamera
        cam = ONVIFCamera(host, port, user, passwd)
        media = cam.create_media_service()
        ptz = cam.create_ptz_service()
        profiles = media.GetProfiles()
        if not profiles:
            raise RuntimeError('No ONVIF media profiles found')

        # Find profile matching camera_id, or use first profile
        token = profiles[0].token
        for p in profiles:
            if camera_id.lower() in (p.Name or '').lower():
                token = p.token
                break

        entry = {'camera': cam, 'ptz': ptz, 'token': token, '_connected_at': time.time()}
        with _onvif_lock:
            _onvif_cache[camera_id] = entry
        logger.info(f"ONVIF connected for {camera_id} at {host}:{port}, profile token: {token}")
        return entry
    except Exception as e:
        logger.error(f"ONVIF connection failed for {camera_id} at {host}:{port}: {e}")
        raise


# Direction → (pan_velocity, tilt_velocity)
_PTZ_DIRECTIONS = {
    'left':       (-1.0,  0.0),
    'right':      ( 1.0,  0.0),
    'up':         ( 0.0,  1.0),
    'down':       ( 0.0, -1.0),
    'up-left':    (-1.0,  1.0),
    'up-right':   ( 1.0,  1.0),
    'down-left':  (-1.0, -1.0),
    'down-right': ( 1.0, -1.0),
}


@camera_sync_bp.route('/api/camera-sync/ptz/move', methods=['POST'])
def ptz_move():
    """Start continuous PTZ movement in a direction."""
    try:
        data = request.json or {}
        camera_id = data.get('camera_id', '').strip()
        direction = data.get('direction', '').strip().lower()
        speed = float(data.get('speed', 0.8))

        if not camera_id:
            return jsonify({'success': False, 'error': 'camera_id required'}), 400
        if direction not in _PTZ_DIRECTIONS:
            return jsonify({'success': False,
                            'error': f'Invalid direction. Use: {", ".join(_PTZ_DIRECTIONS)}'}), 400

        onvif = _get_onvif(camera_id)
        ptz = onvif['ptz']
        token = onvif['token']

        pan_v, tilt_v = _PTZ_DIRECTIONS[direction]
        pan_v *= speed
        tilt_v *= speed

        ptz.ContinuousMove({
            'ProfileToken': token,
            'Velocity': {'PanTilt': {'x': pan_v, 'y': tilt_v}}
        })
        logger.info(f"PTZ move {camera_id}: {direction} (pan={pan_v}, tilt={tilt_v})")
        return jsonify({'success': True, 'direction': direction})

    except Exception as e:
        logger.error(f"PTZ move error: {e}")
        # Clear cache on connection errors so next attempt reconnects
        with _onvif_lock:
            _onvif_cache.pop(camera_id, None)
        return jsonify({'success': False, 'error': str(e)}), 502


@camera_sync_bp.route('/api/camera-sync/ptz/stop', methods=['POST'])
def ptz_stop():
    """Stop all PTZ movement."""
    try:
        data = request.json or {}
        camera_id = data.get('camera_id', '').strip()
        if not camera_id:
            return jsonify({'success': False, 'error': 'camera_id required'}), 400

        onvif = _get_onvif(camera_id)
        ptz = onvif['ptz']
        token = onvif['token']

        ptz.Stop({'ProfileToken': token, 'PanTilt': True, 'Zoom': True})

        # Lock position: read current pos and send AbsoluteMove to prevent
        # firmware-level auto-return / patrol overriding the stop.
        try:
            time.sleep(0.3)  # let camera settle
            status = ptz.GetStatus({'ProfileToken': token})
            cur_pan = status.Position.PanTilt.x
            cur_tilt = status.Position.PanTilt.y
            cur_zoom = status.Position.Zoom.x
            ptz.AbsoluteMove({
                'ProfileToken': token,
                'Position': {
                    'PanTilt': {'x': cur_pan, 'y': cur_tilt},
                    'Zoom': {'x': cur_zoom},
                },
            })
            logger.info(f"PTZ stop+lock {camera_id}: pan={cur_pan:.4f} tilt={cur_tilt:.4f} zoom={cur_zoom:.4f}")
        except Exception as lock_err:
            logger.warning(f"PTZ position lock failed for {camera_id}: {lock_err}")

        return jsonify({'success': True})

    except Exception as e:
        logger.error(f"PTZ stop error: {e}")
        with _onvif_lock:
            _onvif_cache.pop(camera_id, None)
        return jsonify({'success': False, 'error': str(e)}), 502


@camera_sync_bp.route('/api/camera-sync/ptz/zoom', methods=['POST'])
def ptz_zoom():
    """Zoom in or out."""
    try:
        data = request.json or {}
        camera_id = data.get('camera_id', '').strip()
        direction = data.get('direction', '').strip().lower()
        speed = float(data.get('speed', 0.3))

        if not camera_id:
            return jsonify({'success': False, 'error': 'camera_id required'}), 400
        if direction not in ('in', 'out'):
            return jsonify({'success': False, 'error': 'direction must be "in" or "out"'}), 400

        onvif = _get_onvif(camera_id)
        ptz = onvif['ptz']
        token = onvif['token']

        zoom_v = speed if direction == 'in' else -speed

        ptz.ContinuousMove({
            'ProfileToken': token,
            'Velocity': {'Zoom': {'x': zoom_v}}
        })
        logger.info(f"PTZ zoom {camera_id}: {direction} (speed={zoom_v})")
        return jsonify({'success': True, 'direction': direction})

    except Exception as e:
        logger.error(f"PTZ zoom error: {e}")
        with _onvif_lock:
            _onvif_cache.pop(camera_id, None)
        return jsonify({'success': False, 'error': str(e)}), 502


def bearing_between(lat1, lng1, lat2, lng2):
    """Compass bearing from point 1 to point 2 (degrees 0-360)."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_lam = math.radians(lng2 - lng1)
    y = math.sin(d_lam) * math.cos(phi2)
    x = (math.cos(phi1) * math.sin(phi2) -
         math.sin(phi1) * math.cos(phi2) * math.cos(d_lam))
    return (math.degrees(math.atan2(y, x)) + 360) % 360


# ---------------------------------------------------------------------------
# PTZ Position & Absolute Move
# ---------------------------------------------------------------------------

@camera_sync_bp.route('/api/camera-sync/ptz/position', methods=['GET'])
def ptz_position():
    """Read current PTZ pan/tilt/zoom via ONVIF GetStatus."""
    try:
        camera_id = request.args.get('camera_id', '').strip()
        if not camera_id:
            return jsonify({'success': False, 'error': 'camera_id required'}), 400

        onvif = _get_onvif(camera_id)
        ptz = onvif['ptz']
        token = onvif['token']

        status = ptz.GetStatus({'ProfileToken': token})
        pos = status.Position
        pan = float(pos.PanTilt.x) if pos.PanTilt else 0.0
        tilt = float(pos.PanTilt.y) if pos.PanTilt else 0.0
        zoom = float(pos.Zoom.x) if pos.Zoom else 0.0

        return jsonify({'success': True, 'pan': pan, 'tilt': tilt, 'zoom': zoom})
    except Exception as e:
        logger.error(f"PTZ position error: {e}")
        with _onvif_lock:
            _onvif_cache.pop(request.args.get('camera_id', ''), None)
        return jsonify({'success': False, 'error': str(e)}), 502


@camera_sync_bp.route('/api/camera-sync/ptz/compass-calibrate', methods=['POST'])
def ptz_compass_calibrate():
    """Calibrate PTZ compass by recording current pan position and user-supplied bearing.

    Accepts JSON: {camera_id, target_bearing}
    - target_bearing: compass bearing (0-360) to the target the camera is currently aimed at
    Computes home_bearing = target_bearing - current_pan * (pan_range / 2)
    Stores in camera_locations.ptz_home_bearing.
    """
    try:
        data = request.json or {}
        camera_id = data.get('camera_id', '').strip()
        target_bearing = data.get('target_bearing')

        if not camera_id:
            return jsonify({'success': False, 'error': 'camera_id required'}), 400
        if target_bearing is None:
            return jsonify({'success': False, 'error': 'target_bearing required'}), 400

        target_bearing = float(target_bearing) % 360

        # Read current pan position
        onvif = _get_onvif(camera_id)
        ptz = onvif['ptz']
        token = onvif['token']
        status = ptz.GetStatus({'ProfileToken': token})
        current_pan = float(status.Position.PanTilt.x) if status.Position.PanTilt else 0.0

        # Get pan range from DB
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
            cursor.execute('SELECT ptz_pan_range FROM camera_locations WHERE camera_id = %s', (camera_id,))
            row = cursor.fetchone()

        pan_range = float(row['ptz_pan_range']) if row and row.get('ptz_pan_range') else 360.0

        # Compute home bearing: heading at pan=0
        home_bearing = (target_bearing - current_pan * (pan_range / 2)) % 360

        # Store in DB
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'UPDATE camera_locations SET ptz_home_bearing = %s WHERE camera_id = %s',
                (home_bearing, camera_id)
            )
            conn.commit()

        logger.info(f"PTZ compass calibrate {camera_id}: target_bearing={target_bearing}, "
                     f"current_pan={current_pan:.4f}, pan_range={pan_range}, home_bearing={home_bearing:.2f}")

        return jsonify({
            'success': True,
            'home_bearing': round(home_bearing, 2),
            'current_pan': round(current_pan, 4),
            'target_bearing': round(target_bearing, 2),
        })

    except Exception as e:
        logger.error(f"PTZ compass calibrate error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@camera_sync_bp.route('/api/camera-sync/ptz/compass-calibrate', methods=['DELETE'])
def ptz_compass_clear():
    """Clear PTZ compass calibration for a camera."""
    try:
        camera_id = request.args.get('camera_id', '').strip()
        if not camera_id:
            return jsonify({'success': False, 'error': 'camera_id required'}), 400

        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'UPDATE camera_locations SET ptz_home_bearing = NULL WHERE camera_id = %s',
                (camera_id,)
            )
            conn.commit()

        logger.info(f"PTZ compass calibration cleared for {camera_id}")
        return jsonify({'success': True})

    except Exception as e:
        logger.error(f"PTZ compass clear error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@camera_sync_bp.route('/api/camera-sync/ptz/absolute', methods=['POST'])
def ptz_absolute():
    """Move PTZ to an absolute pan/tilt position via ONVIF AbsoluteMove."""
    try:
        data = request.json or {}
        camera_id = data.get('camera_id', '').strip()
        if not camera_id:
            return jsonify({'success': False, 'error': 'camera_id required'}), 400

        pan = float(data.get('pan', 0))
        tilt = float(data.get('tilt', 0))

        onvif = _get_onvif(camera_id)
        ptz = onvif['ptz']
        token = onvif['token']

        ptz.AbsoluteMove({
            'ProfileToken': token,
            'Position': {'PanTilt': {'x': pan, 'y': tilt}}
        })
        logger.info(f"PTZ absolute move {camera_id}: pan={pan}, tilt={tilt}")
        return jsonify({'success': True, 'pan': pan, 'tilt': tilt})

    except Exception as e:
        logger.error(f"PTZ absolute move error: {e}")
        with _onvif_lock:
            _onvif_cache.pop(data.get('camera_id', ''), None)
        return jsonify({'success': False, 'error': str(e)}), 502


@camera_sync_bp.route('/api/camera-sync/ptz/focus-bbox', methods=['POST'])
def ptz_focus_bbox():
    """Move PTZ to center on a drawn bbox and zoom to fill the frame."""
    try:
        data = request.json or {}
        camera_id = data.get('camera_id', '').strip()
        if not camera_id:
            return jsonify({'success': False, 'error': 'camera_id required'}), 400

        bbox_x = float(data.get('bbox_x', 0))
        bbox_y = float(data.get('bbox_y', 0))
        bbox_w = float(data.get('bbox_w', 1))
        bbox_h = float(data.get('bbox_h', 1))

        onvif = _get_onvif(camera_id)
        ptz = onvif['ptz']
        token = onvif['token']

        # Get current position
        status = ptz.GetStatus({'ProfileToken': token})
        pos = status.Position
        cur_pan = float(pos.PanTilt.x) if pos.PanTilt else 0.0
        cur_tilt = float(pos.PanTilt.y) if pos.PanTilt else 0.0
        cur_zoom = float(pos.Zoom.x) if pos.Zoom else 0.0

        # Bbox center offset from frame center (range -0.5 to 0.5)
        dx = (bbox_x + bbox_w / 2) - 0.5
        dy = (bbox_y + bbox_h / 2) - 0.5

        # Step 1: Pan/tilt using ContinuousMove for a calculated duration.
        # This avoids needing exact FOV-to-ONVIF mapping.
        # Speed proportional to offset magnitude, duration proportional to offset.
        magnitude = math.sqrt(dx * dx + dy * dy)

        if magnitude > 0.02:  # ignore tiny offsets
            cal = _ptz_cal_cache.get(camera_id)
            move_speed = 0.5

            if cal and cal.get('pan_units_per_sec'):
                # Use calibration data for precise movement
                # dx/dy are in frame-fraction units (-0.5 to 0.5)
                # We need to convert to ONVIF units of displacement
                # Approximate: half the frame ≈ some portion of the PTZ range
                # At zoom=0, full frame might span ~0.3 ONVIF units (varies by camera)
                # The calibration tells us units/sec at test_speed
                pan_rate = cal['pan_units_per_sec']  # ONVIF units per sec at speed 0.5
                tilt_rate = cal['tilt_units_per_sec']

                # Desired displacement in ONVIF units (rough: frame fraction → ONVIF)
                # Scale factor: at zoom=0, half frame ≈ pan_rate * 1.0s worth of movement
                zoom_scale = max(0.1, 1.0 - cur_zoom * 0.8)
                desired_pan_displacement = dx * pan_rate * 2.0 * zoom_scale
                desired_tilt_displacement = -dy * tilt_rate * 2.0 * zoom_scale

                # Compute duration to achieve desired displacement at move_speed
                if abs(dx) > abs(dy):
                    duration = abs(desired_pan_displacement) / pan_rate if pan_rate > 0 else 0.5
                else:
                    duration = abs(desired_tilt_displacement) / tilt_rate if tilt_rate > 0 else 0.5
                duration = max(0.05, min(duration, 3.0))  # clamp

                # Direction
                pan_dir = 1.0 if dx > 0 else -1.0
                tilt_dir = -1.0 if dy > 0 else 1.0  # invert Y
                pan_speed = pan_dir * move_speed * min(1.0, abs(dx) / max(abs(dy), 0.001))
                tilt_speed = tilt_dir * move_speed * min(1.0, abs(dy) / max(abs(dx), 0.001))

                logger.info(f"PTZ focus-bbox calibrated: dx={dx:.3f} dy={dy:.3f} "
                             f"duration={duration:.2f}s pan_spd={pan_speed:.2f} tilt_spd={tilt_speed:.2f}")
            else:
                # Fallback: uncalibrated guess
                pan_speed = dx / max(magnitude, 0.01) * 0.4
                tilt_speed = -dy / max(magnitude, 0.01) * 0.4
                base_duration = magnitude * 1.5
                zoom_factor = max(0.1, 1.0 - cur_zoom * 0.7)
                duration = base_duration * zoom_factor
                logger.info(f"PTZ focus-bbox uncalibrated: dx={dx:.3f} dy={dy:.3f} "
                             f"duration={duration:.2f}s")

            ptz.ContinuousMove({
                'ProfileToken': token,
                'Velocity': {'PanTilt': {'x': pan_speed, 'y': tilt_speed}}
            })
            time.sleep(duration)
            ptz.Stop({'ProfileToken': token, 'PanTilt': True, 'Zoom': True})

        # Step 2: Zoom to fill frame with bbox content
        bbox_size = max(bbox_w, bbox_h)
        if bbox_size < 0.8 and bbox_size > 0.01:
            # Target: bbox should fill ~80% of frame
            # zoom_ratio: how much more to zoom (e.g. bbox=0.25 → 4x more zoom needed)
            zoom_ratio = 0.8 / bbox_size
            zoom_steps = math.log2(zoom_ratio) * 0.12
            new_zoom = min(1.0, cur_zoom + zoom_steps)

            ptz.AbsoluteMove({
                'ProfileToken': token,
                'Position': {
                    'PanTilt': {'x': cur_pan + dx * 0.05, 'y': cur_tilt - dy * 0.05},
                    'Zoom': {'x': new_zoom}
                }
            })
        else:
            new_zoom = cur_zoom

        # Read final position
        time.sleep(0.3)
        final_status = ptz.GetStatus({'ProfileToken': token})
        final_pos = final_status.Position
        new_pan = float(final_pos.PanTilt.x) if final_pos.PanTilt else cur_pan
        new_tilt = float(final_pos.PanTilt.y) if final_pos.PanTilt else cur_tilt

        logger.info(f"PTZ focus-bbox {camera_id}: pan {cur_pan:.3f}->{new_pan:.3f}, "
                     f"tilt {cur_tilt:.3f}->{new_tilt:.3f}, zoom {cur_zoom:.3f}->{new_zoom:.3f}")
        return jsonify({
            'success': True,
            'from': {'pan': cur_pan, 'tilt': cur_tilt, 'zoom': cur_zoom},
            'to': {'pan': new_pan, 'tilt': new_tilt, 'zoom': new_zoom}
        })

    except Exception as e:
        logger.error(f"PTZ focus-bbox error: {e}")
        with _onvif_lock:
            _onvif_cache.pop(data.get('camera_id', ''), None)
        return jsonify({'success': False, 'error': str(e)}), 502


@camera_sync_bp.route('/api/camera-sync/ptz/auto-calibrate', methods=['POST'])
def ptz_auto_calibrate():
    """Comprehensive PTZ calibration: speed at multiple rates, full range, all axes.

    Measures:
    - Pan/tilt/zoom speed at 0.5, 0.8, 1.0
    - Full pan range (continuous move to limits)
    - Full tilt range
    - Full zoom range
    Takes ~90 seconds.
    """
    try:
        data = request.json or {}
        camera_id = data.get('camera_id', '').strip()
        if not camera_id:
            return jsonify({'success': False, 'error': 'camera_id required'}), 400

        onvif = _get_onvif(camera_id)
        ptz = onvif['ptz']
        token = onvif['token']

        # Read starting position
        status = ptz.GetStatus({'ProfileToken': token})
        start_pan = float(status.Position.PanTilt.x) if status.Position.PanTilt else 0.0
        start_tilt = float(status.Position.PanTilt.y) if status.Position.PanTilt else 0.0
        start_zoom = float(status.Position.Zoom.x) if status.Position.Zoom else 0.0

        settle = 0.5
        test_dur = 1.0

        def read_pos():
            s = ptz.GetStatus({'ProfileToken': token})
            return {
                'pan': float(s.Position.PanTilt.x) if s.Position.PanTilt else 0.0,
                'tilt': float(s.Position.PanTilt.y) if s.Position.PanTilt else 0.0,
                'zoom': float(s.Position.Zoom.x) if s.Position.Zoom else 0.0,
            }

        def go_home():
            ptz.AbsoluteMove({
                'ProfileToken': token,
                'Position': {
                    'PanTilt': {'x': start_pan, 'y': start_tilt},
                    'Zoom': {'x': start_zoom},
                }
            })
            time.sleep(2)

        # ==================================================================
        # PHASE 1: Full range discovery (must come first so speed tests
        #          can start from mid-range, not from a limit)
        # ==================================================================
        logger.info(f"PTZ cal {camera_id}: finding full range")
        ranges = {'pan': {}, 'tilt': {}, 'zoom': {}}

        def move_to_limit(axis, direction, max_time=25):
            """Move in a direction until position stops changing (hit limit).
            Returns the limit position value for the axis."""
            if axis == 'zoom':
                vel = {'Zoom': {'x': direction}}
            else:
                vel = {'PanTilt': {
                    'x': direction if axis == 'pan' else 0,
                    'y': direction if axis == 'tilt' else 0,
                }}
            ptz.ContinuousMove({'ProfileToken': token, 'Velocity': vel})
            # Wait for camera to start moving before polling
            time.sleep(2.0)
            prev = read_pos()[axis]
            elapsed = 0
            poll_interval = 1.0
            stall_count = 0
            while elapsed < max_time:
                time.sleep(poll_interval)
                elapsed += poll_interval
                cur = read_pos()[axis]
                logger.info(f"PTZ cal {camera_id}: {axis} dir={direction} pos={cur:.4f} prev={prev:.4f}")
                if abs(cur - prev) < 0.002:
                    stall_count += 1
                    if stall_count >= 3:  # 3s of no movement = at limit
                        break
                else:
                    stall_count = 0
                prev = cur
            ptz.Stop({'ProfileToken': token, 'PanTilt': True, 'Zoom': True})
            time.sleep(settle)
            final = read_pos()[axis]
            logger.info(f"PTZ cal {camera_id}: {axis} limit at {final:.4f}")
            return round(final, 4)

        # Pan range
        go_home()
        time.sleep(1)
        ranges['pan']['max'] = move_to_limit('pan', 1.0)
        time.sleep(1)
        ranges['pan']['min'] = move_to_limit('pan', -1.0)
        logger.info(f"PTZ cal {camera_id}: pan range {ranges['pan']}")

        go_home()
        time.sleep(1)

        # Tilt range
        ranges['tilt']['max'] = move_to_limit('tilt', 1.0)
        time.sleep(1)
        ranges['tilt']['min'] = move_to_limit('tilt', -1.0)
        logger.info(f"PTZ cal {camera_id}: tilt range {ranges['tilt']}")

        go_home()

        # Zoom range (use AbsoluteMove — more reliable for zoom)
        ptz.AbsoluteMove({'ProfileToken': token, 'Position': {'Zoom': {'x': 0.0}}})
        time.sleep(3)
        pos = read_pos()
        ranges['zoom']['min'] = round(pos['zoom'], 4)
        ptz.AbsoluteMove({'ProfileToken': token, 'Position': {'Zoom': {'x': 1.0}}})
        time.sleep(5)
        pos = read_pos()
        ranges['zoom']['max'] = round(pos['zoom'], 4)
        logger.info(f"PTZ cal {camera_id}: zoom range {ranges['zoom']}")

        go_home()

        # ==================================================================
        # PHASE 2: Speed tests — start from mid-range so both directions
        #          have room to move
        # ==================================================================
        speed_tests = []
        test_speeds = [0.5, 0.8, 1.0]

        # Detect 360° wrap: if min > max or limits are very close, it's a
        # full-rotation axis. In that case, mid-point should be opposite
        # the dead zone (at -0.5 in ONVIF coords).
        pan_range_span = ranges['pan']['max'] - ranges['pan']['min']
        if pan_range_span <= 0 or abs(pan_range_span) < 0.1:
            # 360° wrap — limits are at ~0.0, pick mid at -0.5
            pan_mid = -0.5
            ranges['pan']['full_rotation'] = True
            ranges['pan']['total_range'] = round(2.0 - abs(pan_range_span), 4)
        else:
            pan_mid = round((ranges['pan']['min'] + ranges['pan']['max']) / 2, 4)

        tilt_range_span = ranges['tilt']['max'] - ranges['tilt']['min']
        tilt_mid = round((ranges['tilt']['min'] + ranges['tilt']['max']) / 2, 4)

        def go_mid():
            ptz.AbsoluteMove({
                'ProfileToken': token,
                'Position': {
                    'PanTilt': {'x': pan_mid, 'y': tilt_mid},
                    'Zoom': {'x': 0.0},
                }
            })
            time.sleep(2)

        for spd in test_speeds:
            logger.info(f"PTZ cal {camera_id}: speed test at {spd}")

            # Pan test (both directions, use the one with more displacement)
            for pan_dir, label in [(spd, 'right'), (-spd, 'left')]:
                go_mid()
                before = read_pos()
                ptz.ContinuousMove({'ProfileToken': token, 'Velocity': {'PanTilt': {'x': pan_dir, 'y': 0}}})
                time.sleep(test_dur)
                ptz.Stop({'ProfileToken': token, 'PanTilt': True, 'Zoom': True})
                time.sleep(settle)
                after = read_pos()
                disp = after['pan'] - before['pan']
                speed_tests.append({'axis': 'pan', 'direction': label, 'speed': spd,
                                    'displacement': round(disp, 6),
                                    'units_per_sec': round(disp / test_dur, 6)})

            # Tilt test (both directions)
            for tilt_dir, label in [(spd, 'up'), (-spd, 'down')]:
                go_mid()
                before = read_pos()
                ptz.ContinuousMove({'ProfileToken': token, 'Velocity': {'PanTilt': {'x': 0, 'y': tilt_dir}}})
                time.sleep(test_dur)
                ptz.Stop({'ProfileToken': token, 'PanTilt': True, 'Zoom': True})
                time.sleep(settle)
                after = read_pos()
                disp = after['tilt'] - before['tilt']
                speed_tests.append({'axis': 'tilt', 'direction': label, 'speed': spd,
                                    'displacement': round(disp, 6),
                                    'units_per_sec': round(disp / test_dur, 6)})

            # Zoom test (from 0, zoom in)
            ptz.AbsoluteMove({'ProfileToken': token, 'Position': {'Zoom': {'x': 0.0}}})
            time.sleep(2)
            before = read_pos()
            ptz.ContinuousMove({'ProfileToken': token, 'Velocity': {'Zoom': {'x': spd}}})
            time.sleep(test_dur)
            ptz.Stop({'ProfileToken': token, 'PanTilt': True, 'Zoom': True})
            time.sleep(settle)
            after = read_pos()
            speed_tests.append({'axis': 'zoom', 'direction': 'in', 'speed': spd,
                                'displacement': round(after['zoom'] - before['zoom'], 6),
                                'units_per_sec': round((after['zoom'] - before['zoom']) / test_dur, 6)})

        # Return to start
        go_home()

        # ==================================================================
        # Build summary
        # ==================================================================
        max_speeds = {}
        for axis in ['pan', 'tilt', 'zoom']:
            axis_tests = [t for t in speed_tests if t['axis'] == axis]
            if axis_tests:
                max_test = max(axis_tests, key=lambda t: abs(t['units_per_sec']))
                max_speeds[axis] = {
                    'max_speed_setting': max_test['speed'],
                    'max_units_per_sec': round(abs(max_test['units_per_sec']), 6),
                }
                # Also compute best at 0.5 across all directions
                half_tests = [t for t in axis_tests if t['speed'] == 0.5]
                if half_tests:
                    best_half = max(half_tests, key=lambda t: abs(t['units_per_sec']))
                    max_speeds[axis]['units_per_sec_at_0.5'] = round(abs(best_half['units_per_sec']), 6)

        cal_data = {
            'camera_id': camera_id,
            'start_position': {'pan': start_pan, 'tilt': start_tilt, 'zoom': start_zoom},
            'speed_tests': speed_tests,
            'ranges': ranges,
            'max_speeds': max_speeds,
        }

        # Cache for use by focus-bbox and targeting
        _ptz_cal_cache[camera_id] = {
            **cal_data,
            'pan_units_per_sec': max_speeds.get('pan', {}).get('units_per_sec_at_0.5', 0),
            'tilt_units_per_sec': max_speeds.get('tilt', {}).get('units_per_sec_at_0.5', 0),
            'zoom_units_per_sec': max_speeds.get('zoom', {}).get('units_per_sec_at_0.5', 0),
        }

        logger.info(f"PTZ auto-calibrate {camera_id}: ranges={ranges} max_speeds={max_speeds}")

        # Persist travel limits to DB for instant limit detection on frontend
        try:
            import json as _json
            limits = {
                'pan': {'min': ranges['pan'].get('min'), 'max': ranges['pan'].get('max')},
                'tilt': {'min': ranges['tilt'].get('min'), 'max': ranges['tilt'].get('max')},
                'zoom': {'min': ranges['zoom'].get('min'), 'max': ranges['zoom'].get('max')},
            }
            with get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    'UPDATE camera_locations SET ptz_travel_limits = %s WHERE camera_id = %s',
                    (_json.dumps(limits), camera_id)
                )
                conn.commit()
            logger.info(f"PTZ travel limits saved for {camera_id}: {limits}")
        except Exception as save_err:
            logger.warning(f"Failed to save PTZ travel limits: {save_err}")

        return jsonify({'success': True, **cal_data})

    except Exception as e:
        logger.error(f"PTZ auto-calibrate error: {e}")
        with _onvif_lock:
            _onvif_cache.pop(data.get('camera_id', ''), None)
        return jsonify({'success': False, 'error': str(e)}), 500


@camera_sync_bp.route('/api/camera-sync/ptz/calibrate-zoom', methods=['POST'])
def ptz_calibrate_zoom():
    """Calibrate PTZ zoom: measure range, speed response, and FOV change.

    Tests zoom from min to max, measuring:
    - Zoom range (ONVIF min/max values)
    - Zoom speed (units per second at different speeds)
    - FOV ratio at each zoom level (via feature size tracking)
    """
    import cv2
    import numpy as np

    try:
        data = request.json or {}
        camera_id = data.get('camera_id', '').strip()
        if not camera_id:
            return jsonify({'success': False, 'error': 'camera_id required'}), 400

        onvif = _get_onvif(camera_id)
        ptz = onvif['ptz']
        token = onvif['token']

        def read_pos():
            s = ptz.GetStatus({'ProfileToken': token})
            return {
                'pan': float(s.Position.PanTilt.x) if s.Position.PanTilt else 0.0,
                'tilt': float(s.Position.PanTilt.y) if s.Position.PanTilt else 0.0,
                'zoom': float(s.Position.Zoom.x) if s.Position.Zoom else 0.0,
            }

        def capture_frame():
            for attempt in range(3):
                try:
                    resp = http_requests.get(
                        f'{GO2RTC_API}/api/frame.jpeg',
                        params={'src': camera_id}, timeout=8,
                    )
                    if resp.status_code == 200 and len(resp.content) > 100:
                        img = cv2.imdecode(np.frombuffer(resp.content, np.uint8), cv2.IMREAD_COLOR)
                        if img is not None:
                            return img
                except Exception:
                    pass
                time.sleep(0.5)
            # ffmpeg fallback
            try:
                import subprocess
                with get_connection() as conn:
                    cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
                    cursor.execute('SELECT onvif_host FROM camera_locations WHERE camera_id = %s', (camera_id,))
                    row = cursor.fetchone()
                cam_host = row['onvif_host'] if row else '192.168.40.18'
                rtsp_url = f'rtsp://{cam_host}:8554/{camera_id}'
                tmp_path = f'/tmp/ptz_zoom_cal_{camera_id}.jpg'
                subprocess.run([
                    'ffmpeg', '-y', '-rtsp_transport', 'tcp',
                    '-i', rtsp_url, '-frames:v', '1', '-q:v', '2',
                    '-update', '1', tmp_path
                ], capture_output=True, timeout=10)
                return cv2.imread(tmp_path)
            except Exception:
                return None

        # Save starting position
        start = read_pos()
        logger.info(f"PTZ zoom-cal {camera_id}: start zoom={start['zoom']:.4f}")

        results = {
            'camera_id': camera_id,
            'start_zoom': start['zoom'],
            'zoom_range': {},
            'speed_tests': [],
            'fov_samples': [],
        }

        # ---- Step 1: Find zoom range ----
        # Zoom all the way out (min)
        ptz.AbsoluteMove({
            'ProfileToken': token,
            'Position': {'Zoom': {'x': 0.0}},
        })
        time.sleep(3)
        min_pos = read_pos()
        results['zoom_range']['min'] = min_pos['zoom']

        # Zoom all the way in (max)
        ptz.AbsoluteMove({
            'ProfileToken': token,
            'Position': {'Zoom': {'x': 1.0}},
        })
        time.sleep(5)
        max_pos = read_pos()
        results['zoom_range']['max'] = max_pos['zoom']

        # Return to minimum zoom for speed tests
        ptz.AbsoluteMove({
            'ProfileToken': token,
            'Position': {'Zoom': {'x': 0.0}},
        })
        time.sleep(3)

        logger.info(f"PTZ zoom-cal {camera_id}: range [{min_pos['zoom']:.4f}, {max_pos['zoom']:.4f}]")

        # ---- Step 2: Zoom speed tests ----
        for test_speed in [0.3, 0.5, 0.8]:
            before = read_pos()
            ptz.ContinuousMove({
                'ProfileToken': token,
                'Velocity': {'Zoom': {'x': test_speed}},
            })
            time.sleep(1.0)
            ptz.Stop({'ProfileToken': token, 'PanTilt': True, 'Zoom': True})
            time.sleep(0.5)
            after = read_pos()
            displacement = after['zoom'] - before['zoom']

            results['speed_tests'].append({
                'speed': test_speed,
                'duration': 1.0,
                'before': round(before['zoom'], 4),
                'after': round(after['zoom'], 4),
                'displacement': round(displacement, 6),
                'units_per_sec': round(displacement / 1.0, 6),
            })

            # Return to min zoom for next test
            ptz.AbsoluteMove({
                'ProfileToken': token,
                'Position': {'Zoom': {'x': 0.0}},
            })
            time.sleep(2)

        # ---- Step 3: FOV sampling at different zoom levels ----
        # Capture frames at several zoom levels to measure feature spread
        # This measures how FOV narrows with zoom
        zoom_levels = [0.0, 0.1, 0.2, 0.3, 0.5, 0.7, 1.0]
        baseline_features = None

        for z in zoom_levels:
            ptz.AbsoluteMove({
                'ProfileToken': token,
                'Position': {
                    'PanTilt': {'x': start['pan'], 'y': start['tilt']},
                    'Zoom': {'x': z},
                },
            })
            time.sleep(2.5)

            actual = read_pos()
            frame = capture_frame()
            sample = {
                'target_zoom': z,
                'actual_zoom': round(actual['zoom'], 4),
                'feature_count': 0,
                'feature_spread': None,
                'avg_brightness': None,
                'fov_ratio': None,
            }

            if frame is not None:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                sample['avg_brightness'] = round(float(np.mean(gray)), 1)

                corners = cv2.goodFeaturesToTrack(
                    gray, maxCorners=50, qualityLevel=0.01, minDistance=30,
                )
                if corners is not None and len(corners) >= 2:
                    pts = corners.reshape(-1, 2)
                    sample['feature_count'] = len(pts)

                    # Measure spread: std dev of feature positions normalized by frame size
                    h, w = gray.shape[:2]
                    norm_pts = pts / np.array([w, h])
                    spread = float(np.std(norm_pts[:, 0]) + np.std(norm_pts[:, 1]))
                    sample['feature_spread'] = round(spread, 4)

                    if baseline_features is None:
                        baseline_features = spread

                    if baseline_features > 0:
                        sample['fov_ratio'] = round(spread / baseline_features, 4)

            results['fov_samples'].append(sample)
            logger.info(f"PTZ zoom-cal {camera_id}: zoom={z:.1f} actual={actual['zoom']:.4f} "
                        f"features={sample['feature_count']} spread={sample['feature_spread']}")

        # ---- Return to starting position ----
        ptz.AbsoluteMove({
            'ProfileToken': token,
            'Position': {
                'PanTilt': {'x': start['pan'], 'y': start['tilt']},
                'Zoom': {'x': start['zoom']},
            },
        })

        # Compute summary
        speed_at_05 = 0.0
        for st in results['speed_tests']:
            if st['speed'] == 0.5:
                speed_at_05 = st['units_per_sec']
                break

        # Cache zoom calibration
        if camera_id not in _ptz_cal_cache:
            _ptz_cal_cache[camera_id] = {}
        _ptz_cal_cache[camera_id]['zoom_range'] = results['zoom_range']
        _ptz_cal_cache[camera_id]['zoom_units_per_sec'] = speed_at_05
        _ptz_cal_cache[camera_id]['fov_samples'] = results['fov_samples']

        results['success'] = True
        results['summary'] = {
            'zoom_range': results['zoom_range'],
            'zoom_speed_at_0.5': speed_at_05,
            'fov_samples_count': len([s for s in results['fov_samples'] if s['fov_ratio'] is not None]),
        }

        logger.info(f"PTZ zoom-cal {camera_id}: complete. Range={results['zoom_range']}, "
                     f"speed@0.5={speed_at_05:.4f}/s")

        return jsonify(results)

    except Exception as e:
        logger.error(f"PTZ zoom-cal error: {e}")
        with _onvif_lock:
            _onvif_cache.pop(data.get('camera_id', ''), None)
        return jsonify({'success': False, 'error': str(e)}), 500


@camera_sync_bp.route('/api/camera-sync/ptz/calibrate-visual', methods=['POST'])
def ptz_calibrate_visual():
    """Run visual PTZ calibration by moving to grid positions, capturing frames,
    finding features, centering them, and measuring pixel-to-PTZ relationships.

    Accepts: { camera_id, num_positions: 9, centering_attempts: 3 }
    Takes 1-2 minutes. Nginx timeout is 300s so this is fine.
    """
    import cv2
    import numpy as np

    try:
        data = request.json or {}
        camera_id = data.get('camera_id', '').strip()
        if not camera_id:
            return jsonify({'success': False, 'error': 'camera_id required'}), 400

        num_positions = int(data.get('num_positions', 9))
        centering_attempts = int(data.get('centering_attempts', 3))

        onvif = _get_onvif(camera_id)
        ptz = onvif['ptz']
        token = onvif['token']

        # ------------------------------------------------------------------
        # Helper: read current PTZ position
        # ------------------------------------------------------------------
        def read_pos():
            s = ptz.GetStatus({'ProfileToken': token})
            return {
                'pan': float(s.Position.PanTilt.x) if s.Position.PanTilt else 0.0,
                'tilt': float(s.Position.PanTilt.y) if s.Position.PanTilt else 0.0,
            }

        # ------------------------------------------------------------------
        # Helper: capture a frame (go2rtc with retries, ffmpeg fallback)
        # ------------------------------------------------------------------
        def capture_frame():
            # Try go2rtc up to 3 times
            for attempt in range(3):
                try:
                    resp = http_requests.get(
                        f'{GO2RTC_API}/api/frame.jpeg',
                        params={'src': camera_id},
                        timeout=8,
                    )
                    if resp.status_code == 200 and len(resp.content) > 100:
                        img = cv2.imdecode(np.frombuffer(resp.content, np.uint8), cv2.IMREAD_COLOR)
                        if img is not None:
                            return img
                except Exception:
                    pass
                time.sleep(0.5)

            # Fallback: ffmpeg single frame from RTSP
            try:
                import subprocess
                with get_connection() as conn:
                    cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
                    cursor.execute('SELECT onvif_host FROM camera_locations WHERE camera_id = %s', (camera_id,))
                    row = cursor.fetchone()
                cam_host = row['onvif_host'] if row else '192.168.40.18'
                rtsp_url = f'rtsp://{cam_host}:8554/{camera_id}'
                tmp_path = f'/tmp/ptz_cal_{camera_id}.jpg'
                subprocess.run([
                    'ffmpeg', '-y', '-rtsp_transport', 'tcp',
                    '-i', rtsp_url, '-frames:v', '1', '-q:v', '2',
                    '-update', '1', tmp_path
                ], capture_output=True, timeout=10)
                img = cv2.imread(tmp_path)
                return img
            except Exception as e:
                logger.warning(f"PTZ visual-cal: ffmpeg fallback failed: {e}")
                return None

        # ------------------------------------------------------------------
        # Read home position to return to at the end
        # ------------------------------------------------------------------
        home = read_pos()
        logger.info(f"PTZ visual-cal {camera_id}: home pan={home['pan']:.4f} tilt={home['tilt']:.4f}")

        # ------------------------------------------------------------------
        # Define grid positions (pan x tilt)
        # Focused on downward angles for surveillance camera
        # ------------------------------------------------------------------
        pan_values = [-0.5, 0.0, 0.5]
        tilt_values = [-0.3, -0.15, 0.0]
        grid = []
        for t_val in tilt_values:
            for p_val in pan_values:
                grid.append((p_val, t_val))
        # Trim to num_positions if caller requested fewer
        grid = grid[:num_positions]

        # ------------------------------------------------------------------
        # Get calibration speed data if available
        # ------------------------------------------------------------------
        cal = _ptz_cal_cache.get(camera_id)
        pan_rate = cal.get('pan_units_per_sec', 0) if cal else 0
        tilt_rate = cal.get('tilt_units_per_sec', 0) if cal else 0

        results = []

        for idx, (target_pan, target_tilt) in enumerate(grid):
            logger.info(f"PTZ visual-cal {camera_id}: grid {idx+1}/{len(grid)} "
                        f"pan={target_pan:.2f} tilt={target_tilt:.2f}")

            entry = {
                'grid_index': idx,
                'target_pan': target_pan,
                'target_tilt': target_tilt,
                'feature_pixel': None,
                'feature_offset': None,
                'ptz_before': None,
                'ptz_after': None,
                'centering_error': None,
                'match_confidence': None,
                'attempts': 0,
                'status': 'failed',
            }

            try:
                # Move to grid position
                ptz.AbsoluteMove({
                    'ProfileToken': token,
                    'Position': {'PanTilt': {'x': target_pan, 'y': target_tilt}},
                })
                time.sleep(2)

                # Capture frame
                frame = capture_frame()
                if frame is None:
                    entry['status'] = 'capture_failed'
                    logger.warning(f"PTZ visual-cal {camera_id}: grid {idx} frame capture failed")
                    results.append(entry)
                    continue

                frame_h, frame_w = frame.shape[:2]

                # Check brightness
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                avg_brightness = float(np.mean(gray))
                if avg_brightness < 10:
                    entry['status'] = 'too_dark'
                    logger.warning(f"PTZ visual-cal {camera_id}: grid {idx} too dark "
                                   f"(brightness={avg_brightness:.1f})")
                    results.append(entry)
                    continue

                # Find distinctive features
                corners = cv2.goodFeaturesToTrack(
                    gray, maxCorners=20, qualityLevel=0.01, minDistance=50,
                )
                if corners is None or len(corners) == 0:
                    entry['status'] = 'no_features'
                    logger.warning(f"PTZ visual-cal {camera_id}: grid {idx} no features found")
                    results.append(entry)
                    continue

                # Pick feature closest to center but not AT center (>5% offset)
                cx, cy = frame_w / 2.0, frame_h / 2.0
                best_feat = None
                best_dist = float('inf')
                for corner in corners:
                    fx, fy = corner.ravel()
                    offset_frac_x = abs(fx / frame_w - 0.5)
                    offset_frac_y = abs(fy / frame_h - 0.5)
                    offset_mag = math.sqrt(offset_frac_x ** 2 + offset_frac_y ** 2)
                    if offset_mag < 0.05:
                        continue  # too close to center
                    dist = math.sqrt((fx - cx) ** 2 + (fy - cy) ** 2)
                    if dist < best_dist:
                        best_dist = dist
                        best_feat = (fx, fy)

                if best_feat is None:
                    # All features are at center; pick the first one anyway
                    best_feat = tuple(corners[0].ravel())

                feat_x, feat_y = best_feat
                feat_x, feat_y = float(feat_x), float(feat_y)

                # Extract 64x64 template patch
                half = 32
                y1 = max(0, int(feat_y) - half)
                y2 = min(frame_h, int(feat_y) + half)
                x1 = max(0, int(feat_x) - half)
                x2 = min(frame_w, int(feat_x) + half)
                template = gray[y1:y2, x1:x2].copy()

                if template.size == 0:
                    entry['status'] = 'template_failed'
                    results.append(entry)
                    continue

                # Record feature pixel position and PTZ coordinates before centering
                ptz_before = read_pos()
                dx = (feat_x / frame_w) - 0.5
                dy = (feat_y / frame_h) - 0.5

                entry['feature_pixel'] = [round(feat_x, 1), round(feat_y, 1)]
                entry['feature_offset'] = [round(dx, 4), round(dy, 4)]
                entry['ptz_before'] = ptz_before

                # ----------------------------------------------------------
                # Centering loop: move feature toward frame center
                # ----------------------------------------------------------
                centering_error = math.sqrt(dx ** 2 + dy ** 2)
                match_conf = 0.0
                attempts_used = 0

                for attempt in range(centering_attempts):
                    attempts_used = attempt + 1

                    if centering_error <= 0.10:
                        break  # good enough

                    # Calculate movement duration from calibration or fallback
                    if pan_rate > 0 and tilt_rate > 0:
                        pan_duration = abs(dx) * 2.0 / pan_rate * 0.5
                        tilt_duration = abs(dy) * 2.0 / tilt_rate * 0.5
                        move_duration = max(pan_duration, tilt_duration)
                        move_duration = max(0.05, min(move_duration, 3.0))
                        move_speed = 0.5
                    else:
                        move_speed = 0.3
                        move_duration = max(abs(dx), abs(dy)) * 2.0
                        move_duration = max(0.1, min(move_duration, 3.0))

                    # Pan: positive dx means feature is right of center, move right
                    # Tilt: positive dy means feature is below center, move down (negative tilt)
                    pan_dir = 1.0 if dx > 0 else -1.0
                    tilt_dir = -1.0 if dy > 0 else 1.0

                    pan_spd = pan_dir * move_speed * min(1.0, abs(dx) / max(abs(dy), 0.001))
                    tilt_spd = tilt_dir * move_speed * min(1.0, abs(dy) / max(abs(dx), 0.001))

                    # Clamp speeds
                    pan_spd = max(-1.0, min(1.0, pan_spd))
                    tilt_spd = max(-1.0, min(1.0, tilt_spd))

                    ptz.ContinuousMove({
                        'ProfileToken': token,
                        'Velocity': {'PanTilt': {'x': pan_spd, 'y': tilt_spd}},
                    })
                    time.sleep(move_duration)
                    ptz.Stop({'ProfileToken': token, 'PanTilt': True, 'Zoom': True})

                    # Wait for settle, capture new frame
                    time.sleep(1)
                    new_frame = capture_frame()
                    if new_frame is None:
                        break

                    new_gray = cv2.cvtColor(new_frame, cv2.COLOR_BGR2GRAY)

                    # Template match to find where the feature ended up
                    result = cv2.matchTemplate(new_gray, template, cv2.TM_CCOEFF_NORMED)
                    _, max_val, _, max_loc = cv2.minMaxLoc(result)
                    match_conf = float(max_val)

                    if match_conf < 0.3:
                        # Template lost — cannot center reliably
                        break

                    # Where did the feature end up?
                    matched_x = max_loc[0] + template.shape[1] / 2.0
                    matched_y = max_loc[1] + template.shape[0] / 2.0
                    new_h, new_w = new_gray.shape[:2]

                    dx = (matched_x / new_w) - 0.5
                    dy = (matched_y / new_h) - 0.5
                    centering_error = math.sqrt(dx ** 2 + dy ** 2)

                # Read final PTZ position
                ptz_after = read_pos()
                entry['ptz_after'] = ptz_after
                entry['centering_error'] = round(centering_error, 4)
                entry['match_confidence'] = round(match_conf, 4)
                entry['attempts'] = attempts_used

                if centering_error <= 0.10:
                    entry['status'] = 'success'
                elif match_conf >= 0.3:
                    entry['status'] = 'partial'
                else:
                    entry['status'] = 'failed'

                logger.info(f"PTZ visual-cal {camera_id}: grid {idx} {entry['status']} "
                            f"err={centering_error:.4f} conf={match_conf:.4f} "
                            f"attempts={attempts_used}")

                # ----------------------------------------------------------
                # Round-trip verification: move away, return, verify visually
                # ----------------------------------------------------------
                if entry['status'] in ('success', 'partial') and template.size > 0:
                    pos_a = read_pos()  # centered position
                    # Move to an "away" position (offset by +0.3 pan, +0.15 tilt)
                    away_pan = pos_a['pan'] + 0.3
                    away_tilt = pos_a['tilt'] + 0.15
                    ptz.AbsoluteMove({
                        'ProfileToken': token,
                        'Position': {'PanTilt': {'x': away_pan, 'y': away_tilt}},
                    })
                    time.sleep(2)

                    # Return to position A
                    ptz.AbsoluteMove({
                        'ProfileToken': token,
                        'Position': {'PanTilt': {'x': pos_a['pan'], 'y': pos_a['tilt']}},
                    })
                    time.sleep(2)

                    # Verify: capture frame and template match
                    verify_frame = capture_frame()
                    return_error = None
                    return_conf = 0.0
                    if verify_frame is not None:
                        verify_gray = cv2.cvtColor(verify_frame, cv2.COLOR_BGR2GRAY)
                        vr = cv2.matchTemplate(verify_gray, template, cv2.TM_CCOEFF_NORMED)
                        _, v_max, _, v_loc = cv2.minMaxLoc(vr)
                        return_conf = float(v_max)
                        if return_conf >= 0.3:
                            v_cx = v_loc[0] + template.shape[1] / 2.0
                            v_cy = v_loc[1] + template.shape[0] / 2.0
                            vh, vw = verify_gray.shape[:2]
                            v_dx = (v_cx / vw) - 0.5
                            v_dy = (v_cy / vh) - 0.5
                            return_error = round(math.sqrt(v_dx ** 2 + v_dy ** 2), 4)

                    return_pos = read_pos()
                    position_drift = round(math.sqrt(
                        (return_pos['pan'] - pos_a['pan']) ** 2 +
                        (return_pos['tilt'] - pos_a['tilt']) ** 2
                    ), 6)

                    entry['roundtrip'] = {
                        'away_position': {'pan': round(away_pan, 4), 'tilt': round(away_tilt, 4)},
                        'return_position': return_pos,
                        'target_position': pos_a,
                        'position_drift': position_drift,
                        'visual_return_error': return_error,
                        'return_match_confidence': round(return_conf, 4),
                        'visual_confirmed': return_error is not None and return_error < 0.15,
                    }

                    logger.info(f"PTZ visual-cal {camera_id}: grid {idx} roundtrip "
                                f"drift={position_drift:.6f} visual_err={return_error} "
                                f"conf={return_conf:.4f}")

            except Exception as e:
                entry['status'] = 'error'
                entry['error'] = str(e)
                logger.error(f"PTZ visual-cal {camera_id}: grid {idx} error: {e}")

            results.append(entry)

        # ------------------------------------------------------------------
        # Return to home position
        # ------------------------------------------------------------------
        try:
            ptz.AbsoluteMove({
                'ProfileToken': token,
                'Position': {'PanTilt': {'x': home['pan'], 'y': home['tilt']}},
            })
            logger.info(f"PTZ visual-cal {camera_id}: returned to home position")
        except Exception as e:
            logger.error(f"PTZ visual-cal {camera_id}: failed to return home: {e}")

        # ------------------------------------------------------------------
        # Store results in calibration cache
        # ------------------------------------------------------------------
        if camera_id not in _ptz_cal_cache:
            _ptz_cal_cache[camera_id] = {}
        _ptz_cal_cache[camera_id]['visual_calibration'] = results

        # ------------------------------------------------------------------
        # Build summary
        # ------------------------------------------------------------------
        successful = [r for r in results if r['status'] == 'success']
        partial = [r for r in results if r['status'] == 'partial']
        ok_results = successful + partial

        avg_centering_error = 0.0
        avg_match_confidence = 0.0
        if ok_results:
            avg_centering_error = sum(r['centering_error'] for r in ok_results) / len(ok_results)
            avg_match_confidence = sum(r['match_confidence'] for r in ok_results) / len(ok_results)

        # Round-trip stats
        roundtrip_results = [r for r in results if r.get('roundtrip')]
        confirmed = [r for r in roundtrip_results if r['roundtrip'].get('visual_confirmed')]
        avg_drift = 0.0
        avg_return_error = 0.0
        if roundtrip_results:
            avg_drift = sum(r['roundtrip']['position_drift'] for r in roundtrip_results) / len(roundtrip_results)
            errs = [r['roundtrip']['visual_return_error'] for r in roundtrip_results
                    if r['roundtrip']['visual_return_error'] is not None]
            if errs:
                avg_return_error = sum(errs) / len(errs)

        return jsonify({
            'success': True,
            'camera_id': camera_id,
            'positions_tested': len(results),
            'positions_successful': len(successful) + len(partial),
            'positions_failed': len(results) - len(successful) - len(partial),
            'results': results,
            'summary': {
                'avg_centering_error': round(avg_centering_error, 4),
                'avg_match_confidence': round(avg_match_confidence, 4),
                'successful_centerings': len(successful),
                'roundtrips_tested': len(roundtrip_results),
                'roundtrips_confirmed': len(confirmed),
                'avg_position_drift': round(avg_drift, 6),
                'avg_visual_return_error': round(avg_return_error, 4),
            },
        })

    except Exception as e:
        logger.error(f"PTZ visual-cal error: {e}")
        with _onvif_lock:
            _onvif_cache.pop(data.get('camera_id', ''), None)
        return jsonify({'success': False, 'error': str(e)}), 500


# ---------------------------------------------------------------------------
# PTZ Targeting Engine
# ---------------------------------------------------------------------------

@camera_sync_bp.route('/api/camera-sync/ptz/target', methods=['POST'])
def ptz_target():
    """Compute PTZ aim from a bbox on a source camera and move PTZ there.

    Uses calibrated RBF interpolation if >= 3 reference points exist,
    otherwise falls back to geometry estimate.
    """
    try:
        data = request.json or {}
        source_camera_id = data.get('source_camera_id', '').strip()
        target_camera_id = data.get('target_camera_id', '').strip()
        bbox_x = float(data.get('bbox_x', 0.5))
        bbox_y = float(data.get('bbox_y', 0.5))

        if not source_camera_id or not target_camera_id:
            return jsonify({'success': False,
                            'error': 'source_camera_id and target_camera_id required'}), 400

        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)

            # Fetch both cameras
            cursor.execute(
                'SELECT * FROM camera_locations WHERE camera_id IN (%s, %s)',
                (source_camera_id, target_camera_id)
            )
            cams = {row['camera_id']: dict(row) for row in cursor.fetchall()}

            if source_camera_id not in cams or target_camera_id not in cams:
                return jsonify({'success': False,
                                'error': 'One or both cameras not found in camera_locations'}), 404

            src = cams[source_camera_id]
            tgt = cams[target_camera_id]

            # Fetch existing calibration points
            cursor.execute('''
                SELECT source_bbox_x, source_bbox_y, actual_pan, actual_tilt,
                       estimated_pan, estimated_tilt
                FROM ptz_calibration_points
                WHERE source_camera_id = %s AND target_camera_id = %s
                ORDER BY created_date
            ''', (source_camera_id, target_camera_id))
            cal_points = [dict(r) for r in cursor.fetchall()]

        method = 'geometry'
        estimated_pan = 0.0
        estimated_tilt = 0.0

        # --- Geometry estimate (always compute for reference) ---
        src_bearing = float(src['bearing'] or 0)
        src_fov = float(src['fov_angle'] or 90)
        src_range = float(src['fov_range'] or 30)
        src_lat = float(src['latitude'])
        src_lng = float(src['longitude'])
        tgt_lat = float(tgt['latitude'])
        tgt_lng = float(tgt['longitude'])
        tgt_bearing = float(tgt['bearing'] or 0)

        horizontal_offset = (bbox_x - 0.5) * src_fov
        bearing_to_object = src_bearing + horizontal_offset
        distance = src_range * max(0.1, 1.0 - bbox_y)

        world_lat, world_lng = destination_point(src_lat, src_lng, distance, bearing_to_object)
        ptz_bearing = bearing_between(tgt_lat, tgt_lng, world_lat, world_lng)

        # Normalize to ONVIF range: bearing relative to PTZ home → -1.0 to 1.0
        relative_bearing = ptz_bearing - tgt_bearing
        # Wrap to -180..180
        relative_bearing = ((relative_bearing + 180) % 360) - 180
        ptz_pan_range = float(tgt.get('ptz_pan_range') or 180)
        geo_pan = max(-1.0, min(1.0, relative_bearing / (ptz_pan_range / 2)))
        geo_tilt = 0.0  # no elevation data

        estimated_pan = geo_pan
        estimated_tilt = geo_tilt

        # --- Calibrated estimate (RBF interpolation) ---
        if len(cal_points) >= 3:
            try:
                from scipy.interpolate import RBFInterpolator
                import numpy as np

                source_coords = np.array([[p['source_bbox_x'], p['source_bbox_y']]
                                          for p in cal_points])
                pan_values = np.array([p['actual_pan'] for p in cal_points])
                tilt_values = np.array([p['actual_tilt'] for p in cal_points])

                pan_interp = RBFInterpolator(source_coords, pan_values)
                tilt_interp = RBFInterpolator(source_coords, tilt_values)

                query = np.array([[bbox_x, bbox_y]])
                estimated_pan = float(pan_interp(query)[0])
                estimated_tilt = float(tilt_interp(query)[0])
                method = 'calibrated'
            except Exception as e:
                logger.warning(f"RBF interpolation failed, falling back to geometry: {e}")
                # Keep geometry estimates

        # Move PTZ to computed position
        try:
            onvif = _get_onvif(target_camera_id)
            ptz = onvif['ptz']
            token = onvif['token']

            ptz.AbsoluteMove({
                'ProfileToken': token,
                'Position': {'PanTilt': {'x': estimated_pan, 'y': estimated_tilt}}
            })
        except Exception as e:
            logger.error(f"PTZ absolute move during targeting failed: {e}")
            with _onvif_lock:
                _onvif_cache.pop(target_camera_id, None)
            # Still return the estimates even if move failed
            return jsonify({
                'success': True,
                'estimated_pan': estimated_pan,
                'estimated_tilt': estimated_tilt,
                'geo_pan': geo_pan,
                'geo_tilt': geo_tilt,
                'method': method,
                'calibration_points': len(cal_points),
                'move_error': str(e)
            })

        return jsonify({
            'success': True,
            'estimated_pan': estimated_pan,
            'estimated_tilt': estimated_tilt,
            'geo_pan': geo_pan,
            'geo_tilt': geo_tilt,
            'method': method,
            'calibration_points': len(cal_points)
        })

    except Exception as e:
        logger.error(f"PTZ target error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ---------------------------------------------------------------------------
# PTZ Calibration CRUD
# ---------------------------------------------------------------------------

@camera_sync_bp.route('/api/camera-sync/ptz/calibrate', methods=['POST'])
def ptz_calibrate():
    """Save a confirmed calibration reference point.

    Reads the current PTZ position via GetStatus and stores it as actual_pan/tilt.
    """
    try:
        data = request.json or {}
        source_camera_id = data.get('source_camera_id', '').strip()
        target_camera_id = data.get('target_camera_id', '').strip()
        source_bbox_x = float(data.get('source_bbox_x', 0))
        source_bbox_y = float(data.get('source_bbox_y', 0))
        estimated_pan = data.get('estimated_pan')
        estimated_tilt = data.get('estimated_tilt')
        label = data.get('label', '').strip() or None

        if not source_camera_id or not target_camera_id:
            return jsonify({'success': False,
                            'error': 'source_camera_id and target_camera_id required'}), 400

        # Read current PTZ position as the confirmed actual position
        onvif = _get_onvif(target_camera_id)
        ptz = onvif['ptz']
        token = onvif['token']

        status = ptz.GetStatus({'ProfileToken': token})
        pos = status.Position
        actual_pan = float(pos.PanTilt.x) if pos.PanTilt else 0.0
        actual_tilt = float(pos.PanTilt.y) if pos.PanTilt else 0.0

        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
            cursor.execute('''
                INSERT INTO ptz_calibration_points
                    (source_camera_id, target_camera_id, source_bbox_x, source_bbox_y,
                     estimated_pan, estimated_tilt, actual_pan, actual_tilt, label)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
            ''', (source_camera_id, target_camera_id, source_bbox_x, source_bbox_y,
                  estimated_pan, estimated_tilt, actual_pan, actual_tilt, label))
            point = dict(cursor.fetchone())
            conn.commit()

        return jsonify({'success': True, 'point': point})

    except Exception as e:
        logger.error(f"PTZ calibrate error: {e}")
        if 'target_camera_id' in dir():
            with _onvif_lock:
                _onvif_cache.pop(target_camera_id, None)
        return jsonify({'success': False, 'error': str(e)}), 500


@camera_sync_bp.route('/api/camera-sync/ptz/calibration', methods=['GET'])
def ptz_calibration_status():
    """Get calibration status and points for a camera pair."""
    try:
        source_camera_id = request.args.get('source_camera_id', '').strip()
        target_camera_id = request.args.get('target_camera_id', '').strip()

        if not source_camera_id or not target_camera_id:
            return jsonify({'success': False,
                            'error': 'source_camera_id and target_camera_id required'}), 400

        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
            cursor.execute('''
                SELECT *, error_pan, error_tilt
                FROM ptz_calibration_points
                WHERE source_camera_id = %s AND target_camera_id = %s
                ORDER BY created_date DESC
            ''', (source_camera_id, target_camera_id))
            points = [dict(r) for r in cursor.fetchall()]

        count = len(points)
        avg_error_pan = 0.0
        avg_error_tilt = 0.0
        if count > 0:
            errors_pan = [p['error_pan'] for p in points if p['error_pan'] is not None]
            errors_tilt = [p['error_tilt'] for p in points if p['error_tilt'] is not None]
            if errors_pan:
                avg_error_pan = sum(abs(e) for e in errors_pan) / len(errors_pan)
            if errors_tilt:
                avg_error_tilt = sum(abs(e) for e in errors_tilt) / len(errors_tilt)

        # Coverage: estimate how well the points span the source FOV (0.0-1.0)
        coverage = 0.0
        if count >= 2:
            xs = [p['source_bbox_x'] for p in points]
            ys = [p['source_bbox_y'] for p in points]
            x_span = max(xs) - min(xs)
            y_span = max(ys) - min(ys)
            coverage = min(1.0, (x_span * y_span) * 4)  # scale up since full coverage ≈ 0.25 area

        return jsonify({
            'success': True,
            'points': points,
            'count': count,
            'avg_error_pan': round(avg_error_pan, 4),
            'avg_error_tilt': round(avg_error_tilt, 4),
            'coverage': round(coverage, 2)
        })

    except Exception as e:
        logger.error(f"PTZ calibration status error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@camera_sync_bp.route('/api/camera-sync/ptz/calibration/<int:point_id>', methods=['DELETE'])
def ptz_calibration_delete(point_id):
    """Delete a calibration reference point."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM ptz_calibration_points WHERE id = %s', (point_id,))
            deleted = cursor.rowcount > 0
            conn.commit()

        return jsonify({'success': deleted})
    except Exception as e:
        logger.error(f"PTZ calibration delete error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
