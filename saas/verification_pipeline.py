"""
AI-First Automated Verification Pipeline.

Multi-step state machine that processes an invoice through:
  1. File validation
  2. AI extraction (Gemini)
  3. Data normalization
  4. Product alias mapping
  5. Data completeness check
  6. POB vs invoice comparison
  7. Campaign verification rules
  8. Duplicate detection
  9. Confidence scoring
  10. Auto-decision (approve / manual review / auto-reject)

Each step is recorded in verification_pipeline_steps for full audit trail.
"""
import json
import logging
from datetime import datetime, timedelta

log = logging.getLogger("saas.verification_pipeline")

# Pipeline step names in order
PIPELINE_STEPS = [
    "file_validated",
    "extraction_completed",
    "normalized",
    "mapped",
    "completeness_checked",
    "pob_compared",
    "rules_checked",
    "duplicates_checked",
    "confidence_computed",
    "auto_decision",
]

# Confidence field weights for aggregate scoring
FIELD_WEIGHTS = {
    "invoice_number": 0.20,
    "invoice_amount": 0.20,
    "invoice_date": 0.15,
    "product_name": 0.20,
    "quantity": 0.15,
    "chemist_cross_check": 0.10,
}


class PipelineResult:
    """Holds the outcome of a pipeline run."""

    def __init__(self):
        self.steps = []           # list of (step_name, status, detail)
        self.pipeline_status = "not_started"
        self.decision = None      # "auto_approved" | "manual_review" | "auto_rejected"
        self.confidence = 0.0
        self.normalized_data = {}
        self.mapped_product_id = None
        self.product_mapping_confidence = 0.0
        self.duplicate_check_result = {}
        self.completeness_score = 0.0
        self.matches = []
        self.mismatches = []
        self.rule_failures = []
        self.ai_extraction = {}


def _record_step(result: PipelineResult, step_name: str, status: str, detail: dict = None):
    """Record a pipeline step result."""
    result.steps.append({
        "step_name": step_name,
        "step_status": status,
        "detail": detail or {},
    })


