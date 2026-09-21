"""
Control-plane (platform) database: divisions, super admins,
provisioning jobs, audit log, and the super-admin refresh-token store.

This is the ONLY database the super admin talks to directly. Division data
lives in per-division databases created by saas/provision.py.
"""
import datetime as dt

from . import config
from . import db_utils
from saas.passwords import hash_pw

PLATFORM_POOL = db_utils.make_pool(
    config.PLATFORM_DB_NAME,
    minconn=config.PLATFORM_POOL_MIN,
    maxconn=config.PLATFORM_POOL_MAX,
)


def get_db():
    return PLATFORM_POOL.get_conn()


_PLATFORM_DDL = """
CREATE TABLE IF NOT EXISTS super_admins (
    id SERIAL PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL,
    full_name TEXT NOT NULL,
    email TEXT,
    status TEXT DEFAULT 'active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    owner_flag BOOLEAN NOT NULL DEFAULT FALSE,
    company_id INTEGER
);

CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    legal_name TEXT NOT NULL,
    display_name TEXT NOT NULL,
    code TEXT NOT NULL DEFAULT '',
    logo_path TEXT,
    address TEXT,
    city TEXT,
    state TEXT,
    pincode TEXT,
    gstin TEXT,
    contact_number TEXT,
    official_email TEXT,
    website TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS divisions (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    code TEXT UNIQUE NOT NULL,
    description TEXT,
    contact_person TEXT,
    contact_email TEXT,
    contact_mobile TEXT,
    logo_path TEXT,
    status TEXT DEFAULT 'inactive',
    tenant_db_name TEXT UNIQUE,
    provisioned_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_by INTEGER,
    covered_regions TEXT[] NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS provisioning_jobs (
    id SERIAL PRIMARY KEY,
    division_id INTEGER NOT NULL REFERENCES divisions(id) ON DELETE CASCADE,
    job_type TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    error TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS division_db_credentials (
    division_id INTEGER PRIMARY KEY REFERENCES divisions(id) ON DELETE CASCADE,
    tenant_db_name TEXT UNIQUE NOT NULL,
    db_user TEXT NOT NULL,
    db_password_enc TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS division_users (
    id SERIAL PRIMARY KEY,
    username TEXT NOT NULL,
    division_id INTEGER NOT NULL REFERENCES divisions(id) ON DELETE CASCADE,
    tenant_db_name TEXT NOT NULL,
    user_id INTEGER,
    email TEXT,
    mobile TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (username, division_id)
);

CREATE TABLE IF NOT EXISTS recovery_otps (
    id SERIAL PRIMARY KEY,
    contact TEXT NOT NULL,
    purpose TEXT NOT NULL,
    otp_hash TEXT NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    attempts INTEGER DEFAULT 0,
    used BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_recovery_otps_lookup
    ON recovery_otps (contact, purpose, used);

CREATE TABLE IF NOT EXISTS platform_audit_logs (
    id SERIAL PRIMARY KEY,
    super_admin_id INTEGER,
    actor TEXT,
    action TEXT NOT NULL,
    entity_type TEXT,
    entity_id INTEGER,
    detail JSONB DEFAULT '{}'::jsonb,
    ip TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS refresh_tokens (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    token_hash TEXT UNIQUE NOT NULL,
    scope TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    revoked BOOLEAN DEFAULT FALSE,
    revoked_at TIMESTAMP,
    family_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS platform_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    platform_name TEXT NOT NULL DEFAULT 'CampaignOS',
    logo_path TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS platform_notifications (
    id SERIAL PRIMARY KEY,
    super_admin_id INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'system',
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    link TEXT NOT NULL DEFAULT '',
    division_id INTEGER,
    division_name TEXT,
    is_read BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_platform_notif_sa
    ON platform_notifications (super_admin_id, is_read, created_at DESC);

-- Merged document-extraction AI tables (previously in the legacy
-- single-company DB). ai_model_routing / ai_model_pricing are platform-wide
-- superadmin configuration (same URL, same page, every tenant); ai_usage_log
-- records per-extraction token+cost, tenant-annotated via division_id.
CREATE TABLE IF NOT EXISTS ai_model_routing (
    category TEXT PRIMARY KEY,
    model_id TEXT NOT NULL,
    updated_by INTEGER,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ai_model_pricing (
    model_id TEXT PRIMARY KEY,
    label TEXT DEFAULT '',
    input_usd REAL,
    output_usd REAL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ai_usage_log (
    id SERIAL PRIMARY KEY,
    upload_id INTEGER,
    company_id INTEGER,
    division_id INTEGER,
    model_name TEXT NOT NULL,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    thinking_tokens INTEGER DEFAULT 0,
    chunk_count INTEGER DEFAULT 1,
    cost_usd REAL DEFAULT 0,
    cost_inr REAL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ai_usage_log_company
    ON ai_usage_log (company_id, created_at);
CREATE INDEX IF NOT EXISTS idx_ai_usage_log_upload
    ON ai_usage_log (upload_id);
"""

