"""Material ordering endpoints: suppliers, SKU catalog with availability,
and order lifecycle (draft → placed → confirmed → received) — all
org-scoped (GATE 1)."""

import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import (
    get_session, SupplierModel, CatalogItemModel, MaterialOrderModel,
    MATERIAL_ORDER_STATUSES, MATERIAL_ORDER_TRANSITIONS,
)
from core.rbac import require_permission
from core.tenancy import AuthContext, scoped_or_404, audit

logger = logging.getLogger("api.materials")

router = APIRouter(prefix="/materials", tags=["materials"])


# ─── Suppliers ──────────────────────────────────────────────────────────────

class SupplierIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    contact_phone: str | None = None
    contact_email: str | None = None
    address: str | None = None
    notes: str | None = None


@router.get("/suppliers")
async def list_suppliers(ctx: AuthContext = Depends(require_permission("materials:read")),
                         session: AsyncSession = Depends(get_session)):
    suppliers = (await session.execute(
        select(SupplierModel)
        .where(SupplierModel.organization_id == ctx.organization_id,
               SupplierModel.is_active.is_(True))
        .order_by(SupplierModel.name))).scalars().all()
    return {"suppliers": [{
        "id": s.id, "name": s.name, "contact_phone": s.contact_phone,
        "contact_email": s.contact_email, "address": s.address,
    } for s in suppliers]}


@router.post("/suppliers", status_code=201)
async def create_supplier(body: SupplierIn,
                          ctx: AuthContext = Depends(require_permission("materials:write")),
                          session: AsyncSession = Depends(get_session)):
    supplier = SupplierModel(organization_id=ctx.organization_id, **body.model_dump())
    session.add(supplier)
    await audit(session, "materials.supplier_created", ctx=ctx, supplier=body.name)
    await session.commit()
    return {"id": supplier.id, "name": supplier.name}


@router.patch("/suppliers/{supplier_id}")
async def update_supplier(supplier_id: str, body: SupplierIn,
                          ctx: AuthContext = Depends(require_permission("materials:write")),
                          session: AsyncSession = Depends(get_session)):
    supplier = await scoped_or_404(session, SupplierModel, supplier_id, ctx, "Supplier")
    for key, value in body.model_dump().items():
        setattr(supplier, key, value)
    await audit(session, "materials.supplier_updated", ctx=ctx, supplier_id=supplier_id)
    await session.commit()
    return {"status": "updated", "id": supplier.id}


@router.delete("/suppliers/{supplier_id}", status_code=204)
async def deactivate_supplier(supplier_id: str,
                              ctx: AuthContext = Depends(require_permission("materials:write")),
                              session: AsyncSession = Depends(get_session)):
    supplier = await scoped_or_404(session, SupplierModel, supplier_id, ctx, "Supplier")
    supplier.is_active = False
    await audit(session, "materials.supplier_deactivated", ctx=ctx, supplier_id=supplier_id)
    await session.commit()


# ─── Catalog (SKU / unit price / availability) ──────────────────────────────

class CatalogItemIn(BaseModel):
    supplier_id: str
    sku: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    unit: str = Field(default="each", max_length=32)
    unit_price: float = Field(gt=0, le=10_000_000)
    quantity_available: float = Field(default=0, ge=0)


class CatalogItemUpdate(BaseModel):
    unit_price: float | None = Field(default=None, gt=0, le=10_000_000)
    quantity_available: float | None = Field(default=None, ge=0)
    name: str | None = None
    is_active: bool | None = None


def _item_out(i: CatalogItemModel) -> dict:
    return {
        "id": i.id, "supplier_id": i.supplier_id, "sku": i.sku, "name": i.name,
        "unit": i.unit, "unit_price": i.unit_price,
        "quantity_available": i.quantity_available, "is_active": i.is_active,
    }


