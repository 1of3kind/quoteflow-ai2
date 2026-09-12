# E-ZFlow — Production Runbook

Operational procedures for running E-ZFlow as a SaaS. Maps to launch gates 1–10.

## Environments

| Environment | URL | Branch | Purpose |
|---|---|---|---|
| local | http://localhost:8000 | any | development, mock Stripe/OpenAI |
| staging | `https://staging.<your-domain>` (Render service `ezflow-staging`) | `main` via CI | pre-release verification |
| production | `https://app.<your-domain>` (Render service `ezflow-prod`) | manual promote from staging | customers |

CI (`.github/workflows/ci.yml`) runs tests + a committed-secret scan on every push, then deploys `main` to staging through a Render deploy hook (`RENDER_STAGING_DEPLOY_HOOK` secret). Promotion to production is a manual job: merge `main` → `release`, or click "Manual Deploy" on the production service after staging verification. HTTPS and the custom domain are terminated by Render (managed certificates).

## Deploy checklist

1. All CI checks green (tests, secret scan).
2. Verified on staging: signup → onboarding → quote → accept → job complete.
3. Database migration applied (see below). Alembic runs before app start in `render.yaml` build/start commands if configured, else run manually.
4. Environment variables present (see `.env.example`): `JWT_SECRET` set to a strong random value, `APP_ENV=production`, real `STRIPE_*`, `DATABASE_URL` with `sslmode=require`.

## Migrations

```bash
DATABASE_URL=... alembic upgrade head        # apply
DATABASE_URL=... alembic downgrade -1        # roll back one revision
```

Never edit an applied migration; add a new revision with
`alembic revision --autogenerate -m "..."` and review the diff before applying.

## Health & monitoring

- `GET /health` — liveness + DB readiness probe (use as Render health check path).
- JSON logs on stdout (request id, status, duration) — ship to Render log streams.
- `core.observability` logs authentication failures, authorization failures (cross-tenant access attempts → 404 + security log), validation failures, and unhandled errors. Logs never contain secrets or request bodies.
- Alerts (`core.alerting` → `ALERT_WEBHOOK_URL`, Slack-compatible): unhandled errors, database unreachable, Stripe webhook signature failures, auth-failure spikes (≥20 in 5 min), AI API failures, failed background jobs.

## Backups (Gate 8)

Automated (Render cron job or system cron, daily 02:00 UTC):

```bash
BACKUP_DIR=/backups BACKUP_RETENTION_DAYS=30 ./scripts/backup_db.sh
```

- Retention: 30 days (override with `BACKUP_RETENTION_DAYS`).
- **Restore drill (run quarterly, and after any provider change):**
  1. Provision a scratch database.
  2. `DATABASE_URL=<scratch> ./scripts/restore_db.sh backups/ezflow_<ts>.dump`
  3. Confirm the verification query returns non-zero row counts for `organizations`, `users`, `quotes`, `jobs`.
  4. Record the drill date in the ops log. A restore that has never been rehearsed does not count.

Restore into production requires `RESTORE_TO_PRODUCTION=yes` (explicit footgun guard).

## Secret management (Gate 6)

- No secrets in the repo or git history — `scripts/check_no_secrets.py` enforces this in CI across the working tree **and every commit**.
- Rotate immediately if a secret ever reaches git: rotate at the provider first, then purge.
- Production secrets live in Render environment groups / GitHub environment secrets.

## Auth & RBAC (Gates 1–2)

- JWT access tokens (15 min) + single-use rotating refresh tokens (14 days).
- Roles: `OWNER` > `ADMIN` > `MANAGER` > `EMPLOYEE` — matrix in `core/rbac.py` and `docs/RBAC.md`.
- Lockout: 5 failed logins → 15-minute lock. Rate limits: login 10/5min, signup 5/h, reset 5/15min per IP.
- Every tenant-owned query goes through `get_scoped()`/`scoped_or_404()`; cross-tenant access returns 404 and writes a security log entry.

## Pricing engine (Gates 3–4)

- The only quote calculator is `core/pricing_engine.py::compute` — pure, Decimal-based, validated.
- Every quote persists `engine_input` + `engine_result`; `GET /quotes/{id}/explain` replays the snapshot and verifies it matches what was stored.
- Change the formula → `tests/test_pricing_engine.py` regression matrix must stay green; bump `ENGINE_VERSION`.

## Stripe billing (Gate 5)

- Activation/state changes only via signed Stripe webhooks (`/webhook/stripe`), processed exactly once per event id (idempotency ledger `webhook_events`).
- Failed payment → `past_due` + 3-day grace (configurable `BILLING_GRACE_DAYS`) → access restricted.
- Configure webhook endpoint in Stripe dashboard: `https://<domain>/webhook/stripe`, events: `checkout.session.completed`, `customer.subscription.*`, `invoice.payment_failed`, `invoice.paid`, `invoice.payment_succeeded`.

## Onboarding (Gate 9)

Signup creates the organization, OWNER user, default skills/rates/settings, and a 14-day trial automatically. Track completion at `GET /org/onboarding`. No manual database configuration per customer.

## Incident playbook

1. Check `/health` and recent JSON logs (grep `request_id`).
2. Database issues → restore per above (staging copy first if data uncertainty).
3. Stripe webhook backlog → replay events from Stripe dashboard (idempotent processing makes replay safe).
4. Security incident → rotate `JWT_SECRET` (invalidates all sessions), lock affected orgs, audit via `audit_log` table.
