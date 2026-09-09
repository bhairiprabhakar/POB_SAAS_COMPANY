"""
COMPANY / PUBLIC PORTAL ROUTES
Ported from the original Flask app.py -- business logic and SQL are
unchanged; only the routing layer (Flask -> FastAPI-compat) differs.
See app/compat.py for how `request`/`session`/`render_template`/etc. work.
"""
import os
import csv
import json
import time
import base64
import shutil
import secrets
import traceback
from io import BytesIO, StringIO
from datetime import datetime

import openpyxl

from ..compat import (
    FlaskCompatRouter, request, session, render_template, redirect, url_for,
    flash, jsonify, send_file, send_from_directory,
)
from ..database import get_db, fetchone_dict, fetchall_dict
from ..security import hash_pw, verify_pw, _looks_like_legacy_sha256
from ..config import UPLOAD_FOLDER, SESSION_TIMEOUT, SESSION_WARNING_AT
from ..helpers import (
    ROLES, ROLE_LABELS, ROLE_RANK, _slugify, get_upload_path,
    generate_company_code, make_company_code, generate_reg_token,
    get_company_credits, consume_credit, log_activity, get_user, get_company,
    get_accessible_ids, require_parent_for_role, classify_period,
    _parse_stmt_date, check_period_duplicate, get_current_quarter,
    save_extraction, get_sa_vcounts, push_notification, get_agent_company_ids,
    notify_registration,
)
from ..auth import login_required, admin_required, superadmin_required, agent_required
from ..extraction import call_ocr_extraction, _compute_file_hash, _compute_content_fingerprint, normalize_image_orientation
from ..ingestion import download_remote_document, RemoteFetchError
from ..security_middleware import account_lockout
from ..worker_pool import OCR_EXECUTOR
from saas.upload_validation import UploadValidationError, validate_upload

router = FlaskCompatRouter()

@router.route("/uploads/<path:filename>")
def serve_upload(filename):
    """
    Serve uploaded files. Handles all stored_filename formats:
      New:    "1/3/20260505_163227.pdf"  → uploads/1/3/20260505_163227.pdf
      Legacy: "1_3_20260505_163227.pdf"  → uploads/1_3_20260505_163227.pdf
      Logos:  "logos/logo_1.png"         → uploads/logos/logo_1.png

    SECURITY: this route used to have NO access control at all -- any file
    path (guessed, enumerated, or leaked via a shared link) was servable to
    anyone, logged in or not. These documents contain GST numbers, stockist
    identities, and sales figures, so this is now gated:
      1. Must be authenticated as SOME identity (company user or superadmin/agent).
      2. A company user may only fetch files that belong to THEIR OWN company.
      3. Superadmin/agent sessions pass through -- they already have
         cross-company visibility everywhere else in this app.

    IMPORTANT: authorization runs AFTER the file is located on disk (using the
    same multi-format fallback resolution below), matched against the
    RESOLVED file's basename/stem -- not against the raw URL string before
    resolution. An earlier version checked authorization first against an
    exact string match, which broke legitimate access to any file needing
    fallback resolution (older naming formats, DB/disk path mismatches) --
    exactly the files this fallback logic exists for in the first place.
    """
    import glob as _glob

    company_uid = session.get("user_id")
    is_super    = session.get("super_admin_id") is not None
    if not company_uid and not is_super:
        return redirect(url_for("login"))

    u = None
    if company_uid and not is_super:
        u = get_user(company_uid)
        if not u or u.get("status") != "active":
            return redirect(url_for("login"))

    def _send(p):
        return send_from_directory(os.path.dirname(p), os.path.basename(p))

    def _authorize_and_send(resolved_path):
        """Runs once we've actually found the file on disk. Logos are always
        allowed for any logged-in identity; everything else requires the
        owning company (looked up by a robust stem match against the
        RESOLVED filename, not the raw URL) to match the requester's."""
        if filename.startswith("logos/") or "/logos/" in resolved_path.replace("\\", "/"):
            return _send(resolved_path)
        if is_super:
            return _send(resolved_path)
        resolved_basename = os.path.basename(resolved_path)
        stem = os.path.splitext(resolved_basename)[0]
        conn_auth = get_db(); c_auth = conn_auth.cursor()
        c_auth.execute("""SELECT company_id FROM uploads
                          WHERE stored_filename LIKE %s OR original_filename LIKE %s
                          OR stored_filename = %s OR original_filename = %s
                          LIMIT 1""",
                       (f"%{stem}%", f"%{stem}%", resolved_basename, resolved_basename))
        owner_row = c_auth.fetchone()
        conn_auth.close()
        # Fail CLOSED: if we can't find who owns this file, don't guess --
        # deny rather than risk serving another company's document.
        if not owner_row or (u and owner_row[0] != u["company_id"]):
            from fastapi import HTTPException
            raise HTTPException(status_code=403, detail="Access denied")
        return _send(resolved_path)

    basename = os.path.basename(filename)

    # 1. Exact path (new subdirectory + logos)
    p = os.path.join(UPLOAD_FOLDER, filename)
    if os.path.exists(p): return _authorize_and_send(p)

    # 2. Basename flat (legacy files still on disk)
    p = os.path.join(UPLOAD_FOLDER, basename)
    if os.path.exists(p): return _authorize_and_send(p)

    # 3. DB/disk mismatch: new path in DB but file still in old location
    parts = filename.split("/")
    if len(parts) == 3:
        co_part, div_part, fname_part = parts
        # Try old flat format: {cid}_{uid}_{fname}
        p = os.path.join(UPLOAD_FOLDER, f"{co_part}_{div_part}_{fname_part}")
        if os.path.exists(p): return _authorize_and_send(p)
        # Try numeric company/user subdirs from first migration
        try:
            conn = get_db(); c = conn.cursor()
            c.execute("SELECT id FROM companies WHERE name=%s OR name=%s OR code=%s LIMIT 1",
                      (co_part.replace("_"," "), co_part, co_part))
            co_row = c.fetchone()
            if co_row:
                # Search across all user subfolders for that company
                import glob as _g
                matches = _g.glob(os.path.join(UPLOAD_FOLDER, str(co_row[0]), "*", fname_part))
                if matches: conn.close(); return _authorize_and_send(matches[0])
                # Also try bare filename in old flat folder
                p = os.path.join(UPLOAD_FOLDER, fname_part)
                if os.path.exists(p): conn.close(); return _authorize_and_send(p)
            conn.close()
        except Exception:
            pass

    # 4. DB lookup by stored_filename / original_filename
    try:
        conn = get_db(); c = conn.cursor()
        c.execute("""SELECT stored_filename FROM uploads
                     WHERE stored_filename=%s OR stored_filename=%s
                     OR original_filename=%s LIMIT 1""",
                  (filename, basename, basename))
        row = c.fetchone(); conn.close()
        if row and row[0]:
            for candidate in [os.path.join(UPLOAD_FOLDER, row[0]),
                               os.path.join(UPLOAD_FOLDER, os.path.basename(row[0]))]:
                if os.path.exists(candidate): return _authorize_and_send(candidate)
    except Exception:
        pass

    # 5. Glob fallback — search all subfolders by timestamp stem
    stem = os.path.splitext(basename)[0]
    matches = _glob.glob(os.path.join(UPLOAD_FOLDER, "**", f"*{stem}*"), recursive=True)
    if matches: return _authorize_and_send(matches[0])

    return f"File not found: {filename}", 404


@router.route("/")
def index():
    return redirect(url_for("dashboard") if "user_id" in session else url_for("login"))


@router.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        co_code = request.form.get("company_code", "").strip().upper()
        cred    = request.form.get("username", "").strip()
        pw      = request.form.get("password", "").strip()
        if not co_code:
            flash("Enter your Company Code")
            return render_template("index.html", page="login")
        if not cred or not pw:
            flash("Username and password are required")
            return render_template("index.html", page="login")

        lockout_key = f"{co_code}:{cred.lower()}"
        locked_secs = account_lockout.is_locked(lockout_key)
        if locked_secs:
            flash(f"Too many failed attempts for this account. Try again in {locked_secs // 60 + 1} minute(s).")
            return render_template("index.html", page="login")

        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM companies WHERE UPPER(code)=%s AND status='active'", (co_code,))
        co = fetchone_dict(c)
        if not co:
            conn.close()
            account_lockout.record_failure(lockout_key)
            flash(f"Company code '{co_code}' not found or inactive")
            return render_template("index.html", page="login")
        cid = co["id"]
        c.execute("""SELECT u.*, co.name as company_name, co.code as company_code,
                   co.status as company_status, co.logo_url as company_logo,
                   d.name as division_name
            FROM users u
            JOIN companies co ON u.company_id=co.id
            LEFT JOIN divisions d ON u.division_id=d.id
            WHERE u.company_id=%s AND u.status='active'
            AND (u.username=%s OR u.mobile=%s OR u.employee_id=%s)""",
            (cid, cred, cred, cred))
        user = fetchone_dict(c)
        if user and not verify_pw(user.get("password"), pw):
            user = None
        elif user and _looks_like_legacy_sha256(user.get("password")):
            # Opportunistically upgrade to a salted hash now that we know the password
            c.execute("UPDATE users SET password=%s WHERE id=%s", (hash_pw(pw), user["id"]))
            conn.commit()
        conn.close()
        if user:
            account_lockout.record_success(lockout_key)
            session["user_id"]    = user["id"]
            session["company_id"] = user["company_id"]
            conn2 = get_db()
            c2 = conn2.cursor()
            c2.execute("UPDATE users SET last_login=%s WHERE id=%s",
                       (datetime.now().isoformat(), user["id"]))
            conn2.commit()
            conn2.close()
            log_activity("login", "user", user["id"], f"Login via code {co_code}")
            return redirect(url_for("dashboard"))
        account_lockout.record_failure(lockout_key)
        flash("Invalid credentials. Try username, mobile number, or employee ID.")
    return render_template("index.html", page="login")


@router.route("/logout")
def logout():
    session.pop("user_id", None)
    session.pop("company_id", None)
    return redirect(url_for("login"))


@router.route("/api/company-by-code")
def api_company_by_code():
    code = request.args.get("code", "").strip().upper()
    if not code:
        return jsonify({"error": "No code"}), 400
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, name, code FROM companies WHERE UPPER(code)=%s AND status='active'", (code,))
    co = fetchone_dict(c)
    if not co:
        conn.close()
        return jsonify({"error": f"Company code '{code}' not found"}), 404
    cid = co["id"]
    c.execute("SELECT id, name FROM divisions WHERE company_id=%s AND status='active' ORDER BY name", (cid,))
    divs = fetchall_dict(c)
    # Distinct geo values from existing users (for smart dropdowns in registration)
    c.execute("SELECT DISTINCT region FROM users WHERE company_id=%s AND region IS NOT NULL AND region!='' ORDER BY region", (cid,))
    regions = [r[0] for r in c.fetchall()]
    c.execute("SELECT DISTINCT area FROM users WHERE company_id=%s AND area IS NOT NULL AND area!='' ORDER BY area", (cid,))
    areas = [r[0] for r in c.fetchall()]
    c.execute("SELECT DISTINCT territory FROM users WHERE company_id=%s AND territory IS NOT NULL AND territory!='' ORDER BY territory", (cid,))
    territories = [r[0] for r in c.fetchall()]
    conn.close()
    return jsonify({"id": cid, "name": co["name"], "code": co["code"],
                    "divisions": [{"id": d["id"], "name": d["name"]} for d in divs],
                    "regions": regions, "areas": areas, "territories": territories})


@router.route("/api/reg-managers")
def api_reg_managers():
    """Eligible reporting managers for a given role+company — used in registration dropdown."""
    company_id = request.args.get("company_id","").strip()
    role       = request.args.get("role","mr").strip()
    if not company_id:
        return jsonify({"managers":[]}), 400
    role_rank  = {"mr":1,"asm":2,"rsm":3,"zsm":4,"nsm":5,"company_admin":6}
    my_rank    = role_rank.get(role, 1)
    eligible   = [r for r,rk in role_rank.items() if rk > my_rank]
    if not eligible:
        eligible = ["company_admin"]
    ep = ",".join(["%s"]*len(eligible))
    conn = get_db(); c = conn.cursor()
    c.execute(f"""SELECT id,full_name,employee_id,role,area
        FROM users WHERE company_id=%s AND status='active' AND role IN ({ep})
        ORDER BY CASE role WHEN 'company_admin' THEN 1 WHEN 'nsm' THEN 2 WHEN 'zsm' THEN 3
                           WHEN 'rsm' THEN 4 ELSE 5 END, full_name""",
        [company_id]+eligible)
    managers = fetchall_dict(c)
    conn.close()
    return jsonify({"managers":[
        {"id":m["id"],"full_name":m["full_name"],
         "employee_id":m["employee_id"] or "",
         "role":m["role"],"area":m["area"] or ""}
        for m in managers]})


@router.route("/api/check-emp-id")
def api_check_emp_id():
    """Check if an employee ID already exists in the company (registration validation)."""
    company_id = request.args.get("company_id","").strip()
    emp_id     = request.args.get("emp_id","").strip()
    if not company_id or not emp_id:
        return jsonify({"exists":False})
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT id FROM users WHERE company_id=%s AND UPPER(employee_id)=UPPER(%s) AND status='active'",
              (company_id, emp_id))
    exists = bool(c.fetchone())
    if not exists:
        c.execute("SELECT id FROM registrations WHERE company_id=%s AND UPPER(employee_id)=UPPER(%s) AND status='pending'",
                  (company_id, emp_id))
        exists = bool(c.fetchone())
    conn.close()
    return jsonify({"exists":exists})


