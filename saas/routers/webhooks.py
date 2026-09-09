"""
Webhook endpoints (Phase 6) -- tenant subscribes URLs to events; delivery +
signing + retries live in saas/webhooks.py.
"""
import secrets

from fastapi import APIRouter, Depends, HTTPException

from ..audit import log_action
from ..db_utils import fetchall_dict, fetchone_dict
from ..deps import TenantContext, require_permission
from ..webhooks import EVENT_TYPES
from ..url_safety import UnsafeWebhookURL, validate_webhook_url
from .. import config

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])


def _row(c):
    return fetchone_dict(c)


@router.get("")
def list_webhooks(ctx: TenantContext = Depends(require_permission("webhook.view"))):
    c = ctx.conn.cursor()
    c.execute("""SELECT id, name, url, events, secret, active, created_by,
                 last_delivery_at, last_delivery_status, created_at
                 FROM webhooks ORDER BY id DESC""")
    items = fetchall_dict(c)
    for it in items:
        it["has_secret"] = bool(it.pop("secret", ""))
    return {"items": items, "events": sorted(EVENT_TYPES)}


@router.post("")
def create_webhook(body: dict, ctx: TenantContext = Depends(require_permission("webhook.manage"))):
    name = (body.get("name") or "").strip()
    url = (body.get("url") or "").strip()
    if not name or not url:
        raise HTTPException(400, "name and url required")
    try:
        validate_webhook_url(url, require_https=getattr(config, "REQUIRE_HTTPS_WEBHOOKS", False))
    except UnsafeWebhookURL as exc:
        raise HTTPException(400, f"invalid webhook url: {exc}")
    events = [e for e in (body.get("events") or []) if e in EVENT_TYPES]
    if not events:
        raise HTTPException(400, f"events must be a non-empty subset of {sorted(EVENT_TYPES)}")
    secret = (body.get("secret") or "").strip() or secrets.token_urlsafe(24)
    c = ctx.conn.cursor()
    c.execute("""INSERT INTO webhooks (name, url, events, secret, active, created_by)
                 VALUES (%s,%s,%s,%s,TRUE,%s) RETURNING id""",
              (name, url, events, secret, ctx.user["id"]))
    wid = c.fetchone()[0]
    ctx.conn.commit()
    log_action(ctx.conn, ctx.user["id"], "webhook.create", "webhook", wid,
               {"name": name, "url": url, "events": events})
    return {"ok": True, "id": wid, "name": name, "url": url,
            "events": events, "secret": secret,
            "note": "Store this secret now -- it is shown only once."}


@router.patch("/{wid}")
def update_webhook(wid: int, body: dict, ctx: TenantContext = Depends(require_permission("webhook.manage"))):
    c = ctx.conn.cursor()
    c.execute("SELECT id FROM webhooks WHERE id=%s", (wid,))
    if not c.fetchone():
        raise HTTPException(404, "webhook not found")
    sets, params = [], []
    for f in ("name", "url", "secret"):
        if body.get(f):
            value = str(body[f]).strip()
            if f == "url":
                try:
                    validate_webhook_url(value, require_https=getattr(config, "REQUIRE_HTTPS_WEBHOOKS", False))
                except UnsafeWebhookURL as exc:
                    raise HTTPException(400, f"invalid webhook url: {exc}")
            sets.append(f"{f}=%s")
            params.append(value)
    if "active" in body and body["active"] is not None:
        sets.append("active=%s")
        params.append(bool(body["active"]))
    if "events" in body:
        events = [e for e in body["events"] if e in EVENT_TYPES]
        sets.append("events=%s")
        params.append(events)
    if not sets:
        raise HTTPException(400, "nothing to update")
    params.append(wid)
    c.execute(f"UPDATE webhooks SET {', '.join(sets)} WHERE id=%s", params)
    ctx.conn.commit()
    log_action(ctx.conn, ctx.user["id"], "webhook.update", "webhook", wid, {"changes": sets})
    return {"ok": True}


@router.delete("/{wid}")
def delete_webhook(wid: int, ctx: TenantContext = Depends(require_permission("webhook.manage"))):
    c = ctx.conn.cursor()
    c.execute("DELETE FROM webhooks WHERE id=%s RETURNING id", (wid,))
    if not c.fetchone():
        raise HTTPException(404, "webhook not found")
    ctx.conn.commit()
    log_action(ctx.conn, ctx.user["id"], "webhook.delete", "webhook", wid)
    return {"ok": True}


@router.get("/{wid}/deliveries")
def webhook_deliveries(wid: int, ctx: TenantContext = Depends(require_permission("webhook.view"))):
    c = ctx.conn.cursor()
    c.execute("SELECT id FROM webhooks WHERE id=%s", (wid,))
    if not c.fetchone():
        raise HTTPException(404, "webhook not found")
    c.execute("""SELECT id, event, status, attempts, http_status, error,
                 created_at, delivered_at FROM webhook_deliveries
                 WHERE webhook_id=%s ORDER BY id DESC LIMIT 200""", (wid,))
    return {"items": fetchall_dict(c)}
