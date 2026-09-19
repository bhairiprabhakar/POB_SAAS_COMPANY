"""
Company-scoped router: settings, dynamic hierarchy, users (incl. Excel bulk
upload), roles and the permission catalog.
"""
import io
import secrets

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from openpyxl import Workbook, load_workbook

from ..audit import log_action
from ..db_utils import fetchall_dict, fetchone_dict
from ..deps import TenantContext, get_tenant_context, require_permission
from ..rbac import list_all_permissions, load_role_permissions
from ..scoping import GLOBAL_ROLES, division_scope
from ..upload_validation import SPREADSHEET_KINDS, UploadValidationError, validate_upload
from ..pagination import PageLimit, PageOffset
from .. import config
from app.security import hash_pw

router = APIRouter(prefix="/api/v1", tags=["company"])


# ── Roles & permissions ─────────────────────────────────────────────────────

@router.get("/permissions")
def permissions(ctx: TenantContext = Depends(require_permission("user.view"))):
    return {"items": list_all_permissions(ctx.conn)}


@router.get("/roles")
def roles(ctx: TenantContext = Depends(require_permission("user.view"))):
    conn = ctx.conn
    c = conn.cursor()
    c.execute("SELECT r.*, count(rp.permission_code) AS perm_count FROM roles r "
              "LEFT JOIN role_permissions rp ON rp.role_id=r.id GROUP BY r.id ORDER BY r.id")
    items = fetchall_dict(c)
    for it in items:
        it["permissions"] = load_role_permissions(conn, it["id"])
        it["global"] = it["name"] in GLOBAL_ROLES
    return {"items": items}


@router.post("/roles")
def create_role(body: dict, ctx: TenantContext = Depends(require_permission("settings.manage"))):
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "role name required")
    conn = ctx.conn
    c = conn.cursor()
    c.execute("SELECT id FROM roles WHERE name=%s", (name,))
    if c.fetchone():
        raise HTTPException(409, "role already exists")
    c.execute("INSERT INTO roles (name, description, data_entry) VALUES (%s,%s,%s) RETURNING id",
              (name, body.get("description"), bool(body.get("data_entry", False))))
    rid = c.fetchone()[0]
    _set_permissions(conn, rid, body.get("permissions") or [])
    log_action(conn, ctx.user["id"], "role.create", "role", rid, {"name": name})
    return {"ok": True, "id": rid}


@router.put("/roles/{rid}")
def update_role(rid: int, body: dict, ctx: TenantContext = Depends(require_permission("settings.manage"))):
    conn = ctx.conn
    c = conn.cursor()
    c.execute("SELECT * FROM roles WHERE id=%s", (rid,))
    if not c.fetchone():
        raise HTTPException(404, "role not found")
    if "name" in body and body["name"]:
        c.execute("UPDATE roles SET name=%s, description=%s, data_entry=%s WHERE id=%s",
                  (body["name"], body.get("description"), bool(body.get("data_entry", False)), rid))
    elif "data_entry" in body:
        c.execute("UPDATE roles SET data_entry=%s WHERE id=%s",
                  (bool(body["data_entry"]), rid))
    if "permissions" in body:
        _set_permissions(conn, rid, body["permissions"])
    conn.commit()
    log_action(conn, ctx.user["id"], "role.update", "role", rid)
    return {"ok": True}


@router.delete("/roles/{rid}")
def delete_role(rid: int, ctx: TenantContext = Depends(require_permission("settings.manage"))):
    conn = ctx.conn
    c = conn.cursor()
    c.execute("SELECT is_system FROM roles WHERE id=%s", (rid,))
    row = c.fetchone()
    if not row:
        raise HTTPException(404, "role not found")
    if row[0]:
        raise HTTPException(409, "system roles cannot be deleted")
    c.execute("SELECT id FROM users WHERE role_id=%s LIMIT 1", (rid,))
    if c.fetchone():
        raise HTTPException(409, "role is assigned to users")
    c.execute("DELETE FROM roles WHERE id=%s", (rid,))
    conn.commit()
    log_action(conn, ctx.user["id"], "role.delete", "role", rid)
    return {"ok": True}


def _set_permissions(conn, rid, perms):
    c = conn.cursor()
    c.execute("DELETE FROM role_permissions WHERE role_id=%s", (rid,))
    for p in perms:
        c.execute(
            "INSERT INTO role_permissions (role_id, permission_code) VALUES (%s,%s) "
            "ON CONFLICT DO NOTHING",
            (rid, p),
        )
    conn.commit()


