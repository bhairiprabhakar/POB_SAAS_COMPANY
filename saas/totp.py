"""
TOTP (RFC 6238) implementation -- no external dependency.

Used for multi-factor authentication: a 6-digit code from a TOTP app
(Google Authenticator / Authy / 1Password) generated from a shared base32
secret. The client scans the otpauth:// provisioning URI as a QR code.
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time

PERIOD = 30          # seconds per time step
DIGITS = 6           # number of digits in the code
ALGORITHM = hashlib.sha1
ISSUER = "POB SaaS"


def generate_secret() -> str:
    """Random 20-byte base32 secret (160-bit, same size as TOTP standards)."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _normalize_secret(secret: str) -> bytes:
    secret = "".join(ch for ch in (secret or "").upper() if ch.isalnum())
    pad = "=" * ((8 - len(secret) % 8) % 8)
    return base64.b32decode(secret + pad, casefold=True)


def hotp(secret: str, counter: int) -> str:
    """HMAC-based one-time password for a given counter (RFC 4226)."""
    key = _normalize_secret(secret)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, ALGORITHM).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** DIGITS)
    return str(code).zfill(DIGITS)


def totp(secret: str, at: float | None = None) -> str:
    """Current TOTP code at time `at` (defaults to now)."""
    at = time.time() if at is None else at
    return hotp(secret, int(at) // PERIOD)


def verify(secret: str, code: str, window: int = 1) -> bool:
    """Accept a code from the current step or `window` steps either side."""
    code = (code or "").strip()
    if not code.isdigit() or len(code) != DIGITS:
        return False
    counter = int(time.time()) // PERIOD
    for delta in range(-window, window + 1):
        if hmac.compare_digest(totp(secret, (counter + delta) * PERIOD), code):
            return True
    return False


def provisioning_uri(secret: str, account: str) -> str:
    """otpauth:// URI for the QR code in the authenticator app."""
    account = account.replace(":", " ")
    return (f"otpauth://totp/{ISSUER}:{account}?secret={secret}"
            f"&issuer={ISSUER}&algorithm=SHA1&digits={DIGITS}&period={PERIOD}")
