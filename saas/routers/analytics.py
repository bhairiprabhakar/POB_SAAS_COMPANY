"""
Analytics router -- hierarchy-scoped performance analytics.

Every caller sees only the users they are entitled to (see saas/scoping.py):

  PSR / MR           -> own data only (scope = "self")
  ASM / RSM / SM     -> own team (scope = "team")
  ZSM / NSM / HO     -> zonal / national rollup (scope = "team" / "all")
  Division Admin     -> every user in their division (scope = "team")
  CampaignOS Admin / verifier / auditor / finance -> everything ("all")

The summary endpoint returns the caller's identity + scope, their own KPIs,
their team rollup (when a manager), a per-member breakdown and monthly trend.

The roi endpoint frames everything as input -> output -> result:

  Input   = rewards cost for the period. "Paid" (fulfilled cash-out) is the
            primary basis; "eligible" (accrued liability) is used only when
            nothing has been paid out yet.
  Output  = verified POB value (invoice-validated POB amount).
  Result  = ROI multiple (output / input) + net value (output - input).

It returns per-campaign, per-member, per-chemist, per-brand, per-product and
per-division breakdowns so the UI can build its own "top 10" lists.
"""
from fastapi import APIRouter, Depends

from ..db_utils import fetchall_dict, fetchone_dict
from ..deps import TenantContext, require_permission
from ..scoping import visible_user_ids

router = APIRouter(prefix="/api/v1/analytics", tags=["analytics"])


def _kpi(conn, sql, params=()):
    c = conn.cursor()
    c.execute(sql, params)
    row = c.fetchone()
    return row[0] if row else 0


def _win(days, alias, col="created_at"):
    days = int(days or 0)
    if not days:
        return "", ()
    return f" AND {alias}.{col} >= CURRENT_DATE - %s::interval", (f"{days} days",)


def _prev(days, alias, col="created_at"):
    days = int(days or 0)
    if not days:
        return "", ()
    return (f" AND {alias}.{col} >= CURRENT_DATE - %s::interval"
            f" AND {alias}.{col} < CURRENT_DATE - %s::interval",
            (f"{days * 2} days", f"{days} days"))


def _where(days, scope_sql, scope_params, alias, col="created_at"):
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


def _prev_where(days, scope_sql, scope_params, alias, col="created_at"):
    """Same as _where but for the previous window (used for period-over-period)."""
    pf, pp = _prev(days, alias, col)
    exprs, params = [], []
    if scope_sql:
        exprs.append(scope_sql)
        params += list(scope_params)
    if pf:
        exprs.append(pf.lstrip(" AND "))
        params += list(pp)
    if not exprs:
        return "", ()
    return " WHERE " + " AND ".join(exprs), tuple(params)


# Gratification statuses that count as "money out / fulfilled" for ROI.
PAID_REWARD_STATUSES = ("paid", "completed", "redeemed", "dispatched", "delivered", "acknowledged")
PAID_SQL = "(" + ",".join("'%s'" % s for s in PAID_REWARD_STATUSES) + ")"


def _roi_metrics(verified_value, rewards_paid, rewards_eligible):
    """ROI is the money multiplier of verified POB value over rewards cost.

    - Input  = rewards cost. "Paid" (cash out / fulfilled) is the primary
      basis; "eligible" (accrued liability) is used only when nothing has
      been paid out yet.
    - Output = verified POB value (invoice-validated).
    - Result = ROI multiple + net value (output - input).
    """
    roi, basis = None, None
    if rewards_paid:
        roi = round(verified_value / rewards_paid, 2)
        basis = "paid"
    elif rewards_eligible:
        roi = round(verified_value / rewards_eligible, 2)
        basis = "eligible"
    return roi, basis, round(verified_value - rewards_paid, 2)


