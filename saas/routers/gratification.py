"""
Gratification router.

Drives the three gratification workflows from the SOW:
  - Physical gift: eligible -> dispatched -> delivered (photo + GPS) -> acknowledged -> completed
  - Cashback / UPI: eligible -> approved -> paid -> completed
  - Voucher: generated -> sent -> redeemed

Every transition is recorded in gratification_events and notified to the
recipient (in-app + configured channels).
"""
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from .. import config, storage
from ..audit import log_action
from ..db_utils import fetchall_dict, fetchone_dict
from ..deps import TenantContext, require_permission
from ..notify import notify_from_template
from ..scoping import visible_user_ids
from ..upload_validation import IMAGE_KINDS, UploadValidationError, validate_upload
from ..pagination import PageLimit, PageOffset

router = APIRouter(prefix="/api/v1", tags=["gratification"])


# ── Gratification types (dropdown master) ──────────────────────────────────

@router.get("/gratification/types")
def gratification_types(ctx: TenantContext = Depends(require_permission("gratification.view"))):
    conn = ctx.conn
    c = conn.cursor()
    c.execute("SELECT * FROM gratification_types ORDER BY id")
    return {"items": fetchall_dict(c)}


@router.post("/gratification/types")
def create_gratification_type(body: dict, ctx: TenantContext = Depends(require_permission("gratification.manage"))):
    code = (body.get("code") or "").strip().lower()
    if not code or not body.get("name"):
        raise HTTPException(400, "code and name required")
    conn = ctx.conn
    c = conn.cursor()
    c.execute("INSERT INTO gratification_types (code, name, description) VALUES (%s,%s,%s) "
              "ON CONFLICT (code) DO UPDATE SET name=EXCLUDED.name RETURNING id",
              (code, body["name"], body.get("description")))
    tid = c.fetchone()[0]
    conn.commit()
    return {"ok": True, "id": tid}


# ── Gifts master ────────────────────────────────────────────────────────────

@router.get("/gifts")
def list_gifts(ctx: TenantContext = Depends(require_permission("gratification.view"))):
    conn = ctx.conn
    c = conn.cursor()
    c.execute("SELECT * FROM gifts ORDER BY id DESC")
    return {"items": fetchall_dict(c)}


@router.post("/gifts")
def create_gift(body: dict, ctx: TenantContext = Depends(require_permission("gratification.manage"))):
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "gift name required")
    conn = ctx.conn
    c = conn.cursor()
    c.execute("INSERT INTO gifts (name, image_path, cost, stock, active) VALUES (%s,%s,%s,%s,%s) RETURNING id",
              (name, body.get("image_path"), body.get("cost") or 0, body.get("stock") or 0,
               body.get("active", True)))
    gid = c.fetchone()[0]
    conn.commit()
    log_action(conn, ctx.user["id"], "gift.create", "gift", gid, {"name": name})
    return {"ok": True, "id": gid}


@router.put("/gifts/{gid}")
def update_gift(gid: int, body: dict, ctx: TenantContext = Depends(require_permission("gratification.manage"))):
    conn = ctx.conn
    c = conn.cursor()
    fields = ["name", "image_path", "cost", "stock", "active"]
    sets, params = [], []
    for f in fields:
        if f in body and body[f] is not None:
            sets.append(f"{f}=%s")
            params.append(body[f])
    if not sets:
        raise HTTPException(400, "Nothing to update")
    params.append(gid)
    c.execute(f"UPDATE gifts SET {', '.join(sets)} WHERE id=%s", params)
    conn.commit()
    log_action(conn, ctx.user["id"], "gift.update", "gift", gid)
    return {"ok": True}


@router.post("/gifts/{gid}/upload")
async def upload_gift_image(gid: int, file: UploadFile = File(...),
                            ctx: TenantContext = Depends(require_permission("gratification.manage"))):
    data = await file.read()
    try:
        validate_upload(data, filename=file.filename or "", allowed_kinds=IMAGE_KINDS,
                        max_size=config.MAX_UPLOAD_SIZE)
    except UploadValidationError as exc:
        raise HTTPException(400, str(exc))
    rel = storage.save(data, ctx.claims["tenant_db"], "gifts", file.filename or "gift.jpg")
    conn = ctx.conn
    c = conn.cursor()
    c.execute("UPDATE gifts SET image_path=%s WHERE id=%s", (rel, gid))
    conn.commit()
    return {"ok": True, "path": rel, "url": storage.public_url(rel)}


# ── Gratifications list / detail ────────────────────────────────────────────

