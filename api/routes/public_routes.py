"""Public customer quote-approval endpoints (MUST-FIX #2).

The customer approves with a token link — no employee login:

    GET  /q/{quote_id}/{token}            → view the quote document
    POST /q/{quote_id}/{token}/approve    → approve the quote (creates the job)

Token properties: cryptographically random (secrets.token_urlsafe), bound to
one quote and one organization, single-purpose (approval only), expiring
(quote expiry), revocable (POST /quotes/{id}/revoke-approval), stored only
as a SHA-256 digest, and rate-limited per IP.

The quote_id in the URL is an identifier, not the secret — every operation
requires the matching token, and failures never reveal whether a quote id
exists.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import RateLimiter, client_ip, token_digest
from core.database import get_session, QuoteModel
from core.observability import security_event
from api.routes.quotes_routes import build_quote_document, _accept_quote_for_customer

logger = logging.getLogger("api.public")

router = APIRouter(prefix="/q", tags=["public-approval"])

# Customer-facing pacing: enough for a human to view + approve, hostile to
# brute force (32 requests / 10 min per IP per token bucket).
public_rate_limiter = RateLimiter(max_requests=32, window_seconds=600)


def _guard(request: Request) -> None:
    if not public_rate_limiter.allow(client_ip(request), "public_approval"):
        security_event("public_approval_rate_limited", ip=client_ip(request))
        raise HTTPException(status_code=429, detail="Too many attempts — try again later")


async def _get_valid_quote(session: AsyncSession, quote_id: str, token: str) -> QuoteModel:
    from datetime import datetime
    q = await session.get(QuoteModel, quote_id)
    if (q is None
            or not q.approval_token_hash
            or q.approval_revoked_at is not None
            or q.approval_token_hash != token_digest(token)
            or (q.approval_token_expires_at and q.approval_token_expires_at < datetime.utcnow())):
        raise HTTPException(status_code=404, detail="Not found")
    return q


@router.get("/{quote_id}/{token}")
async def view_quote(quote_id: str, token: str, request: Request,
                     session: AsyncSession = Depends(get_session)):
    """Customer view of the quote — same document the business sent."""
    _guard(request)
    q = await _get_valid_quote(session, quote_id, token)
    document = await build_quote_document(session, q)
    return {"quote": document, "approvable": q.status == "sent"}


@router.post("/{quote_id}/{token}/approve")
async def approve_quote(quote_id: str, token: str, request: Request,
                        session: AsyncSession = Depends(get_session)):
    """Customer approval: quote → accepted → job created. No login needed;
    the token is the authorization."""
    _guard(request)
    q = await _get_valid_quote(session, quote_id, token)
    result = await _accept_quote_for_customer(session, q, via="link")
    security_event("quote_approved_by_customer", quote_id=quote_id,
                   organization_id=q.organization_id, via="link")
    return result
