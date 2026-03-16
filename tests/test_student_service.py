"""Tests for student service (raw Postgres)."""
import unittest
import json

from utils import db as db_utils
from utils.encryption import encrypt_data
from services.student_service import StudentService


class TestStudentService(unittest.TestCase):
    def setUp(self):
        try:
            db_utils.init_pool()
            db_utils.init_db_schema()
        except Exception as exc:
            self.skipTest(f"DB not reachable: {exc}")

        db_utils.execute(
            "TRUNCATE TABLE verification_logs, verification_challenges, exam_registrations, "
            "session_invigilators, exam_papers, attendances, exam_stations, "
            "examination_sessions, students, admins CASCADE"
        )
        self.service = StudentService()

    def test_update_and_deactivate(self):
        enc = encrypt_data(json.dumps([[0.1] * 128]))
        db_utils.execute(
            """
            INSERT INTO students (student_id, first_name, last_name, email, face_encodings)
            VALUES (%s, %s, %s, %s, %s)
            """,
            ("STU001", "John", "Doe", "john@example.com", enc)
        )

        ok, updated = self.service.update_student("STU001", {"first_name": "Jane"})
        self.assertTrue(ok)
        self.assertEqual(updated["first_name"], "Jane")

        ok, deactivated = self.service.deactivate_student("STU001")
        self.assertTrue(ok)
        self.assertFalse(deactivated["is_active"])


if __name__ == '__main__':
    unittest.main()