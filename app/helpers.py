"""
Shared business-logic helpers -- ported unchanged from the original app.py.
No framework-specific code lives here except the `request`/`session` compat
proxies, which behave like Flask's globals (see app/compat.py).
"""
import hashlib
import json
import re
from datetime import datetime
from io import BytesIO

from .compat import request, session, send_file
from .config import UPLOAD_FOLDER
from .database import get_db, fetchone_dict, fetchall_dict
from .security import hash_pw, verify_pw, _looks_like_legacy_sha256

ROLES = ["mr", "asm", "rsm", "zsm", "nsm", "company_admin"]
ROLE_LABELS = {
    "mr":            "MR -- Medical Representative",
    "asm":           "ASM -- Area Sales Manager",
    "rsm":           "RSM -- Regional Sales Manager",
    "zsm":           "ZSM -- Zonal Sales Manager",
    "nsm":           "NSM -- National Sales Manager",
    "company_admin": "Admin / NSM",
    "super_admin":   "Super Admin",
}
ROLE_RANK = {"mr": 1, "asm": 2, "rsm": 3, "zsm": 4, "nsm": 5, "company_admin": 6, "super_admin": 99}


def _slugify(text):
    """Readable folder name -- keep letters/digits/spaces/hyphens, collapse spaces to underscore."""
    text = str(text or "").strip()
    text = re.sub(r"[^\w\s\-]", "", text)
    text = re.sub(r"\s+", "_", text)
    return text[:64] or "UNKNOWN"


def get_upload_path(company_id, user_id, filename, company_name=None, division_name=None):
    """
    Returns (stored_relative, absolute_path) for a new upload.
    Folder layout: uploads/{Company_Name}/{Division_Name}/filename
    Falls back to numeric IDs when names are not available.
    """
    import os
    co_folder  = _slugify(company_name)  if company_name  else str(company_id)
    div_folder = _slugify(division_name) if division_name else "General"
    folder     = os.path.join(UPLOAD_FOLDER, co_folder, div_folder)
    os.makedirs(folder, exist_ok=True)
    stored_relative = f"{co_folder}/{div_folder}/{filename}"
    abs_path        = os.path.join(UPLOAD_FOLDER, stored_relative)
    return stored_relative, abs_path


def generate_company_code():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT code FROM companies WHERE code LIKE 'CMP%' ORDER BY id DESC LIMIT 1")
    row = c.fetchone()
    conn.close()
    if row:
        try:
            n = int(row[0][3:]) + 1
        except ValueError:
            n = 1
    else:
        n = 1
    return f"CMP{n:03d}"


def make_company_code(short_name):
    import random
    sn = (short_name or "").strip().upper()[:4].ljust(4, "X")
    conn = get_db()
    c = conn.cursor()
    for _ in range(20):
        digits = f"{random.randint(1000, 9999)}"
        code = f"{sn}{digits}"
        c.execute("SELECT id FROM companies WHERE code=%s", (code,))
        if not c.fetchone():
            conn.close()
            return code
    conn.close()
    return None


def generate_reg_token():
    import secrets
    return secrets.token_urlsafe(24)


def get_company_credits(cid):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM credits WHERE company_id=%s", (cid,))
    row = fetchone_dict(c)
    conn.close()
    if not row:
        return {"company_id": cid, "total_credits": 0, "used_credits": 0, "remaining": 0, "plan": "demo"}
    rem = (row["total_credits"] or 0) - (row["used_credits"] or 0)
    return {"company_id": cid, "total_credits": row["total_credits"] or 0,
            "used_credits": row["used_credits"] or 0, "remaining": rem, "plan": row["plan"] or "demo"}


