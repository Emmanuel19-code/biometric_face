"""Attendance tracking service"""
from datetime import datetime, timezone
import logging
import os
from time import perf_counter

from services.student_service import StudentService
from utils.face_recognition_engine import FaceRecognitionEngine
from utils import db as db_utils

logger = logging.getLogger(__name__)


class AttendanceService:
    """Service for attendance operations"""

    def __init__(self):
        self.face_engine = FaceRecognitionEngine()
        self.student_service = StudentService()
        self._session_cache = {}
        self._invigilator_cache = {}
        self._invigilator_assignment_cache = {}
        self._registered_students_cache = {}
        self._session_cache_ttl = float(os.getenv("VERIFY_CACHE_SESSION_TTL_SEC", "5"))
        self._invigilator_cache_ttl = float(os.getenv("VERIFY_CACHE_INVIGILATOR_TTL_SEC", "20"))
        self._assignment_cache_ttl = float(os.getenv("VERIFY_CACHE_ASSIGNMENT_TTL_SEC", "15"))
        self._registered_cache_ttl = float(os.getenv("VERIFY_CACHE_REGISTERED_TTL_SEC", "20"))
        self._timing_log_enabled = str(os.getenv("VERIFY_TIMING_LOG", "true")).strip().lower() in {"1", "true", "yes"}

    @staticmethod
    def _fmt_dt(value):
        return value.isoformat() if value else None

    @staticmethod
    def _cache_get(cache, key):
        row = cache.get(key)
        if not row:
            return None
        expires_at, value = row
        if datetime.utcnow().timestamp() >= float(expires_at):
            cache.pop(key, None)
            return None
        return value

    @staticmethod
    def _cache_set(cache, key, value, ttl_seconds):
        cache[key] = (datetime.utcnow().timestamp() + max(0.1, float(ttl_seconds)), value)

    def _invalidate_session_caches(self, session_id=None):
        if session_id is None:
            self._session_cache.clear()
            self._invigilator_assignment_cache.clear()
            self._registered_students_cache.clear()
            return
        try:
            sid = int(session_id)
        except Exception:
            sid = session_id
        self._session_cache.pop(sid, None)
        for k in list(self._invigilator_assignment_cache.keys()):
            if isinstance(k, tuple) and k and k[0] == sid:
                self._invigilator_assignment_cache.pop(k, None)
        for k in list(self._registered_students_cache.keys()):
            if isinstance(k, tuple) and k and k[0] == sid:
                self._registered_students_cache.pop(k, None)

    def _get_session_cached(self, session_id):
        try:
            sid = int(session_id)
        except Exception:
            sid = session_id
        cached = self._cache_get(self._session_cache, sid)
        if cached is not None:
            return dict(cached)
        row = self._get_session(sid)
        self._cache_set(self._session_cache, sid, dict(row) if row else None, self._session_cache_ttl)
        return row

    def _get_invigilator_cached(self, invigilator_id):
        inv_id = int(invigilator_id)
        cached = self._cache_get(self._invigilator_cache, inv_id)
        if cached is not None:
            return dict(cached) if cached else None
        row = db_utils.fetch_one(
            "SELECT id, role, is_active FROM admins WHERE id = %s",
            (inv_id,),
        )
        self._cache_set(self._invigilator_cache, inv_id, dict(row) if row else None, self._invigilator_cache_ttl)
        return row

    def _get_registered_student_ids(self, session_id, course_code):
        key = (int(session_id), str(course_code or "").strip().upper())
        cached = self._cache_get(self._registered_students_cache, key)
        if cached is not None:
            return set(cached)
        reg_rows = db_utils.fetch_all(
            """
            SELECT DISTINCT student_id
            FROM (
                SELECT r.student_id
                FROM exam_registrations r
                WHERE r.session_id = %s
                UNION
                SELECT scr.student_id
                FROM student_course_registrations scr
                WHERE UPPER(scr.course_code) = UPPER(%s)
            ) eligible
            """,
            (int(session_id), str(course_code or "").strip()),
        )
        out = {r["student_id"] for r in reg_rows if r.get("student_id") is not None}
        self._cache_set(self._registered_students_cache, key, set(out), self._registered_cache_ttl)
        return out

    def _log_attempt(
        self,
        session_id,
        student_db_id,
        claimed_student_id,
        outcome,
        reason,
        confidence,
        ip_address,
        device_info,
        station_id=None
    ):
        """Write an audit log entry. Never raise from here."""
        try:
            db_utils.execute(
                """
                INSERT INTO verification_logs
                    (session_id, student_id, claimed_student_id, outcome, reason, confidence, ip_address, device_info, station_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    session_id,
                    student_db_id,
                    claimed_student_id,
                    outcome,
                    reason,
                    float(confidence) if confidence is not None else None,
                    ip_address,
                    device_info,
                    station_id
                )
            )
        except Exception as e:
            logger.warning(f"Failed to write VerificationLog: {e}")

    def verify_and_record_attendance(
        self,
        live_image,
        session_id,
        student_id=None,
        ip_address=None,
        device_info=None,
        frames=None,
        challenge=None,
        station_id=None,
        invigilator_id=None
    ):
        """
        Verify student identity and record attendance

        Returns:
            (success, attendance_object_or_error_message, confidence_score)
        """
        confidence = 0.0
        claimed = str(student_id) if student_id is not None else None
        t0 = perf_counter()
        timings = {}
        def mark(name, start):
            timings[name] = round((perf_counter() - start) * 1000.0, 2)

        try:
            t = perf_counter()
            session = self._get_session_cached(session_id)
            mark("session_lookup_ms", t)
            if not session:
                self._log_attempt(
                    session_id, None, claimed, "FAIL", "Session not found", 0.0, ip_address, device_info, station_id
                )
                return False, "Examination session not found", 0.0

            now = datetime.utcnow()
            if not session.get("is_active"):
                start_time = session.get("start_time")
                end_time = session.get("end_time")
                if start_time and end_time and start_time <= now <= end_time:
                    db_utils.execute(
                        "UPDATE examination_sessions SET is_active = TRUE WHERE id = %s",
                        (session["id"],)
                    )
                    session["is_active"] = True
                else:
                    self._log_attempt(
                        session_id, None, claimed, "FAIL", "Session inactive", 0.0, ip_address, device_info, station_id
                    )
                    return False, "Examination session is not active", 0.0

            if not session.get("is_active"):
                self._log_attempt(
                    session_id, None, claimed, "FAIL", "Session inactive", 0.0, ip_address, device_info, station_id
                )
                return False, "Examination session is not active", 0.0

            if now < session.get("start_time"):
                self._log_attempt(
                    session_id, None, claimed, "FAIL", "Session not started", 0.0, ip_address, device_info, station_id
                )
                return False, "Examination session has not started", 0.0

            if now > session.get("end_time"):
                self._log_attempt(
                    session_id, None, claimed, "FAIL", "Session ended", 0.0, ip_address, device_info, station_id
                )
                return False, "Examination session has ended", 0.0

            if not invigilator_id:
                self._log_attempt(
                    session_id, None, claimed, "FAIL", "Invigilator not identified", 0.0, ip_address, device_info, station_id
                )
                return False, "Invigilator authentication required for this session", 0.0

            t = perf_counter()
            invigilator = self._get_invigilator_cached(invigilator_id)
            mark("invigilator_lookup_ms", t)
            if not invigilator or not invigilator.get("is_active"):
                self._log_attempt(
                    session_id,
                    None,
                    claimed,
                    "FAIL",
                    f"Invigilator {invigilator_id} not found or inactive",
                    0.0,
                    ip_address,
                    device_info,
                    station_id
                )
                return False, "Invigilator account not found or inactive", 0.0

            invigilator_role = str(invigilator.get("role") or "").strip().lower()
            is_admin_bypass = invigilator_role in {"admin", "super_admin"}

            t = perf_counter()
            assigned = self.is_invigilator_assigned(session["id"], int(invigilator_id))
            mark("invigilator_assignment_check_ms", t)
            if (not is_admin_bypass) and (not assigned):
                self._log_attempt(
                    session_id,
                    None,
                    claimed,
                    "FAIL",
                    f"Invigilator {invigilator_id} not assigned to session",
                    0.0,
                    ip_address,
                    device_info,
                    station_id
                )
                return False, "You are not assigned as invigilator for this exam session", 0.0

            if frames and len(frames) >= 2:
                ok, msg = self.face_engine.basic_liveness_check(frames, challenge)
                if not ok:
                    self._log_attempt(
                        session_id, None, claimed, "FAIL", f"Liveness failed: {msg}", 0.0, ip_address, device_info, station_id
                    )
                    return False, f"Liveness check failed: {msg}", 0.0

            t = perf_counter()
            live_emb = self.face_engine.extract_live_embedding(live_image)
            mark("extract_live_embedding_ms", t)
            if live_emb is None:
                self._log_attempt(
                    session_id, None, claimed, "FAIL", "No face detected in live image", 0.0, ip_address, device_info, station_id
                )
                return False, "No face detected in the image. Keep your full face centered and retry.", 0.0

            if student_id:
                student = self.student_service.get_student(student_id)
                if not student:
                    self._log_attempt(
                        session_id, None, claimed, "FAIL", "Student not found", 0.0, ip_address, device_info, station_id
                    )
                    return False, "Student not found", 0.0

                if not student.get("is_active"):
                    self._log_attempt(
                        session_id, student["id"], claimed, "FAIL", "Student inactive", 0.0, ip_address, device_info, station_id
                    )
                    return False, "Student account is inactive", 0.0

                if not self.is_student_registered_for_session(session["id"], student["id"]):
                    self._log_attempt(
                        session_id,
                        student["id"],
                        claimed,
                        "FAIL",
                        "Student not registered for session",
                        0.0,
                        ip_address,
                        device_info,
                        station_id
                    )
                    return False, "Student is not registered for this examination session", 0.0

                stored_encodings = self.student_service.get_face_encodings(student)
                is_match, confidence = self.face_engine.verify_live_embedding(live_emb, stored_encodings)

                if not is_match:
                    self._log_attempt(
                        session_id, student["id"], claimed, "FAIL", "Face mismatch", confidence, ip_address, device_info, station_id
                    )
                    return False, f"Identity verification failed. Confidence: {confidence:.2f}", confidence

                existing = db_utils.fetch_one(
                    "SELECT id FROM attendances WHERE student_id = %s AND session_id = %s",
                    (student["id"], session_id)
                )
                if existing:
                    self._log_attempt(
                        session_id, student["id"], claimed, "FAIL", "Duplicate attendance", confidence, ip_address, device_info, station_id
                    )
                    return False, "Attendance already recorded for this session", confidence

                attendance = db_utils.execute_returning(
                    """
                    INSERT INTO attendances
                        (student_id, session_id, verification_confidence, ip_address, device_info)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING *
                    """,
                    (student["id"], session_id, confidence, ip_address, device_info)
                )
                self._log_attempt(
                    session_id, student["id"], claimed, "SUCCESS", "ok", confidence, ip_address, device_info, station_id
                )

                if self._timing_log_enabled:
                    timings["total_ms"] = round((perf_counter() - t0) * 1000.0, 2)
                    logger.info(
                        "Verify timing | session=%s | mode=1:1 | total_ms=%s | %s",
                        session_id,
                        timings.get("total_ms"),
                        timings,
                    )
                logger.info(f"Attendance recorded: Student {student.get('student_id')} for session {session_id}")
                return True, self._attendance_to_dict(attendance, student=student, session=session), confidence

            try:
                cache = self.student_service.get_encoding_cache()
            except Exception:
                cache = None

            best_match = None
            best_confidence = 0.0
            best_attempt_confidence = 0.0

            t = perf_counter()
            registered_student_ids = self._get_registered_student_ids(session["id"], str(session.get("course_code") or "").strip())
            mark("eligible_students_lookup_ms", t)
            if not registered_student_ids:
                self._log_attempt(
                    session_id, None, claimed, "FAIL", "No registered students for session", 0.0, ip_address, device_info, station_id
                )
                return False, "No registered candidates found for this session yet. Please register students for this paper before verification.", 0.0

            if cache is not None:
                for s, encs in cache:
                    if s.get("id") not in registered_student_ids:
                        continue
                    is_match, conf = self.face_engine.verify_live_embedding(live_emb, encs)
                    best_attempt_confidence = max(best_attempt_confidence, float(conf))
                    if is_match and conf > best_confidence:
                        best_match = s
                        best_confidence = conf
            else:
                students = self.student_service.get_all_students(active_only=True)
                for s in students:
                    if s.get("id") not in registered_student_ids:
                        continue
                    encs = self.student_service.get_face_encodings(s)
                    is_match, conf = self.face_engine.verify_live_embedding(live_emb, encs)
                    best_attempt_confidence = max(best_attempt_confidence, float(conf))
                    if is_match and conf > best_confidence:
                        best_match = s
                        best_confidence = conf

            if not best_match:
                self._log_attempt(
                    session_id, None, None, "FAIL", "No biometric match found", best_attempt_confidence, ip_address, device_info, station_id
                )
                return False, "No biometric match found. Recapture in better lighting or enter Student ID for direct verification.", best_attempt_confidence

            existing = db_utils.fetch_one(
                "SELECT id FROM attendances WHERE student_id = %s AND session_id = %s",
                (best_match["id"], session_id)
            )
            if existing:
                self._log_attempt(
                    session_id, best_match["id"], None, "FAIL", "Duplicate attendance", best_confidence, ip_address, device_info, station_id
                )
                return False, "Attendance already recorded for this session", best_confidence

            attendance = db_utils.execute_returning(
                """
                INSERT INTO attendances
                    (student_id, session_id, verification_confidence, ip_address, device_info)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
                """,
                (best_match["id"], session_id, best_confidence, ip_address, device_info)
            )
            self._log_attempt(
                session_id, best_match["id"], None, "SUCCESS", "ok", best_confidence, ip_address, device_info, station_id
            )

            if self._timing_log_enabled:
                timings["total_ms"] = round((perf_counter() - t0) * 1000.0, 2)
                logger.info(
                    "Verify timing | session=%s | mode=1:N | total_ms=%s | %s",
                    session_id,
                    timings.get("total_ms"),
                    timings,
                )
            logger.info(f"Attendance recorded: Student {best_match.get('student_id')} for session {session_id}")
            return True, self._attendance_to_dict(attendance, student=best_match, session=session), best_confidence

        except Exception as e:
            logger.error(f"Attendance verification error: {str(e)}")
            return False, f"Verification failed: {str(e)}", 0.0

    def get_session_attendance(self, session_id):
        """Get all attendance records for a session"""
        try:
            session = self._get_session(session_id)
            if not session:
                return None

            attendances = self._fetch_attendances(session_id=session_id)
            return {
                'session': self._session_to_dict(session),
                'attendances': [self._attendance_row_to_dict(a) for a in attendances],
                'total_count': len(attendances)
            }
        except Exception as e:
            logger.error(f"Get session attendance error: {str(e)}")
            return None

    def get_student_attendance_history(self, student_id):
        """Get attendance history for a student"""
        try:
            student = self.student_service.get_student(student_id)
            if not student:
                return None

            attendances = self._fetch_attendances(student_id=student["id"])

            return {
                'student': self.student_service._student_to_dict(student),
                'attendances': [self._attendance_row_to_dict(a) for a in attendances],
                'total_count': len(attendances)
            }
        except Exception as e:
            logger.error(f"Get student attendance history error: {str(e)}")
            return None

    def create_session(self, session_data, created_by):
        """Create a new examination session"""
        try:
            start_time = self._parse_iso_utc_naive(session_data['start_time'])
            end_time = self._parse_iso_utc_naive(session_data['end_time'])
            if end_time <= start_time:
                return False, "end_time must be after start_time"

            session = db_utils.execute_returning(
                """
                INSERT INTO examination_sessions
                    (session_name, course_code, venue, start_time, end_time, created_by, is_active)
                VALUES (%s, %s, %s, %s, %s, %s, FALSE)
                RETURNING *
                """,
                (
                    session_data["session_name"],
                    session_data.get("course_code"),
                    session_data.get("venue"),
                    start_time,
                    end_time,
                    created_by,
                )
            )

            papers = session_data.get("papers", []) or []
            invigilator_ids = session_data.get("invigilator_ids", []) or []

            if papers:
                self.set_session_papers(session["id"], papers)
            if invigilator_ids:
                self.assign_invigilators(session["id"], invigilator_ids, assigned_by=created_by)

            logger.info(f"Session created: {session.get('session_name')} by admin {created_by}")
            return True, self._session_to_dict(session)

        except Exception as e:
            logger.error(f"Create session error: {str(e)}")
            return False, f"Session creation failed: {str(e)}"

    def is_student_registered_for_session(self, session_id, student_db_id):
        """Check if student is eligible via session registration or course registration."""
        row = db_utils.fetch_one(
            "SELECT id FROM exam_registrations WHERE session_id = %s AND student_id = %s",
            (session_id, student_db_id)
        )
        if row is not None:
            return True

        session_row = db_utils.fetch_one(
            "SELECT course_code FROM examination_sessions WHERE id = %s",
            (session_id,)
        ) or {}
        course_code = str(session_row.get("course_code") or "").strip()
        if not course_code:
            return False

        course_reg = db_utils.fetch_one(
            """
            SELECT id
            FROM student_course_registrations
            WHERE student_id = %s
              AND UPPER(course_code) = UPPER(%s)
              AND COALESCE(is_active, TRUE) = TRUE
            """,
            (student_db_id, course_code),
        )
        return course_reg is not None

    def register_students_for_session(self, session_id, student_identifiers, registered_by=None):
        """Register one or more students for a specific examination session."""
        try:
            session = self._get_session(session_id)
            if not session:
                return False, "Session not found"

            added = 0
            skipped = 0
            unresolved = []
            students = []

            for identifier in student_identifiers:
                student = self.student_service.get_student(identifier)
                if not student:
                    unresolved.append(str(identifier))
                    continue
                students.append(student)

            # Capacity guard: avoid over-registering students beyond session/hall limits.
            current_count_row = db_utils.fetch_one(
                "SELECT COUNT(*) AS c FROM exam_registrations WHERE session_id = %s",
                (session_id,)
            ) or {}
            current_count = int(current_count_row.get("c") or 0)
            expected_limit = session.get("expected_students")
            hall_capacity_limit = None
            if session.get("hall_id"):
                hall_row = db_utils.fetch_one(
                    "SELECT capacity FROM exam_halls WHERE id = %s AND is_active = TRUE",
                    (session.get("hall_id"),)
                )
                if hall_row and hall_row.get("capacity") is not None:
                    hall_capacity_limit = int(hall_row["capacity"])
            hard_limit = None
            if expected_limit is not None:
                hard_limit = int(expected_limit)
            if hall_capacity_limit is not None:
                hard_limit = min(hard_limit, hall_capacity_limit) if hard_limit is not None else hall_capacity_limit

            unique_new_students = {
                int(s["id"])
                for s in students
                if not db_utils.fetch_one(
                    "SELECT id FROM exam_registrations WHERE session_id = %s AND student_id = %s",
                    (session_id, s["id"])
                )
            }
            if hard_limit is not None and current_count + len(unique_new_students) > hard_limit:
                return (
                    False,
                    f"Registration exceeds hall/session limit. Current: {current_count}, "
                    f"attempted new: {len(unique_new_students)}, allowed: {hard_limit}."
                )

            for student in students:
                exists = db_utils.fetch_one(
                    "SELECT id FROM exam_registrations WHERE session_id = %s AND student_id = %s",
                    (session_id, student["id"])
                )
                if exists:
                    skipped += 1
                    continue
                db_utils.execute(
                    """
                    INSERT INTO exam_registrations (session_id, student_id, registered_by)
                    VALUES (%s, %s, %s)
                    """,
                    (session_id, student["id"], registered_by)
                )
                added += 1

            self._invalidate_session_caches(session_id)
            return True, {
                "session_id": session_id,
                "added": added,
                "skipped_existing": skipped,
                "unresolved_students": unresolved
            }
        except Exception as e:
            logger.error(f"Register students for session error: {str(e)}")
            return False, f"Failed to register students: {str(e)}"

    def remove_student_registration(self, session_id, student_identifier):
        """Remove a student from an exam session registration list."""
        try:
            student = self.student_service.get_student(student_identifier)
            if not student:
                return False, "Student not found"

            registration = db_utils.fetch_one(
                "SELECT * FROM exam_registrations WHERE session_id = %s AND student_id = %s",
                (session_id, student["id"])
            )
            if not registration:
                return False, "Registration not found"

            db_utils.execute(
                "DELETE FROM exam_registrations WHERE id = %s",
                (registration["id"],)
            )
            self._invalidate_session_caches(session_id)
            return True, self._registration_to_dict(registration, student)
        except Exception as e:
            logger.error(f"Remove registration error: {str(e)}")
            return False, f"Failed to remove registration: {str(e)}"

    def get_session_registrations(self, session_id):
        """Return all students registered for a session."""
        session = self._get_session(session_id)
        if not session:
            return None

        regs = db_utils.fetch_all(
            """
            SELECT
                r.*,
                s.id AS s_id, s.student_id AS s_student_id, s.first_name AS s_first_name,
                s.last_name AS s_last_name, s.email AS s_email, s.phone AS s_phone,
                s.department AS s_department, s.course AS s_course, s.year_level AS s_year_level,
                s.registration_date AS s_registration_date, s.is_active AS s_is_active,
                s.last_updated AS s_last_updated
            FROM exam_registrations r
            LEFT JOIN students s ON s.id = r.student_id
            WHERE r.session_id = %s
            ORDER BY r.registered_at DESC
            """,
            (session_id,)
        )
        students = [self._student_from_row(r) for r in regs if r.get("s_id") is not None]
        return {
            "session": self._session_to_dict(session),
            "registrations": [self._registration_row_to_dict(r) for r in regs],
            "students": students,
            "total_count": len(regs)
        }

    @staticmethod
    def _parse_iso_utc_naive(value):
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt

    def get_all_sessions(self, active_only=False):
        """Get all examination sessions"""
        try:
            if active_only:
                rows = db_utils.fetch_all(
                    "SELECT * FROM examination_sessions WHERE is_active = TRUE ORDER BY start_time DESC"
                )
            else:
                rows = db_utils.fetch_all(
                    "SELECT * FROM examination_sessions ORDER BY start_time DESC"
                )
            return [self._session_to_dict(r) for r in rows]
        except Exception as e:
            logger.error(f"Get all sessions error: {str(e)}")
            return []

    def end_session(self, session_id):
        """End a session immediately and disable further verifications."""
        try:
            session = self._get_session(session_id)
            if not session:
                return False, "Session not found"

            now = datetime.utcnow()
            end_time = session.get("end_time")
            if end_time is None or end_time > now:
                end_time = now
            db_utils.execute(
                "UPDATE examination_sessions SET is_active = FALSE, end_time = %s WHERE id = %s",
                (end_time, session_id)
            )
            logger.info(f"Session ended by admin: session_id={session_id}")
            updated = self._get_session(session_id)
            return True, self._session_to_dict(updated)
        except Exception as e:
            logger.error(f"End session error: {str(e)}")
            return False, f"Failed to end session: {str(e)}"

    def start_session(self, session_id):
        """Start a session manually and allow verifications."""
        try:
            session = self._get_session(session_id)
            if not session:
                return False, "Session not found"

            now = datetime.utcnow()
            start_time = session.get("start_time")
            end_time = session.get("end_time")

            if end_time is not None and end_time <= now:
                return False, "Session end time has passed. Update/create a new session."

            effective_start = start_time if start_time and start_time <= now else now
            db_utils.execute(
                "UPDATE examination_sessions SET is_active = TRUE, start_time = %s WHERE id = %s",
                (effective_start, session_id)
            )
            logger.info(f"Session started by admin: session_id={session_id}")
            updated = self._get_session(session_id)
            return True, self._session_to_dict(updated)
        except Exception as e:
            logger.error(f"Start session error: {str(e)}")
            return False, f"Failed to start session: {str(e)}"

    def set_session_papers(self, session_id, papers):
        """Replace papers list for a session."""
        try:
            session = self._get_session(session_id)
            if not session:
                return False, "Session not found"

            db_utils.execute("DELETE FROM exam_papers WHERE session_id = %s", (session_id,))
            for paper in papers:
                if isinstance(paper, str):
                    title = paper.strip()
                    code = None
                else:
                    title = str((paper or {}).get("paper_title") or "").strip()
                    code = str((paper or {}).get("paper_code") or "").strip() or None
                if not title:
                    continue
                db_utils.execute(
                    """
                    INSERT INTO exam_papers (session_id, paper_code, paper_title)
                    VALUES (%s, %s, %s)
                    """,
                    (session_id, code, title)
                )
            return True, self.get_session_papers(session_id)
        except Exception as e:
            logger.error(f"Set session papers error: {str(e)}")
            return False, f"Failed to set papers: {str(e)}"

    def get_session_papers(self, session_id):
        papers = db_utils.fetch_all(
            "SELECT * FROM exam_papers WHERE session_id = %s ORDER BY created_at DESC",
            (session_id,)
        )
        return [self._paper_to_dict(p) for p in papers]

    def assign_invigilators(self, session_id, invigilator_ids, assigned_by=None):
        """Assign invigilators to a session (replace current assignments)."""
        try:
            session = self._get_session(session_id)
            if not session:
                return False, "Session not found"

            resolved = []
            unresolved = []
            for raw_id in invigilator_ids:
                try:
                    inv_id = int(raw_id)
                except (TypeError, ValueError):
                    unresolved.append(str(raw_id))
                    continue
                invigilator = db_utils.fetch_one(
                    "SELECT id, is_active FROM admins WHERE id = %s",
                    (inv_id,)
                )
                if not invigilator or not invigilator.get("is_active"):
                    unresolved.append(str(raw_id))
                    continue
                resolved.append(invigilator["id"])

            db_utils.execute("DELETE FROM session_invigilators WHERE session_id = %s", (session_id,))
            for inv_id in sorted(set(resolved)):
                db_utils.execute(
                    """
                    INSERT INTO session_invigilators (session_id, invigilator_id, assigned_at, assigned_by, is_active)
                    VALUES (%s, %s, %s, %s, TRUE)
                    """,
                    (session_id, inv_id, datetime.utcnow(), assigned_by)
                )
            self._invalidate_session_caches(session_id)
            return True, {
                "session_id": session_id,
                "assigned_invigilators": resolved,
                "unresolved_invigilators": unresolved
            }
        except Exception as e:
            logger.error(f"Assign invigilators error: {str(e)}")
            return False, f"Failed to assign invigilators: {str(e)}"

    def get_session_invigilators(self, session_id):
        assignments = db_utils.fetch_all(
            """
            SELECT
                si.*,
                a.id AS a_id, a.username AS a_username, a.email AS a_email,
                a.full_name AS a_full_name, a.role AS a_role, a.is_active AS a_is_active,
                a.last_login AS a_last_login, a.created_at AS a_created_at
            FROM session_invigilators si
            LEFT JOIN admins a ON a.id = si.invigilator_id
            WHERE si.session_id = %s AND si.is_active = TRUE
            ORDER BY si.assigned_at DESC
            """,
            (session_id,)
        )
        return [self._session_invigilator_row_to_dict(a) for a in assignments]

    def is_invigilator_assigned(self, session_id, invigilator_id):
        cache_key = (int(session_id), int(invigilator_id))
        cached = self._cache_get(self._invigilator_assignment_cache, cache_key)
        if cached is not None:
            return bool(cached)
        row = db_utils.fetch_one(
            """
            SELECT id FROM session_invigilators
            WHERE session_id = %s AND invigilator_id = %s AND is_active = TRUE
            """,
            (session_id, invigilator_id)
        )
        out = row is not None
        self._cache_set(self._invigilator_assignment_cache, cache_key, out, self._assignment_cache_ttl)
        return out

    def _get_session(self, session_id):
        return db_utils.fetch_one("SELECT * FROM examination_sessions WHERE id = %s", (session_id,))

    def _session_to_dict(self, row):
        if not row:
            return None
        return {
            "id": row.get("id"),
            "session_name": row.get("session_name"),
            "course_code": row.get("course_code"),
            "venue": row.get("venue"),
            "hall_id": row.get("hall_id"),
            "expected_students": row.get("expected_students"),
            "start_time": self._fmt_dt(row.get("start_time")),
            "end_time": self._fmt_dt(row.get("end_time")),
            "created_by": row.get("created_by"),
            "created_at": self._fmt_dt(row.get("created_at")),
            "is_active": row.get("is_active")
        }

    def _student_from_row(self, row):
        if not row or row.get("s_id") is None:
            return None
        return {
            "id": row.get("s_id"),
            "student_id": row.get("s_student_id"),
            "first_name": row.get("s_first_name"),
            "last_name": row.get("s_last_name"),
            "email": row.get("s_email"),
            "phone": row.get("s_phone"),
            "department": row.get("s_department"),
            "course": row.get("s_course"),
            "year_level": row.get("s_year_level"),
            "registration_date": self._fmt_dt(row.get("s_registration_date")),
            "is_active": row.get("s_is_active"),
            "last_updated": self._fmt_dt(row.get("s_last_updated"))
        }

    def _attendance_row_to_dict(self, row):
        return {
            "id": row.get("id"),
            "student_id": row.get("student_id"),
            "student": self._student_from_row(row),
            "session_id": row.get("session_id"),
            "session": self._session_from_row(row),
            "timestamp": self._fmt_dt(row.get("timestamp")),
            "verification_confidence": row.get("verification_confidence"),
            "verification_method": row.get("verification_method"),
            "ip_address": row.get("ip_address"),
            "device_info": row.get("device_info")
        }

    def _attendance_to_dict(self, row, student=None, session=None):
        return {
            "id": row.get("id"),
            "student_id": row.get("student_id"),
            "student": self.student_service._student_to_dict(student) if student else None,
            "session_id": row.get("session_id"),
            "session": self._session_to_dict(session) if session else None,
            "timestamp": self._fmt_dt(row.get("timestamp")),
            "verification_confidence": row.get("verification_confidence"),
            "verification_method": row.get("verification_method"),
            "ip_address": row.get("ip_address"),
            "device_info": row.get("device_info")
        }

    def _session_from_row(self, row):
        if not row or row.get("es_id") is None:
            return None
        return {
            "id": row.get("es_id"),
            "session_name": row.get("es_session_name"),
            "course_code": row.get("es_course_code"),
            "venue": row.get("es_venue"),
            "hall_id": row.get("es_hall_id"),
            "expected_students": row.get("es_expected_students"),
            "start_time": self._fmt_dt(row.get("es_start_time")),
            "end_time": self._fmt_dt(row.get("es_end_time")),
            "created_by": row.get("es_created_by"),
            "created_at": self._fmt_dt(row.get("es_created_at")),
            "is_active": row.get("es_is_active")
        }

    def _paper_to_dict(self, row):
        return {
            "id": row.get("id"),
            "session_id": row.get("session_id"),
            "paper_code": row.get("paper_code"),
            "paper_title": row.get("paper_title"),
            "created_at": self._fmt_dt(row.get("created_at"))
        }

    def _registration_row_to_dict(self, row):
        return {
            "id": row.get("id"),
            "session_id": row.get("session_id"),
            "student_id": row.get("student_id"),
            "registered_at": self._fmt_dt(row.get("registered_at")),
            "registered_by": row.get("registered_by"),
            "student": self._student_from_row(row)
        }

    def _registration_to_dict(self, row, student=None):
        return {
            "id": row.get("id"),
            "session_id": row.get("session_id"),
            "student_id": row.get("student_id"),
            "registered_at": self._fmt_dt(row.get("registered_at")),
            "registered_by": row.get("registered_by"),
            "student": self.student_service._student_to_dict(student) if student else None
        }

    def _session_invigilator_row_to_dict(self, row):
        return {
            "id": row.get("id"),
            "session_id": row.get("session_id"),
            "invigilator_id": row.get("invigilator_id"),
            "assigned_at": self._fmt_dt(row.get("assigned_at")),
            "assigned_by": row.get("assigned_by"),
            "is_active": row.get("is_active"),
            "invigilator": {
                "id": row.get("a_id"),
                "username": row.get("a_username"),
                "email": row.get("a_email"),
                "full_name": row.get("a_full_name"),
                "role": row.get("a_role"),
                "is_active": row.get("a_is_active"),
                "last_login": self._fmt_dt(row.get("a_last_login")),
                "created_at": self._fmt_dt(row.get("a_created_at"))
            } if row.get("a_id") else None
        }

    def _fetch_attendances(self, session_id=None, student_id=None):
        clauses = []
        params = []
        if session_id:
            clauses.append("a.session_id = %s")
            params.append(session_id)
        if student_id:
            clauses.append("a.student_id = %s")
            params.append(student_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        sql = f"""
            SELECT
                a.*,
                s.id AS s_id, s.student_id AS s_student_id, s.first_name AS s_first_name,
                s.last_name AS s_last_name, s.email AS s_email, s.phone AS s_phone,
                s.department AS s_department, s.course AS s_course, s.year_level AS s_year_level,
                s.registration_date AS s_registration_date, s.is_active AS s_is_active,
                s.last_updated AS s_last_updated,
                es.id AS es_id, es.session_name AS es_session_name, es.course_code AS es_course_code,
                es.venue AS es_venue, es.hall_id AS es_hall_id, es.expected_students AS es_expected_students,
                es.start_time AS es_start_time, es.end_time AS es_end_time,
                es.created_by AS es_created_by, es.created_at AS es_created_at,
                es.is_active AS es_is_active
            FROM attendances a
            LEFT JOIN students s ON s.id = a.student_id
            LEFT JOIN examination_sessions es ON es.id = a.session_id
            {where}
            ORDER BY a.timestamp DESC
        """
        return db_utils.fetch_all(sql, tuple(params))
