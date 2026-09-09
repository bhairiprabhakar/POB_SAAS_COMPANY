"""
Campaign structure business logic shared by BOTH the tenant (read-only) and
the super admin (full CRUD) route layers.

Campaigns, divisions and brands are tenant-owned data, but their CREATION /
EDIT / DELETION is a platform-level capability: only the super admin may
manage campaign structure. Tenant users only read them (list / get / tracking)
so they can execute POB work against them.

`actor` is a dict with an optional `id` (tenant user id) and `name` (used for
the tenant audit trail). Super admin routes pass id=None + their username.
"""
import datetime as dt

from fastapi import HTTPException

from .audit import log_action
from .db_utils import fetchall_dict, fetchone_dict


# ── Brands ──────────────────────────────────────────────────────────────────

def _validate_division(conn, division_id):
    """Return a valid division id, or None. Raises when the id does not exist."""
    if division_id in (None, "", 0):
        return None
    c = conn.cursor()
    c.execute("SELECT id FROM divisions WHERE id=%s", (division_id,))
    if not c.fetchone():
        raise HTTPException(400, "invalid division_id")
    return int(division_id)


def list_brands(conn, q: str = "", status: str = "", division_id: int = None) -> list[dict]:
    sql = """SELECT b.*, d.name AS division_name, d.slug AS division_slug
             FROM brands b LEFT JOIN divisions d ON d.id=b.division_id"""
    where, params = [], []
    if q:
        where.append("(b.name ILIKE %s OR b.code ILIKE %s)")
        params.extend([f"%{q}%", f"%{q}%"])
    if status:
        where.append("b.status=%s")
        params.append(status)
    if division_id:
        where.append("b.division_id=%s")
        params.append(division_id)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY b.id DESC"
    c = conn.cursor()
    c.execute(sql, params)
    return fetchall_dict(c)


def create_brand(conn, actor: dict, body: dict) -> int:
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "brand name required")
    division_id = _validate_division(conn, body.get("division_id"))
    c = conn.cursor()
    c.execute("INSERT INTO brands (name, code, description, status, division_id) "
              "VALUES (%s,%s,%s,%s,%s) RETURNING id",
              (name, body.get("code"), body.get("description"),
               body.get("status") or "active", division_id))
    bid = c.fetchone()[0]
    conn.commit()
    log_action(conn, actor.get("id"), "brand.create", "brand", bid, {"name": name}, actor=actor.get("name"))
    return bid


def update_brand(conn, actor: dict, bid: int, body: dict) -> None:
    if "division_id" in body:
        _validate_division(conn, body.get("division_id"))
    fields = ["name", "code", "description", "status", "division_id"]
    sets, params = [], []
    for f in fields:
        if f in body and body[f] is not None:
            sets.append(f"{f}=%s")
            params.append(body[f])
    if not sets:
        raise HTTPException(400, "Nothing to update")
    params.append(bid)
    conn.cursor().execute(f"UPDATE brands SET {', '.join(sets)} WHERE id=%s", params)
    conn.commit()
    log_action(conn, actor.get("id"), "brand.update", "brand", bid, actor=actor.get("name"))


def delete_brand(conn, actor: dict, bid: int) -> None:
    c = conn.cursor()
    c.execute("SELECT id FROM campaigns WHERE brand_id=%s LIMIT 1", (bid,))
    if c.fetchone():
        raise HTTPException(409, "brand is used by campaigns")
    c.execute("DELETE FROM brands WHERE id=%s", (bid,))
    conn.commit()
    log_action(conn, actor.get("id"), "brand.delete", "brand", bid, actor=actor.get("name"))


# ── Divisions ───────────────────────────────────────────────────────────────

import re as _re


def slugify(value: str) -> str:
    """URL-safe segment for a division login link: 'Cardio Care' -> 'cardio-care'."""
    out = _re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return out or "division"


def _unique_slug(conn, slug: str, exclude_id: int = None) -> str:
    """Ensure the slug is unique within the tenant, suffixing -2, -3 ... if not."""
    base, n = slug, 1
    c = conn.cursor()
    while True:
        if exclude_id:
            c.execute("SELECT id FROM divisions WHERE lower(slug)=lower(%s) AND id<>%s",
                      (slug, exclude_id))
        else:
            c.execute("SELECT id FROM divisions WHERE lower(slug)=lower(%s)", (slug,))
        if not c.fetchone():
            return slug
        n += 1
        slug = f"{base}-{n}"


