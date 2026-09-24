# Changelog

All notable changes to this project are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); the schema history itself lives
in `core/database.py` (idempotent DDL — there are no migration files; see
[docs/RELEASE_RUNBOOK.md](docs/RELEASE_RUNBOOK.md)).

## Unreleased — production release candidate (2026-09-24)

### UX — one-message product

- Full navigation rework: first-run language selection, Home → Download /
  Profile / (Admin, admin-only), every screen edited in place in one message
  (TAP → WAIT → MEDIA). The Help section is removed entirely.
- Media cards redesigned with grouped vertical spacing: video (`🎬 title`,
  `🎞 quality • size`, `🔗 source`, `🤖 bot`) and audio (`🎵 title`, `🎤 artist`,
  `💿 album · year`, `⏱ duration`, `🎧 format · rate · size`, `🔗 source`).
- Ephemeral chat-action pulses (typing / upload actions) instead of progress
  messages; a successful download is exactly **two** user-visible messages
  (card + file), pinned by end-to-end regression tests.
- Group support: the bot works in groups/supergroups (send a link, choose,
  receive), with an «add to group» button using Telegram's real `?startgroup`
  deep link generated from the live bot username.
- Copy rewritten throughout: natural, concise Persian/English; no robotic
  status sentences.

### Media & audio capability

- **Source-first format discovery** (`AudioCapability` in `services/extractor.py`):
  the format grid and bitrate rows are driven by one capability model — what
  this source, engine and transport can actually produce. No generic catalogue.
- **FLAC** added alongside MP3 / M4A / OPUS / WAV where it is genuinely
  producible *and* deliverable within the transport limit (lossless for long
  tracks is hidden on the 50 MB cloud transport, offered on the 2 GB local one).
- Real bitrate ladders (320/256/192/128/… kbps) with per-row size estimates
  computed from duration × bitrate; abstract labels (Best/High/Balanced/Small)
  are gone.
- **Source-bitrate probing**: output rates above the source's own rate are not
  offered as quality improvements (`ℹ️ Source quality: …` footnote when the
  ladder is trimmed); the untouched-stream tier (`M4A · Original`) exists only
  when the source stream really is AAC-in-M4A.
- Selection↔file agreement: a post-conversion **container guard** raises
  `CONVERSION_MISMATCH` instead of ever delivering a substituted file; captions
  name the file by its produced container and only show a bitrate when the
  requested codec is what happened.
- **Video quality ladder from real probed formats**: only resolutions the URL
  actually has (up to `1440p (2K)` / `2160p (4K)`), per-row size estimates with
  an honest `size unknown` marker, no "best / up to / smallest" wording anywhere.
- Spotify links resolve to their real track metadata (title, artist, album,
  year, duration, cover) and drive tags, captions and size estimates.
- **Three honest audio states, per URL** (`AudioCapability.native` /
  `.converted`): each offered format is labelled as *native* (the source stream
  really is that container/codec and is passed through), *converted* (reachable
  through the supported transcode path — source audio and ffmpeg both present),
  or is simply **not offered** when the complete pipeline cannot deliver it. A
  converted MP3 is never presented as source-native.
- **No empty menus, no silent defaults** (intake): a video link whose qualities
  cannot be discovered gets an explicit localized "not discovered" message with
  a retry that re-extracts — never a generic download button quietly fetching
  whatever yt-dlp picks. An automatic "best available" row exists only behind
  the opt-in `MENU_AUTO_BEST` flag and names itself as an automatic pick, not an
  exact quality.
- **Spotify honesty**: when the track lookup cannot resolve a Spotify link, the
  user is told so (with a retry) instead of being shown a fabricated format
  grid. Quality, bitrate, codec and size are never invented — estimates stay
  marked as estimates (`≈` / `size unknown`).

### Extraction & routing

- **Reddit (and friends) canonicalization**: `/s/` share links and media-viewer
  wrappers (`reddit.com/media?url=…`) are unwrapped/resolved before extractor
  selection; the intake gate consults the bot's platform families, so legitimate
  links reach the engines and "unsupported" is only said after both engines
  genuinely refuse.
- One duplicate metadata extraction removed per job (3 calls → 2); per-stage
  timing logged per job (`stages probe=… download=… convert=… upload=…`).

### Delivery & reliability

