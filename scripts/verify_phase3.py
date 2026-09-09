"""Phase 3 E2E verification: inventory (warehouses/gift stock/movements) and
payout batches (instant cycle run -> pay -> reconcile), wired to the
Phase 2 approval flow (instant-payout campaign)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from saas.main import app


def login(c, code, u, p):
    r = c.post('/api/v1/auth/login', json={'company_code': code, 'username': u, 'password': p})
    assert r.status_code < 400, r.json()
    return {'Authorization': 'Bearer ' + r.json()['access_token']}


with TestClient(app) as c:
    CODE = 'DEMO1234'
    admin = login(c, CODE, 'company_admin', 'Admin@123')
    mr = login(c, CODE, 'mr.amit', 'Demo@123')
    asm = login(c, CODE, 'asm.rahul', 'Demo@123')
    verifier = login(c, CODE, 'verifier.kavita', 'Demo@123')
    finance = login(c, CODE, 'finance.sunil', 'Demo@123')

    # ── 1. warehouses + gift + inbound stock ───────────────────────────────
    wid = c.post('/api/v1/warehouses', headers=admin, json={
        'name': 'Pune DC', 'code': 'PUNE-01', 'location': 'Pune', 'contact': 'Suresh'}).json()['id']
    print('create warehouse ->', wid)
    wid2 = c.post('/api/v1/warehouses', headers=admin, json={
        'name': 'Nagpur DC', 'code': 'NGP-01', 'location': 'Nagpur'}).json()['id']
    gid = c.post('/api/v1/gifts', headers=admin, json={
        'name': 'Bluetooth Speaker', 'cost': 500, 'sku': 'G-SPK-100'}).json()['id']
    print('create gift ->', gid)
    r = c.post('/api/v1/inventory/gift-stock/inbound', headers=admin, json={
        'gift_id': gid, 'warehouse_id': wid, 'quantity': 50, 'note': 'initial'})
    print('inbound 50 ->', r.status_code, r.json())
    r = c.post('/api/v1/inventory/gift-stock/transfer', headers=admin, json={
        'gift_id': gid, 'from_warehouse_id': wid, 'to_warehouse_id': wid2, 'quantity': 10})
    print('transfer 10 ->', r.status_code, r.json())
    st = c.get('/api/v1/inventory/gift-stock', headers=admin).json()
    it = next(i for i in st['items'] if i['gift_id'] == gid)
    print('gift stock ->', 'qty:', it['quantity'], '| available:', it['available'], '| warehouses:', len(it['warehouses']))
    mv = c.get('/api/v1/inventory/movements', headers=admin).json()['items']
    print('movements recorded ->', len(mv))

    # ── 2. instant-payout campaign + full approval flow ────────────────────
    wf = c.post('/api/v1/workflows/', headers=admin, json={
        'name': 'Instant Chain', 'description': 'ASM then verifier',
        'steps': [
            {'order': 1, 'role': 'asm', 'label': 'ASM Review', 'notify': True},
            {'order': 2, 'role': 'verifier', 'label': 'Verifier Check', 'notify': True},
        ]}).json()['id']
    cid = c.post('/api/v1/campaigns', headers=admin, json={
        'name': 'Instant Payout Campaign', 'brand_id': 1, 'status': 'active',
        'scheme_type': 'cashback', 'approval_workflow_id': wf,
        'upload_roles': 'mr', 'payout_cycle': 'instant', 'auto_verify': False,
        'rules': [
            {'name': 'Cash 300', 'priority': 1,
             'conditions': [{'field': 'invoice_amount', 'op': '>=', 'value': 1000}],
             'then_action': 'cashback', 'value': 300, 'active': True},
        ]}).json()['id']
    pid = c.post('/api/v1/products', headers=admin, json={
        'campaign_id': cid, 'name': 'Instant Product', 'sku': 'INST-1', 'ptr': 120, 'mrp': 150}).json()['id']
    r = c.post('/api/v1/pob/submit', headers=mr, data={
        'campaign_id': cid, 'product_id': pid, 'chemist_id': 1,
        'quantity': 30, 'ptr': 120, 'invoice_amount': 5000, 'pob_amount': 3600,
        'invoice_number': 'INV-INST-001'})
    print('MR submit ->', r.status_code)
    vid = r.json()['verification_id']
    c.post(f'/api/v1/verification/{vid}/approve', headers=asm, json={'note': 'ok'})
    r = c.post(f'/api/v1/verification/{vid}/approve', headers=verifier, json={})
    print('final approve ->', r.status_code, r.json())
    gid2 = r.json().get('gratification_id')
    g = c.get(f'/api/v1/gratification/{gid2}', headers=admin).json()
    print('gratification ->', g['type_code'], g['scheme_value'], '| cycle:', g['payout_cycle'],
          '| batch_date:', g['payout_batch_date'], '| status:', g['status'])

    # ── 3. finance approves cashback → payout run → pay → reconcile ────────
    r = c.post(f'/api/v1/gratification/{gid2}/approve', headers=finance, json={'note': 'ok'})
    print('cashback approve ->', r.status_code, r.json())
    r = c.post('/api/v1/payouts/run', headers=finance, json={'cycle': 'instant'})
    print('payout run ->', r.status_code, r.json())
    bid = r.json()['batch_id']
    b = c.get(f'/api/v1/payouts/{bid}', headers=finance).json()
    print('batch ->', b['status'], '| items:', len(b['items']), '| total:', b['total_amount'])
    r = c.post(f'/api/v1/payouts/{bid}/pay', headers=finance, json={'payment_ref': 'BANK-REF-1'})
    print('batch pay ->', r.status_code, r.json())
    r = c.post(f'/api/v1/payouts/{bid}/reconcile', headers=finance,
               json={'reconciliation_ref': 'GL-2026-001'})
    print('batch reconcile ->', r.status_code, r.json())
    bl = c.get('/api/v1/payouts', headers=finance).json()['items']
    print('payout batches listed ->', [(x['id'], x['cycle'], x['status']) for x in bl[:2]])
