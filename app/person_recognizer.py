"""
Person Recognition Engine

Builds a reference gallery from manually tagged person detections,
then matches new face embeddings against the gallery using cosine similarity.
"""

import json
import logging
import os
import threading
import time
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np
import requests

from db_connection import get_cursor, get_connection
from psycopg2 import extras

logger = logging.getLogger(__name__)

INSIGHTFACE_URL = "http://localhost:5060"
SIMILARITY_THRESHOLD = 0.5
GALLERY_CACHE_TTL = 300  # 5 minutes

class PersonRecognizer:
    """Recognizes known people by comparing face embeddings against a reference gallery."""

    def __init__(self):
        self._gallery = None
        self._gallery_timestamp = 0
        self._lock = threading.Lock()

    def build_reference_gallery(self) -> Dict:
        """
        Build reference gallery from tagged person detections in annotation_tags.

        Query all keyframe_annotations that have annotation_tags with person_name set.
        For each, crop the face region from the thumbnail, send to InsightFace,
        and store as a reference embedding (is_reference=True).

        Returns summary dict with counts.
        """
        summary = {
            'identities_count': 0,
            'embeddings_count': 0,
            'skipped': 0,
            'errors': []
        }

        # Clear existing reference embeddings
        with get_cursor(commit=True) as cur:
            cur.execute("DELETE FROM embeddings WHERE is_reference = true AND embedding_type = 'face'")
            logger.info(f"Cleared {cur.rowcount} existing reference embeddings")

        # Query tagged person detections
        with get_cursor(commit=False) as cur:
            cur.execute("""
                SELECT ka.id, ka.video_id, ka.bbox_x, ka.bbox_y, ka.bbox_width, ka.bbox_height,
                       v.thumbnail_path,
                       at.tag_value
                FROM keyframe_annotations ka
                JOIN videos v ON ka.video_id = v.id
                JOIN annotation_tags at ON ka.id = at.annotation_id AND at.annotation_type = 'keyframe'
                WHERE at.tag_value LIKE '%person_name%'
            """)
            tagged_detections = cur.fetchall()

        logger.info(f"Found {len(tagged_detections)} tagged person detections")

        processed_identities = set()

        for detection in tagged_detections:
            try:
                # Parse tag_value JSON
                tag_value = detection['tag_value']
                if isinstance(tag_value, str):
                    tag_data = json.loads(tag_value)
                else:
                    tag_data = tag_value

                person_name = tag_data.get('person_name', '').strip()

                # Skip invalid names
                if not person_name or person_name.lower() in ('unknown', 'none', ''):
                    summary['skipped'] += 1
                    continue

                # Resolve thumbnail path
                thumbnail_path = detection['thumbnail_path']
                if thumbnail_path and not os.path.exists(thumbnail_path):
                    # Try fallback location
                    basename = os.path.basename(thumbnail_path)
                    fallback_path = f"/opt/groundtruth-studio/thumbnails/{basename}"
                    if os.path.exists(fallback_path):
                        thumbnail_path = fallback_path
                    else:
                        logger.warning(f"Thumbnail not found: {thumbnail_path}")
                        summary['skipped'] += 1
                        summary['errors'].append(f"Missing thumbnail: {thumbnail_path}")
                        continue

                # Build bbox dict
                bbox = {
                    'x': int(detection['bbox_x']),
                    'y': int(detection['bbox_y']),
                    'width': int(detection['bbox_width']),
                    'height': int(detection['bbox_height'])
                }

                # Extract embedding
                result = self._get_embedding(thumbnail_path, bbox)
                if not result or not result.get('face_detected'):
                    logger.warning(f"No face detected for {person_name} in annotation {detection['id']}")
                    summary['skipped'] += 1
                    continue

                # Get or create identity
                identity_id = self._get_or_create_identity(person_name)
                processed_identities.add(identity_id)

                # Store reference embedding
                with get_cursor(commit=True) as cur:
                    cur.execute("""
                        INSERT INTO embeddings
                        (identity_id, embedding_type, vector, confidence, source_image_path, is_reference)
                        VALUES (%s, 'face', %s, %s, %s, true)
                    """, (
                        identity_id,
                        result['embedding'],
                        result.get('confidence', 0.0),
                        thumbnail_path
                    ))

                summary['embeddings_count'] += 1
                logger.info(f"Added reference embedding for {person_name} (identity {identity_id})")

            except Exception as e:
                logger.error(f"Error processing detection {detection.get('id')}: {e}")
                summary['errors'].append(f"Detection {detection.get('id')}: {str(e)}")
                summary['skipped'] += 1

        summary['identities_count'] = len(processed_identities)

        # Invalidate cache
        with self._lock:
            self._gallery = None
            self._gallery_timestamp = 0

        logger.info(f"Reference gallery built: {summary}")
        return summary

    def _get_or_create_identity(self, person_name: str) -> str:
        """Find identity by name or create new one. Returns identity_id."""
        with get_cursor(commit=False) as cur:
            cur.execute("SELECT identity_id FROM identities WHERE name = %s AND identity_type = 'person'", (person_name,))
            row = cur.fetchone()
            if row:
                return str(row['identity_id'])

        with get_cursor(commit=True) as cur:
            cur.execute(
                "INSERT INTO identities (name, identity_type) VALUES (%s, 'person') RETURNING identity_id",
                (person_name,)
            )
            return str(cur.fetchone()['identity_id'])

    def _get_embedding(self, image_path: str, bbox: dict = None) -> Optional[Dict]:
        """
        Extract face embedding from image (optionally cropped to bbox).
        Sends to InsightFace API.
        Returns dict with face_detected, embedding, confidence, bbox or None on error.
        """
        try:
            img = cv2.imread(image_path)
            if img is None:
                logger.error(f"Failed to load image: {image_path}")
                return None

            if bbox:
                # Crop with adaptive padding - small faces need more context
                h, w = img.shape[:2]
                face_size = max(bbox['width'], bbox['height'])
                # Small faces (<50px): 200% pad, medium (<100px): 100%, large: 50%
                if face_size < 50:
                    pad_ratio = 2.0
                elif face_size < 100:
                    pad_ratio = 1.0
                else:
                    pad_ratio = 0.5
                pad_x = int(bbox['width'] * pad_ratio)
                pad_y = int(bbox['height'] * pad_ratio)
                x1 = max(0, bbox['x'] - pad_x)
                y1 = max(0, bbox['y'] - pad_y)
                x2 = min(w, bbox['x'] + bbox['width'] + pad_x)
                y2 = min(h, bbox['y'] + bbox['height'] + pad_y)
                img = img[y1:y2, x1:x2]

            _, buf = cv2.imencode('.jpg', img)
            resp = requests.post(
                f"{INSIGHTFACE_URL}/embed",
                files={'image': ('crop.jpg', buf.tobytes(), 'image/jpeg')},
                timeout=10
            )
            if resp.status_code == 200:
                return resp.json()
            else:
                logger.error(f"InsightFace API error: {resp.status_code}")
                return None
        except Exception as e:
            logger.error(f"Embedding extraction failed: {e}")
            return None

    def get_reference_gallery(self) -> Dict[str, Dict]:
        """
        Get cached reference gallery. Returns dict of:
        {identity_id: {"name": str, "embeddings": list[list[float]]}}

        Reloads from DB if cache is stale (>5 min).
        """
        with self._lock:
            if self._gallery and (time.time() - self._gallery_timestamp) < GALLERY_CACHE_TTL:
                return self._gallery

        gallery = {}
        with get_cursor(commit=False) as cur:
            cur.execute("""
                SELECT e.identity_id, e.vector, i.name
                FROM embeddings e
                JOIN identities i ON e.identity_id = i.identity_id
                WHERE e.is_reference = true AND e.embedding_type = 'face'
                AND i.name IS NOT NULL AND i.name NOT LIKE 'Unknown%%'
            """)
            for row in cur.fetchall():
                iid = str(row['identity_id'])
                if iid not in gallery:
                    gallery[iid] = {"name": row['name'], "embeddings": []}
                gallery[iid]["embeddings"].append(row['vector'])

        with self._lock:
            self._gallery = gallery
            self._gallery_timestamp = time.time()

        logger.info(f"Reference gallery loaded: {len(gallery)} identities")
        return gallery

    def recognize_face(self, embedding: List[float]) -> Optional[Dict]:
        """
        Match a face embedding against the reference gallery.

        Returns best match dict {identity_id, name, similarity} if above threshold, else None.
        Uses cosine similarity, comparing against mean embedding per identity.
        """
        gallery = self.get_reference_gallery()
        if not gallery:
            return None

        query_vec = np.array(embedding)
        query_norm = np.linalg.norm(query_vec)
        if query_norm == 0:
            return None
        query_vec = query_vec / query_norm

        best_match = None
        best_sim = -1

        for iid, data in gallery.items():
            # Compute mean embedding for this identity
            ref_vecs = np.array(data["embeddings"])
            mean_vec = np.mean(ref_vecs, axis=0)
            mean_norm = np.linalg.norm(mean_vec)
            if mean_norm == 0:
                continue
            mean_vec = mean_vec / mean_norm

            sim = float(np.dot(query_vec, mean_vec))
            if sim > best_sim:
                best_sim = sim
                best_match = {"identity_id": iid, "name": data["name"], "similarity": sim}

        if best_match and best_match["similarity"] >= SIMILARITY_THRESHOLD:
            return best_match
        return None

    def recognize_faces_in_thumbnail(self, thumbnail_path: str, face_detections: List[Dict]) -> List[Dict]:
        """
        Run recognition on all face detections in a thumbnail.

        Args:
            thumbnail_path: Path to the thumbnail image
            face_detections: List of dicts with bbox keys (x, y, width, height)

        Returns:
            List of prediction dicts for person_identification scenario
        """
        results = []

        for det in face_detections:
            bbox = det.get('bbox', det)
            result = self._get_embedding(thumbnail_path, bbox)
            if not result or not result.get('face_detected'):
                continue

            match = self.recognize_face(result['embedding'])
            if match:
                results.append({
                    'prediction_type': 'keyframe',
                    'confidence': round(match['similarity'], 4),
                    'timestamp': 0.0,
                    'bbox': bbox,
                    'scenario': 'person_identification',
                    'tags': {
                        'person_name': match['name'],
                        'identity_id': match['identity_id'],
                        'match_similarity': round(match['similarity'], 4),
                        'class': 'person',
                        'class_id': 0
                    }
                })

        return results

    def gallery_stats(self) -> Dict:
        """Return gallery statistics for the API."""
        gallery = self.get_reference_gallery()
        identities = []
        for iid, data in gallery.items():
            identities.append({
                "identity_id": iid,
                "name": data["name"],
                "embedding_count": len(data["embeddings"])
            })
        return {
            "total_identities": len(identities),
            "total_embeddings": sum(i["embedding_count"] for i in identities),
            "identities": identities
        }


# Singleton instance
_recognizer = None
_recognizer_lock = threading.Lock()

def get_recognizer() -> PersonRecognizer:
    """Get or create singleton PersonRecognizer instance."""
    global _recognizer
    if _recognizer is None:
        with _recognizer_lock:
            if _recognizer is None:
                _recognizer = PersonRecognizer()
    return _recognizer
