from datetime import datetime, timezone, timedelta
import logging
import re
import calendar
import os
import random
import math
from flask import Blueprint, render_template, redirect, url_for, request, session, abort, jsonify, current_app
from functools import wraps
import base64
import io
import json
import secrets
from PIL import Image
from utils import db as db_utils
from utils import pause_controls
from utils.encryption import encrypt_data
from services.student_service import StudentService
from config import get_database_backend
from werkzeug.routing import BuildError
from werkzeug.security import check_password_hash, generate_password_hash

web_bp = Blueprint("web", __name__)  # no url_prefix so it uses /
logger = logging.getLogger(__name__)
_student_service = None


def _get_student_service():
    global _student_service
    if _student_service is None:
        _student_service = StudentService()
    return _student_service


def _decode_b64_image(raw):
    if not isinstance(raw, str):
        return None
    encoded = raw.split(",", 1)[1] if "," in raw else raw
    image_bytes = base64.b64decode(encoded)
    return Image.open(io.BytesIO(image_bytes)).convert("RGB")


def _normalize_data_url_image(raw):
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    try:
        _decode_b64_image(cleaned)
        return cleaned
    except Exception:
        return None


def _split_name(full_name):
    parts = (full_name or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _parse_iso_utc_naive(value):
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _parse_iso_date(value):
    return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()


def _tail_file_lines(file_path, limit=200):
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except Exception:
        return []
    if limit <= 0:
        return []
    return [str(line).rstrip("\r\n") for line in lines[-limit:]]


def _looks_like_failure_line(text):
    return bool(
        re.search(
            r"(error|exception|traceback|fail(ed|ure)?|critical|refused|timeout|forbidden|not found|unreachable)",
            str(text or ""),
            flags=re.IGNORECASE,
        )
    )


def _collect_log_sources():
    root = current_app.root_path
    sources = {}

    logs_dir = os.path.join(root, "logs")
    if os.path.isdir(logs_dir):
        for name in os.listdir(logs_dir):
            full_path = os.path.join(logs_dir, name)
            if os.path.isfile(full_path):
                key = f"logs/{name}"
                sources[key] = full_path

    for name in ("run.err.log", "run.out.log"):
        full_path = os.path.join(root, name)
        if os.path.isfile(full_path):
            sources[name] = full_path

    out = []
    for key, path in sources.items():
        try:
            stat = os.stat(path)
            out.append(
                {
                    "name": key,
                    "path": path,
                    "size": int(stat.st_size or 0),
                    "updated_at": datetime.utcfromtimestamp(stat.st_mtime),
                }
            )
        except OSError:
            continue
    out.sort(key=lambda item: item.get("updated_at") or datetime.min, reverse=True)
    return out


def _client_ip():
    forwarded_for = str(request.headers.get("X-Forwarded-For") or "").strip()
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return (request.remote_addr or "").strip() or None


def _client_user_agent():
    ua = str(request.headers.get("User-Agent") or "").strip()
    return ua[:500] if ua else None


def _current_actor_snapshot():
    role = str(session.get("role") or "").strip().lower()
    if session.get("admin_id"):
        return {
            "actor_type": role or "admin",
            "actor_id": int(session.get("admin_id")),
            "actor_username": str(session.get("username") or session.get("user_email") or "").strip() or None,
            "actor_email": str(session.get("user_email") or "").strip().lower() or None,
            "actor_full_name": str(session.get("full_name") or "").strip() or None,
        }
    if session.get("student_db_id"):
        return {
            "actor_type": role or "student",
            "actor_id": int(session.get("student_db_id")),
            "actor_username": str(session.get("student_id") or "").strip() or None,
            "actor_email": None,
            "actor_full_name": str(session.get("student_name") or "").strip() or None,
        }
    return {
        "actor_type": "system",
        "actor_id": None,
        "actor_username": None,
        "actor_email": None,
        "actor_full_name": None,
    }


def _record_system_event(action, entity_type=None, entity_id=None, details=None, actor_override=None):
    try:
        actor = dict(_current_actor_snapshot())
        if isinstance(actor_override, dict):
            actor.update(actor_override)
        action_value = str(action or "").strip()[:120] or "unknown"
        details_text = None
        if details is not None:
            if isinstance(details, (dict, list)):
                details_text = json.dumps(details, ensure_ascii=True, default=str)
            else:
                details_text = str(details)
            details_text = details_text[:12000]
        db_utils.execute(
            """
            INSERT INTO system_event_logs (
                actor_type, actor_id, actor_username, actor_email, actor_full_name,
                action, entity_type, entity_id, details, ip_address, user_agent
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                actor.get("actor_type") or "system",
                actor.get("actor_id"),
                actor.get("actor_username"),
                actor.get("actor_email"),
                actor.get("actor_full_name"),
                action_value,
                (str(entity_type).strip()[:80] if entity_type else None),
                (str(entity_id).strip()[:80] if entity_id is not None else None),
                details_text,
                _client_ip(),
                _client_user_agent(),
            ),
        )
    except Exception as exc:
        logger.warning(f"Failed to record system event ({action}): {exc}")


def _record_login_event(user_type, user_id, username=None, email=None, full_name=None):
    payload = (
        str(user_type or "").strip().lower(),
        int(user_id),
        (str(username or "").strip() or None),
        (str(email or "").strip().lower() or None),
        (str(full_name or "").strip() or None),
        _client_ip(),
        _client_user_agent(),
    )
    if get_database_backend() == "postgresql":
        created = db_utils.execute_returning(
            """
            INSERT INTO login_audit_logs (user_type, user_id, username, email, full_name, ip_address, user_agent)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            payload,
        )
        return int((created or {}).get("id") or 0) or None

    db_utils.execute(
        """
        INSERT INTO login_audit_logs (user_type, user_id, username, email, full_name, ip_address, user_agent)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        payload,
    )
    row = db_utils.fetch_one(
        """
        SELECT id
        FROM login_audit_logs
        WHERE user_type = %s AND user_id = %s AND logout_at IS NULL
        ORDER BY login_at DESC
        LIMIT 1
        """,
        (payload[0], payload[1]),
    )
    return int((row or {}).get("id") or 0) or None


def _record_logout_event():
    login_audit_id = session.get("login_audit_id")
    if login_audit_id:
        db_utils.execute(
            """
            UPDATE login_audit_logs
            SET logout_at = CURRENT_TIMESTAMP
            WHERE id = %s AND logout_at IS NULL
            """,
            (int(login_audit_id),),
        )
        return

    user_type = None
    user_id = None
    if session.get("admin_id"):
        user_type = "admin"
        user_id = int(session.get("admin_id"))
    elif session.get("student_db_id"):
        user_type = "student"
        user_id = int(session.get("student_db_id"))
    if not user_type or not user_id:
        return

    row = db_utils.fetch_one(
        """
        SELECT id
        FROM login_audit_logs
        WHERE user_type = %s AND user_id = %s AND logout_at IS NULL
        ORDER BY login_at DESC
        LIMIT 1
        """,
        (user_type, user_id),
    )
    if row and row.get("id"):
        db_utils.execute(
            """
            UPDATE login_audit_logs
            SET logout_at = CURRENT_TIMESTAMP
            WHERE id = %s AND logout_at IS NULL
            """,
            (int(row["id"]),),
        )


def _normalize_paper_group_code(value):
    cleaned = str(value or "").strip().upper()
    if not cleaned:
        return ""
    cleaned = re.sub(r"[^A-Z0-9_-]+", "-", cleaned)
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-_")
    return cleaned[:80]


def _default_paper_group_code(course_code, start_time, session_period=None):
    base_code = _normalize_paper_group_code(course_code) or "PAPER"
    slot = str(session_period or "").strip().upper()
    slot = slot if slot in {"MORNING", "EVENING"} else ""
    if isinstance(start_time, datetime):
        date_part = start_time.strftime('%Y%m%d')
        if slot:
            return f"{base_code}-{date_part}-{slot}"
        return f"{base_code}-{date_part}-{start_time.strftime('%H%M')}"
    return f"{base_code}-{slot}" if slot else base_code


def _extract_level_number(level_name):
    digits = "".join(ch for ch in str(level_name or "") if ch.isdigit())
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _student_can_access_level(student_level_name, course_level_name):
    student_level_num = _extract_level_number(student_level_name)
    course_level_num = _extract_level_number(course_level_name)
    if student_level_num is not None and course_level_num is not None:
        return course_level_num <= student_level_num
    return str(course_level_name or "").strip().lower() == str(student_level_name or "").strip().lower()


def _program_levels(duration_years):
    return _program_levels_by_category("undergraduate", duration_years)


def _normalize_study_category(raw_value):
    val = str(raw_value or "").strip().lower()
    if val in {"undergraduate", "masters", "phd"}:
        return val
    return "undergraduate"


def _optional_study_category(raw_value):
    val = str(raw_value or "").strip().lower()
    return val if val in {"undergraduate", "masters", "phd"} else ""


def _program_levels_by_category(study_category, duration_years=None):
    category = _normalize_study_category(study_category)
    if category == "masters":
        return ["M1", "M2"]
    if category == "phd":
        try:
            years = int(duration_years)
        except (TypeError, ValueError):
            years = 4
        years = max(3, min(years, 6))
        return [f"PHD{idx}" for idx in range(1, years + 1)]
    try:
        years = int(duration_years)
    except (TypeError, ValueError):
        years = 4
    years = max(1, min(years, 10))
    return [str((idx + 1) * 100) for idx in range(years)]


def _get_program_definition(program_name):
    if not str(program_name or "").strip():
        return None
    return db_utils.fetch_one(
        """
        SELECT id, program_name, study_category, duration_years, semesters_per_year, is_active, created_at
        FROM academic_programs
        WHERE LOWER(program_name) = LOWER(%s)
        LIMIT 1
        """,
        (program_name,),
    )


def _get_current_academic_year():
    return db_utils.fetch_one(
        """
        SELECT id, year_label, is_current, enrollment_open, is_active, created_at
        FROM academic_years
        WHERE is_current = TRUE
        ORDER BY id DESC
        LIMIT 1
        """
    )


def _get_current_open_exam_period(for_date=None):
    target_date = for_date or datetime.utcnow().date()
    return db_utils.fetch_one(
        """
        SELECT id, period_name, period_type, start_date, end_date, is_active
        FROM exam_periods
        WHERE is_active = TRUE
          AND %s BETWEEN start_date AND end_date
        ORDER BY start_date DESC, id DESC
        LIMIT 1
        """,
        (target_date,),
    )


def _get_exam_period_by_id(period_id):
    try:
        pid = int(period_id)
    except (TypeError, ValueError):
        return None
    return db_utils.fetch_one(
        """
        SELECT id, period_name, period_type, start_date, end_date, is_active
        FROM exam_periods
        WHERE id = %s
        LIMIT 1
        """,
        (pid,),
    )


def _coerce_datetime(value):
    if isinstance(value, datetime):
        return value
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None


def _coerce_date(value):
    if value is None:
        return None
    if hasattr(value, "year") and hasattr(value, "month") and hasattr(value, "day"):
        try:
            return value if not isinstance(value, datetime) else value.date()
        except Exception:
            pass
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except Exception:
        return None


def _session_overlaps_period(row, period):
    if not period:
        return True
    period_start = _coerce_date(period.get("start_date"))
    period_end = _coerce_date(period.get("end_date"))
    if not period_start or not period_end:
        return True

    start_dt = _coerce_datetime(row.get("start_time"))
    end_dt = _coerce_datetime(row.get("end_time")) or start_dt
    if not start_dt and not end_dt:
        return True

    start_date = (start_dt or end_dt).date()
    end_date = (end_dt or start_dt).date()
    return start_date <= period_end and end_date >= period_start


def _build_hall_usage_summary(period=None):
    now_utc = datetime.utcnow()
    rows = db_utils.fetch_all(
        """
        SELECT
            h.id AS hall_id,
            h.name AS hall_name,
            h.capacity AS hall_capacity,
            es.id AS session_id,
            es.session_name,
            es.course_code,
            es.expected_students,
            es.start_time,
            es.end_time
        FROM exam_halls h
        LEFT JOIN examination_sessions es ON es.hall_id = h.id
        WHERE h.is_active = TRUE
        ORDER BY h.name ASC, es.start_time ASC, es.id ASC
        """
    )

    out = {}
    for row in rows:
        hall_id = int(row.get("hall_id"))
        if hall_id not in out:
            out[hall_id] = {
                "hall_id": hall_id,
                "hall_name": row.get("hall_name"),
                "hall_capacity": int(row.get("hall_capacity") or 0),
                "total_expected_used": 0,
                "sessions_count": 0,
                "sessions": [],
            }
        session_id = row.get("session_id")
        if session_id is None:
            continue
        if not _session_overlaps_period(row, period):
            continue

        start_dt = _coerce_datetime(row.get("start_time"))
        end_dt = _coerce_datetime(row.get("end_time"))
        if end_dt and end_dt < now_utc:
            # Exclude sessions that have already ended.
            continue

        expected_students = int(row.get("expected_students") or 0)
        session_label = str(row.get("course_code") or row.get("session_name") or "N/A").strip() or "N/A"
        is_current = False
        if start_dt and end_dt:
            is_current = start_dt <= now_utc <= end_dt
        elif start_dt and not end_dt:
            is_current = start_dt <= now_utc
        elif end_dt and not start_dt:
            is_current = now_utc <= end_dt

        out[hall_id]["sessions"].append(
            {
                "session_id": int(session_id),
                "session_label": session_label,
                "expected_students": expected_students,
                "start_time": start_dt.isoformat() if start_dt else None,
                "end_time": end_dt.isoformat() if end_dt else None,
                "is_current": bool(is_current),
            }
        )
        out[hall_id]["total_expected_used"] += expected_students
        if is_current:
            out[hall_id]["current_expected_used"] = int(out[hall_id].get("current_expected_used") or 0) + expected_students

    for hall in out.values():
        hall["current_expected_used"] = int(hall.get("current_expected_used") or 0)
        hall["current_available_seats"] = max(0, int(hall["hall_capacity"]) - hall["current_expected_used"])
        hall["sessions_count"] = len(hall["sessions"])
    return sorted(out.values(), key=lambda h: str(h.get("hall_name") or ""))


def _parse_hhmm_time(value):
    raw = str(value or "").strip()
    try:
        return datetime.strptime(raw, "%H:%M").time()
    except ValueError:
        raise ValueError("time value must be in HH:MM format")


def _expand_period_days_by_weekdays(start_date, end_date, weekday_indexes):
    """
    Build all dates within [start_date, end_date] whose weekday is selected.
    Weekday format: Monday=0 ... Sunday=6
    """
    selected = {int(w) for w in weekday_indexes if 0 <= int(w) <= 6}
    if not selected:
        return []
    cursor = start_date
    out = []
    while cursor <= end_date:
        if int(cursor.weekday()) in selected:
            out.append(cursor)
        cursor = cursor + timedelta(days=1)
    return out


def _build_scheduler_course_catalog():
    rows = db_utils.fetch_all(
        """
        SELECT
            UPPER(course_code) AS course_code,
            MAX(course_title) AS course_title
        FROM program_level_courses
        WHERE is_active = TRUE
          AND COALESCE(LTRIM(RTRIM(course_code)), '') <> ''
        GROUP BY UPPER(course_code)
        ORDER BY UPPER(course_code) ASC
        """
    )
    out = []
    for row in rows:
        code = str(row.get("course_code") or "").strip().upper()
        if not code:
            continue
        out.append(
            {
                "course_code": code,
                "course_title": str(row.get("course_title") or "").strip(),
            }
        )
    return out


def _fetch_scheduler_course_details(course_codes):
    valid_codes = []
    for code in course_codes or []:
        cleaned = str(code or "").strip().upper()
        if cleaned:
            valid_codes.append(cleaned)
    valid_codes = sorted(set(valid_codes))
    if not valid_codes:
        return {}

    placeholders = ", ".join(["%s"] * len(valid_codes))
    rows = db_utils.fetch_all(
        f"""
        SELECT UPPER(course_code) AS course_code, MAX(course_title) AS course_title
        FROM program_level_courses
        WHERE is_active = TRUE
          AND UPPER(course_code) IN ({placeholders})
        GROUP BY UPPER(course_code)
        ORDER BY UPPER(course_code) ASC
        """,
        tuple(valid_codes),
    )
    out = {}
    for row in rows:
        code = str(row.get("course_code") or "").strip().upper()
        if not code:
            continue
        out[code] = {
            "course_code": code,
            "course_title": str(row.get("course_title") or "").strip(),
        }
    return out


def _fetch_course_registration_counts(course_codes):
    valid_codes = []
    for code in course_codes or []:
        cleaned = str(code or "").strip().upper()
        if cleaned:
            valid_codes.append(cleaned)
    valid_codes = sorted(set(valid_codes))
    if not valid_codes:
        return {}
    placeholders = ", ".join(["%s"] * len(valid_codes))
    rows = db_utils.fetch_all(
        f"""
        SELECT UPPER(course_code) AS course_code, COUNT(DISTINCT student_id) AS c
        FROM student_course_registrations
        WHERE UPPER(course_code) IN ({placeholders})
        GROUP BY UPPER(course_code)
        """,
        tuple(valid_codes),
    )
    out = {}
    for row in rows:
        code = str(row.get("course_code") or "").strip().upper()
        if not code:
            continue
        out[code] = int(row.get("c") or 0)
    return out


def _fetch_course_registration_breakdown(course_codes):
    valid_codes = []
    for code in course_codes or []:
        cleaned = str(code or "").strip().upper()
        if cleaned:
            valid_codes.append(cleaned)
    valid_codes = sorted(set(valid_codes))
    if not valid_codes:
        return {}
    placeholders = ", ".join(["%s"] * len(valid_codes))
    rows = db_utils.fetch_all(
        f"""
        SELECT
            UPPER(course_code) AS course_code,
            COALESCE(NULLIF(LTRIM(RTRIM(program_name)), ''), 'UNSPECIFIED') AS program_name,
            COALESCE(NULLIF(LTRIM(RTRIM(level_name)), ''), 'UNSPECIFIED') AS level_name,
            COUNT(DISTINCT student_id) AS c
        FROM student_course_registrations
        WHERE UPPER(course_code) IN ({placeholders})
        GROUP BY
            UPPER(course_code),
            COALESCE(NULLIF(LTRIM(RTRIM(program_name)), ''), 'UNSPECIFIED'),
            COALESCE(NULLIF(LTRIM(RTRIM(level_name)), ''), 'UNSPECIFIED')
        ORDER BY UPPER(course_code) ASC, c DESC, program_name ASC, level_name ASC
        """,
        tuple(valid_codes),
    )
    out = {}
    for row in rows:
        code = str(row.get("course_code") or "").strip().upper()
        if not code:
            continue
        out.setdefault(code, []).append(
            {
                "program_name": str(row.get("program_name") or "UNSPECIFIED").strip(),
                "level_name": str(row.get("level_name") or "UNSPECIFIED").strip(),
                "count": int(row.get("c") or 0),
            }
        )
    return out


def _fetch_period_available_invigilator_ids(period_id):
    rows = db_utils.fetch_all(
        """
        SELECT eia.invigilator_id
        FROM exam_period_invigilator_availability eia
        INNER JOIN admins a ON a.id = eia.invigilator_id
        WHERE eia.exam_period_id = %s
          AND eia.is_available = TRUE
          AND a.role = 'lecturer'
          AND a.is_active = TRUE
        ORDER BY eia.invigilator_id ASC
        """,
        (int(period_id),),
    )
    return [int(r.get("invigilator_id")) for r in rows if r.get("invigilator_id") is not None]


def _fetch_busy_invigilator_ids(slot_start, slot_end, cooldown_minutes=0):
    cooldown = max(0, int(cooldown_minutes or 0))
    threshold_start = slot_start - timedelta(minutes=cooldown)
    rows = db_utils.fetch_all(
        """
        SELECT DISTINCT si.invigilator_id
        FROM session_invigilators si
        INNER JOIN examination_sessions es ON es.id = si.session_id
        WHERE si.is_active = TRUE
          AND es.start_time < %s
          AND es.end_time > %s
        """,
        (slot_end, threshold_start),
    )
    return {int(r.get("invigilator_id")) for r in rows if r.get("invigilator_id") is not None}


def _get_exam_period_tracking_data(period_id):
    period = db_utils.fetch_one(
        """
        SELECT id, period_name, period_type, start_date, end_date, is_active
        FROM exam_periods
        WHERE id = %s
        LIMIT 1
        """,
        (int(period_id),),
    )
    if not period:
        return None

    start_date = period.get("start_date")
    end_date = period.get("end_date")
    if not start_date or not end_date:
        return {"period": period, "summary": {}, "recent_logs": [], "sessions": []}

    verification_total = db_utils.fetch_one(
        """
        SELECT COUNT(*) AS c
        FROM verification_logs
        WHERE CAST(timestamp AS DATE) BETWEEN %s AND %s
        """,
        (start_date, end_date),
    )
    verification_success = db_utils.fetch_one(
        """
        SELECT COUNT(*) AS c
        FROM verification_logs
        WHERE CAST(timestamp AS DATE) BETWEEN %s AND %s
          AND outcome = 'SUCCESS'
        """,
        (start_date, end_date),
    )
    verification_fail = db_utils.fetch_one(
        """
        SELECT COUNT(*) AS c
        FROM verification_logs
        WHERE CAST(timestamp AS DATE) BETWEEN %s AND %s
          AND outcome <> 'SUCCESS'
        """,
        (start_date, end_date),
    )
    session_total = db_utils.fetch_one(
        """
        SELECT COUNT(*) AS c
        FROM examination_sessions
        WHERE CAST(start_time AS DATE) BETWEEN %s AND %s
        """,
        (start_date, end_date),
    )
    exam_attendance_total = db_utils.fetch_one(
        """
        SELECT COUNT(*) AS c
        FROM attendances
        WHERE CAST(timestamp AS DATE) BETWEEN %s AND %s
        """,
        (start_date, end_date),
    )
    class_attendance_total = db_utils.fetch_one(
        """
        SELECT COUNT(*) AS c
        FROM class_attendances
        WHERE attendance_date BETWEEN %s AND %s
        """,
        (start_date, end_date),
    )

    recent_rows = db_utils.fetch_all(
        """
        SELECT
            vl.timestamp,
            vl.outcome,
            vl.confidence,
            vl.reason,
            COALESCE(es.course_code, es.session_name, 'N/A') AS session_label,
            s.student_id AS index_no,
            s.first_name,
            s.last_name
        FROM verification_logs vl
        LEFT JOIN examination_sessions es ON es.id = vl.session_id
        LEFT JOIN students s ON s.id = vl.student_id
        WHERE CAST(vl.timestamp AS DATE) BETWEEN %s AND %s
        ORDER BY vl.timestamp DESC
        LIMIT 100
        """,
        (start_date, end_date),
    )
    period_sessions = db_utils.fetch_all(
        """
        SELECT id, session_name, course_code, venue, start_time, end_time, is_active
        FROM examination_sessions
        WHERE CAST(start_time AS DATE) BETWEEN %s AND %s
        ORDER BY start_time ASC
        """,
        (start_date, end_date),
    )

    detailed_runs = []
    session_ids = [int(s.get("id")) for s in period_sessions if s.get("id") is not None]
    if session_ids:
        placeholders = ", ".join(["%s"] * len(session_ids))
        invigilator_rows = db_utils.fetch_all(
            f"""
            SELECT
                si.session_id,
                a.id AS invigilator_id,
                a.full_name,
                a.email,
                a.username
            FROM session_invigilators si
            INNER JOIN admins a ON a.id = si.invigilator_id
            WHERE si.is_active = TRUE
              AND si.session_id IN ({placeholders})
            ORDER BY si.session_id ASC, a.full_name ASC
            """,
            tuple(session_ids),
        )
        student_rows = db_utils.fetch_all(
            f"""
            SELECT
                atd.session_id,
                atd.timestamp,
                atd.verification_confidence,
                s.id AS student_db_id,
                s.student_id,
                s.first_name,
                s.last_name
            FROM attendances atd
            INNER JOIN students s ON s.id = atd.student_id
            WHERE atd.session_id IN ({placeholders})
            ORDER BY atd.session_id ASC, atd.timestamp ASC
            """,
            tuple(session_ids),
        )
        paper_rows = db_utils.fetch_all(
            f"""
            SELECT session_id, paper_code, paper_title
            FROM exam_papers
            WHERE session_id IN ({placeholders})
            ORDER BY session_id ASC, paper_title ASC
            """,
            tuple(session_ids),
        )

        inv_by_session = {}
        for r in invigilator_rows:
            sid = int(r.get("session_id"))
            inv_by_session.setdefault(sid, []).append(
                {
                    "id": r.get("invigilator_id"),
                    "full_name": r.get("full_name") or r.get("username") or "Unknown Invigilator",
                    "email": r.get("email") or "",
                }
            )

        students_by_session = {}
        for r in student_rows:
            sid = int(r.get("session_id"))
            students_by_session.setdefault(sid, []).append(
                {
                    "student_db_id": r.get("student_db_id"),
                    "student_id": r.get("student_id") or "",
                    "full_name": f"{r.get('first_name') or ''} {r.get('last_name') or ''}".strip() or "Unknown Student",
                    "timestamp": r.get("timestamp"),
                    "confidence": r.get("verification_confidence"),
                }
            )

        papers_by_session = {}
        for r in paper_rows:
            sid = int(r.get("session_id"))
            papers_by_session.setdefault(sid, []).append(
                {
                    "paper_code": r.get("paper_code") or "",
                    "paper_title": r.get("paper_title") or "",
                }
            )

        for s in period_sessions:
            sid = int(s.get("id"))
            students = students_by_session.get(sid, [])
            invigilators = inv_by_session.get(sid, [])
            papers = papers_by_session.get(sid, [])
            detailed_runs.append(
                {
                    "session_id": sid,
                    "session_name": s.get("session_name") or "",
                    "course_code": s.get("course_code") or "",
                    "venue": s.get("venue") or "",
                    "start_time": s.get("start_time"),
                    "end_time": s.get("end_time"),
                    "is_active": bool(s.get("is_active")),
                    "papers": papers,
                    "students": students,
                    "invigilators": invigilators,
                    "student_count": len(students),
                    "invigilator_count": len(invigilators),
                }
            )

    return {
        "period": period,
        "summary": {
            "sessions": int((session_total or {}).get("c") or 0),
            "verifications_total": int((verification_total or {}).get("c") or 0),
            "verifications_success": int((verification_success or {}).get("c") or 0),
            "verifications_fail": int((verification_fail or {}).get("c") or 0),
            "exam_attendance": int((exam_attendance_total or {}).get("c") or 0),
            "class_attendance": int((class_attendance_total or {}).get("c") or 0),
        },
        "recent_logs": recent_rows,
        "sessions": period_sessions,
        "detailed_runs": detailed_runs,
    }


def _academic_year_program_exception_exists(academic_year_id, program_name):
    if not academic_year_id or not str(program_name or "").strip():
        return False
    row = db_utils.fetch_one(
        """
        SELECT id
        FROM academic_year_program_exceptions
        WHERE academic_year_id = %s AND LOWER(program_name) = LOWER(%s)
        LIMIT 1
        """,
        (int(academic_year_id), str(program_name).strip()),
    )
    return bool(row)


def _next_academic_year_label(current_label):
    label = str(current_label or "").strip()
    match = re.fullmatch(r"(\d{4})\s*/\s*(\d{4})", label)
    if match:
        start = int(match.group(1)) + 1
        end = int(match.group(2)) + 1
        return f"{start}/{end}"
    match_single = re.fullmatch(r"(\d{4})", label)
    if match_single:
        start = int(match_single.group(1)) + 1
        return f"{start}/{start + 1}"
    year_now = datetime.utcnow().year
    return f"{year_now}/{year_now + 1}"


def _parse_month_day(month_value, day_value, prefix):
    try:
        month = int(month_value)
        day = int(day_value)
    except (TypeError, ValueError):
        raise ValueError(f"{prefix}_month and {prefix}_day must be numbers")

    if month < 1 or month > 12:
        raise ValueError(f"{prefix}_month must be between 1 and 12")
    max_day = calendar.monthrange(2000, month)[1]
    if day < 1 or day > max_day:
        raise ValueError(f"{prefix}_day must be between 1 and {max_day} for month {month}")
    return month, day


def _is_level_completed(program_name, level_name):
    max_semesters = _effective_semester_count(program_name, level_name)
    if not max_semesters:
        return False
    ended_map = _semester_end_map(program_name, level_name)
    for sem_no in range(1, int(max_semesters) + 1):
        if not ended_map.get(sem_no):
            return False
    return True


def _promote_eligible_students():
    students = db_utils.fetch_all(
        """
        SELECT id, student_id, course, year_level, is_active
        FROM students
        WHERE COALESCE(is_active, TRUE) = TRUE
        ORDER BY id ASC
        """
    )
    promoted = []
    skipped = []
    for student in students:
        program_name = str(student.get("course") or "").strip()
        current_level = str(student.get("year_level") or "").strip()
        if not program_name or not current_level:
            skipped.append({"student_id": student.get("student_id"), "reason": "Missing program or level"})
            continue

        program = _get_program_definition(program_name)
        if not program:
            skipped.append({"student_id": student.get("student_id"), "reason": "Program definition not found"})
            continue

        levels = _program_levels_by_category(program.get("study_category"), program.get("duration_years"))
        if current_level not in levels:
            skipped.append({"student_id": student.get("student_id"), "reason": "Current level not in program levels"})
            continue

        if not _is_level_completed(program_name, current_level):
            skipped.append({"student_id": student.get("student_id"), "reason": f"Level {current_level} not completed"})
            continue

        current_idx = levels.index(current_level)
        if current_idx >= len(levels) - 1:
            skipped.append({"student_id": student.get("student_id"), "reason": "Final level already reached"})
            continue

        next_level = levels[current_idx + 1]
        db_utils.execute(
            """
            UPDATE students
            SET year_level = %s, last_updated = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (next_level, int(student.get("id"))),
        )
        promoted.append(
            {
                "student_id": student.get("student_id"),
                "from_level": current_level,
                "to_level": next_level,
                "program": program_name,
            }
        )
    return promoted, skipped


def _effective_semester_count(program_name, level_name):
    program = _get_program_definition(program_name)
    if not program:
        return None
    return int(program.get("semesters_per_year") or 2)


def _semester_end_map(program_name, level_name):
    rows = db_utils.fetch_all(
        """
        SELECT semester_no, is_ended
        FROM program_level_semester_statuses
        WHERE LOWER(program_name) = LOWER(%s) AND LOWER(level_name) = LOWER(%s)
        """,
        (program_name, level_name),
    )
    out = {}
    for row in rows:
        try:
            sem_no = int(row.get("semester_no"))
        except (TypeError, ValueError):
            continue
        out[sem_no] = bool(row.get("is_ended"))
    return out


def _unlocked_semester(program_name, level_name):
    max_semesters = _effective_semester_count(program_name, level_name) or 1
    ended = _semester_end_map(program_name, level_name)
    unlocked = 1
    for sem_no in range(1, max_semesters):
        if ended.get(sem_no):
            unlocked = sem_no + 1
        else:
            break
    return min(unlocked, max_semesters)


def _student_can_access_course(student_program, student_level_name, course_level_name, course_semester_no):
    student_level_num = _extract_level_number(student_level_name)
    course_level_num = _extract_level_number(course_level_name)
    if student_level_num is not None and course_level_num is not None:
        if course_level_num > student_level_num:
            return False
        if course_level_num < student_level_num:
            return True
    elif str(course_level_name or "").strip().lower() != str(student_level_name or "").strip().lower():
        return False

    semester_no = int(course_semester_no or 1)
    unlocked = _unlocked_semester(student_program, course_level_name)
    return semester_no <= unlocked


def _auto_activate_live_sessions():
    db_utils.execute(
        """
        UPDATE examination_sessions
        SET is_active = TRUE
        WHERE is_active = FALSE
          AND CURRENT_TIMESTAMP BETWEEN start_time AND end_time
        """
    )


def _has_role(*allowed_roles):
    role = str(session.get("role") or "").strip().lower()
    return role in {str(r).strip().lower() for r in allowed_roles}


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin_id"):
            return redirect(url_for("web.login_page"))
        if session.get("admin_force_change_password"):
            return redirect(url_for("web.admin_change_password_page"))
        return fn(*args, **kwargs)
    return wrapper


def roles_required(*allowed_roles):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not session.get("admin_id"):
                return redirect(url_for("web.login_page"))
            if session.get("admin_force_change_password"):
                return redirect(url_for("web.admin_change_password_page"))
            if not _has_role(*allowed_roles):
                abort(403)
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def student_portal_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        student_db_id = session.get("student_db_id")
        if not student_db_id:
            return redirect(url_for("web.student_login_page"))
        if session.get("student_force_change_password"):
            return redirect(url_for("web.student_change_password_page"))
        return fn(*args, **kwargs)
    return wrapper


def _lecturer_courses(lecturer_id):
    rows = db_utils.fetch_all(
        """
        SELECT
            lc.course_code,
            COALESCE(NULLIF(lc.course_title, ''), MIN(plc.course_title)) AS course_title
        FROM lecturer_courses lc
        LEFT JOIN program_level_courses plc
               ON UPPER(plc.course_code) = UPPER(lc.course_code)
              AND plc.is_active = TRUE
        WHERE lc.lecturer_id = %s
          AND lc.is_active = TRUE
        GROUP BY lc.course_code, lc.course_title
        ORDER BY course_code ASC
        """,
        (lecturer_id,),
    )
    return rows


def _lecturer_course_codes(lecturer_id):
    rows = _lecturer_courses(lecturer_id)
    return sorted(
        {
            str(r.get("course_code") or "").strip().upper()
            for r in rows
            if str(r.get("course_code") or "").strip()
        }
    )


def _lecturer_teaching_scope_rows(lecturer_id):
    _ensure_departments_schema()
    return db_utils.fetch_all(
        """
        SELECT
            lc.course_code,
            COALESCE(NULLIF(lc.course_title, ''), plc.course_title, '') AS course_title,
            COALESCE(plc.program_name, '') AS program_name,
            COALESCE(plc.level_name, '') AS level_name,
            plc.semester_no,
            COALESCE(d.department_name, 'Unassigned') AS department_name
        FROM lecturer_courses lc
        LEFT JOIN program_level_courses plc
               ON UPPER(plc.course_code) = UPPER(lc.course_code)
              AND plc.is_active = TRUE
        LEFT JOIN program_department_map pdm
               ON LOWER(pdm.program_name) = LOWER(plc.program_name)
        LEFT JOIN departments d
               ON d.id = pdm.department_id
        WHERE lc.lecturer_id = %s
          AND lc.is_active = TRUE
        ORDER BY department_name ASC, program_name ASC, level_name ASC, UPPER(lc.course_code) ASC
        """,
        (int(lecturer_id),),
    )


@web_bp.app_context_processor
def inject_template_helpers():
    fallback_paths = {
        "web.dashboard_page": "/dashboard",
        "web.register_student_page": "/students/register",
        "web.students_directory_page": "/students",
        "web.exam_session_page": "/exams/session",
        "web.verification_test_page": "/verify/test",
        "web.all_sessions_page": "/exams/sessions",
        "web.attendance_logs_page": "/attendance/logs",
        "web.class_attendance_page": "/class/attendance",
        "web.class_attendance_logs_page": "/class/attendance/logs",
        "web.lecturer_teaching_scope_page": "/lecturer/teaching-scope",
        "web.session_setup_page": "/admin/session-setup",
        "web.halls_setup_page": "/admin/halls/setup",
        "web.course_catalog_page": "/admin/courses/setup",
        "web.add_program_course_page": "/admin/program-courses/add",
        "web.departments_page": "/admin/departments/manage",
        "web.academic_years_page": "/admin/academic-years/manage",
        "web.semester_control_page": "/admin/semester-control",
        "web.lecturer_course_assignments_page": "/admin/lecturer-courses/manage",
        "web.exam_periods_page": "/admin/exam-periods/manage",
        "web.exam_scheduler_page": "/admin/exam-scheduler",
        "web.add_lecturer_page": "/admin/lecturers/new",
        "web.add_invigilator_page": "/admin/lecturers/new",
        "web.lecturers_manage_page": "/admin/lecturers/manage",
        "web.event_logs_page": "/admin/audit/events",
        "web.system_status_page": "/admin/system-status",
        "web.login_audit_logs_page": "/admin/audit/login-logs",
        "web.logout_page": "/logout",
        "web.login_page": "/login",
    }

    def safe_url_for(endpoint, **values):
        try:
            return url_for(endpoint, **values)
        except BuildError:
            return fallback_paths.get(endpoint)
    return {"safe_url_for": safe_url_for}

def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if session.get("admin_force_change_password"):
            return redirect(url_for("web.admin_change_password_page"))
        if not _has_role("admin", "super_admin"):
            abort(403)
        return fn(*args, **kwargs)
    return wrapper

@web_bp.get("/", endpoint="home_page")
def home():
    return redirect(url_for("web.login_page"))

@web_bp.route("/login", methods=["GET", "POST"])
@web_bp.route("/login", methods=["GET", "POST"], endpoint="login_page")
def login():
    if request.method == "GET":
        return render_template("auth/login.html", info=request.args.get("info"))

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    if not username or not password:
        return render_template("auth/login.html", error="Username and password are required.")

    admin = db_utils.fetch_one(
        """
        SELECT id, username, email, full_name, role, password_hash, must_change_password, is_active
        FROM admins
        WHERE LOWER(username) = LOWER(%s) OR LOWER(email) = LOWER(%s)
        LIMIT 1
        """,
        (username, username),
    )
    if not admin or not admin.get("is_active"):
        return render_template("auth/login.html", error="Invalid credentials.")
    if not check_password_hash(admin.get("password_hash") or "", password):
        return render_template("auth/login.html", error="Invalid credentials.")

    db_utils.execute(
        "UPDATE admins SET last_login = CURRENT_TIMESTAMP WHERE id = %s",
        (admin["id"],),
    )
    session.clear()
    session["admin_id"] = admin["id"]
    session["user_email"] = admin.get("email") or ""
    session["role"] = (admin.get("role") or "invigilator").lower()
    session["full_name"] = admin.get("full_name") or admin.get("username") or ""
    session["admin_force_change_password"] = bool(admin.get("must_change_password"))
    session["login_audit_id"] = _record_login_event(
        user_type="admin",
        user_id=admin["id"],
        username=admin.get("username"),
        email=admin.get("email"),
        full_name=admin.get("full_name"),
    )
    if session.get("admin_force_change_password"):
        return redirect(url_for("web.admin_change_password_page"))
    return redirect(url_for("web.dashboard_page"))


@web_bp.route("/student/login", methods=["GET", "POST"])
@web_bp.route("/student/login", methods=["GET", "POST"], endpoint="student_login_page")
def student_login():
    if request.method == "GET":
        return render_template(
            "student/login.html",
            info=request.args.get("info"),
            temp_password=request.args.get("temp_password"),
        )

    student_id = str(request.form.get("student_id") or "").strip()
    email = str(request.form.get("email") or "").strip().lower()
    password = str(request.form.get("password") or "")
    if not student_id or not email or not password:
        return render_template("student/login.html", error="Student ID, email, and password are required.")

    student = db_utils.fetch_one(
        """
        SELECT id, student_id, first_name, last_name, email, course, year_level, is_active,
               password_hash, must_change_password
        FROM students
        WHERE LOWER(student_id) = LOWER(%s) AND LOWER(email) = LOWER(%s)
        LIMIT 1
        """,
        (student_id, email),
    )
    if not student or not student.get("is_active"):
        return render_template("student/login.html", error="Invalid credentials.")
    if not check_password_hash(student.get("password_hash") or "", password):
        return render_template("student/login.html", error="Invalid credentials.")

    full_name = f"{student.get('first_name') or ''} {student.get('last_name') or ''}".strip()
    session.clear()
    session["student_db_id"] = int(student["id"])
    session["student_id"] = student.get("student_id")
    session["student_name"] = full_name or student.get("student_id")
    session["student_program"] = student.get("course")
    session["student_level"] = student.get("year_level")
    session["student_force_change_password"] = bool(student.get("must_change_password"))
    session["login_audit_id"] = _record_login_event(
        user_type="student",
        user_id=student["id"],
        username=student.get("student_id"),
        email=student.get("email"),
        full_name=full_name or student.get("student_id"),
    )
    if session.get("student_force_change_password"):
        return redirect(url_for("web.student_change_password_page"))
    return redirect(url_for("web.student_portal_page"))


@web_bp.route("/account/change-password", methods=["GET", "POST"])
@web_bp.route("/account/change-password", methods=["GET", "POST"], endpoint="admin_change_password_page")
def admin_change_password():
    admin_id = session.get("admin_id")
    if not admin_id:
        return redirect(url_for("web.login_page"))

    if request.method == "GET":
        return render_template("auth/change_password.html")

    old_password = str(request.form.get("old_password") or "")
    new_password = str(request.form.get("new_password") or "")
    confirm_password = str(request.form.get("confirm_password") or "")
    if not old_password or not new_password or not confirm_password:
        return render_template("auth/change_password.html", error="All password fields are required.")
    if len(new_password) < 8:
        return render_template("auth/change_password.html", error="New password must be at least 8 characters.")
    if new_password != confirm_password:
        return render_template("auth/change_password.html", error="New passwords do not match.")

    admin = db_utils.fetch_one(
        "SELECT id, password_hash FROM admins WHERE id = %s",
        (int(admin_id),),
    )
    if not admin:
        session.clear()
        return redirect(url_for("web.login_page"))
    if not check_password_hash(admin.get("password_hash") or "", old_password):
        return render_template("auth/change_password.html", error="Current password is incorrect.")

    db_utils.execute(
        """
        UPDATE admins
        SET password_hash = %s, must_change_password = FALSE
        WHERE id = %s
        """,
        (generate_password_hash(new_password), int(admin_id)),
    )
    session["admin_force_change_password"] = False
    return redirect(url_for("web.dashboard_page"))


@web_bp.post("/student/forgot-password")
def student_forgot_password():
    student_id = str(request.form.get("student_id") or "").strip()
    email = str(request.form.get("email") or "").strip().lower()
    if not student_id or not email:
        return render_template(
            "student/login.html",
            error="Provide both Student ID and email to reset password.",
        )

    student = db_utils.fetch_one(
        """
        SELECT id, is_active
        FROM students
        WHERE LOWER(student_id) = LOWER(%s) AND LOWER(email) = LOWER(%s)
        LIMIT 1
        """,
        (student_id, email),
    )
    if not student or not student.get("is_active"):
        return render_template("student/login.html", error="Student record not found for that ID and email.")

    temporary_password = secrets.token_urlsafe(8)
    db_utils.execute(
        """
        UPDATE students
        SET password_hash = %s, must_change_password = TRUE, last_updated = CURRENT_TIMESTAMP
        WHERE id = %s
        """,
        (generate_password_hash(temporary_password), int(student["id"])),
    )
    return render_template(
        "student/login.html",
        info="Temporary password generated. Use it to sign in, then change your password.",
        temp_password=temporary_password,
    )


@web_bp.route("/student/change-password", methods=["GET", "POST"])
@web_bp.route("/student/change-password", methods=["GET", "POST"], endpoint="student_change_password_page")
def student_change_password():
    student_db_id = session.get("student_db_id")
    if not student_db_id:
        return redirect(url_for("web.student_login_page"))

    if request.method == "GET":
        return render_template("student/change_password.html")

    old_password = str(request.form.get("old_password") or "")
    new_password = str(request.form.get("new_password") or "")
    confirm_password = str(request.form.get("confirm_password") or "")
    if not old_password or not new_password or not confirm_password:
        return render_template("student/change_password.html", error="All password fields are required.")
    if len(new_password) < 8:
        return render_template("student/change_password.html", error="New password must be at least 8 characters.")
    if new_password != confirm_password:
        return render_template("student/change_password.html", error="New passwords do not match.")

    student = db_utils.fetch_one(
        "SELECT id, password_hash FROM students WHERE id = %s",
        (int(student_db_id),),
    )
    if not student:
        session.clear()
        return redirect(url_for("web.student_login_page"))
    if not check_password_hash(student.get("password_hash") or "", old_password):
        return render_template("student/change_password.html", error="Current password is incorrect.")

    db_utils.execute(
        """
        UPDATE students
        SET password_hash = %s, must_change_password = FALSE, last_updated = CURRENT_TIMESTAMP
        WHERE id = %s
        """,
        (generate_password_hash(new_password), int(student_db_id)),
    )
    session["student_force_change_password"] = False
    return redirect(url_for("web.student_portal_page", msg="Password changed successfully."))


@web_bp.get("/student/portal")
@web_bp.get("/student/portal", endpoint="student_portal_page")
@student_portal_required
def student_portal_page():
    student_db_id = int(session.get("student_db_id"))
    student = db_utils.fetch_one(
        """
        SELECT
            id, student_id, first_name, last_name, email,
            course, year_level, study_category, program_name, level_name,
            profile_photo, is_active
        FROM students
        WHERE id = %s
        LIMIT 1
        """,
        (student_db_id,),
    )
    if not student or not student.get("is_active"):
        session.clear()
        return redirect(url_for("web.student_login_page"))

    program_name = (student.get("program_name") or student.get("course") or "").strip()
    student_category = _normalize_study_category(student.get("study_category"))
    program_state = db_utils.fetch_one(
        """
        SELECT program_name, study_category, is_active
        FROM academic_programs
        WHERE LOWER(program_name) = LOWER(%s)
        LIMIT 1
        """,
        (program_name,),
    )
    level_name = (student.get("level_name") or student.get("year_level") or "").strip()
    all_program_courses = db_utils.fetch_all(
        """
        SELECT id, study_category, course_code, course_title, program_name, level_name, semester_no
        FROM program_level_courses
        WHERE is_active = TRUE
          AND LOWER(COALESCE(study_category, 'undergraduate')) = LOWER(%s)
          AND LOWER(program_name) = LOWER(%s)
        ORDER BY level_name ASC, COALESCE(semester_no, 1) ASC, course_code ASC
        """,
        (student_category, program_name),
    )
    available_courses = [
        c for c in all_program_courses
        if _student_can_access_course(
            program_name,
            level_name,
            c.get("level_name"),
            c.get("semester_no"),
        )
    ]
    grouped_available_courses = {}
    for row in available_courses:
        level_key = str(row.get("level_name") or "").strip() or "Unknown"
        semester_value = row.get("semester_no")
        semester_key = int(semester_value) if semester_value is not None else 1
        grouped_available_courses.setdefault(level_key, {}).setdefault(semester_key, []).append(row)
    registered_courses = db_utils.fetch_all(
        """
        SELECT id, course_code, course_title, level_name, semester_no, registered_at
        FROM student_course_registrations
        WHERE student_id = %s
        ORDER BY level_name ASC, COALESCE(semester_no, 1) ASC, course_code ASC, registered_at DESC
        """,
        (student_db_id,),
    )
    registered_course_codes = {
        str(row.get("course_code") or "").strip().upper()
        for row in registered_courses
        if row.get("course_code")
    }
    return render_template(
        "student/portal.html",
        title="Student Exam Course Registration",
        student=student,
        program_state=program_state,
        available_courses=available_courses,
        grouped_available_courses=grouped_available_courses,
        registered_courses=registered_courses,
        registered_course_codes=registered_course_codes,
        msg=request.args.get("msg"),
        err=request.args.get("err"),
    )


@web_bp.post("/student/courses/register")
@student_portal_required
def student_register_course():
    student_db_id = int(session.get("student_db_id"))
    student = db_utils.fetch_one(
        "SELECT id, study_category, course, year_level, program_name, level_name FROM students WHERE id = %s",
        (student_db_id,),
    )
    if not student:
        session.clear()
        return redirect(url_for("web.student_login_page"))

    course_id = request.form.get("course_id", type=int)
    course_code = str(request.form.get("course_code") or "").strip().upper()
    if not course_id and not course_code:
        return redirect(url_for("web.student_portal_page", err="Select a course to register."))

    student_category = _normalize_study_category(student.get("study_category"))
    student_program_name = student.get("program_name") or student.get("course")
    student_level_name = student.get("level_name") or student.get("year_level")

    if course_id:
        course_row = db_utils.fetch_one(
            """
            SELECT id, study_category, course_code, course_title, program_name, level_name, semester_no
            FROM program_level_courses
            WHERE is_active = TRUE
              AND id = %s
              AND LOWER(COALESCE(study_category, 'undergraduate')) = LOWER(%s)
              AND LOWER(program_name) = LOWER(%s)
            LIMIT 1
            """,
            (course_id, student_category, student_program_name),
        )
    else:
        course_row = db_utils.fetch_one(
            """
            SELECT id, study_category, course_code, course_title, program_name, level_name, semester_no
            FROM program_level_courses
            WHERE is_active = TRUE
              AND UPPER(course_code) = %s
              AND LOWER(COALESCE(study_category, 'undergraduate')) = LOWER(%s)
              AND LOWER(program_name) = LOWER(%s)
            ORDER BY level_name ASC, COALESCE(semester_no, 1) ASC
            LIMIT 1
            """,
            (course_code, student_category, student_program_name),
        )
    if not course_row:
        return redirect(url_for("web.student_portal_page", err="Selected course is not available for your program."))
    if not _student_can_access_course(
        student_program_name,
        student_level_name,
        course_row.get("level_name"),
        course_row.get("semester_no"),
    ):
        return redirect(
            url_for(
                "web.student_portal_page",
                err="You can only register courses in unlocked semesters and your current/previous levels.",
            )
        )

    existing = db_utils.fetch_one(
        """
        SELECT id
        FROM student_course_registrations
        WHERE student_id = %s AND UPPER(course_code) = %s
        """,
        (student_db_id, course_row.get("course_code")),
    )
    if existing:
        db_utils.execute(
            """
            UPDATE student_course_registrations
            SET program_name = %s,
                level_name = %s,
                semester_no = %s,
                course_title = %s,
                registered_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (
                course_row.get("program_name"),
                course_row.get("level_name"),
                course_row.get("semester_no"),
                course_row.get("course_title"),
                existing.get("id"),
            ),
        )
    else:
        db_utils.execute(
            """
            INSERT INTO student_course_registrations
                (student_id, program_name, level_name, semester_no, course_code, course_title)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                student_db_id,
                course_row.get("program_name"),
                course_row.get("level_name"),
                course_row.get("semester_no"),
                course_row.get("course_code"),
                course_row.get("course_title"),
            ),
        )
    return redirect(url_for("web.student_portal_page", msg=f"Registered {course_row.get('course_code')} successfully."))


