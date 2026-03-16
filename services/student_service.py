"""Student registration and management service"""
import json
import logging
import numpy as np
import re
from datetime import datetime
import secrets

from utils.face_recognition_engine import FaceRecognitionEngine
from utils.encryption import encrypt_data, decrypt_data
from utils import db as db_utils
from werkzeug.security import generate_password_hash

logger = logging.getLogger(__name__)


class StudentService:
    """Service for student operations"""

    def __init__(self):
        self.face_engine = FaceRecognitionEngine()
        self._encoding_cache = None

    @staticmethod
    def _program_code(program_name):
        tokens = re.findall(r"[A-Za-z0-9]+", str(program_name or "").upper())
        if not tokens:
            return "GEN"
        letters = "".join(t[0] for t in tokens if t)
        if not letters:
            letters = "".join(tokens)[:3]
        return (letters[:3]).ljust(3, "X")

    @staticmethod
    def _level_code(level_name):
        raw = str(level_name or "").strip()
        digits = "".join(ch for ch in raw if ch.isdigit())
        if digits:
            return digits.zfill(3)[:3]
        cleaned = re.sub(r"[^A-Za-z0-9]", "", raw.upper())
        return (cleaned[:3]).ljust(3, "X") if cleaned else "000"

    def _generate_student_id(self, program_name, level_name):
        now = datetime.utcnow()
        year = now.year
        year_start = datetime(year, 1, 1)
        next_year = datetime(year + 1, 1, 1)
        prefix = f"{year}{self._program_code(program_name)}{self._level_code(level_name)}"

        rows = db_utils.fetch_all(
            """
            SELECT student_id
            FROM students
            WHERE LOWER(course) = LOWER(%s)
              AND LOWER(year_level) = LOWER(%s)
              AND registration_date >= %s
              AND registration_date < %s
            """,
            (program_name, level_name, year_start, next_year),
        )
        max_seq = 0
        for row in rows:
            sid = str(row.get("student_id") or "").strip().upper()
            if not sid.startswith(prefix):
                continue
            suffix = sid[len(prefix):]
            if suffix.isdigit():
                max_seq = max(max_seq, int(suffix))
        return f"{prefix}{str(max_seq + 1).zfill(4)}"

    def register_student(self, student_data, face_images, profile_photo=None):
        """
        Register a new student with facial biometric data
        Returns:
            (success, student_object or error_message)
        """
        try:
            program_name = (student_data.get("course") or "").strip()
            level_name = (student_data.get("year_level") or "").strip()
            if not program_name:
                return False, "Program/course is required for automatic index generation"
            if not level_name:
                return False, "Year level is required for automatic index generation"

            existing_email = db_utils.fetch_one(
                "SELECT id FROM students WHERE email = %s",
                (student_data["email"],)
            )
            if existing_email:
                return False, "Email already registered"

            student_data["student_id"] = self._generate_student_id(program_name, level_name)

            # Validate and capture face encodings
            face_encodings = self.face_engine.capture_multiple_angles(face_images)
            if not face_encodings:
                return False, f"Failed to capture sufficient face data. Required: {len(face_images)} angles"

            # Validate image quality for each image
            for idx, image in enumerate(face_images):
                is_valid, message = self.face_engine.validate_image_quality(image)
                if not is_valid:
                    return False, f"Image {idx + 1} validation failed: {message}"

            encodings_json = json.dumps([encoding.tolist() for encoding in face_encodings])
            encrypted_encodings = encrypt_data(encodings_json)

            temporary_password = student_data.get("default_password")
            if not temporary_password:
                temporary_password = secrets.token_urlsafe(8)

            student = db_utils.execute_returning(
                """
                INSERT INTO students
                    (student_id, first_name, last_name, email, phone, department, course, year_level, face_encodings, profile_photo, admission_academic_year, password_hash, must_change_password)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
                RETURNING *
                """,
                (
                    student_data["student_id"],
                    student_data["first_name"],
                    student_data["last_name"],
                    student_data["email"],
                    student_data.get("phone"),
                    student_data.get("department"),
                    student_data.get("course"),
                    student_data.get("year_level"),
                    encrypted_encodings,
                    profile_photo,
                    student_data.get("admission_academic_year"),
                    generate_password_hash(temporary_password),
                )
            )

            # 🔁 Invalidate cache so new student is included for 1:N
            self.invalidate_encoding_cache()

            logger.info(f"Student registered: {student_data['student_id']}")
            response = self._student_to_dict(student)
            response["temporary_password"] = temporary_password
            return True, response

        except Exception as e:
            logger.error(f"Student registration error: {str(e)}")
            return False, f"Registration failed: {str(e)}"

    def get_student(self, student_id):
        """Get student by ID (int) or student_id (string)"""
        try:
            if isinstance(student_id, int):
                return db_utils.fetch_one("SELECT * FROM students WHERE id = %s", (student_id,))
            return db_utils.fetch_one("SELECT * FROM students WHERE student_id = %s", (str(student_id),))
        except Exception as e:
            logger.error(f"Get student error: {str(e)}")
            return None

    def get_all_students(self, active_only=True):
        """Get all students"""
        try:
            if active_only:
                return db_utils.fetch_all("SELECT * FROM students WHERE is_active = TRUE ORDER BY id ASC")
            return db_utils.fetch_all("SELECT * FROM students ORDER BY id ASC")
        except Exception as e:
            logger.error(f"Get all students error: {str(e)}")
            return []

    def update_student(self, student_id, update_data):
        """Update student information"""
        try:
            student = self.get_student(student_id)
            if not student:
                return False, "Student not found"

            allowed = {
                "first_name", "last_name", "email", "phone",
                "department", "course", "year_level", "is_active"
            }
            fields = []
            params = []
            for key, value in update_data.items():
                if key in allowed:
                    fields.append(f"{key} = %s")
                    params.append(value)
            if fields:
                fields.append("last_updated = %s")
                params.append(datetime.utcnow())
                params.append(student["id"])
                sql = "UPDATE students SET " + ", ".join(fields) + " WHERE id = %s"
                db_utils.execute(sql, tuple(params))

            # 🔁 Invalidate cache because data may affect filtering/active flag
            self.invalidate_encoding_cache()

            updated = self.get_student(student_id)
            logger.info(f"Student updated: {updated['student_id'] if updated else student_id}")
            return True, self._student_to_dict(updated)

        except Exception as e:
            logger.error(f"Update student error: {str(e)}")
            return False, f"Update failed: {str(e)}"

    def deactivate_student(self, student_id):
        """Deactivate a student"""
        try:
            student = self.get_student(student_id)
            if not student:
                return False, "Student not found"

            db_utils.execute(
                "UPDATE students SET is_active = FALSE, last_updated = %s WHERE id = %s",
                (datetime.utcnow(), student["id"])
            )

            # 🔁 Invalidate cache because student removed from 1:N
            self.invalidate_encoding_cache()

            updated = self.get_student(student_id)
            logger.info(f"Student deactivated: {student['student_id']}")
            return True, self._student_to_dict(updated)

        except Exception as e:
            logger.error(f"Deactivate student error: {str(e)}")
            return False, f"Deactivation failed: {str(e)}"

    def build_encoding_cache(self):
        """Load active students + decrypted encodings once into memory."""
        students = self.get_all_students(active_only=True)
        cache = []
        for s in students:
            try:
                cache.append((s, self.get_face_encodings(s)))
            except Exception as e:
                logger.warning(f"Failed to load encodings for {s.get('student_id')}: {e}")
        self._encoding_cache = cache
        return cache

    def get_encoding_cache(self):
        """Return cached encodings; build if missing."""
        if self._encoding_cache is None:
            return self.build_encoding_cache()
        return self._encoding_cache

    def invalidate_encoding_cache(self):
        """Clear cached encodings."""
        self._encoding_cache = None

    def get_face_encodings(self, student_row):
        decrypted = decrypt_data(student_row["face_encodings"])
        encodings_list = json.loads(decrypted)
        return [np.array(encoding) for encoding in encodings_list]

    def _student_to_dict(self, row, include_encodings=False):
        if not row:
            return None
        data = {
            "id": row.get("id"),
            "student_id": row.get("student_id"),
            "first_name": row.get("first_name"),
            "last_name": row.get("last_name"),
            "email": row.get("email"),
            "phone": row.get("phone"),
            "department": row.get("department"),
            "course": row.get("course"),
            "year_level": row.get("year_level"),
            "profile_photo": row.get("profile_photo"),
            "admission_academic_year": row.get("admission_academic_year"),
            "registration_date": row.get("registration_date").isoformat() if row.get("registration_date") else None,
            "is_active": row.get("is_active"),
            "last_updated": row.get("last_updated").isoformat() if row.get("last_updated") else None
        }
        if include_encodings:
            data["face_encodings_count"] = len(self.get_face_encodings(row))
        return data
