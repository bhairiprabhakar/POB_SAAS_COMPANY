"""
Invoice Verification Portal.

Dedicated verifier queue with statuses (pending / approved / rejected /
duplicate / needs_review), mandatory reason on reject, duplicate detection
(already flagged at submission), full POB+invoice context, and TAT reporting.
"""
from fastapi import APIRouter, Depends, HTTPException, Request

from ..audit import log_action
from ..db_utils import fetchall_dict, fetchone_dict
from ..deps import TenantContext, require_permission
from ..notify import notify_from_template
from ..scoping import visible_user_ids
from ..pagination import PageLimit, PageOffset

router = APIRouter(prefix="/api/v1/verification", tags=["verification"])

# Fields that internal AI/OCR/pipeline details should be hidden from end users
_VERIFIER_FIELDS = {
    "confidence", "auto_verified", "ai_extraction", "normalized_data",
    "mapped_product_id", "product_mapping_confidence", "duplicate_check_result",
    "completeness_score", "correction_count", "pipeline_status",
    "campaign_auto_verify", "campaign_auto_verify_confidence",
    "campaign_pob_required", "campaign_start", "campaign_end",
    "campaign_period_type", "campaign_grace_days", "campaign_grace_months",
    "campaign_pre_grace_days",
}


def _is_verifier_or_admin(ctx):
    role = (ctx.user.get("role_name") or "").lower()
    return role in ("verification_agent", "verifier", "company_admin", "division_admin")


def _assert_scope(conn, ctx, vid):
    """Bar cross-division access on single-verification actions.

    Division-scoped roles (e.g. verification_agent) may only act on a
    verification whose POB belongs to a user in their view. Unrestricted roles
    (verifier / campaignos_admin / unassigned division admin) pass through.
    """
    visible = visible_user_ids(conn, ctx)
    if visible is None:
        return
    c = conn.cursor()
    c.execute("SELECT pa.user_id FROM pob_verifications v "
              "JOIN pob_activities pa ON pa.id=v.pob_id WHERE v.id=%s", (vid,))
    row = c.fetchone()
    if not row:
        raise HTTPException(404, "verification not found")
    if row[0] not in visible:
        raise HTTPException(403, "not allowed to act on this verification")


def _filter_for_role(row, ctx):
    """Strip internal fields from a row when the user is not a verifier/admin."""
    if _is_verifier_or_admin(ctx):
        return row
    return {k: v for k, v in row.items() if k not in _VERIFIER_FIELDS}


def _full_query(extra_where="", params=()):
    sql = f"""
      SELECT v.id AS verification_id, v.status AS v_status, v.reason, v.duplicate_of,
             v.started_at, v.verified_at, v.created_at AS v_created_at,
             v.pipeline_status,
             pa.id AS pob_id, pa.user_id, pa.product_id, pa.quantity, pa.ptr, pa.mrp,
             pa.invoice_amount, pa.pob_amount, pa.invoice_number, pa.invoice_date,
             pa.invoice_path, pa.invoice_original_name, pa.remarks, pa.workflow_id,
             pa.workflow_step, pa.workflow_done, pa.auto_verified, pa.confidence,
             pa.campaign_id, pa.chemist_id, pa.submission_group,
             u.full_name AS mr_name, u.username AS mr_username,
             cmp.name AS campaign_name, cmp.status AS campaign_status,
             cmp.scheme_type, cmp.approval_workflow_id,
             cmp.invoice_verification_required,
             cmp.auto_verify AS campaign_auto_verify,
              cmp.auto_verify_confidence AS campaign_auto_verify_confidence,
              cmp.pob_required AS campaign_pob_required,
              cmp.start_date AS campaign_start, cmp.end_date AS campaign_end,
             cmp.period_type AS campaign_period_type, cmp.grace_days AS campaign_grace_days,
             cmp.grace_months AS campaign_grace_months, cmp.pre_grace_days AS campaign_pre_grace_days,
             pr.name AS product_name, pr.sku,
             ch.name AS chemist_name, ch.shop_name, ch.city, ch.state, ch.mobile,
             vu.full_name AS verifier_name
      FROM pob_verifications v
      JOIN pob_activities pa ON pa.id = v.pob_id
      LEFT JOIN users u ON u.id = pa.user_id
      LEFT JOIN campaigns cmp ON cmp.id = pa.campaign_id
      LEFT JOIN products pr ON pr.id = pa.product_id
      LEFT JOIN chemists ch ON ch.id = pa.chemist_id
      LEFT JOIN users vu ON vu.id = v.verifier_id
      {extra_where}
      ORDER BY v.id DESC
    """
    return sql, params


