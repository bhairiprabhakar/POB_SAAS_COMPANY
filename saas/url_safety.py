"""
SSRF guard for outbound webhook URLs.

Webhook URLs are supplied by tenant admins and later fetched server-side by
`saas.webhooks._post`. Without validation an admin (or a compromised admin
account) could register a URL pointing at loopback, RFC1918/link-local
ranges, or the cloud metadata endpoint (169.254.169.254) and use the
platform as an SSRF proxy into its own network / AWS credentials.

Used in two places:
  1. At webhook create/update time (`saas/routers/webhooks.py`) -- reject
     obviously-unsafe URLs up front so bad config never gets saved.
  2. At delivery time (`saas/webhooks.py`) -- re-resolve and re-check
     immediately before every request (including each redirect hop), so a
     hostname that resolved to a public IP at registration time but has
     since been repointed at an internal address (DNS rebinding) is still
     caught.

Note on DNS rebinding: this re-checks on every delivery attempt and every
redirect hop, which closes the registration-time-vs-delivery-time gap. It
does not pin the TCP connection to the exact IP it validated (that needs a
custom transport), so a small TOCTOU window remains between the check and
httpx's own connect. If stronger guarantees are needed later, add a custom
httpx transport that connects to the validated IP directly.
"""
import ipaddress
import socket
from urllib.parse import urlsplit

import httpx


class UnsafeWebhookURL(Exception):
    pass


def _is_blocked_ip(ip) -> bool:
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def resolve_safe(hostname: str) -> list[str]:
    """Resolve hostname and return only IPs safe to connect to. Raises
    UnsafeWebhookURL if resolution fails or every address is blocked
    (loopback / private / link-local / metadata / reserved / etc.)."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise UnsafeWebhookURL(f"could not resolve host: {exc}")
    safe_ips = []
    for info in infos:
        raw_ip = info[4][0]
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError:
            continue
        if not _is_blocked_ip(ip):
            safe_ips.append(str(ip))
    if not safe_ips:
        raise UnsafeWebhookURL("host resolves only to blocked/internal addresses")
    return safe_ips


def validate_webhook_url(url: str, *, require_https: bool = False) -> None:
    """Raise UnsafeWebhookURL if the URL is not an acceptable webhook target.
    Called at registration time; delivery time does an additional re-check
    on every attempt (see safe_post) to guard against DNS rebinding."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeWebhookURL("url must be http(s)")
    if require_https and parts.scheme != "https":
        raise UnsafeWebhookURL("url must be https in production")
    if not parts.hostname:
        raise UnsafeWebhookURL("url missing host")
    if parts.hostname.lower() == "localhost":
        raise UnsafeWebhookURL("url must not target localhost")
    resolve_safe(parts.hostname)


def safe_post(url: str, *, content: bytes, headers: dict, timeout: float = 10.0):
    """POST to url, re-validating the host on every hop (initial request and
    each redirect) so a URL that has been re-pointed at an internal address
    since registration is still rejected. Redirects are not auto-followed by
    httpx; we follow them manually so each target gets re-checked."""
    current_url = url
    for _ in range(5):  # bounded manual redirect handling
        parts = urlsplit(current_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise UnsafeWebhookURL("invalid redirect target")
        resolve_safe(parts.hostname)  # raises UnsafeWebhookURL if blocked
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            resp = client.post(current_url, content=content, headers=headers)
        if resp.status_code in (301, 302, 303, 307, 308) and "location" in resp.headers:
            current_url = resp.headers["location"]
            continue
        return resp
    raise UnsafeWebhookURL("too many redirects")
