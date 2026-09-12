"""PostgreSQL database layer — async SQLAlchemy models and session management.

Replaces the three in-memory singletons (ConversationManager, OrderManager,
BillingManager) with durable Postgres storage so conversations, quotes,
appointments, and contractor accounts survive restarts and scale across
multiple web/worker instances.
"""

import os
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict, AsyncGenerator
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from sqlalchemy import (
    Column, String, Integer, Float, Boolean, DateTime, Text,
    ForeignKey, JSON, Enum as SAEnum, select, update, delete, and_, or_,
)
from sqlalchemy.ext.asyncio import (
    create_async_engine, AsyncSession, async_sessionmaker, AsyncEngine,
)
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.dialects.postgresql import UUID
import uuid

logger = logging.getLogger("database")

# ---------------------------------------------------------------------------
# Engine & session helpers
# ---------------------------------------------------------------------------

def normalize_database_url(raw_url: Optional[str] = None) -> str:
    """Normalize database connection URL for async SQLAlchemy.
    
    Translates Render's PostgreSQL connection strings (postgres://, postgresql://)
    to postgresql+asyncpg:// and maps unsupported driver query parameters like
    sslmode to ssl.
    """
    # Dev fallback only — no credentials are ever hardcoded here. Real
    # connection strings (with passwords) come from DATABASE_URL at runtime.
    url = (raw_url or os.getenv("DATABASE_URL", "postgresql+asyncpg://localhost:5432/ezflow")).strip()
    
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgresql://") and not url.startswith("postgresql+"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)

    parsed = urlparse(url)
    if parsed.query:
        query_params = parse_qs(parsed.query)
        if "sslmode" in query_params:
            ssl_val = query_params.pop("sslmode")[0]
            query_params["ssl"] = [ssl_val]
        new_query = urlencode(query_params, doseq=True)
        url = urlunparse(parsed._replace(query=new_query))

    return url


DATABASE_URL = normalize_database_url()

def get_engine_args(url: str) -> dict:
    """Return dialect-appropriate engine arguments."""
    if "sqlite" in url:
        return {"echo": False}
    return {"echo": False, "pool_size": 10, "max_overflow": 20}

engine: AsyncEngine = create_async_engine(DATABASE_URL, **get_engine_args(DATABASE_URL))
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

Base = declarative_base()


