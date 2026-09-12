"""Growth features: narrative explanation, material ordering, scheduling,
and the AI assistant."""

from datetime import datetime, timedelta

import pytest

from core.pricing_engine import PricingInput, LaborLine, MaterialLine, compute
from core.assistant import parse_command_rules, next_weekday, detect_trade
from tests.conftest import make_customer, invite_member


def _golden_input() -> PricingInput:
    return PricingInput(
        labor_lines=[LaborLine(skill_name="Tradesperson", workers=2, hours=8.0, hourly_rate=50.0)],
        material_lines=[MaterialLine(name="Shingles", unit_cost=25.50, quantity=10)],
        overhead_pct=0.15, profit_margin_pct=0.20, tax_rate=0.08,
    )


# ─── Feature 2: narrative quote explanation ────────────────────────────────

class TestNarrativeExplanation:
    def test_narrative_answers_why_this_price(self):
        r = compute(_golden_input())
        assert r.narrative.startswith("Your recommended price is $1,516.56 because")
        assert "$800.00" in r.narrative          # labor
        assert "$255.00" in r.narrative          # materials
        assert "$158.25" in r.narrative          # overhead
        assert "$303.31" in r.narrative          # profit
        assert "$1,637.89" in r.narrative        # total with tax

    def test_narrative_handles_no_materials(self):
        r = compute(PricingInput(
            labor_lines=[LaborLine(skill_name="X", hours=2, hourly_rate=40)],
            material_lines=[], overhead_pct=0.1, profit_margin_pct=0.2, tax_rate=0))
        assert "materials" not in r.narrative or "0 line item" not in r.narrative
        assert "Your recommended price is" in r.narrative

    def test_narrative_is_in_engine_result_dict(self):
        r = compute(_golden_input())
        assert r.as_dict()["narrative"] == r.narrative


# ─── Feature 3: material ordering ──────────────────────────────────────────

async def _supplier_with_catalog(client, headers):
    supplier = (await client.post("/materials/suppliers",
                                  json={"name": "Acme Supply", "address": "1 Depot Rd"},
                                  headers=headers)).json()
    item = (await client.post("/materials/catalog", json={
        "supplier_id": supplier["id"], "sku": "WH-40G", "name": "40-Gal Water Heater",
        "unit": "each", "unit_price": 890.00, "quantity_available": 5,
    }, headers=headers)).json()
    return supplier, item


class TestMaterialOrdering:
    async def test_supplier_and_catalog_scoped(self, client, org_a, org_b):
        supplier, item = await _supplier_with_catalog(client, org_a["headers"])
        rb = (await client.get("/materials/catalog", headers=org_b["headers"])).json()
        assert item["id"] not in [i["id"] for i in rb["items"]]
        r = await client.get(f"/materials/catalog/{item['id']}", headers=org_b["headers"])
        assert r.status_code in (404, 405)

    async def test_duplicate_sku_rejected(self, client, org_a):
        supplier, _ = await _supplier_with_catalog(client, org_a["headers"])
        r = await client.post("/materials/catalog", json={
            "supplier_id": supplier["id"], "sku": "WH-40G", "name": "Dup",
            "unit_price": 100}, headers=org_a["headers"])
        assert r.status_code == 409

    async def test_availability_lookup(self, client, org_a):
        await _supplier_with_catalog(client, org_a["headers"])
        r = (await client.get("/materials/availability", params={"sku": "WH-40G"},
                              headers=org_a["headers"])).json()
        assert r["offers"][0]["in_stock"] is True
        assert r["offers"][0]["quantity_available"] == 5

    async def test_order_lifecycle(self, client, org_a):
        supplier, item = await _supplier_with_catalog(client, org_a["headers"])
        order = (await client.post("/materials/orders", json={
            "supplier_id": supplier["id"],
            "items": [{"catalog_item_id": item["id"], "quantity": 2}],
        }, headers=org_a["headers"])).json()
        assert order["status"] == "draft"
        assert order["subtotal"] == 1780.00
        assert order["items"][0]["sku"] == "WH-40G"
        assert order["items"][0]["unit_price"] == 890.00

        oid = order["order_id"]
        for status in ("placed", "confirmed", "received"):
            r = await client.patch(f"/materials/orders/{oid}/status",
                                   json={"status": status}, headers=org_a["headers"])
            assert r.status_code == 200, r.text
            assert r.json()["status"] == status

    async def test_invalid_status_transition_rejected(self, client, org_a):
        supplier, item = await _supplier_with_catalog(client, org_a["headers"])
        order = (await client.post("/materials/orders", json={
            "supplier_id": supplier["id"],
            "items": [{"catalog_item_id": item["id"], "quantity": 1}],
        }, headers=org_a["headers"])).json()
        # draft → received is not a legal transition
        r = await client.patch(f"/materials/orders/{order['order_id']}/status",
                               json={"status": "received"}, headers=org_a["headers"])
        assert r.status_code == 409

    async def test_insufficient_availability_rejected(self, client, org_a):
        supplier, item = await _supplier_with_catalog(client, org_a["headers"])
        r = await client.post("/materials/orders", json={
            "supplier_id": supplier["id"],
            "items": [{"catalog_item_id": item["id"], "quantity": 99}],
        }, headers=org_a["headers"])
        assert r.status_code == 409
        assert r.json()["available"] == 5

    async def test_cross_supplier_item_rejected(self, client, org_a):
        s1, item = await _supplier_with_catalog(client, org_a["headers"])
        s2 = (await client.post("/materials/suppliers", json={"name": "Other Co"},
                                headers=org_a["headers"])).json()
        r = await client.post("/materials/orders", json={
            "supplier_id": s2["id"],
            "items": [{"catalog_item_id": item["id"], "quantity": 1}],
        }, headers=org_a["headers"])
        assert r.status_code == 422

    async def test_orders_scoped_across_orgs(self, client, org_a, org_b):
        supplier, item = await _supplier_with_catalog(client, org_a["headers"])
        order = (await client.post("/materials/orders", json={
            "supplier_id": supplier["id"],
            "items": [{"catalog_item_id": item["id"], "quantity": 1}],
        }, headers=org_a["headers"])).json()
        rb = (await client.get("/materials/orders", headers=org_b["headers"])).json()
        assert order["order_id"] not in [o["order_id"] for o in rb["orders"]]
        r = await client.get(f"/materials/orders/{order['order_id']}",
                             headers=org_b["headers"])
        assert r.status_code == 404


