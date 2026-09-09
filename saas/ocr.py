"""
OCR / AI invoice verification (Phase 4).

Two paths:

  - Gemini (OCR_PROVIDER=gemini, GOOGLE_API_KEY set): send the invoice image
    to the Gemini vision model and get a structured JSON of invoice fields.
  - Text-mode fallback: plain-text invoice stubs (".txt", ".csv", ".log")
    with `Key: Value` lines parse deterministically. This is what the E2E
    verification scripts use so auto-verification is testable offline.

Binary images without a Gemini key return an empty low-confidence extraction,
so the POB routes to manual review (the system degrades gracefully).

verify_invoice() compares the extracted fields against the submitted POB with
tolerances and returns a verdict the submit flow can act on.
"""
import json
import os
import re
from datetime import datetime

from . import config


def _normalise_amount(value) -> float:
    try:
        return float(str(value).replace(",", "").replace("Rs", "").replace("₹", "").strip())
    except (TypeError, ValueError):
        return 0.0


def _normalise_date(value) -> str:
    if not value:
        return ""
    v = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%b %d, %Y", "%d %b %Y"):
        try:
            return datetime.strptime(v, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return v


def _normalise_invoice_no(value) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()


def extract_fields(data: bytes, filename: str = "") -> dict:
    """Return {fields, confidence, engine, raw} for an uploaded invoice."""
    ext = (filename or "").lower().rsplit(".", 1)[-1] if "." in (filename or "") else ""
    if ext in ("txt", "csv", "log"):
        return _extract_text(data.decode("utf-8", errors="ignore"))
    if config.GOOGLE_API_KEY:
        try:
            return _extract_gemini(data, filename)
        except Exception as exc:  # provider failure -> degrade to manual review
            return {"fields": {}, "confidence": 0.0, "engine": "gemini",
                    "raw": {}, "error": str(exc)}
    return {"fields": {}, "confidence": 0.0, "engine": "none",
            "raw": {}, "error": "no OCR provider configured for binary invoice"}


def _extract_text(text: str) -> dict:
    """Deterministic parser for plain-text invoice stubs (offline tests / demo).

    Understands `Key: Value` lines for the core invoice fields plus the richer
    header/tax fields a real extraction returns, so the verification report has
    something to compare even without a vision model."""
    fields = {}
    alias = {
        "chemist": "chemist_name",
        "chemist_name": "chemist_name",
        "party": "buyer_name",
        "customer": "buyer_name",
        "buyer": "buyer_name",
        "seller": "seller_name",
        "distributor": "seller_name",
        "vendor": "seller_name",
        "gstin": "gstin",
        "seller_gstin": "seller_gstin",
        "buyer_gstin": "buyer_gstin",
        "invoice_type": "invoice_type",
        "currency": "currency",
        "cgst": "cgst",
        "sgst": "sgst",
        "igst": "igst",
        "taxable_amount": "taxable_amount",
        "batch": "batch",
        "expiry": "expiry",
        "free_qty": "free_qty",
        "mrp": "mrp",
        "discount": "discount",
    }
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower().replace(" ", "_")
        value = value.strip()
        if not key or not value:
            continue
        if "invoice_no" in key or "invoice_number" in key:
            fields["invoice_number"] = value
        elif key == "amount" or "invoice_amount" in key or "total_amount" in key:
            fields["invoice_amount"] = value
        elif key == "date" or "invoice_date" in key:
            fields["invoice_date"] = value
        elif key in alias:
            fields.setdefault(alias[key], value)
        else:
            fields.setdefault(key, value)
    has_core = bool(fields.get("invoice_number") and fields.get("invoice_amount"))
    confidence = 0.98 if has_core else 0.3
    fields["document"] = {
        "quality": "readable" if has_core else "poor",
        "structure_detected": has_core,
        "legible": has_core,
        "note": "" if has_core else "Text stub missing invoice number/amount",
    }
    return {"fields": fields, "confidence": confidence, "engine": "text",
            "raw": {"text": text[:4000]}}


def _extract_gemini(data: bytes, filename: str) -> dict:
    import mimetypes
    import os
    import tempfile

    from google import genai
    from google.genai import types

    mime = mimetypes.guess_type(filename or "")[0] or "application/pdf"
    if mime.startswith("image/"):
        mime = "image/jpeg"
    client = genai.Client(api_key=config.GOOGLE_API_KEY)
    model = config.GEMINI_MODEL

    prompt = (
        "You are reading a pharmacy purchase invoice. Return ONLY a compact JSON object with "
        "this structure (use null when a value cannot be read; no commentary):\n"
        "{\n"
        "  \"invoice\": { \"number\": \"\", \"date\": \"YYYY-MM-DD\", \"type\": \"\", "
        "\"total\": 0, \"currency\": \"INR\" },\n"
        "  \"seller\": { \"name\": \"\", \"gstin\": \"\", \"address\": \"\", \"phone\": \"\", "
        "\"license_no\": \"\" },\n"
        "  \"buyer\": { \"name\": \"\", \"code\": \"\", \"gstin\": \"\", \"address\": \"\", "
        "\"phone\": \"\" },\n"
        "  \"tax\": { \"taxable_amount\": 0, \"cgst\": 0, \"sgst\": 0, \"igst\": 0 },\n"
        "  \"items\": [ { \"description\": \"\", \"sku\": \"\", \"batch\": \"\", "
        "\"expiry\": \"MM/YYYY\", \"qty\": 0, \"free_qty\": 0, \"mrp\": 0, \"ptr\": 0, "
        "\"discount\": 0, \"tax\": 0, \"amount\": 0 } ],\n"
        "  \"document\": { \"quality\": \"readable|poor|blurry\", \"resolution\": [width, height], "
        "\"structure_detected\": true, \"legible\": true, \"note\": \"\" }\n"
        "}\n"
        "Every item line needs description, qty and amount. Free units go under free_qty. "
        "'ptr' is the printed trade/rate price; if not printed use mrp. If the bill has no "
        "itemized list, return an empty items array. The buyer is the pharmacy/customer the "
        "invoice is billed to."
    )

    fd, path = tempfile.mkstemp(suffix=os.path.splitext(filename or "")[1] or ".bin")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    uploaded = None
    try:
        try:
            uploaded = client.files.upload(file=path, config={"mime_type": mime})
        except Exception:
            uploaded = None
        contents = [prompt]
        if uploaded is not None:
            contents.append(uploaded)
        else:
            contents.append(types.Part.from_bytes(data=data, mime_type=mime))
        resp = client.models.generate_content(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0,
            ),
        )
        raw = resp.text or ""
        usage = None
        um = getattr(resp, "usage_metadata", None)
        if um is not None:
            try:
                usage = {
                    "prompt_token_count": int(getattr(um, "prompt_token_count", 0) or 0),
                    "candidates_token_count": int(getattr(um, "candidates_token_count", 0) or 0),
                    "total_token_count": int(getattr(um, "total_token_count", 0) or 0),
                }
            except Exception:
                usage = None
    finally:
        try:
            if uploaded is not None:
                client.files.delete(name=uploaded.name)
        except Exception:
            pass
        try:
            os.remove(path)
        except OSError:
            pass
    fields = {}
    try:
        parsed = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        inv = parsed.get("invoice") or {}
        if inv.get("number") is not None:
            fields["invoice_number"] = inv["number"]
        if inv.get("date") is not None:
            fields["invoice_date"] = inv["date"]
        if inv.get("total") is not None:
            fields["invoice_amount"] = inv["total"]
        if inv.get("type") is not None:
            fields["invoice_type"] = inv["type"]
        if inv.get("currency") is not None:
            fields["currency"] = inv["currency"]
        if isinstance(parsed.get("seller"), dict):
            seller = {k: v for k, v in parsed["seller"].items() if v is not None}
            if seller:
                fields["seller"] = seller
                if seller.get("name"):
                    fields.setdefault("seller_name", seller["name"])
                if seller.get("gstin"):
                    fields.setdefault("seller_gstin", seller["gstin"])
        if isinstance(parsed.get("buyer"), dict):
            buyer = {k: v for k, v in parsed["buyer"].items() if v is not None}
            if buyer:
                fields["buyer"] = buyer
                if buyer.get("name"):
                    fields.setdefault("chemist_name", buyer["name"])
                    fields.setdefault("buyer_name", buyer["name"])
                if buyer.get("gstin"):
                    fields.setdefault("buyer_gstin", buyer["gstin"])
        if isinstance(parsed.get("tax"), dict):
            tax = {k: v for k, v in parsed["tax"].items() if v is not None}
            if tax:
                fields["tax"] = tax
        if isinstance(parsed.get("document"), dict):
            doc = parsed["document"]
            if any(doc.values()):
                fields["document"] = doc
        # Legacy flat shape fallback (older model outputs).
        for k in ("invoice_number", "invoice_amount", "invoice_date", "chemist_name"):
            if k not in fields and parsed.get(k) is not None:
                fields[k] = parsed[k]
        if isinstance(parsed.get("items"), list):
            clean_items = []
            for it in parsed["items"]:
                if isinstance(it, dict) and str(it.get("description") or "").strip():
                    row = {
                        "description": str(it.get("description") or "").strip(),
                        "qty": _normalise_amount(it.get("qty") or 0),
                        "rate": _normalise_amount(it.get("rate") or 0),
                        "amount": _normalise_amount(it.get("amount") or 0),
                    }
                    for k in ("sku", "batch", "expiry"):
                        if it.get(k):
                            row[k] = str(it[k])
                    for k in ("free_qty", "mrp", "ptr", "pts", "discount", "tax"):
                        row[k] = _normalise_amount(it.get(k) or 0)
                    clean_items.append(row)
            if clean_items:
                fields["items"] = clean_items
    except Exception:
        pass
    confidence = 0.92 if fields.get("invoice_number") and fields.get("invoice_amount") else 0.4
    return {"fields": fields, "confidence": confidence, "engine": "gemini",
            "model": model, "raw": {"text": raw}, "usage": usage}


