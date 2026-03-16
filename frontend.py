from flask import Flask, render_template, redirect, url_for, request, session, abort, jsonify
from functools import wraps
from datetime import date, datetime, timedelta
from collections import defaultdict
from werkzeug.routing import BuildError

app = Flask(__name__)
app.secret_key = "dev-secret-change-this"


@app.context_processor
def inject_template_helpers():
    fallback_paths = {
        "web.dashboard_page": "/dashboard",
        "web.register_student_page": "/students/register",
        "web.students_directory_page": "/students",
        "web.exam_session_page": "/exams/session",
        "web.verification_test_page": "/verify/test",
        "web.all_sessions_page": "/exams/sessions",
        "web.attendance_logs_page": "/attendance/logs",
        "web.session_setup_page": "/admin/session-setup",
        "web.add_invigilator_page": "/admin/invigilators/new",
        "web.academic_years_page": "/admin/academic-years/manage",
        "web.semester_control_page": "/admin/semester-control",
        "web.logout_page": "/logout",
        "web.login_page": "/login",
    }

    def safe_url_for(endpoint, **values):
        candidates = [endpoint]
        if endpoint.startswith("web."):
            stripped = endpoint.split(".", 1)[1]
            candidates.append(stripped)
            if stripped.endswith("_page"):
                candidates.append(stripped[:-5])

        for candidate in candidates:
            try:
                return url_for(candidate, **values)
            except BuildError:
                continue
        return fallback_paths.get(endpoint)
    return {"safe_url_for": safe_url_for}

SESSIONS = [
    {"id": "S1", "course": "ITM 401", "title": "Information Security", "time": "09:00 - 12:00", "venue": "Hall A"},
    {"id": "S2", "course": "MIS 203", "title": "Systems Analysis", "time": "13:00 - 16:00", "venue": "Auditorium"},
]

REGISTRATIONS = {
    "S1": [
        {"index": "10300087", "name": "Asare Ophielia", "program": "BSc ITM", "level": "400"},
        {"index": "10299663", "name": "Koranteng Joshua", "program": "BSc ITM", "level": "400"},
    ],
    "S2": [
        {"index": "10298741", "name": "Tetteh Darpoh Lemuel", "program": "BSc ITM", "level": "200"},
    ]
}

def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if session.get("role") != "admin":
            abort(403)
        return fn(*args, **kwargs)
    return wrapper


@app.get("/")
def home():
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("auth/login.html")  

    # FRONTEND-ONLY DEMO: accept any credentials
    email = request.form.get("email", "").strip().lower()
    role = request.form.get("role", "invigilator").strip().lower()

    if role not in ("admin", "invigilator"):
        role = "invigilator"

    session["user_email"] = email or "demo@upsa.edu.gh"
    session["role"] = role

    return redirect(url_for("dashboard"))


@app.get("/dashboard")
def dashboard():
    stats = {"students": 12450, "sessions": len(SESSIONS), "verified": 0, "failed": 0}
    recent_activities = []
    return render_template(
        "dashboard/index.html",
        title="Dashboard",
        stats=stats,
        recent_activities=recent_activities
    )

@app.get("/students/register")
def register_student():
    return render_template("students/register.html", title="Register Student")


@app.post("/students/register")
def register_student_submit():
    payload = request.get_json() or {}
    student_id = str(payload.get("student_id") or "").strip()
    full_name = str(payload.get("full_name") or "").strip()
    email = str(payload.get("email") or "").strip()
    course = str(payload.get("course") or "N/A").strip() or "N/A"
    level = str(payload.get("year_level") or "Unknown").strip() or "Unknown"
    if not student_id:
        return jsonify({"error": "student_id is required"}), 400
    if not full_name:
        return jsonify({"error": "full_name is required"}), 400
    if not email:
        return jsonify({"error": "email is required"}), 400
    if not isinstance(payload.get("face_images"), list) or len(payload.get("face_images")) < 1:
        return jsonify({"error": "face_images is required"}), 400

    student = {"index": student_id, "name": full_name, "program": course, "level": level}
    if "S1" not in REGISTRATIONS:
        REGISTRATIONS["S1"] = []
    REGISTRATIONS["S1"] = [s for s in REGISTRATIONS["S1"] if s.get("index") != student_id]
    REGISTRATIONS["S1"].append(student)
    return jsonify(
        {
            "message": "Student registered successfully",
            "student": {
                "id": student_id,
                "student_id": student_id,
                "first_name": full_name.split()[0],
                "last_name": " ".join(full_name.split()[1:]),
                "email": email,
                "phone": str(payload.get("phone") or "").strip(),
                "department": str(payload.get("department") or "General").strip(),
                "course": course,
                "year_level": level,
                "is_active": True,
                "biometric_enrolled": True,
                "biometric_blob_size": len(payload.get("face_images") or []) * 1024,
            },
        }
    ), 201

