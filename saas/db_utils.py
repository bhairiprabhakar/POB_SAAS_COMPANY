"""
Connection + row helpers shared across the SaaS platform.

A small wrapper over psycopg2.pool similar to app/database.py but generic
enough to serve BOTH the control-plane DB and per-tenant DB pools.
"""
import datetime as _dt
import threading

import psycopg2
import psycopg2.pool


def _serialize(v):
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        return v.isoformat()
    if isinstance(v, (dict, list)):
        import json
        return json.dumps(v)
    return v


def fetchone_dict(cursor):
    row = cursor.fetchone()
    if row is None or cursor.description is None:
        return None
    cols = [d[0] for d in cursor.description]
    return {k: _serialize(v) for k, v in zip(cols, row)}


def fetchall_dict(cursor):
    rows = cursor.fetchall()
    if not rows or cursor.description is None:
        return []
    cols = [d[0] for d in cursor.description]
    return [{k: _serialize(v) for k, v in zip(cols, row)} for row in rows]


class _PooledConnWrapper:
    """Returns the connection to the pool on close() instead of closing it."""

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


class Pool:
    """Lazily-created psycopg2 ThreadedConnectionPool with thread-safe access."""

    def __init__(self, dbname, host, port, user, password, minconn, maxconn):
        self._lock = threading.Lock()
        self._pool = None
        self._args = dict(dbname=dbname, host=host, port=port,
                          user=user, password=password)
        self._minconn = minconn
        self._maxconn = maxconn
        self._closed = False

    def _ensure(self):
        if self._pool is None:
            with self._lock:
                if self._pool is None:
                    self._pool = psycopg2.pool.ThreadedConnectionPool(
                        minconn=self._minconn, maxconn=self._maxconn, **self._args)
        return self._pool

    def get_conn(self):
        pool = self._ensure()
        real = pool.getconn()
        real.autocommit = False
        return _PooledConnWrapper(real, pool)

    def ping(self) -> bool:
        try:
            conn = self.get_conn()
            c = conn.cursor()
            c.execute("SELECT 1")
            c.fetchone()
            conn.close()
            return True
        except Exception:
            return False

    def close_all(self):
        with self._lock:
            if self._pool is not None:
                self._pool.closeall()
                self._pool = None


def make_pool(dbname, minconn=2, maxconn=20, host=None, port=None, user=None, password=None):
    from . import config as cfg
    return Pool(
        dbname=dbname,
        host=host or cfg.DB_HOST,
        port=port or cfg.DB_PORT,
        user=user or cfg.DB_USER,
        password=password or cfg.DB_PASSWORD,
        minconn=minconn,
        maxconn=maxconn,
    )


def get_conn(dbname, autocommit=False, host=None, port=None, user=None, password=None):
    """One-off direct connection (used for CREATE DATABASE, which cannot run
    inside a transaction/pooled connection)."""
    from . import config as cfg
    conn = psycopg2.connect(
        dbname=dbname,
        host=host or cfg.DB_HOST,
        port=port or cfg.DB_PORT,
        user=user or cfg.DB_USER,
        password=password or cfg.DB_PASSWORD,
    )
    conn.autocommit = autocommit
    return conn


def admin_conn():
    """Connection to the `postgres` maintenance database -- used for
    CREATE/DROP DATABASE of tenants."""
    return get_conn("postgres", autocommit=True)