# ── Dynamic hierarchy ───────────────────────────────────────────────────────

@router.get("/hierarchy/levels")
def hierarchy_levels(ctx: TenantContext = Depends(require_permission("hierarchy.view"))):
    conn = ctx.conn
    c = conn.cursor()
    c.execute("SELECT * FROM hierarchy_levels ORDER BY rank DESC, id")
    levels = fetchall_dict(c)
    for lv in levels:
        c.execute("SELECT count(*) FROM users WHERE hierarchy_level_id=%s", (lv["id"],))
        lv["user_count"] = c.fetchone()[0]
    return {"items": levels}


@router.post("/hierarchy/levels")
def create_level(body: dict, ctx: TenantContext = Depends(require_permission("hierarchy.manage"))):
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "level name required")
    conn = ctx.conn
    c = conn.cursor()
    c.execute("SELECT id FROM hierarchy_levels WHERE name=%s", (name,))
    if c.fetchone():
        raise HTTPException(409, "level already exists")
    rank = body.get("rank")
    if rank is None:
        c.execute("SELECT coalesce(max(rank),0)+1 FROM hierarchy_levels")
        rank = c.fetchone()[0]
    else:
        rank = int(rank)
        c.execute("UPDATE hierarchy_levels SET rank = rank + 1 WHERE rank >= %s", (rank,))
    c.execute(
        "INSERT INTO hierarchy_levels (name, label, rank, parent_level_id) "
        "VALUES (%s,%s,%s,%s) RETURNING id",
        (name, body.get("label") or name, rank, body.get("parent_level_id")),
    )
    lid = c.fetchone()[0]
    conn.commit()
    log_action(conn, ctx.user["id"], "hierarchy.create", "hierarchy_level", lid, {"name": name, "rank": rank})
    return {"ok": True, "id": lid}


@router.put("/hierarchy/levels/{lid}")
def update_level(lid: int, body: dict, ctx: TenantContext = Depends(require_permission("hierarchy.manage"))):
    conn = ctx.conn
    c = conn.cursor()
    c.execute("SELECT * FROM hierarchy_levels WHERE id=%s", (lid,))
    cur = c.fetchone()
    if not cur:
        raise HTTPException(404, "level not found")
    old_rank = cur[3] if isinstance(cur, tuple) else (cur["rank"] if isinstance(cur, dict) else None)
    if old_rank is None:
        c.execute("SELECT rank FROM hierarchy_levels WHERE id=%s", (lid,))
        old_rank = c.fetchone()[0]
    name = body.get("name")
    if name:
        c.execute("SELECT id FROM hierarchy_levels WHERE name=%s AND id<>%s", (name, lid))
        if c.fetchone():
            raise HTTPException(409, "level name already in use")
        c.execute("UPDATE hierarchy_levels SET name=%s WHERE id=%s", (name, lid))
    if "parent_level_id" in body and body["parent_level_id"] is not None:
        c.execute("UPDATE hierarchy_levels SET parent_level_id=%s WHERE id=%s", (body["parent_level_id"], lid))
    if "label" in body and body["label"] is not None:
        c.execute("UPDATE hierarchy_levels SET label=%s WHERE id=%s", (body["label"], lid))
    if "active" in body and body["active"] is not None:
        c.execute("UPDATE hierarchy_levels SET active=%s WHERE id=%s", (body["active"], lid))
    if "rank" in body and body["rank"] is not None:
        new_rank = int(body["rank"])
        if new_rank != old_rank:
            if new_rank > old_rank:
                c.execute("UPDATE hierarchy_levels SET rank = rank - 1 WHERE rank > %s AND rank <= %s AND id <> %s",
                          (old_rank, new_rank, lid))
            else:
                c.execute("UPDATE hierarchy_levels SET rank = rank + 1 WHERE rank >= %s AND rank < %s AND id <> %s",
                          (new_rank, old_rank, lid))
            c.execute("UPDATE hierarchy_levels SET rank=%s WHERE id=%s", (new_rank, lid))
    conn.commit()
    log_action(conn, ctx.user["id"], "hierarchy.update", "hierarchy_level", lid)
    return {"ok": True}


