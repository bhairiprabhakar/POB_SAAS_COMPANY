"""Validate /api/v1/analytics/roi math with seeded data in a HERMETIC tenant.

A scratch control-plane DB + one provisioned division are created before the
saas package is imported, two campaigns with verified/rejected POBs +
gratifications are seeded, the roi endpoint is hit as the division admin, the
input->output->result numbers are asserted, and both scratch DBs are dropped
at the end (the live platform is never touched).

Ported in Batch 4 from the companies/company_code model to the current
divisions/division_slug architecture. The seeded MR user carries the tenant's
division_id so the division-scoped admin's visible set includes their rows.

Run:  venv\\Scripts\\python.exe scripts\\test_roi_analytics.py
"""
import os
import sys
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# hermetic: point the platform at a scratch control-plane DB BEFORE the saas
# package is imported (config reads this at import time)
SCRATCH_PLATFORM = f"psk_roi_{uuid.uuid4().hex[:10]}"
os.environ["PLATFORM_DB_NAME"] = SCRATCH_PLATFORM

from fastapi.testclient import TestClient  # noqa: E402
from saas.passwords import hash_pw  # noqa: E402
from saas import platform_db  # noqa: E402
from saas.db_utils import get_conn  # noqa: E402
from saas.main import app  # noqa: E402

platform_db.init_platform_db()

BOOT = os.environ.get("SUPERADMIN_BOOTSTRAP_PASSWORD", "101990")
ADMIN = ("bi_admin", "Admin@123")
FAILED = []
_TENANT_DB = None


def check(name, cond, extra=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" -- {extra}" if extra and not cond else ""))
    if not cond:
        FAILED.append(name)


def main():
    global _TENANT_DB
    tag = uuid.uuid4().hex[:6]
    with TestClient(app) as client:
        # ── 0. hermetic provisioning ────────────────────────────────────────
        r = client.post("/api/v1/auth/superadmin/login",
                        json={"username": "superadmin", "password": BOOT})
        check("superadmin login", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        SA = {"Authorization": f"Bearer {r.json()['access_token']}"}

        r = client.post("/api/v1/superadmin/divisions", headers=SA, json={
            "name": f"ROI Test Div {tag}", "code": f"ROI{tag.upper()}",
            "contact_person": "Test", "contact_email": f"roi{tag}@test.in",
            "contact_mobile": "9800000000",
            "provision": True, "admin_username": "bi_admin",
            "admin_password": "Admin@123", "admin_email": f"roi{tag}@admin.in",
        })
        check("create+provision division", r.status_code == 200, f"{r.status_code} {r.text[:300]}")
        div = r.json()
        tenant_db = div["tenant_db_name"]
        _TENANT_DB = tenant_db
        slug = div["code"]

        # clear the admin's onboarding flags so login proceeds straight through
        conn = get_conn(tenant_db)
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM divisions ORDER BY id LIMIT 1")
            tenant_division_id = cur.fetchone()[0]
            cur.execute("SELECT id FROM users WHERE username='bi_admin'")
            admin_id = cur.fetchone()[0]
            cur.execute("UPDATE users SET must_change_password=FALSE, "
                        "mfa_setup_required=FALSE, profile_pending=FALSE WHERE id=%s",
                        (admin_id,))
            conn.commit()
        finally:
            conn.close()

        t = get_conn(tenant_db)
        cur = t.cursor()

        created = {"pobs": [], "grat": [], "product": None,
                   "campaigns": [], "chemist": None, "user": None}

        def q(sql, params=()):
            if params:
                cur.execute(sql, params)
            else:
                cur.execute(sql)

        def sq(sql, params=()):
            cur.execute(sql, params)
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

            q("SELECT id FROM roles WHERE name='mr'")
            mr_role = cur.fetchone()[0]
            q("""INSERT INTO users (username, password, full_name, role_id, email, mobile, status,
                 division_id)
                 VALUES (%s,%s,%s,%s,%s,%s,'active',%s) RETURNING id""",
              (f"roi_mr_{tag}", hash_pw("Test@123"), "ROI Test MR", mr_role,
               "roimr@test.in", "9822222222", tenant_division_id))
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

            q("""INSERT INTO products (campaign_id, sku, name, strength, pack, ptr, mrp, status)
                 VALUES (%s,%s,%s,%s,%s,%s,%s,'active') RETURNING id""",
              (camp_a, f"ROI-{tag}", "ROI Test Product", "10 mg", "10x10", 100.0, 120.0))
            created["product"] = cur.fetchone()[0]
            q("""INSERT INTO campaign_products (campaign_id, product_id, sort_order,
                 min_quantity, min_pob, max_pob, scheme_eligibility)
                 VALUES (%s,%s,0,1,100.0,100000.0,TRUE)""",
              (camp_a, created["product"]))

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
            r = client.post("/api/v1/auth/login",
                            json={"division_slug": slug, "username": ADMIN[0], "password": ADMIN[1]})
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
            # remove seed rows regardless of outcome
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
    try:
        main()
    finally:
        # hermetic: drop the scratch tenant + scratch platform DBs
        if _TENANT_DB:
            import psycopg2
            from saas import config
            conn = psycopg2.connect(
                host=config.DB_HOST, port=config.DB_PORT,
                user=config.DB_USER, password=config.DB_PASSWORD, dbname="postgres")
            conn.autocommit = True
            try:
                cur = conn.cursor()
                for name in (_TENANT_DB, SCRATCH_PLATFORM):
                    cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,))
                    if not cur.fetchone():
                        continue
                    cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                                "WHERE datname=%s AND pid<>pg_backend_pid()", (name,))
                    cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
            finally:
                conn.close()