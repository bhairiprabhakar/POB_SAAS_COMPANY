"""
Business-alignment hermetic gate (Batch 5).

Provisions two isolated tenant divisions (never touches production) and asserts
the FINAL division-company model:

  - division-scoped brand/product masters (no NULL/global fallback)
  - Brand REQUIRED on product create, cannot be cleared on update
  - product.manage / brand.manage gates (end users view-only)
  - product + brand delete-protection (409 + deactivate message)
  - verification agents cannot correct invoices / run the pipeline / re-extract
  - UPI QR capture: decode -> save -> confirmed chemist UPI, masked in
    gratification responses for non-payment callers, chemist-UPI fallback on
    cashback approval
  - configurable classification master seeds (FINAL codes) and gratification
    type seeds (incl. e_voucher + reward_points)

Run:  venv/Scripts/python.exe scripts/test_business_alignment.py [--keep]
"""
import os
import sys
import uuid

UUID = uuid.uuid4().hex[:8].upper()
os.environ["PLATFORM_DB_NAME"] = f"POB_ALIGN_TEST_{UUID}"
os.environ["TENANT_DB_PREFIX"] = f"POB_ALIGN_TEST_{UUID}_"
os.environ["SUPERADMIN_BOOTSTRAP_PASSWORD"] = "TestPass123!"
os.environ["ALLOW_INSECURE_DEV_DEFAULTS"] = "true"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from saas import ratelimit
ratelimit.api_limiter.limit = 10 ** 9

from fastapi.testclient import TestClient
from saas.main import app
from saas import platform_db, provision

platform_db.init_platform_db()

BASE = "/api/v1/superadmin"
PASSWD = "TestPass123!"
PASSED, FAILED = [], []
_section = ""


def section(name):
    global _section
    _section = name
    print(f"\n{'=' * 64}\n{name}\n{'=' * 64}")


def check(name, cond, extra=""):
    label = f"{_section} :: {name}"
    if cond:
        PASSED.append(label)
        print(f"  [PASS] {name}")
    else:
        FAILED.append((label, extra))
        print(f"  [FAIL] {name}  {extra}")
    return cond


def ok(r, *codes):
    return r.status_code in (codes or (200,))


def j(r):
    try:
        return r.json()
    except Exception:
        return {"_text": r.text[:500]}


def provision_pool_conn(tenant_db):
    from saas import pools
    return pools.get_tenant_conn(tenant_db)


def finish(keep):
    section("Results")
    print(f"\n  Passed: {len(PASSED)}")
    print(f"  Failed: {len(FAILED)}")
    if FAILED:
        print("\n  Failures:")
        for label, extra in FAILED:
            print(f"    - {label}: {extra}")
    print()

    if not keep:
        print("  Cleaning up test databases...")
        platform_db.PLATFORM_POOL.close_all()
        from saas import config as _cfg
        import psycopg2 as _pg
        admin = _pg.connect(host=_cfg.DB_HOST, port=_cfg.DB_PORT, user=_cfg.DB_USER,
                            password=_cfg.DB_PASSWORD, dbname="postgres")
        admin.autocommit = True
        try:
            c = admin.cursor()
            for prefix in (_cfg.PLATFORM_DB_NAME, _cfg.TENANT_DB_PREFIX):
                c.execute("SELECT datname FROM pg_database WHERE datname LIKE %s",
                          (prefix + "%",) if prefix == _cfg.TENANT_DB_PREFIX else (prefix,))
                for (db,) in c.fetchall():
                    c.execute(f'DROP DATABASE IF EXISTS "{db}"')
            print("  Databases cleaned up.")
        except Exception as e:
            print(f"  Cleanup error (non-fatal): {e}")
        finally:
            admin.close()
    else:
        print(f"  Keeping test databases (prefix: {os.environ['TENANT_DB_PREFIX']})")

    return 1 if FAILED else 0


