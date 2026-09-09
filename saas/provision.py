"""
Division provisioning: create/deprovision a division's isolated database.

CREATE DATABASE cannot run inside a transaction, so provisioning uses a
one-off autocommit connection to the maintenance DB, then connects to the new
tenant DB directly to apply the schema + seed + bootstrap the division admin.
"""
import os
import re

from . import config
from . import db_utils
from . import db_creds
from .tenant_schema import TENANT_DDL, seed_tenant
from app.security import hash_pw

SCHEMA_VERSION = "1.0.0"


def tenant_db_name(division_id: int, division_name: str | None = None) -> str:
    """Derive a division's tenant database name: <company prefix><division>.

    With the default config this is POB_SAAS_COMPANY_<division-name>, e.g.
    POB_SAAS_COMPANY_North. The division name is slugified to a safe
    Postgres identifier; if it's missing/blank the division id is used as a
    fallback. The result is a valid Postgres identifier (see
    is_valid_db_identifier) and leaves room for the '<db>_user' role name
    under PostgreSQL's 63-byte NAMELEN limit."""
    prefix = config.TENANT_DB_PREFIX or ""
    slug = _slugify_division_name(division_name) if division_name else ""
    if slug:
        limit = 58 - len(prefix)
        if limit < 1:
            limit = 1
        base = f"{prefix}{slug[:limit].rstrip('_')}"
    else:
        base = f"{prefix}{division_id:04d}"
    return base


def _slugify_division_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_")


def resolve_tenant_db_name(tenant_db: str, division_id: int) -> str:
    """Guarantee a unique tenant db name. If a database with the derived name
    already exists (e.g. two divisions share a name), suffix the division id:
    POB_SAAS_COMPANY_North_0003."""
    from . import config
    candidate = tenant_db
    if candidate.lower() == (config.PLATFORM_DB_NAME or "").lower():
        candidate = f"{candidate}_{division_id}"
    return candidate if not _db_exists(candidate) else f"{candidate}_{division_id}"


def _db_exists(dbname: str) -> bool:
    admin = db_utils.admin_conn()
    try:
        c = admin.cursor()
        c.execute("SELECT 1 FROM pg_database WHERE datname=%s", (dbname,))
        return c.fetchone() is not None
    finally:
        try:
            admin.close()
        except Exception:
            pass


def is_valid_db_identifier(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name or ""))


def provision_tenant(division_id: int, admin_username: str, admin_password: str,
                     admin_email: str | None = None, admin_full_name: str | None = None,
                     division_name: str | None = None, division_code: str | None = None) -> str:
    """Provision a new division database. Returns the tenant db name.

    Every division gets its own Postgres database AND its own LOGIN role with a
    randomly generated password (see saas/db_creds.py). The plaintext password
    is only ever stored encrypted (Fernet) in the control plane; all tenant
    connections use the dedicated role instead of the shared DB_PASSWORD."""
    tenant_db = resolve_tenant_db_name(
        tenant_db_name(division_id, division_name), division_id)
    if not is_valid_db_identifier(tenant_db):
        raise ValueError(f"invalid tenant db name: {tenant_db}")

    _record_job(division_id, "provision", "running")

    db_role = db_creds.tenant_db_user(tenant_db)
    db_password = db_creds.generate_password()

    admin = db_utils.admin_conn()
    try:
        c = admin.cursor()
        c.execute("SELECT 1 FROM pg_database WHERE datname=%s", (tenant_db,))
        if not c.fetchone():
            c.execute(f'CREATE DATABASE "{tenant_db}"')
        admin.close()
    finally:
        try:
            admin.close()
        except Exception:
            pass

    db_creds.create_tenant_role(tenant_db, db_password)

    conn = db_utils.get_conn(tenant_db, autocommit=False,
                             user=db_role, password=db_password)
    try:
        cur = conn.cursor()
        cur.execute(TENANT_DDL)
        from .tenant_schema import TENANT_PATCHES
        cur.execute(TENANT_PATCHES)
        conn.commit()
        seed_tenant(conn)

        role_id = _role_id(conn, "division_admin")
        admin_user = admin_full_name or "Division Administrator"
        cur.execute(
            """INSERT INTO users (username, password, full_name, email, role_id, status)
               VALUES (%s,%s,%s,%s,%s,'active')
               ON CONFLICT (username) DO NOTHING
               RETURNING id""",
            (admin_username, hash_pw(admin_password), admin_user, admin_email, role_id),
        )
        row = cur.fetchone()
        admin_uid = row[0] if row else None
        conn.commit()

        # The tenant's own division record. The division login link (code-based
        # boundary check in routers/auth.py) matches this row, and every user /
        # campaign / brand created for the company's single division hangs off
        # it, so the tenant always starts with exactly its own division.
        from .campaign_service import slugify
        div_slug = slugify(division_name or f"Division {division_id}")
        cur.execute(
            """INSERT INTO divisions (name, code, description, status, slug)
               VALUES (%s,%s,%s,'active',%s)
               ON CONFLICT (name) DO UPDATE SET code=EXCLUDED.code, slug=EXCLUDED.slug, status='active'
               RETURNING id""",
            (division_name or f"Division {division_id}",
             division_code or f"DIV{division_id}", None, div_slug),
        )
        internal_div_id = cur.fetchone()[0]
        if admin_uid is not None:
            cur.execute(
                "UPDATE users SET division_id=%s WHERE id=%s AND division_id IS NULL",
                (internal_div_id, admin_uid),
            )
            from .user_index import sync_user
            sync_user(tenant_db, admin_username, admin_uid, division_id=division_id,
                      email=admin_email)
        conn.commit()
    finally:
        conn.close()

    db_creds.store_division_credentials(division_id, tenant_db, db_role, db_password)
    _record_job(division_id, "provision", "success")
    return tenant_db


def deprovision_tenant(division_id: int, tenant_db: str) -> None:
    """Drop a division's database + its dedicated role. Destructive -- only for
    deactivation flows."""
    if not is_valid_db_identifier(tenant_db):
        raise ValueError(f"invalid tenant db name: {tenant_db}")
    _record_job(division_id, "deprovision", "running")
    admin = db_utils.admin_conn()
    try:
        c = admin.cursor()
        c.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s", (tenant_db,))
        c.execute(f'DROP DATABASE IF EXISTS "{tenant_db}"')
        admin.close()
    finally:
        try:
            admin.close()
        except Exception:
            pass
    db_creds.drop_tenant_role(tenant_db)
    db_creds.clear_division_credentials(tenant_db)
    _record_job(division_id, "deprovision", "success")


def _role_id(conn, role_name: str):
    c = conn.cursor()
    c.execute("SELECT id FROM roles WHERE name=%s", (role_name,))
    row = c.fetchone()
    return row[0] if row else None


def _record_job(division_id: int, job_type: str, status: str, error: str | None = None):
    from . import platform_db
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute(
            """UPDATE provisioning_jobs SET status=%s, error=%s, completed_at=CURRENT_TIMESTAMP
               WHERE division_id=%s AND job_type=%s AND status='running'""",
            (status, error, division_id, job_type),
        )
        conn.commit()
    finally:
        conn.close()
