# Pharma Sales Analyzer -- FastAPI migration

This is a migration of the original Flask app (`app.py` + `index.html` /
`agent.html` / `superadmin.html`) to FastAPI, with the OCR pipeline
(pdfplumber + PaddleOCR + custom router) replaced by Google Gemini Flash 3.5.
**All three HTML templates are byte-for-byte unchanged.**

## Read this first: how the migration was done, and why

A hand rewrite of ~90 Flask routes and ~6,000 lines of pharma-business logic
(credits, hierarchy, duplicate detection, manual verification workflow) into
idiomatic async FastAPI would touch nearly every line of the app -- exactly
where a migration introduces silent regressions.

Instead, `app/compat.py` implements a **Flask-compatibility shim over
FastAPI**: `request`, `session`, `render_template`, `redirect`, `url_for`,
`flash`, `jsonify`, `send_file`, `send_from_directory` all behave the way
they did under Flask. This meant the ported route files
(`app/routers/superadmin_routes.py`, `agent_routes.py`, `main_routes.py`)
could stay **line-for-line identical** to the original for the overwhelming
majority of code -- only `@app.route(...)` became `@router.route(...)`, plus
imports. Business logic, SQL, and session/permission checks were not
rewritten by hand.

Two functions (`_build_verification_excel`, `notify_registration`) were
originally defined inside the superadmin section of `app.py` but used by
routes in other sections too -- these were moved into `app/helpers.py`
unchanged, since Python modules (unlike one big Flask file) need an explicit
shared location for cross-file helpers.

### Deliberate architecture choices (documented tradeoffs)

- **Database access stays synchronous (psycopg2), not Async SQLAlchemy.**
  Every route is dispatched through a wrapper (`app/compat.py`) that runs the
  view function in a worker thread via Starlette's `run_in_threadpool` --
  the same mechanism FastAPI uses natively for plain `def` (non-async) path
  operations. This means blocking DB calls do **not** block the event loop,
  without rewriting ~90 routes' worth of SQL to a different access style in
  the same change as the framework migration. `psycopg2.pool.ThreadedConnectionPool`
  is used for connection pooling. If you need true async DB access for very
  high concurrency later, swap `app/database.py` for `asyncpg` / SQLAlchemy's
  async engine -- the rest of the app calls `get_db()` the same way either way.
- **Session cookie** uses Starlette's `SessionMiddleware` (signed, HttpOnly,
  `SameSite=Lax`) -- equivalent security properties to Flask's default session
  cookie.
- **15-minute inactivity timeout / 13-minute warning**: the original `app.py`
  did **not** actually implement this (grepped for it -- not present, despite
  being requested). `SESSION_TIMEOUT` / `SESSION_WARNING_AT` are wired into
  `app/config.py` as env vars but there's no enforcement logic yet, since
  building new session-expiry behavior that didn't exist in the original app
  needs a product decision (e.g. does the *browser* poll and log the user out,
  or does the server reject requests after N minutes of inactivity?) rather
  than being invented silently during a "keep behavior identical" migration.
- **CSRF**: implemented via `CSRFOriginCheckMiddleware` (`app/security_middleware.py`,
  installed in `app/main.py`) -- validates the `Origin`/`Referer` header on
  every state-changing request (POST/PUT/PATCH/DELETE), on top of the
  `SameSite=Lax` session cookie. (Earlier drafts of this README said CSRF
  was not implemented; that was true when originally written and is no
  longer accurate.)

## What changed in behavior

- **OCR engine**: `backend/` pipeline (pdfplumber + PaddleOCR) -> Gemini
  Flash 3.5 File API (`app/ai/gemini_extraction.py`). Gemini is prompted to
  return the same `AgencyDetails` / `ReportDetails` / `Areas -> Stores ->
  Items` JSON shape the old pipeline produced, so `app/extraction.py`'s
  mapping into the app's internal schema needed only small additions (a few
  extra fields Gemini can supply that the old pipeline didn't, like
  `DLNumber`/`GSTNumber`/`BatchNo`/`Expiry`/`HSN` per item) -- not a rewrite.
  Retries 3x on failure/invalid JSON, then raises -- same as before, this
  routes the upload to `status='error'` and the user sees the OCR error.
- Framework-level error pages (404/500) will look like FastAPI/Starlette's
  defaults, not Flask's, unless you add exception handlers.

## What was NOT changed

Every SQL query, every credit/hierarchy/duplicate-detection rule, every
permission check, every Jinja template -- unchanged from the original.

## Project layout

