"""Stripe payment processing for QuoteFlow AI."""

import os
import logging
from typing import Optional, Dict, List
from dataclasses import dataclass
from datetime import datetime

import stripe as stripe_lib

logger = logging.getLogger("stripe_client")

# Initialize Stripe
stripe_lib.api_key = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")


@dataclass
class PaymentIntent:
    id: str
    amount: float
    currency: str
    status: str
    customer_email: str
    quote_id: str
    metadata: Dict
    client_secret: Optional[str]
    created_at: str


@dataclass
class Subscription:
    id: str
    customer_id: str
    status: str
    plan: str
    current_period_start: str
    current_period_end: str
    cancel_at_period_end: bool


class StripeClient:
    """Handles all Stripe operations for QuoteFlow AI."""

    def __init__(self):
        self.stripe = stripe_lib
        self.webhook_secret = STRIPE_WEBHOOK_SECRET

    # ─── CUSTOMER PAYMENTS (Deposits & Full Payments) ─────────────

    async def create_quote_payment(
        self,
        quote_id: str,
        amount: float,
        customer_email: str,
        description: str,
        metadata: Optional[Dict] = None,
        capture_method: str = "automatic",
    ) -> PaymentIntent:
        """Create a payment intent for a quote acceptance."""

        try:
            intent = self.stripe.PaymentIntent.create(
                amount=int(amount * 100),  # Stripe uses cents
                currency="usd",
                customer=await self._get_or_create_customer(customer_email),
                description=description,
                metadata={
                    "quote_id": quote_id,
                    "type": "quote_payment",
                    **(metadata or {}),
                },
                receipt_email=customer_email,
                automatic_payment_methods={"enabled": True},
                capture_method=capture_method,
            )

            return PaymentIntent(
                id=intent.id,
                amount=amount,
                currency="usd",
                status=intent.status,
                customer_email=customer_email,
                quote_id=quote_id,
                metadata=intent.metadata,
                client_secret=intent.client_secret,
                created_at=datetime.utcnow().isoformat(),
            )
        except Exception as e:
            logger.error(f"Payment intent creation failed: {e}")
            raise

    async def create_deposit_intent(
        self,
        quote_id: str,
        total_amount: float,
        deposit_percent: float = 0.5,
        customer_email: str = "",
        description: str = "",
    ) -> PaymentIntent:
        """Create a 50% deposit payment intent."""
        deposit_amount = round(total_amount * deposit_percent, 2)

        return await self.create_quote_payment(
            quote_id=quote_id,
            amount=deposit_amount,
            customer_email=customer_email,
            description=f"Deposit for {description}",
            metadata={
                "payment_type": "deposit",
                "total_amount": total_amount,
                "deposit_percent": deposit_percent,
            },
        )

    async def capture_payment(self, payment_intent_id: str) -> PaymentIntent:
        """Capture an authorized payment (for manual capture)."""
        try:
            intent = self.stripe.PaymentIntent.capture(payment_intent_id)
            return PaymentIntent(
                id=intent.id,
                amount=intent.amount / 100,
                currency=intent.currency,
                status=intent.status,
                customer_email="",
                quote_id=intent.metadata.get("quote_id", ""),
                metadata=intent.metadata,
                client_secret=None,
                created_at=datetime.utcnow().isoformat(),
            )
        except Exception as e:
            logger.error(f"Payment capture failed: {e}")
            raise

    async def refund_payment(
        self,
        payment_intent_id: str,
        amount: Optional[float] = None,
        reason: str = "requested_by_customer",
    ) -> Dict:
        """Refund a payment (partial or full)."""
        try:
            refund_data = {"payment_intent": payment_intent_id, "reason": reason}
            if amount:
                refund_data["amount"] = int(amount * 100)

            refund = self.stripe.Refund.create(**refund_data)
            return {
                "id": refund.id,
                "status": refund.status,
                "amount": refund.amount / 100,
                "reason": refund.reason,
            }
        except Exception as e:
            logger.error(f"Refund failed: {e}")
            raise

    # ─── SAAS SUBSCRIPTIONS ───────────────────────────────────────

    async def create_subscription(
        self,
        customer_email: str,
        price_id: str,
        trial_days: int = 14,
    ) -> Subscription:
        """Create a SaaS subscription for a contractor."""

        try:
            customer = await self._get_or_create_customer(customer_email)

            subscription = self.stripe.Subscription.create(
                customer=customer,
                items=[{"price": price_id}],
                trial_period_days=trial_days,
                payment_behavior="default_incomplete",
                expand=["latest_invoice.payment_intent"],
                metadata={"type": "contractor_subscription"},
            )

            return Subscription(
                id=subscription.id,
                customer_id=customer,
                status=subscription.status,
                plan=price_id,
                current_period_start=datetime.fromtimestamp(
                    subscription.current_period_start
                ).isoformat(),
                current_period_end=datetime.fromtimestamp(
                    subscription.current_period_end
                ).isoformat(),
                cancel_at_period_end=subscription.cancel_at_period_end,
            )
        except Exception as e:
            logger.error(f"Subscription creation failed: {e}")
            raise

    async def cancel_subscription(
        self,
        subscription_id: str,
        at_period_end: bool = True,
    ) -> Dict:
        """Cancel a subscription."""
        try:
            if at_period_end:
                sub = self.stripe.Subscription.modify(
                    subscription_id,
                    cancel_at_period_end=True,
                )
            else:
                sub = self.stripe.Subscription.delete(subscription_id)

            return {
                "id": sub.id,
                "status": sub.status,
                "cancel_at_period_end": sub.cancel_at_period_end,
            }
        except Exception as e:
            logger.error(f"Subscription cancellation failed: {e}")
            raise

    # ─── WEBHOOK HANDLING ───────────────────────────────────────

    def verify_webhook(self, payload: bytes, signature: str) -> Dict:
        """Verify Stripe webhook signature."""
        try:
            event = self.stripe.Webhook.construct_event(
                payload, signature, self.webhook_secret
            )
            return event
        except ValueError as e:
            logger.error(f"Invalid webhook payload: {e}")
            raise
        except self.stripe.error.SignatureVerificationError as e:
            logger.error(f"Invalid webhook signature: {e}")
            raise

    def handle_webhook_event(self, event: Dict) -> Dict:
        """Process Stripe webhook events."""
        event_type = event["type"]
        data = event["data"]["object"]

        handlers = {
            "payment_intent.succeeded": self._on_payment_success,
            "payment_intent.payment_failed": self._on_payment_failed,
            "invoice.paid": self._on_invoice_paid,
            "invoice.payment_failed": self._on_invoice_failed,
            "customer.subscription.created": self._on_subscription_created,
            "customer.subscription.deleted": self._on_subscription_cancelled,
        }

        handler = handlers.get(event_type, self._on_unknown_event)
        return handler(data)

    def _on_payment_success(self, data: Dict) -> Dict:
        quote_id = data.get("metadata", {}).get("quote_id", "")
        logger.info(f"Payment succeeded for quote {quote_id}")
        return {
            "event": "payment_success",
            "quote_id": quote_id,
            "payment_intent_id": data["id"],
            "amount": data["amount"] / 100,
        }

    def _on_payment_failed(self, data: Dict) -> Dict:
        quote_id = data.get("metadata", {}).get("quote_id", "")
        logger.warning(f"Payment failed for quote {quote_id}")
        return {
            "event": "payment_failed",
            "quote_id": quote_id,
            "payment_intent_id": data["id"],
        }

    def _on_invoice_paid(self, data: Dict) -> Dict:
        logger.info(f"Invoice paid: {data['id']}")
        return {"event": "invoice_paid", "invoice_id": data["id"]}

    def _on_invoice_failed(self, data: Dict) -> Dict:
        logger.warning(f"Invoice payment failed: {data['id']}")
        return {"event": "invoice_failed", "invoice_id": data["id"]}

    def _on_subscription_created(self, data: Dict) -> Dict:
        logger.info(f"Subscription created: {data['id']}")
        return {"event": "subscription_created", "subscription_id": data["id"]}

    def _on_subscription_cancelled(self, data: Dict) -> Dict:
        logger.info(f"Subscription cancelled: {data['id']}")
        return {"event": "subscription_cancelled", "subscription_id": data["id"]}

    def _on_unknown_event(self, data: Dict) -> Dict:
        return {"event": "unknown", "message": "Unhandled event type"}

    # ─── HELPERS ─────────────────────────────────────────────────

    async def _get_or_create_customer(self, email: str) -> str:
        """Get existing Stripe customer or create new one."""
        try:
            customers = self.stripe.Customer.list(email=email, limit=1)
            if customers.data:
                return customers.data[0].id

            customer = self.stripe.Customer.create(email=email)
            return customer.id
        except Exception as e:
            logger.error(f"Customer lookup/creation failed: {e}")
            raise

    async def create_checkout_session(
        self,
        quote_id: str,
        amount: float,
        customer_email: str,
        success_url: str,
        cancel_url: str,
    ) -> Dict:
        """Create a Stripe Checkout session for quote payment."""
        try:
            session = self.stripe.checkout.Session.create(
                payment_method_types=["card"],
                line_items=[{
                    "price_data": {
                        "currency": "usd",
                        "product_data": {
                            "name": f"Quote #{quote_id}",
                            "description": "Service deposit",
                        },
                        "unit_amount": int(amount * 100),
                    },
                    "quantity": 1,
                }],
                mode="payment",
                success_url=success_url,
                cancel_url=cancel_url,
                customer_email=customer_email,
                metadata={"quote_id": quote_id},
            )
            return {
                "session_id": session.id,
                "url": session.url,
                "quote_id": quote_id,
            }
        except Exception as e:
            logger.error(f"Checkout session creation failed: {e}")
            raise


# Singleton
_stripe_client = None

def get_stripe_client() -> StripeClient:
    global _stripe_client
    if _stripe_client is None:
        _stripe_client = StripeClient()
    return _stripe_client
