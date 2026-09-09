"""Validate /api/v1/analytics/roi math with seeded data in an existing tenant.

Seeds two campaigns with verified/rejected POBs + gratifications, hits the
roi endpoint as the tenant admin, asserts input->output->result numbers, then
removes the seed rows so the tenant is left clean.

Run:  python scripts/test_roi_analytics.py
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from app.security import hash_pw
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
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="leave seed rows in place (for UI smoke checks)")
    args = ap.parse_args()

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
    created = {"pobs": [], "grat": [], "product": None, "campaigns": [], "chemist": None, "user": None}

    def q(sql, params=()):
        if params:
            cur.execute(sql, params)
        else:
            cur.execute(sql)

    def sq(sql, params=()):
        if params:
            cur.execute(sql, params)
        else:
            cur.execute(sql)
        return cur.fetchall()

    try:
        # defensive: clear any leftover ROI seed rows from earlier runs
        for tbl, col in [("gratifications", "campaign_id"), ("pob_activities", "campaign_id")]:
            q(f"DELETE FROM {tbl} WHERE {col} IN "
              "(SELECT id FROM campaigns WHERE name LIKE 'ROI Test %')")
        q("DELETE FROM products WHERE sku LIKE 'ROI-%'")
        q("DELETE FROM chemists WHERE name LIKE 'ROI Test Chemist %'")
        q("DELETE FROM campaigns WHERE name LIKE 'ROI Test %'")
        q("DELETE FROM users WHERE username LIKE 'roi_mr_%'")
        t.commit()

        q("SELECT id FROM users WHERE username=%s", (ADMIN[0],))
        admin_id = cur.fetchone()[0]
        q("SELECT id FROM roles WHERE name='mr'")
        mr_role = cur.fetchone()[0]
        q("SELECT id FROM users WHERE username='roi_mr_demo'")
        existing = cur.fetchone()
        if existing:
            mr_id = existing[0]
        else:
            q("""INSERT INTO users (username, password, full_name, role_id, email, mobile, status)
                 VALUES (%s,%s,%s,%s,%s,%s,'active') RETURNING id""",
              (f"roi_mr_{tag}", hash_pw("Test@123"), "ROI Test MR", mr_role, "roimr@test.in", "9822222222"))
            mr_id = cur.fetchone()[0]
            created["user"] = mr_id

        q("""INSERT INTO campaigns (name, division, start_date, end_date, active,
             status, scheme_type, invoice_verification_required)
             VALUES (%s,%s,current_date,current_date+30,TRUE,'active','cashback',TRUE) RETURNING id""",
          (f"ROI Test A {tag}", "Test Div"))
        camp_a = cur.fetchone()[0]
        created["campaigns"].append(camp_a)

        q("""INSERT INTO campaigns (name, division, start_date, end_date, active,
             status, scheme_type, invoice_verification_required)
             VALUES (%s,%s,current_date,current_date+30,TRUE,'active','voucher',TRUE) RETURNING id""",
          (f"ROI Test B {tag}", "Test Div"))
        camp_b = cur.fetchone()[0]
        created["campaigns"].append(camp_b)

        q("""INSERT INTO products (campaign_id, sku, name, strength, pack, ptr, mrp, min_quantity,
             min_pob, max_pob, scheme_eligibility, status)
             VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,'active') RETURNING id""",
          (camp_a, f"ROI-{tag}", "ROI Test Product", "10 mg", "10x10", 100.0, 120.0, 1, 100.0, 100000.0))
        created["product"] = cur.fetchone()[0]

        q("""INSERT INTO chemists (name, shop_name, gst, mobile, city, state, status)
             VALUES (%s,%s,%s,%s,%s,%s,'active') RETURNING id""",
          (f"ROI Test Chemist {tag}", "ROI Shop", f"27ROI{tag}1Z", "9811100000", "Mumbai", "MH"))
        created["chemist"] = cur.fetchone()[0]

        # A: admin verified 1000 + mr verified 2000 + admin rejected 500; B: mr verified 5000
        pob_spec = [
            (admin_id, camp_a, 1000, "verified", 10),
            (mr_id, camp_a, 2000, "verified", 20),
            (admin_id, camp_a, 500, "rejected", 5),
            (mr_id, camp_b, 5000, "verified", 50),
        ]
        for uid, camp, amount, status, qty in pob_spec:
            q("""INSERT INTO pob_activities (user_id, campaign_id, product_id, chemist_id, quantity,
                 ptr, mrp, invoice_amount, pob_amount, invoice_number, invoice_date, status)
                 VALUES (%s,%s,%s,%s,%s,100,120,%s,%s,%s,%s,%s) RETURNING id""",
              (uid, camp, created["product"], created["chemist"], qty, amount, amount,
               f"ROIINV{tag}{uid}", "2026-08-01", status))
            created["pobs"].append(cur.fetchone()[0])

        # A/1000->paid (100), A/2000->eligible (200), B/5000->paid (2500)
        g_spec = [(created["pobs"][0], admin_id, camp_a, 100, "paid"),
                  (created["pobs"][1], mr_id, camp_a, 200, "eligible"),
                  (created["pobs"][3], mr_id, camp_b, 2500, "paid")]
        for pob_id, uid, camp, value, status in g_spec:
            q("""INSERT INTO gratifications (pob_id, user_id, campaign_id, type_code, scheme_value, status)
                 VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
              (pob_id, uid, camp, "cashback", value, status))
            created["grat"].append(cur.fetchone()[0])
        t.commit()

        # ---- endpoint checks ----------------------------------------------
        with TestClient(app) as client:
            r = client.post("/api/v1/auth/login",
                            json={"company_code": COMPANY_CODE, "username": ADMIN[0], "password": ADMIN[1]})
            check("admin login", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
            h = {"Authorization": f"Bearer {r.json()['access_token']}"}

            r = client.get("/api/v1/analytics/roi?days=0", headers=h)
            check("roi endpoint 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
            d = r.json()
            t_ = d["totals"]

            # expected: verified 1000+2000+5000=8000, rejected 500, paid 100+2500=2600
            check("totals verified_value", t_["verified_value"] == 8000,
                  f"got {t_['verified_value']}")
            check("totals rewards_paid", t_["rewards_paid"] == 2600,
                  f"got {t_['rewards_paid']}")
            check("totals rewards_eligible", t_["rewards_eligible"] == 2800,
                  f"got {t_['rewards_eligible']}")
            check("totals roi (8000/2600)", abs(t_["roi"] - 8000 / 2600) < 0.01,
                  f"got {t_['roi']}")
            check("totals roi_basis paid", t_["roi_basis"] == "paid", f"got {t_['roi_basis']}")
            check("totals net (8000-2600)", t_["net"] == 5400, f"got {t_['net']}")

            camps = {c["id"]: c for c in d["campaigns"]}
            ca, cb = camps.get(camp_a), camps.get(camp_b)
            check("campaign A present", ca is not None)
            check("campaign A verified_value 3000", ca and ca["verified_value"] == 3000,
                  f"got {ca and ca['verified_value']}")
            check("campaign A rewards_paid 100", ca and ca["rewards_paid"] == 100,
                  f"got {ca and ca['rewards_paid']}")
            check("campaign A rewards_eligible 300", ca and ca["rewards_eligible"] == 300,
                  f"got {ca and ca['rewards_eligible']}")
            check("campaign A roi 30.0", ca and ca["roi"] == 30.0,
                  f"got {ca and ca['roi']}")
            check("campaign A net 2900", ca and ca["net"] == 2900,
                  f"got {ca and ca['net']}")
            check("campaign A rejected 1", ca and ca["rejected"] == 1,
                  f"got {ca and ca['rejected']}")
            check("campaign A approval 66.67", ca and abs(ca["approval_rate"] - 66.67) < 0.01,
                  f"got {ca and ca['approval_rate']}")
            check("campaign B roi 2.0", cb and cb["roi"] == 2.0,
                  f"got {cb and cb['roi']}")
            check("campaign B approval 100%", cb and cb["approval_rate"] == 100.0,
                  f"got {cb and cb['approval_rate']}")

            m = {x["id"]: x for x in d["members"]}
            ma = m.get(admin_id)
            check("admin member verified_value 1000", ma and ma["verified_value"] == 1000,
                  f"got {ma and ma['verified_value']}")
            check("admin member roi 10.0", ma and ma["roi"] == 10.0,
                  f"got {ma and ma['roi']}")
            mm = m.get(mr_id)
            check("mr member verified_value 7000", mm and mm["verified_value"] == 7000,
                  f"got {mm and mm['verified_value']}")
            check("mr member rewards_paid 2500", mm and mm["rewards_paid"] == 2500,
                  f"got {mm and mm['rewards_paid']}")

            check("chemists present", any(x["id"] == created["chemist"] for x in d["chemists"]))
            check("products present", any(x["id"] == created["product"] for x in d["products"]))
            check("divisions has Test Div", any(x["division"] == "Test Div" for x in d["divisions"]))

    finally:
        if not args.keep:
            for gid in created["grat"]:
                q("DELETE FROM gratifications WHERE id=%s", (gid,))
            for pid in created["pobs"]:
                q("DELETE FROM pob_activities WHERE id=%s", (pid,))
            if created["product"]:
                q("DELETE FROM products WHERE id=%s", (created["product"],))
            if created["chemist"]:
                q("DELETE FROM chemists WHERE id=%s", (created["chemist"],))
            for cid_ in created["campaigns"]:
                q("DELETE FROM campaigns WHERE id=%s", (cid_,))
            if created["user"]:
                q("DELETE FROM users WHERE id=%s", (created["user"],))
            t.commit()
        t.close()

    print("\n" + ("ALL PASS" if not FAILED else f"{len(FAILED)} FAILED: {FAILED}"))
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
