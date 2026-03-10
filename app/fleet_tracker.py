"""
Golf Cart Fleet Utilization Logging — tracks fleet vehicle deployment.

OCR-based identification for fleet carts, ReID for private carts.
Departure/return tracking per cart identity. VLM-generated descriptions
for private carts. Fleet utilization dashboard data.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from uuid import uuid4

from db_connection import get_cursor

logger = logging.getLogger(__name__)


class FleetTracker:
    """Tracks golf cart and fleet vehicle deployment and utilization."""

    def register_fleet_vehicle(self, fleet_id: str, fleet_type: str,
                                description: str = None,
                                metadata: Dict = None) -> Dict:
        """Register a new fleet vehicle.

        Args:
            fleet_id: Unique fleet identifier (e.g., cart number)
            fleet_type: Vehicle type (e.g., 'golf_cart', 'utility_cart')
            description: Optional description
            metadata: Optional metadata dict

        Returns:
            dict with registration details
        """
        import json
        with get_cursor() as cursor:
            cursor.execute("""
                INSERT INTO fleet_vehicles (fleet_id, fleet_type, description, metadata)
                VALUES (%s, %s, %s, %s::jsonb)
                ON CONFLICT (fleet_id) DO UPDATE SET
                    fleet_type = EXCLUDED.fleet_type,
                    description = COALESCE(EXCLUDED.description, fleet_vehicles.description),
                    metadata = COALESCE(EXCLUDED.metadata, fleet_vehicles.metadata)
                RETURNING id, fleet_id, fleet_type
            """, (fleet_id, fleet_type, description,
                  json.dumps(metadata) if metadata else None))
            row = cursor.fetchone()

        return dict(row) if row else {'fleet_id': fleet_id, 'fleet_type': fleet_type}

    def record_sighting(self, fleet_id: str, camera_id: str,
                        prediction_id: int = None,
                        direction: str = None) -> Dict:
        """Record a fleet vehicle sighting at a camera.

        Args:
            fleet_id: Fleet vehicle identifier
            camera_id: Camera where sighting occurred
            prediction_id: Associated prediction ID
            direction: Travel direction ('departing', 'arriving', 'passing')

        Returns:
            dict with sighting details and inferred status
        """
        now = datetime.now(timezone.utc)

        # Get or create identity for this fleet vehicle
        identity_id = self._get_or_create_identity(fleet_id)

        # Record sighting
        with get_cursor() as cursor:
            cursor.execute("""
                INSERT INTO sightings (identity_id, prediction_id, sighting_type, metadata)
                VALUES (%s, %s, 'fleet_tracking', %s::jsonb)
            """, (str(identity_id), prediction_id,
                  __import__('json').dumps({
                      'camera_id': camera_id,
                      'direction': direction,
                      'timestamp': now.isoformat(),
                  })))

            # Update fleet vehicle last_seen
            cursor.execute("""
                UPDATE fleet_vehicles SET metadata = COALESCE(metadata, '{}'::jsonb) ||
                    jsonb_build_object('last_seen', %s, 'last_camera', %s)
                WHERE fleet_id = %s
            """, (now.isoformat(), camera_id, fleet_id))

        # Infer status from direction
        status = 'unknown'
        if direction == 'departing':
            status = 'deployed'
        elif direction == 'arriving':
            status = 'returned'

        return {
            'fleet_id': fleet_id,
            'camera_id': camera_id,
            'direction': direction,
            'status': status,
            'timestamp': now.isoformat(),
        }

    def _get_or_create_identity(self, fleet_id: str) -> str:
        """Get or create an identity for a fleet vehicle."""
        import json

        with get_cursor() as cursor:
            cursor.execute("""
                SELECT identity_id FROM identities
                WHERE metadata->>'fleet_id' = %s AND identity_type = 'vehicle'
                LIMIT 1
            """, (fleet_id,))
            row = cursor.fetchone()

            if row:
                return row['identity_id']

            identity_id = uuid4()
            cursor.execute("""
                INSERT INTO identities (identity_id, identity_type, name, metadata)
                VALUES (%s, 'vehicle', %s, %s::jsonb)
            """, (str(identity_id), f"Fleet {fleet_id}",
                  json.dumps({'fleet_id': fleet_id, 'is_fleet': True})))

            return str(identity_id)

    def get_fleet_status(self) -> List[Dict]:
        """Get current status of all fleet vehicles.

        Returns:
            List of fleet vehicles with last sighting and inferred status
        """
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT fv.fleet_id, fv.fleet_type, fv.description,
                       fv.metadata, fv.is_active
                FROM fleet_vehicles fv
                WHERE fv.is_active = TRUE
                ORDER BY fv.fleet_id
            """)
            vehicles = [dict(r) for r in cursor.fetchall()]

        for v in vehicles:
            meta = v.get('metadata') or {}
            v['last_seen'] = meta.get('last_seen')
            v['last_camera'] = meta.get('last_camera')

        return vehicles

    def get_utilization_report(self, days_back: int = 7) -> Dict:
        """Generate fleet utilization report.

        Args:
            days_back: How many days of history to analyze

        Returns:
            dict with per-vehicle utilization stats
        """
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT fv.fleet_id, fv.fleet_type,
                       COUNT(s.id) AS sighting_count
                FROM fleet_vehicles fv
                LEFT JOIN identities i ON i.metadata->>'fleet_id' = fv.fleet_id
                LEFT JOIN sightings s ON s.identity_id = i.identity_id
                    AND s.created_at > NOW() - INTERVAL '%s days'
                WHERE fv.is_active = TRUE
                GROUP BY fv.fleet_id, fv.fleet_type
                ORDER BY sighting_count DESC
            """, (days_back,))
            rows = cursor.fetchall()

        total_vehicles = len(rows)
        active_vehicles = sum(1 for r in rows if r['sighting_count'] > 0)

        return {
            'period_days': days_back,
            'total_vehicles': total_vehicles,
            'active_vehicles': active_vehicles,
            'utilization_rate': round(active_vehicles / max(1, total_vehicles), 2),
            'vehicles': [dict(r) for r in rows],
        }

    def identify_by_ocr(self, prediction_id: int) -> Optional[str]:
        """Try to identify a fleet vehicle by OCR on its number.

        Args:
            prediction_id: Prediction to check for fleet number

        Returns:
            fleet_id if identified, None otherwise
        """
        try:
            from plate_reader import PlateReader
            reader = PlateReader()

            with get_cursor(commit=False) as cursor:
                cursor.execute("""
                    SELECT p.id, p.video_id, p.timestamp,
                           p.bbox_x, p.bbox_y, p.bbox_width, p.bbox_height,
                           v.filename
                    FROM ai_predictions p
                    JOIN videos v ON v.id = p.video_id
                    WHERE p.id = %s
                """, (prediction_id,))
                pred = cursor.fetchone()

            if not pred:
                return None

            from vlm_reviewer import _extract_frame, _crop_with_padding, _image_to_base64
            from PIL import Image
            from io import BytesIO

            frame_bytes = _extract_frame(pred['filename'], pred['timestamp'] or 0)
            if not frame_bytes:
                return None

            full_frame = Image.open(BytesIO(frame_bytes)).convert('RGB')
            bbox = {
                'x': pred['bbox_x'], 'y': pred['bbox_y'],
                'width': pred['bbox_width'], 'height': pred['bbox_height']
            }
            crop = _crop_with_padding(full_frame, bbox, padding=0.2)
            crop_b64 = _image_to_base64(crop)

            text_result = reader.extract_plate_text(crop_b64)
            if not text_result:
                return None

            # Check if extracted text matches a fleet ID
            plate_text = text_result['plate_text'].strip()
            with get_cursor(commit=False) as cursor:
                cursor.execute("""
                    SELECT fleet_id FROM fleet_vehicles
                    WHERE fleet_id = %s AND is_active = TRUE
                """, (plate_text,))
                row = cursor.fetchone()

            if row:
                return row['fleet_id']

            # Also check if it's a short number (1-3 digits) typical of golf carts
            if plate_text.isdigit() and len(plate_text) <= 3:
                with get_cursor(commit=False) as cursor:
                    cursor.execute("""
                        SELECT fleet_id FROM fleet_vehicles
                        WHERE fleet_id LIKE %s AND is_active = TRUE
                    """, (f"%{plate_text}%",))
                    row = cursor.fetchone()
                if row:
                    return row['fleet_id']

            return None

        except Exception as e:
            logger.debug(f"Fleet OCR identification failed: {e}")
            return None
