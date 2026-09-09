"""
Background jobs (Phase 7) -- read-only visibility into the tenant job queue
plus a manual "tick" endpoint so support staff can drain due jobs on demand.
"""
from fastapi import APIRouter, Depends, HTTPException

from ..audit import log_action
from ..db_utils import fetchall_dict
from ..deps import TenantContext, require_permission
from ..pagination import PageLimit

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


@router.get("")
def list_jobs(status: str = "", limit: int = PageLimit(), ctx: TenantContext = Depends(require_permission("job.view"))):
    c = ctx.conn.cursor()
    where, params = ["1=1"], []
    if status:
        where.append("status=%s")
        params.append(status)
    params.append(limit)
    c.execute(
        f"""SELECT id, job_type, payload, status, attempts, max_attempts, error,
            run_at, started_at, finished_at, created_at
            FROM job_queue WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT %s""",
        params,
    )
    return {"items": fetchall_dict(c)}


@router.post("/tick")
def tick(ctx: TenantContext = Depends(require_permission("job.run"))):
    """Run due jobs for this tenant now (drives webhook retries on demand)."""
    from ..scheduler import process_tenant_jobs
    processed = process_tenant_jobs(ctx.claims["tenant_db"])
    log_action(ctx.conn, ctx.user["id"], "job.tick", "job_queue", None, {"processed": processed})
    return {"ok": True, "processed": processed}
