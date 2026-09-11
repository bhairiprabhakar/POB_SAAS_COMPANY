"""
Platform-side notification fan-out for super admins.

Actions taken by tenant users (campaign submitted, POB awaiting verification,
gratification eligible) must reach the relevant platform admin roles. This
module inserts `platform_notifications` rows for every matching active super
admin and, when SMTP credentials exist, emails them too.

Role fan-out: the specialised role listed for the event plus owner/full, who
see everything.
"""
import logging

from . import config
from . import platform_db
from .db_utils import fetchone_dict
from .notify import send_email

log = logging.getLogger("saas.platform_notify")

_EVENT_ROLES = {
    "campaign.pending": ("campaign_admin",),
    "pob.pending": ("verification_admin",),
    "gratification.eligible": ("finance_admin",),
}

_ALWAYS_ROLES = ("owner", "full")


def is_enabled() -> bool:
    """Platform notifications are on unless explicitly disabled."""
    return config.PLATFORM_NOTIFY_ENABLED


def tenant_division(tenant_db: str) -> dict:
    """Resolve a tenant database name to its platform divisions row."""
    if not tenant_db:
        return {}
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT id, name, code FROM divisions WHERE tenant_db_name=%s", (tenant_db,))
        return fetchone_dict(c) or {}
    finally:
        conn.close()


def notify_roles(event: str, roles, title: str, message: str,
                 link: str = "", tenant_db: str | None = None,
                 division_id: int | None = None,
                 division_name: str | None = None, kind: str | None = None) -> int:
    """Insert a platform notification (+ email) for every active super admin
    whose role is in `roles`. The given specialised roles are merged with the
    always-notified owner/full roles. Returns the number of admins notified."""
    if not is_enabled():
        return 0
    if division_id is None and tenant_db:
        row = tenant_division(tenant_db)
        division_id = row.get("id")
        division_name = row.get("name") or division_name
    wanted = tuple(set(r for r in roles if r) | set(_ALWAYS_ROLES))
    conn = platform_db.get_db()
    try:
        c = conn.cursor()
        c.execute(
            """SELECT id, email FROM super_admins
               WHERE status='active' AND role = ANY(%s)""", (list(wanted),))
        admins = c.fetchall()
        n = 0
        for sid, email in admins:
            c.execute(
                """INSERT INTO platform_notifications
                   (super_admin_id, kind, title, message, link, division_id, division_name)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (sid, kind or event.split(".")[0], title, message, link,
                 division_id, division_name))
            n += 1
            if email:
                try:
                    send_email(email, title, message)
                except Exception:
                    log.exception("email to super admin %s failed", sid)
        conn.commit()
        return n
    finally:
        conn.close()


def notify_event(event: str, title: str, message: str, link: str = "",
                 tenant_db: str | None = None,
                 division_id: int | None = None,
                 division_name: str | None = None) -> int:
    """Notify the platform role responsible for an event type."""
    roles = _EVENT_ROLES.get(event, ())
    if not roles:
        log.warning("no platform route for event %s", event)
        return 0
    return notify_roles(event, roles, title, message, link,
                        tenant_db=tenant_db, division_id=division_id,
                        division_name=division_name)