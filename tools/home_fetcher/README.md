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
`GET /home-fetch/jobs`, is handed a username, fetches
`instagram.com/<username>/` from where it sits, and posts Instagram's answer
back to `POST /home-fetch/jobs/<id>`. The bot parses the same payload it parses
from its own page reading and stores it as a normal page reading.

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

- Each page is about 700 KB from Instagram and about 150 KB compressed on
  its way to the bot. For 17 accounts at three sweeps a day that is roughly
  1.1 GB a month downloaded on the phone's connection.
- The worker paces itself to one Instagram request every 2 seconds, on top
  of the bot's own pacing.
- Your VPN, if any, is fine: the request goes out the same way your browser's
  does. Instagram's answer depends on the IP, and yours is trusted.

## Endpoints the worker uses

| Call | Auth | Meaning |
|---|---|---|
| `GET /home-fetch/jobs?wait=25` | `X-Watcher-Token`, `X-Watcher-Worker` | Long-poll: `{"job": {"id", "username"}}` or `{"job": null}` |
| `POST /home-fetch/jobs/<id>` | same, plus `X-IG-Status`, `X-IG-Final-Url`, gzip body | Instagram's status and HTML, as received |