@web_bp.post("/student/courses/unregister/<int:registration_id>")
@student_portal_required
def student_unregister_course_by_id(registration_id):
    student_db_id = int(session.get("student_db_id"))
    deleted = db_utils.execute(
        """
        DELETE FROM student_course_registrations
        WHERE id = %s AND student_id = %s
        """,
        (int(registration_id), student_db_id),
    )
    if deleted:
        return redirect(url_for("web.student_portal_page", msg="Removed selected course from your registered courses."))
    return redirect(url_for("web.student_portal_page", err="Selected registration was not found."))


@web_bp.post("/student/courses/unregister/<course_code>")
@student_portal_required
def student_unregister_course(course_code):
    student_db_id = int(session.get("student_db_id"))
    code = str(course_code or "").strip().upper()
    if not code:
        return redirect(url_for("web.student_portal_page", err="Invalid course code."))

    deleted = db_utils.execute(
        """
        DELETE FROM student_course_registrations
        WHERE student_id = %s AND UPPER(course_code) = %s
        """,
        (student_db_id, code),
    )
    if deleted:
        return redirect(url_for("web.student_portal_page", msg=f"Removed {code} from your registered courses."))
    return redirect(url_for("web.student_portal_page", err=f"{code} was not found in your registered courses."))


@web_bp.get("/student/logout")
@web_bp.get("/student/logout", endpoint="student_logout_page")
def student_logout():
    _record_logout_event()
    session.clear()
    return redirect(url_for("web.student_login_page"))

@web_bp.get("/dashboard")
@web_bp.get("/dashboard", endpoint="dashboard_page")
@login_required
def dashboard():
    if _has_role("lecturer"):
        admin_id = int(session.get("admin_id"))
        scope_rows = _lecturer_teaching_scope_rows(admin_id)
        assigned_course_map = {}
        for row in scope_rows:
            code = str(row.get("course_code") or "").strip().upper()
            if not code:
                continue
            title = str(row.get("course_title") or "").strip()
            if code not in assigned_course_map:
                assigned_course_map[code] = title
            elif not assigned_course_map[code] and title:
                assigned_course_map[code] = title
        assigned_course_codes = sorted(
            {
                str(r.get("course_code") or "").strip().upper()
                for r in scope_rows
                if str(r.get("course_code") or "").strip()
            }
        )
        assigned_courses = [
            {"course_code": code, "course_title": assigned_course_map.get(code) or ""}
            for code in assigned_course_codes
        ]
        department_names = sorted(
            {
                str(r.get("department_name") or "").strip()
                for r in scope_rows
                if str(r.get("department_name") or "").strip()
            }
        )
        program_names = sorted(
            {
                str(r.get("program_name") or "").strip()
                for r in scope_rows
                if str(r.get("program_name") or "").strip()
            }
        )
        level_names = sorted(
            {
                str(r.get("level_name") or "").strip()
                for r in scope_rows
                if str(r.get("level_name") or "").strip()
            }
        )

        today_class_count = db_utils.fetch_one(
            """
            SELECT COUNT(*) AS c
            FROM class_attendances
            WHERE lecturer_id = %s
              AND attendance_date = CURRENT_DATE
            """,
            (admin_id,),
        )
        upcoming_classes_count = db_utils.fetch_one(
            """
            SELECT COUNT(*) AS c
            FROM lecturer_courses
            WHERE lecturer_id = %s
              AND is_active = TRUE
            """,
            (admin_id,),
        )

        exam_verified_today = {"c": 0}
        recent_exam_activities = []
        if assigned_course_codes:
            placeholders = ", ".join(["%s"] * len(assigned_course_codes))
            exam_verified_today = db_utils.fetch_one(
                f"""
                SELECT COUNT(*) AS c
                FROM verification_logs vl
                INNER JOIN examination_sessions es ON es.id = vl.session_id
                WHERE DATE(vl.timestamp) = CURRENT_DATE
                  AND vl.outcome = 'SUCCESS'
                  AND UPPER(COALESCE(es.course_code, '')) IN ({placeholders})
                """,
                tuple(assigned_course_codes),
            )
            recent_exam_activities = db_utils.fetch_all(
                f"""
                SELECT
                    vl.timestamp,
                    vl.outcome,
                    s.student_id AS index_no,
                    s.first_name,
                    s.last_name,
                    COALESCE(es.course_code, 'N/A') AS course_code
                FROM verification_logs vl
                LEFT JOIN students s ON s.id = vl.student_id
                INNER JOIN examination_sessions es ON es.id = vl.session_id
                WHERE UPPER(COALESCE(es.course_code, '')) IN ({placeholders})
                ORDER BY vl.timestamp DESC
                LIMIT 10
                """,
                tuple(assigned_course_codes),
            )

        recent_class_rows = db_utils.fetch_all(
            """
            SELECT
                ca.timestamp,
                ca.course_code,
                s.student_id AS index_no,
                s.first_name,
                s.last_name
            FROM class_attendances ca
            INNER JOIN students s ON s.id = ca.student_id
            WHERE ca.lecturer_id = %s
            ORDER BY ca.timestamp DESC
            LIMIT 10
            """,
            (admin_id,),
        )

        return render_template(
            "dashboard/lecturer.html",
            title="Lecturer Dashboard",
            stats={
                "departments": len(department_names),
                "programs": len(program_names),
                "levels": len(level_names),
                "courses": len(assigned_course_codes),
                "class_marked_today": int((today_class_count or {}).get("c") or 0),
                "exam_verified_today": int((exam_verified_today or {}).get("c") or 0),
                "assigned_courses": int((upcoming_classes_count or {}).get("c") or 0),
            },
            departments=department_names,
            programs=program_names,
            levels=level_names,
            assigned_courses=assigned_courses,
            recent_class_rows=recent_class_rows,
            recent_exam_activities=recent_exam_activities,
        )

    try:
        student_row = db_utils.fetch_one("SELECT COUNT(*) AS c FROM students")
        sessions_row = db_utils.fetch_one(
            """
            SELECT COUNT(*) AS c
            FROM examination_sessions
            WHERE DATE(start_time) = CURRENT_DATE
            """
        )
        verified_row = db_utils.fetch_one(
            """
            SELECT COUNT(*) AS c
            FROM verification_logs
            WHERE DATE(timestamp) = CURRENT_DATE AND outcome = 'SUCCESS'
            """
        )
        failed_row = db_utils.fetch_one(
            """
            SELECT COUNT(*) AS c
            FROM verification_logs
            WHERE DATE(timestamp) = CURRENT_DATE AND outcome <> 'SUCCESS'
            """
        )
        stats = {
            "students": int((student_row or {}).get("c") or 0),
            "sessions": int((sessions_row or {}).get("c") or 0),
            "verified": int((verified_row or {}).get("c") or 0),
            "failed": int((failed_row or {}).get("c") or 0),
        }
        category_rows = db_utils.fetch_all(
            """
            SELECT LOWER(COALESCE(study_category, 'undergraduate')) AS category, COUNT(*) AS c
            FROM students
            GROUP BY LOWER(COALESCE(study_category, 'undergraduate'))
            ORDER BY category ASC
            """
        )
        category_breakdown = {
            "undergraduate": 0,
            "masters": 0,
            "phd": 0,
        }
        for row in category_rows:
            key = _normalize_study_category(row.get("category"))
            category_breakdown[key] = int(row.get("c") or 0)

        rows = db_utils.fetch_all(
            """
            SELECT
                vl.timestamp,
                vl.outcome,
                s.student_id AS index_no,
                s.first_name,
                s.last_name,
                COALESCE(es.course_code, s.course, 'N/A') AS course
            FROM verification_logs vl
            LEFT JOIN students s ON s.id = vl.student_id
            LEFT JOIN examination_sessions es ON es.id = vl.session_id
            ORDER BY vl.timestamp DESC
            LIMIT 20
            """
        )
        recent_activities = []
        for r in rows:
            ts = r.get("timestamp")
            first = (r.get("first_name") or "").strip()
            last = (r.get("last_name") or "").strip()
            name = f"{first} {last}".strip() or "Unknown Student"
            recent_activities.append(
                {
                    "time": ts.strftime("%H:%M:%S") if ts else "N/A",
                    "name": name,
                    "index": r.get("index_no") or "N/A",
                    "course": r.get("course") or "N/A",
                    "status": "Verified" if (r.get("outcome") == "SUCCESS") else "Failed",
                }
            )
    except Exception as exc:
        logger.warning(f"Dashboard data fallback used: {exc}")
        stats = {"students": 0, "sessions": 0, "verified": 0, "failed": 0}
        recent_activities = []
        category_breakdown = {"undergraduate": 0, "masters": 0, "phd": 0}

    return render_template(
        "dashboard/index.html",
        title="Dashboard",
        stats=stats,
        recent_activities=recent_activities,
        category_breakdown=category_breakdown,
    )

