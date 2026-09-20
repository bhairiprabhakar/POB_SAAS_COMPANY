"""
Gemini model pricing + cost calculation. Merged home of the legacy
app/ai/pricing.py -- prices, rates, and resolution logic are identical; only
the location changed (saas/ai/ instead of app/ai/).

Prices are Google's published per-1M-token rates (Standard tier) as of the
Gemini 3.5 Flash GA release (May 2026) -- see the model card / pricing page
for current numbers, since these change over time.

WHERE PRICES LIVE -- DB overrides, not just this file: per-model prices can
now be edited from the superadmin "AI Models" page and are stored in
ai_model_pricing (see saas/ai/model_registry.py, platform DB). Resolution
per model:
  ai_model_pricing row  >  STATIC_PRICING below  >  conservative estimate.
A saved price applies to the next document processed with that model, never
retroactively (model_name + cost are captured in ai_usage_log at extraction
time, so historical reports stay accurate).

"model selection" support: cost is computed against whichever model actually
served that request (model_name in ai_usage_log) -- not a single hardcoded
rate -- so per-category routing (also managed on the AI Models page) costs
out correctly per model.
"""
from .model_registry import effective_pricing as _effective_pricing
from .model_registry import STATIC_PRICING as GEMINI_PRICING

# Fallback if a model isn't in the table above (e.g. you changed a model env
# var to something new before adding it) -- better to log a conservative
# estimate than silently record zero cost.
_FALLBACK_PRICING = {"input": 1.50, "output": 9.00}


def get_pricing(model_name: str) -> dict:
    return _effective_pricing(model_name)


def compute_cost(model_name: str, input_tokens: int, output_tokens: int, thinking_tokens: int = 0):
    """
    Returns (cost_usd, cost_inr). Thinking tokens are billed at the output
    rate (Gemini 3.x "thinking" tokens are part of the generated output,
    same as visible completion tokens) -- see Google's token-usage docs.
    """
    pricing = get_pricing(model_name)
    billable_output = (output_tokens or 0) + (thinking_tokens or 0)
    cost_usd = (
        (input_tokens or 0) / 1_000_000 * pricing["input"]
        + billable_output / 1_000_000 * pricing["output"]
    )
    from ..config import USD_TO_INR_RATE
    cost_inr = cost_usd * USD_TO_INR_RATE
    return round(cost_usd, 6), round(cost_inr, 4)