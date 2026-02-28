#!/usr/bin/env python3
"""Embedding worker - continuously embeds new predictions using DINOv2.

Runs as a one-shot process (called by systemd timer) that embeds any
predictions missing from the prediction_embeddings table, then exits.
"""
import base64
import json
import os
import sys
import logging

import psycopg2
import psycopg2.extras
import requests

from color_hist import compute_color_hist

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('embed_worker')

FASTREID_URL = 'http://localhost:5061'
EMBED_ENDPOINT = '/embed-batch/dino'
CROPS_DIR = '/opt/groundtruth-studio/clips/crops'
BATCH_SIZE = 32
DB_DSN = 'dbname=groundtruth_studio'


def generate_crop(row, crop_path):
    """Generate a tight crop from the video thumbnail."""
    from PIL import Image
    thumb_path = row['thumbnail_path']
    if not thumb_path or not os.path.exists(thumb_path):
        return False
    try:
        img = Image.open(thumb_path)
        iw, ih = img.size
        vw = row['video_width'] or iw
        vh = row['video_height'] or ih
        sx = iw / vw
        sy = ih / vh
        x = int(row['bbox_x'] * sx)
        y = int(row['bbox_y'] * sy)
        w = int(row['bbox_width'] * sx)
        h = int(row['bbox_height'] * sy)
        pad_x = int(w * 0.03)
        pad_y = int(h * 0.03)
        x1 = max(0, x - pad_x)
        y1 = max(0, y - pad_y)
        x2 = min(iw, x + w + pad_x)
        y2 = min(ih, y + h + pad_y)
        crop = img.crop((x1, y1, x2, y2))
        crop.save(crop_path, 'JPEG', quality=85)
        return True
    except Exception as e:
        logger.warning("Crop failed for %s: %s", row['id'], e)
        return False


def main():
    conn = psycopg2.connect(DB_DSN)
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Find predictions without embeddings (exclude no_detection)
    cur.execute('''
        SELECT p.id, p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
               v.thumbnail_path, v.width AS video_width, v.height AS video_height
        FROM ai_predictions p
        JOIN videos v ON p.video_id = v.id
        WHERE p.review_status NOT IN ('no_detection')
          AND p.id NOT IN (SELECT prediction_id FROM prediction_embeddings)
        ORDER BY p.id
        LIMIT 1000
    ''')
    rows = cur.fetchall()

    if not rows:
        logger.info("No new predictions to embed")
        conn.close()
        return

    logger.info("Found %d predictions to embed", len(rows))
    os.makedirs(CROPS_DIR, exist_ok=True)

    embedded = 0
    skipped = 0
    batch_items = []
    batch_images = []

    for row in rows:
        crop_path = os.path.join(CROPS_DIR, f"gallery_{row['id']}.jpg")
        if not os.path.exists(crop_path):
            if not generate_crop(row, crop_path):
                skipped += 1
                continue
        try:
            with open(crop_path, 'rb') as f:
                b64 = base64.b64encode(f.read()).decode('ascii')
            batch_items.append(row['id'])
            batch_images.append(b64)
        except Exception:
            skipped += 1
            continue

        if len(batch_images) >= BATCH_SIZE:
            embedded += flush_batch(cur, conn, batch_items, batch_images)
            batch_items = []
            batch_images = []

    if batch_images:
        embedded += flush_batch(cur, conn, batch_items, batch_images)

    conn.close()
    logger.info("Done: %d embedded, %d skipped", embedded, skipped)


def flush_batch(cur, conn, ids, images):
    try:
        resp = requests.post(f'{FASTREID_URL}{EMBED_ENDPOINT}',
                             json={'images': images}, timeout=60)
        resp.raise_for_status()
        embeddings = resp.json()['embeddings']

        for pred_id, emb in zip(ids, embeddings):
            vec_str = '[' + ','.join(str(v) for v in emb) + ']'
            crop_path = os.path.join(CROPS_DIR, f'gallery_{pred_id}.jpg')
            hist = compute_color_hist(crop_path)
            hist_json = json.dumps(hist) if hist else None
            cur.execute(
                'INSERT INTO prediction_embeddings (prediction_id, embedding, color_hist) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING',
                (pred_id, vec_str, hist_json)
            )
        conn.commit()
        return len(embeddings)
    except Exception as e:
        logger.error("Batch embed failed: %s", e)
        conn.rollback()
        return 0


if __name__ == '__main__':
    main()
