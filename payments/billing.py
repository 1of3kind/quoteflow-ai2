"""SaaS subscription management for organizations (GATE 5).

Rules of the system:
  - The Stripe webhook is the ONLY writer of paid subscription state.
    The browser/checkout success page grants nothing.
  - Every webhook event is processed exactly once (WebhookEventModel ledger,
    keyed by Stripe event id, enforced with a savepoint insert).
  - Failed payments move the org to `past_due` with a bounded grace period;
    after the grace window, access is restricted until payment recovers.
  - New organizations start on a 14-day trial (trial handling).
"""

import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import SubscriptionModel, WebhookEventModel, OrganizationModel
from payments.stripe_gateway import get_gateway, GatewayError, MockStripeGateway

logger = logging.getLogger("billing")

TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "14"))
GRACE_DAYS = int(os.getenv("BILLING_GRACE_DAYS", "3"))

# Plans kept in sync with the public /plans listing.
from dataclasses import dataclass, field


@dataclass
class Plan:
    id: str
    name: str
    price_monthly: float
    price_annual: float
    stripe_price_id_monthly: str
    stripe_price_id_annual: str
    features: list
    limits: dict = field(default_factory=dict)

    def public_limits(self) -> dict:
        """JSON-safe limits: infinite quotas become None (= unlimited)."""
        import math
        return {
            k: (None if isinstance(v, float) and math.isinf(v) else v)
            for k, v in self.limits.items()
        }


PLANS: dict[str, Plan] = {
    "starter": Plan(
        id="starter", name="Starter", price_monthly=0, price_annual=0,
        stripe_price_id_monthly="price_free", stripe_price_id_annual="price_free",
        features=["10 quotes per month", "1 trade", "SMS quotes only", "Basic analytics"],
        limits={"quotes_per_month": 10, "trades": 1, "team_members": 1},
    ),
    "pro": Plan(
        id="pro", name="Pro", price_monthly=49, price_annual=39,
        stripe_price_id_monthly=os.getenv("STRIPE_PRICE_PRO_MONTHLY", "price_pro_monthly"),
        stripe_price_id_annual=os.getenv("STRIPE_PRICE_PRO_ANNUAL", "price_pro_annual"),
        features=["Unlimited quotes", "All trades", "SMS + Email + Voice",
                  "Materials ordering", "Advanced analytics", "3 team members"],
        limits={"quotes_per_month": float("inf"), "trades": 5, "team_members": 3},
    ),
    "business": Plan(
        id="business", name="Business", price_monthly=99, price_annual=79,
        stripe_price_id_monthly=os.getenv("STRIPE_PRICE_BUSINESS_MONTHLY", "price_business_monthly"),
        stripe_price_id_annual=os.getenv("STRIPE_PRICE_BUSINESS_ANNUAL", "price_business_annual"),
        features=["Everything in Pro", "White-label branding", "API access",
                  "Priority support", "Unlimited team members"],
        limits={"quotes_per_month": float("inf"), "trades": 10, "team_members": float("inf")},
    ),
}

# Stripe subscription status → our status
_STATUS_MAP = {
    "trialing": "trialing",
    "active": "active",
    "past_due": "past_due",
    "canceled": "canceled",
    "unpaid": "past_due",
    "incomplete": "past_due",
    "incomplete_expired": "canceled",
}


class BillingError(ValueError):
    pass


