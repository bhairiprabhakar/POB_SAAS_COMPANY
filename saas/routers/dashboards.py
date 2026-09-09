"""
Dashboards router -- real-time KPIs for company, campaign, brand, MR,
manager (ASM/RSM/NSM/SM), verification, finance and gift dashboards.

All queries are tenant-scoped (the connection is the tenant's own database)
and hierarchy-scoped: each caller only sees the users they are entitled to
(see saas/scoping.py).
"""
from fastapi import APIRouter, Depends, HTTPException

from ..db_utils import fetchall_dict, fetchone_dict
from ..deps import TenantContext, require_permission
from ..scoping import visible_user_ids

router = APIRouter(prefix="/api/v1/dashboards", tags=["dashboards"])


def _scope(ctx, conn):
    """Return (visible_list|None)."""
    return visible_user_ids(conn, ctx)


def _pob_where(ctx, conn, alias="pa"):
    """SQL fragment + params scoping POB rows to the caller's view."""
    visible = _scope(ctx, conn)
    if visible is None:
        return "", ()
    return f"{alias}.user_id = ANY(%s)", (visible,)


def _kpi(conn, sql, params=()):
    c = conn.cursor()
    c.execute(sql, params)
    row = c.fetchone()
    return row[0] if row else 0


def _win(days, alias, col="created_at"):
    """Trailing-window SQL fragment + params (empty when days=0 = all time)."""
    days = int(days or 0)
    if not days:
        return "", ()
    return f" AND {alias}.{col} >= CURRENT_DATE - %s::interval", (f"{days} days",)


def _where(days, scope_sql, scope_params, alias="pa", col="created_at"):
    """Combine a scope expression + optional trailing window into a WHERE clause
    with params in the right order."""
    wf, wp = _win(days, alias, col)
    exprs, params = [], []
    if scope_sql:
        exprs.append(scope_sql)
        params += list(scope_params)
    if wf:
        exprs.append(wf.lstrip(" AND "))
        params += list(wp)
    if not exprs:
        return "", ()
    return " WHERE " + " AND ".join(exprs), tuple(params)


def _and(days, scope_sql, scope_params, alias="pa", col="created_at"):
    """Like _where but returns an ' AND ...' continuation for queries that
    already carry their own WHERE clause."""
    where, params = _where(days, scope_sql, scope_params, alias, col)
    if not where:
        return "", ()
    return where.replace(" WHERE ", " AND ", 1), params


