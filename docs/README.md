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