def list_divisions(conn, q: str = "", status: str = "") -> list[dict]:
    sql = """SELECT d.*,
             (SELECT count(*) FROM campaigns c WHERE c.division_id=d.id) AS campaign_count,
             (SELECT count(*) FROM brands b WHERE b.division_id=d.id) AS brand_count,
             (SELECT count(*) FROM users u WHERE u.division_id=d.id) AS user_count
             FROM divisions d"""
    where, params = [], []
    if q:
        where.append("(d.name ILIKE %s OR d.code ILIKE %s OR d.description ILIKE %s)")
        params.extend([f"%{q}%"] * 3)
    if status:
        where.append("d.status=%s")
        params.append(status)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY d.name"
    c = conn.cursor()
    c.execute(sql, params)
    return fetchall_dict(c)


def create_division(conn, actor: dict, body: dict) -> int:
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "division name required")
    slug = _unique_slug(conn, slugify(body.get("slug") or name))
    c = conn.cursor()
    try:
        c.execute(
            "INSERT INTO divisions (name, code, description, status, slug) "
            "VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (name, body.get("code"), body.get("description"),
             body.get("status") or "active", slug),
        )
    except Exception:
        conn.rollback()
        raise HTTPException(409, "division name already exists")
    did = c.fetchone()[0]
    conn.commit()
    log_action(conn, actor.get("id"), "division.create", "division", did, {"name": name}, actor=actor.get("name"))
    return did


def update_division(conn, actor: dict, did: int, body: dict) -> None:
    fields = ["name", "code", "description", "status"]
    sets, params = [], []
    for f in fields:
        if f in body and body[f] is not None:
            sets.append(f"{f}=%s")
            params.append(body[f])
    # The slug is the printed login URL, so it only ever changes when the super
    # admin explicitly edits it -- never as a side effect of renaming.
    if body.get("slug") is not None:
        sets.append("slug=%s")
        params.append(_unique_slug(conn, slugify(body["slug"]), exclude_id=did))
    if not sets:
        raise HTTPException(400, "Nothing to update")
    params.append(did)
    conn.cursor().execute(f"UPDATE divisions SET {', '.join(sets)} WHERE id=%s", params)
    conn.commit()
    log_action(conn, actor.get("id"), "division.update", "division", did, actor=actor.get("name"))


def delete_division(conn, actor: dict, did: int) -> None:
    c = conn.cursor()
    c.execute("SELECT id FROM campaigns WHERE division_id=%s LIMIT 1", (did,))
    if c.fetchone():
        raise HTTPException(409, "division has campaigns")
    c.execute("SELECT id FROM users WHERE division_id=%s LIMIT 1", (did,))
    if c.fetchone():
        raise HTTPException(409, "division has users")
    c.execute("SELECT id FROM chemists WHERE division_id=%s LIMIT 1", (did,))
    if c.fetchone():
        raise HTTPException(409, "division has chemists")
    c.execute("DELETE FROM divisions WHERE id=%s", (did,))
    conn.commit()
    log_action(conn, actor.get("id"), "division.delete", "division", did, actor=actor.get("name"))


# ── Campaigns ───────────────────────────────────────────────────────────────

def list_campaigns(conn, q: str = "", status: str = "", active: bool = None,
                   brand_id: int = None, division_id: int = None) -> list[dict]:
    sql = """SELECT c.*, b.name AS brand_name, d.name AS division_name,
             (SELECT count(*) FROM products p WHERE p.campaign_id=c.id) AS product_count,
             (SELECT count(*) FROM pob_activities pa WHERE pa.campaign_id=c.id) AS pob_count
             FROM campaigns c
             LEFT JOIN brands b ON b.id=c.brand_id
             LEFT JOIN divisions d ON d.id=c.division_id"""
    where, params = [], []
    if q:
        where.append("c.name ILIKE %s")
        params.append(f"%{q}%")
    if status:
        where.append("c.status=%s")
        params.append(status)
    if active is not None:
        where.append("c.active=%s")
        params.append(active)
    if brand_id:
        where.append("(c.brand_id=%s OR c.brand_ids ~ ('(^|,)' || %s || '($|,)'))")
        params.extend([brand_id, str(brand_id)])
    if division_id:
        # A campaign belongs to a division either directly, or through the
        # brand it promotes (division -> brands -> campaigns). Matching only
        # campaigns.division_id hid brand-linked campaigns from the very users
        # the division login link is meant to serve.
        where.append("(c.division_id=%s OR b.division_id=%s)")
        params.extend([division_id, division_id])
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY c.id DESC"
    c = conn.cursor()
    c.execute(sql, params)
    rows = fetchall_dict(c)
    for r in rows:
        r["brand_ids"] = _brand_ids_list(r)
        r["brand_names"] = _brand_names(conn, r["brand_ids"])
    return rows