@router.get("/company")
def company_dashboard(days: int = 0, ctx: TenantContext = Depends(require_permission("dashboard.view"))):
    """Company KPIs. Optional `days` (30/90/180) narrows every metric to the
    trailing window and also returns the *previous* window of the same width
    (`prev`) so the UI can show vs-period deltas on the KPI cards."""
    conn = ctx.conn
    pw, pp = _pob_where(ctx, conn)
    gw = _scope(ctx, conn)
    grat_scope, gp = ("g.user_id = ANY(%s)", (gw,)) if gw else ("", ())
    days = int(days or 0)

    def win(alias, col="created_at"):
        """SQL fragment + params for the selected window (all-time if days=0)."""
        if not days:
            return "", ()
        return f"{alias}.{col} >= CURRENT_DATE - %s::interval", (f"{days} days",)

    def prev(alias, col="created_at"):
        """The window immediately before the selected one (for deltas)."""
        if not days:
            return "", ()
        return (f"{alias}.{col} >= CURRENT_DATE - %s::interval"
                f" AND {alias}.{col} < CURRENT_DATE - %s::interval",
                (f"{days * 2} days", f"{days} days"))

    def _scoped(expr, wfn, scope_sql, scope_params, table, alias):
        wf, wparams = wfn(alias)
        exprs, params = [], []
        if scope_sql:
            exprs.append(scope_sql)
            params += list(scope_params)
        if wf:
            exprs.append(wf)
            params += list(wparams)
        sql = f"SELECT {expr} FROM {table} {alias}"
        if exprs:
            sql += " WHERE " + " AND ".join(exprs)
        return _kpi(conn, sql, tuple(params))

    def pob_kpi(expr, wfn):
        return _scoped(expr, wfn, pw, pp, "pob_activities", "pa")

    def grat_kpi(expr, wfn):
        return _scoped(expr, wfn, grat_scope, gp, "gratifications", "g")

    # Approved invoice value = the verified POB's invoice amount (the real
    # value per project once the invoice proof is approved), falling back to
    # the submitted POB amount when no invoice amount was captured.
    INV_VALUE = "COALESCE(NULLIF(pa.invoice_amount,0), pa.pob_amount)"
    POB_EXPR = {
        "pob_total": "count(*)",
        "pob_amount": "coalesce(sum(pa.pob_amount),0)",
        "pob_invoice_value": f"coalesce(sum(CASE WHEN pa.status='verified' THEN {INV_VALUE} ELSE 0 END),0)",
        "pob_verified": "count(*) FILTER (WHERE pa.status='verified')",
        "pob_rejected": "count(*) FILTER (WHERE pa.status='rejected')",
        "pob_pending": "count(*) FILTER (WHERE pa.status='pending_verification')",
        "pob_flagged": "count(*) FILTER (WHERE pa.status IN ('duplicate','needs_review'))",
    }
    cur = {k: pob_kpi(e, win) for k, e in POB_EXPR.items()}
    previous = {f"prev_{k}": pob_kpi(e, prev) for k, e in POB_EXPR.items()}
    cur["gratifications"] = grat_kpi("count(*)", win)
    cur["gratifications_completed"] = grat_kpi(
        "count(*) FILTER (WHERE g.status='completed')", win)
    previous["prev_gratifications"] = grat_kpi("count(*)", prev)

    totals = {
        "users": _kpi(conn, "SELECT count(*) FROM users"),
        "active_users": _kpi(conn, "SELECT count(*) FROM users WHERE status='active'"),
        "campaigns": _kpi(conn, "SELECT count(*) FROM campaigns"),
        "active_campaigns": _kpi(conn, "SELECT count(*) FROM campaigns WHERE active=TRUE AND status='active'"),
        "products": _kpi(conn, "SELECT count(*) FROM products"),
        "chemists": _kpi(conn, "SELECT count(*) FROM chemists"),
        "visits": _kpi(conn, f"SELECT count(*) FROM chemist_visits cv{' WHERE cv.user_id = ANY(%s)' if gw else ''}", gp),
        "followups_due": _kpi(conn, f"""SELECT count(*) FROM chemists ch
             JOIN pob_activities pa ON pa.chemist_id=ch.id AND pa.status='verified'
             WHERE pa.created_at >= current_date - INTERVAL '120 days'{' AND pa.user_id = ANY(%s)' if gw else ''}
             AND COALESCE((SELECT max(v2.visit_date) FROM chemist_visits v2 WHERE v2.chemist_id=ch.id), current_date)
                 <= current_date - INTERVAL '15 days'""", gp),
    }
    verified = cur["pob_verified"]
    rejected = cur["pob_rejected"]
    cur["approval_rate"] = round(verified / (verified + rejected) * 100, 2) if (verified + rejected) else 0.0
    prev_verified = previous["prev_pob_verified"]
    prev_rejected = previous["prev_pob_rejected"]
    previous["prev_approval_rate"] = round(prev_verified / (prev_verified + prev_rejected) * 100, 2) \
        if (prev_verified + prev_rejected) else 0.0

    where, wparams = _where(days, pw, pp, "pa")
    c = conn.cursor()
    c.execute(f"SELECT pa.status, count(*) FROM pob_activities pa{where} GROUP BY pa.status", wparams)
    totals["pob_by_status"] = {r[0]: r[1] for r in c.fetchall()}
    gwhere, gwparams = _where(days, grat_scope, gp, "g")
    c.execute(f"SELECT g.type_code, count(*) FROM gratifications g{gwhere} GROUP BY g.type_code", gwparams)
    totals["gratification_by_type"] = {r[0]: r[1] for r in c.fetchall()}
    c.execute(f"""SELECT to_char(pa.created_at,'YYYY-MM') AS month, count(*), coalesce(sum(pa.pob_amount),0)
                 FROM pob_activities pa{where} GROUP BY 1 ORDER BY 1 DESC LIMIT 12""", wparams)
    totals["monthly_pob"] = [{"month": r[0], "count": r[1], "amount": r[2]} for r in c.fetchall()]
    totals.update(cur)
    totals.update(previous)
    totals["pob_duplicates"] = cur["pob_flagged"]
    totals["pob_needs_review"] = pob_kpi("count(*) FILTER (WHERE pa.status='needs_review')", win)
    totals["days"] = days
    return totals


