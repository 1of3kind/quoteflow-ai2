"""GATE 2 — Authentication & permissions.

Signup, login, logout, refresh rotation, email verification, password reset,
account lockout, rate limiting, and the full RBAC matrix.
"""

import pytest

from core.auth import RateLimiter, hash_password, verify_password
from core.rbac import can
from tests.conftest import signup_org, login, invite_member, make_customer


class TestPasswordHashing:
    def test_hash_and_verify(self):
        stored = hash_password("correct horse battery 9")
        assert verify_password("correct horse battery 9", stored)
        assert not verify_password("wrong password 9", stored)

    def test_hash_is_salted(self):
        assert hash_password("same password 1") != hash_password("same password 1")

    def test_malformed_hash_fails_closed(self):
        assert not verify_password("x", "garbage")
        assert not verify_password("x", "")


class TestSignupLogin:
    async def test_signup_creates_org_owner_and_defaults(self, client, org_a):
        r = await client.get("/org", headers=org_a["headers"])
        org = r.json()
        assert org["name"] == "Alpha Roofing"
        assert org["settings"]["overhead_pct"] == 0.15
        skills = (await client.get("/org/skills", headers=org_a["headers"])).json()["skills"]
        assert len(skills) == 3  # seeded default labor rates

    async def test_duplicate_email_rejected(self, client, org_a):
        r = await client.post("/auth/signup", json={
            "organization_name": "Another Co", "email": org_a["email"],
            "password": "An0therPass!", "full_name": "X"})
        assert r.status_code == 409

    async def test_weak_password_rejected(self, client):
        r = await client.post("/auth/signup", json={
            "organization_name": "Weak Co", "email": "w@weak.test",
            "password": "short"})
        assert r.status_code == 422

    async def test_login_success_and_failure(self, client, org_a):
        ok = await client.post("/auth/login", json={
            "email": org_a["email"], "password": org_a["password"]})
        assert ok.status_code == 200

        bad = await client.post("/auth/login", json={
            "email": org_a["email"], "password": "WrongPass123"})
        assert bad.status_code == 401

    async def test_login_unknown_user_same_error(self, client, org_a):
        """No user-enumeration oracle: unknown user == wrong password."""
        bad = (await client.post("/auth/login", json={
            "email": "nobody@nowhere.test", "password": "Whatever123"}))
        wrong = (await client.post("/auth/login", json={
            "email": org_a["email"], "password": "WrongPass123"}))
        assert bad.status_code == wrong.status_code == 401
        assert bad.json() == wrong.json()

    async def test_logout_revokes_refresh(self, client, org_a):
        r = await client.post("/auth/logout", json={"refresh_token": org_a["refresh_token"]})
        assert r.status_code == 200
        rr = await client.post("/auth/refresh", json={"refresh_token": org_a["refresh_token"]})
        assert rr.status_code == 401

    async def test_refresh_rotation_single_use(self, client, org_a):
        r1 = await client.post("/auth/refresh", json={"refresh_token": org_a["refresh_token"]})
        assert r1.status_code == 200
        r2 = await client.post("/auth/refresh", json={"refresh_token": org_a["refresh_token"]})
        assert r2.status_code == 401  # old token cannot be reused

    async def test_access_token_required(self, client):
        r = await client.get("/org")
        assert r.status_code == 401

    async def test_garbage_token_rejected(self, client):
        r = await client.get("/org", headers={"Authorization": "Bearer not-a-jwt"})
        assert r.status_code == 401


class TestAccountLockout:
    async def test_lockout_after_failed_attempts(self, client, org_a):
        for _ in range(5):
            rb = await client.post("/auth/login", json={
                "email": org_a["email"], "password": "BadPass123"})
            assert rb.status_code == 401
        # Even the CORRECT password is rejected while locked
        r = await client.post("/auth/login", json={
            "email": org_a["email"], "password": org_a["password"]})
        assert r.status_code == 423


