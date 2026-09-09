"""
Backfill the platform-level username -> company index (company_users) from
every existing tenant database.

Run once after deploying the login-without-company-code feature so users in
pre-existing companies can sign in with username + password only:
    venv\\Scripts\\python.exe scripts\\backfill_user_index.py

Usernames are not globally unique, so a username found in several companies is
reported as ambiguous -- those accounts must keep using their company code.

Also mirrors each user's email/mobile into the index so the account-recovery
flow ("forgot company code / username / password") works for pre-existing
tenants; re-run any time after deploying that feature too.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from saas import platform_db, pools
from saas.user_index import sync_user


def _companies():
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT id, code, tenant_db_name FROM companies "
                  "WHERE tenant_db_name IS NOT NULL AND tenant_db_name <> '' ORDER BY id")
        return c.fetchall()
    finally:
        conn.close()


def main() -> int:
    companies = _companies()
    if not companies:
        print("No provisioned companies to index.")
        return 0

    total = indexed = skipped = 0
    ambiguous: dict[str, list[str]] = {}
    for company_id, code, tenant_db in companies:
        try:
            conn = pools.get_tenant_conn(tenant_db)
        except Exception as exc:
            print(f"[FAIL] {code} ({tenant_db}): {exc}")
            skipped += 1
            continue
        try:
            c = conn.cursor()
            c.execute("SELECT id, username, email, mobile FROM users ORDER BY id")
            users = c.fetchall()
        finally:
            conn.close()
        for uid, username, email, mobile in users:
            total += 1
            sync_user(tenant_db, username, uid, email=email, mobile=mobile)
            indexed += 1
        print(f"[OK]   {code} ({tenant_db}): {len(users)} users indexed")

    # Report usernames that exist in more than one company (ambiguous).
    index = {}
    for company_id, code, tenant_db in companies:
        try:
            conn = pools.get_tenant_conn(tenant_db)
            c = conn.cursor()
            c.execute("SELECT username FROM users")
            for (username,) in c.fetchall():
                index.setdefault(username, set()).add(code)
            conn.close()
        except Exception:
            pass
    for username, codes in sorted(index.items()):
        if len(codes) > 1:
            ambiguous[username] = sorted(codes)

    print(f"\nDone: {indexed} user mappings ({skipped} companies failed).")
    if ambiguous:
        print(f"{len(ambiguous)} ambiguous username(s) belong to multiple companies "
              "(company code still required to sign in):")
        for username, codes in ambiguous.items():
            print(f"  {username}: {', '.join(codes)}")
    return 1 if skipped else 0


if __name__ == "__main__":
    sys.exit(main())
