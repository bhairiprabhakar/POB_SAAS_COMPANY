"""
Invoice Proof verification report.

Keeps three concepts separate (the UI must not conflate them):

  1. INVOICE EXTRACTION   - "what does the document say?" (AI evidence)
  2. BUSINESS VERIFICATION - "does the document support the submitted POB?"
  3. CAMPAIGN VALIDATION  - "is this POB eligible under the campaign rules?"

compute_checks(conn, row) renders one nested report consumed by both the agent
Verification detail and the MR's POB detail, so every surface shows the same
checklist, numbers and status. ``row`` is the merged POB dict returned by
pob.py's get_pob / verification.py's verification_detail (it carries the
campaign period fields and campaign_products).

Every check has a three-state outcome:

  * "pass" - verified against evidence
  * "fail" - contradicted by evidence (blocker for the agent)
  * "na"   - no evidence to compare / needs manual review
"""
from datetime import date as _date, timedelta

from . import ocr as _ocr


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _norm_name(value):
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def _parse_date(value):
    if not value:
        return None
    for cand in (str(value), str(value)[:10]):
        try:
            return _date.fromisoformat(cand)
        except ValueError:
            continue
    return None


def _iso(value):
    d = _parse_date(value)
    return d.isoformat() if d else (str(value).strip() if value is not None else None)


def _money(value):
    if value is None:
        return "—"
    try:
        n = float(value)
    except (TypeError, ValueError):
        return str(value)
    s = f"{n:,.2f}".rstrip("0").rstrip(".")
    return f"₹{s}"


def _fields(row):
    ocr = row.get("ocr") or []
    o = ocr[-1] if ocr else None
    if not o:
        return {}
    f = o.get("fields") or {}
    return f if isinstance(f, dict) else {}


def _extraction_meta(row):
    ocr = row.get("ocr") or []
    o = ocr[-1] if ocr else None
    if not o:
        return None
    return {
        "engine": o.get("engine"),
        "confidence": float(o.get("confidence") or 0),
        "error": o.get("error"),
    }


def _invoice_items(row):
    return _fields(row).get("items") or []


def _invoice_chemist(fields):
    for k in ("chemist_name", "chemist", "party_name"):
        if fields.get(k):
            return str(fields[k])
    b = fields.get("buyer") or {}
    if isinstance(b, dict) and b.get("name"):
        return str(b["name"])
    return None


def _invoice_number(fields):
    for k in ("invoice_number", "invoice_no"):
        if fields.get(k):
            return str(fields[k])
    return None


def _invoice_date(fields):
    for k in ("invoice_date",):
        if fields.get(k):
            return str(fields[k])
    return None


def _invoice_amount(fields):
    for k in ("invoice_amount", "total_amount", "grand_total", "amount"):
        v = fields.get(k)
        if v is not None:
            n = _num(v)
            if n is not None:
                return n
    return None


def _product_row(row):
    for p in row.get("campaign_products") or []:
        if p.get("id") == row.get("product_id"):
            return p
    return None


def _matched_item(row, items):
    """The extracted invoice line for this POB's product (brand + SKU aware)."""
    if not items:
        return None
    p = _product_row(row)
    product = row.get("product_name") or (p or {}).get("name")
    brand = (p or {}).get("brand_name") if p else None
    sku = (p or {}).get("sku") if p else None
    for it in items:
        if _ocr.match_brand([it], product, brand or "", sku or ""):
            return it
    return None


def _find_duplicate(conn, row):
    """Mirror the submit-time composite duplicate rule so the report never
    flags a POB the submit flow would accept: same chemist + invoice number +
    invoice date + brand lines (see pob._find_invoice_duplicate). Returns
    (id, status) of the earlier POB or None."""
    from saas.routers.pob import _find_invoice_duplicate
    norm = "".join(ch for ch in str(row.get("invoice_number") or "").upper() if ch.isalnum())
    if not norm:
        return None
    pob_id = row.get("id") or row.get("pob_id")
    return _find_invoice_duplicate(conn, row.get("chemist_id"), row.get("invoice_number"),
                                   row.get("invoice_date"), _invoice_items(row),
                                   exclude_pob_ids=(pob_id,))