@router.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        co_code = request.form.get("company_code", "").strip().upper()
        fn      = request.form.get("full_name", "").strip()
        mob     = request.form.get("mobile", "").strip().replace(" ", "").replace("-", "")
        emp     = request.form.get("employee_id", "").strip()
        role    = request.form.get("role", "mr").strip()
        div_id  = request.form.get("division_id", "").strip() or None

        mgr   = request.form.get("manager_emp_id","").strip()  # emp_id of reporting manager
        email = request.form.get("email","").strip()
        # Geo: prefer dropdown, fall back to custom typed value
        reg   = (request.form.get("region","").strip()    or request.form.get("region_custom","").strip())
        area  = (request.form.get("area","").strip()      or request.form.get("area_custom","").strip())
        ter   = (request.form.get("territory","").strip() or request.form.get("territory_custom","").strip())
        pw    = request.form.get("password","").strip()
        cfm   = request.form.get("confirm_password","").strip()
        # Username = employee_id (strict) if given, else mobile number
        un    = emp if emp else mob
        errs    = []
        conn = get_db()
        c = conn.cursor()
        co = None
        if not co_code:
            errs.append("Company Code is required")
        else:
            c.execute("SELECT id FROM companies WHERE UPPER(code)=%s AND status='active'", (co_code,))
            co = fetchone_dict(c)
            if not co:
                errs.append(f"Company code '{co_code}' not found or inactive")
        if not fn:   errs.append("Full name is required")
        if not mob:  errs.append("Mobile number is required")
        elif not mob.isdigit(): errs.append("Mobile must contain digits only")
        elif len(mob) != 10:   errs.append(f"Mobile must be exactly 10 digits")
        if not pw:       errs.append("Password is required")
        elif pw != cfm:  errs.append("Passwords do not match")
        elif len(pw) < 8: errs.append("Password must be at least 8 characters")
        if errs:
            for e in errs:
                flash(e)
            conn.close()
            return render_template("index.html", page="register")
        cid = co["id"]
        # Mobile already active
        c.execute("SELECT id FROM users WHERE company_id=%s AND mobile=%s AND status='active'", (cid, mob))
        if c.fetchone():
            flash("Mobile already registered and active. Contact your admin.")
            conn.close()
            return render_template("index.html", page="register")
        # Employee ID uniqueness (strict — same company, active or pending)
        if emp:
            c.execute("SELECT id FROM users WHERE company_id=%s AND UPPER(employee_id)=UPPER(%s) AND status='active'", (cid, emp))
            if c.fetchone():
                flash(f"Employee ID '{emp}' is already registered in this company. Contact your admin.")
                conn.close()
                return render_template("index.html", page="register")
            c.execute("SELECT id FROM registrations WHERE company_id=%s AND UPPER(employee_id)=UPPER(%s) AND status='pending'", (cid, emp))
            if c.fetchone():
                flash(f"Employee ID '{emp}' already has a pending registration.")
                conn.close()
                return render_template("index.html", page="register")
        # Pending mobile duplicate
        c.execute("SELECT id FROM registrations WHERE company_id=%s AND mobile=%s AND status='pending'", (cid, mob))
        if c.fetchone():
            flash("Registration already pending approval.")
            conn.close()
            return render_template("index.html", page="register")
        try:
            c.execute("""INSERT INTO registrations
                (company_id,full_name,proposed_username,password,role,division_id,
                 employee_id,mobile,region,area,territory,manager_name,email)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (cid, fn, un, hash_pw(pw), role, div_id, emp, mob, reg, area, ter, mgr, email))
            conn.commit()
            # Notify company admin of new registration
            try:
                c.execute("SELECT id FROM registrations WHERE company_id=%s AND mobile=%s ORDER BY id DESC LIMIT 1", (cid, mob))
                reg_row = c.fetchone()
                if reg_row:
                    notify_registration({"id": reg_row[0], "company_id": cid, "full_name": fn, "role": role, "mobile": mob})
            except Exception:
                pass
            conn.close()
            return render_template("index.html", page="reg_success")
        except Exception as ex:
            conn.rollback()
            flash(f"Error: {ex}")
            conn.close()
            return render_template("index.html", page="register")
    return render_template("index.html", page="register")


@router.route("/dashboard")
@login_required
def dashboard():
    user = get_user(session["user_id"])
    accessible = get_accessible_ids(user)
    # NSM fallback: if no subordinates configured, show all users in their division
    if user["role"] == "nsm" and len(accessible) <= 1:
        conn_fb = get_db(); c_fb = conn_fb.cursor()
        c_fb.execute("SELECT id FROM users WHERE company_id=%s AND division_id=%s AND status='active'",
                     (user["company_id"], user.get("division_id")))
        div_ids = [r[0] for r in c_fb.fetchall()]
        conn_fb.close()
        if div_ids: accessible = div_ids
    if not accessible: accessible = [user["id"]]
    date_to      = request.args.get("date_to", "")
    status_filter = request.args.get("status_filter", "all")  # all | done | rejected | error
    conn = get_db()
    c = conn.cursor()

    ph = ",".join(["%s"] * len(accessible))
    df_clause = ""
    df_params = list(accessible)
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to') 
    if date_from:
        df_clause += " AND DATE(u.upload_date)>=%s"
        df_params.append(date_from)
    if date_to:
        df_clause += " AND DATE(u.upload_date)<=%s"
        df_params.append(date_to)
    if status_filter and status_filter not in ("all", ""):
        if status_filter == "verified_stage":
            df_clause += " AND u.verification_status='verified'"
        elif status_filter == "pending_verif":
            df_clause += " AND u.verification_status='pending_verification'"
        else:
            df_clause += " AND u.status=%s"
            df_params.append(status_filter)

    c.execute(f"""
        SELECT u.*,us.full_name as uploader_name,us.area,us.role as uploader_role,
               e.stockist_name,e.total_amount,e.total_quantity,e.invoice_net,
               e.bill_date,e.bill_number,e.statement_from_date,e.statement_to_date,
               (SELECT COUNT(*) FROM parties p WHERE p.extraction_id=e.id) as party_count,
               rej.full_name as rejected_by_name,
               u.verification_status,
               mv.status as mv_status, mv.verified_at
        FROM uploads u JOIN users us ON u.user_id=us.id
        LEFT JOIN extractions e ON e.upload_id=u.id
        LEFT JOIN users rej ON u.rejected_by=rej.id
        LEFT JOIN manual_verifications mv ON mv.upload_id=u.id
        WHERE u.user_id IN ({ph}) {df_clause} ORDER BY u.upload_date DESC LIMIT 200
    """, df_params)
    uploads = fetchall_dict(c)

    # Count by status + verification_status for filter badges
    c.execute(f"""
        SELECT u.status, u.verification_status, COUNT(*) as cnt FROM uploads u
        WHERE u.user_id IN ({ph}) GROUP BY u.status, u.verification_status
    """, accessible)
    status_counts = {}
    verif_counts  = {"ocr_done": 0, "pending_verification": 0, "verified": 0}
    for r in fetchall_dict(c):
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + r["cnt"]
        vs = r["verification_status"] or "ocr_done"
        verif_counts[vs] = verif_counts.get(vs, 0) + r["cnt"]

    ph_a = ph

    c.execute(f"""
        SELECT
            COUNT(DISTINCT u.id) as total_bills,
            COUNT(DISTINCT u.id) FILTER (WHERE u.status='done') as ocr_done,
            COUNT(DISTINCT u.id) FILTER (WHERE u.verification_status='verified') as verified_count,
            COUNT(DISTINCT u.id) FILTER (WHERE u.verification_status='pending_verification') as pending_verif,
            -- Per-document net value (one extraction row per upload -- no party join fan-out)
            COALESCE(SUM(e.invoice_net) FILTER (WHERE u.status='done'), 0) as total_revenue,
            COALESCE(SUM(e.invoice_net) FILTER (WHERE u.verification_status='verified'), 0) as statement_value,
            COALESCE(SUM(e.invoice_net) FILTER (WHERE u.status='done' AND e.doc_type='INVOICE'), 0) as invoice_value,
            COALESCE(SUM(e.invoice_net) FILTER (WHERE u.verification_status='verified'), 0) as verified_revenue,
            COALESCE(SUM(e.invoice_net) FILTER (WHERE u.verification_status='verified' AND e.doc_type='STATEMENT'), 0) as verified_statement_value,
            COALESCE(SUM(e.invoice_net) FILTER (WHERE u.verification_status='verified' AND e.doc_type='INVOICE'), 0) as verified_invoice_value,
            COALESCE(SUM(e.total_quantity) FILTER (WHERE u.status='done'), 0) as total_units,
            COUNT(DISTINCT NULLIF(TRIM(e.stockist_name), '')) as agency_count,
            (SELECT COUNT(DISTINCT p2.name) FROM parties p2
             JOIN extractions e2 ON p2.extraction_id=e2.id
             JOIN uploads u2 ON e2.upload_id=u2.id WHERE u2.user_id IN ({ph_a})) as unique_parties
        FROM uploads u LEFT JOIN extractions e ON e.upload_id=u.id
        WHERE u.user_id IN ({ph_a})
    """, accessible + accessible)
    my_stats = fetchone_dict(c)

    # Team stats (only if user is a manager)
    team_stats = {}
    team_uploads = []
    if user["role"] in ("company_admin", "nsm", "zsm", "rsm", "asm"):
        # Get team member IDs (subordinates only, excluding self)
        if user["role"] == "company_admin":
            c.execute(f"SELECT id FROM users WHERE company_id=%s AND id!=%s", (user["company_id"], user["id"]))
        else:
            c.execute(f"""
                WITH RECURSIVE sub(id) AS (
                    SELECT id FROM users WHERE id=%s AND id!=%s
                    UNION ALL
                    SELECT u.id FROM users u JOIN sub s ON u.parent_id=s.id
                ) SELECT id FROM sub""", (user["id"], user["id"]))
        team_ids = [r[0] for r in c.fetchall()]
        
        if team_ids:
            team_ph = ",".join(["%s"] * len(team_ids))
            c.execute(f"""
                SELECT
                    COUNT(DISTINCT u.id) as total_bills,
                    COUNT(DISTINCT u.id) FILTER (WHERE u.status='done') as ocr_done,
                    COUNT(DISTINCT u.id) FILTER (WHERE u.verification_status='verified') as verified_count,
                    COUNT(DISTINCT u.id) FILTER (WHERE u.verification_status='pending_verification') as pending_verif,
                    COALESCE(SUM(e.invoice_net) FILTER (WHERE u.status='done'), 0) as total_revenue,
                    COALESCE(SUM(e.invoice_net) FILTER (WHERE u.verification_status='verified'), 0) as statement_value,
                    COALESCE(SUM(e.invoice_net) FILTER (WHERE u.status='done' AND e.doc_type='INVOICE'), 0) as invoice_value,
                    COALESCE(SUM(e.invoice_net) FILTER (WHERE u.verification_status='verified'), 0) as verified_revenue,
                    COALESCE(SUM(e.invoice_net) FILTER (WHERE u.verification_status='verified' AND e.doc_type='STATEMENT'), 0) as verified_statement_value,
                    COALESCE(SUM(e.invoice_net) FILTER (WHERE u.verification_status='verified' AND e.doc_type='INVOICE'), 0) as verified_invoice_value,
                    COALESCE(SUM(e.total_quantity) FILTER (WHERE u.status='done'), 0) as total_units
                FROM uploads u LEFT JOIN extractions e ON e.upload_id=u.id
                WHERE u.user_id IN ({team_ph})
            """, team_ids)
            team_stats = fetchone_dict(c) or {}
    
    stats = my_stats  # already computed over all accessible IDs (full hierarchy)
    # team_stats kept only for the team member table breakdown, NOT added to stats
    # (adding was causing double-count: accessible already includes all subordinates)

    c.execute(f"""
        SELECT p.type,COUNT(*) as cnt,COALESCE(SUM(p.total_amount),0) as amt
        FROM parties p JOIN extractions e ON p.extraction_id=e.id
        JOIN uploads u ON e.upload_id=u.id
        WHERE u.user_id IN ({ph_a}) GROUP BY p.type
    """, accessible)
    party_types = fetchall_dict(c)

    # Use accessible IDs for hierarchy-wide analytics
    ph_a = ",".join(["%s"] * len(accessible))

    c.execute(f"""
        SELECT i.brand, SUM(i.quantity) as qty, SUM(i.final_amount) as amt
        FROM items i JOIN parties p ON i.party_id=p.id
        JOIN extractions e ON p.extraction_id=e.id
        JOIN uploads u ON e.upload_id=u.id
        WHERE u.user_id IN ({ph_a}) AND i.brand != ''
        AND UPPER(TRIM(i.brand)) NOT IN ('CUMULATIVE SUMMARY','TOTAL','GRAND TOTAL','SUB TOTAL','SUBTOTAL')
        AND i.final_amount > 0 AND LENGTH(TRIM(i.brand)) > 2
        GROUP BY i.brand ORDER BY amt DESC LIMIT 10
    """, accessible)
    top_brands = fetchall_dict(c)

    # Top 10 Agency Names (stockist_name from extractions)
    c.execute(f"""
        SELECT e.stockist_name as name,
               COUNT(DISTINCT u.id) as uploads,
               COALESCE(SUM(e.invoice_net),0) as amt
        FROM extractions e JOIN uploads u ON e.upload_id=u.id
        WHERE u.user_id IN ({ph_a}) AND e.stockist_name != ''
        AND e.stockist_name IS NOT NULL
        GROUP BY e.stockist_name ORDER BY amt DESC LIMIT 10
    """, accessible)
    top_agencies = fetchall_dict(c)

    # Top 10 Party Names (store-level)
    c.execute(f"""
        SELECT p.name, SUM(p.total_amount) as amt, COUNT(DISTINCT u.id) as uploads
        FROM parties p JOIN extractions e ON p.extraction_id=e.id
        JOIN uploads u ON e.upload_id=u.id
        WHERE u.user_id IN ({ph_a}) AND p.name != '' AND p.total_amount > 0
        AND UPPER(TRIM(p.name)) NOT IN ('CUMULATIVE SUMMARY','TOTAL','GRAND TOTAL')
        AND LENGTH(TRIM(p.name)) > 2
        GROUP BY p.name ORDER BY amt DESC LIMIT 10
    """, accessible)
    top_parties = fetchall_dict(c)

    # Team: direct children + their upload stats (hierarchy-scoped)
    c.execute(f"""
        SELECT us.id, us.full_name, us.role, us.area, us.mobile, us.territory,
               d.name as div_name,
               COUNT(DISTINCT u.id) as upload_count,
               COALESCE(SUM(e.invoice_net),0) as revenue
        FROM users us LEFT JOIN divisions d ON us.division_id=d.id
        LEFT JOIN uploads u ON u.user_id=us.id
        LEFT JOIN extractions e ON e.upload_id=u.id
        WHERE us.id IN ({ph_a}) AND us.id != %s
        GROUP BY us.id, us.full_name, us.role, us.area, us.mobile, us.territory, d.name
        ORDER BY us.role, us.full_name
    """, accessible + [user["id"]])
    team = fetchall_dict(c)

    pending_count = 0
    if user["role"] in ("company_admin", "nsm"):
        c.execute("SELECT COUNT(*) as n FROM registrations WHERE company_id=%s AND status='pending'",
                  (user["company_id"],))
        pending_count = c.fetchone()[0]

    q_info = get_current_quarter()
    c.execute(f"""
        SELECT COUNT(DISTINCT e.id) as bills,
               COALESCE(SUM(e.invoice_net),0) as revenue,
               COALESCE(SUM(e.invoice_net) FILTER (WHERE u.verification_status='verified'),0) as statement_value,
               COALESCE(SUM(e.invoice_net) FILTER (WHERE e.doc_type='INVOICE'),0) as invoice_value,
               COALESCE(SUM(e.total_quantity),0) as units
        FROM uploads u LEFT JOIN extractions e ON e.upload_id=u.id
        WHERE u.user_id IN ({ph_a})
        AND (e.statement_from_date >= %s OR DATE(u.upload_date) >= %s)
        AND (e.statement_to_date <= %s OR DATE(u.upload_date) <= %s)
    """, accessible + [q_info["from"], q_info["from"], q_info["to"], q_info["to"]])
    q_stats = fetchone_dict(c)

    q_growth = None
    pq_revenue = 0
    try:
        from datetime import date as _date, timedelta as _td
        qs = _date.fromisoformat(q_info["from"])
        pm = qs.month - 3
        py = qs.year
        if pm <= 0:
            pm += 12
            py -= 1
        pq_from = _date(py, pm, 1).strftime("%Y-%m-%d")
        pq_to   = (qs - _td(days=1)).strftime("%Y-%m-%d")
        c.execute(f"""
            SELECT COALESCE(SUM(e.invoice_net),0) as revenue
            FROM uploads u LEFT JOIN extractions e ON e.upload_id=u.id
            WHERE u.user_id IN ({ph_a}) AND DATE(u.upload_date) >= %s AND DATE(u.upload_date) <= %s
        """, accessible + [pq_from, pq_to])
        pq_row = fetchone_dict(c)
        pq_revenue = pq_row["revenue"] if pq_row else 0
        if pq_revenue:
            q_growth = round(((q_stats["revenue"] - pq_revenue) / pq_revenue * 100), 1)
    except Exception:
        pass

    upload_gaps = []
    # Only immediate reporting managers (ASM and RSM) see their own MRs/ASMs not uploading.
    # Admin/NSM/ZSM are too far removed — noise without actionability.
    if user["role"] in ("asm", "rsm"):
        c.execute(f"""
            SELECT us.id, us.full_name, us.area, us.mobile,
                   (SELECT MAX(u.upload_date) FROM uploads u WHERE u.user_id=us.id) as last_upload,
                   (SELECT COUNT(*) FROM uploads u
                    WHERE u.user_id=us.id AND DATE(u.upload_date) >= %s) as q_uploads
            FROM users us
            WHERE us.parent_id=%s AND us.status='active' AND us.role='mr'
        """, [q_info["from"], user["id"]])
        for mr in fetchall_dict(c):
            if mr["q_uploads"] == 0:
                upload_gaps.append(mr)

    conn.close()
    creds = get_company_credits(user["company_id"])
    return render_template("index.html", page="dashboard",
        user=user, uploads=uploads, stats=stats, party_types=party_types,
        top_brands=top_brands, top_agencies=top_agencies, top_parties=top_parties,
        team=team, date_from=date_from, date_to=date_to,
        pending_count=pending_count, q_info=q_info, q_stats=q_stats,
        q_growth=q_growth, pq_revenue=pq_revenue, upload_gaps=upload_gaps, creds=creds,
        my_stats=my_stats, team_stats=team_stats,
        status_filter=status_filter, status_counts=status_counts, verif_counts=verif_counts)


@router.route("/documents")
@login_required
def documents():
    user          = get_user(session["user_id"])
    accessible    = get_accessible_ids(user)
    # For NSM: if only self is returned (no subordinates configured yet),
    # fall back to all users in their division
    if user["role"] == "nsm" and len(accessible) <= 1:
        conn_fb = get_db(); c_fb = conn_fb.cursor()
        c_fb.execute("""SELECT id FROM users WHERE company_id=%s AND division_id=%s AND status='active'""",
                     (user["company_id"], user.get("division_id")))
        div_ids = [r[0] for r in c_fb.fetchall()]
        conn_fb.close()
        if div_ids:
            accessible = div_ids
    if not accessible:
        accessible = [user["id"]]
    date_from     = request.args.get("date_from", "")
    date_to       = request.args.get("date_to", "")
    status_filter = request.args.get("status_filter", "all")

    conn = get_db()
    c    = conn.cursor()
    ph   = ",".join(["%s"] * len(accessible))

    clause = ""
    params = list(accessible)
    if date_from:
        clause += " AND DATE(u.upload_date)>=%s"; params.append(date_from)
    if date_to:
        clause += " AND DATE(u.upload_date)<=%s"; params.append(date_to)
    if status_filter and status_filter not in ("all", ""):
        if status_filter == "verified_stage":
            clause += " AND u.verification_status='verified'"
        elif status_filter == "pending_verif":
            clause += " AND u.verification_status='pending_verification'"
        else:
            clause += " AND u.status=%s"; params.append(status_filter)

    c.execute(f"""
        SELECT u.*,
               us.full_name  AS uploader_name,
               us.role       AS uploader_role,
               e.stockist_name, e.invoice_net,
               e.statement_from_date, e.statement_to_date,
               rej.full_name AS rejected_by_name,
               mv.verified_at
        FROM uploads u
        JOIN  users us  ON u.user_id  = us.id
        LEFT JOIN extractions        e   ON e.upload_id  = u.id
        LEFT JOIN users              rej ON u.rejected_by = rej.id
        LEFT JOIN manual_verifications mv ON mv.upload_id = u.id
        WHERE u.user_id IN ({ph}) {clause}
        ORDER BY u.upload_date DESC LIMIT 200
    """, params)
    uploads = fetchall_dict(c)

    c.execute(f"""
        SELECT u.status, u.verification_status, COUNT(*) AS cnt
        FROM uploads u WHERE u.user_id IN ({ph})
        GROUP BY u.status, u.verification_status
    """, accessible)
    status_counts = {}
    for r in fetchall_dict(c):
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + r["cnt"]
        if r.get("verification_status") == "verified":
            status_counts["verified_stage"] = status_counts.get("verified_stage", 0) + r["cnt"]
        if r.get("verification_status") == "pending_verification":
            status_counts["pending_verif"] = status_counts.get("pending_verif", 0) + r["cnt"]

    pending_count = 0
    if user["role"] in ("company_admin", "nsm"):
        c.execute("SELECT COUNT(*) FROM registrations WHERE company_id=%s AND status='pending'",
                  (user["company_id"],))
        pending_count = c.fetchone()[0]

    conn.close()
    creds = get_company_credits(user["company_id"])
    return render_template("index.html", page="documents",
        user=user, uploads=uploads,
        date_from=date_from, date_to=date_to,
        status_filter=status_filter, status_counts=status_counts,
        pending_count=pending_count, creds=creds)


class _TTLDict:
    """
    Drop-in replacement for the plain `_upload_progress = {}` dict that used
    to grow forever -- at 100k uploads/day, every single one left a stale
    entry behind permanently for the life of the process, which is a
    guaranteed slow memory leak that eventually crashes the process. Entries
    older than TTL_SECONDS are swept out periodically (piggybacked on writes
    rather than a separate thread, since writes already happen constantly
    during real usage).
    """
    TTL_SECONDS = 30 * 60   # nobody polls progress for an upload from 30+ min ago
    SWEEP_EVERY = 200        # amortize the cleanup scan instead of doing it on every write

    def __init__(self):
        self._data = {}       # key -> (value, set_at_timestamp)
        self._writes = 0

    def __setitem__(self, key, value):
        self._data[key] = (value, time.time())
        self._writes += 1
        if self._writes % self.SWEEP_EVERY == 0:
            self._sweep()

    def get(self, key, default=None):
        entry = self._data.get(key)
        if entry is None:
            return default
        return entry[0]

    def _sweep(self):
        cutoff = time.time() - self.TTL_SECONDS
        stale = [k for k, (_, ts) in self._data.items() if ts < cutoff]
        for k in stale:
            del self._data[k]


_upload_progress = _TTLDict()


@router.route("/api/session/status")
def api_session_status():
    """
    Read-only session-timeout check for the frontend's warning countdown.
    Deliberately does NOT use login_required -- merely polling this endpoint
    must not itself count as "activity" and reset the inactivity clock,
    or the timeout could never actually fire while the tab is open.
    """
    uid = session.get("user_id") or session.get("super_admin_id")
    if not uid:
        return jsonify({"authenticated": False})
    last = session.get("last_activity")
    warn_at_remaining = max(0, SESSION_TIMEOUT - SESSION_WARNING_AT)
    if last is None:
        return jsonify({"authenticated": True, "remaining": SESSION_TIMEOUT,
                         "timeout": SESSION_TIMEOUT, "warn_at_remaining": warn_at_remaining})
    remaining = max(0, int(SESSION_TIMEOUT - (time.time() - last)))
    return jsonify({"authenticated": True, "remaining": remaining,
                     "timeout": SESSION_TIMEOUT, "warn_at_remaining": warn_at_remaining})


@router.route("/api/session/keepalive", methods=["POST"])
def api_session_keepalive():
    """
    Explicit "Stay logged in" action from the warning modal -- this DOES
    refresh last_activity, extending the session by a full SESSION_TIMEOUT
    from now. Works for both company-user and superadmin/agent sessions.
    """
    uid = session.get("user_id") or session.get("super_admin_id")
    if not uid:
        return jsonify({"authenticated": False}), 401
    session["last_activity"] = time.time()
    return jsonify({"authenticated": True, "remaining": SESSION_TIMEOUT})


@router.route("/api/upload-progress/<int:up_id>")
@login_required
def upload_progress(up_id):
    prog = _upload_progress.get(up_id, {"pct": 0, "stage": "Waiting"})
    return jsonify(prog)


@router.route("/api/period-check")
@login_required
def api_period_check():
    user = get_user(session["user_id"])
    cid  = user["company_id"]
    q    = get_current_quarter()
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT COUNT(*) as n FROM uploads u
        JOIN extractions e ON e.upload_id=u.id
        WHERE u.company_id=%s AND u.user_id=%s
        AND e.statement_from_date >= %s AND e.statement_to_date <= %s
        AND u.status='done'
    """, (cid, user["id"], q["from"], q["to"]))
    qt_uploads = c.fetchone()[0]
    conn.close()
    return jsonify({
        "current_quarter": q,
        "uploads_this_quarter": qt_uploads,
        "expected_period_type": "quarterly",
        "reminder": f"Upload statements for {q['label']}: {q['from_display']} to {q['to_display']}"
                     if qt_uploads == 0 else None
    })


