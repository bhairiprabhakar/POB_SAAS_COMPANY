"""
Super admin / control-plane router: divisions, provisioning,
audit log, and platform metrics.
"""
import datetime as dt
import io
import json
import logging
import random
import re

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from openpyxl import Workbook

from .. import platform_db, provision, storage
from ..audit import log_action
from ..db_utils import fetchall_dict, fetchone_dict
from ..deps import require_owner, require_sa_path, require_sa_roles, require_superadmin
from ..upload_validation import IMAGE_KINDS, UploadValidationError, validate_upload
from ..pagination import PageLimit, PageOffset
from saas.passwords import hash_pw

log = logging.getLogger("saas.superadmin")

router = APIRouter(prefix="/api/v1/superadmin", tags=["superadmin"],
                   dependencies=[Depends(require_superadmin), Depends(require_sa_path)])


def _audit(conn, claims, action, entity_type=None, entity_id=None, detail=None):
    c = conn.cursor()
    c.execute(
        "INSERT INTO platform_audit_logs (super_admin_id, actor, action, entity_type, entity_id, detail) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (claims.get("sub"), claims.get("username"), action, entity_type, entity_id,
         json.dumps(detail or {})),
    )
    conn.commit()


def _regions(value):
    """db_utils serializes text[] columns to a JSON string; unwrap back to a list."""
    if isinstance(value, str) and value.startswith("["):
        try:
            v = json.loads(value)
            if isinstance(v, list):
                return v
        except Exception:
            pass
    return value


def _gen_code(name: str) -> str:
    stem = re.sub(r"[^A-Za-z]", "", name or "DIV").upper()[:4].ljust(4, "X")
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        for _ in range(20):
            code = f"{stem}{random.randint(1000, 9999)}"
            c.execute("SELECT id FROM divisions WHERE code=%s", (code,))
            if not c.fetchone():
                return code
        return f"DIV{random.randint(10000, 99999)}"
    finally:
        conn.close()


# -- Divisions --

@router.get("/divisions")
def list_divisions(q: str = "", status: str = ""):
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        sql = "SELECT * FROM divisions"
        where, params = [], []
        if q:
            where.append("(name ILIKE %s OR code ILIKE %s)")
            params.extend([f"%{q}%", f"%{q}%"])
        if status:
            where.append("status=%s")
            params.append(status)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC"
        c.execute(sql, params)
        items = fetchall_dict(c)
        for it in items:
            it["covered_regions"] = _regions(it.get("covered_regions"))
        c.execute("SELECT count(*) FROM divisions")
        total = c.fetchone()[0]
        return {"items": items, "total": total}
    finally:
        conn.close()


@router.get("/divisions/{did}")
def get_division(did: int):
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM divisions WHERE id=%s", (did,))
        row = fetchone_dict(c)
        if not row:
            raise HTTPException(404, "Division not found")
        row["covered_regions"] = _regions(row.get("covered_regions"))
        if row["tenant_db_name"]:
            from .. import pools
            tconn = pools.get_tenant_conn(row["tenant_db_name"])
            try:
                cur = tconn.cursor()
                cur.execute("SELECT count(*) FROM users")
                row["user_count"] = cur.fetchone()[0]
                cur.execute("SELECT count(*) FROM pob_activities")
                row["pob_count"] = cur.fetchone()[0]
                cur.execute("SELECT count(*) FROM campaigns")
                row["campaign_count"] = cur.fetchone()[0]
            except Exception:
                row["user_count"] = row["pob_count"] = row["campaign_count"] = None
            finally:
                tconn.close()
        return row
    finally:
        conn.close()


@router.post("/divisions")
def create_division(body: dict, claims=Depends(require_superadmin)):
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Division name is required")
    code = (body.get("code") or _gen_code(name)).strip().upper()
    if body.get("provision"):
        if not (body.get("admin_username") or "").strip():
            raise HTTPException(400, "admin_username required for provisioning")
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT id FROM divisions WHERE code=%s", (code,))
        if c.fetchone():
            raise HTTPException(409, f"Division code already exists: {code}")
        c.execute(
            """INSERT INTO divisions (name, code, description, contact_person, contact_email,
               contact_mobile, status, logo_path, created_by, covered_regions)
               VALUES (%s,%s,%s,%s,%s,%s,'draft',%s,%s,%s) RETURNING id""",
            (name, code, body.get("description"), body.get("contact_person"),
             body.get("contact_email"), body.get("contact_mobile"),
             body.get("logo_path"), claims.get("sub"), body.get("covered_regions") or []),
        )
        cid = c.fetchone()[0]
        conn.commit()
        _audit(conn, claims, "division.create", "division", cid, {"code": code})

        if body.get("provision") and body.get("admin_username"):
            provisioned = _provision_division(conn, cid, code, claims, body)
        else:
            provisioned = None
        c.execute("SELECT * FROM divisions WHERE id=%s", (cid,))
        row = fetchone_dict(c)
        row["covered_regions"] = _regions(row.get("covered_regions"))
        if provisioned:
            row["tenant_db_name"] = provisioned["tenant_db"]
            row["temp_password"] = provisioned["temp_password"]
        return row
    finally:
        conn.close()


def _provision_division(conn, cid, code, claims, body):
    from .. import pools
    username = (body.get("admin_username") or "division_admin").strip().lower()
    password = (body.get("admin_password") or "").strip()
    generated = False
    if not password:
        password = provision.generate_temp_password()
        generated = True
    elif len(password) < 6:
        raise HTTPException(400, "admin_password must be at least 6 characters")
    c = conn.cursor()
    c.execute("UPDATE divisions SET status='provisioning' WHERE id=%s", (cid,))
    conn.commit()
    try:
        tenant_db = provision.provision_tenant(
            cid, username, password,
            admin_email=body.get("admin_email"),
            admin_full_name=body.get("admin_full_name") or "Division Administrator",
            division_name=body.get("name"),
            division_code=code,
        )
    except Exception as exc:
        c.execute("UPDATE divisions SET status='draft', tenant_db_name=NULL, provisioned_at=NULL WHERE id=%s", (cid,))
        conn.commit()
        _audit(conn, claims, "division.provision_failed", "division", cid, {"error": str(exc)})
        raise HTTPException(500, f"Provisioning failed: {exc}")
    c.execute("UPDATE divisions SET tenant_db_name=%s, status='active', provisioned_at=CURRENT_TIMESTAMP WHERE id=%s",
              (tenant_db, cid))
    conn.commit()
    pools.get_tenant_pool(tenant_db)
    _audit(conn, claims, "division.provision", "division", cid, {"tenant_db": tenant_db})
    return {"tenant_db": tenant_db, "temp_password": password if generated else None}


@router.post("/divisions/{did}/provision")
def provision_division(did: int, body: dict, claims=Depends(require_superadmin)):
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM divisions WHERE id=%s", (did,))
        division = fetchone_dict(c)
        if not division:
            raise HTTPException(404, "Division not found")
        if division["tenant_db_name"]:
            return {"ok": True, "tenant_db": division["tenant_db_name"]}
        provisioned = _provision_division(conn, did, division["code"], claims, body)
        return {"ok": True, "tenant_db": provisioned["tenant_db"],
                "temp_password": provisioned["temp_password"]}
    finally:
        conn.close()


@router.put("/divisions/{did}")
def update_division(did: int, body: dict, claims=Depends(require_superadmin)):
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM divisions WHERE id=%s", (did,))
        if not c.fetchone():
            raise HTTPException(404, "Division not found")
        fields = ["name", "description", "contact_person", "contact_email",
                  "contact_mobile", "status", "logo_path", "covered_regions"]
        sets, params = [], []
        for f in fields:
            if f in body and body[f] is not None:
                sets.append(f"{f}=%s")
                params.append(body[f])
        if body.get("status") == "inactive":
            sets.append("status='inactive'")
        if not sets:
            raise HTTPException(400, "Nothing to update")
        params.append(did)
        c.execute(f"UPDATE divisions SET {', '.join(sets)} WHERE id=%s", params)
        conn.commit()
        _audit(conn, claims, "division.update", "division", did, {k: body.get(k) for k in fields if k in body})
        c.execute("SELECT * FROM divisions WHERE id=%s", (did,))
        row = fetchone_dict(c)
        row["covered_regions"] = _regions(row.get("covered_regions"))
        return row
    finally:
        conn.close()


_TRANSITIONS = {
    "activate": {"active", "draft", "inactive", "suspended"},
    "deactivate": {"active", "draft", "suspended"},
    "suspend": {"active"},
    "resume": {"suspended"},
    "archive": {"active", "suspended", "inactive", "draft"},
}


@router.post("/divisions/{did}/deactivate")
def deactivate_division(did: int, claims=Depends(require_superadmin)):
    return _set_status(did, "inactive", claims, allowed_from=_TRANSITIONS["deactivate"])


@router.post("/divisions/{did}/activate")
def activate_division(did: int, claims=Depends(require_superadmin)):
    return _set_status(did, "active", claims, allowed_from=_TRANSITIONS["activate"])


@router.post("/divisions/{did}/suspend")
def suspend_division(did: int, claims=Depends(require_superadmin)):
    return _set_status(did, "suspended", claims, allowed_from=_TRANSITIONS["suspend"])


@router.post("/divisions/{did}/resume")
def resume_division(did: int, claims=Depends(require_superadmin)):
    return _set_status(did, "active", claims, allowed_from=_TRANSITIONS["resume"])


@router.post("/divisions/{did}/archive")
def archive_division(did: int, claims=Depends(require_superadmin)):
    return _set_status(did, "archived", claims, allowed_from=_TRANSITIONS["archive"])


def _set_status(did, status, claims, allowed_from=None):
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT status FROM divisions WHERE id=%s", (did,))
        row = c.fetchone()
        if not row:
            raise HTTPException(404, "Division not found")
        if allowed_from is not None and row[0] not in allowed_from:
            raise HTTPException(409,
                f"Cannot move division from '{row[0]}' to '{status}'")
        c.execute("UPDATE divisions SET status=%s WHERE id=%s", (status, did))
        conn.commit()
        _audit(conn, claims, f"division.{status}", "division", did)
        return {"ok": True, "status": status}
    finally:
        conn.close()


@router.post("/divisions/{did}/reset-admin-password")
def reset_admin_password(did: int, body: dict, claims=Depends(require_superadmin)):
    username = (body.get("username") or "division_admin").strip().lower()
    password = body.get("new_password") or ""
    if len(password) < 6:
        raise HTTPException(400, "new_password must be at least 6 characters")
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT tenant_db_name FROM divisions WHERE id=%s", (did,))
        row = c.fetchone()
        if not row or not row[0]:
            raise HTTPException(409, "Division not provisioned")
        from .. import pools
        tconn = pools.get_tenant_conn(row[0])
        try:
            cur = tconn.cursor()
            cur.execute(
                """UPDATE users SET password=%s, must_change_password=TRUE,
                   mfa_setup_required=TRUE, profile_pending=TRUE WHERE username=%s""",
                (hash_pw(password), username))
            if cur.rowcount == 0:
                tconn.rollback()
                raise HTTPException(404, f"No user '{username}' in this division")
            cur.execute("SELECT id FROM users WHERE username=%s", (username,))
            target_uid = cur.fetchone()[0]
            tconn.commit()
            log_action(tconn, None, "user.reset_password", "user", target_uid,
                       {"username": username}, actor=claims.get("username"))
        finally:
            tconn.close()
        _audit(conn, claims, "division.reset_admin_password", "division", did)
        return {"ok": True}
    finally:
        conn.close()


# -- Division organization (users / roles / hierarchy) --

def _tenant_db_name(conn, did: int) -> str:
    c = conn.cursor()
    c.execute("SELECT tenant_db_name FROM divisions WHERE id=%s", (did,))
    row = c.fetchone()
    if not row:
        raise HTTPException(404, "Division not found")
    if not row[0]:
        raise HTTPException(409, "Division is not provisioned")
    return row[0]


def _tenant_conn_for(conn, did: int):
    from .. import pools
    return pools.get_tenant_conn(_tenant_db_name(conn, did))


def _actor(claims) -> dict:
    return {"id": None, "name": claims.get("username")}


def _org_ctx(tconn, claims, tenant_db):
    from types import SimpleNamespace
    return SimpleNamespace(
        conn=tconn,
        user={"id": None, "username": claims.get("username")},
        claims={"tenant_db": tenant_db},
        perms=[],
    )


def _org(claims, did, fn):
    conn = platform_db.get_db()
    tconn = None
    try:
        from . import company
        tconn = _tenant_conn_for(conn, did)
        return fn(company, _org_ctx(tconn, claims, _tenant_db_name(conn, did)))
    finally:
        if tconn:
            tconn.close()
        conn.close()


@router.get("/divisions/{did}/permissions")
def sa_permissions(did: int, claims=Depends(require_superadmin)):
    return _org(claims, did, lambda company, ctx: company.permissions(ctx=ctx))


@router.get("/divisions/{did}/roles")
def sa_roles(did: int, claims=Depends(require_superadmin)):
    return _org(claims, did, lambda company, ctx: company.roles(ctx=ctx))


@router.post("/divisions/{did}/roles")
def sa_create_role(did: int, body: dict, claims=Depends(require_superadmin)):
    return _org(claims, did, lambda company, ctx: company.create_role(body, ctx=ctx))


@router.put("/divisions/{did}/roles/{rid}")
def sa_update_role(did: int, rid: int, body: dict, claims=Depends(require_superadmin)):
    return _org(claims, did, lambda company, ctx: company.update_role(rid, body, ctx=ctx))


@router.delete("/divisions/{did}/roles/{rid}")
def sa_delete_role(did: int, rid: int, claims=Depends(require_superadmin)):
    return _org(claims, did, lambda company, ctx: company.delete_role(rid, ctx=ctx))


@router.get("/divisions/{did}/hierarchy/levels")
def sa_hierarchy_levels(did: int, claims=Depends(require_superadmin)):
    return _org(claims, did, lambda company, ctx: company.hierarchy_levels(ctx=ctx))


@router.post("/divisions/{did}/hierarchy/levels")
def sa_create_level(did: int, body: dict, claims=Depends(require_superadmin)):
    return _org(claims, did, lambda company, ctx: company.create_level(body, ctx=ctx))


@router.put("/divisions/{did}/hierarchy/levels/{lid}")
def sa_update_level(did: int, lid: int, body: dict, claims=Depends(require_superadmin)):
    return _org(claims, did, lambda company, ctx: company.update_level(lid, body, ctx=ctx))


@router.delete("/divisions/{did}/hierarchy/levels/{lid}")
def sa_delete_level(did: int, lid: int, claims=Depends(require_superadmin)):
    return _org(claims, did, lambda company, ctx: company.delete_level(lid, ctx=ctx))


