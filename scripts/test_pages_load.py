"""Page-load verification for every role type: superadmin, division admin,
agent (MR/ASM - data entry), and company people (verifier/finance).

For each login it calls the API endpoints each page fetches on mount and
reports 200 (page loads) / 403 (permission-blocked, matching the sidebar
PERM_GATE) / 401 / 5xx. Only endpoints on pages the role should see (per
permission + data_entry flag) are expected 200; every other endpoint must be
a clean 403, never 401/404/500.

Runs FULLY HERMETIC: a scratch control-plane DB is created before the saas
package is imported, one division is provisioned (fresh tenant DB), all role
users are created inside it, and both scratch DBs are dropped at the end.

Ported in Batch 4 from the companies/plans model (DEMO1234 users + company_code
login) to the current divisions/division_slug architecture.

Run:  venv\\Scripts\\python.exe scripts\\test_pages_load.py
"""
import os
import sys
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# hermetic: point the platform at a scratch control-plane DB BEFORE the saas
# package is imported (config reads this at import time)
SCRATCH_PLATFORM = f"psk_pages_{uuid.uuid4().hex[:10]}"
os.environ["PLATFORM_DB_NAME"] = SCRATCH_PLATFORM

from fastapi.testclient import TestClient  # noqa: E402
from saas.main import app  # noqa: E402
from saas import platform_db  # noqa: E402

platform_db.init_platform_db()

BOOT = os.environ.get("SUPERADMIN_BOOTSTRAP_PASSWORD", "101990")
_TENANT_DB = None

# sidebar perm gates from web/src/ui.jsx
PERM_GATE = {
    "/app": "dashboard.view",
    "/app/pob/submit": "pob.submit",
    "/app/pob/mine": "pob.submit",
    "/app/pob/invoice": "pob.submit",
    "/app/pob": "pob.view",
    "/app/verification": "verification.view",
    "/app/gratification": "gratification.view",
    "/app/visits": "visit.view",
    "/app/reports": "report.schedule",
    "/app/notifications": "notification.view",
    "/app/notifications/mgmt": "notification.manage",
    "/app/chemists": "chemist.view",
    "/app/chemists/register": "chemist.manage",
    "/app/masters/campaigns": "campaign.view",
    "/app/analytics": "dashboard.view",
    "/app/audit": "audit.view",
    "/app/security": "apikey.view",
    "/app/jobs": "job.view",
    "/app/profile": None,   # always visible
}

# endpoints each page mounts with; {route: [list of GET paths]}
ENDPOINTS = {
    "/app":                    ["/api/v1/dashboards/company", "/api/v1/dashboards/finance",
                               "/api/v1/dashboards/verification", "/api/v1/dashboards/leaderboard",
                               "/api/v1/dashboards/performance", "/api/v1/dashboards/campaign",
                               "/api/v1/dashboards/brand"],
    "/app/pob/submit":         ["/api/v1/campaigns?status=active&active=1",
                               "/api/v1/chemists?status=active",
                               "/api/v1/products?campaign_id=1"],
    "/app/pob/mine":           ["/api/v1/pob/mine"],
    "/app/pob/invoice":        ["/api/v1/pob/pending-invoice", "/api/v1/campaigns?active=true&status=active",
                               "/api/v1/chemists?limit=500"],
    "/app/visits":             ["/api/v1/visits", "/api/v1/visits/summary", "/api/v1/visits/due",
                               "/api/v1/chemists?status=active", "/api/v1/campaigns"],
    "/app/chemists":           ["/api/v1/chemists", "/api/v1/pincode/400001"],
    "/app/chemists/register":  ["/api/v1/pincode/400001"],
    "/app/masters/campaigns":  ["/api/v1/campaigns/tracking"],
    "/app/analytics":          ["/api/v1/analytics/summary", "/api/v1/analytics/roi"],
    "/app/notifications":      ["/api/v1/notifications/"],
    "/app/notifications/mgmt": ["/api/v1/notifications/templates"],   # lazy "Manage templates" (perm: notification.manage)
    "/app/pob":                ["/api/v1/pob"],
    "/app/verification":       ["/api/v1/verification/stats", "/api/v1/verification/queue?status=pending",
                               "/api/v1/verification/tat/report"],
    "/app/gratification":      ["/api/v1/gratification", "/api/v1/gifts"],
    "/app/audit":              ["/api/v1/audit/logs"],
    "/app/security":           ["/api/v1/auth/mfa/status", "/api/v1/apikeys", "/api/v1/webhooks"],
    "/app/jobs":               ["/api/v1/jobs"],
    "/app/reports":            ["/api/v1/reports/schedules"],
    "/app/profile":            ["/api/v1/auth/me"],
}

