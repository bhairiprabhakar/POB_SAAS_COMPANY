"""
SUPER ADMIN ROUTES
Ported from the original Flask app.py -- business logic and SQL are
unchanged; only the routing layer (Flask -> FastAPI-compat) differs.
See app/compat.py for how `request`/`session`/`render_template`/etc. work.
"""
import os
import csv
import json
import base64
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
from ..config import UPLOAD_FOLDER
from ..helpers import (
    ROLES, ROLE_LABELS, ROLE_RANK, _slugify, get_upload_path,
    generate_company_code, make_company_code, generate_reg_token,
    get_company_credits, consume_credit, log_activity, get_user, get_company,
    get_accessible_ids, require_parent_for_role, classify_period,
    _parse_stmt_date, check_period_duplicate, get_current_quarter,
    save_extraction, get_sa_vcounts, push_notification, get_agent_company_ids,
    _build_verification_excel, notify_registration,
)
from ..auth import login_required, admin_required, superadmin_required, agent_required
from ..extraction import call_ocr_extraction, _compute_file_hash, _compute_content_fingerprint
from ..security_middleware import account_lockout
from saas.upload_validation import IMAGE_KINDS, UploadValidationError, is_safe_svg, validate_upload

router = FlaskCompatRouter()

@router.route("/superadmin")
@router.route("/superadmin/")
def sa_index():
    if "super_admin_id" in session:
        return redirect(url_for("sa_dashboard"))
    return redirect(url_for("sa_login"))


@router.route("/superadmin/login", methods=["GET", "POST"])
def sa_login():
    if request.method == "POST":
        un = request.form.get("username", "").strip()
        pw = request.form.get("password", "").strip()

        lockout_key = f"sa:{un.lower()}"
        locked_secs = account_lockout.is_locked(lockout_key)
        if locked_secs:
            flash(f"Too many failed attempts for this account. Try again in {locked_secs // 60 + 1} minute(s).")
            return render_template("superadmin.html", page="sa_login")

        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM super_admins WHERE username=%s AND status='active'", (un,))
        sa = fetchone_dict(c)
        if sa and not verify_pw(sa.get("password"), pw):
            sa = None
        elif sa and _looks_like_legacy_sha256(sa.get("password")):
            # Opportunistically upgrade to a salted hash now that we know the password
            c.execute("UPDATE super_admins SET password=%s WHERE id=%s", (hash_pw(pw), sa["id"]))
            conn.commit()
        conn.close()
        if sa:
            account_lockout.record_success(lockout_key)
            session["super_admin_id"]    = sa["id"]
            session["super_admin_name"]  = sa["full_name"]
            session["super_admin_role"]  = sa.get("role", "superadmin")
            session["agent_company_ids"] = [
                int(x.strip()) for x in (sa.get("assigned_company_ids") or "").split(",")
                if x.strip().isdigit()
            ]
            if sa.get("role") == "agent":
                return redirect(url_for("agent_dashboard"))
            return redirect(url_for("sa_dashboard"))
        account_lockout.record_failure(lockout_key)
        flash("Invalid credentials")
    return render_template("superadmin.html", page="sa_login")


@router.route("/superadmin/logout")
def sa_logout():
    for k in ["super_admin_id","super_admin_name","super_admin_role","agent_company_ids"]:
        session.pop(k, None)
    return redirect(url_for("sa_login"))


@router.route("/superadmin/reg-links")
@superadmin_required
def sa_reg_links():
    conn = get_db()
    c = conn.cursor()
    c.execute("""SELECT rl.*, co.name as company_name, co.code as company_code
        FROM registration_links rl
        LEFT JOIN companies co ON rl.used_by_company_id=co.id
        ORDER BY rl.created_at DESC LIMIT 50""")
    links = fetchall_dict(c)
    conn.close()
    return render_template("superadmin.html", page="sa_reg_links", links=links,
        base_url=request.host_url.rstrip('/'),
        sa_name=session.get("super_admin_name", "Super Admin"),
        vcounts=get_sa_vcounts())


@router.route("/superadmin/reg-links/create", methods=["POST"])
@superadmin_required
def sa_reg_link_create():
    from datetime import datetime as _dt, timedelta as _td
    expires_days = int(request.form.get("expires_days", 7))
    token   = generate_reg_token()
    expires = (_dt.now() + _td(days=expires_days)).isoformat()
    sa_id   = session.get("super_admin_id", 0)
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT INTO registration_links (token,created_by,status,expires_at) VALUES (%s,%s,%s,%s)",
              (token, sa_id, "active", expires))
    conn.commit()
    conn.close()
    flash(f"✓ Registration link created -- valid for {expires_days} days")
    return redirect(url_for("sa_reg_links"))


@router.route("/superadmin/reg-links/revoke/<int:lid>", methods=["POST"])
@superadmin_required
def sa_reg_link_revoke(lid):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE registration_links SET status='revoked' WHERE id=%s", (lid,))
    conn.commit()
    conn.close()
    flash("✓ Link revoked")
    return redirect(url_for("sa_reg_links"))


@router.route("/superadmin/credits")
@superadmin_required
def sa_credits():
    conn = get_db()
    c = conn.cursor()
    c.execute("""SELECT co.id, co.name, co.code, cr.total_credits, cr.used_credits,
               cr.plan, cr.updated_at,
               (cr.total_credits - cr.used_credits) as remaining
        FROM companies co
        LEFT JOIN credits cr ON cr.company_id=co.id
        ORDER BY co.name""")
    companies_credits = fetchall_dict(c)
    c.execute("""SELECT cr.*, co.name as company_name, co.code as company_code,
               u.full_name as requester_name
        FROM credit_requests cr
        JOIN companies co ON cr.company_id=co.id
        JOIN users u ON cr.requested_by=u.id
        WHERE cr.status='pending'
        ORDER BY cr.created_at DESC""")
    pending_requests = fetchall_dict(c)
    conn.close()
    return render_template("superadmin.html", page="sa_credits",
        companies_credits=companies_credits, pending_requests=pending_requests,
        sa_name=session.get("super_admin_name", "Super Admin"),
        vcounts=get_sa_vcounts())


