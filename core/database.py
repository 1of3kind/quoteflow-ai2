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
    url = (raw_url or os.getenv("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/quoteflow")).strip()
    
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
    trade = Column(String, nullable=False)
    customer_id = Column(String, nullable=False, index=True)
    line_items = Column(JSON, nullable=False, default=list)
    subtotal = Column(Float, nullable=False)
    tax_rate = Column(Float, nullable=False)
    tax_amount = Column(Float, nullable=False)
    total = Column(Float, nullable=False)
    breakdown = Column(JSON, nullable=False, default=dict)
    valid_days = Column(Integer, nullable=False, default=14)
    notes = Column(Text, nullable=True)
    tier_applied = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class AppointmentModel(Base):
    __tablename__ = "appointments"

    appointment_id = Column(String, primary_key=True)
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


class MaterialOrderModel(Base):
    __tablename__ = "material_orders"

    order_id = Column(String, primary_key=True)
    quote_id = Column(String, nullable=False, index=True)
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
