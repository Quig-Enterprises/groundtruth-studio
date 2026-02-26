"""
Groundtruth Studio — Flask application entry point.

All routes live in the ``routes/`` package as Flask Blueprints.
Shared service instances are initialized in ``services.py``.
"""
from flask import Flask, request, jsonify, render_template, g
import os
import logging

# Initialize shared services (must happen before blueprint imports)
import services

# Import all blueprints (services are already initialized)
from routes import (
    videos_bp, annotations_bp, ecoeye_bp, unifi_bp,
    training_bp, yolo_export_bp, predictions_bp, tracks_bp,
    models_bp, persons_bp, frigate_bp, locations_bp, vibration_bp,
    camera_map_bp, clip_analysis_bp, training_gallery_bp, documents_bp,
    doc_training_browser_bp, doc_template_annotator_bp,
    face_photo_manager_bp, identities_bp,
)

app = Flask(__name__,
            template_folder='../templates',
            static_folder='../static')
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024 * 1024  # 2GB max upload
logger = logging.getLogger(__name__)


# ---- Middleware ----

@app.after_request
def add_cors_headers(response):
    """Allow cross-origin requests from LAN dashboard."""
    origin = request.headers.get('Origin', '')
    if origin and '192.168.50.' in origin:
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response


# ---- RBAC: Role-based access control via nginx auth_request ----

@app.before_request
def check_role_permissions():
    """Enforce write protection for viewers. Nginx passes X-Auth-Role header."""
    g.user_role = request.headers.get('X-Auth-Role', 'viewer')
    g.can_write = g.user_role in ('super', 'admin', 'user')

    # Block write operations for viewers
    if request.method in ('POST', 'PUT', 'DELETE') and not g.can_write:
        # Allow static files and client logging through
        if request.path.startswith('/static/') or request.path == '/api/client-log':
            return None
        return jsonify({'success': False, 'error': 'Insufficient permissions. Write access requires user role or above.'}), 403


@app.context_processor
def inject_role():
    """Make role info available in all templates."""
    return {
        'user_role': getattr(g, 'user_role', 'viewer'),
        'can_write': getattr(g, 'can_write', False)
    }


@app.context_processor
def inject_static_helpers():
    """Provide static_v() for cache-busting static files with their mtime."""
    def static_v(filename):
        filepath = app.static_folder and os.path.join(app.static_folder, filename)
        try:
            mtime = int(os.path.getmtime(filepath))
        except OSError:
            mtime = 0
        return f'/static/{filename}?v={mtime}'
    return {'static_v': static_v}


# ---- Register Blueprints ----

app.register_blueprint(videos_bp)
app.register_blueprint(annotations_bp)
app.register_blueprint(ecoeye_bp)
app.register_blueprint(unifi_bp)
app.register_blueprint(training_bp)
app.register_blueprint(yolo_export_bp)
app.register_blueprint(predictions_bp)
app.register_blueprint(tracks_bp)
app.register_blueprint(models_bp)
app.register_blueprint(persons_bp)
app.register_blueprint(frigate_bp)
app.register_blueprint(locations_bp)
app.register_blueprint(vibration_bp)
app.register_blueprint(camera_map_bp)
app.register_blueprint(clip_analysis_bp)
app.register_blueprint(training_gallery_bp)
app.register_blueprint(documents_bp)
app.register_blueprint(doc_training_browser_bp)
app.register_blueprint(doc_template_annotator_bp)
app.register_blueprint(face_photo_manager_bp)
app.register_blueprint(identities_bp)


# ---- Home Page ----

@app.route('/')
def index():
    return render_template('index.html')


# Support for reverse proxy
app.wsgi_app = app.wsgi_app


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5050, threaded=True)
