"""
JWT tokens for the SaaS platform.

- Access token: short-lived (15 min default), carries identity + tenant DB.
- Refresh token: long-lived (7 days), stored (hashed) in the DB so it can be
  revoked. Rotated on every refresh.

Password hashing is shared with the legacy app (app/security.py) so a company
admin bootstrapped by this platform and a user imported from the legacy app
verify identically.
"""
import hashlib
import secrets
import time
import uuid

import jwt

from . import config
from . import db_utils

_bearer_error = None
try:
    from fastapi import HTTPException
    _bearer_error = HTTPException
except Exception:  # pragma: no cover
    pass


class TokenError(Exception):
    pass


def _now():
    return int(time.time())


def create_access_token(subject: str, scope: str, division_id: int | None,
                        tenant_db: str | None, role: str | None = None,
                        extra: dict | None = None, token_type: str = "access",
                        ttl_seconds: int | None = None) -> str:
    """token_type distinguishes a fully-authenticated access token ("access")
    from an intermediate token that still needs a second factor
    ("mfa_pending") -- see saas/deps.py get_claims(), which rejects anything
    that isn't token_type="access" for normal API use. This used to be a
    bare `mfa_pending: True` flag in `extra` that only /mfa/verify checked;
    nothing stopped that same pending token from being used directly as a
    Bearer token against the rest of the API, which was a full MFA bypass.
    ttl_seconds overrides config.ACCESS_TOKEN_TTL when set (used to give
    mfa_pending tokens a much shorter lifetime than a real session)."""
    payload = {
        "sub": subject,
        "scope": scope,          # "superadmin" | "tenant"
        "division_id": division_id,
        "tenant_db": tenant_db,
        "role": role,
        "token_type": token_type,
        "iat": _now(),
        "exp": _now() + (ttl_seconds if ttl_seconds is not None else config.ACCESS_TOKEN_TTL),
        "jti": secrets.token_hex(8),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, config.JWT_SECRET, algorithm=config.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    try:
        return jwt.decode(token, config.JWT_SECRET, algorithms=[config.JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise TokenError("token expired")
    except jwt.InvalidTokenError:
        raise TokenError("invalid token")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_refresh_token(conn, user_id: int, scope: str, family_id: str | None = None) -> str:
    """Insert a refresh token row and return the raw token string.

    Every token belongs to a `family_id` (a fresh uuid4 for a brand-new
    login, carried forward across rotations of the same session). The
    family is what lets rotate_refresh_token() detect reuse of an
    already-rotated token below."""
    token = secrets.token_urlsafe(48)
    expires = _now() + config.REFRESH_TOKEN_TTL
    fam = family_id or str(uuid.uuid4())
    c = conn.cursor()
    c.execute(
        "INSERT INTO refresh_tokens (user_id, token_hash, scope, expires_at, family_id) "
        "VALUES (%s, %s, %s, %s, %s)",
        (user_id, _hash_token(token), scope, expires, fam),
    )
    conn.commit()
    return token


def rotate_refresh_token(conn, raw_token: str) -> dict | None:
    """Validate + rotate a refresh token. Returns {user_id, scope, token} or
    None if the token is unknown/expired/revoked.

    Everything below runs in a single transaction with the row locked via
    SELECT ... FOR UPDATE, so two concurrent refresh requests presenting the
    same token can no longer both read it as valid before either revokes it
    (previously: A and B could both pass the validity check, then both
    revoke-and-reissue, handing out two live tokens for what should be one
    rotation). The second request now blocks on the lock, then sees the
    row already revoked and is rejected.

    Reuse detection: if the presented token has already been revoked (i.e.
    it was already rotated once), that's a signal the token was intercepted
    and used by two parties -- the entire token family is revoked so the
    legitimate holder is forced to re-authenticate rather than silently
    handing the attacker a working session.
    """
    c = conn.cursor()
    try:
        c.execute(
            "SELECT id, user_id, scope, expires_at, revoked, family_id "
            "FROM refresh_tokens WHERE token_hash=%s FOR UPDATE",
            (_hash_token(raw_token),),
        )
        row = db_utils.fetchone_dict(c)
        if not row:
            conn.commit()
            return None
        if row["revoked"]:
            # Already-rotated token presented again -> possible theft/replay.
            # Nuke the whole family so both the attacker and the legitimate
            # client are forced to log in again.
            if row.get("family_id"):
                c.execute(
                    "UPDATE refresh_tokens SET revoked=TRUE, revoked_at=CURRENT_TIMESTAMP "
                    "WHERE family_id=%s AND revoked=FALSE",
                    (row["family_id"],),
                )
            conn.commit()
            return None
        if _now() > row["expires_at"]:
            c.execute(
                "UPDATE refresh_tokens SET revoked=TRUE, revoked_at=CURRENT_TIMESTAMP WHERE id=%s",
                (row["id"],),
            )
            conn.commit()
            return None
        # rotate: revoke old, issue new, same family -- one transaction.
        c.execute(
            "UPDATE refresh_tokens SET revoked=TRUE, revoked_at=CURRENT_TIMESTAMP WHERE id=%s",
            (row["id"],),
        )
        new_token = secrets.token_urlsafe(48)
        expires = _now() + config.REFRESH_TOKEN_TTL
        c.execute(
            "INSERT INTO refresh_tokens (user_id, token_hash, scope, expires_at, family_id) "
            "VALUES (%s, %s, %s, %s, %s)",
            (row["user_id"], _hash_token(new_token), row["scope"], expires, row["family_id"]),
        )
        conn.commit()
        return {"user_id": row["user_id"], "scope": row["scope"], "token": new_token}
    except Exception:
        conn.rollback()
        raise


def _revoke(conn, rt_id: int):
    c = conn.cursor()
    c.execute("UPDATE refresh_tokens SET revoked=TRUE, revoked_at=CURRENT_TIMESTAMP WHERE id=%s", (rt_id,))
    conn.commit()


def revoke_all_for_user(conn, user_id: int):
    c = conn.cursor()
    c.execute("UPDATE refresh_tokens SET revoked=TRUE WHERE user_id=%s AND revoked=FALSE", (user_id,))
    conn.commit()


# ── Password reset tokens ────────────────────────────────────────────────
# Stored hashed (like refresh tokens) so a DB compromise alone can't be used
# to reset a password -- only the raw token, which is sent to the user and
# never persisted, can. See password_reset_tokens.token_hash.

def issue_password_reset_token(conn, user_id: int, ttl_seconds: int = 3600) -> str:
    """Create a password-reset token, return the raw token to send to the
    user (e.g. in a reset-link email). Only its hash is stored."""
    raw_token = secrets.token_urlsafe(48)
    expires = _now() + ttl_seconds
    c = conn.cursor()
    c.execute(
        "INSERT INTO password_reset_tokens (user_id, token_hash, expires_at) VALUES (%s, %s, %s)",
        (user_id, _hash_token(raw_token), expires),
    )
    conn.commit()
    return raw_token


def verify_password_reset_token(conn, raw_token: str) -> int | None:
    """Return the user_id for a valid, unused, unexpired reset token, or
    None. Does NOT mark it used -- call consume_password_reset_token() once
    the password has actually been changed."""
    c = conn.cursor()
    c.execute(
        "SELECT id, user_id, expires_at, used FROM password_reset_tokens WHERE token_hash=%s",
        (_hash_token(raw_token),),
    )
    row = db_utils.fetchone_dict(c)
    if not row or row["used"] or _now() > row["expires_at"]:
        return None
    return row["user_id"]


def consume_password_reset_token(conn, raw_token: str) -> None:
    c = conn.cursor()
    c.execute(
        "UPDATE password_reset_tokens SET used=TRUE WHERE token_hash=%s",
        (_hash_token(raw_token),),
    )
    conn.commit()
