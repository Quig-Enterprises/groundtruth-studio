#!/usr/bin/env python3
"""Batch extract person ReID (body appearance) embeddings from mwcam9 detections.

Crops person bboxes from thumbnails and video clips, sends to FastReID API
for body embeddings and InsightFace API for face embeddings, then stores
results in the embeddings table and links similar tracks.
"""

import base64
import io
import logging
import os
import sys
import time
import uuid

import cv2
import numpy as np
import requests
from PIL import Image

os.environ.setdefault(
    'DATABASE_URL',
    'postgresql://groundtruth:bZv6QbJ8KCAQubJFb+frmbGNKUiPm7lBUg0XgMvEzNQ=@localhost:5432/groundtruth_studio'
)

sys.path.insert(0, '/opt/groundtruth-studio/app')
from db_connection import init_connection_pool, get_cursor
from track_builder import TrackBuilder

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

FASTREID_API_URL = 'http://localhost:5061'
INSIGHTFACE_API_URL = 'http://localhost:5060'
FRIGATE_URL = os.environ.get('FRIGATE_URL', 'http://localhost:5000')
CLIPS_DIR = '/opt/groundtruth-studio/clips'
ECOEYE_VIDEOS_DIR = '/opt/app/videos'
BATCH_SIZE = 16
MIN_BBOX_AREA = 2500        # minimum bbox area for body embedding
FACE_BBOX_AREA = 10000      # minimum bbox area to also try face embedding
BODY_COSINE_THRESHOLD = 0.85  # for linking tracks (conservative to avoid chain-merging)


def get_unprocessed_predictions():
    """Fetch mwcam9 person predictions that don't have body_reid embeddings yet."""
    with get_cursor() as cursor:
        cursor.execute("""
            SELECT p.id, p.video_id, p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
                   p.confidence, v.thumbnail_path, v.camera_id, v.metadata
            FROM ai_predictions p
            JOIN classification_classes cc ON cc.name = p.classification
            JOIN videos v ON v.id = p.video_id
            WHERE cc.scenario = 'person_identification'
              AND v.camera_id = 'mwcam9'
              AND p.bbox_width * p.bbox_height >= %s
              AND NOT EXISTS (
                  SELECT 1 FROM embeddings e
                  WHERE e.source_image_path LIKE '%%pred_' || p.id::text || '%%'
                    AND e.embedding_type = 'body_reid'
              )
            ORDER BY p.id
        """, (MIN_BBOX_AREA,))
        return cursor.fetchall()


def crop_person(thumbnail_path, bbox_x, bbox_y, bbox_width, bbox_height, padding=0.1):
    """Crop person from thumbnail with padding.

    Args:
        thumbnail_path: Path to the source image.
        bbox_x, bbox_y, bbox_width, bbox_height: Bounding box coordinates.
        padding: Fractional padding to add around the bbox (default 10%).

    Returns:
        PIL Image of the cropped person, or None on failure.
    """
    try:
        if not os.path.exists(thumbnail_path):
            return None

        img = Image.open(thumbnail_path).convert('RGB')

        pad_w = bbox_width * padding
        pad_h = bbox_height * padding

        x1 = max(0, int(bbox_x - pad_w))
        y1 = max(0, int(bbox_y - pad_h))
        x2 = min(img.width, int(bbox_x + bbox_width + pad_w))
        y2 = min(img.height, int(bbox_y + bbox_height + pad_h))

        if x2 <= x1 or y2 <= y1:
            return None

        crop = img.crop((x1, y1, x2, y2))
        if crop.width < 20 or crop.height < 20:
            return None

        return crop
    except Exception as e:
        logger.warning("Failed to crop %s: %s", thumbnail_path, e)
        return None


def image_to_base64(img):
    """Convert PIL image to base64 JPEG string."""
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=90)
    return base64.b64encode(buf.getvalue()).decode('utf-8')


