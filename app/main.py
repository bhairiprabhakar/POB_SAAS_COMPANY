"""
App factory. Run with:  uvicorn app.main:app --host 0.0.0.0 --port 5000
"""
import os

import anyio.to_thread
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import compat
from .compat import templates
from .config import SECRET_KEY, ALLOWED_ORIGINS, ENFORCE_HTTPS, THREAD_POOL_SIZE
from .database import init_db
from .helpers import ROLE_LABELS, ROLES
from .routers import agent_routes, main_routes, superadmin_routes
from .security_middleware import (
    SecurityHeadersMiddleware, CSRFOriginCheckMiddleware, RateLimitMiddleware,
    BodySizeLimitMiddleware,
)

app = FastAPI(title="Data Extraction", docs_url=None, redoc_url=None, openapi_url=None)

_STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

# Middleware order matters: Starlette applies the LAST-added middleware as
# the OUTERMOST layer (it sees the request first / response last). We want,
# from outermost to innermost: security headers -> body size limit -> rate
# limit -> CSRF check -> CORS -> sessions -> (routes). So they're added here
# in the REVERSE of that order.
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    session_cookie="session",
    same_site="lax",
    https_only=ENFORCE_HTTPS,  # set ENFORCE_HTTPS=true once served over HTTPS
    max_age=None,       # browser-session cookie; app-level 15-min inactivity
                          # timeout is enforced separately in app/auth.py
)
if ALLOWED_ORIGINS:
    # Empty by default (see .env's CORS_ORIGINS) -- no cross-origin JS access
    # until you explicitly list origins that should be allowed to call this
    # API from client-side JS running on another domain.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["*"],
    )
app.add_middleware(CSRFOriginCheckMiddleware, allowed_hosts=ALLOWED_ORIGINS)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(BodySizeLimitMiddleware)
app.add_middleware(SecurityHeadersMiddleware)

compat.set_app(app)

# Jinja globals -- mirrors the original @app.context_processor + Flask's
# built-in `get_flashed_messages`.
templates.env.globals["ROLE_LABELS"] = ROLE_LABELS
templates.env.globals["ROLES"] = ROLES
templates.env.globals["get_flashed_messages"] = compat.get_flashed_messages

app.include_router(superadmin_routes.router)
app.include_router(agent_routes.router)
app.include_router(main_routes.router)


@app.on_event("startup")
def _startup():
    init_db()


@app.on_event("startup")
async def _raise_thread_limit():
    # Must run inside a running event loop (anyio requires it to resolve the
    # backend) -- an @app.on_event("startup") async function guarantees that.
    limiter = anyio.to_thread.current_default_thread_limiter()
    limiter.total_tokens = THREAD_POOL_SIZE


if __name__ == "__main__":
    import uvicorn
    print("\n" + "=" * 65)
    print("  Data Extraction -- Enterprise SaaS (PostgreSQL, FastAPI)")
    print("  Company Portal  : http://localhost:5000")
    print("  Super Admin     : http://localhost:5000/superadmin")
    print("=" * 65)
    print("  Extraction   : Google Gemini (2.5-flash for text PDFs, 3.5-flash for visual)")
    print("=" * 65 + "\n")
    uvicorn.run("app.main:app", host="0.0.0.0", port=5000, reload=False)
