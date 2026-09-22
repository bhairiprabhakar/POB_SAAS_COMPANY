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


def _validate_custom_fields(conn, division_id, custom_fields: dict) -> None:
    """Reject any custom_fields key that isn't an active campaign-field
    template for this campaign's division -- defends against stale/garbage
    field_keys (e.g. from a deactivated template) reaching the database.
    Unscoped campaigns (no division_id) skip validation entirely, matching
    the "legacy company-wide" permissiveness used elsewhere in this file."""
    if not custom_fields or not division_id:
        return
    c = conn.cursor()
    c.execute(
        "SELECT field_key FROM field_templates "
        "WHERE entity_type='campaign' AND division_id=%s AND active=TRUE",
        (division_id,),
    )
    valid_keys = {r[0] for r in c.fetchall()}
    unknown = [k for k in custom_fields if k not in valid_keys]
    if unknown:
        raise HTTPException(400, f"Unknown custom field(s): {', '.join(unknown)}")


# ── Campaign readiness gate (perspective 7) ─────────────────────────────────

def campaign_readiness(conn) -> dict:
    """Configuration checklist that must be in place before a campaign can be
    created (masters before campaigns). Shared by the tenant create route and
    the super admin create route so the sequence holds everywhere."""
    c = conn.cursor()

    def _count(sql):
        c.execute(sql)
        return int((c.fetchone() or [0])[0] or 0)

    items = [
        {"key": "hierarchy", "label": "Hierarchy levels (designations)",
         "count": _count("SELECT count(*) FROM hierarchy_levels"), "where": "platform"},
        {"key": "employees", "label": "Employees / field users",
         "count": _count("SELECT count(*) FROM users WHERE status='active'"), "where": "platform"},
        {"key": "brands", "label": "Brands",
         "count": _count("SELECT count(*) FROM brands"), "where": "platform"},
        {"key": "chemists", "label": "Chemists (retail network)",
         "count": _count("SELECT count(*) FROM chemists"), "where": "tenant"},
        {"key": "regions", "label": "Regions covered (user territories)",
         "count": _count("SELECT count(DISTINCT region) FROM users WHERE region IS NOT NULL AND region<>''"), "where": "tenant"},
        {"key": "states", "label": "States covered (chemist network)",
         "count": _count("SELECT count(DISTINCT state) FROM chemists WHERE state IS NOT NULL AND state<>''"), "where": "tenant"},
        {"key": "gifts", "label": "Gratification masters (gifts)",
         "count": _count("SELECT count(*) FROM gifts"), "where": "platform"},
    ]
    for i in items:
        i["ready"] = i["count"] > 0
    keyed = {i["key"]: i for i in items}
    # Only brands are truly mandatory for drafting. A campaign starts as a
    # Draft and needs the platform approver's sign-off before it goes live, so
    # chemists (built by MR/ASM on the ground), gifts, hierarchy, and territories
    # are guidance, not gates. Requiring them blocked every division (chemists /
    # gifts are platform-owned and usually 0 when a rollout starts).
    ready = all(keyed[k]["ready"] for k in ("brands",))
    complete = ready and all(keyed[k]["ready"] for k in ("hierarchy", "employees", "regions", "states"))
    return {"ready": ready, "complete": complete, "items": items}


# ── Campaign -> employee assignment (perspective 9) ───────────────────────────

def list_campaign_assignments(conn, cid: int) -> list[dict]:
    """The assignment rule rows for a campaign (decorated with names)."""
    c = conn.cursor()
    c.execute(
        """SELECT ca.*, u.full_name AS employee_name, m.full_name AS manager_name
           FROM campaign_assignments ca
           LEFT JOIN users u ON u.id=ca.employee_id
           LEFT JOIN users m ON m.id=ca.manager_id
           WHERE ca.campaign_id=%s ORDER BY ca.id""",
        (cid,),
    )
    return fetchall_dict(c)