def consume_credit(company_id, operation_type, division_id=None, user_id=None,
                   reference_id=None, detail=None, amount=1):
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("SELECT * FROM credits WHERE company_id=%s", (company_id,))
        row = fetchone_dict(c)
        if not row:
            c.execute("INSERT INTO credits (company_id,total_credits,used_credits,plan) VALUES (%s,100,0,'demo') ON CONFLICT DO NOTHING",
                      (company_id,))
            conn.commit()
            c.execute("SELECT * FROM credits WHERE company_id=%s", (company_id,))
            row = fetchone_dict(c)
        remaining = (row["total_credits"] or 0) - (row["used_credits"] or 0)
        if remaining < amount:
            conn.close()
            return False, remaining
        new_used = (row["used_credits"] or 0) + amount
        new_rem  = (row["total_credits"] or 0) - new_used
        c.execute("UPDATE credits SET used_credits=%s, updated_at=%s WHERE company_id=%s",
                  (new_used, datetime.now().isoformat(), company_id))
        c.execute("""INSERT INTO credit_transactions
            (company_id,division_id,user_id,operation_type,credits_used,reference_id,detail,balance_after)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (company_id, division_id, user_id, operation_type, amount, reference_id, detail, new_rem))
        conn.commit()
        conn.close()
        return True, new_rem
    except Exception:
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        return True, -1


def log_activity(action, entity_type=None, entity_id=None, detail=None, company_id=None):
    try:
        user_id = session.get("user_id") or session.get("super_admin_id")
        actor   = session.get("super_admin_name", "")
        if not actor:
            u = get_user(user_id) if user_id else None
            actor = u["full_name"] if u else "System"
        if company_id is None:
            company_id = session.get("company_id")
        ip = request.remote_addr if request else None
        conn = get_db()
        c = conn.cursor()
        c.execute("""INSERT INTO activity_logs
               (company_id,user_id,actor,action,entity_type,entity_id,detail,ip)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (company_id, user_id, actor, action, entity_type, entity_id, detail, ip))
        conn.commit()
        conn.close()
    except Exception:
        pass


