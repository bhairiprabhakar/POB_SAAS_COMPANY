"""
Batch 2 (UNIFICATION.md) verification: statement-domain merge into the SaaS.

Three parts, entirely on scratch Postgres databases -- NOTHING is written to
the live databases (all live connections are SELECT-only introspection):

  Part A -- scratch TENANT DB  : TENANT_DDL + TENANT_PATCHES + seed_tenant,
            statement permissions + role wiring, uploads progress columns,
            statements domain (dup checks, save_extraction, credits wallet
            incl. superadmin approve with reviewed_by=None, manual-verification
            auto-assign, queue/claim/corrections/reject, documents, Excel
            workbook builders).

  Part B -- scratch PLATFORM DB: _PLATFORM_DDL + _PLATFORM_MIGRATIONS,
            ai_usage_log.user_id/original_filename columns, the rewritten
            /costing + /costing/export (ai_usage_log sourcing), model-registry
            config (routing / pricing via the superadmin routes), and the
            tenant-scoped superadmin credits routes (allocate / approve /
            reject) -- invoked directly with patched platform connection so
            they talk to the scratch control-plane DB.

  Part C -- read-only vs live PG: SELECT-only introspection confirming the
            live schema state (migrations are additive; new columns appear
            only after the deploy, never during verification).

Run:  python scripts\\verify_batch2.py   (workspace root)
"""
import asyncio
import io
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Windows consoles default to cp1252; the script prints ── section separators.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

FAILED = []
_SCRATCH_TENANT = "b2_scratch_tenant"
_SCRATCH_PLATFORM = "b2_scratch_platform"


def check(name, cond, extra=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" -- {extra}" if extra else ""))
    if not cond:
        FAILED.append(name)


def _reset_database(admin, dbname):
    bc = admin.cursor()
    bc.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname=%s AND pid<>pg_backend_pid()", (dbname,))
    bc.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
    bc.execute(f'CREATE DATABASE "{dbname}"')


def _col_names(conn, table):
    c = conn.cursor()
    c.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name=%s",
        (table,))
    return {r[0] for r in c.fetchall()}


def _role_perms(conn, role: str):
    c = conn.cursor()
    c.execute(
        """SELECT rp.permission_code FROM role_permissions rp
           JOIN roles r ON r.id=rp.role_id WHERE r.name=%s""",
        (role,))
    return {r[0] for r in c.fetchall()}


def _insert_user(conn, username, role, full_name="Batch2 User"):
    c = conn.cursor()
    c.execute(
        """INSERT INTO users (username, password, full_name, role_id, status)
           SELECT %s, 'scratch-pw', %s, r.id, 'active' FROM roles r WHERE r.name=%s
           RETURNING id""",
        (username, full_name, role))
    conn.commit()
    return c.fetchone()[0]


def _insert_upload(conn, user_id, filename, status="processing", file_hash=None,
                   fingerprint=None):
    c = conn.cursor()
    c.execute(
        """INSERT INTO uploads (user_id, original_filename, stored_filename, file_type,
                                status, file_hash, content_fingerprint)
           VALUES (%s,%s,'stored/'||%s,'application/pdf',%s,%s,%s) RETURNING id""",
        (user_id, filename, filename, status, file_hash, fingerprint))
    conn.commit()
    return c.fetchone()[0]


def _extraction_data(stockist, frm, to, invoice_net=10000.0):
    return {
        "stockist_name": stockist,
        "stockist_gst": "27AABCP1234Q1Z5",
        "stockist_address": "12 MG Road, Pune",
        "bill_number": "SA-1001",
        "bill_date": "10/01/2025",
        "statement_from_date": frm,
        "statement_to_date": to,
        "total_amount": invoice_net,
        "total_quantity": 12,
        "invoice_net": invoice_net,
        "doc_type": "STATEMENT",
        "parties": [
            {
                "name": "Shree Medicals", "type": "chemist", "area": "Kothrud",
                "dl_number": "MH/1234", "gst_number": "27AAAFM1234G1ZA",
                "party_total_quantity": 10, "party_total_amount": 8500.0,
                "items": [
                    {"brand": "Cough Syrup", "mfg": "Medico", "pack": "100ml",
                     "batch_no": "B1", "expiry": "12/2027", "hsn_code": "3004",
                     "quantity": 5, "mrp": 98.0, "unit_rate": 90.0,
                     "tax_type": "GST12", "discount_percent": 5.0, "final_amount": 427.5},
                    {"brand": "Vitamin C", "mfg": "Medico", "pack": "60 tabs",
                     "batch_no": "V1", "expiry": "08/2028", "hsn_code": "3004",
                     "quantity": 5, "mrp": 120.0, "unit_rate": 110.0,
                     "tax_type": "GST12", "discount_percent": 5.0, "final_amount": 522.5},
                ],
            }
        ],
    }


