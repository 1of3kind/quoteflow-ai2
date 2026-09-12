"""Alerting (GATE 8): fire-and-forget webhook alerts for serious failures.

Covers the required alert classes: application errors, database failures,
Stripe webhook failures, authentication attack patterns, AI/API failures,
and slow requests / failed jobs. Alerts are POSTed to ALERT_WEBHOOK_URL
(Slack-compatible payload). Alerts must never include secrets or customer
data — only event names, identifiers, and counts.
"""

import json
import logging
import os
import threading
import time
import urllib.request
from collections import Counter, deque

logger = logging.getLogger("alerts")

ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "").strip()
_THRESHOLD_WINDOW_SECONDS = 300
_thresholds: Counter = Counter()
_window_start = time.monotonic()


def send_alert(event: str, severity: str = "critical", **fields) -> None:
    """Queue an alert. Non-blocking; delivery failures are logged, never raised."""
    payload = {"event": event, "severity": severity,
               "service": "ezflow", **fields}
    logger.error("alert %s %s", event,
                 " ".join(f"{k}={v}" for k, v in fields.items()))
    if not ALERT_WEBHOOK_URL:
        return
    body = json.dumps({
        "text": f"🚨 E-ZFlow [{severity}] {event}: "
                + ", ".join(f"{k}={v}" for k, v in fields.items()),
        "ezflow_alert": payload,
    }).encode()
    threading.Thread(
        target=_post, args=(ALERT_WEBHOOK_URL, body), daemon=True).start()


def _post(url: str, body: bytes) -> None:
    try:
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        logger.warning("alert delivery failed (will not retry)")


def record_auth_failure(ip: str | None = None) -> None:
    """Alert on sustained authentication attack patterns (not single failures)."""
    _tick("auth_failure")
    if _threshold("auth_failure") >= 20:
        send_alert("auth_failures_spike", severity="high",
                   count=_threshold("auth_failure"), window_seconds=_THRESHOLD_WINDOW_SECONDS)


def record_webhook_failure(source: str) -> None:
    send_alert("webhook_signature_failures", severity="critical", source=source)


def record_ai_failure(provider: str = "openai") -> None:
    send_alert("ai_api_failure", severity="critical", provider=provider)


def record_database_failure() -> None:
    send_alert("database_unreachable", severity="critical")


def record_failed_job(task: str) -> None:
    send_alert("background_job_failed", severity="high", task=task)


def _tick(bucket: str) -> None:
    global _window_start
    now = time.monotonic()
    if now - _window_start > _THRESHOLD_WINDOW_SECONDS:
        _thresholds.clear()
        _window_start = now
    _thresholds[bucket] += 1


def _threshold(bucket: str) -> int:
    return _thresholds[bucket]
