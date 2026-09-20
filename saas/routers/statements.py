"""
Statements router -- tenant-scoped statement-document portal (Batch 2).

Ports the legacy single-company flows from app/routers/main_routes.py
(upload / upload-from-url / extraction pipeline / documents / period checks /
credits) and app/routers/agent_routes.py (verification queue, claim, inline
edit, reject, re-extract, Excel download + corrections) onto the SaaS tenant
boundary. The tenant database IS the company, so the legacy company_id flags
collapse to the connection itself (see saas/statements.py).

Every route runs on the caller's tenant connection (``ctx.conn``) like the
rest of the tenant API, and the heavy Gemini extraction pipeline runs on the
dedicated OCR worker pool (saas/worker_pool.py) so a burst of uploads can
never stall out the request threadpool.
"""
import hashlib
import io
import logging
import os
import tempfile
import time
import traceback

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from .. import credits, extraction as x, ingestion, statements, storage
from ..config import MAX_UPLOAD_SIZE
from ..db_utils import fetchall_dict, fetchone_dict
from ..deps import TenantContext, require_permission
from ..pools import get_tenant_conn
from ..upload_validation import UploadValidationError, validate_upload
from ..worker_pool import OCR_EXECUTOR

log = logging.getLogger("saas.routers.statements")

router = APIRouter(prefix="/api/v1/statements", tags=["statements"])

# Extension -> (mime, allowed content kinds). The extension is only a UX hint:
# validate_upload() verifies the actual bytes against the allowed kinds.
_EXT_MIME = {
    ".pdf": ("application/pdf", {"pdf"}),
    ".jpg": ("image/jpeg", {"jpeg"}),
    ".jpeg": ("image/jpeg", {"jpeg"}),
    ".png": ("image/png", {"png"}),
    ".xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", {"office_zip"}),
    ".xls": ("application/vnd.ms-excel", {"office_legacy"}),
    ".csv": ("text/csv", {"csv"}),
}

_PERIOD_LABELS = {
    "monthly": "Monthly statement",
    "quarterly": "Quarterly statement",
    "half-yearly": "Half-yearly statement",
    "yearly": "Full-year statement",
}


class _TTLDict:
    """Upload-progress cache with TTL sweep (ported from legacy main_routes).
    The DB remains the source of truth for terminal state; this only adds the
    transient pct/stage values while a worker is running."""

    TTL_SECONDS = 30 * 60
    SWEEP_EVERY = 200

    def __init__(self):
        self._data = {}
        self._writes = 0

    def __setitem__(self, key, value):
        self._data[key] = (value, time.time())
        self._writes += 1
        if self._writes % self.SWEEP_EVERY == 0:
            self._sweep()

    def get(self, key, default=None):
        entry = self._data.get(key)
        return default if entry is None else entry[0]

    def _sweep(self):
        cutoff = time.time() - self.TTL_SECONDS
        stale = [k for k, (_, ts) in self._data.items() if ts < cutoff]
        for k in stale:
            del self._data[k]


_upload_progress = _TTLDict()


def _tenant_db(ctx: TenantContext) -> str:
    return ctx.claims.get("tenant_db") or ""


def _progress_key(ctx_or_db, up_id: int):
    return (ctx_or_db, up_id)


def _platform_division_id(tenant_db: str) -> int | None:
    """Platform control-plane division id for a tenant db (used to attribute
    AI usage rows to the right tenant). Best-effort."""
    try:
        from ..platform_db import get_db
        conn = get_db()
        try:
            c = conn.cursor()
            c.execute("SELECT id FROM divisions WHERE tenant_db_name=%s", (tenant_db,))
            row = c.fetchone()
            return row[0] if row else None
        finally:
            conn.close()
    except Exception:
        return None


# ── Helpers shared by upload + pipeline ─────────────────────────────────────

def _check_credits_or_raise(conn):
    creds = credits.get_credits(conn)
    if (creds.get("remaining") or 0) <= 0:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "No credits remaining. Please request more credits from your admin.",
                "credits_exhausted": True,
                "credits": creds,
            },
        )
    return creds


