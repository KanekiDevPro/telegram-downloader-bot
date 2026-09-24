# Rollback Plan

Three independent rollback axes — **code**, **database**, **config** — because
they fail at different speeds and must not be confused. The release's database
changes are purely additive, which makes the database axis almost a no-op.

## 0. Principles

- The bot is stateless; all durable state is PostgreSQL (users, cache,
  telemetry) and Redis (queue + FSM). Rolling back code never requires touching
  the database.
- The new schema is **additive** and every new column is **nullable with a
  default-safe semantics** (except `users.language`, which has a default). Code
  from the previous release ignores the new columns/tables entirely.
- Never "fix" a rollback by dropping tables — see §2.

## 1. Code rollback

Docker:

```bash
git revert <release-commit>          # or check out the previous known-good ref
docker compose build bot
docker compose up -d --no-deps bot
```

Host install: check out the previous ref, `pip install -r requirements.txt`
(only if requirements changed), restart the service
(`deploy/telegram-downloader-bot.service` / supervisor).

Then verify: `docker compose logs -f bot` shows a clean start, one test link
downloads, and `scripts/boot_check.py` passes against the live database (it is
schema-tolerant both directions).

Interrupted jobs: workers re-queue in-flight tasks on shutdown, so a task
enqueued by the new release is processed by the old code — old task payloads
carry the same fields (new payload fields are optional and ignored).

## 2. Database migration rollback

**Default answer: no action needed.** Nothing in this release is destructive;
the previous release's code runs unchanged against the new schema:

| Change | Old code's behaviour after rollback |
|---|---|
| `users.language` | ignored (column has a default) |
| `smart_cache.kind / title / label` | ignored; old code reads `SELECT *` and uses only the columns it knows |
| `block_events`, `bot_state`, `fix_events`, `helper_events` | untouched and unused by old code — telemetry rows preserved |
| `group_downloads` | unused by old code — group analytics history preserved |
| `bot_texts` | unused by old code — admin text edits are preserved but not applied; old code ships the catalogue defaults |
| new indexes | harmless |

**Only if** you are certain the rollback is permanent *and* want the schema
exactly as before, run this cleanup manually (optional, off by default):

```sql
DROP TABLE IF EXISTS bot_texts;
DROP TABLE IF EXISTS group_downloads;
DROP TABLE IF EXISTS helper_events;
DROP TABLE IF EXISTS fix_events;
DROP TABLE IF EXISTS bot_state;
DROP TABLE IF EXISTS block_events;
ALTER TABLE smart_cache DROP COLUMN IF EXISTS label;
ALTER TABLE smart_cache DROP COLUMN IF EXISTS title;
ALTER TABLE smart_cache DROP COLUMN IF EXISTS kind;
ALTER TABLE users      DROP COLUMN IF EXISTS language;
```

Consequences of the optional cleanup (read before running):

- **`smart_cache` rows survive** (good — instant replays keep working), but
  older rows lose their stored captions' `title`/`label` facts permanently.
- `users.language` dropping **loses every user's language choice** — usually a
  bad trade. Prefer leaving it.
- Telemetry history (blocks, fixes, helper states, group analytics) is gone.
- Dropping `bot_texts` **loses every admin text edit** (a global reset to the
  shipped defaults). To keep the edits but stop applying them, skip this DROP.

There is no "down migration" in the codebase on purpose: schema direction is
forward-only, and this table is the rollback.

## 3. Environment / config rollback

Config-only issues need no code rollback — edit `.env` and restart:

| Symptom | Config rollback |
|---|---|
| Large uploads failing after enabling the local API | clear `TELEGRAM_API_BASE_URL`, set `TELEGRAM_API_LOCAL=false`, restart (back to cloud API, 50 MB cap) |
| Local API misbehaving | `docker compose --profile local-api down`, revert to cloud API as above |
| Upload ceiling wrong for the transport | adjust `MAX_FILE_SIZE_MB` (the bot clamps to what the transport enforces) |
| Bad proxy/tunnel behaviour | clear `YTDLP_PROXY` (direct egress) or fix `warp` |
| Helper flapping | clear `YTDLP_POT_PROVIDER_URL` / `YOUTUBE_SESSION_SERVER` to disable those routes |
| Wrong quota behaviour | `DEFAULT_DAILY_LIMIT` / `PREMIUM_DAILY_LIMIT` |
| Wrong daily-reset timezone | `TIMEZONE` (also drives the analytics week boundaries) |
| Automatic "best available" row unwanted after enabling it | clear `MENU_AUTO_BEST` (or set it to `0`), restart — empty quality lookups go back to the explicit retry message |
| An admin text edit reads wrong or broke a send | admin panel → Messages → Bot texts → Reset — or `DELETE FROM bot_texts WHERE key = '<key>';` (defaults take over on the next save/restart) |

Secrets rotation (token/cookie compromise) is not a rollback: rotate
`BOT_TOKEN` or re-export the cookie jar (`scripts/export_cookies.py`), restart.

## 4. Recommended order

1. **Config first** (seconds, no rebuild).
2. **Code** if the fault is in behaviour (minutes, rebuild + restart).
3. **Database cleanup last**, only after the code rollback has settled and only
   with a fresh `pg_dump` in hand:

```bash
docker compose exec postgres pg_dump -U postgres <db> > backup-$(date +%F).sql
```

## 5. Verification after any rollback

- `docker compose logs -f bot` — clean start, no tracebacks.
- One video link + one Spotify link end-to-end.
- One cached replay of a link downloaded *before* the rollback.
- Admin panel opens (all six category submenus render; Bot texts lists the
  defaults and any edits).
- If the DB was touched: `scripts/smoke.py`.
