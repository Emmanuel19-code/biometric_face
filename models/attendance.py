from datetime import datetime
from app import db


class ExaminationSession(db.Model):
    """Examination session model"""
    __tablename__ = 'examination_sessions'
    
    id = db.Column(db.Integer, primary_key=True)
    session_name = db.Column(db.String(200), nullable=False)
    course_code = db.Column(db.String(50))
    venue = db.Column(db.String(200))
    start_time = db.Column(db.DateTime, nullable=False)
    end_time = db.Column(db.DateTime, nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey('admins.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_active = db.Column(db.Boolean, default=True)
    
    # Relationships
    attendances = db.relationship('Attendance', backref='session', lazy=True, cascade='all, delete-orphan')
    creator = db.relationship('Admin', backref='created_sessions')
    
    def to_dict(self):
        """Convert session to dictionary"""
        return {
            'id': self.id,
            'session_name': self.session_name,
            'course_code': self.course_code,
            'venue': self.venue,
            'start_time': self.start_time.isoformat() if self.start_time else None,
            'end_time': self.end_time.isoformat() if self.end_time else None,
            'created_by': self.created_by,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'is_active': self.is_active,
            'attendance_count': len(self.attendances)
        }
    
    def __repr__(self):
        return f'<ExaminationSession {self.session_name}>'


class Attendance(db.Model):
    """Attendance record model"""
    __tablename__ = 'attendances'
    
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False, index=True)
    session_id = db.Column(db.Integer, db.ForeignKey('examination_sessions.id'), nullable=False, index=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    verification_confidence = db.Column(db.Float)  # Face recognition confidence score
    verification_method = db.Column(db.String(50), default='face_recognition')
    ip_address = db.Column(db.String(45))  # IPv6 compatible
    device_info = db.Column(db.String(200))
    
    # Ensure one attendance per student per session
    __table_args__ = (db.UniqueConstraint('student_id', 'session_id', name='unique_student_session'),)
    
    def to_dict(self):
        """Convert attendance to dictionary"""
        return {
            'id': self.id,
            'student_id': self.student_id,
            'student': self.student.to_dict() if self.student else None,
            'session_id': self.session_id,
            'session': self.session.to_dict() if self.session else None,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None,
            'verification_confidence': self.verification_confidence,
            'verification_method': self.verification_method,
            'ip_address': self.ip_address,
            'device_info': self.device_info
        }
    
    def __repr__(self):
        return f'<Attendance Student:{self.student_id} Session:{self.session_id} at {self.timestamp}>'
