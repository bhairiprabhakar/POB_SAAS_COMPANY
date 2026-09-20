"""Verify superadmin division console campaign multi-brand + date handling.

- create campaign with brand_ids list -> brand_id = first, brand_ids persisted
- get_campaign returns brand_ids + brand_names
- list returns brand_ids + brand_names
- update can add a brand mid-campaign
- brand filter matches secondary brand
- tenant side (division admin) sees brand_names on get + tracking

Runs FULLY HERMETIC: a scratch control-plane DB is created before the saas
package is imported, a single division is provisioned (fresh tenant DB), and
both scratch DBs are dropped at the end. The live platform is never touched.

Ported in Batch 4 from the companies-era console routes
(/superadmin/companies/{cid}/*) to the current division console
(/superadmin/divisions/{did}/*).

Run:  venv\\Scripts\\python.exe scripts\\test_campaign_multibrand.py
"""
import os
import sys
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# hermetic: point the platform at a scratch control-plane DB BEFORE the saas
# package is imported (config reads this at import time)
SCRATCH_PLATFORM = f"psk_mb_{uuid.uuid4().hex[:10]}"
os.environ["PLATFORM_DB_NAME"] = SCRATCH_PLATFORM

from fastapi.testclient import TestClient  # noqa: E402
from saas.main import app  # noqa: E402
from saas import platform_db  # noqa: E402

platform_db.init_platform_db()

BOOT = os.environ.get("SUPERADMIN_BOOTSTRAP_PASSWORD", "101990")
_TENANT_DB = None
PASSED = []


def check(name, ok, detail=''):
    PASSED.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not ok else ''))