@web_bp.get("/students/register")
@web_bp.get("/students/register", endpoint="register_student_page")
@roles_required("admin", "super_admin")
def register_student_page():
    return render_template("students/register.html", title="Register Student")


@web_bp.post("/students/register")
@roles_required("admin", "super_admin")
def register_student_submit():
    try:
        data = request.get_json() or {}
        email = str(data.get("email") or "").strip()
        full_name = str(data.get("full_name") or "").strip()
        first_name = str(data.get("first_name") or "").strip()
        middle_name = str(data.get("middle_name") or "").strip()
        last_name = str(data.get("last_name") or "").strip()
        if full_name and (not first_name and not last_name):
            first_name, last_name = _split_name(full_name)
        if middle_name:
            first_name = f"{first_name} {middle_name}".strip()

        if not first_name:
            return jsonify({"error": "first_name is required"}), 400
        if not last_name:
            return jsonify({"error": "last_name is required"}), 400
        if not email:
            return jsonify({"error": "email is required"}), 400
        dob_raw = str(data.get("dob") or "").strip()
        if not dob_raw:
            return jsonify({"error": "date of birth is required"}), 400
        try:
            dob = _parse_iso_date(dob_raw)
        except Exception:
            return jsonify({"error": "Invalid date of birth format"}), 400
        today = datetime.utcnow().date()
        if dob >= today:
            return jsonify({"error": "Date of birth must be earlier than today"}), 400
        age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
        if age < 15:
            return jsonify({"error": "Student must be at least 15 years old"}), 400
        study_category = _normalize_study_category(data.get("study_category"))
        program_choice = str(data.get("program_name") or data.get("course") or "").strip()
        level_choice = str(data.get("level_name") or data.get("year_level") or "").strip()
        if not program_choice:
            return jsonify({"error": "program_name is required"}), 400
        if not level_choice:
            return jsonify({"error": "level_name is required"}), 400
        program_row = db_utils.fetch_one(
            """
            SELECT program_name, study_category, duration_years, is_active
            FROM academic_programs
            WHERE LOWER(program_name) = LOWER(%s)
            LIMIT 1
            """,
            (program_choice,),
        )
        if not program_row:
            return jsonify({"error": "Selected program is invalid"}), 400
        program_category = _normalize_study_category(program_row.get("study_category"))
        if study_category != program_category:
            return jsonify({"error": "Selected program does not match study_category"}), 400
        allowed_levels = set(_program_levels_by_category(program_category, program_row.get("duration_years")))
        if level_choice not in allowed_levels:
            return jsonify({"error": f"level_name must be one of: {', '.join(sorted(allowed_levels))}"}), 400

        entry_cohort = data.get("entry_cohort")
        expected_graduation_year = data.get("expected_graduation_year")
        try:
            entry_cohort = int(entry_cohort) if str(entry_cohort).strip() else None
        except Exception:
            return jsonify({"error": "entry_cohort must be a valid year"}), 400
        try:
            expected_graduation_year = int(expected_graduation_year) if str(expected_graduation_year).strip() else None
        except Exception:
            return jsonify({"error": "expected_graduation_year must be a valid year"}), 400
        current_year = _get_current_academic_year()
        if not current_year:
            return jsonify({"error": "No current academic year is set. Create or set the upcoming year first."}), 400

        if not bool(program_row.get("is_active")):
            return jsonify(
                {
                    "error": (
                        f"Program is unavailable for new enrollment in academic year "
                        f"{current_year.get('year_label')}."
                    )
                }
            ), 400

        if current_year and not bool(current_year.get("enrollment_open")):
            has_exception = _academic_year_program_exception_exists(current_year.get("id"), program_choice)
            if has_exception:
                pass
            else:
                return jsonify(
                    {
                        "error": (
                            f"Enrollment is currently closed for academic year "
                            f"{current_year.get('year_label')} for this program."
                        )
                    }
                ), 400

        raw_images = data.get("face_images") or []
        if not isinstance(raw_images, list) or len(raw_images) == 0:
            return jsonify({"error": "face_images is required"}), 400

        face_images = []
        profile_photo = None
        for raw in raw_images:
            img = _decode_b64_image(raw)
            if img:
                face_images.append(img)
                if not profile_photo:
                    profile_photo = _normalize_data_url_image(raw)
        if not face_images:
            return jsonify({"error": "No valid face images provided"}), 400

        _ensure_departments_schema()
        program_department = db_utils.fetch_one(
            """
            SELECT d.department_name
            FROM program_department_map m
            LEFT JOIN departments d ON d.id = m.department_id
            WHERE LOWER(m.program_name) = LOWER(%s)
            LIMIT 1
            """,
            (program_row.get("program_name"),),
        )
        derived_department = (program_department or {}).get("department_name")

        student_service = _get_student_service()
        success, result = student_service.register_student(
            {
                "first_name": first_name,
                "last_name": last_name,
                "email": email,
                "phone": data.get("phone"),
                "department": derived_department,
                "study_category": program_category,
                "course": program_row.get("program_name"),
                "year_level": level_choice,
                "program_name": program_row.get("program_name"),
                "level_name": level_choice,
                "entry_cohort": entry_cohort,
                "expected_graduation_year": expected_graduation_year,
                "admission_academic_year": current_year.get("year_label"),
                "date_of_birth": dob,
            },
            face_images,
            profile_photo=profile_photo,
        )
        if not success:
            return jsonify({"error": result}), 400
        temp_password = result.get("temporary_password") if isinstance(result, dict) else None
        return jsonify(
            {
                "message": "Student registered successfully",
                "student": result,
                "temporary_password": temp_password,
            }
        ), 201
    except Exception as exc:
        logger.error(f"Register student (web) failed: {exc}")
        return jsonify({"error": f"Registration failed: {exc}"}), 500

@web_bp.get("/exams/session")
@web_bp.get("/exams/session", endpoint="exam_session_page")
@roles_required("invigilator", "lecturer", "admin", "super_admin")
def exam_session_page():
    if _has_role("invigilator"):
        admin_id = session.get("admin_id")
        live_assignment = db_utils.fetch_one(
            """
            SELECT si.id
            FROM session_invigilators si
            INNER JOIN examination_sessions es ON es.id = si.session_id
            WHERE si.invigilator_id = %s
              AND si.is_active = TRUE
              AND es.is_active = TRUE
              AND CURRENT_TIMESTAMP BETWEEN es.start_time AND es.end_time
            LIMIT 1
            """,
            (admin_id,),
        )
        if not live_assignment:
            abort(403, description="Exam verification is unavailable until admin activates and assigns your session.")
    if _has_role("lecturer"):
        active_period = _get_current_open_exam_period()
        if not active_period:
            abort(403, description="Exam verification is unavailable. No active exam/midsem period is open.")

    now = datetime.utcnow()
    rows = db_utils.fetch_all(
        """
        SELECT id, session_name, course_code, venue, hall_id, start_time, end_time, is_active, allow_file_upload
        FROM examination_sessions
        ORDER BY start_time DESC
        """
    )
    if _has_role("lecturer"):
        admin_id = int(session.get("admin_id"))
        assigned_codes = set(_lecturer_course_codes(admin_id))
        rows = [
            r for r in rows
            if str(r.get("course_code") or "").strip().upper() in assigned_codes
        ]

    sessions = []
    for row in rows:
        start_time = row.get("start_time")
        end_time = row.get("end_time")
        is_active = bool(row.get("is_active"))

        if is_active and start_time and end_time and start_time <= now <= end_time:
            status = "live"
        elif start_time and now < start_time:
            status = "upcoming"
        elif end_time and now > end_time:
            status = "ended"
        else:
            status = "inactive"

        sessions.append(
            {
                "id": row.get("id"),
                "course_code": row.get("course_code"),
                "course": row.get("course_code") or row.get("session_name") or "Untitled",
                "title": row.get("session_name") or row.get("course_code") or "Untitled",
                "hall_id": row.get("hall_id"),
                "time": (
                    f"{start_time.strftime('%H:%M')} - {end_time.strftime('%H:%M')}"
                    if start_time and end_time
                    else "Time not set"
                ),
                "venue": row.get("venue") or "Venue not set",
                "status": status,
                "allow_file_upload": bool(row.get("allow_file_upload")),
            }
        )

    selected = None
    selected_id = request.args.get("session_id", type=int)
    if selected_id is not None:
        selected = next((s for s in sessions if s["id"] == selected_id), None)
    if selected is None and sessions:
        selected = sessions[0]

    registered = []
    selected_pause_state = {
        "verification_paused": False,
        "time_paused": False,
        "verification_reason": None,
        "time_reason": None,
    }
    if selected:
        pause_state = pause_controls.get_pause_state(
            int(selected["id"]),
            int(selected["hall_id"]) if selected.get("hall_id") is not None else None,
        )
        selected_pause_state = {
            "verification_paused": bool(pause_state.get("verification_paused")),
            "time_paused": bool(pause_state.get("time_paused")),
            "verification_reason": (pause_state.get("verification_pause") or {}).get("reason"),
            "time_reason": (pause_state.get("time_pause") or {}).get("reason"),
        }
        reg_rows = db_utils.fetch_all(
            """
            SELECT DISTINCT s.student_id, s.first_name, s.last_name
            FROM students s
            WHERE s.id IN (
                SELECT r.student_id
                FROM exam_registrations r
                WHERE r.session_id = %s
                UNION
                SELECT scr.student_id
                FROM student_course_registrations scr
                WHERE UPPER(scr.course_code) = UPPER(%s)
            )
            ORDER BY s.last_name ASC, s.first_name ASC
            """,
            (selected["id"], selected.get("course_code") or selected.get("course")),
        )
        registered = [
            {
                "index": row.get("student_id"),
                "name": f"{row.get('first_name') or ''} {row.get('last_name') or ''}".strip(),
            }
            for row in reg_rows
        ]

    return render_template(
        "exams/session.html",
        title="Live Verification",
        sessions=sessions,
        selected=selected,
        registered=registered,
        selected_pause_state=selected_pause_state,
        can_manage_pause=_has_role("admin", "super_admin"),
    )

@web_bp.get("/attendance/logs")
@web_bp.get("/attendance/logs", endpoint="attendance_logs_page")
@roles_required("invigilator", "lecturer", "admin", "super_admin")
def attendance_logs_page():
    if _has_role("lecturer", "invigilator"):
        active_period = _get_current_open_exam_period()
        if not active_period:
            abort(403, description="Exam attendance logs are unavailable. No active exam/midsem period is open.")

    session_id = request.args.get("session_id", type=int)
    outcome = str(request.args.get("outcome") or "").strip().upper()
    q = str(request.args.get("q") or "").strip()
    study_category = _optional_study_category(request.args.get("category"))
    program_name = str(request.args.get("program") or "").strip()
    level_name = str(request.args.get("level") or "").strip()
    page = max(1, request.args.get("page", default=1, type=int) or 1)
    per_page = request.args.get("per_page", default=100, type=int) or 100
    per_page = max(20, min(per_page, 300))

    where = []
    params = []
    if session_id:
        where.append("vl.session_id = %s")
        params.append(session_id)
    if outcome in {"SUCCESS", "FAIL"}:
        where.append("vl.outcome = %s")
        params.append(outcome)
    if q:
        where.append(
            """
            (
                LOWER(COALESCE(s.student_id, '')) LIKE LOWER(%s)
                OR LOWER(COALESCE(vl.claimed_student_id, '')) LIKE LOWER(%s)
                OR LOWER(CONCAT(COALESCE(s.first_name, ''), ' ', COALESCE(s.last_name, ''))) LIKE LOWER(%s)
                OR LOWER(COALESCE(es.course_code, '')) LIKE LOWER(%s)
                OR LOWER(COALESCE(es.session_name, '')) LIKE LOWER(%s)
                OR LOWER(COALESCE(ep.paper_title, '')) LIKE LOWER(%s)
            )
            """
        )
        like = f"%{q}%"
        params.extend([like, like, like, like, like, like])
    if study_category:
        where.append("LOWER(COALESCE(s.study_category, 'undergraduate')) = LOWER(%s)")
        params.append(study_category)
    if program_name:
        where.append("LOWER(COALESCE(s.program_name, s.course, '')) = LOWER(%s)")
        params.append(program_name)
    if level_name:
        where.append("LOWER(COALESCE(s.level_name, s.year_level, '')) = LOWER(%s)")
        params.append(level_name)

    if _has_role("lecturer"):
        admin_id = int(session.get("admin_id"))
        assigned_codes = _lecturer_course_codes(admin_id)
        if not assigned_codes:
            return render_template(
                "attendance/logs.html",
                rows=[],
                sessions=[],
                category_options=[],
                program_options=[],
                level_options=[],
                filters={
                    "session_id": session_id,
                    "outcome": outcome,
                    "q": q,
                    "category": study_category,
                    "program": program_name,
                    "level": level_name,
                    "per_page": per_page,
                },
                pagination={
                    "page": 1,
                    "per_page": per_page,
                    "total_count": 0,
                    "total_pages": 1,
                    "has_prev": False,
                    "has_next": False,
                    "prev_page": 1,
                    "next_page": 1,
                    "start_item": 0,
                    "end_item": 0,
                },
            )
        placeholders = ", ".join(["%s"] * len(assigned_codes))
        where.append(f"UPPER(COALESCE(es.course_code, '')) IN ({placeholders})")
        params.extend(assigned_codes)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    total_row = db_utils.fetch_one(
        f"""
        SELECT COUNT(*) AS c
        FROM verification_logs vl
        LEFT JOIN students s ON s.id = vl.student_id
        LEFT JOIN examination_sessions es ON es.id = vl.session_id
        LEFT JOIN (
            SELECT session_id, MIN(paper_title) AS paper_title
            FROM exam_papers
            GROUP BY session_id
        ) ep ON ep.session_id = es.id
        {where_sql}
        """,
        tuple(params),
    )
    total_count = int((total_row or {}).get("c") or 0)
    total_pages = max(1, math.ceil(total_count / per_page)) if total_count else 1
    page = min(page, total_pages)
    offset = (page - 1) * per_page

    page_params = list(params)
    page_params.extend([int(per_page), int(offset)])
    rows = db_utils.fetch_all(
        f"""
        SELECT
            vl.id,
            vl.session_id,
            vl.student_id,
            vl.claimed_student_id,
            vl.timestamp,
            vl.outcome,
            vl.reason,
            vl.confidence,
            vl.ip_address,
            vl.device_info,
            s.student_id AS index_no,
            s.first_name,
            s.last_name,
            es.session_name,
            es.course_code,
            es.venue,
            ep.paper_title
        FROM verification_logs vl
        LEFT JOIN students s ON s.id = vl.student_id
        LEFT JOIN examination_sessions es ON es.id = vl.session_id
        LEFT JOIN (
            SELECT session_id, MIN(paper_title) AS paper_title
            FROM exam_papers
            GROUP BY session_id
        ) ep ON ep.session_id = es.id
        {where_sql}
        ORDER BY vl.timestamp DESC
        LIMIT %s OFFSET %s
        """,
        tuple(page_params),
    )
    grouped_logs = {}
    for r in rows:
        session_key = r.get("session_id")
        if session_key is None:
            session_key = f"session-na-{r.get('session_name') or r.get('course_code') or 'N/A'}"
        session_label = r.get("course_code") or r.get("session_name") or "N/A"
        session_bucket = grouped_logs.setdefault(
            session_key,
            {
                "session_id": r.get("session_id"),
                "session_label": session_label,
                "session_name": r.get("session_name") or "N/A",
                "venue": r.get("venue") or "",
                "papers": {},
                "total": 0,
                "success": 0,
                "fail": 0,
            },
        )
        paper_title = r.get("paper_title") or "No Paper Title"
        paper_bucket = session_bucket["papers"].setdefault(
            paper_title,
            {
                "paper_title": paper_title,
                "rows": [],
                "total": 0,
                "success": 0,
                "fail": 0,
            },
        )
        paper_bucket["rows"].append(r)
        paper_bucket["total"] += 1
        session_bucket["total"] += 1
        if str(r.get("outcome") or "").upper() == "SUCCESS":
            paper_bucket["success"] += 1
            session_bucket["success"] += 1
        else:
            paper_bucket["fail"] += 1
            session_bucket["fail"] += 1

    grouped_sessions = sorted(
        grouped_logs.values(),
        key=lambda g: (
            0 if g.get("session_id") is not None else 1,
            -(int(g.get("session_id") or 0)),
            str(g.get("session_label") or ""),
        ),
    )
    for g in grouped_sessions:
        g["paper_groups"] = sorted(g["papers"].values(), key=lambda p: str(p.get("paper_title") or "").lower())
        del g["papers"]

    sessions = db_utils.fetch_all(
        """
        SELECT id, session_name, course_code, start_time
        FROM examination_sessions
        ORDER BY start_time DESC
        """
    )
    if _has_role("lecturer"):
        assigned_codes = set(_lecturer_course_codes(int(session.get("admin_id"))))
        sessions = [
            s for s in sessions
            if str(s.get("course_code") or "").strip().upper() in assigned_codes
        ]

    category_rows = db_utils.fetch_all(
        """
        SELECT DISTINCT LOWER(COALESCE(study_category, 'undergraduate')) AS category
        FROM students
        WHERE COALESCE(is_active, TRUE) = TRUE
        ORDER BY category ASC
        """
    )
    program_rows = db_utils.fetch_all(
        """
        SELECT DISTINCT COALESCE(program_name, course, '') AS program_name
        FROM students
        WHERE COALESCE(is_active, TRUE) = TRUE
          AND COALESCE(LTRIM(RTRIM(COALESCE(program_name, course, ''))), '') <> ''
        ORDER BY program_name ASC
        """
    )
    level_rows = db_utils.fetch_all(
        """
        SELECT DISTINCT COALESCE(level_name, year_level, '') AS level_name
        FROM students
        WHERE COALESCE(is_active, TRUE) = TRUE
          AND COALESCE(LTRIM(RTRIM(COALESCE(level_name, year_level, ''))), '') <> ''
        ORDER BY level_name ASC
        """
    )

    return render_template(
        "attendance/logs.html",
        rows=rows,
        grouped_sessions=grouped_sessions,
        sessions=sessions,
        category_options=[r.get("category") for r in category_rows if r.get("category")],
        program_options=[r.get("program_name") for r in program_rows if r.get("program_name")],
        level_options=[r.get("level_name") for r in level_rows if r.get("level_name")],
        filters={
            "session_id": session_id,
            "outcome": outcome,
            "q": q,
            "category": study_category,
            "program": program_name,
            "level": level_name,
            "per_page": per_page,
        },
        pagination={
            "page": page,
            "per_page": per_page,
            "total_count": total_count,
            "total_pages": total_pages,
            "has_prev": page > 1,
            "has_next": page < total_pages,
            "prev_page": page - 1,
            "next_page": page + 1,
            "start_item": (offset + 1) if total_count else 0,
            "end_item": min(offset + per_page, total_count),
        },
    )

@web_bp.get("/students")
@web_bp.get("/students", endpoint="students_directory_page")
@roles_required("admin", "super_admin")
def students_directory_page():
    return render_template("students/students_directory.html")


@web_bp.get("/verify/test")
@web_bp.get("/verification/test")
@web_bp.get("/verify/test", endpoint="verification_test_page")
@roles_required("invigilator", "lecturer", "admin", "super_admin")
def verification_test_page():
    return render_template("verification/test.html", title="Biometric Verification Test")


@web_bp.post("/stations/auto-key")
@roles_required("invigilator", "lecturer", "admin", "super_admin")
def auto_station_key():
    payload = request.get_json() or {}
    session_id = payload.get("session_id")
    try:
        session_id = int(session_id)
    except (TypeError, ValueError):
        return jsonify({"error": "session_id is required"}), 400

    exam_session = db_utils.fetch_one(
        "SELECT id, hall_id FROM examination_sessions WHERE id = %s",
        (session_id,),
    )
    if not exam_session:
        return jsonify({"error": "Session not found"}), 404

    admin_id = int(session.get("admin_id"))
    station_name = f"web-s{session_id}-a{admin_id}-{secrets.token_hex(4)}"
    raw_key = secrets.token_urlsafe(32)
    ip_whitelist = request.remote_addr or None

    station = db_utils.execute_returning(
        """
        INSERT INTO exam_stations (name, api_key_hash, hall_id, ip_whitelist, is_active)
        VALUES (%s, %s, %s, %s, TRUE)
        RETURNING id, name, hall_id, created_at
        """,
        (station_name, generate_password_hash(raw_key), exam_session.get("hall_id"), ip_whitelist),
    )
    return jsonify(
        {
            "message": "Station key prepared",
            "api_key": raw_key,
            "station": station,
        }
    ), 201


@web_bp.get("/class/attendance")
@web_bp.get("/class/attendance", endpoint="class_attendance_page")
@roles_required("lecturer", "admin", "super_admin")
def class_attendance_page():
    admin_id = session.get("admin_id")
    if _has_role("lecturer"):
        courses = _lecturer_courses(admin_id)
    else:
        courses = db_utils.fetch_all(
            """
            SELECT lc.course_code, lc.course_title, a.full_name AS lecturer_name
            FROM lecturer_courses lc
            LEFT JOIN admins a ON a.id = lc.lecturer_id
            WHERE lc.is_active = TRUE
            ORDER BY lc.course_code ASC
            """
        )
    return render_template(
        "attendance/class_session.html",
        title="Class Attendance",
        courses=courses,
    )


@web_bp.get("/lecturer/teaching-scope")
@web_bp.get("/lecturer/teaching-scope", endpoint="lecturer_teaching_scope_page")
@roles_required("lecturer")
def lecturer_teaching_scope_page():
    admin_id = int(session.get("admin_id"))
    rows = _lecturer_teaching_scope_rows(admin_id)
    departments = {
        str(r.get("department_name") or "").strip()
        for r in rows
        if str(r.get("department_name") or "").strip()
    }
    programs = {
        str(r.get("program_name") or "").strip()
        for r in rows
        if str(r.get("program_name") or "").strip()
    }
    levels = {
        str(r.get("level_name") or "").strip()
        for r in rows
        if str(r.get("level_name") or "").strip()
    }
    courses = {
        str(r.get("course_code") or "").strip().upper()
        for r in rows
        if str(r.get("course_code") or "").strip()
    }
    summary = {
        "departments": len(departments),
        "programs": len(programs),
        "levels": len(levels),
        "courses": len(courses),
    }
    return render_template(
        "lecturer/teaching_scope.html",
        title="My Teaching Scope",
        rows=rows,
        summary=summary,
    )


@web_bp.post("/class/attendance/verify")
@roles_required("lecturer", "admin", "super_admin")
def verify_class_attendance():
    try:
        payload = request.get_json() or {}
        student_identifier = str(payload.get("student_id") or "").strip()
        course_code = str(payload.get("course_code") or "").strip().upper()
        raw_image = payload.get("live_image")

        if not course_code:
            return jsonify({"error": "course_code is required"}), 400
        if not raw_image:
            return jsonify({"error": "live_image is required"}), 400

        admin_id = int(session.get("admin_id"))
        if _has_role("lecturer"):
            allowed = db_utils.fetch_one(
                """
                SELECT id FROM lecturer_courses
                WHERE lecturer_id = %s AND UPPER(course_code) = %s AND is_active = TRUE
                """,
                (admin_id, course_code),
            )
            if not allowed:
                return jsonify({"error": "You can only mark attendance for courses assigned to you"}), 403

        live_img = _decode_b64_image(raw_image)
        if live_img is None:
            return jsonify({"error": "Invalid live image"}), 400

        student_service = _get_student_service()
        student = None
        confidence = 0.0
        live_emb = student_service.face_engine.extract_live_embedding(live_img)
        if live_emb is None:
            return jsonify({"error": "No face detected in the captured image. Keep your full face centered and retry."}), 400
        if student_identifier:
            student = student_service.get_student(student_identifier)
            if not student:
                return jsonify({"error": "Student not found"}), 404
            if not student.get("is_active"):
                return jsonify({"error": "Student account is inactive"}), 400

            stored_encodings = student_service.get_face_encodings(student)
            if not stored_encodings:
                return jsonify({"error": "Student has no saved biometric templates"}), 400

            is_match, confidence = student_service.face_engine.verify_live_embedding(live_emb, stored_encodings)
            if not is_match:
                return jsonify({
                    "error": f"Identity verification failed. Confidence: {confidence:.2f}",
                    "confidence": float(confidence)
                }), 400
        else:
            cache = student_service.get_encoding_cache() or []
            if not cache:
                return jsonify({"error": "No enrolled students with biometric templates found"}), 404

            best_student = None
            best_confidence = -1.0
            best_attempt_confidence = 0.0
            for student_row, stored_encodings in cache:
                if not stored_encodings:
                    continue
                if not student_row.get("is_active"):
                    continue
                is_match, conf = student_service.face_engine.verify_live_embedding(live_emb, stored_encodings)
                best_attempt_confidence = max(best_attempt_confidence, float(conf))
                if is_match and conf > best_confidence:
                    best_student = student_row
                    best_confidence = float(conf)
            if not best_student:
                return jsonify({
                    "error": "No biometric match found among enrolled students. Recapture in better lighting or enter Student ID for 1:1 verification.",
                    "confidence": float(best_attempt_confidence),
                }), 400
            student = best_student
            confidence = best_confidence

        row = db_utils.execute_returning(
            """
            INSERT INTO class_attendances
                (student_id, course_code, lecturer_id, attendance_date, verification_confidence, ip_address, device_info)
            VALUES (%s, %s, %s, CURRENT_DATE, %s, %s, %s)
            ON CONFLICT (student_id, course_code, attendance_date)
            DO UPDATE SET
                lecturer_id = EXCLUDED.lecturer_id,
                verification_confidence = EXCLUDED.verification_confidence,
                timestamp = CURRENT_TIMESTAMP,
                ip_address = EXCLUDED.ip_address,
                device_info = EXCLUDED.device_info
            RETURNING *
            """,
            (
                student["id"],
                course_code,
                admin_id,
                float(confidence),
                request.remote_addr,
                request.headers.get("User-Agent", "Unknown"),
            ),
        )
        return jsonify(
            {
                "message": "Class attendance marked successfully",
                "attendance": {
                    "id": row.get("id"),
                    "course_code": row.get("course_code"),
                    "attendance_date": row.get("attendance_date").isoformat() if row.get("attendance_date") else None,
                    "timestamp": row.get("timestamp").isoformat() if row.get("timestamp") else None,
                    "confidence": row.get("verification_confidence"),
                },
                "student": student_service._student_to_dict(student),
                "confidence": float(confidence),
            }
        ), 200
    except Exception as exc:
        logger.error(f"Class attendance verify failed: {exc}")
        return jsonify({"error": f"Class attendance verification failed: {exc}"}), 500


@web_bp.get("/class/attendance/logs")
@web_bp.get("/class/attendance/logs", endpoint="class_attendance_logs_page")
@roles_required("lecturer", "admin", "super_admin")
def class_attendance_logs_page():
    admin_id = int(session.get("admin_id"))
    course_code = str(request.args.get("course_code") or "").strip().upper()
    page = max(1, request.args.get("page", default=1, type=int) or 1)
    per_page = request.args.get("per_page", default=100, type=int) or 100
    per_page = max(20, min(per_page, 300))

    where = []
    params = []

    if _has_role("lecturer"):
        where.append("ca.lecturer_id = %s")
        params.append(admin_id)
        assigned = _lecturer_courses(admin_id)
        if not assigned:
            rows = []
            courses = []
            return render_template(
                "attendance/class_logs.html",
                title="Class Attendance Logs",
                rows=rows,
                courses=courses,
                selected_course=course_code,
                pagination={
                    "page": 1,
                    "per_page": per_page,
                    "total_count": 0,
                    "total_pages": 1,
                    "has_prev": False,
                    "has_next": False,
                    "prev_page": 1,
                    "next_page": 1,
                    "start_item": 0,
                    "end_item": 0,
                },
            )
        if course_code:
            where.append("UPPER(ca.course_code) = %s")
            params.append(course_code)
        courses = assigned
    else:
        if course_code:
            where.append("UPPER(ca.course_code) = %s")
            params.append(course_code)
        courses = db_utils.fetch_all(
            "SELECT DISTINCT course_code, NULL::TEXT AS course_title FROM class_attendances ORDER BY course_code ASC"
        )

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    total_row = db_utils.fetch_one(
        f"""
        SELECT COUNT(*) AS c
        FROM class_attendances ca
        INNER JOIN students s ON s.id = ca.student_id
        LEFT JOIN admins a ON a.id = ca.lecturer_id
        {where_sql}
        """,
        tuple(params),
    )
    total_count = int((total_row or {}).get("c") or 0)
    total_pages = max(1, math.ceil(total_count / per_page)) if total_count else 1
    page = min(page, total_pages)
    offset = (page - 1) * per_page
    page_params = list(params)
    page_params.extend([int(per_page), int(offset)])
    rows = db_utils.fetch_all(
        f"""
        SELECT
            ca.id,
            ca.course_code,
            ca.attendance_date,
            ca.timestamp,
            ca.verification_confidence,
            s.student_id,
            s.first_name,
            s.last_name,
            a.full_name AS lecturer_name
        FROM class_attendances ca
        INNER JOIN students s ON s.id = ca.student_id
        LEFT JOIN admins a ON a.id = ca.lecturer_id
        {where_sql}
        ORDER BY ca.timestamp DESC
        LIMIT %s OFFSET %s
        """,
        tuple(page_params),
    )
    return render_template(
        "attendance/class_logs.html",
        title="Class Attendance Logs",
        rows=rows,
        courses=courses,
        selected_course=course_code,
        pagination={
            "page": page,
            "per_page": per_page,
            "total_count": total_count,
            "total_pages": total_pages,
            "has_prev": page > 1,
            "has_next": page < total_pages,
            "prev_page": page - 1,
            "next_page": page + 1,
            "start_item": (offset + 1) if total_count else 0,
            "end_item": min(offset + per_page, total_count),
        },
    )


