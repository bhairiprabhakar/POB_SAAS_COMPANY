"""UPI QR capture router (End User module).

Lets a field employee scan / paste a UPI QR payload against a chemist in their
own division, verify the decoded payee against the chemist identity, and save a
confirmed UPI scan record. The raw payload is stored for audit; the VPA on the
chemist master and gratifications is what payout uses. All payment-sensitive
fields are masked for callers without payment/management rights.
"""
from fastapi import APIRouter, Depends, HTTPException

from ..audit import log_action
from ..db_utils import fetchall_dict, fetchone_dict
from ..deps import TenantContext, require_permission
from ..pagination import PageLimit, PageOffset
from ..scoping import user_division_id
from ..upi import (extract_upi_id, is_valid_vpa, mask_upi_id, name_similarity,
                   parse_upi_payload)

router = APIRouter(prefix="/api/v1", tags=["upi"])


def _resolve_chemist(conn, ctx: TenantContext, cid: int):
    """Fetch a chemist and enforce the caller's division isolation."""
    c = conn.cursor()
    c.execute("SELECT * FROM chemists WHERE id=%s", (cid,))
    chem = fetchone_dict(c)
    if not chem:
        raise HTTPException(404, "chemist not found")
    div = user_division_id(conn, ctx)
    if div and chem.get("division_id") not in (None, div):
        raise HTTPException(403, "not allowed to modify this chemist")
    return chem


@router.get("/chemists/{cid}/upi")
def chemist_upi_records(cid: int, limit: int = PageLimit(default=20), offset: int = PageOffset(),
                        ctx: TenantContext = Depends(require_permission("chemist.view"))):
    """Recent UPI scan records for a chemist, with payment fields masked for
    callers who do not manage or pay gratifications."""
    conn = ctx.conn
    _resolve_chemist(conn, ctx, cid)
    can_see = bool(ctx.perms & {"gratification.pay", "gratification.manage"})
    c = conn.cursor()
    c.execute("SELECT * FROM upi_scans WHERE chemist_id=%s ORDER BY id DESC LIMIT %s OFFSET %s",
              (cid, limit, offset))
    items = fetchall_dict(c)
    for r in items:
        if can_see:
            r["masked_upi_id"] = mask_upi_id(r.get("upi_id"))
        else:
            r["cant_see_raw"] = True
            r.pop("raw_payload", None)
            r["masked_upi_id"] = mask_upi_id(r.get("upi_id"))
            r["upi_id"] = r["masked_upi_id"]
    return {"items": items}


@router.post("/chemists/{cid}/upi/decode")
def decode_upi_payload(cid: int, body: dict,
                       ctx: TenantContext = Depends(require_permission("chemist.manage"))):
    """Parse a UPI QR payload and validate the VPA (no persistence)."""
    conn = ctx.conn
    chem = _resolve_chemist(conn, ctx, cid)
    payload = (body.get("payload") or "").strip()
    if not payload:
        raise HTTPException(400, "payload required")
    details = parse_upi_payload(payload)
    raw_vpa = details["upi_id"]
    details["valid"] = True
    if not raw_vpa:
        details["valid"] = False
        details["error"] = "No VPA (pa field) found in the QR payload"
    elif not is_valid_vpa(raw_vpa):
        details["valid"] = False
        details["error"] = "Scanned VPA does not look like a valid UPI address"
    details["masked_upi_id"] = mask_upi_id(raw_vpa)
    if details.get("payee_name"):
        name_checks = [chem.get("name"), chem.get("shop_name")]
        details["name_score"] = max(name_similarity(details["payee_name"], n)
                                    for n in name_checks if n)
        details["name_matches"] = details["name_score"] >= 0.6
    return {"ok": True, "details": details}


@router.post("/chemists/{cid}/upi")
def save_upi_details(cid: int, body: dict,
                     ctx: TenantContext = Depends(require_permission("chemist.manage"))):
    """Save (and confirm) a chemist's UPI address.

    body: upi_id (VPA), source ('qr'|'manual', default 'qr'), raw_payload,
          payee_name, merchant_name, confirmed (default True), name_score.
    Records the scan (including QR type / raw payload) for audit, updates
    chemists.upi_id plus the confirmation provenance columns, and marks the
    chemist UPI as confirmed for the gratification pipeline. Duplicate
    submissions for the same VPA update the confirmation, not re-insert.
    """
    conn = ctx.conn
    chem = _resolve_chemist(conn, ctx, cid)
    raw_vpa = extract_upi_id(body.get("upi_id") or "")
    raw_payload = (body.get("raw_payload") or "").strip()
    source = "manual" if (body.get("source") or "").startswith("manual") else "qr"
    if not is_valid_vpa(raw_vpa):
        raise HTTPException(400, "Invalid UPI address (expected something like name@bank)")
    confirmed = bool(body.get("confirmed", True))
    parsed = parse_upi_payload(raw_payload)
    qr_type = (body.get("qr_type") or parsed.get("qr_type") or None)
    merchant_name = (body.get("merchant_name") or parsed.get("payee_name") or None)
    c = conn.cursor()
    name_score = body.get("name_score")
    if name_score is None and body.get("payee_name"):
        name_checks = [chem.get("name"), chem.get("shop_name")]
        name_score = max((name_similarity(body["payee_name"], n) for n in name_checks if n), default=None)
    c.execute("SELECT id FROM upi_scans WHERE chemist_id=%s AND upi_id=%s",
              (cid, raw_vpa))
    existing = c.fetchone()
    if existing:
        c.execute(
            "UPDATE upi_scans SET source=%s, payee_name=%s, merchant_name=%s, qr_type=%s, "
            "raw_payload=%s, name_score=%s, confirmed=%s, confirmed_by=%s, confirmed_at=CURRENT_TIMESTAMP "
            "WHERE id=%s",
            (source, body.get("payee_name"), merchant_name, qr_type,
             (raw_payload or "")[:2000], name_score,
             confirmed, ctx.user["id"] if confirmed else None, existing[0]),
        )
        scan_id = existing[0]
        log_action(conn, ctx.user["id"], "chemist.upi_confirmed", "chemist", cid,
                   {"upi_id": mask_upi_id(raw_vpa), "source": source})
    else:
        c.execute(
            """INSERT INTO upi_scans (chemist_id, upi_id, payee_name, merchant_name,
               qr_type, raw_payload, source, name_score, confirmed, confirmed_by, confirmed_at, created_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP,%s) RETURNING id""",
            (cid, raw_vpa, body.get("payee_name"), merchant_name, qr_type,
             (raw_payload or "")[:2000], source, name_score,
             confirmed, ctx.user["id"] if confirmed else None, ctx.user["id"]),
        )
        scan_id = c.fetchone()[0]
        log_action(conn, ctx.user["id"], "chemist.upi_scanned", "chemist", cid,
                   {"upi_id": mask_upi_id(raw_vpa), "source": source})
    c.execute(
        """UPDATE chemists
           SET upi_id=%s, upi_payee_name=%s, upi_scan_source=%s,
               upi_confirmed=%s, upi_confirmed_by=CASE WHEN %s THEN %s ELSE upi_confirmed_by END,
               upi_confirmed_at=CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE upi_confirmed_at END
           WHERE id=%s""",
        (raw_vpa, body.get("payee_name") or merchant_name, source, confirmed,
         confirmed, ctx.user["id"], confirmed, cid))
    conn.commit()
    return {"ok": True, "id": scan_id, "masked_upi_id": mask_upi_id(raw_vpa)}