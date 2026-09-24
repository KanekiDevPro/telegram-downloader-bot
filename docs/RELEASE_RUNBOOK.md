# Production Release Runbook

Everything an operator needs to take this release live and confirm it works.
The changelog is [../CHANGELOG.md](../CHANGELOG.md); rollback paths are
[ROLLBACK.md](ROLLBACK.md); the manual release gate is
[LIVE_TAP_CHECKLIST.md](LIVE_TAP_CHECKLIST.md).

## 1. Requirements

- Docker + Docker Compose (or a host install: Python 3.11+, PostgreSQL 16, Redis 7).
- `ffmpeg` **and** `ffprobe` on PATH inside the bot container/image (`ffprobe`
  powers delivery verification — see §7 for the policy when it is absent).
- Outbound network to Telegram and to the source platforms.

## 2. Secrets & environment

Configured via `.env` (never committed — `.gitignore` covers `.env`,
`cookies.txt`, `cobalt/`, `*.session`). The full annotated list is
[../.env.example](../.env.example). Required before first boot:

| Variable | Secret? | Notes |
|---|---|---|
| `BOT_TOKEN` | **yes** | from @BotFather |
| `ADMIN_IDS` | sensitive | comma-separated Telegram user ids |
| `DATABASE_URL` | **yes** (credentials) | asyncpg DSN |
| `REDIS_URL` | possibly | queue + FSM storage |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | **yes** | from https://my.telegram.org — only for the local Bot API server |
| `COOKIE_FILE` / `cookies.txt` | **yes** (a login) | Netscape cookie jar, read-only source; yt-dlp gets a copy |
| `COBALT_API_KEY` | **yes** | if the fallback instance requires one |
| `MANUAL_CARD_NUMBER` / `MANUAL_CARD_HOLDER` | sensitive | payment info |

Behaviour-affecting (non-secret) knobs: `WORKER_COUNT`, `MAX_FILE_SIZE_MB`,
`TELEGRAM_API_BASE_URL` + `TELEGRAM_API_LOCAL`, `DEFAULT_DAILY_LIMIT` /
`PREMIUM_DAILY_LIMIT`, `YTDLP_PROXY`, `COBALT_API_URL`,
`YTDLP_POT_PROVIDER_URL`, `YOUTUBE_SESSION_SERVER`, `TIMEZONE`, `BOT_MODE`.

> `.env` parsing: keep comments on their own lines — a trailing comment after an
> empty value is absorbed into the value by both dotenv and Compose.

## 3. Deployment order

```bash
# 1. Infrastructure first
docker compose up -d postgres redis

# 2. Build the bot image for this release
docker compose build bot

# 3. Start the bot (schema bootstrap + migrations run automatically at boot)
docker compose up -d bot

# 4. The supporting stack (YouTube helpers + tunnel) and optional fallback
docker compose up -d pot-provider yt-session-generator warp cobalt

# 5. Optional: self-hosted Bot API server for uploads over 50 MB
#    (requires TELEGRAM_API_ID / TELEGRAM_API_HASH in .env first)
docker compose --profile local-api up -d
```

Rolling upgrade of a running deployment: step 2, then
`docker compose up -d --no-deps bot` (the bot is stateless; the queue in Redis
survives and interrupted tasks are re-queued on shutdown).

## 4. Database / schema — what actually runs

There are **no migration files**. The schema is idempotent DDL in
`core/database.py` (`SCHEMA_SQL`), executed by `init_db()` on every startup —
safe to run repeatedly. This release's changes, exactly as applied:

```sql
ALTER TABLE users      ADD COLUMN IF NOT EXISTS language TEXT NOT NULL DEFAULT 'en';
ALTER TABLE smart_cache ADD COLUMN IF NOT EXISTS kind  TEXT;
ALTER TABLE smart_cache ADD COLUMN IF NOT EXISTS title TEXT;
ALTER TABLE smart_cache ADD COLUMN IF NOT EXISTS label TEXT;
CREATE TABLE IF NOT EXISTS block_events  (…);   -- + ix_block_events_created_at, ix_block_events_cause
CREATE TABLE IF NOT EXISTS bot_state     (…);
CREATE TABLE IF NOT EXISTS fix_events    (…);   -- + ix_fix_events_created_at, ix_fix_events_kind
CREATE TABLE IF NOT EXISTS helper_events (…);   -- + ix_helper_events_created_at, ix_helper_events_helper
CREATE TABLE IF NOT EXISTS group_downloads (…); -- + ix_group_downloads_created_at, ix_group_downloads_chat_id
```

(Full definitions: `core/database.py`, top of file.) All changes are
**additive** — no table drops, no data resets, no required downtime. Old rows
carry NULL in the new `smart_cache` columns and replay safely.

To confirm the schema after boot:

```bash
docker compose exec postgres psql -U postgres -d <db> -c "\d smart_cache" -c "\d group_downloads"
```

## 5. Smoke tests

```bash
.venv/bin/pytest            # full suite (no Postgres/Redis needed)
.venv/bin/ruff check .
.venv/bin/mypy .
python scripts/boot_check.py  # app wiring + schema bootstrap against real Postgres
python scripts/smoke.py       # integration smoke — needs Postgres + Redis
```

`boot_check.py`'s known environmental failure: a check FAILs when the
`yt-session-generator` session server is not running on the box — expected
unless that helper is up.

## 6. Post-deploy verification

1. `docker compose logs -f bot` — expect `worker N started`, `maintenance loop
   started`, no tracebacks.
2. Send `/start` from a test account — language picker (first run) or Home.
3. One real link per shape (video + Spotify audio) — the file arrives with the
   media card; the log shows a `stages probe=… download=… upload=…` line per job.
4. Admin panel → **👥 Groups** — totals render; after a group download the
   numbers move and the week-over-week block appears.
5. Re-send a previously downloaded link — instant cached replay with the same
   caption.
6. Run the tracked manual gate: [LIVE_TAP_CHECKLIST.md](LIVE_TAP_CHECKLIST.md)
   (sections A and B, on a real device).

**Success criteria:** all checklist rows PASS (or explicitly BLOCKED with a
reason), no unexpected errors in the bot log for 24 h, admin panels render.

## 7. Delivery verification — operational notes

`services/verify.py` checks every produced media file against its caption
before upload. Policy, to be explicit:

- **Measured contradiction** (wrong codec for the container, bitrate far below
  the claimed rate, wrong video height) → the delivery fails with
  `CONVERSION_MISMATCH`; the user is told to pick another option; the detail is
  logged, never shown to the user.
- **ffprobe missing / timing out / answering garbage** → delivery proceeds
  exactly as previous releases did, and the log records
  `delivery verification unavailable (…)` **once per process**. If you see that
  warning in production, install ffprobe in the image — verification is a
  promise you want on.

## 8. What to watch in the logs

| Log line | Meaning |
|---|---|
| `stages probe=… download=… upload=… total=…` | per-job timing breakdown |
| `delivery verification failed for …` | a produced file contradicted its caption — investigate the encoder path |
| `delivery verification unavailable (…)` | ffprobe absent/broken on this host |
| `login-shaped block … told N admin(s)` | cookie jar needs attention |
