# Telegram Downloader Bot

Scalable Telegram bot that downloads media (YouTube, Twitter/X, Instagram, TikTok, …)
via `yt-dlp` and uploads it back to the user. Python 3.11+ · Aiogram 3.x (async) ·
PostgreSQL (`asyncpg`) · Redis (queue + FSM state) · fully decoupled Gateway/Worker model.

## Highlights

- **Premium output, HEVC first** — the extractor prefers H.265/HEVC video (`hev1`/`hvc1`)
  with an H.264/AAC fallback, m4a audio and MP4 merging, so files are smaller and stream in
  Telegram without remuxing. The selector is validated against yt-dlp's own parser in tests.
- **Local Telegram Bot API server** — the cloud API refuses bot uploads over 50 MB, which
  would make `MAX_FILE_SIZE_MB=2000` a lie. `docker-compose` runs a `telegram-api` service
  and the bot routes every call through it (2000 MB uploads, files served from disk in
  `--local` mode). Leave `TELEGRAM_API_BASE_URL` empty to fall back to the cloud API.
- **Cookies for blocked content** — drop a `cookies.txt` in the project root (or point
  `COOKIE_FILE` at one) and yt-dlp uses it, which is what gets past YouTube's
  "confirm you're not a bot" check. A missing/blank path is ignored with a warning, never
  passed to yt-dlp as a broken path.
- **Fallback extractor (Cobalt), embedded** — `docker-compose.yml` runs a Cobalt instance
  next to the bot and `COBALT_API_URL` already points at it, so a link yt-dlp is refused on is
  served by the fallback with **no configuration at all** (the public `api.cobalt.tools` is not
  a usable default: it retired its v7 endpoint and refuses anonymous callers). The user gets
  their file instead of an error, while the yt-dlp failure is still recorded for `/blocks` and
  `/trend`. Empty = fallback off.
- **Image posts, not just videos** — «No video could be found in this tweet» is not a failure: the
  post's content is a picture, so the link goes to the fallback and comes back as a **photo** (or a
  **media group** when the post has several), sent with the right Telegram method because the file's
  own shape decides — never as a document. The cache remembers *how* it was sent, so a repeat is
  still instant.
- **Spotify, through YouTube** — Spotify is refused by *both* engines (yt-dlp by policy, cobalt has
  no Spotify service), so the link itself is rewritten: title, artists and duration come from
  Spotify's public embed page (no login, no key), and the matching video — closest length, not the
  top hit — is downloaded through the hardened YouTube path, cobalt and all. The cache key stays the
  Spotify URL, so sending the same link twice is instant.
- **Decoupled Gateway & Workers** — the bot process only validates links, checks limits and
  pushes `DownloadTask`s onto a Redis queue. Heavy yt-dlp work (metadata + download) runs in
  background workers with `asyncio.to_thread`, so the bot never freezes under load.
- **Smart cache** — every successful upload stores `telegram_file_id` keyed by
  `SHA-256(canonical_url | format)`; repeat requests for the same URL are answered instantly
  from the cache (no re-download). Tracking params (`utm_*`, `fbclid`, …) and fragments are
  stripped before hashing, and **the requested format is part of the key** — an MP3 request
  never receives a previously cached video.
- **A menu, not a command list** — `/start` opens an inline keyboard (profile / VIP upgrade /
  help), and those buttons call the same handlers as the commands, so the two cannot drift. A link
  is acknowledged before it is queued (*«🔍 در حال تحلیل و ارسال به صف…»*), because a request that
  looks ignored gets sent twice, and the same message is then edited through the whole download.
- **Quotas & premium** — atomic per-day download counters (reset automatically by local date),
  premium limits, and an always-on expiry sweep.
- **Subscription & payments (Strategy pattern)** — `PaymentStrategy` interface with a
  `ManualPaymentStrategy` (card-to-card): user picks a plan → sends a receipt photo → admins
  get the receipt with Approve/Reject inline buttons → premium is granted on approval.
- **Crash-proof loops** — workers retry with backoff, survive Redis outages, and **requeue
  interrupted tasks on shutdown** so a restart doesn't lose a user's request.
  > Worth knowing: redis-py's connection default is a **5s socket timeout**. A blocking
  > `BRPOP` that waits longer than that is aborted client-side with `TimeoutError` instead
  > of returning `None`, which silently killed every worker ~5s after startup in the first
  > version of this queue. `services/queue.py` therefore always builds its client through
  > `create_redis_client()` with `socket_timeout = BLOCK_TIMEOUT_S + 10`, and
  > `scripts/boot_check.py` idles past `BLOCK_TIMEOUT_S` to prove workers survive it.
- **Graceful lifecycle** — schema auto-bootstrap + plan seeding on startup, token check before
  polling, signal-driven shutdown, polling *and* webhook modes.

## Architecture

```
Telegram
   │  updates
   ▼
┌─────────────────┐   enqueue    ┌────────────┐   dequeue    ┌─────────────────────────────┐
│  GATEWAY (bot)  │────────────▶│ Redis list │─────────────▶│  WORKERS (N asyncio tasks) │
│  /start /status │  DownloadTask│  dl:tasks  │              │  extract → download → upload│
│  URL validate   │              └────────────┘              │  cache file_id → reply     │
│  quota check    │       ▲                                   └─────────────┬───────────────┘
│  cache check    │       │ SHOW users, plans, txns, smart_cache            │ write
└─────────────────┘       └───────────────────────────────── PostgreSQL ◀──┘
      FSM state ◀─────────── Redis (RedisStorage)         smart_cache: url_hash → file_id
```

## Project structure

```
main.py                      entrypoint: wiring, workers, graceful shutdown
core/
  config.py                  pydantic-settings (env / .env), path + id parsing
  database.py                asyncpg pool, schema bootstrap, typed accessors
  utils.py                   hashing, URL canonicalization, formatting, MediaFormat
  telegram_api.py            aiogram session for the cloud or local Bot API server
  logging.py                 logging + force_utf8_console (Windows-safe output)
services/
  extractor.py               yt-dlp wrapped for async (HEVC-first formats, cookies)
  queue.py                   TaskQueue (Redis / in-memory fallback), requeue
  cache.py                   smart-cache service (canonical URL + format → file_id)
  delivery.py                re-send cached file_ids (shared by gateway + workers)
  subscription.py            premium status + effective daily limits
  worker.py                  background download/upload workers + maintenance loop
  payments/
    base.py                  PaymentStrategy / PaymentService (registry)
    manual.py                ManualPaymentStrategy (receipt + admin approval)
handlers/
  user.py                    start, URL intake, format choice, queueing, status
  payment.py                 plans, receipt flow, admin approve/reject
middlewares/
  user_middleware.py         auto-create users row on every update
scripts/smoke.py             integration smoke test (needs Postgres + Redis)
scripts/boot_check.py        offline wiring check (needs Postgres only)
tests/                       unit tests (no infrastructure required)
deploy/                      install.sh, run.sh, systemd unit, supervisor conf, guide
Dockerfile                   bot image (python:3.11-slim + ffmpeg)
docker-compose.yml           bot + Postgres 16 + Redis 7 + cobalt + pot-provider +
                             yt-session-generator (+ telegram-api behind a profile)
pyproject.toml               ruff / mypy / pytest configuration
```

