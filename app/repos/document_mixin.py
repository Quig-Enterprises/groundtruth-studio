"""
Document scan and identity document repository mixin.

Provides database operations for document scanning, OCR tracking,
and identity document management.
"""

import logging
from typing import Dict, List, Optional
from db_connection import get_cursor
from psycopg2 import extras

logger = logging.getLogger(__name__)


class DocumentMixin:
    """Document scan, OCR result, and identity document methods."""

    def create_document_scan(self, prediction_id: int, video_id: int,
                              document_type: Optional[str] = None,
                              source_method: str = 'manual_upload',
                              crop_image_path: Optional[str] = None) -> int:
        """Create a document scan record linked to a detection prediction."""
        with get_cursor() as cursor:
            cursor.execute("""
                INSERT INTO document_scans
                    (prediction_id, video_id, document_type, source_method, crop_image_path)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
            """, (prediction_id, video_id, document_type, source_method, crop_image_path))
            return cursor.fetchone()['id']

    def update_document_scan(self, scan_id: int, **kwargs):
        """Update document scan fields (ocr_status, document_type, identity_id, etc.)."""
        allowed = {'ocr_status', 'ocr_completed_at', 'document_type', 'identity_id',
                   'crop_image_path', 'metadata'}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return

        set_clauses = []
        params = []
        for key, value in updates.items():
            if key == 'metadata':
                set_clauses.append("metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb")
                params.append(extras.Json(value))
            else:
                set_clauses.append(f"{key} = %s")
                params.append(value)
        params.append(scan_id)

        with get_cursor() as cursor:
            cursor.execute(
                f"UPDATE document_scans SET {', '.join(set_clauses)} WHERE id = %s",
                params
            )

    def get_document_scan(self, scan_id: int) -> Optional[Dict]:
        """Get a document scan by ID."""
        with get_cursor(commit=False) as cursor:
            cursor.execute("SELECT * FROM document_scans WHERE id = %s", (scan_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_document_scan_by_prediction(self, prediction_id: int) -> Optional[Dict]:
        """Get document scan linked to a detection prediction."""
        with get_cursor(commit=False) as cursor:
            cursor.execute("SELECT * FROM document_scans WHERE prediction_id = %s", (prediction_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def create_identity_document(self, document_scan_id: int, document_type: str,
                                  identity_id=None, document_number: Optional[str] = None,
                                  holder_name: Optional[str] = None,
                                  expiry_date=None, issuing_authority: Optional[str] = None,
                                  extracted_fields: Optional[Dict] = None) -> int:
        """Create an identity document record."""
        with get_cursor() as cursor:
            cursor.execute("""
                INSERT INTO identity_documents
                    (identity_id, document_scan_id, document_type, document_number,
                     holder_name, expiry_date, issuing_authority, extracted_fields)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (document_scan_id, document_type) DO UPDATE SET
                    document_number = EXCLUDED.document_number,
                    holder_name = EXCLUDED.holder_name,
                    expiry_date = EXCLUDED.expiry_date,
                    issuing_authority = EXCLUDED.issuing_authority,
                    extracted_fields = EXCLUDED.extracted_fields
                RETURNING id
            """, (identity_id, document_scan_id, document_type, document_number,
                  holder_name, expiry_date, issuing_authority,
                  extras.Json(extracted_fields or {})))
            return cursor.fetchone()['id']

    def link_document_to_identity(self, document_id: int, identity_id) -> bool:
        """Link an identity document to an identity (person/vehicle/boat)."""
        with get_cursor() as cursor:
            cursor.execute("""
                UPDATE identity_documents SET identity_id = %s WHERE id = %s
            """, (str(identity_id), document_id))
            cursor.execute("""
                UPDATE document_scans SET identity_id = %s
                WHERE id = (SELECT document_scan_id FROM identity_documents WHERE id = %s)
            """, (str(identity_id), document_id))
            return cursor.rowcount > 0

    def get_documents_for_identity(self, identity_id) -> List[Dict]:
        """Get all identity documents for a given identity."""
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT id.*, ds.crop_image_path, ds.ocr_status, ds.video_id
                FROM identity_documents id
                JOIN document_scans ds ON ds.id = id.document_scan_id
                WHERE id.identity_id = %s
                ORDER BY id.created_at DESC
            """, (str(identity_id),))
            return [dict(row) for row in cursor.fetchall()]

    def search_documents(self, document_number: Optional[str] = None,
                          holder_name: Optional[str] = None,
                          document_type: Optional[str] = None,
                          limit: int = 50) -> List[Dict]:
        """Search identity documents by number, holder name, or type."""
        conditions = []
        params = []
        if document_number:
            conditions.append("id.document_number ILIKE %s")
            params.append(f"%{document_number}%")
        if holder_name:
            conditions.append("id.holder_name ILIKE %s")
            params.append(f"%{holder_name}%")
        if document_type:
            conditions.append("id.document_type = %s")
            params.append(document_type)

        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        params.append(limit)

        with get_cursor(commit=False) as cursor:
            cursor.execute(f"""
                SELECT id.*, ds.crop_image_path, ds.video_id, ds.source_method,
                       i.name as identity_name, i.identity_type
                FROM identity_documents id
                JOIN document_scans ds ON ds.id = id.document_scan_id
                LEFT JOIN identities i ON i.identity_id = id.identity_id
                {where}
                ORDER BY id.created_at DESC
                LIMIT %s
            """, params)
            return [dict(row) for row in cursor.fetchall()]