@router.get("/divisions/{did}/hierarchy/tree")
def sa_hierarchy_tree(did: int, claims=Depends(require_superadmin)):
    return _org(claims, did, lambda company, ctx: company.hierarchy_tree(ctx=ctx))


@router.get("/divisions/{did}/hierarchy/bulk-template")
def sa_bulk_template(did: int, claims=Depends(require_superadmin)):
    return _org(claims, did, lambda company, ctx: company.bulk_template(ctx=ctx))


@router.get("/divisions/{did}/admins")
def sa_division_admins(did: int, claims=Depends(require_superadmin)):
    """List the division's admin users (roles division_admin / campaignos_admin).

    Division admins hold every permission within their division. The platform
    super admin manages them directly from the console: profile, password and
    status (update via PUT /divisions/{did}/users/{uid}). Password hashes and
    MFA secrets are never returned."""
    conn = platform_db.get_db()
    tconn = None
    try:
        tconn = _tenant_conn_for(conn, did)
        cur = tconn.cursor()
        cur.execute(
            """SELECT u.*, r.name AS role_name FROM users u
               LEFT JOIN roles r ON r.id=u.role_id
               WHERE r.name IN ('division_admin','campaignos_admin')
               ORDER BY u.id""")
        items = fetchall_dict(cur)
        for it in items:
            it.pop("password", None)
            it.pop("mfa_secret", None)
        return {"items": items, "total": len(items)}
    finally:
        if tconn:
            tconn.close()
        conn.close()


@router.get("/divisions/{did}/users")
def sa_list_users(did: int, q: str = "", level_id: int = None, role_id: int = None,
                  status: str = "", limit: int = PageLimit(default=200), offset: int = PageOffset(),
                  claims=Depends(require_superadmin)):
    return _org(claims, did, lambda company, ctx: company.list_users(q, level_id, role_id, status, limit, offset, ctx=ctx))


@router.get("/divisions/{did}/users/{uid}")
def sa_get_user(did: int, uid: int, claims=Depends(require_superadmin)):
    return _org(claims, did, lambda company, ctx: company.get_user(uid, ctx=ctx))


@router.post("/divisions/{did}/users")
def sa_create_user(did: int, body: dict, claims=Depends(require_superadmin)):
    return _org(claims, did, lambda company, ctx: company.create_user(body, ctx=ctx))


@router.put("/divisions/{did}/users/{uid}")
def sa_update_user(did: int, uid: int, body: dict, claims=Depends(require_superadmin)):
    return _org(claims, did, lambda company, ctx: company.update_user(uid, body, ctx=ctx))


@router.delete("/divisions/{did}/users/{uid}")
def sa_delete_user(did: int, uid: int, claims=Depends(require_superadmin)):
    return _org(claims, did, lambda company, ctx: company.delete_user(uid, ctx=ctx))


@router.post("/divisions/{did}/users/bulk-upload")
async def sa_bulk_upload_users(did: int, file: UploadFile = File(...), claims=Depends(require_superadmin)):
    conn = platform_db.get_db()
    tconn = None
    try:
        from . import company
        tconn = _tenant_conn_for(conn, did)
        return await company.bulk_upload_users(file, ctx=_org_ctx(tconn, claims, _tenant_db_name(conn, did)))
    finally:
        if tconn:
            tconn.close()
        conn.close()


# -- Brands --

@router.get("/divisions/{did}/divisions")
def sa_internal_list_divisions(did: int, q: str = "", status: str = "",
                               claims=Depends(require_superadmin)):
    conn = platform_db.get_db()
    tconn = None
    try:
        tconn = _tenant_conn_for(conn, did)
        from .. import campaign_service
        return {"items": campaign_service.list_divisions(tconn, q, status)}
    finally:
        if tconn:
            tconn.close()
        conn.close()


@router.post("/divisions/{did}/divisions")
def sa_internal_create_division(did: int, body: dict, claims=Depends(require_superadmin)):
    conn = platform_db.get_db()
    tconn = None
    try:
        tconn = _tenant_conn_for(conn, did)
        from .. import campaign_service
        new_id = campaign_service.create_division(tconn, _actor(claims), body)
        return {"ok": True, "id": new_id}
    finally:
        if tconn:
            tconn.close()
        conn.close()


@router.put("/divisions/{did}/divisions/{idv}")
def sa_internal_update_division(did: int, idv: int, body: dict, claims=Depends(require_superadmin)):
    conn = platform_db.get_db()
    tconn = None
    try:
        tconn = _tenant_conn_for(conn, did)
        from .. import campaign_service
        campaign_service.update_division(tconn, _actor(claims), idv, body)
        return {"ok": True}
    finally:
        if tconn:
            tconn.close()
        conn.close()


@router.delete("/divisions/{did}/divisions/{idv}")
def sa_internal_delete_division(did: int, idv: int, claims=Depends(require_superadmin)):
    conn = platform_db.get_db()
    tconn = None
    try:
        tconn = _tenant_conn_for(conn, did)
        from .. import campaign_service
        campaign_service.delete_division(tconn, _actor(claims), idv)
        return {"ok": True}
    finally:
        if tconn:
            tconn.close()
        conn.close()


@router.get("/divisions/{did}/brands")
def sa_list_brands(did: int, q: str = "", status: str = "", division_id: int = None,
                   claims=Depends(require_superadmin)):
    conn = platform_db.get_db()
    tconn = None
    try:
        tconn = _tenant_conn_for(conn, did)
        from .. import campaign_service
        return {"items": campaign_service.list_brands(tconn, q, status, division_id)}
    finally:
        if tconn:
            tconn.close()
        conn.close()


@router.post("/divisions/{did}/brands")
def sa_create_brand(did: int, body: dict, claims=Depends(require_superadmin)):
    conn = platform_db.get_db()
    tconn = None
    try:
        tconn = _tenant_conn_for(conn, did)
        from .. import campaign_service
        bid = campaign_service.create_brand(tconn, _actor(claims), body)
        from ..notify import notify_admins
        notify_admins(tconn, "platform.brand_created", "New brand added",
                      f"Platform added a new brand: {body.get('name') or ''}")
        return {"ok": True, "id": bid}
    finally:
        if tconn:
            tconn.close()
        conn.close()


@router.put("/divisions/{did}/brands/{bid}")
def sa_update_brand(did: int, bid: int, body: dict, claims=Depends(require_superadmin)):
    conn = platform_db.get_db()
    tconn = None
    try:
        tconn = _tenant_conn_for(conn, did)
        from .. import campaign_service
        campaign_service.update_brand(tconn, _actor(claims), bid, body)
        from ..notify import notify_admins
        notify_admins(tconn, "platform.brand_updated", "Brand updated",
                      f"Brand #{bid} was updated by the platform.")
        return {"ok": True}
    finally:
        if tconn:
            tconn.close()
        conn.close()


@router.delete("/divisions/{did}/brands/{bid}")
def sa_delete_brand(did: int, bid: int, claims=Depends(require_superadmin)):
    conn = platform_db.get_db()
    tconn = None
    try:
        tconn = _tenant_conn_for(conn, did)
        from .. import campaign_service
        campaign_service.delete_brand(tconn, _actor(claims), bid)
        from ..notify import notify_admins
        notify_admins(tconn, "platform.brand_deleted", "Brand removed",
                      f"Brand #{bid} was deleted by the platform.")
        return {"ok": True}
    finally:
        if tconn:
            tconn.close()
        conn.close()


# -- Campaigns --

@router.get("/divisions/{did}/campaigns")
def sa_list_campaigns(did: int, q: str = "", status: str = "", active: bool = None,
                      brand_id: int = None, division_id: int = None,
                      claims=Depends(require_superadmin)):
    conn = platform_db.get_db()
    tconn = None
    try:
        tconn = _tenant_conn_for(conn, did)
        from .. import campaign_service
        return {"items": campaign_service.list_campaigns(tconn, q, status, active, brand_id, division_id)}
    finally:
        if tconn:
            tconn.close()
        conn.close()


@router.get("/divisions/{did}/campaigns/{campaign_id}")
def sa_get_campaign(did: int, campaign_id: int, claims=Depends(require_superadmin)):
    conn = platform_db.get_db()
    tconn = None
    try:
        tconn = _tenant_conn_for(conn, did)
        from .. import campaign_service
        row = campaign_service.get_campaign(tconn, campaign_id)
        if not row:
            raise HTTPException(404, "campaign not found")
        return row
    finally:
        if tconn:
            tconn.close()
        conn.close()


@router.post("/divisions/{did}/campaigns")
def sa_create_campaign(did: int, body: dict, request: Request = None,
                       claims=Depends(require_superadmin)):
    conn = platform_db.get_db()
    tconn = None
    try:
        tconn = _tenant_conn_for(conn, did)
        from .. import campaign_service
        readiness = campaign_service.campaign_readiness(tconn)
        if not readiness["ready"]:
            missing = ", ".join(i["label"] for i in readiness["items"]
                                if i["key"] in ("brands", "chemists", "gifts") and not i["ready"])
            raise HTTPException(
                409,
                "Set up configuration before creating a campaign. Still missing: " + (missing or "—"),
            )
        campaign_id = campaign_service.create_campaign(tconn, _actor(claims), body)
        from ..notify import notify_admins
        notify_admins(tconn, "platform.campaign_created", "New campaign created",
                      f"A new campaign '{body.get('name') or ''}' was created by the platform.",
                      "campaign", campaign_id)
        return {"ok": True, "id": campaign_id}
    finally:
        if tconn:
            tconn.close()
        conn.close()


@router.post("/divisions/{did}/campaigns/{cid}/approve")
def sa_approve_campaign(did: int, cid: int, request: Request = None,
                        claims=Depends(require_superadmin)):
    """pending_approval -> scheduled -> active. The daily sweep is what
    normally flips scheduled campaigns to active once their start window
    opens, but that sweep only runs once per calendar day — an approval
    that lands after today's sweep has already fired would otherwise sit
    at 'scheduled' and be invisible to field users until tomorrow, even
    though the campaign's start date has already arrived. So: activate
    immediately here too, whenever the start date is already due."""
    conn = platform_db.get_db()
    tconn = None
    try:
        tconn = _tenant_conn_for(conn, did)
        c = tconn.cursor()
        c.execute("SELECT id, status, name, start_date FROM campaigns WHERE id=%s", (cid,))
        row = c.fetchone()
        if not row:
            raise HTTPException(404, "campaign not found")
        if row[1] != "pending_approval":
            raise HTTPException(409, f"Only pending campaigns can be approved (current: {row[1]})")
        start_date = row[3]
        new_status = "active" if (start_date and start_date <= dt.date.today()) else "scheduled"
        c.execute("UPDATE campaigns SET status=%s, approved_at=CURRENT_TIMESTAMP, "
                  "approved_by=%s, rejected_at=NULL, rejected_by=NULL, rejection_note=NULL "
                  "WHERE id=%s", (new_status, claims.get("sub"), cid))
        tconn.commit()
        from ..notify import notify_admins
        notify_admins(tconn, "campaign.approved", "Campaign approved",
                      f"'{row[2]}' was approved" + (" and is now active." if new_status == "active" else " and scheduled."),
                      "campaign", cid)
        return {"ok": True, "status": new_status}
    finally:
        if tconn:
            tconn.close()
        conn.close()


@router.post("/divisions/{did}/campaigns/{cid}/reject")
def sa_reject_campaign(did: int, cid: int, body: dict = None, request: Request = None,
                       claims=Depends(require_superadmin)):
    """pending_approval/scheduled -> rejected, with a reason the submitter can
    see. A terminal decision -- resubmitting starts the review over from
    scratch. Use /request-changes instead for a correction that should keep
    the campaign's history as "sent back", not "rejected"."""
    body = body or {}
    conn = platform_db.get_db()
    tconn = None
    try:
        tconn = _tenant_conn_for(conn, did)
        reason = str(body.get("reason") or "").strip()
        if not reason:
            raise HTTPException(400, "reason is required")
        c = tconn.cursor()
        c.execute("SELECT id, status, name FROM campaigns WHERE id=%s", (cid,))
        row = c.fetchone()
        if not row:
            raise HTTPException(404, "campaign not found")
        if row[1] not in ("pending_approval", "scheduled"):
            raise HTTPException(409, f"Only campaigns awaiting approval can be rejected (current: {row[1]})")
        c.execute("UPDATE campaigns SET status='rejected', rejection_note=%s, rejected_at=CURRENT_TIMESTAMP, "
                  "rejected_by=%s, approved_at=NULL, approved_by=NULL WHERE id=%s",
                  (reason, claims.get("sub"), cid))
        tconn.commit()
        from ..notify import notify_admins
        notify_admins(tconn, "campaign.rejected", "Campaign rejected",
                      f"'{row[2]}' was rejected: {reason}", "campaign", cid)
        return {"ok": True, "status": "rejected", "rejection_note": reason}
    finally:
        if tconn:
            tconn.close()
        conn.close()


@router.post("/divisions/{did}/campaigns/{cid}/request-changes")
def sa_request_campaign_changes(did: int, cid: int, body: dict = None, request: Request = None,
                                claims=Depends(require_superadmin)):
    """pending_approval -> changes_required, with a mandatory reason. A softer
    outcome than reject: the campaign goes back to the division admin to fix
    and resubmit, without it counting as a rejection in its history."""
    body = body or {}
    conn = platform_db.get_db()
    tconn = None
    try:
        tconn = _tenant_conn_for(conn, did)
        reason = str(body.get("reason") or "").strip()
        if not reason:
            raise HTTPException(400, "reason is required")
        c = tconn.cursor()
        c.execute("SELECT id, status, name FROM campaigns WHERE id=%s", (cid,))
        row = c.fetchone()
        if not row:
            raise HTTPException(404, "campaign not found")
        if row[1] != "pending_approval":
            raise HTTPException(409, f"Only campaigns awaiting approval can be sent back for changes (current: {row[1]})")
        c.execute("UPDATE campaigns SET status='changes_required', changes_required_note=%s, "
                  "changes_requested_at=CURRENT_TIMESTAMP, changes_requested_by=%s WHERE id=%s",
                  (reason, claims.get("sub"), cid))
        tconn.commit()
        from ..notify import notify_admins
        notify_admins(tconn, "campaign.changes_required", "Changes requested",
                      f"'{row[2]}' needs changes: {reason}", "campaign", cid)
        return {"ok": True, "status": "changes_required", "changes_required_note": reason}
    finally:
        if tconn:
            tconn.close()
        conn.close()


