#!/usr/bin/env python3
"""
SQLite to PostgreSQL Data Migration Script

Migrates all data from SQLite database to PostgreSQL, handling:
- Boolean conversion (SQLite 0/1 to Python bool)
- Auto-incrementing ID columns (skips 'id', lets PostgreSQL generate)
- Sequence reset after import
- Dependency order (parent tables before children)
- Graceful handling of missing tables

Usage:
    python3 scripts/migrate_sqlite_to_postgres.py /path/to/video_archive.db
"""

import sys
import sqlite3
import logging
from typing import List, Dict, Any, Optional

# Import PostgreSQL connection from app
from db_connection import get_cursor, get_connection

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# Tables to migrate in dependency order (parents before children)
MIGRATION_ORDER = [
    'videos',
    'tags',
    'video_tags',
    'behaviors',
    'time_range_tags',
    'keyframe_annotations',
    'tag_groups',
    'tag_options',
    'annotation_tags',
    'tag_suggestions',
    'yolo_export_configs',
    'yolo_export_videos',
    'yolo_export_filters',
    'yolo_export_logs',
    'fleet_vehicles',
    'vehicle_person_links',
    'trailers',
    'vehicle_trailer_links',
    'sync_config',
    'sync_history',
    'ecoeye_alerts',
    'training_jobs',
]


def table_exists_in_sqlite(conn: sqlite3.Connection, table_name: str) -> bool:
    """Check if table exists in SQLite database."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,)
    )
    return cursor.fetchone() is not None


def get_table_columns(conn: sqlite3.Connection, table_name: str) -> List[str]:
    """Get list of columns for a table, excluding 'id' column."""
    cursor = conn.cursor()
    cursor.execute(f"PRAGMA table_info({table_name})")
    columns = [row[1] for row in cursor.fetchall() if row[1] != 'id']
    return columns


def convert_sqlite_row_to_postgres(row: Dict[str, Any], columns: List[str]) -> tuple:
    """
    Convert SQLite row values to PostgreSQL-compatible values.

    Handles:
    - Boolean conversion: SQLite 0/1 -> Python True/False
    - NULL preservation
    """
    values = []
    for col in columns:
        value = row.get(col)

        # Convert SQLite boolean integers to Python booleans
        # Common boolean column patterns
        if col in ('is_negative', 'is_required', 'is_encrypted', 'included',
                   'is_exclusion', 'reviewed'):
            if value is not None:
                value = bool(value)

        values.append(value)

    return tuple(values)


def migrate_table(sqlite_conn: sqlite3.Connection, table_name: str) -> int:
    """
    Migrate a single table from SQLite to PostgreSQL.

    Returns: Number of rows migrated
    """
    if not table_exists_in_sqlite(sqlite_conn, table_name):
        logger.warning(f"Table '{table_name}' does not exist in SQLite database, skipping")
        return 0

    logger.info(f"Migrating table: {table_name}")

    # Get columns (excluding 'id')
    columns = get_table_columns(sqlite_conn, table_name)
    if not columns:
        logger.warning(f"No columns found for table '{table_name}', skipping")
        return 0

    # Fetch all rows from SQLite
    sqlite_conn.row_factory = sqlite3.Row
    sqlite_cursor = sqlite_conn.cursor()
    sqlite_cursor.execute(f"SELECT * FROM {table_name}")
    rows = sqlite_cursor.fetchall()

    if not rows:
        logger.info(f"Table '{table_name}' is empty, skipping")
        return 0

    # Insert into PostgreSQL
    placeholders = ', '.join(['%s'] * len(columns))
    column_names = ', '.join(columns)
    insert_sql = f"INSERT INTO {table_name} ({column_names}) VALUES ({placeholders})"

    migrated_count = 0
    with get_cursor() as cursor:
        for row in rows:
            row_dict = dict(row)
            values = convert_sqlite_row_to_postgres(row_dict, columns)

            try:
                cursor.execute(insert_sql, values)
                migrated_count += 1
            except Exception as e:
                logger.error(f"Error inserting row into {table_name}: {e}")
                logger.error(f"Row data: {row_dict}")
                raise

    logger.info(f"Successfully migrated {migrated_count} rows to {table_name}")
    return migrated_count


def reset_sequences(table_name: str) -> None:
    """
    Reset PostgreSQL sequence for a table's id column.

    This ensures that new inserts will use the correct next ID value.
    """
    logger.info(f"Resetting sequence for {table_name}")

    with get_cursor() as cursor:
        # Get the maximum ID value
        cursor.execute(f"SELECT MAX(id) FROM {table_name}")
        result = cursor.fetchone()
        max_id = result['max'] if result and result['max'] else 0

        # Reset the sequence
        sequence_name = f"{table_name}_id_seq"
        cursor.execute(f"SELECT setval('{sequence_name}', {max_id}, true)")

    logger.info(f"Sequence {sequence_name} reset to {max_id}")


def migrate_database(sqlite_db_path: str) -> None:
    """
    Main migration function.

    Connects to both SQLite and PostgreSQL, migrates all tables in order,
    and resets sequences.
    """
    logger.info(f"Starting migration from SQLite: {sqlite_db_path}")
    logger.info(f"Target: PostgreSQL (via DATABASE_URL)")

    # Connect to SQLite
    try:
        sqlite_conn = sqlite3.connect(sqlite_db_path)
        logger.info("Connected to SQLite database")
    except Exception as e:
        logger.error(f"Failed to connect to SQLite database: {e}")
        sys.exit(1)

    # Test PostgreSQL connection
    try:
        with get_connection() as pg_conn:
            logger.info("Connected to PostgreSQL database")
    except Exception as e:
        logger.error(f"Failed to connect to PostgreSQL database: {e}")
        logger.error("Make sure DATABASE_URL environment variable is set")
        sys.exit(1)

    # Migrate tables in order
    total_rows = 0
    for table_name in MIGRATION_ORDER:
        try:
            rows_migrated = migrate_table(sqlite_conn, table_name)
            total_rows += rows_migrated

            # Reset sequence if rows were migrated
            if rows_migrated > 0:
                try:
                    reset_sequences(table_name)
                except Exception as e:
                    logger.warning(f"Could not reset sequence for {table_name}: {e}")

        except Exception as e:
            logger.error(f"Failed to migrate table '{table_name}': {e}")
            sqlite_conn.close()
            sys.exit(1)

    # Close SQLite connection
    sqlite_conn.close()

    logger.info("=" * 60)
    logger.info("Migration completed successfully!")
    logger.info(f"Total rows migrated: {total_rows}")
    logger.info("=" * 60)


def main():
    """Entry point for migration script."""
    if len(sys.argv) != 2:
        print("Usage: python3 scripts/migrate_sqlite_to_postgres.py /path/to/video_archive.db")
        sys.exit(1)

    sqlite_db_path = sys.argv[1]

    # Confirm before proceeding
    print(f"This will migrate data from:")
    print(f"  SQLite: {sqlite_db_path}")
    print(f"  To PostgreSQL (via DATABASE_URL)")
    print()
    response = input("Continue? (yes/no): ")

    if response.lower() not in ('yes', 'y'):
        print("Migration cancelled")
        sys.exit(0)

    migrate_database(sqlite_db_path)


if __name__ == '__main__':
    main()
