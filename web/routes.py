from functools import wraps
import secrets
import base64
import io
import logging
from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, session, jsonify
from services.admin_service import AdminService
from services.attendance_service import AttendanceService
from services.student_service import StudentService
from utils import db as db_utils
from werkzeug.security import generate_password_hash
from PIL import Image

web_bp = Blueprint("web", __name__)
admin_service = AdminService()
attendance_service = AttendanceService()
student_service = StudentService()
logger = logging.getLogger(__name__)


def _decode_image(image_data):
    try:
        if isinstance(image_data, str):
            if "," in image_data:
                image_data = image_data.split(",", 1)[1]
            image_bytes = base64.b64decode(image_data)
        else:
            image_bytes = image_data
        return Image.open(io.BytesIO(image_bytes))
    except Exception as exc:
        logger.error(f"Web student image decode error: {exc}")
        return None


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin_id"):
            return redirect(url_for("web.login_page"))
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if session.get("role") != "super_admin":
            return redirect(url_for("web.dashboard_page"))
        return fn(*args, **kwargs)
    return wrapper


@web_bp.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "GET":
        return render_template("auth/login.html")

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""

    if not username or not password:
        return render_template("auth/login.html", error="Username and password are required"), 400

    ok, admin = admin_service.authenticate_admin(username, password)
    if not ok:
        return render_template("auth/login.html", error="Invalid credentials"), 401

    session["admin_id"] = admin["id"]
    session["role"] = admin["role"]
    session["user_email"] = admin["email"]
    return redirect(url_for("web.dashboard_page"))


@web_bp.get("/dashboard")
@login_required
def dashboard_page():
    students = db_utils.fetch_one("SELECT COUNT(*) AS c FROM students")["c"]
    sessions = db_utils.fetch_one(
        "SELECT COUNT(*) AS c FROM examination_sessions WHERE DATE(start_time) = CURRENT_DATE"
    )["c"]
    verified = db_utils.fetch_one(
        "SELECT COUNT(*) AS c FROM attendances WHERE DATE(timestamp) = CURRENT_DATE"
    )["c"]
    failed = db_utils.fetch_one(
        "SELECT COUNT(*) AS c FROM verification_logs WHERE outcome = 'FAIL' AND DATE(timestamp) = CURRENT_DATE"
    )["c"]
    stats = {"students": students, "sessions": sessions, "verified": verified, "failed": failed}

    recent_rows = db_utils.fetch_all(
        """
        SELECT
            vl.timestamp,
            vl.outcome,
            vl.reason,
            vl.confidence,
            COALESCE(NULLIF(TRIM(COALESCE(s.first_name, '') || ' ' || COALESCE(s.last_name, '')), ''), 'Unknown Student') AS student_name,
            COALESCE(s.student_id, vl.claimed_student_id, 'N/A') AS student_index,
            COALESCE(es.course_code, 'N/A') AS course_code
        FROM verification_logs vl
        LEFT JOIN students s ON s.id = vl.student_id
        LEFT JOIN examination_sessions es ON es.id = vl.session_id
        ORDER BY vl.timestamp DESC
        LIMIT 20
        """
    )
    recent_activities = [
        {
            "time": r["timestamp"].strftime("%H:%M:%S") if r.get("timestamp") else "N/A",
            "name": r.get("student_name") or "Unknown Student",
            "index": r.get("student_index") or "N/A",
            "course": r.get("course_code") or "N/A",
            "status": "Verified" if str(r.get("outcome") or "").upper() == "SUCCESS" else "Failed",
            "reason": r.get("reason") or ""
        }
        for r in recent_rows
    ]
    return render_template("dashboard/index.html", stats=stats, recent_activities=recent_activities)


@web_bp.get("/students/register")
@login_required
def register_student_page():
    return render_template("students/register.html")


