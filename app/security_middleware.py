"""
Security middleware.

Covers the items from the original migration spec's SECURITY section that
were never actually implemented: secure headers, CSRF protection, and rate
limiting. SQL-injection prevention and XSS protection are addressed
elsewhere (see the notes at the bottom of this file) rather than here.
"""
import time
from collections import defaultdict, deque
from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from .config import MAX_UPLOAD_SIZE, ENFORCE_HTTPS


# ═══════════════════════════════════════════════════════════════════════════
# 0. REQUEST BODY SIZE LIMIT
# ═══════════════════════════════════════════════════════════════════════════
class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """
    Starlette/FastAPI does NOT enforce any request body size limit by
    default -- without this, a client can send an arbitrarily large POST
    body (declared via Content-Length, or streamed via chunked encoding
    with no Content-Length at all) and the server will fully buffer/parse
    it before any application code gets a chance to reject it. That's a
    straightforward memory/disk exhaustion DoS vector, and the /upload and
    /upload-from-url routes make it an especially easy target to hit by
    accident even without malicious intent.

    This checks Content-Length up front (fast path, rejects most oversized
    requests before any body is read) and also enforces the limit while
    streaming the body for requests that lie about or omit Content-Length.

    IMPORTANT: this is app-level defense-in-depth, not a replacement for a
    hard limit at your reverse proxy / load balancer (e.g. nginx's
    `client_max_body_size`), which can reject oversized requests before
    they even reach this process at all -- set both in production.
    """
    LIMIT = MAX_UPLOAD_SIZE + (5 * 1024 * 1024)  # small buffer for multipart overhead

    async def dispatch(self, request, call_next):
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > self.LIMIT:
                    return JSONResponse({"error": "Request body too large."}, status_code=413)
            except ValueError:
                pass

        # Defense for chunked/streamed bodies with no (or a lying) Content-Length:
        # wrap request.stream() so it raises once the running total exceeds LIMIT.
        # FastAPI/Starlette read the body lazily via request.stream(), so this
        # catches it regardless of whether Content-Length was present.
        total = 0
        limit = self.LIMIT

        original_stream = request.stream

        async def limited_stream():
            nonlocal total
            async for chunk in original_stream():
                total += len(chunk)
                if total > limit:
                    raise _BodyTooLarge()
                yield chunk

        request.stream = limited_stream

        try:
            return await call_next(request)
        except _BodyTooLarge:
            return JSONResponse({"error": "Request body too large."}, status_code=413)


class _BodyTooLarge(Exception):
    pass


# ═══════════════════════════════════════════════════════════════════════════
# 1. SECURE HEADERS
# ═══════════════════════════════════════════════════════════════════════════
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Adds standard defensive headers to every response.

    CSP note: this app's templates use inline `onclick=`/`style=` attributes
    extensively (thousands of them across 3 templates), so this CSP allows
    'unsafe-inline' for script-src/style-src rather than breaking the UI --
    retrofitting a nonce- or hash-based CSP would mean touching every inline
    handler in agent.html/index.html/superadmin.html, which is a much bigger,
    separate piece of work. Even with 'unsafe-inline', this CSP still blocks
    the most common exploitation step after an XSS bug is found: loading a
    script from an attacker-controlled domain, framing the site for
    clickjacking, or exfiltrating form submissions to another origin.
    """
    CSP = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: blob:; "
        "connect-src 'self'; "
        "frame-ancestors 'self'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "object-src 'none'"
    )

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        response.headers["Content-Security-Policy"] = self.CSP
        if ENFORCE_HTTPS:
            # Only sent once you've confirmed HTTPS is actually in place end
            # to end (see ENFORCE_HTTPS in app/config.py) -- this tells
            # browsers to refuse plain HTTP for this host for the next ~2
            # years, which is exactly backwards to enable before HTTPS works.
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        return response


# ═══════════════════════════════════════════════════════════════════════════
# 2. CSRF PROTECTION -- Origin/Referer validation
# ═══════════════════════════════════════════════════════════════════════════
class CSRFOriginCheckMiddleware(BaseHTTPMiddleware):
    """
    Blocks cross-site state-changing requests by checking that the
    Origin (or, failing that, Referer) header matches this app's own host,
    for every POST/PUT/PATCH/DELETE request.

    WHY THIS APPROACH INSTEAD OF PER-FORM CSRF TOKENS: this app's session
    cookie already uses SameSite=Lax (see app/main.py), which is itself a
    strong, browser-enforced CSRF defense in all modern browsers -- a
    cross-site <form> submit or fetch() simply doesn't send the cookie, so
    the request would hit login_required's "not logged in" path regardless.
    This middleware is defense-in-depth on top of that (covers older
    browsers, or any cookie-policy override) using the same technique
    Django offers as an alternative to its per-form token under
    CSRF_USE_SESSIONS. Retrofitting a hidden csrf_token input into every
    <form> across 3 large templates (dozens of forms) is a much larger,
    separate change -- flagged here rather than silently skipped.
    """
    SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}

    def __init__(self, app, allowed_hosts=None):
        super().__init__(app)
        self.allowed_hosts = set(allowed_hosts or [])

    async def dispatch(self, request, call_next):
        if request.method not in self.SAFE_METHODS:
            source = request.headers.get("origin") or request.headers.get("referer")
            if source:
                source_host = urlparse(source).netloc.lower()
                request_host = (request.headers.get("host") or "").lower()
                if source_host and source_host != request_host and source_host not in self.allowed_hosts:
                    return JSONResponse(
                        {"error": "Cross-site request blocked (CSRF protection)."},
                        status_code=403,
                    )
            # No Origin/Referer at all: allow through -- some legitimate
            # clients omit both, and SameSite=Lax is still enforced by the
            # browser as the primary defense in that case.
        return await call_next(request)


# ═══════════════════════════════════════════════════════════════════════════
# 3. RATE LIMITING (in-memory -- see limitation note below)
# ═══════════════════════════════════════════════════════════════════════════
class _SlidingWindowLimiter:
    def __init__(self):
        self._hits = defaultdict(deque)

    def allow(self, key: str, limit: int, window_seconds: int) -> bool:
        now = time.time()
        q = self._hits[key]
        while q and q[0] < now - window_seconds:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


_limiter = _SlidingWindowLimiter()

# path -> (max requests, window in seconds). Focused on brute-force-prone
# endpoints (login/registration) rather than every route.
RATE_LIMITS = {
    "/login": (8, 60),
    "/superadmin/login": (8, 60),
    "/register": (15, 60),
    "/forgot-password": (5, 300),
}


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    IMPORTANT LIMITATION: this is in-memory, per-process. It resets on every
    restart and does NOT share state across multiple worker processes (same
    caveat as the SECRET_KEY fallback in app/config.py -- see that file's
    comments). Fine for a single-process deployment; if you run with
    --workers > 1 or multiple containers behind a load balancer, replace
    _SlidingWindowLimiter's storage with Redis (e.g. the `limits` package's
    RedisStorage) so all workers share one counter.
    """
    async def dispatch(self, request, call_next):
        limits = RATE_LIMITS.get(request.url.path)
        if limits and request.method == "POST":
            client_ip = request.client.host if request.client else "unknown"
            key = f"{request.url.path}:{client_ip}"
            limit, window = limits
            if not _limiter.allow(key, limit, window):
                return JSONResponse(
                    {"error": "Too many attempts. Please wait a minute and try again."},
                    status_code=429,
                )
        return await call_next(request)


