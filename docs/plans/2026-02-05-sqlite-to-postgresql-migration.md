# SQLite to PostgreSQL Migration Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Migrate GroundTruth Studio from SQLite to PostgreSQL, replacing all `.db` file references with a shared PostgreSQL connection module.

**Architecture:** Create a centralized `db_connection.py` module that provides connection pooling and a unified interface. Update all 6 files with direct SQLite usage to use this shared module. Migrate schema and data from SQLite to PostgreSQL.

**Tech Stack:** PostgreSQL 14+, psycopg2-binary, SQLAlchemy (optional for connection pooling)

---

## Scope Analysis

### Files with Direct SQLite Usage (Must Update)

| File | SQLite References | Tables Managed |
|------|-------------------|----------------|
| `app/database.py` | 58 `get_connection()` calls, `sqlite3` import | 17 tables (videos, tags, behaviors, annotations, etc.) |
| `app/camera_topology.py` | Own `get_connection()`, `sqlite3` import | Queries only (no CREATE TABLE) |
| `app/sync_config.py` | Own `sqlite3.connect()`, 8 cursor.execute | sync_config, sync_history |
| `app/ecoeye_sync.py` | Own `sqlite3.connect()`, 10 cursor.execute | ecoeye_alerts |
| `app/api.py` | 11 `db.get_connection()` calls | Uses VideoDatabase |
| `app/training_queue.py` | 9 `db.get_connection()` calls | training_jobs |
| `app/yolo_exporter.py` | 6 `db.get_connection()` calls | Uses VideoDatabase |
| `app/vibration_exporter.py` | 2 `db.get_connection()` calls | Uses VideoDatabase |

### Files that Import VideoDatabase (Indirect - Just Update Import)

| File | Usage |
|------|-------|
| `batch_download_example.py` | `VideoDatabase('video_archive.db')` |
| `download_cli.py` | `VideoDatabase('video_archive.db')` |
| `test_installation.py` | `VideoDatabase(':memory:')` |
| `seed_taxonomy.py` | `VideoDatabase()` |
| `app/download_queue.py` | Receives `db: VideoDatabase` |

### Database Files to Remove

- `/opt/groundtruth-studio/video_archive.db` (303KB - main data)
- `/opt/groundtruth-studio/groundtruth.db` (0 bytes - unused)

---

## Task 1: Create PostgreSQL Database and User

**Files:**
- Create: `scripts/setup_postgres.sh`

**Step 1: Create setup script**

```bash
#!/bin/bash
# scripts/setup_postgres.sh
# Run as: sudo -u postgres bash scripts/setup_postgres.sh

set -e

DB_NAME="groundtruth_studio"
DB_USER="groundtruth"
DB_PASS="$(openssl rand -base64 32)"

echo "Creating PostgreSQL database and user..."

psql <<EOF
CREATE USER $DB_USER WITH PASSWORD '$DB_PASS';
CREATE DATABASE $DB_NAME OWNER $DB_USER;
GRANT ALL PRIVILEGES ON DATABASE $DB_NAME TO $DB_USER;
\c $DB_NAME
GRANT ALL ON SCHEMA public TO $DB_USER;
EOF

echo ""
echo "=== PostgreSQL Setup Complete ==="
echo "Database: $DB_NAME"
echo "User: $DB_USER"
echo "Password: $DB_PASS"
echo ""
echo "Add to environment:"
echo "export DATABASE_URL=\"postgresql://$DB_USER:$DB_PASS@localhost:5432/$DB_NAME\""
```

**Step 2: Run the setup**

```bash
sudo -u postgres bash scripts/setup_postgres.sh
```

**Step 3: Add DATABASE_URL to systemd service**

Edit `/opt/groundtruth-studio/groundtruth-studio.service`:
```ini
[Service]
Environment="DATABASE_URL=postgresql://groundtruth:PASSWORD@localhost:5432/groundtruth_studio"
```

**Step 4: Commit**

```bash
git add scripts/setup_postgres.sh
git commit -m "feat: add PostgreSQL setup script"
```

---

## Task 2: Create Shared Database Connection Module

**Files:**
- Create: `app/db_connection.py`

**Step 1: Create the connection module**

