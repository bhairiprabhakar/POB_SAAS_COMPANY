"""
Tenant product CRUD test: /api/v1/products POST / PUT / DELETE.

Provisions a fresh tenant division (isolated platform DB, never touches
production), logs in as the division admin, and exercises the standalone
product add/edit/delete endpoints exposed by the "Products" master page.

Run:  venv/Scripts/python.exe scripts/test_product_crud.py [--keep]
"""
import os
import sys
import uuid

UUID = uuid.uuid4().hex[:8].upper()
os.environ["PLATFORM_DB_NAME"] = f"POB_PRODUCT_TEST_{UUID}"
os.environ["TENANT_DB_PREFIX"] = f"POB_PRODUCT_TEST_{UUID}_"
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


def main():
    keep = "--keep" in sys.argv
    client = TestClient(app, raise_server_exceptions=False)

    section("1. Provision an isolated division")
    r = client.post("/api/v1/auth/register", json={
        "company": {"legal_name": "Product Test Co", "display_name": "PTC",
                     "code": f"PTC{UUID}"},
        "owner": {"username": "owner01", "password": PASSWD,
                  "full_name": "Test Owner", "email": "owner@test.local"},
    })
    check("register company creates owner", ok(r), f"{r.status_code} {j(r)}")
    owner_token = j(r).get("access_token")
    OA = {"Authorization": f"Bearer {owner_token}"}

    r = client.post(f"{BASE}/divisions", headers=OA, json={
        "name": f"Product Div {UUID}", "code": f"PDV{UUID}",
        "provision": True, "admin_username": "division_admin",
        "admin_password": PASSWD, "admin_email": "da@test.local",
        "admin_full_name": "Division Administrator",
    })
    check("create + provision division", ok(r), f"{r.status_code} {j(r)}")
    data = j(r)
    division_id = data.get("id")
    tenant_db = data.get("tenant_db_name")
    check("division has a tenant db", bool(tenant_db), data)
    check("division is active", data.get("status") == "active", data.get("status"))

    # Provisioning leaves the admin in password-change onboarding; clear the
    # flags so the login below returns a normal working session.
    conn = provision_pool_conn(tenant_db)
    cur = conn.cursor()
    cur.execute("UPDATE users SET must_change_password=FALSE, mfa_setup_required=FALSE, "
                "profile_pending=FALSE WHERE username='division_admin'")
    conn.commit()
    conn.close()

    section("2. Tenant login as division admin")
    r = client.post("/api/v1/auth/login", json={
        "division_slug": f"PDV{UUID}", "username": "division_admin", "password": PASSWD,
    })
    check("division admin login", ok(r), f"{r.status_code} {j(r)}")
    if not ok(r):
        return finish(keep)
    T = {"Authorization": f"Bearer {j(r)['access_token']}"}

    me = client.get("/api/v1/auth/me", headers=T)
    perms = (j(me).get("permissions") or []) if ok(me) else []
    check("division admin has campaign.manage", "campaign.manage" in perms, perms)
    check("division admin has product.manage", "product.manage" in perms, perms)
    check("division admin has product.view", "product.view" in perms, perms)

    section("3. Product create (brand REQUIRED)")
    r = client.post("/api/v1/products", headers=T, json={"name": "  "})
    check("product with blank name -> 400", r.status_code == 400, f"{r.status_code} {j(r)}")

    r = client.post("/api/v1/products", headers=T, json={
        "name": "Unbranded Product", "sku": "NOBRAND-1", "composition": "None",
        "strength": "", "dosage_form": "Tablet", "pack": "10×10",
        "ptr": 10, "pts": 9, "mrp": 12, "gst": 12,
    })
    check("product without a brand -> 400 (Brand REQUIRED stored in product master)",
          r.status_code == 400 and "brand" in j(r).get("detail", "").lower(), f"{r.status_code} {j(r)}")

    # Put a campaign in place directly so the endpoint can reference it, and
    # bind it to the admin's tenant division so the division-scoped listing
    # actually returns linkable masters.
    conn = provision_pool_conn(tenant_db)
    cur = conn.cursor()
    cur.execute("SELECT division_id FROM users WHERE username='division_admin'")
    admin_div_id = None
    row = cur.fetchone()
    if row and row[0]:
        admin_div_id = row[0]
    else:
        cur.execute("SELECT id FROM divisions WHERE status='active' ORDER BY id LIMIT 1")
        row = cur.fetchone()
        admin_div_id = row[0] if row else None
    cur.execute("""INSERT INTO campaigns (name, division, division_id, start_date, end_date, active, status)
                   VALUES (%s,'Test Div',%s,current_date,current_date+30,TRUE,'active') RETURNING id""",
                (f"PROD CAMP {UUID}", admin_div_id))
    campaign_id = cur.fetchone()[0]
    cur.execute("""INSERT INTO brands (name, code, division_id, status) VALUES (%s,%s,%s,'active') RETURNING id""",
                (f"PROD Brand {UUID}", f"PB{UUID}", admin_div_id))
    brand_id = cur.fetchone()[0]
    conn.commit()
    conn.close()

    r = client.post("/api/v1/products", headers=T, json={
        "name": "Master Panadol Solo", "brand_id": brand_id, "sku": "PAN-SOLO-1",
        "composition": "Paracetamol", "strength": "500 mg", "dosage_form": "Tablet",
        "pack": "10×10", "ptr": 32.5, "pts": 30.0, "mrp": 40.0, "gst": 12,
    })
    check("product can be created without a campaign (master)", ok(r, 200, 201), f"{r.status_code} {j(r)}")
    master_id = j(r).get("id")
    check("create returns product id", bool(master_id), j(r))

    r = client.post("/api/v1/products", headers=T, json={
        "campaign_id": campaign_id, "brand_id": brand_id,
        "name": "Allegra 120mg Strip", "sku": "ALG-120-1", "composition": "Fexofenadine HCl",
        "strength": "120 mg", "dosage_form": "Tablet", "pack": "10×10",
        "ptr": 112.5, "pts": 105.0, "mrp": 128.0, "gst": 18,
    })
    check("create product linked to campaign", ok(r, 200, 201), f"{r.status_code} {j(r)}")
    pid = j(r).get("id")

    r = client.post("/api/v1/products", headers=T, json={
        "campaign_id": 999999, "brand_id": brand_id, "name": "Ghost",
    })
    check("product on non-existent campaign -> 404", r.status_code == 404, f"{r.status_code}")

    section("4. Product list / get-after-create")
    r = client.get("/api/v1/products", headers=T)
    check("list products", ok(r), f"{r.status_code}")
    mine = [p for p in j(r).get("items", []) if p["id"] == pid]
    check("created product appears with joined brand + division + campaign link",
          len(mine) == 1 and mine[0]["brand_name"] == f"PROD Brand {UUID}", mine)
    master = [p for p in j(r).get("items", []) if p["id"] == master_id]
    check("master product visible in catalogue without campaign",
          len(master) == 1 and master[0]["name"] == "Master Panadol Solo"
          and master[0]["campaign_id"] is None, master)
    r = client.get(f"/api/v1/products?campaign_id={campaign_id}", headers=T)
    camp = [p for p in j(r).get("items", []) if p["id"] == pid]
    check("campaign-scoped list returns linked product",
          len(camp) == 1 and camp[0]["name"] == "Allegra 120mg Strip", camp)
    r = client.get(f"/api/v1/products?campaign_id={campaign_id}", headers=T)
    camp_not_master = any(p["id"] == master_id for p in j(r).get("items", []))
    check("campaign-scoped list excludes unlinked master", not camp_not_master, j(r))

    section("5. Product update (name mismatch fix)")
    r = client.put(f"/api/v1/products/{pid}", headers=T, json={
        "name": "Allegra 120mg Strip (New)",
    })
    check("update product", ok(r), f"{r.status_code} {j(r)}")
    r = client.get("/api/v1/products", headers=T)
    mine = [p for p in j(r).get("items", []) if p["id"] == pid]
    check("rename persisted + brand kept",
          mine and mine[0]["name"] == "Allegra 120mg Strip (New)" and mine[0]["brand_id"] == brand_id, mine)

    r = client.put(f"/api/v1/products/{pid}", headers=T, json={"brand_id": None})
    check("clearing a product's brand blocked -> 400",
          r.status_code == 400 and "brand" in j(r).get("detail", "").lower(), f"{r.status_code} {j(r)}")

    r = client.put(f"/api/v1/products/{pid}", headers=T, json={"name": ""})
    check("blank name on update -> 400", r.status_code == 400, f"{r.status_code}")

    section("6. Product delete protection")
    r = client.delete(f"/api/v1/products/{pid}", headers=T)
    check("campaign-linked product cannot be deleted -> 409 + deactivate message",
          r.status_code == 409 and "Deactivate it instead" in j(r).get("detail", ""), f"{r.status_code} {j(r)}")
    r = client.put(f"/api/v1/products/{pid}", headers=T, json={"status": "inactive"})
    check("deactivate product instead", ok(r), f"{r.status_code} {j(r)}")
    r = client.get("/api/v1/products", headers=T)
    mine = [p for p in j(r).get("items", []) if p["id"] == pid]
    check("deactivated product still listed as inactive",
          mine and mine[0]["status"] == "inactive", mine)
    r = client.delete(f"/api/v1/products/{master_id}", headers=T)
    check("unreferenced master product deletion ok", ok(r), f"{r.status_code} {j(r)}")
    r = client.delete(f"/api/v1/products/{master_id}", headers=T)
    check("re-delete deleted master -> 404", r.status_code == 404, f"{r.status_code}")

    section("7. _sync_products via campaign builder links masters")
    from saas import campaign_service
    conn = provision_pool_conn(tenant_db)
    cur = conn.cursor()
    cur.execute("""INSERT INTO products (division_id, sku, name, brand_id, ptr)
                   VALUES (%s,'SYNC-1','Sync Prod',NULL,50) RETURNING id""",
                (admin_div_id,))
    spid = cur.fetchone()[0]
    conn.commit()
    # The campaign builder sends the ids of division masters it selected.
    campaign_service._sync_products(conn, campaign_id, [{"id": spid}], division_id=admin_div_id)
    conn.commit()
    cur.execute("SELECT product_id FROM campaign_products WHERE campaign_id=%s", (campaign_id,))
    linked = [r[0] for r in cur.fetchall()]
    check("sync links the master product to the campaign", spid in linked, linked)
    cur.execute("SELECT name, ptr, sku FROM products WHERE id=%s", (spid,))
    sync_row = cur.fetchone()
    check("sync keeps master fields untouched (no edits sent)",
          sync_row == ("Sync Prod", 50, "SYNC-1"), sync_row)
    # Removing the selection unlinks but never deletes the master.
    campaign_service._sync_products(conn, campaign_id, [], division_id=admin_div_id)
    conn.commit()
    cur.execute("SELECT count(*) FROM campaign_products WHERE campaign_id=%s AND product_id=%s",
                (campaign_id, spid))
    check("sync unlinks removed selection", cur.fetchone()[0] == 0, "")
    cur.execute("SELECT id FROM products WHERE id=%s", (spid,))
    check("sync did not delete the master product", cur.fetchone() is not None, "")
    conn.close()

    section("8. Excel template download + bulk upload")
    from io import BytesIO
    from openpyxl import Workbook
    r = client.get("/api/v1/products/bulk-template", headers=T)
    check("template download ok", ok(r) and "spreadsheet" in (r.headers.get("content-type") or ""),
          f"{r.status_code} {r.headers.get('content-type')}")
    wb = Workbook()
    ws = wb.active
    ws.append(["brand", "name", "sku", "composition", "strength", "dosage_form", "pack",
               "ptr", "pts", "mrp", "gst", "status"])
    ws.append([f"PROD Brand {UUID}", "Bulk One", "BLK-1", "Diclofenac", "250 mg", "Tablet", "5×5", 20, 18, 25, 12, "active"])
    ws.append(["", "Bulk Two (no brand)", "BLK-2", "", "10 mg", "Drops", "", 9.5, 8, 12, 5, "active"])
    ws.append(["", "", "BLK-3-no-name", "", "", "", "", 10, 9, 14, 0, "active"])
    ws.append(["No Such Brand", "Bulk Four", "BLK-4", "", "", "", "", 5, 4, 8, 0, "active"])
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    r = client.post("/api/v1/products/bulk-upload", headers=T,
                    files={"file": ("products.xlsx", buf.getvalue(),
                                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    check("bulk upload ok", r.status_code == 200, f"{r.status_code} {j(r)}")
    res = j(r)
    check("upload created only branded rows; skipped brand-less / nameless / unknown-brand rows",
          res.get("created") == 1 and len(res.get("errors", [])) == 3, res)
    r = client.get("/api/v1/products", headers=T)
    items = j(r).get("items", [])
    blk = [p for p in items if p["sku"] in ("BLK-1", "BLK-2")]
    check("branded bulk row persisted with its brand; brand-less row rejected",
          len(blk) == 1 and blk[0]["sku"] == "BLK-1" and blk[0]["brand_id"] == brand_id, blk)
    return finish(keep)


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


if __name__ == "__main__":
    sys.exit(main())