def _compute_file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _store_upload(conn, tenant_db, data, mime, original_filename, file_hash,
                  user_id, division_id):
    stored = storage.save(data, tenant_db, "statements", original_filename)
    c = conn.cursor()
    c.execute(
        """INSERT INTO uploads
           (division_id, user_id, original_filename, stored_filename, file_type,
            status, file_hash)
           VALUES (%s,%s,%s,%s,%s,'processing',%s) RETURNING id""",
        (division_id, user_id, original_filename, stored, mime, file_hash),
    )
    up_id = c.fetchone()[0]
    c.execute("UPDATE uploads SET progress_pct=5, progress_stage='Queued for extraction...' WHERE id=%s",
              (up_id,))
    conn.commit()
    return up_id


def _queue_pipeline(tenant_db, up_id, user_id, force_proceed, manual_from, manual_to):
    _upload_progress[_progress_key(tenant_db, up_id)] = {
        "pct": 5, "stage": "Queued for extraction...", "done": False,
    }
    OCR_EXECUTOR.submit(
        _run_extraction_pipeline, tenant_db, up_id, user_id,
        bool(force_proceed), manual_from, manual_to,
    )


def _set_progress(tenant_db, up_id, payload):
    key = (tenant_db, up_id)
    _upload_progress[key] = payload
    try:
        conn = get_tenant_conn(tenant_db)
        try:
            c = conn.cursor()
            c.execute(
                "UPDATE uploads SET progress_pct=%s, progress_stage=%s WHERE id=%s",
                (payload.get("pct", 0), payload.get("stage", "") or "", up_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


# ── Upload + extraction pipeline ────────────────────────────────────────────

@router.post("/upload")
def upload_statement(
    file: UploadFile = File(...),
    force_proceed: str = Form("0"),
    manual_from: str = Form(""),
    manual_to: str = Form(""),
    ctx: TenantContext = Depends(require_permission("statement.upload")),
):
    """Upload a statement document for AI extraction (PDF / JPG / PNG / XLSX /
    XLS / CSV). Runs the cheap L1 duplicate check inline, then hands the heavy
    extraction to the OCR worker pool. Poll GET /uploads/{id}/progress."""
    tenant_db = _tenant_db(ctx)
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in _EXT_MIME:
        raise HTTPException(400, f"Unsupported format: {ext}")

    data = file.file.read()
    mime, allowed_kinds = _EXT_MIME[ext]
    try:
        validate_upload(data, filename=file.filename or "",
                        allowed_kinds=allowed_kinds, max_size=MAX_UPLOAD_SIZE)
    except UploadValidationError as exc:
        raise HTTPException(400, f"Invalid file: {exc}")

    _check_credits_or_raise(ctx.conn)

    file_hash = _compute_file_hash(data)
    if force_proceed != "1":
        dup = statements.check_file_hash_duplicate(ctx.conn, file_hash)
        if dup:
            raise HTTPException(409, {
                "error": (
                    f"Duplicate within your company! This exact file was already uploaded on "
                    f"{str(dup['upload_date'])[:10]} by {dup['uploader']} "
                    f"(original: {dup['original_filename']}). Each document should be uploaded once."
                ),
                "duplicate_level": 1,
                "existing_upload_id": dup["id"],
            })

    up_id = _store_upload(
        ctx.conn, tenant_db, data, mime, file.filename or "",
        file_hash, ctx.user.get("id"), ctx.user.get("division_id"),
    )
    _queue_pipeline(tenant_db, up_id, ctx.user.get("id"),
                    force_proceed == "1", manual_from.strip(), manual_to.strip())
    return {"success": True, "upload_id": up_id, "processing": True}


@router.post("/upload-from-url")
def upload_statement_from_url(
    source_url: str = Form(...),
    force_proceed: str = Form("0"),
    manual_from: str = Form(""),
    manual_to: str = Form(""),
    ctx: TenantContext = Depends(require_permission("statement.upload")),
):
    """Import a document from a remote link (S3/Drive/direct web link) and run
    the same extraction pipeline as /upload."""
    source_url = (source_url or "").strip()
    if not source_url:
        raise HTTPException(400, "Please provide a document URL")

    _check_credits_or_raise(ctx.conn)

    try:
        tmp_path, ext, original_name = ingestion.download_remote_document(source_url)
    except ingestion.RemoteFetchError as exc:
        raise HTTPException(400, str(exc))

    try:
        with open(tmp_path, "rb") as f:
            data = f.read()
        mime, allowed_kinds = _EXT_MIME.get(ext, ("application/octet-stream", set()))
        if not allowed_kinds:
            raise HTTPException(400, f"Unsupported format: {ext}")
        try:
            validate_upload(data, filename=original_name,
                            allowed_kinds=allowed_kinds, max_size=MAX_UPLOAD_SIZE)
        except UploadValidationError as exc:
            raise HTTPException(400, f"Invalid file: {exc}")

        file_hash = _compute_file_hash(data)
        if force_proceed != "1":
            dup = statements.check_file_hash_duplicate(ctx.conn, file_hash)
            if dup:
                raise HTTPException(409, {
                    "error": (
                        f"Duplicate within your company! This exact file was already uploaded on "
                        f"{str(dup['upload_date'])[:10]} by {dup['uploader']} "
                        f"(original: {dup['original_filename']}). Each document should be uploaded once."
                    ),
                    "duplicate_level": 1,
                    "existing_upload_id": dup["id"],
                })

        up_id = _store_upload(
            ctx.conn, _tenant_db(ctx), data, mime, original_name, file_hash,
            ctx.user.get("id"), ctx.user.get("division_id"),
        )
        _queue_pipeline(_tenant_db(ctx), up_id, ctx.user.get("id"),
                        force_proceed == "1", manual_from.strip(), manual_to.strip())
        return {"success": True, "upload_id": up_id, "processing": True}
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


@router.get("/uploads/{up_id}/progress")
def upload_progress(up_id: int, ctx: TenantContext = Depends(require_permission("statement.upload"))):
    """Poll extraction progress. Reads the uploads row (status/error_msg/
    progress_*) as the source of truth and overlays the in-memory cache, so a
    worker restart or multi-process deployment still reports the real state."""
    tenant_db = _tenant_db(ctx)
    c = ctx.conn.cursor()
    c.execute(
        "SELECT status, error_msg, progress_pct, progress_stage, verification_status "
        "FROM uploads WHERE id=%s AND user_id=%s",
        (up_id, ctx.user.get("id")),
    )
    row = fetchone_dict(c)
    if not row:
        raise HTTPException(404, "Upload not found")

    live = _upload_progress.get((tenant_db, up_id), {})
    if row["status"] in ("done", "rejected", "error"):
        return {
            "pct": 100,
            "done": True,
            "status": row["status"],
            "error_msg": row["error_msg"],
            "verification_status": row["verification_status"],
            **(live or {}),
        }
    return {
        "pct": row["progress_pct"] if row["progress_pct"] is not None else live.get("pct", 0),
        "stage": row["progress_stage"] or live.get("stage", "Waiting"),
        "done": False,
        "status": row["status"],
    }


def _run_extraction_pipeline(tenant_db, up_id, user_id, force_proceed, manual_from, manual_to):
    """Background worker -- NOT on the request thread. Mirrors legacy
    main_routes._run_extraction_pipeline, driving the same duplicate checks,
    OCR, period warnings, credit consumption and auto-assigned verification
    task, with every outcome written back through the progress cache + DB."""
    tmp_path = None
    conn = None
    try:
        conn = get_tenant_conn(tenant_db)
        c = conn.cursor()
        c.execute(
            "SELECT * FROM uploads WHERE id=%s AND user_id=%s",
            (up_id, user_id),
        )
        up = fetchone_dict(c)
        if not up:
            return

        _set_progress(tenant_db, up_id, {"pct": 15, "stage": "Extracting text from document...", "done": False})

        stored = up.get("stored_filename") or ""
        mime = up.get("file_type") or "application/octet-stream"
        original_filename = up.get("original_filename") or stored

        raw = storage.read(stored)
        if raw is None:
            _set_progress(tenant_db, up_id, {
                "pct": 100, "done": True,
                "error": "Stored document file is missing from storage.",
            })
            c.execute("UPDATE uploads SET status='error', error_msg=%s WHERE id=%s",
                      ("Stored document file is missing from storage.", up_id))
            conn.commit()
            return

        ext = os.path.splitext(original_filename)[1].lower() or ".bin"
        fd, tmp_path = tempfile.mkstemp(suffix=ext)
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        if mime and mime.startswith("image/"):
            try:
                x.normalize_image_orientation(tmp_path)
            except Exception:
                log.exception("image orientation fix failed (continuing)")
                try:
                    conn.rollback()
                except Exception:
                    pass

        p_div = _platform_division_id(tenant_db)
        try:
            data = x.call_ocr_extraction(
                tmp_path, mime, original_filename,
                company_id=p_div, upload_id=up_id, division_id=p_div, user_id=user_id,
            )
        except Exception as ocr_err:
            traceback.print_exc()
            c.execute("UPDATE uploads SET status='error', error_msg=%s WHERE id=%s",
                      (f"OCR extraction failed: {ocr_err}", up_id))
            conn.commit()
            _set_progress(tenant_db, up_id, {
                "pct": 100, "done": True,
                "error": f"OCR extraction failed: {ocr_err}", "ocr_error": True,
            })
            return

        n_parties = len(data.get("parties", []))
        _set_progress(tenant_db, up_id, {"pct": 65, "stage": f"Saving {n_parties} parties...", "done": False})

        if manual_from and manual_to:
            try:
                from datetime import datetime as _dtt
                data["statement_from_date"] = _dtt.strptime(manual_from, "%Y-%m-%d").strftime("%d/%m/%Y")
                data["statement_to_date"] = _dtt.strptime(manual_to, "%Y-%m-%d").strftime("%d/%m/%Y")
            except Exception:
                pass

        # ── Level 2: content fingerprint (renamed file, same stockist+period) ──
        content_fp = x._compute_content_fingerprint(data)
        stockist_for_fp = (data.get("stockist_name", "") or "").strip()
        if not force_proceed and stockist_for_fp:
            fp_dup = statements.check_content_fingerprint_duplicate(conn, content_fp)
            if fp_dup and (fp_dup.get("stockist_name") or "").strip():
                c.execute("UPDATE uploads SET status='rejected', error_msg=%s WHERE id=%s",
                          (f"Content fingerprint matched upload #{fp_dup['id']}", up_id))
                conn.commit()
                _set_progress(tenant_db, up_id, {
                    "pct": 100, "done": True,
                    "error": (
                        f"Smart duplicate detected! Same stockist ({fp_dup['stockist_name']}) "
                        f"with identical statement period ({fp_dup['statement_from_date']} – "
                        f"{fp_dup['statement_to_date']}) was already uploaded on "
                        f"{str(fp_dup['upload_date'])[:10]} by {fp_dup['uploader']}. "
                        f"Renaming the file does not bypass this check."
                    ),
                    "duplicate_level": 2,
                    "existing_upload_id": fp_dup["id"],
                })
                return

        c.execute("UPDATE uploads SET content_fingerprint=%s WHERE id=%s", (content_fp, up_id))
        conn.commit()

        save_stats = statements.save_extraction(conn, up_id, data)

        if manual_from and manual_to and data.get("statement_from_date"):
            try:
                c.execute(
                    "UPDATE extractions SET statement_from_date=%s, statement_to_date=%s WHERE upload_id=%s",
                    (data["statement_from_date"], data["statement_to_date"], up_id),
                )
                conn.commit()
            except Exception:
                conn.rollback()

        _set_progress(tenant_db, up_id, {"pct": 85, "stage": "Running period checks...", "done": False})

        warnings = []
        s_from = data.get("statement_from_date", "")
        s_to = data.get("statement_to_date", "")
        stockist = data.get("stockist_name", "")
        date_source = "manual" if (manual_from and manual_to) else "auto"

        if s_from or s_to:
            warnings.append({
                "type": "period_info", "level": "info",
                "message": f"📅 Statement period {'manually set' if date_source == 'manual' else 'auto-detected'}: "
                           f"{s_from or '?'} → {s_to or '?'}",
            })

        if s_from and s_to:
            period_type, days = statements.classify_period(s_from, s_to)
            plabel = _PERIOD_LABELS.get(period_type, f"Statement ({days} days)")
            warnings.append({"type": "period_type", "level": "info",
                             "message": f"✓ {plabel} detected -- {s_from} to {s_to}"})
            if not force_proceed:
                dupes = statements.check_period_duplicate(
                    conn, stockist, s_from, s_to, exclude_upload_id=up_id)
                if dupes:
                    d0 = dupes[0]
                    ov_type = d0.get("overlap_type", "exact")
                    ex_ptype = d0.get("existing_period_type", "")
                    ex_plabel = _PERIOD_LABELS.get(ex_ptype, "statement")
                    ex_range = f"{d0['statement_from_date']}–{d0['statement_to_date']}"
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
                        "message": ov_msg, "upload_id": d0["id"],
                        "overlap_type": ov_type, "overlap_count": len(dupes),
                    })

        dbg = data.get("_debug", {})
        extracted_total = float(data.get("invoice_net", 0) or 0)
        doc_grand_total = float(dbg.get("doc_grand_total", 0) or 0)
        match_pct = 100.0
        match_diff = 0.0
        if doc_grand_total > 0:
            match_diff = abs(extracted_total - doc_grand_total)
            match_pct = round(max(0, (1 - match_diff / doc_grand_total) * 100), 1)

        ok, remaining = credits.consume_credit(
            conn, "extraction",
            division_id=up.get("division_id"), user_id=up.get("user_id"),
            reference_id=up_id,
            detail=f"Extracted: {stockist[:40]}, {n_parties} parties",
        )

        # ── Auto-create verification task + auto-assign to least-loaded agent ──
        try:
            statements.ensure_manual_verification(conn, up_id, up.get("division_id"))
        except Exception as av_err:
            traceback.print_exc()
            try:
                conn.rollback()
            except Exception:
                pass

        _set_progress(tenant_db, up_id, {
            "pct": 100, "done": True, "success": True,
            "upload_id": up_id, "data": data,
            "warnings": warnings, "date_source": date_source,
            "credits_remaining": remaining, "extraction_debug": dbg,
            "match_pct": match_pct, "match_diff": match_diff,
            "extracted_total": extracted_total, "doc_grand_total": doc_grand_total,
            "stage": f"✓ Complete -- {n_parties} parties, {save_stats.get('items_saved', 0)} items",
        })
    except Exception as e:
        traceback.print_exc()
        try:
            if conn is not None:
                c = conn.cursor()
                c.execute("UPDATE uploads SET status='error', error_msg=%s WHERE id=%s", (str(e), up_id))
                conn.commit()
        except Exception:
            pass
        _set_progress(tenant_db, up_id, {"pct": 100, "done": True, "error": str(e)})
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ── Upload admin (delete / restore) ─────────────────────────────────────────

