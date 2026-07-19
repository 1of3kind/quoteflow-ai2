"""Voice call handler using Twilio for phone-based quotes."""

import os
import logging
from typing import Optional

from twilio.twiml.voice_response import VoiceResponse, Gather, Say

logger = logging.getLogger("voice_handler")


class VoiceHandler:
    """Handles inbound voice calls for quote requests."""

    def __init__(self):
        self.from_number = os.getenv("TWILIO_PHONE_NUMBER", "+1234567890")

    def create_welcome_response(self) -> str:
        resp = VoiceResponse()
        gather = Gather(
            num_digits=1,
            action="/voice/trade-select",
            method="POST",
            timeout=5,
        )
        gather.say(
            "Thank you for calling QuoteFlow. "
            "For landscaping, press 1. "
            "For roofing, press 2. "
            "For plumbing, press 3. "
            "For auto body, press 4. "
            "For electrical, press 5.",
            voice="Polly.Joanna",
        )
        resp.append(gather)
        resp.say("We didn't receive your selection. Please call back.", voice="Polly.Joanna")
        return str(resp)

    def create_photo_instructions(self, trade: str) -> str:
        resp = VoiceResponse()
        resp.say(
            f"You selected {trade}. To get your instant quote, "
            f"please text photos of the work area to this phone number. "
            f"Once we receive your photos, we'll text you a quote within 2 minutes.",
            voice="Polly.Joanna",
        )
        resp.hangup()
        return str(resp)

    def create_quote_ready_callback(self, quote_text: str) -> str:
        resp = VoiceResponse()
        resp.say(
            "Your quote is ready. We have sent the full details via text message. "
            "To accept this quote, press 1. To speak with an agent, press 2.",
            voice="Polly.Joanna",
        )
        gather = Gather(num_digits=1, action="/voice/quote-response", method="POST")
        resp.append(gather)
        return str(resp)

    def create_transfer(self) -> str:
        resp = VoiceResponse()
        resp.say("Please hold while we connect you to an agent.", voice="Polly.Joanna")
        resp.dial(os.getenv("AGENT_PHONE_NUMBER", "+1234567890"))
        return str(resp)
