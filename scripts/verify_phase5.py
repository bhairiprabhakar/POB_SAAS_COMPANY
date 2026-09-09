"""Phase 5 E2E verification: notification channels + template builder +
enriched audit trail (IP/user-agent captured, before/after state)."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from saas.main import app

UNIQ = str(int(time.time()))


def login(c, code, u, p, **headers):
    r = c.post('/api/v1/auth/login', json={'company_code': code, 'username': u, 'password': p},
               headers=headers or None)
    assert r.status_code < 400, r.json()
    return {'Authorization': 'Bearer ' + r.json()['access_token']}


with TestClient(app) as c:
    CODE = 'DEMO1234'
    admin = login(c, CODE, 'company_admin', 'Admin@123',
                  **{'X-Forwarded-For': '203.0.113.7', 'User-Agent': 'VerifyBot/1.0', 'X-Lat': '18.5204', 'X-Lng': '73.8567'})
    mr = login(c, CODE, 'mr.amit', 'Demo@123')

    # ── 1. template builder ────────────────────────────────────────────────
    r = c.post('/api/v1/notifications/templates', headers=admin, json={
        'code': f'promo.phase5.{UNIQ}', 'channel': 'inapp', 'subject': 'Campaign {campaign}',
        'body': 'Hi {full_name}, POB {pob_id} approved for campaign {campaign}.',
        'variables': ['full_name', 'pob_id', 'campaign'], 'active': True})
    print('template create ->', r.status_code, r.json())
    tid = r.json()['id']
    tpls = c.get('/api/v1/notifications/templates', headers=admin).json()['items']
    mine = [t for t in tpls if t['id'] == tid][0]
    print('template listed ->', mine['code'], '| vars:', mine['variables'])
    r = c.post(f'/api/v1/notifications/templates/{tid}/test', headers=admin, json={
        'variables': {'full_name': 'Amit', 'pob_id': 9, 'campaign': 'OCR Manual'}})
    print('template test ->', r.status_code, '| rendered:', r.json().get('rendered'))
    r = c.put(f'/api/v1/notifications/templates/{tid}', headers=admin,
              json={'body': 'Hi {full_name}, updated body.'})
    print('template update ->', r.status_code, r.json())

    # ── 2. enable extra channels in company settings ───────────────────────
    r = c.put('/api/v1/company/settings', headers=admin, json={
        'notifications': {
            'provider': 'console', 'channels': ['inapp', 'email', 'sms', 'whatsapp'],
            'whatsapp_api_key': '', 'sms_api_key': '',
        }})
    print('settings save ->', r.status_code)
    st = c.get('/api/v1/company/settings', headers=admin).json()
    print('settings channels ->', st['notifications'].get('channels'))

    # ── 3. trigger a real notification (POB submit -> pob.submitted) ───────
    cid = c.post('/api/v1/campaigns', headers=admin, json={
        'name': f'Notify Camp {UNIQ}', 'brand_id': 1, 'status': 'active',
        'scheme_type': 'cashback', 'upload_roles': 'mr', 'payout_cycle': 'instant',
        'auto_verify': False,
        'rules': [{'name': 'R', 'priority': 1, 'conditions': [],
                   'then_action': 'cashback', 'value': 50, 'active': True}]}).json()['id']
    pid = c.post('/api/v1/products', headers=admin, json={
        'campaign_id': cid, 'name': 'Notify Prod', 'sku': f'N-{UNIQ}', 'ptr': 100, 'mrp': 150}).json()['id']
    r = c.post('/api/v1/pob/submit', headers=mr, data={
        'campaign_id': cid, 'product_id': pid, 'chemist_id': 1,
        'quantity': 5, 'ptr': 100, 'invoice_amount': 500, 'pob_amount': 500,
        'invoice_number': f'INV-N-{UNIQ}', 'invoice_date': '2026-08-01'})
    print('submit (notification trigger) ->', r.status_code)
    feed = c.get('/api/v1/notifications/', headers=mr).json()
    latest = feed['items'][0]
    print('notification feed ->', latest['type'], '| title:', latest['title'], '| unread:', feed['unread'])

    # ── 4. enriched audit trail ────────────────────────────────────────────
    al = c.get('/api/v1/audit/logs', headers=admin, params={'action': 'auth.login'}).json()
    row = next(i for i in al['items'] if (i.get('user_agent') or '') == 'VerifyBot/1.0')
    print('audit login -> ip:', row.get('ip'), '| ua:', row.get('user_agent'), '| gps:', row.get('gps_lat'))
    # campaign update logs before/after
    c.put(f'/api/v1/campaigns/{cid}', headers=admin, json={'status': 'draft'})
    al2 = c.get('/api/v1/audit/logs', headers=admin, params={'action': 'campaign.update'}).json()
    row2 = al2['items'][0]
    print('audit campaign.update -> before.status:', row2.get('before_state', {}).get('status'),
          '| after.status:', row2.get('after_state', {}).get('status'),
          '| ua:', row2.get('user_agent'))
    # verification approve with request meta
    vid = r.json()['verification_id']
    verifier = login(c, CODE, 'verifier.kavita', 'Demo@123',
                     **{'X-Forwarded-For': '198.51.100.9', 'User-Agent': 'VerifyBot/2.0'})
    c.post(f'/api/v1/verification/{vid}/approve', headers=verifier, json={'note': 'ok'})
    al3 = c.get('/api/v1/audit/logs', headers=admin, params={'action': 'verification.approve'}).json()
    row3 = al3['items'][0]
    print('audit verification.approve -> ip:', row3.get('ip'), '| detail.gratification_id present:',
          'gratification_id' in (row3.get('detail') or {}))
