"""
Notifications router -- in-app feed + template management.
"""
from fastapi import APIRouter, Depends, HTTPException

from ..audit import log_action
from ..db_utils import fetchall_dict, fetchone_dict
from ..deps import TenantContext, require_permission
from ..pagination import PageLimit

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])


@router.get("/")
def my_notifications(unread_only: bool = False, limit: int = PageLimit(default=50),
                     ctx: TenantContext = Depends(require_permission("notification.view"))):
    conn = ctx.conn
    c = conn.cursor()
    sql = "SELECT * FROM notifications WHERE user_id=%s"
    params = [ctx.user["id"]]
    if unread_only:
        sql += " AND is_read=FALSE"
    sql += " ORDER BY id DESC LIMIT %s"
    params.append(limit)
    c.execute(sql, params)
    items = fetchall_dict(c)
    c.execute("SELECT count(*) FROM notifications WHERE user_id=%s AND is_read=FALSE", (ctx.user["id"],))
    return {"items": items, "unread": c.fetchone()[0]}


@router.get("/unread-count")
def unread_count(ctx: TenantContext = Depends(require_permission("notification.view"))):
    conn = ctx.conn
    c = conn.cursor()
    c.execute("SELECT count(*) FROM notifications WHERE user_id=%s AND is_read=FALSE", (ctx.user["id"],))
    return {"unread": c.fetchone()[0]}


@router.post("/{nid}/read")
def mark_read(nid: int, ctx: TenantContext = Depends(require_permission("notification.view"))):
    conn = ctx.conn
    c = conn.cursor()
    c.execute("UPDATE notifications SET is_read=TRUE WHERE id=%s AND user_id=%s", (nid, ctx.user["id"]))
    conn.commit()
    return {"ok": True}


@router.post("/read-all")
def mark_all_read(ctx: TenantContext = Depends(require_permission("notification.view"))):
    conn = ctx.conn
    c = conn.cursor()
    c.execute("UPDATE notifications SET is_read=TRUE WHERE user_id=%s AND is_read=FALSE", (ctx.user["id"],))
    conn.commit()
    return {"ok": True}


@router.get("/templates")
def list_templates(ctx: TenantContext = Depends(require_permission("notification.manage"))):
    conn = ctx.conn
    c = conn.cursor()
    c.execute("SELECT * FROM notification_templates ORDER BY code")
    return {"items": fetchall_dict(c)}


@router.post("/templates")
def create_template(body: dict, ctx: TenantContext = Depends(require_permission("notification.manage"))):
    code = (body.get("code") or "").strip()
    if not code or not body.get("body"):
        raise HTTPException(400, "code and body required")
    conn = ctx.conn
    c = conn.cursor()
    c.execute(
        """INSERT INTO notification_templates (code, channel, subject, body, variables, active)
           VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (code) DO UPDATE SET
           channel=EXCLUDED.channel, subject=EXCLUDED.subject, body=EXCLUDED.body,
           variables=EXCLUDED.variables, active=EXCLUDED.active RETURNING id""",
        (code, body.get("channel") or "inapp", body.get("subject"),
         body["body"], body.get("variables") or [], body.get("active", True)),
    )
    tid = c.fetchone()[0]
    conn.commit()
    log_action(conn, ctx.user["id"], "notification_template.create", "notification_template", tid,
               {"code": code})
    return {"ok": True, "id": tid}


@router.put("/templates/{tid}")
def update_template(tid: int, body: dict, ctx: TenantContext = Depends(require_permission("notification.manage"))):
    conn = ctx.conn
    c = conn.cursor()
    fields = ["subject", "body", "channel", "active", "variables"]
    sets, params = [], []
    for f in fields:
        if f in body and body[f] is not None:
            sets.append(f"{f}=%s")
            params.append(body[f])
    if not sets:
        raise HTTPException(400, "Nothing to update")
    params.append(tid)
    c.execute(f"UPDATE notification_templates SET {', '.join(sets)} WHERE id=%s", params)
    conn.commit()
    log_action(conn, ctx.user["id"], "notification_template.update", "notification_template", tid)
    return {"ok": True}


@router.delete("/templates/{tid}")
def delete_template(tid: int, ctx: TenantContext = Depends(require_permission("notification.manage"))):
    conn = ctx.conn
    c = conn.cursor()
    c.execute("DELETE FROM notification_templates WHERE id=%s", (tid,))
    conn.commit()
    log_action(conn, ctx.user["id"], "notification_template.delete", "notification_template", tid)
    return {"ok": True}


@router.post("/templates/{tid}/test")
def test_template(tid: int, body: dict, ctx: TenantContext = Depends(require_permission("notification.manage"))):
    from ..notify import notify_from_template, render_template
    conn = ctx.conn
    c = conn.cursor()
    c.execute("SELECT * FROM notification_templates WHERE id=%s", (tid,))
    tpl = fetchone_dict(c)
    if not tpl:
        raise HTTPException(404, "template not found")
    vars_ = body.get("variables") or {}
    message = render_template(tpl["body"], vars_)
    nid = notify_from_template(conn, ctx.user["id"], tpl["code"], vars_,
                               "notification_template", tid)
    return {"ok": True, "notification_id": nid, "rendered": message}
