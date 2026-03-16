"""Database initialization script (raw Postgres)"""
from werkzeug.security import generate_password_hash
from utils import db as db_utils


def init_database():
    """Initialize database with tables"""
    db_utils.init_pool()
    db_utils.init_db_schema()

    existing = db_utils.fetch_one("SELECT id FROM admins LIMIT 1")
    if not existing:
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
        print("Default admin created: username='admin', password='admin123'")

    print("Database initialized successfully!")


if __name__ == '__main__':
    init_database()
