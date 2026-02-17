"""
Crossing-Line Spatial-Temporal Matching Engine

Matches vehicles across cameras using paired crossing lines drawn in the UI.
Each crossing line defines a spatial gate on a camera view; paired lines on
different cameras represent the same physical boundary seen from two angles.

Scoring combines:
1. Lane proximity   (0.45) -- projection along the crossing line
2. Temporal gap     (0.35) -- departure-to-arrival vs learned topology
3. Size similarity  (0.10) -- bbox area ratio
4. Direction match  (0.10) -- travel direction consistent with line normals

This module is intended to run BEFORE the ReID-based CrossCameraMatcher so
that high-confidence spatial matches are locked in first.
"""

import logging

import numpy as np

from db_connection import get_cursor

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Scoring weights
# ---------------------------------------------------------------------------
LANE_WEIGHT = 0.45
TEMPORAL_WEIGHT = 0.35
SIZE_WEIGHT = 0.10
DIRECTION_BONUS = 0.10

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
        """Compute normalised travel direction for a multi-member track.

        Queries ai_predictions ordered by timestamp and uses bbox centroid
        movement from first to last prediction.

        Returns:
            (dx, dy) normalised direction vector, or None if undetermined.
        """
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
        dx = last['cx'] - first['cx']
        dy = last['cy'] - first['cy']
        length = np.sqrt(dx * dx + dy * dy)
        if length < 1e-6:
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
            'size_score': 0.0, 'direction_score': 0.0,
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

        # -- Direction bonus --
        dir_a = self.compute_direction(track_a)
        dir_b = self.compute_direction(track_b)
        match_a = self.compute_direction_match(dir_a, line_a)
        match_b = self.compute_direction_match(dir_b, line_b)

        direction_score = 0.0
        if match_a is not None and match_b is not None:
            # Both tracks have direction info.  Consistent means both
            # forward or both reverse relative to their paired lines.
            if match_a == match_b:
                direction_score = DIRECTION_BONUS

        total = lane_score + temporal_score + size_score + direction_score

        return {
            'total': round(total, 4),
            'lane_score': round(lane_score, 4),
            'temporal_score': round(temporal_score, 4),
            'size_score': round(size_score, 4),
            'direction_score': round(direction_score, 4),
            'lane_distance': round(lane_distance, 4),
            'temporal_gap': round(gap, 1),
            'rejected': None,
        }

    # ------------------------------------------------------------------
    # Per-pair matching
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
        """Run crossing-line matching for every paired line.

        Returns:
            dict with total_links_created, per_pair results.
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

        logger.info("Crossing-line matching complete: %d total links created "
                     "across %d line pairs", total_links, len(pairs))

        return {
            'total_links_created': total_links,
            'line_pairs_processed': len(pairs),
            'per_pair': per_pair,
        }

    # ------------------------------------------------------------------
    # Link creation
    # ------------------------------------------------------------------

    def create_link(self, track_a_id, track_b_id, entity_type, match_info,
                    crossing_line_id=None):
        """Create a cross_camera_links record for a crossing-line match.

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
                            %s, 'crossing_line', NULL,
                            %s, %s,
                            %s, %s, %s)
                    ON CONFLICT (track_a_id, track_b_id) DO UPDATE SET
                        match_confidence   = GREATEST(
                            cross_camera_links.match_confidence,
                            EXCLUDED.match_confidence),
                        match_method       = EXCLUDED.match_method,
                        temporal_gap_seconds = EXCLUDED.temporal_gap_seconds,
                        classification_match = EXCLUDED.classification_match,
                        status             = EXCLUDED.status,
                        lane_distance      = EXCLUDED.lane_distance,
                        crossing_line_id   = EXCLUDED.crossing_line_id
                    RETURNING id
                """, (
                    track_a_id, track_b_id, entity_type,
                    round(confidence, 4),
                    match_info.get('temporal_gap'),
                    self._classification_match_for(track_a_id, track_b_id),
                    status,
                    match_info.get('lane_distance'),
                    crossing_line_id,
                ))
                row = cursor.fetchone()
                return row['id'] if row else None
        except Exception as e:
            logger.error("Error creating crossing-line link %d <-> %d: %s",
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
