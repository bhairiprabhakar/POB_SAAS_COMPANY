"""
Tenant credit ledger (Batch 1b, ported from app/helpers.py).

The legacy portal stored one credit wallet per company (credits.company_id
UNIQUE). In the merged SaaS the tenant database IS the company, so the wallet
is a **singleton row** (`credits.id = 1`, enforced with a CHECK) and every
function here operates on the caller's tenant connection -- no function opens
or closes a connection, matching the rest of saas/ (see saas/audit.py).

Semantics preserved from the legacy port:
  - consume_credit() deducts and appends a credit_transactions row carrying
    the post-operation balance; returns (False, remaining) when the wallet is
    short.
  - An unexpected DB error never aborts the caller's extraction pipeline: the
    legacy code reported (True, -1) and continued, and the merged port keeps
    that fail-open shape (log + rollback on our shared connection only).
"""
import logging
from datetime import datetime

from .db_utils import fetchall_dict, fetchone_dict

log = logging.getLogger("saas.credits")

_DEMO_TOTAL = 100


def ensure_credits_row(conn) -> dict:
    """Return the singleton wallet row, seeding the demo wallet (100 credits,
    'demo' plan) if the tenant has none yet. Idempotent."""
    c = conn.cursor()
    c.execute("SELECT * FROM credits WHERE id=1")
    row = fetchone_dict(c)
    if row:
        return row
    c.execute(
        "INSERT INTO credits (id,total_credits,used_credits,plan) "
        "VALUES (1,%s,0,'demo') ON CONFLICT (id) DO NOTHING",
        (_DEMO_TOTAL,),
    )
    conn.commit()
    c.execute("SELECT * FROM credits WHERE id=1")
    return fetchone_dict(c)


def get_credits(conn) -> dict:
    """Current wallet state: total_credits, used_credits, remaining, plan."""
    row = ensure_credits_row(conn)
    total = row["total_credits"] or 0
    used = row["used_credits"] or 0
    return {
        "total_credits": total,
        "used_credits": used,
        "remaining": total - used,
        "plan": row.get("plan") or "demo",
        "updated_at": row.get("updated_at"),
    }


