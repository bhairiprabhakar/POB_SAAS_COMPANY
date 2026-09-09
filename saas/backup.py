"""
Daily backup (Phase 7 audit gap).

Produces a compressed PostgreSQL dump per tenant (plus the control-plane
database) under BACKUP_ROOT/<dbname>/<dbname>_<timestamp>.sql.gz using
`pg_dump`. A retention window keeps the latest BACKUP_RETENTION dumps and
drops older ones.

`run_backup_all()` is driven both by the superadmin route and the scheduler
(once per day). Pure-SQL fallback (`sql_dump`) exists for hosts without the
pg_dump binary so backups never silently fail.
"""
import gzip
import io
import os
import shutil
import subprocess
import time

from . import config

BACKUP_ROOT = os.environ.get("BACKUP_ROOT", os.path.join(config.BASE_DIR, "backups"))
BACKUP_RETENTION = int(os.environ.get("BACKUP_RETENTION", "30"))

os.makedirs(BACKUP_ROOT, exist_ok=True)


def tenant_db_names() -> list[str]:
    from .db_utils import admin_conn
    conn = admin_conn()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT datname FROM pg_database WHERE datname LIKE %s AND datallowconn=TRUE",
            (config.TENANT_DB_PREFIX + "%",),
        )
        return [row[0] for row in c.fetchall()]
    finally:
        conn.close()


def _resolve_creds(dbname: str) -> tuple[str, str] | None:
    """Per-tenant credentials when a tenant has a dedicated role, else None to
    use the shared platform DB_USER / DB_PASSWORD."""
    if dbname.startswith(config.TENANT_DB_PREFIX):
        from .db_creds import lookup_tenant_credentials
        return lookup_tenant_credentials(dbname)
    return None


def _pg_dump(dbname: str) -> bytes:
    """pg_dump the whole database to a gzip'd byte string."""
    creds = _resolve_creds(dbname)
    env = dict(os.environ)
    dump_user = config.DB_USER
    dump_password = config.DB_PASSWORD
    if creds:
        dump_user, dump_password = creds
    if dump_password:
        env["PGPASSWORD"] = dump_password
    cmd = [
        shutil.which("pg_dump") or "pg_dump",
        "-h", config.DB_HOST, "-p", str(config.DB_PORT),
        "-U", dump_user, "--no-owner", "--no-privileges",
        "-d", dbname,
    ]
    out = subprocess.run(cmd, capture_output=True, env=env, timeout=1800, check=False)
    if out.returncode != 0:
        raise RuntimeError(f"pg_dump failed for {dbname}: {out.stderr.decode(errors='replace')[:1000]}")
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=5) as gz:
        gz.write(out.stdout)
    return buf.getvalue()


def _schema_dump(conn) -> str:
    """CREATE TABLE + constraints for a tenant (pure-catalog fallback)."""
    c = conn.cursor()
    c.execute("""SELECT table_name FROM information_schema.tables
                 WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY table_name""")
    tables = [r[0] for r in c.fetchall()]
    parts = []
    for t in tables:
        c.execute("""SELECT column_name, data_type, is_nullable, column_default
                     FROM information_schema.columns
                     WHERE table_schema='public' AND table_name=%s
                     ORDER BY ordinal_position""", (t,))
        coldefs = []
        for name, dtype, nullable, default in c.fetchall():
            d = f'"{name}" {dtype}'
            if default is not None:
                d += f" DEFAULT {default}"
            if nullable == "NO":
                d += " NOT NULL"
            coldefs.append(d)
        parts.append(f"CREATE TABLE {t} (\n  " + ",\n  ".join(coldefs) + "\n);")
    c.execute("""SELECT c.conname, r.relname, pg_get_constraintdef(c.oid)
                 FROM pg_constraint c
                 JOIN pg_class r ON r.oid=c.conrelid
                 JOIN pg_namespace n ON n.oid=r.relnamespace
                 WHERE n.nspname='public' AND c.contype='p'
                 ORDER BY r.relname""")
    for conname, relname, defn in c.fetchall():
        parts.append(f"ALTER TABLE {relname} ADD CONSTRAINT {conname} {defn};")
    return "\n\n".join(parts) + "\n"


