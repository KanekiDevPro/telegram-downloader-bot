# Deployment

Two supported targets: a **VPS/dedicated server with Docker**, and a **shared
Python host / bare VPS without Docker**. Pick one; both run the same code.

> **Read this first.** Regardless of target, the bot needs **PostgreSQL**
> (`asyncpg` is the only database driver). A typical Iranian "هاست اشتراکی
> تلگرام" panel gives you Python + a process manager but *not* Postgres — in
> that case point `DATABASE_URL` at a managed Postgres (Neon, Supabase, a small
> VPS, or your panel's Postgres add-on). **Redis is optional**: without it the
> bot uses an in-memory queue and FSM state (`QUEUE_BACKEND=memory`), which is
> fine for one small instance but loses queued jobs on restart and cannot be
> scaled to multiple processes.

---

## Option A — Docker (recommended for a VPS)

```bash
git clone <your-repo> telegram-downloader-bot
cd telegram-downloader-bot
cp .env.example .env
# edit .env: BOT_TOKEN, ADMIN_IDS, MANUAL_CARD_NUMBER, MANUAL_CARD_HOLDER
# and, for the local Bot API server: TELEGRAM_API_ID + TELEGRAM_API_HASH
# (https://my.telegram.org -> API development tools)

# optional but recommended for YouTube: put a real Netscape-format cookie
# export in the project root as cookies.txt (see the note below — an empty file
# is worse than no file)

docker compose up -d --build          # bot + postgres + redis + cobalt + the two YouTube helpers
docker compose --profile local-api up -d --build   # + telegram-bot-api, 2000 MB uploads
docker compose logs -f bot

# The two YouTube helpers (a PO-token provider and a session server) run by default:
# they are the login-free routes past YouTube's bot check, and neither is required
# for the bot to work. To leave them out, name the services you do want:
#   docker compose up -d --build bot db redis cobalt
```

### YouTube: bot checks, PO tokens and JavaScript

YouTube's "Sign in to confirm you're not a bot" has two very different causes, and they look
identical in the logs. Rule out the first before spending money on the second.

**1. The cookie jar is not actually logged in.** yt-dlp signs in only when the jar holds
`LOGIN_INFO` *and* a SAPISID-family cookie (`SAPISID` / `__Secure-1PAPISID` /
`__Secure-3PAPISID`); otherwise every request is anonymous and the bot check appears wherever
you run it. Those cookies are HTTP-only, so exporters silently drop them, and `LOGIN_INFO`
disappears when Google rotates a session — a jar with 20 other cookies still proves nothing.
`boot_check.py` and the bot's startup log name the missing cookies:

```
WARNING COOKIE_FILE=./cookies.txt loads, but has no YouTube login: LOGIN_INFO missing.
```

**1b. Everything is configured and YouTube still refuses.** Run the doctor instead of guessing:
`/doctor` in Telegram (admin only) or `python scripts/youtube_doctor.py` (`--no-probe` = offline).
It checks the login, JavaScript runtime, PO-token provider, ffmpeg and the optional proxy, probes a
real video, and prints one verdict plus the single next step:

```
🩺 دکتر یوتیوب
✅ کوکی: /cookies/cookies.txt — لاگین یوتیوب کامل است
✅ منبع کوکی: /cookies/cookies.txt — mount /cookies (9p، فقط-خواندنی)، مسیر میزبان:
   /Users/mo/bot، 31 کوکی، اکسپورت 4 دقیقه پیش؛ از همین اکسپورت استفاده می‌شود
✅ JS runtime: deno
✅ PO token: http://pot-provider:4416 — v2.0.0
🎫 سرور سشن یوتیوب: http://yt-session-generator:8080 — توکن آماده، ساخته‌شده 6 دقیقه پیش؛ کوبالت هر ۵ دقیقه خودش دوباره می‌خواند
⛔️ تست زنده: SESSION_STALE: یوتیوب این درخواست را نپذیرفت (سشن کهنه است)…

حکم / قدم بعدی: دقیقاً همان کاری که باید بکنید
```

The `منبع کوکی` line answers "I re-exported — did it take?" without touching a terminal on the
server: it names the mount (`/cookies`, read-only) and the host directory behind it, when the jar
was exported, and whether the copy yt-dlp reads is already that export. A newer export shows as a
warning, since the next download picks it up on its own.

**And you do not even have to run the doctor.** A fresh export reaches the next download by
itself, which means nothing tells you whether it has happened yet — so the bot polls the jar
(`COOKIE_WATCH_INTERVAL_S`, default 60s) and sends every admin one message the first time it sees
an export the running bot has not loaded:

```
🍪 کوکی تازه اکسپورت شد، ولی ربات هنوز برنداشته
فایل: /cookies/cookies.txt
منبع: mount /cookies (9p، فقط-خواندنی)، مسیر میزبان: /Users/mo/bot
جار: 24 کوکی، اکسپورت همین حالا
وضعیت: ربات هنوز روی اکسپورت قبلی است

👉 دانلود بعدی خودش این نسخه را برمی‌دارد؛ ری‌استارت یا ری‌بیلد لازم نیست.
```

One message per export: a bot restart is the baseline and never alerts, and a jar that a download
already took is silent (the alert means *not picked up yet*). If the new jar carries no YouTube
login — the failure that masquerades as a blocked IP — the same message says so and points at
`scripts/export_cookies.py`. Set `COOKIE_WATCH_INTERVAL_S=0` to turn it off; without `ADMIN_IDS`
there is nobody to tell, and the bot logs that instead.

The alert is not a dead end: its **«بررسی همین حالا»** button runs the same doctor and edits the
message with the verdict, so the check happens from the chat you are already reading. It stays
admin-only — a forwarded alert answers "⛔️ فقط ادمین می‌تونه." for anyone else.

**Spotify is never downloaded *from* Spotify.** Both engines refuse the link itself — yt-dlp by
policy (its `KnownDRMIE` list matches `open.spotify.com`) and the embedded cobalt because it has no
Spotify service — so the bot rewrites the link to the same song on YouTube instead: title, artists
and duration are read from Spotify's public embed page (no login, no key), and the candidate whose
length is closest is downloaded through the same YouTube path as everything else, cookies, PO
tokens, proxy and cobalt fallback included. Nothing to configure. If YouTube refuses the *search*,
the failure is an ordinary block: it is recorded for the digest, the admins are alerted, and
`/doctor` explains it.

Before it is sent, the watcher also *uses* the new jar once: a metadata-only extraction (no
download, seconds) of one public video, reported in the same message as `✅ تست با همین اکسپورت …`
(YouTube accepted it) or `⛔️ … همان لاگین ناقص … نه IP` (a complete login would make that line say
the opposite — the IP). So the alert answers "did my export work?" by itself; a probe that cannot
run says so and never blocks the alert.

Users get the same diagnosis instead of a generic failure whenever the block is one the jar
explains (no usable jar, or a YouTube link with a jar that cannot sign in): "این لینک فقط با یک
حساب واردشده قابل دانلود است … موضوع را به ادمین اطلاع دادیم". The matching admin notice is
rate-limited to once per 10 minutes, since a logged-out jar fails every link while each user still
gets their own answer.

A YouTube link is also checked *before* it is queued: a jar that cannot sign in adds a note to the
queued message (most videos still extract anonymously), and once a refusal has actually been
observed the next link is not queued at all — the user is told why instead of waiting for the
failure, and no quota is spent. A successful anonymous download or a fresh signed-in jar clears
that state immediately.

The mechanical part can also be automatic: `COOKIE_AUTO_EXPORT=edge:Default` (same syntax as
`COOKIES_FROM_BROWSER`) makes the bot re-export the jar from that profile the first time a download
fails because YouTube treated it as anonymous — at most once per 30 minutes, never overwriting a jar
that does sign in, written atomically, then probed with the verdict sent to the admins. Inside a
plain container there is no profile to read, and the admins are told exactly that (the export belongs
on the host, where the browser is).

Three login failures inside an hour page the admins immediately with the same report, headed by what
happened, instead of waiting for the weekly one — threshold and cooldown apply, and only the fixable
cause counts.

The alert's second button (**«♻️ اکسپورت دوباره»**) and `/refresh` do the export on demand —
`/refresh edge:Default` names a profile without a restart, and the 30-minute cooldown does not apply
to a human's request. Both ask the cheap question first: if this machine has no profile for that
browser (the plain-container case), the attempt is skipped and the message names the paths we looked
at and the command to run where the browser is. `boot_check.py` reports the same thing at startup.

When the login itself is what is broken, `/fixlogin` prints the diagnosis and the steps, and
`python scripts/fix_login.py` walks them on the machine with the browser: it detects a jar that reads
but cannot sign in, lists the profiles this machine has, exports, checks the login landed before
saying anything, probes YouTube, tells the admins, and records the fix (`--dry-run` changes nothing).

On Windows, Chrome 127+ / Edge / Brave lock the cookie database with **App-Bound Encryption**: the
profile exists, no outside tool can decrypt it, and yt-dlp fails with a generic "failed to load
cookies". The wizard reads the browser's `Local State`, marks those profiles as locked, and —
instead of an attempt that cannot work — names the two ways through: an export extension that keeps
the HttpOnly rows, or Firefox (which has no such lock).

`/trend` (14 days) shows the failures per day, marks the last jar fix on its own day, and weighs the
failures on either side of it — by daily rates, and only once six hours have passed, because one
quiet afternoon after an export is not evidence that it worked.

Failures are recorded per cause, and the maintenance loop sends the admins a **weekly digest**
(`/blocks` prints it on demand): `login` means re-export the cookies, `ip` means a proxy or the token
provider, `session` is transient, and `site` is the link's own fault. A week with no failures stays
silent and changes nothing (the stamp records an actual delivery, so a quiet week cannot consume the
window); rows older than 90 days are pruned.

`/blocks` ends with the safety net's health, because the counts above read differently depending on
it: **which** instance, in what state, and what happened **the last time a real blocked link needed
it** — served it, failed, or was never asked, with the reason and how long ago. That last part is
written by the worker on real traffic (so it survives restarts and outlives the 10-minute
quarantine), and it is the fastest way to notice that the fallback has become decorative without
crawling the logs — the reason an instance was skipped is printed as it was skipped.

It separates a stale session (YouTube's "The page needs to be reloaded" — a rotated login or a
visitor binding the provider has not refreshed; a retry often clears it, then a fresh jar, then the
provider) from a real block.

A stale session is retried automatically before the user sees anything
(`EXTRACTOR_RETRY_ATTEMPTS=2`, base `EXTRACTOR_RETRY_BACKOFF_S=3` → 3s then 6s, per attempt logged).
Blocks are not retried on purpose: a flagged IP fails the same way every time, and a worker slot is
worth more than a fourth identical failure. The doctor's own probe always runs once, so its verdict
describes the first response rather than a retried one.

**2. The IP is flagged.** Only meaningful once the login is complete; typical on VPS ranges,
filtered or VPN exit IPs. Levers:

| Lever | How |
|---|---|
| `YTDLP_PROXY` | `socks5://user:pass@host:1080` — a *residential* exit is the reliable fix on a blocked IP; a cloud one is what the check targets |
| `YTDLP_POT_PROVIDER_URL` | `http://pot-provider:4416` — on by default (its container ships with the stack) |
| `YOUTUBE_SESSION_SERVER` | `http://yt-session-generator:8080` — the same idea for the fallback engine; also on |
| `COOKIES_FROM_BROWSER` | reads a browser profile directly; ignored (with a warning) when unreachable |

The PO-token provider (bgutil) runs in its own container and is unauthenticated — it
publishes `127.0.0.1:4416` only, never expose it. It needs the
`bgutil-ytdlp-pot-provider` plugin, which the image installs; a configured provider URL
without the plugin is detected and reported at startup instead of silently doing nothing.
Two things about it that `/doctor` now puts in the same line: the plugin *rejects* a server
whose major version differs from its own (the image is `:latest`, so a pull can drift the two
apart — and then every download silently loses its token), and a provider that is down is
survivable rather than fatal (the bot drops the URL at startup only when the server really
does not answer, and yt-dlp skips an unreachable provider either way).

Neither helper has to start before the bot: both are optional at runtime, so a stack started
without them (name the services you want: `docker compose up -d bot db redis cobalt`) keeps
serving links, and the doctor reports which route is missing instead of failing a download.

**Both are watched, not just probed on request.** ``/doctor` is one admin asking one question at one
moment; `HELPER_WATCH_INTERVAL_S` (300s) is the machine asking every few minutes, which is the
difference between "is it up?" and "when did it break, for how long, and does anyone know?". A
state change is a row in `helper_events`, ten minutes down is a page (once per hour per helper), a
recovery is a message, and the weekly digest carries the paragraph: which helper, how many hours,
apex what it cost the users. Drift — a provider the plugin rejects over a version major — counts as
down, because it answers everything and mints nothing. `0` turns the watch off; the sweep is local
traffic and cannot break a download.

The measured answer to "which proxy fixes YouTube?": with `YTDLP_PROXY` set, yt-dlp **did** use it
(a bogus port fails with *Connection refused* instead of the bot check), and an Azure exit got the
identical bot check — a datacenter address is what the check is for. Same for the fallback, once
its own proxy is wired correctly: `COBALT_HTTP_PROXY` reaches the cobalt container as
`API_EXTERNAL_PROXY`, which is the only name cobalt reads (`HTTP_PROXY`/`HTTPS_PROXY` are ignored
by its dispatcher — setting those was a no-op), and with a cloud exit it answers the same login
error as without one. Order of fixes stands: a jar that signs in, then a residential exit.

The image also ships the **Deno** JavaScript runtime, because yt-dlp degrades YouTube
extraction without one ("some formats may be missing"). Host deployments pick up whatever
runtime is installed via `YTDLP_JS_RUNTIME=auto`.

Regenerate cookies whenever downloads start failing again:

```bash
python scripts/export_cookies.py --browser chrome   # run where the browser lives
# nothing else: the jar is mounted, re-read per download, and a restart is only
# needed if you changed the mount itself
```

**2b. Nothing works and users are still waiting?** The fallback extractor is the safety net for
exactly this: when yt-dlp comes back *blocked* (`EXTRACTOR_BLOCKED`, or `SESSION_STALE` after its
retries), the link is handed to a Cobalt instance instead, the file is streamed into the same job
directory, and the user sees a normal download instead of «🚧». The failure is still written to
`block_events`, so `/blocks` and `/trend` keep showing that the primary engine is degraded. Empty
`COBALT_API_URL` = off, and every other failure (private, geo, live, playlist) is untouched — it is
not the fallback's job.

Two other kinds of link belong to it as well. **Image posts** have no video for yt-dlp at all
(«No video could be found in this tweet»), so they are handed over and arrive as a **photo** — or as
a **media group** when the post has several pictures, each downloaded in the post's own order.
And **Spotify** links are refused by *both* engines, so they are rewritten to the same song on
YouTube before anything is tried (see the note below). Both need no configuration: with the cobalt
service running, they work.

**It ships with the stack, so there is nothing to configure.** `docker-compose.yml` runs
`ghcr.io/imputnet/cobalt:10` as a `cobalt` service (`init: true`, `restart: unless-stopped`, port
published on `127.0.0.1:9000` only — it is unauthenticated), and `COBALT_API_URL` defaults to
`http://cobalt:9000`. No public instance, no signup: `api.cobalt.tools` is not a usable default any
more (its v7 endpoint was retired in Nov 2024 and its v10 API refuses anonymous callers with
`error.api.auth.jwt.missing`).

**Be clear about what "embedded" means on a VPS:** the instance shares the host's address, so it
is *not* another address for an IP-level block. It fixes a broken extractor, a site that refuses
yt-dlp, and YouTube's per-client bot check once it has a session — but a flagged IP is still fixed
by a clean proxy (`YTDLP_PROXY`). If you want the fallback to leave through a *different* path,
give it its own proxy instead (it does not inherit `YTDLP_PROXY`):

```bash
# .env, then: docker compose up -d cobalt
COBALT_HTTP_PROXY=socks5h://user:pass@host:1080
```

**YouTube needs a cookie inside cobalt too** (it has no login of its own) — and it is generated
from the bot's own jar, so one export signs both engines in. The bot writes `cookies.json` (cobalt's
shape: service → `Cookie:` header strings) into `COBALT_COOKIES_DIR`, the host directory compose
mounts into both containers, at startup and on every export the watcher sees. Two things matter in
production:

