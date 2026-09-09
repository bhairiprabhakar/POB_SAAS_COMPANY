"""Phase 4 E2E verification: OCR auto-verification + manual-review fallback.

Two campaigns:
  1. auto_verify=True  -> text-stub invoice matches -> POB verified instantly,
     gratification created, no approval chain, no queue item.
  2. auto_verify=False (or mismatched stub) -> routes to the manual queue.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from saas.main import app

UNIQ = str(int(time.time()))


def login(c, code, u, p):
    r = c.post('/api/v1/auth/login', json={'company_code': code, 'username': u, 'password': p})
    assert r.status_code < 400, r.json()
    return {'Authorization': 'Bearer ' + r.json()['access_token']}


def invoice_stub(inv_no, amount, date):
    return (f"Invoice Number: {inv_no}\n"
            f"Date: {date}\n"
            f"Amount: {amount}\n"
            f"Chemist: LifeCare Pharmacy\n").encode()


with TestClient(app) as c:
    CODE = 'DEMO1234'
    admin = login(c, CODE, 'company_admin', 'Admin@123')
    mr = login(c, CODE, 'mr.amit', 'Demo@123')
    verifier = login(c, CODE, 'verifier.kavita', 'Demo@123')

    # ── 0. register chemist (full registration flow) ───────────────────────
    chem_id = c.post('/api/v1/chemists', headers=admin, json={
        'name': f'LifeCare Pharmacy {UNIQ}', 'shop_name': 'LifeCare Pharmacy',
        'gst': f'27AAAPL{UNIQ}1ZC', 'mobile': '9880098800', 'city': 'Pune',
        'district': 'Pune', 'state': 'Maharashtra', 'pin': '411001',
        'category': 'Retail', 'status': 'active'}).json()['id']
    print('chemist registered ->', chem_id)
    ch_listed = c.get('/api/v1/chemists', headers=admin, params={'city': 'Pune'}).json()['items']
    print('chemists listed ->', len(ch_listed))

    # ── 1. auto-verify campaign (matches -> instant) ───────────────────────
    cid_a = c.post('/api/v1/campaigns', headers=admin, json={
        'name': 'OCR Auto Campaign', 'brand_id': 1, 'status': 'active',
        'scheme_type': 'cashback', 'upload_roles': 'mr',
        'payout_cycle': 'instant', 'auto_verify': True, 'auto_verify_confidence': 0.9,
        'rules': [
            {'name': 'Cash 200', 'priority': 1,
             'conditions': [{'field': 'invoice_amount', 'op': '>=', 'value': 1000}],
             'then_action': 'cashback', 'value': 200, 'active': True},
        ]}).json()['id']
    pid_a = c.post('/api/v1/products', headers=admin, json={
        'campaign_id': cid_a, 'name': 'OCR Product A', 'sku': 'OCR-A', 'ptr': 100, 'mrp': 140}).json()['id']
    r = c.post('/api/v1/pob/submit', headers=mr, data={
        'campaign_id': cid_a, 'product_id': pid_a, 'chemist_id': chem_id,
        'quantity': 25, 'ptr': 100, 'invoice_amount': 2500, 'pob_amount': 2500,
        'invoice_number': f'INV-OCR-{UNIQ}', 'invoice_date': '2026-08-01'},
        files={'invoice': ('invoice.txt', invoice_stub(f'INV-OCR-{UNIQ}', 2500, '2026-08-01'), 'text/plain')})
    print('auto submit ->', r.status_code, r.json())
    assert r.json()['auto_verified'] is True and r.json()['status'] == 'verified', r.json()
    gid = r.json()['verification_id']  # not used; fetch via gratification endpoint
    pob_a = c.get('/api/v1/pob/mine', headers=mr).json()['items'][0]
    print('pob status ->', pob_a['status'], '| auto_verified:', pob_a['auto_verified'],
          '| confidence:', pob_a['confidence'])
    ocr = c.get('/api/v1/pob/ocr', headers=admin).json()['items']
    print('ocr extractions ->', len(ocr), '| last auto_approved:', ocr[0]['auto_approved'],
          '| engine:', ocr[0]['engine'])
    g = c.get('/api/v1/gratification', headers=admin, params={'campaign_id': cid_a}).json()['items'][0]
    print('instant gratification ->', g['type_code'], g['scheme_value'], '| status:', g['status'])

    # ── 2. manual-review campaign (no auto_verify) ─────────────────────────
    cid_m = c.post('/api/v1/campaigns', headers=admin, json={
        'name': 'OCR Manual Campaign', 'brand_id': 1, 'status': 'active',
        'scheme_type': 'cashback', 'upload_roles': 'mr',
        'payout_cycle': 'instant', 'auto_verify': False,
        'rules': [
            {'name': 'Cash 150', 'priority': 1,
             'conditions': [], 'then_action': 'cashback', 'value': 150, 'active': True},
        ]}).json()['id']
    pid_m = c.post('/api/v1/products', headers=admin, json={
        'campaign_id': cid_m, 'name': 'OCR Product M', 'sku': 'OCR-M', 'ptr': 100, 'mrp': 140}).json()['id']
    r = c.post('/api/v1/pob/submit', headers=mr, data={
        'campaign_id': cid_m, 'product_id': pid_m, 'chemist_id': chem_id,
        'quantity': 10, 'ptr': 100, 'invoice_amount': 1000, 'pob_amount': 1000,
        'invoice_number': f'INV-OCR-M-{UNIQ}', 'invoice_date': '2026-08-01'},
        files={'invoice': ('invoice.txt', invoice_stub(f'INV-OCR-M-{UNIQ}', 1000, '2026-08-01'), 'text/plain')})
    print('manual submit ->', r.status_code, r.json())
    assert r.json()['auto_verified'] is False and r.json()['status'] == 'pending_verification', r.json()
    vid = r.json()['verification_id']
    q = c.get('/api/v1/verification/queue', headers=verifier, params={'status': 'pending'}).json()
    in_queue = any(i['pob_id'] == r.json()['pob_id'] for i in q['items'])
    print('in manual queue ->', in_queue)
    r = c.post(f'/api/v1/verification/{vid}/approve', headers=verifier, json={'note': 'ok'})
    print('manual approve ->', r.status_code, r.json())

    # ── 3. auto_verify campaign but MISMATCHED stub -> manual review ───────
    r = c.post('/api/v1/pob/submit', headers=mr, data={
        'campaign_id': cid_a, 'product_id': pid_a, 'chemist_id': chem_id,
        'quantity': 5, 'ptr': 100, 'invoice_amount': 500, 'pob_amount': 500,
        'invoice_number': f'INV-OCR-X-{UNIQ}', 'invoice_date': '2026-08-01'},
        files={'invoice': ('invoice.txt', invoice_stub(f'INV-OCR-X-{UNIQ}', 40, '2026-07-15'), 'text/plain')})
    print('mismatch submit ->', r.status_code, r.json())
    assert r.json()['auto_verified'] is False, r.json()
    vd = c.get(f"/api/v1/verification/{r.json()['verification_id']}", headers=verifier).json()
    print('mismatch flagged ->', 'ocr records:', len(vd.get('ocr', [])), '| queue status:', vd['v_status'])
