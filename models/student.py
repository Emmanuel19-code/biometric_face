from datetime import datetime
from app import db
from utils.encryption import encrypt_data, decrypt_data


class Student(db.Model):
    """Student model with encrypted biometric data"""
    __tablename__ = 'students'
    
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.String(50), unique=True, nullable=False, index=True)
    first_name = db.Column(db.String(100), nullable=False)
    last_name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    profile_photo = db.Column(db.Text)
    phone = db.Column(db.String(20))
    department = db.Column(db.String(100))
    course = db.Column(db.String(100))
    year_level = db.Column(db.String(20))
    
    # Encrypted face encodings (stored as JSON string)
    face_encodings = db.Column(db.Text, nullable=False)
    
    # Metadata
    registration_date = db.Column(db.DateTime, default=datetime.utcnow)
    is_active = db.Column(db.Boolean, default=True)
    last_updated = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    attendances = db.relationship('Attendance', backref='student', lazy=True, cascade='all, delete-orphan')
    
    def __init__(self, **kwargs):
        super(Student, self).__init__(**kwargs)
    
    def set_face_encodings(self, encodings):
        """Encrypt and store face encodings"""
        import json
        encodings_json = json.dumps([encoding.tolist() for encoding in encodings])
        self.face_encodings = encrypt_data(encodings_json)
    
    def get_face_encodings(self):
        """Decrypt and retrieve face encodings"""
        import json
        import numpy as np
        decrypted = decrypt_data(self.face_encodings)
        encodings_list = json.loads(decrypted)
        return [np.array(encoding) for encoding in encodings_list]
    
    def to_dict(self, include_encodings=False):
        """Convert student to dictionary"""
        data = {
            'id': self.id,
            'student_id': self.student_id,
            'first_name': self.first_name,
            'last_name': self.last_name,
            'email': self.email,
            'profile_photo': self.profile_photo,
            'phone': self.phone,
            'department': self.department,
            'course': self.course,
            'year_level': self.year_level,
            'registration_date': self.registration_date.isoformat() if self.registration_date else None,
            'is_active': self.is_active,
            'last_updated': self.last_updated.isoformat() if self.last_updated else None
        }
        if include_encodings:
            data['face_encodings_count'] = len(self.get_face_encodings())
        return data
    
    def __repr__(self):
        return f'<Student {self.student_id}: {self.first_name} {self.last_name}>'
