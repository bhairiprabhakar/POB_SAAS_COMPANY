"""
Authentication endpoints for the SaaS platform.

Two scopes:
  - superadmin  -> control plane (platform DB)
  - tenant      -> a specific division database, resolved by division slug at
                   login time. The access token carries tenant_db so every
                   later request routes to the right isolated database.
"""
from fastapi import APIRouter, Depends, HTTPException, Request

from .. import config, platform_db, pools, security, storage
from ..db_utils import fetchone_dict
from ..deps import get_tenant_context
from ..ratelimit import login_allowed, login_failed, login_reset, login_succeeded
from saas.passwords import hash_pw, verify_pw

import os
import json

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _lockout():
    raise HTTPException(429, "Too many login attempts. Try again later.")


# -- Super admin --

@router.post("/superadmin/login")
def superadmin_login(body: dict, request: Request):
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not login_allowed(request, "platform", username):
        _lockout()
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM super_admins WHERE username=%s", (username,))
        sa = fetchone_dict(c)
        if not sa or not verify_pw(sa["password"], password):
            login_failed(request, "platform", username)
            raise HTTPException(401, "Invalid credentials")
        if sa["status"] != "active":
            raise HTTPException(403, "Account disabled")
        login_succeeded(request, "platform", username)
        access = security.create_access_token(
            subject=str(sa["id"]), scope="superadmin",
            division_id=None, tenant_db=None, role="superadmin",
            extra={"username": sa["username"], "full_name": sa["full_name"],
                   "sa_role": sa.get("role") or "full"},
        )
        refresh = security.issue_refresh_token(conn, sa["id"], "superadmin")
        _audit(conn, sa, "superadmin.login", "super_admin", sa["id"], request)
        return {"access_token": access, "refresh_token": refresh,
                "user": {"id": sa["id"], "username": sa["username"], "full_name": sa["full_name"],
                         "owner": bool(sa.get("owner_flag")), "role": sa.get("role") or "full"}}
    finally:
        conn.close()


@router.post("/superadmin/refresh")
def superadmin_refresh(body: dict):
    token = body.get("refresh_token")
    conn = platform_db.get_db()
    try:
        res = security.rotate_refresh_token(conn, token)
        if not res or res["scope"] != "superadmin":
            raise HTTPException(401, "Invalid refresh token")
        c = conn.cursor()
        c.execute("SELECT * FROM super_admins WHERE id=%s", (res["user_id"],))
        sa = fetchone_dict(c)
        if not sa or sa["status"] != "active":
            raise HTTPException(401, "Account disabled")
        access = security.create_access_token(
            subject=str(sa["id"]), scope="superadmin", division_id=None, tenant_db=None,
            role="superadmin", extra={"username": sa["username"], "full_name": sa["full_name"],
                                      "sa_role": sa.get("role") or "full"},
        )
        return {"access_token": access, "refresh_token": res["token"]}
    finally:
        conn.close()


@router.post("/superadmin/logout")
def superadmin_logout(body: dict):
    token = body.get("refresh_token")
    conn = platform_db.get_db()
    try:
        res = security.rotate_refresh_token(conn, token)
        if res:
            c = conn.cursor()
            c.execute("UPDATE refresh_tokens SET revoked=TRUE WHERE user_id=%s AND scope='superadmin' AND revoked=FALSE",
                      (res["user_id"],))
            conn.commit()
        return {"ok": True}
    finally:
        conn.close()


# -- Company owner registration (single-company, first run) --

def _registration_open(conn) -> bool:
    c = conn.cursor()
    c.execute("SELECT id FROM companies WHERE id=1")
    return c.fetchone() is None


@router.get("/register-status")
def register_status():
    """Public: whether first-run company-owner registration is still open.
    Once the single Company exists, registration is permanently closed."""
    conn = platform_db.get_db()
    try:
        return {"registration_open": _registration_open(conn)}
    finally:
        conn.close()


