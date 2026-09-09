"""
Dynamic workflow definitions (Phase 2).

Admins build reusable approval chains as named step sequences, e.g.:
  MR -> ASM -> ZSM -> HO
or
  FLM -> SLM -> Sales Manager -> Finance -> Payment
Each step references a role by name; campaigns opt into a workflow via
approval_workflow_id. POBs in that campaign then move step-by-step, and the
final step triggers gratification creation via the rule engine.
"""
import json

from fastapi import APIRouter, Depends, HTTPException

from ..deps import TenantContext, require_permission
from ..audit import log_action

router = APIRouter(prefix="/api/v1/workflows", tags=["workflows"])


def _steps(body: dict) -> list[dict]:
    steps = body.get("steps") or []
    if not isinstance(steps, list) or not steps:
        raise HTTPException(400, "steps must be a non-empty list")
    for i, s in enumerate(steps, start=1):
        role = (s.get("role") or "").strip()
        if not role:
            raise HTTPException(400, f"step {i}: role is required")
    return [
        {
            "order": i,
            "action": (s.get("action") or "approve").strip(),
            "role": (s.get("role") or "").strip(),
            "notify": bool(s.get("notify", True)),
            "label": (s.get("label") or "").strip() or f"Approval {i}",
        }
        for i, s in enumerate(steps, start=1)
    ]


@router.get("/")
def list_workflows(ctx: TenantContext = Depends(require_permission("settings.manage"))):
    c = ctx.conn.cursor()
    c.execute("SELECT * FROM workflow_definitions ORDER BY id")
    rows = c.fetchall()
    cols = [d[0] for d in c.description]
    out = []
    for r in rows:
        d = dict(zip(cols, r))
        d["steps"] = json.loads(d.get("steps")) if isinstance(d.get("steps"), str) else (d.get("steps") or [])
        out.append(d)
    return {"items": out}


@router.post("/")
def create_workflow(body: dict, ctx: TenantContext = Depends(require_permission("settings.manage"))):
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    steps = _steps(body)
    c = ctx.conn.cursor()
    c.execute(
        "INSERT INTO workflow_definitions (name, description, steps, active, created_by) "
        "VALUES (%s,%s,%s,%s,%s) RETURNING id",
        (name, body.get("description"), json.dumps(steps), body.get("active", True), ctx.user["id"]),
    )
    wid = c.fetchone()[0]
    ctx.conn.commit()
    log_action(ctx.conn, ctx.user["id"], "workflow.create", "workflow_definitions", wid, {"name": name})
    return {"id": wid, "name": name}


@router.put("/{wid}")
def update_workflow(wid: int, body: dict, ctx: TenantContext = Depends(require_permission("settings.manage"))):
    steps = _steps(body)
    c = ctx.conn.cursor()
    c.execute(
        "UPDATE workflow_definitions SET name=%s, description=%s, steps=%s, active=%s WHERE id=%s",
        ((body.get("name") or "").strip(), body.get("description"),
         json.dumps(steps), body.get("active", True), wid),
    )
    if c.rowcount == 0:
        raise HTTPException(404, "Workflow not found")
    ctx.conn.commit()
    log_action(ctx.conn, ctx.user["id"], "workflow.update", "workflow_definitions", wid)
    return {"ok": True}


@router.delete("/{wid}")
def delete_workflow(wid: int, ctx: TenantContext = Depends(require_permission("settings.manage"))):
    c = ctx.conn.cursor()
    c.execute("DELETE FROM workflow_definitions WHERE id=%s", (wid,))
    if c.rowcount == 0:
        raise HTTPException(404, "Workflow not found")
    ctx.conn.commit()
    log_action(ctx.conn, ctx.user["id"], "workflow.delete", "workflow_definitions", wid)
    return {"ok": True}
