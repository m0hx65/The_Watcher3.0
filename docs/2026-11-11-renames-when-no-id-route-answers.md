# 2026-11-11 — A renamed target, and no route left that takes an id

## What the owner saw

```
@8dvuu
Instagram ID: <its numeric id, on file>
State: 🟢 active
Last check: 2026-11-11 10:39:02 AM · HTTP 401
Consecutive failures: 9
Latest snapshot (captured 2026-10-30 05:08:17 PM)
```

The account had changed its username. The bot keeps the numeric id for
exactly this — the id survives a rename — and had found renamed targets
through it before. This time it found nothing, said nothing, and its card
called the account active.

## What was actually happening

Measured 2026-11-11, for the target's id and for `@instagram`'s own
(`25025320`), from a VPN exit and through the Worker:

| Route | Asked directly | Through the Worker |
|---|---|---|
| graphql reel query `?query_id=9957820854288654&user_id=<id>` | **401** `"require_login": true, "igweb_rollout": true` | **401** `upstream blocked after retries` (10.0 s) |
| the same query by legacy `query_hash` | 401, same body | — |
| mobile `i.instagram.com/api/v1/users/<id>/info/` | **403** `"something went wrong"` | **401** `upstream blocked after retries` (9.4 s) |
| `www.instagram.com/api/v1/users/<id>/info/` | redirect to `/accounts/login/` | — |

The same answers for a brand's id as for the target's: this is not about
the account. Since 2026-11-04 **no route answers an anonymous lookup by
numeric id.**

Four things in the bot turned that into a silent, permanent failure:

1. **One route, no second.** `probe_by_id` asked the reel query and nothing
   else. `users/<id>/info` was in the client — as a fill-in for a reel answer
   that had already arrived (`_fill_probe`). The one route that could have
   answered when the reel query was walled was unreachable exactly then.
2. **The old username's page read as a block.** Instagram serves its
   "Profile isn't available" page with HTTP 200; until the fix for that
   (committed 2026-11-11 12:42, two hours after the last check above) it was
   booked as "no profile payload — login wall", i.e. a 401. A 401 writes no
   snapshot and sends no alert — rightly, for a block — so nine checks failed
   without a word. With that fix the account reads "not found" and is
   announced on the second miss; it still could not be followed.
3. **"Active" meant "not paused".** The card's State line never looked at
   whether checks were succeeding.
4. **The obvious workaround made it worse.** `/add newname` created a second
   entry; its first reading stored the same Instagram ID on it; the first
   entry kept the history and went on failing.

And one hole next to it, found while tracing the path: a reading under the
stored username was accepted **whatever id it carried**. A target that
renames frees its handle; whoever takes it — the target itself, on a fresh
account, is the obvious case — would have been read as the target. Their
numbers would be diffed against the target's history and announced as
changes, and the story phase, which works by username, would deliver their
stories.

## What changed

**The second id route is asked on its own** (`probe_by_id(user_info=True)`).
When the reel query gives no answer the client goes on to `users/<id>/info`:
through the Worker, then directly; with `IG_SESSION_COOKIE` set, directly
only, with the cookie, never through the Worker. An answer is used only when
its own `pk` is the id that was asked and it carries a valid handle. Its 404
is not "id gone" — a route that refuses anonymous callers cannot say that;
only the reel query's own 404 can. It has its own breaker (three refusals,
ten minutes): a sweep knocks once per cooldown, a manual check asks it only
after its username side has failed, so a Recheck of a healthy account costs
what it did before. `/add <numeric id>` uses both routes too.

**Identity is the id** (`MonitorService._foreign_owner`). A reading whose id
differs from the stored one is not this account: nothing from it is stored,
the check fails as "this username now belongs to a different Instagram
account", the id routes are asked where the account went, and a sweep skips
that account's story phase.

**A lost username is said to be lost.** `identity_lost:<id>` records since
when, why (`id_blocked`, `id_gone`, `no_id`, `foreign`) and what each id route
answered. The card:

```
State: 🔴 active — username gone, current one not found
Last check: … · HTTP 404
Consecutive failures: 9
Since …: Instagram has no profile under @8dvuu.
Its current username could not be read from the ID: Instagram refused the
lookup (reel query: HTTP 401; user info: HTTP 403). Renamed, deactivated or
deleted cannot be told apart until it answers; it is asked again on every check.
Know the new username? Send /add newname — it is matched to this entry by its
Instagram ID, so the history stays and nothing is duplicated.
```

A plain block reads `⚠️ active — not answering (9 checks failed in a row)`,
and `🟢 active` is kept for an account that is answering. A 401/403 still
never alerts as deleted or renamed.

**The new username relinks the old entry** (`_relink_to_known_entry`). Every
profile read by name carries the id, so `/add newname` works while no id
route does: the entry that holds the id takes the new username and keeps its
row, id, snapshots, topic and settings; the entry just added is removed; one
rename message says it was confirmed by the id on the new profile. An entry
with history of its own is never merged, and an id is never written to a
second entry. A rename that finds the new name already added as an empty
entry folds that entry in; one that finds it held by a real, different entry
says so once instead of on every check.

## What the owner sees now

Until an id route answers, on the second sweep after the rename:

```
❓ @8dvuu — Instagram says this username doesn't exist (2 checks in a row).
The Instagram ID lookup was blocked (reel query: HTTP 401; user info: HTTP 403),
so a rename couldn't be confirmed this time — it is re-tried on every check.
Know the new username? Send /add newname — it is matched to this entry by its
Instagram ID, so the history stays and nothing is duplicated.
```

After `/add newname` (or the moment either id route answers):

```
🔁 @8dvuu changed username → @newname
Confirmed by the Instagram ID on @newname's own profile — same account,
followed under its new name from now on, with its history.
```

## What is still not possible anonymously

Finding the new username **from the id alone**. Both id routes are refused
to anonymous callers from every network measured; the bot now asks both, once
per sweep, and recovers on its own the moment either answers. Whether a home
line still gets `users/<id>/info` (it did on 2026-09-05) was not measured —
the machine this was written on leaves through a VPN exit Instagram sends to
its login page — and the home fetcher has no job for it yet. With a logged-in
session (`IG_SESSION_COOKIE`) `users/<id>/info` is the route the HD-avatar
lookup already relies on, so it is expected to answer; that was not measured
either (no session was used for any of this), and the bot stays login-free
by default — that stays the owner's call.

Two entries that ALREADY share an id — an account re-added under its new name
before this fix — are not merged: each has history, and joining two histories
is not something to do behind the owner's back. The failing one's alert and
card name the entry that holds the same id, so the stale one can be removed.