@router.get("/campaign")
def campaign_dashboard(days: int = 0, ctx: TenantContext = Depends(require_permission("dashboard.view"))):
    conn = ctx.conn
    sc, sp = _pob_where(ctx, conn, "pa")
    where, params = _where(days, sc, sp, "pa")
    c = conn.cursor()
    c.execute(f"""SELECT c.id, c.name, count(pa.id) AS pobs,
                 coalesce(sum(pa.pob_amount),0) AS amount,
                 coalesce(sum(CASE WHEN pa.status='verified' THEN COALESCE(NULLIF(pa.invoice_amount,0), pa.pob_amount) ELSE 0 END),0) AS invoice_value,
                 sum(CASE WHEN pa.status='verified' THEN 1 ELSE 0 END) AS verified,
                 sum(CASE WHEN pa.status='pending_verification' THEN 1 ELSE 0 END) AS pending,
                 sum(CASE WHEN pa.status IN ('duplicate','needs_review') THEN 1 ELSE 0 END) AS flagged
                 FROM campaigns c LEFT JOIN pob_activities pa ON pa.campaign_id=c.id
                 {where} GROUP BY c.id ORDER BY amount DESC""", params)
    return {"items": fetchall_dict(c)}


@router.get("/brand")
def brand_dashboard(days: int = 0, ctx: TenantContext = Depends(require_permission("dashboard.view"))):
    conn = ctx.conn
    sc, sp = _pob_where(ctx, conn, "pa")
    where, params = _where(days, sc, sp, "pa")
    c = conn.cursor()
    c.execute(f"""SELECT b.id, b.name, count(pa.id) AS pobs,
                 coalesce(sum(pa.pob_amount),0) AS amount
                 FROM brands b
                 LEFT JOIN campaigns c ON c.brand_id=b.id
                 LEFT JOIN pob_activities pa ON pa.campaign_id=c.id
                 {where} GROUP BY b.id ORDER BY amount DESC""", params)
    return {"items": fetchall_dict(c)}