@router.put("/divisions/{did}/campaigns/{campaign_id}")
def sa_update_campaign(did: int, campaign_id: int, body: dict, request: Request = None,
                       claims=Depends(require_superadmin)):
    conn = platform_db.get_db()
    tconn = None
    try:
        tconn = _tenant_conn_for(conn, did)
        from .. import campaign_service
        campaign_service.update_campaign(tconn, _actor(claims), campaign_id, body, request=request)
        from ..notify import notify_admins
        notify_admins(tconn, "platform.campaign_updated", "Campaign updated",
                      f"Campaign #{campaign_id} was updated by the platform.",
                      "campaign", campaign_id)
        return {"ok": True}
    finally:
        if tconn:
            tconn.close()
        conn.close()


@router.post("/divisions/{did}/campaigns/{campaign_id}/extend")
def sa_extend_campaign(did: int, campaign_id: int, body: dict, request: Request = None,
                       claims=Depends(require_superadmin)):
    conn = platform_db.get_db()
    tconn = None
    try:
        tconn = _tenant_conn_for(conn, did)
        from .. import campaign_service
        return campaign_service.extend_campaign(tconn, _actor(claims), campaign_id, body, request=request)
    finally:
        if tconn:
            tconn.close()
        conn.close()


@router.delete("/divisions/{did}/campaigns/{campaign_id}")
def sa_delete_campaign(did: int, campaign_id: int, claims=Depends(require_superadmin)):
    conn = platform_db.get_db()
    tconn = None
    try:
        tconn = _tenant_conn_for(conn, did)
        from .. import campaign_service
        campaign_service.delete_campaign(tconn, _actor(claims), campaign_id)
        from ..notify import notify_admins
        notify_admins(tconn, "platform.campaign_deleted", "Campaign removed",
                      f"Campaign #{campaign_id} was deleted by the platform.")
        return {"ok": True}
    finally:
        if tconn:
            tconn.close()
        conn.close()


@router.patch("/divisions/{did}/campaigns/{campaign_id}/toggle-active")
def sa_toggle_campaign_active(did: int, campaign_id: int, claims=Depends(require_superadmin)):
    conn = platform_db.get_db()
    tconn = None
    try:
        tconn = _tenant_conn_for(conn, did)
        c = tconn.cursor()
        c.execute("SELECT id, active, name FROM campaigns WHERE id=%s", (campaign_id,))
        row = c.fetchone()
        if not row:
            raise HTTPException(404, "campaign not found")
        new_active = not row[1]
        c.execute("UPDATE campaigns SET active=%s WHERE id=%s", (new_active, campaign_id))
        tconn.commit()
        from ..notify import notify_admins
        status_word = "activated" if new_active else "deactivated"
        notify_admins(tconn, "platform.campaign_toggled", f"Campaign {status_word}",
                      f"Campaign '{row[2]}' (#{campaign_id}) was {status_word}.")
        return {"id": campaign_id, "active": new_active, "name": row[2]}
    finally:
        if tconn:
            tconn.close()
        conn.close()


@router.post("/divisions/{did}/campaigns/{campaign_id}/asset")
async def sa_upload_campaign_asset(did: int, campaign_id: int, kind: str = "logo",
                                   file: UploadFile = File(...), claims=Depends(require_superadmin)):
    data = await file.read()
    if len(data) > 2 * 1024 * 1024:
        raise HTTPException(400, "File must be under 2 MB")
    if kind not in ("logo", "banner"):
        raise HTTPException(400, "kind must be 'logo' or 'banner'")
    try:
        validate_upload(data, filename=file.filename or "", allowed_kinds=IMAGE_KINDS,
                        max_size=2 * 1024 * 1024)
    except UploadValidationError as exc:
        raise HTTPException(400, str(exc))
    conn = platform_db.get_db()
    tconn = None
    try:
        c = conn.cursor()
        c.execute("SELECT tenant_db_name FROM divisions WHERE id=%s", (did,))
        row = c.fetchone()
        if not row:
            raise HTTPException(404, "Division not found")
        if not row[0]:
            raise HTTPException(409, "Division is not provisioned")
        tenant_db = row[0]
        tconn = _tenant_conn_for(conn, did)
        cur = tconn.cursor()
        cur.execute("SELECT id FROM campaigns WHERE id=%s", (campaign_id,))
        if not cur.fetchone():
            raise HTTPException(404, "campaign not found")
        rel = storage.save(data, tenant_db, "campaigns", file.filename or "asset")
        col = "logo_path" if kind == "logo" else "banner_path"
        cur.execute(f"UPDATE campaigns SET {col}=%s WHERE id=%s", (rel, campaign_id))
        tconn.commit()
        from ..audit import log_action
        log_action(tconn, None, f"campaign.{kind}_upload", "campaign", campaign_id,
                   {"path": rel}, actor=_actor(claims).get("name"))
        from ..notify import notify_admins
        notify_admins(tconn, "platform.campaign_asset", "Campaign asset updated",
                      f"Campaign #{campaign_id} {kind} was updated by the platform.",
                      "campaign", campaign_id)
        return {"ok": True, "path": rel, "url": storage.public_url(rel)}
    finally:
        if tconn:
            tconn.close()
        conn.close()


@router.post("/divisions/{did}/logo")
async def sa_upload_division_logo(did: int, file: UploadFile = File(...),
                                 claims=Depends(require_superadmin)):
    data = await file.read()
    if len(data) > 2 * 1024 * 1024:
        raise HTTPException(400, "Logo must be under 2 MB")
    try:
        validate_upload(data, filename=file.filename or "", allowed_kinds=IMAGE_KINDS,
                        max_size=2 * 1024 * 1024)
    except UploadValidationError as exc:
        raise HTTPException(400, str(exc))
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT code, tenant_db_name, status FROM divisions WHERE id=%s", (did,))
        row = c.fetchone()
        if not row:
            raise HTTPException(404, "Division not found")
        code, tenant_db, status = row
        if status not in ("active", "provisioning"):
            raise HTTPException(403, "Division is not active")
        seg = tenant_db or f"division_{code.lower()}"
        rel = storage.save(data, seg, "branding", file.filename or "logo")
        c.execute("UPDATE divisions SET logo_path=%s WHERE id=%s", (rel, did))
        conn.commit()
        _audit(conn, claims, "division.logo_upload", "division", did, {"path": rel})
        return {"ok": True, "path": rel}
    finally:
        conn.close()


@router.delete("/divisions/{did}/logo")
def sa_delete_division_logo(did: int, claims=Depends(require_superadmin)):
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT logo_path FROM divisions WHERE id=%s", (did,))
        row = c.fetchone()
        if not row:
            raise HTTPException(404, "Division not found")
        rel = row[0]
        if rel:
            storage.delete(rel)
        c.execute("UPDATE divisions SET logo_path=%s WHERE id=%s", (None, did))
        conn.commit()
        _audit(conn, claims, "division.logo_removed", "division", did, {"removed": bool(rel)})
        return {"ok": True}
    finally:
        conn.close()


@router.get("/divisions/{did}/roles/names")
def sa_list_role_names(did: int, claims=Depends(require_superadmin)):
    conn = platform_db.get_db()
    tconn = None
    try:
        tconn = _tenant_conn_for(conn, did)
        cur = tconn.cursor()
        cur.execute("SELECT id, name FROM roles ORDER BY name")
        return {"items": [{"id": r[0], "name": r[1]} for r in cur.fetchall()]}
    finally:
        if tconn:
            tconn.close()
        conn.close()


# -- Audit + metrics --

@router.get("/audit-logs")
def audit_logs(limit: int = PageLimit(), offset: int = PageOffset()):
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM platform_audit_logs ORDER BY id DESC LIMIT %s OFFSET %s", (limit, offset))
        items = fetchall_dict(c)
        c.execute("SELECT count(*) FROM platform_audit_logs")
        return {"items": items, "total": c.fetchone()[0]}
    finally:
        conn.close()


@router.get("/metrics")
def platform_metrics():
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        def one(sql, params=()):
            c.execute(sql, params)
            return c.fetchone()[0]
        return {
            "divisions": {
                "total": one("SELECT count(*) FROM divisions"),
                "active": one("SELECT count(*) FROM divisions WHERE status='active'"),
                "provisioned": one("SELECT count(*) FROM divisions WHERE tenant_db_name IS NOT NULL"),
            },
            "super_admins": one("SELECT count(*) FROM super_admins"),
        }
    finally:
        conn.close()


# -- Platform analytics (division-level) --

_ANALYTICS_TTL = 60
_analytics_cache: dict = {"at": 0.0, "days": None, "data": None}


def _direct_tenant_conn(tenant_db: str):
    """Open a plain (unpooled) connection to a tenant DB."""
    import psycopg2
    from .. import config, db_creds
    creds = db_creds.lookup_tenant_credentials(tenant_db)
    user, password = creds if creds else (config.DB_USER, config.DB_PASSWORD)
    return psycopg2.connect(dbname=tenant_db, user=user, password=password,
                            host=config.DB_HOST, port=config.DB_PORT,
                            connect_timeout=5)


@router.get("/analytics")
def platform_analytics(days: int = 0, claims=Depends(require_superadmin)):
    import time as _time
    now = _time.time()
    if (_analytics_cache["data"] is not None and _analytics_cache["days"] == days
            and now - _analytics_cache["at"] < _ANALYTICS_TTL):
        return _analytics_cache["data"]

    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute("""SELECT d.id, d.name, d.code, d.status, d.tenant_db_name
                     FROM divisions d
                     WHERE d.tenant_db_name IS NOT NULL
                     ORDER BY d.id""")
        divisions = fetchall_dict(c)
        c.execute("SELECT count(*) FROM divisions")
        total_divisions = c.fetchone()[0]
        c.execute("SELECT count(*) FROM divisions WHERE status='active'")
        active_divisions = c.fetchone()[0]
    finally:
        conn.close()

    since = "" if not days else f" WHERE created_at >= now() - interval '{int(days)} days'"
    rows, unreachable = [], []
    totals = {"users": 0, "active_users": 0, "campaigns": 0, "active_campaigns": 0,
              "chemists": 0, "pobs": 0, "pob_value": 0.0, "verified": 0,
              "pending": 0, "rejected": 0}
    monthly: dict = {}

    for div in divisions:
        tdb = div["tenant_db_name"]
        tconn = None
        try:
            tconn = _direct_tenant_conn(tdb)
            tc = tconn.cursor()

            def one(sql, default=0):
                tc.execute(sql)
                r = tc.fetchone()
                return (r[0] if r and r[0] is not None else default)

            users = one("SELECT count(*) FROM users")
            active_users = one("SELECT count(*) FROM users WHERE status='active'")
            campaigns = one("SELECT count(*) FROM campaigns")
            active_campaigns = one("SELECT count(*) FROM campaigns WHERE active=TRUE AND status='active'")
            chemists = one("SELECT count(*) FROM chemists")
            pobs = one(f"SELECT count(*) FROM pob_activities{since}")
            value = float(one(f"SELECT coalesce(sum(pob_amount),0) FROM pob_activities{since}", 0) or 0)

            joiner = " AND" if since else " WHERE"
            verified = one(f"SELECT count(*) FROM pob_activities{since}{joiner} status IN ('verified','approved')")
            pending = one(f"SELECT count(*) FROM pob_activities{since}{joiner} status IN ('pending','pending_verification','submitted')")
            rejected = one(f"SELECT count(*) FROM pob_activities{since}{joiner} status IN ('rejected','duplicate')")

            tc.execute(f"""SELECT to_char(created_at,'YYYY-MM') AS m, count(*), coalesce(sum(pob_amount),0)
                           FROM pob_activities{since}
                           GROUP BY 1 ORDER BY 1 DESC LIMIT 12""")
            for m, cnt, amt in tc.fetchall():
                slot = monthly.setdefault(m, {"month": m, "pobs": 0, "amount": 0.0})
                slot["pobs"] += cnt
                slot["amount"] += float(amt or 0)

            decided = verified + rejected
            rows.append({
                "division_id": div["id"], "name": div["name"], "code": div["code"],
                "status": div["status"],
                "users": users, "active_users": active_users,
                "campaigns": campaigns, "active_campaigns": active_campaigns,
                "chemists": chemists, "pobs": pobs, "pob_value": round(value, 2),
                "verified": verified, "pending": pending, "rejected": rejected,
                "approval_rate": round(verified / decided * 100, 1) if decided else None,
            })
            totals["users"] += users
            totals["active_users"] += active_users
            totals["campaigns"] += campaigns
            totals["active_campaigns"] += active_campaigns
            totals["chemists"] += chemists
            totals["pobs"] += pobs
            totals["pob_value"] += value
            totals["verified"] += verified
            totals["pending"] += pending
            totals["rejected"] += rejected
        except Exception as exc:
            unreachable.append({"division_id": div["id"], "name": div["name"],
                                "code": div["code"], "error": str(exc).strip().split("\n")[0][:200]})
        finally:
            if tconn:
                try:
                    tconn.close()
                except Exception:
                    pass

    totals["pob_value"] = round(totals["pob_value"], 2)
    decided = totals["verified"] + totals["rejected"]
    totals["approval_rate"] = round(totals["verified"] / decided * 100, 1) if decided else None

    rows.sort(key=lambda r: r["pob_value"], reverse=True)
    data = {
        "days": days,
        "divisions": {"total": total_divisions, "active": active_divisions,
                      "reporting": len(rows), "unreachable": len(unreachable)},
        "totals": totals,
        "by_division": rows,
        "monthly": sorted(monthly.values(), key=lambda m: m["month"]),
        "unreachable": unreachable,
    }
    _analytics_cache.update({"at": now, "days": days, "data": data})
    return data


# -- AI usage / Gemini costing (division-wise + user-wise) --

_COSTING_TTL = 60
_costing_cache: dict = {"at": 0.0, "key": None, "data": None}


def _costing_where(days: int, model: str, division_id: int = 0):
    """Return (sql, params) for the platform ai_usage_log WHERE clause shared
    by every costing aggregate. Uses the ``a.`` alias so the same clause works
    inside every aggregate query."""
    sql, params = [], []
    if days:
        sql.append("a.created_at >= now() - make_interval(days => %s)")
        params.append(int(days))
    if model:
        sql.append("a.model_name = %s")
        params.append(model)
    if division_id:
        sql.append("COALESCE(a.division_id, a.company_id) = %s")
        params.append(int(division_id))
    return ("WHERE " + " AND ".join(sql)) if sql else "", params


