"""Background tasks for async image processing and quote generation.

Photo quotes are calculated by the authoritative pricing engine
(core.pricing_engine) with the organization's own settings and skill rates,
persisted with a full reproducibility snapshot, and usage-metered against
the org's subscription.
"""

import os
import logging
import secrets
from datetime import datetime, timedelta
from typing import List, Optional

from celery import Celery

from core.image_analyzer import get_analyzer
from core.quote_calculator import QuoteCalculator
from core.conversation_manager import ConversationManager, ConversationStage, get_response
from core.security import is_allowed_twilio_media_url
from services.notification_service import NotificationService
from materials.order_manager import get_order_manager

logger = logging.getLogger("celery_tasks")

celery_app = Celery(
    "quoteflow",
    broker=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
    backend=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=300,
    beat_schedule={
        "daily-follow-ups": {
            "task": "workers.celery_tasks.daily_follow_ups",
            "schedule": 3600.0,
        },
    },
)

conversation_mgr = ConversationManager()

_COMPLEXITY_HOURS = {"simple": 3.0, "medium": 5.0, "complex": 8.0, "moderate": 5.0}
_DAMAGE_MULTIPLIER = {"minor": 1.0, "moderate": 1.25, "major": 1.5, "severe": 1.5, "none": 1.0}
_TRADE_SKILL_LEVEL = {
    "landscaping": ("General Labor", "junior"),
    "roofing": ("Tradesperson", "standard"),
    "plumbing": ("Tradesperson", "standard"),
    "electrical": ("Master Specialist", "master"),
    "autobody": ("Master Specialist", "master"),
}
_FALLBACK_RATE = {"junior": 25.0, "standard": 45.0, "master": 68.0}


async def _engine_quote_for_analysis(organization_id: Optional[str], trade: str, analysis) -> dict:
    """Build a PricingInput from the org's settings + skill rates and the
    image analysis, compute it with the authoritative engine, and persist
    the quote with its reproducibility snapshot."""
    from core.database import (
        AsyncSessionLocal, OrganizationModel, SkillRateModel, QuoteModel,
    )
    from core.pricing_engine import PricingInput, LaborLine, compute
    from sqlalchemy import select

    hours = _COMPLEXITY_HOURS.get((analysis.complexity or "medium").lower(), 5.0)
    hours *= _DAMAGE_MULTIPLIER.get((analysis.damage_level or "none").lower(), 1.0)
    if trade in ("landscaping", "roofing") and analysis.estimated_sqft:
        hours += min(float(analysis.estimated_sqft) / 1000.0, 8.0)

    skill_name, skill_level = _TRADE_SKILL_LEVEL.get(trade, ("Tradesperson", "standard"))
    settings = {}
    org = None

    async with AsyncSessionLocal() as session:
        rate = _FALLBACK_RATE[skill_level]
        if organization_id:
            org = await session.get(OrganizationModel, organization_id)
            if org:
                settings = org.settings or {}
                skill = (await session.execute(
                    select(SkillRateModel).where(
                        SkillRateModel.organization_id == organization_id,
                        SkillRateModel.level == skill_level,
                        SkillRateModel.is_active.is_(True),
                    ).order_by(SkillRateModel.hourly_rate.desc()).limit(1))).scalar()
                if skill:
                    rate = skill.hourly_rate
                    skill_name = skill.name

        pricing_input = PricingInput(
            labor_lines=[LaborLine(
                skill_name=skill_name, skill_level=skill_level,
                workers=1, hours=round(hours, 2), hourly_rate=rate,
            )],
            material_lines=[],
            overhead_pct=settings.get("overhead_pct", 0.15),
            profit_margin_pct=settings.get("profit_margin_pct", 0.20),
            tax_rate=settings.get("tax_rate", 0.08),
            title=f"{trade.title()} service",
        )
        result = compute(pricing_input)
        quote_id = f"Q-{trade[:3].upper()}-{secrets.token_hex(4).upper()}"
        valid_days = settings.get("valid_days", 14)
        quote = QuoteModel(
            quote_id=quote_id,
            organization_id=organization_id,
            trade=trade,
            customer_id="",
            title=pricing_input.title,
            line_items=[*result.labor_lines, *result.material_lines],
            subtotal=result.recommended_price,
            tax_rate=pricing_input.tax_rate,
            tax_amount=result.tax_amount,
            total=result.total_with_tax,
            breakdown=result.as_dict(),
            engine_input=pricing_input.snapshot(),
            engine_result=result.as_dict(),
            valid_days=valid_days,
            expires_at=datetime.utcnow() + timedelta(days=valid_days),
            status="sent",
            sent_at=datetime.utcnow(),
            notes=f"Auto-generated from photo analysis. Confidence: {analysis.confidence:.0%}. {analysis.notes}",
        )
        session.add(quote)

        if organization_id:
            from payments.billing import get_billing_service
            service = get_billing_service()
            await service.ensure_subscription(session, organization_id)
            await service.record_quote_usage(session, organization_id)
        await session.commit()

    return {"quote_id": quote_id, "total": result.total_with_tax,
            "result": result, "explanation": result.explanation_text()}


