"""
Masters router (tenant, READ-ONLY for campaign structure).

Campaigns, divisions and brands are managed by the super admin from the
platform console (see saas/routers/superadmin.py + saas/campaign_service.py).
Tenant users only read them -- plus the per-campaign POB tracking view --
so they can execute POB work against them.

Chemist management remains a tenant capability (field teams own the retail
network), and a read-only product list is exposed for POB submission flows.
"""
import io

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from openpyxl import Workbook, load_workbook

from .. import campaign_service
from .. import config
from .. import storage
from ..audit import log_action
from ..db_utils import fetchall_dict, fetchone_dict
from ..deps import TenantContext, require_permission
from ..notify import notify_admins
from ..scoping import division_scope, user_division_id, visible_user_ids
from ..upi import mask_upi_id
from ..upload_validation import IMAGE_KINDS, SPREADSHEET_KINDS, UploadValidationError, validate_upload
from ..pagination import PageLimit, PageOffset

router = APIRouter(prefix="/api/v1", tags=["masters"])


# ── Brands (read-only) ──────────────────────────────────────────────────────

@router.get("/brands")
def list_brands(q: str = "", status: str = "",
                ctx: TenantContext = Depends(require_permission("brand.view"))):
    """Brands visible to the caller, narrowed to their division when they have
    one. Brands with no division stay visible so unmapped data still works."""
    div = user_division_id(ctx.conn, ctx)
    items = campaign_service.list_brands(ctx.conn, q, status)
    if div:
        items = [b for b in items if b.get("division_id") in (None, div)]
    return {"items": items}


def _actor(ctx: TenantContext) -> dict:
    u = ctx.user or {}
    return {"id": u.get("id"), "name": u.get("full_name") or u.get("username")}


def _assert_brand_in_div(conn, bid: int, div: int) -> None:
    c = conn.cursor()
    c.execute("SELECT id FROM brands WHERE id=%s AND (division_id=%s OR division_id IS NULL)", (bid, div))
    if not c.fetchone():
        raise HTTPException(404, "brand not found")


def _assert_campaign_in_div(conn, cid: int, div: int) -> None:
    """A campaign belongs to a division directly, or through the brand it
    promotes (division -> brands -> campaigns). Mirrors list_campaigns."""
    c = conn.cursor()
    c.execute(
        "SELECT c.id FROM campaigns c LEFT JOIN brands b ON b.id=c.brand_id "
        "WHERE c.id=%s AND (c.division_id=%s OR b.division_id=%s)",
        (cid, div, div),
    )
    if not c.fetchone():
        raise HTTPException(404, "campaign not found")


@router.post("/brands")
def create_brand(body: dict, ctx: TenantContext = Depends(require_permission("brand.manage"))):
    div = division_scope(ctx.conn, ctx)
    if div:
        body = {**body, "division_id": div}
    bid = campaign_service.create_brand(ctx.conn, _actor(ctx), body)
    return {"ok": True, "id": bid}


@router.put("/brands/{bid}")
def update_brand(bid: int, body: dict, ctx: TenantContext = Depends(require_permission("brand.manage"))):
    div = division_scope(ctx.conn, ctx)
    if div:
        _assert_brand_in_div(ctx.conn, bid, div)
        body = {**body, "division_id": div}
    campaign_service.update_brand(ctx.conn, _actor(ctx), bid, body)
    return {"ok": True}


@router.delete("/brands/{bid}")
def delete_brand(bid: int, ctx: TenantContext = Depends(require_permission("brand.manage"))):
    div = division_scope(ctx.conn, ctx)
    if div:
        _assert_brand_in_div(ctx.conn, bid, div)
    campaign_service.delete_brand(ctx.conn, _actor(ctx), bid)
    return {"ok": True}


# ── Divisions (read-only) ───────────────────────────────────────────────────

@router.get("/divisions")
def list_divisions(q: str = "", status: str = "",
                   ctx: TenantContext = Depends(require_permission("campaign.view"))):
    """Divisions visible to the caller.

    A user bound to a division sees only that division -- otherwise an Alpha
    rep could enumerate every other division in the company, which defeats the
    division-wise separation the login links are built around.
    """
    items = campaign_service.list_divisions(ctx.conn, q, status)
    div = user_division_id(ctx.conn, ctx)
    if div:
        items = [d for d in items if d.get("id") == div]
    return {"items": items}


@router.post("/divisions")
def create_division(body: dict, ctx: TenantContext = Depends(require_permission("brand.manage"))):
    if division_scope(ctx.conn, ctx):
        raise HTTPException(403, "You can only manage your own division")
    did = campaign_service.create_division(ctx.conn, _actor(ctx), body)
    return {"ok": True, "id": did}


@router.put("/divisions/{did}")
def update_division(did: int, body: dict, ctx: TenantContext = Depends(require_permission("brand.manage"))):
    div = division_scope(ctx.conn, ctx)
    if div and div != did:
        raise HTTPException(404, "division not found")
    campaign_service.update_division(ctx.conn, _actor(ctx), did, body)
    return {"ok": True}


@router.delete("/divisions/{did}")
def delete_division(did: int, ctx: TenantContext = Depends(require_permission("brand.manage"))):
    div = division_scope(ctx.conn, ctx)
    if div and div != did:
        raise HTTPException(404, "division not found")
    campaign_service.delete_division(ctx.conn, _actor(ctx), did)
    return {"ok": True}


# ── Campaigns ───────────────────────────────────────────────────────────────

