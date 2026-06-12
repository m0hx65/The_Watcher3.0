# Telegram forum topics — how to give each target its own thread

Telegram **Topics** turn one group into a forum: a left-hand list of named
threads, each its own scrollable conversation. Pointing The Watcher at a forum
group lets every monitored account become its own topic — `@nasa` alerts in the
*nasa* thread, `@natgeo` in the *natgeo* thread — instead of one merged stream.

This is set up entirely on Telegram's side; the bot already sends to whatever
chat `TELEGRAM_CHAT_ID` names. Routing **each account to its own topic** needs a
small code change, described at the end.

---

## 1. Turn a group into a forum (enable Topics)

1. Create a group (or use an existing one).
2. Open the group → **Edit** (pencil) → **Topics** → toggle it **on**.
   - On mobile: group name → ✏️ Edit → enable **Topics**.
   - On Telegram Desktop: group → ⋮ → **Manage group** → **Topics**.
3. The group now shows a **General** topic plus a ➕ to add more.

> Topics require the group to be a *supergroup*. Telegram upgrades it
> automatically the first time you enable Topics or exceed the basic-group
> limits — nothing for you to do.

## 2. Add the bot and let it post

1. Add your bot to the group as a **member**.
2. Promote it to **admin** (group → Administrators → Add). Topic posting works
   for non-admins too, but admin avoids "send to topic" restrictions and lets it
   manage messages.
3. If you keep [privacy mode](https://core.telegram.org/bots/features#privacy-mode)
   on (the default), the bot still receives commands addressed to it and can
   send freely — that's all The Watcher needs.

## 3. Point the bot at the group

Set `TELEGRAM_CHAT_ID` to the **group's** id (not your personal chat). The fast
way to get it:

1. Add [@RawDataBot](https://t.me/RawDataBot) (or @getidsbot) to the group
   briefly and read `chat.id` — a supergroup id looks like `-1001234567890`.
2. Put that in `TELEGRAM_CHAT_ID` and redeploy. Remove the helper bot.

At this point **all** alerts land in the group's **General** topic. That alone
is often enough — but the bot can also give **each account its own thread**
(next section).

## 4. (Optional) Get a topic's id

Each topic has a numeric `message_thread_id`. To send into a specific topic the
bot must pass that id with every message. To find it:

- Open the topic in **Telegram Desktop/Web**, copy a message link — it looks
  like `https://t.me/c/1234567890/<topic_id>/<message_id>`; the middle number is
  the topic id. (The **General** topic is id `1`, or omit the id entirely.)
- Or, when the bot itself creates the topic via the Bot API
  (`createForumTopic`), the response includes `message_thread_id` — the clean,
  programmatic way.

---

## 5. Per-account topics — one thread per account (built in)

The bot can give **every monitored account its own topic**: `@nasa` alerts in
the *@nasa* thread, `@natgeo` in *@natgeo*, while global messages (sweep
start/complete, ID-backfill summaries, the menu panel) stay in **General**.

### Turn it on
1. Make the chat a forum and the bot an admin with **Manage topics** (sections
   1–3 above).
2. Set `TELEGRAM_FORUM_TOPICS=true` and redeploy.
3. Tap **📊 Status → 🧵 Sync topics** (or run `/synctopics`) once. The bot
   creates a topic named `@<username>` for every monitored account, including
   private ones. New accounts get a topic automatically when added.

That's it. From then on each account's profile changes, story/live status,
highlight updates, new story/post/reel media, and went-dark alerts route to its
own thread.

### How it works (for the curious)
- **Mapping:** each account's `message_thread_id` is stored in `app_settings`
  under `topic:<account_id>` — no schema migration needed.
- **Lazy creation:** `MonitorService.topic_for(account_id, username)` creates the
  topic on first use (`bot.create_forum_topic`), caches it in memory, and
  persists it. Public accounts also get one on the next sweep (their per-sweep
  story-status message triggers creation), so `/synctopics` is mainly for an
  immediate, complete backfill.
- **Threading:** `NotificationDispatcher.send_text/photo/video/document` take a
  `message_thread_id`; the per-account service paths pass the resolved topic,
  global paths pass `None` (General).
- **Resilience:** if a topic is later deleted, Telegram's "thread not found"
  error is caught and the message is resent to General instead of being lost.
  If the chat isn't a forum or the bot lacks the right, topic creation fails
  once, the feature latches off for the process, and everything posts to
  General — so a misconfiguration never drops alerts.

### Turn it off
Set `TELEGRAM_FORUM_TOPICS=false` (default) and everything posts to General
again — a plain 1:1 chat or non-forum group behaves exactly as before.