- **Cobalt reads it once, at startup** and then keeps its own refreshed copy in memory (writing
  changes back into the file). So a fresh export is one command away from live:
  `docker compose restart cobalt`. The jar alert says that in the message, and `/doctor` compares the
  sidecar's stamp with the instance's own start time, so "the running instance is on the old
  version" is a fact you can read, not an mtime to interpret.
- **A file the bot did not write is never replaced.** A hand-made `cookies.json` — including the
  flat-array shape one of cobalt's docs examples suggests — is reported by `/doctor` and left alone;
  other services in it (`instagram`, `twitter`, …) survive every regeneration.

A logged-out jar is still written on purpose: cobalt only retrieves YouTube's player — and its
bot-check handling — when it has *a* cookie (`retrieve_player = Boolean(sessionTokens || cookie)`).
For an IP even a login does not satisfy there is a **cookie-free** route, and it also ships with the
stack: `ghcr.io/imputnet/yt-session-generator:webserver` as a `yt-session-generator` service, which
runs a real Chromium (under Xvfb) and hands out a `po_token` bound to this host's public IP on
`GET /token`. `YOUTUBE_SESSION_SERVER` defaults to it, and cobalt reads it through its own loader —
which means three things are worth knowing:

- **It is a browser, so it is the one helper that can cost something.** Its first token can take a
  few minutes (cobalt logs `[✓] poToken & visitor_data loaded successfully!` once it has one), and it
  needs the host's Chromium to be able to play a real YouTube embed. Where it cannot — a locked-down
  host, or an IP YouTube will not even serve a player to — no token ever appears. `/doctor` reports
  that state (`🎫 سرور سشن یوتیوب — توکن هنوز ساخته نشده`) and names the log to read, instead of
  implying the fallback has a session it does not have.
