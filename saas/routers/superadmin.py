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
from ..db_utils import fetchall_dict, fetchone_dict
from ..deps import require_owner, require_sa_path, require_sa_roles, require_superadmin
from ..upload_validation import IMAGE_KINDS, UploadValidationError, validate_upload
from ..pagination import PageLimit, PageOffset
from app.security import hash_pw

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
            tconn.commit()
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
    """pending_approval -> scheduled. The campaign becomes executable once its
    start window opens; the daily sweep flips scheduled -> active."""
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
        c.execute("UPDATE campaigns SET status='scheduled', approved_at=CURRENT_TIMESTAMP, "
                  "approved_by=%s, rejected_at=NULL, rejected_by=NULL, rejection_note=NULL "
                  "WHERE id=%s", (claims.get("sub"), cid))
        tconn.commit()
        from ..notify import notify_admins
        notify_admins(tconn, "campaign.approved", "Campaign approved",
                      f"'{row[2]}' was approved and scheduled.", "campaign", cid)
        return {"ok": True, "status": "scheduled"}
    finally:
        if tconn:
            tconn.close()
        conn.close()


@router.post("/divisions/{did}/campaigns/{cid}/reject")
def sa_reject_campaign(did: int, cid: int, body: dict = None, request: Request = None,
                       claims=Depends(require_superadmin)):
    """pending_approval -> draft with a rejection reason the submitter can see."""
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


def _costing_where(days: int, model: str):
    """Return (sql, params) for the ocr_usage WHERE clause shared by every
    costing aggregate. `model` matches the model that served the call."""
    sql, params = [], []
    if days:
        sql.append("created_at >= now() - make_interval(days => %s)")
        params.append(int(days))
    if model:
        sql.append("model_name = %s")
        params.append(model)
    return ("WHERE " + " AND ".join(sql)) if sql else "", params