def _merge_rewards(rows, key, reward_map):
    """Stamp rewards_cost / roi / net onto agg rows keyed by ``key``."""
    for r in rows:
        rw = reward_map.get(r[key], {})
        r["rewards_count"] = rw.get("rewards_count", 0)
        r["rewards_eligible"] = rw.get("rewards_eligible", 0)
        r["rewards_paid"] = rw.get("rewards_paid", 0)
        r["roi"], r["roi_basis"], r["net"] = _roi_metrics(
            r.get("verified_value", 0), r["rewards_paid"], r["rewards_eligible"])
        r["rewards_per_pob"] = round(r["rewards_paid"] / r["verified"], 2) \
            if r.get("verified") else 0.0
        r["approval_rate"] = round(r["verified"] / (r["verified"] + r["rejected"]) * 100, 2) \
            if (r.get("verified") + r.get("rejected", 0)) else 0.0
    return rows


def _team_rollup(conn, ids, days):
    """Rollup KPIs + per-member breakdown for the given user ids.

    ``ids=None`` means every active user (company-wide). Used for the admin/HO
    "All teams" rollup as well as a manager's own team / own division rollup.
    """
    c = conn.cursor()
    wf, wp = _win(days, "pa")
    pv, pvp = _prev(days, "pa")
    if ids is None:
        team_where = "TRUE"
        team_params = ()
    else:
        team_where = "pa.user_id = ANY(%s)"
        team_params = (ids,)

    team = {
        "members": len(ids) if ids is not None
                   else _kpi(conn, "SELECT count(*) FROM users WHERE status='active'"),
        "pobs": _kpi(conn, f"SELECT count(*) FROM pob_activities pa WHERE {team_where}{wf}", team_params + wp),
        "amount": _kpi(conn, f"SELECT coalesce(sum(pa.pob_amount),0) FROM pob_activities pa WHERE {team_where}{wf}", team_params + wp),
        "invoice_value": _kpi(conn, f"SELECT coalesce(sum(CASE WHEN pa.status='verified' THEN COALESCE(NULLIF(pa.invoice_amount,0), pa.pob_amount) ELSE 0 END),0) FROM pob_activities pa WHERE {team_where}{wf}", team_params + wp),
        "verified": _kpi(conn, f"SELECT count(*) FROM pob_activities pa WHERE {team_where}{wf} AND pa.status='verified'", team_params + wp),
        "pending": _kpi(conn, f"SELECT count(*) FROM pob_activities pa WHERE {team_where}{wf} AND pa.status='pending_verification'", team_params + wp),
        "rejected": _kpi(conn, f"SELECT count(*) FROM pob_activities pa WHERE {team_where}{wf} AND pa.status='rejected'", team_params + wp),
    }
    team["approval_rate"] = round(team["verified"] / (team["verified"] + team["rejected"]) * 100, 2) \
        if (team["verified"] + team["rejected"]) else 0.0
    team_prev = {
        "pobs": _kpi(conn, f"SELECT count(*) FROM pob_activities pa WHERE {team_where}{pv}", team_params + pvp),
        "amount": _kpi(conn, f"SELECT coalesce(sum(pa.pob_amount),0) FROM pob_activities pa WHERE {team_where}{pv}", team_params + pvp),
        "invoice_value": _kpi(conn, f"SELECT coalesce(sum(CASE WHEN pa.status='verified' THEN COALESCE(NULLIF(pa.invoice_amount,0), pa.pob_amount) ELSE 0 END),0) FROM pob_activities pa WHERE {team_where}{pv}", team_params + pvp),
        "verified": _kpi(conn, f"SELECT count(*) FROM pob_activities pa WHERE {team_where}{pv} AND pa.status='verified'", team_params + pvp),
        "rejected": _kpi(conn, f"SELECT count(*) FROM pob_activities pa WHERE {team_where}{pv} AND pa.status='rejected'", team_params + pvp),
    }

    where, params = _where(days, "u.id = ANY(%s)" if ids is not None else "u.status='active'",
                           (ids,) if ids is not None else (), "pa")
    c.execute(f"""SELECT u.id, u.full_name, h.name AS level_name, u.region, u.state, u.division,
                 count(pa.id) AS pobs, coalesce(sum(pa.pob_amount),0) AS amount,
                 coalesce(sum(CASE WHEN pa.status='verified' THEN COALESCE(NULLIF(pa.invoice_amount,0), pa.pob_amount) ELSE 0 END),0) AS invoice_value,
                 sum(CASE WHEN pa.status='verified' THEN 1 ELSE 0 END) AS verified,
                 sum(CASE WHEN pa.status='pending_verification' THEN 1 ELSE 0 END) AS pending,
                 sum(CASE WHEN pa.status='rejected' THEN 1 ELSE 0 END) AS rejected
                 FROM users u
                 LEFT JOIN hierarchy_levels h ON h.id=u.hierarchy_level_id
                 LEFT JOIN pob_activities pa ON pa.user_id=u.id
                 {where} GROUP BY u.id, u.full_name, h.name, u.region, u.state, u.division
                 ORDER BY amount DESC""", params)
    members = []
    for r in fetchall_dict(c):
        r["approval_rate"] = round(r["verified"] / (r["verified"] + r["rejected"]) * 100, 2) \
            if (r["verified"] + r["rejected"]) else 0.0
        members.append(r)
    return team, team_prev, members