def compute_checks(conn, row) -> dict:
    """Full Invoice Proof verification report for a POB row."""
    fields = _fields(row)
    items = _invoice_items(row)
    meta = _extraction_meta(row)
    p = _product_row(row)

    has_doc = bool(meta) and bool(fields)
    has_line_items = bool(items)
    extraction_failed = bool(meta) and not fields and not (meta.get("confidence") or 0) > 0

    sub_name = row.get("product_name")
    sub_chemist = row.get("chemist_name") or row.get("shop_name")
    sub_qty = _num(row.get("quantity"))
    sub_ptr = _num(row.get("ptr"))
    sub_amt = _num(row.get("pob_amount")) or _num(row.get("invoice_amount")) or 0.0

    # ── matched invoice line (eligible campaign product only) ──────────────
    item = _matched_item(row, items)
    inv_desc = (item or {}).get("description") if item else None
    inv_qty = _num(item.get("qty")) if item else None
    inv_rate = _num(item.get("rate")) if item else None
    if inv_rate is None and item:
        inv_rate = _num(item.get("ptr")) or _num(item.get("price"))
    inv_amt = _num(item.get("amount")) if item else None
    if inv_amt is None and item and inv_qty is not None and inv_rate is not None:
        inv_amt = round(inv_qty * inv_rate, 2)

    # ── top-level document status ───────────────────────────────────────────
    status = row.get("verification_status") or row.get("v_status") or row.get("status") or "submitted"
    if status in ("verified", "approved", "paid"):
        top = "verified"
    elif status == "rejected":
        top = "rejected"
    elif status == "duplicate":
        top = "duplicate"
    elif extraction_failed:
        top = "extraction_failed"
    elif meta:
        top = "ready"
    else:
        top = "no_invoice"

    # ── product ─────────────────────────────────────────────────────────────
    if item:
        product_state, product_detail = "pass", f"Matched invoice line “{inv_desc}”"
    elif has_line_items:
        product_state, product_detail = "fail", f"“{sub_name}” not found on the submitted invoice"
    elif has_doc:
        product_state, product_detail = "na", "No itemized lines extracted from the invoice"
    else:
        product_state, product_detail = "na", "No invoice data to compare"

    # ── quantity ────────────────────────────────────────────────────────────
    if inv_qty is not None:
        qty_diff = round(inv_qty - (sub_qty or 0), 2)
        qty_state = "pass" if qty_diff == 0 else "fail"
        qty_detail = f"submitted {sub_qty} · invoice {inv_qty} · difference {qty_diff:+g}"
    else:
        qty_state, qty_diff = "na", None
        qty_detail = "No matching line on the invoice" if has_line_items else "No invoice line data"

    # ── PTR ─────────────────────────────────────────────────────────────────
    if inv_rate is not None and sub_ptr is not None:
        ptr_state = "pass" if abs(inv_rate - sub_ptr) <= 0.01 else "fail"
        ptr_detail = f"submitted {_money(sub_ptr)} · invoice {_money(inv_rate)}"
    else:
        ptr_state = "na"
        ptr_detail = "No rate/PTR on the invoice line"

    # ── POB amount (eligible product line only, NOT the grand total) ────────
    if inv_amt is not None:
        amt_diff = round(inv_amt - sub_amt, 2)
        amt_state = "pass" if amt_diff == 0 else "fail"
        amt_detail = f"submitted {_money(sub_amt)} · invoice line {_money(inv_amt)} · difference {amt_diff:+g}"
        amt_note = ("Compared the eligible campaign product line only — the invoice grand total "
                    "can include other products.")
    else:
        amt_state, amt_diff, amt_note = "na", None, None
        amt_detail = "No line amount on the invoice"

    # ── chemist ─────────────────────────────────────────────────────────────
    inv_chemist = _invoice_chemist(fields)
    if inv_chemist:
        a, b = _norm_name(sub_chemist or ""), _norm_name(inv_chemist)
        chem_match = bool(a and b and (a == b or a in b or b in a))
        chem_state = "pass" if chem_match else "fail"
        chem_detail = f"POB “{sub_chemist}” vs invoice “{inv_chemist}”"
    else:
        chem_state, chem_detail = "na", "No customer name on the invoice"

    # ── invoice date vs campaign period (same window as the submit guard) ──
    inv_date_raw = _invoice_date(fields) or row.get("invoice_date")
    start = _iso(row.get("campaign_start"))
    end = _iso(row.get("campaign_end"))
    try:
        grace = int(row.get("campaign_grace_days")) if row.get("campaign_grace_days") is not None else 15
    except (TypeError, ValueError):
        grace = 15
    try:
        grace_months = int(row.get("campaign_grace_months") or 0)
    except (TypeError, ValueError):
        grace_months = 0
    try:
        pre_grace = int(row.get("campaign_pre_grace_days") or 0)
    except (TypeError, ValueError):
        pre_grace = 0
    parts = []
    if pre_grace:
        parts.append(f"{pre_grace}d pre-launch")
    parts.append(f"{grace_months}m" if grace_months else "")
    if not (parts and grace_months):
        parts.append(f"{grace}d")
    grace_desc = " + ".join(p for p in parts if p)
    period_label = f"{start or '—'} → {end or '—'}{f' + {grace_desc} grace' if end else ''}"
    d = _parse_date(inv_date_raw)
    if d is None:
        date_state, date_detail = "na", f"Campaign period {period_label} · no invoice date to check"
    else:
        from saas.routers.pob import _add_months
        floor = _parse_date(start) - timedelta(days=pre_grace) if start else None
        ceiling = None
        if end:
            ceiling = _add_months(_parse_date(end), grace_months)
            if ceiling:
                ceiling += timedelta(days=grace)
        before = floor and d < floor
        after = ceiling and d > ceiling
        if before or after:
            date_state = "fail"
            date_detail = f"{inv_date_raw} is outside campaign period {period_label}"
        else:
            date_state = "pass"
            date_detail = f"{inv_date_raw} is within campaign period {period_label}"

    # ── invoice number + duplicate ──────────────────────────────────────────
    inv_no = row.get("invoice_number") or _invoice_number(fields)
    dup = _find_duplicate(conn, row) if inv_no else None
    if not inv_no:
        invno_state, invno_detail = "na", "No invoice number on the POB or the document"
    else:
        invno_state, invno_detail = "pass", f"“{inv_no}” recorded"
    if dup:
        dup_state = "fail"
        dup_detail = f"Duplicate — “{inv_no}” already used on POB #{dup[0]} ({dup[1]})"
    elif inv_no:
        dup_state, dup_detail = "pass", f"“{inv_no}” is unique across this chemist"
    else:
        dup_state, dup_detail = "na", "No invoice number to check"

    # ── campaign eligibility rules ──────────────────────────────────────────
    campaign_active = (row.get("campaign_status") or "active") == "active"
    min_qty = _num(p.get("min_quantity")) if p else None
    min_pob = _num(p.get("min_pob")) if p else None
    max_pob = _num(p.get("max_pob")) if p else None
    rules = [
        {
            "label": "Campaign active",
            "requirement": row.get("campaign_name") or "—",
            "submitted": "—",
            "ok": campaign_active,
            "detail": "Campaign is active" if campaign_active else "Campaign is not active",
        },
        {
            "label": "Product belongs to campaign",
            "requirement": sub_name or "—",
            "submitted": sub_name or "—",
            "ok": sub_name is not None,
            "detail": "Submitted product belongs to the campaign",
        },
        {
            "label": "Minimum quantity",
            "requirement": str(min_qty) if min_qty is not None else "—",
            "submitted": str(sub_qty) if sub_qty is not None else "—",
            "ok": min_qty is None or (sub_qty is not None and sub_qty >= min_qty),
            "detail": f"min {min_qty or '—'}",
        },
        {
            "label": "Minimum POB",
            "requirement": _money(min_pob),
            "submitted": _money(sub_amt),
            "ok": min_pob is None or sub_amt >= min_pob,
            "detail": f"min {_money(min_pob)}",
        },
        {
            "label": "Maximum POB",
            "requirement": _money(max_pob) if max_pob is not None else "No cap",
            "submitted": _money(sub_amt),
            "ok": max_pob is None or sub_amt <= max_pob,
            "detail": f"max {_money(max_pob) if max_pob is not None else '—'}",
        },
        {
            "label": "Campaign period",
            "requirement": period_label,
            "submitted": inv_date_raw or "—",
            "ok": date_state == "pass",
            "detail": date_detail,
        },
    ]

    # ── document quality ────────────────────────────────────────────────────
    doc = fields.get("document") or {}
    quality = (doc.get("quality") or "").lower()
    if meta is None:
        doc_state, doc_detail = "na", "No AI extraction recorded for this document"
    elif extraction_failed:
        doc_state = "fail"
        doc_detail = "AI extraction failed — no invoice data could be read. Manual verification required."
    elif quality in ("poor", "blurry") or doc.get("legible") is False:
        doc_state = "fail"
        doc_detail = "Poor image quality — fields could not be reliably extracted. Manual verification required."
    else:
        doc_state, doc_detail = "pass", "Document readable, invoice structure detected"

    # ── checklist ───────────────────────────────────────────────────────────
    checklist = [
        {"key": "product", "label": "Product verification",
         "state": product_state, "detail": product_detail},
        {"key": "quantity", "label": "Quantity verification",
         "state": qty_state, "detail": qty_detail},
        {"key": "amount", "label": "POB amount verification",
         "state": amt_state, "detail": amt_detail},
        {"key": "chemist", "label": "Chemist verification",
         "state": chem_state, "detail": chem_detail},
        {"key": "date", "label": "Invoice date verification",
         "state": date_state, "detail": date_detail},
        {"key": "invoice_no", "label": "Invoice number available",
         "state": invno_state, "detail": invno_detail},
        {"key": "duplicate", "label": "Duplicate invoice check",
         "state": dup_state, "detail": dup_detail},
        {"key": "campaign", "label": "Campaign eligibility",
         "state": "pass" if all(r["ok"] for r in rules)
         else ("fail" if any(not r["ok"] for r in rules) else "na"),
         "detail": "All campaign rules satisfied" if all(r["ok"] for r in rules)
         else "One or more campaign rules not satisfied"},
        {"key": "document", "label": "Invoice document readable",
         "state": doc_state, "detail": doc_detail},
    ]
    summary = {
        "passed": sum(1 for c in checklist if c["state"] == "pass"),
        "failed": sum(1 for c in checklist if c["state"] == "fail"),
        "manual": sum(1 for c in checklist if c["state"] == "na"),
    }

    # ── automation verdict (mirrors the submit-flow decision in ocr.verify_invoice) ─
    enabled = bool(row.get("campaign_auto_verify") or row.get("auto_verify"))
    try:
        threshold = float(row.get("campaign_auto_verify_confidence")
                          if row.get("campaign_auto_verify_confidence") is not None else 0.9)
    except (TypeError, ValueError):
        threshold = 0.9
    if enabled and meta:
        # Build full context for auto-verify (all 12 checks)
        auto_ctx = {
            "chemist_name": row.get("chemist_name") or "",
            "shop_name": row.get("shop_name") or "",
            "ptr": _num(row.get("ptr")),
            "is_duplicate": bool(_find_duplicate(conn, row)),
            "campaign_start": row.get("campaign_start"),
            "campaign_end": row.get("campaign_end"),
            "campaign_grace_days": row.get("campaign_grace_days"),
            "campaign_grace_months": row.get("campaign_grace_months"),
            "campaign_pre_grace_days": row.get("campaign_pre_grace_days"),
            "min_quantity": _num(p.get("min_quantity")) if p else None,
            "min_pob": _num(p.get("min_pob")) if p else None,
            "max_pob": _num(p.get("max_pob")) if p else None,
        }
        verdict = _ocr.verify_invoice(
            {
                "invoice_number": row.get("invoice_number"),
                "invoice_amount": row.get("invoice_amount") or row.get("invoice_amt"),
                "invoice_date": row.get("invoice_date"),
                "product_name": row.get("product_name") or "",
                "quantity": row.get("quantity"),
                "pob_amount": row.get("pob_amount"),
            },
            {"fields": fields, "confidence": meta.get("confidence") or 0},
            min_confidence=threshold,
            context=auto_ctx,
        )
        sub_no = row.get("invoice_number")
        ext_no = _invoice_number(fields)
        sub_amt = _num(row.get("invoice_amount")) or _num(row.get("invoice_amt"))
        ext_amt = _invoice_amount(fields)
        sub_date = row.get("invoice_date")
        ext_date = _invoice_date(fields)
        sub_product = row.get("product_name") or ""
        sub_qty = row.get("quantity")
        sub_pob = _num(row.get("pob_amount"))

        # Matched invoice line for product/qty/amount comparison
        matched = _matched_item(row, items)
        ext_product = matched.get("description") if matched else None
        ext_qty = matched.get("qty") if matched else None
        ext_line_amt = _ocr._normalise_amount(matched.get("amount")) if matched else None
        ext_ptr = (_ocr._normalise_amount(matched.get("ptr"))
                   or _ocr._normalise_amount(matched.get("rate"))) if matched else None

        # Chemist from invoice
        ext_chemist = ""
        buyer = (fields.get("buyer") or {}) if isinstance(fields.get("buyer"), dict) else {}
        ext_chemist = (buyer.get("name") or "").strip()

        def _field_state(key, sub=None, ext=None):
            if key in verdict["matches"]:
                return "pass"
            if key in verdict["mismatches"]:
                return "fail"
            return "na"

        field_rows = [
            {"label": "Invoice number", "submitted": sub_no or "—",
             "invoice": ext_no or "—",
             "state": _field_state("invoice_number", sub_no, ext_no)},
            {"label": "Invoice amount", "submitted": _money(sub_amt) if sub_amt is not None else "—",
             "invoice": _money(ext_amt) if ext_amt is not None else "—",
             "state": _field_state("invoice_amount", sub_amt, ext_amt)},
            {"label": "Invoice date", "submitted": sub_date or "—",
             "invoice": ext_date or "—",
             "state": _field_state("invoice_date", sub_date, ext_date)},
            {"label": "Product", "submitted": sub_product or "—",
             "invoice": ext_product or "—",
             "state": _field_state("product_name", sub_product, ext_product)},
            {"label": "Quantity", "submitted": str(sub_qty) if sub_qty is not None else "—",
             "invoice": str(int(ext_qty)) if ext_qty is not None else "—",
             "state": _field_state("quantity", sub_qty, ext_qty)},
            {"label": "POB amount", "submitted": _money(sub_pob) if sub_pob is not None else "—",
             "invoice": _money(ext_line_amt) if ext_line_amt is not None else "—",
             "state": _field_state("pob_amount", sub_pob, ext_line_amt)},
            {"label": "Chemist cross-check", "submitted": sub_chemist or "—",
             "invoice": ext_chemist or "—",
             "state": _field_state("chemist_cross_check", sub_chemist, ext_chemist)},
            {"label": "PTR", "submitted": _money(sub_ptr) if sub_ptr is not None else "—",
             "invoice": _money(ext_ptr) if ext_ptr is not None else "—",
             "state": _field_state("ptr", sub_ptr, ext_ptr)},
            {"label": "Duplicate", "submitted": "—",
             "invoice": "—",
             "state": _field_state("duplicate")},
            {"label": "Campaign period", "submitted": sub_date or "—",
             "invoice": "—",
             "state": _field_state("campaign_period")},
            {"label": "Min quantity", "submitted": str(sub_qty) if sub_qty is not None else "—",
             "invoice": str(min_qty) if min_qty is not None else "—",
             "state": _field_state("min_quantity")},
            {"label": "Min POB", "submitted": _money(sub_pob) if sub_pob is not None else "—",
             "invoice": _money(min_pob) if min_pob is not None else "—",
             "state": _field_state("min_pob")},
            {"label": "Max POB", "submitted": _money(sub_pob) if sub_pob is not None else "—",
             "invoice": _money(max_pob) if max_pob is not None else "—",
             "state": _field_state("max_pob")},
        ]
        automation = {
            "enabled": True,
            "threshold": threshold,
            "verdict": verdict["ok"],
            "auto_verified": bool(row.get("auto_verified")),
            "confidence": verdict["confidence"],
            "matches": verdict["matches"],
            "mismatches": verdict["mismatches"],
            "message": verdict["message"],
            "fields": field_rows,
        }
    else:
        automation = {
            "enabled": enabled,
            "threshold": threshold,
            "verdict": None,
            "auto_verified": False,
            "confidence": meta.get("confidence") if meta else None,
            "matches": [],
            "mismatches": [],
            "message": ("Manual review — no AI extraction recorded"
                        if enabled and not meta else "Automation not enabled for this campaign"),
            "fields": [],
        }

    return {
        "status": top,
        "automation": automation,
        "extraction": {
            "engine": meta.get("engine") if meta else None,
            "confidence": meta.get("confidence") if meta else None,
            "error": meta.get("error") if meta else None,
            "fields": fields,
        },
        "product": {
            "submitted": sub_name, "invoice": inv_desc, "matched": bool(item),
            "state": product_state, "detail": product_detail,
        },
        "quantity": {
            "submitted": sub_qty, "invoice": inv_qty, "diff": qty_diff,
            "state": qty_state, "detail": qty_detail,
        },
        "ptr": {
            "submitted": sub_ptr, "invoice": inv_rate, "state": ptr_state, "detail": ptr_detail,
        },
        "amount": {
            "submitted": sub_amt, "invoice": inv_amt, "diff": amt_diff,
            "state": amt_state, "detail": amt_detail, "note": amt_note,
        },
        "chemist": {
            "submitted": sub_chemist, "invoice": inv_chemist,
            "state": chem_state, "detail": chem_detail,
        },
        "date": {
            "invoice_date": inv_date_raw, "campaign_start": start, "campaign_end": end,
            "grace_days": grace, "state": date_state, "detail": date_detail,
        },
        "invoice_number": {
            "value": inv_no, "present": bool(inv_no),
            "duplicate": bool(dup), "duplicate_pob": dup[0] if dup else None,
            "state": invno_state, "detail": invno_detail,
            "duplicate_detail": dup_detail,
        },
        "campaign": {"active": campaign_active, "rules": rules},
        "document": {"state": doc_state, "detail": doc_detail, "quality": quality},
        "checklist": checklist,
        "summary": summary,
    }