@router.delete("/hierarchy/levels/{lid}")
def delete_level(lid: int, ctx: TenantContext = Depends(require_permission("hierarchy.manage"))):
    conn = ctx.conn
    c = conn.cursor()
    c.execute("SELECT id, rank FROM hierarchy_levels WHERE id=%s", (lid,))
    row = c.fetchone()
    if not row:
        raise HTTPException(404, "level not found")
    del_rank = row[1] if isinstance(row, tuple) else row["rank"]
    c.execute("SELECT id FROM users WHERE hierarchy_level_id=%s LIMIT 1", (lid,))
    if c.fetchone():
        raise HTTPException(409, "level has users assigned")
    c.execute("SELECT id FROM hierarchy_levels WHERE parent_level_id=%s LIMIT 1", (lid,))
    if c.fetchone():
        raise HTTPException(409, "level has child levels -- re-parent them first")
    c.execute("DELETE FROM hierarchy_levels WHERE id=%s", (lid,))
    c.execute("UPDATE hierarchy_levels SET rank = rank - 1 WHERE rank > %s", (del_rank,))
    conn.commit()
    log_action(conn, ctx.user["id"], "hierarchy.delete", "hierarchy_level", lid)
    return {"ok": True}


@router.get("/hierarchy/tree")
def hierarchy_tree(ctx: TenantContext = Depends(require_permission("hierarchy.view"))):
    conn = ctx.conn
    c = conn.cursor()
    c.execute("SELECT * FROM hierarchy_levels ORDER BY rank DESC, id")
    levels = fetchall_dict(c)
    c.execute("""SELECT id, full_name, username, hierarchy_level_id, parent_id, role_id, status
                 FROM users ORDER BY id""")
    users = fetchall_dict(c)
    by_id = {lv["id"]: {**lv, "children": []} for lv in levels}
    roots = []
    for lv in levels:
        node = by_id[lv["id"]]
        node["users"] = [u for u in users if u["hierarchy_level_id"] == lv["id"]]
        parent = lv.get("parent_level_id")
        if parent and parent in by_id:
            by_id[parent]["children"].append(node)
        else:
            roots.append(node)
    return {"items": roots}


# ── Users ───────────────────────────────────────────────────────────────────

def _row(c):
    return fetchone_dict(c)


@router.get("/users")
def list_users(q: str = "", level_id: int = None, role_id: int = None,
               status: str = "", limit: int = PageLimit(default=200), offset: int = PageOffset(),
               ctx: TenantContext = Depends(require_permission("user.view"))):
    conn = ctx.conn
    c = conn.cursor()
    sql = """SELECT u.*, h.name AS hierarchy_level_name, h.rank AS hierarchy_rank,
             r.name AS role_name, p.full_name AS parent_name, d.name AS division_name
             FROM users u
             LEFT JOIN hierarchy_levels h ON h.id=u.hierarchy_level_id
             LEFT JOIN roles r ON r.id=u.role_id
             LEFT JOIN users p ON p.id=u.parent_id
             LEFT JOIN divisions d ON d.id=u.division_id"""
    where, params = [], []
    if q:
        where.append("(u.full_name ILIKE %s OR u.username ILIKE %s OR u.email ILIKE %s OR u.mobile ILIKE %s)")
        params.extend([f"%{q}%"] * 4)
    if level_id:
        where.append("u.hierarchy_level_id=%s")
        params.append(level_id)
    if role_id:
        where.append("u.role_id=%s")
        params.append(role_id)
    if status:
        where.append("u.status=%s")
        params.append(status)
    div = division_scope(conn, ctx)
    if div:
        where.append("u.division_id=%s")
        params.append(div)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY h.rank DESC NULLS LAST, u.id"
    count_sql = "SELECT count(*) FROM users u" + (" WHERE " + " AND ".join(where) if where else "")
    c.execute(count_sql, params)
    total = c.fetchone()[0]
    sql += " LIMIT %s OFFSET %s"
    c.execute(sql, params + [limit, offset])
    items = fetchall_dict(c)
    for it in items:
        it.pop("password", None)
    return {"items": items, "total": total}


@router.get("/users/{uid}")
def get_user(uid: int, ctx: TenantContext = Depends(require_permission("user.view"))):
    conn = ctx.conn
    c = conn.cursor()
    c.execute("""SELECT u.*, h.name AS hierarchy_level_name, r.name AS role_name,
                 p.full_name AS parent_name, d.name AS division_name FROM users u
                 LEFT JOIN hierarchy_levels h ON h.id=u.hierarchy_level_id
                 LEFT JOIN roles r ON r.id=u.role_id
                 LEFT JOIN users p ON p.id=u.parent_id
                 LEFT JOIN divisions d ON d.id=u.division_id WHERE u.id=%s""", (uid,))
    user = _row(c)
    if not user:
        raise HTTPException(404, "user not found")
    div = division_scope(conn, ctx)
    if div and user.get("division_id") != div:
        raise HTTPException(404, "user not found")
    user.pop("password", None)
    return user


