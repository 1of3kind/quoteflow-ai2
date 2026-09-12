"""Billing endpoints (GATE 5). OWNER-only per the RBAC table: the browser
never grants anything; activation/state comes from the Stripe webhook."""

import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_session
from core.rbac import require_permission
from core.tenancy import AuthContext, audit
from payments.billing import (
    BillingService, BillingError, PLANS, get_billing_service, set_billing_service,
)
from payments.stripe_gateway import get_gateway, GatewayError, set_gateway

logger = logging.getLogger("api.billing")

router = APIRouter(prefix="/billing", tags=["billing"])


class CheckoutIn(BaseModel):
    plan: str
    billing_cycle: str = "monthly"


def _base_url(request: Request) -> str:
    base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    return base or str(request.base_url).rstrip("/")


@router.get("/plans")
async def list_plans():
    return {"plans": [{
        "id": p.id, "name": p.name,
        "price_monthly": p.price_monthly, "price_annual": p.price_annual,
        "features": p.features, "limits": p.public_limits(),
    } for p in PLANS.values()]}


@router.get("/subscription")
async def subscription_status(ctx: AuthContext = Depends(require_permission("billing")),
                              session: AsyncSession = Depends(get_session)):
    return await get_billing_service().subscription_status(session, ctx.organization_id)


@router.post("/checkout")
async def create_checkout(body: CheckoutIn, request: Request,
                          ctx: AuthContext = Depends(require_permission("billing")),
                          session: AsyncSession = Depends(get_session)):
    try:
        result = await get_billing_service().create_checkout(
            session, ctx.organization_id, body.plan, body.billing_cycle, _base_url(request))
    except BillingError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except GatewayError as e:
        raise HTTPException(status_code=503, detail=str(e))
    await audit(session, "billing.checkout_started", ctx=ctx, plan=body.plan)
    await session.commit()
    return result


@router.post("/portal")
async def billing_portal(request: Request,
                         ctx: AuthContext = Depends(require_permission("billing")),
                         session: AsyncSession = Depends(get_session)):
    try:
        result = await get_billing_service().create_portal(
            session, ctx.organization_id, _base_url(request) + "/billing")
    except BillingError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except GatewayError as e:
        raise HTTPException(status_code=503, detail=str(e))
    await session.commit()
    return result


class ChangePlanIn(BaseModel):
    plan: str


@router.post("/change-plan")
async def change_plan(body: ChangePlanIn,
                      ctx: AuthContext = Depends(require_permission("billing")),
                      session: AsyncSession = Depends(get_session)):
    try:
        result = await get_billing_service().change_plan(session, ctx.organization_id, body.plan)
    except BillingError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except GatewayError as e:
        raise HTTPException(status_code=503, detail=str(e))
    await audit(session, "billing.plan_change_requested", ctx=ctx, plan=body.plan)
    await session.commit()
    return result


@router.post("/cancel")
async def cancel_subscription(ctx: AuthContext = Depends(require_permission("billing")),
                              session: AsyncSession = Depends(get_session)):
    result = await get_billing_service().cancel(session, ctx.organization_id)
    await audit(session, "billing.cancel_requested", ctx=ctx)
    await session.commit()
    return result


@router.get("/invoices")
async def invoices(ctx: AuthContext = Depends(require_permission("billing")),
                   session: AsyncSession = Depends(get_session)):
    try:
        return {"invoices": await get_billing_service().list_invoices(session, ctx.organization_id)}
    except GatewayError as e:
        raise HTTPException(status_code=503, detail=str(e))
