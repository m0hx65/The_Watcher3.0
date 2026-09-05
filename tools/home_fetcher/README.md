# Home page fetcher

A 150-line service you run on your own PC so the bot can read Instagram
profile pages from a connection Instagram trusts.

## Why it exists

Since 2026-09-05 Instagram hands the logged-out profile page — the one whose
embedded payload carries the follower/following counts, the bio and the
privacy flag — only to IPs it hasn't seen scraping. Render's shared IP and
Cloudflare's egress are both bounced to the login page and answered `429`.
Your PC, on your home line or behind your VPN, gets the page every time. This
service turns that into a fourth door for the bot: after its own page attempt
is refused, it asks your PC for the same page and parses the same payload.

It fetches nothing but `instagram.com/<username>/`, keeps nothing, logs one
line per request, and refuses anything without the shared token.

## Setup (once)

1. **Python + curl_cffi on the PC.** You already have both if you have run the
   bot's tests here; otherwise `pip install curl_cffi`.
2. **Start the service.**

       python tools\home_fetcher\home_fetcher.py

   It prints its access token and listens on `127.0.0.1:8787`. The token is
   saved next to the script in `home_fetcher.token` (git-ignored) so it stays
   the same across restarts. To start it with Windows, put a shortcut to
   `home_fetcher.bat` in your Startup folder (`Win+R`, `shell:startup`).
3. **Expose it with a stable HTTPS URL, for free.** Install
   [Tailscale](https://tailscale.com/download) on the PC, sign in, then:

       tailscale funnel --bg 8787

   The first run prints a link to enable Funnel for your tailnet (one click in
   the admin console, plus HTTPS certificates if not already on). It prints
   your public URL — `https://<machine>.<tailnet>.ts.net` — and `--bg` makes
   it persist across reboots. Check it in a browser: `<url>/health` should
   show `{"ok": true, ...}`.

   Any other tunnel with a stable hostname works too (ngrok's free static
   domain, a named Cloudflare Tunnel on your own domain). The bot only needs a
   URL it can reach and the token.
4. **Tell the bot.** In Render → your service → Environment, add

       HOME_FETCH_URL   = https://<machine>.<tailnet>.ts.net
       HOME_FETCH_TOKEN = <the token from step 2>

   and redeploy. `/probe <username>` in Telegram now shows a **Home fetcher**
   line, and the sweep summary counts profiles read from the page.

## What to expect

- Each check that reaches this door costs one page fetch, about 700 KB. For
  17 accounts at three sweeps a day that is roughly 1.1 GB a month through
  your connection.
- The service paces itself to one Instagram request every 2 seconds, on top
  of the bot's own pacing.
- If the PC is off or the tunnel is down, the bot notices within a few
  seconds and carries on id-only for that sweep — username, picture and story
  status stay live; counts and bio wait for the next sweep. Nothing breaks.
- Your VPN, if any, is fine: the request goes out the same way your browser's
  does. Instagram's answer depends on the IP, and yours is trusted.

## Endpoints

| Path | Auth | Answer |
|---|---|---|
| `GET /health` | none | `{"ok": true, "requests": N}` |
| `GET /page/<username>` | `X-Watcher-Token` header | Instagram's status code and HTML as-is, plus `X-IG-Final-Url` and `X-IG-Payload: 1/0` |
