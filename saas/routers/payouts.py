"""
Payout router (Phase 3).

Batch payouts for the campaign payout cycle (instant / weekly / monthly).
  - run: collect eligible approved cash/UPI gratifications due on/up to today
         (or any cycle specified), create a payout_batch + items.
  - pay: mark a batch paid (all items) with a payment reference.
  - reconcile: reconcile a batch / individual items (bank statement ref).
Gift gratifications are not part of money batches; they are tracked via stock.
"""
import json

from fastapi import APIRouter, Depends, HTTPException

from ..audit import log_action
from ..db_utils import fetchall_dict, fetchone_dict
from ..deps import TenantContext, require_permission
from ..pagination import PageLimit, PageOffset

router = APIRouter(prefix="/api/v1", tags=["payout"])


def _batch_rows(c, extra="", params=(), limit=None, offset=None):
    sql = f"""
        SELECT b.*, COUNT(i.id) AS item_count,
               COALESCE(SUM(CASE WHEN i.status='paid' THEN 1 ELSE 0 END),0) AS paid_count
        FROM payout_batches b
        LEFT JOIN payout_batch_items i ON i.batch_id=b.id
        {extra}
        GROUP BY b.id ORDER BY b.id DESC"""
    if limit is not None:
        sql += " LIMIT %s OFFSET %s"
        params = tuple(params) + (limit, offset or 0)
    c.execute(sql, params)
    return fetchall_dict(c)


@router.get("/payouts")
def list_batches(status: str = "", limit: int = PageLimit(default=200), offset: int = PageOffset(),
                 ctx: TenantContext = Depends(require_permission("payout.view"))):
    extra, params = "", ()
    if status:
        extra = "WHERE b.status=%s"
        params = (status,)
    return {"items": _batch_rows(ctx.conn.cursor(), extra, params, limit=limit, offset=offset)}


@router.get("/payouts/{bid}")
def get_batch(bid: int, ctx: TenantContext = Depends(require_permission("payout.view"))):
    conn = ctx.conn
    c = conn.cursor()
    c.execute("SELECT * FROM payout_batches WHERE id=%s", (bid,))
    batch = fetchone_dict(c)
    if not batch:
        raise HTTPException(404, "batch not found")
    c.execute("""
        SELECT i.*, g.type_code, g.scheme_value, g.upi_id, g.payout_cycle,
               u.full_name AS recipient, cmp.name AS campaign_name
        FROM payout_batch_items i
        JOIN gratifications g ON g.id=i.gratification_id
        JOIN users u ON u.id=g.user_id
        LEFT JOIN campaigns cmp ON cmp.id=g.campaign_id
        WHERE i.batch_id=%s ORDER BY i.id""", (bid,))
    batch["items"] = fetchall_dict(c)
    return batch


@router.post("/payouts/run")
def run_payout(body: dict, ctx: TenantContext = Depends(require_permission("payout.run"))):
    """Collect approved cash/UPI gratifications due for a cycle into a new batch."""
    conn = ctx.conn
    c = conn.cursor()
    cycle = (body.get("cycle") or "").strip().lower() or "instant"
    if cycle not in ("instant", "weekly", "monthly"):
        raise HTTPException(400, "cycle must be instant|weekly|monthly")

    c.execute("""SELECT g.*, u.full_name AS recipient
                 FROM gratifications g JOIN users u ON u.id=g.user_id
                 WHERE g.type_code IN ('cashback','upi')
                   AND g.status='approved'
                   AND COALESCE(g.payout_cycle,'instant')=%s
                   AND (COALESCE(g.payout_batch_date,CURRENT_DATE) <= CURRENT_DATE)
                   AND NOT EXISTS (SELECT 1 FROM payout_batch_items i WHERE i.gratification_id=g.id)
                 ORDER BY g.id""", (cycle,))
    rows = fetchall_dict(c)
    if not rows:
        raise HTTPException(404, f"no approved gratifications due for cycle '{cycle}'")

    total = round(sum(r["scheme_value"] or 0 for r in rows), 2)
    c.execute("""INSERT INTO payout_batches (cycle, period_start, period_end, total_amount,
                 status, created_by)
                 VALUES (%s, CURRENT_DATE, CURRENT_DATE, %s, 'open', %s) RETURNING id""",
              (cycle, total, ctx.user["id"]))
    bid = c.fetchone()[0]
    for r in rows:
        c.execute("""INSERT INTO payout_batch_items (batch_id, gratification_id, amount, status)
                     VALUES (%s,%s,%s,'pending')""",
                  (bid, r["id"], r["scheme_value"] or 0))
    conn.commit()
    log_action(conn, ctx.user["id"], "payout.run", "payout_batch", bid,
               {"cycle": cycle, "items": len(rows), "total": total})
    return {"ok": True, "batch_id": bid, "cycle": cycle, "items": len(rows), "total": total}


@router.post("/payouts/{bid}/pay")
def mark_batch_paid(bid: int, body: dict, ctx: TenantContext = Depends(require_permission("payout.run"))):
    payment_ref = (body.get("payment_ref") or "").strip()
    if not payment_ref:
        raise HTTPException(400, "payment_ref required")
    conn = ctx.conn
    c = conn.cursor()
    c.execute("SELECT status FROM payout_batches WHERE id=%s", (bid,))
    row = c.fetchone()
    if not row:
        raise HTTPException(404, "batch not found")
    if row[0] == "reconciled":
        raise HTTPException(409, "batch already reconciled")
    c.execute("UPDATE payout_batches SET status='paid', payment_ref=%s WHERE id=%s", (payment_ref, bid))
    c.execute("UPDATE payout_batch_items SET status='paid' WHERE batch_id=%s", (bid,))
    c.execute("""UPDATE gratifications g SET status='completed', payment_ref=%s, paid_at=CURRENT_TIMESTAMP,
                 completed_at=CURRENT_TIMESTAMP
                 FROM payout_batch_items i WHERE i.batch_id=%s AND i.gratification_id=g.id
                   AND g.type_code IN ('cashback','upi')""",
              (payment_ref, bid))
    conn.commit()
    log_action(conn, ctx.user["id"], "payout.pay", "payout_batch", bid, {"payment_ref": payment_ref})
    return {"ok": True, "status": "paid", "payment_ref": payment_ref}


@router.post("/payouts/{bid}/reconcile")
def reconcile_batch(bid: int, body: dict, ctx: TenantContext = Depends(require_permission("payout.reconcile"))):
    ref = (body.get("reconciliation_ref") or "").strip()
    conn = ctx.conn
    c = conn.cursor()
    c.execute("SELECT status FROM payout_batches WHERE id=%s", (bid,))
    row = c.fetchone()
    if not row:
        raise HTTPException(404, "batch not found")
    if row[0] != "paid":
        raise HTTPException(409, "only paid batches can be reconciled")
    c.execute("UPDATE payout_batches SET status='reconciled', payment_ref=%s WHERE id=%s",
              (ref or row[0], bid))
    c.execute("UPDATE payout_batch_items SET status='reconciled' WHERE batch_id=%s", (bid,))
    c.execute("""UPDATE gratifications g SET reconciled=TRUE, reconciliation_ref=%s
                 FROM payout_batch_items i WHERE i.batch_id=%s AND i.gratification_id=g.id""",
              (ref, bid))
    conn.commit()
    log_action(conn, ctx.user["id"], "payout.reconcile", "payout_batch", bid, {"ref": ref})
    return {"ok": True, "status": "reconciled"}
