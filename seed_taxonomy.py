#!/usr/bin/env python3
"""
Seed script to populate database with comprehensive tag taxonomy
Run this once to set up the 29-group multi-type tagging system
"""

import sys
import os

# Add app directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'app'))

from database import VideoDatabase

def main():
    print("Seeding comprehensive tag taxonomy...")
    db = VideoDatabase()

    try:
        db.seed_comprehensive_tag_taxonomy()
        print("✓ Successfully seeded 29 tag groups with all options")
        print("\nTag groups created:")
        groups = db.get_tag_groups()
        for group in groups:
            print(f"  - {group['display_name']} ({group['group_type']})")
        print(f"\nTotal: {len(groups)} tag groups")
    except Exception as e:
        print(f"✗ Error seeding taxonomy: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == '__main__':
    main()
