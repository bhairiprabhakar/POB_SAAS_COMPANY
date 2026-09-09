"""Targeted test for dashboard/analytics `days` period filter + prev-window deltas."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from saas.main import app

BASE = "/api/v1"
FAILED = []
PWD = os.environ.get("SUPERADMIN_BOOTSTRAP_PASSWORD", "Pob_Saas@2026")


def check(name, cond, extra=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" -- {extra}" if extra and not cond else ""))
    if not cond:
        FAILED.append(name)


def h(token):
    return {"Authorization": f"Bearer {token}"}


def main():
    import uuid
    uid = uuid.uuid4().hex[:6].upper()
    with TestClient(app) as client:
        r = client.post(f"{BASE}/auth/superadmin/login",
                        json={"username": "superadmin", "password": PWD})
        check("superadmin login", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        SA = h(r.json()["access_token"])

        r = client.get(f"{BASE}/superadmin/plans", headers=SA)
        trial = next(p for p in r.json()["items"] if p["code"] == "trial")

        r = client.post(f"{BASE}/superadmin/companies", headers=SA, json={
            "name": f"BI Test {uid}", "gst": f"27ABCDE{uid}1F2Z", "pan": f"ABCDE{uid}F",
            "contact_person": "Test", "contact_email": f"bi{uid}@test.in",
            "contact_mobile": "9800000000", "plan_id": trial["id"], "user_limit": 100,
            "provision": True, "admin_username": "bi_admin",
            "admin_password": "Admin@123", "admin_email": f"bi{uid}@admin.in",
        })
        check("create company", r.status_code == 200, f"{r.status_code} {r.text[:300]}")
        code = r.json()["code"]

        r = client.post(f"{BASE}/auth/login",
                        json={"company_code": code, "username": "bi_admin", "password": "Admin@123"})
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
    main()