def get_user(uid):
    if not uid:
        return None
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT u.*,
               co.name as company_name, co.code as company_code,
               d.name as division_name,
               p.full_name  as parent_name,
               p.employee_id as parent_emp_id,
               p.mobile      as parent_mobile
        FROM users u
        LEFT JOIN companies co ON u.company_id=co.id
        LEFT JOIN divisions d  ON u.division_id=d.id
        LEFT JOIN users p      ON u.parent_id=p.id
        WHERE u.id=%s""", (uid,))
    row = fetchone_dict(c)
    conn.close()
    if row and row.get("role"):
        row["role"] = str(row["role"]).strip().lower()
    return row


def get_company(cid):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM companies WHERE id=%s", (cid,))
    row = fetchone_dict(c)
    conn.close()
    return row


def get_accessible_ids(user, scope="hierarchy"):
    """
    Returns user IDs accessible to this user based on role + scope.

    scope="hierarchy"  -- own subtree (default, used for dashboard/analytics)
    scope="mine"       -- only self
    scope="company"    -- all users in company (only company_admin level)

    Division scoping rules:
    - company_admin: sees all users in the company across all divisions
    - nsm: sees only users within their own division subtree (parent_id chain)
    - zsm/rsm/asm: see their direct subtree via recursive CTE (unchanged)
    - mr: sees only self
    """
    conn = get_db()
    c = conn.cursor()
    role = str(user.get("role", "mr") or "mr").strip().lower()

    if scope == "mine":
        ids = [user["id"]]

    elif role == "company_admin":
        # Only true admin sees full company
        c.execute("SELECT id FROM users WHERE company_id=%s AND status='active'",
                  (user["company_id"],))
        ids = [r[0] for r in c.fetchall()]

    elif role == "nsm":
        # NSM sees only their own division subtree via recursive CTE
        c.execute("""
            WITH RECURSIVE sub(id) AS (
                SELECT id FROM users WHERE id=%s
                UNION ALL
                SELECT u.id FROM users u JOIN sub s ON u.parent_id=s.id
                WHERE u.status='active'
            ) SELECT id FROM sub""", (user["id"],))
        ids = [r[0] for r in c.fetchall()]

    elif role in ("zsm", "rsm", "asm"):
        # Mid-level managers: own subtree
        c.execute("""
            WITH RECURSIVE sub(id) AS (
                SELECT id FROM users WHERE id=%s
                UNION ALL
                SELECT u.id FROM users u JOIN sub s ON u.parent_id=s.id
                WHERE u.status='active'
            ) SELECT id FROM sub""", (user["id"],))
        ids = [r[0] for r in c.fetchall()]

    else:
        # MR or unknown: only self
        ids = [user["id"]]

    conn.close()
    return ids


def require_parent_for_role(role, parent_id):
    role = str(role or "").strip().lower()
    return not (role in ("mr", "asm", "rsm", "zsm") and not parent_id)


# NOTE: login_required / admin_required / superadmin_required / agent_required
# decorators live in app/auth.py (kept together since they're framework-facing).


def classify_period(from_date_str, to_date_str):
    try:
        from datetime import datetime as _dt
        fmt_options = ["%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y"]
        d1 = d2 = None
        for fmt in fmt_options:
            try:
                d1 = _dt.strptime(from_date_str.strip(), fmt)
                break
            except Exception:
                pass
        for fmt in fmt_options:
            try:
                d2 = _dt.strptime(to_date_str.strip(), fmt)
                break
            except Exception:
                pass
        if not d1 or not d2:
            return "unknown", 0
        days = (d2 - d1).days
        if days <= 35:
            return "monthly", days
        elif days <= 100:
            return "quarterly", days
        elif days <= 200:
            return "half-yearly", days
        else:
            return "yearly", days
    except Exception:
        return "unknown", 0


def _parse_stmt_date(date_str):
    """Parse a statement date string using the same format list classify_period
    uses, so both functions agree on what a given date string means."""
    if not date_str:
        return None
    from datetime import datetime as _dt
    fmt_options = ["%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y"]
    for fmt in fmt_options:
        try:
            return _dt.strptime(date_str.strip(), fmt).date()
        except Exception:
            continue
    return None


def check_period_duplicate(company_id, stockist_name, from_date, to_date, exclude_upload_id=None):
    """
    Detect duplicate / overlapping statement periods for the same agency
    (stockist) within a company.

    The previous version only flagged an exact from/to date string match,
    which missed real duplicates such as:
      - a monthly statement re-uploaded with slightly different OCR'd
        date boundaries (e.g. 1-30 June vs 1-29 June),
      - a monthly statement whose period is already covered by an existing
        quarterly statement for the same agency, or vice versa.

    This version compares actual date ranges and flags any overlap, tagging
    each match with an `overlap_type` so the caller can show an accurate,
    period-aware warning:
      - "exact"     : identical from/to dates
      - "contains"  : the new period fully contains an existing one
                       (e.g. new quarterly upload covers an existing monthly)
      - "contained" : the new period is fully contained within an existing one
                       (e.g. new monthly upload falls inside an existing quarterly)
      - "partial"   : the two periods overlap but neither fully contains the other
    """
    new_from = _parse_stmt_date(from_date)
    new_to   = _parse_stmt_date(to_date)

    conn = get_db()
    c = conn.cursor()
    q = """SELECT u.id, u.upload_date, u.user_id, us.full_name as uploader,
                  e.statement_from_date, e.statement_to_date
           FROM extractions e JOIN uploads u ON e.upload_id=u.id
           JOIN users us ON u.user_id=us.id
           WHERE u.company_id=%s AND e.stockist_name=%s
           AND u.status='done'"""
    params = [company_id, stockist_name]
    if exclude_upload_id:
        q += " AND u.id!=%s"
        params.append(exclude_upload_id)
    c.execute(q, params)
    rows = fetchall_dict(c)
    conn.close()

    if not new_from or not new_to:
        # Can't reliably compare ranges without valid dates on the new upload
        # -- fall back to the previous exact-string-match behaviour.
        return [r for r in rows if r["statement_from_date"] == from_date
                and r["statement_to_date"] == to_date]

    overlaps = []
    for r in rows:
        e_from = _parse_stmt_date(r.get("statement_from_date"))
        e_to   = _parse_stmt_date(r.get("statement_to_date"))
        if not e_from or not e_to:
            continue
        if new_to < e_from or new_from > e_to:
            continue  # no overlap
        if new_from == e_from and new_to == e_to:
            r["overlap_type"] = "exact"
        elif new_from <= e_from and new_to >= e_to:
            r["overlap_type"] = "contains"
        elif new_from >= e_from and new_to <= e_to:
            r["overlap_type"] = "contained"
        else:
            r["overlap_type"] = "partial"
        r["existing_period_type"], _ = classify_period(
            r.get("statement_from_date", ""), r.get("statement_to_date", ""))
        overlaps.append(r)

    priority = {"exact": 0, "contains": 1, "contained": 1, "partial": 2}
    overlaps.sort(key=lambda r: priority.get(r["overlap_type"], 3))
    return overlaps


def get_current_quarter():
    """
    Indian fiscal year quarters:
      Q1 = April   – June      (months 4–6)
      Q2 = July    – September (months 7–9)
      Q3 = October – December  (months 10–12)
      Q4 = January – March     (months 1–3,  belongs to previous FY)
    """
    from datetime import date
    import calendar
    today = date.today()
    m = today.month

    # Map calendar month → fiscal quarter + start/end months
    if   4 <= m <= 6:   q, sm, em, fy = 1, 4,  6,  today.year
    elif 7 <= m <= 9:   q, sm, em, fy = 2, 7,  9,  today.year
    elif 10 <= m <= 12: q, sm, em, fy = 3, 10, 12, today.year
    else:               q, sm, em, fy = 4, 1,  3,  today.year   # Jan-Mar = Q4 of prev FY

    q_start = date(fy, sm, 1)
    q_end   = date(fy, em, calendar.monthrange(fy, em)[1])

    fy_label = f"FY {fy}-{str(fy+1)[2:]}" if q < 4 else f"FY {fy-1}-{str(fy)[2:]}"
    return {
        "quarter":      q,
        "label":        f"Q{q} {fy_label}",
        "from":         q_start.strftime("%Y-%m-%d"),
        "to":           q_end.strftime("%Y-%m-%d"),
        "from_display": q_start.strftime("%d/%m/%Y"),
        "to_display":   q_end.strftime("%d/%m/%Y"),
    }


def save_extraction(upload_id, data, company_id):
    conn = get_db()
    c = conn.cursor()
    raw_with_debug = json.dumps(data)
    c.execute("""INSERT INTO extractions
        (upload_id,stockist_name,stockist_gst,stockist_address,bill_number,bill_date,
         statement_from_date,statement_to_date,total_amount,total_quantity,
         discount_percent,discount_amount,net_sale,sgst,cgst,invoice_net,doc_type,raw_json)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING id""",
        (upload_id,
         str(data.get("stockist_name", "") or "")[:500],
         data.get("stockist_gst", ""),
         str(data.get("stockist_address", "") or "")[:1000],
         data.get("bill_number", ""),
         data.get("bill_date", ""),
         data.get("statement_from_date", ""),
         data.get("statement_to_date", ""),
         float(data.get("total_amount", 0) or 0),
         int(data.get("total_quantity", 0) or 0),
         float(data.get("discount_percent", 0) or 0),
         float(data.get("discount_amount", 0) or 0),
         float(data.get("net_sale", 0) or 0),
         float(data.get("sgst", 0) or 0),
         float(data.get("cgst", 0) or 0),
         float(data.get("invoice_net", 0) or 0),
         data.get("doc_type", "STATEMENT"),
         raw_with_debug))
    ext_id = c.fetchone()[0]

    parties_saved = 0
    items_saved   = 0

    for party in data.get("parties", []):
        pname = str(party.get("name", "") or "").strip()
        if not pname:
            continue
        c.execute("""INSERT INTO parties
            (extraction_id,name,type,area,dl_number,gst_number,total_quantity,total_amount)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (ext_id,
             pname[:500],
             str(party.get("type", "shop") or "shop")[:50],
             str(party.get("area", "") or "")[:200],
             str(party.get("dl_number", "") or "")[:100],
             str(party.get("gst_number", "") or "")[:50],
             int(party.get("party_total_quantity", 0) or 0),
             float(party.get("party_total_amount", 0) or 0)))
        pid = c.fetchone()[0]
        parties_saved += 1

        for item in party.get("items", []):
            brand = str(item.get("brand", "") or "").strip()
            qty   = int(item.get("quantity", 0) or 0)
            if not brand and qty == 0:
                continue
            c.execute("""INSERT INTO items
                (party_id,brand,mfg,pack,batch_no,expiry,hsn_code,
                 quantity,mrp,unit_rate,tax_type,discount_percent,final_amount)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (pid,
                 brand[:300],
                 str(item.get("mfg", "") or "")[:200],
                 str(item.get("pack", "") or "")[:100],
                 str(item.get("batch_no", "") or "")[:50],
                 str(item.get("expiry", "") or "")[:30],
                 str(item.get("hsn_code", "") or "")[:20],
                 qty,
                 float(item.get("mrp", 0) or 0),
                 float(item.get("unit_rate", 0) or 0),
                 str(item.get("tax_type", "") or "")[:20],
                 float(item.get("discount_percent", 0) or 0),
                 float(item.get("final_amount", 0) or 0)))
            items_saved += 1

    c.execute("UPDATE uploads SET status='done' WHERE id=%s", (upload_id,))
    conn.commit()
    conn.close()
    return {"parties_saved": parties_saved, "items_saved": items_saved}



def get_sa_vcounts():
    """Return pending verification count for sidebar badge -- used by all SA pages."""
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT status, COUNT(*) as cnt FROM manual_verifications GROUP BY status")
        rows = fetchall_dict(c)
        conn.close()
        return {r["status"]: r["cnt"] for r in rows}
    except Exception:
        return {}

def push_notification(company_id, user_id, recipient_role, event_type, subject, message, ref_id=None, ref_type=None):
    """Insert a notification into notification_log for in-app display."""
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("""INSERT INTO notification_log
            (company_id,user_id,recipient_role,event_type,subject,message,reference_id,reference_type)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (company_id, user_id, recipient_role, event_type, subject, message, ref_id, ref_type))
        conn.commit()
        conn.close()
    except Exception:
        pass


