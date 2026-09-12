"""Organization endpoints: settings, pricing defaults, skill rates, users,
customers, and the onboarding checklist (GATE 1 scoping, GATE 2 RBAC,
GATE 9 repeatable onboarding)."""

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import hash_password, client_ip, validate_password_strength, generate_token_value, token_digest, expiry_for
from core.database import (
    get_session, OrganizationModel, UserModel, SkillRateModel,
    OrgCustomerModel, AuthTokenModel, JobModel, ONBOARDING_STEPS,
    DEFAULT_ORG_SETTINGS, count_scoped,
)
from core.rbac import Role, require_permission, ALL_ROLES
from core.tenancy import AuthContext, get_current_user, scoped_or_404, audit
from api.routes.auth_routes import _send_email, _frontend_base, _valid_email

logger = logging.getLogger("api.org")

router = APIRouter(prefix="/org", tags=["organization"])


# ─── Organization profile & settings ────────────────────────────────────────

class BusinessInfoIn(BaseModel):
    address: str = ""
    phone: str = ""
    email: str = ""
    website: str = ""
    logo_url: str = ""
    license_number: str = ""


class PricingSettingsIn(BaseModel):
    overhead_pct: float = Field(ge=0, le=0.9)
    profit_margin_pct: float = Field(ge=0, lt=0.9)
    tax_rate: float = Field(ge=0, le=0.5, default=0.08)
    material_markup_pct: float = Field(ge=0, le=2.0, default=0.0)
    valid_days: int = Field(ge=1, le=365, default=14)
    terms: str = ""


@router.get("")
async def get_org(ctx: AuthContext = Depends(get_current_user),
                  session: AsyncSession = Depends(get_session)):
    org = await session.get(OrganizationModel, ctx.organization_id)
    return {
        "id": org.id, "name": org.name, "slug": org.slug,
        "business_info": org.business_info, "settings": org.settings,
        "onboarding": org.onboarding, "created_at": org.created_at.isoformat(),
    }


@router.patch("/business-info")
async def update_business_info(
        body: BusinessInfoIn,
        ctx: AuthContext = Depends(require_permission("org:settings")),
        session: AsyncSession = Depends(get_session)):
    org = await session.get(OrganizationModel, ctx.organization_id)
    org.business_info = body.model_dump()
    _onboarding_done(org, "business_info")
    await audit(session, "org.business_info_updated", ctx=ctx)
    await session.commit()
    return {"status": "updated", "business_info": org.business_info}


@router.get("/pricing")
async def get_pricing(ctx: AuthContext = Depends(require_permission("org:read")),
                      session: AsyncSession = Depends(get_session)):
    org = await session.get(OrganizationModel, ctx.organization_id)
    return {"settings": org.settings}


@router.patch("/pricing")
async def update_pricing(
        body: PricingSettingsIn,
        ctx: AuthContext = Depends(require_permission("pricing:configure")),
        session: AsyncSession = Depends(get_session)):
    if body.overhead_pct + body.profit_margin_pct >= 0.95:
        raise HTTPException(status_code=422,
                            detail="overhead_pct + profit_margin_pct must be < 0.95")
    org = await session.get(OrganizationModel, ctx.organization_id)
    org.settings = {**org.settings, **body.model_dump()}
    _onboarding_done(org, "pricing_configured", "overhead_configured",
                     "material_settings_configured")
    await audit(session, "org.pricing_updated", ctx=ctx,
                overhead_pct=body.overhead_pct, profit_margin_pct=body.profit_margin_pct)
    await session.commit()
    return {"status": "updated", "settings": org.settings}


# ─── Skill rates (labor) ────────────────────────────────────────────────────

class SkillIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    level: str = Field(default="standard", pattern="^(junior|standard|master)$")
    hourly_rate: float = Field(gt=0, le=10000)


class SkillUpdate(BaseModel):
    name: str | None = None
    level: str | None = Field(default=None, pattern="^(junior|standard|master)$")
    hourly_rate: float | None = Field(default=None, gt=0, le=10000)
    is_active: bool | None = None


@router.get("/skills")
async def list_skills(ctx: AuthContext = Depends(require_permission("org:read")),
                      session: AsyncSession = Depends(get_session)):
    skills = (await session.execute(
        select(SkillRateModel).where(SkillRateModel.organization_id == ctx.organization_id)
        .order_by(SkillRateModel.hourly_rate))).scalars().all()
    return {"skills": [{
        "id": s.id, "name": s.name, "level": s.level,
        "hourly_rate": s.hourly_rate, "is_active": s.is_active,
    } for s in skills]}


