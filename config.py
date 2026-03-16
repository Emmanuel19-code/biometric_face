import os
from dotenv import load_dotenv

load_dotenv()

def _build_sqlserver_odbc_dsn():
    """Build SQL Server ODBC DSN from environment variables."""
    host = os.getenv("SQLSERVER_HOST", "").strip()
    if not host:
        return None

    instance = os.getenv("SQLSERVER_INSTANCE", "").strip()
    port = os.getenv("SQLSERVER_PORT", "").strip()
    database = os.getenv("SQLSERVER_DATABASE", "attendance_system").strip() or "attendance_system"
    driver = os.getenv("SQLSERVER_DRIVER", "ODBC Driver 18 for SQL Server").strip()
    trust_cert = os.getenv("SQLSERVER_TRUST_SERVER_CERTIFICATE", "yes").strip().lower() in ("1", "true", "yes")
    trusted_connection = os.getenv("SQLSERVER_TRUSTED_CONNECTION", "no").strip().lower() in ("1", "true", "yes")

    parts = [
        f"DRIVER={{{driver}}}",
        (
            f"SERVER={host}\\{instance}"
            if instance
            else (f"SERVER={host},{port}" if port else f"SERVER={host}")
        ),
        f"DATABASE={database}",
    ]

    if trusted_connection:
        parts.append("Trusted_Connection=yes")
    else:
        user = os.getenv("SQLSERVER_USER", "").strip()
        password = os.getenv("SQLSERVER_PASSWORD", "").strip()
        if user:
            parts.append(f"UID={user}")
        if password:
            parts.append(f"PWD={password}")

    if trust_cert:
        parts.append("TrustServerCertificate=yes")

    encrypt = os.getenv("SQLSERVER_ENCRYPT", "").strip()
    if encrypt:
        parts.append(f"Encrypt={encrypt}")

    return ";".join(parts)


def _resolve_database_config():
    """Resolve DB backend and connection data from supported env vars."""
    db_uri = (os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL") or "").strip()
    if db_uri:
        if db_uri.startswith("postgres://"):
            db_uri = db_uri.replace("postgres://", "postgresql://", 1)
        return {"backend": "postgresql", "dsn": db_uri}

    sqlserver_dsn = _build_sqlserver_odbc_dsn()
    if sqlserver_dsn:
        return {"backend": "sqlserver", "dsn": sqlserver_dsn}

    return {"backend": "sqlite", "dsn": "sqlite:///attendance_system.db"}


def get_database_backend():
    return _resolve_database_config()["backend"]


def get_database_dsn():
    return _resolve_database_config()["dsn"]


def _resolve_database_uri():
    """Backward-compatible URI resolver for SQLAlchemy settings."""
    cfg = _resolve_database_config()
    if cfg["backend"] == "sqlserver":
        # Flask-SQLAlchemy is not used by this project for DB access, but this
        # keeps a valid URI in config when SQL Server is selected.
        return "mssql+pyodbc:///?odbc_connect=<configured-via-env>"
    return cfg["dsn"]


class Config:
    """Application configuration"""
    SECRET_KEY = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')
    JWT_SECRET_KEY = os.getenv('JWT_SECRET_KEY', 'dev-jwt-secret-key-change-in-production')
    JWT_ACCESS_TOKEN_EXPIRES = int(os.getenv('JWT_ACCESS_TOKEN_EXPIRES', 3600))  # 1 hour
    JWT_REFRESH_TOKEN_EXPIRES = int(os.getenv('JWT_REFRESH_TOKEN_EXPIRES', 86400))  # 24 hours
    
    # Database
    SQLALCHEMY_DATABASE_URI = _resolve_database_uri()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    
    # Face Recognition
    FACE_ENCODING_TOLERANCE = float(os.getenv('FACE_ENCODING_TOLERANCE', 0.6))
    MIN_FACE_DETECTION_CONFIDENCE = float(os.getenv('MIN_FACE_DETECTION_CONFIDENCE', 0.5))
    REQUIRED_ANGLES = int(os.getenv('REQUIRED_ANGLES', 3))
    
    # Security
    ENCRYPTION_KEY = os.getenv('ENCRYPTION_KEY', 'dev-encryption-key-32-bytes!!').encode()
    
    # File Upload
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB max file size
    UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'uploads')
    FACE_DATA_FOLDER = os.path.join(os.path.dirname(__file__), 'face_data')
    
    # Server
    HOST = os.getenv('HOST', '0.0.0.0')
    PORT = int(os.getenv('PORT', 5000))
    
    @staticmethod
    def init_app(app):
        """Initialize application directories"""
        os.makedirs(Config.UPLOAD_FOLDER, exist_ok=True)
        os.makedirs(Config.FACE_DATA_FOLDER, exist_ok=True)


class DevelopmentConfig(Config):
    DEBUG = True


class ProductionConfig(Config):
    DEBUG = False


config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'default': DevelopmentConfig
}

EXAM_STATION_KEYS = set(
    k.strip() for k in os.getenv("EXAM_STATION_KEYS", "").split(",") if k.strip()
)
# Face Recognition (MediaPipe + ArcFace ONNX)
FACE_MATCH_THRESHOLD = float(os.getenv("FACE_MATCH_THRESHOLD", 0.35))  # cosine distance threshold (lower = stricter)
EMBEDDING_MODEL_PATH = os.getenv("EMBEDDING_MODEL_PATH", "models/arcface_r100.onnx")
EMBEDDING_MODEL_URL = os.getenv("EMBEDDING_MODEL_URL", os.getenv("ARCFACE_MODEL_URL", ""))
LIVENESS_REQUIRED = os.getenv("LIVENESS_REQUIRED", "true").lower() == "true"
