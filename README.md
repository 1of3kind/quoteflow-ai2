# QuoteFlow AI

AI-powered instant quoting for small businesses. Customers text, email, or call with photos of work they need done — the AI analyzes the images and generates a professional quote in under 2 minutes.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy)

## Supported Trades

- **Landscaping** (lawn, sod, mulch, hardscaping)
- **Roofing** (repair, replacement, inspection)
- **Plumbing** (repairs, installs, drain cleaning)
- **Auto Body** (dent repair, paint, collision)
- **Electrical** (repairs, panel upgrades, EV charging)

## How It Works

1. **Sign up** — creates your organization, owner account, default skills/rates, and a 14-day trial
2. **Customer sends photos** via SMS, email, or voice call prompt
3. **AI analyzes images** using GPT-4 Vision (or mock for testing)
4. **The pricing engine** (`core/pricing_engine.py`) computes the quote:
   `labor + materials + overhead → target margin → recommended price`
   Every quote stores its exact inputs and can be explained/replayed via
   `GET /quotes/{id}/explain`
5. **Customer receives the quote**, approves it, and the workflow creates the
   job, materials order, and schedule automatically

## Web Frontend

The API serves the web UI directly (no separate build step):

- `/` — marketing landing page: hero, how-it-works, feature grid, and a pricing table loaded live from `/billing/plans`.
- `/app.html` — the application: signup/login (with email-verification and password-reset deep links), dashboard (stats, upcoming jobs, activity, onboarding checklist), AI assistant chat, quote creation + send/accept/explain, jobs, day schedule with crew assignment, materials (suppliers, catalog, orders), customers, org settings, and billing.

Static files are mounted **after** all API routes, so the API always takes precedence. `tests/test_frontend.py` verifies serving, API/static coexistence, and that the app JavaScript parses (via `node --check` when Node is available).

## Growth Features

- **AI assistant** — `POST /assistant/command` with plain English
  ("Create a quote for John Smith. Replace the water heater and schedule it
  for Tuesday.") executes the real workflow — customer lookup/creation,
  engine-priced quote, scheduling, sending — using the caller's own
  permissions. Deterministic parser built in; set `OPENAI_API_KEY` +
  `ASSISTANT_USE_LLM=true` for LLM-based extraction.
- **Quote explanation** — every quote ships a one-paragraph narrative
  ("Your recommended price is $1,516.56 because labor is…"); see
  `GET /quotes/{id}/explain`.
- **Material ordering** — org-scoped suppliers, SKU catalog with live
  availability, and an enforced order lifecycle
  (`draft → placed → confirmed → received`) under `/materials/*`.
- **Scheduling** — jobs declare required skills/hours
  (`POST /jobs/{id}/requirements`), the API suggests available workers for
  the job's time window (`GET /jobs/{id}/suggestions`), assignments
  conflict-check against overlapping jobs, and `GET /jobs/schedule/day`
  renders the day's crew plan.

## Multi-Tenant SaaS Architecture

Every customer is an isolated **Organization** with its own users, customers,
jobs, quotes, materials, orders, documents, settings, and subscription.
All tenant-owned queries are org-scoped (`core/database.py::get_scoped`);
cross-tenant access returns 404 and is logged as a security event.
Roles: `OWNER > ADMIN > MANAGER > EMPLOYEE` (matrix in `docs/RBAC.md`).
Authentication: JWT access tokens, rotating refresh tokens, email
verification, password reset, lockout, and rate limiting (`core/auth.py`).

Billing is webhook-authoritative: Stripe Checkout + signed webhooks activate
subscriptions (idempotent processing); failed payments trigger a grace
period, then access restriction. See `docs/RUNBOOK.md` for operations
(deployments, migrations, backups, monitoring, restore drills).

---

## 🚀 Deployment on Render

QuoteFlow AI includes a complete Infrastructure-as-Code [Render Blueprint](render.yaml) (`render.yaml`) that automatically deploys:
1. **FastAPI Web Service** (`quoteflow-api`) with automated health checks (`/health`).
2. **PostgreSQL Database** (`quoteflow-db`) for durable conversations, quotes, appointments, and billing accounts.
3. **Redis / Key-Value Instance** (`quoteflow-redis`) for Celery task queuing.
4. **Celery Background Worker** (`quoteflow-worker`) for async GPT-4 Vision image analysis & quote generation.
5. **Celery Beat Scheduler** (`quoteflow-scheduler`) for automated follow-ups.

👉 **See the complete [Render Deployment Guide (RENDER_DEPLOYMENT.md)](RENDER_DEPLOYMENT.md) for step-by-step instructions and webhook setup.**

---

## Quick Start

### Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your API keys

# Start Redis
redis-server

# Terminal 1: API
python -m api.main

# Terminal 2: Celery Worker
celery -A workers.celery_tasks.celery_app worker --loglevel=info

# Terminal 3: Scheduler (optional)
celery -A workers.celery_tasks.celery_app beat --loglevel=info
```

### Docker (Production)

```bash
docker-compose up --build
```

## Twilio Setup

1. Buy a phone number in Twilio Console
2. Set **Messaging Webhook** to `https://<YOUR-RENDER-SUBDOMAIN>.onrender.com/webhook/sms`
3. Set **Voice Webhook** to `https://<YOUR-RENDER-SUBDOMAIN>.onrender.com/voice/welcome`
4. Enable **MMS** for photo receiving

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Liveness + database readiness |
| `POST /auth/signup` | Create organization + owner (starts 14-day trial) |
| `POST /auth/login` / `refresh` / `logout` | Session lifecycle |
| `POST /auth/verify-email` / `forgot-password` / `reset-password` | Account recovery |
| `GET /org` / `PATCH /org/pricing` / `GET /org/onboarding` | Organization settings & onboarding |
| `POST /org/skills` · `/org/users` · `/org/customers` | Skills, team, customers (RBAC-enforced) |
| `POST /quotes` · `GET /quotes/{id}/explain` | Engine-backed quotes + reproducible breakdown |
| `POST /quotes/{id}/send` / `accept` / `materials-order` | Quote → document → job workflow |
| `GET /jobs` · `PATCH /jobs/{id}` | Job scheduling & completion |
| `GET /billing/plans` / `subscription` / `invoices` | Subscription status & history (OWNER) |
| `POST /billing/checkout` / `portal` / `change-plan` / `cancel` | Stripe lifecycle (OWNER) |
| `GET /dashboard/summary` | Active jobs, pending/approved quotes, revenue, profit |
| `POST /webhook/stripe` | Signature-verified, idempotent Stripe webhooks |
| `POST /webhook/sms` / `email`, `POST /voice/*` | Inbound channels (signature-verified) |

## Testing

```bash
python -m pytest tests/ -v
```

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `OPENAI_API_KEY` | GPT-4 Vision image analysis |
| `TWILIO_*` | SMS and voice calls |
| `SENDGRID_API_KEY` | Email sending |
| `REDIS_URL` | Celery task queue & Key-Value broker |
| `DATABASE_URL` | PostgreSQL connection string |
| `FEEDBACK_API_KEY` | API & Admin authentication |
| `EMAIL_WEBHOOK_SECRET` | Inbound email webhook authentication |

## Architecture

```
Customer (SMS/Email/Voice)
    ↓
Twilio / SendGrid Webhooks
    ↓
FastAPI Server (api/main.py)
    ↓
Celery Worker (workers/celery_tasks.py)
    ↓
OpenAI Vision → Image Analysis
    ↓
Pricing Formula → Quote Generation
    ↓
Notification Service → Customer receives quote
```

## License

MIT
