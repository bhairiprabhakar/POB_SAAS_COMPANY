"""
SaaS platform configuration -- multi-tenant control plane + tenant provisioning.

Separate from app/config.py (the legacy single-company app). Both read the
same .env; this module adds the platform-specific variables.
"""
import os

from dotenv import load_dotenv

load_dotenv()

# ── Postgres (shared host/user/password with the legacy app) ────────────────
DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_USER = os.environ.get("DB_USER", "postgres")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")

# Control-plane database (the company/project database). Every division gets
# its own database created at provisioning time, named
# <TENANT_DB_PREFIX><division-name> (see provision.py).
PLATFORM_DB_NAME = os.environ.get("PLATFORM_DB_NAME", "POB_SAAS_COMPANY")
TENANT_DB_PREFIX = os.environ.get("TENANT_DB_PREFIX", "POB_SAAS_COMPANY_")

PLATFORM_POOL_MIN = int(os.environ.get("PLATFORM_POOL_MIN", "2"))
PLATFORM_POOL_MAX = int(os.environ.get("PLATFORM_POOL_MAX", "40"))

# Tenant connection pools are created lazily and cached. Idle-pool cap keeps
# us from holding one pool per company forever; pools beyond this are closed
# after TTL minutes of inactivity (checked on a background thread).
TENANT_POOL_MAX = int(os.environ.get("TENANT_POOL_MAX", "30"))
TENANT_POOL_IDLE_TTL_MIN = int(os.environ.get("TENANT_POOL_IDLE_TTL_MIN", "20"))

# ── JWT ─────────────────────────────────────────────────────────────────────
# A missing/placeholder secret must not silently start the app -- that was
# previously `... or os.environ.get("SECRET_KEY", "change-me")`, meaning a
# deployment that forgot to set either variable would boot with a
# publicly-known JWT signing secret and every token it issues would be
# forgeable. ALLOW_INSECURE_DEV_DEFAULTS is an explicit, deliberate opt-out
# for local development only -- never set it in production.
_ALLOW_INSECURE_DEFAULTS = os.environ.get("ALLOW_INSECURE_DEV_DEFAULTS", "false").lower() == "true"

JWT_SECRET = os.environ.get("SAAS_JWT_SECRET") or os.environ.get("SECRET_KEY")
if not JWT_SECRET:
    if _ALLOW_INSECURE_DEFAULTS:
        JWT_SECRET = "insecure-dev-only-secret-do-not-use-in-production"
    else:
        raise RuntimeError(
            "SAAS_JWT_SECRET (or SECRET_KEY) must be set before startup -- "
            "generate one with `python -c \"import secrets; "
            "print(secrets.token_urlsafe(48))\"` and put it in your .env. "
            "For local development only, set ALLOW_INSECURE_DEV_DEFAULTS=true "
            "to bypass this check."
        )
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_TTL = int(os.environ.get("ACCESS_TOKEN_TTL", "900"))       # 15 min
REFRESH_TOKEN_TTL = int(os.environ.get("REFRESH_TOKEN_TTL", "604800"))  # 7 days
# Short-lived token issued between password verification and TOTP
# verification. Deliberately much shorter than ACCESS_TOKEN_TTL since it
# grants no API access on its own (see saas/deps.py) -- it only exists to be
# exchanged at POST /mfa/verify.
MFA_PENDING_TTL = int(os.environ.get("MFA_PENDING_TTL", "300"))         # 5 min

# ── File storage ────────────────────────────────────────────────────────────
# backend: "local" (default) or "s3". Local stores under STORAGE_ROOT,
# organised storage/<tenant_db>/<module>/<filename>.
STORAGE_BACKEND = os.environ.get("STORAGE_BACKEND", "local")
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORAGE_ROOT = os.environ.get("STORAGE_ROOT", os.path.join(BASE_DIR, "storage"))
os.makedirs(STORAGE_ROOT, exist_ok=True)

