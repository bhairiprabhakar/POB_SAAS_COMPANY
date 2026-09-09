"""
Storage router -- serves uploaded files (invoices, photos, logos, banners)
with tenant isolation enforced on the path: the first path segment is the
tenant database name and must match the caller's own tenant.
"""
import os

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from .. import storage
from ..deps import get_claims

router = APIRouter(prefix="/api/v1/storage", tags=["storage"])


@router.get("/{path:path}")
def serve_file(path: str, claims: dict = Depends(get_claims)):
    parts = path.split("/")
    # Reject traversal/absolute segments outright -- '..' , '.', or an empty
    # segment (which can result from '//' or a leading '/') anywhere in the
    # path is never legitimate for a stored file.
    if any(p in ("", ".", "..") for p in parts):
        raise HTTPException(400, "malformed storage path")
    if len(parts) < 2 or not parts[0]:
        raise HTTPException(400, "malformed storage path")
    tenant_db = parts[0]
    if claims.get("scope") == "tenant" and claims.get("tenant_db") != tenant_db:
        raise HTTPException(403, "cross-tenant access denied")
    if claims.get("scope") not in ("tenant", "superadmin"):
        raise HTTPException(403, "access denied")

    # storage.read() re-validates and canonicalizes against STORAGE_ROOT
    # (defense in depth against traversal, symlinks, etc.)
    data = storage.read(path)
    if data is None:
        raise HTTPException(404, "file not found")
    import mimetypes
    mime, _ = mimetypes.guess_type(os.path.basename(path))
    return Response(content=data, media_type=mime or "application/octet-stream")