_PLATFORM_MIGRATIONS = """
ALTER TABLE super_admins ADD COLUMN IF NOT EXISTS owner_flag BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE super_admins ADD COLUMN IF NOT EXISTS company_id INTEGER;
ALTER TABLE super_admins ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'full';
UPDATE super_admins SET role='owner' WHERE owner_flag=TRUE AND role='full';
ALTER TABLE divisions ADD COLUMN IF NOT EXISTS covered_regions TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE ai_usage_log ADD COLUMN IF NOT EXISTS user_id INTEGER;
ALTER TABLE ai_usage_log ADD COLUMN IF NOT EXISTS original_filename TEXT;
-- Platform-console role 'division_admin' collided in name with the
-- tenant-level 'division_admin' role (a different concept in a different
-- database) -- renamed to platform_division_admin to remove the ambiguity.
UPDATE super_admins SET role='platform_division_admin' WHERE role='division_admin';
"""


def init_platform_db() -> None:
    """Create the platform database if missing, then create tables + seed."""
    _ensure_database()
    conn = get_db()
    c = conn.cursor()
    c.execute(_PLATFORM_DDL)
    c.execute(_PLATFORM_MIGRATIONS)
    conn.commit()

    _bootstrap_superadmin(conn)
    _bootstrap_platform_settings(conn)
    conn.close()


def _bootstrap_platform_settings(conn):
    """Ensure the singleton platform_settings row exists (id=1)."""
    c = conn.cursor()
    c.execute(
        "INSERT INTO platform_settings (id, platform_name) "
        "VALUES (1, 'CampaignOS') ON CONFLICT (id) DO NOTHING"
    )
    conn.commit()


def _ensure_database():
    """CREATE DATABASE cannot run in a transaction; do it on an autocommit
    connection against the `postgres` maintenance DB."""
    admin = db_utils.admin_conn()
    try:
        c = admin.cursor()
        c.execute("SELECT 1 FROM pg_database WHERE datname=%s", (config.PLATFORM_DB_NAME,))
        if not c.fetchone():
            c.execute(f'CREATE DATABASE "{config.PLATFORM_DB_NAME}"')
        admin.close()
    finally:
        try:
            admin.close()
        except Exception:
            pass


def _bootstrap_superadmin(conn):
    import os
    from . import config
    c = conn.cursor()
    boot = os.environ.get("SUPERADMIN_BOOTSTRAP_PASSWORD")
    if not boot:
        if config._ALLOW_INSECURE_DEFAULTS:
            boot = "superadmin@2025"
            print("WARNING: ALLOW_INSECURE_DEV_DEFAULTS=true -- bootstrapping "
                  "platform superadmin with the default password. Never do "
                  "this in production.")
        else:
            raise RuntimeError(
                "SUPERADMIN_BOOTSTRAP_PASSWORD must be set before the platform "
                "database can be initialized. For local development only, set "
                "ALLOW_INSECURE_DEV_DEFAULTS=true to bypass this check."
            )
    pw = hash_pw(boot)
    c.execute(
        "INSERT INTO super_admins (username, password, full_name, email) "
        "VALUES (%s,%s,%s,%s) ON CONFLICT (username) DO NOTHING",
        ("superadmin", pw, "Platform Super Admin", "admin@pobplatform.local"),
    )
    conn.commit()
