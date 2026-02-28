#!/usr/bin/env python3
"""Backfill prediction_embeddings table using FastReID batch embedding."""
import sys
import os
import base64
import time
import psycopg2
import psycopg2.extras
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

FASTREID_URL = 'http://localhost:5061'
EMBED_ENDPOINT = '/embed-batch/dino'  # DINOv2 384-dim embeddings
CROPS_DIR = '/opt/groundtruth-studio/clips/crops'
BATCH_SIZE = 32
DB_DSN = "dbname=groundtruth_studio"


def generate_crop(row, crop_path):
    """Generate a crop from the video thumbnail."""
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
        # Add 3% padding (tight crop for better embeddings)
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
        print(f"  Crop failed for {row['id']}: {e}")
        return False


def main():
    conn = psycopg2.connect(DB_DSN)
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Get predictions that don't have embeddings yet
    cur.execute('''
        SELECT p.id, p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
               v.thumbnail_path, v.width AS video_width, v.height AS video_height
        FROM ai_predictions p
        JOIN videos v ON p.video_id = v.id
        WHERE p.review_status NOT IN ('no_detection')
          AND p.id NOT IN (SELECT prediction_id FROM prediction_embeddings)
        ORDER BY p.id
    ''')
    rows = cur.fetchall()
    total = len(rows)
    print(f"Found {total} predictions to embed")

    os.makedirs(CROPS_DIR, exist_ok=True)

    embedded = 0
    skipped = 0
    batch_items = []
    batch_images = []

    for i, row in enumerate(rows):
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
        except Exception as e:
            print(f"  Read failed for {row['id']}: {e}")
            skipped += 1
            continue

        # Process batch
        if len(batch_images) >= BATCH_SIZE:
            embedded += flush_batch(cur, conn, batch_items, batch_images)
            batch_items = []
            batch_images = []
            if embedded % 200 == 0:
                print(f"  Progress: {embedded}/{total} embedded, {skipped} skipped")

    # Final batch
    if batch_images:
        embedded += flush_batch(cur, conn, batch_items, batch_images)

    conn.close()
    print(f"\nDone: {embedded} embedded, {skipped} skipped out of {total}")


def flush_batch(cur, conn, ids, images):
    """Send batch to FastReID and insert embeddings."""
    try:
        resp = requests.post(f'{FASTREID_URL}{EMBED_ENDPOINT}',
                             json={'images': images}, timeout=60)
        resp.raise_for_status()
        embeddings = resp.json()['embeddings']

        for j, (pred_id, emb) in enumerate(zip(ids, embeddings)):
            vec_str = '[' + ','.join(str(v) for v in emb) + ']'
            cur.execute(
                'INSERT INTO prediction_embeddings (prediction_id, embedding) VALUES (%s, %s) ON CONFLICT DO NOTHING',
                (pred_id, vec_str)
            )
        conn.commit()
        return len(embeddings)
    except Exception as e:
        print(f"  Batch embed failed: {e}")
        conn.rollback()
        return 0


if __name__ == '__main__':
    main()