def consume_credit(conn, operation_type, division_id=None, user_id=None,
                   reference_id=None, detail=None, amount=1):
    """Deduct ``amount`` credits and append a ledger row.

    Returns ``(ok, remaining)`` -- ``ok`` is False (and the wallet untouched)
    when there are not enough credits left. Matches the legacy
    app/helpers.consume_credit() flow, minus the self-owned connection:
    callers pass ``ctx.conn``.
    """
    c = conn.cursor()
    try:
        row = ensure_credits_row(conn)
        remaining = (row["total_credits"] or 0) - (row["used_credits"] or 0)
        if remaining < amount:
            return False, remaining
        new_used = (row["used_credits"] or 0) + amount
        new_rem = (row["total_credits"] or 0) - new_used
        c.execute("UPDATE credits SET used_credits=%s, updated_at=%s WHERE id=1",
                  (new_used, datetime.now().isoformat()))
        c.execute(
            """INSERT INTO credit_transactions
               (division_id,user_id,operation_type,credits_used,reference_id,detail,balance_after)
               VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (division_id, user_id, operation_type, amount, reference_id, detail, new_rem),
        )
        conn.commit()
        return True, new_rem
    except Exception:
        # Fail-open like the legacy port: a ledger hiccup must not kill the
        # extraction pipeline that is consuming the credit. Roll back on the
        # caller's shared connection so it stays usable.
        log.exception("consume_credit failed; treating as consumed")
        try:
            conn.rollback()
        except Exception:
            pass
        return True, -1


def top_up_credits(conn, amount, plan=None) -> dict:
    """Add ``amount`` credits to the tenant wallet (super-admin allocate /
    approve flow). When ``plan`` is given the wallet plan is set too.
    Returns the updated wallet."""
    ensure_credits_row(conn)
    now = datetime.now().isoformat()
    c = conn.cursor()
    if plan:
        c.execute(
            "UPDATE credits SET total_credits=total_credits+%s, plan=%s, updated_at=%s WHERE id=1",
            (amount, plan, now),
        )
    else:
        c.execute("UPDATE credits SET total_credits=total_credits+%s, updated_at=%s WHERE id=1",
                  (amount, now))
    conn.commit()
    return get_credits(conn)


# ── Credit requests (tenant-side request, super-admin review) ───────────────

def request_credits(conn, requested_by, credits_requested, message=None) -> int:
    """File a pending credit request; returns the request id."""
    c = conn.cursor()
    c.execute(
        "INSERT INTO credit_requests (requested_by,credits_requested,message) "
        "VALUES (%s,%s,%s) RETURNING id",
        (requested_by, int(credits_requested), message),
    )
    rid = c.fetchone()[0]
    conn.commit()
    return rid


def pending_credit_requests(conn) -> list:
    """All pending credit requests, oldest-first query shape is DESC like the
    legacy SA page (newest first)."""
    c = conn.cursor()
    c.execute(
        """SELECT cr.*, u.full_name AS requester_name, u.employee_id
           FROM credit_requests cr JOIN users u ON u.id=cr.requested_by
           WHERE cr.status='pending' ORDER BY cr.created_at DESC""",
    )
    return fetchall_dict(c)


def approve_credit_request(conn, request_id, reviewed_by):
    """Approve a pending request: add the credits, mark reviewed and append a
    ledger row. Returns the request row, or None if it is missing / no longer
    pending."""
    c = conn.cursor()
    c.execute("SELECT * FROM credit_requests WHERE id=%s", (request_id,))
    req = fetchone_dict(c)
    if not req or req.get("status") != "pending":
        return None
    amount = req["credits_requested"]
    now = datetime.now().isoformat()
    ensure_credits_row(conn)
    c.execute("UPDATE credits SET total_credits=total_credits+%s, updated_at=%s WHERE id=1",
              (amount, now))
    c.execute(
        "UPDATE credit_requests SET status='approved', reviewed_by=%s, reviewed_at=%s WHERE id=%s",
        (reviewed_by, now, request_id),
    )
    c.execute("SELECT (total_credits-used_credits) FROM credits WHERE id=1")
    remaining = c.fetchone()[0]
    c.execute(
        """INSERT INTO credit_transactions
           (user_id,operation_type,credits_used,reference_id,detail,balance_after)
           VALUES (%s,'request_approved',%s,%s,%s,%s)""",
        (reviewed_by, amount, request_id,
         f"Approved credit request {request_id}: {amount} credits", remaining),
    )
    conn.commit()
    return req


def reject_credit_request(conn, request_id, reviewed_by) -> bool:
    """Reject a pending request. Returns True when a row changed."""
    c = conn.cursor()
    c.execute(
        "UPDATE credit_requests SET status='rejected', reviewed_by=%s, reviewed_at=%s "
        "WHERE id=%s AND status='pending'",
        (reviewed_by, datetime.now().isoformat(), request_id),
    )
    conn.commit()
    return c.rowcount > 0


# ── Ledger views ────────────────────────────────────────────────────────────

def credit_ledger(conn, limit=50, offset=0) -> list:
    """Recent credit_transactions joined with user/division names."""
    c = conn.cursor()
    c.execute(
        """SELECT ct.*, u.full_name AS user_name, d.name AS division_name
           FROM credit_transactions ct
           LEFT JOIN users u ON u.id=ct.user_id
           LEFT JOIN divisions d ON d.id=ct.division_id
           ORDER BY ct.id DESC LIMIT %s OFFSET %s""",
        (limit, offset),
    )
    return fetchall_dict(c)


def credit_usage_summary(conn) -> dict:
    """Per-operation credit usage for the tenant's credits view."""
    c = conn.cursor()
    c.execute(
        """SELECT operation_type, SUM(credits_used) AS credits, COUNT(*) AS n
           FROM credit_transactions GROUP BY operation_type ORDER BY credits DESC""",
    )
    rows = fetchall_dict(c)
    return {r["operation_type"]: {"credits": r["credits"] or 0, "count": r["n"] or 0} for r in rows}