"""
Prediction Grouper - Groups overlapping predictions from the same camera.

Uses Union-Find clustering on IoU (Intersection over Union) to merge predictions
of the same object across multiple frames into "prediction groups" for batch review.
"""

import logging
from db_connection import get_cursor
from psycopg2 import extras

logger = logging.getLogger(__name__)

# IoU threshold for merging predictions into the same group
IOU_THRESHOLD = 0.3

# Centroid distance threshold as fraction of average bbox size
# Predictions of same class with centroids within this fraction are grouped
CENTROID_DISTANCE_FRACTION = 0.5


def compute_iou(box_a, box_b):
    """Compute Intersection over Union between two bboxes.

    Args:
        box_a: dict with keys x, y, width, height
        box_b: dict with keys x, y, width, height
    Returns:
        float: IoU value between 0.0 and 1.0
    """
    ax1, ay1 = box_a['x'], box_a['y']
    ax2, ay2 = ax1 + box_a['width'], ay1 + box_a['height']
    bx1, by1 = box_b['x'], box_b['y']
    bx2, by2 = bx1 + box_b['width'], by1 + box_b['height']

    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    if inter == 0:
        return 0.0

    area_a = box_a['width'] * box_a['height']
    area_b = box_b['width'] * box_b['height']
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


class UnionFind:
    """Disjoint-set (Union-Find) data structure with path compression and union by rank."""

    def __init__(self, n):
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]  # path compression
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1

    def components(self):
        """Return list of lists of indices grouped by component."""
        groups = {}
        for i in range(len(self.parent)):
            root = self.find(i)
            if root not in groups:
                groups[root] = []
            groups[root].append(i)
        return list(groups.values())


