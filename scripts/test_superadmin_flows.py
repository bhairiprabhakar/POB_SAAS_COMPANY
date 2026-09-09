"""
Super admin end-to-end test: every page's read path plus every process flow.

Read paths hit the endpoints each super admin page mounts with. Process flows
run against a DISPOSABLE company this script creates and tears down again, so
no existing tenant is touched.

Run:  venv\\Scripts\\python.exe scripts\\test_superadmin_flows.py
      venv\\Scripts\\python.exe scripts\\test_superadmin_flows.py --keep   (skip teardown)
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:                                   # Windows consoles default to cp1252
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from saas import ratelimit
ratelimit.api_limiter.limit = 10 ** 9          # this suite is deliberately chatty

from fastapi.testclient import TestClient
from saas.main import app

BASE = "/api/v1/superadmin"
SA_PASSWORD = os.environ.get("SUPERADMIN_PASSWORD", "Pob_Saas@2026")

PASSED, FAILED, SKIPPED = [], [], []
_section = ""


def section(name):
    global _section
    _section = name
    print(f"\n{'-' * 72}\n{name}\n{'-' * 72}")


def check(name, cond, extra=""):
    label = f"{_section} :: {name}"
    if cond:
        PASSED.append(label)
        print(f"  [PASS] {name}")
    else:
        FAILED.append((label, extra))
        print(f"  [FAIL] {name}  {extra}")
    return bool(cond)


def skip(name, why):
    SKIPPED.append(f"{_section} :: {name}")
    print(f"  [SKIP] {name} — {why}")


def ok(r, *codes):
    return r.status_code in (codes or (200,))


def body(r, limit=180):
    try:
        return str(r.json())[:limit]
    except Exception:
        return r.text[:limit]


def main():
    keep = "--keep" in sys.argv
    client = TestClient(app, raise_server_exceptions=False)
    created_cid = None
    tenant_db = None

    # ── 1. Authentication ───────────────────────────────────────────────────
    section("1. Super admin authentication")

    r = client.post("/api/v1/auth/superadmin/login",
                    json={"username": "superadmin", "password": "definitely-wrong"})
    check("wrong password is rejected", r.status_code in (401, 403, 429), f"{r.status_code} {body(r)}")

    r = client.post("/api/v1/auth/superadmin/login",
                    json={"username": "superadmin", "password": SA_PASSWORD})
    if not check("superadmin login", ok(r), f"{r.status_code} {body(r)}"):
        print("\nCannot continue without a super admin session.")
        return summarise()
    tok = r.json()["access_token"]
    SA = {"Authorization": f"Bearer {tok}"}

    check("no token is rejected", client.get(f"{BASE}/companies").status_code in (401, 403))
    check("garbage token is rejected",
          client.get(f"{BASE}/companies", headers={"Authorization": "Bearer nope"}).status_code in (401, 403))

    # a tenant user must never reach the control plane
    tr = client.post("/api/v1/auth/login",
                     json={"company_code": "DEMO1234", "username": "company_admin", "password": "Admin@123"})
    if ok(tr):
        th = {"Authorization": f"Bearer {tr.json()['access_token']}"}
        code = client.get(f"{BASE}/companies", headers=th).status_code
        check("tenant admin cannot read the control plane", code in (401, 403), f"got {code}")
    else:
        skip("tenant admin cannot read the control plane", "DEMO1234 login unavailable")

    # ── 2. Page read paths ──────────────────────────────────────────────────
    section("2. Page read paths (platform level)")

    pages = {
        "Dashboard":        [f"{BASE}/analytics", f"{BASE}/metrics"],
        "Companies":        [f"{BASE}/companies"],
        "Manage Campaigns": [f"{BASE}/campaigns"],
        "Analytics":        [f"{BASE}/analytics"],
        "Plans":            [f"{BASE}/plans"],
        "Audit Log":        [f"{BASE}/audit-logs"],
        "OCR Usage":        [f"{BASE}/ocr-usage/companies"],
    }
    for page, eps in pages.items():
        for ep in eps:
            r = client.get(ep, headers=SA)
            check(f"{page} → {ep}", ok(r), f"{r.status_code} {body(r)}")

    # data sanity, not just a 200
    r = client.get(f"{BASE}/campaigns", headers=SA)
    if ok(r):
        d = r.json()
        check("Manage Campaigns returns campaigns", len(d.get("items", [])) > 0,
              "empty list — tenant reads may be failing silently")
        check("Manage Campaigns reports unreadable tenants", "unreadable" in d,
              "no 'unreadable' key: failures would be invisible")
        if d.get("unreadable"):
            check("no tenant is unreadable", False, str(d["unreadable"])[:200])

    r = client.get(f"{BASE}/analytics", headers=SA)
    if ok(r):
        a = r.json()
        check("analytics aggregates tenants", (a.get("companies") or {}).get("reporting", 0) > 0)
        check("analytics reports zero unreachable tenants",
              (a.get("companies") or {}).get("unreachable", 0) == 0, str(a.get("unreachable"))[:200])

    # ── 3. Company lifecycle ────────────────────────────────────────────────
    section("3. Company lifecycle: create → provision → subscribe")

    stamp = uuid.uuid4().hex[:6].upper()
    cname = f"QA Flow {stamp}"
    ccode = f"QA{stamp}"[:10]
    admin_user = f"qa_admin_{stamp.lower()}"
    admin_pw = "QaFlow@2026"

    plans = client.get(f"{BASE}/plans", headers=SA)
    plan_id = None
    if ok(plans) and plans.json().get("items"):
        plan_id = plans.json()["items"][0]["id"]

    r = client.post(f"{BASE}/companies", headers=SA, json={
        "name": cname, "code": ccode, "contact_person": "QA Bot",
        "contact_email": "qa@example.invalid", "contact_mobile": "9000000000",
        "plan_id": plan_id, "user_limit": 25,
    })
    if not check("create company", ok(r, 200, 201), f"{r.status_code} {body(r)}"):
        return summarise()
    created_cid = r.json().get("id")
    check("create returns a company id", bool(created_cid), body(r))

    r = client.post(f"{BASE}/companies", headers=SA, json={"name": cname, "code": ccode})
    check("duplicate company code is rejected", r.status_code == 409, f"{r.status_code} {body(r)}")

    r = client.post(f"{BASE}/companies", headers=SA, json={"name": ""})
    check("blank company name is rejected", r.status_code == 400, f"{r.status_code} {body(r)}")

    r = client.post(f"{BASE}/companies/{created_cid}/provision", headers=SA,
                    json={"admin_username": admin_user, "admin_password": admin_pw})
    if check("provision tenant database", ok(r), f"{r.status_code} {body(r)}"):
        tenant_db = r.json().get("tenant_db")
        check("provision returns a tenant db name", bool(tenant_db), body(r))

    r = client.post(f"{BASE}/companies/{created_cid}/provision", headers=SA,
                    json={"admin_username": admin_user, "admin_password": admin_pw})
    check("re-provisioning is idempotent", ok(r), f"{r.status_code} {body(r)}")

    r = client.get(f"{BASE}/companies/{created_cid}", headers=SA)
    if check("read company detail", ok(r), f"{r.status_code} {body(r)}"):
        c = r.json()
        check("detail exposes tenant counts", "user_count" in c, body(r))
        check("provisioned admin user exists", (c.get("user_count") or 0) >= 1,
              f"user_count={c.get('user_count')}")

    r = client.get(f"{BASE}/companies/by-code/{ccode}", headers=SA)
    check("lookup company by code", ok(r) and r.json().get("id") == created_cid, f"{r.status_code} {body(r)}")

    if plan_id:
        r = client.post(f"{BASE}/companies/{created_cid}/subscribe", headers=SA,
                        json={"plan_id": plan_id})
        check("subscribe company to a plan", ok(r, 200, 201), f"{r.status_code} {body(r)}")

    r = client.put(f"{BASE}/companies/{created_cid}", headers=SA,
                   json={"contact_person": "QA Bot Updated", "user_limit": 50})
    check("update company", ok(r), f"{r.status_code} {body(r)}")
    r = client.get(f"{BASE}/companies/{created_cid}", headers=SA)
    check("company update persisted", ok(r) and r.json().get("contact_person") == "QA Bot Updated",
          body(r))

    # ── 4. Company masters ──────────────────────────────────────────────────
    section("4. Company masters: brands, divisions, hierarchy, roles")

    brand_id = div_id = level_id = None

    r = client.post(f"{BASE}/companies/{created_cid}/brands", headers=SA,
                    json={"name": "QA Brand", "code": "QAB", "status": "active"})
    if check("create brand", ok(r, 200, 201), f"{r.status_code} {body(r)}"):
        brand_id = r.json().get("id")
    r = client.get(f"{BASE}/companies/{created_cid}/brands", headers=SA)
    check("list brands includes the new brand", ok(r)
          and any(b.get("name") == "QA Brand" for b in r.json().get("items", [])), body(r))
    if brand_id:
        r = client.put(f"{BASE}/companies/{created_cid}/brands/{brand_id}", headers=SA,
                       json={"name": "QA Brand Renamed"})
        check("update brand", ok(r), f"{r.status_code} {body(r)}")

    r = client.post(f"{BASE}/companies/{created_cid}/divisions", headers=SA,
                    json={"name": "QA Division", "code": "QAD", "status": "active"})
    if check("create division", ok(r, 200, 201), f"{r.status_code} {body(r)}"):
        div_id = r.json().get("id")
    r = client.get(f"{BASE}/companies/{created_cid}/divisions", headers=SA)
    check("list divisions", ok(r), f"{r.status_code} {body(r)}")

    r = client.get(f"{BASE}/companies/{created_cid}/hierarchy/levels", headers=SA)
    check("list hierarchy levels (seeded by provisioning)", ok(r)
          and len(r.json().get("items", [])) > 0, body(r))
    r = client.post(f"{BASE}/companies/{created_cid}/hierarchy/levels", headers=SA,
                    json={"name": "QA Level", "rank": 9})
    if check("create hierarchy level", ok(r, 200, 201), f"{r.status_code} {body(r)}"):
        level_id = r.json().get("id")

    r = client.get(f"{BASE}/companies/{created_cid}/hierarchy/tree", headers=SA)
    check("read hierarchy tree", ok(r), f"{r.status_code} {body(r)}")
    r = client.get(f"{BASE}/companies/{created_cid}/permissions", headers=SA)
    check("read permission catalogue", ok(r), f"{r.status_code} {body(r)}")

    r = client.get(f"{BASE}/companies/{created_cid}/roles", headers=SA)
    roles_ok = check("read roles", ok(r), f"{r.status_code} {body(r)}")
    if roles_ok:
        items = r.json().get("items", [])
        check("roles carry id and name (campaign builder needs both)",
              bool(items) and all("id" in x and "name" in x for x in items), body(r))

    # ── 5. Users ────────────────────────────────────────────────────────────
    section("5. Company users")

    r = client.get(f"{BASE}/companies/{created_cid}/users", headers=SA)
    check("list users", ok(r), f"{r.status_code} {body(r)}")

    uid = None
    role_id = None
    rr = client.get(f"{BASE}/companies/{created_cid}/roles", headers=SA)
    if ok(rr) and rr.json().get("items"):
        role_id = rr.json()["items"][0]["id"]
    r = client.post(f"{BASE}/companies/{created_cid}/users", headers=SA, json={
        "username": f"qa_user_{stamp.lower()}", "password": "QaUser@2026",
        "full_name": "QA Test User", "email": "qa.user@example.invalid",
        "role_id": role_id, "hierarchy_level_id": level_id,
    })
    if check("create user", ok(r, 200, 201), f"{r.status_code} {body(r)}"):
        uid = r.json().get("id")

    if uid:
        r = client.get(f"{BASE}/companies/{created_cid}/users/{uid}", headers=SA)
        check("read user detail", ok(r), f"{r.status_code} {body(r)}")
        check("user detail never leaks the password hash",
              ok(r) and "password" not in (r.json() or {}), body(r))
        r = client.put(f"{BASE}/companies/{created_cid}/users/{uid}", headers=SA,
                       json={"full_name": "QA Test User Renamed"})
        check("update user", ok(r), f"{r.status_code} {body(r)}")

    r = client.post(f"{BASE}/companies/{created_cid}/reset-admin-password", headers=SA,
                    json={"username": admin_user, "new_password": "short"})
    check("short reset password is rejected", r.status_code == 400, f"{r.status_code} {body(r)}")

    r = client.post(f"{BASE}/companies/{created_cid}/reset-admin-password", headers=SA,
                    json={"username": admin_user, "new_password": "QaReset@2026"})
    check("reset admin password", ok(r), f"{r.status_code} {body(r)}")

    r = client.post(f"{BASE}/companies/{created_cid}/reset-admin-password", headers=SA,
                    json={"username": "no_such_user_here", "new_password": "QaReset@2026"})
    check("resetting a non-existent user does not report success",
          not (ok(r) and (r.json() or {}).get("ok")),
          f"{r.status_code} {body(r)} — handler ignores rowcount")
    r = client.post("/api/v1/auth/login",
                    json={"company_code": ccode, "username": admin_user, "password": "QaReset@2026"})
    check("provisioned admin can log in with the reset password", ok(r), f"{r.status_code} {body(r)}")

    # ── 6. Campaign flow ────────────────────────────────────────────────────
    section("6. Campaign flow: create → update → toggle → extend → delete")

    camp_id = None
    r = client.post(f"{BASE}/companies/{created_cid}/campaigns", headers=SA, json={
        "name": "QA Campaign",
        "brand_id": brand_id, "brand_ids": [brand_id] if brand_id else [],
        "division_id": div_id,
        "start_date": "2026-01-01", "end_date": "2026-12-31",
        "status": "active", "active": True,
        "products": [{"name": "QA Product", "sku": "QA-SKU-1", "ptr": 100, "mrp": 130}],
    })
    if check("create campaign", ok(r, 200, 201), f"{r.status_code} {body(r)}"):
        camp_id = r.json().get("id")

    if camp_id:
        r = client.get(f"{BASE}/companies/{created_cid}/campaigns/{camp_id}", headers=SA)
        check("read campaign detail", ok(r), f"{r.status_code} {body(r)}")

        # this exact payload shape (brand_id AND brand_ids) used to 500
        r = client.put(f"{BASE}/companies/{created_cid}/campaigns/{camp_id}", headers=SA, json={
            "name": "QA Campaign Renamed",
            "brand_id": brand_id, "brand_ids": [brand_id] if brand_id else [],
        })
        check("update campaign with brand_id + brand_ids", ok(r), f"{r.status_code} {body(r)}")
        r = client.get(f"{BASE}/companies/{created_cid}/campaigns/{camp_id}", headers=SA)
        check("campaign rename persisted",
              ok(r) and r.json().get("name") == "QA Campaign Renamed", body(r))

        r = client.patch(f"{BASE}/companies/{created_cid}/campaigns/{camp_id}/toggle-active", headers=SA)
        check("toggle campaign active (per-company route)", ok(r), f"{r.status_code} {body(r)}")
        first = r.json().get("active") if ok(r) else None
        r = client.patch(f"{BASE}/campaigns/{created_cid}/{camp_id}/toggle-active", headers=SA)
        check("toggle campaign active (global route)", ok(r), f"{r.status_code} {body(r)}")
        if first is not None and ok(r):
            check("the two toggle routes act on the same record",
                  r.json().get("active") == (not first),
                  f"{first} then {r.json().get('active')}")

        r = client.post(f"{BASE}/companies/{created_cid}/campaigns/{camp_id}/extend", headers=SA,
                        json={"end_date": "2027-06-30"})
        check("extend campaign", ok(r), f"{r.status_code} {body(r)}")

        r = client.get(f"{BASE}/campaigns?company_id={created_cid}", headers=SA)
        check("new campaign appears in the global Manage Campaigns list",
              ok(r) and any(x.get("id") == camp_id for x in r.json().get("items", [])), body(r))

    # ── 7. Activation state ─────────────────────────────────────────────────
    section("7. Company activation state")

    r = client.post(f"{BASE}/companies/{created_cid}/deactivate", headers=SA)
    check("deactivate company", ok(r), f"{r.status_code} {body(r)}")
    r = client.get(f"{BASE}/companies/{created_cid}", headers=SA)
    check("company reads as inactive", ok(r) and r.json().get("status") != "active",
          f"status={r.json().get('status') if ok(r) else body(r)}")
    r = client.post("/api/v1/auth/login",
                    json={"company_code": ccode, "username": admin_user, "password": "QaReset@2026"})
    check("login is refused while the company is deactivated",
          r.status_code in (401, 403), f"{r.status_code} {body(r)}")

    r = client.post(f"{BASE}/companies/{created_cid}/activate", headers=SA)
    check("reactivate company", ok(r), f"{r.status_code} {body(r)}")
    r = client.post("/api/v1/auth/login",
                    json={"company_code": ccode, "username": admin_user, "password": "QaReset@2026"})
    check("login works again after reactivation", ok(r), f"{r.status_code} {body(r)}")

    # ── 8. Audit trail ──────────────────────────────────────────────────────
    section("8. Audit trail")

    r = client.get(f"{BASE}/audit-logs", headers=SA)
    if check("read platform audit log", ok(r), f"{r.status_code} {body(r)}"):
        items = r.json().get("items", [])
        actions = {a.get("action") for a in items}
        check("company.create was audited", "company.create" in actions,
              f"recent actions: {sorted(actions)[:10]}")
        check("audit rows name the actor",
              all(("actor" in a) for a in items[:5]), body(r))

    # ── 9. Teardown ─────────────────────────────────────────────────────────
    section("9. Teardown")

    if keep:
        skip("teardown", f"--keep given; company {ccode} (id {created_cid}) left in place")
    else:
        deleted = []
        if camp_id:
            r = client.delete(f"{BASE}/companies/{created_cid}/campaigns/{camp_id}", headers=SA)
            deleted.append(("campaign", r.status_code))
        if uid:
            r = client.delete(f"{BASE}/companies/{created_cid}/users/{uid}", headers=SA)
            deleted.append(("user", r.status_code))
        if div_id:
            r = client.delete(f"{BASE}/companies/{created_cid}/divisions/{div_id}", headers=SA)
            deleted.append(("division", r.status_code))
        if brand_id:
            r = client.delete(f"{BASE}/companies/{created_cid}/brands/{brand_id}", headers=SA)
            deleted.append(("brand", r.status_code))
        for what, code in deleted:
            check(f"delete {what}", code in (200, 204), f"status {code}")

        check("tenant database and company row removed",
              _drop_company(created_cid, tenant_db), "see stderr for the failure")

    return summarise()


def _drop_company(cid, tenant_db):
    """Remove the disposable tenant database and its control-plane row."""
    from saas import platform_db, provision
    try:
        if tenant_db:
            provision.deprovision_tenant(cid, tenant_db)
        conn = platform_db.get_db()
        try:
            c = conn.cursor()
            for table, col in (("company_subscriptions", "company_id"),
                               ("company_db_credentials", "company_id"),
                               ("platform_audit_logs", "entity_id"),
                               ("provisioning_jobs", "company_id")):
                try:
                    c.execute(f"DELETE FROM {table} WHERE {col}=%s", (cid,))
                    conn.commit()
                except Exception:
                    conn.rollback()
            c.execute("DELETE FROM companies WHERE id=%s", (cid,))
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as exc:
        print(f"    teardown error: {exc}", file=sys.stderr)
        return False


def summarise():
    print(f"\n{'=' * 72}")
    print(f"PASSED {len(PASSED)}   FAILED {len(FAILED)}   SKIPPED {len(SKIPPED)}")
    if FAILED:
        print("\nFailures:")
        for name, extra in FAILED:
            print(f"  x {name}\n      {extra}")
    print('=' * 72)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
