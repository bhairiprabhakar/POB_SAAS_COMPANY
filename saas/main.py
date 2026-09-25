"""
SaaS platform application factory.

Run:  uvicorn saas.main:app --host 0.0.0.0 --port 8000

Routes:
  /api/v1/...  REST API (see saas/routers/*)
  /            React SPA (served from web/dist when built)
  /healthz     liveness probe
  /metrics     Prometheus text metrics
"""
import os
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response

from . import config
from .middleware import BodySizeLimitMiddleware
from .platform_db import init_platform_db
from .pools import start_idle_sweeper
from .routers import (
    analytics, apikeys, audit, auth, company, dashboards, gratification, inventory, jobs,
    masters, mfa, notifications, pob, payouts, recovery, reports, settings,
    storage, superadmin, upi, verification, visits, webhooks, workflows,
)

app = FastAPI(title="POB SaaS Platform", version="1.0.0")

# Small buffer over MAX_UPLOAD_SIZE for multipart overhead (boundaries,
# field headers) -- mirrors app/security_middleware.py's BodySizeLimitMiddleware.
app.add_middleware(BodySizeLimitMiddleware, limit=config.MAX_UPLOAD_SIZE + (5 * 1024 * 1024))

if config.CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    """Per-IP budget for /api/* calls (Phase 6). Login endpoints have their
    own, tighter brute-force limit (see routers/auth.py)."""
    if request.url.path.startswith("/api/"):
        from . import ratelimit
        if not ratelimit.check_api_budget(request):
            retry = getattr(request.state, "rate_limit_retry", 60)
            return JSONResponse(
                {"detail": "Rate limit exceeded",
                 "retry_after": retry},
                status_code=429,
                headers={"Retry-After": str(retry)},
            )
    return await call_next(request)


app.include_router(auth.router)
app.include_router(mfa.router)
app.include_router(recovery.router)
app.include_router(apikeys.router)
app.include_router(webhooks.router)
app.include_router(jobs.router)
app.include_router(superadmin.router)
app.include_router(company.router)
app.include_router(masters.router)
app.include_router(pob.router)
app.include_router(verification.router)
app.include_router(gratification.router)
app.include_router(visits.router)
app.include_router(upi.router)
app.include_router(analytics.router)
app.include_router(dashboards.router)
app.include_router(reports.router)
app.include_router(notifications.router)
app.include_router(settings.router)
app.include_router(workflows.router)
app.include_router(inventory.router)
app.include_router(payouts.router)
app.include_router(audit.router)
app.include_router(storage.router)


# ── Liveness / metrics ──────────────────────────────────────────────────────

@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "pob-saas", "ts": time.time()}


@app.get("/metrics")
def metrics():
    from .pools import _pools as tenant_pools
    lines = [
        "# HELP pob_saas_tenant_pools Number of open tenant connection pools",
        "# TYPE pob_saas_tenant_pools gauge",
        f"pob_saas_tenant_pools {len(tenant_pools)}",
        "# HELP pob_saas_up Whether the service is up",
        "# TYPE pob_saas_up gauge",
        "pob_saas_up 1",
    ]
    return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


# ── Startup ─────────────────────────────────────────────────────────────────

@app.on_event("startup")
def _startup():
    init_platform_db()
    start_idle_sweeper()
    from .scheduler import start_scheduler
    start_scheduler()


# ── React SPA (served from web/dist when built) ─────────────────────────────

_DIST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "dist")

# Paths that must never hit the SPA fallback (API, probes, docs, uploads).
_SPA_EXCLUDED_PREFIXES = ("api", "healthz", "metrics", "docs", "redoc",
                          "openapi.json", "uploads", "s3")
_SPA_FILES = {".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
              ".woff", ".woff2", ".ttf", ".map", ".webmanifest"}


def _spa_asset(rel: str):
    """Resolve rel (no leading slash) to a real file under web/dist, if any."""
    full = os.path.realpath(os.path.join(_DIST, rel))
    if full.startswith(os.path.realpath(_DIST) + os.sep) and os.path.isfile(full):
        return full
    return None


@app.get("/", include_in_schema=False)
@app.get("/app", include_in_schema=False)
@app.get("/superadmin-panel", include_in_schema=False)
@app.get("/app/{rest:path}", include_in_schema=False)
@app.get("/{rest:path}", include_in_schema=False)
def spa(request: Request, rest: str = ""):
    # Real static assets built by Vite. Vite content-hashes filenames, so a
    # long max-age is safe -- the browser can only reuse a valid build.
    asset = _spa_asset(rest) if rest else None
    if asset:
        return FileResponse(asset, headers={"Cache-Control": "public, max-age=31536000, immutable"})

    # Anything that looks like a static file but doesn't exist -> real 404,
    # not the HTML fallback (avoids masking broken asset links).
    if rest and os.path.splitext(rest)[1] in _SPA_FILES:
        return JSONResponse({"detail": "Not Found"}, status_code=404)

    index = os.path.join(_DIST, "index.html")
    if not os.path.isfile(index):
        return JSONResponse({"message": "Frontend not built. Run: cd web && npm install && npm run build"},
                            status_code=200)

    # Non-file routes under the SPA (client-side routing) fall back to index.
    # No-cache on index.html: it's tiny and always points at the current build.
    if not rest or rest.split("/")[0] not in _SPA_EXCLUDED_PREFIXES:
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return JSONResponse({"detail": "Not Found"}, status_code=404)
