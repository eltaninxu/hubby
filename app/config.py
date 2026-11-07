"""Environment loading and validation.

The app fails fast with a message that names the missing variable, because the
first thing a judge hits is usually a half-filled .env.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

from app.ratelimit import parse_limit

ROOT = Path(__file__).resolve().parent.parent

# .env next to this repo; real values never live in the repo itself.
load_dotenv(ROOT / ".env")

PAYPAL_SANDBOX_API = "https://api-m.sandbox.paypal.com"


class ConfigError(RuntimeError):
    """Raised when the environment is incomplete or unsupported."""


def _env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return default


@dataclass(frozen=True)
class Settings:
    paypal_env: str
    paypal_client_id: str
    paypal_client_secret: str
    paypal_return_base: str
    openai_api_key: str
    openai_base_url: str | None
    llm_model: str
    recipient_email: str
    payout_poll_attempts: int
    payout_poll_interval: float
    db_path: Path
    llm_stub: bool
    demo_reset: bool
    demo_reset_after_hours: float
    maintenance_seconds: float
    rate_api: tuple[int, int]
    rate_fund: tuple[int, int]
    rate_settings: tuple[int, int]
    rate_global: tuple[int, int]

    @property
    def paypal_api(self) -> str:
        if self.paypal_env != "sandbox":
            raise ConfigError(
                f"PAYPAL_ENV={self.paypal_env!r} is not supported; this build is sandbox-only."
            )
        return PAYPAL_SANDBOX_API

    @property
    def return_url(self) -> str:
        return f"{self.paypal_return_base}/paypal/return"

    @property
    def cancel_url(self) -> str:
        return f"{self.paypal_return_base}/paypal/cancel"


def load_settings(*, require_llm: bool = True) -> Settings:
    paypal_env = _env("PAYPAL_ENV", default="sandbox").lower()
    if paypal_env != "sandbox":
        raise ConfigError(
            f"PAYPAL_ENV={paypal_env!r} is not supported; set PAYPAL_ENV=sandbox."
        )

    client_id = _env("PAYPAL_CLIENT_ID", "PAYPAL_SANDBOX_CLIENT_ID")
    client_secret = _env("PAYPAL_CLIENT_SECRET", "PAYPAL_SANDBOX_SECRET")
    missing = [
        name
        for name, value in (
            ("PAYPAL_CLIENT_ID", client_id),
            ("PAYPAL_CLIENT_SECRET", client_secret),
        )
        if not value
    ]
    if missing:
        raise ConfigError(
            "Missing required env var(s): "
            + ", ".join(missing)
            + ". Copy .env.example to .env and fill them in."
        )

    api_key = _env("OPENAI_API_KEY")
    if require_llm and not api_key:
        raise ConfigError(
            "Missing required env var: OPENAI_API_KEY. "
            "Copy .env.example to .env and fill it in."
        )
    llm_model = _env("LLM_MODEL")
    if require_llm and not llm_model:
        raise ConfigError(
            "Missing required env var: LLM_MODEL. Set it to a chat model your "
            "endpoint actually serves and that supports tool calling."
        )

    recipient_email = _env("HUBBY_RECIPIENT_EMAIL", "PAYPAL_SANDBOX_PERSONAL_EMAIL")
    if require_llm and not recipient_email:
        raise ConfigError(
            "Missing required env var: HUBBY_RECIPIENT_EMAIL. "
            "It is the PayPal address approved money is released to."
        )

    db_path = Path(_env("HUBBY_DB", default=str(ROOT / "data" / "hubby.db")))

    def number(name: str, default: str) -> float:
        try:
            return float(_env(name, default=default))
        except ValueError:
            return float(default)

    return Settings(
        paypal_env=paypal_env,
        paypal_client_id=client_id,
        paypal_client_secret=client_secret,
        paypal_return_base=_env(
            "PAYPAL_RETURN_BASE", default="http://localhost:8080"
        ).rstrip("/"),
        openai_api_key=api_key,
        openai_base_url=_env("OPENAI_BASE_URL") or None,
        llm_model=llm_model,
        recipient_email=recipient_email,
        payout_poll_attempts=max(1, int(number("PAYOUT_POLL_ATTEMPTS", "30"))),
        payout_poll_interval=max(0.0, number("PAYOUT_POLL_INTERVAL", "1.5")),
        db_path=db_path,
        llm_stub=_env("LLM_STUB", default="").lower() in {"1", "true", "yes"},
        demo_reset=_env("HUBBY_DEMO_RESET", default="0").lower() in {"1", "true", "yes"},
        demo_reset_after_hours=number("HUBBY_DEMO_RESET_AFTER_HOURS", "12"),
        maintenance_seconds=max(30.0, number("HUBBY_MAINTENANCE_SECONDS", "1800")),
        rate_api=parse_limit(_env("RATE_LIMIT_API", default="40/600"), default_limit=40, default_window=600),
        rate_fund=parse_limit(_env("RATE_LIMIT_FUND", default="10/600"), default_limit=10, default_window=600),
        rate_settings=parse_limit(_env("RATE_LIMIT_SETTINGS", default="10/3600"), default_limit=10, default_window=3600),
        rate_global=parse_limit(_env("RATE_LIMIT_GLOBAL", default="600/3600"), default_limit=600, default_window=3600),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