```python
"""
Shared PostgreSQL Database Connection Module

Provides centralized connection management for all database operations.
Replaces all direct sqlite3 usage throughout the application.
"""

import os
import logging
from contextlib import contextmanager
from typing import Optional, List, Dict, Any
from urllib.parse import urlparse

import psycopg2
from psycopg2 import pool, extras

logger = logging.getLogger(__name__)

# Global connection pool
_connection_pool: Optional[pool.ThreadedConnectionPool] = None


def get_database_url() -> str:
    """Get database URL from environment"""
    url = os.environ.get('DATABASE_URL')
    if not url:
        raise RuntimeError(
            "DATABASE_URL environment variable not set. "
            "Expected format: postgresql://user:pass@host:port/dbname"
        )
    return url


def init_connection_pool(min_conn: int = 2, max_conn: int = 10) -> None:
    """Initialize the connection pool. Call once at app startup."""
    global _connection_pool

    if _connection_pool is not None:
        return  # Already initialized

    url = get_database_url()
    parsed = urlparse(url)

    _connection_pool = pool.ThreadedConnectionPool(
        min_conn,
        max_conn,
        host=parsed.hostname,
        port=parsed.port or 5432,
        database=parsed.path[1:],  # Remove leading /
        user=parsed.username,
        password=parsed.password,
    )
    logger.info(f"Database connection pool initialized (min={min_conn}, max={max_conn})")


def close_connection_pool() -> None:
    """Close all connections in the pool. Call at app shutdown."""
    global _connection_pool
    if _connection_pool:
        _connection_pool.closeall()
        _connection_pool = None
        logger.info("Database connection pool closed")


@contextmanager
def get_connection():
    """
    Get a database connection from the pool.

    Usage:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM videos")
            rows = cursor.fetchall()

    Connection is automatically returned to pool when context exits.
    """
    global _connection_pool

    if _connection_pool is None:
        init_connection_pool()

    conn = _connection_pool.getconn()
    try:
        yield conn
    finally:
        _connection_pool.putconn(conn)


@contextmanager
def get_cursor(commit: bool = True):
    """
    Get a cursor with automatic commit/rollback and connection management.

    Usage:
        with get_cursor() as cursor:
            cursor.execute("INSERT INTO videos ...")
            # Auto-commits on success, rolls back on exception

    Args:
        commit: If True, commit transaction on success. Default True.
    """
    with get_connection() as conn:
        cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
        try:
            yield cursor
            if commit:
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()


def execute_query(query: str, params: tuple = None, fetch: str = 'all') -> Any:
    """
    Execute a query and return results.

    Args:
        query: SQL query string (use %s for parameters)
        params: Query parameters as tuple
        fetch: 'all', 'one', 'none' (for INSERT/UPDATE/DELETE)

    Returns:
        List of dicts (fetch='all'), single dict (fetch='one'), or None
    """
    with get_cursor(commit=(fetch == 'none')) as cursor:
        cursor.execute(query, params)

        if fetch == 'all':
            return cursor.fetchall()
        elif fetch == 'one':
            return cursor.fetchone()
        elif fetch == 'none':
            return None
        else:
            raise ValueError(f"Invalid fetch mode: {fetch}")


def execute_returning(query: str, params: tuple = None, returning_col: str = 'id') -> Any:
    """
    Execute INSERT/UPDATE with RETURNING clause.

    Args:
        query: SQL query with RETURNING clause
        params: Query parameters
        returning_col: Column name to return (default 'id')

    Returns:
        Value of the returning column
    """
    with get_cursor() as cursor:
        cursor.execute(query, params)
        result = cursor.fetchone()
        return result[returning_col] if result else None
```

**Step 2: Verify module loads**

```bash
cd /opt/groundtruth-studio
source venv/bin/activate
python -c "from app.db_connection import get_connection, init_connection_pool; print('Module loads OK')"
```

**Step 3: Commit**

```bash
git add app/db_connection.py
git commit -m "feat: add shared PostgreSQL connection module

- Connection pooling with ThreadedConnectionPool
- Context managers for safe connection/cursor handling
- Helper functions for common query patterns
- Reads DATABASE_URL from environment"
```

---

## Task 3: Create PostgreSQL Schema Migration

**Files:**
- Create: `app/schema.py`

**Step 1: Create schema module with all table definitions**

