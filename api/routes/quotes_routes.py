"""Quote endpoints: pricing-engine-backed quote creation, explanation,
customer-facing document, and the approve → job → materials → schedule
workflow (GATE 3, GATE 10). Every query is org-scoped (GATE 1)."""

import logging
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import (
    get_session, OrganizationModel, QuoteModel, JobModel, OrgCustomerModel,
    MaterialOrderModel, DocumentModel,
)
from core.pricing_engine import (
    PricingInput, LaborLine, MaterialLine, compute, recompute, PricingError,
)
from core.rbac import require_permission
from core.tenancy import AuthContext, get_current_user, scoped_or_404, audit
from payments.billing import get_billing_service, BillingError

logger = logging.getLogger("api.quotes")

router = APIRouter(prefix="/quotes", tags=["quotes"])

QUOTE_STATUSES = {"draft", "sent", "accepted", "expired", "declined"}


class LaborLineIn(BaseModel):
    skill_name: str = Field(min_length=1, max_length=120)
    skill_level: str = Field(default="standard")
    workers: int = Field(default=1, ge=1, le=100)
    hours: float = Field(ge=0, le=10000)
    hourly_rate: float = Field(ge=0, le=10000)
    skill_id: str | None = None  # reference to an org skill rate (audit trail)


class MaterialLineIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    unit_cost: float = Field(ge=0, le=10_000_000)
    quantity: float = Field(default=1, ge=0, le=1_000_000)
    unit: str = "each"


class QuoteOverrides(BaseModel):
    """Per-quote adjustments. Only pricing:override roles may set these."""
    overhead_pct: float | None = Field(default=None, ge=0, le=0.9)
    profit_margin_pct: float | None = Field(default=None, ge=0, lt=0.9)
    tax_rate: float | None = Field(default=None, ge=0, le=0.5)


class CreateQuoteIn(BaseModel):
    org_customer_id: str
    trade: str = Field(min_length=2, max_length=64)
    title: str = ""
    description: str = ""
    labor_lines: list[LaborLineIn]
    material_lines: list[MaterialLineIn] = Field(default_factory=list)
    overrides: QuoteOverrides | None = None
    valid_days: int | None = Field(default=None, ge=1, le=365)


@router.post("", status_code=201)
async def create_quote(body: CreateQuoteIn,
                       ctx: AuthContext = Depends(require_permission("quotes:write")),
                       session: AsyncSession = Depends(get_session)):
    # Billing gate (GATE 5): quota + subscription state checked server-side.
    allowance = await get_billing_service().check_quote_allowance(session, ctx.organization_id)
    if not allowance["allowed"]:
        raise HTTPException(status_code=402, detail={
            "message": "Subscription does not allow quote creation",
            "state": allowance.get("state"), "reason": allowance.get("reason"),
        })

    # Tenant check on the customer this quote belongs to (GATE 1).
    customer = await scoped_or_404(session, OrgCustomerModel, body.org_customer_id,
                                   ctx, "Customer")

    org = await session.get(OrganizationModel, ctx.organization_id)
    settings = org.settings

    overrides_applied = {}
    if body.overrides and (body.overrides.overhead_pct is not None
                           or body.overrides.profit_margin_pct is not None
                           or body.overrides.tax_rate is not None):
        # Managers may override per quote; employees may not (RBAC table).
        from core.rbac import can
        if not can(ctx.role, "pricing:override"):
            raise HTTPException(status_code=403,
                                detail="Role EMPLOYEE may not override pricing")
        overrides_applied = body.overrides.model_dump(exclude_none=True)

    overhead_pct = overrides_applied.get("overhead_pct", settings.get("overhead_pct", 0.15))
    margin_pct = overrides_applied.get("profit_margin_pct", settings.get("profit_margin_pct", 0.20))
    tax_rate = overrides_applied.get("tax_rate", settings.get("tax_rate", 0.08))
    valid_days = body.valid_days or settings.get("valid_days", 14)

    pricing_input = PricingInput(
        labor_lines=[LaborLine(**l.model_dump(exclude={"skill_id"})) for l in body.labor_lines],
        material_lines=[MaterialLine(**m.model_dump()) for m in body.material_lines],
        overhead_pct=overhead_pct,
        profit_margin_pct=margin_pct,
        tax_rate=tax_rate,
        title=body.title,
    )
    try:
        result = compute(pricing_input)
    except PricingError as e:
        raise HTTPException(status_code=422, detail=str(e))

    quote_id = f"Q-{secrets.token_hex(6).upper()}"
    quote = QuoteModel(
        quote_id=quote_id,
        organization_id=ctx.organization_id,
        org_customer_id=customer.id,
        trade=body.trade.strip().lower(),
        customer_id=customer.id,
        title=body.title or f"{body.trade.title()} quote for {customer.name}",
        line_items=[*result.labor_lines, *result.material_lines],
        subtotal=result.recommended_price,
        tax_rate=tax_rate,
        tax_amount=result.tax_amount,
        total=result.total_with_tax,
        breakdown=result.as_dict(),
        engine_input=pricing_input.snapshot(),   # reproducibility (GATE 3)
        engine_result=result.as_dict(),
        valid_days=valid_days,
        expires_at=datetime.utcnow() + timedelta(days=valid_days),
        status="draft",
        notes=body.description,
        created_by=ctx.user_id,
    )
    session.add(quote)

    org2 = await session.get(OrganizationModel, ctx.organization_id)
    done = set(org2.onboarding or [])
    done.add("first_quote_generated")
    org2.onboarding = [s for s in org2.onboarding or [] if s != "first_quote_generated"] + ["first_quote_generated"]

    await get_billing_service().record_quote_usage(session, ctx.organization_id)
    await audit(session, "quote.created", ctx=ctx, quote_id=quote_id,
                recommended_price=result.recommended_price)
    await session.commit()

    return {
        "quote_id": quote_id,
        "status": "draft",
        "customer": {"id": customer.id, "name": customer.name},
        "result": result.as_dict(),
        "explanation": result.explanation_text(),
        "expires_at": quote.expires_at.isoformat(),
    }


