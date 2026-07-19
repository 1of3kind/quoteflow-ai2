"""Manages multi-channel customer conversations (SMS, email, voice)."""

import json
import logging
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

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


class ConversationManager:
    """Manages all customer conversations across channels."""

    def __init__(self):
        self.conversations: Dict[str, Conversation] = {}

    def get_or_create(
        self, customer_id: str,
        phone: Optional[str] = None,
        email: Optional[str] = None,
    ) -> Conversation:
        if customer_id not in self.conversations:
            self.conversations[customer_id] = Conversation(
                customer_id=customer_id,
                customer_phone=phone,
                customer_email=email,
                trade=None,
                stage=ConversationStage.GREETING,
            )
            logger.info(f"Created conversation for {customer_id}")
        return self.conversations[customer_id]

    def get(self, customer_id: str) -> Optional[Conversation]:
        return self.conversations.get(customer_id)

    def update_stage(self, customer_id: str, stage: ConversationStage):
        conv = self.conversations.get(customer_id)
        if conv:
            conv.stage = stage
            conv.last_activity = datetime.utcnow().isoformat()

    def get_stale_conversations(self, hours: int = 24) -> List[Conversation]:
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        stale = []
        for conv in self.conversations.values():
            last = datetime.fromisoformat(conv.last_activity)
            if last < cutoff and conv.stage not in [ConversationStage.BOOKED, ConversationStage.CLOSED]:
                stale.append(conv)
        return stale

    def get_active_count(self) -> int:
        return len([
            c for c in self.conversations.values()
            if c.stage not in [ConversationStage.BOOKED, ConversationStage.CLOSED]
        ])

    def get_conversion_rate(self) -> float:
        total = len(self.conversations)
        booked = len([c for c in self.conversations.values() if c.stage == ConversationStage.BOOKED])
        return (booked / total * 100) if total > 0 else 0.0


RESPONSE_TEMPLATES = {
    "greeting": (
        "Hi! I'm the QuoteFlow AI assistant. I can give you an instant quote "
        "for your project. What type of work do you need?\n\n"
        "Reply with: landscaping, roofing, plumbing, autobody, or electrical"
    ),
    "trade_select": (
        "Great! To give you an accurate quote, I\'ll need photos of the work area. "
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
