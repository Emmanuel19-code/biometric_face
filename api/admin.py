"""Administrator dashboard API routes"""
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from services.admin_service import AdminService
from services.attendance_service import AttendanceService
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

admin_bp = Blueprint('admin', __name__, url_prefix='/api/admin')
admin_service = AdminService()
attendance_service = AttendanceService()


@admin_bp.route('/stats', methods=['GET'])
@jwt_required()
def get_stats():
    """Get system statistics"""
    try:
        stats = admin_service.get_system_stats()
        return jsonify(stats), 200
    
    except Exception as e:
        logger.error(f"Get stats API error: {str(e)}")
        return jsonify({'error': 'Failed to retrieve statistics'}), 500


@admin_bp.route('/sessions', methods=['POST'])
@jwt_required()
def create_session():
    """Create a new examination session"""
    try:
        data = request.get_json()
        
        required_fields = ['session_name', 'start_time', 'end_time']
        for field in required_fields:
            if field not in data:
                return jsonify({'error': f'Missing required field: {field}'}), 400
        
        admin_id = int(get_jwt_identity())
        success, result = attendance_service.create_session(data, admin_id)
        
        if success:
            return jsonify({
                'message': 'Session created successfully',
                'session': result
            }), 201
        else:
            return jsonify({'error': result}), 400
    
    except Exception as e:
        logger.error(f"Create session API error: {str(e)}")
        return jsonify({'error': 'Session creation failed'}), 500


@admin_bp.route('/sessions', methods=['GET'])
@jwt_required()
def list_sessions():
    """List all examination sessions"""
    try:
        active_only = request.args.get('active_only', 'false').lower() == 'true'
        sessions = attendance_service.get_all_sessions(active_only=active_only)
        
        return jsonify({
            'sessions': sessions,
            'total': len(sessions)
        }), 200
    
    except Exception as e:
        logger.error(f"List sessions API error: {str(e)}")
        return jsonify({'error': 'Failed to retrieve sessions'}), 500


@admin_bp.route('/sessions/<int:session_id>', methods=['GET'])
@jwt_required()
def get_session(session_id):
    """Get session details"""
    try:
        sessions = attendance_service.get_all_sessions(active_only=False)
        session = next((s for s in sessions if s.get("id") == session_id), None)
        
        if not session:
            return jsonify({'error': 'Session not found'}), 404
        
        return jsonify({
            'session': session,
            'papers': attendance_service.get_session_papers(session["id"]),
            'invigilators': attendance_service.get_session_invigilators(session["id"])
        }), 200
    
    except Exception as e:
        logger.error(f"Get session API error: {str(e)}")
        return jsonify({'error': 'Failed to retrieve session'}), 500


@admin_bp.route('/invigilators', methods=['POST'])
@jwt_required()
def create_invigilator():
    """Create an invigilator account."""
    try:
        data = request.get_json() or {}
        required_fields = ['username', 'email', 'full_name', 'password']
        for field in required_fields:
            if not data.get(field):
                return jsonify({'error': f'Missing required field: {field}'}), 400

        payload = {
            'username': data['username'],
            'email': data['email'],
            'full_name': data['full_name'],
            'password': data['password'],
            'role': 'invigilator'
        }
        success, result = admin_service.create_admin(payload)
        if not success:
            return jsonify({'error': result}), 400
        return jsonify({'message': 'Invigilator created', 'invigilator': result}), 201
    except Exception as e:
        logger.error(f"Create invigilator API error: {str(e)}")
        return jsonify({'error': 'Failed to create invigilator'}), 500


@admin_bp.route('/invigilators', methods=['GET'])
@jwt_required()
def list_invigilators():
    """List active invigilators."""
    try:
        from utils import db as db_utils
        invigilators = db_utils.fetch_all(
            "SELECT * FROM admins WHERE role = 'invigilator' AND is_active = TRUE ORDER BY full_name ASC"
        )
        def fmt(row):
            return {
                "id": row.get("id"),
                "username": row.get("username"),
                "email": row.get("email"),
                "full_name": row.get("full_name"),
                "role": row.get("role"),
                "is_active": row.get("is_active"),
                "last_login": row.get("last_login").isoformat() if row.get("last_login") else None,
                "created_at": row.get("created_at").isoformat() if row.get("created_at") else None
            }
        return jsonify({'invigilators': [fmt(i) for i in invigilators], 'total': len(invigilators)}), 200
    except Exception as e:
        logger.error(f"List invigilators API error: {str(e)}")
        return jsonify({'error': 'Failed to retrieve invigilators'}), 500


@admin_bp.route('/sessions/<int:session_id>/papers', methods=['POST'])
@jwt_required()
def set_session_papers(session_id):
    """Set papers for a session (replace all)."""
    try:
        data = request.get_json() or {}
        papers = data.get('papers')
        if not isinstance(papers, list):
            return jsonify({'error': 'papers must be a list'}), 400
        success, result = attendance_service.set_session_papers(session_id, papers)
        if not success:
            return jsonify({'error': result}), 400
        return jsonify({'message': 'Session papers updated', 'papers': result}), 200
    except Exception as e:
        logger.error(f"Set session papers API error: {str(e)}")
        return jsonify({'error': 'Failed to set session papers'}), 500


@admin_bp.route('/sessions/<int:session_id>/papers', methods=['GET'])
@jwt_required()
def get_session_papers(session_id):
    """Get session papers."""
    try:
        return jsonify({'papers': attendance_service.get_session_papers(session_id)}), 200
    except Exception as e:
        logger.error(f"Get session papers API error: {str(e)}")
        return jsonify({'error': 'Failed to retrieve session papers'}), 500