@router.get("/campaigns/readiness")
def campaign_readiness(ctx: TenantContext = Depends(require_permission("campaign.view"))):
    return campaign_service.campaign_readiness(ctx.conn)


@router.get("/campaigns")
def list_campaigns(q: str = "", status: str = "", active: bool = None, brand_id: int = None,
                   division_id: int = None,
                   ctx: TenantContext = Depends(require_permission("campaign.view"))):
    div = user_division_id(ctx.conn, ctx)
    if div:
        division_id = div  # scope to user's division
    return {"items": campaign_service.list_campaigns(ctx.conn, q, status, active, brand_id, division_id)}


@router.get("/campaigns/tracking")
def campaign_tracking(ctx: TenantContext = Depends(require_permission("campaign.view"))):
    """Divisions -> campaigns -> team hierarchy with per-member POB stats.

    Members are scoped by the caller's reporting tree (see saas/scoping.py):
    MR/PSR see themselves, managers see everyone reporting to them, division
    admins see their division, and CampaignOS admin / HO see the whole company.
    """
    conn = ctx.conn
    c = conn.cursor()
    visible = visible_user_ids(conn, ctx)
    div = division_scope(conn, ctx)

    user_sql = """SELECT u.id, u.full_name, u.parent_id, u.region, u.state,
                         h.name AS level_name, h.rank, r.name AS role_name
                  FROM users u
                  LEFT JOIN hierarchy_levels h ON h.id=u.hierarchy_level_id
                  LEFT JOIN roles r ON r.id=u.role_id
                  WHERE u.status='active'"""
    params = []
    if visible is not None:
        user_sql += " AND u.id = ANY(%s)"
        params.append(visible)
    if div:
        user_sql += " AND u.division_id=%s"
        params.append(div)
    user_sql += " ORDER BY h.rank DESC NULLS LAST, u.full_name"
    c.execute(user_sql, params)
    users = fetchall_dict(c)

    campaign_sql = """SELECT c.*, b.name AS brand_name, d.name AS division_name
                 FROM campaigns c
                 LEFT JOIN brands b ON b.id=c.brand_id
                 LEFT JOIN divisions d ON d.id=c.division_id"""
    params = []
    if div:
        campaign_sql += " WHERE c.division_id=%s"
        params.append(div)
    campaign_sql += " ORDER BY c.id DESC"
    c.execute(campaign_sql, params)
    campaigns = fetchall_dict(c)

    from .. import campaign_service
    for cm in campaigns:
        cm["brand_ids"] = campaign_service._brand_ids_list(cm)
        cm["brand_names"] = campaign_service._brand_names(conn, cm["brand_ids"])

    c.execute("""SELECT pa.campaign_id, pa.user_id, count(*) AS pobs,
                 coalesce(sum(pa.pob_amount),0) AS amount,
                 sum(CASE WHEN pa.status='verified' THEN 1 ELSE 0 END) AS verified,
                 sum(CASE WHEN pa.status='pending_verification' THEN 1 ELSE 0 END) AS pending,
                 sum(CASE WHEN pa.status='rejected' THEN 1 ELSE 0 END) AS rejected,
                 sum(CASE WHEN pa.status IN ('duplicate','needs_review') THEN 1 ELSE 0 END) AS flagged
                 FROM pob_activities pa GROUP BY pa.campaign_id, pa.user_id""")
    stats = {}
    for cid, uid, pobs, amount, verified, pending, rejected, flagged in c.fetchall():
        stats.setdefault(cid, {})[uid] = {
            "pobs": pobs, "amount": amount, "verified": verified,
            "pending": pending, "rejected": rejected, "flagged": flagged,
        }

    divs = {}
    c.execute("SELECT * FROM divisions WHERE status='active' ORDER BY name")
    for d in fetchall_dict(c):
        divs[d["id"]] = {"division": {"id": d["id"], "name": d["name"]}, "campaigns": []}
    unassigned = {"division": {"id": None, "name": "Unassigned"}, "campaigns": []}

    for cm in campaigns:
        members = []
        for u in users:
            s = stats.get(cm["id"], {}).get(u["id"], {})
            members.append({**u, **s})
        node = {**cm, "members": members}
        did = cm.get("division_id")
        if did and did in divs:
            divs[did]["campaigns"].append(node)
        else:
            unassigned["campaigns"].append(node)

    groups = [g for g in divs.values() if g["campaigns"]]
    if unassigned["campaigns"]:
        groups.append(unassigned)
    return {"items": groups, "users_total": len(users), "campaign_total": len(campaigns)}


@router.get("/campaigns/{cid}")
def get_campaign(cid: int, ctx: TenantContext = Depends(require_permission("campaign.view"))):
    row = campaign_service.get_campaign(ctx.conn, cid)
    if not row:
        raise HTTPException(404, "campaign not found")
    div = division_scope(ctx.conn, ctx)
    if div and row.get("division_id") != div:
        raise HTTPException(404, "campaign not found")
    return row


@router.post("/campaigns")
def create_campaign(body: dict, request: Request = None,
                    ctx: TenantContext = Depends(require_permission("campaign.manage"))):
    readiness = campaign_service.campaign_readiness(ctx.conn)
    if not readiness["ready"]:
        missing = ", ".join(i["label"] for i in readiness["items"]
                            if i["key"] in ("brands", "chemists", "gifts") and not i["ready"])
        raise HTTPException(
            409,
            "Set up configuration before creating a campaign. Still missing: " + (missing or "—"),
        )
    div = division_scope(ctx.conn, ctx)
    if div:
        body = {**body, "division_id": div}
    cid = campaign_service.create_campaign(ctx.conn, _actor(ctx), body)
    notify_admins(ctx.conn, "campaign.created", "New campaign created",
                  f"A new campaign '{body.get('name') or ''}' was created.", "campaign", cid)
    return {"ok": True, "id": cid}


