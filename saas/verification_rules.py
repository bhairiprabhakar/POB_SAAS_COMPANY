"""
Campaign verification rule evaluation.

Separate from gratification rules — these are verification-specific eligibility
checks evaluated during the automated verification pipeline.
"""
import json
import logging
from datetime import datetime, timedelta

log = logging.getLogger("saas.verification_rules")


def evaluate_verification_rules(conn, pob_data: dict, invoice_data: dict,
                                 campaign_id: int) -> list:
    """Evaluate campaign_verification_rules for a POB + invoice.

    Returns a list of dicts:
        [{"rule_id": int, "rule_type": str, "passed": bool, "detail": str,
          "action_on_fail": str, "params": dict}, ...]
    """
    c = conn.cursor()
    c.execute("""
        SELECT id, rule_type, params, action_on_fail
        FROM campaign_verification_rules
        WHERE campaign_id = %s AND active = TRUE
        ORDER BY id
    """, (campaign_id,))
    rules = c.fetchall()

    if not rules:
        return []

    results = []
    for rule_id, rule_type, params, action_on_fail in rules:
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except (json.JSONDecodeError, TypeError):
                params = {}

        passed = True
        detail = ""

        if rule_type == "min_quantity":
            min_val = float(params.get("min", 0))
            actual = float(invoice_data.get("quantity") or pob_data.get("quantity") or 0)
            if actual < min_val:
                passed = False
                detail = f"Quantity {actual:.0f} is below minimum {min_val:.0f}"

        elif rule_type == "max_quantity":
            max_val = float(params.get("max", 999999))
            actual = float(invoice_data.get("quantity") or pob_data.get("quantity") or 0)
            if actual > max_val:
                passed = False
                detail = f"Quantity {actual:.0f} exceeds maximum {max_val:.0f}"

        elif rule_type == "min_pob":
            min_val = float(params.get("min", 0))
            actual = float(pob_data.get("pob_amount") or 0)
            if actual < min_val:
                passed = False
                detail = f"POB amount ₹{actual:.2f} is below minimum ₹{min_val:.2f}"

        elif rule_type == "max_pob":
            max_val = float(params.get("max", 9999999))
            actual = float(pob_data.get("pob_amount") or 0)
            if actual > max_val:
                passed = False
                detail = f"POB amount ₹{actual:.2f} exceeds maximum ₹{max_val:.2f}"

        elif rule_type == "campaign_period":
            invoice_date_str = invoice_data.get("invoice_date")
            if invoice_date_str:
                try:
                    inv_date = datetime.strptime(str(invoice_date_str)[:10], "%Y-%m-%d").date()
                except (ValueError, TypeError):
                    inv_date = None
                if inv_date:
                    c.execute("SELECT start_date, end_date, grace_days, grace_months, pre_grace_days "
                              "FROM campaigns WHERE id=%s", (campaign_id,))
                    cmp = c.fetchone()
                    if cmp:
                        start, end, grace_d, grace_m, pre_grace = cmp
                        floor = None
                        if start:
                            floor = start - timedelta(days=int(pre_grace or 0))
                        ceiling = None
                        if end:
                            m = end.month + int(grace_m or 0)
                            y = end.year + (m - 1) // 12
                            m = (m - 1) % 12 + 1
                            ceiling = end.replace(month=m, year=y) + timedelta(days=int(grace_d or 15))
                        if floor and inv_date < floor:
                            passed = False
                            detail = f"Invoice date {inv_date} is before campaign window ({floor})"
                        elif ceiling and inv_date > ceiling:
                            passed = False
                            detail = f"Invoice date {inv_date} is after campaign window ({ceiling})"

        elif rule_type == "chemist_match":
            inv_buyer = ""
            buyer = invoice_data.get("buyer") or {}
            if isinstance(buyer, dict):
                inv_buyer = (buyer.get("name") or "").strip()
            sub_chemist = (pob_data.get("chemist_name") or "").strip()
            if inv_buyer and sub_chemist:
                def _norm(s):
                    return "".join(c for c in s.lower() if c.isalnum())
                a, b = _norm(sub_chemist), _norm(inv_buyer)
                if not (a and b and (a == b or a in b or b in a)):
                    passed = False
                    detail = f"Invoice buyer '{inv_buyer}' does not match submitted chemist '{sub_chemist}'"

        elif rule_type == "ptr_match":
            submitted_ptr = pob_data.get("ptr")
            invoice_ptr = invoice_data.get("ptr") or invoice_data.get("rate")
            if submitted_ptr is not None and invoice_ptr is not None:
                if abs(float(submitted_ptr) - float(invoice_ptr)) > 0.01:
                    passed = False
                    detail = f"Submitted PTR {submitted_ptr} does not match invoice rate {invoice_ptr}"

        results.append({
            "rule_id": rule_id,
            "rule_type": rule_type,
            "passed": passed,
            "detail": detail,
            "action_on_fail": action_on_fail,
            "params": params,
        })

    return results


def add_verification_rule(conn, campaign_id: int, rule_type: str,
                           params: dict = None, action_on_fail: str = "flag") -> int:
    """Insert a new campaign verification rule. Returns the rule ID."""
    c = conn.cursor()
    c.execute("""
        INSERT INTO campaign_verification_rules (campaign_id, rule_type, params, action_on_fail)
        VALUES (%s, %s, %s, %s) RETURNING id
    """, (campaign_id, rule_type, json.dumps(params or {}), action_on_fail))
    rule_id = c.fetchone()[0]
    conn.commit()
    return rule_id


def remove_verification_rule(conn, rule_id: int) -> bool:
    """Soft-delete a verification rule by setting active=FALSE."""
    c = conn.cursor()
    c.execute("UPDATE campaign_verification_rules SET active=FALSE WHERE id=%s", (rule_id,))
    conn.commit()
    return c.rowcount > 0


def list_verification_rules(conn, campaign_id: int) -> list:
    """List all active verification rules for a campaign."""
    from .db_utils import fetchall_dict
    c = conn.cursor()
    c.execute("""
        SELECT id, campaign_id, rule_type, params, action_on_fail, active, created_at
        FROM campaign_verification_rules
        WHERE campaign_id = %s AND active = TRUE
        ORDER BY id
    """, (campaign_id,))
    return fetchall_dict(c)
