"""Main application file"""
from flask import Flask, jsonify, redirect, url_for
from flask_jwt_extended import JWTManager
from flask import request, render_template
from flask_cors import CORS
from config import config
from utils.logger import setup_logger
from utils import db as db_utils
from werkzeug.security import generate_password_hash
import os
import sys
import importlib
import importlib.util

# Allow running as `py app.py` without creating a second `app` module object.
if __name__ == "__main__":
    sys.modules.setdefault("app", sys.modules[__name__])

# Initialize extensions
jwt = JWTManager()

# Setup logger
logger = setup_logger()


def create_app(config_name=None):
    """Application factory"""
    app = Flask(__name__, template_folder="templates", static_folder="static")
    
    # Load configuration
    config_name = config_name or os.getenv('FLASK_ENV', 'development')
    app.config.from_object(config[config_name])
    config[config_name].init_app(app)
    
    # Initialize extensions
    jwt.init_app(app)
    CORS(app)  # Enable CORS for all routes

    # Initialize database connection and schema
    db_utils.init_pool()
    db_utils.init_db_schema()
    
    # Ensure this project directory is first on import path.
    project_root = os.path.dirname(os.path.abspath(__file__))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    # Register blueprints
    web_file_path = os.path.join(project_root, "web.py")
    web_spec = importlib.util.spec_from_file_location("web_file_module", web_file_path)
    if web_spec is None or web_spec.loader is None:
        raise RuntimeError(f"Failed to load web routes module from {web_file_path}")
    web_module = importlib.util.module_from_spec(web_spec)
    web_spec.loader.exec_module(web_module)
    web_bp = web_module.web_bp
    from api.auth import auth_bp
    from api.students import students_bp
    from api.attendance import attendance_bp
    from api.admin import admin_bp
    from api.admin_stations import stations_bp
    from api.logs import logs_bp
    from api.challenge import challenge_bp
    
    
    app.register_blueprint(auth_bp)
    app.register_blueprint(students_bp)
    app.register_blueprint(attendance_bp)
    app.register_blueprint(admin_bp)
    
    app.register_blueprint(logs_bp)
    
    app.register_blueprint(stations_bp)
    app.register_blueprint(challenge_bp)

    # ✅ Web pages blueprint (HTML)
    app.register_blueprint(web_bp)
    
    # Error handlers
    @app.errorhandler(400)
    def bad_request(error):
        return jsonify({'error': 'Bad request'}), 400
    
    @app.errorhandler(401)
    def unauthorized(error):
        return jsonify({'error': 'Unauthorized'}), 401
    
    @app.errorhandler(404)
    def not_found(error):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Resource not found"}), 404
        return render_template("errors/404.html"), 404
    
    @app.errorhandler(500)
    def internal_error(error):
        logger.error(f"Internal server error: {str(error)}")
        return jsonify({'error': 'Internal server error'}), 500
    
    # Health check endpoint
    @app.route('/health', methods=['GET'])
    def health_check():
        return jsonify({'status': 'healthy', 'service': 'Facial Recognition Attendance System'}), 200

    @app.route('/health/db', methods=['GET'])
    def health_check_db():
        try:
            db_utils.fetch_one("SELECT 1 AS ok")
            return jsonify({'status': 'ok', 'db': 'reachable'}), 200
        except Exception as exc:
            logger.error(f"DB health check failed: {exc}")
            return jsonify({'status': 'error', 'db': 'unreachable'}), 500

    @app.route('/debug/instance', methods=['GET'])
    def debug_instance():
        routes = sorted(str(r.rule) for r in app.url_map.iter_rules())
        return jsonify({
            'app_file': __file__,
            'cwd': os.getcwd(),
            'web_file': getattr(web_module, '__file__', None),
            'has_verify_test': '/verify/test' in routes,
            'has_debug_routes': '/debug/routes' in routes,
        }), 200
    
    # Root endpoint
    @app.route('/', methods=['GET'])
    def root():
        return redirect(url_for("web.login_page"))
    
    # Create default admin if none exists
    admin_exists = db_utils.fetch_one("SELECT id FROM admins LIMIT 1")
    if not admin_exists:
        db_utils.execute(
            """
            INSERT INTO admins (username, email, full_name, role, password_hash)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                "admin",
                "admin@example.com",
                "System Administrator",
                "super_admin",
                generate_password_hash("admin123")
            )
        )
        logger.info("Default admin user created: username='admin', password='admin123'")
    
    logger.info("Application initialized successfully")
    return app


if __name__ == '__main__':
    app = create_app()
    app.run(
        host=app.config['HOST'],
        port=app.config['PORT'],
        debug=app.config['DEBUG']
    )
