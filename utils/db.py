from contextlib import contextmanager
import os
import re
import psycopg2
from psycopg2 import pool, extras

from config import get_database_backend, get_database_dsn

try:
    import pyodbc
except Exception:  # pragma: no cover - optional dependency
    pyodbc = None

_POOL = None


def _get_dsn():
    dsn = get_database_dsn()
    backend = get_database_backend()
    if backend == "postgresql":
        if not dsn.startswith("postgresql://") and not dsn.startswith("postgres://"):
            raise RuntimeError("DATABASE_URL must be a Postgres URL")
    elif backend == "sqlserver":
        if pyodbc is None:
            raise RuntimeError(
                "pyodbc is required for SQL Server. Install it and ODBC Driver 18 for SQL Server."
            )
    else:
        raise RuntimeError(
            "No supported database configured. Set DATABASE_URL (Postgres) "
            "or SQLSERVER_HOST/SQLSERVER_DATABASE for local SQL Server."
        )
    return dsn


def _parse_sqlserver_dsn(dsn):
    parts = {}
    for chunk in dsn.split(";"):
        if "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        parts[key.strip().upper()] = value.strip()
    return parts


def _get_sqlserver_database_name():
    parts = _parse_sqlserver_dsn(_get_dsn())
    return (parts.get("DATABASE") or "").strip().lower()


def _replace_sqlserver_database(dsn, database_name):
    chunks = dsn.split(";")
    out = []
    replaced = False
    for chunk in chunks:
        if "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        if key.strip().upper() == "DATABASE":
            out.append(f"{key.strip()}={database_name}")
            replaced = True
        else:
            out.append(f"{key.strip()}={value.strip()}")
    if not replaced:
        out.append(f"DATABASE={database_name}")
    return ";".join(out)


def _ensure_sqlserver_database_exists():
    auto_create = os.getenv("SQLSERVER_AUTO_CREATE_DB", "true").strip().lower() in ("1", "true", "yes")
    if not auto_create:
        return

    dsn = _get_dsn()
    parts = _parse_sqlserver_dsn(dsn)
    database_name = parts.get("DATABASE", "").strip()
    if not database_name:
        return

    master_dsn = _replace_sqlserver_database(dsn, "master")
    try:
        conn = pyodbc.connect(master_dsn, timeout=5, autocommit=True)
        try:
            cur = conn.cursor()
            escaped_for_literal = database_name.replace("'", "''")
            escaped_for_identifier = database_name.replace("]", "]]")
            cur.execute(
                f"""
                IF DB_ID(N'{escaped_for_literal}') IS NULL
                BEGIN
                    EXEC(N'CREATE DATABASE [{escaped_for_identifier}]');
                END
                """
            )
        finally:
            conn.close()
    except pyodbc.Error as exc:
        message = str(exc).lower()
        # SQL Server error 262: CREATE DATABASE permission denied.
        if "(262)" in message or "create database permission denied" in message:
            return
        raise


