"""
Self-service account recovery for tenant users.

A user who forgot their division code, their username, or their password can
prove ownership of an account's contact (email or mobile) by requesting a
one-time code and entering it. On success the API returns what was forgotten
(division code / username) or -- for the password purpose -- issues a short-lived
signed recovery token that lets the user set a new password for exactly one of
the matched accounts.

Security notes:
  - The code is only ever stored as a bcrypt hash (never plaintext), is
    single-use, expires after RECOVERY_OTP_TTL, and is invalidated after a
    handful of wrong attempts.
  - The request endpoint deliberately returns a generic "if an account exists"
    message so a caller cannot use it to enumerate which emails/mobiles exist.
  - For the password purpose, the recovery token pins the list of accounts the
    OTP verified ownership of; the reset call must name one of them.
  - When no real SMTP/SMS credentials are configured, delivery is log-only and
    the code is echoed in the response (dev_otp) so local testing works.
"""
import datetime as dt
import logging
import secrets

from fastapi import APIRouter, HTTPException, Request

from .. import config, notify, platform_db, pools, security
from ..db_utils import fetchone_dict
from ..ratelimit import RateLimiter
from app.security import hash_pw, verify_pw

log = logging.getLogger("saas.recovery")

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

RECOVERY_OTP_TTL = dt.timedelta(minutes=10)
RECOVERY_OTP_MAX_ATTEMPTS = 5
RECOVERY_TOKEN_TTL = 600            # seconds
RECOVERY_REQUEST_LIMIT = 5          # OTP requests per contact per window
RECOVERY_REQUEST_WINDOW = 600       # seconds

PURPOSES = ("division_code", "username", "password")

_request_limiter = RateLimiter(RECOVERY_REQUEST_LIMIT, RECOVERY_REQUEST_WINDOW)


def _is_email(contact: str) -> bool:
    return "@" in contact


def _normalise_email(contact: str) -> str:
    return (contact or "").strip().lower()


def _normalise_mobile(contact: str) -> str:
    return "".join(ch for ch in (contact or "").strip() if ch.isdigit())


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _deliver(contact: str, code: str) -> None:
    if _is_email(contact):
        notify.send_email(contact, "Your verification code",
                          f"Your verification code is {code}. It expires in "
                          f"10 minutes. If you did not request this, ignore it.")
    else:
        notify.send_sms(contact, f"Your verification code is {code}. "
                                 "It expires in 10 minutes.")


def _dev_echo() -> bool:
    """Echo the code in the response only when there is no real delivery
    channel (otherwise the code could never reach the user and local testing
    would be impossible). Production with SMTP/SMS credentials never echoes."""
    return not (config.SMTP_HOST and config.SMTP_USER) and not config.SMS_API_KEY


def _store_otp(contact: str, purpose: str, code: str) -> None:
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute(
            "DELETE FROM recovery_otps WHERE contact=%s AND purpose=%s",
            (contact, purpose),
        )
        c.execute(
            "INSERT INTO recovery_otps (contact, purpose, otp_hash, expires_at) "
            "VALUES (%s, %s, %s, %s)",
            (contact, purpose, hash_pw(code),
             dt.datetime.utcnow() + RECOVERY_OTP_TTL),
        )
        conn.commit()
    finally:
        conn.close()


def _find_otp(contact: str, purpose: str) -> dict | None:
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT * FROM recovery_otps WHERE contact=%s AND purpose=%s "
            "AND used=FALSE ORDER BY id DESC LIMIT 1",
            (contact, purpose),
        )
        return fetchone_dict(c)
    finally:
        conn.close()


def _mark_used(otp_id: int) -> None:
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute("UPDATE recovery_otps SET used=TRUE WHERE id=%s", (otp_id,))
        conn.commit()
    finally:
        conn.close()


def _bump_attempts(otp_id: int) -> None:
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute("UPDATE recovery_otps SET attempts=attempts+1 WHERE id=%s", (otp_id,))
        conn.commit()
    finally:
        conn.close()


def _accounts_for(contact: str) -> list[dict]:
    """Resolve every account that owns this contact (email or mobile)."""
    from ..user_index import lookup_divisions_by_contact
    kind = "email" if _is_email(contact) else "mobile"
    value = _normalise_email(contact) if kind == "email" else _normalise_mobile(contact)
    rows = lookup_divisions_by_contact(kind, value)
    accounts = []
    for r in rows:
        accounts.append({
            "division_id": r["id"],
            "division_code": r["code"],
            "division_name": r["name"],
            "username": r.get("index_username"),
            "user_id": r.get("index_user_id"),
            "email": r.get("index_email"),
            "mobile": r.get("index_mobile"),
        })
    return accounts


