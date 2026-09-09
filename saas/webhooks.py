"""
Outbound webhook engine (Phase 6).

Tenant admins register endpoint URLs subscribed to specific event types
(pob.submitted, pob.approved, ...). Every dispatch is:

  1. recorded in webhook_deliveries with the full payload,
  2. handed to the background job queue as a 'webhook.deliver' job (never
     POSTed synchronously in the request path -- see dispatch_event below),
  3. delivered by saas/scheduler.py, which POSTs it (HMAC-SHA256 signed with
     the endpoint secret when one is configured) and, on failure, requeues
     the same job with exponential backoff (max attempts -> give up + mark
     failed).
"""
import hashlib
import hmac
import json
import logging

from . import config
from .url_safety import UnsafeWebhookURL, safe_post

log = logging.getLogger("saas.webhooks")

MAX_ATTEMPTS = 5

# events that trigger webhook delivery
EVENT_TYPES = ("pob.submitted", "pob.approved", "pob.rejected", "pob.duplicate",
               "verification.approved", "verification.rejected",
               "gratification.created", "cashback.paid", "gift.ready",
               "gift.delivered", "user.created", "payout.batch.paid")


def sign(secret: str, body: bytes) -> str:
    return hmac.new((secret or "").encode(), body, hashlib.sha256).hexdigest()


def matching_webhooks(conn, event: str) -> list[dict]:
    c = conn.cursor()
    c.execute("SELECT * FROM webhooks WHERE active=TRUE AND %s = ANY(events) ORDER BY id", (event,))
    from .db_utils import fetchall_dict
    return fetchall_dict(c)


def record_delivery(conn, webhook_id: int, event: str, payload: dict) -> int:
    c = conn.cursor()
    c.execute(
        "INSERT INTO webhook_deliveries (webhook_id, event, payload) VALUES (%s,%s,%s) RETURNING id",
        (webhook_id, event, json.dumps(payload)),
    )
    did = c.fetchone()[0]
    conn.commit()
    return did


def update_delivery(conn, delivery_id: int, *, status: str, http_status: int = None,
                    response: str = None, error: str = None, attempts: int = None):
    c = conn.cursor()
    sets = ["status=%s"]
    params: list = [status]
    if http_status is not None:
        sets.append("http_status=%s")
        params.append(http_status)
    if response is not None:
        sets.append("response=%s")
        params.append(response[:2000])
    if error is not None:
        sets.append("error=%s")
        params.append(error[:2000])
    if attempts is not None:
        sets.append("attempts=%s")
        params.append(attempts)
    if status == "delivered":
        sets.append("delivered_at=CURRENT_TIMESTAMP")
    c.execute(f"UPDATE webhook_deliveries SET {', '.join(sets)} WHERE id=%s", params + [delivery_id])
    conn.commit()


def update_webhook_meta(conn, webhook_id: int, status: str):
    c = conn.cursor()
    c.execute(
        "UPDATE webhooks SET last_delivery_at=CURRENT_TIMESTAMP, last_delivery_status=%s WHERE id=%s",
        (status, webhook_id),
    )
    conn.commit()


def _post(webhook: dict, payload: dict, delivery_id: int) -> tuple[int, str, str]:
    """Return (http_status, response_text, error)."""
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = {
        "Content-Type": "application/json",
        "X-POB-Event": payload.get("event", ""),
        "X-POB-Delivery": str(delivery_id),
        "X-POB-Tenant": webhook.get("tenant_db") or "",
        "User-Agent": "POB-SaaS-Webhook/1.0",
    }
    if webhook.get("secret"):
        headers["X-POB-Signature"] = f"sha256={sign(webhook['secret'], body)}"
    try:
        # safe_post re-validates the host (and each redirect hop) against
        # loopback/private/link-local/metadata ranges immediately before
        # connecting -- see saas/url_safety.py for why this can't just be a
        # one-time check at webhook-registration time.
        resp = safe_post(webhook["url"], content=body, headers=headers, timeout=10.0)
        return resp.status_code, resp.text[:2000], ""
    except UnsafeWebhookURL as exc:
        log.warning("[webhook] blocked unsafe delivery target: %s", exc)
        return 0, "", f"blocked unsafe webhook url: {exc}"
    except Exception as exc:  # network / timeout / DNS
        return 0, "", str(exc)


def deliver_webhook(conn, webhook_id: int, delivery_id: int) -> dict:
    """One delivery attempt. Returns the delivery row state."""
    c = conn.cursor()
    c.execute("SELECT * FROM webhooks WHERE id=%s", (webhook_id,))
    row = c.fetchone()
    if not row:
        return {"error": "webhook not found"}
    cols = [d[0] for d in c.description]
    webhook = dict(zip(cols, row))
    c.execute("SELECT payload FROM webhook_deliveries WHERE id=%s", (delivery_id,))
    prow = c.fetchone()
    payload = prow[0] if prow else {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}

    http_status, response, error = _post(webhook, payload, delivery_id)
    if http_status and 200 <= http_status < 300:
        update_delivery(conn, delivery_id, status="delivered", http_status=http_status,
                        response=response or "", attempts=webhook.get("attempts", 0) + 1)
        update_webhook_meta(conn, webhook_id, "delivered")
        return {"status": "delivered", "http_status": http_status}
    status = "failed"
    update_delivery(conn, delivery_id, status=status, http_status=http_status or None,
                    response=response or None, error=error or None,
                    attempts=webhook.get("attempts", 0) + 1)
    update_webhook_meta(conn, webhook_id, status)
    return {"status": status, "http_status": http_status, "error": error}


def enqueue_delivery(conn, webhook_id: int, delivery_id: int, run_at, job_type: str = "webhook.deliver"):
    c = conn.cursor()
    c.execute(
        f"""INSERT INTO job_queue (job_type, payload, status, run_at)
           VALUES (%s, %s, 'queued', %s)""",
        (job_type, json.dumps({"webhook_id": webhook_id, "delivery_id": delivery_id}), run_at),
    )
    conn.commit()


# Back-compat alias -- retries were previously the only thing enqueued here.
def enqueue_retry(conn, webhook_id: int, delivery_id: int, run_at):
    enqueue_delivery(conn, webhook_id, delivery_id, run_at, job_type="webhook.retry")


def dispatch_event(conn, event: str, payload: dict, tenant_db: str):
    """Record a delivery row for every subscribed webhook and hand the
    actual HTTP delivery off to the background job queue (saas/scheduler.py,
    job_type='webhook.deliver'/'webhook.retry').

    This used to POST synchronously, in the request path, before falling
    back to the retry queue only on failure -- meaning a slow or hanging
    webhook endpoint could add up to the full httpx timeout (10s) to every
    request that triggers an event, even though the caller has nothing to
    do with the receiving endpoint's availability. Delivery is now always
    asynchronous: this function only ever writes rows (fast, in the same
    transaction as the business action) and never makes an outbound HTTP
    call. Never raises -- a broken webhook must not break the API."""
    if event not in EVENT_TYPES:
        return
    try:
        hooks = matching_webhooks(conn, event)
        if not hooks:
            return
        payload = dict(payload)
        payload["event"] = event
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        for wh in hooks:
            did = record_delivery(conn, wh["id"], event, payload)
            enqueue_delivery(conn, wh["id"], did, now, job_type="webhook.deliver")
    except Exception as exc:  # never break the caller
        log.error("[webhook] dispatch_event(%s) failed: %s", event, exc)
