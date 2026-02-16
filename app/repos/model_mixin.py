import json
from datetime import datetime
from typing import List, Dict, Optional

from psycopg2 import extras

from db_connection import get_cursor


class ModelMixin:
    """Model registry and training metrics."""

    # ==================== Model Registry ====================

    def get_or_create_model_registry(self, model_name: str, model_version: str,
                                     model_type: str = 'yolo') -> Dict:
        """Get or create a model registry entry."""
        with get_cursor() as cursor:
            cursor.execute('''
                SELECT * FROM model_registry WHERE model_name = %s AND model_version = %s
            ''', (model_name, model_version))
            row = cursor.fetchone()
            if row:
                return dict(row)

            cursor.execute('''
                INSERT INTO model_registry (model_name, model_version, model_type)
                VALUES (%s, %s, %s)
                RETURNING *
            ''', (model_name, model_version, model_type))
            row = cursor.fetchone()
            return dict(row)

    def get_model_registry(self, model_name: str = None, active_only: bool = True) -> List[Dict]:
        """Get model registry entries, optionally filtered."""
        with get_cursor(commit=False) as cursor:
            conditions = []
            params = []
            if model_name:
                conditions.append("model_name = %s")
                params.append(model_name)
            if active_only:
                conditions.append("is_active = true")
            where = "WHERE " + " AND ".join(conditions) if conditions else ""
            cursor.execute(f'''
                SELECT * FROM model_registry {where}
                ORDER BY model_name, model_version DESC
            ''', params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def update_model_thresholds(self, model_name: str, model_version: str,
                                thresholds: Dict) -> Optional[Dict]:
        """Update confidence thresholds for a model."""
        with get_cursor() as cursor:
            cursor.execute('''
                UPDATE model_registry
                SET confidence_thresholds = %s, updated_at = NOW()
                WHERE model_name = %s AND model_version = %s
                RETURNING *
            ''', (extras.Json(thresholds), model_name, model_version))
            row = cursor.fetchone()
            return dict(row) if row else None

    def update_model_approval_stats(self, model_name: str, model_version: str) -> Optional[Dict]:
        """Recalculate and update approval stats for a model from actual prediction data."""
        with get_cursor() as cursor:
            cursor.execute('''
                SELECT
                    COUNT(*) as total,
                    COUNT(*) FILTER (WHERE review_status IN ('approved', 'auto_approved')) as approved,
                    COUNT(*) FILTER (WHERE review_status IN ('rejected', 'auto_rejected')) as rejected
                FROM ai_predictions
                WHERE model_name = %s AND model_version = %s
            ''', (model_name, model_version))
            stats = cursor.fetchone()

            total = stats['total']
            approved = stats['approved']
            rejected = stats['rejected']
            approval_rate = approved / total if total > 0 else None

            cursor.execute('''
                UPDATE model_registry
                SET total_predictions = %s, total_approved = %s, total_rejected = %s,
                    approval_rate = %s, updated_at = NOW()
                WHERE model_name = %s AND model_version = %s
                RETURNING *
            ''', (total, approved, rejected, approval_rate, model_name, model_version))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_model_stats(self, model_name: str, model_version: str = None) -> Dict:
        """Get comprehensive model performance stats."""
        with get_cursor(commit=False) as cursor:
            conditions = ["model_name = %s"]
            params = [model_name]
            if model_version:
                conditions.append("model_version = %s")
                params.append(model_version)
            where = " AND ".join(conditions)

            # Overall stats
            cursor.execute(f'''
                SELECT
                    COUNT(*) as total_predictions,
                    COUNT(*) FILTER (WHERE review_status IN ('approved', 'auto_approved')) as approved,
                    COUNT(*) FILTER (WHERE review_status IN ('rejected', 'auto_rejected')) as rejected,
                    COUNT(*) FILTER (WHERE review_status = 'pending') as pending,
                    COUNT(*) FILTER (WHERE review_status = 'needs_correction') as needs_correction,
                    AVG(confidence) FILTER (WHERE review_status IN ('approved', 'auto_approved')) as avg_confidence_approved,
                    AVG(confidence) FILTER (WHERE review_status IN ('rejected', 'auto_rejected')) as avg_confidence_rejected
                FROM ai_predictions
                WHERE {where}
            ''', params)
            overall = dict(cursor.fetchone())

            total = overall['total_predictions']
            overall['approval_rate'] = overall['approved'] / total if total > 0 else None

            # Per-scenario stats
            cursor.execute(f'''
                SELECT scenario,
                    COUNT(*) as total,
                    COUNT(*) FILTER (WHERE review_status IN ('approved', 'auto_approved')) as approved,
                    COUNT(*) FILTER (WHERE review_status IN ('rejected', 'auto_rejected')) as rejected
                FROM ai_predictions
                WHERE {where}
                GROUP BY scenario
                ORDER BY COUNT(*) DESC
            ''', params)
            scenarios = {}
            for row in cursor.fetchall():
                row = dict(row)
                stotal = row['total']
                scenarios[row['scenario']] = {
                    'total': stotal,
                    'approved': row['approved'],
                    'rejected': row['rejected'],
                    'approval_rate': row['approved'] / stotal if stotal > 0 else None
                }

            overall['scenarios'] = scenarios
            return overall

    # ==================== Training Metrics ====================

    def insert_training_metrics(self, training_job_id: int, model_name: str,
                                model_version: str, metrics: Dict) -> int:
        """Insert training metrics for a completed job."""
        with get_cursor() as cursor:
            cursor.execute('''
                INSERT INTO training_metrics
                (training_job_id, model_name, model_version, accuracy, loss,
                 val_accuracy, val_loss, class_metrics, confusion_matrix,
                 epochs, training_duration_seconds, dataset_size, dataset_hash)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            ''', (
                training_job_id, model_name, model_version,
                metrics.get('accuracy'), metrics.get('loss'),
                metrics.get('val_accuracy'), metrics.get('val_loss'),
                extras.Json(metrics.get('class_metrics')) if metrics.get('class_metrics') else None,
                extras.Json(metrics.get('confusion_matrix')) if metrics.get('confusion_matrix') else None,
                metrics.get('epochs'), metrics.get('training_duration_seconds'),
                metrics.get('dataset_size'), metrics.get('dataset_hash')
            ))
            result = cursor.fetchone()

            # Also update the model registry with latest metrics
            cursor.execute('''
                UPDATE model_registry
                SET latest_metrics = %s, updated_at = NOW()
                WHERE model_name = %s AND model_version = %s
            ''', (extras.Json(metrics), model_name, model_version))

            return result['id']

    def get_training_metrics_history(self, model_name: str, model_version: str = None,
                                     limit: int = 20) -> List[Dict]:
        """Get training metrics history for a model."""
        with get_cursor(commit=False) as cursor:
            if model_version:
                cursor.execute('''
                    SELECT tm.*, tj.job_type, tj.status as job_status
                    FROM training_metrics tm
                    LEFT JOIN training_jobs tj ON tm.training_job_id = tj.id
                    WHERE tm.model_name = %s AND tm.model_version = %s
                    ORDER BY tm.created_at DESC
                    LIMIT %s
                ''', (model_name, model_version, limit))
            else:
                cursor.execute('''
                    SELECT tm.*, tj.job_type, tj.status as job_status
                    FROM training_metrics tm
                    LEFT JOIN training_jobs tj ON tm.training_job_id = tj.id
                    WHERE tm.model_name = %s
                    ORDER BY tm.created_at DESC
                    LIMIT %s
                ''', (model_name, limit))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
