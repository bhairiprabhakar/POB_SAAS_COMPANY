"""
Gratification rule engine (Phase 2).

Campaign rules are stored as condition/action pairs in gratification_rules.
When a POB is finally approved, the first matching rule (by priority) decides
the gratification: which type, what value, and (for gifts) which gift.

Condition example:
  [{"field": "invoice_amount", "op": ">=", "value": 5000},
   {"field": "quantity",      "op": ">=", "value": 50}]
  -> gift = Mixer

Action: {"action": "gift"|"cashback"|"voucher"|"points"|"coupon",
         "value": <number>, "gift_id": <id>}
"""
from .db_utils import fetchone_dict


def evaluate(conn, campaign_id, ctx: dict) -> dict | None:
    """Return the winning rule (as a dict) for campaign_id, or None."""
    c = conn.cursor()
    c.execute(
        "SELECT * FROM gratification_rules WHERE campaign_id=%s AND active=TRUE "
        "ORDER BY priority ASC, id ASC",
        (campaign_id,),
    )
    rules = fetchall_rule(c)
    for rule in rules:
        if _conditions_match(rule.get("conditions") or [], ctx):
            return rule
    return None


def default_action(campaign: dict) -> dict:
    """Fallback when no rule matches -- honour the campaign's scheme_type."""
    return {
        "action": campaign.get("scheme_type") or "others",
        "value": 0,
        "gift_id": None,
    }


def decide(conn, campaign: dict, ctx: dict) -> dict | None:
    """Choose the gratification action for an approved POB.

    Returns the winning rule dict when a condition matches, or None when no
    rule matches (the caller should skip gratification creation so the POB
    can be re-evaluated later when siblings push the aggregate over the
    threshold).
    """
    rule = evaluate(conn, campaign["id"], ctx)
    if rule:
        return {
            "action": rule.get("then_action") or "others",
            "value": rule.get("value") or 0,
            "gift_id": rule.get("gift_id"),
            "rule_name": rule.get("name"),
        }
    return None


def fetchall_rule(c) -> list[dict]:
    import json
    rows = c.fetchall()
    cols = [d[0] for d in c.description]
    out = []
    for row in rows:
        r = dict(zip(cols, row))
        if isinstance(r.get("conditions"), str):
            r["conditions"] = json.loads(r["conditions"])
        out.append(r)
    return out


def _conditions_match(conditions: list, ctx: dict) -> bool:
    if not conditions:
        return True
    for cond in conditions:
        field = cond.get("field")
        op = cond.get("op")
        expected = cond.get("value")
        actual = ctx.get(field)
        if not _compare(actual, op, expected):
            return False
    return True


def _compare(actual, op, expected) -> bool:
    if op == "in":
        return str(actual) in [str(x) for x in (expected or [])]
    if op == "contains":
        return str(expected) in str(actual or "")
    if op in (">=", ">", "<=", "<"):
        try:
            a, b = float(actual or 0), float(expected or 0)
        except (TypeError, ValueError):
            return False
        if op == ">=":
            return a >= b
        if op == ">":
            return a > b
        if op == "<=":
            return a <= b
        return a < b
    if op == "==":
        return str(actual) == str(expected)
    if op == "!=":
        return str(actual) != str(expected)
    return False


# ── Human-readable labels for rule context fields ──────────────────────────────
FIELD_LABELS = {
    "invoice_amount": "Individual invoice value",
    "pob_amount": "POB value",
    "quantity": "Quantity",
    "ptr": "PTR",
    "mrp": "MRP",
    "product_count": "Product count",
    "chemist_city": "City",
    "chemist_monthly_total_invoice": "Total invoice value (this month)",
    "chemist_monthly_total_pob": "Total POB value (this month)",
    "chemist_monthly_total_qty": "Total quantity (this month)",
    "chemist_monthly_pob_count": "POB count (this month)",
    "chemist_monthly_invoice_count": "Invoice count (this month)",
}


