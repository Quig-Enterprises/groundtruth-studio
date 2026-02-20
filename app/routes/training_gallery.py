"""Training Image Gallery routes for Groundtruth Studio."""
from flask import Blueprint, request, jsonify, render_template, send_file, g
from pathlib import Path
from db_connection import get_cursor
from services import db, THUMBNAIL_DIR, BASE_DIR
import logging

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

        with get_cursor(commit=False) as cursor:
            # Scenarios with counts
            cursor.execute('''
                SELECT cc.scenario, COUNT(DISTINCT p.id) as count
                FROM ai_predictions p
                JOIN classification_classes cc ON cc.name = p.classification
                WHERE p.review_status IN ('approved', 'auto_approved')
                GROUP BY cc.scenario
                ORDER BY count DESC
            ''')
            scenarios = [dict(row) for row in cursor.fetchall()]

            # Classifications with counts, optionally filtered by scenario
            if scenario_param:
                cursor.execute('''
                    SELECT p.classification as name, cc.scenario, cc.display_name,
                           COUNT(*) as count
                    FROM ai_predictions p
                    JOIN classification_classes cc ON cc.name = p.classification
                    WHERE p.review_status IN ('approved', 'auto_approved')
                      AND cc.scenario = %s
                    GROUP BY p.classification, cc.scenario, cc.display_name
                    ORDER BY count DESC
                ''', (scenario_param,))
            else:
                cursor.execute('''
                    SELECT p.classification as name, cc.scenario, cc.display_name,
                           COUNT(*) as count
                    FROM ai_predictions p
                    JOIN classification_classes cc ON cc.name = p.classification
                    WHERE p.review_status IN ('approved', 'auto_approved')
                    GROUP BY p.classification, cc.scenario, cc.display_name
                    ORDER BY count DESC
                ''')
            classifications = [dict(row) for row in cursor.fetchall()]

        return jsonify({
            'success': True,
            'scenarios': scenarios,
            'classifications': classifications
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
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 60, type=int)
        sort = request.args.get('sort', 'confidence')

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

        # Subquery filter for cluster_count subqueries
        cluster_count_filter = ''
        cluster_count_params = []
        if classification:
            cluster_count_filter += ' AND p2.classification = %s'
            cluster_count_params.append(classification)

        # Sort order
        if sort == 'date':
            order_by = 'created_at DESC'
        else:
            order_by = 'confidence DESC'

        # We need to pass params in order for the UNION ALL query.
        # Each branch may have different param sets.
        # track branch params: filter_params for WHERE + cluster_count_params for subquery
        # group branch params: filter_params for WHERE + cluster_count_params for subquery
        # standalone branch params: filter_params for WHERE

        # Build scenario join snippet (needed when filtering by scenario)
        scenario_join = ''
        if scenario:
            scenario_join = 'JOIN classification_classes cc ON cc.name = p.classification'

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
                           {cluster_count_filter}) AS cluster_count,
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
                      AND p.camera_object_track_id IS NOT NULL
                      {filter_sql}
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
                           {cluster_count_filter}) AS cluster_count,
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
                      AND p.prediction_group_id IS NOT NULL
                      AND p.camera_object_track_id IS NULL
                      {filter_sql}
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
                  AND p.prediction_group_id IS NULL
                  AND p.camera_object_track_id IS NULL
                  {filter_sql}
            ) AS gallery_items
            ORDER BY {order_by}
        '''

        # Assemble params: for each of the 3 UNION branches:
        #   track branch:      cluster_count_params + filter_params
        #   group branch:      cluster_count_params + filter_params
        #   standalone branch: filter_params
        all_params = (
            cluster_count_params + filter_params +
            cluster_count_params + filter_params +
            filter_params
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

        with get_cursor(commit=False) as cursor:
            if cluster_type == 'track':
                cursor.execute('''
                    SELECT p.id, p.classification, p.confidence,
                           p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
                           p.video_id, p.scenario, p.created_at, p.review_status,
                           v.thumbnail_path, v.width AS video_width, v.height AS video_height,
                           v.camera_id
                    FROM ai_predictions p
                    JOIN videos v ON p.video_id = v.id
                    WHERE p.camera_object_track_id = %s
                      AND p.review_status IN ('approved', 'auto_approved')
                    ORDER BY p.confidence DESC
                ''', (cluster_id,))
            else:
                cursor.execute('''
                    SELECT p.id, p.classification, p.confidence,
                           p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
                           p.video_id, p.scenario, p.created_at, p.review_status,
                           v.thumbnail_path, v.width AS video_width, v.height AS video_height,
                           v.camera_id
                    FROM ai_predictions p
                    JOIN videos v ON p.video_id = v.id
                    WHERE p.prediction_group_id = %s
                      AND p.review_status IN ('approved', 'auto_approved')
                    ORDER BY p.confidence DESC
                ''', (cluster_id,))

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

        if action not in ('reclassify', 'requeue', 'remove'):
            return jsonify({'success': False, 'error': 'action must be reclassify, requeue, or remove'}), 400

        if not prediction_ids or not isinstance(prediction_ids, list):
            return jsonify({'success': False, 'error': 'prediction_ids array required'}), 400

        if action == 'reclassify' and not new_classification:
            return jsonify({'success': False, 'error': 'new_classification required for reclassify action'}), 400

        with get_cursor(commit=True) as cursor:
            if action == 'reclassify':
                cursor.execute('''
                    UPDATE ai_predictions
                    SET classification = %s,
                        corrected_tags = COALESCE(corrected_tags, '{}'::jsonb)
                            || jsonb_build_object('gallery_reclassify', %s)
                    WHERE id = ANY(%s)
                ''', (new_classification, new_classification, prediction_ids))
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
                cursor.execute('''
                    UPDATE ai_predictions
                    SET review_status = 'rejected',
                        reviewed_by = 'gallery_removal',
                        reviewed_at = NOW()
                    WHERE id = ANY(%s)
                ''', (prediction_ids,))
                affected = cursor.rowcount

        return jsonify({'success': True, 'affected': affected})

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
            thumb_path = THUMBNAIL_DIR / row['thumbnail_path']
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

            # Add 10% padding
            pad_x = int(bw * 0.1)
            pad_y = int(bh * 0.1)
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

        return send_file(str(crop_path), mimetype='image/jpeg')

    except Exception as e:
        logger.error(f'Failed to generate crop for prediction {prediction_id}: {e}')
        return jsonify({'error': 'Failed to generate crop'}), 500