@router.route("/superadmin/credits/allocate", methods=["POST"])
@superadmin_required
def sa_credits_allocate():
    cid    = int(request.form.get("company_id", 0))
    amount = int(request.form.get("amount", 0))
    plan   = request.form.get("plan", "demo")
    if not cid or amount <= 0:
        flash("Invalid parameters")
        return redirect(url_for("sa_credits"))
    conn = get_db()
    c = conn.cursor()
    c.execute("""INSERT INTO credits (company_id,total_credits,used_credits,plan,updated_at)
        VALUES (%s,%s,0,%s,%s)
        ON CONFLICT (company_id) DO UPDATE
        SET total_credits=credits.total_credits+%s, plan=%s, updated_at=%s""",
        (cid, amount, plan, datetime.now().isoformat(),
         amount, plan, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    flash(f"✓ {amount} credits allocated")
    return redirect(url_for("sa_credits"))


@router.route("/superadmin/credits/approve/<int:rid>", methods=["POST"])
@superadmin_required
def sa_credits_approve(rid):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM credit_requests WHERE id=%s", (rid,))
    req = fetchone_dict(c)
    if not req:
        conn.close()
        flash("Not found")
        return redirect(url_for("sa_credits"))
    cid = req["company_id"]
    amt = req["credits_requested"]
    c.execute("""INSERT INTO credits (company_id,total_credits,used_credits,plan,updated_at)
        VALUES (%s,%s,0,'paid',%s)
        ON CONFLICT (company_id) DO UPDATE
        SET total_credits=credits.total_credits+%s, updated_at=%s""",
        (cid, amt, datetime.now().isoformat(), amt, datetime.now().isoformat()))
    c.execute("UPDATE credit_requests SET status='approved',reviewed_by=%s,reviewed_at=%s WHERE id=%s",
              (session.get("super_admin_id", 0), datetime.now().isoformat(), rid))
    conn.commit()
    conn.close()
    flash(f"✓ Credit request approved -- {amt} credits added")
    return redirect(url_for("sa_credits"))


@router.route("/superadmin/api/verification-pending-count")
@superadmin_required
def sa_verification_pending_count_api():
    """Lightweight polling endpoint so the sidebar's Verification Queue badge
    updates live instead of only refreshing on full page navigation."""
    counts = get_sa_vcounts()
    return jsonify({"pending": counts.get("pending", 0)})


@router.route("/superadmin/dashboard")
@superadmin_required
def sa_dashboard():
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT co.*,
               COUNT(DISTINCT u.id) as user_count,
               COUNT(DISTINCT up.id) as upload_count,
               COUNT(DISTINCT CASE WHEN r.status='pending' THEN r.id END) as pending_regs,
               COUNT(DISTINCT d.id) as division_count,
               COALESCE(cr.total_credits, 0) as total_credits,
               COALESCE(cr.used_credits, 0) as used_credits,
               COALESCE(cr.total_credits,0)-COALESCE(cr.used_credits,0) as remaining_credits
        FROM companies co
        LEFT JOIN users u ON u.company_id=co.id AND u.status='active'
        LEFT JOIN uploads up ON up.company_id=co.id
        LEFT JOIN registrations r ON r.company_id=co.id
        LEFT JOIN divisions d ON d.company_id=co.id AND d.status='active'
        LEFT JOIN credits cr ON cr.company_id=co.id
        GROUP BY co.id, cr.total_credits, cr.used_credits
        ORDER BY co.name""")
    companies = fetchall_dict(c)
    c.execute("SELECT COUNT(*) as n FROM companies WHERE status='active'")
    total_companies = c.fetchone()[0]
    c.execute("SELECT COUNT(*) as n FROM users WHERE status='active'")
    total_users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) as n FROM uploads")
    total_uploads = c.fetchone()[0]
    c.execute("SELECT COUNT(*) as n FROM registrations WHERE status='pending'")
    pending_regs = c.fetchone()[0]
    c.execute("SELECT COUNT(*) as n FROM credit_requests WHERE status='pending'")
    pending_credits = c.fetchone()[0]
    c.execute("SELECT COUNT(*) as n FROM registration_links WHERE status='active' AND expires_at>%s",
              (datetime.now().isoformat(),))
    reg_links_active = c.fetchone()[0]
    conn.close()
    return render_template("superadmin.html", page="sa_dashboard",
        companies=companies, total_companies=total_companies, total_users=total_users,
        total_uploads=total_uploads, pending_regs=pending_regs,
        pending_credits=pending_credits, reg_links_active=reg_links_active,
        sa_name=session.get("super_admin_name", "Super Admin"),
        vcounts=get_sa_vcounts())


@router.route("/superadmin/companies/add", methods=["POST"])
@superadmin_required
def sa_company_add():
    d = request.form
    name = d.get("name", "").strip()
    code = d.get("code", "").strip().upper()
    if not code:
        code = generate_company_code()
    if not name:
        flash("Company name is required")
        return redirect(url_for("sa_dashboard"))
    admin_pw = d.get("admin_password", "admin@123").strip()
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("""INSERT INTO companies
            (name,code,address,city,state,gst_number,drug_license,
             contact_person,contact_mobile,contact_email,plan,max_users,created_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0)""",
            (name, code, d.get("address", ""), d.get("city", ""), d.get("state", ""),
             d.get("gst_number", ""), d.get("drug_license", ""),
             d.get("contact_person", ""), d.get("contact_mobile", ""),
             d.get("contact_email", ""), d.get("plan", "basic"),
             int(d.get("max_users", 100))))
        c.execute("SELECT id FROM companies WHERE code=%s", (code,))
        cid = c.fetchone()[0]
        for div in ["Sales Division", "Marketing Division", "OTC Division"]:
            c.execute("INSERT INTO divisions (company_id,name,code,status) VALUES (%s,%s,%s,'active')",
                      (cid, div, div.split()[0].upper()[:4]))
        un = f"admin.{code.lower()}"
        c.execute("""INSERT INTO users
            (company_id,username,password,full_name,role,status,joined_date,employee_id)
            VALUES (%s,%s,%s,'Company Admin','company_admin','active',%s,%s)""",
            (cid, un, hash_pw(admin_pw), datetime.now().strftime("%Y-%m-%d"), f"{code}-ADM"))
        conn.commit()
        flash(f"✓ Company '{name}' created -- Code: {code} -- Admin: {un} / {admin_pw}")
    except Exception as e:
        conn.rollback()
        flash(f"Company name or code already exists: {e}")
    finally:
        conn.close()
    return redirect(url_for("sa_dashboard"))


@router.route("/superadmin/companies/edit/<int:cid>", methods=["POST"])
@superadmin_required
def sa_company_edit(cid):
    d = request.form
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("""UPDATE companies SET
            name=%s,address=%s,city=%s,state=%s,gst_number=%s,
            drug_license=%s,contact_person=%s,contact_mobile=%s,contact_email=%s,
            website=%s,plan=%s,status=%s,max_users=%s,notes=%s
            WHERE id=%s""",
            (d.get("name", "").strip(),
             d.get("address", ""), d.get("city", ""), d.get("state", ""),
             d.get("gst_number", ""), d.get("drug_license", ""),
             d.get("contact_person", ""), d.get("contact_mobile", ""),
             d.get("contact_email", ""), d.get("website", ""),
             d.get("plan", "basic"), d.get("status", "active"),
             int(d.get("max_users", 100) or 100), d.get("notes", ""), cid))
        conn.commit()
        flash("✓ Company updated successfully")
    except Exception as e:
        conn.rollback()
        flash(f"Error updating company: {e}")
    finally:
        conn.close()
    return redirect(url_for("sa_company_detail", cid=cid))


@router.route("/superadmin/company/<int:cid>")
@superadmin_required
def sa_company_detail(cid):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM companies WHERE id=%s", (cid,))
    company = fetchone_dict(c)
    c.execute("""SELECT d.*,COUNT(u.id) as member_count FROM divisions d
        LEFT JOIN users u ON u.division_id=d.id
        WHERE d.company_id=%s GROUP BY d.id""", (cid,))
    divisions = fetchall_dict(c)
    c.execute("SELECT * FROM zones WHERE company_id=%s", (cid,))
    zones = fetchall_dict(c)
    c.execute("""SELECT u.*,p.full_name as parent_name,d.name as div_name
        FROM users u LEFT JOIN users p ON u.parent_id=p.id
        LEFT JOIN divisions d ON u.division_id=d.id
        WHERE u.company_id=%s ORDER BY
            CASE u.role WHEN 'company_admin' THEN 1 WHEN 'nsm' THEN 2 WHEN 'zsm' THEN 3
            WHEN 'rsm' THEN 4 WHEN 'asm' THEN 5 ELSE 6 END, u.full_name""", (cid,))
    users = fetchall_dict(c)
    c.execute("""SELECT h.*,f.full_name as from_name,t.full_name as to_name
        FROM handovers h JOIN users f ON h.from_user_id=f.id JOIN users t ON h.to_user_id=t.id
        WHERE h.company_id=%s ORDER BY h.done_at DESC LIMIT 20""", (cid,))
    handovers = fetchall_dict(c)
    c.execute("""SELECT COUNT(DISTINCT u.id) as users, COUNT(DISTINCT up.id) as uploads,
               COALESCE(SUM(e.invoice_net),0) as revenue
        FROM companies co
        LEFT JOIN users u ON u.company_id=co.id AND u.status='active'
        LEFT JOIN uploads up ON up.company_id=co.id
        LEFT JOIN extractions e ON e.upload_id=up.id
        WHERE co.id=%s""", (cid,))
    stats = fetchone_dict(c)

    # Recent uploads for this company with verification status
    c.execute("""SELECT u.*,
               us.full_name as uploader_name, us.role as uploader_role, us.area,
               d.name as division_name,
               e.stockist_name, e.invoice_net, e.total_quantity,
               e.statement_from_date, e.statement_to_date,
               mv.id as mv_id, mv.status as mv_status
        FROM uploads u
        JOIN users us ON u.user_id=us.id
        LEFT JOIN divisions d ON u.division_id=d.id
        LEFT JOIN extractions e ON e.upload_id=u.id
        LEFT JOIN manual_verifications mv ON mv.upload_id=u.id
        WHERE u.company_id=%s
        ORDER BY u.upload_date DESC LIMIT 50""", (cid,))
    recent_uploads = fetchall_dict(c)

    conn.close()
    return render_template("superadmin.html", page="sa_company_detail",
        company=company or {}, divisions=divisions, zones=zones, users=users,
        handovers=handovers, stats=stats or {}, recent_uploads=recent_uploads,
        sa_name=session.get("super_admin_name", "Super Admin"),
        vcounts=get_sa_vcounts())


@router.route("/superadmin/divisions/add", methods=["POST"])
@superadmin_required
def sa_division_add():
    d = request.form
    cid = d.get("company_id")
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("INSERT INTO divisions (company_id,name,code,description,status) VALUES (%s,%s,%s,%s,'active')",
            (cid, d.get("name"), d.get("code", "").upper(), d.get("description", "")))
        conn.commit()
        flash("Division added")
    except Exception as e:
        conn.rollback()
        flash(str(e))
    finally:
        conn.close()
    return redirect(url_for("sa_company_detail", cid=cid))


@router.route("/superadmin/divisions/edit/<int:did>", methods=["POST"])
@superadmin_required
def sa_division_edit(did):
    d = request.form
    cid = d.get("company_id")
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("UPDATE divisions SET name=%s,code=%s,description=%s,status=%s WHERE id=%s",
            (d.get("name"), d.get("code", "").upper(), d.get("description", ""),
             d.get("status", "active"), did))
        conn.commit()
        flash("Division updated")
    except Exception as e:
        conn.rollback()
        flash(str(e))
    finally:
        conn.close()
    return redirect(url_for("sa_company_detail", cid=cid))


@router.route("/superadmin/companies/logo/<int:cid>", methods=["POST"])
@superadmin_required
def sa_company_logo(cid):
    if "logo" not in request.files:
        flash("No file selected")
        return redirect(url_for("sa_company_detail", cid=cid))
    f = request.files["logo"]
    if not f.filename:
        flash("No file")
        return redirect(url_for("sa_company_detail", cid=cid))
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in {".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp"}:
        flash("Only image files allowed")
        return redirect(url_for("sa_company_detail", cid=cid))
    file_bytes = f.read()
    f.seek(0)
    if ext == ".svg":
        if not is_safe_svg(file_bytes):
            flash("SVG file rejected: contains scripts or unsafe content")
            return redirect(url_for("sa_company_detail", cid=cid))
    else:
        try:
            validate_upload(file_bytes, filename=f.filename or "", allowed_kinds=IMAGE_KINDS,
                            max_size=5 * 1024 * 1024)
        except UploadValidationError as exc:
            flash(f"Invalid image file: {exc}")
            return redirect(url_for("sa_company_detail", cid=cid))
    logo_dir = os.path.join(UPLOAD_FOLDER, "logos")
    os.makedirs(logo_dir, exist_ok=True)
    fname = f"logo_{cid}{ext}"
    f.save(os.path.join(logo_dir, fname))
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE companies SET logo_url=%s WHERE id=%s", (f"logos/{fname}", cid))
    conn.commit()
    conn.close()
    flash("Logo uploaded successfully")
    return redirect(url_for("sa_company_detail", cid=cid))


@router.route("/superadmin/zones/add", methods=["POST"])
@superadmin_required
def sa_zone_add():
    d = request.form
    cid = d.get("company_id")
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("INSERT INTO zones (company_id,name,code,states) VALUES (%s,%s,%s,%s)",
            (cid, d.get("name"), d.get("code", "").upper(), d.get("states", "")))
        conn.commit()
        flash("Zone added")
    except Exception as e:
        conn.rollback()
        flash(str(e))
    finally:
        conn.close()
    return redirect(url_for("sa_company_detail", cid=cid))


@router.route("/superadmin/bulk-upload", methods=["GET", "POST"])
@superadmin_required
def sa_bulk_upload():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id,name,code FROM companies WHERE status='active' ORDER BY name")
    companies_list = fetchall_dict(c)

    cid_sel = request.args.get("company_id") or request.form.get("company_id","")
    divisions_for_company = []
    managers_for_company  = []
    if cid_sel:
        c.execute("SELECT id,name,code FROM divisions WHERE company_id=%s AND status='active' ORDER BY name", (cid_sel,))
        divisions_for_company = fetchall_dict(c)
        c.execute("""SELECT id,full_name,role,employee_id FROM users
            WHERE company_id=%s AND status='active' AND role!='mr'
            ORDER BY CASE role WHEN 'company_admin' THEN 1 WHEN 'nsm' THEN 2 WHEN 'zsm' THEN 3 WHEN 'rsm' THEN 4 ELSE 5 END, full_name""",
            (cid_sel,))
        managers_for_company = fetchall_dict(c)

    bulk_result = None
    if request.method == "POST":
        cid  = request.form.get("company_id", "").strip()
        file = request.files.get("csv_file")
        if not cid or not file:
            flash("Select company and CSV file")
            conn.close()
            return render_template("superadmin.html", page="sa_bulk",
                companies=companies_list, divisions=divisions_for_company,
                managers=managers_for_company, cid_sel=cid_sel,
                sa_name=session.get("super_admin_name", "Super Admin"),
                vcounts=get_sa_vcounts())

        raw = file.read().decode("utf-8-sig", errors="ignore").replace("\r\n","\n").replace("\r","\n")
        reader = csv.DictReader(StringIO(raw))
        created, updated_count, skipped, errors = 0, 0, 0, []

        # "update_existing" checkbox — if ticked, update instead of skip on duplicate
        do_update = request.form.get("update_existing") == "1"

        # Build division name→id lookup for this company
        c.execute("SELECT id,name,code FROM divisions WHERE company_id=%s AND status='active'", (cid,))
        div_lookup = {}
        for d in fetchall_dict(c):
            div_lookup[d["name"].lower()] = d["id"]
            div_lookup[(d["code"] or "").lower()] = d["id"]

        # Build manager mobile/name→id lookup
        c.execute("SELECT id,full_name,mobile,employee_id FROM users WHERE company_id=%s AND status='active' AND role!='mr'", (cid,))
        mgr_lookup = {}
        for m in fetchall_dict(c):
            mgr_lookup[(m["mobile"] or "").strip()] = m["id"]
            mgr_lookup[(m["employee_id"] or "").strip().lower()] = m["id"]
            mgr_lookup[m["full_name"].lower()] = m["id"]

        for i, row in enumerate(reader, 2):
            fn   = (row.get("full_name","") or row.get("name","")).strip()
            mob  = (row.get("mobile","")).strip().replace(" ","").replace("-","")
            un   = (row.get("username","") or mob).strip()
            pw   = (row.get("password","") or "Pass@123").strip()
            role = (row.get("role","mr")).strip().lower()
            if role not in ROLES:
                role = "mr"
            emp  = (row.get("employee_id","")).strip()
            reg  = (row.get("region","")).strip()
            area = (row.get("area","")).strip()
            ter  = (row.get("territory","")).strip()
            email = (row.get("email","")).strip()
            div_raw = (row.get("division","") or row.get("division_name","") or row.get("division_id","")).strip()
            mgr_raw = (row.get("reporting_manager","") or row.get("manager","") or row.get("parent","")).strip()

            # Mandatory field validation
            miss = []
            if not fn:   miss.append("full_name")
            if not mob:  miss.append("mobile")
            if not un:   miss.append("username")
            if not div_raw: miss.append("division")
            if miss:
                errors.append(f"Row {i} ({fn or 'unknown'}): missing {', '.join(miss)}")
                skipped += 1
                continue
            if not mob.isdigit() or len(mob) != 10:
                errors.append(f"Row {i} ({fn}): mobile must be 10 digits — got '{mob}'")
                skipped += 1
                continue

            # Resolve division
            div_id = None
            if div_raw.isdigit():
                div_id = int(div_raw)
            else:
                div_id = div_lookup.get(div_raw.lower())
            if not div_id:
                errors.append(f"Row {i} ({fn}): division '{div_raw}' not found — valid: {', '.join(list(dict.fromkeys(v for v in div_lookup.keys() if v))[:6])}")
                skipped += 1
                continue

            # Resolve reporting manager
            parent_id = None
            if mgr_raw:
                parent_id = (mgr_lookup.get(mgr_raw.strip()) or
                             mgr_lookup.get(mgr_raw.strip().lower()) or
                             mgr_lookup.get(mgr_raw.strip().lstrip("0")))

            if not require_parent_for_role(role, parent_id):
                errors.append(f"Row {i} ({fn}): reporting_manager required for role '{role}'")
                skipped += 1
                continue

            # ── Pre-check for existing user (mobile or username in this company) ──
            c.execute("""SELECT id, full_name, mobile FROM users
                         WHERE company_id=%s AND (mobile=%s OR username=%s)
                         LIMIT 1""", (cid, mob, un))
            existing = c.fetchone()

            if existing:
                if do_update:
                    # Update existing user's details
                    try:
                        c.execute("""UPDATE users SET
                            full_name=%s, role=%s, region=%s, area=%s, territory=%s,
                            employee_id=%s, email=%s, division_id=%s, parent_id=%s
                            WHERE id=%s""",
                            (fn, role, reg, area, ter, emp, email, div_id, parent_id, existing[0]))
                        conn.commit()
                        updated_count += 1
                    except Exception as ex:
                        conn.rollback()
                        errors.append(f"Row {i} ({fn}): update failed — {str(ex)[:80]}")
                        skipped += 1
                else:
                    errors.append(f"Row {i} ({fn}): mobile '{mob}' already exists — tick 'Update existing users' to overwrite")
                    skipped += 1
                continue

            # ── Insert new user ───────────────────────────────────────────────
            try:
                c.execute("""INSERT INTO users
                    (company_id,username,password,full_name,role,region,area,territory,
                     employee_id,mobile,email,division_id,parent_id,status,joined_date)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',%s)""",
                    (cid, un, hash_pw(pw), fn, role, reg, area, ter,
                     emp, mob, email, div_id, parent_id,
                     datetime.now().strftime("%Y-%m-%d")))
                conn.commit()
                created += 1
                push_notification(int(cid), None, "company_admin", "user_created",
                    "New User Added via Bulk Upload",
                    f"{fn} ({role.upper()}) added to {div_raw}.", ref_type="user")
            except Exception as ex:
                conn.rollback()
                # Parse constraint name for friendly message
                ex_str = str(ex)
                if "mobile" in ex_str or "users_compan" in ex_str:
                    errors.append(f"Row {i} ({fn}): mobile '{mob}' already registered in this company")
                elif "username" in ex_str:
                    errors.append(f"Row {i} ({fn}): username '{un}' already taken")
                elif "employee_id" in ex_str:
                    errors.append(f"Row {i} ({fn}): employee_id '{emp}' already exists")
                else:
                    errors.append(f"Row {i} ({fn}): {ex_str[:80]}")
                skipped += 1

        bulk_result = {"created": created, "updated": updated_count, "skipped": skipped, "errors": errors}
        parts = []
        if created:      parts.append(f"✓ {created} created")
        if updated_count: parts.append(f"✓ {updated_count} updated")
        if skipped:      parts.append(f"{skipped} skipped")
        if parts:
            flash(" · ".join(parts))
        else:
            flash("No changes made.")

    conn.close()
    return render_template("superadmin.html", page="sa_bulk",
        companies=companies_list, divisions=divisions_for_company,
        managers=managers_for_company, cid_sel=cid_sel,
        bulk_result=bulk_result,
        sa_name=session.get("super_admin_name", "Super Admin"),
        vcounts=get_sa_vcounts())


@router.route("/superadmin/bulk-upload/template")
@superadmin_required
def sa_bulk_template():
    cid = request.args.get("company_id","")
    conn = get_db()
    c = conn.cursor()
    div_names = ["Sales Division"]
    mgr_names = [""]
    if cid:
        c.execute("SELECT name FROM divisions WHERE company_id=%s AND status='active' ORDER BY name", (cid,))
        div_names = [r["name"] for r in fetchall_dict(c)] or div_names
        c.execute("SELECT full_name FROM users WHERE company_id=%s AND status='active' AND role!='mr' ORDER BY full_name LIMIT 5", (cid,))
        mgr_names = [r["full_name"] for r in fetchall_dict(c)] or mgr_names
    conn.close()
    lines = ["full_name,username,mobile,employee_id,password,role,division,reporting_manager,region,area,territory,email"]
    # Sample rows using actual division/manager names from company
    d1 = div_names[0] if div_names else "Sales Division"
    d2 = div_names[1] if len(div_names)>1 else d1
    m1 = mgr_names[0] if mgr_names else ""
    lines.append(f"Raj Kumar,mr_raj,9855555551,MR001,Pass@123,mr,{d1},{m1},North,Delhi-East,East Delhi,raj@example.com")
    lines.append(f"Priya Sharma,asm_priya,9833333333,ASM001,Pass@123,asm,{d2},,North,Delhi,,priya@example.com")
    lines.append(f"Anil Verma,rsm_anil,9877777777,RSM001,Pass@123,rsm,{d1},,South,,,anil@example.com")
    buf = BytesIO("\n".join(lines).encode())
    buf.seek(0)
    return send_file(buf, mimetype="text/csv", as_attachment=True, download_name="user_template.csv")


@router.route("/superadmin/password-reset", methods=["POST"])
@superadmin_required
def sa_password_reset():
    user_id = request.form.get("user_id")
    new_pw  = request.form.get("new_password", "pass@123").strip()
    cid     = request.form.get("company_id")
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE users SET password=%s WHERE id=%s", (hash_pw(new_pw), user_id))
    conn.commit()
    conn.close()
    flash(f"Password reset to: {new_pw}")
    return redirect(url_for("sa_company_detail", cid=cid))




# ═══════════════════════════════════════════════════════════════════════════════
# NOTIFICATIONS HELPER -- get_sa_vcounts() / push_notification() now live in
# app/helpers.py (imported above) since agent_routes.py needs them too.
# ═══════════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════════
# SUPERADMIN -- MANUAL VERIFICATION QUEUE
# ═══════════════════════════════════════════════════════════════════════════════

@router.route("/superadmin/verification")
@superadmin_required
def sa_verification():
    """List all uploads pending / completed manual verification, across all companies."""
    status_f = request.args.get("status", "all")
    cid_f    = request.args.get("company_id", "")
    conn = get_db()
    c = conn.cursor()

    where = "WHERE 1=1"
    params = []
    if status_f and status_f != "all":
        where += " AND mv.status=%s"
        params.append(status_f)
    if cid_f:
        where += " AND u.company_id=%s"
        params.append(cid_f)

    c.execute(f"""
        SELECT mv.*,
               u.original_filename, u.stored_filename, u.upload_date, u.user_id as uploader_id,
               up_user.full_name as uploader_name, up_user.area as uploader_area,
               co.name as company_name, co.code as company_code,
               d.name as division_name,
               e.stockist_name, e.statement_from_date, e.statement_to_date,
               e.invoice_net, e.total_quantity,
               (SELECT COUNT(*) FROM parties p WHERE p.extraction_id=e.id) as party_count,
               vby.full_name as verified_by_name
        FROM manual_verifications mv
        JOIN uploads u ON mv.upload_id=u.id
        JOIN users up_user ON u.user_id=up_user.id
        JOIN companies co ON u.company_id=co.id
        LEFT JOIN divisions d ON mv.division_id=d.id
        LEFT JOIN extractions e ON e.upload_id=u.id
        LEFT JOIN users vby ON mv.verified_by=vby.id
        {where}
        ORDER BY mv.created_at DESC LIMIT 100
    """, params)
    verifications = fetchall_dict(c)

    c.execute("SELECT id,name,code FROM companies WHERE status='active' ORDER BY name")
    companies = fetchall_dict(c)

    # Status counts
    c.execute("SELECT status, COUNT(*) as cnt FROM manual_verifications GROUP BY status")
    vcounts = {r["status"]: r["cnt"] for r in fetchall_dict(c)}

    conn.close()
    return render_template("superadmin.html", page="sa_verification",
        verifications=verifications, companies=companies,
        status_f=status_f, cid_f=cid_f, vcounts=vcounts,
        sa_name=session.get("super_admin_name", "Super Admin"))


@router.route("/superadmin/verification/<int:mv_id>/download-excel")
@superadmin_required
def sa_verification_download(mv_id):
    """SA download -- delegates to shared builder."""
    return _build_verification_excel(mv_id)


@router.route("/superadmin/verification/<int:mv_id>/upload-corrections", methods=["POST"])
@superadmin_required
def sa_verification_upload_corrections(mv_id):
    """Upload corrected Excel, apply diffs to DB, mark verification complete."""
    import json as _json
    if "excel_file" not in request.files:
        flash("No file uploaded")
        return redirect(url_for("sa_verification"))

    f = request.files["excel_file"]
    if not f.filename.endswith((".xlsx", ".xls")):
        flash("Only .xlsx files accepted")
        return redirect(url_for("sa_verification"))

    conn = get_db()
    c = conn.cursor()
    c.execute("""SELECT mv.*, e.id as ext_id FROM manual_verifications mv
        JOIN uploads u ON mv.upload_id=u.id
        LEFT JOIN extractions e ON e.upload_id=u.id
        WHERE mv.id=%s""", (mv_id,))
    mv = fetchone_dict(c)
    if not mv:
        conn.close()
        flash("Verification record not found")
        return redirect(url_for("sa_verification"))

    try:
        from openpyxl import load_workbook
        buf = BytesIO(f.read())
        wb = load_workbook(buf, data_only=True)

        corrections = {"parties": [], "items": [], "new_parties": 0, "new_items": 0, "summary": {}}
        party_updates = 0
        item_updates  = 0
        new_parties   = 0
        new_items     = 0

        # ── Party id → extraction_id map (needed for new party inserts) ───────
        c.execute("SELECT id FROM extractions WHERE upload_id=%s", (mv["upload_id"],))
        ext_row = c.fetchone()
        ext_id = ext_row[0] if ext_row else mv.get("ext_id")

        # ── Apply Party corrections (UPDATE existing, INSERT new) ─────────────
        if "Parties" in wb.sheetnames:
            ws_p = wb["Parties"]
            for row in ws_p.iter_rows(min_row=2, values_only=True):
                if not row or all(v is None or str(v).strip() == "" for v in row):
                    continue
                pid   = row[0]
                name  = str(row[1] or "").strip()
                area  = str(row[2] or "").strip()
                ptype = str(row[3] or "chemist").strip()
                dl    = str(row[4] or "").strip()
                gst   = str(row[5] or "").strip()
                if not name:
                    continue
                try:
                    pid_int = int(float(str(pid))) if pid and str(pid).strip() not in ("", "0", "NEW") else 0
                except (ValueError, TypeError):
                    pid_int = 0

                if pid_int > 0:
                    # UPDATE existing party
                    c.execute("""UPDATE parties SET name=%s, area=%s, type=%s,
                        dl_number=%s, gst_number=%s WHERE id=%s""",
                        (name, area, ptype, dl, gst, pid_int))
                    party_updates += 1
                    corrections["parties"].append({"id": pid_int, "action": "updated", "name": name})
                else:
                    # INSERT new party (agent added a missing store)
                    c.execute("""INSERT INTO parties
                        (extraction_id, name, type, area, dl_number, gst_number,
                         total_quantity, total_amount)
                        VALUES (%s,%s,%s,%s,%s,%s,0,0) RETURNING id""",
                        (ext_id, name, ptype, area, dl, gst))
                    new_pid = c.fetchone()[0]
                    new_parties += 1
                    corrections["parties"].append({"id": new_pid, "action": "inserted", "name": name})

        # Build party name→id lookup for new items referencing new parties
        c.execute("SELECT id, name FROM parties WHERE extraction_id=%s", (ext_id,))
        party_name_to_id = {r[1].strip().upper(): r[0] for r in c.fetchall()}

        # ── Apply Item corrections (UPDATE existing, INSERT new) ──────────────
        if "Items" in wb.sheetnames:
            ws_i = wb["Items"]
            for row in ws_i.iter_rows(min_row=2, values_only=True):
                if not row or all(v is None or str(v).strip() == "" for v in row):
                    continue
                iid        = row[0]
                party_ref  = row[1]   # party_id (int) or party name (str for new rows)
                store_name = str(row[2] or "").strip()
                brand      = str(row[3] or "").strip()
                mfg        = str(row[4] or "").strip()
                pack       = str(row[5] or "").strip()
                batch      = str(row[6] or "").strip()
                exp        = str(row[7] or "").strip()
                qty        = int(float(str(row[8] or 0)))
                mrp        = float(str(row[9]  or 0))
                rate       = float(str(row[10] or 0))
                disc       = float(str(row[11] or 0))
                amt        = float(str(row[12] or 0))
                if not brand and qty == 0 and amt == 0:
                    continue

                try:
                    iid_int = int(float(str(iid))) if iid and str(iid).strip() not in ("", "0", "NEW") else 0
                except (ValueError, TypeError):
                    iid_int = 0

                if iid_int > 0:
                    # UPDATE existing item
                    c.execute("""UPDATE items SET brand=%s, mfg=%s, pack=%s,
                        batch_no=%s, expiry=%s, quantity=%s, mrp=%s,
                        unit_rate=%s, discount_percent=%s, final_amount=%s
                        WHERE id=%s""",
                        (brand, mfg, pack, batch, exp, qty, mrp, rate, disc, amt, iid_int))
                    item_updates += 1
                    corrections["items"].append({"id": iid_int, "action": "updated", "brand": brand})
                else:
                    # INSERT new item -- resolve party_id
                    resolved_pid = None
                    try:
                        resolved_pid = int(float(str(party_ref))) if party_ref else None
                    except (ValueError, TypeError):
                        # party_ref might be a store name string
                        resolved_pid = party_name_to_id.get(str(party_ref or "").strip().upper())
                    if not resolved_pid:
                        # Try store_name column
                        resolved_pid = party_name_to_id.get(store_name.upper())
                    if resolved_pid:
                        c.execute("""INSERT INTO items
                            (party_id, brand, mfg, pack, batch_no, expiry,
                             quantity, mrp, unit_rate, discount_percent, final_amount)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                            (resolved_pid, brand, mfg, pack, batch, exp,
                             qty, mrp, rate, disc, amt))
                        new_iid = c.fetchone()[0]
                        new_items += 1
                        corrections["items"].append({"id": new_iid, "action": "inserted", "brand": brand})

        # ── Guard: ext_id must exist before recalc ────────────────────────────
        if not ext_id:
            c.execute("SELECT id FROM extractions WHERE upload_id=%s", (mv["upload_id"],))
            ext_fallback = c.fetchone()
            ext_id = ext_fallback[0] if ext_fallback else None
        if not ext_id:
            conn.rollback()
            conn.close()
            flash("⚠ Extraction data not found -- corrections could not be applied.")
            return redirect(url_for("sa_verification"))
        # ── Recalculate party totals ──────────────────────────────────────────
        c.execute("""UPDATE parties SET
            total_quantity=(SELECT COALESCE(SUM(quantity),0) FROM items WHERE party_id=parties.id),
            total_amount=(SELECT COALESCE(SUM(final_amount),0) FROM items WHERE party_id=parties.id)
            WHERE extraction_id=%s""", (ext_id,))

        # ── Recalculate extraction totals ─────────────────────────────────────
        c.execute("""UPDATE extractions SET
            total_quantity=(SELECT COALESCE(SUM(i.quantity),0) FROM items i JOIN parties p ON i.party_id=p.id WHERE p.extraction_id=extractions.id),
            total_amount=(SELECT COALESCE(SUM(i.final_amount),0) FROM items i JOIN parties p ON i.party_id=p.id WHERE p.extraction_id=extractions.id),
            invoice_net=(SELECT COALESCE(SUM(i.final_amount),0) FROM items i JOIN parties p ON i.party_id=p.id WHERE p.extraction_id=extractions.id),
            net_sale=(SELECT COALESCE(SUM(i.final_amount),0) FROM items i JOIN parties p ON i.party_id=p.id WHERE p.extraction_id=extractions.id)
            WHERE id=%s""", (ext_id,))

        corrections["summary"] = {"party_updates": party_updates, "item_updates": item_updates, "new_parties": new_parties, "new_items": new_items}

        # ── Mark verification complete ────────────────────────────────────────
        c.execute("""UPDATE manual_verifications SET
            status='verified', verified_by=%s, verified_at=%s,
            excel_uploaded_at=%s, corrections_json=%s
            WHERE id=%s""",
            (session.get("super_admin_id", 0), datetime.now().isoformat(),
             datetime.now().isoformat(), _json.dumps(corrections), mv_id))

        # Update upload verification status
        c.execute("UPDATE uploads SET verification_status='verified' WHERE id=%s", (mv["upload_id"],))

        # Notify company admin
        push_notification(
            mv["company_id"], None, "company_admin", "verification_complete",
            "Manual Verification Complete",
            f"Document verification completed. {party_updates} party records and {item_updates} item records updated.",
            ref_id=mv["upload_id"], ref_type="upload"
        )

        conn.commit()
        conn.close()
        added_msg = ""
        if new_parties or new_items:
            added_msg = f" + {new_parties} new parties, {new_items} new items added."
        flash(f"✓ Corrections applied -- {party_updates} parties, {item_updates} items updated.{added_msg} Status: Verified.")
    except Exception as ex:
        conn.rollback()
        conn.close()
        flash(f"Error processing corrections: {ex}")

    return redirect(url_for("sa_verification"))


@router.route("/superadmin/verification/create/<int:upload_id>", methods=["POST"])
@superadmin_required
def sa_verification_create(upload_id):
    """Manually trigger a verification task for an upload."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM uploads WHERE id=%s", (upload_id,))
    up = fetchone_dict(c)
    if not up:
        conn.close()
        return jsonify({"error": "Upload not found"}), 404
    # Check not already pending
    c.execute("SELECT id FROM manual_verifications WHERE upload_id=%s AND status='pending'", (upload_id,))
    if c.fetchone():
        conn.close()
        return jsonify({"error": "Already in verification queue"}), 409
    c.execute("""INSERT INTO manual_verifications
        (upload_id, company_id, division_id, status)
        VALUES (%s, %s, %s, 'pending') RETURNING id""",
        (upload_id, up["company_id"], up["division_id"]))
    mv_id = c.fetchone()[0]
    c.execute("UPDATE uploads SET verification_status='pending_verification' WHERE id=%s", (upload_id,))
    conn.commit()
    conn.close()
    flash(f"✓ Verification task #{mv_id} created")
    return redirect(url_for("sa_verification"))


# ── In-app notifications API ──────────────────────────────────────────────────
@router.route("/api/notifications")
@login_required
def api_notifications():
    user = get_user(session.get("user_id"))
    if not user:
        return jsonify([]), 401
    conn = get_db()
    c = conn.cursor()
    c.execute("""SELECT * FROM notification_log
        WHERE company_id=%s AND (user_id=%s OR recipient_role=%s)
        AND is_read=FALSE ORDER BY created_at DESC LIMIT 20""",
        (user["company_id"], user["id"], user["role"]))
    notes = fetchall_dict(c)
    conn.close()
    return jsonify(notes)


@router.route("/api/notifications/read/<int:nid>", methods=["POST"])
@login_required
def api_notification_read(nid):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE notification_log SET is_read=TRUE WHERE id=%s", (nid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@router.route("/api/notifications/read-all", methods=["POST"])
@login_required
def api_notification_read_all():
    user = get_user(session["user_id"])
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE notification_log SET is_read=TRUE WHERE company_id=%s AND (user_id=%s OR recipient_role=%s)",
              (user["company_id"], user["id"], user["role"]))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ── Notify on registration ────────────────────────────────────────────────────
