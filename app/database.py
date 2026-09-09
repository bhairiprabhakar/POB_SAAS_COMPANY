"""
DATABASE -- PostgreSQL

NOTE ON ASYNC: this module deliberately keeps the original psycopg2, synchronous
style used by the Flask app rather than rewriting every query to Async SQLAlchemy.
Rationale (documented, not accidental):
  - ~90 routes' worth of business-critical SQL (credits, hierarchy, duplicate
    detection, verification workflow) is far lower-risk to migrate framework-only
    than to simultaneously rewrite to a different DB access style.
  - Every route in this app is dispatched through `app.compat`'s request wrapper
    using `starlette.concurrency.run_in_threadpool`, so these blocking calls do
    NOT block the asyncio event loop -- FastAPI runs them in a worker thread,
    same as it does natively for plain `def` (non-async) path operations.
  - If/when you want true async DB access (e.g. for very high concurrency),
    swap `get_db()`/queries here for `asyncpg` or SQLAlchemy's async engine.
    The rest of the app calls `get_db()` + cursor the same way regardless.
"""
import datetime as _dt_module
import psycopg2
import psycopg2.pool

from .config import DB_CONFIG, DB_POOL_MIN, DB_POOL_MAX, SUPERADMIN_BOOTSTRAP_PASSWORD
from .security import hash_pw

_pool = None


def _get_pool():
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=DB_POOL_MIN, maxconn=DB_POOL_MAX, **DB_CONFIG
        )
    return _pool


class _PooledConnWrapper:
    """Thin wrapper so existing code's conn.close() returns the connection
    to the pool instead of actually closing the socket."""
    def __init__(self, real_conn, pool):
        self._conn = real_conn
        self._pool = pool
        self._closed = False

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        if not self._closed:
            self._closed = True
            self._pool.putconn(self._conn)


def get_db():
    """Returns a connection (pooled). Matches original get_db() signature/behaviour:
    caller does cursor()/execute()/commit()/close() exactly as before."""
    pool = _get_pool()
    real_conn = pool.getconn()
    real_conn.autocommit = False
    return _PooledConnWrapper(real_conn, pool)


def _serialize(v):
    if isinstance(v, (_dt_module.datetime, _dt_module.date)):
        return v.isoformat()
    return v


def dict_row(cursor, row):
    if row is None:
        return None
    columns = [desc[0] for desc in cursor.description]
    return {k: _serialize(v) for k, v in zip(columns, row)}


def fetchone_dict(cursor):
    row = cursor.fetchone()
    return dict_row(cursor, row)


def fetchall_dict(cursor):
    rows = cursor.fetchall()
    if not rows:
        return []
    columns = [desc[0] for desc in cursor.description]
    return [{k: _serialize(v) for k, v in zip(columns, row)} for row in rows]


