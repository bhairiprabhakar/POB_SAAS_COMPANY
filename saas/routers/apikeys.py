"""
API keys (Phase 6) -- server-to-server access for tenants.

Keys are tenant-scoped. The raw key embeds the tenant database so an
authenticator can route without a global lookup:

    pob_<tenant_db>_<random>

Only the SHA-256 hash is stored. `scopes` (permission codes) bound the key,
mirroring the role_permissions model. Authentication is handled by
deps.get_api_key_ctx (X-API-Key header).
"""
import hashlib
import secrets

from fastapi import APIRouter, Depends, HTTPException

from ..audit import log_action
from ..db_utils import fetchall_dict, fetchone_dict
from ..deps import TenantContext, require_permission

router = APIRouter(prefix="/api/v1/apikeys", tags=["api keys"])

KEY_PREFIX = "pob_"
HASHED_TAIL = 6  # characters of the raw key kept for display


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def mask_key(raw: str) -> str:
    return raw[:-HASHED_TAIL] + "*" * HASHED_TAIL


def generate_key(tenant_db: str) -> str:
    return f"{KEY_PREFIX}{tenant_db}.{secrets.token_urlsafe(32)}"


def _clean_and_validate_scopes(conn, raw_scopes) -> list[str]:
    """Shared by create + update so both paths reject the same typo'd/
    unknown permission codes -- update() previously accepted any string
    into `scopes` unvalidated, so a key's effective permissions could
    silently diverge from anything in the permission catalog."""
    scopes = [s.strip() for s in (raw_scopes or []) if s and s.strip()]
    if scopes:
        c = conn.cursor()
        c.execute("SELECT code FROM permissions WHERE code = ANY(%s)", (scopes,))
        valid = {row[0] for row in c.fetchall()}
        invalid = [s for s in scopes if s not in valid]
        if invalid:
            raise HTTPException(400, f"unknown permission codes: {', '.join(invalid)}")
    return scopes


@router.get("")
def list_keys(ctx: TenantContext = Depends(require_permission("apikey.view"))):
    c = ctx.conn.cursor()
    c.execute("""SELECT id, name, key_hash, scope, scopes, active, created_by, last_used_at,
                 created_at FROM api_keys ORDER BY id DESC""")
    items = fetchall_dict(c)
    for it in items:
        it["key"] = f"{KEY_PREFIX}{ctx.claims['tenant_db']}.{it['key_hash'][:HASHED_TAIL]}*"
    return {"items": items}


@router.post("")
def create_key(body: dict, ctx: TenantContext = Depends(require_permission("apikey.manage"))):
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    scopes = _clean_and_validate_scopes(ctx.conn, body.get("scopes"))
    raw = generate_key(ctx.claims["tenant_db"])
    c = ctx.conn.cursor()
    c.execute("""INSERT INTO api_keys (name, key_hash, scope, scopes, created_by)
                 VALUES (%s,%s,'custom',%s,%s) RETURNING id""",
              (name, hash_key(raw), scopes, ctx.user["id"]))
    kid = c.fetchone()[0]
    ctx.conn.commit()
    log_action(ctx.conn, ctx.user["id"], "apikey.create", "api_key", kid,
               {"name": name, "scopes": scopes})
    return {"ok": True, "id": kid, "name": name, "key": raw,
            "scopes": scopes,
            "note": "Store this key now -- it is shown only once."}


@router.patch("/{kid}")
def update_key(kid: int, body: dict, ctx: TenantContext = Depends(require_permission("apikey.manage"))):
    c = ctx.conn.cursor()
    c.execute("SELECT id, active FROM api_keys WHERE id=%s", (kid,))
    if not c.fetchone():
        raise HTTPException(404, "key not found")
    sets, params = [], []
    if "active" in body and body["active"] is not None:
        sets.append("active=%s")
        params.append(bool(body["active"]))
    if "name" in body and body["name"]:
        sets.append("name=%s")
        params.append(str(body["name"]).strip())
    if "scopes" in body:
        scopes = _clean_and_validate_scopes(ctx.conn, body["scopes"])
        sets.append("scopes=%s")
        params.append(scopes)
    if not sets:
        raise HTTPException(400, "nothing to update")
    params.append(kid)
    c.execute(f"UPDATE api_keys SET {', '.join(sets)} WHERE id=%s", params)
    ctx.conn.commit()
    log_action(ctx.conn, ctx.user["id"], "apikey.update", "api_key", kid, {"changes": sets})
    return {"ok": True}


@router.delete("/{kid}")
def revoke_key(kid: int, ctx: TenantContext = Depends(require_permission("apikey.manage"))):
    c = ctx.conn.cursor()
    c.execute("UPDATE api_keys SET active=FALSE WHERE id=%s RETURNING id", (kid,))
    if not c.fetchone():
        raise HTTPException(404, "key not found")
    ctx.conn.commit()
    log_action(ctx.conn, ctx.user["id"], "apikey.revoke", "api_key", kid)
    return {"ok": True}