def _init_sqlserver_schema():
    db_name = _get_sqlserver_database_name()
    if db_name in {"master", "model", "msdb", "tempdb"}:
        raise RuntimeError(
            "SQLSERVER_DATABASE is set to a SQL Server system database. "
            "Set SQLSERVER_DATABASE to an app database (for example, attendance_system) "
            "before running schema initialization."
        )

    statements = [
        """
        IF OBJECT_ID('admins', 'U') IS NULL
        CREATE TABLE admins (
            id INT IDENTITY(1,1) PRIMARY KEY,
            username NVARCHAR(80) NOT NULL UNIQUE,
            email NVARCHAR(120) NOT NULL UNIQUE,
            password_hash NVARCHAR(255) NOT NULL,
            must_change_password BIT NOT NULL DEFAULT 0,
            profile_photo NVARCHAR(MAX) NULL,
            full_name NVARCHAR(200) NOT NULL,
            role NVARCHAR(50) NOT NULL DEFAULT 'admin',
            is_active BIT NOT NULL DEFAULT 1,
            last_login DATETIME2 NULL,
            created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
        );
        """,
        "IF COL_LENGTH('admins', 'must_change_password') IS NULL ALTER TABLE admins ADD must_change_password BIT NOT NULL CONSTRAINT DF_admins_must_change_password DEFAULT 0;",
        "IF COL_LENGTH('admins', 'profile_photo') IS NULL ALTER TABLE admins ADD profile_photo NVARCHAR(MAX) NULL;",
        """
        IF OBJECT_ID('students', 'U') IS NULL
        CREATE TABLE students (
            id INT IDENTITY(1,1) PRIMARY KEY,
            student_id NVARCHAR(50) NOT NULL UNIQUE,
            first_name NVARCHAR(100) NOT NULL,
            last_name NVARCHAR(100) NOT NULL,
            email NVARCHAR(120) NOT NULL UNIQUE,
            password_hash NVARCHAR(255) NULL,
            must_change_password BIT NOT NULL DEFAULT 1,
            profile_photo NVARCHAR(MAX) NULL,
            admission_academic_year NVARCHAR(40) NULL,
            phone NVARCHAR(20) NULL,
            department NVARCHAR(100) NULL,
            course NVARCHAR(100) NULL,
            year_level NVARCHAR(20) NULL,
            face_encodings NVARCHAR(MAX) NOT NULL,
            registration_date DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
            is_active BIT NOT NULL DEFAULT 1,
            last_updated DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
        );
        """,
        "IF COL_LENGTH('students', 'password_hash') IS NULL ALTER TABLE students ADD password_hash NVARCHAR(255) NULL;",
        "IF COL_LENGTH('students', 'must_change_password') IS NULL ALTER TABLE students ADD must_change_password BIT NOT NULL CONSTRAINT DF_students_must_change_password DEFAULT 1;",
        "IF COL_LENGTH('students', 'profile_photo') IS NULL ALTER TABLE students ADD profile_photo NVARCHAR(MAX) NULL;",
        "IF COL_LENGTH('students', 'admission_academic_year') IS NULL ALTER TABLE students ADD admission_academic_year NVARCHAR(40) NULL;",
        """
        IF OBJECT_ID('exam_halls', 'U') IS NULL
        CREATE TABLE exam_halls (
            id INT IDENTITY(1,1) PRIMARY KEY,
            name NVARCHAR(120) NOT NULL UNIQUE,
            capacity INT NOT NULL,
            is_active BIT NOT NULL DEFAULT 1,
            created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
        );
        """,
        """
        IF OBJECT_ID('examination_sessions', 'U') IS NULL
        CREATE TABLE examination_sessions (
            id INT IDENTITY(1,1) PRIMARY KEY,
            session_name NVARCHAR(200) NOT NULL,
            course_code NVARCHAR(50) NULL,
            venue NVARCHAR(200) NULL,
            hall_id INT NULL,
            expected_students INT NULL,
            allow_file_upload BIT NOT NULL DEFAULT 0,
            start_time DATETIME2 NOT NULL,
            end_time DATETIME2 NOT NULL,
            created_by INT NOT NULL,
            created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
            is_active BIT NOT NULL DEFAULT 1,
            CONSTRAINT FK_examination_sessions_created_by FOREIGN KEY (created_by) REFERENCES admins(id),
            CONSTRAINT FK_examination_sessions_hall FOREIGN KEY (hall_id) REFERENCES exam_halls(id)
        );
        """,
        "IF COL_LENGTH('examination_sessions', 'hall_id') IS NULL ALTER TABLE examination_sessions ADD hall_id INT NULL;",
        "IF COL_LENGTH('examination_sessions', 'expected_students') IS NULL ALTER TABLE examination_sessions ADD expected_students INT NULL;",
        "IF COL_LENGTH('examination_sessions', 'allow_file_upload') IS NULL ALTER TABLE examination_sessions ADD allow_file_upload BIT NOT NULL CONSTRAINT DF_examination_sessions_allow_file_upload DEFAULT 0;",
        "IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = 'FK_examination_sessions_hall') ALTER TABLE examination_sessions ADD CONSTRAINT FK_examination_sessions_hall FOREIGN KEY (hall_id) REFERENCES exam_halls(id);",
        """
        IF OBJECT_ID('attendances', 'U') IS NULL
        CREATE TABLE attendances (
            id INT IDENTITY(1,1) PRIMARY KEY,
            student_id INT NOT NULL,
            session_id INT NOT NULL,
            timestamp DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
            verification_confidence FLOAT NULL,
            verification_method NVARCHAR(50) NOT NULL DEFAULT 'face_recognition',
            ip_address NVARCHAR(45) NULL,
            device_info NVARCHAR(200) NULL,
            CONSTRAINT UQ_attendances_student_session UNIQUE (student_id, session_id),
            CONSTRAINT FK_attendances_student FOREIGN KEY (student_id) REFERENCES students(id),
            CONSTRAINT FK_attendances_session FOREIGN KEY (session_id) REFERENCES examination_sessions(id)
        );
        """,
        """
        IF OBJECT_ID('exam_registrations', 'U') IS NULL
        CREATE TABLE exam_registrations (
            id INT IDENTITY(1,1) PRIMARY KEY,
            session_id INT NOT NULL,
            student_id INT NOT NULL,
            registered_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
            registered_by INT NULL,
            CONSTRAINT UQ_exam_registrations_session_student UNIQUE (session_id, student_id),
            CONSTRAINT FK_exam_registrations_session FOREIGN KEY (session_id) REFERENCES examination_sessions(id),
            CONSTRAINT FK_exam_registrations_student FOREIGN KEY (student_id) REFERENCES students(id),
            CONSTRAINT FK_exam_registrations_registered_by FOREIGN KEY (registered_by) REFERENCES admins(id)
        );
        """,
        """
        IF OBJECT_ID('student_course_registrations', 'U') IS NULL
        CREATE TABLE student_course_registrations (
            id INT IDENTITY(1,1) PRIMARY KEY,
            student_id INT NOT NULL,
            program_name NVARCHAR(150) NOT NULL,
            level_name NVARCHAR(30) NOT NULL,
            semester_no INT NULL,
            course_code NVARCHAR(50) NOT NULL,
            course_title NVARCHAR(200) NULL,
            registered_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
            CONSTRAINT UQ_student_course_registrations UNIQUE (student_id, course_code),
            CONSTRAINT FK_student_course_registrations_student FOREIGN KEY (student_id) REFERENCES students(id)
        );
        """,
        "IF COL_LENGTH('student_course_registrations', 'semester_no') IS NULL ALTER TABLE student_course_registrations ADD semester_no INT NULL;",
        """
        IF OBJECT_ID('session_invigilators', 'U') IS NULL
        CREATE TABLE session_invigilators (
            id INT IDENTITY(1,1) PRIMARY KEY,
            session_id INT NOT NULL,
            invigilator_id INT NOT NULL,
            assigned_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
            assigned_by INT NULL,
            is_active BIT NOT NULL DEFAULT 1,
            CONSTRAINT UQ_session_invigilators_session_invigilator UNIQUE (session_id, invigilator_id),
            CONSTRAINT FK_session_invigilators_session FOREIGN KEY (session_id) REFERENCES examination_sessions(id),
            CONSTRAINT FK_session_invigilators_invigilator FOREIGN KEY (invigilator_id) REFERENCES admins(id),
            CONSTRAINT FK_session_invigilators_assigned_by FOREIGN KEY (assigned_by) REFERENCES admins(id)
        );
        """,
        """
        IF OBJECT_ID('lecturer_courses', 'U') IS NULL
        CREATE TABLE lecturer_courses (
            id INT IDENTITY(1,1) PRIMARY KEY,
            lecturer_id INT NOT NULL,
            course_code NVARCHAR(50) NOT NULL,
            course_title NVARCHAR(200) NULL,
            is_active BIT NOT NULL DEFAULT 1,
            assigned_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
            CONSTRAINT UQ_lecturer_courses_lecturer_course UNIQUE (lecturer_id, course_code),
            CONSTRAINT FK_lecturer_courses_lecturer FOREIGN KEY (lecturer_id) REFERENCES admins(id)
        );
        """,
        """
        IF OBJECT_ID('class_attendances', 'U') IS NULL
        CREATE TABLE class_attendances (
            id INT IDENTITY(1,1) PRIMARY KEY,
            student_id INT NOT NULL,
            course_code NVARCHAR(50) NOT NULL,
            lecturer_id INT NOT NULL,
            attendance_date DATE NOT NULL DEFAULT CAST(GETDATE() AS DATE),
            timestamp DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
            verification_confidence FLOAT NULL,
            verification_method NVARCHAR(50) NOT NULL DEFAULT 'face_recognition',
            ip_address NVARCHAR(45) NULL,
            device_info NVARCHAR(200) NULL,
            CONSTRAINT UQ_class_attendances UNIQUE (student_id, course_code, attendance_date),
            CONSTRAINT FK_class_att_student FOREIGN KEY (student_id) REFERENCES students(id),
            CONSTRAINT FK_class_att_lecturer FOREIGN KEY (lecturer_id) REFERENCES admins(id)
        );
        """,
        """
        IF OBJECT_ID('program_level_courses', 'U') IS NULL
        CREATE TABLE program_level_courses (
            id INT IDENTITY(1,1) PRIMARY KEY,
            program_name NVARCHAR(150) NOT NULL,
            level_name NVARCHAR(30) NOT NULL,
            semester_no INT NULL,
            course_code NVARCHAR(50) NOT NULL,
            course_title NVARCHAR(200) NOT NULL,
            credit_units INT NULL,
            is_active BIT NOT NULL DEFAULT 1,
            created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
            CONSTRAINT UQ_program_level_courses UNIQUE (program_name, level_name, course_code)
        );
        """,
        "IF COL_LENGTH('program_level_courses', 'semester_no') IS NULL ALTER TABLE program_level_courses ADD semester_no INT NULL;",
        """
        IF OBJECT_ID('program_level_semesters', 'U') IS NULL
        CREATE TABLE program_level_semesters (
            id INT IDENTITY(1,1) PRIMARY KEY,
            program_name NVARCHAR(150) NOT NULL,
            level_name NVARCHAR(30) NOT NULL,
            semester_count INT NOT NULL,
            created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
            updated_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
            CONSTRAINT UQ_program_level_semesters UNIQUE (program_name, level_name)
        );
        """,
        """
        IF OBJECT_ID('program_level_semester_statuses', 'U') IS NULL
        CREATE TABLE program_level_semester_statuses (
            id INT IDENTITY(1,1) PRIMARY KEY,
            program_name NVARCHAR(150) NOT NULL,
            level_name NVARCHAR(30) NOT NULL,
            semester_no INT NOT NULL,
            is_ended BIT NOT NULL DEFAULT 0,
            ended_at DATETIME2 NULL,
            updated_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
            CONSTRAINT UQ_program_level_semester_statuses UNIQUE (program_name, level_name, semester_no)
        );
        """,
        """
        IF OBJECT_ID('academic_programs', 'U') IS NULL
        CREATE TABLE academic_programs (
            id INT IDENTITY(1,1) PRIMARY KEY,
            program_name NVARCHAR(150) NOT NULL UNIQUE,
            duration_years INT NOT NULL DEFAULT 4,
            semesters_per_year INT NOT NULL DEFAULT 2,
            is_active BIT NOT NULL DEFAULT 1,
            created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
        );
        """,
        """
        IF OBJECT_ID('academic_years', 'U') IS NULL
        CREATE TABLE academic_years (
            id INT IDENTITY(1,1) PRIMARY KEY,
            year_label NVARCHAR(40) NOT NULL UNIQUE,
            is_current BIT NOT NULL DEFAULT 0,
            enrollment_open BIT NOT NULL DEFAULT 1,
            is_active BIT NOT NULL DEFAULT 1,
            start_month INT NOT NULL DEFAULT 9,
            start_day INT NOT NULL DEFAULT 1,
            end_month INT NOT NULL DEFAULT 8,
            end_day INT NOT NULL DEFAULT 31,
            created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
        );
        """,
        """
        IF OBJECT_ID('academic_year_program_exceptions', 'U') IS NULL
        CREATE TABLE academic_year_program_exceptions (
            id INT IDENTITY(1,1) PRIMARY KEY,
            academic_year_id INT NOT NULL,
            program_name NVARCHAR(150) NOT NULL,
            created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
            CONSTRAINT UQ_academic_year_program_exception UNIQUE (academic_year_id, program_name),
            CONSTRAINT FK_academic_year_program_exception_year FOREIGN KEY (academic_year_id) REFERENCES academic_years(id)
        );
        """,
        "IF COL_LENGTH('academic_programs', 'duration_years') IS NULL ALTER TABLE academic_programs ADD duration_years INT NOT NULL CONSTRAINT DF_academic_programs_duration_years DEFAULT 4;",
        "IF COL_LENGTH('academic_programs', 'semesters_per_year') IS NULL ALTER TABLE academic_programs ADD semesters_per_year INT NOT NULL CONSTRAINT DF_academic_programs_semesters_per_year DEFAULT 2;",
        "IF COL_LENGTH('academic_years', 'enrollment_open') IS NULL ALTER TABLE academic_years ADD enrollment_open BIT NOT NULL CONSTRAINT DF_academic_years_enrollment_open DEFAULT 1;",
        "IF COL_LENGTH('academic_years', 'is_active') IS NULL ALTER TABLE academic_years ADD is_active BIT NOT NULL CONSTRAINT DF_academic_years_is_active DEFAULT 1;",
        "IF COL_LENGTH('academic_years', 'start_month') IS NULL ALTER TABLE academic_years ADD start_month INT NOT NULL CONSTRAINT DF_academic_years_start_month DEFAULT 9;",
        "IF COL_LENGTH('academic_years', 'start_day') IS NULL ALTER TABLE academic_years ADD start_day INT NOT NULL CONSTRAINT DF_academic_years_start_day DEFAULT 1;",
        "IF COL_LENGTH('academic_years', 'end_month') IS NULL ALTER TABLE academic_years ADD end_month INT NOT NULL CONSTRAINT DF_academic_years_end_month DEFAULT 8;",
        "IF COL_LENGTH('academic_years', 'end_day') IS NULL ALTER TABLE academic_years ADD end_day INT NOT NULL CONSTRAINT DF_academic_years_end_day DEFAULT 31;",
        """
        IF OBJECT_ID('exam_papers', 'U') IS NULL
        CREATE TABLE exam_papers (
            id INT IDENTITY(1,1) PRIMARY KEY,
            session_id INT NOT NULL,
            paper_code NVARCHAR(50) NULL,
            paper_title NVARCHAR(200) NOT NULL,
            created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
            CONSTRAINT FK_exam_papers_session FOREIGN KEY (session_id) REFERENCES examination_sessions(id)
        );
        """,
        """
        IF OBJECT_ID('exam_stations', 'U') IS NULL
        CREATE TABLE exam_stations (
            id INT IDENTITY(1,1) PRIMARY KEY,
            name NVARCHAR(120) NOT NULL UNIQUE,
            api_key_hash NVARCHAR(255) NOT NULL,
            ip_whitelist NVARCHAR(MAX) NULL,
            is_active BIT NOT NULL DEFAULT 1,
            created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
        );
        """,
        """
        IF OBJECT_ID('verification_logs', 'U') IS NULL
        CREATE TABLE verification_logs (
            id INT IDENTITY(1,1) PRIMARY KEY,
            session_id INT NULL,
            student_id INT NULL,
            claimed_student_id NVARCHAR(50) NULL,
            timestamp DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
            outcome NVARCHAR(30) NOT NULL,
            reason NVARCHAR(255) NULL,
            confidence FLOAT NULL,
            ip_address NVARCHAR(45) NULL,
            device_info NVARCHAR(200) NULL,
            station_id INT NULL,
            CONSTRAINT FK_verification_logs_session FOREIGN KEY (session_id) REFERENCES examination_sessions(id),
            CONSTRAINT FK_verification_logs_student FOREIGN KEY (student_id) REFERENCES students(id),
            CONSTRAINT FK_verification_logs_station FOREIGN KEY (station_id) REFERENCES exam_stations(id)
        );
        """,
        """
        IF OBJECT_ID('verification_challenges', 'U') IS NULL
        CREATE TABLE verification_challenges (
            id INT IDENTITY(1,1) PRIMARY KEY,
            station_id INT NOT NULL,
            session_id INT NOT NULL,
            challenge NVARCHAR(50) NOT NULL,
            nonce NVARCHAR(120) NOT NULL UNIQUE,
            expires_at DATETIME2 NOT NULL,
            used_at DATETIME2 NULL,
            created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
            CONSTRAINT FK_verification_challenges_station FOREIGN KEY (station_id) REFERENCES exam_stations(id),
            CONSTRAINT FK_verification_challenges_session FOREIGN KEY (session_id) REFERENCES examination_sessions(id)
        );
        """,
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_admins_username' AND object_id=OBJECT_ID('admins')) CREATE INDEX idx_admins_username ON admins (username);",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_admins_email' AND object_id=OBJECT_ID('admins')) CREATE INDEX idx_admins_email ON admins (email);",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_students_student_id' AND object_id=OBJECT_ID('students')) CREATE INDEX idx_students_student_id ON students (student_id);",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_students_email' AND object_id=OBJECT_ID('students')) CREATE INDEX idx_students_email ON students (email);",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_sessions_start_time' AND object_id=OBJECT_ID('examination_sessions')) CREATE INDEX idx_sessions_start_time ON examination_sessions (start_time);",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_exam_halls_name' AND object_id=OBJECT_ID('exam_halls')) CREATE INDEX idx_exam_halls_name ON exam_halls (name);",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_sessions_hall_id' AND object_id=OBJECT_ID('examination_sessions')) CREATE INDEX idx_sessions_hall_id ON examination_sessions (hall_id);",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_attendance_student_id' AND object_id=OBJECT_ID('attendances')) CREATE INDEX idx_attendance_student_id ON attendances (student_id);",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_attendance_session_id' AND object_id=OBJECT_ID('attendances')) CREATE INDEX idx_attendance_session_id ON attendances (session_id);",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_attendance_timestamp' AND object_id=OBJECT_ID('attendances')) CREATE INDEX idx_attendance_timestamp ON attendances ([timestamp]);",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_exam_reg_session_id' AND object_id=OBJECT_ID('exam_registrations')) CREATE INDEX idx_exam_reg_session_id ON exam_registrations (session_id);",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_exam_reg_student_id' AND object_id=OBJECT_ID('exam_registrations')) CREATE INDEX idx_exam_reg_student_id ON exam_registrations (student_id);",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_student_course_reg_student_id' AND object_id=OBJECT_ID('student_course_registrations')) CREATE INDEX idx_student_course_reg_student_id ON student_course_registrations (student_id);",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_student_course_reg_course_code' AND object_id=OBJECT_ID('student_course_registrations')) CREATE INDEX idx_student_course_reg_course_code ON student_course_registrations (course_code);",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_invigilators_session_id' AND object_id=OBJECT_ID('session_invigilators')) CREATE INDEX idx_invigilators_session_id ON session_invigilators (session_id);",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_invigilators_invigilator_id' AND object_id=OBJECT_ID('session_invigilators')) CREATE INDEX idx_invigilators_invigilator_id ON session_invigilators (invigilator_id);",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_lecturer_courses_lecturer_id' AND object_id=OBJECT_ID('lecturer_courses')) CREATE INDEX idx_lecturer_courses_lecturer_id ON lecturer_courses (lecturer_id);",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_lecturer_courses_course_code' AND object_id=OBJECT_ID('lecturer_courses')) CREATE INDEX idx_lecturer_courses_course_code ON lecturer_courses (course_code);",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_class_att_student_id' AND object_id=OBJECT_ID('class_attendances')) CREATE INDEX idx_class_att_student_id ON class_attendances (student_id);",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_class_att_course_code' AND object_id=OBJECT_ID('class_attendances')) CREATE INDEX idx_class_att_course_code ON class_attendances (course_code);",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_class_att_lecturer_id' AND object_id=OBJECT_ID('class_attendances')) CREATE INDEX idx_class_att_lecturer_id ON class_attendances (lecturer_id);",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_class_att_date' AND object_id=OBJECT_ID('class_attendances')) CREATE INDEX idx_class_att_date ON class_attendances (attendance_date);",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_plc_program_level' AND object_id=OBJECT_ID('program_level_courses')) CREATE INDEX idx_plc_program_level ON program_level_courses (program_name, level_name);",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_plc_program_level_semester' AND object_id=OBJECT_ID('program_level_courses')) CREATE INDEX idx_plc_program_level_semester ON program_level_courses (program_name, level_name, semester_no);",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_plc_course_code' AND object_id=OBJECT_ID('program_level_courses')) CREATE INDEX idx_plc_course_code ON program_level_courses (course_code);",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_pls_program_level' AND object_id=OBJECT_ID('program_level_semesters')) CREATE INDEX idx_pls_program_level ON program_level_semesters (program_name, level_name);",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_plss_program_level_semester' AND object_id=OBJECT_ID('program_level_semester_statuses')) CREATE INDEX idx_plss_program_level_semester ON program_level_semester_statuses (program_name, level_name, semester_no);",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_academic_programs_active' AND object_id=OBJECT_ID('academic_programs')) CREATE INDEX idx_academic_programs_active ON academic_programs (is_active);",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_exam_papers_session_id' AND object_id=OBJECT_ID('exam_papers')) CREATE INDEX idx_exam_papers_session_id ON exam_papers (session_id);",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_ver_logs_session_id' AND object_id=OBJECT_ID('verification_logs')) CREATE INDEX idx_ver_logs_session_id ON verification_logs (session_id);",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_ver_logs_student_id' AND object_id=OBJECT_ID('verification_logs')) CREATE INDEX idx_ver_logs_student_id ON verification_logs (student_id);",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_ver_logs_timestamp' AND object_id=OBJECT_ID('verification_logs')) CREATE INDEX idx_ver_logs_timestamp ON verification_logs ([timestamp]);",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_ver_logs_station_id' AND object_id=OBJECT_ID('verification_logs')) CREATE INDEX idx_ver_logs_station_id ON verification_logs (station_id);",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_ver_challenge_nonce' AND object_id=OBJECT_ID('verification_challenges')) CREATE INDEX idx_ver_challenge_nonce ON verification_challenges (nonce);",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_ver_challenge_session_id' AND object_id=OBJECT_ID('verification_challenges')) CREATE INDEX idx_ver_challenge_session_id ON verification_challenges (session_id);",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_ver_challenge_station_id' AND object_id=OBJECT_ID('verification_challenges')) CREATE INDEX idx_ver_challenge_station_id ON verification_challenges (station_id);",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_academic_years_current' AND object_id=OBJECT_ID('academic_years')) CREATE INDEX idx_academic_years_current ON academic_years (is_current);",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_ay_exception_year' AND object_id=OBJECT_ID('academic_year_program_exceptions')) CREATE INDEX idx_ay_exception_year ON academic_year_program_exceptions (academic_year_id);",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='idx_ay_exception_program' AND object_id=OBJECT_ID('academic_year_program_exceptions')) CREATE INDEX idx_ay_exception_program ON academic_year_program_exceptions (program_name);",
    ]
    with db_cursor() as cur:
        for stmt in statements:
            try:
                cur.execute(stmt)
            except Exception as exc:
                message = str(exc).lower()
                if "(262)" in message or "permission denied" in message:
                    raise RuntimeError(
                        "Insufficient permission to create SQL Server tables in SQLSERVER_DATABASE. "
                        "Grant CREATE TABLE (or db_owner) on that database, or use a login with proper rights."
                    ) from exc
                raise