@celery_app.task(bind=True, max_retries=2)
def process_photos_and_quote(self, customer_id: str, photo_urls: List[str], trade: str,
                             organization_id: Optional[str] = None):
    """Async task: download photos, analyze, calculate quote, send to customer."""
    try:
        import asyncio
        import httpx
        import tempfile

        local_paths = []
        for url in photo_urls:
            try:
                if not is_allowed_twilio_media_url(url):
                    raise ValueError("Unsupported media URL host")
                auth = (os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
                with httpx.stream("GET", url, auth=auth, timeout=30, follow_redirects=False) as resp:
                    resp.raise_for_status()
                    content_length = int(resp.headers.get("content-length", "0"))
                    if content_length > 20 * 1024 * 1024:
                        raise ValueError("Image exceeds 20 MB limit")
                    total = 0
                    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
                        for chunk in resp.iter_bytes():
                            total += len(chunk)
                            if total > 20 * 1024 * 1024:
                                raise ValueError("Image exceeds 20 MB limit")
                            f.write(chunk)
                        local_paths.append(f.name)
            except Exception as e:
                logger.warning(f"Failed to download {url}: {e}")

        if not local_paths:
            raise self.retry(countdown=30)

        api_key = os.getenv("OPENAI_API_KEY")
        analyzer = get_analyzer(api_key, mock=(not api_key))
        analysis = asyncio.run(analyzer.analyze_batch(local_paths, trade))

        # Authoritative pricing engine path (org-scoped, reproducible).
        engine_out = asyncio.run(
            _engine_quote_for_analysis(organization_id, trade, analysis))
        quote_id = engine_out["quote_id"]
        quote_text = engine_out["explanation"]

        conv = asyncio.run(conversation_mgr.get(customer_id))
        if conv:
            asyncio.run(conversation_mgr.set_quote_id(customer_id, quote_id))
            asyncio.run(conversation_mgr.update_stage(customer_id, ConversationStage.QUOTE_SENT))

        notifier = NotificationService()
        asyncio.run(notifier.send_quote(
            phone=conv.customer_phone if conv else None,
            email=conv.customer_email if conv else None,
            quote_text=quote_text,
            quote_id=quote_id,
        ))

        for path in local_paths:
            try:
                os.unlink(path)
            except Exception:
                pass

        # Auto-create materials order only for legacy (non-org) flow.
        materials_order_id = None
        if not organization_id:
            calculator = QuoteCalculator()
            legacy_quote = calculator.calculate(trade, customer_id, analysis)
            order_manager = get_order_manager()
            materials_order = asyncio.run(order_manager.create_materials_order(legacy_quote, trade))
            materials_order_id = materials_order.order_id if materials_order else None

        return {
            "status": "success",
            "quote_id": quote_id,
            "total": engine_out["total"],
            "materials_order_id": materials_order_id,
            "customer_id": customer_id,
            "organization_id": organization_id,
        }

    except Exception as e:
        logger.error(f"Quote generation failed: {e}")
        if self.request.retries < 2:
            raise self.retry(countdown=60)
        return {"status": "failed", "error": str(e), "customer_id": customer_id}


@celery_app.task
def send_follow_up(customer_id: str, quote_id: str):
    """Send follow-up for pending quotes."""
    import asyncio
    conv = asyncio.run(conversation_mgr.get(customer_id))
    if not conv or conv.stage == ConversationStage.BOOKED:
        return {"status": "skipped", "reason": "already booked or no conversation"}

    message = get_response("follow_up", quote_id=quote_id)
    notifier = NotificationService()
    notifier.send_follow_up(
        phone=conv.customer_phone,
        email=conv.customer_email,
        quote_id=quote_id,
        message=message,
    )
    return {"status": "sent", "customer_id": customer_id, "quote_id": quote_id}


@celery_app.task
def daily_follow_ups():
    """Check for stale quotes and send follow-ups."""
    import asyncio
    stale = asyncio.run(conversation_mgr.get_stale_conversations(hours=48))
    for conv in stale:
        if conv.quote_id and conv.stage == ConversationStage.QUOTE_SENT:
            send_follow_up.delay(conv.customer_id, conv.quote_id)
    return {"follow_ups_sent": len(stale)}