@router.route("/upload", methods=["POST"])
@login_required
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400
    file = request.files["file"]
    ext  = os.path.splitext(file.filename)[1].lower()
    if ext not in {".pdf", ".jpg", ".jpeg", ".png", ".xlsx", ".xls", ".csv"}:
        return jsonify({"error": f"Unsupported format: {ext}"}), 400

    # The extension above is only a UX hint -- validate actual file content
    # (magic bytes, and a real decode for images/spreadsheets/PDFs) before
    # trusting it as that type. A renamed arbitrary file should not reach
    # storage.save()/downstream OCR just because it's named "invoice.pdf".
    _kind_by_ext = {
        ".pdf": {"pdf"}, ".jpg": {"jpeg"}, ".jpeg": {"jpeg"}, ".png": {"png"},
        ".xlsx": {"office_zip"}, ".xls": {"office_legacy"}, ".csv": {"csv"},
    }
    file_bytes = file.read()
    file.seek(0)
    try:
        validate_upload(file_bytes, filename=file.filename or "",
                        allowed_kinds=_kind_by_ext[ext], max_size=50 * 1024 * 1024)
    except UploadValidationError as exc:
        return jsonify({"error": f"Invalid file: {exc}"}), 400

    uid_s = session["user_id"]
    cid   = session.get("company_id", 0)
    creds = get_company_credits(cid)
    if creds["remaining"] <= 0:
        return jsonify({
            "error": "❌ No credits remaining. Please request more credits from your admin.",
            "credits_exhausted": True,
            "credits": creds
        }), 402

    force_proceed = request.form.get("force_proceed", "0") == "1"
    manual_from   = request.form.get("manual_from", "").strip()
    manual_to     = request.form.get("manual_to", "").strip()

    # Fetch company name + division name for human-readable folder structure
    _u = get_user(uid_s)
    co_name  = (_u.get("company_name")  or "") if _u else ""
    div_name = (_u.get("division_name") or "") if _u else ""

    ts             = datetime.now().strftime("%Y%m%d_%H%M%S")
    bare           = f"{ts}{ext}"
    stored, fpath  = get_upload_path(cid, uid_s, bare,
                                     company_name=co_name,
                                     division_name=div_name)
    file.save(fpath)
    normalize_image_orientation(fpath)

    early, up_id, mime = _create_upload_record(fpath, stored, file.filename, ext, uid_s, cid, force_proceed)
    if early is not None:
        payload, status = early
        return jsonify(payload), status

    _upload_progress[up_id] = {"pct": 5, "stage": "Queued for extraction...", "done": False}
    OCR_EXECUTOR.submit(_run_extraction_pipeline, fpath, file.filename, mime, up_id, uid_s, cid,
                        force_proceed, manual_from, manual_to)
    return jsonify({"success": True, "upload_id": up_id, "processing": True})


@router.route("/upload-from-url", methods=["POST"])
@login_required
def upload_from_url():
    """
    Import a document from a remote link instead of a local file picker --
    S3/EC2-hosted URLs, Google Drive share links, or any direct web link to
    a PDF/JPG/PNG. Shares all duplicate-detection / OCR / credit / auto-assign
    logic with the regular /upload route via _create_upload_record() and
    _run_extraction_pipeline() below -- the only difference is *how* the
    file lands on local disk first.
    """
    source_url = request.form.get("source_url", "").strip()
    if not source_url:
        return jsonify({"error": "Please provide a document URL"}), 400

    uid_s = session["user_id"]
    cid   = session.get("company_id", 0)
    creds = get_company_credits(cid)
    if creds["remaining"] <= 0:
        return jsonify({
            "error": "❌ No credits remaining. Please request more credits from your admin.",
            "credits_exhausted": True,
            "credits": creds
        }), 402

    force_proceed = request.form.get("force_proceed", "0") == "1"
    manual_from   = request.form.get("manual_from", "").strip()
    manual_to     = request.form.get("manual_to", "").strip()

    try:
        tmp_path, ext, original_name = download_remote_document(source_url)
    except RemoteFetchError as e:
        return jsonify({"error": str(e)}), 400

    _u = get_user(uid_s)
    co_name  = (_u.get("company_name")  or "") if _u else ""
    div_name = (_u.get("division_name") or "") if _u else ""

    ts            = datetime.now().strftime("%Y%m%d_%H%M%S")
    bare          = f"{ts}{ext}"
    stored, fpath = get_upload_path(cid, uid_s, bare,
                                    company_name=co_name,
                                    division_name=div_name)
    shutil.move(tmp_path, fpath)
    normalize_image_orientation(fpath)

    early, up_id, mime = _create_upload_record(fpath, stored, original_name, ext, uid_s, cid, force_proceed)
    if early is not None:
        payload, status = early
        return jsonify(payload), status

    _upload_progress[up_id] = {"pct": 5, "stage": "Queued for extraction...", "done": False}
    OCR_EXECUTOR.submit(_run_extraction_pipeline, fpath, original_name, mime, up_id, uid_s, cid,
                        force_proceed, manual_from, manual_to)
    return jsonify({"success": True, "upload_id": up_id, "processing": True})


def _create_upload_record(fpath, stored, original_filename, ext, uid_s, cid, force_proceed):
    """
    Fast, synchronous part of an upload -- runs inline in the request.
    Does the cheap file-hash duplicate check and creates the `uploads` row,
    then hands off. Returns (early_error_response_or_None, up_id_or_None, mime).
    """
    file_hash = _compute_file_hash(fpath)
    if not force_proceed:
        conn0 = get_db()
        c0 = conn0.cursor()
        # Level-1: exact same file bytes within SAME COMPANY
        # Different companies can upload the same PDF (e.g. both buy from same stockist)
        c0.execute("""SELECT u.id, u.upload_date, us.full_name as uploader, u.original_filename
            FROM uploads u JOIN users us ON u.user_id=us.id
            WHERE u.company_id=%s AND u.file_hash=%s AND u.status='done'""",
            (cid, file_hash))
        fh_dup = fetchone_dict(c0)
        conn0.close()
        if fh_dup:
            os.remove(fpath)
            dup_payload = {
                "error": f"❌ Duplicate within your company! This exact file was already uploaded on "
                         f"{str(fh_dup['upload_date'])[:10]} by {fh_dup['uploader']} "
                         f"(original: {fh_dup['original_filename']}). "
                         f"Each document should be uploaded once per company.",
                "duplicate_level": 1,
                "existing_upload_id": fh_dup["id"]
            }
            return (dup_payload, 409), None, None

    mime = {
        ".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xls": "application/vnd.ms-excel", ".csv": "text/csv"
    }.get(ext, "application/octet-stream")

    u = get_user(uid_s)
    div_id = u["division_id"] if u else None
    conn = get_db()
    c2 = conn.cursor()
    c2.execute("""INSERT INTO uploads
        (company_id,division_id,user_id,original_filename,stored_filename,file_type,status,file_hash)
        VALUES (%s,%s,%s,%s,%s,%s,'processing',%s) RETURNING id""",
        (cid, div_id, uid_s, original_filename, stored, mime, file_hash))
    up_id = c2.fetchone()[0]
    conn.commit()
    conn.close()
    return None, up_id, mime