@router.post("/register")
def register(body: dict, request: Request):
    """First-run registration: creates the single Company (id=1) plus the
    Company Owner, who is marked as the owning super admin (owner_flag=TRUE).
    Registration is structurally a one-shot: `companies` is a single-row
    (id=1) table and the insert is race-safe via ON CONFLICT DO NOTHING.
    On success returns super admin tokens exactly like /superadmin/login,
    so the owner is dropped straight into the platform console."""
    conn = platform_db.get_db()
    try:
        if not _registration_open(conn):
            raise HTTPException(409, "Registration is closed. A company has already been registered.")

        company = body.get("company") or {}
        owner = body.get("owner") or {}

        legal_name = (company.get("legal_name") or "").strip()
        display_name = (company.get("display_name") or legal_name).strip()
        if not legal_name or not display_name:
            raise HTTPException(400, "Company legal name and display name are required")
        username = (owner.get("username") or "").strip()
        password = owner.get("password") or ""
        full_name = (owner.get("full_name") or "").strip()
        if not username or not full_name:
            raise HTTPException(400, "Owner username and full name are required")
        if len(password) < 6:
            raise HTTPException(400, "Password must be at least 6 characters")

        c = conn.cursor()
        c.execute("SELECT id FROM super_admins WHERE username=%s", (username,))
        if c.fetchone():
            raise HTTPException(409, f"Username '{username}' is already taken")

        ok = c.execute(
            """INSERT INTO companies (id, legal_name, display_name, code, logo_path,
               address, city, state, pincode, gstin, contact_number, official_email, website)
               VALUES (1, %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (id) DO NOTHING""",
            (legal_name, display_name, (company.get("code") or "").strip(), None,
             (company.get("address") or "").strip(), (company.get("city") or "").strip(),
             (company.get("state") or "").strip(), (company.get("pincode") or "").strip(),
             (company.get("gstin") or "").strip(), (company.get("contact_number") or "").strip(),
             (company.get("official_email") or "").strip(), (company.get("website") or "").strip()),
        )
        if c.rowcount == 0:
            raise HTTPException(409, "Registration is closed. A company has already been registered.")

        c.execute(
            """INSERT INTO super_admins (username, password, full_name, email, owner_flag, company_id, role)
               VALUES (%s,%s,%s,%s,TRUE,1,'owner') RETURNING id""",
            (username, hash_pw(password), full_name,
             (owner.get("email") or "").strip()),
        )
        sa_id = c.fetchone()[0]
        conn.commit()

        access = security.create_access_token(
            subject=str(sa_id), scope="superadmin", division_id=None, tenant_db=None,
            role="superadmin", extra={"username": username, "full_name": full_name, "sa_role": "owner"},
        )
        refresh = security.issue_refresh_token(conn, sa_id, "superadmin")

        c.execute(
            "INSERT INTO platform_audit_logs (super_admin_id, actor, action, entity_type, entity_id, detail) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (sa_id, username, "superadmin.register", "company", 1,
             json.dumps({"company": legal_name, "designation": (owner.get("designation") or "Owner").strip()})),
        )
        conn.commit()

        return {
            "access_token": access,
            "refresh_token": refresh,
            "user": {"id": sa_id, "username": username, "full_name": full_name, "owner": True, "role": "owner"},
        }
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()


# -- Tenant (division) --

@router.get("/login-context/{division_slug}")
def login_context(division_slug: str):
    """Public endpoint for division-specific login URLs.

    Resolves division and returns only the branding the sign-in card
    needs. No auth required, so it must expose nothing beyond that."""
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute("""SELECT id, name, code, description, logo_path, tenant_db_name FROM divisions
                     WHERE lower(code)=lower(%s) AND status IN ('active','provisioning')""",
                  (division_slug.strip(),))
        div = c.fetchone()
        if not div:
            raise HTTPException(404, "Division not found")
        division = {"id": div[0], "name": div[1], "code": div[2],
                    "description": div[3], "has_logo": bool(div[4])}
    finally:
        conn.close()

    return {"division": division}


@router.get("/divisions")
def divisions():
    """Public: which divisions can be signed into. Lets a single-company
    deployment hide the division-code field on /login (auto-scoping the
    form to the one division) while still showing the picker when the
    platform hosts several companies."""
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute("""SELECT id, name, code FROM divisions
                     WHERE status='active' AND tenant_db_name IS NOT NULL
                     ORDER BY name""")
        rows = c.fetchall() or []
    finally:
        conn.close()
    return {"divisions": [{"id": r[0], "name": r[1], "code": r[2]} for r in rows]}


