"""Twilio SMS handler for receiving photos and sending quotes."""

import os
import logging
from typing import Optional, List
from datetime import datetime

from twilio.rest import Client as TwilioClient
from twilio.twiml.messaging_response import MessagingResponse

logger = logging.getLogger("sms_handler")


class SMSHandler:
    """Handles inbound/outbound SMS with media support."""

    def __init__(self):
        self.account_sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
        self.auth_token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
        self.client = None
        if self.account_sid and self.auth_token:
            try:
                self.client = TwilioClient(self.account_sid, self.auth_token)
            except Exception as e:
                logger.warning(f"Could not initialize Twilio client: {e}")
        else:
            logger.info("Twilio credentials not configured — SMS/MMS client running in simulation mode")
        self.from_number = os.getenv("TWILIO_PHONE_NUMBER", "+1234567890")

    def send_text(self, to: str, body: str) -> dict:
        if not self.client:
            logger.warning(f"SMS send skipped (no Twilio client configured): {to}")
            return {"status": "skipped_no_credentials", "to": to}
        try:
            msg = self.client.messages.create(
                body=body, from_=self.from_number, to=to,
            )
            return {"sid": msg.sid, "status": msg.status, "to": to}
        except Exception as e:
            logger.error(f"SMS send failed: {e}")
            return {"error": str(e), "to": to}

    def send_with_media(self, to: str, body: str, media_url: Optional[str] = None) -> dict:
        if not self.client:
            logger.warning(f"SMS with media skipped (no Twilio client configured): {to}")
            return {"status": "skipped_no_credentials", "to": to}
        try:
            kwargs = {"body": body, "from_": self.from_number, "to": to}
            if media_url:
                kwargs["media_url"] = [media_url]
            msg = self.client.messages.create(**kwargs)
            return {"sid": msg.sid, "status": msg.status, "to": to}
        except Exception as e:
            logger.error(f"SMS with media failed: {e}")
            return {"error": str(e), "to": to}

    def parse_inbound(self, form_data: dict) -> dict:
        return {
            "from": form_data.get("From", ""),
            "to": form_data.get("To", ""),
            "body": form_data.get("Body", "").strip(),
            "media_count": int(form_data.get("NumMedia", 0)),
            "media_urls": [
                form_data.get(f"MediaUrl{i}", "")
                for i in range(int(form_data.get("NumMedia", 0)))
            ],
            "message_sid": form_data.get("MessageSid", ""),
            "timestamp": datetime.utcnow().isoformat(),
        }

    def create_response(self, message: str) -> str:
        resp = MessagingResponse()
        resp.message(message)
        return str(resp)

    def get_media(self, media_url: str) -> bytes:
        import httpx
        auth = (os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
        resp = httpx.get(media_url, auth=auth)
        return resp.content
