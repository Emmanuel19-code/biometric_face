"""Admin service for dashboard and management"""
from datetime import datetime, timedelta
import logging
from werkzeug.security import generate_password_hash, check_password_hash

from utils import db as db_utils

logger = logging.getLogger(__name__)


class AdminService:
    """Service for admin operations"""

    @staticmethod
    def _fmt_dt(value):
        return value.isoformat() if value else None

    def _admin_to_dict(self, row):
        if not row:
            return None
        return {
            "id": row.get("id"),
            "username": row.get("username"),
            "email": row.get("email"),
            "full_name": row.get("full_name"),
            "role": row.get("role"),
            "is_active": row.get("is_active"),
            "last_login": self._fmt_dt(row.get("last_login")),
            "created_at": self._fmt_dt(row.get("created_at"))
        }
    
    def create_admin(self, admin_data):
        """Create a new admin user"""
        try:
            existing = db_utils.fetch_one(
                "SELECT id FROM admins WHERE username = %s",
                (admin_data["username"],)
            )
            if existing:
                return False, "Username already exists"
            
            existing_email = db_utils.fetch_one(
                "SELECT id FROM admins WHERE email = %s",
                (admin_data["email"],)
            )
            if existing_email:
                return False, "Email already exists"
            
            admin = db_utils.execute_returning(
                """
                INSERT INTO admins (username, email, full_name, role, password_hash, is_active)
                VALUES (%s, %s, %s, %s, %s, TRUE)
                RETURNING *
                """,
                (
                    admin_data["username"],
                    admin_data["email"],
                    admin_data["full_name"],
                    admin_data.get("role", "admin"),
                    generate_password_hash(admin_data["password"])
                )
            )
            
            logger.info(f"Admin created: {admin_data['username']}")
            return True, self._admin_to_dict(admin)
        
        except Exception as e:
            logger.error(f"Create admin error: {str(e)}")
            return False, f"Admin creation failed: {str(e)}"
    
    def authenticate_admin(self, username, password):
        """Authenticate admin user"""
        try:
            admin = db_utils.fetch_one(
                "SELECT * FROM admins WHERE username = %s",
                (username,)
            )
            if not admin:
                return False, None
            
            if not admin.get("is_active"):
                return False, None
            
            if not check_password_hash(admin["password_hash"], password):
                return False, None
            
            # Update last login
            db_utils.execute(
                "UPDATE admins SET last_login = %s WHERE id = %s",
                (datetime.utcnow(), admin["id"])
            )
            
            return True, self._admin_to_dict(admin)
        
        except Exception as e:
            logger.error(f"Admin authentication error: {str(e)}")
            return False, None
    
    def get_system_stats(self):
        """Get system statistics for dashboard"""
        try:
            total_students = db_utils.fetch_one("SELECT COUNT(*) AS c FROM students")["c"]
            active_students = db_utils.fetch_one(
                "SELECT COUNT(*) AS c FROM students WHERE is_active = TRUE"
            )["c"]
            total_sessions = db_utils.fetch_one("SELECT COUNT(*) AS c FROM examination_sessions")["c"]
            active_sessions = db_utils.fetch_one(
                "SELECT COUNT(*) AS c FROM examination_sessions WHERE is_active = TRUE"
            )["c"]
            total_attendances = db_utils.fetch_one("SELECT COUNT(*) AS c FROM attendances")["c"]
            today_attendances = db_utils.fetch_one(
                "SELECT COUNT(*) AS c FROM attendances WHERE timestamp >= %s",
                (datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0),)
            )["c"]
            this_week_attendances = db_utils.fetch_one(
                "SELECT COUNT(*) AS c FROM attendances WHERE timestamp >= %s",
                (datetime.utcnow() - timedelta(days=7),)
            )["c"]
            this_month_attendances = db_utils.fetch_one(
                "SELECT COUNT(*) AS c FROM attendances WHERE timestamp >= %s",
                (datetime.utcnow() - timedelta(days=30),)
            )["c"]

            stats = {
                'total_students': total_students,
                'active_students': active_students,
                'total_sessions': total_sessions,
                'active_sessions': active_sessions,
                'total_attendances': total_attendances,
                'today_attendances': today_attendances,
                'this_week_attendances': this_week_attendances,
                'this_month_attendances': this_month_attendances
            }
            
            return stats
        
        except Exception as e:
            logger.error(f"Get system stats error: {str(e)}")
            return {}
    
    def generate_attendance_report(self, session_id=None, start_date=None, end_date=None, student_id=None):
        """Generate attendance report"""
        try:
            clauses = []
            params = []
            if session_id:
                clauses.append("a.session_id = %s")
                params.append(session_id)
            if student_id:
                clauses.append("a.student_id = %s")
                params.append(student_id)
            if start_date:
                clauses.append("a.timestamp >= %s")
                params.append(start_date)
            if end_date:
                clauses.append("a.timestamp <= %s")
                params.append(end_date)

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
                    es.venue AS es_venue, es.start_time AS es_start_time, es.end_time AS es_end_time,
                    es.created_by AS es_created_by, es.created_at AS es_created_at,
                    es.is_active AS es_is_active
                FROM attendances a
                LEFT JOIN students s ON s.id = a.student_id
                LEFT JOIN examination_sessions es ON es.id = a.session_id
                {where}
                ORDER BY a.timestamp DESC
            """
            attendances = db_utils.fetch_all(sql, tuple(params))

            report = {
                'generated_at': datetime.utcnow().isoformat(),
                'filters': {
                    'session_id': session_id,
                    'start_date': start_date.isoformat() if start_date else None,
                    'end_date': end_date.isoformat() if end_date else None,
                    'student_id': student_id
                },
                'total_records': len(attendances),
                'attendances': [self._attendance_row_to_dict(a) for a in attendances]
            }
            
            return report
        
        except Exception as e:
            logger.error(f"Generate attendance report error: {str(e)}")
            return None

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

    def _session_from_row(self, row):
        if not row or row.get("es_id") is None:
            return None
        return {
            "id": row.get("es_id"),
            "session_name": row.get("es_session_name"),
            "course_code": row.get("es_course_code"),
            "venue": row.get("es_venue"),
            "start_time": self._fmt_dt(row.get("es_start_time")),
            "end_time": self._fmt_dt(row.get("es_end_time")),
            "created_by": row.get("es_created_by"),
            "created_at": self._fmt_dt(row.get("es_created_at")),
            "is_active": row.get("es_is_active")
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
