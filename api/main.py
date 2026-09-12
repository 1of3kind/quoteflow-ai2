"""Main FastAPI application for E-ZFlow.

Architecture:
  - Public webhooks (Twilio SMS/voice, email, Stripe) — signature-verified.
  - Authenticated multi-tenant API (JWT + RBAC + org scoping) under
    /auth, /org, /quotes, /jobs, /billing, /dashboard.
  - Legacy single-key admin endpoints have been removed; every
    customer-owned read is now scoped to the caller's organization.
"""

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from core.conversation_manager import ConversationManager, ConversationStage, get_response
from core.database import init_db
from core.security import verify_email_webhook, verify_twilio_request
from core.observability import configure_logging, observability_middleware, install_error_handlers
from services.sms_handler import SMSHandler
from services.email_processor import EmailProcessor
from services.voice_handler import VoiceHandler
from workers.celery_tasks import process_photos_and_quote

from api.routes import (
    auth_router, org_router, quotes_router,
    jobs_router, billing_router, dashboard_router,
    materials_router, assistant_router,
)

configure_logging()
logger = logging.getLogger("api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("E-ZFlow starting env=%s", os.getenv("APP_ENV", "development"))
    if not os.getenv("JWT_SECRET", "").strip():
        if os.getenv("APP_ENV") == "production":
            raise RuntimeError("JWT_SECRET is required in production")
        logger.warning("JWT_SECRET not set — using insecure dev secret (never in production)")
    await init_db()
    app.state.conversations = ConversationManager()
    app.state.sms = SMSHandler()
    app.state.email = EmailProcessor()
    app.state.voice = VoiceHandler()
    yield
    logger.info("E-ZFlow shutting down")


app = FastAPI(
    title="E-ZFlow",
    description="AI-powered quoting for small businesses. Customers send photos, AI analyzes and generates instant quotes.",
    version="4.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in os.getenv("CORS_ALLOW_ORIGINS", "*").split(",") if origin.strip()] or ["*"],
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

app.middleware("http")(observability_middleware)
install_error_handlers(app)


@app.get("/api")
async def api_info():
    """API discovery endpoint (the web landing page is served at '/')."""
    return {
        "service": "E-ZFlow",
        "version": "4.1.0",
        "docs": "/docs",
        "app": "/app.html",
        "public": ["/health", "/webhook/sms", "/webhook/email", "/webhook/stripe",
                   "/voice/welcome", "/voice/trade-select", "/billing/plans"],
        "authenticated": ["/auth", "/org", "/quotes", "/jobs", "/billing",
                          "/dashboard", "/materials", "/assistant"],
    }


@app.get("/health")
async def health():
    """Liveness + database readiness probe."""
    from core.database import AsyncSessionLocal
    from sqlalchemy import text
    db_ok = True
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        db_ok = False
    return {
        "status": "healthy" if db_ok else "degraded",
        "database": "ok" if db_ok else "unreachable",
        "timestamp": datetime.utcnow().isoformat(),
    }


# ─── Inbound webhooks (signature-verified, public) ──────────────────────────

@app.post("/webhook/sms")
async def webhook_sms(request: Request):
    """Handle incoming SMS with photos from customers."""
    form = await request.form()
    form_data = {key: str(value) for key, value in form.items()}
    await verify_twilio_request(request, form_data)
    data = app.state.sms.parse_inbound(form_data)

    customer_id = f"sms_{data['from']}"
    body = data["body"].lower().strip()
    media_urls = [url for url in data["media_urls"] if url]

    conv = await app.state.conversations.get_or_create(
        customer_id=customer_id,
        phone=data["from"],
    )

    trades = ["landscaping", "roofing", "plumbing", "autobody", "electrical"]
    if body in trades and conv.stage == ConversationStage.GREETING:
        await app.state.conversations.set_trade(customer_id, body)
        await app.state.conversations.update_stage(customer_id, ConversationStage.TRADE_SELECT)

        from config.pricing_configs import get_trade_config
        config = get_trade_config(body)
        required = ", ".join(config.required_photos)

        response_text = get_response("trade_select", required_photos=required)
        return app.state.sms.create_response(response_text)

    if media_urls and conv.trade:
        await app.state.conversations.add_photos(customer_id, media_urls)
        await app.state.conversations.update_stage(customer_id, ConversationStage.PHOTO_RECEIVED)
        process_photos_and_quote.delay(customer_id, media_urls, conv.trade)

        response_text = get_response("analyzing")
        return app.state.sms.create_response(response_text)

    if body in ("accept", "yes", "book", "schedule") and conv.quote_id:
        await app.state.conversations.update_stage(customer_id, ConversationStage.BOOKED)
        response_text = get_response("booked", quote_id=conv.quote_id, total="TBD")
        return app.state.sms.create_response(response_text)

    if conv.stage == ConversationStage.GREETING:
        response_text = get_response("greeting")
    else:
        response_text = "I'm not sure what you mean. Reply with your trade type or send photos of the work area."

    return app.state.sms.create_response(response_text)


@app.post("/webhook/email")
async def webhook_email(request: Request):
    """Handle incoming emails with photo attachments."""
    await verify_email_webhook(request)
    payload = await request.json()
    email = app.state.email.parse_inbound(payload)

    customer_id = f"email_{email.from_email}"
    conv = await app.state.conversations.get_or_create(
        customer_id=customer_id,
        email=email.from_email,
    )

    trades = ["landscaping", "roofing", "plumbing", "autobody", "electrical"]
    body_lower = (email.subject + " " + email.body_text).lower()
    detected_trade = next((t for t in trades if t in body_lower), None)

    if detected_trade and not conv.trade:
        await app.state.conversations.set_trade(customer_id, detected_trade)
        await app.state.conversations.update_stage(customer_id, ConversationStage.TRADE_SELECT)

    photo_urls = [att["url"] for att in email.attachments if "image" in att.get("content_type", "")]
    if photo_urls and conv.trade:
        await app.state.conversations.add_photos(customer_id, photo_urls)
        process_photos_and_quote.delay(customer_id, photo_urls, conv.trade)

        await app.state.email.send_quote_email(
            email.from_email,
            "Photo received - analyzing now",
            "Thanks for the photos! Our AI is analyzing them now. You'll receive your quote within 2 minutes.",
            "ack",
        )

    return {"status": "processed"}


@app.post("/voice/welcome")
async def voice_welcome(request: Request):
    """Twilio voice webhook: initial greeting."""
    form = await request.form()
    await verify_twilio_request(request, {key: str(value) for key, value in form.items()})
    return app.state.voice.create_welcome_response()


@app.post("/voice/trade-select")
async def voice_trade_select(request: Request, Digits: str = None):
    """Handle trade selection from phone keypad."""
    form = await request.form()
    await verify_twilio_request(request, {key: str(value) for key, value in form.items()})
    trades = {
        "1": "landscaping",
        "2": "roofing",
        "3": "plumbing",
        "4": "autobody",
        "5": "electrical",
    }
    digits = Digits or form.get("Digits", "")
    trade = trades.get(str(digits), "unknown")
    return app.state.voice.create_photo_instructions(trade)


@app.post("/webhook/stripe")
async def stripe_webhook(request: Request):
    """Stripe webhooks: signature-verified, idempotent, authoritative (GATE 5)."""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    from payments.stripe_gateway import get_gateway, GatewayError
    from payments.billing import get_billing_service
    from core.database import AsyncSessionLocal

    try:
        gateway = get_gateway()
    except GatewayError as e:
        raise HTTPException(status_code=503, detail=str(e))

    try:
        event = gateway.construct_event(payload, sig_header)
    except Exception:
        logger.warning("stripe webhook rejected: invalid signature")
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    async with AsyncSessionLocal() as session:
        result = await get_billing_service().handle_webhook(session, event)
        await session.commit()
    return {"status": "processed", "result": result}


# ─── Authenticated multi-tenant API ─────────────────────────────────────────

app.include_router(auth_router)
app.include_router(org_router)
app.include_router(quotes_router)
app.include_router(jobs_router)
app.include_router(billing_router)
app.include_router(dashboard_router)
app.include_router(materials_router)
app.include_router(assistant_router)

# ─── Static frontend (landing page + app UI) ────────────────────────────────
# Mounted LAST so all API routes above take precedence; serves index.html
# at '/' and /app.html for the web app.
from pathlib import Path
from fastapi.staticfiles import StaticFiles

_static_dir = Path(__file__).resolve().parent.parent / "frontend" / "static"
if _static_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="frontend")
else:  # pragma: no cover
    logger.warning("frontend/static not found — web UI not served")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
