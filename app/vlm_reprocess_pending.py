#!/usr/bin/env python3
"""Re-run VLM on pending predictions with updated prompt."""
import os, sys, json, time
sys.path.insert(0, os.path.dirname(__file__) + '/..')
from app.vlm_benchmark import (
    image_to_base64, crop_with_padding, draw_detection_box,
    make_composite, query_vlm, parse_response, THUMBNAIL_DIR
)
import psycopg2, logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://groundtruth:bZv6QbJ8KCAQubJFb+frmbGNKUiPm7lBUg0XgMvEzNQ=@localhost:5432/groundtruth_studio"
)

def main():
    from PIL import Image
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()

    cur.execute("""
        SELECT p.id, v.filename, p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
               p.confidence, p.predicted_tags
        FROM ai_predictions p
        JOIN videos v ON p.video_id = v.id
        WHERE p.review_status = 'pending'
          AND p.scenario = 'vehicle_detection'
        ORDER BY p.id
    """)
    rows = cur.fetchall()
    log.info(f"Re-processing {len(rows)} pending predictions with updated VLM prompt")

    updated = 0
    errors = 0
    start = time.time()

    for i, row in enumerate(rows):
        pid, filename, bx, by, bw, bh, conf, pred_tags = row
        img_path = os.path.join(THUMBNAIL_DIR, filename)

        if not os.path.exists(img_path):
            log.warning(f"Skip {pid}: thumbnail not found")
            continue

        tags = pred_tags if isinstance(pred_tags, dict) else json.loads(pred_tags)
        predicted_class = tags.get("class", "vehicle")

        try:
            img = Image.open(img_path).convert("RGB")
            if bx is not None and by is not None and bw and bh:
                crop = crop_with_padding(img, bx, by, bw, bh)
                annotated = draw_detection_box(img, bx, by, bw, bh)
            else:
                crop = img
                annotated = img

            composite = make_composite(crop, annotated)
            composite_b64 = image_to_base64(composite)

            raw = query_vlm(composite_b64, predicted_class, conf)
            result = parse_response(raw)
            vlm_says_vehicle = result.get("is_vehicle", True)

            vlm_update = {
                "vlm_model": "llama3.2-vision",
                "vlm_is_vehicle": vlm_says_vehicle,
                "vlm_suggested_class": result.get("suggested_class", ""),
                "vlm_confidence": result.get("confidence", 0.5),
                "vlm_reasoning": result.get("reasoning", "")[:200],
            }
            if not vlm_says_vehicle:
                vlm_update["actual_class"] = result.get("suggested_class", "unknown")
                vlm_update["needs_negative_review"] = True

            cur.execute("""
                UPDATE ai_predictions
                SET corrected_tags = COALESCE(corrected_tags, '{}'::jsonb) || %s::jsonb
                WHERE id = %s
            """, (json.dumps(vlm_update), pid))
            conn.commit()
            updated += 1

            elapsed = time.time() - start
            rate = updated / elapsed if elapsed > 0 else 0
            if (i + 1) % 5 == 0 or (i + 1) == len(rows):
                log.info(f"[{i+1}/{len(rows)}] updated={updated} errors={errors} "
                         f"({rate:.1f}/s) — id={pid} is_vehicle={vlm_says_vehicle} "
                         f"class='{result.get('suggested_class','')}'")

        except Exception as e:
            errors += 1
            log.warning(f"Error on {pid}: {e}")

    elapsed = time.time() - start
    log.info(f"Done: {updated} updated, {errors} errors in {elapsed:.0f}s")
    conn.close()

if __name__ == "__main__":
    main()
