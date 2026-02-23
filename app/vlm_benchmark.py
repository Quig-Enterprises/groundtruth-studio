#!/usr/bin/env python3
"""
VLM Benchmark — Score VLM against human-reviewed predictions.

Loads reviewed vehicle_detection predictions, runs VLM on each,
and compares VLM judgment (is_vehicle) against human review status
(approved = vehicle, rejected = not vehicle).
"""

import os, sys, json, time, base64, io, logging, random
import requests
from PIL import Image, ImageDraw

# ── Config ──────────────────────────────────────────────────────────
OLLAMA_URL = "http://localhost:11434"
VLM_MODEL = "llama3.2-vision"
VLM_TIMEOUT = 60
THUMBNAIL_DIR = "/opt/groundtruth-studio/thumbnails"
SAMPLE_SIZE = int(os.environ.get("VLM_SAMPLE_SIZE", "100"))
DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://groundtruth:bZv6QbJ8KCAQubJFb+frmbGNKUiPm7lBUg0XgMvEzNQ=@localhost:5432/groundtruth_studio"
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)


# ── Helpers ─────────────────────────────────────────────────────────

def image_to_base64(img):
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def crop_with_padding(img, x, y, w, h, padding=0.2):
    iw, ih = img.size
    pad_x = int(w * padding)
    pad_y = int(h * padding)
    left = max(0, x - pad_x)
    top = max(0, y - pad_y)
    right = min(iw, x + w + pad_x)
    bottom = min(ih, y + h + pad_y)
    if right <= left or bottom <= top:
        return img
    return img.crop((left, top, right, bottom))


def draw_detection_box(img, x, y, w, h):
    annotated = img.copy()
    draw = ImageDraw.Draw(annotated)
    draw.rectangle([x, y, x + w, y + h], outline="red", width=3)
    return annotated


def make_composite(crop_img, annotated_frame):
    """Side-by-side composite: crop on left, annotated frame on right."""
    frame_w, frame_h = annotated_frame.size
    crop_w, crop_h = crop_img.size
    if crop_h > 0:
        scale = frame_h / crop_h
        scaled_crop = crop_img.resize((max(1, int(crop_w * scale)), frame_h), Image.LANCZOS)
    else:
        scaled_crop = crop_img
    sc_w, _ = scaled_crop.size
    gap = 10
    composite = Image.new('RGB', (sc_w + gap + frame_w, frame_h), (40, 40, 40))
    composite.paste(scaled_crop, (0, 0))
    composite.paste(annotated_frame, (sc_w + gap, 0))
    return composite


def query_vlm(composite_b64, predicted_class, confidence):
    prompt = (
        f'You are analyzing a detection from a vehicle monitoring camera.\n'
        f'The AI detected this as: "{predicted_class}" (confidence: {confidence:.0%})\n\n'
        f'The image shows two views side by side:\n'
        f'LEFT: Cropped detection region\n'
        f'RIGHT: Full camera frame (detection marked with red box)\n\n'
        f'RULES:\n'
        f'- is_vehicle=true means ANY motorized transport: cars, trucks, SUVs, vans, buses, motorcycles, ATVs, UTVs, sedans, semi trucks, trailers, snowmobiles, etc.\n'
        f'- IMPORTANT: A car IS a vehicle. A sedan IS a vehicle. A minivan IS a vehicle. A trailer IS a vehicle. ALL motorized transport counts as a vehicle.\n'
        f'- Trailers (flatbed, enclosed, utility) are vehicles even when parked or unhitched\n'
        f'- Snowmobiles are vehicles. A snowmobile on a trailer counts as a vehicle\n'
        f'- is_vehicle=false ONLY for non-motorized objects: tree, shadow, sign, person, building, snow, mailbox, animal, fence, chair, table, ladder, flag, etc.\n'
        f'- If it IS a vehicle but a different type than predicted, set is_vehicle=true with the correct type\n'
        f'- Be SKEPTICAL: low-confidence detections ({confidence:.0%}) are often false positives like shadows, snow, or background objects\n'
        f'- Look carefully at the LEFT crop — can you clearly see a real motor vehicle, trailer, or snowmobile?\n\n'
        f'Respond ONLY with JSON: {{"is_vehicle": bool, "suggested_class": "...", "confidence": 0.0-1.0, "reasoning": "..."}}'
    )
    payload = {
        "model": VLM_MODEL,
        "prompt": prompt,
        "images": [composite_b64],
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 256},
    }
    resp = requests.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=VLM_TIMEOUT)
    resp.raise_for_status()
    return resp.json().get("response", "")


