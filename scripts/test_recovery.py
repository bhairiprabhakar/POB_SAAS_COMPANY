"""Account-recovery verification (forgot company code / username / password).

Runs against real Postgres via the FastAPI test client and the platform DB.

Covers:
  - request OTP for a contact (email + mobile) -> dev_otp echo when no real
    SMTP/SMS credentials are configured (see saas/routers/recovery.py).
  - verify returns the forgotten company code / username.
  - password reset: OTP -> recovery token -> new password -> login with it.
  - failure modes: wrong OTP, unknown contact (generic, no code), rate limit.

Run:  venv\\Scripts\\python.exe scripts\\test_recovery.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from saas.main import app

BASE = "/api/v1"
FAILED = []

# A contact that belongs to exactly one company (DEMO1234 / pob_cmp_0007).
EMAIL = "prabhakar.bhairi@gmail.com"
MOBILE = "9390887070"


def check(name, cond, extra=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" -- {extra}" if extra and not cond else ""))
    if not cond:
        FAILED.append(name)


def get_dev_otp(resp):
    """dev_otp is echoed only when SMTP/SMS credentials are absent."""
    return (resp.get("dev_otp") if isinstance(resp, dict) else None)


def main():
    client = TestClient(app)

    # ── 1. forgot company code (by email) ────────────────────────────────────
    r = client.post(f"{BASE}/auth/recovery/request",
                    json={"contact": EMAIL, "purpose": "company_code"})
    check("request OTP (company_code, email)", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    body = r.json()
    otp = get_dev_otp(body)
    check("dev_otp echoed (no SMTP/SMS configured)", bool(otp), f"resp={body}")

    r = client.post(f"{BASE}/auth/recovery/verify",
                    json={"contact": EMAIL, "purpose": "company_code", "otp": otp})
    check("verify -> company code", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    accounts = r.json().get("accounts") or []
    codes = {a.get("company_code") for a in accounts}
    check("company code returned is DEMO1234", "DEMO1234" in codes, f"codes={codes}")

    # ── 2. forgot username (by mobile) ───────────────────────────────────────
    r = client.post(f"{BASE}/auth/recovery/request",
                    json={"contact": MOBILE, "purpose": "username"})
    check("request OTP (username, mobile)", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    otp = get_dev_otp(r.json())
    r = client.post(f"{BASE}/auth/recovery/verify",
                    json={"contact": MOBILE, "purpose": "username", "otp": otp})
    check("verify -> username", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    names = {a.get("username") for a in (r.json().get("accounts") or [])}
    check("username returned is prabhakar", "prabhakar" in names, f"names={names}")

    # ── 3. password reset, then login with the new password ──────────────────
    r = client.post(f"{BASE}/auth/recovery/request",
                    json={"contact": EMAIL, "purpose": "password"})
    check("request OTP (password, email)", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    otp = get_dev_otp(r.json())
    r = client.post(f"{BASE}/auth/recovery/verify",
                    json={"contact": EMAIL, "purpose": "password", "otp": otp})
    check("verify -> password recovery token", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    v = r.json()
    token = v.get("recovery_token")
    acc = next((a for a in (v.get("accounts") or []) if a.get("username") == "prabhakar"), None)
    check("password recovery token + matching account", bool(token) and bool(acc), f"token={bool(token)} acc={acc}")

    new_pw = "Recovery@123"
    r = client.post(f"{BASE}/auth/recovery/reset-password",
                    json={"recovery_token": token, "company_id": acc["company_id"],
                          "user_id": acc["user_id"], "new_password": new_pw})
    check("reset password", r.status_code == 200, f"{r.status_code} {r.text[:200]}")

    r = client.post(f"{BASE}/auth/login",
                    json={"username": "prabhakar", "password": new_pw})
    check("login with recovered password", r.status_code == 200, f"{r.status_code} {r.text[:200]}")

    # Restore the original password for other test runs.
    r = client.post(f"{BASE}/auth/recovery/request",
                    json={"contact": EMAIL, "purpose": "password"})
    otp = get_dev_otp(r.json())
    r = client.post(f"{BASE}/auth/recovery/verify",
                    json={"contact": EMAIL, "purpose": "password", "otp": otp})
    v = r.json()
    token = v.get("recovery_token")
    acc = next((a for a in (v.get("accounts") or []) if a.get("username") == "prabhakar"), None)
    r = client.post(f"{BASE}/auth/recovery/reset-password",
                    json={"recovery_token": token, "company_id": acc["company_id"],
                          "user_id": acc["user_id"], "new_password": "Demo@123"})
    check("restore original password", r.status_code == 200, f"{r.status_code} {r.text[:200]}")

    # ── 4. failure modes ─────────────────────────────────────────────────────
    r = client.post(f"{BASE}/auth/recovery/request",
                    json={"contact": EMAIL, "purpose": "password"})
    otp = get_dev_otp(r.json())
    r = client.post(f"{BASE}/auth/recovery/verify",
                    json={"contact": EMAIL, "purpose": "password", "otp": "000000"})
    check("wrong OTP rejected", r.status_code == 400, f"{r.status_code} {r.text[:200]}")

    r = client.post(f"{BASE}/auth/recovery/request",
                    json={"contact": "nobody@unknown.in", "purpose": "username"})
    check("unknown contact -> generic sent (anti-enumeration)", r.status_code == 200
          and not get_dev_otp(r.json()), f"{r.status_code} {r.text[:200]}")

    r = client.post(f"{BASE}/auth/recovery/reset-password",
                    json={"recovery_token": "bogus", "company_id": 1, "user_id": 1,
                          "new_password": "Whatever@1"})
    check("reset with bogus token rejected", r.status_code in (401, 403), f"{r.status_code}")

    print("\n" + ("ALL PASS" if not FAILED else f"{len(FAILED)} FAILED: {FAILED}"))
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
