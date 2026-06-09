"""Inline keyboard builders for The Watcher bot.

Callback-data scheme (kept short — Telegram caps callback_data at 64 bytes):
  menu:main                — show main menu
  menu:list:<page>         — show accounts list, page index (0-based)
  menu:status              — show monitoring stats
  menu:add                 — prompt user for a username to add
  menu:fetch               — prompt for any username to grab its story/highlights
  menu:export              — send CSV export
  menu:help                — show help
  menu:interval            — show interval preset chooser
  menu:setinterval:<sec>   — set scheduler interval to <sec>
  menu:setinterval:custom  — prompt for free-form interval text
  acc:open:<username>      — open account card
  acc:recheck:<username>   — force a re-check
  acc:history:<username>   — recent change log for account
  acc:photo:<username>     — send latest stored profile picture
  acc:story:<username>     — download & send the current story now
  acc:highlights:<u>       — list highlight names
  acc:hldl:<idx>:<u>       — download highlight at list index <idx>
  acc:hlall:<u>            — download every highlight reel at once
  acc:remove:<username>    — show remove confirmation
  acc:remove_yes:<u>       — confirmed remove
  menu:cleardb             — show clear-history confirmation
  menu:cleardb_yes         — execute clear-history
  noop                     — non-actionable button (e.g. page indicator)
"""

from __future__ import annotations

from typing import Sequence

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

PAGE_SIZE = 6


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📋 Accounts", callback_data="menu:list:0"),
                InlineKeyboardButton("📊 Status", callback_data="menu:status"),
            ],
            [
                InlineKeyboardButton("➕ Add", callback_data="menu:add"),
                InlineKeyboardButton("⏱ Interval", callback_data="menu:interval"),
            ],
            [
                InlineKeyboardButton("📤 Export", callback_data="menu:export"),
                InlineKeyboardButton("ℹ️ Help", callback_data="menu:help"),
            ],
            [
                InlineKeyboardButton("🔎 Any user", callback_data="menu:fetch"),
            ],
            [
                InlineKeyboardButton("🔄 Sweep All", callback_data="menu:sweep"),
            ],
        ]
    )


