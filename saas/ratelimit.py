"""
Rate limiting + brute-force protection (Phase 6).

Sliding-window counters kept in-process (single instance). Two layers:

  - API throttle: per-client-IP request budget over a rolling window; a 429 is
    returned once the budget is exhausted (applied via middleware in main.py).
  - Login lockout: failed credential checks are counted per
    (company_code, username) and per IP; the account/IP is locked for a window
    after too many failures (enforced inside the login endpoints).
"""
import threading
import time

DEFAULT_WINDOW = 60            # seconds
DEFAULT_LIMIT = 300            # requests per window per IP
LOGIN_MAX_ATTEMPTS = 5         # failed logins before lockout
LOGIN_LOCKOUT = 15 * 60        # lockout window (seconds)


class SlidingWindow:
    def __init__(self, limit: int, window: float = DEFAULT_WINDOW):
        self.limit = limit
        self.window = window
        self._hits: list[float] = []
        self._lock = threading.Lock()

    def allow(self, now: float | None = None) -> tuple[bool, int, int]:
        """Return (allowed, hits_in_window, retry_after_seconds)."""
        now = time.time() if now is None else now
        with self._lock:
            cutoff = now - self.window
            self._hits = [t for t in self._hits if t > cutoff]
            if len(self._hits) >= self.limit:
                retry = int(self.window - (now - self._hits[0])) + 1
                return False, len(self._hits), retry
            self._hits.append(now)
            return True, len(self._hits), 0

    def peek(self, now: float | None = None) -> tuple[bool, int, int]:
        """Budget status without recording the hit."""
        now = time.time() if now is None else now
        with self._lock:
            cutoff = now - self.window
            hits = [t for t in self._hits if t > cutoff]
            if len(hits) >= self.limit:
                retry = int(self.window - (now - hits[0])) + 1
                return False, len(hits), retry
            return True, len(hits), 0


class RateLimiter:
    """Key -> SlidingWindow with automatic cleanup of stale keys."""

    def __init__(self, limit: int = DEFAULT_LIMIT, window: float = DEFAULT_WINDOW,
                 max_keys: int = 10000):
        self.limit = limit
        self.window = window
        self.max_keys = max_keys
        self._buckets: dict[str, tuple[SlidingWindow, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> tuple[bool, int, int]:
        now = time.time() if now is None else now
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= self.max_keys:
                    self._buckets = {k: v for k, v in self._buckets.items()
                                     if now - v[1] < self.window}
                bucket = (SlidingWindow(self.limit, self.window), now)
                self._buckets[key] = bucket
            else:
                # prune keys idle longer than one window
                if now - bucket[1] > self.window:
                    self._buckets[key] = (bucket[0], now)
            bucket = self._buckets[key]
        return bucket[0].allow(now)

    def check(self, key: str, now: float | None = None) -> tuple[bool, int, int]:
        """Peek at a key's budget without consuming it."""
        now = time.time() if now is None else now
        bucket = self._buckets.get(key)
        if bucket is None:
            return True, 0, 0
        return bucket[0].peek(now)

    def reset(self, key: str):
        with self._lock:
            self._buckets.pop(key, None)


# ── Shared instances ─────────────────────────────────────────────────────────

api_limiter = RateLimiter(DEFAULT_LIMIT, DEFAULT_WINDOW)

# login lockout: key "acct:<code>:<username>" or "ip:<addr>"
_login = RateLimiter(LOGIN_MAX_ATTEMPTS, LOGIN_LOCKOUT)


def client_key(request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def check_api_budget(request) -> bool:
    """True when the request is within budget. Called by middleware."""
    allowed, _, retry = api_limiter.allow(client_key(request))
    if not allowed:
        request.state.rate_limit_retry = retry
    return allowed


def login_allowed(request, company_code: str, username: str) -> bool:
    """False when either the account or IP is locked out (no budget consumed)."""
    account_ok, _, _ = _login.check(f"acct:{company_code}:{username.lower()}")
    ip_ok, _, _ = _login.check(f"ip:{client_key(request)}")
    return account_ok and ip_ok


def login_failed(request, company_code: str, username: str) -> None:
    """Record a failed attempt (a key's budget is consumed on failure only)."""
    _login.allow(f"acct:{company_code}:{username.lower()}")


def login_succeeded(request, company_code: str, username: str) -> None:
    _login.reset(f"acct:{company_code}:{username.lower()}")


def login_reset(request, company_code: str, username: str) -> None:
    """Clear the account lockout (e.g. when a valid password entered the MFA
    stage)."""
    _login.reset(f"acct:{company_code}:{username.lower()}")
