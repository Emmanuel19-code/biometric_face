from datetime import datetime
from app import db


class ExamRegistration(db.Model):
    """Student registration for a specific examination session."""
    __tablename__ = "exam_registrations"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(
        db.Integer,
        db.ForeignKey("examination_sessions.id"),
        nullable=False,
        index=True
    )
    student_id = db.Column(
        db.Integer,
        db.ForeignKey("students.id"),
        nullable=False,
        index=True
    )
    registered_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    registered_by = db.Column(db.Integer, db.ForeignKey("admins.id"), nullable=True)

    __table_args__ = (
        db.UniqueConstraint("session_id", "student_id", name="uq_session_student_registration"),
    )

    session = db.relationship("ExaminationSession", backref="registrations")
    student = db.relationship("Student", backref="exam_registrations")

    def to_dict(self):
        return {
            "id": self.id,
            "session_id": self.session_id,
            "student_id": self.student_id,
            "registered_at": self.registered_at.isoformat() if self.registered_at else None,
            "registered_by": self.registered_by,
            "student": self.student.to_dict() if self.student else None
        }
