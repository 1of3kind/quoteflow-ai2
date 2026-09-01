"""Main FastAPI application for QuoteFlow AI."""

import os
import logging
from typing import Optional, List
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, Depends, status, Form, UploadFile, File, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from core.conversation_manager import ConversationManager, ConversationStage, get_response
from core.database import init_db
from core.security import api_key_is_valid, required_secret, verify_email_webhook, verify_twilio_request
from core.image_analyzer import get_analyzer
from core.quote_calculator import QuoteCalculator
from services.sms_handler import SMSHandler
from services.email_processor import EmailProcessor
from services.voice_handler import VoiceHandler
from services.notification_service import NotificationService
from materials.store_inventory import StoreFinder, StoreItem
from payments.stripe_client import get_stripe_client, PaymentIntent
from dashboard.api import router as dashboard_router
from payments.billing import get_billing_manager, PLANS

from materials.order_manager import OrderManager, AppointmentWithMaterials, get_order_manager

from workers.celery_tasks import process_photos_and_quote, daily_follow_ups

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("api")

security = HTTPBearer()


def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if not api_key_is_valid(credentials.credentials):
        raise HTTPException(status_code=401, detail="Invalid API key")
    return credentials.credentials


class TradeSelect(BaseModel):
    customer_id: str
    trade: str
    phone: Optional[str] = None
    email: Optional[str] = None


class QuoteAccept(BaseModel):
    customer_id: str
    quote_id: str

class MaterialsOrderRequest(BaseModel):
    quote_id: str
    trade: str
    zip_code: str = ""


class ScheduleRequest(BaseModel):
    customer_id: str
    customer_name: str
    customer_phone: str
    trade: str
    quote_id: str
    preferred_date: str  # ISO format
    duration_hours: float = 4.0


class PickupConfirm(BaseModel):
    appointment_id: str

class PaymentRequest(BaseModel):
    quote_id: str
    amount: float
    customer_email: str
    payment_type: str = "deposit"  # deposit, full
    deposit_percent: float = 0.5


class CheckoutRequest(BaseModel):
    quote_id: str
    amount: float
    customer_email: str
    success_url: str
    cancel_url: str


class ContractorSignup(BaseModel):
    contractor_id: str
    email: str
    business_name: str
    phone: str
    plan: str = "starter"


class PlanUpgrade(BaseModel):
    contractor_id: str
    plan: str
    billing_cycle: str = "monthly"  # monthly, annual




@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("QuoteFlow AI starting...")
    # Fail closed at startup rather than exposing a predictable fallback key.
    required_secret("FEEDBACK_API_KEY")
    required_secret("OPENAI_API_KEY")
    await init_db()
    app.state.conversations = ConversationManager()
    app.state.sms = SMSHandler()
    app.state.email = EmailProcessor()
    app.state.voice = VoiceHandler()
    app.state.notifier = NotificationService()
    app.state.calculator = QuoteCalculator()
    yield
    logger.info("QuoteFlow AI shutting down...")


app = FastAPI(
    title="QuoteFlow AI",
    description="AI-powered quoting for small businesses. Customers send photos, AI analyzes and generates instant quotes.",
    version="3.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in os.getenv("CORS_ALLOW_ORIGINS", "").split(",") if origin.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    return {
        "service": "QuoteFlow AI",
        "version": "3.0.0",
        "description": "Send photos → AI analyzes → Instant quote via SMS/Email",
        "trades": ["landscaping", "roofing", "plumbing", "autobody", "electrical"],
        "endpoints": [
            "/health",
            "/webhook/sms",
            "/webhook/email",
            "/webhook/stripe",
            "/voice/welcome",
            "/voice/trade-select",
            "/quote/start",
            "/quote/accept",
            "/materials/order",
            "/appointments/schedule",
            "/appointments/daily",
            "/appointments/confirm-pickup",
            "/materials/pending-pickups",
            "/stores/search",
            "/payments/create",
            "/payments/checkout",
            "/payments/refund",
            "/plans",
            "/contractors/signup",
            "/contractors/upgrade",
            "/contractors/{id}/status",
            "/admin/conversations",
            "/admin/analytics",
        ],
    }


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "active_conversations": await app.state.conversations.get_active_count(),
    }


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
async def voice_trade_select(request: Request, Digits: str = Form(...)):
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
    trade = trades.get(Digits, "unknown")
    return app.state.voice.create_photo_instructions(trade)


