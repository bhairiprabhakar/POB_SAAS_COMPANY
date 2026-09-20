"""Targeted test for dashboard/analytics `days` period filter + prev-window deltas.

Runs fully HERMETIC: a scratch control-plane DB is created before the saas
package is imported, a single division is provisioned (fresh tenant DB), and
both scratch DBs are dropped afterwards. The live platform is never touched.

Ported in Batch 4 from the companies/plans model (company_code login) to the
current divisions/division_slug architecture.

Run:  venv\\Scripts\\python.exe scripts\\test_dashboards_period.py
"""
import os
import sys
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# hermetic: point the platform at a scratch control-plane DB BEFORE the saas
# package is imported (config reads this at import time)
SCRATCH_PLATFORM = f"psk_bi_{uuid.uuid4().hex[:10]}"
os.environ["PLATFORM_DB_NAME"] = SCRATCH_PLATFORM

from fastapi.testclient import TestClient  # noqa: E402
from saas.main import app  # noqa: E402
from saas import platform_db  # noqa: E402

platform_db.init_platform_db()

BASE = "/api/v1"
FAILED = []
BOOT = os.environ.get("SUPERADMIN_BOOTSTRAP_PASSWORD", "101990")
_TENANT_DB = None


def check(name, cond, extra=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" -- {extra}" if extra and not cond else ""))
    if not cond:
        FAILED.append(name)


def h(token):
    return {"Authorization": f"Bearer {token}"}


def _drop_database(name):
    """Terminate lingering connections and drop a database (scratch DBs only)."""
    import psycopg2
    from saas import config
    conn = psycopg2.connect(
        host=config.DB_HOST, port=config.DB_PORT, user=config.DB_USER,
        password=config.DB_PASSWORD, dbname="postgres", connect_timeout=10,
    )
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname=%s AND pid<>pg_backend_pid()", (name,))
    cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
    cur.close()
    conn.close()


def main():
    global _TENANT_DB
    tag = uuid.uuid4().hex[:6].upper()
    with TestClient(app) as client:
        r = client.post(f"{BASE}/auth/superadmin/login",
                        json={"username": "superadmin", "password": BOOT})
        check("superadmin login", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        SA = h(r.json()["access_token"])

        r = client.post(f"{BASE}/superadmin/divisions", headers=SA, json={
            "name": f"BI Period {tag}", "code": f"PBI{tag}",
            "contact_person": "Test", "contact_email": f"bi{tag}@test.in",
            "contact_mobile": "9800000000",
            "provision": True, "admin_username": "bi_admin",
            "admin_password": "Admin@123", "admin_email": f"bi{tag}@admin.in",
        })
        check("create+provision division", r.status_code == 200, f"{r.status_code} {r.text[:300]}")
        div = r.json()
        _TENANT_DB = div.get("tenant_db_name")
        slug = div.get("code") or div.get("slug")

        # clear the admin's onboarding flags so login proceeds straight through
        from saas.db_utils import get_conn
        conn = get_conn(_TENANT_DB)
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM divisions ORDER BY id LIMIT 1")
            cur.execute("SELECT id FROM users WHERE username='bi_admin'")
            uid = cur.fetchone()[0]
            cur.execute(
                "UPDATE users SET must_change_password=FALSE, mfa_setup_required=FALSE, "
                "profile_pending=FALSE WHERE id=%s", (uid,))
            conn.commit()
        finally:
            conn.close()

        r = client.post(f"{BASE}/auth/login",
                        json={"division_slug": slug, "username": "bi_admin", "password": "Admin@123"})
        check("admin login", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        ADM = h(r.json()["access_token"])

        # company dashboard: all-time + 30-day window + prev deltas
        r = client.get(f"{BASE}/dashboards/company", headers=ADM)
        d0 = r.json()
        check("company all-time", r.status_code == 200 and d0.get("pob_total") == 0,
              f"{r.status_code} {r.text[:200]}")
        check("company has prev", "prev_pob_total" in d0 and "prev_pob_amount" in d0)

        r = client.get(f"{BASE}/dashboards/company?days=30", headers=ADM)
        d30 = r.json()
        check("company days=30", r.status_code == 200 and d30.get("days") == 30 and d30.get("pob_total") == 0,
              f"{r.status_code} {r.text[:200]}")

        for ep in ("campaign", "brand", "mr", "leaderboard", "performance", "verification", "finance", "gift"):
            r = client.get(f"{BASE}/dashboards/{ep}?days=90", headers=ADM)
            check(f"{ep}?days=90", r.status_code == 200, f"{r.status_code} {r.text[:200]}")

        r = client.get(f"{BASE}/analytics/summary?days=90", headers=ADM)
        a = r.json()
        check("analytics summary days=90", r.status_code == 200 and a.get("days") == 90
              and "own_prev" in a and a["own_prev"].get("amount") == 0,
              f"{r.status_code} {r.text[:200]}")

    print("\n" + ("ALL PASS" if not FAILED else f"{len(FAILED)} FAILED: {FAILED}"))
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    try:
        main()
    finally:
        # hermetic: drop the scratch tenant + scratch platform DBs
        if _TENANT_DB:
            _drop_database(_TENANT_DB)
        _drop_database(SCRATCH_PLATFORM)