"""Application configuration loaded from environment variables."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Telegram
    telegram_bot_token: str = Field(..., alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(..., alias="TELEGRAM_CHAT_ID")
    telegram_admin_ids: str = Field(default="", alias="TELEGRAM_ADMIN_IDS")

    # Telegram webhook mode. If a public URL is available we run as a webhook
    # (one HTTP consumer, no `getUpdates` Conflict). If no URL is set we fall
    # back to long-polling, which is convenient for local development.
    telegram_webhook_url: Optional[str] = Field(default=None, alias="TELEGRAM_WEBHOOK_URL")
    telegram_webhook_secret: Optional[str] = Field(default=None, alias="TELEGRAM_WEBHOOK_SECRET")
    telegram_webhook_path: str = Field(default="/telegram/webhook", alias="TELEGRAM_WEBHOOK_PATH")
    # Render injects this with the public service URL — we use it as a fallback.
    render_external_url: Optional[str] = Field(default=None, alias="RENDER_EXTERNAL_URL")

    # Database
    database_url: str = Field(..., alias="DATABASE_URL")

    # Instagram request shape is fixed in app.monitor.instagram.

    # Scheduler
    check_interval: int = Field(default=1800, alias="CHECK_INTERVAL")
    jitter_seconds: int = Field(default=120, alias="JITTER_SECONDS")
    request_timeout: int = Field(default=20, alias="REQUEST_TIMEOUT")
    max_concurrent_fetches: int = Field(default=3, alias="MAX_CONCURRENT_FETCHES")

    # Storage
    media_dir: str = Field(default="./data/media", alias="MEDIA_DIR")

    # Logging
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # Optional proxy (single URL applied to both http and https)
    proxy_url: Optional[str] = Field(default=None, alias="PROXY_URL")
    http_proxy: Optional[str] = Field(default=None, alias="HTTP_PROXY")
    https_proxy: Optional[str] = Field(default=None, alias="HTTPS_PROXY")

    # Optional Web API auth
    web_api_token: Optional[str] = Field(default=None, alias="WEB_API_TOKEN")

    # Render injects PORT
    port: int = Field(default=8000, alias="PORT")

    @field_validator("database_url")
    @classmethod
    def normalize_database_url(cls, v: str) -> str:
        if v.startswith("postgres://"):
            v = v.replace("postgres://", "postgresql+asyncpg://", 1)
        elif v.startswith("postgresql://") and "+asyncpg" not in v:
            v = v.replace("postgresql://", "postgresql+asyncpg://", 1)
        return v

    @property
    def admin_ids(self) -> List[int]:
        if not self.telegram_admin_ids:
            return []
        out: List[int] = []
        for chunk in self.telegram_admin_ids.split(","):
            chunk = chunk.strip()
            if chunk.isdigit() or (chunk.startswith("-") and chunk[1:].isdigit()):
                out.append(int(chunk))
        return out

    @property
    def media_path(self) -> Path:
        p = Path(self.media_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def telegram_webhook_base(self) -> Optional[str]:
        """Resolve the public base URL for the Telegram webhook.
        Priority: explicit TELEGRAM_WEBHOOK_URL → Render's RENDER_EXTERNAL_URL."""
        base = self.telegram_webhook_url or self.render_external_url
        return base.rstrip("/") if base else None

    @property
    def telegram_webhook_full_url(self) -> Optional[str]:
        """Full URL Telegram will POST updates to. None if webhook mode is off."""
        base = self.telegram_webhook_base
        if not base:
            return None
        path = self.telegram_webhook_path
        if not path.startswith("/"):
            path = "/" + path
        return base + path

    @property
    def telegram_use_webhook(self) -> bool:
        """Webhook mode is enabled whenever we have a public URL to receive on."""
        return self.telegram_webhook_full_url is not None

    @property
    def proxy(self) -> Optional[str]:
        """Single proxy URL applied uniformly. httpx 0.28+ no longer accepts a
        per-scheme `proxies` dict — only one proxy is honored here, falling back
        from PROXY_URL → HTTPS_PROXY → HTTP_PROXY."""
        return self.proxy_url or self.https_proxy or self.http_proxy or None


settings = Settings()