@app.post("/quote/start", dependencies=[Depends(verify_token)])
async def start_quote(data: TradeSelect):
    """Manually start a quote (for web dashboard)."""
    conv = await app.state.conversations.get_or_create(
        customer_id=data.customer_id,
        phone=data.phone,
        email=data.email,
    )
    await app.state.conversations.set_trade(data.customer_id, data.trade)
    await app.state.conversations.update_stage(data.customer_id, ConversationStage.TRADE_SELECT)

    from config.pricing_configs import get_trade_config
    config = get_trade_config(data.trade)

    return {
        "customer_id": data.customer_id,
        "trade": data.trade,
        "required_photos": config.required_photos,
        "message": f"Please send photos of: {', '.join(config.required_photos)}",
    }


@app.post("/quote/accept", dependencies=[Depends(verify_token)])
async def accept_quote(data: QuoteAccept):
    """Customer accepts a quote."""
    conv = await app.state.conversations.get(data.customer_id)
    if not conv or conv.quote_id != data.quote_id:
        raise HTTPException(status_code=404, detail="Quote not found")

    await app.state.conversations.update_stage(data.customer_id, ConversationStage.BOOKED)
    return {
        "status": "booked",
        "quote_id": data.quote_id,
        "customer_id": data.customer_id,
        "next_steps": "An agent will contact you within 24 hours to schedule.",
    }


@app.get("/admin/conversations", dependencies=[Depends(verify_token)])
async def list_conversations(stage: Optional[str] = None):
    """List all conversations with optional filter."""
    convs = await app.state.conversations.list_all()
    if stage:
        convs = [c for c in convs if c.stage.value == stage]
    return {
        "count": len(convs),
        "conversations": [c.to_dict() for c in convs],
    }


@app.get("/admin/analytics", dependencies=[Depends(verify_token)])
async def analytics():
    """Get business analytics."""
    convs = await app.state.conversations.list_all()
    return {
        "total_conversations": len(convs),
        "active": await app.state.conversations.get_active_count(),
        "conversion_rate": round(await app.state.conversations.get_conversion_rate(), 2),
        "by_stage": {stage.value: sum(c.stage == stage for c in convs) for stage in ConversationStage},
    }


@app.post("/admin/trigger-followups", dependencies=[Depends(verify_token)])
async def trigger_followups():
    """Manually trigger follow-up messages."""
    result = daily_follow_ups.delay()
    return {"task_id": result.id, "status": "queued"}




# ─── MATERIALS & APPOINTMENTS ──────────────────────────────────

@app.post("/materials/order", dependencies=[Depends(verify_token)])
async def create_materials_order(data: MaterialsOrderRequest):
    """Create a materials order for a quote."""
    manager = get_order_manager()

    # Get the quote (in production, fetch from DB)
    # For now, we need to reconstruct from the quote_id
    # This would normally fetch the saved quote

    # Mock quote reconstruction for demo
    from core.quote_calculator import QuoteCalculator
    from core.image_analyzer import MockAnalyzer

    analyzer = MockAnalyzer()
    calc = QuoteCalculator()

    import asyncio
    analysis = asyncio.run(analyzer.analyze_image("fake.jpg", data.trade))
    quote = calc.calculate(data.trade, "cust_001", analysis)
    quote.quote_id = data.quote_id

    order = await manager.create_materials_order(quote, data.trade, data.zip_code)

    if not order:
        return {"status": "no_materials_needed", "quote_id": data.quote_id}

    return {
        "status": "order_created",
        "order_id": order.order_id,
        "quote_id": order.quote_id,
        "store": order.store_name,
        "store_address": order.store_address,
        "pickup_time": order.pickup_time,
        "total": order.total,
        "items": [
            {
                "name": item.name,
                "brand": item.brand,
                "price": item.price,
                "aisle": item.aisle_location,
            }
            for item in order.items
        ],
    }


