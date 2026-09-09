import hashlib
import re
from werkzeug.security import generate_password_hash, check_password_hash


def hash_pw(pw):
    """Hash a new/changed password. Salted hash (werkzeug default,
    scrypt/pbkdf2 depending on version) instead of plain unsalted SHA-256."""
    return generate_password_hash(pw)


def _looks_like_legacy_sha256(stored_hash):
    return bool(stored_hash) and len(stored_hash) == 64 and re.fullmatch(r"[0-9a-f]{64}", stored_hash) is not None


def verify_pw(stored_hash, plain_pw):
    """Verify a password against a stored hash. Supports both the new salted
    hashes and legacy unsalted SHA-256 hashes still present in older rows."""
    if not stored_hash:
        return False
    if _looks_like_legacy_sha256(stored_hash):
        return hashlib.sha256(plain_pw.encode()).hexdigest() == stored_hash
    try:
        return check_password_hash(stored_hash, plain_pw)
    except Exception:
        return False
