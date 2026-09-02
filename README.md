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

1. **Customer sends photos** via SMS, email, or voice call prompt
2. **AI analyzes images** using GPT-4 Vision (or mock for testing)
3. **Quote engine applies** trade-specific pricing formulas
4. **Customer receives quote** via their original channel (SMS/email)
5. **Follow-up automation** nudges pending quotes after 48 hours

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
| `GET /health` | Service health & active conversation count |
| `POST /webhook/sms` | Receive SMS + photos from customers |
| `POST /webhook/email` | Receive emails with attachments |
| `POST /webhook/stripe` | Stripe payment webhook |
| `POST /voice/welcome` | Twilio voice IVR |
| `POST /quote/start` | Manually initiate quote |
| `POST /quote/accept` | Mark quote as booked |
| `POST /materials/order` | Generate store materials order |
| `POST /appointments/schedule` | Schedule appointment with pickup |
| `GET /admin/conversations` | View all conversations |
| `GET /admin/analytics` | Business metrics |
| `GET /dashboard/metrics` | Retool dashboard KPI metrics |

## Testing

```bash
python -m pytest tests/test_all.py -v
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
