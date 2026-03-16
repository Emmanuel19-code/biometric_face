from datetime import datetime
from app import db


class SessionInvigilator(db.Model):
    """Invigilator assignment for an exam session."""
    __tablename__ = "session_invigilators"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(
        db.Integer,
        db.ForeignKey("examination_sessions.id"),
        nullable=False,
        index=True
    )
    invigilator_id = db.Column(
        db.Integer,
        db.ForeignKey("admins.id"),
        nullable=False,
        index=True
    )
    assigned_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    assigned_by = db.Column(db.Integer, db.ForeignKey("admins.id"), nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("session_id", "invigilator_id", name="uq_session_invigilator"),
    )

    session = db.relationship("ExaminationSession", backref="invigilator_assignments")
    invigilator = db.relationship("Admin", foreign_keys=[invigilator_id], backref="session_assignments")

    def to_dict(self):
        return {
            "id": self.id,
            "session_id": self.session_id,
            "invigilator_id": self.invigilator_id,
            "assigned_at": self.assigned_at.isoformat() if self.assigned_at else None,
            "assigned_by": self.assigned_by,
            "is_active": self.is_active,
            "invigilator": self.invigilator.to_dict() if self.invigilator else None
        }
