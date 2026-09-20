"""Cross-tenant division isolation.

Each division is a *physically separate* Postgres database (see
saas/deps.py::_resolve_tenant, which derives the tenant DB purely from the
signed JWT -- no client-supplied id ever selects a connection). That makes
most cross-division leaks structurally impossible; this test proves it end
to end rather than re-deriving the architecture argument statically.

Two divisions (A, B) are provisioned in one HERMETIC scratch platform DB. One
row of each of brand/product/campaign/chemist/POB/verification/gratification
is seeded directly in B's own tenant database, each carrying a random tag in
a free-text field. Division A's admin token is then used to try to read those
exact rows (by id where a single-item route exists, by list/search
otherwise). Because A and B are independent fresh databases their primary
keys can coincidentally collide (e.g. both may have a campaign #1), so a
plain 404 is not the only acceptable outcome -- what must NEVER happen is A
getting back B's tagged content. Every check therefore either expects a
403/404, or (if 200) asserts the tagged field does not match B's value.

Run:  venv\\Scripts\\python.exe scripts\\test_division_isolation.py
"""
import os
import sys
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SCRATCH_PLATFORM = f"psk_isol_{uuid.uuid4().hex[:10]}"
os.environ["PLATFORM_DB_NAME"] = SCRATCH_PLATFORM

from fastapi.testclient import TestClient  # noqa: E402
from saas import platform_db  # noqa: E402
from saas.db_utils import get_conn  # noqa: E402
from saas.main import app  # noqa: E402

platform_db.init_platform_db()

BOOT = os.environ.get("SUPERADMIN_BOOTSTRAP_PASSWORD", "101990")
FAILED = []
_TENANT_DBS = []


def check(name, cond, extra=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" -- {extra}" if extra and not cond else ""))
    if not cond:
        FAILED.append(name)


def check_no_leak(name, resp, field, leaked_value):
    """A cross-division fetch must either be denied outright, or -- if it
    happens to hit an unrelated same-id row of the caller's OWN division --
    must not return the other division's tagged content."""
    if resp.status_code in (403, 404):
        check(name, True)
        return
    if resp.status_code == 200:
        body = resp.json()
        got = body.get(field)
        check(name, got != leaked_value, f"200 leaked {field}={got!r}")
        return
    check(name, False, f"unexpected {resp.status_code}: {resp.text[:200]}")


