"""
Runtime model registry -- per-category Gemini model routing + model pricing,
stored in the app database so a superadmin can change either WITHOUT a restart.

Two small tables (see app/database.py init_db()):

  ai_model_routing  (category -> model_id) : a per-category override. A row
      means "use this model for this category"; a MISSING row means "use the
      .env default" for that category (which is also what the "Default"
      choice in the UI saves as -- deleting the override row).
  ai_model_pricing  (model_id -> label/input/output) : a per-model price
      override in USD per 1M tokens. A MISSING row means use the static
      table below (MODEL_CATALOG). Rows can also add models Google just
      shipped that aren't in the static catalog yet.

Resolution order everywhere is: DB override first, then static/.env defaults.
Both tables are tiny PK lookups read fresh on every extraction, so a save
takes effect on the next document processed -- anything mid-extraction keeps
the model/price it started with (model_name is captured in ai_usage_log at
extraction time, so historical cost stays accurate).

The categories mirror the per-document routing in
app/ai/gemini_extraction.py (_category_for / _select_model). env_attr names
the app.config attribute whose value is that category's ".env default".
"""
import re
from .. import config

# ─────────────────────────────────────────────────────────────────────────────
# Category catalog -- order here is the display order in the superadmin UI.
# ─────────────────────────────────────────────────────────────────────────────
CATEGORIES = [
    {"key": "pdf_text", "label": "PDF (real text layer)",
     "desc": "Native PDF export / ERP print with selectable text", "env_attr": "GEMINI_MODEL_TEXT_PDF"},
    {"key": "pdf_scan", "label": "PDF (scanned / images)",
     "desc": "Scanned or photographed pages, no genuine text layer", "env_attr": "GEMINI_MODEL_VISUAL"},
    {"key": "image", "label": "Images (.jpg/.png/.webp)",
     "desc": "Photographed or scanned image files", "env_attr": "GEMINI_MODEL_VISUAL"},
    {"key": "xlsx", "label": "Excel/CSV (.xlsx/.xls/.csv)",
     "desc": "Spreadsheet exports from ERP/Tally", "env_attr": "GEMINI_MODEL_VISUAL"},
    {"key": "docx", "label": "Word (.docx/.doc)",
     "desc": "Word-processor documents", "env_attr": "GEMINI_MODEL_VISUAL"},
    {"key": "text", "label": "Text (.txt)",
     "desc": "Plain-text invoices / stubs", "env_attr": "GEMINI_MODEL_VISUAL"},
]

_CATEGORY_INDEX = {c["key"]: c for c in CATEGORIES}


def category_default(category: str) -> str:
    """The .env value a category falls back to when no override is set."""
    cat = _CATEGORY_INDEX.get(category)
    return getattr(config, cat["env_attr"], "") if cat else ""


def env_defaults() -> dict:
    """{category: current .env model} for the superadmin UI's 'Default' row."""
    return {c["key"]: category_default(c["key"]) for c in CATEGORIES}


# ─────────────────────────────────────────────────────────────────────────────
# Static model catalog + pricing.
#
# Prices are Google's published Standard-tier USD per 1M tokens where known
# (Gemini 3.5 Flash GA, May 2026 -- these change over time and do NOT
# auto-update). input/output = None for models without a verified rate yet:
# they still appear in the UI so an admin can pick them for testing, but cost
# is computed against the conservative estimate (_FALLBACK_PRICING) until a
# real price is saved for them. A saved ai_model_pricing row always wins over
# this table.
# ─────────────────────────────────────────────────────────────────────────────
MODEL_CATALOG = [
    {"model_id": "gemini-2.5-flash",        "label": "Gemini 2.5 Flash",          "input": 0.30,  "output": 2.50},
    {"model_id": "gemini-3.5-flash",        "label": "Gemini 3.5 Flash",          "input": 1.50,  "output": 9.00},
    {"model_id": "gemini-flash-latest",     "label": "Gemini Flash (latest)",     "input": 1.50,  "output": 9.00},
    {"model_id": "gemini-2.5-flash-lite",   "label": "Gemini 2.5 Flash Lite",     "input": None,  "output": None},
    {"model_id": "gemini-3.1-flash-lite",   "label": "Gemini 3.1 Flash Lite",     "input": None,  "output": None},
    {"model_id": "gemini-3-flash-preview",  "label": "Gemini 3 Flash Preview",    "input": None,  "output": None},
    {"model_id": "gemini-2.5-pro",          "label": "Gemini 2.5 Pro",            "input": None,  "output": None},
    {"model_id": "gemini-3.1-pro-preview",  "label": "Gemini 3.1 Pro Preview",    "input": None,  "output": None},
]

