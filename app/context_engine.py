"""
Association chain builder for the Multi-Entity Detection System.

This module analyzes spatial co-occurrence of tracked entities to build association chains:
- Person exits/enters vehicle → person_vehicle association
- Vehicle tows trailer → vehicle_trailer association
- Trailer carries boat → trailer_boat association
- Person near boat → person_boat association
"""

import logging
import numpy as np
from collections import defaultdict, deque
from db_connection import get_cursor

logger = logging.getLogger(__name__)

# Association rules defining how entities relate spatially
ASSOCIATION_RULES = [
    {
        'type': 'person_vehicle',
        'check': 'proximity',
        'threshold': 0.15,
        'confidence_delta': 0.1
    },
    {
        'type': 'vehicle_trailer',
        'check': 'adjacency',
        'threshold': 0.2,
        'confidence_delta': 0.08
    },
    {
        'type': 'trailer_boat',
        'check': 'overlap',
        'threshold': 0.3,
        'confidence_delta': 0.1
    },
    {
        'type': 'person_boat',
        'check': 'proximity',
        'threshold': 0.2,
        'confidence_delta': 0.05
    }
]


class ContextEngine:
    """
    Builds and manages association chains between tracked entities.
    """

    def __init__(self, config=None):
        """
        Initialize the context engine.

        Args:
            config: Optional dict with threshold overrides
        """
        self.config = config or {}
        self.proximity_threshold = self.config.get('proximity_threshold', 0.15)
        self.overlap_threshold = self.config.get('overlap_threshold', 0.3)
        self.adjacency_threshold = self.config.get('adjacency_threshold', 0.2)
        logger.info("ContextEngine initialized")

    def _compute_overlap(self, bbox_a, bbox_b):
        """
        Compute Intersection over Union (IoU) between two bounding boxes.

        Args:
            bbox_a: [x, y, w, h] normalized coordinates
            bbox_b: [x, y, w, h] normalized coordinates

        Returns:
            float: IoU value between 0 and 1
        """
        x1_a, y1_a = bbox_a[0], bbox_a[1]
        x2_a, y2_a = bbox_a[0] + bbox_a[2], bbox_a[1] + bbox_a[3]

        x1_b, y1_b = bbox_b[0], bbox_b[1]
        x2_b, y2_b = bbox_b[0] + bbox_b[2], bbox_b[1] + bbox_b[3]

        # Compute intersection
        x1_i = max(x1_a, x1_b)
        y1_i = max(y1_a, y1_b)
        x2_i = min(x2_a, x2_b)
        y2_i = min(y2_a, y2_b)

        if x2_i < x1_i or y2_i < y1_i:
            return 0.0

        intersection = (x2_i - x1_i) * (y2_i - y1_i)

        # Compute union
        area_a = bbox_a[2] * bbox_a[3]
        area_b = bbox_b[2] * bbox_b[3]
        union = area_a + area_b - intersection

        if union == 0:
            return 0.0

        return intersection / union

    def _compute_proximity(self, bbox_a, bbox_b):
        """
        Compute Euclidean distance between bounding box centers.

        Args:
            bbox_a: [x, y, w, h] normalized coordinates
            bbox_b: [x, y, w, h] normalized coordinates

        Returns:
            float: Distance between centers (normalized)
        """
        center_a = np.array([bbox_a[0] + bbox_a[2] / 2, bbox_a[1] + bbox_a[3] / 2])
        center_b = np.array([bbox_b[0] + bbox_b[2] / 2, bbox_b[1] + bbox_b[3] / 2])

        return np.linalg.norm(center_a - center_b)

    def _is_inside(self, inner_bbox, outer_bbox):
        """
        Check if inner bounding box center is inside outer bounding box.

        Args:
            inner_bbox: [x, y, w, h] normalized coordinates
            outer_bbox: [x, y, w, h] normalized coordinates

        Returns:
            bool: True if inner center is within outer bounds
        """
        inner_cx = inner_bbox[0] + inner_bbox[2] / 2
        inner_cy = inner_bbox[1] + inner_bbox[3] / 2

        outer_x1 = outer_bbox[0]
        outer_y1 = outer_bbox[1]
        outer_x2 = outer_bbox[0] + outer_bbox[2]
        outer_y2 = outer_bbox[1] + outer_bbox[3]

        return (outer_x1 <= inner_cx <= outer_x2 and
                outer_y1 <= inner_cy <= outer_y2)

    def _check_adjacency(self, bbox_a, bbox_b, threshold):
        """
        Check if two bounding boxes are horizontally adjacent (for vehicle-trailer).

        Args:
            bbox_a: [x, y, w, h] normalized coordinates
            bbox_b: [x, y, w, h] normalized coordinates
            threshold: Maximum gap between boxes

        Returns:
            bool: True if boxes are adjacent within threshold
        """
        # Get right edge of left box and left edge of right box
        if bbox_a[0] < bbox_b[0]:
            left_box, right_box = bbox_a, bbox_b
        else:
            left_box, right_box = bbox_b, bbox_a

        left_right_edge = left_box[0] + left_box[2]
        right_left_edge = right_box[0]

        # Check horizontal gap
        gap = right_left_edge - left_right_edge

        # Check vertical alignment (centers should be roughly aligned)
        center_a_y = bbox_a[1] + bbox_a[3] / 2
        center_b_y = bbox_b[1] + bbox_b[3] / 2
        vertical_diff = abs(center_a_y - center_b_y)

        return 0 <= gap <= threshold and vertical_diff < 0.2

    def _upsert_association(self, identity_a, identity_b, association_type, confidence_delta):
        """
        Insert or update an association between two identities.

        Args:
            identity_a: First identity ID
            identity_b: Second identity ID
            association_type: Type of association (e.g., 'person_vehicle')
            confidence_delta: Confidence increment for this observation
        """
        try:
            with get_cursor() as cursor:
                cursor.execute("""
                    INSERT INTO associations (identity_a, identity_b, association_type, confidence, observation_count)
                    VALUES (%s, %s, %s, %s, 1)
                    ON CONFLICT (identity_a, identity_b, association_type)
                    DO UPDATE SET
                        confidence = LEAST(1.0, associations.confidence + excluded.confidence),
                        observation_count = associations.observation_count + 1,
                        last_observed = NOW()
                """, (identity_a, identity_b, association_type, confidence_delta))

                logger.debug(f"Upserted association: {identity_a} <-> {identity_b} ({association_type})")
        except Exception as e:
            logger.error(f"Failed to upsert association: {e}")

    def analyze_frame_associations(self, camera_id, tracked_objects):
        """
        Analyze spatial relationships in a single frame.

        Args:
            camera_id: Camera identifier
            tracked_objects: List of dicts with keys: identity_id, entity_type, bbox
        """
        # Group objects by entity type
        entities_by_type = defaultdict(list)
        for obj in tracked_objects:
            entities_by_type[obj['entity_type']].append(obj)

        # Apply association rules
        for rule in ASSOCIATION_RULES:
            rule_type = rule['type']
            check_method = rule['check']
            threshold = rule['threshold']
            confidence_delta = rule['confidence_delta']

            # Parse entity types from rule type (e.g., 'person_vehicle' -> 'person', 'vehicle')
            entity_types = rule_type.split('_')
            if len(entity_types) != 2:
                logger.warning(f"Invalid rule type: {rule_type}")
                continue

            type_a, type_b = entity_types
            entities_a = entities_by_type.get(type_a, [])
            entities_b = entities_by_type.get(type_b, [])

            # Check all pairs
            for obj_a in entities_a:
                for obj_b in entities_b:
                    bbox_a = obj_a['bbox']
                    bbox_b = obj_b['bbox']

                    # Apply spatial check
                    passes_check = False
                    if check_method == 'proximity':
                        distance = self._compute_proximity(bbox_a, bbox_b)
                        passes_check = distance < threshold
                    elif check_method == 'overlap':
                        iou = self._compute_overlap(bbox_a, bbox_b)
                        passes_check = iou > threshold
                    elif check_method == 'adjacency':
                        passes_check = self._check_adjacency(bbox_a, bbox_b, threshold)

                    if passes_check:
                        # Record association
                        self._upsert_association(
                            obj_a['identity_id'],
                            obj_b['identity_id'],
                            rule_type,
                            confidence_delta
                        )

    def process_tracked_frame(self, camera_id, tracked_objects):
        """
        Main entry point for processing a frame with tracked objects.

        Args:
            camera_id: Camera identifier
            tracked_objects: List of tracked object dicts
        """
        logger.debug(f"Processing frame from camera {camera_id} with {len(tracked_objects)} objects")
        self.analyze_frame_associations(camera_id, tracked_objects)

    def get_entity_associations(self, identity_id):
        """
        Get all direct associations for an identity.

        Args:
            identity_id: Identity to query

        Returns:
            list: List of dicts with association details
        """
        try:
            with get_cursor() as cursor:
                cursor.execute("""
                    SELECT
                        a.association_type,
                        a.confidence,
                        a.observation_count,
                        a.last_observed,
                        CASE
                            WHEN a.identity_a = %s THEN a.identity_b
                            ELSE a.identity_a
                        END as associated_identity,
                        i.identity_type,
                        i.name
                    FROM associations a
                    JOIN identities i ON (
                        CASE
                            WHEN a.identity_a = %s THEN a.identity_b
                            ELSE a.identity_a
                        END = i.identity_id
                    )
                    WHERE a.identity_a = %s OR a.identity_b = %s
                    ORDER BY a.confidence DESC
                """, (identity_id, identity_id, identity_id, identity_id))

                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Failed to get entity associations: {e}")
            return []

    def get_association_chain(self, identity_id):
        """
        Get full association chain using BFS traversal.

        Args:
            identity_id: Starting identity

        Returns:
            dict: Chain with structure {root, persons, vehicles, trailers, boats}
        """
        visited = set()
        chain = {
            'root': identity_id,
            'persons': [],
            'vehicles': [],
            'trailers': [],
            'boats': []
        }

        queue = deque([identity_id])
        visited.add(identity_id)

        try:
            with get_cursor() as cursor:
                while queue:
                    current_id = queue.popleft()

                    # Get associations for current identity
                    cursor.execute("""
                        SELECT
                            CASE
                                WHEN a.identity_a = %s THEN a.identity_b
                                ELSE a.identity_a
                            END as associated_id,
                            i.identity_type,
                            i.name,
                            a.association_type,
                            a.confidence
                        FROM associations a
                        JOIN identities i ON (
                            CASE
                                WHEN a.identity_a = %s THEN a.identity_b
                                ELSE a.identity_a
                            END = i.identity_id
                        )
                        WHERE (a.identity_a = %s OR a.identity_b = %s)
                        AND a.confidence > 0.3
                    """, (current_id, current_id, current_id, current_id))

                    for row in cursor.fetchall():
                        associated_id = row['associated_id']
                        entity_type = row['identity_type']

                        if associated_id not in visited:
                            visited.add(associated_id)
                            queue.append(associated_id)

                            # Add to appropriate category
                            entity_info = {
                                'identity_id': associated_id,
                                'name': row['name'],
                                'association_type': row['association_type'],
                                'confidence': float(row['confidence'])
                            }

                            if entity_type == 'person':
                                chain['persons'].append(entity_info)
                            elif entity_type == 'vehicle':
                                chain['vehicles'].append(entity_info)
                            elif entity_type == 'trailer':
                                chain['trailers'].append(entity_info)
                            elif entity_type == 'boat':
                                chain['boats'].append(entity_info)
        except Exception as e:
            logger.error(f"Failed to build association chain: {e}")

        return chain

    def get_strong_associations(self, min_confidence=0.5, min_observations=3):
        """
        Get all associations above specified thresholds.

        Args:
            min_confidence: Minimum confidence score
            min_observations: Minimum observation count

        Returns:
            list: List of strong association dicts
        """
        try:
            with get_cursor() as cursor:
                cursor.execute("""
                    SELECT
                        a.association_type,
                        a.confidence,
                        a.observation_count,
                        a.last_observed,
                        i1.identity_type as identity_type_a,
                        i1.name as name_a,
                        i2.identity_type as identity_type_b,
                        i2.name as name_b
                    FROM associations a
                    JOIN identities i1 ON a.identity_a = i1.identity_id
                    JOIN identities i2 ON a.identity_b = i2.identity_id
                    WHERE a.confidence >= %s
                    AND a.observation_count >= %s
                    ORDER BY a.confidence DESC, a.observation_count DESC
                """, (min_confidence, min_observations))

                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Failed to get strong associations: {e}")
            return []


if __name__ == '__main__':
    # Standalone mode: print strong associations
    logging.basicConfig(level=logging.INFO)

    engine = ContextEngine()
    strong_associations = engine.get_strong_associations()

    print("\nStrong Associations (confidence >= 0.5, observations >= 3):")
    print("=" * 80)

    for assoc in strong_associations:
        print(f"\n{assoc['association_type']}:")
        print(f"  {assoc['entity_type_a']} ({assoc['unique_id_a']}) <-> "
              f"{assoc['entity_type_b']} ({assoc['unique_id_b']})")
        print(f"  Confidence: {assoc['confidence']:.2f}")
        print(f"  Observations: {assoc['observation_count']}")
        print(f"  Last seen: {assoc['last_observed']}")

    if not strong_associations:
        print("No strong associations found.")
