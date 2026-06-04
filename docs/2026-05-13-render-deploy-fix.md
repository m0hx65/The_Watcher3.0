# Render Deploy Fix — 2026-05-13

## Issues addressed

### 1. Telegram startup crash — `Secret token contains unallowed characters`

**Root cause:** `render.yaml` uses `generateValue: true` for `TELEGRAM_WEBHOOK_SECRET`.
Render generates a base64-ish string that includes `+`, `/`, and `=`.
Telegram's `setWebhook` API only accepts `[A-Za-z0-9_-]{1,256}` for `secret_token`,
so startup blew up at `main.py:88` every deploy.

**Fix:** Added a `field_validator` on `telegram_webhook_secret` in `app/config.py`
that strips disallowed characters at config load time and caps the result at 256 chars.
If nothing valid remains after stripping, the field is set to `None` (no secret registered).

Both sides of the secret check — `main.py` (registering the webhook via `set_webhook`)
and `routes.py` (verifying inbound `X-Telegram-Bot-Api-Secret-Token` headers) — read
from the same sanitized `settings.telegram_webhook_secret`, so they stay in sync.
No env var changes required on Render.

```python
# app/config.py
@field_validator("telegram_webhook_secret")
@classmethod
def sanitize_webhook_secret(cls, v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    cleaned = "".join(c for c in v if c.isalnum() or c in "_-")[:256]
    return cleaned or None
```

**Commit:** `c68c2be`

---

### 2. Instagram fetch shape — confirmed correct, no change needed

The client at `app/monitor/instagram.py` is already locked to the single allowed endpoint:

```
GET /api/v1/users/web_profile_info/?username=<u> HTTP/2
Host: www.instagram.com
x-ig-app-id: 936619743392459
```

No other Instagram endpoints, cookies (unless `IG_SESSION_COOKIE` is set), or media
downloads are issued by this client. HTTP/2 is enforced — non-HTTP/2 responses are
rejected immediately rather than silently accepted.