def _provision_division(client, sa_headers, tag, letter):
    admin_user = f"isoadm{tag}{letter}"
    r = client.post("/api/v1/superadmin/divisions", headers=sa_headers, json={
        "name": f"ISO Test Div {tag}{letter}", "code": f"ISO{tag.upper()}{letter.upper()}",
        "contact_person": "Test", "contact_email": f"iso{tag}{letter}@test.in",
        "contact_mobile": "9800000000",
        "provision": True, "admin_username": admin_user,
        "admin_password": "Admin@123", "admin_email": f"iso{tag}{letter}@admin.in",
    })
    check(f"create+provision division {letter}", r.status_code == 200, f"{r.status_code} {r.text[:300]}")
    div = r.json()
    tenant_db = div["tenant_db_name"]
    _TENANT_DBS.append(tenant_db)
    slug = div["code"]

    conn = get_conn(tenant_db)
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE username=%s", (admin_user,))
        admin_id = cur.fetchone()[0]
        cur.execute("UPDATE users SET must_change_password=FALSE, "
                    "mfa_setup_required=FALSE, profile_pending=FALSE WHERE id=%s", (admin_id,))
        conn.commit()
    finally:
        conn.close()

    r = client.post("/api/v1/auth/login",
                    json={"division_slug": slug, "username": admin_user, "password": "Admin@123"})
    check(f"login division {letter} admin", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    token = {"Authorization": f"Bearer {r.json()['access_token']}"}
    return tenant_db, admin_user, admin_id, token


def _seed_division_b_data(tenant_db, admin_id, tag):
    """Insert one row per entity directly into B's own database, each tagged
    with a unique free-text value so a same-id collision in A can never be
    mistaken for a real leak."""
    conn = get_conn(tenant_db)
    try:
        c = conn.cursor()
        c.execute("SELECT id FROM divisions ORDER BY id LIMIT 1")
        division_id = c.fetchone()[0]

        c.execute("INSERT INTO brands (name, division_id) VALUES (%s,%s) RETURNING id",
                  (f"ISO Brand {tag}", division_id))
        brand_id = c.fetchone()[0]

        c.execute("INSERT INTO products (brand_id, division_id, name, ptr, mrp) "
                  "VALUES (%s,%s,%s,100,120) RETURNING id",
                  (brand_id, division_id, f"ISO Product {tag}"))
        product_id = c.fetchone()[0]

        c.execute("INSERT INTO campaigns (name, brand_id, division_id, status) "
                  "VALUES (%s,%s,%s,'active') RETURNING id",
                  (f"ISO Campaign {tag}", brand_id, division_id))
        campaign_id = c.fetchone()[0]

        c.execute("INSERT INTO chemists (name, division_id) VALUES (%s,%s) RETURNING id",
                  (f"ISO Chemist {tag}", division_id))
        chemist_id = c.fetchone()[0]

        c.execute("INSERT INTO pob_activities (user_id, campaign_id, product_id, chemist_id, "
                  "quantity, pob_amount, invoice_number) VALUES (%s,%s,%s,%s,1,100,%s) RETURNING id",
                  (admin_id, campaign_id, product_id, chemist_id, f"ISO-{tag}"))
        pob_id = c.fetchone()[0]

        c.execute("INSERT INTO pob_verifications (pob_id, status, reason) "
                  "VALUES (%s,'pending',%s) RETURNING id",
                  (pob_id, f"ISO reason {tag}"))
        verification_id = c.fetchone()[0]

        c.execute("INSERT INTO gratifications (pob_id, user_id, campaign_id, type_code, voucher_code) "
                  "VALUES (%s,%s,%s,'voucher',%s) RETURNING id",
                  (pob_id, admin_id, campaign_id, f"ISO-{tag}"))
        gratification_id = c.fetchone()[0]

        conn.commit()
        return {
            "brand_id": brand_id, "product_id": product_id, "campaign_id": campaign_id,
            "chemist_id": chemist_id, "pob_id": pob_id, "verification_id": verification_id,
            "gratification_id": gratification_id,
        }
    finally:
        conn.close()


def main():
    tag = uuid.uuid4().hex[:6]
    with TestClient(app) as client:
        r = client.post("/api/v1/auth/superadmin/login",
                        json={"username": "superadmin", "password": BOOT})
        check("superadmin login", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        SA = {"Authorization": f"Bearer {r.json()['access_token']}"}

        _, admin_a, _, token_a = _provision_division(client, SA, tag, "a")
        tenant_b, admin_b, admin_b_id, _ = _provision_division(client, SA, tag, "b")
        ids = _seed_division_b_data(tenant_b, admin_b_id, tag)

        # ---- direct-id reads: A must never see B's tagged content ----------
        r = client.get(f"/api/v1/campaigns/{ids['campaign_id']}", headers=token_a)
        check_no_leak("campaigns: A cannot read B's campaign", r, "name", f"ISO Campaign {tag}")

        r = client.get(f"/api/v1/chemists/{ids['chemist_id']}", headers=token_a)
        check_no_leak("chemists: A cannot read B's chemist", r, "name", f"ISO Chemist {tag}")

        r = client.get(f"/api/v1/pob/{ids['pob_id']}", headers=token_a)
        check_no_leak("pob: A cannot read B's POB", r, "invoice_number", f"ISO-{tag}")

        r = client.get(f"/api/v1/verification/{ids['verification_id']}", headers=token_a)
        check_no_leak("verification: A cannot read B's verification", r, "reason", f"ISO reason {tag}")

        r = client.get(f"/api/v1/gratification/{ids['gratification_id']}", headers=token_a)
        check_no_leak("gratification: A cannot read B's gratification", r, "voucher_code", f"ISO-{tag}")

        r = client.get(f"/api/v1/users/{admin_b_id}", headers=token_a)
        check_no_leak("users: A cannot read B's admin profile", r, "username", admin_b)

        # ---- list/search reads: B's tagged rows must never appear for A ----
        r = client.get("/api/v1/brands", params={"q": f"ISO Brand {tag}"}, headers=token_a)
        check("brands: A's brand list excludes B's brand",
              r.status_code == 200 and all(b.get("name") != f"ISO Brand {tag}" for b in r.json().get("items", [])),
              f"{r.status_code} {r.text[:200]}")

        r = client.get("/api/v1/products", params={"q": f"ISO Product {tag}"}, headers=token_a)
        check("products: A's product list excludes B's product",
              r.status_code == 200 and all(p.get("name") != f"ISO Product {tag}" for p in r.json().get("items", [])),
              f"{r.status_code} {r.text[:200]}")

    print("\n" + ("ALL PASS" if not FAILED else f"{len(FAILED)} FAILED: {FAILED}"))
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    try:
        main()
    finally:
        # hermetic: drop both scratch tenant DBs + the scratch platform DB
        if _TENANT_DBS:
            import psycopg2
            from saas import config
            conn = psycopg2.connect(
                host=config.DB_HOST, port=config.DB_PORT,
                user=config.DB_USER, password=config.DB_PASSWORD, dbname="postgres")
            conn.autocommit = True
            try:
                cur = conn.cursor()
                for name in (*_TENANT_DBS, SCRATCH_PLATFORM):
                    cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,))
                    if not cur.fetchone():
                        continue
                    cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                                "WHERE datname=%s AND pid<>pg_backend_pid()", (name,))
                    cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
            finally:
                conn.close()
