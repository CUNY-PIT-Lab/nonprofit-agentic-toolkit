"""Environment configuration with secure production defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    environment: str
    database_url: str
    public_app_url: str
    allowed_origin: str
    ollama_api_key: str
    toolkit_model: str
    model_backend: str
    resend_api_key: str
    email_from: str
    email_backend: str
    auth_pepper: str
    cookie_secure: bool
    session_days: int
    verification_hours: int
    reset_minutes: int
    test_mode: bool

    @property
    def production(self) -> bool:
        return self.environment == "production"

    @property
    def email_ready(self) -> bool:
        return bool(
            self.email_backend == "resend"
            and self.resend_api_key
            and self.email_from
            and self.public_app_url
        )

    @classmethod
    def from_env(cls) -> "Settings":
        railway_env = os.environ.get("RAILWAY_ENVIRONMENT_NAME", "").strip().lower()
        environment = os.environ.get(
            "APP_ENV",
            os.environ.get("ENVIRONMENT", railway_env or "development"),
        ).strip().lower()
        production = environment == "production"
        database_url = os.environ.get("DATABASE_URL", "").strip()
        if not database_url:
            if production:
                raise RuntimeError("DATABASE_URL is required in production")
            database_url = os.environ.get(
                "TOOLKIT_SQLITE_URL", "sqlite:///./toolkit-local.db"
            ).strip()
        if database_url.startswith("postgres://"):
            database_url = "postgresql+psycopg://" + database_url[len("postgres://") :]
        elif database_url.startswith("postgresql://"):
            database_url = "postgresql+psycopg://" + database_url[len("postgresql://") :]

        public_url = os.environ.get(
            "PUBLIC_APP_URL", "http://127.0.0.1:8765"
        ).strip().rstrip("/")
        parts = urlsplit(public_url)
        if not parts.scheme or not parts.netloc:
            raise RuntimeError("PUBLIC_APP_URL must be an absolute URL")
        if production and parts.scheme != "https":
            raise RuntimeError("PUBLIC_APP_URL must use HTTPS in production")
        origin = f"{parts.scheme}://{parts.netloc}"
        auth_pepper = os.environ.get(
            "AUTH_PEPPER", os.environ.get("SESSION_SECRET", "")
        ).strip()
        if production and len(auth_pepper) < 32:
            raise RuntimeError("AUTH_PEPPER or SESSION_SECRET is required in production")
        model_backend = os.environ.get("MODEL_BACKEND", "ollama").strip().lower()
        if model_backend not in {"ollama", "stub"}:
            raise RuntimeError("MODEL_BACKEND must be ollama or stub")
        if production and model_backend != "ollama":
            raise RuntimeError("MODEL_BACKEND=stub is unavailable in production")
        return cls(
            environment=environment,
            database_url=database_url,
            public_app_url=public_url,
            allowed_origin=origin,
            ollama_api_key=os.environ.get("OLLAMA_API_KEY", "").strip(),
            toolkit_model=os.environ.get("TOOLKIT_MODEL", "glm-5.2").strip(),
            model_backend=model_backend,
            resend_api_key=os.environ.get("RESEND_API_KEY", "").strip(),
            email_from=os.environ.get(
                "MAIL_FROM", os.environ.get("EMAIL_FROM", "")
            ).strip(),
            email_backend=os.environ.get(
                "EMAIL_BACKEND", "resend" if production else "memory"
            ).strip().lower(),
            auth_pepper=auth_pepper,
            cookie_secure=production or _truthy(os.environ.get("COOKIE_SECURE")),
            session_days=max(1, int(os.environ.get("SESSION_DAYS", "14"))),
            verification_hours=max(1, int(os.environ.get("VERIFICATION_HOURS", "24"))),
            reset_minutes=max(5, int(os.environ.get("RESET_MINUTES", "30"))),
            test_mode=_truthy(os.environ.get("TOOLKIT_TEST_MODE")),
        )
