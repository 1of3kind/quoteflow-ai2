"""Dashboard API endpoints for Retool integration.

These endpoints return flat, table-friendly data structures
that Retool can directly bind to tables, charts, and lists.
"""

import os
import logging
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel

from core.conversation_manager import ConversationManager, ConversationStage
from core.quote_calculator import QuoteCalculator
from materials.order_manager import get_order_manager
from payments.billing import get_billing_manager, PLANS
from payments.stripe_client import get_stripe_client

logger = logging.getLogger("dashboard")

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


# ─── DATA MODELS FOR RETOOL ────────────────────────────────────

class DashboardMetrics(BaseModel):
    total_quotes_today: int
    total_quotes_this_month: int
    conversion_rate: float
    revenue_today: float
    revenue_this_month: float
    active_conversations: int
    pending_pickups: int
    avg_quote_value: float
    top_trade: str


class QuoteRow(BaseModel):
    quote_id: str
    customer_id: str
    customer_phone: str
    trade: str
    total: float
    status: str
    created_at: str
    materials_ordered: bool
    appointment_scheduled: bool
    payment_status: str


class AppointmentRow(BaseModel):
    appointment_id: str
    customer_name: str
    customer_phone: str
    trade: str
    scheduled_date: str
    time_slot: str
    duration_hours: float
    materials_store: str
    materials_picked_up: bool
    quote_total: float
    payment_status: str
    status: str


class ContractorRow(BaseModel):
    contractor_id: str
    business_name: str
    email: str
    phone: str
    plan: str
    plan_name: str
    is_active: bool
    in_trial: bool
    trial_ends: Optional[str]
    quotes_used: int
    quotes_limit: str
    trades_enabled: List[str]
    mrr: float


class MaterialPickupRow(BaseModel):
    appointment_id: str
    customer_name: str
    trade: str
    job_date: str
    store_name: str
    store_address: str
    pickup_time: str
    items_count: int
    total: float
    order_id: str
    confirmed: bool


class RevenueRow(BaseModel):
    date: str
    quotes_sent: int
    quotes_accepted: int
    conversion_rate: float
    revenue: float
    materials_cost: float
    net_profit: float


# ─── MOCK DATA STORE (Replace with DB in production) ───────────

class DashboardDataStore:
    """In-memory store for dashboard data. Replace with PostgreSQL queries."""

    def __init__(self):
        self.quotes: List[Dict] = []
        self.appointments: List[Dict] = []
        self.payments: List[Dict] = []
        self.daily_revenue: List[Dict] = []

    def add_quote(self, quote_data: dict):
        self.quotes.append({
            **quote_data,
            "created_at": datetime.utcnow().isoformat(),
        })

    def get_today_quotes(self) -> List[Dict]:
        today = datetime.utcnow().date()
        return [q for q in self.quotes if datetime.fromisoformat(q["created_at"]).date() == today]

    def get_month_quotes(self) -> List[Dict]:
        now = datetime.utcnow()
        return [q for q in self.quotes if datetime.fromisoformat(q["created_at"]).month == now.month]


_store = DashboardDataStore()


# ─── ENDPOINTS ─────────────────────────────────────────────────

@router.get("/metrics")
async def get_metrics() -> DashboardMetrics:
    """Get high-level dashboard metrics for Retool KPI cards."""

    conv_mgr = ConversationManager()  # Would fetch from DB
    order_mgr = get_order_manager()
    billing = get_billing_manager()

    today = datetime.utcnow().date()
    month_start = today.replace(day=1)

    # Calculate metrics from store data
    today_quotes = _store.get_today_quotes()
    month_quotes = _store.get_month_quotes()

    total_revenue_today = sum(q.get("total", 0) for q in today_quotes)
    total_revenue_month = sum(q.get("total", 0) for q in month_quotes)

    accepted = len([q for q in month_quotes if q.get("status") == "accepted"])
    conversion = (accepted / len(month_quotes) * 100) if month_quotes else 0

    avg_value = (total_revenue_month / len(month_quotes)) if month_quotes else 0

    # Count trades
    trade_counts = {}
    for q in month_quotes:
        t = q.get("trade", "unknown")
        trade_counts[t] = trade_counts.get(t, 0) + 1
    top_trade = max(trade_counts, key=trade_counts.get) if trade_counts else "none"

    pending_pickups = len(order_mgr.get_pending_pickups())

    return DashboardMetrics(
        total_quotes_today=len(today_quotes),
        total_quotes_this_month=len(month_quotes),
        conversion_rate=round(conversion, 1),
        revenue_today=round(total_revenue_today, 2),
        revenue_this_month=round(total_revenue_month, 2),
        active_conversations=0,  # Would query DB
        pending_pickups=pending_pickups,
        avg_quote_value=round(avg_value, 2),
        top_trade=top_trade,
    )


