"""Phase 6+7 E2E verification: MFA (TOTP), rate limiting + login lockout,
scoped API keys, webhooks (HMAC delivery + retry queue), scheduler (jobs)."""
import hashlib
import hmac
import http.server
import json
import socketserver
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from saas.main import app
from saas import ratelimit, totp
from saas.db_utils import make_pool

UNIQ = str(int(time.time()))


def login(c, code, u, p, **headers):
    r = c.post('/api/v1/auth/login', json={'company_code': code, 'username': u, 'password': p},
               headers=headers or None)
    assert r.status_code < 400, (r.status_code, r.json())
    j = r.json()
    if j.get('mfa_required'):
        code2 = j['mfa_token']
        rr = c.post('/api/v1/auth/mfa/verify',
                    json={'company_code': code, 'username': u, 'mfa_token': code2, 'code': MFA_CODE})
        assert rr.status_code < 400, rr.json()
        return {'Authorization': 'Bearer ' + rr.json()['access_token']}
    return {'Authorization': 'Bearer ' + j['access_token']}


# ── local webhook receiver ──────────────────────────────────────────────────
received = []


class HookHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        received.append({'path': self.path, 'body': body,
                         'headers': {k.lower(): v for k, v in self.headers.items()}})
        self.send_response(200)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def log_message(self, *a):
        pass


httpd = socketserver.TCPServer(('127.0.0.1', 0), HookHandler)
port = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()
HOOK_URL = f'http://127.0.0.1:{port}/hook'
DEAD_URL = f'http://127.0.0.1:{port + 1}/hook'  # nothing listens here

MFA_CODE = None


def tenant_conn():
    return make_pool('pob_cmp_0007', minconn=1, maxconn=5).get_conn()


