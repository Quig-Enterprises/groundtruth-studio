"""
Cross-Camera Entity Matching Engine

Matches the same real-world entity across different cameras using:
1. Visual appearance (ReID embeddings)
2. Temporal proximity (within camera transit times)
3. Classification consistency
4. Bounding box size similarity

Uses camera_topology_learned for transit time constraints and
vehicle ReID embeddings for visual fingerprinting.
"""

import logging
from collections import defaultdict

import numpy as np
from psycopg2.extras import Json

from db_connection import get_cursor
from prediction_grouper import UnionFind

logger = logging.getLogger(__name__)

# Scoring weights
TEMPORAL_MAX_SCORE = 0.3
REID_MAX_SCORE = 0.4
CLASSIFICATION_MATCH_SCORE = 0.2
CLASSIFICATION_CONFLICT_PENALTY = -0.2
BBOX_SIZE_MAX_SCORE = 0.1
MATCH_THRESHOLD = 0.8
MIN_REID_SIMILARITY = 0.80  # Minimum cosine similarity to count as a ReID match
DIRECTION_PENALTY = 0.7  # Multiplier on temporal score when travel direction opposes learned topology


class CrossCameraMatcher:
    """Matches entity tracks across different cameras."""

    def __init__(self, reid_api_url='http://localhost:5061'):
        self.reid_api_url = reid_api_url
        self._topology_cache = {}
        self._embedding_cache = {}

    # ------------------------------------------------------------------
    # Topology
    # ------------------------------------------------------------------

    def get_topology(self, camera_a, camera_b):
        """Get learned transit time between two cameras."""
        key = (camera_a, camera_b)
        if key not in self._topology_cache:
            with get_cursor(commit=False) as cursor:
                cursor.execute("""
                    SELECT min_transit_seconds, max_transit_seconds, avg_transit_seconds
                    FROM camera_topology_learned
                    WHERE camera_a = %s AND camera_b = %s
                """, (camera_a, camera_b))
                row = cursor.fetchone()
                self._topology_cache[key] = dict(row) if row else None
        return self._topology_cache[key]

    def get_all_camera_pairs(self):
        """Get all camera pairs with known topology."""
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT DISTINCT camera_a, camera_b
                FROM camera_topology_learned
                WHERE camera_a < camera_b
            """)
            return [(row['camera_a'], row['camera_b']) for row in cursor.fetchall()]

    # ------------------------------------------------------------------
    # Track retrieval
    # ------------------------------------------------------------------

    def get_approved_tracks(self, camera_id, entity_type='vehicle'):
        """Get approved tracks for a camera with temporal bounds."""
        scenario = 'vehicle_detection' if entity_type == 'vehicle' else entity_type + '_detection'
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
    # Embedding retrieval
    # ------------------------------------------------------------------

    def get_track_embedding(self, track_id):
        """Get mean embedding for a track from predictions' embeddings.

        Falls back to querying embeddings linked via predictions in this track.
        """
        if track_id in self._embedding_cache:
            return self._embedding_cache[track_id]

        with get_cursor(commit=False) as cursor:
            # Get embeddings linked to predictions in this track
            # The embeddings table uses identity_id (UUID) not track_id directly,
            # so we look for embeddings by camera_id and source_image matching track predictions
            cursor.execute("""
                SELECT e.vector
                FROM embeddings e
                WHERE e.source_image_path LIKE 'prediction_%%_track_' || %s::text || '_%%'
                  AND e.embedding_type = 'vehicle_appearance'
            """, (track_id,))
            rows = cursor.fetchall()

            if not rows:
                # Try alternate: match by camera_id and temporal proximity
                self._embedding_cache[track_id] = None
                return None

            vectors = [np.array(r['vector'], dtype=np.float32) for r in rows]
            mean_vec = np.mean(vectors, axis=0)
            # L2 normalize
            norm = np.linalg.norm(mean_vec)
            if norm > 0:
                mean_vec = mean_vec / norm

            self._embedding_cache[track_id] = mean_vec
            return mean_vec

    # ------------------------------------------------------------------
    # Scoring functions
    # ------------------------------------------------------------------

    def compute_temporal_score(self, track_a, track_b, topology, direction_match=None):
        """Score temporal plausibility of two tracks being the same entity.

        Uses departure-to-arrival gap: time between last_seen on one camera
        and first_seen on the other. This properly handles long-lived tracks
        (e.g. parked vehicles visible all day) by measuring actual transit
        rather than naive time-range overlap.

        Args:
            track_a: dict with first_seen, last_seen (epoch floats)
            track_b: dict with first_seen, last_seen (epoch floats)
            topology: dict with min_transit_seconds, max_transit_seconds, avg_transit_seconds
            direction_match: True if travel direction matches learned topology,
                             False if reversed, None if bidirectional/unknown

        Returns:
            float score 0.0 to TEMPORAL_MAX_SCORE
        """
        a_start, a_end = track_a['first_seen'], track_a['last_seen']
        b_start, b_end = track_b['first_seen'], track_b['last_seen']

        if not all([a_start, a_end, b_start, b_end]):
            return 0.0

        max_transit = topology['max_transit_seconds']
        avg_transit = topology.get('avg_transit_seconds') or max_transit / 2

        # Departure-to-arrival gap: time between last_seen on one camera
        # and first_seen on the other. Try both directions, use the most
        # favorable (smallest gap). Negative = cameras saw vehicle simultaneously.
        gap_a_to_b = b_start - a_end   # A departs -> B arrives
        gap_b_to_a = a_start - b_end   # B departs -> A arrives
        gap = min(gap_a_to_b, gap_b_to_a)

        if gap <= 0:
            # Ranges overlap — but for overlapping camera views, also check
            # how close the first_seen times are. Two all-day tracks will always
            # overlap, but if first_seen differs by hours they're different entities.
            first_seen_gap = abs(a_start - b_start)
            if first_seen_gap <= max_transit:
                score = TEMPORAL_MAX_SCORE
            elif first_seen_gap <= max_transit * 3:
                score = TEMPORAL_MAX_SCORE * 0.4
            else:
                score = TEMPORAL_MAX_SCORE * 0.1
        elif gap <= avg_transit * 1.5:
            # Near expected transit time (includes brief stops)
            score = TEMPORAL_MAX_SCORE * 0.9
        elif gap <= max_transit:
            # Within plausible range (allows for longer stops)
            score = TEMPORAL_MAX_SCORE * 0.6
        else:
            return 0.0  # Too far apart

        # Apply direction-of-travel penalty when direction opposes learned topology
        if direction_match is False:
            score *= DIRECTION_PENALTY

        return score

    def compute_reid_score(self, track_a_id, track_b_id):
        """Score visual similarity between two tracks using ReID embeddings.

        Returns:
            tuple (score, similarity) where score is 0.0 to REID_MAX_SCORE
        """
        emb_a = self.get_track_embedding(track_a_id)
        emb_b = self.get_track_embedding(track_b_id)

        if emb_a is None or emb_b is None:
            return 0.0, None

        similarity = float(np.dot(emb_a, emb_b))

        if similarity >= 0.90:
            return REID_MAX_SCORE, similarity
        elif similarity >= MIN_REID_SIMILARITY:
            return REID_MAX_SCORE * 0.7, similarity
        elif similarity >= 0.7:
            return REID_MAX_SCORE * 0.2, similarity
        else:
            return 0.0, similarity

    def compute_classification_score(self, track_a, track_b):
        """Score classification consistency between two tracks.

        Returns:
            tuple (score, is_match)
        """
        cls_a = self._get_vehicle_subtype(track_a)
        cls_b = self._get_vehicle_subtype(track_b)

        if cls_a is None or cls_b is None:
            # One or both unclassified -- neutral (slight positive for compatible)
            return 0.1 if (cls_a is None and cls_b is None) else 0.05, None

        if cls_a == cls_b:
            return CLASSIFICATION_MATCH_SCORE, True
        else:
            return CLASSIFICATION_CONFLICT_PENALTY, False

    def compute_bbox_score(self, track_a, track_b):
        """Score bounding box size similarity (accounts for different camera angles).

        Compares relative bbox area as a proxy for vehicle size.
        """
        area_a = track_a['avg_bbox_width'] * track_a['avg_bbox_height']
        area_b = track_b['avg_bbox_width'] * track_b['avg_bbox_height']

        if area_a == 0 or area_b == 0:
            return 0.0

        ratio = min(area_a, area_b) / max(area_a, area_b)

        # Different cameras have very different perspectives, so be lenient
        if ratio > 0.3:
            return BBOX_SIZE_MAX_SCORE * ratio
        return 0.0

    # ------------------------------------------------------------------
    # Main matching
    # ------------------------------------------------------------------

    def match_cameras(self, camera_a, camera_b, entity_type='vehicle'):
        """Run cross-camera matching between two cameras.

        Args:
            camera_a: camera ID
            camera_b: camera ID
            entity_type: 'vehicle', 'person', or 'boat'

        Returns:
            dict with links_created, pairs_evaluated counts
        """
        topology_ab = self.get_topology(camera_a, camera_b)
        topology_ba = self.get_topology(camera_b, camera_a)

        if not topology_ab and not topology_ba:
            logger.warning("No topology between %s and %s", camera_a, camera_b)
            return {'links_created': 0, 'pairs_evaluated': 0, 'error': 'no_topology'}

        topology = topology_ab or topology_ba

        # Direction-of-travel: if only one topology direction exists,
        # we know the expected travel order between cameras.
        # Bidirectional topology (both exist) means no direction penalty.
        is_bidirectional = topology_ab is not None and topology_ba is not None

        tracks_a = self.get_approved_tracks(camera_a, entity_type)
        tracks_b = self.get_approved_tracks(camera_b, entity_type)

        logger.info("Matching %s: %d tracks on %s vs %d tracks on %s (bidirectional=%s)",
                     entity_type, len(tracks_a), camera_a, len(tracks_b), camera_b, is_bidirectional)

        if not tracks_a or not tracks_b:
            return {'links_created': 0, 'pairs_evaluated': 0}

        links_created = 0
        pairs_evaluated = 0

        for ta in tracks_a:
            best_match = None
            best_score = 0

            for tb in tracks_b:
                pairs_evaluated += 1

                # Determine direction-of-travel match
                direction_match = None  # None = bidirectional/unknown, no penalty
                if not is_bidirectional:
                    if topology_ab:
                        # Topology says A→B, so A should be seen first
                        direction_match = (ta['first_seen'] or 0) <= (tb['first_seen'] or 0)
                    elif topology_ba:
                        # Topology says B→A, so B should be seen first
                        direction_match = (tb['first_seen'] or 0) <= (ta['first_seen'] or 0)

                # Temporal score
                temporal = self.compute_temporal_score(ta, tb, topology, direction_match=direction_match)
                if temporal == 0.0:
                    continue  # Skip if temporally impossible

                # ReID score
                reid, reid_sim = self.compute_reid_score(ta['id'], tb['id'])

                # Classification score
                cls_score, cls_match = self.compute_classification_score(ta, tb)

                # Hard veto: if both tracks have classifications and they conflict, skip
                if cls_match is False:
                    continue

                # Bbox size score
                bbox_score = self.compute_bbox_score(ta, tb)

                total = temporal + reid + cls_score + bbox_score

                if total >= MATCH_THRESHOLD and total > best_score:
                    best_score = total
                    best_match = {
                        'track_b': tb,
                        'confidence': total,
                        'reid_similarity': reid_sim,
                        'temporal_gap': self._compute_gap(ta, tb),
                        'classification_match': cls_match,
                        'method': self._determine_method(reid_sim, temporal, cls_match),
                    }

            if best_match:
                created = self._create_link(
                    ta['id'], best_match['track_b']['id'],
                    entity_type, best_match
                )
                if created:
                    links_created += 1

        logger.info("Matching complete: %d links from %d pairs", links_created, pairs_evaluated)
        return {'links_created': links_created, 'pairs_evaluated': pairs_evaluated}

    def match_all_pairs(self, entity_type='vehicle'):
        """Run matching for all camera pairs with known topology.

        Returns:
            dict with total_links, total_pairs, per_pair results
        """
        pairs = self.get_all_camera_pairs()
        total_links = 0
        total_pairs = 0
        results = []

        for cam_a, cam_b in pairs:
            result = self.match_cameras(cam_a, cam_b, entity_type)
            total_links += result.get('links_created', 0)
            total_pairs += result.get('pairs_evaluated', 0)
            results.append({
                'camera_a': cam_a,
                'camera_b': cam_b,
                **result
            })

        # After all matching, assign identities
        identities = self.assign_identities()

        return {
            'total_links': total_links,
            'total_pairs_evaluated': total_pairs,
            'identities_assigned': identities,
            'per_pair': results,
        }

    def match_track(self, track_id, entity_type='vehicle'):
        """Find cross-camera matches for a specific track.

        Returns:
            list of potential matches with scores
        """
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT * FROM camera_object_tracks WHERE id = %s
            """, (track_id,))
            track = cursor.fetchone()
            if not track:
                return []
            track = dict(track)

        source_camera = track['camera_id']
        matches = []

        # Find all cameras with topology to this camera
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT DISTINCT camera_b FROM camera_topology_learned
                WHERE camera_a = %s
            """, (source_camera,))
            target_cameras = [r['camera_b'] for r in cursor.fetchall()]

        for target_cam in target_cameras:
            topology = self.get_topology(source_camera, target_cam)
            if not topology:
                continue

            target_tracks = self.get_approved_tracks(target_cam, entity_type)
            for tb in target_tracks:
                temporal = self.compute_temporal_score(track, tb, topology)
                if temporal == 0.0:
                    continue

                reid, reid_sim = self.compute_reid_score(track['id'], tb['id'])
                cls_score, cls_match = self.compute_classification_score(track, tb)
                bbox_score = self.compute_bbox_score(track, tb)
                total = temporal + reid + cls_score + bbox_score

                if total >= MATCH_THRESHOLD * 0.7:  # Lower threshold for suggestions
                    matches.append({
                        'track_id': tb['id'],
                        'camera_id': tb['camera_id'],
                        'confidence': round(total, 3),
                        'reid_similarity': round(reid_sim, 3) if reid_sim else None,
                        'temporal_gap': self._compute_gap(track, tb),
                        'classification_match': cls_match,
                    })

        matches.sort(key=lambda m: m['confidence'], reverse=True)
        return matches

    # ------------------------------------------------------------------
    # Identity assignment (Union-Find)
    # ------------------------------------------------------------------

    def assign_identities(self):
        """Assign cross_camera_identity_id to linked tracks using Union-Find.

        Returns:
            dict with identities_count, tracks_linked
        """
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT track_a_id, track_b_id
                FROM cross_camera_links
                WHERE status != 'rejected'
            """)
            links = cursor.fetchall()

        if not links:
            return {'identities_count': 0, 'tracks_linked': 0}

        # Collect all track IDs
        track_ids = set()
        for link in links:
            track_ids.add(link['track_a_id'])
            track_ids.add(link['track_b_id'])

        track_list = sorted(track_ids)
        id_to_idx = {tid: i for i, tid in enumerate(track_list)}

        # Build Union-Find
        uf = UnionFind(len(track_list))
        for link in links:
            uf.union(id_to_idx[link['track_a_id']], id_to_idx[link['track_b_id']])

        # Collect components and assign identity IDs
        # Use the minimum track_id in each component as the identity_id
        components = uf.components()
        tracks_linked = 0

        with get_cursor() as cursor:
            # Reset all identity IDs first
            cursor.execute("""
                UPDATE camera_object_tracks
                SET cross_camera_identity_id = NULL
                WHERE cross_camera_identity_id IS NOT NULL
            """)

            for component in components:
                if len(component) < 2:
                    continue  # Single tracks don't need identity

                component_track_ids = [track_list[i] for i in component]
                identity_id = min(component_track_ids)  # Use min track_id as identity

                cursor.execute("""
                    UPDATE camera_object_tracks
                    SET cross_camera_identity_id = %s
                    WHERE id = ANY(%s)
                """, (identity_id, component_track_ids))
                tracks_linked += len(component_track_ids)

        identity_count = sum(1 for c in components if len(c) >= 2)
        logger.info("Identity assignment: %d identities, %d tracks linked",
                     identity_count, tracks_linked)
        return {'identities_count': identity_count, 'tracks_linked': tracks_linked}

    # ------------------------------------------------------------------
    # Classification propagation (Phase 4)
    # ------------------------------------------------------------------

    def propagate_classifications(self, identity_id=None):
        """Propagate majority-vote classification across linked tracks.

        Args:
            identity_id: specific identity to propagate, or None for all

        Returns:
            dict with propagated_count, conflict_count
        """
        with get_cursor(commit=False) as cursor:
            if identity_id:
                cursor.execute("""
                    SELECT cross_camera_identity_id,
                           array_agg(id) as track_ids,
                           array_agg(anchor_classification) as classifications,
                           array_agg(member_count) as member_counts,
                           array_agg(camera_id) as camera_ids
                    FROM camera_object_tracks
                    WHERE cross_camera_identity_id = %s
                    GROUP BY cross_camera_identity_id
                """, (identity_id,))
            else:
                cursor.execute("""
                    SELECT cross_camera_identity_id,
                           array_agg(id) as track_ids,
                           array_agg(anchor_classification) as classifications,
                           array_agg(member_count) as member_counts,
                           array_agg(camera_id) as camera_ids
                    FROM camera_object_tracks
                    WHERE cross_camera_identity_id IS NOT NULL
                    GROUP BY cross_camera_identity_id
                """)
            groups = cursor.fetchall()

        propagated = 0
        conflicts = 0

        for group in groups:
            result = self._propagate_group_classification(dict(group))
            propagated += result['propagated']
            conflicts += result['conflicts']

        return {'propagated_count': propagated, 'conflict_count': conflicts}

    def _propagate_group_classification(self, group):
        """Propagate classification within a single identity group."""
        track_ids = group['track_ids']
        classifications = group['classifications']
        member_counts = group['member_counts']

        # Extract vehicle_subtypes with weights
        votes = defaultdict(int)
        for cls, weight in zip(classifications, member_counts):
            if cls and isinstance(cls, dict) and cls.get('vehicle_subtype'):
                votes[cls['vehicle_subtype']] += weight

        if not votes:
            return {'propagated': 0, 'conflicts': 0}

        # Find majority
        majority_type = max(votes, key=votes.get)
        unique_types = set(votes.keys())
        has_conflict = len(unique_types) > 1

        propagated = 0
        with get_cursor() as cursor:
            for track_id, cls in zip(track_ids, classifications):
                current_subtype = None
                if cls and isinstance(cls, dict):
                    current_subtype = cls.get('vehicle_subtype')

                if current_subtype == majority_type:
                    continue  # Already correct

                if current_subtype and current_subtype != majority_type:
                    # Conflict -- mark but don't override manual classification
                    cursor.execute("""
                        UPDATE camera_object_tracks
                        SET cross_camera_conflict = TRUE
                        WHERE id = %s
                    """, (track_id,))
                else:
                    # Unclassified -- apply majority
                    import json
                    new_cls = Json({'vehicle_subtype': majority_type,
                                    'classified_by': 'cross_camera_propagation'})
                    cursor.execute("""
                        UPDATE camera_object_tracks
                        SET anchor_classification = %s,
                            cross_camera_conflict = %s
                        WHERE id = %s
                    """, (new_cls, has_conflict, track_id))

                    # Also propagate to member predictions
                    cls_payload = json.dumps({
                        'vehicle_subtype': majority_type,
                        'classified_by': 'cross_camera_propagation'
                    })
                    cursor.execute("""
                        UPDATE ai_predictions
                        SET corrected_tags = COALESCE(corrected_tags, '{}'::jsonb) || %s::jsonb
                        WHERE camera_object_track_id = %s
                          AND scenario = 'vehicle_detection'
                          AND (corrected_tags IS NULL
                               OR corrected_tags->>'vehicle_subtype' IS NULL)
                          AND review_status IN ('approved', 'auto_approved')
                    """, (cls_payload, track_id))
                    propagated += 1

        return {'propagated': propagated, 'conflicts': 1 if has_conflict else 0}

    # ------------------------------------------------------------------
    # Link management
    # ------------------------------------------------------------------

    def get_links(self, track_id=None, identity_id=None, camera_ids=None, status=None):
        """Get cross-camera links with optional filters."""
        conditions = []
        params = []

        if track_id:
            conditions.append("(l.track_a_id = %s OR l.track_b_id = %s)")
            params.extend([track_id, track_id])
        if identity_id:
            conditions.append("""
                (l.track_a_id IN (SELECT id FROM camera_object_tracks WHERE cross_camera_identity_id = %s)
                 OR l.track_b_id IN (SELECT id FROM camera_object_tracks WHERE cross_camera_identity_id = %s))
            """)
            params.extend([identity_id, identity_id])
        if camera_ids:
            conditions.append("""
                (ta.camera_id = ANY(%s) OR tb.camera_id = ANY(%s))
            """)
            params.extend([camera_ids, camera_ids])
        if status:
            conditions.append("l.status = %s")
            params.append(status)

        where = "WHERE " + " AND ".join(conditions) if conditions else ""

        with get_cursor(commit=False) as cursor:
            cursor.execute(f"""
                SELECT l.*,
                       ta.camera_id as camera_a, ta.anchor_classification as cls_a,
                       ta.member_count as members_a, ta.first_seen as first_seen_a, ta.last_seen as last_seen_a,
                       tb.camera_id as camera_b, tb.anchor_classification as cls_b,
                       tb.member_count as members_b, tb.first_seen as first_seen_b, tb.last_seen as last_seen_b,
                       (SELECT p.id FROM ai_predictions p
                        WHERE p.camera_object_track_id = l.track_a_id
                          AND p.review_status = 'approved'
                        ORDER BY p.confidence DESC LIMIT 1) as pred_id_a,
                       (SELECT p.id FROM ai_predictions p
                        WHERE p.camera_object_track_id = l.track_b_id
                          AND p.review_status = 'approved'
                        ORDER BY p.confidence DESC LIMIT 1) as pred_id_b
                FROM cross_camera_links l
                JOIN camera_object_tracks ta ON l.track_a_id = ta.id
                JOIN camera_object_tracks tb ON l.track_b_id = tb.id
                {where}
                ORDER BY l.match_confidence DESC
            """, params if params else None)
            return [dict(r) for r in cursor.fetchall()]

    def confirm_link(self, link_id, confirmed_by='studio_user', reject=False, rejection_reason=None):
        """Confirm or reject a cross-camera link."""
        status = 'rejected' if reject else 'confirmed'
        with get_cursor() as cursor:
            cursor.execute("""
                UPDATE cross_camera_links
                SET status = %s, confirmed_by = %s, rejection_reason = %s
                WHERE id = %s
                RETURNING id
            """, (status, confirmed_by, rejection_reason, link_id))
            row = cursor.fetchone()
            if not row:
                return None

        # Re-run identity assignment if a link was rejected
        if reject:
            self.assign_identities()

        return {'id': link_id, 'status': status}

    def get_identities(self, entity_type='vehicle', camera_ids=None):
        """Get unique entities with linked tracks per camera."""
        conditions = ["cross_camera_identity_id IS NOT NULL"]
        params = []

        if camera_ids:
            conditions.append("camera_id = ANY(%s)")
            params.append(camera_ids)

        scenario = 'vehicle_detection' if entity_type == 'vehicle' else entity_type + '_detection'

        with get_cursor(commit=False) as cursor:
            cursor.execute(f"""
                SELECT cross_camera_identity_id as identity_id,
                       array_agg(DISTINCT camera_id) as cameras,
                       array_agg(id) as track_ids,
                       SUM(member_count) as total_predictions,
                       MIN(first_seen) as first_seen,
                       MAX(last_seen) as last_seen,
                       COUNT(*) as track_count
                FROM camera_object_tracks
                WHERE {' AND '.join(conditions)}
                  AND scenario = %s
                GROUP BY cross_camera_identity_id
                ORDER BY track_count DESC
            """, params + [scenario])
            return [dict(r) for r in cursor.fetchall()]

    def get_summary(self, camera_ids=None):
        """Get cross-camera matching summary statistics."""
        with get_cursor(commit=False) as cursor:
            if camera_ids:
                cursor.execute("""
                    SELECT COUNT(*) as total_tracks,
                           COUNT(cross_camera_identity_id) as linked_tracks,
                           COUNT(DISTINCT cross_camera_identity_id) as unique_identities
                    FROM camera_object_tracks
                    WHERE camera_id = ANY(%s)
                      AND scenario = 'vehicle_detection'
                      AND anchor_status IN ('approved', 'conflict')
                """, (camera_ids,))
            else:
                cursor.execute("""
                    SELECT COUNT(*) as total_tracks,
                           COUNT(cross_camera_identity_id) as linked_tracks,
                           COUNT(DISTINCT cross_camera_identity_id) as unique_identities
                    FROM camera_object_tracks
                    WHERE scenario = 'vehicle_detection'
                      AND anchor_status IN ('approved', 'conflict')
                """)
            totals = dict(cursor.fetchone())

            # Unlinked = total - linked
            unlinked = totals['total_tracks'] - totals['linked_tracks']
            unique_entities = totals['unique_identities'] + unlinked

            # Per camera breakdown
            if camera_ids:
                cursor.execute("""
                    SELECT camera_id,
                           COUNT(*) as tracks,
                           COUNT(cross_camera_identity_id) as linked
                    FROM camera_object_tracks
                    WHERE camera_id = ANY(%s)
                      AND scenario = 'vehicle_detection'
                      AND anchor_status IN ('approved', 'conflict')
                    GROUP BY camera_id
                """, (camera_ids,))
            else:
                cursor.execute("""
                    SELECT camera_id,
                           COUNT(*) as tracks,
                           COUNT(cross_camera_identity_id) as linked
                    FROM camera_object_tracks
                    WHERE scenario = 'vehicle_detection'
                      AND anchor_status IN ('approved', 'conflict')
                    GROUP BY camera_id
                """)
            by_camera = {r['camera_id']: {'tracks': r['tracks'], 'linked': r['linked']}
                         for r in cursor.fetchall()}

            # Link stats
            cursor.execute("SELECT COUNT(*) as count FROM cross_camera_links WHERE status != 'rejected'")
            link_count = cursor.fetchone()['count']

        return {
            'total_tracks': totals['total_tracks'],
            'unique_entities': unique_entities,
            'cross_camera_links': link_count,
            'linked_tracks': totals['linked_tracks'],
            'unlinked_tracks': unlinked,
            'by_camera': by_camera,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_vehicle_subtype(self, track):
        """Extract vehicle_subtype from track anchor_classification."""
        cls = track.get('anchor_classification')
        if cls and isinstance(cls, dict):
            return cls.get('vehicle_subtype')
        return None

    def _compute_gap(self, track_a, track_b):
        """Compute temporal gap in seconds between two tracks."""
        a_end = track_a.get('last_seen', 0) or 0
        b_start = track_b.get('first_seen', 0) or 0
        a_start = track_a.get('first_seen', 0) or 0
        b_end = track_b.get('last_seen', 0) or 0

        # Gap = time between closest endpoints
        gap1 = abs(b_start - a_end)
        gap2 = abs(a_start - b_end)
        return round(min(gap1, gap2), 1)

    def _determine_method(self, reid_sim, temporal_score, cls_match):
        """Determine the match method string for recording."""
        parts = []
        if reid_sim is not None and reid_sim > 0.3:
            parts.append('reid')
        if temporal_score > 0:
            parts.append('temporal')
        if cls_match is True:
            parts.append('classification')
        return '+'.join(parts) if parts else 'combined'

    def _create_link(self, track_a_id, track_b_id, entity_type, match_info):
        """Create a cross_camera_links record."""
        # Normalize order (smaller ID first)
        if track_a_id > track_b_id:
            track_a_id, track_b_id = track_b_id, track_a_id

        try:
            with get_cursor() as cursor:
                cursor.execute("""
                    INSERT INTO cross_camera_links
                    (track_a_id, track_b_id, entity_type, match_confidence,
                     match_method, reid_similarity, temporal_gap_seconds,
                     classification_match, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'auto')
                    ON CONFLICT (track_a_id, track_b_id) DO UPDATE SET
                        match_confidence = EXCLUDED.match_confidence,
                        match_method = EXCLUDED.match_method,
                        reid_similarity = EXCLUDED.reid_similarity,
                        temporal_gap_seconds = EXCLUDED.temporal_gap_seconds,
                        classification_match = EXCLUDED.classification_match
                    RETURNING id
                """, (
                    track_a_id, track_b_id, entity_type,
                    round(match_info['confidence'], 4),
                    match_info['method'],
                    round(match_info['reid_similarity'], 4) if match_info.get('reid_similarity') is not None else None,
                    match_info.get('temporal_gap'),
                    match_info.get('classification_match'),
                ))
                row = cursor.fetchone()
                return row['id'] if row else None
        except Exception as e:
            logger.error("Error creating link %d <-> %d: %s", track_a_id, track_b_id, e)
            return None