@router.put("/campaigns/{cid}")
def update_campaign(cid: int, body: dict, request: Request = None,
                    ctx: TenantContext = Depends(require_permission("campaign.manage"))):
    div = division_scope(ctx.conn, ctx)
    if div:
        _assert_campaign_in_div(ctx.conn, cid, div)
    campaign_service.update_campaign(ctx.conn, _actor(ctx), cid, body, request=request)
    notify_admins(ctx.conn, "campaign.updated", "Campaign updated",
                  f"Campaign #{cid} was updated.", "campaign", cid)
    return {"ok": True}


@router.get("/campaigns/{cid}/assignment")
def get_campaign_assignment(cid: int, ctx: TenantContext = Depends(require_permission("campaign.view"))):
    div = division_scope(ctx.conn, ctx)
    if div:
        _assert_campaign_in_div(ctx.conn, cid, div)
    return campaign_service.assignment_summary(ctx.conn, cid)


@router.put("/campaigns/{cid}/assignment")
def put_campaign_assignment(cid: int, body: dict,
                            ctx: TenantContext = Depends(require_permission("campaign.manage"))):
    div = division_scope(ctx.conn, ctx)
    if div:
        _assert_campaign_in_div(ctx.conn, cid, div)
    rules = campaign_service.set_campaign_assignments(ctx.conn, cid, {"assignment": body}, _actor(ctx))
    notify_admins(ctx.conn, "campaign.assigned", "Campaign assignments updated",
                  f"Executing audience updated for campaign #{cid}.")
    return {"ok": True, "rules": rules}


@router.get("/campaigns/{cid}/eligible-chemist")
def campaign_chemist_eligibility(cid: int, chemist_id: int,
                                 ctx: TenantContext = Depends(require_permission("campaign.view"))):
    """Is a chemist within this campaign's eligible chemist segment? Used by the
    field team before submitting a POB; the same check is enforced server-side
    on POST /pob/submit."""
    div = division_scope(ctx.conn, ctx)
    if div:
        _assert_campaign_in_div(ctx.conn, cid, div)
    return {"ok": True,
            "eligibility": campaign_service.chemist_eligibility(ctx.conn, cid, chemist_id)}


@router.post("/campaigns/{cid}/submit")
def submit_campaign_for_approval(cid: int, request: Request = None,
                                 ctx: TenantContext = Depends(require_permission("campaign.manage"))):
    """Draft -> pending_approval. The campaign is not executable until approved."""
    conn = ctx.conn
    c = conn.cursor()
    div = division_scope(conn, ctx)
    if div:
        _assert_campaign_in_div(conn, cid, div)
    c.execute("SELECT id, status, name FROM campaigns WHERE id=%s", (cid,))
    row = c.fetchone()
    if not row:
        raise HTTPException(404, "campaign not found")
    if row[1] not in ("draft", "rejected"):
        raise HTTPException(409, f"Only draft campaigns can be submitted for approval (current: {row[1]})")
    c.execute("UPDATE campaigns SET status='pending_approval', submitted_at=CURRENT_TIMESTAMP, "
              "submitted_by=%s, rejected_at=NULL, rejected_by=NULL, rejection_note=NULL WHERE id=%s",
              (ctx.user.get("id"), cid))
    conn.commit()
    log_action(conn, ctx.user.get("id"), "campaign.submit", "campaign", cid,
               request=request, actor=ctx.user.get("full_name") or f"#{cid}")
    notify_admins(conn, "campaign.submitted", "Campaign submitted for approval",
                  f"'{row[2]}' was submitted for approval.", "campaign", cid)
    try:
        from ..platform_notify import notify_event
        notify_event("campaign.pending", "Campaign submitted for approval",
                     f"'{row[2]}' is pending your approval.", "/superadmin/campaigns",
                     tenant_db=ctx.claims.get("tenant_db"))
    except Exception:
        pass
    return {"ok": True, "status": "pending_approval"}


@router.post("/campaigns/{cid}/withdraw")
def withdraw_campaign(cid: int, request: Request = None,
                      ctx: TenantContext = Depends(require_permission("campaign.manage"))):
    """Withdraw a pending campaign back to draft before a decision is made."""
    conn = ctx.conn
    c = conn.cursor()
    div = division_scope(conn, ctx)
    if div:
        _assert_campaign_in_div(conn, cid, div)
    c.execute("SELECT id, status, name FROM campaigns WHERE id=%s", (cid,))
    row = c.fetchone()
    if not row:
        raise HTTPException(404, "campaign not found")
    if row[1] != "pending_approval":
        raise HTTPException(409, f"Only campaigns awaiting approval can be withdrawn (current: {row[1]})")
    c.execute("UPDATE campaigns SET status='draft', submitted_at=NULL, submitted_by=NULL "
              "WHERE id=%s", (cid,))
    conn.commit()
    log_action(conn, ctx.user.get("id"), "campaign.withdraw", "campaign", cid,
               request=request, actor=ctx.user.get("full_name") or f"#{cid}")
    return {"ok": True, "status": "draft"}


@router.post("/campaigns/{cid}/extend")
def extend_campaign(cid: int, body: dict, request: Request = None,
                    ctx: TenantContext = Depends(require_permission("campaign.manage"))):
    div = division_scope(ctx.conn, ctx)
    if div:
        _assert_campaign_in_div(ctx.conn, cid, div)
    return campaign_service.extend_campaign(ctx.conn, _actor(ctx), cid, body, request=request)


