"""Manages multi-channel customer conversations (SMS, email, voice).

Postgres-backed — conversations survive restarts and scale across instances.
"""

import logging
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

from sqlalchemy import select, update, delete, and_
from sqlalchemy.ext.asyncio import AsyncSession

from core import database
from core.database import (
    ConversationModel, MessageModel, PhotoModel,
)

logger = logging.getLogger("conversation_manager")


class Channel(Enum):
    SMS = "sms"
    EMAIL = "email"
    VOICE = "voice"
    WEB = "web"


class ConversationStage(Enum):
    GREETING = "greeting"
    TRADE_SELECT = "trade_select"
    PHOTO_REQUEST = "photo_request"
    PHOTO_RECEIVED = "photo_received"
    ANALYZING = "analyzing"
    QUOTE_READY = "quote_ready"
    QUOTE_SENT = "quote_sent"
    FOLLOW_UP = "follow_up"
    BOOKED = "booked"
    CLOSED = "closed"


@dataclass
class Message:
    channel: Channel
    direction: str
    content: str
    media_urls: List[str] = field(default_factory=list)
    timestamp: str = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.utcnow().isoformat()


@dataclass
class Conversation:
    customer_id: str
    customer_phone: Optional[str]
    customer_email: Optional[str]
    trade: Optional[str]
    stage: ConversationStage
    messages: List[Message] = field(default_factory=list)
    photos_received: List[str] = field(default_factory=list)
    quote_id: Optional[str] = None
    appointment_id: Optional[str] = None
    created_at: str = None
    last_activity: str = None

    def __post_init__(self):
        now = datetime.utcnow().isoformat()
        if self.created_at is None:
            self.created_at = now
        if self.last_activity is None:
            self.last_activity = now

    def add_message(self, msg: Message):
        self.messages.append(msg)
        self.last_activity = datetime.utcnow().isoformat()

    def add_photos(self, urls: List[str]):
        self.photos_received.extend(urls)
        self.last_activity = datetime.utcnow().isoformat()

    def to_dict(self) -> dict:
        return {
            "customer_id": self.customer_id,
            "customer_phone": self.customer_phone,
            "customer_email": self.customer_email,
            "trade": self.trade,
            "stage": self.stage.value,
            "message_count": len(self.messages),
            "photos_count": len(self.photos_received),
            "quote_id": self.quote_id,
            "appointment_id": self.appointment_id,
            "created_at": self.created_at,
            "last_activity": self.last_activity,
        }

    @classmethod
    def from_model(cls, model: ConversationModel) -> "Conversation":
        """Hydrate a domain Conversation from the DB row + its relationships."""
        messages = [
            Message(
                channel=Channel(m.channel),
                direction=m.direction,
                content=m.content,
                timestamp=m.timestamp.isoformat() if m.timestamp else None,
            )
            for m in (model.messages or [])
        ]
        photos = [p.url for p in (model.photos or [])]

        return cls(
            customer_id=model.id,
            customer_phone=model.customer_phone,
            customer_email=model.customer_email,
            trade=model.trade,
            stage=ConversationStage(model.stage),
            messages=messages,
            photos_received=photos,
            quote_id=model.quote_id,
            appointment_id=model.appointment_id,
            created_at=model.created_at.isoformat() if model.created_at else None,
            last_activity=model.last_activity.isoformat() if model.last_activity else None,
        )


