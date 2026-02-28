"""Training Image Gallery routes for Groundtruth Studio."""
from flask import Blueprint, request, jsonify, render_template, send_file, g
from pathlib import Path
from db_connection import get_cursor
from services import db, THUMBNAIL_DIR, BASE_DIR
import json
import logging
import base64
import sys
import requests as http_requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / 'worker'))
from color_hist import compute_color_hist, hist_intersection

training_gallery_bp = Blueprint('training_gallery', __name__)
logger = logging.getLogger(__name__)

CROPS_DIR = BASE_DIR / 'clips' / 'crops'


# ---- Page Route ----

@training_gallery_bp.route('/training-gallery')
def training_gallery_page():
    return render_template('training_gallery.html')


# ---- API Routes ----

@training_gallery_bp.route('/api/training-gallery/filters')
def get_gallery_filters():
    """Return distinct scenarios and classifications with counts for filter dropdowns."""
    try:
        scenario_param = request.args.get('scenario')
        status_param = request.args.get('status', 'approved')
        camera_param = request.args.get('camera')

        if status_param == 'pending':
            status_list = ('pending', 'processing')
        elif status_param == 'rejected':
            status_list = ('rejected', 'auto_rejected')
        else:
            status_list = ('approved', 'auto_approved')

        camera_join = ''
        camera_filter = ''
        camera_params = []
        if camera_param:
            camera_join = 'JOIN videos v ON v.id = p.video_id'
            camera_filter = ' AND v.camera_id = %s'
            camera_params = [camera_param]

        with get_cursor(commit=False) as cursor:
            # Scenarios with counts
            cursor.execute(f'''
                SELECT cc.scenario, COUNT(DISTINCT p.id) as count
                FROM ai_predictions p
                JOIN classification_classes cc ON cc.name = p.classification
                {camera_join}
                WHERE p.review_status IN %s
                  {camera_filter}
                GROUP BY cc.scenario
                ORDER BY count DESC
            ''', (status_list,) + tuple(camera_params))
            scenarios = [dict(row) for row in cursor.fetchall()]

            # Classifications with counts, optionally filtered by scenario
            if scenario_param:
                cursor.execute(f'''
                    SELECT p.classification as name, cc.scenario, cc.display_name,
                           COUNT(*) as count
                    FROM ai_predictions p
                    JOIN classification_classes cc ON cc.name = p.classification
                    {camera_join}
                    WHERE p.review_status IN %s
                      AND cc.scenario = %s
                      {camera_filter}
                    GROUP BY p.classification, cc.scenario, cc.display_name
                    ORDER BY count DESC
                ''', (status_list, scenario_param) + tuple(camera_params))
            else:
                cursor.execute(f'''
                    SELECT p.classification as name, cc.scenario, cc.display_name,
                           COUNT(*) as count
                    FROM ai_predictions p
                    JOIN classification_classes cc ON cc.name = p.classification
                    {camera_join}
                    WHERE p.review_status IN %s
                      {camera_filter}
                    GROUP BY p.classification, cc.scenario, cc.display_name
                    ORDER BY count DESC
                ''', (status_list,) + tuple(camera_params))
            classifications = [dict(row) for row in cursor.fetchall()]

            # All configured classes for reclassify suggestions
            cursor.execute('''
                SELECT name, scenario, display_name
                FROM classification_classes
                WHERE is_active = true
                ORDER BY scenario, display_name
            ''')
            all_classes = [dict(row) for row in cursor.fetchall()]

            # Pending count for badge
            cursor.execute(f'''
                SELECT COUNT(*) AS cnt
                FROM ai_predictions p
                {camera_join}
                WHERE p.review_status IN ('pending', 'processing')
                  {camera_filter}
            ''', tuple(camera_params))
            pending_count = cursor.fetchone()['cnt']

            # Rejected count for badge
            cursor.execute(f'''
                SELECT COUNT(*) AS cnt
                FROM ai_predictions p
                {camera_join}
                WHERE p.review_status IN ('rejected', 'auto_rejected')
                  {camera_filter}
            ''', tuple(camera_params))
            rejected_count = cursor.fetchone()['cnt']

            # Reject reason breakdown (for reason filter in rejected mode)
            reject_reasons = []
            if status_param == 'rejected':
                cursor.execute(f'''
                    SELECT COALESCE(p.corrected_tags->>'actual_class', '') AS reason,
                           COUNT(*) AS count
                    FROM ai_predictions p
                    {camera_join}
                    WHERE p.review_status IN ('rejected', 'auto_rejected')
                      {camera_filter}
                    GROUP BY reason
                    ORDER BY count DESC
                ''', tuple(camera_params))
                reject_reasons = [dict(row) for row in cursor.fetchall()]

        return jsonify({
            'success': True,
            'scenarios': scenarios,
            'classifications': classifications,
            'all_classes': all_classes,
            'pending_count': pending_count,
            'rejected_count': rejected_count,
            'reject_reasons': reject_reasons
        })

    except Exception as e:
        logger.error(f'Failed to get gallery filters: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


@training_gallery_bp.route('/api/training-gallery/items')
def get_gallery_items():
    """Main gallery feed with pagination, filtering, and cluster grouping."""
    try:
        scenario = request.args.get('scenario')
        classification = request.args.get('classification')
        status_mode = request.args.get('status', 'approved')
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 60, type=int)
        sort = request.args.get('sort', 'confidence')
        camera = request.args.get('camera')

        offset = (page - 1) * per_page

        # Build filter fragments for SQL injection safety
        filter_clauses = []
        filter_params = []

        if classification:
            filter_clauses.append('p.classification = %s')
            filter_params.append(classification)
        if scenario:
            filter_clauses.append('cc.scenario = %s')
            filter_params.append(scenario)

        # Build the classification join and where clauses
        cc_join = 'LEFT JOIN classification_classes cc ON cc.name = p.classification'
        if scenario:
            # Need the join for filtering by scenario
            pass

        filter_sql = ''
        if filter_clauses:
            filter_sql = ' AND ' + ' AND '.join(filter_clauses)

        # Sort order
        if sort == 'date':
            order_by = 'created_at DESC'
        else:
            order_by = 'confidence DESC'

        # ── Pending mode: simple flat query, no clustering ──
        if status_mode == 'pending':
            scenario_join = ''
            if scenario:
                scenario_join = 'JOIN classification_classes cc ON cc.name = p.classification'

            camera_filter = ''
            if camera:
                camera_filter = ' AND v.camera_id = %s'
                filter_params.append(camera)

            pending_query = f'''
                SELECT
                    NULL::text AS cluster_type,
                    NULL::text AS cluster_id,
                    1::bigint AS cluster_count,
                    p.id,
                    p.classification,
                    p.confidence,
                    p.bbox_x,
                    p.bbox_y,
                    p.bbox_width,
                    p.bbox_height,
                    p.video_id,
                    p.scenario,
                    p.created_at,
                    v.thumbnail_path,
                    v.width AS video_width,
                    v.height AS video_height,
                    v.camera_id
                FROM ai_predictions p
                JOIN videos v ON p.video_id = v.id
                {scenario_join}
                WHERE p.review_status IN ('pending', 'processing')
                  {filter_sql}
                  {camera_filter}
                ORDER BY {order_by}
            '''

            count_query = f'SELECT COUNT(*) AS total FROM ({pending_query}) AS counted'
            paginated_query = pending_query + ' LIMIT %s OFFSET %s'

            with get_cursor(commit=False) as cursor:
                cursor.execute(count_query, filter_params)
                total = cursor.fetchone()['total']
                cursor.execute(paginated_query, filter_params + [per_page, offset])
                rows = cursor.fetchall()

            items = []
            for row in rows:
                item = dict(row)
                if item.get('created_at'):
                    item['created_at'] = item['created_at'].isoformat()
                if item.get('confidence') is not None:
                    item['confidence'] = float(item['confidence'])
                item['crop_url'] = f'/api/training-gallery/crop/{item["id"]}'
                items.append(item)

            pages = (total + per_page - 1) // per_page if per_page > 0 else 1
            return jsonify({
                'success': True,
                'items': items,
                'total': total,
                'page': page,
                'pages': pages,
            })

        # ── Rejected mode: flat query, no clustering, include reject reason ──
        elif status_mode == 'rejected':
            scenario_join = ''
            if scenario:
                scenario_join = 'JOIN classification_classes cc ON cc.name = p.classification'

            camera_filter = ''
            if camera:
                camera_filter = ' AND v.camera_id = %s'
                filter_params.append(camera)

            # Reject reason multi-filter (__none__ sentinel = no reason set)
            reject_reasons_param = request.args.get('reject_reasons', '')
            reason_filter = ''
            if reject_reasons_param:
                reason_list = [r.strip() for r in reject_reasons_param.split(',') if r.strip()]
                if reason_list:
                    has_none = '__none__' in reason_list
                    named_reasons = [r for r in reason_list if r != '__none__']
                    reason_parts = []
                    if named_reasons:
                        reason_parts.append("p.corrected_tags->>'actual_class' IN %s")
                        filter_params.append(tuple(named_reasons))
                    if has_none:
                        reason_parts.append("(p.corrected_tags->>'actual_class' IS NULL OR p.corrected_tags->>'actual_class' = '')")
                    if reason_parts:
                        reason_filter = ' AND (' + ' OR '.join(reason_parts) + ')'

            rejected_query = f'''
                SELECT
                    NULL::text AS cluster_type,
                    NULL::text AS cluster_id,
                    1::bigint AS cluster_count,
                    p.id,
                    p.classification,
                    p.confidence,
                    p.bbox_x,
                    p.bbox_y,
                    p.bbox_width,
                    p.bbox_height,
                    p.video_id,
                    p.scenario,
                    p.created_at,
                    v.thumbnail_path,
                    v.width AS video_width,
                    v.height AS video_height,
                    v.camera_id,
                    p.corrected_tags->>'actual_class' AS reject_reason,
                    p.review_notes
                FROM ai_predictions p
                JOIN videos v ON p.video_id = v.id
                {scenario_join}
                WHERE p.review_status IN ('rejected', 'auto_rejected')
                  {filter_sql}
                  {camera_filter}
                  {reason_filter}
                ORDER BY {order_by}
            '''

            count_query = f'SELECT COUNT(*) AS total FROM ({rejected_query}) AS counted'
            paginated_query = rejected_query + ' LIMIT %s OFFSET %s'

            with get_cursor(commit=False) as cursor:
                cursor.execute(count_query, filter_params)
                total = cursor.fetchone()['total']
                cursor.execute(paginated_query, filter_params + [per_page, offset])
                rows = cursor.fetchall()

            items = []
            for row in rows:
                item = dict(row)
                if item.get('created_at'):
                    item['created_at'] = item['created_at'].isoformat()
                if item.get('confidence') is not None:
                    item['confidence'] = float(item['confidence'])
                item['crop_url'] = f'/api/training-gallery/crop/{item["id"]}'
                items.append(item)

            pages = (total + per_page - 1) // per_page if per_page > 0 else 1
            return jsonify({
                'success': True,
                'items': items,
                'total': total,
                'page': page,
                'pages': pages,
            })

        # ── Approved mode: clustered UNION query ──

        # We need to pass params in order for the UNION ALL query.
        # Each branch may have different param sets.
        # track branch params: filter_params for WHERE + cluster_count_params for subquery
        # group branch params: filter_params for WHERE + cluster_count_params for subquery
        # standalone branch params: filter_params for WHERE

        # Build scenario join snippet (needed when filtering by scenario)
        scenario_join = ''
        if scenario:
            scenario_join = 'JOIN classification_classes cc ON cc.name = p.classification'

        camera_filter_sql = ''
        camera_params = []
        if camera:
            camera_filter_sql = ' AND v.camera_id = %s'
            camera_params = [camera]

        union_query = f'''
            SELECT *
            FROM (
                -- Clustered by camera_object_track
                SELECT * FROM (
                    SELECT DISTINCT ON (p.camera_object_track_id)
                        'track'::text AS cluster_type,
                        p.camera_object_track_id::text AS cluster_id,
                        (SELECT COUNT(*)
                         FROM ai_predictions p2
                         WHERE p2.camera_object_track_id = p.camera_object_track_id
                           AND p2.review_status IN ('approved', 'auto_approved')
                           ) AS cluster_count,
                        p.id,
                        p.classification,
                        p.confidence,
                        p.bbox_x,
                        p.bbox_y,
                        p.bbox_width,
                        p.bbox_height,
                        p.video_id,
                        p.scenario,
                        p.created_at,
                        v.thumbnail_path,
                        v.width AS video_width,
                        v.height AS video_height,
                        v.camera_id
                    FROM ai_predictions p
                    JOIN videos v ON p.video_id = v.id
                    JOIN camera_object_tracks t ON t.id = p.camera_object_track_id
                    {scenario_join}
                    WHERE p.review_status IN ('approved', 'auto_approved')
                      AND p.camera_object_track_id IS NOT NULL
                      -- Single-class check
                      AND (SELECT COUNT(DISTINCT p3.classification)
                           FROM ai_predictions p3
                           WHERE p3.camera_object_track_id = p.camera_object_track_id
                             AND p3.review_status IN ('approved', 'auto_approved')) = 1
                      -- Velocity check: reject moving vehicles (> 0.1 px/s)
                      AND (
                          t.last_seen - t.first_seen = 0
                          OR (SELECT (MAX(p4.bbox_x) - MIN(p4.bbox_x)
                                    + MAX(p4.bbox_y) - MIN(p4.bbox_y))::float
                                    / GREATEST(t.last_seen - t.first_seen, 1)
                              FROM ai_predictions p4
                              WHERE p4.camera_object_track_id = p.camera_object_track_id
                                AND p4.review_status IN ('approved', 'auto_approved')
                             ) < 0.1
                      )
                      {filter_sql}
                      {camera_filter_sql}
                    ORDER BY p.camera_object_track_id, p.confidence DESC
                ) AS track_clusters

                UNION ALL

                -- Clustered by prediction_group
                SELECT * FROM (
                    SELECT DISTINCT ON (p.prediction_group_id)
                        'group'::text AS cluster_type,
                        p.prediction_group_id::text AS cluster_id,
                        (SELECT COUNT(*)
                         FROM ai_predictions p2
                         WHERE p2.prediction_group_id = p.prediction_group_id
                           AND p2.review_status IN ('approved', 'auto_approved')
                           ) AS cluster_count,
                        p.id,
                        p.classification,
                        p.confidence,
                        p.bbox_x,
                        p.bbox_y,
                        p.bbox_width,
                        p.bbox_height,
                        p.video_id,
                        p.scenario,
                        p.created_at,
                        v.thumbnail_path,
                        v.width AS video_width,
                        v.height AS video_height,
                        v.camera_id
                    FROM ai_predictions p
                    JOIN videos v ON p.video_id = v.id
                    JOIN prediction_groups pg ON pg.id = p.prediction_group_id
                    {scenario_join}
                    WHERE p.review_status IN ('approved', 'auto_approved')
                      AND p.prediction_group_id IS NOT NULL
                      AND p.camera_object_track_id IS NULL
                      -- Single-class check
                      AND (SELECT COUNT(DISTINCT p3.classification)
                           FROM ai_predictions p3
                           WHERE p3.prediction_group_id = p.prediction_group_id
                             AND p3.review_status IN ('approved', 'auto_approved')) = 1
                      -- Velocity check: reject moving vehicles (> 0.1 px/s)
                      AND (
                          COALESCE(pg.max_timestamp - pg.min_timestamp, 0) = 0
                          OR (SELECT (MAX(p4.bbox_x) - MIN(p4.bbox_x)
                                    + MAX(p4.bbox_y) - MIN(p4.bbox_y))::float
                                    / GREATEST(pg.max_timestamp - pg.min_timestamp, 1)
                              FROM ai_predictions p4
                              WHERE p4.prediction_group_id = p.prediction_group_id
                                AND p4.review_status IN ('approved', 'auto_approved')
                             ) < 0.1
                      )
                      {filter_sql}
                      {camera_filter_sql}
                    ORDER BY p.prediction_group_id, p.confidence DESC
                ) AS group_clusters

                UNION ALL

                -- Standalone (no cluster)
                SELECT
                    NULL::text AS cluster_type,
                    NULL::text AS cluster_id,
                    1::bigint AS cluster_count,
                    p.id,
                    p.classification,
                    p.confidence,
                    p.bbox_x,
                    p.bbox_y,
                    p.bbox_width,
                    p.bbox_height,
                    p.video_id,
                    p.scenario,
                    p.created_at,
                    v.thumbnail_path,
                    v.width AS video_width,
                    v.height AS video_height,
                    v.camera_id
                FROM ai_predictions p
                JOIN videos v ON p.video_id = v.id
                {scenario_join}
                WHERE p.review_status IN ('approved', 'auto_approved')
                  AND (
                    -- No track or group
                    (p.prediction_group_id IS NULL AND p.camera_object_track_id IS NULL)
                    -- Track with mixed classes = not a real single-object cluster
                    OR (p.camera_object_track_id IS NOT NULL
                        AND (SELECT COUNT(DISTINCT p3.classification)
                             FROM ai_predictions p3
                             WHERE p3.camera_object_track_id = p.camera_object_track_id
                               AND p3.review_status IN ('approved', 'auto_approved')) > 1)
                    -- Track with high velocity = moving vehicle, not clusterable
                    OR (p.camera_object_track_id IS NOT NULL
                        AND (SELECT COUNT(DISTINCT p3.classification)
                             FROM ai_predictions p3
                             WHERE p3.camera_object_track_id = p.camera_object_track_id
                               AND p3.review_status IN ('approved', 'auto_approved')) = 1
                        AND EXISTS (
                            SELECT 1 FROM camera_object_tracks t
                            WHERE t.id = p.camera_object_track_id
                              AND t.last_seen - t.first_seen > 0
                              AND (SELECT (MAX(p4.bbox_x) - MIN(p4.bbox_x)
                                        + MAX(p4.bbox_y) - MIN(p4.bbox_y))::float
                                        / GREATEST(t.last_seen - t.first_seen, 1)
                                   FROM ai_predictions p4
                                   WHERE p4.camera_object_track_id = p.camera_object_track_id
                                     AND p4.review_status IN ('approved', 'auto_approved')
                                  ) >= 0.1
                        ))
                    -- Group with mixed classes = not a real cluster
                    OR (p.prediction_group_id IS NOT NULL AND p.camera_object_track_id IS NULL
                        AND (SELECT COUNT(DISTINCT p3.classification)
                             FROM ai_predictions p3
                             WHERE p3.prediction_group_id = p.prediction_group_id
                               AND p3.review_status IN ('approved', 'auto_approved')) > 1)
                    -- Group with high velocity = moving vehicle, not clusterable
                    OR (p.prediction_group_id IS NOT NULL AND p.camera_object_track_id IS NULL
                        AND (SELECT COUNT(DISTINCT p3.classification)
                             FROM ai_predictions p3
                             WHERE p3.prediction_group_id = p.prediction_group_id
                               AND p3.review_status IN ('approved', 'auto_approved')) = 1
                        AND EXISTS (
                            SELECT 1 FROM prediction_groups pg
                            WHERE pg.id = p.prediction_group_id
                              AND COALESCE(pg.max_timestamp - pg.min_timestamp, 0) > 0
                              AND (SELECT (MAX(p4.bbox_x) - MIN(p4.bbox_x)
                                        + MAX(p4.bbox_y) - MIN(p4.bbox_y))::float
                                        / GREATEST(pg.max_timestamp - pg.min_timestamp, 1)
                                   FROM ai_predictions p4
                                   WHERE p4.prediction_group_id = p.prediction_group_id
                                     AND p4.review_status IN ('approved', 'auto_approved')
                                  ) >= 0.1
                        ))
                  )
                  {filter_sql}
                  {camera_filter_sql}
            ) AS gallery_items
            ORDER BY {order_by}
        '''

        # Assemble params: for each of the 3 UNION branches:
        #   track branch:      cluster_count_params + filter_params
        #   group branch:      cluster_count_params + filter_params
        #   standalone branch: filter_params
        all_params = (
            filter_params + camera_params +
            filter_params + camera_params +
            filter_params + camera_params
        )

        count_query = f'SELECT COUNT(*) AS total FROM ({union_query}) AS counted'
        paginated_query = union_query + ' LIMIT %s OFFSET %s'

        with get_cursor(commit=False) as cursor:
            cursor.execute(count_query, all_params)
            total = cursor.fetchone()['total']

            cursor.execute(paginated_query, all_params + [per_page, offset])
            rows = cursor.fetchall()

        items = []
        for row in rows:
            item = dict(row)
            # Serialize datetime
            if item.get('created_at'):
                item['created_at'] = item['created_at'].isoformat()
            # Serialize floats
            if item.get('confidence') is not None:
                item['confidence'] = float(item['confidence'])
            # Add crop URL
            item['crop_url'] = f'/api/training-gallery/crop/{item["id"]}'
            items.append(item)

        pages = (total + per_page - 1) // per_page if per_page > 0 else 1

        return jsonify({
            'success': True,
            'items': items,
            'total': total,
            'page': page,
            'pages': pages
        })

    except Exception as e:
        logger.error(f'Failed to get gallery items: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


@training_gallery_bp.route('/api/training-gallery/cluster/<cluster_type>/<int:cluster_id>')
def get_cluster_items(cluster_type, cluster_id):
    """Get all predictions in a cluster."""
    try:
        if cluster_type not in ('track', 'group'):
            return jsonify({'success': False, 'error': 'cluster_type must be track or group'}), 400

        status_mode = request.args.get('status', 'approved')
        if status_mode == 'rejected':
            status_filter = ('rejected', 'auto_rejected')
        else:
            status_filter = ('approved', 'auto_approved')

        with get_cursor(commit=False) as cursor:
            if cluster_type == 'track':
                cursor.execute('''
                    SELECT p.id, p.classification, p.confidence,
                           p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
                           p.video_id, p.scenario, p.created_at, p.review_status,
                           v.thumbnail_path, v.width AS video_width, v.height AS video_height,
                           v.camera_id,
                           p.corrected_tags->>'actual_class' AS reject_reason
                    FROM ai_predictions p
                    JOIN videos v ON p.video_id = v.id
                    WHERE p.camera_object_track_id = %s
                      AND p.review_status IN %s
                    ORDER BY p.confidence DESC
                ''', (cluster_id, status_filter))
            else:
                cursor.execute('''
                    SELECT p.id, p.classification, p.confidence,
                           p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
                           p.video_id, p.scenario, p.created_at, p.review_status,
                           v.thumbnail_path, v.width AS video_width, v.height AS video_height,
                           v.camera_id,
                           p.corrected_tags->>'actual_class' AS reject_reason
                    FROM ai_predictions p
                    JOIN videos v ON p.video_id = v.id
                    WHERE p.prediction_group_id = %s
                      AND p.review_status IN %s
                    ORDER BY p.confidence DESC
                ''', (cluster_id, status_filter))

            rows = cursor.fetchall()

        items = []
        for row in rows:
            item = dict(row)
            if item.get('created_at'):
                item['created_at'] = item['created_at'].isoformat()
            if item.get('confidence') is not None:
                item['confidence'] = float(item['confidence'])
            item['crop_url'] = f'/api/training-gallery/crop/{item["id"]}'
            items.append(item)

        return jsonify({
            'success': True,
            'cluster_type': cluster_type,
            'cluster_id': cluster_id,
            'items': items,
            'count': len(items)
        })

    except Exception as e:
        logger.error(f'Failed to get cluster items for {cluster_type}/{cluster_id}: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


@training_gallery_bp.route('/api/training-gallery/cameras')
def get_gallery_cameras():
    """Return distinct camera names from predictions in the pending queue."""
    try:
        status_mode = request.args.get('status', 'pending')
        if status_mode == 'pending':
            status_list = ('pending', 'processing')
        elif status_mode == 'rejected':
            status_list = ('rejected', 'auto_rejected')
        else:
            status_list = ('approved', 'auto_approved')

        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT v.camera_id as name, COUNT(DISTINCT p.id) as count
                FROM ai_predictions p
                JOIN videos v ON v.id = p.video_id
                WHERE p.review_status IN %s
                  AND v.camera_id IS NOT NULL
                GROUP BY v.camera_id
                ORDER BY count DESC
            ''', (status_list,))
            cameras = [dict(row) for row in cursor.fetchall()]

        return jsonify({'success': True, 'cameras': cameras})
    except Exception as e:
        logger.error(f'Failed to get gallery cameras: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


@training_gallery_bp.route('/api/training-gallery/bulk-action', methods=['POST'])
def bulk_gallery_action():
    """Bulk operations on selected predictions."""
    if not g.can_write:
        return jsonify({'success': False, 'error': 'Write access required'}), 403

    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'JSON body required'}), 400

        action = data.get('action')
        prediction_ids = data.get('prediction_ids', [])
        new_classification = data.get('new_classification')
        actual_class = data.get('actual_class')

        if action not in ('reclassify', 'requeue', 'remove', 'approve', 'update_reason'):
            return jsonify({'success': False, 'error': 'action must be reclassify, requeue, remove, approve, or update_reason'}), 400

        if not prediction_ids or not isinstance(prediction_ids, list):
            return jsonify({'success': False, 'error': 'prediction_ids array required'}), 400

        if action == 'reclassify' and not new_classification:
            return jsonify({'success': False, 'error': 'new_classification required for reclassify action'}), 400

        # Normalize classification to lowercase to match classification_classes table
        if new_classification:
            new_classification = new_classification.lower().strip()

        with get_cursor(commit=True) as cursor:
            if action == 'reclassify':
                logger.info(f'Reclassify: ids={prediction_ids}, new_class={new_classification}')
                cursor.execute('SELECT id, review_status, classification FROM ai_predictions WHERE id = ANY(%s)', (prediction_ids,))
                logger.info(f'Reclassify PRE-UPDATE: {[dict(r) for r in cursor.fetchall()]}')

                cursor.execute('''
                    UPDATE ai_predictions
                    SET classification = %s,
                        review_status = 'approved',
                        reviewed_by = COALESCE(reviewed_by, 'gallery_reclassify'),
                        reviewed_at = COALESCE(reviewed_at, NOW()),
                        corrected_tags = COALESCE(corrected_tags, '{}'::jsonb)
                            || jsonb_build_object('gallery_reclassify', %s, 'vehicle_subtype', %s)
                    WHERE id = ANY(%s)
                ''', (new_classification, new_classification, new_classification, prediction_ids))
                affected = cursor.rowcount

                cursor.execute('SELECT id, review_status, classification FROM ai_predictions WHERE id = ANY(%s)', (prediction_ids,))
                logger.info(f'Reclassify POST-UPDATE: {[dict(r) for r in cursor.fetchall()]}, affected={affected}')

            elif action == 'approve':
                cursor.execute('''
                    UPDATE ai_predictions
                    SET review_status = 'approved',
                        reviewed_by = 'gallery_approval',
                        reviewed_at = NOW(),
                        corrected_tags = COALESCE(corrected_tags, '{}'::jsonb)
                            || jsonb_build_object('vehicle_subtype', classification)
                    WHERE id = ANY(%s)
                ''', (prediction_ids,))
                affected = cursor.rowcount

            elif action == 'requeue':
                cursor.execute('''
                    UPDATE ai_predictions
                    SET review_status = 'needs_reclassification',
                        routed_by = 'gallery_reclassify'
                    WHERE id = ANY(%s)
                ''', (prediction_ids,))
                affected = cursor.rowcount

            elif action == 'remove':
                if actual_class:
                    cursor.execute('''
                        UPDATE ai_predictions
                        SET review_status = 'rejected',
                            reviewed_by = 'gallery_removal',
                            reviewed_at = NOW(),
                            corrected_tags = COALESCE(corrected_tags, '{}'::jsonb)
                                || jsonb_build_object('actual_class', %s)
                        WHERE id = ANY(%s)
                    ''', (actual_class, prediction_ids))
                else:
                    cursor.execute('''
                        UPDATE ai_predictions
                        SET review_status = 'rejected',
                            reviewed_by = 'gallery_removal',
                            reviewed_at = NOW()
                        WHERE id = ANY(%s)
                    ''', (prediction_ids,))
                affected = cursor.rowcount

            elif action == 'update_reason':
                new_reason = data.get('new_reason')
                if not new_reason:
                    return jsonify({'success': False, 'error': 'new_reason required for update_reason action'}), 400
                cursor.execute('''
                    UPDATE ai_predictions
                    SET corrected_tags = COALESCE(corrected_tags, '{}'::jsonb)
                        || jsonb_build_object('actual_class', %s)
                    WHERE id = ANY(%s)
                ''', (new_reason, prediction_ids))
                affected = cursor.rowcount

        # Create training annotations for approved predictions
        annotations_created = 0
        if action in ('approve', 'reclassify'):
            for pid in prediction_ids:
                ann_id = db.approve_prediction_to_annotation(pid)
                if ann_id:
                    annotations_created += 1

        # Cascade reject child predictions when parent is removed
        cascaded = 0
        if action == 'remove':
            for pid in prediction_ids:
                cascaded += db.cascade_reject_children(pid, reviewed_by='gallery_removal')

        # Update model approval stats after approve/reclassify
        if action in ('approve', 'reclassify') and affected > 0:
            try:
                with get_cursor(commit=False) as cursor:
                    cursor.execute('''
                        SELECT DISTINCT model_name, model_version
                        FROM ai_predictions WHERE id = ANY(%s)
                    ''', (prediction_ids,))
                    for row in cursor.fetchall():
                        db.update_model_approval_stats(row['model_name'], row['model_version'])
            except Exception as e:
                logger.warning(f'Failed to update model approval stats: {e}')

        result = {'success': True, 'affected': affected}
        if action in ('approve', 'reclassify'):
            result['annotations_created'] = annotations_created
        if action == 'remove':
            result['cascaded'] = cascaded
        return jsonify(result)

    except Exception as e:
        logger.error(f'Failed to execute bulk gallery action: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


@training_gallery_bp.route('/api/training-gallery/crop/<int:prediction_id>')
def get_prediction_crop(prediction_id):
    """Serve a cropped image of the prediction bbox from the video thumbnail."""
    from PIL import Image

    try:
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT p.id, p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
                       v.thumbnail_path, v.width AS video_width, v.height AS video_height
                FROM ai_predictions p
                JOIN videos v ON p.video_id = v.id
                WHERE p.id = %s
            ''', (prediction_id,))
            row = cursor.fetchone()

        if not row or not row['thumbnail_path']:
            return jsonify({'error': 'Not found'}), 404

        # Check cache
        CROPS_DIR.mkdir(parents=True, exist_ok=True)
        crop_path = CROPS_DIR / f'gallery_{prediction_id}.jpg'

        if not crop_path.exists():
            tp = row['thumbnail_path']
            thumb_path = Path(tp) if tp.startswith('/') else THUMBNAIL_DIR / tp
            if not thumb_path.exists():
                return jsonify({'error': 'Thumbnail not found'}), 404

            img = Image.open(thumb_path)

            # Scale bbox from video coords to thumbnail coords
            thumb_w, thumb_h = img.size
            vid_w = row['video_width'] or thumb_w
            vid_h = row['video_height'] or thumb_h
            scale_x = thumb_w / vid_w
            scale_y = thumb_h / vid_h

            bx = int((row['bbox_x'] or 0) * scale_x)
            by = int((row['bbox_y'] or 0) * scale_y)
            bw = int((row['bbox_width'] or 0) * scale_x)
            bh = int((row['bbox_height'] or 0) * scale_y)

            # Add 3% padding (tight crop for better embeddings)
            pad_x = int(bw * 0.03)
            pad_y = int(bh * 0.03)
            left = max(0, bx - pad_x)
            top = max(0, by - pad_y)
            right = min(thumb_w, bx + bw + pad_x)
            bottom = min(thumb_h, by + bh + pad_y)

            # Guard against degenerate bbox
            if right <= left or bottom <= top:
                left, top, right, bottom = 0, 0, thumb_w, thumb_h

            crop = img.crop((left, top, right, bottom))

            # Resize to max 400px on longest side
            max_dim = 400
            if max(crop.size) > max_dim:
                ratio = max_dim / max(crop.size)
                new_size = (int(crop.size[0] * ratio), int(crop.size[1] * ratio))
                crop = crop.resize(new_size, Image.LANCZOS)

            crop.save(str(crop_path), 'JPEG', quality=85)

        resp = send_file(str(crop_path), mimetype='image/jpeg')
        resp.headers['Cache-Control'] = 'public, max-age=604800'  # 7 days
        return resp

    except Exception as e:
        logger.error(f'Failed to generate crop for prediction {prediction_id}: {e}')
        return jsonify({'error': 'Failed to generate crop'}), 500


@training_gallery_bp.route('/api/training-gallery/full-image/<int:prediction_id>')
def get_prediction_full_image(prediction_id):
    """Serve the full thumbnail image for overlay-mode predictions (documents)."""
    from PIL import Image
    try:
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT p.id, p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
                       v.thumbnail_path, v.width AS video_width, v.height AS video_height
                FROM ai_predictions p
                JOIN videos v ON p.video_id = v.id
                WHERE p.id = %s
            ''', (prediction_id,))
            row = cursor.fetchone()

        if not row or not row['thumbnail_path']:
            return jsonify({'error': 'Not found'}), 404

        tp = row['thumbnail_path']
        thumb_path = Path(tp) if tp.startswith('/') else THUMBNAIL_DIR / tp
        if not thumb_path.exists():
            return jsonify({'error': 'Thumbnail not found'}), 404

        # Resize to max 600px for gallery display
        CROPS_DIR.mkdir(parents=True, exist_ok=True)
        full_path = CROPS_DIR / f'gallery_full_{prediction_id}.jpg'

        if not full_path.exists():
            img = Image.open(thumb_path)
            max_dim = 600
            if max(img.size) > max_dim:
                ratio = max_dim / max(img.size)
                new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
                img = img.resize(new_size, Image.LANCZOS)
            img.save(str(full_path), 'JPEG', quality=85)

        resp = send_file(str(full_path), mimetype='image/jpeg')
        resp.headers['Cache-Control'] = 'public, max-age=604800'
        return resp
    except Exception as e:
        logger.error(f'Failed to generate full image for prediction {prediction_id}: {e}')
        return jsonify({'error': 'Failed to generate image'}), 500


FASTREID_URL = 'http://localhost:5061'
SIMILARITY_LIMIT = 200  # max items to return in similarity results
SIMILARITY_THRESHOLD = 0.55  # minimum cosine similarity to include in results


@training_gallery_bp.route('/api/training-gallery/similar/<int:seed_id>')
def get_similar_items(seed_id):
    """Find visually similar predictions using pgvector cosine similarity."""
    try:
        scenario = request.args.get('scenario')
        classification = request.args.get('classification')
        status_mode = request.args.get('status', 'approved')
        camera = request.args.get('camera')
        reject_reasons_param = request.args.get('reject_reasons', '')

        # 1. Get or compute seed embedding + seed classification
        seed_vec_str = _get_or_compute_embedding(seed_id)
        if not seed_vec_str:
            return jsonify({'success': False, 'error': 'Could not generate embedding for seed'}), 400

        # Get seed item full data + color histogram for display and re-ranking
        seed_item = None
        seed_classification = None
        seed_color_hist = None
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT p.id, p.classification, p.confidence,
                       p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
                       p.video_id, p.scenario, p.created_at,
                       v.thumbnail_path, v.width AS video_width, v.height AS video_height,
                       v.camera_id,
                       p.corrected_tags->>'actual_class' AS reject_reason,
                       pe.color_hist
                FROM ai_predictions p
                JOIN videos v ON p.video_id = v.id
                LEFT JOIN prediction_embeddings pe ON pe.prediction_id = p.id
                WHERE p.id = %s
            ''', (seed_id,))
            seed_row = cursor.fetchone()
            if seed_row:
                seed_classification = seed_row['classification']
                seed_color_hist = seed_row.get('color_hist')
                seed_item = dict(seed_row)
                seed_item.pop('color_hist', None)
                seed_item['similarity'] = 1.0
                if seed_item.get('created_at'):
                    seed_item['created_at'] = seed_item['created_at'].isoformat()
                if seed_item.get('confidence') is not None:
                    seed_item['confidence'] = float(seed_item['confidence'])
                seed_item['crop_url'] = f'/api/training-gallery/crop/{seed_item["id"]}'
                seed_item['cluster_type'] = None
                seed_item['cluster_id'] = None
                seed_item['cluster_count'] = 1
                seed_item['is_seed'] = True

        # 2. Build filters
        if status_mode == 'pending':
            status_list = ('pending', 'processing')
        elif status_mode == 'rejected':
            status_list = ('rejected', 'auto_rejected')
        else:
            status_list = ('approved', 'auto_approved')

        filter_clauses = []
        filter_params = []
        if classification:
            filter_clauses.append('p.classification = %s')
            filter_params.append(classification)
        if scenario:
            filter_clauses.append('cc.scenario = %s')
            filter_params.append(scenario)

        scenario_join = ''
        if scenario:
            scenario_join = 'JOIN classification_classes cc ON cc.name = p.classification'

        camera_filter = ''
        if camera:
            camera_filter = ' AND v.camera_id = %s'
            filter_params.append(camera)

        filter_sql = ''
        if filter_clauses:
            filter_sql = ' AND ' + ' AND '.join(filter_clauses)

        reason_filter = ''
        if status_mode == 'rejected' and reject_reasons_param:
            reason_list = [r.strip() for r in reject_reasons_param.split(',') if r.strip()]
            if reason_list:
                has_none = '__none__' in reason_list
                named_reasons = [r for r in reason_list if r != '__none__']
                reason_parts = []
                if named_reasons:
                    reason_parts.append("p.corrected_tags->>'actual_class' IN %s")
                    filter_params.append(tuple(named_reasons))
                if has_none:
                    reason_parts.append("(p.corrected_tags->>'actual_class' IS NULL OR p.corrected_tags->>'actual_class' = '')")
                if reason_parts:
                    reason_filter = ' AND (' + ' OR '.join(reason_parts) + ')'

        # 3. Query with pgvector cosine distance — searches ALL matching items
        with get_cursor(commit=False) as cursor:
            cursor.execute(f'''
                SELECT p.id, p.classification, p.confidence,
                       p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
                       p.video_id, p.scenario, p.created_at,
                       v.thumbnail_path, v.width AS video_width, v.height AS video_height,
                       v.camera_id,
                       p.corrected_tags->>'actual_class' AS reject_reason,
                       1 - (pe.embedding <=> %s::vector) AS similarity,
                       pe.color_hist
                FROM ai_predictions p
                JOIN videos v ON p.video_id = v.id
                JOIN prediction_embeddings pe ON pe.prediction_id = p.id
                {scenario_join}
                WHERE p.review_status IN %s
                  AND p.id != %s
                  {filter_sql}
                  {camera_filter}
                  {reason_filter}
                  AND 1 - (pe.embedding <=> %s::vector) >= %s
                ORDER BY pe.embedding <=> %s::vector
                LIMIT %s
            ''', (seed_vec_str, status_list, seed_id) + tuple(filter_params) +
                 (seed_vec_str, SIMILARITY_THRESHOLD, seed_vec_str, SIMILARITY_LIMIT))
            rows = cursor.fetchall()

        scored_items = []
        for row in rows:
            item = dict(row)
            embed_sim = float(item['similarity'])
            # Re-rank with color histogram boost if available
            item_hist = item.pop('color_hist', None)
            if seed_color_hist and item_hist:
                color_sim = hist_intersection(seed_color_hist, item_hist)
                item['similarity'] = round(0.75 * embed_sim + 0.25 * color_sim, 4)
            else:
                item['similarity'] = round(embed_sim, 4)
            if item.get('created_at'):
                item['created_at'] = item['created_at'].isoformat()
            if item.get('confidence') is not None:
                item['confidence'] = float(item['confidence'])
            item['crop_url'] = f'/api/training-gallery/crop/{item["id"]}'
            item['cluster_type'] = None
            item['cluster_id'] = None
            item['cluster_count'] = 1
            scored_items.append(item)

        # Re-sort by combined score
        scored_items.sort(key=lambda x: x['similarity'], reverse=True)

        return jsonify({
            'success': True,
            'seed_id': seed_id,
            'seed_classification': seed_classification,
            'seed_item': seed_item,
            'items': scored_items,
            'total': len(scored_items)
        })

    except Exception as e:
        logger.error(f'Failed to find similar items for {seed_id}: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


def _get_or_compute_embedding(prediction_id):
    """Get embedding vector string from DB, or compute via FastReID and store it."""
    with get_cursor(commit=False) as cursor:
        cursor.execute('SELECT embedding::text FROM prediction_embeddings WHERE prediction_id = %s', (prediction_id,))
        row = cursor.fetchone()
        if row:
            return row['embedding']

    # Not cached — compute it
    seed_crop = CROPS_DIR / f'gallery_{prediction_id}.jpg'
    if not seed_crop.exists():
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT p.id, p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
                       v.thumbnail_path, v.width AS video_width, v.height AS video_height
                FROM ai_predictions p
                JOIN videos v ON p.video_id = v.id
                WHERE p.id = %s
            ''', (prediction_id,))
            row = cursor.fetchone()
        if not row or not row['thumbnail_path']:
            return None
        _generate_crop(row, seed_crop)

    if not seed_crop.exists():
        return None

    with open(str(seed_crop), 'rb') as f:
        seed_b64 = base64.b64encode(f.read()).decode('ascii')
    resp = http_requests.post(f'{FASTREID_URL}/embed/dino', json={'image': seed_b64}, timeout=30)
    resp.raise_for_status()
    embedding = resp.json()['embedding']
    vec_str = '[' + ','.join(str(v) for v in embedding) + ']'

    # Store for future use
    with get_cursor(commit=True) as cursor:
        cursor.execute(
            'INSERT INTO prediction_embeddings (prediction_id, embedding) VALUES (%s, %s) ON CONFLICT DO NOTHING',
            (prediction_id, vec_str)
        )
    return vec_str


def _generate_crop(row, crop_path):
    """Generate a crop file for a prediction row."""
    from PIL import Image

    tp = row['thumbnail_path']
    if not tp:
        return
    thumb_path = Path(tp) if tp.startswith('/') else THUMBNAIL_DIR / tp
    if not thumb_path.exists():
        return

    img = Image.open(thumb_path)
    thumb_w, thumb_h = img.size
    vid_w = row.get('video_width') or thumb_w
    vid_h = row.get('video_height') or thumb_h
    scale_x = thumb_w / vid_w
    scale_y = thumb_h / vid_h

    bx = int((row.get('bbox_x') or 0) * scale_x)
    by = int((row.get('bbox_y') or 0) * scale_y)
    bw = int((row.get('bbox_width') or 0) * scale_x)
    bh = int((row.get('bbox_height') or 0) * scale_y)

    pad_x = int(bw * 0.03)
    pad_y = int(bh * 0.03)
    left = max(0, bx - pad_x)
    top = max(0, by - pad_y)
    right = min(thumb_w, bx + bw + pad_x)
    bottom = min(thumb_h, by + bh + pad_y)

    if right <= left or bottom <= top:
        left, top, right, bottom = 0, 0, thumb_w, thumb_h

    crop = img.crop((left, top, right, bottom))
    max_dim = 400
    if max(crop.size) > max_dim:
        ratio = max_dim / max(crop.size)
        new_size = (int(crop.size[0] * ratio), int(crop.size[1] * ratio))
        crop = crop.resize(new_size, Image.LANCZOS)

    CROPS_DIR.mkdir(parents=True, exist_ok=True)
    crop.save(str(crop_path), 'JPEG', quality=85)