@router.get("/division")
def division_dashboard(days: int = 0, ctx: TenantContext = Depends(require_permission("dashboard.view"))):
    """Division-wise POB KPIs. Campaigns from the same division name are clubbed."""
    conn = ctx.conn
    sc, sp = _pob_where(ctx, conn, "pa")
    where, params = _where(days, sc, sp, "pa")
    c = conn.cursor()
    c.execute(f"""SELECT COALESCE(NULLIF(d.name,''), NULLIF(c.division,''), 'Unassigned') AS division,
                 count(DISTINCT c.id) AS campaigns, count(pa.id) AS pobs,
                 coalesce(sum(pa.pob_amount),0) AS amount,
                 coalesce(sum(CASE WHEN pa.status='verified' THEN COALESCE(NULLIF(pa.invoice_amount,0), pa.pob_amount) ELSE 0 END),0) AS invoice_value,
                 sum(CASE WHEN pa.status='verified' THEN 1 ELSE 0 END) AS verified,
                 sum(CASE WHEN pa.status='pending_verification' THEN 1 ELSE 0 END) AS pending,
                 sum(CASE WHEN pa.status='rejected' THEN 1 ELSE 0 END) AS rejected
                 FROM campaigns c
                 LEFT JOIN divisions d ON d.id=c.division_id
                 LEFT JOIN pob_activities pa ON pa.campaign_id=c.id
                 {where}
                 GROUP BY COALESCE(NULLIF(d.name,''), NULLIF(c.division,''), 'Unassigned')
                 ORDER BY amount DESC""", params)
    items = fetchall_dict(c)
    for r in items:
        r["approval_rate"] = round(r["verified"] / (r["verified"] + r["rejected"]) * 100, 2) \
            if (r["verified"] + r["rejected"]) else 0.0
    return {"items": items}


@router.get("/mr")
def mr_dashboard(days: int = 0, ctx: TenantContext = Depends(require_permission("dashboard.view"))):
    conn = ctx.conn
    visible = _scope(ctx, conn)
    sc, sp = "", ()
    if visible is not None:
        sc = "u.id = ANY(%s)"
        sp = (visible,)
    where, params = _where(days, sc, sp, "pa")
    c = conn.cursor()
    c.execute(f"""SELECT u.id, u.full_name, h.name AS level_name, u.region,
                 count(pa.id) AS pobs, coalesce(sum(pa.pob_amount),0) AS amount,
                 coalesce(sum(CASE WHEN pa.status='verified' THEN COALESCE(NULLIF(pa.invoice_amount,0), pa.pob_amount) ELSE 0 END),0) AS invoice_value,
                 sum(CASE WHEN pa.status='verified' THEN 1 ELSE 0 END) AS verified,
                 sum(CASE WHEN pa.status='pending_verification' THEN 1 ELSE 0 END) AS pending,
                 sum(CASE WHEN pa.status='rejected' THEN 1 ELSE 0 END) AS rejected
                 FROM users u
                 LEFT JOIN hierarchy_levels h ON h.id=u.hierarchy_level_id
                 LEFT JOIN pob_activities pa ON pa.user_id=u.id
                 {where} GROUP BY u.id, h.name ORDER BY amount DESC""", params)
    return {"items": fetchall_dict(c)}


@router.get("/manager")
def manager_dashboard(uid: int, ctx: TenantContext = Depends(require_permission("dashboard.view"))):
    """KPI rollup for a manager and everyone reporting to them (any depth).
    The manager must be within the caller's own visibility scope."""
    conn = ctx.conn
    visible = _scope(ctx, conn)
    if visible is not None and uid not in visible:
        raise HTTPException(403, "manager is outside your visibility scope")
    c = conn.cursor()
    c.execute("SELECT id, full_name FROM users WHERE id=%s", (uid,))
    mgr = c.fetchone()
    if not mgr:
        raise HTTPException(404, "manager not found")
    team = [uid] + _descendants(conn, uid)
    c.execute("""SELECT count(*), coalesce(sum(pa.pob_amount),0),
                 sum(CASE WHEN pa.status='verified' THEN 1 ELSE 0 END),
                 sum(CASE WHEN pa.status='rejected' THEN 1 ELSE 0 END),
                 sum(CASE WHEN pa.status='pending_verification' THEN 1 ELSE 0 END)
                 FROM pob_activities pa WHERE pa.user_id = ANY(%s)""", (team,))
    r = c.fetchone()
    c.execute("SELECT count(*) FROM users WHERE id = ANY(%s)", (team,))
    team_size = c.fetchone()[0]
    return {"manager_id": uid, "manager_name": mgr[1], "team_size": team_size,
            "pobs": r[0], "amount": r[1], "verified": r[2], "rejected": r[3],
            "pending": r[4]}