@web_bp.get("/admin/system-status")
@web_bp.get("/admin/system-status", endpoint="system_status_page")
@roles_required("admin", "super_admin")
def system_status_page():
    db_status = {"state": "ok", "message": "Database reachable."}
    try:
        db_utils.fetch_one("SELECT 1 AS ok")
    except Exception as exc:
        logger.error(f"System status DB check failed: {exc}")
        db_status = {"state": "error", "message": str(exc)}

    sources = _collect_log_sources()
    source_names = [s.get("name") for s in sources if s.get("name")]
    selected_source = str(request.args.get("source") or "").strip()
    if selected_source not in source_names:
        selected_source = source_names[0] if source_names else ""

    limit_raw = request.args.get("limit", "200")
    try:
        limit = int(limit_raw)
    except (TypeError, ValueError):
        limit = 200
    limit = max(20, min(limit, 1000))

    selected_path = None
    for item in sources:
        if item.get("name") == selected_source:
            selected_path = item.get("path")
            break

    raw_lines = _tail_file_lines(selected_path, limit=limit) if selected_path else []
    numbered_lines = [{"line_no": idx + 1, "text": text} for idx, text in enumerate(raw_lines)]
    failure_lines = [row for row in numbered_lines if _looks_like_failure_line(row.get("text"))]
    failure_lines = failure_lines[-200:]

    return render_template(
        "admin/system_status.html",
        title="System Status",
        db_status=db_status,
        log_sources=sources,
        selected_source=selected_source,
        limit=limit,
        raw_lines=numbered_lines,
        failure_lines=failure_lines,
    )


@web_bp.get("/admin/exam-periods/manage")
@web_bp.get("/admin/exam-periods/manage", endpoint="exam_periods_page")
@roles_required("admin", "super_admin")
def exam_periods_page():
    periods = db_utils.fetch_all(
        """
        SELECT
            ep.id,
            ep.period_name,
            ep.period_type,
            ep.start_date,
            ep.end_date,
            ep.is_active,
            ep.created_at,
            a.full_name AS created_by_name
        FROM exam_periods ep
        LEFT JOIN admins a ON a.id = ep.created_by
        ORDER BY ep.start_date DESC, ep.id DESC
        """
    )
    active_period = _get_current_open_exam_period()
    return render_template(
        "admin/exam_periods.html",
        title="Exam Periods",
        periods=periods,
        active_period=active_period,
    )


@web_bp.post("/admin/exam-periods/save")
@roles_required("admin", "super_admin")
def save_exam_period():
    payload = request.get_json() or {}
    period_id_raw = payload.get("id")
    period_name = str(payload.get("period_name") or "").strip()
    period_type = str(payload.get("period_type") or "").strip().upper()
    start_date_raw = str(payload.get("start_date") or "").strip()
    end_date_raw = str(payload.get("end_date") or "").strip()
    is_active = bool(payload.get("is_active", True))

    if not period_name:
        return jsonify({"error": "period_name is required"}), 400
    if period_type not in {"EXAMS", "MIDSEM"}:
        return jsonify({"error": "period_type must be EXAMS or MIDSEM"}), 400
    try:
        start_date = _parse_iso_date(start_date_raw)
        end_date = _parse_iso_date(end_date_raw)
    except Exception:
        return jsonify({"error": "Invalid date format. Use YYYY-MM-DD"}), 400
    if end_date < start_date:
        return jsonify({"error": "end_date must be on or after start_date"}), 400

    admin_id = int(session.get("admin_id"))
    if period_id_raw in (None, "", 0, "0"):
        row = db_utils.execute_returning(
            """
            INSERT INTO exam_periods (period_name, period_type, start_date, end_date, is_active, created_by)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id, period_name, period_type, start_date, end_date, is_active, created_at
            """,
            (period_name, period_type, start_date, end_date, is_active, admin_id),
        )
        _record_system_event(
            "exam_period.created",
            entity_type="exam_period",
            entity_id=(row or {}).get("id"),
            details={
                "period_name": period_name,
                "period_type": period_type,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "is_active": bool(is_active),
            },
        )
        return jsonify({"message": "Exam period created", "period": row}), 201

    try:
        period_id = int(period_id_raw)
    except (TypeError, ValueError):
        return jsonify({"error": "id must be numeric"}), 400
    row = db_utils.execute_returning(
        """
        UPDATE exam_periods
        SET period_name = %s,
            period_type = %s,
            start_date = %s,
            end_date = %s,
            is_active = %s
        WHERE id = %s
        RETURNING id, period_name, period_type, start_date, end_date, is_active, created_at
        """,
        (period_name, period_type, start_date, end_date, is_active, period_id),
    )
    if not row:
        return jsonify({"error": "Exam period not found"}), 404
    _record_system_event(
        "exam_period.updated",
        entity_type="exam_period",
        entity_id=(row or {}).get("id"),
        details={
            "period_name": period_name,
            "period_type": period_type,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "is_active": bool(is_active),
        },
    )
    return jsonify({"message": "Exam period updated", "period": row}), 200


@web_bp.post("/admin/exam-periods/<int:period_id>/toggle")
@roles_required("admin", "super_admin")
def toggle_exam_period(period_id):
    payload = request.get_json() or {}
    if "is_active" not in payload:
        return jsonify({"error": "is_active is required"}), 400
    is_active = bool(payload.get("is_active"))
    row = db_utils.execute_returning(
        """
        UPDATE exam_periods
        SET is_active = %s
        WHERE id = %s
        RETURNING id, period_name, period_type, start_date, end_date, is_active
        """,
        (is_active, int(period_id)),
    )
    if not row:
        return jsonify({"error": "Exam period not found"}), 404
    _record_system_event(
        "exam_period.toggled",
        entity_type="exam_period",
        entity_id=(row or {}).get("id"),
        details={"is_active": bool(is_active)},
    )
    return jsonify({"message": "Exam period updated", "period": row}), 200


@web_bp.get("/admin/exam-periods/<int:period_id>/tracking")
@roles_required("admin", "super_admin")
def exam_period_tracking(period_id):
    payload = _get_exam_period_tracking_data(period_id)
    if not payload:
        return jsonify({"error": "Exam period not found"}), 404
    return jsonify(
        {
            "period": payload.get("period"),
            "summary": payload.get("summary", {}),
            "recent_logs": payload.get("recent_logs", []),
            "sessions": payload.get("sessions", []),
            "total_recent_logs": len(payload.get("recent_logs", [])),
        }
    ), 200


@web_bp.get("/admin/exam-periods/<int:period_id>/details")
@web_bp.get("/admin/exam-periods/<int:period_id>/details", endpoint="exam_period_detail_page")
@roles_required("admin", "super_admin")
def exam_period_detail_page(period_id):
    payload = _get_exam_period_tracking_data(period_id)
    if not payload:
        abort(404, description="Exam period not found.")
    return render_template(
        "admin/exam_period_detail.html",
        title="Exam Period Details",
        period=payload.get("period"),
        summary=payload.get("summary", {}),
        recent_logs=payload.get("recent_logs", []),
        sessions=payload.get("sessions", []),
        detailed_runs=payload.get("detailed_runs", []),
    )


@web_bp.get("/admin/exam-scheduler")
@web_bp.get("/admin/exam-scheduler", endpoint="exam_scheduler_page")
@roles_required("admin", "super_admin")
def exam_scheduler_page():
    periods = db_utils.fetch_all(
        """
        SELECT id, period_name, period_type, start_date, end_date, is_active
        FROM exam_periods
        ORDER BY start_date DESC, id DESC
        """
    )
    selected_period_id = request.args.get("period_id")
    selected_period = None
    if selected_period_id:
        selected_period = _get_exam_period_by_id(selected_period_id)
    if not selected_period:
        selected_period = _get_current_open_exam_period()
    if not selected_period and periods:
        selected_period = periods[0]

    available_invigilator_ids = []
    if selected_period and selected_period.get("id"):
        available_invigilator_ids = _fetch_period_available_invigilator_ids(int(selected_period["id"]))

    lecturers = db_utils.fetch_all(
        """
        SELECT id, username, email, full_name, role, is_active
        FROM admins
        WHERE role = 'lecturer' AND is_active = TRUE
        ORDER BY full_name ASC
        """
    )
    halls = db_utils.fetch_all(
        """
        SELECT id, name, capacity, is_active
        FROM exam_halls
        WHERE is_active = TRUE
        ORDER BY name ASC
        """
    )
    return render_template(
        "admin/exam_scheduler.html",
        title="Exam Scheduler",
        periods=periods,
        selected_period=selected_period,
        available_invigilator_ids=available_invigilator_ids,
        lecturers=lecturers,
        halls=halls,
        courses=_build_scheduler_course_catalog(),
    )


@web_bp.get("/admin/exam-scheduler/hall-usage")
@roles_required("admin", "super_admin")
def exam_scheduler_hall_usage():
    period_id = request.args.get("period_id")
    period = _get_exam_period_by_id(period_id) if period_id else None
    usage = _build_hall_usage_summary(period=period)
    return jsonify(
        {
            "period_id": int(period["id"]) if period and period.get("id") is not None else None,
            "period_name": period.get("period_name") if period else None,
            "hall_usage": usage,
        }
    ), 200


@web_bp.post("/admin/exam-periods/<int:period_id>/invigilator-availability")
@roles_required("admin", "super_admin")
def save_exam_period_invigilator_availability(period_id):
    period = _get_exam_period_by_id(period_id)
    if not period:
        return jsonify({"error": "Exam period not found"}), 404

    payload = request.get_json() or {}
    raw_ids = payload.get("invigilator_ids")
    if not isinstance(raw_ids, list):
        return jsonify({"error": "invigilator_ids must be a list"}), 400

    inv_ids = []
    for raw in raw_ids:
        try:
            inv_ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    inv_ids = sorted(set(inv_ids))

    valid_ids = []
    if inv_ids:
        placeholders = ", ".join(["%s"] * len(inv_ids))
        rows = db_utils.fetch_all(
            f"""
            SELECT id
            FROM admins
            WHERE role = 'lecturer'
              AND is_active = TRUE
              AND id IN ({placeholders})
            """,
            tuple(inv_ids),
        )
        valid_ids = sorted({int(r["id"]) for r in rows if r.get("id") is not None})

    db_utils.execute(
        "DELETE FROM exam_period_invigilator_availability WHERE exam_period_id = %s",
        (int(period_id),),
    )
    admin_id = int(session.get("admin_id"))
    for inv_id in valid_ids:
        db_utils.execute(
            """
            INSERT INTO exam_period_invigilator_availability
                (exam_period_id, invigilator_id, is_available, marked_by)
            VALUES (%s, %s, TRUE, %s)
            """,
            (int(period_id), int(inv_id), admin_id),
        )

    return jsonify(
        {
            "message": "Invigilator availability saved",
            "period_id": int(period_id),
            "available_count": len(valid_ids),
            "available_invigilator_ids": valid_ids,
        }
    ), 200


@web_bp.post("/admin/exam-scheduler/generate")
@roles_required("admin", "super_admin")
def generate_exam_schedule():
    payload = request.get_json() or {}

    period_id = payload.get("period_id")
    period = _get_exam_period_by_id(period_id)
    if not period:
        return jsonify({"error": "Valid period_id is required"}), 400

    period_start = period.get("start_date")
    period_end = period.get("end_date")
    if not period_start or not period_end:
        return jsonify({"error": "Selected exam period has no valid dates"}), 400

    raw_weekdays = payload.get("selected_weekdays")
    selected_weekdays = []
    if raw_weekdays is not None:
        if not isinstance(raw_weekdays, list):
            return jsonify({"error": "selected_weekdays must be a list of numbers (0=Mon ... 6=Sun)"}), 400
        for w in raw_weekdays:
            try:
                wi = int(w)
            except (TypeError, ValueError):
                return jsonify({"error": f"Invalid weekday value: {w}"}), 400
            if wi < 0 or wi > 6:
                return jsonify({"error": f"Weekday value out of range: {wi}. Use 0..6"}), 400
            selected_weekdays.append(wi)
        selected_weekdays = sorted(set(selected_weekdays))

    parsed_days = []
    if selected_weekdays:
        parsed_days = _expand_period_days_by_weekdays(period_start, period_end, selected_weekdays)
        if not parsed_days:
            return jsonify(
                {
                    "error": (
                        "No exam days match the selected weekdays inside this exam period. "
                        "Adjust weekdays or period dates."
                    )
                }
            ), 400
    else:
        raw_days = payload.get("exam_days")
        if not isinstance(raw_days, list) or not raw_days:
            return jsonify(
                {
                    "error": (
                        "Provide either exam_days or selected_weekdays. "
                        "exam_days must be a non-empty list."
                    )
                }
            ), 400
        for day in raw_days:
            try:
                parsed_days.append(_parse_iso_date(day))
            except Exception:
                return jsonify({"error": f"Invalid exam day: {day}. Use YYYY-MM-DD"}), 400
        parsed_days = sorted(set(parsed_days))
        for day in parsed_days:
            if day < period_start or day > period_end:
                return jsonify(
                    {
                        "error": (
                            f"Exam day {day.isoformat()} is outside period "
                            f"{period_start.isoformat()} to {period_end.isoformat()}."
                        )
                    }
                ), 400

    period_type = str(period.get("period_type") or "").strip().upper()
    default_hours_by_period = 1.0 if period_type == "MIDSEM" else 2.0
    raw_default_hours = payload.get("default_paper_duration_hours", default_hours_by_period)
    try:
        default_paper_duration_hours = float(raw_default_hours)
    except (TypeError, ValueError):
        return jsonify({"error": "default_paper_duration_hours must be numeric"}), 400
    if default_paper_duration_hours < 0.5 or default_paper_duration_hours > 8:
        return jsonify({"error": "default_paper_duration_hours must be between 0.5 and 8"}), 400

    exam_day_start_raw = str(payload.get("exam_day_start_time") or "").strip() or "00:00"
    exam_day_end_raw = str(payload.get("exam_day_end_time") or "").strip() or "23:59"
    try:
        exam_day_start_time = _parse_hhmm_time(exam_day_start_raw)
        exam_day_end_time = _parse_hhmm_time(exam_day_end_raw)
    except ValueError:
        return jsonify({"error": "exam_day_start_time and exam_day_end_time must be in HH:MM format"}), 400
    if datetime.combine(parsed_days[0], exam_day_end_time) <= datetime.combine(parsed_days[0], exam_day_start_time):
        return jsonify({"error": "exam_day_end_time must be later than exam_day_start_time"}), 400

    try:
        paper_gap_minutes = int(payload.get("paper_gap_minutes", payload.get("break_minutes", 15)))
    except (TypeError, ValueError):
        return jsonify({"error": "paper_gap_minutes must be numeric"}), 400
    if paper_gap_minutes < 0 or paper_gap_minutes > 180:
        return jsonify({"error": "paper_gap_minutes must be between 0 and 180"}), 400

    try:
        invigilator_cooldown_minutes = int(
            payload.get("invigilator_cooldown_minutes", payload.get("invigilator_break_minutes", 0))
        )
    except (TypeError, ValueError):
        return jsonify({"error": "invigilator_cooldown_minutes must be numeric"}), 400
    if invigilator_cooldown_minutes < 0 or invigilator_cooldown_minutes > 240:
        return jsonify({"error": "invigilator_cooldown_minutes must be between 0 and 240"}), 400

    try:
        fallback_invigilators_per_hall = int(payload.get("invigilators_per_hall", 1))
    except (TypeError, ValueError):
        return jsonify({"error": "invigilators_per_hall must be numeric"}), 400
    if fallback_invigilators_per_hall < 1 or fallback_invigilators_per_hall > 8:
        return jsonify({"error": "invigilators_per_hall must be between 1 and 8"}), 400

    shared_paper_hall_mode = str(payload.get("shared_paper_hall_mode") or "same_halls").strip().lower()
    if shared_paper_hall_mode not in {"same_halls", "separate_halls"}:
        return jsonify({"error": "shared_paper_hall_mode must be same_halls or separate_halls"}), 400

    hall_required_map = {}
    raw_hall_configs = payload.get("hall_configs")
    if isinstance(raw_hall_configs, list) and raw_hall_configs:
        for cfg in raw_hall_configs:
            if not isinstance(cfg, dict):
                continue
            try:
                hall_id = int(cfg.get("hall_id"))
                inv_required = int(cfg.get("invigilators_required"))
            except (TypeError, ValueError):
                continue
            if inv_required < 1 or inv_required > 8:
                return jsonify({"error": "Each hall invigilators_required must be between 1 and 8"}), 400
            hall_required_map[hall_id] = inv_required
    if hall_required_map:
        hall_ids = sorted(hall_required_map.keys())
    else:
        raw_hall_ids = payload.get("hall_ids")
        if not isinstance(raw_hall_ids, list) or not raw_hall_ids:
            return jsonify({"error": "hall_ids is required and must be a non-empty list"}), 400
        hall_ids = []
        for raw in raw_hall_ids:
            try:
                hall_ids.append(int(raw))
            except (TypeError, ValueError):
                continue
        hall_ids = sorted(set(hall_ids))
        for hid in hall_ids:
            hall_required_map[hid] = fallback_invigilators_per_hall

    if not hall_ids:
        return jsonify({"error": "No valid hall ids provided"}), 400
    hall_placeholders = ", ".join(["%s"] * len(hall_ids))
    hall_rows = db_utils.fetch_all(
        f"""
        SELECT id, name, capacity, is_active
        FROM exam_halls
        WHERE is_active = TRUE
          AND id IN ({hall_placeholders})
        ORDER BY id ASC
        """,
        tuple(hall_ids),
    )
    halls = []
    for h in hall_rows:
        try:
            halls.append(
                {
                    "id": int(h.get("id")),
                    "name": str(h.get("name") or "").strip(),
                    "capacity": int(h.get("capacity") or 0),
                }
            )
        except (TypeError, ValueError):
            continue
    if len(halls) != len(hall_ids):
        return jsonify({"error": "One or more selected halls are invalid or inactive"}), 400
    if any(int(h["capacity"]) <= 0 for h in halls):
        return jsonify({"error": "All selected halls must have capacity greater than zero"}), 400

    raw_courses = payload.get("courses")
    if not isinstance(raw_courses, list) or not raw_courses:
        return jsonify({"error": "courses is required and must be a non-empty list"}), 400
    normalized_courses = []
    for item in raw_courses:
        if not isinstance(item, dict):
            continue
        code = str(item.get("course_code") or "").strip().upper()
        if not code:
            continue
        duration_minutes = None
        if item.get("duration_hours") is not None:
            try:
                duration_hours = float(item.get("duration_hours"))
            except (TypeError, ValueError):
                return jsonify({"error": f"duration_hours must be numeric for course {code}"}), 400
            if duration_hours < 0.5 or duration_hours > 8:
                return jsonify({"error": f"duration_hours for {code} must be between 0.5 and 8"}), 400
            duration_minutes = int(round(duration_hours * 60))
        elif item.get("duration_minutes") is not None:
            try:
                duration_minutes = int(item.get("duration_minutes"))
            except (TypeError, ValueError):
                return jsonify({"error": f"duration_minutes must be numeric for course {code}"}), 400
        else:
            duration_minutes = int(round(default_paper_duration_hours * 60))

        if duration_minutes < 30 or duration_minutes > 480:
            return jsonify({"error": f"duration for {code} must be between 30 and 480 minutes"}), 400

        normalized_courses.append(
            {
                "course_code": code,
                "duration_minutes": duration_minutes,
                "duration_hours": round(float(duration_minutes) / 60.0, 2),
                "session_period": "day",
            }
        )
    if not normalized_courses:
        return jsonify({"error": "No valid courses were provided"}), 400

    deduped_by_code = {}
    for c in normalized_courses:
        deduped_by_code[c["course_code"]] = c
    normalized_courses = list(deduped_by_code.values())

    course_meta = _fetch_scheduler_course_details([c["course_code"] for c in normalized_courses])
    missing = [c["course_code"] for c in normalized_courses if c["course_code"] not in course_meta]
    if missing:
        return jsonify({"error": f"Invalid or inactive courses: {', '.join(sorted(missing))}"}), 400

    course_counts = _fetch_course_registration_counts([c["course_code"] for c in normalized_courses])
    course_breakdown = _fetch_course_registration_breakdown([c["course_code"] for c in normalized_courses])

    availability_ids = _fetch_period_available_invigilator_ids(int(period["id"]))
    if not availability_ids:
        return jsonify(
            {
                "error": (
                    "No available invigilators are marked for this exam period. "
                    "Mark availability first before generating schedule."
                )
            }
        ), 400

    slots = []
    daily_window_minutes = int(
        (datetime.combine(parsed_days[0], exam_day_end_time) - datetime.combine(parsed_days[0], exam_day_start_time))
        .total_seconds()
        / 60
    )
    for c in normalized_courses:
        if int(c["duration_minutes"]) > daily_window_minutes:
            return jsonify(
                {
                    "error": (
                        f"{c['course_code']} duration ({c['duration_minutes']} mins) exceeds daily window "
                        f"({daily_window_minutes} mins). Increase exam day window or reduce duration."
                    )
                }
            ), 409

    course_cursor = 0
    total_courses = len(normalized_courses)
    for exam_day in parsed_days:
        if course_cursor >= total_courses:
            break
        day_window_start = datetime.combine(exam_day, exam_day_start_time)
        day_window_end = datetime.combine(exam_day, exam_day_end_time)
        current_start = day_window_start

        while course_cursor < total_courses:
            c = normalized_courses[course_cursor]
            code = c["course_code"]
            slot_start = current_start
            slot_end = slot_start + timedelta(minutes=int(c["duration_minutes"]))
            if slot_end > day_window_end:
                # Move this paper to the next available exam day.
                break

            slots.append(
                {
                    "exam_day": exam_day,
                    "session_period": "day",
                    "course_code": code,
                    "course_title": course_meta.get(code, {}).get("course_title") or code,
                    "duration_minutes": int(c["duration_minutes"]),
                    "duration_hours": c["duration_hours"],
                    "start_time": slot_start,
                    "end_time": slot_end,
                    "paper_group_code": _default_paper_group_code(code, slot_start, "day"),
                }
            )
            course_cursor += 1
            current_start = slot_end + timedelta(minutes=paper_gap_minutes)

    if course_cursor < total_courses:
        remaining_codes = [c["course_code"] for c in normalized_courses[course_cursor:]]
        return jsonify(
            {
                "error": (
                    "Insufficient exam days/time window to schedule all papers. "
                    f"Unscheduled papers: {', '.join(remaining_codes)}. "
                    "Add more exam days, reduce paper durations, reduce gaps, or extend day end time."
                )
            }
        ), 409

    course_slot_counts = {}
    for slot in slots:
        code = slot["course_code"]
        course_slot_counts[code] = int(course_slot_counts.get(code, 0)) + 1

    def _distribute_students_across_halls(total_students, assigned_halls):
        if not assigned_halls:
            return {}, int(total_students or 0)
        if total_students <= 0:
            return {int(h["id"]): 0 for h in assigned_halls}, 0

        # Allocate by hall capacity so large candidate pools are spread across
        # all selected halls up to each hall's seat limit.
        out = {int(h["id"]): 0 for h in assigned_halls}
        remaining = int(total_students)
        for hall in sorted(assigned_halls, key=lambda h: int(h["capacity"]), reverse=True):
            if remaining <= 0:
                break
            cap = int(hall["capacity"])
            assigned = min(cap, remaining)
            out[int(hall["id"])] = max(0, int(assigned))
            remaining -= int(assigned)
        return out, remaining

    total_selected_capacity = sum(int(h["capacity"]) for h in halls)
    slot_hall_plans = []
    for slot in slots:
        code = slot["course_code"]
        course_total = int(course_counts.get(code) or 0)
        course_occurrences = max(1, int(course_slot_counts.get(code, 1)))
        slot_total_target = math.ceil(course_total / course_occurrences) if course_total > 0 else 0

        # default mode: same paper in same hall pool (current behavior)
        if shared_paper_hall_mode == "same_halls":
            expected_map, remaining = _distribute_students_across_halls(slot_total_target, halls)
            if remaining > 0:
                return jsonify(
                    {
                        "error": (
                            f"Hall capacity is insufficient for {code}. "
                            f"Needed at least {slot_total_target} seats, available {total_selected_capacity}."
                        )
                    }
                ), 409
            plan_entries = []
            for hall in halls:
                plan_entries.append(
                    {
                        "hall": hall,
                        "group_label": "",
                        "group_suffix": "",
                        "expected_students": int(expected_map.get(int(hall["id"]), 0)),
                    }
                )
            slot_hall_plans.append({"slot": slot, "entries": plan_entries})
            continue

        # separate_halls mode: split shared paper by program/level if multiple groups exist
        groups = [g for g in course_breakdown.get(code, []) if int(g.get("count") or 0) > 0]
        if len(groups) <= 1:
            expected_map, remaining = _distribute_students_across_halls(slot_total_target, halls)
            if remaining > 0:
                return jsonify(
                    {
                        "error": (
                            f"Hall capacity is insufficient for {code}. "
                            f"Needed at least {slot_total_target} seats, available {total_selected_capacity}."
                        )
                    }
                ), 409
            plan_entries = []
            for hall in halls:
                plan_entries.append(
                    {
                        "hall": hall,
                        "group_label": "",
                        "group_suffix": "",
                        "expected_students": int(expected_map.get(int(hall["id"]), 0)),
                    }
                )
            slot_hall_plans.append({"slot": slot, "entries": plan_entries})
            continue

        group_targets = []
        for g in groups:
            group_total = int(g["count"])
            group_slot_target = math.ceil(group_total / course_occurrences) if group_total > 0 else 0
            group_label = f"{g['program_name']} - {g['level_name']}"
            group_suffix = _normalize_paper_group_code(f"{g['program_name']}-{g['level_name']}")
            group_targets.append(
                {
                    "label": group_label,
                    "suffix": group_suffix or "GROUP",
                    "target": group_slot_target,
                }
            )
        group_targets = [g for g in group_targets if int(g["target"]) > 0]
        if not group_targets:
            expected_map, _remaining = _distribute_students_across_halls(slot_total_target, halls)
            slot_hall_plans.append(
                {
                    "slot": slot,
                    "entries": [
                        {
                            "hall": hall,
                            "group_label": "",
                            "group_suffix": "",
                            "expected_students": int(expected_map.get(int(hall["id"]), 0)),
                        }
                        for hall in halls
                    ],
                }
            )
            continue

        if len(group_targets) > len(halls):
            return jsonify(
                {
                    "error": (
                        f"{code} has {len(group_targets)} programme/level groups but only {len(halls)} halls selected. "
                        "Select more halls or use same_halls mode."
                    )
                }
            ), 409

        remaining_halls = sorted(halls, key=lambda h: int(h["capacity"]), reverse=True)
        group_to_halls = {}
        group_targets_sorted = sorted(group_targets, key=lambda g: int(g["target"]), reverse=True)
        for group in group_targets_sorted:
            assigned = []
            covered = 0
            while remaining_halls and (covered < int(group["target"]) or not assigned):
                hall = remaining_halls.pop(0)
                assigned.append(hall)
                covered += int(hall["capacity"])
            if not assigned or covered < int(group["target"]):
                return jsonify(
                    {
                        "error": (
                            f"Insufficient hall capacity to keep {code} groups separate "
                            f"for '{group['label']}'. Select more/larger halls or use same_halls mode."
                        )
                    }
                ), 409
            group_to_halls[group["label"]] = (group, assigned)

        # Any leftover halls are attached to the biggest group.
        if remaining_halls and group_targets_sorted:
            primary_label = group_targets_sorted[0]["label"]
            primary_group, primary_halls = group_to_halls[primary_label]
            primary_halls.extend(remaining_halls)
            group_to_halls[primary_label] = (primary_group, primary_halls)

        plan_entries = []
        for label in [g["label"] for g in group_targets_sorted]:
            group, assigned_halls = group_to_halls[label]
            expected_map, remaining = _distribute_students_across_halls(int(group["target"]), assigned_halls)
            if remaining > 0:
                return jsonify(
                    {
                        "error": (
                            f"Insufficient hall capacity for {code} group '{label}'. "
                            "Select more halls or reduce expected load."
                        )
                    }
                ), 409
            for hall in assigned_halls:
                plan_entries.append(
                    {
                        "hall": hall,
                        "group_label": label,
                        "group_suffix": group["suffix"],
                        "expected_students": int(expected_map.get(int(hall["id"]), 0)),
                    }
                )
        slot_hall_plans.append({"slot": slot, "entries": plan_entries})

    for plan in slot_hall_plans:
        slot = plan["slot"]
        planned_by_hall = {}
        hall_meta = {}
        for entry in plan["entries"]:
            expected = int(entry.get("expected_students") or 0)
            if expected <= 0:
                continue
            hall = entry["hall"]
            hall_id = int(hall["id"])
            planned_by_hall[hall_id] = planned_by_hall.get(hall_id, 0) + expected
            hall_meta[hall_id] = hall

        if not planned_by_hall:
            continue

        for hall_id, planned_expected in planned_by_hall.items():
            hall = hall_meta[hall_id]
            overlaps = db_utils.fetch_all(
                """
                SELECT id, session_name, expected_students
                FROM examination_sessions
                WHERE hall_id = %s
                  AND start_time < %s
                  AND end_time > %s
                ORDER BY start_time ASC
                """,
                (hall_id, slot["end_time"], slot["start_time"]),
            )
            existing_expected = sum(int(r.get("expected_students") or 0) for r in overlaps)
            hall_capacity = int(hall.get("capacity") or 0)
            if hall_capacity > 0 and (existing_expected + int(planned_expected)) > hall_capacity:
                overlap_names = ", ".join(str(r.get("session_name") or "N/A") for r in overlaps[:3])
                return jsonify(
                    {
                        "error": (
                            f"Hall capacity conflict on {slot['exam_day'].isoformat()} for {hall['name']}: "
                            f"existing overlap load {existing_expected}, new load {int(planned_expected)}, "
                            f"capacity {hall_capacity}. Overlapping sessions: {overlap_names or 'N/A'}."
                        )
                    }
                ), 409

    for plan in slot_hall_plans:
        slot = plan["slot"]
        plan_halls = [
            entry["hall"]
            for entry in plan["entries"]
            if int(entry.get("expected_students") or 0) > 0
        ]
        if not plan_halls:
            continue
        required_per_slot = sum(int(hall_required_map.get(int(h["id"]), 1)) for h in plan_halls)
        busy_ids = _fetch_busy_invigilator_ids(
            slot["start_time"],
            slot["end_time"],
            cooldown_minutes=invigilator_cooldown_minutes,
        )
        free_ids = [inv_id for inv_id in availability_ids if inv_id not in busy_ids]
        if len(free_ids) < required_per_slot:
            return jsonify(
                {
                    "error": (
                        f"Warning: insufficient available invigilators for {slot['course_code']} on "
                        f"{slot['exam_day'].isoformat()} ({slot['start_time'].strftime('%H:%M')}). "
                        f"Required: {required_per_slot}, available: {len(free_ids)}. "
                        f"Current invigilator cooldown: {invigilator_cooldown_minutes} minute(s)."
                    )
                }
            ), 409

    allow_file_upload = bool(payload.get("allow_file_upload", False))
    admin_id = int(session.get("admin_id"))
    created_sessions = []
    randomizer = random.SystemRandom()

    for plan in slot_hall_plans:
        slot = plan["slot"]
        plan_entries = [
            entry for entry in plan["entries"] if int(entry.get("expected_students") or 0) > 0
        ]
        if not plan_entries:
            continue
        plan_halls = [entry["hall"] for entry in plan_entries]
        busy_ids = _fetch_busy_invigilator_ids(
            slot["start_time"],
            slot["end_time"],
            cooldown_minutes=invigilator_cooldown_minutes,
        )
        free_ids = [inv_id for inv_id in availability_ids if inv_id not in busy_ids]
        randomizer.shuffle(free_ids)

        assignment_map = {}
        cursor = 0
        for hall in plan_halls:
            required_for_hall = int(hall_required_map.get(int(hall["id"]), 1))
            assigned = free_ids[cursor:cursor + required_for_hall]
            assignment_map[int(hall["id"])] = assigned
            cursor += required_for_hall

        for entry in plan_entries:
            hall = entry["hall"]
            hall_expected = max(1, min(int(entry["expected_students"]), int(hall["capacity"])))
            if int(entry["expected_students"]) <= 0:
                continue
            session_name = slot["course_title"]
            if entry["group_label"]:
                session_name = f"{slot['course_title']} ({entry['group_label']})"
            paper_group_code = slot["paper_group_code"]
            if entry["group_suffix"]:
                paper_group_code = f"{paper_group_code}-{entry['group_suffix']}"

            created = db_utils.execute_returning(
                """
                INSERT INTO examination_sessions
                    (session_name, course_code, paper_group_code, venue, hall_id, expected_students, start_time, end_time, created_by, is_active, allow_file_upload)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE, %s)
                RETURNING id, session_name, course_code, paper_group_code, venue, hall_id, expected_students, start_time, end_time, is_active
                """,
                (
                    session_name,
                    slot["course_code"],
                    paper_group_code,
                    hall["name"],
                    int(hall["id"]),
                    int(hall_expected),
                    slot["start_time"],
                    slot["end_time"],
                    admin_id,
                    allow_file_upload,
                ),
            )
            session_id = int(created.get("id"))
            db_utils.execute(
                """
                INSERT INTO exam_papers (session_id, paper_code, paper_title)
                VALUES (%s, %s, %s)
                """,
                (session_id, slot["course_code"], slot["course_title"]),
            )

            for inv_id in assignment_map.get(int(hall["id"]), []):
                db_utils.execute(
                    """
                    INSERT INTO session_invigilators (session_id, invigilator_id, assigned_by, is_active)
                    VALUES (%s, %s, %s, TRUE)
                    ON CONFLICT (session_id, invigilator_id)
                    DO UPDATE SET is_active = TRUE, assigned_by = EXCLUDED.assigned_by
                    """,
                    (session_id, int(inv_id), admin_id),
                )

            created_sessions.append(
                {
                    "session_id": session_id,
                    "course_code": slot["course_code"],
                    "course_title": slot["course_title"],
                    "session_period": slot["session_period"],
                    "duration_minutes": slot["duration_minutes"],
                    "duration_hours": slot["duration_hours"],
                    "exam_day": slot["exam_day"].isoformat(),
                    "start_time": slot["start_time"].isoformat(),
                    "end_time": slot["end_time"].isoformat(),
                    "hall_id": int(hall["id"]),
                    "hall_name": hall["name"],
                    "paper_group_code": paper_group_code,
                    "group_label": entry["group_label"] or None,
                    "required_invigilators": int(hall_required_map.get(int(hall["id"]), 1)),
                    "assigned_invigilator_ids": assignment_map.get(int(hall["id"]), []),
                }
            )

    return jsonify(
        {
            "message": "Exam schedule generated successfully",
            "period_id": int(period["id"]),
            "period_name": period.get("period_name"),
            "session_count": len(created_sessions),
            "slots_count": len(slots),
            "exam_days_count": len(parsed_days),
            "exam_days_used": [d.isoformat() for d in parsed_days],
            "selected_weekdays": selected_weekdays,
            "exam_day_start_time": exam_day_start_raw,
            "exam_day_end_time": exam_day_end_raw,
            "default_paper_duration_hours": default_paper_duration_hours,
            "paper_gap_minutes": paper_gap_minutes,
            "invigilator_cooldown_minutes": invigilator_cooldown_minutes,
            "shared_paper_hall_mode": shared_paper_hall_mode,
            # Backward-compatible response keys
            "break_minutes": paper_gap_minutes,
            "invigilator_break_minutes": invigilator_cooldown_minutes,
            "sessions": created_sessions,
        }
    ), 201


