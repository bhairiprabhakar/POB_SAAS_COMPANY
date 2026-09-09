"""Verify the flexible invoice-proof grace + proof-lag + reminders + direct
proof flow:

  - pre_grace_days: invoices before the campaign start are accepted.
  - grace_months: invoices after the campaign end (+ months grace) are accepted.
  - proof_lag_days: exposed in /pob/mine, /pob/{id} and /pob/my-stats.
  - awaiting_proof in /pob/my-stats.
  - direct invoice-proof (no prior POB visit) auto-creates lines from OCR.
  - _send_proof_reminders notifies field users once/day after campaign end.

Run:  python scripts/test_grace_proof.py
"""
import io
import os
import sys
import uuid
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from saas import platform_db
from saas.db_utils import get_conn
from saas.main import app

COMPANY_CODE = os.environ.get("POB_COMPANY", "BITE3878")
ADMIN = ("bi_admin", "Admin@123")
FAILED = []


def check(name, cond, extra=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" -- {extra}" if extra and not cond else ""))
    if not cond:
        FAILED.append(name)


def main():
    conn = platform_db.get_db()
    c = conn.cursor()
    c.execute("SELECT tenant_db_name FROM companies WHERE code=%s", (COMPANY_CODE,))
    row = c.fetchone()
    conn.close()
    if not row or not row[0]:
        print(f"[FAIL] no tenant db for {COMPANY_CODE}")
        sys.exit(1)
    tenant_db = row[0]

    from saas.migrations import ensure_migrated
    ensure_migrated(tenant_db)

    t = get_conn(tenant_db)
    cur = t.cursor()
    tag = uuid.uuid4().hex[:6]
    created = {"pobs": [], "products": [], "campaigns": [], "chemists": []}
    reminder_before = 0

    def q(sql, params=()):
        cur.execute(sql, params) if params else cur.execute(sql)

    def sq(sql, params=()):
        cur.execute(sql, params) if params else cur.execute(sql)
        return cur.fetchall()

    def add_campaign(name, start, end, pre_grace=0, grace_months=0, grace_days=15, status="active"):
        q("""INSERT INTO campaigns (name, division, start_date, end_date, active,
             status, scheme_type, invoice_verification_required, period_type, grace_days,
             grace_months, pre_grace_days)
             VALUES (%s,%s,%s,%s,TRUE,%s,'cashback',TRUE,'none',%s,%s,%s) RETURNING id""",
          (name, "Test Div", start, end, status, grace_days, grace_months, pre_grace))
        cid_ = cur.fetchone()[0]
        created["campaigns"].append(cid_)
        return cid_

    def add_product(campaign_id, name):
        q("""INSERT INTO products (campaign_id, sku, name, strength, pack, ptr, mrp, min_quantity,
             min_pob, max_pob, scheme_eligibility, status)
             VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,'active') RETURNING id""",
          (campaign_id, f"SKU-{tag}", name, "10 mg", "10x10", 100.0, 120.0, 1, 100.0, 100000.0))
        pid = cur.fetchone()[0]
        created["products"].append(pid)
        return pid

    def add_chemist():
        q("""INSERT INTO chemists (name, shop_name, gst, mobile, city, state, status)
             VALUES (%s,%s,%s,%s,%s,%s,'active') RETURNING id""",
          (f"GRACE Chemist {tag}", "GR Shop", f"27GR{tag}1Z", "9822222222", "Mumbai", "MH"))
        cid_ = cur.fetchone()[0]
        created["chemists"].append(cid_)
        return cid_

    today = date.today()

    try:
        q("SELECT id FROM users WHERE username=%s", (ADMIN[0],))
        admin_id = cur.fetchone()[0]

        # ── pre_grace_days ───────────────────────────────────────────────────
        pre_camp = add_campaign(f"GRACE PRE {tag}", today + timedelta(days=5), today + timedelta(days=35),
                                pre_grace=0, grace_months=0, grace_days=15)
        pre_prod = add_product(pre_camp, "Grace Pre Product")
        chemist = add_chemist()
        t.commit()

        # ── campaigns for months-grace / reminder ───────────────────────────
        end_mon = today - timedelta(days=60)
        mon_camp = add_campaign(f"GRACE MON {tag}", today - timedelta(days=120), end_mon,
                                pre_grace=0, grace_months=3, grace_days=15)
        mon_prod = add_product(mon_camp, "Grace Mon Product")

        remind_camp = add_campaign(f"GRACE REMIND {tag}", today - timedelta(days=90), today - timedelta(days=10),
                                   pre_grace=0, grace_months=0, grace_days=5)

        live_camp = add_campaign(f"GRACE LIVE {tag}", today - timedelta(days=10), today + timedelta(days=10),
                                 pre_grace=0, grace_months=0, grace_days=15)
        live_prod = add_product(live_camp, "Grace Live Product")
        direct_camp = add_campaign(f"GRACE DIRECT {tag}", today - timedelta(days=10), today + timedelta(days=20),
                                   pre_grace=0, grace_months=0, grace_days=15)
        direct_prod = add_product(direct_camp, "Voveran 50")
        t.commit()

        from saas.routers.pob import _add_months

        def png_bytes(seed=0):
            from PIL import Image
            buf = io.BytesIO()
            Image.new("RGB", (8, 8), (200 + seed, 150, 100)).save(buf, format="PNG")
            return buf.getvalue()

        with TestClient(app) as client:
            r = client.post("/api/v1/auth/login",
                            json={"company_code": COMPANY_CODE, "username": ADMIN[0], "password": ADMIN[1]})
            check("admin login", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
            h = {"Authorization": f"Bearer {r.json()['access_token']}"}

            def submit(campaign_id, product_id, invoice_number, invoice_date, amount):
                return client.post("/api/v1/pob/submit", headers=h, data={
                    "campaign_id": campaign_id, "product_id": product_id,
                    "chemist_id": chemist, "quantity": 10, "ptr": 100, "mrp": 120,
                    "invoice_amount": amount, "pob_amount": 1000,
                    "invoice_number": invoice_number, "invoice_date": invoice_date,
                    "remarks": "grace-test",
                })

            # 1. pre-launch invoice rejected without pre_grace_days
            r = submit(pre_camp, pre_prod, f"PRE-REJ-{tag}", today.isoformat(), 1000)
            check("pre-start invoice rejected 400", r.status_code == 400, f"{r.status_code} {r.text[:200]}")

            # 2. ...accepted once pre_grace_days set (new launches)
            q("UPDATE campaigns SET pre_grace_days=10 WHERE id=%s", (pre_camp,))
            t.commit()
            r = submit(pre_camp, pre_prod, f"PRE-OK-{tag}", today.isoformat(), 1000)
            check("pre-start invoice accepted after pre_grace_days", r.status_code == 200,
                  f"{r.status_code} {r.text[:200]}")
            if r.status_code == 200:
                created["pobs"].append(r.json()["pob_id"])

            # 3. months-grace: invoice after campaign end but within grace_months
            r = submit(mon_camp, mon_prod, f"MON-OK-{tag}", today.isoformat(), 1000)
            check("post-end invoice within grace_months accepted 200", r.status_code == 200,
                  f"{r.status_code} {r.text[:200]}")
            if r.status_code == 200:
                created["pobs"].append(r.json()["pob_id"])

            # 4. months-grace: beyond the months+days window -> rejected
            ceiling = _add_months(end_mon, 3) + timedelta(days=15)
            far = ceiling + timedelta(days=1)
            r = submit(mon_camp, mon_prod, f"MON-REJ-{tag}", far.isoformat(), 1000)
            check("post-end invoice beyond grace_months rejected 400", r.status_code == 400,
                  f"{r.status_code} {r.text[:200]}")
            check("months-grace error mentions months", r.status_code == 400 and "months" in r.json().get("detail", ""),
                  f"{r.json()}")

            # 5. single-shot lag = 0
            r = submit(live_camp, live_prod, f"LIVE-{tag}", today.isoformat(), 1000)
            check("live single-shot submit 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
            if r.status_code == 200:
                pid = r.json()["pob_id"]
                created["pobs"].append(pid)
                d = client.get(f"/api/v1/pob/{pid}", headers=h).json()
                check("single-shot proof_lag_days == 0", d.get("proof_lag_days") == 0, f"{d.get('proof_lag_days')}")

            # 6. visit flow: submitted rows have no lag until proof attached
            r = client.post("/api/v1/pob/visit", headers=h, json={
                "campaign_id": live_camp, "chemist_id": chemist,
                "items": [{"product_id": live_prod, "quantity": 5}],
            })
            check("visit submission 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
            visit_pob = None
            if r.status_code == 200:
                visit_pob = r.json()["pob_ids"][0]
                created["pobs"].append(visit_pob)
                mine = client.get("/api/v1/pob/mine", headers=h).json()["items"]
                vrow = next(x for x in mine if x["id"] == visit_pob)
                check("visit row proof_lag_days is None before proof",
                      vrow.get("proof_lag_days") is None, f"{vrow.get('proof_lag_days')}")

            # backdate the visit so the lag is measurable
            q("UPDATE pob_activities SET created_at=CURRENT_TIMESTAMP - INTERVAL '2 days' WHERE id=%s",
              (visit_pob,))
            t.commit()
            r = client.post("/api/v1/pob/invoice-proof", headers=h, data={
                "campaign_id": live_camp, "chemist_id": chemist,
                "invoice_number": f"VIS-{tag}", "invoice_date": today.isoformat(),
            }, files={"file": (f"vis-{tag}.png", png_bytes(), "image/png")})
            check("invoice-proof attach 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
            d = client.get(f"/api/v1/pob/{visit_pob}", headers=h).json()
            check("visit proof_lag_days == 2 after backdated proof",
                  d.get("proof_lag_days") == 2, f"{d.get('proof_lag_days')}")
            mine = client.get("/api/v1/pob/mine", headers=h).json()["items"]
            vrow = next(x for x in mine if x["id"] == visit_pob)
            check("/pob/mine proof_lag_days == 2", vrow.get("proof_lag_days") == 2, f"{vrow.get('proof_lag_days')}")

            # 7. awaiting_proof: a visit without proof stays in my-stats
            r = client.post("/api/v1/pob/visit", headers=h, json={
                "campaign_id": live_camp, "chemist_id": chemist,
                "items": [{"product_id": live_prod, "quantity": 3}],
            })
            check("second visit submission 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
            if r.status_code == 200:
                created["pobs"].append(r.json()["pob_ids"][0])
            st = client.get("/api/v1/pob/my-stats", headers=h).json()
            check("my-stats awaiting_proof >= 1", (st.get("awaiting_proof") or 0) >= 1, f"{st}")
            check("my-stats proof_count >= 2", (st.get("proof_count") or 0) >= 2, f"{st}")
            check("my-stats avg_proof_lag_days >= 1", (st.get("avg_proof_lag_days") or 0) >= 1, f"{st}")
            check("my-stats max_proof_lag_days == 2", (st.get("max_proof_lag_days") or 0) == 2, f"{st}")

            # 8. direct proof (no POB visit) auto-creates lines from OCR
            from saas import ocr as ocr_mod
            real_extract = ocr_mod.extract_fields

            def fake_extract(data, filename=""):
                return {
                    "fields": {
                        "invoice_number": f"DIR-{tag}", "invoice_date": today.isoformat(),
                        "invoice_amount": "500",
                        "items": [{"description": "Voveran 50", "qty": 5, "amount": 500}],
                    },
                    "confidence": 0.95, "engine": "gemini", "raw": {},
                }

            ocr_mod.extract_fields = fake_extract
            try:
                r = client.post("/api/v1/pob/invoice-proof", headers=h, data={
                    "campaign_id": direct_camp, "chemist_id": chemist, "direct": 1,
                }, files={"file": (f"dir-{tag}.png", png_bytes(seed=7), "image/png")})
                check("direct proof 200", r.status_code == 200, f"{r.status_code} {r.text[:300]}")
                results = r.json().get("results") or [] if r.status_code == 200 else []
                check("direct proof created a POB line", len(results) >= 1, f"{r.text[:300]}")
                if results:
                    created["pobs"].append(results[0]["pob_id"])
                    d = client.get(f"/api/v1/pob/{results[0]['pob_id']}", headers=h).json()
                    check("direct proof product matched", d.get("product_name") == "Voveran 50",
                          f"{d.get('product_name')}")
                    check("direct proof invoice number from OCR", d.get("invoice_number") == f"DIR-{tag}",
                          f"{d.get('invoice_number')}")
            finally:
                ocr_mod.extract_fields = real_extract

            # 9. no-pending + non-direct still 404 (unchanged guard)
            r = client.post("/api/v1/pob/invoice-proof", headers=h, data={
                "campaign_id": mon_camp, "chemist_id": chemist,
            }, files={"file": (f"x-{tag}.png", png_bytes(), "image/png")})
            check("non-direct without pending visit 404", r.status_code == 404, f"{r.status_code} {r.text[:200]}")

            # 10. composite duplicate detection (direct proof flow, patched OCR)
            from saas import ocr as ocr_mod
            real_extract = ocr_mod.extract_fields
            fake_items = [{"description": "Grace Live Product", "qty": 10, "amount": 1000}]

            def dup_extract(data, filename=""):
                return {
                    "fields": {
                        "invoice_number": f"DUP-{tag}", "invoice_date": today.isoformat(),
                        "invoice_amount": "1000", "items": fake_items,
                    },
                    "confidence": 0.95, "engine": "gemini", "raw": {},
                }

            ocr_mod.extract_fields = dup_extract
            try:
                def direct_upload(no, seed, date=today.isoformat()):
                    return client.post("/api/v1/pob/invoice-proof", headers=h, data={
                        "campaign_id": live_camp, "chemist_id": chemist, "direct": 1,
                        "invoice_number": no, "invoice_date": date,
                    }, files={"file": (f"dup-{seed}-{tag}.png", png_bytes(seed=seed), "image/png")})

                # a. first upload 200; then the identical composite re-upload
                #    of a VERIFIED POB is rejected as a duplicate.
                r = direct_upload(f"DUP-V-{tag}", 20)
                check("composite: first direct upload 200", r.status_code == 200,
                      f"{r.status_code} {r.text[:300]}")
                first_pob = None
                if r.status_code == 200 and r.json().get("results"):
                    first_pob = r.json()["results"][0]["pob_id"]
                    created["pobs"].append(first_pob)
                    q("UPDATE pob_activities SET status='verified' WHERE id=%s", (first_pob,))
                    t.commit()
                r = direct_upload(f"DUP-V-{tag}", 21)
                check("composite: identical verified re-upload rejected 400", r.status_code == 400,
                      f"{r.status_code} {r.text[:300]}")
                check("composite: reason says already extracted",
                      r.status_code == 400 and "already extracted" in r.json().get("detail", ""),
                      f"{r.json()}")

                # b. same number+date but different brand details -> accepted
                fake_items = [{"description": "Grace Live Product", "qty": 25, "amount": 2500}]
                r = direct_upload(f"DUP-V-{tag}", 22)
                check("composite: different qty/amount accepted 200", r.status_code == 200,
                      f"{r.status_code} {r.text[:300]}")
                if r.status_code == 200 and r.json().get("results"):
                    created["pobs"].append(r.json()["results"][0]["pob_id"])

                # c. same number but a different invoice date -> accepted
                fake_items = [{"description": "Grace Live Product", "qty": 10, "amount": 1000}]
                r = direct_upload(f"DUP-V-{tag}", 23, date=(today - timedelta(days=2)).isoformat())
                check("composite: different date accepted 200", r.status_code == 200,
                      f"{r.status_code} {r.text[:300]}")
                if r.status_code == 200 and r.json().get("results"):
                    created["pobs"].append(r.json()["results"][0]["pob_id"])

                # d. re-upload of a REJECTED invoice (better quality) -> accepted
                q("UPDATE pob_activities SET status='rejected' WHERE id=%s", (first_pob,))
                t.commit()
                r = direct_upload(f"DUP-V-{tag}", 24)
                check("composite: rejected re-upload accepted 200", r.status_code == 200,
                      f"{r.status_code} {r.text[:300]}")
                if r.status_code == 200 and r.json().get("results"):
                    created["pobs"].append(r.json()["results"][0]["pob_id"])
            finally:
                ocr_mod.extract_fields = real_extract

        # 11. reminder sweep: notifies the field user once/day after campaign end
        q("""INSERT INTO pob_activities (user_id, campaign_id, product_id, chemist_id, quantity,
             ptr, mrp, invoice_amount, pob_amount, status)
             VALUES (%s,%s,%s,%s,4,100,120,400,400,'submitted') RETURNING id""",
          (admin_id, remind_camp, mon_prod, chemist))
        remind_pob = cur.fetchone()[0]
        created["pobs"].append(remind_pob)
        t.commit()

        from saas import scheduler as sched
        sched._send_proof_reminders(t)
        t.commit()
        q("SELECT count(*) FROM notifications WHERE user_id=%s AND type='pob.proof.due'", (admin_id,))
        after_first = cur.fetchone()[0]
        check("proof reminder created >= 1 notification", after_first >= 1, f"{after_first}")
        sched._send_proof_reminders(t)
        t.commit()
        q("SELECT count(*) FROM notifications WHERE user_id=%s AND type='pob.proof.due'", (admin_id,))
        after_second = cur.fetchone()[0]
        check("proof reminder dedupes within the same day", after_second == after_first,
              f"{after_second} vs {after_first}")

    finally:
        for pid in created["pobs"]:
            q("DELETE FROM pob_activities WHERE id=%s", (pid,))
        for pid in created["products"]:
            q("DELETE FROM products WHERE id=%s", (pid,))
        for cid_ in created["chemists"]:
            q("DELETE FROM chemists WHERE id=%s", (cid_,))
        for cid_ in created["campaigns"]:
            q("DELETE FROM campaigns WHERE id=%s", (cid_,))
        q("DELETE FROM notifications WHERE type='pob.proof.due'")
        q("DELETE FROM campaigns WHERE name LIKE 'GRACE %'")
        q("DELETE FROM products WHERE sku LIKE %s", (f"SKU-{tag}%",))
        q("DELETE FROM chemists WHERE name LIKE %s", ("GRACE Chemist %",))
        t.commit()
        t.close()

    print("\n" + ("ALL PASS" if not FAILED else f"{len(FAILED)} FAILED: {FAILED}"))
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
