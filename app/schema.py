"""
PostgreSQL Database Schema for GroundTruth Studio

This module defines all database tables and provides initialization functions.
Replaces SQLite schema with PostgreSQL-native types and features.
"""

from db_connection import get_cursor
import logging

logger = logging.getLogger(__name__)

# Complete PostgreSQL schema for all tables
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
    file_size INTEGER,
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

-- Video tags junction table
CREATE TABLE IF NOT EXISTS video_tags (
    video_id INTEGER,
    tag_id INTEGER,
    added_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (video_id, tag_id),
    FOREIGN KEY (video_id) REFERENCES videos(id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
);

-- Behaviors table
CREATE TABLE IF NOT EXISTS behaviors (
    id SERIAL PRIMARY KEY,
    video_id INTEGER,
    behavior_type TEXT NOT NULL,
    start_time REAL,
    end_time REAL,
    confidence REAL,
    notes TEXT,
    annotated_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (video_id) REFERENCES videos(id) ON DELETE CASCADE
);

-- Time range tags table
CREATE TABLE IF NOT EXISTS time_range_tags (
    id SERIAL PRIMARY KEY,
    video_id INTEGER NOT NULL,
    tag_name TEXT NOT NULL,
    start_time REAL NOT NULL,
    end_time REAL,
    is_negative BOOLEAN DEFAULT FALSE,
    comment TEXT,
    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (video_id) REFERENCES videos(id) ON DELETE CASCADE
);

-- Keyframe annotations table
CREATE TABLE IF NOT EXISTS keyframe_annotations (
    id SERIAL PRIMARY KEY,
    video_id INTEGER NOT NULL,
    timestamp REAL NOT NULL,
    bbox_x INTEGER NOT NULL,
    bbox_y INTEGER NOT NULL,
    bbox_width INTEGER NOT NULL,
    bbox_height INTEGER NOT NULL,
    activity_tag TEXT,
    moment_tag TEXT,
    is_negative BOOLEAN DEFAULT FALSE,
    comment TEXT,
    reviewed BOOLEAN DEFAULT FALSE,
    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (video_id) REFERENCES videos(id) ON DELETE CASCADE
);

-- Tag groups table
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

-- Tag options table
CREATE TABLE IF NOT EXISTS tag_options (
    id SERIAL PRIMARY KEY,
    group_id INTEGER NOT NULL,
    option_value TEXT NOT NULL,
    display_text TEXT NOT NULL,
    is_negative BOOLEAN DEFAULT FALSE,
    description TEXT,
    sort_order INTEGER DEFAULT 0,
    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (group_id) REFERENCES tag_groups(id) ON DELETE CASCADE
);

-- Annotation tags table
CREATE TABLE IF NOT EXISTS annotation_tags (
    id SERIAL PRIMARY KEY,
    annotation_id INTEGER NOT NULL,
    annotation_type TEXT NOT NULL,
    group_id INTEGER NOT NULL,
    tag_value TEXT NOT NULL,
    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (group_id) REFERENCES tag_groups(id) ON DELETE CASCADE
);

-- Tag suggestions table
CREATE TABLE IF NOT EXISTS tag_suggestions (
    id SERIAL PRIMARY KEY,
    category TEXT NOT NULL,
    tag_text TEXT NOT NULL,
    is_negative BOOLEAN DEFAULT FALSE,
    description TEXT,
    sort_order INTEGER DEFAULT 0,
    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- YOLO export configurations table
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

-- YOLO export videos junction table
CREATE TABLE IF NOT EXISTS yolo_export_videos (
    id SERIAL PRIMARY KEY,
    export_config_id INTEGER NOT NULL,
    video_id INTEGER NOT NULL,
    included BOOLEAN DEFAULT TRUE,
    notes TEXT,
    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (export_config_id) REFERENCES yolo_export_configs(id) ON DELETE CASCADE,
    FOREIGN KEY (video_id) REFERENCES videos(id) ON DELETE CASCADE,
    UNIQUE(export_config_id, video_id)
);

-- YOLO export filters table
CREATE TABLE IF NOT EXISTS yolo_export_filters (
    id SERIAL PRIMARY KEY,
    export_config_id INTEGER NOT NULL,
    filter_type TEXT NOT NULL,
    filter_value TEXT NOT NULL,
    is_exclusion BOOLEAN DEFAULT FALSE,
    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (export_config_id) REFERENCES yolo_export_configs(id) ON DELETE CASCADE
);

-- YOLO export logs table
CREATE TABLE IF NOT EXISTS yolo_export_logs (
    id SERIAL PRIMARY KEY,
    export_config_id INTEGER NOT NULL,
    export_path TEXT NOT NULL,
    video_count INTEGER NOT NULL,
    annotation_count INTEGER NOT NULL,
    export_format TEXT NOT NULL,
    export_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    notes TEXT,
    FOREIGN KEY (export_config_id) REFERENCES yolo_export_configs(id) ON DELETE CASCADE
);

-- Fleet vehicles table
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

-- Vehicle person links table
CREATE TABLE IF NOT EXISTS vehicle_person_links (
    id SERIAL PRIMARY KEY,
    vehicle_fleet_id TEXT NOT NULL,
    person_name TEXT NOT NULL,
    first_seen_together TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen_together TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    times_seen_together INTEGER DEFAULT 1,
    FOREIGN KEY (vehicle_fleet_id) REFERENCES fleet_vehicles(fleet_id) ON DELETE CASCADE
);

-- Trailers table
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

-- Vehicle trailer links table
CREATE TABLE IF NOT EXISTS vehicle_trailer_links (
    id SERIAL PRIMARY KEY,
    vehicle_fleet_id TEXT NOT NULL,
    trailer_id TEXT NOT NULL,
    first_seen_together TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen_together TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    times_seen_together INTEGER DEFAULT 1,
    FOREIGN KEY (vehicle_fleet_id) REFERENCES fleet_vehicles(fleet_id) ON DELETE CASCADE,
    FOREIGN KEY (trailer_id) REFERENCES trailers(trailer_id) ON DELETE CASCADE
);

-- Sync configuration table
CREATE TABLE IF NOT EXISTS sync_config (
    id SERIAL PRIMARY KEY,
    config_key TEXT UNIQUE NOT NULL,
    config_value TEXT,
    is_encrypted BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Sync history table
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

-- EcoEye alerts table
CREATE TABLE IF NOT EXISTS ecoeye_alerts (
    id SERIAL PRIMARY KEY,
    alert_id TEXT UNIQUE NOT NULL,
    camera_id TEXT,
    timestamp TIMESTAMP,
    alert_type TEXT,
    confidence REAL,
    video_id TEXT,
    video_available BOOLEAN,
    video_downloaded BOOLEAN DEFAULT FALSE,
    local_video_path TEXT,
    video_path TEXT,
    metadata TEXT,
    synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Training jobs table
CREATE TABLE IF NOT EXISTS training_jobs (
    id SERIAL PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    s3_uri TEXT,
    config_json TEXT,
    result_json TEXT,
    error_message TEXT,
    submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    export_config_id INTEGER,
    FOREIGN KEY (export_config_id) REFERENCES yolo_export_configs(id)
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_videos_filename ON videos(filename);
CREATE INDEX IF NOT EXISTS idx_tags_name ON tags(name);
CREATE INDEX IF NOT EXISTS idx_behaviors_video ON behaviors(video_id);
CREATE INDEX IF NOT EXISTS idx_time_range_tags_video ON time_range_tags(video_id);
CREATE INDEX IF NOT EXISTS idx_keyframe_annotations_video ON keyframe_annotations(video_id);
CREATE INDEX IF NOT EXISTS idx_tag_suggestions_category ON tag_suggestions(category);
CREATE INDEX IF NOT EXISTS idx_tag_groups_name ON tag_groups(group_name);
CREATE INDEX IF NOT EXISTS idx_tag_options_group ON tag_options(group_id);
CREATE INDEX IF NOT EXISTS idx_annotation_tags_annotation ON annotation_tags(annotation_id, annotation_type);
CREATE INDEX IF NOT EXISTS idx_yolo_export_videos_config ON yolo_export_videos(export_config_id);
CREATE INDEX IF NOT EXISTS idx_yolo_export_filters_config ON yolo_export_filters(export_config_id);
CREATE INDEX IF NOT EXISTS idx_yolo_export_logs_config ON yolo_export_logs(export_config_id);
CREATE INDEX IF NOT EXISTS idx_fleet_vehicles_fleet_id ON fleet_vehicles(fleet_id);
CREATE INDEX IF NOT EXISTS idx_fleet_vehicles_type ON fleet_vehicles(fleet_type);
CREATE INDEX IF NOT EXISTS idx_vehicle_person_links_vehicle ON vehicle_person_links(vehicle_fleet_id);
CREATE INDEX IF NOT EXISTS idx_vehicle_person_links_person ON vehicle_person_links(person_name);
CREATE INDEX IF NOT EXISTS idx_trailers_trailer_id ON trailers(trailer_id);
CREATE INDEX IF NOT EXISTS idx_vehicle_trailer_links_vehicle ON vehicle_trailer_links(vehicle_fleet_id);
CREATE INDEX IF NOT EXISTS idx_vehicle_trailer_links_trailer ON vehicle_trailer_links(trailer_id);
"""


def init_schema():
    """
    Initialize all database tables and indexes.

    This function creates all 23 tables required for GroundTruth Studio
    with PostgreSQL-native types and constraints.

    Safe to call multiple times - uses CREATE TABLE IF NOT EXISTS.
    """
    logger.info("Initializing PostgreSQL database schema...")

    try:
        with get_cursor() as cursor:
            # Execute the complete schema
            cursor.execute(SCHEMA_SQL)
            logger.info("Database schema initialized successfully")

    except Exception as e:
        logger.error(f"Failed to initialize schema: {e}")
        raise


def verify_schema():
    """
    Verify that all required tables exist.

    Returns:
        dict: Status report with table counts and verification results
    """
    required_tables = [
        'videos', 'tags', 'video_tags', 'behaviors', 'time_range_tags',
        'keyframe_annotations', 'tag_groups', 'tag_options', 'annotation_tags',
        'tag_suggestions', 'yolo_export_configs', 'yolo_export_videos',
        'yolo_export_filters', 'yolo_export_logs', 'fleet_vehicles',
        'vehicle_person_links', 'trailers', 'vehicle_trailer_links',
        'sync_config', 'sync_history', 'ecoeye_alerts', 'training_jobs'
    ]

    with get_cursor(commit=False) as cursor:
        cursor.execute("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
        """)
        existing_tables = [row['table_name'] for row in cursor.fetchall()]

        missing_tables = [t for t in required_tables if t not in existing_tables]

        return {
            'total_required': len(required_tables),
            'total_existing': len(existing_tables),
            'missing_tables': missing_tables,
            'all_tables_present': len(missing_tables) == 0
        }


if __name__ == '__main__':
    # Allow running as standalone script for schema initialization
    logging.basicConfig(level=logging.INFO)
    init_schema()
    status = verify_schema()
    print(f"Schema verification: {status}")