@router.delete("/campaigns/{cid}")
def delete_campaign(cid: int, ctx: TenantContext = Depends(require_permission("campaign.manage"))):
    div = division_scope(ctx.conn, ctx)
    if div:
        _assert_campaign_in_div(ctx.conn, cid, div)
    campaign_service.delete_campaign(ctx.conn, _actor(ctx), cid)
    notify_admins(ctx.conn, "campaign.deleted", "Campaign removed",
                  f"Campaign #{cid} was deleted.")
    return {"ok": True}


@router.patch("/campaigns/{cid}/toggle-active")
def toggle_campaign_active(cid: int, ctx: TenantContext = Depends(require_permission("campaign.manage"))):
    conn = ctx.conn
    div = division_scope(conn, ctx)
    if div:
        _assert_campaign_in_div(conn, cid, div)
    c = conn.cursor()
    c.execute("SELECT id, active, name FROM campaigns WHERE id=%s", (cid,))
    row = c.fetchone()
    if not row:
        raise HTTPException(404, "campaign not found")
    new_active = not row[1]
    c.execute("UPDATE campaigns SET active=%s WHERE id=%s", (new_active, cid))
    conn.commit()
    status_word = "activated" if new_active else "deactivated"
    notify_admins(conn, "campaign.toggled", f"Campaign {status_word}",
                  f"Campaign '{row[2]}' (#{cid}) was {status_word}.")
    return {"id": cid, "active": new_active, "name": row[2]}


@router.post("/campaigns/{cid}/asset")
async def upload_campaign_asset(cid: int, kind: str = "logo", file: UploadFile = File(...),
                                ctx: TenantContext = Depends(require_permission("campaign.manage"))):
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
    conn = ctx.conn
    div = division_scope(conn, ctx)
    if div:
        _assert_campaign_in_div(conn, cid, div)
    cur = conn.cursor()
    cur.execute("SELECT id FROM campaigns WHERE id=%s", (cid,))
    if not cur.fetchone():
        raise HTTPException(404, "campaign not found")
    tenant_db = ctx.claims.get("tenant_db") or "tenant"
    rel = storage.save(data, tenant_db, "campaigns", file.filename or "asset")
    col = "logo_path" if kind == "logo" else "banner_path"
    cur.execute(f"UPDATE campaigns SET {col}=%s WHERE id=%s", (rel, cid))
    conn.commit()
    log_action(conn, _actor(ctx).get("id"), f"campaign.{kind}_upload", "campaign", cid,
               {"path": rel}, actor=_actor(ctx).get("name"))
    notify_admins(conn, "campaign.asset", "Campaign asset updated",
                  f"Campaign #{cid} {kind} was updated.", "campaign", cid)
    return {"ok": True, "path": rel, "url": storage.public_url(rel)}


# ── Products (read-only list for POB flows) ─────────────────────────────────

@router.get("/products")
def list_products(campaign_id: int = None, brand_id: int = None, q: str = "",
                  limit: int = PageLimit(default=200), offset: int = PageOffset(),
                  ctx: TenantContext = Depends(require_permission("product.view"))):
    conn = ctx.conn
    c = conn.cursor()
    sql = """SELECT p.*, c.name AS campaign_name, b.name AS brand_name FROM products p
             LEFT JOIN campaigns c ON c.id=p.campaign_id
             LEFT JOIN brands b ON b.id=p.brand_id"""
    where, params = [], []
    if campaign_id:
        where.append("p.campaign_id=%s")
        params.append(campaign_id)
    if brand_id:
        where.append("p.brand_id=%s")
        params.append(brand_id)
    if q:
        where.append("(p.name ILIKE %s OR p.sku ILIKE %s)")
        params.extend([f"%{q}%", f"%{q}%"])
    div = user_division_id(ctx.conn, ctx)
    if div:
        where.append("c.division_id=%s")
        params.append(div)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY p.id DESC LIMIT %s OFFSET %s"
    c.execute(sql, params + [limit, offset])
    return {"items": fetchall_dict(c)}


# ── Chemists ────────────────────────────────────────────────────────────────