@web_bp.get("/admin/audit/login-logs")
@web_bp.get("/admin/audit/login-logs", endpoint="login_audit_logs_page")
@roles_required("admin", "super_admin")
def login_audit_logs_page():
    user_type = str(request.args.get("user_type") or "").strip().lower()
    q = str(request.args.get("q") or "").strip()
    page = max(1, request.args.get("page", default=1, type=int) or 1)
    per_page_raw = request.args.get("per_page", "100")
    try:
        per_page = int(per_page_raw)
    except (TypeError, ValueError):
        per_page = 100
    per_page = max(20, min(per_page, 300))

    where = []
    params = []
    if user_type in {"admin", "student"}:
        where.append("lal.user_type = %s")
        params.append(user_type)
    if q:
        query = f"%{q.lower()}%"
        where.append(
            "("
            "LOWER(COALESCE(lal.username, '')) LIKE %s OR "
            "LOWER(COALESCE(lal.email, '')) LIKE %s OR "
            "LOWER(COALESCE(lal.full_name, '')) LIKE %s"
            ")"
        )
        params.extend([query, query, query])

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    total_row = db_utils.fetch_one(
        f"""
        SELECT COUNT(*) AS c
        FROM login_audit_logs lal
        {where_sql}
        """,
        tuple(params),
    )
    total_count = int((total_row or {}).get("c") or 0)
    total_pages = max(1, math.ceil(total_count / per_page)) if total_count else 1
    page = min(page, total_pages)
    offset = (page - 1) * per_page

    page_items = []
    if total_pages <= 9:
        page_items = list(range(1, total_pages + 1))
    else:
        # Always show first/last and a focused window around current page.
        window_start = max(1, page - 2)
        window_end = min(total_pages, page + 2)
        page_items.append(1)
        if window_start > 2:
            page_items.append(None)
        for p in range(window_start, window_end + 1):
            if p not in (1, total_pages):
                page_items.append(p)
        if window_end < total_pages - 1:
            page_items.append(None)
        page_items.append(total_pages)

    page_params = list(params)
    page_params.extend([int(per_page), int(offset)])
    rows = db_utils.fetch_all(
        f"""
        SELECT
            lal.id,
            lal.user_type,
            lal.user_id,
            lal.username,
            lal.email,
            lal.full_name,
            lal.login_at,
            lal.logout_at,
            lal.ip_address,
            lal.user_agent
        FROM login_audit_logs lal
        {where_sql}
        ORDER BY lal.login_at DESC
        LIMIT %s OFFSET %s
        """,
        tuple(page_params),
    )
    return render_template(
        "admin/login_audit_logs.html",
        title="Login Audit Logs",
        rows=rows,
        filters={
            "user_type": user_type,
            "q": q,
            "per_page": per_page,
        },
        pagination={
            "page": page,
            "per_page": per_page,
            "total_count": total_count,
            "total_pages": total_pages,
            "has_prev": page > 1,
            "has_next": page < total_pages,
            "prev_page": page - 1,
            "next_page": page + 1,
            "first_page": 1,
            "last_page": total_pages,
            "page_items": page_items,
            "start_item": (offset + 1) if total_count else 0,
            "end_item": min(offset + per_page, total_count),
        },
    )


@web_bp.get("/admin/audit/events")
@web_bp.get("/admin/audit/events", endpoint="event_logs_page")
@roles_required("admin", "super_admin")
def event_logs_page():
    actor_type = str(request.args.get("actor_type") or "").strip().lower()
    action = str(request.args.get("action") or "").strip().lower()
    entity_type = str(request.args.get("entity_type") or "").strip().lower()
    q = str(request.args.get("q") or "").strip()
    page = max(1, request.args.get("page", default=1, type=int) or 1)
    per_page_raw = request.args.get("per_page", "100")
    try:
        per_page = int(per_page_raw)
    except (TypeError, ValueError):
        per_page = 100
    per_page = max(20, min(per_page, 300))

    where = []
    params = []
    if actor_type:
        where.append("LOWER(COALESCE(sel.actor_type, '')) = %s")
        params.append(actor_type)
    if action:
        where.append("LOWER(COALESCE(sel.action, '')) LIKE %s")
        params.append(f"%{action}%")
    if entity_type:
        where.append("LOWER(COALESCE(sel.entity_type, '')) = %s")
        params.append(entity_type)
    if q:
        q_like = f"%{q.lower()}%"
        where.append(
            "("
            "LOWER(COALESCE(sel.actor_username, '')) LIKE %s OR "
            "LOWER(COALESCE(sel.actor_email, '')) LIKE %s OR "
            "LOWER(COALESCE(sel.actor_full_name, '')) LIKE %s OR "
            "LOWER(COALESCE(sel.action, '')) LIKE %s OR "
            "LOWER(COALESCE(sel.entity_type, '')) LIKE %s OR "
            "LOWER(COALESCE(sel.entity_id, '')) LIKE %s OR "
            "LOWER(COALESCE(sel.details, '')) LIKE %s"
            ")"
        )
        params.extend([q_like, q_like, q_like, q_like, q_like, q_like, q_like])

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    total_row = db_utils.fetch_one(
        f"""
        SELECT COUNT(*) AS c
        FROM system_event_logs sel
        {where_sql}
        """,
        tuple(params),
    )
    total_count = int((total_row or {}).get("c") or 0)
    total_pages = max(1, math.ceil(total_count / per_page)) if total_count else 1
    page = min(page, total_pages)
    offset = (page - 1) * per_page

    page_params = list(params)
    page_params.extend([int(per_page), int(offset)])
    rows = db_utils.fetch_all(
        f"""
        SELECT
            sel.id,
            sel.actor_type,
            sel.actor_id,
            sel.actor_username,
            sel.actor_email,
            sel.actor_full_name,
            sel.action,
            sel.entity_type,
            sel.entity_id,
            sel.details,
            sel.ip_address,
            sel.user_agent,
            sel.created_at
        FROM system_event_logs sel
        {where_sql}
        ORDER BY sel.created_at DESC, sel.id DESC
        LIMIT %s OFFSET %s
        """,
        tuple(page_params),
    )

    return render_template(
        "admin/event_logs.html",
        title="Event Logs",
        rows=rows,
        filters={
            "actor_type": actor_type,
            "action": action,
            "entity_type": entity_type,
            "q": q,
            "per_page": per_page,
        },
        pagination={
            "page": page,
            "per_page": per_page,
            "total_count": total_count,
            "total_pages": total_pages,
            "has_prev": page > 1,
            "has_next": page < total_pages,
            "prev_page": page - 1,
            "next_page": page + 1,
            "start_item": (offset + 1) if total_count else 0,
            "end_item": min(offset + per_page, total_count),
        },
    )


@web_bp.get("/exams/sessions")
@web_bp.get("/exams/sessions", endpoint="all_sessions_page")
@roles_required("invigilator", "lecturer", "admin", "super_admin")
def all_sessions_page():
    if _has_role("lecturer", "invigilator"):
        active_period = _get_current_open_exam_period()
        if not active_period:
            abort(403, description="Sessions are unavailable. No active exam/midsem period is open.")

    page = max(1, int(request.args.get("page", 1) or 1))
    per_page = int(request.args.get("per_page", 25) or 25)
    per_page = max(10, min(per_page, 50))

    sessions = db_utils.fetch_all(
        """
        SELECT
            es.id,
            es.session_name,
            es.course_code,
            es.venue,
            es.start_time,
            es.end_time,
            es.is_active,
            (
                SELECT COUNT(DISTINCT x.student_id)
                FROM (
                    SELECT r.student_id
                    FROM exam_registrations r
                    WHERE r.session_id = es.id
                    UNION
                    SELECT scr.student_id
                    FROM student_course_registrations scr
                    WHERE UPPER(scr.course_code) = UPPER(es.course_code)
                ) x
            ) AS registered_count,
            (
                SELECT COUNT(*)
                FROM attendances a
                WHERE a.session_id = es.id
            ) AS verified_count,
            (
                SELECT COUNT(*)
                FROM session_invigilators si
                WHERE si.session_id = es.id
                  AND si.is_active = TRUE
            ) AS invigilator_count
        FROM examination_sessions es
        ORDER BY es.start_time DESC
        """
    )
    now = datetime.utcnow()
    normalized_sessions = []
    for s in sessions:
        start_time = s.get("start_time")
        end_time = s.get("end_time")
        is_active = bool(s.get("is_active"))
        if end_time and end_time <= now:
            status = "Ended"
        elif start_time and start_time > now:
            status = "Upcoming"
        elif is_active and start_time and end_time and start_time <= now <= end_time:
            status = "Live"
        elif is_active:
            status = "Live"
        else:
            status = "Inactive"
        s["status"] = status
        s["can_start"] = bool(status in {"Upcoming", "Inactive"})
        s["can_end"] = bool(is_active and status == "Live")
        normalized_sessions.append(s)
    sessions = normalized_sessions
    if _has_role("lecturer"):
        assigned_codes = set(_lecturer_course_codes(int(session.get("admin_id"))))
        sessions = [
            s for s in sessions
            if str(s.get("course_code") or "").strip().upper() in assigned_codes
        ]

    def _sort_key(row):
        start = row.get("start_time") or datetime.min
        return (start, int(row.get("id") or 0))

    sessions = sorted(sessions, key=_sort_key, reverse=True)

    total_count = len(sessions)
    total_pages = max(1, math.ceil(total_count / per_page)) if total_count else 1
    page = min(page, total_pages)
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    page_rows = sessions[start_idx:end_idx]

    return render_template(
        "exams/sessions_list.html",
        title="All Sessions",
        sessions=page_rows,
        pagination={
            "page": page,
            "per_page": per_page,
            "total_count": total_count,
            "total_pages": total_pages,
            "has_prev": page > 1,
            "has_next": page < total_pages,
            "prev_page": page - 1,
            "next_page": page + 1,
            "start_item": (start_idx + 1) if total_count else 0,
            "end_item": min(end_idx, total_count),
        },
    )


@web_bp.get("/exams/sessions/<int:session_id>")
@web_bp.get("/exams/sessions/<int:session_id>", endpoint="session_activity_page")
@roles_required("invigilator", "lecturer", "admin", "super_admin")
def session_activity_page(session_id):
    if _has_role("lecturer", "invigilator"):
        active_period = _get_current_open_exam_period()
        if not active_period:
            abort(403, description="Session activities are unavailable. No active exam/midsem period is open.")

    session_row = db_utils.fetch_one(
        """
        SELECT
            es.id,
            es.session_name,
            es.course_code,
            es.venue,
            es.start_time,
            es.end_time,
            es.is_active,
            es.hall_id
        FROM examination_sessions es
        WHERE es.id = %s
        LIMIT 1
        """,
        (int(session_id),),
    )
    if not session_row:
        abort(404, description="Session not found")

    if _has_role("lecturer"):
        assigned_codes = set(_lecturer_course_codes(int(session.get("admin_id"))))
        course_code = str(session_row.get("course_code") or "").strip().upper()
        if course_code not in assigned_codes:
            abort(403, description="You do not have access to this session.")

    summary = db_utils.fetch_one(
        """
        SELECT
            (
                SELECT COUNT(DISTINCT x.student_id)
                FROM (
                    SELECT r.student_id
                    FROM exam_registrations r
                    WHERE r.session_id = es.id
                    UNION
                    SELECT scr.student_id
                    FROM student_course_registrations scr
                    WHERE UPPER(scr.course_code) = UPPER(es.course_code)
                ) x
            ) AS registered_count,
            (
                SELECT COUNT(*)
                FROM attendances a
                WHERE a.session_id = es.id
            ) AS attendance_count,
            (
                SELECT COUNT(*)
                FROM verification_logs vl
                WHERE vl.session_id = es.id
            ) AS verification_total,
            (
                SELECT COUNT(*)
                FROM verification_logs vl
                WHERE vl.session_id = es.id
                  AND vl.outcome = 'SUCCESS'
            ) AS verification_success,
            (
                SELECT COUNT(*)
                FROM verification_logs vl
                WHERE vl.session_id = es.id
                  AND vl.outcome <> 'SUCCESS'
            ) AS verification_fail,
            (
                SELECT COUNT(*)
                FROM session_invigilators si
                WHERE si.session_id = es.id
                  AND si.is_active = TRUE
            ) AS invigilator_count
        FROM examination_sessions es
        WHERE es.id = %s
        LIMIT 1
        """,
        (int(session_id),),
    ) or {}

    invigilators = db_utils.fetch_all(
        """
        SELECT
            a.id,
            a.full_name,
            a.email,
            a.username,
            si.assigned_at
        FROM session_invigilators si
        INNER JOIN admins a ON a.id = si.invigilator_id
        WHERE si.session_id = %s
          AND si.is_active = TRUE
        ORDER BY a.full_name ASC
        """,
        (int(session_id),),
    )

    verification_rows = db_utils.fetch_all(
        """
        SELECT
            vl.id,
            vl.timestamp,
            vl.outcome,
            vl.reason,
            vl.confidence,
            vl.claimed_student_id,
            s.student_id AS index_no,
            s.first_name,
            s.last_name,
            st.name AS station_name
        FROM verification_logs vl
        LEFT JOIN students s ON s.id = vl.student_id
        LEFT JOIN exam_stations st ON st.id = vl.station_id
        WHERE vl.session_id = %s
        ORDER BY vl.timestamp DESC
        LIMIT 500
        """,
        (int(session_id),),
    )

    attendance_rows = db_utils.fetch_all(
        """
        SELECT
            a.id,
            a.timestamp,
            a.verification_confidence,
            s.student_id AS index_no,
            s.first_name,
            s.last_name
        FROM attendances a
        LEFT JOIN students s ON s.id = a.student_id
        WHERE a.session_id = %s
        ORDER BY a.timestamp DESC
        LIMIT 500
        """,
        (int(session_id),),
    )

    pause_rows = db_utils.fetch_all(
        """
        SELECT
            pc.id,
            pc.pause_type,
            pc.reason,
            pc.started_at,
            pc.resumed_at,
            pc.pause_seconds,
            hs.name AS hall_name,
            a1.full_name AS started_by_name,
            a2.full_name AS resumed_by_name
        FROM verification_pause_controls pc
        LEFT JOIN exam_halls hs ON hs.id = pc.hall_id
        LEFT JOIN admins a1 ON a1.id = pc.started_by
        LEFT JOIN admins a2 ON a2.id = pc.resumed_by
        WHERE pc.session_id = %s
        ORDER BY pc.started_at DESC
        LIMIT 500
        """,
        (int(session_id),),
    )

    activities = []
    for row in verification_rows:
        student_name = f"{row.get('first_name') or ''} {row.get('last_name') or ''}".strip() or "Unknown Student"
        outcome = str(row.get("outcome") or "FAIL").upper()
        activities.append(
            {
                "timestamp": row.get("timestamp"),
                "event_type": "verification",
                "badge": "SUCCESS" if outcome == "SUCCESS" else "FAIL",
                "title": f"Verification {outcome}",
                "details": (
                    f"{student_name} ({row.get('index_no') or row.get('claimed_student_id') or 'N/A'})"
                ),
                "meta": (
                    f"Confidence: {float(row.get('confidence') or 0.0):.2f}"
                    + (f" | Station: {row.get('station_name')}" if row.get("station_name") else "")
                    + (f" | Reason: {row.get('reason')}" if row.get("reason") else "")
                ),
            }
        )

    for row in attendance_rows:
        student_name = f"{row.get('first_name') or ''} {row.get('last_name') or ''}".strip() or "Unknown Student"
        activities.append(
            {
                "timestamp": row.get("timestamp"),
                "event_type": "attendance",
                "badge": "ATTENDANCE",
                "title": "Attendance recorded",
                "details": f"{student_name} ({row.get('index_no') or 'N/A'})",
                "meta": f"Verification confidence: {float(row.get('verification_confidence') or 0.0):.2f}",
            }
        )

    for row in pause_rows:
        scope = row.get("hall_name") or "Session-wide"
        started_by = row.get("started_by_name") or "Unknown"
        activities.append(
            {
                "timestamp": row.get("started_at"),
                "event_type": "pause_start",
                "badge": "PAUSE",
                "title": f"{str(row.get('pause_type') or 'verification').capitalize()} pause started",
                "details": f"Scope: {scope}",
                "meta": f"By: {started_by}" + (f" | Reason: {row.get('reason')}" if row.get("reason") else ""),
            }
        )
        if row.get("resumed_at"):
            resumed_by = row.get("resumed_by_name") or "Unknown"
            pause_secs = int(row.get("pause_seconds") or 0)
            activities.append(
                {
                    "timestamp": row.get("resumed_at"),
                    "event_type": "pause_resume",
                    "badge": "RESUME",
                    "title": f"{str(row.get('pause_type') or 'verification').capitalize()} pause resumed",
                    "details": f"Scope: {scope}",
                    "meta": f"By: {resumed_by} | Duration: {pause_secs}s",
                }
            )

    activities.sort(key=lambda x: x.get("timestamp") or datetime.min, reverse=True)

    return render_template(
        "exams/session_activity.html",
        title="Session Activity",
        session_row=session_row,
        summary=summary,
        invigilators=invigilators,
        verification_rows=verification_rows,
        pause_rows=pause_rows,
        activities=activities,
    )


@web_bp.post("/admin/sessions/<int:session_id>/start")
@roles_required("super_admin")
def start_session_web(session_id):
    row = db_utils.fetch_one(
        """
        SELECT id, session_name, course_code, venue, start_time, end_time, is_active
        FROM examination_sessions
        WHERE id = %s
        """,
        (session_id,),
    )
    if not row:
        return jsonify({"error": "Session not found"}), 404

    now = datetime.utcnow()
    end_time = row.get("end_time")
    if end_time and end_time <= now:
        return jsonify({"error": "Session end time has passed. Update/create a new session."}), 400

    start_time = row.get("start_time")
    effective_start = start_time if start_time and start_time <= now else now
    updated = db_utils.execute_returning(
        """
        UPDATE examination_sessions
        SET is_active = TRUE, start_time = %s
        WHERE id = %s
        RETURNING id, session_name, course_code, venue, start_time, end_time, is_active
        """,
        (effective_start, session_id),
    )
    _record_system_event(
        "exam_session.started",
        entity_type="exam_session",
        entity_id=session_id,
        details={"effective_start": effective_start.isoformat() if effective_start else None},
    )
    return jsonify({"message": "Session started", "session": updated}), 200


@web_bp.post("/admin/sessions/<int:session_id>/end")
@roles_required("super_admin")
def end_session_web(session_id):
    row = db_utils.fetch_one(
        """
        SELECT id, session_name, course_code, venue, start_time, end_time, is_active
        FROM examination_sessions
        WHERE id = %s
        """,
        (session_id,),
    )
    if not row:
        return jsonify({"error": "Session not found"}), 404

    now = datetime.utcnow()
    end_time = row.get("end_time")
    effective_end = now if (end_time is None or end_time > now) else end_time
    updated = db_utils.execute_returning(
        """
        UPDATE examination_sessions
        SET is_active = FALSE, end_time = %s
        WHERE id = %s
        RETURNING id, session_name, course_code, venue, start_time, end_time, is_active
        """,
        (effective_end, session_id),
    )
    _record_system_event(
        "exam_session.ended",
        entity_type="exam_session",
        entity_id=session_id,
        details={"effective_end": effective_end.isoformat() if effective_end else None},
    )
    return jsonify({"message": "Session ended", "session": updated}), 200


@web_bp.get("/admin/session-setup")
@web_bp.get("/admin/session-setup", endpoint="session_setup_page")
@roles_required("admin", "super_admin")
def session_setup_page():
    sessions = db_utils.fetch_all(
        """
        SELECT id, session_name, course_code, paper_group_code, venue, start_time, end_time, is_active
        FROM examination_sessions
        ORDER BY start_time DESC
        """
    )
    lecturers = db_utils.fetch_all(
        """
        SELECT id, username, email, full_name, role, is_active
        FROM admins
        WHERE role = 'lecturer' AND is_active = TRUE
        ORDER BY full_name ASC
        """
    )
    courses = db_utils.fetch_all(
        """
        SELECT course_code, MAX(course_title) AS course_title
        FROM program_level_courses
        WHERE is_active = TRUE
        GROUP BY course_code
        ORDER BY course_code ASC
        """
    )
    session_names = db_utils.fetch_all(
        """
        SELECT DISTINCT course_title
        FROM program_level_courses
        WHERE is_active = TRUE AND COALESCE(BTRIM(course_title), '') <> ''
        ORDER BY course_title ASC
        """
    )
    halls = db_utils.fetch_all(
        """
        SELECT id, name, capacity, is_active
        FROM exam_halls
        WHERE is_active = TRUE
        ORDER BY name ASC
        """
    )
    paper_groups = db_utils.fetch_all(
        """
        SELECT DISTINCT paper_group_code
        FROM examination_sessions
        WHERE paper_group_code IS NOT NULL AND paper_group_code <> ''
        ORDER BY paper_group_code ASC
        """
    )
    return render_template(
        "admin/session_setup.html",
        title="Session Setup",
        sessions=sessions,
        invigilators=lecturers,
        courses=courses,
        session_names=session_names,
        halls=halls,
        paper_groups=paper_groups,
    )


