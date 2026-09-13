"""
End-to-end smoke test for the SaaS platform, run against real Postgres.

Covers the full happy path:
  superadmin -> create+provision company -> company admin -> masters
  -> MR submits POB (with invoice upload) -> duplicate detection
  -> verifier approves -> gratification (cashback) paid -> dashboards/reports

Run:  venv\\Scripts\\python.exe scripts\\smoke_test.py
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from saas.main import app

BASE = "/api/v1"
FAILED = []
BOOT_PASSWORD = os.environ.get("SUPERADMIN_BOOTSTRAP_PASSWORD", "superadmin@2025")


def check(name, cond, extra=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" -- {extra}" if extra and not cond else ""))
    if not cond:
        FAILED.append(name)


def auth_header(token):
    return {"Authorization": f"Bearer {token}"}


def main():
    with TestClient(app) as client:
        # ── superadmin login ────────────────────────────────────────────────
        r = client.post(f"{BASE}/auth/superadmin/login",
                        json={"username": "superadmin", "password": BOOT_PASSWORD})
        check("superadmin login", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        sa_token = r.json().get("access_token")
        SA = auth_header(sa_token)

        # ── plans seeded ────────────────────────────────────────────────────
        r = client.get(f"{BASE}/superadmin/plans", headers=SA)
        check("plans listed", r.status_code == 200 and len(r.json()["items"]) >= 4)
        trial_plan = next(p for p in r.json()["items"] if p["code"] == "trial")

        # ── create + provision a company ────────────────────────────────────
        r = client.post(f"{BASE}/superadmin/companies", headers=SA, json={
            "name": "Acme Pharma Ltd",
            "gst": "27AABCU9603R1ZM",
            "pan": "AABCU9603R",
            "contact_person": "Rahul Mehta",
            "contact_email": "rahul@acme.in",
            "contact_mobile": "9898989898",
            "plan_id": trial_plan["id"],
            "user_limit": 500,
            "provision": True,
            "admin_username": "company_admin",
            "admin_password": "Admin@123",
            "admin_email": "admin@acme.in",
        })
        check("company created + provisioned", r.status_code == 200, f"{r.status_code} {r.text[:300]}")
        company = r.json()
        check("company status active", company.get("status") == "active", company.get("status"))
        check("tenant db assigned", bool(company.get("tenant_db_name")), company.get("tenant_db_name"))
        tenant_db = company.get("tenant_db_name")

        # ── tenant login as company admin ───────────────────────────────────
        r = client.post(f"{BASE}/auth/login", json={
            "company_code": company["code"], "username": "company_admin", "password": "Admin@123"})
        check("company admin login", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        admin_token = r.json()["access_token"]
        ADM = auth_header(admin_token)

        r = client.get(f"{BASE}/auth/me", headers=ADM)
        check("me: full permissions", r.status_code == 200 and "permissions" in r.json())
        perms = r.json().get("permissions", [])
        check("admin has campaign.manage", "campaign.manage" in perms)

        # ── hierarchy is dynamic ────────────────────────────────────────────
        r = client.get(f"{BASE}/hierarchy/levels", headers=ADM)
        levels = {lv["name"]: lv for lv in r.json()["items"]}
        check("default hierarchy HO..MR", all(k in levels for k in ("HO", "NSM", "ZSM", "RSM", "ASM", "MR")))
        # add a custom level to prove nothing is hardcoded
        r = client.post(f"{BASE}/hierarchy/levels", headers=ADM,
                        json={"name": "GM", "label": "General Manager", "rank": 5, "parent_level_id": levels["HO"]["id"]})
        check("custom hierarchy level GM", r.status_code == 200, r.text[:200])

        # ── roles + users ───────────────────────────────────────────────────
        r = client.get(f"{BASE}/roles", headers=ADM)
        roles = {r0["name"]: r0 for r0 in r.json()["items"]}
        mr_role = roles["mr"]["id"]
        verifier_role = roles["verifier"]["id"]

        r = client.post(f"{BASE}/users", headers=ADM, json={
            "username": "mr.ashok", "password": "Mr@12345", "full_name": "Ashok Kumar",
            "email": "ashok@acme.in", "mobile": "9000000001",
            "hierarchy_level_id": levels["MR"]["id"], "role_id": mr_role,
            "parent_id": None, "territory": "Pune East"})
        check("create MR user", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        mr_id = r.json()["id"]

        r = client.post(f"{BASE}/users", headers=ADM, json={
            "username": "verifier1", "password": "Vf@12345", "full_name": "Verifier One",
            "email": "verifier@acme.in", "role_id": verifier_role})
        check("create verifier user", r.status_code == 200, r.text[:200])

        # ── masters: brand / campaign / product / chemist ──────────────────
        r = client.post(f"{BASE}/brands", headers=ADM, json={"name": "Asthakind", "code": "ASKD"})
        check("create brand", r.status_code == 200, r.text[:200])
        brand_id = r.json()["id"]

        r = client.post(f"{BASE}/campaigns", headers=ADM, json={
            "name": "Asthakind POB Cashback", "brand_id": brand_id,
            "division": "Cardio", "start_date": "2026-01-01", "end_date": "2026-12-31",
            "status": "active", "active": True, "scheme_type": "cashback",
            "terms_conditions": "Invoice must be legible."})
        check("create campaign", r.status_code == 200, r.text[:200])
        campaign_id = r.json()["id"]

        r = client.post(f"{BASE}/products", headers=ADM, json={
            "campaign_id": campaign_id, "brand_id": brand_id, "sku": "ASKD-10",
            "name": "Asthakind 10", "composition": "Amlodipine 10mg", "strength": "10mg",
            "dosage_form": "Tablet", "pack": "15x10", "ptr": 185.0, "pts": 178.0,
            "mrp": 205.0, "gst": 12})
        check("create product", r.status_code == 200, r.text[:200])
        product_id = r.json()["id"]

        r = client.post(f"{BASE}/chemists", headers=ADM, json={
            "name": "Sunrise Medical", "shop_name": "Sunrise Medical Store",
            "gst": "27AABCU9603R1ZM", "mobile": "9888898888", "city": "Pune",
            "state": "Maharashtra", "pin": "411001", "owner_name": "Sunil Jain",
            "doctor_name": "Dr. Deshpande", "category": "Retail", "ocid": "OC-0001"})
        check("create chemist", r.status_code == 200, r.text[:200])
        chemist_id = r.json()["id"]

        # ── MR submits POB with invoice ─────────────────────────────────────
        r = client.post(f"{BASE}/auth/login", json={
            "company_code": company["code"], "username": "mr.ashok", "password": "Mr@12345"})
        check("MR login", r.status_code == 200, r.text[:200])
        mr_token = r.json()["access_token"]
        MR = auth_header(mr_token)

        invoice_bytes = b"%PDF-1.4 fake invoice content 001"
        r = client.post(
            f"{BASE}/pob/submit", headers=MR,
            data={"campaign_id": campaign_id, "product_id": product_id, "chemist_id": chemist_id,
                  "quantity": 10, "ptr": 185.0, "mrp": 205.0, "invoice_amount": 1850.0,
                  "pob_amount": 1850.0, "invoice_number": "INV-1001", "invoice_date": "2026-07-01",
                  "remarks": "Cash sale"},
            files={"invoice": ("inv1001.pdf", invoice_bytes, "application/pdf")})
        check("MR submits POB", r.status_code == 200, f"{r.status_code} {r.text[:300]}")
        pob_body = r.json()
        check("POB pending verification", pob_body.get("status") == "pending_verification", pob_body)
        pob_id = pob_body["pob_id"]

        # duplicate detection on identical invoice
        r = client.post(
            f"{BASE}/pob/submit", headers=MR,
            data={"campaign_id": campaign_id, "product_id": product_id, "chemist_id": chemist_id,
                  "quantity": 5, "ptr": 185.0, "invoice_amount": 925.0, "pob_amount": 925.0,
                  "invoice_number": "INV-1002", "invoice_date": "2026-07-01"},
            files={"invoice": ("inv1001.pdf", invoice_bytes, "application/pdf")})
        check("duplicate detected", r.status_code == 200 and r.json().get("status") == "duplicate",
              r.text[:300])

        # ── verifier approves ───────────────────────────────────────────────
        r = client.post(f"{BASE}/auth/login", json={
            "company_code": company["code"], "username": "verifier1", "password": "Vf@12345"})
        check("verifier login", r.status_code == 200, r.text[:200])
        vf_token = r.json()["access_token"]
        VF = auth_header(vf_token)

        r = client.get(f"{BASE}/verification/stats", headers=VF)
        check("verification stats", r.status_code == 200 and r.json().get("pending", 0) >= 1)

        r = client.get(f"{BASE}/verification/queue?status=pending", headers=VF)
        check("verification queue", r.status_code == 200 and len(r.json()["items"]) >= 1)
        vid = r.json()["items"][0]["verification_id"]

        r = client.post(f"{BASE}/verification/{vid}/approve", headers=VF, json={})
        check("approve POB", r.status_code == 200 and r.json()["status"] == "approved", r.text[:200])

        # ── gratification (cashback) workflow ───────────────────────────────
        r = client.get(f"{BASE}/gratification?status=eligible", headers=ADM)
        check("gratification created", r.status_code == 200 and len(r.json()["items"]) == 1)
        gid = r.json()["items"][0]["id"]

        r = client.post(f"{BASE}/gratification/{gid}/approve", headers=ADM, json={"upi_id": "ashok@upi"})
        check("cashback approved", r.status_code == 200 and r.json()["status"] == "approved", r.text[:200])

        r = client.post(f"{BASE}/gratification/{gid}/pay", headers=ADM, json={"payment_ref": "UTR-123456"})
        check("cashback paid", r.status_code == 200 and r.json()["status"] == "completed", r.text[:200])

        # ── reject flow with mandatory reason ───────────────────────────────
        r = client.post(f"{BASE}/pob/submit", headers=MR,
                        data={"campaign_id": campaign_id, "product_id": product_id, "chemist_id": chemist_id,
                              "quantity": 3, "ptr": 185.0, "invoice_amount": 555.0, "pob_amount": 555.0,
                              "invoice_number": "INV-2001", "invoice_date": "2026-07-02"},
                        files={"invoice": ("inv2001.pdf", b"%PDF-1.4 fake 002", "application/pdf")})
        pob2 = r.json()["pob_id"]
        r = client.get(f"{BASE}/verification/queue?status=pending", headers=VF)
        v2 = next(i["verification_id"] for i in r.json()["items"] if i["pob_id"] == pob2)
        r = client.post(f"{BASE}/verification/{v2}/reject", headers=VF, json={})
        check("reject requires reason", r.status_code == 400)
        r = client.post(f"{BASE}/verification/{v2}/reject", headers=VF, json={"reason": "Illegible invoice"})
        check("reject with reason", r.status_code == 200 and r.json()["status"] == "rejected", r.text[:200])

        # ── dashboards + reports ────────────────────────────────────────────
        r = client.get(f"{BASE}/dashboards/company", headers=ADM)
        check("company dashboard", r.status_code == 200 and r.json().get("pob_total") == 3, r.text[:200])
        r = client.get(f"{BASE}/dashboards/verification", headers=ADM)
        check("verification dashboard", r.status_code == 200 and "avg_tat_hours" in r.json())
        r = client.get(f"{BASE}/dashboards/finance", headers=ADM)
        check("finance dashboard", r.status_code == 200 and r.json().get("paid") == 1850.0, r.text[:200])

        r = client.get(f"{BASE}/reports/campaign", headers=ADM)
        check("campaign report xlsx", r.status_code == 200 and r.content[:2] == b"PK", "not xlsx")
        r = client.get(f"{BASE}/reports/duplicate", headers=ADM)
        check("duplicate report", r.status_code == 200 and r.content[:2] == b"PK")

        # ── notifications to MR ─────────────────────────────────────────────
        r = client.post(f"{BASE}/auth/login", json={
            "company_code": company["code"], "username": "mr.ashok", "password": "Mr@12345"})
        MR = auth_header(r.json()["access_token"])
        r = client.get(f"{BASE}/notifications/", headers=MR)
        check("MR notifications", r.status_code == 200 and r.json()["unread"] >= 2,
              f"{r.status_code} {r.text[:200]}")

        # ── physical gift flow (different scheme) ───────────────────────────
        r = client.post(f"{BASE}/campaigns", headers=ADM, json={
            "name": "Celevida Reward", "status": "active", "scheme_type": "physical_gift"})
        gift_campaign = r.json()["id"]
        r = client.post(f"{BASE}/products", headers=ADM, json={
            "campaign_id": gift_campaign, "name": "Celevida 100g", "ptr": 500, "mrp": 550})
        gift_product = r.json()["id"]
        r = client.post(f"{BASE}/gifts", headers=ADM, json={
            "name": "Bluetooth Speaker", "cost": 1200, "stock": 5})
        check("create gift", r.status_code == 200)
        gift_id = r.json()["id"]

        r = client.post(f"{BASE}/pob/submit", headers=MR,
                        data={"campaign_id": gift_campaign, "product_id": gift_product, "chemist_id": chemist_id,
                              "quantity": 4, "ptr": 500.0, "invoice_amount": 2000.0, "pob_amount": 2000.0,
                              "invoice_number": "INV-3001", "invoice_date": "2026-07-03"},
                        files={"invoice": ("inv3001.pdf", b"%PDF-1.4 fake 003", "application/pdf")})
        gift_pob_id = r.json()["pob_id"]
        r = client.get(f"{BASE}/verification/queue?status=pending", headers=VF)
        v3 = next(i["verification_id"] for i in r.json()["items"] if i["pob_id"] == gift_pob_id)
        r = client.post(f"{BASE}/verification/{v3}/approve", headers=VF, json={})
        check("gift POB approved", r.status_code == 200, r.text[:200])
        r = client.get(f"{BASE}/gratification?type_code=physical_gift&status=eligible", headers=ADM)
        gift_grat = r.json()["items"][0]
        r = client.post(f"{BASE}/gratification/{gift_grat['id']}/dispatch", headers=ADM,
                        json={"gift_id": gift_id, "tracking": "DTDC-7788"})
        check("gift dispatched", r.status_code == 200 and r.json()["status"] == "dispatched", r.text[:200])
        r = client.post(f"{BASE}/gratification/{gift_grat['id']}/delivered", headers=ADM,
                        data={"latitude": 18.5204, "longitude": 73.8567},
                        files={"photo": ("proof.jpg", b"jpgbytes", "image/jpeg")})
        check("gift delivered + GPS + photo", r.status_code == 200 and r.json()["status"] == "delivered",
              r.text[:200])
        r = client.post(f"{BASE}/gratification/{gift_grat['id']}/acknowledge", headers=ADM,
                        json={"acknowledgement": "Received in good condition"})
        check("gift acknowledged", r.status_code == 200 and r.json()["status"] == "completed", r.text[:200])

        # ── voucher flow ────────────────────────────────────────────────────
        r = client.post(f"{BASE}/campaigns", headers=ADM, json={
            "name": "Nise Cashback Voucher", "status": "active", "scheme_type": "voucher"})
        vch_campaign = r.json()["id"]
        r = client.post(f"{BASE}/products", headers=ADM, json={
            "campaign_id": vch_campaign, "name": "Nise 100", "ptr": 300, "mrp": 340})
        vch_product = r.json()["id"]
        r = client.post(f"{BASE}/pob/submit", headers=MR,
                        data={"campaign_id": vch_campaign, "product_id": vch_product, "chemist_id": chemist_id,
                              "quantity": 2, "ptr": 300.0, "invoice_amount": 600.0, "pob_amount": 600.0,
                              "invoice_number": "INV-4001", "invoice_date": "2026-07-04"},
                        files={"invoice": ("inv4001.pdf", b"%PDF-1.4 fake 004", "application/pdf")})
        vch_pob = r.json()["pob_id"]
        r = client.get(f"{BASE}/verification/queue?status=pending", headers=VF)
        v4 = next(i["verification_id"] for i in r.json()["items"] if i["pob_id"] == vch_pob)
        r = client.post(f"{BASE}/verification/{v4}/approve", headers=VF, json={})
        r = client.get(f"{BASE}/gratification?type_code=voucher&status=eligible", headers=ADM)
        vch_grat = r.json()["items"][0]
        r = client.post(f"{BASE}/gratification/{vch_grat['id']}/generate-voucher", headers=ADM, json={})
        check("voucher generated", r.status_code == 200 and r.json()["status"] == "generated", r.text[:200])
        code = r.json()["code"]
        r = client.post(f"{BASE}/gratification/{vch_grat['id']}/send-voucher", headers=ADM, json={})
        check("voucher sent", r.status_code == 200 and r.json()["status"] == "sent", r.text[:200])
        r = client.post(f"{BASE}/gratification/{vch_grat['id']}/redeem-voucher", headers=ADM, json={})
        check("voucher redeemed", r.status_code == 200 and r.json()["status"] == "completed", r.text[:200])

        # ── RBAC: MR must NOT access verification ───────────────────────────
        r = client.get(f"{BASE}/verification/queue", headers=MR)
        check("RBAC: MR blocked from verification", r.status_code == 403, f"expected 403 got {r.status_code}")

        # ── cross-tenant isolation: file route ──────────────────────────────
        r = client.get(f"{BASE}/storage/{tenant_db}/invoices/x.pdf", headers=MR)
        check("storage tenant scoped", r.status_code == 404, f"expected 404 got {r.status_code}")

        print("\nCompany code:", company["code"], "| tenant_db:", tenant_db)
        print(f"\n{len(FAILED)} failures out of the checks above.")
        if FAILED:
            print("Failed:", FAILED)
            sys.exit(1)
        print("SMOKE TEST PASSED")


if __name__ == "__main__":
    main()