@router.get("/chemists")
def list_chemists(q: str = "", city: str = "", state: str = "", status: str = "",
                  campaign_id: int = None,
                  limit: int = PageLimit(default=200), offset: int = PageOffset(),
                  ctx: TenantContext = Depends(require_permission("chemist.view"))):
    conn = ctx.conn
    c = conn.cursor()
    sql, where, params = "SELECT c.*", [], []
    # When campaign_id is provided, join POB status and registered_by info
    if campaign_id:
        sql += """, registered_by_name, registered_by_role, registered_by_level,
            pob_status, pob_id, invoice_status, verification_state, pob_created_at,
            grat_count, grat_total_value, grat_latest_status"""
        # Build the FROM with lateral joins
        sql += """ FROM chemists c
            LEFT JOIN LATERAL (
                SELECT u.full_name AS registered_by_name, r.name AS registered_by_role,
                       hl.label AS registered_by_level
                FROM users u LEFT JOIN roles r ON r.id=u.role_id
                LEFT JOIN hierarchy_levels hl ON hl.id=u.hierarchy_level_id
                WHERE u.id=c.created_by
            ) reg ON TRUE
            LEFT JOIN LATERAL (
                SELECT pa.status AS pob_status, pa.id AS pob_id,
                       CASE WHEN pa.invoice_path IS NOT NULL THEN 'uploaded' ELSE 'pending' END AS invoice_status,
                       pa.verification_state, pa.created_at AS pob_created_at
                FROM pob_activities pa
                WHERE pa.chemist_id=c.id AND pa.campaign_id=%s
                ORDER BY pa.id DESC LIMIT 1
            ) pob ON TRUE
            LEFT JOIN LATERAL (
                SELECT COUNT(*) AS grat_count,
                       COALESCE(SUM(g.scheme_value), 0) AS grat_total_value,
                       (SELECT g2.status FROM gratifications g2
                        JOIN pob_activities pa2 ON pa2.id=g2.pob_id
                        WHERE pa2.chemist_id=c.id AND g2.campaign_id=%s
                        ORDER BY g2.id DESC LIMIT 1) AS grat_latest_status
                FROM gratifications g
                JOIN pob_activities pa ON pa.id=g.pob_id
                WHERE pa.chemist_id=c.id AND g.campaign_id=%s
            ) grat ON TRUE"""
        params.extend([campaign_id, campaign_id, campaign_id])
    else:
        sql += """, NULL AS registered_by_name, NULL AS registered_by_role,
                   NULL AS registered_by_level,
                   NULL AS pob_status, NULL AS pob_id, NULL AS invoice_status,
                   NULL AS verification_state, NULL AS pob_created_at,
                   NULL AS grat_count, NULL AS grat_total_value, NULL AS grat_latest_status
                   FROM chemists c"""
    if q:
        where.append("(c.name ILIKE %s OR c.shop_name ILIKE %s OR c.mobile ILIKE %s OR c.gst ILIKE %s)")
        params.extend([f"%{q}%"] * 4)
    if city:
        where.append("c.city ILIKE %s")
        params.append(f"%{city}%")
    if state:
        where.append("c.state ILIKE %s")
        params.append(f"%{state}%")
    if status:
        where.append("c.status=%s")
        params.append(status)
    div = division_scope(conn, ctx)
    if div:
        where.append("(c.division_id=%s OR c.division_id IS NULL)")
        params.append(div)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY c.id DESC LIMIT %s OFFSET %s"
    c.execute(sql, params + [limit, offset])
    items = fetchall_dict(c)

# Batch-fetch hierarchy chains for all chemists with a registered_by user
    chemist_ids = [it["id"] for it in items if it.get("registered_by_name")]
    chains = _batch_hierarchy_chains(c, chemist_ids)
    for it in items:
        it["registered_by_hierarchy"] = chains.get(it["id"])
    # Payment-sensitive field: end users who do not manage or pay gratifications
    # only ever see a masked UPI address.
    if not (ctx.perms & {"gratification.pay", "gratification.manage"}):
        for it in items:
            if it.get("upi_id"):
                it["upi_id"] = mask_upi_id(it["upi_id"])

    return {"items": items}


def _batch_hierarchy_chains(c, chemist_ids):
    """Fetch hierarchy chains for multiple chemists in batch using a recursive CTE."""
    if not chemist_ids:
        return {}
    # Get created_by user IDs for all chemists
    c.execute("SELECT id, created_by FROM chemists WHERE id = ANY(%s) AND created_by IS NOT NULL",
              (chemist_ids,))
    chem_user = {row[0]: row[1] for row in c.fetchall()}
    if not chem_user:
        return {}

    user_ids = list(set(chem_user.values()))
    # Recursive CTE to walk the parent_id chain from each user up to HO
    c.execute("""
        WITH RECURSIVE chain AS (
            SELECT u.id AS user_id, u.full_name, r.name AS role_name,
                   hl.label AS level_label, hl.rank AS level_rank,
                   u.parent_id, 1 AS depth
            FROM users u
            LEFT JOIN roles r ON r.id = u.role_id
            LEFT JOIN hierarchy_levels hl ON hl.id = u.hierarchy_level_id
            WHERE u.id = ANY(%s)
            UNION ALL
            SELECT u.id, u.full_name, r.name, hl.label, hl.rank, u.parent_id, ch.depth + 1
            FROM users u
            JOIN chain ch ON u.id = ch.parent_id
            LEFT JOIN roles r ON r.id = u.role_id
            LEFT JOIN hierarchy_levels hl ON hl.id = u.hierarchy_level_id
            WHERE ch.depth < 15
        )
        SELECT user_id, full_name, role_name, level_label, level_rank
        FROM chain ORDER BY user_id, level_rank DESC
    """, (user_ids,))
    # Build per-user chains
    user_chains = {}
    for row in c.fetchall():
        uid = row[0]
        user_chains.setdefault(uid, []).append({
            "user_id": uid, "name": row[1], "role": row[2],
            "level": row[3], "rank": row[4],
        })
    # Map chemist_id -> chain
    return {cid: user_chains.get(uid) for cid, uid in chem_user.items()}