@web_bp.post("/admin/sessions/setup/create")
@roles_required("admin", "super_admin")
def create_session_setup():
    payload = request.get_json() or {}
    session_name = str(payload.get("session_name") or "").strip()
    if not session_name:
        return jsonify({"error": "session_name selection is required"}), 400
    valid_session_name = db_utils.fetch_one(
        """
        SELECT course_title
        FROM program_level_courses
        WHERE LOWER(course_title) = LOWER(%s) AND is_active = TRUE
        LIMIT 1
        """,
        (session_name,),
    )
    if not valid_session_name:
        return jsonify({"error": "Selected session name is invalid or inactive"}), 400

    course_code = str(payload.get("course_code") or "").strip().upper()
    if not course_code:
        return jsonify({"error": "course_code selection is required"}), 400

    course_row = db_utils.fetch_one(
        """
        SELECT course_code
        FROM program_level_courses
        WHERE UPPER(course_code) = %s AND is_active = TRUE
        LIMIT 1
        """,
        (course_code,),
    )
    if not course_row:
        return jsonify({"error": "Selected course code is invalid or inactive"}), 400

    hall_id = payload.get("hall_id")
    try:
        hall_id = int(hall_id)
    except (TypeError, ValueError):
        return jsonify({"error": "hall selection is required"}), 400
    hall = db_utils.fetch_one(
        """
        SELECT id, name, capacity, is_active
        FROM exam_halls
        WHERE id = %s
        LIMIT 1
        """,
        (hall_id,),
    )
    if not hall or not hall.get("is_active"):
        return jsonify({"error": "Selected hall is invalid or inactive"}), 400

    expected_students_raw = payload.get("expected_students")
    try:
        expected_students = int(expected_students_raw)
    except (TypeError, ValueError):
        return jsonify({"error": "expected_students must be a valid number"}), 400
    if expected_students <= 0:
        return jsonify({"error": "expected_students must be greater than zero"}), 400
    hall_capacity = int(hall.get("capacity") or 0)
    if hall_capacity <= 0:
        return jsonify({"error": "Selected hall has invalid capacity"}), 400
    if expected_students > hall_capacity:
        return jsonify(
            {
                "error": (
                    f"Expected students ({expected_students}) exceed hall capacity ({hall_capacity}) "
                    f"for {hall.get('name')}."
                )
            }
        ), 400

    start_raw = payload.get("start_time")
    end_raw = payload.get("end_time")
    if not start_raw or not end_raw:
        return jsonify({"error": "start_time and end_time are required"}), 400
    try:
        start_time = _parse_iso_utc_naive(start_raw)
        end_time = _parse_iso_utc_naive(end_raw)
    except Exception:
        return jsonify({"error": "Invalid date format"}), 400
    if end_time <= start_time:
        return jsonify({"error": "end_time must be after start_time"}), 400

    session_period = str(payload.get("session_period") or "").strip().lower()
    if session_period not in {"morning", "evening"}:
        return jsonify({"error": "session_period must be either morning or evening"}), 400
    if session_period == "morning" and int(start_time.hour) >= 12:
        return jsonify({"error": "Morning session must start before 12:00"}), 400
    if session_period == "evening" and int(start_time.hour) < 12:
        return jsonify({"error": "Evening session must start from 12:00 and above"}), 400

    provided_paper_group = _normalize_paper_group_code(payload.get("paper_group_code"))
    paper_group_code = provided_paper_group or _default_paper_group_code(course_code, start_time, session_period)
    if not paper_group_code:
        return jsonify({"error": "Could not determine paper_group_code"}), 400

    overlaps = db_utils.fetch_all(
        """
        SELECT id, session_name, expected_students
        FROM examination_sessions
        WHERE hall_id = %s
          AND start_time < %s
          AND end_time > %s
        ORDER BY start_time ASC
        """,
        (hall_id, end_time, start_time),
    )
    existing_expected = sum(int(r.get("expected_students") or 0) for r in overlaps)
    if hall_capacity > 0 and (existing_expected + expected_students) > hall_capacity:
        overlap_names = ", ".join(str(r.get("session_name") or "N/A") for r in overlaps[:3])
        return jsonify(
            {
                "error": (
                    f"Hall capacity conflict in '{hall.get('name')}'. "
                    f"Overlapping sessions currently use {existing_expected} seats; "
                    f"this session adds {expected_students}, exceeding capacity {hall_capacity}. "
                    f"Overlapping sessions: {overlap_names or 'N/A'}."
                )
            }
        ), 409

    admin_id = int(session.get("admin_id"))
    allow_file_upload = bool(payload.get("allow_file_upload"))
    created = db_utils.execute_returning(
        """
        INSERT INTO examination_sessions
            (session_name, course_code, paper_group_code, venue, hall_id, expected_students, start_time, end_time, created_by, is_active, allow_file_upload)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE, %s)
        RETURNING *
        """,
        (
            valid_session_name.get("course_title") or session_name,
            course_row.get("course_code"),
            paper_group_code,
            hall.get("name"),
            hall["id"],
            expected_students,
            start_time,
            end_time,
            admin_id,
            allow_file_upload,
        ),
    )

    papers = payload.get("papers") or []
    if isinstance(papers, list):
        for p in papers:
            if isinstance(p, str):
                title = p.strip()
                code = None
            else:
                title = str((p or {}).get("paper_title") or "").strip()
                code = str((p or {}).get("paper_code") or "").strip() or None
            if not title:
                continue
            db_utils.execute(
                "INSERT INTO exam_papers (session_id, paper_code, paper_title) VALUES (%s, %s, %s)",
                (created["id"], code, title),
            )

    raw_ids = payload.get("invigilator_ids") or []
    inv_ids = []
    if isinstance(raw_ids, list):
        for raw in raw_ids:
            try:
                inv_ids.append(int(raw))
            except (TypeError, ValueError):
                continue
    if inv_ids:
        valid_rows = db_utils.fetch_all(
            """
            SELECT id
            FROM admins
            WHERE id = ANY(%s) AND role = 'lecturer' AND is_active = TRUE
            """,
            (inv_ids,),
        )
        valid_ids = sorted({int(r["id"]) for r in valid_rows})
        for inv_id in valid_ids:
            db_utils.execute(
                """
                INSERT INTO session_invigilators (session_id, invigilator_id, assigned_by, is_active)
                VALUES (%s, %s, %s, TRUE)
                ON CONFLICT (session_id, invigilator_id)
                DO UPDATE SET is_active = TRUE, assigned_by = EXCLUDED.assigned_by
                """,
                (created["id"], inv_id, admin_id),
            )

    _record_system_event(
        "exam_session.created",
        entity_type="exam_session",
        entity_id=(created or {}).get("id"),
        details={
            "session_name": valid_session_name.get("course_title") or session_name,
            "course_code": course_row.get("course_code"),
            "paper_group_code": paper_group_code,
            "hall_id": hall.get("id"),
            "hall_name": hall.get("name"),
            "expected_students": expected_students,
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "allow_file_upload": bool(allow_file_upload),
            "papers_count": len(papers) if isinstance(papers, list) else 0,
            "requested_invigilators_count": len(inv_ids),
        },
    )
    return jsonify({"message": "Session created", "session": created}), 201


@web_bp.get("/admin/halls/setup")
@web_bp.get("/admin/halls/setup", endpoint="halls_setup_page")
@roles_required("admin", "super_admin")
def halls_setup_page():
    return render_template("admin/halls_setup.html", title="Exam Halls Setup")


@web_bp.get("/admin/halls")
@roles_required("admin", "super_admin")
def list_exam_halls():
    active_only = str(request.args.get("active_only") or "").strip().lower() in {"1", "true", "yes"}
    where_sql = "WHERE is_active = TRUE" if active_only else ""
    rows = db_utils.fetch_all(
        f"""
        SELECT id, name, capacity, is_active, created_at
        FROM exam_halls
        {where_sql}
        ORDER BY name ASC
        """
    )
    return jsonify({"halls": rows, "total": len(rows)}), 200