@router.get("/costing")
def platform_costing(days: int = 0, division_id: int = 0, model: str = "",
                     claims=Depends(require_superadmin)):
    """Aggregate Gemini invoice-extraction spend (Batch 2: ai_usage_log).

    Reads the platform control-plane ai_usage_log -- the merged, tenant-
    annotated ledger written by saas/ai/gemini_extraction.py -- instead of the
    legacy per-tenant ocr_usage tables (vestigial under the merged path).
    Reports input/output/thinking tokens, chunks, model used and computed
    cost, summarised platform-wide, per division, per model and per (tenant)
    user, plus the newest extraction calls. ``days=0`` means all time;
    ``division_id`` / ``model`` narrow the view.
    """
    import time as _time
    from .. import config
    now = _time.time()
    key = (days, division_id, model)
    if (_costing_cache["data"] is not None and _costing_cache["key"] == key
            and now - _costing_cache["at"] < _COSTING_TTL):
        return _costing_cache["data"]

    currency = config.OCR_COST_CURRENCY or "USD"
    use_inr = currency.upper() == "INR"

    def _cost(cusd, cinr):
        return round(cinr if use_inr else cusd, 6)

    calls = itok = otok = thok = chunks = 0
    cost_usd = cost_inr = 0.0
    by_division_list = []
    by_model: dict = {}
    user_slots: dict = {}
    monthly: dict = {}
    recent: list = []

    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        div_meta = {}
        c.execute("SELECT id, name, code, tenant_db_name FROM divisions")
        for didx, dname, dcode, tdb in c.fetchall():
            div_meta[didx] = {"name": dname, "code": dcode, "tenant_db_name": tdb}

        def _div_label(didx):
            meta = div_meta.get(didx)
            if meta:
                return meta["name"], meta["code"]
            return (f"Division #{didx}" if didx else "Unattributed"), ""

        where_sql, where_params = _costing_where(days, model, division_id)

        # ── Platform totals ──────────────────────────────────────────────
        c.execute(
            f"""SELECT COUNT(*),
                       COALESCE(SUM(a.input_tokens),0), COALESCE(SUM(a.output_tokens),0),
                       COALESCE(SUM(a.thinking_tokens),0), COALESCE(SUM(a.chunk_count),0),
                       COALESCE(SUM(a.cost_usd),0), COALESCE(SUM(a.cost_inr),0)
                FROM ai_usage_log a {where_sql}""",
            where_params,
        )
        row = c.fetchone() or (0, 0, 0, 0, 0, 0, 0)
        calls, itok, otok = int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)
        thok, chunks = int(row[3] or 0), int(row[4] or 0)
        cost_usd, cost_inr = float(row[5] or 0), float(row[6] or 0)

        # ── Per division ─────────────────────────────────────────────────
        c.execute(
            f"""SELECT COALESCE(a.division_id, a.company_id, 0), COUNT(*),
                       COALESCE(SUM(a.input_tokens),0), COALESCE(SUM(a.output_tokens),0),
                       COALESCE(SUM(a.cost_usd),0), COALESCE(SUM(a.cost_inr),0)
                FROM ai_usage_log a {where_sql} GROUP BY 1""",
            where_params,
        )
        by_division_index: dict = {}
        for didx, cnt, di, do, cusd, cinr in c.fetchall():
            name, code = _div_label(didx)
            rec = {
                "division_id": didx, "name": name, "code": code,
                "calls": int(cnt or 0), "input_tokens": int(di or 0),
                "output_tokens": int(do or 0), "cost": _cost(cusd, cinr), "models": [],
            }
            by_division_index[didx] = rec
            by_division_list.append(rec)

        # per-division model sub-list (matches the legacy shape)
        c.execute(
            f"""SELECT COALESCE(a.division_id, a.company_id, 0), COALESCE(a.model_name, '(text)'),
                       COUNT(*), COALESCE(SUM(a.input_tokens),0), COALESCE(SUM(a.output_tokens),0),
                       COALESCE(SUM(a.cost_usd),0)
                FROM ai_usage_log a {where_sql} GROUP BY 1, 2""",
            where_params,
        )
        for didx, mdl, cnt, di, do, cusd in c.fetchall():
            slot = by_division_index.get(didx)
            if slot is not None:
                slot["models"].append({"model": mdl or "(text)", "calls": int(cnt or 0),
                                       "input_tokens": int(di or 0),
                                       "output_tokens": int(do or 0),
                                       "cost": round(float(cusd or 0), 6)})

        # ── Per model ────────────────────────────────────────────────────
        c.execute(
            f"""SELECT COALESCE(a.model_name, '(text)'), COUNT(*),
                       COALESCE(SUM(a.input_tokens),0), COALESCE(SUM(a.output_tokens),0),
                       COALESCE(SUM(a.thinking_tokens),0),
                       COALESCE(SUM(a.cost_usd),0), COALESCE(SUM(a.cost_inr),0)
                FROM ai_usage_log a {where_sql} GROUP BY 1""",
            where_params,
        )
        for mdl, cnt, di, do, dt, cusd, cinr in c.fetchall():
            m = mdl or "(text)"
            by_model[m] = {"model": m, "calls": int(cnt or 0),
                           "input_tokens": int(di or 0), "output_tokens": int(do or 0),
                           "thinking_tokens": int(dt or 0), "cost": _cost(cusd, cinr)}

        # ── Per (tenant) user, attributed per division ───────────────────
        c.execute(
            f"""SELECT COALESCE(a.division_id, a.company_id, 0), COALESCE(a.user_id, 0),
                       COUNT(*), COALESCE(SUM(a.input_tokens),0), COALESCE(SUM(a.output_tokens),0),
                       COALESCE(SUM(a.cost_usd),0), COALESCE(SUM(a.cost_inr),0)
                FROM ai_usage_log a {where_sql} GROUP BY 1, 2""",
            where_params,
        )
        user_ids_by_div: dict = {}
        for didx, uid, cnt, di, do, cusd, cinr in c.fetchall():
            name, code = _div_label(didx)
            ukey = f"{didx}:{uid}"
            user_slots[ukey] = {
                "division_id": didx, "division_name": name, "division_code": code,
                "user_id": uid, "username": "", "full_name": "", "role": "",
                "calls": int(cnt or 0), "input_tokens": int(di or 0),
                "output_tokens": int(do or 0), "cost": _cost(cusd, cinr),
            }
            user_ids_by_div.setdefault(didx, set()).add(uid)

        # Best-effort tenant user names (names live in each tenant DB).
        from .. import pools as _pools
        for didx, uids in user_ids_by_div.items():
            tdb = div_meta.get(didx, {}).get("tenant_db_name")
            if not tdb or not uids:
                continue
            try:
                tconn = _pools.get_tenant_conn(tdb)
                try:
                    cur = tconn.cursor()
                    cur.execute(
                        "SELECT u.id, COALESCE(u.username,''), COALESCE(u.full_name,''), "
                        "COALESCE(r.name,'') FROM users u "
                        "LEFT JOIN roles r ON r.id=u.role_id WHERE u.id = ANY(%s)",
                        (list(uids),),
                    )
                    for uid, uname, fname, rn in cur.fetchall():
                        slot = user_slots.get(f"{didx}:{uid}")
                        if slot:
                            slot["username"], slot["full_name"], slot["role"] = uname, fname, rn or ""
                finally:
                    tconn.close()
            except Exception:
                pass

        # ── Monthly trend for the spend chart ────────────────────────────
        c.execute(
            f"""SELECT to_char(a.created_at, 'YYYY-MM'), COUNT(*),
                       COALESCE(SUM(a.input_tokens),0), COALESCE(SUM(a.output_tokens),0),
                       COALESCE(SUM(a.cost_usd),0), COALESCE(SUM(a.cost_inr),0)
                FROM ai_usage_log a {where_sql} GROUP BY 1""",
            where_params,
        )
        for mth, cnt, di, do, cusd, cinr in c.fetchall():
            monthly[mth] = {"month": mth, "calls": int(cnt or 0),
                            "input_tokens": int(di or 0), "output_tokens": int(do or 0),
                            "cost": _cost(cusd, cinr)}

        # ── Newest calls (detail table) ──────────────────────────────────
        c.execute(
            f"""SELECT a.id, COALESCE(a.division_id, a.company_id, 0), a.model_name,
                       a.input_tokens, a.output_tokens, a.thinking_tokens, a.chunk_count,
                       a.cost_usd, a.cost_inr, a.original_filename, a.user_id,
                       to_char(a.created_at, 'YYYY-MM-DD HH24:MI:SS')
                FROM ai_usage_log a {where_sql}
                ORDER BY a.created_at DESC, a.id DESC LIMIT %s""",
            where_params + [200],
        )
        for rid, didx, mdl, it, ot, dt, ch, cusd, cinr, fname, uid, ts in c.fetchall():
            dname, dcode = _div_label(didx)
            recent.append({
                "id": rid, "division_id": didx, "division_name": dname,
                "division_code": dcode, "model": mdl or "(text)",
                "input_tokens": int(it or 0), "output_tokens": int(ot or 0),
                "thinking_tokens": int(dt or 0), "chunks": int(ch or 0),
                "cost": _cost(cusd, cinr), "filename": fname or "",
                "user_id": uid, "created_at": ts,
            })
    finally:
        conn.close()

    summary = {
        "calls": calls, "input_tokens": itok, "output_tokens": otok,
        "thinking_tokens": thok, "chunks": chunks, "cost": _cost(cost_usd, cost_inr),
        "cost_usd": round(cost_usd, 6), "cost_inr": round(cost_inr, 6),
        "reported_divisions": len(by_division_list), "unreachable_divisions": 0,
        "avg_cost": round(_cost(cost_usd, cost_inr) / calls, 6) if calls else 0.0,
    }
    by_model_list = sorted(by_model.values(), key=lambda x: x["cost"], reverse=True)
    by_user_list = sorted(user_slots.values(), key=lambda x: x["cost"], reverse=True)[:500]
    div_list = sorted(by_division_list, key=lambda x: x["cost"], reverse=True)

    data = {
        "days": days,
        "currency": currency,
        "summary": summary,
        "by_division": div_list,
        "by_model": by_model_list,
        "by_user": by_user_list,
        "monthly": sorted(monthly.values(), key=lambda m: m["month"]),
        "recent": recent,
        "unreachable": [],
    }
    _costing_cache.update({"at": now, "key": key, "data": data})
    return data


@router.get("/costing/export")
def platform_costing_export(days: int = 0, division_id: int = 0, model: str = "",
                            claims=Depends(require_superadmin)):
    """Export the AI Costing report as Excel: summary + by division + by
    user + daily trend + the full per-extraction detail (not capped at 200).
    Ported from legacy sa_ai_costing_export, sourced from ai_usage_log."""
    import time as _time
    now = _time.time()
    from .. import config
    currency = config.OCR_COST_CURRENCY or "USD"
    use_inr = currency.upper() == "INR"
    effective_days = days or 3650

    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        where_sql, where_params = _costing_where(days, model, division_id)

        if division_id:
            c.execute("SELECT id, name, code FROM divisions WHERE id=%s", (division_id,))
        else:
            c.execute("SELECT id, name, code FROM divisions ORDER BY name")
        divisions = {r[0]: (r[1], r[2]) for r in c.fetchall()}

        def _dlabel(didx):
            m = divisions.get(didx)
            return (m[0], m[1]) if m else (f"Division #{didx}" if didx else "Unattributed", "")

        def _cost(cusd, cinr):
            return cinr if use_inr else cusd

        # Sums / by model / by user / daily / detail.
        c.execute(
            f"""SELECT COUNT(*), COALESCE(SUM(a.input_tokens),0), COALESCE(SUM(a.output_tokens),0),
                       COALESCE(SUM(a.thinking_tokens),0), COALESCE(SUM(a.chunk_count),0),
                       COALESCE(SUM(a.cost_usd),0), COALESCE(SUM(a.cost_inr),0)
                FROM ai_usage_log a {where_sql}""",
            where_params,
        )
        totals = c.fetchone() or (0, 0, 0, 0, 0, 0, 0)

        c.execute(
            f"""SELECT COALESCE(a.model_name, '(text)'), COUNT(*),
                       COALESCE(SUM(a.input_tokens),0), COALESCE(SUM(a.output_tokens),0),
                       COALESCE(SUM(a.thinking_tokens),0),
                       COALESCE(SUM(a.cost_usd),0), COALESCE(SUM(a.cost_inr),0)
                FROM ai_usage_log a {where_sql} GROUP BY 1""",
            where_params,
        )
        by_model = c.fetchall()

        c.execute(
            f"""SELECT COALESCE(a.division_id, a.company_id, 0), COALESCE(a.user_id, 0),
                       COUNT(*), COALESCE(SUM(a.input_tokens),0), COALESCE(SUM(a.output_tokens),0),
                       COALESCE(SUM(a.cost_usd),0), COALESCE(SUM(a.cost_inr),0)
                FROM ai_usage_log a {where_sql} GROUP BY 1, 2 ORDER BY 7 DESC""",
            where_params,
        )
        by_user = c.fetchall()

        c.execute(
            f"""SELECT to_char(a.created_at, 'YYYY-MM-DD'), COUNT(*),
                       COALESCE(SUM(a.cost_inr),0), COALESCE(SUM(a.cost_usd),0)
                FROM ai_usage_log a {where_sql} GROUP BY 1 ORDER BY 1""",
            where_params,
        )
        daily = c.fetchall()

        c.execute(
            f"""SELECT a.created_at, a.original_filename, a.model_name,
                       a.input_tokens, a.output_tokens, a.thinking_tokens, a.chunk_count,
                       a.cost_usd, a.cost_inr, COALESCE(a.division_id, a.company_id, 0), a.user_id
                FROM ai_usage_log a {where_sql}
                ORDER BY a.created_at DESC LIMIT 5000""",
            where_params,
        )
        detail = c.fetchall()
    finally:
        conn.close()

    from openpyxl import Workbook
    wb = Workbook()
    ws1 = wb.active
    ws1.title = "Summary"
    ws1.append(["AI Costing Report", f"Last {days or 'all-time'} days",
                f"Currency: {currency}"])
    ws1.append([])
    ws1.append(["Calls", "Input Tokens", "Output Tokens", "Thinking Tokens",
                "Chunks", "Cost", "Cost (USD)", "Cost (INR)"])
    ws1.append([totals[0], totals[1], totals[2], totals[3], totals[4],
                _cost(totals[5], totals[6]), round(totals[5], 6), round(totals[6], 6)])
    ws1.append([])
    ws1.append(["Model", "Calls", "Input Tokens", "Output Tokens", "Thinking Tokens",
                "Cost", "Cost (USD)", "Cost (INR)"])
    for mdl, cnt, di, do, dt, cusd, cinr in by_model:
        ws1.append([mdl, cnt, di, do, dt, _cost(cusd, cinr),
                    round(cusd or 0, 6), round(cinr or 0, 6)])

    ws2 = wb.create_sheet("By User")
    ws2.append(["Division", "Code", "User ID", "Calls", "Input Tokens", "Output Tokens",
                "Cost", "Cost (USD)", "Cost (INR)"])
    for didx, uid, cnt, di, do, cusd, cinr in by_user:
        name, code = _dlabel(didx)
        ws2.append([name, code, uid, cnt, di, do, _cost(cusd, cinr),
                    round(cusd or 0, 6), round(cinr or 0, 6)])

    ws3 = wb.create_sheet("Daily Trend")
    ws3.append(["Date", "Calls", "Cost (USD)", "Cost (INR)"])
    for d, cnt, cinr, cusd in daily:
        ws3.append([str(d), cnt, round(cusd or 0, 6), round(cinr or 0, 6)])

    ws4 = wb.create_sheet("Per-Extraction Detail")
    ws4.append(["When", "Document", "Division", "Division Code", "User ID", "Model Used",
                "Input Tokens", "Output Tokens", "Thinking Tokens", "Chunks",
                "Cost (USD)", "Cost (INR)"])
    for ts, fname, mdl, it, ot, dt, ch, cusd, cinr, didx, uid in detail:
        name, code = _dlabel(didx)
        ws4.append([str(ts), fname or "", name, code, uid, mdl,
                    it or 0, ot or 0, dt or 0, ch or 1,
                    round(cusd or 0, 6), round(cinr or 0, 6)])

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="ai_costing_report_last_{effective_days}_days.xlsx"'},
    )


