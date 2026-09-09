"""Cross-check hierarchy scoping for analytics + dashboards.

Builds a full reporting chain HO -> NSM -> RSM -> ASM -> MR(+MR) in an existing
tenant, seeds one POB per user, then logs in as every level + the division admin
and asserts each caller only sees their own scope:

  MR   -> own rows only (scope "self")
  ASM  -> own + direct team (scope "team")
  RSM  -> own + deeper team
  NSM  -> own + deeper team
  HO   -> everything (rank 7 -> unrestricted)
  admin (division_admin, unassigned) -> everything

Also checks cross-user POB reads (403 for out-of-scope) and /dashboards/manager
visibility. Seed rows are removed afterwards.

Run:  python scripts/test_scoping.py
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from app.security import hash_pw
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

    t = get_conn(tenant_db)
    cur = t.cursor()

    tag = uuid.uuid4().hex[:6]
    created = {"users": {}, "pobs": {}, "campaign": None, "product": None, "chemist": None}

    def q(sql, params=()):
        if params:
            cur.execute(sql, params)
        else:
            cur.execute(sql)

    def sq(sql, params=()):
        cur.execute(sql, params)
        return cur.fetchall()

    def rid(role):
        return sq("SELECT id FROM roles WHERE name=%s", (role,))[0][0]

    def lid(level):
        return sq("SELECT id FROM hierarchy_levels WHERE name=%s", (level,))[0][0]

    try:
        # ---- clean leftovers from earlier runs ---------------------------
        q("DELETE FROM pob_activities WHERE user_id IN (SELECT id FROM users WHERE username LIKE 'scop_%')")
        q("DELETE FROM users WHERE username LIKE 'scop_%'")
        q("DELETE FROM products WHERE sku LIKE 'SCOP-%'")
        q("DELETE FROM chemists WHERE name LIKE 'SCOP Chemist %'")
        q("DELETE FROM campaigns WHERE name LIKE 'SCOP Test %'")
        t.commit()

        # ---- hierarchy chain (unique region/state per level so the RSM/NSM
        #      region+state add-ons stay empty and scope == own team) -------
        chain = [
            ("scop_ho",  "HO",  "ho",  None,      "R-H", "S-H"),
            ("scop_nsm", "NSM", "nsm", "scop_ho", "R-C", "S-C"),
            ("scop_rsm", "RSM", "rsm", "scop_nsm", "R-B", "S-B"),
            ("scop_asm", "ASM", "asm", "scop_rsm", "R-A", "S-A"),
            ("scop_mra", "MR",  "mr",  "scop_asm", "R-A", "S-A"),
            ("scop_mrb", "MR",  "mr",  "scop_asm", "R-A", "S-A"),
        ]
        for username, level, role, parent, region, state in chain:
            q("""INSERT INTO users (username, password, full_name, email, mobile,
                 role_id, hierarchy_level_id, parent_id, region, state, status)
                 VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active') RETURNING id""",
              (username, hash_pw("Test@123"), username.replace("_", " ").title(),
               f"{username}@test.in", "98" + str(abs(hash(username)) % 100000000).zfill(8),
               rid(role), lid(level), created["users"].get(parent), region, state))
            created["users"][username] = cur.fetchone()[0]

        q("SELECT id FROM users WHERE username=%s", (ADMIN[0],))
        admin_id = cur.fetchone()[0]

        q("""INSERT INTO campaigns (name, division, start_date, end_date, active,
             status, scheme_type, invoice_verification_required)
             VALUES (%s,%s,current_date,current_date+30,TRUE,'active','cashback',TRUE) RETURNING id""",
          (f"SCOP Test {tag}", "Test Div"))
        created["campaign"] = cur.fetchone()[0]
        q("""INSERT INTO products (campaign_id, sku, name, strength, pack, ptr, mrp, min_quantity,
             min_pob, max_pob, scheme_eligibility, status)
             VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,'active') RETURNING id""",
          (created["campaign"], f"SCOP-{tag}", "SCOP Product", "1 mg", "1x1",
           100.0, 120.0, 1, 100.0, 100000.0))
        created["product"] = cur.fetchone()[0]
        q("""INSERT INTO chemists (name, shop_name, gst, mobile, city, state, status)
             VALUES (%s,%s,%s,%s,%s,%s,'active') RETURNING id""",
          (f"SCOP Chemist {tag}", "SCOP Shop", f"27SCOP{tag}1Z", "9833333333", "Mumbai", "MH"))
        created["chemist"] = cur.fetchone()[0]

        # (username, [(amount, status), ...]) -- invoice_amount == pob_amount
        pob_spec = {
            "scop_mra": [(1000, "verified"), (500, "pending_verification")],
            "scop_mrb": [(500, "verified"), (300, "rejected")],
            "scop_asm": [(200, "verified")],
            "scop_rsm": [(300, "verified")],
            "scop_nsm": [(400, "verified")],
            "scop_ho":  [(600, "verified")],
        }
        for username, rows in pob_spec.items():
            created["pobs"][username] = []
            for i, (amount, status) in enumerate(rows):
                q("""INSERT INTO pob_activities (user_id, campaign_id, product_id, chemist_id,
                     quantity, ptr, mrp, invoice_amount, pob_amount, invoice_number, invoice_date, status)
                     VALUES (%s,%s,%s,%s,10,100,120,%s,%s,%s,current_date,%s) RETURNING id""",
                  (created["users"][username], created["campaign"], created["product"],
                   created["chemist"], amount, amount,
                   f"SCOP-{tag}-{username}-{i}", status))
                created["pobs"][username].append(cur.fetchone()[0])
        t.commit()

        # ---- expected per-caller totals (all-time) --------------------------
        # verified_value uses COALESCE(NULLIF(invoice_amount,0), pob_amount)
        # own_verified/own_value = caller's OWN rows; team_verified/team_value =
        # everyone visible EXCEPT self (team scope only); m_sum/m_roi =
        # member-row counts returned by each endpoint; lb = leaderboard rows.
        active_users = sq("SELECT count(*) FROM users WHERE status='active'")[0][0]
        expect = {
            "scop_mra": dict(pob_total=2, verified=1, value=1000, scope="self",
                             own_verified=1, own_value=1000, m_sum=0, m_roi=1, lb=1),
            "scop_mrb": dict(pob_total=2, verified=1, value=500, scope="self",
                             own_verified=1, own_value=500, m_sum=0, m_roi=1, lb=1),
            "scop_asm": dict(pob_total=5, verified=3, value=1700, scope="team",
                             own_verified=1, own_value=200, team_verified=2, team_value=1500,
                             m_sum=2, m_roi=3, lb=3),
            "scop_rsm": dict(pob_total=6, verified=4, value=2000, scope="team",
                             own_verified=1, own_value=300, team_verified=3, team_value=1700,
                             m_sum=3, m_roi=4, lb=4),
            "scop_nsm": dict(pob_total=7, verified=5, value=2400, scope="team",
                             own_verified=1, own_value=400, team_verified=4, team_value=2000,
                             m_sum=4, m_roi=5, lb=5),
            "scop_ho":  dict(pob_total=8, verified=6, value=3000, scope="all",
                             own_verified=1, own_value=600, m_sum=0, all_sum=active_users, m_roi=active_users, lb=6),
            ADMIN[0]:   dict(pob_total=8, verified=6, value=3000, scope="all",
                             own_verified=0, own_value=0, m_sum=0, all_sum=active_users, m_roi=active_users, lb=6),
        }
        active_users = sq("SELECT count(*) FROM users WHERE status='active'")[0][0]

        with TestClient(app) as client:
            tokens = {}
            for who in list(expect):
                u, p = (who, "Test@123") if who != ADMIN[0] else ADMIN
                r = client.post("/api/v1/auth/login",
                                json={"company_code": COMPANY_CODE, "username": u, "password": p})
                check(f"login {who}", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
                tokens[who] = {"Authorization": f"Bearer {r.json()['access_token']}"}

            # ---- company dashboard per caller --------------------------------
            for who, exp in expect.items():
                r = client.get("/api/v1/dashboards/company", headers=tokens[who])
                d = r.json()
                ok = (r.status_code == 200 and d.get("pob_total") == exp["pob_total"]
                      and d.get("pob_verified") == exp["verified"]
                      and d.get("pob_invoice_value") == exp["value"])
                check(f"company {who}: pob_total={exp['pob_total']} verified={exp['verified']} value={exp['value']}",
                      ok, f"{r.status_code} got {d.get('pob_total')}/{d.get('pob_verified')}/{d.get('pob_invoice_value')}")

            # ---- analytics summary: scope + own/team KPIs ----------------------
            for who, exp in expect.items():
                r = client.get("/api/v1/analytics/summary", headers=tokens[who])
                d = r.json()
                ok = (r.status_code == 200 and d.get("scope") == exp["scope"]
                      and d["own"]["verified"] == exp["own_verified"]
                      and d["own"]["invoice_value"] == exp["own_value"])
                if exp.get("team_verified") is not None:
                    ok = ok and d.get("team") and d["team"]["verified"] == exp["team_verified"] \
                        and d["team"]["invoice_value"] == exp["team_value"]
                # scope "all" (admin/HO): "members" is the caller's own division
                # (none assigned here); company-wide data lives in all_members.
                ok = ok and len(d.get("members") or []) == exp["m_sum"]
                if exp.get("all_sum") is not None:
                    ok = ok and len(d.get("all_members") or []) == exp["all_sum"] \
                        and d.get("all_team") is not None \
                        and d["all_team"]["invoice_value"] == exp["value"]
                check(f"summary {who}: scope={exp['scope']} own v={exp['own_verified']}/{exp['own_value']}"
                      + (f" team v={exp['team_verified']}/{exp['team_value']}" if exp.get("team_verified") is not None else ""),
                      ok, f"{r.status_code} got scope={d.get('scope')} own={d.get('own')} team={d.get('team')} members={len(d.get('members') or [])} all={len(d.get('all_members') or [])}")

            # ---- analytics roi: totals + member list size ----------------------
            for who, exp in expect.items():
                r = client.get("/api/v1/analytics/roi", headers=tokens[who])
                d = r.json()
                ok = (r.status_code == 200 and d["totals"]["verified_value"] == exp["value"]
                      and len(d["members"]) == exp["m_roi"])
                check(f"roi {who}: verified_value={exp['value']} members={exp['m_roi']}",
                      ok, f"{r.status_code} got {d.get('totals', {}).get('verified_value')}/{len(d.get('members') or [])}")

            # ---- leaderboard size -------------------------------------------------
            for who, exp in expect.items():
                r = client.get("/api/v1/dashboards/leaderboard", headers=tokens[who])
                items = r.json().get("items") or []
                check(f"leaderboard {who}: {exp['lb']} rows", len(items) == exp["lb"],
                      f"got {len(items)}")

            # ---- isolation: MR must not see team-mates' numbers -------------------
            r = client.get("/api/v1/dashboards/company", headers=tokens["scop_mra"])
            assert r.json()["pob_total"] != expect["scop_asm"]["pob_total"]

            # ---- /dashboards/mr: only visible users listed -------------------------
            r = client.get("/api/v1/dashboards/mr", headers=tokens["scop_asm"])
            ids = {x["id"] for x in r.json().get("items") or []}
            want = {created["users"]["scop_asm"], created["users"]["scop_mra"], created["users"]["scop_mrb"]}
            check("mr dashboard asm -> 3 rows (self + 2 MRs)", ids == want, f"got {ids}")

            # ---- /dashboards/manager: inside scope ok, outside 403 -------------------
            r = client.get(f"/api/v1/dashboards/manager?uid={created['users']['scop_asm']}",
                           headers=tokens["scop_asm"])
            check("manager: asm sees own team (3 members)", r.status_code == 200
                  and r.json().get("team_size") == 3, f"{r.status_code} {r.text[:200]}")
            r = client.get(f"/api/v1/dashboards/manager?uid={created['users']['scop_mra']}",
                           headers=tokens["scop_asm"])
            check("manager: asm can view own MR (team_size 1)", r.status_code == 200
                  and r.json().get("team_size") == 1, f"{r.status_code} {r.text[:200]}")
            r = client.get(f"/api/v1/dashboards/manager?uid={created['users']['scop_asm']}",
                           headers=tokens["scop_mra"])
            check("manager: MR cannot view ASM (403)", r.status_code == 403, f"{r.status_code}")
            r = client.get(f"/api/v1/dashboards/manager?uid={created['users']['scop_nsm']}",
                           headers=tokens[ADMIN[0]])
            check("manager: admin can view NSM", r.status_code == 200, f"{r.status_code}")

            # ---- POB record access: own/team vs out-of-scope -------------------------
            r = client.get(f"/api/v1/pob/{created['pobs']['scop_mra'][0]}", headers=tokens["scop_mra"])
            check("pob: MR reads own POB", r.status_code == 200, f"{r.status_code}")
            r = client.get(f"/api/v1/pob/{created['pobs']['scop_mrb'][0]}", headers=tokens["scop_mra"])
            check("pob: MR cannot read team-mate POB (403)", r.status_code == 403, f"{r.status_code}")
            r = client.get(f"/api/v1/pob/{created['pobs']['scop_mrb'][0]}", headers=tokens["scop_asm"])
            check("pob: ASM reads team POB", r.status_code == 200, f"{r.status_code}")
            r = client.get(f"/api/v1/pob/{created['pobs']['scop_nsm'][0]}", headers=tokens["scop_asm"])
            check("pob: ASM cannot read NSM POB (403)", r.status_code == 403, f"{r.status_code}")
            r = client.get(f"/api/v1/pob/{created['pobs']['scop_ho'][0]}", headers=tokens[ADMIN[0]])
            check("pob: admin reads any POB", r.status_code == 200, f"{r.status_code}")

            # ---- admin == HO (both unrestricted) ---------------------------------------
            a = client.get("/api/v1/analytics/roi", headers=tokens[ADMIN[0]]).json()
            h = client.get("/api/v1/analytics/roi", headers=tokens["scop_ho"]).json()
            check("admin and HO see identical totals", a["totals"]["verified_value"] == h["totals"]["verified_value"])

    finally:
        # remove seed rows regardless of outcome
        for username in created["users"]:
            q("DELETE FROM pob_activities WHERE user_id=%s", (created["users"][username],))
        q("DELETE FROM users WHERE username LIKE 'scop_%'")
        if created["product"]:
            q("DELETE FROM products WHERE id=%s", (created["product"],))
        if created["chemist"]:
            q("DELETE FROM chemists WHERE id=%s", (created["chemist"],))
        if created["campaign"]:
            q("DELETE FROM campaigns WHERE id=%s", (created["campaign"],))
        t.commit()
        t.close()

    print("\n" + ("ALL PASS" if not FAILED else f"{len(FAILED)} FAILED: {FAILED}"))
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
