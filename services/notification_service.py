"""Unified notification service across SMS, email, voice."""

import logging
from typing import Optional, List
from dataclasses import dataclass

from services.sms_handler import SMSHandler
from services.email_processor import EmailProcessor

logger = logging.getLogger("notification_service")


@dataclass
class Notification:
    channel: str
    to_phone: Optional[str]
    to_email: Optional[str]
    subject: Optional[str]
    body: str
    media_url: Optional[str] = None


class NotificationService:
    """Sends notifications through customer\'s preferred channel(s)."""

    def __init__(self):
        self.sms = SMSHandler()
        self.email = EmailProcessor()

    async def send(self, notification: Notification) -> dict:
        results = {}
        if notification.channel in ("sms", "both") and notification.to_phone:
            if notification.media_url:
                results["sms"] = self.sms.send_with_media(
                    notification.to_phone, notification.body, notification.media_url,
                )
            else:
                results["sms"] = self.sms.send_text(notification.to_phone, notification.body)

        if notification.channel in ("email", "both") and notification.to_email:
            results["email"] = await self.email.send_quote_email(
                notification.to_email,
                notification.subject or "Your Quote from E-ZFlow",
                notification.body,
                "quote_id",
            )
        return results

    def send_quote(self, phone: Optional[str], email: Optional[str], quote_text: str, quote_id: str) -> dict:
        return self.send(Notification(
            channel="both" if (phone and email) else ("sms" if phone else "email"),
            to_phone=phone, to_email=email,
            subject=f"Your Quote #{quote_id} - E-ZFlow",
            body=quote_text,
        ))

    def send_follow_up(self, phone: Optional[str], email: Optional[str], quote_id: str, message: str) -> dict:
        return self.send(Notification(
            channel="sms" if phone else "email",
            to_phone=phone, to_email=email,
            subject=f"Follow-up: Quote #{quote_id}",
            body=message,
        ))
