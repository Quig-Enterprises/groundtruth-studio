#!/usr/bin/env python3
"""
Retroactive audit of all existing visits for classification consistency.

Usage:
    cd /opt/groundtruth-studio/app && python ../scripts/audit_visit_consistency.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

from db_connection import init_connection_pool, get_cursor, close_connection_pool
from visit_consistency import VisitConsistencyChecker


def main():
    print("Initializing connection pool...")
    init_connection_pool()
    checker = VisitConsistencyChecker()

    try:
        summary = checker.run_retroactive_audit(limit=5000)
        print(f"\nAudit complete:")
        print(f"  Visits checked: {summary['visits_checked']}")
        print(f"  Flags created:  {summary['flags_created']}")

        # Show top flag types
        flags = checker.get_flags(resolved=False, limit=20)
        if flags:
            print(f"\nTop unresolved flags:")
            for f in flags[:10]:
                print(f"  Visit {str(f['visit_id'])[:8]}... "
                      f"{f['camera_a']} ({f['class_a']}) vs "
                      f"{f['camera_b']} ({f['class_b']}) - {f['flag_type']}")
    finally:
        close_connection_pool()


if __name__ == '__main__':
    main()
