"""
Shared request-level middleware for the SaaS app.

app/security_middleware.py already has BodySizeLimitMiddleware for the
legacy Flask-style app (app/main.py), scoped to app/config.py's
MAX_UPLOAD_SIZE. The SaaS app (saas/main.py) is a separate FastAPI
application with its own config and its own set of upload endpoints
(saas/routers/*.py), and previously had no equivalent -- Nginx's
`client_max_body_size` and per-endpoint `validate_upload(max_size=...)`
calls were the only limits, meaning a large non-multipart request (or one
sent directly to the FastAPI process, bypassing Nginx) would still be fully
buffered in memory before any endpoint code got a chance to reject it.
"""
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


class _BodyTooLarge(Exception):
    pass


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests whose body exceeds `limit` bytes -- checked via
    Content-Length up front, and enforced while streaming for chunked/
    streamed bodies that omit or lie about Content-Length.

    IMPORTANT: this is app-level defense-in-depth, not a replacement for a
    hard limit at your reverse proxy (e.g. nginx's `client_max_body_size`) --
    set both in production."""

    def __init__(self, app, limit: int):
        super().__init__(app)
        self.limit = limit

    async def dispatch(self, request, call_next):
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > self.limit:
                    return JSONResponse({"error": "Request body too large."}, status_code=413)
            except ValueError:
                pass

        total = 0
        limit = self.limit
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