def _grat_query(extra="", params=()):
    sql = f"""
      SELECT g.*, pa.user_id AS mr_id, u.full_name AS mr_name,
             cmp.name AS campaign_name, ch.name AS chemist_name, ch.shop_name,
             gift.name AS gift_name, gift.image_path AS gift_image
      FROM gratifications g
      JOIN pob_activities pa ON pa.id=g.pob_id
      JOIN users u ON u.id=g.user_id
      JOIN campaigns cmp ON cmp.id=g.campaign_id
      JOIN chemists ch ON ch.id=pa.chemist_id
      LEFT JOIN gifts gift ON gift.id=g.gift_id
      {extra} ORDER BY g.id DESC
    """
    return sql, params


@router.get("/gratification")
def list_gratifications(status: str = "", campaign_id: int = None, type_code: str = "",
                        limit: int = PageLimit(default=200), offset: int = PageOffset(),
                        ctx: TenantContext = Depends(require_permission("gratification.view"))):
    conn = ctx.conn
    where, params = [], []
    visible = visible_user_ids(conn, ctx)
    if visible is not None:
        where.append("g.user_id = ANY(%s)")
        params.append(visible)
    if status:
        where.append("g.status=%s")
        params.append(status)
    if campaign_id:
        where.append("g.campaign_id=%s")
        params.append(campaign_id)
    if type_code:
        where.append("g.type_code=%s")
        params.append(type_code)
    extra = "WHERE " + " AND ".join(where) if where else ""
    sql, params = _grat_query(extra, params)
    sql += " LIMIT %s OFFSET %s"
    params = list(params) + [limit, offset]
    c = conn.cursor()
    c.execute(sql, params)
    return {"items": fetchall_dict(c)}


@router.get("/gratification/{gid}")
def get_gratification(gid: int, ctx: TenantContext = Depends(require_permission("gratification.view"))):
    conn = ctx.conn
    c = conn.cursor()
    sql, params = _grat_query("WHERE g.id=%s", (gid,))
    c.execute(sql, params)
    row = fetchone_dict(c)
    if not row:
        raise HTTPException(404, "gratification not found")
    visible = visible_user_ids(conn, ctx)
    if visible is not None and row["mr_id"] not in visible:
        raise HTTPException(403, "not allowed to view this gratification")
    c.execute("SELECT * FROM gratification_events WHERE gratification_id=%s ORDER BY id", (gid,))
    row["events"] = fetchall_dict(c)
    return row


def _transition(conn, gid, event, detail, actor_id, new_status=None, updates=None):
    c = conn.cursor()
    c.execute("SELECT * FROM gratifications WHERE id=%s", (gid,))
    g = fetchone_dict(c)
    if not g:
        raise HTTPException(404, "gratification not found")
    visible = visible_user_ids(conn, ctx)
    if updates:
        sets = ", ".join(f"{k}=%s" for k in updates)
        params = list(updates.values())
        params.append(gid)
        c.execute(f"UPDATE gratifications SET {sets} WHERE id=%s", params)
    if new_status:
        c.execute("UPDATE gratifications SET status=%s WHERE id=%s", (new_status, gid))
    c.execute("INSERT INTO gratification_events (gratification_id, event, detail, actor_id) "
              "VALUES (%s,%s,%s,%s)", (gid, event, detail, actor_id))
    conn.commit()
    return g


def _scoped_gratification(conn, ctx, gid):
    """Fetch a gratification and enforce the caller's user-visibility scope.

    Mirrors the read-path check in GET /gratification/{id}. Write transitions
    previously resolved the row by id without this check, letting a hierarchy-
    scoped actor (e.g. a division admin) dispatch / approve / pay / redeem
    gratifications for users outside their visible set.
    """
    c = conn.cursor()
    c.execute("SELECT * FROM gratifications WHERE id=%s", (gid,))
    g = fetchone_dict(c)
    if not g:
        raise HTTPException(404, "gratification not found")
    visible = visible_user_ids(conn, ctx)
    if visible is not None and g["user_id"] not in visible:
        raise HTTPException(403, "not allowed to act on this gratification")
    return g


# ── Workflow: physical gift ─────────────────────────────────────────────────