@router.post("/uploads/{up_id}/delete")
def upload_delete(up_id: int, ctx: TenantContext = Depends(require_permission("statement.manage"))):
    c = ctx.conn.cursor()
    c.execute("SELECT id FROM uploads WHERE id=%s", (up_id,))
    if not c.fetchone():
        raise HTTPException(404, "Upload not found")
    c.execute("UPDATE uploads SET status='deleted', verification_status='deleted' WHERE id=%s", (up_id,))
    ctx.conn.commit()
    return {"success": True}


@router.post("/uploads/{up_id}/restore")
def upload_restore(up_id: int, ctx: TenantContext = Depends(require_permission("statement.manage"))):
    c = ctx.conn.cursor()
    c.execute("SELECT id FROM uploads WHERE id=%s AND status='deleted'", (up_id,))
    if not c.fetchone():
        raise HTTPException(404, "Deleted upload not found")
    c.execute("UPDATE uploads SET status='done', verification_status='ocr_done' WHERE id=%s", (up_id,))
    ctx.conn.commit()
    return {"success": True}


# ── Documents + extraction detail ───────────────────────────────────────────

def _visible_ids(ctx, conn):
    from ..scoping import visible_user_ids
    visible = visible_user_ids(conn, ctx)
    if visible is None:
        return None
    return visible or [ctx.user.get("id")]


