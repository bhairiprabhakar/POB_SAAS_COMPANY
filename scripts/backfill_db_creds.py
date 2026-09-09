"""
Backfill dedicated DB credentials for existing tenant databases.

Every company provisioned before the per-company-password feature was connected
via the shared platform DB_USER / DB_PASSWORD. This script creates a dedicated
LOGIN role + random password for each such tenant, grants it full rights on the
tenant DB, and stores the (encrypted) credentials in the control plane. After
running it, existing tenants connect exactly like newly provisioned ones.

Run:  venv\\Scripts\\python.exe scripts\\backfill_db_creds.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from saas import db_creds, db_utils, platform_db


def _pending_companies():
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute(
            """SELECT co.id, co.tenant_db_name FROM companies co
               LEFT JOIN company_db_credentials cr
                 ON cr.company_id = co.id
               WHERE co.tenant_db_name IS NOT NULL
                 AND co.tenant_db_name <> ''
                 AND cr.company_id IS NULL
               ORDER BY co.id"""
        )
        return c.fetchall()
    finally:
        conn.close()


def main() -> int:
    rows = _pending_companies()
    if not rows:
        print("No companies need backfilling.")
        return 0

    ok = fail = 0
    for company_id, tenant_db in rows:
        role = db_creds.tenant_db_user(tenant_db)
        password = db_creds.generate_password()
        try:
            db_creds.create_tenant_role(tenant_db, password)
            db_creds.store_company_credentials(company_id, tenant_db, role, password)
            # Validate the dedicated credentials actually work.
            conn = db_utils.get_conn(tenant_db, user=role, password=password)
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
            conn.close()
            print(f"[OK]   {tenant_db} -> role {role}")
            ok += 1
        except Exception as exc:
            print(f"[FAIL] {tenant_db}: {exc}")
            fail += 1

    print(f"\nDone: {ok} backfilled, {fail} failed.")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