def find_clip(video_metadata, camera_id):
    """Find best clip source for a prediction.

    Checks EcoEye videos first, then cached Frigate clips, then fetches
    from Frigate API on-demand.

    Returns:
        Path to clip file, or None.
    """
    # 1. Check EcoEye path
    ecoeye_dir = os.path.join(ECOEYE_VIDEOS_DIR, camera_id)
    if os.path.isdir(ecoeye_dir):
        mp4_files = [f for f in os.listdir(ecoeye_dir) if f.endswith('.mp4')]
        if mp4_files:
            # Return most recent
            mp4_files.sort(reverse=True)
            return os.path.join(ecoeye_dir, mp4_files[0])

    # 2. Check Frigate event clips
    event_id = None
    if isinstance(video_metadata, dict):
        event_id = video_metadata.get('frigate_event_id') or video_metadata.get('event_id')

    if event_id:
        cached_clip = os.path.join(CLIPS_DIR, f'frigate_{event_id}.mp4')
        if os.path.exists(cached_clip):
            return cached_clip

        # 3. Fetch from Frigate API on-demand
        try:
            os.makedirs(CLIPS_DIR, exist_ok=True)
            resp = requests.get(
                f'{FRIGATE_URL}/api/events/{event_id}/clip.mp4',
                timeout=30,
                stream=True,
            )
            resp.raise_for_status()
            with open(cached_clip, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            logger.info("Fetched Frigate clip for event %s", event_id)
            return cached_clip
        except Exception as e:
            logger.debug("Could not fetch Frigate clip for event %s: %s", event_id, e)

    return None


def extract_clip_crops(clip_path, bbox_x, bbox_y, bbox_width, bbox_height,
                       max_crops=5, sample_fps=2):
    """Extract person crops from video clip frames.

    Samples frames at ~2fps, crops the person region with padding, and
    returns the top N crops sorted by quality (area * sharpness).

    Returns:
        List of PIL Images (best crops first).
    """
    crops = []
    try:
        cap = cv2.VideoCapture(clip_path)
        if not cap.isOpened():
            return crops

        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        frame_skip = max(1, int(fps / sample_fps))
        frame_idx = 0

        padding = 0.2
        pad_w = bbox_width * padding
        pad_h = bbox_height * padding

        while True:
            ret, frame = cap.read()
            if not ret:
                break

            if frame_idx % frame_skip == 0:
                h, w = frame.shape[:2]
                x1 = max(0, int(bbox_x - pad_w))
                y1 = max(0, int(bbox_y - pad_h))
                x2 = min(w, int(bbox_x + bbox_width + pad_w))
                y2 = min(h, int(bbox_y + bbox_height + pad_h))

                if x2 > x1 and y2 > y1:
                    crop_bgr = frame[y1:y2, x1:x2]
                    area = (x2 - x1) * (y2 - y1)
                    gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
                    sharpness = cv2.Laplacian(gray, cv2.CV_64F).var()
                    score = area * sharpness

                    crop_rgb = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2RGB)
                    pil_img = Image.fromarray(crop_rgb)
                    crops.append((score, pil_img))

            frame_idx += 1

        cap.release()

        # Sort by quality descending, return top N images
        crops.sort(key=lambda x: x[0], reverse=True)
        return [img for _, img in crops[:max_crops]]

    except Exception as e:
        logger.warning("Failed to extract clip crops from %s: %s", clip_path, e)
        return []


def get_or_create_person_identity(cursor, prediction_id):
    """Get or create a person identity for this prediction.

    Returns:
        identity_id as a string UUID.
    """
    # Check if prediction already has an identity via existing embeddings
    cursor.execute("""
        SELECT e.identity_id FROM embeddings e
        WHERE e.source_image_path LIKE %s
        LIMIT 1
    """, (f'%pred_{prediction_id}%',))
    row = cursor.fetchone()
    if row:
        return row['identity_id']

    # Create new person identity
    identity_id = str(uuid.uuid4())
    cursor.execute("""
        INSERT INTO identities (identity_id, name, identity_type, first_seen, last_seen, metadata)
        VALUES (%s, %s, 'person', NOW(), NOW(), '{}')
    """, (identity_id, f'person_mwcam9_{prediction_id}'))
    return identity_id


def store_embedding(cursor, identity_id, embedding_type, vector, confidence,
                    source_path, camera_id):
    """Insert an embedding into the embeddings table."""
    cursor.execute("""
        INSERT INTO embeddings (identity_id, embedding_type, vector, confidence,
                               source_image_path, camera_id, session_date)
        VALUES (%s, %s, %s, %s, %s, %s, CURRENT_DATE)
        ON CONFLICT DO NOTHING
    """, (identity_id, embedding_type, vector, confidence, source_path, camera_id))


