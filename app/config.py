"""Application configuration loaded from environment variables."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Union

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.db_url import normalize_db_url


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
    # Extra chats that receive a COPY of every notification (comma-separated).
    # Use this to mirror alerts to your DM while the primary TELEGRAM_CHAT_ID is
    # a forum group — the group keeps per-account topics, the mirrors get a flat
    # stream (DMs/non-forum chats can't have topics).
    telegram_mirror_chat_ids: str = Field(default="", alias="TELEGRAM_MIRROR_CHAT_IDS")

    # Telegram webhook mode. If a public URL is available we run as a webhook
    # (one HTTP consumer, no `getUpdates` Conflict). If no URL is set we fall
    # back to long-polling, which is convenient for local development.
    telegram_webhook_url: Optional[str] = Field(default=None, alias="TELEGRAM_WEBHOOK_URL")
    telegram_webhook_secret: Optional[str] = Field(default=None, alias="TELEGRAM_WEBHOOK_SECRET")
    telegram_webhook_path: str = Field(default="/telegram/webhook", alias="TELEGRAM_WEBHOOK_PATH")
    # When the chat is a forum-enabled group, give each monitored account its
    # own topic (thread). Per-account alerts/media route to that thread; global
    # messages (sweeps, summaries, the menu) stay in General. Off by default so
    # plain 1:1 chats and non-forum groups behave exactly as before.
    telegram_forum_topics: bool = Field(default=False, alias="TELEGRAM_FORUM_TOPICS")
    # Render injects this with the public service URL — we use it as a fallback.
    render_external_url: Optional[str] = Field(default=None, alias="RENDER_EXTERNAL_URL")

    # Database
    database_url: str = Field(..., alias="DATABASE_URL")

    # Instagram request shape is fixed in app.monitor.instagram.
    # Optional Cookie header value. Paste the full cookie string from a
    # logged-in browser session (e.g.
    # "sessionid=...; csrftoken=...; ds_user_id=...; mid=...; ig_did=...").
    # When unset, requests go out unauthenticated.
    ig_session_cookie: Optional[str] = Field(default=None, alias="IG_SESSION_COOKIE")
    ig_proxy_url: Optional[str] = Field(default=None, alias="IG_PROXY_URL")
    # A page-fetch service on a machine whose connection Instagram trusts —
    # your PC (see tools/home_fetcher). Asked for the public profile page only,
    # and only after this host's own page request was refused. Leave empty to
    # disable. The token is the shared secret the service prints at startup.
    home_fetch_url: Optional[str] = Field(default=None, alias="HOME_FETCH_URL")
    home_fetch_token: Optional[str] = Field(default=None, alias="HOME_FETCH_TOKEN")

    # How many times a 401/403 from the Cloudflare Worker is re-asked. One
    # worker call is already 6 upstream attempts with rotating UAs and hosts —
    # but it may also leave from a different Cloudflare colo, and Instagram's
    # datacenter gate answers differently per colo, so a re-ask is a real second
    # chance rather than a repeat of the same question.
    #
    # The cost is blocked traffic, and that is where the two paths differ. A
    # sweep multiplies every extra attempt by every account (14 accounts × 8
    # upstream = a lot of blocked requests, which is what keeps the gate shut),
    # so it asks once and leaves the second chance to the paced retry rounds.
    # An on-demand check — Recheck, a card open, /story — is ONE account with
    # someone waiting for the answer, so it tries harder; nothing it does is
    # what shuts the gate.
    ig_sweep_auth_attempts: int = Field(default=1, alias="IG_SWEEP_AUTH_ATTEMPTS")
    ig_manual_auth_attempts: int = Field(default=3, alias="IG_MANUAL_AUTH_ATTEMPTS")

    # Scheduler
    check_interval: int = Field(default=1800, alias="CHECK_INTERVAL")
    jitter_seconds: int = Field(default=120, alias="JITTER_SECONDS")
    request_timeout: int = Field(default=20, alias="REQUEST_TIMEOUT")
    max_concurrent_fetches: int = Field(default=3, alias="MAX_CONCURRENT_FETCHES")

    # Sweep pacing + rate-limit guard (pacing-on-failure only — never changes a
    # request). How many accounts a sweep checks at once: 1 means one at a time,
    # the same request rhythm as a manual Recheck, which is the pattern
    # Instagram's anonymous gate answers reliably. Raise it only if a sweep
    # can't finish in time — every extra lane is a bigger burst.
    sweep_concurrency: int = Field(default=1, alias="SWEEP_CONCURRENCY")
    # The gap between checks widens as consecutive 401/403 blocks pile up (up to
    # the max). Once this many blocks happen in a row the guard pauses the sweep
    # for the cooldown so Instagram's short throttle window can clear, then
    # carries on with the remaining accounts; only after the pauses run out
    # (_SWEEP_BREAKER_MAX_PAUSES) are they deferred to the next sweep. Set the
    # threshold to 0 to disable the guard (adaptive pacing still runs); set the
    # cooldown to 0 to defer immediately instead of pausing.
    sweep_stagger_max_seconds: float = Field(default=12.0, alias="SWEEP_STAGGER_MAX_SECONDS")
    sweep_breaker_threshold: int = Field(default=5, alias="SWEEP_BREAKER_THRESHOLD")
    sweep_breaker_cooldown_seconds: float = Field(
        default=90.0, alias="SWEEP_BREAKER_COOLDOWN_SECONDS"
    )
    # Blocked accounts are re-checked in this many rounds after the sweep, each
    # after a longer cooldown (30s, 60s, 120s…), one account at a time. A 401
    # from the anonymous gate blocks a request, not an account, so a paced retry
    # usually goes through — this is what stops the sweep from reporting
    # failures that a manual Recheck can't reproduce. The rounds share one
    # wall-clock budget so a real outage can't stretch the sweep. 0 disables.
    sweep_retry_rounds: int = Field(default=3, alias="SWEEP_RETRY_ROUNDS")
    sweep_retry_budget_seconds: int = Field(default=300, alias="SWEEP_RETRY_BUDGET_SECONDS")
    # Hard cap on one sweep, after which it is abandoned so a hung connection
    # can't block every later run. It has to clear the paced sweep itself plus
    # everything the guard may legitimately spend waiting — the mid-sweep pauses
    # and the retry budget above — or the timeout would start killing healthy
    # sweeps mid-flight, which looks exactly like the failures it exists to
    # prevent. Keep it well under CHECK_INTERVAL.
    sweep_timeout_seconds: int = Field(default=1500, alias="SWEEP_TIMEOUT_SECONDS")

    # Stakeout mode — temporary high-frequency watch on a single target.
    # The floor sits above the 90s reel-data cache so every tick gets fresh
    # data without hammering Instagram into rate-limit 401s. Kept gentle on
    # purpose: all calls already route through the Cloudflare edge proxy.
    stakeout_default_interval: int = Field(default=180, alias="STAKEOUT_DEFAULT_INTERVAL")
    stakeout_min_interval: int = Field(default=120, alias="STAKEOUT_MIN_INTERVAL")
    stakeout_default_duration: int = Field(default=3600, alias="STAKEOUT_DEFAULT_DURATION")
    stakeout_max_duration: int = Field(default=21600, alias="STAKEOUT_MAX_DURATION")

    # Went-dark radar: alert when a monitored account posts nothing (no story,
    # post, or reel) for this many days. 0 disables the radar.
    dark_radar_days: int = Field(default=3, alias="DARK_RADAR_DAYS")

    # When a monitored account flips from private to public, automatically grab
    # its whole backlog (posts, reels, highlights, current story) and send it —
    # instead of silently baselining it like a normal first public sighting. Set
    # to false to keep the old baseline-only behavior.
    auto_grab_on_public: bool = Field(default=True, alias="AUTO_GRAB_ON_PUBLIC")

    # Highlight re-scan cadence (seconds). Listing every highlight reel's media
    # on every sweep is close to pure bandwidth: a reel's contents only change
    # when its owner adds a story to it, and that story was already detected and
    # delivered live minutes earlier. Reels that are NEW to the catalog are
    # always listed immediately, whatever this is set to; existing reels are
    # re-listed at most this often. 0 re-lists every reel on every sweep (the
    # old behavior) at roughly 12× the traffic.
    highlight_scan_interval: int = Field(default=21600, alias="HIGHLIGHT_SCAN_INTERVAL")

    # Per-sweep story/live status line ("HAS STORY" / "NO STORY" / "LIVE NOW").
    # ON by default: every check says where each public account stands, so
    # silence is never something you have to interpret. "No story" is a real
    # answer and it is the one the quiet mode never gave — it only spoke when
    # the status CHANGED, which left "@user has nothing up right now" and "the
    # bot didn't check" looking identical.
    #
    # What it does NOT do is repeat itself: a check that already announced the
    # story (the media, or the text stand-in when the download fails) does not
    # also get a "HAS STORY" line, so a check is still one message. That was
    # the actual complaint behind the old quiet mode — one story producing a
    # line every sweep ON TOP of the alert and the media — and it stays fixed.
    #
    # Private accounts never reach this path at all (the story phase is gated
    # on the privacy flag), so no line is ever printed for stories the bot
    # cannot and must not see.
    #
    # STORY_STATUS_HEARTBEAT=false restores change-only announcements. Every
    # status is logged for the digest and history either way.
    story_status_heartbeat: bool = Field(default=True, alias="STORY_STATUS_HEARTBEAT")

    # Follower-anomaly alert: a follower change is flagged only when it's large
    # in BOTH absolute terms (≥ abs_min) and relative terms (≥ pct_min of the
    # prior count), so it never fires on a small account's noise or a big
    # account's normal drift. Set either to 0 to disable the alert.
    follower_anomaly_abs_min: int = Field(default=500, alias="FOLLOWER_ANOMALY_ABS_MIN")
    follower_anomaly_pct_min: float = Field(default=0.10, alias="FOLLOWER_ANOMALY_PCT_MIN")

    # Digest: an opt-in daily/weekly roll-up of all changes instead of (well, on
    # top of) per-event alerts. The mode (off/daily/weekly) is a runtime setting
    # toggled with /digest — these two only pick WHEN the scheduled digest fires.
    # A daily digest goes out every day at digest_hour (UTC); a weekly one only
    # on digest_weekday (0=Monday). Reads the already-logged notifications, so
    # nothing extra is stored.
    digest_hour: int = Field(default=9, alias="DIGEST_HOUR")
    digest_weekday: int = Field(default=0, alias="DIGEST_WEEKDAY")

    # Storage
    media_dir: str = Field(default="./data/media", alias="MEDIA_DIR")

    # Data retention (days; 0 = keep forever)
    snapshot_retention_days: int = Field(default=30, alias="SNAPSHOT_RETENTION_DAYS")
    notification_retention_days: int = Field(default=90, alias="NOTIFICATION_RETENTION_DAYS")
    # raw_response JSONB is nulled after this many days even when the row is kept
    raw_response_retention_days: int = Field(default=7, alias="RAW_RESPONSE_RETENTION_DAYS")
    # Downloaded story/post media files older than this are deleted by the
    # daily cleanup — they were already delivered to Telegram, and an on-demand
    # request just re-downloads them. 0 keeps every file forever.
    media_retention_days: int = Field(default=14, alias="MEDIA_RETENTION_DAYS")

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
        return normalize_db_url(v)

    @field_validator("telegram_webhook_secret")
    @classmethod
    def sanitize_webhook_secret(cls, v: Optional[str]) -> Optional[str]:
        # Telegram's setWebhook rejects secret_token outside [A-Za-z0-9_-]{1,256}.
        # Render's `generateValue: true` produces a base64-ish string that can
        # include `+`, `/`, or `=` and crashes startup. Strip the disallowed
        # characters so the registered secret is always accepted.
        if v is None:
            return None
        cleaned = "".join(c for c in v if c.isalnum() or c in "_-")[:256]
        return cleaned or None

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
    def mirror_chat_ids(self) -> List[Union[int, str]]:
        """Parsed mirror chat ids — numeric ones become ints, the rest stay
        strings (e.g. @channel). Empty when no mirrors are configured."""
        out: List[Union[int, str]] = []
        for chunk in self.telegram_mirror_chat_ids.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            if chunk.lstrip("-").isdigit():
                out.append(int(chunk))
            else:
                out.append(chunk)
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