@app.post("/appointments/schedule", dependencies=[Depends(verify_token)])
async def schedule_appointment(data: ScheduleRequest):
    """Schedule an appointment with materials pickup."""
    from datetime import datetime

    manager = get_order_manager()
    date = datetime.fromisoformat(data.preferred_date)

    appointment = manager.schedule_appointment_with_pickup(
        customer_id=data.customer_id,
        customer_name=data.customer_name,
        customer_phone=data.customer_phone,
        trade=data.trade,
        quote_id=data.quote_id,
        preferred_date=date,
        duration_hours=data.duration_hours,
    )

    return {
        "appointment_id": appointment.appointment_id,
        "scheduled_date": appointment.scheduled_date.isoformat(),
        "materials_order_id": appointment.materials_order.order_id if appointment.materials_order else None,
        "pickup_reminder": manager.get_pickup_reminder(appointment) if appointment.materials_order else None,
    }


@app.get("/appointments/daily", dependencies=[Depends(verify_token)])
async def get_daily_schedule(date: str):
    """Get daily schedule with materials pickup info."""
    from datetime import datetime

    manager = get_order_manager()
    query_date = datetime.fromisoformat(date)
    appointments = manager.get_daily_schedule(query_date)

    return {
        "date": date,
        "appointment_count": len(appointments),
        "appointments": [
            {
                "id": apt.appointment_id,
                "customer": apt.customer_name,
                "phone": apt.customer_phone,
                "trade": apt.trade,
                "time": apt.scheduled_date.strftime("%I:%M %p"),
                "duration": apt.estimated_duration_hours,
                "materials_picked_up": apt.materials_confirmed,
                "store": apt.materials_order.store_name if apt.materials_order else None,
                "materials_total": apt.materials_order.total if apt.materials_order else 0,
            }
            for apt in appointments
        ],
    }


@app.post("/appointments/confirm-pickup", dependencies=[Depends(verify_token)])
async def confirm_pickup(data: PickupConfirm):
    """Confirm materials have been picked up."""
    manager = get_order_manager()
    success = manager.confirm_pickup(data.appointment_id)

    return {
        "status": "confirmed" if success else "not_found",
        "appointment_id": data.appointment_id,
    }


@app.get("/materials/pending-pickups", dependencies=[Depends(verify_token)])
async def pending_pickups():
    """Get all appointments with pending material pickups."""
    manager = get_order_manager()
    pending = manager.get_pending_pickups()

    return {
        "count": len(pending),
        "pickups": [
            {
                "appointment_id": apt.appointment_id,
                "customer": apt.customer_name,
                "trade": apt.trade,
                "date": apt.scheduled_date.isoformat(),
                "store": apt.materials_order.store_name if apt.materials_order else None,
                "store_address": apt.materials_order.store_address if apt.materials_order else None,
                "order_id": apt.materials_order.order_id if apt.materials_order else None,
            }
            for apt in pending
        ],
    }


@app.get("/stores/search", dependencies=[Depends(verify_token)])
async def search_stores(query: str, zip_code: str, trade: str = ""):
    """Search local stores for materials."""
    finder = StoreFinder(zip_code=zip_code)
    results = await finder.find_materials(trade, [query])

    items = results.get(query, [])
    return {
        "query": query,
        "zip_code": zip_code,
        "results_count": len(items),
        "results": [
            {
                "sku": item.sku,
                "name": item.name,
                "brand": item.brand,
                "price": item.price,
                "in_stock": item.in_stock,
                "store": item.store_name,
                "address": item.store_address,
                "distance_miles": item.distance_miles,
                "aisle": item.aisle_location,
            }
            for item in items[:10]
        ],
    }




# ─── PAYMENTS ──────────────────────────────────────────────────

