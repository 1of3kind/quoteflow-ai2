"""GATE 5 — Stripe billing lifecycle, webhook authority, idempotency."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from core import database as db
from core.database import SubscriptionModel
from payments.billing import BillingService, PLANS
from payments.stripe_gateway import MockStripeGateway


@pytest.fixture
def billing():
    """Point the app's singletons at one shared mock gateway so events
    produced in tests flow through the same gateway the API used."""
    from payments.stripe_gateway import set_gateway
    from payments.billing import set_billing_service
    gateway = MockStripeGateway()
    set_gateway(gateway)
    service = BillingService()
    set_billing_service(service)
    return service


async def _get_sub(session, org_id) -> SubscriptionModel:
    return (await session.execute(
        select(SubscriptionModel).where(
            SubscriptionModel.organization_id == org_id))).scalar_one()


class TestTrial:
    async def test_signup_grants_14_day_trial(self, client, org_a):
        r = await client.get("/billing/subscription", headers=org_a["headers"])
        sub = r.json()
        assert sub["status"] == "trialing"
        assert sub["access"]["state"] == "trial"
        assert sub["access"]["allowed"] is True

    async def test_expired_trial_blocks_quotes(self, client, org_a, billing):
        async with db.AsyncSessionLocal() as s:
            sub = await _get_sub(s, org_a["org_id"])
            sub.trial_ends_at = datetime.utcnow() - timedelta(days=1)
            await s.commit()
        cust_id = (await client.post("/org/customers", json={"name": "X"},
                                     headers=org_a["headers"])).json()["id"]
        r = await client.post("/quotes", json={
            "org_customer_id": cust_id, "trade": "roofing",
            "labor_lines": [{"skill_name": "R", "workers": 1, "hours": 1, "hourly_rate": 10}],
        }, headers=org_a["headers"])
        assert r.status_code == 402
        assert r.json()["state"] == "trial_expired"


class TestCheckoutLifecycle:
    async def test_browser_cannot_activate_subscription(self, client, org_a, billing):
        """Choosing a plan creates checkout — but grants NOTHING until the
        signed webhook arrives (the browser never tells us someone paid)."""
        r = await client.post("/billing/checkout",
                              json={"plan": "pro", "billing_cycle": "monthly"},
                              headers=org_a["headers"])
        assert r.status_code == 200
        assert r.json()["status"] == "awaiting_webhook"
        sub = (await client.get("/billing/subscription", headers=org_a["headers"])).json()
        assert sub["status"] == "trialing"          # NOT active yet

    async def test_webhook_activates_subscription(self, client, org_a, billing):
        checkout = (await client.post("/billing/checkout",
                                      json={"plan": "pro", "billing_cycle": "monthly"},
                                      headers=org_a["headers"])).json()
        session_id = checkout["checkout_session_id"]

        event = billing.gateway.checkout_completed_event(session_id)
        async with db.AsyncSessionLocal() as s:
            result = await billing.handle_webhook(s, event)
            await s.commit()
        assert result["status"] == "activated"

        sub = (await client.get("/billing/subscription", headers=org_a["headers"])).json()
        assert sub["status"] == "active"
        assert sub["plan"] == "pro"

    async def test_webhook_idempotency(self, client, org_a, billing):
        checkout = (await client.post("/billing/checkout",
                                      json={"plan": "pro", "billing_cycle": "monthly"},
                                      headers=org_a["headers"])).json()
        event = billing.gateway.checkout_completed_event(checkout["checkout_session_id"])
        async with db.AsyncSessionLocal() as s:
            first = await billing.handle_webhook(s, event)
            await s.commit()
        async with db.AsyncSessionLocal() as s:
            second = await billing.handle_webhook(s, event)
            await s.commit()
        assert first["status"] == "activated"
        assert second["status"] == "already_processed"

    async def test_tampered_signature_rejected(self, client, monkeypatch):
        """With the real Stripe gateway, a bad signature never reaches
        processing — the endpoint must reject with 400."""
        class FailingRealGateway:
            def construct_event(self, payload, signature):
                from payments.stripe_gateway import GatewayError
                raise GatewayError("Invalid webhook signature")

        monkeypatch.setattr("payments.stripe_gateway.get_gateway",
                            lambda: FailingRealGateway())
        r = await client.post("/webhook/stripe", content=b'{"id":"evt_x","type":"x"}',
                              headers={"stripe-signature": "bogus"})
        assert r.status_code == 400


class TestFailedPayment:
    async def test_payment_failed_sets_past_due_with_grace(self, client, org_a, billing):
        checkout = (await client.post("/billing/checkout",
                                      json={"plan": "pro", "billing_cycle": "monthly"},
                                      headers=org_a["headers"])).json()
        event = billing.gateway.checkout_completed_event(checkout["checkout_session_id"])
        async with db.AsyncSessionLocal() as s:
            await billing.handle_webhook(s, event)
            await s.commit()
            sub = await _get_sub(s, org_a["org_id"])
            sub_id = sub.stripe_subscription_id

        failed = billing.gateway.invoice_failed_event(sub_id)
        async with db.AsyncSessionLocal() as s:
            r = await billing.handle_webhook(s, failed)
            await s.commit()
        assert r["status"] == "past_due"

        sub = (await client.get("/billing/subscription", headers=org_a["headers"])).json()
        assert sub["status"] == "past_due"
        assert sub["access"]["state"] == "grace"       # grace period active
        assert sub["access"]["allowed"] is True

    async def test_access_restricted_after_grace_expires(self, org_a, billing):
        async with db.AsyncSessionLocal() as s:
            sub = await _get_sub(s, org_a["org_id"])
            sub.status = "past_due"
            sub.grace_until = datetime.utcnow() - timedelta(days=1)
            await s.commit()
            state = billing.access_state(sub)
        assert state["allowed"] is False
        assert state["state"] == "suspended"

    async def test_payment_recovery_reactivates(self, client, org_a, billing):
        checkout = (await client.post("/billing/checkout",
                                      json={"plan": "pro", "billing_cycle": "monthly"},
                                      headers=org_a["headers"])).json()
        event = billing.gateway.checkout_completed_event(checkout["checkout_session_id"])
        async with db.AsyncSessionLocal() as s:
            await billing.handle_webhook(s, event)
            await s.commit()
            sub = await _get_sub(s, org_a["org_id"])
            sub_id = sub.stripe_subscription_id
            sub.status = "past_due"
            await s.commit()

        paid = billing.gateway.invoice_paid_event(sub_id)
        async with db.AsyncSessionLocal() as s:
            r = await billing.handle_webhook(s, paid)
            await s.commit()
        assert r["status"] == "active"


class TestUpgradeDowngradeCancel:
    async def test_plan_change_requests_update(self, client, org_a, billing):
        checkout = (await client.post("/billing/checkout",
                                      json={"plan": "pro", "billing_cycle": "monthly"},
                                      headers=org_a["headers"])).json()
        event = billing.gateway.checkout_completed_event(checkout["checkout_session_id"])
        async with db.AsyncSessionLocal() as s:
            await billing.handle_webhook(s, event)
            await s.commit()

        r = await client.post("/billing/change-plan", json={"plan": "business"},
                              headers=org_a["headers"])
        assert r.status_code == 200
        assert r.json()["pending_plan"] == "business"
        # Authoritative change happens on subscription.updated webhook
        async with db.AsyncSessionLocal() as s:
            sub = await _get_sub(s, org_a["org_id"])
            update_event = billing.gateway.subscription_event(
                "customer.subscription.updated", sub.stripe_subscription_id)
            await billing.handle_webhook(s, update_event)
            await s.commit()
        sub_now = (await client.get("/billing/subscription", headers=org_a["headers"])).json()
        assert sub_now["plan"] == "business"

    async def test_cancellation(self, client, org_a, billing):
        checkout = (await client.post("/billing/checkout",
                                      json={"plan": "pro", "billing_cycle": "monthly"},
                                      headers=org_a["headers"])).json()
        event = billing.gateway.checkout_completed_event(checkout["checkout_session_id"])
        async with db.AsyncSessionLocal() as s:
            await billing.handle_webhook(s, event)
            await s.commit()
            sub = await _get_sub(s, org_a["org_id"])
            billing.gateway.subscriptions[sub.stripe_subscription_id]["current_period_end"] = 9999999999

        r = await client.post("/billing/cancel", headers=org_a["headers"])
        assert r.status_code == 200
        assert r.json()["status"] == "cancel_scheduled"
        sub = (await client.get("/billing/subscription", headers=org_a["headers"])).json()
        assert sub["cancel_at_period_end"] is True
        assert sub["status"] == "active"   # still active until period end

        # Simulate Stripe confirming the cancellation
        async with db.AsyncSessionLocal() as s:
            sub_row = await _get_sub(s, org_a["org_id"])
            del_event = billing.gateway.subscription_event(
                "customer.subscription.deleted", sub_row.stripe_subscription_id)
            await billing.handle_webhook(s, del_event)
            await s.commit()
        final = (await client.get("/billing/subscription", headers=org_a["headers"])).json()
        assert final["status"] == "canceled"


class TestQuota:
    async def test_starter_plan_quota_enforced(self, org_a, billing):
        async with db.AsyncSessionLocal() as s:
            sub = await _get_sub(s, org_a["org_id"])
            assert PLANS["starter"].limits["quotes_per_month"] == 10
            for _ in range(10):
                await billing.record_quote_usage(s, org_a["org_id"])
                await s.flush()
            allowance = await billing.check_quote_allowance(s, org_a["org_id"])
            await s.commit()
        assert allowance["allowed"] is False
        assert allowance["reason"] == "quota_exceeded"


class TestInvoiceHistoryAndPortal:
    async def test_invoice_history(self, client, org_a, billing):
        checkout = (await client.post("/billing/checkout",
                                      json={"plan": "pro", "billing_cycle": "monthly"},
                                      headers=org_a["headers"])).json()
        event = billing.gateway.checkout_completed_event(checkout["checkout_session_id"])
        async with db.AsyncSessionLocal() as s:
            await billing.handle_webhook(s, event)
            await s.commit()
            sub = await _get_sub(s, org_a["org_id"])
            paid = billing.gateway.invoice_paid_event(sub.stripe_subscription_id)
            await billing.handle_webhook(s, paid)
            await s.commit()

        invoices = (await client.get("/billing/invoices", headers=org_a["headers"])).json()
        assert len(invoices["invoices"]) == 1
        assert invoices["invoices"][0]["status"] == "paid"

    async def test_portal_session(self, client, org_a, billing):
        checkout = (await client.post("/billing/checkout",
                                      json={"plan": "pro", "billing_cycle": "monthly"},
                                      headers=org_a["headers"])).json()
        event = billing.gateway.checkout_completed_event(checkout["checkout_session_id"])
        async with db.AsyncSessionLocal() as s:
            await billing.handle_webhook(s, event)
            await s.commit()
        r = await client.post("/billing/portal", headers=org_a["headers"])
        assert r.status_code == 200
        assert "portal_url" in r.json()