@web_bp.post("/students/register")
@login_required
def register_student_web():
    data = request.get_json() or {}
    required = ["student_id", "full_name", "email", "course", "year_level", "face_images"]
    for field in required:
        if not data.get(field):
            return jsonify({"error": f"Missing required field: {field}"}), 400

    if not isinstance(data.get("face_images"), list) or len(data["face_images"]) < 2:
        return jsonify({"error": "At least 2 face captures are required"}), 400

    name_parts = [p for p in str(data["full_name"]).strip().split(" ") if p]
    first_name = name_parts[0] if name_parts else ""
    last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else first_name

    face_images = []
    for img_data in data["face_images"]:
        image = _decode_image(img_data)
        if image is None:
            return jsonify({"error": "Invalid image data in face capture"}), 400
        face_images.append(image)

    success, result = student_service.register_student(
        {
            "student_id": str(data["student_id"]).strip(),
            "first_name": first_name,
            "last_name": last_name,
            "email": str(data["email"]).strip().lower(),
            "phone": (data.get("phone") or "").strip() or None,
            "department": (data.get("department") or "").strip() or None,
            "course": str(data["course"]).strip(),
            "year_level": str(data["year_level"]).strip(),
        },
        face_images
    )
    if not success:
        return jsonify({"error": result}), 400
    return jsonify({"message": "Student registered successfully", "student": result}), 201


@web_bp.get("/students")
@login_required
def students_directory_page():
    return render_template("students/students_directory.html")


@web_bp.get("/students/data")
@login_required
def students_directory_data():
    active_only = request.args.get("active_only", "true").lower() == "true"
    students = student_service.get_all_students(active_only=active_only)
    payload = [student_service._student_to_dict(s) for s in students]
    return jsonify({"students": payload, "total": len(payload)}), 200


@web_bp.get("/exams/session")
@login_required
def exam_session_page():
    sessions = db_utils.fetch_all(
        "SELECT * FROM examination_sessions ORDER BY start_time DESC"
    )
    now = datetime.utcnow()

    def session_status(s):
        if not s.get("is_active"):
            return "inactive"
        if s.get("end_time") and s["end_time"] < now:
            return "ended"
        if s.get("start_time") and s["start_time"] > now:
            return "upcoming"
        return "live"

    session_id = request.args.get("session_id", type=int)

    selected = None
    if session_id:
        selected = next((s for s in sessions if s.get("id") == session_id), None)
    elif sessions:
        selected = next((s for s in sessions if session_status(s) == "live"), None) or sessions[0]

    registered = []
    if selected:
        regs = db_utils.fetch_all(
            """
            SELECT s.student_id, s.first_name, s.last_name
            FROM exam_registrations r
            JOIN students s ON s.id = r.student_id
            WHERE r.session_id = %s
            """,
            (selected["id"],)
        )
        registered = [
            {"index": r["student_id"], "name": f"{r['first_name']} {r['last_name']}"}
            for r in regs
        ]

    sessions_view = [
        {
            "id": s["id"],
            "course": s.get("course_code") or "N/A",
            "time": f"{s['start_time'].strftime('%Y-%m-%d %H:%M')} - {s['end_time'].strftime('%H:%M')}",
            "venue": s.get("venue") or "N/A",
            "title": s.get("session_name"),
            "status": session_status(s),
            "is_active": s.get("is_active")
        }
        for s in sessions
    ]

    selected_view = None
    if selected:
        selected_view = {
            "id": selected["id"],
            "course": selected.get("course_code") or "N/A",
            "time": f"{selected['start_time'].strftime('%Y-%m-%d %H:%M')} - {selected['end_time'].strftime('%H:%M')}",
            "venue": selected.get("venue") or "N/A",
            "title": selected.get("session_name"),
            "status": session_status(selected),
            "is_active": selected.get("is_active")
        }

    return render_template(
        "exams/session.html",
        sessions=sessions_view,
        selected=selected_view,
        registered=registered
    )