# ─── Feature 4: scheduling ─────────────────────────────────────────────────

async def _job_with_schedule(client, headers, org, skill_id=None):
    cust = await make_customer(client, headers, "Sched Customer")
    scheduled = (datetime.utcnow() + timedelta(days=2)).replace(microsecond=0)
    job = (await client.post("/jobs", json={
        "org_customer_id": cust, "trade": "plumbing", "title": "Water heater",
        "scheduled_date": scheduled.isoformat(), "duration_hours": 4,
    }, headers=headers)).json()
    return job, cust


class TestScheduling:
    async def test_requirement_flow_and_suggestions(self, client, org_a):
        job, _ = await _job_with_schedule(client, org_a["headers"], org_a)
        skill = (await client.get("/org/skills", headers=org_a["headers"])).json()["skills"][1]

        r = await client.post(f"/jobs/{job['id']}/requirements", json={
            "skill_id": skill["id"], "workers_needed": 2, "hours": 4},
            headers=org_a["headers"])
        assert r.status_code == 201

        sug = (await client.get(f"/jobs/{job['id']}/suggestions",
                                headers=org_a["headers"])).json()["suggestions"]
        assert sug[0]["workers_needed"] == 2
        # Owner is the only org user; they're available (no other jobs)
        assert len(sug[0]["available_workers"]) == 1

    async def test_assign_and_conflict_detection(self, client, org_a):
        job, _ = await _job_with_schedule(client, org_a["headers"], org_a)
        owner = (await client.get("/org/users", headers=org_a["headers"])).json()["users"][0]

        r = await client.post(f"/jobs/{job['id']}/assign", json={
            "user_id": owner["id"]}, headers=org_a["headers"])
        assert r.status_code == 201
        assert r.json()["worker_name"]

        # A second job overlapping the same window must reject the same worker
        cust = await make_customer(client, org_a["headers"], "Other Cust")
        overlapping = (await client.post("/jobs", json={
            "org_customer_id": cust, "trade": "plumbing",
            "scheduled_date": job["scheduled_date"], "duration_hours": 4,
        }, headers=org_a["headers"])).json()
        r2 = await client.post(f"/jobs/{overlapping['id']}/assign", json={
            "user_id": owner["id"]}, headers=org_a["headers"])
        assert r2.status_code == 409

        # But a non-overlapping job is fine
        far = (await client.post("/jobs", json={
            "org_customer_id": cust, "trade": "plumbing",
            "scheduled_date": (datetime.utcnow() + timedelta(days=9)).isoformat(),
            "duration_hours": 2,
        }, headers=org_a["headers"])).json()
        r3 = await client.post(f"/jobs/{far['id']}/assign", json={
            "user_id": owner["id"]}, headers=org_a["headers"])
        assert r3.status_code == 201

    async def test_day_view_shows_crew(self, client, org_a):
        job, _ = await _job_with_schedule(client, org_a["headers"], org_a)
        owner = (await client.get("/org/users", headers=org_a["headers"])).json()["users"][0]
        await client.post(f"/jobs/{job['id']}/assign", json={
            "user_id": owner["id"]}, headers=org_a["headers"])

        day = job["scheduled_date"][:10]
        r = (await client.get("/jobs/schedule/day", params={"date": day},
                              headers=org_a["headers"])).json()
        assert r["jobs"][0]["id"] == job["id"]
        assert r["jobs"][0]["crew"][0]["user_id"] == owner["id"]

    async def test_unassign(self, client, org_a):
        job, _ = await _job_with_schedule(client, org_a["headers"], org_a)
        owner = (await client.get("/org/users", headers=org_a["headers"])).json()["users"][0]
        a = (await client.post(f"/jobs/{job['id']}/assign", json={
            "user_id": owner["id"]}, headers=org_a["headers"])).json()
        r = await client.delete(f"/jobs/{job['id']}/assign/{a['id']}",
                                headers=org_a["headers"])
        assert r.status_code == 204

    async def test_scheduling_scoped(self, client, org_a, org_b):
        job, _ = await _job_with_schedule(client, org_a["headers"], org_a)
        r = await client.get(f"/jobs/{job['id']}/suggestions", headers=org_b["headers"])
        assert r.status_code == 404


