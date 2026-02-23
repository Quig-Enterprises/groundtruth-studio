"""
VideoDatabase — thin façade that composes all domain mixins.

Each mixin lives in its own file under ``repos/``.
VideoDatabase inherits every mixin so existing call-sites
(``db.some_method(...)``) continue to work without changes.
"""
from repos import (
    VideoMixin, AnnotationMixin, PredictionMixin, TrackMixin,
    ModelMixin, PersonMixin, FleetMixin, YoloExportMixin, ViolationMixin,
    DocumentMixin,
)


class VideoDatabase(
    VideoMixin, AnnotationMixin, PredictionMixin, TrackMixin,
    ModelMixin, PersonMixin, FleetMixin, YoloExportMixin, ViolationMixin,
    DocumentMixin,
):
    def __init__(self, db_path=None):
        """
        Initialize VideoDatabase.

        Args:
            db_path: Ignored (kept for backwards compatibility).
                     PostgreSQL connection is configured via DATABASE_URL env var.
        """
        pass