@router.get("/division-logo/{division_id}")
def division_logo(division_id: int):
    """Publicly serve a division's branding logo (used by the login page before
    any authentication). Only ever returns the exact stored logo_path file."""
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT logo_path, tenant_db_name, status FROM divisions WHERE id=%s", (division_id,))
        row = c.fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "Division not found")
    logo_path, tenant_db, status = row
    if status not in ("active", "provisioning") or not logo_path:
        raise HTTPException(404, "No logo")
    from fastapi.responses import Response
    data = storage.read(logo_path)
    if data is None:
        raise HTTPException(404, "No logo")
    import mimetypes
    mime, _ = mimetypes.guess_type(os.path.basename(logo_path))
    return Response(content=data, media_type=mime or "image/png")


@router.get("/platform-branding")
def platform_branding():
    """Public: platform name + logo flag for superadmin login / console.
    Exposes only what the sign-in card needs (never the file itself here)."""
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT platform_name, logo_path FROM platform_settings WHERE id=1")
        row = c.fetchone()
    finally:
        conn.close()
    if not row:
        return {"platform_name": "CampaignOS", "has_logo": False}
    return {"platform_name": row[0], "has_logo": bool(row[1])}


@router.get("/platform-logo")
def platform_logo():
    """Publicly serve the platform's own logo (superadmin login page)."""
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT logo_path FROM platform_settings WHERE id=1")
        row = c.fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        raise HTTPException(404, "No logo")
    data = storage.read(row[0])
    if data is None:
        raise HTTPException(404, "No logo")
    from fastapi.responses import Response
    import mimetypes
    mime, _ = mimetypes.guess_type(os.path.basename(row[0]))
    return Response(content=data, media_type=mime or "image/png")


def _resolve_division_by_slug(slug: str) -> dict:
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM divisions WHERE lower(code)=lower(%s)", (slug,))
        row = fetchone_dict(c)
        if not row:
            raise HTTPException(404, "Division not found")
        if row["status"] not in ("active", "provisioning"):
            raise HTTPException(403, "Division is not active")
        if not row["tenant_db_name"]:
            raise HTTPException(409, "Division is not provisioned")
        return row
    finally:
        conn.close()


def _resolve_division_by_tenant_db(tenant_db: str | None) -> dict:
    """Resolve the division that owns a tenant database (used by MFA, where the
    pending token has already pinned the tenant)."""
    if not tenant_db:
        raise HTTPException(401, "Invalid MFA session")
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM divisions WHERE tenant_db_name=%s", (tenant_db,))
        row = fetchone_dict(c)
        if not row:
            raise HTTPException(404, "Division not found")
        if row["status"] not in ("active", "provisioning"):
            raise HTTPException(403, "Division is not active")
        return row
    finally:
        conn.close()


def _password_change_required(user: dict, division: dict, tenant_db: str) -> dict:
    """A user who signed in with a generated temporary password must set a real
    one before using the app. Return a short-lived `password_change` token
    instead of real session tokens: deps.get_claims rejects that token_type for
    every normal API route, so only the /change-password endpoint accepts it."""
    token = security.create_access_token(
        subject=str(user["id"]), scope="tenant",
        division_id=division["id"], tenant_db=tenant_db,
        role=user.get("role_name"),
        extra={"username": user["username"], "full_name": user["full_name"]},
        token_type="password_change", ttl_seconds=900,
    )
    return {
        "password_change_required": True,
        "password_change_token": token,
        "user": {"id": user["id"], "username": user["username"],
                 "full_name": user["full_name"]},
        "division": {"id": division["id"], "name": division.get("name"),
                     "code": division.get("code")},
    }


def _issue_tenant_tokens(conn, user: dict, division: dict, level: dict | None):
    """Issue access + refresh tokens for a tenant user (shared by login,
    refresh and the MFA completion step)."""
    access = security.create_access_token(
        subject=str(user["id"]), scope="tenant",
        division_id=division["id"], tenant_db=division["tenant_db_name"],
        role=user.get("role_name"),
        extra={"username": user["username"], "full_name": user["full_name"]},
    )
    refresh = security.issue_refresh_token(conn, user["id"], "tenant")
    return {
        "access_token": access,
        "refresh_token": refresh,
        "user": {
            "id": user["id"], "username": user["username"], "full_name": user["full_name"],
            "email": user["email"], "mobile": user["mobile"], "role": user.get("role_name"),
            "hierarchy_level": level,
        },
        "division": {"id": division["id"], "name": division["name"], "code": division["code"],
                     "logo_path": division.get("logo_path")},
    }


