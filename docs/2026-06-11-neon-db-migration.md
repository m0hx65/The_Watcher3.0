# Migrating off Render's expiring free Postgres → Neon (free, no expiry)

**Why:** Render's free PostgreSQL (`watcher-db`) is suspended ~30 days after
creation and deleted after a grace period. Neon's free tier does **not** expire,
so moving there keeps the bot free *and* stops the monthly data-loss clock.

The migration uses `scripts/migrate_db.py`, which copies data through the app's
own SQLAlchemy models — **no `pg_dump`/`psql` install required**, just the
`sqlalchemy` + `asyncpg` already in `requirements.txt`.

> ⚠️ **Do step 1 first, today.** Once `watcher-db` is suspended it becomes
> inaccessible and the backup can't be taken.

---

## 1. Back up the current database now (safety net)

1. Render dashboard → **watcher-db** → **Connect** → copy the **External
   Database URL** (the external one — you're connecting from your laptop, not
   from inside Render). It looks like
   `postgresql://watcher:…@dpg-….frankfurt-postgres.render.com/watcher_xxxx`.
2. From the repo root:

   ```powershell
   python scripts/migrate_db.py --source "<EXTERNAL_DATABASE_URL>" --dump-json watcher-backup.json
   ```

   You'll see a per-table row count and `✅ Backup written: watcher-backup.json`.
   This file holds every account, snapshot, seen-story, highlight, and setting.
   It's git-ignored — keep it somewhere safe.

## 2. Create the free Neon database

1. Sign up at <https://neon.tech> (free, no card).
2. **Create project** → pick a region near your Render region (Frankfurt/EU is
   closest to `frankfurt`). A database named `neondb` is created.
3. **Connection string** → copy the **psql/asyncpg** string. It looks like
   `postgresql://user:pw@ep-….eu-central-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require`.
   Paste it verbatim — the bot normalizes the prefix and the `sslmode` /
   `channel_binding` params for asyncpg automatically.

## 3. Restore the backup into Neon

```powershell
python scripts/migrate_db.py --from-json watcher-backup.json --target "<NEON_URL>"
```

It creates the schema, inserts every row preserving ids, and resets the id
sequences. Expect `✅ Restore complete`.

*(Prefer one shot and skip the file? `--source "<RENDER_URL>" --target "<NEON_URL>"`
copies directly. The two-step route just leaves you an offline copy.)*

## 4. Point the bot at Neon

1. Render dashboard → **the-watcher** service → **Environment**.
2. Set **`DATABASE_URL`** to the Neon connection string from step 2. (If it was
   linked to `watcher-db` via the blueprint, replace it with this literal
   value.) Save — Render redeploys automatically.
3. Watch the deploy logs for `Database schema verified` and
   `The Watcher is online.`

## 5. Verify

- In Telegram, send `/status` — the account count should match what you had.
- Open `/list` — every monitored account is present.
- Trigger a sweep (**🔄 Sweep All**) and confirm a notification arrives.

## 6. Let `watcher-db` go

Once verified, the old Render database can expire/delete on its own — its data
now lives in Neon (and in `watcher-backup.json`). No upgrade, no cost.

---

### Notes

- **Neon auto-suspends on idle** and wakes on the next connection (a second or
  two). The engine's `pool_pre_ping=True` + `pool_recycle=1800` already handle
  the reconnect, so sweeps just work.
- `render.yaml` no longer declares a managed `databases:` block; `DATABASE_URL`
  is now `sync: false` (set in the dashboard). Re-applying the blueprint won't
  recreate an expiring Render DB.
- Re-running the restore against a non-empty target is refused unless you pass
  `--force`, so an accidental double-run can't duplicate rows.