class ConversationManager:
    """Manages all customer conversations — backed by Postgres."""

    async def get_or_create(
        self,
        customer_id: str,
        phone: Optional[str] = None,
        email: Optional[str] = None,
    ) -> Conversation:
        async with database.AsyncSessionLocal() as session:
            result = await session.execute(
                select(ConversationModel).where(ConversationModel.id == customer_id)
            )
            model = result.scalar_one_or_none()

            if model is None:
                now = datetime.utcnow()
                model = ConversationModel(
                    id=customer_id,
                    customer_phone=phone,
                    customer_email=email,
                    trade=None,
                    stage="greeting",
                    created_at=now,
                    last_activity=now,
                )
                session.add(model)
                await session.commit()
                await session.refresh(model)
                logger.info(f"Created conversation for {customer_id}")
            elif phone and not model.customer_phone:
                model.customer_phone = phone
                await session.commit()
            elif email and not model.customer_email:
                model.customer_email = email
                await session.commit()

            # Re-fetch with relationships
            result = await session.execute(
                select(ConversationModel).where(ConversationModel.id == customer_id)
            )
            model = result.scalar_one()
            return Conversation.from_model(model)

    async def get(self, customer_id: str) -> Optional[Conversation]:
        async with database.AsyncSessionLocal() as session:
            result = await session.execute(
                select(ConversationModel).where(ConversationModel.id == customer_id)
            )
            model = result.scalar_one_or_none()
            return Conversation.from_model(model) if model else None

    async def update_stage(self, customer_id: str, stage: ConversationStage):
        async with database.AsyncSessionLocal() as session:
            await session.execute(
                update(ConversationModel)
                .where(ConversationModel.id == customer_id)
                .values(stage=stage.value, last_activity=datetime.utcnow())
            )
            await session.commit()

    async def set_trade(self, customer_id: str, trade: str):
        async with database.AsyncSessionLocal() as session:
            await session.execute(
                update(ConversationModel)
                .where(ConversationModel.id == customer_id)
                .values(trade=trade, last_activity=datetime.utcnow())
            )
            await session.commit()

    async def add_message(self, customer_id: str, msg: Message):
        """Persist a message to the conversation."""
        async with database.AsyncSessionLocal() as session:
            model = MessageModel(
                conversation_id=customer_id,
                channel=msg.channel.value,
                direction=msg.direction,
                content=msg.content,
                timestamp=datetime.utcnow(),
            )
            session.add(model)
            await session.execute(
                update(ConversationModel)
                .where(ConversationModel.id == customer_id)
                .values(last_activity=datetime.utcnow())
            )
            await session.commit()

    async def add_photos(self, customer_id: str, urls: List[str]):
        """Persist photo URLs to the conversation."""
        async with database.AsyncSessionLocal() as session:
            for url in urls:
                photo = PhotoModel(conversation_id=customer_id, url=url)
                session.add(photo)
            await session.execute(
                update(ConversationModel)
                .where(ConversationModel.id == customer_id)
                .values(last_activity=datetime.utcnow())
            )
            await session.commit()

    async def set_quote_id(self, customer_id: str, quote_id: str):
        async with database.AsyncSessionLocal() as session:
            await session.execute(
                update(ConversationModel)
                .where(ConversationModel.id == customer_id)
                .values(quote_id=quote_id, last_activity=datetime.utcnow())
            )
            await session.commit()

    async def get_stale_conversations(self, hours: int = 24) -> List[Conversation]:
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        async with database.AsyncSessionLocal() as session:
            result = await session.execute(
                select(ConversationModel).where(
                    and_(
                        ConversationModel.last_activity < cutoff,
                        ConversationModel.stage.notin_(["booked", "closed"]),
                    )
                )
            )
            models = result.scalars().all()
            return [Conversation.from_model(m) for m in models]

    async def list_all(self) -> List[Conversation]:
        async with database.AsyncSessionLocal() as session:
            result = await session.execute(select(ConversationModel))
            return [Conversation.from_model(model) for model in result.scalars().all()]

    async def get_active_count(self) -> int:
        async with database.AsyncSessionLocal() as session:
            result = await session.execute(
                select(ConversationModel).where(
                    ConversationModel.stage.notin_(["booked", "closed"])
                )
            )
            return len(result.scalars().all())

    async def get_conversion_rate(self) -> float:
        async with database.AsyncSessionLocal() as session:
            total_result = await session.execute(select(ConversationModel))
            total = len(total_result.scalars().all())
            booked_result = await session.execute(
                select(ConversationModel).where(ConversationModel.stage == "booked")
            )
            booked = len(booked_result.scalars().all())
            return (booked / total * 100) if total > 0 else 0.0


# ---------------------------------------------------------------------------
# Response templates (unchanged)
# ---------------------------------------------------------------------------

RESPONSE_TEMPLATES = {
    "greeting": (
        "Hi! I'm the E-ZFlow assistant. I can give you an instant quote "
        "for your project. What type of work do you need?\n\n"
        "Reply with: landscaping, roofing, plumbing, autobody, or electrical"
    ),
    "trade_select": (
        "Great! To give you an accurate quote, I'll need photos of the work area. "
        "Please send {required_photos}.\n\n"
        "You can text or email them to this number/address."
    ),
    "photo_request": (
        "I need a few more photos to complete your quote. Please send: {missing_photos}"
    ),
    "analyzing": (
        "Thanks for the photos! I'm analyzing them now. This takes about 30 seconds..."
    ),
    "quote_ready": (
        "Your quote is ready! Here are the details:\n\n{quote_text}\n\n"
        "Reply ACCEPT to schedule the work or QUESTION to talk to a human."
    ),
    "follow_up": (
        "Hi! Just following up on your quote #{quote_id}. "
        "Are you ready to move forward? Reply YES to book or NO if you have questions."
    ),
    "booked": (
        "Excellent! Your job has been scheduled. "
        "An agent will contact you within 24 hours to confirm timing. "
        "Quote #{quote_id} | Total: ${total}"
    ),
}


def get_response(stage: str, **kwargs) -> str:
    template = RESPONSE_TEMPLATES.get(stage, "How can I help you today?")
    try:
        return template.format(**kwargs)
    except KeyError:
        return template
