"""
Flask-compatibility shim over FastAPI / Starlette.

WHY THIS EXISTS
----------------
The original app.py is ~6,000 lines of Flask route handlers that call global
`request`/`session` proxies, `render_template`, `redirect(url_for(...))`,
`flash`, `jsonify`, `send_file`. Rewriting all of that by hand into idiomatic
FastAPI would touch nearly every line of business logic in a pharma production
system -- exactly where migration bugs are most costly.

Instead, this module reproduces Flask's request-handling *ergonomics* on top
of FastAPI, so the ported route files are almost line-for-line identical to
the original Flask code. Only the decorator (`@app.route` -> `@router.route`)
and imports change in most functions.

HOW IT WORKS
------------
- `FlaskCompatRouter.route()` mimics `Flask.route()`, including `<int:id>`
  style URL converters, method lists, and stacking multiple `@route()` calls
  on one function.
- Each registered endpoint is wrapped in an async function that:
    1. stores the current Starlette `Request` in a contextvar
    2. eagerly awaits form/JSON body parsing (Starlette requires `await`,
       Flask does not) and caches it on `request.state`
    3. runs the ORIGINAL (synchronous) view function in a worker thread via
       `run_in_threadpool`, so blocking DB calls don't block the event loop
    4. coerces the return value (Flask supports returning a bare Response,
       `(body, status)`, `(body, status, headers)`, dict, or string) into a
       proper Starlette Response.
- Module-level `request` / `session` objects are thin proxies that read from
  the contextvar, so existing code (including helper functions that were
  never passed a `request` argument in the original app, e.g. `log_activity`)
  keeps working unmodified.

LIMITATION: because contextvars are used instead of true per-object request
threading, code must not spawn *new* asyncio tasks / background threads that
outlive the request and expect `request`/`session` to still resolve -- none
of the ported code does this (BackgroundTasks are used explicitly instead).
"""
import io
import mimetypes
import os
from contextvars import ContextVar

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, PlainTextResponse,
    RedirectResponse, Response, StreamingResponse,
)
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile as StarletteUploadFile

_request_ctx: ContextVar = ContextVar("current_request", default=None)
_app_ref = {"app": None}

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")
templates = Jinja2Templates(directory=TEMPLATES_DIR)


def set_app(app: FastAPI):
    _app_ref["app"] = app


def _current_request() -> Request:
    req = _request_ctx.get()
    if req is None:
        raise RuntimeError("No active request in this context (compat.request used outside a request)")
    return req


# ═══════════════════════════════════════════════════════════════════════════
# URL rule conversion: Flask <int:id> / <path:filename> -> FastAPI {id} etc.
# ═══════════════════════════════════════════════════════════════════════════
import re as _re

_CONVERTER_RE = _re.compile(r"<(?:(?P<type>[a-zA-Z_]+):)?(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)>")
_TYPE_MAP = {"int": int, "float": float, "path": str, "string": str, None: str}


def _convert_flask_rule(rule: str):
    converters = {}

    def _sub(m):
        ptype = m.group("type")
        name = m.group("name")
        converters[name] = _TYPE_MAP.get(ptype, str)
        if ptype == "path":
            # Starlette requires the explicit `:path` converter in the path
            # template itself to allow a segment to contain "/" -- a plain
            # "{name}" only matches a single path segment. Flask's <path:x>
            # matches everything (slashes included), so this must carry over.
            return "{%s:path}" % name
        return "{%s}" % name

    fastapi_path = _CONVERTER_RE.sub(_sub, rule)
    return fastapi_path, converters


def _coerce_response(result, request: Request) -> Response:
    """Mimic Flask's flexible view-function return values."""
    status_code = None
    headers = None
    body = result

    if isinstance(result, tuple):
        if len(result) == 2:
            body, status_code = result
        elif len(result) == 3:
            body, status_code, headers = result

    if isinstance(body, Response):
        if status_code is not None:
            body.status_code = status_code
        if headers:
            body.headers.update(headers)
        return body

    if isinstance(body, (dict, list)):
        return JSONResponse(body, status_code=status_code or 200, headers=headers)

    if isinstance(body, str):
        return HTMLResponse(body, status_code=status_code or 200, headers=headers)

    if body is None:
        return Response(status_code=status_code or 204, headers=headers)

    # Fallback -- best effort
    return PlainTextResponse(str(body), status_code=status_code or 200, headers=headers)