@router.get("/stats")
def verification_stats(ctx: TenantContext = Depends(require_permission("verification.view"))):
    conn = ctx.conn
    visible = visible_user_ids(conn, ctx)
    sc, sp = "", ()
    if visible is not None:
        sc = " WHERE pa.user_id = ANY(%s)"
        sp = (visible,)
    c = conn.cursor()
    c.execute("SELECT v.status, count(*) FROM pob_verifications v "
              f"JOIN pob_activities pa ON pa.id=v.pob_id{sc} GROUP BY v.status", sp)
    stats = {r[0]: r[1] for r in c.fetchall()}
    for s in ("pending", "approved", "rejected", "duplicate", "needs_review", "auto_approved", "superseded"):
        stats.setdefault(s, 0)
    # Manual-review queue = pipelined to an agent but not yet decided.
    and_scope = f" AND {sc[7:]}" if sc else ""
    c.execute("SELECT count(*) FROM pob_verifications v JOIN pob_activities pa ON pa.id=v.pob_id "
              f"WHERE v.pipeline_status='pending_agent'{and_scope}", sp)
    stats["manual_review"] = c.fetchone()[0]
    c.execute("SELECT coalesce(avg(extract(epoch from (verified_at - created_at)))/3600.0, 0) "
              "FROM pob_verifications WHERE verified_at IS NOT NULL")
    stats["avg_tat_hours"] = round(c.fetchone()[0], 2)
    return stats


@router.get("/queue")
def verification_queue(status: str = "pending", q: str = "", limit: int = PageLimit(default=50), offset: int = PageOffset(),
                       ctx: TenantContext = Depends(require_permission("verification.view"))):
    conn = ctx.conn
    where, params = ["1=1"], []
    scope_sql, scope_params = "", ()
    visible = visible_user_ids(conn, ctx)
    if visible is not None:
        where.append("pa.user_id = ANY(%s)")
        params.append(visible)
    if status:
        if status == "pending_agent":
            where.append("v.pipeline_status='pending_agent'")
        elif status == "auto_approved":
            where.append("v.status=%s AND pa.auto_verified=TRUE")
            params.append("approved")
        else:
            where.append("v.status=%s")
            params.append(status)
    if q:
        where.append("(ch.name ILIKE %s OR pa.invoice_number ILIKE %s OR u.full_name ILIKE %s)")
        params.extend([f"%{q}%"] * 3)
    sql, params = _full_query("WHERE " + " AND ".join(where), params)
    sql += " LIMIT %s OFFSET %s"
    params.extend([limit, offset])
    c = conn.cursor()
    c.execute(sql, params)
    items = fetchall_dict(c)
    c.execute("SELECT count(*) FROM pob_verifications v JOIN pob_activities pa ON pa.id=v.pob_id WHERE "
              + " AND ".join(where), params[:-2])
    return {"items": [_filter_for_role(r, ctx) for r in items], "total": c.fetchone()[0]}


@router.get("/product-aliases")
def list_product_aliases(campaign_id: int = None,
                         ctx: TenantContext = Depends(require_permission("verification.view"))):
    """List product aliases, optionally filtered by campaign."""
    from ..product_alias import list_product_aliases as _list
    return {"items": _list(ctx.conn, campaign_id)}


@router.get("/{vid}")
def verification_detail(vid: int, ctx: TenantContext = Depends(require_permission("verification.view"))):
    conn = ctx.conn
    c = conn.cursor()
    sql, params = _full_query("WHERE v.id=%s", (vid,))
    c.execute(sql, params)
    row = fetchone_dict(c)
    if not row:
        raise HTTPException(404, "verification not found")
    visible = visible_user_ids(conn, ctx)
    if visible is not None and row["user_id"] not in visible:
        raise HTTPException(403, "not allowed to view this verification")
    c.execute("SELECT * FROM verification_history WHERE pob_id=%s ORDER BY id", (row["pob_id"],))
    row["history"] = fetchall_dict(c)
    c.execute("SELECT * FROM ocr_extractions WHERE pob_id=%s ORDER BY id", (row["pob_id"],))
    row["ocr"] = fetchall_dict(c)
    for o in row["ocr"]:
        for k in ("fields", "raw"):
            if isinstance(o.get(k), str):
                try:
                    import json
                    o[k] = json.loads(o[k])
                except Exception:
                    o[k] = {}
    c.execute("SELECT * FROM pob_approvals WHERE pob_id=%s ORDER BY step", (row["pob_id"],))
    row["approvals"] = fetchall_dict(c)
    row["lines"] = _pob_lines(conn, row)
    _annotate_brand_matches(row)
    # Campaign product master (brand-wise qty/amount bounds) so the report can
    # show what the campaign expects per brand alongside the submission.
    c.execute(
        """SELECT cp.min_quantity, cp.min_pob, cp.max_pob, cp.scheme_eligibility,
           pr.*, b.name AS brand_name FROM campaign_products cp
           JOIN products pr ON pr.id=cp.product_id
           LEFT JOIN brands b ON b.id=pr.brand_id
           WHERE cp.campaign_id=%s AND pr.status='active' ORDER BY pr.name, pr.id""",
        (row["campaign_id"],),
    )
    row["campaign_products"] = fetchall_dict(c)
    # Full Invoice Proof verification report (extraction + business checks +
    # campaign eligibility) — shared with the MR's POB detail.
    from ..verification_checks import compute_checks
    row["report"] = compute_checks(conn, row)
    return _filter_for_role(row, ctx)


