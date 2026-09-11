"""UPI QR payload decoding / validation helpers (End User module).

A UPI QR code is a plain-text string such as::

    upi://pay?pa=merchant@bank&pn=Merchant%20Name&mc=1234&tr=txnref&tn=note&am=100.00&cu=INR

The only mandatory field is ``pa`` (the VPA / payment address). Everything else
is optional metadata. We parse the payload directly (no OCR), validate the VPA,
and mask payment addresses before they reach the UI.
"""
import re
from urllib.parse import parse_qsl, unquote, urlsplit

_VPA_RE = re.compile(r"^[a-zA-Z0-9.\-_]{2,}@[a-zA-Z0-9]{1,}$")


def normalize_upi_id(raw: str) -> str:
    """Trim / strip prefix junk and percent-decoding from a raw UPI identifier."""
    if not raw:
        return ""
    value = raw.strip()
    if value.startswith("upi://"):
        value = unquote(value)
    return value


def extract_upi_id(raw: str) -> str:
    """Pull the VPA (``pa`` parameter) out of a raw payload.

    Accepts either a full ``upi://pay?pa=...`` URI or a bare ``vpa@bank``
    string. Returns an empty string when no usable VPA is present.
    """
    if not raw:
        return ""
    raw = raw.strip()
    if raw.startswith("upi://"):
        query = urlsplit(raw).query
        for key, value in parse_qsl(query, keep_blank_values=True):
            if key.lower() == "pa" and value.strip():
                return value.strip()
        return ""
    value = unquote(raw)
    fields = [f for f in re.split(r"[;\n\t ]+", value) if f]
    for field in fields:
        if "@" in field and "://" not in field:
            return field
    return value


def parse_upi_payload(raw: str) -> dict:
    """Parse a UPI QR payload into its component fields.

    Returns a dict with ``upi_id``, ``payee_name``, ``merchant_code``,
    ``transaction_ref``, ``txn_note``, ``amount``, ``currency`` and
    ``qr_type``. Missing/unknown fields are None; ``upi_id`` is never None
    (may be empty string when the payload carries no usable VPA).
    """
    raw = (raw or "").strip()
    if not raw:
        return _blank_details()
    if not raw.startswith("upi://"):
        return {"upi_id": extract_upi_id(raw), "payee_name": None,
                "merchant_code": None, "transaction_ref": None,
                "txn_note": None, "amount": None, "currency": None,
                "qr_type": "manual"}
    scheme, _, rest = raw.partition("://")
    netloc, _, path = rest.partition("?")
    values = dict(parse_qsl(path, keep_blank_values=True))
    qr_type = path.split("/")[0] if path else "pay"
    return {
        "upi_id": (values.get("pa") or "").strip(),
        "payee_name": (values.get("pn") or "").strip() or None,
        "merchant_code": (values.get("mc") or "").strip() or None,
        "transaction_ref": (values.get("tr") or "").strip() or None,
        "txn_note": (values.get("tn") or "").strip() or None,
        "amount": (values.get("am") or "").strip() or None,
        "currency": (values.get("cu") or "").strip() or None,
        "qr_type": qr_type,
    }


def _blank_details():
    return {"upi_id": "", "payee_name": None, "merchant_code": None,
            "transaction_ref": None, "txn_note": None, "amount": None,
            "currency": None, "qr_type": "manual"}


def is_valid_vpa(value: str) -> bool:
    if not value:
        return False
    candidate = (value or "").strip().lower()
    candidate = unquote(candidate)
    if len(candidate) > 100:
        return False
    if "@" not in candidate:
        return False
    localpart, _, domain = candidate.partition("@")
    if not localpart or not domain or " " in localpart or " " in domain:
        return False
    return bool(_VPA_RE.match(candidate)) and "." not in domain


def mask_upi_id(value: str) -> str:
    """Obscure a VPA so payment-sensitive info never renders in full.

    ``alice@paytm`` -> ``al****@paytm``; bare strings are masked by length.
    """
    if not value:
        return ""
    if "@" in value:
        local, _, domain = value.partition("@")
        head = local[:2]
        tail = "@" + domain
        if len(local) <= 2:
            return local[0] + "*" + tail
        return f"{head}{'*' * min(len(local) - 2, 4)}{tail}"
    if len(value) <= 4:
        return value[0] + "*" * (len(value) - 1)
    return value[:2] + "*" * 4


def name_similarity(a: str, b: str) -> float:
    """0..1 fuzzy score between two names (used for payee-name confirmation)."""
    import difflib
    a = re.sub(r"[^a-zA-Z0-9 ]", "", (a or "").lower()).strip()
    b = re.sub(r"[^a-zA-Z0-9 ]", "", (b or "").lower()).strip()
    if not a or not b:
        return 0.0
    score = difflib.SequenceMatcher(None, a, b).ratio()
    toks_a = set(a.split())
    toks_b = set(b.split())
    if toks_a and toks_b:
        keyword = 2.0 * len(toks_a & toks_b) / (len(toks_a) + len(toks_b))
        score = 0.6 * score + 0.4 * keyword
    return round(min(score, 1.0), 3)