def _assignment_bodies(body: dict) -> list[dict]:
    """Normalize the assignment payload into a list of rule dicts.

    Accepts either a full replacement list ({'rules': [...]}) or the shorthand
    forms the wizard uses:
      {'mode': 'all'}
      {'mode': 'region', 'regions': ['South', ...]}
      {'mode': 'employee', 'employee_ids': [12, 34]}
      {'mode': 'hierarchy', 'manager_id': 5}
    Returns [] when the payload is absent or empty.
    """
    raw = body.get("assignment")
    if raw is None:
        return []
    if isinstance(raw, dict) and raw.get("rules"):
        raw = raw["rules"]
    if not raw:
        return []
    if isinstance(raw, dict):
        mode = str(raw.get("mode") or "").strip()
        if not mode:
            return []
        if mode == "all":
            return [{"mode": "all"}]
        rules = []
        if mode == "region":
            for r in raw.get("regions") or []:
                r = str(r).strip()
                if r:
                    rules.append({"mode": "region", "region": r})
        elif mode == "employee":
            for uid in raw.get("employee_ids") or []:
                if str(uid).strip().lstrip("-").isdigit():
                    rules.append({"mode": "employee", "employee_id": int(uid)})
        elif mode == "hierarchy":
            mid = raw.get("manager_id")
            if mid is not None and str(mid).strip().lstrip("-").isdigit():
                rules.append({"mode": "hierarchy", "manager_id": int(mid)})
        return rules
    rules = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        m = str(r.get("mode") or "").strip()
        if m not in ("all", "region", "employee", "hierarchy"):
            continue
        row = {"mode": m}
        if m == "region" and r.get("region"):
            row["region"] = str(r["region"]).strip()
        if m == "employee" and r.get("employee_id") is not None:
            try:
                row["employee_id"] = int(r["employee_id"])
            except (TypeError, ValueError):
                continue
        if m == "hierarchy" and r.get("manager_id") is not None:
            try:
                row["manager_id"] = int(r["manager_id"])
            except (TypeError, ValueError):
                continue
        if m != "all" and "employee_id" not in row and "manager_id" not in row and "region" not in row:
            continue
        rules.append(row)
    return rules