```python
"""
PostgreSQL Schema Definition

Defines all tables for GroundTruth Studio.
Run init_schema() once to create all tables.
"""

from app.db_connection import get_cursor
import logging

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
-- Videos table
CREATE TABLE IF NOT EXISTS videos (
    id SERIAL PRIMARY KEY,
    filename TEXT NOT NULL UNIQUE,
    original_url TEXT,
    title TEXT,
    duration REAL,
    width INTEGER,
    height INTEGER,
    file_size BIGINT,
    thumbnail_path TEXT,
    upload_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    notes TEXT
);

-- Tags table
CREATE TABLE IF NOT EXISTS tags (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    category TEXT,
    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Video-Tag associations
CREATE TABLE IF NOT EXISTS video_tags (
    video_id INTEGER REFERENCES videos(id) ON DELETE CASCADE,
    tag_id INTEGER REFERENCES tags(id) ON DELETE CASCADE,
    added_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (video_id, tag_id)
);

-- Legacy behaviors table
CREATE TABLE IF NOT EXISTS behaviors (
    id SERIAL PRIMARY KEY,
    video_id INTEGER REFERENCES videos(id) ON DELETE CASCADE,
    behavior_type TEXT NOT NULL,
    start_time REAL,
    end_time REAL,
    confidence REAL,
    notes TEXT,
    annotated_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Time range tags
CREATE TABLE IF NOT EXISTS time_range_tags (
    id SERIAL PRIMARY KEY,
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    tag_name TEXT NOT NULL,
    start_time REAL NOT NULL,
    end_time REAL,
    is_negative BOOLEAN DEFAULT FALSE,
    comment TEXT,
    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Keyframe annotations with bounding boxes
CREATE TABLE IF NOT EXISTS keyframe_annotations (
    id SERIAL PRIMARY KEY,
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    timestamp REAL NOT NULL,
    bbox_x INTEGER NOT NULL,
    bbox_y INTEGER NOT NULL,
    bbox_width INTEGER NOT NULL,
    bbox_height INTEGER NOT NULL,
    activity_tag TEXT,
    moment_tag TEXT,
    is_negative BOOLEAN DEFAULT FALSE,
    comment TEXT,
    reviewed BOOLEAN DEFAULT TRUE,
    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Tag groups for structured annotation
CREATE TABLE IF NOT EXISTS tag_groups (
    id SERIAL PRIMARY KEY,
    group_name TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    group_type TEXT NOT NULL,
    description TEXT,
    is_required BOOLEAN DEFAULT FALSE,
    applies_to TEXT,
    sort_order INTEGER DEFAULT 0,
    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Tag options within groups
CREATE TABLE IF NOT EXISTS tag_options (
    id SERIAL PRIMARY KEY,
    group_id INTEGER NOT NULL REFERENCES tag_groups(id) ON DELETE CASCADE,
    option_value TEXT NOT NULL,
    display_text TEXT NOT NULL,
    is_negative BOOLEAN DEFAULT FALSE,
    description TEXT,
    sort_order INTEGER DEFAULT 0,
    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Annotation tags (many-to-many)
CREATE TABLE IF NOT EXISTS annotation_tags (
    id SERIAL PRIMARY KEY,
    annotation_id INTEGER NOT NULL,
    annotation_type TEXT NOT NULL,
    group_id INTEGER NOT NULL REFERENCES tag_groups(id) ON DELETE CASCADE,
    tag_value TEXT NOT NULL,
    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Tag suggestions for autocomplete
CREATE TABLE IF NOT EXISTS tag_suggestions (
    id SERIAL PRIMARY KEY,
    category TEXT NOT NULL,
    tag_text TEXT NOT NULL,
    is_negative BOOLEAN DEFAULT FALSE,
    description TEXT,
    sort_order INTEGER DEFAULT 0,
    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- YOLO export configurations
CREATE TABLE IF NOT EXISTS yolo_export_configs (
    id SERIAL PRIMARY KEY,
    config_name TEXT NOT NULL UNIQUE,
    description TEXT,
    class_mapping TEXT NOT NULL,
    include_reviewed_only BOOLEAN DEFAULT FALSE,
    include_ai_generated BOOLEAN DEFAULT TRUE,
    include_negative_examples BOOLEAN DEFAULT TRUE,
    min_confidence REAL DEFAULT 0.0,
    export_format TEXT DEFAULT 'yolov8',
    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_export_date TIMESTAMP,
    last_export_count INTEGER DEFAULT 0
);

-- YOLO export video selections
CREATE TABLE IF NOT EXISTS yolo_export_videos (
    id SERIAL PRIMARY KEY,
    export_config_id INTEGER NOT NULL REFERENCES yolo_export_configs(id) ON DELETE CASCADE,
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    included BOOLEAN DEFAULT TRUE,
    notes TEXT,
    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(export_config_id, video_id)
);

-- YOLO export filters
CREATE TABLE IF NOT EXISTS yolo_export_filters (
    id SERIAL PRIMARY KEY,
    export_config_id INTEGER NOT NULL REFERENCES yolo_export_configs(id) ON DELETE CASCADE,
    filter_type TEXT NOT NULL,
    filter_value TEXT NOT NULL,
    is_exclusion BOOLEAN DEFAULT FALSE,
    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- YOLO export logs
CREATE TABLE IF NOT EXISTS yolo_export_logs (
    id SERIAL PRIMARY KEY,
    export_config_id INTEGER NOT NULL REFERENCES yolo_export_configs(id) ON DELETE CASCADE,
    export_path TEXT NOT NULL,
    video_count INTEGER NOT NULL,
    annotation_count INTEGER NOT NULL,
    export_format TEXT NOT NULL,
    export_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    notes TEXT
);

-- Fleet vehicles
CREATE TABLE IF NOT EXISTS fleet_vehicles (
    id SERIAL PRIMARY KEY,
    fleet_id TEXT NOT NULL UNIQUE,
    fleet_type TEXT,
    vehicle_type TEXT,
    vehicle_make TEXT,
    vehicle_model TEXT,
    primary_color TEXT,
    secondary_color TEXT,
    agency_name TEXT,
    plate_number TEXT,
    plate_state TEXT,
    first_seen_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    total_detections INTEGER DEFAULT 0,
    notes TEXT
);

-- Vehicle-Person links
CREATE TABLE IF NOT EXISTS vehicle_person_links (
    id SERIAL PRIMARY KEY,
    vehicle_fleet_id TEXT NOT NULL REFERENCES fleet_vehicles(fleet_id) ON DELETE CASCADE,
    person_name TEXT NOT NULL,
    first_seen_together TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen_together TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    times_seen_together INTEGER DEFAULT 1
);

-- Trailers
CREATE TABLE IF NOT EXISTS trailers (
    id SERIAL PRIMARY KEY,
    trailer_id TEXT NOT NULL UNIQUE,
    trailer_type TEXT,
    trailer_color TEXT,
    plate_number TEXT,
    plate_state TEXT,
    first_seen_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    total_detections INTEGER DEFAULT 0,
    notes TEXT
);

-- Vehicle-Trailer links
CREATE TABLE IF NOT EXISTS vehicle_trailer_links (
    id SERIAL PRIMARY KEY,
    vehicle_fleet_id TEXT NOT NULL REFERENCES fleet_vehicles(fleet_id) ON DELETE CASCADE,
    trailer_id TEXT NOT NULL REFERENCES trailers(trailer_id) ON DELETE CASCADE,
    first_seen_together TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen_together TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    times_seen_together INTEGER DEFAULT 1
);

-- Sync configuration (encrypted credentials)
CREATE TABLE IF NOT EXISTS sync_config (
    id SERIAL PRIMARY KEY,
    config_key TEXT UNIQUE NOT NULL,
    config_value TEXT,
    is_encrypted BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Sync history
CREATE TABLE IF NOT EXISTS sync_history (
    id SERIAL PRIMARY KEY,
    sync_type TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    items_processed INTEGER DEFAULT 0,
    items_succeeded INTEGER DEFAULT 0,
    items_failed INTEGER DEFAULT 0,
    error_message TEXT,
    details TEXT
);

-- EcoEye alerts
CREATE TABLE IF NOT EXISTS ecoeye_alerts (
    id SERIAL PRIMARY KEY,
    alert_id TEXT UNIQUE NOT NULL,
    camera_id TEXT,
    timestamp TIMESTAMP,
    alert_type TEXT,
    confidence REAL,
    video_url TEXT,
    thumbnail_url TEXT,
    video_available BOOLEAN DEFAULT FALSE,
    video_downloaded BOOLEAN DEFAULT FALSE,
    local_video_id INTEGER REFERENCES videos(id),
    metadata JSONB,
    synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Training jobs queue
CREATE TABLE IF NOT EXISTS training_jobs (
    id SERIAL PRIMARY KEY,
    job_name TEXT NOT NULL,
    job_type TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    config JSONB,
    export_path TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    error_message TEXT,
    result JSONB
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_videos_filename ON videos(filename);
CREATE INDEX IF NOT EXISTS idx_videos_upload_date ON videos(upload_date);
CREATE INDEX IF NOT EXISTS idx_tags_name ON tags(name);
CREATE INDEX IF NOT EXISTS idx_behaviors_video ON behaviors(video_id);
CREATE INDEX IF NOT EXISTS idx_time_range_tags_video ON time_range_tags(video_id);
CREATE INDEX IF NOT EXISTS idx_keyframe_annotations_video ON keyframe_annotations(video_id);
CREATE INDEX IF NOT EXISTS idx_keyframe_annotations_timestamp ON keyframe_annotations(timestamp);
CREATE INDEX IF NOT EXISTS idx_tag_suggestions_category ON tag_suggestions(category);
CREATE INDEX IF NOT EXISTS idx_tag_groups_name ON tag_groups(group_name);
CREATE INDEX IF NOT EXISTS idx_tag_options_group ON tag_options(group_id);
CREATE INDEX IF NOT EXISTS idx_annotation_tags_annotation ON annotation_tags(annotation_id, annotation_type);
CREATE INDEX IF NOT EXISTS idx_yolo_export_videos_config ON yolo_export_videos(export_config_id);
CREATE INDEX IF NOT EXISTS idx_fleet_vehicles_fleet_id ON fleet_vehicles(fleet_id);
CREATE INDEX IF NOT EXISTS idx_ecoeye_alerts_alert_id ON ecoeye_alerts(alert_id);
CREATE INDEX IF NOT EXISTS idx_ecoeye_alerts_timestamp ON ecoeye_alerts(timestamp);
CREATE INDEX IF NOT EXISTS idx_training_jobs_status ON training_jobs(status);
"""


def init_schema():
    """Create all tables and indexes"""
    with get_cursor() as cursor:
        cursor.execute(SCHEMA_SQL)
    logger.info("PostgreSQL schema initialized successfully")


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    init_schema()
    print("Schema created successfully")
```