def _quote_out(q: QuoteModel, customer_name: str | None = None) -> dict:
    return {
        "quote_id": q.quote_id,
        "status": q.status,
        "trade": q.trade,
        "title": q.title,
        "customer_id": q.org_customer_id,
        "customer_name": customer_name,
        "recommended_price": q.subtotal,
        "tax_amount": q.tax_amount,
        "total": q.total,
        "valid_days": q.valid_days,
        "expires_at": q.expires_at.isoformat() if q.expires_at else None,
        "job_id": q.job_id,
        "created_at": q.created_at.isoformat(),
    }


async def _customer_name(session: AsyncSession, q: QuoteModel) -> str | None:
    if not q.org_customer_id:
        return None
    c = await session.get(OrgCustomerModel, q.org_customer_id)
    return c.name if c else None


@router.get("")
async def list_quotes(status: str | None = None, trade: str | None = None,
                      limit: int = Query(50, ge=1, le=500),
                      offset: int = Query(0, ge=0),
                      ctx: AuthContext = Depends(require_permission("quotes:read")),
                      session: AsyncSession = Depends(get_session)):
    stmt = select(QuoteModel).where(QuoteModel.organization_id == ctx.organization_id)
    if status:
        if status not in QUOTE_STATUSES:
            raise HTTPException(status_code=422, detail=f"status must be one of {sorted(QUOTE_STATUSES)}")
        stmt = stmt.where(QuoteModel.status == status)
    if trade:
        stmt = stmt.where(QuoteModel.trade == trade.lower())
    stmt = stmt.order_by(QuoteModel.created_at.desc()).offset(offset).limit(limit)
    quotes = (await session.execute(stmt)).scalars().all()
    return {"quotes": [await _wrap(q, session) for q in quotes]}


async def _wrap(q: QuoteModel, session: AsyncSession) -> dict:
    out = _quote_out(q)
    out["customer_name"] = await _customer_name(session, q)
    return out


@router.get("/{quote_id}")
async def get_quote(quote_id: str,
                    ctx: AuthContext = Depends(require_permission("quotes:read")),
                    session: AsyncSession = Depends(get_session)):
    q = await scoped_or_404(session, QuoteModel, quote_id, ctx, "Quote")
    out = await _wrap(q, session)
    out["result"] = q.engine_result
    return out


@router.get("/{quote_id}/explain")
async def explain_quote(quote_id: str,
                        ctx: AuthContext = Depends(require_permission("quotes:read")),
                        session: AsyncSession = Depends(get_session)):
    """Why did E-ZFlow give this price? Reproduce it from the stored
    snapshot and return the full breakdown (GATE 3)."""
    q = await scoped_or_404(session, QuoteModel, quote_id, ctx, "Quote")
    try:
        replayed = recompute(q.engine_input)
    except PricingError as e:
        raise HTTPException(status_code=500, detail=f"Stored snapshot is invalid: {e}")
    # The replay must match what was stored — otherwise data was tampered with.
    if abs(replayed.recommended_price - (q.engine_result or {}).get("recommended_price", -1)) > 0.011:
        raise HTTPException(status_code=500, detail="Stored quote does not match its snapshot")
    return {
        "quote_id": q.quote_id,
        "recomputed": replayed.as_dict(),
        "explanation": replayed.explanation_text(),
        "narrative": replayed.narrative,
        "snapshot": q.engine_input,
    }


@router.post("/{quote_id}/send")
async def send_quote(quote_id: str, request: Request,
                     ctx: AuthContext = Depends(require_permission("quotes:write")),
                     session: AsyncSession = Depends(get_session)):
    """Send the quote and mint the customer approval token (MUST-FIX #2).

    The customer approves via /q/{quote_id}/{token} — no employee login.
    Only the SHA-256 hash of the token is stored; the raw token exists only
    in the approval URL handed to the customer.
    """
    import os
    from core.auth import generate_token_value, token_digest

    q = await scoped_or_404(session, QuoteModel, quote_id, ctx, "Quote")
    if q.status not in ("draft", "sent"):
        raise HTTPException(status_code=409, detail=f"Cannot send a quote in status {q.status}")
    q.status = "sent"
    q.sent_at = datetime.utcnow()

    # Issue (or re-issue on resend) the single-purpose approval token.
    raw_token = generate_token_value()
    q.approval_token_hash = token_digest(raw_token)
    q.approval_token_expires_at = q.expires_at or (
        datetime.utcnow() + timedelta(days=q.valid_days or 14))
    q.approval_revoked_at = None

    base_url = os.getenv("PUBLIC_BASE_URL", "").rstrip("/") or str(request.base_url).rstrip("/")
    approval_url = f"{base_url}/q/{q.quote_id}/{raw_token}"

    document = await build_quote_document(session, q, approval_url=approval_url)
    session.add(DocumentModel(organization_id=ctx.organization_id,
                              related_type="quote", related_id=q.quote_id,
                              content=document))
    await audit(session, "quote.sent", ctx=ctx, quote_id=q.quote_id)
    await session.commit()
    return {"status": "sent", "quote_id": q.quote_id,
            "approval_url": approval_url, "document": document}


async def build_quote_document(session: AsyncSession, q: QuoteModel,
                               approval_url: str | None = None) -> dict:
    """The professional customer-facing quote (GATE 10)."""
    org = await session.get(OrganizationModel, q.organization_id)
    customer = await session.get(OrgCustomerModel, q.org_customer_id) if q.org_customer_id else None
    result = q.engine_result or {}
    info = (org.business_info or {}) if org else {}
    return {
        "company": {
            "name": org.name if org else "",
            "logo_url": info.get("logo_url", ""),
            "address": info.get("address", ""),
            "phone": info.get("phone", ""),
            "email": info.get("email", ""),
            "license_number": info.get("license_number", ""),
        },
        "quote": {
            "quote_id": q.quote_id,
            "date_issued": q.sent_at.isoformat() if q.sent_at else datetime.utcnow().isoformat(),
            "valid_until": q.expires_at.isoformat() if q.expires_at else None,
            "title": q.title,
            "description": q.notes or "",
            "terms": (org.settings or {}).get("terms", "") if org else "",
        },
        "customer": {
            "name": customer.name if customer else "",
            "email": customer.email if customer else None,
            "phone": customer.phone if customer else None,
            "address": customer.address if customer else None,
        },
        "labor_lines": result.get("labor_lines", []),
        "material_lines": result.get("material_lines", []),
        "totals": {
            "labor_cost": result.get("labor_cost", 0),
            "material_cost": result.get("material_cost", 0),
            "overhead": result.get("overhead", 0),
            "profit": result.get("profit", 0),
            "recommended_price": result.get("recommended_price", q.subtotal),
            "tax_amount": result.get("tax_amount", q.tax_amount),
            "total_with_tax": result.get("total_with_tax", q.total),
            "margin": result.get("margin", 0),
        },
        "explanation": result.get("explanation", []),
        "narrative": result.get("narrative", ""),
        "approval": {
            "url": approval_url,
            "accept_endpoint": f"/q/{q.quote_id}/<token>/approve",
            "instructions": "Click the approval link (or reply ACCEPT) to book this work. The link is valid until the quote expires.",
        },
    }