def _run_extraction_pipeline(fpath, original_filename, mime, up_id, uid_s, cid,
                              force_proceed, manual_from, manual_to):
    """
    Runs on app.worker_pool.OCR_EXECUTOR -- NOT on the request thread. There is
    no HTTP response to return to by the time this runs (the client already
    got back {"processing": true} from /upload), so every outcome is instead
    written into _upload_progress[up_id] for the frontend to pick up via
    /api/upload-progress/<up_id> polling. This is the exact same
    duplicate-detection / OCR / credit / auto-assign logic that used to run
    inline in the request -- only *where* it runs changed.
    """
    _upload_progress[up_id] = {"pct": 15, "stage": "Extracting text from document...", "done": False}
    try:
        try:
            data = call_ocr_extraction(fpath, mime, original_filename, company_id=cid, upload_id=up_id)
        except Exception as ocr_err:
            traceback.print_exc()
            conn_e = get_db(); ce = conn_e.cursor()
            ce.execute("UPDATE uploads SET status='error',error_msg=%s WHERE id=%s", (f"OCR extraction failed: {ocr_err}", up_id))
            conn_e.commit(); conn_e.close()
            _upload_progress[up_id] = {"pct": 100, "done": True, "error": f"OCR extraction failed: {str(ocr_err)}", "ocr_error": True}
            return

        n_parties = len(data.get("parties", []))
        _upload_progress[up_id] = {"pct": 65, "stage": f"Saving {n_parties} parties...", "done": False}

        # Apply manual date override
        if manual_from and manual_to:
            try:
                from datetime import datetime as _dtt
                mf = _dtt.strptime(manual_from, "%Y-%m-%d").strftime("%d/%m/%Y")
                mt = _dtt.strptime(manual_to,   "%Y-%m-%d").strftime("%d/%m/%Y")
                data["statement_from_date"] = mf
                data["statement_to_date"]   = mt
            except Exception:
                pass

        # ── LEVEL 2: Content fingerprint check (renamed file, same content) ──────
        content_fp = _compute_content_fingerprint(data)
        _sn_for_fp = (data.get("stockist_name","") or "").strip()
        if not force_proceed and _sn_for_fp:  # skip level-2 if OCR couldn't detect stockist name
            conn_fp = get_db()
            cfp = conn_fp.cursor()
            # Level-2: same stockist + same period, SAME COMPANY only
            cfp.execute("""SELECT u.id, u.upload_date, us.full_name as uploader, u.original_filename,
                          e.stockist_name, e.statement_from_date, e.statement_to_date
                FROM uploads u JOIN users us ON u.user_id=us.id
                JOIN extractions e ON e.upload_id=u.id
                WHERE u.company_id=%s AND u.content_fingerprint=%s AND u.status='done'""",
                (cid, content_fp))
            fp_dup = fetchone_dict(cfp)
            conn_fp.close()
            if fp_dup and fp_dup.get("stockist_name","").strip():
                conn_rm = get_db()
                cr = conn_rm.cursor()
                cr.execute("UPDATE uploads SET status='rejected',error_msg=%s WHERE id=%s",
                    (f"Content fingerprint matched upload #{fp_dup['id']}", up_id))
                conn_rm.commit()
                conn_rm.close()
                _upload_progress[up_id] = {
                    "pct": 100, "done": True,
                    "error": f"❌ Smart duplicate detected! Same stockist ({fp_dup['stockist_name']}) "
                             f"with identical statement period ({fp_dup['statement_from_date']} – "
                             f"{fp_dup['statement_to_date']}) was already uploaded on "
                             f"{str(fp_dup['upload_date'])[:10]} by {fp_dup['uploader']}. "
                             f"Renaming the file does not bypass this check.",
                    "duplicate_level": 2,
                    "existing_upload_id": fp_dup["id"],
                }
                return

        # Store content fingerprint
        conn_sf = get_db()
        csf = conn_sf.cursor()
        csf.execute("UPDATE uploads SET content_fingerprint=%s WHERE id=%s", (content_fp, up_id))
        conn_sf.commit()
        conn_sf.close()

        save_stats = save_extraction(up_id, data, cid)

        # Update dates in DB if manually overridden
        if manual_from and manual_to and data.get("statement_from_date"):
            try:
                conn2 = get_db()
                cc = conn2.cursor()
                cc.execute("UPDATE extractions SET statement_from_date=%s,statement_to_date=%s WHERE upload_id=%s",
                           (data["statement_from_date"], data["statement_to_date"], up_id))
                conn2.commit()
                conn2.close()
            except Exception:
                pass

        _upload_progress[up_id] = {"pct": 85, "stage": "Running period checks...", "done": False}

        warnings = []
        s_from   = data.get("statement_from_date", "")
        s_to     = data.get("statement_to_date", "")
        stockist = data.get("stockist_name", "")
        date_source = "manual" if (manual_from and manual_to) else "auto"

        if s_from or s_to:
            warnings.append({
                "type": "period_info", "level": "info",
                "message": f"📅 Statement period {'manually set' if date_source=='manual' else 'auto-detected'}: "
                           f"{s_from or '?'} → {s_to or '?'}"
            })

        if s_from and s_to:
            period_type, days = classify_period(s_from, s_to)
            period_labels = {"monthly": "Monthly statement", "quarterly": "Quarterly statement",
                             "half-yearly": "Half-yearly statement", "yearly": "Full-year statement"}
            plabel = period_labels.get(period_type, f"Statement ({days} days)")
            warnings.append({"type": "period_type", "level": "info",
                              "message": f"✓ {plabel} detected -- {s_from} to {s_to}"})
            if not force_proceed:
                dupes = check_period_duplicate(cid, stockist, s_from, s_to, exclude_upload_id=up_id)
                if dupes:
                    d0 = dupes[0]
                    ov_type   = d0.get("overlap_type", "exact")
                    ex_ptype  = d0.get("existing_period_type", "")
                    ex_plabel = period_labels.get(ex_ptype, "statement")
                    ex_range  = f"{d0['statement_from_date']}–{d0['statement_to_date']}"
                    uploaded_meta = f"(uploaded {str(d0['upload_date'])[:10]} by {d0['uploader']})"
                    if ov_type == "exact":
                        ov_msg = (f"⚠️ Duplicate! '{stockist}' already has a statement for "
                                  f"{s_from}–{s_to} {uploaded_meta}.")
                    elif ov_type == "contained":
                        ov_msg = (f"⚠️ This {period_type} period ({s_from}–{s_to}) is already covered by an "
                                  f"existing {ex_plabel.lower()} for '{stockist}' spanning {ex_range} "
                                  f"{uploaded_meta}.")
                    elif ov_type == "contains":
                        ov_msg = (f"⚠️ This {period_type} period ({s_from}–{s_to}) already includes an "
                                  f"existing {ex_plabel.lower()} for '{stockist}' covering {ex_range} "
                                  f"{uploaded_meta}. Uploading both may double-count that range.")
                    else:
                        ov_msg = (f"⚠️ This {period_type} period ({s_from}–{s_to}) partially overlaps an "
                                  f"existing {ex_plabel.lower()} for '{stockist}' covering {ex_range} "
                                  f"{uploaded_meta}.")
                    ov_msg += " You can proceed anyway or view the existing."
                    warnings.append({
                        "type": "duplicate_period", "level": "warning",
                        "message": ov_msg,
                        "upload_id": d0["id"],
                        "overlap_type": ov_type,
                        "overlap_count": len(dupes)
                    })

        dbg = data.get("_debug", {})
        extracted_total = float(data.get("invoice_net", 0) or 0)
        doc_grand_total = float(dbg.get("doc_grand_total", 0) or 0)
        match_pct = 100.0
        match_diff = 0.0
        if doc_grand_total > 0:
            match_diff = abs(extracted_total - doc_grand_total)
            match_pct  = round(max(0, (1 - match_diff / doc_grand_total) * 100), 1)

        u_obj = get_user(uid_s)
        ok, remaining = consume_credit(
            cid, "extraction",
            division_id=u_obj["division_id"] if u_obj else None,
            user_id=uid_s, reference_id=up_id,
            detail=f"Extracted: {data.get('stockist_name', '')[:40]}, {n_parties} parties")

        # ── Auto-create verification task + auto-assign to least-loaded agent ──
        try:
            conn_av = get_db()
            cav = conn_av.cursor()
            cav.execute("SELECT id FROM manual_verifications WHERE upload_id=%s", (up_id,))
            existing_mv = cav.fetchone()
            if not existing_mv:
                cav.execute("""INSERT INTO manual_verifications
                    (upload_id, company_id, division_id, status)
                    VALUES (%s, %s, %s, 'pending')""",
                    (up_id, cid, u_obj["division_id"] if u_obj else None))
                conn_av.commit()
            cav.execute("""SELECT sa.id,
                COUNT(mv2.id) FILTER (WHERE mv2.status='pending') as pending_count
                FROM super_admins sa
                LEFT JOIN manual_verifications mv2 ON mv2.assigned_to=sa.id AND mv2.status='pending'
                WHERE sa.status='active' AND sa.role='agent'
                GROUP BY sa.id ORDER BY pending_count ASC LIMIT 1""")
            agent_row = cav.fetchone()
            if agent_row:
                cav.execute("""UPDATE manual_verifications
                    SET assigned_to=%s
                    WHERE upload_id=%s AND status='pending' AND assigned_to IS NULL""",
                    (agent_row[0], up_id))
            cav.execute("UPDATE uploads SET verification_status='pending_verification' WHERE id=%s", (up_id,))
            conn_av.commit()
            conn_av.close()
        except Exception as av_err:
            traceback.print_exc()
            print(f"[AUTO-ASSIGN ERROR] {av_err}")
            try: conn_av.rollback()
            except Exception: pass
            try: conn_av.close()
            except Exception: pass

        _upload_progress[up_id] = {
            "pct": 100, "done": True, "success": True,
            "upload_id": up_id, "data": data,
            "warnings": warnings, "date_source": date_source,
            "credits_remaining": remaining, "extraction_debug": dbg,
            "match_pct": match_pct, "match_diff": match_diff,
            "extracted_total": extracted_total, "doc_grand_total": doc_grand_total,
            "stage": f"✓ Complete -- {n_parties} parties, {save_stats.get('items_saved', 0)} items",
        }

    except Exception as e:
        traceback.print_exc()
        conn = get_db()
        c3 = conn.cursor()
        c3.execute("UPDATE uploads SET status='error',error_msg=%s WHERE id=%s", (str(e), up_id))
        conn.commit()
        conn.close()
        _upload_progress[up_id] = {"pct": 100, "done": True, "error": str(e)}


@router.route("/extraction/<int:up_id>")
@login_required
def extraction_detail(up_id):
    user = get_user(session["user_id"])
    accessible = get_accessible_ids(user)
    conn = get_db()
    c = conn.cursor()
    ph = ",".join(["%s"] * len(accessible))
    c.execute(f"""
        SELECT u.*,us.full_name as uploader_name,us.area,us.region,us.role as uploader_role
        FROM uploads u JOIN users us ON u.user_id=us.id
        WHERE u.id=%s AND u.user_id IN ({ph})
    """, [up_id] + accessible)
    upload = fetchone_dict(c)
    if not upload:
        conn.close()
        flash("Not found")
        return redirect(url_for("dashboard"))
    c.execute("SELECT * FROM extractions WHERE upload_id=%s", (up_id,))
    ext = fetchone_dict(c)
    parties = []
    if ext:
        c.execute("SELECT * FROM parties WHERE extraction_id=%s ORDER BY id", (ext["id"],))
        for p in fetchall_dict(c):
            c.execute("SELECT * FROM items WHERE party_id=%s ORDER BY id", (p["id"],))
            parties.append({"party": p, "items": fetchall_dict(c)})
    conn.close()
    debug_info = {}
    if ext and ext.get("raw_json"):
        try:
            raw = json.loads(ext["raw_json"])
            debug_info = raw.get("_debug", {})
        except Exception:
            pass
    return render_template("index.html", page="detail", user=user, upload=upload,
                           extraction=ext, parties=parties, debug_info=debug_info)


