"""Tests for service layer (raw Postgres)."""
import unittest
import json
import numpy as np
from werkzeug.security import generate_password_hash

from utils import db as db_utils
from utils.encryption import encrypt_data
from services.admin_service import AdminService
from services.student_service import StudentService
from services.attendance_service import AttendanceService


class BaseDbTest(unittest.TestCase):
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


class TestAdminService(BaseDbTest):
    def test_create_admin(self):
        service = AdminService()
        admin_data = {
            'username': 'testadmin',
            'email': 'test@example.com',
            'full_name': 'Test Admin',
            'password': 'testpass123',
            'role': 'admin'
        }

        success, result = service.create_admin(admin_data)
        self.assertTrue(success)
        self.assertIsInstance(result, dict)
        self.assertEqual(result['username'], 'testadmin')

    def test_authenticate_admin(self):
        service = AdminService()
        admin_data = {
            'username': 'testadmin',
            'email': 'test@example.com',
            'full_name': 'Test Admin',
            'password': 'testpass123',
            'role': 'admin'
        }
        service.create_admin(admin_data)

        success, admin = service.authenticate_admin('testadmin', 'testpass123')
        self.assertTrue(success)
        self.assertIsNotNone(admin)

        success, admin = service.authenticate_admin('testadmin', 'wrongpass')
        self.assertFalse(success)


class TestStudentService(BaseDbTest):
    def test_get_student(self):
        service = StudentService()
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

        retrieved = service.get_student('STU001')
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved['student_id'], 'STU001')

        retrieved = service.get_student(999999)
        self.assertIsNone(retrieved)

    def test_get_all_students(self):
        service = StudentService()
        for i in range(3):
            enc = encrypt_data(json.dumps([[0.1] * 128]))
            db_utils.execute(
                """
                INSERT INTO students (student_id, first_name, last_name, email, face_encodings)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (f"STU{i:03d}", f"Student{i}", "Test", f"student{i}@example.com", enc)
            )

        students = service.get_all_students()
        self.assertEqual(len(students), 3)

        students = service.get_all_students(active_only=True)
        self.assertEqual(len(students), 3)


class TestAttendanceService(BaseDbTest):
    def test_create_session_and_list(self):
        service = AttendanceService()

        admin = db_utils.execute_returning(
            """
            INSERT INTO admins (username, email, full_name, role, password_hash)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING *
            """,
            ("admin", "admin@example.com", "Admin", "admin", generate_password_hash("pass"))
        )

        session_data = {
            'session_name': 'Test Exam',
            'course_code': 'CS101',
            'venue': 'Hall A',
            'start_time': '2026-02-15T09:00:00Z',
            'end_time': '2026-02-15T12:00:00Z'
        }

        success, result = service.create_session(session_data, admin['id'])
        self.assertTrue(success)
        self.assertIsInstance(result, dict)
        self.assertEqual(result['session_name'], 'Test Exam')

        sessions = service.get_all_sessions()
        self.assertEqual(len(sessions), 1)


if __name__ == '__main__':
    unittest.main()