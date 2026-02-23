"""Database mixin modules for VideoDatabase."""
from repos.video_mixin import VideoMixin
from repos.annotation_mixin import AnnotationMixin
from repos.prediction_mixin import PredictionMixin
from repos.track_mixin import TrackMixin
from repos.model_mixin import ModelMixin
from repos.person_mixin import PersonMixin
from repos.fleet_mixin import FleetMixin
from repos.yolo_export_mixin import YoloExportMixin
from repos.violation_mixin import ViolationMixin
from repos.document_mixin import DocumentMixin

__all__ = [
    'VideoMixin', 'AnnotationMixin', 'PredictionMixin', 'TrackMixin',
    'ModelMixin', 'PersonMixin', 'FleetMixin', 'YoloExportMixin', 'ViolationMixin',
    'DocumentMixin',
]
