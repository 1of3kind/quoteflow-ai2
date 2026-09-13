"""The /api discovery endpoint must report integration readiness as
booleans only — never echo any secret value (GATE 6)."""

import asyncio

from api.main import api_info


def test_integrations_booleans_reflect_env(monkeypatch):
    for name in ("OPENAI_API_KEY", "STRIPE_SECRET_KEY", "TWILIO_ACCOUNT_SID",
                 "TWILIO_AUTH_TOKEN", "SENDGRID_API_KEY", "ASSISTANT_USE_LLM"):
        monkeypatch.delenv(name, raising=False)
    info = asyncio.run(api_info())
    assert info["integrations"] == {
        "openai": False, "stripe": False, "twilio": False,
        "sendgrid": False, "assistant_llm": False,
    }

    for name in ("OPENAI_API_KEY", "STRIPE_SECRET_KEY", "TWILIO_ACCOUNT_SID",
                 "TWILIO_AUTH_TOKEN", "SENDGRID_API_KEY"):
        monkeypatch.setenv(name, "super-secret-value-xyz")
    monkeypatch.setenv("ASSISTANT_USE_LLM", "true")
    info = asyncio.run(api_info())
    assert info["integrations"]["openai"] is True
    assert info["integrations"]["stripe"] is True
    assert info["integrations"]["twilio"] is True
    assert info["integrations"]["sendgrid"] is True
    assert info["integrations"]["assistant_llm"] is True


def test_integrations_never_leak_values(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_super_secret_value")
    info = asyncio.run(api_info())
    assert "sk_live_super_secret_value" not in str(info)
    assert all(isinstance(v, bool) for v in info["integrations"].values())