# Known rates only -- kept as the pricing.GEMINI_PRICING re-export so existing
# imports (superadmin export, costing page) keep working unchanged.
STATIC_PRICING = {
    m["model_id"]: {"input": m["input"], "output": m["output"]}
    for m in MODEL_CATALOG if m["input"] is not None
}

# Fallback if a model isn't priced anywhere (static or DB) -- better a
# conservative estimate than silently recording zero cost.
_FALLBACK_PRICING = {"input": 1.50, "output": 9.00}


# ─────────────────────────────────────────────────────────────────────────────
# DB access -- all best-effort: any failure falls back to defaults so a
# broken/absent table can never take down extraction.
# ─────────────────────────────────────────────────────────────────────────────
def _conn():
    from ..database import get_db
    return get_db()


def get_routing() -> dict:
    """{category: model_id} for every saved override. Empty on DB failure."""
    try:
        conn = _conn()
        c = conn.cursor()
        c.execute("SELECT category, model_id FROM ai_model_routing")
        out = {r[0]: r[1] for r in c.fetchall()}
        conn.close()
        return out
    except Exception:
        return {}


def save_routing(overrides: dict, updated_by=None):
    """Persist per-category overrides. category->'' (or missing) deletes the
    override so that category falls back to its .env default."""
    conn = _conn()
    c = conn.cursor()
    try:
        for category in overrides:
            model_id = (overrides.get(category) or "").strip()
            if not model_id:
                c.execute("DELETE FROM ai_model_routing WHERE category=%s", (category,))
            else:
                c.execute(
                    """INSERT INTO ai_model_routing (category, model_id, updated_by, updated_at)
                       VALUES (%s,%s,%s, CURRENT_TIMESTAMP)
                       ON CONFLICT (category) DO UPDATE
                       SET model_id=EXCLUDED.model_id, updated_by=EXCLUDED.updated_by,
                           updated_at=CURRENT_TIMESTAMP""",
                    (category, model_id, updated_by))
        conn.commit()
    finally:
        conn.close()


def resolve_model(category: str) -> str:
    """Model for a category: DB override, else .env default, else the visual
    default that historically handled 'everything else'."""
    override = get_routing().get(category)
    return override or category_default(category) or "gemini-3.5-flash"


def get_pricing_rows() -> dict:
    """{model_id: {label, input, output}} from the pricing table. Empty on
    DB failure. Blank input/output stay None (fallback pricing applies)."""
    try:
        conn = _conn()
        c = conn.cursor()
        c.execute("SELECT model_id, COALESCE(label,''), input_usd, output_usd FROM ai_model_pricing")
        rows = {}
        for mid, label, inp, out in c.fetchall():
            rows[mid] = {"label": label,
                         "input": float(inp) if inp is not None else None,
                         "output": float(out) if out is not None else None}
        conn.close()
        return rows
    except Exception:
        return {}


def effective_pricing(model_id: str) -> dict:
    """{input, output} USD per 1M tokens for a model: DB override > static > fallback."""
    db = get_pricing_rows().get(model_id)
    if db and db.get("input") is not None and db.get("output") is not None:
        return {"input": db["input"], "output": db["output"]}
    static = STATIC_PRICING.get(model_id)
    if static:
        return static
    return _FALLBACK_PRICING