# -- AI model routing + pricing (platform-wide superadmin config) --

@router.get("/ai-models")
def sa_ai_models(claims=Depends(require_superadmin)):
    """Model Settings: which Gemini model handles each file category, plus the
    editable per-model USD pricing table (see saas/ai/model_registry.py)."""
    from ..ai import model_registry
    return {
        "categories": model_registry.CATEGORIES,
        "routing": model_registry.get_routing(),
        "env_defaults": model_registry.env_defaults(),
        "models": model_registry.model_options(),
        "pricing": model_registry.get_pricing_rows(),
    }


@router.post("/ai-models/routing")
def sa_ai_models_routing_save(body: dict, claims=Depends(require_superadmin)):
    """Save the per-category model overrides. An empty choice deletes that
    category's override so it follows .env again."""
    from ..ai import model_registry
    overrides = (body or {}).get("overrides")
    if overrides is None:
        overrides = body or {}
    try:
        model_registry.save_routing(overrides or {}, claims.get("sub"))
        return {"success": True,
                "message": "Model settings saved -- applies to the next document processed."}
    except Exception as exc:
        raise HTTPException(400, f"Error saving model settings: {exc}")


@router.post("/ai-models/pricing")
def sa_ai_models_pricing_save(body: dict, claims=Depends(require_superadmin)):
    """Save the pricing table rows plus an optional new-model entry. A blank
    price falls back to default pricing."""
    from ..ai import model_registry
    body = body or {}
    rows = body.get("rows") or []
    new_model = body.get("new_model") or {}
    saved = 0
    try:
        for r in rows:
            mid = (r.get("model_id") or "").strip()
            if mid:
                model_registry.upsert_pricing(
                    mid, r.get("label", ""), r.get("input"), r.get("output"))
                saved += 1
        nm = (new_model.get("model_id") or "").strip()
        if nm:
            model_registry.upsert_pricing(
                nm, new_model.get("label", ""),
                new_model.get("input"), new_model.get("output"))
            saved += 1
        return {"success": True,
                "message": f"Pricing saved -- {saved} model(s) updated. New prices apply to the next document processed."}
    except Exception as exc:
        raise HTTPException(400, f"Error saving pricing: {exc}")


@router.post("/ai-models/pricing/delete")
def sa_ai_models_pricing_delete(body: dict, claims=Depends(require_superadmin)):
    """Remove a model from pricing. Refuses if the model is currently selected
    in any routing category (see model_registry.delete_pricing)."""
    from ..ai import model_registry
    mid = ((body or {}).get("model_id") or "").strip()
    if not mid:
        raise HTTPException(400, "model_id required")
    ok, reason = model_registry.delete_pricing(mid)
    if not ok:
        raise HTTPException(400, reason or "Cannot delete that model")
    return {"success": True, "message": f"Removed '{mid}' from pricing."}


# -- Tenant credits admin (statement wallet; superadmin review) --

@router.get("/divisions/{did}/credits")
def sa_tenant_credits(did: int, claims=Depends(require_superadmin)):
    """Tenant statement-credit wallet + pending requests + ledger (the merged
    home of the legacy /superadmin/credits page)."""
    from .. import credits
    conn = platform_db.get_db()
    try:
        tconn = _tenant_conn_for(conn, did)
        try:
            return {
                "division_id": did,
                "credits": credits.get_credits(tconn),
                "pending_requests": credits.pending_credit_requests(tconn),
                "ledger": credits.credit_ledger(tconn, limit=50),
                "usage_summary": credits.credit_usage_summary(tconn),
            }
        finally:
            tconn.close()
    finally:
        conn.close()


@router.post("/divisions/{did}/credits/allocate")
def sa_tenant_credits_allocate(did: int, body: dict, claims=Depends(require_superadmin)):
    """Allocate credits to a tenant wallet (legacy /superadmin/credits/allocate)."""
    from .. import credits
    amount = int((body or {}).get("amount") or 0)
    plan = ((body or {}).get("plan") or "demo").strip() or "demo"
    if amount <= 0:
        raise HTTPException(400, "Amount must be positive")
    conn = platform_db.get_db()
    try:
        tconn = _tenant_conn_for(conn, did)
        try:
            wallet = credits.top_up_credits(tconn, amount, plan=plan)
            _audit(conn, claims, "credits.allocate", "division", did,
                   {"amount": amount, "plan": plan})
            return {"success": True,
                    "message": f"✓ {amount} credits allocated",
                    "credits": wallet}
        finally:
            tconn.close()
    finally:
        conn.close()


@router.post("/divisions/{did}/credits/requests/{rid}/approve")
def sa_tenant_credits_approve(did: int, rid: int, body: dict = None,
                              claims=Depends(require_superadmin)):
    """Approve a tenant credit request. reviewed_by stays NULL because the
    platform superadmin is not a tenant users(id) FK; the actor is recorded in
    platform_audit_logs by the platform-side _audit helper."""
    from .. import credits
    conn = platform_db.get_db()
    try:
        tconn = _tenant_conn_for(conn, did)
        try:
            req = credits.approve_credit_request(tconn, rid, reviewed_by=None)
            if not req:
                raise HTTPException(404, "Request not found or no longer pending")
            _audit(conn, claims, "credits.request.approve", "division", did,
                   {"request_id": rid, "amount": req["credits_requested"]})
            return {"success": True,
                    "message": f"✓ Credit request approved -- {req['credits_requested']} credits added"}
        finally:
            tconn.close()
    finally:
        conn.close()


@router.post("/divisions/{did}/credits/requests/{rid}/reject")
def sa_tenant_credits_reject(did: int, rid: int, body: dict = None,
                             claims=Depends(require_superadmin)):
    """Reject a tenant credit request (reviewed_by=NULL; actor in audit log)."""
    from .. import credits
    conn = platform_db.get_db()
    try:
        tconn = _tenant_conn_for(conn, did)
        try:
            ok = credits.reject_credit_request(tconn, rid, reviewed_by=None)
            if not ok:
                raise HTTPException(404, "Request not found or no longer pending")
            _audit(conn, claims, "credits.request.reject", "division", did,
                   {"request_id": rid})
            return {"success": True, "message": f"Credit request #{rid} rejected"}
        finally:
            tconn.close()
    finally:
        conn.close()


# -- Platform-wide modules (campaigns / POB / gratification / users) --
# These aggregate the same tenant tables every division holds; the loops
# mirror the analytics/costing sections above. They power the SA console's
# cross-division views (Campaigns with its approval queue, POB operations,
# gratification pipeline and the platform employee directory).

def _provisioned_divisions(conn) -> list[dict]:
    c = conn.cursor()
    c.execute("""SELECT id, name, code, status, tenant_db_name
                 FROM divisions WHERE tenant_db_name IS NOT NULL ORDER BY id""")
    return fetchall_dict(c)


@router.get("/campaigns")
def sa_all_campaigns(q: str = "", status: str = "", limit: int = 300,
                     claims=Depends(require_superadmin)):
    """Cross-division campaign list. `status` filters the list; `counts` is the
    full status histogram so the sidebar tabs can show live badges."""
    from .. import campaign_service
    conn = platform_db.get_db()
    items, counts, unreachable = [], {}, []
    try:
        divisions = _provisioned_divisions(conn)
    finally:
        conn.close()
    for div in divisions:
        tconn = None
        try:
            tconn = _direct_tenant_conn(div["tenant_db_name"])
            tc = tconn.cursor()
            tc.execute("SELECT status, count(*) FROM campaigns GROUP BY status")
            for st, n in tc.fetchall():
                counts[st] = counts.get(st, 0) + int(n or 0)
            rows = campaign_service.list_campaigns(tconn, q, status)
            for r in rows:
                r["division_id"] = div["id"]
                r["division_name"] = div["name"]
                r["division_code"] = div["code"]
                items.append(r)
        except Exception as exc:
            unreachable.append({"division_id": div["id"], "name": div["name"],
                                "code": div["code"],
                                "error": str(exc).strip().split("\n")[0][:200]})
        finally:
            if tconn:
                try:
                    tconn.close()
                except Exception:
                    pass
    items = items[:limit]
    return {"items": items, "counts": counts, "unreachable": unreachable}


# -- Verification agent management (verification_admin's own area) --
# The tenant-side verification_agent role is where POB decisions actually
# happen (division-scoped, see saas/routers/verification.py); this section
# gives the platform verification_admin a cross-division view to add/assign/
# activate those agents and see their performance, WITHOUT granting them
# direct access to the tenant's full /divisions/{did}/users surface (which
# would also expose creating/editing division admins and every other role).

def _verification_agent_role_id(tconn) -> int | None:
    c = tconn.cursor()
    c.execute("SELECT id FROM roles WHERE name='verification_agent'")
    row = c.fetchone()
    return row[0] if row else None


def _assert_is_verification_agent(tconn, uid: int) -> None:
    c = tconn.cursor()
    c.execute("SELECT r.name FROM users u JOIN roles r ON r.id=u.role_id WHERE u.id=%s", (uid,))
    row = c.fetchone()
    if not row or row[0] != "verification_agent":
        raise HTTPException(404, "verification agent not found")


@router.get("/verification-agents")
def sa_list_verification_agents(status: str = "", claims=Depends(require_superadmin)):
    """Every verification_agent across every provisioned division, with their
    approve/reject/duplicate counts and average turnaround time."""
    conn = platform_db.get_db()
    items, unreachable = [], []
    try:
        divisions = _provisioned_divisions(conn)
    finally:
        conn.close()
    for div in divisions:
        tconn = None
        try:
            tconn = _direct_tenant_conn(div["tenant_db_name"])
            tc = tconn.cursor()
            tc.execute(
                """SELECT u.id, u.username, u.full_name, u.email, u.mobile, u.status,
                          u.division_id, d.name AS internal_division_name, u.created_at
                   FROM users u JOIN roles r ON r.id=u.role_id
                   LEFT JOIN divisions d ON d.id=u.division_id
                   WHERE r.name='verification_agent' ORDER BY u.id""")
            agents = fetchall_dict(tc)
            if agents:
                ids = [a["id"] for a in agents]
                tc.execute(
                    """SELECT v.verifier_id,
                              count(*) FILTER (WHERE v.status='approved') AS approved,
                              count(*) FILTER (WHERE v.status='rejected') AS rejected,
                              count(*) FILTER (WHERE v.status='duplicate') AS duplicate,
                              count(*) AS total,
                              avg(EXTRACT(EPOCH FROM (v.verified_at - v.started_at)) / 3600.0)
                                FILTER (WHERE v.verified_at IS NOT NULL AND v.started_at IS NOT NULL) AS avg_tat_hours
                       FROM pob_verifications v
                       WHERE v.verifier_id = ANY(%s)
                       GROUP BY v.verifier_id""", (ids,))
                perf = {r["verifier_id"]: r for r in fetchall_dict(tc)}
                for a in agents:
                    p = perf.get(a["id"], {})
                    a["verified_total"] = p.get("total") or 0
                    a["approved"] = p.get("approved") or 0
                    a["rejected"] = p.get("rejected") or 0
                    a["duplicate"] = p.get("duplicate") or 0
                    a["avg_tat_hours"] = round(p["avg_tat_hours"], 1) if p.get("avg_tat_hours") is not None else None
                    a["division_id"] = div["id"]
                    a["division_name"] = div["name"]
                    a["division_code"] = div["code"]
            if status:
                agents = [a for a in agents if a["status"] == status]
            items.extend(agents)
        except Exception as exc:
            unreachable.append({"division_id": div["id"], "name": div["name"],
                                "error": str(exc).strip().split("\n")[0][:200]})
        finally:
            if tconn:
                try:
                    tconn.close()
                except Exception:
                    pass
    return {"items": items, "unreachable": unreachable}


@router.post("/verification-agents")
def sa_create_verification_agent(body: dict, claims=Depends(require_superadmin)):
    """Create a verification_agent bound to a division. `division_id` (the
    platform's division id) picks the tenant; a tenant with exactly one
    internal division is auto-assigned, otherwise pass `internal_division_id`
    explicitly."""
    division_id = body.get("division_id")
    if not division_id:
        raise HTTPException(400, "division_id is required")
    conn = platform_db.get_db()
    tconn = None
    try:
        tconn = _tenant_conn_for(conn, division_id)
        role_id = _verification_agent_role_id(tconn)
        if not role_id:
            raise HTTPException(500, "verification_agent role is not configured for this division")
        c = tconn.cursor()
        internal_division_id = body.get("internal_division_id")
        if not internal_division_id:
            c.execute("SELECT id FROM divisions ORDER BY id")
            rows = c.fetchall()
            if len(rows) == 1:
                internal_division_id = rows[0][0]
            elif len(rows) > 1:
                raise HTTPException(400, "This company has multiple internal divisions -- specify internal_division_id")
        from . import company
        user_body = {
            "username": body.get("username"), "password": body.get("password"),
            "full_name": body.get("full_name"), "email": body.get("email"),
            "mobile": body.get("mobile"), "employee_id": body.get("employee_id"),
            "role_id": role_id, "division_id": internal_division_id,
        }
        result = company.create_user(user_body, ctx=_org_ctx(tconn, claims, _tenant_db_name(conn, division_id)))
        _audit(conn, claims, "verification_agent.create", "user", result.get("id"),
               {"division_id": division_id, "username": body.get("username")})
        result["division_id"] = division_id
        return result
    finally:
        if tconn:
            tconn.close()
        conn.close()


