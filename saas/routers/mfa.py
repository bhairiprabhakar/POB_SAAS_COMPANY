"""
MFA management (Phase 6) -- TOTP via any authenticator app.

Flow:
  1. GET  /api/v1/auth/mfa/status   -> {enabled, has_secret}
  2. POST /api/v1/auth/mfa/setup    -> {secret, otpauth_uri}  (store secret)
  3. POST /api/v1/auth/mfa/enable   -> verify one code, enable 2FA
  4. POST /api/v1/auth/mfa/disable  -> verify one code, turn off + wipe secret

The login-side gate lives in routers/auth.py (/login returns mfa_required and
POST /mfa/verify completes the login with the TOTP code).
"""
from fastapi import APIRouter, Depends, HTTPException

from .. import totp
from ..audit import log_action
from ..deps import TenantContext, require_permission

router = APIRouter(prefix="/api/v1/auth/mfa", tags=["auth mfa"])


@router.get("/status")
def mfa_status(ctx: TenantContext = Depends(require_permission("mfa.manage"))):
    return {"enabled": bool(ctx.user.get("mfa_enabled")),
            "has_secret": bool(ctx.user.get("mfa_secret"))}


@router.post("/setup")
def mfa_setup(ctx: TenantContext = Depends(require_permission("mfa.manage"))):
    """Generate a fresh TOTP secret. Re-running rotates the pending secret."""
    conn = ctx.conn
    if ctx.user.get("mfa_enabled"):
        raise HTTPException(409, "Two-factor auth is already enabled")
    secret = totp.generate_secret()
    uri = totp.provisioning_uri(secret, f"{ctx.user['username']}@{ctx.claims.get('tenant_db', 'tenant')}")
    c = conn.cursor()
    c.execute("UPDATE users SET mfa_secret=%s WHERE id=%s", (secret, ctx.user["id"]))
    conn.commit()
    log_action(conn, ctx.user["id"], "mfa.setup", "user", ctx.user["id"],
               {"enabled": False})
    return {"secret": secret, "otpauth_uri": uri}


@router.post("/enable")
def mfa_enable(body: dict, ctx: TenantContext = Depends(require_permission("mfa.manage"))):
    """Enable 2FA once the user proves they can generate codes."""
    conn = ctx.conn
    secret = ctx.user.get("mfa_secret")
    if not secret:
        raise HTTPException(400, "Run /mfa/setup first")
    if ctx.user.get("mfa_enabled"):
        raise HTTPException(409, "Two-factor auth is already enabled")
    if not totp.verify(secret, body.get("code") or ""):
        raise HTTPException(400, "Invalid code")
    c = conn.cursor()
    c.execute("UPDATE users SET mfa_enabled=TRUE, mfa_setup_required=FALSE WHERE id=%s", (ctx.user["id"],))
    conn.commit()
    log_action(conn, ctx.user["id"], "mfa.enable", "user", ctx.user["id"])
    return {"ok": True, "enabled": True}


@router.post("/disable")
def mfa_disable(body: dict, ctx: TenantContext = Depends(require_permission("mfa.manage"))):
    """Disable 2FA (requires a valid current code) and wipe the secret."""
    conn = ctx.conn
    secret = ctx.user.get("mfa_secret")
    if not ctx.user.get("mfa_enabled") or not secret:
        raise HTTPException(400, "Two-factor auth is not enabled")
    if not totp.verify(secret, body.get("code") or ""):
        raise HTTPException(400, "Invalid code")
    c = conn.cursor()
    c.execute("UPDATE users SET mfa_enabled=FALSE, mfa_secret=NULL WHERE id=%s", (ctx.user["id"],))
    conn.commit()
    log_action(conn, ctx.user["id"], "mfa.disable", "user", ctx.user["id"])
    return {"ok": True, "enabled": False}


# ── Admin helper (unused by UI for now) ─────────────────────────────────────

def reset_user_mfa(conn, actor_id: int, user_id: int) -> None:
    """Force-reset another user's 2FA (help desk flow)."""
    c = conn.cursor()
    c.execute("UPDATE users SET mfa_enabled=FALSE, mfa_secret=NULL WHERE id=%s", (user_id,))
    conn.commit()
    log_action(conn, actor_id, "mfa.reset", "user", user_id)