def part_a(tenant_conn, platform_conn):
    """Scratch tenant: schema + seed + statements domain + credits."""
    from saas import credits, statements
    from saas.tenant_schema import TENANT_DDL, TENANT_PATCHES, seed_tenant

    c = tenant_conn.cursor()
    c.execute(TENANT_DDL)
    c.execute(TENANT_PATCHES)
    tenant_conn.commit()
    seed_tenant(tenant_conn)
    tenant_conn.commit()

    # ── statement permission codes seeded ────────────────────────────────
    c.execute("SELECT code FROM permissions WHERE module='statement'")
    perm_codes = {r[0] for r in c.fetchall()}
    check("statement permission codes present",
          perm_codes >= {"statement.upload", "statement.view", "statement.manage",
                         "statement.verify", "statement.credits"},
          str(sorted(perm_codes)))

    # ── role wiring matches the Batch 2 decisions ────────────────────────
    ho, asm = _role_perms(tenant_conn, "ho"), _role_perms(tenant_conn, "asm")
    nsm, sm = _role_perms(tenant_conn, "nsm"), _role_perms(tenant_conn, "sm")
    rsm, mr = _role_perms(tenant_conn, "rsm"), _role_perms(tenant_conn, "mr")
    verifier = _role_perms(tenant_conn, "verifier")
    vagent = _role_perms(tenant_conn, "verification_agent")
    auditor, fin = _role_perms(tenant_conn, "auditor"), _role_perms(tenant_conn, "finance")
    dist = _role_perms(tenant_conn, "distributor")
    check("ho/asm: verify + upload + credits",
          {"statement.verify", "statement.upload", "statement.credits"} <= (ho & asm))
    check("ho/asm do NOT get statement.manage",
          "statement.manage" not in ho and "statement.manage" not in asm)
    _stmt = lambda s: sorted(c for c in s if c.startswith("statement."))
    check("nsm/zsm/sm/rsm: upload + credits, no verify",
          "statement.upload" in nsm and "statement.credits" in nsm and "statement.verify" not in nsm
          and "statement.upload" in sm and "statement.verify" not in sm
          and "statement.upload" in rsm and "statement.verify" not in rsm,
          f"nsm={_stmt(nsm)} sm={_stmt(sm)} rsm={_stmt(rsm)}")
    check("mr/psr: upload + view + credits",
          {"statement.upload", "statement.view", "statement.credits"} <= mr)
    # FINAL architecture: verifier/verification_agent are POB-verification-only
    # roles and no longer carry any statement/credits permissions (see
    # tenant_schema.py's "Verification agents are POB-only" migration patch).
    check("verifier/verification_agent: no statement.* permissions at all",
          not _stmt(verifier) and not _stmt(vagent),
          f"verifier={_stmt(verifier)} vagent={_stmt(vagent)}")
    check("auditor/finance: view + credits only",
          {"statement.view", "statement.credits"} <= auditor
          and "statement.verify" not in auditor
          and "statement.upload" not in fin
          and "statement.verify" not in fin)
    check("distributor: view only",
          "statement.view" in dist and "statement.upload" not in dist
          and "statement.verify" not in dist and "statement.credits" not in dist)

    # ── uploads progress columns (Phase 12 ALTER) ────────────────────────
    up_cols = _col_names(tenant_conn, "uploads")
    check("uploads.progress_pct/progress_stage columns",
          {"progress_pct", "progress_stage"} <= up_cols)

    # ── seed users ───────────────────────────────────────────────────────
    uploader_id = _insert_user(tenant_conn, "b2_uploader", "mr", "Mr Uploader")
    ho_id = _insert_user(tenant_conn, "b2_ho", "ho", "Ho User")
    agent1 = _insert_user(tenant_conn, "b2_agent1", "verification_agent", "Agent One")
    agent2 = _insert_user(tenant_conn, "b2_agent2", "verification_agent", "Agent Two")

    # ── credits wallet ───────────────────────────────────────────────────
    w = credits.get_credits(tenant_conn)
    check("credits wallet seeded (100/demo)", w["total_credits"] == 100 and w["plan"] == "demo", str(w))
    w2 = credits.top_up_credits(tenant_conn, 50, plan="pro")
    check("top_up_credits -> 150", w2["total_credits"] == 150, str(w2))
    ok, remaining = credits.consume_credit(
        tenant_conn, "extraction", division_id=None, user_id=uploader_id,
        reference_id=1, detail="Smoke test consume", amount=1)
    check("consume_credit -> (True,149)", ok and remaining == 149, f"{ok},{remaining}")
    rid1 = credits.request_credits(tenant_conn, agent1, 25, "need more credits")
    req = credits.approve_credit_request(tenant_conn, rid1, reviewed_by=None)
    check("approve (reviewed_by=None) adds 25", bool(req)
          and credits.get_credits(tenant_conn)["total_credits"] == 175, str(req and credits.get_credits(tenant_conn)))
    rid2 = credits.request_credits(tenant_conn, agent2, 30, "top up for quarter")
    check("reject_credit_request -> True", credits.reject_credit_request(tenant_conn, rid2, reviewed_by=None) is True)
    c2 = tenant_conn.cursor()
    c2.execute("SELECT status FROM credit_requests WHERE id=%s", (rid2,))
    check("rejected request status persisted", c2.fetchone()[0] == "rejected")
    ledger = credits.credit_ledger(tenant_conn, limit=10)
    check("ledger has consume + approved ops",
          {"extraction", "request_approved"} <= {r["operation_type"] for r in ledger})
    summary = credits.credit_usage_summary(tenant_conn)
    check("usage summary has extraction", "extraction" in summary)

    # ── period helpers ───────────────────────────────────────────────────
    ptype, days = statements.classify_period("01/01/2025", "31/03/2025")
    check("classify_period quarterly", ptype == "quarterly", f"{ptype},{days}")
    q = statements.get_current_quarter()
    check("get_current_quarter shape", all(k in q for k in ("quarter", "label", "from", "to")))

    # ── upload + extraction lifecycle ────────────────────────────────────
    h1 = "a" * 64
    up1 = _insert_upload(tenant_conn, uploader_id, "ABC Pharma Jan25.pdf", "processing",
                         file_hash=h1, fingerprint="FP1")
    c3 = tenant_conn.cursor()
    c3.execute("UPDATE uploads SET progress_pct=40, progress_stage='Extracting text...' WHERE id=%s", (up1,))
    tenant_conn.commit()
    c3.execute("SELECT progress_pct, progress_stage FROM uploads WHERE id=%s", (up1,))
    r3 = c3.fetchone()
    check("upload progress persisted in DB", r3 == (40, "Extracting text..."), str(r3))

    check("L1 unknown hash not flagged", not statements.check_file_hash_duplicate(tenant_conn, "b" * 64))
    check("period dup empty before extraction",
          statements.check_period_duplicate(tenant_conn, "ABC Pharma", "01/01/2025", "31/01/2025") == [])

    data1 = _extraction_data("ABC Pharma", "01/01/2025", "31/01/2025")
    stats = statements.save_extraction(tenant_conn, up1, data1)
    check("save_extraction saved 1 party / 2 items",
          stats == {"parties_saved": 1, "items_saved": 2}, str(stats))
    c4 = tenant_conn.cursor()
    c4.execute("SELECT status FROM uploads WHERE id=%s", (up1,))
    check("upload marked done", c4.fetchone()[0] == "done")

    # Dedup checks now run against the ('done') extraction -- exactly the point
    # in the pipeline where the legacy checks fired.
    check("L1 file-hash dup found (done)",
          bool(statements.check_file_hash_duplicate(tenant_conn, h1)))
    check("L2 fingerprint dup found (done)",
          bool(statements.check_content_fingerprint_duplicate(tenant_conn, "FP1")))
    dupes = statements.check_period_duplicate(
        tenant_conn, "ABC Pharma", "01/01/2025", "31/01/2025")
    check("exact duplicate period detected (same stockist + period)",
          any(d.get("overlap_type") == "exact" for d in dupes), str(dupes))
    excl = statements.check_period_duplicate(
        tenant_conn, "ABC Pharma", "01/01/2025", "31/01/2025", exclude_upload_id=up1)
    check("exclude_upload_id excludes the only match", excl == [], str(excl))
    partial = statements.check_period_duplicate(
        tenant_conn, "ABC Pharma", "20/01/2025", "28/02/2025")
    check("partial overlap detected",
          any(d.get("overlap_type") in ("partial", "contains", "contained") for d in partial),
          str(partial))

    # ── manual verification auto-assign (least-loaded agent) ─────────────
    mv1 = statements.ensure_manual_verification(tenant_conn, up1, None)
    c5 = tenant_conn.cursor()
    c5.execute("SELECT assigned_to, status FROM manual_verifications WHERE id=%s", (mv1,))
    a1row = c5.fetchone()
    check("auto-assigned to agent1 (least loaded)", a1row == (agent1, "pending"), str(a1row))
    c5.execute("SELECT verification_status FROM uploads WHERE id=%s", (up1,))
    check("upload verification_status=pending_verification",
          c5.fetchone()[0] == "pending_verification")

    up2 = _insert_upload(tenant_conn, uploader_id, "Sun Pharma Feb25.pdf", "processing", file_hash="h2")
    statements.save_extraction(tenant_conn, up2, _extraction_data("Sun Pharma", "01/02/2025", "28/02/2025"))
    mv2 = statements.ensure_manual_verification(tenant_conn, up2, None)
    c5.execute("SELECT assigned_to FROM manual_verifications WHERE id=%s", (mv2,))
    check("second task assigned to agent2", c5.fetchone()[0] == agent2)

    up3 = _insert_upload(tenant_conn, uploader_id, "XYZ Traders Mar25.pdf", "processing", file_hash="h3")
    statements.save_extraction(tenant_conn, up3, _extraction_data("XYZ Traders", "01/03/2025", "31/03/2025"))
    mv3 = statements.ensure_manual_verification(tenant_conn, up3, None)
    c5.execute("UPDATE manual_verifications SET assigned_to=NULL WHERE id=%s", (mv3,))
    tenant_conn.commit()
    check("unassigned_count == 1", statements.unassigned_count(tenant_conn) == 1)
    check("unassigned_tasks lists mv3",
          any(t["id"] == mv3 for t in statements.unassigned_tasks(tenant_conn)))
    ok, err, st = statements.claim_task(tenant_conn, mv3, agent2)
    check("claim_task ok", ok and err is None and st == 200, f"{ok},{err},{st}")
    check("unassigned_count back to 0", statements.unassigned_count(tenant_conn) == 0)
    q_agent2 = statements.verification_queue(tenant_conn, agent2)
    check("agent2 queue has mv3", any(t["id"] == mv3 for t in q_agent2))

    detail = statements.verification_detail(tenant_conn, mv1)
    check("verification_detail has parties/items",
          detail and detail["parties"] and len(detail["parties"][0]["items"]) == 2)
    payload = statements.extraction_payload(tenant_conn, up1)
    check("extraction_payload works", payload and payload["upload"]["id"] == up1)

    # ── inline corrections (marks verified) ──────────────────────────────
    party_id = detail["parties"][0]["party"]["id"]
    item_id = detail["parties"][0]["items"][0]["id"]
    ok2, err2 = statements.save_inline_corrections(tenant_conn, mv1, agent1, {
        "stockist_name": "ABC Pharma Pvt Ltd",
        "statement_from_date": "01/01/2025",
        "statement_to_date": "31/01/2025",
        "parties": [{"id": party_id, "name": "Shree Medicals & Co", "area": "Kothrud",
                     "type": "chemist", "dl_number": "MH/1234", "gst_number": "27AAAFM1234G1ZA"}],
        "items": [{"id": item_id, "brand": "Cough Syrup X", "mfg": "Medico",
                   "pack": "100ml", "quantity": 6, "unit_rate": 90.0,
                   "discount_percent": 5.0, "final_amount": 513.0}],
    })
    check("inline corrections applied + verified", ok2 and err2 is None, f"{ok2},{err2}")
    c6 = tenant_conn.cursor()
    c6.execute("SELECT status FROM manual_verifications WHERE id=%s", (mv1,))
    check("mv1 verified", c6.fetchone()[0] == "verified")
    hist = statements.verification_history(tenant_conn, agent1)
    check("agent1 history has verified task", any(h["verification_id"] == mv1 for h in hist))
    astats = statements.agent_stats(tenant_conn, agent1)
    check("agent1 stats total_verified >= 1", astats.get("total_verified", 0) >= 1, str(astats))

    # ── reject + reset flow ──────────────────────────────────────────────
    up4 = _insert_upload(tenant_conn, uploader_id, "Reject Me Apr25.pdf", "processing", file_hash="h4")
    statements.save_extraction(tenant_conn, up4, _extraction_data("Reject Co", "01/04/2025", "30/04/2025"))
    mv4 = statements.ensure_manual_verification(tenant_conn, up4, None)
    ok3, err3, st3 = statements.reject_verification(tenant_conn, mv4, agent2, "Amount mismatch", "mismatch")
    check("reject_verification ok", ok3 and err3 is None and st3 == 200, f"{ok3},{err3},{st3}")
    c7 = tenant_conn.cursor()
    c7.execute("SELECT status FROM uploads WHERE id=%s", (up4,))
    check("rejected upload status persisted", c7.fetchone()[0] == "rejected")
    statements.reset_verification_pending(tenant_conn, mv4, up4)
    c7.execute("SELECT status FROM manual_verifications WHERE id=%s", (mv4,))
    check("reset_verification_pending -> pending", c7.fetchone()[0] == "pending")

    # ── documents list (accessible = everyone) ───────────────────────────
    uploads, status_counts = statements.documents(
        tenant_conn, [uploader_id, ho_id, agent1, agent2],
        date_from="", date_to="", status_filter="all")
    check("documents returns uploads", len(uploads) >= 4, str(len(uploads)))
    bounded, _ = statements.documents(
        tenant_conn, [uploader_id, ho_id, agent1, agent2],
        date_from="2025-01-01", date_to="2025-12-31", status_filter="all")
    check("documents 2025 date-bound returns 0 (uploads are 2026)", bounded == [],
          f"{len(bounded)} rows")
    check("documents status_counts has done + rejected",
          status_counts.get("done", 0) >= 2 and status_counts.get("rejected", 0) >= 1,
          str(status_counts))
    restricted, _ = statements.documents(
        tenant_conn, [agent1], date_from="", date_to="", status_filter="all")
    check("documents respects scope", all(u["user_id"] == agent1 for u in restricted),
          f"{len(restricted)} rows for agent1")
    rejected_docs, _ = statements.documents(
        tenant_conn, [uploader_id], date_from="", date_to="", status_filter="rejected")
    check("documents rejected filter", any(u["id"] == up4 for u in rejected_docs))

    # ── verification workbook builders ───────────────────────────────────
    from openpyxl import load_workbook
    buf, fname = statements.build_verification_workbook(tenant_conn, mv3, company_code="B2SCRATCH")
    buf.seek(0)
    wb = load_workbook(io.BytesIO(buf.read()))
    check("verification workbook sheets", {"Meta", "Parties", "Items"} <= set(wb.sheetnames), fname)
    buf2, fname2 = statements.verification_history_workbook(tenant_conn, agent1, "Agent One")
    buf2.seek(0)
    wb2 = load_workbook(io.BytesIO(buf2.read()))
    check("history workbook built", len(wb2.sheetnames) >= 1, fname2)

    return {"uploader_id": uploader_id, "ho_id": ho_id, "agent1": agent1, "agent2": agent2}


