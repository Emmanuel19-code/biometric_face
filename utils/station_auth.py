from werkzeug.security import check_password_hash
from utils import db as db_utils

def verify_station(raw_key: str, ip: str):
    if not raw_key:
        return False, "Missing station key"

    stations = db_utils.fetch_all("SELECT * FROM exam_stations WHERE is_active = TRUE")
    for st in stations:
        if check_password_hash(st["api_key_hash"], raw_key):
            if not _allowed_ip(st.get("ip_whitelist"), ip):
                return False, "Station not allowed from this IP"
            return True, st

    return False, "Invalid station key"

def _allowed_ip(ip_whitelist, ip):
    if not ip_whitelist:
        return True
    allowed = [x.strip() for x in ip_whitelist.split(",") if x.strip()]
    return ip in allowed