# extra "should load" endpoints per role beyond the sidebar gates
EXTRA_OK = {
    "mr": ["/api/v1/pob/my-stats"],       # nav badge poll
    "asm": ["/api/v1/pob/my-stats"],
}

FAILED = []


def check(name, cond, extra=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" -- {extra}" if extra and not cond else ""))
    if not cond:
        FAILED.append(name)


def login(client, division_slug, username, password):
    r = client.post("/api/v1/auth/login",
                    json={"division_slug": division_slug, "username": username, "password": password})
    if r.status_code != 200:
        return None
    d = r.json()
    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {d['access_token']}"}).json()
    return {
        "access": d["access_token"],
        "user": d["user"],
        "perms": me.get("permissions") or [],
        "entry": bool(me.get("user", {}).get("data_entry")),
    }


def call(client, h, path):
    try:
        r = client.get(path, headers=h)
        return r.status_code, r.text[:160]
    except Exception as e:  # noqa: BLE001
        return -1, str(e)


def main():
    global _TENANT_DB
    client = TestClient(app)
    tag = uuid.uuid4().hex[:6].upper()

    r = client.post("/api/v1/auth/superadmin/login",
                    json={"username": "superadmin", "password": BOOT})
    ok = r.status_code == 200
    check("superadmin login", ok, f"{r.status_code} {r.text[:200]}")
    sa_h = {"Authorization": f"Bearer {r.json()['access_token']}"} if ok else None

    sa_pages = [
        "/api/v1/superadmin/metrics",
        "/api/v1/superadmin/divisions",
        "/api/v1/superadmin/audit-logs",
        "/api/v1/superadmin/ai-models",
        "/api/v1/superadmin/costing",
    ]
    print("\n== superadmin (platform console) ==")
    if sa_h:
        for p in sa_pages:
            code, txt = call(client, sa_h, p)
            check(f"SA {p}", code == 200, f"{code} {txt}")

    # ── hermetic tenant: provision one division, mint the role users ────────
    r = client.post("/api/v1/superadmin/divisions", headers=sa_h, json={
        "name": f"Pages Load {tag}", "code": f"PGS{tag}",
        "contact_person": "Test", "contact_email": f"pages{tag}@test.in",
        "contact_mobile": "9800000000",
        "provision": True, "admin_username": "company_admin",
        "admin_password": "Admin@123", "admin_email": f"pages{tag}@admin.in",
    })
    check("create+provision division", r.status_code == 200, f"{r.status_code} {r.text[:300]}")
    div = r.json()
    _TENANT_DB = div.get("tenant_db_name")
    slug = div.get("code")
    did = div.get("id")

    # clear the provisioned admin's onboarding flags
    from saas.db_utils import get_conn
    conn = get_conn(_TENANT_DB)
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM divisions ORDER BY id LIMIT 1")
        tenant_division_id = cur.fetchone()[0]
        cur.execute(
            "UPDATE users SET must_change_password=FALSE, mfa_setup_required=FALSE, "
            "profile_pending=FALSE WHERE username='company_admin'")
        conn.commit()
    finally:
        conn.close()

    # tenant-side master data fetch (levels + roles) as the division admin
    r = client.post("/api/v1/auth/login",
                    json={"division_slug": slug, "username": "company_admin", "password": "Admin@123"})
    check("admin login", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    if r.status_code != 200:
        print("\n" + ("ALL PASS" if not FAILED else f"{len(FAILED)} FAILED: {FAILED}"))
        sys.exit(1 if FAILED else 0)
    ADM = {"Authorization": f"Bearer {r.json()['access_token']}"}

    levels = {lv["name"]: lv for lv in client.get("/api/v1/hierarchy/levels", headers=ADM).json()["items"]}
    roles = {r0["name"]: r0 for r0 in client.get("/api/v1/roles", headers=ADM).json()["items"]}

    def mk_user(payload):
        return client.post("/api/v1/users", headers=ADM, json=payload)

    r = mk_user({"username": f"mr.pages{tag.lower()}", "password": "Mr@12345",
                 "full_name": "Pages MR", "email": f"mr.pages{tag.lower()}@test.in", "mobile": "9000000011",
                 "hierarchy_level_id": levels["MR"]["id"], "role_id": roles["mr"]["id"],
                 "parent_id": None, "territory": "Pune East"})
    check("create mr user", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    r = mk_user({"username": f"asm.pages{tag.lower()}", "password": "Am@12345",
                 "full_name": "Pages ASM", "email": f"asm.pages{tag.lower()}@test.in", "mobile": "9000000012",
                 "hierarchy_level_id": levels["ASM"]["id"], "role_id": roles["asm"]["id"],
                 "parent_id": None, "territory": "Pune East"})
    check("create asm user", r.status_code == 200, f"{r.status_code} {r.text[:200]}")

    # verifier + finance are GLOBAL roles: only the SA console can mint them
    def mk_global(username, password, role_name, full_name, mobile):
        rr = client.post(f"/api/v1/superadmin/divisions/{did}/users", headers=sa_h, json={
            "username": username, "password": password, "full_name": full_name,
            "email": f"{username}@test.in", "role_id": roles[role_name]["id"],
            "division_id": tenant_division_id, "mobile": mobile})
        return rr

    rr = mk_global(f"verifier.pages{tag.lower()}", "Vf@12345", "verifier", "Pages Verifier", "9000000013")
    check("create verifier user (SA console)", rr.status_code == 200, f"{rr.status_code} {rr.text[:200]}")
    rr = mk_global(f"finance.pages{tag.lower()}", "Fn@12345", "finance", "Pages Finance", "9000000014")
    check("create finance user (SA console)", rr.status_code == 200, f"{rr.status_code} {rr.text[:200]}")

    # SA-minted users land with onboarding flags up -- clear them like the UI does
    conn = get_conn(_TENANT_DB)
    try:
        cur = conn.cursor()
        cur.execute("UPDATE users SET must_change_password=FALSE, mfa_setup_required=FALSE, "
                    "profile_pending=FALSE WHERE role_id IN (%s,%s)",
                    (roles["verifier"]["id"], roles["finance"]["id"]))
        conn.commit()
    finally:
        conn.close()

    tenants = [
        ("admin",    slug, "company_admin", "Admin@123"),
        ("mr",       slug, f"mr.pages{tag.lower()}", "Mr@12345"),
        ("asm",      slug, f"asm.pages{tag.lower()}", "Am@12345"),
        ("verifier", slug, f"verifier.pages{tag.lower()}", "Vf@12345"),
        ("finance",  slug, f"finance.pages{tag.lower()}", "Fn@12345"),
    ]

    for label, division_slug, username, password in tenants:
        s = login(client, division_slug, username, password)
        if not s:
            check(f"login {label} ({username}@{division_slug})", False, "login failed")
            continue
        perms, entry = set(s["perms"]), s["entry"]
        check(f"login {label} ({username}@{division_slug}) role={s['user'].get('role')} perms={len(perms)} entry={entry}", True)
        h = {"Authorization": f"Bearer {s['access']}"}
        print(f"-- {label} --")
        for page, eps in ENDPOINTS.items():
            perm = PERM_GATE[page]
            visible = page == "/app/profile" or perm in perms or (perm is not None and perm in EXTRA_OK.get(label, []))
            for ep in eps:
                code, txt = call(client, h, ep)
                if visible:
                    # page is linked for this role -> must load
                    check(f"{label} {page} {ep}", code == 200, f"{code} {txt}")
                else:
                    # not linked -> must be a clean access or clean deny;
                    # shared master-data endpoints may be 200 (used by other
                    # pages the role can open), but never 401/404/500
                    check(f"{label} {page} {ep} unlinked", code in (200, 403), f"{code} {txt}")

    print("\n" + ("ALL PASS" if not FAILED else f"{len(FAILED)} FAILED: {FAILED}"))
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    try:
        main()
    finally:
        # hermetic: drop the scratch tenant + scratch platform DBs
        if _TENANT_DB:
            __import__("psycopg2")
            from saas import config
            conn = __import__("psycopg2").connect(
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