def _brand_ids_list(row: dict) -> list:
    """Parse the comma-separated brand_ids column into an int list."""
    raw = row.get("brand_ids") or ""
    out = []
    for part in str(raw).split(","):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return out


def _brand_names(conn, brand_ids: list) -> list:
    if not brand_ids:
        return []
    c = conn.cursor()
    c.execute("SELECT id, name FROM brands WHERE id = ANY(%s) ORDER BY id", (brand_ids,))
    return [r[1] for r in c.fetchall()]


def _primary_brand_id(body: dict) -> int | None:
    """Resolve the primary brand: explicit brand_id, else first of brand_ids."""
    if body.get("brand_id"):
        return int(body["brand_id"])
    ids = body.get("brand_ids") or []
    if isinstance(ids, str):
        ids = [p.strip() for p in ids.split(",") if p.strip().isdigit()]
    if isinstance(ids, (list, tuple)) and ids:
        first = ids[0]
        return int(first) if str(first).isdigit() else None
    return None


def _normalize_brand_ids(conn, body: dict) -> list:
    """Validate and normalize brand_ids (list or comma string) against brands."""
    raw = body.get("brand_ids")
    if raw is None:
        if body.get("brand_id"):
            return [int(body["brand_id"])]
        return []
    if isinstance(raw, str):
        raw = [p.strip() for p in raw.split(",") if p.strip().isdigit()]
    raw = [int(x) for x in raw if str(x).strip().isdigit()]
    if not raw:
        if body.get("brand_id"):
            return [int(body["brand_id"])]
        return []
    cur = conn.cursor()
    seen, out = set(), []
    for bid in raw:
        if bid in seen:
            continue
        cur.execute("SELECT id FROM brands WHERE id=%s", (bid,))
        if not cur.fetchone():
            raise HTTPException(400, f"invalid brand_id {bid}")
        seen.add(bid)
        out.append(bid)
    return out


def get_campaign(conn, cid: int) -> dict | None:
    c = conn.cursor()
    c.execute("""SELECT c.*, b.name AS brand_name, d.name AS division_name FROM campaigns c
                 LEFT JOIN brands b ON b.id=c.brand_id
                 LEFT JOIN divisions d ON d.id=c.division_id WHERE c.id=%s""", (cid,))
    row = fetchone_dict(c)
    if not row:
        return None
    row["brand_ids"] = _brand_ids_list(row)
    row["brand_names"] = _brand_names(conn, row["brand_ids"])
    c.execute("SELECT * FROM products WHERE campaign_id=%s ORDER BY id", (cid,))
    row["products"] = fetchall_dict(c)
    from .rules import list_rules
    row["rules"] = list_rules(conn, cid)
    return row


