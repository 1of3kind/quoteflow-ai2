"""Security helpers shared by the public API endpoints."""

import hashlib
import hmac
import os
from urllib.parse import urlparse

from fastapi import HTTPException, Request, status
from twilio.request_validator import RequestValidator


def required_secret(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value or value.lower() in {"replace-me", "change-me"}:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{name} is not configured",
        )
    return value


def api_key_is_valid(candidate: str) -> bool:
    try:
        secret = required_secret("FEEDBACK_API_KEY")
        return hmac.compare_digest(candidate, secret)
    except HTTPException:
        return False


async def verify_twilio_request(request: Request, form: dict[str, str]) -> None:
    """Reject spoofed Twilio webhooks before any side effect occurs."""
    token = required_secret("TWILIO_AUTH_TOKEN")
    public_base_url = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    url = f"{public_base_url}{request.url.path}" if public_base_url else str(request.url)
    if not RequestValidator(token).validate(url, form, request.headers.get("X-Twilio-Signature", "")):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid Twilio signature")


async def verify_email_webhook(request: Request) -> None:
    """Protect the email ingestion endpoint with a deployment-specific secret."""
    expected = required_secret("EMAIL_WEBHOOK_SECRET")
    provided = request.headers.get("X-EZFlow-Webhook-Secret", "")
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid email webhook secret")


def is_allowed_twilio_media_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname in {"api.twilio.com", "media.twiliocdn.com"}
