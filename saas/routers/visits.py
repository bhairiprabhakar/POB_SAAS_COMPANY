"""
Chemist follow-up visits (15-day cycle) + stock liquidation tracking.

A PSR records a visit against a chemist: how much stock was placed at the
last call (opening_stock), how much was sold (quantity_sold / liquidated),
what is now in hand (current_stock) and any fresh purchase. The `due`
endpoint flags chemists with verified POB activity whose last visit is older
than N days (default 15) or who were never visited.
"""
import datetime

from fastapi import APIRouter, Depends, HTTPException

from ..audit import log_action
from ..db_utils import fetchall_dict, fetchone_dict
from ..deps import TenantContext, require_permission
from ..scoping import visible_user_ids
from ..pagination import PageLimit

router = APIRouter(prefix="/api/v1", tags=["visits"])


def _base_query(extra="", params=()):
    sql = f"""
      SELECT cv.*, ch.name AS chemist_name, ch.shop_name, ch.city, ch.state,
             u.full_name AS recorded_by, cmp.name AS campaign_name
      FROM chemist_visits cv
      LEFT JOIN chemists ch ON ch.id=cv.chemist_id
      LEFT JOIN users u ON u.id=cv.user_id
      LEFT JOIN campaigns cmp ON cmp.id=cv.campaign_id
      {extra} ORDER BY cv.visit_date DESC, cv.id DESC
    """
    return sql, params


@router.get("/visits")
def list_visits(chemist_id: int = None, user_id: int = None, q: str = "",
                from_date: str = "", to_date: str = "",
                ctx: TenantContext = Depends(require_permission("visit.view"))):
    conn = ctx.conn
    where, params = ["1=1"], []
    visible = visible_user_ids(conn, ctx)
    if visible is not None:
        where.append("cv.user_id = ANY(%s)")
        params.append(visible)
    if chemist_id:
        where.append("cv.chemist_id=%s")
        params.append(chemist_id)
    if user_id:
        where.append("cv.user_id=%s")
        params.append(user_id)
    if q:
        where.append("(ch.name ILIKE %s OR ch.shop_name ILIKE %s)")
        params.extend([f"%{q}%", f"%{q}%"])
    if from_date:
        where.append("cv.visit_date>=%s")
        params.append(from_date)
    if to_date:
        where.append("cv.visit_date<=%s")
        params.append(to_date)
    sql, params = _base_query("WHERE " + " AND ".join(where), params)
    sql += " LIMIT 200"
    c = conn.cursor()
    c.execute(sql, params)
    return {"items": fetchall_dict(c)}


@router.get("/visits/summary")
def visits_summary(ctx: TenantContext = Depends(require_permission("visit.view"))):
    """Liquidation / follow-up KPIs for the caller's visible scope."""
    conn = ctx.conn
    visible = visible_user_ids(conn, ctx)
    scope_sql, params = "", ()
    if visible is not None:
        scope_sql = " WHERE cv.user_id = ANY(%s)"
        params = (visible,)
    c = conn.cursor()
    c.execute(
        f"""SELECT count(*), coalesce(sum(cv.opening_stock),0), coalesce(sum(cv.quantity_sold),0),
            coalesce(sum(cv.current_stock),0), coalesce(sum(cv.fresh_purchase),0),
            count(DISTINCT cv.chemist_id)
            FROM chemist_visits cv{scope_sql}""",
        params,
    )
    r = c.fetchone()
    opening, sold, current, fresh, visits, chemists = (r[0], r[1], r[2], r[3], r[4], r[5])
    due, overdue = _due_counts(conn, visible)
    return {
        "visits": visits,
        "chemists_visited": chemists,
        "opening_stock": opening,
        "quantity_sold": sold,
        "current_stock": current,
        "fresh_purchase": fresh,
        "liquidation_pct": round((sold / opening * 100) if opening else 0, 2),
        "due_chemists": due,
        "overdue_chemists": overdue,
    }


@router.get("/visits/due")
def visits_due(days: int = 15, limit: int = PageLimit(default=200),
               ctx: TenantContext = Depends(require_permission("visit.view"))):
    """Chemists with verified POB activity that need a follow-up visit: either
    never visited or last visited more than `days` days ago."""
    conn = ctx.conn
    visible = visible_user_ids(conn, ctx)
    user_clause, params = "", ()
    if visible is not None:
        user_clause = " AND pa.user_id = ANY(%s)"
        params = (visible,)
    c = conn.cursor()
    c.execute(
        f"""SELECT ch.id AS chemist_id, ch.name, ch.shop_name, ch.city, ch.state, ch.mobile,
            count(pa.id) AS pobs, coalesce(sum(pa.pob_amount),0) AS amount,
            max(pa.created_at)::date AS last_pob_date,
            (SELECT max(cv2.visit_date) FROM chemist_visits cv2 WHERE cv2.chemist_id=ch.id) AS last_visit_date
            FROM chemists ch
            JOIN pob_activities pa ON pa.chemist_id=ch.id AND pa.status='verified'
            WHERE pa.created_at >= current_date - INTERVAL '120 days'{user_clause}
            GROUP BY ch.id
            HAVING COALESCE((SELECT max(cv3.visit_date) FROM chemist_visits cv3
                             WHERE cv3.chemist_id=ch.id), current_date) <= current_date - %s""",
        params + (datetime.timedelta(days=days),),
    )
    rows = fetchall_dict(c)
    items = []
    for r in rows:
        last_visit = r["last_visit_date"] or r["last_pob_date"]
        days_due = (datetime.date.today() - (last_visit or datetime.date.today())).days
        r["days_due"] = max(0, days_due)
        r["overdue"] = r["days_due"] > days
        items.append(r)
    items.sort(key=lambda x: -x["days_due"])
    return {"items": items[:limit], "days": days}