@router.post("/skills", status_code=201)
async def create_skill(body: SkillIn,
                       ctx: AuthContext = Depends(require_permission("pricing:configure")),
                       session: AsyncSession = Depends(get_session)):
    skill = SkillRateModel(organization_id=ctx.organization_id, name=body.name.strip(),
                           level=body.level, hourly_rate=body.hourly_rate)
    session.add(skill)
    org = await session.get(OrganizationModel, ctx.organization_id)
    _onboarding_done(org, "skills_configured", "labor_rates_configured")
    await audit(session, "org.skill_created", ctx=ctx, skill=body.name)
    await session.commit()
    return {"id": skill.id, "name": skill.name, "level": skill.level,
            "hourly_rate": skill.hourly_rate}


@router.patch("/skills/{skill_id}")
async def update_skill(skill_id: str, body: SkillUpdate,
                       ctx: AuthContext = Depends(require_permission("pricing:configure")),
                       session: AsyncSession = Depends(get_session)):
    skill = await scoped_or_404(session, SkillRateModel, skill_id, ctx, "Skill")
    if body.name is not None:
        skill.name = body.name.strip()
    if body.level is not None:
        skill.level = body.level
    if body.hourly_rate is not None:
        skill.hourly_rate = body.hourly_rate
    if body.is_active is not None:
        skill.is_active = body.is_active
    org = await session.get(OrganizationModel, ctx.organization_id)
    _onboarding_done(org, "skills_configured", "labor_rates_configured")
    await audit(session, "org.skill_updated", ctx=ctx, skill_id=skill_id)
    await session.commit()
    return {"status": "updated", "id": skill.id}


@router.delete("/skills/{skill_id}", status_code=204)
async def deactivate_skill(skill_id: str,
                           ctx: AuthContext = Depends(require_permission("pricing:configure")),
                           session: AsyncSession = Depends(get_session)):
    skill = await scoped_or_404(session, SkillRateModel, skill_id, ctx, "Skill")
    skill.is_active = False
    await audit(session, "org.skill_deactivated", ctx=ctx, skill_id=skill_id)
    await session.commit()


# ─── Users & roles ──────────────────────────────────────────────────────────

class InviteUserIn(BaseModel):
    email: str
    full_name: str = ""
    role: str
    password: str


class UpdateUserIn(BaseModel):
    role: str | None = None
    is_active: bool | None = None
    full_name: str | None = None


@router.get("/users")
async def list_users(ctx: AuthContext = Depends(require_permission("org:users")),
                     session: AsyncSession = Depends(get_session)):
    users = (await session.execute(
        select(UserModel).where(UserModel.organization_id == ctx.organization_id))).scalars().all()
    return {"users": [{
        "id": u.id, "email": u.email, "full_name": u.full_name, "role": u.role,
        "is_active": u.is_active, "email_verified": u.email_verified_at is not None,
        "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
    } for u in users]}


@router.post("/users", status_code=201)
async def invite_user(body: InviteUserIn, request: Request,
                      ctx: AuthContext = Depends(require_permission("org:users")),
                      session: AsyncSession = Depends(get_session)):
    if body.role not in ALL_ROLES:
        raise HTTPException(status_code=422, detail=f"role must be one of {sorted(ALL_ROLES)}")
    if body.role == Role.OWNER.value:
        # Ownership transfers are explicit, not an invite side effect.
        raise HTTPException(status_code=422, detail="Cannot invite another OWNER")
    if err := validate_password_strength(body.password):
        raise HTTPException(status_code=422, detail=err)
    email = _valid_email(body.email)

    existing = await session.scalar(select(UserModel).where(UserModel.email == email))
    if existing:
        raise HTTPException(status_code=409, detail="Email already in use")

    user = UserModel(
        organization_id=ctx.organization_id, email=email,
        password_hash=hash_password(body.password),
        full_name=body.full_name.strip(), role=body.role, invited_by=ctx.user_id)
    session.add(user)
    await session.flush()

    verify_token = generate_token_value()
    session.add(AuthTokenModel(token_hash=token_digest(verify_token), user_id=user.id,
                               token_type="email_verify", expires_at=expiry_for("email_verify")))

    org = await session.get(OrganizationModel, ctx.organization_id)
    _onboarding_done(org, "employees_invited")
    await audit(session, "org.user_invited", ctx=ctx, ip=client_ip(request),
                invited_email=email, role=body.role)
    await session.commit()

    import os
    base = _frontend_base()
    if base:
        await _send_email(email, "Your QuoteFlow invite",
                          f"You've been invited to {org.name} on QuoteFlow.\n"
                          f"Verify your email: {base}/verify-email?token={verify_token}\n"
                          f"Then sign in with the password your administrator gave you.")
    return {"id": user.id, "email": user.email, "role": user.role}