def run_pipeline(conn, verification_id: int, pob_id: int, campaign: dict,
                 product: dict, chemist: dict, extraction: dict,
                 submitted_data: dict, auto_verify: bool = True) -> PipelineResult:
    """Execute the full verification pipeline for a POB.

    Args:
        conn: Database connection
        verification_id: ID of the pob_verifications record
        pob_id: ID of the pob_activities record
        campaign: Campaign dict (from campaigns table)
        product: Product dict (from products table)
        chemist: Chemist dict (from chemists table)
        extraction: AI extraction result from ocr.extract_fields
        submitted_data: Dict with invoice_number, invoice_amount, invoice_date,
                        product_name, quantity, pob_amount, ptr
        auto_verify: Whether auto-verification is enabled for this campaign

    Returns: PipelineResult
    """
    result = PipelineResult()
    result.ai_extraction = extraction

    # ── Step 1: File validation ──────────────────────────────────────────
    try:
        if not extraction or not extraction.get("fields"):
            _record_step(result, "file_validated", "failed",
                         {"error": "No extraction data available"})
            result.pipeline_status = "processing_failed"
            _save_pipeline_steps(conn, verification_id, result)
            return result
        _record_step(result, "file_validated", "passed")
    except Exception as e:
        _record_step(result, "file_validated", "failed", {"error": str(e)})
        result.pipeline_status = "processing_failed"
        _save_pipeline_steps(conn, verification_id, result)
        return result

    # ── Step 2: AI extraction (already done by caller) ───────────────────
    fields = extraction.get("fields") or {}
    _record_step(result, "extraction_completed", "passed",
                 {"fields_extracted": list(fields.keys())})

    # ── Step 3: Data normalization ───────────────────────────────────────
    from .. import ocr as _ocr
    normalized = {
        "invoice_number": _normalise_invoice_no(fields.get("invoice_number")),
        "invoice_amount": _ocr._normalise_amount(fields.get("invoice_amount")),
        "invoice_date": _ocr._normalise_date(fields.get("invoice_date") or ""),
        "buyer": fields.get("buyer") or {},
        "items": fields.get("items") or [],
    }
    result.normalized_data = normalized
    _record_step(result, "normalized", "passed", {"normalized_fields": list(normalized.keys())})

    # ── Step 4: Product alias mapping ────────────────────────────────────
    from ..product_alias import find_matching_product
    product_name_from_invoice = ""
    matched_item = None
    items = normalized.get("items") or []
    sub_product = (submitted_data.get("product_name") or "").strip()
    if items:
        for it in items:
            desc = (it.get("description") or "").strip()
            if sub_product and desc and (sub_product.lower() in desc.lower() or desc.lower() in sub_product.lower()):
                matched_item = it
                product_name_from_invoice = desc
                break
    if not product_name_from_invoice and sub_product:
        product_name_from_invoice = sub_product

    alias_match = find_matching_product(conn, product_name_from_invoice, campaign.get("id"))
    if alias_match:
        result.mapped_product_id = alias_match["product_id"]
        result.product_mapping_confidence = alias_match["confidence"]
        _record_step(result, "mapped", "passed", {
            "product_id": alias_match["product_id"],
            "product_name": alias_match["product_name"],
            "confidence": alias_match["confidence"],
            "match_type": alias_match["match_type"],
        })
    else:
        result.mapped_product_id = product.get("id") if product else None
        result.product_mapping_confidence = 0.5  # Default when no alias match
        _record_step(result, "mapped", "passed", {
            "product_id": result.mapped_product_id,
            "note": "No alias match; using submitted product",
        })

    # ── Step 5: Data completeness check ──────────────────────────────────
    required_fields = ["invoice_number", "invoice_amount", "invoice_date"]
    present = sum(1 for f in required_fields if normalized.get(f))
    completeness = present / len(required_fields) if required_fields else 1.0
    # Also check buyer and items
    if normalized.get("buyer"):
        completeness = min(1.0, completeness + 0.1)
    if normalized.get("items"):
        completeness = min(1.0, completeness + 0.1)
    result.completeness_score = round(completeness, 3)
    _record_step(result, "completeness_checked", "passed" if completeness >= 0.5 else "failed",
                 {"score": completeness, "present_fields": present, "total": len(required_fields)})

    # ── Step 6: POB vs invoice comparison ────────────────────────────────
    from ..ocr import verify_invoice
    auto_ctx = {
        "chemist_name": chemist.get("name") or "",
        "shop_name": chemist.get("shop_name") or "",
        "ptr": submitted_data.get("ptr"),
        "is_duplicate": False,  # Checked in step 8
        "campaign_start": campaign.get("start_date"),
        "campaign_end": campaign.get("end_date"),
        "campaign_grace_days": campaign.get("grace_days"),
        "campaign_grace_months": campaign.get("grace_months"),
        "campaign_pre_grace_days": campaign.get("pre_grace_days"),
        "min_quantity": product.get("min_quantity") if product else None,
        "min_pob": product.get("min_pob") if product else None,
        "max_pob": product.get("max_pob") if product else None,
    }
    verdict = verify_invoice(submitted_data, extraction, 0.0, context=auto_ctx)
    result.matches = verdict.get("matches", [])
    result.mismatches = verdict.get("mismatches", [])
    _record_step(result, "pob_compared", "passed" if not result.mismatches else "failed",
                 {"matches": result.matches, "mismatches": result.mismatches,
                  "raw_confidence": verdict.get("confidence", 0)})

    # ── Step 7: Campaign verification rules ──────────────────────────────
    from ..verification_rules import evaluate_verification_rules
    invoice_data = {
        "invoice_number": normalized.get("invoice_number"),
        "invoice_amount": normalized.get("invoice_amount"),
        "invoice_date": normalized.get("invoice_date"),
        "quantity": submitted_data.get("quantity"),
        "pob_amount": submitted_data.get("pob_amount"),
        "buyer": normalized.get("buyer"),
        "ptr": submitted_data.get("ptr"),
    }
    rule_results = evaluate_verification_rules(conn, submitted_data, invoice_data, campaign.get("id"))
    result.rule_failures = [r for r in rule_results if not r["passed"]]
    _record_step(result, "rules_checked", "passed" if not result.rule_failures else "failed",
                 {"total_rules": len(rule_results),
                  "passed": sum(1 for r in rule_results if r["passed"]),
                  "failures": [{"type": r["rule_type"], "detail": r["detail"],
                                "action": r["action_on_fail"]} for r in result.rule_failures]})

    # ── Step 8: Duplicate detection ──────────────────────────────────────
    dup_result = {"is_duplicate": False}
    # Check invoice number duplicate
    norm_invoice_no = _normalise_invoice_no(submitted_data.get("invoice_number"))
    if norm_invoice_no:
        c = conn.cursor()
        c.execute("""
            SELECT id, status FROM pob_activities
            WHERE invoice_number_norm = %s AND chemist_id = %s AND id != %s
              AND status NOT IN ('rejected')
            LIMIT 1
        """, (norm_invoice_no, chemist.get("id"), pob_id))
        dup = c.fetchone()
        if dup:
            dup_result = {"is_duplicate": True, "duplicate_of": dup[0], "status": dup[1]}
    # Check document hash duplicate
    c = conn.cursor()
    c.execute("SELECT content_hash FROM pob_activities WHERE id=%s", (pob_id,))
    hash_row = c.fetchone()
    if hash_row and hash_row[0]:
        c.execute("""
            SELECT id FROM pob_activities
            WHERE content_hash = %s AND id != %s AND status NOT IN ('rejected')
            LIMIT 1
        """, (hash_row[0], pob_id))
        hash_dup = c.fetchone()
        if hash_dup:
            dup_result = {"is_duplicate": True, "duplicate_of": hash_dup[0],
                          "reason": "identical_document"}
    result.duplicate_check_result = dup_result
    _record_step(result, "duplicates_checked", "passed" if not dup_result.get("is_duplicate") else "failed",
                 dup_result)

    # ── Step 9: Confidence scoring ───────────────────────────────────────
    # Aggregate per-field confidence
    total_weight = 0
    weighted_sum = 0
    for field_name, weight in FIELD_WEIGHTS.items():
        if field_name in result.matches:
            weighted_sum += weight
        elif field_name in result.mismatches:
            weighted_sum += 0
        else:
            # Field not checked — give partial credit
            weighted_sum += weight * 0.5
        total_weight += weight

    base_confidence = weighted_sum / total_weight if total_weight else 0

    # Adjust for product mapping
    base_confidence *= (0.8 + 0.2 * result.product_mapping_confidence)

    # Adjust for completeness
    base_confidence *= (0.7 + 0.3 * result.completeness_score)

    # Penalty for rule failures
    auto_reject_failures = [f for f in result.rule_failures if f["action_on_fail"] == "auto_reject"]
    if auto_reject_failures:
        base_confidence *= 0.3

    # Duplicate → confidence = 0
    if dup_result.get("is_duplicate"):
        base_confidence = 0.0

    result.confidence = round(min(1.0, max(0.0, base_confidence)), 3)
    _record_step(result, "confidence_computed", "passed",
                 {"confidence": result.confidence, "matches_count": len(result.matches),
                  "mismatches_count": len(result.mismatches),
                  "rule_failures_count": len(result.rule_failures)})

    # ── Step 10: Auto-decision ───────────────────────────────────────────
    auto_confidence = float(campaign.get("auto_verify_confidence") or 0.9)
    reject_confidence = float(campaign.get("auto_reject_confidence") or 0.3)

    has_auto_reject_failure = any(f["action_on_fail"] == "auto_reject" for f in result.rule_failures)

    if (result.confidence >= auto_confidence
            and not result.mismatches
            and not has_auto_reject_failure
            and not dup_result.get("is_duplicate")
            and auto_verify):
        result.decision = "auto_approved"
        result.pipeline_status = "completed"
    elif result.confidence < reject_confidence or has_auto_reject_failure:
        result.decision = "auto_rejected"
        result.pipeline_status = "completed"
    else:
        result.decision = "manual_review"
        result.pipeline_status = "pending_agent"

    _record_step(result, "auto_decision", "passed",
                 {"decision": result.decision, "confidence": result.confidence,
                  "threshold_auto": auto_confidence, "threshold_reject": reject_confidence})

    # ── Save all pipeline steps ──────────────────────────────────────────
    _save_pipeline_steps(conn, verification_id, result)

    log.info("Pipeline completed for verification %s: decision=%s, confidence=%.3f",
             verification_id, result.decision, result.confidence)
    return result


def _save_pipeline_steps(conn, verification_id: int, result: PipelineResult):
    """Persist pipeline steps and update verification record."""
    c = conn.cursor()
    for step in result.steps:
        c.execute("""
            INSERT INTO verification_pipeline_steps
                (verification_id, step_name, step_status, detail, started_at, completed_at)
            VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """, (verification_id, step["step_name"], step["step_status"],
              json.dumps(step.get("detail") or {})))
    conn.commit()


def _normalise_invoice_no(value) -> str:
    """Normalise invoice number for comparison."""
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())