def init_pool():
    global _POOL
    backend = get_database_backend()
    dsn = _get_dsn()
    if backend == "postgresql" and _POOL is None:
        _POOL = pool.SimpleConnectionPool(1, 10, dsn=dsn)
    if backend == "sqlserver":
        _ensure_sqlserver_database_exists()
        # Validate SQL Server connection during app startup.
        try:
            conn = pyodbc.connect(dsn, timeout=5)
            conn.close()
        except pyodbc.Error as exc:
            message = str(exc)
            if "(4060)" in message or "cannot open database" in message.lower():
                raise RuntimeError(
                    "Cannot open SQL Server database configured in SQLSERVER_DATABASE. "
                    "Either create it manually and grant your login access, or set "
                    "SQLSERVER_AUTO_CREATE_DB=true and use a login with CREATE DATABASE permission."
                ) from exc
            raise
    return _POOL


@contextmanager
def get_conn():
    backend = get_database_backend()
    if backend == "postgresql":
        p = init_pool()
        conn = p.getconn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            p.putconn(conn)
        return

    conn = pyodbc.connect(_get_dsn(), timeout=5)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def db_cursor():
    with get_conn() as conn:
        if get_database_backend() == "postgresql":
            with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
                yield cur
            return
        with conn.cursor() as cur:
            yield cur


