"""
Bridges Gemini's raw JSON output to the app's internal extraction schema, and
provides duplicate-detection hashing. This is the same mapping logic the
original app.py used for its backend/ pipeline output -- Gemini is prompted
(see app/ai/gemini_extraction.py) to emit the same Areas -> Stores -> Items
shape, so this mapper is unchanged.
"""
import hashlib as _hl

from .ai.gemini_extraction import call_gemini_extraction


def normalize_image_orientation(path: str) -> None:
    """
    Auto-rotate an uploaded image in place according to its EXIF Orientation
    tag, if present. Phone cameras very commonly save a photo with pixel data
    in one orientation plus an EXIF tag saying "display this rotated" -- most
    viewers respect that tag, but a raw <img> tag (and Gemini's raw pixel
    read) does not. Re-saving with the tag "baked in" fixes the sideways
    appearance for both the verification-agent viewer and the OCR call,
    without touching non-EXIF images (genuinely-rotated photos with no EXIF
    tag are unaffected -- use the viewer's rotate buttons for those).
    Safe no-op for PDFs and any image without an orientation tag.
    """
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if ext not in ("jpg", "jpeg", "png"):
        return
    try:
        from PIL import Image, ImageOps
        with Image.open(path) as im:
            exif = im.getexif()
            if not exif or exif.get(0x0112, 1) == 1:  # 0x0112 = Orientation tag; 1 = normal
                return
            fixed = ImageOps.exif_transpose(im)
            fixed.save(path)
    except Exception:
        # Never let a rotation-correction failure break the upload itself.
        pass


def _ocr_data_to_extraction_format(data: dict, original_name: str) -> dict:
    """
    Convert the Gemini-parsed data structure to the app's internal format.
    Maps Areas -> Parties, Store Items -> Items.
    """
    ag = data.get("AgencyDetails", {})
    rd = data.get("ReportDetails", {})

    from_date = rd.get("FromDate", "")
    to_date   = rd.get("ToDate", from_date)

    parties        = []
    total_amount   = 0.0
    total_quantity = 0

    for area in data.get("Areas", []):
        area_name = area.get("AreaName", "")
        for store in area.get("Stores", []):
            store_name = store.get("StoreName", "").strip()
            store_loc  = store.get("StoreLocation", "")
            if not store_name or store_name == "UNKNOWN STORE":
                continue
            items_raw = store.get("Items", [])
            if not items_raw:
                continue

            party_items = []
            party_qty   = 0
            party_amt   = 0.0

            for item in items_raw:
                desc = str(item.get("Description", "") or "").strip()
                qty  = int(item.get("Qty", 0) or 0)
                amt  = float(item.get("Amount", 0) or 0)
                rate = float(item.get("Rate", 0) or 0)
                if not desc and qty == 0 and amt == 0:
                    continue
                party_items.append({
                    "brand":            desc,
                    "mfg":              rd.get("Company", "") or ag.get("Name", "") or item.get("Manufacturer", ""),
                    "pack":             item.get("Pack", ""),
                    "batch_no":         item.get("BatchNo", ""),
                    "expiry":           item.get("Expiry", ""),
                    "hsn_code":         item.get("HSN", ""),
                    "quantity":         qty,
                    "mrp":              float(item.get("MRP", 0) or 0),
                    "unit_rate":        rate,
                    "tax_type":         "",
                    "discount_percent": float(item.get("Percent", 0) or 0),
                    "final_amount":     amt,
                })
                party_qty += qty
                party_amt += amt

            total_amount   += party_amt
            total_quantity += party_qty

            parties.append({
                "name":                 store_name,
                "type":                 "chemist",
                "area":                 store_loc or area_name,
                "dl_number":            store.get("DLNumber", ""),
                "gst_number":           store.get("GSTNumber", ""),
                "party_total_quantity": party_qty,
                "party_total_amount":   party_amt,
                "items":                party_items,
            })

    doc_type = str(data.get("DocType", "STATEMENT") or "STATEMENT").upper()
    if doc_type not in ("INVOICE", "STATEMENT"):
        doc_type = "STATEMENT"

    return {
        "stockist_name":       ag.get("Name", "") or "",
        "stockist_gst":        ag.get("GSTIN", "") or "",
        "stockist_address":    ag.get("Address", "") or "",
        "bill_number":         rd.get("InvoiceNumber", "") or "",
        "bill_date":           rd.get("InvoiceDate", "") or "",
        "statement_from_date": from_date,
        "statement_to_date":   to_date,
        "doc_type":            doc_type,
        "total_amount":        total_amount,
        "total_quantity":      total_quantity,
        "discount_percent":    0.0,
        "discount_amount":     0.0,
        "net_sale":            total_amount,
        "sgst":                0.0,
        "cgst":                0.0,
        "invoice_net":         float(data.get("NetTotal", total_amount) or total_amount),
        "parties":             parties,
        "_debug": {
            "mode":            data.get("_model_used", ""),
            "source_file":     original_name,
            "areas":           len(data.get("Areas", [])),
            "parties":         len(parties),
            "confidence":      data.get("ConfidenceScore", None),
            "validation_notes": data.get("ValidationNotes", ""),
            "doc_grand_total": float(data.get("InvoiceTotal", 0) or 0),
        },
    }


def _compute_file_hash(path: str) -> str:
    """SHA-256 of file bytes -- for duplicate detection level 1."""
    h = _hl.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def _compute_content_fingerprint(data: dict) -> str:
    """
    Level-2 fingerprint: agency name + from_date + to_date (normalised).
    Smart people rename files but can't change the content inside.
    """
    sn = (data.get("stockist_name", "") or "").strip().upper()
    fd = (data.get("statement_from_date", "") or "").strip()
    td = (data.get("statement_to_date", "") or "").strip()
    key = f"{sn}||{fd}||{td}"
    return _hl.sha256(key.encode()).hexdigest()


def call_ocr_extraction(file_path: str, file_type: str, original_name: str,
                         company_id=None, upload_id=None) -> dict:
    """
    Extract data from a document using Gemini Flash 3.5, then convert to the
    app's internal format. Supports PDF, JPEG, PNG (per upload validation in
    the /upload route). company_id/upload_id are optional and only used to
    attribute AI usage cost to the right company (see app/ai/pricing.py and
    the superadmin AI Costing dashboard) -- omitting them just means that
    call's cost isn't logged, extraction itself is unaffected.
    """
    parsed = call_gemini_extraction(file_path, file_type, original_name,
                                     company_id=company_id, upload_id=upload_id)
    return _ocr_data_to_extraction_format(parsed, original_name)
