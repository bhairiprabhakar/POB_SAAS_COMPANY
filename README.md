# POB SaaS Platform — unified application

A single multi-tenant application: a FastAPI backend (`saas/`) serving a
React single-page app (`web/`), one Postgres control-plane database plus one
database per tenant (division), Google Gemini for statement/invoice
extraction, and a full pharma sales workflow (POBs, campaign hierarchy,
verification, gratification, credits, dashboards).

The original single-company Flask app (`app.py` + the `index.html` /
`agent.html` / `superadmin.html` Jinja portals) was reimplemented feature-
for-feature in the SaaS platform across Batches 1–3 (see `UNIFICATION.md`)
and its tree (`app/`, `templates/`, `static/`) was **removed** in Batch 4.
Everything the old portals did — statement upload & extraction, credits
billing, the L2 manual verification workflow, AI model config, costing
exports — is now served by the React SPA against `saas/routers/*` endpoints,
inside every tenant database.

## Architecture

```
Browser ──> FastAPI (saas.main:app, :8000)
              ├── /api/v1/*        REST endpoints (saas/routers/*)
              ├── /                built React SPA (web/dist, client-side fallback)
              ├── /healthz         liveness probe
              └── /metrics         Prometheus text metrics
              |
              ├── control-plane DB (PLATFORM_DB_NAME)
              │     super_admins, companies (single row), plans, phases,
              │     platform_audit_logs, refresh_tokens, ai_model_routing,
              │     ai_model_pricing, ai_usage_log, ...
              └── per-tenant DBs (<TENANT_DB_PREFIX><division-name>)
                    users/roles, hierarchy, chemists/doctors, POBs,
                    uploads/extractions/parties/items, manual_verifications,
                    credits wallet + ledger + requests, audit_logs, ...
```

- **Auth**: JWT access tokens (15 min) + rotating refresh tokens (7 days),
  MFA (TOTP) on both super-admin and company accounts, per-IP login
  rate-limiting and lockout. Tenant sessions carry `permissions` baked into
  the token; the nav is permission-gated.
- **Provisioning**: a company is created + provisioned in one call; the
  tenant database is created and migrated (`saas/tenant_schema.py`,
  `SCHEMA_VERSION`-tracked) the same request, so a new company always start
  current.
- **Storage**: POB invoices/photos go to `STORAGE_BACKEND=local`
  (`STORAGE_ROOT`, organised `storage/<tenant_db>/<module>/<filename>`) or
  an S3-compatible store (MinIO).
- **Extraction**: per-document Gemini routing (`saas/ai/gemini_extraction.py`)
  — PDFs with a real text layer use the cheap `GEMINI_MODEL_TEXT_PDF` model,
  everything else uses `GEMINI_MODEL_VISUAL`; overridable per category from
  the superadmin **AI Models** page and priced against `ai_usage_log` for
  exact costing.
- **Statement/credits domain** (the merged legacy portal): uploaded
  statements are deduplicated at L1 (file hash) and L2 (content fingerprint),
  extracted to parties/items with a period overlap check, billed from the
  tenant's statement-credit wallet, and verified through an agent workspace
  (queue/claim/inline edit/reject/re-extract/Excel export).

## Project layout

```
saas/
  main.py                 FastAPI app factory -- run this
  config.py               env vars (shares .env with the platform)
  platform_db.py          control-plane pool + schema + superadmin bootstrap
  tenant_schema.py        per-tenant DDL + patches, SCHEMA_VERSION
  provision.py            company provisioning (tenant DB create/migrate)
  deps.py                 JWT/tenant connection/permission dependencies
  passwords.py            hashing (werkzeug + legacy unsalted SHA-256 check)
  security.py             token issue/validate/rotate
  credits.py              tenant statement-credit wallet/ledger/requests
  statements.py           upload/extraction/dedup/verification domain
  ai/                     model_registry, pricing, gemini_extraction
  extraction.py           Gemini JSON -> internal schema mapping
  ingestion.py            upload ingestion orchestration
  worker_pool.py          OCR/detection worker pool
  routers/                api/v1 route modules (auth, company, statements,
                          superadmin, verification, credits UI payloads, ...)
web/                      React SPA (src/pages/app, src/pages/superadmin)
scripts/                  E2E/verify suite (smoke_test, verify_*, test_*)
deploy/nginx.conf         single entry point (proxy to FastAPI)
```

## Setup

```bash
pip install -r requirements.txt
# copy .env values for your environment (Postgres creds, SAAS_JWT_SECRET,
# GEMINI_API_KEY, SUPERADMIN_BOOTSTRAP_PASSWORD, STORAGE_BACKEND)
```

The control-plane database is created automatically on first startup. The
`superadmin` account is bootstrapped on the FIRST run using
`SUPERADMIN_BOOTSTRAP_PASSWORD`.

### Common first-run errors

- **`psycopg2.OperationalError: connection ... Connection refused`** —
  Postgres isn't running, or `DB_HOST`/`DB_PORT` in `.env` don't point at it.
- **`FATAL: password authentication failed`** — `DB_USER`/`DB_PASSWORD` in
  `.env` don't match your Postgres role.
- **Startup error about a JWT secret** — set `SAAS_JWT_SECRET` (or
  `SECRET_KEY`); for local development only, `ALLOW_INSECURE_DEV_DEFAULTS=true`
  bypasses the placeholder checks. Never true in production.

## Run

Backend (serves the built SPA + API):

```bash
uvicorn saas.main:app --host 0.0.0.0 --port 8000
```

Frontend (production build, or dev server):

```bash
cd web && npm install
npm run build            # -> web/dist, served by FastAPI
# or for development:
npm run dev              # Vite on :5173, /api proxied to :8000
```

- Company (tenant) portal: `http://localhost:8000/app`
- Super Admin console: `http://localhost:8000/superadmin`

Docker: `docker compose up --build` brings up Postgres + the app + nginx
(`http://localhost`), with the SPA built into the image.

## Testing

The full suite runs the whole platform against real Postgres using hermetic
data — every script points the app at a scratch control-plane database and a
freshly provisioned tenant before importing `saas`, and drops the scratch DBs
when it finishes. Nothing in the suite touches live tenant data:

```bash
venv\Scripts\python.exe scripts\smoke_test.py              # full E2E happy path
venv\Scripts\python.exe scripts\verify_batch2.py           # statements/credits/ai-models wiring
venv\Scripts\python.exe scripts\test_ai_models.py          # AI model routing + pricing
venv\Scripts\python.exe scripts\test_platform_roles.py     # role/permission matrix
venv\Scripts\python.exe scripts\test_product_crud.py       # products/campaign-products CRUD
venv\Scripts\python.exe scripts\test_business_alignment.py  # division-company model gate (brand REQUIRED, delete-protection, verifier restrictions, UPI flow)
venv\Scripts\python.exe scripts\test_pages_load.py         # SPA/API surface loads per role
venv\Scripts\python.exe scripts\test_dashboards_period.py  # dashboard/analytics days filter + prev deltas
venv\Scripts\python.exe scripts\test_scoping.py            # hierarchy scoping (analytics/dashboards)
venv\Scripts\python.exe scripts\test_roi_analytics.py      # analytics/roi math
venv\Scripts\python.exe scripts\test_campaign_multibrand.py# division console multi-brand campaigns
```

They print `[PASS]/[FAIL]` lines and exit non-zero if any group failed.
`verify_batch2.py` additionally has a `--read-only-live` mode that probes the
live control-plane DB without writing to it.