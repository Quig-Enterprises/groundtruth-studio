"""
Document upload, scanning, OCR, and identity linking API routes.
"""

import logging
import os
import time
import threading
from flask import Blueprint, request, jsonify, render_template

from database import VideoDatabase
from db_connection import get_cursor

logger = logging.getLogger(__name__)
documents_bp = Blueprint('documents', __name__)
db = VideoDatabase()

UPLOAD_DIR = "/opt/groundtruth-studio/thumbnails"
ALLOWED_EXTENSIONS = {'jpg', 'jpeg', 'png', 'tiff', 'tif', 'pdf'}


def _allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


# ---- Page Routes ----

@documents_bp.route('/document-upload')
def document_upload_page():
    return render_template('document_upload.html')


# ---- Upload & Scanner API ----

@documents_bp.route('/api/documents/upload', methods=['POST'])
def upload_document():
    """Upload a document image for detection + OCR processing."""
    try:
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'No file provided'}), 400

        file = request.files['file']
        if not file.filename or not _allowed_file(file.filename):
            return jsonify({'success': False, 'error': 'Invalid file type'}), 400

        document_type = request.form.get('document_type')
        uploader = request.headers.get('X-Auth-User', 'anonymous')

        # Save file
        timestamp = int(time.time())
        ext = file.filename.rsplit('.', 1)[1].lower()
        filename = f"doc_upload_{timestamp}_{uploader}.{ext}"
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        filepath = os.path.join(UPLOAD_DIR, filename)
        file.save(filepath)

        # Create video record (documents use the videos table as image container)
        with get_cursor() as cursor:
            cursor.execute("""
                INSERT INTO videos (title, thumbnail_path, source, metadata)
                VALUES (%s, %s, %s, %s::jsonb)
                RETURNING id
            """, (
                f"Document Upload - {file.filename}",
                filepath,
                'upload',
                '{"type": "document_upload", "uploader": "' + uploader.replace('"', '') + '"}'
            ))
            video_id = cursor.fetchone()['id']

        # Trigger document detection pipeline
        _trigger_detect_pipeline(video_id, filepath, document_type, 'manual_upload')

        return jsonify({
            'success': True,
            'video_id': video_id,
            'message': 'Document uploaded. Detection and OCR processing started.',
        })

    except Exception as e:
        logger.error(f"Document upload failed: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@documents_bp.route('/api/documents/scan', methods=['POST'])
def scanner_api():
    """Scanner API endpoint. Accepts base64 or multipart image from document scanners."""
    try:
        scanner_id = request.headers.get('X-Scanner-ID', 'unknown')
        document_type = None

        if request.content_type and 'multipart' in request.content_type:
            file = request.files.get('file')
            if not file:
                return jsonify({'success': False, 'error': 'No file in multipart'}), 400
            document_type = request.form.get('document_type')

            timestamp = int(time.time())
            ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else 'jpg'
            filename = f"scan_{scanner_id}_{timestamp}.{ext}"
            os.makedirs(UPLOAD_DIR, exist_ok=True)
            filepath = os.path.join(UPLOAD_DIR, filename)
            file.save(filepath)
        else:
            import base64
            data = request.get_json()
            if not data or 'image' not in data:
                return jsonify({'success': False, 'error': 'No image data'}), 400

            document_type = data.get('document_type')
            image_data = base64.b64decode(data['image'])
            timestamp = int(time.time())
            filename = f"scan_{scanner_id}_{timestamp}.jpg"
            os.makedirs(UPLOAD_DIR, exist_ok=True)
            filepath = os.path.join(UPLOAD_DIR, filename)
            with open(filepath, 'wb') as f:
                f.write(image_data)

        # Create video record
        with get_cursor() as cursor:
            cursor.execute("""
                INSERT INTO videos (title, thumbnail_path, source, metadata)
                VALUES (%s, %s, %s, %s::jsonb)
                RETURNING id
            """, (
                f"Scanner: {scanner_id}",
                filepath,
                'scanner',
                '{"type": "scanner", "scanner_id": "' + scanner_id.replace('"', '') + '"}'
            ))
            video_id = cursor.fetchone()['id']

        _trigger_detect_pipeline(video_id, filepath, document_type, 'scanner')

        return jsonify({
            'success': True,
            'video_id': video_id,
            'message': 'Scan received. Processing started.',
        })

    except Exception as e:
        logger.error(f"Scanner API failed: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


def _trigger_detect_pipeline(video_id: int, image_path: str,
                             document_type: str = None, source_method: str = 'manual_upload'):
    """Trigger document detection + OCR in background."""
    def _run():
        try:
            from doc_detect_runner import run_document_detection
            result = run_document_detection(
                video_id, image_path,
                force_review=True,
                source_method=source_method
            )
            if result:
                logger.info(f"Document detection complete for video {video_id}: {result.get('documents', 0)} documents")
        except Exception as e:
            logger.error(f"Document detection pipeline failed for video {video_id}: {e}")

    thread = threading.Thread(target=_run, daemon=True, name=f"doc-pipeline-{video_id}")
    thread.start()


# ---- Status & Retrieval ----

@documents_bp.route('/api/documents/scan/<int:scan_id>/status', methods=['GET'])
def get_scan_status(scan_id):
    """Poll OCR completion status for a document scan."""
    try:
        scan = db.get_document_scan(scan_id)
        if not scan:
            return jsonify({'success': False, 'error': 'Scan not found'}), 404

        return jsonify({
            'success': True,
            'scan_id': scan_id,
            'ocr_status': scan['ocr_status'],
            'document_type': scan['document_type'],
            'ocr_completed_at': scan['ocr_completed_at'].isoformat() if scan.get('ocr_completed_at') else None,
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@documents_bp.route('/api/documents/<int:doc_id>', methods=['GET'])
def get_document(doc_id):
    """Get full document details including OCR fields and identity link."""
    try:
        with get_cursor(commit=False) as cursor:
            cursor.execute("""
                SELECT id.*, ds.crop_image_path, ds.ocr_status, ds.video_id,
                       ds.source_method, ds.metadata as scan_metadata,
                       i.name as identity_name, i.identity_type
                FROM identity_documents id
                JOIN document_scans ds ON ds.id = id.document_scan_id
                LEFT JOIN identities i ON i.identity_id = id.identity_id
                WHERE id.id = %s
            """, (doc_id,))
            row = cursor.fetchone()
            if not row:
                return jsonify({'success': False, 'error': 'Document not found'}), 404

            doc = dict(row)
            # Parse JSONB fields
            import json
            if doc.get('extracted_fields') and isinstance(doc['extracted_fields'], str):
                doc['extracted_fields'] = json.loads(doc['extracted_fields'])

            return jsonify({'success': True, 'document': doc})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ---- Identity Linking ----

@documents_bp.route('/api/documents/<int:doc_id>/link-identity', methods=['POST'])
def link_document_to_identity(doc_id):
    """Link a document to an existing identity or create a new one."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'Request body required'}), 400

        identity_id = data.get('identity_id')
        create_new = data.get('create_new', False)

        if create_new:
            # Create new identity from document data
            name = data.get('name', '')
            identity_type = data.get('identity_type', 'person')
            with get_cursor() as cursor:
                cursor.execute("""
                    INSERT INTO identities (name, identity_type)
                    VALUES (%s, %s)
                    RETURNING identity_id
                """, (name, identity_type))
                identity_id = cursor.fetchone()['identity_id']

        if not identity_id:
            return jsonify({'success': False, 'error': 'identity_id required or create_new=true'}), 400

        linked = db.link_document_to_identity(doc_id, identity_id)

        # Update identity name from document if empty
        if linked:
            with get_cursor(commit=False) as cursor:
                cursor.execute("SELECT name FROM identities WHERE identity_id = %s", (str(identity_id),))
                identity = cursor.fetchone()
                if identity and not identity['name']:
                    # Get holder_name from the document
                    cursor2_result = None
                    with get_cursor() as cursor2:
                        cursor2.execute("SELECT holder_name FROM identity_documents WHERE id = %s", (doc_id,))
                        cursor2_result = cursor2.fetchone()
                        if cursor2_result and cursor2_result['holder_name']:
                            cursor2.execute(
                                "UPDATE identities SET name = %s WHERE identity_id = %s",
                                (cursor2_result['holder_name'], str(identity_id))
                            )

        # Check for duplicate document numbers
        duplicate_warning = None
        if linked:
            with get_cursor(commit=False) as cursor:
                cursor.execute("""
                    SELECT id.id, id.document_number, id.identity_id, i.name as identity_name
                    FROM identity_documents id
                    LEFT JOIN identities i ON i.identity_id = id.identity_id
                    WHERE id.document_number IS NOT NULL
                      AND id.document_number != ''
                      AND id.document_number = (
                          SELECT document_number FROM identity_documents WHERE id = %s
                      )
                      AND id.id != %s
                """, (doc_id, doc_id))
                dupes = [dict(r) for r in cursor.fetchall()]
                if dupes:
                    duplicate_warning = {
                        'message': f"Document number matches {len(dupes)} other document(s)",
                        'duplicates': [{'id': d['id'], 'identity_name': d.get('identity_name', 'Unknown')} for d in dupes]
                    }

        return jsonify({
            'success': True,
            'document_id': doc_id,
            'identity_id': str(identity_id),
            'linked': linked,
            'duplicate_warning': duplicate_warning,
        })

    except Exception as e:
        logger.error(f"Link document {doc_id} to identity failed: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@documents_bp.route('/api/documents/by-identity/<identity_id>', methods=['GET'])
def get_documents_by_identity(identity_id):
    """Get all documents for a given identity."""
    try:
        documents = db.get_documents_for_identity(identity_id)
        return jsonify({'success': True, 'documents': documents, 'count': len(documents)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@documents_bp.route('/api/documents/search', methods=['GET'])
def search_documents():
    """Search identity documents by number, holder name, or type."""
    try:
        document_number = request.args.get('document_number')
        holder_name = request.args.get('holder_name')
        document_type = request.args.get('document_type')
        limit = request.args.get('limit', 50, type=int)

        results = db.search_documents(
            document_number=document_number,
            holder_name=holder_name,
            document_type=document_type,
            limit=limit,
        )
        return jsonify({'success': True, 'documents': results, 'count': len(results)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ---- Training Data Export ----

@documents_bp.route('/api/documents/export-training', methods=['POST'])
def export_training_data():
    """Export document training data (YOLO detection + DocTR OCR formats)."""
    try:
        data = request.get_json() or {}
        export_type = data.get('type', 'both')  # 'detection', 'ocr', 'both'
        output_dir = data.get('output_dir', '/opt/groundtruth-studio/exports/documents')

        def _run_export():
            try:
                from ocr_exporter import DocumentTrainingExporter
                exporter = DocumentTrainingExporter(output_dir)
                if export_type in ('detection', 'both'):
                    exporter.export_detection_dataset()
                if export_type in ('ocr', 'both'):
                    exporter.export_ocr_dataset()
                logger.info(f"Document training export complete: {export_type}")
            except Exception as e:
                logger.error(f"Document training export failed: {e}")

        thread = threading.Thread(target=_run_export, daemon=True, name="doc-export")
        thread.start()

        return jsonify({
            'success': True,
            'message': f'Export started ({export_type}). Output directory: {output_dir}',
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