def fetch_actions(username: str) -> InlineKeyboardMarkup:
    """Story/Highlights actions for an arbitrary (possibly non-monitored) user."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📖 Story", callback_data=f"acc:story:{username}"
                ),
                InlineKeyboardButton(
                    "✨ Highlights", callback_data=f"acc:highlights:{username}"
                ),
            ],
            [
                InlineKeyboardButton("🏠 Home", callback_data="menu:main"),
            ],
        ]
    )


def accounts_list(accounts: Sequence, page: int = 0) -> InlineKeyboardMarkup:
    total = len(accounts)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE

    rows: list[list[InlineKeyboardButton]] = []
    for a in accounts[start:end]:
        marker = "🟢" if a.active else "⏸"
        rows.append(
            [
                InlineKeyboardButton(
                    f"{marker} @{a.username}",
                    callback_data=f"acc:open:{a.username}",
                ),
            ]
        )

    if pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(
                InlineKeyboardButton("◀️", callback_data=f"menu:list:{page - 1}")
            )
        nav.append(
            InlineKeyboardButton(f"· {page + 1} / {pages} ·", callback_data="noop")
        )
        if page < pages - 1:
            nav.append(
                InlineKeyboardButton("▶️", callback_data=f"menu:list:{page + 1}")
            )
        rows.append(nav)

    rows.append(
        [
            InlineKeyboardButton("➕ Add", callback_data="menu:add"),
            InlineKeyboardButton("🔄 Refresh", callback_data=f"menu:list:{page}"),
            InlineKeyboardButton("🏠 Home", callback_data="menu:main"),
        ]
    )

    return InlineKeyboardMarkup(rows)


def account_actions(username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔄 Recheck", callback_data=f"acc:recheck:{username}"
                ),
                InlineKeyboardButton(
                    "📜 History", callback_data=f"acc:history:{username}"
                ),
            ],
            [
                InlineKeyboardButton(
                    "🖼 Photo", callback_data=f"acc:photo:{username}"
                ),
                InlineKeyboardButton(
                    "🗑 Remove", callback_data=f"acc:remove:{username}"
                ),
            ],
            [
                InlineKeyboardButton(
                    "📖 Story", callback_data=f"acc:story:{username}"
                ),
                InlineKeyboardButton(
                    "✨ Highlights", callback_data=f"acc:highlights:{username}"
                ),
            ],
            [
                InlineKeyboardButton("◀️ List", callback_data="menu:list:0"),
                InlineKeyboardButton("🏠 Home", callback_data="menu:main"),
            ],
        ]
    )


def highlights_view(
    username: str, items: Sequence[tuple[str, str]]
) -> InlineKeyboardMarkup:
    """List one download button per highlight, referenced by list index.

    `items` is the ordered (highlight_id, title) list shown to the user; the
    index in the callback maps back to the same ordering on the download side.
    """
    rows: list[list[InlineKeyboardButton]] = []
    if items:
        rows.append(
            [
                InlineKeyboardButton(
                    f"⬇️ Download all ({len(items)})",
                    callback_data=f"acc:hlall:{username}",
                )
            ]
        )
    for idx, (_hid, title) in enumerate(items):
        label = title.strip() or "(untitled)"
        if len(label) > 28:
            label = label[:27] + "…"
        rows.append(
            [
                InlineKeyboardButton(
                    f"⬇️ {label}",
                    callback_data=f"acc:hldl:{idx}:{username}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                "🔄 Refresh", callback_data=f"acc:highlights:{username}"
            ),
            InlineKeyboardButton(
                "◀️ Back", callback_data=f"acc:open:{username}"
            ),
        ]
    )
    return InlineKeyboardMarkup(rows)


def confirm_remove(username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🗑 Remove",
                    callback_data=f"acc:remove_yes:{username}",
                ),
                InlineKeyboardButton(
                    "✕ Cancel", callback_data=f"acc:open:{username}"
                ),
            ],
        ]
    )


def open_account(username: str) -> InlineKeyboardMarkup:
    """Single button that opens the account card (used after Add)."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"👁 @{username}",
                    callback_data=f"acc:open:{username}",
                ),
                InlineKeyboardButton("🏠 Home", callback_data="menu:main"),
            ]
        ]
    )


def back_to_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🏠 Home", callback_data="menu:main")]]
    )


def status_actions() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔄 Sweep Now", callback_data="menu:sweep:ids"),
            ],
            [
                InlineKeyboardButton("⏱ Interval", callback_data="menu:interval"),
                InlineKeyboardButton("🏠 Home", callback_data="menu:main"),
            ],
            [
                InlineKeyboardButton("🗑 Clear Old Data", callback_data="menu:cleardb"),
            ],
        ]
    )


def confirm_clear_db() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🗑 Yes, clear it", callback_data="menu:cleardb_yes"
                ),
                InlineKeyboardButton("✕ Cancel", callback_data="menu:status"),
            ],
        ]
    )


def back_to_list() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("◀️ List", callback_data="menu:list:0")]]
    )


def cancel_only() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("✕ Cancel", callback_data="menu:main")]]
    )


INTERVAL_PRESETS: list[tuple[str, int]] = [
    ("5m", 300),
    ("15m", 900),
    ("30m", 1800),
    ("1h", 3600),
    ("2h", 7200),
    ("6h", 21600),
]


def interval_presets(current_seconds: int) -> InlineKeyboardMarkup:
    """Two-by-three preset grid + Custom + back."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for label, seconds in INTERVAL_PRESETS:
        marker = "✓ " if seconds == current_seconds else ""
        row.append(
            InlineKeyboardButton(
                f"{marker}{label}",
                callback_data=f"menu:setinterval:{seconds}",
            )
        )
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [
            InlineKeyboardButton("✏️ Custom", callback_data="menu:setinterval:custom"),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton("◀️ Status", callback_data="menu:status"),
            InlineKeyboardButton("🏠 Home", callback_data="menu:main"),
        ]
    )
    return InlineKeyboardMarkup(rows)
