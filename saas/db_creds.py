"""
Per-division database credentials.

Every tenant database gets its own Postgres LOGIN role with a randomly
generated password (instead of all tenants sharing the platform's DB_USER /
DB_PASSWORD). The plaintext password is never persisted: only a Fernet
token (encrypted with a key derived from the JWT secret) is stored in the
control-plane division_db_credentials table.

The key is derived from SECRET_KEY via SHA-256, so rotating SECRET_KEY
invalidates every stored password -- a restore or rotation flow must either
re-encrypt all rows or re-provision the roles.
"""
import base64
import hashlib
import re
import secrets

from cryptography.fernet import Fernet, InvalidToken

from . import config

ROLE_MAX_LEN = 63  # PostgreSQL identifier limit (NAMELEN)


def _fernet() -> Fernet:
    digest = hashlib.sha256((config.JWT_SECRET or "").encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_password(plain: str) -> str:
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_password(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken:
        raise ValueError("Stored DB credential cannot be decrypted (SECRET_KEY changed?)")


def generate_password(length: int = 24) -> str:
    """Strong URL-safe password. Uses chars Postgres accepts unquoted in a
    password connection string; avoids quotes/backslash entirely."""
    alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if pw and not pw[0].isspace():
            return pw


def tenant_db_user(tenant_db: str) -> str:
    """Dedicated role name for a tenant DB. Must be a valid Postgres
    identifier <= 63 chars and must not collide with the platform DB_USER."""
    base = f"{tenant_db}_user"
    return _clip_role_name(base)


def _clip_role_name(name: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    return name[:ROLE_MAX_LEN]


# -- Role provisioning (Postgres superuser / CREATEROLE) --

def create_tenant_role(tenant_db: str, password: str) -> str:
    """Create (or reset) the dedicated LOGIN role for a tenant DB and grant it
    full rights on that database + public schema. Runs against the `postgres`
    maintenance DB and the tenant DB itself as the platform admin. Returns the
    role name."""
    from . import db_utils
    role = tenant_db_user(tenant_db)

    admin = db_utils.admin_conn()
    try:
        c = admin.cursor()
        c.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,))
        exists = c.fetchone() is not None
        if exists:
            c.execute(f'ALTER ROLE "{role}" WITH LOGIN PASSWORD %s', (password,))
        else:
            c.execute(
                f'CREATE ROLE "{role}" WITH LOGIN PASSWORD %s NOSUPERUSER NOCREATEDB NOCREATEROLE',
                (password,),
            )
    finally:
        try:
            admin.close()
        except Exception:
            pass

    conn = db_utils.get_conn(tenant_db)
    try:
        c = conn.cursor()
        c.execute(f'GRANT CONNECT ON DATABASE "{tenant_db}" TO "{role}"')
        c.execute(f'GRANT ALL ON SCHEMA public TO "{role}"')
        c.execute(f'GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO "{role}"')
        c.execute(f'GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO "{role}"')
        c.execute(f'GRANT ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public TO "{role}"')
        c.execute(f'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO "{role}"')
        c.execute(f'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO "{role}"')
        conn.commit()
    finally:
        conn.close()
    return role


def drop_tenant_role(tenant_db: str) -> None:
    """Drop the dedicated role for a tenant DB (after its DB is gone)."""
    from . import db_utils
    role = tenant_db_user(tenant_db)
    admin = db_utils.admin_conn()
    try:
        c = admin.cursor()
        c.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,))
        if c.fetchone():
            c.execute(f'DROP ROLE IF EXISTS "{role}"')
    finally:
        try:
            admin.close()
        except Exception:
            pass


# -- Control-plane persistence --

def store_division_credentials(division_id: int, tenant_db: str, role: str, password: str) -> None:
    from . import platform_db
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute(
            """INSERT INTO division_db_credentials (division_id, tenant_db_name, db_user, db_password_enc)
               VALUES (%s,%s,%s,%s)
               ON CONFLICT (division_id) DO UPDATE
               SET tenant_db_name=EXCLUDED.tenant_db_name, db_user=EXCLUDED.db_user,
                   db_password_enc=EXCLUDED.db_password_enc""",
            (division_id, tenant_db, role, encrypt_password(password)),
        )
        conn.commit()
    finally:
        conn.close()


def lookup_tenant_credentials(tenant_db: str) -> tuple[str, str] | None:
    """Return (db_user, plaintext_password) for a tenant, or None when the
    tenant has no dedicated credentials yet (fall back to shared config)."""
    from . import platform_db
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT db_user, db_password_enc FROM division_db_credentials WHERE tenant_db_name=%s",
            (tenant_db,),
        )
        row = c.fetchone()
        if not row:
            return None
        return row[0], decrypt_password(row[1])
    finally:
        conn.close()


def clear_division_credentials(tenant_db: str) -> None:
    from . import platform_db
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute("DELETE FROM division_db_credentials WHERE tenant_db_name=%s", (tenant_db,))
        conn.commit()
    finally:
        conn.close()