@router.route("/analytics")
@login_required
def analytics():
    user = get_user(session["user_id"])
    accessible = get_accessible_ids(user)

    # NSM fallback: if no configured subtree, use full division
    if user["role"] == "nsm" and len(accessible) <= 1:
        conn_fb = get_db(); c_fb = conn_fb.cursor()
        c_fb.execute("SELECT id FROM users WHERE company_id=%s AND division_id=%s AND status='active'",
                     (user["company_id"], user.get("division_id")))
        div_ids = [r[0] for r in c_fb.fetchall()]
        conn_fb.close()
        if div_ids: accessible = div_ids
    if not accessible: accessible = [user["id"]]

    conn = get_db()
    c = conn.cursor()
    ph = ",".join(["%s"] * len(accessible))

    # ── All analytics use VERIFIED data only ─────────────────────────────────
    # Monthly trend — verified revenue + upload counts
    c.execute(f"""
        SELECT TO_CHAR(u.upload_date,'YYYY-MM') as month,
               COUNT(*) as uploads,
               COUNT(*) FILTER (WHERE u.status='done')                         as done_count,
               COUNT(*) FILTER (WHERE u.status='rejected')                     as rejected_count,
               COALESCE(SUM(CASE WHEN u.verification_status='verified'
                            THEN e.invoice_net ELSE 0 END),0)                  as revenue,
               COALESCE(SUM(CASE WHEN u.status='done'
                            THEN e.total_quantity ELSE 0 END),0)               as units
        FROM uploads u LEFT JOIN extractions e ON e.upload_id=u.id
        WHERE u.user_id IN ({ph}) AND u.upload_date>=NOW()-INTERVAL '12 months'
        GROUP BY month ORDER BY month
    """, accessible)
    monthly = fetchall_dict(c)

    # Weekly trend — verified revenue
    c.execute(f"""
        SELECT TO_CHAR(u.upload_date,'IYYY-IW') as week,
               COUNT(*) as uploads,
               COALESCE(SUM(CASE WHEN u.verification_status='verified'
                            THEN e.invoice_net ELSE 0 END),0) as revenue
        FROM uploads u LEFT JOIN extractions e ON e.upload_id=u.id
        WHERE u.user_id IN ({ph}) AND u.upload_date>=NOW()-INTERVAL '84 days'
        GROUP BY week ORDER BY week
    """, accessible)
    weekly = fetchall_dict(c)

    # Party type distribution — verified only
    c.execute(f"""
        SELECT p.type, COUNT(DISTINCT p.name) as count,
               COALESCE(SUM(p.total_amount),0) as revenue
        FROM parties p JOIN extractions e ON p.extraction_id=e.id
        JOIN uploads u ON e.upload_id=u.id
        WHERE u.user_id IN ({ph}) AND u.verification_status='verified'
        GROUP BY p.type
    """, accessible)
    party_dist = fetchall_dict(c)

    # Top 10 brands — verified only
    c.execute(f"""
        SELECT i.brand, SUM(i.quantity) as qty, SUM(i.final_amount) as amt
        FROM items i JOIN parties p ON i.party_id=p.id
        JOIN extractions e ON p.extraction_id=e.id
        JOIN uploads u ON e.upload_id=u.id
        WHERE u.user_id IN ({ph}) AND u.verification_status='verified'
          AND i.brand!='' AND i.final_amount>0
          AND UPPER(TRIM(i.brand)) NOT IN ('CUMULATIVE SUMMARY','TOTAL','GRAND TOTAL','SUB TOTAL','SUBTOTAL')
        GROUP BY i.brand ORDER BY amt DESC LIMIT 10
    """, accessible)
    brands = fetchall_dict(c)

    # ── Role-aware performance leaderboard ───────────────────────────────────
    # Determine which role level to show in leaderboard per viewer's role
    role = user["role"]
    perf_role_map = {
        "company_admin": "nsm",
        "nsm":           "zsm",
        "zsm":           "rsm",
        "rsm":           "asm",
        "asm":           "mr",
        "mr":            "mr",
    }
    perf_role = perf_role_map.get(role, "mr")

    # Drill-down: if a specific user is selected, show their direct reports one level down
    perf_uid = request.args.get("perf_uid", "")
    drill_accessible = accessible
    if perf_uid and perf_uid.isdigit():
        perf_uid_int = int(perf_uid)
        # Get subordinates of the selected user
        conn2 = get_db(); c2 = conn2.cursor()
        c2.execute("""
            WITH RECURSIVE sub(id) AS (
                SELECT id FROM users WHERE id=%s
                UNION ALL
                SELECT u.id FROM users u JOIN sub s ON u.parent_id=s.id WHERE u.status='active'
            ) SELECT id FROM sub""", (perf_uid_int,))
        drill_accessible = [r[0] for r in c2.fetchall()]
        conn2.close()
        # Move one level deeper for drill-down
        drill_role_map = {"nsm":"zsm","zsm":"rsm","rsm":"asm","asm":"mr","mr":"mr"}
        perf_role = drill_role_map.get(perf_role, "mr")

    ph_perf = ",".join(["%s"] * len(drill_accessible))
    c.execute(f"""
        WITH RECURSIVE sub(manager_id, leaf_id) AS (
            -- Each manager is their own leaf (captures their direct uploads too)
            SELECT id, id FROM users WHERE id IN ({ph_perf}) AND role=%s AND status='active'
            UNION ALL
            -- Walk down the tree from each manager
            SELECT s.manager_id, u.id
            FROM users u JOIN sub s ON u.parent_id=s.leaf_id
            WHERE u.status='active'
        )
        SELECT us.id, us.full_name, us.area, us.role, us.employee_id,
               COUNT(DISTINCT u.id) as bills,
               COUNT(DISTINCT u.id) FILTER (WHERE u.status='rejected') as rejected_bills,
               COALESCE(SUM(CASE WHEN u.status='done' THEN e.invoice_net ELSE 0 END),0) as revenue,
               COALESCE(SUM(CASE WHEN u.verification_status='verified' THEN e.invoice_net ELSE 0 END),0) as verified_revenue
        FROM users us
        JOIN sub ON sub.manager_id=us.id
        LEFT JOIN uploads u ON u.user_id=sub.leaf_id
        LEFT JOIN extractions e ON e.upload_id=u.id
        WHERE us.id IN ({ph_perf}) AND us.role=%s AND us.status='active'
        GROUP BY us.id, us.full_name, us.area, us.role, us.employee_id
        ORDER BY verified_revenue DESC, revenue DESC
    """, drill_accessible + [perf_role] + drill_accessible + [perf_role])
    mr_perf = fetchall_dict(c)

    # Drill-down selector list: direct reports of viewer at the leaderboard role
    ph_sel = ",".join(["%s"] * len(accessible))
    c.execute(f"""SELECT id, full_name, role FROM users
        WHERE id IN ({ph_sel}) AND role=%s AND status='active'
        ORDER BY full_name""", accessible + [perf_role])
    perf_members = fetchall_dict(c)

    c.execute(f"""
        SELECT u.id, u.upload_date, u.original_filename, u.status,
               COALESCE(e.invoice_net, e.total_amount, 0) as invoice_net,
               e.bill_number, e.stockist_name, us.full_name as uploaded_by
        FROM uploads u
        LEFT JOIN extractions e ON e.upload_id=u.id
        LEFT JOIN users us ON us.id=u.user_id
        WHERE u.user_id IN ({ph})
        ORDER BY u.upload_date DESC
        LIMIT 10
    """, accessible)
    recent_docs = fetchall_dict(c)

    # Rejection summary
    c.execute(f"""
        SELECT u.rejection_reason, u.rejection_type,
               COUNT(*) as cnt,
               rej.full_name as rejected_by_name
        FROM uploads u
        LEFT JOIN users rej ON u.rejected_by=rej.id
        WHERE u.user_id IN ({ph}) AND u.status='rejected'
          AND u.rejection_reason IS NOT NULL
        GROUP BY u.rejection_reason, u.rejection_type, rej.full_name
        ORDER BY cnt DESC LIMIT 10
    """, accessible)
    rejection_summary = fetchall_dict(c)

    # Top 10 agencies — verified only
    c.execute(f"""
        SELECT e.stockist_name as name,
               COUNT(DISTINCT u.id) as uploads,
               COALESCE(SUM(e.invoice_net),0) as amt
        FROM extractions e JOIN uploads u ON e.upload_id=u.id
        WHERE u.user_id IN ({ph}) AND u.verification_status='verified'
          AND e.stockist_name IS NOT NULL AND e.stockist_name!=''
        GROUP BY e.stockist_name ORDER BY amt DESC LIMIT 10
    """, accessible)
    top_agencies = fetchall_dict(c)

    # Top 10 party names — verified only
    c.execute(f"""
        SELECT p.name, COALESCE(SUM(p.total_amount),0) as amt,
               COUNT(DISTINCT u.id) as uploads
        FROM parties p JOIN extractions e ON p.extraction_id=e.id
        JOIN uploads u ON e.upload_id=u.id
        WHERE u.user_id IN ({ph}) AND u.verification_status='verified'
          AND p.name IS NOT NULL AND TRIM(p.name)!=''
          AND p.total_amount>0
          AND UPPER(TRIM(p.name)) NOT IN ('CUMULATIVE SUMMARY','TOTAL','GRAND TOTAL',
              'SUB TOTAL','SUBTOTAL','NET SALES','NET TOTAL')
          AND LENGTH(TRIM(p.name))>2
        GROUP BY p.name HAVING COALESCE(SUM(p.total_amount),0)>0
        ORDER BY amt DESC LIMIT 10
    """, accessible)
    top_parties = fetchall_dict(c)

    # KPI — verified revenue only (no double counting)
    c.execute(f"""
        SELECT COUNT(DISTINCT u.id)                                                    AS total_bills,
               COUNT(DISTINCT u.id) FILTER (WHERE u.status='done')                    AS done_bills,
               COUNT(DISTINCT u.id) FILTER (WHERE u.status='rejected')                AS rejected_bills,
               COALESCE(SUM(CASE WHEN u.verification_status='verified'
                            THEN e.invoice_net ELSE 0 END),0)                         AS total_revenue
        FROM uploads u
        LEFT JOIN extractions e ON e.upload_id=u.id
        WHERE u.user_id IN ({ph})
    """, accessible)
    kpi = dict(fetchone_dict(c) or {})

    # Unique parties — verified only
    c.execute(f"""
        SELECT COUNT(DISTINCT p.name) AS total_parties
        FROM parties p
        JOIN extractions e ON p.extraction_id=e.id
        JOIN uploads u ON e.upload_id=u.id
        WHERE u.user_id IN ({ph}) AND u.verification_status='verified'
          AND p.name IS NOT NULL AND TRIM(p.name)!=''
    """, accessible)
    parties_row = fetchone_dict(c)
    kpi["total_parties"] = (parties_row or {}).get("total_parties", 0)

    conn.close()
    return render_template("index.html", page="analytics", user=user,
        monthly=monthly, weekly=weekly, party_dist=party_dist, brands=brands,
        mr_perf=mr_perf, perf_members=perf_members,
        rejection_summary=rejection_summary, recent_docs=recent_docs,
        top_agencies=top_agencies, top_parties=top_parties, kpi=kpi)


@router.route("/team")
@login_required
def team_list():
    user = get_user(session["user_id"])
    if user["role"] not in ("company_admin", "nsm", "zsm", "rsm", "asm"):
        flash("Access denied")
        return redirect(url_for("dashboard"))
    cid = user["company_id"]
    conn = get_db()
    c = conn.cursor()

    # ── Scope: who can this user see? ────────────────────────────────────────
    accessible = get_accessible_ids(user)
    ph = ",".join(["%s"] * len(accessible))

    # Role caps: what roles are visible to each manager level
    role_caps = {
        "company_admin": ["company_admin", "nsm", "zsm", "rsm", "asm", "mr"],
        "nsm":           ["nsm", "zsm", "rsm", "asm", "mr"],
        "zsm":           ["zsm", "rsm", "asm", "mr"],
        "rsm":           ["rsm", "asm", "mr"],
        "asm":           ["asm", "mr"],
    }
    team_roles = role_caps.get(user["role"], ["mr"])
    role_ph    = ",".join(["%s"] * len(team_roles))

    c.execute(f"""
        SELECT u.*, p.full_name as parent_name, p.employee_id as parent_emp_id,
               d.name as div_name, COUNT(DISTINCT up.id) as upload_count
        FROM users u LEFT JOIN users p ON u.parent_id=p.id
        LEFT JOIN divisions d ON u.division_id=d.id
        LEFT JOIN uploads up ON up.user_id=u.id
        WHERE u.id IN ({ph}) AND u.role IN ({role_ph})
        GROUP BY u.id, p.full_name, p.employee_id, d.name
        ORDER BY CASE u.role
            WHEN 'company_admin' THEN 1 WHEN 'nsm' THEN 2 WHEN 'zsm' THEN 3
            WHEN 'rsm' THEN 4 WHEN 'asm' THEN 5 ELSE 6 END, u.full_name
    """, accessible + team_roles)
    users = fetchall_dict(c)
    for _u in users:
        _u.pop("password", None)  # never send password hashes to the client --
                                    # this list gets embedded client-side via
                                    # {{ u|tojson|safe }} for the edit-user modal

    # Managers list (for reporting-to dropdowns) — scoped to hierarchy
    c.execute(f"""SELECT id,full_name,role,employee_id FROM users
        WHERE id IN ({ph}) AND status='active' AND role!='mr' AND role IN ({role_ph})
        ORDER BY CASE role WHEN 'company_admin' THEN 1 WHEN 'nsm' THEN 2
            WHEN 'zsm' THEN 3 WHEN 'rsm' THEN 4 ELSE 5 END, full_name""",
        accessible + team_roles)
    managers = fetchall_dict(c)

    c.execute(f"SELECT id,full_name,employee_id,role FROM users WHERE id IN ({ph}) AND status='active' ORDER BY full_name", accessible)
    all_active = fetchall_dict(c)

    c.execute("SELECT * FROM divisions WHERE company_id=%s AND status='active'", (cid,))
    divisions = fetchall_dict(c)
    c.execute("SELECT * FROM zones WHERE company_id=%s", (cid,))
    zones = fetchall_dict(c)

    c.execute(f"SELECT id,full_name,employee_id FROM users WHERE id IN ({ph}) AND status='active' AND role='mr' ORDER BY full_name", accessible)
    mr_list = fetchall_dict(c)

    # Broken hierarchy — scoped
    c.execute(f"""SELECT u.id,u.full_name,u.employee_id,u.role
        FROM users u
        LEFT JOIN users p ON u.parent_id=p.id AND p.company_id=u.company_id AND p.status='active'
        WHERE u.id IN ({ph}) AND u.status='active' AND u.role IN ('mr','asm','rsm','zsm')
          AND (u.parent_id IS NULL OR p.id IS NULL)
        ORDER BY u.role, u.full_name""", accessible)
    broken_hierarchy = fetchall_dict(c)

    c.execute(f"""SELECT h.*,f.full_name as from_name,t.full_name as to_name
        FROM handovers h JOIN users f ON h.from_user_id=f.id JOIN users t ON h.to_user_id=t.id
        WHERE h.company_id=%s AND (h.from_user_id IN ({ph}) OR h.to_user_id IN ({ph}))
        ORDER BY h.done_at DESC LIMIT 10""", (cid,) + tuple(accessible) + tuple(accessible))
    handovers = fetchall_dict(c)
    conn.close()
    return render_template("index.html", page="team", user=user,
        users=users, managers=managers, divisions=divisions, zones=zones,
        mr_list=mr_list, all_active=all_active, handovers=handovers,
        broken_hierarchy=broken_hierarchy)


@router.route("/team/add", methods=["POST"])
@admin_required
def team_add():
    user = get_user(session["user_id"])
    d = request.form
    cid = user["company_id"]
    un  = d.get("username", "").strip() or d.get("mobile", "").strip()
    pw  = d.get("password", "").strip()
    fn  = d.get("full_name", "").strip()
    if not un or not pw or not fn:
        flash("Name, username and password required")
        return redirect(url_for("team_list"))
    parent_id = d.get("parent_id") or None
    role = (d.get("role", "mr") or "mr").strip().lower()
    if not require_parent_for_role(role, parent_id):
        flash("Reporting manager is required for MR/ASM/RSM/ZSM users")
        return redirect(url_for("team_list"))
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("""INSERT INTO users
            (company_id,division_id,zone_id,username,password,full_name,role,
             parent_id,region,area,territory,employee_id,mobile,email,status,joined_date)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',%s)""",
            (cid, d.get("division_id") or None, d.get("zone_id") or None,
             un, hash_pw(pw), fn, d.get("role", "mr"), d.get("parent_id") or None,
             d.get("region", ""), d.get("area", ""), d.get("territory", ""),
             d.get("employee_id", ""), d.get("mobile", ""), d.get("email", ""),
             datetime.now().strftime("%Y-%m-%d")))
        conn.commit()
        flash(f"User '{fn}' created successfully")
    except Exception:
        conn.rollback()
        flash("Username or mobile already exists in this company")
    finally:
        conn.close()
    return redirect(url_for("team_list"))


@router.route("/team/edit/<int:uid>", methods=["POST"])
@admin_required
def team_edit(uid):
    d = request.form
    new_pw = d.get("new_password", "").strip()
    role = (d.get("role", "mr") or "mr").strip().lower()
    parent_id = d.get("parent_id") or None
    if not require_parent_for_role(role, parent_id):
        flash("Reporting manager is required for MR/ASM/RSM/ZSM users")
        return redirect(url_for("team_list"))
    conn = get_db()
    c = conn.cursor()
    try:
        if new_pw:
            c.execute("""UPDATE users SET full_name=%s,role=%s,parent_id=%s,division_id=%s,zone_id=%s,
                region=%s,area=%s,territory=%s,employee_id=%s,mobile=%s,email=%s,status=%s,password=%s
                WHERE id=%s""",
                (d.get("full_name", ""), d.get("role", "mr"), parent_id,
                 d.get("division_id") or None, d.get("zone_id") or None,
                 d.get("region", ""), d.get("area", ""), d.get("territory", ""),
                 d.get("employee_id", ""), d.get("mobile", ""), d.get("email", ""),
                 d.get("status", "active"), hash_pw(new_pw), uid))
        else:
            c.execute("""UPDATE users SET full_name=%s,role=%s,parent_id=%s,division_id=%s,zone_id=%s,
                region=%s,area=%s,territory=%s,employee_id=%s,mobile=%s,email=%s,status=%s
                WHERE id=%s""",
                (d.get("full_name", ""), d.get("role", "mr"), d.get("parent_id") or None,
                 d.get("division_id") or None, d.get("zone_id") or None,
                 d.get("region", ""), d.get("area", ""), d.get("territory", ""),
                 d.get("employee_id", ""), d.get("mobile", ""), d.get("email", ""),
                 d.get("status", "active"), uid))
        if d.get("status") == "left":
            c.execute("UPDATE users SET left_date=%s WHERE id=%s",
                      (datetime.now().strftime("%Y-%m-%d"), uid))
        conn.commit()
        flash("✓ User updated successfully")
    except Exception as e:
        conn.rollback()
        flash(f"Error: {e}")
    finally:
        conn.close()
    return redirect(url_for("team_list"))