@router.get("/chemists/{cid}")
def chemist_detail(cid: int,
                   ctx: TenantContext = Depends(require_permission("chemist.view"))):
    """Full chemist profile for the detail view: identity, address, classification,
    potential, registrant lineage and activity summary (POB / gratification / visits).
    Scoped to the caller's division the same way the list endpoint is."""
    conn = ctx.conn
    c = conn.cursor()
    c.execute("""SELECT c.*,
            u.full_name AS registered_by_name, r.name AS registered_by_role,
            hl.label AS registered_by_level
        FROM chemists c
        LEFT JOIN users u ON u.id = c.created_by
        LEFT JOIN roles r ON r.id = u.role_id
        LEFT JOIN hierarchy_levels hl ON hl.id = u.hierarchy_level_id
        WHERE c.id=%s""", (cid,))
    row = fetchone_dict(c)
    if not row:
        raise HTTPException(404, "chemist not found")
    div = division_scope(conn, ctx)
    if div and row.get("division_id") not in (None, div):
        raise HTTPException(404, "chemist not found")

    c.execute("""
        SELECT
            COUNT(*) AS pobs,
            COUNT(*) FILTER (WHERE status IN ('verified', 'approved')) AS verified_pobs,
            COALESCE(SUM(pob_amount) FILTER (WHERE status IN ('verified', 'approved')), 0) AS verified_value,
            COUNT(*) FILTER (WHERE status IN ('pending_verification', 'pending', 'submitted')) AS pending_pobs,
            COUNT(*) FILTER (WHERE status IN ('rejected', 'duplicate')) AS rejected_pobs,
            COUNT(*) FILTER (WHERE invoice_path IS NOT NULL) AS invoiced_pobs
        FROM pob_activities WHERE chemist_id=%s""", (cid,))
    activity = fetchone_dict(c) or {}
    c.execute("""
        SELECT COUNT(*) AS grants_count,
               COALESCE(SUM(g.scheme_value), 0) AS grants_value,
               COALESCE(SUM(g.scheme_value) FILTER (WHERE g.status IN
                   ('paid', 'dispatched', 'delivered', 'completed')), 0) AS paid_value
        FROM gratifications g
        JOIN pob_activities pa ON pa.id = g.pob_id
        WHERE pa.chemist_id=%s""", (cid,))
    grats = fetchone_dict(c) or {}
    c.execute("""
        SELECT COUNT(*) AS visits, MAX(visit_date) AS last_visit
        FROM chemist_visits WHERE chemist_id=%s""", (cid,))
    visits = fetchone_dict(c) or {}

    chains = _batch_hierarchy_chains(c, [cid])
    row["registered_by_hierarchy"] = chains.get(cid)
    row["activity"] = {
        "pobs": activity.get("pobs", 0),
        "verified_pobs": activity.get("verified_pobs", 0),
        "verified_value": round(activity.get("verified_value", 0), 2),
        "pending_pobs": activity.get("pending_pobs", 0),
        "rejected_pobs": activity.get("rejected_pobs", 0),
        "invoiced_pobs": activity.get("invoiced_pobs", 0),
    }
    row["gratification"] = {
        "count": grats.get("grants_count", 0),
        "value": round(grats.get("grants_value", 0), 2),
        "paid_value": round(grats.get("paid_value", 0), 2),
    }
    row["visits"] = {
        "count": visits.get("visits", 0),
        "last_visit": visits.get("last_visit"),
    }
    if not (ctx.perms & {"gratification.pay", "gratification.manage"}):
        if row.get("upi_id"):
            row["upi_id"] = mask_upi_id(row["upi_id"])
    return row


@router.post("/chemists")
def create_chemist(body: dict, ctx: TenantContext = Depends(require_permission("chemist.manage"))):
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "chemist name required")
    conn = ctx.conn
    c = conn.cursor()
    # Configurable duplicate detection: each enabled check looks for an existing
    # chemist that already covers this chemist's identity, so the field team
    # does not re-register the same shop. The frontend renders a picker.
    checks = body.get("duplicate_checks")
    if isinstance(checks, list) and checks:
        enabled = {x for x in checks if x in ("mobile", "shop_pincode", "dl", "gst")}
    else:
        enabled = {"mobile", "shop_pincode", "dl", "gst"}
    scoped_div = division_scope(conn, ctx)
    dup_rules = []
    mobile = (body.get("mobile") or "").strip()
    if mobile and "mobile" in enabled:
        dup_rules.append(("mobile", "mobile=%s", (mobile,)))
    shop_name = (body.get("shop_name") or "").strip()
    pin = (body.get("pin") or "").strip()
    if shop_name and pin and "shop_pincode" in enabled:
        dup_rules.append(("shop_pincode", "upper(shop_name)=%s AND pin=%s",
                          (shop_name.upper(), pin)))
    dl_number = (body.get("dl_number") or "").strip()
    if dl_number and "dl" in enabled:
        dup_rules.append(("dl", "upper(dl_number)=%s", (dl_number.upper(),)))
    gst = (body.get("gst") or "").strip()
    if gst and "gst" in enabled:
        dup_rules.append(("gst", "upper(gst)=%s", (gst.upper(),)))
    if dup_rules:
        matches, seen = [], set()
        for rule, cond, vals in dup_rules:
            extra = " AND (division_id=%s OR division_id IS NULL)" if scoped_div else ""
            params = list(vals) + ([scoped_div] if scoped_div else [])
            sql = (f"SELECT id, name, shop_name, city, mobile FROM chemists "
                   f"WHERE {cond}{extra} ORDER BY id")
            c.execute(sql, params)
            for row in c.fetchall():
                if row[0] in seen:
                    continue
                seen.add(row[0])
                matches.append({"rule": rule, "chemist": {
                    "id": row[0], "name": row[1], "shop_name": row[2],
                    "city": row[3], "mobile": row[4],
                }})
        if matches:
            return {"ok": False, "duplicates": matches,
                    "message": "A chemist matching this data already exists"}
    cols = ["name", "shop_name", "gst", "dl_number", "owner_name", "mobile", "alternate_mobile",
            "email", "address", "city", "district", "state", "pin", "latitude", "longitude",
            "ocid", "doctor_name", "category", "area", "upi_id", "status",
            "attachment_type", "potential_category", "institution_name", "institution_type",
            "institution_department", "institution_contact_person", "institution_address",
            "monthly_business_potential", "estimated_monthly_sales", "brand_potential",
            "strategic_importance", "last_visit_date", "visit_frequency"]
    vals = [body.get(col) for col in cols]
    vals[0] = name
    if not vals[20]:
        vals[20] = "active"
    division_id = body.get("division_id") or division_scope(conn, ctx)
    if division_id:
        cols.append("division_id")
        vals.append(division_id)
    cols.append("created_by")
    vals.append(ctx.user["id"])
    c.execute(
        f"""INSERT INTO chemists ({', '.join(cols)}) VALUES ({', '.join(['%s']*len(cols))}) RETURNING id""",
        vals,
    )
    cid = c.fetchone()[0]
    c.execute("UPDATE chemists SET chemist_code=%s WHERE id=%s", (f"CH-{cid:05d}", cid))
    conn.commit()
    log_action(conn, ctx.user["id"], "chemist.create", "chemist", cid, {"name": name})
    return {"ok": True, "id": cid, "chemist_code": f"CH-{cid:05d}"}


