#!/usr/bin/env python3
"""
Migrate flat classification values to tiered hierarchy columns.

Backfills vehicle_tier1, vehicle_tier2, vehicle_tier3 on ai_predictions
using the classification_hierarchy table and EXISTING_CLASS_MAP.

Usage:
    cd /opt/groundtruth-studio/app && python ../scripts/migrate_flat_to_tiered.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

from db_connection import init_connection_pool, get_cursor, close_connection_pool

# Map from existing flat class names (lowercased) to (tier1, tier2, tier3)
FLAT_TO_TIER = {
    'sedan': ('vehicle', 'small_vehicle', 'sedan'),
    'car': ('vehicle', 'small_vehicle', 'sedan'),
    'pickup truck': ('vehicle', 'pickup', 'pickup — open bed'),
    'pickup': ('vehicle', 'pickup', 'pickup — open bed'),
    'suv': ('vehicle', 'large_vehicle', 'SUV'),
    'minivan': ('vehicle', 'large_vehicle', 'minivan'),
    'van': ('vehicle', 'large_vehicle', 'full-size van (panel van)'),
    'tractor': ('vehicle', 'commercial_truck', None),
    'atv': ('vehicle', 'offroad', 'ATV'),
    'utv': ('vehicle', 'offroad', 'UTV/side-by-side'),
    'snowmobile': ('vehicle', 'offroad', 'snowmobile'),
    'golf cart': ('vehicle', 'offroad', 'golf cart'),
    'motorcycle': ('motorcycle', 'motorcycle', None),
    'trailer': ('trailer', 'utility_trailer', None),
    'bus': ('vehicle', 'bus', None),
    'semi truck': ('vehicle', 'commercial_truck', None),
    'dump truck': ('vehicle', 'commercial_truck', 'dump truck'),
    'box truck': ('vehicle', 'commercial_truck', 'box truck'),
    'delivery truck': ('vehicle', 'commercial_truck', 'box truck'),
    'truck': ('vehicle', 'commercial_truck', None),
    'rowboat': ('boat', 'non_motorized', None),
    'fishing boat': ('boat', 'powerboat', 'bass boat'),
    'speed boat': ('boat', 'powerboat', 'center console'),
    'pontoon boat': ('boat', 'powerboat', 'pontoon'),
    'boat': ('boat', 'powerboat', None),
    'kayak': ('boat', 'non_motorized', 'kayak'),
    'canoe': ('boat', 'non_motorized', 'canoe'),
    'sailboat': ('boat', 'non_motorized', None),
    'jet ski': ('boat', 'personal_watercraft', 'jet ski'),
    'person': ('person', 'person', None),
    'skid loader': ('vehicle', 'commercial_truck', None),
    'fence': (None, None, None),  # non-vehicle, skip
    'ambulance': ('vehicle', 'large_vehicle', None),
    'other truck': ('vehicle', 'commercial_truck', None),
    'multiple_vehicles': ('vehicle', None, None),
    'unknown vehicle': ('vehicle', None, None),
}


def main():
    print("Initializing connection pool...")
    init_connection_pool()

    try:
        total_updated = 0
        skipped = 0

        with get_cursor() as cursor:
            # First try hierarchy table lookup
            cursor.execute("""
                SELECT DISTINCT LOWER(TRIM(classification)) AS cls
                FROM ai_predictions
                WHERE classification IS NOT NULL
                  AND vehicle_tier1 IS NULL
            """)
            classes = [r['cls'] for r in cursor.fetchall()]
            print(f"Found {len(classes)} distinct untiered classifications")

            for cls in classes:
                if not cls:
                    continue

                # Check hierarchy table first
                cursor.execute("""
                    SELECT tier1, tier2, tier3 FROM classification_hierarchy
                    WHERE LOWER(tier3) = %s OR LOWER(tier2) = %s
                      OR LOWER(display_name) = %s
                    LIMIT 1
                """, (cls, cls, cls))
                row = cursor.fetchone()

                if row:
                    tier1, tier2, tier3 = row['tier1'], row['tier2'], row['tier3']
                elif cls in FLAT_TO_TIER:
                    tier1, tier2, tier3 = FLAT_TO_TIER[cls]
                else:
                    print(f"  No mapping for '{cls}', skipping")
                    skipped += 1
                    continue

                if not tier1:
                    continue

                cursor.execute("""
                    UPDATE ai_predictions
                    SET vehicle_tier1 = %s, vehicle_tier2 = %s, vehicle_tier3 = %s
                    WHERE LOWER(TRIM(classification)) = %s
                      AND vehicle_tier1 IS NULL
                """, (tier1, tier2, tier3, cls))
                count = cursor.rowcount
                if count:
                    print(f"  {cls} -> {tier1}/{tier2}/{tier3}: {count} predictions")
                    total_updated += count

            # Also backfill from predicted_tags/corrected_tags for predictions without classification
            cursor.execute("""
                UPDATE ai_predictions
                SET vehicle_tier1 = 'vehicle'
                WHERE vehicle_tier1 IS NULL
                  AND classification IS NULL
                  AND scenario = 'vehicle_detection'
                  AND review_status IN ('approved', 'pending', 'processing')
            """)
            generic = cursor.rowcount
            if generic:
                print(f"  Set tier1='vehicle' for {generic} vehicle_detection predictions without classification")
                total_updated += generic

        print(f"\nMigration complete: {total_updated} predictions updated, {skipped} classes skipped")

    finally:
        close_connection_pool()


if __name__ == '__main__':
    main()
