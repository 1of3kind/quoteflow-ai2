"""SaaS billing and subscription management for contractors."""

import os
import logging
from typing import Dict, List, Optional
from dataclasses import dataclass
from datetime import datetime, timedelta

from payments.stripe_client import get_stripe_client, Subscription

logger = logging.getLogger("billing")


@dataclass
class Plan:
    id: str
    name: str
    price_monthly: float
    price_annual: float
    stripe_price_id_monthly: str
    stripe_price_id_annual: str
    features: List[str]
    limits: Dict


# QuoteFlow AI Pricing Plans
PLANS = {
    "starter": Plan(
        id="starter",
        name="Starter",
        price_monthly=0,
        price_annual=0,
        stripe_price_id_monthly="price_free",
        stripe_price_id_annual="price_free",
        features=[
            "10 quotes per month",
            "1 trade",
            "SMS quotes only",
            "Basic analytics",
        ],
        limits={
            "quotes_per_month": 10,
            "trades": 1,
            "team_members": 1,
            "storage_mb": 100,
        },
    ),
    "pro": Plan(
        id="pro",
        name="Pro",
        price_monthly=49,
        price_annual=39,
        stripe_price_id_monthly=os.getenv("STRIPE_PRICE_PRO_MONTHLY", "price_pro_monthly"),
        stripe_price_id_annual=os.getenv("STRIPE_PRICE_PRO_ANNUAL", "price_pro_annual"),
        features=[
            "Unlimited quotes",
            "All trades",
            "SMS + Email + Voice",
            "Materials ordering",
            "Advanced analytics",
            "3 team members",
        ],
        limits={
            "quotes_per_month": float('inf'),
            "trades": 5,
            "team_members": 3,
            "storage_mb": 1000,
        },
    ),
    "business": Plan(
        id="business",
        name="Business",
        price_monthly=99,
        price_annual=79,
        stripe_price_id_monthly=os.getenv("STRIPE_PRICE_BUSINESS_MONTHLY", "price_business_monthly"),
        stripe_price_id_annual=os.getenv("STRIPE_PRICE_BUSINESS_ANNUAL", "price_business_annual"),
        features=[
            "Everything in Pro",
            "White-label branding",
            "API access",
            "Priority support",
            "Custom integrations",
            "Unlimited team members",
        ],
        limits={
            "quotes_per_month": float('inf'),
            "trades": 10,
            "team_members": float('inf'),
            "storage_mb": 10000,
        },
    ),
}


@dataclass
class ContractorAccount:
    contractor_id: str
    email: str
    business_name: str
    phone: str
    plan: str
    stripe_customer_id: Optional[str]
    stripe_subscription_id: Optional[str]
    quotes_used_this_month: int
    trades_enabled: List[str]
    created_at: str
    trial_ends_at: Optional[str]
    is_active: bool


