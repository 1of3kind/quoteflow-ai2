"""GATE 10 — Customer-facing polish: the full workflow and dashboard.

Signup → onboarding → customer → quote → explain → send (document) →
accept → job → materials order → dashboard metrics.
"""

import pytest

from tests.conftest import make_customer, invite_member


async def _quote(client, headers, customer_id, **extra):
    payload = {
        "org_customer_id": customer_id,
        "trade": "roofing",
        "labor_lines": [{"skill_name": "Master Roofer", "skill_level": "master",
                         "workers": 2, "hours": 8, "hourly_rate": 68}],
        "material_lines": [{"name": "Architectural Shingles", "unit_cost": 35, "quantity": 40}],
        **extra,
    }
    r = await client.post("/quotes", json=payload, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


class TestFullWorkflow:
    async def test_quote_to_job_lifecycle(self, client, org_a):
        # 1. Onboarding checklist reflects a fresh org
        ob = (await client.get("/org/onboarding", headers=org_a["headers"])).json()
        steps = {s["step"]: s["done"] for s in ob["steps"]}
        assert steps["company_created"] is True
        assert steps["skills_configured"] is True      # seeded defaults

        # 2. Customer + quote
        cust = await make_customer(client, org_a["headers"], "Dana Homeowner")
        quote = await _quote(client, org_a["headers"], cust,
                             title="Full roof replacement",
                             description="Tear off and replace, 2400 sqft")
        assert quote["status"] == "draft"
        assert quote["result"]["recommended_price"] > 0

        # 3. Explanation endpoint reproduces the price
        exp = (await client.get(f"/quotes/{quote['quote_id']}/explain",
                                headers=org_a["headers"])).json()
        assert exp["recomputed"]["recommended_price"] == quote["result"]["recommended_price"]
        assert "Recommended price" in exp["explanation"]

        # 4. Send → professional document
        sent = (await client.post(f"/quotes/{quote['quote_id']}/send",
                                  headers=org_a["headers"])).json()
        doc = sent["document"]
        assert doc["company"]["name"] == "Alpha Roofing"
        assert doc["customer"]["name"] == "Dana Homeowner"
        assert doc["quote"]["description"] == "Tear off and replace, 2400 sqft"
        assert doc["totals"]["recommended_price"] == quote["result"]["recommended_price"]
        assert doc["quote"]["valid_until"]
        assert "ACCEPT" in doc["approval"]["instructions"]

        # 5. Accept → job created
        acc = (await client.post(f"/quotes/{quote['quote_id']}/accept",
                                 headers=org_a["headers"])).json()
        assert acc["status"] == "accepted"
        job_id = acc["job_id"]

        # 6. Materials order from accepted quote
        mo = (await client.post(f"/quotes/{quote['quote_id']}/materials-order",
                                headers=org_a["headers"])).json()
        assert mo["status"] == "order_created"
        assert mo["subtotal"] > 0

        # 7. Schedule and complete the job
        r = await client.patch(f"/jobs/{job_id}", json={"status": "in_progress"},
                               headers=org_a["headers"])
        assert r.json()["status"] == "in_progress"
        r = await client.patch(f"/jobs/{job_id}", json={"status": "completed"},
                               headers=org_a["headers"])
        assert r.json()["status"] == "completed"
        assert r.json()["completed_at"]

        # 8. Onboarding shows first quote flow done
        ob2 = (await client.get("/org/onboarding", headers=org_a["headers"])).json()
        steps2 = {s["step"]: s["done"] for s in ob2["steps"]}
        assert steps2["first_quote_generated"] is True
        assert steps2["first_quote_approved"] is True

    async def test_dashboard_metrics(self, client, org_a):
        cust = await make_customer(client, org_a["headers"], "Ernie Homeowner")
        quote = await _quote(client, org_a["headers"], cust)
        await client.post(f"/quotes/{quote['quote_id']}/send", headers=org_a["headers"])
        await client.post(f"/quotes/{quote['quote_id']}/accept", headers=org_a["headers"])
        acc_job = (await client.get("/jobs", headers=org_a["headers"])).json()["jobs"][0]
        await client.patch(f"/jobs/{acc_job['id']}", json={"status": "scheduled"},
                           headers=org_a["headers"])

        summary = (await client.get("/dashboard/summary", headers=org_a["headers"])).json()
        assert summary["approved_quotes"] == 1
        assert summary["pending_quotes"] == 0
        assert summary["revenue_this_month"] == quote["result"]["total_with_tax"]
        assert summary["profit_this_month"] == quote["result"]["profit"]
        assert any(j["id"] == acc_job["id"] for j in summary["upcoming_jobs"])
        assert len(summary["recent_activity"]) > 0

    async def test_revenue_series_scoped(self, client, org_a, org_b):
        cust = await make_customer(client, org_a["headers"], "Fay")
        quote = await _quote(client, org_a["headers"], cust)
        await client.post(f"/quotes/{quote['quote_id']}/send", headers=org_a["headers"])

        r = (await client.get("/dashboard/revenue", headers=org_a["headers"])).json()
        total_sent = sum(d["quotes_sent"] for d in r["series"])
        assert total_sent == 1

        rb = (await client.get("/dashboard/revenue", headers=org_b["headers"])).json()
        assert all(d["quotes_sent"] == 0 for d in rb["series"])


class TestQuoteExpiration:
    async def test_expired_quote_cannot_be_accepted(self, client, org_a):
        from datetime import datetime, timedelta
        from core import database as db
        from core.database import QuoteModel
        cust = await make_customer(client, org_a["headers"], "Gil")
        quote = await _quote(client, org_a["headers"], cust)
        await client.post(f"/quotes/{quote['quote_id']}/send", headers=org_a["headers"])

        async with db.AsyncSessionLocal() as s:
            q = await s.get(QuoteModel, quote["quote_id"])
            q.expires_at = datetime.utcnow() - timedelta(days=1)
            await s.commit()

        r = await client.post(f"/quotes/{quote['quote_id']}/accept", headers=org_a["headers"])
        assert r.status_code == 410