## Quickstart

```bash
# 1. Stack (Docker): bot + Postgres + Redis, on the official cloud API — plus the
#    two YouTube helpers that need no login (a PO-token provider for yt-dlp and a
#    session server for the fallback engine). Each is optional at runtime, so the
#    bot works without them; naming services skips the ones you do not want.
docker compose up -d --build
docker compose logs -f bot

#    … add the self-hosted Bot API server for >50 MB uploads (needs
#    TELEGRAM_API_ID / TELEGRAM_API_HASH in .env)
docker compose --profile local-api up -d --build

# — or run the bot on the host against containerised infra —
docker compose up -d postgres redis cobalt pot-provider yt-session-generator

# 2. Config
cp .env.example .env
#    → set BOT_TOKEN (BotFather) and ADMIN_IDS (comma-separated ints)

# 3. Dependencies
python -m venv .venv && .venv/bin/pip install -r requirements.txt   # Windows: .venv\Scripts\pip
# or: uv pip install -r requirements.txt --python .venv

# 4. Run (polling)
.venv/bin/python main.py
```

**Prerequisite: `ffmpeg` on PATH** (required for MP3 audio conversion and for merging
HEVC/H.264 video with m4a audio — install it for full functionality).

**Cookies:** drop a `cookies.txt` (Netscape format, exported from a logged-in browser) in the
project root. yt-dlp picks it up automatically; without it YouTube often answers with
"Sign in to confirm you're not a bot" — especially from datacenter IPs. An empty or
malformed jar is ignored with a warning instead of failing every request.

**Rotating cookies without a rebuild:** `docker-compose.yml` mounts the project directory
read-only at `/cookies` and points `COOKIE_FILE` at `/cookies/cookies.txt`. The jar is never baked
into the image (nothing to go stale, and no login in an image layer), and a replaced file is
picked up on the very next download — the jar and its writable copy are re-checked per task, so
even `docker compose restart bot` is only needed if you changed the mount itself. That copy exists
because yt-dlp *rewrites* its cookiefile when a download ends: it lives in `downloads/.cookies/`
and is refreshed whenever the source changes.

The mounted *source* is a directory on purpose: bind-mounting the file itself makes Docker
create a directory when the host file is missing (the state a fresh clone is in), and the
container then refuses to start with "not a directory". With a directory source an absent jar is
just a missing file: the bot logs one warning and keeps serving everything that does not need a
login. Set `COOKIES_HOST_DIR` to mount a jar kept elsewhere.

**"Sign in to confirm you're not a bot" — check the login before blaming the IP.** yt-dlp
only treats a jar as signed in when it carries `LOGIN_INFO` **and** one SAPISID-family cookie
(`SAPISID` / `__Secure-1PAPISID` / `__Secure-3PAPISID`); with anything less every request goes
out anonymous and YouTube answers the bot check regardless of where it runs. Cookie exporters
routinely drop those HTTP-only rows, and `LOGIN_INFO` is cleared when Google rotates a session,
so a jar can hold 20 plausible-looking cookies and still be logged out. The bot checks this at
startup and tells you exactly which cookies are missing:

```
WARNING COOKIE_FILE=./cookies.txt loads, but has no YouTube login: LOGIN_INFO missing. …
```

Re-export while signed in (the script warns when the result still has no login):

```bash
python scripts/export_cookies.py --browser chrome --profile Default
```

Only when the login is complete and the block persists is it an IP-level refusal — typical on
VPS ranges, filtered or VPN exit IPs. Then, best first:

```bash
# 1. proxy on a clean IP — but a *residential* one. Measured on this stack: with
#    YTDLP_PROXY set, yt-dlp really used it (a deliberately bogus port fails with
#    "Connection refused" instead of the bot check) and a cloud exit changed
#    nothing, because a datacenter IP is what the check is *for*. A proxy on this
#    machine is `http://host.docker.internal:<port>` from inside the containers.
YTDLP_PROXY=socks5://user:pass@host:1080

# 2. PO-token provider — requests look like a real player, mitigates (does not
#    guarantee) the bot check. It needs no login, and it is already part of the
#    stack and the default (docker-compose.yml runs it, requirements.txt installs
#    its plugin): nothing to enable. `docker compose up -d pot-provider` is only
#    needed if you started the stack without it, and /doctor says so in one line.

# 3. the same idea for the fallback engine — a session server instead of a token
#    for yt-dlp. Also on by default (`yt-session-generator`), also login-free,
#    also reported by /doctor (YOUTUBE_SESSION_SERVER).

```

**Ask the bot instead of guessing.** `/doctor` (admin-only) and
`python scripts/youtube_doctor.py` run the same checks — cookie login, JavaScript runtime,
PO-token provider, ffmpeg, optional proxy — plus a live probe, and answer with one verdict
and the single next step worth taking:

```
🩺 دکتر یوتیوب
✅ کوکی: /cookies/cookies.txt — لاگین یوتیوب کامل است
✅ منبع کوکی: /cookies/cookies.txt — mount /cookies (9p، فقط-خواندنی)، مسیر میزبان:
   /Users/mo/bot، 31 کوکی، اکسپورت 4 دقیقه پیش؛ از همین اکسپورت استفاده می‌شود
⚠️ PO token: http://pot-provider:4416 پاسخ نمی‌دهد — دانلودها بدون توکن ادامه پیدا می‌کنند …
🎫 سرور سشن یوتیوب: http://yt-session-generator:8080 — توکن آماده، ساخته‌شده 6 دقیقه پیش؛ کوبالت هر ۵ دقیقه خودش دوباره می‌خواند
⛔️ تست زنده: SESSION_STALE: یوتیوب این درخواست را نپذیرفت (سشن کهنه است)…