@web_bp.get("/exams/sessions")
@login_required
def all_sessions_page():
    sessions = db_utils.fetch_all(
        """
        SELECT
            es.*,
            COALESCE(reg.reg_count, 0) AS registered_count,
            COALESCE(att.att_count, 0) AS verified_count,
            COALESCE(inv.inv_count, 0) AS invigilator_count,
            CASE
                WHEN es.is_active = FALSE THEN 'Inactive'
                WHEN es.end_time < NOW() THEN 'Ended'
                WHEN es.start_time > NOW() THEN 'Upcoming'
                ELSE 'Live'
            END AS status
        FROM examination_sessions es
        LEFT JOIN (
            SELECT session_id, COUNT(*) AS reg_count
            FROM exam_registrations
            GROUP BY session_id
        ) reg ON reg.session_id = es.id
        LEFT JOIN (
            SELECT session_id, COUNT(*) AS att_count
            FROM attendances
            GROUP BY session_id
        ) att ON att.session_id = es.id
        LEFT JOIN (
            SELECT session_id, COUNT(*) AS inv_count
            FROM session_invigilators
            WHERE is_active = TRUE
            GROUP BY session_id
        ) inv ON inv.session_id = es.id
        ORDER BY es.start_time DESC
        """
    )
    return render_template("exams/sessions_list.html", sessions=sessions)


@web_bp.post("/admin/sessions/<int:session_id>/start")
@login_required
@admin_required
def start_session_web(session_id):
    success, result = attendance_service.start_session(session_id)
    if not success:
        return jsonify({"error": result}), 400
    return jsonify({"message": "Session started", "session": result}), 200


@web_bp.post("/admin/sessions/<int:session_id>/end")
@login_required
@admin_required
def end_session_web(session_id):
    success, result = attendance_service.end_session(session_id)
    if not success:
        return jsonify({"error": result}), 400
    return jsonify({"message": "Session ended", "session": result}), 200


@web_bp.get("/attendance/logs")
@login_required
def attendance_logs_page():
    return render_template("attendance/logs.html")


@web_bp.post("/stations/auto-key")
@login_required
def auto_station_key():
    """Create (or reuse from session) a station key for the selected session/hall."""
    data = request.get_json() or {}
    session_id = data.get("session_id")
    try:
        session_id = int(session_id)
    except (TypeError, ValueError):
        return jsonify({"error": "Valid session_id is required"}), 400

    exam_session = db_utils.fetch_one(
        "SELECT id FROM examination_sessions WHERE id = %s",
        (session_id,)
    )
    if not exam_session:
        return jsonify({"error": "Session not found"}), 404

    admin_id = session.get("admin_id")
    if not admin_id:
        return jsonify({"error": "Unauthorized"}), 401

    key_cache = session.get("station_keys", {})
    cache_key = str(session_id)
    if cache_key in key_cache and key_cache[cache_key]:
        return jsonify({"api_key": key_cache[cache_key], "auto_created": False}), 200

    station_name = f"AUTO-S{session_id}-A{admin_id}"
    raw_key = secrets.token_urlsafe(32)
    existing = db_utils.fetch_one(
        "SELECT id FROM exam_stations WHERE name = %s AND is_active = TRUE",
        (station_name,)
    )
    if existing:
        db_utils.execute("UPDATE exam_stations SET is_active = FALSE WHERE id = %s", (existing["id"],))

    db_utils.execute(
        """
        INSERT INTO exam_stations (name, api_key_hash, ip_whitelist, is_active)
        VALUES (%s, %s, %s, TRUE)
        """,
        (station_name, generate_password_hash(raw_key), None)
    )

    key_cache[cache_key] = raw_key
    session["station_keys"] = key_cache
    session.modified = True

    return jsonify({"api_key": raw_key, "auto_created": True}), 201


@web_bp.get("/admin/invigilators/new")
@login_required
@admin_required
def add_invigilator_page():
    invigilators = db_utils.fetch_all(
        "SELECT * FROM admins WHERE role = 'invigilator' ORDER BY full_name ASC"
    )
    return render_template("add_invigilator.html", invigilators=invigilators)