def _annotate_brand_matches(row):
    """Link each submitted product line to its AI-extracted invoice line so the
    UI can show which required/campaign brands were found on the invoice and
    how qty/amount were auto-updated from it. Also flags invoice brands that
    were NOT in the submitted POB (extras)."""
    from .. import ocr as _ocr
    items = []
    if row.get("ocr"):
        fields = row["ocr"][0].get("fields") or {}
        items = fields.get("items") or []
    lines = row["lines"] or []
    if not items:
        for line in lines:
            line["brand_matched"] = False
        row["extra_invoice_brands"] = []
        return

    matched_indices = set()
    for line in lines:
        match, idx = _match_line_to_items(line, items, matched_indices)
        if match is not None:
            line["extracted_qty"] = _ocr._normalise_amount(match.get("qty"))
            line["extracted_amount"] = _ocr._normalise_amount(match.get("amount"))
            line["brand_matched"] = True
        else:
            line["extracted_qty"] = None
            line["extracted_amount"] = None
            line["brand_matched"] = False

    extras = []
    for i, it in enumerate(items):
        if i not in matched_indices:
            extras.append(it)
    row["extra_invoice_brands"] = extras


def _match_line_to_items(line, items, matched_indices):
    """Best-effort: return the first unused extracted item matching this line
    (so each invoice brand maps to at most one submitted brand)."""
    from .. import ocr as _ocr
    for i, it in enumerate(items):
        if i in matched_indices:
            continue
        if _ocr.match_brand([it], line.get("product_name"), line.get("brand_name")):
            matched_indices.add(i)
            return it, i
    return None, None


def _pob_lines(conn, row):
    """All product lines of the same visit (submission group when available,
    otherwise same user + campaign + chemist), for a detailed view."""
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


@router.post("/{vid}/claim")
def claim_verification(vid: int, ctx: TenantContext = Depends(require_permission("verification.view"))):
    conn = ctx.conn
    c = conn.cursor()
    c.execute("SELECT * FROM pob_verifications WHERE id=%s FOR UPDATE", (vid,))
    v = fetchone_dict(c)
    if not v:
        raise HTTPException(404, "verification not found")
    _assert_scope(conn, ctx, vid)
    if v["status"] != "pending":
        raise HTTPException(409, "only pending items can be claimed")
    c.execute("UPDATE pob_verifications SET verifier_id=%s, started_at=CURRENT_TIMESTAMP WHERE id=%s",
              (ctx.user["id"], vid))
    conn.commit()
    log_action(conn, ctx.user["id"], "verification.claim", "pob_verification", vid)
    return {"ok": True}


@router.post("/{vid}/approve")
def approve_verification(vid: int, body: dict = None, request: Request = None,
                         ctx: TenantContext = Depends(require_permission("verification.approve"))):
    conn = ctx.conn
    c = conn.cursor()
    c.execute("SELECT v.*, pa.campaign_id, pa.user_id, pa.pob_amount, pa.invoice_number, "
              "pa.workflow_id, pa.workflow_step, pa.workflow_done, "
              "cmp.scheme_type, cmp.name AS campaign_name, u.username AS mr_username "
              "FROM pob_verifications v "
              "JOIN pob_activities pa ON pa.id=v.pob_id "
              "JOIN campaigns cmp ON cmp.id=pa.campaign_id "
              "JOIN users u ON u.id=pa.user_id WHERE v.id=%s FOR UPDATE OF v", (vid,))
    v = fetchone_dict(c)
    if not v:
        raise HTTPException(404, "verification not found")
    if v["status"] != "pending":
        raise HTTPException(409, f"item is already {v['status']}")

    _assert_scope(conn, ctx, vid)
    body = body or {}
    note = body.get("note")

    # ── Dynamic workflow path ────────────────────────────────────────────────
    if v["workflow_id"]:
        steps = _workflow_steps(conn, v["workflow_id"])
        idx = int(v["workflow_step"] or 0)
        if idx >= len(steps):
            return _finalize_approval(conn, v, vid, note, ctx, request)
        step = steps[idx]
        if step.get("role"):
            if not _role_matches(conn, ctx.user["id"], step["role"]):
                raise HTTPException(403, f"Step '{step.get('label')}' must be approved by role '{step['role']}'")
        c.execute("UPDATE pob_approvals SET status='approved', approver_id=%s, comment=%s, "
                  "decided_at=CURRENT_TIMESTAMP WHERE pob_id=%s AND step=%s",
                  (ctx.user["id"], note, v["pob_id"], idx + 1))
        c.execute("UPDATE pob_activities SET workflow_step=%s WHERE id=%s", (idx + 1, v["pob_id"]))
        c.execute("INSERT INTO verification_history (pob_id, verifier_id, action, reason) "
                  "VALUES (%s,%s,'approved',%s)", (v["pob_id"], ctx.user["id"], note or step.get("label")))
        conn.commit()
        log_action(conn, ctx.user["id"], "workflow.approve_step", "pob_approval", vid,
                   {"pob_id": v["pob_id"], "step": idx + 1, "role": step.get("role")},
                   request=request)
        if idx + 1 < len(steps):
            _notify_next_approvers(conn, v["pob_id"], steps[idx + 1])
            return {"ok": True, "status": "pending", "step": idx + 1,
                    "message": f"Step {idx + 1} approved; awaiting {steps[idx + 1].get('label')}"}
        return _finalize_approval(conn, v, vid, note, ctx, request)

    # ── Single-step (verifier) path ──────────────────────────────────────────
    return _finalize_approval(conn, v, vid, note, ctx, request)


