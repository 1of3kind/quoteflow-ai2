"""Production authentication primitives (GATE 2).

- Password hashing: scrypt (N=2^15, r=8, p=1) from the standard library,
  per-user random salt, constant-time verification.
- Sessions: short-lived JWT access tokens + revocable refresh tokens
  (refresh token values are stored only as SHA-256 digests).
- One-time tokens: email verification and password reset, single-use,
  time-limited, stored as digests.
- Abuse protection: per-account failed-login lockout + per-IP rate limiting.

No secrets are ever logged.
"""

import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

from jose import jwt, JWTError, ExpiredSignatureError

logger = logging.getLogger("auth")

# ─── Configuration (env-driven, safe defaults) ─────────────────────────────

JWT_SECRET = os.getenv("JWT_SECRET", "")
JWT_ISSUER = "ezflow"
ACCESS_TOKEN_TTL_MINUTES = int(os.getenv("ACCESS_TOKEN_TTL_MINUTES", "15"))
REFRESH_TOKEN_TTL_DAYS = int(os.getenv("REFRESH_TOKEN_TTL_DAYS", "14"))
EMAIL_VERIFY_TTL_HOURS = int(os.getenv("EMAIL_VERIFY_TTL_HOURS", "48"))
PASSWORD_RESET_TTL_MINUTES = int(os.getenv("PASSWORD_RESET_TTL_MINUTES", "60"))
MAX_FAILED_LOGINS = int(os.getenv("MAX_FAILED_LOGINS", "5"))
LOCKOUT_MINUTES = int(os.getenv("LOCKOUT_MINUTES", "15"))
REQUIRE_VERIFIED_EMAIL = os.getenv("REQUIRE_VERIFIED_EMAIL", "true").lower() == "true"


def jwt_secret() -> str:
    """Return the signing secret, failing closed in production."""
    secret = JWT_SECRET or os.getenv("JWT_SECRET", "")
    if not secret or secret.lower() in {"replace-me", "change-me", "dev-secret"}:
        if os.getenv("APP_ENV", "development") == "production":
            raise RuntimeError("JWT_SECRET must be set to a strong random value in production")
        # Deterministic dev fallback so local servers restart cleanly.
        return "insecure-dev-only-secret"
    return secret


# ─── Password hashing (scrypt) ──────────────────────────────────────────────

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 15, 8, 1


def hash_password(password: str) -> str:
    if not password or len(password) > 1024:
        raise ValueError("Invalid password")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32,
        maxmem=64 * 1024 * 1024,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p), dklen=len(expected),
            maxmem=64 * 1024 * 1024,
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


PASSWORD_MIN_LENGTH = 10


def validate_password_strength(password: str) -> Optional[str]:
    """Return an error message if the password is weak, else None."""
    if len(password) < PASSWORD_MIN_LENGTH:
        return f"Password must be at least {PASSWORD_MIN_LENGTH} characters"
    if password.lower() in {"password123!", "changeme123"}:
        return "Password is too common"
    if not any(c.isdigit() for c in password) or not any(c.isalpha() for c in password):
        return "Password must contain both letters and numbers"
    return None


# ─── Access tokens (JWT) ────────────────────────────────────────────────────

def create_access_token(user_id: str, organization_id: str, role: str) -> str:
    now = datetime.utcnow()
    payload = {
        "sub": user_id,
        "org": organization_id,
        "role": role,
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=ACCESS_TOKEN_TTL_MINUTES)).timestamp()),
        "iss": JWT_ISSUER,
        "jti": secrets.token_hex(16),
    }
    return jwt.encode(payload, jwt_secret(), algorithm="HS256")


class TokenPayload:
    __slots__ = ("user_id", "organization_id", "role", "token_type", "jti")

    def __init__(self, user_id: str, organization_id: str, role: str, token_type: str, jti: str):
        self.user_id = user_id
        self.organization_id = organization_id
        self.role = role
        self.token_type = token_type
        self.jti = jti


def decode_access_token(token: str) -> TokenPayload:
    """Decode + validate an access token. Raises JWTError subclasses on
    invalid/expired tokens or wrong type."""
    claims = jwt.decode(
        token, jwt_secret(), algorithms=["HS256"],
        issuer=JWT_ISSUER, options={"require": ["exp", "iat", "sub", "type"]},
    )
    if claims.get("type") != "access":
        raise JWTError("Wrong token type")
    return TokenPayload(
        user_id=claims["sub"],
        organization_id=claims["org"],
        role=claims["role"],
        token_type="access",
        jti=claims.get("jti", ""),
    )


# ─── One-time / refresh tokens (opaque, stored hashed) ─────────────────────

def generate_token_value() -> str:
    return secrets.token_urlsafe(32)


def token_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def expiry_for(token_type: str) -> datetime:
    if token_type == "refresh":
        return datetime.utcnow() + timedelta(days=REFRESH_TOKEN_TTL_DAYS)
    if token_type == "email_verify":
        return datetime.utcnow() + timedelta(hours=EMAIL_VERIFY_TTL_HOURS)
    if token_type == "password_reset":
        return datetime.utcnow() + timedelta(minutes=PASSWORD_RESET_TTL_MINUTES)
    raise ValueError(f"Unknown token type {token_type}")


# ─── Lockout helpers ────────────────────────────────────────────────────────

def is_locked_out(user) -> bool:
    return bool(user.locked_until and user.locked_until > datetime.utcnow())


def register_failed_login(user) -> None:
    user.failed_login_count = (user.failed_login_count or 0) + 1
    if user.failed_login_count >= MAX_FAILED_LOGINS:
        user.locked_until = datetime.utcnow() + timedelta(minutes=LOCKOUT_MINUTES)
        user.failed_login_count = 0
        logger.warning("security event=account_lockout user=%s", user.id)


def register_successful_login(user) -> None:
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = datetime.utcnow()


# ─── Rate limiting (per-process sliding window) ────────────────────────────
# Suitable for a single API instance; for multi-instance deployments point
# this at Redis (see docs/RUNBOOK.md). Keyed by client IP + bucket name.

class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[tuple[str, str], list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, bucket: str = "default") -> bool:
        now = time.monotonic()
        with self._lock:
            k = (key, bucket)
            window = [t for t in self._hits.get(k, []) if now - t < self.window_seconds]
            if len(window) >= self.max_requests:
                self._hits[k] = window
                return False
            window.append(now)
            self._hits[k] = window
            # Opportunistic cleanup to bound memory
            if len(self._hits) > 10_000:
                self._hits = {
                    kk: vv for kk, vv in self._hits.items()
                    if any(now - t < self.window_seconds for t in vv)
                }
            return True


# login: 10 attempts / 5 min per IP; stricter buckets for reset endpoints
login_rate_limiter = RateLimiter(int(os.getenv("LOGIN_RATE_LIMIT", "10")), 300)
password_reset_rate_limiter = RateLimiter(int(os.getenv("RESET_RATE_LIMIT", "5")), 900)
signup_rate_limiter = RateLimiter(int(os.getenv("SIGNUP_RATE_LIMIT", "5")), 3600)


def client_ip(request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
