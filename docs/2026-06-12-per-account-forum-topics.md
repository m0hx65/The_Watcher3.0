# Feature: per-account forum topics (2026-06-12)

Give each monitored account its own Telegram forum **topic** (thread). That
account's alerts route to its thread; global messages stay in General. Opt-in
via `TELEGRAM_FORUM_TOPICS=true`; off by default so 1:1 chats and non-forum
groups behave exactly as before.

## How it works

- **Mapping** lives in `app_settings` under `topic:<account_id>` (no schema
  migration) — `crud.get_account_topic / set_account_topic`.
- **Resolution:** `MonitorService.topic_for(account_id, username)` returns the
  thread id, creating it on first use via `bot.create_forum_topic` (named
  `@<username>`), caching it in memory and persisting it. One topic per account;
  a single creation failure (not a forum / missing *Manage topics*) latches the
  feature off for the process so a misconfigured chat is never hammered.
- **Threading:** `NotificationDispatcher.send_text/photo/video/document` gained
  a `message_thread_id` param passed straight to the Bot API. Per-account
  service paths pass the resolved topic; global paths pass `None`.
- **Resilience:** a deleted/invalid topic raises Telegram "thread not found";
  `_send_with_retry` catches it, clears the thread, and resends to General
  instead of dropping the message.

### What routes to a topic vs General

| → account's topic | → General (thread None) |
|---|---|
| profile change alerts + profile-pic doc | sweep started / complete |
| story / live status | ID-backfill summaries |
| highlight catalog changes | stakeout start/stop/complete |
| new story / post / reel media | the menu panel |
| went-dark / back-active alerts | |

## Using it

1. Group → enable **Topics**; add the bot as **admin** with *Manage topics*.
2. `TELEGRAM_FORUM_TOPICS=true`, redeploy.
3. **📊 Status → 🧵 Sync topics** (or `/synctopics`) once to backfill all
   existing accounts (incl. private). New accounts get a topic on add; public
   accounts also self-create on their next sweep story-status message.

## Tests

`scripts/test_forum_topics.py` (recording fake notifier captures every
`message_thread_id`): lazy create + persist, cache reuse (no duplicate topic),
distinct topics per account, per-account alert routed to its topic, global
message → None, `sync_topics` counts, feature-flag-off → General, and
create-failure latch → General.

Full pre-existing suite still passes — notably `test_notification_retry`, which
covers the refactored `_send_with_retry` (actions now take the thread id and the
new BadRequest/thread-fallback branch).
