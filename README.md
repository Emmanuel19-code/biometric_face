# Facial Recognition Attendance System - Backend

A robust Python backend system for facial recognition-based examination attendance tracking.

## Features

- **User Registration Module**: Secure enrollment with multi-angle facial image capture
- **Authentication Module**: Real-time identity verification during examinations
- **Attendance Tracking Module**: Automated attendance recording with timestamps
- **Administrator Dashboard**: Comprehensive API for managing students, sessions, and reports

## Installation

1. Create a virtual environment:
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Configure environment:
```bash
cp .env.example .env
# Edit .env with your configuration
```

4. Initialize database:
```bash
python -c "from app import create_app, db; app = create_app(); app.app_context().push(); db.create_all()"
```

Optional: verify database connectivity
```bash
curl http://127.0.0.1:5000/health/db
```

## API Endpoints

### Authentication
- `POST /api/auth/login` - Admin login
- `POST /api/auth/refresh` - Refresh JWT token

### Student Registration
- `POST /api/students/register` - Register new student with facial data
- `GET /api/students/<student_id>` - Get student details
- `GET /api/students` - List all students (admin only)

### Attendance
- `POST /api/attendance/verify` - Verify identity and record attendance
- `GET /api/attendance/session/<session_id>` - Get attendance for a session
- `GET /api/attendance/student/<student_id>` - Get student attendance history

### Admin Dashboard
- `GET /api/admin/stats` - System statistics
- `POST /api/admin/sessions` - Create examination session
- `GET /api/admin/sessions` - List all sessions
- `GET /api/admin/reports/attendance` - Generate attendance reports

## Security Features

- JWT-based authentication
- Encrypted biometric data storage
- Role-based access control
- Secure password hashing
- Input validation and sanitization

## Project Structure

```
.
├── app.py                 # Application entry point
├── config.py             # Configuration management
├── models/               # Database models
├── api/                  # API routes
├── services/             # Business logic
├── utils/                # Utilities (face recognition, encryption)
└── tests/                # Test suite
```
# biometric_face
