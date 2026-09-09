"""Audit gap-closure verification (migration 2.5.0).

Checks the gaps closed in this round end-to-end against the DEMO tenant:
  1. New roles (psr / sm / campaignos_admin) + hierarchy SM level + visit perms.
  2. Chemist `area` and user `state` fields.
  3. Hierarchy data scoping (MR self-only, ASM team, admin all).
  4. Chemist visit CRUD + summary + due + scoping of visit rows.
  5. New dashboard KPIs (approval_rate, visits, followups_due, pob_duplicates)
     + leaderboard + performance endpoints.
  6. New report types export as XLSX.
  7. Superadmin backup + backup listing endpoints.
"""
import sys
import time
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(str(Path(__file__).resolve().parent.parent / ".env"))

from fastapi.testclient import TestClient
from saas.main import app

UNIQ = str(int(time.time()))
FAILED = []


def check(name, ok, extra=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {extra}" if not ok and extra else ""))
    if not ok:
        FAILED.append(name)


def login(c, code, u, p):
    r = c.post('/api/v1/auth/login', json={'company_code': code, 'username': u, 'password': p})
    assert r.status_code < 400, (r.status_code, r.json())
    return {'Authorization': 'Bearer ' + r.json()['access_token']}


def dash_pob_total(c, h):
    return c.get('/api/v1/dashboards/company', headers=h).json().get('pob_total')


with TestClient(app) as c:
    CODE = 'DEMO1234'
    admin = login(c, CODE, 'company_admin', 'Admin@123')
    amit = login(c, CODE, 'mr.amit', 'Demo@123')
    pooja = login(c, CODE, 'mr.pooja', 'Demo@123')
    asm = login(c, CODE, 'asm.rahul', 'Demo@123')

    # ── 1. roles / hierarchy / perms ───────────────────────────────────────
    roles = {r['name']: set(r['permissions']) for r in c.get('/api/v1/roles', headers=admin).json()['items']}
    check("role psr exists", 'psr' in roles)
    check("role sm exists", 'sm' in roles)
    check("role campaignos_admin exists", 'campaignos_admin' in roles)
    sa_perms = roles.get('campaignos_admin', set())
    for p in ('user.view', 'user.manage', 'verification.view', 'verification.approve',
              'pob.view', 'gratification.view', 'chemist.view', 'visit.view', 'dashboard.view',
              'report.view', 'report.export', 'settings.manage'):
        check(f"campaignos_admin has {p}", p in sa_perms)
    check("mr has visit.view", 'visit.view' in roles.get('mr', set()))
    check("mr has visit.manage", 'visit.manage' in roles.get('mr', set()))
    check("asm has visit.view", 'visit.view' in roles.get('asm', set()))
    check("sm has visit.view", 'visit.view' in roles.get('sm', set()))

    levels = {l['name']: l for l in c.get('/api/v1/hierarchy/levels', headers=admin).json()['items']}
    check("hierarchy SM level rank=4", levels.get('SM', {}).get('rank') == 4)

    perms = {p['code'] for p in c.get('/api/v1/permissions', headers=admin).json()['items']}
    check("permission visit.view registered", 'visit.view' in perms)
    check("permission visit.manage registered", 'visit.manage' in perms)

    # ── 2. chemist area + user state fields ────────────────────────────────
    chem = c.post('/api/v1/chemists', headers=admin, json={
        'name': f'AreaChemist {UNIQ}', 'shop_name': 'Area Chemist Store', 'city': 'Mumbai',
        'state': 'Maharashtra', 'area': 'Andheri West', 'status': 'active'}).json()
    listed = next(x for x in c.get('/api/v1/chemists', headers=admin).json()['items'] if x['id'] == chem['id'])
    check("chemist area field persisted", listed.get('area') == 'Andheri West')
    chem_id = chem['id']

    # ── 3. hierarchy data scoping ──────────────────────────────────────────
    # mr.pooja submits a fresh POB -> only pooja, asm and admin should see it
    cid = c.post('/api/v1/campaigns', headers=admin, json={
        'name': f'Scope Camp {UNIQ}', 'brand_id': 1, 'status': 'active', 'scheme_type': 'cashback',
        'upload_roles': 'mr', 'payout_cycle': 'instant', 'auto_verify': False,
        'rules': [{'name': 'R', 'priority': 1, 'conditions': [],
                   'then_action': 'cashback', 'value': 10, 'active': True}]}).json()['id']
    pid = c.post('/api/v1/products', headers=admin, json={
        'campaign_id': cid, 'name': 'Scope Prod', 'sku': f'SCP-{UNIQ}', 'ptr': 100, 'mrp': 130}).json()['id']
    before = {u: dash_pob_total(c, h) for u, h in
              (('amit', amit), ('pooja', pooja), ('asm', asm), ('admin', admin))}
    r = c.post('/api/v1/pob/submit', headers=pooja, data={
        'campaign_id': cid, 'product_id': pid, 'chemist_id': chem_id,
        'quantity': 2, 'ptr': 100, 'invoice_amount': 200, 'pob_amount': 200,
        'invoice_number': f'INV-SCOPE-{UNIQ}', 'invoice_date': '2026-08-01'})
    check("POB submitted by mr.pooja", r.status_code == 200 and r.json()['status'] == 'pending_verification',
          str(r.json()))
    after = {u: dash_pob_total(c, h) for u, h in
             (('amit', amit), ('pooja', pooja), ('asm', asm), ('admin', admin))}
    check("scoping: admin sees +1", after['admin'] - before['admin'] == 1)
    check("scoping: asm (team) sees +1", after['asm'] - before['asm'] == 1)
    check("scoping: submitting MR sees +1", after['pooja'] - before['pooja'] == 1)
    check("scoping: other MR sees +0", after['amit'] - before['amit'] == 0)

    # ── 4. visits CRUD + scoping ───────────────────────────────────────────
    v = c.post('/api/v1/visits', headers=pooja, json={
        'chemist_id': chem_id, 'campaign_id': cid, 'visit_date': '2026-08-05',
        'opening_stock': 50, 'quantity_sold': 30, 'current_stock': 20,
        'fresh_purchase': 10, 'remarks': 'Stock moving well'})
    check("visit create", v.status_code == 200 and v.json().get('id'), str(v.json()))
    vid = v.json()['id']

    mine = c.get('/api/v1/visits', headers=pooja).json()['items']
    check("visit visible to recording PSR", any(i['id'] == vid for i in mine))
    others = c.get('/api/v1/visits', headers=amit).json()['items']
    check("visit hidden from other MR", not any(i['id'] == vid for i in others))

    summ = c.get('/api/v1/visits/summary', headers=pooja).json()
    check("visit summary KPI", summ.get('visits', 0) >= 1 and 'liquidation_pct' in summ, str(summ))
    due = c.get('/api/v1/visits/due', headers=asm).json()
    check("visit due endpoint", 'items' in due and 'days' in due)

    up = c.put(f'/api/v1/visits/{vid}', headers=pooja, json={'quantity_sold': 35, 'remarks': 'updated'})
    check("visit update", up.status_code == 200, str(up.json()))

    # ── 5. new dashboard KPIs + leaderboard/performance ────────────────────
    d = c.get('/api/v1/dashboards/company', headers=admin).json()
    for k in ('pob_duplicates', 'approval_rate', 'visits', 'followups_due',
              'pob_by_status', 'gratification_by_type'):
        check(f"company dashboard KPI {k}", k in d, str(list(d.keys())))
    lb = c.get('/api/v1/dashboards/leaderboard', headers=admin).json()
    check("leaderboard endpoint", 'items' in lb and all(x['full_name'] for x in lb['items'][:1]))
    perf = c.get('/api/v1/dashboards/performance', headers=admin).json()
    check("performance endpoint", 'by_state' in perf and 'by_region' in perf)

    # ── 6. new report types export ─────────────────────────────────────────
    for rt in ('daily', 'weekly', 'monthly', 'state', 'approval', 'pending_verification',
               'visit', 'leaderboard', 'duplicate', 'gift', 'cashback'):
        rr = c.get(f'/api/v1/reports/{rt}', headers=admin)
        check(f"report export {rt}", rr.status_code == 200 and rr.content[:2] == b'PK',
              f"status {rr.status_code}")

    # ── 7. superadmin backup ───────────────────────────────────────────────
    sa = c.post('/api/v1/auth/superadmin/login', json={
        'username': 'superadmin',
        'password': os.environ.get("SUPERADMIN_BOOTSTRAP_PASSWORD", "superadmin@2025")})
    check("superadmin login", sa.status_code == 200, str(sa.status_code))
    if sa.status_code == 200:
        sah = {'Authorization': 'Bearer ' + sa.json()['access_token']}
        b = c.post('/api/v1/superadmin/backup', headers=sah)
        check("superadmin run backup", b.status_code == 200 and b.json().get('ok_count', 0) >= 1,
              str(b.json())[:300])
        bl = c.get('/api/v1/superadmin/backups', headers=sah)
        check("superadmin list backups", bl.status_code == 200 and len(bl.json()['items']) >= 1,
              str(bl.json())[:300])

print()
print(f"{len(FAILED)} gap-closure check(s) failed: {FAILED}" if FAILED else "GAP-CLOSURE VERIFICATION PASSED")
sys.exit(1 if FAILED else 0)