@router.get("/costing")
def platform_costing(days: int = 0, division_id: int = 0, model: str = "",
                     claims=Depends(require_superadmin)):
    """Aggregate Gemini invoice-extraction spend across every division tenant.

    Reports input/output tokens, cost and model used -- summarised platform-wide,
    per division, per model and per user, plus the newest extraction calls.
    `days=0` means all time; `division_id` / `model` narrow the view.
    """
    import time as _time
    from .. import config
    now = _time.time()
    key = (days, division_id, model)
    if (_costing_cache["data"] is not None and _costing_cache["key"] == key
            and now - _costing_cache["at"] < _COSTING_TTL):
        return _costing_cache["data"]

    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        if division_id:
            c.execute("SELECT id, name, code, status, tenant_db_name FROM divisions WHERE id=%s",
                      (division_id,))
        else:
            c.execute("""SELECT id, name, code, status, tenant_db_name
                         FROM divisions WHERE tenant_db_name IS NOT NULL ORDER BY id""")
        divisions = fetchall_dict(c)
    finally:
        conn.close()

    where_sql, where_params = _costing_where(days, model)

    summary = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost": 0.0,
               "reported_divisions": 0, "unreachable_divisions": 0}
    by_division: dict = {}
    by_model: dict = {}
    by_user: dict = {}
    monthly: dict = {}
    recent: list = []
    unreachable: list = []
    currency = config.OCR_COST_CURRENCY or "USD"

    for div in divisions:
        tdb = div.get("tenant_db_name")
        if not tdb:
            continue
        tconn = None
        try:
            from .. import migrations
            migrations.ensure_migrated(tdb)
            tconn = _direct_tenant_conn(tdb)
            tc = tconn.cursor()

            # ── Division totals ───────────────────────────────────────────
            tc.execute(
                f"""SELECT count(*), coalesce(sum(input_tokens),0),
                           coalesce(sum(output_tokens),0), coalesce(sum(cost),0)
                    FROM ocr_usage {where_sql}""",
                where_params,
            )
            row = tc.fetchone() or (0, 0, 0, 0)
            calls = int(row[0] or 0)
            itok = int(row[1] or 0)
            otok = int(row[2] or 0)
            cost = float(row[3] or 0)
            summary["calls"] += calls
            summary["input_tokens"] += itok
            summary["output_tokens"] += otok
            summary["cost"] += cost
            summary["reported_divisions"] += 1
            div_id = div["id"]
            drec = {
                "division_id": div_id, "name": div["name"], "code": div["code"],
                "calls": calls, "input_tokens": itok, "output_tokens": otok,
                "cost": round(cost, 6), "models": [],
            }
            by_division[div_id] = drec

            # ── Per model ─────────────────────────────────────────────────
            tc.execute(
                f"""SELECT COALESCE(model_name, '(text)') m,
                           count(*), coalesce(sum(input_tokens),0),
                           coalesce(sum(output_tokens),0), coalesce(sum(cost),0)
                    FROM ocr_usage {where_sql} GROUP BY 1""",
                where_params,
            )
            for m, cnt, it, ot, cst in tc.fetchall():
                model = m or "(text)"
                slot = by_model.setdefault(model, {"model": model, "calls": 0,
                                                   "input_tokens": 0, "output_tokens": 0, "cost": 0.0})
                slot["calls"] += int(cnt or 0)
                slot["input_tokens"] += int(it or 0)
                slot["output_tokens"] += int(ot or 0)
                slot["cost"] = round(slot["cost"] + float(cst or 0), 6)
                drec["models"].append({"model": model, "calls": int(cnt or 0),
                                       "input_tokens": int(it or 0),
                                       "output_tokens": int(ot or 0),
                                       "cost": round(float(cst or 0), 6)})

            # ── Per user (joined across the tenant's ocr_usage) ───────────
            tc.execute(
                f"""SELECT COALESCE(u.id, 0) uid, COALESCE(u.username, '(deleted)'),
                           COALESCE(u.full_name, '—') fname, r.name rn,
                           count(*) calls, coalesce(sum(o.input_tokens),0),
                           coalesce(sum(o.output_tokens),0), coalesce(sum(o.cost),0)
                    FROM ocr_usage o
                    LEFT JOIN users u ON u.id = o.user_id
                    LEFT JOIN roles r ON r.id = u.role_id
                    {where_sql} GROUP BY 1, 2, 3, 4""",
                where_params,
            )
            for uid, uname, fname, rn, cnt, it, ot, cst in tc.fetchall():
                ukey = f"{div_id}:{uid}"
                uslot = by_user.setdefault(ukey, {
                    "division_id": div_id, "division_name": div["name"],
                    "division_code": div["code"], "user_id": uid,
                    "username": uname, "full_name": fname, "role": rn or "—",
                    "calls": 0, "input_tokens": 0, "output_tokens": 0, "cost": 0.0,
                })
                uslot["calls"] += int(cnt or 0)
                uslot["input_tokens"] += int(it or 0)
                uslot["output_tokens"] += int(ot or 0)
                uslot["cost"] = round(uslot["cost"] + float(cst or 0), 6)

            # ── Monthly trend for the spend chart ─────────────────────────
            tc.execute(
                f"""SELECT to_char(created_at, 'YYYY-MM') m, count(*),
                           coalesce(sum(input_tokens),0), coalesce(sum(output_tokens),0),
                           coalesce(sum(cost),0)
                    FROM ocr_usage {where_sql} GROUP BY 1""",
                where_params,
            )
            for mth, cnt, it, ot, cst in tc.fetchall():
                ms = monthly.setdefault(mth, {"month": mth, "calls": 0,
                                              "input_tokens": 0, "output_tokens": 0, "cost": 0.0})
                ms["calls"] += int(cnt or 0)
                ms["input_tokens"] += int(it or 0)
                ms["output_tokens"] += int(ot or 0)
                ms["cost"] = round(ms["cost"] + float(cst or 0), 6)

            # ── Newest calls (detail table) ───────────────────────────────
            tc.execute(
                f"""SELECT o.id, COALESCE(u.full_name, '—'), COALESCE(u.username, ''),
                           o.engine, o.model_name, o.input_tokens, o.output_tokens,
                           o.cost, o.currency, o.status, o.invoice_number, o.filename,
                           to_char(o.created_at, 'YYYY-MM-DD HH24:MI:SS')
                    FROM ocr_usage o
                    LEFT JOIN users u ON u.id = o.user_id
                    {where_sql} ORDER BY o.created_at DESC, o.id DESC LIMIT %s""",
                where_params + [200],
            )
            for _id, fname, uname, engine, mdl, it, ot, cst, cur, st, inv, fn, ts in tc.fetchall():
                recent.append({
                    "id": _id, "division_id": div_id, "division_name": div["name"],
                    "division_code": div["code"], "full_name": fname, "username": uname,
                    "engine": engine, "model": mdl, "input_tokens": int(it or 0),
                    "output_tokens": int(ot or 0), "cost": round(float(cst or 0), 6),
                    "currency": cur, "status": st, "invoice_number": inv, "filename": fn,
                    "created_at": ts,
                })
        except Exception as exc:
            unreachable.append({"division_id": div["id"], "name": div["name"],
                                "code": div["code"], "error": str(exc).strip().split("\n")[0][:200]})
        finally:
            if tconn:
                try:
                    tconn.close()
                except Exception:
                    pass

    by_model_list = [{"model": m["model"], "calls": m["calls"],
                      "input_tokens": m["input_tokens"], "output_tokens": m["output_tokens"],
                      "cost": round(m["cost"], 6)}
                     for m in sorted(by_model.values(), key=lambda x: x["cost"], reverse=True)]
    by_user_list = sorted(by_user.values(), key=lambda x: x["cost"], reverse=True)[:500]
    div_list = sorted(by_division.values(), key=lambda x: x["cost"], reverse=True)
    summary["cost"] = round(summary["cost"], 6)
    summary["avg_cost"] = round(summary["cost"] / summary["calls"], 6) if summary["calls"] else 0.0

    data = {
        "days": days,
        "currency": currency,
        "summary": summary,
        "by_division": div_list,
        "by_model": by_model_list,
        "by_user": by_user_list,
        "monthly": sorted(monthly.values(), key=lambda m: m["month"]),
        "recent": recent,
        "unreachable": unreachable,
    }
    _costing_cache.update({"at": now, "key": key, "data": data})
    return data


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
)


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


@router.post("/platform-admins", dependencies=[Depends(require_owner)])
def create_platform_admin(body: dict):
    username = str(body.get("username") or "").strip()
    password = body.get("password") or ""
    full_name = str(body.get("full_name") or "").strip()
    email = str(body.get("email") or "").strip()
    role = str(body.get("role") or "full").strip()
    if not username or not full_name:
        raise HTTPException(400, "Username and full name are required")
    if len(password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    if role not in ("full", "campaign_admin", "finance_admin", "verification_admin"):
        raise HTTPException(400, "Invalid platform role")
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
        if role not in ("full", "campaign_admin", "finance_admin", "verification_admin"):
            raise HTTPException(400, "Invalid platform role")
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