class FlaskCompatRouter(APIRouter):
    """APIRouter with a Flask-style `.route()` decorator."""

    def route(self, rule, methods=None, **opts):
        methods = methods or ["GET"]
        fastapi_path, converters = _convert_flask_rule(rule)

        def decorator(func):
            name = opts.get("endpoint", func.__name__)

            # NOTE: deliberately NOT using @functools.wraps(func) here.
            # wraps() sets endpoint.__wrapped__ = func, and FastAPI's dependency
            # analysis calls inspect.signature() which follows __wrapped__ by
            # default -- it would "see" the original view function's signature
            # (which takes no `request` arg) instead of this wrapper's, and then
            # call the wrapper with zero arguments. Keep this wrapper's own
            # signature (request: Request) visible to FastAPI.
            async def endpoint(request: Request):
                token = _request_ctx.set(request)
                try:
                    ct = request.headers.get("content-type", "")
                    if request.method in ("POST", "PUT", "PATCH") and (
                        "multipart/form-data" in ct or "application/x-www-form-urlencoded" in ct
                    ):
                        request.state.form = await request.form()
                    else:
                        request.state.form = None

                    if "application/json" in ct:
                        try:
                            request.state.json = await request.json()
                        except Exception:
                            request.state.json = None
                    else:
                        request.state.json = None

                    kwargs = {}
                    for pname, ptype in converters.items():
                        raw = request.path_params.get(pname)
                        try:
                            kwargs[pname] = ptype(raw) if raw is not None else raw
                        except (TypeError, ValueError):
                            kwargs[pname] = raw

                    result = await run_in_threadpool(func, **kwargs)
                    return _coerce_response(result, request)
                finally:
                    _request_ctx.reset(token)

            endpoint.__name__ = name
            self.add_api_route(
                fastapi_path, endpoint, methods=methods, name=name,
                include_in_schema=False,
            )
            return func

        return decorator


# ═══════════════════════════════════════════════════════════════════════════
# request / session proxies
# ═══════════════════════════════════════════════════════════════════════════

class _FileStorageAdapter:
    """Wraps a Starlette UploadFile so `.filename` / `.save(path)` behave like
    werkzeug's FileStorage (used by the original Flask code)."""

    def __init__(self, upload_file: StarletteUploadFile):
        self._uf = upload_file
        self.filename = upload_file.filename
        self.content_type = upload_file.content_type

    def save(self, dst_path):
        self._uf.file.seek(0)
        with open(dst_path, "wb") as out:
            while True:
                chunk = self._uf.file.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)

    def read(self):
        self._uf.file.seek(0)
        return self._uf.file.read()

    @property
    def stream(self):
        return self._uf.file


class _FilesProxy:
    def _form(self):
        req = _current_request()
        return req.state.form or {}

    def __contains__(self, key):
        form = self._form()
        val = form.get(key) if hasattr(form, "get") else None
        return isinstance(val, StarletteUploadFile)

    def __getitem__(self, key):
        form = self._form()
        val = form.get(key)
        if val is None or not isinstance(val, StarletteUploadFile):
            raise KeyError(key)
        return _FileStorageAdapter(val)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default


class _FormProxy:
    """dict-like proxy over the pre-parsed Starlette FormData, excluding file fields."""
    def _form(self):
        req = _current_request()
        return req.state.form or {}

    def get(self, key, default=None):
        form = self._form()
        val = form.get(key) if hasattr(form, "get") else default
        if isinstance(val, StarletteUploadFile):
            return default
        return val if val is not None else default

    def getlist(self, key):
        form = self._form()
        if hasattr(form, "getlist"):
            return [v for v in form.getlist(key) if not isinstance(v, StarletteUploadFile)]
        return []

    def __contains__(self, key):
        return self.get(key) is not None

    def __getitem__(self, key):
        v = self.get(key)
        if v is None:
            raise KeyError(key)
        return v

    def to_dict(self, flat=True):
        form = self._form()
        return {k: v for k, v in (form.multi_items() if hasattr(form, "multi_items") else [])
                if not isinstance(v, StarletteUploadFile)}


class _ArgsProxy:
    def get(self, key, default=None, type=None):
        req = _current_request()
        val = req.query_params.get(key, default)
        if type is not None and val is not None:
            try:
                val = type(val)
            except (TypeError, ValueError):
                val = default
        return val

    def getlist(self, key):
        req = _current_request()
        return req.query_params.getlist(key)

    def __contains__(self, key):
        return self.get(key) is not None

    def __getitem__(self, key):
        v = self.get(key)
        if v is None:
            raise KeyError(key)
        return v