def _data_dump(conn) -> bytes:
    """INSERT-based data dump (fallback when pg_dump is missing)."""
    c = conn.cursor()
    c.execute("""SELECT tablename FROM pg_tables
                 WHERE schemaname='public' AND tablename NOT LIKE 'pg_%' ORDER BY tablename""")
    tables = [r[0] for r in c.fetchall()]
    out = io.StringIO()
    for t in tables:
        c.execute(f'SELECT * FROM "{t}"')
        cols = [d[0] for d in c.description]
        out.write(f"\n-- data for {t}\n")
        for row in c.fetchall():
            vals = []
            for v in row:
                if v is None:
                    vals.append("NULL")
                elif isinstance(v, (int, float)):
                    vals.append(str(v))
                else:
                    vals.append("'" + str(v).replace("'", "''") + "'")
            out.write(f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({', '.join(vals)});\n")
    return out.getvalue().encode("utf-8")


def sql_dump(dbname: str) -> bytes:
    """Pure-psycopg2 fallback dump: schema + INSERTs, gzip'd."""
    from .db_utils import get_conn
    creds = _resolve_creds(dbname)
    if creds:
        conn = get_conn(dbname, user=creds[0], password=creds[1])
    else:
        conn = get_conn(dbname)
    try:
        schema = _schema_dump(conn)
        data = _data_dump(conn)
    finally:
        conn.close()
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=5) as gz:
        gz.write(b"-- POB_SAAS backup\n")
        gz.write(schema.encode("utf-8"))
        gz.write(b"\nBEGIN;\n")
        gz.write(data)
        gz.write(b"COMMIT;\n")
    return buf.getvalue()


def backup_db(dbname: str) -> str | None:
    """Dump one database, prune old dumps, return the archive path."""
    if shutil.which("pg_dump"):
        payload = _pg_dump(dbname)
    else:
        payload = sql_dump(dbname)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest_dir = os.path.join(BACKUP_ROOT, dbname)
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, f"{dbname}_{stamp}.sql.gz")
    with open(path, "wb") as fh:
        fh.write(payload)
    _prune(dest_dir, dbname)
    return path


def _prune(dest_dir: str, dbname: str) -> None:
    try:
        files = sorted(
            f for f in os.listdir(dest_dir) if f.startswith(dbname) and f.endswith(".sql.gz")
        )
        for old in files[:-BACKUP_RETENTION] if BACKUP_RETENTION > 0 else []:
            os.remove(os.path.join(dest_dir, old))
    except OSError:
        pass


def list_backups() -> list[dict]:
    items = []
    for name in sorted(os.listdir(BACKUP_ROOT)):
        full = os.path.join(BACKUP_ROOT, name)
        if not os.path.isdir(full):
            continue
        for f in sorted(os.listdir(full)):
            if f.endswith(".sql.gz"):
                p = os.path.join(full, f)
                items.append({
                    "db": name,
                    "file": f,
                    "size": os.path.getsize(p),
                    "created": time.strftime("%Y-%m-%d %H:%M:%S",
                                             time.localtime(os.path.getmtime(p))),
                })
    return items


def run_backup_all() -> dict:
    """Backup every tenant plus the control-plane DB. Returns a summary."""
    dbs = tenant_db_names()
    if config.PLATFORM_DB_NAME not in dbs:
        dbs.append(config.PLATFORM_DB_NAME)
    results = []
    for db in dbs:
        try:
            path = backup_db(db)
            results.append({"db": db, "ok": True, "path": path})
        except Exception as exc:  # one bad tenant must not kill the sweep
            results.append({"db": db, "ok": False, "error": str(exc)})
    return {
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "backup_root": BACKUP_ROOT,
        "results": results,
        "ok_count": sum(1 for r in results if r["ok"]),
        "fail_count": sum(1 for r in results if not r["ok"]),
    }
