"""Authentication API routes"""
from flask import Blueprint, request, jsonify
from flask_jwt_extended import create_access_token, create_refresh_token, jwt_required, get_jwt_identity
from services.admin_service import AdminService
import logging

logger = logging.getLogger(__name__)

auth_bp = Blueprint('auth', __name__, url_prefix='/api/auth')
admin_service = AdminService()


@auth_bp.route('/login', methods=['POST'])
def login():
    """Admin login endpoint"""
    try:
        data = request.get_json()
        
        if not data or not data.get('username') or not data.get('password'):
            return jsonify({'error': 'Username and password required'}), 400
        
        success, admin = admin_service.authenticate_admin(
            data['username'],
            data['password']
        )
        
        if not success:
            return jsonify({'error': 'Invalid credentials'}), 401
        
        # Create tokens
        access_token = create_access_token(identity=str(admin["id"]))
        refresh_token = create_refresh_token(identity=str(admin["id"]))
        
        return jsonify({
            'access_token': access_token,
            'refresh_token': refresh_token,
            'admin': admin
        }), 200
    
    except Exception as e:
        logger.error(f"Login error: {str(e)}")
        return jsonify({'error': 'Login failed'}), 500


@auth_bp.route('/refresh', methods=['POST'])
@jwt_required(refresh=True)
def refresh():
    """Refresh access token"""
    try:
        admin_id = str(get_jwt_identity())
        access_token = create_access_token(identity=admin_id)
        
        return jsonify({'access_token': access_token}), 200
    
    except Exception as e:
        logger.error(f"Token refresh error: {str(e)}")
        return jsonify({'error': 'Token refresh failed'}), 500
