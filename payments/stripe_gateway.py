"""Stripe gateway abstraction.

`RealStripeGateway` talks to Stripe. `MockStripeGateway` implements the same
surface in-memory so the subscription lifecycle, webhook idempotency, and
failure paths are fully testable without network access or keys.

Nothing here trusts client-side claims about payment — activation always
arrives via a signed webhook processed by BillingService.
"""

import logging
import os
import secrets
from datetime import datetime, timedelta
from typing import Optional, Protocol

logger = logging.getLogger("stripe_gateway")


class GatewayError(RuntimeError):
    pass


class StripeGatewayProtocol(Protocol):
    def create_customer(self, email: str, name: str) -> str: ...
    def create_subscription_checkout(self, *, stripe_customer_id: str, price_id: str,
                                     success_url: str, cancel_url: str,
                                     metadata: dict) -> tuple[str, str]: ...
    def create_portal_session(self, stripe_customer_id: str, return_url: str) -> str: ...
    def update_subscription_price(self, subscription_id: str, price_id: str) -> None: ...
    def cancel_subscription_at_period_end(self, subscription_id: str) -> None: ...
    def list_invoices(self, stripe_customer_id: str, limit: int = 20) -> list[dict]: ...
    def construct_event(self, payload: bytes, signature: str) -> dict: ...


class RealStripeGateway:
    """Thin async-friendly wrappers over the stripe SDK (sync calls)."""

    def __init__(self):
        import stripe as stripe_lib
        self.stripe = stripe_lib
        api_key = os.getenv("STRIPE_SECRET_KEY", "")
        if not api_key:
            raise GatewayError("STRIPE_SECRET_KEY is not configured")
        stripe_lib.api_key = api_key
        self.webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")

    def create_customer(self, email: str, name: str) -> str:
        customer = self.stripe.Customer.create(email=email, name=name,
                                               metadata={"product": "ezflow"})
        return customer.id

    def create_subscription_checkout(self, *, stripe_customer_id: str, price_id: str,
                                     success_url: str, cancel_url: str,
                                     metadata: dict) -> tuple[str, str]:
        session = self.stripe.checkout.Session.create(
            mode="subscription",
            customer=stripe_customer_id,
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            metadata=metadata,
            subscription_data={"metadata": metadata},
        )
        return session.id, session.url

    def create_portal_session(self, stripe_customer_id: str, return_url: str) -> str:
        session = self.stripe.billing_portal.Session.create(
            customer=stripe_customer_id, return_url=return_url)
        return session.url

    def update_subscription_price(self, subscription_id: str, price_id: str) -> None:
        self.stripe.Subscription.modify(subscription_id,
                                        items=[{"price": price_id}],
                                        proration_behavior="create_prorations")

    def cancel_subscription_at_period_end(self, subscription_id: str) -> None:
        self.stripe.Subscription.modify(subscription_id, cancel_at_period_end=True)

    def list_invoices(self, stripe_customer_id: str, limit: int = 20) -> list[dict]:
        invoices = self.stripe.Invoice.list(customer=stripe_customer_id, limit=limit)
        return [{
            "invoice_id": inv.id,
            "amount_due": inv.amount_due / 100 if inv.amount_due else 0,
            "currency": inv.currency,
            "status": inv.status,
            "created_at": datetime.utcfromtimestamp(inv.created).isoformat(),
            "hosted_invoice_url": inv.hosted_invoice_url,
        } for inv in invoices.auto_paging_iter()]

    def construct_event(self, payload: bytes, signature: str) -> dict:
        if not self.webhook_secret:
            raise GatewayError("STRIPE_WEBHOOK_SECRET is not configured")
        return self.stripe.Webhook.construct_event(payload, signature, self.webhook_secret)


