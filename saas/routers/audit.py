"""
Audit logs router (Phase 5). Read-only trail with filters + old/new state.
"""
import json

from fastapi import APIRouter, Depends, HTTPException

from ..db_utils import fetchall_dict
from ..deps import TenantContext, require_permission
from ..pagination import PageLimit, PageOffset

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])


def _loads(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return v
    return v


@router.get("/logs")
def list_audit(action: str = "", user_id: int = None, entity_type: str = "",
               entity_id: int = None, q: str = "", limit: int = PageLimit(), offset: int = PageOffset(),
               ctx: TenantContext = Depends(require_permission("audit.view"))):
    c = ctx.conn.cursor()
    where, params = ["user_id IS NOT NULL"], []
    if action:
        where.append("action=%s")
        params.append(action)
    if user_id:
        where.append("user_id=%s")
        params.append(user_id)
    if entity_type:
        where.append("entity_type=%s")
        params.append(entity_type)
    if entity_id:
        where.append("entity_id=%s")
        params.append(entity_id)
    if q:
        where.append("(actor ILIKE %s OR action ILIKE %s OR detail::text ILIKE %s)")
        params.extend([f"%{q}%"] * 3)
    sql = (f"SELECT * FROM audit_logs WHERE {' AND '.join(where)} "
           f"ORDER BY id DESC LIMIT %s OFFSET %s")
    params.extend([limit, offset])
    c.execute(sql, params)
    items = fetchall_dict(c)
    for it in items:
        for k in ("detail", "before_state", "after_state"):
            if k in it:
                it[k] = _loads(it[k])
    c.execute(f"SELECT count(*) FROM audit_logs WHERE {' AND '.join(where)}", params[:-2])
    return {"items": items, "total": c.fetchone()[0]}
