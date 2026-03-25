"""Attendance tracking API routes"""
from flask import Blueprint, request, jsonify, session
from flask_jwt_extended import jwt_required
from services.attendance_service import AttendanceService
from utils.station_auth import verify_station
from utils import db as db_utils
from utils import pause_controls
from PIL import Image
import base64
import io
import logging
from datetime import datetime
from time import perf_counter

logger = logging.getLogger(__name__)

attendance_bp = Blueprint('attendance', __name__, url_prefix='/api/attendance')
_attendance_service = None


def _get_attendance_service():
    global _attendance_service
    if _attendance_service is None:
        _attendance_service = AttendanceService()
    return _attendance_service


def decode_image(image_data):
    try:
        if isinstance(image_data, str):
            if ',' in image_data:
                image_data = image_data.split(',')[1]
            image_bytes = base64.b64decode(image_data)
        else:
            image_bytes = image_data
        return Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception as e:
        logger.error(f"Image decode error: {str(e)}")
        return None


@attendance_bp.route('/verify', methods=['POST'])
def verify_attendance():
    """
    Secured verify endpoint for exam stations:
    - Requires X-Station-Key
    - Requires nonce (from /api/attendance/challenge)
    - Optional frames[] + challenge for liveness
    """
    try:
        t0 = perf_counter()
        timings = {}
        def mark(name, start):
            timings[name] = round((perf_counter() - start) * 1000.0, 2)

        t = perf_counter()
        raw_key = (request.headers.get("X-Station-Key") or "").strip()
        ok, station_or_err = verify_station(raw_key, request.remote_addr)
        mark("station_auth_ms", t)
        if not ok:
            return jsonify({"error": station_or_err}), 401
        station = station_or_err

        t = perf_counter()
        data = request.get_json() or {}
        mark("request_json_parse_ms", t)
        if 'live_image' not in data or 'session_id' not in data or 'nonce' not in data:
            return jsonify({'error': 'live_image, session_id, and nonce required'}), 400

        # Validate nonce
        nonce = str(data["nonce"]).strip()
        t = perf_counter()
        ch = db_utils.fetch_one("SELECT * FROM verification_challenges WHERE nonce = %s", (nonce,))
        mark("nonce_lookup_ms", t)
        if not ch:
            return jsonify({"error": "Invalid nonce"}), 400
        if ch["station_id"] != station["id"]:
            return jsonify({"error": "Nonce not issued for this station"}), 400
        if ch["session_id"] != int(data["session_id"]):
            return jsonify({"error": "Nonce not issued for this session"}), 400
        if ch["used_at"] is not None:
            return jsonify({"error": "Nonce already used"}), 400
        if datetime.utcnow() > ch["expires_at"]:
            return jsonify({"error": "Nonce expired"}), 400

        t = perf_counter()
        session_row = db_utils.fetch_one(
            "SELECT id, hall_id FROM examination_sessions WHERE id = %s",
            (int(data["session_id"]),),
        )
        mark("session_lookup_ms", t)
        if not session_row:
            return jsonify({"error": "Session not found"}), 404
        effective_hall_id = station.get("hall_id")
        if effective_hall_id is None:
            effective_hall_id = session_row.get("hall_id")
        if effective_hall_id is not None:
            effective_hall_id = int(effective_hall_id)
        t = perf_counter()
        pause_state = pause_controls.get_pause_state(int(data["session_id"]), effective_hall_id)
        mark("pause_state_ms", t)
        verification_pause = pause_state.get("verification_pause")
        if verification_pause:
            reason = str(verification_pause.get("reason") or "").strip()
            detail = f": {reason}" if reason else ""
            return jsonify({"error": f"Verification is currently paused for this scope{detail}"}), 423

        # Mark nonce used (one-time)
        t = perf_counter()
        db_utils.execute(
            "UPDATE verification_challenges SET used_at = %s WHERE id = %s",
            (datetime.utcnow(), ch["id"])
        )
        mark("nonce_mark_used_ms", t)

        t = perf_counter()
        live_image = decode_image(data['live_image'])
        mark("decode_live_image_ms", t)
        if not live_image:
            return jsonify({'error': 'Invalid image data'}), 400

        frames = []
        if isinstance(data.get("frames"), list):
            t = perf_counter()
            for f in data["frames"][:5]:
                img = decode_image(f)
                if img:
                    frames.append(img)
            mark("decode_frames_ms", t)

        challenge = data.get("challenge")  # should match challenge endpoint response

        ip_address = request.remote_addr
        device_info = request.headers.get('User-Agent', 'Unknown')
        invigilator_id = session.get("admin_id") or data.get("invigilator_id") or request.headers.get("X-Invigilator-Id")

        try:
            invigilator_id = int(invigilator_id) if invigilator_id is not None else None
        except (TypeError, ValueError):
            return jsonify({'error': 'Invalid invigilator identity'}), 400

        attendance_service = _get_attendance_service()
        t = perf_counter()
        success, result, confidence = attendance_service.verify_and_record_attendance(
            live_image=live_image,
            session_id=data['session_id'],
            student_id=data.get('student_id'),
            ip_address=ip_address,
            device_info=device_info,
            frames=frames,
            challenge=challenge,
            station_id=station["id"],
            invigilator_id=invigilator_id
        )
        mark("service_verify_ms", t)
        timings["total_ms"] = round((perf_counter() - t0) * 1000.0, 2)
        logger.info(
            "API verify timing | session=%s | success=%s | total_ms=%s | %s",
            data.get("session_id"),
            bool(success),
            timings.get("total_ms"),
            timings,
        )

        if success:
            return jsonify({
                'message': 'Attendance recorded successfully',
                'attendance': result,
                'confidence': confidence
            }), 200

        return jsonify({'error': result, 'confidence': confidence}), 400

    except Exception as e:
        logger.error(f"Attendance verification API error: {str(e)}")
        return jsonify({'error': 'Verification failed'}), 500


@attendance_bp.route('/session/<int:session_id>', methods=['GET'])
@jwt_required()
def get_session_attendance(session_id):
    try:
        attendance_service = _get_attendance_service()
        result = attendance_service.get_session_attendance(session_id)
        if not result:
            return jsonify({'error': 'Session not found'}), 404
        return jsonify(result), 200
    except Exception as e:
        logger.error(f"Get session attendance API error: {str(e)}")
        return jsonify({'error': 'Failed to retrieve attendance'}), 500


@attendance_bp.route('/student/<student_id>', methods=['GET'])
@jwt_required()
def get_student_attendance(student_id):
    try:
        attendance_service = _get_attendance_service()
        result = attendance_service.get_student_attendance_history(student_id)
        if not result:
            return jsonify({'error': 'Student not found'}), 404
        return jsonify(result), 200
    except Exception as e:
        logger.error(f"Get student attendance API error: {str(e)}")
        return jsonify({'error': 'Failed to retrieve attendance history'}), 500
