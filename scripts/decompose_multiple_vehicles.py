#!/usr/bin/env python3
"""
Process existing "multiple_vehicles" predictions through VLM decomposition.

Usage:
    cd /opt/groundtruth-studio/app && python ../scripts/decompose_multiple_vehicles.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

from db_connection import init_connection_pool, get_cursor, close_connection_pool
from entity_decomposer import EntityDecomposer


def main():
    print("Initializing connection pool...")
    init_connection_pool()
    decomposer = EntityDecomposer()

    try:
        summary = decomposer.decompose_batch(limit=100)
        print(f"\nDecomposition complete:")
        print(f"  Queued:           {summary['queued']}")
        print(f"  Children created: {summary['children_created']}")
        print(f"  Errors:           {summary['errors']}")
    finally:
        close_connection_pool()


if __name__ == '__main__':
    main()