@router.get("/leaderboard")
def leaderboard(days: int = 0, ctx: TenantContext = Depends(require_permission("dashboard.view"))):
    """Top field force by verified POB value within the caller's scope."""
    conn = ctx.conn
    visible = _scope(ctx, conn)
    sc = "pa.status='verified'"
    sp = ()
    if visible is not None:
        sc += " AND pa.user_id = ANY(%s)"
        sp = (visible,)
    where, params = _where(days, sc, sp, "pa")
    c = conn.cursor()
    c.execute(f"""SELECT u.id, u.full_name, h.name AS level_name, u.region,
                 count(pa.id) AS verified_pobs, coalesce(sum(pa.pob_amount),0) AS amount,
                 coalesce(sum(COALESCE(NULLIF(pa.invoice_amount,0), pa.pob_amount)),0) AS invoice_value
                 FROM pob_activities pa
                 JOIN users u ON u.id=pa.user_id
                 LEFT JOIN hierarchy_levels h ON h.id=u.hierarchy_level_id
                 {where}
                 GROUP BY u.id, u.full_name, h.name, u.region
                 ORDER BY amount DESC LIMIT 20""", params)
    return {"items": fetchall_dict(c)}


@router.get("/performance")
def performance(days: int = 0, ctx: TenantContext = Depends(require_permission("dashboard.view"))):
    """State / region / ASM / PSR breakdown within the caller's scope."""
    conn = ctx.conn
    visible = _scope(ctx, conn)
    sc, sp = "", ()
    if visible is not None:
        sc = "u.id = ANY(%s)"
        sp = (visible,)
    where, params = _where(days, sc, sp, "pa")
    c = conn.cursor()
    c.execute(f"""SELECT u.region, ch.state, count(pa.id) AS pobs,
                 coalesce(sum(pa.pob_amount),0) AS amount,
                 coalesce(sum(CASE WHEN pa.status='verified' THEN COALESCE(NULLIF(pa.invoice_amount,0), pa.pob_amount) ELSE 0 END),0) AS invoice_value,
                 sum(CASE WHEN pa.status='verified' THEN 1 ELSE 0 END) AS verified
                 FROM pob_activities pa
                 JOIN users u ON u.id=pa.user_id
                 JOIN chemists ch ON ch.id=pa.chemist_id
                 {where}
                 GROUP BY u.region, ch.state ORDER BY amount DESC""", params)
    rows = fetchall_dict(c)
    state_map, region_map = {}, {}
    for r in rows:
        state = r["state"] or "—"
        region = r["region"] or "—"
        s = state_map.setdefault(state, {"state": state, "pobs": 0, "amount": 0, "invoice_value": 0, "verified": 0})
        s["pobs"] += r["pobs"]; s["amount"] += r["amount"]
        s["invoice_value"] += r["invoice_value"]; s["verified"] += r["verified"]
        rg = region_map.setdefault(region, {"region": region, "pobs": 0, "amount": 0, "invoice_value": 0, "verified": 0})
        rg["pobs"] += r["pobs"]; rg["amount"] += r["amount"]
        rg["invoice_value"] += r["invoice_value"]; rg["verified"] += r["verified"]
    return {
        "by_state": sorted(state_map.values(), key=lambda x: -x["amount"]),
        "by_region": sorted(region_map.values(), key=lambda x: -x["amount"]),
    }


