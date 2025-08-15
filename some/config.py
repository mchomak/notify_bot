CSV_FIELDS = [
  "user_id","created_at","send_at","alert_id",
  "title","kind","times","days_of_week",
  "window_start","window_end","interval_minutes","cron_expr",
  "src_chat_id","src_message_id","content_type"
]

INTERVAL_JSON_SPEC = {
    "kind": "one_time|daily|weekly|window_interval|cron",
    "times": "list of 'HH:MM' strings in 24h (for daily/weekly)",
    "days_of_week": "list of ['mon','tue','wed','thu','fri','sat','sun'] (weekly only)",
    "window": {"start": "HH:MM", "end": "HH:MM"},
    "interval_minutes": "integer minutes for window_interval",
    "cron_expr": "string crontab expression (optional)",
    "timezone": "IANA tz, optional (ignore if absent)",
    "name": "optional short title (<=100 chars)"
}


# config.py
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, Dict, Any

from dotenv import load_dotenv as _load_dotenv


class AppEnv(Enum):
    DEV = "dev"
    STAGE = "stage"
    PROD = "prod"


@dataclass(frozen=True)
class Settings:
    """Immutable application settings resolved from ENV."""
    app_name: str
    app_env: AppEnv
    debug: bool
    log_level: str
    telegram_bot_token: str
    database_url: str

    telegram_alerts_chat_id: Optional[str] = None
    redis_url: Optional[str] = None
    webhook_url: Optional[str] = None
    sentry_dsn: Optional[str] = None
    deepseek_key: Optional[str] = None
    api_url: Optional[str] = None


def _to_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_env(env_file: str = ".env") -> Settings:
    """
    Load and validate settings from environment (optionally via .env).
    Raises ValueError if required fields are missing or invalid.
    """
    if _load_dotenv:
        _load_dotenv(dotenv_path=env_file, override=False)

    app_name = os.getenv("APP_NAME", "mybot").strip()
    app_env_str = os.getenv("APP_ENV", "dev").strip().lower()
    log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    debug = _to_bool(os.getenv("DEBUG"), default=(app_env_str != "prod"))

    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    telegram_alerts_chat_id = (os.getenv("TELEGRAM_ALERTS_CHAT_ID") or "").strip() or None

    database_url = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./data/app.db").strip()
    redis_url = os.getenv("REDIS_URL")
    webhook_url = os.getenv("WEBHOOK_URL")
    sentry_dsn = os.getenv("SENTRY_DSN")

    try:
        app_env = AppEnv(app_env_str)
    except ValueError:
        allowed = [e.value for e in AppEnv]
        raise ValueError(f"APP_ENV must be one of {allowed} (got: {app_env_str!r}).")

    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is required but not set.")

    valid_levels = {"TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    if log_level not in valid_levels:
        raise ValueError(f"LOG_LEVEL must be one of {sorted(valid_levels)} (got: {log_level}).")

    if webhook_url and not webhook_url.startswith("https://"):
        raise ValueError("WEBHOOK_URL must start with https:// (Telegram requires HTTPS).")

    if database_url.startswith("sqlite"):
        try:
            path_part = database_url.split("///", 1)[1]
            Path(path_part).parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    return Settings(
        app_name=app_name,
        app_env=app_env,
        debug=debug,
        log_level=log_level,
        telegram_bot_token=token,
        telegram_alerts_chat_id=telegram_alerts_chat_id,
        database_url=database_url,
        redis_url=redis_url,
        webhook_url=webhook_url,
        sentry_dsn=sentry_dsn,
    )


def get_runtime_env(settings: Optional[Settings] = None) -> Dict[str, Any]:
    """Normalized runtime snapshot to inject into logs/metrics."""
    settings = settings or load_env()
    env = settings.app_env
    return {
        "env": env.value,
        "is_dev": env is AppEnv.DEV,
        "is_stage": env is AppEnv.STAGE,
        "is_prod": env is AppEnv.PROD,
        "debug": settings.debug,
        "log_level": settings.log_level,
        "app_name": settings.app_name,
    }