@router.get("/quotes")
async def get_quotes(
    status: Optional[str] = None,
    trade: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> List[QuoteRow]:
    """Get quotes table for Retool. Supports filtering."""

    quotes = _store.quotes.copy()

    # Apply filters
    if status:
        quotes = [q for q in quotes if q.get("status") == status]
    if trade:
        quotes = [q for q in quotes if q.get("trade") == trade]
    if date_from:
        from_date = datetime.fromisoformat(date_from).date()
        quotes = [q for q in quotes if datetime.fromisoformat(q["created_at"]).date() >= from_date]
    if date_to:
        to_date = datetime.fromisoformat(date_to).date()
        quotes = [q for q in quotes if datetime.fromisoformat(q["created_at"]).date() <= to_date]

    # Sort by date desc
    quotes.sort(key=lambda x: x["created_at"], reverse=True)

    # Paginate
    quotes = quotes[offset:offset + limit]

    return [
        QuoteRow(
            quote_id=q.get("quote_id", ""),
            customer_id=q.get("customer_id", ""),
            customer_phone=q.get("customer_phone", ""),
            trade=q.get("trade", "").title(),
            total=q.get("total", 0),
            status=q.get("status", "pending").title(),
            created_at=q.get("created_at", ""),
            materials_ordered=q.get("materials_ordered", False),
            appointment_scheduled=q.get("appointment_scheduled", False),
            payment_status=q.get("payment_status", "unpaid"),
        )
        for q in quotes
    ]


@router.get("/appointments")
async def get_appointments(
    date: Optional[str] = None,
    trade: Optional[str] = None,
    status: Optional[str] = None,
) -> List[AppointmentRow]:
    """Get appointments table for Retool calendar/list view."""

    order_mgr = get_order_manager()

    query_date = datetime.fromisoformat(date).date() if date else datetime.utcnow().date()
    appointments = order_mgr.get_daily_schedule(datetime.combine(query_date, datetime.min.time()))

    if trade:
        appointments = [a for a in appointments if a.trade == trade]
    if status:
        appointments = [a for a in appointments if a.status == status]

    return [
        AppointmentRow(
            appointment_id=apt.appointment_id,
            customer_name=apt.customer_name,
            customer_phone=apt.customer_phone,
            trade=apt.trade.title(),
            scheduled_date=apt.scheduled_date.strftime("%Y-%m-%d"),
            time_slot=apt.scheduled_date.strftime("%I:%M %p"),
            duration_hours=apt.estimated_duration_hours,
            materials_store=apt.materials_order.store_name if apt.materials_order else "N/A",
            materials_picked_up=apt.materials_confirmed,
            quote_total=apt.materials_order.total if apt.materials_order else 0,
            payment_status="paid",  # Would fetch from payments
            status=apt.status if hasattr(apt, 'status') else "scheduled",
        )
        for apt in appointments
    ]


@router.get("/materials/pickups")
async def get_pending_pickups() -> List[MaterialPickupRow]:
    """Get pending material pickups for contractor daily checklist."""

    order_mgr = get_order_manager()
    pending = order_mgr.get_pending_pickups()

    return [
        MaterialPickupRow(
            appointment_id=apt.appointment_id,
            customer_name=apt.customer_name,
            trade=apt.trade.title(),
            job_date=apt.scheduled_date.strftime("%Y-%m-%d"),
            store_name=apt.materials_order.store_name if apt.materials_order else "N/A",
            store_address=apt.materials_order.store_address if apt.materials_order else "",
            pickup_time=apt.materials_order.pickup_time if apt.materials_order else "",
            items_count=len(apt.materials_order.items) if apt.materials_order else 0,
            total=apt.materials_order.total if apt.materials_order else 0,
            order_id=apt.materials_order.order_id if apt.materials_order else "",
            confirmed=apt.materials_confirmed,
        )
        for apt in pending
    ]


@router.get("/contractors")
async def get_contractors(
    plan: Optional[str] = None,
    status: Optional[str] = None,
) -> List[ContractorRow]:
    """Get contractor accounts for admin management."""

    billing = get_billing_manager()

    contractors = []
    for cid, account in billing.accounts.items():
        plan_info = PLANS.get(account.plan)

        if plan and account.plan != plan:
            continue
        if status == "active" and not account.is_active:
            continue
        if status == "trial" and not account.trial_ends_at:
            continue

        contractors.append(ContractorRow(
            contractor_id=account.contractor_id,
            business_name=account.business_name,
            email=account.email,
            phone=account.phone,
            plan=account.plan,
            plan_name=plan_info.name if plan_info else "Unknown",
            is_active=account.is_active,
            in_trial=account.trial_ends_at is not None and datetime.fromisoformat(account.trial_ends_at) > datetime.utcnow(),
            trial_ends=account.trial_ends_at,
            quotes_used=account.quotes_used_this_month,
            quotes_limit="Unlimited" if plan_info and plan_info.limits["quotes_per_month"] == float('inf') else str(plan_info.limits["quotes_per_month"]),
            trades_enabled=account.trades_enabled,
            mrr=plan_info.price_monthly if plan_info else 0,
        ))

    return contractors


@router.get("/revenue")
async def get_revenue(
    period: str = Query("daily", enum=["daily", "weekly", "monthly"]),
    days: int = Query(30, ge=7, le=365),
) -> List[RevenueRow]:
    """Get revenue chart data for Retool charts."""

    end_date = datetime.utcnow().date()

    if period == "daily":
        dates = [end_date - timedelta(days=i) for i in range(days)]
    elif period == "weekly":
        dates = [end_date - timedelta(weeks=i) for i in range(days // 7)]
    else:
        dates = [end_date - timedelta(days=i*30) for i in range(days // 30)]

    dates.reverse()  # Oldest first for charts

    revenue_data = []
    for d in dates:
        # Mock data - replace with actual DB queries
        quotes_sent = len([q for q in _store.quotes if datetime.fromisoformat(q["created_at"]).date() == d])
        quotes_accepted = len([q for q in _store.quotes if datetime.fromisoformat(q["created_at"]).date() == d and q.get("status") == "accepted"])
        revenue = sum(q.get("total", 0) for q in _store.quotes if datetime.fromisoformat(q["created_at"]).date() == d and q.get("status") == "accepted")
        materials_cost = revenue * 0.35  # Rough estimate

        revenue_data.append(RevenueRow(
            date=d.isoformat(),
            quotes_sent=quotes_sent,
            quotes_accepted=quotes_accepted,
            conversion_rate=round((quotes_accepted / quotes_sent * 100), 1) if quotes_sent else 0,
            revenue=round(revenue, 2),
            materials_cost=round(materials_cost, 2),
            net_profit=round(revenue - materials_cost, 2),
        ))

    return revenue_data


@router.post("/quotes/{quote_id}/status")
async def update_quote_status(quote_id: str, status: str):
    """Update quote status from Retool (admin action)."""
    for q in _store.quotes:
        if q.get("quote_id") == quote_id:
            q["status"] = status
            return {"status": "updated", "quote_id": quote_id, "new_status": status}
    raise HTTPException(status_code=404, detail="Quote not found")


@router.get("/trades/breakdown")
async def get_trade_breakdown() -> List[Dict]:
    """Get quote volume by trade for pie/bar charts."""

    trade_counts = {}
    trade_revenue = {}

    for q in _store.quotes:
        t = q.get("trade", "unknown")
        trade_counts[t] = trade_counts.get(t, 0) + 1
        if q.get("status") == "accepted":
            trade_revenue[t] = trade_revenue.get(t, 0) + q.get("total", 0)

    return [
        {
            "trade": t.title(),
            "quote_count": c,
            "revenue": round(trade_revenue.get(t, 0), 2),
            "avg_quote": round(trade_revenue.get(t, 0) / c, 2) if c else 0,
        }
        for t, c in trade_counts.items()
    ]
