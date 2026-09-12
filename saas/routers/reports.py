"""
Reports router -- Excel exports for every report in the SOW plus report
scheduling (stored; execution by a worker/cron job).

Excel is generated with openpyxl and streamed to the client. Every report
respects the caller's hierarchy visibility scope (see saas/scoping.py).
"""
import io

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from openpyxl import Workbook

from ..audit import log_action
from ..db_utils import fetchall_dict
from ..deps import TenantContext, require_permission
from ..scoping import visible_user_ids

router = APIRouter(prefix="/api/v1/reports", tags=["reports"])


def _scope(ctx, conn):
    return visible_user_ids(conn, ctx)


def _build_report(conn, report_type: str, visible=None) -> tuple[list[list], list[str]]:
    c = conn.cursor()
    pcol = "pa.user_id"   # column used by scoping for POB-centric reports
    if report_type == "campaign":
        c.execute("""SELECT c.id, c.name, b.name AS brand, c.division, c.start_date,
                     c.end_date, c.status, c.scheme_type,
                     (SELECT count(*) FROM campaign_products cp WHERE cp.campaign_id=c.id) AS products,
                     (SELECT count(*) FROM pob_activities pa WHERE pa.campaign_id=c.id) AS pobs,
                     (SELECT coalesce(sum(pa.pob_amount),0) FROM pob_activities pa WHERE pa.campaign_id=c.id) AS amount
                     FROM campaigns c LEFT JOIN brands b ON b.id=c.brand_id ORDER BY c.id""")
        cols = ["ID", "Campaign", "Brand", "Division", "Start", "End", "Status",
                "Scheme", "Products", "POBs", "Amount"]
    elif report_type == "invoice":
        where, params = _scoped_where(visible, pcol)
        c.execute(f"""SELECT pa.id, pa.invoice_number, pa.invoice_date, pa.invoice_amount, pa.pob_amount,
                     pa.quantity, u.full_name AS mr, cmp.name AS campaign, ch.name AS chemist, pa.status,
                     v.status AS v_status, v.reason
                     FROM pob_activities pa
                     LEFT JOIN users u ON u.id=pa.user_id
                     LEFT JOIN campaigns cmp ON cmp.id=pa.campaign_id
                     LEFT JOIN chemists ch ON ch.id=pa.chemist_id
                     LEFT JOIN pob_verifications v ON v.id=pa.current_verification_id
                     {where}
                     ORDER BY pa.id DESC""", params)
        cols = ["POB ID", "Invoice No", "Invoice Date", "Invoice Amount", "POB Amount", "Qty",
                "MR", "Campaign", "Chemist", "Status", "Verification", "Reason"]
    elif report_type == "chemist":
        c.execute("SELECT * FROM chemists ORDER BY id")
        cols = ["ID", "Chemist", "Shop", "GST", "DL", "Owner", "Mobile", "City", "District",
                "State", "PIN", "OCID", "Doctor", "Category", "Area", "Status"]
    elif report_type in ("mr_performance", "leaderboard"):
        where, params = _scoped_where(visible, "u.id")
        c.execute(f"""SELECT u.id, u.full_name, h.name AS level, u.region, u.state,
                     count(pa.id) AS pobs, coalesce(sum(pa.pob_amount),0) AS amount,
                     sum(CASE WHEN pa.status='verified' THEN 1 ELSE 0 END) AS verified,
                     sum(CASE WHEN pa.status='rejected' THEN 1 ELSE 0 END) AS rejected
                     FROM users u
                     LEFT JOIN hierarchy_levels h ON h.id=u.hierarchy_level_id
                     LEFT JOIN pob_activities pa ON pa.user_id=u.id
                     {where}
                     GROUP BY u.id, u.full_name, h.name, u.region, u.state ORDER BY amount DESC""", params)
        cols = ["User ID", "Name", "Level", "Region", "State", "POBs", "Amount", "Verified", "Rejected"]
    elif report_type == "manager_performance":
        where, params = _manager_scope(visible)
        c.execute(f"""SELECT u.id, u.full_name, h.name AS level, u.region,
                     (SELECT count(*) FROM users c WHERE c.parent_id=u.id) AS direct_reports,
                     (SELECT count(*) FROM pob_activities pa JOIN users u2 ON u2.id=pa.user_id
                       WHERE u2.parent_id=u.id) AS team_pobs,
                     (SELECT coalesce(sum(pa.pob_amount),0) FROM pob_activities pa
                       JOIN users u2 ON u2.id=pa.user_id WHERE u2.parent_id=u.id) AS team_amount
                     FROM users u
                     LEFT JOIN hierarchy_levels h ON h.id=u.hierarchy_level_id
                     {where}
                     ORDER BY team_amount DESC""", params)
        cols = ["User ID", "Manager", "Level", "Region", "Direct Reports", "Team POBs", "Team Amount"]
    elif report_type == "brand_performance":
        where, params = _scoped_where(visible, "pa.user_id")
        c.execute(f"""SELECT b.id, b.name, count(pa.id) AS pobs, coalesce(sum(pa.pob_amount),0) AS amount,
                     count(DISTINCT cmp.id) AS campaigns
                     FROM brands b LEFT JOIN campaigns cmp ON cmp.brand_id=b.id
                     LEFT JOIN pob_activities pa ON pa.campaign_id=cmp.id
                     {where}
                     GROUP BY b.id ORDER BY amount DESC""", params)
        cols = ["Brand ID", "Brand", "POBs", "Amount", "Campaigns"]
    elif report_type == "verification_tat":
        where, params = _scoped_where(visible, "pa.user_id")
        c.execute(f"""SELECT v.id, v.pob_id, v.status, v.started_at, v.verified_at,
                     round(extract(epoch from (v.verified_at - v.created_at))/3600.0, 2) AS tat_hours,
                     u.full_name AS verifier, u2.full_name AS mr
                     FROM pob_verifications v
                     JOIN pob_activities pa ON pa.id=v.pob_id
                     LEFT JOIN users u ON u.id=v.verifier_id
                     LEFT JOIN users u2 ON u2.id=pa.user_id
                     {where}
                     ORDER BY v.id DESC""", params)
        cols = ["Verification ID", "POB", "Status", "Started", "Verified", "TAT (hrs)", "Verifier", "MR"]
    elif report_type == "duplicate":
        where, params = _scoped_and(visible, "pa.user_id")
        c.execute(f"""SELECT pa.id, pa.invoice_number, pa.content_hash, pa.status,
                     u.full_name AS mr, ch.name AS chemist, pa.created_at, v.reason
                     FROM pob_activities pa
                     LEFT JOIN users u ON u.id=pa.user_id
                     LEFT JOIN chemists ch ON ch.id=pa.chemist_id
                     LEFT JOIN pob_verifications v ON v.id=pa.current_verification_id
                     WHERE pa.status IN ('duplicate','needs_review'){where} ORDER BY pa.id DESC""", params)
        cols = ["POB ID", "Invoice No", "Content Hash", "Status", "MR", "Chemist", "Created", "Reason"]
    elif report_type == "gift":
        where, params = _scoped_and(visible, "g.user_id")
        c.execute(f"""SELECT g.id, u.full_name AS mr, cmp.name AS campaign, gift.name AS gift,
                     gift.cost, g.status, g.dispatch_status, g.delivery_status, g.gps_lat, g.gps_lng,
                     g.dispatched_at, g.delivered_at
                     FROM gratifications g
                     LEFT JOIN users u ON u.id=g.user_id
                     LEFT JOIN campaigns cmp ON cmp.id=g.campaign_id
                     LEFT JOIN gifts gift ON gift.id=g.gift_id
                     WHERE g.type_code='physical_gift'{where} ORDER BY g.id DESC""", params)
        cols = ["ID", "MR", "Campaign", "Gift", "Cost", "Status", "Dispatch", "Delivery",
                "Lat", "Lng", "Dispatched At", "Delivered At"]
    elif report_type == "cashback":
        where, params = _scoped_and(visible, "g.user_id")
        c.execute(f"""SELECT g.id, u.full_name AS mr, cmp.name AS campaign, g.type_code, g.scheme_value,
                     g.upi_id, g.status, g.payment_ref, g.paid_at
                     FROM gratifications g
                     LEFT JOIN users u ON u.id=g.user_id
                     LEFT JOIN campaigns cmp ON cmp.id=g.campaign_id
                     WHERE g.type_code IN ('cashback','upi'){where} ORDER BY g.id DESC""", params)
        cols = ["ID", "MR", "Campaign", "Type", "Value", "UPI", "Status", "Payment Ref", "Paid At"]
    elif report_type in ("daily", "weekly", "monthly"):
        period = {"daily": "YYYY-MM-DD", "weekly": "IYYY-IW", "monthly": "YYYY-MM"}[report_type]
        where, params = _scoped_where(visible, pcol)
        c.execute(f"""SELECT to_char(pa.created_at,'{period}') AS period, count(*) AS pobs,
                     coalesce(sum(pa.pob_amount),0) AS amount,
                     sum(CASE WHEN pa.status='verified' THEN 1 ELSE 0 END) AS verified,
                     sum(CASE WHEN pa.status='rejected' THEN 1 ELSE 0 END) AS rejected
                     FROM pob_activities pa
                     {where}
                     GROUP BY 1 ORDER BY 1 DESC LIMIT 365""", params)
        cols = ["Period", "POBs", "Amount", "Verified", "Rejected"]
    elif report_type == "state":
        where, params = _scoped_where(visible, "pa.user_id")
        c.execute(f"""SELECT ch.state, count(DISTINCT ch.id) AS chemists,
                     count(pa.id) AS pobs, coalesce(sum(pa.pob_amount),0) AS amount
                     FROM chemists ch
                     LEFT JOIN pob_activities pa ON pa.chemist_id=ch.id
                     {where}
                     GROUP BY ch.state ORDER BY amount DESC""", params)
        cols = ["State", "Chemists", "POBs", "Amount"]
    elif report_type == "approval":
        where, params = _scoped_where(visible, "pa.user_id")
        c.execute(f"""SELECT h.id, h.pob_id, h.action, h.reason, h.created_at,
                     u.full_name AS actor, u2.full_name AS mr
                     FROM verification_history h
                     JOIN pob_activities pa ON pa.id=h.pob_id
                     LEFT JOIN users u ON u.id=h.verifier_id
                     LEFT JOIN users u2 ON u2.id=pa.user_id
                     {where}
                     ORDER BY h.id DESC""", params)
        cols = ["History ID", "POB", "Action", "Reason", "When", "Actor", "MR"]
    elif report_type == "pending_verification":
        where, params = _scoped_and(visible, "pa.user_id")
        c.execute(f"""SELECT pa.id AS pob_id, pa.invoice_number, pa.invoice_amount, pa.pob_amount,
                     u.full_name AS mr, cmp.name AS campaign, ch.name AS chemist, pa.created_at
                     FROM pob_activities pa
                     LEFT JOIN users u ON u.id=pa.user_id
                     LEFT JOIN campaigns cmp ON cmp.id=pa.campaign_id
                     LEFT JOIN chemists ch ON ch.id=pa.chemist_id
                     WHERE pa.status='pending_verification'{where}
                     ORDER BY pa.id""", params)
        cols = ["POB ID", "Invoice No", "Invoice Amount", "POB Amount", "MR", "Campaign", "Chemist", "Submitted"]
    elif report_type == "visit":
        where, params = _scoped_where(visible, "cv.user_id")
        c.execute(f"""SELECT cv.id, cv.visit_date, ch.name AS chemist, ch.shop_name, ch.city, ch.state,
                     u.full_name AS psr, cmp.name AS campaign,
                     cv.opening_stock, cv.quantity_sold, cv.current_stock, cv.fresh_purchase,
                     cv.remarks, cv.created_at
                     FROM chemist_visits cv
                     LEFT JOIN chemists ch ON ch.id=cv.chemist_id
                     LEFT JOIN users u ON u.id=cv.user_id
                     LEFT JOIN campaigns cmp ON cmp.id=cv.campaign_id
                     {where}
                     ORDER BY cv.visit_date DESC, cv.id DESC""", params)
        cols = ["Visit ID", "Date", "Chemist", "Shop", "City", "State", "PSR", "Campaign",
                "Opening Stock", "Sold", "Current Stock", "Fresh Purchase", "Remarks", "Created"]
    elif report_type == "audit":
        c.execute("""SELECT id, actor, action, entity_type, entity_id, detail, ip, created_at
                     FROM audit_logs ORDER BY id DESC LIMIT 1000""")
        cols = ["ID", "Actor", "Action", "Entity", "Entity ID", "Detail", "IP", "Created"]
    else:
        raise HTTPException(404, f"unknown report type: {report_type}")
    return cols, [list(r) for r in c.fetchall()]


