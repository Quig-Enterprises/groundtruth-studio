#!/usr/bin/env python3
"""
Backfill spatial scale models from existing approved predictions.

Iterates all approved ai_predictions with bbox + classification,
populates the spatial_scale_models table.

Usage:
    cd /opt/groundtruth-studio/app && python ../scripts/backfill_spatial_scale.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

from db_connection import init_connection_pool, get_cursor, close_connection_pool
from spatial_scale import SpatialScaleModel

BATCH_SIZE = 500


def main():
    print("Initializing connection pool...")
    init_connection_pool()
    model = SpatialScaleModel()

    try:
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT COUNT(*) AS cnt FROM ai_predictions
                WHERE review_status IN ('approved', 'auto_approved')
                  AND bbox_width > 0 AND bbox_height > 0
                  AND classification IS NOT NULL
            """)
            total = cursor.fetchone()['cnt']
            print(f"Found {total} approved predictions to process")

        offset = 0
        processed = 0
        errors = 0

        while offset < total:
            with get_cursor(commit=False) as cursor:
                cursor.execute("""
                    SELECT p.id, p.classification, p.bbox_x, p.bbox_y,
                           p.bbox_width, p.bbox_height, v.camera_id,
                           v.width AS frame_width, v.height AS frame_height
                    FROM ai_predictions p
                    JOIN videos v ON v.id = p.video_id
                    WHERE p.review_status IN ('approved', 'auto_approved')
                      AND p.bbox_width > 0 AND p.bbox_height > 0
                      AND p.classification IS NOT NULL
                      AND v.camera_id IS NOT NULL
                    ORDER BY p.id
                    LIMIT %s OFFSET %s
                """, (BATCH_SIZE, offset))
                rows = cursor.fetchall()

            if not rows:
                break

            for row in rows:
                try:
                    if not row['frame_width'] or not row['frame_height']:
                        continue
                    bbox = {
                        'x': row['bbox_x'], 'y': row['bbox_y'],
                        'width': row['bbox_width'], 'height': row['bbox_height']
                    }
                    model.record_observation(
                        row['camera_id'], row['classification'],
                        bbox, (row['frame_width'], row['frame_height'])
                    )
                    processed += 1
                except Exception as e:
                    errors += 1
                    if errors <= 5:
                        print(f"  Error on prediction {row['id']}: {e}")

            offset += BATCH_SIZE
            print(f"  Processed {min(offset, total)}/{total} ({processed} recorded, {errors} errors)")

        print(f"\nBackfill complete: {processed} observations recorded, {errors} errors")

        # Print stats
        with get_cursor(commit=False) as cursor:
            cursor.execute("SELECT COUNT(*) AS cnt FROM spatial_scale_models")
            print(f"Total spatial_scale_models rows: {cursor.fetchone()['cnt']}")
            cursor.execute("""
                SELECT COUNT(*) AS cnt FROM spatial_scale_models
                WHERE sample_count >= 20
            """)
            print(f"Cells with >= 20 observations (trusted): {cursor.fetchone()['cnt']}")

    finally:
        close_connection_pool()


if __name__ == '__main__':
    main()