@web_bp.post("/admin/invigilators/new")
@login_required
@admin_required
def create_invigilator_web():
    data = request.get_json() or request.form.to_dict()

    username = (data.get("username") or data.get("email") or "").strip().lower()
    email = (data.get("email") or "").strip().lower()
    full_name = (data.get("full_name") or "").strip()
    password = data.get("password") or ""

    if not username or not email or not full_name or not password:
        return jsonify({"error": "username, email, full_name, and password are required"}), 400

    payload = {
        "username": username,
        "email": email,
        "full_name": full_name,
        "password": password,
        "role": "invigilator"
    }
    success, result = admin_service.create_admin(payload)
    if not success:
        return jsonify({"error": result}), 400
    return jsonify({"message": "Invigilator created", "invigilator": result}), 201


@web_bp.get("/admin/sessions/setup")
@login_required
@admin_required
def session_setup_page():
    sessions = db_utils.fetch_all(
        "SELECT * FROM examination_sessions ORDER BY start_time DESC"
    )
    invigilators = db_utils.fetch_all(
        "SELECT * FROM admins WHERE role = 'invigilator' AND is_active = TRUE ORDER BY full_name ASC"
    )
    return render_template(
        "admin/session_setup.html",
        sessions=sessions,
        invigilators=invigilators
    )


@web_bp.get("/admin/sessions/<int:session_id>/setup-data")
@login_required
@admin_required
def session_setup_data(session_id):
    sess = db_utils.fetch_one(
        "SELECT * FROM examination_sessions WHERE id = %s",
        (session_id,)
    )
    if not sess:
        return jsonify({"error": "Session not found"}), 404

    return jsonify({
        "session": attendance_service._session_to_dict(sess),
        "papers": attendance_service.get_session_papers(session_id),
        "invigilators": attendance_service.get_session_invigilators(session_id)
    }), 200


@web_bp.post("/admin/sessions/setup/create")
@login_required
@admin_required
def session_setup_create():
    data = request.get_json() or {}
    required_fields = ["session_name", "start_time", "end_time"]
    for field in required_fields:
        if not data.get(field):
            return jsonify({"error": f"Missing required field: {field}"}), 400

    papers = data.get("papers", [])
    invigilator_ids = data.get("invigilator_ids", [])

    payload = {
        "session_name": data.get("session_name"),
        "course_code": data.get("course_code"),
        "venue": data.get("venue"),
        "start_time": data.get("start_time"),
        "end_time": data.get("end_time"),
        "papers": papers if isinstance(papers, list) else [],
        "invigilator_ids": invigilator_ids if isinstance(invigilator_ids, list) else []
    }
    created_by = int(session.get("admin_id"))
    success, result = attendance_service.create_session(payload, created_by)
    if not success:
        return jsonify({"error": result}), 400
    return jsonify({"message": "Session created", "session": result}), 201


@web_bp.post("/admin/sessions/<int:session_id>/setup-data/papers")
@login_required
@admin_required
def session_setup_set_papers(session_id):
    data = request.get_json() or {}
    papers = data.get("papers", [])
    if not isinstance(papers, list):
        return jsonify({"error": "papers must be a list"}), 400
    success, result = attendance_service.set_session_papers(session_id, papers)
    if not success:
        return jsonify({"error": result}), 400
    return jsonify({"message": "Papers updated", "papers": result}), 200


@web_bp.post("/admin/sessions/<int:session_id>/setup-data/invigilators")
@login_required
@admin_required
def session_setup_set_invigilators(session_id):
    data = request.get_json() or {}
    invigilator_ids = data.get("invigilator_ids", [])
    if not isinstance(invigilator_ids, list):
        return jsonify({"error": "invigilator_ids must be a list"}), 400
    success, result = attendance_service.assign_invigilators(
        session_id=session_id,
        invigilator_ids=invigilator_ids,
        assigned_by=int(session.get("admin_id"))
    )
    if not success:
        return jsonify({"error": result}), 400
    return jsonify({"message": "Invigilators updated", "result": result}), 200


@web_bp.get("/logout")
def logout_page():
    session.clear()
    return redirect(url_for("web.login_page"))
