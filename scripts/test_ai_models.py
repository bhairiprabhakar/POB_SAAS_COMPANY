"""
Verification for the SaaS "AI Models" superadmin feature. Ported from the
legacy-app script of the same name -- the legacy `/superadmin/ai-models` page
and form posts are now the FastAPI endpoints in saas/routers/superadmin.py
(`/api/v1/superadmin/ai-models*`), backed by saas/ai/model_registry.py,
saas/ai/pricing.py and saas/ai/gemini_extraction.py:

  - per-category Gemini model routing (DB override > .env default), saved
    without restart, applied on the next document processed
  - editable per-model pricing (DB override > static > conservative estimate),
    add-new-model support, and the "can't remove a model selected in a
    category" guard

Run:  venv\\Scripts\\python.exe scripts\\test_ai_models.py
against a running Postgres (model routing/pricing live in the control-plane
DB, config.PLATFORM_DB_NAME; a throwaway superadmin is created and removed).
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from saas.main import app  # triggers platform init + pool setup on startup
from saas.passwords import hash_pw
from saas.platform_db import get_db
from saas.ai import model_registry
from saas.ai.pricing import compute_cost, get_pricing, GEMINI_PRICING
from saas.ai.gemini_extraction import _category_for, _select_model

BASE = "/api/v1"
FAILED = []


def check(name, cond, extra=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" -- {extra}" if extra and not cond else ""))
    if not cond:
        FAILED.append(name)


def auth_header(token):
    return {"Authorization": f"Bearer {token}"}


def main():
    tag = f"aim{int(time.time())}"
    sa_user = f"test_sa_models_{tag}"
    sa_pw = "Models@1234"
    test_model = f"gemini-test-{tag}"

    with TestClient(app) as client:
        # ── unit: category detection (unchanged from legacy port) ──
        check("category pdf resolves to pdf_text/pdf_scan",
              _category_for("x.pdf", "application/pdf") in ("pdf_text", "pdf_scan"))
        check("category image", _category_for("x.jpg", "image/jpeg") == "image")
        check("category jpeg2", _category_for("x.png", "image/png") == "image")
        check("category xlsx", _category_for("x.xlsx",
              "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet") == "xlsx")
        check("category xls", _category_for("x.xls", "application/vnd.ms-excel") == "xlsx")
        check("category csv", _category_for("x.csv", "text/csv") == "xlsx")
        check("category docx", _category_for("x.docx",
              "application/vnd.openxmlformats-officedocument.wordprocessingml.document") == "docx")
        check("category doc", _category_for("x.doc", "application/msword") == "docx")
        check("category text", _category_for("x.txt", "text/plain") == "text")
        check("category unknown -> image", _category_for("x.xyz", "application/octet-stream") == "image")

        # ── unit: pricing resolution ──
        base_in = GEMINI_PRICING["gemini-2.5-flash"]["input"]
        base_out = GEMINI_PRICING["gemini-2.5-flash"]["output"]
        p = get_pricing("gemini-2.5-flash")
        check("static pricing applies when no DB row",
              p["input"] == base_in and p["output"] == base_out, f"{p}")
        fp = get_pricing("gemini-totally-unknown-xyz")
        check("unknown model -> conservative estimate",
              fp["input"] == 1.50 and fp["output"] == 9.00, f"{fp}")

        # ── unit: default routing = .env values ──
        check("pdf_text default from env",
              model_registry.resolve_model("pdf_text") == model_registry.category_default("pdf_text"))
        check("image default from env",
              model_registry.resolve_model("image") == model_registry.category_default("image"))

        # ── create a throwaway superadmin ──
        conn = get_db()
        c = conn.cursor()
        c.execute(
            "INSERT INTO super_admins (username,password,full_name,email) VALUES (%s,%s,%s,%s)",
            (sa_user, hash_pw(sa_pw), "Test SA Models", "test@models.in"))
        conn.commit()
        conn.close()

        try:
            # login (SaaS /api/v1/auth/superadmin/login, JWT bearer)
            r = client.post(f"{BASE}/auth/superadmin/login",
                            json={"username": sa_user, "password": sa_pw})
            check("superadmin login", r.status_code == 200,
                  f"{r.status_code} {r.text[:120]}")
            sa_token = r.json().get("access_token")
            SA = auth_header(sa_token)

            # GET: settings + pricing payload (the ported "page render")
            r = client.get(f"{BASE}/superadmin/ai-models", headers=SA)
            check("ai-models GET 200", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
            data = r.json() if r.status_code == 200 else {}
            cats = {c.get("key") for c in data.get("categories") or []}
            check("page lists all six categories",
                  {"pdf_text", "pdf_scan", "image", "xlsx", "docx", "text"} <= cats, f"{cats}")
            check("page returns env defaults", bool(data.get("env_defaults")), f"{data.get('env_defaults')}")
            check("page returns .env default hint",
                  data.get("env_defaults", {}).get("image") == model_registry.category_default("image"))

            # ── routing save: override image + docx, reset the rest ──
            # (docx is overridden to a model that DIFFERS from the .env default
            # so the DB-override-wins path is proven, not just echoed)
            r = client.post(f"{BASE}/superadmin/ai-models/routing", headers=SA, json={
                "overrides": {
                    "image": "gemini-2.5-flash",
                    "docx": "gemini-3.5-flash",
                    "pdf_text": "", "pdf_scan": "", "xlsx": "", "text": "",
                },
            })
            check("routing save (POST ok)", r.status_code == 200,
                  f"{r.status_code} {r.text[:120]}")
            routing = model_registry.get_routing()
            check("image override persisted in DB",
                  routing.get("image") == "gemini-2.5-flash", f"{routing}")
            check("docx override resolved live (differs from env default)",
                  model_registry.resolve_model("docx") == "gemini-3.5-flash",
                  model_registry.resolve_model("docx"))
            check("pdf_text still follows env",
                  model_registry.resolve_model("pdf_text") == model_registry.category_default("pdf_text"))
            check("image override resolved live",
                  model_registry.resolve_model("image") == "gemini-2.5-flash",
                  model_registry.resolve_model("image"))

            # ── pricing save: edit a known model + add a new one ──
            r = client.post(f"{BASE}/superadmin/ai-models/pricing", headers=SA, json={
                "rows": [
                    {"model_id": "gemini-2.5-flash", "label": "Gemini 2.5 Flash",
                     "input": "0.33", "output": "2.75"},
                ],
                "new_model": {
                    "model_id": test_model, "label": "Test Model",
                    "input": "0.12", "output": "0.45",
                },
            })
            check("pricing save (POST ok)", r.status_code == 200,
                  f"{r.status_code} {r.text[:120]}")
            p = get_pricing("gemini-2.5-flash")
            check("DB price override wins for known model",
                  p["input"] == 0.33 and p["output"] == 2.75, f"{p}")
            pn = get_pricing(test_model)
            check("added model priced", pn["input"] == 0.12 and pn["output"] == 0.45, f"{pn}")
            usd, _inr = compute_cost(test_model, 1_000_000, 100_000)
            check("compute_cost uses DB price",
                  abs(usd - (1.0 * 0.12 + 0.1 * 0.45)) < 1e-6, f"{usd}")

            # ── delete guard: selected-in-category can't be removed ──
            r = client.post(f"{BASE}/superadmin/ai-models/pricing/delete", headers=SA,
                            json={"model_id": "gemini-2.5-flash"})
            body = (r.text or "").lower()
            check("delete in-use blocked with reason",
                  r.status_code == 400 and "selected in" in body,
                  f"{r.status_code} {body[:300]}")
            check("in-use model still priced after blocked delete",
                  get_pricing("gemini-2.5-flash")["input"] == 0.33)

            # ── delete a free (unselected) model succeeds ──
            r = client.post(f"{BASE}/superadmin/ai-models/pricing/delete", headers=SA,
                            json={"model_id": test_model})
            check("free model removable", r.status_code == 200,
                  f"{r.status_code} {r.text[:120]}")
            p = get_pricing(test_model)
            check("free model falls back to estimate",
                  p["input"] == 1.50 and p["output"] == 9.00, f"{p}")

            # ── GET again: settings + pricing reflected in the payload ──
            r = client.get(f"{BASE}/superadmin/ai-models", headers=SA)
            data = r.json() if r.status_code == 200 else {}
            pricing = data.get("pricing") or {}
            models = [m.get("model_id") for m in data.get("models") or []]
            check("page still lists every category", len(data.get("categories") or []) == 6)
            check("saved price shown on page",
                  pricing.get("gemini-2.5-flash", {}).get("input") == 0.33, f"{pricing.get('gemini-2.5-flash')}")
            check("added model removed from pricing after delete", test_model not in models)

            # ── _select_model honours override + default ──
            check("_select_model image uses override",
                  _select_model("/tmp/x.jpg", "image/jpeg") == "gemini-2.5-flash",
                  _select_model("/tmp/x.jpg", "image/jpeg"))
            check("_select_model text uses default",
                  _select_model("/tmp/x.txt", "text/plain") == model_registry.category_default("text"),
                  _select_model("/tmp/x.txt", "text/plain"))
        finally:
            # cleanup: restore routing + pricing, drop the throwaway superadmin
            conn = get_db()
            c = conn.cursor()
            c.execute("DELETE FROM ai_model_routing WHERE category IN ('image','docx')")
            c.execute("DELETE FROM ai_model_pricing WHERE model_id IN (%s,%s)",
                      ("gemini-2.5-flash", test_model))
            c.execute("DELETE FROM super_admins WHERE username=%s", (sa_user,))
            conn.commit()
            conn.close()

    if FAILED:
        print(f"\n{len(FAILED)} FAILED: {FAILED}")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    main()