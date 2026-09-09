"""
File storage abstraction: invoices, gift photos, logos, banners.

Backends:
  - "local" (default): writes under STORAGE_ROOT/<tenant_db>/<module>/<file>
  - "s3": MinIO / AWS S3 via boto3 (installed separately; stubbed otherwise)

The rest of the platform only ever calls save()/read()/url(), so swapping the
backend is a config change, not a code change.
"""
import os
import re
import secrets
from pathlib import Path

from . import config

# tenant_db and module are internal identifiers, never raw user input beyond
# this shape -- but we validate them anyway since defense-in-depth is cheap.
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


class StorageError(Exception):
    pass


class InvalidStoragePath(StorageError):
    """Raised when a caller-supplied relative path escapes STORAGE_ROOT."""


def _safe_filename(filename: str) -> str:
    base = os.path.basename(str(filename or "").replace("\\", "/"))
    if not base:
        base = "file"
    stem, _, ext = base.rpartition(".")
    if not ext:
        return base
    return f"{stem}-{secrets.token_hex(4)}.{ext[:16]}"


def _resolve_within_root(rel_path: str) -> Path:
    """Canonicalize rel_path under STORAGE_ROOT and refuse to return anything
    outside it. This is the path-traversal guard: it defends against '../'
    segments, absolute paths, and symlink escapes regardless of how the
    caller-supplied path was constructed upstream."""
    root = Path(config.STORAGE_ROOT).resolve()
    # Reject null bytes and absolute-looking segments outright.
    if "\x00" in rel_path:
        raise InvalidStoragePath("invalid storage path")
    candidate = (root / rel_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise InvalidStoragePath("invalid storage path")
    return candidate


def save(data: bytes, tenant_db: str, module: str, filename: str) -> str:
    """Persist bytes, return a stored relative path like
    'cmp_0001/invoices/abc.pdf' (module + safe filename under tenant dir)."""
    if not _SAFE_SEGMENT_RE.match(tenant_db or "") or not _SAFE_SEGMENT_RE.match(module or ""):
        raise InvalidStoragePath("invalid tenant_db/module")
    safe = _safe_filename(filename)
    rel = f"{tenant_db}/{module}/{safe}"
    if config.STORAGE_BACKEND == "s3":
        return _s3_save(data, rel)
    path = _resolve_within_root(rel)
    os.makedirs(path.parent, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return rel


def read(rel_path: str) -> bytes | None:
    if config.STORAGE_BACKEND == "s3":
        return _s3_read(rel_path)
    try:
        path = _resolve_within_root(rel_path)
    except InvalidStoragePath:
        return None
    if not path.is_file():
        return None
    with open(path, "rb") as f:
        return f.read()


def delete(rel_path: str) -> bool:
    """Remove a stored file. Returns False when nothing was stored for rel_path."""
    if config.STORAGE_BACKEND == "s3":
        try:
            _s3_client().delete_object(Bucket=config.S3_BUCKET, Key=rel_path)
            return True
        except Exception:
            return False
    try:
        path = _resolve_within_root(rel_path)
    except InvalidStoragePath:
        return False
    if not path.is_file():
        return False
    os.remove(path)
    return True


def abspath(rel_path: str) -> str | None:
    if config.STORAGE_BACKEND == "s3":
        return None
    try:
        path = _resolve_within_root(rel_path)
    except InvalidStoragePath:
        return None
    return str(path) if path.is_file() else None


def public_url(rel_path: str) -> str:
    return f"/api/v1/storage/{rel_path}"


def _s3_client():
    try:
        import boto3  # optional dependency
    except ImportError:
        raise StorageError("STORAGE_BACKEND=s3 requires 'boto3' to be installed")
    return boto3.client(
        "s3",
        endpoint_url=config.S3_ENDPOINT,
        aws_access_key_id=config.S3_ACCESS_KEY,
        aws_secret_access_key=config.S3_SECRET_KEY,
    )


def _s3_save(data: bytes, rel: str) -> str:
    _s3_client().put_object(Bucket=config.S3_BUCKET, Key=rel, Body=data)
    return rel


def _s3_read(rel: str) -> bytes | None:
    try:
        obj = _s3_client().get_object(Bucket=config.S3_BUCKET, Key=rel)
        return obj["Body"].read()
    except Exception:
        return None
