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
    materials_router, assistant_router, public_router,
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

# CORS (MUST-FIX #5): fail closed in production. A missing or wildcard
# CORS_ALLOW_ORIGINS is a dev convenience only — production must name its
# explicit frontend origins or refuse to start.
_app_env = os.getenv("APP_ENV", "development")
_raw_cors = os.getenv("CORS_ALLOW_ORIGINS", "*").strip()
_cors_origins = [o.strip() for o in _raw_cors.split(",") if o.strip()] or ["*"]
if _app_env == "production" and "*" in _cors_origins:
    raise RuntimeError(
        "CORS_ALLOW_ORIGINS must list explicit origins (e.g. https://app.e-zflow.com) "
        "in production — a wildcard is not allowed")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
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

async def resolve_sms_tenant(session, to_number: str):
    """Twilio authenticity ≠ tenant identity: map the inbound 'To' number to
    the owning organization BEFORE creating or loading any conversation."""
    from core.database import OrganizationChannelModel, OrganizationModel
    from sqlalchemy import select
    row = (await session.execute(
        select(OrganizationChannelModel).where(
            OrganizationChannelModel.channel_value == to_number,
            OrganizationChannelModel.provider.in_(["sms", "voice"]),
            OrganizationChannelModel.active.is_(True)))).scalar()
    if not row:
        return None, None
    org = await session.get(OrganizationModel, row.organization_id)
    if not org or not org.is_active:
        return None, None
    return org.id, row


@app.post("/webhook/sms")
async def webhook_sms(request: Request):
    """Handle incoming SMS with photos from customers.

    Tenant routing: the Twilio signature proves the sender; the 'To' number
    is resolved against organization_channels to prove the tenant. Unmapped
    numbers get a polite rejection and NO conversation is created.
    """
    from core.database import AsyncSessionLocal
    from core.observability import security_event

    form = await request.form()
    form_data = {key: str(value) for key, value in form.items()}
    await verify_twilio_request(request, form_data)
    data = app.state.sms.parse_inbound(form_data)

    to_number = form_data.get("To", "").strip()
    async with AsyncSessionLocal() as session:
        organization_id, channel = await resolve_sms_tenant(session, to_number)
    if not organization_id:
        security_event("inbound_sms_unmapped_number", to=to_number)
        # Tell the sender's phone this number isn't served; create nothing —
        # no conversation, no customer, no organization leak.
        return app.state.sms.create_response(
            "Sorry — this number is not accepting messages right now.")

    customer_id = f"sms:{organization_id}:{data['from']}"
    body = data["body"].lower().strip()
    media_urls = [url for url in data["media_urls"] if url]

    conv = await app.state.conversations.get_or_create(
        customer_id=customer_id,
        phone=data["from"],
        organization_id=organization_id,
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
        process_photos_and_quote.delay(customer_id, media_urls, conv.trade,
                                       organization_id=organization_id)

        response_text = get_response("analyzing")
        return app.state.sms.create_response(response_text)

    if body in ("accept", "yes", "book", "schedule") and conv.quote_id:
        # Customer approval via SMS reply — the real accept path (MUST-FIX #2).
        from core.database import QuoteModel, AsyncSessionLocal as _ASL
        from api.routes.quotes_routes import _accept_quote_for_customer
        from datetime import datetime as _dt
        async with _ASL() as session:
            q = await session.get(QuoteModel, conv.quote_id)
            accepted = None
            if (q is not None and q.organization_id == organization_id
                    and q.status == "sent"
                    and (q.expires_at is None or q.expires_at > _dt.utcnow())):
                accepted = await _accept_quote_for_customer(session, q, via="sms_reply")
        await app.state.conversations.update_stage(customer_id, ConversationStage.BOOKED)
        total = ""
        if accepted:
            from core.database import QuoteModel as _QM
            async with _ASL() as session:
                q2 = await session.get(_QM, conv.quote_id)
                total = f" Total: ${q2.total:,.2f}." if q2 else ""
        response_text = get_response("booked", quote_id=conv.quote_id, total=total)
        return app.state.sms.create_response(response_text)

    if conv.stage == ConversationStage.GREETING:
        response_text = get_response("greeting")
    else:
        response_text = "I'm not sure what you mean. Reply with your trade type or send photos of the work area."

    return app.state.sms.create_response(response_text)


@app.post("/webhook/email")
async def webhook_email(request: Request):
    """Handle inbound emails with photo attachments.

    Tenant routing: the recipient address is resolved against
    organization_channels (provider=email) before any conversation exists.
    """
    from core.database import AsyncSessionLocal, OrganizationChannelModel, OrganizationModel
    from core.observability import security_event
    from sqlalchemy import select

    await verify_email_webhook(request)
    payload = await request.json()
    email = app.state.email.parse_inbound(payload)

    # Recipient = an organization's inbound address (To / Cc / recipient)
    candidates = [str(c).strip().lower() for c in (
        getattr(email, "to_email", None),
        getattr(email, "recipient", None),
        payload.get("To"), payload.get("to"), payload.get("envelope_to"),
    ) if c]
    organization_id = None
    async with AsyncSessionLocal() as session:
        for candidate in candidates:
            row = (await session.execute(
                select(OrganizationChannelModel).where(
                    OrganizationChannelModel.channel_value == candidate,
                    OrganizationChannelModel.provider == "email",
                    OrganizationChannelModel.active.is_(True)))).scalar()
            if row:
                org = await session.get(OrganizationModel, row.organization_id)
                if org and org.is_active:
                    organization_id = org.id
                    break
    if not organization_id:
        security_event("inbound_email_unmapped_recipient",
                       recipients=",".join(candidates)[:120])
        return {"status": "ignored", "reason": "unmapped_recipient"}

    customer_id = f"email:{organization_id}:{email.from_email}"
    conv = await app.state.conversations.get_or_create(
        customer_id=customer_id,
        email=email.from_email,
        organization_id=organization_id,
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
        process_photos_and_quote.delay(customer_id, photo_urls, conv.trade,
                                       organization_id=organization_id)

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
app.include_router(public_router)  # customer token approval — no auth by design

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
