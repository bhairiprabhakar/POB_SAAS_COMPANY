"""
Tenant schema migrations.

All patches are idempotent (CREATE TABLE IF NOT EXISTS / ADD COLUMN IF NOT
EXISTS), so they are safe to re-run. A schema_migrations table records which
version a tenant has reached; existing tenants are upgraded lazily the first
time their connection pool is opened, and new tenants run the patches right
after provisioning.
"""
import logging
import threading

from .tenant_schema import TENANT_PATCHES

log = logging.getLogger("saas.migrations")

# Bump this whenever a statement is added to TENANT_PATCHES. Tenants already
# stamped with the previous version return early and would otherwise never see
# the new patch -- that is how campaigns.pob_required went missing on 27 of 35
# tenant databases. Every patch is idempotent, so re-running the whole set is
# safe and is exactly how existing tenants catch up.
SCHEMA_VERSION = "3.4.0"

# RLock: ensure_migrated re-enters via get_tenant_pool → _try_migrate path.
_lock = threading.RLock()
_migrated: set[str] = set()


def _ensure_migrations_table(conn):
    c = conn.cursor()
    c.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               version TEXT PRIMARY KEY,
               applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"""
    )
    conn.commit()


def _split_statements(sql: str) -> list[str]:
    """Split a multi-statement SQL block into individual statements.

    Handles DO $$ ... END $$ blocks (which contain internal semicolons) by
    tracking $$-delimited dollar-quoted strings.  SQL line-comments (--)
    and inline comments after ``;`` are stripped before checking for the
    statement-terminating semicolon.
    """
    stmts: list[str] = []
    buf: list[str] = []
    in_dollar = False

    def _flush():
        nonlocal buf
        if not buf:
            return
        stmt = "\n".join(buf).strip()
        lines = stmt.split("\n")
        lines = [l for l in lines if not l.strip().startswith("--")]
        stmt = "\n".join(lines).strip()
        if stmt:
            stmts.append(stmt)
        buf = []

    def _ends_with_semicolon(stripped: str) -> bool:
        """Check if a stripped line effectively ends with ``;``.

        Ignores trailing inline comments (``-- ...``) and trailing whitespace.
        """
        # Strip trailing inline comment if present.
        idx = stripped.find("--")
        if idx != -1:
            stripped = stripped[:idx].rstrip()
        return stripped.endswith(";")

    for line in sql.split("\n"):
        stripped = line.strip()
        if not in_dollar:
            dq_count = stripped.count("$$")
            if dq_count % 2 == 1:
                in_dollar = True
                buf.append(line)
                continue
            buf.append(line)
            if _ends_with_semicolon(stripped):
                _flush()
        else:
            buf.append(line)
            dq_count = stripped.count("$$")
            if dq_count % 2 == 1:
                in_dollar = False
                if _ends_with_semicolon(stripped):
                    _flush()

    _flush()
    return stmts


def apply_tenant_migrations(conn) -> None:
    """Apply any pending idempotent patches to an existing tenant database.

    Statements are executed individually and committed one-by-one so a single
    failure (e.g. a table owned by a different role) does not undo or block
    the rest of the patches.  Callers must ensure single-threaded access
    (e.g. via ``pools._lock`` or ``_migrated`` guard in ``ensure_migrated``).
    """
    _ensure_migrations_table(conn)
    c = conn.cursor()
    c.execute("SELECT version FROM schema_migrations")
    applied = {row[0] for row in c.fetchall()}
    if SCHEMA_VERSION in applied:
        return

    stmts = _split_statements(TENANT_PATCHES)
    failed = 0
    for stmt in stmts:
        try:
            c.execute(stmt)
            conn.commit()
        except Exception:
            failed += 1
            log.warning("Migration statement skipped for tenant: %s",
                        stmt[:120].replace("\n", " "))
            conn.rollback()

    # Only stamp the version when every statement landed. Recording it after a
    # partial run marked the tenant "done" forever, so the missing columns were
    # never retried and the tenant stayed broken with no retry path.
    if failed:
        log.error("Tenant migration incomplete: %d of %d statements failed; "
                  "not recording version %s so the next open retries.",
                  failed, len(stmts), SCHEMA_VERSION)
        return

    try:
        c.execute(
            "INSERT INTO schema_migrations (version) VALUES (%s) ON CONFLICT (version) DO NOTHING",
            (SCHEMA_VERSION,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def ensure_migrated(tenant_db: str) -> None:
    """Idempotent per-process guard: migrate a tenant at most once per run."""
    if tenant_db in _migrated:
        return
    with _lock:
        if tenant_db in _migrated:
            return
        from .pools import get_tenant_conn
        conn = get_tenant_conn(tenant_db)
        try:
            apply_tenant_migrations(conn)
            _migrated.add(tenant_db)
        finally:
            conn.close()
