"""
Crossing-Line Spatial-Temporal Matching Engine

Matches vehicles across cameras using paired crossing lines drawn in the UI.
Each crossing line defines a spatial gate on a camera view; paired lines on
different cameras represent the same physical boundary seen from two angles.

Scoring combines:
1. Lane proximity   (0.50) -- projection along the crossing line
2. Temporal gap     (0.35) -- departure-to-arrival vs learned topology
3. Size similarity  (0.15) -- bbox area ratio

Direction of travel is a HARD FILTER: if both tracks have known direction
(from Frigate path_data or multi-frame bbox movement) and they disagree,
the pair is rejected outright.

This module is intended to run BEFORE the ReID-based CrossCameraMatcher so
that high-confidence spatial matches are locked in first.
"""

import logging
import os

import cv2
import numpy as np

from db_connection import get_cursor

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Scoring weights
# ---------------------------------------------------------------------------
LANE_WEIGHT = 0.50
TEMPORAL_WEIGHT = 0.35
SIZE_WEIGHT = 0.15

MATCH_THRESHOLD = 0.55


class CrossingLineMatcher:
    """Matches entity tracks across cameras using paired crossing lines."""

    def __init__(self):
        self._topology_cache = {}

    # ------------------------------------------------------------------
    # Topology (cached)
    # ------------------------------------------------------------------

    def _get_topology(self, camera_a, camera_b):
        """Get learned transit time between two cameras (cached)."""
        key = (camera_a, camera_b)
        if key not in self._topology_cache:
            with get_cursor(commit=False) as cursor:
                cursor.execute("""
                    SELECT min_transit_seconds, max_transit_seconds,
                           avg_transit_seconds
                    FROM camera_topology_learned
                    WHERE camera_a = %s AND camera_b = %s
                """, (camera_a, camera_b))
                row = cursor.fetchone()
                self._topology_cache[key] = dict(row) if row else None
        return self._topology_cache[key]

    # ------------------------------------------------------------------
    # Crossing-line queries
    # ------------------------------------------------------------------

    def get_paired_crossing_lines(self):
        """Return all crossing-line pairs where both sides are linked.

        Returns:
            list of (line_a, line_b) dicts.  line_a.paired_line_id == line_b.id
            and vice-versa.  Each pair appears once (lower id first).
        """
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT a.id         AS a_id,
                       a.camera_id  AS a_camera_id,
                       a.line_name  AS a_line_name,
                       a.x1 AS a_x1, a.y1 AS a_y1,
                       a.x2 AS a_x2, a.y2 AS a_y2,
                       a.forward_dx AS a_forward_dx,
                       a.forward_dy AS a_forward_dy,
                       a.lane_mapping_reversed AS a_lane_mapping_reversed,
                       b.id         AS b_id,
                       b.camera_id  AS b_camera_id,
                       b.line_name  AS b_line_name,
                       b.x1 AS b_x1, b.y1 AS b_y1,
                       b.x2 AS b_x2, b.y2 AS b_y2,
                       b.forward_dx AS b_forward_dx,
                       b.forward_dy AS b_forward_dy,
                       b.lane_mapping_reversed AS b_lane_mapping_reversed
                FROM camera_crossing_lines a
                JOIN camera_crossing_lines b ON a.paired_line_id = b.id
                WHERE a.paired_line_id IS NOT NULL
                  AND a.id < b.id
            """)
            rows = cursor.fetchall()

        pairs = []
        for r in rows:
            line_a = {
                'id': r['a_id'], 'camera_id': r['a_camera_id'],
                'line_name': r['a_line_name'],
                'x1': r['a_x1'], 'y1': r['a_y1'],
                'x2': r['a_x2'], 'y2': r['a_y2'],
                'forward_dx': r['a_forward_dx'],
                'forward_dy': r['a_forward_dy'],
                'lane_mapping_reversed': r['a_lane_mapping_reversed'],
            }
            line_b = {
                'id': r['b_id'], 'camera_id': r['b_camera_id'],
                'line_name': r['b_line_name'],
                'x1': r['b_x1'], 'y1': r['b_y1'],
                'x2': r['b_x2'], 'y2': r['b_y2'],
                'forward_dx': r['b_forward_dx'],
                'forward_dy': r['b_forward_dy'],
                'lane_mapping_reversed': r['b_lane_mapping_reversed'],
            }
            pairs.append((line_a, line_b))

        logger.info("Found %d paired crossing-line pair(s)", len(pairs))
        return pairs

    # ------------------------------------------------------------------
    # Track retrieval (mirrors CrossCameraMatcher pattern)
    # ------------------------------------------------------------------

    def get_approved_tracks(self, camera_id, entity_type='vehicle'):
        """Get approved/conflict tracks for a camera, ordered by first_seen."""
        scenario = (
            'vehicle_detection' if entity_type == 'vehicle'
            else entity_type + '_detection'
        )
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT t.id, t.camera_id, t.scenario, t.member_count,
                       t.bbox_centroid_x, t.bbox_centroid_y,
                       t.avg_bbox_width, t.avg_bbox_height,
                       t.anchor_status, t.anchor_classification,
                       t.first_seen, t.last_seen,
                       t.cross_camera_identity_id
                FROM camera_object_tracks t
                WHERE t.camera_id = %s
                  AND t.scenario = %s
                  AND t.anchor_status IN ('approved', 'conflict')
                  AND (t.first_seen IS NOT NULL AND t.first_seen > 0)
                ORDER BY t.first_seen
            """, (camera_id, scenario))
            return [dict(r) for r in cursor.fetchall()]

    # ------------------------------------------------------------------
    # Geometric helpers
    # ------------------------------------------------------------------

    @staticmethod
    def compute_lane_position(bbox_cx, bbox_cy, line):
        """Project bbox centroid onto the crossing line.

        Returns a 0.0-1.0 parameter along the line segment, clamped.
        0.0 = at (x1,y1), 1.0 = at (x2,y2).
        """
        dx = line['x2'] - line['x1']
        dy = line['y2'] - line['y1']
        length_sq = dx * dx + dy * dy
        if length_sq == 0:
            return 0.5
        t = ((bbox_cx - line['x1']) * dx + (bbox_cy - line['y1']) * dy) / length_sq
        return max(0.0, min(1.0, t))

    # ------------------------------------------------------------------
    # Direction helpers
    # ------------------------------------------------------------------

    def compute_direction(self, track):
        """Compute normalised travel direction for a track.

        Tries these sources in order:
        1. Frigate path_data from the video's metadata (most common — 86%
           of Frigate events have meaningful path displacement).
        2. Multi-frame bbox centroid movement from ai_predictions (works
           for tracks with member_count > 1).

        Returns:
            (dx, dy) normalised direction vector, or None if undetermined.
        """
        # --- Source 1: Frigate path_data from video metadata ---
        direction = self._direction_from_path_data(track)
        if direction is not None:
            return direction

        # --- Source 2: multi-member bbox movement ---
        if (track.get('member_count') or 1) <= 1:
            return None

        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT bbox_x + bbox_width / 2.0  AS cx,
                       bbox_y + bbox_height / 2.0  AS cy
                FROM ai_predictions
                WHERE camera_object_track_id = %s
                ORDER BY timestamp ASC
            """, (track['id'],))
            rows = cursor.fetchall()

        if len(rows) < 2:
            return None

        first = rows[0]
        last = rows[-1]
        dx = float(last['cx'] - first['cx'])
        dy = float(last['cy'] - first['cy'])
        length = np.sqrt(dx * dx + dy * dy)

        # Require meaningful displacement — at least 5% of frame diagonal.
        # Parked cars with multi-member tracks have tiny jitter (~30-50px
        # over hours) that should not be treated as direction of travel.
        min_displacement = 0.05 * np.sqrt(1920 ** 2 + 1080 ** 2)  # ~110px
        if length < min_displacement:
            return None
        return (dx / length, dy / length)

    def _get_path_time(self, track):
        """Get the midpoint timestamp from Frigate path_data.

        This is far more accurate than first_seen/last_seen which are often
        stored as the same rounded epoch value for all tracks in a batch.

        Returns:
            (start_time, mid_time, end_time) tuple, or None if no path_data.
        """
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT v.metadata
                FROM ai_predictions p
                JOIN videos v ON v.id = p.video_id
                WHERE p.camera_object_track_id = %s
                  AND v.metadata IS NOT NULL
                LIMIT 1
            """, (track['id'],))
            row = cursor.fetchone()

        if row is None:
            return None

        metadata = row['metadata']
        if not isinstance(metadata, dict):
            return None

        path_data = metadata.get('path_data')
        if not path_data or not isinstance(path_data, list) or len(path_data) < 1:
            return None

        try:
            # path_data format: [[[cx, cy], timestamp], ...]
            first_entry = path_data[0]
            last_entry = path_data[-1]

            if isinstance(first_entry[0], (list, tuple)):
                t_start = first_entry[1]
                t_end = last_entry[1]
            else:
                # Flat format — no timestamps available
                return None
        except (IndexError, TypeError):
            return None

        if not isinstance(t_start, (int, float)) or not isinstance(t_end, (int, float)):
            return None

        t_mid = (t_start + t_end) / 2.0
        return (t_start, t_mid, t_end)

    def _direction_from_path_data(self, track):
        """Extract travel direction from Frigate path_data stored in video metadata.

        path_data is a list of [[cx, cy], timestamp] entries in normalised
        coordinates (0-1).  We compute direction from first to last centroid.

        The chain is: track → ai_predictions → video → metadata.path_data

        Returns:
            (dx, dy) normalised direction vector in pixel-space (converted
            from normalised coords using the video dimensions), or None.
        """
        with get_cursor(commit=False) as cursor:
            # Get the video associated with this track's anchor prediction
            cursor.execute("""
                SELECT DISTINCT v.metadata, v.width, v.height
                FROM ai_predictions p
                JOIN videos v ON v.id = p.video_id
                WHERE p.camera_object_track_id = %s
                  AND v.metadata IS NOT NULL
                LIMIT 1
            """, (track['id'],))
            row = cursor.fetchone()

        if row is None:
            return None

        metadata = row['metadata']
        if not isinstance(metadata, dict):
            return None

        path_data = metadata.get('path_data')
        if not path_data or not isinstance(path_data, list) or len(path_data) < 2:
            return None

        # path_data format: [[cx, cy], timestamp] — normalised 0-1 coords
        # Extract first and last centroids
        try:
            first_entry = path_data[0]
            last_entry = path_data[-1]

            # Each entry is [cx, cy] or [[cx, cy], timestamp]
            if isinstance(first_entry[0], (list, tuple)):
                first_cx, first_cy = first_entry[0][0], first_entry[0][1]
                last_cx, last_cy = last_entry[0][0], last_entry[0][1]
                t_start = first_entry[1]
                t_end = last_entry[1]
            else:
                first_cx, first_cy = first_entry[0], first_entry[1]
                last_cx, last_cy = last_entry[0], last_entry[1]
                t_start = t_end = 0
        except (IndexError, TypeError):
            return None

        # Require minimum path duration (0.5s) to filter out jitter on
        # parked vehicles that barely pass displacement thresholds
        if isinstance(t_start, (int, float)) and isinstance(t_end, (int, float)):
            duration = t_end - t_start
            if duration < 0.5:
                return None

        # Convert normalised coords to pixel space using video dimensions
        vid_w = row.get('width') or 1920
        vid_h = row.get('height') or 1080
        dx = (last_cx - first_cx) * vid_w
        dy = (last_cy - first_cy) * vid_h
        length = np.sqrt(dx * dx + dy * dy)

        # Require minimum displacement (at least ~5% of frame diagonal)
        min_displacement = 0.05 * np.sqrt(vid_w ** 2 + vid_h ** 2)
        if length < min_displacement:
            return None

        return (dx / length, dy / length)

    @staticmethod
    def compute_direction_match(direction, line):
        """Check if track direction aligns with the line's forward vector.

        Args:
            direction: (dx, dy) normalised travel direction, or None.
            line: dict with forward_dx, forward_dy.

        Returns:
            True  -- direction aligns with line forward (dot > 0)
            False -- direction opposes line forward (dot < 0)
            None  -- unknown (no direction or no forward vector)
        """
        if direction is None:
            return None
        fwd_dx = line.get('forward_dx')
        fwd_dy = line.get('forward_dy')
        if fwd_dx is None or fwd_dy is None:
            return None
        dot = direction[0] * fwd_dx + direction[1] * fwd_dy
        if abs(dot) < 1e-6:
            return None
        return dot > 0

    # ------------------------------------------------------------------
    # ReID embeddings (pre-computed, stored in DB)
    # ------------------------------------------------------------------

    def _load_embeddings(self, track_ids):
        """Bulk-load ReID embeddings for a list of track IDs.

        Returns dict: track_id -> np.float32[2048] (L2-normalised).
        Averages multiple embeddings per track if present.
        """
        if not track_ids:
            return {}

        embeddings = {}
        with get_cursor(commit=False) as cursor:
            # Use ANY() for efficient bulk lookup
            cursor.execute("""
                SELECT e.source_image_path, e.vector
                FROM embeddings e
                WHERE e.embedding_type = 'vehicle_appearance'
                  AND e.vector IS NOT NULL
            """)
            # Index by track_id extracted from source_image_path
            # Format: prediction_{pred_id}_track_{track_id}_crop
            track_id_set = set(track_ids)
            track_vectors = {}  # track_id -> list of vectors
            for row in cursor:
                path = row['source_image_path'] or ''
                # Extract track_id from the path
                if '_track_' in path:
                    try:
                        part = path.split('_track_')[1]
                        tid = int(part.split('_')[0])
                    except (IndexError, ValueError):
                        continue
                    if tid in track_id_set:
                        vec = row['vector']
                        if vec is not None and len(vec) > 0:
                            if tid not in track_vectors:
                                track_vectors[tid] = []
                            track_vectors[tid].append(np.array(vec, dtype=np.float32))

        # Average and normalise per track
        for tid, vecs in track_vectors.items():
            if vecs:
                mean_vec = np.mean(vecs, axis=0)
                norm = np.linalg.norm(mean_vec)
                if norm > 1e-6:
                    embeddings[tid] = mean_vec / norm

        return embeddings

    @staticmethod
    def _reid_similarity(emb_a, emb_b):
        """Cosine similarity between two L2-normalised embedding vectors."""
        if emb_a is None or emb_b is None:
            return None
        return float(np.dot(emb_a, emb_b))

    # ------------------------------------------------------------------
    # Color histogram comparison
    # ------------------------------------------------------------------

    THUMBNAIL_DIR = '/opt/groundtruth-studio/thumbnails'

    def _load_color_histograms(self, tracks):
        """Compute HSV color histograms for vehicle crops.

        Returns dict: track_id -> normalised HSV histogram (flattened).
        """
        histograms = {}
        for t in tracks:
            hist = self._compute_crop_histogram(t)
            if hist is not None:
                histograms[t['id']] = hist
        return histograms

    def _compute_crop_histogram(self, track):
        """Crop the vehicle bbox from thumbnail and compute HSV histogram."""
        # Find the thumbnail path for this track's anchor prediction
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT v.thumbnail_path, p.bbox_x, p.bbox_y,
                       p.bbox_width, p.bbox_height
                FROM ai_predictions p
                JOIN videos v ON v.id = p.video_id
                WHERE p.camera_object_track_id = %s
                ORDER BY p.id
                LIMIT 1
            """, (track['id'],))
            row = cursor.fetchone()

        if row is None:
            return None

        row = dict(row)
        thumb_path = row['thumbnail_path']
        if not thumb_path or not os.path.isfile(thumb_path):
            return None

        img = cv2.imread(thumb_path)
        if img is None:
            return None

        # Crop to bbox
        x = max(0, int(row['bbox_x'] or 0))
        y = max(0, int(row['bbox_y'] or 0))
        w = max(1, int(row['bbox_width'] or 1))
        h = max(1, int(row['bbox_height'] or 1))
        crop = img[y:y+h, x:x+w]

        if crop.size == 0:
            return None

        # Convert to HSV and compute histogram
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        # H: 16 bins (0-180), S: 8 bins (0-256), V: 4 bins (0-256)
        hist = cv2.calcHist([hsv], [0, 1, 2], None,
                            [16, 8, 4], [0, 180, 0, 256, 0, 256])
        hist = cv2.normalize(hist, hist).flatten()
        return hist

    @staticmethod
    def _color_similarity(hist_a, hist_b):
        """Compare two HSV histograms using correlation."""
        if hist_a is None or hist_b is None:
            return None
        return float(cv2.compareHist(hist_a, hist_b, cv2.HISTCMP_CORREL))

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    def score_pair(self, track_a, track_b, line_a, line_b, topology):
        """Score a candidate (track_a, track_b) pair across paired lines.

        Returns:
            dict with 'total', 'lane_score', 'temporal_score', 'size_score',
            'direction_score', 'lane_distance', 'temporal_gap', or
            a dict with total=0 on hard-filter rejection.
        """
        zero = {
            'total': 0.0, 'lane_score': 0.0, 'temporal_score': 0.0,
            'size_score': 0.0, 'direction_agreed': None,
            'lane_distance': None, 'temporal_gap': None,
            'rejected': None,
        }

        # -- Hard filter: classification mismatch --
        cls_a = self._get_vehicle_subtype(track_a)
        cls_b = self._get_vehicle_subtype(track_b)
        if cls_a is not None and cls_b is not None and cls_a != cls_b:
            zero['rejected'] = 'classification_mismatch'
            return zero

        # -- Temporal gap --
        a_end = track_a.get('last_seen') or 0
        b_start = track_b.get('first_seen') or 0
        a_start = track_a.get('first_seen') or 0
        b_end = track_b.get('last_seen') or 0

        gap_a_to_b = b_start - a_end
        gap_b_to_a = a_start - b_end
        gap = min(gap_a_to_b, gap_b_to_a)

        max_transit = topology['max_transit_seconds']
        avg_transit = topology.get('avg_transit_seconds') or max_transit / 2.0

        # Hard filter: beyond max transit (but allow overlapping ranges if
        # first_seen gap is within max_transit)
        if gap > max_transit:
            zero['rejected'] = 'temporal_gap_exceeded'
            zero['temporal_gap'] = round(gap, 1)
            return zero

        # For overlapping ranges (gap <= 0), verify first_seen proximity
        first_seen_gap = abs(a_start - b_start)
        if gap <= 0 and first_seen_gap > max_transit:
            zero['rejected'] = 'temporal_gap_exceeded'
            zero['temporal_gap'] = round(first_seen_gap, 1)
            return zero

        # -- Temporal score --
        if gap <= 0 and first_seen_gap <= max_transit:
            temporal_score = TEMPORAL_WEIGHT  # full
        elif gap <= avg_transit * 1.5:
            temporal_score = TEMPORAL_WEIGHT * 0.9
        elif gap <= max_transit:
            temporal_score = TEMPORAL_WEIGHT * 0.6
        else:
            temporal_score = 0.0

        # -- Lane proximity --
        lane_a = self.compute_lane_position(
            track_a['bbox_centroid_x'], track_a['bbox_centroid_y'], line_a)
        lane_b = self.compute_lane_position(
            track_b['bbox_centroid_x'], track_b['bbox_centroid_y'], line_b)

        # Apply lane_mapping_reversed: when True, the lane coordinate on
        # line_b runs in the opposite direction to line_a.
        if line_a.get('lane_mapping_reversed') or line_b.get('lane_mapping_reversed'):
            lane_b = 1.0 - lane_b

        lane_distance = abs(lane_a - lane_b)
        lane_score = LANE_WEIGHT * (1.0 - lane_distance)

        # -- Size similarity --
        area_a = (track_a['avg_bbox_width'] or 0) * (track_a['avg_bbox_height'] or 0)
        area_b = (track_b['avg_bbox_width'] or 0) * (track_b['avg_bbox_height'] or 0)

        if area_a > 0 and area_b > 0:
            size_ratio = min(area_a, area_b) / max(area_a, area_b)
            size_score = SIZE_WEIGHT * size_ratio
        else:
            size_score = 0.0

        # -- Direction HARD FILTER --
        # If both tracks have known direction and they disagree, reject.
        dir_a = self.compute_direction(track_a)
        dir_b = self.compute_direction(track_b)
        match_a = self.compute_direction_match(dir_a, line_a)
        match_b = self.compute_direction_match(dir_b, line_b)

        direction_agreed = None  # None = unknown, True = same, False = opposite
        if match_a is not None and match_b is not None:
            direction_agreed = (match_a == match_b)
            if not direction_agreed:
                zero['rejected'] = 'direction_mismatch'
                zero['temporal_gap'] = round(gap, 1)
                return zero

        total = lane_score + temporal_score + size_score

        return {
            'total': round(total, 4),
            'lane_score': round(lane_score, 4),
            'temporal_score': round(temporal_score, 4),
            'size_score': round(size_score, 4),
            'direction_agreed': direction_agreed,
            'lane_distance': round(lane_distance, 4),
            'temporal_gap': round(gap, 1),
            'rejected': None,
        }

    # ------------------------------------------------------------------
    # Direction-based matching (primary method — no crossing lines needed)
    # ------------------------------------------------------------------

    # Scoring for direction-based matching (direction is a hard pre-filter)
    DIRECTION_TEMPORAL_WEIGHT = 0.30
    DIRECTION_REID_WEIGHT = 0.30
    DIRECTION_COLOR_WEIGHT = 0.20
    DIRECTION_SIZE_WEIGHT = 0.20
    DIRECTION_MATCH_THRESHOLD = 0.40

    def match_camera_pair_by_direction(self, cam_a, cam_b, entity_type='vehicle'):
        """Match tracks across a camera pair using path_data direction.

        Direction of travel determines the lane on a two-lane road:
        positive dx = one direction, negative dx = the other.  Tracks are
        grouped by direction and only matched within the same group.

        Within each direction group, scoring uses temporal proximity and
        size similarity with mutual best-match.

        Returns:
            list of dicts with track_a_id, track_b_id, score_info.
        """
        # Topology
        topo = self._get_topology(cam_a, cam_b) or self._get_topology(cam_b, cam_a)
        if topo is None:
            logger.warning("No topology between %s and %s", cam_a, cam_b)
            return []

        tracks_a = self.get_approved_tracks(cam_a, entity_type)
        tracks_b = self.get_approved_tracks(cam_b, entity_type)
        if not tracks_a or not tracks_b:
            return []

        # Filter out stationary objects: tracks whose observation span
        # (last_seen - first_seen) vastly exceeds the expected transit time
        # are parked/stationary and should not be matched with transiting vehicles.
        max_span = max(60.0, topo['max_transit_seconds'] * 4)

        def _is_transiting(t):
            fs = t.get('first_seen') or 0
            ls = t.get('last_seen') or 0
            span = ls - fs
            return span <= max_span

        tracks_a = [t for t in tracks_a if _is_transiting(t)]
        tracks_b = [t for t in tracks_b if _is_transiting(t)]

        if not tracks_a or not tracks_b:
            return []

        # Exclude tracks that already have a confirmed cross-camera link
        # (they're settled — no need to re-match).
        with get_cursor(commit=False) as cursor:
            cursor.execute(
                "SELECT track_a_id, track_b_id FROM cross_camera_links "
                "WHERE status = 'confirmed'")
            confirmed_ids = set()
            for row in cursor:
                confirmed_ids.add(row['track_a_id'])
                confirmed_ids.add(row['track_b_id'])

        tracks_a = [t for t in tracks_a if t['id'] not in confirmed_ids]
        tracks_b = [t for t in tracks_b if t['id'] not in confirmed_ids]

        if not tracks_a or not tracks_b:
            return []

        # Compute directions and path timestamps for all tracks
        dirs_a = {}
        dirs_b = {}
        path_times = {}  # track_id -> (start, mid, end)

        for t in tracks_a:
            dirs_a[t['id']] = self.compute_direction(t)
            pt = self._get_path_time(t)
            if pt:
                path_times[t['id']] = pt

        for t in tracks_b:
            dirs_b[t['id']] = self.compute_direction(t)
            pt = self._get_path_time(t)
            if pt:
                path_times[t['id']] = pt

        # Pre-load ReID embeddings and color histograms for all tracks
        all_ids = [t['id'] for t in tracks_a] + [t['id'] for t in tracks_b]
        reid_embeddings = self._load_embeddings(all_ids)
        color_histograms = self._load_color_histograms(tracks_a + tracks_b)
        logger.info(
            "Loaded %d ReID embeddings, %d color histograms for %d tracks",
            len(reid_embeddings), len(color_histograms), len(all_ids))

        # Bundle precomputed data for scoring
        precomputed = {
            'path_times': path_times,
            'reid': reid_embeddings,
            'color': color_histograms,
        }

        # Group by direction sign (dx > 0 vs dx < 0)
        # Tracks without direction go into a separate "unknown" group
        def direction_bucket(d):
            if d is None:
                return 'unknown'
            return 'positive' if d[0] > 0 else 'negative'

        groups_a = {'positive': [], 'negative': [], 'unknown': []}
        groups_b = {'positive': [], 'negative': [], 'unknown': []}

        for t in tracks_a:
            groups_a[direction_bucket(dirs_a[t['id']])].append(t)
        for t in tracks_b:
            groups_b[direction_bucket(dirs_b[t['id']])].append(t)

        known_a = len(groups_a['positive']) + len(groups_a['negative'])
        known_b = len(groups_b['positive']) + len(groups_b['negative'])
        logger.info(
            "Direction groups %s: +%d -%d ?%d | %s: +%d -%d ?%d",
            cam_a, len(groups_a['positive']), len(groups_a['negative']),
            len(groups_a['unknown']),
            cam_b, len(groups_b['positive']), len(groups_b['negative']),
            len(groups_b['unknown']))

        # Tracks without known direction are excluded — direction is
        # the primary discriminator and without it we can't be confident.

        # Auto-detect camera direction relationship.
        # Cameras may face the same or opposite directions. A vehicle
        # going east has positive dx on one camera but may have negative
        # dx on the other if the cameras face ~180° apart.
        #
        # Try both pairings and pick whichever produces more matches:
        #   Same-facing:     positive↔positive, negative↔negative
        #   Opposite-facing: positive↔negative, negative↔positive

        same_facing_matches = []
        for bucket in ('positive', 'negative'):
            a_tracks = groups_a[bucket]
            b_tracks = groups_b[bucket]
            if a_tracks and b_tracks:
                same_facing_matches.extend(
                    self._mutual_best_match(a_tracks, b_tracks, topo, bucket,
                                            precomputed))

        opposite_facing_matches = []
        opposite_map = {'positive': 'negative', 'negative': 'positive'}
        for bucket_a, bucket_b in opposite_map.items():
            a_tracks = groups_a[bucket_a]
            b_tracks = groups_b[bucket_b]
            if a_tracks and b_tracks:
                opposite_facing_matches.extend(
                    self._mutual_best_match(a_tracks, b_tracks, topo,
                                            f'{bucket_a}_opp', precomputed))

        # Pick the pairing that produces more matches (and higher avg score)
        def _quality(matches):
            if not matches:
                return (0, 0.0)
            avg = sum(m['score_info']['total'] for m in matches) / len(matches)
            return (len(matches), avg)

        same_q = _quality(same_facing_matches)
        opp_q = _quality(opposite_facing_matches)

        if opp_q > same_q:
            all_matches = opposite_facing_matches
            facing = 'opposite'
        else:
            all_matches = same_facing_matches
            facing = 'same'

        logger.info(
            "Direction matching %s <-> %s: %d matches (%s-facing cameras) "
            "[same=%d opp=%d] (%d/%d with known direction)",
            cam_a, cam_b, len(all_matches), facing,
            same_q[0], opp_q[0], known_a, known_b)

        # Also match unknown-direction tracks, split by TEMPORAL ORDER
        # to prevent cross-direction matches.  On a two-lane road, vehicles
        # going one way appear on cam_a first, the other way on cam_b first.
        # We run two rounds: one for "a appears before b" pairs, one for
        # "b appears before a" pairs.  The scoring function rejects pairs
        # with the wrong temporal sign via the direction_bucket name.
        already_matched_a = {m['track_a_id'] for m in all_matches}
        already_matched_b = {m['track_b_id'] for m in all_matches}

        unknown_a = [t for t in groups_a['unknown']
                     if t['id'] not in already_matched_a]
        unknown_b = [t for t in groups_b['unknown']
                     if t['id'] not in already_matched_b]
        all_b_remaining = [t for t in tracks_b
                           if t['id'] not in already_matched_b]
        all_a_remaining = [t for t in tracks_a
                           if t['id'] not in already_matched_a]

        # Round 1: unknown_a appearing BEFORE cam_b candidates (a_first)
        # Round 2: unknown_a appearing AFTER cam_b candidates (b_first)
        # The bucket name 'unknown_a_first' / 'unknown_b_first' tells the
        # scoring function to reject pairs with the wrong temporal sign.
        for temporal_bucket in ('unknown_a_first', 'unknown_b_first'):
            ua = [t for t in unknown_a if t['id'] not in already_matched_a]
            ub = [t for t in all_b_remaining if t['id'] not in already_matched_b]
            if ua and ub:
                matches = self._mutual_best_match(
                    ua, ub, topo, temporal_bucket, precomputed)
                all_matches.extend(matches)
                already_matched_a.update(m['track_a_id'] for m in matches)
                already_matched_b.update(m['track_b_id'] for m in matches)

        # Remaining unknown_b against remaining all_a (same split)
        for temporal_bucket in ('unknown_a_first', 'unknown_b_first'):
            ua = [t for t in all_a_remaining if t['id'] not in already_matched_a]
            ub = [t for t in unknown_b if t['id'] not in already_matched_b]
            if ub and ua:
                matches = self._mutual_best_match(
                    ua, ub, topo, temporal_bucket, precomputed)
                all_matches.extend(matches)
                already_matched_a.update(m['track_a_id'] for m in matches)
                already_matched_b.update(m['track_b_id'] for m in matches)

        logger.info(
            "Total matches %s <-> %s: %d (incl. %d unknown-direction)",
            cam_a, cam_b, len(all_matches),
            len(all_matches) - (same_q[0] if facing == 'same' else opp_q[0]))

        return all_matches

    def _score_direction_pair(self, track_a, track_b, topology,
                              direction_bucket, precomputed=None):
        """Score a pair for direction-based matching (no crossing lines).

        Uses path_data midpoint timestamps for temporal scoring, ReID embeddings
        and color histograms as visual tiebreakers, plus size similarity.

        Returns dict with 'total' and component scores, or total=0 on rejection.
        """
        precomputed = precomputed or {}
        path_times = precomputed.get('path_times', {})
        reid_embs = precomputed.get('reid', {})
        color_hists = precomputed.get('color', {})

        zero = {
            'total': 0.0, 'temporal_score': 0.0, 'size_score': 0.0,
            'reid_score': 0.0, 'color_score': 0.0, 'reid_similarity': None,
            'direction_bucket': direction_bucket,
            'temporal_gap': None, 'rejected': None,
        }

        # Hard filter: classification mismatch
        cls_a = self._get_vehicle_subtype(track_a)
        cls_b = self._get_vehicle_subtype(track_b)
        if cls_a is not None and cls_b is not None and cls_a != cls_b:
            zero['rejected'] = 'classification_mismatch'
            return zero

        # Temporal gap — prefer path_data timestamps (sub-second accuracy)
        pt_a = path_times.get(track_a['id'])
        pt_b = path_times.get(track_b['id'])

        max_transit = topology['max_transit_seconds']

        if pt_a and pt_b:
            signed_gap = pt_b[1] - pt_a[1]
        else:
            a_start = track_a.get('first_seen') or 0
            b_start = track_b.get('first_seen') or 0
            signed_gap = b_start - a_start

        gap = abs(signed_gap)

        # For unknown-direction matching, enforce temporal order to prevent
        # cross-direction matches.  'unknown_a_first' requires track_a
        # appears before track_b (signed_gap > 0); 'unknown_b_first' is
        # the reverse.
        if direction_bucket == 'unknown_a_first' and signed_gap < 0:
            zero['rejected'] = 'temporal_order_mismatch'
            return zero
        if direction_bucket == 'unknown_b_first' and signed_gap > 0:
            zero['rejected'] = 'temporal_order_mismatch'
            return zero

        if gap > max_transit:
            zero['rejected'] = 'temporal_gap_exceeded'
            zero['temporal_gap'] = round(gap, 1)
            return zero

        # -- Temporal score (continuous decay) --
        temporal_score = self.DIRECTION_TEMPORAL_WEIGHT * max(
            0.0, 1.0 - (gap / max_transit))

        # -- ReID similarity score --
        emb_a = reid_embs.get(track_a['id'])
        emb_b = reid_embs.get(track_b['id'])
        reid_sim = self._reid_similarity(emb_a, emb_b)
        if reid_sim is not None:
            # Fine-tuned model: matches ~0.67, non-matches ~0.24
            # Map: 0.20 → 0, 0.70 → full weight
            reid_score = self.DIRECTION_REID_WEIGHT * max(
                0.0, min(1.0, (reid_sim - 0.20) / 0.50))
        else:
            reid_score = 0.0

        # -- Color histogram similarity --
        hist_a = color_hists.get(track_a['id'])
        hist_b = color_hists.get(track_b['id'])
        color_sim = self._color_similarity(hist_a, hist_b)
        if color_sim is not None:
            # Correlation ranges from -1 to 1; map 0.0→0, 1.0→full weight
            color_score = self.DIRECTION_COLOR_WEIGHT * max(0.0, color_sim)
        else:
            color_score = 0.0

        # -- Size similarity --
        area_a = (track_a['avg_bbox_width'] or 0) * (track_a['avg_bbox_height'] or 0)
        area_b = (track_b['avg_bbox_width'] or 0) * (track_b['avg_bbox_height'] or 0)
        if area_a > 0 and area_b > 0:
            size_ratio = min(area_a, area_b) / max(area_a, area_b)
            size_score = self.DIRECTION_SIZE_WEIGHT * size_ratio
        else:
            size_score = 0.0

        total = temporal_score + reid_score + color_score + size_score

        return {
            'total': round(total, 4),
            'temporal_score': round(temporal_score, 4),
            'reid_score': round(reid_score, 4),
            'color_score': round(color_score, 4),
            'size_score': round(size_score, 4),
            'reid_similarity': round(reid_sim, 4) if reid_sim is not None else None,
            'direction_bucket': direction_bucket,
            'temporal_gap': round(gap, 1),
            'rejected': None,
        }

    def _mutual_best_match(self, tracks_a, tracks_b, topology, direction_bucket,
                           precomputed=None):
        """Mutual best-match within a direction bucket.

        Skips pairs that have already been rejected by a human reviewer,
        so those tracks can find different partners.
        """
        # Load rejected pairs to skip (normalised: smaller id first)
        if not hasattr(self, '_rejected_pairs'):
            self._rejected_pairs = set()
            with get_cursor(commit=False) as cursor:
                cursor.execute(
                    "SELECT track_a_id, track_b_id FROM cross_camera_links "
                    "WHERE status = 'rejected'")
                for row in cursor:
                    a, b = row['track_a_id'], row['track_b_id']
                    self._rejected_pairs.add((min(a, b), max(a, b)))

        best_for_a = {}
        best_for_b = {}

        for ta in tracks_a:
            for tb in tracks_b:
                # Skip previously rejected pairs
                pair_key = (min(ta['id'], tb['id']), max(ta['id'], tb['id']))
                if pair_key in self._rejected_pairs:
                    continue

                info = self._score_direction_pair(ta, tb, topology,
                                                  direction_bucket, precomputed)
                if info['total'] < self.DIRECTION_MATCH_THRESHOLD:
                    continue

                score = info['total']
                if ta['id'] not in best_for_a or score > best_for_a[ta['id']][0]:
                    best_for_a[ta['id']] = (score, tb['id'], info)
                if tb['id'] not in best_for_b or score > best_for_b[tb['id']][0]:
                    best_for_b[tb['id']] = (score, ta['id'], info)

        matches = []
        for a_id, (score_a, b_id, info) in best_for_a.items():
            if b_id in best_for_b and best_for_b[b_id][1] == a_id:
                matches.append({
                    'track_a_id': a_id,
                    'track_b_id': b_id,
                    'score_info': info,
                })
        return matches

    # ------------------------------------------------------------------
    # Crossing-line matching (MOTHBALLED — kept for future use)
    # ------------------------------------------------------------------

    def match_crossing_line_pair(self, line_a, line_b, entity_type='vehicle'):
        """Match tracks across one pair of crossing lines.

        Uses mutual best-match: both sides must agree on the pairing.

        Returns:
            list of dicts, each with track_a_id, track_b_id, score_info.
        """
        cam_a = line_a['camera_id']
        cam_b = line_b['camera_id']

        # Topology -- try both directions
        topo_ab = self._get_topology(cam_a, cam_b)
        topo_ba = self._get_topology(cam_b, cam_a)
        topology = topo_ab or topo_ba
        if topology is None:
            logger.warning(
                "No topology between cameras %s and %s for lines %s/%s",
                cam_a, cam_b, line_a['id'], line_b['id'])
            return []

        tracks_a = self.get_approved_tracks(cam_a, entity_type)
        tracks_b = self.get_approved_tracks(cam_b, entity_type)

        if not tracks_a or not tracks_b:
            return []

        # Phase 1: score all pairs, track best from each side
        best_for_a = {}   # a_id -> (score, b_id, score_info)
        best_for_b = {}   # b_id -> (score, a_id, score_info)

        for ta in tracks_a:
            for tb in tracks_b:
                info = self.score_pair(ta, tb, line_a, line_b, topology)
                if info['total'] < MATCH_THRESHOLD:
                    continue

                score = info['total']

                if ta['id'] not in best_for_a or score > best_for_a[ta['id']][0]:
                    best_for_a[ta['id']] = (score, tb['id'], info)

                if tb['id'] not in best_for_b or score > best_for_b[tb['id']][0]:
                    best_for_b[tb['id']] = (score, ta['id'], info)

        # Phase 2: mutual best-match
        matches = []
        for a_id, (score_a, b_id, info) in best_for_a.items():
            if b_id in best_for_b and best_for_b[b_id][1] == a_id:
                matches.append({
                    'track_a_id': a_id,
                    'track_b_id': b_id,
                    'score_info': info,
                })

        logger.info(
            "Crossing-line pair %s(%s) <-> %s(%s): %d mutual matches "
            "from %d A-candidates, %d B-candidates",
            line_a['line_name'], cam_a, line_b['line_name'], cam_b,
            len(matches), len(best_for_a), len(best_for_b))

        return matches

    # ------------------------------------------------------------------
    # Full run
    # ------------------------------------------------------------------

    def match_all(self, entity_type='vehicle'):
        """Run spatial matching for all topology-connected camera pairs.

        Primary method: direction-based matching using Frigate path_data.
        Crossing-line matching is mothballed but available via
        match_all_crossing_lines().

        Returns:
            dict with total_links_created, per_pair results.
        """
        # Get all camera pairs from topology
        camera_pairs = self._get_all_topology_pairs()
        total_links = 0
        per_pair = []

        for cam_a, cam_b in camera_pairs:
            matches = self.match_camera_pair_by_direction(
                cam_a, cam_b, entity_type)
            pair_links = 0

            for m in matches:
                link_id = self.create_link(
                    m['track_a_id'], m['track_b_id'], entity_type,
                    m['score_info'],
                )
                if link_id is not None:
                    pair_links += 1

            total_links += pair_links
            per_pair.append({
                'camera_a': cam_a,
                'camera_b': cam_b,
                'matches_found': len(matches),
                'links_created': pair_links,
            })

        logger.info("Direction-based matching complete: %d total links "
                     "across %d camera pairs", total_links, len(camera_pairs))

        return {
            'total_links_created': total_links,
            'camera_pairs_processed': len(camera_pairs),
            'per_pair': per_pair,
        }

    def match_all_crossing_lines(self, entity_type='vehicle'):
        """Run crossing-line matching for every paired line (MOTHBALLED).

        Kept for future use (intrusion detection, multi-lane roads).
        """
        pairs = self.get_paired_crossing_lines()
        total_links = 0
        per_pair = []

        for line_a, line_b in pairs:
            matches = self.match_crossing_line_pair(line_a, line_b, entity_type)
            pair_links = 0

            for m in matches:
                link_id = self.create_link(
                    m['track_a_id'], m['track_b_id'], entity_type,
                    m['score_info'],
                    crossing_line_id=line_a['id'],
                    method='crossing_line',
                )
                if link_id is not None:
                    pair_links += 1

            total_links += pair_links
            per_pair.append({
                'line_a_id': line_a['id'],
                'line_b_id': line_b['id'],
                'camera_a': line_a['camera_id'],
                'camera_b': line_b['camera_id'],
                'matches_found': len(matches),
                'links_created': pair_links,
            })

        return {
            'total_links_created': total_links,
            'line_pairs_processed': len(pairs),
            'per_pair': per_pair,
        }

    def _get_all_topology_pairs(self):
        """Get unique camera pairs from learned topology.

        Deduplicates (A,B) and (B,A) by keeping the pair with the
        smaller camera_a value first.
        """
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT DISTINCT
                    LEAST(camera_a, camera_b) AS cam_lo,
                    GREATEST(camera_a, camera_b) AS cam_hi
                FROM camera_topology_learned
                ORDER BY cam_lo, cam_hi
            """)
            return [(r['cam_lo'], r['cam_hi']) for r in cursor.fetchall()]

    # ------------------------------------------------------------------
    # Link creation
    # ------------------------------------------------------------------

    def create_link(self, track_a_id, track_b_id, entity_type, match_info,
                    crossing_line_id=None, method='direction'):
        """Create a cross_camera_links record for a spatial match.

        Normalises order (smaller ID first), same as CrossCameraMatcher.

        Returns:
            link id on success, None on failure.
        """
        # Normalise order
        if track_a_id > track_b_id:
            track_a_id, track_b_id = track_b_id, track_a_id

        confidence = match_info['total']
        status = 'auto_confirmed' if confidence >= 0.90 else 'auto'

        try:
            with get_cursor() as cursor:
                cursor.execute("""
                    INSERT INTO cross_camera_links
                        (track_a_id, track_b_id, entity_type,
                         match_confidence, match_method, reid_similarity,
                         temporal_gap_seconds, classification_match,
                         status, lane_distance, crossing_line_id)
                    VALUES (%s, %s, %s,
                            %s, %s, %s,
                            %s, %s,
                            %s, %s, %s)
                    ON CONFLICT (track_a_id, track_b_id) DO UPDATE SET
                        match_confidence   = GREATEST(
                            cross_camera_links.match_confidence,
                            EXCLUDED.match_confidence),
                        match_method       = EXCLUDED.match_method,
                        reid_similarity    = COALESCE(EXCLUDED.reid_similarity,
                                                      cross_camera_links.reid_similarity),
                        temporal_gap_seconds = EXCLUDED.temporal_gap_seconds,
                        classification_match = EXCLUDED.classification_match,
                        -- Never overwrite user-reviewed statuses
                        status             = CASE
                            WHEN cross_camera_links.status IN ('confirmed', 'rejected')
                            THEN cross_camera_links.status
                            ELSE EXCLUDED.status
                        END,
                        lane_distance      = EXCLUDED.lane_distance,
                        crossing_line_id   = EXCLUDED.crossing_line_id
                    RETURNING id
                """, (
                    track_a_id, track_b_id, entity_type,
                    round(confidence, 4), method,
                    match_info.get('reid_similarity'),
                    match_info.get('temporal_gap'),
                    self._classification_match_for(track_a_id, track_b_id),
                    status,
                    match_info.get('lane_distance'),
                    crossing_line_id,
                ))
                row = cursor.fetchone()
                return row['id'] if row else None
        except Exception as e:
            logger.error("Error creating spatial link %d <-> %d: %s",
                         track_a_id, track_b_id, e)
            return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_vehicle_subtype(track):
        """Extract vehicle_subtype from anchor_classification JSONB."""
        cls = track.get('anchor_classification')
        if cls and isinstance(cls, dict):
            return cls.get('vehicle_subtype')
        return None

    def _classification_match_for(self, track_a_id, track_b_id):
        """Determine classification_match boolean for the link record.

        Loads both tracks' anchor_classification and compares subtypes.
        Returns True (match), False (conflict), or None (unknown).
        """
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT id, anchor_classification
                FROM camera_object_tracks
                WHERE id IN (%s, %s)
            """, (track_a_id, track_b_id))
            rows = {r['id']: r for r in cursor.fetchall()}

        if len(rows) < 2:
            return None

        sub_a = self._get_vehicle_subtype(rows[track_a_id])
        sub_b = self._get_vehicle_subtype(rows[track_b_id])

        if sub_a is None or sub_b is None:
            return None
        return sub_a == sub_b