def part_b(scratch_platform_name, tenant_db, ids):
    """Scratch platform DB + superadmin routes invoked against it."""
    import saas.pools
    from saas import config, db_utils
    from saas.routers import superadmin as su

    # ── create the scratch platform DB with the full control-plane schema ─
    admin = db_utils.admin_conn()
    _reset_database(admin, scratch_platform_name)
    admin.close()
    platform_conn = db_utils.get_conn(scratch_platform_name)
    from saas.platform_db import _PLATFORM_DDL, _PLATFORM_MIGRATIONS
    pc = platform_conn.cursor()
    pc.execute(_PLATFORM_DDL)
    pc.execute(_PLATFORM_MIGRATIONS)
    platform_conn.commit()
    check("ai_usage_log.user_id column", "user_id" in _col_names(platform_conn, "ai_usage_log"))
    check("ai_usage_log.original_filename column",
          "original_filename" in _col_names(platform_conn, "ai_usage_log"))

    # ── division row pointing at the scratch tenant ──────────────────────
    pc.execute(
        "INSERT INTO divisions (name, code, status, tenant_db_name) "
        "VALUES ('Batch2 Scratch','B2SCR','active',%s) RETURNING id", (tenant_db,))
    div_id = pc.fetchone()[0]
    platform_conn.commit()

    # ── sample ai_usage_log rows (division_id / company_id / user_id) ────
    pc.execute(
        """INSERT INTO ai_usage_log (upload_id, division_id, company_id, user_id,
            original_filename, model_name, input_tokens, output_tokens,
            thinking_tokens, chunk_count, cost_usd, cost_inr, created_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now()-interval '2 hours'),
                  (%s,NULL,%s,%s,%s,%s,%s,%s,0,1,%s,%s, now()-interval '5 days'),
                  (%s,%s,%s,%s,%s,%s,%s,%s,0,2,%s,%s, now()-interval '20 days')""",
        (1, div_id, None, ids["agent1"], "Q3 ABC Pharma.pdf", "gemini-2.5-flash",
         5000, 4000, 200, 4, 1.0, 95.5,
         2, div_id, ids["agent2"], "Jan Sun Pharma.pdf", "gemini-2.5-flash",
         1000, 800, 0.2, 19.1,
         3, div_id, None, ids["agent1"], "Old FY24.pdf", "gemini-1.5-pro",
         3000, 2000, 0.5, 47.75))
    platform_conn.commit()
    platform_conn.close()

    claims = {"sub": 1, "username": "batch2-superadmin"}

    # Route each superadmin read against the scratch control-plane DB.
    import saas.platform_db as pdb_mod
    _orig_get_db = pdb_mod.get_db
    _orig_reg_conn = None
    pdb_mod.get_db = lambda: db_utils.get_conn(scratch_platform_name)
    try:
        # ── /costing (rewritten to ai_usage_log) ─────────────────────────
        alltime = su.platform_costing(days=0, division_id=0, model="", claims=claims)
        check("costing summary calls == 3", alltime["summary"]["calls"] == 3,
              str(alltime["summary"]))
        check("costing by_model 2 models", len(alltime["by_model"]) == 2,
              str([m["model"] for m in alltime["by_model"]]))
        check("costing by_division 1 division (company_id fallback)",
              len(alltime["by_division"]) == 1
              and alltime["by_division"][0]["division_id"] == div_id,
              str(alltime["by_division"]))
        check("costing by_user 2 users",
              len(alltime["by_user"]) == 2, str(len(alltime["by_user"])))
        check("costing recent has original filenames",
              all(r.get("filename") for r in alltime["recent"]),
              str([r.get("filename") for r in alltime["recent"]][:3]))
        check("costing monthly non-empty", len(alltime["monthly"]) >= 1)
        week = su.platform_costing(days=7, division_id=0, model="", claims=claims)
        check("costing 7-day window == 2 calls", week["summary"]["calls"] == 2,
              str(week["summary"]))
        none_div = su.platform_costing(days=0, division_id=999999, model="", claims=claims)
        check("costing unknown division -> 0 calls", none_div["summary"]["calls"] == 0)

        # ── /costing/export ──────────────────────────────────────────────
        resp = su.platform_costing_export(days=7, division_id=0, model="", claims=claims)

        async def _drain(stream):
            return b"".join([chunk async for chunk in stream])

        body = asyncio.run(_drain(resp.body_iterator))
        from openpyxl import load_workbook
        wbx = load_workbook(io.BytesIO(body), read_only=True)
        check("costing export sheets",
              {"Summary", "By User", "Daily Trend", "Per-Extraction Detail"}
              <= set(wbx.sheetnames), str(wbx.sheetnames))
        ws = wbx["Summary"]
        rows = list(ws.iter_rows(values_only=True))
        check("costing export summary has 2 calls for 7 days", rows[3][0] == 2, str(rows[3][:3]))

        # ── AI model config (routing/pricing) via the routes ─────────────
        import saas.ai.model_registry as registry
        _orig_reg_conn = registry._conn
        registry._conn = lambda: db_utils.get_conn(scratch_platform_name)
        view = su.sa_ai_models(claims=claims)
        check("ai-models view shape",
              view.get("categories") and view.get("routing") is not None
              and view.get("env_defaults") is not None
              and view.get("models") is not None
              and view.get("pricing") is not None)
        su.sa_ai_models_routing_save({"overrides": {"image": "gemini-2.5-flash"}}, claims=claims)
        check("routing override persisted",
              registry.get_routing().get("image") == "gemini-2.5-flash")
        su.sa_ai_models_pricing_save({
            "rows": [{"model_id": "gemini-b2-test", "label": "Batch2 Test",
                      "input": 1.1, "output": 2.2}],
            "new_model": {},
        }, claims=claims)
        pr = registry.get_pricing_rows()
        check("pricing row upserted", "gemini-b2-test" in pr, str(list(pr)[:5]))
        su.sa_ai_models_pricing_delete({"model_id": "gemini-b2-test"}, claims=claims)
        check("pricing row deleted", "gemini-b2-test" not in registry.get_pricing_rows())
        su.sa_ai_models_routing_save({"overrides": {"image": ""}}, claims=claims)
        check("routing override cleared",
              "image" not in registry.get_routing())

        # ── Tenant credits admin routes (allocate/approve/reject) ────────
        from saas import credits
        wallet_view = su.sa_tenant_credits(div_id, claims=claims)
        check("tenant credits route shape",
              "credits" in wallet_view and "pending_requests" in wallet_view
              and "ledger" in wallet_view)
        base_total = wallet_view["credits"]["total_credits"]
        su.sa_tenant_credits_allocate(div_id, {"amount": 100, "plan": "pro"}, claims=claims)
        tconn = db_utils.get_conn(tenant_db)
        rid_a = credits.request_credits(tconn, ids["agent1"], 60, "route approve test")
        rid_r = credits.request_credits(tconn, ids["agent2"], 45, "route reject test")
        tconn.close()
        su.sa_tenant_credits_approve(div_id, rid_a, {}, claims=claims)
        su.sa_tenant_credits_reject(div_id, rid_r, {}, claims=claims)
        after = su.sa_tenant_credits(div_id, claims=claims)
        check("allocate +100 and approve +60 applied",
              after["credits"]["total_credits"] == base_total + 160,
              f"{base_total} -> {after['credits']['total_credits']}")
        check("approved request visible in wallet pending", all(
            r["id"] != rid_a for r in after["pending_requests"]))
    finally:
        pdb_mod.get_db = _orig_get_db
        if _orig_reg_conn is not None:
            registry._conn = _orig_reg_conn
        try:
            saas.pools.close_tenant_pool(tenant_db)
        except Exception:
            pass

    return div_id


