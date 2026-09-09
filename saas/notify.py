"""
Notification engine (Phase 5).

Every notification is recorded in the tenant `notifications` table (in-app
feed). Channel dispatch is driven by the tenant's company_settings
(`notifications` section) plus global provider credentials:

  settings.notifications = {
    "channels": ["email", "sms", "whatsapp", "webhook"],
    "webhooks": ["https://.../hook"],
    "webhook_secret": "...",
  }

Dispatch rules:
  - email   -> sent only when SMTP_* credentials exist.
  - sms     -> sent only when SMS_API_KEY exists.
  - whatsapp-> sent only when WHATSAPP_API_KEY exists.
  - webhook -> POSTs the notification JSON to each configured URL (HMAC
               signed when a secret is set).
Without credentials the channel is logged to the server console (dry-run).
"""
import hashlib
import hmac
import json
import logging
import smtplib
from email.message import EmailMessage

from . import config

log = logging.getLogger("saas.notify")


# ── Tenant channel configuration ────────────────────────────────────────────

def tenant_notify_settings(conn) -> dict:
    """Read the tenant's notifications section (merged with defaults)."""
    from .db_utils import fetchone_dict
    c = conn.cursor()
    c.execute("SELECT value FROM company_settings WHERE key='notifications'")
    row = fetchone_dict(c)
    value = (row or {}).get("value") or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            value = {}
    channels = value.get("channels")
    if not channels:
        channels = ["inapp"]
        if value.get("provider") in ("smtp", "email") or value.get("smtp_host"):
            channels.append("email")
        if value.get("whatsapp_api_key"):
            channels.append("whatsapp")
        if value.get("sms_api_key"):
            channels.append("sms")
        if value.get("webhook_url"):
            channels.append("webhook")
        if value.get("push_enabled"):
            channels.append("push")
    webhooks = value.get("webhooks")
    if not webhooks and value.get("webhook_url"):
        webhooks = [value["webhook_url"]]
    return {
        "channels": channels or ["inapp"],
        "webhooks": webhooks or [],
        "webhook_secret": value.get("webhook_secret") or "",
    }


def channel_enabled(conn, channel: str) -> bool:
    return channel in tenant_notify_settings(conn)["channels"]


# ── Channel senders (log-only until real credentials exist) ─────────────────

def send_email(to: str, subject: str, body: str) -> None:
    if not (config.SMTP_HOST and config.SMTP_USER):
        log.info("[notify:email] to=%s subject=%s body=%s", to, subject, body)
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.SMTP_FROM
    msg["To"] = to
    msg.set_content(body)
    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as s:
        s.starttls()
        s.login(config.SMTP_USER, config.SMTP_PASSWORD)
        s.send_message(msg)


def send_whatsapp(to: str, message: str) -> None:
    if not config.WHATSAPP_API_KEY:
        log.info("[notify:whatsapp] to=%s message=%s", to, message)
        return
    # Meta Cloud API / Gupshup / Interakt etc. Wire provider-specific payload here.
    url = config.WHATSAPP_API_URL or "https://graph.facebook.com/v19.0/undefined/messages"
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text",
               "text": {"body": message}}
    _post_json(url, payload, {"Authorization": f"Bearer {config.WHATSAPP_API_KEY}"})


def send_sms(to: str, message: str) -> None:
    if not config.SMS_API_KEY:
        log.info("[notify:sms] to=%s message=%s", to, message)
        return
    url = config.SMS_API_URL or "https://api.smsprovider.in/v1/send"
    payload = {"to": to, "message": message, "sender": config.SMS_SENDER}
    _post_json(url, payload, {"X-API-Key": config.SMS_API_KEY})


def send_webhook(conn, payload: dict) -> None:
    settings = tenant_notify_settings(conn)
    for url in settings["webhooks"]:
        headers = {"Content-Type": "application/json"}
        secret = settings.get("webhook_secret")
        if secret:
            body = json.dumps(payload, separators=(",", ":"))
            sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
            headers["X-Webhook-Signature"] = sig
        _post_json(url, payload, headers)


def _post_json(url: str, payload: dict, headers: dict) -> None:
    import urllib.error
    import urllib.request
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            log.info("[notify:webhook] %s -> %s", url, resp.status)
    except urllib.error.HTTPError as exc:
        log.warning("[notify:webhook] %s -> HTTP %s", url, exc.code)
    except Exception as exc:  # network errors must never break the request
        log.warning("[notify:webhook] %s -> %s", url, exc)


# ── In-app + channel dispatch ───────────────────────────────────────────────

def notify_user(conn, user_id: int, type_: str, title: str, message: str,
                reference_type: str | None = None, reference_id: int | None = None,
                channel: str = "inapp") -> int:
    """Insert an in-app notification and dispatch on other channels if the
    tenant has them enabled. Returns the notification id."""
    c = conn.cursor()
    c.execute(
        "INSERT INTO notifications (user_id, channel, type, title, message, reference_type, reference_id) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (user_id, channel, type_, title, message, reference_type, reference_id),
    )
    nid = c.fetchone()[0]
    conn.commit()

    c.execute("SELECT email, mobile FROM users WHERE id=%s", (user_id,))
    row = c.fetchone()
    email, mobile = (row[0], row[1]) if row else (None, None)

    if channel_enabled(conn, "email") and email:
        send_email(email, title, message)
    if channel_enabled(conn, "whatsapp") and mobile:
        send_whatsapp(mobile, message)
    if channel_enabled(conn, "sms") and mobile:
        send_sms(mobile, message)
    if channel_enabled(conn, "webhook"):
        send_webhook(conn, {
            "event": type_, "user_id": user_id, "title": title, "message": message,
            "reference_type": reference_type, "reference_id": reference_id,
        })
    return nid


def render_template(body: str, variables: dict) -> str:
    out = body
    for k, v in (variables or {}).items():
        out = out.replace("{" + str(k) + "}", str(v))
    return out


def notify_from_template(conn, user_id: int, template_code: str, variables: dict,
                         reference_type: str | None = None, reference_id: int | None = None) -> int | None:
    """Render a stored template and notify the user. Returns notification id or None."""
    c = conn.cursor()
    c.execute("SELECT * FROM notification_templates WHERE code=%s AND active=TRUE", (template_code,))
    from .db_utils import fetchone_dict
    tpl = fetchone_dict(c)
    if not tpl:
        return None
    body = render_template(tpl["body"], variables)
    subject = render_template(tpl["subject"] or template_code, variables) if tpl["subject"] else template_code
    return notify_user(conn, user_id, template_code, subject, body, reference_type, reference_id)


def notify_admins(conn, type_: str, title: str, message: str,
                  reference_type: str | None = None, reference_id: int | None = None) -> int:
    """Push an in-app notification to every active admin of the company.
    Admin roles: division_admin and campaignos_admin (the legacy company_admin
    role was renamed to division_admin by the schema patch)."""
    c = conn.cursor()
    c.execute(
        """SELECT u.id FROM users u JOIN roles r ON r.id = u.role_id
           WHERE u.status='active' AND r.name IN ('division_admin','campaignos_admin')""")
    for (uid,) in c.fetchall():
        notify_user(conn, uid, type_, title, message, reference_type, reference_id)
    return c.rowcount