class BillingService:
    def __init__(self, gateway=None):
        self.gateway = gateway or get_gateway()

    # ── Trial & access control ─────────────────────────────────────────

    async def ensure_subscription(self, session: AsyncSession, organization_id: str) -> SubscriptionModel:
        sub = await session.scalar(
            select(SubscriptionModel).where(SubscriptionModel.organization_id == organization_id))
        if sub:
            return sub
        sub = SubscriptionModel(
            organization_id=organization_id,
            plan="starter",
            status="trialing",
            trial_ends_at=datetime.utcnow() + timedelta(days=TRIAL_DAYS),
        )
        session.add(sub)
        await session.flush()
        return sub

    def access_state(self, sub: SubscriptionModel) -> dict:
        """Whether the org may use paid features, and why."""
        now = datetime.utcnow()
        if sub.status == "active":
            return {"allowed": True, "state": "active"}
        if sub.status == "trialing":
            if sub.trial_ends_at and sub.trial_ends_at > now:
                return {"allowed": True, "state": "trial",
                        "trial_ends_at": sub.trial_ends_at.isoformat()}
            return {"allowed": False, "state": "trial_expired"}
        if sub.status == "past_due":
            if sub.grace_until and sub.grace_until > now:
                return {"allowed": True, "state": "grace",
                        "grace_until": sub.grace_until.isoformat()}
            return {"allowed": False, "state": "suspended"}
        return {"allowed": False, "state": sub.status or "none"}

    async def check_quote_allowance(self, session: AsyncSession, organization_id: str) -> dict:
        """Gate quote creation on subscription state + plan quota."""
        sub = await self.ensure_subscription(session, organization_id)
        access = self.access_state(sub)
        if not access["allowed"]:
            return {**access, "allowed": False, "reason": "subscription_inactive"}
        plan = PLANS.get(sub.plan)
        limit = plan.limits.get("quotes_per_month", 0) if plan else 0
        if sub.quotes_used_this_period >= limit:
            return {"allowed": False, "state": "quota_exceeded", "reason": "quota_exceeded"}
        return {**access, "allowed": True}

    async def record_quote_usage(self, session: AsyncSession, organization_id: str) -> None:
        sub = await self.ensure_subscription(session, organization_id)
        sub.quotes_used_this_period += 1

    # ── Checkout / portal / plan changes ───────────────────────────────

    async def create_checkout(self, session: AsyncSession, organization_id: str,
                              plan: str, billing_cycle: str, base_url: str) -> dict:
        plan_obj = PLANS.get(plan)
        if not plan_obj:
            raise BillingError(f"Unknown plan: {plan}")
        if billing_cycle not in ("monthly", "annual"):
            raise BillingError("billing_cycle must be monthly or annual")
        sub = await self.ensure_subscription(session, organization_id)

        if not sub.stripe_customer_id:
            org = await session.get(OrganizationModel, organization_id)
            sub.stripe_customer_id = self.gateway.create_customer(
                email=os.getenv("BILLING_EMAIL", f"billing+{organization_id}@ezflow.local"),
                name=org.name if org else organization_id)

        price_id = (plan_obj.stripe_price_id_monthly if billing_cycle == "monthly"
                    else plan_obj.stripe_price_id_annual)
        metadata = {"organization_id": organization_id, "plan": plan,
                    "billing_cycle": billing_cycle}
        checkout_id, url = self.gateway.create_subscription_checkout(
            stripe_customer_id=sub.stripe_customer_id,
            price_id=price_id,
            success_url=f"{base_url}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base_url}/billing/cancelled",
            metadata=metadata,
        )
        # Activation happens ONLY when the signed webhook arrives.
        sub.pending_plan = plan
        await session.flush()
        return {"checkout_url": url, "checkout_session_id": checkout_id,
                "status": "awaiting_webhook"}

    async def create_portal(self, session: AsyncSession, organization_id: str, return_url: str) -> dict:
        sub = await self.ensure_subscription(session, organization_id)
        if not sub.stripe_customer_id:
            raise BillingError("No billing profile yet — start a subscription first")
        url = self.gateway.create_portal_session(sub.stripe_customer_id, return_url)
        return {"portal_url": url}

    async def change_plan(self, session: AsyncSession, organization_id: str,
                          plan: str) -> dict:
        """Upgrade/downgrade. Stripe is asked to change the price; local
        state updates authoritatively when the subscription.updated webhook
        arrives."""
        plan_obj = PLANS.get(plan)
        if not plan_obj:
            raise BillingError(f"Unknown plan: {plan}")
        sub = await self.ensure_subscription(session, organization_id)
        if not sub.stripe_subscription_id:
            raise BillingError("No active subscription to change — use checkout")
        self.gateway.update_subscription_price(sub.stripe_subscription_id,
                                               plan_obj.stripe_price_id_monthly)
        sub.pending_plan = plan
        await session.flush()
        return {"status": "change_requested", "pending_plan": plan}

    async def cancel(self, session: AsyncSession, organization_id: str) -> dict:
        sub = await self.ensure_subscription(session, organization_id)
        if not sub.stripe_subscription_id:
            sub.cancel_at_period_end = True
            await session.flush()
            return {"status": "canceled", "effective": "immediate"}
        self.gateway.cancel_subscription_at_period_end(sub.stripe_subscription_id)
        sub.cancel_at_period_end = True
        await session.flush()
        return {"status": "cancel_scheduled", "effective": "period_end"}

    async def list_invoices(self, session: AsyncSession, organization_id: str) -> list[dict]:
        sub = await self.ensure_subscription(session, organization_id)
        if not sub.stripe_customer_id:
            return []
        return self.gateway.list_invoices(sub.stripe_customer_id)

    async def subscription_status(self, session: AsyncSession, organization_id: str) -> dict:
        sub = await self.ensure_subscription(session, organization_id)
        plan = PLANS.get(sub.plan)
        import math
        raw_limit = plan.limits.get("quotes_per_month") if plan else 0
        quotes_limit = None if (isinstance(raw_limit, float) and math.isinf(raw_limit)) else raw_limit
        return {
            "plan": sub.plan,
            "plan_name": plan.name if plan else sub.plan,
            "billing_cycle": sub.billing_cycle,
            "status": sub.status,
            "cancel_at_period_end": sub.cancel_at_period_end,
            "trial_ends_at": sub.trial_ends_at.isoformat() if sub.trial_ends_at else None,
            "grace_until": sub.grace_until.isoformat() if sub.grace_until else None,
            "current_period_end": sub.current_period_end.isoformat() if sub.current_period_end else None,
            "quotes_used_this_period": sub.quotes_used_this_period,
            "quotes_per_month_limit": quotes_limit,
            "access": self.access_state(sub),
        }

    # ── Webhook processing (authoritative + idempotent) ─────────────────

    async def handle_webhook(self, session: AsyncSession, event: dict) -> dict:
        event_id = str(event.get("id", ""))
        event_type = str(event.get("type", ""))
        if not event_id or not event_type:
            return {"status": "rejected", "reason": "malformed_event"}

        # Idempotency: claim the event id first. A duplicate insert means we
        # already processed this exact event — report and do nothing.
        try:
            async with session.begin_nested():
                session.add(WebhookEventModel(event_id=event_id, source="stripe",
                                              event_type=event_type))
                await session.flush()
        except IntegrityError:
            return {"status": "already_processed", "event_id": event_id}

        obj = (event.get("data") or {}).get("object") or {}
        try:
            result = await self._dispatch(session, event_type, obj)
        except Exception:
            logger.exception("stripe webhook processing failed event=%s type=%s",
                             event_id, event_type)
            raise

        logger.info("stripe webhook processed event=%s type=%s result=%s",
                    event_id, event_type, result.get("status"))
        return result

    async def _find_by_subscription(self, session: AsyncSession, subscription_id: str) -> Optional[SubscriptionModel]:
        if not subscription_id:
            return None
        return await session.scalar(
            select(SubscriptionModel).where(
                SubscriptionModel.stripe_subscription_id == subscription_id))

    async def _find_by_customer(self, session: AsyncSession, customer_id: str) -> Optional[SubscriptionModel]:
        if not customer_id:
            return None
        return await session.scalar(
            select(SubscriptionModel).where(
                SubscriptionModel.stripe_customer_id == customer_id))

    async def _dispatch(self, session: AsyncSession, event_type: str, obj: dict) -> dict:
        if event_type == "checkout.session.completed":
            return await self._on_checkout_completed(session, obj)
        if event_type in ("customer.subscription.updated", "customer.subscription.created"):
            return await self._on_subscription_updated(session, obj)
        if event_type == "customer.subscription.deleted":
            sub = await self._find_by_subscription(session, obj.get("id", ""))
            if sub:
                sub.status = "canceled"
                sub.cancel_at_period_end = False
                await session.flush()
                return {"status": "canceled", "organization_id": sub.organization_id}
            return {"status": "ignored", "reason": "unknown_subscription"}
        if event_type == "invoice.payment_failed":
            return await self._on_payment_failed(session, obj)
        if event_type in ("invoice.paid", "invoice.payment_succeeded"):
            return await self._on_payment_recovered(session, obj)
        return {"status": "ignored", "reason": f"unhandled_type:{event_type}"}

    async def _on_checkout_completed(self, session: AsyncSession, obj: dict) -> dict:
        meta = obj.get("metadata") or {}
        org_id = meta.get("organization_id")
        sub = None
        if org_id:
            sub = await self.ensure_subscription(session, org_id)
        else:
            sub = await self._find_by_customer(session, obj.get("customer", ""))
        if not sub:
            return {"status": "ignored", "reason": "unknown_organization"}

        plan = meta.get("plan") or sub.pending_plan or "pro"
        sub.stripe_customer_id = obj.get("customer") or sub.stripe_customer_id
        sub.stripe_subscription_id = obj.get("subscription") or sub.stripe_subscription_id
        sub.plan = plan
        sub.billing_cycle = meta.get("billing_cycle", sub.billing_cycle)
        sub.status = "active"
        sub.pending_plan = None
        sub.cancel_at_period_end = False
        sub.trial_ends_at = None
        sub.grace_until = None
        await session.flush()
        return {"status": "activated", "organization_id": sub.organization_id, "plan": plan}

    async def _on_subscription_updated(self, session: AsyncSession, obj: dict) -> dict:
        sub = await self._find_by_subscription(session, obj.get("id", ""))
        if not sub:
            sub = await self._find_by_customer(session, obj.get("customer", ""))
        if not sub:
            return {"status": "ignored", "reason": "unknown_subscription"}

        stripe_status = obj.get("status", "")
        new_status = _STATUS_MAP.get(stripe_status)
        if new_status:
            sub.status = new_status
        if new_status == "past_due" and not sub.grace_until:
            sub.grace_until = datetime.utcnow() + timedelta(days=GRACE_DAYS)
        if new_status in ("active", "trialing"):
            sub.grace_until = None
            if sub.pending_plan:
                sub.plan = sub.pending_plan
                sub.pending_plan = None
        period_end = obj.get("current_period_end")
        if period_end:
            sub.current_period_end = datetime.utcfromtimestamp(int(period_end))
        await session.flush()
        return {"status": sub.status, "organization_id": sub.organization_id}

    async def _on_payment_failed(self, session: AsyncSession, obj: dict) -> dict:
        sub = await self._find_by_subscription(session, obj.get("subscription", "")) \
            or await self._find_by_customer(session, obj.get("customer", ""))
        if not sub:
            return {"status": "ignored", "reason": "unknown_subscription"}
        sub.status = "past_due"
        sub.grace_until = datetime.utcnow() + timedelta(days=GRACE_DAYS)
        await session.flush()
        logger.warning("billing event=payment_failed org=%s grace_until=%s",
                       sub.organization_id, sub.grace_until)
        return {"status": "past_due", "organization_id": sub.organization_id,
                "grace_until": sub.grace_until.isoformat()}

    async def _on_payment_recovered(self, session: AsyncSession, obj: dict) -> dict:
        sub = await self._find_by_subscription(session, obj.get("subscription", "")) \
            or await self._find_by_customer(session, obj.get("customer", ""))
        if not sub:
            return {"status": "ignored", "reason": "unknown_subscription"}
        sub.status = "active"
        sub.grace_until = None
        await session.flush()
        return {"status": "active", "organization_id": sub.organization_id}


_service: Optional[BillingService] = None


def get_billing_service() -> BillingService:
    global _service
    if _service is None:
        _service = BillingService()
    return _service


def set_billing_service(service: Optional[BillingService]) -> None:
    """Dependency injection for tests."""
    global _service
    _service = service