🔴 موتور جایگزین (داخلی): http://cobalt:9000 — برای یوتیوب سشن/کوکی ندارد • v10 • 0.4s
     علت: ERROR: نمونهٔ کوبالت خطا داد (400): این نمونه برای یوتیوب سشن/کوکی ندارد (error.api.youtube.login)
     قدم بعدی: دو راه دارد و هر دو در /doctor خط وضعیت دارند: یک اکسپورت با لاگین یوتیوب …، یا سرور سشن …

حکم: ⛔️ سشن و provider ناهماهماند.
قدم بعدی: دسترس‌بودن provider را درست کنید (docker compose up -d pot-provider)؛ بدون توکن، یوتیوب درخواست را نیمه‌کاره رد می‌کند. ⚠️ و موتور جایگزین هم آماده نیست — کاربران بلاک می‌مانند.
```

The `منبع کوکی` line is the answer to "I exported new cookies — is the bot using them?":
where the jar is mounted (and whether that mount is read-only), which host directory it came
from, when it was exported, and whether the copy yt-dlp reads is the current one. A jar that is
newer than the copy reports `warn` — not a failure, because the next download picks it up
without a restart.

**The fallback gets its own section, because it is a different chain.** It is what happens
*after* yt-dlp says no, so it is probed separately and reported as one of five states — each
with a different action:

| Line | Means | Do |
|---|---|---|
| 🟢 آماده به کار | answered a real resolve (dialect shown: `v7`/`v10`) | nothing |
| 🟡 قرنطینه | failed as an *instance* and is being left alone (`COBALT_…`) — the reason is printed | fix the reason, or wait 10 min |
| 🔴 نیازمند کلید احراز هویت | the instance rejects anonymous requests (the public one does) | `COBALT_API_KEY`, or self-host |
| ❌ در دسترس نیست | unreachable from *this* host (timeout, DNS, proxy) | check `COBALT_API_URL`/`COBALT_PROXY` |
| ⚫️ خاموش | `COBALT_API_URL` is empty | nothing (a block behaves as before) |

The verdict follows: when the primary path is blocked and the fallback is 🟢 it says so
(*"لینک‌های بلاک‌شده از موتور جایگزین دانلود می‌شوند"*) instead of leaving the admin to work out
whether users are affected. The last verdict is stored in `bot_state`, so an offline run
(`--no-probe`) or one after a restart prints it as *آخرین نتیجهٔ ثبت‌شده* rather than shrugging —
and the report never fabricates a 🟢 it did not observe.

**You do not have to ask.** Picking a new jar up on the next download is convenient but silent,
so the bot watches the jar itself (every `COOKIE_WATCH_INTERVAL_S`, 60s by default) and messages
every admin *once* when it sees an export the running bot has not loaded yet — naming the mount,
the age, and whether the new jar would even sign YouTube in:

```
🍪 کوکی تازه اکسپورت شد، ولی ربات هنوز برنداشته
فایل: /cookies/cookies.txt
منبع: mount /cookies (9p، فقط-خواندنی)، مسیر میزبان: /Users/mo/bot
جار: 24 کوکی، اکسپورت همین حالا
وضعیت: ربات هنوز روی اکسپورت قبلی است

👉 دانلود بعدی خودش این نسخه را برمی‌دارد؛ ری‌استارت یا ری‌بیلد لازم نیست.
```

Restarting is never announced — the jar present at startup is the baseline, not news — and a jar
that was already taken by a download is silent too (the alert is "not picked up *yet*"). Losing a
login is the one failure that looks like a blocked IP, so a fresh export without `LOGIN_INFO`
says so in the same message. `COOKIE_WATCH_INTERVAL_S=0` switches the watcher off.

Every alert carries two buttons. **«بررسی همین حالا»** runs the doctor and edits that same message
with the verdict — the report above, without typing `/doctor` on the server or on your phone — and
**«♻️ اکسپورت دوباره»** reads the browser profile and replaces the jar right then (the same guarded
path the automatic refresh uses), edits the alert with the outcome, and probes the result. Both
stay admin-only: a forwarded alert answers "⛔️ فقط ادمین می‌تونه." for anyone else.

**On demand, by hand.** `/refresh` (admin-only) does the same thing from a message, and takes the
profile to read — `/refresh edge:Default`, or nothing to use `COOKIE_AUTO_EXPORT`. It ignores the
30-minute cooldown (a human asked) while keeping every guard rail that matters, so it is the way to
try a different profile without editing `.env` or restarting:

```
♻️ جار کوکی خودکار تازه شد (31 کوکی از پروفایل مرورگر) و همین حالا تست شد:

✅ تست با همین اکسپورت: متادیتای «Big Buck Bunny» خوانده شد — یوتیوب الان درخواست را رد نکرد.

📣 همین برای ادمین‌ها هم رفت.
```

Before spending an attempt, the refresh asks the cheap question first: does this machine even have
that browser's profile? Inside a plain container it does not, so instead of a wasted export and a
confusing yt-dlp error the admins get the paths we looked at and the command to run where the
browser is:

```
🍪 اکسپورت خودکار کوکی ممکن نشد
از این اجرا مرورگرِ edge دیده نشد، پس چیزی برای خواندن نبود و اصلاً تلاش نکردم.
مسیرهایی که گشته شد:
• /root/AppData/Local/Microsoft/Edge/User Data/Default/Network/Cookies
…
👉 روی همان ماشینی که مرورگر لاگین‌شده دارد اجرا کنید: python scripts/export_cookies.py
```

A layout we do not know is never a refusal — the attempt happens — and that answer is also what
`boot_check.py` reports on startup, so the container case is visible before a download fails.

**Helpers are watched the same way, because their failures have no user-facing voice.** The
PO-token provider and the session server are two of YouTube's three routes, and when one dies the
next user's link simply fails — which looks exactly like a blocked IP. Every
`HELPER_WATCH_INTERVAL_S` (5 minutes by default) the bot probes both, writes a row **only when a
state changes** (so `helper_events` is a history, not a log), pages the admins once a helper has
been down ten minutes — and once more when it comes back, at most one page per helper per hour so a
flapping container cannot flood the chat — and puts a paragraph in the weekly digest: which helper
was down, for how many hours, and what it cost the users. A provider whose major version the plugin
rejects (`drift`) counts as down: it answers every health check with a 🟢 and mints nothing, which
is the one failure a status check alone can never show.

**And when the login itself is the problem, there is a wizard.** `/fixlogin` (admin-only) prints the
diagnosis and the exact steps; `python scripts/fix_login.py` walks them on the machine that has the
browser: it detects a jar that reads but cannot sign in (and names the missing HTTP-only rows),
lists the profiles this machine actually has, exports, **checks the login landed before claiming
anything**, probes YouTube live, messages the admins the verdict, and records the fix for `/trend`.
It refuses the two traps by name — a browser that was never signed in to YouTube, and an export that
dropped the HTTP-only rows. `--dry-run` does all of it without touching the real jar.

**One Windows wall worth knowing about.** Chrome 127+ (Edge, Brave and the rest followed) encrypt
the key that protects the cookie database with the browser's *own* identity — App-Bound Encryption —
and nothing outside the browser can decrypt it, yt-dlp included. The profile is there, the attempt
fails with a generic *“failed to load cookies”* that reads like a missing browser, so the bot reads
the browser's `Local State` and says what it actually is before wasting an attempt:

```
✘ مرورگرِ edge کوکی‌هایش را با App-Bound Encryption ویندوز قفل کرده و هیچ برنامه‌ای بیرون از خود
  مرورگر (از جمله yt-dlp) نمی‌تواند رمزگشایی‌شان کند — این خطا ربطی به لاگین‌بودن یا نبودن ندارد.