def _role_matches(conn, user_id: int, role_name: str) -> bool:
    c = conn.cursor()
    c.execute("SELECT r.name FROM roles r JOIN users u ON u.role_id=r.id WHERE u.id=%s", (user_id,))
    row = c.fetchone()
    return bool(row and row[0] == role_name)


def _workflow_steps(conn, workflow_id):
    import json
    if not workflow_id:
        return []
    c = conn.cursor()
    c.execute("SELECT steps FROM workflow_definitions WHERE id=%s AND active=TRUE", (workflow_id,))
    row = c.fetchone()
    if not row:
        return []
    steps = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    return sorted(steps, key=lambda s: s.get("order", 0))


def _notify_next_approvers(conn, pob_id, step):
    role = step.get("role")
    if not role:
        return
    c = conn.cursor()
    c.execute("SELECT id FROM users WHERE role_id=(SELECT id FROM roles WHERE name=%s) AND status='active'", (role,))
    for (uid,) in c.fetchall():
        c.execute(
            "INSERT INTO notifications (user_id, channel, type, title, message, reference_type, reference_id) "
            "VALUES (%s,'inapp','workflow','Approval pending',%s,'pob',%s)",
            (uid, f"{step.get('label')} required for POB #{pob_id}", pob_id),
        )
    conn.commit()


def _finalize_approval(conn, v, vid, note, ctx, request=None):
    """Mark the POB verified and create the gratification via the rule engine."""
    from .. import rules, webhooks
    from ..notify import notify_from_template
    c = conn.cursor()

    c.execute("UPDATE pob_verifications SET status='approved', verifier_id=%s, "
              "verified_at=CURRENT_TIMESTAMP, reason=%s, pipeline_status='completed' WHERE id=%s",
              (ctx.user["id"], note, vid))
    c.execute("UPDATE pob_activities SET status='verified', workflow_done=TRUE, "
              "verification_state='approved' WHERE id=%s", (v["pob_id"],))

    gid, decision = rules.create_gratification(conn, v["pob_id"], v["user_id"],
                                               v["campaign_id"], v["pob_amount"], ctx.user["id"])
    c.execute("INSERT INTO verification_history (pob_id, verifier_id, action, reason) "
              "VALUES (%s,%s,'approved',%s)", (v["pob_id"], ctx.user["id"], note))
    conn.commit()
    log_action(conn, ctx.user["id"], "verification.approve", "pob_verification", vid,
               {"pob_id": v["pob_id"], "gratification_id": gid, "rule": decision.get("rule_name")},
               request=request)
    notify_from_template(conn, v["user_id"], "pob.approved",
                         {"pob_id": v["pob_id"], "campaign": v.get("campaign_name")},
                         "pob", v["pob_id"])
    webhooks.dispatch_event(conn, "pob.approved", {
        "pob_id": v["pob_id"], "verification_id": vid,
        "invoice_number": v.get("invoice_number"),
        "gratification_id": gid, "rule": decision.get("rule_name"),
    }, ctx.claims.get("tenant_db", ""))
    return {"ok": True, "status": "approved", "gratification_id": gid,
            "decision": decision.get("action")}


@router.post("/{vid}/reject")
def reject_verification(vid: int, body: dict, request: Request = None,
                        ctx: TenantContext = Depends(require_permission("verification.reject"))):
    reason = (body.get("reason") or "").strip()
    if not reason:
        raise HTTPException(400, "reason is mandatory when rejecting")
    conn = ctx.conn
    c = conn.cursor()
    c.execute("SELECT v.*, pa.user_id, pa.invoice_number FROM pob_verifications v "
              "JOIN pob_activities pa ON pa.id=v.pob_id WHERE v.id=%s FOR UPDATE OF v", (vid,))
    v = fetchone_dict(c)
    if not v:
        raise HTTPException(404, "verification not found")
    if v["status"] != "pending":
        raise HTTPException(409, f"item is already {v['status']}")
    _assert_scope(conn, ctx, vid)
    c.execute("UPDATE pob_verifications SET status='rejected', verifier_id=%s, "
              "verified_at=CURRENT_TIMESTAMP, reason=%s, pipeline_status='completed' WHERE id=%s",
              (ctx.user["id"], reason, vid))
    c.execute("UPDATE pob_activities SET status='rejected', verification_state='rejected' WHERE id=%s", (v["pob_id"],))
    c.execute("INSERT INTO verification_history (pob_id, verifier_id, action, reason) "
              "VALUES (%s,%s,'rejected',%s)", (v["pob_id"], ctx.user["id"], reason))
    conn.commit()
    log_action(conn, ctx.user["id"], "verification.reject", "pob_verification", vid,
               {"pob_id": v["pob_id"], "reason": reason}, request=request)
    notify_from_template(conn, v["user_id"], "pob.rejected",
                         {"pob_id": v["pob_id"], "reason": reason}, "pob", v["pob_id"])
    from ..webhooks import dispatch_event
    dispatch_event(conn, "pob.rejected", {
        "pob_id": v["pob_id"], "verification_id": vid,
        "invoice_number": v.get("invoice_number"), "reason": reason,
    }, ctx.claims.get("tenant_db", ""))
    return {"ok": True, "status": "rejected"}


