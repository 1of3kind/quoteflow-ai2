"""Org-scoped dashboard endpoints backed by real database queries (GATE 10).

Replaces the previous mock in-memory dashboard. Every metric is filtered by
the authenticated organization (GATE 1)."""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import (
    get_session, QuoteModel, JobModel, OrgCustomerModel, AuditLogModel,
    MaterialOrderModel,
)
from core.rbac import can, require_permission
from core.tenancy import AuthContext, AuthContext, get_current_user

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/summary")
async def dashboard_summary(ctx: AuthContext = Depends(require_permission("org:read")),
                            session: AsyncSession = Depends(get_session)):
    """Active Jobs, Pending Quotes, Approved Quotes, Revenue, Profit,
    Upcoming Jobs, Recent Activity — for this organization only."""
    org = ctx.organization_id
    now = datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    async def count(model, *conds) -> int:
        stmt = select(func.count()).select_from(model).where(model.organization_id == org, *conds)
        return (await session.execute(stmt)).scalar_one()

    active_jobs = await count(JobModel, JobModel.status.in_(["scheduled", "in_progress"]))
    pending_quotes = await count(QuoteModel, QuoteModel.status.in_(["draft", "sent"]))
    approved_quotes = await count(QuoteModel, QuoteModel.status == "accepted")

    # Revenue & profit this month, computed from the authoritative engine
    # results stored on each accepted quote.
    accepted_month = (await session.execute(
        select(QuoteModel).where(and_(
            QuoteModel.organization_id == org,
            QuoteModel.status == "accepted",
            QuoteModel.accepted_at >= month_start)))).scalars().all()
    revenue = sum(q.total for q in accepted_month)
    profit = sum((q.engine_result or {}).get("profit", 0.0) for q in accepted_month)

    upcoming = (await session.execute(
        select(JobModel).where(and_(
            JobModel.organization_id == org,
            JobModel.status.in_(["scheduled", "in_progress"]),
            or_(JobModel.scheduled_date >= now, JobModel.scheduled_date.is_(None)),
        )).order_by(JobModel.scheduled_date.asc().nullslast()).limit(10))).scalars().all()

    activity = (await session.execute(
        select(AuditLogModel).where(AuditLogModel.organization_id == org)
        .order_by(AuditLogModel.created_at.desc()).limit(10))).scalars().all()

    return {
        "active_jobs": active_jobs,
        "pending_quotes": pending_quotes,
        "approved_quotes": approved_quotes,
        "revenue_this_month": round(revenue, 2),
        "profit_this_month": round(profit, 2),
        "upcoming_jobs": [{
            "id": j.id, "title": j.title, "trade": j.trade, "status": j.status,
            "scheduled_date": j.scheduled_date.isoformat() if j.scheduled_date else None,
        } for j in upcoming],
        "recent_activity": [{
            "action": a.action,
            "detail": a.detail,
            "at": a.created_at.isoformat(),
        } for a in activity],
    }


@router.get("/revenue")
async def revenue_series(days: int = Query(30, ge=7, le=365),
                         ctx: AuthContext = Depends(require_permission("reports:full")),
                         session: AsyncSession = Depends(get_session)):
    """Daily quote/revenue series for charts — org-scoped."""
    org = ctx.organization_id
    start = datetime.utcnow() - timedelta(days=days)
    quotes = (await session.execute(
        select(QuoteModel).where(and_(
            QuoteModel.organization_id == org,
            QuoteModel.created_at >= start)))).scalars().all()

    by_day: dict[str, dict] = {}
    for q in quotes:
        key = q.created_at.date().isoformat()
        day = by_day.setdefault(key, {"date": key, "quotes_sent": 0, "quotes_accepted": 0,
                                      "revenue": 0.0, "profit": 0.0})
        if q.status in ("sent", "accepted", "expired", "declined"):
            day["quotes_sent"] += 1
        if q.status == "accepted":
            day["quotes_accepted"] += 1
            day["revenue"] += q.total
            day["profit"] += (q.engine_result or {}).get("profit", 0.0)
    series = sorted(by_day.values(), key=lambda d: d["date"])
    for d in series:
        d["revenue"] = round(d["revenue"], 2)
        d["profit"] = round(d["profit"], 2)
    return {"series": series}


@router.get("/materials/pickups")
async def pending_pickups(ctx: AuthContext = Depends(require_permission("orders:read")),
                          session: AsyncSession = Depends(get_session)):
    org = ctx.organization_id
    orders = (await session.execute(
        select(MaterialOrderModel).where(MaterialOrderModel.organization_id == org)
        .order_by(MaterialOrderModel.created_at.desc()))).scalars().all()
    return {"orders": [{
        "order_id": o.order_id, "quote_id": o.quote_id, "subtotal": o.subtotal,
        "items": o.items, "created_at": o.created_at.isoformat(),
    } for o in orders]}
