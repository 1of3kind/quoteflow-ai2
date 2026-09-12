"""Observability: structured request logging, request IDs, slow-request
detection, security-event logging, and sanitized error handling (GATE 8).

Logs are JSON so they can ship to any aggregator. Secrets and request
bodies/tokens are never logged (OWASP: log auth/authorization failures and
system errors, but scrub sensitive data).
"""

import json
import logging
import os
import sys
import time
import uuid

from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("quoteflow")

SENSITIVE_HEADERS = {"authorization", "cookie", "x-twilio-signature",
                     "stripe-signature", "x-quoteflow-webhook-secret"}
SENSITIVE_PATH_MARKERS = ("token", "password", "secret", "webhook")


def configure_logging() -> None:
    """JSON lines to stdout, level from LOG_LEVEL."""
    level = os.getenv("LOG_LEVEL", "INFO").upper()

    class JsonFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            payload = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
            if record.exc_info and record.exc_info[0] is not None:
                payload["exception"] = self.formatException(record.exc_info)[-2000:]
            return json.dumps(payload, default=str)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, level, logging.INFO))
    for noisy in ("uvicorn.access",):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _is_sensitive(path: str) -> bool:
    return any(marker in path.lower() for marker in SENSITIVE_PATH_MARKERS)


async def observability_middleware(request: Request, call_next):
    """Request ID, structured access log, slow-request warning."""
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    request.state.request_id = request_id
    start = time.perf_counter()

    response = await call_next(request)

    elapsed_ms = (time.perf_counter() - start) * 1000
    response.headers["X-Request-ID"] = request_id
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")

    log = logger.info
    if elapsed_ms > float(os.getenv("SLOW_REQUEST_MS", "2000")):
        log = logger.warning
    if request.url.path not in ("/health",):  # keep health checks out of logs
        log("request method=%s path=%s status=%s duration_ms=%.1f request_id=%s",
            request.method, request.url.path, response.status_code,
            elapsed_ms, request_id)
    return response


def security_event(event: str, **fields) -> None:
    """Authentication / authorization / validation failures (OWASP A09).
    Values are redacted if a key looks secret-shaped."""
    safe = {}
    for key, value in fields.items():
        safe[key] = "[REDACTED]" if any(w in key.lower() for w in
                                        ("password", "token", "secret", "key")) else value
    logger.warning("security event=%s %s", event,
                   " ".join(f"{k}={v}" for k, v in safe.items()))


def install_error_handlers(app) -> None:
    """Sanitized error responses: internal details never leak in production."""
    from fastapi import HTTPException
    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        if isinstance(exc.detail, dict):
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        return JSONResponse(status_code=exc.status_code,
                            content={"error": str(exc.detail)})

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        logger.warning("validation failed path=%s request_id=%s",
                       request.url.path, getattr(request.state, "request_id", "-"))
        return JSONResponse(status_code=422, content={"error": "Invalid request data"})

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception):
        request_id = getattr(request.state, "request_id", "-")
        logger.exception("unhandled error path=%s request_id=%s",
                         request.url.path, request_id)
        if os.getenv("APP_ENV", "development") == "production":
            return JSONResponse(status_code=500, content={
                "error": "Internal server error", "request_id": request_id})
        return JSONResponse(status_code=500, content={
            "error": f"{type(exc).__name__}: {exc}", "request_id": request_id})