@router.post("/login")
def tenant_login(body: dict, request: Request):
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    division_slug = (body.get("division_slug") or "").strip()

    if division_slug:
        division = _resolve_division_by_slug(division_slug)
    else:
        # Division code is not asked on the login screen. When the username is
        # unique across the platform we resolve its single division; ambiguous
        # usernames must use their division's own sign-in link.
        from .. import user_index
        matches = user_index.lookup_divisions_by_username(username)
        if len(matches) == 1:
            division = matches[0]
        elif not matches:
            raise HTTPException(401, "Invalid credentials")
        else:
            raise HTTPException(
                400, "This username belongs to several divisions. "
                     "Sign in with your division's sign-in link.")
    division_code = division["code"]
    if not login_allowed(request, division_code, username):
        _lockout()
    tenant_db = division["tenant_db_name"]

    conn = pools.get_tenant_conn(tenant_db)
    try:
        c = conn.cursor()
        c.execute(
            "SELECT u.*, r.name AS role_name FROM users u LEFT JOIN roles r ON r.id=u.role_id "
            "WHERE u.username=%s",
            (username,),
        )
        user = fetchone_dict(c)
        if not user or not verify_pw(user["password"], password):
            login_failed(request, division_code, username)
            raise HTTPException(401, "Invalid credentials")
        if user["status"] != "active":
            raise HTTPException(403, "Account disabled")

        # Division login links are an access boundary
        if division_slug:
            c.execute("""SELECT id, name FROM divisions
                         WHERE (lower(slug)=lower(%s) OR upper(name)=upper(%s) OR lower(code)=lower(%s))
                           AND status='active'
                         ORDER BY (lower(slug)=lower(%s)) DESC LIMIT 1""",
                      (division_slug, division_slug, division_slug, division_slug))
            div = c.fetchone()
            if not div:
                raise HTTPException(404, "Division not found")
            if user.get("division_id") != div[0]:
                login_failed(request, division_code, username)
                raise HTTPException(
                    403, f"This sign-in link is for the {div[1]} division. "
                         "Use your own division's link, or contact your administrator.")

        login_succeeded(request, division_code, username)

        # MFA gate: no tokens until the TOTP code is verified
        if user.get("mfa_enabled"):
            pending = security.create_access_token(
                subject=str(user["id"]), scope="tenant",
                division_id=division["id"], tenant_db=tenant_db,
                role=user.get("role_name"),
                extra={"username": user["username"], "mfa_pending": True,
                       "full_name": user["full_name"]},
                token_type="mfa_pending", ttl_seconds=config.MFA_PENDING_TTL,
            )
            login_reset(request, division_code, username)
            return {
                "mfa_required": True,
                "mfa_token": pending,
                "user": {"id": user["id"], "username": user["username"],
                         "full_name": user["full_name"]},
            }

        # Temporary-password users get no working session until they set their
        # own password -- the password_change token only unlocks /change-password.
        if user.get("must_change_password"):
            return _password_change_required(user, division, tenant_db)

        # First-login onboarding: freshly invited admins complete TOTP enrollment
        # and their profile before the dashboard is reachable. These steps are
        # frontend-routed; the token below is a real session so the endpoints
        # used by the onboarding steps (/mfa/setup, /mfa/enable, /auth/me) work.
        step = _onboarding_step(user)
        if step:
            from ..audit import log_action_req
            log_action_req(conn, user["id"], "auth.login_onboarding", request, "user",
                           user["id"], {"division": division_code, "role": user.get("role_name"),
                                        "step": step})

            level = None
            if user.get("hierarchy_level_id"):
                c.execute("SELECT id, name, label, rank FROM hierarchy_levels WHERE id=%s",
                          (user["hierarchy_level_id"],))
                level = fetchone_dict(c)
            resp = _issue_tenant_tokens(conn, user, division, level)
            c.execute("UPDATE users SET last_login=CURRENT_TIMESTAMP WHERE id=%s", (user["id"],))
            conn.commit()
            resp["user"]["onboarding"] = step
            resp["onboarding"] = step
            return resp

        from ..audit import log_action_req
        log_action_req(conn, user["id"], "auth.login", request, "user", user["id"],
                       {"division": division_code, "role": user.get("role_name")})

        level = None
        if user.get("hierarchy_level_id"):
            c.execute("SELECT id, name, label, rank FROM hierarchy_levels WHERE id=%s",
                      (user["hierarchy_level_id"],))
            level = fetchone_dict(c)
        resp = _issue_tenant_tokens(conn, user, division, level)
        c.execute("UPDATE users SET last_login=CURRENT_TIMESTAMP WHERE id=%s", (user["id"],))
        conn.commit()
        return resp
    finally:
        conn.close()


