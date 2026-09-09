import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from io import BytesIO
from openpyxl import Workbook
from fastapi.testclient import TestClient
from saas.main import app

with TestClient(app) as c:
    d = c.post('/api/v1/auth/login', json={'company_code': 'DEMO1234', 'username': 'company_admin', 'password': 'Admin@123'}).json()
    h = {'Authorization': 'Bearer ' + d['access_token']}

    tpl = c.get('/api/v1/hierarchy/bulk-template', headers=h)
    print('template download ->', tpl.status_code, tpl.headers.get('content-type'))

    wb = Workbook(); ws = wb.active
    ws.append(["username", "full_name", "password", "email", "mobile", "employee_id",
               "hierarchy_level", "role", "parent_username", "region", "area", "territory"])
    ws.append(["mr.testuser", "Test User", "Test@123", "test@demopharma.in", "9830000099",
               "EMP99", "MR", "mr", "mr.amit", "Mumbai", "West", "T1"])
    ws.append(["", "", "", "", "", "", "", "", "", "", "", ""])
    ws.append(["mr.baduser", "No Role User", "Test@123", "", "", "", "", "nope_role", "", "", "", ""])
    buf = BytesIO(); wb.save(buf); buf.seek(0)
    up = c.post('/api/v1/users/bulk-upload', headers=h, files={'file': ('users.xlsx', buf.getvalue(),
                'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')})
    print('upload ->', up.status_code, up.json())

    users = c.get('/api/v1/users', headers=h).json()['items']
    tu = [u['username'] for u in users if u['username'].startswith('mr.testuser')]
    print('created user in list:', tu)

    if tu:
        uid = next(u['id'] for u in users if u['username'] == 'mr.testuser')
        c.delete(f'/api/v1/users/{uid}', headers=h)
        print('cleaned up test user', uid)
