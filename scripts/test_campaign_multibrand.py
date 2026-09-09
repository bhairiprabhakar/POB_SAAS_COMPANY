"""
Verify superadmin campaign multi-brand + date handling.
- create campaign with brand_ids list -> brand_id = first, brand_ids persisted
- get_campaign returns brand_ids + brand_names
- list returns brand_ids + brand_names
- update can add a brand mid-campaign
- brand filter matches secondary brand
"""
import sys
sys.path.insert(0, r'D:\POB_SAAS')
from fastapi.testclient import TestClient
from saas.main import app

COMPANY_CODE = 'BITE3878'
PASSED = []


def check(name, ok, detail=''):
    PASSED.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ''))


with TestClient(app) as client:
    r = client.post('/api/v1/auth/superadmin/login', json={'username': 'superadmin', 'password': 'Pob_Saas@2026'})
    check('sa login', r.status_code == 200)
    h = {'Authorization': 'Bearer ' + r.json()['access_token']}
    r = client.get('/api/v1/superadmin/companies', headers=h)
    cid = [c for c in r.json()['items'] if c['code'] == COMPANY_CODE][0]['id']

    # create brands
    brand_ids = []
    for name in ('Brand Alpha', 'Brand Beta', 'Brand Gamma'):
        r = client.post(f'/api/v1/superadmin/companies/{cid}/brands', headers=h, json={'name': name})
        brand_ids.append(r.json()['id'])

    camp_id = None
    try:
        r = client.post(f'/api/v1/superadmin/companies/{cid}/campaigns', headers=h,
                        json={'name': 'Multi Brand Test', 'status': 'draft', 'scheme_type': 'others',
                              'start_date': '2026-08-14', 'end_date': '2026-09-30',
                              'brand_ids': [brand_ids[0], brand_ids[1]]})
        check('create multi-brand campaign', r.status_code == 200, r.text[:200])
        camp_id = r.json()['id']

        r = client.get(f'/api/v1/superadmin/companies/{cid}/campaigns/{camp_id}', headers=h)
        d = r.json()
        check('brand_id = first selected', d.get('brand_id') == brand_ids[0], f"got {d.get('brand_id')}")
        check('brand_ids persisted', sorted(d.get('brand_ids') or []) == sorted([brand_ids[0], brand_ids[1]]),
              f"got {d.get('brand_ids')}")
        check('brand_names joined', sorted(d.get('brand_names') or []) == ['Brand Alpha', 'Brand Beta'],
              f"got {d.get('brand_names')}")
        check('start_date roundtrip', d.get('start_date') == '2026-08-14', f"got {d.get('start_date')}")

        # list includes new fields
        r = client.get(f'/api/v1/superadmin/companies/{cid}/campaigns', headers=h)
        row = [c for c in r.json()['items'] if c['id'] == camp_id][0]
        check('list brand_ids', sorted(row.get('brand_ids') or []) == sorted([brand_ids[0], brand_ids[1]]),
              f"got {row.get('brand_ids')}")
        check('list brand_names', sorted(row.get('brand_names') or []) == ['Brand Alpha', 'Brand Beta'],
              f"got {row.get('brand_names')}")

        # filter by the SECONDARY brand id
        r = client.get(f'/api/v1/superadmin/companies/{cid}/campaigns?brand_id={brand_ids[1]}', headers=h)
        ids = [c['id'] for c in r.json()['items']]
        check('filter by secondary brand', camp_id in ids, f"ids={ids}")

        # update mid-campaign: add a third brand
        r = client.put(f'/api/v1/superadmin/companies/{cid}/campaigns/{camp_id}', headers=h,
                       json={'brand_ids': [brand_ids[0], brand_ids[1], brand_ids[2]]})
        check('update add brand', r.status_code == 200, r.text[:200])
        r = client.get(f'/api/v1/superadmin/companies/{cid}/campaigns/{camp_id}', headers=h)
        d = r.json()
        check('brand_ids after update', sorted(d.get('brand_ids') or []) == sorted(brand_ids),
              f"got {d.get('brand_ids')}")
        check('brand_id synced to first', d.get('brand_id') == brand_ids[0], f"got {d.get('brand_id')}")

        # single brand mode still works
        r = client.post(f'/api/v1/superadmin/companies/{cid}/campaigns', headers=h,
                        json={'name': 'Single Brand Test', 'status': 'draft', 'scheme_type': 'others',
                              'brand_ids': [brand_ids[2]]})
        single_id = r.json()['id']
        r = client.get(f'/api/v1/superadmin/companies/{cid}/campaigns/{single_id}', headers=h)
        d = r.json()
        check('single brand mode', d.get('brand_id') == brand_ids[2]
              and d.get('brand_ids') == [brand_ids[2]] and d.get('brand_names') == ['Brand Gamma'],
              f"got brand_id={d.get('brand_id')} brand_ids={d.get('brand_ids')}")

        # tenant side sees brand_names
        r = client.post('/api/v1/auth/login', json={'company_code': COMPANY_CODE, 'username': 'bi_admin', 'password': 'Admin@123'})
        th = {'Authorization': 'Bearer ' + r.json()['access_token']}
        r = client.get(f'/api/v1/campaigns/{camp_id}', headers=th)
        td = r.json()
        check('tenant brand_names', sorted(td.get('brand_names') or []) == ['Brand Alpha', 'Brand Beta', 'Brand Gamma'],
              f"got {td.get('brand_names')}")
        r = client.get('/api/v1/campaigns/tracking', headers=th)
        tr = [c for g in (r.json().get('items') or []) for c in (g.get('campaigns') or []) if c['id'] == camp_id]
        check('tenant tracking brand_names', tr and sorted(tr[0].get('brand_names') or []) == ['Brand Alpha', 'Brand Beta', 'Brand Gamma'],
              f"got {tr and tr[0].get('brand_names')}")

        # cleanup single
        client.delete(f'/api/v1/superadmin/companies/{cid}/campaigns/{single_id}', headers=h)
    finally:
        if camp_id:
            client.delete(f'/api/v1/superadmin/companies/{cid}/campaigns/{camp_id}', headers=h)
        for bid in brand_ids:
            client.delete(f'/api/v1/superadmin/companies/{cid}/brands/{bid}', headers=h)

failed = [n for n, ok in PASSED if not ok]
print(f"\n{len(PASSED) - len(failed)}/{len(PASSED)} passed")
sys.exit(1 if failed else 0)
