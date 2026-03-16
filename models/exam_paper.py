from datetime import datetime
from app import db


class ExamPaper(db.Model):
    """Exam paper attached to a specific exam session."""
    __tablename__ = "exam_papers"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(
        db.Integer,
        db.ForeignKey("examination_sessions.id"),
        nullable=False,
        index=True
    )
    paper_code = db.Column(db.String(50), nullable=True)
    paper_title = db.Column(db.String(200), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    session = db.relationship("ExaminationSession", backref="papers")

    def to_dict(self):
        return {
            "id": self.id,
            "session_id": self.session_id,
            "paper_code": self.paper_code,
            "paper_title": self.paper_title,
            "created_at": self.created_at.isoformat() if self.created_at else None
        }