class TestRateLimiting:
    def test_rate_limiter_blocks_after_limit(self):
        limiter = RateLimiter(max_requests=3, window_seconds=60)
        assert all(limiter.allow("1.2.3.4", "login") for _ in range(3))
        assert not limiter.allow("1.2.3.4", "login")
        assert limiter.allow("5.6.7.8", "login")  # other IPs unaffected

    async def test_forgot_password_no_user_enumeration(self, client):
        r1 = await client.post("/auth/forgot-password", json={"email": "ghost@x.test"})
        r2 = await client.post("/auth/signup", json={
            "organization_name": "Real Co", "email": "real@x.test",
            "password": "RealPass123!"})
        r3 = await client.post("/auth/forgot-password", json={"email": "real@x.test"})
        assert r1.status_code == r3.status_code == 200
        # Same public response for both (dev token field is test-only; ignore it)
        assert r1.json()["status"] == r3.json()["status"] == "sent"


class TestEmailVerificationAndPasswordReset:
    async def test_verify_email_token_single_use(self, client):
        data = (await client.post("/auth/signup", json={
            "organization_name": "Verify Co", "email": "v@verify.test",
            "password": "V3rifyPass!"})).json()
        token = data["dev_email_verify_token"]
        assert (await client.post("/auth/verify-email", json={"token": token})).status_code == 200
        # Reuse must fail
        assert (await client.post("/auth/verify-email", json={"token": token})).status_code == 400

    async def test_password_reset_flow(self, client, org_a):
        r = await client.post("/auth/forgot-password", json={"email": org_a["email"]})
        token = r.json()["dev_reset_token"]
        new_password = "BrandNewPass!9"
        r2 = await client.post("/auth/reset-password",
                               json={"token": token, "new_password": new_password})
        assert r2.status_code == 200

        # Old password no longer works; new one does
        assert (await client.post("/auth/login", json={
            "email": org_a["email"], "password": org_a["password"]})).status_code == 401
        assert (await client.post("/auth/login", json={
            "email": org_a["email"], "password": new_password})).status_code == 200

    async def test_password_reset_revokes_sessions(self, client, org_a):
        r = await client.post("/auth/forgot-password", json={"email": org_a["email"]})
        token = r.json()["dev_reset_token"]
        await client.post("/auth/reset-password",
                          json={"token": token, "new_password": "ReRevoked!99"})
        rr = await client.post("/auth/refresh", json={"refresh_token": org_a["refresh_token"]})
        assert rr.status_code == 401

    async def test_weak_reset_password_rejected(self, client, org_a):
        r = await client.post("/auth/forgot-password", json={"email": org_a["email"]})
        token = r.json()["dev_reset_token"]
        r2 = await client.post("/auth/reset-password",
                               json={"token": token, "new_password": "tiny"})
        assert r2.status_code == 422


class TestRbacMatrix:
    """The exact matrix from the product spec."""

    def test_billing_owner_only(self):
        assert can("OWNER", "billing")
        assert not can("ADMIN", "billing")
        assert not can("MANAGER", "billing")
        assert not can("EMPLOYEE", "billing")

    def test_company_settings_owner_admin(self):
        assert can("OWNER", "org:settings")
        assert can("ADMIN", "org:settings")
        assert not can("MANAGER", "org:settings")
        assert not can("EMPLOYEE", "org:settings")

    def test_users_owner_admin(self):
        assert can("OWNER", "org:users")
        assert can("ADMIN", "org:users")
        assert not can("MANAGER", "org:users")
        assert not can("EMPLOYEE", "org:users")

    def test_quotes_everyone(self):
        for role in ("OWNER", "ADMIN", "MANAGER", "EMPLOYEE"):
            assert can(role, "quotes:write")

    def test_jobs_everyone(self):
        for role in ("OWNER", "ADMIN", "MANAGER", "EMPLOYEE"):
            assert can(role, "jobs:write")

    def test_pricing_formula_owner_admin_manager_limited(self):
        assert can("OWNER", "pricing:configure")
        assert can("ADMIN", "pricing:configure")
        assert not can("MANAGER", "pricing:configure")   # limited: overrides only
        assert can("MANAGER", "pricing:override")
        assert not can("EMPLOYEE", "pricing:override")

    def test_reports_owner_admin_manager_employee_limited(self):
        assert can("OWNER", "reports:full")
        assert can("ADMIN", "reports:full")
        assert can("MANAGER", "reports:full")
        assert not can("EMPLOYEE", "reports:full")
        assert can("EMPLOYEE", "reports:own")            # limited: own data only


