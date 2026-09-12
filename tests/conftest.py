"""Shared fixtures: isolated per-test database + authenticated multi-tenant
HTTP clients for two organizations (the cross-tenant isolation harness)."""

import os

# Must be set before app modules are imported (they read env at import time).
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("STRIPE_MODE", "mock")
os.environ.setdefault("REQUIRE_VERIFIED_EMAIL", "false")
os.environ.setdefault("SIGNUP_RATE_LIMIT", "10000")
os.environ.setdefault("LOGIN_RATE_LIMIT", "10000")
os.environ.setdefault("RESET_RATE_LIMIT", "10000")

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from core.database import set_database_url, init_db
from api.main import app  # noqa: E402  (imports after env setup)


@pytest_asyncio.fixture(autouse=True)
async def test_db(tmp_path):
    """File-backed SQLite so all connections share one database."""
    db_file = tmp_path / "test.db"
    set_database_url(f"sqlite+aiosqlite:///{db_file.as_posix()}")
    await init_db()
    yield
    set_database_url("sqlite+aiosqlite:///:memory:")  # drop engine refs


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def signup_org(client: AsyncClient, name: str, email: str,
                     password: str = "S3curePass!x") -> dict:
    """Create an org + owner, verify email, return auth info."""
    r = await client.post("/auth/signup", json={
        "organization_name": name, "email": email,
        "password": password, "full_name": "Owner",
    })
    assert r.status_code == 201, r.text
    data = r.json()
    verify = data.get("dev_email_verify_token")
    if verify:
        rv = await client.post("/auth/verify-email", json={"token": verify})
        assert rv.status_code == 200, rv.text
    return {
        "email": email,
        "password": password,
        "user_id": data["user"]["id"],
        "org_id": data["organization"]["id"],
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "headers": {"Authorization": f"Bearer {data['access_token']}"},
    }


@pytest_asyncio.fixture
async def org_a(client):
    return await signup_org(client, "Alpha Roofing", "owner@alpha.test")


@pytest_asyncio.fixture
async def org_b(client):
    return await signup_org(client, "Beta Plumbing", "owner@beta.test")


async def login(client: AsyncClient, email: str, password: str) -> dict:
    r = await client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    data = r.json()
    return {**data, "headers": {"Authorization": f"Bearer {data['access_token']}"}}


async def invite_member(client: AsyncClient, owner_headers: dict,
                        email: str, role: str) -> dict:
    r = await client.post("/org/users", json={
        "email": email, "full_name": email.split("@")[0].title(),
        "role": role, "password": "M3mberPass!x",
    }, headers=owner_headers)
    assert r.status_code == 201, r.text
    member = await login(client, email, "M3mberPass!x")
    member["user_id"] = member["user"]["id"]
    return member


async def make_customer(client: AsyncClient, headers: dict, name: str) -> str:
    r = await client.post("/org/customers", json={"name": name}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["id"]
