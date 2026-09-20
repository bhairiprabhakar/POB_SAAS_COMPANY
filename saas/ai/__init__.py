"""
Merged AI / document-extraction stack (ported from legacy app/ai/).

Owns statement + invoice extraction via the Gemini File API, per-category
model routing, and per-model cost calculation. Database side lives in the
platform control-plane DB (see saas/platform_db.py), NOT per tenant: model
routing + pricing are platform-wide superadmin configuration, and ai_usage_log
records tenant-annotated (division_id) usage rows.
"""