def set_database_url(new_url: str):
    """Dynamically update the active engine and sessionmaker (used for testing/in-memory SQLite)."""
    global DATABASE_URL, engine, AsyncSessionLocal
    DATABASE_URL = normalize_database_url(new_url)
    engine = create_async_engine(DATABASE_URL, **get_engine_args(DATABASE_URL))
    AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yields an async DB session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db():
    """Create all tables (idempotent). Call once at startup."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables created / verified")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class ConversationModel(Base):
    __tablename__ = "conversations"

    id = Column(String, primary_key=True)
    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=True, index=True)
    customer_phone = Column(String, nullable=True)
    customer_email = Column(String, nullable=True)
    trade = Column(String, nullable=True)
    stage = Column(String, nullable=False, default="greeting")
    quote_id = Column(String, nullable=True)
    appointment_id = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_activity = Column(DateTime, nullable=False, default=datetime.utcnow)

    messages = relationship("MessageModel", back_populates="conversation",
                            cascade="all, delete-orphan", lazy="selectin")
    photos = relationship("PhotoModel", back_populates="conversation",
                          cascade="all, delete-orphan", lazy="selectin")


class MessageModel(Base):
    __tablename__ = "messages"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    conversation_id = Column(String, ForeignKey("conversations.id"), nullable=False, index=True)
    channel = Column(String, nullable=False)
    direction = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    timestamp = Column(DateTime, nullable=False, default=datetime.utcnow)

    conversation = relationship("ConversationModel", back_populates="messages")


class PhotoModel(Base):
    __tablename__ = "photos"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    conversation_id = Column(String, ForeignKey("conversations.id"), nullable=False, index=True)
    url = Column(String, nullable=False)
    uploaded_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    conversation = relationship("ConversationModel", back_populates="photos")


class QuoteModel(Base):
    __tablename__ = "quotes"

    quote_id = Column(String, primary_key=True)
    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=True, index=True)
    org_customer_id = Column(String(36), ForeignKey("org_customers.id"), nullable=True, index=True)
    job_id = Column(String(36), nullable=True, index=True)
    trade = Column(String, nullable=False)
    customer_id = Column(String, nullable=False, index=True)
    title = Column(String(255), nullable=True)
    line_items = Column(JSON, nullable=False, default=list)
    subtotal = Column(Float, nullable=False)
    tax_rate = Column(Float, nullable=False)
    tax_amount = Column(Float, nullable=False)
    total = Column(Float, nullable=False)
    breakdown = Column(JSON, nullable=False, default=dict)
    # Authoritative pricing-engine snapshot: exact inputs that produced this
    # quote, so every number is reproducible and explainable after the fact.
    engine_input = Column(JSON, nullable=True)
    engine_result = Column(JSON, nullable=True)
    valid_days = Column(Integer, nullable=False, default=14)
    expires_at = Column(DateTime, nullable=True)
    status = Column(String(32), nullable=False, default="draft", index=True)
    sent_at = Column(DateTime, nullable=True)
    accepted_at = Column(DateTime, nullable=True)
    notes = Column(Text, nullable=True)
    tier_applied = Column(String, nullable=True)
    created_by = Column(String(36), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class AppointmentModel(Base):
    __tablename__ = "appointments"

    appointment_id = Column(String, primary_key=True)
    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=True, index=True)
    customer_id = Column(String, nullable=False, index=True)
    customer_name = Column(String, nullable=False)
    customer_phone = Column(String, nullable=False)
    trade = Column(String, nullable=False)
    quote_id = Column(String, nullable=False)
    scheduled_date = Column(DateTime, nullable=False)
    estimated_duration_hours = Column(Float, nullable=False, default=4.0)
    materials_order_id = Column(String, nullable=True)
    pickup_reminder_sent = Column(Boolean, nullable=False, default=False)
    materials_confirmed = Column(Boolean, nullable=False, default=False)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class SupplierModel(Base):
    """A materials supplier for one organization (GATE: material ordering)."""

    __tablename__ = "suppliers"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    contact_phone = Column(String(32), nullable=True)
    contact_email = Column(String(320), nullable=True)
    address = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class CatalogItemModel(Base):
    """A stocked SKU: supplier, unit price, and live availability."""

    __tablename__ = "catalog_items"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)
    supplier_id = Column(String(36), ForeignKey("suppliers.id"), nullable=False, index=True)
    sku = Column(String(64), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    unit = Column(String(32), nullable=False, default="each")
    unit_price = Column(Float, nullable=False)
    quantity_available = Column(Float, nullable=False, default=0.0)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        # A SKU identifies one stocked item per supplier per organization.
        {"sqlite_autoincrement": True},
    )


# Material order lifecycle: draft → placed → confirmed → received (or canceled)
MATERIAL_ORDER_STATUSES = ["draft", "placed", "confirmed", "received", "canceled"]

MATERIAL_ORDER_TRANSITIONS = {
    "draft": {"placed", "canceled"},
    "placed": {"confirmed", "canceled"},
    "confirmed": {"received", "canceled"},
    "received": set(),
    "canceled": set(),
}


class MaterialOrderModel(Base):
    __tablename__ = "material_orders"

    order_id = Column(String, primary_key=True)
    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=True, index=True)
    supplier_id = Column(String(36), ForeignKey("suppliers.id"), nullable=True, index=True)
    quote_id = Column(String, nullable=False, index=True)
    status = Column(String(32), nullable=False, default="draft", index=True)
    store_name = Column(String, nullable=True)
    store_address = Column(String, nullable=True)
    pickup_time = Column(String, nullable=True)
    subtotal = Column(Float, nullable=False, default=0.0)
    tax = Column(Float, nullable=False, default=0.0)
    total = Column(Float, nullable=False, default=0.0)
    items = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class ContractorAccountModel(Base):
    __tablename__ = "contractor_accounts"

    contractor_id = Column(String, primary_key=True)
    email = Column(String, nullable=False)
    business_name = Column(String, nullable=False)
    phone = Column(String, nullable=False)
    plan = Column(String, nullable=False, default="starter")
    stripe_customer_id = Column(String, nullable=True)
    stripe_subscription_id = Column(String, nullable=True)
    quotes_used_this_month = Column(Integer, nullable=False, default=0)
    trades_enabled = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    trial_ends_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)


# ---------------------------------------------------------------------------
# Multi-tenant models (GATE 1)
#
# Every tenant-owned row carries organization_id and every read path goes
# through get_scoped() below, which refuses to return rows that do not belong
# to the authenticated organization. Cross-tenant access is a 404, not 403,
# so the existence of another tenant's data is never disclosed.
# ---------------------------------------------------------------------------

DEFAULT_ORG_SETTINGS = {
    "overhead_pct": 0.15,          # overhead as a share of direct cost
    "profit_margin_pct": 0.20,     # target margin on the final price
    "tax_rate": 0.08,
    "material_markup_pct": 0.0,    # optional markup applied to material cost
    "valid_days": 14,
    "terms": "Payment due upon completion. 50% deposit required to schedule.",
}

DEFAULT_BUSINESS_INFO = {
    "address": "",
    "phone": "",
    "email": "",
    "website": "",
    "logo_url": "",
    "license_number": "",
}

ONBOARDING_STEPS = [
    "company_created",
    "business_info",
    "pricing_configured",
    "skills_configured",
    "labor_rates_configured",
    "overhead_configured",
    "material_settings_configured",
    "employees_invited",
    "first_job_created",
    "first_quote_generated",
    "first_quote_approved",
]


class OrganizationModel(Base):
    __tablename__ = "organizations"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(255), nullable=False)
    slug = Column(String(255), nullable=False, unique=True, index=True)
    business_info = Column(JSON, nullable=False, default=lambda: dict(DEFAULT_BUSINESS_INFO))
    settings = Column(JSON, nullable=False, default=lambda: dict(DEFAULT_ORG_SETTINGS))
    onboarding = Column(JSON, nullable=False, default=list)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class UserModel(Base):
    __tablename__ = "users"
    __table_args__ = (
        # Email is globally unique: one login identity, exactly one org.
        {"sqlite_autoincrement": True},
    )

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)
    email = Column(String(320), nullable=False, unique=True, index=True)
    password_hash = Column(String(512), nullable=False)
    full_name = Column(String(255), nullable=False, default="")
    # OWNER | ADMIN | MANAGER | EMPLOYEE — validated in core.rbac
    role = Column(String(32), nullable=False, default="EMPLOYEE")
    is_active = Column(Boolean, nullable=False, default=True)
    email_verified_at = Column(DateTime, nullable=True)
    failed_login_count = Column(Integer, nullable=False, default=0)
    locked_until = Column(DateTime, nullable=True)
    last_login_at = Column(DateTime, nullable=True)
    invited_by = Column(String(36), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class AuthTokenModel(Base):
    """One-time / session tokens: refresh sessions, email verification,
    password reset. Only a SHA-256 digest of the token value is stored."""

    __tablename__ = "auth_tokens"

    token_hash = Column(String(64), primary_key=True)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    # refresh | email_verify | password_reset
    token_type = Column(String(32), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class SkillRateModel(Base):
    """Per-organization labor skills and their hourly rates."""

    __tablename__ = "skill_rates"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)
    name = Column(String(120), nullable=False)
    # junior | standard | master (informational tier for the skill)
    level = Column(String(32), nullable=False, default="standard")
    hourly_rate = Column(Float, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class OrgCustomerModel(Base):
    """A tenant's own customers — never shared across organizations."""

    __tablename__ = "org_customers"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    email = Column(String(320), nullable=True)
    phone = Column(String(32), nullable=True)
    address = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class JobModel(Base):
    __tablename__ = "jobs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)
    org_customer_id = Column(String(36), ForeignKey("org_customers.id"), nullable=False, index=True)
    quote_id = Column(String, nullable=True, index=True)
    trade = Column(String(64), nullable=False)
    title = Column(String(255), nullable=True)
    description = Column(Text, nullable=True)
    # draft | scheduled | in_progress | completed | canceled
    status = Column(String(32), nullable=False, default="draft", index=True)
    scheduled_date = Column(DateTime, nullable=True)
    duration_hours = Column(Float, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_by = Column(String(36), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class JobRequirementModel(Base):
    """Skills + hours a job needs (Approved Quote → required skills/hours)."""

    __tablename__ = "job_requirements"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)
    job_id = Column(String(36), ForeignKey("jobs.id"), nullable=False, index=True)
    skill_id = Column(String(36), ForeignKey("skill_rates.id"), nullable=False, index=True)
    workers_needed = Column(Integer, nullable=False, default=1)
    hours = Column(Float, nullable=False, default=0.0)


class JobAssignmentModel(Base):
    """A worker assigned to a job (available workers → schedule)."""

    __tablename__ = "job_assignments"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)
    job_id = Column(String(36), ForeignKey("jobs.id"), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    skill_id = Column(String(36), ForeignKey("skill_rates.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class DocumentModel(Base):
    """Immutable customer-facing documents (e.g. the rendered quote)."""

    __tablename__ = "documents"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)
    related_type = Column(String(64), nullable=False)
    related_id = Column(String(64), nullable=False, index=True)
    content = Column(JSON, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class SubscriptionModel(Base):
    """Stripe-backed subscription state for an organization. The Stripe
    webhook is the only writer of paid status — never the browser."""

    __tablename__ = "subscriptions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False, unique=True, index=True)
    plan = Column(String(32), nullable=False, default="starter")
    billing_cycle = Column(String(16), nullable=False, default="monthly")
    # none | trialing | active | past_due | canceled
    status = Column(String(32), nullable=False, default="trialing")
    stripe_customer_id = Column(String(120), nullable=True)
    stripe_subscription_id = Column(String(120), nullable=True, index=True)
    pending_plan = Column(String(32), nullable=True)
    trial_ends_at = Column(DateTime, nullable=True)
    grace_until = Column(DateTime, nullable=True)
    current_period_end = Column(DateTime, nullable=True)
    cancel_at_period_end = Column(Boolean, nullable=False, default=False)
    quotes_used_this_period = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class WebhookEventModel(Base):
    """Idempotency ledger for external webhooks, keyed by provider event id."""

    __tablename__ = "webhook_events"

    event_id = Column(String(255), primary_key=True)
    source = Column(String(32), nullable=False, default="stripe")
    event_type = Column(String(120), nullable=False)
    organization_id = Column(String(36), nullable=True, index=True)
    processed_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class AuditLogModel(Base):
    """Security & operations audit trail (no secrets, no message bodies)."""

    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    organization_id = Column(String(36), nullable=True, index=True)
    user_id = Column(String(36), nullable=True, index=True)
    action = Column(String(120), nullable=False, index=True)
    detail = Column(JSON, nullable=True)
    ip = Column(String(64), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)


# ---------------------------------------------------------------------------
# Tenant-scoped access helpers
# ---------------------------------------------------------------------------

class TenantAccessError(Exception):
    """Raised when a row exists but belongs to a different organization."""

async def get_scoped(session: AsyncSession, model, row_id: str, organization_id: str):
    """Fetch a row by primary key and return it ONLY if it belongs to the
    given organization. Cross-tenant lookups raise TenantAccessError so
    callers translate them to 404 (existence is never disclosed)."""
    row = await session.get(model, row_id)
    if row is None:
        return None
    if getattr(row, "organization_id", None) != organization_id:
        raise TenantAccessError(f"{model.__tablename__} access denied")
    return row


async def count_scoped(session: AsyncSession, model, organization_id: str, *conditions) -> int:
    from sqlalchemy import func as sa_func
    stmt = select(sa_func.count()).select_from(model).where(
        model.organization_id == organization_id, *conditions
    )
    return (await session.execute(stmt)).scalar_one()