@router.get("/documents")
def documents_list(
    date_from: str = "",
    date_to: str = "",
    status_filter: str = "all",
    ctx: TenantContext = Depends(require_permission("statement.view")),
):
    from ..scoping import visible_user_ids
    visible = _visible_ids(ctx, ctx.conn)
    uploads, status_counts = statements.documents(
        ctx.conn, visible, date_from, date_to, status_filter)
    return {"uploads": uploads, "status_counts": status_counts}


@router.get("/extractions/{up_id}")
def extraction_detail(up_id: int, ctx: TenantContext = Depends(require_permission("statement.view"))):
    payload = statements.extraction_payload(ctx.conn, up_id)
    if not payload:
        raise HTTPException(404, "Extraction not found")
    visible = _visible_ids(ctx, ctx.conn)
    if visible is not None and payload["upload"].get("user_id") not in visible:
        raise HTTPException(403, "Not authorized for this document")
    return payload


@router.get("/period-check")
def period_check(ctx: TenantContext = Depends(require_permission("statement.view"))):
    q = statements.get_current_quarter()
    c = ctx.conn.cursor()
    c.execute(
        """SELECT COUNT(*) AS n FROM uploads u
           JOIN extractions e ON e.upload_id=u.id
           WHERE u.user_id=%s AND u.status='done'
           AND e.statement_from_date >= %s AND e.statement_to_date <= %s""",
        (ctx.user.get("id"), q["from"], q["to"]),
    )
    qt_uploads = c.fetchone()[0]
    return {
        "current_quarter": q,
        "uploads_this_quarter": qt_uploads,
        "expected_period_type": "quarterly",
        "reminder": (f"Upload statements for {q['label']}: {q['from_display']} to {q['to_display']}"
                     if qt_uploads == 0 else None),
    }