@router.post("/users")
def create_user(body: dict, ctx: TenantContext = Depends(require_permission("user.manage"))):
    conn = ctx.conn
    c = conn.cursor()
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not username or not body.get("full_name"):
        raise HTTPException(400, "username and full_name required")
    if len(password) < 6:
        raise HTTPException(400, "password must be at least 6 characters")
    c.execute("SELECT id FROM users WHERE username=%s", (username,))
    if c.fetchone():
        raise HTTPException(409, "username already exists")
    _enforce_user_limit(conn, ctx)
    division_id = body.get("division_id")
    division = body.get("division")
    div = division_scope(conn, ctx)
    if div:
        # explicit conflicting division -> reject rather than silently move
        req_div = body.get("division_id")
        try:
            req_div = int(req_div) if req_div is not None else None
        except (TypeError, ValueError):
            req_div = None
        if req_div is not None and req_div != div:
            raise HTTPException(403, "You are not authorized to create users for this division")
        division_id = div  # division admins create users inside their division
    if body.get("role_id") and div and _is_global_role(conn, body.get("role_id")):
        raise HTTPException(403, "You are not authorized to assign that role")
    if division_id:
        c.execute("SELECT name FROM divisions WHERE id=%s", (division_id,))
        row = c.fetchone()
        if not row:
            raise HTTPException(400, "invalid division_id")
        division = row[0]
    c.execute(
        """INSERT INTO users (username, password, full_name, email, mobile, employee_id,
           division, division_id, hierarchy_level_id, role_id, parent_id, region, area, territory, status)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (username, hash_pw(password), body["full_name"], body.get("email"), body.get("mobile"),
         body.get("employee_id"), division, division_id, body.get("hierarchy_level_id"),
         body.get("role_id"), body.get("parent_id"), body.get("region"), body.get("area"),
         body.get("territory"), body.get("status") or "active"),
    )
    uid = c.fetchone()[0]
    conn.commit()
    from ..user_index import sync_user
    sync_user(ctx.claims.get("tenant_db") or "", username, uid,
              email=body.get("email"), mobile=body.get("mobile"))
    log_action(conn, ctx.user["id"], "user.create", "user", uid, {"username": username})
    return get_user(uid, ctx)


def _enforce_user_limit(conn, ctx):
    # Per-division user caps no longer come from a subscriptions table; a fixed
    # safety ceiling is enforced to keep any single division from growing without
    # bound. Raise/wire the limit via the division's row if that's needed later.
    limit = 100
    c = conn.cursor()
    c.execute("SELECT count(*) FROM users")
    count = c.fetchone()[0]
    if count >= limit:
        raise HTTPException(409, f"User limit ({limit}) reached for this division")


def _would_create_parent_cycle(conn, uid: int, new_parent_id) -> bool:
    """True if assigning users[uid].parent_id = new_parent_id closes a cycle.

    Walking the chain upwards from the new parent must never reach the target
    user; otherwise scoping._descendants (a stack DFS) would loop forever and
    hang every hierarchy-scoped request.
    """
    if new_parent_id == uid:
        return True
    c = conn.cursor()
    seen = set()
    node = new_parent_id
    while node is not None and node not in seen:
        seen.add(node)
        if node == uid:
            return True
        c.execute("SELECT parent_id FROM users WHERE id=%s", (node,))
        row = c.fetchone()
        node = row[0] if row else None
    return False


@router.put("/users/{uid}")
def update_user(uid: int, body: dict, ctx: TenantContext = Depends(require_permission("user.manage"))):
    conn = ctx.conn
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE id=%s", (uid,))
    row = fetchone_dict(c)
    if not row:
        raise HTTPException(404, "user not found")
    current_role_id = row.get("role_id")
    if body.get("division_id"):
        c.execute("SELECT name FROM divisions WHERE id=%s", (body["division_id"],))
        row = c.fetchone()
        if row:
            body.setdefault("division", row[0])
        else:
            raise HTTPException(400, "invalid division_id")
    div = division_scope(conn, ctx)
    if div:
        # a division-scoped actor may only manage users inside their division
        c.execute("SELECT division_id FROM users WHERE id=%s", (uid,))
        row = c.fetchone()
        if not row or row[0] != div:
            raise HTTPException(403, "user does not belong to your division")
        # division admins cannot move users between divisions
        body["division_id"] = div
        if "division" not in body:
            c.execute("SELECT name FROM divisions WHERE id=%s", (div,))
            row = c.fetchone()
            body["division"] = row[0] if row else None
    fields = ["full_name", "email", "mobile", "employee_id", "division", "division_id", "hierarchy_level_id",
              "role_id", "parent_id", "region", "area", "territory", "status"]
    sets, params = [], []
    for f in fields:
        if f in body and body[f] is not None:
            sets.append(f"{f}=%s")
            params.append(body[f])
    if body.get("password"):
        sets.append("password=%s")
        params.append(hash_pw(body["password"]))
    if body.get("parent_id") is not None and _would_create_parent_cycle(conn, uid, body["parent_id"]):
        raise HTTPException(400, "parent_id would create a reporting cycle")
    if uid == ctx.user.get("id") and any(f in body for f in
                                         ("role_id", "parent_id", "hierarchy_level_id", "division_id", "status")):
        raise HTTPException(400, "you cannot change your own role, reporting line, division or status via this endpoint")
    if "role_id" in body:
        c.execute("SELECT id FROM roles WHERE id=%s", (body["role_id"],))
        if not c.fetchone():
            raise HTTPException(400, "invalid role_id")
        if div and _is_global_role(conn, body["role_id"]) and body["role_id"] != current_role_id:
            raise HTTPException(403, "You are not authorized to assign that role")
    if div and body.get("parent_id") is not None:
        c.execute("SELECT division_id FROM users WHERE id=%s", (body["parent_id"],))
        prow = c.fetchone()
        if prow and prow[0] != div:
            raise HTTPException(403, "parent user must be in your division")
    if not sets:
        raise HTTPException(400, "Nothing to update")
    params.append(uid)
    c.execute(f"UPDATE users SET {', '.join(sets)} WHERE id=%s", params)
    conn.commit()
    from ..user_index import sync_user
    c.execute("SELECT username, email, mobile FROM users WHERE id=%s", (uid,))
    u = c.fetchone()
    if u:
        sync_user(ctx.claims.get("tenant_db") or "", u[0], uid, email=u[1], mobile=u[2])
    log_action(conn, ctx.user["id"], "user.update", "user", uid)
    return get_user(uid, ctx)


@router.post("/users/{uid}/reset-password")
def reset_user_password(uid: int, ctx: TenantContext = Depends(require_permission("user.manage"))):
    """Reset an employee's password to a random temporary one.

    The affected account is flagged must_change_password so the user is forced to
    choose their own password at next sign-in, and all existing sessions are
    revoked. The temporary password is returned exactly once.
    """
    conn = ctx.conn
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE id=%s", (uid,))
    user = fetchone_dict(c)
    if not user:
        raise HTTPException(404, "user not found")
    div = division_scope(conn, ctx)
    if div and user.get("division_id") != div:
        raise HTTPException(403, "user does not belong to your division")
    if uid == ctx.user.get("id"):
        raise HTTPException(400, "you cannot reset your own password")
    from ..provision import generate_temp_password
    from ..security import revoke_all_for_user
    temp = generate_temp_password()
    c.execute("UPDATE users SET password=%s, must_change_password=TRUE WHERE id=%s",
              (hash_pw(temp), uid))
    revoke_all_for_user(conn, uid)
    conn.commit()
    log_action(conn, ctx.user["id"], "user.reset_password", "user", uid,
               {"username": user.get("username")})
    return {"ok": True, "temp_password": temp, "must_change_password": True}


@router.delete("/users/{uid}")
def delete_user(uid: int, ctx: TenantContext = Depends(require_permission("user.manage"))):
    if uid == ctx.user["id"]:
        raise HTTPException(409, "cannot delete your own account")
    conn = ctx.conn
    div = division_scope(conn, ctx)
    if div:
        c = conn.cursor()
        c.execute("SELECT 1 FROM users WHERE id=%s AND division_id=%s", (uid, div))
        if not c.fetchone():
            raise HTTPException(404, "user not found")
    c = conn.cursor()
    c.execute("UPDATE users SET status='inactive' WHERE id=%s", (uid,))
    conn.commit()
    log_action(conn, ctx.user["id"], "user.deactivate", "user", uid)
    return {"ok": True}


@router.post("/users/{uid}/offboard")
def offboard_user(uid: int, body: dict = None,
                  ctx: TenantContext = Depends(require_permission("user.manage"))):
    """Deactivate a user, optionally moving their direct reports to a new
    manager first. Offboarding a manager previously left their reports'
    parent_id pointing at a now-inactive account with no UI path to fix it
    in bulk — this closes that gap. `reassign_to` moves every active direct
    report to the given manager; `force` skips reassignment and leaves any
    reports pointing at the deactivated user (matches the old DELETE
    behaviour, for callers who explicitly want that)."""
    if uid == ctx.user["id"]:
        raise HTTPException(409, "cannot deactivate your own account")
    body = body or {}
    reassign_to = body.get("reassign_to")
    force = bool(body.get("force"))
    conn = ctx.conn
    c = conn.cursor()
    div = division_scope(conn, ctx)
    if div:
        c.execute("SELECT 1 FROM users WHERE id=%s AND division_id=%s", (uid, div))
        if not c.fetchone():
            raise HTTPException(404, "user not found")
    c.execute("SELECT count(*) FROM users WHERE parent_id=%s AND status='active'", (uid,))
    report_count = c.fetchone()[0]
    if report_count and not reassign_to and not force:
        raise HTTPException(409, f"This user manages {report_count} active report(s) — "
                                  "choose someone to reassign them to, or deactivate anyway")
    reassigned = 0
    if reassign_to:
        reassign_to = int(reassign_to)
        if reassign_to == uid:
            raise HTTPException(400, "cannot reassign a team to the person being offboarded")
        c.execute("SELECT id, status, division_id FROM users WHERE id=%s", (reassign_to,))
        target = c.fetchone()
        if not target:
            raise HTTPException(400, "invalid reassign_to user")
        if div and target[2] != div:
            raise HTTPException(403, "the new manager must be in your division")
        if target[1] != "active":
            raise HTTPException(400, "the new manager must be an active user")
        # Excludes reassign_to itself: if the promoted person was one of the
        # offboarded user's own direct reports, this must not re-point them
        # to themselves.
        c.execute("UPDATE users SET parent_id=%s WHERE parent_id=%s AND id != %s",
                  (reassign_to, uid, reassign_to))
        reassigned = c.rowcount
    c.execute("UPDATE users SET status='inactive' WHERE id=%s", (uid,))
    conn.commit()
    log_action(conn, ctx.user["id"], "user.offboard", "user", uid,
              {"reassigned_reports": reassigned, "reassigned_to": reassign_to})
    return {"ok": True, "reassigned": reassigned}


@router.post("/users/bulk-upload")
async def bulk_upload_users(file: UploadFile = File(...),
                            ctx: TenantContext = Depends(require_permission("user.manage"))):
    """Excel upload of users. Expected columns:
    username, full_name, password, email, mobile, employee_id, division,
    hierarchy_level, role, parent_username, region, area, territory.

    Leave `password` blank and the API generates a secure random temporary
    password per user (returned in `generated`) instead of using a known
    default. Never store real passwords in the spreadsheet."""
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
    generated = []
    user_ids = {}
    user_contacts = {}
    user_divisions = {}  # username -> division_id (used to block cross-division parents)

    def col(d, name):
        v = d.get(name)
        return str(v).strip() if v is not None else ""

    # Pass 1: insert every row (parents resolved in pass 2 so row order is free)
    for i, row in enumerate(rows, start=2):
        if not row or all(v is None or str(v).strip() == "" for v in row):
            continue
        d = {headers[j]: (row[j] if j < len(row) else None) for j in range(len(headers))}
        username = col(d, "username")
        full_name = col(d, "full_name")
        if not username or not full_name:
            errors.append(f"row {i}: username and full_name required")
            continue
        try:
            c.execute("SELECT id FROM users WHERE username=%s", (username,))
            if c.fetchone():
                errors.append(f"row {i}: username {username} already exists")
                continue
            level_id = _resolve_level(conn, d.get("hierarchy_level"))
            role_id = _resolve_role(conn, d.get("role"))
            if d.get("hierarchy_level") and not level_id:
                errors.append(f"row {i}: unknown hierarchy_level '{col(d, 'hierarchy_level')}' (use MR/ASM/RSM/SM/ZSM/NSM/HO)")
            if d.get("role") and not role_id:
                errors.append(f"row {i}: unknown role '{col(d, 'role')}'")
            act_div = division_scope(conn, ctx)
            uploaded_div_id = _resolve_division_id(conn, d.get("division")) if col(d, "division") else None
            if col(d, "division") and not uploaded_div_id:
                errors.append(f"row {i}: unknown division '{col(d, 'division')}'")
                continue
            if act_div:
                # a division-scoped actor is bound to their own division: the
                # Excel `division` column must not move users across divisions
                if uploaded_div_id and uploaded_div_id != act_div:
                    errors.append(f"row {i}: You are not authorized to create users for this division")
                    continue
                division_id = act_div
                division = _division_name(conn, act_div)
            else:
                division_id = uploaded_div_id
                division = _resolve_division(conn, d.get("division"))
            if act_div and role_id and _is_global_role(conn, role_id):
                errors.append(f"row {i}: You are not authorized to assign the '{col(d, 'role')}' role")
                continue
            must_change = not bool(col(d, "password"))
            password = col(d, "password") or secrets.token_hex(6)
            c.execute(
                """INSERT INTO users (username, password, full_name, email, mobile, employee_id,
                   division, division_id, hierarchy_level_id, role_id, region, area, territory,
                   must_change_password)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (username, hash_pw(password), full_name, d.get("email"), col(d, "mobile"),
                 col(d, "employee_id"), division, division_id, level_id, role_id,
                 d.get("region"), d.get("area"), d.get("territory"), must_change),
            )
            user_ids[username] = c.fetchone()[0]
            user_divisions[username] = division_id
            user_contacts[username] = (d.get("email"), col(d, "mobile"))
            created += 1
            if not col(d, "password"):
                generated.append({"username": username, "full_name": full_name,
                                  "temp_password": password, "must_change_password": True})
        except Exception as exc:
            errors.append(f"row {i}: {exc}")

    # Pass 2: link managers once everyone exists
    for i, row in enumerate(rows, start=2):
        if not row or all(v is None or str(v).strip() == "" for v in row):
            continue
        d = {headers[j]: (row[j] if j < len(row) else None) for j in range(len(headers))}
        username = col(d, "username")
        parent = col(d, "parent_username")
        if not username or not parent or username not in user_ids:
            continue
        pid = user_ids.get(parent)
        if pid is None:
            errors.append(f"row {i}: parent_username '{parent}' not found in file or system")
            continue
        child_div = user_divisions.get(username)
        if child_div is not None:
            c.execute("SELECT division_id FROM users WHERE id=%s", (pid,))
            prow = c.fetchone()
            if prow and prow[0] != child_div:
                errors.append(f"row {i}: parent_username '{parent}' is in a different division")
                continue
        c.execute("UPDATE users SET parent_id=%s WHERE id=%s", (pid, user_ids[username]))

    conn.commit()
    from ..user_index import sync_user
    tenant_db = ctx.claims.get("tenant_db") or ""
    for uname, uid in user_ids.items():
        email, mobile = user_contacts.get(uname, (None, None))
        sync_user(tenant_db, uname, uid, email=email, mobile=mobile)
    log_action(conn, ctx.user["id"], "user.bulk_upload", "user", None, {"created": created, "errors": len(errors)})
    return {"created": created, "errors": errors, "generated": generated}