**Step 2: Run schema creation**

```bash
cd /opt/groundtruth-studio
source venv/bin/activate
python -m app.schema
```

**Step 3: Commit**

```bash
git add app/schema.py
git commit -m "feat: add PostgreSQL schema definition

- All 23 tables from SQLite schema
- Proper PostgreSQL types (SERIAL, BOOLEAN, JSONB)
- Foreign key constraints with CASCADE
- Performance indexes"
```

---

## Task 4: Create Data Migration Script

**Files:**
- Create: `scripts/migrate_sqlite_to_postgres.py`

**Step 1: Create migration script**

```python
#!/usr/bin/env python3
"""
Migrate data from SQLite to PostgreSQL

Usage:
    python scripts/migrate_sqlite_to_postgres.py /path/to/video_archive.db
"""

import sys
import sqlite3
from pathlib import Path

# Add app to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db_connection import get_cursor, init_connection_pool
from app.schema import init_schema


def migrate_table(sqlite_conn, table_name: str, column_mapping: dict = None):
    """
    Migrate a single table from SQLite to PostgreSQL

    Args:
        sqlite_conn: SQLite connection
        table_name: Name of table to migrate
        column_mapping: Optional dict to rename/transform columns
    """
    sqlite_cursor = sqlite_conn.cursor()

    # Get all rows from SQLite
    sqlite_cursor.execute(f"SELECT * FROM {table_name}")
    rows = sqlite_cursor.fetchall()

    if not rows:
        print(f"  {table_name}: 0 rows (empty)")
        return 0

    # Get column names
    columns = [desc[0] for desc in sqlite_cursor.description]

    # Apply column mapping if provided
    if column_mapping:
        columns = [column_mapping.get(c, c) for c in columns]

    # Skip 'id' column - let PostgreSQL generate it
    if 'id' in columns:
        id_idx = columns.index('id')
        columns = columns[:id_idx] + columns[id_idx+1:]
        rows = [row[:id_idx] + row[id_idx+1:] for row in rows]

    # Build INSERT query
    placeholders = ', '.join(['%s'] * len(columns))
    column_names = ', '.join(columns)
    insert_sql = f"INSERT INTO {table_name} ({column_names}) VALUES ({placeholders})"

    # Insert into PostgreSQL
    with get_cursor() as pg_cursor:
        for row in rows:
            # Convert SQLite booleans (0/1) to Python booleans
            row = tuple(
                bool(v) if isinstance(v, int) and columns[i].startswith('is_')
                else v
                for i, v in enumerate(row)
            )
            try:
                pg_cursor.execute(insert_sql, row)
            except Exception as e:
                print(f"  Error inserting row into {table_name}: {e}")
                print(f"  Row: {row[:3]}...")
                raise

    print(f"  {table_name}: {len(rows)} rows migrated")
    return len(rows)


def reset_sequences(tables: list):
    """Reset PostgreSQL sequences after data import"""
    with get_cursor() as cursor:
        for table in tables:
            cursor.execute(f"""
                SELECT setval(pg_get_serial_sequence('{table}', 'id'),
                       COALESCE((SELECT MAX(id) FROM {table}), 1))
            """)
    print("Sequences reset")


def main():
    if len(sys.argv) < 2:
        print("Usage: python migrate_sqlite_to_postgres.py /path/to/video_archive.db")
        sys.exit(1)

    sqlite_path = sys.argv[1]
    if not Path(sqlite_path).exists():
        print(f"SQLite database not found: {sqlite_path}")
        sys.exit(1)

    print(f"Migrating from: {sqlite_path}")
    print("Initializing PostgreSQL connection...")
    init_connection_pool()

    print("Creating schema...")
    init_schema()

    print("Connecting to SQLite...")
    sqlite_conn = sqlite3.connect(sqlite_path)
    sqlite_conn.row_factory = sqlite3.Row

    # Tables in dependency order (parents before children)
    tables = [
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

    print("\nMigrating tables...")
    total_rows = 0
    for table in tables:
        try:
            # Check if table exists in SQLite
            sqlite_cursor = sqlite_conn.cursor()
            sqlite_cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,)
            )
            if sqlite_cursor.fetchone():
                total_rows += migrate_table(sqlite_conn, table)
            else:
                print(f"  {table}: skipped (not in SQLite)")
        except Exception as e:
            print(f"  {table}: ERROR - {e}")

    print(f"\nResetting sequences...")
    reset_sequences([t for t in tables if t not in ['video_tags']])

    sqlite_conn.close()

    print(f"\n=== Migration Complete ===")
    print(f"Total rows migrated: {total_rows}")


if __name__ == '__main__':
    main()
```