# ── Credits (tenant wallet) ─────────────────────────────────────────────────

@router.get("/credits")
def credits_wallet(ctx: TenantContext = Depends(require_permission("statement.credits"))):
    creds = credits.get_credits(ctx.conn)
    recent = credits.credit_ledger(ctx.conn, limit=20)
    return {"credits": creds, "recent_transactions": recent}


@router.post("/credits/request")
def credits_request(
    credits_requested: int = Form(...),
    message: str = Form(""),
    ctx: TenantContext = Depends(require_permission("statement.credits")),
):
    if credits_requested <= 0:
        raise HTTPException(400, "Enter a valid credit amount")
    rid = credits.request_credits(ctx.conn, ctx.user.get("id"), credits_requested,
                                  (message or "").strip() or None)
    return {"success": True, "request_id": rid,
            "message": f"Credit request for {credits_requested} credits submitted."}


# ── Verification (tenant verification agents) ───────────────────────────────

@router.get("/verification/queue")
def verification_queue(ctx: TenantContext = Depends(require_permission("statement.verify"))):
    return {"tasks": statements.verification_queue(ctx.conn, ctx.user.get("id")),
            "stats": statements.agent_stats(ctx.conn, ctx.user.get("id"))}


@router.get("/verification/unassigned")
def verification_unassigned(ctx: TenantContext = Depends(require_permission("statement.verify"))):
    return {"tasks": statements.unassigned_tasks(ctx.conn),
            "unassigned_count": statements.unassigned_count(ctx.conn)}