def _onboarding_step(user: dict) -> str | None:
    """First-login onboarding gate: 'mfa' (enroll TOTP) then 'profile'."""
    if user.get("mfa_setup_required") and not user.get("mfa_enabled"):
        return "mfa"
    if user.get("profile_pending"):
        return "profile"
    return None


@router.post("/mfa/verify")
def mfa_verify(body: dict, request: Request):
    """Complete a password login with a TOTP code."""
    username = (body.get("username") or "").strip()
    mfa_token = body.get("mfa_token") or ""
    code = body.get("code") or ""

    try:
        claims = security.decode_access_token(mfa_token)
    except Exception:
        raise HTTPException(401, "Invalid MFA session")
    if (claims.get("scope") != "tenant" or claims.get("token_type") != "mfa_pending"
            or not claims.get("mfa_pending")):
        raise HTTPException(401, "Invalid MFA session")
    if claims.get("sub") is None or (claims.get("username") or "") != username:
        raise HTTPException(401, "Invalid MFA session")

    division = _resolve_division_by_tenant_db(claims.get("tenant_db"))
    division_code = division["code"]
    conn = pools.get_tenant_conn(division["tenant_db_name"])
    try:
        c = conn.cursor()
        c.execute(
            "SELECT u.*, r.name AS role_name FROM users u LEFT JOIN roles r ON r.id=u.role_id "
            "WHERE u.id=%s",
            (claims["sub"],),
        )
        user = fetchone_dict(c)
        if not user or user["status"] != "active" or not user.get("mfa_enabled"):
            raise HTTPException(403, "Account not eligible for MFA login")
        from .. import totp
        if not totp.verify(user["mfa_secret"], code):
            login_failed(request, division_code, username)
            raise HTTPException(401, "Invalid two-factor code")
        login_succeeded(request, division_code, username)

        if user.get("must_change_password"):
            return _password_change_required(user, division, division["tenant_db_name"])

        step = _onboarding_step(user)
        if step:
            from ..audit import log_action_req
            log_action_req(conn, user["id"], "auth.login_onboarding", request, "user",
                           user["id"], {"division": division_code, "role": user.get("role_name"),
                                        "step": step, "mfa": True})
            level = None
            if user.get("hierarchy_level_id"):
                c.execute("SELECT id, name, label, rank FROM hierarchy_levels WHERE id=%s",
                          (user["hierarchy_level_id"],))
                level = fetchone_dict(c)
            resp = _issue_tenant_tokens(conn, user, division, level)
            c.execute("UPDATE users SET last_login=CURRENT_TIMESTAMP WHERE id=%s", (user["id"],))
            conn.commit()
            resp["user"]["onboarding"] = step
            resp["onboarding"] = step
            return resp

        from ..audit import log_action_req
        log_action_req(conn, user["id"], "auth.login", request, "user", user["id"],
                       {"division": division_code, "role": user.get("role_name"), "mfa": True})

        level = None
        if user.get("hierarchy_level_id"):
            c.execute("SELECT id, name, label, rank FROM hierarchy_levels WHERE id=%s",
                      (user["hierarchy_level_id"],))
            level = fetchone_dict(c)
        resp = _issue_tenant_tokens(conn, user, division, level)
        c.execute("UPDATE users SET last_login=CURRENT_TIMESTAMP WHERE id=%s", (user["id"],))
        conn.commit()
        return resp
    finally:
        conn.close()