def _resolve_level(conn, value):
    if not value:
        return None
    value = str(value).strip()
    c = conn.cursor()
    c.execute("SELECT id FROM hierarchy_levels WHERE lower(name)=lower(%s) OR lower(label)=lower(%s)", (value, value))
    row = c.fetchone()
    return row[0] if row else None


def _resolve_role(conn, value):
    if not value:
        return None
    value = str(value).strip()
    c = conn.cursor()
    c.execute("SELECT id FROM roles WHERE lower(name)=lower(%s)", (value,))
    row = c.fetchone()
    return row[0] if row else None


def _is_global_role(conn, role_id) -> bool:
    """True when role_id names a platform-wide role.

    The roles table has no scope column, so the known platform role names are
    the source of truth: a division-scoped actor must never be able to hand
    out a role that sees every division's data (campaignos_admin, verifier,
    auditor, finance)."""
    if not role_id:
        return False
    c = conn.cursor()
    c.execute("SELECT lower(name) FROM roles WHERE id=%s", (role_id,))
    row = c.fetchone()
    return bool(row and row[0] in GLOBAL_ROLES)


def _division_name(conn, division_id):
    if not division_id:
        return None
    c = conn.cursor()
    c.execute("SELECT name FROM divisions WHERE id=%s", (division_id,))
    row = c.fetchone()
    return row[0] if row else None