def _gap_for_condition(cond: dict, actual, expected) -> dict | None:
    """Return shortfall details for a single condition that is NOT met."""
    field = cond.get("field")
    op = cond.get("op")
    label = FIELD_LABELS.get(field, field)

    if op in (">=", ">", "<=", "<"):
        try:
            a = float(actual or 0)
            b = float(expected or 0)
        except (TypeError, ValueError):
            return None
        if op in (">=", ">"):
            gap = b - a
            if gap > 0:
                return {"field": field, "label": label, "required": b, "current": a,
                        "shortfall": round(gap, 2), "message": f"Need ₹{round(gap, 2):,.0f} more"}
        elif op in ("<=", "<"):
            gap = a - b
            if gap > 0:
                return {"field": field, "label": label, "required": b, "current": a,
                        "shortfall": round(gap, 2), "message": f"Currently ₹{round(gap, 2):,.0f} over the limit"}

    if op == "in":
        if str(actual) not in [str(x) for x in (expected or [])]:
            return {"field": field, "label": label, "required": expected, "current": actual,
                    "shortfall": 1, "message": f"'{actual}' is not in allowed values ({', '.join(str(v) for v in (expected or []))})"}

    return None


def evaluate_proximity(conn, campaign_id: int, ctx: dict) -> dict:
    """Evaluate all active rules for a campaign and return eligibility + gap analysis.

    Returns:
    {
        "eligible": True/False,
        "matched_rule": { name, action, value } or None,
        "shortfalls": [
            { "field", "label", "required", "current", "shortfall", "message" },
            ...
        ],
        "nearest_rule": { name, missing: [...] }  # closest unmet rule
    }
    """
    c = conn.cursor()
    c.execute(
        "SELECT * FROM gratification_rules WHERE campaign_id=%s AND active=TRUE "
        "ORDER BY priority ASC, id ASC",
        (campaign_id,),
    )
    rules = fetchall_rule(c)

    if not rules:
        return {"eligible": False, "matched_rule": None, "shortfalls": [],
                "nearest_rule": None, "message": "No gratification rules configured for this campaign"}

    for rule in rules:
        conds = rule.get("conditions") or []
        if _conditions_match(conds, ctx):
            return {
                "eligible": True,
                "matched_rule": {"name": rule.get("name"), "action": rule.get("then_action"),
                                 "value": rule.get("value"), "gift_id": rule.get("gift_id")},
                "shortfalls": [],
                "nearest_rule": None,
                "message": f"Eligible for {rule.get('then_action', 'gratification')}"
                           + (f" (worth ₹{rule.get('value')})" if rule.get("value") else ""),
            }

    # No rule matched — find the nearest rule (fewest unmet conditions)
    best = None
    best_missing = []
    for rule in rules:
        conds = rule.get("conditions") or []
        missing = []
        for cond in conds:
            actual = ctx.get(cond.get("field"))
            if not _compare(actual, cond.get("op"), cond.get("value")):
                gap = _gap_for_condition(cond, actual, cond.get("value"))
                if gap:
                    missing.append(gap)
        if best is None or len(missing) < len(best_missing):
            best = rule
            best_missing = missing

    return {
        "eligible": False,
        "matched_rule": None,
        "shortfalls": best_missing,
        "nearest_rule": {"name": best.get("name") if best else None, "missing": best_missing},
        "message": _build_shortfall_message(best_missing),
    }


def _build_shortfall_message(shortfalls: list[dict]) -> str:
    """Build a human-readable message explaining why the user is not eligible."""
    if not shortfalls:
        return "Not eligible — no matching rule found"
    parts = []
    for s in shortfalls:
        parts.append(s.get("message", ""))
    return "To qualify: " + "; ".join(parts)


def build_rule_context(pob: dict, products: list[dict]) -> dict:
    """Normalize a POB into the fields the rule engine compares against."""
    return {
        "invoice_amount": float(pob.get("invoice_amount") or 0),
        "pob_amount": float(pob.get("pob_amount") or 0),
        "quantity": int(pob.get("quantity") or 0),
        "ptr": float(pob.get("ptr") or 0),
        "mrp": float(pob.get("mrp") or 0),
        "product_count": len(products or []),
        "invoice_number": str(pob.get("invoice_number") or ""),
        "chemist_city": str(pob.get("chemist_city") or ""),
    }


