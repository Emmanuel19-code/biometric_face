from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required
from utils import db as db_utils

logs_bp = Blueprint("logs", __name__, url_prefix="/api/admin/logs")


def _fmt_dt(value):
    return value.isoformat() if value else None


def _student_from_row(row, prefix):
    student_db_id = row.get(f"{prefix}id")
    if student_db_id is None:
        return None
    return {
        "id": student_db_id,
        "student_id": row.get(f"{prefix}student_id"),
        "first_name": row.get(f"{prefix}first_name"),
        "last_name": row.get(f"{prefix}last_name"),
        "email": row.get(f"{prefix}email"),
        "phone": row.get(f"{prefix}phone"),
        "department": row.get(f"{prefix}department"),
        "course": row.get(f"{prefix}course"),
        "year_level": row.get(f"{prefix}year_level"),
        "is_active": row.get(f"{prefix}is_active"),
    }


def _log_to_dict(row):
    return {
        "id": row.get("id"),
        "session_id": row.get("session_id"),
        "student_id": row.get("student_id"),
        "claimed_student_id": row.get("claimed_student_id"),
        "timestamp": _fmt_dt(row.get("timestamp")),
        "outcome": row.get("outcome"),
        "reason": row.get("reason"),
        "confidence": row.get("confidence"),
        "ip_address": row.get("ip_address"),
        "device_info": row.get("device_info"),
        "station_id": row.get("station_id"),
        "student": _student_from_row(row, "s_"),
        "claimed_student": _student_from_row(row, "cs_")
    }


@logs_bp.get("/verifications")
@jwt_required()
def list_verification_logs():
    session_id = request.args.get("session_id", type=int)
    outcome = request.args.get("outcome")  # SUCCESS / FAIL
    limit = min(request.args.get("limit", 200, type=int), 1000)

    clauses = []
    params = []
    if session_id:
        clauses.append("v.session_id = %s")
        params.append(session_id)
    if outcome:
        clauses.append("v.outcome = %s")
        params.append(outcome)

    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    sql = f"""
        SELECT
            v.*,
            s.id AS s_id, s.student_id AS s_student_id, s.first_name AS s_first_name,
            s.last_name AS s_last_name, s.email AS s_email, s.phone AS s_phone,
            s.department AS s_department, s.course AS s_course, s.year_level AS s_year_level,
            s.is_active AS s_is_active,
            cs.id AS cs_id, cs.student_id AS cs_student_id, cs.first_name AS cs_first_name,
            cs.last_name AS cs_last_name, cs.email AS cs_email, cs.phone AS cs_phone,
            cs.department AS cs_department, cs.course AS cs_course, cs.year_level AS cs_year_level,
            cs.is_active AS cs_is_active
        FROM verification_logs v
        LEFT JOIN students s ON s.id = v.student_id
        LEFT JOIN students cs ON cs.student_id = v.claimed_student_id
        {where}
        ORDER BY v.timestamp DESC
        LIMIT %s
    """
    params.append(limit)

    logs = db_utils.fetch_all(sql, tuple(params))
    return jsonify({"logs": [_log_to_dict(l) for l in logs], "count": len(logs)}), 200