def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS super_admins (
        id SERIAL PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        full_name TEXT NOT NULL,
        email TEXT,
        status TEXT DEFAULT 'active',
        role TEXT DEFAULT 'superadmin',
        assigned_company_ids TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS companies (
        id SERIAL PRIMARY KEY,
        name TEXT UNIQUE NOT NULL,
        code TEXT UNIQUE NOT NULL,
        short_name TEXT,
        address TEXT,
        city TEXT,
        state TEXT,
        gst_number TEXT,
        drug_license TEXT,
        contact_person TEXT,
        contact_mobile TEXT,
        contact_email TEXT,
        website TEXT,
        plan TEXT DEFAULT 'basic',
        status TEXT DEFAULT 'active',
        max_users INTEGER DEFAULT 100,
        notes TEXT,
        logo_url TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        created_by INTEGER
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS divisions (
        id SERIAL PRIMARY KEY,
        company_id INTEGER NOT NULL REFERENCES companies(id),
        name TEXT NOT NULL,
        code TEXT,
        description TEXT,
        head_user_id INTEGER,
        status TEXT DEFAULT 'active'
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS zones (
        id SERIAL PRIMARY KEY,
        company_id INTEGER NOT NULL REFERENCES companies(id),
        name TEXT NOT NULL,
        code TEXT,
        states TEXT
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        company_id INTEGER NOT NULL REFERENCES companies(id),
        division_id INTEGER,
        zone_id INTEGER,
        username TEXT NOT NULL,
        password TEXT NOT NULL,
        full_name TEXT NOT NULL,
        role TEXT NOT NULL,
        parent_id INTEGER,
        region TEXT,
        area TEXT,
        territory TEXT,
        employee_id TEXT,
        mobile TEXT,
        email TEXT,
        status TEXT DEFAULT 'active',
        joined_date TEXT,
        left_date TEXT,
        successor_id INTEGER,
        last_login TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(company_id, username),
        UNIQUE(company_id, mobile)
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS registrations (
        id SERIAL PRIMARY KEY,
        company_id INTEGER NOT NULL REFERENCES companies(id),
        full_name TEXT NOT NULL,
        proposed_username TEXT,
        password TEXT NOT NULL,
        role TEXT DEFAULT 'mr',
        division_id INTEGER,
        employee_id TEXT,
        mobile TEXT NOT NULL,
        email TEXT,
        region TEXT,
        area TEXT,
        territory TEXT,
        manager_name TEXT,
        status TEXT DEFAULT 'pending',
        applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        reviewed_by INTEGER,
        reviewed_at TEXT,
        reject_reason TEXT
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS uploads (
        id SERIAL PRIMARY KEY,
        company_id INTEGER NOT NULL REFERENCES companies(id),
        division_id INTEGER,
        user_id INTEGER NOT NULL REFERENCES users(id),
        original_filename TEXT,
        stored_filename TEXT,
        file_type TEXT,
        upload_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        status TEXT DEFAULT 'pending',
        error_msg TEXT,
        file_hash TEXT,
        content_fingerprint TEXT
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS extractions (
        id SERIAL PRIMARY KEY,
        upload_id INTEGER UNIQUE NOT NULL REFERENCES uploads(id),
        stockist_name TEXT,
        stockist_gst TEXT,
        stockist_address TEXT,
        bill_number TEXT,
        bill_date TEXT,
        statement_from_date TEXT,
        statement_to_date TEXT,
        total_amount REAL DEFAULT 0,
        total_quantity INTEGER DEFAULT 0,
        discount_percent REAL DEFAULT 0,
        discount_amount REAL DEFAULT 0,
        net_sale REAL DEFAULT 0,
        sgst REAL DEFAULT 0,
        cgst REAL DEFAULT 0,
        invoice_net REAL DEFAULT 0,
        doc_type TEXT DEFAULT 'STATEMENT',
        raw_json TEXT,
        extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS parties (
        id SERIAL PRIMARY KEY,
        extraction_id INTEGER NOT NULL REFERENCES extractions(id),
        name TEXT,
        type TEXT,
        area TEXT,
        dl_number TEXT,
        gst_number TEXT,
        total_quantity INTEGER DEFAULT 0,
        total_amount REAL DEFAULT 0
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS items (
        id SERIAL PRIMARY KEY,
        party_id INTEGER NOT NULL REFERENCES parties(id),
        brand TEXT,
        mfg TEXT,
        pack TEXT,
        batch_no TEXT,
        expiry TEXT,
        hsn_code TEXT,
        quantity INTEGER DEFAULT 0,
        mrp REAL DEFAULT 0,
        unit_rate REAL DEFAULT 0,
        tax_type TEXT,
        discount_percent REAL DEFAULT 0,
        final_amount REAL DEFAULT 0
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS handovers (
        id SERIAL PRIMARY KEY,
        company_id INTEGER NOT NULL,
        from_user_id INTEGER NOT NULL,
        to_user_id INTEGER NOT NULL,
        transfers INTEGER DEFAULT 0,
        notes TEXT,
        done_by INTEGER,
        done_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS activity_logs (
        id SERIAL PRIMARY KEY,
        company_id INTEGER,
        user_id INTEGER,
        actor TEXT,
        action TEXT NOT NULL,
        entity_type TEXT,
        entity_id INTEGER,
        detail TEXT,
        ip TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS registration_links (
        id SERIAL PRIMARY KEY,
        token TEXT UNIQUE NOT NULL,
        created_by INTEGER NOT NULL,
        used_by_company_id INTEGER,
        status TEXT DEFAULT 'active',
        expires_at TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS credits (
        id SERIAL PRIMARY KEY,
        company_id INTEGER UNIQUE NOT NULL REFERENCES companies(id),
        total_credits INTEGER DEFAULT 100,
        used_credits INTEGER DEFAULT 0,
        plan TEXT DEFAULT 'demo',
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS credit_transactions (
        id SERIAL PRIMARY KEY,
        company_id INTEGER NOT NULL,
        division_id INTEGER,
        user_id INTEGER,
        operation_type TEXT NOT NULL,
        credits_used INTEGER NOT NULL DEFAULT 1,
        reference_id INTEGER,
        detail TEXT,
        balance_after INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS credit_requests (
        id SERIAL PRIMARY KEY,
        company_id INTEGER NOT NULL,
        requested_by INTEGER NOT NULL,
        credits_requested INTEGER NOT NULL,
        message TEXT,
        status TEXT DEFAULT 'pending',
        reviewed_by INTEGER,
        reviewed_at TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS manual_verifications (
        id SERIAL PRIMARY KEY,
        upload_id INTEGER NOT NULL REFERENCES uploads(id),
        company_id INTEGER NOT NULL,
        division_id INTEGER,
        status TEXT DEFAULT 'pending',
        assigned_to INTEGER,
        verified_by INTEGER,
        verified_at TEXT,
        notes TEXT,
        excel_downloaded_at TEXT,
        excel_uploaded_at TEXT,
        corrections_json TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS password_reset_requests (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id),
        company_id INTEGER NOT NULL REFERENCES companies(id),
        new_password_hash TEXT NOT NULL,
        requested_ip TEXT,
        status TEXT DEFAULT 'pending',
        reviewed_by INTEGER,
        reviewed_at TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS ai_usage_log (
        id SERIAL PRIMARY KEY,
        upload_id INTEGER,
        company_id INTEGER,
        model_name TEXT NOT NULL,
        input_tokens INTEGER DEFAULT 0,
        output_tokens INTEGER DEFAULT 0,
        thinking_tokens INTEGER DEFAULT 0,
        chunk_count INTEGER DEFAULT 1,
        cost_usd REAL DEFAULT 0,
        cost_inr REAL DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS ai_model_routing (
        category TEXT PRIMARY KEY,
        model_id TEXT NOT NULL,
        updated_by INTEGER,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS ai_model_pricing (
        model_id TEXT PRIMARY KEY,
        label TEXT DEFAULT '',
        input_usd REAL,
        output_usd REAL,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS notification_log (
        id SERIAL PRIMARY KEY,
        company_id INTEGER,
        user_id INTEGER,
        recipient_role TEXT,
        event_type TEXT NOT NULL,
        subject TEXT,
        message TEXT NOT NULL,
        reference_id INTEGER,
        reference_type TEXT,
        is_read BOOLEAN DEFAULT FALSE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    conn.commit()

    migrations = [
        "ALTER TABLE super_admins ADD COLUMN IF NOT EXISTS role TEXT DEFAULT 'superadmin'",
        "ALTER TABLE super_admins ADD COLUMN IF NOT EXISTS assigned_company_ids TEXT DEFAULT ''",
        "ALTER TABLE uploads ADD COLUMN IF NOT EXISTS file_hash TEXT",
        "ALTER TABLE extractions ADD COLUMN IF NOT EXISTS doc_type TEXT DEFAULT 'STATEMENT'",
        "ALTER TABLE manual_verifications ADD COLUMN IF NOT EXISTS assigned_to INTEGER",
        "ALTER TABLE uploads ADD COLUMN IF NOT EXISTS content_fingerprint TEXT",
        "ALTER TABLE uploads ADD COLUMN IF NOT EXISTS rejected_by INTEGER",
        "ALTER TABLE uploads ADD COLUMN IF NOT EXISTS rejected_at TEXT",
        "ALTER TABLE uploads ADD COLUMN IF NOT EXISTS rejection_reason TEXT",
        "ALTER TABLE uploads ADD COLUMN IF NOT EXISTS rejection_type TEXT",
        "ALTER TABLE uploads ADD COLUMN IF NOT EXISTS verification_status TEXT DEFAULT 'ocr_done'",
    ]
    for sql in migrations:
        try:
            c.execute(sql)
            conn.commit()
        except Exception:
            conn.rollback()

    # ── Indexes ──────────────────────────────────────────────────────────────
    # None of these existed before. At 100k+ documents/day, `uploads`,
    # `extractions`, `parties`, `items`, and `activity_logs` will reach tens
    # of millions of rows within months -- every dashboard view, duplicate
    # check, and team/verification list filters by these columns, and
    # without an index each of those becomes a full table scan that gets
    # slower every single day as the tables grow.
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_users_company     ON users(company_id)",
        "CREATE INDEX IF NOT EXISTS idx_users_parent       ON users(parent_id)",
        "CREATE INDEX IF NOT EXISTS idx_users_status       ON users(company_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_uploads_company    ON uploads(company_id)",
        "CREATE INDEX IF NOT EXISTS idx_uploads_user       ON uploads(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_uploads_status     ON uploads(company_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_uploads_date       ON uploads(upload_date)",
        "CREATE INDEX IF NOT EXISTS idx_uploads_hash       ON uploads(company_id, file_hash)",
        "CREATE INDEX IF NOT EXISTS idx_uploads_fingerprint ON uploads(company_id, content_fingerprint)",
        "CREATE INDEX IF NOT EXISTS idx_extractions_upload ON extractions(upload_id)",
        "CREATE INDEX IF NOT EXISTS idx_parties_extraction ON parties(extraction_id)",
        "CREATE INDEX IF NOT EXISTS idx_items_party        ON items(party_id)",
        "CREATE INDEX IF NOT EXISTS idx_mv_status          ON manual_verifications(status)",
        "CREATE INDEX IF NOT EXISTS idx_mv_upload          ON manual_verifications(upload_id)",
        "CREATE INDEX IF NOT EXISTS idx_mv_assigned        ON manual_verifications(assigned_to, status)",
        "CREATE INDEX IF NOT EXISTS idx_mv_company         ON manual_verifications(company_id)",
        "CREATE INDEX IF NOT EXISTS idx_activity_company   ON activity_logs(company_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_activity_entity    ON activity_logs(entity_type, entity_id)",
        "CREATE INDEX IF NOT EXISTS idx_credit_tx_company  ON credit_transactions(company_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_notif_user_unread  ON notification_log(user_id, is_read)",
        "CREATE INDEX IF NOT EXISTS idx_registrations_co   ON registrations(company_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_pwreset_company    ON password_reset_requests(company_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_ai_usage_company   ON ai_usage_log(company_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_ai_usage_upload    ON ai_usage_log(upload_id)",
    ]
    for sql in indexes:
        try:
            c.execute(sql)
            conn.commit()
        except Exception:
            conn.rollback()

    pw = hash_pw(SUPERADMIN_BOOTSTRAP_PASSWORD)
    try:
        c.execute(
            "INSERT INTO super_admins (username,password,full_name,email) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            ('superadmin', pw, 'Super Administrator', 'admin@pharmaanalyzer.com'))
        conn.commit()
        if SUPERADMIN_BOOTSTRAP_PASSWORD == "superadmin@2025":
            print("WARNING: superadmin account bootstrapped with the default password "
                  "'superadmin@2025'. Log in and change it (or set "
                  "SUPERADMIN_BOOTSTRAP_PASSWORD before first run) before going live.")
    except Exception:
        conn.rollback()

    conn.close()
