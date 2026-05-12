"""HTML formatting helpers for Telegram messages."""

from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Optional


def esc(value: Optional[str]) -> str:
    """Escape user-controlled text for HTML parse mode."""
    if value is None:
        return ""
    return html.escape(str(value), quote=False)


def fmt_number(n: Optional[int]) -> str:
    if n is None:
        return "—"
    return f"{n:,}"


def fmt_delta(old: Optional[int], new: Optional[int]) -> str:
    if old is None or new is None:
        return ""
    diff = new - old
    if diff == 0:
        return ""
    sign = "+" if diff > 0 else ""
    return f" ({sign}{diff:,})"


def fmt_bool(b: Optional[bool]) -> str:
    if b is None:
        return "—"
    return "YES" if b else "NO"


def fmt_timestamp(dt: Optional[datetime]) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def truncate(text: Optional[str], limit: int = 1000) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