@router.get("/verification/unassigned-count")
def verification_unassigned_count(ctx: TenantContext = Depends(require_permission("statement.verify"))):
    return {"unassigned_count": statements.unassigned_count(ctx.conn)}


@router.get("/verification/stats")
def verification_stats(ctx: TenantContext = Depends(require_permission("statement.verify"))):
    return {"stats": statements.agent_stats(ctx.conn, ctx.user.get("id")),
            "queue_count": len(statements.verification_queue(ctx.conn, ctx.user.get("id")))}


@router.get("/verification/history")
def verification_history(ctx: TenantContext = Depends(require_permission("statement.verify"))):
    return {"history": statements.verification_history(ctx.conn, ctx.user.get("id")),
            "stats": statements.agent_stats(ctx.conn, ctx.user.get("id"))}


@router.get("/verification/history/export")
def verification_history_export(ctx: TenantContext = Depends(require_permission("statement.verify"))):
    buf, fname = statements.verification_history_workbook(
        ctx.conn, ctx.user.get("id"), ctx.user.get("full_name") or "")
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/verification/{mv_id}")
def verification_detail(mv_id: int, ctx: TenantContext = Depends(require_permission("statement.verify"))):
    payload = statements.verification_detail(ctx.conn, mv_id)
    if not payload:
        raise HTTPException(404, "Verification task not found")
    return payload


@router.get("/verification/{mv_id}/party-items")
def verification_party_items(mv_id: int, ctx: TenantContext = Depends(require_permission("statement.verify"))):
    payload = statements.verification_detail(ctx.conn, mv_id)
    if not payload:
        raise HTTPException(404, "Verification task not found")
    return {"parties": payload["parties"]}


@router.get("/verification/{mv_id}/download-excel")
def verification_download_excel(mv_id: int, ctx: TenantContext = Depends(require_permission("statement.verify"))):
    code = (_tenant_db(ctx).replace("POB_SAAS_COMPANY_", "") or "tenant").upper()[:8]
    try:
        buf, fname = statements.build_verification_workbook(ctx.conn, mv_id, company_code=code)
    except LookupError:
        raise HTTPException(404, "Verification task not found")
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.post("/verification/{mv_id}/claim")
def verification_claim(mv_id: int, ctx: TenantContext = Depends(require_permission("statement.verify"))):
    ok, err, status = statements.claim_task(ctx.conn, mv_id, ctx.user.get("id"))
    if not ok:
        raise HTTPException(status, err)
    return {"success": True, "message": "Task claimed successfully"}


