from flask import Blueprint, request, jsonify
from datetime import datetime, timedelta
import secrets
import random

from utils.station_auth import verify_station
from utils import db as db_utils

challenge_bp = Blueprint("challenge", __name__, url_prefix="/api/attendance")


@challenge_bp.get("/challenge")
def get_challenge():
    """
    Station requests a liveness challenge.
    Header: X-Station-Key
    Query: session_id=<int>
    """
    raw_key = (request.headers.get("X-Station-Key") or "").strip()
    ok, station_or_err = verify_station(raw_key, request.remote_addr)
    if not ok:
        return jsonify({"error": station_or_err}), 401
    station = station_or_err

    session_id = request.args.get("session_id", type=int)
    if not session_id:
        return jsonify({"error": "session_id is required"}), 400

    session = db_utils.fetch_one("SELECT id FROM examination_sessions WHERE id = %s", (session_id,))
    if not session:
        return jsonify({"error": "Session not found"}), 404

    challenge = random.choice(["blink", "turn_left", "turn_right"])
    nonce = secrets.token_urlsafe(24)
    expires_at = datetime.utcnow() + timedelta(seconds=30)

    db_utils.execute(
        """
        INSERT INTO verification_challenges (station_id, session_id, challenge, nonce, expires_at)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (station["id"], session_id, challenge, nonce, expires_at)
    )

    return jsonify({
        "challenge": challenge,
        "nonce": nonce,
        "expires_at": expires_at.isoformat() + "Z"
    }), 200