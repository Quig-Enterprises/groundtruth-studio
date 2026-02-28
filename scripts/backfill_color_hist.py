#!/usr/bin/env python3
"""Backfill color_hist column in prediction_embeddings table."""
import os
import sys
import json

import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'worker'))
from color_hist import compute_color_hist

CROPS_DIR = '/opt/groundtruth-studio/clips/crops'
DB_DSN = 'dbname=groundtruth_studio'
BATCH_SIZE = 200


def main():
    conn = psycopg2.connect(DB_DSN)
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute('SELECT prediction_id FROM prediction_embeddings WHERE color_hist IS NULL ORDER BY prediction_id')
    rows = cur.fetchall()
    total = len(rows)
    print(f"Found {total} predictions needing color histograms")

    updated = 0
    skipped = 0

    for i, row in enumerate(rows):
        pid = row['prediction_id']
        crop_path = os.path.join(CROPS_DIR, f'gallery_{pid}.jpg')

        if not os.path.exists(crop_path):
            skipped += 1
            continue

        hist = compute_color_hist(crop_path)
        if hist is None:
            skipped += 1
            continue

        cur.execute(
            'UPDATE prediction_embeddings SET color_hist = %s WHERE prediction_id = %s',
            (json.dumps(hist), pid)
        )
        updated += 1

        if updated % BATCH_SIZE == 0:
            conn.commit()
            print(f"  Progress: {updated}/{total} updated, {skipped} skipped")

    conn.commit()
    conn.close()
    print(f"\nDone: {updated} updated, {skipped} skipped out of {total}")


if __name__ == '__main__':
    main()
