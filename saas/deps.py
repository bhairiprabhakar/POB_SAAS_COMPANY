"""
FastAPI dependencies: authentication + tenant resolution + permission checks.

Flow per request:
  1. Bearer access token -> claims (scope, division_id, tenant_db, role)
  2. For tenant scope: open a pooled connection to the tenant DB, load the
     user + their live permission set (so role changes apply immediately),
  3. Enforce required permissions.
The tenant connection is returned to its pool when the request ends.
"""
from dataclasses import dataclass, field

from fastapi import Depends, HTTPException, Request, status

from . import pools, rbac
from .db_utils import fetchone_dict
from .security import TokenError, decode_access_token

_credentials_exc = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


def _bearer_token(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def get_claims(request: Request) -> dict:
    token = _bearer_token(request)
    if not token:
        raise _credentials_exc
    try:
        claims = decode_access_token(token)
    except TokenError:
        raise _credentials_exc
    # Reject anything that isn't a fully-authenticated access token -- in
    # particular the short-lived "mfa_pending" token issued between
    # password verification and TOTP verification (see
    # saas/routers/auth.py tenant_login/mfa_verify). That token used to be
    # decodable and accepted here like any other Bearer token, which meant
    # a user (or anyone holding a leaked mfa_pending token) could skip
    # POST /mfa/verify entirely and call the rest of the API directly --
    # a full MFA bypass. token_type defaults to "access" for every other
    # token this codebase issues, so this only affects mfa_pending tokens.
    if claims.get("token_type") not in (None, "access"):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="MFA verification required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return claims


# ── Super-admin scope ───────────────────────────────────────────────────────

def require_superadmin(claims: dict = Depends(get_claims)) -> dict:
    if claims.get("scope") != "superadmin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Super admin access required")
    return claims


# ── Tenant scope ────────────────────────────────────────────────────────────

@dataclass
class TenantContext:
    conn: object
    user: dict
    perms: set[str] = field(default_factory=set)
    claims: dict = field(default_factory=dict)

    def has(self, *codes: str) -> bool:
        return all(c in self.perms for c in codes)

    def require(self, *codes: str):
        if not self.has(*codes):
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail=f"Missing permission: {codes[0] if codes else ''}")


def _resolve_tenant(claims: dict) -> TenantContext:
    if claims.get("scope") != "tenant":
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Tenant access required")
    tenant_db = claims.get("tenant_db")
    if not tenant_db:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="No tenant context")
    conn = pools.get_tenant_conn(tenant_db)
    try:
        c = conn.cursor()
        c.execute(
            "SELECT u.*, r.name AS role_name FROM users u LEFT JOIN roles r ON r.id=u.role_id "
            "WHERE u.id=%s",
            (claims.get("sub"),),
        )
        user = fetchone_dict(c)
        if not user:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="User not found")
        if user["status"] != "active":
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Account is not active")
        perms = rbac.load_role_permissions(conn, user.get("role_id"))
        return TenantContext(conn=conn, user=user, perms=perms, claims=claims)
    except HTTPException:
        conn.close()
        raise
    except Exception:
        conn.close()
        raise


def get_tenant_context(request: Request) -> TenantContext:
    """Yields a live tenant connection for the request duration. Supports
    both interactive (Bearer) and machine (X-API-Key) authentication; every
    permission-gated route works either way."""
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        claims = get_claims(request)
        ctx = _resolve_tenant(claims)
    elif request.headers.get("x-api-key"):
        ctx = get_api_key_ctx(request)
    else:
        raise _credentials_exc
    try:
        yield ctx
    finally:
        ctx.conn.close()


def require_permission(*codes: str):
    def _dep(ctx: TenantContext = Depends(get_tenant_context)) -> TenantContext:
        ctx.require(*codes)
        return ctx
    return _dep


# ── API-key scope (Phase 6) ─────────────────────────────────────────────────

def get_api_key_ctx(request: Request) -> TenantContext:
    """Authenticate a tenant via the X-API-Key header.

    The raw key embeds the tenant database:  pob_<tenant_db>.<random>
    (see routers/apikeys.generate_key). Only its SHA-256 hash is stored, and
    the key's `scopes` (permission codes) become the request's permission set.
    """
    from . import pools, security as _sec
    from .db_utils import fetchone_dict

    raw = request.headers.get("x-api-key", "").strip()
    if not raw:
        raise _credentials_exc
    if not raw.startswith("pob_"):
        raise _credentials_exc
    rest = raw[4:]
    if "." not in rest:
        raise _credentials_exc
    tenant_db, _ = rest.split(".", 1)
    if not tenant_db.startswith("pob_"):
        raise _credentials_exc

    # Control-plane check: the tenant_db embedded in the raw key is
    # caller-supplied and must not be trusted to pick which database we
    # connect to. Validate it against the platform's `divisions` record
    # first -- an unknown or inactive tenant_db_name is rejected before we
    # ever open a tenant pool connection, so a crafted/guessed key can't be
    # used to probe for the existence of arbitrary tenant databases or force
    # connections to non-provisioned ones. Key-hash verification (below)
    # remains the actual authentication step.
    from . import platform_db
    pconn = platform_db.get_db()
    try:
        pc = pconn.cursor()
        pc.execute(
            "SELECT tenant_db_name FROM divisions WHERE tenant_db_name=%s AND status='active'",
            (tenant_db,),
        )
        if not pc.fetchone():
            raise _credentials_exc
    finally:
        pconn.close()

    import hashlib
    key_hash = hashlib.sha256(raw.encode()).hexdigest()
    conn = pools.get_tenant_conn(tenant_db)
    try:
        from . import migrations
        migrations.ensure_migrated(tenant_db)
        c = conn.cursor()
        c.execute("SELECT * FROM api_keys WHERE key_hash=%s", (key_hash,))
        key = fetchone_dict(c)
        if not key:
            raise _credentials_exc
        if not key["active"]:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="API key is revoked")
        c.execute("UPDATE api_keys SET last_used_at=CURRENT_TIMESTAMP WHERE id=%s", (key["id"],))
        conn.commit()
        user = {
            "id": None, "username": key["name"], "full_name": f"API key: {key['name']}",
            "status": "active", "role_name": None,
        }
        import json as _json
        raw_scopes = key.get("scopes") or []
        if isinstance(raw_scopes, str):
            try:
                raw_scopes = _json.loads(raw_scopes)
            except Exception:
                raw_scopes = []
        perms = set(raw_scopes)
        return TenantContext(conn=conn, user=user, perms=perms, claims={
            "scope": "tenant", "tenant_db": tenant_db, "sub": None,
            "api_key_id": key["id"], "api_key_name": key["name"],
        })
    except HTTPException:
        conn.close()
        raise
    except Exception:
        conn.close()
        raise


def require_api_key(*codes: str):
    """Dependency: tenant context authenticated by API key with the given
    permission codes."""
    def _dep(ctx: TenantContext = Depends(get_api_key_ctx)) -> TenantContext:
        ctx.require(*codes)
        return ctx
    return _dep
