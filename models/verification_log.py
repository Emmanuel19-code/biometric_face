from datetime import datetime
from app import db

class VerificationLog(db.Model):
    __tablename__ = "verification_logs"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("examination_sessions.id"), index=True)
    student_id = db.Column(db.Integer, db.ForeignKey("students.id"), index=True, nullable=True)
    claimed_student_id = db.Column(db.String(50), nullable=True)

    timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    outcome = db.Column(db.String(30), nullable=False)  # SUCCESS / FAIL
    reason = db.Column(db.String(255), nullable=True)
    confidence = db.Column(db.Float, nullable=True)

    ip_address = db.Column(db.String(45))
    device_info = db.Column(db.String(200))
    station_id = db.Column(db.Integer, db.ForeignKey("exam_stations.id"), nullable=True, index=True)

    def to_dict(self):
        return {
            "id": self.id,
            "session_id": self.session_id,
            "student_id": self.student_id,
            "claimed_student_id": self.claimed_student_id,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "outcome": self.outcome,
            "reason": self.reason,
            "confidence": self.confidence,
            "ip_address": self.ip_address,
            "device_info": self.device_info,
            "station_id": self.station_id
        }