# Docs

Operator notes that do not belong to a single runbook. Start with
[RELEASE_RUNBOOK.md](RELEASE_RUNBOOK.md) for deploys,
[ROLLBACK.md](ROLLBACK.md) for retreats, and
[LIVE_TAP_CHECKLIST.md](LIVE_TAP_CHECKLIST.md) for tap-level verification.

## Single-instance tripwire (services/instance_guard.py)

Session modes, the double-tap guard and the single-flight claim shelf are
per-process state. One bot process per Redis is therefore a correctness
requirement: a second live instance silently forks all of it (a user locked in
SPOTIFY on one replica is unconstrained on the other, and mode counters on
both replicas go wrong).

The guard is a tripwire, not a fix. At boot the process claims a best-effort
Redis lease (`bot:instance_lease`: random id plus boot epoch, 60 s TTL,
refreshed every 20 s, released on graceful shutdown). What happens next:

- No lease held: this process becomes the holder, silently. Normal startup.
- A *stale* foreign lease (low TTL — a crashed peer that stopped refreshing):
  taken over silently after a short grace period plus a re-check. A
  restart-after-crash must not page.
- A *live* foreign lease (recently refreshed): warn loudly in the log and add
  a `تک‌نمونه` row to /doctor. Warn-only — startup is never blocked by the
  guard, and the /doctor verdict never flips to fail because of it.
- No Redis (memory queue/backend): the guard stays disabled and /doctor says
  so. Memory mode is single-process by definition, so there is nothing to fork.

What is detected: two live processes sharing one Redis — a second
`docker compose up`, a split gateway/worker topology against one Redis, a
host-run bot next to the compose stack. What is NOT detected: anything about
Telegram itself (two polling instances with the same token already get
conflict errors from Telegram; the guard matters most for webhook mode and
the shared-Redis cases above), or a peer whose lease already expired (nothing
is left to see — that is the safe case, not a gap).

Redesigning the coordination layer on top of Redis (shared mode counters and
claim authority) is the real fix and is deliberately out of scope for the
tripwire: do not scale past one instance until that lands.

## Lossless menu rows (services/content.py)

Generic audio menus hide the FLAC/WAV rows: no registered provider can
honestly produce provider-verified lossless today (every production
candidate carries `provider_verified_lossless=False`), so those taps would
always end in `err.LOSSLESS_UNAVAILABLE`. One predicate decides —
`content.lossless_offered()`, false today — and it may only turn true when a
registered provider honestly produces `provider_verified_lossless`
(`services/audio_models.py`, `services/providers.py`) so the quality gate
(`services/quality.py`) can plan true lossless from it. Hiding is menus-only:
the validation vocabulary still recognizes the rows, and a tap from an old
menu is answered with the existing refusal before anything is queued (no
download, no quota movement).

## Host guard (services/host_guard.py)

User links reach server-side fetches, so every fetch of a stranger's URL goes
through one policy: `http(s)` only, no credentials in the URL, literal IPs
(incl. decimal/hex/octal/short IPv4, IPv4-mapped IPv6, zone ids) must be
global, local/internal/compose-service names are refused, known platform hosts
(exact or dot-suffix, never substring) skip DNS, and anything else must
resolve — with a timeout — to only global addresses. Refusals carry a host
digest in logs and a catalogue sentence (`err.PRIVATE_HOST`,
`intake.private_host`) for users; never a URL or an address.

Covered (bot-controlled HTTP, every redirect hop re-validated): intake triage,
share-link canonical resolve, Spotify share/page/cover fetch, Cobalt file
download (tunnel URLs to the configured instance hosts are allowlisted as
operator configuration). DNS failure fails closed at the fetch sites; intake
triage instead defers to them, and platform hosts never depend on DNS.

NOT covered (residual, by construction): yt-dlp and the Cobalt API resolve
DNS and follow redirects internally — a pre-check cannot stop their
redirect-to-internal or DNS rebinding, and this guard does not claim to. The
thumbnail URL handed to Telegram's `send_photo` is fetched by Telegram, not
this host. Recommendation only (no compose/firewall change made here): run
the engines behind egress network controls that cannot reach instance
metadata or the internal network, independent of any URL check.

