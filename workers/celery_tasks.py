"""Background tasks for async image processing and quote generation."""

import os
import logging
from typing import List
from datetime import datetime

from celery import Celery

from core.image_analyzer import get_analyzer
from core.quote_calculator import QuoteCalculator
from core.conversation_manager import ConversationManager, ConversationStage, get_response
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
)

conversation_mgr = ConversationManager()


@celery_app.task(bind=True, max_retries=2)
def process_photos_and_quote(self, customer_id: str, photo_urls: List[str], trade: str):
    """Async task: download photos, analyze, calculate quote, send to customer."""
    try:
        import asyncio
        import httpx
        import tempfile

        local_paths = []
        for url in photo_urls:
            try:
                resp = httpx.get(url, timeout=30)
                resp.raise_for_status()
                with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
                    f.write(resp.content)
                    local_paths.append(f.name)
            except Exception as e:
                logger.warning(f"Failed to download {url}: {e}")

        if not local_paths:
            raise self.retry(countdown=30)

        api_key = os.getenv("OPENAI_API_KEY")
        analyzer = get_analyzer(api_key, mock=(not api_key))
        analysis = asyncio.run(analyzer.analyze_batch(local_paths, trade))

        calculator = QuoteCalculator()
        quote = calculator.calculate(trade, customer_id, analysis)

        conv = conversation_mgr.get(customer_id)
        if conv:
            conv.quote_id = quote.quote_id
            conversation_mgr.update_stage(customer_id, ConversationStage.QUOTE_READY)

        notifier = NotificationService()
        quote_text = calculator.format_quote_text(quote)

        asyncio.run(notifier.send_quote(
            phone=conv.customer_phone if conv else None,
            email=conv.customer_email if conv else None,
            quote_text=quote_text,
            quote_id=quote.quote_id,
        ))

        for path in local_paths:
            try:
                os.unlink(path)
            except Exception:
                pass

        # Auto-create materials order
        order_manager = get_order_manager()
        materials_order = asyncio.run(order_manager.create_materials_order(quote, trade))

        # Send materials info if order created
        if materials_order and conv:
            materials_text = f"\n\n📦 MATERIALS ORDER\nStore: {materials_order.store_name}\n"
            materials_text += f"Pickup: {materials_order.pickup_time}\n"
            materials_text += f"Items: {len(materials_order.items)}\n"
            materials_text += f"Materials Total: ${materials_order.total:.2f}"

            # Append to quote notification
            asyncio.run(notifier.send_quote(
                phone=conv.customer_phone,
                email=conv.customer_email,
                quote_text=quote_text + materials_text,
                quote_id=quote.quote_id,
            ))

        return {
            "status": "success",
            "quote_id": quote.quote_id,
            "total": quote.total,
            "materials_order_id": materials_order.order_id if materials_order else None,
            "customer_id": customer_id,
        }

    except Exception as e:
        logger.error(f"Quote generation failed: {e}")
        if self.request.retries < 2:
            raise self.retry(countdown=60)
        return {"status": "failed", "error": str(e), "customer_id": customer_id}


@celery_app.task
def send_follow_up(customer_id: str, quote_id: str):
    """Send follow-up for pending quotes."""
    conv = conversation_mgr.get(customer_id)
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
    stale = conversation_mgr.get_stale_conversations(hours=48)
    for conv in stale:
        if conv.quote_id and conv.stage == ConversationStage.QUOTE_SENT:
            send_follow_up.delay(conv.customer_id, conv.quote_id)
    return {"follow_ups_sent": len(stale)}
