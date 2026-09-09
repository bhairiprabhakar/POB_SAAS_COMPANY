"""
Inventory router (Phase 3).

Warehouses, gift stock (with per-warehouse quantities + reservations) and
stock movements (inbound / allocation / dispatch / return / adjustment).
Stock is consumed from a warehouse when a physical gift is dispatched; the
warehouse is chosen in dispatch and recorded on the gratification.
"""
from fastapi import APIRouter, Depends, HTTPException

from ..audit import log_action
from ..db_utils import fetchall_dict, fetchone_dict
from ..deps import TenantContext, require_permission

router = APIRouter(prefix="/api/v1", tags=["inventory"])


# ── Warehouses ──────────────────────────────────────────────────────────────

@router.get("/warehouses")
def list_warehouses(ctx: TenantContext = Depends(require_permission("inventory.view"))):
    c = ctx.conn.cursor()
    c.execute("""SELECT w.*, COALESCE(SUM(gs.quantity),0) AS stock_total,
                        COALESCE(SUM(gs.reserved),0) AS reserved_total
                 FROM warehouses w
                 LEFT JOIN gift_stock gs ON gs.warehouse_id=w.id
                 GROUP BY w.id ORDER BY w.id""")
    return {"items": fetchall_dict(c)}


@router.post("/warehouses")
def create_warehouse(body: dict, ctx: TenantContext = Depends(require_permission("inventory.manage"))):
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "warehouse name required")
    c = ctx.conn.cursor()
    c.execute("INSERT INTO warehouses (name, code, location, contact, active) "
              "VALUES (%s,%s,%s,%s,%s) RETURNING id",
              (name, body.get("code"), body.get("location"), body.get("contact"),
               body.get("active", True)))
    wid = c.fetchone()[0]
    ctx.conn.commit()
    log_action(ctx.conn, ctx.user["id"], "warehouse.create", "warehouse", wid, {"name": name})
    return {"ok": True, "id": wid}


@router.put("/warehouses/{wid}")
def update_warehouse(wid: int, body: dict, ctx: TenantContext = Depends(require_permission("inventory.manage"))):
    c = ctx.conn.cursor()
    fields = ["name", "code", "location", "contact", "active"]
    sets, params = [], []
    for f in fields:
        if f in body and body[f] is not None:
            sets.append(f"{f}=%s")
            params.append(body[f])
    if not sets:
        raise HTTPException(400, "Nothing to update")
    params.append(wid)
    c.execute(f"UPDATE warehouses SET {', '.join(sets)} WHERE id=%s", params)
    ctx.conn.commit()
    log_action(ctx.conn, ctx.user["id"], "warehouse.update", "warehouse", wid)
    return {"ok": True}


# ── Gift stock ──────────────────────────────────────────────────────────────

@router.get("/inventory/gift-stock")
def list_gift_stock(ctx: TenantContext = Depends(require_permission("inventory.view"))):
    c = ctx.conn.cursor()
    c.execute("""
        SELECT g.id AS gift_id, g.name AS gift_name, g.sku, g.category, g.cost,
               COALESCE(SUM(gs.quantity),0) AS quantity,
               COALESCE(SUM(gs.reserved),0) AS reserved,
               COALESCE(SUM(gs.quantity - gs.reserved),0) AS available
        FROM gifts g
        LEFT JOIN gift_stock gs ON gs.gift_id=g.id
        GROUP BY g.id ORDER BY g.id""")
    items = fetchall_dict(c)
    c.execute("""SELECT gs.*, g.name AS gift_name, w.name AS warehouse_name
                 FROM gift_stock gs
                 JOIN gifts g ON g.id=gs.gift_id
                 LEFT JOIN warehouses w ON w.id=gs.warehouse_id
                 ORDER BY gs.id""")
    rows = fetchall_dict(c)
    for it in items:
        it["warehouses"] = [r for r in rows if r["gift_id"] == it["gift_id"]]
    return {"items": items}


@router.get("/inventory/movements")
def list_movements(gift_id: int = None, ctx: TenantContext = Depends(require_permission("inventory.view"))):
    c = ctx.conn.cursor()
    sql = """SELECT m.*, g.name AS gift_name, w.name AS warehouse_name, u.full_name AS created_by_name
             FROM gift_stock_movements m
             LEFT JOIN gifts g ON g.id=m.gift_id
             LEFT JOIN warehouses w ON w.id=m.warehouse_id
             LEFT JOIN users u ON u.id=m.created_by"""
    params = ()
    if gift_id:
        sql += " WHERE m.gift_id=%s"
        params = (gift_id,)
    sql += " ORDER BY m.id DESC LIMIT 200"
    c.execute(sql, params)
    return {"items": fetchall_dict(c)}