def set_campaign_assignments(conn, cid: int, body: dict, actor: dict) -> list[dict]:
    """Replace a campaign's assignment rules. Returns the new rule rows."""
    rules = _assignment_bodies(body)
    c = conn.cursor()
    c.execute("DELETE FROM campaign_assignments WHERE campaign_id=%s", (cid,))
    for r in rules:
        c.execute(
            """INSERT INTO campaign_assignments (campaign_id, mode, region, employee_id, manager_id, created_by)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            (cid, r["mode"], r.get("region"), r.get("employee_id"), r.get("manager_id"), actor.get("id")),
        )
    conn.commit()
    log_action(conn, actor.get("id"), "campaign.assign", "campaign", cid,
               {"rules": rules}, actor=actor.get("name"))
    return list_campaign_assignments(conn, cid)


def assigned_user_ids(conn, cid: int) -> list[int] | None:
    """Which tenant user ids may execute this campaign.

    Returns None when the campaign has no assignment rows OR an 'all' rule'
    (open to every eligible employee - the legacy behaviour). Otherwise the
    union of the rule matches:
      region     -> active users with a matching region
      employee   -> that user
      hierarchy  -> the manager plus every subordinate in the reporting tree
    All matches are restricted to the campaign's division when one is set, so a
    Region/Rule can never leak to employees of another division.
    """
    c = conn.cursor()
    c.execute("SELECT division_id, name FROM campaigns WHERE id=%s", (cid,))
    row = c.fetchone()
    if not row:
        return None
    division_id = row[0]
    c.execute("SELECT mode, region, employee_id, manager_id FROM campaign_assignments WHERE campaign_id=%s", (cid,))
    rules = c.fetchall()
    if not rules:
        return None
    if any(mode == "all" for mode, _, _, _ in rules):
        return None

    from .scoping import _descendants

    ids: set[int] = set()
    for mode, region, employee_id, manager_id in rules:
        if mode == "region":
            if division_id is not None:
                c.execute("SELECT id FROM users WHERE status='active' AND region=%s AND division_id=%s",
                          (region, division_id))
            else:
                c.execute("SELECT id FROM users WHERE status='active' AND region=%s", (region,))
            ids.update(r[0] for r in c.fetchall())
        elif mode == "employee" and employee_id:
            if division_id is not None:
                c.execute("SELECT id FROM users WHERE id=%s AND status='active' AND division_id=%s",
                          (employee_id, division_id))
            else:
                c.execute("SELECT id FROM users WHERE id=%s AND status='active'", (employee_id,))
            r = c.fetchone()
            if r:
                ids.add(r[0])
        elif mode == "hierarchy" and manager_id:
            if division_id is not None:
                c.execute("SELECT id FROM users WHERE id=%s AND status='active' AND division_id=%s",
                          (manager_id, division_id))
            else:
                c.execute("SELECT id FROM users WHERE id=%s AND status='active'", (manager_id,))
            if c.fetchone():
                ids.add(manager_id)
                ids.update(uid for uid in _descendants(conn, manager_id, division_id)
                           if uid != manager_id)
    return sorted(ids)


def assignment_summary(conn, cid: int) -> dict:
    """Compact assignment info for list/detail payloads."""
    rules = list_campaign_assignments(conn, cid)
    assigned = assigned_user_ids(conn, cid)
    return {
        "rules": rules,
        "mode": (rules[0]["mode"] if rules else "open"),
        "assigned_count": None if assigned is None else len(assigned),
        "open": assigned is None,
    }


def chemist_eligibility(conn, cid: int, chemist_id: int) -> dict:
    """Does a chemist match the campaign's eligible chemist segments?

    Campaigns may restrict execution to chemists of certain attachment types
    and/or potential categories. Empty segments mean "any chemist". Returns a
    decision dict with per-rule matches.
    """
    c = conn.cursor()
    c.execute("SELECT id FROM campaigns WHERE id=%s", (cid,))
    if not c.fetchone():
        raise HTTPException(404, "campaign not found")
    c.execute("SELECT * FROM chemists WHERE id=%s", (chemist_id,))
    chem = fetchone_dict(c)
    if not chem:
        raise HTTPException(404, "chemist not found")

    att = chem.get("attachment_type") or ""
    pot = chem.get("potential_category") or ""
    st = (chem.get("state") or "").strip().lower()
    c.execute(
        "SELECT eligible_chemist_attachment_types, eligible_chemist_potential_categories, "
        "eligible_states FROM campaigns WHERE id=%s", (cid,),
    )
    types, cats, states = c.fetchone()
    types = [t for t in (types or []) if t]
    cats = [t for t in (cats or []) if t]
    states = [s for s in (states or []) if s]

    match_att = (not types) or att in types
    match_pot = (not cats) or pot in cats
    match_state = (not states) or st in [s.strip().lower() for s in states if s.strip()]
    eligible = match_att and match_pot and match_state
    missing = []
    if not match_att:
        missing.append("attachment_type")
    if not match_pot:
        missing.append("potential_category")
    if not match_state:
        missing.append("state")
    return {
        "eligible": eligible,
        "restricted": bool(types or cats or states),
        "matches": {"attachment_type": match_att, "potential_category": match_pot,
                    "state": match_state},
        "chemist": {"attachment_type": att, "potential_category": pot, "state": st},
        "campaign": {"eligible_chemist_attachment_types": types,
                     "eligible_chemist_potential_categories": cats,
                     "eligible_states": states},
        "reason": ("Chemist does not match the campaign's eligible chemist "
                   f"types/categories/states ({', '.join(missing)})") if missing else None,
    }


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
    sql = """SELECT b.*, d.name AS division_name, d.slug AS division_slug,
             (SELECT count(*) FROM products p WHERE p.brand_id=b.id) AS product_count
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
    code = (body.get("code") or "").strip() or None
    _assert_brand_unique(conn, name=name, code=code, division_id=division_id)
    c.execute("INSERT INTO brands (name, code, description, status, division_id, created_by, updated_by, updated_at) "
              "VALUES (%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP) RETURNING id",
              (name, code, body.get("description"),
               body.get("status") or "active", division_id, actor.get("id"), actor.get("id")))
    bid = c.fetchone()[0]
    conn.commit()
    log_action(conn, actor.get("id"), "brand.create", "brand", bid, {"name": name}, actor=actor.get("name"))
    return bid


def _assert_brand_unique(conn, name: str, code: str = None, division_id: int = None,
                         exclude_id: int = None) -> None:
    """Brand name and code must each be unique within the same division."""
    c = conn.cursor()
    if name:
        if division_id:
            c.execute("SELECT id FROM brands WHERE lower(name)=lower(%s) AND division_id=%s AND id<>%s",
                      (name, division_id, exclude_id if exclude_id else 0))
        else:
            c.execute("SELECT id FROM brands WHERE lower(name)=lower(%s) AND division_id IS NULL AND id<>%s",
                      (name, exclude_id if exclude_id else 0))
        if c.fetchone():
            raise HTTPException(409, "a brand with this name already exists in your division")
    if code:
        if division_id:
            c.execute("SELECT id FROM brands WHERE lower(code)=lower(%s) AND division_id=%s AND id<>%s",
                      (code, division_id, exclude_id if exclude_id else 0))
        else:
            c.execute("SELECT id FROM brands WHERE lower(code)=lower(%s) AND division_id IS NULL AND id<>%s",
                      (code, exclude_id if exclude_id else 0))
        if c.fetchone():
            raise HTTPException(409, "a brand with this code already exists in your division")


def update_brand(conn, actor: dict, bid: int, body: dict) -> None:
    if "division_id" in body:
        _validate_division(conn, body.get("division_id"))
    name = body.get("name")
    if name is not None and not str(name).strip():
        raise HTTPException(400, "brand name cannot be empty")
    fields = ["name", "code", "description", "status", "division_id"]
    sets, params = [], []
    for f in fields:
        if f in body and body[f] is not None:
            sets.append(f"{f}=%s")
            params.append(body[f])
    if body.get("name") is not None:
        _assert_brand_unique(conn, name=str(body["name"]).strip(),
                             code=(body.get("code") or "").strip() or None,
                             division_id=_validate_division(conn, body.get("division_id")) or _current_brand_division(conn, bid),
                             exclude_id=bid)
    elif body.get("code") is not None:
        _assert_brand_unique(conn, name=None, code=str(body["code"]).strip() or None,
                             division_id=_current_brand_division(conn, bid), exclude_id=bid)
    if not sets:
        raise HTTPException(400, "Nothing to update")
    sets.append("updated_by=%s")
    params.append(actor.get("id"))
    sets.append("updated_at=CURRENT_TIMESTAMP")
    params.append(bid)
    conn.cursor().execute(f"UPDATE brands SET {', '.join(sets)} WHERE id=%s", params)
    conn.commit()
    log_action(conn, actor.get("id"), "brand.update", "brand", bid, actor=actor.get("name"))


def _current_brand_division(conn, bid: int):
    c = conn.cursor()
    c.execute("SELECT division_id FROM brands WHERE id=%s", (bid,))
    row = c.fetchone()
    return row[0] if row else None


def delete_brand(conn, actor: dict, bid: int) -> None:
    c = conn.cursor()
    c.execute("SELECT id FROM campaigns WHERE brand_id=%s LIMIT 1", (bid,))
    if c.fetchone():
        raise HTTPException(409, "Brand has historical/campaign usage and cannot be deleted. Deactivate it instead.")
    c.execute("SELECT id FROM products WHERE brand_id=%s LIMIT 1", (bid,))
    if c.fetchone():
        raise HTTPException(409, "Brand has products attached. Deactivate the brand instead of deleting it.")
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
             (SELECT count(*) FROM campaign_products cp WHERE cp.campaign_id=c.id) AS product_count,
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
        r["assignment"] = assignment_summary(conn, r["id"])
        r["custom_fields"] = _loads_custom_fields(r)
        _loads_array_cols(r)
    return rows


def _loads_custom_fields(row: dict) -> dict:
    """fetchone_dict/fetchall_dict re-stringify JSONB columns back to JSON
    text (db_utils._serialize) so callers that want the raw text keep
    getting it; campaign consumers want the parsed object."""
    import json
    v = row.get("custom_fields")
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return {}
    return v or {}


_ARRAY_COLS = ("eligible_states", "eligible_chemist_attachment_types", "eligible_chemist_potential_categories")


def _loads_array_cols(row: dict) -> None:
    """Same re-stringification as _loads_custom_fields, but for the
    campaigns table's native TEXT[] columns (db_utils._serialize json.dumps's
    any list/dict it gets back from psycopg2, arrays included)."""
    import json
    for key in _ARRAY_COLS:
        if key not in row:
            continue
        v = row[key]
        if isinstance(v, str):
            try:
                row[key] = json.loads(v)
            except Exception:
                row[key] = []
        elif v is None:
            row[key] = []


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
    row["custom_fields"] = _loads_custom_fields(row)
    _loads_array_cols(row)
    c.execute("""SELECT cp.min_quantity, cp.min_pob, cp.max_pob, cp.scheme_eligibility,
                 p.*, b.name AS brand_name, b.division_id AS brand_division_id
                 FROM campaign_products cp
                 JOIN products p ON p.id=cp.product_id
                 LEFT JOIN brands b ON b.id=p.brand_id
                 WHERE cp.campaign_id=%s ORDER BY cp.sort_order, cp.product_id""", (cid,))
    row["products"] = fetchall_dict(c)
    from .rules import list_rules
    row["rules"] = list_rules(conn, cid)
    row["assignment"] = assignment_summary(conn, cid)
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
    _validate_custom_fields(conn, body.get("division_id"), body.get("custom_fields") or {})
    brand_ids = _normalize_brand_ids(conn, body)
    primary = _primary_brand_id(body)
    c.execute(
        """INSERT INTO campaigns (name, brand_id, brand_ids, division_id, division, start_date, end_date,
           active, status, scheme_type, invoice_verification_required, logo_path, banner_path,
           description, terms_conditions, created_by,
           upload_roles, approval_workflow_id, payout_cycle, payout_weekday, payout_month_day,
           auto_verify, auto_verify_confidence, pob_required, notification_rules,
           period_type, grace_days, grace_months, pre_grace_days,
           eligible_chemist_attachment_types, eligible_chemist_potential_categories,
           eligible_states, custom_fields)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (name, primary, ",".join(map(str, brand_ids)),
         body.get("division_id"),
         body.get("division"), body.get("start_date"), body.get("end_date"), body.get("active", True),
         "draft", body.get("scheme_type") or "others",
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
         body.get("grace_months") or 0, body.get("pre_grace_days") or 0,
         body.get("eligible_chemist_attachment_types") or [],
         body.get("eligible_chemist_potential_categories") or [],
         body.get("eligible_states") or [],
         json.dumps(body.get("custom_fields") or {})),
    )
    cid = c.fetchone()[0]
    _sync_campaign_rules(conn, cid, body.get("rules"), actor)
    _sync_products(conn, cid, body.get("products"), division_id=body.get("division_id"),
                   actor_id=actor.get("id"))
    if body.get("assignment") is not None:
        set_campaign_assignments(conn, cid, body, actor)
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
    # Campaign lifecycle (perspective 10): only the platform approver may put a
    # campaign in an approved/executable state. Tenant edits can still change
    # draft/completed/paused; active/scheduled/pending_approval/rejected are
    # driven by the submit/approve/reject endpoints.
    pending_status = str(body.get("status") or "")
    if pending_status in ("active", "scheduled", "pending_approval", "rejected", "changes_required") and actor.get("id"):
        raise HTTPException(
            403,
            "Status changes to the approval lifecycle go through Submit/Approve/Reject, not the edit form",
        )
    if body.get("division_id"):
        c.execute("SELECT id FROM divisions WHERE id=%s", (body["division_id"],))
        if not c.fetchone():
            raise HTTPException(400, "invalid division_id")
    if "custom_fields" in body:
        _validate_custom_fields(conn, body.get("division_id") or before.get("division_id"),
                                body.get("custom_fields") or {})
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
              "period_type", "grace_days", "grace_months", "pre_grace_days",
              "eligible_chemist_attachment_types", "eligible_chemist_potential_categories",
              "eligible_states", "custom_fields"]
    sets, params = [], []
    for f in fields:
        if f in body and body[f] is not None:
            sets.append(f"{f}=%s")
            val = body[f]
            if f in ("notification_rules", "custom_fields"):
                import json
                val = json.dumps(val or {})
            if f.startswith("eligible_"):
                val = val or []
            params.append(val)
    # Editing a rejected/changes_required campaign back to draft (the only way
    # to leave those states from the edit form -- Status only offers
    # draft/completed/paused) left the old rejection/changes-required note
    # stored indefinitely, since editing never touched those columns; only an
    # actual resubmit did. Clear them here too so the note doesn't linger
    # confusingly until the next submit.
    if body.get("status") == "draft" and before.get("status") != "draft":
        sets.extend([
            "rejected_at=NULL", "rejected_by=NULL", "rejection_note=NULL",
            "changes_requested_at=NULL", "changes_requested_by=NULL", "changes_required_note=NULL",
        ])
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
    _sync_products(conn, cid, body.get("products"),
                   division_id=body.get("division_id") or before.get("division_id"),
                   actor_id=actor.get("id"))
    if body.get("assignment") is not None:
        set_campaign_assignments(conn, cid, body, actor)
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


# ── Products (division-scoped master catalogue; campaigns link via junction) ─

def _insert_product(conn, p: dict, division_id: int = None, actor_id: int = None) -> int:
    if not p.get("brand_id"):
        # Product Master: brand is REQUIRED (mirrors routers/masters.py's
        # create_product) -- no unbranded products, including from the
        # campaign builder's inline "new product" quick-add.
        raise HTTPException(400, "Product must belong to a brand")
    c = conn.cursor()
    c.execute(
        """INSERT INTO products (brand_id, division_id, sku, name, composition, strength, dosage_form,
           pack, ptr, pts, mrp, gst, status, created_by, updated_by, updated_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP) RETURNING id""",
        (p.get("brand_id"), division_id, p.get("sku"), (p.get("name") or "").strip(),
         p.get("composition"), p.get("strength"), p.get("dosage_form"), p.get("pack"),
         p.get("ptr") or 0, p.get("pts") or 0, p.get("mrp") or 0, p.get("gst") or 0,
         p.get("status") or "active", actor_id, actor_id),
    )
    return c.fetchone()[0]


def _update_product(conn, pid: int, p: dict, actor_id: int = None) -> None:
    fields = ["brand_id", "division_id", "sku", "name", "composition", "strength", "dosage_form",
              "pack", "ptr", "pts", "mrp", "gst", "status"]
    sets, params = [], []
    for f in fields:
        if f in p:
            # Explicit None clears the column; absent keys are left untouched.
            sets.append(f"{f}=%s")
            params.append(p[f])
    if not sets:
        return
    sets.append("updated_by=%s")
    params.append(actor_id)
    sets.append("updated_at=CURRENT_TIMESTAMP")
    params.append(pid)
    conn.cursor().execute(f"UPDATE products SET {', '.join(sets)} WHERE id=%s", params)


_CONSTRAINT_FIELDS = ("min_quantity", "min_pob", "max_pob", "scheme_eligibility")


def _constraint_vals(p: dict) -> dict:
    """The per-campaign POB constraints carried on a product payload, or defaults."""
    if not isinstance(p, dict):
        return {}
    out = {}
    for f in _CONSTRAINT_FIELDS:
        if f in p:
            out[f] = p[f]
    return out


def _sync_products(conn, cid: int, products, division_id: int = None, actor_id: int = None) -> None:
    """Synchronize a campaign's product links from the builder. Rows carrying an
    existing id are linked to the campaign (and updated in place), new rows are
    inserted as division master products, and links that disappeared are removed
    -- master products themselves are never deleted from a campaign sync.
    Per-campaign constraints (min quantity / min/max POB / scheme eligibility)
    ride on the link, so the same master can carry a different threshold per
    campaign."""
    if products is None:
        return
    c = conn.cursor()
    c.execute("SELECT product_id FROM campaign_products WHERE campaign_id=%s", (cid,))
    existing = {r[0] for r in c.fetchall()}
    linked = set()
    for p in products:
        if isinstance(p, dict) and not p.get("id"):
            pid = _insert_product(conn, p, division_id, actor_id)
            _link_product(c, cid, pid, p, list(existing))
            linked.add(pid)
            continue
        pid = p.get("id") if isinstance(p, dict) else p
        if not pid:
            continue
        if division_id:
            _assert_product_in_division(c, pid, division_id)
        linked.add(pid)
        # Existing products are only ever linked with this campaign's own
        # constraints (min/max POB, scheme eligibility) -- the master row
        # itself (brand, PTR/PTS/MRP, ...) is edited on the Products page
        # through routers/masters.py's validated update, never silently
        # rewritten by a campaign save.
        _link_product(c, cid, pid, p if isinstance(p, dict) else {}, list(existing))
    for pid in existing - linked:
        c.execute("DELETE FROM campaign_products WHERE campaign_id=%s AND product_id=%s", (cid, pid))


def _assert_product_in_division(c, pid: int, division_id: int) -> None:
    """An existing product attached to a campaign by id must belong to the
    campaign's own division (directly, or via its brand) -- never attachable
    across divisions through a manipulated request."""
    c.execute("SELECT division_id, brand_id FROM products WHERE id=%s", (pid,))
    row = c.fetchone()
    if not row:
        raise HTTPException(400, f"Product {pid} not found")
    p_division_id, brand_id = row
    if p_division_id == division_id:
        return
    if brand_id:
        c.execute("SELECT id FROM brands WHERE id=%s AND division_id=%s", (brand_id, division_id))
        if c.fetchone():
            return
    raise HTTPException(400, f"Product {pid} does not belong to this campaign's division")


def _link_product(c, cid: int, pid: int, p: dict, existing: list) -> None:
    """Link a product to a campaign and persist the campaign-level constraints
    carried on the payload (falling back to the link defaults)."""
    vals = _constraint_vals(p)
    c.execute(
        """INSERT INTO campaign_products (campaign_id, product_id, sort_order,
           min_quantity, min_pob, max_pob, scheme_eligibility)
           VALUES (%s,%s,0,%s,%s,%s,%s)
           ON CONFLICT (campaign_id, product_id) DO UPDATE SET
             min_quantity=EXCLUDED.min_quantity,
             min_pob=EXCLUDED.min_pob,
             max_pob=EXCLUDED.max_pob,
             scheme_eligibility=EXCLUDED.scheme_eligibility""",
        (cid, pid, vals.get("min_quantity") or 1, vals.get("min_pob") or 0,
         vals.get("max_pob"), vals.get("scheme_eligibility", True)),
    )


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