def extraction_cost(extraction) -> float:
    """Monetary cost of one extraction call, token-based for Gemini and 0 for
    text-mode parsing / no provider. Returns the amount in the configured
    OCR_COST_CURRENCY (see saas/config.py)."""
    if not isinstance(extraction, dict):
        return 0.0
    if extraction.get("engine") != "gemini":
        return 0.0
    usage = extraction.get("usage") or {}
    try:
        prompt = float(usage.get("prompt_token_count") or 0)
        cand = float(usage.get("candidates_token_count") or 0)
        total = float(usage.get("total_token_count") or 0)
    except (TypeError, ValueError):
        return 0.0
    if prompt <= 0 and cand <= 0 and total > 0:
        prompt = total  # only a combined count was reported
    cost = (prompt / 1_000_000.0 * config.OCR_COST_INPUT_PER_MTOK
            + cand / 1_000_000.0 * config.OCR_COST_OUTPUT_PER_MTOK)
    return round(cost, 6)


def match_brand(items, product_name: str = "", brand_name: str = "", sku: str = ""):
    """Find the extracted invoice line that matches a submitted product/brand.

    Matches case-insensitively on significant (alphanumeric) characters, and
    accepts either the product name or the brand name as the key, or an exact
    SKU code. Returns the matching extracted item dict or None."""
    if not items:
        return None

    def key(s):
        return re.sub(r"[^a-z0-9]", "", str(s or "").lower())

    targets = [key(product_name), key(brand_name)]
    targets = [t for t in targets if t]
    if not targets:
        return None
    sku_key = key(sku)
    for it in items or []:
        if sku_key and key(it.get("sku")) and sku_key == key(it.get("sku")):
            return it
        desc = key(it.get("description"))
        if not desc:
            continue
        for t in targets:
            if t == desc or t in desc or desc in t:
                return it
    return None


