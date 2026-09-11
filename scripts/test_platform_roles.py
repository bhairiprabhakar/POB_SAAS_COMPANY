"""
Platform admin roles hermetic test.

Creates a fresh platform DB (never touches production), provisions a tenant
division, creates campaigns and POBs, then exercises every platform-admin
role: login, path gating, role gating, cross-division proxies.

Run:  venv/Scripts/python.exe scripts/test_platform_roles.py [--keep]
"""
import os, sys, uuid

# ── Environment setup BEFORE importing saas modules ─────────────────────────
UUID = uuid.uuid4().hex[:8].upper()
os.environ["PLATFORM_DB_NAME"] = f"POB_PLATFORM_ROLES_TEST_{UUID}"
os.environ["TENANT_DB_PREFIX"] = f"POB_PLATFORM_ROLES_TEST_{UUID}_"
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

# Trigger the startup that creates + migrates the fresh platform DB.
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

    created_admins = {}
    tenant_db = None
    division_id = None
    campaign_id = None
    user_id = None
    chemist_id = None
    pob_id = None
    verification_id = None
    gratification_id = None

    # ── 1. Register a company (creates owner account) ────────────────────────
    section("1. Owner registration and login")

    r = client.post("/api/v1/auth/register", json={
        "company": {"legal_name": "Platform Roles Test Co", "display_name": "PRTC",
                     "code": f"PRC{UUID}"},
        "owner": {"username": "owner01", "password": PASSWD,
                  "full_name": "Test Owner", "email": "owner@test.local"},
    })
    print(f"  Register response: {r.status_code} {r.text[:300]}")
    check("register company creates owner", ok(r), f"{r.status_code} {r.text[:300]}")
    try:
        rj = r.json()
        owner_token = rj.get("access_token")
        owner_refresh = rj.get("refresh_token")
        owner_user = rj.get("user", {})
    except Exception:
        owner_token = None
        owner_user = {}
    check("owner role=owner", owner_user.get("role") == "owner", owner_user)
    check("owner_flag=True", owner_user.get("owner") is True, owner_user)

    OA = {"Authorization": f"Bearer {owner_token}"}

    # Verify login works too
    r = client.post("/api/v1/auth/superadmin/login",
                     json={"username": "owner01", "password": PASSWD})
    check("owner can login", ok(r), f"{r.status_code}")
    check("login returns role=owner", j(r)["user"]["role"] == "owner")

    # Verify refresh
    r = client.post("/api/v1/auth/superadmin/refresh",
                     json={"refresh_token": owner_refresh})
    check("owner refresh works", ok(r), f"{r.status_code}")
    # The refresh returns only new tokens; verify the new access token decodes
    if ok(r):
        import jwt as _jwt
        from saas import config as _cfg
        token = j(r)["access_token"]
        claims = _jwt.decode(token, _cfg.JWT_SECRET, algorithms=["HS256"])
        check("refresh keeps sa_role=owner", claims.get("sa_role") == "owner", claims)

    # ── 2. Owner creates platform admins ─────────────────────────────────────
    section("2. Platform admin CRUD (owner-only)")

    for role in ("campaign_admin", "finance_admin", "verification_admin"):
        r = client.post(f"{BASE}/platform-admins", headers=OA, json={
            "username": f"test_{role}", "password": PASSWD,
            "full_name": f"Test {role}", "email": f"{role}@test.local",
            "role": role,
        })
        check(f"create {role}", ok(r), f"{r.status_code} {j(r)}")
        if ok(r):
            created_admins[role] = j(r).get("id")

    # Create a full-access admin too
    r = client.post(f"{BASE}/platform-admins", headers=OA, json={
        "username": "test_full", "password": PASSWD,
        "full_name": "Test Full", "email": "full@test.local",
        "role": "full",
    })
    check("create full admin", ok(r), f"{r.status_code}")
    if ok(r):
        created_admins["full"] = j(r).get("id")

    # Duplicate username rejected
    r = client.post(f"{BASE}/platform-admins", headers=OA, json={
        "username": "test_full", "password": PASSWD,
        "full_name": "Dup Full", "role": "full",
    })
    check("duplicate username rejected", r.status_code == 409, f"{r.status_code}")

    # Short password rejected
    r = client.post(f"{BASE}/platform-admins", headers=OA, json={
        "username": "shortpw", "password": "123", "full_name": "Short",
        "role": "full",
    })
    check("short password rejected", r.status_code == 400, f"{r.status_code}")

    # Invalid role rejected
    r = client.post(f"{BASE}/platform-admins", headers=OA, json={
        "username": "badrole", "password": PASSWD, "full_name": "Bad",
        "role": "division_admin",
    })
    check("invalid role rejected", r.status_code == 400, f"{r.status_code}")

    # List admins shows all created
    r = client.get(f"{BASE}/platform-admins", headers=OA)
    check("list admins succeeds", ok(r), f"{r.status_code}")
    admins = j(r).get("items", [])
    check("correct admin count", len(admins) == 6, f"expected 6, got {len(admins)}")
    owner_count = sum(1 for a in admins if a["role"] == "owner")
    check("exactly 1 owner", owner_count == 1)

    # Non-owner cannot access admin endpoints
    campaign_token = None
    r = client.post("/api/v1/auth/superadmin/login",
                     json={"username": "test_campaign_admin", "password": PASSWD})
    if ok(r):
        campaign_token = j(r).get("access_token")
    if campaign_token:
        CA = {"Authorization": f"Bearer {campaign_token}"}
        r = client.get(f"{BASE}/platform-admins", headers=CA)
        check("campaign_admin 403 on platform-admins", r.status_code == 403, f"{r.status_code}")

    # Owner update admin
    r = client.put(f"{BASE}/platform-admins/{created_admins['full']}", headers=OA, json={
        "full_name": "Updated Full Admin", "status": "suspended",
    })
    check("update admin status+name", ok(r), f"{r.status_code}")
    # Verify the update
    r = client.get(f"{BASE}/platform-admins", headers=OA)
    updated = [a for a in j(r)["items"] if a["id"] == created_admins["full"]]
    check("status updated to suspended", updated and updated[0]["status"] == "suspended")

    # Restore for later test
    client.put(f"{BASE}/platform-admins/{created_admins['full']}", headers=OA,
               json={"status": "active"})

    # Cannot edit owner
    owner_id = owner_user["id"]
    r = client.put(f"{BASE}/platform-admins/{owner_id}", headers=OA,
                   json={"full_name": "Hacked"})
    check("cannot edit owner account", r.status_code == 403, f"{r.status_code}")

    # ── 3. Role login returns correct role ────────────────────────────────────
    section("3. Role-based login and token claims")
    role_tokens = {}
    for role in ("campaign_admin", "finance_admin", "verification_admin", "full"):
        r = client.post("/api/v1/auth/superadmin/login",
                         json={"username": f"test_{role}", "password": PASSWD})
        check(f"{role} login succeeds", ok(r), f"{r.status_code}")
        user = j(r).get("user", {})
        check(f"  role in payload = {role}", user.get("role") == role, user)
        role_tokens[role] = j(r).get("access_token")

    # ── 4. Path gating ────────────────────────────────────────────────────────
    section("4. Path gating (specialised roles restricted to own area)")
    ROLE_TOKENS = {r: {"Authorization": f"Bearer {t}"}
                   for r, t in role_tokens.items()}

    # campaign_admin can access /campaigns but not /gratification, /pob, /platform-admins
    r = client.get(f"{BASE}/campaigns", headers=ROLE_TOKENS["campaign_admin"])
    check("campaign_admin -> /campaigns OK", ok(r), f"{r.status_code}")
    r = client.get(f"{BASE}/campaigns/roi", headers=ROLE_TOKENS["campaign_admin"])
    check("campaign_admin -> /campaigns/roi OK", ok(r), f"{r.status_code}")
    if ok(r):
        roi = j(r)
        check("roi shape has summary + campaigns",
              "summary" in roi and "campaigns" in roi and "top_winning" in roi,
              list(roi.keys()))
        check("roi summary counters present",
              all(k in roi["summary"] for k in ("campaigns", "profitable", "loss_making")),
              roi["summary"])
    r = client.get(f"{BASE}/gratification", headers=ROLE_TOKENS["campaign_admin"])
    check("campaign_admin -> /gratification BLOCKED", r.status_code == 403, f"{r.status_code}")
    r = client.get(f"{BASE}/pob", headers=ROLE_TOKENS["campaign_admin"])
    check("campaign_admin -> /pob BLOCKED", r.status_code == 403, f"{r.status_code}")
    r = client.get(f"{BASE}/platform-admins", headers=ROLE_TOKENS["campaign_admin"])
    check("campaign_admin -> /platform-admins BLOCKED", r.status_code == 403, f"{r.status_code}")

    # finance_admin can access /gratification, /analytics, but not /campaigns, /pob
    r = client.get(f"{BASE}/gratification", headers=ROLE_TOKENS["finance_admin"])
    check("finance_admin -> /gratification OK", ok(r), f"{r.status_code}")
    r = client.get(f"{BASE}/analytics", headers=ROLE_TOKENS["finance_admin"])
    check("finance_admin -> /analytics OK", ok(r), f"{r.status_code}")
    r = client.get(f"{BASE}/finance", headers=ROLE_TOKENS["finance_admin"])
    check("finance_admin -> /finance OK", ok(r), f"{r.status_code}")
    r = client.get(f"{BASE}/campaigns", headers=ROLE_TOKENS["finance_admin"])
    check("finance_admin -> /campaigns BLOCKED", r.status_code == 403, f"{r.status_code}")
    r = client.get(f"{BASE}/campaigns/roi", headers=ROLE_TOKENS["finance_admin"])
    check("finance_admin -> /campaigns/roi BLOCKED", r.status_code == 403, f"{r.status_code}")
    r = client.get(f"{BASE}/campaigns/roi", headers=ROLE_TOKENS["verification_admin"])
    check("verification_admin -> /campaigns/roi BLOCKED", r.status_code == 403, f"{r.status_code}")
    r = client.get(f"{BASE}/pob", headers=ROLE_TOKENS["finance_admin"])
    check("finance_admin -> /pob BLOCKED", r.status_code == 403, f"{r.status_code}")

    # verification_admin can access /pob, /analytics, but not /gratification, /campaigns
    r = client.get(f"{BASE}/pob", headers=ROLE_TOKENS["verification_admin"])
    check("verification_admin -> /pob OK", ok(r), f"{r.status_code}")
    r = client.get(f"{BASE}/analytics", headers=ROLE_TOKENS["verification_admin"])
    check("verification_admin -> /analytics OK", ok(r), f"{r.status_code}")
    r = client.get(f"{BASE}/gratification", headers=ROLE_TOKENS["verification_admin"])
    check("verification_admin -> /gratification BLOCKED", r.status_code == 403, f"{r.status_code}")
    r = client.get(f"{BASE}/campaigns", headers=ROLE_TOKENS["verification_admin"])
    check("verification_admin -> /campaigns BLOCKED", r.status_code == 403, f"{r.status_code}")

    # campaign_admin cannot read the finance overview
    r = client.get(f"{BASE}/finance", headers=ROLE_TOKENS["campaign_admin"])
    check("campaign_admin -> /finance BLOCKED", r.status_code == 403, f"{r.status_code}")
    r = client.get(f"{BASE}/finance", headers=ROLE_TOKENS["verification_admin"])
    check("verification_admin -> /finance BLOCKED", r.status_code == 403, f"{r.status_code}")

    # Owner and full can access everything
    for rname, hdr in [("owner", OA), ("full", ROLE_TOKENS["full"])]:
        r = client.get(f"{BASE}/finance", headers=hdr)
        check(f"{rname} -> /finance OK", ok(r), f"{r.status_code}")
        r = client.get(f"{BASE}/campaigns/roi", headers=hdr)
        check(f"{rname} -> /campaigns/roi OK", ok(r), f"{r.status_code}")
        r = client.get(f"{BASE}/campaigns", headers=hdr)
        check(f"{rname} -> /campaigns OK", ok(r), f"{r.status_code}")
        r = client.get(f"{BASE}/gratification", headers=hdr)
        check(f"{rname} -> /gratification OK", ok(r), f"{r.status_code}")
        r = client.get(f"{BASE}/pob", headers=hdr)
        check(f"{rname} -> /pob OK", ok(r), f"{r.status_code}")
        r = client.get(f"{BASE}/platform-admins", headers=hdr)
        check(f"{rname} -> /platform-admins OK",
              ok(r) if rname == "owner" else r.status_code == 403,
              f"{r.status_code}")

    # ── 5. Role gate (require_sa_roles) on cross-division proxies ──────────────
    section("5. Role gate on cross-division proxy endpoints")

    # Without a valid division+gratification, the proxies return 404 or 500
    # (tenant not found). We just check role-gating here.
    r = client.post(f"{BASE}/divisions/999999/gratification/999999/approve",
                    headers=ROLE_TOKENS["campaign_admin"])
    check("campaign_admin -> gratification approve BLOCKED", r.status_code == 403, f"{r.status_code}")
    r = client.post(f"{BASE}/divisions/999999/verification/999999/approve",
                    headers=ROLE_TOKENS["finance_admin"])
    check("finance_admin -> verification approve BLOCKED", r.status_code == 403, f"{r.status_code}")
    r = client.post(f"{BASE}/divisions/999999/gratification/999999/approve",
                    headers=ROLE_TOKENS["verification_admin"])
    check("verification_admin -> gratification approve BLOCKED", r.status_code == 403, f"{r.status_code}")
    r = client.post(f"{BASE}/divisions/999999/verification/999999/approve",
                    headers=ROLE_TOKENS["verification_admin"])
    check("verification_admin -> verification approve allowed (404 not 403)",
          r.status_code in (404, 500), f"{r.status_code}")

    # finance_admin -> gratification approve allowed (will 404 for bad division)
    r = client.post(f"{BASE}/divisions/999999/gratification/999999/approve",
                    headers=ROLE_TOKENS["finance_admin"])
    check("finance_admin -> gratification approve allowed (404 not 403)",
          r.status_code in (404, 500), f"{r.status_code}")

    # ── 6. Summary ─────────────────────────────────────────────────────────────
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