def _resolve_parent(conn, value):
    if not value:
        return None
    value = str(value).strip()
    c = conn.cursor()
    c.execute("SELECT id FROM users WHERE username=%s", (value,))
    row = c.fetchone()
    return row[0] if row else None


def _resolve_division(conn, value):
    if not value:
        return None
    value = str(value).strip()
    c = conn.cursor()
    c.execute("SELECT name FROM divisions WHERE lower(name)=lower(%s) OR lower(code)=lower(%s)", (value, value))
    row = c.fetchone()
    return row[0] if row else value


def _resolve_division_id(conn, value):
    if not value:
        return None
    value = str(value).strip()
    c = conn.cursor()
    c.execute("SELECT id FROM divisions WHERE lower(name)=lower(%s) OR lower(code)=lower(%s)", (value, value))
    row = c.fetchone()
    return row[0] if row else None


@router.get("/hierarchy/bulk-template")
def bulk_template(ctx: TenantContext = Depends(require_permission("user.manage"))):
    wb = Workbook()
    ws = wb.active
    ws.title = "Users"
    ws.append(["username", "full_name", "password", "email", "mobile", "employee_id",
               "division", "hierarchy_level", "role", "parent_username", "region", "area", "territory"])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    from fastapi.responses import StreamingResponse
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": "attachment; filename=users_template.xlsx"})


