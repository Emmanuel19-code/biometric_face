"""
End-to-end verification flow using HTTP API (raw Postgres backend).

Usage (PowerShell):
  .\venv\Scripts\python.exe scripts\verify_flow.py ^
    --base-url http://127.0.0.1:5000 ^
    --admin-user admin --admin-pass admin123 ^
    --student-id STU001 --student-email john@example.com ^
    --student-first John --student-last Doe ^
    --face-images C:\path\face1.jpg C:\path\face2.jpg C:\path\face3.jpg ^
    --live-image C:\path\live.jpg
"""
import argparse
import base64
import json
import sys
import urllib.request


def b64_file(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def http_json(method, url, payload=None, headers=None):
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    with urllib.request.urlopen(req) as resp:
        body = resp.read().decode("utf-8")
        return resp.status, json.loads(body) if body else {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:5000")
    ap.add_argument("--admin-user", required=True)
    ap.add_argument("--admin-pass", required=True)
    ap.add_argument("--student-id", required=True)
    ap.add_argument("--student-email", required=True)
    ap.add_argument("--student-first", required=True)
    ap.add_argument("--student-last", required=True)
    ap.add_argument("--face-images", nargs="+", required=True)
    ap.add_argument("--live-image", required=True)
    ap.add_argument("--session-name", default="Live Verification Session")
    ap.add_argument("--course-code", default="CS101")
    ap.add_argument("--venue", default="Hall A")
    args = ap.parse_args()

    base = args.base_url.rstrip("/")

    # Login
    status, login = http_json(
        "POST",
        f"{base}/api/auth/login",
        {"username": args.admin_user, "password": args.admin_pass}
    )
    if status != 200:
        print("Login failed:", login)
        return 1
    token = login["access_token"]
    auth = {"Authorization": f"Bearer {token}"}

    # Create session
    from datetime import datetime, timedelta
    start = datetime.utcnow().isoformat() + "Z"
    end = (datetime.utcnow() + timedelta(hours=2)).isoformat() + "Z"
    status, session = http_json(
        "POST",
        f"{base}/api/admin/sessions",
        {
            "session_name": args.session_name,
            "course_code": args.course_code,
            "venue": args.venue,
            "start_time": start,
            "end_time": end
        },
        headers=auth
    )
    if status not in (200, 201):
        print("Session create failed:", session)
        return 1
    session_id = session["session"]["id"]

    # Register student (with face images)
    face_images = [b64_file(p) for p in args.face_images]
    status, reg = http_json(
        "POST",
        f"{base}/api/students/register",
        {
            "student_id": args.student_id,
            "first_name": args.student_first,
            "last_name": args.student_last,
            "email": args.student_email,
            "face_images": face_images
        },
        headers=auth
    )
    if status not in (200, 201):
        print("Student registration failed:", reg)
        return 1

    # Register student to session
    status, reg2 = http_json(
        "POST",
        f"{base}/api/admin/sessions/{session_id}/registrations",
        {"student_ids": [args.student_id]},
        headers=auth
    )
    if status != 200:
        print("Session registration failed:", reg2)
        return 1

    # Create station key
    status, station = http_json(
        "POST",
        f"{base}/api/admin/stations",
        {"name": f"local-station-{session_id}"},
        headers=auth
    )
    if status not in (200, 201):
        print("Station create failed:", station)
        return 1
    station_key = station["api_key"]

    # Issue challenge
    status, ch = http_json(
        "GET",
        f"{base}/api/attendance/challenge?session_id={session_id}",
        headers={"X-Station-Key": station_key}
    )
    if status != 200:
        print("Challenge failed:", ch)
        return 1

    # Verify attendance
    live_image_b64 = b64_file(args.live_image)
    status, verify = http_json(
        "POST",
        f"{base}/api/attendance/verify",
        {
            "live_image": live_image_b64,
            "session_id": session_id,
            "nonce": ch["nonce"],
            "challenge": ch["challenge"],
            "student_id": args.student_id,
            "invigilator_id": login["admin"]["id"]
        },
        headers={"X-Station-Key": station_key}
    )
    print("Verify status:", status)
    print(json.dumps(verify, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