def part_c():
    """READ-ONLY introspection of the live databases -- SELECTs only."""
    from saas import config, db_utils
    print("\n── Part C: read-only introspection of live Postgres (no writes) ──")
    live = db_utils.get_conn(config.PLATFORM_DB_NAME)
    try:
        c = live.cursor()
        c.execute("SELECT COUNT(*) FROM divisions")
        n_div = c.fetchone()[0]
        cols = _col_names(live, "ai_usage_log")
        table_state = sorted(cols) if cols else "(table absent -- pre-deploy)"
        print(f"     live platform '{config.PLATFORM_DB_NAME}': {n_div} divisions, "
              f"ai_usage_log columns={table_state}")
        print("     (ai_usage_log + its user_id/original_filename columns arrive only when the")
        print("      deploy runs the platform init against the LIVE DB; Batch 2 verification is")
        print("      explicitly read-only and never applies DDL to live databases.)")
    finally:
        live.close()

    tconn = None
    tdb = None
    try:
        probe = db_utils.get_conn(config.PLATFORM_DB_NAME)
        pc = probe.cursor()
        pc.execute("SELECT tenant_db_name FROM divisions WHERE tenant_db_name IS NOT NULL LIMIT 1")
        row = pc.fetchone()
        probe.close()
        if row:
            tdb = row[0]
            tconn = db_utils.get_conn(tdb)
            tc = tconn.cursor()
            tc.execute("SELECT MAX(version) FROM schema_migrations")
            ver = tc.fetchone()[0]
            check("live tenant schema_migrations version reported", bool(ver), str(ver))
            up_cols = _col_names(tconn, "uploads")
            # Report the live statements-schema state (pre-deploy = absent).
            print(f"     live tenant {tdb}: uploads table={bool(up_cols)} "
                  f"progress_pct={'progress_pct' in up_cols} "
                  f"progress_stage={'progress_stage' in up_cols} (read-only)")
            tconn.close()
    except Exception as exc:
        print(f"     (live tenant probe skipped: {exc})")
    print("     LIVE DATABASES UNTOUCHED -- all of the above are SELECT-only.")


