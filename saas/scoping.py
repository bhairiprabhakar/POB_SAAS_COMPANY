"""
Hierarchical data scoping.

Decides which users' rows a logged-in user may see based on their role and
position in the reporting tree (mirrors the SOW visibility rules):

  PSR / MR                 -> own data only
  ASM                      -> own team (everyone reporting to them)
  RSM                      -> team + users in the same region
  SM                       -> team + users in the same state / region
  ZSM / NSM                -> team rollup (zonal / national)
  HO                       -> all India
  Division Admin           -> every user in their division (company-wide when
                             no division is assigned to the account)
  CampaignOS Admin / Verifier / Auditor / Finance -> all data

`visible_user_ids(conn, ctx)` returns None for unrestricted access (all rows)
or a list of user ids. Use it as a `WHERE user_id = ANY(%s)` filter.
"""
from .db_utils import fetchone_dict

# Roles whose work is company-wide and never filtered by hierarchy.
GLOBAL_ROLES = {"campaignos_admin", "verifier", "auditor", "finance"}

# hierarchy ranks (see tenant_schema.DEFAULT_HIERARCHY)
_HO_RANK = 7


def _descendants(conn, root_user_id: int, division_id: int | None = None) -> list[int]:
    """All user ids whose reporting chain leads to root_user_id.

    Cycle-safe: a parent_id cycle (which the update-user guard now rejects at
    write time) must not hang every hierarchical query, so each node is
    visited at most once.

    When division_id is set, the tree is read from that division's users only,
    so a manager can never roll up subordinates from another division.
    """
    c = conn.cursor()
    if division_id is not None:
        c.execute("SELECT id, parent_id FROM users WHERE division_id=%s", (division_id,))
    else:
        c.execute("SELECT id, parent_id FROM users")
    rows = c.fetchall()
    children = {}
    for uid, parent in rows:
        children.setdefault(parent, []).append(uid)
    out = []
    visited = {root_user_id}
    stack = [root_user_id]
    while stack:
        node = stack.pop()
        for child in children.get(node, []):
            if child in visited:
                continue
            visited.add(child)
            out.append(child)
            stack.append(child)
    return out


def division_scope(conn, ctx) -> int | None:
    """The caller's division_id when they hold the division_admin role, else None.

    An unassigned division admin (division_id NULL) keeps the legacy company-wide
    view so existing deployments continue to work until the super admin binds them.
    """
    role = (ctx.user.get("role_name") or "").lower()
    if role != "division_admin":
        return None
    uid = ctx.user.get("id")
    if not uid:
        return None
    c = conn.cursor()
    c.execute("SELECT division_id FROM users WHERE id=%s", (uid,))
    row = c.fetchone()
    return row[0] if row else None


def user_division_id(conn, ctx) -> int | None:
    """The caller's division_id regardless of role.

    Returns the user's division_id if they have one assigned, else None.
    Used to scope campaign/product queries so each user only sees their
    division's data.  Division admins and regular users with a division_id
    are both scoped.
    """
    uid = ctx.user.get("id")
    if not uid:
        return None
    c = conn.cursor()
    c.execute("SELECT division_id FROM users WHERE id=%s", (uid,))
    row = c.fetchone()
    return row[0] if row and row[0] else None


def visible_user_ids(conn, ctx) -> list[int] | None:
    """None => unrestricted. Otherwise the list of visible user ids."""
    role = (ctx.user.get("role_name") or "").lower()
    if role in GLOBAL_ROLES:
        return None
    uid = ctx.user.get("id")
    if not uid:
        return None  # API-key / machine context: scoped by the key's own perms
    if role == "division_admin":
        div = division_scope(conn, ctx)
        if not div:
            return None  # unassigned division admin -> company-wide (legacy)
        c = conn.cursor()
        c.execute("SELECT id FROM users WHERE division_id=%s", (div,))
        return [r[0] for r in c.fetchall()]
    if role == "verification_agent":
        # Each division runs its own agent: they see every POB submitted by
        # their own division's users and nothing else. An unassigned agent
        # falls back to self-only (conservative) until the admin binds one.
        div = user_division_id(conn, ctx)
        if not div:
            return [uid]
        c = conn.cursor()
        c.execute("SELECT id FROM users WHERE division_id=%s", (div,))
        return [r[0] for r in c.fetchall()]
    c = conn.cursor()
    c.execute(
        """SELECT h.rank, u.region, u.state FROM users u
           LEFT JOIN hierarchy_levels h ON h.id=u.hierarchy_level_id
           WHERE u.id=%s""",
        (uid,),
    )
    row = fetchone_dict(c)
    rank = (row or {}).get("rank")
    region = (row or {}).get("region")
    state = (row or {}).get("state")
    if not rank:
        return [uid]  # no position assigned -> conservative: self only
    if rank >= _HO_RANK:
        return None  # HO sees all India
    div = user_division_id(conn, ctx)
    team = [uid] + _descendants(conn, uid, div)
    if rank >= 3 and region:
        if div is not None:
            c.execute("SELECT id FROM users WHERE division_id=%s AND region=%s "
                      "AND NOT (id = ANY(%s))", (div, region, team))
        else:
            c.execute("SELECT id FROM users WHERE region=%s AND NOT (id = ANY(%s))",
                      (region, team))
        team.extend(r[0] for r in c.fetchall())
    if rank >= 4 and state:
        if div is not None:
            c.execute("SELECT id FROM users WHERE division_id=%s AND state=%s "
                      "AND NOT (id = ANY(%s))", (div, state, team))
        else:
            c.execute("SELECT id FROM users WHERE state=%s AND NOT (id = ANY(%s))",
                      (state, team))
        team.extend(r[0] for r in c.fetchall())
    return sorted(set(team))


def scope_filter(ctx, conn, column: str = "pa.user_id"):
    """Return (sql_fragment, params) that scopes a query to the caller's view."""
    visible = visible_user_ids(conn, ctx)
    if visible is None:
        return "", ()
    return f"{column} = ANY(%s)", (visible,)