**Step 2: Run migration**

```bash
cd /opt/groundtruth-studio
source venv/bin/activate
python scripts/migrate_sqlite_to_postgres.py video_archive.db
```

**Step 3: Verify migration**

```bash
psql -U groundtruth -d groundtruth_studio -c "SELECT COUNT(*) FROM videos;"
```

**Step 4: Commit**

```bash
git add scripts/migrate_sqlite_to_postgres.py
git commit -m "feat: add SQLite to PostgreSQL migration script

- Migrates all 22 tables in dependency order
- Handles boolean conversion (0/1 to true/false)
- Resets PostgreSQL sequences after import
- Skips missing tables gracefully"
```

---

## Task 5: Update database.py to Use PostgreSQL

**Files:**
- Modify: `app/database.py`

**Step 1: Replace sqlite3 imports and connection handling**

Replace the entire file with PostgreSQL version. Key changes:
- Replace `sqlite3` with `app.db_connection`
- Replace `?` placeholders with `%s`
- Replace `INTEGER PRIMARY KEY AUTOINCREMENT` with `SERIAL PRIMARY KEY`
- Replace `cursor.lastrowid` with `RETURNING id`
- Replace `sqlite3.IntegrityError` with `psycopg2.IntegrityError`

```python
"""
Video Database - PostgreSQL Version

Provides the VideoDatabase class for video and annotation management.
Uses shared connection pool from db_connection module.
"""

import psycopg2
from psycopg2 import extras
from datetime import datetime
from typing import List, Dict, Optional

from app.db_connection import get_connection, get_cursor, execute_query, execute_returning
from app.schema import init_schema


class VideoDatabase:
    """
    Database interface for video archive operations.

    Note: db_path parameter is kept for backwards compatibility but ignored.
    Connection is handled by the shared db_connection module.
    """

    def __init__(self, db_path: str = None):
        """
        Initialize database connection.

        Args:
            db_path: Ignored (kept for backwards compatibility)
        """
        # Ensure schema exists
        init_schema()

    def get_connection(self):
        """
        Get database connection context manager.

        Returns a context manager that yields a connection.
        For backwards compatibility with code that calls db.get_connection().
        """
        return get_connection()

    # ... [Rest of methods with SQL updated for PostgreSQL]
```