@router.get("/catalog")
async def list_catalog(supplier_id: str | None = None, sku: str | None = None,
                       ctx: AuthContext = Depends(require_permission("materials:read")),
                       session: AsyncSession = Depends(get_session)):
    stmt = select(CatalogItemModel).where(
        CatalogItemModel.organization_id == ctx.organization_id,
        CatalogItemModel.is_active.is_(True))
    if supplier_id:
        await scoped_or_404(session, SupplierModel, supplier_id, ctx, "Supplier")
        stmt = stmt.where(CatalogItemModel.supplier_id == supplier_id)
    if sku:
        stmt = stmt.where(CatalogItemModel.sku == sku)
    items = (await session.execute(stmt.order_by(CatalogItemModel.name))).scalars().all()
    return {"items": [_item_out(i) for i in items]}


@router.post("/catalog", status_code=201)
async def create_catalog_item(body: CatalogItemIn,
                              ctx: AuthContext = Depends(require_permission("materials:write")),
                              session: AsyncSession = Depends(get_session)):
    await scoped_or_404(session, SupplierModel, body.supplier_id, ctx, "Supplier")

    existing = (await session.execute(
        select(CatalogItemModel).where(
            CatalogItemModel.organization_id == ctx.organization_id,
            CatalogItemModel.supplier_id == body.supplier_id,
            CatalogItemModel.sku == body.sku))).scalar()
    if existing:
        raise HTTPException(status_code=409,
                            detail="This SKU already exists for this supplier")

    item = CatalogItemModel(organization_id=ctx.organization_id, **body.model_dump())
    session.add(item)
    await audit(session, "materials.catalog_item_created", ctx=ctx,
                sku=body.sku, name=body.name)
    await session.commit()
    return _item_out(item)


@router.patch("/catalog/{item_id}")
async def update_catalog_item(item_id: str, body: CatalogItemUpdate,
                              ctx: AuthContext = Depends(require_permission("materials:write")),
                              session: AsyncSession = Depends(get_session)):
    item = await scoped_or_404(session, CatalogItemModel, item_id, ctx, "Catalog item")
    for key, value in body.model_dump(exclude_none=True).items():
        setattr(item, key, value)
    await audit(session, "materials.catalog_item_updated", ctx=ctx, item_id=item_id)
    await session.commit()
    return _item_out(item)


@router.get("/availability")
async def check_availability(sku: str = Query(min_length=1),
                             ctx: AuthContext = Depends(require_permission("materials:read")),
                             session: AsyncSession = Depends(get_session)):
    """Availability for a SKU across this org's suppliers, best price first."""
    items = (await session.execute(
        select(CatalogItemModel).where(
            CatalogItemModel.organization_id == ctx.organization_id,
            CatalogItemModel.sku == sku,
            CatalogItemModel.is_active.is_(True))
        .order_by(CatalogItemModel.unit_price))).scalars().all()
    return {"sku": sku, "offers": [{
        "supplier_id": i.supplier_id, "item_id": i.id, "name": i.name,
        "unit_price": i.unit_price, "quantity_available": i.quantity_available,
        "in_stock": i.quantity_available > 0,
    } for i in items]}


# ─── Orders & lifecycle ─────────────────────────────────────────────────────

class OrderItemIn(BaseModel):
    catalog_item_id: str
    quantity: float = Field(gt=0, le=1_000_000)


class OrderIn(BaseModel):
    supplier_id: str
    quote_id: str | None = None
    items: list[OrderItemIn] = Field(min_length=1)
    pickup_time: str | None = None


class OrderStatusIn(BaseModel):
    status: str


def _order_out(o: MaterialOrderModel) -> dict:
    return {
        "order_id": o.order_id, "supplier_id": o.supplier_id,
        "quote_id": o.quote_id, "status": o.status, "items": o.items,
        "subtotal": o.subtotal, "tax": o.tax, "total": o.total,
        "pickup_time": o.pickup_time, "created_at": o.created_at.isoformat(),
    }


