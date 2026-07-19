# QuoteFlow AI

AI-powered instant quoting for small businesses. Customers text, email, or call with photos of work they need done — the AI analyzes the images and generates a professional quote in under 2 minutes.

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

## Quick Start

### Local Development

```bash
# Install
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env with your API keys

# Start Redis
redis-server

# Terminal 1: API
python -m api.main

# Terminal 2: Worker
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
2. Set **Messaging Webhook** to `https://yourdomain.com/webhook/sms`
3. Set **Voice Webhook** to `https://yourdomain.com/voice/welcome`
4. Enable **MMS** for photo receiving

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `POST /webhook/sms` | Receive SMS + photos from customers |
| `POST /webhook/email` | Receive emails with attachments |
| `POST /voice/welcome` | Twilio voice IVR |
| `POST /quote/start` | Manually initiate quote |
| `POST /quote/accept` | Mark quote as booked |
| `GET /admin/conversations` | View all conversations |
| `GET /admin/analytics` | Business metrics |

## Testing

```bash
pytest tests/test_all.py -v
```

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `OPENAI_API_KEY` | GPT-4 Vision image analysis |
| `TWILIO_*` | SMS and voice calls |
| `SENDGRID_API_KEY` | Email sending |
| `REDIS_URL` | Celery task queue |
| `FEEDBACK_API_KEY` | API authentication |

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