@router.get("/verification")
def verification_dashboard(days: int = 0, ctx: TenantContext = Depends(require_permission("dashboard.view"))):
    conn = ctx.conn
    visible = _scope(ctx, conn)
    sc, sp = "", ()
    if visible is not None:
        sc = "pa.user_id = ANY(%s)"
        sp = (visible,)
    where, params = _where(days, sc, sp, "pa")
    c = conn.cursor()
    c.execute(f"""SELECT v.status, count(*) FROM pob_verifications v
                 JOIN pob_activities pa ON pa.id=v.pob_id{where} GROUP BY v.status""", params)
    stats = {r[0]: r[1] for r in c.fetchall()}
    where_v = (where + " AND v.verified_at IS NOT NULL") if where else " WHERE v.verified_at IS NOT NULL"
    c.execute(f"""SELECT coalesce(avg(extract(epoch from (v.verified_at - v.created_at))/3600.0),0)
                 FROM pob_verifications v JOIN pob_activities pa ON pa.id=v.pob_id{where_v}""", params)
    avg_tat = c.fetchone()[0]
    c.execute(f"""SELECT to_char(v.verified_at,'YYYY-MM-DD') AS day, count(*)
                 FROM pob_verifications v JOIN pob_activities pa ON pa.id=v.pob_id{where_v}
                 GROUP BY 1 ORDER BY 1 DESC LIMIT 14""", params)
    return {"stats": stats, "avg_tat_hours": round(avg_tat, 2),
            "daily": [{"day": r[0], "count": r[1]} for r in c.fetchall()]}


@router.get("/finance")
def finance_dashboard(days: int = 0, ctx: TenantContext = Depends(require_permission("dashboard.view"))):
    conn = ctx.conn
    visible = _scope(ctx, conn)
    sc, sp = "", ()
    if visible is not None:
        sc = "g.user_id = ANY(%s)"
        sp = (visible,)
    where, params = _and(days, sc, sp, "g")
    c = conn.cursor()
    c.execute(f"""SELECT coalesce(sum(CASE WHEN g.status='completed' THEN g.scheme_value ELSE 0 END),0)
                 FROM gratifications g WHERE g.type_code IN ('cashback','upi'){where}""", params)
    paid = c.fetchone()[0]
    c.execute(f"""SELECT coalesce(sum(CASE WHEN g.status IN ('eligible','approved') THEN g.scheme_value ELSE 0 END),0)
                 FROM gratifications g WHERE g.type_code IN ('cashback','upi'){where}""", params)
    pending = c.fetchone()[0]
    c.execute(f"SELECT count(*) FROM gratifications g WHERE g.type_code IN ('cashback','upi'){where}", params)
    total_tx = c.fetchone()[0]
    c.execute(f"""SELECT status, count(*), coalesce(sum(scheme_value),0) FROM gratifications g
                 WHERE g.type_code IN ('cashback','upi'){where} GROUP BY status""", params)
    return {"paid": paid, "pending": pending, "total_transactions": total_tx,
            "by_status": [{"status": r[0], "count": r[1], "value": r[2]} for r in c.fetchall()]}


@router.get("/gift")
def gift_dashboard(days: int = 0, ctx: TenantContext = Depends(require_permission("dashboard.view"))):
    conn = ctx.conn
    visible = _scope(ctx, conn)
    sc, sp = "", ()
    if visible is not None:
        sc = "g.user_id = ANY(%s)"
        sp = (visible,)
    where, params = _and(days, sc, sp, "g")
    c = conn.cursor()
    c.execute(f"""SELECT gift.name, count(*) AS dispatched,
                 coalesce(sum(gift.cost),0) AS cost_value
                 FROM gratifications g JOIN gifts gift ON gift.id=g.gift_id
                 WHERE g.dispatch_status='dispatched'{where}
                 GROUP BY gift.name ORDER BY dispatched DESC""", params)
    return {"by_gift": fetchall_dict(c)}


def _descendants(conn, root_user_id: int) -> list[int]:
    """All user ids whose reporting chain leads to root_user_id."""
    c = conn.cursor()
    c.execute("SELECT id, parent_id FROM users")
    rows = c.fetchall()
    children = {}
    for uid, parent in rows:
        children.setdefault(parent, []).append(uid)
    out = []
    stack = [root_user_id]
    while stack:
        node = stack.pop()
        for child in children.get(node, []):
            out.append(child)
            stack.append(child)
    return out
