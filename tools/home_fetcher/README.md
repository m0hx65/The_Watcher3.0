# Home page fetcher

A small worker you run on a device with a connection Instagram trusts — an
old Android phone in Termux, or your PC — so the bot can read Instagram
profile pages again.

## Why it exists

Since 2026-09-05 Instagram hands the logged-out profile page — the one whose
embedded payload carries the follower/following counts, the bio and the
privacy flag — only to IPs it hasn't seen scraping. Render's shared IP and
Cloudflare's egress are both bounced to the login page and answered `429`.
Your home line, your VPN, your phone's data plan: all get the page every time.

## How it works

The worker **polls the bot** for work over ordinary outbound HTTPS: it asks
`GET /home-fetch/jobs`, is handed a batch of usernames, fetches
`instagram.com/<username>/` for each from where it sits, and posts Instagram's
answer back to `POST /home-fetch/jobs/<id>`. The bot parses the same payload it
parses from its own page reading and stores it as a normal page reading.

It is built for a slow link (a phone on a VPN): one keep-alive connection per
thread instead of a fresh DNS lookup + TLS handshake per request (that setup
cost was ~30 s per request on the phone), IPv4 tried first, one poll brings a
whole batch, only the page's few-kilobyte payload goes back, not the 700 KB
page, uploads run in the background so the next fetch never waits on the last
upload, and the bot never waits on the phone — a sweep hands it the whole list
up front and each check picks up its page when it gets there.

The worker fetches two kinds of job: the profile page, and the graphql reel
query by numeric id (current username, avatar, story/live status, highlight
catalog). The bot's Cloudflare Worker is refused on that query per colo, ~9 s
per refusal; the phone answers it in about a second, so a sweep hands over
every account's reel query up front too. The page also answers the story
question on its own (`latest_reel_media`: 0 = no story, else its timestamp).

Pacing: one Instagram request every 2 seconds. If Instagram tells the phone to
"wait a few minutes", the worker reports that as a 429 and pauses for a minute
before fetching again.

Nothing dials into your network, so it works behind carrier-grade NAT (your
router's 10.x WAN address), on a phone, without root, and without any tunnel.
The worker fetches nothing but that page, stores nothing, logs one line per
job, and is refused without the shared token. If the device is off, the bot
notices within a second and carries on id-only for that sweep — username,
picture and story status stay live; counts and bio wait for the next sweep.

## Setup on an old Android phone (Termux)

Needs Android 7 or newer, Wi-Fi at home (or mobile data — either IP is fine),
and a charger.

1. **Install Termux** from [F-Droid](https://f-droid.org/packages/com.termux/)
   or the [GitHub releases](https://github.com/termux/termux-app/releases) —
   **not** the Play Store build, which is abandoned. Also install
   **Termux:Boot** from the same source and open it once, so the worker can
   start when the phone boots.
2. **Open Termux** and install Python:

       pkg update -y && pkg install -y python

3. **Get the worker** (one file) and the start script:

       mkdir -p ~/home_fetcher && cd ~/home_fetcher
       curl -fsSLO https://raw.githubusercontent.com/m0hx65/The_Watcher3.0/main/tools/home_fetcher/home_fetcher.py
       curl -fsSLO https://raw.githubusercontent.com/m0hx65/The_Watcher3.0/main/tools/home_fetcher/termux/start.sh
       chmod +x start.sh

4. **Make a token** and write the settings file. Replace the URL with your
   bot's Render URL (Render → your service → the `onrender.com` address):

       python home_fetcher.py --new-token
       printf 'WATCHER_URL=https://YOUR-BOT.onrender.com\nHOME_FETCH_TOKEN=PASTE-THE-TOKEN\nHOME_FETCH_WORKER=xiaomi\n' > home_fetcher.env

5. **Tell the bot.** In Render → your service → Environment, add
   `HOME_FETCH_TOKEN` with the same token and redeploy. Until the bot has it,
   the worker logs "home fetcher is disabled" and keeps waiting.
6. **Start it**: `./start.sh`. You should see `polling https://… as 'xiaomi'`,
   and within the next sweep lines like
   `@65xim: Instagram answered HTTP 200, 701408 bytes, payload=yes`.
7. **Stop MIUI from killing it.** Settings → Apps → Manage apps → Termux:
   *Battery saver* → **No restrictions**; *Autostart* → on; allow
   notifications. In Recents, pull the Termux card down and tap the lock so
   it is never swiped away. Settings → Wi-Fi → Additional settings → *Keep
   Wi-Fi on during sleep* → **Always**. Leave the phone on the charger.
8. **Start on boot**: `mkdir -p ~/.termux/boot && cp ~/home_fetcher/start.sh ~/.termux/boot/`.
   Reboot once to check: Termux should come up and start polling by itself.

In Telegram, `/probe 65xim` now shows a **Home fetcher — connected** line with
the counts, and the sweep summary counts profiles read from the page. The
status line shows the phone's battery too: the worker reads it from Android
with every poll, and if it drops to 20% while not charging (the charger fell
out, the power went) the bot sends you one alert, another at 10%, and a
"charging again" once it's back on power (`HOME_FETCH_LOW_BATTERY_PERCENT`).

## Setup on a Windows PC

Same worker, same settings file. Put a shortcut to `home_fetcher.bat` in the
Startup folder (`Win+R`, `shell:startup`). It runs whenever the PC is on and
the bot uses it whenever it happens to be polling during a sweep.

## What to expect

- Each page is about 700 KB from Instagram and a few KB on its way back to
  the bot (just the payload). For 17 accounts at three sweeps a day that is
  roughly 1.1 GB a month downloaded on the phone's connection.
- The worker paces itself to one Instagram request per second, on top of
  the bot's own pacing (about one account every 2-3 seconds).
- Your VPN, if any, is fine: the request goes out the same way your browser's
  does. Instagram's answer depends on the IP, and yours is trusted.

## Endpoints the worker uses

| Call | Auth | Meaning |
|---|---|---|
| `GET /home-fetch/jobs?wait=25&batch=8` | `X-Watcher-Token`, `X-Watcher-Worker`, `X-Watcher-Battery`, `X-Watcher-Charging` | Long-poll: `{"jobs": [{"id", "username"}, …]}` (`job` = the first, for old workers) |
| `POST /home-fetch/jobs/<id>` | same, plus `X-IG-Status`, `X-IG-Final-Url`, `X-IG-Payload`, gzip body | Instagram's status and the page's payload (or the head of the page when it has none) |
