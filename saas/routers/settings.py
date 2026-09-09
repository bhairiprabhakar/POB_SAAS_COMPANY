"""
Per-tenant configuration (Phase 1).

Each company stores its own settings -- branding (name/logo/theme), locale
(timezone/currency/language), notification provider creds, payout defaults,
security policy and feature toggles -- in the company_settings table as
JSONB. Everything is exposed as configuration, not code changes.
"""
import json

from fastapi import APIRouter, Depends, HTTPException

from ..deps import TenantContext, require_permission
from ..audit import log_action

router = APIRouter(prefix="/api/v1/company", tags=["company settings"])

SETTINGS_DEFAULTS = {
    "branding": {
        "company_name": "",
        "tagline": "Pharma Point-of-Purchase campaign management",
        "logo_path": "",
        "theme_color": "#0f766e",
        "theme_mode": "light",          # light | dark
    },
    "locale": {
        "timezone": "Asia/Kolkata",
        "currency": "INR",
        "currency_symbol": "₹",
        "language": "en",
    },
    "notifications": {
        "provider": "console",           # console | smtp | whatsapp | sms
        "smtp_host": "", "smtp_port": 587, "smtp_user": "", "smtp_password": "",
        "smtp_from": "",
        "whatsapp_api_key": "", "whatsapp_sender": "",
        "sms_api_key": "", "sms_sender": "",
        "webhook_url": "",
        "push_enabled": False,
    },
    "payments": {
        "default_payout_cycle": "instant",  # instant | weekly | monthly
        "payout_weekday": 6,
        "payout_month_day": 1,
        "upi_handle": "",
        "bank_account": "",
        "payment_gateway": "",
        "payment_gateway_key": "",
    },
    "storage": {
        "backend": "local",
        "s3_bucket": "",
    },
    "security": {
        "password_min_length": 6,
        "mfa_required": False,
        "session_timeout_min": 15,
        "rate_limit_per_min": 300,
    },
    "features": {
        "manual_upload": True,
        "ai_verification": False,
        "auto_approve": False,
        "weekly_payouts": True,
        "inventory": False,
        "gift_self_service": False,
    },
}


def _deep_merge(base: dict, update: dict) -> dict:
    for k, v in (update or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = _deep_merge(base.get(k), v)
        else:
            base[k] = v
    return base


def get_settings(conn) -> dict:
    """Load all company settings merged over defaults."""
    c = conn.cursor()
    c.execute("SELECT key, value FROM company_settings")
    stored = dict(c.fetchall())
    merged = json.loads(json.dumps(SETTINGS_DEFAULTS))
    for key, value in stored.items():
        if isinstance(value, dict) and key in merged:
            merged[key] = _deep_merge(merged[key], value)
        elif value is not None:
            merged[key] = value
    return merged


def save_settings(conn, updates: dict, actor_id: int | None) -> None:
    c = conn.cursor()
    for key, value in (updates or {}).items():
        if key not in SETTINGS_DEFAULTS:
            continue
        c.execute(
            """INSERT INTO company_settings (key, value, updated_by, updated_at)
               VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
               ON CONFLICT (key) DO UPDATE
               SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by,
                   updated_at = CURRENT_TIMESTAMP""",
            (key, json.dumps(value), actor_id),
        )
    conn.commit()


def get_setting(conn, section: str, name: str, default=None):
    return get_settings(conn).get(section, {}).get(name, default)


@router.get("/settings")
def company_settings(ctx: TenantContext = Depends(require_permission("settings.manage"))):
    return get_settings(ctx.conn)


# Branding (company name / logo / theme) is owned by the platform super admin
# (companies.logo_path + the superadmin console) and must never be changed from
# a tenant context -- even by an admin holding settings.manage.
BRANDING_LOCKED_MSG = "Branding is managed by the super admin and cannot be changed from the company console"


@router.put("/settings")
def update_settings(body: dict, ctx: TenantContext = Depends(require_permission("settings.manage"))):
    body = body or {}
    if body.get("branding"):
        raise HTTPException(403, BRANDING_LOCKED_MSG)
    save_settings(ctx.conn, body, ctx.user["id"])
    log_action(ctx.conn, ctx.user["id"], "settings.update", "company_settings", None,
               {"sections": sorted(k for k in body.keys())})
    return get_settings(ctx.conn)


# ── Public branding (no auth) so the login page can theme itself ────────────

def public_branding(ctx) -> dict:
    settings = get_settings(ctx.conn)
    return {
        "company_name": settings["branding"].get("company_name"),
        "tagline": settings["branding"].get("tagline"),
        "logo_path": settings["branding"].get("logo_path"),
        "theme_color": settings["branding"].get("theme_color"),
        "theme_mode": settings["branding"].get("theme_mode"),
    }
