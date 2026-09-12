"""Review fixes: inbound tenant routing (MUST-FIX #1) and public customer
approval tokens (MUST-FIX #2)."""

import os
import pytest
from datetime import datetime, timedelta

from core import database as db
from core.database import AsyncSessionLocal, ConversationModel, QuoteModel
from tests.conftest import make_customer


@pytest.fixture(autouse=True)
def _app_state():
    """httpx ASGITransport does not run the FastAPI lifespan — the webhook
    handlers need app.state.sms / app.state.conversations initialized."""
    from api.main import app
    from services.sms_handler import SMSHandler
    from core.conversation_manager import ConversationManager
    if not hasattr(app.state, "sms"):
        app.state.sms = SMSHandler()
    if not hasattr(app.state, "conversations"):
        app.state.conversations = ConversationManager()
    yield


# ─── MUST-FIX #1: inbound SMS/email tenant routing ─────────────────────────

TWILIO_TOKEN = "test-twilio-auth-token"


def _twilio_headers(form: dict, path: str) -> dict:
    from twilio.request_validator import RequestValidator
    os.environ.setdefault("TWILIO_AUTH_TOKEN", TWILIO_TOKEN)
    os.environ["PUBLIC_BASE_URL"] = "http://testserver"
    sig = RequestValidator(TWILIO_TOKEN).compute_signature(
        f"http://testserver{path}", form)
    return {"X-Twilio-Signature": sig}


