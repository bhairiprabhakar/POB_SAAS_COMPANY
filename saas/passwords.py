"""
Password hashing for the merged platform (was app/security.py).

Single home for password hashing now that the two apps are being unified.
Imported by the SaaS platform (auth, recovery, provisioning, user admin)
and -- via the legacy shim at app/security.py -- by the legacy single-company
app, so hashes stay compatible across both.

Two hash formats are supported on purpose:
  * new passwords  -> werkzeug salted hash (generate_password_hash)
  * pre-existing   -> legacy unsalted SHA-256 rows from the original app
All tenant/provisioned users use the salted format; the SHA-256 fallback is
what lets pre-SaaS-era user rows keep signing in after the merge.
"""
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