@router.route("/team/delete/<int:uid>", methods=["POST"])
@admin_required
def team_delete(uid):
    """
    NOTE: this no longer deletes the user record. Users are never hard-deleted
    -- their uploads, verification history, and audit trail need to stay
    attached to a real user row. This toggles status between 'active' and
    'inactive' instead (an inactive user can't log in -- see login_required /
    get_user -- but all their historical data stays intact and visible).
    """
    if uid == session["user_id"]:
        flash("Cannot deactivate your own account")
        return redirect(url_for("team_list"))
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT status, full_name FROM users WHERE id=%s", (uid,))
    row = c.fetchone()
    if not row:
        conn.close()
        flash("User not found")
        return redirect(url_for("team_list"))
    current_status, full_name = row[0], row[1]
    new_status = "inactive" if current_status != "inactive" else "active"
    c.execute("UPDATE users SET status=%s WHERE id=%s", (new_status, uid))
    conn.commit()
    conn.close()
    admin_user = get_user(session["user_id"])
    log_activity(
        "deactivate_user" if new_status == "inactive" else "reactivate_user",
        "user", uid, f"{full_name} set to {new_status} by {admin_user['full_name'] if admin_user else session['user_id']}"
    )
    flash(f"✓ {full_name} is now {new_status}")
    return redirect(url_for("team_list"))


@router.route("/divisions")
@admin_required
def division_list():
    user = get_user(session["user_id"])
    cid  = user["company_id"]
    conn = get_db()
    c = conn.cursor()
    c.execute("""SELECT d.*, COUNT(u.id) as member_count
        FROM divisions d LEFT JOIN users u ON u.division_id=d.id AND u.status='active'
        WHERE d.company_id=%s GROUP BY d.id ORDER BY d.name""", (cid,))
    divs = fetchall_dict(c)
    conn.close()
    return render_template("index.html", page="divisions", user=user, divisions=divs)


@router.route("/divisions/add", methods=["POST"])
@admin_required
def division_add():
    user = get_user(session["user_id"])
    cid  = user["company_id"]
    name = request.form.get("name", "").strip()
    code = request.form.get("code", "").strip().upper()
    desc = request.form.get("description", "").strip()
    if not name:
        flash("Division name is required")
        return redirect(url_for("division_list"))
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("INSERT INTO divisions (company_id,name,code,description,status) VALUES (%s,%s,%s,%s,'active')",
                  (cid, name, code, desc))
        conn.commit()
        flash(f"✓ Division '{name}' added")
    except Exception as e:
        conn.rollback()
        flash(f"Error: {e}")
    finally:
        conn.close()
    return redirect(url_for("division_list"))


@router.route("/divisions/edit/<int:did>", methods=["POST"])
@admin_required
def division_edit(did):
    user = get_user(session["user_id"])
    cid  = user["company_id"]
    name = request.form.get("name", "").strip()
    code = request.form.get("code", "").strip().upper()
    desc = request.form.get("description", "").strip()
    status = request.form.get("status", "active")
    if not name:
        flash("Division name is required")
        return redirect(url_for("division_list"))
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("UPDATE divisions SET name=%s,code=%s,description=%s,status=%s WHERE id=%s AND company_id=%s",
                  (name, code, desc, status, did, cid))
        conn.commit()
        flash(f"✓ Division updated to '{name}'")
    except Exception as e:
        conn.rollback()
        flash(f"Error: {e}")
    finally:
        conn.close()
    return redirect(url_for("division_list"))


@router.route("/divisions/delete/<int:did>", methods=["POST"])
@admin_required
def division_delete(did):
    user = get_user(session["user_id"])
    cid  = user["company_id"]
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) as n FROM users WHERE division_id=%s AND status='active'", (did,))
    cnt = c.fetchone()[0]
    if cnt > 0:
        conn.close()
        flash(f"Cannot delete -- {cnt} active user(s) assigned. Reassign them first.")
        return redirect(url_for("division_list"))
    c.execute("DELETE FROM divisions WHERE id=%s AND company_id=%s", (did, cid))
    conn.commit()
    conn.close()
    flash("✓ Division deleted")
    return redirect(url_for("division_list"))


@router.route("/team/handover", methods=["POST"])
@login_required
def team_handover():
    user    = get_user(session["user_id"])
    if user["role"] not in ("company_admin", "nsm", "zsm", "rsm", "asm"):
        flash("Access denied"); return redirect(url_for("team_list"))

    from_id      = int(request.form.get("from_user_id", 0))
    to_id        = int(request.form.get("to_user_id", 0))
    notes        = request.form.get("notes", "").strip()
    mode         = request.form.get("mode", "full")   # "full" = exit+transfer | "remap" = change manager only

    if not from_id or not to_id or from_id == to_id:
        flash("Select two different employees"); return redirect(url_for("team_list"))

    conn = get_db()
    c = conn.cursor()
    cid = user["company_id"]

    # Verify both users belong to this company
    c.execute("SELECT id,full_name,role FROM users WHERE id IN (%s,%s) AND company_id=%s",
              (from_id, to_id, cid))
    rows = {r[0]: {"name": r[1], "role": r[2]} for r in c.fetchall()}
    if len(rows) < 2:
        flash("Invalid users selected"); conn.close(); return redirect(url_for("team_list"))

    from_name = rows[from_id]["name"]
    to_name   = rows[to_id]["name"]

    if mode == "remap":
        # ── Change reporting manager only — no upload transfer, no exit ──────
        # All direct reports of from_id get reassigned to to_id
        c.execute("SELECT COUNT(*) FROM users WHERE parent_id=%s AND company_id=%s AND status='active'",
                  (from_id, cid))
        sub_count = c.fetchone()[0]
        c.execute("UPDATE users SET parent_id=%s WHERE parent_id=%s AND company_id=%s AND status='active'",
                  (to_id, from_id, cid))
        c.execute("INSERT INTO handovers (company_id,from_user_id,to_user_id,transfers,notes,done_by) VALUES (%s,%s,%s,%s,%s,%s)",
                  (cid, from_id, to_id, sub_count, f"[REMAP] {notes}", user["id"]))
        conn.commit(); conn.close()
        flash(f"✓ {sub_count} team member(s) under {from_name} reassigned to {to_name}.")
        return redirect(url_for("team_list"))

    else:
        # ── Full exit handover ────────────────────────────────────────────────
        # 1. Transfer all uploads
        c.execute("SELECT COUNT(*) FROM uploads WHERE user_id=%s", (from_id,))
        upload_cnt = c.fetchone()[0]
        c.execute("UPDATE uploads SET user_id=%s WHERE user_id=%s", (to_id, from_id))

        # 2. Reassign all direct reports (subordinates) to to_id
        c.execute("SELECT COUNT(*) FROM users WHERE parent_id=%s AND company_id=%s AND status='active'",
                  (from_id, cid))
        sub_count = c.fetchone()[0]
        c.execute("UPDATE users SET parent_id=%s WHERE parent_id=%s AND company_id=%s",
                  (to_id, from_id, cid))

        # 3. Mark from_user as left
        c.execute("UPDATE users SET status='left', left_date=%s, successor_id=%s WHERE id=%s",
                  (datetime.now().strftime("%Y-%m-%d"), to_id, from_id))

        # 4. Log handover
        detail = f"Uploads: {upload_cnt} | Subordinates reassigned: {sub_count} | {notes}"
        c.execute("INSERT INTO handovers (company_id,from_user_id,to_user_id,transfers,notes,done_by) VALUES (%s,%s,%s,%s,%s,%s)",
                  (cid, from_id, to_id, upload_cnt, detail, user["id"]))
        conn.commit(); conn.close()
        flash(f"✓ Full handover complete — {upload_cnt} uploads and {sub_count} subordinate(s) transferred from {from_name} to {to_name}.")
        return redirect(url_for("team_list"))


@router.route("/registrations")
@admin_required
def registrations_list():
    user = get_user(session["user_id"])
    if user["role"] not in ("company_admin", "nsm", "zsm"):
        flash("Access denied")
        return redirect(url_for("dashboard"))
    cid = user["company_id"]
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM registrations WHERE company_id=%s AND status='pending' ORDER BY applied_at DESC", (cid,))
    pending = fetchall_dict(c)
    c.execute("SELECT * FROM registrations WHERE company_id=%s AND status!='pending' ORDER BY reviewed_at DESC LIMIT 50", (cid,))
    reviewed = fetchall_dict(c)
    c.execute("""SELECT id,full_name,role,employee_id FROM users
        WHERE company_id=%s AND status='active' AND role!='mr'
        ORDER BY CASE role WHEN 'company_admin' THEN 1 WHEN 'nsm' THEN 2 WHEN 'zsm' THEN 3
                           WHEN 'rsm' THEN 4 ELSE 5 END, full_name""", (cid,))
    managers = fetchall_dict(c)
    conn.close()
    return render_template("index.html", page="registrations", user=user,
        pending=pending, reviewed=reviewed, all_users=managers)


@router.route("/registrations/approve/<int:rid>", methods=["POST"])
@admin_required
def approve_registration(rid):
    user = get_user(session["user_id"])
    parent_id_form = request.form.get("parent_id") or None
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM registrations WHERE id=%s", (rid,))
    reg = fetchone_dict(c)
    if not reg:
        flash("Not found")
        conn.close()
        return redirect(url_for("registrations_list"))
    # Username: employee_id first, then proposed_username, then mobile
    un = reg.get("employee_id") or reg.get("proposed_username") or reg["mobile"]

    # Resolve parent_id:
    # 1. Admin's dropdown selection takes priority
    # 2. Auto-resolve from manager_emp_id stored in manager_name field
    parent_id = parent_id_form
    if not parent_id and reg.get("manager_name"):
        mgr_emp = reg["manager_name"].strip()
        if mgr_emp:
            c.execute("""SELECT id FROM users
                WHERE company_id=%s AND UPPER(employee_id)=UPPER(%s) AND status='active'
                LIMIT 1""", (reg["company_id"], mgr_emp))
            mgr_row = c.fetchone()
            if mgr_row:
                parent_id = mgr_row[0]

    if not require_parent_for_role(reg["role"], parent_id):
        flash("Reporting manager is required for MR/ASM/RSM/ZSM registrations")
        conn.close()
        return redirect(url_for("registrations_list"))

    try:
        c.execute("""INSERT INTO users
            (company_id,division_id,username,password,full_name,role,parent_id,
             region,area,territory,employee_id,mobile,email,status,joined_date)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',%s)""",
            (reg["company_id"], reg.get("division_id"),
             un, reg["password"], reg["full_name"], reg["role"],
             parent_id, reg.get("region",""), reg.get("area",""), reg.get("territory",""),
             reg.get("employee_id",""), reg["mobile"], reg.get("email",""),
             datetime.now().strftime("%Y-%m-%d")))
        c.execute("UPDATE registrations SET status='approved',reviewed_by=%s,reviewed_at=%s WHERE id=%s",
                  (user["id"], datetime.now().isoformat(), rid))
        conn.commit()
        # Notify -- in-app notification to the new user (they'll see on first login)
        c.execute("SELECT id FROM users WHERE company_id=%s AND mobile=%s LIMIT 1", (reg["company_id"], reg["mobile"]))
        new_user = c.fetchone()
        if new_user:
            push_notification(reg["company_id"], new_user[0], reg["role"], "registration_approved",
                "Registration Approved",
                f"Welcome {reg['full_name']}! Your account has been approved. You can now login with mobile {reg['mobile']}.",
                ref_id=new_user[0], ref_type="user")
        conn.commit()
        flash(f"✓ Approved! {reg['full_name']} can now login with mobile {reg['mobile']}")
    except Exception:
        conn.rollback()
        flash("Username or mobile already exists")
    finally:
        conn.close()
    return redirect(url_for("registrations_list"))


@router.route("/registrations/reject/<int:rid>", methods=["POST"])
@admin_required
def reject_registration(rid):
    user = get_user(session["user_id"])
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE registrations SET status='rejected',reviewed_by=%s,reviewed_at=%s WHERE id=%s",
              (user["id"], datetime.now().isoformat(), rid))
    conn.commit()
    conn.close()
    flash("Registration rejected")
    log_activity("reject_registration","registration",rid,"Registration rejected")
    return redirect(url_for("registrations_list"))


@router.route("/team/broken-hierarchy.csv")
@login_required
def export_broken_hierarchy():
    user = get_user(session["user_id"])
    if user["role"] not in ("company_admin", "nsm"):
        flash("Access denied")
        return redirect(url_for("team_list"))
    conn = get_db()
    c = conn.cursor()
    c.execute("""SELECT u.id,u.full_name,u.employee_id,u.role,u.parent_id,
        p.full_name as parent_name,p.employee_id as parent_emp_id
        FROM users u
        LEFT JOIN users p ON u.parent_id=p.id AND p.company_id=u.company_id AND p.status='active'
        WHERE u.company_id=%s AND u.status='active' AND u.role IN ('mr','asm','rsm','zsm')
          AND (u.parent_id IS NULL OR p.id IS NULL)
        ORDER BY u.role, u.full_name""", (user["company_id"],))
    rows = fetchall_dict(c)
    conn.close()

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "full_name", "employee_id", "role", "parent_id", "parent_name", "parent_emp_id"])
    for row in rows:
        writer.writerow([
            row.get("id"), row.get("full_name"), row.get("employee_id"),
            row.get("role"), row.get("parent_id"), row.get("parent_name"), row.get("parent_emp_id")
        ])
    output.seek(0)
    buf = BytesIO(output.getvalue().encode("utf-8"))
    return send_file(buf, mimetype="text/csv", as_attachment=True,
                     download_name="broken_hierarchy.csv")


