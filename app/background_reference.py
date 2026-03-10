"""
Background Reference Image Auto-Generation — clean background plates per camera.

Identifies time windows with no active detections, median-stacks frames
from quiet periods to produce clean background images for synthetic
training compositing and crop quality filtering.
"""

import logging
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from db_connection import get_cursor

logger = logging.getLogger(__name__)

DOWNLOAD_DIR = os.environ.get('DOWNLOAD_DIR', '/opt/groundtruth-studio/downloads')

# Time categories for background references
TIME_CATEGORIES = {
    'morning': (6, 10),
    'midday': (10, 14),
    'afternoon': (14, 17),
    'dusk': (17, 20),
    'night': (20, 6),
}

SEASON_MAP = {
    12: 'winter', 1: 'winter', 2: 'winter',
    3: 'spring', 4: 'spring', 5: 'spring',
    6: 'summer', 7: 'summer', 8: 'summer',
    9: 'fall', 10: 'fall', 11: 'fall',
}


class BackgroundReferenceGenerator:
    """Generates clean background reference images from quiet camera periods."""

    def find_quiet_windows(self, camera_id: str, hours_back: int = 24,
                           min_gap_minutes: int = 5) -> List[Dict]:
        """Find time windows with no detections for a camera.

        Args:
            camera_id: Camera identifier
            hours_back: How far back to search
            min_gap_minutes: Minimum gap duration to consider

        Returns:
            List of dicts with start_time, end_time, duration_minutes
        """
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT p.created_at
                FROM ai_predictions p
                JOIN videos v ON v.id = p.video_id
                WHERE v.camera_id = %s
                  AND p.created_at > NOW() - INTERVAL '%s hours'
                ORDER BY p.created_at
            """, (camera_id, hours_back))
            rows = cursor.fetchall()

        if not rows:
            return []

        gaps = []
        for i in range(1, len(rows)):
            prev_time = rows[i - 1]['created_at']
            curr_time = rows[i]['created_at']
            gap = (curr_time - prev_time).total_seconds() / 60.0

            if gap >= min_gap_minutes:
                gaps.append({
                    'start_time': prev_time,
                    'end_time': curr_time,
                    'duration_minutes': round(gap, 1),
                })

        return gaps

    def get_time_category(self, dt: datetime) -> str:
        """Classify a datetime into a time-of-day category."""
        hour = dt.hour
        for category, (start, end) in TIME_CATEGORIES.items():
            if category == 'night':
                if hour >= start or hour < end:
                    return category
            elif start <= hour < end:
                return category
        return 'midday'

    def get_season(self, dt: datetime) -> str:
        """Get season from datetime."""
        return SEASON_MAP.get(dt.month, 'summer')

    def generate_background(self, camera_id: str, video_path: str,
                            timestamps: List[float],
                            output_dir: str = None) -> Optional[str]:
        """Generate a median-stacked background image from multiple frames.

        Args:
            camera_id: Camera identifier
            video_path: Path to video file
            timestamps: List of timestamps to sample
            output_dir: Output directory for the image

        Returns:
            Path to generated background image, or None
        """
        try:
            import numpy as np
            from PIL import Image
            from io import BytesIO
            from vlm_reviewer import _extract_frame

            frames = []
            for ts in timestamps[:20]:  # Cap at 20 frames
                frame_bytes = _extract_frame(video_path, ts)
                if frame_bytes:
                    img = Image.open(BytesIO(frame_bytes)).convert('RGB')
                    frames.append(np.array(img))

            if len(frames) < 3:
                return None

            # Median stack — robust to transient objects
            stacked = np.median(np.array(frames), axis=0).astype(np.uint8)
            result = Image.fromarray(stacked)

            if output_dir is None:
                output_dir = os.path.join(DOWNLOAD_DIR, 'backgrounds')
            os.makedirs(output_dir, exist_ok=True)

            now = datetime.now()
            time_cat = self.get_time_category(now)
            season = self.get_season(now)
            filename = f"bg_{camera_id}_{time_cat}_{season}_{now.strftime('%Y%m%d_%H%M%S')}.jpg"
            output_path = os.path.join(output_dir, filename)
            result.save(output_path, 'JPEG', quality=95)

            # Store in DB
            with get_cursor() as cursor:
                cursor.execute("""
                    INSERT INTO background_references
                        (camera_id, image_path, time_category, season, frame_count)
                    VALUES (%s, %s, %s, %s, %s)
                """, (camera_id, output_path, time_cat, season, len(frames)))

            return output_path

        except Exception as e:
            logger.warning(f"Background generation failed for {camera_id}: {e}")
            return None

    def get_background(self, camera_id: str, time_category: str = None,
                       season: str = None) -> Optional[str]:
        """Get the most recent background reference for a camera.

        Args:
            camera_id: Camera identifier
            time_category: Optional time-of-day filter
            season: Optional season filter

        Returns:
            Path to background image, or None
        """
        with get_cursor(commit=False) as cursor:
            query = """
                SELECT image_path FROM background_references
                WHERE camera_id = %s
            """
            params = [camera_id]

            if time_category:
                query += " AND time_category = %s"
                params.append(time_category)
            if season:
                query += " AND season = %s"
                params.append(season)

            query += " ORDER BY created_at DESC LIMIT 1"
            cursor.execute(query, params)
            row = cursor.fetchone()

        if row and os.path.exists(row['image_path']):
            return row['image_path']
        return None

    def get_all_references(self, camera_id: str = None) -> List[Dict]:
        """List all background references."""
        with get_cursor(commit=False) as cursor:
            if camera_id:
                cursor.execute("""
                    SELECT id, camera_id, image_path, time_category, season,
                           frame_count, created_at
                    FROM background_references
                    WHERE camera_id = %s
                    ORDER BY created_at DESC
                """, (camera_id,))
            else:
                cursor.execute("""
                    SELECT id, camera_id, image_path, time_category, season,
                           frame_count, created_at
                    FROM background_references
                    ORDER BY created_at DESC
                """)
            return [dict(r) for r in cursor.fetchall()]