@router.get("/verification-agents/{did}/{uid}")
def sa_verification_agent_detail(did: int, uid: int, claims=Depends(require_superadmin)):
    """A single agent's profile plus their most recent verification decisions."""
    conn = platform_db.get_db()
    tconn = None
    try:
        tconn = _tenant_conn_for(conn, did)
        _assert_is_verification_agent(tconn, uid)
        c = tconn.cursor()
        c.execute("SELECT * FROM users WHERE id=%s", (uid,))
        row = fetchone_dict(c)
        row.pop("password", None)
        row.pop("mfa_secret", None)
        c.execute(
            """SELECT v.id, v.status, v.reason, v.started_at, v.verified_at,
                      pa.id AS pob_id, pa.invoice_number, pa.pob_amount,
                      cmp.name AS campaign_name, ch.name AS chemist_name
               FROM pob_verifications v
               JOIN pob_activities pa ON pa.id=v.pob_id
               LEFT JOIN campaigns cmp ON cmp.id=pa.campaign_id
               LEFT JOIN chemists ch ON ch.id=pa.chemist_id
               WHERE v.verifier_id=%s ORDER BY v.id DESC LIMIT 25""", (uid,))
        row["recent_activity"] = fetchall_dict(c)
        return row
    finally:
        if tconn:
            tconn.close()
        conn.close()


@router.put("/verification-agents/{did}/{uid}")
def sa_update_verification_agent(did: int, uid: int, body: dict, claims=Depends(require_superadmin)):
    """Edit profile / status / password / internal division assignment. Only
    ever touches users who already hold the verification_agent role."""
    conn = platform_db.get_db()
    tconn = None
    try:
        tconn = _tenant_conn_for(conn, did)
        _assert_is_verification_agent(tconn, uid)
        from . import company
        result = company.update_user(uid, body, ctx=_org_ctx(tconn, claims, _tenant_db_name(conn, did)))
        _audit(conn, claims, "verification_agent.update", "user", uid,
               {k: body[k] for k in ("status", "division_id") if k in body})
        return result
    finally:
        if tconn:
            tconn.close()
        conn.close()


@router.get("/pob")
def sa_all_pob(status: str = "", limit: int = 200, claims=Depends(require_superadmin)):
    """Cross-division POB operations: per-status totals per division plus a
    unified list of the most recent records (optionally filtered by status)."""
    conn = platform_db.get_db()
    counts, recent, unreachable = {}, [], []
    try:
        divisions = _provisioned_divisions(conn)
    finally:
        conn.close()
    where = "WHERE pa.status=%s" if status else ""
    params = [status] if status else []
    for div in divisions:
        tconn = None
        try:
            tconn = _direct_tenant_conn(div["tenant_db_name"])
            tc = tconn.cursor()
            tc.execute("SELECT status, count(*) FROM pob_activities GROUP BY status")
            for st, n in tc.fetchall():
                counts[st] = counts.get(st, 0) + int(n or 0)
            tc.execute(
                f"""SELECT pa.id, pa.status, pa.pob_amount, pa.created_at,
                           COALESCE(u.full_name, '—'), COALESCE(c.name, '—'),
                           COALESCE(ch.name, '—'), pv.verification_id
                    FROM pob_activities pa
                    LEFT JOIN users u ON u.id=pa.user_id
                    LEFT JOIN campaigns c ON c.id=pa.campaign_id
                    LEFT JOIN chemists ch ON ch.id=pa.chemist_id
                    LEFT JOIN LATERAL (
                        SELECT v.id AS verification_id
                        FROM pob_verifications v
                        WHERE v.pob_id = pa.id AND v.status = 'pending'
                        ORDER BY v.id DESC LIMIT 1
                    ) pv ON TRUE
                    {where} ORDER BY pa.id DESC LIMIT %s""",
                params + [100])
            for pid, st, amt, ts, uname, cname, chname, verification_id in tc.fetchall():
                recent.append({
                    "id": pid, "status": st, "pob_amount": float(amt or 0),
                    "created_at": ts, "user_name": uname, "campaign_name": cname,
                    "chemist_name": chname, "verification_id": verification_id,
                    "division_id": div["id"], "division_name": div["name"],
                    "division_code": div["code"],
                })
        except Exception as exc:
            unreachable.append({"division_id": div["id"], "name": div["name"],
                                "code": div["code"],
                                "error": str(exc).strip().split("\n")[0][:200]})
        finally:
            if tconn:
                try:
                    tconn.close()
                except Exception:
                    pass
    recent = sorted(recent, key=lambda r: r["created_at"] or "", reverse=True)[:limit]
    return {"counts": counts, "recent": recent, "unreachable": unreachable}


@router.get("/gratification")
def sa_all_gratification(status: str = "", limit: int = 200,
                         claims=Depends(require_superadmin)):
    """Cross-division gratification pipeline: per-status/per-type totals plus a
    unified recent list (optionally filtered by status)."""
    conn = platform_db.get_db()
    counts, types, recent, unreachable = {}, {}, [], []
    try:
        divisions = _provisioned_divisions(conn)
    finally:
        conn.close()
    where = "WHERE g.status=%s" if status else ""
    params = [status] if status else []
    for div in divisions:
        tconn = None
        try:
            tconn = _direct_tenant_conn(div["tenant_db_name"])
            tc = tconn.cursor()
            tc.execute("SELECT status, count(*) FROM gratifications GROUP BY status")
            for st, n in tc.fetchall():
                counts[st] = counts.get(st, 0) + int(n or 0)
            tc.execute("SELECT type_code, count(*) FROM gratifications GROUP BY type_code")
            for tcode, n in tc.fetchall():
                types[tcode] = types.get(tcode, 0) + int(n or 0)
            tc.execute(
                f"""SELECT g.id, g.status, g.type_code, g.scheme_value, g.created_at,
                           COALESCE(u.full_name, '—'), COALESCE(c.name, '—')
                    FROM gratifications g
                    LEFT JOIN users u ON u.id=g.user_id
                    LEFT JOIN campaigns c ON c.id=g.campaign_id
                    {where} ORDER BY g.id DESC LIMIT %s""",
                params + [100])
            for gid, st, tcode, val, ts, uname, cname in tc.fetchall():
                recent.append({
                    "id": gid, "status": st, "type_code": tcode,
                    "scheme_value": float(val or 0), "created_at": ts,
                    "user_name": uname, "campaign_name": cname,
                    "division_id": div["id"], "division_name": div["name"],
                    "division_code": div["code"],
                })
        except Exception as exc:
            unreachable.append({"division_id": div["id"], "name": div["name"],
                                "code": div["code"],
                                "error": str(exc).strip().split("\n")[0][:200]})
        finally:
            if tconn:
                try:
                    tconn.close()
                except Exception:
                    pass
    recent = sorted(recent, key=lambda r: r["created_at"] or "", reverse=True)[:limit]
    return {"counts": counts, "types": types, "recent": recent, "unreachable": unreachable}


# -- Owner finance view (money out / liability / cleared value) --

_FINANCE_TTL = 60
_finance_cache: dict = {"at": 0.0, "key": None, "data": None}

_PAYOUT_STAGES = ("eligible", "approved", "paid", "dispatched", "delivered", "completed")


@router.get("/finance")
def platform_finance(days: int = 0, claims=Depends(require_superadmin)):
    """Owner-finance aggregation across every division tenant.

    Money owed and money actually out come from the gratification pipeline
    (scheme_value in ₹); cleared claim value is the verified/approved POB
    amount (the value that has actually cleared verification); AI spend is
    the Gemini extraction cost. `days=0` means all time.
    """
    import time as _time
    from .. import config
    now = _time.time()
    if (_finance_cache["data"] is not None and _finance_cache["key"] == days
            and now - _finance_cache["at"] < _FINANCE_TTL):
        return _finance_cache["data"]

    conn = platform_db.get_db()
    try:
        divisions = _provisioned_divisions(conn)
    finally:
        conn.close()

    since_expr, since_params = "", []
    if days:
        since_expr = "created_at >= now() - make_interval(days => %s)"
        since_params = [int(days)]

    def _fwhere(extra: str = ""):
        conds = [c for c in (extra, since_expr) if c]
        return (("WHERE " + " AND ".join(conds)) if conds else ""), since_params

    payout_counts = {s: 0 for s in _PAYOUT_STAGES}
    payout_values = {s: 0.0 for s in _PAYOUT_STAGES}
    types: dict = {}
    verified_count, verified_value = 0, 0.0
    ai_calls, ai_cost = 0, 0.0
    monthly: dict = {}
    by_division: list = []
    unreachable: list = []
    currency = config.OCR_COST_CURRENCY or "USD"
    use_inr = currency.upper() == "INR"

    # AI extraction spend comes from the platform ai_usage_log (Batch 2): the
    # legacy per-tenant ocr_usage tables are vestigial under the merged path,
    # so the control-plane ledger is the single source of truth. We aggregate
    # per division + the monthly trend once, before the tenant loop below.
    ai_by_div: dict = {}
    ai_since = ("a.created_at >= now() - make_interval(days => %s)" if days else "")
    ai_since_params = [int(days)] if days else []
    ai_where = ("WHERE " + ai_since) if ai_since else ""
    platform_conn = platform_db.get_db()
    try:
        pc = platform_conn.cursor()
        pc.execute(
            f"""SELECT COALESCE(a.division_id, a.company_id, 0), COUNT(*),
                       COALESCE(SUM(a.cost_usd),0), COALESCE(SUM(a.cost_inr),0)
                FROM ai_usage_log a {ai_where} GROUP BY 1""",
            ai_since_params,
        )
        for fidx, cnt, cusd, cinr in pc.fetchall():
            ai_by_div[fidx] = (int(cnt or 0), float(cinr if use_inr else cusd or 0))
            ai_calls += int(cnt or 0)
            ai_cost += float(cinr if use_inr else cusd or 0)
        pc.execute(
            f"""SELECT to_char(a.created_at, 'YYYY-MM'), COUNT(*),
                       COALESCE(SUM(a.cost_usd),0), COALESCE(SUM(a.cost_inr),0)
                FROM ai_usage_log a {ai_where} GROUP BY 1""",
            ai_since_params,
        )
        for mth, cnt, cusd, cinr in pc.fetchall():
            ms = monthly.setdefault(mth, {"month": mth, "paid_out": 0.0,
                                          "cleared": 0.0, "ai_cost": 0.0})
            ms["ai_cost"] = round(ms["ai_cost"] + float(cinr if use_inr else cusd or 0), 2)
    finally:
        platform_conn.close()

    for div in divisions:
        tdb = div["tenant_db_name"]
        tconn = None
        try:
            from .. import migrations
            migrations.ensure_migrated(tdb)
            tconn = _direct_tenant_conn(tdb)
            tc = tconn.cursor()

            # Gratification pipeline: ₹ per status and per grant type.
            w, p = _fwhere()
            tc.execute(
                f"""SELECT COALESCE(status,'unknown'), count(*), coalesce(sum(scheme_value),0)
                    FROM gratifications {w} GROUP BY 1""", p)
            div_payout = {s: 0.0 for s in _PAYOUT_STAGES}
            for st, cnt, val in tc.fetchall():
                if st in div_payout:
                    payout_counts[st] += int(cnt or 0)
                    payout_values[st] += float(val or 0)
                    div_payout[st] += float(val or 0)
            tc.execute(
                f"""SELECT COALESCE(type_code,'other'), count(*), coalesce(sum(scheme_value),0)
                    FROM gratifications {w} GROUP BY 1""", p)
            for tcode, cnt, val in tc.fetchall():
                slot = types.setdefault(tcode, {"count": 0, "value": 0.0})
                slot["count"] += int(cnt or 0)
                slot["value"] = round(slot["value"] + float(val or 0), 2)

            # Cleared claim value: verified/approved POB amount.
            wv, pv = _fwhere("status IN ('verified','approved')")
            tc.execute(
                f"""SELECT count(*), coalesce(sum(pob_amount),0)
                    FROM pob_activities {wv}""", pv)
            vrow = tc.fetchone() or (0, 0)
            vcnt, vval = int(vrow[0] or 0), float(vrow[1] or 0)
            verified_count += vcnt
            verified_value += vval

            # AI extraction spend (from the platform ledger aggregated above).
            acnt, acost = ai_by_div.get(div["id"], (0, 0))

            # Monthly money-out trend (by pay/dispatch time where set).
            tc.execute(
                f"""SELECT to_char(COALESCE(paid_at, created_at),'YYYY-MM'), count(*),
                           coalesce(sum(scheme_value),0)
                    FROM gratifications {w} GROUP BY 1""", p)
            for mth, cnt, val in tc.fetchall():
                ms = monthly.setdefault(mth, {"month": mth, "paid_out": 0.0,
                                              "cleared": 0.0, "ai_cost": 0.0})
                ms["paid_out"] = round(ms["paid_out"] + float(val or 0), 2)

            # Monthly cleared value.
            tc.execute(
                f"""SELECT to_char(created_at,'YYYY-MM'), count(*), coalesce(sum(pob_amount),0)
                    FROM pob_activities {wv} GROUP BY 1""", pv)
            for mth, cnt, val in tc.fetchall():
                ms = monthly.setdefault(mth, {"month": mth, "paid_out": 0.0,
                                              "cleared": 0.0, "ai_cost": 0.0})
                ms["cleared"] = round(ms["cleared"] + float(val or 0), 2)

            by_division.append({
                "division_id": div["id"], "name": div["name"], "code": div["code"],
                "verified_count": vcnt, "verified_value": round(vval, 2),
                "liability": round(div_payout["eligible"] + div_payout["approved"], 2),
                "paid_out": round(sum(div_payout[s] for s in ("paid", "dispatched",
                                                              "delivered", "completed")), 2),
                "ai_cost": round(acost, 6),
            })
        except Exception as exc:
            unreachable.append({"division_id": div["id"], "name": div["name"],
                                "code": div["code"],
                                "error": str(exc).strip().split("\n")[0][:200]})
        finally:
            if tconn:
                try:
                    tconn.close()
                except Exception:
                    pass

    committed = round(payout_values["eligible"] + payout_values["approved"], 2)
    data = {
        "days": days,
        "currency": currency,
        "payout_counts": payout_counts,
        "payout_values": {s: round(v, 2) for s, v in payout_values.items()},
        "committed": committed,
        "paid_out": round(sum(payout_values[s] for s in ("paid", "dispatched",
                                                         "delivered", "completed")), 2),
        "types": types,
        "verified": {"count": verified_count, "value": round(verified_value, 2)},
        "ai": {"calls": ai_calls, "cost": round(ai_cost, 6)},
        "monthly": sorted(monthly.values(), key=lambda m: m["month"]),
        "by_division": sorted(by_division, key=lambda r: r["liability"], reverse=True),
        "unreachable": unreachable,
    }
    _finance_cache.update({"at": now, "key": days, "data": data})
    return data


