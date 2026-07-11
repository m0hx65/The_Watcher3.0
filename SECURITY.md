# Security Policy

The Watcher is a self-hosted, login-free Instagram-monitoring Telegram bot.
Because every deployment is run by its own operator, this policy covers both the
code in this repository and the safe operation of a deployment.

## Supported Versions

This project ships from `main` — there are no long-term release branches. Only
the latest commit on `main` receives security fixes. If you run a fork or a
pinned older commit, rebase onto the current `main` to pick up fixes.

| Version           | Supported |
| ----------------- | --------- |
| Latest `main`     | ✅        |
| Older commits/forks | ❌      |

## Reporting a Vulnerability

**Please do not open a public issue for security problems**, and do not include
secrets (tokens, cookies, connection strings) in any public report.

Report privately through GitHub's **“Report a vulnerability”** button, under the
repository's **Security → Advisories** tab
(<https://github.com/m0hx65/The_Watcher3.0/security/advisories/new>). This opens
a private advisory visible only to the maintainer.

When reporting, please include:

- A description of the issue and its impact.
- Steps to reproduce (a minimal proof-of-concept if possible).
- Affected file(s)/commit, and any relevant configuration.

**What to expect:**

- Acknowledgement within **7 days**.
- An initial assessment (accepted / needs-info / declined) within **14 days**.
- For accepted issues, a fix on `main` as soon as practical, credited to the
  reporter unless anonymity is requested.

## Scope

In scope:

- Code in this repository (the bot, monitor, scheduler, and API).
- Handling of operator secrets (Telegram token, session cookie, database URL,
  proxy/worker credentials).
- Injection, path traversal, SSRF, deserialization, or authorization flaws in
  the request-handling and media paths.

Out of scope:

- Vulnerabilities in Instagram, Telegram, saveinsta.to, or other third-party
  services the bot talks to.
- Denial of service caused by the operator's own configuration (e.g. an
  aggressive check interval) or by upstream rate limiting.
- Findings that require a malicious operator (whoever runs the deployment
  already controls it).

## Operator Security Notes

If you deploy your own instance:

- **Never commit `.env`.** It is git-ignored; keep tokens, the optional
  Instagram session cookie, and the database URL out of version control.
- **Restrict the bot to trusted users** with `TELEGRAM_ADMIN_IDS`; an empty
  value allows anyone who finds the bot to control it.
- **Set `WEB_API_TOKEN`** if the HTTP API (`/sweep`, `/accounts/*/recheck`) is
  reachable from the internet, and set `TELEGRAM_WEBHOOK_SECRET` for webhook
  mode.
- **Keep dependencies current.** Dependabot alerts on this repo are addressed on
  `main`; rebuild after pulling fixes.
- The bot is designed to stay **100% login-free**; it never asks for your
  Instagram password, and you should never give it one.
