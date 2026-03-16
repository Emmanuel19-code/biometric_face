"""Student registration API routes"""
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from services.student_service import StudentService
from PIL import Image
import base64
import io
import logging

logger = logging.getLogger(__name__)

students_bp = Blueprint('students', __name__, url_prefix='/api/students')
_student_service = None


def _get_student_service():
    global _student_service
    if _student_service is None:
        _student_service = StudentService()
    return _student_service


def decode_image(image_data):
    """Decode base64 image data"""
    try:
        if isinstance(image_data, str):
            # Remove data URL prefix if present
            if ',' in image_data:
                image_data = image_data.split(',')[1]
            image_bytes = base64.b64decode(image_data)
        else:
            image_bytes = image_data
        
        return Image.open(io.BytesIO(image_bytes))
    except Exception as e:
        logger.error(f"Image decode error: {str(e)}")
        return None


@students_bp.route('/register', methods=['POST'])
@jwt_required()
def register_student():
    """Register a new student with facial biometric data"""
    try:
        data = request.get_json()
        
        # Validate required fields
        required_fields = ['first_name', 'last_name', 'email', 'face_images']
        for field in required_fields:
            if field not in data:
                return jsonify({'error': f'Missing required field: {field}'}), 400
        
        # Decode face images
        face_images = []
        for img_data in data['face_images']:
            image = decode_image(img_data)
            if not image:
                return jsonify({'error': 'Invalid image data'}), 400
            face_images.append(image)
        
        student_service = _get_student_service()

        # Register student
        success, result = student_service.register_student(
            {
                'student_id': data.get('student_id'),
                'first_name': data['first_name'],
                'last_name': data['last_name'],
                'email': data['email'],
                'phone': data.get('phone'),
                'department': data.get('department'),
                'course': data.get('course'),
                'year_level': data.get('year_level')
            },
            face_images
        )
        
        if success:
            temp_password = result.get("temporary_password") if isinstance(result, dict) else None
            return jsonify({
                'message': 'Student registered successfully',
                'student': result,
                'temporary_password': temp_password
            }), 201
        else:
            return jsonify({'error': result}), 400
    
    except Exception as e:
        logger.error(f"Student registration API error: {str(e)}")
        return jsonify({'error': 'Registration failed'}), 500


@students_bp.route('/<student_id>', methods=['GET'])
@jwt_required()
def get_student(student_id):
    """Get student details"""
    try:
        student_service = _get_student_service()
        student = student_service.get_student(student_id)
        
        if not student:
            return jsonify({'error': 'Student not found'}), 404
        
        return jsonify({'student': student_service._student_to_dict(student, include_encodings=True)}), 200
    
    except Exception as e:
        logger.error(f"Get student API error: {str(e)}")
        return jsonify({'error': 'Failed to retrieve student'}), 500


@students_bp.route('', methods=['GET'])
@jwt_required()
def list_students():
    """List all students"""
    try:
        active_only = request.args.get('active_only', 'true').lower() == 'true'
        student_service = _get_student_service()
        students = student_service.get_all_students(active_only=active_only)
        
        return jsonify({
            'students': [student_service._student_to_dict(s) for s in students],
            'total': len(students)
        }), 200
    
    except Exception as e:
        logger.error(f"List students API error: {str(e)}")
        return jsonify({'error': 'Failed to retrieve students'}), 500


@students_bp.route('/<student_id>', methods=['PUT'])
@jwt_required()
def update_student(student_id):
    """Update student information"""
    try:
        data = request.get_json()
        
        # Remove fields that shouldn't be updated
        data.pop('id', None)
        data.pop('face_encodings', None)
        data.pop('registration_date', None)
        
        student_service = _get_student_service()
        success, result = student_service.update_student(student_id, data)
        
        if success:
            return jsonify({
                'message': 'Student updated successfully',
                'student': result
            }), 200
        else:
            return jsonify({'error': result}), 400
    
    except Exception as e:
        logger.error(f"Update student API error: {str(e)}")
        return jsonify({'error': 'Update failed'}), 500


@students_bp.route('/<student_id>/deactivate', methods=['POST'])
@jwt_required()
def deactivate_student(student_id):
    """Deactivate a student"""
    try:
        student_service = _get_student_service()
        success, result = student_service.deactivate_student(student_id)
        
        if success:
            return jsonify({
                'message': 'Student deactivated successfully',
                'student': result
            }), 200
        else:
            return jsonify({'error': result}), 400
    
    except Exception as e:
        logger.error(f"Deactivate student API error: {str(e)}")
        return jsonify({'error': 'Deactivation failed'}), 500