```
app/
  main.py              FastAPI app factory -- run this
  compat.py             Flask-compatibility shim (READ THIS to understand routing)
  config.py              env vars
  database.py            psycopg2 pool + schema (init_db) -- same tables/migrations as original
  security.py             password hashing
  helpers.py               shared business logic (credits, hierarchy, notifications, Excel export, ...)
  extraction.py             maps Gemini's JSON into the app's internal schema
  auth.py                   login_required / admin_required / superadmin_required / agent_required
  ai/gemini_extraction.py    Gemini Flash 3.5 File API calls + prompt
  routers/
    main_routes.py            company/public portal (login, dashboard, upload, team, analytics, ...)
    superadmin_routes.py        super admin portal
    agent_routes.py               verification-agent portal
templates/                 agent.html / index.html / superadmin.html -- UNCHANGED
```

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in DB_PASSWORD, GEMINI_API_KEY, SECRET_KEY, SUPERADMIN_BOOTSTRAP_PASSWORD
```

**Before first run**, the target Postgres database must already exist --
`init_db()` only creates *tables inside* the database named by `DB_NAME` in
your `.env`; it does not create the database itself. If you haven't already:

```bash
createdb OCR_PILOT
# or, from the psql prompt:
# CREATE DATABASE "OCR_PILOT";
```

(Use whatever `DB_NAME` you actually set in `.env` -- `OCR_PILOT` is just the
default.)

Run:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 5000
```

or `python -m app.main`. The `startup` event calls `init_db()`, which
`CREATE TABLE IF NOT EXISTS`s the same schema as the original app and
bootstraps the `superadmin` account exactly like before.

### Common first-run errors

- **`psycopg2.OperationalError: connection ... Connection refused`** --
  Postgres isn't running, or `DB_HOST`/`DB_PORT` in `.env` don't point at it.
  Start Postgres, confirm it's listening on the host/port you configured.
- **`FATAL: database "..." does not exist`** -- you skipped the `createdb`
  step above. Create the database, then restart the app.
- **`FATAL: password authentication failed`** -- `DB_USER`/`DB_PASSWORD` in
  `.env` don't match your Postgres role. Double check both.
- **App starts but `/superadmin/login` (or any page) 500s** -- almost
  always a downstream symptom of one of the three above; check the terminal
  running uvicorn for the actual traceback, the browser's error page won't
  show it.

- Company Portal: `http://localhost:5000`
- Super Admin: `http://localhost:5000/superadmin`

## Testing status -- please read before deploying

This was built and syntax/route-verified in a sandbox **without a live
PostgreSQL instance or a GEMINI_API_KEY**, so I could confirm:

- All ~89 routes register correctly (verified via FastAPI's `TestClient`,
  including `<int:id>`-style path parameters, POST/GET method matching, and
  redirect chains).
- Session handling, Jinja template rendering (including the site's
  `request.form.get(...)`-in-templates pattern and `get_flashed_messages()`),
  and form-data parsing all work end-to-end up to the database call.
- Every ported file passes `pyflakes` with zero undefined names (this caught
  two real cross-module bugs during the port, both fixed -- see git-style
  notes in `helpers.py`).

I could **not** verify, and you should before going live:

- Actual DB reads/writes against real data (schema should be identical, but
  please run this against a staging DB first).
- File upload end-to-end (`/upload`, bulk CSV upload, Excel corrections
  upload, company logo upload) -- the `_FileStorageAdapter` in `compat.py`
  wraps Starlette's `UploadFile` to behave like werkzeug's `FileStorage`
  (`.filename`, `.save(path)`), but this needs a real multipart request to
  confirm.
- Gemini extraction quality/accuracy on your real documents -- the prompt in
  `app/ai/gemini_extraction.py` mirrors the field list from your spec, but
  extraction quality tuning (especially for messy scanned statements) will
  need iteration against real samples.
- Load/concurrency behavior of the threadpool-dispatch approach under your
  actual traffic.

## If something breaks

The compat shim is the one piece of genuinely new code in this migration
(everything else is close to a straight port). If a route errors in a way
that looks framework-related rather than business-logic-related, start in
`app/compat.py`.

---

# SaaS Platform (`saas/`) — Multi-tenant POB Campaign Management

The single-company app above is now the foundation of a full SaaS platform.
The new control plane lives in `saas/` and serves **isolated per-company
databases**, a JSON/JWT REST API, and a React (Vite) single-page app.

## Architecture