def main():
    keep = "--keep" in sys.argv
    client = TestClient(app, raise_server_exceptions=False)

    section("1. Provision division 1 (isolated)")
    r = client.post("/api/v1/auth/register", json={
        "company": {"legal_name": "Alignment Test Co", "display_name": "ALC",
                     "code": f"ALC{UUID}"},
        "owner": {"username": "owner01", "password": PASSWD,
                  "full_name": "Test Owner", "email": "owner@test.local"},
    })
    check("register company creates owner", ok(r), f"{r.status_code} {j(r)}")
    owner_token = j(r).get("access_token")
    OA = {"Authorization": f"Bearer {owner_token}"}

    r = client.post(f"{BASE}/divisions", headers=OA, json={
        "name": f"Align Div A {UUID}", "code": f"ADA{UUID}",
        "provision": True, "admin_username": "division_admin",
        "admin_password": PASSWD, "admin_email": "da@test.local",
        "admin_full_name": "Division Administrator",
    })
    check("create + provision division A", ok(r), f"{r.status_code} {j(r)}")
    div1 = j(r)
    div1_id = div1.get("id")
    tenant_db1 = div1.get("tenant_db_name")
    check("division A has a tenant db", bool(tenant_db1), div1)

    conn = provision_pool_conn(tenant_db1)
    cur = conn.cursor()
    cur.execute("UPDATE users SET must_change_password=FALSE, mfa_setup_required=FALSE, "
                "profile_pending=FALSE WHERE username='division_admin'")
    conn.commit()
    conn.close()

    section("2. Tenant login as division admin A")
    r = client.post("/api/v1/auth/login", json={
        "division_slug": f"ADA{UUID}", "username": "division_admin", "password": PASSWD,
    })
    check("division admin login", ok(r), f"{r.status_code} {j(r)}")
    if not ok(r):
        return finish(keep)
    T1 = {"Authorization": f"Bearer {j(r)['access_token']}"}
    me = client.get("/api/v1/auth/me", headers=T1)
    perms = (j(me).get("permissions") or []) if ok(me) else []
    check("admin has brand.manage", "brand.manage" in perms, perms)
    check("admin has product.manage", "product.manage" in perms, perms)
    check("admin has verification.manage", "verification.manage" in perms, perms)
    check("admin has gratification.pay", "gratification.pay" in perms, perms)

    section("3. Classification master seeds (FINAL codes, configurable)")
    conn = provision_pool_conn(tenant_db1)
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM gratification_types WHERE code IN ('e_voucher','reward_points')")
    check("gratification type seeds include e_voucher + reward_points", cur.fetchone()[0] == 2)
    cur.execute("""SELECT count(*) FROM chemist_attachment_types
                   WHERE code IN ('individual','hospital_attached','clinic_attached',
                                  'nursing_home_attached','institutional_pharmacy',
                                  'chain_pharmacy','others')""")
    check("attachment type seeds = 7 FINAL codes", cur.fetchone()[0] == 7)
    cur.execute("""SELECT count(*) FROM chemist_potential_categories
                   WHERE code IN ('a_plus','a','b','c','new')""")
    check("potential category seeds = 5 FINAL codes", cur.fetchone()[0] == 5)
    cur.execute("""SELECT r.name FROM roles r
                   JOIN role_permissions rp ON rp.role_id=r.id
                   WHERE rp.permission_code='chemist.classification.view'
                   AND r.name IN ('ho','nsm','zsm','sm','rsm','asm')""")
    has_view = {row[0] for row in cur.fetchall()}
    check("ho/nsm/zsm/sm/rsm/asm all have chemist.classification.view",
          has_view == {'ho', 'nsm', 'zsm', 'sm', 'rsm', 'asm'}, has_view)
    conn.close()

    section("4. Brand + product creation (brand REQUIRED)")
    r = client.post("/api/v1/brands", headers=T1, json={"name": f"Align Brand {UUID}", "code": f"AB{UUID}"})
    check("create brand (auto-scoped to admin division)", ok(r), f"{r.status_code} {j(r)}")
    B1 = j(r).get("id")

    r = client.post("/api/v1/products", headers=T1, json={"name": "No Brand Prod", "sku": "NB-1"})
    check("product without brand -> 400", r.status_code == 400 and "brand" in j(r).get("detail", "").lower(),
          f"{r.status_code} {j(r)}")

    r = client.post("/api/v1/products", headers=T1, json={
        "name": f"Align Product A {UUID}", "brand_id": B1, "sku": "PA-1",
        "composition": "Paracetamol", "strength": "500 mg", "dosage_form": "Tablet",
        "pack": "10x10", "ptr": 20, "pts": 18, "mrp": 25, "gst": 12,
    })
    check("create product with brand", ok(r), f"{r.status_code} {j(r)}")
    P1 = j(r).get("id")

    conn = provision_pool_conn(tenant_db1)
    cur = conn.cursor()
    cur.execute("SELECT division_id FROM users WHERE username='division_admin'")
    admin_div = cur.fetchone()[0]
    cur.execute("""INSERT INTO campaigns (name, division_id, start_date, end_date, active, status)
                   VALUES (%s,%s,current_date,current_date+30,TRUE,'active') RETURNING id""",
                (f"Align Camp {UUID}", admin_div))
    C1 = cur.fetchone()[0]
    cur.execute("SELECT id FROM roles WHERE name='mr'")
    MR_ROLE = cur.fetchone()[0]
    cur.execute("SELECT id FROM roles WHERE name='verifier'")
    VF_ROLE = cur.fetchone()[0]
    conn.commit()
    conn.close()

    r = client.post("/api/v1/products", headers=T1, json={
        "campaign_id": C1, "brand_id": B1, "name": f"Align Product B {UUID}",
        "sku": "PB-1", "ptr": 10, "pts": 9, "mrp": 12, "gst": 5,
    })
    check("create campaign-linked product", ok(r), f"{r.status_code} {j(r)}")
    P2 = j(r).get("id")

    section("5. Delete protection (deactivate instead)")
    r = client.delete(f"/api/v1/products/{P2}", headers=T1)
    check("campaign-linked product delete -> 409 + deactivate message",
          r.status_code == 409 and "Deactivate it instead" in j(r).get("detail", ""), f"{r.status_code} {j(r)}")
    r = client.delete(f"/api/v1/brands/{B1}", headers=T1)
    check("brand with products delete -> 409 + products-attached message",
          r.status_code == 409 and "products attached" in j(r).get("detail", "").lower(), f"{r.status_code} {j(r)}")

    section("6. Division isolation (division B)")
    r = client.post(f"{BASE}/divisions", headers=OA, json={
        "name": f"Align Div B {UUID}", "code": f"ADB{UUID}",
        "provision": True, "admin_username": "division_admin_b",
        "admin_password": PASSWD, "admin_email": "db@test.local",
        "admin_full_name": "Division B Administrator",
    })
    div2 = j(r)
    tenant_db2 = div2.get("tenant_db_name")
    check("provision division B", ok(r) and bool(tenant_db2), f"{r.status_code} {j(r)}")
    conn = provision_pool_conn(tenant_db2)
    cur = conn.cursor()
    cur.execute("UPDATE users SET must_change_password=FALSE, mfa_setup_required=FALSE, "
                "profile_pending=FALSE WHERE username='division_admin_b'")
    conn.commit()
    conn.close()

    r = client.post("/api/v1/auth/login", json={
        "division_slug": f"ADB{UUID}", "username": "division_admin_b", "password": PASSWD,
    })
    check("division B admin login", ok(r), f"{r.status_code} {j(r)}")
    if not ok(r):
        return finish(keep)
    T2 = {"Authorization": f"Bearer {j(r)['access_token']}"}

    r = client.get("/api/v1/brands", headers=T2)
    check("division B sees no division A brands",
          ok(r) and not any(b["id"] == B1 for b in j(r).get("items", [])), f"{r.status_code} {j(r)}")
    r = client.put(f"/api/v1/brands/{B1}", headers=T2, json={"name": "Hijack"})
    check("division B cannot edit division A brand -> 404", r.status_code == 404, f"{r.status_code} {j(r)}")
    r = client.post("/api/v1/products", headers=T2, json={
        "name": "Cross Prod", "brand_id": B1, "sku": "XP-1",
    })
    check("division B cannot use division A brand -> 404", r.status_code == 404, f"{r.status_code} {j(r)}")
    r = client.get("/api/v1/products", headers=T2)
    check("division B product list excludes division A products",
          ok(r) and not any(p["id"] in (P1, P2) for p in j(r).get("items", [])), f"{r.status_code} {j(r)}")
    r = client.put(f"/api/v1/products/{P1}", headers=T2, json={"name": "Hijack"})
    check("division B cannot edit division A product -> 404", r.status_code == 404, f"{r.status_code} {j(r)}")

    section("7. End-user (MR) gating")
    r = client.post("/api/v1/users", headers=T1, json={
        "username": "mr_align", "password": PASSWD, "full_name": "MR Align",
        "role_id": MR_ROLE, "division_id": admin_div,
    })
    check("create MR user in division A", ok(r), f"{r.status_code} {j(r)}")
    MR_ID = j(r).get("id")
    r = client.post("/api/v1/auth/login", json={
        "division_slug": f"ADA{UUID}", "username": "mr_align", "password": PASSWD,
    })
    check("MR login", ok(r), f"{r.status_code} {j(r)}")
    if not ok(r):
        return finish(keep)
    TM = {"Authorization": f"Bearer {j(r)['access_token']}"}
    me = client.get("/api/v1/auth/me", headers=TM)
    mperms = (j(me).get("permissions") or []) if ok(me) else []
    check("MR has product.view + brand.view", "product.view" in mperms and "brand.view" in mperms, mperms)
    check("MR lacks product.manage + brand.manage",
          "product.manage" not in mperms and "brand.manage" not in mperms, mperms)

    r = client.get("/api/v1/products", headers=TM)
    check("MR can view products", ok(r), f"{r.status_code} {j(r)}")
    r = client.get("/api/v1/brands", headers=TM)
    check("MR can view brands", ok(r), f"{r.status_code} {j(r)}")
    r = client.post("/api/v1/products", headers=TM, json={"name": "MR Prod", "brand_id": B1})
    check("MR cannot create product -> 403", r.status_code == 403, f"{r.status_code} {j(r)}")
    r = client.post("/api/v1/brands", headers=TM, json={"name": "MR Brand"})
    check("MR cannot create brand -> 403", r.status_code == 403, f"{r.status_code} {j(r)}")
    r = client.get("/api/v1/products/bulk-template", headers=TM)
    check("MR cannot fetch product bulk template -> 403", r.status_code == 403, f"{r.status_code} {j(r)}")

    section("8. Verification agent restrictions")
    # The verifier role is a global (super-admin assigned) role, so seed the
    # test verifier directly with the admin's password hash.
    conn = provision_pool_conn(tenant_db1)
    cur = conn.cursor()
    cur.execute("SELECT password FROM users WHERE username='division_admin'")
    _pw = cur.fetchone()[0]
    cur.execute("""INSERT INTO users (username, password, full_name, role_id, division_id, status)
                   VALUES (%s,%s,%s,%s,%s,'active') RETURNING id""",
                ("verifier_align", _pw, "Verifier Align", VF_ROLE, admin_div))
    VF_ID = cur.fetchone()[0]
    conn.commit()
    conn.close()
    check("verifier user seeded", bool(VF_ID), VF_ID)
    r = client.post("/api/v1/auth/login", json={
        "division_slug": f"ADA{UUID}", "username": "verifier_align", "password": PASSWD,
    })
    check("verifier login", ok(r), f"{r.status_code} {j(r)}")
    if not ok(r):
        return finish(keep)
    TV = {"Authorization": f"Bearer {j(r)['access_token']}"}

    conn = provision_pool_conn(tenant_db1)
    cur = conn.cursor()
    cur.execute("""INSERT INTO chemists (name, shop_name, mobile, division_id, status)
                   VALUES (%s,%s,%s,%s,'active') RETURNING id""",
                (f"Align Chemist {UUID}", "Align Pharmacy", "9812312345", admin_div))
    CHEM_ID = cur.fetchone()[0]
    cur.execute("""INSERT INTO pob_activities (user_id, campaign_id, product_id, chemist_id,
                   quantity, ptr, mrp, invoice_amount, pob_amount, status)
                   VALUES (%s,%s,%s,%s,10,20,25,250,250,'pending_verification') RETURNING id""",
                (MR_ID, C1, P1, CHEM_ID))
    POB_ID = cur.fetchone()[0]
    cur.execute("INSERT INTO pob_verifications (pob_id, status) VALUES (%s,'pending') RETURNING id", (POB_ID,))
    VID = cur.fetchone()[0]
    conn.commit()
    conn.close()

    r = client.post(f"/api/v1/pob/{POB_ID}/re-extract", headers=TV,
                    files={"file": ("x.png", b"", "image/png")})
    check("verifier cannot re-extract -> 403", r.status_code == 403, f"{r.status_code} {j(r)}")
    r = client.post(f"/api/v1/verification/{VID}/correct", headers=TV,
                    json={"field_name": "invoice_number", "corrected_value": "123", "reason": "typo"})
    check("verifier cannot correct invoice -> 403", r.status_code == 403, f"{r.status_code} {j(r)}")
    r = client.post("/api/v1/verification/run-pipeline", headers=TV, json={"verification_id": VID})
    check("verifier cannot run pipeline -> 403", r.status_code == 403, f"{r.status_code} {j(r)}")
    r = client.post(f"/api/v1/verification/{VID}/approve", headers=TV, json={"note": ""})
    check("verifier CAN approve (review decision stays)", ok(r), f"{r.status_code} {j(r)}")

    section("9. UPI QR capture + introspection")
    payload = "upi://pay?pa=shop123@okaxis&pn=Align Pharmacy&am=250.00&tr=TX123"
    r = client.post(f"/api/v1/chemists/{CHEM_ID}/upi/decode", headers=TM, json={"payload": payload})
    d = j(r)
    check("decode UPI payload from QR text", ok(r) and d.get("details", {}).get("valid"), f"{r.status_code} {d}")
    details = d.get("details", {})
    check("decoded VPA extracted", details.get("upi_id") == "shop123@okaxis", details)

    r = client.post(f"/api/v1/chemists/{CHEM_ID}/upi", headers=TM, json={
        "upi_id": "shop123@okaxis", "source": "qr", "raw_payload": payload,
        "payee_name": "Align Pharmacy", "confirmed": True,
    })
    check("save confirmed UPI scan", ok(r), f"{r.status_code} {j(r)}")
    conn = provision_pool_conn(tenant_db1)
    cur = conn.cursor()
    cur.execute("SELECT upi_id, upi_confirmed, upi_scan_source, upi_payee_name, upi_confirmed_by "
                "FROM chemists WHERE id=%s", (CHEM_ID,))
    row = cur.fetchone()
    check("chemist UPI persisted + confirmed",
          row[0] == "shop123@okaxis" and row[1] is True and row[2] == "qr" and row[3] == "Align Pharmacy" and row[4] == MR_ID,
          row)
    cur.execute("SELECT qr_type FROM upi_scans WHERE chemist_id=%s AND upi_id=%s", (CHEM_ID, "shop123@okaxis"))
    qrow = cur.fetchone()
    check("upi_scan captured qr_type from payload", qrow and qrow[0] == "pay", qrow)
    conn.close()

    section("10. Chemist UPI flows into gratification approval")
    conn = provision_pool_conn(tenant_db1)
    cur = conn.cursor()
    cur.execute("""INSERT INTO gratifications (pob_id, user_id, campaign_id, type_code, scheme_value)
                   VALUES (%s,%s,%s,'cashback',250) RETURNING id""", (POB_ID, MR_ID, C1))
    GID = cur.fetchone()[0]
    conn.commit()
    conn.close()

    r = client.post(f"/api/v1/gratification/{GID}/approve", headers=T1, json={})
    check("cashback approval falls back to confirmed chemist UPI", ok(r), f"{r.status_code} {j(r)}")

    r = client.get(f"/api/v1/gratification/{GID}", headers=T1)
    g = j(r)
    check("admin sees raw chemist UPI", ok(r) and g.get("chemist_upi_id") == "shop123@okaxis"
          and g.get("upi_id") == "shop123@okaxis", f"{r.status_code} {g}")
    r = client.get(f"/api/v1/gratification/{GID}", headers=TM)
    g2 = j(r)
    check("MR sees masked chemist UPI (no payment perms)",
          g2.get("chemist_upi_id") == "sh****@okaxis" and g2.get("upi_id") == "sh****@okaxis", f"{r.status_code} {g2}")

    section("11. E-Voucher + Reward Points lifecycles (configurable types)")
    r = client.get("/api/v1/gratification/types", headers=T1)
    types = {t["code"] for t in j(r).get("items", [])}
    check("configurable types include E-Voucher + Reward Points",
          ok(r) and {"e_voucher", "reward_points", "cashback", "voucher"} <= types,
          f"{r.status_code} {j(r)}")

    conn = provision_pool_conn(tenant_db1)
    cur = conn.cursor()
    cur.execute("""INSERT INTO pob_activities (user_id, campaign_id, product_id, chemist_id,
                   quantity, ptr, mrp, invoice_amount, pob_amount, status)
                   VALUES (%s,%s,%s,%s,10,20,25,250,250,'pending_verification') RETURNING id""",
                (MR_ID, C1, P1, CHEM_ID))
    POB2 = cur.fetchone()[0]
    cur.execute("""INSERT INTO gratifications (pob_id, user_id, campaign_id, type_code, scheme_value)
                   VALUES (%s,%s,%s,'e_voucher',500) RETURNING id""", (POB2, MR_ID, C1))
    EV = cur.fetchone()[0]
    cur.execute("""INSERT INTO pob_activities (user_id, campaign_id, product_id, chemist_id,
                   quantity, ptr, mrp, invoice_amount, pob_amount, status)
                   VALUES (%s,%s,%s,%s,10,20,25,250,250,'pending_verification') RETURNING id""",
                (MR_ID, C1, P1, CHEM_ID))
    POB3 = cur.fetchone()[0]
    cur.execute("""INSERT INTO gratifications (pob_id, user_id, campaign_id, type_code, scheme_value)
                   VALUES (%s,%s,%s,'reward_points',1200) RETURNING id""", (POB3, MR_ID, C1))
    RP = cur.fetchone()[0]
    conn.commit()
    conn.close()

    r = client.post(f"/api/v1/gratification/{EV}/generate-voucher", headers=T1, json={})
    check("e-voucher generates a voucher code", ok(r) and j(r).get("status") == "generated",
          f"{r.status_code} {j(r)}")
    r = client.post(f"/api/v1/gratification/{EV}/send-voucher", headers=T1, json={"to": "MR"})
    sent_ok = ok(r)
    r = client.post(f"/api/v1/gratification/{EV}/redeem-voucher", headers=T1, json={"redeemed_by": "MR"})
    check("e-voucher completes via the voucher lifecycle",
          sent_ok and ok(r) and j(r).get("status") == "completed", f"{r.status_code} {j(r)}")

    r = client.post(f"/api/v1/gratification/{RP}/approve", headers=T1, json={})
    check("reward points approve (no UPI required)", ok(r) and j(r).get("status") == "approved",
          f"{r.status_code} {j(r)}")
    r = client.post(f"/api/v1/gratification/{RP}/pay", headers=T1, json={"payment_ref": "CR-0001"})
    check("reward points paid -> completed", ok(r) and j(r).get("status") == "completed",
          f"{r.status_code} {j(r)}")

    section("12. Brand-change lock for used products")
    r = client.post("/api/v1/brands", headers=T1, json={"name": f"Align Brand 2 {UUID}", "code": f"AB2{UUID}"})
    check("create second brand", ok(r), f"{r.status_code} {j(r)}")
    B2 = j(r).get("id")

    # P1 already has POB usage from section 8/11 (product_id=P1) -- use a
    # freshly created, never-referenced product for the "allowed" case.
    r = client.post("/api/v1/products", headers=T1, json={
        "name": f"Align Product C {UUID}", "brand_id": B1, "sku": "PC-1", "ptr": 5, "pts": 4, "mrp": 6,
    })
    check("create unused product", ok(r), f"{r.status_code} {j(r)}")
    P3 = j(r).get("id")

    r = client.put(f"/api/v1/products/{P2}", headers=T1, json={"brand_id": B2})
    check("brand change blocked on campaign-linked product -> 409",
          r.status_code == 409 and "brand" in j(r).get("detail", "").lower(), f"{r.status_code} {j(r)}")
    r = client.put(f"/api/v1/products/{P3}", headers=T1, json={"brand_id": B2})
    check("brand change allowed on unused product", ok(r), f"{r.status_code} {j(r)}")
    r = client.put(f"/api/v1/products/{P3}", headers=T1, json={"brand_id": B1})
    check("brand change back (still unused)", ok(r), f"{r.status_code} {j(r)}")

    section("13. Product pricing safety (active-campaign guard)")
    r = client.put(f"/api/v1/products/{P2}", headers=T1, json={"ptr": 999})
    check("pricing change blocked on active-campaign product -> 409",
          r.status_code == 409 and "active campaign" in j(r).get("detail", "").lower(), f"{r.status_code} {j(r)}")
    r = client.put(f"/api/v1/products/{P2}", headers=T1, json={"ptr": 999, "confirm_price_change": True})
    check("pricing change allowed with explicit confirm_price_change", ok(r), f"{r.status_code} {j(r)}")

    section("14. Campaign builder cannot create an unbranded product")
    r = client.put(f"/api/v1/campaigns/{C1}", headers=T1, json={
        "products": [{"name": f"Unbranded Quick {UUID}", "ptr": 5}],
    })
    check("campaign save rejects an inline product with no brand -> 400",
          r.status_code == 400 and "brand" in j(r).get("detail", "").lower(), f"{r.status_code} {j(r)}")

    section("15. Verification agent -- explicit Needs Review")
    conn = provision_pool_conn(tenant_db1)
    cur = conn.cursor()
    cur.execute("""INSERT INTO pob_activities (user_id, campaign_id, product_id, chemist_id,
                   quantity, ptr, mrp, invoice_amount, pob_amount, status)
                   VALUES (%s,%s,%s,%s,10,20,25,250,250,'pending_verification') RETURNING id""",
                (MR_ID, C1, P1, CHEM_ID))
    POB4 = cur.fetchone()[0]
    cur.execute("INSERT INTO pob_verifications (pob_id, status) VALUES (%s,'pending') RETURNING id", (POB4,))
    VID4 = cur.fetchone()[0]
    conn.commit()
    conn.close()

    r = client.post(f"/api/v1/verification/{VID4}/flag_review", headers=TV, json={})
    check("flag_review requires a reason -> 400", r.status_code == 400, f"{r.status_code} {j(r)}")
    r = client.post(f"/api/v1/verification/{VID4}/flag_review", headers=TV,
                    json={"reason": "Invoice legible but needs a follow-up call"})
    check("verifier can flag a pending item for review", ok(r), f"{r.status_code} {j(r)}")
    check("flag_review response reports pending/pending_agent",
          j(r).get("status") == "pending" and j(r).get("pipeline_status") == "pending_agent", j(r))

    conn = provision_pool_conn(tenant_db1)
    cur = conn.cursor()
    cur.execute("SELECT status, pipeline_status, reason FROM pob_verifications WHERE id=%s", (VID4,))
    vrow = cur.fetchone()
    check("verification stays pending with pipeline_status=pending_agent",
          vrow[0] == "pending" and vrow[1] == "pending_agent", vrow)
    cur.execute("SELECT verification_state FROM pob_activities WHERE id=%s", (POB4,))
    check("POB verification_state -> manual_review", cur.fetchone()[0] == "manual_review")
    cur.execute("SELECT action FROM verification_history WHERE pob_id=%s ORDER BY id DESC LIMIT 1", (POB4,))
    check("verification_history records needs_review", cur.fetchone()[0] == "needs_review")
    cur.execute("SELECT count(*) FROM gratifications WHERE pob_id=%s", (POB4,))
    check("no gratification created by flag_review", cur.fetchone()[0] == 0)
    conn.close()

    r = client.get("/api/v1/verification/queue", headers=TV, params={"status": "pending"})
    check("flagged POB still appears in the pending queue",
          ok(r) and any(item.get("verification_id") == VID4 for item in j(r).get("items", [])),
          f"{r.status_code} {j(r)}")

    section("16. Verification agent permissions are POB-only")
    r = client.get("/api/v1/auth/me", headers=TV)
    vperms = (j(r).get("permissions") or []) if ok(r) else []
    for forbidden in ("statement.verify", "statement.credits", "statement.view",
                      "verification.manage", "product.manage", "campaign.manage",
                      "user.manage", "gratification.manage", "gratification.approve",
                      "gratification.pay", "brand.manage", "chemist.classification.manage"):
        check(f"verifier lacks {forbidden}", forbidden not in vperms, vperms)
    for required in ("dashboard.view", "verification.view", "verification.approve",
                     "verification.reject", "pob.view", "report.view", "notification.view"):
        check(f"verifier has {required}", required in vperms, vperms)

    section("17. Intra-tenant division isolation -- set up Division A2")
    # Division A (admin_div / T1 / TM) and a second internal division inside
    # the SAME tenant (tenant_db1), to test chemist.division_id / POB / campaign
    # product isolation between two divisions sharing one company.
    conn = provision_pool_conn(tenant_db1)
    cur = conn.cursor()
    cur.execute("INSERT INTO divisions (name, code, status) VALUES (%s,%s,'active') RETURNING id",
                (f"Align Div A2 {UUID}", f"ADA2{UUID}"))
    DIV_A2 = cur.fetchone()[0]
    cur.execute("SELECT id FROM roles WHERE name='division_admin'")
    DA_ROLE = cur.fetchone()[0]
    cur.execute("SELECT password FROM users WHERE username='division_admin'")
    _pw2 = cur.fetchone()[0]
    cur.execute("""INSERT INTO users (username, password, full_name, role_id, division_id, status,
                   must_change_password, mfa_setup_required, profile_pending)
                   VALUES (%s,%s,%s,%s,%s,'active',FALSE,FALSE,FALSE) RETURNING id""",
                ("division_admin_a2", _pw2, "Division A2 Administrator", DA_ROLE, DIV_A2))
    DA2_UID = cur.fetchone()[0]
    cur.execute("""INSERT INTO users (username, password, full_name, role_id, division_id, status,
                   must_change_password, mfa_setup_required, profile_pending)
                   VALUES (%s,%s,%s,%s,%s,'active',FALSE,FALSE,FALSE) RETURNING id""",
                ("mr_align_a2", _pw2, "MR Align A2", MR_ROLE, DIV_A2))
    MR_A2_ID = cur.fetchone()[0]
    conn.commit()
    conn.close()
    check("division A2 created inside tenant 1", bool(DIV_A2), DIV_A2)

    # division A2 is a SECOND internal division inside the same tenant, not a
    # separate platform-provisioned division, so it has no division-slug login
    # link of its own -- route these logins by (globally unique) username
    # instead, via the same cross-tenant index provisioning itself uses.
    from saas import user_index as _user_index
    _user_index.sync_user(tenant_db1, "division_admin_a2", DA2_UID, division_id=div1_id)
    _user_index.sync_user(tenant_db1, "mr_align_a2", MR_A2_ID, division_id=div1_id)

    r = client.post("/api/v1/auth/login", json={"username": "division_admin_a2", "password": PASSWD})
    check("division A2 admin login (same tenant)", ok(r), f"{r.status_code} {j(r)}")
    T3 = {"Authorization": f"Bearer {j(r)['access_token']}"}
    r = client.post("/api/v1/auth/login", json={"username": "mr_align_a2", "password": PASSWD})
    check("division A2 MR login (same tenant)", ok(r), f"{r.status_code} {j(r)}")
    TM2 = {"Authorization": f"Bearer {j(r)['access_token']}"}

    r = client.post("/api/v1/brands", headers=T3, json={"name": f"Align Brand A2 {UUID}", "code": f"ABA2{UUID}"})
    check("division A2 admin creates own brand", ok(r), f"{r.status_code} {j(r)}")
    B_A2 = j(r).get("id")
    r = client.post("/api/v1/products", headers=T3, json={
        "name": f"Align Product A2 {UUID}", "brand_id": B_A2, "sku": "PA2-1",
        "ptr": 15, "pts": 13, "mrp": 18, "gst": 12,
    })
    check("division A2 admin creates own product", ok(r), f"{r.status_code} {j(r)}")
    P_A2 = j(r).get("id")

    conn = provision_pool_conn(tenant_db1)
    cur = conn.cursor()
    cur.execute("""INSERT INTO campaigns (name, division_id, start_date, end_date, active, status)
                   VALUES (%s,%s,current_date,current_date+30,TRUE,'active') RETURNING id""",
                (f"Align Camp A2 {UUID}", DIV_A2))
    C_A2 = cur.fetchone()[0]
    cur.execute("""INSERT INTO campaign_products (campaign_id, product_id, sort_order,
                   min_quantity, min_pob, scheme_eligibility)
                   VALUES (%s,%s,0,1,0,TRUE)""", (C_A2, P_A2))
    cur.execute("""INSERT INTO chemists (name, shop_name, mobile, division_id, status)
                   VALUES (%s,%s,%s,%s,'active') RETURNING id""",
                (f"Align Chemist A2 {UUID}", "Align Pharmacy A2", "9812399999", DIV_A2))
    CHEM_A2 = cur.fetchone()[0]
    # A legacy chemist with no division at all (pre-division-model data).
    cur.execute("""INSERT INTO chemists (name, shop_name, mobile, division_id, status)
                   VALUES (%s,%s,%s,NULL,'active') RETURNING id""",
                (f"Align Chemist Legacy {UUID}", "Legacy Pharmacy", "9812388888"))
    CHEM_LEGACY = cur.fetchone()[0]
    conn.commit()
    conn.close()
    check("division A2 campaign + product + chemist seeded", all([C_A2, P_A2, CHEM_A2, CHEM_LEGACY]))

    section("18. Chemist division isolation + CRUD security (items 1-4)")
    r = client.get(f"/api/v1/chemists/{CHEM_ID}", headers=T3)
    check("division A2 cannot view division A chemist -> 404", r.status_code == 404, f"{r.status_code} {j(r)}")
    r = client.put(f"/api/v1/chemists/{CHEM_ID}", headers=TM2, json={"name": "Hijack"})
    check("division A2 cannot update division A chemist -> 404", r.status_code == 404, f"{r.status_code} {j(r)}")
    r = client.delete(f"/api/v1/chemists/{CHEM_ID}", headers=TM2)
    check("division A2 cannot delete division A chemist -> 404", r.status_code == 404, f"{r.status_code} {j(r)}")
    r = client.get("/api/v1/chemists", headers=T3)
    check("division A2 chemist list excludes division A chemist",
          ok(r) and not any(c["id"] == CHEM_ID for c in j(r).get("items", [])), f"{r.status_code} {j(r)}")

    r = client.get(f"/api/v1/chemists/{CHEM_A2}", headers=T1)
    check("division A cannot view division A2 chemist -> 404", r.status_code == 404, f"{r.status_code} {j(r)}")
    r = client.put(f"/api/v1/chemists/{CHEM_A2}", headers=TM, json={"name": "Hijack"})
    check("division A cannot update division A2 chemist -> 404", r.status_code == 404, f"{r.status_code} {j(r)}")
    r = client.delete(f"/api/v1/chemists/{CHEM_A2}", headers=TM)
    check("division A cannot delete division A2 chemist -> 404", r.status_code == 404, f"{r.status_code} {j(r)}")
    r = client.get("/api/v1/chemists", headers=T1)
    check("division A chemist list excludes division A2 chemist",
          ok(r) and not any(c["id"] == CHEM_A2 for c in j(r).get("items", [])), f"{r.status_code} {j(r)}")

    r = client.post("/api/v1/chemists", headers=TM, json={
        "name": "Cross Division Chemist", "mobile": "9800011122", "division_id": DIV_A2,
    })
    check("division A MR cannot create a chemist for division A2 -> 403", r.status_code == 403,
          f"{r.status_code} {j(r)}")

    r = client.get("/api/v1/chemists", headers=T1)
    check("legacy NULL-division chemist excluded from division A list",
          ok(r) and not any(c["id"] == CHEM_LEGACY for c in j(r).get("items", [])), f"{r.status_code} {j(r)}")
    r = client.get("/api/v1/chemists", headers=T3)
    check("legacy NULL-division chemist excluded from division A2 list",
          ok(r) and not any(c["id"] == CHEM_LEGACY for c in j(r).get("items", [])), f"{r.status_code} {j(r)}")
    r = client.get(f"/api/v1/chemists/{CHEM_LEGACY}", headers=T1)
    check("legacy NULL-division chemist not viewable by division A -> 404", r.status_code == 404,
          f"{r.status_code} {j(r)}")

    section("19. UPI security (item 5)")
    r = client.post(f"/api/v1/chemists/{CHEM_ID}/upi/decode", headers=TM2,
                    json={"payload": "shop@upi"})
    check("division A2 cannot decode UPI for division A chemist -> 403", r.status_code == 403,
          f"{r.status_code} {j(r)}")
    r = client.post(f"/api/v1/chemists/{CHEM_ID}/upi", headers=TM2, json={
        "upi_id": "hijack@upi", "source": "manual", "confirmed": True,
    })
    check("division A2 cannot save UPI for division A chemist -> 403", r.status_code == 403,
          f"{r.status_code} {j(r)}")

    section("20. POB cross-division validation (item 12) + product/campaign link (item 13)")
    r = client.post("/api/v1/pob/submit", headers=TM, data={
        "campaign_id": C_A2, "product_id": P_A2, "chemist_id": CHEM_ID,
        "quantity": 5, "invoice_amount": 100, "pob_amount": 100,
    })
    check("division A MR cannot submit POB against division A2 campaign -> 403", r.status_code == 403,
          f"{r.status_code} {j(r)}")
    r = client.post("/api/v1/pob/submit", headers=TM, data={
        "campaign_id": C1, "product_id": P2, "chemist_id": CHEM_A2,
        "quantity": 5, "invoice_amount": 100, "pob_amount": 100,
    })
    check("division A MR cannot submit POB against division A2 chemist -> 403", r.status_code == 403,
          f"{r.status_code} {j(r)}")

    r = client.put(f"/api/v1/campaigns/{C1}", headers=T1, json={"products": [{"id": P_A2}]})
    check("division A cannot link division A2's product into its campaign -> 400",
          r.status_code == 400, f"{r.status_code} {j(r)}")

    section("21. POB list pagination count (item 11)")
    r = client.get("/api/v1/pob", headers=T1)
    check("POB list (unfiltered) call ok", ok(r), f"{r.status_code} {j(r)}")
    total_all = j(r).get("total")
    r = client.get("/api/v1/pob", headers=T1, params={"status": "verified"})
    check("POB list (status=verified) call ok", ok(r), f"{r.status_code} {j(r)}")
    items_verified = j(r).get("items", [])
    total_verified = j(r).get("total")
    check("filtered POB total matches the number of matching rows, not the global count",
          total_verified is not None and total_verified < total_all, (total_verified, total_all))
    check("filtered POB total matches returned items (single page)",
          total_verified == len(items_verified), (total_verified, len(items_verified)))

    section("22. Verification decision integrity + dead claim endpoint (items 8-9)")
    r = client.post(f"/api/v1/verification/{VID}/duplicate", headers=TV, json={"reason": "late dup check"})
    check("cannot mark an already-approved verification as duplicate -> 409", r.status_code == 409,
          f"{r.status_code} {j(r)}")
    r = client.post(f"/api/v1/verification/{VID}/claim", headers=TV)
    check("verification claim endpoint no longer exists", r.status_code in (404, 405),
          f"{r.status_code} {j(r)}")

    section("23. Verification agent division isolation (explicit invariant)")
    # Each division runs its OWN verification_agent (division-scoped role --
    # distinct from the global 'verifier' role used above in section 8/15/16,
    # which is intentionally company-wide). Prove an agent bound to one
    # division can never act on another division's verification queue.
    conn = provision_pool_conn(tenant_db1)
    cur = conn.cursor()
    cur.execute("SELECT id FROM roles WHERE name='verification_agent'")
    VA_ROLE = cur.fetchone()[0]
    cur.execute("""INSERT INTO users (username, password, full_name, role_id, division_id, status)
                   VALUES (%s,%s,%s,%s,%s,'active') RETURNING id""",
                ("agent_align_a", _pw, "Agent Align A", VA_ROLE, admin_div))
    VA1_ID = cur.fetchone()[0]
    cur.execute("""INSERT INTO users (username, password, full_name, role_id, division_id, status)
                   VALUES (%s,%s,%s,%s,%s,'active') RETURNING id""",
                ("agent_align_a2", _pw, "Agent Align A2", VA_ROLE, DIV_A2))
    VA2_ID = cur.fetchone()[0]
    cur.execute("""INSERT INTO pob_activities (user_id, campaign_id, product_id, chemist_id,
                   quantity, ptr, mrp, invoice_amount, pob_amount, status)
                   VALUES (%s,%s,%s,%s,10,20,25,250,250,'pending_verification') RETURNING id""",
                (MR_ID, C1, P2, CHEM_ID))
    POB_VA_A = cur.fetchone()[0]
    cur.execute("INSERT INTO pob_verifications (pob_id, status) VALUES (%s,'pending') RETURNING id", (POB_VA_A,))
    VID_VA_A = cur.fetchone()[0]
    cur.execute("""INSERT INTO pob_activities (user_id, campaign_id, product_id, chemist_id,
                   quantity, ptr, mrp, invoice_amount, pob_amount, status)
                   VALUES (%s,%s,%s,%s,10,15,18,180,180,'pending_verification') RETURNING id""",
                (MR_A2_ID, C_A2, P_A2, CHEM_A2))
    POB_VA_A2 = cur.fetchone()[0]
    cur.execute("INSERT INTO pob_verifications (pob_id, status) VALUES (%s,'pending') RETURNING id", (POB_VA_A2,))
    VID_VA_A2 = cur.fetchone()[0]
    conn.commit()
    conn.close()
    _user_index.sync_user(tenant_db1, "agent_align_a2", VA2_ID, division_id=div1_id)

    r = client.post("/api/v1/auth/login", json={
        "division_slug": f"ADA{UUID}", "username": "agent_align_a", "password": PASSWD,
    })
    check("division A verification_agent login", ok(r), f"{r.status_code} {j(r)}")
    TVA1 = {"Authorization": f"Bearer {j(r)['access_token']}"}
    r = client.post("/api/v1/auth/login", json={"username": "agent_align_a2", "password": PASSWD})
    check("division A2 verification_agent login", ok(r), f"{r.status_code} {j(r)}")
    TVA2 = {"Authorization": f"Bearer {j(r)['access_token']}"}

    r = client.post(f"/api/v1/verification/{VID_VA_A}/approve", headers=TVA2, json={"note": ""})
    check("division A2 agent cannot approve division A verification -> 403", r.status_code == 403,
          f"{r.status_code} {j(r)}")
    r = client.post(f"/api/v1/verification/{VID_VA_A2}/approve", headers=TVA1, json={"note": ""})
    check("division A agent cannot approve division A2 verification -> 403", r.status_code == 403,
          f"{r.status_code} {j(r)}")
    r = client.post(f"/api/v1/verification/{VID_VA_A}/reject", headers=TVA2, json={"reason": "x"})
    check("division A2 agent cannot reject division A verification -> 403", r.status_code == 403,
          f"{r.status_code} {j(r)}")
    r = client.post(f"/api/v1/verification/{VID_VA_A}/duplicate", headers=TVA2, json={"reason": "x"})
    check("division A2 agent cannot mark division A verification duplicate -> 403", r.status_code == 403,
          f"{r.status_code} {j(r)}")

    r = client.post(f"/api/v1/verification/{VID_VA_A}/approve", headers=TVA1, json={"note": ""})
    check("division A agent CAN approve its own division's verification", ok(r), f"{r.status_code} {j(r)}")
    r = client.post(f"/api/v1/verification/{VID_VA_A2}/approve", headers=TVA2, json={"note": ""})
    check("division A2 agent CAN approve its own division's verification", ok(r), f"{r.status_code} {j(r)}")

    return finish(keep)


if __name__ == "__main__":
    sys.exit(main())