# -- Campaign ROI (profit / loss per campaign, with driver reasons) --

_ROI_TTL = 120
_roi_cache: dict = {"at": 0.0, "key": None, "data": None}

_CLEARED_STATUSES = ("verified", "approved")
_PENDING_STATUSES = ("pending", "pending_verification", "submitted")
_LOST_STATUSES = ("rejected", "duplicate")
_PAID_STAGES = ("paid", "dispatched", "delivered", "completed")


def _campaign_roi_reason(c) -> dict:
    """Explain *why* a campaign looks profitable or loss-making, computed from
    its own numbers so the owner gets a driver, not just a number."""
    cleared = c["cleared_value"]
    cost = c["cost"]
    net = c["net"]
    if cleared <= 0:
        if cost <= 0:
            return {"code": "no_activity", "tone": "gray",
                    "reason": "No verified sales and no payouts yet — campaign hasn't produced value."}
        return {"code": "cost_no_value", "tone": "red",
                "reason": "Payouts were generated but no sales value cleared — rewards given with nothing sold yet."}
    if net <= 0:
        ratio = (cost / cleared) * 100 if cleared else 0
        if ratio >= 100:
            return {"code": "over_spend", "tone": "red",
                    "reason": f"Reward cost ({_inr(cost)}) exceeds cleared value ({_inr(cleared)}) — payouts outrun sales."}
        if ratio >= 60:
            return {"code": "heavy_rewards", "tone": "red",
                    "reason": f"Rewards consume {round(ratio)}% of cleared value — scheme is too generous for the sales it drives."}
        if c["pending_value"] > cleared:
            return {"code": "blocked_value", "tone": "amber",
                    "reason": f"More value ({_inr(c['pending_value'])}) is stuck pending verification than has cleared — unblock it first."}
        if c["rejected_value"] > cleared:
            return {"code": "rejected_sales", "tone": "red",
                    "reason": f"Rejected/duplicate submissions ({_inr(c['rejected_value'])}) outweigh cleared sales — invoice quality is the problem."}
        return {"code": "thin_margin", "tone": "amber",
                "reason": "Cleared value is close to reward cost — margin is too thin to cover the scheme."}
    # Profitable below.
    ratio = (cost / cleared) * 100 if cleared else 0
    if ratio <= 15:
        return {"code": "low_reward_cost", "tone": "green",
                "reason": f"Rewards are only {round(ratio)}% of cleared value — cheap incentives driving real sales."}
    if c["verified_ratio"] and c["verified_ratio"] >= 85:
        return {"code": "clean_verification", "tone": "green",
                "reason": f"{c['verified_ratio']}% of submissions cleared verification — healthy, well-documented sales."}
    if c["pob_value"] >= 100000:
        return {"code": "high_volume", "tone": "green",
                "reason": "High sales volume carries the campaign into profit despite modest per-unit margins."}
    return {"code": "solid", "tone": "green",
            "reason": "Revenue comfortably exceeds reward cost — a fundamentally sound campaign."}


def _inr(v):
    return "₹{:,.0f}".format(round(float(v or 0)))


@router.get("/campaigns/roi")
def sa_campaign_roi(days: int = 0, limit: int = 400,
                    claims=Depends(require_sa_roles("campaign_admin"))):
    """Cross-division campaign ROI.

    For every campaign across all provisioned tenants this computes the sales
    value that actually cleared (?) and the reward cost that was generated
    against it, then labels each campaign profit/loss/flat with the reason
    behind that outcome. `days` restricts to campaigns with activity in the
    window (their submissions/payouts filtered by created_at).
    """
    import time as _time
    now = _time.time()
    key = (days, limit)
    if (_roi_cache["data"] is not None and _roi_cache["key"] == key
            and now - _roi_cache["at"] < _ROI_TTL):
        return _roi_cache["data"]

    conn = platform_db.get_db()
    try:
        divisions = _provisioned_divisions(conn)
    finally:
        conn.close()

    params_days = [int(days)] if days else []
    params_days_x2 = params_days + params_days if days else []

    campaigns, unreachable = [], []
    summary = {"campaigns": 0, "profitable": 0, "loss_making": 0, "flat": 0,
               "total_cleared": 0.0, "total_cost": 0.0, "total_net": 0.0}

    for div in divisions:
        tdb = div["tenant_db_name"]
        tconn = None
        try:
            from .. import migrations
            migrations.ensure_migrated(tdb)
            tconn = _direct_tenant_conn(tdb)
            tc = tconn.cursor()
            tc.execute(
                f"""SELECT c.id, c.name, c.status, c.start_date, c.end_date,
                           COALESCE(b.name, '') AS brand_name,
                           count(pa.id) AS submissions,
                           count(pa.id) FILTER (WHERE pa.status IN
                               ('verified','approved')) AS verified,
                           count(pa.id) FILTER (WHERE pa.status IN
                               ('rejected','duplicate')) AS rejected,
                           coalesce(sum(pa.pob_amount),0) AS pob_value,
                           coalesce(sum(pa.pob_amount) FILTER (WHERE pa.status IN
                               ('verified','approved')),0) AS cleared_value,
                           coalesce(sum(pa.pob_amount) FILTER (WHERE pa.status IN
                               ('pending_verification','pending','submitted')),0) AS pending_value,
                           coalesce(sum(pa.pob_amount) FILTER (WHERE pa.status IN
                               ('rejected','duplicate')),0) AS rejected_value,
                           coalesce(sum(pa.pob_amount) FILTER (WHERE pa.status IN
                               ('paid','completed')),0) AS paid_value
                    FROM campaigns c
                    LEFT JOIN brands b ON b.id = c.brand_id
                    LEFT JOIN pob_activities pa ON pa.campaign_id = c.id
                    WHERE EXISTS (SELECT 1 FROM pob_activities pa2
                                  WHERE pa2.campaign_id = c.id
                                  {('AND pa2.created_at >= now() - make_interval(days => %s)' if days else '')})
                       OR EXISTS (SELECT 1 FROM gratifications g
                                  WHERE g.campaign_id = c.id
                                  {('AND g.created_at >= now() - make_interval(days => %s)' if days else '')})
                    GROUP BY c.id, b.name, c.name, c.status, c.start_date, c.end_date
                    ORDER BY cleared_value DESC, c.id""",
                params_days_x2,
            )
            rows = fetchall_dict(tc)

            # Reward cost per campaign: scheme value booked against it.
            tc.execute(
                f"""SELECT g.campaign_id, count(*) AS payouts,
                           coalesce(sum(g.scheme_value),0) AS cost,
                           coalesce(sum(g.scheme_value) FILTER (WHERE g.status IN
                               ('paid','dispatched','delivered','completed')),0) AS cost_paid
                    FROM gratifications g
                    WHERE EXISTS (SELECT 1 FROM campaigns c WHERE c.id = g.campaign_id)
                    {('AND g.created_at >= now() - make_interval(days => %s)' if days else '')}
                    GROUP BY g.campaign_id""",
                params_days if days else [],
            )
            costs = {r["campaign_id"]: r for r in fetchall_dict(tc)}

            for r in rows:
                cid = r["id"]
                cost_row = costs.get(cid, {})
                cost = float(cost_row.get("cost") or 0)
                cleared = float(r["cleared_value"] or 0)
                net = cleared - cost
                r.update({
                    "division_id": div["id"], "division_name": div["name"],
                    "division_code": div["code"],
                    "verified_ratio": round(float(r["verified"] or 0) / float(r["submissions"] or 1) * 100, 0)
                        if float(r["submissions"] or 0) else 0,
                    "cost": round(cost, 2), "cost_paid": round(float(cost_row.get("cost_paid") or 0), 2),
                    "payouts": int(cost_row.get("payouts") or 0),
                    "net": round(net, 2),
                    "roi_pct": round(net / cost * 100, 1) if cost else None,
                    "margin_pct": round(net / cleared * 100, 1) if cleared else None,
                })
                verdict = "flat"
                if net > 0:
                    verdict = "profit"
                elif net < 0:
                    verdict = "loss"
                r["verdict"] = verdict
                r.update(_campaign_roi_reason(r))
                campaigns.append(r)

                summary["campaigns"] += 1
                summary["total_cleared"] += cleared
                summary["total_cost"] += cost
                summary["total_net"] += net
                if verdict == "profit":
                    summary["profitable"] += 1
                elif verdict == "loss":
                    summary["loss_making"] += 1
                else:
                    summary["flat"] += 1
        except Exception as exc:
            unreachable.append({"division_id": div["id"], "name": div["name"],
                                "code": div["code"],
                                "error": str(exc).strip().split("\n")[0][:200]})
        finally:
            if tconn:
                try:
                    tconn.close()
                except Exception:
                    pass

    campaigns = campaigns[:limit]
    total_cleared = summary["total_cleared"]
    summary.update({
        "total_cleared": round(total_cleared, 2),
        "total_cost": round(summary["total_cost"], 2),
        "total_net": round(summary["total_net"], 2),
        "roi_pct": round(summary["total_net"] / summary["total_cost"] * 100, 1)
                   if summary["total_cost"] else None,
    })

    # Leaderboards for the owner: biggest winners, biggest losers.
    data = {
        "days": days,
        "summary": summary,
        "campaigns": campaigns,
        "top_winning": sorted([c for c in campaigns if c["net"] > 0],
                              key=lambda c: c["net"], reverse=True)[:8],
        "top_losing": sorted([c for c in campaigns if c["net"] < 0],
                             key=lambda c: c["net"])[:8],
        "unreachable": unreachable,
    }
    _roi_cache.update({"at": now, "key": key, "data": data})
    return data


@router.get("/users")
def sa_all_users(q: str = "", limit: int = 2000, claims=Depends(require_superadmin)):
    """Cross-division employee directory: every tenant user with its division,
    role, region, status and who they report to."""
    conn = platform_db.get_db()
    items, role_counts, status_counts, unreachable = [], {}, {}, []
    try:
        divisions = _provisioned_divisions(conn)
    finally:
        conn.close()
    for div in divisions:
        tconn = None
        try:
            tconn = _direct_tenant_conn(div["tenant_db_name"])
            tc = tconn.cursor()
            tc.execute(
                """SELECT u.id, u.username, u.full_name, COALESCE(r.name, '—'), u.status,
                          COALESCE(u.region, ''), COALESCE(u.email, ''), COALESCE(u.mobile, ''),
                          u.parent_id
                   FROM users u LEFT JOIN roles r ON r.id=u.role_id ORDER BY u.id""")
            for uid, uname, fname, role, st, region, email, mobile, parent_id in tc.fetchall():
                role_counts[role] = role_counts.get(role, 0) + 1
                status_counts[st] = status_counts.get(st, 0) + 1
                items.append({
                    "division_id": div["id"], "division_name": div["name"],
                    "division_code": div["code"],
                    "user_id": uid, "username": uname, "full_name": fname,
                    "role": role, "status": st, "region": region,
                    "email": email, "mobile": mobile, "parent_id": parent_id,
                })
        except Exception as exc:
            unreachable.append({"division_id": div["id"], "name": div["name"],
                                "code": div["code"],
                                "error": str(exc).strip().split("\n")[0][:200]})
        finally:
            if tconn:
                try:
                    tconn.close()
                except Exception:
                    pass
    if q:
        n = q.lower()
        items = [i for i in items if n in (i["username"] or "").lower()
                 or n in (i["full_name"] or "").lower()
                 or n in (i["email"] or "").lower()
                 or n in (i["mobile"] or "").lower()
                 or n in (i["region"] or "").lower()]
    items = items[:limit]
    return {"items": items, "role_counts": role_counts,
            "status_counts": status_counts, "unreachable": unreachable}


# -- Backups --

@router.get("/backups")
def list_backups(claims=Depends(require_superadmin)):
    from .. import backup
    return {"items": backup.list_backups()}


@router.post("/backup")
def run_backup(claims=Depends(require_superadmin)):
    from .. import backup
    summary = backup.run_backup_all()
    conn = platform_db.get_db()
    try:
        _audit(conn, claims, "backup.run", None, None,
               {"ok": summary["ok_count"], "fail": summary["fail_count"],
                "backup_root": summary["backup_root"]})
    finally:
        conn.close()
    return summary


# -- Platform settings (own branding: name + logo) --

@router.get("/platform-settings")
def sa_get_platform_settings(claims=Depends(require_superadmin)):
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT platform_name, logo_path FROM platform_settings WHERE id=1")
        row = c.fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "Platform settings not initialized")
    return {"platform_name": row[0], "has_logo": bool(row[1]),
            "logo_url": "/api/v1/auth/platform-logo" if row[1] else None}


@router.put("/platform-settings")
def sa_update_platform_settings(payload: dict, claims=Depends(require_superadmin)):
    name = (payload.get("platform_name") or "").strip()
    if not name or len(name) > 60:
        raise HTTPException(400, "Platform name must be 1–60 characters")
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute(
            """INSERT INTO platform_settings (id, platform_name)
               VALUES (1, %s)
               ON CONFLICT (id) DO UPDATE SET platform_name=EXCLUDED.platform_name,
                   updated_at=CURRENT_TIMESTAMP""",
            (name,))
        conn.commit()
        _audit(conn, claims, "platform.settings_update", "platform", 1,
               {"platform_name": name})
        return {"ok": True, "platform_name": name}
    finally:
        conn.close()


@router.post("/platform-settings/logo")
async def sa_upload_platform_logo(file: UploadFile = File(...),
                                  claims=Depends(require_superadmin)):
    data = await file.read()
    if len(data) > 2 * 1024 * 1024:
        raise HTTPException(400, "Logo must be under 2 MB")
    try:
        validate_upload(data, filename=file.filename or "", allowed_kinds=IMAGE_KINDS,
                        max_size=2 * 1024 * 1024)
    except UploadValidationError as exc:
        raise HTTPException(400, str(exc))
    rel = storage.save(data, "platform", "branding", file.filename or "logo")
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT logo_path FROM platform_settings WHERE id=1")
        row = c.fetchone()
        if row and row[0]:
            storage.delete(row[0])
        c.execute("""INSERT INTO platform_settings (id, logo_path)
                     VALUES (1, %s)
                     ON CONFLICT (id) DO UPDATE SET logo_path=EXCLUDED.logo_path,
                         updated_at=CURRENT_TIMESTAMP""", (rel,))
        conn.commit()
        _audit(conn, claims, "platform.logo_upload", "platform", 1, {"path": rel})
        return {"ok": True, "path": rel,
                "logo_url": "/api/v1/auth/platform-logo"}
    finally:
        conn.close()