@router.post("/gratification/{gid}/dispatch")
def dispatch_gift(gid: int, body: dict, ctx: TenantContext = Depends(require_permission("gratification.dispatch"))):
    conn = ctx.conn
    gift_id = body.get("gift_id")
    g = _scoped_gratification(conn, ctx, gid)
    c = conn.cursor()
    if g["type_code"] != "physical_gift":
        raise HTTPException(400, "only physical_gift gratifications can be dispatched")
    if gift_id:
        c.execute("SELECT * FROM gifts WHERE id=%s", (gift_id,))
        gift = fetchone_dict(c)
        if not gift:
            raise HTTPException(400, "gift not found")
        if (gift["stock"] or 0) <= 0:
            raise HTTPException(409, "gift out of stock")
        c.execute("UPDATE gifts SET stock=stock-1 WHERE id=%s", (gift_id,))
    c.execute("UPDATE gratifications SET gift_id=%s, dispatch_status='dispatched', status='dispatched', "
              "dispatched_at=CURRENT_TIMESTAMP WHERE id=%s", (gift_id, gid))
    c.execute("INSERT INTO gratification_events (gratification_id, event, detail, actor_id) "
              "VALUES (%s,'dispatched',%s,%s)", (gid, body.get("tracking") or "Dispatched", ctx.user["id"]))
    conn.commit()
    log_action(conn, ctx.user["id"], "gratification.dispatch", "gratification", gid)
    notify_from_template(conn, g["user_id"], "gift.ready", {"gift": body.get("gift_id")},
                         "gratification", gid)
    return {"ok": True, "status": "dispatched"}


@router.post("/gratification/{gid}/delivered")
async def deliver_gift(gid: int,
                       latitude: float = Form(0), longitude: float = Form(0),
                       photo: UploadFile = File(None),
                       ctx: TenantContext = Depends(require_permission("gratification.dispatch"))):
    conn = ctx.conn
    g = _scoped_gratification(conn, ctx, gid)
    c = conn.cursor()
    if g["status"] not in ("dispatched", "delivered"):
        raise HTTPException(409, f"cannot deliver a {g['status']} gratification")
    photo_path = g.get("photo_path")
    if photo is not None and photo.filename:
        data = await photo.read()
        try:
            validate_upload(data, filename=photo.filename, allowed_kinds=IMAGE_KINDS,
                            max_size=config.MAX_UPLOAD_SIZE)
        except UploadValidationError as exc:
            raise HTTPException(400, str(exc))
        photo_path = storage.save(data, ctx.claims["tenant_db"], "deliveries", photo.filename)
    c.execute("UPDATE gratifications SET status='delivered', delivery_status='delivered', "
              "delivered_at=CURRENT_TIMESTAMP, photo_path=%s, gps_lat=%s, gps_lng=%s WHERE id=%s",
              (photo_path, latitude, longitude, gid))
    c.execute("INSERT INTO gratification_events (gratification_id, event, detail, actor_id) "
              "VALUES (%s,'delivered',%s,%s)",
              (gid, f"GPS {latitude},{longitude}" + (" + photo" if photo_path else ""), ctx.user["id"]))
    conn.commit()
    log_action(conn, ctx.user["id"], "gratification.deliver", "gratification", gid,
               {"lat": latitude, "lng": longitude})
    notify_from_template(conn, g["user_id"], "gift.delivered", {"gift": g.get("gift_id")},
                         "gratification", gid)
    return {"ok": True, "status": "delivered", "photo_path": photo_path,
            "gps": {"lat": latitude, "lng": longitude}}


@router.post("/gratification/{gid}/acknowledge")
def acknowledge_gift(gid: int, body: dict, ctx: TenantContext = Depends(require_permission("gratification.dispatch"))):
    ack = (body.get("acknowledgement") or "").strip()
    if not ack:
        raise HTTPException(400, "acknowledgement required")
    conn = ctx.conn
    g = _scoped_gratification(conn, ctx, gid)
    c = conn.cursor()
    c.execute("UPDATE gratifications SET status='completed', acknowledgement=%s, completed_at=CURRENT_TIMESTAMP "
              "WHERE id=%s", (ack, gid))
    c.execute("INSERT INTO gratification_events (gratification_id, event, detail, actor_id) "
              "VALUES (%s,'acknowledged',%s,%s)", (gid, ack, ctx.user["id"]))
    conn.commit()
    log_action(conn, ctx.user["id"], "gratification.acknowledge", "gratification", gid)
    return {"ok": True, "status": "completed"}


# ── Workflow: cashback / UPI ────────────────────────────────────────────────

@router.post("/gratification/{gid}/approve")
def approve_cashback(gid: int, body: dict, ctx: TenantContext = Depends(require_permission("gratification.approve"))):
    conn = ctx.conn
    g = _scoped_gratification(conn, ctx, gid)
    c = conn.cursor()
    if g["type_code"] not in ("cashback", "upi"):
        raise HTTPException(400, "only cashback/upi gratifications can be cashback-approved")
    c.execute("UPDATE gratifications SET status='approved', upi_id=%s WHERE id=%s",
              (body.get("upi_id") or g.get("upi_id"), gid))
    c.execute("INSERT INTO gratification_events (gratification_id, event, detail, actor_id) "
              "VALUES (%s,'approved',%s,%s)", (gid, body.get("note") or "Cashback approved", ctx.user["id"]))
    conn.commit()
    log_action(conn, ctx.user["id"], "gratification.approve", "gratification", gid)
    return {"ok": True, "status": "approved"}


