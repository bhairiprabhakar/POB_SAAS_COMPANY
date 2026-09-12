"""Verify invoice-proof feature: period/grace rejection, duplicate invoice
rejection, and pob_invoice_value on the company dashboard.

Run:  python scripts/test_invoice_proof.py
"""
import os
import sys
import uuid
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from saas import platform_db
from saas.db_utils import get_conn
from saas.main import app

COMPANY_CODE = os.environ.get("POB_COMPANY", "BITE3878")
ADMIN = ("bi_admin", "Admin@123")
FAILED = []


def check(name, cond, extra=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" -- {extra}" if extra and not cond else ""))
    if not cond:
        FAILED.append(name)


def main():
    conn = platform_db.get_db()
    c = conn.cursor()
    c.execute("SELECT tenant_db_name FROM companies WHERE code=%s", (COMPANY_CODE,))
    row = c.fetchone()
    conn.close()
    if not row or not row[0]:
        print(f"[FAIL] no tenant db for {COMPANY_CODE}")
        sys.exit(1)
    tenant_db = row[0]

    t = get_conn(tenant_db)
    cur = t.cursor()
    tag = uuid.uuid4().hex[:6]
    created = {"pobs": [], "product": None, "campaigns": [], "chemist": None}

    def q(sql, params=()):
        cur.execute(sql, params) if params else cur.execute(sql)

    def sq(sql, params=()):
        cur.execute(sql, params) if params else cur.execute(sql)
        return cur.fetchall()

    today = date.today()

    try:
        # defensive cleanup from earlier runs
        q("DELETE FROM pob_activities WHERE campaign_id IN "
          "(SELECT id FROM campaigns WHERE name LIKE 'INVPROOF %')")
        q("DELETE FROM campaign_products WHERE campaign_id IN "
          "(SELECT id FROM campaigns WHERE name LIKE 'INVPROOF %')")
        q("DELETE FROM products WHERE campaign_id IN "
          "(SELECT id FROM campaigns WHERE name LIKE 'INVPROOF %')")
        q("DELETE FROM gratifications WHERE campaign_id IN "
          "(SELECT id FROM campaigns WHERE name LIKE 'INVPROOF %')")
        q("DELETE FROM campaigns WHERE name LIKE 'INVPROOF %'")
        q("DELETE FROM chemists WHERE name LIKE 'INVPROOF Chemist %'")
        t.commit()

        q("SELECT id FROM users WHERE username=%s", (ADMIN[0],))
        admin_id = cur.fetchone()[0]

        # Campaign: window is in the past, grace 15 days. Today is beyond the
        # grace window -> an invoice dated today must be rejected on period.
        past_end = today - timedelta(days=30)
        past_start = today - timedelta(days=60)
        q("""INSERT INTO campaigns (name, division, start_date, end_date, active,
             status, scheme_type, invoice_verification_required, period_type, grace_days)
             VALUES (%s,%s,%s,%s,TRUE,'active','cashback',TRUE,%s,%s) RETURNING id""",
          (f"INVPROOF Old {tag}", "Test Div", past_start, past_end, "monthly", 15))
        old_camp = cur.fetchone()[0]
        created["campaigns"].append(old_camp)

        # Campaign: window covers today -> in-window invoice accepted.
        q("""INSERT INTO campaigns (name, division, start_date, end_date, active,
             status, scheme_type, invoice_verification_required, period_type, grace_days)
             VALUES (%s,%s,%s,%s,TRUE,'active','cashback',TRUE,%s,%s) RETURNING id""",
          (f"INVPROOF Live {tag}", "Test Div",
           today - timedelta(days=10), today + timedelta(days=10), "none", 15))
        live_camp = cur.fetchone()[0]
        created["campaigns"].append(live_camp)

        q("""INSERT INTO products (campaign_id, sku, name, strength, pack, ptr, mrp, min_quantity,
             min_pob, max_pob, scheme_eligibility, status)
             VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,'active') RETURNING id""",
          (live_camp, f"INVP-{tag}", "INVPROOF Product", "10 mg", "10x10", 100.0, 120.0, 1, 100.0, 100000.0))
        created["product"] = cur.fetchone()[0]
        q("INSERT INTO campaign_products (campaign_id, product_id, sort_order) VALUES (%s,%s,0)",
          (live_camp, created["product"]))
        old_product = None
        q("""INSERT INTO products (campaign_id, sku, name, strength, pack, ptr, mrp, min_quantity,
             min_pob, max_pob, scheme_eligibility, status)
             VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,'active') RETURNING id""",
          (old_camp, f"INVP-O-{tag}", "INVPROOF Old Product", "10 mg", "10x10",
           100.0, 120.0, 1, 100.0, 100000.0))
        old_product = cur.fetchone()[0]
        q("INSERT INTO campaign_products (campaign_id, product_id, sort_order) VALUES (%s,%s,0)",
          (old_camp, old_product))

        q("""INSERT INTO chemists (name, shop_name, gst, mobile, city, state, status)
             VALUES (%s,%s,%s,%s,%s,%s,'active') RETURNING id""",
          (f"INVPROOF Chemist {tag}", "INV Shop", f"27INV{tag}1Z", "9833333333", "Mumbai", "MH"))
        created["chemist"] = cur.fetchone()[0]
        t.commit()

        with TestClient(app) as client:
            r = client.post("/api/v1/auth/login",
                            json={"company_code": COMPANY_CODE, "username": ADMIN[0], "password": ADMIN[1]})
            check("admin login", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
            h = {"Authorization": f"Bearer {r.json()['access_token']}"}

            def submit(campaign_id, product_id, invoice_number, invoice_date, amount):
                return client.post("/api/v1/pob/submit", headers=h, data={
                    "campaign_id": campaign_id, "product_id": product_id,
                    "chemist_id": created["chemist"], "quantity": 10, "ptr": 100, "mrp": 120,
                    "invoice_amount": amount, "pob_amount": 1000,
                    "invoice_number": invoice_number, "invoice_date": invoice_date,
                    "remarks": "invproof-test",
                })

            # 1. Out-of-period: old campaign, invoice dated today -> 400
            r = submit(old_camp, old_product, f"OLD-{tag}", today.isoformat(), 1000)
            check("out-of-period invoice rejected 400", r.status_code == 400,
                  f"{r.status_code} {r.text[:200]}")
            check("period error mentions grace", r.status_code == 400 and "grace" in r.json().get("detail", ""),
                  f"{r.json()}")

            # 2. In-window submission succeeds -> 200
            inv_no = f"LIVE-{tag}"
            r = submit(live_camp, created["product"], inv_no, today.isoformat(), 1000)
            check("in-window submission 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
            if r.status_code == 200:
                created["pobs"].append(r.json()["pob_id"])

            # 3. Same invoice number + same date but a DIFFERENT amount no
            #    longer trips the duplicate guard: rejection now requires the
            #    brand lines to match too (number+date+chemist+brands+qty+
            #    amount all equal). Without an uploaded invoice there are no
            #    extracted brand lines to compare, so this is accepted.
            r = submit(live_camp, created["product"], inv_no.lower().replace("-", " "), today.isoformat(), 2000)
            check("same number/date, different amount accepted 200", r.status_code == 200,
                  f"{r.status_code} {r.text[:200]}")
            if r.status_code == 200:
                created["pobs"].append(r.json()["pob_id"])

            # 4. Invoice-number-less submission still allowed (norm empty skips dup check)
            r = submit(live_camp, created["product"], "", today.isoformat(), 500)
            check("blank invoice number still allowed", r.status_code == 200,
                  f"{r.status_code} {r.text[:200]}")
            if r.status_code == 200:
                created["pobs"].append(r.json()["pob_id"])

            # 5. Company dashboard exposes pob_invoice_value; it only counts
            #    verified POBs, so mark the first one verified first.
            if created["pobs"]:
                q("UPDATE pob_activities SET status='verified' WHERE id=%s", (created["pobs"][0],))
                t.commit()
            r = client.get("/api/v1/dashboards/company?days=0", headers=h)
            check("company dashboard 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
            if r.status_code == 200:
                kpi = r.json().get("kpis") or r.json()
                check("pob_invoice_value present", "pob_invoice_value" in kpi,
                      f"keys: {sorted(kpi.keys())}")
                iv = kpi.get("pob_invoice_value") or 0
                check("pob_invoice_value reflects invoice amounts", iv >= 1000,
                      f"got {iv}")

    finally:
        for pid in created["pobs"]:
            q("DELETE FROM pob_activities WHERE id=%s", (pid,))
        if created["product"]:
            q("DELETE FROM products WHERE id=%s", (created["product"],))
        q("DELETE FROM products WHERE sku LIKE 'INVP-O-%'")
        if created["chemist"]:
            q("DELETE FROM chemists WHERE id=%s", (created["chemist"],))
        for cid_ in created["campaigns"]:
            q("DELETE FROM campaigns WHERE id=%s", (cid_,))
        t.commit()
        t.close()

    print("\n" + ("ALL PASS" if not FAILED else f"{len(FAILED)} FAILED: {FAILED}"))
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