def verify_invoice(submitted: dict, extracted: dict, min_confidence: float = 0.9,
                    context: dict = None) -> dict:
    """Compare OCR fields against the submitted POB.

    Checks ALL parameters needed for auto-approve:
      1. Invoice number
      2. Invoice amount
      3. Invoice date
      4. Product name
      5. Quantity
      6. POB amount
      7. Chemist cross-check (buyer on invoice vs submitted chemist)
      8. PTR match (submitted PTR vs invoice line rate)
      9. Duplicate check (invoice number already used)
     10. Campaign period (invoice date within campaign window)
     11. Min/max quantity (submitted qty vs product constraints)
     12. Min/max POB value (submitted amount vs product constraints)

    ``context`` is an optional dict with supplementary data:
      - chemist_name / shop_name: for chemist cross-check
      - ptr: submitted PTR for rate comparison
      - is_duplicate: bool if invoice# already used
      - campaign_start / campaign_end: for period check
      - campaign_grace_days / campaign_grace_months / campaign_pre_grace_days
      - min_quantity / min_pob / max_pob: product constraints

    Returns {ok, confidence, matches, mismatches, message}.
    Auto-approve ONLY when ALL checks pass.  Any mismatch → manual review.
    """
    fields = extracted.get("fields") or {}
    confidence = float(extracted.get("confidence") or 0)
    ctx = context or {}
    matches, mismatches = [], []

    # ── 1. Invoice number ────────────────────────────────────────────────
    sub_no = _normalise_invoice_no(submitted.get("invoice_number"))
    ext_no = _normalise_invoice_no(fields.get("invoice_number"))
    if sub_no and ext_no:
        if sub_no == ext_no or sub_no in ext_no or ext_no in sub_no:
            matches.append("invoice_number")
        else:
            mismatches.append("invoice_number")
    elif sub_no or ext_no:
        mismatches.append("invoice_number")

    # ── 2. Invoice amount ────────────────────────────────────────────────
    sub_amt = _normalise_amount(submitted.get("invoice_amount"))
    ext_amt = _normalise_amount(fields.get("invoice_amount"))
    if sub_amt and ext_amt:
        diff = abs(sub_amt - ext_amt)
        if diff <= max(5.0, sub_amt * 0.02):
            matches.append("invoice_amount")
        else:
            mismatches.append("invoice_amount")
    elif sub_amt or ext_amt:
        mismatches.append("invoice_amount")

    # ── 3. Invoice date ──────────────────────────────────────────────────
    sub_date = _normalise_date(submitted.get("invoice_date"))
    ext_date = _normalise_date(fields.get("invoice_date"))
    if sub_date and ext_date:
        if sub_date == ext_date:
            matches.append("invoice_date")
        else:
            mismatches.append("invoice_date")

    # ── 4-6. Product line items (product, quantity, POB amount) ──────────
    sub_product = (submitted.get("product_name") or "").strip().lower()
    sub_qty = submitted.get("quantity")
    sub_pob = _normalise_amount(submitted.get("pob_amount"))
    items = fields.get("items") or []

    # Find the matching invoice line for the submitted product
    matched_item = None
    for item in items:
        desc = (item.get("description") or "").strip().lower()
        if sub_product and desc and (sub_product in desc or desc in sub_product):
            matched_item = item
            break

    # ── 4. Product name ──────────────────────────────────────────────────
    if sub_product:
        if matched_item:
            matches.append("product_name")
        else:
            mismatches.append("product_name")

    # ── 5. Quantity ──────────────────────────────────────────────────────
    if sub_qty is not None and matched_item:
        ext_qty = matched_item.get("qty")
        if ext_qty is not None and int(ext_qty) == int(sub_qty):
            matches.append("quantity")
        elif ext_qty is not None:
            mismatches.append("quantity")
    elif sub_qty is not None and not matched_item:
        mismatches.append("quantity")

    # ── 6. POB amount ────────────────────────────────────────────────────
    if sub_pob is not None and matched_item:
        ext_line_amt = _normalise_amount(matched_item.get("amount"))
        if ext_line_amt is not None:
            diff = abs(ext_line_amt - sub_pob)
            if diff <= max(5.0, sub_pob * 0.02):
                matches.append("pob_amount")
            else:
                mismatches.append("pob_amount")
    elif sub_pob is not None and not matched_item:
        mismatches.append("pob_amount")

    # ── 7. Chemist cross-check (buyer on invoice vs submitted chemist) ───
    inv_buyer = ""
    buyer = fields.get("buyer") or {}
    if isinstance(buyer, dict):
        inv_buyer = (buyer.get("name") or "").strip()
    sub_chemist = (ctx.get("chemist_name") or ctx.get("shop_name") or "").strip()
    if inv_buyer and sub_chemist:
        # Fuzzy match: normalise, compare, allow substring
        def _norm(s):
            return "".join(c for c in s.lower() if c.isalnum())
        a, b = _norm(sub_chemist), _norm(inv_buyer)
        if a and b and (a == b or a in b or b in a):
            matches.append("chemist_cross_check")
        else:
            mismatches.append("chemist_cross_check")
    elif inv_buyer or sub_chemist:
        # One present, other not — can't verify
        mismatches.append("chemist_cross_check")

    # ── 8. PTR match (submitted PTR vs invoice line rate) ────────────────
    sub_ptr = ctx.get("ptr")
    if sub_ptr is not None and matched_item:
        ext_rate = _normalise_amount(matched_item.get("ptr")) or _normalise_amount(matched_item.get("rate"))
        if ext_rate is not None:
            if abs(float(sub_ptr) - float(ext_rate)) <= 0.01:
                matches.append("ptr")
            else:
                mismatches.append("ptr")

    # ── 9. Duplicate check ───────────────────────────────────────────────
    if ctx.get("is_duplicate"):
        mismatches.append("duplicate")
    else:
        matches.append("duplicate")

    # ── 10. Campaign period ──────────────────────────────────────────────
    inv_date_str = ext_date or sub_date
    campaign_start = ctx.get("campaign_start")
    campaign_end = ctx.get("campaign_end")
    grace_days = int(ctx.get("campaign_grace_days") or 15)
    grace_months = int(ctx.get("campaign_grace_months") or 0)
    pre_grace_days = int(ctx.get("campaign_pre_grace_days") or 0)

    if inv_date_str and (campaign_start or campaign_end):
        from datetime import datetime, timedelta
        try:
            d = datetime.strptime(inv_date_str, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            d = None
        if d:
            floor = None
            if campaign_start:
                try:
                    floor = datetime.strptime(campaign_start, "%Y-%m-%d").date() - timedelta(days=pre_grace_days)
                except (ValueError, TypeError):
                    pass
            ceiling = None
            if campaign_end:
                try:
                    end_dt = datetime.strptime(campaign_end, "%Y-%m-%d").date()
                    # Add grace_months
                    m = end_dt.month + grace_months
                    y = end_dt.year + (m - 1) // 12
                    m = (m - 1) % 12 + 1
                    ceiling = end_dt.replace(month=m, year=y) + timedelta(days=grace_days)
                except (ValueError, TypeError):
                    pass
            before = floor and d < floor
            after = ceiling and d > ceiling
            if before or after:
                mismatches.append("campaign_period")
            else:
                matches.append("campaign_period")

    # ── 11. Min quantity (product constraint) ────────────────────────────
    min_qty = ctx.get("min_quantity")
    if min_qty is not None and sub_qty is not None:
        if int(sub_qty) >= int(min_qty):
            matches.append("min_quantity")
        else:
            mismatches.append("min_quantity")

    # ── 12. Min/max POB (product constraint) ─────────────────────────────
    min_pob = ctx.get("min_pob")
    max_pob = ctx.get("max_pob")
    if min_pob is not None and sub_pob is not None:
        if float(sub_pob) >= float(min_pob):
            matches.append("min_pob")
        else:
            mismatches.append("min_pob")
    if max_pob is not None and sub_pob is not None:
        if float(sub_pob) <= float(max_pob):
            matches.append("max_pob")
        else:
            mismatches.append("max_pob")

    # ── Verdict ──────────────────────────────────────────────────────────
    # Auto-approve ONLY when:
    #   - confidence meets threshold
    #   - zero mismatches
    #   - at least 2 core fields matched (invoice# + amount at minimum)
    ok = confidence >= min_confidence and not mismatches and len(matches) >= 2
    return {
        "ok": ok,
        "confidence": confidence,
        "matches": matches,
        "mismatches": mismatches,
        "message": "Auto-verified by OCR" if ok else "Flagged for manual review",
    }
