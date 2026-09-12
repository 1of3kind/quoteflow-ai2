"""Authentication endpoints (GATE 2): signup, login, logout, refresh,
email verification, password reset — with rate limiting and lockout."""

import logging
import re
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core import auth as auth_mod
from core.auth import (
    hash_password, verify_password, validate_password_strength,
    create_access_token, generate_token_value, token_digest, expiry_for,
    is_locked_out, register_failed_login, register_successful_login,
    login_rate_limiter, signup_rate_limiter, password_reset_rate_limiter,
    client_ip, REQUIRE_VERIFIED_EMAIL,
)
from core.database import get_session, UserModel, OrganizationModel, AuthTokenModel, \
    SkillRateModel, WebhookEventModel
from core.tenancy import AuthContext, get_current_user, audit
from payments.billing import get_billing_service

logger = logging.getLogger("api.auth")

router = APIRouter(prefix="/auth", tags=["auth"])

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

DEV_MODE = lambda: __import__("os").getenv("APP_ENV", "development") != "production"


class SignupRequest(BaseModel):
    organization_name: str = Field(min_length=2, max_length=255)
    email: str = Field(min_length=5, max_length=320)
    password: str = Field(min_length=1, max_length=1024)
    full_name: str = Field(default="", max_length=255)


class LoginRequest(BaseModel):
    email: str
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class EmailTokenRequest(BaseModel):
    token: str


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


def _valid_email(email: str) -> str:
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail="Invalid email address")
    return email