## Force-join (services/subscription.py)

Optionally require regular users to join configured channels/groups before
downloading. Empty FORCE_JOIN_TARGETS (the default) disables the check
entirely: no middleware, no extra handler work, no Telegram or Redis calls.

Setup: list targets comma-separated as @ChannelUsername (public) or
-100123456789|<invite link>|<Title> (private). The bot must be an admin of
channels (so it can see members) and at least a member of groups. Premium/VIP
users and ADMIN_IDS always bypass the check, and it only ever applies in
private chats - language, /start, /help, /status, /profile, /premium, the
whole payment/receipt flow, admin screens and the verify button itself are
never gated.

The check runs where a download starts (URL intake and the download-menu
section taps, after the user middleware) via bot.get_chat_member: member,
administrator and creator pass, restricted passes only while is_member is
true, left/kicked do not. Passes are cached in Redis as fj:ok:<target>:<user>
with an int EX of FORCE_JOIN_CACHE_TTL_S; negatives are never cached, so a
user who just joined passes immediately, and the verify button always
re-checks live. After a pass the user resends the link (no pending-URL state).

Fail-open: a check that cannot run (bot removed, not an admin, flood, timeout,
dead cache) lets the user through, logs one warning per target, and pages the
admins once per target per 6h through notify_admins plus a bot_state throttle.
Each check is bounded (~5s) and concurrent with a total timeout, so a slow
Telegram never stalls intake. After boot, the maintenance loop validates each
target once on its first tick (time-bounded, never blocking startup).

NOT supported: sponsor bots. The Bot API cannot verify membership of another
bot, so a verify button for one would verify nothing.

## Inline mode (handlers/inline.py)

Typing @BotUsername <link> in any chat serves what this bot already
downloaded: each cached request for that link becomes the matching
InlineQueryResultCached{Video,Audio,Photo,Document} (at most 10, best
quality first, honest replay captions), answered personally with a small
cache time. Anything else becomes a deep-link button that opens the bot with
that link (/start dl_<digest> runs the normal intake for it).

The inline path performs NO network I/O and NO DNS: only structural parsing,
pure platform classification and a cache lookup. Links that would need
redirect resolution, cache misses, unknown users (only ever the plain start
button — no row is created from an inline query), exhausted quotas and
unverified membership all become the button instead of results. Membership is
read from the fj:ok cache only, never a Telegram call. Inline sends do
NOT consume quota (known property). A flood refusal on the answer itself is
absorbed and logged, never slept out.

Enabling is a BotFather step (/setinline); the code works whether or not
inline is enabled. Deep-link tokens live in Redis as dl:<digest> (int EX
3600, idempotent per URL) with a per-user mint rate limit (about 30/minute);
Redis trouble means a payload-less start button, never an error.

## Song lookup (services/song_id.py)

Videos delivered from Instagram or TikTok carry a "Full song" button only
when it can work: the metadata provider already named a real song in the
extraction info, or a non-metadata recognizer is enabled and healthy.
Videos without a detected song get no button, and neither do inline-sent
messages. Tapping shows the detected "Artist - Title" (worded as a likely
match, never a certainty) with the top candidate's title and ONE button
that downloads it as MP3 through the normal intake path — session modes,
quota, force-join, preflight, host guard, single-flight and cache all apply
unchanged, and nothing auto-downloads.

Providers come from SHAZAM_PROVIDERS (comma list, default "metadata");
unknown names are skipped with one startup warning. The tap mapping lives
in Redis as shz:<digest> (int EX 86400, same song link re-stores the same
key); an expired mapping answers "send the link again", and Redis trouble
at delivery time means no button plus one warning. Taps are budgeted per
user (about 5 per 10 minutes, Redis INCR + int EXPIRE), misses are
remembered briefly (int EX about 600), and recognizer work runs at most
two at a time per process.

What each provider sends to third parties: the metadata provider sends
nothing — it only reads the extraction info the engines already produced.
shazamio (opt-in only, named explicitly) is a free but UNOFFICIAL,
reverse-engineered Shazam client: at tap time it sends a derived audio
fingerprint, never the file. It is not covered by Shazam's terms and may
stop working or be rate-limited without notice.