@router.get("/my-division")
def my_division(ctx: TenantContext = Depends(require_permission("dashboard.view"))):
    """The tenant's own division profile plus headline counts — the data behind
    the layout's 'My Division' page."""
    c = ctx.conn.cursor()
    c.execute("SELECT * FROM divisions ORDER BY id LIMIT 1")
    div = fetchone_dict(c)
    if not div:
        raise HTTPException(404, "Division not set up")

    def _count(sql):
        c.execute(sql)
        return int((c.fetchone() or [0])[0] or 0)

    div["user_count"] = _count("SELECT count(*) FROM users")
    div["active_user_count"] = _count("SELECT count(*) FROM users WHERE status='active'")
    div["campaign_count"] = _count("SELECT count(*) FROM campaigns")
    div["active_campaign_count"] = _count("SELECT count(*) FROM campaigns WHERE status='active'")
    div["chemist_count"] = _count("SELECT count(*) FROM chemists")
    div["pob_count"] = _count("SELECT count(*) FROM pob_activities")
    div["verified_pob_count"] = _count(
        "SELECT count(*) FROM pob_activities WHERE status IN ('verified','approved')")
    div["gratification_count"] = _count("SELECT count(*) FROM gratifications")
    return {"division": div}


@router.get("/teams")
def teams(ctx: TenantContext = Depends(require_permission("user.view"))):
    """Manager-centric team view: every user with reports, plus the size and
    the direct-report list of each team. Field users without reports are
    included as their own one-person team."""
    c = ctx.conn.cursor()
    c.execute(
        """SELECT u.id, u.username, u.full_name, COALESCE(r.name, '—') AS role,
                  COALESCE(u.region, ''), COALESCE(u.area, ''), u.status, u.parent_id
           FROM users u LEFT JOIN roles r ON r.id=u.role_id
           ORDER BY COALESCE(u.parent_id, 0), u.id""")
    rows = fetchall_dict(c)
    by_id = {r["id"]: r for r in rows}
    reports_by: dict = {}
    for r in rows:
        if r["parent_id"]:
            reports_by.setdefault(r["parent_id"], []).append(r)

    teams_out, seen = [], set()
    for pid, members in sorted(reports_by.items()):
        if pid not in by_id:
            continue
        mgr = by_id[pid]
        seen.add(pid)
        for m in members:
            seen.add(m["id"])
        teams_out.append({
            "manager_id": pid, "manager_name": mgr["full_name"],
            "manager_username": mgr["username"], "manager_role": mgr["role"],
            "size": len(members), "members": members,
        })
    for r in rows:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        teams_out.append({
            "manager_id": r["id"], "manager_name": r["full_name"],
            "manager_username": r["username"], "manager_role": r["role"],
            "size": 1, "members": [r],
        })
    teams_out.sort(key=lambda t: t["manager_name"] or "")
    counts = {
        "teams": len(teams_out),
        "managed": len([t for t in teams_out if t["size"] > 1]),
        "members": len(rows),
    }
    return {"teams": teams_out, "counts": counts}