class _RequestProxy:
    form = _FormProxy()
    args = _ArgsProxy()
    files = _FilesProxy()

    @property
    def method(self):
        return _current_request().method

    @property
    def path(self):
        return _current_request().url.path

    @property
    def full_path(self):
        req = _current_request()
        return str(req.url)

    @property
    def remote_addr(self):
        req = _current_request()
        return req.client.host if req.client else None

    @property
    def host_url(self):
        return str(_current_request().base_url)

    @property
    def headers(self):
        return _current_request().headers

    @property
    def cookies(self):
        return _current_request().cookies

    @property
    def is_json(self):
        ct = _current_request().headers.get("content-type", "")
        return "application/json" in ct

    def get_json(self, silent=False):
        req = _current_request()
        return getattr(req.state, "json", None) or ({} if silent else None)

    @property
    def json(self):
        req = _current_request()
        return getattr(req.state, "json", None)

    @property
    def raw(self):
        return _current_request()

    def get(self, key, default=None):
        """Mapping-style access over the ASGI scope -- needed because Starlette's
        _TemplateResponse.__call__ does request.get('extensions', {}) on
        whatever object is in context['request']."""
        try:
            return _current_request().get(key, default)
        except Exception:
            return default


request = _RequestProxy()


class _SessionProxy:
    """Delegates to Starlette SessionMiddleware's request.session dict --
    behaves like flask.session for the subset of the API this app uses."""

    def _dict(self):
        return _current_request().session

    def get(self, key, default=None):
        return self._dict().get(key, default)

    def pop(self, key, default=None):
        return self._dict().pop(key, default)

    def setdefault(self, key, default):
        return self._dict().setdefault(key, default)

    def __contains__(self, key):
        return key in self._dict()

    def __getitem__(self, key):
        return self._dict()[key]

    def __setitem__(self, key, value):
        self._dict()[key] = value

    def __delitem__(self, key):
        del self._dict()[key]

    def clear(self):
        self._dict().clear()


session = _SessionProxy()


# ═══════════════════════════════════════════════════════════════════════════
# render_template / redirect / url_for / flash / jsonify / send_file
# ═══════════════════════════════════════════════════════════════════════════

def render_template(template_name, **context):
    req = _current_request()
    # Templates use Flask-style `request.form.get(...)` (synchronous property),
    # not Starlette's `request.form()` (async method) -- pass our compat proxy
    # as "request" in the context. Jinja2Templates.TemplateResponse uses
    # context.setdefault("request", ...) so this takes precedence.
    context["request"] = request
    return templates.TemplateResponse(req, template_name, context)


def redirect(location, code=302):
    return RedirectResponse(url=location, status_code=code)


def url_for(endpoint, **kwargs):
    app = _app_ref["app"]
    if app is None:
        raise RuntimeError("compat.set_app() was never called")
    path_params = {k: v for k, v in kwargs.items() if k not in ("_external",)}
    try:
        url_path = app.url_path_for(endpoint, **path_params)
        return str(url_path)
    except Exception:
        # fall back: some legacy Flask endpoints accepted extra query kwargs
        return "/"


def flash(message, category="message"):
    sess = _current_request().session
    sess.setdefault("_flashes", [])
    sess["_flashes"].append([category, message])


def get_flashed_messages(with_categories=False, category_filter=()):
    req = _request_ctx.get()
    if req is None:
        return []
    sess = req.session
    flashes = sess.pop("_flashes", [])
    if category_filter:
        flashes = [f for f in flashes if f[0] in category_filter]
    if with_categories:
        return flashes
    return [f[1] for f in flashes]


def jsonify(*args, **kwargs):
    if args:
        data = args[0] if len(args) == 1 else list(args)
    else:
        data = kwargs
    return JSONResponse(data)


def send_file(path_or_buffer, mimetype=None, as_attachment=False, download_name=None, **kwargs):
    filename = download_name or kwargs.get("attachment_filename")
    if isinstance(path_or_buffer, (str, os.PathLike)):
        return FileResponse(
            path_or_buffer,
            media_type=mimetype,
            filename=filename if as_attachment else None,
        )
    # BytesIO / file-like object
    buf = path_or_buffer
    buf.seek(0)
    media_type = mimetype or "application/octet-stream"
    headers = {}
    if as_attachment and filename:
        headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return StreamingResponse(buf, media_type=media_type, headers=headers)


def send_from_directory(directory, filename, **kwargs):
    full_path = os.path.join(directory, filename)
    if not os.path.abspath(full_path).startswith(os.path.abspath(directory)):
        from fastapi import HTTPException
        raise HTTPException(status_code=404)
    if not os.path.isfile(full_path):
        from fastapi import HTTPException
        raise HTTPException(status_code=404)
    mt, _ = mimetypes.guess_type(full_path)
    return FileResponse(full_path, media_type=mt)


def abort(code, description=None):
    from fastapi import HTTPException
    raise HTTPException(status_code=code, detail=description)