@router.get("/summary")
def analytics_summary(days: int = 0, ctx: TenantContext = Depends(require_permission("dashboard.view"))):
    conn = ctx.conn
    c = conn.cursor()
    me = ctx.user
    uid = me.get("id")
    visible = visible_user_ids(conn, ctx)

    c.execute(
        """SELECT u.id, u.full_name, u.username, u.region, u.state, u.division,
                  u.division_id, r.name AS role_name, h.name AS level_name, h.rank
           FROM users u
           LEFT JOIN roles r ON r.id=u.role_id
           LEFT JOIN hierarchy_levels h ON h.id=u.hierarchy_level_id
           WHERE u.id=%s""", (uid,))
    me_row = fetchone_dict(c)
    if not me_row:
        me_row = {"id": uid, "full_name": me.get("full_name") or me.get("username"),
                  "role_name": me.get("role_name"), "level_name": None, "rank": None,
                  "division_id": None}

    # Determine scope for the caller.
    if visible is None:
        scope = "all"
    elif visible == [uid] or visible == sorted([uid]):
        scope = "self"
    else:
        scope = "team"

    wf, wp = _win(days, "pa")
    pv, pvp = _prev(days, "pa")

    # ---- Own performance ---------------------------------------------------
    own_where = "pa.user_id=%s"
    own_params = (uid,)
    own = {
        "pobs": _kpi(conn, f"SELECT count(*) FROM pob_activities pa WHERE {own_where}{wf}", own_params + wp),
        "amount": _kpi(conn, f"SELECT coalesce(sum(pa.pob_amount),0) FROM pob_activities pa WHERE {own_where}{wf}", own_params + wp),
        "invoice_value": _kpi(conn, f"SELECT coalesce(sum(CASE WHEN pa.status='verified' THEN COALESCE(NULLIF(pa.invoice_amount,0), pa.pob_amount) ELSE 0 END),0) FROM pob_activities pa WHERE {own_where}{wf}", own_params + wp),
        "verified": _kpi(conn, f"SELECT count(*) FROM pob_activities pa WHERE {own_where}{wf} AND pa.status='verified'", own_params + wp),
        "pending": _kpi(conn, f"SELECT count(*) FROM pob_activities pa WHERE {own_where}{wf} AND pa.status='pending_verification'", own_params + wp),
        "rejected": _kpi(conn, f"SELECT count(*) FROM pob_activities pa WHERE {own_where}{wf} AND pa.status='rejected'", own_params + wp),
        "visits": _kpi(conn, "SELECT count(*) FROM chemist_visits WHERE user_id=%s", (uid,)),
        "gratifications": _kpi(conn, "SELECT count(*) FROM gratifications WHERE user_id=%s", (uid,)),
    }
    own["approval_rate"] = round(own["verified"] / (own["verified"] + own["rejected"]) * 100, 2) \
        if (own["verified"] + own["rejected"]) else 0.0
    own_prev = {
        "pobs": _kpi(conn, f"SELECT count(*) FROM pob_activities pa WHERE {own_where}{pv}", own_params + pvp),
        "amount": _kpi(conn, f"SELECT coalesce(sum(pa.pob_amount),0) FROM pob_activities pa WHERE {own_where}{pv}", own_params + pvp),
        "invoice_value": _kpi(conn, f"SELECT coalesce(sum(CASE WHEN pa.status='verified' THEN COALESCE(NULLIF(pa.invoice_amount,0), pa.pob_amount) ELSE 0 END),0) FROM pob_activities pa WHERE {own_where}{pv}", own_params + pvp),
        "verified": _kpi(conn, f"SELECT count(*) FROM pob_activities pa WHERE {own_where}{pv} AND pa.status='verified'", own_params + pvp),
        "rejected": _kpi(conn, f"SELECT count(*) FROM pob_activities pa WHERE {own_where}{pv} AND pa.status='rejected'", own_params + pvp),
    }

    c.execute(f"""SELECT to_char(pa.created_at,'YYYY-MM') AS month,
                 count(*), coalesce(sum(pa.pob_amount),0)
                 FROM pob_activities pa WHERE {own_where}{wf}
                 GROUP BY 1 ORDER BY 1 DESC LIMIT 12""", own_params + wp)
    own["monthly"] = [{"month": r[0], "count": r[1], "amount": r[2]} for r in c.fetchall()]
    c.execute(f"""SELECT pa.status, count(*) FROM pob_activities pa
                 WHERE {own_where}{wf} GROUP BY pa.status""", own_params + wp)
    own["by_status"] = {r[0]: r[1] for r in c.fetchall()}

    # ---- Team rollup (if the caller manages anyone) ------------------------
    # For admin/HO (scope "all") we return two rollups: the whole company
    # ("all teams") and the caller's own team, which is everyone in their own
    # division. Managers get a single rollup for their visible team.
    team = None
    team_prev = None
    members = []
    all_team = None
    all_team_prev = None
    all_members = []
    if visible is not None:
        team_ids = [i for i in visible if i != uid]
        if team_ids:
            team, team_prev, members = _team_rollup(conn, team_ids, days)
    elif scope == "all":
        all_team, all_team_prev, all_members = _team_rollup(conn, None, days)
        div_id = me_row.get("division_id")
        if div_id:
            c.execute("SELECT id FROM users WHERE division_id=%s", (div_id,))
            div_ids = [r[0] for r in c.fetchall() if r[0] != uid]
            if div_ids:
                team, team_prev, members = _team_rollup(conn, div_ids, days)

    # ---- Campaign-wise & division-wise breakdown (caller's scope) ----------
    scope_join = " AND pa.user_id = ANY(%s)" if visible is not None else ""
    scope_params = (visible,) if visible is not None else ()
    wf2, wp2 = _win(days, "pa")
    scope_join += wf2
    scope_params = scope_params + wp2

    c.execute(f"""SELECT c.id, c.name, d.id AS division_id,
                  COALESCE(NULLIF(d.name,''), NULLIF(c.division,''), '') AS division_name,
                  count(pa.id) AS pobs, coalesce(sum(pa.pob_amount),0) AS amount,
                  coalesce(sum(CASE WHEN pa.status='verified' THEN COALESCE(NULLIF(pa.invoice_amount,0), pa.pob_amount) ELSE 0 END),0) AS invoice_value,
                  sum(CASE WHEN pa.status='verified' THEN 1 ELSE 0 END) AS verified,
                  sum(CASE WHEN pa.status='pending_verification' THEN 1 ELSE 0 END) AS pending,
                  sum(CASE WHEN pa.status='rejected' THEN 1 ELSE 0 END) AS rejected
                  FROM campaigns c
                  LEFT JOIN divisions d ON d.id=c.division_id
                  LEFT JOIN pob_activities pa ON pa.campaign_id=c.id{scope_join}
                  GROUP BY c.id, d.id, c.division
                  ORDER BY amount DESC""", scope_params)
    campaigns = fetchall_dict(c)
    for r in campaigns:
        r["division"] = r.pop("division_name") or "—"
        r["approval_rate"] = round(r["verified"] / (r["verified"] + r["rejected"]) * 100, 2) \
            if (r["verified"] + r["rejected"]) else 0.0

    c.execute(f"""SELECT COALESCE(NULLIF(d.name,''), NULLIF(c.division,''), 'Unassigned') AS division,
                  count(DISTINCT c.id) AS campaigns, count(pa.id) AS pobs,
                  coalesce(sum(pa.pob_amount),0) AS amount,
                  coalesce(sum(CASE WHEN pa.status='verified' THEN COALESCE(NULLIF(pa.invoice_amount,0), pa.pob_amount) ELSE 0 END),0) AS invoice_value,
                  sum(CASE WHEN pa.status='verified' THEN 1 ELSE 0 END) AS verified,
                  sum(CASE WHEN pa.status='pending_verification' THEN 1 ELSE 0 END) AS pending,
                  sum(CASE WHEN pa.status='rejected' THEN 1 ELSE 0 END) AS rejected
                  FROM campaigns c
                  LEFT JOIN divisions d ON d.id=c.division_id
                  LEFT JOIN pob_activities pa ON pa.campaign_id=c.id{scope_join}
                  GROUP BY COALESCE(NULLIF(d.name,''), NULLIF(c.division,''), 'Unassigned')
                  ORDER BY amount DESC""", scope_params)
    divisions = fetchall_dict(c)
    for r in divisions:
        r["approval_rate"] = round(r["verified"] / (r["verified"] + r["rejected"]) * 100, 2) \
            if (r["verified"] + r["rejected"]) else 0.0

    return {
        "me": me_row,
        "scope": scope,
        "days": int(days or 0),
        "own": own,
        "own_prev": own_prev,
        "team": team,
        "team_prev": team_prev if team else None,
        "members": members,
        "all_team": all_team,
        "all_team_prev": all_team_prev,
        "all_members": all_members,
        "campaigns": campaigns,
        "divisions": divisions,
    }


