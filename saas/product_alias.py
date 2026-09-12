"""
Product alias matching for the AI-first verification pipeline.

Maps AI-extracted product text to canonical product IDs using exact, fuzzy,
or regex matching against the product_aliases table.
"""
import re
import logging
from difflib import SequenceMatcher

log = logging.getLogger("saas.product_alias")


def normalize_text(text: str) -> str:
    """Lowercase, remove special chars, collapse whitespace."""
    if not text:
        return ""
    t = text.lower().strip()
    t = re.sub(r"[^a-z0-9\s]", "", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def find_matching_product(conn, extracted_text: str, campaign_id: int) -> dict:
    """Match AI-extracted product text against product_aliases for a campaign.

    Returns {"product_id": int, "product_name": str, "confidence": float, "match_type": str}
    or None.
    """
    if not extracted_text or not campaign_id:
        return None

    normalized = normalize_text(extracted_text)
    if not normalized:
        return None

    c = conn.cursor()
    c.execute("""
        SELECT pa.product_id, pa.alias_text, pa.match_type, pa.confidence_weight,
               pr.name AS product_name
        FROM product_aliases pa
        JOIN products pr ON pr.id = pa.product_id
        JOIN campaign_products cp ON cp.product_id = pr.id
        WHERE pa.active = TRUE
          AND cp.campaign_id = %s
          AND pr.status = 'active'
    """, (campaign_id,))
    aliases = c.fetchall()

    best_match = None
    best_confidence = 0.0

    for product_id, alias_text, match_type, weight, product_name in aliases:
        alias_norm = normalize_text(alias_text)
        confidence = 0.0

        if match_type == "exact":
            if normalized == alias_norm:
                confidence = float(weight)
            elif alias_norm in normalized or normalized in alias_norm:
                confidence = float(weight) * 0.9
        elif match_type == "fuzzy":
            ratio = SequenceMatcher(None, normalized, alias_norm).ratio()
            if ratio >= 0.75:
                confidence = float(weight) * ratio
        elif match_type == "regex":
            try:
                if re.search(alias_text, normalized, re.IGNORECASE):
                    confidence = float(weight) * 0.85
            except re.error:
                pass

        if confidence > best_confidence:
            best_confidence = confidence
            best_match = {
                "product_id": product_id,
                "product_name": product_name,
                "confidence": round(confidence, 3),
                "match_type": match_type,
            }

    if best_match and best_match["confidence"] >= 0.5:
        log.info("Product alias matched '%s' → product %s (confidence=%.3f, type=%s)",
                 extracted_text, best_match["product_id"], best_match["confidence"],
                 best_match["match_type"])
        return best_match

    log.info("No product alias match for '%s' in campaign %s", extracted_text, campaign_id)
    return None


def add_product_alias(conn, product_id: int, alias_text: str,
                      match_type: str = "exact", confidence_weight: float = 1.0,
                      created_by: int = None) -> int:
    """Insert a new product alias. Returns the alias ID."""
    c = conn.cursor()
    c.execute("""
        INSERT INTO product_aliases (product_id, alias_text, match_type, confidence_weight, created_by)
        VALUES (%s, %s, %s, %s, %s) RETURNING id
    """, (product_id, normalize_text(alias_text), match_type, confidence_weight, created_by))
    alias_id = c.fetchone()[0]
    conn.commit()
    return alias_id


def remove_product_alias(conn, alias_id: int) -> bool:
    """Soft-delete a product alias by setting active=FALSE."""
    c = conn.cursor()
    c.execute("UPDATE product_aliases SET active=FALSE WHERE id=%s", (alias_id,))
    conn.commit()
    return c.rowcount > 0


def list_product_aliases(conn, campaign_id: int = None) -> list:
    """List all active product aliases, optionally filtered by campaign."""
    c = conn.cursor()
    if campaign_id:
        c.execute("""
            SELECT pa.id, pa.product_id, pa.alias_text, pa.match_type, pa.confidence_weight,
                   pr.name AS product_name, cp.campaign_id
            FROM product_aliases pa
            JOIN products pr ON pr.id = pa.product_id
            JOIN campaign_products cp ON cp.product_id = pr.id
            WHERE pa.active = TRUE AND cp.campaign_id = %s
            ORDER BY pa.product_id, pa.alias_text
        """, (campaign_id,))
    else:
        c.execute("""
            SELECT pa.id, pa.product_id, pa.alias_text, pa.match_type, pa.confidence_weight,
                   pr.name AS product_name,
                   (SELECT MAX(cp2.campaign_id) FROM campaign_products cp2 WHERE cp2.product_id=pr.id) AS campaign_id
            FROM product_aliases pa
            JOIN products pr ON pr.id = pa.product_id
            WHERE pa.active = TRUE
            ORDER BY pa.product_id, pa.alias_text
        """)
    from .db_utils import fetchall_dict
    return fetchall_dict(c)