def parse_response(text):
    """Extract JSON from VLM response."""
    import re
    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Try extracting JSON block
    m = re.search(r'\{[^{}]*"is_vehicle"[^{}]*\}', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    # Fallback: look for keywords
    lower = text.lower()
    is_vehicle = "not a vehicle" not in lower and "is not a vehicle" not in lower
    return {"is_vehicle": is_vehicle, "suggested_class": "unknown", "confidence": 0.5, "reasoning": text[:200]}


# ── Main ────────────────────────────────────────────────────────────

def main():
    import psycopg2

    run_all = os.environ.get("VLM_RUN_ALL", "0") == "1"
    requeue = os.environ.get("VLM_REQUEUE", "0") == "1"

    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()

    if run_all:
        log.info(f"VLM Benchmark — scoring ALL reviewed vehicle predictions against {VLM_MODEL}")
        cur.execute("""
            SELECT p.id, v.filename, p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
                   p.confidence, p.review_status, p.predicted_tags
            FROM ai_predictions p
            JOIN videos v ON p.video_id = v.id
            WHERE p.review_status IN ('approved', 'rejected')
            AND p.scenario = 'vehicle_detection'
            ORDER BY p.id
        """)
    else:
        log.info(f"VLM Benchmark — scoring {SAMPLE_SIZE} reviewed predictions against {VLM_MODEL}")
        half = SAMPLE_SIZE // 2
        cur.execute("""
            (SELECT p.id, v.filename, p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
                    p.confidence, p.review_status, p.predicted_tags
             FROM ai_predictions p
             JOIN videos v ON p.video_id = v.id
             WHERE p.review_status = 'approved' AND p.scenario = 'vehicle_detection'
             ORDER BY RANDOM() LIMIT %s)
            UNION ALL
            (SELECT p.id, v.filename, p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
                    p.confidence, p.review_status, p.predicted_tags
             FROM ai_predictions p
             JOIN videos v ON p.video_id = v.id
             WHERE p.review_status = 'rejected' AND p.scenario = 'vehicle_detection'
             ORDER BY RANDOM() LIMIT %s)
        """, (half, SAMPLE_SIZE - half))

    rows = cur.fetchall()
    if not run_all:
        random.shuffle(rows)

    log.info(f"Loaded {len(rows)} predictions ({sum(1 for r in rows if r[7]=='approved')} approved, "
             f"{sum(1 for r in rows if r[7]=='rejected')} rejected)")

    # Results tracking
    results = {"tp": 0, "tn": 0, "fp": 0, "fn": 0, "errors": 0, "skipped": 0}
    details = []
    start = time.time()

    for i, row in enumerate(rows):
        pid, filename, bx, by, bw, bh, conf, review_status, pred_tags = row
        img_path = os.path.join(THUMBNAIL_DIR, filename)

        if not os.path.exists(img_path):
            results["skipped"] += 1
            continue

        tags = pred_tags if isinstance(pred_tags, dict) else json.loads(pred_tags)
        predicted_class = tags.get("class", "vehicle")

        try:
            img = Image.open(img_path).convert("RGB")

            # Crop and annotate
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

            # Human truth: approved = is vehicle, rejected = not vehicle
            human_says_vehicle = (review_status == "approved")

            if vlm_says_vehicle and human_says_vehicle:
                results["tp"] += 1
                verdict = "TP"
            elif not vlm_says_vehicle and not human_says_vehicle:
                results["tn"] += 1
                verdict = "TN"
            elif vlm_says_vehicle and not human_says_vehicle:
                results["fp"] += 1
                verdict = "FP"
            else:
                results["fn"] += 1
                verdict = "FN"

            details.append({
                "id": pid, "verdict": verdict,
                "human": review_status, "vlm_is_vehicle": vlm_says_vehicle,
                "vlm_class": result.get("suggested_class", ""),
                "vlm_conf": result.get("confidence", 0),
                "yolo_class": predicted_class, "yolo_conf": conf,
                "reasoning": result.get("reasoning", "")[:100],
            })

            done = results["tp"] + results["tn"] + results["fp"] + results["fn"]
            acc = (results["tp"] + results["tn"]) / done if done else 0
            elapsed = time.time() - start
            rate = done / elapsed if elapsed > 0 else 0

            if (i + 1) % 5 == 0 or (i + 1) == len(rows):
                log.info(f"[{done}/{len(rows)}] acc={acc:.1%} tp={results['tp']} tn={results['tn']} "
                         f"fp={results['fp']} fn={results['fn']} ({rate:.1f}/s) — last: {verdict} "
                         f"id={pid} '{predicted_class}' conf={conf:.2f}")

        except Exception as e:
            results["errors"] += 1
            log.warning(f"Error on prediction {pid}: {e}")

    # ── Final Report ────────────────────────────────────────────
    elapsed = time.time() - start
    total = results["tp"] + results["tn"] + results["fp"] + results["fn"]
    acc = (results["tp"] + results["tn"]) / total if total else 0
    precision = results["tp"] / (results["tp"] + results["fp"]) if (results["tp"] + results["fp"]) else 0
    recall = results["tp"] / (results["tp"] + results["fn"]) if (results["tp"] + results["fn"]) else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0

    # FP detail: what did VLM wrongly call a vehicle?
    fp_details = [d for d in details if d["verdict"] == "FP"]
    fn_details = [d for d in details if d["verdict"] == "FN"]

    print("\n" + "=" * 60)
    print(f"  VLM BENCHMARK RESULTS — {VLM_MODEL}")
    print("=" * 60)
    print(f"  Sample size:  {total} ({results['skipped']} skipped, {results['errors']} errors)")
    print(f"  Elapsed:      {elapsed:.0f}s ({total/elapsed:.1f} predictions/s)")
    print(f"")
    print(f"  Accuracy:     {acc:.1%}")
    print(f"  Precision:    {precision:.1%}  (of VLM 'vehicle' calls, how many correct)")
    print(f"  Recall:       {recall:.1%}  (of actual vehicles, how many VLM caught)")
    print(f"  F1 Score:     {f1:.1%}")
    print(f"")
    print(f"  Confusion Matrix:")
    print(f"                    VLM: Vehicle    VLM: Not Vehicle")
    print(f"  Human: Vehicle    TP={results['tp']:<10d}  FN={results['fn']}")
    print(f"  Human: Not Veh    FP={results['fp']:<10d}  TN={results['tn']}")
    print(f"")

    if fp_details:
        print(f"  False Positives (VLM said vehicle, human rejected): {len(fp_details)}")
        for d in fp_details[:5]:
            print(f"    id={d['id']} yolo='{d['yolo_class']}' conf={d['yolo_conf']:.2f} — {d['reasoning']}")

    if fn_details:
        print(f"\n  False Negatives (VLM said not vehicle, human approved): {len(fn_details)}")
        for d in fn_details[:5]:
            print(f"    id={d['id']} yolo='{d['yolo_class']}' conf={d['yolo_conf']:.2f} vlm_class='{d['vlm_class']}' — {d['reasoning']}")

    print("=" * 60)

    # Save full details
    report_path = "/opt/groundtruth-studio/vlm_benchmark_results.json"
    with open(report_path, "w") as f:
        json.dump({"summary": {
            "model": VLM_MODEL, "sample_size": total, "accuracy": acc,
            "precision": precision, "recall": recall, "f1": f1,
            "confusion": results, "elapsed_seconds": elapsed,
        }, "details": details}, f, indent=2)
    log.info(f"Full results saved to {report_path}")

    # ── Re-queue disagreements ──────────────────────────────────
    disagreements = [d for d in details if d["verdict"] in ("FP", "FN")]

    if requeue and disagreements:
        log.info(f"Re-queuing {len(disagreements)} disagreements for human review...")
        requeue_conn = psycopg2.connect(DB_URL)
        requeue_cur = requeue_conn.cursor()

        requeue_ids = [d["id"] for d in disagreements]
        for pid in requeue_ids:
            d = next(x for x in disagreements if x["id"] == pid)
            vlm_note = (
                f"VLM disagreed with previous {d['human']} decision. "
                f"VLM says is_vehicle={d['vlm_is_vehicle']} "
                f"(class={d['vlm_class']}, conf={d['vlm_conf']}). "
                f"Reason: {d['reasoning']}"
            )
            requeue_cur.execute("""
                UPDATE ai_predictions
                SET review_status = 'pending',
                    corrected_tags = COALESCE(corrected_tags, '{}'::jsonb) || %s::jsonb
                WHERE id = %s
            """, (json.dumps({
                "vlm_model": VLM_MODEL,
                "vlm_is_vehicle": d["vlm_is_vehicle"],
                "vlm_suggested_class": d["vlm_class"],
                "vlm_confidence": d["vlm_conf"],
                "vlm_reasoning": d["reasoning"],
                "vlm_benchmark_requeue": True,
                "previous_review_status": d["human"],
            }), pid))

        requeue_conn.commit()
        requeue_conn.close()
        log.info(f"Re-queued {len(requeue_ids)} predictions to 'pending' with VLM context")
        print(f"\n  RE-QUEUED: {len(requeue_ids)} disagreements sent back to review queue")
        print(f"    FP (VLM=vehicle, human=rejected): {len([d for d in disagreements if d['verdict']=='FP'])}")
        print(f"    FN (VLM=not vehicle, human=approved): {len([d for d in disagreements if d['verdict']=='FN'])}")
    elif disagreements and not requeue:
        print(f"\n  {len(disagreements)} disagreements found. Set VLM_REQUEUE=1 to send back to review queue.")

    if not run_all:
        conn.close()


if __name__ == "__main__":
    main()
