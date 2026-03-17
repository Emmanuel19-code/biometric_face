from datetime import datetime, timedelta
from typing import Optional, Tuple

from utils import db as db_utils


PAUSE_TYPES = {"verification", "time", "both"}


def _matches_pause_type(active_type: str, requested_type: str) -> bool:
    active = str(active_type or "").strip().lower()
    requested = str(requested_type or "").strip().lower()
    if active == "both" or requested == "both":
        return True
    return active == requested


def _normalize_pause_type(pause_type: str) -> str:
    value = str(pause_type or "").strip().lower()
    if value not in PAUSE_TYPES:
        raise ValueError("pause_type must be one of: verification, time, both")
    return value


def _active_rows_for_scope(session_id: int, hall_id: Optional[int]):
    if hall_id is None:
        return db_utils.fetch_all(
            """
            SELECT id, session_id, hall_id, pause_type, reason, started_at, started_by
            FROM verification_pause_controls
            WHERE is_active = TRUE
              AND session_id = %s
              AND hall_id IS NULL
            ORDER BY started_at DESC
            """,
            (int(session_id),),
        )
    return db_utils.fetch_all(
        """
        SELECT id, session_id, hall_id, pause_type, reason, started_at, started_by
        FROM verification_pause_controls
        WHERE is_active = TRUE
          AND session_id = %s
          AND (hall_id = %s OR hall_id IS NULL)
        ORDER BY
            CASE WHEN hall_id IS NULL THEN 0 ELSE 1 END DESC,
            started_at DESC
        """,
        (int(session_id), int(hall_id)),
    )


def get_active_pause(session_id: int, hall_id: Optional[int], pause_type: str = "verification"):
    normalized_type = _normalize_pause_type(pause_type)
    rows = _active_rows_for_scope(int(session_id), hall_id)
    for row in rows:
        if _matches_pause_type(row.get("pause_type"), normalized_type):
            return row
    return None


def start_pause(
    session_id: int,
    hall_id: Optional[int],
    pause_type: str,
    reason: Optional[str],
    started_by: int,
):
    normalized_type = _normalize_pause_type(pause_type)
    active = get_active_pause(int(session_id), hall_id, normalized_type)
    if active:
        return False, active

    now = datetime.utcnow()
    db_utils.execute(
        """
        INSERT INTO verification_pause_controls
            (session_id, hall_id, pause_type, reason, started_at, started_by, is_active)
        VALUES (%s, %s, %s, %s, %s, %s, TRUE)
        """,
        (
            int(session_id),
            int(hall_id) if hall_id is not None else None,
            normalized_type,
            str(reason or "").strip() or None,
            now,
            int(started_by),
        ),
    )
    created = get_active_pause(int(session_id), hall_id, normalized_type)
    return True, created


def resume_pause(
    session_id: int,
    hall_id: Optional[int],
    pause_type: str,
    resumed_by: int,
) -> Tuple[bool, Optional[dict], int, int]:
    normalized_type = _normalize_pause_type(pause_type)
    active = get_active_pause(int(session_id), hall_id, normalized_type)
    if not active:
        return False, None, 0, 0

    now = datetime.utcnow()
    started_at = active.get("started_at")
    pause_seconds = 0
    if started_at:
        pause_seconds = max(0, int((now - started_at).total_seconds()))

    db_utils.execute(
        """
        UPDATE verification_pause_controls
        SET is_active = FALSE,
            resumed_at = %s,
            resumed_by = %s,
            pause_seconds = %s
        WHERE id = %s
        """,
        (now, int(resumed_by), pause_seconds, int(active["id"])),
    )

    extended_seconds = 0
    active_pause_type = str(active.get("pause_type") or "").strip().lower()
    if pause_seconds > 0 and active_pause_type in {"time", "both"}:
        session_row = db_utils.fetch_one(
            "SELECT id, end_time FROM examination_sessions WHERE id = %s",
            (int(session_id),),
        )
        if session_row and session_row.get("end_time"):
            new_end_time = session_row["end_time"] + timedelta(seconds=pause_seconds)
            db_utils.execute(
                "UPDATE examination_sessions SET end_time = %s WHERE id = %s",
                (new_end_time, int(session_id)),
            )
            extended_seconds = pause_seconds

    updated = db_utils.fetch_one(
        """
        SELECT id, session_id, hall_id, pause_type, reason, started_at, started_by, resumed_at, resumed_by, pause_seconds, is_active
        FROM verification_pause_controls
        WHERE id = %s
        LIMIT 1
        """,
        (int(active["id"]),),
    )
    return True, updated, pause_seconds, extended_seconds


def get_pause_state(session_id: int, hall_id: Optional[int]):
    verification = get_active_pause(int(session_id), hall_id, "verification")
    timing = get_active_pause(int(session_id), hall_id, "time")
    return {
        "verification_paused": bool(verification),
        "time_paused": bool(timing),
        "verification_pause": verification,
        "time_pause": timing,
    }