@admin_bp.route('/sessions/<int:session_id>/invigilators', methods=['POST'])
@jwt_required()
def assign_session_invigilators(session_id):
    """Assign invigilators to a session (replace all)."""
    try:
        data = request.get_json() or {}
        invigilator_ids = data.get('invigilator_ids')
        if not isinstance(invigilator_ids, list):
            return jsonify({'error': 'invigilator_ids must be a list'}), 400

        admin_id = int(get_jwt_identity())
        success, result = attendance_service.assign_invigilators(
            session_id=session_id,
            invigilator_ids=invigilator_ids,
            assigned_by=admin_id
        )
        if not success:
            return jsonify({'error': result}), 400
        return jsonify({'message': 'Session invigilators updated', 'result': result}), 200
    except Exception as e:
        logger.error(f"Assign session invigilators API error: {str(e)}")
        return jsonify({'error': 'Failed to assign session invigilators'}), 500


@admin_bp.route('/sessions/<int:session_id>/invigilators', methods=['GET'])
@jwt_required()
def get_session_invigilators(session_id):
    """Get invigilators assigned to a session."""
    try:
        assignments = attendance_service.get_session_invigilators(session_id)
        return jsonify({'invigilators': assignments, 'total': len(assignments)}), 200
    except Exception as e:
        logger.error(f"Get session invigilators API error: {str(e)}")
        return jsonify({'error': 'Failed to retrieve session invigilators'}), 500


@admin_bp.route('/sessions/<int:session_id>/registrations', methods=['POST'])
@jwt_required()
def register_students_for_session(session_id):
    """Register students for a specific exam session."""
    try:
        data = request.get_json() or {}
        student_ids = data.get('student_ids', [])
        if not isinstance(student_ids, list) or len(student_ids) == 0:
            return jsonify({'error': 'student_ids must be a non-empty list'}), 400

        admin_id = int(get_jwt_identity())
        success, result = attendance_service.register_students_for_session(
            session_id=session_id,
            student_identifiers=student_ids,
            registered_by=admin_id
        )
        if not success:
            return jsonify({'error': result}), 400

        return jsonify({
            'message': 'Session registrations updated',
            'result': result
        }), 200
    except Exception as e:
        logger.error(f"Register students for session API error: {str(e)}")
        return jsonify({'error': 'Failed to register students for session'}), 500


@admin_bp.route('/sessions/<int:session_id>/registrations', methods=['GET'])
@jwt_required()
def get_session_registrations(session_id):
    """Get official registration list for a session."""
    try:
        result = attendance_service.get_session_registrations(session_id)
        if result is None:
            return jsonify({'error': 'Session not found'}), 404
        return jsonify(result), 200
    except Exception as e:
        logger.error(f"Get session registrations API error: {str(e)}")
        return jsonify({'error': 'Failed to retrieve session registrations'}), 500


@admin_bp.route('/sessions/<int:session_id>/registrations/<student_identifier>', methods=['DELETE'])
@jwt_required()
def remove_session_registration(session_id, student_identifier):
    """Remove a student from the registration list of a session."""
    try:
        success, result = attendance_service.remove_student_registration(session_id, student_identifier)
        if not success:
            return jsonify({'error': result}), 404
        return jsonify({
            'message': 'Student removed from session registration',
            'registration': result
        }), 200
    except Exception as e:
        logger.error(f"Remove session registration API error: {str(e)}")
        return jsonify({'error': 'Failed to remove session registration'}), 500


@admin_bp.route('/reports/attendance', methods=['GET'])
@jwt_required()
def generate_attendance_report():
    """Generate attendance report"""
    try:
        session_id = request.args.get('session_id', type=int)
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        student_id = request.args.get('student_id', type=int)
        
        # Parse dates
        start_date_obj = None
        end_date_obj = None
        
        if start_date:
            try:
                start_date_obj = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
            except:
                return jsonify({'error': 'Invalid start_date format'}), 400
        
        if end_date:
            try:
                end_date_obj = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
            except:
                return jsonify({'error': 'Invalid end_date format'}), 400
        
        report = admin_service.generate_attendance_report(
            session_id=session_id,
            start_date=start_date_obj,
            end_date=end_date_obj,
            student_id=student_id
        )
        
        if report is None:
            return jsonify({'error': 'Failed to generate report'}), 500
        
        return jsonify(report), 200
    
    except Exception as e:
        logger.error(f"Generate report API error: {str(e)}")
        return jsonify({'error': 'Report generation failed'}), 500


@admin_bp.route('/sessions/<int:session_id>/end', methods=['POST'])
@jwt_required()
def end_session(session_id):
    """End an exam session immediately."""
    try:
        success, result = attendance_service.end_session(session_id)
        if not success:
            return jsonify({'error': result}), 404
        return jsonify({
            'message': 'Session ended successfully',
            'session': result
        }), 200
    except Exception as e:
        logger.error(f"End session API error: {str(e)}")
        return jsonify({'error': 'Failed to end session'}), 500


@admin_bp.route('/sessions/<int:session_id>/start', methods=['POST'])
@jwt_required()
def start_session(session_id):
    """Start an exam session manually."""
    try:
        success, result = attendance_service.start_session(session_id)
        if not success:
            return jsonify({'error': result}), 400
        return jsonify({
            'message': 'Session started successfully',
            'session': result
        }), 200
    except Exception as e:
        logger.error(f"Start session API error: {str(e)}")
        return jsonify({'error': 'Failed to start session'}), 500
