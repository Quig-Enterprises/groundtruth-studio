"""
Synthetic Training Data Pipeline — generates camera-realistic synthetic crops.

Acquires high-res source images for underrepresented classes, applies
camera-specific degradation profiles, and composites onto background
reference plates. Tagged as source_type='synthetic' in training gallery.
"""

import logging
import os
import random
from typing import Dict, List, Optional, Tuple

from db_connection import get_cursor

logger = logging.getLogger(__name__)

DOWNLOAD_DIR = os.environ.get('DOWNLOAD_DIR', '/opt/groundtruth-studio/downloads')


class SyntheticGenerator:
    """Generates camera-realistic synthetic training data."""

    def get_degradation_profile(self, camera_id: str,
                                 distance_bucket: str = 'mid') -> Optional[Dict]:
        """Get camera-specific degradation profile.

        Args:
            camera_id: Camera identifier
            distance_bucket: 'near', 'mid', or 'far'

        Returns:
            dict with noise_level, compression_quality, motion_blur_kernel, color_profile
        """
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT avg_crop_width, avg_crop_height, noise_level,
                       compression_quality, motion_blur_kernel, color_profile,
                       sample_count
                FROM camera_degradation_profiles
                WHERE camera_id = %s AND distance_bucket = %s
            """, (camera_id, distance_bucket))
            row = cursor.fetchone()

        if row:
            return dict(row)
        return None

    def learn_degradation_profile(self, camera_id: str) -> Dict:
        """Learn camera degradation profile from approved predictions.

        Analyzes crop quality metrics from approved predictions to build
        a degradation profile per distance bucket.

        Returns:
            dict with per-bucket profile summaries
        """
        profiles = {}
        for bucket, size_range in [('near', (200, 99999)), ('mid', (80, 200)), ('far', (20, 80))]:
            with get_cursor(commit=False) as cursor:
                cursor.execute("""
                    SELECT AVG(bbox_width) AS avg_w, AVG(bbox_height) AS avg_h,
                           AVG(quality_score) AS avg_quality,
                           COUNT(*) AS cnt
                    FROM ai_predictions p
                    JOIN videos v ON v.id = p.video_id
                    WHERE v.camera_id = %s
                      AND review_status IN ('approved', 'auto_approved')
                      AND GREATEST(bbox_width, bbox_height) BETWEEN %s AND %s
                """, (camera_id, size_range[0], size_range[1]))
                row = cursor.fetchone()

            if not row or row['cnt'] < 10:
                continue

            # Derive degradation parameters from quality metrics
            avg_quality = row['avg_quality'] or 0.5
            noise_level = max(0.0, 1.0 - avg_quality) * 0.3
            compression = max(40, min(95, int(avg_quality * 100)))
            blur_kernel = max(0.0, (1.0 - avg_quality) * 3.0) if bucket == 'far' else 0.0

            with get_cursor() as cursor:
                cursor.execute("""
                    INSERT INTO camera_degradation_profiles
                        (camera_id, distance_bucket, avg_crop_width, avg_crop_height,
                         noise_level, compression_quality, motion_blur_kernel, sample_count)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (camera_id, distance_bucket) DO UPDATE SET
                        avg_crop_width = EXCLUDED.avg_crop_width,
                        avg_crop_height = EXCLUDED.avg_crop_height,
                        noise_level = EXCLUDED.noise_level,
                        compression_quality = EXCLUDED.compression_quality,
                        motion_blur_kernel = EXCLUDED.motion_blur_kernel,
                        sample_count = EXCLUDED.sample_count
                """, (camera_id, bucket, row['avg_w'], row['avg_h'],
                      noise_level, compression, blur_kernel, row['cnt']))

            profiles[bucket] = {
                'avg_crop_size': (row['avg_w'], row['avg_h']),
                'noise_level': noise_level,
                'compression_quality': compression,
                'motion_blur_kernel': blur_kernel,
                'sample_count': row['cnt'],
            }

        return profiles

    def apply_degradation(self, image, profile: Dict) -> 'Image':
        """Apply camera-specific degradation to a clean source image.

        Args:
            image: PIL Image (clean, high-res source)
            profile: Degradation profile dict

        Returns:
            Degraded PIL Image
        """
        import numpy as np
        from PIL import Image, ImageFilter

        img = image.copy()

        # Resize to match camera's typical crop size
        target_w = int(profile.get('avg_crop_width', img.width))
        target_h = int(profile.get('avg_crop_height', img.height))
        if target_w > 0 and target_h > 0:
            img = img.resize((target_w, target_h), Image.LANCZOS)

        # Add Gaussian noise
        noise_level = profile.get('noise_level', 0.0)
        if noise_level > 0:
            arr = np.array(img).astype(np.float32)
            noise = np.random.normal(0, noise_level * 255, arr.shape)
            arr = np.clip(arr + noise, 0, 255).astype(np.uint8)
            img = Image.fromarray(arr)

        # Motion blur
        blur_kernel = profile.get('motion_blur_kernel', 0.0)
        if blur_kernel > 0.5:
            img = img.filter(ImageFilter.GaussianBlur(radius=blur_kernel))

        # JPEG compression artifacts
        compression = int(profile.get('compression_quality', 85))
        if compression < 95:
            from io import BytesIO
            buf = BytesIO()
            img.save(buf, 'JPEG', quality=compression)
            buf.seek(0)
            img = Image.open(buf).convert('RGB')

        return img

    def composite_on_background(self, foreground, background,
                                 position: Tuple[int, int]) -> 'Image':
        """Composite a foreground crop onto a background plate.

        Args:
            foreground: PIL Image of the object crop
            background: PIL Image of the clean background
            position: (x, y) placement coordinates

        Returns:
            Composited PIL Image
        """
        from PIL import Image

        bg = background.copy()
        x, y = position

        # Ensure foreground fits within background
        if x + foreground.width > bg.width:
            x = max(0, bg.width - foreground.width)
        if y + foreground.height > bg.height:
            y = max(0, bg.height - foreground.height)

        bg.paste(foreground, (x, y))
        return bg

    def generate_synthetic_prediction(self, camera_id: str, classification: str,
                                       source_image_path: str,
                                       video_id: int) -> Optional[Dict]:
        """Generate a synthetic training prediction.

        Args:
            camera_id: Target camera for realistic degradation
            classification: Vehicle class to generate
            source_image_path: Path to clean source image
            video_id: Video to associate the synthetic prediction with

        Returns:
            dict with prediction details, or None
        """
        try:
            from PIL import Image
            from background_reference import BackgroundReferenceGenerator

            source = Image.open(source_image_path).convert('RGB')
            profile = self.get_degradation_profile(camera_id, 'mid')
            if not profile:
                profile = {'avg_crop_width': 150, 'avg_crop_height': 100,
                           'noise_level': 0.05, 'compression_quality': 75}

            degraded = self.apply_degradation(source, profile)

            # Try to get a background
            bg_gen = BackgroundReferenceGenerator()
            bg_path = bg_gen.get_background(camera_id)
            if bg_path:
                background = Image.open(bg_path).convert('RGB')
                x = random.randint(0, max(0, background.width - degraded.width))
                y = random.randint(0, max(0, background.height - degraded.height))
                composited = self.composite_on_background(degraded, background, (x, y))

                bbox = {'x': x, 'y': y,
                        'width': degraded.width, 'height': degraded.height}
            else:
                composited = degraded
                bbox = {'x': 0, 'y': 0,
                        'width': degraded.width, 'height': degraded.height}

            # Save synthetic crop
            output_dir = os.path.join(DOWNLOAD_DIR, 'synthetic')
            os.makedirs(output_dir, exist_ok=True)
            import time
            filename = f"syn_{camera_id}_{classification}_{int(time.time())}.jpg"
            output_path = os.path.join(output_dir, filename)
            composited.save(output_path, 'JPEG', quality=90)

            return {
                'camera_id': camera_id,
                'classification': classification,
                'source_type': 'synthetic',
                'image_path': output_path,
                'bbox': bbox,
                'degradation_profile': profile,
            }

        except Exception as e:
            logger.warning(f"Synthetic generation failed: {e}")
            return None

    def get_underrepresented_classes(self, min_count: int = 50) -> List[Dict]:
        """Find classes with fewer than min_count approved predictions.

        Returns:
            List of dicts with classification, count, deficit
        """
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT classification, COUNT(*) AS cnt
                FROM ai_predictions
                WHERE review_status IN ('approved', 'auto_approved')
                  AND classification IS NOT NULL
                GROUP BY classification
                ORDER BY cnt ASC
            """)
            rows = cursor.fetchall()

        underrepresented = []
        for row in rows:
            if row['cnt'] < min_count:
                underrepresented.append({
                    'classification': row['classification'],
                    'count': row['cnt'],
                    'deficit': min_count - row['cnt'],
                })

        return underrepresented
