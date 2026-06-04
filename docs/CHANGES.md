# Session changelog — 2026-05-13

## 1. UI polish (keyboards & messages)

**Problem:** Buttons looked unbalanced and inconsistent. Main menu had a lone "Help" button on its own row. Labels were verbose and mixed in style. The interval preset active-state indicator (`• `) was barely visible.

**Files changed:** `app/bot/keyboards.py`, `app/bot/handlers.py`

### keyboards.py
| Before | After |
|---|---|
| 2-row menu with orphaned "Help" | Balanced 2×3 grid |
| `➕ Add account` / `📤 Export CSV` | `➕ Add` / `📤 Export` (Interval promoted to main menu) |
| All back buttons labelled "Menu" | Unified to "Home" |
| Active preset marker `• 30m` | `✓ 30m` (clearly visible) |
| Page indicator `Page 1/2` (looks tappable) | `· 1 / 2 ·` (decorative) |
| `✅ Yes, remove` / `❌ Cancel` | `🗑 Remove` / `✕ Cancel` |
| `Open @username` (no emoji) | `👁 @username` |
| `◀️ Back to list` / `◀️ Accounts` | `◀️ List` (consistent) |
| `✏️ Custom…` | `✏️ Custom` |

### handlers.py
- `WELCOME_TEXT`: tightened copy, removed "for example" verbosity
- `HELP_TEXT`: removed redundant `/menu` entry, tighter command descriptions

---

## 2. Panel always stays as last message

**Problem:** The main-menu panel (inline keyboard) got buried under automated notifications as sweeps ran. Users had to scroll up to find it.

### Iteration 1 — post-sweep hook (commit `6fd8df6`)

Added `post_sweep_hook` to `WatcherScheduler`. After each scheduled sweep completes, the hook deletes the old panel message and re-sends it at the bottom of the chat.

**Shortfall:** Only fired after *scheduled sweeps*. Manual rechecks (tapping the Recheck button) also send notifications but didn't trigger the bump. Also, `bot_data` is wiped on server restart, so the panel ID was lost.

### Iteration 2 — per-notification hook + debounce + DB persistence (commit `3c1d81e`)

**Files changed:** `app/bot/notifications.py`, `app/bot/handlers.py`, `app/main.py`, `app/workers/scheduler.py`

#### How it works

1. `NotificationDispatcher` gains a `post_send_hook` callback, called after every successful `send_text` or `send_photo`.

2. The hook (`_schedule_bump` in `main.py`) fires an `asyncio.Task` with a **2-second debounce**:
   - If a bump task is already pending, do nothing (the existing task will cover this notification too).
   - This means 6 accounts checked concurrently in a sweep → 6 hook calls → only **1 bump** after 2 seconds, when all notifications have landed.

3. `_do_bump` runs after the delay:
   - Deletes the current panel message (silent-fails if already gone).
   - Sends a fresh main menu at the bottom.
   - Saves the new message ID + chat ID to the **DB settings table** (`panel_msg_id` / `panel_chat_id`).

4. On **startup**, `main.py` loads the persisted panel IDs from the DB into `bot_data`, so tracking survives server restarts without any user action.

5. `/menu` and `/start` also persist the panel ID to DB immediately (in `_send_panel`).

#### Trigger coverage

| Event | Bumps panel? |
|---|---|
| Scheduled sweep with changes | ✅ via `post_send_hook` |
| Scheduled sweep with failures | ✅ via `post_send_hook` |
| Manual recheck (button) | ✅ via `post_send_hook` |
| `/menu` or `/start` command | ✅ deletes old + sends new directly |
| Sweep with no changes (no notification sent) | — (nothing to bump over) |

#### Note on first run after deploy

If the server restarts and the user has never sent `/menu` with the new code (so `panel_msg_id` is not in DB), the bump simply does nothing until the user opens `/menu` once. Subsequent restarts load from DB automatically.
