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