async def _map_number(client, headers, phone_number: str, provider: str = "sms"):
    r = await client.post("/org/channels", json={
        "provider": provider, "channel_value": phone_number},
        headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


class TestChannelRegistration:
    async def test_register_and_list_channels(self, client, org_a):
        ch = await _map_number(client, org_a["headers"], "+15551110000")
        r = await client.get("/org/channels", headers=org_a["headers"])
        vals = [c["channel_value"] for c in r.json()["channels"]]
        assert "+15551110000" in vals

    async def test_duplicate_channel_rejected(self, client, org_a, org_b):
        await _map_number(client, org_a["headers"], "+15552220000")
        # Another org cannot claim the same number
        r = await client.post("/org/channels", json={
            "provider": "sms", "channel_value": "+15552220000"},
            headers=org_b["headers"])
        assert r.status_code == 409

    async def test_channels_scoped(self, client, org_a, org_b):
        ch = await _map_number(client, org_a["headers"], "+15553330000")
        r = await client.delete(f"/org/channels/{ch['id']}", headers=org_b["headers"])
        assert r.status_code == 404


class TestSmsTenantRouting:
    async def _sms(self, client, form, path="/webhook/sms"):
        return await client.post(path, data=form, headers=_twilio_headers(form, path))

    async def test_unmapped_number_creates_nothing(self, client):
        form = {"From": "+15550001111", "To": "+15999999999", "Body": "roofing"}
        r = await self._sms(client, form)
        assert r.status_code == 200
        assert "not accepting messages" in r.text
        async with db.AsyncSessionLocal() as s:
            convs = (await s.execute(
                db.select(ConversationModel))).scalars().all()
        assert convs == []

    async def test_mapped_number_routes_to_org(self, client, org_a):
        await _map_number(client, org_a["headers"], "+15554440000")
        form = {"From": "+15550002222", "To": "+15554440000", "Body": "roofing"}
        r = await self._sms(client, form)
        assert r.status_code == 200
        async with db.AsyncSessionLocal() as s:
            convs = (await s.execute(
                db.select(ConversationModel))).scalars().all()
        assert len(convs) == 1
        assert convs[0].organization_id == org_a["org_id"]
        assert convs[0].id == f"sms:{org_a['org_id']}:+15550002222"

    async def test_same_customer_two_orgs_isolated(self, client, org_a, org_b):
        """The SAME customer phone texting two different businesses gets two
        isolated conversations — the old shared 'sms_<phone>' key is gone."""
        await _map_number(client, org_a["headers"], "+15554440000")
        await _map_number(client, org_b["headers"], "+15554440111")
        phone = "+15556667777"
        await self._sms(client, {"From": phone, "To": "+15554440000", "Body": "roofing"})
        await self._sms(client, {"From": phone, "To": "+15554440111", "Body": "plumbing"})

        async with db.AsyncSessionLocal() as s:
            convs = (await s.execute(
                db.select(ConversationModel).order_by(ConversationModel.id))).scalars().all()
        assert len(convs) == 2
        orgs = {c.organization_id for c in convs}
        assert orgs == {org_a["org_id"], org_b["org_id"]}
        assert len({c.id for c in convs}) == 2
        trades = sorted(c.trade for c in convs)
        assert trades == ["plumbing", "roofing"]


# ─── MUST-FIX #2: public customer approval tokens ──────────────────────────

async def _quote_sent(client, headers, customer_id):
    r = await client.post("/quotes", json={
        "org_customer_id": customer_id, "trade": "plumbing",
        "labor_lines": [{"skill_name": "Plumber", "workers": 1,
                         "hours": 3, "hourly_rate": 60}],
    }, headers=headers)
    assert r.status_code == 201
    quote_id = r.json()["quote_id"]
    r2 = await client.post(f"/quotes/{quote_id}/send", headers=headers)
    assert r2.status_code == 200
    return quote_id, r2.json()["approval_url"]


class TestPublicApproval:
    async def test_send_returns_token_url_and_document_has_it(self, client, org_a):
        cust = await make_customer(client, org_a["headers"], "Taylor Homeowner")
        quote_id, url = await _quote_sent(client, org_a["headers"], cust)
        assert url.startswith("http")
        assert f"/q/{quote_id}/" in url
        # raw token never stored in plaintext
        token = url.rsplit("/", 1)[1]
        async with db.AsyncSessionLocal() as s:
            q = await s.get(QuoteModel, quote_id)
            from core.auth import token_digest
            assert q.approval_token_hash == token_digest(token)
            assert token not in (q.approval_token_hash or "")

    async def test_customer_can_view_and_approve_without_login(self, client, org_a):
        cust = await make_customer(client, org_a["headers"], "Morgan Buyer")
        quote_id, url = await _quote_sent(client, org_a["headers"], cust)
        token = url.rsplit("/", 1)[1]

        # View: no Authorization header at all
        view = await client.get(f"/q/{quote_id}/{token}")
        assert view.status_code == 200
        assert view.json()["approvable"] is True
        assert view.json()["quote"]["customer"]["name"] == "Morgan Buyer"

        # Approve: no login — the token IS the authorization
        approve = await client.post(f"/q/{quote_id}/{token}/approve")
        assert approve.status_code == 200, approve.text
        assert approve.json()["status"] == "accepted"
        assert approve.json()["job_id"]

        async with db.AsyncSessionLocal() as s:
            q = await s.get(QuoteModel, quote_id)
            assert q.status == "accepted"
            assert q.approved_via == "link"

    async def test_wrong_token_is_uniform_404(self, client, org_a):
        cust = await make_customer(client, org_a["headers"], "Casey Safe")
        quote_id, url = await _quote_sent(client, org_a["headers"], cust)
        bad = await client.post(f"/q/{quote_id}/definitely-not-the-token/approve")
        assert bad.status_code == 404
        view = await client.get(f"/q/{quote_id}/definitely-not-the-token")
        assert view.status_code == 404

    async def test_revoked_token_stops_working(self, client, org_a):
        cust = await make_customer(client, org_a["headers"], "Riley Revoke")
        quote_id, url = await _quote_sent(client, org_a["headers"], cust)
        token = url.rsplit("/", 1)[1]
        r = await client.post(f"/quotes/{quote_id}/revoke-approval",
                              headers=org_a["headers"])
        assert r.status_code == 200
        assert (await client.post(f"/q/{quote_id}/{token}/approve")).status_code == 404
        assert (await client.get(f"/q/{quote_id}/{token}")).status_code == 404

    async def test_expired_token_cannot_approve(self, client, org_a):
        cust = await make_customer(client, org_a["headers"], "Evan Expired")
        quote_id, url = await _quote_sent(client, org_a["headers"], cust)
        token = url.rsplit("/", 1)[1]
        async with db.AsyncSessionLocal() as s:
            q = await s.get(QuoteModel, quote_id)
            q.approval_token_expires_at = datetime.utcnow() - timedelta(days=1)
            await s.commit()
        assert (await client.post(f"/q/{quote_id}/{token}/approve")).status_code == 404

    async def test_double_approval_is_idempotent(self, client, org_a):
        cust = await make_customer(client, org_a["headers"], "Dana Twice")
        quote_id, url = await _quote_sent(client, org_a["headers"], cust)
        token = url.rsplit("/", 1)[1]
        first = await client.post(f"/q/{quote_id}/{token}/approve")
        # Token remains valid for viewing; approving again reports state
        second = await client.post(f"/q/{quote_id}/{token}/approve")
        assert first.json()["status"] == "accepted"
        assert second.json()["status"] in ("already_accepted", "accepted")
        assert first.json()["job_id"] == second.json()["job_id"]

    async def test_employees_still_have_explicit_accept(self, client, org_a):
        """The authenticated endpoint still works for employees — via='api'."""
        cust = await make_customer(client, org_a["headers"], "Oscar Office")
        quote_id, url = await _quote_sent(client, org_a["headers"], cust)
        r = await client.post(f"/quotes/{quote_id}/accept", headers=org_a["headers"])
        assert r.status_code == 200
        async with db.AsyncSessionLocal() as s:
            q = await s.get(QuoteModel, quote_id)
            assert q.approved_via == "api"


class TestSmsApproval:
    async def test_sms_accept_reply_accepts_quote(self, client, org_a):
        """'ACCEPT' reply on the mapped number actually accepts the quote."""
        await _map_number(client, org_a["headers"], "+15554440000")
        cust = await make_customer(client, org_a["headers"], "Sam Texter")
        quote_id, url = await _quote_sent(client, org_a["headers"], cust)

        # First, a greeting SMS creates the org-scoped conversation; then the
        # photo flow attaches the quote to it (simulated here).
        form = {"From": "+15559990000", "To": "+15554440000", "Body": "hello"}
        await client.post("/webhook/sms", data=form,
                          headers=_twilio_headers(form, "/webhook/sms"))
        from core.conversation_manager import ConversationManager
        await ConversationManager().set_quote_id(
            f"sms:{org_a['org_id']}:+15559990000", quote_id)

        form = {"From": "+15559990000", "To": "+15554440000", "Body": "accept"}
        r = await client.post("/webhook/sms", data=form,
                              headers=_twilio_headers(form, "/webhook/sms"))
        assert r.status_code == 200
        async with db.AsyncSessionLocal() as s:
            q = await s.get(QuoteModel, quote_id)
            assert q.status == "accepted"
            assert q.approved_via == "sms_reply"


class TestCorsFailClosed:
    def test_production_boots_without_explicit_cors(self):
        """MUST-FIX #5: production with a wildcard CORS origin refuses to
        start instead of silently allowing every origin."""
        import subprocess
        import sys
        env = {**os.environ,
               "APP_ENV": "production",
               "CORS_ALLOW_ORIGINS": "*",
               "JWT_SECRET": "x" * 48,
               "STRIPE_MODE": "mock",
               "DATABASE_URL": "sqlite+aiosqlite:///:memory:"}
        code = "import api.main"
        r = subprocess.run([sys.executable, "-c", code], env=env,
                           capture_output=True, text=True)
        assert r.returncode != 0
        assert "CORS_ALLOW_ORIGINS" in (r.stderr or "")

    def test_production_boots_with_explicit_cors(self):
        import subprocess
        import sys
        env = {**os.environ,
               "APP_ENV": "production",
               "CORS_ALLOW_ORIGINS": "https://app.e-zflow.com",
               "JWT_SECRET": "x" * 48,
               "STRIPE_MODE": "mock",
               "DATABASE_URL": "sqlite+aiosqlite:///:memory:"}
        code = ("import api.main; "
                "print('CORS_ORIGINS_OK')")
        r = subprocess.run([sys.executable, "-c", code], env=env,
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        assert "CORS_ORIGINS_OK" in r.stdout