- **It needs `shm_size: 1g`, and that is not a tuning detail.** Chromium dies on Docker's 64 MB
  `/dev/shm`, the generator process then exits, and `restart: unless-stopped` brings it back every
  ~13 seconds — forever. The port still answers briefly between restarts, so the loop is invisible
  from the outside; the setting is in `docker-compose.yml` (and asserted by `boot_check.py`) because
  the symptom otherwise reads as "the browser cannot mint a token", which is a different problem.
- **No restart is needed for a fresh token.** Cobalt re-reads the server every 5 minutes on its own
  (that interval is built into the instance), so a token that appears after cobalt started is picked
  up without touching anything.
- **It is optional at runtime.** An unreachable server is a log line in cobalt, not a crash; the
  login jar and the PO-token provider are independent routes, and `/doctor` gives each its own line.

Without any of this a YouTube link is answered with `error.api.youtube.login` — TikTok, Instagram,
Twitter, Streamable and the rest work regardless.

Its state is reported in two places, and neither needs a log hunt: `/doctor` probes it live, and
`/blocks` shows the same section from facts that cost nothing — a quarantine in the running process
plus the recorded outcome of the last real link. An embedded instance is labelled **(داخلی)**, since
"the instance we run" and "an instance somewhere else" differ in exactly the way that matters. A 🟢
probe with *«آخرین لینک بلاک‌شده: ❌ خودش هم نشد»* underneath means the instance answers your test but
not the links — the case a probe alone would hide.