with TestClient(app) as c:
    CODE = 'DEMO1234'
    admin = login(c, CODE, 'company_admin', 'Admin@123')
    mr = login(c, CODE, 'mr.amit', 'Demo@123')

    # ── 1. MFA lifecycle ───────────────────────────────────────────────────
    st = c.get('/api/v1/auth/mfa/status', headers=admin).json()
    print('mfa status ->', st)
    r = c.post('/api/v1/auth/mfa/setup', headers=admin)
    print('mfa setup ->', r.status_code, '| secret len:', len(r.json()['secret']),
          '| uri has otpauth:', r.json()['otpauth_uri'].startswith('otpauth://'))
    secret = r.json()['secret']
    MFA_CODE = totp.totp(secret)
    r = c.post('/api/v1/auth/mfa/enable', headers=admin, json={'code': MFA_CODE})
    print('mfa enable ->', r.status_code, r.json())

    # login now requires the code
    r = c.post('/api/v1/auth/login', json={'company_code': CODE, 'username': 'company_admin',
                                           'password': 'Admin@123'})
    assert r.status_code == 200 and r.json().get('mfa_required'), r.json()
    print('login mfa gate -> mfa_required:', r.json()['mfa_required'])
    r = c.post('/api/v1/auth/mfa/verify', json={
        'company_code': CODE, 'username': 'company_admin',
        'mfa_token': r.json()['mfa_token'], 'code': MFA_CODE})
    assert r.status_code == 200 and 'access_token' in r.json(), r.json()
    print('mfa verify -> access_token issued, user:', r.json()['user']['username'])
    # wrong code is rejected
    r = c.post('/api/v1/auth/login', json={'company_code': CODE, 'username': 'company_admin',
                                           'password': 'Admin@123'})
    bad = c.post('/api/v1/auth/mfa/verify', json={
        'company_code': CODE, 'username': 'company_admin',
        'mfa_token': r.json()['mfa_token'], 'code': '000000'})
    print('mfa wrong code ->', bad.status_code)
    r = c.post('/api/v1/auth/mfa/disable', headers=admin, json={'code': MFA_CODE})
    print('mfa disable ->', r.status_code, r.json())

    # ── 2. API keys ────────────────────────────────────────────────────────
    r = c.post('/api/v1/apikeys', headers=admin, json={'name': 'e2e-key', 'scopes': ['pob.view']})
    assert r.status_code == 200, r.json()
    key = r.json()['key']
    kid = r.json()['id']
    print('apikey create ->', key.startswith('pob_pob_cmp_0007.'), '| id:', kid)
    r = c.get('/api/v1/pob', headers={'X-API-Key': key})
    print('apikey auth GET /pob ->', r.status_code)
    r = c.get('/api/v1/apikeys', headers={'X-API-Key': key})
    print('apikey scope denied GET /apikeys ->', r.status_code)
    r = c.delete(f'/api/v1/apikeys/{kid}', headers=admin)
    print('apikey revoke ->', r.status_code, r.json())
    r = c.get('/api/v1/pob', headers={'X-API-Key': key})
    print('apikey after revoke ->', r.status_code)
    # invalid scope on create is rejected
    r = c.post('/api/v1/apikeys', headers=admin, json={'name': 'bad', 'scopes': ['does.not.exist']})
    print('apikey bad scope ->', r.status_code)

    # ── 3. webhooks: signed delivery + retry queue ─────────────────────────
    r = c.post('/api/v1/webhooks', headers=admin, json={
        'name': 'live-hook', 'url': HOOK_URL, 'events': ['pob.submitted', 'pob.approved'],
        'secret': 'test-secret'})
    assert r.status_code == 200, r.json()
    wh_live = r.json()['id']
    print('webhook create ->', wh_live, '| secret returned:', r.json()['secret'] == 'test-secret')
    r = c.post('/api/v1/webhooks', headers=admin, json={
        'name': 'dead-hook', 'url': DEAD_URL, 'events': ['pob.submitted']})
    wh_dead = r.json()['id']
    print('webhook create (dead) ->', wh_dead)

    # submit a POB -> both hooks fire; live one gets a signed 200
    cid = c.post('/api/v1/campaigns', headers=admin, json={
        'name': f'Wh Camp {UNIQ}', 'brand_id': 1, 'status': 'active', 'scheme_type': 'cashback',
        'upload_roles': 'mr', 'payout_cycle': 'instant', 'auto_verify': False,
        'rules': [{'name': 'R', 'priority': 1, 'conditions': [],
                   'then_action': 'cashback', 'value': 25, 'active': True}]}).json()['id']
    pid = c.post('/api/v1/products', headers=admin, json={
        'campaign_id': cid, 'name': 'Wh Prod', 'sku': f'W-{UNIQ}', 'ptr': 100, 'mrp': 150}).json()['id']
    r = c.post('/api/v1/pob/submit', headers=mr, data={
        'campaign_id': cid, 'product_id': pid, 'chemist_id': 1,
        'quantity': 3, 'ptr': 100, 'invoice_amount': 300, 'pob_amount': 300,
        'invoice_number': f'INV-W-{UNIQ}', 'invoice_date': '2026-08-01'})
    print('submit ->', r.status_code, r.json()['status'])
    time.sleep(0.5)  # let the async delivery land

    assert received, 'no webhook delivery received'
    got = received[-1]
    body = got['body']
    expect_sig = 'sha256=' + hmac.new(b'test-secret', body, hashlib.sha256).hexdigest()
    print('webhook received ->', got['path'],
          '| signature ok:', got['headers'].get('x-pob-signature') == expect_sig,
          '| event header:', got['headers'].get('x-pob-event'))
    dl = c.get(f'/api/v1/webhooks/{wh_live}/deliveries', headers=admin).json()['items']
    print('webhook deliveries (live) ->', dl[0]['event'], dl[0]['status'], dl[0]['http_status'])

    dl = c.get(f'/api/v1/webhooks/{wh_dead}/deliveries', headers=admin).json()['items']
    print('webhook deliveries (dead) ->', dl[0]['status'])

    # ── 4. scheduler / job queue ───────────────────────────────────────────
    jobs = c.get('/api/v1/jobs', headers=admin).json()['items']
    print('job queue lists ->', len(jobs), 'jobs (retry queued for dead hook)')
    # bring the retry due and tick the scheduler for this tenant
    conn = tenant_conn()
    cur = conn.cursor()
    cur.execute("UPDATE job_queue SET run_at=CURRENT_TIMESTAMP WHERE job_type='webhook.retry'")
    conn.commit()
    conn.close()
    r = c.post('/api/v1/jobs/tick', headers=admin)
    print('jobs/tick ->', r.status_code, '| processed:', r.json().get('processed'))
    dl = c.get(f'/api/v1/webhooks/{wh_dead}/deliveries', headers=admin).json()['items']
    print('dead hook after retry -> attempts:', dl[0]['attempts'], '| status:', dl[0]['status'])

    # a maintenance job runs to completion
    conn = tenant_conn()
    cur = conn.cursor()
    cur.execute("""INSERT INTO job_queue (job_type, payload, status, run_at)
                   VALUES ('maintenance', '{}'::jsonb, 'queued', CURRENT_TIMESTAMP)""")
    conn.commit()
    conn.close()
    r = c.post('/api/v1/jobs/tick', headers=admin)
    jobs = c.get('/api/v1/jobs', headers=admin, params={'status': 'done'}).json()['items']
    print('maintenance job ->', jobs[0]['job_type'], jobs[0]['status'])
    # whole-platform sweep (payout.schedule + followups across all tenants)
    from saas.scheduler import run_scheduler_once
    n = run_scheduler_once()
    print('run_scheduler_once ->', n, 'jobs processed across tenants')

    # ── 5. login lockout ───────────────────────────────────────────────────
    original = ratelimit._login
    ratelimit._login = ratelimit.RateLimiter(3, 900)
    for i in range(3):
        c.post('/api/v1/auth/login', json={'company_code': CODE, 'username': 'mr.amit',
                                           'password': 'wrong'})
    r = c.post('/api/v1/auth/login', json={'company_code': CODE, 'username': 'mr.amit',
                                           'password': 'Demo@123'})
    print('login after 3 failures ->', r.status_code)
    ratelimit._login = original

    # ── 6. generic API throttle ────────────────────────────────────────────
    orig_limiter = ratelimit.api_limiter
    ratelimit.api_limiter = ratelimit.RateLimiter(3, 60)
    codes = []
    for _ in range(4):
        codes.append(c.get('/api/v1/campaigns', headers=admin).status_code)
    print('api throttle ->', codes)
    ratelimit.api_limiter = orig_limiter

httpd.shutdown()
print('PHASE6+7 OK')
