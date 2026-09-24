# Telegram Downloader Bot

A scalable Telegram bot that downloads media (YouTube, Twitter/X, Instagram, TikTok, Spotify, SoundCloud, Reddit, …) via `yt-dlp` and delivers it back to the user. Python 3.11+ · Aiogram 3.x (async) · PostgreSQL (`asyncpg`) · Redis (queue + FSM) · a fully decoupled Gateway/Worker model.

One-line install (Debian/Ubuntu):

```bash
sudo bash -c "$(wget -qO- https://raw.githubusercontent.com/KanekiDevPro/telegram-downloader-bot/main/install.sh)"
```

## Features

- **Honest media menus.** Video rows show only the resolutions the link really has (480p → 2160p (4K), named 2K/4K when applicable) with an estimated size per row; audio rows show real bitrates with sizes, capped by the source's own quality — no "best", no fake upscales.
- **Source-aware audio formats** — MP3 / M4A / OPUS / FLAC / WAV, offered only when this source and the active transport can actually produce and deliver them.
- **Delivery verification.** Before upload, `ffprobe` measures the produced file; a caption that would disagree with the real codec/bitrate/resolution is refused instead of sent.
- **One-message UI** in Persian and English (chosen on first run): Home → Download / Profile / Admin, every screen edited in place.
- **Group support** — add the bot to a group, send a link, get the same compact flow without chatter; admin analytics track group usage with week-over-week trends.
- Background queue and workers, with a smart cache that replays finished files instantly (same caption as a fresh send).
- Premium (VIP) with manual receipt payments and admin approval; daily quotas for free and VIP users.
- Admin dashboard (server-side authorized): Statistics, Users, Broadcast, Blocks, Trends, Failures, Groups, System, Settings.
- YouTube survival kit: cookie login from a read-only jar (copy-on-write), automatic cookie export with admin alerts, Smart-TV OAuth login, PO-token and session helpers, optional WARP exit, and `/doctor` for a one-verdict diagnosis.
- Optional self-hosted Telegram Bot API server for uploads beyond the cloud API's 50 MB cap, with transport-aware file limits.

## Supported media

Video and audio from dozens of platforms (YouTube, Twitter/X, Instagram, TikTok, Facebook, Reddit, Pinterest, Vimeo, Twitch, Dailymotion, Spotify, SoundCloud, …), photo posts delivered as photos, and a Cobalt fallback for the moments a site refuses yt-dlp. By design: no private links, no live streams, no playlists. The bot only offers what a link really contains.

## Architecture

```
Telegram
   │  updates
   ▼
┌─────────────────┐   enqueue    ┌────────────┐   dequeue    ┌─────────────────────────────┐
│  GATEWAY (bot)  │────────────▶│ Redis list │─────────────▶│  WORKERS (N asyncio tasks) │
│  menus, intake  │  DownloadTask│  dl:tasks  │              │  extract → download → upload│
│  quota + cache  │              └────────────┘              │  verify file → send media   │
└─────────────────┘       ▲                                   └─────────────┬───────────────┘
      FSM state ◀── Redis │ reads users, plans, txns, smart_cache           │ writes
                          └───────────────────────────────── PostgreSQL ◀──┘
```

`core/` is configuration, database and shared helpers; `services/` is the pipeline (extractor, fallback, queue, worker, delivery, verification, cache, telemetry, cookie tooling); `handlers/` is the UI layer; `tests/` is a self-contained unit suite.

## Requirements

- Docker + Docker Compose — or, for a host install: Python 3.11+, PostgreSQL 16, Redis 7.
- `ffmpeg` and `ffprobe` on PATH (conversion, stream merging and delivery verification).
- `BOT_TOKEN` from @BotFather; `ADMIN_IDS` for the admin dashboard.
- `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` from https://my.telegram.org — only if you enable the local Bot API server (large uploads).

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

**YouTube cookies (optional but recommended).** Put a Netscape-format `cookies.txt` at `cookies/cookies.txt`, or run `python scripts/export_cookies.py`. The bot never writes the source file — yt-dlp gets a writable copy — and admins are alerted when the login is missing or going stale.

## Configuration

Everything is environment-driven; the full annotated list is in [.env.example](.env.example). The variables most deployments touch:

| Variable | Default | Purpose |
|---|---|---|
| `BOT_TOKEN` | — | Bot token (required) |
| `ADMIN_IDS` | — | Comma-separated Telegram ids: dashboard, broadcast, payment approvals |
| `DATABASE_URL` | local postgres | asyncpg connection string |
| `REDIS_URL` | local redis | queue + FSM storage |
| `WORKER_COUNT` | `2` | concurrent download workers |
| `MAX_FILE_SIZE_MB` | `2000` | per-file cap (silently capped to the active transport's limit) |
| `TELEGRAM_API_BASE_URL` | — | local Bot API server URL, e.g. `http://telegram-api:8081`; empty = cloud API |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | — | from https://my.telegram.org — required by the `telegram-api` container |
| `COOKIE_FILE` | `cookies.txt` | YouTube cookie jar; empty = disabled |
| `DEFAULT_DAILY_LIMIT` / `PREMIUM_DAILY_LIMIT` | `10` / `60` | daily quotas |
| `YTDLP_PROXY` | `http://warp:1080` | tunnel yt-dlp leaves through; empty = direct |
| `COBALT_API_URL` | `http://cobalt:9000` | fallback extractor instance; empty = off |
| `YTDLP_POT_PROVIDER_URL` | `http://pot-provider:4416` | PO-token helper; empty = off |
| `YOUTUBE_SESSION_SERVER` | `http://yt-session-generator:8080` | session helper for the fallback; empty = off |
| `BOT_MODE` / `WEBHOOK_PATH` | `polling` / `/webhook` | webhook mode needs a public HTTPS reverse proxy |
| `TIMEZONE` | `Asia/Tehran` | daily quota reset and analytics week boundaries |

> **Writing `.env`:** keep comments on their own lines — a trailing comment after an empty value (`TELEGRAM_API_ID=  # note`) is absorbed into the value by both dotenv and Docker Compose's parser.

## Running

```bash
docker compose up -d
```

Uploads over 50 MB need the self-hosted Bot API server (set `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` in `.env` first):

```bash
docker compose --profile local-api up -d
```

The YouTube helpers (`pot-provider`, `yt-session-generator`) and the `warp` tunnel are part of the default stack; leave `warp` out to download from this host's own address.

## Development & testing

```bash
pytest                  # unit tests — no Postgres/Redis needed
ruff check .
mypy .

python scripts/smoke.py       # integration smoke — needs Postgres + Redis
python scripts/boot_check.py  # offline wiring check — needs Postgres only
```

## Production notes

- Deployment order, secrets, schema changes and smoke tests: [docs/RELEASE_RUNBOOK.md](docs/RELEASE_RUNBOOK.md).
- Rollback (code / database / config, kept separate): [docs/ROLLBACK.md](docs/ROLLBACK.md).
- Manual release gate (live taps, PASS/FAIL tracking): [docs/LIVE_TAP_CHECKLIST.md](docs/LIVE_TAP_CHECKLIST.md).
- Changelog: [CHANGELOG.md](CHANGELOG.md). Operations detail (installer, systemd/supervisor, cookie pipeline, fallback engine): [deploy/README.md](deploy/README.md).
- **"Sign in to confirm you're not a bot"** is YouTube's bot check, not a crash: fix the login first (`scripts/export_cookies.py`, `/fixlogin`), then the exit route (a residential `YTDLP_PROXY`, or the `warp` tunnel). `/doctor` names the actual cause.
- Respect each platform's Terms of Service and copyright rules.

## License

No license has been published for this project yet — treat it as all rights reserved.

---
---

# ربات دانلود تلگرام

ربات مقیاس‌پذیر تلگرام برای دانلود رسانه (یوتیوب، توییتر/ایکس، اینستاگرام، تیک‌تاک، اسپاتیفای، ساوندکلاد، ردیت و…) با `yt-dlp` و تحویل فایل به کاربر. پایتون ۳٫۱۱+ · Aiogram 3.x (غیرهمزمان) · PostgreSQL · Redis (صف + وضعیت FSM) · معماری کاملاً جداشدهٔ Gateway/Worker.

نصب تک‌دستوری (دبیان/اوبونتو):

```bash
sudo bash -c "$(wget -qO- https://raw.githubusercontent.com/KanekiDevPro/telegram-downloader-bot/main/install.sh)"
```

## امکانات

- **منوهای صادقانه.** ردیف‌های ویدیو فقط کیفیت‌هایی را نشان می‌دهند که واقعاً در لینک هستند (۴۸۰p تا ۲۱۶۰p (4K)) همراه با حجم تخمینی هر ردیف؛ ردیف‌های صدا بیت‌ریت واقعی با حجم تخمینی نشان می‌دهند و از کیفیت خود منبع بالاتر نمی‌روند — نه «بهترین کیفیت»، نه کیفیت ساختگی.
- **فرمت‌های صدای وابسته به منبع** — MP3 / M4A / OPUS / FLAC / WAV، فقط وقتی نمایش داده می‌شوند که همین منبع و مسیر انتقال فعال واقعاً بتواند آن‌ها را بسازد و تحویل دهد.
- **تأیید هنگام تحویل.** پیش از آپلود، `ffprobe` فایل ساخته‌شده را اندازه می‌گیرد؛ اگر کپشن با کدک/بیت‌ریت/رزولوشن واقعی نخواند، به‌جای ارسالِ گمراه‌کننده رد می‌شود.
- **رابط تک‌پیامی** به فارسی و انگلیسی (انتخاب در اولین ورود): خانه ← دانلود / پروفایل / ادمین، و هر صفحه به‌جای پیام جدید ویرایش می‌شود.
- **کار در گروه** — ربات را به گروه اضافه کنید، لینک بفرستید و بدون شلوغ‌کاری همان فلوی فشرده را بگیرید؛ آمار گروه‌ها با روند هفته به هفته در پنل ادمین.
- صف پس‌زمینه و ورکرها، با کش هوشمند که فایل‌های آماده را آنی دوباره می‌فرستد (همان کپشنِ ارسال تازه).
- اشتراک VIP با پرداخت رسید دستی و تأیید ادمین؛ سهمیهٔ روزانه برای کاربران عادی و VIP.
- داشبورد ادمین (مجوز سمت سرور): آمار، کاربران، اطلاع‌رسانی، مسدودها، روندها، خطاها، گروه‌ها، سیستم، تنظیمات.
- جعبه‌ابزار بقای یوتیوب: لاگین کوکی از فایل فقط‌خواندنی (کپی هنگام نوشتن)، خروجی خودکار کوکی با هشدار به ادمین، لاگین OAuth تلویزیونی، ابزارهای PO-token و session، خروجی WARP اختیاری و دستور `/doctor` برای تشخیص یک‌جوابه.
- سرور Bot API شخصی اختیاری برای آپلودهای بالای سقف ۵۰ مگابایتِ API ابری، با سقف فایل هوشمند نسبت به مسیر انتفال.

## رسانه‌های پشتیبانی‌شده

ویدیو و صدا از ده‌ها پلتفرم (یوتیوب، توییتر/ایکس، اینستاگرام، تیک‌تاک، فیسبوک، ردیت، پینترست، ویمئو، توییچ، دیلی‌موشن، اسپاتیفای، ساوندکلاد و…)، پست‌های عکس به‌صورت عکس، و موتور جایگزین Cobalt برای وقتی که سایتی `yt-dlp` را پس می‌زند. طبق طراحی: نه لینک خصوصی، نه استریم زنده، نه پلی‌لیست. ربات فقط چیزی را پیشنهاد می‌دهد که لینک واقعاً دارد.

## معماری

همان نمودار بالا: دریافت آپدیت از تلگرام توسط Gateway، صف در Redis، ورکرهای غیرهمزمان برای استخراج ← دانلود ← تأیید فایل ← آپلود، و PostgreSQL برای کاربران، اشتراک‌ها، تراکنش‌ها و کش.

`core/` تنظیمات، دیتابیس و ابزارهای مشترک است؛ `services/` خط لولهٔ پردازش (استخراج، موتور جایگزین، صف، ورکر، تحویل، تأیید، کش، تله‌מטרی، ابزار کوکی)؛ `handlers/` لایهٔ رابط کاربری؛ و `tests/` مجموعه‌تست مستقل.

## پیش‌نیازها

- Docker + Docker Compose — یا برای نصب روی سیستم: پایتون ۳٫۱۱+، PostgreSQL 16، Redis 7.
- `ffmpeg` و `ffprobe` در مسیر PATH (تبدیل، ادغام استریم و تأیید تحویل).
- `BOT_TOKEN` از @BotFather و `ADMIN_IDS` برای داشبورد ادمین.
- `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` از https://my.telegram.org — فقط اگر سرور Bot API شخصی (آپلود حجیم) را فعال می‌کنید.

## نصب

```bash
git clone https://github.com/KanekiDevPro/telegram-downloader-bot.git
cd telegram-downloader-bot
cp .env.example .env
# → مقدار BOT_TOKEN و ADMIN_IDS (شناسه‌های تلگرام، جداشده با کاما) را بگذارید
docker compose up -d
```

نصب روی سیستم (ربات روی میزبان، زیرساخت در کانتینر):

```bash
docker compose up -d postgres redis
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # → BOT_TOKEN و ADMIN_IDS
.venv/bin/python main.py
```

**کوکی‌های یوتیوب (اختیاری ولی توصیه‌شده).** فایل `cookies.txt` با قالب Netscape را در `cookies/cookies.txt` بگذارید یا `python scripts/export_cookies.py` را اجرا کنید. ربات هرگز فایل اصلی را بازنویسی نمی‌کند — `yt-dlp` یک نسخهٔ قابل‌نوشتن می‌گیرد — و در صورت نبود یا کهنه‌شدن لاگین، به ادمین هشدار می‌دهد.

## پیکربندی

همه‌چیز با متغیرهای محیطی تنظیم می‌شود؛ فهرست کامل با توضیح در [.env.example](.env.example) است. متغیرهای پرکاربرد: `BOT_TOKEN`، `ADMIN_IDS`، `DATABASE_URL`، `REDIS_URL`، `WORKER_COUNT`، `MAX_FILE_SIZE_MB`، `TELEGRAM_API_BASE_URL`، `TELEGRAM_API_ID` / `TELEGRAM_API_HASH`، `COOKIE_FILE`، `DEFAULT_DAILY_LIMIT` / `PREMIUM_DAILY_LIMIT`، `YTDLP_PROXY`، `COBALT_API_URL`، `YTDLP_POT_PROVIDER_URL`، `YOUTUBE_SESSION_SERVER`، `BOT_MODE` / `WEBHOOK_PATH` و `TIMEZONE` (مرز بازنشانی سهمیهٔ روزانه و هفته‌های آمار).

> **نوشتن `.env`:** کامنت‌ها را در خط جدا بگذارید — کامنت بعد از مقدار خالی (`TELEGRAM_API_ID=  # note`) توسط dotenv و Docker Compose بخشی از مقدار تلقی می‌شود.

## اجرا

```bash
docker compose up -d
```

برای آپلود بالای ۵۰ مگابایت، سرور Bot API شخصی را بالا بیاورید (ابتدا `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` را در `.env` تنظیم کنید):

```bash
docker compose --profile local-api up -d
```

ابزارهای یوتیوب (`pot-provider`، `yt-session-generator`) و تونل `warp` بخشی از پیش‌فرض‌اند؛ برای دانلود با آدرس خود میزبان، `warp` را حذف کنید.

## توسعه و تست

```bash
pytest                  # تست‌های واحد — بدون نیاز به Postgres/Redis
ruff check .
mypy .

python scripts/smoke.py       # تست یکپارچگی — نیازمند Postgres و Redis
python scripts/boot_check.py  # بررسی سیم‌کشی — فقط نیازمند Postgres
```

## نکات عملیاتی

- ترتیب استقرار، سیکرت‌ها، تغییرات دیتابیس و تست‌های دود: [docs/RELEASE_RUNBOOK.md](docs/RELEASE_RUNBOOK.md).
- بازگردانی (کد / دیتابیس / پیکربندی، جدا از هم): [docs/ROLLBACK.md](docs/ROLLBACK.md).
- دروازهٔ انتشار (بازبینی دستی روی دستگاه واقعی): [docs/LIVE_TAP_CHECKLIST.md](docs/LIVE_TAP_CHECKLIST.md).
- تاریخچهٔ تغییرات: [CHANGELOG.md](CHANGELOG.md). جزئیات عملیاتی (نصب‌کننده، systemd/supervisor، خط لولهٔ کوکی، موتور جایگزین): [deploy/README.md](deploy/README.md).
- پیام **«Sign in to confirm you're not a bot»** خطای یوتیوب است، نه خرابی ربات: اول لاگین را درست کنید (`scripts/export_cookies.py`، `/fixlogin`)، بعد مسیر خروج را (`YTDLP_PROXY` مناسب یا تونل `warp`). دستور `/doctor` علت دقیق را می‌گوید.
- قوانین و حق‌کپی هر پلتفرم را رعایت کنید.

## مجوز

هنوز مجوزی برای این پروژه منتشر نشده است — همهٔ حقوق محفوظ است.