- **ffprobe delivery verification** (`services/verify.py`): before upload, the
  produced file is measured (container, codec, bitrate, duration, sample rate,
  channels, dimensions) against the caption's claim. A measured contradiction
  fails the delivery (`CONVERSION_MISMATCH`) instead of captioning a lie;
  ffprobe being unavailable is logged and does not block delivery (fail-open —
  see the module docstring for the full policy).
- **Transport-aware upload limits**: the file ceiling mirrors the transport
  actually enforcing it (2 GB with a `--local` Bot API server, 50 MB otherwise).
- Truthful failure categories: `CONVERSION_MISMATCH`, `FILE_TOO_LARGE`,
  `DELIVERY_FAILED` (plus the existing block/DRM/ffmpeg/timeout codes) — user
  text by code in both languages, precise code kept in logs and telemetry.
- **Delivery verification extended**: a produced file that is missing or empty,
  or that contains **no media streams at all**, fails before upload; the
  delivered video height is checked against the user's *selection* as well as
  the caption's claim, so a delivered rung that differs from the chosen one is
  a clear failure instead of a silent send. A source-quality upscale is logged
  as an INFO observation (`source-quality upscale observed for …`) — reported,
  never a verdict and never user-facing.

### Cache

- `smart_cache` remembers `kind`, `title` and the produced quality `label`, so
  a cached replay is byte-for-byte the fresh-send card (nullable columns — old
  rows replay safely and simply omit what they never knew).

### Groups & admin analytics

- `group_downloads` telemetry (chat id, known title, outcome, failure code —
  never content) with indexed aggregation.
- Admin panel **Groups** screen: totals, top groups, failure-code breakdown, and
  **week-over-week trends** — trailing 7 whole local days vs the 7 before
  (volume + movement, failure rate + movement), aggregated in SQL.
- All admin screens remain server-side authorized per tap.

### Admin — editable texts & category panel

- **Editable user-facing texts**: every user-facing catalogue family (start &
  menu, language, link intake, media prompts, download progress, errors,
  profile/premium, payments) can be viewed, edited per language, previewed,
  saved and reset to its default from the admin panel, grouped by feature.
  Replacements are validated at the desk — bounded length (≤ 4000 chars), only
  the default's own `{placeholders}` (an override can never smuggle a new field
  into a `str.format` call site), balanced Telegram-HTML with supported tags and
  sane link schemes — so an invalid edit is refused instead of breaking every
  send that uses it. Operator screens (`admin.` / `panel.`) and the command
  menu (`cmd.`) are deliberately locked. Edits persist in the new `bot_texts`
  table, a reset is a delete (catalogue default takes over), and every save and
  reset is recorded in the existing `fix_events` audit trail.
- **Admin panel reorganized into six category submenus** — Users & groups,
  Downloads & media, Sources & extractors, Messages & localization, System &
  configuration, Diagnostics & maintenance — each screen with Back/Home
  navigation and per-tap server-side authorization. The obsolete **recent
  failures** and **failure trends** buttons and their callbacks are removed
  (old callback names are answered safely, not errors); the failure-code
  breakdown remains under Diagnostics.

### Release-blocker hardening

- **Text overrides are coherent across processes, without restarts**: the
  database stays the source of truth, one Redis counter (`bot:texts:version`)
  is the invalidation signal, and every process refreshes its in-memory
  snapshot at a rate-limited sync (update middleware + worker loop). With no
  reachable Redis, the fallback is a bounded **30 s TTL** re-read — staleness
  is bounded either way, a reload failure keeps the last known texts live, and
  synchronous `t()` still never touches the database.
- **Migration safety proven** (offline statement-semantics tests + an opt-in
  live proof, `tests/test_schema_live.py`, against fresh / pre-upgrade /
  repeatedly-initialized databases): the DDL is additive-only, a pre-upgrade
  database only gains `bot_texts`, repeated `init_db` is a no-op, and rollback
  needs no `DROP` — a text reset is one `DELETE`.
- **Upscaling is named where the label is read**: a target rate over a weaker
  source (320 kbps from ≈130 kbps) now carries `media.upscale_mark` on the
  quality label — caption *and* cached replay — instead of only a log line.
  Native vs converted audio stay distinct in every caption («Original» only
  for the copied stream), and the video height tolerance (±5%, ≥16 px) is
  pinned as the documented contract.