def _adapt_sql(sql):
    if get_database_backend() == "sqlserver":
        adapted = sql.replace("%s", "?")
        if re.search(r"^\s*select\s", adapted, flags=re.IGNORECASE):
            m = re.search(r"\s+LIMIT\s+(\d+)\s*;?\s*$", adapted, flags=re.IGNORECASE)
            if m:
                limit_value = m.group(1)
                adapted = re.sub(r"\s+LIMIT\s+\d+\s*;?\s*$", "", adapted, flags=re.IGNORECASE)
                adapted = re.sub(
                    r"^\s*SELECT\s+",
                    f"SELECT TOP {limit_value} ",
                    adapted,
                    count=1,
                    flags=re.IGNORECASE,
                )
        return adapted
    return sql


def _row_to_dict(cursor, row):
    if not row:
        return None
    if isinstance(row, dict):
        return dict(row)
    cols = [c[0] for c in cursor.description]
    return dict(zip(cols, row))


def fetch_one(sql, params=None):
    with db_cursor() as cur:
        cur.execute(_adapt_sql(sql), params or ())
        row = cur.fetchone()
        return _row_to_dict(cur, row)


def fetch_all(sql, params=None):
    with db_cursor() as cur:
        cur.execute(_adapt_sql(sql), params or ())
        rows = cur.fetchall() or []
        if get_database_backend() == "postgresql":
            return [dict(r) for r in rows]
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in rows]