@router.post("/refresh")
def tenant_refresh(body: dict):
    token = body.get("refresh_token")
    division_slug = (body.get("division_slug") or "").strip()
    if not division_slug:
        raise HTTPException(400, "division_slug required to route the refresh token")
    division = _resolve_division_by_slug(division_slug)
    conn = pools.get_tenant_conn(division["tenant_db_name"])
    try:
        res = security.rotate_refresh_token(conn, token)
        if not res or res["scope"] != "tenant":
            raise HTTPException(401, "Invalid refresh token")
        c = conn.cursor()
        c.execute("SELECT u.*, r.name AS role_name FROM users u LEFT JOIN roles r ON r.id=u.role_id WHERE u.id=%s",
                  (res["user_id"],))
        user = fetchone_dict(c)
        if not user or user["status"] != "active":
            raise HTTPException(401, "Account disabled")
        if user.get("must_change_password"):
            # a temp-password user must not keep a session alive — they must
            # complete /change-password before they can use the app
            raise HTTPException(403, "Password change required")
        access = security.create_access_token(
            subject=str(user["id"]), scope="tenant",
            division_id=division["id"], tenant_db=division["tenant_db_name"],
            role=user["role_name"],
            extra={"username": user["username"], "full_name": user["full_name"]},
        )
        return {"access_token": access, "refresh_token": res["token"]}
    finally:
        conn.close()


@router.post("/change-password")
def change_password(body: dict):
    """Set a real password for a user who signed in with a temporary one.

    The short-lived `password_change` token from the login response unlocks
    this endpoint (a normal access token also works while a session is still
    valid). The current password must be supplied; on success
    must_change_password is cleared and every existing session is revoked."""
    username = (body.get("username") or "").strip()
    token = body.get("password_change_token") or ""
    current_password = body.get("current_password") or ""
    new_password = body.get("new_password") or ""
    if len(new_password) < 6:
        raise HTTPException(400, "new password must be at least 6 characters")
    try:
        claims = security.decode_access_token(token)
    except Exception:
        raise HTTPException(401, "Invalid or expired password-change session")
    if (claims.get("scope") != "tenant"
            or claims.get("token_type") not in ("password_change", "access", None)
            or (claims.get("username") or "") != username):
        raise HTTPException(401, "Invalid or expired password-change session")

    division = _resolve_division_by_tenant_db(claims.get("tenant_db"))
    conn = pools.get_tenant_conn(division["tenant_db_name"])
    try:
        c = conn.cursor()
        c.execute(
            "SELECT u.*, r.name AS role_name FROM users u LEFT JOIN roles r ON r.id=u.role_id "
            "WHERE u.id=%s",
            (claims["sub"],),
        )
        user = fetchone_dict(c)
        if not user or user["status"] != "active":
            raise HTTPException(401, "Account unavailable")
        if claims.get("token_type") == "password_change" and not user.get("must_change_password"):
            raise HTTPException(400, "Password change is not required for this account")
        if not verify_pw(user["password"], current_password):
            raise HTTPException(401, "Current password is incorrect")
        c.execute("UPDATE users SET password=%s, must_change_password=FALSE, "
                  "last_login=CURRENT_TIMESTAMP WHERE id=%s",
                  (hash_pw(new_password), user["id"]))
        security.revoke_all_for_user(conn, user["id"])
        conn.commit()
        from ..audit import log_action
        log_action(conn, user["id"], "auth.change_password", "user", user["id"])
    finally:
        conn.close()
    return {"ok": True, "message": "Password updated. You can sign in now."}


@router.post("/logout")
def tenant_logout(body: dict):
    token = body.get("refresh_token")
    division_slug = (body.get("division_slug") or "").strip()
    if not division_slug:
        return {"ok": True}
    division = _resolve_division_by_slug(division_slug)
    conn = pools.get_tenant_conn(division["tenant_db_name"])
    try:
        res = security.rotate_refresh_token(conn, token)
        if res:
            c = conn.cursor()
            c.execute("UPDATE refresh_tokens SET revoked=TRUE WHERE user_id=%s AND scope='tenant' AND revoked=FALSE",
                      (res["user_id"],))
            conn.commit()
        return {"ok": True}
    finally:
        conn.close()


# -- Impersonation (superadmin as tenant user) --