- **Whole-flow regression coverage** (`tests/test_flow_end_to_end.py`, offline
  fixtures): URL → probe → capability → menu → callback → result →
  verification → caption, for a multi-rung YouTube link, an undiscovered
  ladder, resolvable/unresolvable Spotify links, stale taps, a 720p selection
  delivered as 480p (fails before any send), and native vs converted audio.
- **Text-editor concurrency defined and audited**: edits are last-write-wins
  over the upsert, and every save/reset writes a `fix_events` row with actor,
  key, language (row timestamp) and the **before/after values** — an
  overwritten edit is never lost from the history.
- **Deployment compatibility documented** (`docs/RELEASE_RUNBOOK.md` §9):
  schema init strictly before traffic, one restart covers all workers,
  `MENU_AUTO_BEST` defaults to false on upgrade, the ffmpeg/ffprobe checks and
  exact post-deploy commands/health checks.

### Database

Additive, idempotent schema changes applied automatically at startup
(`core.database.init_db`): `users.language`; `smart_cache.kind` / `title` /
`label`; new tables `block_events`, `bot_state`, `bot_texts`, `fix_events`,
`helper_events`, `group_downloads` (+ indexes). No destructive migration, no
data reset. `bot_texts(key, lang, value, …)` holds admin text overrides —
deleting its rows restores every default.

### Docs

- Bilingual README (EN + FA).
- `docs/RELEASE_RUNBOOK.md` — deployment order, env/secrets, schema notes,
  smoke tests, post-deploy verification.
- `docs/ROLLBACK.md` — code / database / config rollback paths.
- `docs/LIVE_TAP_CHECKLIST.md` — the tracked manual release gate, now with the
  measured live-tap evidence (A/B/C) recorded in a separate section.

### Validation summary — release-notes draft (2026-09-24)

*Draft only — not published, committed, tagged, or released.*

**Validated**

- **YouTube quality ladder** (live, production path): 360p/480p/720p/1080p
  delivered and structurally verified; 1440p/2160p measured to resolve to the
  1080p H.264 rung on sources whose high rungs are VP9/AV1-only (no YouTube
  source exposes H.264/HEVC above 1080p — 23-ladder survey).
- **Spotify → MP3**: 128/192/256/320 kbps all delivered as measured CBR MP3
  (44.1 kHz stereo, full-file decode clean) from a lossy ≈129.5 kbps AAC
  source — encoding targets satisfied; no information added above the source.
- **Spotify → FLAC**: valid FLAC container/codec, full-file decode clean;
  measured 24-bit / 44.1 kHz / stereo representation of that same lossy source
  (lossless container ≠ lossless source quality).
- **Audio/video menu and delivery contracts**: advertised rung == delivered
  rung (video); audio rows are encoding targets under a stated source-quality
  ceiling; delivery verification accepts documented bitrate variance and keeps
  source-upscale a non-failing local observation.
- **Offline regression coverage**: 13 new tests this run — historical baseline
  1,810 tests / 57 files; post-change **1,823 / 57** — all passing (`ruff` and
  `mypy` clean). 0 commits created during the validation run.
- **Menu-honesty / texts / panel batch** (same day, the sections above):
  +114 tests (new `tests/test_menu_honesty.py`, `tests/test_admin_texts.py`) —
  **1,937 tests / 59 files** passing, `ruff` clean, `mypy` clean (112 source
  files). 0 commits created during this validation run either.

**Remaining manual items**

- **yt-session-generator `/token`** — external endpoint still returning 503
  (Layer-2); untouched, unresolved.
- **Chromium compatibility** — pinned Chromium build suspected in the
  session-generator's request-capture failure; untested, unresolved.
- **Cookie/session state** — the YouTube cookie jar is logged out; no
  login-state verification performed.
- **Device taps** — `docs/LIVE_TAP_CHECKLIST.md` sections A/B remain `NOT RUN`
  (human-on-device release gate).
- **Tier-above-ceiling via crafted/stale callback** — was menu-visibility-only;
  now closed server-side (`_tap_was_offered` validates every video quality and
  audio tier against the FSM-carried offer list, and stale taps get a retry
  prompt) — re-verify on device as part of sections A/B.
