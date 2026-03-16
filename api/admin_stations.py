from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required
from werkzeug.security import generate_password_hash
from utils import db as db_utils
import secrets

stations_bp = Blueprint("stations", __name__, url_prefix="/api/admin/stations")


def _fmt_dt(value):
    return value.isoformat() if value else None


def _station_to_dict(row):
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "ip_whitelist": row.get("ip_whitelist"),
        "is_active": row.get("is_active"),
        "created_at": _fmt_dt(row.get("created_at"))
    }


@stations_bp.post("")
@jwt_required()
def create_station():
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    ip_whitelist = (data.get("ip_whitelist") or "").strip() or None

    if not name:
        return jsonify({"error": "name is required"}), 400

    existing = db_utils.fetch_one("SELECT id FROM exam_stations WHERE name = %s", (name,))
    if existing:
        return jsonify({"error": "Station name already exists"}), 400

    raw_key = secrets.token_urlsafe(32)  # show once
    st = db_utils.execute_returning(
        """
        INSERT INTO exam_stations (name, api_key_hash, ip_whitelist, is_active)
        VALUES (%s, %s, %s, TRUE)
        RETURNING *
        """,
        (name, generate_password_hash(raw_key), ip_whitelist)
    )

    return jsonify({
        "message": "Station created",
        "station": _station_to_dict(st),
        "api_key": raw_key
    }), 201


@stations_bp.get("")
@jwt_required()
def list_stations():
    stations = db_utils.fetch_all("SELECT * FROM exam_stations ORDER BY created_at DESC")
    return jsonify({"stations": [_station_to_dict(s) for s in stations]}), 200


@stations_bp.post("/<int:station_id>/deactivate")
@jwt_required()
def deactivate_station(station_id):
    st = db_utils.fetch_one("SELECT * FROM exam_stations WHERE id = %s", (station_id,))
    if not st:
        return jsonify({"error": "Station not found"}), 404
    db_utils.execute("UPDATE exam_stations SET is_active = FALSE WHERE id = %s", (station_id,))
    updated = db_utils.fetch_one("SELECT * FROM exam_stations WHERE id = %s", (station_id,))
    return jsonify({"message": "Station deactivated", "station": _station_to_dict(updated)}), 200