# API Usage Guide

## Base URL
```
http://localhost:5000
```

## Authentication

### 1. Admin Login
```bash
POST /api/auth/login
Content-Type: application/json

{
  "username": "admin",
  "password": "admin123"
}

Response:
{
  "access_token": "eyJ0eXAiOiJKV1QiLCJhbGc...",
  "refresh_token": "eyJ0eXAiOiJKV1QiLCJhbGc...",
  "admin": {
    "id": 1,
    "username": "admin",
    "email": "admin@example.com",
    "full_name": "System Administrator",
    "role": "super_admin"
  }
}
```

### 2. Use Access Token
Include the token in the Authorization header:
```
Authorization: Bearer <access_token>
```

## Student Registration

### Register New Student
```bash
POST /api/students/register
Authorization: Bearer <token>
Content-Type: application/json

{
  "student_id": "STU001",
  "first_name": "John",
  "last_name": "Doe",
  "email": "john.doe@example.com",
  "phone": "+1234567890",
  "department": "Computer Science",
  "course": "BSc Computer Science",
  "year_level": "Year 3",
  "face_images": [
    "<base64_encoded_image_1>",
    "<base64_encoded_image_2>",
    "<base64_encoded_image_3>"
  ]
}
```

**Note:** `face_images` should be base64-encoded images from multiple angles (front, left, right).

### Get Student Details
```bash
GET /api/students/STU001
Authorization: Bearer <token>
```

### List All Students
```bash
GET /api/students?active_only=true
Authorization: Bearer <token>
```

## Attendance Verification

### 1) Create Liveness Challenge (station-authenticated)
```bash
GET /api/attendance/challenge?session_id=1
X-Station-Key: <station_api_key>
```

Response:
```json
{
  "challenge": "blink",
  "nonce": "one_time_nonce_here",
  "expires_at": "2026-03-09T18:00:00Z"
}
```

### 2) Verify Face and Record Attendance
```bash
POST /api/attendance/verify
X-Station-Key: <station_api_key>
Content-Type: application/json

{
  "live_image": "<base64_encoded_live_image>",
  "session_id": 1,
  "nonce": "<nonce_from_challenge>",
  "challenge": "blink",
  "student_id": "STU001",  # Optional 1:1 verification
  "invigilator_id": 2,  # Required if no authenticated web session cookie
  "frames": ["<base64_frame_1>", "<base64_frame_2>"]  # Optional for liveness
}

Response (Success):
{
  "message": "Attendance recorded successfully",
  "attendance": {
    "id": 1,
    "student_id": 1,
    "session_id": 1,
    "timestamp": "2026-02-13T10:30:00",
    "verification_confidence": 0.95,
    "verification_method": "face_recognition"
  },
  "confidence": 0.95
}

Response (Failure):
{
  "error": "Student is not registered for this examination session",
  "confidence": 0.45
}
```

Notes:
- Attendance is written immediately on successful verification.
- Students must be registered for the session before they can be marked present.
- Invigilator must be assigned to the session before verification is allowed.

### Get Session Attendance
```bash
GET /api/attendance/session/1
Authorization: Bearer <token>
```

### Get Student Attendance History
```bash
GET /api/attendance/student/STU001
Authorization: Bearer <token>
```

## Administrator Dashboard

### Get System Statistics
```bash
GET /api/admin/stats
Authorization: Bearer <token>

Response:
{
  "total_students": 150,
  "active_students": 145,
  "total_sessions": 25,
  "active_sessions": 3,
  "total_attendances": 1200,
  "today_attendances": 45,
  "this_week_attendances": 320,
  "this_month_attendances": 1200
}
```

### Create Examination Session
```bash
POST /api/admin/sessions
Authorization: Bearer <token>
Content-Type: application/json

{
  "session_name": "Final Examination - Computer Science 101",
  "course_code": "CS101",
  "venue": "Hall A",
  "start_time": "2026-02-15T09:00:00Z",
  "end_time": "2026-02-15T12:00:00Z",
  "papers": [
    {"paper_code": "CS101-A", "paper_title": "Data Structures"},
    {"paper_code": "CS101-B", "paper_title": "Computer Architecture"}
  ],
  "invigilator_ids": [2, 3]
}
```

### List All Sessions
```bash
GET /api/admin/sessions?active_only=false
Authorization: Bearer <token>
```

### End Session Immediately
```bash
POST /api/admin/sessions/1/end
Authorization: Bearer <token>
```

Once ended, face verification for that session is blocked.

### Register Students for a Session
```bash
POST /api/admin/sessions/1/registrations
Authorization: Bearer <token>
Content-Type: application/json

{
  "student_ids": ["STU001", "STU002"]
}
```

### Create Invigilator Account
```bash
POST /api/admin/invigilators
Authorization: Bearer <token>
Content-Type: application/json

{
  "username": "inv_jane",
  "email": "inv.jane@upsa.edu.gh",
  "full_name": "Jane Mensah",
  "password": "StrongPass123!"
}
```

### List Invigilators
```bash
GET /api/admin/invigilators
Authorization: Bearer <token>
```

### Assign Invigilators to Session
```bash
POST /api/admin/sessions/1/invigilators
Authorization: Bearer <token>
Content-Type: application/json

{
  "invigilator_ids": [2, 3]
}
```

### Set/Replace Session Papers
```bash
POST /api/admin/sessions/1/papers
Authorization: Bearer <token>
Content-Type: application/json

{
  "papers": [
    {"paper_code": "CS101-A", "paper_title": "Data Structures"},
    {"paper_code": "CS101-B", "paper_title": "Computer Architecture"}
  ]
}
```

### Get Session Registration List
```bash
GET /api/admin/sessions/1/registrations
Authorization: Bearer <token>
```

### Remove Student Registration from Session
```bash
DELETE /api/admin/sessions/1/registrations/STU001
Authorization: Bearer <token>
```

### Generate Attendance Report
```bash
GET /api/admin/reports/attendance?session_id=1&start_date=2026-02-01T00:00:00Z&end_date=2026-02-28T23:59:59Z
Authorization: Bearer <token>
```

## Python Example

```python
import requests
import base64
from PIL import Image
import io

BASE_URL = "http://localhost:5000"

# Login
response = requests.post(f"{BASE_URL}/api/auth/login", json={
    "username": "admin",
    "password": "admin123"
})
tokens = response.json()
access_token = tokens["access_token"]

headers = {"Authorization": f"Bearer {access_token}"}

# Register student
def encode_image(image_path):
    with open(image_path, 'rb') as f:
        return base64.b64encode(f.read()).decode('utf-8')

face_images = [
    encode_image("face_front.jpg"),
    encode_image("face_left.jpg"),
    encode_image("face_right.jpg")
]

response = requests.post(
    f"{BASE_URL}/api/students/register",
    json={
        "student_id": "STU001",
        "first_name": "John",
        "last_name": "Doe",
        "email": "john@example.com",
        "face_images": face_images
    },
    headers=headers
)
print(response.json())

# Verify attendance
live_image = encode_image("live_capture.jpg")
response = requests.post(
    f"{BASE_URL}/api/attendance/verify",
    json={
        "live_image": live_image,
        "session_id": 1
    }
)
print(response.json())
```

## Error Responses

All endpoints return standard error responses:

```json
{
  "error": "Error message here"
}
```

Common HTTP status codes:
- `200`: Success
- `201`: Created
- `400`: Bad Request
- `401`: Unauthorized
- `404`: Not Found
- `500`: Internal Server Error
