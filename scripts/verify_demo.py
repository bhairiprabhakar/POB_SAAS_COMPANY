import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from saas.main import app

with TestClient(app) as c:
    r = c.post('/api/v1/auth/login', json={'company_code': 'DEMO1234', 'username': 'company_admin', 'password': 'Admin@123'})
    ok = r.status_code < 400
    print('demo admin login:', r.status_code, r.json().get('user', {}).get('role') if ok else r.json())
    if ok:
        h = {'Authorization': 'Bearer ' + r.json()['access_token']}
        print('brands:', c.get('/api/v1/brands', headers=h).json()['items'][0]['name'])
        print('campaigns:', [x['name'] for x in c.get('/api/v1/campaigns', headers=h).json()['items']])
        print('chemists:', c.get('/api/v1/chemists', headers=h).json()['items'][0]['name'])
        print('users total:', c.get('/api/v1/users', headers=h).json()['total'])
        print('roles:', len(c.get('/api/v1/roles', headers=h).json()['items']))
    for u in ('mr.amit', 'asm.rahul', 'verifier.kavita', 'finance.sunil'):
        r2 = c.post('/api/v1/auth/login', json={'company_code': 'DEMO1234', 'username': u, 'password': 'Demo@123'})
        if r2.status_code < 400:
            me = c.get('/api/v1/auth/me', headers={'Authorization': 'Bearer ' + r2.json()['access_token']}).json()
            print(f'{u} login OK, role={me["role"]}, perms={len(me["permissions"])}')
        else:
            print(f'{u} login FAILED:', r2.json())