@router.route("/export/excel")
@login_required
def export_excel():
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    user = get_user(session["user_id"])
    date_from     = request.args.get("date_from", "")
    date_to       = request.args.get("date_to", "")
    scope         = request.args.get("scope", "mine")

    # ── Scope: company_admin gets all; everyone else gets own hierarchy ───────
    if user["role"] == "company_admin":
        accessible = get_accessible_ids(user, scope="company")
    elif scope == "mine":
        accessible = get_accessible_ids(user, scope="mine")
    else:
        accessible = get_accessible_ids(user, scope="hierarchy")

    # NSM division fallback
    if user["role"] == "nsm" and len(accessible) <= 1:
        conn_fb = get_db(); c_fb = conn_fb.cursor()
        c_fb.execute("SELECT id FROM users WHERE company_id=%s AND division_id=%s AND status='active'",
                     (user["company_id"], user.get("division_id")))
        div_ids = [r[0] for r in c_fb.fetchall()]
        conn_fb.close()
        if div_ids: accessible = div_ids
    if not accessible: accessible = [user["id"]]

    ph  = ",".join(["%s"] * len(accessible))

    # ── Always verified-only — extracted values not in reports ────────────────
    verified_only = True
    df  = " AND u.verification_status='verified'"   # outer query alias u
    df2 = " AND u2.verification_status='verified'"  # subquery alias u2
    dp  = list(accessible)
    if date_from:
        df  += " AND DATE(u.upload_date)>=%s"
        df2 += " AND DATE(u2.upload_date)>=%s"
        dp.append(date_from)
    if date_to:
        df  += " AND DATE(u.upload_date)<=%s"
        df2 += " AND DATE(u2.upload_date)<=%s"
        dp.append(date_to)
    conn = get_db()
    c = conn.cursor()
    thin = Side(style="thin", color="CCCCCC")
    B = Border(left=thin, right=thin, top=thin, bottom=thin)

    def H(ws, r, cols):
        for i, t in enumerate(cols, 1):
            cl = ws.cell(r, i, t)
            cl.fill = PatternFill("solid", fgColor="1A3560")
            cl.font = Font(bold=True, color="FFFFFF", size=10)
            cl.alignment = Alignment(horizontal="center")
            cl.border = B

    def D(ws, r, vals):
        for i, v in enumerate(vals, 1):
            cl = ws.cell(r, i, v)
            cl.border = B
            cl.font = Font(size=9)
            if isinstance(v, float):
                cl.number_format = "#,##0.00"

    def AW(ws):
        for col in ws.columns:
            mx = max((len(str(c.value)) if c.value else 0) for c in col)
            ws.column_dimensions[get_column_letter(col[0].column)].width = min(mx + 4, 40)

    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    export_type = "Verified" if verified_only else "All OCR"
    ws["A1"] = f"Pharma Sales Report [{export_type}] -- {user['company_name']} ({user['company_code']}) -- {datetime.now().strftime('%d/%m/%Y')}"
    ws["A1"].font = Font(bold=True, size=13, color="1A3560")
    ws.append([])
    c.execute(f"""SELECT
        COUNT(DISTINCT u.id)                          as b,
        COALESCE(SUM(e.invoice_net),0)                as r,
        COALESCE(SUM(e.total_quantity),0)             as u_qty,
        (SELECT COUNT(DISTINCT p2.name)
         FROM parties p2 JOIN extractions e2 ON p2.extraction_id=e2.id
         JOIN uploads u2 ON e2.upload_id=u2.id
         WHERE u2.user_id IN ({ph}) {df2})             as p
        FROM uploads u LEFT JOIN extractions e ON e.upload_id=u.id
        WHERE u.user_id IN ({ph}) {df}""", dp + dp)
    s = fetchone_dict(c)
    r = 3
    H(ws, r, ["Metric", "Value"])
    for lbl, val in [
        ("Company", f"{user['company_name']} ({user['company_code']})"),
        ("Export Type", export_type),
        ("Period", f"{date_from or 'All'} to {date_to or 'All'}"),
        ("Bills", s["b"] or 0),
        ("Invoice Net (₹)", round(s["r"] or 0, 2)),
        ("Parties", s["p"] or 0),
        ("Generated On", datetime.now().strftime("%d/%m/%Y %H:%M")),
    ]:
        r += 1
        D(ws, r, [lbl, val])
    AW(ws)

    ws2 = wb.create_sheet("Bill Register")
    H(ws2, 1, ["Company", "Division", "Doc Type", "Stockist", "Bill No", "Bill Date",
               "From", "To", "Uploader", "Role", "Area",
               "Parties", "Qty", "Invoice Net (₹)", "Verif Status"])
    c.execute(f"""SELECT co.name as co_name, co.code as co_code,
        d.name as div_name,
        us.full_name, us.area, us.role,
        e.stockist_name, e.bill_number, e.bill_date,
        e.statement_from_date, e.statement_to_date,
        e.total_quantity, e.invoice_net, e.doc_type,
        u.verification_status,
        u.upload_date,
        (SELECT COUNT(*) FROM parties p WHERE p.extraction_id=e.id) as pc
        FROM uploads u
        JOIN users us ON u.user_id=us.id
        JOIN companies co ON u.company_id=co.id
        LEFT JOIN divisions d ON u.division_id=d.id
        LEFT JOIN extractions e ON e.upload_id=u.id
        WHERE u.user_id IN ({ph}) {df} ORDER BY u.upload_date DESC""", dp)
    for i, b in enumerate(fetchall_dict(c), 2):
        D(ws2, i, [
            f"{b['co_name']} ({b['co_code']})",
            b["div_name"] or "",
            b["doc_type"] or "STATEMENT",
            b["stockist_name"] or "",
            b["bill_number"] or "", b["bill_date"] or "",
            b["statement_from_date"] or "", b["statement_to_date"] or "",
            b["full_name"], b["role"].upper(), b["area"] or "",
            b["pc"] or 0, b["total_quantity"] or 0,
            round(b["invoice_net"] or 0, 2),
            b["verification_status"] or "ocr_done"
        ])
    AW(ws2)

    # ── Party Sales ───────────────────────────────────────────────────────────
    ws3 = wb.create_sheet("Party Sales")
    H(ws3, 1, ["Company", "Division", "Doc Type", "Stockist", "Bill", "Date",
               "Party", "Type", "Area", "DL No",
               "Brand", "MFG", "Pack", "Batch", "Exp",
               "Qty", "MRP", "Rate", "Tax", "Disc%", "Amount (₹)"])
    # Remove i.final_amount > 0 filter — some verified bills store 0 at item level
    # (total is in invoice_net at extraction level). Keep >=0 to include all rows.
    # Also widen the tax/summary line filter to catch SGST/CGST/GST lines.
    TAX_BRANDS = (
        'CUMULATIVE SUMMARY','TOTAL','GRAND TOTAL','SUB TOTAL','SUBTOTAL',
        'SGST CGST ADD GST','SGST','CGST','IGST','GST','TAX AMOUNT',
        'ROUND OFF','ROUND-OFF','ROUNDING OFF','NET TOTAL','NET SALES',
        'INVOICE TOTAL','TOTAL AMOUNT','BILL TOTAL'
    )
    tb_ph = ",".join(["%s"] * len(TAX_BRANDS))
    c.execute(f"""SELECT co.name as co_name, co.code as co_code,
        d.name as div_name, e.doc_type,
        e.stockist_name, e.bill_number, e.bill_date,
        p.name as pname, p.type as ptype, p.area as parea, p.dl_number,
        i.brand, i.mfg, i.pack, i.batch_no, i.expiry,
        i.quantity, i.mrp, i.unit_rate, i.tax_type, i.discount_percent, i.final_amount
        FROM uploads u
        JOIN companies co ON u.company_id=co.id
        LEFT JOIN divisions d ON u.division_id=d.id
        JOIN extractions e ON e.upload_id=u.id
        JOIN parties p ON p.extraction_id=e.id
        JOIN items i ON i.party_id=p.id
        WHERE u.user_id IN ({ph}) {df}
          AND TRIM(i.brand) != ''
          AND LENGTH(TRIM(i.brand)) > 1
          AND UPPER(TRIM(i.brand)) NOT IN ({tb_ph})
          AND i.final_amount >= 0
        ORDER BY co.code, e.stockist_name, p.name, i.brand""",
        dp + list(TAX_BRANDS))
    for row_i, d in enumerate(fetchall_dict(c), 2):
        D(ws3, row_i, [
            f"{d['co_name']} ({d['co_code']})",
            d["div_name"] or "",
            d["doc_type"] or "STATEMENT",
            d["stockist_name"], d["bill_number"] or "", d["bill_date"] or "",
            d["pname"], d["ptype"] or "", d["parea"] or "", d["dl_number"] or "",
            d["brand"], d["mfg"] or "", d["pack"] or "",
            d["batch_no"] or "", d["expiry"] or "",
            d["quantity"],
            round(d["mrp"] or 0, 2), round(d["unit_rate"] or 0, 2),
            d["tax_type"] or "",
            round(d["discount_percent"] or 0, 1),
            round(d["final_amount"] or 0, 2)
        ])
    AW(ws3)

    # ── Brand Summary ─────────────────────────────────────────────────────────
    ws4 = wb.create_sheet("Brand Summary")
    H(ws4, 1, ["Brand", "Total Qty", "Revenue (₹)", "Avg Disc%", "Buyers"])
    c.execute(f"""SELECT i.brand, SUM(i.quantity) as q, SUM(i.final_amount) as a,
        AVG(i.discount_percent) as d, COUNT(DISTINCT p.name) as b
        FROM items i JOIN parties p ON i.party_id=p.id
        JOIN extractions e ON p.extraction_id=e.id
        JOIN uploads u ON e.upload_id=u.id
        WHERE u.user_id IN ({ph}) {df}
          AND TRIM(i.brand) != ''
          AND LENGTH(TRIM(i.brand)) > 1
          AND UPPER(TRIM(i.brand)) NOT IN ({tb_ph})
          AND i.final_amount >= 0
        GROUP BY i.brand
        HAVING SUM(i.final_amount) > 0
        ORDER BY a DESC""",
        dp + list(TAX_BRANDS))
    for row_i, b in enumerate(fetchall_dict(c), 2):
        D(ws4, row_i, [b["brand"], b["q"] or 0, round(b["a"] or 0, 2),
                       round(b["d"] or 0, 1), b["b"]])
    AW(ws4)

    # ── Team Performance — verified revenue only, no Units column ─────────────
    ws5 = wb.create_sheet("Team Performance")
    H(ws5, 1, ["Name", "Emp ID", "Role", "Division", "Area", "Bills",
               "Verified Revenue (₹)", "Verified Bills"])
    c.execute(f"""SELECT us.full_name, us.employee_id, us.role,
        d.name as div_name, us.area,
        COUNT(DISTINCT u.id)                                                      AS bills,
        COALESCE(SUM(CASE WHEN u.verification_status='verified'
                     THEN e.invoice_net ELSE 0 END),0)                            AS verified_rev,
        COUNT(DISTINCT u.id) FILTER (WHERE u.verification_status='verified')      AS verified_bills
        FROM users us
        LEFT JOIN divisions d ON us.division_id=d.id
        LEFT JOIN uploads u   ON u.user_id=us.id
        LEFT JOIN extractions e ON e.upload_id=u.id
        WHERE us.id IN ({ph})
        GROUP BY us.id, us.full_name, us.employee_id, us.role, d.name, us.area
        ORDER BY verified_rev DESC""", accessible)
    for row_i, t in enumerate(fetchall_dict(c), 2):
        D(ws5, row_i, [
            t["full_name"],
            t["employee_id"] or "",
            t["role"].upper(),
            t["div_name"] or "",
            t["area"] or "",
            t["bills"],
            round(t["verified_rev"] or 0, 2),
            t["verified_bills"] or 0
        ])
    AW(ws5)
    conn.close()
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f"pharma_{user['company_code']}_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    return send_file(buf, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name=fname)


@router.route("/api/whatsapp-report")
@login_required
def whatsapp_report():
    user = get_user(session["user_id"])
    accessible = get_accessible_ids(user)
    ph = ",".join(["%s"] * len(accessible))
    date_from = request.args.get("date_from", "")
    date_to   = request.args.get("date_to", "")
    df = ""
    dp = list(accessible)
    if date_from:
        df += " AND DATE(u.upload_date)>=%s"
        dp.append(date_from)
    if date_to:
        df += " AND DATE(u.upload_date)<=%s"
        dp.append(date_to)
    conn = get_db()
    c = conn.cursor()
    c.execute(f"""SELECT COUNT(DISTINCT e.id) as b,COALESCE(SUM(e.invoice_net),0) as r,
        COALESCE(SUM(e.total_quantity),0) as u FROM uploads u
        LEFT JOIN extractions e ON e.upload_id=u.id WHERE u.user_id IN ({ph}) {df}""", dp)
    s = fetchone_dict(c)
    c.execute(f"""SELECT i.brand,SUM(i.final_amount) as a FROM items i
        JOIN parties p ON i.party_id=p.id JOIN extractions e ON p.extraction_id=e.id
        JOIN uploads u ON e.upload_id=u.id WHERE u.user_id IN ({ph}) {df} AND i.brand!=''
        AND UPPER(TRIM(i.brand)) NOT IN ('CUMULATIVE SUMMARY','TOTAL','GRAND TOTAL','SUB TOTAL','SUBTOTAL')
        AND i.final_amount > 0
        GROUP BY i.brand ORDER BY a DESC LIMIT 5""", dp)
    tb = fetchall_dict(c)
    c.execute(f"""SELECT p.type,COUNT(DISTINCT p.name) as c FROM parties p
        JOIN extractions e ON p.extraction_id=e.id JOIN uploads u ON e.upload_id=u.id
        WHERE u.user_id IN ({ph}) {df} GROUP BY p.type""", dp)
    pt = fetchall_dict(c)
    c.execute(f"""SELECT us.full_name,us.area,COUNT(DISTINCT u.id) as b,COALESCE(SUM(e.invoice_net),0) as r
        FROM users us LEFT JOIN uploads u ON u.user_id=us.id
        LEFT JOIN extractions e ON e.upload_id=u.id
        WHERE us.id IN ({ph}) AND us.role='mr' GROUP BY us.id, us.full_name, us.area
        ORDER BY r DESC LIMIT 8""", accessible)
    mr = fetchall_dict(c)
    conn.close()
    period = f"{date_from} to {date_to}" if date_from and date_to else "All Time"
    emap = {"distributor": "🏭", "chemist": "💊", "retailer": "🛒", "shop": "🏪"}
    lines = [f"📊 *PHARMA SALES REPORT*", f"🏢 _{user['company_name']}_", f"📅 _{period}_",
             f"👤 {user['full_name']} | {ROLE_LABELS.get(user['role'], user['role'])}", "",
             "📋 *Summary*", f"• Bills: *{s['b']}*", f"• Revenue: *₹{s['r']:,.0f}*",
             f"• Units: *{s['u']:,}*", "", "🏪 *Party Breakdown*"] + \
            [f"• {emap.get(p['type'],'📦')} {p['type'].title()}: {p['c']}" for p in pt]
    if tb:
        lines += ["", "🔝 *Top Brands*"] + [f"{i+1}. {b['brand']} -- ₹{b['a']:,.0f}" for i, b in enumerate(tb)]
    if mr:
        lines += ["", "👥 *MR Performance*"] + \
                 [f"• {m['full_name']} ({m['area']}): {m['b']} bills | ₹{m['r']:,.0f}" for m in mr]
    lines += ["", f"_Pharma Analyzer | {datetime.now().strftime('%d/%m/%Y %H:%M')}_"]
    msg = "\\n".join(lines)
    return jsonify({"message": msg, "url": f"https://wa.me/?text={msg}"})


@router.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    user = get_user(session["user_id"])
    if request.method == "POST":
        old = request.form.get("old_password", "").strip()
        new = request.form.get("new_password", "").strip()
        cfm = request.form.get("confirm_password", "").strip()
        if not verify_pw(user["password"], old):
            flash("Wrong current password")
        elif new != cfm:
            flash("Passwords do not match")
        elif len(new) < 6:
            flash("Minimum 6 characters")
        else:
            conn = get_db()
            c = conn.cursor()
            c.execute("UPDATE users SET password=%s WHERE id=%s", (hash_pw(new), user["id"]))
            conn.commit()
            conn.close()
            flash("Password changed successfully")
        return redirect(url_for("profile"))
    return render_template("index.html", page="profile", user=user)