@router.put("/chemists/{cid}")
def update_chemist(cid: int, body: dict, ctx: TenantContext = Depends(require_permission("chemist.manage"))):
    conn = ctx.conn
    c = conn.cursor()
    fields = ["name", "shop_name", "gst", "dl_number", "owner_name", "mobile", "alternate_mobile",
              "email", "address", "city", "district", "state", "pin", "latitude", "longitude",
              "ocid", "doctor_name", "category", "area", "upi_id", "status",
              "attachment_type", "potential_category", "institution_name", "institution_type",
              "institution_department", "institution_contact_person", "institution_address",
              "monthly_business_potential", "estimated_monthly_sales", "brand_potential",
              "strategic_importance", "last_visit_date", "visit_frequency"]
    sets, params = [], []
    for f in fields:
        if f in body and body[f] is not None:
            sets.append(f"{f}=%s")
            params.append(body[f])
    if not sets:
        raise HTTPException(400, "Nothing to update")
    params.append(cid)
    c.execute(f"UPDATE chemists SET {', '.join(sets)} WHERE id=%s", params)
    conn.commit()
    log_action(conn, ctx.user["id"], "chemist.update", "chemist", cid)
    return {"ok": True}


@router.delete("/chemists/{cid}")
def delete_chemist(cid: int, ctx: TenantContext = Depends(require_permission("chemist.manage"))):
    conn = ctx.conn
    c = conn.cursor()
    c.execute("SELECT id FROM pob_activities WHERE chemist_id=%s LIMIT 1", (cid,))
    if c.fetchone():
        raise HTTPException(409, "chemist has POB activities")
    c.execute("DELETE FROM chemists WHERE id=%s", (cid,))
    conn.commit()
    log_action(conn, ctx.user["id"], "chemist.delete", "chemist", cid)
    return {"ok": True}


# ── Chemist classification masters ───────────────────────────────────────────
# Attachment (institution) types + potential categories are company config,
# seeded with sensible defaults. View is open to the field team so the
# dropdowns on the chemist forms work; only managers edit them.

_MASTER_TABLES = {
    "attachment-types": "chemist_attachment_types",
    "potential-categories": "chemist_potential_categories",
}
_MASTER_FIELDS = ["code", "name", "description", "active", "sort_order"]


def _master_rows(conn, table: str, active: bool = None) -> list[dict]:
    c = conn.cursor()
    sql = f"SELECT * FROM {table}"
    params = []
    if active is not None:
        sql += " WHERE active=%s"
        params.append(active)
    sql += " ORDER BY sort_order, name"
    c.execute(sql, params)
    return fetchall_dict(c)


@router.get("/chemist-masters")
def list_chemist_masters(active: bool = None,
                         ctx: TenantContext = Depends(require_permission("chemist.classification.view"))):
    conn = ctx.conn
    return {
        "attachment_types": _master_rows(conn, _MASTER_TABLES["attachment-types"], active),
        "potential_categories": _master_rows(conn, _MASTER_TABLES["potential-categories"], active),
    }


@router.post("/chemist-masters/{kind}")
def create_chemist_master(kind: str, body: dict,
                          ctx: TenantContext = Depends(require_permission("chemist.classification.manage"))):
    table = _MASTER_TABLES.get(kind)
    if not table:
        raise HTTPException(404, "unknown master kind")
    code = (body.get("code") or "").strip().lower()
    name = (body.get("name") or "").strip()
    if not code or not name:
        raise HTTPException(400, "code and name required")
    if code in ("others", "not_defined"):
        raise HTTPException(400, "reserved code")
    conn = ctx.conn
    c = conn.cursor()
    c.execute(f"SELECT id FROM {table} WHERE code=%s", (code,))
    if c.fetchone():
        raise HTTPException(409, "code already exists")
    c.execute(
        f"INSERT INTO {table} (code, name, description, active, sort_order) VALUES (%s,%s,%s,%s,%s) RETURNING id",
        (code, name, body.get("description"), body.get("active", True), body.get("sort_order") or 0))
    mid = c.fetchone()[0]
    conn.commit()
    log_action(conn, ctx.user["id"], f"chemist_master.{kind}.create", "chemist",
               mid, {"code": code, "name": name})
    return {"ok": True, "id": mid}