def get_agent_company_ids():
    """Return list of all active company IDs.
    Agents see ALL companies -- they claim tasks themselves by scope.
    Company restriction removed: agents are not bound to specific companies."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id FROM companies WHERE status='active'")
    ids = [r[0] for r in c.fetchall()]
    conn.close()
    return ids


def _build_verification_excel(mv_id):
    """Build verification Excel workbook -- shared by SA and agent download routes."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side, Protection
    from openpyxl.utils import get_column_letter

    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT mv.*, u.original_filename, u.stored_filename,
               co.name as company_name, co.code as company_code,
               e.id as ext_id, e.stockist_name, e.stockist_gst, e.stockist_address,
               e.statement_from_date, e.statement_to_date,
               e.invoice_net, e.total_quantity,
               e.bill_number, e.bill_date
        FROM manual_verifications mv
        JOIN uploads u ON mv.upload_id=u.id
        JOIN companies co ON u.company_id=co.id
        LEFT JOIN extractions e ON e.upload_id=u.id
        WHERE mv.id=%s
    """, (mv_id,))
    mv = fetchone_dict(c)
    if not mv:
        conn.close()
        return "Not found", 404

    c.execute("""
        SELECT p.*, array_agg(row_to_json(i)) as items_json
        FROM parties p
        LEFT JOIN items i ON i.party_id=p.id
        WHERE p.extraction_id=%s
        GROUP BY p.id ORDER BY p.name
    """, (mv["ext_id"],))
    parties_raw = fetchall_dict(c)

    # Update download timestamp
    c.execute("UPDATE manual_verifications SET excel_downloaded_at=%s WHERE id=%s",
              (datetime.now().isoformat(), mv_id))
    conn.commit()
    conn.close()

    wb = Workbook()
    thin = Side(style="thin", color="CCCCCC")
    B = Border(left=thin, right=thin, top=thin, bottom=thin)

    def hdr(ws, row, cols, bg="1A3560"):
        for i, t in enumerate(cols, 1):
            cl = ws.cell(row, i, t)
            cl.fill = PatternFill("solid", fgColor=bg)
            cl.font = Font(bold=True, color="FFFFFF", size=10)
            cl.alignment = Alignment(horizontal="center", vertical="center")
            cl.border = B

    def cell(ws, row, col, val, editable=False):
        cl = ws.cell(row, col, val)
        cl.border = B
        cl.font = Font(size=9, color="000000" if editable else "444444")
        if editable:
            cl.fill = PatternFill("solid", fgColor="FFFDE7")  # light yellow = editable
        if isinstance(val, float):
            cl.number_format = "#,##0.00"
        cl.alignment = Alignment(vertical="center")
        return cl

    # ── Sheet 1: META (read-only reference) ─────────────────────────────────
    ws_meta = wb.active
    ws_meta.title = "Meta"
    ws_meta["A1"] = "MANUAL VERIFICATION SHEET"
    ws_meta["A1"].font = Font(bold=True, size=14, color="1A3560")
    ws_meta["A1"].alignment = Alignment(horizontal="center")
    ws_meta.merge_cells("A1:D1")

    meta_rows = [
        ("Verification ID", mv_id),
        ("Company", f"{mv['company_name']} ({mv['company_code']})"),
        ("Stockist / Agency", mv["stockist_name"] or ""),
        ("GST", mv["stockist_gst"] or ""),
        ("Address", mv["stockist_address"] or ""),
        ("Statement From", mv["statement_from_date"] or ""),
        ("Statement To", mv["statement_to_date"] or ""),
        ("Bill Number", mv["bill_number"] or ""),
        ("Original File", mv["original_filename"] or ""),
        ("Invoice Net (₹)", mv["invoice_net"] or 0),
        ("Total Qty", mv["total_quantity"] or 0),
        ("Instructions", "Yellow cells are editable. Do NOT change column headers or mv_id."),
    ]
    for r, (lbl, val) in enumerate(meta_rows, 3):
        ws_meta.cell(r, 1, lbl).font = Font(bold=True, size=10)
        ws_meta.cell(r, 2, val).font = Font(size=10)
    ws_meta.column_dimensions["A"].width = 22
    ws_meta.column_dimensions["B"].width = 50

    # ── Sheet 2: PARTIES (editable) ──────────────────────────────────────────
    ws_p = wb.create_sheet("Parties")
    hdr(ws_p, 1, ["party_id", "Store Name", "Location / Area", "Type", "DL Number",
                   "GST Number", "Total Qty", "Total Amount ₹"])
    for r, p in enumerate(parties_raw, 2):
        cell(ws_p, r, 1, p["id"])                           # ID -- read only
        cell(ws_p, r, 2, p["name"] or "", editable=True)    # editable
        cell(ws_p, r, 3, p["area"] or "", editable=True)
        cell(ws_p, r, 4, p["type"] or "chemist", editable=True)
        cell(ws_p, r, 5, p["dl_number"] or "", editable=True)
        cell(ws_p, r, 6, p["gst_number"] or "", editable=True)
        cell(ws_p, r, 7, p["total_quantity"] or 0)
        cell(ws_p, r, 8, float(p["total_amount"] or 0))
    for col in ws_p.columns:
        ws_p.column_dimensions[get_column_letter(col[0].column)].width = 22

    # ── Sheet 3: ITEMS (editable) ────────────────────────────────────────────
    ws_i = wb.create_sheet("Items")
    hdr(ws_i, 1, ["item_id", "party_id", "Store Name", "Brand / Product",
                   "Company (MFG)", "Pack", "Batch No", "Expiry",
                   "Qty", "MRP ₹", "Rate ₹", "Disc%", "Amount ₹"])
    row = 2
    import json as _json
    for p in parties_raw:
        items = []
        try:
            raw = p.get("items_json") or []
            if isinstance(raw, str):
                raw = _json.loads(raw)
            for itm in raw:
                if isinstance(itm, str):
                    itm = _json.loads(itm)
                if itm:
                    items.append(itm)
        except Exception:
            pass
        for itm in items:
            cell(ws_i, row, 1,  itm.get("id", ""))
            cell(ws_i, row, 2,  p["id"])
            cell(ws_i, row, 3,  p["name"] or "")
            cell(ws_i, row, 4,  itm.get("brand", ""),  editable=True)
            cell(ws_i, row, 5,  itm.get("mfg", ""),    editable=True)
            cell(ws_i, row, 6,  itm.get("pack", ""),   editable=True)
            cell(ws_i, row, 7,  itm.get("batch_no", ""), editable=True)
            cell(ws_i, row, 8,  itm.get("expiry", ""), editable=True)
            cell(ws_i, row, 9,  itm.get("quantity", 0),  editable=True)
            cell(ws_i, row, 10, float(itm.get("mrp", 0) or 0), editable=True)
            cell(ws_i, row, 11, float(itm.get("unit_rate", 0) or 0), editable=True)
            cell(ws_i, row, 12, float(itm.get("discount_percent", 0) or 0), editable=True)
            cell(ws_i, row, 13, float(itm.get("final_amount", 0) or 0), editable=True)
            row += 1
    for col in ws_i.columns:
        ws_i.column_dimensions[get_column_letter(col[0].column)].width = 18

    # ── Sheet 4: INSTRUCTIONS ────────────────────────────────────────────────
    ws_inst = wb.create_sheet("Instructions")
    instructions = [
        ("HOW TO USE THIS SHEET", ""),
        ("", ""),
        ("1. Yellow cells", "Are editable -- correct any wrong values here"),
        ("2. ID columns (col A)", "Do NOT edit existing IDs. For NEW rows leave ID blank or put 0"),
        ("3. Add a new STORE", "New row in Parties sheet -- ID=0, fill Store Name and Area"),
        ("4. Add a new BRAND", "New row in Items sheet -- ID=0, party_id = the store ID from Parties col A"),
        ("5. Remove a row", "Do NOT delete rows -- set Qty=0 and Amount=0 instead"),
        ("6. Do NOT", "Rename sheet tabs or change column headers"),
        ("7. After editing", "Save the file and upload via Verification > Upload Corrections"),
        ("8. Verification ID", f"Must remain: {mv_id} (shown in Meta sheet)"),
    ]
    for r, (lbl, val) in enumerate(instructions, 1):
        ws_inst.cell(r, 1, lbl).font = Font(bold=True, size=11 if r==1 else 10)
        ws_inst.cell(r, 2, val).font = Font(size=10)
    ws_inst.column_dimensions["A"].width = 22
    ws_inst.column_dimensions["B"].width = 55

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f"verify_{mv['company_code']}_{mv_id}_{datetime.now().strftime('%Y%m%d')}.xlsx"
    return send_file(buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True, download_name=fname)


def notify_registration(reg):
    """Push notification to company_admin when new registration arrives."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id FROM users WHERE company_id=%s AND role='company_admin' AND status='active' LIMIT 1",
              (reg["company_id"],))
    admin = c.fetchone()
    conn.close()
    if admin:
        push_notification(
            reg["company_id"], admin[0], "company_admin", "new_registration",
            "New Registration Request",
            f"{reg['full_name']} ({reg.get('role','mr').upper()}) has applied for access. Mobile: {reg['mobile']}",
            ref_id=reg["id"], ref_type="registration"
        )