class TestRbacEnforcement:
    async def test_owner_can_manage_billing(self, client, org_a):
        r = await client.get("/billing/subscription", headers=org_a["headers"])
        assert r.status_code == 200

    async def test_admin_cannot_access_billing(self, client, org_a):
        admin = await invite_member(client, org_a["headers"], "admin@alpha.test", "ADMIN")
        r = await client.get("/billing/subscription", headers=admin["headers"])
        assert r.status_code == 403

    async def test_manager_cannot_change_pricing_config(self, client, org_a):
        manager = await invite_member(client, org_a["headers"], "mgr@alpha.test", "MANAGER")
        r = await client.patch("/org/pricing", json={"overhead_pct": 0.3,
                                                     "profit_margin_pct": 0.3},
                               headers=manager["headers"])
        assert r.status_code == 403

    async def test_employee_cannot_invite_users(self, client, org_a):
        employee = await invite_member(client, org_a["headers"], "emp@alpha.test", "EMPLOYEE")
        r = await client.post("/org/users", json={
            "email": "sneaky@alpha.test", "role": "EMPLOYEE",
            "password": "SneakyPass!1"}, headers=employee["headers"])
        assert r.status_code == 403

    async def test_employee_cannot_override_pricing(self, client, org_a):
        employee = await invite_member(client, org_a["headers"], "emp2@alpha.test", "EMPLOYEE")
        cust = await make_customer(client, employee["headers"], "Cust")
        r = await client.post("/quotes", json={
            "org_customer_id": cust, "trade": "roofing",
            "labor_lines": [{"skill_name": "R", "workers": 1, "hours": 1, "hourly_rate": 10}],
            "overrides": {"profit_margin_pct": 0.5}},
            headers=employee["headers"])
        assert r.status_code == 403

    async def test_manager_can_override_pricing_per_quote(self, client, org_a):
        manager = await invite_member(client, org_a["headers"], "mgr2@alpha.test", "MANAGER")
        cust = await make_customer(client, manager["headers"], "Cust")
        r = await client.post("/quotes", json={
            "org_customer_id": cust, "trade": "roofing",
            "labor_lines": [{"skill_name": "R", "workers": 1, "hours": 1, "hourly_rate": 10}],
            "overrides": {"profit_margin_pct": 0.5}},
            headers=manager["headers"])
        assert r.status_code == 201, r.text
        assert r.json()["result"]["margin"] == pytest.approx(0.5, abs=1e-3)

    async def test_cannot_invite_owner(self, client, org_a):
        r = await client.post("/org/users", json={
            "email": "second-owner@alpha.test", "role": "OWNER",
            "password": "OwnerPass!99"}, headers=org_a["headers"])
        assert r.status_code == 422

    async def test_inactive_member_cannot_login(self, client, org_a):
        member = await invite_member(client, org_a["headers"], "temp@alpha.test", "EMPLOYEE")
        # Owner deactivates the member
        r = await client.patch(f"/org/users/{member['user_id']}",
                               json={"is_active": False}, headers=org_a["headers"])
        assert r.status_code == 200
        rb = await client.post("/auth/login", json={
            "email": "temp@alpha.test", "password": "M3mberPass!x"})
        assert rb.status_code == 401