@router.get("/roi")
def analytics_roi(days: int = 0, ctx: TenantContext = Depends(require_permission("dashboard.view"))):
    """Input -> output -> result (ROI) analytics across campaigns, members,
    chemists, brands, products and divisions. Hierarchy-scoped like /summary."""
    conn = ctx.conn
    c = conn.cursor()
    uid = ctx.user.get("id")
    visible = visible_user_ids(conn, ctx)
    days = int(days or 0)

    if visible is None:
        scope = "all"
    elif visible == [uid] or visible == sorted([uid]):
        scope = "self"
    else:
        scope = "team"

    scope_sql = "pa.user_id = ANY(%s)" if visible is not None else None
    scope_params = (visible,) if visible is not None else ()
    where, params = _where(days, scope_sql, scope_params, "pa")

    g_scope = "g.user_id = ANY(%s)" if visible is not None else None
    g_scope_params = (visible,) if visible is not None else ()
    g_where, g_params = _where(days, g_scope, g_scope_params, "g")

    # ---- Totals (current window) -----------------------------------------
    c.execute(f"""SELECT count(pa.id),
                  coalesce(sum(pa.pob_amount),0),
                  sum(CASE WHEN pa.status='verified' THEN 1 ELSE 0 END),
                  coalesce(sum(CASE WHEN pa.status='verified' THEN COALESCE(NULLIF(pa.invoice_amount,0), pa.pob_amount) ELSE 0 END),0),
                  sum(CASE WHEN pa.status='pending_verification' THEN 1 ELSE 0 END),
                  sum(CASE WHEN pa.status='rejected' THEN 1 ELSE 0 END)
                  FROM pob_activities pa{where}""", params)
    pob_t = c.fetchone() or (0, 0, 0, 0, 0, 0)

    c.execute(f"""SELECT count(g.id), coalesce(sum(g.scheme_value),0),
                  coalesce(sum(CASE WHEN g.status IN {PAID_SQL} THEN g.scheme_value ELSE 0 END),0)
                  FROM gratifications g{g_where}""", g_params)
    rew_t = c.fetchone() or (0, 0, 0)

    verified_value = pob_t[3] or 0
    rewards_paid = rew_t[2] or 0
    rewards_eligible = rew_t[1] or 0
    roi, roi_basis, net = _roi_metrics(verified_value, rewards_paid, rewards_eligible)
    totals = {
        "pobs": pob_t[0] or 0,
        "amount": pob_t[1] or 0,
        "verified": pob_t[2] or 0,
        "verified_value": verified_value,
        "pending": pob_t[4] or 0,
        "rejected": pob_t[5] or 0,
        "rewards_count": rew_t[0] or 0,
        "rewards_eligible": rewards_eligible,
        "rewards_paid": rewards_paid,
        "roi": roi,
        "roi_basis": roi_basis,
        "net": net,
        "rewards_per_pob": round(rewards_paid / (pob_t[2] or 0), 2) if pob_t[2] else 0.0,
        "approval_rate": round((pob_t[2] or 0) / ((pob_t[2] or 0) + (pob_t[5] or 0)) * 100, 2)
                         if ((pob_t[2] or 0) + (pob_t[5] or 0)) else 0.0,
    }

    # ---- Previous window (period-over-period deltas) ----------------------
    p_where, p_params = _prev_where(days, scope_sql, scope_params, "pa")
    pg_where, pg_params = _prev_where(days, g_scope, g_scope_params, "g")
    prev = {"pobs": 0, "amount": 0, "verified_value": 0, "rewards_paid": 0}
    if p_where:
        c.execute(f"""SELECT count(pa.id), coalesce(sum(pa.pob_amount),0),
                      coalesce(sum(CASE WHEN pa.status='verified' THEN COALESCE(NULLIF(pa.invoice_amount,0), pa.pob_amount) ELSE 0 END),0)
                      FROM pob_activities pa{p_where}""", p_params)
        r = c.fetchone()
        prev.update({"pobs": r[0] or 0, "amount": r[1] or 0, "verified_value": r[2] or 0})
    if pg_where:
        c.execute(f"""SELECT coalesce(sum(CASE WHEN g.status IN {PAID_SQL} THEN g.scheme_value ELSE 0 END),0)
                      FROM gratifications g{pg_where}""", pg_params)
        prev["rewards_paid"] = c.fetchone()[0] or 0

    # ---- Campaign-wise ----------------------------------------------------
    c.execute(f"""SELECT c.id, c.name, c.status,
                  COALESCE(NULLIF(d.name,''), NULLIF(c.division,''), '') AS division_name,
                  count(pa.id) AS pobs, coalesce(sum(pa.pob_amount),0) AS amount,
                  sum(CASE WHEN pa.status='verified' THEN 1 ELSE 0 END) AS verified,
                  coalesce(sum(CASE WHEN pa.status='verified' THEN COALESCE(NULLIF(pa.invoice_amount,0), pa.pob_amount) ELSE 0 END),0) AS verified_value,
                  sum(CASE WHEN pa.status='pending_verification' THEN 1 ELSE 0 END) AS pending,
                  sum(CASE WHEN pa.status='rejected' THEN 1 ELSE 0 END) AS rejected
                  FROM campaigns c
                  LEFT JOIN divisions d ON d.id=c.division_id
                  LEFT JOIN pob_activities pa ON pa.campaign_id=c.id{where}
                  GROUP BY c.id, c.name, c.status, d.name, c.division
                  ORDER BY verified_value DESC""", params)
    campaigns = fetchall_dict(c)
    for r in campaigns:
        r["division"] = r.pop("division_name") or "—"

    c.execute(f"""SELECT g.campaign_id, count(*), coalesce(sum(g.scheme_value),0),
                  coalesce(sum(CASE WHEN g.status IN {PAID_SQL} THEN g.scheme_value ELSE 0 END),0)
                  FROM gratifications g{g_where} GROUP BY g.campaign_id""", g_params)
    reward_map = {r[0]: {"rewards_count": r[1], "rewards_eligible": r[2], "rewards_paid": r[3]}
                  for r in c.fetchall()}
    campaigns = _merge_rewards(campaigns, "id", reward_map)
    totals["campaigns"] = len([r for r in campaigns if r["pobs"] or r["rewards_count"]])

    # ---- Member-wise ------------------------------------------------------
    member_scope = "u.status='active'"
    member_params = ()
    if visible is not None:
        member_scope += " AND u.id = ANY(%s)"
        member_params = (visible,)
    m_where, m_params = _where(days, member_scope, member_params, "pa")
    c.execute(f"""SELECT u.id, u.full_name, h.name AS level_name, u.region, u.state, u.division,
                  count(pa.id) AS pobs, coalesce(sum(pa.pob_amount),0) AS amount,
                  sum(CASE WHEN pa.status='verified' THEN 1 ELSE 0 END) AS verified,
                  coalesce(sum(CASE WHEN pa.status='verified' THEN COALESCE(NULLIF(pa.invoice_amount,0), pa.pob_amount) ELSE 0 END),0) AS verified_value,
                  sum(CASE WHEN pa.status='pending_verification' THEN 1 ELSE 0 END) AS pending,
                  sum(CASE WHEN pa.status='rejected' THEN 1 ELSE 0 END) AS rejected
                  FROM users u
                  LEFT JOIN hierarchy_levels h ON h.id=u.hierarchy_level_id
                  LEFT JOIN pob_activities pa ON pa.user_id=u.id
                  {m_where} GROUP BY u.id, u.full_name, h.name, u.region, u.state, u.division
                  ORDER BY verified_value DESC""", m_params)
    members = fetchall_dict(c)

    c.execute(f"""SELECT g.user_id, count(*), coalesce(sum(g.scheme_value),0),
                  coalesce(sum(CASE WHEN g.status IN {PAID_SQL} THEN g.scheme_value ELSE 0 END),0)
                  FROM gratifications g{g_where} GROUP BY g.user_id""", g_params)
    member_rewards = {r[0]: {"rewards_count": r[1], "rewards_eligible": r[2], "rewards_paid": r[3]}
                      for r in c.fetchall()}
    members = _merge_rewards(members, "id", member_rewards)
    totals["members_count"] = len(members)

    # ---- Chemist-wise -----------------------------------------------------
    c.execute(f"""SELECT ch.id, ch.name, ch.shop_name, ch.city, ch.state,
                  count(pa.id) AS pobs, coalesce(sum(pa.pob_amount),0) AS amount,
                  sum(CASE WHEN pa.status='verified' THEN 1 ELSE 0 END) AS verified,
                  coalesce(sum(CASE WHEN pa.status='verified' THEN COALESCE(NULLIF(pa.invoice_amount,0), pa.pob_amount) ELSE 0 END),0) AS verified_value
                  FROM chemists ch
                  JOIN pob_activities pa ON pa.chemist_id=ch.id
                  {where} GROUP BY ch.id, ch.name, ch.shop_name, ch.city, ch.state
                  ORDER BY amount DESC""", params)
    chemists = fetchall_dict(c)

    # ---- Brand-wise -------------------------------------------------------
    c.execute(f"""SELECT b.id, b.name,
                  count(pa.id) AS pobs, coalesce(sum(pa.pob_amount),0) AS amount,
                  sum(CASE WHEN pa.status='verified' THEN 1 ELSE 0 END) AS verified,
                  coalesce(sum(CASE WHEN pa.status='verified' THEN COALESCE(NULLIF(pa.invoice_amount,0), pa.pob_amount) ELSE 0 END),0) AS verified_value
                  FROM brands b
                  JOIN campaigns c ON c.brand_id=b.id
                  JOIN pob_activities pa ON pa.campaign_id=c.id
                  {where} GROUP BY b.id, b.name
                  ORDER BY amount DESC""", params)
    brands = fetchall_dict(c)

    # ---- Product-wise -----------------------------------------------------
    c.execute(f"""SELECT pr.id, pr.name, pr.sku,
                  count(pa.id) AS pobs, coalesce(sum(pa.pob_amount),0) AS amount,
                  sum(CASE WHEN pa.status='verified' THEN 1 ELSE 0 END) AS verified,
                  coalesce(sum(CASE WHEN pa.status='verified' THEN COALESCE(NULLIF(pa.invoice_amount,0), pa.pob_amount) ELSE 0 END),0) AS verified_value
                  FROM products pr
                  JOIN pob_activities pa ON pa.product_id=pr.id
                  {where} GROUP BY pr.id, pr.name, pr.sku
                  ORDER BY amount DESC""", params)
    products = fetchall_dict(c)

    # ---- Division-wise ----------------------------------------------------
    c.execute(f"""SELECT COALESCE(NULLIF(d.name,''), NULLIF(c.division,''), 'Unassigned') AS division,
                  count(DISTINCT c.id) AS campaigns, count(pa.id) AS pobs,
                  coalesce(sum(pa.pob_amount),0) AS amount,
                  sum(CASE WHEN pa.status='verified' THEN 1 ELSE 0 END) AS verified,
                  coalesce(sum(CASE WHEN pa.status='verified' THEN COALESCE(NULLIF(pa.invoice_amount,0), pa.pob_amount) ELSE 0 END),0) AS verified_value,
                  sum(CASE WHEN pa.status='rejected' THEN 1 ELSE 0 END) AS rejected
                  FROM campaigns c
                  LEFT JOIN divisions d ON d.id=c.division_id
                  LEFT JOIN pob_activities pa ON pa.campaign_id=c.id{where}
                  GROUP BY COALESCE(NULLIF(d.name,''), NULLIF(c.division,''), 'Unassigned')
                  ORDER BY amount DESC""", params)
    divisions = fetchall_dict(c)

    c.execute(f"""SELECT COALESCE(NULLIF(d.name,''), NULLIF(c.division,''), 'Unassigned') AS division,
                  count(g.id), coalesce(sum(g.scheme_value),0),
                  coalesce(sum(CASE WHEN g.status IN {PAID_SQL} THEN g.scheme_value ELSE 0 END),0)
                  FROM gratifications g
                  JOIN campaigns c ON c.id=g.campaign_id
                  LEFT JOIN divisions d ON d.id=c.division_id
                  {g_where} GROUP BY COALESCE(NULLIF(d.name,''), NULLIF(c.division,''), 'Unassigned')""", g_params)
    div_rewards = {r[0]: {"rewards_count": r[1], "rewards_eligible": r[2], "rewards_paid": r[3]}
                   for r in c.fetchall()}
    divisions = _merge_rewards(divisions, "division", div_rewards)

    return {
        "days": days,
        "scope": scope,
        "totals": totals,
        "prev": prev,
        "campaigns": campaigns,
        "members": members,
        "chemists": chemists,
        "brands": brands,
        "products": products,
        "divisions": divisions,
    }
