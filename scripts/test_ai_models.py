"""
Verification for the legacy-app "AI Models" superadmin feature:
  - per-category Gemini model routing (DB override > .env default), saved
    without restart, applied on the next document processed
  - editable per-model pricing (DB override > static > conservative estimate),
    add-new-model support, and the "can't remove a model selected in a
    category" guard
Run:  venv\\Scripts\\python.exe scripts\\test_ai_models.py
against a running Postgres (POB_SAAS database).
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from app.main import app  # triggers init_db() on startup
from app.security import hash_pw
from app.ai import model_registry
from app.ai.pricing import compute_cost, get_pricing, GEMINI_PRICING
from app.ai.gemini_extraction import _category_for, _select_model

FAILED = []


def check(name, cond, extra=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" -- {extra}" if extra and not cond else ""))
    if not cond:
        FAILED.append(name)


def main():
    tag = f"aim{int(time.time())}"
    sa_user = f"test_sa_models_{tag}"
    sa_pw = "Models@1234"
    test_model = f"gemini-test-{tag}"

    with TestClient(app) as client:
        # ── unit: category detection ──
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
        from app.database import get_db
        conn = get_db()
        c = conn.cursor()
        c.execute(
            "INSERT INTO super_admins (username,password,full_name,email,role) VALUES (%s,%s,%s,%s,%s)",
            (sa_user, hash_pw(sa_pw), "Test SA Models", "test@models.in", "superadmin"))
        conn.commit()
        conn.close()

        try:
            # login
            r = client.post("/superadmin/login", data={"username": sa_user, "password": sa_pw})
            check("superadmin login", r.status_code in (200, 302, 303, 307),
                  f"{r.status_code} {r.text[:120]}")
            r = client.get("/superadmin/dashboard")
            check("superadmin session works", r.status_code == 200, f"{r.status_code}")

            # ── routing save (override image, reset the rest to default) ──
            r = client.post("/superadmin/api/ai-models/routing", data={
                "routing[image]": "gemini-2.5-flash",
                "routing[pdf_text]": "",
                "routing[pdf_scan]": "",
                "routing[xlsx]": "",
                "routing[docx]": "",
                "routing[text]": "",
            })
            check("routing save (POST ok)", r.status_code in (200, 302, 303, 307),
                  f"{r.status_code} {r.text[:120]}")
            check("image override resolved live",
                  model_registry.resolve_model("image") == "gemini-2.5-flash",
                  model_registry.resolve_model("image"))
            check("pdf_text still follows env",
                  model_registry.resolve_model("pdf_text") == model_registry.category_default("pdf_text"))

            # ── pricing save: edit a known model + add a new one ──
            r = client.post("/superadmin/api/ai-models/pricing", data={
                "label[gemini-2.5-flash]": "Gemini 2.5 Flash",
                "input[gemini-2.5-flash]": "0.33",
                "output[gemini-2.5-flash]": "2.75",
                "new_model_id": test_model,
                "new_model_label": "Test Model",
                "new_model_input": "0.12",
                "new_model_output": "0.45",
            })
            check("pricing save (POST ok)", r.status_code in (200, 302, 303, 307),
                  f"{r.status_code} {r.text[:120]}")
            p = get_pricing("gemini-2.5-flash")
            check("DB price override wins for known model",
                  p["input"] == 0.33 and p["output"] == 2.75, f"{p}")
            pn = get_pricing(test_model)
            check("added model priced", pn["input"] == 0.12 and pn["output"] == 0.45, f"{pn}")
            usd, _inr = compute_cost(test_model, 1_000_000, 100_000)
            check("compute_cost uses DB price",
                  abs(usd - (1.0 * 0.12 + 0.1 * 0.45)) < 1e-6, f"{usd}")

            # ── delete guard: model selected in a category can't be removed ──
            r = client.post("/superadmin/api/ai-models/pricing/delete", data={"model_id": "gemini-2.5-flash"})
            body = r.text.lower()
            check("delete in-use blocked with reason",
                  "selected in" in body or "can't be removed" in body or "is selected" in body,
                  body[:300])
            check("in-use model still priced after blocked delete",
                  get_pricing("gemini-2.5-flash")["input"] == 0.33)

            # ── delete a free (unselected) model succeeds ──
            r = client.post("/superadmin/api/ai-models/pricing/delete", data={"model_id": test_model})
            p = get_pricing(test_model)
            check("free model removable (falls back to estimate)",
                  p["input"] == 1.50 and p["output"] == 9.00, f"{p}")

            # ── GET page renders with settings + pricing ──
            r = client.get("/superadmin/ai-models")
            check("ai-models page 200", r.status_code == 200, f"{r.status_code}")
            check("page shows Model Settings", b"Model Settings" in r.content, "")
            check("page shows Model Pricing", b"Model Pricing" in r.content, "")
            check("page lists scanned-pdf category", b"PDF (scanned / images)" in r.content, "")
            check("page shows .env default hint", b"from .env" in r.content, "")

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
            c.execute("DELETE FROM ai_model_routing WHERE category='image'")
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
