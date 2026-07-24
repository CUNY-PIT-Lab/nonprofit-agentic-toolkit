"""Password, token, cookie, CSRF, and request-throttling helpers."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError


SESSION_COOKIE = "toolkit_session"
CSRF_COOKIE = "toolkit_csrf"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
password_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
)


def normalize_email(value: str) -> str:
    email = (value or "").strip().casefold()
    if len(email) > 254 or not EMAIL_RE.fullmatch(email):
        raise ValueError("Enter a valid email address")
    return email


def validate_password(value: str) -> str:
    password = value or ""
    if len(password) < 12:
        raise ValueError("Use at least 12 characters")
    if len(password) > 1024:
        raise ValueError("Password is too long")
    return password


def hash_password(password: str) -> str:
    return password_hasher.hash(validate_password(password))


def verify_password(stored: str, supplied: str) -> bool:
    try:
        return password_hasher.verify(stored, supplied)
    except (VerifyMismatchError, InvalidHashError):
        return False


def needs_password_rehash(stored: str) -> bool:
    try:
        return password_hasher.check_needs_rehash(stored)
    except InvalidHashError:
        return True


def opaque_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    material = (token or "").encode("utf-8")
    pepper = os.environ.get(
        "AUTH_PEPPER", os.environ.get("SESSION_SECRET", "")
    ).encode("utf-8")
    if pepper:
        return hmac.new(pepper, material, hashlib.sha256).hexdigest()
    return hashlib.sha256(material).hexdigest()


def user_agent_hash(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value[:512].encode("utf-8")).hexdigest()


def is_expired(value: datetime) -> bool:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value <= datetime.now(timezone.utc)


def constant_equal(left: str | None, right: str | None) -> bool:
    return bool(left and right and hmac.compare_digest(left, right))


class RateLimiter:
    """Small per-process limiter for abuse-prone authentication routes."""

    def __init__(self):
        self._events: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, bucket: str, key: str, limit: int, window_seconds: int) -> bool:
        now = time.monotonic()
        cutoff = now - window_seconds
        compound = (bucket, key)
        with self._lock:
            events = self._events[compound]
            while events and events[0] < cutoff:
                events.popleft()
            if len(events) >= limit:
                return False
            events.append(now)
            return True