class BillingManager:
    """Manages contractor subscriptions, usage, and plan limits."""

    def __init__(self):
        self.stripe = get_stripe_client()
        self.accounts: Dict[str, ContractorAccount] = {}

    def create_account(
        self,
        contractor_id: str,
        email: str,
        business_name: str,
        phone: str,
        plan: str = "starter",
    ) -> ContractorAccount:
        """Create a new contractor account."""

        trial_end = (datetime.utcnow() + timedelta(days=14)).isoformat()

        account = ContractorAccount(
            contractor_id=contractor_id,
            email=email,
            business_name=business_name,
            phone=phone,
            plan=plan,
            stripe_customer_id=None,
            stripe_subscription_id=None,
            quotes_used_this_month=0,
            trades_enabled=[],
            created_at=datetime.utcnow().isoformat(),
            trial_ends_at=trial_end,
            is_active=True,
        )

        self.accounts[contractor_id] = account
        logger.info(f"Created account for {business_name} on {plan} plan")
        return account

    async def upgrade_plan(
        self,
        contractor_id: str,
        new_plan: str,
        billing_cycle: str = "monthly",
    ) -> Dict:
        """Upgrade contractor to a paid plan."""

        account = self.accounts.get(contractor_id)
        if not account:
            raise ValueError(f"Account {contractor_id} not found")

        plan = PLANS.get(new_plan)
        if not plan:
            raise ValueError(f"Invalid plan: {new_plan}")

        price_id = (
            plan.stripe_price_id_monthly
            if billing_cycle == "monthly"
            else plan.stripe_price_id_annual
        )

        # Create Stripe subscription
        subscription = await self.stripe.create_subscription(
            customer_email=account.email,
            price_id=price_id,
            trial_days=0 if account.trial_ends_at and datetime.fromisoformat(account.trial_ends_at) < datetime.utcnow() else 14,
        )

        account.plan = new_plan
        account.stripe_subscription_id = subscription.id
        account.trial_ends_at = None

        logger.info(f"Upgraded {contractor_id} to {new_plan}")
        return {
            "status": "upgraded",
            "plan": new_plan,
            "subscription_id": subscription.id,
            "billing_cycle": billing_cycle,
        }

    def can_create_quote(self, contractor_id: str) -> bool:
        """Check if contractor can create another quote this month."""
        account = self.accounts.get(contractor_id)
        if not account or not account.is_active:
            return False

        # Check trial status
        if account.trial_ends_at:
            if datetime.fromisoformat(account.trial_ends_at) > datetime.utcnow():
                return True  # Still in trial

        plan = PLANS.get(account.plan)
        if not plan:
            return False

        return account.quotes_used_this_month < plan.limits["quotes_per_month"]

    def record_quote(self, contractor_id: str) -> bool:
        """Record a quote creation and check limits."""
        account = self.accounts.get(contractor_id)
        if not account:
            return False

        if not self.can_create_quote(contractor_id):
            return False

        account.quotes_used_this_month += 1
        return True

    def reset_monthly_usage(self, contractor_id: str):
        """Reset monthly quote counter (call at start of month)."""
        account = self.accounts.get(contractor_id)
        if account:
            account.quotes_used_this_month = 0
            logger.info(f"Reset usage for {contractor_id}")

    def get_account_status(self, contractor_id: str) -> Dict:
        """Get full account status for dashboard."""
        account = self.accounts.get(contractor_id)
        if not account:
            return {"error": "Account not found"}

        plan = PLANS.get(account.plan)

        return {
            "contractor_id": account.contractor_id,
            "business_name": account.business_name,
            "plan": account.plan,
            "plan_name": plan.name if plan else "Unknown",
            "is_active": account.is_active,
            "in_trial": (
                account.trial_ends_at is not None
                and datetime.fromisoformat(account.trial_ends_at) > datetime.utcnow()
            ),
            "trial_ends_at": account.trial_ends_at,
            "quotes_used": account.quotes_used_this_month,
            "quotes_limit": plan.limits["quotes_per_month"] if plan else 0,
            "quotes_remaining": (
                plan.limits["quotes_per_month"] - account.quotes_used_this_month
                if plan and plan.limits["quotes_per_month"] != float('inf')
                else "Unlimited"
            ),
            "trades_enabled": account.trades_enabled,
            "trades_limit": plan.limits["trades"] if plan else 0,
        }

    def enable_trade(self, contractor_id: str, trade: str) -> bool:
        """Enable a trade for a contractor (if plan allows)."""
        account = self.accounts.get(contractor_id)
        if not account:
            return False

        plan = PLANS.get(account.plan)
        if not plan:
            return False

        if len(account.trades_enabled) >= plan.limits["trades"]:
            logger.warning(f"Trade limit reached for {contractor_id}")
            return False

        if trade not in account.trades_enabled:
            account.trades_enabled.append(trade)
            logger.info(f"Enabled {trade} for {contractor_id}")

        return True


# Singleton
_billing_manager = BillingManager()

def get_billing_manager() -> BillingManager:
    return _billing_manager