@router.delete("/platform-settings/logo")
def sa_delete_platform_logo(claims=Depends(require_superadmin)):
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT logo_path FROM platform_settings WHERE id=1")
        row = c.fetchone()
        rel = row[0] if row else None
        if rel:
            storage.delete(rel)
        c.execute("UPDATE platform_settings SET logo_path=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=1")
        conn.commit()
        _audit(conn, claims, "platform.logo_removed", "platform", 1,
               {"removed": bool(rel)})
        return {"ok": True}
    finally:
        conn.close()


# -- Company profile (the single Company record) --

_COMPANY_PROFILE_COLS = ("legal_name", "display_name", "address", "city", "state",
                         "pincode", "gstin", "contact_number", "official_email", "website")


@router.get("/company-profile")
def sa_get_company_profile():
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT id, legal_name, display_name, code, address, city, state, "
            "pincode, gstin, contact_number, official_email, website "
            "FROM companies WHERE id=1")
        row = c.fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "Company not registered")
    cols = ("id", "legal_name", "display_name", "code", "address", "city",
            "state", "pincode", "gstin", "contact_number", "official_email",
            "website")
    return dict(zip(cols, row))


@router.put("/company-profile")
def sa_update_company_profile(body: dict, claims=Depends(require_superadmin)):
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT id FROM companies WHERE id=1")
        if not c.fetchone():
            raise HTTPException(404, "Company not registered")
        fields = list(_COMPANY_PROFILE_COLS)
        sets, params = [], []
        for f in fields:
            if f in body and body[f] is not None:
                sets.append(f"{f}=%s")
                params.append(str(body[f]).strip())
        if not sets:
            raise HTTPException(400, "Nothing to update")
        params.append(1)
        c.execute(f"UPDATE companies SET {', '.join(sets)}, updated_at=CURRENT_TIMESTAMP WHERE id=%s", params)
        conn.commit()
        _audit(conn, claims, "company.profile_update", "company", 1,
               {k: body.get(k) for k in fields if k in body})
        c.execute("SELECT id, legal_name, display_name, code, address, city, state, "
                  "pincode, gstin, contact_number, official_email, website "
                  "FROM companies WHERE id=1")
        row = c.fetchone()
        cols = ("id", "legal_name", "display_name", "code", "address", "city",
                "state", "pincode", "gstin", "contact_number", "official_email",
                "website")
        return dict(zip(cols, row))
    finally:
        conn.close()


# -- Platform admin (role) management — owner only --

PLATFORM_ROLES = (
    "full",
    "campaign_admin",
    "finance_admin",
    "verification_admin",
    "platform_division_admin",
)
# 'full' is a legacy unrestricted role kept only so existing accounts that
# already hold it keep working; the console no longer lets anyone assign it
# to a new or existing admin -- delegate one of the specialised roles instead.
_ASSIGNABLE_PLATFORM_ROLES = tuple(r for r in PLATFORM_ROLES if r != "full")


def _platform_admin_row(row):
    return {
        "id": row[0], "username": row[1], "full_name": row[2], "email": row[3],
        "status": row[4], "owner": bool(row[5]), "role": row[6],
        "created_at": row[7],
    }


@router.get("/platform-admins", dependencies=[Depends(require_owner)])
def list_platform_admins():
    """List every platform console account (owner + delegated admins)."""
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT id, username, full_name, email, status, owner_flag, role, created_at "
            "FROM super_admins ORDER BY id")
        return {"items": [_platform_admin_row(r) for r in c.fetchall()]}
    finally:
        conn.close()


@router.post("/platform-admins")
def create_platform_admin(body: dict, claims=Depends(require_owner)):
    username = str(body.get("username") or "").strip()
    password = body.get("password") or ""
    full_name = str(body.get("full_name") or "").strip()
    email = str(body.get("email") or "").strip()
    role = str(body.get("role") or "").strip()
    if not username or not full_name:
        raise HTTPException(400, "Username and full name are required")
    if len(password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    if role not in _ASSIGNABLE_PLATFORM_ROLES:
        raise HTTPException(400, "Invalid platform role" if role not in PLATFORM_ROLES else
                             "Full-access admins can no longer be created — assign a specialised role instead")
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT id FROM super_admins WHERE username=%s", (username,))
        if c.fetchone():
            raise HTTPException(409, f"Username '{username}' is already taken")
        c.execute(
            "INSERT INTO super_admins (username, password, full_name, email, status, role, owner_flag, company_id) "
            "VALUES (%s,%s,%s,%s,'active',%s,FALSE,1) RETURNING id",
            (username, hash_pw(password), full_name, email, role))
        aid = c.fetchone()[0]
        conn.commit()
        _audit(conn, claims, "platform_admin.create", "platform_admin", aid,
               {"username": username, "role": role})
        return {"ok": True, "id": aid}
    finally:
        conn.close()


@router.put("/platform-admins/{aid}", dependencies=[Depends(require_owner)])
def update_platform_admin(aid: int, body: dict, request: Request = None,
                          claims=Depends(require_owner)):
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT id, username, full_name, email, status, owner_flag, role, created_at "
                  "FROM super_admins WHERE id=%s", (aid,))
        row = c.fetchone()
        if not row:
            raise HTTPException(404, "Platform admin not found")
        target = _platform_admin_row(row)
        if target["owner"]:
            raise HTTPException(403, "The company owner account cannot be edited")
        role = str(body.get("role") or target["role"]).strip()
        if role not in PLATFORM_ROLES:
            raise HTTPException(400, "Invalid platform role")
        if role == "full" and target["role"] != "full":
            raise HTTPException(400, "Full-access admins can no longer be assigned — choose a specialised role instead")
        status = str(body.get("status") or target["status"]).strip()
        if status not in ("active", "suspended"):
            raise HTTPException(400, "Invalid status")
        full_name = str(body.get("full_name") or target["full_name"]).strip()
        email = str(body.get("email") or target["email"]).strip()
        password = body.get("password")
        if password is not None and str(password) and len(str(password)) < 6:
            raise HTTPException(400, "Password must be at least 6 characters")
        if password:
            c.execute("UPDATE super_admins SET password=%s WHERE id=%s", (hash_pw(password), aid))
        c.execute("UPDATE super_admins SET role=%s, status=%s, full_name=%s, email=%s WHERE id=%s",
                  (role, status, full_name, email, aid))
        conn.commit()
        _audit(conn, claims, "platform_admin.update", "super_admin", aid,
               {"role": role, "status": status, "full_name": full_name})
        return {"ok": True, "id": aid, "role": role, "status": status}
    finally:
        conn.close()


# -- Cross-division financial approvals (finance_admin) --

def _platform_ctx(tconn, claims, tenant_db):
    """TenantContext-shaped object for a platform admin acting inside a
    division: unrestricted scope (role_name in GLOBAL_ROLES), all perms.
    The synthetic user has no tenant id because the platform admin is not a
    row in the division's users table; tenant-side FK columns (verifier_id,
    actor_id) stay NULL, and the real actor is recorded in the platform
    audit log via _audit()."""
    from ..deps import TenantContext
    return TenantContext(
        conn=tconn,
        user={"id": None, "username": claims.get("username"),
              "role_name": "campaignos_admin", "status": "active",
              "full_name": claims.get("full_name")},
        perms={
            "gratification.approve", "gratification.pay", "gratification.manage",
            "gratification.view", "verification.approve", "verification.reject",
            "verification.view", "pob.view",
        },
        claims={"tenant_db": tenant_db},
    )


@router.post("/divisions/{did}/gratification/{gid}/approve")
def sa_approve_gratification(did: int, gid: int, body: dict = None, request: Request = None,
                             claims=Depends(require_sa_roles("finance_admin"))):
    """Cross-division cashback/UPI approval performed by a finance admin."""
    from . import gratification as grat_router
    body = body or {}
    conn = platform_db.get_db()
    tconn = None
    try:
        tenant_db = _tenant_db_name(conn, did)
        tconn = _tenant_conn_for(conn, did)
        ctx = _platform_ctx(tconn, claims, tenant_db)
        resp = grat_router.approve_cashback(gid, body, ctx=ctx)
        _audit(conn, claims, "gratification.approve", "gratification", gid,
               {"division_id": did, "via": "platform_finance_admin"})
        return resp
    finally:
        if tconn:
            tconn.close()
        conn.close()


@router.post("/divisions/{did}/gratification/{gid}/pay")
def sa_pay_gratification(did: int, gid: int, body: dict = None, request: Request = None,
                         claims=Depends(require_sa_roles("finance_admin"))):
    """Cross-division cashback/UPI payment performed by a finance admin."""
    from . import gratification as grat_router
    body = body or {}
    conn = platform_db.get_db()
    tconn = None
    try:
        tenant_db = _tenant_db_name(conn, did)
        tconn = _tenant_conn_for(conn, did)
        ctx = _platform_ctx(tconn, claims, tenant_db)
        resp = grat_router.pay_cashback(gid, body, ctx=ctx)
        _audit(conn, claims, "gratification.pay", "gratification", gid,
               {"division_id": did, "via": "platform_finance_admin"})
        return resp
    finally:
        if tconn:
            tconn.close()
        conn.close()


# -- Cross-division verification approvals (verification_admin) --

@router.post("/divisions/{did}/verification/{vid}/approve")
def sa_approve_verification(did: int, vid: int, body: dict = None, request: Request = None,
                            claims=Depends(require_sa_roles("verification_admin"))):
    from . import verification as verif_router
    body = body or {}
    conn = platform_db.get_db()
    tconn = None
    try:
        tenant_db = _tenant_db_name(conn, did)
        tconn = _tenant_conn_for(conn, did)
        ctx = _platform_ctx(tconn, claims, tenant_db)
        resp = verif_router.approve_verification(vid, body, request, ctx=ctx)
        _audit(conn, claims, "verification.approve", "pob_verification", vid,
               {"division_id": did, "via": "platform_verification_admin"})
        return resp
    finally:
        if tconn:
            tconn.close()
        conn.close()


@router.post("/divisions/{did}/verification/{vid}/reject")
def sa_reject_verification(did: int, vid: int, body: dict = None, request: Request = None,
                           claims=Depends(require_sa_roles("verification_admin"))):
    from . import verification as verif_router
    body = body or {}
    conn = platform_db.get_db()
    tconn = None
    try:
        tenant_db = _tenant_db_name(conn, did)
        tconn = _tenant_conn_for(conn, did)
        ctx = _platform_ctx(tconn, claims, tenant_db)
        resp = verif_router.reject_verification(vid, body, request, ctx=ctx)
        _audit(conn, claims, "verification.reject", "pob_verification", vid,
               {"division_id": did, "via": "platform_verification_admin"})
        return resp
    finally:
        if tconn:
            tconn.close()
        conn.close()


@router.post("/divisions/{did}/verification/{vid}/duplicate")
def sa_mark_duplicate(did: int, vid: int, body: dict = None, request: Request = None,
                      claims=Depends(require_sa_roles("verification_admin"))):
    from . import verification as verif_router
    body = body or {}
    conn = platform_db.get_db()
    tconn = None
    try:
        tenant_db = _tenant_db_name(conn, did)
        tconn = _tenant_conn_for(conn, did)
        ctx = _platform_ctx(tconn, claims, tenant_db)
        resp = verif_router.mark_duplicate(vid, body, ctx=ctx)
        _audit(conn, claims, "verification.mark_duplicate", "pob_verification", vid,
               {"division_id": did, "via": "platform_verification_admin"})
        return resp
    finally:
        if tconn:
            tconn.close()
        conn.close()


# -- Platform notification feed + queue badges --

_QUEUE_STATUS = {
    "campaign": "pending_approval",
    "pob": "pending_verification",
    "gratification": "eligible",
}


@router.get("/queue-counts")
def sa_queue_counts(claims=Depends(require_superadmin)):
    """Light per-division pending counts for the console sidebar badges:
    campaigns awaiting approval, POBs pending verification, gratifications
    eligible for payout. Returns an aggregate histogram of each one."""
    conn = platform_db.get_db()
    schema = {k: {} for k in _QUEUE_STATUS}
    try:
        divisions = _provisioned_divisions(conn)
    finally:
        conn.close()
    for div in divisions:
        tconn = None
        try:
            tconn = _direct_tenant_conn(div["tenant_db_name"])
            for key, st in _QUEUE_STATUS.items():
                table = {"campaign": "campaigns", "pob": "pob_activities",
                         "gratification": "gratifications"}[key]
                tc = tconn.cursor()
                tc.execute(f"SELECT status, count(*) FROM {table} GROUP BY status")
                for s, n in tc.fetchall():
                    if s in schema[key]:
                        schema[key][s] += int(n or 0)
                    else:
                        schema[key][s] = int(n or 0)
        except Exception as exc:
            pass
        finally:
            if tconn:
                try:
                    tconn.close()
                except Exception:
                    pass
    return schema


@router.get("/notifications")
def sa_list_notifications(limit: int = PageLimit(default=50),
                          claims=Depends(require_superadmin)):
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute("""SELECT * FROM platform_notifications
                     WHERE super_admin_id=%s
                     ORDER BY id DESC LIMIT %s""", (claims.get("sub"), limit))
        items = fetchall_dict(c)
        c.execute("""SELECT count(*) FROM platform_notifications
                     WHERE super_admin_id=%s AND is_read=FALSE""", (claims.get("sub"),))
        unread = c.fetchone()[0]
    finally:
        conn.close()
    return {"items": items, "unread": unread}


@router.get("/notifications/unread-count")
def sa_notification_unread(claims=Depends(require_superadmin)):
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute("""SELECT count(*) FROM platform_notifications
                     WHERE super_admin_id=%s AND is_read=FALSE""", (claims.get("sub"),))
        return {"unread": c.fetchone()[0]}
    finally:
        conn.close()


@router.post("/notifications/{nid}/read")
def sa_mark_notification_read(nid: int, claims=Depends(require_superadmin)):
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute("""UPDATE platform_notifications SET is_read=TRUE
                     WHERE id=%s AND super_admin_id=%s""", (nid, claims.get("sub")))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@router.post("/notifications/read-all")
def sa_mark_notifications_read_all(claims=Depends(require_superadmin)):
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute("""UPDATE platform_notifications SET is_read=TRUE
                     WHERE super_admin_id=%s AND is_read=FALSE""", (claims.get("sub"),))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()
