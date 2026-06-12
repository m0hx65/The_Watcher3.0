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
is often enough — you get a dedicated, shareable feed.

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

## 5. (Optional) Wire per-account topic routing in the bot

Today every send goes to `TELEGRAM_CHAT_ID` with no thread id, so everything
lands in General. To route each account to its own topic you'd:

1. **Store a topic id per account.** Add a nullable `topic_id` column to
   `monitored_accounts` (mirrors how `instagram_id` was added), or keep a
   `app_settings` entry keyed `topic:<account_id>`.
2. **Auto-create on add.** In the `/add` flow call the Bot API
   `createForumTopic(chat_id, name="@<username>")`, store the returned
   `message_thread_id`. python-telegram-bot exposes this as
   `bot.create_forum_topic(...)`.
3. **Thread every send.** `NotificationDispatcher.send_text/photo/video/document`
   take a `message_thread_id` and pass it through to the
   `bot.send_*` calls (the parameter already exists in the Bot API). The
   `MonitorService` story/profile paths would look up the account's topic id and
   hand it to the dispatcher.
4. **Fallback.** When an account has no topic id (or the chat isn't a forum),
   omit `message_thread_id` so it posts to General exactly like today — no
   regression for single-chat users.

This is a self-contained feature (one column, one dispatcher param, one lookup);
it's deliberately left out of the default build so the bot keeps working in a
plain one-on-one chat with zero configuration. Open an issue or ask if you want
it turned on — the hooks above are where it slots in.