**Step 2: Update all SQL queries**

Key patterns to change throughout the file:

| SQLite | PostgreSQL |
|--------|------------|
| `?` | `%s` |
| `cursor.lastrowid` | `RETURNING id` clause |
| `sqlite3.IntegrityError` | `psycopg2.IntegrityError` |
| `conn.close()` | (handled by context manager) |

Example method transformation:

```python
# BEFORE (SQLite)
def add_video(self, filename: str, ...) -> int:
    conn = self.get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO videos (...) VALUES (?, ?, ?, ...)
    ''', (filename, ...))
    video_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return video_id

# AFTER (PostgreSQL)
def add_video(self, filename: str, ...) -> int:
    with get_cursor() as cursor:
        cursor.execute('''
            INSERT INTO videos (...) VALUES (%s, %s, %s, ...)
            RETURNING id
        ''', (filename, ...))
        result = cursor.fetchone()
        return result['id']
```

**Step 3: Test the updated module**

```bash
cd /opt/groundtruth-studio
source venv/bin/activate
python -c "from app.database import VideoDatabase; db = VideoDatabase(); print('VideoDatabase OK')"
```

**Step 4: Commit**

```bash
git add app/database.py
git commit -m "refactor: migrate database.py from SQLite to PostgreSQL

- Use shared db_connection module
- Update all SQL placeholders (? -> %s)
- Use RETURNING clause instead of lastrowid
- Use context managers for connection handling
- Keep backwards-compatible constructor signature"
```