@app.post("/payments/create", dependencies=[Depends(verify_token)])
async def create_payment(data: PaymentRequest):
    """Create a payment intent for quote acceptance."""
    if os.getenv("PAYMENTS_ENABLED", "false").lower() != "true":
        raise HTTPException(status_code=503, detail="Payments are disabled until server-side quote pricing is enabled")
    stripe = get_stripe_client()

    if data.payment_type == "deposit":
        intent = await stripe.create_deposit_intent(
            quote_id=data.quote_id,
            total_amount=data.amount,
            deposit_percent=data.deposit_percent,
            customer_email=data.customer_email,
            description=f"Deposit for Quote #{data.quote_id}",
        )
    else:
        intent = await stripe.create_quote_payment(
            quote_id=data.quote_id,
            amount=data.amount,
            customer_email=data.customer_email,
            description=f"Payment for Quote #{data.quote_id}",
        )

    return {
        "client_secret": intent.client_secret,
        "payment_intent_id": intent.id,
        "amount": intent.amount,
        "status": intent.status,
    }


@app.post("/payments/checkout", dependencies=[Depends(verify_token)])
async def create_checkout(data: CheckoutRequest):
    """Create a Stripe Checkout session for customer payment."""
    if os.getenv("PAYMENTS_ENABLED", "false").lower() != "true":
        raise HTTPException(status_code=503, detail="Payments are disabled until server-side quote pricing is enabled")
    stripe = get_stripe_client()

    session = await stripe.create_checkout_session(
        quote_id=data.quote_id,
        amount=data.amount,
        customer_email=data.customer_email,
        success_url=data.success_url,
        cancel_url=data.cancel_url,
    )

    return {
        "checkout_url": session["url"],
        "session_id": session["session_id"],
    }


@app.post("/payments/refund", dependencies=[Depends(verify_token)])
async def refund_payment(payment_intent_id: str, amount: Optional[float] = None):
    """Refund a payment (partial or full)."""
    if os.getenv("PAYMENTS_ENABLED", "false").lower() != "true":
        raise HTTPException(status_code=503, detail="Payments are disabled until server-side quote pricing is enabled")
    stripe = get_stripe_client()
    result = await stripe.refund_payment(payment_intent_id, amount)
    return result


@app.post("/webhook/stripe")
async def stripe_webhook(request: Request):
    """Handle Stripe webhooks for payment events."""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    stripe = get_stripe_client()

    try:
        event = stripe.verify_webhook(payload, sig_header)
        result = stripe.handle_webhook_event(event)
        return {"status": "processed", "result": result}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ─── SAAS BILLING ──────────────────────────────────────────────

@app.get("/plans")
async def list_plans():
    """List available subscription plans."""
    return {
        "plans": [
            {
                "id": plan.id,
                "name": plan.name,
                "price_monthly": plan.price_monthly,
                "price_annual": plan.price_annual,
                "features": plan.features,
                "limits": plan.limits,
            }
            for plan in PLANS.values()
        ]
    }


@app.post("/contractors/signup", dependencies=[Depends(verify_token)])
async def signup_contractor(data: ContractorSignup):
    """Create a new contractor account."""
    billing = get_billing_manager()
    account = billing.create_account(
        contractor_id=data.contractor_id,
        email=data.email,
        business_name=data.business_name,
        phone=data.phone,
        plan=data.plan,
    )
    return {
        "status": "created",
        "contractor_id": account.contractor_id,
        "plan": account.plan,
        "trial_ends_at": account.trial_ends_at,
    }


@app.post("/contractors/upgrade", dependencies=[Depends(verify_token)])
async def upgrade_contractor(data: PlanUpgrade):
    """Upgrade contractor subscription."""
    billing = get_billing_manager()
    result = await billing.upgrade_plan(
        contractor_id=data.contractor_id,
        new_plan=data.plan,
        billing_cycle=data.billing_cycle,
    )
    return result


@app.get("/contractors/{contractor_id}/status", dependencies=[Depends(verify_token)])
async def contractor_status(contractor_id: str):
    """Get contractor account status and usage."""
    billing = get_billing_manager()
    return billing.get_account_status(contractor_id)


# Include dashboard router
app.include_router(dashboard_router, dependencies=[Depends(verify_token)])


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
