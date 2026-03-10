#!/usr/bin/env python3
"""
Seed script for classification_hierarchy and classification_roles tables.

Populates the full vehicle/boat/trailer/motorcycle/person classification
hierarchy and the 10 standard annotation roles.

Usage:
    cd /opt/groundtruth-studio/app && python ../scripts/seed_classification_hierarchy.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

from db_connection import init_connection_pool, get_cursor, close_connection_pool

# (tier1, tier2, tier3, yolo_prompt, display_name, enforcement_eligible, disqualification_reason)
HIERARCHY_ROWS = [
    # VEHICLE — small_vehicle
    ('vehicle', 'small_vehicle', 'sedan', 'sedan car', 'Sedan', False, None),
    ('vehicle', 'small_vehicle', 'hatchback', 'hatchback car', 'Hatchback', False, None),
    ('vehicle', 'small_vehicle', 'coupe', 'coupe car', 'Coupe', False, None),
    ('vehicle', 'small_vehicle', 'wagon/crossover', 'station wagon or crossover vehicle', 'Wagon / Crossover', False, None),
    # VEHICLE — large_vehicle
    ('vehicle', 'large_vehicle', 'SUV', 'SUV sport utility vehicle', 'SUV', False, None),
    ('vehicle', 'large_vehicle', 'minivan', 'minivan', 'Minivan', False, None),
    ('vehicle', 'large_vehicle', 'full-size van (panel van)', 'full-size panel van', 'Full-Size Van (Panel Van)', False, None),
    ('vehicle', 'large_vehicle', 'cargo van', 'cargo van', 'Cargo Van', False, None),
    # VEHICLE — pickup
    ('vehicle', 'pickup', 'pickup — open bed', 'pickup truck open bed', 'Pickup — Open Bed', False, None),
    ('vehicle', 'pickup', 'pickup — with cap/topper', 'pickup truck with cap topper', 'Pickup — With Cap / Topper', False, None),
    ('vehicle', 'pickup', 'pickup — with toolbox', 'pickup truck with toolbox', 'Pickup — With Toolbox', False, None),
    ('vehicle', 'pickup', 'pickup — flatbed', 'pickup truck flatbed', 'Pickup — Flatbed', False, None),
    # VEHICLE — commercial_truck
    ('vehicle', 'commercial_truck', 'box truck', 'box truck', 'Box Truck', False, None),
    ('vehicle', 'commercial_truck', 'flatbed truck', 'flatbed truck', 'Flatbed Truck', False, None),
    ('vehicle', 'commercial_truck', 'tow truck', 'tow truck wrecker', 'Tow Truck', False, None),
    ('vehicle', 'commercial_truck', 'dump truck', 'dump truck', 'Dump Truck', False, None),
    ('vehicle', 'commercial_truck', 'garbage truck', 'garbage truck refuse truck', 'Garbage Truck', False, None),
    ('vehicle', 'commercial_truck', 'tanker/propane', 'tanker truck propane truck', 'Tanker / Propane Truck', False, None),
    ('vehicle', 'commercial_truck', 'cement mixer', 'cement mixer truck', 'Cement Mixer', False, None),
    ('vehicle', 'commercial_truck', 'utility truck (bucket)', 'utility truck bucket truck', 'Utility Truck (Bucket)', False, None),
    # VEHICLE — bus
    ('vehicle', 'bus', 'school bus', 'school bus', 'School Bus', False, None),
    ('vehicle', 'bus', 'transit bus', 'city transit bus', 'Transit Bus', False, None),
    ('vehicle', 'bus', 'shuttle/minibus', 'shuttle bus minibus', 'Shuttle / Minibus', False, None),
    ('vehicle', 'bus', 'coach bus', 'coach bus motor coach', 'Coach Bus', False, None),
    # VEHICLE — offroad
    ('vehicle', 'offroad', 'ATV', 'ATV all-terrain vehicle', 'ATV', False, 'not_watercraft'),
    ('vehicle', 'offroad', 'UTV/side-by-side', 'UTV side-by-side off-road vehicle', 'UTV / Side-by-Side', False, 'not_watercraft'),
    ('vehicle', 'offroad', 'snowmobile', 'snowmobile', 'Snowmobile', False, 'not_watercraft'),
    ('vehicle', 'offroad', 'golf cart', 'golf cart', 'Golf Cart', False, None),
    # TRAILER — boat_trailer
    ('trailer', 'boat_trailer', 'single-axle', 'single-axle boat trailer', 'Boat Trailer — Single Axle', True, None),
    ('trailer', 'boat_trailer', 'tandem-axle', 'tandem-axle boat trailer', 'Boat Trailer — Tandem Axle', True, None),
    ('trailer', 'boat_trailer', 'triple-axle', 'triple-axle boat trailer', 'Boat Trailer — Triple Axle', True, None),
    # TRAILER — utility_trailer
    ('trailer', 'utility_trailer', 'flatbed trailer', 'flatbed utility trailer', 'Flatbed Trailer', False, None),
    ('trailer', 'utility_trailer', 'landscape trailer', 'landscape trailer open utility trailer', 'Landscape Trailer', False, None),
    ('trailer', 'utility_trailer', 'car hauler', 'car hauler auto transport trailer', 'Car Hauler', False, None),
    # TRAILER — enclosed_trailer
    ('trailer', 'enclosed_trailer', 'cargo trailer', 'enclosed cargo trailer', 'Cargo Trailer', False, None),
    ('trailer', 'enclosed_trailer', 'camper/RV trailer', 'camper RV travel trailer', 'Camper / RV Trailer', False, None),
    # TRAILER — snowmobile_trailer
    ('trailer', 'snowmobile_trailer', 'snowmobile trailer — open', 'open snowmobile trailer', 'Snowmobile Trailer — Open', False, None),
    ('trailer', 'snowmobile_trailer', 'snowmobile trailer — enclosed', 'enclosed snowmobile trailer', 'Snowmobile Trailer — Enclosed', False, None),
    # BOAT — powerboat
    ('boat', 'powerboat', 'bass boat', 'bass boat fishing boat', 'Bass Boat', True, None),
    ('boat', 'powerboat', 'pontoon', 'pontoon boat', 'Pontoon', True, None),
    ('boat', 'powerboat', 'center console', 'center console boat speed boat', 'Center Console', True, None),
    ('boat', 'powerboat', 'cabin cruiser', 'cabin cruiser motorboat', 'Cabin Cruiser', True, None),
    ('boat', 'powerboat', 'jon boat', 'jon boat flat bottom boat', 'Jon Boat', True, None),
    # BOAT — personal_watercraft
    ('boat', 'personal_watercraft', 'jet ski', 'jet ski personal watercraft', 'Jet Ski', True, None),
    # BOAT — non_motorized
    ('boat', 'non_motorized', 'kayak', 'kayak', 'Kayak', False, 'non_motorized'),
    ('boat', 'non_motorized', 'canoe', 'canoe', 'Canoe', False, 'non_motorized'),
    ('boat', 'non_motorized', 'paddleboard', 'stand up paddleboard SUP', 'Paddleboard', False, 'non_motorized'),
    # MOTORCYCLE
    ('motorcycle', 'motorcycle', None, 'motorcycle', 'Motorcycle', False, None),
    # PERSON
    ('person', 'person', None, 'person pedestrian', 'Person', False, None),
]

# Mapping from existing flat VEHICLE_CLASSES to hierarchy
EXISTING_CLASS_MAP = {
    'sedan': ('vehicle', 'small_vehicle', 'sedan'),
    'pickup truck': ('vehicle', 'pickup', 'pickup — open bed'),
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
    'rowboat': ('boat', 'non_motorized', None),
    'fishing boat': ('boat', 'powerboat', 'bass boat'),
    'speed boat': ('boat', 'powerboat', 'center console'),
    'pontoon boat': ('boat', 'powerboat', 'pontoon'),
    'kayak': ('boat', 'non_motorized', 'kayak'),
    'canoe': ('boat', 'non_motorized', 'canoe'),
    'sailboat': ('boat', 'non_motorized', None),
    'jet ski': ('boat', 'personal_watercraft', 'jet ski'),
    'person': ('person', 'person', None),
}

ROLES_ROWS = [
    ('civilian', 'Civilian', 'Default / no markings'),
    ('law_enforcement', 'Law Enforcement', 'Light bar, markings, push bar, black/white livery'),
    ('fire', 'Fire', 'Red, emergency lights, markings'),
    ('ems', 'EMS', 'Ambulance markings, emergency lights'),
    ('government', 'Government', 'Municipal markings, exempt plates, logos'),
    ('commercial', 'Commercial', 'Company logos, fleet markings, DOT numbers'),
    ('utility', 'Utility', 'Power/water/telecom markings, equipment body'),
    ('construction', 'Construction', 'Company markings, equipment, orange accents'),
    ('rental', 'Rental', 'Rental company decals/plates'),
    ('military', 'Military', 'OD green/tan, military plates, markings'),
]


def main():
    print("Initializing connection pool...")
    init_connection_pool()

    try:
        with get_cursor() as cursor:
            # Seed hierarchy
            print(f"Seeding {len(HIERARCHY_ROWS)} hierarchy rows...")
            for row in HIERARCHY_ROWS:
                cursor.execute("""
                    INSERT INTO classification_hierarchy
                        (tier1, tier2, tier3, yolo_prompt, display_name,
                         enforcement_eligible, disqualification_reason)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (tier1, tier2, tier3) DO NOTHING
                """, row)

            # Seed roles
            print(f"Seeding {len(ROLES_ROWS)} role rows...")
            for name, display, cues in ROLES_ROWS:
                cursor.execute("""
                    INSERT INTO classification_roles (name, display_name, visual_cues)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (name) DO NOTHING
                """, (name, display, cues))

            # Print summary
            cursor.execute("SELECT COUNT(*) AS cnt FROM classification_hierarchy WHERE is_active = TRUE")
            h_count = cursor.fetchone()['cnt']
            cursor.execute("SELECT COUNT(*) AS cnt FROM classification_roles WHERE is_active = TRUE")
            r_count = cursor.fetchone()['cnt']
            cursor.execute("""
                SELECT tier1, COUNT(*) AS cnt FROM classification_hierarchy
                WHERE is_active = TRUE GROUP BY tier1 ORDER BY tier1
            """)
            tier1_counts = cursor.fetchall()

            print(f"\nSeeded {h_count} hierarchy entries, {r_count} roles")
            for row in tier1_counts:
                print(f"  {row['tier1']}: {row['cnt']} entries")

            cursor.execute("SELECT COUNT(*) AS cnt FROM classification_hierarchy WHERE enforcement_eligible = TRUE")
            print(f"  Enforcement-eligible: {cursor.fetchone()['cnt']}")

    finally:
        close_connection_pool()
        print("Done.")


if __name__ == '__main__':
    main()