@router.post("/{vid}/duplicate")
def mark_duplicate(vid: int, body: dict,
                   ctx: TenantContext = Depends(require_permission("verification.reject"))):
    conn = ctx.conn
    c = conn.cursor()
    c.execute("SELECT * FROM pob_verifications WHERE id=%s", (vid,))
    v = fetchone_dict(c)
    if not v:
        raise HTTPException(404, "verification not found")
    _assert_scope(conn, ctx, vid)
    c.execute("UPDATE pob_verifications SET status='duplicate', verifier_id=%s, "
              "verified_at=CURRENT_TIMESTAMP, reason=%s, duplicate_of=%s WHERE id=%s",
              (ctx.user["id"], body.get("reason") or "Marked duplicate",
               body.get("duplicate_of"), vid))
    c.execute("UPDATE pob_activities SET status='duplicate' WHERE id=%s", (v["pob_id"],))
    conn.commit()
    log_action(conn, ctx.user["id"], "verification.mark_duplicate", "pob_verification", vid)
    return {"ok": True, "status": "duplicate"}


@router.post("/{vid}/re_open")
def re_open_verification(vid: int, body: dict = None, request: Request = None,
                         ctx: TenantContext = Depends(require_permission("verification.approve"))):
    """Re-open an auto-approved (or manually approved) verification for manual re-review.

    Creates a fresh pending verification record so the verifier can confirm or
    reject the POB.  Used when a client disputes an auto-approved POB.
    """
    conn = ctx.conn
    c = conn.cursor()
    c.execute("SELECT v.*, pa.user_id, pa.pob_amount, pa.invoice_number, pa.auto_verified "
              "FROM pob_verifications v JOIN pob_activities pa ON pa.id=v.pob_id WHERE v.id=%s", (vid,))
    v = fetchone_dict(c)
    if not v:
        raise HTTPException(404, "verification not found")
    _assert_scope(conn, ctx, vid)
    if v["status"] not in ("approved",):
        raise HTTPException(409, f"Can only re-open approved verifications (current: {v['status']})")
    body = body or {}
    reason = body.get("reason") or "Re-opened for manual re-verification"

    # Mark the old verification as superseded
    c.execute("UPDATE pob_verifications SET status='superseded', reason=%s WHERE id=%s",
              (f"Re-opened by verifier: {reason}", vid))
    # Create a fresh pending verification
    c.execute(
        """INSERT INTO pob_verifications (pob_id, status, reason, pipeline_status)
           VALUES (%s,'pending',%s,'pending_agent') RETURNING id""",
        (v["pob_id"], reason),
    )
    new_vid = c.fetchone()[0]
    c.execute("UPDATE pob_activities SET status='pending_verification', auto_verified=FALSE, "
              "current_verification_id=%s, verification_state='manual_review' WHERE id=%s",
              (new_vid, v["pob_id"]))
    c.execute("INSERT INTO verification_history (pob_id, verifier_id, action, reason) "
              "VALUES (%s,%s,'re_opened',%s)", (v["pob_id"], ctx.user["id"], reason))
    conn.commit()
    log_action(conn, ctx.user["id"], "verification.re_open", "pob_verification", vid,
               {"pob_id": v["pob_id"], "new_verification_id": new_vid, "reason": reason},
               request=request)
    return {"ok": True, "status": "pending", "verification_id": new_vid,
            "message": "POB re-opened for manual re-verification"}


@router.get("/tat/report")
def tat_report(ctx: TenantContext = Depends(require_permission("verification.view"))):
    conn = ctx.conn
    c = conn.cursor()
    c.execute(
        """SELECT verifier_id, u.full_name AS verifier_name,
           count(*) AS verified,
           coalesce(avg(extract(epoch from (v.verified_at - v.created_at))/3600.0), 0) AS avg_tat_hours
           FROM pob_verifications v LEFT JOIN users u ON u.id=v.verifier_id
           WHERE v.verified_at IS NOT NULL GROUP BY verifier_id, u.full_name ORDER BY verified DESC""",
    )
    return {"items": fetchall_dict(c)}