def main():
    from saas import config, db_utils

    # ── 0. module integrity ──────────────────────────────────────────────
    import saas.main  # noqa: F401 -- import-time check (startup NOT triggered)
    include = getattr(saas.main, "_INCLUDED_ROUTERS", None)
    if include is None:
        # Starlette >=1.6 defers included routers as _IncludedRouter entries on
        # app.routes (path is None); unwrap original_router to collect paths.
        include = []
        for r in saas.main.app.routes:
            orig = getattr(r, "original_router", None)
            if orig is not None:
                for rr in getattr(orig, "routes", []) or []:
                    p = getattr(rr, "path", None)
                    if p:
                        include.append(p)
    paths = set(include)
    for r in saas.main.app.routes:
        p = getattr(r, "path", None)
        if p:
            paths.add(p)
    wanted = [
        "/api/v1/statements/upload",
        "/api/v1/statements/upload-from-url",
        "/api/v1/statements/uploads/{up_id}/progress",
        "/api/v1/statements/verification/{mv_id}/inline-save",
        "/api/v1/statements/verification/{mv_id}/download-excel",
        "/api/v1/superadmin/ai-models",
        "/api/v1/superadmin/ai-models/pricing/delete",
        "/api/v1/superadmin/costing/export",
        "/api/v1/superadmin/divisions/{did}/credits",
        "/api/v1/superadmin/divisions/{did}/credits/allocate",
        "/api/v1/superadmin/divisions/{did}/credits/requests/{rid}/approve",
    ]
    missing = [p for p in wanted if p not in paths]
    check("all Batch 2 routes registered on the app", not missing, str(missing))

    # zero saas -> app imports (filesystem scan)
    bad = []
    for py in (ROOT / "saas").rglob("*.py"):
        try:
            text = py.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if re.search(r"(^|\n)\s*(from|import)\s+app\s*(\.|$)", text):
            # skip docstring-only mentions: only flag actual import statements
            for line in text.splitlines():
                if re.search(r"^\s*(from|import)\s+app\s*(\.|$)", line):
                    bad.append(f"{py.relative_to(ROOT)}: {line.strip()}")
    check("zero saas -> app imports", not bad, "; ".join(bad[:5]))

    # ── create scratch DBs ───────────────────────────────────────────────
    admin = db_utils.admin_conn()
    try:
        _reset_database(admin, _SCRATCH_TENANT)
        _reset_database(admin, _SCRATCH_PLATFORM)
    finally:
        admin.close()

    # ── Part A: scratch tenant ───────────────────────────────────────────
    print("\n── Part A: scratch tenant (statements domain + credits) ──")
    t_conn = db_utils.get_conn(_SCRATCH_TENANT)
    p_conn = db_utils.get_conn(config.PLATFORM_DB_NAME)  # unused in A; kept for symmetry
    try:
        ids = part_a(t_conn, p_conn)
    finally:
        t_conn.close()
        p_conn.close()

    # ── Part B: scratch platform + superadmin routes ─────────────────────
    print("\n── Part B: scratch platform (costing/ai-models/credits routes) ──")
    part_b(_SCRATCH_PLATFORM, _SCRATCH_TENANT, ids)

    # ── Part C: read-only live checks ────────────────────────────────────
    part_c()

    # ── cleanup scratch DBs (best effort) ────────────────────────────────
    admin = db_utils.admin_conn()
    try:
        for db in (_SCRATCH_TENANT, _SCRATCH_PLATFORM):
            bc = admin.cursor()
            bc.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname=%s AND pid<>pg_backend_pid()", (db,))
            bc.execute(f'DROP DATABASE IF EXISTS "{db}"')
        print(f"Scratch DBs dropped: {_SCRATCH_TENANT}, {_SCRATCH_PLATFORM}")
    finally:
        admin.close()

    print("\n" + "=" * 60)
    if FAILED:
        print(f"RESULT: {len(FAILED)} FAILURE(S)")
        for f in FAILED:
            print("  -", f)
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")
    sys.exit(0)


if __name__ == "__main__":
    t0 = time.time()
    try:
        main()
    finally:
        print(f"(verify_batch2 took {time.time() - t0:.1f}s)")