@app.get("/exams/session")
def exam_session():
    session_id = request.args.get("session_id", "S1")
    selected = next((s for s in SESSIONS if s["id"] == session_id), None)
    registered = REGISTRATIONS.get(session_id, [])
    return render_template(
        "exams/session.html",
        title="Live Verification",
        sessions=SESSIONS,
        selected=selected,
        registered=registered
    )


@app.get("/exams/sessions")
def all_sessions():
    sessions = []
    for s in SESSIONS:
        sessions.append(
            {
                "id": s.get("id"),
                "session_name": s.get("title") or s.get("course"),
                "course_code": s.get("course"),
                "venue": s.get("venue"),
                "start_time": None,
                "end_time": None,
                "is_active": True,
            }
        )
    return render_template("exams/sessions_list.html", title="All Sessions", sessions=sessions)

def _parse_date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None
    
@app.route("/attendance/logs")
def attendance_logs():
    return render_template("attendance/logs.html")

@app.route("/students")
def students_directory():
    return render_template("students/students_directory.html")


@app.get("/verify/test")
@app.get("/verification/test")
def verification_test_page():
    return render_template("verification/test.html", title="Biometric Verification Test")


@app.get("/students/data")
def students_data():
    """Frontend demo data endpoint used by students_directory.html."""
    seen = set()
    rows = []

    for registrations in REGISTRATIONS.values():
        for student in registrations:
            index_no = (student.get("index") or "").strip()
            if not index_no or index_no in seen:
                continue
            seen.add(index_no)

            name = (student.get("name") or "").strip()
            parts = name.split()
            first_name = parts[0] if parts else ""
            last_name = " ".join(parts[1:]) if len(parts) > 1 else ""

            rows.append(
                {
                    "id": index_no,
                    "student_id": index_no,
                    "first_name": first_name,
                    "last_name": last_name,
                    "email": "",
                    "phone": "",
                    "department": "General",
                    "course": student.get("program") or "N/A",
                    "year_level": student.get("level") or "Unknown",
                    "is_active": True,
                    "registration_date": None,
                    "last_updated": None,
                    "biometric_enrolled": True,
                    "biometric_blob_size": 0,
                }
            )

    return jsonify({"students": rows, "total": len(rows)}), 200


@app.post("/students/update/<student_id>")
def update_student_data(student_id):
    """Frontend demo update endpoint used by students_directory.html."""
    student_id = str(student_id).strip()
    payload = request.get_json() or {}

    for session_id, registrations in REGISTRATIONS.items():
        for student in registrations:
            if (student.get("index") or "").strip() != str(student_id):
                continue

            first_name = (payload.get("first_name") or "").strip()
            last_name = (payload.get("last_name") or "").strip()
            if first_name or last_name:
                student["name"] = f"{first_name} {last_name}".strip()

            if "course" in payload:
                student["program"] = (payload.get("course") or "").strip() or "N/A"
            if "year_level" in payload:
                student["level"] = (payload.get("year_level") or "").strip() or "Unknown"
            if "department" in payload:
                student["department"] = (payload.get("department") or "").strip() or "General"

            name = (student.get("name") or "").strip()
            parts = name.split()
            updated = {
                "id": student_id,
                "student_id": student_id,
                "first_name": parts[0] if parts else "",
                "last_name": " ".join(parts[1:]) if len(parts) > 1 else "",
                "email": (payload.get("email") or "").strip(),
                "phone": (payload.get("phone") or "").strip(),
                "department": student.get("department") or "General",
                "course": student.get("program") or "N/A",
                "year_level": student.get("level") or "Unknown",
                "is_active": bool(payload.get("is_active", True)),
                "registration_date": None,
                "last_updated": datetime.utcnow().isoformat(),
                "biometric_enrolled": True,
                "biometric_blob_size": 0,
            }
            return jsonify({"message": "Student updated successfully", "student": updated}), 200

    return jsonify({"error": "Student not found"}), 404


@app.post("/students/update/<student_id>/biometric")
def update_student_biometric_data(student_id):
    """Frontend demo biometric update endpoint."""
    student_id = str(student_id).strip()
    payload = request.get_json() or {}
    raw_images = payload.get("face_images") or []
    if not isinstance(raw_images, list) or len(raw_images) == 0:
        return jsonify({"error": "face_images is required"}), 400

    for registrations in REGISTRATIONS.values():
        for student in registrations:
            if (student.get("index") or "").strip() != student_id:
                continue
            return jsonify(
                {
                    "message": "Biometric data updated successfully",
                    "student": {
                        "id": student_id,
                        "student_id": student_id,
                        "first_name": (student.get("name") or "").split(" ")[0] if student.get("name") else "",
                        "last_name": " ".join((student.get("name") or "").split(" ")[1:]) if student.get("name") else "",
                        "email": "",
                        "phone": "",
                        "department": student.get("department") or "General",
                        "course": student.get("program") or "N/A",
                        "year_level": student.get("level") or "Unknown",
                        "is_active": True,
                        "registration_date": None,
                        "last_updated": datetime.utcnow().isoformat(),
                        "biometric_enrolled": True,
                        "biometric_blob_size": len(raw_images) * 1024,
                    },
                }
            ), 200

    return jsonify({"error": "Student not found"}), 404