# ═══════════════════════════════════════════════════════════════════════════
# 4. PER-ACCOUNT LOGIN LOCKOUT (in addition to the IP-based rate limit above)
# ═══════════════════════════════════════════════════════════════════════════
class _AccountLockout:
    """
    RateLimitMiddleware above is per-IP -- it doesn't stop a distributed
    brute-force where an attacker spreads guesses for ONE account across
    many IPs/proxies. This tracks failures per ACCOUNT identifier instead
    (e.g. "company_code:username") regardless of which IP they came from,
    and locks that specific account out temporarily after repeated failures.

    Same in-memory/single-process limitation as _SlidingWindowLimiter above.
    """
    THRESHOLD = 6          # failed attempts
    WINDOW = 15 * 60       # within this many seconds
    LOCKOUT_SECONDS = 15 * 60

    def __init__(self):
        self._failures = defaultdict(deque)
        self._locked_until = {}

    def is_locked(self, key: str):
        until = self._locked_until.get(key)
        if until and time.time() < until:
            return int(until - time.time())
        if until:
            del self._locked_until[key]
        return None

    def record_failure(self, key: str):
        now = time.time()
        q = self._failures[key]
        q.append(now)
        while q and q[0] < now - self.WINDOW:
            q.popleft()
        if len(q) >= self.THRESHOLD:
            self._locked_until[key] = now + self.LOCKOUT_SECONDS

    def record_success(self, key: str):
        self._failures.pop(key, None)
        self._locked_until.pop(key, None)


account_lockout = _AccountLockout()


# ═══════════════════════════════════════════════════════════════════════════
# NOTES on the other items from the original SECURITY spec
# ═══════════════════════════════════════════════════════════════════════════
# - SQL Injection Prevention: already handled -- every query in this codebase
#   uses psycopg2 parameterized queries (%s placeholders), never manual
#   string interpolation into SQL. Nothing to add here; just don't regress it.
# - XSS Protection: Jinja2 autoescaping is on by default for .html templates
#   (app/compat.py's Jinja2Templates uses Jinja's default autoescape=True for
#   .html), so `{{ user_input }}` in templates is escaped automatically.
#   The one thing to audit periodically: any `{{ ... | safe }}` filter usage
#   bypasses escaping deliberately -- grep for `| safe` and `|safe` in the
#   templates and confirm each one is only ever fed trusted/server-generated
#   content, never raw user input.
# - CORS: wired in app/main.py via CORSMiddleware, using ALLOWED_ORIGINS from
#   app/config.py (CORS_ORIGINS env var) -- empty by default, meaning no
#   cross-origin JS access is permitted until you explicitly list origins.
