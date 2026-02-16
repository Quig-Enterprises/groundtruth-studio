"""
PostgreSQL Database Schema for GroundTruth Studio

This module defines all database tables and provides initialization functions.
PostgreSQL schema with native types and features.
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
    notes TEXT,
    camera_id TEXT,
    metadata JSONB DEFAULT '{}'
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

-- Camera locations table (for location recognition training)
CREATE TABLE IF NOT EXISTS camera_locations (
    id SERIAL PRIMARY KEY,
    camera_id TEXT NOT NULL,
    camera_name TEXT,
    location_name TEXT NOT NULL,
    location_description TEXT,
    site_name TEXT,
    reference_image_path TEXT,
    latitude REAL,
    longitude REAL,
    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(camera_id)
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

-- AI Predictions table (for model prediction submission and human review)
CREATE TABLE IF NOT EXISTS ai_predictions (
    id BIGSERIAL PRIMARY KEY,
    video_id INTEGER NOT NULL,
    model_name VARCHAR(255) NOT NULL,
    model_version VARCHAR(50) NOT NULL,
    prediction_type VARCHAR(20) NOT NULL CHECK (prediction_type IN ('keyframe', 'time_range')),
    confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    timestamp REAL,
    start_time REAL,
    end_time REAL,
    bbox_x INTEGER,
    bbox_y INTEGER,
    bbox_width INTEGER,
    bbox_height INTEGER,
    scenario VARCHAR(255) NOT NULL,
    predicted_tags JSONB NOT NULL DEFAULT '{}',
    review_status VARCHAR(20) DEFAULT 'pending'
        CHECK (review_status IN ('pending', 'approved', 'rejected', 'needs_correction', 'auto_approved', 'auto_rejected', 'processing')),
    reviewed_by VARCHAR(255),
    reviewed_at TIMESTAMP WITH TIME ZONE,
    review_notes TEXT,
    corrected_tags JSONB,
    corrected_bbox JSONB,
    correction_type VARCHAR(20),
    routed_by VARCHAR(20) DEFAULT 'manual'
        CHECK (routed_by IN ('manual', 'auto_confidence', 'auto_threshold')),
    routing_threshold_used JSONB,
    created_annotation_id INTEGER,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    batch_id VARCHAR(255),
    inference_time_ms INTEGER,
    FOREIGN KEY (video_id) REFERENCES videos(id) ON DELETE CASCADE
);

-- Model registry table (tracks registered models and their routing thresholds)
CREATE TABLE IF NOT EXISTS model_registry (
    id BIGSERIAL PRIMARY KEY,
    model_name VARCHAR(255) NOT NULL,
    model_version VARCHAR(50) NOT NULL,
    model_type VARCHAR(50) NOT NULL,
    description TEXT,
    confidence_thresholds JSONB NOT NULL DEFAULT '{"auto_approve": 0.95, "review": 0.7, "auto_reject": 0.3}',
    latest_metrics JSONB,
    approval_rate REAL,
    total_predictions INTEGER DEFAULT 0,
    total_approved INTEGER DEFAULT 0,
    total_rejected INTEGER DEFAULT 0,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(model_name, model_version)
);

-- Training metrics table (stores per-job training performance metrics)
CREATE TABLE IF NOT EXISTS training_metrics (
    id BIGSERIAL PRIMARY KEY,
    training_job_id INTEGER REFERENCES training_jobs(id),
    model_name VARCHAR(255) NOT NULL,
    model_version VARCHAR(50) NOT NULL,
    accuracy REAL,
    loss REAL,
    val_accuracy REAL,
    val_loss REAL,
    class_metrics JSONB,
    confusion_matrix JSONB,
    epochs INTEGER,
    training_duration_seconds INTEGER,
    dataset_size INTEGER,
    dataset_hash VARCHAR(64),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Content libraries table
CREATE TABLE IF NOT EXISTS content_libraries (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    is_default BOOLEAN DEFAULT FALSE,
    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Content library items junction table (many-to-many)
CREATE TABLE IF NOT EXISTS content_library_items (
    library_id INTEGER NOT NULL,
    video_id INTEGER NOT NULL,
    added_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (library_id, video_id),
    FOREIGN KEY (library_id) REFERENCES content_libraries(id) ON DELETE CASCADE,
    FOREIGN KEY (video_id) REFERENCES videos(id) ON DELETE CASCADE
);

-- Multi-Entity Detection System tables

-- Identities table (long-term identity records)
CREATE TABLE IF NOT EXISTS identities (
    identity_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255),
    identity_type VARCHAR(20) NOT NULL CHECK (identity_type IN ('person', 'vehicle', 'boat', 'trailer')),
    first_seen TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    last_seen TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    metadata JSONB DEFAULT '{}',
    is_flagged BOOLEAN NOT NULL DEFAULT FALSE,
    notes TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- Embeddings table (face, body ReID, and visual appearance embeddings)
CREATE TABLE IF NOT EXISTS embeddings (
    embedding_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    identity_id UUID NOT NULL REFERENCES identities(identity_id) ON DELETE CASCADE,
    embedding_type VARCHAR(30) NOT NULL CHECK (embedding_type IN ('face', 'body_reid', 'boat_reid', 'vehicle_appearance')),
    vector REAL[] NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    source_image_path VARCHAR(500),
    camera_id VARCHAR(100),
    is_reference BOOLEAN NOT NULL DEFAULT FALSE,
    session_date DATE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- Associations table (links entities together with probabilistic confidence)
CREATE TABLE IF NOT EXISTS associations (
    association_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    identity_a UUID NOT NULL REFERENCES identities(identity_id) ON DELETE CASCADE,
    identity_b UUID NOT NULL REFERENCES identities(identity_id) ON DELETE CASCADE,
    association_type VARCHAR(30) NOT NULL CHECK (association_type IN ('person_vehicle', 'vehicle_trailer', 'trailer_boat', 'person_boat')),
    confidence REAL NOT NULL DEFAULT 0.0 CHECK (confidence >= 0.0 AND confidence <= 1.0),
    observation_count INTEGER NOT NULL DEFAULT 1,
    first_observed TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    last_observed TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    UNIQUE(identity_a, identity_b, association_type)
);

-- Tracks table (continuous observation of a single entity within one camera)
CREATE TABLE IF NOT EXISTS tracks (
    track_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    identity_id UUID REFERENCES identities(identity_id) ON DELETE SET NULL,
    camera_id VARCHAR(100) NOT NULL,
    entity_type VARCHAR(20) NOT NULL CHECK (entity_type IN ('person', 'vehicle', 'boat', 'trailer')),
    start_time TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    end_time TIMESTAMP WITH TIME ZONE,
    identity_method VARCHAR(20) CHECK (identity_method IN ('face', 'reid', 'plate', 'registration', 'manual', 'association')),
    identity_confidence REAL CHECK (identity_confidence >= 0.0 AND identity_confidence <= 1.0)
);

-- Sightings table (per-frame detections within a track)
CREATE TABLE IF NOT EXISTS sightings (
    sighting_id BIGSERIAL PRIMARY KEY,
    track_id UUID NOT NULL REFERENCES tracks(track_id) ON DELETE CASCADE,
    timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
    bbox REAL[4] NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    face_visible BOOLEAN NOT NULL DEFAULT FALSE
);

-- Camera topology learned (spatial relationships between cameras)
CREATE TABLE IF NOT EXISTS camera_topology_learned (
    camera_a VARCHAR(100) NOT NULL,
    camera_b VARCHAR(100) NOT NULL,
    min_transit_seconds INTEGER NOT NULL,
    max_transit_seconds INTEGER NOT NULL,
    avg_transit_seconds REAL,
    observation_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (camera_a, camera_b)
);

-- Violations table (detected violations with full association chain)
CREATE TABLE IF NOT EXISTS violations (
    violation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    violation_type VARCHAR(50) NOT NULL CHECK (violation_type IN ('power_loading', 'unauthorized_dock', 'speed_violation', 'no_wake_zone', 'other')),
    camera_id VARCHAR(100) NOT NULL,
    timestamp TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    person_identity_id UUID REFERENCES identities(identity_id) ON DELETE SET NULL,
    vehicle_identity_id UUID REFERENCES identities(identity_id) ON DELETE SET NULL,
    boat_identity_id UUID REFERENCES identities(identity_id) ON DELETE SET NULL,
    trailer_identity_id UUID REFERENCES identities(identity_id) ON DELETE SET NULL,
    evidence_paths TEXT[] NOT NULL DEFAULT '{}',
    confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    status VARCHAR(20) NOT NULL DEFAULT 'detected' CHECK (status IN ('detected', 'confirmed', 'false_positive', 'actioned')),
    reviewed_by VARCHAR(100),
    notes TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- Visits table (aggregated visit records - primary enforcement unit)
CREATE TABLE IF NOT EXISTS visits (
    visit_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    person_identity_id UUID REFERENCES identities(identity_id) ON DELETE SET NULL,
    vehicle_identity_id UUID REFERENCES identities(identity_id) ON DELETE SET NULL,
    boat_identity_id UUID REFERENCES identities(identity_id) ON DELETE SET NULL,
    arrival_time TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    departure_time TIMESTAMP WITH TIME ZONE,
    violation_ids UUID[] DEFAULT '{}',
    track_ids UUID[] NOT NULL DEFAULT '{}',
    camera_timeline JSONB NOT NULL DEFAULT '[]',
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
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
CREATE INDEX IF NOT EXISTS idx_camera_locations_camera_id ON camera_locations(camera_id);
CREATE INDEX IF NOT EXISTS idx_camera_locations_location ON camera_locations(location_name);
CREATE INDEX IF NOT EXISTS idx_videos_camera_id ON videos(camera_id);
CREATE INDEX IF NOT EXISTS idx_ai_predictions_video ON ai_predictions(video_id);
CREATE INDEX IF NOT EXISTS idx_ai_predictions_status ON ai_predictions(review_status);
CREATE INDEX IF NOT EXISTS idx_ai_predictions_model ON ai_predictions(model_name, model_version);
CREATE INDEX IF NOT EXISTS idx_ai_predictions_confidence ON ai_predictions(confidence);
CREATE INDEX IF NOT EXISTS idx_ai_predictions_batch ON ai_predictions(batch_id);
CREATE INDEX IF NOT EXISTS idx_ai_predictions_created ON ai_predictions(created_at);

-- Interpolation tracks table (guided keyframe interpolation between approved predictions)
CREATE TABLE IF NOT EXISTS interpolation_tracks (
    id SERIAL PRIMARY KEY,
    video_id INTEGER NOT NULL,
    class_name VARCHAR(255) NOT NULL,
    start_prediction_id INTEGER NOT NULL,
    end_prediction_id INTEGER NOT NULL,
    start_timestamp REAL NOT NULL,
    end_timestamp REAL NOT NULL,
    frame_interval REAL DEFAULT 1.0,
    status VARCHAR(20) DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'ready', 'approved', 'rejected')),
    frames_generated INTEGER DEFAULT 0,
    frames_detected INTEGER DEFAULT 0,
    batch_id VARCHAR(255),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    reviewed_at TIMESTAMP WITH TIME ZONE,
    reviewed_by VARCHAR(255),
    FOREIGN KEY (video_id) REFERENCES videos(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_interp_tracks_video ON interpolation_tracks(video_id);
CREATE INDEX IF NOT EXISTS idx_interp_tracks_status ON interpolation_tracks(status);
CREATE INDEX IF NOT EXISTS idx_model_registry_name ON model_registry(model_name);
CREATE INDEX IF NOT EXISTS idx_model_registry_active ON model_registry(is_active);
CREATE INDEX IF NOT EXISTS idx_training_metrics_job ON training_metrics(training_job_id);
CREATE INDEX IF NOT EXISTS idx_training_metrics_model ON training_metrics(model_name, model_version);

-- Prediction groups table (same-camera prediction grouping for batch review)
CREATE TABLE IF NOT EXISTS prediction_groups (
    id BIGSERIAL PRIMARY KEY,
    camera_id TEXT NOT NULL,
    scenario VARCHAR(255) NOT NULL,
    representative_prediction_id BIGINT,
    bbox_centroid_x INTEGER NOT NULL,
    bbox_centroid_y INTEGER NOT NULL,
    avg_bbox_width INTEGER NOT NULL,
    avg_bbox_height INTEGER NOT NULL,
    member_count INTEGER NOT NULL DEFAULT 1,
    min_confidence REAL,
    max_confidence REAL,
    avg_confidence REAL,
    min_timestamp REAL,
    max_timestamp REAL,
    review_status VARCHAR(20) DEFAULT 'pending'
        CHECK (review_status IN ('pending', 'approved', 'rejected', 'partial')),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_pred_groups_camera ON prediction_groups(camera_id);
CREATE INDEX IF NOT EXISTS idx_pred_groups_status ON prediction_groups(review_status);
CREATE INDEX IF NOT EXISTS idx_pred_groups_scenario ON prediction_groups(scenario);

-- Camera object tracks (cross-status spatial grouping for decision propagation)
CREATE TABLE IF NOT EXISTS camera_object_tracks (
    id BIGSERIAL PRIMARY KEY,
    camera_id TEXT NOT NULL,
    scenario VARCHAR(255) NOT NULL,
    bbox_centroid_x INTEGER NOT NULL,
    bbox_centroid_y INTEGER NOT NULL,
    avg_bbox_width INTEGER NOT NULL,
    avg_bbox_height INTEGER NOT NULL,
    member_count INTEGER NOT NULL DEFAULT 0,
    approved_count INTEGER NOT NULL DEFAULT 0,
    rejected_count INTEGER NOT NULL DEFAULT 0,
    pending_count INTEGER NOT NULL DEFAULT 0,
    auto_approved_count INTEGER NOT NULL DEFAULT 0,
    anchor_status VARCHAR(20) DEFAULT 'pending'
        CHECK (anchor_status IN ('pending', 'approved', 'rejected', 'conflict')),
    anchor_classification JSONB,
    classification_conflict BOOLEAN DEFAULT FALSE,
    representative_prediction_id BIGINT,
    min_confidence REAL,
    max_confidence REAL,
    avg_confidence REAL,
    first_seen REAL,
    last_seen REAL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_cam_obj_tracks_camera ON camera_object_tracks(camera_id);
CREATE INDEX IF NOT EXISTS idx_cam_obj_tracks_status ON camera_object_tracks(anchor_status);
CREATE INDEX IF NOT EXISTS idx_cam_obj_tracks_scenario ON camera_object_tracks(scenario);
CREATE INDEX IF NOT EXISTS idx_cam_obj_tracks_camera_scenario ON camera_object_tracks(camera_id, scenario);

CREATE INDEX IF NOT EXISTS idx_content_libraries_name ON content_libraries(name);
CREATE INDEX IF NOT EXISTS idx_library_items_library ON content_library_items(library_id);
CREATE INDEX IF NOT EXISTS idx_library_items_video ON content_library_items(video_id);

-- Multi-Entity Detection System indexes
CREATE INDEX IF NOT EXISTS idx_identities_type ON identities(identity_type);
CREATE INDEX IF NOT EXISTS idx_identities_name ON identities(name);
CREATE INDEX IF NOT EXISTS idx_identities_flagged ON identities(is_flagged) WHERE is_flagged = TRUE;
CREATE INDEX IF NOT EXISTS idx_identities_last_seen ON identities(last_seen);
CREATE INDEX IF NOT EXISTS idx_embeddings_identity ON embeddings(identity_id);
CREATE INDEX IF NOT EXISTS idx_embeddings_type ON embeddings(embedding_type);
CREATE INDEX IF NOT EXISTS idx_embeddings_session ON embeddings(session_date);
CREATE INDEX IF NOT EXISTS idx_embeddings_camera ON embeddings(camera_id);
CREATE INDEX IF NOT EXISTS idx_associations_identity_a ON associations(identity_a);
CREATE INDEX IF NOT EXISTS idx_associations_identity_b ON associations(identity_b);
CREATE INDEX IF NOT EXISTS idx_associations_type ON associations(association_type);
CREATE INDEX IF NOT EXISTS idx_tracks_identity ON tracks(identity_id);
CREATE INDEX IF NOT EXISTS idx_tracks_camera ON tracks(camera_id);
CREATE INDEX IF NOT EXISTS idx_tracks_camera_time ON tracks(camera_id, start_time);
CREATE INDEX IF NOT EXISTS idx_tracks_entity_type ON tracks(entity_type);
CREATE INDEX IF NOT EXISTS idx_sightings_track ON sightings(track_id);
CREATE INDEX IF NOT EXISTS idx_sightings_track_time ON sightings(track_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_violations_type ON violations(violation_type);
CREATE INDEX IF NOT EXISTS idx_violations_status ON violations(status);
CREATE INDEX IF NOT EXISTS idx_violations_camera ON violations(camera_id);
CREATE INDEX IF NOT EXISTS idx_violations_timestamp ON violations(timestamp);
CREATE INDEX IF NOT EXISTS idx_violations_person ON violations(person_identity_id);
CREATE INDEX IF NOT EXISTS idx_visits_person ON visits(person_identity_id);
CREATE INDEX IF NOT EXISTS idx_visits_vehicle ON visits(vehicle_identity_id);
CREATE INDEX IF NOT EXISTS idx_visits_arrival ON visits(arrival_time);

-- Cross-camera entity links (matches same real-world entity across different cameras)
CREATE TABLE IF NOT EXISTS cross_camera_links (
    id BIGSERIAL PRIMARY KEY,
    track_a_id BIGINT NOT NULL REFERENCES camera_object_tracks(id) ON DELETE CASCADE,
    track_b_id BIGINT NOT NULL REFERENCES camera_object_tracks(id) ON DELETE CASCADE,
    entity_type VARCHAR(20) NOT NULL CHECK (entity_type IN ('vehicle', 'person', 'boat')),
    match_confidence REAL NOT NULL,
    match_method VARCHAR(50) NOT NULL,
    reid_similarity REAL,
    temporal_gap_seconds REAL,
    classification_match BOOLEAN,
    status VARCHAR(20) DEFAULT 'auto' CHECK (status IN ('auto', 'confirmed', 'rejected')),
    confirmed_by VARCHAR(100),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(track_a_id, track_b_id)
);
CREATE INDEX IF NOT EXISTS idx_xcam_track_a ON cross_camera_links(track_a_id);
CREATE INDEX IF NOT EXISTS idx_xcam_track_b ON cross_camera_links(track_b_id);
CREATE INDEX IF NOT EXISTS idx_xcam_entity ON cross_camera_links(entity_type);
CREATE INDEX IF NOT EXISTS idx_xcam_status ON cross_camera_links(status);
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

            # Migrations for cross-camera tracking
            migration_sqls = [
                "ALTER TABLE camera_object_tracks ADD COLUMN IF NOT EXISTS cross_camera_identity_id BIGINT",
                "ALTER TABLE camera_object_tracks ADD COLUMN IF NOT EXISTS cross_camera_conflict BOOLEAN DEFAULT FALSE",
                "CREATE INDEX IF NOT EXISTS idx_cam_obj_tracks_xcam_identity ON camera_object_tracks(cross_camera_identity_id)",
            ]
            for sql in migration_sqls:
                try:
                    cursor.execute(sql)
                except Exception:
                    pass  # Column may already exist

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
        'sync_config', 'sync_history', 'ecoeye_alerts', 'training_jobs',
        'camera_locations', 'ai_predictions', 'model_registry', 'training_metrics',
        'content_libraries', 'content_library_items', 'interpolation_tracks',
        'identities', 'embeddings', 'associations', 'tracks', 'sightings',
        'camera_topology_learned', 'violations', 'visits', 'prediction_groups',
        'camera_object_tracks', 'cross_camera_links'
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


def run_migrations():
    """Run database migrations for schema changes."""
    logger.info("Running database migrations...")

    try:
        with get_cursor() as cursor:
            # Migration: Add camera_id column to videos table if missing
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'videos' AND column_name = 'camera_id'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE videos ADD COLUMN camera_id TEXT")
                logger.info("Added camera_id column to videos table")

                # Backfill camera_id from original_url for ecoeye imports
                # URL format: ecoeye://{timestamp}_{MAC}_{type}
                cursor.execute("""
                    UPDATE videos
                    SET camera_id = split_part(
                        split_part(replace(original_url, 'ecoeye://', ''), '_', 2),
                        '_', 1
                    )
                    WHERE original_url LIKE 'ecoeye://%%'
                    AND camera_id IS NULL
                    AND original_url ~ 'ecoeye://[^_]+_[A-Fa-f0-9]+'
                """)
                logger.info("Backfilled camera_id from original_url for existing ecoeye imports")

            # Migration: Add is_active column to model_registry if missing
            try:
                cursor.execute("ALTER TABLE model_registry ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE")
            except Exception as e:
                logger.warning(f"is_active migration note: {e}")

            # Migration: Create content_libraries tables if missing
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS content_libraries (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    is_default BOOLEAN DEFAULT FALSE,
                    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS content_library_items (
                    library_id INTEGER NOT NULL,
                    video_id INTEGER NOT NULL,
                    added_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (library_id, video_id),
                    FOREIGN KEY (library_id) REFERENCES content_libraries(id) ON DELETE CASCADE,
                    FOREIGN KEY (video_id) REFERENCES videos(id) ON DELETE CASCADE
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_content_libraries_name ON content_libraries(name)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_library_items_library ON content_library_items(library_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_library_items_video ON content_library_items(video_id)")
            # Seed default "Uncategorized" library
            cursor.execute("""
                INSERT INTO content_libraries (name, is_default)
                VALUES ('Uncategorized', TRUE)
                ON CONFLICT (name) DO NOTHING
            """)
            logger.info("Content libraries tables ready")

            # Migration: Create Multi-Entity Detection System tables
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS identities (
                    identity_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    name VARCHAR(255),
                    identity_type VARCHAR(20) NOT NULL CHECK (identity_type IN ('person', 'vehicle', 'boat', 'trailer')),
                    first_seen TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                    last_seen TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                    metadata JSONB DEFAULT '{}',
                    is_flagged BOOLEAN NOT NULL DEFAULT FALSE,
                    notes TEXT,
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS embeddings (
                    embedding_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    identity_id UUID NOT NULL REFERENCES identities(identity_id) ON DELETE CASCADE,
                    embedding_type VARCHAR(30) NOT NULL CHECK (embedding_type IN ('face', 'body_reid', 'boat_reid', 'vehicle_appearance')),
                    vector REAL[] NOT NULL,
                    confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
                    source_image_path VARCHAR(500),
                    camera_id VARCHAR(100),
                    is_reference BOOLEAN NOT NULL DEFAULT FALSE,
                    session_date DATE,
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS associations (
                    association_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    identity_a UUID NOT NULL REFERENCES identities(identity_id) ON DELETE CASCADE,
                    identity_b UUID NOT NULL REFERENCES identities(identity_id) ON DELETE CASCADE,
                    association_type VARCHAR(30) NOT NULL CHECK (association_type IN ('person_vehicle', 'vehicle_trailer', 'trailer_boat', 'person_boat')),
                    confidence REAL NOT NULL DEFAULT 0.0 CHECK (confidence >= 0.0 AND confidence <= 1.0),
                    observation_count INTEGER NOT NULL DEFAULT 1,
                    first_observed TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                    last_observed TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                    UNIQUE(identity_a, identity_b, association_type)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS tracks (
                    track_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    identity_id UUID REFERENCES identities(identity_id) ON DELETE SET NULL,
                    camera_id VARCHAR(100) NOT NULL,
                    entity_type VARCHAR(20) NOT NULL CHECK (entity_type IN ('person', 'vehicle', 'boat', 'trailer')),
                    start_time TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                    end_time TIMESTAMP WITH TIME ZONE,
                    identity_method VARCHAR(20) CHECK (identity_method IN ('face', 'reid', 'plate', 'registration', 'manual', 'association')),
                    identity_confidence REAL CHECK (identity_confidence >= 0.0 AND identity_confidence <= 1.0)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sightings (
                    sighting_id BIGSERIAL PRIMARY KEY,
                    track_id UUID NOT NULL REFERENCES tracks(track_id) ON DELETE CASCADE,
                    timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
                    bbox REAL[4] NOT NULL,
                    confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
                    face_visible BOOLEAN NOT NULL DEFAULT FALSE
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS camera_topology_learned (
                    camera_a VARCHAR(100) NOT NULL,
                    camera_b VARCHAR(100) NOT NULL,
                    min_transit_seconds INTEGER NOT NULL,
                    max_transit_seconds INTEGER NOT NULL,
                    avg_transit_seconds REAL,
                    observation_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (camera_a, camera_b)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS violations (
                    violation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    violation_type VARCHAR(50) NOT NULL CHECK (violation_type IN ('power_loading', 'unauthorized_dock', 'speed_violation', 'no_wake_zone', 'other')),
                    camera_id VARCHAR(100) NOT NULL,
                    timestamp TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                    person_identity_id UUID REFERENCES identities(identity_id) ON DELETE SET NULL,
                    vehicle_identity_id UUID REFERENCES identities(identity_id) ON DELETE SET NULL,
                    boat_identity_id UUID REFERENCES identities(identity_id) ON DELETE SET NULL,
                    trailer_identity_id UUID REFERENCES identities(identity_id) ON DELETE SET NULL,
                    evidence_paths TEXT[] NOT NULL DEFAULT '{}',
                    confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
                    status VARCHAR(20) NOT NULL DEFAULT 'detected' CHECK (status IN ('detected', 'confirmed', 'false_positive', 'actioned')),
                    reviewed_by VARCHAR(100),
                    notes TEXT,
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS visits (
                    visit_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    person_identity_id UUID REFERENCES identities(identity_id) ON DELETE SET NULL,
                    vehicle_identity_id UUID REFERENCES identities(identity_id) ON DELETE SET NULL,
                    boat_identity_id UUID REFERENCES identities(identity_id) ON DELETE SET NULL,
                    arrival_time TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                    departure_time TIMESTAMP WITH TIME ZONE,
                    violation_ids UUID[] DEFAULT '{}',
                    track_ids UUID[] NOT NULL DEFAULT '{}',
                    camera_timeline JSONB NOT NULL DEFAULT '[]',
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
                )
            """)
            # Create indexes for Multi-Entity Detection System
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_identities_type ON identities(identity_type)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_identities_name ON identities(name)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_identities_flagged ON identities(is_flagged) WHERE is_flagged = TRUE")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_identities_last_seen ON identities(last_seen)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_embeddings_identity ON embeddings(identity_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_embeddings_type ON embeddings(embedding_type)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_embeddings_session ON embeddings(session_date)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_embeddings_camera ON embeddings(camera_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_associations_identity_a ON associations(identity_a)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_associations_identity_b ON associations(identity_b)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_associations_type ON associations(association_type)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tracks_identity ON tracks(identity_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tracks_camera ON tracks(camera_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tracks_camera_time ON tracks(camera_id, start_time)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tracks_entity_type ON tracks(entity_type)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_sightings_track ON sightings(track_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_sightings_track_time ON sightings(track_id, timestamp)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_violations_type ON violations(violation_type)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_violations_status ON violations(status)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_violations_camera ON violations(camera_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_violations_timestamp ON violations(timestamp)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_violations_person ON violations(person_identity_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_visits_person ON visits(person_identity_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_visits_vehicle ON visits(vehicle_identity_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_visits_arrival ON visits(arrival_time)")
            logger.info("Multi-Entity Detection System tables ready")

            # Migration: Create interpolation_tracks table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS interpolation_tracks (
                    id SERIAL PRIMARY KEY,
                    video_id INTEGER NOT NULL,
                    class_name VARCHAR(255) NOT NULL,
                    start_prediction_id INTEGER NOT NULL,
                    end_prediction_id INTEGER NOT NULL,
                    start_timestamp REAL NOT NULL,
                    end_timestamp REAL NOT NULL,
                    frame_interval REAL DEFAULT 1.0,
                    status VARCHAR(20) DEFAULT 'pending'
                        CHECK (status IN ('pending', 'processing', 'ready', 'approved', 'rejected')),
                    frames_generated INTEGER DEFAULT 0,
                    frames_detected INTEGER DEFAULT 0,
                    batch_id VARCHAR(255),
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    reviewed_at TIMESTAMP WITH TIME ZONE,
                    reviewed_by VARCHAR(255),
                    FOREIGN KEY (video_id) REFERENCES videos(id) ON DELETE CASCADE
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_interp_tracks_video ON interpolation_tracks(video_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_interp_tracks_status ON interpolation_tracks(status)")
            logger.info("Interpolation tracks table ready")

            # Migration: Add metadata JSONB column to videos table
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'videos' AND column_name = 'metadata'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE videos ADD COLUMN metadata JSONB DEFAULT '{}'")
                logger.info("Added metadata column to videos table")

            # Migration: Create prediction_groups table and add prediction_group_id to ai_predictions
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS prediction_groups (
                    id BIGSERIAL PRIMARY KEY,
                    camera_id TEXT NOT NULL,
                    scenario VARCHAR(255) NOT NULL,
                    representative_prediction_id BIGINT,
                    bbox_centroid_x INTEGER NOT NULL,
                    bbox_centroid_y INTEGER NOT NULL,
                    avg_bbox_width INTEGER NOT NULL,
                    avg_bbox_height INTEGER NOT NULL,
                    member_count INTEGER NOT NULL DEFAULT 1,
                    min_confidence REAL,
                    max_confidence REAL,
                    avg_confidence REAL,
                    min_timestamp REAL,
                    max_timestamp REAL,
                    review_status VARCHAR(20) DEFAULT 'pending'
                        CHECK (review_status IN ('pending', 'approved', 'rejected', 'partial')),
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_pred_groups_camera ON prediction_groups(camera_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_pred_groups_status ON prediction_groups(review_status)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_pred_groups_scenario ON prediction_groups(scenario)")

            # Add prediction_group_id column to ai_predictions if not exists
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'ai_predictions' AND column_name = 'prediction_group_id'
            """)
            if not cursor.fetchone():
                cursor.execute("""
                    ALTER TABLE ai_predictions ADD COLUMN prediction_group_id BIGINT
                    REFERENCES prediction_groups(id) ON DELETE SET NULL
                """)
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_ai_predictions_group ON ai_predictions(prediction_group_id)")
                logger.info("Added prediction_group_id column to ai_predictions table")
            logger.info("Prediction groups table ready")

            # Camera object tracks table + column migration
            cursor.execute("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables
                    WHERE table_name = 'camera_object_tracks'
                )
            """)
            if not cursor.fetchone()['exists']:
                cursor.execute("""
                    CREATE TABLE camera_object_tracks (
                        id BIGSERIAL PRIMARY KEY,
                        camera_id TEXT NOT NULL,
                        scenario VARCHAR(255) NOT NULL,
                        bbox_centroid_x INTEGER NOT NULL,
                        bbox_centroid_y INTEGER NOT NULL,
                        avg_bbox_width INTEGER NOT NULL,
                        avg_bbox_height INTEGER NOT NULL,
                        member_count INTEGER NOT NULL DEFAULT 0,
                        approved_count INTEGER NOT NULL DEFAULT 0,
                        rejected_count INTEGER NOT NULL DEFAULT 0,
                        pending_count INTEGER NOT NULL DEFAULT 0,
                        auto_approved_count INTEGER NOT NULL DEFAULT 0,
                        anchor_status VARCHAR(20) DEFAULT 'pending'
                            CHECK (anchor_status IN ('pending', 'approved', 'rejected', 'conflict')),
                        anchor_classification JSONB,
                        classification_conflict BOOLEAN DEFAULT FALSE,
                        representative_prediction_id BIGINT,
                        min_confidence REAL,
                        max_confidence REAL,
                        avg_confidence REAL,
                        first_seen REAL,
                        last_seen REAL,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                        updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                    )
                """)
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_cam_obj_tracks_camera ON camera_object_tracks(camera_id)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_cam_obj_tracks_status ON camera_object_tracks(anchor_status)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_cam_obj_tracks_scenario ON camera_object_tracks(scenario)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_cam_obj_tracks_camera_scenario ON camera_object_tracks(camera_id, scenario)")
                logger.info("Created camera_object_tracks table")

            # Add camera_object_track_id to ai_predictions
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'ai_predictions' AND column_name = 'camera_object_track_id'
            """)
            if not cursor.fetchone():
                cursor.execute("""
                    ALTER TABLE ai_predictions ADD COLUMN camera_object_track_id BIGINT
                    REFERENCES camera_object_tracks(id) ON DELETE SET NULL
                """)
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_ai_predictions_cam_track ON ai_predictions(camera_object_track_id)")
                logger.info("Added camera_object_track_id to ai_predictions")

            # Migration: Add rejection_reason to cross_camera_links
            cursor.execute("ALTER TABLE cross_camera_links ADD COLUMN IF NOT EXISTS rejection_reason VARCHAR(100)")

            # Migration: Add 'processing' to review_status CHECK constraint
            # Allows predictions to be held back from review until automated processing completes
            try:
                cursor.execute("""
                    SELECT conname FROM pg_constraint
                    WHERE conrelid = 'ai_predictions'::regclass
                      AND contype = 'c'
                      AND consrc LIKE '%%review_status%%'
                """)
                existing = cursor.fetchone()
                if existing:
                    constraint_name = existing['conname']
                    # Check if 'processing' is already in the constraint
                    cursor.execute("""
                        SELECT consrc FROM pg_constraint WHERE conname = %s
                    """, (constraint_name,))
                    consrc_row = cursor.fetchone()
                    if consrc_row and 'processing' not in (consrc_row.get('consrc') or ''):
                        cursor.execute(f"ALTER TABLE ai_predictions DROP CONSTRAINT {constraint_name}")
                        cursor.execute("""
                            ALTER TABLE ai_predictions ADD CONSTRAINT ai_predictions_review_status_check
                            CHECK (review_status IN ('pending', 'approved', 'rejected', 'needs_correction',
                                                     'auto_approved', 'auto_rejected', 'processing'))
                        """)
                        logger.info("Updated review_status CHECK constraint to include 'processing'")
            except Exception as e:
                logger.warning(f"review_status constraint migration note: {e}")

        logger.info("Migrations completed successfully")
    except Exception as e:
        logger.error(f"Migration error: {e}")
        raise


if __name__ == '__main__':
    # Allow running as standalone script for schema initialization
    logging.basicConfig(level=logging.INFO)
    init_schema()
    run_migrations()
    status = verify_schema()
    print(f"Schema verification: {status}")