@router.route("/credits")
@login_required
def credits_dashboard():
    user = get_user(session["user_id"])
    if user["role"] not in ("company_admin", "nsm"):
        flash("Access denied")
        return redirect(url_for("dashboard"))
    cid  = user["company_id"]
    conn = get_db()
    c = conn.cursor()
    creds = get_company_credits(cid)

    # NSM scoped to their own division only
    if user["role"] == "nsm":
        div_id = user.get("division_id")
        if div_id:
            # Division usage — only their division
            c.execute("""SELECT d.name as division_name, d.id as division_id,
                           COUNT(ct.id) as operations,
                           COALESCE(SUM(ct.credits_used),0) as credits_used
                    FROM divisions d
                    LEFT JOIN credit_transactions ct ON ct.division_id=d.id AND ct.company_id=%s
                    WHERE d.company_id=%s AND d.id=%s
                    GROUP BY d.id, d.name ORDER BY credits_used DESC""",
                    (cid, cid, div_id))
            div_usage = fetchall_dict(c)

            # Recent transactions — only their division's users
            accessible = get_accessible_ids(user)
            ph = ",".join(["%s"] * len(accessible))
            c.execute(f"""SELECT ct.*, u.full_name as user_name, d.name as division_name
                FROM credit_transactions ct
                LEFT JOIN users u ON ct.user_id=u.id
                LEFT JOIN divisions d ON ct.division_id=d.id
                WHERE ct.company_id=%s AND ct.user_id IN ({ph})
                ORDER BY ct.created_at DESC LIMIT 20""",
                [cid] + accessible)
            recent_tx = fetchall_dict(c)

            # Daily usage — their division users only
            c.execute(f"""SELECT DATE(ct.created_at) as day, SUM(ct.credits_used) as used
                FROM credit_transactions ct
                WHERE ct.company_id=%s AND ct.user_id IN ({ph})
                  AND ct.created_at >= NOW()-INTERVAL '14 days'
                GROUP BY day ORDER BY day""",
                [cid] + accessible)
            daily = fetchall_dict(c)
        else:
            div_usage = []; recent_tx = []; daily = []
    else:
        # company_admin sees full company
        c.execute("""SELECT d.name as division_name, d.id as division_id,
                       COUNT(ct.id) as operations,
                       COALESCE(SUM(ct.credits_used),0) as credits_used
                FROM divisions d LEFT JOIN credit_transactions ct ON ct.division_id=d.id AND ct.company_id=%s
                WHERE d.company_id=%s GROUP BY d.id, d.name ORDER BY credits_used DESC""", (cid, cid))
        div_usage = fetchall_dict(c)
        c.execute("""SELECT ct.*, u.full_name as user_name, d.name as division_name
            FROM credit_transactions ct
            LEFT JOIN users u ON ct.user_id=u.id
            LEFT JOIN divisions d ON ct.division_id=d.id
            WHERE ct.company_id=%s ORDER BY ct.created_at DESC LIMIT 20""", (cid,))
        recent_tx = fetchall_dict(c)
        c.execute("""SELECT DATE(created_at) as day, SUM(credits_used) as used
            FROM credit_transactions WHERE company_id=%s AND created_at >= NOW()-INTERVAL '14 days'
            GROUP BY day ORDER BY day""", (cid,))
        daily = fetchall_dict(c)

    c.execute("SELECT * FROM credit_requests WHERE company_id=%s ORDER BY created_at DESC LIMIT 10", (cid,))
    my_requests = fetchall_dict(c)
    conn.close()
    return render_template("index.html", page="credits", user=user, creds=creds,
        div_usage=div_usage, recent_tx=recent_tx, daily=daily, my_requests=my_requests)


@router.route("/credits/request", methods=["POST"])
@login_required
def credits_request():
    user = get_user(session["user_id"])
    if user["role"] not in ("company_admin", "nsm"):
        flash("Access denied")
        return redirect(url_for("credits_dashboard"))
    cid     = user["company_id"]
    amount  = int(request.form.get("amount", 0) or 0)
    message = request.form.get("message", "").strip()
    if amount <= 0:
        flash("Enter a valid credit amount")
        return redirect(url_for("credits_dashboard"))
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT INTO credit_requests (company_id,requested_by,credits_requested,message) VALUES (%s,%s,%s,%s)",
              (cid, user["id"], amount, message))
    conn.commit()
    conn.close()
    flash(f"✓ Credit request for {amount} credits submitted.")
    return redirect(url_for("credits_dashboard"))


@router.route("/api/credits-status")
@login_required
def api_credits_status():
    cid = session.get("company_id", 0)
    return jsonify(get_company_credits(cid))


@router.route("/company/register/<token>", methods=["GET", "POST"])
def company_register(token):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM registration_links WHERE token=%s AND status='active'", (token,))
    link = fetchone_dict(c)
    if not link:
        conn.close()
        return render_template("index.html", page="reg_link_invalid")
    try:
        from datetime import datetime as _dt
        if _dt.fromisoformat(link["expires_at"]) < _dt.now():
            c.execute("UPDATE registration_links SET status='expired' WHERE token=%s", (token,))
            conn.commit()
            conn.close()
            return render_template("index.html", page="reg_link_invalid", expired=True)
    except Exception:
        pass

    if request.method == "POST":
        company_name = request.form.get("company_name", "").strip()
        short_name   = request.form.get("short_name", "").strip().upper()[:4]
        admin_name   = request.form.get("admin_name", "").strip()
        admin_mobile = request.form.get("admin_mobile", "").strip().replace(" ", "").replace("-", "")
        admin_email  = request.form.get("admin_email", "").strip()
        admin_pw     = request.form.get("password", "").strip()
        admin_pw_cfm = request.form.get("confirm_password", "").strip()
        city         = request.form.get("city", "").strip()
        state        = request.form.get("state", "").strip()
        errs = []
        if not company_name: errs.append("Company name is required")
        if not short_name or len(short_name) < 2: errs.append("Short name must be 2–4 characters")
        if not admin_name:   errs.append("Admin full name is required")
        if not admin_mobile or not admin_mobile.isdigit() or len(admin_mobile) != 10:
            errs.append("Admin mobile must be 10 digits")
        if not admin_pw or len(admin_pw) < 8: errs.append("Password must be at least 8 characters")
        if admin_pw != admin_pw_cfm: errs.append("Passwords do not match")
        if errs:
            for e in errs:
                flash(e)
            conn.close()
            return render_template("index.html", page="company_register", token=token, form=request.form)
        code = make_company_code(short_name)
        if not code:
            flash("Could not generate unique code. Try a different short name.")
            conn.close()
            return render_template("index.html", page="company_register", token=token, form=request.form)
        try:
            c.execute("""INSERT INTO companies
                (name,code,short_name,city,state,contact_person,contact_mobile,
                 contact_email,plan,status,max_users)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'demo','active',50)""",
                (company_name, code, short_name, city, state, admin_name, admin_mobile, admin_email))
            c.execute("SELECT id FROM companies WHERE code=%s", (code,))
            cid = c.fetchone()[0]
            for div in ["Sales Division", "Marketing Division", "OTC Division"]:
                c.execute("INSERT INTO divisions (company_id,name,code,status) VALUES (%s,%s,%s,'active')",
                          (cid, div, div.split()[0].upper()[:4]))
            admin_un = f"admin.{code.lower()}"
            c.execute("""INSERT INTO users
                (company_id,username,password,full_name,role,status,joined_date,employee_id,mobile,email)
                VALUES (%s,%s,%s,%s,'company_admin','active',%s,%s,%s,%s)""",
                (cid, admin_un, hash_pw(admin_pw), admin_name,
                 datetime.now().strftime("%Y-%m-%d"), f"{code}-ADM", admin_mobile, admin_email))
            c.execute("INSERT INTO credits (company_id,total_credits,used_credits,plan) VALUES (%s,100,0,'demo')", (cid,))
            c.execute("UPDATE registration_links SET status='used',used_by_company_id=%s WHERE token=%s", (cid, token))
            conn.commit()
            conn.close()
            return render_template("index.html", page="company_reg_success",
                                   company_name=company_name, code=code,
                                   admin_username=admin_un, admin_password=admin_pw)
        except Exception as e:
            conn.rollback()
            flash(f"Company name already exists: {e}")
            conn.close()
            return render_template("index.html", page="company_register", token=token, form=request.form)
    conn.close()
    return render_template("index.html", page="company_register", token=token, form={})


@router.route("/forgot-password", methods=["GET","POST"])
def forgot_password():
    """
    SECURITY NOTE: this used to update the password directly as soon as
    someone submitted a matching company_code + mobile number -- neither of
    which is actually secret (company codes are shared internally, mobile
    numbers are often known/guessable), so this was a full account-takeover
    vector with zero identity verification. It now creates a PENDING request
    that the company's admin/NSM must explicitly approve (see
    /team/password-resets below) before the password actually changes --
    the same human-in-the-loop pattern this app already uses for new-user
    registrations. The response message is deliberately the same whether or
    not the company code / mobile actually matched anything, so this can't
    be used to enumerate valid company codes or registered mobile numbers.
    """
    GENERIC_MSG = ("If those details match an active account, a password reset "
                   "request has been sent to your company admin for approval. "
                   "You'll be able to log in with the new password once it's approved.")
    if request.method == "POST":
        co_code = request.form.get("company_code","").strip().upper()
        mobile  = request.form.get("mobile","").strip()
        new_pw  = request.form.get("new_password","").strip()
        cfm     = request.form.get("confirm_password","").strip()
        if not co_code or not mobile or not new_pw:
            flash("All fields are required")
            return render_template("index.html", page="forgot_password")
        if new_pw != cfm:
            flash("Passwords do not match")
            return render_template("index.html", page="forgot_password")
        if len(new_pw) < 8:
            flash("Minimum 8 characters required")
            return render_template("index.html", page="forgot_password")

        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT id FROM companies WHERE UPPER(code)=%s AND status='active'", (co_code,))
        co = fetchone_dict(c)
        u = None
        if co:
            c.execute("SELECT id FROM users WHERE company_id=%s AND mobile=%s AND status='active'",
                      (co["id"], mobile))
            u = fetchone_dict(c)
        if co and u:
            c.execute("""INSERT INTO password_reset_requests
                (user_id, company_id, new_password_hash, requested_ip)
                VALUES (%s,%s,%s,%s)""",
                (u["id"], co["id"], hash_pw(new_pw), request.remote_addr or ""))
            conn.commit()
            # Notify a company_admin/NSM so they see it without having to poll
            c.execute("""SELECT id FROM users WHERE company_id=%s
                         AND role IN ('company_admin','nsm') AND status='active' LIMIT 1""", (co["id"],))
            admin_row = c.fetchone()
            if admin_row:
                push_notification(co["id"], admin_row[0], "company_admin", "password_reset_request",
                    "Password Reset Request",
                    f"A password reset was requested for a user with mobile {mobile}. Review it under Team → Password Resets.",
                    ref_id=u["id"], ref_type="user")
                conn.commit()
        # Same message either way -- don't reveal whether co_code/mobile matched anything.
        conn.close()
        flash(GENERIC_MSG)
        return redirect(url_for("login"))
    return render_template("index.html", page="forgot_password")


@router.route("/team/password-resets/count")
@admin_required
def team_password_resets_count():
    user = get_user(session["user_id"])
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM password_reset_requests WHERE company_id=%s AND status='pending'",
              (user["company_id"],))
    count = c.fetchone()[0]
    conn.close()
    return jsonify({"pending": count})


@router.route("/team/password-resets")
@admin_required
def team_password_resets():
    user = get_user(session["user_id"])
    conn = get_db(); c = conn.cursor()
    c.execute("""SELECT pr.id, pr.created_at, pr.status, u.full_name, u.mobile, u.username, u.role
        FROM password_reset_requests pr JOIN users u ON pr.user_id=u.id
        WHERE pr.company_id=%s AND pr.status='pending' ORDER BY pr.created_at DESC""",
        (user["company_id"],))
    pending = fetchall_dict(c)
    conn.close()
    return render_template("index.html", page="password_resets", pending=pending)


@router.route("/team/password-resets/<int:req_id>/approve", methods=["POST"])
@admin_required
def team_password_reset_approve(req_id):
    admin_user = get_user(session["user_id"])
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT * FROM password_reset_requests WHERE id=%s AND company_id=%s",
              (req_id, admin_user["company_id"]))
    req = fetchone_dict(c)
    if not req or req["status"] != "pending":
        conn.close()
        flash("Request not found or already handled")
        return redirect(url_for("team_password_resets"))
    c.execute("UPDATE users SET password=%s WHERE id=%s", (req["new_password_hash"], req["user_id"]))
    c.execute("""UPDATE password_reset_requests SET status='approved',
        reviewed_by=%s, reviewed_at=%s WHERE id=%s""",
        (admin_user["id"], datetime.now().isoformat(), req_id))
    conn.commit()
    conn.close()
    log_activity("approve_password_reset", "user", req["user_id"],
                 f"Password reset approved by {admin_user['full_name']}")
    flash("✓ Password reset approved -- the user can now log in with their new password.")
    return redirect(url_for("team_password_resets"))


@router.route("/team/password-resets/<int:req_id>/reject", methods=["POST"])
@admin_required
def team_password_reset_reject(req_id):
    admin_user = get_user(session["user_id"])
    conn = get_db(); c = conn.cursor()
    c.execute("""UPDATE password_reset_requests SET status='rejected',
        reviewed_by=%s, reviewed_at=%s WHERE id=%s AND company_id=%s""",
        (admin_user["id"], datetime.now().isoformat(), req_id, admin_user["company_id"]))
    conn.commit()
    conn.close()
    flash("Password reset request rejected")
    return redirect(url_for("team_password_resets"))


@router.route("/api/admin/upload/<int:up_id>/delete", methods=["POST"])
@login_required
def admin_delete_upload(up_id):
    """
    Admin rejects/deletes a wrong upload -- Level-3 check.
    Data is SOFT-DELETED: marked rejected with reason, kept for audit trail.
    Team members can see why their upload was rejected.
    """
    user = get_user(session["user_id"])
    if user["role"] not in ("company_admin","nsm","zsm","rsm","asm"):
        return jsonify({"error":"Access denied"}), 403
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM uploads WHERE id=%s AND company_id=%s", (up_id, user["company_id"]))
    up = fetchone_dict(c)
    if not up:
        conn.close()
        return jsonify({"error":"Not found"}), 404
    data = request.json if request.is_json else {}
    reason  = (data.get("reason","") or request.form.get("reason","")).strip()
    rej_type = data.get("rejection_type","wrong_document")
    if not reason:
        conn.close()
        return jsonify({"error":"Rejection reason is required"}), 400
    # Soft-delete: mark as rejected, keep all data for audit
    c.execute("""UPDATE uploads
        SET status='rejected', rejection_reason=%s, rejection_type=%s,
            rejected_by=%s, rejected_at=%s
        WHERE id=%s""",
        (reason, rej_type, user["id"], datetime.now().isoformat(), up_id))
    conn.commit()
    conn.close()
    log_activity("reject_upload","upload",up_id,
        f"Rejected by {user['full_name']} ({user['role']}): {reason}")
    return jsonify({"success":True, "message":f"Upload marked as rejected: {reason}"})


@router.route("/api/admin/upload/<int:up_id>/restore", methods=["POST"])
@login_required
def admin_restore_upload(up_id):
    """Restore a wrongly rejected upload back to done status."""
    user = get_user(session["user_id"])
    if user["role"] not in ("company_admin","nsm"):
        return jsonify({"error":"Access denied"}), 403
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM uploads WHERE id=%s AND company_id=%s AND status='rejected'",
              (up_id, user["company_id"]))
    up = fetchone_dict(c)
    if not up:
        conn.close()
        return jsonify({"error":"Not found or not rejected"}), 404
    c.execute("""UPDATE uploads
        SET status='done', rejection_reason=NULL, rejection_type=NULL,
            rejected_by=NULL, rejected_at=NULL
        WHERE id=%s""", (up_id,))
    conn.commit()
    conn.close()
    log_activity("restore_upload","upload",up_id,f"Restored by {user['full_name']}")
    return jsonify({"success":True})
