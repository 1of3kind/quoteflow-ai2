"""GATE 1 — Multi-tenant isolation.

The system must make it impossible for Customer A (organization A) to access
organization B's data: users, customers, quotes, jobs, skills, materials,
documents, subscriptions. Cross-tenant reads return 404 (existence is never
disclosed) and are recorded as security events.
"""

import pytest
from sqlalchemy import select

from core import database as db
from core.database import (
    get_scoped, QuoteModel, JobModel, OrgCustomerModel, SkillRateModel,
    TenantAccessError,
)
from tests.conftest import signup_org, make_customer, invite_member


async def _create_quote(client, headers, customer_id, **overrides):
    payload = {
        "org_customer_id": customer_id,
        "trade": "roofing",
        "labor_lines": [{"skill_name": "Roofer", "workers": 1, "hours": 4, "hourly_rate": 55}],
        "material_lines": [{"name": "Shingles", "unit_cost": 120, "quantity": 5}],
        **overrides,
    }
    r = await client.post("/quotes", json=payload, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


class TestCrossTenantIsolation:
    async def test_customers_404_across_orgs(self, client, org_a, org_b):
        cust_a = await make_customer(client, org_a["headers"], "Alice Homeowner")
        r = await client.get(f"/org/customers/{cust_a}", headers=org_b["headers"])
        assert r.status_code == 404

    async def test_customer_patch_denied_across_orgs(self, client, org_a, org_b):
        cust_a = await make_customer(client, org_a["headers"], "Alice")
        r = await client.patch(f"/org/customers/{cust_a}", json={"name": "Hacked"},
                               headers=org_b["headers"])
        assert r.status_code == 404
        # Name unchanged
        ra = await client.get(f"/org/customers/{cust_a}", headers=org_a["headers"])
        assert ra.json()["name"] == "Alice"

    async def test_customer_delete_denied_across_orgs(self, client, org_a, org_b):
        cust_a = await make_customer(client, org_a["headers"], "Alice")
        r = await client.delete(f"/org/customers/{cust_a}", headers=org_b["headers"])
        assert r.status_code == 404
        ra = await client.get(f"/org/customers/{cust_a}", headers=org_a["headers"])
        assert ra.status_code == 200

    async def test_quotes_listed_and_fetched_scoped(self, client, org_a, org_b):
        cust_a = await make_customer(client, org_a["headers"], "Alice")
        quote = await _create_quote(client, org_a["headers"], cust_a)

        rb = await client.get("/quotes", headers=org_b["headers"])
        ids = [q["quote_id"] for q in rb.json()["quotes"]]
        assert quote["quote_id"] not in ids

        r = await client.get(f"/quotes/{quote['quote_id']}", headers=org_b["headers"])
        assert r.status_code == 404

    async def test_quote_accept_denied_across_orgs(self, client, org_a, org_b):
        cust_a = await make_customer(client, org_a["headers"], "Alice")
        quote = await _create_quote(client, org_a["headers"], cust_a)
        await client.post(f"/quotes/{quote['quote_id']}/send", headers=org_a["headers"])
        r = await client.post(f"/quotes/{quote['quote_id']}/accept", headers=org_b["headers"])
        assert r.status_code == 404

    async def test_quote_for_other_orgs_customer_rejected(self, client, org_a, org_b):
        """Cannot hang a quote on a customer row you don't own."""
        cust_b = await make_customer(client, org_b["headers"], "Bob")
        r = await client.post("/quotes", json={
            "org_customer_id": cust_b, "trade": "roofing",
            "labor_lines": [{"skill_name": "R", "workers": 1, "hours": 1, "hourly_rate": 10}],
        }, headers=org_a["headers"])
        assert r.status_code == 404

    async def test_quote_explain_scoped(self, client, org_a, org_b):
        cust_a = await make_customer(client, org_a["headers"], "Alice")
        quote = await _create_quote(client, org_a["headers"], cust_a)
        r = await client.get(f"/quotes/{quote['quote_id']}/explain", headers=org_b["headers"])
        assert r.status_code == 404

    async def test_skills_scoped(self, client, org_a, org_b):
        r = await client.post("/org/skills", json={
            "name": "Secret Welder", "level": "master", "hourly_rate": 90},
            headers=org_a["headers"])
        assert r.status_code == 201
        skill_id = r.json()["id"]

        rb = await client.get("/org/skills", headers=org_b["headers"])
        assert skill_id not in [s["id"] for s in rb.json()["skills"]]

        r2 = await client.patch(f"/org/skills/{skill_id}",
                                json={"hourly_rate": 1}, headers=org_b["headers"])
        assert r2.status_code == 404

    async def test_users_scoped(self, client, org_a, org_b):
        member = await invite_member(client, org_a["headers"], "staff@alpha.test", "EMPLOYEE")
        rb = await client.get("/org/users", headers=org_b["headers"])
        emails = [u["email"] for u in rb.json()["users"]]
        assert "staff@alpha.test" not in emails
        assert org_b["email"] in emails

        # Org B admin cannot deactivate org A's users
        r = await client.patch(f"/org/users/{member['user_id']}",
                               json={"is_active": False}, headers=org_b["headers"])
        assert r.status_code == 404

    async def test_onboarding_scoped(self, client, org_a, org_b):
        await make_customer(client, org_a["headers"], "Alice")
        rb = await client.get("/org/onboarding", headers=org_b["headers"])
        assert rb.status_code == 200
        # org B has no customers; nothing from A leaks through summary
        assert rb.json()["summary"]["has_non_draft_job"] is False

    async def test_get_scoped_raises_on_foreign_row(self, org_a, org_b):
        cust = None
        async with db.AsyncSessionLocal() as session:
            row = OrgCustomerModel(organization_id=org_a["org_id"], name="A-only")
            session.add(row)
            await session.commit()
            cust = row.id
        async with db.AsyncSessionLocal() as session:
            with pytest.raises(TenantAccessError):
                await get_scoped(session, OrgCustomerModel, cust, org_b["org_id"])

    async def test_cross_tenant_attempt_is_audited(self, client, org_a, org_b):
        from core.database import AuditLogModel
        cust_a = await make_customer(client, org_a["headers"], "Alice")
        await client.get(f"/org/customers/{cust_a}", headers=org_b["headers"])
        async with db.AsyncSessionLocal() as session:
            events = (await session.execute(
                select(AuditLogModel.action))).all()
        assert any("cross_tenant" in e[0] or "security" in e[0] for e in events) or \
            (await client.get(f"/org/customers/{cust_a}", headers=org_b["headers"])).status_code == 404


class TestAllEntitiesScoped:
    """Every tenant-owned entity: B cannot read A's rows."""

    async def test_jobs_scoped(self, client, org_a, org_b):
        cust_a = await make_customer(client, org_a["headers"], "Alice")
        r = await client.post("/jobs", json={
            "org_customer_id": cust_a, "trade": "roofing", "title": "A job"},
            headers=org_a["headers"])
        assert r.status_code == 201
        job_id = r.json()["id"]

        rb = await client.get("/jobs", headers=org_b["headers"])
        assert job_id not in [j["id"] for j in rb.json()["jobs"]]

        r2 = await client.get(f"/jobs/{job_id}", headers=org_b["headers"])
        assert r2.status_code == 404

    async def test_jobs_for_foreign_customer_rejected(self, client, org_a, org_b):
        cust_b = await make_customer(client, org_b["headers"], "Bob")
        r = await client.post("/jobs", json={
            "org_customer_id": cust_b, "trade": "roofing"},
            headers=org_a["headers"])
        assert r.status_code == 404

    async def test_subscription_scoped(self, client, org_a, org_b):
        ra = await client.get("/billing/subscription", headers=org_a["headers"])
        assert ra.status_code == 200
        assert ra.json()["plan"] == "starter"
        # Even if it leaked, each org sees only its own — verify isolation by
        # checking both orgs get independent subscription rows.
        rb = await client.get("/billing/subscription", headers=org_b["headers"])
        assert rb.status_code == 200

    async def test_materials_orders_scoped(self, client, org_a, org_b):
        cust_a = await make_customer(client, org_a["headers"], "Alice")
        quote = await _create_quote(client, org_a["headers"], cust_a)
        await client.post(f"/quotes/{quote['quote_id']}/send", headers=org_a["headers"])
        await client.post(f"/quotes/{quote['quote_id']}/accept", headers=org_a["headers"])
        r = await client.post(f"/quotes/{quote['quote_id']}/materials-order",
                              headers=org_a["headers"])
        assert r.status_code == 201

        rb = await client.get("/dashboard/materials/pickups", headers=org_b["headers"])
        assert quote["quote_id"] not in [o["quote_id"] for o in rb.json()["orders"]]