def execute(sql, params=None):
    with db_cursor() as cur:
        cur.execute(_adapt_sql(sql), params or ())
        return cur.rowcount


def execute_returning(sql, params=None):
    with db_cursor() as cur:
        cur.execute(_adapt_sql(sql), params or ())
        row = cur.fetchone()
        return _row_to_dict(cur, row)


def init_db_schema():
    if get_database_backend() == "sqlserver":
        _init_sqlserver_schema()
        return

    statements = [
        """
        CREATE TABLE IF NOT EXISTS admins (
            id SERIAL PRIMARY KEY,
            username VARCHAR(80) UNIQUE NOT NULL,
            email VARCHAR(120) UNIQUE NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            must_change_password BOOLEAN NOT NULL DEFAULT FALSE,
            profile_photo TEXT,
            full_name VARCHAR(200) NOT NULL,
            role VARCHAR(50) DEFAULT 'admin',
            is_active BOOLEAN DEFAULT TRUE,
            last_login TIMESTAMP NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """,
        "ALTER TABLE admins ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT FALSE;",
        "ALTER TABLE admins ADD COLUMN IF NOT EXISTS profile_photo TEXT;",
        "CREATE INDEX IF NOT EXISTS idx_admins_username ON admins (username);",
        "CREATE INDEX IF NOT EXISTS idx_admins_email ON admins (email);",
        """
        CREATE TABLE IF NOT EXISTS students (
            id SERIAL PRIMARY KEY,
            student_id VARCHAR(50) UNIQUE NOT NULL,
            first_name VARCHAR(100) NOT NULL,
            last_name VARCHAR(100) NOT NULL,
            email VARCHAR(120) UNIQUE NOT NULL,
            password_hash VARCHAR(255),
            must_change_password BOOLEAN NOT NULL DEFAULT TRUE,
            profile_photo TEXT,
            admission_academic_year VARCHAR(40),
            phone VARCHAR(20),
            department VARCHAR(100),
            course VARCHAR(100),
            year_level VARCHAR(20),
            face_encodings TEXT NOT NULL,
            registration_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_active BOOLEAN DEFAULT TRUE,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """,
        "ALTER TABLE students ADD COLUMN IF NOT EXISTS password_hash VARCHAR(255);",
        "ALTER TABLE students ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT TRUE;",
        "ALTER TABLE students ADD COLUMN IF NOT EXISTS profile_photo TEXT;",
        "ALTER TABLE students ADD COLUMN IF NOT EXISTS admission_academic_year VARCHAR(40);",
        "CREATE INDEX IF NOT EXISTS idx_students_student_id ON students (student_id);",
        "CREATE INDEX IF NOT EXISTS idx_students_email ON students (email);",
        """
        CREATE TABLE IF NOT EXISTS exam_halls (
            id SERIAL PRIMARY KEY,
            name VARCHAR(120) UNIQUE NOT NULL,
            capacity INTEGER NOT NULL,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """,
        "CREATE INDEX IF NOT EXISTS idx_exam_halls_name ON exam_halls (name);",
        """
        CREATE TABLE IF NOT EXISTS examination_sessions (
            id SERIAL PRIMARY KEY,
            session_name VARCHAR(200) NOT NULL,
            course_code VARCHAR(50),
            venue VARCHAR(200),
            hall_id INTEGER REFERENCES exam_halls(id),
            expected_students INTEGER,
            allow_file_upload BOOLEAN NOT NULL DEFAULT FALSE,
            start_time TIMESTAMP NOT NULL,
            end_time TIMESTAMP NOT NULL,
            created_by INTEGER NOT NULL REFERENCES admins(id),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_active BOOLEAN DEFAULT TRUE
        );
        """,
        "ALTER TABLE examination_sessions ADD COLUMN IF NOT EXISTS hall_id INTEGER REFERENCES exam_halls(id);",
        "ALTER TABLE examination_sessions ADD COLUMN IF NOT EXISTS expected_students INTEGER;",
        "ALTER TABLE examination_sessions ADD COLUMN IF NOT EXISTS allow_file_upload BOOLEAN NOT NULL DEFAULT FALSE;",
        "CREATE INDEX IF NOT EXISTS idx_sessions_hall_id ON examination_sessions (hall_id);",
        "CREATE INDEX IF NOT EXISTS idx_sessions_start_time ON examination_sessions (start_time);",
        """
        CREATE TABLE IF NOT EXISTS attendances (
            id SERIAL PRIMARY KEY,
            student_id INTEGER NOT NULL REFERENCES students(id),
            session_id INTEGER NOT NULL REFERENCES examination_sessions(id),
            timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            verification_confidence DOUBLE PRECISION,
            verification_method VARCHAR(50) DEFAULT 'face_recognition',
            ip_address VARCHAR(45),
            device_info VARCHAR(200),
            UNIQUE (student_id, session_id)
        );
        """,
        "CREATE INDEX IF NOT EXISTS idx_attendance_student_id ON attendances (student_id);",
        "CREATE INDEX IF NOT EXISTS idx_attendance_session_id ON attendances (session_id);",
        "CREATE INDEX IF NOT EXISTS idx_attendance_timestamp ON attendances (timestamp);",
        """
        CREATE TABLE IF NOT EXISTS exam_registrations (
            id SERIAL PRIMARY KEY,
            session_id INTEGER NOT NULL REFERENCES examination_sessions(id),
            student_id INTEGER NOT NULL REFERENCES students(id),
            registered_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            registered_by INTEGER REFERENCES admins(id),
            UNIQUE (session_id, student_id)
        );
        """,
        "CREATE INDEX IF NOT EXISTS idx_exam_reg_session_id ON exam_registrations (session_id);",
        "CREATE INDEX IF NOT EXISTS idx_exam_reg_student_id ON exam_registrations (student_id);",
        """
        CREATE TABLE IF NOT EXISTS student_course_registrations (
            id SERIAL PRIMARY KEY,
            student_id INTEGER NOT NULL REFERENCES students(id),
            program_name VARCHAR(150) NOT NULL,
            level_name VARCHAR(30) NOT NULL,
            semester_no INTEGER,
            course_code VARCHAR(50) NOT NULL,
            course_title VARCHAR(200),
            registered_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (student_id, course_code)
        );
        """,
        "ALTER TABLE student_course_registrations ADD COLUMN IF NOT EXISTS semester_no INTEGER;",
        "CREATE INDEX IF NOT EXISTS idx_student_course_reg_student_id ON student_course_registrations (student_id);",
        "CREATE INDEX IF NOT EXISTS idx_student_course_reg_course_code ON student_course_registrations (course_code);",
        """
        CREATE TABLE IF NOT EXISTS session_invigilators (
            id SERIAL PRIMARY KEY,
            session_id INTEGER NOT NULL REFERENCES examination_sessions(id),
            invigilator_id INTEGER NOT NULL REFERENCES admins(id),
            assigned_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            assigned_by INTEGER REFERENCES admins(id),
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            UNIQUE (session_id, invigilator_id)
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS lecturer_courses (
            id SERIAL PRIMARY KEY,
            lecturer_id INTEGER NOT NULL REFERENCES admins(id),
            course_code VARCHAR(50) NOT NULL,
            course_title VARCHAR(200),
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            assigned_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (lecturer_id, course_code)
        );
        """,
        "CREATE INDEX IF NOT EXISTS idx_lecturer_courses_lecturer_id ON lecturer_courses (lecturer_id);",
        "CREATE INDEX IF NOT EXISTS idx_lecturer_courses_course_code ON lecturer_courses (course_code);",
        """
        CREATE TABLE IF NOT EXISTS class_attendances (
            id SERIAL PRIMARY KEY,
            student_id INTEGER NOT NULL REFERENCES students(id),
            course_code VARCHAR(50) NOT NULL,
            lecturer_id INTEGER NOT NULL REFERENCES admins(id),
            attendance_date DATE NOT NULL DEFAULT CURRENT_DATE,
            timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            verification_confidence DOUBLE PRECISION,
            verification_method VARCHAR(50) DEFAULT 'face_recognition',
            ip_address VARCHAR(45),
            device_info VARCHAR(200),
            UNIQUE (student_id, course_code, attendance_date)
        );
        """,
        "CREATE INDEX IF NOT EXISTS idx_class_att_student_id ON class_attendances (student_id);",
        "CREATE INDEX IF NOT EXISTS idx_class_att_course_code ON class_attendances (course_code);",
        "CREATE INDEX IF NOT EXISTS idx_class_att_lecturer_id ON class_attendances (lecturer_id);",
        "CREATE INDEX IF NOT EXISTS idx_class_att_date ON class_attendances (attendance_date);",
        """
        CREATE TABLE IF NOT EXISTS program_level_courses (
            id SERIAL PRIMARY KEY,
            program_name VARCHAR(150) NOT NULL,
            level_name VARCHAR(30) NOT NULL,
            semester_no INTEGER,
            course_code VARCHAR(50) NOT NULL,
            course_title VARCHAR(200) NOT NULL,
            credit_units INTEGER,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (program_name, level_name, course_code)
        );
        """,
        "ALTER TABLE program_level_courses ADD COLUMN IF NOT EXISTS semester_no INTEGER;",
        "CREATE INDEX IF NOT EXISTS idx_plc_program_level ON program_level_courses (program_name, level_name);",
        "CREATE INDEX IF NOT EXISTS idx_plc_program_level_semester ON program_level_courses (program_name, level_name, semester_no);",
        "CREATE INDEX IF NOT EXISTS idx_plc_course_code ON program_level_courses (course_code);",
        """
        CREATE TABLE IF NOT EXISTS program_level_semesters (
            id SERIAL PRIMARY KEY,
            program_name VARCHAR(150) NOT NULL,
            level_name VARCHAR(30) NOT NULL,
            semester_count INTEGER NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (program_name, level_name)
        );
        """,
        "CREATE INDEX IF NOT EXISTS idx_pls_program_level ON program_level_semesters (program_name, level_name);",
        """
        CREATE TABLE IF NOT EXISTS program_level_semester_statuses (
            id SERIAL PRIMARY KEY,
            program_name VARCHAR(150) NOT NULL,
            level_name VARCHAR(30) NOT NULL,
            semester_no INTEGER NOT NULL,
            is_ended BOOLEAN NOT NULL DEFAULT FALSE,
            ended_at TIMESTAMP NULL,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (program_name, level_name, semester_no)
        );
        """,
        "CREATE INDEX IF NOT EXISTS idx_plss_program_level_semester ON program_level_semester_statuses (program_name, level_name, semester_no);",
        """
        CREATE TABLE IF NOT EXISTS academic_programs (
            id SERIAL PRIMARY KEY,
            program_name VARCHAR(150) NOT NULL UNIQUE,
            duration_years INTEGER NOT NULL DEFAULT 4,
            semesters_per_year INTEGER NOT NULL DEFAULT 2,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """,
        "ALTER TABLE academic_programs ADD COLUMN IF NOT EXISTS duration_years INTEGER NOT NULL DEFAULT 4;",
        "ALTER TABLE academic_programs ADD COLUMN IF NOT EXISTS semesters_per_year INTEGER NOT NULL DEFAULT 2;",
        "CREATE INDEX IF NOT EXISTS idx_academic_programs_active ON academic_programs (is_active);",
        """
        CREATE TABLE IF NOT EXISTS academic_years (
            id SERIAL PRIMARY KEY,
            year_label VARCHAR(40) NOT NULL UNIQUE,
            is_current BOOLEAN NOT NULL DEFAULT FALSE,
            enrollment_open BOOLEAN NOT NULL DEFAULT TRUE,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            start_month INTEGER NOT NULL DEFAULT 9,
            start_day INTEGER NOT NULL DEFAULT 1,
            end_month INTEGER NOT NULL DEFAULT 8,
            end_day INTEGER NOT NULL DEFAULT 31,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """,
        "ALTER TABLE academic_years ADD COLUMN IF NOT EXISTS enrollment_open BOOLEAN NOT NULL DEFAULT TRUE;",
        "ALTER TABLE academic_years ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;",
        "ALTER TABLE academic_years ADD COLUMN IF NOT EXISTS start_month INTEGER NOT NULL DEFAULT 9;",
        "ALTER TABLE academic_years ADD COLUMN IF NOT EXISTS start_day INTEGER NOT NULL DEFAULT 1;",
        "ALTER TABLE academic_years ADD COLUMN IF NOT EXISTS end_month INTEGER NOT NULL DEFAULT 8;",
        "ALTER TABLE academic_years ADD COLUMN IF NOT EXISTS end_day INTEGER NOT NULL DEFAULT 31;",
        "CREATE INDEX IF NOT EXISTS idx_academic_years_current ON academic_years (is_current);",
        """
        CREATE TABLE IF NOT EXISTS academic_year_program_exceptions (
            id SERIAL PRIMARY KEY,
            academic_year_id INTEGER NOT NULL REFERENCES academic_years(id),
            program_name VARCHAR(150) NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (academic_year_id, program_name)
        );
        """,
        "CREATE INDEX IF NOT EXISTS idx_ay_exception_year ON academic_year_program_exceptions (academic_year_id);",
        "CREATE INDEX IF NOT EXISTS idx_ay_exception_program ON academic_year_program_exceptions (program_name);",
        """
        ALTER TABLE session_invigilators
        ALTER COLUMN assigned_at SET DEFAULT CURRENT_TIMESTAMP;
        """,
        "CREATE INDEX IF NOT EXISTS idx_invigilators_session_id ON session_invigilators (session_id);",
        "CREATE INDEX IF NOT EXISTS idx_invigilators_invigilator_id ON session_invigilators (invigilator_id);",
        """
        CREATE TABLE IF NOT EXISTS exam_papers (
            id SERIAL PRIMARY KEY,
            session_id INTEGER NOT NULL REFERENCES examination_sessions(id),
            paper_code VARCHAR(50),
            paper_title VARCHAR(200) NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """,
        "CREATE INDEX IF NOT EXISTS idx_exam_papers_session_id ON exam_papers (session_id);",
        """
        CREATE TABLE IF NOT EXISTS exam_stations (
            id SERIAL PRIMARY KEY,
            name VARCHAR(120) UNIQUE NOT NULL,
            api_key_hash VARCHAR(255) NOT NULL,
            ip_whitelist TEXT,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS verification_logs (
            id SERIAL PRIMARY KEY,
            session_id INTEGER REFERENCES examination_sessions(id),
            student_id INTEGER REFERENCES students(id),
            claimed_student_id VARCHAR(50),
            timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            outcome VARCHAR(30) NOT NULL,
            reason VARCHAR(255),
            confidence DOUBLE PRECISION,
            ip_address VARCHAR(45),
            device_info VARCHAR(200),
            station_id INTEGER REFERENCES exam_stations(id)
        );
        """,
        "CREATE INDEX IF NOT EXISTS idx_ver_logs_session_id ON verification_logs (session_id);",
        "CREATE INDEX IF NOT EXISTS idx_ver_logs_student_id ON verification_logs (student_id);",
        "CREATE INDEX IF NOT EXISTS idx_ver_logs_timestamp ON verification_logs (timestamp);",
        "CREATE INDEX IF NOT EXISTS idx_ver_logs_station_id ON verification_logs (station_id);",
        """
        CREATE TABLE IF NOT EXISTS verification_challenges (
            id SERIAL PRIMARY KEY,
            station_id INTEGER NOT NULL REFERENCES exam_stations(id),
            session_id INTEGER NOT NULL REFERENCES examination_sessions(id),
            challenge VARCHAR(50) NOT NULL,
            nonce VARCHAR(120) UNIQUE NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            used_at TIMESTAMP NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """,
        "CREATE INDEX IF NOT EXISTS idx_ver_challenge_nonce ON verification_challenges (nonce);",
        "CREATE INDEX IF NOT EXISTS idx_ver_challenge_session_id ON verification_challenges (session_id);",
        "CREATE INDEX IF NOT EXISTS idx_ver_challenge_station_id ON verification_challenges (station_id);",
    ]

    with db_cursor() as cur:
        # Some managed Postgres setups enforce low statement_timeout values.
        # Disable it for schema bootstrap so CREATE INDEX/TABLE can finish.
        try:
            cur.execute("SET LOCAL statement_timeout = 0;")
        except Exception:
            pass

        for stmt in statements:
            try:
                cur.execute(stmt)
            except psycopg2.errors.QueryCanceled as exc:
                raise RuntimeError(
                    "Database schema initialization timed out while executing: "
                    f"{stmt.strip().splitlines()[0][:120]}"
                ) from exc