@router.get("/orders")
async def list_orders(status: str | None = None,
                      ctx: AuthContext = Depends(require_permission("orders:read")),
                      session: AsyncSession = Depends(get_session)):
    stmt = select(MaterialOrderModel).where(
        MaterialOrderModel.organization_id == ctx.organization_id)
    if status:
        if status not in MATERIAL_ORDER_STATUSES:
            raise HTTPException(status_code=422,
                                detail=f"status must be one of {MATERIAL_ORDER_STATUSES}")
        stmt = stmt.where(MaterialOrderModel.status == status)
    orders = (await session.execute(stmt.order_by(MaterialOrderModel.created_at.desc()))).scalars().all()
    return {"orders": [_order_out(o) for o in orders]}


@router.post("/orders", status_code=201)
async def create_order(body: OrderIn,
                       ctx: AuthContext = Depends(require_permission("orders:write")),
                       session: AsyncSession = Depends(get_session)):
    """Create a materials order against a supplier's catalog. Availability
    is validated and reserved into the order line at creation time."""
    supplier = await scoped_or_404(session, SupplierModel, body.supplier_id, ctx, "Supplier")
    if body.quote_id:
        from core.database import QuoteModel
        await scoped_or_404(session, QuoteModel, body.quote_id, ctx, "Quote")

    resolved = []
    subtotal = 0.0
    for line in body.items:
        item = await scoped_or_404(session, CatalogItemModel, line.catalog_item_id,
                                   ctx, "Catalog item")
        if item.supplier_id != supplier.id:
            raise HTTPException(status_code=422,
                                detail=f"Item {item.sku} belongs to a different supplier")
        if item.quantity_available < line.quantity:
            raise HTTPException(status_code=409, detail={
                "message": "Insufficient availability",
                "sku": item.sku,
                "requested": line.quantity,
                "available": item.quantity_available,
            })
        line_cost = round(item.unit_price * line.quantity, 2)
        subtotal += line_cost
        resolved.append({
            "catalog_item_id": item.id, "sku": item.sku, "name": item.name,
            "unit": item.unit, "unit_price": item.unit_price,
            "quantity": line.quantity, "cost": line_cost,
            "availability_at_order": item.quantity_available,
        })

    order = MaterialOrderModel(
        order_id=f"ORD-{secrets.token_hex(5).upper()}",
        organization_id=ctx.organization_id,
        supplier_id=supplier.id,
        quote_id=body.quote_id or "",
        status="draft",
        store_name=supplier.name,
        store_address=supplier.address,
        pickup_time=body.pickup_time,
        subtotal=round(subtotal, 2),
        items=resolved,
    )
    order.total = order.subtotal  # supplier totals exclude tax unless added
    session.add(order)
    await audit(session, "materials.order_created", ctx=ctx,
                order_id=order.order_id, supplier=supplier.name)
    await session.commit()
    return _order_out(order)


@router.patch("/orders/{order_id}/status")
async def update_order_status(order_id: str, body: OrderStatusIn,
                              ctx: AuthContext = Depends(require_permission("orders:write")),
                              session: AsyncSession = Depends(get_session)):
    order = await scoped_or_404(session, MaterialOrderModel, order_id, ctx, "Order")
    if body.status not in MATERIAL_ORDER_STATUSES:
        raise HTTPException(status_code=422,
                            detail=f"status must be one of {MATERIAL_ORDER_STATUSES}")
    allowed = MATERIAL_ORDER_TRANSITIONS[order.status]
    if body.status not in allowed:
        raise HTTPException(status_code=409, detail={
            "message": f"Cannot move order from '{order.status}' to '{body.status}'",
            "allowed_next": sorted(allowed),
        })
    order.status = body.status
    await audit(session, "materials.order_status_changed", ctx=ctx,
                order_id=order_id, status=body.status)
    await session.commit()
    return _order_out(order)


@router.get("/orders/{order_id}")
async def get_order(order_id: str,
                    ctx: AuthContext = Depends(require_permission("orders:read")),
                    session: AsyncSession = Depends(get_session)):
    order = await scoped_or_404(session, MaterialOrderModel, order_id, ctx, "Order")
    return _order_out(order)
