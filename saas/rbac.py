"""
Dynamic permission engine.

Permissions are stored as rows in the tenant `permissions` table; roles map
to a set of permission codes in `role_permissions`. Super admins can create
new roles and re-assign permissions at runtime -- nothing is hardcoded beyond
the initial seed catalog.
"""
from .db_utils import fetchall_dict


def load_role_permissions(conn, role_id: int | None) -> set[str]:
    if role_id is None:
        return set()
    c = conn.cursor()
    c.execute(
        "SELECT p.code FROM permissions p JOIN role_permissions rp ON rp.permission_code=p.code "
        "WHERE rp.role_id=%s",
        (role_id,),
    )
    return {row[0] for row in c.fetchall()}


def load_user_roles(conn, user_id: int) -> list[dict]:
    c = conn.cursor()
    c.execute(
        """SELECT r.* FROM roles r
           JOIN users u ON u.role_id = r.id
           WHERE u.id=%s""",
        (user_id,),
    )
    return fetchall_dict(c)


def has_permission(perms: set[str], code: str) -> bool:
    return code in perms


def list_all_permissions(conn) -> list[dict]:
    c = conn.cursor()
    c.execute("SELECT code, label, module FROM permissions ORDER BY module, code")
    return fetchall_dict(c)
