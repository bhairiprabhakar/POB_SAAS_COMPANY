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

    # 1. workflow definition: MR->ASM->verifier
    wf = c.post('/api/v1/workflows/', headers=admin, json={
        'name': 'FLM Approval Chain', 'description': 'MR upload, ASM then verifier',
        'steps': [
            {'order': 1, 'role': 'asm', 'label': 'ASM Review', 'notify': True},
            {'order': 2, 'role': 'verifier', 'label': 'Verifier Check', 'notify': True},
        ]})
    print('create workflow ->', wf.status_code, wf.json())
    wf_id = wf.json()['id']
    wl = c.get('/api/v1/workflows/', headers=admin).json()['items']
    print('workflows listed ->', len(wl), '| steps of first:', [s['role'] for s in wl[-1]['steps']])

    # 2. campaign with workflow + upload roles + payout cycle + rule
    cid = c.post('/api/v1/campaigns', headers=admin, json={
        'name': 'Builder Campaign', 'brand_id': 1, 'status': 'active',
        'scheme_type': 'gift', 'approval_workflow_id': wf_id,
        'upload_roles': 'mr,flm', 'payout_cycle': 'weekly', 'auto_verify': False,
        'rules': [
            {'name': 'Big POB', 'priority': 1,
             'conditions': [{'field': 'invoice_amount', 'op': '>=', 'value': 3000}],
             'then_action': 'cashback', 'value': 300, 'active': True},
            {'name': 'Default gift', 'priority': 2,
             'conditions': [], 'then_action': 'gift', 'gift_id': 1, 'active': True},
        ]})
    print('create campaign ->', cid.status_code, cid.json())
    cid = cid.json()['id']
    got = c.get(f'/api/v1/campaigns/{cid}', headers=admin).json()
    print('campaign fields ->', 'upload_roles:', got['upload_roles'], '| payout:', got['payout_cycle'],
          '| workflow:', got['approval_workflow_id'], '| rules:', len(got['rules']))

    # add product
    pid = c.post('/api/v1/products', headers=admin, json={
        'campaign_id': cid, 'name': 'Builder Product', 'sku': 'B-P1', 'ptr': 120, 'mrp': 150}).json()['id']

    # 3. MR submits (allowed role) and a role NOT allowed is rejected
    mr = login(c, CODE, 'mr.amit', 'Demo@123')
    bad = login(c, CODE, 'verifier.kavita', 'Demo@123')
    r_bad = c.post('/api/v1/pob/submit', headers=bad, data={
        'campaign_id': cid, 'product_id': pid, 'chemist_id': 1,
        'quantity': 30, 'ptr': 120, 'invoice_amount': 5000, 'pob_amount': 3600,
        'invoice_number': 'INV-BUILD-001'})
    print('verifier submit blocked ->', r_bad.status_code, r_bad.json().get('detail'))
    r = c.post('/api/v1/pob/submit', headers=mr, data={
        'campaign_id': cid, 'product_id': pid, 'chemist_id': 1,
        'quantity': 30, 'ptr': 120, 'invoice_amount': 5000, 'pob_amount': 3600,
        'invoice_number': 'INV-BUILD-001'})
    print('MR submit ->', r.status_code, r.json())
    vid = r.json()['verification_id']

    # 4. workflow approval steps
    vd = c.get(f'/api/v1/verification/{vid}', headers=admin).json()
    print('approval steps ->', [(a['step'], a['role_name'], a['status']) for a in vd['approvals']])

    # wrong role tries step 1
    r = c.post(f'/api/v1/verification/{vid}/approve', headers=login(c, CODE, 'verifier.kavita', 'Demo@123'), json={})
    print('verifier tries step1 (asm) ->', r.status_code, r.json().get('detail'))

    # asm approves step 1
    r = c.post(f'/api/v1/verification/{vid}/approve', headers=login(c, CODE, 'asm.rahul', 'Demo@123'), json={'note': 'ok'})
    print('asm approves step1 ->', r.status_code, r.json())

    # verifier approves step 2 (final) -> gratification via rule (cashback 300)
    r = c.post(f'/api/v1/verification/{vid}/approve', headers=login(c, CODE, 'verifier.kavita', 'Demo@123'), json={})
    print('verifier approves step2 ->', r.status_code, r.json())
    gid = r.json().get('gratification_id')
    if gid:
        g = c.get(f'/api/v1/gratification/{gid}', headers=admin).json()
        print('gratification ->', 'type:', g.get('type_code'), '| value:', g.get('scheme_value'), '| status:', g.get('status'))