class MockStripeGateway:
    """Deterministic in-memory Stripe for tests and local development.

    Events produced here have the exact shape of Stripe webhook payloads and
    must be delivered through BillingService.handle_webhook to take effect.
    """

    def __init__(self):
        self.customers: dict[str, dict] = {}
        self.checkout_sessions: dict[str, dict] = {}
        self.subscriptions: dict[str, dict] = {}
        self.invoices: list[dict] = []

    def create_customer(self, email: str, name: str) -> str:
        cid = f"cus_mock_{secrets.token_hex(6)}"
        self.customers[cid] = {"email": email, "name": name}
        return cid

    def create_subscription_checkout(self, *, stripe_customer_id: str, price_id: str,
                                     success_url: str, cancel_url: str,
                                     metadata: dict) -> tuple[str, str]:
        session_id = f"cs_mock_{secrets.token_hex(8)}"
        self.checkout_sessions[session_id] = {
            "customer": stripe_customer_id,
            "price_id": price_id,
            "metadata": metadata,
        }
        return session_id, f"https://mock.stripe.local/checkout/{session_id}"

    def create_portal_session(self, stripe_customer_id: str, return_url: str) -> str:
        return f"https://mock.stripe.local/porta_{secrets.token_hex(6)}"

    def update_subscription_price(self, subscription_id: str, price_id: str) -> None:
        sub = self.subscriptions.get(subscription_id)
        if sub:
            sub["price_id"] = price_id

    def cancel_subscription_at_period_end(self, subscription_id: str) -> None:
        sub = self.subscriptions.get(subscription_id)
        if sub:
            sub["cancel_at_period_end"] = True

    def list_invoices(self, stripe_customer_id: str, limit: int = 20) -> list[dict]:
        return [dict(inv) for inv in self.invoices
                if inv["customer"] == stripe_customer_id][:limit]

    def construct_event(self, payload: bytes, signature: str) -> dict:
        import json
        # Dev/test only: unsigned payloads accepted solely because this
        # gateway is never configured in production.
        event = json.loads(payload)
        if not isinstance(event, dict) or "id" not in event or "type" not in event:
            raise GatewayError("Malformed mock event")
        return event

    # ── Test-side event factory helpers (never called by production code) ──

    def event(self, event_type: str, obj: dict) -> dict:
        return {"id": f"evt_mock_{secrets.token_hex(8)}", "type": event_type,
                "data": {"object": obj}}

    def checkout_completed_event(self, session_id: str) -> dict:
        sess = self.checkout_sessions[session_id]
        sub_id = f"sub_mock_{secrets.token_hex(6)}"
        self.subscriptions[sub_id] = {
            "id": sub_id, "customer": sess["customer"], "price_id": sess["price_id"],
            "status": "active",
            "current_period_end": int((datetime.utcnow() + timedelta(days=30)).timestamp()),
        }
        return self.event("checkout.session.completed", {
            "id": session_id, "customer": sess["customer"],
            "subscription": sub_id, "metadata": sess["metadata"],
        })

    def subscription_event(self, event_type: str, subscription_id: str,
                           status: str = "active") -> dict:
        sub = self.subscriptions.get(subscription_id)
        if sub:
            sub["status"] = status
        return self.event(event_type, {"id": subscription_id, "customer": sub["customer"] if sub else None,
                                       "status": status,
                                       "current_period_end": sub.get("current_period_end") if sub else None})

    def invoice_failed_event(self, subscription_id: str) -> dict:
        sub = self.subscriptions.get(subscription_id)
        return self.event("invoice.payment_failed", {
            "id": f"in_mock_{secrets.token_hex(5)}",
            "subscription": subscription_id,
            "customer": sub["customer"] if sub else None,
        })

    def invoice_paid_event(self, subscription_id: str) -> dict:
        sub = self.subscriptions.get(subscription_id)
        invoice = {
            "invoice_id": f"in_mock_{secrets.token_hex(5)}",
            "customer": sub["customer"] if sub else None,
            "amount_due": 49.0,
            "currency": "usd",
            "status": "paid",
            "created_at": datetime.utcnow().isoformat(),
            "hosted_invoice_url": None,
        }
        self.invoices.append(invoice)
        return self.event("invoice.paid", {
            "id": invoice["invoice_id"], "subscription": subscription_id,
            "customer": invoice["customer"], "status": "paid",
        })


_gateway: Optional[StripeGatewayProtocol] = None


def get_gateway() -> StripeGatewayProtocol:
    """Real gateway when Stripe is configured; mock gateway when explicitly
    enabled (tests / offline dev); error otherwise."""
    global _gateway
    if _gateway is not None:
        return _gateway
    if os.getenv("STRIPE_MODE", "").lower() == "mock":
        _gateway = MockStripeGateway()
    elif os.getenv("STRIPE_SECRET_KEY", "").strip():
        _gateway = RealStripeGateway()
    else:
        raise GatewayError("Stripe is not configured (set STRIPE_SECRET_KEY or STRIPE_MODE=mock)")
    return _gateway


def set_gateway(gateway: Optional[StripeGatewayProtocol]) -> None:
    """Dependency injection for tests."""
    global _gateway
    _gateway = gateway