def main():
    global _TENANT_DB
    tag = uuid.uuid4().hex[:6]
    with TestClient(app) as client:
        r = client.post('/api/v1/auth/superadmin/login',
                        json={'username': 'superadmin', 'password': BOOT})
        check('sa login', r.status_code == 200, r.text[:200])
        h = {'Authorization': 'Bearer ' + r.json()['access_token']}

        r = client.post('/api/v1/superadmin/divisions', headers=h, json={
            'name': f'Multi Brand Div {tag}', 'code': f'MB{tag.upper()}',
            'contact_person': 'Test', 'contact_email': f'mb{tag}@test.in',
            'contact_mobile': '9800000000',
            'provision': True, 'admin_username': 'bi_admin',
            'admin_password': 'Admin@123', 'admin_email': f'mb{tag}@admin.in',
        })
        check('create+provision division', r.status_code == 200, r.text[:300])
        div = r.json()
        did = div['id']
        _TENANT_DB = div['tenant_db_name']
        slug = div['code']

        # clear the admin's onboarding flags so login proceeds straight through
        from saas.db_utils import get_conn
        conn = get_conn(_TENANT_DB)
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM divisions ORDER BY id LIMIT 1")
            tenant_division_id = cur.fetchone()[0]
            cur.execute("UPDATE users SET must_change_password=FALSE, "
                        "mfa_setup_required=FALSE, profile_pending=FALSE "
                        "WHERE username='bi_admin'")
            conn.commit()
        finally:
            conn.close()

        # create brands (platform-shared: no division binding)
        brand_ids = []
        for name in ('Brand Alpha', 'Brand Beta', 'Brand Gamma'):
            r = client.post(f'/api/v1/superadmin/divisions/{did}/brands', headers=h,
                            json={'name': name})
            check(f"create brand {name}", r.status_code == 200, r.text[:200])
            brand_ids.append(r.json()['id'])

        camp_id = None
        try:
            r = client.post(f'/api/v1/superadmin/divisions/{did}/campaigns', headers=h,
                            json={'name': 'Multi Brand Test', 'status': 'draft', 'scheme_type': 'others',
                                  'start_date': '2026-08-14', 'end_date': '2026-09-30',
                                  'division_id': tenant_division_id,
                                  'brand_ids': [brand_ids[0], brand_ids[1]]})
            check('create multi-brand campaign', r.status_code == 200, r.text[:200])
            camp_id = r.json()['id']

            r = client.get(f'/api/v1/superadmin/divisions/{did}/campaigns/{camp_id}', headers=h)
            d = r.json()
            check('brand_id = first selected', d.get('brand_id') == brand_ids[0], f"got {d.get('brand_id')}")
            check('brand_ids persisted', sorted(d.get('brand_ids') or []) == sorted([brand_ids[0], brand_ids[1]]),
                  f"got {d.get('brand_ids')}")
            check('brand_names joined', sorted(d.get('brand_names') or []) == ['Brand Alpha', 'Brand Beta'],
                  f"got {d.get('brand_names')}")
            check('start_date roundtrip', str(d.get('start_date') or '')[:10] == '2026-08-14',
                  f"got {d.get('start_date')}")

            # list includes new fields
            r = client.get(f'/api/v1/superadmin/divisions/{did}/campaigns', headers=h)
            row = [c for c in r.json()['items'] if c['id'] == camp_id][0]
            check('list brand_ids', sorted(row.get('brand_ids') or []) == sorted([brand_ids[0], brand_ids[1]]),
                  f"got {row.get('brand_ids')}")
            check('list brand_names', sorted(row.get('brand_names') or []) == ['Brand Alpha', 'Brand Beta'],
                  f"got {row.get('brand_names')}")

            # filter by the SECONDARY brand id
            r = client.get(f'/api/v1/superadmin/divisions/{did}/campaigns?brand_id={brand_ids[1]}', headers=h)
            ids = [c['id'] for c in r.json()['items']]
            check('filter by secondary brand', camp_id in ids, f"ids={ids}")

            # update mid-campaign: add a third brand
            r = client.put(f'/api/v1/superadmin/divisions/{did}/campaigns/{camp_id}', headers=h,
                           json={'brand_ids': [brand_ids[0], brand_ids[1], brand_ids[2]]})
            check('update add brand', r.status_code == 200, r.text[:200])
            r = client.get(f'/api/v1/superadmin/divisions/{did}/campaigns/{camp_id}', headers=h)
            d = r.json()
            check('brand_ids after update', sorted(d.get('brand_ids') or []) == sorted(brand_ids),
                  f"got {d.get('brand_ids')}")
            check('brand_id synced to first', d.get('brand_id') == brand_ids[0], f"got {d.get('brand_id')}")

            # single brand mode still works
            r = client.post(f'/api/v1/superadmin/divisions/{did}/campaigns', headers=h,
                            json={'name': 'Single Brand Test', 'status': 'draft', 'scheme_type': 'others',
                                  'division_id': tenant_division_id,
                                  'brand_ids': [brand_ids[2]]})
            single_id = r.json()['id']
            r = client.get(f'/api/v1/superadmin/divisions/{did}/campaigns/{single_id}', headers=h)
            d = r.json()
            check('single brand mode', d.get('brand_id') == brand_ids[2]
                  and d.get('brand_ids') == [brand_ids[2]] and d.get('brand_names') == ['Brand Gamma'],
                  f"got brand_id={d.get('brand_id')} brand_ids={d.get('brand_ids')}")

            # tenant side sees brand_names
            r = client.post('/api/v1/auth/login',
                            json={'division_slug': slug, 'username': 'bi_admin', 'password': 'Admin@123'})
            check('tenant admin login', r.status_code == 200, r.text[:200])
            th = {'Authorization': 'Bearer ' + r.json()['access_token']}
            r = client.get(f'/api/v1/campaigns/{camp_id}', headers=th)
            td = r.json()
            check('tenant brand_names', r.status_code == 200
                  and sorted(td.get('brand_names') or []) == ['Brand Alpha', 'Brand Beta', 'Brand Gamma'],
                  f"{r.status_code} got {td.get('brand_names')}")
            r = client.get('/api/v1/campaigns/tracking', headers=th)
            tr = [c for g in (r.json().get('items') or []) for c in (g.get('campaigns') or []) if c['id'] == camp_id]
            check('tenant tracking brand_names',
                  tr and sorted(tr[0].get('brand_names') or []) == ['Brand Alpha', 'Brand Beta', 'Brand Gamma'],
                  f"got {tr and tr[0].get('brand_names')}")

            # cleanup single
            client.delete(f'/api/v1/superadmin/divisions/{did}/campaigns/{single_id}', headers=h)
        finally:
            if camp_id:
                client.delete(f'/api/v1/superadmin/divisions/{did}/campaigns/{camp_id}', headers=h)
            for bid in brand_ids:
                client.delete(f'/api/v1/superadmin/divisions/{did}/brands/{bid}', headers=h)

    failed = [n for n, ok in PASSED if not ok]
    print(f"\n{len(PASSED) - len(failed)}/{len(PASSED)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    try:
        main()
    finally:
        # hermetic: drop the scratch tenant + scratch platform DBs
        if _TENANT_DB:
            import psycopg2
            from saas import config
            conn = psycopg2.connect(
                host=config.DB_HOST, port=config.DB_PORT,
                user=config.DB_USER, password=config.DB_PASSWORD, dbname="postgres")
            conn.autocommit = True
            try:
                cur = conn.cursor()
                for name in (_TENANT_DB, SCRATCH_PLATFORM):
                    cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,))
                    if not cur.fetchone():
                        continue
                    cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                                "WHERE datname=%s AND pid<>pg_backend_pid()", (name,))
                    cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
            finally:
                conn.close()