---

## Task 6: Update camera_topology.py

**Files:**
- Modify: `app/camera_topology.py`

**Step 1: Replace imports and connection handling**

```python
# BEFORE
import sqlite3
...
def get_connection(self):
    conn = sqlite3.connect(self.db_path)
    conn.row_factory = sqlite3.Row
    return conn

# AFTER
from app.db_connection import get_connection, get_cursor
...
def get_connection(self):
    """Backwards-compatible connection getter"""
    return get_connection()
```

**Step 2: Update SQL queries to use %s placeholders**

**Step 3: Remove db_path from constructor (or ignore it)**

**Step 4: Test**

```bash
python -c "from app.camera_topology import CameraTopologyLearner; print('OK')"
```

**Step 5: Commit**

```bash
git add app/camera_topology.py
git commit -m "refactor: migrate camera_topology.py to PostgreSQL"
```

---

## Task 7: Update sync_config.py

**Files:**
- Modify: `app/sync_config.py`

**Step 1: Replace sqlite3 imports with db_connection**

**Step 2: Remove _init_config_tables (schema handled by schema.py)**

**Step 3: Update all cursor.execute with %s placeholders**

**Step 4: Commit**

```bash
git add app/sync_config.py
git commit -m "refactor: migrate sync_config.py to PostgreSQL"
```

---

## Task 8: Update ecoeye_sync.py

**Files:**
- Modify: `app/ecoeye_sync.py`

**Step 1: Replace sqlite3 imports**

**Step 2: Update SQL placeholders**

**Step 3: Remove table creation (handled by schema.py)**

**Step 4: Commit**

```bash
git add app/ecoeye_sync.py
git commit -m "refactor: migrate ecoeye_sync.py to PostgreSQL"
```

---

## Task 9: Update api.py

**Files:**
- Modify: `app/api.py`

**Step 1: Remove DB_PATH constant**

```python
# REMOVE THIS LINE
DB_PATH = BASE_DIR / 'video_archive.db'
```

**Step 2: Update VideoDatabase instantiation**

```python
# BEFORE
db = VideoDatabase(str(DB_PATH))

# AFTER
db = VideoDatabase()  # Uses DATABASE_URL from environment
```

**Step 3: Update direct get_connection() calls**

All 11 places where `conn = db.get_connection()` is used need to change to:

```python
# BEFORE
conn = db.get_connection()
cursor = conn.cursor()
cursor.execute('SELECT ...', (param,))
rows = cursor.fetchall()
conn.close()

# AFTER
with db.get_connection() as conn:
    cursor = conn.cursor(cursor_factory=extras.RealDictCursor)
    cursor.execute('SELECT ...', (param,))
    rows = cursor.fetchall()
```

**Step 4: Add connection pool initialization at startup**

```python
from app.db_connection import init_connection_pool, close_connection_pool
import atexit

# Initialize at startup
init_connection_pool()

# Close at shutdown
atexit.register(close_connection_pool)
```

**Step 5: Commit**

```bash
git add app/api.py
git commit -m "refactor: migrate api.py to PostgreSQL

- Remove hardcoded DB_PATH
- Initialize connection pool at startup
- Update all direct connection usage"
```

---

## Task 10: Update Remaining Files

**Files:**
- Modify: `app/training_queue.py`
- Modify: `app/yolo_exporter.py`
- Modify: `app/vibration_exporter.py`
- Modify: `batch_download_example.py`
- Modify: `download_cli.py`
- Modify: `seed_taxonomy.py`

**Step 1: Update each file's VideoDatabase instantiation**

Remove path arguments:

```python
# BEFORE
db = VideoDatabase(str(base_dir / 'video_archive.db'))

# AFTER
db = VideoDatabase()
```

**Step 2: Update test_installation.py for PostgreSQL**

```python
# BEFORE
db = VideoDatabase(':memory:')

# AFTER
# Skip DB test or use test database
import os
os.environ.setdefault('DATABASE_URL', 'postgresql://test:test@localhost/test_groundtruth')
db = VideoDatabase()
```

**Step 3: Commit all**

```bash
git add app/training_queue.py app/yolo_exporter.py app/vibration_exporter.py \
        batch_download_example.py download_cli.py seed_taxonomy.py test_installation.py
git commit -m "refactor: update remaining files for PostgreSQL

- Remove hardcoded .db paths from all files
- Update test_installation.py for PostgreSQL"
```

---

