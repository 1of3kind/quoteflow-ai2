"""Email handler for receiving photos and sending quotes via SendGrid."""

import os
import logging
import re
from typing import List, Optional
from dataclasses import dataclass
from datetime import datetime

import httpx

logger = logging.getLogger("email_processor")


@dataclass
class EmailMessage:
    from_email: str
    to_email: str
    subject: str
    body_text: str
    body_html: Optional[str]
    attachments: List[dict]
    received_at: str


class EmailProcessor:
    """Processes incoming emails with photo attachments."""

    def __init__(self):
        self.sendgrid_key = os.getenv("SENDGRID_API_KEY", "")
        self.webhook_secret = os.getenv("EMAIL_WEBHOOK_SECRET", "")

    def parse_inbound(self, payload: dict) -> EmailMessage:
        headers = payload.get("headers", "")
        from_match = re.search(r'From: (.+)', headers)
        to_match = re.search(r'To: (.+)', headers)

        attachments = []
        for i in range(int(payload.get("attachments", 0))):
            att = {
                "filename": payload.get(f"attachment{i+1}", f"attachment_{i+1}"),
                "url": payload.get(f"attachment{i+1}_url", ""),
                "content_type": payload.get(f"attachment{i+1}_type", "application/octet-stream"),
            }
            attachments.append(att)

        return EmailMessage(
            from_email=from_match.group(1) if from_match else payload.get("from", ""),
            to_email=to_match.group(1) if to_match else payload.get("to", ""),
            subject=payload.get("subject", ""),
            body_text=payload.get("text", ""),
            body_html=payload.get("html", None),
            attachments=attachments,
            received_at=datetime.utcnow().isoformat(),
        )

    async def send_quote_email(self, to: str, subject: str, quote_text: str, quote_id: str) -> bool:
        if not self.sendgrid_key:
            logger.warning("No SendGrid API key configured")
            return False

        url = "https://api.sendgrid.com/v3/mail/send"
        headers = {
            "Authorization": f"Bearer {self.sendgrid_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "personalizations": [{"to": [{"email": to}]}],
            "from": {"email": "quotes@quoteflow.ai", "name": "QuoteFlow AI"},
            "subject": subject,
            "content": [
                {"type": "text/plain", "value": quote_text},
                {"type": "text/html", "value": f"<pre>{quote_text}</pre><p>Reply to this email to accept or ask questions.</p>"},
            ],
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, headers=headers, json=payload)
            return resp.status_code == 202