@router.post("/verification/bulk-claim")
def verification_bulk_claim(payload: dict, ctx: TenantContext = Depends(require_permission("statement.verify"))):
    mv_ids = (payload or {}).get("mv_ids") or []
    if not mv_ids:
        raise HTTPException(400, "No task IDs provided")
    result = statements.bulk_claim(ctx.conn, mv_ids, ctx.user.get("id"))
    return {"success": True, **result}


@router.post("/verification/{mv_id}/inline-save")
def verification_inline_save(mv_id: int, payload: dict,
                             ctx: TenantContext = Depends(require_permission("statement.verify"))):
    ok, err = statements.save_inline_corrections(ctx.conn, mv_id, ctx.user.get("id"), payload or {})
    if not ok:
        raise HTTPException(500, err or "Failed to save corrections")
    return {"success": True, "message": "Saved & marked verified"}


@router.post("/verification/{mv_id}/upload-corrections")
def verification_upload_corrections(mv_id: int, file: UploadFile = File(...),
                                    ctx: TenantContext = Depends(require_permission("statement.verify"))):
    from openpyxl import load_workbook
    fname = (file.filename or "").lower()
    if not fname.endswith((".xlsx", ".xls")):
        raise HTTPException(400, "Only .xlsx files accepted")
    try:
        wb = load_workbook(io.BytesIO(file.file.read()), data_only=True)
    except Exception as exc:
        raise HTTPException(400, f"Could not read workbook: {exc}")
    try:
        party_updates, item_updates = statements.apply_excel_corrections(
            ctx.conn, mv_id, ctx.user.get("id"), wb)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {
        "success": True,
        "message": f"Corrections applied -- {party_updates} parties, {item_updates} items updated. Document marked verified.",
    }


@router.post("/verification/{mv_id}/reject")
def verification_reject(mv_id: int, payload: dict,
                        ctx: TenantContext = Depends(require_permission("statement.verify"))):
    reason = (payload or {}).get("reason", "").strip()
    reason_label = (payload or {}).get("reason_label") or reason
    if not reason:
        raise HTTPException(400, "Rejection reason required")
    ok, err, status = statements.reject_verification(
        ctx.conn, mv_id, ctx.user.get("id"), reason, reason_label)
    if not ok:
        raise HTTPException(status, err)
    return {"success": True, "message": f"Rejected: {reason_label}"}


@router.post("/verification/{mv_id}/reextract")
def verification_reextract(mv_id: int, ctx: TenantContext = Depends(require_permission("statement.verify"))):
    """Discard the current extraction and re-run Gemini on the original
    document, then reset the task to pending. Runs inline (single document,
    agent-invoked) -- the same path the legacy route used."""
    c = ctx.conn.cursor()
    c.execute(
        "SELECT mv.id, mv.upload_id, u.stored_filename, u.file_type, u.original_filename "
        "FROM manual_verifications mv JOIN uploads u ON mv.upload_id=u.id WHERE mv.id=%s",
        (mv_id,),
    )
    mv_row = fetchone_dict(c)
    if not mv_row:
        raise HTTPException(404, "Verification task not found")

    raw = storage.read(mv_row.get("stored_filename") or "")
    if raw is None:
        raise HTTPException(404, "Original document file is missing from storage -- can't re-extract.")

    ext = os.path.splitext(mv_row.get("original_filename") or mv_row.get("stored_filename") or "")[1].lower()
    fd, tmp_path = tempfile.mkstemp(suffix=ext or ".bin")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        p_div = _platform_division_id(_tenant_db(ctx))
        try:
            new_data = x.call_ocr_extraction(
                tmp_path, mv_row.get("file_type") or "",
                mv_row.get("original_filename") or mv_row.get("stored_filename") or "",
                company_id=p_div, upload_id=mv_row["upload_id"],
                division_id=p_div, user_id=ctx.user.get("id"),
            )
        except Exception as exc:
            raise HTTPException(500, f"AI re-extraction failed: {exc}")

        upload_id = mv_row["upload_id"]
        statements.reextract_discard(ctx.conn, upload_id)
        statements.save_extraction(ctx.conn, upload_id, new_data)
        statements.reset_verification_pending(ctx.conn, mv_id, upload_id)

        return {
            "success": True,
            "message": "Re-extracted with AI -- please review the new data.",
            "data": new_data,
        }
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass