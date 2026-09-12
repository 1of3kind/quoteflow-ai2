"""AI assistant endpoint: natural language → executed workflow.

POST /assistant/command  {  "text": "Create a quote for John Smith.
    Replace the water heater and schedule it for Tuesday." }

Executes with the caller's identity: the underlying create_quote /
create_job handlers enforce the caller's role, org scoping, billing
quota, and write audit entries — the assistant cannot bypass any of it.
"""

import logging
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from core.assistant import parse_command
from core.database import get_session, OrgCustomerModel, SkillRateModel, UserModel
from core.pricing_engine import PricingInput, LaborLine, compute, PricingError
from core.rbac import can, require_permission
from core.tenancy import AuthContext, get_current_user, audit

logger = logging.getLogger("api.assistant")

router = APIRouter(prefix="/assistant", tags=["assistant"])

FALLBACK_RATE = {"junior": 25.0, "standard": 45.0, "master": 68.0}
DEFAULT_TRADE_SKILL = {
    "landscaping": ("General Labor", "junior"),
    "roofing": ("Tradesperson", "standard"),
    "plumbing": ("Tradesperson", "standard"),
    "electrical": ("Master Specialist", "master"),
    "autobody": ("Master Specialist", "master"),
}


class CommandIn(BaseModel):
    text: str = Field(min_length=2, max_length=2000)


@router.post("/command")
async def run_command(body: CommandIn, request: Request,
                      ctx: AuthContext = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    """Interpret and execute: find/create customer → create quote →
    (optionally) schedule → (optionally) send."""
    cmd = await parse_command(body.text)
    actions = []

    if not cmd.customer_name or not cmd.trade:
        await audit(session, "assistant.unparsed", ctx=ctx, text=body.text[:120])
        await session.commit()
        return {
            "understood": cmd.__dict__,
            "actions": [],
            "summary": ("I couldn't fully understand that. "
                        + " ".join(cmd.notes)) if cmd.notes else "Command not understood.",
        }

    # ── 1. Find or create the customer (org-scoped) ──
    customer = (await session.execute(
        select(OrgCustomerModel).where(
            OrgCustomerModel.organization_id == ctx.organization_id,
            func.lower(OrgCustomerModel.name) == cmd.customer_name.lower()
        ).limit(1))).scalar()
    if customer:
        actions.append({"action": "customer_found",
                        "customer_id": customer.id, "name": customer.name})
    else:
        customer = OrgCustomerModel(organization_id=ctx.organization_id,
                                    name=cmd.customer_name)
        session.add(customer)
        await session.flush()
        actions.append({"action": "customer_created",
                        "customer_id": customer.id, "name": customer.name})

    # ── 2. Build the labor line from the org's own skill rates ──
    skill_name, skill_level = DEFAULT_TRADE_SKILL.get(cmd.trade, ("Tradesperson", "standard"))
    rate = FALLBACK_RATE[skill_level]
    skill = (await session.execute(
        select(SkillRateModel).where(
            SkillRateModel.organization_id == ctx.organization_id,
            SkillRateModel.level == skill_level,
            SkillRateModel.is_active.is_(True))
        .order_by(SkillRateModel.hourly_rate.desc()).limit(1))).scalar()
    if skill:
        rate = skill.hourly_rate
        skill_name = skill.name

    hours = cmd.hours if cmd.hours else 4.0
    pricing = PricingInput(
        labor_lines=[LaborLine(skill_name=skill_name, skill_level=skill_level,
                               workers=1, hours=hours, hourly_rate=rate)],
        material_lines=[],
        title=f"{cmd.trade.title()} — {cmd.description[:80]}",
    )
    try:
        result = compute(pricing)
    except PricingError as e:
        actions.append({"action": "error", "detail": str(e)})
        await session.commit()
        return {"understood": cmd.__dict__, "actions": actions,
                "summary": f"Could not price this job: {e}"}

    # ── 3. Create the quote through the real handler (RBAC + quota + audit) ──
    from api.routes.quotes_routes import CreateQuoteIn, create_quote
    quote_body = CreateQuoteIn(
        org_customer_id=customer.id, trade=cmd.trade,
        title=f"{cmd.trade.title()} for {customer.name}",
        description=cmd.description,
        labor_lines=[{
            "skill_name": skill_name, "skill_level": skill_level,
            "workers": 1, "hours": hours, "hourly_rate": rate,
        }],
    )
    quote_out = await create_quote(quote_body, ctx, session)
    actions.append({"action": "quote_created", "quote_id": quote_out["quote_id"],
                    "recommended_price": quote_out["result"]["recommended_price"],
                    "narrative": quote_out["result"]["narrative"]})

    # ── 4. Schedule the job if a date was understood ──
    job_out = None
    if cmd.schedule_date:
        from api.routes.jobs_routes import JobCreateIn, create_job
        scheduled = datetime.fromisoformat(cmd.schedule_date)
        job_body = JobCreateIn(
            org_customer_id=customer.id, trade=cmd.trade,
            title=cmd.description[:100],
            description=cmd.description,
            quote_id=quote_out["quote_id"],
            scheduled_date=scheduled,
            duration_hours=hours,
        )
        job_out = await create_job(job_body, ctx, session)
        actions.append({"action": "job_scheduled", "job_id": job_out["id"],
                        "scheduled_date": job_out["scheduled_date"]})

    # ── 5. Send the quote if requested ──
    if cmd.send_quote:
        from api.routes.quotes_routes import send_quote as send_quote_handler
        sent = await send_quote_handler(quote_out["quote_id"], request, ctx, session)
        actions.append({"action": "quote_sent", "quote_id": quote_out["quote_id"]})

    await audit(session, "assistant.command_executed", ctx=ctx,
                actions=[a["action"] for a in actions])
    await session.commit()

    summary = (f"Created a {cmd.trade} quote for {customer.name} at "
               f"${quote_out['result']['recommended_price']:,.2f}.")
    if job_out:
        summary += f" Scheduled for {cmd.schedule_date}."
    if cmd.send_quote:
        summary += " Quote sent to the customer."

    return {"understood": cmd.__dict__, "actions": actions, "summary": summary}