S3_ENDPOINT = os.environ.get("S3_ENDPOINT", "http://localhost:9000")
S3_ACCESS_KEY = os.environ.get("S3_ACCESS_KEY", "")
S3_SECRET_KEY = os.environ.get("S3_SECRET_KEY", "")
S3_BUCKET = os.environ.get("S3_BUCKET", "pob-invoices")

# ── Notification providers (stubs by default) ───────────────────────────────
# provider: "console" logs the message to the server log. Swap in real
# credentials to enable email/whatsapp/sms dispatch (see saas/notify.py).
NOTIFY_PROVIDER = os.environ.get("NOTIFY_PROVIDER", "console")
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "no-reply@pobplatform.local")
WHATSAPP_API_KEY = os.environ.get("WHATSAPP_API_KEY", "")
WHATSAPP_SENDER = os.environ.get("WHATSAPP_SENDER", "")
WHATSAPP_API_URL = os.environ.get("WHATSAPP_API_URL", "")
SMS_API_KEY = os.environ.get("SMS_API_KEY", "")
SMS_SENDER = os.environ.get("SMS_SENDER", "")
SMS_API_URL = os.environ.get("SMS_API_URL", "")

# ── Platform (super admin) notification fan-out ─────────────────────────────
# When disabled, cross-division events (campaign submitted, POB pending,
# gratification eligible) do not create platform_notifications rows.
PLATFORM_NOTIFY_ENABLED = os.environ.get("PLATFORM_NOTIFY_ENABLED", "true").lower() not in ("false", "0", "no")

# ── OCR / AI invoice verification ───────────────────────────────────────────
# provider: "gemini" requires a Google API key. Accept both GOOGLE_API_KEY
# (legacy name) and GEMINI_API_KEY. Without a key, binary images extract
# nothing (low confidence -> manual review) and text-mode stubs (.txt/.csv/.log)
# still parse for E2E/dry-run verification.
OCR_PROVIDER = os.environ.get("OCR_PROVIDER", "gemini")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "") or os.environ.get("GEMINI_API_KEY", "")
# Gemini model for invoice extraction. Defaults to a current-generation
# model; override with GEMINI_MODEL (or the legacy GEMINI_MODEL_VISUAL).
GEMINI_MODEL = (os.environ.get("GEMINI_MODEL")
                or os.environ.get("GEMINI_MODEL_VISUAL") or "gemini-2.5-flash")

# OCR cost attribution (token-based). Each Gemini call reports its prompt /
# candidate token counts; cost = tokens/1M * rate, in OCR_COST_CURRENCY.
# Defaults are the Gemini 1.5 Flash list prices (USD per 1M tokens). To bill
# in another currency, set the rates and currency together, e.g. INR-per-1M
# rates with OCR_COST_CURRENCY=INR. Text-mode parsing never charges.
OCR_COST_INPUT_PER_MTOK = float(os.environ.get("OCR_COST_INPUT_PER_MTOK", "0.075"))
OCR_COST_OUTPUT_PER_MTOK = float(os.environ.get("OCR_COST_OUTPUT_PER_MTOK", "0.30"))
OCR_COST_CURRENCY = os.environ.get("OCR_COST_CURRENCY", "USD")

# ── Misc ────────────────────────────────────────────────────────────────────
MAX_UPLOAD_SIZE = int(os.environ.get("MAX_UPLOAD_SIZE", str(50 * 1024 * 1024)))
# Reject non-HTTPS webhook registration/delivery targets. Defaults to true
# (secure by default) -- previously this was read via
# getattr(config, "REQUIRE_HTTPS_WEBHOOKS", False) with no variable defined
# at all, so production silently allowed plaintext HTTP webhook endpoints.
REQUIRE_HTTPS_WEBHOOKS = os.environ.get("REQUIRE_HTTPS_WEBHOOKS", "true").lower() == "true"
CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]