```
pob_platform  (control plane)                per-company DBs  pob_cmp_0001 …
 └─ companies, subscription_plans,             └─ brands, campaigns, products,
    company_subscriptions, billing,                chemists, pob_activities,
    super_admins, provisioning_jobs,               pob_verifications, gratifications,
    platform_audit_logs, refresh_tokens            notifications, roles, users, …
```

- **Isolation**: every company gets its own PostgreSQL database (`pob_cmp_%04d`),
  created at provisioning time from `saas/tenant_schema.py` (TENANT_DDL). There
  are **no `company_id` columns** — the database itself is the tenant boundary.
- **Routing**: the JWT carries `tenant_db`; every request is routed to that
  database through a per-tenant connection pool (`saas/pools.py`), so tenants
  never share connections. Cross-tenant file access is blocked in
  `saas/routers/storage.py`.
- **Hierarchy & RBAC**: dynamic hierarchy levels (HO→NSM→ZSM→RSM→ASM→MR by
  default, fully configurable) + a permission catalog with 11 seeded roles.
  Permissions are enforced per-route via `require_permission(...)`.
- **Auth**: two scopes — superadmin (control plane) and tenant (company).
  Short-lived JWT access tokens with rotating refresh tokens stored hashed in
  the DB. Passwords hashed via `app/security` (werkzeug).
- **Workflows**:
  - MR submits a POB with an invoice; SHA-256 content-hash + invoice
    no/date/chemist checks flag duplicates/needs-review automatically.
  - Verifier queue: claim → approve (creates the gratification) or reject
    (mandatory reason) or mark duplicate; TAT is tracked.
  - Gratification: physical gift (eligible→dispatched→delivered w/ GPS+photo→
    acknowledged→completed), cashback/UPI (eligible→approved→paid), voucher
    (generated→sent→redeemed). Every transition is event-logged + notified.
  - Dashboards (company/campaign/brand/mr/manager-rollup/verification/
    finance/gift), 11 Excel reports, and scheduled-report records.

## Run locally

Prereqs: Python 3.12+, Node 20+, PostgreSQL 15+.

```bash
python -m venv venv
venv\Scripts\activate            # Windows
pip install -r requirements.txt

# .env must set DB_USER/DB_PASSWORD (legacy vars) plus:
#   SUPERADMIN_BOOTSTRAP_PASSWORD=<choose-a-password>
#   SAAS_JWT_SECRET=<long-random-string>

# frontend
cd web && npm install && npm run build && cd ..

# backend (serves API + built SPA)
uvicorn saas.main:app --port 8000
```

The platform DB is created and the superadmin bootstrapped on first startup.

- Company login: `http://localhost:8000/login`
- Superadmin login: `http://localhost:8000/superadmin-login`
  (username `superadmin`, password = `SUPERADMIN_BOOTSTRAP_PASSWORD`)
- API docs: `http://localhost:8000/docs`

### Seed a demo tenant

```bash
python scripts/seed_demo.py                 # creates DEMO1234
```

Logins: `company_admin` / `Admin@123` · `mr.amit`, `asm.rahul`,
`verifier.kavita`, `finance.sunil` / `Demo@123`.

### Smoke test (end-to-end, runs against a real Postgres)

```bash
python scripts/smoke_test.py
```

Creates a company, provisions its DB, then drives the full flow: hierarchy,
users, masters, POB submission, duplicate detection, verification, cashback /
gift / voucher workflows, dashboards, reports and RBAC checks.

## Docker

```bash
docker compose up --build
```

Postgres + app (uvicorn) + nginx on `:80`. Storage is mounted from `./storage`.
Set `STORAGE_BACKEND=s3` + MinIO/S3 credentials to use object storage instead.

## Key modules

| Module | Purpose |
|---|---|
| `saas/platform_db.py` | Control-plane schema, plans, superadmin bootstrap |
| `saas/provision.py` | Create/drop tenant databases, apply schema + seed |
| `saas/tenant_schema.py` | Per-tenant DDL + defaults (hierarchy, roles, templates) |
| `saas/pools.py` | Per-tenant connection pool registry + idle sweeper |
| `saas/security.py` | JWT access/refresh, rotation, revocation |
| `saas/rbac.py` / `saas/deps.py` | Permission engine + `require_permission` |
| `saas/routers/*` | auth, superadmin, company, masters, pob, verification, gratification, dashboards, reports, notifications, storage |
| `saas/notify.py` | Notification templates + in-app/email/WhatsApp/SMS stubs |
| `web/` | React SPA (Vite), builds to `web/dist` |