Two operational details worth knowing on a VPS. **The two addresses are handled for you:** every
helper answers to its compose name inside the network and to its published loopback port on the
host (`cobalt:9000`/`127.0.0.1:9000`, `pot-provider:4416`, `yt-session-generator:8080`), and the
client or probe follows whichever exists — so `boot_check`/`smoke`, which run on the host by design,
do not report a healthy stack as unreachable. And **don't trust a resolved URL blindly:** services hand
out malformed ones (streamable's own API answers every video with a doubled scheme, `https:https://…`),
so a link is repaired where it enters the client, with one log line naming it — and `boot_check`
asserts the result is fetchable rather than discovering it inside a transfer.

Other levers:

- **Both API shapes are spoken.** The client sends the documented v7 request
  (`/api/json` + `vQuality`/`filenamePattern`) first, then the current one (v10 moved the API to the
  instance root and renamed the fields), and remembers whichever answered — one wasted round trip per
  process, not per link. An instance that fails as an *instance* (auth, rate limit, 5xx, unreachable)
  is left alone for 10 minutes, so a keyless instance costs one probe instead of one probe per
  blocked link.
- **A YouTube-only gap is not a broken instance.** `error.api.youtube.login` names one service, so
  the fallback stays available for the others and the report says exactly that.
- **Its own proxy**, and for an instance you host *elsewhere*: `COBALT_PROXY` (bot → instance) is
  separate from `COBALT_HTTP_PROXY` (instance → the internet) on purpose, and neither is inherited
  from `YTDLP_PROXY`. A key is sent as `Authorization: Api-Key …` when `COBALT_API_KEY` is set.
- **What it cannot do.** Titles come from the downloaded file's name (no duration, no thumbnail),
  album/playlist links stay refused, and the audio path is converted server-side (no ffmpeg
  needed).

Check it before you need it: `python scripts/boot_check.py` resolves one real link through the
configured instance and warns (never fails) when it cannot — an unreachable fallback degrades to
exactly the behaviour this deployment had before it existed.

### Cookies

Export cookies for the sites you download from (a browser extension that writes Netscape
format) and save them as `cookies.txt` in the project root. `COOKIE_FILE` defaults to that
path, and the bot only passes the file to yt-dlp when it actually parses as a non-empty cookie
jar — an **empty or truncated `cookies.txt` would otherwise make yt-dlp fail on every link**,
so it is skipped with a warning instead. Fix it by re-exporting, or point `COOKIE_FILE` at a
blank value to disable cookies entirely.

Inside Docker, the project directory is mounted read-only at `/cookies` and `COOKIE_FILE` points
at `/cookies/cookies.txt`, so a fresh export takes effect on the next download — no rebuild, no
restart, and the container never writes back to the file that holds your login. yt-dlp itself
rewrites its cookiefile when a download finishes, so the bot copies the jar to a writable place
first; because the mount is read-only, the copy is the only thing yt-dlp can touch. Set
`COOKIES_HOST_DIR` (compose interpolation) to mount a jar kept somewhere else.

The mount is a *directory* and not the file, and that is deliberate: bind-mounting a file whose
host path does not exist makes Docker create a directory there, after which the container refuses
to start ("not a directory: Are you trying to mount a directory onto a file"). That is exactly
the state of a fresh clone, where the jar is optional. With a directory source an absent jar is
simply a missing file inside the container: the bot logs one warning and keeps serving every site
that does not need cookies.

### The `telegram-api` service

The cloud Bot API only accepts bot uploads up to 50 MB, so with `MAX_FILE_SIZE_MB=2000`
every real video would fail. The `telegram-api` service removes that limit (2000 MB uploads,
files served from disk with `--local`). It needs `TELEGRAM_API_ID` / `TELEGRAM_API_HASH`
from my.telegram.org and **exits immediately without them**, so it lives behind the
`local-api` profile — `docker compose up -d` will not start it, and nothing restart-loops:

```bash
# with credentials in .env
docker compose --profile local-api up -d --build

# cloud only (default)
docker compose up -d --build
```

If `TELEGRAM_API_BASE_URL` points at the server while the profile is off, the bot logs the
connection failure and **falls back to the cloud API**, lowering the effective per-file
ceiling to 50 MB so a doomed download is rejected before it starts (rather than after). It
never exits because of this — check `docker compose logs bot` for the `falling back` line.

Point the bot at the server with `TELEGRAM_API_BASE_URL`:

| Bot runs | Value |
|---|---|
| inside `docker compose` | `http://telegram-api:8081` |
| directly on the host | `http://localhost:8081` |

A bot can be served by either the cloud API or a local server — don't point two setups at the
same token at the same time.

`docker-compose.yml` starts Postgres, Redis and the bot. The compose service
overrides `DATABASE_URL` / `REDIS_URL` to the in-network hostnames, so the
`localhost` values in your `.env` stay valid for the host-based workflow.

Useful commands:

```bash
docker compose ps                     # status
docker compose restart bot            # apply .env changes
docker compose up -d --build          # apply code changes
docker compose exec bot python scripts/boot_check.py   # wiring check (scripts are not baked in; see note)
```

> The image intentionally excludes `scripts/` and `tests/`. Run the smoke and
> boot checks from the host (`docker compose up -d postgres redis` first) or
> copy the file in with `docker compose cp scripts/smoke.py bot:/app/scripts/`.

### Upgrading yt-dlp

Sites break extractors regularly. yt-dlp ships a fix within days, so pin to
latest and rebuild:

```bash
docker compose build --pull --no-cache bot && docker compose up -d bot
```

---

## Option B — Shared Python host / bare VPS (no Docker)

```bash
bash deploy/install.sh      # venv + deps + ffmpeg check + .env seed
# edit .env
bash deploy/run.sh          # foreground test run
```

Then keep it alive with whichever supervisor the host provides.

### systemd (VPS, full control)

```bash
sudo useradd --system --home /opt/telegram-downloader-bot bot
sudo cp -r . /opt/telegram-downloader-bot
sudo chown -R bot:bot /opt/telegram-downloader-bot
sudo -u bot bash /opt/telegram-downloader-bot/deploy/install.sh

sudo cp deploy/telegram-downloader-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-downloader-bot
journalctl -u telegram-downloader-bot -f
```

### supervisor / cPanel-style panels

Paste the values from `deploy/supervisor.conf` into the panel's "Create
application" form. The essentials:

| Field | Value |
|---|---|
| Command | `/opt/telegram-downloader-bot/.venv/bin/python main.py` (or `bash deploy/run.sh`) |
| Directory | `/opt/telegram-downloader-bot` |
| Restart | always |
| Stop signal | `TERM` (the bot needs it for a clean drain) |

### Webhook mode

If the host forbids long-running processes or you prefer pushing updates:

```bash
BOT_MODE=webhook WEBHOOK_URL=https://bot.example.com WEBHOOK_SECRET=... bash deploy/run.sh
```

Terminate TLS in the panel/nginx and proxy `POST /webhook` to
`WEBHOOK_HOST:WEBHOOK_PORT` (default `0.0.0.0:8080`).

---

## Checklist before going live

1. `BOT_TOKEN` from [@BotFather](https://t.me/BotFather).
2. `ADMIN_IDS` — comma-separated numeric Telegram ids. The admin receives
   payment receipts and owns the Approve/Reject buttons. Without it the bot runs
   but nobody can approve a purchase.
3. `DATABASE_URL` reachable **from the bot process** (test with
   `python scripts/boot_check.py`).
4. `MANUAL_CARD_NUMBER` / `MANUAL_CARD_HOLDER` — shown during checkout.
5. `ffmpeg` installed if you want MP3 downloads (`MAX_FILE_SIZE_MB` also caps
   what users can request; Telegram's bot limit is 2 GB).
6. `WORKER_COUNT` ≈ number of CPU cores you can spare for transcoding.
7. `cookies.txt` in place if you expect YouTube traffic (`COOKIE_FILE` defaults to it).
8. `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` set if you want the local Bot API server
   (otherwise clear `TELEGRAM_API_BASE_URL` and start only `postgres redis bot`).
9. Quotes/limits: `DEFAULT_DAILY_LIMIT`, `PREMIUM_DAILY_LIMIT`, and the plan
   prices seeded in `subscription_plans` (edit them in the DB after the first
   start — seeding is one-shot).

## Backups

Only Postgres holds state (users, transactions, `smart_cache` mappings of URLs
to `telegram_file_id`s). Losing `smart_cache` costs re-downloads, not content.

```bash
docker compose exec postgres pg_dump -U downloader downloader | gzip > backup-$(date +%F).sql.gz
```