def create_campaign(conn, actor: dict, body: dict) -> int:
    import json
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "campaign name required")
    c = conn.cursor()
    if body.get("brand_id"):
        c.execute("SELECT id FROM brands WHERE id=%s", (body["brand_id"],))
        if not c.fetchone():
            raise HTTPException(400, "invalid brand_id")
    if body.get("division_id"):
        c.execute("SELECT id FROM divisions WHERE id=%s", (body["division_id"],))
        if not c.fetchone():
            raise HTTPException(400, "invalid division_id")
    brand_ids = _normalize_brand_ids(conn, body)
    primary = _primary_brand_id(body)
    c.execute(
        """INSERT INTO campaigns (name, brand_id, brand_ids, division_id, division, start_date, end_date,
           active, status, scheme_type, invoice_verification_required, logo_path, banner_path,
           description, terms_conditions, created_by,
           upload_roles, approval_workflow_id, payout_cycle, payout_weekday, payout_month_day,
           auto_verify, auto_verify_confidence, pob_required, notification_rules,
           period_type, grace_days, grace_months, pre_grace_days)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (name, primary, ",".join(map(str, brand_ids)),
         body.get("division_id"),
         body.get("division"), body.get("start_date"), body.get("end_date"), body.get("active", True),
         body.get("status") or "draft", body.get("scheme_type") or "others",
         body.get("invoice_verification_required", True), body.get("logo_path"),
         body.get("banner_path"), body.get("description"), body.get("terms_conditions"),
         actor.get("id"),
         body.get("upload_roles") or "", body.get("approval_workflow_id"),
         body.get("payout_cycle") or "instant", body.get("payout_weekday"),
         body.get("payout_month_day"), body.get("auto_verify", False),
         body.get("auto_verify_confidence", 0.95),
         body.get("pob_required", True),
         json.dumps(body.get("notification_rules") or {}),
         body.get("period_type") or "none", body.get("grace_days") or 15,
         body.get("grace_months") or 0, body.get("pre_grace_days") or 0),
    )
    cid = c.fetchone()[0]
    _sync_campaign_rules(conn, cid, body.get("rules"), actor)
    for p in body.get("products") or []:
        _insert_product(conn, cid, p)
    conn.commit()
    log_action(conn, actor.get("id"), "campaign.create", "campaign", cid,
               {"name": name, "division_id": body.get("division_id"),
                "products": len(body.get("products") or [])}, actor=actor.get("name"))
    return cid


def update_campaign(conn, actor: dict, cid: int, body: dict, request=None) -> None:
    c = conn.cursor()
    c.execute("SELECT * FROM campaigns WHERE id=%s", (cid,))
    before = fetchone_dict(c)
    if not before:
        raise HTTPException(404, "campaign not found")
    if body.get("division_id"):
        c.execute("SELECT id FROM divisions WHERE id=%s", (body["division_id"],))
        if not c.fetchone():
            raise HTTPException(400, "invalid division_id")
    # brand_id is deliberately NOT in this list: the brand_ids block below owns
    # it (and validates it against the brands table). Setting it in both places
    # emitted "SET brand_id=%s, ... , brand_id=%s", which Postgres rejects with
    # "multiple assignments to same column" -- so saving any campaign that had a
    # brand selected failed outright.
    fields = ["name", "division_id", "division", "start_date", "end_date",
              "active", "status", "scheme_type", "invoice_verification_required",
              "logo_path", "banner_path", "description", "terms_conditions",
              "upload_roles", "approval_workflow_id", "payout_cycle", "payout_weekday",
              "payout_month_day", "auto_verify", "auto_verify_confidence", "pob_required", "notification_rules",
              "period_type", "grace_days", "grace_months", "pre_grace_days"]
    sets, params = [], []
    for f in fields:
        if f in body and body[f] is not None:
            sets.append(f"{f}=%s")
            val = body[f]
            if f == "notification_rules":
                import json
                val = json.dumps(val or {})
            params.append(val)
    # brand_ids column is stored as a comma-separated string; also keep the
    # primary brand_id in sync so joins/reports keep working.
    if "brand_ids" in body or "brand_id" in body:
        ids = _normalize_brand_ids(conn, body)
        if ids:
            sets.append("brand_ids=%s")
            params.append(",".join(map(str, ids)))
            sets.append("brand_id=%s")
            params.append(ids[0])
    if not sets and not body.get("products") and not body.get("rules"):
        raise HTTPException(400, "Nothing to update")
    if sets:
        params.append(cid)
        c.execute(f"UPDATE campaigns SET {', '.join(sets)} WHERE id=%s", params)
    _sync_campaign_rules(conn, cid, body.get("rules"), actor)
    _sync_products(conn, cid, body.get("products"))
    conn.commit()
    log_action(conn, actor.get("id"), "campaign.update", "campaign", cid,
               request=request, before=before, after=body, actor=actor.get("name"))


def extend_campaign(conn, actor: dict, cid: int, body: dict, request=None) -> dict:
    from .notify import notify_user
    c = conn.cursor()
    c.execute("SELECT * FROM campaigns WHERE id=%s", (cid,))
    before = fetchone_dict(c)
    if not before:
        raise HTTPException(404, "campaign not found")
    if body.get("end_date"):
        new_end = str(body["end_date"])
        try:
            dt.date.fromisoformat(new_end)
        except ValueError:
            raise HTTPException(400, "end_date must be a YYYY-MM-DD date")
    elif body.get("days"):
        try:
            days = int(body["days"])
        except (TypeError, ValueError):
            raise HTTPException(400, "days must be a number")
        if days <= 0:
            raise HTTPException(400, "days must be positive")
        base = before["end_date"] or dt.date.today().isoformat()
        new_end = (dt.date.fromisoformat(base) + dt.timedelta(days=days)).isoformat()
    else:
        raise HTTPException(400, "provide end_date or days")
    status = before["status"]
    if new_end >= dt.date.today().isoformat() and status in ("completed", "draft", "paused"):
        status = "active"
    c.execute("UPDATE campaigns SET end_date=%s, status=%s WHERE id=%s", (new_end, status, cid))
    conn.commit()
    log_action(conn, actor.get("id"), "campaign.extend", "campaign", cid,
               request=request, before=before, after={"end_date": new_end, "status": status},
               actor=actor.get("name"))
    c.execute("SELECT id FROM users WHERE status='active'")
    for (uid,) in c.fetchall():
        notify_user(conn, uid, "campaign.extended", "Campaign extended",
                    f"{before['name']} is now live until {new_end}.", "campaign", cid)
    return {"ok": True, "end_date": new_end, "status": status}


def delete_campaign(conn, actor: dict, cid: int) -> None:
    c = conn.cursor()
    c.execute("SELECT id FROM pob_activities WHERE campaign_id=%s LIMIT 1", (cid,))
    if c.fetchone():
        raise HTTPException(409, "campaign has POB activities")
    c.execute("DELETE FROM campaigns WHERE id=%s", (cid,))
    conn.commit()
    log_action(conn, actor.get("id"), "campaign.delete", "campaign", cid, actor=actor.get("name"))


# ── Products (inline with campaigns) ────────────────────────────────────────

def _insert_product(conn, campaign_id: int, p: dict) -> int:
    c = conn.cursor()
    c.execute(
        """INSERT INTO products (campaign_id, brand_id, sku, name, strength, pack, ptr, pts, mrp,
           min_quantity, min_pob, max_pob, scheme_eligibility, status, division)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (campaign_id, p.get("brand_id"), p.get("sku"), (p.get("name") or "").strip(),
         p.get("strength"), p.get("pack"), p.get("ptr") or 0, p.get("pts") or 0,
         p.get("mrp") or 0, p.get("min_quantity") or 1, p.get("min_pob") or 0,
         p.get("max_pob"), p.get("scheme_eligibility", True), p.get("status") or "active",
         p.get("division")),
    )
    return c.fetchone()[0]


