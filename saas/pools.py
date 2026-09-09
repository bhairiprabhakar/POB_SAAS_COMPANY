"""
Per-tenant connection pool registry.

Pools are created lazily the first time a tenant is accessed and cached by
database name. Idle pools are closed by a background sweep so we don't hold
one pool per company forever (each pool holds up to TENANT_POOL_MAX sockets).
"""
import logging
import threading
import time

from . import config
from . import db_utils
from . import db_creds

log = logging.getLogger("saas.pools")

# RLock: get_tenant_pool re-enters when _try_migrate calls get_tenant_pool.
_lock = threading.RLock()
_pools: dict[str, "Pool"] = {}
_last_used: dict[str, float] = {}
_migration_failed: set[str] = set()  # tenants whose migration failed; retry once
_migration_retried: set[str] = set()  # already retried; do not retry again


def get_tenant_pool(tenant_db: str):
    with _lock:
        now = time.time()
        pool = _pools.get(tenant_db)
        if pool is None:
            # Tenants with dedicated per-company credentials connect with
            # those; anything without them yet (e.g. pre-backfill) falls back
            # to the shared platform DB_USER / DB_PASSWORD.
            # minconn=0: a pool must not pin a Postgres connection just because
            # a tenant was touched once. With one pool per company and
            # minconn=1, 35 tenants held 35+ idle connections and the server hit
            # "remaining connection slots are reserved" on max_connections=100.
            creds = db_creds.lookup_tenant_credentials(tenant_db)
            if creds:
                user, password = creds
                pool = db_utils.make_pool(tenant_db, minconn=0, maxconn=config.TENANT_POOL_MAX,
                                          user=user, password=password)
            else:
                pool = db_utils.make_pool(tenant_db, minconn=0, maxconn=config.TENANT_POOL_MAX)
            _pools[tenant_db] = pool
            _migration_failed.add(tenant_db)  # assume needs migration until proven otherwise
            # Upgrade existing tenants lazily on first open.
            _try_migrate(tenant_db)
        elif tenant_db in _migration_failed and tenant_db not in _migration_retried:
            # Previous migration attempt failed; retry exactly once.
            _migration_retried.add(tenant_db)
            _try_migrate(tenant_db)
        _last_used[tenant_db] = now
        return pool


def admin_tenant_conn(tenant_db: str):
    """Direct, unpooled connection to *tenant_db* using the platform admin role.

    Migrations must NOT run as the per-company role. Tenant tables are owned by
    the platform user, and `db_creds.grant_tenant_access` gives the company role
    GRANT ALL PRIVILEGES -- which is not ownership. `ALTER TABLE ... ADD COLUMN`
    requires ownership, so every schema patch failed with "must be owner of
    table" and was swallowed as a skipped statement.

    Day-to-day traffic still uses the least-privilege company role; only DDL
    runs with the admin credentials.
    """
    import psycopg2
    return psycopg2.connect(dbname=tenant_db, user=config.DB_USER,
                            password=config.DB_PASSWORD, host=config.DB_HOST,
                            port=config.DB_PORT, connect_timeout=5)


def _try_migrate(tenant_db: str):
    """Run pending migrations for *tenant_db* on an admin connection.

    Opened directly rather than through the pool, which also avoids the
    recursive get_tenant_pool → ensure_migrated → get_tenant_conn →
    get_tenant_pool loop that the old code triggered.
    """
    try:
        from .migrations import apply_tenant_migrations, _migrated
        conn = admin_tenant_conn(tenant_db)
        try:
            if tenant_db not in _migrated:
                apply_tenant_migrations(conn)
                _migrated.add(tenant_db)
            _migration_failed.discard(tenant_db)
        finally:
            conn.close()
    except Exception:
        log.exception("Migration failed for tenant %s", tenant_db)


def get_tenant_conn(tenant_db: str):
    return get_tenant_pool(tenant_db).get_conn()


def close_tenant_pool(tenant_db: str):
    with _lock:
        pool = _pools.pop(tenant_db, None)
        _last_used.pop(tenant_db, None)
        _migration_failed.discard(tenant_db)
        _migration_retried.discard(tenant_db)
    if pool:
        pool.close_all()


def sweep_idle_pools():
    """Close pools idle for longer than TENANT_POOL_IDLE_TTL_MIN."""
    with _lock:
        cutoff = time.time() - config.TENANT_POOL_IDLE_TTL_MIN * 60
        idle = [name for name, ts in _last_used.items() if ts < cutoff]
        for name in idle:
            pool = _pools.pop(name, None)
            _last_used.pop(name, None)
            _migration_failed.discard(name)
            _migration_retried.discard(name)
            if pool:
                pool.close_all()


def start_idle_sweeper(interval_seconds: int = 300):
    import threading

    def _loop():
        while True:
            try:
                sweep_idle_pools()
            except Exception:
                pass
            time.sleep(interval_seconds)

    t = threading.Thread(target=_loop, daemon=True, name="tenant-pool-sweeper")
    t.start()
    return t