@router.get("/visits/chemist/{chemist_id}")
def chemist_visit_history(chemist_id: int,
                          ctx: TenantContext = Depends(require_permission("visit.view"))):
    conn = ctx.conn
    c = conn.cursor()
    c.execute("SELECT id, name, shop_name FROM chemists WHERE id=%s", (chemist_id,))
    chemist = fetchone_dict(c)
    if not chemist:
        raise HTTPException(404, "chemist not found")
    sql, params = _base_query("WHERE cv.chemist_id=%s", (chemist_id,))
    sql += " LIMIT 100"
    c.execute(sql, params)
    chemist["visits"] = fetchall_dict(c)
    return chemist


@router.post("/visits")
def create_visit(body: dict, ctx: TenantContext = Depends(require_permission("visit.manage"))):
    conn = ctx.conn
    c = conn.cursor()
    chemist_id = body.get("chemist_id")
    if not chemist_id:
        raise HTTPException(400, "chemist_id required")
    c.execute("SELECT id FROM chemists WHERE id=%s", (chemist_id,))
    if not c.fetchone():
        raise HTTPException(400, "chemist not found")
    visit_date = body.get("visit_date") or str(datetime.date.today())
    opening = float(body.get("opening_stock") or 0)
    sold = float(body.get("quantity_sold") or 0)
    current = float(body.get("current_stock") or 0)
    fresh = float(body.get("fresh_purchase") or 0)
    c.execute(
        """INSERT INTO chemist_visits (chemist_id, user_id, campaign_id, visit_date,
           opening_stock, quantity_sold, current_stock, fresh_purchase, remarks)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (chemist_id, ctx.user["id"], body.get("campaign_id"), visit_date,
         opening, sold, current, fresh, body.get("remarks")),
    )
    vid = c.fetchone()[0]
    conn.commit()
    log_action(conn, ctx.user["id"], "visit.create", "chemist_visit", vid,
               {"chemist_id": chemist_id, "sold": sold, "current": current})
    return {"ok": True, "id": vid}


@router.put("/visits/{vid}")
def update_visit(vid: int, body: dict, ctx: TenantContext = Depends(require_permission("visit.manage"))):
    conn = ctx.conn
    c = conn.cursor()
    c.execute("SELECT * FROM chemist_visits WHERE id=%s", (vid,))
    before = fetchone_dict(c)
    if not before:
        raise HTTPException(404, "visit not found")
    if before["user_id"] != ctx.user["id"]:
        visible = visible_user_ids(conn, ctx)
        if visible is None or before["user_id"] not in visible:
            raise HTTPException(403, "only the recording PSR or a manager may edit this visit")
    fields = ["chemist_id", "campaign_id", "visit_date", "opening_stock", "quantity_sold",
              "current_stock", "fresh_purchase", "remarks"]
    sets, params = [], []
    for f in fields:
        if f in body and body[f] is not None:
            sets.append(f"{f}=%s")
            params.append(body[f])
    if not sets:
        raise HTTPException(400, "Nothing to update")
    params.append(vid)
    c.execute(f"UPDATE chemist_visits SET {', '.join(sets)} WHERE id=%s", params)
    conn.commit()
    log_action(conn, ctx.user["id"], "visit.update", "chemist_visit", vid)
    return {"ok": True}


@router.delete("/visits/{vid}")
def delete_visit(vid: int, ctx: TenantContext = Depends(require_permission("visit.manage"))):
    conn = ctx.conn
    c = conn.cursor()
    c.execute("SELECT * FROM chemist_visits WHERE id=%s", (vid,))
    before = fetchone_dict(c)
    if not before:
        raise HTTPException(404, "visit not found")
    if before["user_id"] != ctx.user["id"]:
        visible = visible_user_ids(conn, ctx)
        if visible is None or before["user_id"] not in visible:
            raise HTTPException(403, "only the recording PSR or a manager may delete this visit")
    c.execute("DELETE FROM chemist_visits WHERE id=%s", (vid,))
    conn.commit()
    log_action(conn, ctx.user["id"], "visit.delete", "chemist_visit", vid)
    return {"ok": True}


def _due_counts(conn, visible):
    """(due, overdue) chemist counts for the visible scope, 15-day cycle."""
    user_clause, params = "", ()
    if visible is not None:
        user_clause = " AND pa.user_id = ANY(%s)"
        params = (visible,)
    c = conn.cursor()
    c.execute(
        f"""SELECT count(*),
            sum(CASE WHEN COALESCE((SELECT max(v2.visit_date) FROM chemist_visits v2
                                    WHERE v2.chemist_id=ch.id), current_date)
                     < current_date - INTERVAL '15 days' THEN 1 ELSE 0 END)
            FROM chemists ch
            JOIN pob_activities pa ON pa.chemist_id=ch.id AND pa.status='verified'
            WHERE pa.created_at >= current_date - INTERVAL '120 days'{user_clause}""",
        params,
    )
    row = c.fetchone()
    return row[0] or 0, row[1] or 0
