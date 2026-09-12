"""Role-based access control (GATE 2).

Roles: OWNER > ADMIN > MANAGER > EMPLOYEE

Permission matrix (mirrors the product spec):

    Function            Owner  Admin  Manager  Employee
    Billing              ✅      ❌      ❌       ❌
    Company settings     ✅      ✅      ❌       ❌
    Users                ✅      ✅      ❌       ❌
    Quotes               ✅      ✅      ✅       ✅
    Jobs                 ✅      ✅      ✅       ✅
    Pricing formula      ✅      ✅    Limited    ❌
    Reports              ✅      ✅      ✅     Limited

"Manager limited pricing": managers may apply per-quote overrides on a job
they are quoting, but cannot change the organization's default pricing
formula (rates, overhead, profit target). Employees see only their own
quotes in reports.
"""

from enum import Enum

from fastapi import Depends, HTTPException, status

from core.tenancy import AuthContext


class Role(str, Enum):
    OWNER = "OWNER"
    ADMIN = "ADMIN"
    MANAGER = "MANAGER"
    EMPLOYEE = "EMPLOYEE"


ALL_ROLES = {r.value for r in Role}

# Every permission and exactly which roles hold it.
PERMISSIONS: dict[str, set[str]] = {
    "billing": {Role.OWNER.value},
    "org:settings": {Role.OWNER.value, Role.ADMIN.value},
    "org:users": {Role.OWNER.value, Role.ADMIN.value},
    "org:read": ALL_ROLES,
    "quotes:read": ALL_ROLES,
    "quotes:write": ALL_ROLES,
    "jobs:read": ALL_ROLES,
    "jobs:write": ALL_ROLES,
    # Organization-level pricing defaults (labor rates, overhead, profit target)
    "pricing:configure": {Role.OWNER.value, Role.ADMIN.value},
    # Per-quote adjustments on top of the org formula
    "pricing:override": {Role.OWNER.value, Role.ADMIN.value, Role.MANAGER.value},
    # Employees see only their own quotes/jobs in reports
    "reports:full": {Role.OWNER.value, Role.ADMIN.value, Role.MANAGER.value},
    "reports:own": ALL_ROLES,
    "materials:read": ALL_ROLES,
    "materials:write": {Role.OWNER.value, Role.ADMIN.value, Role.MANAGER.value},
    "orders:read": ALL_ROLES,
    "orders:write": {Role.OWNER.value, Role.ADMIN.value, Role.MANAGER.value},
    "schedules:read": ALL_ROLES,
    "schedules:write": ALL_ROLES,
    "documents:read": ALL_ROLES,
    "documents:write": ALL_ROLES,
    "customers:read": ALL_ROLES,
    "customers:write": ALL_ROLES,
}


def can(role: str, permission: str) -> bool:
    allowed = PERMISSIONS.get(permission)
    if not allowed:
        return False
    return role in allowed


def require_permission(permission: str):
    """FastAPI dependency enforcing a permission on the current user."""
    return _permission_dependency(permission)


def _permission_dependency(permission: str):
    from core.tenancy import get_current_user

    async def dependency(ctx: AuthContext = Depends(get_current_user)) -> AuthContext:
        if not can(ctx.role, permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role {ctx.role} may not perform '{permission}'",
            )
        return ctx
    return dependency