راه‌هایی که واقعاً کار می‌کنند:
• اکسپورت با افزونه‌ای که include HttpOnly را دارد (کوکی‌های لاگین فقط HttpOnly هستند).
• یا Firefox برای این کار (این قفل را ندارد) و بعد COOKIE_AUTO_EXPORT/COOKIES_FROM_BROWSER روی firefox.
• یا همان روش دستی: اکسپورت بگیرید و cookies.txt را جایگزین کنید.
```

Locked profiles still appear in the wizard's list, marked as locked, so the answer is "use that other
browser" rather than "why is this not working?".

**The alert checks the export before it claims anything.** Counting cookies cannot separate a
signed-in jar from a plausible signed-out one, so before sending, the watcher runs one
*metadata-only* extraction (no download — a few seconds) with the new jar and puts the answer in
the same message:

```
✅ تست با همین اکسپورت: متادیتای «Big Buck Bunny» خوانده شد — یوتیوب الان درخواست را رد نکرد.
⛔️ با همین اکسپورت هم یوتیوب رد کرد — همان لاگین ناقص (LOGIN_INFO) توضیحش می‌دهد، نه IP.
⚠️ با همین اکسپورت، یوتیوب سشن را کهنه دید — خطای گذراست.
❓ نشد با این اکسپورت تست کنم (TimeoutError) — /doctor حکم نهایی را می‌دهد.
```

So "did my export work?" is answered in the alert itself, not by a later failed download. When a
complete login still gets blocked the same line says the opposite — that one is the IP, and a
proxy is the fix — which is the distinction this project kept getting wrong in the other direction.
A probe that cannot run (offline host, a 45s timeout) says so instead of inventing a verdict, and
never blocks the alert. Probing also hands yt-dlp the new jar, so the export is picked up as part
of answering.

Use `--no-probe` for an offline, config-only run.

**Or let the bot do the mechanical part.** Set `COOKIE_AUTO_EXPORT` to the same profile spec
`COOKIES_FROM_BROWSER` takes (`chrome`, `edge:Default`, `chrome+gnomekeyring`) and the bot re-exports
the jar *itself* the first time a download fails because YouTube treated it as anonymous — once per
30 minutes at most, from the profile it can actually read. Three rules keep it from making things
worse: the candidate must be a usable jar, it never overwrites a jar that *does* sign in, and it is
written beside the target and moved into place (so a worker thread never reads a half-written cookie
file). Then the new jar is probed and the admins get the verdict:

```
🍪 جار کوکی خودکار تازه شد (31 کوکی از پروفایل مرورگر) و همین حالا تست شد:

✅ تست با همین اکسپورت: متادیتای «Big Buck Bunny» خوانده شد — یوتیوب الان درخواست را رد نکرد.
```

On a plain container there is no browser profile to read, so the honest answer is the one that
arrives instead: *«اکسپورت خودکار کوکی ممکن نشد … روی همان ماشینی که مرورگر لاگین‌شده دارد اجرا
کنید»* — which is also why the feature is off by default.

**The user hears the same diagnosis.** When a download fails with a block our jar explains — no
usable jar at all, or a YouTube link with a jar that cannot sign anyone in — the user gets the
cause in plain words instead of a generic failure:

```
🔒 این لینک فقط با یک حساب واردشده (لاگین) قابل دانلود است و اتصال فعلی ربات اجازهٔ دانلود را
ندارد — مشکل از سمت شما نیست.
موضوع را به ادمین اطلاع دادیم؛ بعد از این‌که برطرف شد، همین لینک را دوباره بفرست.
```

and the admins get one notice naming the missing cookies and the fix. That notice is rate-limited
to once per 10 minutes (`LOGIN_BLOCK_ALERT_INTERVAL_S`): a logged-out jar fails *every* link, and
while each of those users deserves an answer, a chat full of identical alerts helps nobody. Blocks
the jar cannot explain keep the generic message — those are the ones worth a proxy.

**The user is told before the wait, not after it.** When a YouTube link arrives, the gateway asks two
cheap questions — can our jar sign in, and were anonymous requests actually refused here recently
(the watcher's probe and any failed download both record that, for 15 minutes)? A jar that cannot
sign in is only a *note* on the queued message, because most videos extract fine anonymously:

```
⏳ لینک در صف پردازش قرار گرفت (موقعیت تقریبی: 1).
ℹ️ نکته: کوکی ربات الان لاگین یوتیوب نیست، پس اگر این ویدیو لاگین لازم داشته باشد دانلود ممکن است
شکست بخورد — در آن صورت به ادمین گزارش می‌شود.
```

but once a refusal has actually been observed, the next YouTube link is not queued at all — the user
is told why in one message instead of waiting for a failure, and no quota is touched. Nothing is
refused on suspicion alone, and the state clears itself: a successful anonymous download or a
fresh signed-in jar wipes the evidence immediately (plus the 15-minute expiry).

A stale session is usually transient, so the bot retries it on its own
(`EXTRACTOR_RETRY_ATTEMPTS`, exponential backoff) before the user ever sees an error — blocks are
deliberately *not* retried, since a flagged IP fails identically every time and would just burn the
queue slot. The doctor always probes once, without retries, so its verdict describes the first
response.

### When the site refuses us: the fallback extractor (Cobalt, embedded)

`docker-compose.yml` ships the instance, so there is nothing to install or sign up for:

```
cobalt: ghcr.io/imputnet/cobalt:10   # API_URL=http://cobalt:9000/ , port published on 127.0.0.1 only
bot:    COBALT_API_URL=http://cobalt:9000   # the default in core/config.py
```

**What it does fix:** an extractor that broke, a site that refuses yt-dlp's requests, a public
instance that died, and YouTube's *per-client* bot check once the instance has a session.

**What it cannot fix — and this one matters:** the embedded instance shares the host's network
address, so it is **not another address** for an IP-level block. A flagged IP is still fixed the
same way it always was (a *residential* `YTDLP_PROXY`), and if you want the *fallback* to leave
through a different path, give the instance its own proxy — it does not inherit `YTDLP_PROXY`:

```bash
COBALT_HTTP_PROXY=socks5h://user:pass@host:1080   # .env → docker compose up -d cobalt
```

That value arrives inside the cobalt container as `API_EXTERNAL_PROXY`, and the name is not a
detail: cobalt's dispatcher never reads `HTTP_PROXY`/`HTTPS_PROXY`, so setting those (as this file
used to) wired nothing at all. Both halves are measured: a deliberately bogus value makes cobalt
answer *"could not reach the source"* rather than YouTube's answer, and a real cloud proxy makes it
answer exactly what it answers with no proxy — a different address is not an accepted one. The
session generator is the one route with no proxy setting at all, and deliberately so: its token is
bound to the IP its browser ran on, which is what makes it trusted.

**One export signs both engines in.** Cobalt has no login of its own and reads cookies in its own
shape (a mapping of service → `Cookie:` header strings), so the bot converts the jar it already
has into the file cobalt reads — at startup, and on every fresh export the watcher sees:

```
cookies.txt (Netscape, yt-dlp)  ──▶  cobalt/cookies.json (what cobalt reads)
```

Nothing to do by hand, and nothing to mount: `COBALT_COOKIES_DIR` (default `cobalt/`, the host
directory compose mounts into both containers) is the whole switch, and `python
scripts/export_cobalt_cookies.py` writes it explicitly when the bot is not running. A signed-in jar
gives cobalt the login; **even an anonymous jar is worth writing**, because cobalt only retrieves
YouTube's player — and its bot-check handling — when it has a cookie at all (`retrieve_player =
Boolean(sessionTokens || cookie)`), which is the difference between a bare innertube call and a
real attempt. For an IP that a login still does not satisfy, point cobalt at a po-token server:

```bash
# on by default (the `yt-session-generator` service) — this is the default value
YOUTUBE_SESSION_SERVER=http://yt-session-generator:8080
# empty = the fallback runs without this route; a fresh token needs no restart,
# because cobalt re-reads the server every 5 minutes on its own
```

A file it did not write is never replaced (including the flat-array shape one of cobalt's own docs
examples suggests — that one is reported, not overwritten), and services that are not ours keep
their entries. Cobalt reads the file **once, at startup** and writes its own refreshes back into it,
which is why the sidecar beside it (`generated.json`) records when the *content* changed: that is
what lets `/doctor` say "the running instance is on the old version" instead of guessing from an
mtime. After a new export the missing step is one command: `docker compose restart cobalt` — the
jar alert says so too.

TikTok, Instagram, Twitter, Streamable and the rest work without any of that. Whatever the state,
`/doctor` reports it in one line instead of leaving it to guesswork — including this exact case,
now followed by what the generated file holds and whether cobalt has it:

```
🔴 موتور جایگزین (Cobalt): http://cobalt:9000 (داخلی) — برای یوتیوب سشن/کوکی ندارد • v10 • 1.1s
     علت: ERROR: کوبالت نتوانست این لینک را بگیرد: … (error.api.youtube.login)
     قدم بعدی: کوکی‌های همین جار خودکار اینجا نوشته می‌شوند — پس یک اکسپورت با لاگین یوتیوب کافی است …
🍪 کوکی کوبالت: /app/cobalt/cookies.json — 24 کوکی یوتیوب، تولید 2 دقیقه پیش، بدون LOGIN_INFO؛ کوبالت همین نسخه را خوانده
```

An embedded instance is labelled **(داخلی)** wherever it is reported (`/doctor` and `/blocks`),
because "the instance we run" and "an instance somewhere else" differ in exactly the way that
matters: only the second one can be the other address. It also answers to two addresses — the
compose name inside the network, the published `127.0.0.1:9000` on the host — and the client
follows the one that exists (`cobalt_probe_url`), so a bot or a script run against the
containerised infra needs nothing set. A compose service name does not resolve outside its
network, which is exactly why the port is published.

**A resolved link is repaired, not trusted.** Cobalt passes a service's URL through untouched, and
services write them badly: streamable's own API answers *every* video with a doubled scheme
(`https:https://cdn-cf-west…`), and protocol-relative links (`//cdn/…`) are an old media-site
habit. Either would fail inside a transfer — where a stranger's typo looks like an outage — so the
repair happens where links enter the client, with one log line naming it. `smoke`/`boot_check`
assert the resolved URL is one an HTTP client can actually fetch, so a shape nobody repaired is
caught at the edge instead of in a user's download.

Some links cannot be fixed from here at all — a flagged IP, or a jar YouTube will not accept — and
until now the honest answer to the user was still a refusal. `COBALT_API_URL` (default
`http://cobalt:9000`, the instance in this compose file) changes only that case: yt-dlp is still
the primary engine, and when it
comes back **blocked** the link is handed to a Cobalt instance, which fetches it from *another
address* and returns a direct link the bot streams into the same per-job directory. The user sees a
download, not an error.

What is handed over is deliberately narrow — `EXTRACTOR_BLOCKED`, and `SESSION_STALE` once its
retries are spent, because those two mean *the site refused this connection*. A private video, an
age-gated one, a live stream or a playlist is the same answer from any address, so those keep the
message they always had, and the fallback is never burned on them:

```
🛠 مسیر اصلی به این لینک دسترسی نداشت؛ از مسیر جایگزین دانلود میشود…
⬇️ دانلود: 42% (12.4 MB / 29.6 MB)
✅ دانلود و ارسال شد.
```

Four things are worth knowing before you rely on it:

- **Configured is the only thing we can check cheaply.** Asking the instance on every link would cost
  more than it saves, so the gateway stops *refusing* links once a fallback is configured (an observed
  refusal becomes a note instead of «🚧»), and if the instance turns out to be unreachable the user
  gets the original diagnosis anyway — no worse than before the fallback existed.
- **The failure is still recorded.** A fallback success writes the same `block_events` row a failure
  would (`login`/`ip`/`session`), so `/blocks`, `/trend` and the weekly digest keep showing that the
  *primary* engine is degraded while nobody is complaining. That is the whole point: the fallback buys
