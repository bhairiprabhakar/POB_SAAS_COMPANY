"""
POB Activity router -- the MR submission flow.

An MR selects a chemist, campaign and product, enters quantities/amounts and
uploads the invoice. The submission is hashed (content hash) and checked
against existing POBs for duplicates before it enters the verification queue.
"""
import hashlib
import io
import json
import uuid
from datetime import date as _date, datetime as _datetime, timedelta

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from openpyxl import Workbook

from .. import config, storage
from ..audit import log_action
from ..db_utils import fetchall_dict, fetchone_dict
from ..deps import TenantContext, get_tenant_context, require_permission
from ..notify import notify_from_template
from ..scoping import scope_filter, visible_user_ids
from ..upload_validation import DOCUMENT_KINDS, IMAGE_KINDS, UploadValidationError, validate_upload
from ..pagination import PageLimit, PageOffset
from ..usage import record_ocr_usage
router = APIRouter(prefix="/api/v1", tags=["pob"])


def _content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _coerce_int(value):
    """Accept ints, digit-strings or floats from JSON bodies so a select that
    sends campaign_id as '2' still matches the DB int column."""
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalise_invoice_no(value) -> str:
    """Case / punctuation-insensitive invoice number for duplicate checks."""
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def _attach_proof_lag(items):
    """Add ``proof_lag_days`` (whole days between the POB submission and its
    invoice-proof upload) to each row carrying created_at / proof_submitted_at.
    Rows without a proof keep lag None; single-shot submissions are 0."""
    def parse_dt(value):
        if isinstance(value, _datetime):
            return value
        if isinstance(value, str):
            for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                        "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
                try:
                    return _datetime.strptime(value, fmt)
                except ValueError:
                    continue
        return None

    for it in items or []:
        lag = None
        created = parse_dt(it.get("created_at"))
        proofed = parse_dt(it.get("proof_submitted_at"))
        if proofed:
            lag = 0 if not created else max(0, int((proofed - created).total_seconds() // 86400))
        it["proof_lag_days"] = lag
    return items


def _parse_date(value):
    """Best-effort YYYY-MM-DD parse (partial datetimes are truncated)."""
    if not value:
        return None
    for cand in (str(value), str(value)[:10]):
        try:
            return _date.fromisoformat(cand)
        except ValueError:
            continue
    return None


def _add_months(d, months):
    """Calendar-aware month arithmetic (clamps to the target month's length,
    e.g. Jan 31 + 1 month -> Feb 28/29)."""
    if not d:
        return None
    try:
        import calendar
        month = d.month - 1 + months
        year = d.year + month // 12
        month = month % 12 + 1
        day = min(d.day, calendar.monthrange(year, month)[1])
        return _date(year, month, day)
    except Exception:
        return d


def _campaign_window(campaign):
    """Resolve the campaign's invoice-acceptance window.

    Returns (start_floor, end_ceiling, parts) where parts describes the
    window in the error/report text:
      start_floor = start_date - pre_grace_days
      end_ceiling = end_date + grace_months months + grace_days
    """
    start = _parse_date(campaign.get("start_date"))
    end = _parse_date(campaign.get("end_date"))
    try:
        pre_grace = int(campaign.get("pre_grace_days") or 0)
    except (TypeError, ValueError):
        pre_grace = 0
    try:
        grace_days = int(campaign.get("grace_days") or 15)
    except (TypeError, ValueError):
        grace_days = 15
    try:
        grace_months = int(campaign.get("grace_months") or 0)
    except (TypeError, ValueError):
        grace_months = 0
    floor = (start - timedelta(days=pre_grace)) if start else None
    ceiling = None
    if end:
        ceiling = _add_months(end, grace_months)
        if ceiling:
            ceiling += timedelta(days=grace_days)
    parts = []
    if pre_grace:
        parts.append(f"{pre_grace}d before start")
    if grace_months:
        parts.append(f"{grace_months} months grace")
    if grace_days and not (parts and grace_months):
        parts.append(f"{grace_days}d grace")
    if not parts:
        parts.append("campaign period")
    return floor, ceiling, parts


def _validate_invoice_period(campaign, invoice_date):
    """Reject an invoice proof whose date falls outside the campaign window
    [start_date - pre_grace_days, end_date + grace_months months + grace_days].

    The months-based grace (selectable when the campaign is configured, for
    new launches) plus the pre-launch days window let invoices issued just
    before/after the campaign dates be accepted. Enforced only when a date is
    known and the campaign defines a bound."""
    if not invoice_date or not campaign:
        return
    d = _parse_date(invoice_date)
    if not d:
        return
    floor, ceiling, parts = _campaign_window(campaign)
    start = _parse_date(campaign.get("start_date"))
    end = _parse_date(campaign.get("end_date"))
    if not start and not end:
        return
    period_type = campaign.get("period_type") or "none"
    period_label = f" ({period_type})" if period_type != "none" else ""
    window_label = " + ".join(parts)
    if floor and d < floor:
        raise HTTPException(
            400,
            f"Invoice date {d.isoformat()} is before the campaign window{period_label} "
            f"started ({start.isoformat()} minus {campaign.get('pre_grace_days') or 0}d pre-launch "
            f"grace). Invoice copies are accepted only for the campaign window.",
        )
    if ceiling and d > ceiling:
        raise HTTPException(
            400,
            f"Invoice date {d.isoformat()} is outside the campaign window{period_label} "
            f"and its grace ({window_label}, last accepted {ceiling.isoformat()}).",
        )


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _find_invoice_duplicate(conn, chemist_id, invoice_number, invoice_date, items,
                            exclude_pob_ids=()):
    """Composite duplicate detection for invoice proofs.

    A re-upload is rejected only when it is for the SAME chemist with the SAME
    invoice number AND the SAME invoice date AND the submitted brand lines
    reproduce an existing POB (same brand, quantity and amount within
    tolerance). Previously-rejected POBs never block a re-upload — a
    better-quality photo of a rejected invoice must be accepted. Returns
    (id, status) of the earlier POB or None.

    ``items`` are the brand lines extracted from the invoice being uploaded.
    Without them (no OCR / no line items) a duplicate cannot be confirmed, so
    nothing is rejected here — the identical-document content-hash guard still
    applies separately."""
    norm = _normalise_invoice_no(invoice_number)
    if not norm:
        return None
    if not items:
        return None
    c = conn.cursor()
    c.execute(
        """SELECT pa.id, pa.status, pa.quantity, pa.invoice_amount, pa.invoice_date,
                  pr.name AS product_name, pr.sku, b.name AS brand_name
           FROM pob_activities pa
           LEFT JOIN products pr ON pr.id=pa.product_id
           LEFT JOIN brands b ON b.id=pr.brand_id
           WHERE pa.chemist_id=%s AND pa.invoice_number_norm=%s AND pa.status<>'rejected'
             AND NOT (pa.id = ANY(%s))
           ORDER BY pa.id""",
        (chemist_id, norm, list(exclude_pob_ids)),
    )
    rows = fetchall_dict(c)
    if not rows:
        return None
    new_date = _parse_date(invoice_date)
    candidates = []
    for r in rows:
        rdate = _parse_date(r.get("invoice_date"))
        if new_date:
            if rdate == new_date:
                candidates.append(r)
        elif not rdate:
            candidates.append(r)
    for row in candidates:
        if _duplicate_line_matches(items, row):
            return (row["id"], row["status"])
    return None


def _duplicate_line_matches(items, row):
    """True when an existing POB row's brand line is reproduced by the new
    invoice's extracted items: same brand with quantity and amount within
    tolerance (qty must agree to 0.01; amount within 1.00 or 2% of the
    recorded amount)."""
    from .. import ocr as _ocr
    row_qty = _num(row.get("quantity"))
    row_amt = _num(row.get("invoice_amount")) or _num(row.get("pob_amount"))
    if row_qty is None:
        return False
    for item in items or []:
        if not _ocr.match_brand([item], row.get("product_name"), row.get("brand_name"),
                                row.get("sku")):
            continue
        new_qty = _ocr._normalise_amount(item.get("qty"))
        new_amt = _ocr._normalise_amount(item.get("amount"))
        if new_qty and abs(new_qty - row_qty) > 0.01:
            continue
        if row_amt and new_amt and abs(new_amt - row_amt) > max(1.0, abs(row_amt) * 0.02):
            continue
        return True
    return False


def _workflow_steps(conn, workflow_id):
    """Return the ordered step list for a workflow definition, or []."""
    if not workflow_id:
        return []
    c = conn.cursor()
    c.execute("SELECT steps FROM workflow_definitions WHERE id=%s AND active=TRUE", (workflow_id,))
    row = c.fetchone()
    if not row:
        return []
    steps = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    return sorted(steps, key=lambda s: s.get("order", 0))


def _notify_step_approvers(conn, pob_id, step):
    """Notify everyone in the role named by the step about a pending approval."""
    role = step.get("role")
    if not role:
        return
    c = conn.cursor()
    c.execute("SELECT id FROM users WHERE role_id=(SELECT id FROM roles WHERE name=%s) AND status='active'", (role,))
    approvers = [r[0] for r in c.fetchall()]
    c.execute("SELECT id FROM pob_activities WHERE id=%s", (pob_id,))
    if not approvers:
        return
    label = step.get("label") or "Approval"
    for uid in approvers:
        c.execute(
            "INSERT INTO notifications (user_id, channel, type, title, message, reference_type, reference_id) "
            "VALUES (%s,'inapp','workflow','Approval pending',%s,'pob',%s)",
            (uid, f"{label} required for POB #{pob_id}", pob_id),
        )
    conn.commit()


def _workflow_approvers(conn, role: str):
    c = conn.cursor()
    c.execute("SELECT id FROM users WHERE role_id=(SELECT id FROM roles WHERE name=%s) AND status='active'", (role,))
    return [r[0] for r in c.fetchall()]


def _finalize_pob(conn, ctx, campaign, pob_id: int, invoice_bytes: bytes, filename: str,
                  invoice_number: str, invoice_date: str, exclude_ids=(), extraction=None):
    """Attach an invoice proof to an already-submitted (visit) POB row and push
    it into the verification pipeline. Mirrors the single-shot submit_pob
    pipeline: duplicate detection, OCR auto-verify, approval steps, notifications.
    ``exclude_ids`` are the sibling rows of the same visit (they all carry the
    same invoice, so they must not trip each other's duplicate checks).
    ``extraction`` reuses the OCR result already obtained by the caller
    (invoice-proof upload) so a multi-row visit extracts the document once.
    Returns (status, verification_id, auto_verified)."""
    from ..webhooks import dispatch_event

    c = conn.cursor()
    c.execute("SELECT * FROM pob_activities WHERE id=%s", (pob_id,))
    pob = fetchone_dict(c)
    if not pob:
        raise HTTPException(404, "POB not found")
    chemist_id = pob["chemist_id"]

    invoice_path = None
    content_hash = None
    if invoice_bytes:
        try:
            validate_upload(invoice_bytes, filename=filename or "", allowed_kinds=DOCUMENT_KINDS | IMAGE_KINDS,
                            max_size=config.MAX_UPLOAD_SIZE)
        except UploadValidationError as exc:
            raise HTTPException(400, str(exc))
        invoice_path = storage.save(invoice_bytes, ctx.claims["tenant_db"], "invoices", filename)
        content_hash = _content_hash(invoice_bytes)

    status = "pending_verification"
    dup_of = None
    dup_reason = None
    if content_hash:
        c.execute(
            "SELECT id FROM pob_activities WHERE content_hash=%s AND status<>'rejected' "
            "AND NOT (id = ANY(%s))",
            (content_hash, list(exclude_ids)),
        )
        dup = c.fetchone()
        if dup:
            status = "duplicate"
            dup_of = dup[0]
            dup_reason = f"Identical invoice document already uploaded (POB #{dup[0]})"

    # ── OCR / AI extraction (Gemini) ─────────────────────────────────────────
    # Always attempt extraction when an OCR provider is configured so the
    # invoice number / date are captured automatically from the uploaded
    # document (mobile uploads — MR does not type them). The extraction is
    # stored and reused for the auto-verify check below.
    extraction = extraction or {}
    auto_ok = False
    ocr_verdict = None
    from .. import ocr as _ocr
    if status == "pending_verification" and invoice_bytes and not extraction:
        from .. import config as _cfg
        if _cfg.GOOGLE_API_KEY or _cfg.OCR_PROVIDER == "text":
            try:
                extraction = _ocr.extract_fields(invoice_bytes, filename)
                record_ocr_usage(conn, ctx.user["id"], extraction, filename=filename,
                                 invoice_number=invoice_number)
            except Exception:
                extraction = {}
    if extraction:
        fields = extraction.get("fields") or {}
        if not invoice_number:
            invoice_number = str(fields.get("invoice_number") or "").strip()
        if not invoice_date:
            invoice_date = _ocr._normalise_date(fields.get("invoice_date") or "")

    # ── Brand match: auto-update qty & amount from the extracted invoice ─────
    # The AI reads the brand-wise lines off the invoice. Match this submitted
    # product/brand to its extracted line and, when found, sync quantity,
    # invoice amount and POB amount to what the invoice actually says.
    items = []
    matched_item = None
    if extraction:
        items = (extraction.get("fields") or {}).get("items") or []
        if items:
            c.execute(
                """SELECT pr.name AS product_name, b.name AS brand_name
                   FROM products pr LEFT JOIN brands b ON b.id=pr.brand_id
                   WHERE pr.id=%s""", (pob["product_id"],))
            pinfo = fetchone_dict(c) or {}
            matched_item = _ocr.match_brand(items, pinfo.get("product_name"), pinfo.get("brand_name"))
            if matched_item:
                new_qty = _ocr._normalise_amount(matched_item.get("qty")) or float(pob["quantity"] or 0)
                new_inv_amt = _ocr._normalise_amount(matched_item.get("amount"))
                new_pob_amt = round(new_qty * float(pob["ptr"] or 0), 2)
                upd, params = [], []
                if new_qty != float(pob["quantity"] or 0):
                    upd.append("quantity=%s")
                    params.append(new_qty)
                if new_inv_amt and new_inv_amt != float(pob["invoice_amount"] or 0):
                    upd.append("invoice_amount=%s")
                    params.append(new_inv_amt)
                if new_qty != float(pob["quantity"] or 0) and new_pob_amt != float(pob["pob_amount"] or 0):
                    upd.append("pob_amount=%s")
                    params.append(new_pob_amt)
                if upd:
                    params.append(pob_id)
                    c.execute(f"UPDATE pob_activities SET {', '.join(upd)} WHERE id=%s", params)
                    pob["quantity"] = new_qty
                    if new_inv_amt:
                        pob["invoice_amount"] = new_inv_amt
                    pob["pob_amount"] = new_pob_amt

    if invoice_number:
        dup = _find_invoice_duplicate(conn, chemist_id, invoice_number, invoice_date,
                                      items, exclude_pob_ids=exclude_ids)
        if dup:
            raise HTTPException(400, f"Duplicate invoice — invoice already extracted (POB #{dup[0]}). "
                                     f"The same invoice number, date and brand details were already "
                                     f"submitted for this chemist.")
    _validate_invoice_period(campaign, invoice_date)

    c.execute(
        """UPDATE pob_activities SET status=%s, invoice_path=%s, invoice_original_name=%s,
           invoice_number=%s, invoice_date=%s, content_hash=%s, invoice_number_norm=%s,
           proof_submitted_at=CURRENT_TIMESTAMP, verification_state=%s WHERE id=%s""",
        (status, invoice_path, filename, invoice_number, invoice_date or None, content_hash,
         _normalise_invoice_no(invoice_number),
         "submitted" if status == "pending_verification" else status, pob_id),
    )

    if extraction:
        c.execute("""INSERT INTO ocr_extractions (pob_id, engine, raw, fields, confidence, auto_approved)
                     VALUES (%s,%s,%s,%s,%s,%s)""",
                  (pob_id, extraction.get("engine"), json.dumps(extraction.get("raw") or {}),
                   json.dumps(extraction.get("fields") or {}),
                   extraction.get("confidence"), False))
    if status == "pending_verification" and campaign.get("auto_verify") and extraction:
        # Build full context for auto-verify (all 12 checks)
        c.execute("SELECT * FROM products WHERE id=%s", (pob["product_id"],))
        product = fetchone_dict(c) or {}
        c.execute("SELECT name, shop_name FROM chemists WHERE id=%s", (pob["chemist_id"],))
        ch = fetchone_dict(c) or {}
        is_dup = bool(_find_invoice_duplicate(conn, chemist_id, invoice_number, invoice_date,
                                              items, exclude_pob_ids=exclude_ids))
        auto_ctx = {
            "chemist_name": ch.get("name") or "",
            "shop_name": ch.get("shop_name") or "",
            "ptr": pob.get("ptr"),
            "is_duplicate": is_dup,
            "campaign_start": campaign.get("start_date"),
            "campaign_end": campaign.get("end_date"),
            "campaign_grace_days": campaign.get("grace_days"),
            "campaign_grace_months": campaign.get("grace_months"),
            "campaign_pre_grace_days": campaign.get("pre_grace_days"),
            "min_quantity": product.get("min_quantity") if product else None,
            "min_pob": product.get("min_pob") if product else None,
            "max_pob": product.get("max_pob") if product else None,
        }
        verdict = _ocr.verify_invoice(
            {"invoice_number": invoice_number, "invoice_amount": pob["invoice_amount"],
             "invoice_date": invoice_date,
             "product_name": product.get("name") if product else "",
             "quantity": pob["quantity"], "pob_amount": pob["pob_amount"]},
            extraction, float(campaign.get("auto_verify_confidence") or 0.9),
            context=auto_ctx)
        ocr_verdict = dict(verdict)
        if verdict["ok"]:
            auto_ok = True
            status = "verified"
            c.execute("UPDATE pob_activities SET status='verified', auto_verified=TRUE, "
                      "confidence=%s, verification_state='auto_verified' WHERE id=%s",
                      (verdict["confidence"], pob_id))

    steps = _workflow_steps(conn, campaign.get("approval_workflow_id"))
    if auto_ok:
        c.execute(
            """INSERT INTO pob_verifications (pob_id, status, reason, pipeline_status)
               VALUES (%s,'approved',%s,'completed') RETURNING id""",
            (pob_id, "Auto-verified by OCR"),
        )
        vid = c.fetchone()[0]
        c.execute("UPDATE pob_activities SET current_verification_id=%s, workflow_done=TRUE, "
                  "verification_state='auto_verified' WHERE id=%s", (vid, pob_id))
        c.execute("UPDATE pob_approvals SET status='skipped' WHERE pob_id=%s", (pob_id,))
    elif steps:
        for i, step in enumerate(steps, start=1):
            c.execute(
                """INSERT INTO pob_approvals (pob_id, step, step_name, role_name, status)
                   VALUES (%s,%s,%s,%s,'pending')""",
                (pob_id, i, step.get("label"), step.get("role")),
            )
        v_status = status if status != "pending_verification" else "pending"
        v_pipeline = "pending_agent" if v_status == "pending" else "completed"
        c.execute(
            """INSERT INTO pob_verifications (pob_id, status, reason, duplicate_of, pipeline_status)
               VALUES (%s,%s,%s,%s,%s) RETURNING id""",
            (pob_id, v_status, dup_reason, dup_of, v_pipeline),
        )
        vid = c.fetchone()[0]
        c.execute("UPDATE pob_activities SET current_verification_id=%s, "
                  "verification_state=%s WHERE id=%s",
                  (vid, "manual_review" if v_status == "pending" else v_status, pob_id))
        _notify_step_approvers(conn, pob_id, steps[0])
    else:
        c.execute(
            """INSERT INTO pob_verifications (pob_id, status, reason, duplicate_of)
               VALUES (%s,%s,%s,%s) RETURNING id""",
            (pob_id, status if status != "pending_verification" else "pending",
             dup_reason, dup_of),
        )
        vid = c.fetchone()[0]
        c.execute("UPDATE pob_activities SET current_verification_id=%s WHERE id=%s", (vid, pob_id))

    c.execute("INSERT INTO verification_history (pob_id, action, reason) VALUES (%s,%s,%s)",
              (pob_id, "invoice_submitted", f"Invoice proof attached: {filename}"))

    if auto_ok:
        from .. import rules
        gid, decision = rules.create_gratification(conn, pob_id, ctx.user["id"],
                                                   campaign["id"], pob["pob_amount"], ctx.user["id"])
        c.execute("INSERT INTO verification_history (pob_id, verifier_id, action, reason) "
                  "VALUES (%s,%s,'approved',%s)",
                  (pob_id, ctx.user["id"], "Auto-verified by OCR"))
        log_action(conn, ctx.user["id"], "verification.auto_approve", "pob_activity", pob_id,
                   {"confidence": ocr_verdict["confidence"], "gratification_id": gid,
                    "rule": decision.get("rule_name")})
        notify_from_template(conn, ctx.user["id"], "pob.approved",
                             {"pob_id": pob_id, "campaign": campaign["name"]}, "pob", pob_id)
        dispatch_event(conn, "pob.approved", {
            "pob_id": pob_id, "verification_id": vid, "campaign_id": campaign["id"],
            "campaign": campaign["name"], "invoice_number": invoice_number,
            "invoice_amount": pob["invoice_amount"] or pob["pob_amount"], "status": status,
            "submitted_by": ctx.user["username"],
        }, ctx.claims.get("tenant_db", ""))
    else:
        notify_from_template(conn, ctx.user["id"], "pob.submitted",
                             {"campaign": campaign["name"],
                              "invoice_amount": pob["invoice_amount"] or pob["pob_amount"]},
                             "pob", pob_id)
        dispatch_event(conn, "pob.submitted", {
            "pob_id": pob_id, "verification_id": vid, "campaign_id": campaign["id"],
            "campaign": campaign["name"], "invoice_number": invoice_number,
            "invoice_amount": pob["invoice_amount"] or pob["pob_amount"], "status": status,
            "submitted_by": ctx.user["username"],
        }, ctx.claims.get("tenant_db", ""))

    log_action(conn, ctx.user["id"], "pob.invoice_proof", "pob_activity", pob_id,
               {"campaign_id": campaign["id"], "chemist_id": chemist_id, "status": status},
               request=None)
    return status, vid, auto_ok


@router.post("/pob/submit")
async def submit_pob(
    campaign_id: int = Form(...),
    product_id: int = Form(...),
    chemist_id: int = Form(...),
    quantity: float = Form(...),
    ptr: float = Form(0),
    mrp: float = Form(0),
    invoice_amount: float = Form(0),
    pob_amount: float = Form(0),
    invoice_number: str = Form(""),
    invoice_date: str = Form(""),
    remarks: str = Form(""),
    invoice: UploadFile = File(None),
    file: UploadFile = File(None),
    request: Request = None,
    ctx: TenantContext = Depends(require_permission("pob.submit")),
):
    invoice = invoice or file
    conn = ctx.conn
    c = conn.cursor()

    c.execute("SELECT * FROM campaigns WHERE id=%s", (campaign_id,))
    campaign = fetchone_dict(c)
    if not campaign or campaign["status"] not in ("active", "draft"):
        raise HTTPException(400, "Campaign is not active")

    # Campaign Builder: restrict who may upload for this campaign.
    upload_roles = (campaign.get("upload_roles") or "").strip()
    if upload_roles and upload_roles != "*":
        c.execute("SELECT r.name FROM roles r JOIN users u ON u.role_id=r.id WHERE u.id=%s", (ctx.user["id"],))
        row = c.fetchone()
        role_name = row[0] if row else ""
        allowed = {r.strip() for r in upload_roles.split(",") if r.strip()}
        if role_name not in allowed:
            raise HTTPException(403, f"Role '{role_name}' is not allowed to upload for this campaign (allowed: {', '.join(sorted(allowed))})")

    c.execute("SELECT * FROM products WHERE id=%s", (product_id,))
    product = fetchone_dict(c)
    if not product or product["campaign_id"] != campaign_id:
        raise HTTPException(400, "Product does not belong to this campaign")

    c.execute("SELECT * FROM chemists WHERE id=%s", (chemist_id,))
    if not c.fetchone():
        raise HTTPException(400, "Chemist not found")

    # POB amount / PTR / MRP are derived from the product master (mirroring the
    # bulk import path) so the caller cannot inflate the authorised POB amount by
    # sending its own pob_amount / ptr / mrp in the form.
    ptr = float(product.get("ptr") or 0)
    mrp = float(product.get("mrp") or 0)
    if ptr <= 0:
        raise HTTPException(400, "Product has no PTR configured — set the PTR in Campaign Builder before submitting")
    pob_amount = round(quantity * ptr, 2)

    # validate product POB bounds (against the server-derived amount)
    if product["scheme_eligibility"]:
        min_pob = product["min_pob"] or 0
        max_pob = product["max_pob"]
        if max_pob is not None and pob_amount > max_pob:
            raise HTTPException(400, f"POB amount exceeds the campaign max ({max_pob})")
        if min_pob and pob_amount < min_pob:
            raise HTTPException(400, f"POB amount is below the campaign minimum ({min_pob})")
        if product["min_quantity"] and quantity < product["min_quantity"]:
            raise HTTPException(400, f"Quantity below the campaign minimum ({product['min_quantity']})")

    # persist invoice
    invoice_path = None
    content_hash = None
    if invoice is not None and invoice.filename:
        data = await invoice.read()
        if not data:
            raise HTTPException(400, "Empty invoice file")
        try:
            validate_upload(data, filename=invoice.filename, allowed_kinds=DOCUMENT_KINDS | IMAGE_KINDS,
                            max_size=config.MAX_UPLOAD_SIZE)
        except UploadValidationError as exc:
            raise HTTPException(400, str(exc))
        invoice_path = storage.save(data, ctx.claims["tenant_db"], "invoices", invoice.filename)
        content_hash = _content_hash(data)

    # duplicate detection
    status = "pending_verification"
    dup_of = None
    dup_reason = None
    if content_hash:
        c.execute("SELECT id, user_id FROM pob_activities WHERE content_hash=%s AND status<>'rejected'",
                  (content_hash,))
        dup = c.fetchone()
        if dup:
            status = "duplicate"
            dup_of = dup[0]
            dup_reason = f"Identical invoice document already uploaded (POB #{dup[0]})"
    # Extract the invoice once (when a provider is configured) so the
    # composite duplicate check has brand lines to compare, the invoice
    # number/date can be auto-filled for mobile uploads, and auto-verify
    # below reuses the same result.
    extraction = {}
    if status == "pending_verification" and invoice_path:
        from .. import ocr as _ocr
        from .. import config as _cfg
        if _cfg.GOOGLE_API_KEY or _cfg.OCR_PROVIDER == "text":
            try:
                extraction = _ocr.extract_fields(data, invoice.filename)
                record_ocr_usage(conn, ctx.user["id"], extraction, filename=invoice.filename,
                                 invoice_number=invoice_number)
            except Exception:
                extraction = {}
        if extraction:
            fields = extraction.get("fields") or {}
            if not invoice_number:
                invoice_number = str(fields.get("invoice_number") or "").strip()
            if not invoice_date:
                invoice_date = _ocr._normalise_date(fields.get("invoice_date") or "")
    if invoice_number:
        dup = _find_invoice_duplicate(conn, chemist_id, invoice_number, invoice_date,
                                      ((extraction or {}).get("fields") or {}).get("items") or [])
        if dup:
            raise HTTPException(400, f"Duplicate invoice — invoice already extracted (POB #{dup[0]}). "
                                     f"The same invoice number, date and brand details were already "
                                     f"submitted for this chemist.")
    _validate_invoice_period(campaign, invoice_date)

    verification_state = "submitted" if status == "pending_verification" else status
    c.execute(
        """INSERT INTO pob_activities (user_id, campaign_id, product_id, chemist_id, quantity, ptr,
           mrp, invoice_amount, pob_amount, remarks, invoice_path, invoice_original_name,
           invoice_number, invoice_date, content_hash, status, workflow_id, invoice_number_norm,
           proof_submitted_at, verification_state)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP,%s) RETURNING id""",
        (ctx.user["id"], campaign_id, product_id, chemist_id, quantity, ptr, mrp,
         invoice_amount, pob_amount, remarks, invoice_path,
         invoice.filename if invoice else None, invoice_number, invoice_date or None,
         content_hash, status, campaign.get("approval_workflow_id"),
         _normalise_invoice_no(invoice_number), verification_state),
    )
    pob_id = c.fetchone()[0]

    # ── Phase 4: OCR / AI auto-verification ─────────────────────────────────
    # Campaigns with auto_verify enabled extract the invoice fields and, when
    # they match the submission above the confidence threshold, the POB is
    # verified instantly (skips the approval chain / manual queue).
    auto_ok = False
    ocr_verdict = None
    if status == "pending_verification" and campaign.get("auto_verify") and invoice_path and extraction:
        from .. import ocr
        c.execute("SELECT name, shop_name FROM chemists WHERE id=%s", (chemist_id,))
        ch = fetchone_dict(c) or {}
        is_dup = bool(_find_invoice_duplicate(conn, chemist_id, invoice_number, invoice_date,
                                              (extraction.get("fields") or {}).get("items") or []))
        auto_ctx = {
            "chemist_name": ch.get("name") or "",
            "shop_name": ch.get("shop_name") or "",
            "ptr": ptr,
            "is_duplicate": is_dup,
            "campaign_start": campaign.get("start_date"),
            "campaign_end": campaign.get("end_date"),
            "campaign_grace_days": campaign.get("grace_days"),
            "campaign_grace_months": campaign.get("grace_months"),
            "campaign_pre_grace_days": campaign.get("pre_grace_days"),
            "min_quantity": product.get("min_quantity") if product else None,
            "min_pob": product.get("min_pob") if product else None,
            "max_pob": product.get("max_pob") if product else None,
        }
        verdict = ocr.verify_invoice(
            {"invoice_number": invoice_number, "invoice_amount": invoice_amount,
             "invoice_date": invoice_date,
             "product_name": product.get("name") if product else "",
             "quantity": quantity, "pob_amount": pob_amount},
            extraction, float(campaign.get("auto_verify_confidence") or 0.9),
            context=auto_ctx)
        ocr_verdict = dict(verdict)
        c.execute("""INSERT INTO ocr_extractions (pob_id, engine, raw, fields, confidence, auto_approved)
                     VALUES (%s,%s,%s,%s,%s,%s)""",
                  (pob_id, extraction.get("engine"), json.dumps(extraction.get("raw") or {}),
                   json.dumps(extraction.get("fields") or {}),
                   extraction.get("confidence"), verdict["ok"]))
        if verdict["ok"]:
            auto_ok = True
            status = "verified"
            c.execute("UPDATE pob_activities SET status='verified', auto_verified=TRUE, "
                      "confidence=%s, verification_state='auto_verified' WHERE id=%s",
                      (verdict["confidence"], pob_id))

    # Campaign Builder: seed approval steps for a dynamic workflow, or a single
    # verification record when no workflow is configured. Auto-verified POBs
    # skip the chain and close their verification record immediately.
    steps = _workflow_steps(conn, campaign.get("approval_workflow_id"))
    if auto_ok:
        c.execute(
            """INSERT INTO pob_verifications (pob_id, status, reason)
               VALUES (%s,'approved',%s) RETURNING id""",
            (pob_id, "Auto-verified by OCR"),
        )
        vid = c.fetchone()[0]
        c.execute("UPDATE pob_activities SET current_verification_id=%s, workflow_done=TRUE "
                  "WHERE id=%s", (vid, pob_id))
        c.execute("UPDATE pob_approvals SET status='skipped' WHERE pob_id=%s", (pob_id,))
    elif steps:
        for i, step in enumerate(steps, start=1):
            c.execute(
                """INSERT INTO pob_approvals (pob_id, step, step_name, role_name, status)
                   VALUES (%s,%s,%s,%s,'pending')""",
                (pob_id, i, step.get("label"), step.get("role")),
            )
        c.execute(
            """INSERT INTO pob_verifications (pob_id, status, reason, duplicate_of)
               VALUES (%s,%s,%s,%s) RETURNING id""",
            (pob_id, status if status != "pending_verification" else "pending",
             dup_reason, dup_of),
        )
        vid = c.fetchone()[0]
        c.execute("UPDATE pob_activities SET current_verification_id=%s WHERE id=%s", (vid, pob_id))
        _notify_step_approvers(conn, pob_id, steps[0])
    else:
        # create the verification record
        c.execute(
            """INSERT INTO pob_verifications (pob_id, status, reason, duplicate_of)
               VALUES (%s,%s,%s,%s) RETURNING id""",
            (pob_id, status if status != "pending_verification" else "pending",
             dup_reason, dup_of),
        )
        vid = c.fetchone()[0]
        c.execute("UPDATE pob_activities SET current_verification_id=%s WHERE id=%s", (vid, pob_id))
    c.execute(
        "INSERT INTO verification_history (pob_id, action, reason) VALUES (%s,%s,%s)",
        (pob_id, "submitted", remarks),
    )
    if auto_ok:
        from .. import rules
        gid, decision = rules.create_gratification(conn, pob_id, ctx.user["id"], campaign_id,
                                                   pob_amount, ctx.user["id"])
        c.execute("INSERT INTO verification_history (pob_id, verifier_id, action, reason) "
                  "VALUES (%s,%s,'approved',%s)",
                  (pob_id, ctx.user["id"], "Auto-verified by OCR"))
        log_action(conn, ctx.user["id"], "verification.auto_approve", "pob_activity", pob_id,
                   {"confidence": ocr_verdict["confidence"], "gratification_id": gid,
                    "rule": decision.get("rule_name")})
        notify_from_template(conn, ctx.user["id"], "pob.approved",
                             {"pob_id": pob_id, "campaign": campaign["name"]},
                             "pob", pob_id)
    conn.commit()

    log_action(conn, ctx.user["id"], "pob.submit", "pob_activity", pob_id,
               {"campaign_id": campaign_id, "amount": pob_amount}, request=request)
    if status == "pending_verification":
        notify_from_template(conn, ctx.user["id"], "pob.submitted",
                             {"campaign": campaign["name"], "invoice_amount": invoice_amount or pob_amount},
                             "pob", pob_id)
    from ..webhooks import dispatch_event
    dispatch_event(conn, "pob.approved" if auto_ok else "pob.submitted", {
        "pob_id": pob_id, "verification_id": vid, "campaign_id": campaign_id,
        "campaign": campaign["name"], "invoice_number": invoice_number,
        "invoice_amount": invoice_amount or pob_amount, "status": status,
        "submitted_by": ctx.user["username"],
    }, ctx.claims.get("tenant_db", ""))

    return {
        "ok": True,
        "pob_id": pob_id,
        "verification_id": vid,
        "status": status,
        "auto_verified": auto_ok,
        "ocr_confidence": (ocr_verdict or {}).get("confidence"),
        "duplicate_reason": dup_reason,
    }


@router.post("/pob/invoice-submit")
async def submit_invoice_only(
    campaign_id: int = Form(...),
    chemist_id: int = Form(...),
    invoice: UploadFile = File(...),
    request: Request = None,
    ctx: TenantContext = Depends(require_permission("pob.submit")),
):
    """Invoice-only submit for campaigns with pob_required=FALSE.

    The MR just uploads an invoice.  The backend:
      1. Extracts all fields via Gemini (product, qty, amount, date, invoice#)
      2. Auto-matches the product to the campaign
      3. Auto-fills quantity, POB amount, PTR from the extraction
      4. Creates the POB record
      5. Runs the full 12-check auto-verify

    If extraction fails or product can't be matched, the POB goes to manual.
    """
    conn = ctx.conn
    c = conn.cursor()

    c.execute("SELECT * FROM campaigns WHERE id=%s", (campaign_id,))
    campaign = fetchone_dict(c)
    if not campaign or campaign["status"] not in ("active", "draft"):
        raise HTTPException(400, "Campaign is not active")
    if campaign.get("pob_required", True):
        raise HTTPException(400, "This campaign requires manual POB entry; use /pob/submit instead")

    # Upload role check
    upload_roles = (campaign.get("upload_roles") or "").strip()
    if upload_roles and upload_roles != "*":
        c.execute("SELECT r.name FROM roles r JOIN users u ON u.role_id=r.id WHERE u.id=%s", (ctx.user["id"],))
        row = c.fetchone()
        role_name = row[0] if row else ""
        allowed = {r.strip() for r in upload_roles.split(",") if r.strip()}
        if role_name not in allowed:
            raise HTTPException(403, f"Role '{role_name}' not allowed for this campaign")

    c.execute("SELECT * FROM chemists WHERE id=%s", (chemist_id,))
    if not c.fetchone():
        raise HTTPException(400, "Chemist not found")

    # Read and validate invoice
    data = await invoice.read()
    if not data:
        raise HTTPException(400, "Empty invoice file")
    try:
        validate_upload(data, filename=invoice.filename, allowed_kinds=DOCUMENT_KINDS | IMAGE_KINDS,
                        max_size=config.MAX_UPLOAD_SIZE)
    except UploadValidationError as exc:
        raise HTTPException(400, str(exc))

    filename = invoice.filename
    invoice_path = storage.save(data, ctx.claims["tenant_db"], "invoices", filename)
    content_hash = _content_hash(data)

    # Extract invoice via Gemini
    from .. import ocr as _ocr
    from .. import config as _cfg
    extraction = {}
    if _cfg.GOOGLE_API_KEY or _cfg.OCR_PROVIDER == "text":
        try:
            extraction = _ocr.extract_fields(data, filename)
            record_ocr_usage(conn, ctx.user["id"], extraction, filename=filename)
        except Exception:
            extraction = {}

    fields = (extraction.get("fields") or {})
    invoice_number = str(fields.get("invoice_number") or "").strip()
    invoice_date = _ocr._normalise_date(fields.get("invoice_date") or "")
    invoice_amount = _ocr._normalise_amount(fields.get("invoice_amount")) or 0

    # Extract line items and match to campaign products
    items = fields.get("items") or []
    c.execute("""SELECT pr.id, pr.name, pr.ptr, pr.mrp, b.name AS brand_name, pr.sku
                 FROM products pr LEFT JOIN brands b ON b.id=pr.brand_id
                 WHERE pr.campaign_id=%s AND pr.status='active'""", (campaign_id,))
    products = fetchall_dict(c)

    matched_product = None
    matched_item = None
    for p in products:
        for it in items:
            if _ocr.match_brand([it], p.get("name"), p.get("brand_name") or "", p.get("sku") or ""):
                matched_product = p
                matched_item = it
                break
        if matched_product:
            break

    if not matched_product:
        # No product match — create POB for first product with extracted qty/amount
        # and send to manual for human to assign correct product
        if products:
            matched_product = products[0]
        else:
            raise HTTPException(400, "No active products in this campaign")

    # Auto-fill from extraction
    extracted_qty = _ocr._normalise_amount(matched_item.get("qty")) if matched_item else 1
    extracted_qty = int(extracted_qty) if extracted_qty else 1
    extracted_line_amount = _ocr._normalise_amount(matched_item.get("amount")) if matched_item else invoice_amount
    extracted_ptr = (_ocr._normalise_amount(matched_item.get("ptr"))
                     or _ocr._normalise_amount(matched_item.get("rate"))
                     or matched_product.get("ptr") or 0) if matched_item else (matched_product.get("ptr") or 0)
    extracted_pob_amount = round(extracted_qty * float(extracted_ptr), 2)
    extracted_mrp = matched_product.get("mrp") or 0

    # Validate POB bounds
    if matched_product.get("scheme_eligibility"):
        min_pob = matched_product.get("min_pob") or 0
        max_pob = matched_product.get("max_pob")
        if max_pob is not None and extracted_pob_amount > max_pob:
            extracted_pob_amount = float(max_pob)
        if min_pob and extracted_pob_amount < min_pob:
            extracted_pob_amount = float(min_pob)

    # Duplicate check
    dup_reason, dup_of, dup = "", None, None
    content_dup = None
    if content_hash:
        c.execute("SELECT id FROM pob_activities WHERE content_hash=%s AND status<>'rejected'",
                  (content_hash,))
        row = c.fetchone()
        if row:
            content_dup = (row[0],)
    if content_dup:
        dup_of = content_dup[0]
        status = "duplicate"
        dup_reason = f"Identical invoice document (POB #{dup_of})"
    if not dup_reason and invoice_number:
        dup = _find_invoice_duplicate(conn, chemist_id, invoice_number, invoice_date, items)
        if dup:
            status = "duplicate"
            dup_of = dup[0]
            dup_reason = f"Invoice #{invoice_number} already on POB #{dup[0]}"

    status = "pending_verification" if not dup_reason else "duplicate"
    _validate_invoice_period(campaign, invoice_date)

    # Create POB record
    c.execute(
        """INSERT INTO pob_activities (user_id, campaign_id, product_id, chemist_id, quantity, ptr,
           mrp, invoice_amount, pob_amount, remarks, invoice_path, invoice_original_name,
           invoice_number, invoice_date, content_hash, status, workflow_id, invoice_number_norm,
           proof_submitted_at, verification_state)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP,%s) RETURNING id""",
        (ctx.user["id"], campaign_id, matched_product["id"], chemist_id,
         extracted_qty, extracted_ptr, extracted_mrp,
         extracted_line_amount or invoice_amount, extracted_pob_amount,
         "Auto-filled from invoice extraction", invoice_path,
         filename, invoice_number, invoice_date or None,
         content_hash, status, campaign.get("approval_workflow_id"),
         _normalise_invoice_no(invoice_number),
         "submitted" if status == "pending_verification" else status),
    )
    pob_id = c.fetchone()[0]

    # Store OCR extraction
    if extraction:
        c.execute("""INSERT INTO ocr_extractions (pob_id, engine, raw, fields, confidence, auto_approved)
                     VALUES (%s,%s,%s,%s,%s,%s)""",
                  (pob_id, extraction.get("engine"), json.dumps(extraction.get("raw") or {}),
                   json.dumps(fields), extraction.get("confidence"), False))

    # Auto-verify with full 12 checks
    auto_ok = False
    ocr_verdict = None
    vid = None
    if status == "pending_verification" and campaign.get("auto_verify") and extraction:
        c.execute("SELECT name, shop_name FROM chemists WHERE id=%s", (chemist_id,))
        ch = fetchone_dict(c) or {}
        is_dup = bool(dup)
        auto_ctx = {
            "chemist_name": ch.get("name") or "",
            "shop_name": ch.get("shop_name") or "",
            "ptr": extracted_ptr,
            "is_duplicate": is_dup,
            "campaign_start": campaign.get("start_date"),
            "campaign_end": campaign.get("end_date"),
            "campaign_grace_days": campaign.get("grace_days"),
            "campaign_grace_months": campaign.get("grace_months"),
            "campaign_pre_grace_days": campaign.get("pre_grace_days"),
            "min_quantity": matched_product.get("min_quantity"),
            "min_pob": matched_product.get("min_pob"),
            "max_pob": matched_product.get("max_pob"),
        }
        verdict = _ocr.verify_invoice(
            {"invoice_number": invoice_number, "invoice_amount": extracted_line_amount or invoice_amount,
             "invoice_date": invoice_date,
             "product_name": matched_product.get("name") or "",
             "quantity": extracted_qty, "pob_amount": extracted_pob_amount},
            extraction, float(campaign.get("auto_verify_confidence") or 0.9),
            context=auto_ctx)
        ocr_verdict = dict(verdict)
        if verdict["ok"]:
            auto_ok = True
            status = "verified"
            c.execute("UPDATE pob_activities SET status='verified', auto_verified=TRUE, "
                      "confidence=%s, verification_state='auto_verified' WHERE id=%s",
                      (verdict["confidence"], pob_id))

    # Create verification record
    steps = _workflow_steps(conn, campaign.get("approval_workflow_id"))
    if auto_ok:
        c.execute(
            """INSERT INTO pob_verifications (pob_id, status, reason)
               VALUES (%s,'approved',%s) RETURNING id""",
            (pob_id, "Auto-verified by OCR (invoice-only mode)"),
        )
        vid = c.fetchone()[0]
        c.execute("UPDATE pob_activities SET current_verification_id=%s, workflow_done=TRUE "
                  "WHERE id=%s", (vid, pob_id))
        c.execute("UPDATE pob_approvals SET status='skipped' WHERE pob_id=%s", (pob_id,))
    elif steps:
        for i, step in enumerate(steps, start=1):
            c.execute(
                """INSERT INTO pob_approvals (pob_id, step, step_name, role_name, status)
                   VALUES (%s,%s,%s,%s,'pending')""",
                (pob_id, i, step.get("label"), step.get("role")),
            )
        c.execute(
            """INSERT INTO pob_verifications (pob_id, status, reason, duplicate_of)
               VALUES (%s,%s,%s,%s) RETURNING id""",
            (pob_id, status if status != "pending_verification" else "pending",
             dup_reason, dup_of),
        )
        vid = c.fetchone()[0]
        c.execute("UPDATE pob_activities SET current_verification_id=%s WHERE id=%s", (vid, pob_id))
        _notify_step_approvers(conn, pob_id, steps[0])
    else:
        c.execute(
            """INSERT INTO pob_verifications (pob_id, status, reason, duplicate_of)
               VALUES (%s,%s,%s,%s) RETURNING id""",
            (pob_id, status if status != "pending_verification" else "pending",
             dup_reason, dup_of),
        )
        vid = c.fetchone()[0]
        c.execute("UPDATE pob_activities SET current_verification_id=%s WHERE id=%s", (vid, pob_id))

    # Gratification on auto-approve
    if auto_ok:
        from .. import rules
        gid, decision = rules.create_gratification(conn, pob_id, ctx.user["id"],
                                                   campaign_id, extracted_pob_amount, ctx.user["id"])
        notify_from_template(conn, ctx.user["id"], "pob.approved",
                             {"pob_id": pob_id, "campaign": campaign["name"]},
                             "pob", pob_id)

    conn.commit()
    log_action(conn, ctx.user["id"], "pob.invoice_submit", "pob_activity", pob_id,
               {"campaign_id": campaign_id, "product": matched_product.get("name"),
                "qty": extracted_qty, "auto_filled": True}, request=request)
    if status == "pending_verification":
        notify_from_template(conn, ctx.user["id"], "pob.submitted",
                             {"campaign": campaign["name"], "invoice_amount": invoice_amount},
                             "pob", pob_id)

    from ..webhooks import dispatch_event
    dispatch_event(conn, "pob.approved" if auto_ok else "pob.submitted", {
        "pob_id": pob_id, "verification_id": vid, "campaign_id": campaign_id,
        "campaign": campaign["name"], "invoice_number": invoice_number,
        "invoice_amount": invoice_amount, "status": status,
        "submitted_by": ctx.user["username"],
    }, ctx.claims.get("tenant_db", ""))

    return {
        "ok": True,
        "pob_id": pob_id,
        "verification_id": vid,
        "status": status,
        "auto_verified": auto_ok,
        "product_name": matched_product.get("name"),
        "quantity": extracted_qty,
        "pob_amount": extracted_pob_amount,
        "invoice_amount": extracted_line_amount or invoice_amount,
        "ocr_confidence": (ocr_verdict or {}).get("confidence"),
        "message": "Invoice uploaded and auto-verified" if auto_ok
                   else "Invoice uploaded — pending manual verification",
    }


@router.get("/pob/my-stats")
def my_pob_stats(ctx: TenantContext = Depends(require_permission("pob.submit"))):
    """Lightweight status counts of the current user's POBs, used for the
    verification-status badge in the navigation bar."""
    conn = ctx.conn
    c = conn.cursor()
    c.execute(
        """SELECT COALESCE(v.status, pa.status) AS status, count(*) AS n
           FROM pob_activities pa
           LEFT JOIN pob_verifications v ON v.id=pa.current_verification_id
           WHERE pa.user_id=%s GROUP BY 1""",
        (ctx.user["id"],),
    )
    counts = {}
    for st, n in c.fetchall():
        counts[st] = n
    pending = counts.get("pending", 0) + counts.get("pending_verification", 0)
    approved = counts.get("approved", 0) + counts.get("verified", 0)
    c.execute(
        """SELECT count(*), round(avg(EXTRACT(EPOCH FROM (proof_submitted_at - created_at)) / 86400))::int,
                  max(EXTRACT(EPOCH FROM (proof_submitted_at - created_at)) / 86400)::int
           FROM pob_activities WHERE user_id=%s AND proof_submitted_at IS NOT NULL""",
        (ctx.user["id"],),
    )
    lagged, avg_lag, max_lag = c.fetchone()
    return {
        "pending": pending,
        "approved": approved,
        "rejected": counts.get("rejected", 0),
        "submitted": counts.get("submitted", 0),
        "awaiting_proof": counts.get("submitted", 0),
        "proof_count": lagged or 0,
        "avg_proof_lag_days": avg_lag if avg_lag is not None else 0,
        "max_proof_lag_days": max_lag if max_lag is not None else 0,
    }


@router.get("/pob/mine")
def my_pobs(ctx: TenantContext = Depends(require_permission("pob.submit"))):
    conn = ctx.conn
    c = conn.cursor()
    c.execute(
        """SELECT pa.*, cmp.name AS campaign_name, pr.name AS product_name, ch.name AS chemist_name,
           ch.shop_name, v.status AS verification_status, v.reason AS verification_reason
           FROM pob_activities pa
           LEFT JOIN campaigns cmp ON cmp.id=pa.campaign_id
           LEFT JOIN products pr ON pr.id=pa.product_id
           LEFT JOIN chemists ch ON ch.id=pa.chemist_id
           LEFT JOIN pob_verifications v ON v.id=pa.current_verification_id
           WHERE pa.user_id=%s ORDER BY pa.id DESC""",
        (ctx.user["id"],),
    )
    items = fetchall_dict(c)
    _attach_proof_lag(items)
    return {"items": items}


# ── Two-phase POB: visit submission (no invoice) ────────────────────────────

@router.post("/pob/visit")
def submit_visit(body: dict, request: Request = None,
                 ctx: TenantContext = Depends(require_permission("pob.submit"))):
    """MR visits a chemist, picks an active campaign and enters brand-wise
    quantities. Creates one pob_activities row per brand, grouped by a shared
    submission_group, all in status 'submitted' (no invoice yet).

    body: {"campaign_id": int, "chemist_id": int, "remarks": str,
           "items": [{"product_id": int, "quantity": float}, ...]}
    """
    conn = ctx.conn
    c = conn.cursor()
    campaign_id = _coerce_int(body.get("campaign_id"))
    chemist_id = _coerce_int(body.get("chemist_id"))
    items = body.get("items") or []
    if not campaign_id or not chemist_id:
        raise HTTPException(400, "campaign_id and chemist_id required")
    if not items:
        raise HTTPException(400, "at least one brand item required")

    c.execute("SELECT * FROM campaigns WHERE id=%s", (campaign_id,))
    campaign = fetchone_dict(c)
    if not campaign or campaign["status"] not in ("active", "draft"):
        raise HTTPException(400, "Campaign is not active")

    upload_roles = (campaign.get("upload_roles") or "").strip()
    if upload_roles and upload_roles != "*":
        c.execute("SELECT r.name FROM roles r JOIN users u ON u.role_id=r.id WHERE u.id=%s", (ctx.user["id"],))
        row = c.fetchone()
        role_name = row[0] if row else ""
        allowed = {r.strip() for r in upload_roles.split(",") if r.strip()}
        if role_name not in allowed:
            raise HTTPException(403, f"Role '{role_name}' is not allowed to upload for this campaign (allowed: {', '.join(sorted(allowed))})")

    c.execute("SELECT * FROM chemists WHERE id=%s", (chemist_id,))
    if not c.fetchone():
        raise HTTPException(400, "Chemist not found")

    group = str(uuid.uuid4())
    total = 0.0
    pob_ids = []
    for item in items:
        product_id = _coerce_int(item.get("product_id"))
        quantity = float(item.get("quantity") or 0)
        if not product_id or quantity <= 0:
            raise HTTPException(400, "each brand item needs product_id and quantity > 0")
        c.execute("SELECT * FROM products WHERE id=%s", (product_id,))
        product = fetchone_dict(c)
        if not product or product["campaign_id"] != campaign_id:
            raise HTTPException(400, f"Product {product_id} does not belong to this campaign")

        ptr = float(product.get("ptr") or 0)
        mrp = float(product.get("mrp") or 0)
        pob_amount = round(quantity * ptr, 2)
        total += pob_amount
        if product.get("scheme_eligibility"):
            min_pob = product.get("min_pob") or 0
            max_pob = product.get("max_pob")
            if max_pob is not None and pob_amount > max_pob:
                raise HTTPException(400, f"POB amount exceeds the campaign max ({max_pob})")
            if min_pob and pob_amount < min_pob:
                raise HTTPException(400, f"POB amount is below the campaign minimum ({min_pob})")
            if product.get("min_quantity") and quantity < product["min_quantity"]:
                raise HTTPException(400, f"Quantity below the campaign minimum ({product['min_quantity']})")

        c.execute(
            """INSERT INTO pob_activities (user_id, campaign_id, product_id, chemist_id, quantity,
               ptr, mrp, invoice_amount, pob_amount, remarks, status, workflow_id, submission_group)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'submitted',%s,%s) RETURNING id""",
            (ctx.user["id"], campaign_id, product_id, chemist_id, quantity, ptr, mrp,
             pob_amount, pob_amount, body.get("remarks"), campaign.get("approval_workflow_id"), group),
        )
        pob_ids.append(c.fetchone()[0])

    conn.commit()
    log_action(conn, ctx.user["id"], "pob.visit_submit", "pob_activity", pob_ids[0],
               {"campaign_id": campaign_id, "chemist_id": chemist_id,
                "submission_group": group, "items": len(pob_ids), "total_amount": total},
               request=request)
    return {"ok": True, "submission_group": group, "pob_ids": pob_ids,
            "total_amount": round(total, 2), "status": "submitted"}


@router.get("/pob/pending-invoice")
def pending_invoice_pobs(ctx: TenantContext = Depends(require_permission("pob.submit"))):
    """Visits (grouped by campaign + chemist) that are still missing invoice
    proof, i.e. every row in the group has status 'submitted'."""
    conn = ctx.conn
    c = conn.cursor()
    c.execute(
        """SELECT pa.submission_group, pa.campaign_id, cmp.name AS campaign_name,
           pa.chemist_id, ch.name AS chemist_name, ch.shop_name, ch.city, ch.state, ch.mobile,
           count(*) AS item_count, sum(pa.pob_amount) AS total_amount,
           max(pa.created_at) AS created_at
           FROM pob_activities pa
           LEFT JOIN campaigns cmp ON cmp.id=pa.campaign_id
           LEFT JOIN chemists ch ON ch.id=pa.chemist_id
           WHERE pa.user_id=%s AND pa.status='submitted'
           GROUP BY pa.submission_group, pa.campaign_id, cmp.name, pa.chemist_id,
                    ch.name, ch.shop_name, ch.city, ch.state, ch.mobile
           ORDER BY max(pa.created_at) DESC""",
        (ctx.user["id"],),
    )
    return {"items": fetchall_dict(c)}


@router.get("/pob/pending-invoice/detail")
def pending_invoice_detail(campaign_id: int, chemist_id: int,
                           ctx: TenantContext = Depends(require_permission("pob.submit"))):
    """Previously submitted brand-wise values for a visit, to show before
    attaching the invoice proof."""
    conn = ctx.conn
    c = conn.cursor()
    c.execute(
        """SELECT pa.id, pa.quantity, pa.ptr, pa.mrp, pa.pob_amount, pa.invoice_amount,
           pr.name AS product_name, pr.sku, pr.pts, b.name AS brand_name
           FROM pob_activities pa
           JOIN products pr ON pr.id=pa.product_id
           LEFT JOIN brands b ON b.id=pr.brand_id
           WHERE pa.user_id=%s AND pa.campaign_id=%s AND pa.chemist_id=%s AND pa.status='submitted'
           ORDER BY pa.id""",
        (ctx.user["id"], campaign_id, chemist_id),
    )
    items = fetchall_dict(c)
    return {"items": items, "total_amount": round(sum(i["pob_amount"] or 0 for i in items), 2)}


@router.post("/pob/invoice-proof")
async def upload_invoice_proof(
    campaign_id: int = Form(...),
    chemist_id: int = Form(...),
    invoice_number: str = Form(""),
    invoice_date: str = Form(""),
    direct: int = Form(0),
    invoice: UploadFile = File(None),
    file: UploadFile = File(None),
    request: Request = None,
    ctx: TenantContext = Depends(require_permission("pob.submit")),
):
    """Attach the invoice proof to a submitted visit. All rows of the visit
    (same campaign + chemist, status 'submitted') move into the verification
    pipeline together.

    With ``direct=1`` (companies that upload proof without a prior POB visit)
    the brand lines are auto-created from the invoice OCR when no pending
    visit exists for the campaign + chemist."""
    invoice = invoice or file
    conn = ctx.conn
    c = conn.cursor()

    if not invoice or not invoice.filename:
        raise HTTPException(400, "invoice proof file required")
    data = await invoice.read()
    if not data:
        raise HTTPException(400, "Empty invoice file")
    try:
        validate_upload(data, filename=invoice.filename, allowed_kinds=DOCUMENT_KINDS | IMAGE_KINDS,
                        max_size=config.MAX_UPLOAD_SIZE)
    except UploadValidationError as exc:
        raise HTTPException(400, str(exc))

    c.execute("SELECT * FROM campaigns WHERE id=%s", (campaign_id,))
    campaign = fetchone_dict(c)
    if not campaign:
        raise HTTPException(404, "Campaign not found")
    c.execute("SELECT id FROM chemists WHERE id=%s", (chemist_id,))
    if not c.fetchone():
        raise HTTPException(400, "Chemist not found")

    c.execute(
        "SELECT id FROM pob_activities WHERE user_id=%s AND campaign_id=%s AND chemist_id=%s "
        "AND status='submitted' ORDER BY id",
        (ctx.user["id"], campaign_id, chemist_id),
    )
    rows = c.fetchall()
    pob_ids = [r[0] for r in rows]

    # Extract the invoice once (direct or visit flow) so the brand lines are
    # available for the composite duplicate check below and are reused by
    # _finalize_pob for each row (no per-row re-extraction of the document).
    from .. import ocr as _ocr
    extraction = {}
    try:
        extraction = _ocr.extract_fields(data, invoice.filename)
        record_ocr_usage(conn, ctx.user["id"], extraction, filename=invoice.filename,
                         invoice_number=invoice_number)
    except Exception:
        extraction = {}
    if extraction:
        fields = extraction.get("fields") or {}
        if not invoice_number:
            invoice_number = str(fields.get("invoice_number") or "").strip()
        if not invoice_date:
            invoice_date = _ocr._normalise_date(fields.get("invoice_date") or "")

    if not pob_ids:
        if not direct:
            raise HTTPException(404, "No pending POB found for this campaign & chemist")
        # Direct proof: auto-create the POB lines from the invoice OCR.
        pob_ids = _create_pobs_from_invoice(conn, ctx, campaign, chemist_id, extraction)

    # Whole-visit validation before any row is touched, so a violation cannot
    # leave the group half-updated.
    _validate_invoice_period(campaign, invoice_date)
    if invoice_number:
        dup = _find_invoice_duplicate(conn, chemist_id, invoice_number, invoice_date,
                                      ((extraction or {}).get("fields") or {}).get("items") or [],
                                      exclude_pob_ids=pob_ids)
        if dup:
            raise HTTPException(400, f"Duplicate invoice — invoice already extracted (POB #{dup[0]}). "
                                     f"The same invoice number, date and brand details were already "
                                     f"submitted for this chemist.")
    c.execute(
        "SELECT id FROM pob_activities WHERE content_hash=%s AND status<>'rejected' "
        "AND NOT (id = ANY(%s))",
        (_content_hash(data), pob_ids),
    )
    dup = c.fetchone()
    if dup:
        raise HTTPException(400, f"Identical invoice document already uploaded (POB #{dup[0]})")

    results = []
    for pob_id in pob_ids:
        status, vid, auto_ok = _finalize_pob(conn, ctx, campaign, pob_id, data,
                                             invoice.filename, invoice_number, invoice_date,
                                             exclude_ids=pob_ids, extraction=extraction)
        results.append({"pob_id": pob_id, "status": status,
                        "verification_id": vid, "auto_verified": auto_ok})
    conn.commit()
    log_action(conn, ctx.user["id"], "pob.invoice_proof", "pob_activity", pob_ids[0],
               {"campaign_id": campaign_id, "chemist_id": chemist_id,
                "submission_group": None, "count": len(pob_ids), "direct": bool(direct)},
               request=request)
    return {"ok": True, "results": results}


def _create_pobs_from_invoice(conn, ctx, campaign, chemist_id, extraction):
    """Direct invoice-proof flow (companies that upload the proof without a
    prior POB visit): create one pob_activities row per invoice brand line
    that matches an active campaign product. Returns the created pob ids."""
    from .. import ocr as _ocr
    items = ((extraction or {}).get("fields") or {}).get("items") or []
    if not items:
        raise HTTPException(400, "No item lines could be read from the invoice. "
                                 "Please upload a clear invoice photo/PDF, or submit a POB visit first.")
    c = conn.cursor()
    c.execute(
        """SELECT pr.*, b.name AS brand_name FROM products pr
           LEFT JOIN brands b ON b.id=pr.brand_id
           WHERE pr.campaign_id=%s AND pr.status='active' ORDER BY pr.name, pr.id""",
        (campaign["id"],),
    )
    products = fetchall_dict(c)
    if not products:
        raise HTTPException(400, "Campaign has no active products to match the invoice lines against")
    group = str(uuid.uuid4())
    created = []
    for item in items:
        pinfo = None
        for p in products:
            if _ocr.match_brand([item], p.get("name"), p.get("brand_name"), p.get("sku")):
                pinfo = p
                break
        if not pinfo:
            continue
        qty = _ocr._normalise_amount(item.get("qty")) or 1
        inv_amt = _ocr._normalise_amount(item.get("amount"))
        ptr = float(pinfo.get("ptr") or 0)
        pob_amt = round(qty * ptr, 2)
        c.execute(
            """INSERT INTO pob_activities (user_id, campaign_id, product_id, chemist_id, quantity,
               ptr, mrp, invoice_amount, pob_amount, status, workflow_id, submission_group)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'submitted',%s,%s) RETURNING id""",
            (ctx.user["id"], campaign["id"], pinfo["id"], chemist_id, qty, ptr,
             float(pinfo.get("mrp") or 0), inv_amt or pob_amt, pob_amt,
             campaign.get("approval_workflow_id"), group),
        )
        created.append(c.fetchone()[0])
    if not created:
        raise HTTPException(400, "None of the invoice lines matched an active campaign product. "
                                 "Please submit a POB visit first.")
    return created


@router.get("/pob/ocr")
def list_ocr(ctx: TenantContext = Depends(require_permission("pob.view"))):
    c = ctx.conn.cursor()
    scope_sql, params = scope_filter(ctx, ctx.conn, "pa.user_id")
    where = f"WHERE {scope_sql}" if scope_sql else ""
    c.execute(f"""SELECT o.*, pa.invoice_number, pa.invoice_amount, pa.status
                 FROM ocr_extractions o JOIN pob_activities pa ON pa.id=o.pob_id
                 {where} ORDER BY o.id DESC LIMIT 200""", params)
    return {"items": fetchall_dict(c)}


@router.get("/pob")
def list_pob(status: str = "", user_id: int = None, campaign_id: int = None,
             q: str = "", limit: int = PageLimit(), offset: int = PageOffset(),
             ctx: TenantContext = Depends(require_permission("pob.view"))):
    conn = ctx.conn
    c = conn.cursor()
    sql = """SELECT pa.*, u.full_name AS submitted_by, u.username AS submitted_username,
             cmp.name AS campaign_name, dv.name AS division_name, pr.name AS product_name,
             br.name AS brand_name, ch.name AS chemist_name, ch.shop_name,
             v.status AS verification_status, v.reason AS verification_reason,
             v.verifier_id, vu.full_name AS verifier_name
             FROM pob_activities pa
             LEFT JOIN users u ON u.id=pa.user_id
             LEFT JOIN campaigns cmp ON cmp.id=pa.campaign_id
             LEFT JOIN divisions dv ON dv.id=cmp.division_id
             LEFT JOIN products pr ON pr.id=pa.product_id
             LEFT JOIN brands br ON br.id=pr.brand_id
             LEFT JOIN chemists ch ON ch.id=pa.chemist_id
             LEFT JOIN pob_verifications v ON v.id=pa.current_verification_id
             LEFT JOIN users vu ON vu.id=v.verifier_id"""
    where, params = [], []
    sc, sp = scope_filter(ctx, conn, "pa.user_id")
    if sc:
        where.append(sc)
        params.extend(sp)
    if status:
        where.append("pa.status=%s")
        params.append(status)
    if user_id:
        where.append("pa.user_id=%s")
        params.append(user_id)
    if campaign_id:
        where.append("pa.campaign_id=%s")
        params.append(campaign_id)
    if q:
        where.append("(ch.name ILIKE %s OR pa.invoice_number ILIKE %s OR u.full_name ILIKE %s)")
        params.extend([f"%{q}%"] * 3)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY pa.id DESC LIMIT %s OFFSET %s"
    params.extend([limit, offset])
    c.execute(sql, params)
    items = fetchall_dict(c)
    _attach_hierarchy(conn, items)
    _attach_proof_lag(items)
    c.execute("SELECT count(*) FROM pob_activities")
    return {"items": items, "total": c.fetchone()[0]}


def _reporting_chains(conn):
    """Map user_id -> reporting chain from the user upward (self first, root last).
    Each entry: {full_name, username, level}."""
    c = conn.cursor()
    c.execute("""SELECT u.id, u.full_name, u.username, u.parent_id, h.name AS level_name
                 FROM users u LEFT JOIN hierarchy_levels h ON h.id=u.hierarchy_level_id""")
    users = {r[0]: {"full_name": r[1], "username": r[2], "parent_id": r[3], "level": r[4]} for r in c.fetchall()}
    chains = {}
    for uid in users:
        chain = []
        cur = uid
        seen = set()
        while cur and cur not in seen:
            seen.add(cur)
            node = users.get(cur)
            if not node:
                break
            chain.append({"full_name": node["full_name"], "username": node["username"], "level": node["level"]})
            cur = node["parent_id"]
        chains[uid] = chain
    return chains


def _attach_hierarchy(conn, items):
    """Attach submitter's hierarchy reporting details (level, manager, chain)
    to each POB row in place."""
    chains = _reporting_chains(conn)
    for it in items:
        chain = chains.get(it.get("user_id"), [])
        it["submitter_level"] = chain[0]["level"] if chain else None
        it["reports_to_name"] = chain[1]["full_name"] if len(chain) > 1 else None
        it["reporting_chain"] = " > ".join(f"{x['full_name']} ({x['level'] or '—'})" for x in chain) or None


@router.get("/pob/export")
def export_pob(status: str = "", q: str = "", include_hierarchy: int = 1,
               ctx: TenantContext = Depends(require_permission("pob.view"))):
    """Excel export of POB records (respects the caller's data scope). Includes
    every POB detail plus the submitter's hierarchy reporting chain."""
    conn = ctx.conn
    c = conn.cursor()
    sql = """SELECT pa.*, u.full_name AS submitted_by, u.username AS submitted_username,
             cmp.name AS campaign_name, dv.name AS division_name, pr.name AS product_name,
             br.name AS brand_name, ch.name AS chemist_name, ch.shop_name,
             v.status AS verification_status, v.reason AS verification_reason,
             vu.full_name AS verifier_name
             FROM pob_activities pa
             LEFT JOIN users u ON u.id=pa.user_id
             LEFT JOIN campaigns cmp ON cmp.id=pa.campaign_id
             LEFT JOIN divisions dv ON dv.id=cmp.division_id
             LEFT JOIN products pr ON pr.id=pa.product_id
             LEFT JOIN brands br ON br.id=pr.brand_id
             LEFT JOIN chemists ch ON ch.id=pa.chemist_id
             LEFT JOIN pob_verifications v ON v.id=pa.current_verification_id
             LEFT JOIN users vu ON vu.id=v.verifier_id"""
    where, params = [], []
    sc, sp = scope_filter(ctx, conn, "pa.user_id")
    if sc:
        where.append(sc)
        params.extend(sp)
    if status:
        where.append("pa.status=%s")
        params.append(status)
    if q:
        where.append("(ch.name ILIKE %s OR pa.invoice_number ILIKE %s OR u.full_name ILIKE %s)")
        params.extend([f"%{q}%"] * 3)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY pa.id DESC"
    c.execute(sql, params)
    items = fetchall_dict(c)
    if include_hierarchy:
        _attach_hierarchy(conn, items)
    _attach_proof_lag(items)

    wb = Workbook()
    ws = wb.active
    ws.title = "POB Records"
    ws.append([
        "POB ID", "Invoice Number", "Invoice Date", "Campaign", "Division", "Brand", "Product",
        "Chemist", "Shop", "Submitted By", "Username", "Submitter Level", "Reports To",
        "Reporting Chain", "Quantity", "PTR", "MRP", "Invoice Amount", "POB Amount",
        "Status", "Verification Reason", "Verifier", "Remarks", "Submission Group", "Submitted At",
        "Proof Submitted At", "Proof Lag (days)",
    ])
    for it in items:
        chain = it.get("reporting_chain") if include_hierarchy else None
        ws.append([
            it.get("id"), it.get("invoice_number"), it.get("invoice_date"),
            it.get("campaign_name"), it.get("division_name"), it.get("brand_name"), it.get("product_name"),
            it.get("chemist_name"), it.get("shop_name"),
            it.get("submitted_by"), it.get("submitted_username"),
            it.get("submitter_level") if include_hierarchy else None,
            it.get("reports_to_name") if include_hierarchy else None,
            chain,
            it.get("quantity"), it.get("ptr"), it.get("mrp"),
            it.get("invoice_amount"), it.get("pob_amount"),
            it.get("verification_status") or it.get("status"),
            it.get("verification_reason"), it.get("verifier_name"),
            it.get("remarks"), it.get("submission_group"), it.get("created_at"),
            it.get("proof_submitted_at"),
            it.get("proof_lag_days") if it.get("proof_lag_days") is not None else "",
        ])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=pob_records.xlsx"},
    )


@router.get("/pob/{pob_id}")
def get_pob(pob_id: int, ctx: TenantContext = Depends(get_tenant_context)):
    conn = ctx.conn
    c = conn.cursor()
    c.execute(
        """SELECT pa.*, u.full_name AS submitted_by, u.username AS submitted_username,
           cmp.name AS campaign_name, cmp.status AS campaign_status,
           cmp.start_date AS campaign_start, cmp.end_date AS campaign_end,
           cmp.period_type AS campaign_period_type, cmp.grace_days AS campaign_grace_days,
           cmp.grace_months AS campaign_grace_months, cmp.pre_grace_days AS campaign_pre_grace_days,
           cmp.scheme_type, cmp.invoice_verification_required,
           cmp.auto_verify AS campaign_auto_verify,
           cmp.auto_verify_confidence AS campaign_auto_verify_confidence,
           pr.name AS product_name, ch.name AS chemist_name,
           ch.shop_name, ch.mobile AS chemist_mobile, ch.city AS chemist_city,
           ch.state AS chemist_state FROM pob_activities pa
           LEFT JOIN users u ON u.id=pa.user_id
           LEFT JOIN campaigns cmp ON cmp.id=pa.campaign_id
           LEFT JOIN products pr ON pr.id=pa.product_id
           LEFT JOIN chemists ch ON ch.id=pa.chemist_id
           WHERE pa.id=%s""",
        (pob_id,),
    )
    row = fetchone_dict(c)
    if not row:
        raise HTTPException(404, "POB not found")
    if ctx.has("pob.view"):
        visible = visible_user_ids(conn, ctx)
        if visible is not None and row["user_id"] not in visible:
            raise HTTPException(403, "not allowed to view this POB")
    elif not (ctx.has("pob.submit") and row["user_id"] == ctx.user["id"]):
        raise HTTPException(403, "Missing permission: pob.view")
    c.execute(
        """SELECT h.*, u.full_name AS verifier_name FROM verification_history h
           LEFT JOIN users u ON u.id=h.verifier_id
           WHERE h.pob_id=%s ORDER BY h.id""",
        (pob_id,),
    )
    row["history"] = fetchall_dict(c)
    c.execute("SELECT * FROM gratifications WHERE pob_id=%s", (pob_id,))
    row["gratifications"] = fetchall_dict(c)
    # AI-extracted invoice fields (see saas/ocr.py) and the sibling product
    # lines from the same visit -- both were previously only returned by
    # /api/v1/verification/{id} (the agent-side view), so the submitting
    # user's own POB detail popup had no extraction/product-table data to
    # show at all. Mirrors verification.py's verification_detail().
    c.execute("SELECT * FROM ocr_extractions WHERE pob_id=%s ORDER BY id", (pob_id,))
    row["ocr"] = fetchall_dict(c)
    for o in row["ocr"]:
        for k in ("fields", "raw"):
            if isinstance(o.get(k), str):
                try:
                    import json
                    o[k] = json.loads(o[k])
                except Exception:
                    o[k] = {}
    row["lines"] = _pob_lines(conn, row)
    # Campaign product master (brand-wise qty/amount bounds) so the POB detail
    # can show what the campaign expects per brand alongside the submission.
    c.execute(
        """SELECT pr.*, b.name AS brand_name FROM products pr
           LEFT JOIN brands b ON b.id=pr.brand_id
           WHERE pr.campaign_id=%s AND pr.status='active' ORDER BY pr.name, pr.id""",
        (row["campaign_id"],),
    )
    row["campaign_products"] = fetchall_dict(c)
    # Link each submitted line to its AI-extracted invoice line (brand_matched,
    # extracted_qty/amount) so the detail can explain the verification outcome.
    from .verification import _annotate_brand_matches
    _annotate_brand_matches(row)
    _attach_hierarchy(conn, [row])
    _attach_proof_lag([row])
    # Full Invoice Proof verification report (extraction + business checks +
    # campaign eligibility) — shared with the agent's Verification detail.
    from ..verification_checks import compute_checks
    row["report"] = compute_checks(conn, row)
    return row


@router.get("/pob/{pob_id}/eligibility")
def pob_eligibility(pob_id: int, ctx: TenantContext = Depends(get_tenant_context)):
    """Return gratification eligibility and proximity info for a POB.

    Shows whether the POB's chemist qualifies for gratification, and if not,
    which conditions are unmet and by how much (shortfall).
    """
    from ..rules import build_rule_context, evaluate_proximity
    conn = ctx.conn
    c = conn.cursor()
    c.execute(
        """SELECT pa.*, cmp.id AS campaign_id, cmp.name AS campaign_name,
           cmp.scheme_type FROM pob_activities pa
           LEFT JOIN campaigns cmp ON cmp.id=pa.campaign_id
           WHERE pa.id=%s""",
        (pob_id,),
    )
    pob = fetchone_dict(c)
    if not pob:
        raise HTTPException(404, "POB not found")
    if ctx.has("pob.view"):
        visible = visible_user_ids(conn, ctx)
        if visible is not None and pob["user_id"] not in visible:
            raise HTTPException(403, "not allowed to view this POB")
    elif not (ctx.has("pob.submit") and pob["user_id"] == ctx.user["id"]):
        raise HTTPException(403, "Missing permission: pob.view")

    # Build individual + aggregate context
    ctx_dict = build_rule_context(pob, [])
    # Aggregate: sum of all verified POBs for same chemist+campaign+month
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
    """, (pob["chemist_id"], pob["campaign_id"], pob["created_at"]))
    row = c.fetchone()
    agg = {
        "chemist_monthly_total_invoice": float(row[0] or 0),
        "chemist_monthly_total_pob": float(row[1] or 0),
        "chemist_monthly_total_qty": int(row[2] or 0),
        "chemist_monthly_pob_count": int(row[3] or 0),
        "chemist_monthly_invoice_count": int(row[4] or 0),
    }
    ctx_dict.update(agg)

    # Check if gratification already exists
    c.execute("SELECT id, type_code, scheme_value, status FROM gratifications WHERE pob_id=%s", (pob_id,))
    existing = fetchone_dict(c)

    result = evaluate_proximity(conn, pob["campaign_id"], ctx_dict)
    result["pob_id"] = pob_id
    result["chemist_name"] = None
    c.execute("SELECT name FROM chemists WHERE id=%s", (pob["chemist_id"],))
    cn = c.fetchone()
    if cn:
        result["chemist_name"] = cn[0]
    result["campaign_name"] = pob.get("campaign_name")
    result["aggregate"] = agg
    result["gratification"] = existing

    # ── Per-product constraint check (min_quantity, min_pob, max_pob) ──
    # Even if the gratification rule matches at campaign level, individual
    # products may not meet their own min/max thresholds.
    c.execute("""
        SELECT pr.id, pr.name AS product_name, pr.min_quantity, pr.min_pob, pr.max_pob
        FROM products pr
        WHERE pr.campaign_id = %s AND pr.status = 'active'
    """, (pob["campaign_id"],))
    products = fetchall_dict(c)

    product_warnings = []
    product_details = []
    for p in products:
        pid = p["id"]
        # Sum this chemist's verified purchases for this product in the month
        c.execute("""
            SELECT COALESCE(SUM(quantity), 0) AS qty,
                   COALESCE(SUM(pob_amount), 0) AS pob_value,
                   COALESCE(SUM(invoice_amount), 0) AS invoice_value
            FROM pob_activities
            WHERE chemist_id = %s AND campaign_id = %s AND product_id = %s
              AND status = 'verified'
              AND date_trunc('month', created_at) = date_trunc('month', %s::timestamp)
        """, (pob["chemist_id"], pob["campaign_id"], pid, pob["created_at"]))
        prow = c.fetchone()
        purchased_qty = int(prow[0] or 0)
        purchased_pob = float(prow[1] or 0)
        purchased_inv = float(prow[2] or 0)

        min_qty = p.get("min_quantity") or 0
        min_pob_val = float(p.get("min_pob") or 0)
        max_pob_val = float(p["max_pob"]) if p.get("max_pob") is not None else None

        warnings = []
        qty_pct = 0
        if min_qty > 0:
            qty_pct = min(100, round((purchased_qty / min_qty) * 100)) if min_qty else 100
            if purchased_qty < min_qty:
                shortfall = min_qty - purchased_qty
                warnings.append({
                    "type": "quantity",
                    "message": f"Need {shortfall} more unit{'s' if shortfall > 1 else ''} of {p['product_name']}",
                    "current": purchased_qty,
                    "required": min_qty,
                    "shortfall": shortfall,
                })

        pob_pct = 0
        if min_pob_val > 0:
            pob_pct = min(100, round((purchased_pob / min_pob_val) * 100)) if min_pob_val else 100
            if purchased_pob < min_pob_val:
                shortfall = min_pob_val - purchased_pob
                warnings.append({
                    "type": "pob_value",
                    "message": f"{p['product_name']} needs ₹{shortfall:,.0f} more POB value",
                    "current": purchased_pob,
                    "required": min_pob_val,
                    "shortfall": shortfall,
                })

        if max_pob_val is not None and purchased_pob > max_pob_val:
            warnings.append({
                "type": "max_pob",
                "message": f"{p['product_name']} exceeds maximum POB value of ₹{max_pob_val:,.0f}",
                "current": purchased_pob,
                "required": max_pob_val,
                "shortfall": 0,
            })

        product_warnings.extend(warnings)
        product_details.append({
            "product_id": pid,
            "product_name": p["product_name"],
            "min_quantity": min_qty,
            "min_pob": min_pob_val,
            "max_pob": max_pob_val,
            "purchased_qty": purchased_qty,
            "purchased_pob": purchased_pob,
            "purchased_invoice": purchased_inv,
            "qty_pct": qty_pct,
            "pob_pct": pob_pct,
            "meets_all": len(warnings) == 0,
        })

    result["product_warnings"] = product_warnings
    result["product_details"] = product_details

    # Update message to include product-level info
    if result["eligible"] and product_warnings:
        warn_msgs = [w["message"] for w in product_warnings]
        result["message"] = f"Eligible for gratification, but: {'; '.join(warn_msgs)}"
    elif not result["eligible"] and product_warnings:
        # Merge rule shortfalls + product warnings
        all_msgs = []
        for s in result.get("shortfalls", []):
            all_msgs.append(s.get("message", ""))
        for w in product_warnings:
            all_msgs.append(w["message"])
        result["message"] = "To qualify: " + "; ".join(all_msgs)

    return result


@router.post("/pob/{pob_id}/re-extract")
async def re_extract_pob(
    pob_id: int,
    invoice: UploadFile = File(None),
    file: UploadFile = File(None),
    invoice_number: str = Form(""),
    invoice_date: str = Form(""),
    request: Request = None,
    ctx: TenantContext = Depends(get_tenant_context),
):
    """Replace / re-extract the invoice of an existing POB when the original
    AI extraction read the wrong details.

    - Uploads a new invoice to replace the stored one, OR re-runs Gemini on
      the already-stored invoice.
    - Refreshes the invoice fields (number, date), brand-matched qty/amount,
      and the stored ocr_extractions row.
    - Reopens a rejected / needs_review POB back to pending_verification and
      re-runs auto-verify when the campaign has it enabled.
    """
    invoice = invoice or file
    conn = ctx.conn
    c = conn.cursor()

    c.execute("SELECT * FROM pob_activities WHERE id=%s", (pob_id,))
    pob = fetchone_dict(c)
    if not pob:
        raise HTTPException(404, "POB not found")
    # Same permission model as get_pob: verifier/admin (pob.view, scoped) or
    # the POB's own submitter (pob.submit).
    if ctx.has("pob.view"):
        visible = visible_user_ids(conn, ctx)
        if visible is not None and pob["user_id"] not in visible:
            raise HTTPException(403, "not allowed to re-extract this POB")
    elif not (ctx.has("pob.submit") and pob["user_id"] == ctx.user["id"]):
        raise HTTPException(403, "Missing permission: pob.view")
    if pob["status"] in ("verified", "approved", "paid", "completed", "duplicate"):
        raise HTTPException(409, f"POB is already {pob['status']}; re-extraction is only "
                                 f"allowed while it is pending or rejected")

    c.execute("SELECT * FROM campaigns WHERE id=%s", (pob["campaign_id"],))
    campaign = fetchone_dict(c) or {}

    # 1) Resolve the invoice bytes: a new upload replaces the stored one,
    #    otherwise re-run against the stored document.
    if invoice is not None and invoice.filename:
        data = await invoice.read()
        if not data:
            raise HTTPException(400, "Empty invoice file")
        try:
            validate_upload(data, filename=invoice.filename, allowed_kinds=DOCUMENT_KINDS | IMAGE_KINDS,
                            max_size=config.MAX_UPLOAD_SIZE)
        except UploadValidationError as exc:
            raise HTTPException(400, str(exc))
        filename = invoice.filename
        invoice_path = storage.save(data, ctx.claims["tenant_db"], "invoices", filename)
        content_hash = _content_hash(data)
        replaced = True
    else:
        if not pob.get("invoice_path"):
            raise HTTPException(400, "No invoice on file to re-extract; upload a replacement invoice instead")
        data = storage.read(pob["invoice_path"])
        if not data:
            raise HTTPException(400, "Stored invoice could not be read; upload a replacement invoice instead")
        filename = pob.get("invoice_original_name") or "invoice"
        invoice_path = pob["invoice_path"]
        content_hash = pob.get("content_hash")
        replaced = False

    # 2) Re-run the AI extraction on the (new or stored) document.
    from .. import ocr as _ocr
    from .. import config as _cfg
    extraction = {}
    if _cfg.GOOGLE_API_KEY or _cfg.OCR_PROVIDER == "text":
        try:
            extraction = _ocr.extract_fields(data, filename)
            record_ocr_usage(conn, ctx.user["id"], extraction, filename=filename,
                             invoice_number=pob.get("invoice_number") or "")
        except Exception:
            extraction = {}
    if not extraction:
        raise HTTPException(400, "Re-extraction produced no invoice fields. Check the invoice "
                                 "is readable and that an OCR provider is configured.")
    fields = extraction.get("fields") or {}
    if not invoice_number:
        invoice_number = str(fields.get("invoice_number") or "").strip()
    if not invoice_date:
        invoice_date = _ocr._normalise_date(fields.get("invoice_date") or "")

    # 3) Brand-match: sync qty / invoice amount / POB amount to the invoice.
    items = fields.get("items") or []
    new_qty = pob.get("quantity")
    new_inv_amt = _ocr._normalise_amount(fields.get("invoice_amount")) or pob.get("invoice_amount")
    new_pob_amt = pob.get("pob_amount")
    if items:
        c.execute(
            """SELECT pr.name AS product_name, b.name AS brand_name
               FROM products pr LEFT JOIN brands b ON b.id=pr.brand_id
               WHERE pr.id=%s""", (pob["product_id"],))
        pinfo = fetchone_dict(c) or {}
        matched = _ocr.match_brand(items, pinfo.get("product_name"), pinfo.get("brand_name"))
        if matched:
            nq = _ocr._normalise_amount(matched.get("qty")) or float(pob["quantity"] or 0)
            na = _ocr._normalise_amount(matched.get("amount"))
            if nq != float(pob["quantity"] or 0):
                new_qty = nq
                new_pob_amt = round(nq * float(pob["ptr"] or 0), 2)
            if na:
                new_inv_amt = na

    # 4) Reopen a rejected / needs_review POB; keep pending ones as-is.
    status = "pending_verification"
    if pob["status"] in ("rejected", "needs_review"):
        c.execute("UPDATE pob_verifications SET status='pending', verifier_id=NULL, "
                  "verified_at=NULL, reason=NULL WHERE pob_id=%s", (pob_id,))
    elif pob["status"] == "submitted":
        c.execute("SELECT id FROM pob_verifications WHERE pob_id=%s", (pob_id,))
        if not c.fetchone():
            c.execute("INSERT INTO pob_verifications (pob_id, status) VALUES (%s,'pending') RETURNING id", (pob_id,))
            vid = c.fetchone()[0]
            c.execute("UPDATE pob_activities SET current_verification_id=%s WHERE id=%s", (vid, pob_id))

    # 5) Persist refreshed POB + invoice + extraction.
    c.execute(
        """UPDATE pob_activities SET status=%s, invoice_path=%s, invoice_original_name=%s,
           invoice_number=%s, invoice_date=%s, content_hash=%s, invoice_number_norm=%s,
           quantity=%s, invoice_amount=%s, pob_amount=%s WHERE id=%s""",
        (status, invoice_path, filename, invoice_number, invoice_date or None, content_hash,
         _normalise_invoice_no(invoice_number), new_qty, new_inv_amt, new_pob_amt, pob_id),
    )
    c.execute("DELETE FROM ocr_extractions WHERE pob_id=%s", (pob_id,))
    c.execute(
        """INSERT INTO ocr_extractions (pob_id, engine, raw, fields, confidence)
           VALUES (%s,%s,%s,%s,%s)""",
        (pob_id, extraction.get("engine"), json.dumps(extraction.get("raw") or {}),
         json.dumps(extraction.get("fields") or {}), extraction.get("confidence")),
    )

    # 6) Auto-verify when the campaign enables it.
    auto_ok = False
    ocr_verdict = None
    if campaign.get("auto_verify") and extraction:
        c.execute("SELECT name FROM products WHERE id=%s", (pob["product_id"],))
        pname_row = c.fetchone()
        pname = pname_row[0] if pname_row else ""
        c.execute("SELECT name, shop_name FROM chemists WHERE id=%s", (pob["chemist_id"],))
        ch = fetchone_dict(c) or {}
        c.execute("SELECT min_quantity, min_pob, max_pob FROM products WHERE id=%s", (pob["product_id"],))
        pr = fetchone_dict(c) or {}
        is_dup = bool(_find_invoice_duplicate(conn, pob["chemist_id"], invoice_number, invoice_date,
                                              (extraction.get("fields") or {}).get("items") or [],
                                              exclude_pob_ids=(pob_id,)))
        auto_ctx = {
            "chemist_name": ch.get("name") or "",
            "shop_name": ch.get("shop_name") or "",
            "ptr": pob.get("ptr"),
            "is_duplicate": is_dup,
            "campaign_start": campaign.get("start_date"),
            "campaign_end": campaign.get("end_date"),
            "campaign_grace_days": campaign.get("grace_days"),
            "campaign_grace_months": campaign.get("grace_months"),
            "campaign_pre_grace_days": campaign.get("pre_grace_days"),
            "min_quantity": pr.get("min_quantity"),
            "min_pob": pr.get("min_pob"),
            "max_pob": pr.get("max_pob"),
        }
        verdict = _ocr.verify_invoice(
            {"invoice_number": invoice_number, "invoice_amount": new_inv_amt,
             "invoice_date": invoice_date,
             "product_name": pname,
             "quantity": new_qty, "pob_amount": new_pob_amt},
            extraction, float(campaign.get("auto_verify_confidence") or 0.9),
            context=auto_ctx)
        ocr_verdict = dict(verdict)
        if verdict["ok"]:
            auto_ok = True
            status = "verified"
            c.execute("UPDATE pob_activities SET status='verified', auto_verified=TRUE, "
                      "confidence=%s, verification_state='auto_verified' WHERE id=%s",
                      (verdict["confidence"], pob_id))

    # 7) Close / seed the verification record + gratification + history.
    if auto_ok:
        c.execute("SELECT id FROM pob_verifications WHERE pob_id=%s", (pob_id,))
        row = c.fetchone()
        if row:
            vid = row[0]
            c.execute("UPDATE pob_verifications SET status='approved', reason=%s, "
                      "pipeline_status='completed', verified_at=CURRENT_TIMESTAMP WHERE id=%s",
                      ("Auto-verified by OCR (re-extract)", vid))
        else:
            c.execute("INSERT INTO pob_verifications (pob_id, status, reason, pipeline_status) "
                      "VALUES (%s,'approved',%s,'completed') RETURNING id",
                      (pob_id, "Auto-verified by OCR (re-extract)"))
            vid = c.fetchone()[0]
        c.execute("UPDATE pob_activities SET current_verification_id=%s, workflow_done=TRUE "
                  "WHERE id=%s", (vid, pob_id))
        c.execute("UPDATE pob_approvals SET status='skipped' WHERE pob_id=%s", (pob_id,))
        from .. import rules
        gid, decision = rules.create_gratification(conn, pob_id, ctx.user["id"],
                                                   pob["campaign_id"], new_pob_amt, ctx.user["id"])
        c.execute("INSERT INTO verification_history (pob_id, verifier_id, action, reason) "
                  "VALUES (%s,%s,'approved',%s)",
                  (pob_id, ctx.user["id"], "Auto-verified by OCR (re-extract)"))
        log_action(conn, ctx.user["id"], "verification.auto_approve", "pob_activity", pob_id,
                   {"confidence": ocr_verdict["confidence"], "gratification_id": gid,
                    "rule": decision.get("rule_name")})
        notify_from_template(conn, ctx.user["id"], "pob.approved",
                             {"pob_id": pob_id, "campaign": campaign["name"]}, "pob", pob_id)
    else:
        c.execute("SELECT id FROM pob_verifications WHERE pob_id=%s AND status='pending'", (pob_id,))
        row = c.fetchone()
        if row:
            vid = row[0]
            c.execute("UPDATE pob_verifications SET pipeline_status='pending_agent' WHERE id=%s", (vid,))
        else:
            c.execute("INSERT INTO pob_verifications (pob_id, status, pipeline_status) "
                      "VALUES (%s,'pending','pending_agent') RETURNING id", (pob_id,))
            vid = c.fetchone()[0]
            c.execute("UPDATE pob_activities SET current_verification_id=%s, verification_state='manual_review' "
                      "WHERE id=%s", (vid, pob_id))
        notify_from_template(conn, ctx.user["id"], "pob.needs_review",
                             {"pob_id": pob_id, "campaign": campaign["name"]}, "pob", pob_id)

    c.execute("INSERT INTO verification_history (pob_id, verifier_id, action, reason) "
              "VALUES (%s,%s,%s,%s)",
              (pob_id, ctx.user["id"], "re_extracted",
               f"Invoice re-extracted ({filename})" + (" — invoice replaced" if replaced else "")))
    conn.commit()

    log_action(conn, ctx.user["id"], "pob.re_extract", "pob_activity", pob_id,
               {"campaign_id": pob["campaign_id"], "status": status, "replaced": replaced},
               request=request)
    if not auto_ok:
        notify_from_template(conn, ctx.user["id"], "pob.submitted",
                             {"campaign": campaign["name"],
                              "invoice_amount": new_inv_amt or new_pob_amt}, "pob", pob_id)
        notify_from_template(conn, ctx.user["id"], "pob.resubmitted",
                             {"pob_id": pob_id, "campaign": campaign["name"],
                              "invoice_number": invoice_number}, "pob", pob_id)
    from ..webhooks import dispatch_event
    dispatch_event(conn, "pob.approved" if auto_ok else "pob.submitted", {
        "pob_id": pob_id, "verification_id": vid, "campaign_id": pob["campaign_id"],
        "campaign": campaign["name"], "invoice_number": invoice_number,
        "invoice_amount": new_inv_amt or new_pob_amt, "status": status,
        "submitted_by": ctx.user["username"],
    }, ctx.claims.get("tenant_db", ""))

    return {
        "ok": True, "pob_id": pob_id, "status": status, "auto_verified": auto_ok,
        "ocr_confidence": (ocr_verdict or {}).get("confidence"),
        "invoice_number": invoice_number, "invoice_date": invoice_date,
        "invoice_amount": new_inv_amt, "quantity": new_qty, "pob_amount": new_pob_amt,
        "replaced": replaced, "extraction": extraction,
    }


def _pob_lines(conn, row):
    """All product lines of the same visit (submission group when available,
    otherwise same user + campaign + chemist). Shared with
    saas/routers/verification.py's identical helper -- kept local here to
    avoid a cross-router import."""
    c = conn.cursor()
    lines_sql = """
      SELECT pa.id, pa.quantity, pa.ptr, pa.mrp, pa.invoice_amount, pa.pob_amount, pa.status,
             pr.name AS product_name, pr.sku, pr.pts, b.name AS brand_name
      FROM pob_activities pa
      LEFT JOIN products pr ON pr.id=pa.product_id
      LEFT JOIN brands b ON b.id=pr.brand_id
    """
    if row.get("submission_group"):
        c.execute(lines_sql + " WHERE pa.submission_group=%s ORDER BY pa.id", (row["submission_group"],))
    else:
        c.execute(lines_sql + " WHERE pa.user_id=%s AND pa.campaign_id=%s AND pa.chemist_id=%s ORDER BY pa.id",
                  (row["user_id"], row["campaign_id"], row["chemist_id"]))
    return fetchall_dict(c)