class PredictionGrouper:
    """Groups overlapping predictions from the same camera for batch review."""

    def group_predictions(self, predictions):
        """Group a list of predictions by IoU overlap using Union-Find.

        Args:
            predictions: list of prediction dicts (same camera + same scenario).
                         Each must have: id, bbox_x, bbox_y, bbox_width, bbox_height,
                         confidence, timestamp
        Returns:
            list of groups, each group is a dict with:
                - prediction_ids: list of prediction IDs in the group
                - representative_id: ID of highest-confidence prediction
                - bbox_centroid_x, bbox_centroid_y: average center of all bboxes
                - avg_bbox_width, avg_bbox_height: average bbox dimensions
                - min_confidence, max_confidence, avg_confidence
                - min_timestamp, max_timestamp
                - member_count: number of predictions in group
        """
        n = len(predictions)
        if n == 0:
            return []
        if n == 1:
            p = predictions[0]
            return [{
                'prediction_ids': [p['id']],
                'representative_id': p['id'],
                'bbox_centroid_x': int(p['bbox_x'] + p['bbox_width'] / 2),
                'bbox_centroid_y': int(p['bbox_y'] + p['bbox_height'] / 2),
                'avg_bbox_width': p['bbox_width'],
                'avg_bbox_height': p['bbox_height'],
                'min_confidence': p['confidence'],
                'max_confidence': p['confidence'],
                'avg_confidence': p['confidence'],
                'min_timestamp': p['timestamp'],
                'max_timestamp': p['timestamp'],
                'member_count': 1
            }]

        # Build Union-Find
        uf = UnionFind(n)
        for i in range(n):
            box_i = {
                'x': predictions[i]['bbox_x'], 'y': predictions[i]['bbox_y'],
                'width': predictions[i]['bbox_width'], 'height': predictions[i]['bbox_height']
            }
            for j in range(i + 1, n):
                box_j = {
                    'x': predictions[j]['bbox_x'], 'y': predictions[j]['bbox_y'],
                    'width': predictions[j]['bbox_width'], 'height': predictions[j]['bbox_height']
                }
                iou = compute_iou(box_i, box_j)
                if iou >= IOU_THRESHOLD:
                    uf.union(i, j)
                    continue

                # Centroid distance fallback: merge same-class nearby detections
                # (catches moving vehicles across frames that don't overlap enough)
                pi_tags = predictions[i].get('predicted_tags') or {}
                pj_tags = predictions[j].get('predicted_tags') or {}
                if isinstance(pi_tags, str):
                    try:
                        import json
                        pi_tags = json.loads(pi_tags)
                    except Exception:
                        pi_tags = {}
                if isinstance(pj_tags, str):
                    try:
                        import json
                        pj_tags = json.loads(pj_tags)
                    except Exception:
                        pj_tags = {}
                class_i = pi_tags.get('vehicle_type') or pi_tags.get('class', '')
                class_j = pj_tags.get('vehicle_type') or pj_tags.get('class', '')

                if class_i and class_i == class_j:
                    cx_i = box_i['x'] + box_i['width'] / 2
                    cy_i = box_i['y'] + box_i['height'] / 2
                    cx_j = box_j['x'] + box_j['width'] / 2
                    cy_j = box_j['y'] + box_j['height'] / 2
                    avg_size = (box_i['width'] + box_i['height'] + box_j['width'] + box_j['height']) / 4
                    dist = ((cx_i - cx_j) ** 2 + (cy_i - cy_j) ** 2) ** 0.5
                    if avg_size > 0 and dist < avg_size * CENTROID_DISTANCE_FRACTION:
                        uf.union(i, j)

        # Collect components
        result = []
        for component in uf.components():
            group_preds = [predictions[i] for i in component]
            pred_ids = [p['id'] for p in group_preds]

            # Representative = highest confidence
            rep = max(group_preds, key=lambda p: p['confidence'])

            # Compute stats
            confidences = [p['confidence'] for p in group_preds]
            timestamps = [p['timestamp'] for p in group_preds if p.get('timestamp') is not None]
            centroids_x = [p['bbox_x'] + p['bbox_width'] / 2 for p in group_preds]
            centroids_y = [p['bbox_y'] + p['bbox_height'] / 2 for p in group_preds]
            widths = [p['bbox_width'] for p in group_preds]
            heights = [p['bbox_height'] for p in group_preds]

            result.append({
                'prediction_ids': pred_ids,
                'representative_id': rep['id'],
                'bbox_centroid_x': int(sum(centroids_x) / len(centroids_x)),
                'bbox_centroid_y': int(sum(centroids_y) / len(centroids_y)),
                'avg_bbox_width': int(sum(widths) / len(widths)),
                'avg_bbox_height': int(sum(heights) / len(heights)),
                'min_confidence': min(confidences),
                'max_confidence': max(confidences),
                'avg_confidence': sum(confidences) / len(confidences),
                'min_timestamp': min(timestamps) if timestamps else None,
                'max_timestamp': max(timestamps) if timestamps else None,
                'member_count': len(group_preds)
            })
        return result

    def run_grouping_for_batch(self, prediction_ids):
        """Group a batch of newly inserted predictions.

        Fetches predictions by ID, partitions by (camera_id, scenario),
        checks against existing groups, then creates new groups for unmatched.

        Args:
            prediction_ids: list of prediction IDs to group
        """
        if not prediction_ids:
            return

        try:
            with get_cursor(commit=False) as cursor:
                # Fetch predictions with camera_id from video
                cursor.execute("""
                    SELECT p.id, p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
                           p.confidence, p.timestamp, p.scenario, p.prediction_group_id,
                           p.predicted_tags, p.video_id,
                           v.camera_id
                    FROM ai_predictions p
                    JOIN videos v ON p.video_id = v.id
                    WHERE p.id = ANY(%s)
                      AND p.bbox_x IS NOT NULL
                      AND p.bbox_width > 0
                      AND v.camera_id IS NOT NULL
                """, (prediction_ids,))
                preds = [dict(r) for r in cursor.fetchall()]

            if not preds:
                return

            # Partition by (camera_id, scenario)
            partitions = {}
            for p in preds:
                key = (p['camera_id'], p['scenario'])
                if key not in partitions:
                    partitions[key] = []
                partitions[key].append(p)

            total_groups = 0
            total_assigned = 0

            for (camera_id, scenario), partition_preds in partitions.items():
                grouped, assigned = self._group_partition(camera_id, scenario, partition_preds)
                total_groups += grouped
                total_assigned += assigned

            if total_groups > 0 or total_assigned > 0:
                logger.info(f"Grouping batch: {total_groups} new groups created, "
                           f"{total_assigned} predictions assigned")

        except Exception as e:
            logger.error(f"Error grouping predictions: {e}")

    def _group_partition(self, camera_id, scenario, new_preds):
        """Group predictions within a single (camera_id, scenario) partition.

        1. Fetch existing pending groups for this camera+scenario
        2. Try to match new predictions to existing groups by IoU
        3. Run Union-Find on remaining unmatched predictions
        4. Create new groups

        Returns:
            tuple: (new_groups_created, predictions_assigned)
        """
        groups_created = 0
        preds_assigned = 0

        # Skip predictions already in a group
        ungrouped = [p for p in new_preds if not p.get('prediction_group_id')]
        if not ungrouped:
            return 0, 0

        # Fetch existing pending groups for this camera+scenario
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT id, bbox_centroid_x, bbox_centroid_y, avg_bbox_width, avg_bbox_height
                FROM prediction_groups
                WHERE camera_id = %s AND scenario = %s AND review_status IN ('pending', 'processing')
            """, (camera_id, scenario))
            existing_groups = [dict(r) for r in cursor.fetchall()]

        # Try to match new predictions to existing groups
        matched_to_existing = {}  # group_id -> [prediction_ids]
        remaining = []

        for pred in ungrouped:
            pred_box = {
                'x': pred['bbox_x'], 'y': pred['bbox_y'],
                'width': pred['bbox_width'], 'height': pred['bbox_height']
            }
            matched = False
            for grp in existing_groups:
                grp_box = {
                    'x': grp['bbox_centroid_x'] - grp['avg_bbox_width'] // 2,
                    'y': grp['bbox_centroid_y'] - grp['avg_bbox_height'] // 2,
                    'width': grp['avg_bbox_width'],
                    'height': grp['avg_bbox_height']
                }
                if compute_iou(pred_box, grp_box) >= IOU_THRESHOLD:
                    if grp['id'] not in matched_to_existing:
                        matched_to_existing[grp['id']] = []
                    matched_to_existing[grp['id']].append(pred['id'])
                    matched = True
                    break
            if not matched:
                remaining.append(pred)

        # Assign matched predictions to existing groups
        for group_id, pred_ids in matched_to_existing.items():
            self._assign_to_group(pred_ids, group_id)
            preds_assigned += len(pred_ids)

        # Run Union-Find on remaining unmatched predictions
        if remaining:
            new_groups = self.group_predictions(remaining)
            for grp_data in new_groups:
                group_id = self._create_group(camera_id, scenario, grp_data)
                if group_id:
                    self._assign_to_group(grp_data['prediction_ids'], group_id)
                    groups_created += 1
                    preds_assigned += len(grp_data['prediction_ids'])

        return groups_created, preds_assigned

    def _create_group(self, camera_id, scenario, grp_data):
        """Create a prediction_groups row and return its ID."""
        try:
            with get_cursor() as cursor:
                cursor.execute("""
                    INSERT INTO prediction_groups
                    (camera_id, scenario, representative_prediction_id,
                     bbox_centroid_x, bbox_centroid_y, avg_bbox_width, avg_bbox_height,
                     member_count, min_confidence, max_confidence, avg_confidence,
                     min_timestamp, max_timestamp)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (
                    camera_id, scenario, grp_data['representative_id'],
                    grp_data['bbox_centroid_x'], grp_data['bbox_centroid_y'],
                    grp_data['avg_bbox_width'], grp_data['avg_bbox_height'],
                    grp_data['member_count'],
                    grp_data['min_confidence'], grp_data['max_confidence'],
                    grp_data['avg_confidence'],
                    grp_data['min_timestamp'], grp_data['max_timestamp']
                ))
                row = cursor.fetchone()
                return row['id'] if row else None
        except Exception as e:
            logger.error(f"Error creating prediction group: {e}")
            return None

    def _assign_to_group(self, prediction_ids, group_id):
        """Assign predictions to a group and update group stats."""
        try:
            with get_cursor() as cursor:
                cursor.execute("""
                    UPDATE ai_predictions
                    SET prediction_group_id = %s
                    WHERE id = ANY(%s)
                """, (group_id, prediction_ids))

            # Update group stats
            self._update_group_stats(group_id)
        except Exception as e:
            logger.error(f"Error assigning predictions to group {group_id}: {e}")

    def _update_group_stats(self, group_id):
        """Recompute group statistics from current members."""
        try:
            with get_cursor() as cursor:
                cursor.execute("""
                    UPDATE prediction_groups pg SET
                        member_count = sub.cnt,
                        min_confidence = sub.min_conf,
                        max_confidence = sub.max_conf,
                        avg_confidence = sub.avg_conf,
                        min_timestamp = sub.min_ts,
                        max_timestamp = sub.max_ts,
                        representative_prediction_id = sub.rep_id,
                        updated_at = NOW()
                    FROM (
                        SELECT
                            prediction_group_id,
                            COUNT(*) as cnt,
                            MIN(confidence) as min_conf,
                            MAX(confidence) as max_conf,
                            AVG(confidence) as avg_conf,
                            MIN(timestamp) as min_ts,
                            MAX(timestamp) as max_ts,
                            (SELECT id FROM ai_predictions
                             WHERE prediction_group_id = %s
                             ORDER BY confidence DESC LIMIT 1) as rep_id
                        FROM ai_predictions
                        WHERE prediction_group_id = %s
                        GROUP BY prediction_group_id
                    ) sub
                    WHERE pg.id = %s
                """, (group_id, group_id, group_id))
        except Exception as e:
            logger.error(f"Error updating group stats for {group_id}: {e}")

    def run_grouping_for_camera(self, camera_id, scenario=None):
        """Group all ungrouped pending predictions for a camera.

        Args:
            camera_id: camera ID to group predictions for
            scenario: optional scenario filter
        """
        with get_cursor(commit=False) as cursor:
            conditions = [
                "v.camera_id = %s",
                "p.review_status IN ('pending', 'processing')",
                "p.prediction_group_id IS NULL",
                "p.bbox_x IS NOT NULL",
                "p.bbox_width > 0"
            ]
            params = [camera_id]
            if scenario:
                conditions.append("p.scenario = %s")
                params.append(scenario)

            cursor.execute(f"""
                SELECT p.id, p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
                       p.confidence, p.timestamp, p.scenario, p.prediction_group_id,
                       p.predicted_tags, p.video_id,
                       v.camera_id
                FROM ai_predictions p
                JOIN videos v ON p.video_id = v.id
                WHERE {' AND '.join(conditions)}
            """, params)
            preds = [dict(r) for r in cursor.fetchall()]

        if not preds:
            return 0, 0

        # Partition by scenario
        partitions = {}
        for p in preds:
            if p['scenario'] not in partitions:
                partitions[p['scenario']] = []
            partitions[p['scenario']].append(p)

        total_groups = 0
        total_assigned = 0
        for scen, partition_preds in partitions.items():
            g, a = self._group_partition(camera_id, scen, partition_preds)
            total_groups += g
            total_assigned += a

        return total_groups, total_assigned

    def regroup_all(self, camera_id=None):
        """Full re-grouping: clear existing groups and recompute from scratch.

        Args:
            camera_id: optional - only regroup this camera. None = all cameras.

        Returns:
            dict with groups_created and predictions_grouped counts
        """
        # Clear existing groups
        with get_cursor() as cursor:
            if camera_id:
                # Get group IDs for this camera
                cursor.execute(
                    "SELECT id FROM prediction_groups WHERE camera_id = %s",
                    (camera_id,)
                )
                group_ids = [r['id'] for r in cursor.fetchall()]
                if group_ids:
                    cursor.execute(
                        "UPDATE ai_predictions SET prediction_group_id = NULL WHERE prediction_group_id = ANY(%s)",
                        (group_ids,)
                    )
                    cursor.execute(
                        "DELETE FROM prediction_groups WHERE camera_id = %s",
                        (camera_id,)
                    )
            else:
                cursor.execute("UPDATE ai_predictions SET prediction_group_id = NULL WHERE prediction_group_id IS NOT NULL")
                cursor.execute("DELETE FROM prediction_groups")

        # Fetch all pending/processing predictions with camera info
        with get_cursor(commit=False) as cursor:
            conditions = [
                "p.review_status IN ('pending', 'processing')",
                "p.bbox_x IS NOT NULL",
                "p.bbox_width > 0",
                "v.camera_id IS NOT NULL"
            ]
            params = []
            if camera_id:
                conditions.append("v.camera_id = %s")
                params.append(camera_id)

            cursor.execute(f"""
                SELECT p.id, p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
                       p.confidence, p.timestamp, p.scenario, p.prediction_group_id,
                       p.predicted_tags, p.video_id,
                       v.camera_id
                FROM ai_predictions p
                JOIN videos v ON p.video_id = v.id
                WHERE {' AND '.join(conditions)}
                ORDER BY v.camera_id, p.scenario
            """, params if params else None)
            preds = [dict(r) for r in cursor.fetchall()]

        if not preds:
            return {'groups_created': 0, 'predictions_grouped': 0}

        # Partition by (camera_id, scenario)
        partitions = {}
        for p in preds:
            key = (p['camera_id'], p['scenario'])
            if key not in partitions:
                partitions[key] = []
            partitions[key].append(p)

        total_groups = 0
        total_assigned = 0
        for (cam, scen), partition_preds in partitions.items():
            # Run Union-Find directly (no existing groups since we cleared them)
            groups = self.group_predictions(partition_preds)
            for grp_data in groups:
                group_id = self._create_group(cam, scen, grp_data)
                if group_id:
                    self._assign_to_group(grp_data['prediction_ids'], group_id)
                    total_groups += 1
                    total_assigned += len(grp_data['prediction_ids'])

        logger.info(f"Regroup complete: {total_groups} groups, {total_assigned} predictions")
        return {'groups_created': total_groups, 'predictions_grouped': total_assigned}
