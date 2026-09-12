"""Tenant resolution and request-scoped guards (GATE 1 + GATE 2).

Every organization-scoped endpoint depends on `get_current_user`, which:
  1. verifies the JWT access token,
  2. loads the user and organization,
  3. rejects inactive users/orgs and (optionally) unverified emails,
  4. returns an AuthContext carrying organization_id.

Handlers must use `get_scoped()` (core.database) for every read of
tenant-owned data so cross-tenant fetches return 404.
"""

import logging
from datetime import datetime

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import decode_access_token, REQUIRE_VERIFIED_EMAIL
from core.database import get_session, UserModel, OrganizationModel, TenantAccessError
from jose import JWTError

logger = logging.getLogger("tenancy")

_bearer = HTTPBearer(auto_error=False)


class AuthContext:
    __slots__ = ("user_id", "organization_id", "role", "email", "org_active", "email_verified")

    def __init__(self, user: UserModel, org: OrganizationModel):
        self.user_id = user.id
        self.organization_id = org.id
        self.role = user.role
        self.email = user.email
        self.org_active = bool(org.is_active)
        self.email_verified = user.email_verified_at is not None


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
) -> AuthContext:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        payload = decode_access_token(credentials.credentials)
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user = await session.get(UserModel, payload.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Account is not active")

    org = await session.get(OrganizationModel, user.organization_id)
    if org is None or not org.is_active:
        raise HTTPException(status_code=403, detail="Organization is disabled")

    if REQUIRE_VERIFIED_EMAIL and user.email_verified_at is None:
        raise HTTPException(status_code=403, detail="Email address not verified")

    return AuthContext(user=user, org=org)


def tenant_not_found(model_name: str = "Resource") -> HTTPException:
    """Uniform 404 for missing OR cross-tenant rows (no existence leak)."""
    return HTTPException(status_code=404, detail=f"{model_name} not found")


async def scoped_or_404(session: AsyncSession, model, row_id: str, ctx: AuthContext, name: str = "Resource"):
    """Fetch a tenant-owned row for the current org, or raise 404 —
    including when the row exists but belongs to another organization."""
    from core.database import get_scoped
    try:
        row = await get_scoped(session, model, row_id, ctx.organization_id)
    except TenantAccessError:
        logger.warning(
            "security event=cross_tenant_access_denied user=%s org=%s table=%s row=%s",
            ctx.user_id, ctx.organization_id, model.__tablename__, row_id,
        )
        raise tenant_not_found(name)
    if row is None:
        raise tenant_not_found(name)
    return row


async def audit(session: AsyncSession, action: str, ctx: "AuthContext | None" = None,
                ip: str | None = None, **detail) -> None:
    """Record a security/ops audit entry. Never include secrets or secrets-
    shaped values in `detail`."""
    from core.database import AuditLogModel
    session.add(AuditLogModel(
        organization_id=ctx.organization_id if ctx else detail.pop("organization_id", None),
        user_id=ctx.user_id if ctx else None,
        action=action,
        detail=detail or None,
        ip=ip,
    ))
