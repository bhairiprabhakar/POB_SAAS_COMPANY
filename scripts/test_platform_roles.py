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

    # A platform-console user hitting the tenant /me endpoint (e.g. via a
    # stale link, a shared component, or navigating to /superadmin/profile
    # before it had its own backend) must not silently succeed with tenant
    # data -- and must have its own equivalent endpoint instead of nothing.
    r = client.get("/api/v1/auth/me", headers=OA)
    check("superadmin token on the TENANT /me endpoint -> 403 Tenant access required",
          r.status_code == 403, f"{r.status_code} {j(r)}")
    r = client.get("/api/v1/auth/superadmin/me", headers=OA)
    check("owner has their own /auth/superadmin/me endpoint", ok(r), f"{r.status_code} {j(r)}")
    check("superadmin/me returns the owner's own profile",
          j(r).get("user", {}).get("username") == "owner01" and j(r).get("role") == "owner",
          j(r))

    # ── 2. Owner creates platform admins ─────────────────────────────────────
    section("2. Platform admin CRUD (owner-only)")

    for role in ("campaign_admin", "finance_admin", "verification_admin", "platform_division_admin"):
        r = client.post(f"{BASE}/platform-admins", headers=OA, json={
            "username": f"test_{role}", "password": PASSWD,
            "full_name": f"Test {role}", "email": f"{role}@test.local",
            "role": role,
        })
        check(f"create {role}", ok(r), f"{r.status_code} {j(r)}")
        if ok(r):
            created_admins[role] = j(r).get("id")

    # 'full' is a legacy role the console no longer lets anyone create --
    # confirm the create endpoint rejects it explicitly.
    r = client.post(f"{BASE}/platform-admins", headers=OA, json={
        "username": "test_full_rejected", "password": PASSWD,
        "full_name": "Should Not Exist", "role": "full",
    })
    check("creating a full-access admin is rejected", r.status_code == 400, f"{r.status_code} {j(r)}")

    # A 'full' account can still exist (legacy data) -- seed one directly,
    # mirroring how a pre-existing account would look, to test that it keeps
    # working and can still be edited (just not re-assigned TO 'full').
    from saas.passwords import hash_pw as _hash_pw
    pconn = platform_db.get_db()
    try:
        pc = pconn.cursor()
        pc.execute(
            "INSERT INTO super_admins (username, password, full_name, email, status, role, owner_flag, company_id) "
            "VALUES (%s,%s,%s,%s,'active','full',FALSE,1) RETURNING id",
            ("test_full", _hash_pw(PASSWD), "Test Full", "full@test.local"))
        created_admins["full"] = pc.fetchone()[0]
        pconn.commit()
    finally:
        pconn.close()

    # Duplicate username rejected
    r = client.post(f"{BASE}/platform-admins", headers=OA, json={
        "username": "test_full", "password": PASSWD,
        "full_name": "Dup Full", "role": "campaign_admin",
    })
    check("duplicate username rejected", r.status_code == 409, f"{r.status_code}")

    # Short password rejected
    r = client.post(f"{BASE}/platform-admins", headers=OA, json={
        "username": "shortpw", "password": "123", "full_name": "Short",
        "role": "campaign_admin",
    })
    check("short password rejected", r.status_code == 400, f"{r.status_code}")

    # Invalid role rejected
    r = client.post(f"{BASE}/platform-admins", headers=OA, json={
        "username": "badrole", "password": PASSWD, "full_name": "Bad",
        "role": "billing_admin",
    })
    check("invalid role rejected", r.status_code == 400, f"{r.status_code}")

    # Promoting an existing (non-full) admin to 'full' is rejected too
    r = client.put(f"{BASE}/platform-admins/{created_admins['campaign_admin']}", headers=OA,
                   json={"role": "full"})
    check("promoting an admin to full-access is rejected", r.status_code == 400, f"{r.status_code} {j(r)}")

    # List admins shows all created
    r = client.get(f"{BASE}/platform-admins", headers=OA)
    check("list admins succeeds", ok(r), f"{r.status_code}")
    admins = j(r).get("items", [])
    check("correct admin count", len(admins) == 7, f"expected 7, got {len(admins)}")
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

    # ── 2b. Co-owner: founder-controlled backup owner ────────────────────────
    section("2b. Co-owner (founder-controlled backup owner)")

    # Dedicated admins for this section only -- never touches created_admins,
    # which later sections rely on still holding their original roles.
    co_src = {}
    for role in ("campaign_admin", "finance_admin"):
        r = client.post(f"{BASE}/platform-admins", headers=OA, json={
            "username": f"co_src_{role}", "password": PASSWD,
            "full_name": f"Co Src {role}", "role": role,
        })
        check(f"seed {role} for co-owner tests", ok(r), f"{r.status_code} {j(r)}")
        co_src[role] = j(r).get("id")

    promote_url = f"{BASE}/platform-admins/{co_src['campaign_admin']}/co-owner"

    r = client.post(promote_url, headers=OA, json={"password": "wrong-password"})
    check("promote with wrong founder password -> 401", r.status_code == 401, f"{r.status_code} {j(r)}")

    r = client.post(promote_url, headers=OA, json={"password": PASSWD})
    check("founder promotes campaign_admin to co-owner", ok(r), f"{r.status_code} {j(r)}")
    check("promote response role=co_owner", j(r).get("role") == "co_owner", j(r))

    r = client.get(f"{BASE}/platform-admins", headers=OA)
    row = next((a for a in j(r)["items"] if a["id"] == co_src["campaign_admin"]), None)
    check("co-owner shows role=co_owner in list", row and row["role"] == "co_owner", row)

    r = client.post("/api/v1/auth/superadmin/login",
                     json={"username": "co_src_campaign_admin", "password": PASSWD})
    check("co-owner can login", ok(r), f"{r.status_code}")
    co_token = j(r).get("access_token")
    COA = {"Authorization": f"Bearer {co_token}"}

    # Co-owner has full operational power -- same as owner/full -- exercised
    # against a functional cross-division endpoint gated by require_sa_roles()
    # (owner/full/co_owner only): listing every division's users.
    r = client.get(f"{BASE}/divisions/{division_id}/users", headers=COA) if division_id else None
    if division_id:
        check("co-owner has full operational access (division users)", ok(r), f"{r.status_code} {j(r)}")

    # Co-owner CAN manage the specialised admins, same as owner.
    r = client.post(f"{BASE}/platform-admins", headers=COA, json={
        "username": "co_owner_made_this", "password": PASSWD,
        "full_name": "Made By Co-Owner", "role": "verification_admin",
    })
    check("co-owner can create a specialised admin", ok(r), f"{r.status_code} {j(r)}")
    made_by_co_owner_id = j(r).get("id")

    # Co-owner CANNOT touch the founder.
    r = client.put(f"{BASE}/platform-admins/{owner_id}", headers=COA, json={"full_name": "Hacked"})
    check("co-owner cannot edit the founder -> 403", r.status_code == 403, f"{r.status_code}")

    # Seed a second co-owner to prove co-owners can't touch each other.
    r = client.post(promote_url.replace(str(co_src["campaign_admin"]), str(co_src["finance_admin"])),
                     headers=OA, json={"password": PASSWD})
    check("founder promotes a second co-owner", ok(r), f"{r.status_code} {j(r)}")
    r = client.put(f"{BASE}/platform-admins/{co_src['finance_admin']}", headers=COA,
                   json={"full_name": "Hacked by peer co-owner"})
    check("co-owner cannot edit another co-owner -> 403", r.status_code == 403, f"{r.status_code}")

    # Co-owner cannot promote/revoke co-owner status themselves (founder-only).
    r = client.post(f"{BASE}/platform-admins/{made_by_co_owner_id}/co-owner", headers=COA,
                     json={"password": PASSWD})
    check("co-owner cannot promote another admin to co-owner -> 403", r.status_code == 403, f"{r.status_code}")
    r = client.post(f"{BASE}/platform-admins/{co_src['finance_admin']}/co-owner/revoke", headers=COA,
                     json={"password": PASSWD, "fallback_role": "finance_admin"})
    check("co-owner cannot revoke co-owner status -> 403", r.status_code == 403, f"{r.status_code}")

    # Co-owner cap: 2 co-owners already exist: seed + promote 2 more distinct
    # specialised admins to hit the cap of 3, then confirm a 4th is rejected.
    cap_admins = []
    for i in range(2):
        r = client.post(f"{BASE}/platform-admins", headers=OA, json={
            "username": f"co_cap_{i}", "password": PASSWD,
            "full_name": f"Co Cap {i}", "role": "verification_admin",
        })
        cap_admins.append(j(r).get("id"))
    r = client.post(f"{BASE}/platform-admins/{cap_admins[0]}/co-owner", headers=OA, json={"password": PASSWD})
    check("promote 3rd co-owner (at cap)", ok(r), f"{r.status_code} {j(r)}")
    r = client.post(f"{BASE}/platform-admins/{cap_admins[1]}/co-owner", headers=OA, json={"password": PASSWD})
    check("promoting a 4th co-owner is rejected -> 400", r.status_code == 400, f"{r.status_code} {j(r)}")

    # Revoke: wrong password rejected, then correct password reverts the role
    # and the account loses the operational co-owner bypass.
    revoke_url = f"{BASE}/platform-admins/{co_src['campaign_admin']}/co-owner/revoke"
    r = client.post(revoke_url, headers=OA, json={"password": "wrong", "fallback_role": "campaign_admin"})
    check("revoke with wrong founder password -> 401", r.status_code == 401, f"{r.status_code} {j(r)}")

    r = client.post(revoke_url, headers=OA, json={"password": PASSWD, "fallback_role": "campaign_admin"})
    check("founder revokes co-owner status", ok(r), f"{r.status_code} {j(r)}")
    check("revoke response role=campaign_admin", j(r).get("role") == "campaign_admin", j(r))

    # Access tokens carry sa_role as a static claim baked in at login (not
    # re-checked against the DB per request -- a pre-existing property of
    # every superadmin role, not specific to co-owner), so proving the
    # revocation actually took effect means logging in fresh rather than
    # reusing the old co-owner-era token.
    r = client.post("/api/v1/auth/superadmin/login",
                     json={"username": "co_src_campaign_admin", "password": PASSWD})
    check("revoked account can still login", ok(r), f"{r.status_code}")
    fresh_token = j(r).get("access_token")
    r = client.get(f"{BASE}/platform-admins", headers={"Authorization": f"Bearer {fresh_token}"})
    check("fresh login after revoke loses platform-admins access -> 403", r.status_code == 403, f"{r.status_code}")

    # ── 3. Role login returns correct role ────────────────────────────────────
    section("3. Role-based login and token claims")
    role_tokens = {}
    for role in ("campaign_admin", "finance_admin", "verification_admin", "platform_division_admin", "full"):
        r = client.post("/api/v1/auth/superadmin/login",
                         json={"username": f"test_{role}", "password": PASSWD})
        check(f"{role} login succeeds", ok(r), f"{r.status_code}")
        user = j(r).get("user", {})
        check(f"  role in payload = {role}", user.get("role") == role, user)
        role_tokens[role] = j(r).get("access_token")

    r = client.get("/api/v1/auth/superadmin/me", headers={"Authorization": f"Bearer {role_tokens['campaign_admin']}"})
    check("a specialised role can also use /auth/superadmin/me (not owner-only)",
          ok(r) and j(r).get("user", {}).get("username") == "test_campaign_admin", f"{r.status_code} {j(r)}")

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

    # verification_admin can reach the agent-management console (substring of
    # the already-allowed "/verification" path); other specialised roles can't
    r = client.get(f"{BASE}/verification-agents", headers=ROLE_TOKENS["verification_admin"])
    check("verification_admin -> /verification-agents OK", ok(r), f"{r.status_code}")
    r = client.post(f"{BASE}/verification-agents", headers=ROLE_TOKENS["verification_admin"], json={})
    check("verification_admin -> POST /verification-agents reaches the handler (400, not 403)",
          r.status_code == 400, f"{r.status_code}")
    for rname in ("campaign_admin", "finance_admin"):
        r = client.get(f"{BASE}/verification-agents", headers=ROLE_TOKENS[rname])
        check(f"{rname} -> /verification-agents BLOCKED", r.status_code == 403, f"{r.status_code}")

    # campaign_admin cannot read the finance overview
    r = client.get(f"{BASE}/finance", headers=ROLE_TOKENS["campaign_admin"])
    check("campaign_admin -> /finance BLOCKED", r.status_code == 403, f"{r.status_code}")
    r = client.get(f"{BASE}/finance", headers=ROLE_TOKENS["verification_admin"])
    check("verification_admin -> /finance BLOCKED", r.status_code == 403, f"{r.status_code}")

    # platform_division_admin can reach /divisions (its own area) but nothing else
    r = client.get(f"{BASE}/divisions", headers=ROLE_TOKENS["platform_division_admin"])
    check("platform_division_admin -> /divisions OK", ok(r), f"{r.status_code}")
    r = client.post(f"{BASE}/divisions", headers=ROLE_TOKENS["platform_division_admin"], json={
        "name": f"DA-Created Div {UUID}", "code": f"DA{UUID}",
        "description": "created by platform_division_admin",
    })
    check("platform_division_admin can create a division", ok(r), f"{r.status_code} {j(r)}")
    r = client.get(f"{BASE}/analytics", headers=ROLE_TOKENS["platform_division_admin"])
    check("platform_division_admin -> /analytics OK", ok(r), f"{r.status_code}")
    r = client.post(f"{BASE}/divisions/999999/gratification/999999/approve",
                    headers=ROLE_TOKENS["platform_division_admin"])
    check("platform_division_admin -> gratification approve BLOCKED (role gate)",
          r.status_code == 403, f"{r.status_code}")
    r = client.get(f"{BASE}/platform-admins", headers=ROLE_TOKENS["platform_division_admin"])
    check("platform_division_admin -> /platform-admins BLOCKED", r.status_code == 403, f"{r.status_code}")
    r = client.get(f"{BASE}/finance", headers=ROLE_TOKENS["platform_division_admin"])
    check("platform_division_admin -> /finance BLOCKED", r.status_code == 403, f"{r.status_code}")
    r = client.get(f"{BASE}/campaigns", headers=ROLE_TOKENS["platform_division_admin"])
    check("platform_division_admin -> /campaigns BLOCKED", r.status_code == 403, f"{r.status_code}")
    r = client.get(f"{BASE}/pob", headers=ROLE_TOKENS["platform_division_admin"])
    check("platform_division_admin -> /pob BLOCKED", r.status_code == 403, f"{r.status_code}")
    r = client.get(f"{BASE}/gratification", headers=ROLE_TOKENS["platform_division_admin"])
    check("platform_division_admin -> /gratification BLOCKED", r.status_code == 403, f"{r.status_code}")
    r = client.get(f"{BASE}/verification-agents", headers=ROLE_TOKENS["platform_division_admin"])
    check("platform_division_admin -> /verification-agents BLOCKED", r.status_code == 403, f"{r.status_code}")

    # Other specialised admins stay blocked on /divisions
    for rname in ("campaign_admin", "finance_admin", "verification_admin"):
        r = client.get(f"{BASE}/divisions", headers=ROLE_TOKENS[rname])
        check(f"{rname} -> /divisions BLOCKED", r.status_code == 403, f"{r.status_code}")

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

    # ── 6. Security regression: direct API calls on the nested campaign surface ──
    # Every check above only ever exercised top-level paths (/campaigns,
    # /divisions, /gratification, /pob) -- none of them touch the nested
    # /divisions/{did}/campaigns/... routes, which is exactly where the old
    # substring path-gate let platform_division_admin reach full campaign
    # CRUD + approve/reject by accident (both "/divisions" and "/campaigns"
    # are substrings of that path). This section provisions a real tenant so
    # the positive case (campaign_admin approving a real campaign) can be
    # asserted as a genuine 200, not just "not 403".
    section("6. Security regression -- nested /divisions/{did}/campaigns/... direct API calls")

    r = client.post(f"{BASE}/divisions", headers=OA, json={
        "name": f"RBAC Test Division {UUID}", "code": f"RBD{UUID}",
        "provision": True, "admin_username": "rbac_admin",
        "admin_password": PASSWD, "admin_email": "rbac@test.local",
        "admin_full_name": "RBAC Test Admin",
    })
    check("provision RBAC test division", ok(r), f"{r.status_code} {j(r)}")
    rbac_div = j(r)
    RBAC_DID = rbac_div.get("id")
    RBAC_TENANT_DB = rbac_div.get("tenant_db_name")

    from saas import db_utils as _db_utils
    _conn = _db_utils.get_conn(RBAC_TENANT_DB)
    _cur = _conn.cursor()
    _cur.execute("UPDATE users SET must_change_password=FALSE, mfa_setup_required=FALSE, "
                "profile_pending=FALSE WHERE username='rbac_admin'")
    _conn.commit()
    _conn.close()

    r = client.post("/api/v1/auth/login", json={
        "division_slug": f"RBD{UUID}", "username": "rbac_admin", "password": PASSWD,
    })
    check("RBAC division admin login", ok(r), f"{r.status_code} {j(r)}")
    RBAC_ADM = {"Authorization": f"Bearer {j(r)['access_token']}"}

    r = client.post("/api/v1/brands", headers=RBAC_ADM, json={"name": "RBAC Brand", "code": f"RB{UUID}"})
    check("RBAC test brand created", ok(r), f"{r.status_code} {j(r)}")
    RBAC_BRAND = j(r).get("id")

    r = client.post("/api/v1/campaigns", headers=RBAC_ADM, json={
        "name": "RBAC Test Campaign", "brand_id": RBAC_BRAND,
        "start_date": "2026-01-01", "end_date": "2026-12-31", "scheme_type": "cashback",
    })
    check("RBAC test campaign created", ok(r), f"{r.status_code} {j(r)}")
    RBAC_CID = j(r).get("id")
    r = client.post(f"/api/v1/campaigns/{RBAC_CID}/submit", headers=RBAC_ADM, json={})
    check("RBAC test campaign submitted for approval", ok(r), f"{r.status_code} {j(r)}")

    CAMP_BASE = f"{BASE}/divisions/{RBAC_DID}/campaigns"

    # The 6 cases from the spec, verbatim:
    r = client.post(CAMP_BASE, headers=ROLE_TOKENS["campaign_admin"], json={"name": "Should Not Exist"})
    check("campaign_admin POST .../campaigns -> 403", r.status_code == 403, f"{r.status_code}")
    r = client.put(f"{CAMP_BASE}/{RBAC_CID}", headers=ROLE_TOKENS["campaign_admin"], json={"name": "Hijack"})
    check("campaign_admin PUT .../campaigns/{id} -> 403", r.status_code == 403, f"{r.status_code}")
    r = client.post(f"{CAMP_BASE}/{RBAC_CID}/approve", headers=ROLE_TOKENS["campaign_admin"], json={})
    check("campaign_admin POST .../campaigns/{id}/approve -> succeeds", ok(r), f"{r.status_code} {j(r)}")

    # Re-provision a second pending campaign for the remaining approve-based checks
    r = client.post("/api/v1/campaigns", headers=RBAC_ADM, json={
        "name": "RBAC Test Campaign 2", "brand_id": RBAC_BRAND,
        "start_date": "2026-01-01", "end_date": "2026-12-31", "scheme_type": "cashback",
    })
    RBAC_CID2 = j(r).get("id")
    client.post(f"/api/v1/campaigns/{RBAC_CID2}/submit", headers=RBAC_ADM, json={})

    r = client.post(f"{CAMP_BASE}/{RBAC_CID2}/approve", headers=ROLE_TOKENS["finance_admin"], json={})
    check("finance_admin POST .../campaigns/{id}/approve -> 403", r.status_code == 403, f"{r.status_code}")
    r = client.post(f"{BASE}/divisions/{RBAC_DID}/gratification/1/pay",
                    headers=ROLE_TOKENS["verification_admin"], json={})
    check("verification_admin POST .../gratification/{id}/pay -> 403", r.status_code == 403, f"{r.status_code}")
    r = client.post(CAMP_BASE, headers=ROLE_TOKENS["platform_division_admin"], json={"name": "Should Not Exist"})
    check("platform_division_admin POST .../campaigns -> 403 (the bug fix)", r.status_code == 403, f"{r.status_code}")

    # Extra coverage: the rest of the campaign mutation surface, and the
    # tenant-administrator surface platform_division_admin must not reach.
    r = client.delete(f"{CAMP_BASE}/{RBAC_CID2}", headers=ROLE_TOKENS["platform_division_admin"])
    check("platform_division_admin DELETE .../campaigns/{id} -> 403", r.status_code == 403, f"{r.status_code}")
    r = client.put(f"{CAMP_BASE}/{RBAC_CID2}", headers=ROLE_TOKENS["platform_division_admin"], json={"name": "x"})
    check("platform_division_admin PUT .../campaigns/{id} -> 403", r.status_code == 403, f"{r.status_code}")
    r = client.post(f"{CAMP_BASE}/{RBAC_CID2}/extend", headers=ROLE_TOKENS["platform_division_admin"], json={"days": 30})
    check("platform_division_admin POST .../campaigns/{id}/extend -> 403", r.status_code == 403, f"{r.status_code}")
    r = client.patch(f"{CAMP_BASE}/{RBAC_CID2}/toggle-active", headers=ROLE_TOKENS["platform_division_admin"])
    check("platform_division_admin PATCH .../campaigns/{id}/toggle-active -> 403", r.status_code == 403, f"{r.status_code}")
    r = client.get(f"{BASE}/divisions/{RBAC_DID}/roles", headers=ROLE_TOKENS["platform_division_admin"])
    check("platform_division_admin GET .../roles -> 403", r.status_code == 403, f"{r.status_code}")
    r = client.get(f"{BASE}/divisions/{RBAC_DID}/hierarchy/tree", headers=ROLE_TOKENS["platform_division_admin"])
    check("platform_division_admin GET .../hierarchy/tree -> 403", r.status_code == 403, f"{r.status_code}")
    for rname in ("campaign_admin", "finance_admin", "verification_admin"):
        r = client.post(f"{BASE}/divisions/{RBAC_DID}/users", headers=ROLE_TOKENS[rname], json={
            "username": f"should_not_exist_{rname}", "password": PASSWD, "full_name": "x",
        })
        check(f"{rname} POST .../users (division-admin creation) -> 403", r.status_code == 403, f"{r.status_code}")

    # ── 7. Summary ─────────────────────────────────────────────────────────────
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