@router.post("/gratification/{gid}/pay")
def pay_cashback(gid: int, body: dict, ctx: TenantContext = Depends(require_permission("gratification.pay"))):
    payment_ref = (body.get("payment_ref") or "").strip()
    conn = ctx.conn
    g = _scoped_gratification(conn, ctx, gid)
    c = conn.cursor()
    if g["type_code"] not in ("cashback", "upi"):
        raise HTTPException(400, "only cashback/upi gratifications can be paid")
    if g["status"] != "approved":
        raise HTTPException(409, "approve before paying")
    c.execute("UPDATE gratifications SET status='completed', payment_ref=%s, paid_at=CURRENT_TIMESTAMP, "
              "completed_at=CURRENT_TIMESTAMP WHERE id=%s", (payment_ref, gid))
    c.execute("INSERT INTO gratification_events (gratification_id, event, detail, actor_id) "
              "VALUES (%s,'paid',%s,%s)", (gid, payment_ref, ctx.user["id"]))
    conn.commit()
    log_action(conn, ctx.user["id"], "gratification.pay", "gratification", gid, {"payment_ref": payment_ref})
    notify_from_template(conn, g["user_id"], "cashback.paid",
                         {"amount": g["scheme_value"], "upi": g.get("upi_id") or "-"},
                         "gratification", gid)
    return {"ok": True, "status": "completed"}


# ── Workflow: voucher ───────────────────────────────────────────────────────

@router.post("/gratification/{gid}/generate-voucher")
def generate_voucher(gid: int, body: dict, ctx: TenantContext = Depends(require_permission("gratification.manage"))):
    code = (body.get("code") or "").strip()
    if not code:
        import secrets, string
        alphabet = string.ascii_uppercase + string.digits
        code = "VCH-" + "".join(secrets.choice(alphabet) for _ in range(10))
    conn = ctx.conn
    g = _scoped_gratification(conn, ctx, gid)
    c = conn.cursor()
    if g["type_code"] != "voucher":
        raise HTTPException(400, "only voucher gratifications can generate vouchers")
    c.execute("UPDATE gratifications SET status='generated', voucher_code=%s, voucher_status='generated' "
              "WHERE id=%s", (code, gid))
    c.execute("INSERT INTO gratification_events (gratification_id, event, detail, actor_id) "
              "VALUES (%s,'generated',%s,%s)", (gid, code, ctx.user["id"]))
    conn.commit()
    log_action(conn, ctx.user["id"], "gratification.generate_voucher", "gratification", gid)
    return {"ok": True, "code": code, "status": "generated"}


@router.post("/gratification/{gid}/send-voucher")
def send_voucher(gid: int, body: dict, ctx: TenantContext = Depends(require_permission("gratification.manage"))):
    conn = ctx.conn
    g = _scoped_gratification(conn, ctx, gid)
    c = conn.cursor()
    c.execute("UPDATE gratifications SET status='sent', voucher_status='sent' WHERE id=%s", (gid,))
    c.execute("INSERT INTO gratification_events (gratification_id, event, detail, actor_id) "
              "VALUES (%s,'sent',%s,%s)", (gid, body.get("to") or "Sent to recipient", ctx.user["id"]))
    conn.commit()
    notify_from_template(conn, g["user_id"], "voucher.issued",
                         {"code": g["voucher_code"], "amount": g["scheme_value"]},
                         "gratification", gid)
    return {"ok": True, "status": "sent"}


@router.post("/gratification/{gid}/redeem-voucher")
def redeem_voucher(gid: int, body: dict, ctx: TenantContext = Depends(require_permission("gratification.manage"))):
    conn = ctx.conn
    g = _scoped_gratification(conn, ctx, gid)
    c = conn.cursor()
    if g["type_code"] != "voucher":
        raise HTTPException(400, "only vouchers can be redeemed")
    c.execute("UPDATE gratifications SET status='completed', voucher_status='redeemed', "
              "completed_at=CURRENT_TIMESTAMP WHERE id=%s", (gid,))
    c.execute("INSERT INTO gratification_events (gratification_id, event, detail, actor_id) "
              "VALUES (%s,'redeemed',%s,%s)", (gid, body.get("redeemed_by") or "Redeemed", ctx.user["id"]))
    conn.commit()
    return {"ok": True, "status": "completed"}
