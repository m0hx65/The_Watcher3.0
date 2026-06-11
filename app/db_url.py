"""Database-URL normalization, kept free of side effects.

Lives in its own module (rather than app.config) so tools like
scripts/migrate_db.py can reuse it without instantiating Settings(), which
would demand the bot's runtime env vars (TELEGRAM_BOT_TOKEN, …).
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def normalize_db_url(url: str) -> str:
    """Make a Postgres URL safe for SQLAlchemy's asyncpg driver.

    1. Any ``postgres://`` / ``postgresql://`` prefix becomes
       ``postgresql+asyncpg://`` (Render and Heroku hand out the bare
       ``postgres://`` form that SQLAlchemy no longer accepts directly).
    2. libpq-only query params that asyncpg rejects are fixed up so a Neon or
       Supabase connection string can be pasted in verbatim:
         * ``sslmode=...``         → asyncpg's ``ssl=...`` (same vocabulary,
           e.g. ``require``), so the TLS Neon/Supabase mandate still applies;
         * ``channel_binding=...`` → dropped (asyncpg has no such connect arg).

    Non-Postgres URLs (e.g. ``sqlite+aiosqlite://``) are returned unchanged.
    """
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgresql://") and "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)

    if not url.startswith("postgresql+asyncpg://") or "?" not in url:
        return url

    parts = urlsplit(url)
    new_params: list[tuple[str, str]] = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        low = key.lower()
        if low == "sslmode":
            new_params.append(("ssl", value))
        elif low == "channel_binding":
            continue  # asyncpg doesn't accept this libpq-only option
        else:
            new_params.append((key, value))
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(new_params), parts.fragment)
    )