@router.route("/superadmin/ai-costing/export")
@superadmin_required
def sa_ai_costing_export():
    """Export the AI Costing report (all breakdowns + full per-extraction
    detail, not just the last-100 shown on-page) as Excel."""
    from ..ai.pricing import GEMINI_PRICING
    from ..config import USD_TO_INR_RATE

    days = int(request.args.get("days", "30"))
    conn = get_db()
    c = conn.cursor()

    c.execute("""SELECT model_name,
            COUNT(*) as calls, SUM(input_tokens) as input_tokens, SUM(output_tokens) as output_tokens,
            SUM(thinking_tokens) as thinking_tokens, SUM(cost_usd) as cost_usd, SUM(cost_inr) as cost_inr
        FROM ai_usage_log WHERE created_at >= NOW() - INTERVAL '%s days'
        GROUP BY model_name ORDER BY cost_inr DESC""", (days,))
    by_model = fetchall_dict(c)

    c.execute("""SELECT co.name as company_name, co.code as company_code,
            COUNT(a.id) as calls, COALESCE(SUM(a.input_tokens),0) as input_tokens,
            COALESCE(SUM(a.output_tokens),0) as output_tokens, COALESCE(SUM(a.cost_usd),0) as cost_usd,
            COALESCE(SUM(a.cost_inr),0) as cost_inr
        FROM ai_usage_log a JOIN companies co ON a.company_id = co.id
        WHERE a.created_at >= NOW() - INTERVAL '%s days'
        GROUP BY co.id, co.name, co.code ORDER BY cost_inr DESC""", (days,))
    by_company = fetchall_dict(c)

    c.execute("""SELECT DATE(a.created_at) as day,
            COALESCE(SUM(a.cost_inr),0) as cost_inr, COUNT(*) as calls
        FROM ai_usage_log a WHERE a.created_at >= NOW() - INTERVAL '%s days'
        GROUP BY DATE(a.created_at) ORDER BY day""", (days,))
    daily = fetchall_dict(c)

    # Full detail -- every single extraction in range, not capped at 100
    c.execute("""SELECT a.created_at, u.original_filename, co.name as company_name, co.code as company_code,
            a.model_name, a.input_tokens, a.output_tokens, a.thinking_tokens, a.chunk_count,
            a.cost_usd, a.cost_inr
        FROM ai_usage_log a
        LEFT JOIN uploads u ON a.upload_id = u.id
        LEFT JOIN companies co ON a.company_id = co.id
        WHERE a.created_at >= NOW() - INTERVAL '%s days'
        ORDER BY a.created_at DESC""", (days,))
    detail = fetchall_dict(c)
    conn.close()

    wb = openpyxl.Workbook()

    ws1 = wb.active
    ws1.title = "Summary"
    ws1.append(["AI Costing Report", f"Last {days} days", f"Exchange rate used: ₹{USD_TO_INR_RATE}/USD"])
    ws1.append([])
    ws1.append(["Model", "Calls", "Input Tokens", "Output Tokens", "Thinking Tokens", "Cost (USD)", "Cost (INR)"])
    for m in by_model:
        ws1.append([m["model_name"], m["calls"], m["input_tokens"] or 0, m["output_tokens"] or 0,
                    m["thinking_tokens"] or 0, m["cost_usd"] or 0, m["cost_inr"] or 0])
    ws1.append([])
    ws1.append(["Current pricing (USD / 1M tokens)"])
    for model, p in GEMINI_PRICING.items():
        ws1.append([model, f"in ${p['input']}", f"out ${p['output']}"])

    ws2 = wb.create_sheet("By Company")
    ws2.append(["Company", "Code", "Calls", "Input Tokens", "Output Tokens", "Cost (USD)", "Cost (INR)"])
    for co in by_company:
        ws2.append([co["company_name"], co["company_code"], co["calls"], co["input_tokens"] or 0,
                    co["output_tokens"] or 0, co["cost_usd"] or 0, co["cost_inr"] or 0])

    ws3 = wb.create_sheet("Daily Trend")
    ws3.append(["Date", "Calls", "Cost (INR)"])
    for d in daily:
        ws3.append([str(d["day"]), d["calls"], d["cost_inr"] or 0])

    ws4 = wb.create_sheet("Per-Extraction Detail")
    ws4.append(["When", "Document", "Company", "Company Code", "Model Used",
                "Input Tokens", "Output Tokens", "Thinking Tokens", "Chunks", "Cost (USD)", "Cost (INR)"])
    for r in detail:
        ws4.append([
            str(r["created_at"]), r["original_filename"] or "", r["company_name"] or "", r["company_code"] or "",
            r["model_name"], r["input_tokens"] or 0, r["output_tokens"] or 0, r["thinking_tokens"] or 0,
            r["chunk_count"], r["cost_usd"] or 0, r["cost_inr"] or 0,
        ])

    output = BytesIO()
    wb.save(output)
    output.seek(0)
    return send_file(output,
        as_attachment=True,
        download_name=f"ai_costing_report_last_{days}_days.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@router.route("/superadmin/ai-costing")
@superadmin_required
def sa_ai_costing():
    """
    AI extraction cost dashboard -- token usage and computed USD/INR cost,
    logged per extraction call in ai_usage_log (see app/ai/gemini_extraction.py
    and app/ai/pricing.py). "Model selection"-aware: cost is computed against
    whichever model actually served each call (model_name column), so if this
    app is ever extended to let companies pick between multiple Gemini
    models, costs stay accurate per-model rather than one blended rate.
    """
    from ..ai.pricing import GEMINI_PRICING
    from ..config import USD_TO_INR_RATE

    days = int(request.args.get("days", "30"))
    conn = get_db()
    c = conn.cursor()

    c.execute("""SELECT
            COALESCE(SUM(input_tokens),0) as total_input,
            COALESCE(SUM(output_tokens),0) as total_output,
            COALESCE(SUM(thinking_tokens),0) as total_thinking,
            COALESCE(SUM(cost_usd),0) as total_usd,
            COALESCE(SUM(cost_inr),0) as total_inr,
            COUNT(*) as call_count,
            COALESCE(SUM(chunk_count),0) as total_chunks
        FROM ai_usage_log
        WHERE created_at >= NOW() - INTERVAL '%s days'""", (days,))
    totals = fetchone_dict(c) or {}

    # Per-model breakdown -- shows exactly which model handled how many
    # documents and at what token/cost cost, per the model-routing logic in
    # app/ai/gemini_extraction.py's _select_model()
    c.execute("""SELECT model_name,
            COUNT(*) as calls, SUM(input_tokens) as input_tokens, SUM(output_tokens) as output_tokens,
            SUM(thinking_tokens) as thinking_tokens,
            SUM(cost_usd) as cost_usd, SUM(cost_inr) as cost_inr
        FROM ai_usage_log
        WHERE created_at >= NOW() - INTERVAL '%s days'
        GROUP BY model_name ORDER BY cost_inr DESC""", (days,))
    by_model = fetchall_dict(c)

    # Per-company breakdown
    c.execute("""SELECT co.id, co.name, co.code,
            COUNT(a.id) as calls,
            COALESCE(SUM(a.input_tokens),0) as input_tokens,
            COALESCE(SUM(a.output_tokens),0) as output_tokens,
            COALESCE(SUM(a.cost_usd),0) as cost_usd,
            COALESCE(SUM(a.cost_inr),0) as cost_inr
        FROM ai_usage_log a JOIN companies co ON a.company_id = co.id
        WHERE a.created_at >= NOW() - INTERVAL '%s days'
        GROUP BY co.id, co.name, co.code
        ORDER BY cost_inr DESC LIMIT 50""", (days,))
    by_company = fetchall_dict(c)

    # Daily trend for the chart
    c.execute("""SELECT DATE(created_at) as day,
            COALESCE(SUM(cost_inr),0) as cost_inr, COUNT(*) as calls
        FROM ai_usage_log
        WHERE created_at >= NOW() - INTERVAL '%s days'
        GROUP BY DATE(created_at) ORDER BY day""", (days,))
    daily = fetchall_dict(c)

    # Recent individual extractions -- per-document model/token/cost detail
    c.execute("""SELECT a.id, a.upload_id, a.model_name, a.input_tokens, a.output_tokens,
            a.thinking_tokens, a.chunk_count, a.cost_inr, a.created_at,
            u.original_filename, co.name as company_name, co.code as company_code
        FROM ai_usage_log a
        LEFT JOIN uploads u ON a.upload_id = u.id
        LEFT JOIN companies co ON a.company_id = co.id
        WHERE a.created_at >= NOW() - INTERVAL '%s days'
        ORDER BY a.created_at DESC LIMIT 100""", (days,))
    recent = fetchall_dict(c)
    conn.close()

    avg_cost_inr = (totals.get("total_inr", 0) / totals["call_count"]) if totals.get("call_count") else 0

    return render_template("superadmin.html", page="sa_ai_costing", cur="ai_costing",
        days=days, totals=totals, by_model=by_model, by_company=by_company,
        daily=daily, recent=recent, avg_cost_inr=avg_cost_inr, usd_to_inr_rate=USD_TO_INR_RATE,
        pricing_table=GEMINI_PRICING)


# ── AI Models: per-category model routing + model pricing ────────────────────
# Both are stored in the app DB (ai_model_routing / ai_model_pricing) and read
# fresh on every extraction, so changes apply with NO restart -- see
# app/ai/model_registry.py for the resolution logic.

@router.route("/superadmin/ai-models")
@superadmin_required
def sa_ai_models():
    """Model Settings (which Gemini model handles each file category) +
    Model Pricing (editable per-model USD rates / add newly shipped models)."""
    from ..ai import model_registry
    return render_template("superadmin.html", page="sa_ai_models", cur="ai_models",
        categories=model_registry.CATEGORIES,
        routing=model_registry.get_routing(),
        env_defaults=model_registry.env_defaults(),
        models=model_registry.model_options(),
        sa_name=session.get("super_admin_name", "Super Admin"))


@router.route("/superadmin/api/ai-models/routing", methods=["POST"])
@superadmin_required
def sa_ai_models_routing_save():
    """Save the per-category model overrides. An empty choice (the "Default"
    option) deletes that category's override so it follows .env again."""
    from ..ai import model_registry
    data = request.form.to_dict(flat=True)
    overrides = {cat["key"]: (data.get(f"routing[{cat['key']}]") or "").strip()
                 for cat in model_registry.CATEGORIES}
    try:
        model_registry.save_routing(overrides, session.get("super_admin_id"))
        flash("✓ Model settings saved -- applies to the next document processed after saving.")
    except Exception as exc:
        flash(f"⚠ Error saving model settings: {exc}")
    return redirect("/superadmin/ai-models")


@router.route("/superadmin/api/ai-models/pricing", methods=["POST"])
@superadmin_required
def sa_ai_models_pricing_save():
    """Save the whole pricing table (one form posts every row) plus an
    optional new-model block. Blank price = fall back to default pricing."""
    from ..ai import model_registry
    data = request.form.to_dict(flat=True)
    try:
        rows = model_registry.parse_form_rows(data)
        for mid, label, inp, out in rows:
            model_registry.upsert_pricing(mid, label, inp, out)
        new_mid = (data.get("new_model_id") or "").strip()
        if new_mid:
            model_registry.upsert_pricing(
                new_mid,
                (data.get("new_model_label") or "").strip(),
                data.get("new_model_input") or "",
                data.get("new_model_output") or "")
        n = len(rows) + (1 if new_mid else 0)
        flash(f"✓ Pricing saved -- {n} model(s) updated. New prices apply to the next document processed.")
    except Exception as exc:
        flash(f"⚠ Error saving pricing: {exc}")
    return redirect("/superadmin/ai-models")


@router.route("/superadmin/api/ai-models/pricing/delete", methods=["POST"])
@superadmin_required
def sa_ai_models_pricing_delete():
    """Remove a model from pricing. Refuses if the model is currently selected
    in any routing category (see model_registry.delete_pricing)."""
    from ..ai import model_registry
    mid = (request.form.get("model_id") or "").strip()
    ok, reason = model_registry.delete_pricing(mid)
    if ok:
        flash(f"✓ Removed '{mid}' from pricing.")
    else:
        flash(f"⚠ {reason}")
    return redirect("/superadmin/ai-models")


@router.route("/superadmin/analytics")
@superadmin_required
def sa_analytics():
    """Super admin analytics -- cross-company upload activity, verification stats, user creation."""
    conn = get_db()
    c = conn.cursor()

    # ── Per-company summary ───────────────────────────────────────────────────
    c.execute("""
        SELECT co.id, co.name, co.code, co.status,
               COUNT(DISTINCT u.id) FILTER (WHERE u.status='active') as active_users,
               COUNT(DISTINCT up.id) as total_uploads,
               COUNT(DISTINCT up.id) FILTER (WHERE up.status='done') as done_uploads,
               COUNT(DISTINCT up.id) FILTER (WHERE up.status='rejected') as rejected_uploads,
               COUNT(DISTINCT up.id) FILTER (WHERE up.verification_status='verified') as verified_uploads,
               COUNT(DISTINCT up.id) FILTER (WHERE up.verification_status='pending_verification') as pending_verif,
               COALESCE(SUM(e.invoice_net) FILTER (WHERE up.status='done'), 0) as total_revenue,
               COALESCE(cr.total_credits, 0) as total_credits,
               COALESCE(cr.used_credits, 0) as used_credits
        FROM companies co
        LEFT JOIN users u ON u.company_id = co.id
        LEFT JOIN uploads up ON up.company_id = co.id
        LEFT JOIN extractions e ON e.upload_id = up.id
        LEFT JOIN credits cr ON cr.company_id = co.id
        GROUP BY co.id, co.name, co.code, co.status, cr.total_credits, cr.used_credits
        ORDER BY total_uploads DESC
    """)
    company_stats = fetchall_dict(c)

    c.execute("""
        SELECT co.name as company_name, co.code as company_code,
               COUNT(mv.id) as verified_docs,
               COALESCE(SUM(e.invoice_net), 0) as verified_net
        FROM manual_verifications mv
        JOIN uploads u ON mv.upload_id = u.id
        JOIN companies co ON u.company_id = co.id
        LEFT JOIN extractions e ON e.upload_id = u.id
        WHERE mv.status = 'verified'
        GROUP BY co.name, co.code
        ORDER BY verified_net DESC
    """)
    company_verified_net = fetchall_dict(c)

    # ── Per-user upload activity (top uploaders across all companies) ─────────
    c.execute("""
        SELECT u.full_name, u.role, u.area, u.mobile,
               co.name as company_name, co.code as company_code,
               d.name as division_name,
               COUNT(up.id) as total_uploads,
               COUNT(up.id) FILTER (WHERE up.status='done') as done_uploads,
               COUNT(up.id) FILTER (WHERE up.status='rejected') as rejected_uploads,
               COUNT(up.id) FILTER (WHERE up.verification_status='verified') as verified_uploads,
               COALESCE(SUM(e.invoice_net) FILTER (WHERE up.status='done'), 0) as revenue,
               MAX(up.upload_date) as last_upload
        FROM users u
        JOIN companies co ON u.company_id = co.id
        LEFT JOIN divisions d ON u.division_id = d.id
        LEFT JOIN uploads up ON up.user_id = u.id
        LEFT JOIN extractions e ON e.upload_id = up.id
        WHERE u.status = 'active'
        GROUP BY u.id, u.full_name, u.role, u.area, u.mobile,
                 co.name, co.code, d.name
        HAVING COUNT(up.id) > 0
        ORDER BY total_uploads DESC
        LIMIT 50
    """)
    user_activity = fetchall_dict(c)

    # ── Monthly upload trend across all companies ─────────────────────────────
    c.execute("""
        SELECT TO_CHAR(upload_date, 'YYYY-MM') as month,
               COUNT(*) as total,
               COUNT(*) FILTER (WHERE status='done') as done,
               COUNT(*) FILTER (WHERE status='rejected') as rejected,
               COUNT(*) FILTER (WHERE verification_status='verified') as verified
        FROM uploads
        WHERE upload_date >= NOW() - INTERVAL '12 months'
        GROUP BY month ORDER BY month
    """)
    monthly_trend = fetchall_dict(c)

    # ── Verification staff performance (detailed) ────────────────────────────
    c.execute("""
        SELECT sa.id as agent_id, sa.full_name as verifier, sa.username,
               COUNT(mv.id)                                                                                    as total_verified,
               COUNT(mv.id) FILTER (WHERE mv.verified_at::timestamptz::date = CURRENT_DATE)                    as today,
               COUNT(mv.id) FILTER (WHERE mv.verified_at::timestamptz >= NOW() - INTERVAL '7 days')            as this_week,
               COUNT(mv.id) FILTER (WHERE mv.verified_at::timestamptz >= NOW() - INTERVAL '30 days')           as this_month,
               COUNT(mv.id) FILTER (WHERE mv.status = 'rejected')                                              as total_rejected,
               MIN(mv.verified_at) as first_verified,
               MAX(mv.verified_at) as last_verified,
               ROUND(AVG(EXTRACT(EPOCH FROM (mv.verified_at::timestamptz -
                   u.upload_date::timestamptz))/3600)::numeric, 1)                                             as avg_hours_to_verify
        FROM super_admins sa
        LEFT JOIN manual_verifications mv ON mv.verified_by = sa.id
        LEFT JOIN uploads u ON mv.upload_id = u.id
        WHERE sa.role = 'agent'
        GROUP BY sa.id, sa.full_name, sa.username
        ORDER BY total_verified DESC
    """)
    verifier_stats = fetchall_dict(c)

    # ── Per-agent recent verification log (last 50 across all agents) ─────────
    c.execute("""
        SELECT sa.full_name as agent_name, sa.username as agent_username,
               mv.id as mv_id, mv.verified_at, mv.status as mv_status,
               co.name as company_name, co.code as company_code,
               e.stockist_name, e.statement_from_date, e.statement_to_date,
               e.invoice_net, e.doc_type,
               u.original_filename,
               up_user.full_name as uploader_name
        FROM manual_verifications mv
        JOIN super_admins sa ON mv.verified_by = sa.id
        JOIN uploads u ON mv.upload_id = u.id
        JOIN companies co ON u.company_id = co.id
        JOIN users up_user ON u.user_id = up_user.id
        LEFT JOIN extractions e ON e.upload_id = u.id
        WHERE mv.status IN ('verified','rejected') AND mv.verified_at IS NOT NULL
        ORDER BY mv.verified_at DESC
        LIMIT 100
    """)
    agent_verif_log = fetchall_dict(c)

    # ── Agent company coverage (which companies each agent has verified for) ──
    c.execute("""
        SELECT sa.full_name as agent_name, co.name as company_name,
               COUNT(mv.id) as count,
               COALESCE(SUM(e.invoice_net),0) as net_value
        FROM manual_verifications mv
        JOIN super_admins sa ON mv.verified_by = sa.id
        JOIN uploads u ON mv.upload_id = u.id
        JOIN companies co ON u.company_id = co.id
        LEFT JOIN extractions e ON e.upload_id = u.id
        WHERE mv.status = 'verified'
        GROUP BY sa.full_name, co.name
        ORDER BY sa.full_name, count DESC
    """)
    agent_company_coverage = fetchall_dict(c)

    # ── Overall totals ────────────────────────────────────────────────────────
    c.execute("""
        SELECT COUNT(DISTINCT co.id) as companies,
               COUNT(DISTINCT u.id) FILTER (WHERE u.status='active') as active_users,
               COUNT(DISTINCT up.id) as total_uploads,
               COUNT(DISTINCT up.id) FILTER (WHERE up.status='done') as done_uploads,
               COUNT(DISTINCT up.id) FILTER (WHERE up.status='rejected') as rejected_uploads,
               COUNT(DISTINCT up.id) FILTER (WHERE up.verification_status='verified') as verified_uploads,
               COUNT(DISTINCT mv.id) FILTER (WHERE mv.status='pending') as pending_verif,
               COALESCE(SUM(e.invoice_net) FILTER (WHERE up.status='done'), 0) as total_revenue
        FROM companies co
        LEFT JOIN users u ON u.company_id = co.id
        LEFT JOIN uploads up ON up.company_id = co.id
        LEFT JOIN extractions e ON e.upload_id = up.id
        LEFT JOIN manual_verifications mv ON mv.upload_id = up.id
    """)
    totals = fetchone_dict(c)

    conn.close()
    return render_template("superadmin.html", page="sa_analytics",
        company_stats=company_stats, company_verified_net=company_verified_net,
        user_activity=user_activity, monthly_trend=monthly_trend,
        verifier_stats=verifier_stats, agent_verif_log=agent_verif_log,
        agent_company_coverage=agent_company_coverage,
        totals=totals or {},
        sa_name=session.get("super_admin_name", "Super Admin"),
        vcounts=get_sa_vcounts())


@router.route("/superadmin/analytics/export-agent-report")
@superadmin_required
def sa_analytics_export_agent_report():
    """Export agent verification analytics as Excel."""
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT sa.full_name as verifier, sa.username,
               COUNT(mv.id) as total_verified,
               COUNT(mv.id) FILTER (WHERE mv.verified_at::timestamptz::date = CURRENT_DATE) as today,
               COUNT(mv.id) FILTER (WHERE mv.verified_at::timestamptz >= NOW() - INTERVAL '7 days') as this_week,
               COUNT(mv.id) FILTER (WHERE mv.verified_at::timestamptz >= NOW() - INTERVAL '30 days') as this_month,
               COUNT(mv.id) FILTER (WHERE mv.status = 'rejected') as total_rejected,
               MIN(mv.verified_at) as first_verified,
               MAX(mv.verified_at) as last_verified,
               ROUND(AVG(EXTRACT(EPOCH FROM (mv.verified_at::timestamptz - u.upload_date::timestamptz))/3600)::numeric, 1) as avg_hours_to_verify
        FROM super_admins sa
        LEFT JOIN manual_verifications mv ON mv.verified_by = sa.id
        LEFT JOIN uploads u ON mv.upload_id = u.id
        WHERE sa.role = 'agent'
        GROUP BY sa.id, sa.full_name, sa.username
        ORDER BY total_verified DESC
    """)
    verifier_stats = fetchall_dict(c)

    c.execute("""
        SELECT sa.full_name as agent_name, co.name as company_name,
               COUNT(mv.id) as doc_count,
               COALESCE(SUM(e.invoice_net),0) as net_value
        FROM manual_verifications mv
        JOIN super_admins sa ON mv.verified_by = sa.id
        JOIN uploads u ON mv.upload_id = u.id
        JOIN companies co ON u.company_id = co.id
        LEFT JOIN extractions e ON e.upload_id = u.id
        WHERE mv.status = 'verified'
        GROUP BY sa.full_name, co.name
        ORDER BY sa.full_name, doc_count DESC
    """)
    agent_company_coverage = fetchall_dict(c)

    c.execute("""
        SELECT co.name as company_name, co.code as company_code,
               COUNT(mv.id) as verified_docs,
               COALESCE(SUM(e.invoice_net),0) as verified_net
        FROM manual_verifications mv
        JOIN uploads u ON mv.upload_id = u.id
        JOIN companies co ON u.company_id = co.id
        LEFT JOIN extractions e ON e.upload_id = u.id
        WHERE mv.status = 'verified'
        GROUP BY co.name, co.code
        ORDER BY verified_net DESC
    """)
    company_verified_net = fetchall_dict(c)

    c.execute("""
        SELECT sa.full_name as agent_name, sa.username as agent_username,
               co.name as company_name, co.code as company_code,
               u.original_filename as document_name,
               e.stockist_name, e.statement_from_date, e.statement_to_date,
               e.invoice_net, mv.status as verification_status,
               mv.verified_at
        FROM manual_verifications mv
        JOIN super_admins sa ON mv.verified_by = sa.id
        JOIN uploads u ON mv.upload_id = u.id
        JOIN companies co ON u.company_id = co.id
        LEFT JOIN extractions e ON e.upload_id = u.id
        WHERE mv.verified_at IS NOT NULL
        ORDER BY sa.full_name, mv.verified_at DESC
    """)
    detailed_verifications = fetchall_dict(c)
    conn.close()

    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = 'Agent Performance'
    ws1.append(['Agent Name', 'Username', 'Total Verified', 'Today', 'This Week', 'This Month', 'Rejected', 'Avg Hours to Verify', 'First Verified', 'Last Verified'])
    for v in verifier_stats:
        ws1.append([
            v['verifier'], v['username'], v['total_verified'], v['today'], v['this_week'],
            v['this_month'], v['total_rejected'], v['avg_hours_to_verify'],
            v['first_verified'] or '', v['last_verified'] or ''
        ])

    ws2 = wb.create_sheet('Agent Company Coverage')
    ws2.append(['Agent Name', 'Company Name', 'Docs Verified', 'Net Value'])
    for row in agent_company_coverage:
        ws2.append([row['agent_name'], row['company_name'], row['doc_count'], row['net_value']])

    ws3 = wb.create_sheet('Company Verified Net')
    ws3.append(['Company Name', 'Company Code', 'Verified Docs', 'Verified Net'])
    for row in company_verified_net:
        ws3.append([row['company_name'], row['company_code'], row['verified_docs'], row['verified_net']])

    ws4 = wb.create_sheet('Verification History')
    ws4.append([
        'Agent Name', 'Username', 'Company', 'Company Code',
        'Document', 'Stockist', 'Period From', 'Period To',
        'Invoice Net', 'Status', 'Verified At'
    ])
    for row in detailed_verifications:
        ws4.append([
            row['agent_name'], row['agent_username'], row['company_name'], row['company_code'],
            row['document_name'] or row['stockist_name'] or '',
            row['stockist_name'] or '', row['statement_from_date'] or '', row['statement_to_date'] or '',
            row['invoice_net'] or 0, row['verification_status'], row['verified_at'] or ''
        ])

    output = BytesIO()
    wb.save(output)
    output.seek(0)
    return send_file(output,
        as_attachment=True,
        download_name='agent_verification_report.xlsx',
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@router.route("/superadmin/verifiers")
@superadmin_required
def sa_verifiers():
    """Manage super admin / verifier accounts."""
    conn = get_db()
    c = conn.cursor()
    c.execute("""SELECT id, username, full_name, email, status, role,
               assigned_company_ids, created_at
               FROM super_admins ORDER BY role, created_at DESC""")
    verifiers = fetchall_dict(c)
    c.execute("SELECT id, name, code FROM companies WHERE status='active' ORDER BY name")
    all_companies = fetchall_dict(c)
    c.execute("""
        SELECT co.name as company_name, co.code as company_code,
               COUNT(mv.id) as verified_docs,
               COALESCE(SUM(e.invoice_net),0) as verified_net
        FROM manual_verifications mv
        JOIN uploads u ON mv.upload_id = u.id
        JOIN companies co ON u.company_id = co.id
        LEFT JOIN extractions e ON e.upload_id = u.id
        WHERE mv.status = 'verified'
        GROUP BY co.name, co.code
        ORDER BY verified_net DESC
    """)
    company_verified_net = fetchall_dict(c)
    conn.close()
    return render_template("superadmin.html", page="sa_verifiers",
        verifiers=verifiers, all_companies=all_companies,
        company_verified_net=company_verified_net,
        sa_name=session.get("super_admin_name", "Super Admin"),
        vcounts=get_sa_vcounts())


@router.route("/superadmin/verifiers/add", methods=["POST"])
@superadmin_required
def sa_verifier_add():
    """Create a new super admin / verifier account."""
    un    = request.form.get("username", "").strip().lower()
    fn    = request.form.get("full_name", "").strip()
    email = request.form.get("email", "").strip()
    pw    = request.form.get("password", "").strip()
    role = request.form.get("role","agent").strip()
    if not un or not fn or not pw:
        flash("Username, full name and password are required")
        return redirect(url_for("sa_verifiers"))
    if len(pw) < 10:
        flash("Password must be at least 10 characters for security")
        return redirect(url_for("sa_verifiers"))
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("""INSERT INTO super_admins
            (username, password, full_name, email, status, role, assigned_company_ids)
            VALUES (%s,%s,%s,%s,'active',%s,'')""",
                  (un, hash_pw(pw), fn, email, role))
        conn.commit()
        flash(f"✓ Verifier account '{un}' created. Share credentials securely -- never via chat/email in plain text.")
    except Exception as ex:
        conn.rollback()
        flash(f"Username already exists: {ex}")
    finally:
        conn.close()
    return redirect(url_for("sa_verifiers"))


@router.route("/superadmin/verifiers/toggle/<int:vid>", methods=["POST"])
@superadmin_required
def sa_verifier_toggle(vid):
    """Enable / disable a verifier account."""
    if vid == session.get("super_admin_id"):
        flash("Cannot deactivate your own account")
        return redirect(url_for("sa_verifiers"))
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT status FROM super_admins WHERE id=%s", (vid,))
    row = c.fetchone()
    if row:
        new_status = "inactive" if row[0] == "active" else "active"
        c.execute("UPDATE super_admins SET status=%s WHERE id=%s", (new_status, vid))
        conn.commit()
        flash(f"✓ Account {'activated' if new_status=='active' else 'deactivated'}")
    conn.close()
    return redirect(url_for("sa_verifiers"))


@router.route("/superadmin/verifiers/reset-password/<int:vid>", methods=["POST"])
@superadmin_required
def sa_verifier_reset_pw(vid):
    """Reset a verifier's password."""
    new_pw = request.form.get("new_password", "").strip()
    if len(new_pw) < 10:
        flash("Password must be at least 10 characters")
        return redirect(url_for("sa_verifiers"))
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE super_admins SET password=%s WHERE id=%s", (hash_pw(new_pw), vid))
    conn.commit()
    conn.close()
    flash("✓ Password reset. Share new password via a secure channel only.")
    return redirect(url_for("sa_verifiers"))


@router.route("/superadmin/tasks")
@superadmin_required
def sa_tasks():
    """Task assignment dashboard for superadmin."""
    conn = get_db()
    c = conn.cursor()
    
    # Get all agents
    c.execute("SELECT id, full_name, username FROM super_admins WHERE role='agent' AND status='active' ORDER BY full_name")
    agents = fetchall_dict(c)
    
    # Get unassigned tasks
    c.execute("""
        SELECT mv.id, mv.created_at,
               u.original_filename, u.upload_date,
               co.name as company_name, co.code as company_code,
               d.name as division_name,
               e.stockist_name, e.statement_from_date, e.statement_to_date,
               e.invoice_net, e.doc_type,
               (SELECT COUNT(*) FROM parties p WHERE p.extraction_id=e.id) as party_count,
               up_user.full_name as uploader_name
        FROM manual_verifications mv
        JOIN uploads u ON mv.upload_id=u.id
        JOIN companies co ON u.company_id=co.id
        LEFT JOIN divisions d ON d.id=u.division_id
        LEFT JOIN extractions e ON e.upload_id=u.id
        JOIN users up_user ON u.user_id=up_user.id
        WHERE mv.status='pending' AND mv.assigned_to IS NULL
        ORDER BY mv.created_at DESC
    """)
    unassigned_tasks = fetchall_dict(c)
    
    # Get assigned tasks
    c.execute("""
        SELECT mv.id, mv.assigned_to, mv.created_at,
               u.original_filename, u.upload_date,
               co.name as company_name, co.code as company_code,
               d.name as division_name,
               e.stockist_name, e.statement_from_date, e.statement_to_date,
               e.invoice_net, e.doc_type,
               (SELECT COUNT(*) FROM parties p WHERE p.extraction_id=e.id) as party_count,
               up_user.full_name as uploader_name,
               sa.full_name as agent_name
        FROM manual_verifications mv
        JOIN uploads u ON mv.upload_id=u.id
        JOIN companies co ON u.company_id=co.id
        LEFT JOIN divisions d ON d.id=u.division_id
        LEFT JOIN extractions e ON e.upload_id=u.id
        JOIN users up_user ON u.user_id=up_user.id
        JOIN super_admins sa ON mv.assigned_to=sa.id
        WHERE mv.status='pending' AND mv.assigned_to IS NOT NULL
        ORDER BY mv.created_at DESC
    """)
    assigned_tasks = fetchall_dict(c)
    
    conn.close()
    return render_template("superadmin.html", page="sa_tasks",
        agents=agents, unassigned_tasks=unassigned_tasks, assigned_tasks=assigned_tasks,
        sa_name=session.get("super_admin_name", "Super Admin"),
        vcounts=get_sa_vcounts())


@router.route("/superadmin/tasks/assign", methods=["POST"])
@superadmin_required
def sa_assign_task():
    """Assign a task to an agent."""
    task_id = request.form.get("task_id")
    agent_id = request.form.get("agent_id")
    
    if not task_id or not agent_id:
        flash("Task and agent are required")
        return redirect(url_for("sa_tasks"))
    
    conn = get_db()
    c = conn.cursor()
    try:
        # Check if task exists and is unassigned
        c.execute("SELECT id FROM manual_verifications WHERE id=%s AND status='pending' AND assigned_to IS NULL", (task_id,))
        if not c.fetchone():
            flash("Task not found or already assigned")
            return redirect(url_for("sa_tasks"))
        
        # Check if agent exists and is active
        c.execute("SELECT id FROM super_admins WHERE id=%s AND role='agent' AND status='active'", (agent_id,))
        if not c.fetchone():
            flash("Agent not found or inactive")
            return redirect(url_for("sa_tasks"))
        
        c.execute("UPDATE manual_verifications SET assigned_to=%s WHERE id=%s", (agent_id, task_id))
        conn.commit()
        flash("✓ Task assigned successfully")
    except Exception as e:
        conn.rollback()
        flash(f"Error assigning task: {e}")
    finally:
        conn.close()
    return redirect(url_for("sa_tasks"))


@router.route("/superadmin/tasks/bulk-assign", methods=["POST"])
@superadmin_required
def sa_bulk_assign_tasks():
    """Bulk assign tasks to an agent."""
    task_ids = request.form.getlist("task_ids[]")
    agent_id = request.form.get("agent_id")
    
    if not task_ids or not agent_id:
        flash("Tasks and agent are required")
        return redirect(url_for("sa_tasks"))
    
    conn = get_db()
    c = conn.cursor()
    assigned_count = 0
    try:
        # Check if agent exists and is active
        c.execute("SELECT id FROM super_admins WHERE id=%s AND role='agent' AND status='active'", (agent_id,))
        if not c.fetchone():
            flash("Agent not found or inactive")
            return redirect(url_for("sa_tasks"))
        
        for task_id in task_ids:
            c.execute("UPDATE manual_verifications SET assigned_to=%s WHERE id=%s AND status='pending' AND assigned_to IS NULL", (agent_id, task_id))
            if c.rowcount > 0:
                assigned_count += 1
        
        conn.commit()
        flash(f"✓ {assigned_count} tasks assigned successfully")
    except Exception as e:
        conn.rollback()
        flash(f"Error assigning tasks: {e}")
    finally:
        conn.close()
    return redirect(url_for("sa_tasks"))


@router.route("/superadmin/tasks/unassign/<int:task_id>", methods=["POST"])
@superadmin_required
def sa_unassign_task(task_id):
    """Unassign a task from an agent."""
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("UPDATE manual_verifications SET assigned_to=NULL WHERE id=%s AND status='pending'", (task_id,))
        conn.commit()
        flash("✓ Task unassigned successfully")
    except Exception as e:
        conn.rollback()
        flash(f"Error unassigning task: {e}")
    finally:
        conn.close()
    return redirect(url_for("sa_tasks"))



# ═══════════════════════════════════════════════════════════════════════════════
# VERIFICATION AGENT -- restricted portal (role = agent)
# ═══════════════════════════════════════════════════════════════════════════════
