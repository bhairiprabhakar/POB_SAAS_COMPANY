"""Check /pob/{id} returns campaign_products."""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from saas import platform_db
from saas.db_utils import get_conn
from saas.main import app

COMPANY_CODE = os.environ.get("POB_COMPANY", "BITE3878")
ADMIN = ("bi_admin", "Admin@123")

conn = platform_db.get_db()
c = conn.cursor()
c.execute("SELECT tenant_db_name FROM companies WHERE code=%s", (COMPANY_CODE,))
row = c.fetchone()
conn.close()
tenant_db = row[0]

t = get_conn(tenant_db)
cur = t.cursor()
tag = uuid.uuid4().hex[:6]
created = {"pob": None, "product": None, "campaign": None, "chemist": None, "second": None}

try:
    cur.execute("SELECT id FROM users WHERE username=%s", (ADMIN[0],))
    admin_id = cur.fetchone()[0]
    cur.execute("""INSERT INTO campaigns (name, division, start_date, end_date, active,
         status, scheme_type, invoice_verification_required)
         VALUES (%s,%s,current_date,current_date+30,TRUE,'active','cashback',TRUE) RETURNING id""",
      (f"CAMPLIST {tag}", "Test Div"))
    created["campaign"] = cur.fetchone()[0]
    cur.execute("""INSERT INTO products (campaign_id, sku, name, strength, pack, ptr, pts, mrp,
         min_quantity, min_pob, max_pob, scheme_eligibility, status)
         VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,'active') RETURNING id""",
      (created["campaign"], f"SKU-A-{tag}", "Voveran 50", "50 mg", "10x10", 100.0, 95.0, 120.0, 1, 500.0, 5000.0))
    created["product"] = cur.fetchone()[0]
    cur.execute("INSERT INTO campaign_products (campaign_id, product_id, sort_order) VALUES (%s,%s,0)",
                (created["campaign"], created["product"]))
    cur.execute("""INSERT INTO products (campaign_id, sku, name, strength, pack, ptr, pts, mrp,
         min_quantity, min_pob, max_pob, scheme_eligibility, status)
         VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,'active') RETURNING id""",
      (created["campaign"], f"SKU-B-{tag}", "Voveran Gel", "30 g", "1x1", 150.0, 140.0, 170.0, 2, 1000.0, None))
    created["second"] = cur.fetchone()[0]
    cur.execute("INSERT INTO campaign_products (campaign_id, product_id, sort_order) VALUES (%s,%s,0)",
                (created["campaign"], created["second"]))
    cur.execute("""INSERT INTO chemists (name, shop_name, gst, mobile, city, state, status)
         VALUES (%s,%s,%s,%s,%s,%s,'active') RETURNING id""",
      (f"CAMPLIST Chemist {tag}", "CL Shop", f"27CL{tag}1Z", "9844444444", "Mumbai", "MH"))
    created["chemist"] = cur.fetchone()[0]
    cur.execute("""INSERT INTO pob_activities (user_id, campaign_id, product_id, chemist_id, quantity,
         ptr, mrp, invoice_amount, pob_amount, invoice_number, invoice_date, status)
         VALUES (%s,%s,%s,%s,25,100,120,2500,2500,%s,current_date,'verified') RETURNING id""",
      (admin_id, created["campaign"], created["product"], created["chemist"], f"INVCL-{tag}"))
    created["pob"] = cur.fetchone()[0]
    cur.execute("""INSERT INTO verification_history (pob_id, action, reason) VALUES (%s,'submitted','test')""",
      (created["pob"],))
    t.commit()

    with TestClient(app) as client:
        r = client.post("/api/v1/auth/login", json={"company_code": COMPANY_CODE, "username": ADMIN[0], "password": ADMIN[1]})
        h = {"Authorization": f"Bearer {r.json()['access_token']}"}
        r = client.get(f"/api/v1/pob/{created['pob']}", headers=h)
        assert r.status_code == 200, f"{r.status_code} {r.text[:300]}"
        d = r.json()
        cps = d.get("campaign_products") or []
        assert len(cps) == 2, f"expected 2 campaign products, got {len(cps)}: {cps}"
        names = sorted(p["name"] for p in cps)
        assert names == ["Voveran 50", "Voveran Gel"], names
        v = next(p for p in cps if p["name"] == "Voveran 50")
        assert v["min_quantity"] == 1 and v["min_pob"] == 500 and v["max_pob"] == 5000 and v["brand_name"] is None
        assert "product_id" in d and d["product_id"] == created["product"]
        assert d.get("campaign_status") == "active", d.get("campaign_status")
        assert d.get("lines") and d["lines"][0].get("brand_matched") is False, d.get("lines")
        assert d["lines"][0].get("product_name") == "Voveran 50"
        hist = d.get("history") or []
        assert len(hist) == 1 and hist[0]["action"] == "submitted", hist
        assert "verifier_name" in hist[0], list(hist[0].keys())
        rep = d.get("report") or {}
        assert rep.get("status") == "verified", rep.get("status")
        assert len(rep.get("checklist") or []) == 9, len(rep.get("checklist") or [])
        keys = set(rep.get("summary") or {})
        assert {"passed", "failed", "manual"} <= keys, keys
        assert rep["product"]["submitted"] == "Voveran 50"
        rules = rep.get("campaign", {}).get("rules") or []
        assert any(r["label"] == "Minimum POB" and r["ok"] for r in rules), rules
        assert all(r["ok"] for r in rules), [r["label"] for r in rules if not r["ok"]]
        print("PASS campaign_products + campaign_status + brand_matched + history verifier_name + invoice-proof report in /pob/{id}")
finally:
    if created["pob"]:
        cur.execute("DELETE FROM pob_activities WHERE id=%s", (created["pob"],))
        cur.execute("DELETE FROM verification_history WHERE pob_id=%s", (created["pob"],))
    if created["second"]:
        cur.execute("DELETE FROM products WHERE id=%s", (created["second"],))
    if created["product"]:
        cur.execute("DELETE FROM products WHERE id=%s", (created["product"],))
    if created["chemist"]:
        cur.execute("DELETE FROM chemists WHERE id=%s", (created["chemist"],))
    if created["campaign"]:
        cur.execute("DELETE FROM campaigns WHERE id=%s", (created["campaign"],))
    t.commit()
    t.close()
