# RBAC — Roles and Permissions

Roles: `OWNER` > `ADMIN` > `MANAGER` > `EMPLOYEE`. Enforced by `core/rbac.py`
(`PERMISSIONS`) via the `require_permission(...)` dependency on every protected
endpoint. A user belongs to exactly one organization; every tenant-owned query
is additionally scoped to that organization regardless of role.

## Capability matrix

| Function | Owner | Admin | Manager | Employee |
|---|---|---|---|---|
| Billing (plans, checkout, portal, invoices) | ✅ | ❌ | ❌ | ❌ |
| Company settings (business info, pricing defaults) | ✅ | ✅ | ❌ | ❌ |
| Users (invite, role changes, deactivate) | ✅ | ✅ | ❌ | ❌ |
| Quotes (create, send, accept) | ✅ | ✅ | ✅ | ✅ |
| Jobs (create, schedule, complete) | ✅ | ✅ | ✅ | ✅ |
| Pricing formula (org-wide rates/overhead/margin) | ✅ | ✅ | ❌ (per-quote override only) | ❌ |
| Reports (full org data) | ✅ | ✅ | ✅ | ❌ (own quotes/jobs only) |
| Customers / materials / orders / documents | ✅ | ✅ | ✅ (write) | ✅ (read/create) |

## Notes

- `MANAGER` may pass per-quote pricing **overrides** (`pricing:override`) but cannot change the organization's default pricing configuration (`pricing:configure`).
- `EMPLOYEE` may create quotes/jobs but cannot override pricing and sees only their own records in report endpoints.
- Only signup creates an `OWNER`. Invites cannot mint additional owners; ownership transfer is a manual, audited operation.
- Role changes and user deactivations are written to `audit_log`.
