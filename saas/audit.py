"""
Tenant audit logging helper (Phase 5). Every critical action in a company
database is recorded so auditors can trace who did what, when, from where.

Audit enrichment: pass a FastAPI `request` (or `meta` dict) and the helper
captures client IP, user-agent and optional GPS coordinates (X-Lat / X-Lng
headers set by the mobile client). `before`/`after` capture old/new state of
the mutated row.
"""
from .db_utils import fetchone_dict


def _json_default(o):
    """Fallback for values json.dumps cannot encode.

    before/after snapshots come straight from the database, so they carry
    Decimal (NUMERIC columns), date/datetime and memoryview values. Without
    this, writing the audit row raised "Object of type Decimal is not JSON
    serializable" and took the whole mutation down with it.
    """
    import datetime
    import decimal
    if isinstance(o, decimal.Decimal):
        return float(o)
    if isinstance(o, (datetime.datetime, datetime.date, datetime.time)):
        return o.isoformat()
    if isinstance(o, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(o))} bytes>"
    if isinstance(o, set):
        return sorted(o)
    return str(o)


def request_meta(request) -> dict:
    """Extract IP / user-agent / GPS from a FastAPI request."""
    if request is None:
        return {}
    meta = {}
    ip = request.headers.get("x-forwarded-for", "")
    if ip:
        meta["ip"] = ip.split(",")[0].strip()
    else:
        client = getattr(request, "client", None)
        if client:
            meta["ip"] = client.host
    ua = request.headers.get("user-agent")
    if ua:
        meta["user_agent"] = ua
    if request.headers.get("x-lat"):
        try:
            meta["gps_lat"] = float(request.headers["x-lat"])
            meta["gps_lng"] = float(request.headers.get("x-lng", 0))
        except ValueError:
            pass
    return meta


def log_action(conn, user_id, action, entity_type=None, entity_id=None, detail=None,
               ip=None, request=None, before=None, after=None, actor=None):
    import json
    c = conn.cursor()
    if actor is None and user_id:
        c.execute("SELECT username FROM users WHERE id=%s", (user_id,))
        row = c.fetchone()
        actor = row[0] if row else None
    meta = request_meta(request)
    ip = ip or meta.get("ip")
    c.execute(
        """INSERT INTO audit_logs (user_id, actor, action, entity_type, entity_id, detail,
           ip, user_agent, gps_lat, gps_lng, before_state, after_state)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (user_id, actor, action, entity_type, entity_id,
         json.dumps(detail or {}, default=_json_default),
         ip, meta.get("user_agent"), meta.get("gps_lat"), meta.get("gps_lng"),
         json.dumps(before or {}, default=_json_default),
         json.dumps(after or {}, default=_json_default)),
    )
    conn.commit()


def log_action_req(conn, user_id, action, request, entity_type=None, entity_id=None,
                   detail=None, before=None, after=None):
    """Shorthand for callers that have the request object."""
    return log_action(conn, user_id, action, entity_type, entity_id, detail,
                      request=request, before=before, after=after)
