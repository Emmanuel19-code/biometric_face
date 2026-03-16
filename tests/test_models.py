"""Tests for database layer (raw Postgres)."""
import unittest
import json
import numpy as np
from werkzeug.security import generate_password_hash, check_password_hash

from utils import db as db_utils
from utils.encryption import encrypt_data


class TestDatabase(unittest.TestCase):
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

    def test_admin_insert_and_password(self):
        pwd_hash = generate_password_hash("testpass123")
        db_utils.execute(
            """
            INSERT INTO admins (username, email, full_name, role, password_hash)
            VALUES (%s, %s, %s, %s, %s)
            """,
            ("testadmin", "test@example.com", "Test Admin", "admin", pwd_hash)
        )
        row = db_utils.fetch_one("SELECT * FROM admins WHERE username = %s", ("testadmin",))
        self.assertIsNotNone(row)
        self.assertEqual(row["email"], "test@example.com")
        self.assertTrue(check_password_hash(row["password_hash"], "testpass123"))

    def test_student_insert(self):
        encodings = [np.random.rand(128).astype(float).tolist() for _ in range(3)]
        enc_json = json.dumps(encodings)
        encrypted = encrypt_data(enc_json)
        db_utils.execute(
            """
            INSERT INTO students (student_id, first_name, last_name, email, face_encodings)
            VALUES (%s, %s, %s, %s, %s)
            """,
            ("STU001", "John", "Doe", "john@example.com", encrypted)
        )
        row = db_utils.fetch_one("SELECT * FROM students WHERE student_id = %s", ("STU001",))
        self.assertIsNotNone(row)
        self.assertEqual(row["first_name"], "John")

    def test_session_and_attendance(self):
        admin = db_utils.execute_returning(
            """
            INSERT INTO admins (username, email, full_name, role, password_hash)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING *
            """,
            ("admin", "admin@example.com", "Admin", "admin", generate_password_hash("pass"))
        )
        student = db_utils.execute_returning(
            """
            INSERT INTO students (student_id, first_name, last_name, email, face_encodings)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING *
            """,
            ("STU001", "John", "Doe", "john@example.com", encrypt_data(json.dumps([[0.1] * 128])))
        )
        session = db_utils.execute_returning(
            """
            INSERT INTO examination_sessions (session_name, course_code, venue, start_time, end_time, created_by)
            VALUES (%s, %s, %s, NOW(), NOW() + INTERVAL '1 hour', %s)
            RETURNING *
            """,
            ("Test Exam", "CS101", "Hall A", admin["id"])
        )
        db_utils.execute(
            """
            INSERT INTO attendances (student_id, session_id, verification_confidence)
            VALUES (%s, %s, %s)
            """,
            (student["id"], session["id"], 0.95)
        )
        row = db_utils.fetch_one(
            "SELECT * FROM attendances WHERE student_id = %s AND session_id = %s",
            (student["id"], session["id"])
        )
        self.assertIsNotNone(row)
        self.assertEqual(row["verification_confidence"], 0.95)


if __name__ == '__main__':
    unittest.main()