def _update_product(conn, pid: int, p: dict) -> None:
    fields = ["brand_id", "sku", "name", "strength", "pack", "ptr", "pts", "mrp",
              "min_quantity", "min_pob", "max_pob", "scheme_eligibility", "status", "division"]
    sets, params = [], []
    for f in fields:
        if f in p and p[f] is not None:
            sets.append(f"{f}=%s")
            params.append(p[f])
    if not sets:
        return
    params.append(pid)
    conn.cursor().execute(f"UPDATE products SET {', '.join(sets)} WHERE id=%s", params)


def _sync_products(conn, cid: int, products) -> None:
    """Synchronize a campaign's products from the inline builder. Rows carrying
    an existing id are updated, new rows are inserted, and rows that disappeared
    are deleted -- unless they already have POB activities."""
    if products is None:
        return
    c = conn.cursor()
    c.execute("SELECT id FROM products WHERE campaign_id=%s", (cid,))
    existing = {r[0] for r in c.fetchall()}
    seen = set()
    for p in products:
        pid = p.get("id")
        if pid in existing:
            seen.add(pid)
            _update_product(conn, pid, p)
        else:
            _insert_product(conn, cid, p)
    for pid in existing - seen:
        c.execute("SELECT id FROM pob_activities WHERE product_id=%s LIMIT 1", (pid,))
        if c.fetchone():
            continue
        c.execute("DELETE FROM products WHERE id=%s", (pid,))


def _sync_campaign_rules(conn, cid: int, rules, actor: dict) -> None:
    """Replace a campaign's gratification rules wholesale (builder semantics)."""
    if not rules:
        return
    import json
    c = conn.cursor()
    c.execute("DELETE FROM gratification_rules WHERE campaign_id=%s", (cid,))
    for r in rules or []:
        gift_id = r.get("gift_id")
        if gift_id is not None:
            c.execute("SELECT id FROM gifts WHERE id=%s", (gift_id,))
            if not c.fetchone():
                gift_id = None
        c.execute(
            """INSERT INTO gratification_rules (campaign_id, name, priority, conditions,
               then_action, value, gift_id, active, created_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (cid, r.get("name") or "Rule", r.get("priority") or 0,
             json.dumps(r.get("conditions") or []), r.get("then_action") or "gift",
             r.get("value") or 0, gift_id, r.get("active", True), actor.get("id")),
        )