# ═══════════════════════════════════════════════════════════════════════════
# Phase 9: AI-First Verification Pipeline Endpoints
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/{vid}/pipeline")
def verification_pipeline_detail(vid: int,
                                 ctx: TenantContext = Depends(require_permission("verification.view"))):
    """Return the pipeline step history for a verification."""
    conn = ctx.conn
    c = conn.cursor()
    c.execute("SELECT id FROM pob_verifications WHERE id=%s", (vid,))
    if not c.fetchone():
        raise HTTPException(404, "verification not found")
    _assert_scope(conn, ctx, vid)
    c.execute("""
        SELECT id, step_name, step_status, detail, started_at, completed_at
        FROM verification_pipeline_steps
        WHERE verification_id = %s
        ORDER BY id
    """, (vid,))
    steps = fetchall_dict(c)
    return {"verification_id": vid, "steps": steps}


@router.post("/{vid}/correct")
def correct_verification(vid: int, body: dict, request: Request = None,
                         ctx: TenantContext = Depends(require_permission("verification.approve"))):
    """Agent correction: update a field on the POB after manual review.

    body: {"field_name": str, "corrected_value": str, "reason": str}
    """
    field_name = (body.get("field_name") or "").strip()
    corrected_value = body.get("corrected_value")
    reason = (body.get("reason") or "").strip()
    if not field_name or not reason:
        raise HTTPException(400, "field_name and reason are required")

    conn = ctx.conn
    c = conn.cursor()
    c.execute("SELECT v.*, pa.id AS pa_id, pa.user_id FROM pob_verifications v "
              "JOIN pob_activities pa ON pa.id=v.pob_id WHERE v.id=%s", (vid,))
    v = fetchone_dict(c)
    if not v:
        raise HTTPException(404, "verification not found")
    _assert_scope(conn, ctx, vid)

    # Get original value
    original_value = None
    if field_name == "invoice_number":
        c.execute("SELECT invoice_number FROM pob_activities WHERE id=%s", (v["pa_id"],))
        row = c.fetchone()
        original_value = row[0] if row else None
        c.execute("UPDATE pob_activities SET invoice_number=%s, invoice_number_norm=%s WHERE id=%s",
                  (corrected_value,
                   "".join(ch for ch in str(corrected_value or "").upper() if ch.isalnum()),
                   v["pa_id"]))
    elif field_name == "invoice_amount":
        c.execute("SELECT invoice_amount FROM pob_activities WHERE id=%s", (v["pa_id"],))
        row = c.fetchone()
        original_value = str(row[0]) if row else None
        c.execute("UPDATE pob_activities SET invoice_amount=%s WHERE id=%s", (corrected_value, v["pa_id"]))
    elif field_name == "invoice_date":
        c.execute("SELECT invoice_date FROM pob_activities WHERE id=%s", (v["pa_id"],))
        row = c.fetchone()
        original_value = row[0] if row else None
        c.execute("UPDATE pob_activities SET invoice_date=%s WHERE id=%s", (corrected_value, v["pa_id"]))
    elif field_name == "quantity":
        c.execute("SELECT quantity FROM pob_activities WHERE id=%s", (v["pa_id"],))
        row = c.fetchone()
        original_value = str(row[0]) if row else None
        c.execute("UPDATE pob_activities SET quantity=%s WHERE id=%s", (corrected_value, v["pa_id"]))
    elif field_name == "product_id":
        c.execute("SELECT product_id FROM pob_activities WHERE id=%s", (v["pa_id"],))
        row = c.fetchone()
        original_value = str(row[0]) if row else None
        c.execute("UPDATE pob_activities SET product_id=%s WHERE id=%s", (int(corrected_value), v["pa_id"]))
    else:
        raise HTTPException(400, f"Cannot correct field: {field_name}")

    # Record correction
    c.execute("""
        INSERT INTO verification_corrections
            (verification_id, agent_id, field_name, original_value, corrected_value, reason)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (vid, ctx.user["id"], field_name, original_value, str(corrected_value), reason))

    # Increment correction count
    c.execute("UPDATE pob_verifications SET correction_count = correction_count + 1 WHERE id=%s", (vid,))

    # History
    c.execute("INSERT INTO verification_history (pob_id, verifier_id, action, reason) "
              "VALUES (%s,%s,'corrected',%s)",
              (v["pob_id"], ctx.user["id"],
               f"Corrected {field_name}: {original_value} → {corrected_value} ({reason})"))
    conn.commit()

    log_action(conn, ctx.user["id"], "verification.correct", "pob_verification", vid,
               {"pob_id": v["pob_id"], "field": field_name, "from": original_value,
                "to": corrected_value, "reason": reason},
               request=request)
    notify_from_template(conn, v["user_id"], "pob.correction_required",
                         {"pob_id": v["pob_id"], "field": field_name,
                          "reason": reason}, "pob", v["pob_id"])
    return {"ok": True, "field": field_name, "original": original_value, "corrected": corrected_value}


@router.get("/{vid}/corrections")
def verification_corrections(vid: int,
                             ctx: TenantContext = Depends(require_permission("verification.view"))):
    """List all agent corrections for a verification."""
    conn = ctx.conn
    c = conn.cursor()
    _assert_scope(conn, ctx, vid)
    c.execute("""
        SELECT vc.id, vc.field_name, vc.original_value, vc.corrected_value,
               vc.reason, vc.created_at, u.full_name AS agent_name
        FROM verification_corrections vc
        LEFT JOIN users u ON u.id = vc.agent_id
        WHERE vc.verification_id = %s
        ORDER BY vc.id
    """, (vid,))
    return {"items": fetchall_dict(c)}


@router.post("/run-pipeline")
def run_verification_pipeline(body: dict, request: Request = None,
                              ctx: TenantContext = Depends(require_permission("verification.approve"))):
    """Manually trigger the verification pipeline for a verification.

    body: {"verification_id": int}
    """
    from ..verification_pipeline import run_pipeline
    from ..db_utils import fetchone_dict as _f1
    vid = body.get("verification_id")
    if not vid:
        raise HTTPException(400, "verification_id required")

    conn = ctx.conn
    c = conn.cursor()
    c.execute("""
        SELECT v.*, pa.campaign_id, pa.product_id, pa.chemist_id, pa.user_id,
               pa.invoice_number, pa.invoice_amount, pa.invoice_date,
               pa.quantity, pa.pob_amount, pa.ptr, pa.invoice_path
        FROM pob_verifications v
        JOIN pob_activities pa ON pa.id = v.pob_id
        WHERE v.id = %s
    """, (vid,))
    v = fetchone_dict(c)
    if not v:
        raise HTTPException(404, "verification not found")
    _assert_scope(conn, ctx, vid)

    c.execute("SELECT * FROM campaigns WHERE id=%s", (v["campaign_id"],))
    campaign = fetchone_dict(c)
    c.execute("SELECT * FROM products WHERE id=%s", (v["product_id"],))
    product = fetchone_dict(c)
    c.execute("""SELECT cp.min_quantity, cp.min_pob, cp.max_pob, cp.scheme_eligibility
                 FROM campaign_products cp
                 WHERE cp.campaign_id=%s AND cp.product_id=%s""",
              (v["campaign_id"], v["product_id"]))
    link = fetchone_dict(c)
    if product and link:
        product = {**product, **link}
    c.execute("SELECT * FROM chemists WHERE id=%s", (v["chemist_id"],))
    chemist = fetchone_dict(c)

    # Get the latest extraction
    c.execute("SELECT * FROM ocr_extractions WHERE pob_id=%s ORDER BY id DESC LIMIT 1", (v["pob_id"],))
    ocr_row = fetchone_dict(c)
    extraction = {}
    if ocr_row:
        for k in ("fields", "raw"):
            val = ocr_row.get(k)
            if isinstance(val, str):
                try:
                    import json
                    extraction[k] = json.loads(val)
                except Exception:
                    extraction[k] = {}
            else:
                extraction[k] = val
        extraction["confidence"] = ocr_row.get("confidence", 0)
        extraction["engine"] = ocr_row.get("engine", "gemini")

    submitted_data = {
        "invoice_number": v.get("invoice_number"),
        "invoice_amount": v.get("invoice_amount"),
        "invoice_date": v.get("invoice_date"),
        "product_name": product.get("name") if product else "",
        "quantity": v.get("quantity"),
        "pob_amount": v.get("pob_amount"),
        "ptr": v.get("ptr"),
    }

    result = run_pipeline(conn, vid, v["pob_id"], campaign, product, chemist,
                          extraction, submitted_data,
                          auto_verify=campaign.get("auto_verify", False))

    # Update verification and POB based on result
    from .. import rules
    c = conn.cursor()
    if result.decision == "auto_approved":
        c.execute("UPDATE pob_verifications SET status='approved', pipeline_status='completed', "
                  "ai_extraction=%s, normalized_data=%s, mapped_product_id=%s, "
                  "product_mapping_confidence=%s, duplicate_check_result=%s, "
                  "completeness_score=%s, verifier_id=%s, verified_at=CURRENT_TIMESTAMP, "
                  "reason='Auto-approved by pipeline' WHERE id=%s",
                  (json.dumps(result.ai_extraction), json.dumps(result.normalized_data),
                   result.mapped_product_id, result.product_mapping_confidence,
                   json.dumps(result.duplicate_check_result), result.completeness_score,
                   ctx.user["id"], vid))
        c.execute("UPDATE pob_activities SET status='verified', auto_verified=TRUE, "
                  "confidence=%s, verification_state='approved' WHERE id=%s",
                  (result.confidence, v["pob_id"]))
        gid, decision = rules.create_gratification(conn, v["pob_id"], v["user_id"],
                                                   v["campaign_id"], v["pob_amount"], ctx.user["id"])
        c.execute("INSERT INTO verification_history (pob_id, verifier_id, action, reason) "
                  "VALUES (%s,%s,'approved',%s)",
                  (v["pob_id"], ctx.user["id"], "Auto-approved by pipeline"))
        from ..notify import notify_from_template
        notify_from_template(conn, v["user_id"], "pob.auto_approved",
                             {"pob_id": v["pob_id"], "campaign": campaign.get("name", "")}, "pob", v["pob_id"])
    elif result.decision == "auto_rejected":
        reason_text = "; ".join(f["detail"] for f in result.rule_failures[:3]) or "Auto-rejected by pipeline"
        c.execute("UPDATE pob_verifications SET status='rejected', pipeline_status='completed', "
                  "ai_extraction=%s, normalized_data=%s, duplicate_check_result=%s, "
                  "completeness_score=%s, verifier_id=%s, verified_at=CURRENT_TIMESTAMP, "
                  "reason=%s WHERE id=%s",
                  (json.dumps(result.ai_extraction), json.dumps(result.normalized_data),
                   json.dumps(result.duplicate_check_result), result.completeness_score,
                   ctx.user["id"], reason_text, vid))
        c.execute("UPDATE pob_activities SET status='rejected', verification_state='rejected' WHERE id=%s",
                  (v["pob_id"],))
        c.execute("INSERT INTO verification_history (pob_id, verifier_id, action, reason) "
                  "VALUES (%s,%s,'rejected',%s)", (v["pob_id"], ctx.user["id"], reason_text))
    else:
        # Manual review
        c.execute("UPDATE pob_verifications SET pipeline_status='pending_agent', "
                  "ai_extraction=%s, normalized_data=%s, mapped_product_id=%s, "
                  "product_mapping_confidence=%s, duplicate_check_result=%s, "
                  "completeness_score=%s WHERE id=%s",
                  (json.dumps(result.ai_extraction), json.dumps(result.normalized_data),
                   result.mapped_product_id, result.product_mapping_confidence,
                   json.dumps(result.duplicate_check_result), result.completeness_score, vid))
        c.execute("UPDATE pob_activities SET status='pending_verification', "
                  "verification_state='manual_review' WHERE id=%s", (v["pob_id"],))
        from ..notify import notify_from_template
        notify_from_template(conn, v["user_id"], "pob.needs_review",
                             {"pob_id": v["pob_id"], "mr_name": v.get("user_id"),
                              "campaign": campaign.get("name", "")}, "pob", v["pob_id"])

    conn.commit()
    log_action(conn, ctx.user["id"], "verification.run_pipeline", "pob_verification", vid,
               {"pob_id": v["pob_id"], "decision": result.decision, "confidence": result.confidence},
               request=request)
    return {
        "ok": True, "decision": result.decision, "confidence": result.confidence,
        "matches": result.matches, "mismatches": result.mismatches,
        "pipeline_status": result.pipeline_status,
        "steps": result.steps,
    }


@router.post("/product-aliases")
def create_product_alias(body: dict,
                         ctx: TenantContext = Depends(require_permission("verification.manage"))):
    """Create a new product alias for matching."""
    from ..product_alias import add_product_alias
    product_id = body.get("product_id")
    alias_text = (body.get("alias_text") or "").strip()
    match_type = body.get("match_type", "exact")
    confidence_weight = float(body.get("confidence_weight", 1.0))
    if not product_id or not alias_text:
        raise HTTPException(400, "product_id and alias_text required")
    alias_id = add_product_alias(ctx.conn, int(product_id), alias_text,
                                  match_type, confidence_weight, ctx.user["id"])
    return {"ok": True, "alias_id": alias_id}


@router.delete("/product-aliases/{alias_id}")
def delete_product_alias(alias_id: int,
                         ctx: TenantContext = Depends(require_permission("verification.manage"))):
    """Soft-delete a product alias."""
    from ..product_alias import remove_product_alias
    ok = remove_product_alias(ctx.conn, alias_id)
    if not ok:
        raise HTTPException(404, "alias not found")
    return {"ok": True}


@router.get("/verification-rules/{campaign_id}")
def list_verification_rules_for_campaign(campaign_id: int,
                                         ctx: TenantContext = Depends(require_permission("verification.view"))):
    """List verification rules for a campaign."""
    from ..verification_rules import list_verification_rules
    return {"items": list_verification_rules(ctx.conn, campaign_id)}


@router.post("/verification-rules")
def create_verification_rule(body: dict,
                             ctx: TenantContext = Depends(require_permission("verification.manage"))):
    """Create a new campaign verification rule."""
    from ..verification_rules import add_verification_rule
    campaign_id = body.get("campaign_id")
    rule_type = (body.get("rule_type") or "").strip()
    params = body.get("params") or {}
    action_on_fail = body.get("action_on_fail", "flag")
    if not campaign_id or not rule_type:
        raise HTTPException(400, "campaign_id and rule_type required")
    rule_id = add_verification_rule(ctx.conn, int(campaign_id), rule_type, params, action_on_fail)
    return {"ok": True, "rule_id": rule_id}


@router.delete("/verification-rules/{rule_id}")
def delete_verification_rule(rule_id: int,
                             ctx: TenantContext = Depends(require_permission("verification.manage"))):
    """Soft-delete a verification rule."""
    from ..verification_rules import remove_verification_rule
    ok = remove_verification_rule(ctx.conn, rule_id)
    if not ok:
        raise HTTPException(404, "rule not found")
    return {"ok": True}