def next_payout_date(cycle: str, weekday, month_day):
    """Return the next scheduled payout date for a gratification cycle."""
    from datetime import date, timedelta
    today = date.today()
    if cycle == "weekly":
        dow = int(weekday or 0)  # 0=Monday .. 6=Sunday
        days_ahead = (dow - today.weekday()) % 7
        return today + timedelta(days=days_ahead) if days_ahead else today + timedelta(days=7)
    if cycle == "monthly":
        day = min(int(month_day or 1), 28)
        nxt = today.replace(day=day)
        if nxt <= today:
            y, m = today.year, today.month
            if m == 12:
                y, m = y + 1, 1
            else:
                m += 1
            nxt = date(y, m, day)
        return nxt
    return today


def create_gratification(conn, pob_id: int, user_id: int, campaign_id: int,
                         pob_amount: float, actor_id: int) -> tuple[int | None, dict]:
    """Decide gratification for an approved POB and its siblings.

    When a chemist has multiple invoices in the same month for the same campaign,
    each POB is evaluated individually BUT the rule engine also sees the aggregate
    totals (total invoice amount, total POB count, etc.) across all verified POBs
    for that chemist+campaign+month.

    If any previously non-eligible POB now qualifies because a sibling pushed the
    aggregate over the threshold, a gratification is created retroactively.

    Returns (gratification_id | None, decision) for the current POB.
    """
    c = conn.cursor()

    c.execute("SELECT * FROM campaigns WHERE id=%s", (campaign_id,))
    campaign = fetchone_dict(c)
    c.execute("SELECT * FROM pob_activities WHERE id=%s", (pob_id,))
    pob = fetchone_dict(c)
    if not campaign or not pob:
        return None, {}

    # ── 1. Find all verified POBs for same chemist+campaign+month without gratification ──
    c.execute("""
        SELECT pa.id FROM pob_activities pa
        WHERE pa.chemist_id = %s
          AND pa.campaign_id = %s
          AND pa.status = 'verified'
          AND date_trunc('month', pa.created_at) = date_trunc('month', %s::timestamp)
          AND NOT EXISTS (
              SELECT 1 FROM gratifications g WHERE g.pob_id = pa.id
          )
        ORDER BY pa.id
    """, (pob["chemist_id"], campaign_id, pob["created_at"]))
    ungratified_ids = [row[0] for row in c.fetchall()]

    if not ungratified_ids:
        return None, {}

    # ── 2. Build aggregate context across ALL verified POBs in the month ──
    c.execute("""
        SELECT
            COALESCE(SUM(invoice_amount), 0)  AS total_invoice,
            COALESCE(SUM(pob_amount), 0)      AS total_pob,
            COALESCE(SUM(quantity), 0)         AS total_qty,
            COUNT(*)                           AS pob_count,
            COUNT(DISTINCT invoice_number)     AS invoice_count
        FROM pob_activities
        WHERE chemist_id = %s
          AND campaign_id = %s
          AND status = 'verified'
          AND date_trunc('month', created_at) = date_trunc('month', %s::timestamp)
    """, (pob["chemist_id"], campaign_id, pob["created_at"]))
    row = c.fetchone()
    agg = {
        "chemist_monthly_total_invoice": float(row[0] or 0),
        "chemist_monthly_total_pob": float(row[1] or 0),
        "chemist_monthly_total_qty": int(row[2] or 0),
        "chemist_monthly_pob_count": int(row[3] or 0),
        "chemist_monthly_invoice_count": int(row[4] or 0),
    }

    # ── 3. Evaluate each ungratified POB with its own fields + aggregate ──
    created = []
    for uid in ungratified_ids:
        c.execute("SELECT * FROM pob_activities WHERE id=%s", (uid,))
        sibling = fetchone_dict(c)
        ctx = build_rule_context(sibling, [])
        ctx.update(agg)
        decision = decide(conn, campaign, ctx)

        # No rule matched → skip gratification; POB stays "verified" without
        # a gratification row so it can be re-evaluated when a later sibling
        # pushes the aggregate over the threshold.
        if decision is None:
            continue

        value_basis = float(sibling.get("invoice_amount") or 0) or (sibling.get("pob_amount") or 0)

        c.execute(
            """INSERT INTO gratifications (pob_id, user_id, campaign_id, type_code, scheme_value,
               status, eligible_at, payout_cycle, payout_batch_date)
               VALUES (%s,%s,%s,%s,%s,'eligible',CURRENT_TIMESTAMP,%s,%s) RETURNING id""",
            (uid, sibling["user_id"], campaign_id, decision["action"],
             decision["value"] or value_basis,
             campaign.get("payout_cycle") or "instant",
             next_payout_date(campaign.get("payout_cycle") or "instant",
                              campaign.get("payout_weekday"), campaign.get("payout_month_day"))),
        )
        gid = c.fetchone()[0]
        if decision.get("gift_id"):
            c.execute("UPDATE gratifications SET gift_id=%s WHERE id=%s", (decision["gift_id"], gid))
        apply_action(conn, gid, decision, fallback_value=value_basis)
        c.execute("INSERT INTO gratification_events (gratification_id, event, detail, actor_id) "
                  "VALUES (%s,%s,%s,%s)", (gid, 'eligible', f'POB #{uid} approved', actor_id))
        created.append((uid, gid, decision))

    conn.commit()

    for uid, gid, decision in created:
        if uid == pob_id:
            return gid, decision
    return None, {}


def apply_action(conn, gratification_id: int, decision: dict, fallback_value: float = 0.0) -> None:
    """Write the rule decision onto a gratification row."""
    c = conn.cursor()
    action = decision.get("action")
    value = decision.get("value") or fallback_value or 0
    gift_id = decision.get("gift_id")
    if action in ("gift", "physical_gift") and gift_id is not None:
        c.execute("UPDATE gratifications SET type_code='physical_gift', gift_id=%s, scheme_value=%s WHERE id=%s",
                  (gift_id, value, gratification_id))
    else:
        c.execute("UPDATE gratifications SET type_code=%s, scheme_value=%s WHERE id=%s",
                  (action, value, gratification_id))
    if decision.get("rule_name"):
        c.execute("INSERT INTO gratification_events (gratification_id, event, detail) VALUES (%s,%s,%s)",
                  (gratification_id, "rule.applied", decision["rule_name"]))
    conn.commit()


def list_rules(conn, campaign_id: int | None = None) -> list[dict]:
    c = conn.cursor()
    if campaign_id:
        c.execute("SELECT * FROM gratification_rules WHERE campaign_id=%s ORDER BY priority, id", (campaign_id,))
    else:
        c.execute("SELECT * FROM gratification_rules ORDER BY campaign_id, priority, id")
    return fetchall_rule(c)


def create_rule(conn, body: dict, campaign_id: int, actor_id: int) -> int:
    import json
    c = conn.cursor()
    c.execute(
        """INSERT INTO gratification_rules (campaign_id, name, priority, conditions, then_action, value, gift_id, active, created_by)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (campaign_id, body.get("name") or "Untitled rule", body.get("priority") or 0,
         json.dumps(body.get("conditions") or []), body.get("then_action") or "gift",
         body.get("value") or 0, body.get("gift_id"), body.get("active", True), actor_id),
    )
    conn.commit()
    return c.fetchone()[0]


def update_rule(conn, rule_id: int, body: dict, campaign_id: int) -> None:
    import json
    c = conn.cursor()
    c.execute(
        """UPDATE gratification_rules SET campaign_id=%s, name=%s, priority=%s, conditions=%s,
           then_action=%s, value=%s, gift_id=%s, active=%s WHERE id=%s""",
        (campaign_id, body.get("name") or "Untitled rule", body.get("priority") or 0,
         json.dumps(body.get("conditions") or []), body.get("then_action") or "gift",
         body.get("value") or 0, body.get("gift_id"), body.get("active", True), rule_id),
    )
    conn.commit()


def delete_rule(conn, rule_id: int) -> None:
    c = conn.cursor()
    c.execute("DELETE FROM gratification_rules WHERE id=%s", (rule_id,))
    conn.commit()
