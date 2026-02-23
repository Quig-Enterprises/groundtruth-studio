#!/usr/bin/env python3
"""Batch extract vehicle ReID embeddings for approved predictions.

Crops vehicle bboxes from thumbnails, sends to Vehicle ReID API,
and stores 2048-dim embeddings in the embeddings table.
"""

import base64
import io
import logging
import sys
import time
import uuid

import requests
from PIL import Image

sys.path.insert(0, '/opt/groundtruth-studio/app')
from db_connection import init_connection_pool, get_cursor

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

REID_API_URL = 'http://localhost:5061'  # FastReID API (vehicle + person)
BATCH_SIZE = 32
TARGET_CAMERAS = ('mwcam8', 'mwcam9', 'mwparkinglot')


def get_predictions():
    """Fetch all approved vehicle predictions on target cameras that don't have embeddings yet."""
    with get_cursor() as cursor:
        cursor.execute("""
            SELECT p.id, p.video_id, p.camera_object_track_id,
                   p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
                   v.camera_id, v.thumbnail_path
            FROM ai_predictions p
            JOIN videos v ON p.video_id = v.id
            LEFT JOIN embeddings e ON e.source_image_path LIKE '%%prediction_' || p.id::text || '_%%'
                AND e.embedding_type = 'vehicle_appearance'
            WHERE v.camera_id IN %s
              AND p.scenario = 'vehicle_detection'
              AND p.review_status = 'approved'
              AND p.camera_object_track_id IS NOT NULL
              AND e.embedding_id IS NULL
            ORDER BY v.camera_id, p.id
        """, (TARGET_CAMERAS,))
        return cursor.fetchall()


def crop_vehicle(thumbnail_path, bbox_x, bbox_y, bbox_width, bbox_height):
    """Crop vehicle region from thumbnail image."""
    try:
        img = Image.open(thumbnail_path).convert('RGB')
        x1 = max(0, int(bbox_x))
        y1 = max(0, int(bbox_y))
        x2 = min(img.width, int(bbox_x + bbox_width))
        y2 = min(img.height, int(bbox_y + bbox_height))
        if x2 <= x1 or y2 <= y1:
            return None
        return img.crop((x1, y1, x2, y2))
    except Exception as e:
        logger.warning("Failed to crop %s: %s", thumbnail_path, e)
        return None


def image_to_base64(img):
    """Convert PIL image to base64 string."""
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=90)
    return base64.b64encode(buf.getvalue()).decode('utf-8')


def get_or_create_vehicle_identity(cursor, camera_id, track_id):
    """Get or create a vehicle identity for this track."""
    # Check if track already has an identity via existing embeddings
    cursor.execute("""
        SELECT e.identity_id FROM embeddings e
        WHERE e.embedding_type = 'vehicle_appearance'
          AND e.source_image_path LIKE %s
        LIMIT 1
    """, (f'%track_{track_id}_%',))
    row = cursor.fetchone()
    if row:
        return row['identity_id']

    # Create new vehicle identity
    identity_id = str(uuid.uuid4())
    cursor.execute("""
        INSERT INTO identities (identity_id, name, identity_type, first_seen, last_seen, metadata)
        VALUES (%s, %s, 'vehicle', NOW(), NOW(), '{}')
    """, (identity_id, f'vehicle_track_{track_id}'))
    return identity_id


def store_embeddings(predictions, embeddings_list):
    """Store embeddings in the database."""
    stored = 0
    with get_cursor() as cursor:
        for pred, embedding in zip(predictions, embeddings_list):
            if embedding is None:
                continue
            identity_id = get_or_create_vehicle_identity(
                cursor, pred['camera_id'], pred['camera_object_track_id']
            )
            source_path = f"prediction_{pred['id']}_track_{pred['camera_object_track_id']}_crop"
            cursor.execute("""
                INSERT INTO embeddings (identity_id, embedding_type, vector, confidence,
                                       source_image_path, camera_id, session_date)
                VALUES (%s, 'vehicle_appearance', %s, %s, %s, %s, CURRENT_DATE)
                ON CONFLICT DO NOTHING
            """, (
                identity_id,
                embedding,
                0.9,
                source_path,
                pred['camera_id'],
            ))
            stored += 1
    return stored


def extract_batch(predictions_batch):
    """Extract embeddings for a batch of predictions."""
    crops = []
    valid_preds = []

    for pred in predictions_batch:
        crop = crop_vehicle(
            pred['thumbnail_path'],
            pred['bbox_x'], pred['bbox_y'],
            pred['bbox_width'], pred['bbox_height']
        )
        if crop is not None:
            crops.append(crop)
            valid_preds.append(pred)

    if not crops:
        return [], []

    # Send batch to ReID API
    b64_images = [image_to_base64(c) for c in crops]
    try:
        resp = requests.post(
            f'{REID_API_URL}/embed-batch',
            json={'images': b64_images},
            timeout=30
        )
        resp.raise_for_status()
        data = resp.json()
        return valid_preds, data['embeddings']
    except Exception as e:
        logger.error("ReID API batch request failed: %s", e)
        return [], []


def main():
    init_connection_pool()

    # Check ReID API health
    try:
        resp = requests.get(f'{REID_API_URL}/health', timeout=5)
        health = resp.json()
        logger.info("ReID API healthy: model=%s, device=%s, dim=%d",
                     health['model'], health['device'], health['embedding_dim'])
    except Exception as e:
        logger.error("ReID API not available: %s", e)
        sys.exit(1)

    # Get predictions needing embeddings
    predictions = get_predictions()
    logger.info("Found %d predictions needing vehicle embeddings", len(predictions))

    if not predictions:
        logger.info("Nothing to do - all predictions already have embeddings")
        return

    total_stored = 0
    total_batches = (len(predictions) + BATCH_SIZE - 1) // BATCH_SIZE
    start_time = time.time()

    for i in range(0, len(predictions), BATCH_SIZE):
        batch = predictions[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1

        valid_preds, embeddings = extract_batch(batch)
        if valid_preds:
            stored = store_embeddings(valid_preds, embeddings)
            total_stored += stored

        elapsed = time.time() - start_time
        rate = total_stored / elapsed if elapsed > 0 else 0
        logger.info("Batch %d/%d: stored %d embeddings (total: %d, %.1f/sec)",
                     batch_num, total_batches, len(valid_preds), total_stored, rate)

    elapsed = time.time() - start_time
    logger.info("Done! Extracted %d embeddings in %.1f seconds (%.1f/sec)",
                total_stored, elapsed, total_stored / elapsed if elapsed > 0 else 0)

    # Summary by camera
    with get_cursor() as cursor:
        cursor.execute("""
            SELECT camera_id, COUNT(*) as count
            FROM embeddings
            WHERE embedding_type = 'vehicle_appearance'
            GROUP BY camera_id
        """)
        for row in cursor.fetchall():
            logger.info("  %s: %d embeddings", row['camera_id'], row['count'])


if __name__ == '__main__':
    main()