@router.put("/chemist-masters/{kind}/{mid}")
def update_chemist_master(kind: str, mid: int, body: dict,
                          ctx: TenantContext = Depends(require_permission("chemist.classification.manage"))):
    table = _MASTER_TABLES.get(kind)
    if not table:
        raise HTTPException(404, "unknown master kind")
    sets, params = [], []
    for f in _MASTER_FIELDS:
        if f in body and body[f] is not None:
            if f == "code":
                body[f] = str(body[f]).strip().lower()
                if body[f] in ("others", "not_defined"):
                    raise HTTPException(400, "reserved code")
            sets.append(f"{f}=%s")
            params.append(body[f])
    if not sets:
        raise HTTPException(400, "Nothing to update")
    params.append(mid)
    conn = ctx.conn
    c = conn.cursor()
    c.execute(f"UPDATE {table} SET {', '.join(sets)} WHERE id=%s", params)
    conn.commit()
    log_action(conn, ctx.user["id"], f"chemist_master.{kind}.update", "chemist", mid)
    return {"ok": True}


@router.post("/chemists/bulk-upload")
async def bulk_upload_chemists(file: UploadFile = File(...),
                               ctx: TenantContext = Depends(require_permission("chemist.manage"))):
    data = await file.read()
    try:
        validate_upload(data, filename=file.filename or "", allowed_kinds=SPREADSHEET_KINDS,
                        max_size=config.MAX_UPLOAD_SIZE)
    except UploadValidationError as exc:
        raise HTTPException(400, str(exc))
    try:
        wb = load_workbook(io.BytesIO(data))
        ws = wb.active
    except Exception:
        raise HTTPException(400, "Invalid Excel file")
    headers = [str(c.value).strip() if c.value is not None else "" for c in ws[1]]
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    conn = ctx.conn
    c = conn.cursor()
    created, errors = 0, []
    fields = ["name", "shop_name", "gst", "dl_number", "owner_name", "mobile", "alternate_mobile",
              "email", "address", "city", "district", "state", "pin", "latitude", "longitude",
              "ocid", "doctor_name", "category", "area", "upi_id", "status",
              "attachment_type", "potential_category", "institution_name", "institution_type",
              "institution_department", "institution_contact_person", "institution_address",
              "monthly_business_potential", "estimated_monthly_sales", "brand_potential",
              "strategic_importance", "last_visit_date", "visit_frequency"]
    division_id = division_scope(conn, ctx)
    for i, row in enumerate(rows, start=2):
        if not row or all(v is None or str(v).strip() == "" for v in row):
            continue
        d = {headers[j]: (row[j] if j < len(row) else None) for j in range(len(headers))}
        if not (d.get("name") or "").strip():
            errors.append(f"row {i}: name required")
            continue
        vals = [d.get(f) for f in fields]
        if not vals[20]:
            vals[20] = "active"
        insert_fields = fields
        if division_id:
            insert_fields = fields + ["division_id"]
            vals = vals + [division_id]
        try:
            c.execute(
                f"INSERT INTO chemists ({', '.join(insert_fields)}) VALUES ({', '.join(['%s']*len(insert_fields))})",
                vals,
            )
            created += 1
        except Exception as exc:
            errors.append(f"row {i}: {exc}")
    conn.commit()
    log_action(conn, ctx.user["id"], "chemist.bulk_upload", "chemist", None, {"created": created, "errors": len(errors)})
    return {"created": created, "errors": errors}


@router.get("/chemists/bulk-template")
def chemist_template(ctx: TenantContext = Depends(require_permission("chemist.manage"))):
    wb = Workbook()
    ws = wb.active
    ws.title = "Chemists"
    ws.append(["name", "shop_name", "gst", "dl_number", "owner_name", "mobile", "alternate_mobile",
               "email", "address", "city", "district", "state", "pin", "latitude", "longitude",
               "ocid", "doctor_name", "category", "area", "upi_id", "status",
               "attachment_type", "potential_category", "institution_name", "institution_type",
               "institution_department", "institution_contact_person", "institution_address",
               "monthly_business_potential", "estimated_monthly_sales", "brand_potential",
               "strategic_importance", "last_visit_date", "visit_frequency"])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": "attachment; filename=chemists_template.xlsx"})


# ── Pincode lookup (India Post) ──────────────────────────────────────────────

@router.get("/pincode/{pincode}")
def lookup_pincode(pincode: str,
                   ctx: TenantContext = Depends(require_permission("chemist.view"))):
    """Proxy the public India Post pincode API so the SPA never talks to a
    third party directly (no CORS, no exposing upstream behavior to clients).

    Returns a normalized list of post offices; each carries the location
    (post-office name), district and state for the given 6-digit PIN.
    """
    import httpx

    pincode = pincode.strip()
    if not pincode.isdigit() or len(pincode) != 6:
        raise HTTPException(400, "pincode must be a 6-digit number")
    try:
        r = httpx.get(f"https://api.postalpincode.in/pincode/{pincode}", timeout=15)
        r.raise_for_status()
        payload = r.json()
    except Exception:
        raise HTTPException(502, "pincode lookup service unavailable")
    if not payload or payload[0].get("Status") != "Success":
        return {"items": [], "pincode": pincode}
    post_offices = payload[0].get("PostOffice") or []
    seen = set()
    items = []
    for po in post_offices:
        name = (po.get("Name") or "").strip()
        district = (po.get("District") or "").strip()
        state = (po.get("State") or "").strip()
        key = (name.lower(), district.lower(), state.lower())
        if not name or key in seen:
            continue
        seen.add(key)
        items.append({"location": name, "city": name, "district": district, "state": state})
    return {"items": items, "pincode": pincode}