def model_options() -> list:
    """Merged catalog for the UI: every static model (with any DB price/label
    overlaid) plus any DB-only models an admin has added. Each entry is
    {model_id, label, input, output, source} where source is 'default'
    (shipped) or 'custom' (added in the UI). input/output may be None."""
    db = get_pricing_rows()
    out, seen = [], set()
    for m in MODEL_CATALOG:
        mid = m["model_id"]
        seen.add(mid)
        d = db.get(mid)
        if d:
            out.append({"model_id": mid, "label": d.get("label") or m["label"],
                        "input": d.get("input") if d.get("input") is not None else m["input"],
                        "output": d.get("output") if d.get("output") is not None else m["output"],
                        "source": "custom" if d.get("label") or d.get("input") is not None else "default"})
        else:
            out.append({"model_id": mid, "label": m["label"], "input": m["input"],
                        "output": m["output"], "source": "default"})
    for mid, d in db.items():
        if mid not in seen:
            out.append({"model_id": mid, "label": d.get("label") or mid,
                        "input": d.get("input"), "output": d.get("output"), "source": "custom"})
    return out


def upsert_pricing(model_id: str, label: str = "", input_usd=None, output_usd=None):
    """Save/replace one model's price override. Blank price -> NULL (falls
    back to static/estimate). Adds models Google just shipped."""
    model_id = model_id.strip()
    if not model_id:
        raise ValueError("model_id is required")

    def _p(v):
        if v is None or v == "":
            return None
        f = float(v)
        if f < 0:
            raise ValueError("prices must be >= 0")
        return f

    inp, out = _p(input_usd), _p(output_usd)
    conn = _conn()
    c = conn.cursor()
    try:
        c.execute(
            """INSERT INTO ai_model_pricing (model_id, label, input_usd, output_usd, updated_at)
               VALUES (%s,%s,%s,%s, CURRENT_TIMESTAMP)
               ON CONFLICT (model_id) DO UPDATE
               SET label=EXCLUDED.label, input_usd=EXCLUDED.input_usd,
                   output_usd=EXCLUDED.output_usd, updated_at=CURRENT_TIMESTAMP""",
            (model_id, (label or "").strip(), inp, out))
        conn.commit()
    finally:
        conn.close()


def delete_pricing(model_id: str):
    """Remove a model's price override. Returns (ok, reason). Refuses to
    remove a model that is currently selected in any routing category."""
    model_id = model_id.strip()
    if not model_id:
        return False, "No model given"
    in_use = [c for c, m in get_routing().items() if m == model_id]
    if in_use:
        cats = ", ".join(_CATEGORY_INDEX[c]["label"] for c in in_use)
        return False, f"'{model_id}' is selected in: {cats}. Change that category to a different model first."
    try:
        conn = _conn()
        c = conn.cursor()
        c.execute("DELETE FROM ai_model_pricing WHERE model_id=%s", (model_id,))
        conn.commit()
        conn.close()
        return True, "ok"
    except Exception as exc:
        return False, f"Could not remove: {exc}"


_MODEL_ID_RE = re.compile(r"\[([^\]]+)\]")


def parse_form_rows(form_dict: dict) -> list:
    """Parse a submitted pricing form into [(model_id, label, input, output)].
    The template emits `input[<model_id>]`, `output[<model_id>]`,
    `label[<model_id>]` fields; any of those marks the model as present."""
    rows, ids = [], set()
    for key in form_dict:
        m = _MODEL_ID_RE.search(key or "")
        if m and (key.startswith("input[") or key.startswith("output[") or key.startswith("label[")):
            ids.add(m.group(1))
    for mid in sorted(ids):
        if not mid.strip():
            continue
        rows.append((mid,
                     (form_dict.get(f"label[{mid}]") or "").strip(),
                     form_dict.get(f"input[{mid}]") or "",
                     form_dict.get(f"output[{mid}]") or ""))
    return rows