time, it does not fix the jar.
- **Metadata is thinner.** Cobalt returns a file, not a media object: the caption's title is read off
  the filename it produces (`Title [id].ext`, minus the bracket), and duration and thumbnail are left
  empty rather than guessed. Album/playlist links stay refused — it is one file per request.
- **The default instance cannot serve you anonymously any more.** Probing it (September 2026) is
  unambiguous: its retired v7 endpoint answers *"the cobalt v7 api has been shut down on nov 11th
  2024"*, and its current v10 API answers unauthenticated requests with
  `error.api.auth.jwt.missing`. So the default is a placeholder, not a working fallback: run your
  own ([ghcr.io/imputnet/cobalt](https://github.com/imputnet/cobalt)) — on a host whose IP YouTube
  does *not* flag, which is the whole point — and set `COBALT_API_URL` to it. Both API shapes are
  spoken (the documented v7 request, and v10's, which moved the API to the instance root), and the
  one that answers is remembered. An instance that fails as an *instance* — auth, rate limit, 5xx,
  unreachable — is left alone for 10 minutes, so a dead fallback costs one round trip rather than
  one per blocked link.
- **How to tell it is working.** `boot_check.py` (`python scripts/boot_check.py`) resolves one real
  link through the configured instance. `✔ the fallback instance answered a real resolve` means this
  deployment has a net; `⚠` with `error.api.auth.jwt.missing` means it does not yet.

`boot_check.py` resolves one real link through the configured instance and only *warns* when it
fails (an unreachable fallback degrades to the behaviour that existed before it, so it must not fail
a deployment), and `smoke.py` checks the same plus the decision itself: a block is handed over, a
geo-blocked link is not.

### Spotify: the link is rewritten, not downloaded

Spotify is the one platform where **both** engines refuse the link itself, and both refusals are
by design rather than by address: yt-dlp answers `open.spotify.com` from its `KnownDRMIE` list
("The requested site is known to use DRM protection. It will NOT be supported."), and the embedded
cobalt v10 has no Spotify service at all (its supported list ends at youtube, and a Spotify link
comes back as `error.api.link.unsupported`). No cookie, token or proxy changes either answer.

What can work is the same song elsewhere, so the link is rewritten before anything tries to fetch
it. Spotify publishes what the mapping needs on the track's *public embed page* — title, artists
and duration, no login and no API key — and the search that turns those into a video runs on the
hardened path this bot already has (cookies, PO token, proxy), with cobalt still behind it, because
by then the link *is* a YouTube one:

```
https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC     (spotify.link share links too)
        │  embed page: "Never Gonna Give You Up" · Rick Astley · 213s
        ▼  ytsearch5: "Rick Astley Never Gonna Give You Up"   (flat: no candidate is downloaded)
https://www.youtube.com/watch?v=dQw4w9WgXcQ                → the ordinary download path
```

Three details are deliberate:

- **The closest length wins, not the top result.** A live version or a "10 hours" loop ranks well and
  is not the song, so the candidate whose duration is nearest to Spotify's is taken, and one that
  drifts more than 90 seconds from it is refused — the honest answer there is «نسخهٔ یوتیوب این آهنگ
  پیدا نشد», never the wrong track.
- **Only tracks are rewritten.** An album or a playlist is a list of songs, and a bot that quietly
  downloaded the first one would answer a question nobody asked.
- **The cache belongs to the link you sent.** The mapped video is an implementation detail: the
  `smart_cache` key stays the Spotify URL, so the same link the second time is instant, and the
  `block_events` row (when YouTube refuses the *search*) names the link the user actually sent.

A refused search is not swallowed either: it arrives as the block it is, so the user gets the known
cause, the admins get the alert and the digest counts it — the same machinery as any other YouTube
link, because that is what it now is. `python scripts/youtube_doctor.py` is where you look when that
happens, and `/doctor` says the same in one line.

Cookies can also be read straight from a browser profile with
`COOKIES_FROM_BROWSER=chrome:Default`. The bot probes it at startup and ignores it with a
warning when the profile is unreachable (the usual case inside a container — Chromium's
cookies are encrypted with the *host* user's keyring), instead of failing every download.

The bot also picks a JavaScript runtime automatically (`YTDLP_JS_RUNTIME=auto`): yt-dlp needs
one to solve YouTube's player challenge, and without it warns that formats may be missing.
The Docker image ships Deno; host installs use whichever of deno/node/bun/qjs is present.

**Local Bot API server:** the cloud API refuses bot uploads over 50 MB, so a self-hosted
`telegram-bot-api` is what makes `MAX_FILE_SIZE_MB=2000` real. It needs `TELEGRAM_API_ID` and
`TELEGRAM_API_HASH` from my.telegram.org, so it sits behind the `local-api` profile and starts
only when asked for (`docker compose --profile local-api up -d`).

If `TELEGRAM_API_BASE_URL` points at a server that is not running, the bot logs the failure
and **falls back to the cloud API** with the upload ceiling capped at 50 MB — it does not
exit, and it never downloads a file too big for the transport it ended up with.

### Webhook mode

```bash
BOT_MODE=webhook WEBHOOK_URL=https://bot.example.com WEBHOOK_SECRET=... .venv/bin/python main.py
```

## Deployment

See **[deploy/README.md](deploy/README.md)** for the full guide:

- **VPS with Docker** — `docker compose up -d --build` (bot + Postgres + Redis).
- **Shared Python host / bare VPS** — `bash deploy/install.sh`, then systemd
  (`deploy/telegram-downloader-bot.service`) or supervisor (`deploy/supervisor.conf`).

The bot requires **PostgreSQL**; Redis is optional (`QUEUE_BACKEND=memory` falls back to an
in-process queue, losing queued work on restart).

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `BOT_TOKEN` | — | Bot token (required) |
| `ADMIN_IDS` | — | Comma/space-separated Telegram IDs (JSON also accepted); receive receipts |
| `BOT_MODE` | `polling` | `polling` or `webhook` |
| `DATABASE_URL` | local postgres | asyncpg connection string |
| `REDIS_URL` | local redis | queue + FSM storage |
| `QUEUE_BACKEND` | `redis` | `memory` = dev only (state lost on restart) |
| `QUEUE_NAME` | `dl:tasks` | Redis list name |
| `WORKER_COUNT` | `2` | concurrent download workers |
| `MAX_FILE_SIZE_MB` | `2000` | per-file cap; capped to 50 MB whenever the cloud API is in use |
| `TELEGRAM_API_BASE_URL` | — | local Bot API server, e.g. `http://telegram-api:8081`; empty = cloud API |
| `TELEGRAM_API_LOCAL` | `false` | server runs with `--local` (files read off disk) |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | — | from [my.telegram.org](https://my.telegram.org); used by the `telegram-api` container |
| `TELEGRAM_API_FILES_DIR` | — | host dir mapped to the server's file storage for local reads |
| `DOWNLOAD_DIR` | `downloads/` | scratch space; relative paths resolve against the project root |
| `EXTRACTOR_TIMEOUT_S` / `DOWNLOAD_TIMEOUT_S` | `90` / `1800` | timeouts |
| `EXTRACTOR_RETRY_ATTEMPTS` | `2` | extra attempts after a *retryable* failure (YouTube's stale session), exponential backoff, `0` disables |
| `EXTRACTOR_RETRY_BACKOFF_S` | `3.0` | base of that backoff (3s, 6s, …) |
| `DEFAULT_DAILY_LIMIT` / `PREMIUM_DAILY_LIMIT` | `10` / `60` | daily quotas |
| `MANUAL_CARD_NUMBER` / `MANUAL_CARD_HOLDER` | — | shown in the payment message |
| `COOKIE_FILE` | `cookies.txt` | cookie jar for YouTube / age-restricted content (empty = disabled); needs `LOGIN_INFO` + a SAPISID cookie to count as a login, checked at startup. Read-only is fine — yt-dlp is given a writable copy |
| `COOKIES_HOST_DIR` | `./` | compose-only: host directory mounted read-only at `/cookies` (jar must be `cookies.txt` inside it); a fresh export needs `docker compose restart bot`, not a rebuild |
| `COOKIE_WATCH_INTERVAL_S` | `60` | how often the running bot checks whether a fresh export is still waiting, and messages the admins once per export; `0` disables the watcher |
| `HELPER_WATCH_INTERVAL_S` | `300` | how often the two YouTube helpers are probed: one `helper_events` row per *change*, a page once a helper has been down 10 minutes (then at most hourly), a recovery message, and a paragraph in the weekly digest; `0` disables it |
| `COOKIES_FROM_BROWSER` | — | `BROWSER[+KEYRING][:PROFILE]`; probed at startup and ignored when unreachable |
| `COOKIE_AUTO_EXPORT` | — | same syntax; re-export the jar from that profile after a login-shaped block (once per 30 min, never overwriting a working login), then probe it and report |
| `YTDLP_JS_RUNTIME` | `auto` | JS runtime for yt-dlp (`auto`/`none`/`node`/`deno`/`bun`/`quickjs`) |
| `YTDLP_POT_PROVIDER_URL` | `http://pot-provider:4416` | PO-token provider base URL (the service compose runs); empty = off |
| `YOUTUBE_SESSION_SERVER` | `http://yt-session-generator:8080` | YouTube session server for the fallback engine; empty = off |
| `YTDLP_PROXY` | — | optional proxy for yt-dlp, e.g. `socks5://user:pass@host:1080`; worth it only with a login in the jar and a *residential* exit |
| `COBALT_API_URL` | `http://cobalt:9000` (the instance compose runs) | fallback extractor, used only when yt-dlp comes back *blocked*; empty = off |
| `COBALT_API_KEY` | — | sent as `Authorization: Api-Key …` (self-hosted instances usually need one) |
| `COBALT_TIMEOUT_S` / `COBALT_DOWNLOAD_TIMEOUT_S` | `30` / `1800` | resolve (one round trip) and transfer budgets |
| `COBALT_PROXY` | — | optional proxy for the fallback only — not inherited from `YTDLP_PROXY` |
| `COBALT_HTTP_PROXY` | — | compose-only: what gives *cobalt* another way out, passed to that container as `API_EXTERNAL_PROXY` (the only name it reads) |
| `WEBHOOK_PATH` | `/webhook` | leading slash is added automatically |

> **Writing `.env`:** put comments on their own line. Both python-dotenv and Docker Compose's
> `env_file` parser absorb a trailing comment into an *empty* value (`TELEGRAM_API_ID=  # note`
> becomes the string `# note`). The bot now reads such a value as blank and warns instead of
> crashing, but a populated value with a trailing comment is still cleaner written separately.
| `TIMEZONE` | `Asia/Tehran` | daily quota reset boundary |

## Database

Six tables (auto-created at startup, seeding is one-shot — edit prices later in DB):

- `users` — `telegram_id` PK, username, `is_premium`, `premium_until`, `daily_downloads`, `last_download_date`
- `subscription_plans` — `id`, `name`, `duration_days`, `price`
- `transactions` — UUID `id`, user/plan FKs, `amount`, `status` enum (`pending|approved|rejected`), `method` enum (`manual`), `receipt_photo_id`
- `smart_cache` — `url_hash` (SHA-256 of canonical URL + format) PK, `original_url`, `platform`, `telegram_file_id`, `quality`, `kind` (how to send it again: `video`/`audio`/`photo`/`photo_group`; a `photo_group` keeps a JSON list of ids, and rows from before the column existed are delivered by `quality` as they always were)
- `block_events` — every failed download: `telegram_id`, `url_host`, `code`, `cause` (`login|ip|site|session`), `created_at`; no FK on purpose, telemetry must outlive a user row
- `bot_state` — small key/value store the bot uses for "when did it last do X" — the weekly digest's
  delivery stamp, the doctor's last fallback verdict, and what the fallback did the last time a real
  link needed it

## The user's side: one answer, one menu

A link in, a file out — and every other question a tap away, because a downloader bot that only
answers one command makes people guess at their own quota and status:

```
/start
├── 👤 پروفایل من      → Telegram ID, username, status (رایگان 🪙 / ویژه 💎), today's quota,
│                        premium expiry, and a 🔙 بازگشت button
├── 💎 ارتقا به ویژه (VIP) → the plans and the manual-payment flow
└── ❓ راهنما          → the supported sites, formats and limits
```

Those buttons are callbacks into the same handlers as `/profile`, `/premium` and `/help` — the menu
is a shortcut, not a second implementation, so the two can never drift. Status and quota are read
at tap time (never cached in a keyboard), the labels carry the current state, and every callback
answers its query, so no button ever leaves a spinner on the user's screen.

**Feedback starts before the work does.** A link gets its answer immediately — *«🔍 در حال تحلیل و
ارسال به صف…»* — because the queue can be busy, and a request that looks ignored gets sent twice.
The same message is then edited as the task moves (queued → downloading with progress → uploading →
done), so a user sees one message per download instead of a stream of them.

## Knowing what failed

Every failed download is stored with the cause we diagnosed: `login` (our jar cannot sign in),
`ip` (a complete login and still blocked — a proxy is the fix), `session` (transient), or `site`
(the link or the content itself, nothing to fix here). Single failures are answered where they
happen; the *pattern* is what no single message can show, so the maintenance loop sends the admins
a weekly digest — and `/blocks` (admin-only) prints it on demand:

```
📉 گزارش ۷ روزهٔ شکست‌ها

کل: 42 شکست
بیشترین میزبان: youtube.com (39)

🔐 کوکی/لاگین: 35
🌐 IP یا PO token: 4
🎬 خود سایت/لینک: 3

قدم بعدی: بیشترین علت لاگین است و از همین‌جا رفع می‌شود: یک اکسپورت تازه از کوکی …

🔌 موتور جایگزین: https://api.cobalt.tools — 🔴 نیازمند کلید احراز هویت • آخرین نتیجهٔ ثبت‌شده
     آخرین لینک بلاک‌شده: 4 دقیقه پیش — ⏭ رد شد (استفاده نشد)
          قرنطینه بود (error.api.auth.jwt.missing: این نمونه احراز هویت می‌خواهد)
     قدم بعدی: یک نمونهٔ خودتان اجرا کنید (ghcr.io/imputnet/cobalt) یا COBALT_API_KEY بگذارید
     برای آزمون تازه: /doctor
```

A failure that the fallback extractor saved is recorded here too, with the same causes. It is not a
reported failure for the user — it is the only place the *operator* can see that the primary engine
is degraded while the queue looks healthy.

**The last paragraph is the one that changes how the numbers read.** 35 login failures mean one
thing while the safety net is serving those links anyway, and something else entirely when it is
not — so `/blocks` ends with the same fallback section `/doctor` shows, without spending a request:
*which* instance (`COBALT_API_URL`, or *خاموش*), in what state, and — the part no probe can answer —
what happened the **last time a real blocked link needed it** (served it, failed, or was never
asked, with the reason and how long ago). That record is written by the worker on real traffic, so
it survives a restart and does not expire with a 10-minute quarantine; a fresh synthetic probe stays
`/doctor`'s job, and both render through one function so the two commands cannot disagree.

**And a run of failures pages immediately.** Three login failures inside an hour send the same
report right away, headed by what happened (*«🚨 3 شکست لاگین در 60 دقیقهٔ اخیر»*) instead of
waiting for the weekly trend — the alarm, not the ledger. It is gated by that threshold and a
30-minute cooldown, so a burst pages at most twice an hour, and only the *fixable* cause (login)
counts: a private video is not an incident.

**`/trend` answers the question the digest cannot: did the fix help?** Every replacement of the jar
— automatic, by hand, by the wizard, or a fresh export the watcher picked up — is recorded with its
detail, and `/trend` (admin-only, 14 days) shows the days, marks the fix on its own day (even when
that day was quiet), and weighs the failures on either side of it:

```
📈 روند ۱۴ روزهٔ شکست‌ها

09-08  5 شکست — 🔐لاگین 4، 🌐IP 1
── 🔧 جار کوکی: edge:Default → 31 کوکی ──
09-12  1 شکست — 🌐IP 1
(12 روز بدون شکست)

🔧 آخرین اصلاح: ۲ روز پیش — جار کوکی
🔐 کوکی/لاگین: 12 → 1 مورد (روزی 6.0 → 0.5) — ✅ محسوس کمتر شد
📊 همهٔ شکست‌ها: 14 → 3 مورد (روزی 7.0 → 1.5) — ✅ محسوس کمتر شد

قدم بعدی: بیشترین علت IP است: یک YTDLP_PROXY روی IP تمیز …
```

The comparison is **daily rates, not raw counts**, and a fix younger than six hours is not judged at
all (*«⏳ برای داوری زود است»*) — one quiet afternoon after an export is not evidence, and saying so
would be exactly the kind of claim this bot keeps having to retract. Days are grouped in your
`TIMEZONE`, so "the day the fix landed" means your day.

A week with no failures stays silent *and changes nothing* — the stamp in `bot_state` records a
**delivery**, so a quiet week (or a diagnostic run like `boot_check.py`, which is started with the
digest switched off) cannot consume the window, and a failed send is retried on the next cycle.
Rows older than `KEEP_DAYS` (90) are pruned hourly — a trend, not a ledger.

## Payment flow (manual)

```
User                     Bot                        Admin
 │  /subscribe            │                           │
 ├───────────────────────▶│  list plans               │
 │  pick a plan           │  create txn (pending)     │
 ├───────────────────────▶│  show card number         │
 │  send receipt photo    │  forward photo + buttons  │
 ├───────────────────────▶├──────────────────────────▶│  ✅/❌ (inline)
 │                        │  grant premium on approve │
 │  ◀─────────────────────┤  notify user              │
```

Add another payment method: subclass `PaymentStrategy` (see `services/payments/manual.py`),
register it in `build_payment_service()`, use its `method` key in the UI. Nothing else changes.

## Known limitations

- Cache only stores *successfully uploaded* files; quota is claimed when the download
  actually starts (so failed extractions don't burn quota, but queued items don't reserve it).
- Cache keys include the format, so a video and its MP3 are two independent entries — switching
  format re-downloads once, then hits the cache.
- A task requeued at shutdown is re-processed from scratch and may claim a second quota slot.
- Playlists are rejected by design (`noplaylist`); live streams are rejected.
- Sites that yt-dlp can't extract from (or that block the datacenter IP) surface as friendly
  errors with retries — not crashes.
- Cache hits don't count toward the daily quota (they're free re-sends).

## Testing

```bash
# Unit tests — no Postgres/Redis needed
.venv/bin/python -m pytest

# Lint + types
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy .

# Integration smoke test — needs Postgres + Redis (docker compose up -d postgres redis)
.venv/bin/python scripts/smoke.py

# Offline wiring check — needs Postgres only, makes no Telegram API calls
.venv/bin/python scripts/boot_check.py
```

The smoke test covers schema/seed, user upsert, quota claiming, transaction lifecycle, cache
roundtrip (including the per-format key), queue roundtrip/requeue, an **idle queue read** (the
socket-timeout regression above), extractor probing and a live metadata extraction (network
dependent, reported as a warning when it can't run).

The boot check additionally idles past `BLOCK_TIMEOUT_S` and asserts the workers are still
alive — catching the whole class of "worker died silently after startup" bugs that unit tests
cannot see.

---

*Use responsibly and respect each platform's Terms of Service and copyright rules.*