@router.post("/recovery/request")
def recovery_request(body: dict, request: Request):
    """Send a one-time code to the contact that owns an account.

    body: {contact: email-or-mobile, purpose: division_code|username|password}
    """
    contact = (body.get("contact") or "").strip()
    purpose = (body.get("purpose") or "").strip()
    if not contact:
        raise HTTPException(400, "contact (email or mobile) is required")
    if purpose not in PURPOSES:
        raise HTTPException(400, "purpose must be one of: division_code, username, password")

    if not _request_limiter.allow(f"contact:{contact.lower()}"):
        raise HTTPException(429, "Too many requests. Try again later.")

    accounts = _accounts_for(contact)
    if not accounts:
        return {"sent": True, "message": "If an account exists for this contact, a verification code has been sent."}

    code = f"{secrets.randbelow(1000000):06d}"
    _store_otp(contact, purpose, code)
    _deliver(contact, code)
    log.info("[recovery] code sent for %s (%s, purpose=%s)", contact, "email" if _is_email(contact) else "sms", purpose)
    resp = {"sent": True, "message": "A verification code has been sent to your contact."}
    if _dev_echo():
        resp["dev_otp"] = code
    return resp


@router.post("/recovery/verify")
def recovery_verify(body: dict, request: Request):
    """Verify a one-time code and return the recovered information.

    body: {contact, purpose, otp}

    Responses by purpose:
      - division_code -> {accounts: [{division_code, division_name, username}]}
      - username      -> {accounts: [{username, division_code, division_name}]}
      - password      -> {accounts: [...], recovery_token} where the token lets
                         the caller reset the password for one of these accounts.
    """
    contact = (body.get("contact") or "").strip()
    purpose = (body.get("purpose") or "").strip()
    otp = str(body.get("otp") or "").strip()
    if not contact or purpose not in PURPOSES or not otp:
        raise HTTPException(400, "contact, purpose and otp are required")

    row = _find_otp(contact, purpose)
    expires = row["expires_at"]
    if isinstance(expires, str):
        expires = dt.datetime.fromisoformat(expires)
    if not row or dt.datetime.utcnow() > expires:
        raise HTTPException(400, "Code expired or not found. Request a new one.")
    if row["attempts"] >= RECOVERY_OTP_MAX_ATTEMPTS:
        raise HTTPException(429, "Too many attempts. Request a new code.")
    if not verify_pw(row["otp_hash"], otp):
        _bump_attempts(row["id"])
        raise HTTPException(400, "Invalid code.")

    _mark_used(row["id"])
    accounts = _accounts_for(contact)

    if purpose == "division_code":
        seen, result = set(), []
        for a in accounts:
            if a["division_code"] not in seen:
                seen.add(a["division_code"])
                result.append({"division_code": a["division_code"],
                               "division_name": a["division_name"],
                               "username": a["username"]})
        return {"accounts": result}

    if purpose == "username":
        return {"accounts": [
            {"username": a["username"], "division_code": a["division_code"],
             "division_name": a["division_name"]} for a in accounts
        ]}

    # password: pin exactly these accounts into a short-lived recovery token.
    token = security.create_access_token(
        subject=f"recovery:{contact}", scope="recovery",
        division_id=None, tenant_db=None, role=None,
        token_type="recovery", ttl_seconds=RECOVERY_TOKEN_TTL,
        extra={"purpose": "password",
               "accounts": [{"division_id": a["division_id"],
                             "user_id": a["user_id"],
                             "username": a["username"]} for a in accounts]},
    )
    return {"accounts": accounts, "recovery_token": token}


@router.post("/recovery/reset-password")
def recovery_reset_password(body: dict):
    """Set a new password for an account the caller just verified via OTP.

    body: {recovery_token, division_id, user_id, new_password}
    """
    token = (body.get("recovery_token") or "").strip()
    division_id = body.get("division_id")
    user_id = body.get("user_id")
    new_password = body.get("new_password") or ""
    if not token or not division_id or not user_id:
        raise HTTPException(400, "recovery_token, division_id and user_id are required")
    if len(new_password) < 6:
        raise HTTPException(400, "password must be at least 6 characters")

    try:
        claims = security.decode_access_token(token)
    except Exception:
        raise HTTPException(401, "Invalid or expired recovery token")
    if claims.get("scope") != "recovery" or claims.get("token_type") != "recovery" \
            or claims.get("purpose") != "password":
        raise HTTPException(401, "Invalid recovery token")
    allowed = {f"{a.get('division_id')}:{a.get('user_id')}"
               for a in (claims.get("accounts") or [])}
    if f"{division_id}:{user_id}" not in allowed:
        raise HTTPException(403, "This account was not verified")

    division = _division_by_id(division_id)
    tenant_db = division["tenant_db_name"]
    conn = pools.get_tenant_conn(tenant_db)
    try:
        c = conn.cursor()
        c.execute("SELECT id, status FROM users WHERE id=%s", (user_id,))
        row = c.fetchone()
        if not row:
            raise HTTPException(404, "Account not found")
        if row[1] != "active":
            raise HTTPException(403, "Account is disabled")
        c.execute("UPDATE users SET password=%s WHERE id=%s",
                  (hash_pw(new_password), user_id))
        security.revoke_all_for_user(conn, user_id)
        conn.commit()
    finally:
        conn.close()
    log.info("[recovery] password reset for user %s in %s", user_id, tenant_db)
    return {"ok": True, "message": "Password updated. You can sign in now."}


def _division_by_id(division_id: int) -> dict:
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM divisions WHERE id=%s", (division_id,))
        row = fetchone_dict(c)
        if not row or row["status"] not in ("active", "provisioning") or not row["tenant_db_name"]:
            raise HTTPException(404, "Division not found")
        return row
    finally:
        conn.close()
