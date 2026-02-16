"""Route blueprints for Groundtruth Studio."""
from routes.videos import videos_bp
from routes.annotations import annotations_bp
from routes.ecoeye import ecoeye_bp
from routes.unifi import unifi_bp
from routes.training import training_bp
from routes.yolo_export import yolo_export_bp
from routes.predictions import predictions_bp
from routes.tracks import tracks_bp
from routes.models import models_bp
from routes.persons import persons_bp
from routes.frigate import frigate_bp
from routes.locations import locations_bp
from routes.vibration import vibration_bp

__all__ = [
    'videos_bp', 'annotations_bp', 'ecoeye_bp', 'unifi_bp',
    'training_bp', 'yolo_export_bp', 'predictions_bp', 'tracks_bp',
    'models_bp', 'persons_bp', 'frigate_bp', 'locations_bp', 'vibration_bp',
]
