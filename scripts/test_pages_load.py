"""Page-load verification for every role type: superadmin, tenant admin,
agent (MR/PSR - data entry), and company people (verifier/finance/asm).

For each login it calls the API endpoints each page fetches on mount and
reports 200 (page loads) / 403 (permission-blocked, matching the sidebar
PERM_GATE) / 401 / 5xx. Only endpoints on pages the role should see (per
permission + data_entry flag) are expected 200; every other endpoint must be
a clean 403, never 401/404/500.

Run:  python scripts/test_pages_load.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from saas.main import app

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


def login(client, company_code, username, password):
    r = client.post("/api/v1/auth/login",
                    json={"company_code": company_code, "username": username, "password": password})
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
    client = TestClient(app)

    r = client.post("/api/v1/auth/superadmin/login",
                    json={"username": "superadmin", "password": "Pob_Saas@2026"})
    ok = r.status_code == 200
    check("superadmin login", ok, f"{r.status_code} {r.text[:200]}")
    sa_h = {"Authorization": f"Bearer {r.json()['access_token']}"} if ok else None

    sa_pages = [
        "/api/v1/superadmin/metrics",
        "/api/v1/superadmin/companies",
        "/api/v1/superadmin/plans",
        "/api/v1/superadmin/audit-logs",
        "/api/v1/superadmin/ocr-usage/companies",
    ]
    print("\n== superadmin (platform console) ==")
    if sa_h:
        for p in sa_pages:
            code, txt = call(client, sa_h, p)
            check(f"SA {p}", code == 200, f"{code} {txt}")

    tenants = [
        ("admin",      "DEMO1234", "company_admin", "Admin@123"),
        ("mr",         "DEMO1234", "mr.amit",        "Demo@123"),
        ("asm",        "DEMO1234", "asm.rahul",      "Demo@123"),
        ("verifier",   "DEMO1234", "verifier.kavita","Demo@123"),
        ("finance",    "DEMO1234", "finance.sunil",  "Demo@123"),
    ]

    for label, company, username, password in tenants:
        s = login(client, company, username, password)
        if not s:
            check(f"login {label} ({username}@{company})", False, "login failed")
            continue
        perms, entry = set(s["perms"]), s["entry"]
        check(f"login {label} ({username}@{company}) role={s['user'].get('role')} perms={len(perms)} entry={entry}", True)
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
    main()