def _slugify(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "org"
    return f"{base}-{secrets.token_hex(3)}"


async def _store_token(session: AsyncSession, user_id: str, token_type: str) -> str:
    value = generate_token_value()
    session.add(AuthTokenModel(
        token_hash=token_digest(value), user_id=user_id, token_type=token_type,
        expires_at=expiry_for(token_type)))
    return value


async def _send_email(to: str, subject: str, body: str) -> None:
    """SendGrid when configured; otherwise log (never silently drop in prod)."""
    import os
    key = os.getenv("SENDGRID_API_KEY", "").strip()
    sender = os.getenv("EMAIL_FROM", "no-reply@ezflow.local")
    if key:
        try:
            from sendgrid import SendGridAPIClient
            from sendgrid.helpers.mail import Mail
            message = Mail(from_email=sender, to_emails=to,
                           subject=subject, plain_text_content=body)
            SendGridAPIClient(key).send(message)
            return
        except Exception:
            logger.exception("email delivery failed to=%s subject=%s", to, subject)
    logger.info("email (no SENDGRID configured) to=%s subject=%s", to, subject)


def _verify_email_message(link: str) -> tuple[str, str]:
    return ("Verify your E-ZFlow email",
            f"Welcome to E-ZFlow!\n\nVerify your email address:\n{link}\n\n"
            f"This link expires in 48 hours.")


def _reset_email_message(link: str) -> tuple[str, str]:
    return ("Reset your E-ZFlow password",
            f"A password reset was requested for your account.\n\n"
            f"Reset it here (valid for 60 minutes):\n{link}\n\n"
            f"If you did not request this, ignore this email — your password is unchanged.")


def _frontend_base() -> str:
    import os
    return os.getenv("FRONTEND_BASE_URL", "").rstrip("/")


def _issue_session(session: AsyncSession, user: UserModel, org: OrganizationModel) -> dict:
    access = create_access_token(user.id, org.id, user.role)
    refresh = generate_token_value()
    session.add(AuthTokenModel(
        token_hash=token_digest(refresh), user_id=user.id, token_type="refresh",
        expires_at=expiry_for("refresh")))
    return {
        "access_token": access,
        "token_type": "bearer",
        "expires_in": auth_mod.ACCESS_TOKEN_TTL_MINUTES * 60,
        "refresh_token": refresh,
        "user": {
            "id": user.id, "email": user.email, "full_name": user.full_name,
            "role": user.role, "email_verified": user.email_verified_at is not None,
        },
        "organization": {"id": org.id, "name": org.name, "slug": org.slug},
    }


DEFAULT_SKILLS = [
    ("General Labor", "junior", 25.0),
    ("Tradesperson", "standard", 45.0),
    ("Master Specialist", "master", 68.0),
]


@router.post("/signup", status_code=201)
async def signup(body: SignupRequest, request: Request,
                 session: AsyncSession = Depends(get_session)):
    """Create an organization + OWNER account (GATE 1 + GATE 9 step 1-2).

    Seeds default pricing settings, skill rates, a 14-day trial subscription,
    and queues the email-verification step.
    """
    ip = client_ip(request)
    if not signup_rate_limiter.allow(ip, "signup"):
        raise HTTPException(status_code=429, detail="Too many signup attempts")

    email = _valid_email(body.email)
    if err := validate_password_strength(body.password):
        raise HTTPException(status_code=422, detail=err)

    existing = await session.scalar(select(UserModel).where(UserModel.email == email))
    if existing:
        raise HTTPException(status_code=409, detail="An account with this email already exists")

    org = OrganizationModel(name=body.organization_name.strip(), slug=_slugify(body.organization_name))
    session.add(org)
    await session.flush()

    user = UserModel(
        organization_id=org.id, email=email,
        password_hash=hash_password(body.password),
        full_name=body.full_name.strip(), role="OWNER",
    )
    session.add(user)
    for name, level, rate in DEFAULT_SKILLS:
        session.add(SkillRateModel(organization_id=org.id, name=name, level=level,
                                   hourly_rate=rate))
    org.onboarding = ["company_created"]
    await session.flush()

    # 14-day trial so the org can work immediately (activation lifecycle in GATE 5)
    await get_billing_service().ensure_subscription(session, org.id)

    verify_token = await _store_token(session, user.id, "email_verify")
    await audit(session, "auth.signup", ctx=None, ip=ip,
                organization_id=org.id, user_id=user.id)
    await session.commit()

    base = _frontend_base()
    if base:
        subject, text = _verify_email_message(f"{base}/verify-email?token={verify_token}")
        await _send_email(email, subject, text)

    result = _issue_session(session, user, org)
    await session.commit()

    if REQUIRE_VERIFIED_EMAIL:
        result["email_verification_required"] = True
    # Dev convenience only: production never returns the token directly.
    if DEV_MODE():
        result["dev_email_verify_token"] = verify_token
    return result


@router.post("/login")
async def login(body: LoginRequest, request: Request,
                session: AsyncSession = Depends(get_session)):
    ip = client_ip(request)
    if not login_rate_limiter.allow(ip, "login"):
        raise HTTPException(status_code=429, detail="Too many login attempts — try again later")

    email = body.email.strip().lower()
    user = await session.scalar(select(UserModel).where(UserModel.email == email))
    generic = HTTPException(status_code=401, detail="Invalid email or password")

    if user is None:
        logger.info("security event=login_failed reason=unknown_user email_domain=%s ip=%s",
                    email.split("@")[-1], ip)
        raise generic
    if is_locked_out(user):
        await audit(session, "auth.login_blocked_lockout", ip=ip,
                    organization_id=user.organization_id, user_id=user.id)
        await session.commit()
        raise HTTPException(status_code=423, detail="Account temporarily locked — try again later")
    if not user.is_active:
        raise generic

    if not verify_password(body.password, user.password_hash):
        register_failed_login(user)
        await audit(session, "auth.login_failed", ip=ip,
                    organization_id=user.organization_id, user_id=user.id)
        await session.commit()
        logger.info("security event=login_failed reason=bad_password user=%s ip=%s", user.id, ip)
        raise generic

    register_successful_login(user)
    org = await session.get(OrganizationModel, user.organization_id)
    result = _issue_session(session, user, org)
    await audit(session, "auth.login", ip=ip,
                organization_id=user.organization_id, user_id=user.id)
    await session.commit()
    return result


@router.post("/refresh")
async def refresh(body: RefreshRequest,
                  session: AsyncSession = Depends(get_session)):
    digest = token_digest(body.refresh_token)
    row = await session.get(AuthTokenModel, digest)
    if (row is None or row.token_type != "refresh"
            or row.used_at is not None or row.expires_at < datetime.utcnow()):
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    user = await session.get(UserModel, row.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Account is not active")
    org = await session.get(OrganizationModel, user.organization_id)

    # Rotation: each refresh token is single-use.
    row.used_at = datetime.utcnow()
    result = _issue_session(session, user, org)
    await session.commit()
    return result


@router.post("/logout")
async def logout(body: LogoutRequest,
                 session: AsyncSession = Depends(get_session)):
    digest = token_digest(body.refresh_token)
    row = await session.get(AuthTokenModel, digest)
    if row and row.token_type == "refresh":
        row.used_at = datetime.utcnow()
    await session.commit()
    return {"status": "logged_out"}


@router.get("/me")
async def me(ctx: AuthContext = Depends(get_current_user),
             session: AsyncSession = Depends(get_session)):
    user = await session.get(UserModel, ctx.user_id)
    org = await session.get(OrganizationModel, ctx.organization_id)
    return {
        "user": {"id": user.id, "email": user.email, "full_name": user.full_name,
                 "role": user.role, "email_verified": user.email_verified_at is not None},
        "organization": {"id": org.id, "name": org.name, "slug": org.slug},
    }


@router.post("/verify-email")
async def verify_email(body: EmailTokenRequest,
                       session: AsyncSession = Depends(get_session)):
    digest = token_digest(body.token)
    row = await session.get(AuthTokenModel, digest)
    if (row is None or row.token_type != "email_verify"
            or row.used_at is not None or row.expires_at < datetime.utcnow()):
        raise HTTPException(status_code=400, detail="Invalid or expired verification link")

    row.used_at = datetime.utcnow()
    user = await session.get(UserModel, row.user_id)
    if user is None:
        raise HTTPException(status_code=400, detail="User no longer exists")
    user.email_verified_at = datetime.utcnow()
    await audit(session, "auth.email_verified", organization_id=user.organization_id,
                user_id=user.id)
    await session.commit()
    return {"status": "verified"}


@router.post("/resend-verification")
async def resend_verification(body: ForgotPasswordRequest, request: Request,
                              session: AsyncSession = Depends(get_session)):
    ip = client_ip(request)
    if not password_reset_rate_limiter.allow(ip, "resend"):
        raise HTTPException(status_code=429, detail="Too many attempts")

    email = _valid_email(body.email)
    user = await session.scalar(select(UserModel).where(UserModel.email == email))
    # Do not reveal whether the account exists.
    if user and user.email_verified_at is None:
        token = await _store_token(session, user.id, "email_verify")
        await session.commit()
        base = _frontend_base()
        if base:
            subject, text = _verify_email_message(f"{base}/verify-email?token={token}")
            await _send_email(email, subject, text)
        if DEV_MODE():
            return {"status": "sent", "dev_email_verify_token": token}
    return {"status": "sent"}


@router.post("/forgot-password")
async def forgot_password(body: ForgotPasswordRequest, request: Request,
                          session: AsyncSession = Depends(get_session)):
    ip = client_ip(request)
    if not password_reset_rate_limiter.allow(ip, "forgot"):
        raise HTTPException(status_code=429, detail="Too many attempts")

    email = _valid_email(body.email)
    user = await session.scalar(select(UserModel).where(UserModel.email == email))
    # Uniform response: never disclose account existence.
    if user and user.is_active:
        token = await _store_token(session, user.id, "password_reset")
        await audit(session, "auth.password_reset_requested", ip=ip,
                    organization_id=user.organization_id, user_id=user.id)
        await session.commit()
        base = _frontend_base()
        if base:
            subject, text = _reset_email_message(f"{base}/reset-password?token={token}")
            await _send_email(email, subject, text)
        if DEV_MODE():
            return {"status": "sent", "dev_reset_token": token}
    return {"status": "sent"}


@router.post("/reset-password")
async def reset_password(body: ResetPasswordRequest, request: Request,
                         session: AsyncSession = Depends(get_session)):
    if not password_reset_rate_limiter.allow(client_ip(request), "reset"):
        raise HTTPException(status_code=429, detail="Too many attempts")
    if err := validate_password_strength(body.new_password):
        raise HTTPException(status_code=422, detail=err)

    digest = token_digest(body.token)
    row = await session.get(AuthTokenModel, digest)
    if (row is None or row.token_type != "password_reset"
            or row.used_at is not None or row.expires_at < datetime.utcnow()):
        raise HTTPException(status_code=400, detail="Invalid or expired reset link")

    row.used_at = datetime.utcnow()
    user = await session.get(UserModel, row.user_id)
    if user is None:
        raise HTTPException(status_code=400, detail="User no longer exists")

    user.password_hash = hash_password(body.new_password)
    user.failed_login_count = 0
    user.locked_until = None

    # Revoke every outstanding refresh token for this user.
    tokens = (await session.execute(
        select(AuthTokenModel).where(AuthTokenModel.user_id == user.id,
                                     AuthTokenModel.token_type == "refresh",
                                     AuthTokenModel.used_at.is_(None)))).scalars().all()
    for t in tokens:
        t.used_at = datetime.utcnow()

    await audit(session, "auth.password_reset_completed", ip=client_ip(request),
                organization_id=user.organization_id, user_id=user.id)
    await session.commit()
    return {"status": "password_updated"}
