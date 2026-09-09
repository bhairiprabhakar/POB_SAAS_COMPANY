"""
OCR usage ledger.

Every invoice extraction is recorded in the tenant's `ocr_usage` table so the
company admin can see who uploaded what (statement without the AI cost) and
the platform super admin can aggregate a company-wise / user-wise cost
statement. Cost is token-based for Gemini (see saas/ocr.py:extraction_cost);
text-mode parsing and unconfigured providers are free.
"""
from . import config
from .ocr import extraction_cost


def record_ocr_usage(conn, user_id, extraction, filename="", invoice_number="", status=None):
    """Append one row to `ocr_usage` for an extraction attempt.

    The row is committed immediately so the cost is retained even when the
    enclosing request later rolls back (e.g. a duplicate-invoice reject after
    the document was already processed by the AI provider). Returns the cost
    recorded, or None when nothing was recorded (no extraction / text engine /
    engine "none").
    """
    if not isinstance(extraction, dict):
        return None
    engine = extraction.get("engine") or "none"
    if engine == "none":
        return None
    if status is None:
        status = "error" if extraction.get("error") else "success"
    cost = extraction_cost(extraction)
    fields = extraction.get("fields") or {}
    inv = (invoice_number or "").strip() or str(fields.get("invoice_number") or "").strip()
    c = conn.cursor()
    c.execute(
        """INSERT INTO ocr_usage (user_id, engine, cost, currency, status, invoice_number, filename)
           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
        (user_id, engine, cost, config.OCR_COST_CURRENCY, status, inv or None, filename or None),
    )
    conn.commit()
    return cost