@router.patch("/users/{user_id}")
async def update_user(user_id: str, body: UpdateUserIn,
                      ctx: AuthContext = Depends(require_permission("org:users")),
                      session: AsyncSession = Depends(get_session)):
    user = await scoped_or_404(session, UserModel, user_id, ctx, "User")
    if user.id == ctx.user_id and body.role is not None and body.role != Role.OWNER.value:
        raise HTTPException(status_code=422, detail="Owners cannot demote themselves")
    if body.role is not None:
        if body.role not in ALL_ROLES:
            raise HTTPException(status_code=422, detail=f"role must be one of {sorted(ALL_ROLES)}")
        user.role = body.role
    if body.is_active is not None:
        user.is_active = body.is_active
    if body.full_name is not None:
        user.full_name = body.full_name.strip()
    await audit(session, "org.user_updated", ctx=ctx, target_user=user_id,
                role=body.role, is_active=body.is_active)
    await session.commit()
    return {"status": "updated", "id": user.id, "role": user.role,
            "is_active": user.is_active}


# ─── Customers (the tenant's own customers) ─────────────────────────────────

class CustomerIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    notes: str | None = None


@router.get("/customers")
async def list_customers(ctx: AuthContext = Depends(require_permission("customers:read")),
                         session: AsyncSession = Depends(get_session)):
    customers = (await session.execute(
        select(OrgCustomerModel)
        .where(OrgCustomerModel.organization_id == ctx.organization_id)
        .order_by(OrgCustomerModel.created_at.desc()))).scalars().all()
    return {"customers": [{
        "id": c.id, "name": c.name, "email": c.email, "phone": c.phone,
        "address": c.address, "created_at": c.created_at.isoformat(),
    } for c in customers]}


@router.post("/customers", status_code=201)
async def create_customer(body: CustomerIn,
                          ctx: AuthContext = Depends(require_permission("customers:write")),
                          session: AsyncSession = Depends(get_session)):
    customer = OrgCustomerModel(organization_id=ctx.organization_id, **body.model_dump())
    session.add(customer)
    await audit(session, "org.customer_created", ctx=ctx, customer=body.name)
    await session.commit()
    return {"id": customer.id, "name": customer.name}


@router.get("/customers/{customer_id}")
async def get_customer(customer_id: str,
                       ctx: AuthContext = Depends(require_permission("customers:read")),
                       session: AsyncSession = Depends(get_session)):
    c = await scoped_or_404(session, OrgCustomerModel, customer_id, ctx, "Customer")
    return {"id": c.id, "name": c.name, "email": c.email, "phone": c.phone,
            "address": c.address, "notes": c.notes,
            "created_at": c.created_at.isoformat()}


@router.patch("/customers/{customer_id}")
async def update_customer(customer_id: str, body: CustomerIn,
                          ctx: AuthContext = Depends(require_permission("customers:write")),
                          session: AsyncSession = Depends(get_session)):
    c = await scoped_or_404(session, OrgCustomerModel, customer_id, ctx, "Customer")
    for key, value in body.model_dump().items():
        setattr(c, key, value)
    await audit(session, "org.customer_updated", ctx=ctx, customer_id=customer_id)
    await session.commit()
    return {"status": "updated", "id": c.id}


@router.delete("/customers/{customer_id}", status_code=204)
async def delete_customer(customer_id: str,
                          ctx: AuthContext = Depends(require_permission("customers:write")),
                          session: AsyncSession = Depends(get_session)):
    c = await scoped_or_404(session, OrgCustomerModel, customer_id, ctx, "Customer")
    await session.delete(c)
    await audit(session, "org.customer_deleted", ctx=ctx, customer_id=customer_id)
    await session.commit()


# ─── Onboarding (GATE 9) ────────────────────────────────────────────────────

@router.get("/onboarding")
async def get_onboarding(ctx: AuthContext = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    org = await session.get(OrganizationModel, ctx.organization_id)
    done = set(org.onboarding or [])
    skills = await count_scoped(session, SkillRateModel, ctx.organization_id)
    users = await count_scoped(session, UserModel, ctx.organization_id)
    jobs = (await session.execute(
        select(JobModel.id).where(JobModel.organization_id == ctx.organization_id,
                                  JobModel.status != "draft").limit(1))).scalar()
    defaults_done = {  # satisfied out-of-the-box by signup seeding
        "pricing_configured", "skills_configured", "labor_rates_configured",
        "overhead_configured", "material_settings_configured",
    }
    return {
        "steps": [
            {"step": s, "done": s in done or s in defaults_done}
            for s in ONBOARDING_STEPS
        ],
        "summary": {
            "company_created": True,
            "users_count": users,
            "skills_count": skills,
            "has_non_draft_job": jobs is not None,
        },
    }


def _onboarding_done(org: OrganizationModel, *steps: str) -> None:
    done = set(org.onboarding or [])
    done.update(steps)
    org.onboarding = [s for s in ONBOARDING_STEPS if s in done]
