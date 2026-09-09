"""Verify the OCR usage ledger + cost statement:

  - every extraction records a row in ocr_usage (user, engine, cost, status).
  - token-based cost is charged for Gemini (usage_metadata), text parse is free.
  - a request that is rejected AFTER extraction still keeps its usage row
    (the row is committed immediately, the surrounding transaction rolls back).
  - the cost statement is super-admin-only (no tenant-facing statement).
  - super admin sees company-wise + user-wise cost statement + Excel export.

Run:  python scripts/test_ocr_usage.py
"""
import io
import os
import sys
import uuid
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from saas import config, ocr as ocr_mod, platform_db
from saas.db_utils import get_conn
from saas.main import app

COMPANY_CODE = os.environ.get("POB_COMPANY", "BITE3878")
ADMIN = ("bi_admin", "Admin@123")
SA_PWD = os.environ.get("SUPERADMIN_BOOTSTRAP_PASSWORD", "Pob_Saas@2026")
FAILED = []


def check(name, cond, extra=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" -- {extra}" if extra and not cond else ""))
    if not cond:
        FAILED.append(name)


def main():
    conn = platform_db.get_db()
    c = conn.cursor()
    c.execute("SELECT id, tenant_db_name FROM companies WHERE code=%s", (COMPANY_CODE,))
    row = c.fetchone()
    conn.close()
    if not row or not row[1]:
        print(f"[FAIL] no tenant db for {COMPANY_CODE}")
        sys.exit(1)
    company_id, tenant_db = row

    from saas.migrations import ensure_migrated
    ensure_migrated(tenant_db)

    # ── unit: extraction_cost (token-based) ────────────────────────────────
    from saas.ocr import extraction_cost
    check("cost 0 for text engine", extraction_cost({"engine": "text"}) == 0.0)
    check("cost 0 for no engine", extraction_cost({"engine": "none"}) == 0.0)
    check("cost 0 for gemini without usage", extraction_cost({"engine": "gemini"}) == 0.0)
    expected = round(
        0.2 * config.OCR_COST_INPUT_PER_MTOK + 0.04 * config.OCR_COST_OUTPUT_PER_MTOK, 6)
    check("gemini token cost computed",
          extraction_cost({"engine": "gemini", "usage": {
              "prompt_token_count": 200_000, "candidates_token_count": 40_000,
              "total_token_count": 240_000}}) == expected,
          f"expected {expected}")
    combined = round(240_000 / 1_000_000.0 * config.OCR_COST_INPUT_PER_MTOK, 6)
    check("gemini total-only usage billed as input",
          extraction_cost({"engine": "gemini", "usage": {"total_token_count": 240_000}}) == combined,
          f"expected {combined}")

    t = get_conn(tenant_db)
    cur = t.cursor()
    tag = uuid.uuid4().hex[:6]
    today = date.today()
    created = {"pobs": [], "campaigns": [], "products": [], "chemists": [], "users": [],
               "ocr_ids": [], "invoice_nos": []}

    def q(sql, params=()):
        cur.execute(sql, params) if params else cur.execute(sql)

    def sq(sql, params=()):
        cur.execute(sql, params) if params else cur.execute(sql)
        return cur.fetchall()

    try:
        q("SELECT id FROM users WHERE username=%s", (ADMIN[0],))
        admin_id = cur.fetchone()[0]

        # campaign + product + chemists
        q("""INSERT INTO campaigns (name, division, start_date, end_date, active,
             status, scheme_type, invoice_verification_required, period_type, grace_days)
             VALUES (%s,%s,%s,%s,TRUE,'active','cashback',TRUE,'none',15) RETURNING id""",
          (f"COST CAMP {tag}", "Test Div", today - timedelta(days=10),
           today + timedelta(days=20)))
        campaign_id = cur.fetchone()[0]
        created["campaigns"].append(campaign_id)

        q("""INSERT INTO products (campaign_id, sku, name, strength, pack, ptr, mrp, min_quantity,
             min_pob, max_pob, scheme_eligibility, status)
             VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,'active') RETURNING id""",
          (campaign_id, f"SKU-{tag}", "Cost Test Product", "10 mg", "10x10", 100.0, 120.0,
           1, 100.0, 100000.0))
        product_id = cur.fetchone()[0]
        created["products"].append(product_id)

        chemist_id = None
        q("""INSERT INTO chemists (name, shop_name, gst, mobile, city, state, status)
             VALUES (%s,%s,%s,%s,%s,%s,'active') RETURNING id""",
          (f"COST Chemist {tag}", "CS", f"27CT{tag}1Z", "9821111111", "Mumbai", "MH"))
        chemist_id = cur.fetchone()[0]
        created["chemists"].append(chemist_id)

        q("""INSERT INTO chemists (name, shop_name, gst, mobile, city, state, status)
             VALUES (%s,%s,%s,%s,%s,%s,'active') RETURNING id""",
          (f"COST Direct {tag}", "CD", f"27CT{tag}2Z", "9821111112", "Mumbai", "MH"))
        direct_chemist = cur.fetchone()[0]
        created["chemists"].append(direct_chemist)

        # MR user (no longer needs the statement permission gate: the tenant
        # statement endpoint was removed in favor of the super-admin console)
        t.commit()

        def png_bytes(seed=0):
            from PIL import Image
            buf = io.BytesIO()
            Image.new("RGB", (8, 8), (180 + seed, 140, 90)).save(buf, format="PNG")
            return buf.getvalue()

        def fake_extract(data, filename=""):
            return {
                "fields": {
                    "invoice_number": f"COST-{tag}", "invoice_date": today.isoformat(),
                    "invoice_amount": "1000",
                    "items": [{"description": "Cost Test Product", "qty": 10, "amount": 1000}],
                },
                "confidence": 0.95, "engine": "gemini", "raw": {},
                "usage": {"prompt_token_count": 200_000, "candidates_token_count": 40_000,
                          "total_token_count": 240_000},
            }

        real_key = config.GOOGLE_API_KEY
        real_extract = ocr_mod.extract_fields
        config.GOOGLE_API_KEY = "test-key"  # let the submit flow attempt extraction
        ocr_mod.extract_fields = fake_extract

        with TestClient(app) as client:
            r = client.post("/api/v1/auth/login",
                            json={"company_code": COMPANY_CODE, "username": ADMIN[0],
                                  "password": ADMIN[1]})
            check("admin login", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
            h = {"Authorization": f"Bearer {r.json()['access_token']}"}

            def submit(cid, pid, chem, invoice_no, invoice_date, seed):
                return client.post("/api/v1/pob/submit", headers=h, data={
                    "campaign_id": cid, "product_id": pid, "chemist_id": chem,
                    "quantity": 10, "ptr": 100, "mrp": 120,
                    "invoice_amount": 1000, "pob_amount": 1000,
                    "invoice_number": invoice_no, "invoice_date": invoice_date,
                    "remarks": "ocr-usage-test",
                }, files={"file": (f"pob-{seed}-{tag}.png", png_bytes(seed), "image/png")})

            # A. accepted submit -> gemini usage row with token cost
            r = submit(campaign_id, product_id, chemist_id, f"COST-{tag}", today.isoformat(), 1)
            check("submit with invoice 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
            if r.status_code == 200:
                created["pobs"].append(r.json()["pob_id"])
                created["invoice_nos"].append(f"COST-{tag}")
            rows = sq("""SELECT engine, cost, status, user_id, invoice_number FROM ocr_usage
                         WHERE invoice_number=%s""", (f"COST-{tag}",))
            check("gemini usage recorded for submit", any(r_[0] == "gemini" for r_ in rows),
                  f"{rows}")
            g = [r_ for r_ in rows if r_[0] == "gemini"]
            check("gemini cost > 0 (token-based)", bool(g) and g[0][1] > 0, f"{rows}")
            check("gemini usage attributed to admin", bool(g) and g[0][3] == admin_id, f"{rows}")
            check("gemini usage status success", bool(g) and g[0][2] == "success", f"{rows}")

            # B. rejected AFTER extraction (out-of-period) -> usage still recorded
            r = submit(campaign_id, product_id, chemist_id, f"COST-B-{tag}",
                       (today + timedelta(days=400)).isoformat(), 2)
            check("out-of-period submit rejected 400", r.status_code == 400,
                  f"{r.status_code} {r.text[:200]}")
            rows = sq("""SELECT count(*) FROM ocr_usage WHERE invoice_number=%s""", (f"COST-B-{tag}",))
            check("usage recorded even though request rejected", rows[0][0] == 1, f"{rows}")

            # C. direct invoice-proof (auto-creates lines from OCR)
            r = client.post("/api/v1/pob/invoice-proof", headers=h, data={
                "campaign_id": campaign_id, "chemist_id": direct_chemist, "direct": 1,
            }, files={"file": (f"dir-{tag}.png", png_bytes(seed=7), "image/png")})
            check("direct invoice-proof 200", r.status_code == 200, f"{r.status_code} {r.text[:300]}")
            if r.status_code == 200:
                res = r.json().get("results") or []
                check("direct proof created POB line", len(res) >= 1, f"{r.text[:300]}")
                for x in res:
                    created["pobs"].append(x["pob_id"])
            check("gemini usage recorded for direct proof",
                  sq("SELECT count(*) FROM ocr_usage WHERE engine='gemini'")[0][0] >= 3,
                  f"{sq('SELECT count(*) FROM ocr_usage WHERE engine=%s', ('gemini',))}")

            # D. text-mode engine -> free usage row (upload validation only
            #    accepts gif/jpeg/pdf/png/webp, so patch a text-engine result)
            ocr_mod.extract_fields = lambda data, filename="": {
                "fields": {"invoice_number": f"TXT-{tag}",
                           "invoice_date": today.isoformat(), "invoice_amount": "1500"},
                "confidence": 0.98, "engine": "text", "raw": {"text": ""},
            }
            r = client.post("/api/v1/pob/submit", headers=h, data={
                "campaign_id": campaign_id, "product_id": product_id, "chemist_id": chemist_id,
                "quantity": 10, "ptr": 100, "mrp": 120,
                "invoice_amount": 1500, "pob_amount": 1500,
                "invoice_number": f"TXT-{tag}", "invoice_date": today.isoformat(),
                "remarks": "ocr-usage-text",
            }, files={"file": (f"txt-{tag}.png", png_bytes(seed=3), "image/png")})
            check("text submit 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
            if r.status_code == 200:
                created["pobs"].append(r.json()["pob_id"])
                created["invoice_nos"].append(f"TXT-{tag}")
            rows = sq("""SELECT engine, cost FROM ocr_usage WHERE invoice_number=%s""",
                      (f"TXT-{tag}",))
            check("text usage recorded with cost 0",
                  len(rows) == 1 and rows[0][0] == "text" and rows[0][1] == 0, f"{rows}")
            ocr_mod.extract_fields = real_extract

            # E. super admin: company-wise + user-wise + export
            r = client.post("/api/v1/auth/superadmin/login",
                            json={"username": "superadmin", "password": SA_PWD})
            check("superadmin login", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
            if r.status_code == 200:
                sh = {"Authorization": f"Bearer {r.json()['access_token']}"}
                r = client.get("/api/v1/superadmin/ocr-usage/companies", headers=sh)
                check("superadmin companies statement 200", r.status_code == 200,
                      f"{r.status_code} {r.text[:200]}")
                if r.status_code == 200:
                    mine = [x for x in r.json()["items"] if x["company_id"] == company_id]
                    check("company row present", len(mine) == 1, f"{mine}")
                    if mine:
                        check("company extractions counted", mine[0]["extractions"] >= 3, f"{mine[0]}")
                        check("company gemini counted", mine[0]["gemini"] >= 3, f"{mine[0]}")
                        check("company cost > 0", mine[0]["cost"] and mine[0]["cost"] > 0, f"{mine[0]}")
                        check("company currency set", bool(mine[0]["currency"]), f"{mine[0]}")
                r = client.get(f"/api/v1/superadmin/ocr-usage/companies/{company_id}", headers=sh)
                check("superadmin company deep dive 200", r.status_code == 200,
                      f"{r.status_code} {r.text[:200]}")
                if r.status_code == 200:
                    dd = r.json()
                    check("deep dive summary cost > 0", (dd.get("summary") or {}).get("cost", 0) > 0,
                          f"{dd.get('summary')}")
                    check("deep dive users include admin",
                          any((u.get("full_name") or u.get("username")) for u in (dd.get("users") or [])
                              if u.get("count", 0) > 0),
                          f"{dd.get('users')}")
                    check("deep dive items carry cost", bool(dd.get("items")) and "cost" in dd["items"][0],
                          f"{dd.get('items')[:1]}")
                r = client.get("/api/v1/superadmin/ocr-usage/export", headers=sh)
                check("superadmin export xlsx", r.status_code == 200
                      and "spreadsheetml" in (r.headers.get("content-type") or ""),
                      f"{r.status_code} {r.headers.get('content-type')}")

        # store usage ids for cleanup
        created["ocr_ids"] = [r[0] for r in sq("SELECT id FROM ocr_usage WHERE filename LIKE %s",
                                               (f"%{tag}%",))]

    finally:
        config.GOOGLE_API_KEY = real_key
        ocr_mod.extract_fields = real_extract
        # cleanup (reverse FK order)
        if created["ocr_ids"]:
            q("DELETE FROM ocr_usage WHERE id = ANY(%s)", (created["ocr_ids"],))
        if created["pobs"]:
            q("DELETE FROM pob_activities WHERE id = ANY(%s)", (created["pobs"],))
        if created["users"]:
            q("DELETE FROM users WHERE id = ANY(%s)", (created["users"],))
        for cid_ in created["products"]:
            q("DELETE FROM products WHERE id=%s", (cid_,))
        for cid_ in created["campaigns"]:
            q("DELETE FROM campaigns WHERE id=%s", (cid_,))
        for cid_ in created["chemists"]:
            q("DELETE FROM chemists WHERE id=%s", (cid_,))
        t.commit()
        t.close()

    print("\n" + ("ALL PASS" if not FAILED else f"{len(FAILED)} FAILED: {FAILED}"))
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