@router.post("/inventory/gift-stock/inbound")
def stock_inbound(body: dict, ctx: TenantContext = Depends(require_permission("inventory.stock"))):
    """Add stock to a gift at a warehouse (positive quantity = inbound)."""
    gift_id = body.get("gift_id")
    warehouse_id = body.get("warehouse_id")
    qty = int(body.get("quantity") or 0)
    if not gift_id:
        raise HTTPException(400, "gift_id required")
    if qty <= 0:
        raise HTTPException(400, "quantity must be positive")
    conn = ctx.conn
    c = conn.cursor()
    c.execute("INSERT INTO gift_stock (gift_id, warehouse_id, quantity, reserved) "
              "VALUES (%s,%s,%s,0) "
              "ON CONFLICT (gift_id, warehouse_id) DO UPDATE SET "
              "quantity=gift_stock.quantity+EXCLUDED.quantity, updated_at=CURRENT_TIMESTAMP",
              (gift_id, warehouse_id, qty))
    c.execute("""INSERT INTO gift_stock_movements (gift_id, warehouse_id, movement, quantity,
                 reference_type, reference_id, note, created_by)
                 VALUES (%s,%s,'inbound',%s,%s,%s,%s,%s)""",
              (gift_id, warehouse_id, qty, body.get("reference_type"), body.get("reference_id"),
               body.get("note"), ctx.user["id"]))
    conn.commit()
    log_action(conn, ctx.user["id"], "stock.inbound", "gift_stock", gift_id, {"quantity": qty})
    return {"ok": True, "quantity": qty}


@router.post("/inventory/gift-stock/transfer")
def stock_transfer(body: dict, ctx: TenantContext = Depends(require_permission("inventory.stock"))):
    """Move stock between warehouses (adjustment with movement rows)."""
    gift_id = body.get("gift_id")
    src = body.get("from_warehouse_id")
    dst = body.get("to_warehouse_id")
    qty = int(body.get("quantity") or 0)
    if not gift_id or not src or not dst:
        raise HTTPException(400, "gift_id, from_warehouse_id, to_warehouse_id required")
    if qty <= 0:
        raise HTTPException(400, "quantity must be positive")
    if src == dst:
        raise HTTPException(400, "source and destination warehouses must differ")
    conn = ctx.conn
    c = conn.cursor()
    c.execute("SELECT quantity FROM gift_stock WHERE gift_id=%s AND warehouse_id=%s", (gift_id, src))
    row = c.fetchone()
    available = (row[0] if row else 0)
    if available < qty:
        raise HTTPException(409, f"insufficient stock at source: {available} available")
    c.execute("UPDATE gift_stock SET quantity=quantity-%s WHERE gift_id=%s AND warehouse_id=%s",
              (qty, gift_id, src))
    c.execute("INSERT INTO gift_stock (gift_id, warehouse_id, quantity, reserved) VALUES (%s,%s,%s,0) "
              "ON CONFLICT (gift_id, warehouse_id) DO UPDATE SET "
              "quantity=gift_stock.quantity+EXCLUDED.quantity, updated_at=CURRENT_TIMESTAMP",
              (gift_id, dst, qty))
    note = f"Transfer {qty} from warehouse {src} to {dst}"
    c.execute("""INSERT INTO gift_stock_movements (gift_id, warehouse_id, movement, quantity,
                 reference_type, reference_id, note, created_by)
                 VALUES (%s,%s,'adjustment',-1*%s,'transfer',%s,%s,%s)""",
              (gift_id, src, qty, body.get("reference_id"), note, ctx.user["id"]))
    c.execute("""INSERT INTO gift_stock_movements (gift_id, warehouse_id, movement, quantity,
                 reference_type, reference_id, note, created_by)
                 VALUES (%s,%s,'adjustment',%s,'transfer',%s,%s,%s)""",
              (gift_id, dst, qty, body.get("reference_id"), note, ctx.user["id"]))
    conn.commit()
    log_action(conn, ctx.user["id"], "stock.transfer", "gift_stock", gift_id, {"quantity": qty})
    return {"ok": True, "quantity": qty}


@router.post("/inventory/gift-stock/adjust")
def stock_adjust(body: dict, ctx: TenantContext = Depends(require_permission("inventory.stock"))):
    """Set an absolute quantity for a gift at a warehouse."""
    gift_id = body.get("gift_id")
    warehouse_id = body.get("warehouse_id")
    qty = int(body.get("quantity") or 0)
    if not gift_id or not warehouse_id:
        raise HTTPException(400, "gift_id and warehouse_id required")
    conn = ctx.conn
    c = conn.cursor()
    c.execute("SELECT quantity FROM gift_stock WHERE gift_id=%s AND warehouse_id=%s", (gift_id, warehouse_id))
    row = c.fetchone()
    prev = row[0] if row else 0
    c.execute("""INSERT INTO gift_stock (gift_id, warehouse_id, quantity, reserved) VALUES (%s,%s,%s,0)
                 ON CONFLICT (gift_id, warehouse_id) DO UPDATE SET
                 quantity=EXCLUDED.quantity, updated_at=CURRENT_TIMESTAMP""",
              (gift_id, warehouse_id, qty))
    c.execute("""INSERT INTO gift_stock_movements (gift_id, warehouse_id, movement, quantity,
                 reference_type, reference_id, note, created_by)
                 VALUES (%s,%s,'adjustment',%s,'adjustment',NULL,%s,%s)""",
              (gift_id, warehouse_id, qty - prev, body.get("note") or f"Adjusted {prev} -> {qty}",
               ctx.user["id"]))
    conn.commit()
    log_action(conn, ctx.user["id"], "stock.adjust", "gift_stock", gift_id,
               {"prev": prev, "new": qty})
    return {"ok": True, "quantity": qty}