# ─── Feature 1: AI assistant ────────────────────────────────────────────────

class TestAssistantParser:
    def test_parses_canonical_command(self):
        cmd = parse_command_rules(
            "Create a quote for John Smith. Replace the water heater and schedule it for Tuesday.")
        assert cmd.intent == "quote"
        assert cmd.customer_name == "John Smith"
        assert cmd.trade == "plumbing"
        assert cmd.schedule_date == next_weekday("tuesday")
        assert cmd.send_quote is False

    def test_parses_send_and_hours(self):
        cmd = parse_command_rules(
            "Quote for Alice Green, rewiring the garage panel, 8 hours, send it.")
        assert cmd.customer_name == "Alice Green"
        assert cmd.trade == "electrical"
        assert cmd.hours == 8.0
        assert cmd.send_quote is True

    def test_tomorrow(self):
        cmd = parse_command_rules("Quote for Bob Roof, fix the roof, schedule tomorrow")
        assert cmd.schedule_date == (datetime.utcnow() + timedelta(days=1)).date().isoformat()

    def test_weekday_math(self):
        today = datetime(2026, 9, 11)  # a Friday
        assert next_weekday("tuesday", today) == "2026-09-15"
        assert next_weekday("friday", today) == "2026-09-18"  # next week, not today


class TestAssistantExecution:
    async def test_full_command_executes_workflow(self, client, org_a):
        r = await client.post("/assistant/command", json={
            "text": "Create a quote for Jane Waters. Replace the water heater "
                    "and schedule it for Tuesday."
        }, headers=org_a["headers"])
        assert r.status_code == 200, r.text
        data = r.json()

        actions = {a["action"] for a in data["actions"]}
        assert "customer_created" in actions
        assert "quote_created" in actions
        assert "job_scheduled" in actions
        quote_action = next(a for a in data["actions"] if a["action"] == "quote_created")
        assert quote_action["recommended_price"] > 0
        assert "scheduled" in data["summary"] or "Scheduled" in data["summary"]

        # Customer + quote + job actually exist for this org
        custs = (await client.get("/org/customers", headers=org_a["headers"])).json()
        assert any(c["name"] == "Jane Waters" for c in custs["customers"])
        jobs = (await client.get("/jobs", headers=org_a["headers"])).json()
        assert any(j["quote_id"] == quote_action["quote_id"] for j in jobs["jobs"])

    async def test_existing_customer_reused(self, client, org_a):
        await make_customer(client, org_a["headers"], "Rita Repeat")
        r1 = await client.post("/assistant/command", json={
            "text": "Create a quote for Rita Repeat, fix the leaking pipe."},
            headers=org_a["headers"])
        actions = {a["action"] for a in r1.json()["actions"]}
        assert "customer_found" in actions

    async def test_unparseable_command_reports_clearly(self, client, org_a):
        r = await client.post("/assistant/command", json={
            "text": "hello there"}, headers=org_a["headers"])
        assert r.status_code == 200
        data = r.json()
        assert data["actions"] == []
        assert "couldn't" in data["summary"] or "not understood" in data["summary"]

    async def test_assistant_requires_auth(self, client):
        r = await client.post("/assistant/command", json={"text": "quote for X"})
        assert r.status_code == 401

    async def test_assistant_respects_rbac(self, client, org_a):
        employee = await invite_member(client, org_a["headers"], "ea@alpha.test", "EMPLOYEE")
        # Employees CAN create quotes, so the command succeeds — but pricing
        # override roles are still enforced inside the handlers it calls.
        r = await client.post("/assistant/command", json={
            "text": "Create a quote for Pat Plumber, replace the toilet."},
            headers=employee["headers"])
        assert r.status_code == 200
        assert any(a["action"] == "quote_created" for a in r.json()["actions"])