## Task 11: Update requirements.txt

**Files:**
- Modify: `requirements.txt`

**Step 1: Add PostgreSQL dependencies**

```
# Add these lines
psycopg2-binary>=2.9.9
```

**Step 2: Install**

```bash
cd /opt/groundtruth-studio
source venv/bin/activate
pip install psycopg2-binary
pip freeze > requirements.txt
```

**Step 3: Commit**

```bash
git add requirements.txt
git commit -m "deps: add psycopg2-binary for PostgreSQL support"
```

---

## Task 12: Update systemd Service

**Files:**
- Modify: `groundtruth-studio.service`

**Step 1: Add DATABASE_URL environment variable**

```ini
[Unit]
Description=GroundTruth Studio
After=network.target postgresql.service

[Service]
Type=simple
User=brandon
WorkingDirectory=/opt/groundtruth-studio
Environment="DATABASE_URL=postgresql://groundtruth:PASSWORD@localhost:5432/groundtruth_studio"
ExecStart=/opt/groundtruth-studio/venv/bin/python app/api.py
Restart=always

[Install]
WantedBy=multi-user.target
```

**Step 2: Reload and restart service**

```bash
sudo cp groundtruth-studio.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart groundtruth-studio
sudo systemctl status groundtruth-studio
```

**Step 3: Commit**

```bash
git add groundtruth-studio.service
git commit -m "config: add DATABASE_URL to systemd service"
```

---

## Task 13: Remove SQLite Files and Update .gitignore

**Files:**
- Delete: `video_archive.db`
- Delete: `groundtruth.db`
- Modify: `.gitignore`

**Step 1: Backup SQLite databases**

```bash
cp video_archive.db video_archive.db.backup-$(date +%Y%m%d)
```

**Step 2: Remove from git tracking**

```bash
git rm --cached video_archive.db groundtruth.db 2>/dev/null || true
```

**Step 3: Update .gitignore**

```
# Database files
*.db
*.db-journal
*.db-shm
*.db-wal

# Keep backup for reference
!*.db.backup-*
```

**Step 4: Commit**

```bash
git add .gitignore
git commit -m "chore: remove SQLite files, update .gitignore

SQLite has been replaced with PostgreSQL.
Backup files preserved locally."
```

---

## Task 14: Update AGENTS.md Documentation

**Files:**
- Modify: `AGENTS.md`

**Step 1: Update database section**

Replace SQLite references with PostgreSQL:

```markdown
## Quick Reference

| Item | Value |
|------|-------|
| **Database** | PostgreSQL (`groundtruth_studio`) |
| **DB Connection** | `DATABASE_URL` environment variable |

## Database

- **Type**: PostgreSQL 14+
- **Database**: `groundtruth_studio`
- **User**: `groundtruth`
- **Connection**: Via `app/db_connection.py` module
- **Schema**: Defined in `app/schema.py`
```

**Step 2: Commit**

```bash
git add AGENTS.md
git commit -m "docs: update AGENTS.md for PostgreSQL migration"
```

---

## Task 15: Final Verification

**Step 1: Run full test**

```bash
cd /opt/groundtruth-studio
source venv/bin/activate

# Test imports
python -c "
from app.db_connection import init_connection_pool
from app.database import VideoDatabase
from app.camera_topology import CameraTopologyLearner
from app.sync_config import SyncConfigManager
from app.ecoeye_sync import EcoEyeSyncClient
print('All imports OK')
"

# Test database operations
python -c "
from app.database import VideoDatabase
db = VideoDatabase()
videos = db.get_all_videos(limit=5)
print(f'Found {len(videos)} videos')
"
```

**Step 2: Restart service and verify**

```bash
sudo systemctl restart groundtruth-studio
curl -s https://studio.ecoeyetech.com/api/videos | head -c 200
```

**Step 3: Final commit**

```bash
git add -A
git commit -m "feat: complete SQLite to PostgreSQL migration

Migration complete:
- All 6 files with direct SQLite usage updated
- Shared db_connection.py module for connection pooling
- schema.py defines all 23 tables
- Data migration script included
- systemd service configured with DATABASE_URL
- Documentation updated

Breaking changes:
- DATABASE_URL environment variable now required
- SQLite databases no longer used"
```

---

## Summary

| Phase | Tasks | Files Changed |
|-------|-------|---------------|
| Setup | 1-2 | 2 new files |
| Schema | 3-4 | 2 new files |
| Core Migration | 5-9 | 5 files |
| Cleanup | 10-14 | 8+ files |
| Verification | 15 | - |

**Total files to modify:** 14
**Total new files:** 4
**Estimated commits:** 15
