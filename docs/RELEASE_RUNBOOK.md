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
`YTDLP_POT_PROVIDER_URL`, `YOUTUBE_SESSION_SERVER`, `TIMEZONE`, `BOT_MODE`,
`MENU_AUTO_BEST` (off by default — see §6 for what it changes).

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
CREATE TABLE IF NOT EXISTS bot_texts     (…);   -- PK (key, lang): admin text overrides
```

(Full definitions: `core/database.py`, top of file.) All changes are
**additive** — no table drops, no data resets, no required downtime. Old rows
carry NULL in the new `smart_cache` columns and replay safely.

`bot_texts` holds admin edits to user-facing texts (one row = one catalogue key
in one language). No row means the shipped default; a deploy that renames a key
ignores stale rows with one log line each. To reset every edited text without
the panel, `DELETE FROM bot_texts;` — that is exactly what the per-text reset
button does.

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
6. Admin panel — the hub is six categories (Users & groups, Downloads, Sources,
   Messages, System, Diagnostics); every submenu has Back and Home. The
   **recent failures** / **failure trends** buttons are gone; a stale client
   tapping the old callback data gets a short stale-menu answer, never an
   error. Try it from an out-of-date open keyboard to confirm.
7. Admin panel → **Messages → Bot texts** — open a text, edit it, read the
   preview, save; the next user-facing message uses the edit. Reset returns the
   default immediately. A locked key (operator/command texts) and invalid
   markup are refused with a reason, not saved.
8. Send a link whose qualities cannot be discovered — the user gets the
   explicit "not discovered" message with a retry, and **no** download starts
   on its own (`MENU_AUTO_BEST` is off by default; if you enable it, the extra
   row must read as an automatic pick).
9. Run the tracked manual gate: [LIVE_TAP_CHECKLIST.md](LIVE_TAP_CHECKLIST.md)
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
- **Missing or empty produced file, or a container with no media streams** →
  hard failure before upload (`output: the produced file is missing or empty`,
  `streams: …`); the user gets the ordinary failure text, nothing is sent.
- **Selection vs delivery** — the delivered video height is checked against the
  *user's chosen rung* as well as the caption's claim. A file that came back at
  a different height than the one selected fails with `CONVERSION_MISMATCH`
  instead of being sent with an apologetic caption.
- **Source-quality upscale** (a 320 kbps target over a ≈130 kbps source, a
  rescaled rung) → logged as an INFO observation
  (`source-quality upscale observed for …`) — a diagnosis aid, never a failure
  and never user-facing. The bitrate window tolerance is `verify.BITRATE_TOLERANCE`.
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
| `source-quality upscale observed for …` | output above the source's own quality — informational only |
| `text override for … ignored` | a `bot_texts` row no longer validates (or its key is gone) — the default is used |
| `login-shaped block … told N admin(s)` | cookie jar needs attention |
| `could not publish a text change …` | Redis unreachable for a text edit — peers pick it up within the 30 s TTL |
| `could not reload text overrides …` | database unreachable at a refresh — the last known texts stay live |

## 9. Deployment compatibility & post-deploy commands

**Schema before traffic — yes.** `main.build_app()` runs `init_db()` (the
idempotent DDL) and loads the `bot_texts` overrides *before* the dispatcher
starts polling (or registers the webhook). A migration failure is therefore a
boot failure: no handler ever accepts an update against an un-migrated
database. There is no window where old schema serves new code.

**All workers restart with the process.** The gateway, the N download workers
and the maintenance loop are asyncio tasks inside **one** Python process
(`deploy/run.sh` → `python main.py`), so one restart restarts all of them:

| Install | Command |
|---|---|
| Docker | `docker compose build bot && docker compose up -d --no-deps bot` |
| systemd | `sudo systemctl restart telegram-downloader-bot` (`Restart=always`, SIGTERM drain, 30 s stop timeout — in-flight tasks are re-queued) |
| supervisor | `sudo supervisorctl restart telegram-downloader-bot` |

Several bot *processes* (replicas) also stay coherent without restarts: text
edits propagate through one Redis counter (`bot:texts:version`) and, when Redis
is unreachable, within the bounded 30 s TTL re-read of `bot_texts` — the
database is the source of truth throughout (see `core/texts.py`).

**Config defaults on upgrade.** `MENU_AUTO_BEST` is absent from old `.env`
files and defaults to **false** (pinned by `tests/test_config.py`): the honest
"not discovered + retry" screen is what users get until an operator opts in.

**ffmpeg / ffprobe on the server.** `deploy/install.sh` **hard-gates both**
before it installs anything and refuses to continue with an actionable message
(the package that provides both, per distro) when either binary is missing —
the gate is pinned by `tests/test_installer_gate.py`. At runtime
`services/verify.py` additionally logs `delivery verification unavailable (…)`
once per process if ffprobe ever disappears, and the post-deploy command below
re-checks the live toolchain.

**Post-deploy commands, in order:**

```bash
# 1. restart (one of the three rows above)

# 2. clean start?
docker compose logs -f bot        # or: journalctl -u telegram-downloader-bot -f
#    expect: "worker N started", "maintenance loop started", no tracebacks

# 3. schema + wiring against the live database
python scripts/boot_check.py

# 4. integration smoke (needs Postgres + Redis)
python scripts/smoke.py

# 5. the delivery-verification toolchain, on the server
ffmpeg -version | head -1 && ffprobe -version | head -1
```

**Health checks in Telegram** (a test account + an admin account):

1. `/start` — language picker (first run) or Home.
2. One video link with several qualities — the ladder matches the link.
3. A link whose qualities cannot be discovered — the explicit retry screen,
   **no** silent download (`MENU_AUTO_BEST` off).
4. Admin → **Messages → Bot texts** — edit one text, watch the next message use
   it (in a *second* worker/replica too, within seconds — no restart), then
   Reset.
5. `/doctor` (admin) — one verdict, including the helper/toolchain rows.
6. A stale keyboard tap (a menu left over from before the deploy) — a short
   stale-menu answer, never an error.
