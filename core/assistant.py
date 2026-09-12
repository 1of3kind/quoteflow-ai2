"""QuoteFlow AI assistant (Growth Feature 1).

Interprets a natural-language command and executes the actual workflow:

    "Create a quote for John Smith. Replace the water heater
     and schedule it for Tuesday."

Parsing strategy:
  1. LLM extraction (OPENAI_API_KEY + ASSISTANT_USE_LLM=true): the model
     returns a strict JSON action spec; any parse failure falls back to —
  2. Deterministic rule-based parser (always available, no key required).

Execution reuses the authenticated route handlers (create_quote / create_job),
so the caller's RBAC, org scoping, billing quota, and audit trail all apply
exactly as if the user had clicked through the API themselves.
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger("assistant")

WEEKDAYS = {
    "monday": 0, "tuesday": 1, "tues": 1, "wednesday": 2, "thursday": 3,
    "thurs": 3, "friday": 4, "saturday": 5, "sunday": 6,
}

# trade keywords, most specific first
TRADE_KEYWORDS = [
    ("water heater", "plumbing"),
    ("waterheater", "plumbing"),
    ("landscap", "landscaping"),
    ("lawn", "landscaping"),
    ("mulch", "landscaping"),
    ("sod", "landscaping"),
    ("garden", "landscaping"),
    ("roof", "roofing"),
    ("shingle", "roofing"),
    ("gutter", "roofing"),
    ("wiring", "electrical"),
    ("outlet", "electrical"),
    ("panel", "electrical"),
    ("electrical", "electrical"),
    ("dent", "autobody"),
    ("bumper", "autobody"),
    ("paint job", "autobody"),
    ("collision", "autobody"),
    ("leak", "plumbing"),
    ("pipe", "plumbing"),
    ("drain", "plumbing"),
    ("toilet", "plumbing"),
    ("plumb", "plumbing"),
]


@dataclass
class ParsedCommand:
    intent: str = "unknown"                # quote | unknown
    customer_name: Optional[str] = None
    trade: Optional[str] = None
    description: str = ""
    hours: Optional[float] = None
    schedule_date: Optional[str] = None    # ISO date
    send_quote: bool = False
    source: str = "rules"                  # rules | llm
    notes: list = field(default_factory=list)


def next_weekday(name: str, today: Optional[datetime] = None) -> Optional[str]:
    """Date (ISO) of the next occurrence of a weekday, strictly after today."""
    target = WEEKDAYS.get(name.lower())
    if target is None:
        return None
    today = today or datetime.utcnow()
    days_ahead = (target - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7  # "Tuesday" said on a Tuesday means next Tuesday
    return (today + timedelta(days=days_ahead)).date().isoformat()


def detect_trade(text: str) -> Optional[str]:
    lowered = text.lower()
    for keyword, trade in TRADE_KEYWORDS:
        if keyword in lowered:
            return trade
    return None


def parse_command_rules(text: str) -> ParsedCommand:
    cmd = ParsedCommand()
    cmd.description = text.strip()

    # Customer: "…for John Smith" / "…for John"
    m = re.search(r"\bfor\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)", text)
    if m:
        cmd.customer_name = m.group(1)

    cmd.trade = detect_trade(text)

    if re.search(r"\b(quote|estimate|bid)\b", text, re.IGNORECASE):
        cmd.intent = "quote"

    # Explicit hours: "8 hours" / "8h"
    m = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:hours|hrs|h)\b", text, re.IGNORECASE)
    if m:
        cmd.hours = float(m.group(1))

    # Scheduling: weekday names or tomorrow
    for name in WEEKDAYS:
        if re.search(rf"\b(on\s+)?{name}\b", text, re.IGNORECASE):
            cmd.schedule_date = next_weekday(name)
            break
    if not cmd.schedule_date and re.search(r"\btomorrow\b", text, re.IGNORECASE):
        cmd.schedule_date = (datetime.utcnow() + timedelta(days=1)).date().isoformat()

    if re.search(r"\b(send|email|text)\b.*\b(quote|it|them)\b", text, re.IGNORECASE) \
            or re.search(r"\bsend\b", text, re.IGNORECASE):
        cmd.send_quote = True

    if not cmd.customer_name:
        cmd.notes.append("No customer name detected — say 'quote for <name>'")
    if not cmd.trade:
        cmd.notes.append("No trade detected — mention the work (e.g. 'replace the water heater')")
    return cmd


async def parse_command_llm(text: str) -> Optional[ParsedCommand]:
    """LLM extraction. Returns None on any failure (caller falls back)."""
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key or os.getenv("ASSISTANT_USE_LLM", "false").lower() != "true":
        return None
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=api_key)
        system = (
            "You extract structured scheduling/quoting commands for a trades "
            "business app. Reply ONLY with JSON: {\"customer_name\": string|null, "
            "\"trade\": one of landscaping|roofing|plumbing|autobody|electrical|null, "
            "\"description\": string, \"hours\": number|null, "
            "\"schedule_weekday\": monday..sunday|\"tomorrow\"|null, "
            "\"send_quote\": boolean, \"intent\": \"quote\"|\"unknown\"}. "
            "Today is {today}.".replace("{today}", datetime.utcnow().strftime("%A %Y-%m-%d"))
        )
        response = await client.chat.completions.create(
            model=os.getenv("ASSISTANT_MODEL", "gpt-4o-mini"),
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": text}],
            temperature=0,
            max_tokens=300,
        )
        raw = response.choices[0].message.content or ""
        data = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])

        cmd = ParsedCommand()
        cmd.source = "llm"
        cmd.intent = data.get("intent", "unknown")
        cmd.customer_name = data.get("customer_name")
        cmd.trade = data.get("trade")
        cmd.description = data.get("description") or text.strip()
        cmd.hours = data.get("hours")
        if data.get("schedule_weekday"):
            weekday = str(data["schedule_weekday"]).lower()
            if weekday == "tomorrow":
                cmd.schedule_date = (datetime.utcnow() + timedelta(days=1)).date().isoformat()
            else:
                cmd.schedule_date = next_weekday(weekday)
        cmd.send_quote = bool(data.get("send_quote"))
        return cmd
    except Exception as e:
        logger.warning("assistant LLM parse failed, using rules: %s", type(e).__name__)
        return None


async def parse_command(text: str) -> ParsedCommand:
    cmd = await parse_command_llm(text)
    if cmd is not None:
        return cmd
    return parse_command_rules(text)