@app.post("/students/verify-biometric")
def verify_student_biometric():
    payload = request.get_json() or {}
    student_id = str(payload.get("student_id") or "").strip()
    raw = payload.get("live_image")
    if not student_id:
        return jsonify({"error": "student_id is required"}), 400
    if not raw:
        return jsonify({"error": "live_image is required"}), 400

    for registrations in REGISTRATIONS.values():
        for student in registrations:
            if (student.get("index") or "").strip() == student_id:
                # Demo-only simulated score.
                confidence = 0.88
                return jsonify(
                    {
                        "match": True,
                        "confidence": confidence,
                        "quality_ok": True,
                        "quality_message": "Image accepted",
                        "student": {
                            "student_id": student_id,
                            "first_name": (student.get("name") or "").split(" ")[0],
                            "last_name": " ".join((student.get("name") or "").split(" ")[1:]),
                            "course": student.get("program") or "N/A",
                            "year_level": student.get("level") or "Unknown",
                        },
                    }
                ), 200
    return jsonify({"error": "Student not found"}), 404

@app.route("/admin/invigilators/new")
@admin_required
def add_invigilator():
    if not admin_required():
        abort(403)  # forbidden
    return render_template("add_invigilator.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/debug/routes")
def debug_routes():
    routes = []
    for rule in app.url_map.iter_rules():
        methods = sorted([m for m in rule.methods if m not in {"HEAD", "OPTIONS"}])
        routes.append(
            {
                "endpoint": rule.endpoint,
                "methods": methods,
                "rule": str(rule),
            }
        )
    routes.sort(key=lambda r: r["rule"])
    return jsonify({"count": len(routes), "routes": routes}), 200



##venv\Scripts\activate

##@app.route("/attendance/logs")
##def attendance_logs():
##    today = date.today()
##    from_date = _parse_date(request.args.get("from")) or (today - timedelta(days=6))
##    to_date = _parse_date(request.args.get("to")) or today
##
##    # ---- YOU MUST REPLACE THESE 3 LINES WITH YOUR OWN DATA SOURCES ----
##    sessions = get_sessions(from_date, to_date)  # list of sessions
##    # Each session must have: id, course, time, venue, session_date (date)
##    # -----------------------------------------------------------------
##
##    grouped = []
##    day_map = defaultdict(list)
##
##    for s in sessions:
##        session_id = s["id"] if isinstance(s, dict) else s.id
##        course = s["course"] if isinstance(s, dict) else s.course
##        time_ = s["time"] if isinstance(s, dict) else s.time
##        venue = s["venue"] if isinstance(s, dict) else s.venue
##        session_date = s["session_date"] if isinstance(s, dict) else s.session_date
##
##        # ---- YOU MUST REPLACE THESE TWO WITH YOUR OWN FUNCTIONS ----
##        registered = get_registered_students(session_id)  # list of {student_id/name/index}
##        logs = get_attendance_logs(session_id)            # list of {student_id/status/time}
##        # -----------------------------------------------------------
##
##        # Make latest log per student (if multiple scans exist)
##        latest = {}
##        for l in sorted(logs, key=lambda x: x["time"], reverse=True):
##            if l["student_id"] not in latest:
##                latest[l["student_id"]] = l
##
##        roster = []
##        counts = {"registered": 0, "verified": 0, "failed": 0, "not_verified": 0}
##
##        for st in registered:
##            counts["registered"] += 1
##            st_id = st["student_id"]
##
##            if st_id in latest:
##                status = "Verified" if str(latest[st_id]["status"]).lower() == "verified" else "Failed"
##                t = latest[st_id]["time"]
##                time_str = t.strftime("%H:%M:%S") if hasattr(t, "strftime") else str(t)
##            else:
##                status = "Not Verified"
##                time_str = "—"
##
##            if status == "Verified":
##                counts["verified"] += 1
##            elif status == "Failed":
##                counts["failed"] += 1
##            else:
##                counts["not_verified"] += 1
##
##            roster.append({
##                "time": time_str,
##                "name": st["name"],
##                "index": st["index"],
##                "course": course,
##                "status": status
##            })
##
##        label = f"{course} ({time_}) - {venue}"
##        day_map[session_date].append({
##            "id": session_id,
##            "label": label,
##            "counts": counts,
##            "roster": roster
##        })
##
##    for d in sorted(day_map.keys(), reverse=True):
##        grouped.append({"date": d, "sessions": day_map[d]})
##
##    return render_template(
##        "attendance_logs.html",
##        grouped=grouped,
##        from_date=from_date,
##        to_date=to_date
##    )

if __name__ == "__main__":
    app.run(debug=True)