@web_bp.post("/admin/halls")
@roles_required("admin", "super_admin")
def create_exam_hall():
    payload = request.get_json() or {}
    name = str(payload.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    try:
        capacity = int(payload.get("capacity"))
    except (TypeError, ValueError):
        return jsonify({"error": "capacity must be a valid number"}), 400
    if capacity <= 0:
        return jsonify({"error": "capacity must be greater than zero"}), 400

    row = db_utils.execute_returning(
        """
        INSERT INTO exam_halls (name, capacity, is_active)
        VALUES (%s, %s, TRUE)
        ON CONFLICT (name)
        DO UPDATE SET capacity = EXCLUDED.capacity, is_active = TRUE
        RETURNING id, name, capacity, is_active, created_at
        """,
        (name, capacity),
    )
    _record_system_event(
        "exam_hall.saved",
        entity_type="exam_hall",
        entity_id=(row or {}).get("id"),
        details={"name": name, "capacity": capacity, "is_active": True},
    )
    return jsonify({"message": "Hall saved successfully", "hall": row}), 200


@web_bp.patch("/admin/halls/<int:hall_id>")
@roles_required("admin", "super_admin")
def update_exam_hall(hall_id):
    payload = request.get_json() or {}
    name = str(payload.get("name") or "").strip()
    capacity_raw = payload.get("capacity")
    is_active = payload.get("is_active")

    fields = []
    params = []
    if name:
        fields.append("name = %s")
        params.append(name)
    if capacity_raw is not None:
        try:
            capacity = int(capacity_raw)
        except (TypeError, ValueError):
            return jsonify({"error": "capacity must be a valid number"}), 400
        if capacity <= 0:
            return jsonify({"error": "capacity must be greater than zero"}), 400
        fields.append("capacity = %s")
        params.append(capacity)
    if is_active is not None:
        fields.append("is_active = %s")
        params.append(bool(is_active))

    if not fields:
        return jsonify({"error": "No valid fields provided"}), 400

    row = db_utils.execute_returning(
        f"""
        UPDATE exam_halls
        SET {", ".join(fields)}
        WHERE id = %s
        RETURNING id, name, capacity, is_active, created_at
        """,
        tuple(params + [hall_id]),
    )
    if not row:
        return jsonify({"error": "Hall not found"}), 404
    _record_system_event(
        "exam_hall.updated",
        entity_type="exam_hall",
        entity_id=(row or {}).get("id"),
        details={
            "name": row.get("name"),
            "capacity": row.get("capacity"),
            "is_active": bool(row.get("is_active")),
        },
    )
    return jsonify({"message": "Hall updated", "hall": row}), 200


@web_bp.get("/admin/sessions/<int:session_id>/setup-data")
@roles_required("admin", "super_admin")
def session_setup_data(session_id):
    row = db_utils.fetch_one(
        """
        SELECT id, session_name, course_code, paper_group_code, hall_id, venue, expected_students,
               start_time, end_time, is_active, allow_file_upload
        FROM examination_sessions
        WHERE id = %s
        """,
        (session_id,),
    )
    if not row:
        return jsonify({"error": "Session not found"}), 404
    papers = db_utils.fetch_all(
        "SELECT id, session_id, paper_code, paper_title, created_at FROM exam_papers WHERE session_id = %s ORDER BY created_at DESC",
        (session_id,),
    )
    invigilators = db_utils.fetch_all(
        """
        SELECT si.id, si.session_id, si.invigilator_id, si.assigned_at, si.assigned_by, si.is_active,
               a.full_name, a.username, a.email
        FROM session_invigilators si
        LEFT JOIN admins a ON a.id = si.invigilator_id
        WHERE si.session_id = %s AND si.is_active = TRUE
        ORDER BY si.assigned_at DESC
        """,
        (session_id,),
    )
    session_payload = {
        "id": row.get("id"),
        "session_name": row.get("session_name"),
        "course_code": row.get("course_code"),
        "paper_group_code": row.get("paper_group_code"),
        "hall_id": row.get("hall_id"),
        "venue": row.get("venue"),
        "expected_students": row.get("expected_students"),
        "start_time": row.get("start_time").isoformat() if row.get("start_time") else None,
        "end_time": row.get("end_time").isoformat() if row.get("end_time") else None,
        "is_active": bool(row.get("is_active")),
        "allow_file_upload": bool(row.get("allow_file_upload")),
    }
    return jsonify({"session": session_payload, "papers": papers, "invigilators": invigilators}), 200


@web_bp.post("/admin/sessions/<int:session_id>/setup-data/schedule")
@roles_required("admin", "super_admin")
def session_setup_reschedule(session_id):
    payload = request.get_json() or {}
    row = db_utils.fetch_one(
        """
        SELECT id, hall_id, venue, expected_students, start_time, end_time, is_active, allow_file_upload
        FROM examination_sessions
        WHERE id = %s
        LIMIT 1
        """,
        (session_id,),
    )
    if not row:
        return jsonify({"error": "Session not found"}), 404

    now = datetime.utcnow()
    start_existing = row.get("start_time")
    end_existing = row.get("end_time")
    is_live = bool(row.get("is_active") and start_existing and end_existing and start_existing <= now <= end_existing)
    if is_live:
        return jsonify({"error": "Cannot reschedule a live session. End it first."}), 409

    start_raw = payload.get("start_time")
    end_raw = payload.get("end_time")
    if not start_raw or not end_raw:
        return jsonify({"error": "start_time and end_time are required"}), 400
    try:
        start_time = _parse_iso_utc_naive(start_raw)
        end_time = _parse_iso_utc_naive(end_raw)
    except Exception:
        return jsonify({"error": "Invalid date format"}), 400
    if end_time <= start_time:
        return jsonify({"error": "end_time must be after start_time"}), 400

    hall_id_raw = payload.get("hall_id", row.get("hall_id"))
    try:
        hall_id = int(hall_id_raw)
    except (TypeError, ValueError):
        return jsonify({"error": "hall_id is required"}), 400
    hall = db_utils.fetch_one(
        "SELECT id, name, capacity, is_active FROM exam_halls WHERE id = %s LIMIT 1",
        (hall_id,),
    )
    if not hall or not hall.get("is_active"):
        return jsonify({"error": "Selected hall is invalid or inactive"}), 400

    expected_raw = payload.get("expected_students", row.get("expected_students"))
    try:
        expected_students = int(expected_raw)
    except (TypeError, ValueError):
        return jsonify({"error": "expected_students must be a valid number"}), 400
    if expected_students <= 0:
        return jsonify({"error": "expected_students must be greater than zero"}), 400
    hall_capacity = int(hall.get("capacity") or 0)
    if hall_capacity > 0 and expected_students > hall_capacity:
        return jsonify({"error": f"Expected students ({expected_students}) exceed hall capacity ({hall_capacity})."}), 400

    overlaps = db_utils.fetch_all(
        """
        SELECT id, session_name, expected_students
        FROM examination_sessions
        WHERE hall_id = %s
          AND id <> %s
          AND start_time < %s
          AND end_time > %s
        ORDER BY start_time ASC
        """,
        (hall_id, session_id, end_time, start_time),
    )
    existing_expected = sum(int(r.get("expected_students") or 0) for r in overlaps)
    if hall_capacity > 0 and (existing_expected + expected_students) > hall_capacity:
        overlap_names = ", ".join(str(r.get("session_name") or "N/A") for r in overlaps[:3])
        return jsonify(
            {
                "error": (
                    f"Hall capacity conflict in '{hall.get('name')}'. "
                    f"Overlapping sessions currently use {existing_expected} seats; "
                    f"this session would use {expected_students}, exceeding capacity {hall_capacity}. "
                    f"Overlapping sessions: {overlap_names or 'N/A'}."
                )
            }
        ), 409

    allow_file_upload = payload.get("allow_file_upload")
    if allow_file_upload is None:
        allow_file_upload = bool(row.get("allow_file_upload"))
    else:
        allow_file_upload = bool(allow_file_upload)

    updated = db_utils.execute_returning(
        """
        UPDATE examination_sessions
        SET hall_id = %s,
            venue = %s,
            expected_students = %s,
            start_time = %s,
            end_time = %s,
            allow_file_upload = %s,
            is_active = FALSE
        WHERE id = %s
        RETURNING id, session_name, course_code, paper_group_code, hall_id, venue, expected_students,
                  start_time, end_time, is_active, allow_file_upload
        """,
        (
            hall_id,
            hall.get("name"),
            expected_students,
            start_time,
            end_time,
            allow_file_upload,
            session_id,
        ),
    )
    _record_system_event(
        "exam_session.rescheduled",
        entity_type="exam_session",
        entity_id=(updated or {}).get("id") or session_id,
        details={
            "hall_id": hall_id,
            "hall_name": hall.get("name"),
            "expected_students": expected_students,
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "allow_file_upload": bool(allow_file_upload),
        },
    )
    return jsonify({"message": "Session rescheduled. It remains inactive until you start it.", "session": updated}), 200


@web_bp.post("/admin/sessions/<int:session_id>/setup-data/papers")
@roles_required("admin", "super_admin")
def session_setup_save_papers(session_id):
    payload = request.get_json() or {}
    papers = payload.get("papers")
    if not isinstance(papers, list):
        return jsonify({"error": "papers must be a list"}), 400
    exists = db_utils.fetch_one("SELECT id FROM examination_sessions WHERE id = %s", (session_id,))
    if not exists:
        return jsonify({"error": "Session not found"}), 404

    db_utils.execute("DELETE FROM exam_papers WHERE session_id = %s", (session_id,))
    for p in papers:
        if isinstance(p, str):
            title = p.strip()
            code = None
        else:
            title = str((p or {}).get("paper_title") or "").strip()
            code = str((p or {}).get("paper_code") or "").strip() or None
        if not title:
            continue
        db_utils.execute(
            "INSERT INTO exam_papers (session_id, paper_code, paper_title) VALUES (%s, %s, %s)",
            (session_id, code, title),
        )
    _record_system_event(
        "exam_session.papers_updated",
        entity_type="exam_session",
        entity_id=session_id,
        details={"papers_count": len(papers)},
    )
    return jsonify({"message": "Papers saved"}), 200


@web_bp.post("/admin/sessions/<int:session_id>/setup-data/invigilators")
@roles_required("admin", "super_admin")
def session_setup_save_invigilators(session_id):
    payload = request.get_json() or {}
    ids = payload.get("invigilator_ids")
    if not isinstance(ids, list):
        return jsonify({"error": "invigilator_ids must be a list"}), 400
    exists = db_utils.fetch_one("SELECT id FROM examination_sessions WHERE id = %s", (session_id,))
    if not exists:
        return jsonify({"error": "Session not found"}), 404

    inv_ids = []
    for raw in ids:
        try:
            inv_ids.append(int(raw))
        except (TypeError, ValueError):
            continue

    valid_rows = db_utils.fetch_all(
        """
        SELECT id
        FROM admins
        WHERE id = ANY(%s) AND role = 'lecturer' AND is_active = TRUE
        """,
        (inv_ids,),
    ) if inv_ids else []
    valid_ids = sorted({int(r["id"]) for r in valid_rows})

    db_utils.execute("DELETE FROM session_invigilators WHERE session_id = %s", (session_id,))
    admin_id = int(session.get("admin_id"))
    for inv_id in valid_ids:
        db_utils.execute(
            """
            INSERT INTO session_invigilators (session_id, invigilator_id, assigned_by, is_active)
            VALUES (%s, %s, %s, TRUE)
            """,
            (session_id, inv_id, admin_id),
        )
    _record_system_event(
        "exam_session.invigilators_updated",
        entity_type="exam_session",
        entity_id=session_id,
        details={"assigned_invigilator_ids": valid_ids, "assigned_count": len(valid_ids)},
    )
    return jsonify({"message": "Invigilators saved", "assigned_count": len(valid_ids)}), 200


@web_bp.get("/admin/courses/setup")
@web_bp.get("/admin/courses/setup", endpoint="course_catalog_page")
@roles_required("admin", "super_admin")
def course_catalog_page():
    return render_template("admin/course_catalog.html", title="Program Setup")


@web_bp.get("/admin/program-courses/add")
@web_bp.get("/admin/program-courses/add", endpoint="add_program_course_page")
@roles_required("admin", "super_admin")
def add_program_course_page():
    return render_template("admin/add_program_course.html", title="Add Program Course")


@web_bp.get("/admin/academic-years/manage")
@web_bp.get("/admin/academic-years/manage", endpoint="academic_years_page")
@roles_required("admin", "super_admin")
def academic_years_page():
    return render_template("admin/academic_years.html", title="Academic Years")


@web_bp.get("/admin/semester-control")
@web_bp.get("/admin/semester-control", endpoint="semester_control_page")
@roles_required("admin", "super_admin")
def semester_control_page():
    return render_template("admin/semester_control.html", title="Semester Control")


def _ensure_departments_schema():
    backend = get_database_backend()
    if backend == "sqlserver":
        db_utils.execute(
            """
            IF OBJECT_ID('departments', 'U') IS NULL
            CREATE TABLE departments (
                id INT IDENTITY(1,1) PRIMARY KEY,
                department_name NVARCHAR(120) NOT NULL UNIQUE,
                is_active BIT NOT NULL DEFAULT 1,
                created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
            )
            """
        )
        db_utils.execute(
            """
            IF OBJECT_ID('program_department_map', 'U') IS NULL
            CREATE TABLE program_department_map (
                id INT IDENTITY(1,1) PRIMARY KEY,
                program_name NVARCHAR(120) NOT NULL UNIQUE,
                department_id INT NULL,
                created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
                CONSTRAINT FK_program_department_map_department
                    FOREIGN KEY (department_id) REFERENCES departments(id)
            )
            """
        )
    else:
        db_utils.execute(
            """
            CREATE TABLE IF NOT EXISTS departments (
                id SERIAL PRIMARY KEY,
                department_name VARCHAR(120) NOT NULL UNIQUE,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        db_utils.execute(
            """
            CREATE TABLE IF NOT EXISTS program_department_map (
                id SERIAL PRIMARY KEY,
                program_name VARCHAR(120) NOT NULL UNIQUE,
                department_id INTEGER NULL REFERENCES departments(id) ON DELETE SET NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


@web_bp.get("/admin/departments/manage")
@web_bp.get("/admin/departments/manage", endpoint="departments_page")
@roles_required("admin", "super_admin")
def departments_page():
    return render_template("admin/departments.html", title="Departments & Programmes")


@web_bp.get("/admin/departments")
@roles_required("admin", "super_admin")
def list_departments():
    _ensure_departments_schema()
    dept_rows = db_utils.fetch_all(
        """
        SELECT id, department_name, is_active
        FROM departments
        ORDER BY department_name ASC
        """
    )
    program_rows = db_utils.fetch_all(
        """
        SELECT
            p.id,
            p.program_name AS programme_name,
            p.duration_years,
            p.is_active,
            m.department_id
        FROM academic_programs p
        LEFT JOIN program_department_map m
            ON LOWER(m.program_name) = LOWER(p.program_name)
        ORDER BY p.program_name ASC
        """
    )

    grouped = []
    for d in dept_rows:
        programmes = [
            {
                "id": r.get("id"),
                "programme_name": r.get("programme_name"),
                "duration_years": int(r.get("duration_years") or 4),
                "is_active": bool(r.get("is_active", True)),
            }
            for r in program_rows
            if r.get("department_id") == d.get("id")
        ]
        grouped.append(
            {
                "id": d.get("id"),
                "department_name": d.get("department_name"),
                "is_active": bool(d.get("is_active", True)),
                "is_virtual": False,
                "programmes": programmes,
            }
        )

    unassigned_programmes = [
        {
            "id": r.get("id"),
            "programme_name": r.get("programme_name"),
            "duration_years": int(r.get("duration_years") or 4),
            "is_active": bool(r.get("is_active", True)),
        }
        for r in program_rows
        if not r.get("department_id")
    ]
    if unassigned_programmes:
        grouped.append(
            {
                "id": "",
                "department_name": "Unassigned Programmes",
                "is_active": True,
                "is_virtual": True,
                "programmes": unassigned_programmes,
            }
        )

    return jsonify({"departments": grouped}), 200


@web_bp.post("/admin/departments")
@roles_required("admin", "super_admin")
def create_department():
    _ensure_departments_schema()
    payload = request.get_json() or {}
    name = str(payload.get("department_name") or "").strip()
    if not name:
        return jsonify({"error": "department_name is required"}), 400

    exists = db_utils.fetch_one(
        "SELECT id FROM departments WHERE LOWER(department_name) = LOWER(%s) LIMIT 1",
        (name,),
    )
    if exists:
        return jsonify({"error": "Department already exists"}), 400

    row = db_utils.execute_returning(
        """
        INSERT INTO departments (department_name, is_active)
        VALUES (%s, TRUE)
        RETURNING id, department_name, is_active
        """,
        (name,),
    )
    return jsonify({"message": "Department added", "department": row}), 201


@web_bp.put("/admin/departments/<int:department_id>")
@roles_required("admin", "super_admin")
def update_department(department_id):
    _ensure_departments_schema()
    payload = request.get_json() or {}
    name = str(payload.get("department_name") or "").strip()
    if not name:
        return jsonify({"error": "department_name is required"}), 400

    row = db_utils.fetch_one("SELECT id FROM departments WHERE id = %s LIMIT 1", (department_id,))
    if not row:
        return jsonify({"error": "Department not found"}), 404

    conflict = db_utils.fetch_one(
        "SELECT id FROM departments WHERE LOWER(department_name) = LOWER(%s) AND id <> %s LIMIT 1",
        (name, department_id),
    )
    if conflict:
        return jsonify({"error": "Department name already used"}), 400

    db_utils.execute(
        "UPDATE departments SET department_name = %s WHERE id = %s",
        (name, department_id),
    )
    updated = db_utils.fetch_one(
        "SELECT id, department_name, is_active FROM departments WHERE id = %s",
        (department_id,),
    )
    return jsonify({"message": "Department updated", "department": updated}), 200


@web_bp.get("/admin/programmes")
@roles_required("admin", "super_admin")
def list_programmes_by_department():
    _ensure_departments_schema()
    rows = db_utils.fetch_all(
        """
        SELECT
            p.id,
            p.program_name AS programme_name,
            p.duration_years,
            p.is_active,
            d.id AS department_id,
            d.department_name
        FROM academic_programs p
        LEFT JOIN program_department_map m
            ON LOWER(m.program_name) = LOWER(p.program_name)
        LEFT JOIN departments d
            ON d.id = m.department_id
        ORDER BY d.department_name ASC, p.program_name ASC
        """
    )
    return jsonify({"programmes": rows}), 200


@web_bp.post("/admin/programmes")
@roles_required("admin", "super_admin")
def create_programme_with_department():
    _ensure_departments_schema()
    payload = request.get_json() or {}
    programme_name = str(payload.get("programme_name") or "").strip()
    department_id = payload.get("department_id")
    duration_years = int(payload.get("duration_years") or 4)
    if not programme_name:
        return jsonify({"error": "programme_name is required"}), 400
    if not department_id:
        return jsonify({"error": "department_id is required"}), 400
    try:
        department_id = int(department_id)
    except (TypeError, ValueError):
        return jsonify({"error": "department_id must be numeric"}), 400

    if duration_years < 1 or duration_years > 10:
        return jsonify({"error": "duration_years must be between 1 and 10"}), 400

    dep = db_utils.fetch_one("SELECT id FROM departments WHERE id = %s", (department_id,))
    if not dep:
        return jsonify({"error": "Department not found"}), 404

    existing = db_utils.fetch_one(
        "SELECT id FROM academic_programs WHERE LOWER(program_name) = LOWER(%s) LIMIT 1",
        (programme_name,),
    )
    if existing:
        return jsonify({"error": "Programme already exists"}), 400

    row = db_utils.execute_returning(
        """
        INSERT INTO academic_programs (program_name, duration_years, semesters_per_year, is_active)
        VALUES (%s, %s, 2, TRUE)
        RETURNING id, program_name, duration_years, is_active
        """,
        (programme_name, duration_years),
    )
    db_utils.execute(
        """
        INSERT INTO program_department_map (program_name, department_id)
        VALUES (%s, %s)
        """,
        (programme_name, department_id),
    )
    return jsonify({"message": "Programme added", "programme": row}), 201


@web_bp.put("/admin/programmes/<int:programme_id>")
@roles_required("admin", "super_admin")
def update_programme_department(programme_id):
    _ensure_departments_schema()
    payload = request.get_json() or {}
    programme_name = str(payload.get("programme_name") or "").strip()
    department_id = payload.get("department_id")
    duration_years = int(payload.get("duration_years") or 4)

    if not programme_name:
        return jsonify({"error": "programme_name is required"}), 400
    if not department_id:
        return jsonify({"error": "department_id is required"}), 400
    try:
        department_id = int(department_id)
    except (TypeError, ValueError):
        return jsonify({"error": "department_id must be numeric"}), 400
    if duration_years < 1 or duration_years > 10:
        return jsonify({"error": "duration_years must be between 1 and 10"}), 400

    prog = db_utils.fetch_one(
        "SELECT id, program_name FROM academic_programs WHERE id = %s LIMIT 1",
        (programme_id,),
    )
    if not prog:
        return jsonify({"error": "Programme not found"}), 404

    dep = db_utils.fetch_one("SELECT id FROM departments WHERE id = %s LIMIT 1", (department_id,))
    if not dep:
        return jsonify({"error": "Department not found"}), 404

    name_conflict = db_utils.fetch_one(
        """
        SELECT id
        FROM academic_programs
        WHERE LOWER(program_name) = LOWER(%s) AND id <> %s
        LIMIT 1
        """,
        (programme_name, programme_id),
    )
    if name_conflict:
        return jsonify({"error": "Programme name already used"}), 400

    old_program_name = str(prog.get("program_name") or "").strip()
    db_utils.execute(
        """
        UPDATE academic_programs
        SET program_name = %s, duration_years = %s
        WHERE id = %s
        """,
        (programme_name, duration_years, programme_id),
    )
    db_utils.execute(
        """
        DELETE FROM program_department_map
        WHERE LOWER(program_name) = LOWER(%s)
        """,
        (old_program_name,),
    )
    db_utils.execute(
        """
        INSERT INTO program_department_map (program_name, department_id)
        VALUES (%s, %s)
        """,
        (programme_name, department_id),
    )

    updated = db_utils.fetch_one(
        """
        SELECT id, program_name, duration_years, is_active
        FROM academic_programs
        WHERE id = %s
        """,
        (programme_id,),
    )
    return jsonify({"message": "Programme updated", "programme": updated}), 200


@web_bp.get("/admin/courses")
@roles_required("admin", "super_admin")
def list_program_level_courses():
    study_category = _optional_study_category(request.args.get("category"))
    program = str(request.args.get("program") or "").strip()
    level = str(request.args.get("level") or "").strip()
    semester = request.args.get("semester", type=int)
    where = ["is_active = TRUE"]
    params = []
    if study_category:
        where.append("LOWER(COALESCE(study_category, 'undergraduate')) = LOWER(%s)")
        params.append(study_category)
    if program:
        where.append("LOWER(program_name) = LOWER(%s)")
        params.append(program)
    if level:
        where.append("LOWER(level_name) = LOWER(%s)")
        params.append(level)
    if semester:
        where.append("semester_no = %s")
        params.append(int(semester))
    rows = db_utils.fetch_all(
        f"""
        SELECT id, study_category, program_name, level_name, semester_no, course_code, course_title, credit_units, is_active, created_at
        FROM program_level_courses
        WHERE {" AND ".join(where)}
        ORDER BY study_category ASC, program_name ASC, level_name ASC, COALESCE(semester_no, 1) ASC, course_code ASC
        """,
        tuple(params),
    )
    return jsonify({"courses": rows, "total": len(rows)}), 200


@web_bp.get("/admin/programs")
@roles_required("admin", "super_admin")
def list_academic_programs():
    _ensure_departments_schema()
    active_only = str(request.args.get("active_only") or "").strip().lower() in {"1", "true", "yes"}
    study_category = _optional_study_category(request.args.get("study_category"))
    where = []
    params = []
    if active_only:
        where.append("p.is_active = TRUE")
    if study_category:
        where.append("LOWER(COALESCE(p.study_category, 'undergraduate')) = LOWER(%s)")
        params.append(study_category)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    rows = db_utils.fetch_all(
        f"""
        SELECT
            p.id,
            p.program_name,
            COALESCE(p.study_category, 'undergraduate') AS study_category,
            p.duration_years,
            p.semesters_per_year,
            p.is_active,
            p.created_at,
            d.id AS department_id,
            d.department_name
        FROM academic_programs p
        LEFT JOIN program_department_map m ON LOWER(m.program_name) = LOWER(p.program_name)
        LEFT JOIN departments d ON d.id = m.department_id
        {where_sql}
        ORDER BY p.is_active DESC, p.program_name ASC
        """,
        tuple(params),
    )
    return jsonify({"programs": rows, "total": len(rows)}), 200


@web_bp.post("/admin/programs")
@roles_required("admin", "super_admin")
def create_academic_program():
    _ensure_departments_schema()
    payload = request.get_json() or {}
    program_name = str(payload.get("program_name") or "").strip()
    study_category = _normalize_study_category(payload.get("study_category"))
    department_id = payload.get("department_id")
    duration_years = payload.get("duration_years")
    semesters_per_year = payload.get("semesters_per_year")
    if not program_name:
        return jsonify({"error": "program_name is required"}), 400
    if department_id is None or str(department_id).strip() == "":
        return jsonify({"error": "department_id is required"}), 400
    try:
        department_id = int(department_id)
    except (TypeError, ValueError):
        return jsonify({"error": "department_id must be numeric"}), 400
    try:
        duration_years = int(duration_years)
    except (TypeError, ValueError):
        return jsonify({"error": "duration_years must be a number"}), 400
    try:
        semesters_per_year = int(semesters_per_year)
    except (TypeError, ValueError):
        return jsonify({"error": "semesters_per_year must be a number"}), 400
    if duration_years <= 0 or duration_years > 10:
        return jsonify({"error": "duration_years must be between 1 and 10"}), 400
    if semesters_per_year <= 0 or semesters_per_year > 8:
        return jsonify({"error": "semesters_per_year must be between 1 and 8"}), 400

    dep = db_utils.fetch_one("SELECT id FROM departments WHERE id = %s LIMIT 1", (department_id,))
    if not dep:
        return jsonify({"error": "Selected department was not found"}), 404

    row = db_utils.execute_returning(
        """
        INSERT INTO academic_programs (program_name, study_category, duration_years, semesters_per_year, is_active)
        VALUES (%s, %s, %s, %s, TRUE)
        ON CONFLICT (program_name)
        DO UPDATE SET
            study_category = EXCLUDED.study_category,
            duration_years = EXCLUDED.duration_years,
            semesters_per_year = EXCLUDED.semesters_per_year,
            is_active = TRUE
        RETURNING id, program_name, study_category, duration_years, semesters_per_year, is_active, created_at
        """,
        (program_name, study_category, duration_years, semesters_per_year),
    )
    db_utils.execute(
        "DELETE FROM program_department_map WHERE LOWER(program_name) = LOWER(%s)",
        (row.get("program_name"),),
    )
    db_utils.execute(
        "INSERT INTO program_department_map (program_name, department_id) VALUES (%s, %s)",
        (row.get("program_name"), department_id),
    )
    return jsonify({"message": "Program saved successfully", "program": row}), 200


@web_bp.delete("/admin/programs/<int:program_id>")
@roles_required("admin", "super_admin")
def deactivate_academic_program(program_id):
    row = db_utils.execute_returning(
        """
        UPDATE academic_programs
        SET is_active = FALSE
        WHERE id = %s
        RETURNING id, program_name, duration_years, semesters_per_year, is_active, created_at
        """,
        (program_id,),
    )
    if not row:
        return jsonify({"error": "Program not found"}), 404
    return jsonify({"message": "Program marked as unavailable", "program": row}), 200


@web_bp.patch("/admin/programs/<int:program_id>/availability")
@roles_required("admin", "super_admin")
def set_program_availability(program_id):
    payload = request.get_json() or {}
    if "is_active" not in payload:
        return jsonify({"error": "is_active is required"}), 400
    is_active = bool(payload.get("is_active"))
    row = db_utils.execute_returning(
        """
        UPDATE academic_programs
        SET is_active = %s
        WHERE id = %s
        RETURNING id, program_name, duration_years, semesters_per_year, is_active, created_at
        """,
        (is_active, program_id),
    )
    if not row:
        return jsonify({"error": "Program not found"}), 404
    message = "Program marked as available" if is_active else "Program marked as unavailable"
    return jsonify({"message": message, "program": row}), 200


@web_bp.get("/admin/academic-years")
@roles_required("admin", "super_admin")
def list_academic_years():
    rows = db_utils.fetch_all(
        """
        SELECT
            id, year_label, is_current, enrollment_open, is_active,
            start_month, start_day, end_month, end_day, created_at
        FROM academic_years
        ORDER BY id DESC
        """
    )
    return jsonify({"academic_years": rows, "total": len(rows)}), 200


@web_bp.post("/admin/academic-years")
@roles_required("admin", "super_admin")
def create_academic_year():
    payload = request.get_json() or {}
    year_label = str(payload.get("year_label") or "").strip()
    if not year_label:
        return jsonify({"error": "year_label is required"}), 400

    make_current = bool(payload.get("is_current"))
    enrollment_open = bool(payload.get("enrollment_open", True))
    is_active = bool(payload.get("is_active", True))
    try:
        start_month, start_day = _parse_month_day(payload.get("start_month", 9), payload.get("start_day", 1), "start")
        end_month, end_day = _parse_month_day(payload.get("end_month", 8), payload.get("end_day", 31), "end")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if make_current:
        db_utils.execute("UPDATE academic_years SET is_current = FALSE WHERE is_current = TRUE")

    row = db_utils.execute_returning(
        """
        INSERT INTO academic_years
            (year_label, is_current, enrollment_open, is_active, start_month, start_day, end_month, end_day)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (year_label)
        DO UPDATE SET
            is_current = EXCLUDED.is_current,
            enrollment_open = EXCLUDED.enrollment_open,
            is_active = EXCLUDED.is_active,
            start_month = EXCLUDED.start_month,
            start_day = EXCLUDED.start_day,
            end_month = EXCLUDED.end_month,
            end_day = EXCLUDED.end_day
        RETURNING
            id, year_label, is_current, enrollment_open, is_active,
            start_month, start_day, end_month, end_day, created_at
        """,
        (year_label, make_current, enrollment_open, is_active, start_month, start_day, end_month, end_day),
    )
    if make_current:
        db_utils.execute(
            "UPDATE academic_years SET is_current = FALSE WHERE id <> %s AND is_current = TRUE",
            (int(row.get("id")),),
        )
    return jsonify({"message": "Academic year saved successfully", "academic_year": row}), 200


@web_bp.patch("/admin/academic-years/<int:year_id>/current")
@roles_required("admin", "super_admin")
def set_current_academic_year(year_id):
    row = db_utils.fetch_one(
        "SELECT id, year_label FROM academic_years WHERE id = %s LIMIT 1",
        (year_id,),
    )
    if not row:
        return jsonify({"error": "Academic year not found"}), 404
    db_utils.execute("UPDATE academic_years SET is_current = FALSE WHERE is_current = TRUE")
    updated = db_utils.execute_returning(
        """
        UPDATE academic_years
        SET is_current = TRUE
        WHERE id = %s
        RETURNING
            id, year_label, is_current, enrollment_open, is_active,
            start_month, start_day, end_month, end_day, created_at
        """,
        (year_id,),
    )
    return jsonify({"message": f"{updated.get('year_label')} is now current", "academic_year": updated}), 200


@web_bp.patch("/admin/academic-years/<int:year_id>/enrollment")
@roles_required("admin", "super_admin")
def set_academic_year_enrollment_status(year_id):
    payload = request.get_json() or {}
    if "enrollment_open" not in payload:
        return jsonify({"error": "enrollment_open is required"}), 400
    enrollment_open = bool(payload.get("enrollment_open"))
    updated = db_utils.execute_returning(
        """
        UPDATE academic_years
        SET enrollment_open = %s
        WHERE id = %s
        RETURNING
            id, year_label, is_current, enrollment_open, is_active,
            start_month, start_day, end_month, end_day, created_at
        """,
        (enrollment_open, year_id),
    )
    if not updated:
        return jsonify({"error": "Academic year not found"}), 404
    message = "Enrollment opened" if enrollment_open else "Enrollment closed"
    return jsonify({"message": message, "academic_year": updated}), 200


@web_bp.post("/admin/academic-years/<int:year_id>/end")
@roles_required("admin", "super_admin")
def end_academic_year(year_id):
    payload = request.get_json() or {}
    year = db_utils.fetch_one(
        """
        SELECT
            id, year_label, is_current, enrollment_open, is_active,
            start_month, start_day, end_month, end_day, created_at
        FROM academic_years
        WHERE id = %s
        LIMIT 1
        """,
        (year_id,),
    )
    if not year:
        return jsonify({"error": "Academic year not found"}), 404
    if not bool(year.get("is_current")):
        return jsonify({"error": "Only the current academic year can be ended"}), 400

    next_year_label = str(payload.get("next_year_label") or "").strip()
    if not next_year_label:
        next_year_label = _next_academic_year_label(year.get("year_label"))
    try:
        start_month, start_day = _parse_month_day(
            payload.get("start_month", year.get("start_month", 9)),
            payload.get("start_day", year.get("start_day", 1)),
            "start",
        )
        end_month, end_day = _parse_month_day(
            payload.get("end_month", year.get("end_month", 8)),
            payload.get("end_day", year.get("end_day", 31)),
            "end",
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    promoted, skipped = _promote_eligible_students()

    db_utils.execute(
        """
        UPDATE academic_years
        SET is_current = FALSE, enrollment_open = FALSE, is_active = FALSE
        WHERE id = %s
        """,
        (int(year_id),),
    )

    next_year = db_utils.execute_returning(
        """
        INSERT INTO academic_years
            (year_label, is_current, enrollment_open, is_active, start_month, start_day, end_month, end_day)
        VALUES (%s, TRUE, TRUE, TRUE, %s, %s, %s, %s)
        ON CONFLICT (year_label)
        DO UPDATE SET
            is_current = TRUE,
            enrollment_open = TRUE,
            is_active = TRUE,
            start_month = EXCLUDED.start_month,
            start_day = EXCLUDED.start_day,
            end_month = EXCLUDED.end_month,
            end_day = EXCLUDED.end_day
        RETURNING
            id, year_label, is_current, enrollment_open, is_active,
            start_month, start_day, end_month, end_day, created_at
        """,
        (next_year_label, start_month, start_day, end_month, end_day),
    )
    db_utils.execute(
        "UPDATE academic_years SET is_current = FALSE WHERE id <> %s AND is_current = TRUE",
        (int(next_year.get("id")),),
    )

    ended_year = db_utils.fetch_one(
        """
        SELECT
            id, year_label, is_current, enrollment_open, is_active,
            start_month, start_day, end_month, end_day, created_at
        FROM academic_years
        WHERE id = %s
        LIMIT 1
        """,
        (int(year_id),),
    )
    return jsonify(
        {
            "message": f"Academic year {year.get('year_label')} ended. {next_year.get('year_label')} is now current.",
            "ended_year": ended_year,
            "next_year": next_year,
            "promotion_summary": {
                "promoted_count": len(promoted),
                "promoted_students": promoted,
                "skipped_count": len(skipped),
            },
        }
    ), 200


@web_bp.get("/admin/academic-years/<int:year_id>/exceptions")
@roles_required("admin", "super_admin")
def list_academic_year_exceptions(year_id):
    year = db_utils.fetch_one(
        "SELECT id, year_label, enrollment_open FROM academic_years WHERE id = %s LIMIT 1",
        (year_id,),
    )
    if not year:
        return jsonify({"error": "Academic year not found"}), 404
    rows = db_utils.fetch_all(
        """
        SELECT id, academic_year_id, program_name, created_at
        FROM academic_year_program_exceptions
        WHERE academic_year_id = %s
        ORDER BY program_name ASC
        """,
        (year_id,),
    )
    return jsonify({"academic_year": year, "exceptions": rows, "total": len(rows)}), 200


@web_bp.post("/admin/academic-years/<int:year_id>/exceptions")
@roles_required("admin", "super_admin")
def create_academic_year_exception(year_id):
    payload = request.get_json() or {}
    program_name = str(payload.get("program_name") or "").strip()
    if not program_name:
        return jsonify({"error": "program_name is required"}), 400

    year = db_utils.fetch_one(
        "SELECT id, year_label, enrollment_open FROM academic_years WHERE id = %s LIMIT 1",
        (year_id,),
    )
    if not year:
        return jsonify({"error": "Academic year not found"}), 404

    program_exists = db_utils.fetch_one(
        "SELECT id, program_name FROM academic_programs WHERE LOWER(program_name) = LOWER(%s) LIMIT 1",
        (program_name,),
    )
    if not program_exists:
        return jsonify({"error": "Program not found"}), 404

    row = db_utils.execute_returning(
        """
        INSERT INTO academic_year_program_exceptions (academic_year_id, program_name)
        VALUES (%s, %s)
        ON CONFLICT (academic_year_id, program_name)
        DO UPDATE SET program_name = EXCLUDED.program_name
        RETURNING id, academic_year_id, program_name, created_at
        """,
        (int(year_id), program_exists.get("program_name")),
    )
    return jsonify({"message": "Exception saved", "exception": row}), 200


@web_bp.delete("/admin/academic-years/<int:year_id>/exceptions/<int:exception_id>")
@roles_required("admin", "super_admin")
def delete_academic_year_exception(year_id, exception_id):
    row = db_utils.execute_returning(
        """
        DELETE FROM academic_year_program_exceptions
        WHERE id = %s AND academic_year_id = %s
        RETURNING id, academic_year_id, program_name, created_at
        """,
        (int(exception_id), int(year_id)),
    )
    if not row:
        return jsonify({"error": "Exception not found"}), 404
    return jsonify({"message": "Exception removed", "exception": row}), 200


@web_bp.get("/admin/program-level-semesters")
@roles_required("admin", "super_admin")
def list_program_level_semesters():
    program = str(request.args.get("program") or "").strip()
    if not program:
        programs = db_utils.fetch_all(
            """
            SELECT id, program_name, study_category, duration_years, semesters_per_year, is_active, created_at
            FROM academic_programs
            WHERE is_active = TRUE
            ORDER BY program_name ASC
            """
        )
        rows = []
        for p in programs:
            for level_name in _program_levels_by_category(p.get("study_category"), p.get("duration_years")):
                rows.append(
                    {
                        "program_name": p.get("program_name"),
                        "level_name": level_name,
                        "semester_count": int(p.get("semesters_per_year") or 2),
                        "default_semester_count": int(p.get("semesters_per_year") or 2),
                        "is_override": False,
                    }
                )
        return jsonify({"semesters": rows, "total": len(rows)}), 200

    program_row = _get_program_definition(program)
    if not program_row:
        return jsonify({"semesters": [], "levels": [], "overrides": [], "total": 0}), 200

    levels = []
    for level_name in _program_levels_by_category(program_row.get("study_category"), program_row.get("duration_years")):
        default_semesters = int(program_row.get("semesters_per_year") or 2)
        levels.append(
            {
                "program_name": program_row.get("program_name"),
                "level_name": level_name,
                "semester_count": default_semesters,
                "default_semester_count": default_semesters,
                "is_override": False,
            }
        )
    return jsonify(
        {
            "program": program_row,
            "semesters": levels,
            "levels": levels,
            "overrides": [],
            "total": len(levels),
        }
    ), 200


@web_bp.post("/admin/program-level-semesters")
@roles_required("admin", "super_admin")
def upsert_program_level_semesters():
    return jsonify(
        {
            "error": "Level-specific semester overrides are disabled. "
                     "Set semesters_per_year on the program and all levels will use it."
        }
    ), 400


@web_bp.get("/admin/program-level-semester-statuses")
@roles_required("admin", "super_admin")
def list_program_level_semester_statuses():
    program = str(request.args.get("program") or "").strip()
    level = str(request.args.get("level") or "").strip()
    where = []
    params = []
    if program:
        where.append("LOWER(program_name) = LOWER(%s)")
        params.append(program)
    if level:
        where.append("LOWER(level_name) = LOWER(%s)")
        params.append(level)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    rows = db_utils.fetch_all(
        f"""
        SELECT id, program_name, level_name, semester_no, is_ended, ended_at, updated_at
        FROM program_level_semester_statuses
        {where_sql}
        ORDER BY program_name ASC, level_name ASC, semester_no ASC
        """,
        tuple(params),
    )
    return jsonify({"statuses": rows, "total": len(rows)}), 200


@web_bp.post("/admin/program-level-semester-statuses")
@roles_required("admin", "super_admin")
def set_program_level_semester_status():
    payload = request.get_json() or {}
    program = str(payload.get("program_name") or "").strip()
    level = str(payload.get("level_name") or "").strip()
    semester_no = payload.get("semester_no")
    is_ended = bool(payload.get("is_ended"))
    if not program or not level or semester_no in (None, ""):
        return jsonify({"error": "program_name, level_name, semester_no and is_ended are required"}), 400

    try:
        semester_no = int(semester_no)
    except (TypeError, ValueError):
        return jsonify({"error": "semester_no must be a number"}), 400
    if semester_no <= 0:
        return jsonify({"error": "semester_no must be greater than zero"}), 400

    program_exists = db_utils.fetch_one(
        """
        SELECT id, program_name, study_category, duration_years, semesters_per_year
        FROM academic_programs
        WHERE LOWER(program_name) = LOWER(%s) AND is_active = TRUE
        LIMIT 1
        """,
        (program,),
    )
    if not program_exists:
        return jsonify({"error": "Select a valid active program first"}), 400

    allowed_levels = set(_program_levels_by_category(program_exists.get("study_category"), program_exists.get("duration_years")))
    if level not in allowed_levels:
        return jsonify({"error": f"level_name must be one of: {', '.join(sorted(allowed_levels))}"}), 400
    max_semesters = int(program_exists.get("semesters_per_year") or 2)
    if semester_no > max_semesters:
        return jsonify({"error": f"semester_no exceeds configured semesters_per_year ({max_semesters})"}), 400

    if is_ended and semester_no > 1:
        previous_open = db_utils.fetch_one(
            """
            SELECT semester_no
            FROM program_level_semester_statuses
            WHERE LOWER(program_name) = LOWER(%s)
              AND LOWER(level_name) = LOWER(%s)
              AND semester_no = %s
              AND is_ended = TRUE
            LIMIT 1
            """,
            (program, level, semester_no - 1),
        )
        if not previous_open:
            return jsonify({"error": f"Semester {semester_no - 1} must be marked ended first"}), 400

    row = db_utils.execute_returning(
        """
        INSERT INTO program_level_semester_statuses
            (program_name, level_name, semester_no, is_ended, ended_at, updated_at)
        VALUES (%s, %s, %s, %s, CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE NULL END, CURRENT_TIMESTAMP)
        ON CONFLICT (program_name, level_name, semester_no)
        DO UPDATE SET
            is_ended = EXCLUDED.is_ended,
            ended_at = CASE WHEN EXCLUDED.is_ended THEN CURRENT_TIMESTAMP ELSE NULL END,
            updated_at = CURRENT_TIMESTAMP
        RETURNING id, program_name, level_name, semester_no, is_ended, ended_at, updated_at
        """,
        (program, level, semester_no, is_ended, is_ended),
    )

    if not is_ended:
        db_utils.execute(
            """
            UPDATE program_level_semester_statuses
            SET is_ended = FALSE, ended_at = NULL, updated_at = CURRENT_TIMESTAMP
            WHERE LOWER(program_name) = LOWER(%s)
              AND LOWER(level_name) = LOWER(%s)
              AND semester_no >= %s
            """,
            (program, level, semester_no),
        )
        row = db_utils.fetch_one(
            """
            SELECT id, program_name, level_name, semester_no, is_ended, ended_at, updated_at
            FROM program_level_semester_statuses
            WHERE LOWER(program_name) = LOWER(%s)
              AND LOWER(level_name) = LOWER(%s)
              AND semester_no = %s
            LIMIT 1
            """,
            (program, level, semester_no),
        )

    action = "marked ended" if is_ended else "reopened"
    return jsonify({"message": f"Semester {semester_no} {action}", "status": row}), 200


@web_bp.post("/admin/courses")
@roles_required("admin", "super_admin")
def create_program_level_course():
    payload = request.get_json() or {}
    study_category = _optional_study_category(payload.get("study_category"))
    program = str(payload.get("program_name") or "").strip()
    level = str(payload.get("level_name") or "").strip()
    semester_no = payload.get("semester_no")
    course_code = str(payload.get("course_code") or "").strip().upper()
    course_title = str(payload.get("course_title") or "").strip()
    credit_units = payload.get("credit_units")

    if not program or not level or not course_code or not course_title or semester_no in (None, ""):
        return jsonify({"error": "program_name, level_name, semester_no, course_code and course_title are required"}), 400
    try:
        semester_no = int(semester_no)
    except (TypeError, ValueError):
        return jsonify({"error": "semester_no must be a number"}), 400
    if semester_no <= 0:
        return jsonify({"error": "semester_no must be greater than zero"}), 400

    program_exists = db_utils.fetch_one(
        """
        SELECT id, program_name, study_category, duration_years, semesters_per_year
        FROM academic_programs
        WHERE LOWER(program_name) = LOWER(%s) AND is_active = TRUE
        LIMIT 1
        """,
        (program,),
    )
    if not program_exists:
        return jsonify({"error": "Select a valid active program first"}), 400
    program_category = _normalize_study_category(program_exists.get("study_category"))
    if study_category and study_category != program_category:
        return jsonify({"error": "study_category does not match selected program"}), 400
    allowed_levels = set(_program_levels_by_category(program_category, program_exists.get("duration_years")))
    if level not in allowed_levels:
        return jsonify({"error": f"level_name must be one of: {', '.join(sorted(allowed_levels))}"}), 400
    max_semesters = _effective_semester_count(program, level)
    if not max_semesters:
        return jsonify({"error": "Invalid program level semester configuration"}), 400
    if semester_no > max_semesters:
        return jsonify({"error": f"semester_no exceeds configured semester_count ({max_semesters}) for this level"}), 400

    if credit_units in ("", None):
        credit_units = None
    else:
        try:
            credit_units = int(credit_units)
            if credit_units < 0:
                return jsonify({"error": "credit_units must be zero or positive"}), 400
        except (TypeError, ValueError):
            return jsonify({"error": "credit_units must be a number"}), 400

    row = db_utils.execute_returning(
        """
        INSERT INTO program_level_courses
            (study_category, program_name, level_name, semester_no, course_code, course_title, credit_units, is_active)
        VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE)
        ON CONFLICT (program_name, level_name, course_code)
        DO UPDATE SET
            study_category = EXCLUDED.study_category,
            semester_no = EXCLUDED.semester_no,
            course_title = EXCLUDED.course_title,
            credit_units = EXCLUDED.credit_units,
            is_active = TRUE
        RETURNING id, study_category, program_name, level_name, semester_no, course_code, course_title, credit_units, is_active, created_at
        """,
        (program_category, program, level, semester_no, course_code, course_title, credit_units),
    )
    return jsonify({"message": "Course saved successfully", "course": row}), 200


@web_bp.put("/admin/courses/<int:course_id>")
@roles_required("admin", "super_admin")
def update_program_level_course(course_id):
    payload = request.get_json() or {}
    existing = db_utils.fetch_one(
        """
        SELECT id, study_category, program_name, level_name, semester_no, course_code, course_title, credit_units, is_active
        FROM program_level_courses
        WHERE id = %s
        LIMIT 1
        """,
        (course_id,),
    )
    if not existing:
        return jsonify({"error": "Course mapping not found"}), 404

    study_category = _normalize_study_category(payload.get("study_category") or existing.get("study_category"))
    program = str(payload.get("program_name") or existing.get("program_name") or "").strip()
    level = str(payload.get("level_name") or existing.get("level_name") or "").strip()
    course_code = str(payload.get("course_code") or existing.get("course_code") or "").strip().upper()
    course_title = str(payload.get("course_title") or existing.get("course_title") or "").strip()
    semester_no = payload.get("semester_no", existing.get("semester_no"))
    credit_units = payload.get("credit_units", existing.get("credit_units"))

    if not program or not level or not course_code or not course_title or semester_no in (None, ""):
        return jsonify({"error": "program_name, level_name, semester_no, course_code and course_title are required"}), 400
    try:
        semester_no = int(semester_no)
    except (TypeError, ValueError):
        return jsonify({"error": "semester_no must be a number"}), 400
    if semester_no <= 0:
        return jsonify({"error": "semester_no must be greater than zero"}), 400

    program_exists = db_utils.fetch_one(
        """
        SELECT id, program_name, study_category, duration_years, semesters_per_year, is_active
        FROM academic_programs
        WHERE LOWER(program_name) = LOWER(%s)
        LIMIT 1
        """,
        (program,),
    )
    if not program_exists:
        return jsonify({"error": "Selected program does not exist"}), 400
    program_category = _normalize_study_category(program_exists.get("study_category"))
    if study_category != program_category:
        return jsonify({"error": "study_category does not match selected program"}), 400
    allowed_levels = set(_program_levels_by_category(program_category, program_exists.get("duration_years")))
    if level not in allowed_levels:
        return jsonify({"error": f"level_name must be one of: {', '.join(sorted(allowed_levels))}"}), 400
    max_semesters = _effective_semester_count(program, level)
    if not max_semesters:
        return jsonify({"error": "Invalid program level semester configuration"}), 400
    if semester_no > max_semesters:
        return jsonify({"error": f"semester_no exceeds configured semester_count ({max_semesters}) for this level"}), 400

    if credit_units in ("", None):
        credit_units = None
    else:
        try:
            credit_units = int(credit_units)
            if credit_units < 0:
                return jsonify({"error": "credit_units must be zero or positive"}), 400
        except (TypeError, ValueError):
            return jsonify({"error": "credit_units must be a number"}), 400

    duplicate = db_utils.fetch_one(
        """
        SELECT id
        FROM program_level_courses
        WHERE id <> %s
          AND LOWER(program_name) = LOWER(%s)
          AND LOWER(level_name) = LOWER(%s)
          AND UPPER(course_code) = UPPER(%s)
        LIMIT 1
        """,
        (course_id, program, level, course_code),
    )
    if duplicate:
        return jsonify({"error": "A course with this code already exists in that program level"}), 409

    row = db_utils.execute_returning(
        """
        UPDATE program_level_courses
        SET
            study_category = %s,
            program_name = %s,
            level_name = %s,
            semester_no = %s,
            course_code = %s,
            course_title = %s,
            credit_units = %s,
            is_active = TRUE
        WHERE id = %s
        RETURNING id, study_category, program_name, level_name, semester_no, course_code, course_title, credit_units, is_active, created_at
        """,
        (program_category, program, level, semester_no, course_code, course_title, credit_units, course_id),
    )
    return jsonify({"message": "Course updated successfully", "course": row}), 200


@web_bp.delete("/admin/courses/<int:course_id>")
@roles_required("admin", "super_admin")
def deactivate_program_level_course(course_id):
    row = db_utils.execute_returning(
        """
        UPDATE program_level_courses
        SET is_active = FALSE
        WHERE id = %s
        RETURNING id, program_name, level_name, semester_no, course_code, course_title, credit_units, is_active, created_at
        """,
        (course_id,),
    )
    if not row:
        return jsonify({"error": "Course mapping not found"}), 404
    return jsonify({"message": "Course removed from active list", "course": row}), 200


@web_bp.get("/admin/lecturer-courses")
@roles_required("admin", "super_admin")
def list_lecturer_courses():
    lecturer_id = request.args.get("lecturer_id", type=int)
    where = ""
    params = ()
    if lecturer_id:
        where = "WHERE lc.lecturer_id = %s"
        params = (lecturer_id,)
    rows = db_utils.fetch_all(
        f"""
        SELECT
            lc.id,
            lc.lecturer_id,
            lc.course_code,
            lc.course_title,
            lc.is_active,
            lc.assigned_at,
            a.full_name AS lecturer_name,
            a.email AS lecturer_email
        FROM lecturer_courses lc
        LEFT JOIN admins a ON a.id = lc.lecturer_id
        {where}
        ORDER BY lc.assigned_at DESC
        """,
        params,
    )
    return jsonify({"mappings": rows, "total": len(rows)}), 200


@web_bp.post("/admin/lecturer-courses")
@roles_required("admin", "super_admin")
def assign_lecturer_course():
    payload = request.get_json() or {}
    lecturer_id = payload.get("lecturer_id")
    course_code = str(payload.get("course_code") or "").strip().upper()
    course_title = str(payload.get("course_title") or "").strip() or None

    if not lecturer_id:
        return jsonify({"error": "lecturer_id is required"}), 400
    if not course_code:
        return jsonify({"error": "course_code is required"}), 400

    lecturer = db_utils.fetch_one(
        "SELECT id, role, is_active FROM admins WHERE id = %s",
        (lecturer_id,),
    )
    if not lecturer or not lecturer.get("is_active"):
        return jsonify({"error": "Lecturer account not found or inactive"}), 404
    if str(lecturer.get("role") or "").lower() != "lecturer":
        return jsonify({"error": "Selected admin is not a lecturer account"}), 400

    row = db_utils.execute_returning(
        """
        INSERT INTO lecturer_courses (lecturer_id, course_code, course_title, is_active)
        VALUES (%s, %s, %s, TRUE)
        ON CONFLICT (lecturer_id, course_code)
        DO UPDATE SET course_title = EXCLUDED.course_title, is_active = TRUE
        RETURNING id, lecturer_id, course_code, course_title, is_active, assigned_at
        """,
        (lecturer_id, course_code, course_title),
    )
    _record_system_event(
        "lecturer_course.assigned",
        entity_type="lecturer_course",
        entity_id=(row or {}).get("id"),
        details={
            "lecturer_id": lecturer_id,
            "course_code": course_code,
            "course_title": course_title,
            "is_active": True,
        },
    )
    return jsonify({"message": "Course assigned to lecturer", "mapping": row}), 200


@web_bp.post("/admin/lecturers")
@roles_required("admin", "super_admin")
def create_lecturer_account():
    from werkzeug.security import generate_password_hash

    payload = request.get_json() or {}
    username = str(payload.get("username") or "").strip()
    email = str(payload.get("email") or "").strip().lower()
    full_name = str(payload.get("full_name") or "").strip()
    profile_photo = _normalize_data_url_image(payload.get("profile_photo"))
    if not username or not email or not full_name:
        return jsonify({"error": "username, email, and full_name are required"}), 400

    exists = db_utils.fetch_one(
        "SELECT id FROM admins WHERE LOWER(username) = LOWER(%s) OR LOWER(email) = LOWER(%s)",
        (username, email),
    )
    if exists:
        return jsonify({"error": "Lecturer username or email already exists"}), 409

    temporary_password = secrets.token_urlsafe(8)
    row = db_utils.execute_returning(
        """
        INSERT INTO admins (username, email, full_name, profile_photo, role, password_hash, must_change_password, is_active)
        VALUES (%s, %s, %s, %s, 'lecturer', %s, TRUE, TRUE)
        RETURNING id, username, email, full_name, profile_photo, role, is_active, must_change_password, created_at
        """,
        (username, email, full_name, profile_photo, generate_password_hash(temporary_password)),
    )
    _record_system_event(
        "lecturer.created",
        entity_type="lecturer",
        entity_id=(row or {}).get("id"),
        details={
            "username": username,
            "email": email,
            "full_name": full_name,
            "is_active": True,
            "source": "admin_api",
        },
    )
    return jsonify(
        {
            "message": "Lecturer account created",
            "lecturer": row,
            "temporary_password": temporary_password,
        }
    ), 201


@web_bp.get("/admin/pause-controls/state")
@roles_required("invigilator", "lecturer", "admin", "super_admin")
def get_pause_control_state():
    session_id = request.args.get("session_id", type=int)
    if not session_id:
        return jsonify({"error": "session_id is required"}), 400
    session_row = db_utils.fetch_one(
        "SELECT id, hall_id, paper_group_code FROM examination_sessions WHERE id = %s",
        (session_id,),
    )
    if not session_row:
        return jsonify({"error": "Session not found"}), 404

    hall_id = int(session_row["hall_id"]) if session_row.get("hall_id") is not None else None
    state = pause_controls.get_pause_state(int(session_id), hall_id)
    verification_pause = state.get("verification_pause") or {}
    time_pause = state.get("time_pause") or {}
    return jsonify(
        {
            "session_id": int(session_id),
            "hall_id": hall_id,
            "paper_group_code": session_row.get("paper_group_code"),
            "verification_paused": bool(state.get("verification_paused")),
            "time_paused": bool(state.get("time_paused")),
            "verification_reason": verification_pause.get("reason"),
            "time_reason": time_pause.get("reason"),
            "verification_pause": verification_pause,
            "time_pause": time_pause,
        }
    ), 200


@web_bp.post("/admin/pause-controls/action")
@roles_required("admin", "super_admin")
def pause_controls_action():
    payload = request.get_json() or {}
    action = str(payload.get("action") or "").strip().lower()
    pause_type = str(payload.get("pause_type") or "").strip().lower()
    scope = str(payload.get("scope") or "hall").strip().lower()
    reason = str(payload.get("reason") or "").strip()
    session_id = payload.get("session_id")

    try:
        session_id = int(session_id)
    except (TypeError, ValueError):
        return jsonify({"error": "session_id is required"}), 400
    if action not in {"pause", "resume"}:
        return jsonify({"error": "action must be pause or resume"}), 400
    if pause_type not in {"verification", "time", "both"}:
        return jsonify({"error": "pause_type must be verification, time, or both"}), 400
    if scope not in {"hall", "session", "paper"}:
        return jsonify({"error": "scope must be hall, session, or paper"}), 400
    if action == "pause" and not reason:
        return jsonify({"error": "reason is required when pausing"}), 400

    session_row = db_utils.fetch_one(
        "SELECT id, hall_id, paper_group_code, is_active, start_time, end_time FROM examination_sessions WHERE id = %s",
        (session_id,),
    )
    if not session_row:
        return jsonify({"error": "Session not found"}), 404

    hall_id = None
    target_sessions = [int(session_id)]
    rows = [session_row]
    if scope == "hall":
        if session_row.get("hall_id") is None:
            return jsonify({"error": "Selected session has no hall assigned for hall-scoped pause"}), 400
        hall_id = int(session_row["hall_id"])
    elif scope == "paper":
        paper_group_code = str(session_row.get("paper_group_code") or "").strip()
        if not paper_group_code:
            return jsonify({"error": "Selected session has no paper group code. Edit/create with paper grouping first."}), 400
        rows = db_utils.fetch_all(
            """
            SELECT id, is_active, start_time, end_time
            FROM examination_sessions
            WHERE paper_group_code = %s
            ORDER BY id ASC
            """,
            (paper_group_code,),
        )
        target_sessions = [int(r["id"]) for r in rows if r.get("id") is not None]
        if not target_sessions:
            return jsonify({"error": "No sessions found for this paper group."}), 404

    now = datetime.utcnow()
    non_live = []
    for row in rows:
        try:
            sid = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        is_active = bool(row.get("is_active"))
        start_time = row.get("start_time")
        end_time = row.get("end_time")
        is_live = bool(is_active and start_time and end_time and start_time <= now <= end_time)
        if not is_live:
            non_live.append(sid)
    if non_live:
        return jsonify(
            {
                "error": "Pause/resume is allowed only for live sessions.",
                "non_live_session_ids": non_live,
            }
        ), 409

    admin_id = int(session.get("admin_id"))
    if action == "pause":
        created_rows = []
        existing_rows = []
        for sid in target_sessions:
            created, pause_row = pause_controls.start_pause(
                session_id=int(sid),
                hall_id=hall_id,
                pause_type=pause_type,
                reason=reason,
                started_by=admin_id,
            )
            if created:
                created_rows.append(pause_row)
            else:
                existing_rows.append(pause_row)
        if not created_rows:
            return jsonify({"error": "A matching active pause already exists for the selected scope.", "existing": existing_rows}), 409
        return jsonify({"message": "Pause activated", "created_count": len(created_rows), "existing_count": len(existing_rows), "pauses": created_rows}), 201

    resumed_rows = []
    paused_seconds_total = 0
    extended_seconds_total = 0
    for sid in target_sessions:
        resumed, pause_row, pause_seconds, extended_seconds = pause_controls.resume_pause(
            session_id=int(sid),
            hall_id=hall_id,
            pause_type=pause_type,
            resumed_by=admin_id,
        )
        if resumed:
            resumed_rows.append(pause_row)
            paused_seconds_total += int(pause_seconds or 0)
            extended_seconds_total += int(extended_seconds or 0)
    if not resumed_rows:
        return jsonify({"error": "No matching active pause found for the selected scope."}), 404
    return jsonify(
        {
            "message": "Pause resumed",
            "resumed_count": len(resumed_rows),
            "pauses": resumed_rows,
            "paused_seconds": paused_seconds_total,
            "extended_seconds": extended_seconds_total,
        }
    ), 200


@web_bp.post("/admin/lecturers/<int:lecturer_id>/reset-password")
@roles_required("admin", "super_admin")
def reset_lecturer_password(lecturer_id):
    payload = request.get_json() or {}
    new_password = str(payload.get("new_password") or "")

    lecturer = db_utils.fetch_one(
        """
        SELECT id, username, email, full_name, role, is_active
        FROM admins
        WHERE id = %s
        LIMIT 1
        """,
        (lecturer_id,),
    )
    if not lecturer:
        return jsonify({"error": "Lecturer not found"}), 404
    if str(lecturer.get("role") or "").lower() != "lecturer":
        return jsonify({"error": "Selected account is not a lecturer"}), 400
    if not lecturer.get("is_active"):
        return jsonify({"error": "Lecturer account is inactive"}), 400

    temporary_password = ""
    if new_password:
        if len(new_password) < 8:
            return jsonify({"error": "new_password must be at least 8 characters"}), 400
        password_to_store = new_password
    else:
        temporary_password = secrets.token_urlsafe(8)
        password_to_store = temporary_password

    db_utils.execute(
        """
        UPDATE admins
        SET password_hash = %s, must_change_password = TRUE
        WHERE id = %s
        """,
        (generate_password_hash(password_to_store), int(lecturer_id)),
    )
    response = {
        "message": "Lecturer password updated. Password change will be required at next login.",
        "lecturer": {
            "id": lecturer.get("id"),
            "username": lecturer.get("username"),
            "email": lecturer.get("email"),
            "full_name": lecturer.get("full_name"),
        },
    }
    if temporary_password:
        response["temporary_password"] = temporary_password
    _record_system_event(
        "lecturer.password_reset",
        entity_type="lecturer",
        entity_id=lecturer_id,
        details={
            "lecturer_username": lecturer.get("username"),
            "lecturer_email": lecturer.get("email"),
            "custom_password_supplied": bool(new_password),
        },
    )
    return jsonify(response), 200


@web_bp.post("/admin/lecturers/<int:lecturer_id>/update")
@roles_required("admin", "super_admin")
def update_lecturer_account(lecturer_id):
    payload = request.get_json() or {}
    lecturer = db_utils.fetch_one(
        """
        SELECT id, username, email, full_name, role, is_active
        FROM admins
        WHERE id = %s
        LIMIT 1
        """,
        (lecturer_id,),
    )
    if not lecturer:
        return jsonify({"error": "Lecturer not found"}), 404
    if str(lecturer.get("role") or "").lower() != "lecturer":
        return jsonify({"error": "Selected account is not a lecturer"}), 400

    full_name = str(payload.get("full_name") or "").strip()
    email = str(payload.get("email") or "").strip().lower()
    username = str(payload.get("username") or "").strip().lower()
    profile_photo = payload.get("profile_photo")
    is_active = payload.get("is_active")

    fields = []
    params = []

    if "full_name" in payload:
        if not full_name:
            return jsonify({"error": "full_name cannot be empty"}), 400
        fields.append("full_name = %s")
        params.append(full_name)
    if "email" in payload:
        if not email:
            return jsonify({"error": "email cannot be empty"}), 400
        fields.append("email = %s")
        params.append(email)
    if "username" in payload:
        if not username:
            return jsonify({"error": "username cannot be empty"}), 400
        fields.append("username = %s")
        params.append(username)
    if "is_active" in payload:
        fields.append("is_active = %s")
        params.append(bool(is_active))
    if "profile_photo" in payload:
        normalized_photo = _normalize_data_url_image(profile_photo)
        if profile_photo and not normalized_photo:
            return jsonify({"error": "Invalid profile photo data"}), 400
        fields.append("profile_photo = %s")
        params.append(normalized_photo)

    if not fields:
        return jsonify({"error": "No valid fields provided"}), 400

    if email or username:
        duplicate = db_utils.fetch_one(
            """
            SELECT id
            FROM admins
            WHERE id <> %s
              AND (
                (%s <> '' AND LOWER(email) = LOWER(%s))
                OR (%s <> '' AND LOWER(username) = LOWER(%s))
              )
            LIMIT 1
            """,
            (int(lecturer_id), email or "", email or "", username or "", username or ""),
        )
        if duplicate:
            return jsonify({"error": "Email or username already exists"}), 409

    params.append(int(lecturer_id))
    updated = db_utils.execute_returning(
        f"""
        UPDATE admins
        SET {", ".join(fields)}
        WHERE id = %s
        RETURNING id, username, email, full_name, profile_photo, role, is_active, must_change_password, created_at
        """,
        tuple(params),
    )
    _record_system_event(
        "lecturer.updated",
        entity_type="lecturer",
        entity_id=(updated or {}).get("id") or lecturer_id,
        details={
            "updated_fields": fields,
            "is_active": bool((updated or {}).get("is_active")) if updated else None,
            "username": (updated or {}).get("username") if updated else username,
            "email": (updated or {}).get("email") if updated else email,
        },
    )
    return jsonify({"message": "Lecturer updated successfully", "lecturer": updated}), 200


@web_bp.delete("/admin/lecturer-courses/<int:mapping_id>")
@roles_required("admin", "super_admin")
def remove_lecturer_course(mapping_id):
    row = db_utils.execute_returning(
        """
        UPDATE lecturer_courses
        SET is_active = FALSE
        WHERE id = %s
        RETURNING id, lecturer_id, course_code, course_title, is_active, assigned_at
        """,
        (mapping_id,),
    )
    if not row:
        return jsonify({"error": "Mapping not found"}), 404
    _record_system_event(
        "lecturer_course.deactivated",
        entity_type="lecturer_course",
        entity_id=(row or {}).get("id"),
        details={
            "lecturer_id": row.get("lecturer_id"),
            "course_code": row.get("course_code"),
            "is_active": bool(row.get("is_active")),
        },
    )
    return jsonify({"message": "Lecturer course mapping deactivated", "mapping": row}), 200


@web_bp.get("/admin/lecturer-courses/manage")
@web_bp.get("/admin/lecturer-courses/manage", endpoint="lecturer_course_assignments_page")
@roles_required("admin", "super_admin")
def lecturer_course_assignments_page():
    lecturers = db_utils.fetch_all(
        """
        SELECT id, username, email, full_name, role, is_active
        FROM admins
        WHERE role = 'lecturer' AND is_active = TRUE
        ORDER BY full_name ASC
        """
    )
    courses = db_utils.fetch_all(
        """
        SELECT course_code, MAX(course_title) AS course_title
        FROM program_level_courses
        WHERE is_active = TRUE
        GROUP BY course_code
        ORDER BY course_code ASC
        """
    )
    mappings = db_utils.fetch_all(
        """
        SELECT
            lc.id,
            lc.lecturer_id,
            lc.course_code,
            lc.course_title,
            lc.is_active,
            lc.assigned_at,
            a.full_name AS lecturer_name,
            a.email AS lecturer_email
        FROM lecturer_courses lc
        LEFT JOIN admins a ON a.id = lc.lecturer_id
        WHERE lc.is_active = TRUE
        ORDER BY a.full_name ASC, lc.course_code ASC
        """
    )
    return render_template(
        "admin/lecturer_courses.html",
        title="Lecturer Course Assignments",
        lecturers=lecturers,
        courses=courses,
        mappings=mappings,
    )


@web_bp.post("/students/verify-biometric")
@roles_required("lecturer", "invigilator", "admin", "super_admin")
def verify_student_biometric():
    try:
        payload = request.get_json() or {}
        student_identifier = str(payload.get("student_id") or "").strip()
        raw_image = payload.get("live_image")
        if not raw_image:
            return jsonify({"error": "live_image is required"}), 400

        live_img = _decode_b64_image(raw_image)
        if live_img is None:
            return jsonify({"error": "Invalid live image"}), 400

        student_service = _get_student_service()
        quality_ok, quality_msg = student_service.face_engine.validate_image_quality(live_img)
        live_emb = student_service.face_engine.extract_live_embedding(live_img)
        if live_emb is None:
            return jsonify(
                {
                    "match": False,
                    "confidence": 0.0,
                    "quality_ok": bool(quality_ok),
                    "quality_message": quality_msg,
                    "student": None,
                    "error": "No face detected in the image. Keep your full face centered and retry in better lighting.",
                }
            ), 200
        if student_identifier:
            student = student_service.get_student(student_identifier)
            if not student:
                return jsonify({"error": "Student not found"}), 404

            stored_encodings = student_service.get_face_encodings(student)
            if not stored_encodings:
                return jsonify({"error": "Student has no saved biometric templates"}), 400

            is_match, confidence = student_service.face_engine.verify_live_embedding(live_emb, stored_encodings)
            return jsonify(
                {
                    "match": bool(is_match),
                    "confidence": float(confidence),
                    "quality_ok": bool(quality_ok),
                    "quality_message": quality_msg,
                    "student": student_service._student_to_dict(student),
                }
            ), 200

        cache = student_service.get_encoding_cache() or []
        if not cache:
            return jsonify({"error": "No enrolled students with biometric templates found"}), 404

        best_match_student = None
        best_match_confidence = -1.0
        best_confidence = -1.0
        for student_row, stored_encodings in cache:
            if not stored_encodings:
                continue
            is_match, confidence = student_service.face_engine.verify_live_embedding(live_emb, stored_encodings)
            conf = float(confidence)
            if conf > best_confidence:
                best_confidence = conf
            if is_match and conf > best_match_confidence:
                best_match_confidence = conf
                best_match_student = student_row

        if best_match_student:
            return jsonify(
                {
                    "match": True,
                    "confidence": float(best_match_confidence),
                    "quality_ok": bool(quality_ok),
                    "quality_message": quality_msg,
                    "student": student_service._student_to_dict(best_match_student),
                }
            ), 200

        return jsonify(
            {
                "match": False,
                "confidence": float(max(best_confidence, 0.0)),
                "quality_ok": bool(quality_ok),
                "quality_message": quality_msg,
                "student": None,
                "error": "No biometric match found. Ensure good lighting, look straight at the camera, and try again or provide Student ID for direct verification.",
            }
        ), 200
    except Exception as exc:
        logger.error(f"Biometric verify test failed: {exc}")
        return jsonify({"error": f"Biometric verification failed: {exc}"}), 500


@web_bp.get("/students/data")
@roles_required("admin", "super_admin")
def students_directory_data():
    try:
        active_only = request.args.get("active_only", "true").lower() == "true"
        if active_only:
            rows = db_utils.fetch_all(
                """
                SELECT
                    id, student_id, first_name, last_name, email, phone,
                    department, course, year_level, study_category, program_name, level_name,
                    entry_cohort, expected_graduation_year,
                    profile_photo, is_active, registration_date, last_updated,
                    CASE
                        WHEN face_encodings IS NOT NULL AND BTRIM(face_encodings) <> '' THEN TRUE
                        ELSE FALSE
                    END AS biometric_enrolled,
                    COALESCE(CHAR_LENGTH(face_encodings), 0) AS biometric_blob_size
                FROM students
                WHERE COALESCE(is_active, TRUE) = TRUE
                ORDER BY id ASC
                """
            )
        else:
            rows = db_utils.fetch_all(
                """
                SELECT
                    id, student_id, first_name, last_name, email, phone,
                    department, course, year_level, study_category, program_name, level_name,
                    entry_cohort, expected_graduation_year,
                    profile_photo, is_active, registration_date, last_updated,
                    CASE
                        WHEN face_encodings IS NOT NULL AND BTRIM(face_encodings) <> '' THEN TRUE
                        ELSE FALSE
                    END AS biometric_enrolled,
                    COALESCE(CHAR_LENGTH(face_encodings), 0) AS biometric_blob_size
                FROM students
                ORDER BY id ASC
                """
            )
        return jsonify({"students": rows, "total": len(rows)}), 200
    except Exception as exc:
        return jsonify({"students": [], "total": 0, "error": str(exc)}), 500


@web_bp.post("/students/update/<student_id>")
@roles_required("admin", "super_admin")
def update_student_directory_data(student_id):
    try:
        student_id = str(student_id).strip()
        payload = request.get_json() or {}
        allowed = {
            "first_name", "last_name", "email", "phone",
            "department", "course", "year_level",
            "study_category", "program_name", "level_name",
            "entry_cohort", "expected_graduation_year",
            "profile_photo", "is_active"
        }
        fields = []
        params = []

        for key in allowed:
            if key in payload:
                value = payload.get(key)
                if key == "study_category":
                    value = _normalize_study_category(value)
                if key in {"entry_cohort", "expected_graduation_year"}:
                    raw = str(value or "").strip()
                    if raw == "":
                        value = None
                    else:
                        try:
                            value = int(raw)
                        except Exception:
                            return jsonify({"error": f"{key} must be numeric"}), 400
                fields.append(f"{key} = %s")
                params.append(value)

        if "program_name" in payload and "course" not in payload:
            fields.append("course = %s")
            params.append(payload.get("program_name"))
        if "level_name" in payload and "year_level" not in payload:
            fields.append("year_level = %s")
            params.append(payload.get("level_name"))

        if not fields:
            return jsonify({"error": "No valid fields provided"}), 400

        fields.append("last_updated = CURRENT_TIMESTAMP")

        sql = f"""
            UPDATE students
            SET {", ".join(fields)}
            WHERE student_id = %s OR CAST(id AS TEXT) = %s
            RETURNING
                id, student_id, first_name, last_name, email, phone,
                department, course, year_level, study_category, program_name, level_name,
                entry_cohort, expected_graduation_year,
                profile_photo, is_active, registration_date, last_updated,
                CASE
                    WHEN face_encodings IS NOT NULL AND BTRIM(face_encodings) <> '' THEN TRUE
                    ELSE FALSE
                END AS biometric_enrolled,
                COALESCE(CHAR_LENGTH(face_encodings), 0) AS biometric_blob_size
        """
        params.append(student_id)
        params.append(student_id)
        updated = db_utils.execute_returning(sql, tuple(params))
        if not updated:
            return jsonify({"error": "Student not found"}), 404

        return jsonify({"message": "Student updated successfully", "student": updated}), 200
    except Exception as exc:
        return jsonify({"error": f"Update failed: {exc}"}), 500


@web_bp.post("/students/update/<student_id>/biometric")
@roles_required("admin", "super_admin")
def update_student_biometric_data(student_id):
    try:
        student_id = str(student_id).strip()
        payload = request.get_json() or {}
        raw_images = payload.get("face_images") or []
        if not isinstance(raw_images, list) or len(raw_images) == 0:
            return jsonify({"error": "face_images is required"}), 400

        student = db_utils.fetch_one(
            "SELECT id, student_id FROM students WHERE student_id = %s OR CAST(id AS TEXT) = %s",
            (student_id, student_id),
        )
        if not student:
            return jsonify({"error": "Student not found"}), 404

        face_images = []
        profile_photo = None
        for raw in raw_images:
            if not isinstance(raw, str):
                continue
            encoded = raw.split(",", 1)[1] if "," in raw else raw
            image_bytes = base64.b64decode(encoded)
            face_images.append(Image.open(io.BytesIO(image_bytes)).convert("RGB"))
            if not profile_photo:
                profile_photo = _normalize_data_url_image(raw)

        if not face_images:
            return jsonify({"error": "No valid images provided"}), 400

        student_service = _get_student_service()
        encodings = student_service.face_engine.capture_multiple_angles(face_images)
        if not encodings:
            return jsonify({"error": "Failed to extract biometric data from provided images"}), 400

        encrypted = encrypt_data(json.dumps([enc.tolist() for enc in encodings]))
        updated = db_utils.execute_returning(
            """
            UPDATE students
            SET face_encodings = %s, profile_photo = %s, last_updated = CURRENT_TIMESTAMP
            WHERE id = %s
            RETURNING
                id, student_id, first_name, last_name, email, phone,
                department, course, year_level, study_category, program_name, level_name,
                entry_cohort, expected_graduation_year,
                profile_photo, is_active, registration_date, last_updated,
                CASE
                    WHEN face_encodings IS NOT NULL AND BTRIM(face_encodings) <> '' THEN TRUE
                    ELSE FALSE
                END AS biometric_enrolled,
                COALESCE(CHAR_LENGTH(face_encodings), 0) AS biometric_blob_size
            """,
            (encrypted, profile_photo, student["id"]),
        )
        return jsonify({"message": "Biometric data updated successfully", "student": updated}), 200
    except Exception as exc:
        return jsonify({"error": f"Biometric update failed: {exc}"}), 500


@web_bp.post("/students/reset-password/<student_id>")
@roles_required("admin", "super_admin")
def reset_student_password(student_id):
    try:
        student_identifier = str(student_id).strip()
        student = db_utils.fetch_one(
            "SELECT id, student_id, first_name, last_name FROM students WHERE student_id = %s OR CAST(id AS TEXT) = %s",
            (student_identifier, student_identifier),
        )
        if not student:
            return jsonify({"error": "Student not found"}), 404

        temporary_password = secrets.token_urlsafe(8)
        db_utils.execute(
            """
            UPDATE students
            SET password_hash = %s, must_change_password = TRUE, last_updated = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (generate_password_hash(temporary_password), int(student["id"])),
        )
        return jsonify(
            {
                "message": "Student password reset successfully",
                "student_id": student.get("student_id"),
                "temporary_password": temporary_password,
            }
        ), 200
    except Exception as exc:
        return jsonify({"error": f"Password reset failed: {exc}"}), 500

@web_bp.get("/admin/lecturers/manage")
@web_bp.get("/admin/lecturers/manage", endpoint="lecturers_manage_page")
@admin_required
def lecturers_manage_page():
    q = str(request.args.get("q") or "").strip()
    status = str(request.args.get("status") or "active").strip().lower()
    if status not in {"active", "inactive", "all"}:
        status = "active"
    page = max(1, request.args.get("page", default=1, type=int) or 1)
    per_page_raw = request.args.get("per_page", "24")
    try:
        per_page = int(per_page_raw)
    except (TypeError, ValueError):
        per_page = 24
    per_page = max(12, min(per_page, 96))

    where = ["LOWER(role) = 'lecturer'"]
    params = []
    if status == "active":
        where.append("is_active = TRUE")
    elif status == "inactive":
        where.append("is_active = FALSE")
    if q:
        query = f"%{q.lower()}%"
        where.append(
            "("
            "LOWER(COALESCE(full_name, '')) LIKE %s OR "
            "LOWER(COALESCE(email, '')) LIKE %s OR "
            "LOWER(COALESCE(username, '')) LIKE %s"
            ")"
        )
        params.extend([query, query, query])
    where_sql = " AND ".join(where)

    total_row = db_utils.fetch_one(
        f"""
        SELECT COUNT(*) AS c
        FROM admins
        WHERE {where_sql}
        """,
        tuple(params),
    )
    total_count = int((total_row or {}).get("c") or 0)
    total_pages = max(1, math.ceil(total_count / per_page)) if total_count else 1
    page = min(page, total_pages)
    offset = (page - 1) * per_page

    rows = db_utils.fetch_all(
        f"""
        SELECT
            id, username, email, full_name, profile_photo, role, is_active,
            must_change_password, created_at, last_login
        FROM admins
        WHERE {where_sql}
        ORDER BY full_name ASC, id ASC
        LIMIT %s OFFSET %s
        """,
        tuple(list(params) + [int(per_page), int(offset)]),
    )

    return render_template(
        "admin/lecturers_manage.html",
        title="Lecturer Profiles",
        lecturers=rows,
        filters={"q": q, "status": status, "per_page": per_page},
        pagination={
            "page": page,
            "per_page": per_page,
            "total_count": total_count,
            "total_pages": total_pages,
            "has_prev": page > 1,
            "has_next": page < total_pages,
            "prev_page": page - 1,
            "next_page": page + 1,
            "start_item": (offset + 1) if total_count else 0,
            "end_item": min(offset + per_page, total_count),
        },
    )


@web_bp.get("/admin/lecturers/new")
@web_bp.get("/admin/lecturers/new", endpoint="add_lecturer_page")
@web_bp.get("/admin/invigilators/new", endpoint="add_invigilator_page")
@admin_required
def add_lecturer():
    lecturers = db_utils.fetch_all(
        """
        SELECT id, username, email, full_name, profile_photo, role, is_active, created_at
        FROM admins
        WHERE role = 'lecturer' AND is_active = TRUE
        ORDER BY full_name ASC
        """
    )
    return render_template("add_invigilator.html", lecturers=lecturers)


@web_bp.post("/admin/lecturers/new")
@web_bp.post("/admin/lecturers/new", endpoint="create_lecturer_web")
@web_bp.post("/admin/invigilators/new", endpoint="create_invigilator_web")
@admin_required
def create_lecturer_web():
    from werkzeug.security import generate_password_hash

    payload = request.get_json() or {}
    full_name = str(payload.get("full_name") or "").strip()
    email = str(payload.get("email") or "").strip().lower()
    username = str(payload.get("username") or "").strip().lower()
    profile_photo = _normalize_data_url_image(payload.get("profile_photo"))

    if not full_name or not email or not username:
        return jsonify({"error": "full_name, email, and username are required"}), 400

    exists = db_utils.fetch_one(
        "SELECT id FROM admins WHERE LOWER(username) = LOWER(%s) OR LOWER(email) = LOWER(%s)",
        (username, email),
    )
    if exists:
        return jsonify({"error": "Username or email already exists"}), 409

    temporary_password = secrets.token_urlsafe(8)
    created = db_utils.execute_returning(
        """
        INSERT INTO admins (username, email, full_name, profile_photo, role, password_hash, must_change_password, is_active)
        VALUES (%s, %s, %s, %s, 'lecturer', %s, TRUE, TRUE)
        RETURNING id, username, email, full_name, profile_photo, role, is_active, must_change_password, created_at
        """,
        (username, email, full_name, profile_photo, generate_password_hash(temporary_password)),
    )
    _record_system_event(
        "lecturer.created",
        entity_type="lecturer",
        entity_id=(created or {}).get("id"),
        details={
            "username": username,
            "email": email,
            "full_name": full_name,
            "is_active": True,
            "source": "web_form",
        },
    )
    return jsonify(
        {
            "message": "Lecturer created",
            "lecturer": created,
            "temporary_password": temporary_password,
        }
    ), 201

@web_bp.get("/logout")
@web_bp.get("/logout", endpoint="logout_page")
def logout():
    _record_logout_event()
    session.clear()
    return redirect(url_for("web.login_page"))


@web_bp.get("/debug/routes")
def debug_routes():
    routes = []
    for rule in current_app.url_map.iter_rules():
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