def _scoped_where(visible, column: str):
    """WHERE fragment + params. Returns ('', ()) when unrestricted."""
    if visible is None:
        return "", ()
    return f"WHERE {column} = ANY(%s)", (visible,)


def _scoped_and(visible, column: str):
    """AND fragment for queries that already have a WHERE clause."""
    if visible is None:
        return "", ()
    return f" AND {column} = ANY(%s)", (visible,)


def _manager_scope(visible):
    if visible is None:
        return "", ()
    return ("WHERE u.id = ANY(%s) OR u.id IN (SELECT parent_id FROM users WHERE id = ANY(%s))",
            (visible, visible))


# ── Scheduling ──────────────────────────────────────────────────────────────
# Registered before the /{report_type} catch-all so "schedules" is matched as
# the fixed route, not treated as a report type.

@router.get("/schedules")
def list_schedules(ctx: TenantContext = Depends(require_permission("report.schedule"))):
    conn = ctx.conn
    c = conn.cursor()
    c.execute("SELECT * FROM report_schedules ORDER BY id DESC")
    return {"items": fetchall_dict(c)}


@router.post("/schedules")
def create_schedule(body: dict, ctx: TenantContext = Depends(require_permission("report.schedule"))):
    if not body.get("name") or not body.get("report_type"):
        raise HTTPException(400, "name and report_type required")
    conn = ctx.conn
    c = conn.cursor()
    c.execute(
        """INSERT INTO report_schedules (name, report_type, format, cron, recipients, enabled)
           VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
        (body["name"], body["report_type"], body.get("format") or "excel", body.get("cron"),
         body.get("recipients") or "", body.get("enabled", True)),
    )
    sid = c.fetchone()[0]
    conn.commit()
    log_action(conn, ctx.user["id"], "report.schedule", "report_schedule", sid, {"type": body["report_type"]})
    return {"ok": True, "id": sid}


@router.delete("/schedules/{sid}")
def delete_schedule(sid: int, ctx: TenantContext = Depends(require_permission("report.schedule"))):
    conn = ctx.conn
    c = conn.cursor()
    c.execute("DELETE FROM report_schedules WHERE id=%s", (sid,))
    conn.commit()
    return {"ok": True}


@router.get("/{report_type}")
def export_report(report_type: str,
                  ctx: TenantContext = Depends(require_permission("report.export"))):
    visible = _scope(ctx, ctx.conn)
    cols, rows = _build_report(ctx.conn, report_type, visible)
    wb = Workbook()
    ws = wb.active
    ws.title = report_type
    ws.append(cols)
    for row in rows:
        ws.append([str(v) if v is not None else "" for v in row])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    log_action(ctx.conn, ctx.user["id"], "report.export", "report", None, {"type": report_type})
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={report_type}_report.xlsx"},
    )
