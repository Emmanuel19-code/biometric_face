from datetime import datetime
from app import db
from werkzeug.security import generate_password_hash, check_password_hash


class Admin(db.Model):
    """Administrator model"""
    __tablename__ = 'admins'
    
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    profile_photo = db.Column(db.Text)
    full_name = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(50), default='admin')  # admin, super_admin
    is_active = db.Column(db.Boolean, default=True)
    last_login = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def set_password(self, password):
        """Hash and set password"""
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
        """Verify password"""
        return check_password_hash(self.password_hash, password)
    
    def to_dict(self, include_sensitive=False):
        """Convert admin to dictionary"""
        data = {
            'id': self.id,
            'username': self.username,
            'email': self.email,
            'profile_photo': self.profile_photo,
            'full_name': self.full_name,
            'role': self.role,
            'is_active': self.is_active,
            'last_login': self.last_login.isoformat() if self.last_login else None,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }
        return data
    
    def __repr__(self):
        return f'<Admin {self.username}>'
