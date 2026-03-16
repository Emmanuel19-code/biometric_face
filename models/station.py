from datetime import datetime
from app import db
from werkzeug.security import generate_password_hash, check_password_hash

class ExamStation(db.Model):
    __tablename__ = "exam_stations"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    api_key_hash = db.Column(db.String(255), nullable=False)
    ip_whitelist = db.Column(db.Text)  # optional: comma-separated IPs
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_key(self, raw_key: str):
        self.api_key_hash = generate_password_hash(raw_key)

    def check_key(self, raw_key: str) -> bool:
        return check_password_hash(self.api_key_hash, raw_key)

    def allowed_ip(self, ip: str) -> bool:
        if not self.ip_whitelist:
            return True
        allowed = [x.strip() for x in self.ip_whitelist.split(",") if x.strip()]
        return ip in allowed

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "ip_whitelist": self.ip_whitelist,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None
        }