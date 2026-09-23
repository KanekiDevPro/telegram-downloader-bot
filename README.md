# Telegram Downloader Bot

Scalable Telegram bot that downloads media (YouTube, Twitter/X, Instagram, TikTok, Spotify, …)
via `yt-dlp` and uploads it back to the user. Python 3.11+ · Aiogram 3.x (async) ·
PostgreSQL (`asyncpg`) · Redis (queue + FSM state) · fully decoupled Gateway/Worker model.

One-line install (Debian/Ubuntu):

```bash
sudo bash -c "$(wget -qO- https://raw.githubusercontent.com/KanekiDevPro/telegram-downloader-bot/main/install.sh)"
```

## Features

- Downloads from YouTube, Twitter/X, Instagram, TikTok, Spotify, SoundCloud, Facebook,
  Reddit, Pinterest, Vimeo, Twitch, Dailymotion and dozens more — with an embedded
  Cobalt fallback for the moments a site refuses yt-dlp.
- Honest menus: video as 1080p/720p/480p ceilings (never upscaled), audio as
  **MP3 / M4A / OPUS / WAV** with quality presets (💎 best → 📦 small size), photo posts
  delivered as photos. The bot only offers what a link really has.
- One-message UI in Persian and English (chosen on first run): Home → Download /
  Profile / Help, every screen edited in place instead of message spam.
- Background queue and workers, with a smart cache that replays finished files instantly.
- Premium (VIP) with manual receipt payments and admin approval; daily quotas for
  free and VIP users.
- Admin dashboard (server-side authorized): Statistics, Users (paging + lookup),
  Broadcast (preview → explicit confirm → throttled send), Blocks, Trends,
  Recent failures, System health, Settings.
- YouTube survival kit: cookie login from a read-only jar (copy-on-write), automatic
  cookie export with admin alerts, Smart-TV OAuth login, PO-token provider and session
  helpers, optional WARP exit, and `/doctor` for a one-verdict diagnosis.
- Optional self-hosted Telegram Bot API server for uploads beyond the cloud API's 50 MB cap.

## Architecture

```
Telegram
   │  updates
   ▼
┌─────────────────┐   enqueue    ┌────────────┐   dequeue    ┌─────────────────────────────┐
│  GATEWAY (bot)  │────────────▶│ Redis list │─────────────▶│  WORKERS (N asyncio tasks) │
│  menus, intake  │  DownloadTask│  dl:tasks  │              │  extract → download → upload│
│  quota + cache  │              └────────────┘              │  cache file_id → reply     │
└─────────────────┘       ▲                                   └─────────────┬───────────────┘
      FSM state ◀── Redis │ reads users, plans, txns, smart_cache           │ writes
                          └───────────────────────────────── PostgreSQL ◀──┘
```

`core/` is configuration, database and shared helpers; `services/` is the pipeline
(extractor, queue, worker, delivery, cache, telemetry, doctor, cookie tooling,
fallback engine); `handlers/` is the UI layer; `tests/` is a self-contained unit
suite. Operational guides (installer, systemd/supervisor, the cookie pipeline, the
fallback engine) live in [deploy/README.md](deploy/README.md).

## Requirements

- Docker + Docker Compose — or, for a host install: Python 3.11+, PostgreSQL 16, Redis 7.
- `ffmpeg` on PATH (audio conversion and stream merging).
- `BOT_TOKEN` from @BotFather; `ADMIN_IDS` for the admin dashboard.
- `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` from https://my.telegram.org — only if you
  enable the local Bot API server (large uploads).

## Installation

```bash
git clone https://github.com/KanekiDevPro/telegram-downloader-bot.git
cd telegram-downloader-bot
cp .env.example .env
# → set BOT_TOKEN and ADMIN_IDS (comma-separated Telegram ids)
docker compose up -d
```

Host install (bot on the host, infrastructure in containers):

```bash
docker compose up -d postgres redis
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # → BOT_TOKEN, ADMIN_IDS
.venv/bin/python main.py
```

**YouTube cookies (optional but recommended).** Put a Netscape-format `cookies.txt`
(exported from a logged-in browser) at `cookies/cookies.txt`, or run
`python scripts/export_cookies.py`. The bot never writes the source file — yt-dlp gets
a writable copy — and its alerts tell admins when the login is missing or going stale.

## Configuration

Everything is environment-driven; the full annotated list is in
[.env.example](.env.example). The variables most deployments touch:

| Variable | Default | Purpose |
|---|---|---|
| `BOT_TOKEN` | — | Bot token (required) |
| `ADMIN_IDS` | — | Comma-separated Telegram ids: dashboard, broadcast, payment approvals |
| `DATABASE_URL` | local postgres | asyncpg connection string |
| `REDIS_URL` | local redis | queue + FSM storage |
| `WORKER_COUNT` | `2` | concurrent download workers |
| `MAX_FILE_SIZE_MB` | `2000` | per-file cap (silently capped to 50 MB on the cloud API) |
| `TELEGRAM_API_BASE_URL` | — | local Bot API server URL, e.g. `http://telegram-api:8081`; empty = cloud API |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | — | from https://my.telegram.org — required by the `telegram-api` container |
| `COOKIE_FILE` | `cookies.txt` | YouTube cookie jar; empty = disabled |
| `DEFAULT_DAILY_LIMIT` / `PREMIUM_DAILY_LIMIT` | `10` / `60` | daily quotas |
| `YTDLP_PROXY` | `http://warp:1080` | tunnel yt-dlp leaves through; empty = direct |
| `COBALT_API_URL` | `http://cobalt:9000` | fallback extractor instance; empty = off |
| `YTDLP_POT_PROVIDER_URL` | `http://pot-provider:4416` | PO-token helper; empty = off |
| `YOUTUBE_SESSION_SERVER` | `http://yt-session-generator:8080` | session helper for the fallback; empty = off |
| `BOT_MODE` / `WEBHOOK_PATH` | `polling` / `/webhook` | webhook mode needs a public HTTPS reverse proxy |
| `TIMEZONE` | `Asia/Tehran` | daily quota reset boundary |

> **Writing `.env`:** keep comments on their own lines — a trailing comment after an
> empty value (`TELEGRAM_API_ID=  # note`) is absorbed into the value by both dotenv
> and Docker Compose's parser.

## Run

```bash
docker compose up -d
```

Uploads over 50 MB need the self-hosted Bot API server (set `TELEGRAM_API_ID` /
`TELEGRAM_API_HASH` in `.env` first):

```bash
docker compose --profile local-api up -d
```

The YouTube helpers (`pot-provider`, `yt-session-generator`) and the `warp` tunnel are
part of the default stack; leave `warp` out to download from this host's own address.

## Development

```bash
pytest                  # unit tests — no Postgres/Redis needed
ruff check .
mypy .

python scripts/smoke.py       # integration smoke — needs Postgres + Redis
python scripts/boot_check.py  # offline wiring check — needs Postgres only
```

## Notes

- **"Sign in to confirm you're not a bot"** is YouTube's bot check, not a crash: fix the
  login first (`scripts/export_cookies.py`, `/fixlogin`), then the exit route (a
  residential `YTDLP_PROXY`, or the `warp` tunnel). `/doctor` names the actual cause.
- **Uploads over 50 MB** fail on the cloud Bot API regardless of `MAX_FILE_SIZE_MB` —
  run the local Bot API server for those.
- By design: no private links, no live streams, no playlists.
- Respect each platform's Terms of Service and copyright rules.

## License

No license has been published for this project yet — treat it as all rights reserved.