def send_body_embedding(image):
    """Send a single PIL image to FastReID for body embedding.

    Returns:
        Embedding list, or None on failure.
    """
    try:
        resp = requests.post(
            f'{FASTREID_API_URL}/embed/person',
            json={'image': image_to_base64(image)},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get('embedding')
    except Exception as e:
        logger.warning("Body embedding request failed: %s", e)
        return None


def send_body_batch(images):
    """Send a batch of PIL images to FastReID for body embeddings.

    Returns:
        List of embeddings, or empty list on failure.
    """
    try:
        b64_images = [image_to_base64(img) for img in images]
        resp = requests.post(
            f'{FASTREID_API_URL}/embed-batch/person',
            json={'images': b64_images},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get('embeddings', [])
    except Exception as e:
        logger.warning("Body batch embedding request failed: %s", e)
        return None


def send_face_embedding(image):
    """Send a PIL image to InsightFace API for face detection + embedding.

    Returns:
        Face embedding list, or None if no face detected.
    """
    try:
        resp = requests.post(
            f'{INSIGHTFACE_API_URL}/embed-base64',
            json={'image': image_to_base64(image)},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get('face_detected') and data.get('embedding'):
            return data['embedding']
        return None
    except Exception as e:
        logger.warning("Face embedding request failed: %s", e)
        return None


def process_predictions(predictions):
    """Process a batch of predictions: extract body and face embeddings.

    Returns:
        Tuple of (total_body, total_face) counts.
    """
    total_body = 0
    total_face = 0

    for pred in predictions:
        thumbnail_path = pred['thumbnail_path']
        bbox = (pred['bbox_x'], pred['bbox_y'], pred['bbox_width'], pred['bbox_height'])
        area = bbox[2] * bbox[3]
        metadata = pred['metadata'] or {}

        # 1. Crop person from thumbnail
        crop = crop_person(thumbnail_path, *bbox)
        if crop is None:
            continue

        # 2. Try to get clip crops for better quality
        clip_path = find_clip(metadata, pred['camera_id'])
        clip_crops = []
        if clip_path:
            clip_crops = extract_clip_crops(clip_path, *bbox)

        # 3. Get body embedding — prefer best clip crop, fallback to thumbnail crop
        all_crops = clip_crops + [crop]  # clip crops first (better quality)
        body_embedding = None

        if len(all_crops) > 1:
            body_embeddings = send_body_batch(all_crops)
            if body_embeddings is None:
                # Batch failed, fallback to single best crop
                body_embedding = send_body_embedding(all_crops[0])
            elif body_embeddings:
                body_embedding = body_embeddings[0]  # Best crop embedding
        else:
            body_embedding = send_body_embedding(crop)

        if body_embedding is None:
            logger.warning("Failed to get body embedding for pred %d", pred['id'])
            continue

        # 4. Store body embedding
        with get_cursor() as cursor:
            identity_id = get_or_create_person_identity(cursor, pred['id'])
            store_embedding(
                cursor, identity_id, 'body_reid', body_embedding,
                float(pred['confidence']), f"pred_{pred['id']}_body", pred['camera_id'],
            )
            total_body += 1

            # 5. Also try face embedding if bbox large enough
            if area >= FACE_BBOX_AREA:
                face_embedding = send_face_embedding(crop)
                if face_embedding:
                    store_embedding(
                        cursor, identity_id, 'face', face_embedding,
                        float(pred['confidence']), f"pred_{pred['id']}_face", pred['camera_id'],
                    )
                    total_face += 1

    return total_body, total_face


def link_similar_tracks():
    """Compare recent body_reid embeddings for mwcam9 and merge similar identities.

    Returns:
        Number of identities linked.
    """
    with get_cursor() as cursor:
        cursor.execute("""
            SELECT e.embedding_id, e.identity_id, e.vector, e.created_at
            FROM embeddings e
            WHERE e.embedding_type = 'body_reid'
              AND e.camera_id = 'mwcam9'
              AND e.created_at > NOW() - INTERVAL '7 days'
            ORDER BY e.created_at DESC
        """)
        embeddings = cursor.fetchall()

    if len(embeddings) < 2:
        return 0

    # Group embeddings by identity_id, compute mean vector per identity
    from collections import defaultdict
    identity_vecs = defaultdict(list)
    for emb in embeddings:
        identity_vecs[emb['identity_id']].append(
            np.array(emb['vector'], dtype=np.float32)
        )

    identities = {}
    for iid, vecs in identity_vecs.items():
        mean_vec = np.mean(vecs, axis=0)
        norm = np.linalg.norm(mean_vec)
        if norm > 0:
            mean_vec = mean_vec / norm
        identities[iid] = mean_vec

    # Find mutual best-match pairs above threshold
    id_list = list(identities.keys())
    merged = set()
    linked = 0

    for i, id_a in enumerate(id_list):
        if id_a in merged:
            continue
        best_sim = -1.0
        best_id = None
        for j, id_b in enumerate(id_list):
            if i == j or id_b in merged:
                continue
            sim = float(np.dot(identities[id_a], identities[id_b]))
            if sim > best_sim:
                best_sim = sim
                best_id = id_b

        if best_id is None or best_sim < BODY_COSINE_THRESHOLD:
            continue

        # Verify mutual: id_b's best match must also be id_a
        reverse_best_sim = -1.0
        reverse_best_id = None
        for k, id_c in enumerate(id_list):
            if id_c == best_id or id_c in merged:
                continue
            sim = float(np.dot(identities[best_id], identities[id_c]))
            if sim > reverse_best_sim:
                reverse_best_sim = sim
                reverse_best_id = id_c

        if reverse_best_id != id_a:
            continue  # Not a mutual best match — skip

        # Mutual best match confirmed — merge best_id into id_a
        logger.info("Merging identity %s into %s (cosine=%.3f)", best_id, id_a, best_sim)
        with get_cursor() as cursor:
            cursor.execute(
                "UPDATE embeddings SET identity_id = %s WHERE identity_id = %s",
                (id_a, best_id),
            )
            cursor.execute(
                "UPDATE identities SET last_seen = NOW() WHERE identity_id = %s",
                (id_a,),
            )
            cursor.execute("""
                DELETE FROM identities WHERE identity_id = %s
                AND NOT EXISTS (SELECT 1 FROM embeddings WHERE identity_id = %s)
            """, (best_id, best_id))
        merged.add(best_id)
        linked += 1

    return linked


def main():
    init_connection_pool()

    # Health check — FastReID API
    try:
        resp = requests.get(f'{FASTREID_API_URL}/health', timeout=5)
        health = resp.json()
        logger.info("FastReID API healthy: %s", health)
    except Exception as e:
        logger.error("FastReID API not available: %s", e)
        sys.exit(1)

    # Get unprocessed predictions
    predictions = get_unprocessed_predictions()
    logger.info("Found %d mwcam9 person predictions needing body ReID", len(predictions))

    if not predictions:
        logger.info("Nothing to do")
        return

    # Process in batches
    start = time.time()
    total_body, total_face = 0, 0
    total_batches = (len(predictions) + BATCH_SIZE - 1) // BATCH_SIZE

    for i in range(0, len(predictions), BATCH_SIZE):
        batch = predictions[i:i + BATCH_SIZE]
        body, face = process_predictions(batch)
        total_body += body
        total_face += face
        logger.info(
            "Batch %d/%d: %d body + %d face embeddings",
            i // BATCH_SIZE + 1, total_batches, body, face,
        )

    elapsed = time.time() - start
    logger.info("Extracted %d body + %d face embeddings in %.1fs", total_body, total_face, elapsed)

    # Identity linking disabled — mwcam9 crops are too similar at distance for
    # reliable same-person matching. Cross-camera matching handles this on-demand.
    # To enable: linked = link_similar_tracks()

    # Build tracks
    try:
        tb = TrackBuilder()
        result = tb.build_tracks(camera_id='mwcam9')
        logger.info("Track building: %s", result)
    except Exception as e:
        logger.warning("Track building failed (non-fatal): %s", e)

    # Summary
    with get_cursor() as cursor:
        cursor.execute("""
            SELECT embedding_type, COUNT(*)
            FROM embeddings
            WHERE camera_id = 'mwcam9'
            GROUP BY embedding_type
        """)
        for row in cursor.fetchall():
            logger.info("  mwcam9 %s: %d embeddings", row['embedding_type'], row['count'])


if __name__ == '__main__':
    main()