async def _accept_quote_for_customer(session: AsyncSession, q: QuoteModel,
                                     via: str, user_id: str | None = None) -> dict:
    """Shared acceptance path: customer link, SMS reply, or an employee.

    Validates state and expiry, marks accepted, creates the job, and bumps
    onboarding. Caller commits. (MUST-FIX #2: one authoritative path so a
    customer's approval really accepts the quote.)
    """
    if q.status == "accepted":
        return {"status": "already_accepted", "quote_id": q.quote_id, "job_id": q.job_id}
    if q.status != "sent":
        raise HTTPException(status_code=409,
                            detail=f"Only sent quotes can be accepted (current: {q.status})")
    if q.expires_at and q.expires_at < datetime.utcnow():
        q.status = "expired"
        await session.commit()
        raise HTTPException(status_code=410, detail="Quote has expired")

    q.status = "accepted"
    q.accepted_at = datetime.utcnow()
    q.approved_via = via

    job = JobModel(
        organization_id=q.organization_id,
        org_customer_id=q.org_customer_id,
        quote_id=q.quote_id,
        trade=q.trade,
        title=q.title,
        description=q.notes,
        status="draft",
        created_by=user_id,
    )
    session.add(job)
    await session.flush()
    q.job_id = job.id

    org = await session.get(OrganizationModel, q.organization_id)
    if org:
        done = set(org.onboarding or [])
        done.add("first_quote_approved")
        org.onboarding = list(done)

    from core.tenancy import audit
    await audit(session, "quote.accepted",
                organization_id=q.organization_id, user_id=user_id,
                quote_id=q.quote_id, job_id=job.id, via=via)
    await session.commit()

    return {
        "status": "accepted",
        "quote_id": q.quote_id,
        "job_id": job.id,
        "next_steps": [
            "POST /jobs/{job_id} to schedule execution",
            "POST /quotes/{quote_id}/materials-order to stage materials",
        ],
    }


@router.post("/{quote_id}/accept")
async def accept_quote(quote_id: str,
                       ctx: AuthContext = Depends(require_permission("quotes:write")),
                       session: AsyncSession = Depends(get_session)):
    """Employee-side acceptance. Customers approve via /q/{quote_id}/{token}."""
    q = await scoped_or_404(session, QuoteModel, quote_id, ctx, "Quote")
    return await _accept_quote_for_customer(session, q, via="api", user_id=ctx.user_id)


@router.post("/{quote_id}/revoke-approval")
async def revoke_approval(quote_id: str,
                          ctx: AuthContext = Depends(require_permission("quotes:write")),
                          session: AsyncSession = Depends(get_session)):
    """Revoke the customer approval link: the stored token hash is cleared
    and any outstanding link stops working immediately."""
    q = await scoped_or_404(session, QuoteModel, quote_id, ctx, "Quote")
    q.approval_revoked_at = datetime.utcnow()
    q.approval_token_hash = None
    await audit(session, "quote.approval_revoked", ctx=ctx, quote_id=q.quote_id)
    await session.commit()
    return {"status": "revoked", "quote_id": q.quote_id}


@router.post("/{quote_id}/materials-order", status_code=201)
async def create_materials_order(quote_id: str,
                                 ctx: AuthContext = Depends(require_permission("orders:write")),
                                 session: AsyncSession = Depends(get_session)):
    """Turn the quote's material lines into a staged pickup order."""
    q = await scoped_or_404(session, QuoteModel, quote_id, ctx, "Quote")
    if q.status != "accepted":
        raise HTTPException(status_code=409, detail="Materials are ordered after quote acceptance")
    existing = await session.scalar(select(MaterialOrderModel).where(
        MaterialOrderModel.quote_id == q.quote_id))
    if existing:
        return {"status": "already_exists", "order_id": existing.order_id}

    materials = (q.engine_result or {}).get("material_lines", [])
    if not materials:
        return {"status": "no_materials_needed"}

    subtotal = sum(m["cost"] for m in materials)
    order = MaterialOrderModel(
        order_id=f"ORD-{secrets.token_hex(5).upper()}",
        organization_id=ctx.organization_id,
        quote_id=q.quote_id,
        subtotal=subtotal,
        items=materials,
    )
    session.add(order)
    await audit(session, "materials.order_created", ctx=ctx, quote_id=q.quote_id)
    await session.commit()
    return {"status": "order_created", "order_id": order.order_id,
            "items": materials, "subtotal": round(subtotal, 2)}


@router.get("/{quote_id}/document")
async def quote_document(quote_id: str,
                         ctx: AuthContext = Depends(require_permission("documents:read")),
                         session: AsyncSession = Depends(get_session)):
    q = await scoped_or_404(session, QuoteModel, quote_id, ctx, "Quote")
    document = await build_quote_document(session, q)
    return document