@router.post("/impersonate")
def impersonate_user(body: dict, request: Request, claims=Depends(__import__('saas.deps', fromlist=['require_superadmin']).require_superadmin)):
    """Superadmin impersonates a tenant user in a specific division.
    Returns tenant-scoped tokens for the target user."""
    division_id = body.get("division_id")
    user_id = body.get("user_id")
    if not division_id or not user_id:
        raise HTTPException(400, "division_id and user_id required")

    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM divisions WHERE id=%s AND status IN ('active','provisioning')", (division_id,))
        division = fetchone_dict(c)
        if not division:
            raise HTTPException(404, "Division not found")
        if not division["tenant_db_name"]:
            raise HTTPException(409, "Division is not provisioned")
    finally:
        conn.close()

    tconn = pools.get_tenant_conn(division["tenant_db_name"])
    try:
        tc = tconn.cursor()
        tc.execute(
            "SELECT u.*, r.name AS role_name FROM users u LEFT JOIN roles r ON r.id=u.role_id WHERE u.id=%s",
            (user_id,),
        )
        user = fetchone_dict(tc)
        if not user:
            raise HTTPException(404, "User not found")
        if user["status"] != "active":
            raise HTTPException(403, "User is not active")

        level = None
        if user.get("hierarchy_level_id"):
            tc.execute("SELECT id, name, label, rank FROM hierarchy_levels WHERE id=%s",
                       (user["hierarchy_level_id"],))
            level = fetchone_dict(tc)

        resp = _issue_tenant_tokens(tconn, user, division, level)
        tc.execute("UPDATE users SET last_login=CURRENT_TIMESTAMP WHERE id=%s", (user["id"],))
        tconn.commit()
        return resp
    finally:
        tconn.close()


# -- Me (tenant profile + permissions) --

@router.get("/me")
def tenant_me(ctx=Depends(get_tenant_context)):
    return _tenant_me(ctx)


def _tenant_me(ctx):
    conn = ctx.conn
    c = conn.cursor()
    c.execute("SELECT users.id, users.username, users.full_name, users.email, users.mobile, "
              "users.employee_id, users.region, users.area, users.territory, "
              "users.hierarchy_level_id, users.role_id, users.parent_id, users.status, "
              "users.last_login, users.created_at, "
              "users.division, users.division_id, d.name AS division_name, "
              "COALESCE(users.mfa_enabled, FALSE) AS mfa_enabled, "
              "COALESCE(users.mfa_setup_required, FALSE) AS mfa_setup_required, "
              "COALESCE(users.profile_pending, FALSE) AS profile_pending, "
              "COALESCE((SELECT data_entry FROM roles WHERE id=users.role_id), FALSE) AS data_entry "
              "FROM users LEFT JOIN divisions d ON d.id=users.division_id WHERE users.id=%s",
              (ctx.user["id"],))
    me = fetchone_dict(c)
    if me:
        me["onboarding"] = _onboarding_step(me)
    level = None
    if me and me.get("hierarchy_level_id"):
        c.execute("SELECT id, name, label, rank FROM hierarchy_levels WHERE id=%s", (me["hierarchy_level_id"],))
        level = fetchone_dict(c)
    manager = None
    if me and me.get("parent_id"):
        c.execute("SELECT id, full_name, employee_id, hierarchy_level_id FROM users WHERE id=%s", (me["parent_id"],))
        manager = fetchone_dict(c)
    return {"user": me, "hierarchy_level": level, "manager": manager, "permissions": sorted(ctx.perms),
            "role": ctx.user.get("role_name")}


@router.put("/me")
def update_me(body: dict, ctx=Depends(get_tenant_context)):
    """Self-service profile update (used by the first-login onboarding step)."""
    conn = ctx.conn
    allowed = ("full_name", "email", "mobile", "employee_id")
    sets, params = [], []
    for f in allowed:
        if f in body and body[f] is not None:
            sets.append(f"{f}=%s")
            params.append(body[f])
    if body.get("mobile") and str(body["mobile"]).strip():
        sets.append("profile_pending=FALSE")
    if not sets:
        raise HTTPException(400, "Nothing to update")
    params.append(ctx.user["id"])
    c = conn.cursor()
    c.execute(f"UPDATE users SET {', '.join(sets)} WHERE id=%s", params)
    conn.commit()
    from ..audit import log_action
    log_action(conn, ctx.user["id"], "profile.update", "user", ctx.user["id"],
               {k: True for k in allowed if k in body and body[k] is not None})
    resp = _tenant_me(ctx)
    return {"ok": True, "user": resp["user"], "permissions": resp["permissions"],
            "role": resp["role"]}


def _audit(conn, sa: dict, action: str, entity_type: str, entity_id, request: Request):
    c = conn.cursor()
    c.execute(
        "INSERT INTO platform_audit_logs (super_admin_id, actor, action, entity_type, entity_id, ip) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (sa["id"], sa["username"], action, entity_type, entity_id, _ip(request)),
    )
    conn.commit()
