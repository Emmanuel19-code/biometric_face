from datetime import datetime
from app import db

class VerificationChallenge(db.Model):
    __tablename__ = "verification_challenges"

    id = db.Column(db.Integer, primary_key=True)
    station_id = db.Column(db.Integer, db.ForeignKey("exam_stations.id"), nullable=False, index=True)
    session_id = db.Column(db.Integer, db.ForeignKey("examination_sessions.id"), nullable=False, index=True)

    challenge = db.Column(db.String(50), nullable=False)   # blink/turn_left/turn_right
    nonce = db.Column(db.String(120), nullable=False, unique=True, index=True)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def is_expired(self):
        return datetime.utcnow() > self.expires_at

    def is_used(self):
        return self.used_at is not None