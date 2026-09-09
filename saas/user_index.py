"""
Platform-level username -> division index.

Tenants sign in with just (username, password) -- no division code -- so the
control plane needs a reverse lookup from username to the tenant DB(s) that
contain that account. This module maintains the ``division_users`` table:

  - kept in sync whenever a user is created (single, bulk, or the provisioned
    bootstrap admin), and
  - backfilled for pre-existing tenants by scripts/backfill_user_index.py.

Usernames are intentionally NOT globally unique (legacy divisions share
usernames like ``division_admin``), so lookups return every match and the auth
layer decides: exactly one division -> sign in; several -> require division code.
"""
from . import platform_db
from .db_utils import fetchall_dict


def sync_user(tenant_db: str, username: str, user_id: int | None,
              division_id: int | None = None, email: str | None = None,
              mobile: str | None = None) -> None:
    """Register/refresh the mapping for one tenant user. Idempotent.

    ``division_id`` may be passed explicitly (e.g. from provisioning, which
    runs before the divisions row carries tenant_db_name); otherwise it is
    derived from ``tenant_db``.

    ``email``/``mobile`` mirror the tenant users row so the account-recovery
    flow can find accounts by contact without scanning tenant databases.

    Safe to call after any user insert: the row is keyed on
    (username, division_id), so a re-insert (e.g. a re-run of provisioning)
    just refreshes the stored tenant user id."""
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        if division_id is None:
            c.execute("SELECT id FROM divisions WHERE tenant_db_name=%s", (tenant_db,))
            row = c.fetchone()
            if not row:
                return  # division not provisioned yet (or already gone) -- nothing to index
            division_id = row[0]
        c.execute(
            """INSERT INTO division_users (username, division_id, tenant_db_name, user_id, email, mobile)
               VALUES (%s,%s,%s,%s,%s,%s)
               ON CONFLICT (username, division_id)
               DO UPDATE SET tenant_db_name=EXCLUDED.tenant_db_name,
                             user_id=EXCLUDED.user_id,
                             email=EXCLUDED.email,
                             mobile=EXCLUDED.mobile""",
            (username, division_id, tenant_db, user_id, email, mobile),
        )
        conn.commit()
    finally:
        conn.close()


def lookup_division_by_username(username: str) -> dict | None:
    """Resolve a username to a division when it maps to EXACTLY one division.

    Returns the division row, or None when the username is unknown or belongs
    to several divisions (ambiguous)."""
    divisions = lookup_divisions_by_username(username)
    if len(divisions) == 1:
        return divisions[0]
    return None


def lookup_divisions_by_username(username: str) -> list[dict]:
    """All active/provisioned divisions whose tenant DB has this username.

    Returns divisions regardless of the user's own status in the tenant (the
    login layer validates password + active status against the tenant DB)."""
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute(
            """SELECT d.* FROM division_users du
               JOIN divisions d ON d.id = du.division_id
               WHERE du.username=%s AND d.tenant_db_name IS NOT NULL
                 AND d.status IN ('active', 'provisioning')
               ORDER BY d.id""",
            (username,),
        )
        return fetchall_dict(c)
    finally:
        conn.close()


def lookup_divisions_by_contact(kind: str, value: str) -> list[dict]:
    """All accounts whose tenant user row matches an email or mobile.

    ``kind`` is ``"email"`` or ``"mobile"``. Returns one entry per matching
    (division, user) pair with the division resolved, so the recovery flow can
    show every division code / username the contact belongs to."""
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        if kind == "email":
            where = "LOWER(du.email)=LOWER(%s)"
        else:
            where = "du.mobile=%s"
        c.execute(
            f"""SELECT d.*, du.user_id AS index_user_id, du.username AS index_username,
                       du.email AS index_email, du.mobile AS index_mobile
                FROM division_users du
                JOIN divisions d ON d.id = du.division_id
                WHERE {where} AND d.tenant_db_name IS NOT NULL
                  AND d.status IN ('active', 'provisioning')
                ORDER BY d.id, du.username""",
            (value,),
        )
        return fetchall_dict(c)
    finally:
        conn.close()
