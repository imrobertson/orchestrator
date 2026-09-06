# BACKLOG: SESSION_TRACKER tracks one model at a time, silently

**Priority:** MEDIUM -- not a correctness bug and not data loss, but it
becomes visible the moment the cluster is routinely used as two
independent 1-node nodes rather than one cluster, which is now the normal
operating mode (a resident agent on the reserved host, testing on the
scratch host).

**Origin:** Fallout from TOMBSTONES.md #127. Fixing the count-based topo
derivation stopped the ledger being poisoned with fabricated
`<model>::2_node` keys, but it did not make the tracker able to see two
models. It converted *corruption* into *under-counting* -- a strictly
better failure mode, and a recoverable one, but still a wrong number on
the dashboard and in the ledger. This document is the remainder.

## What's actually wrong

`SESSION_TRACKER` is a single module-level instance holding one
`self.model` and one `self.topo`. Everything downstream inherits that
assumption:

- `_compute_cluster_status_impl()` picks a single `serving_host` by
  iterating `HOSTS` and taking the first entry with a
  `STANDALONE`/`HEAD` container, then feeds only that host's `/metrics`
  into `SESSION_TRACKER.update()`.
- With two independent 1-node deploys, the loser of that iteration is
  simply invisible: its prompt/generation tokens never reach the ledger,
  its session never starts, and `session_stats` reports the winner's
  numbers as though they were the cluster's.
- Which host wins is determined by `cluster_config.yaml`'s `hosts:`
  ordering, via `PRIMARY_HOST = next(iter(HOSTS))`. That ordering is
  load-bearing for the Ray head role, where it is meaningful; it is
  incidental for telemetry, where it silently decides whose tokens count.

Concretely, in the current reserved-host layout (hermes on spark-4,
testing on spark-3): spark-4 is first, so hermes is always the tracked
model and every test container's tokens are dropped on the floor. Flip
the ordering and the inverse happens -- the long-lived service stops
accruing while short-lived test containers do.

The dashboard's `SESSION SPEED` / `system_tps` readouts have the same
blind spot for the same reason, so during a manual test on the scratch
node the header reflects the *other* host.

## Why this is under-counting, not corruption

Worth stating explicitly so nobody re-escalates it: the tokens that DO
get recorded are recorded under the correct `<model>::<topo>` key, with a
correct baseline. Nothing is attributed to the wrong model, and nothing
fabricates a key that has no recipe. The missing tokens are recoverable
after the fact via `correct-ledger` against the untracked host, which was
NOT true of the `::2_node` keys #127 was producing.

## Proposed shape

Two independent pieces; the first is worth doing alone if the second
looks like too much.

**1. Make the telemetry host preference explicit.** Today it falls out of
dict ordering. Even without multi-model support, `serving_host` selection
should say what it is choosing and why -- most plausibly "prefer the
reserved host if it is serving, else first serving host", so the
long-lived service is the one that keeps accruing. Small, self-contained,
and removes an invisible dependency on YAML ordering.

**2. Per-host tracker instances.** Replace the single global with a dict
keyed on host, each holding its own model/topo/baselines/flush state, and
have `_compute_cluster_status_impl()` update every serving host rather
than one. `_commit_session()` already writes per-`<model>::<topo>` keys,
so the ledger format needs no change -- this is a fan-out at the caller,
not a schema migration.

Watch for, when doing (2):

- `execute_teardown()`'s survivor gate (added alongside #127) exists
  precisely because one global tracker could not tell "my host went away"
  from "some host went away". With per-host trackers that gate becomes
  simpler, not more complex -- a host's tracker deactivates when that host
  is torn down, full stop. Simplify it rather than leaving both mechanisms
  in place.
- `session_stats` in `/api/status` is currently a single object. Making it
  per-host is an API shape change that `index.html` reads directly
  (`latestSessionStats.active`, `.tps`, `.mtp_rate`) -- either keep a
  merged/primary view for backward compatibility or update the dashboard
  in the same pass, but don't ship a shape change without one of those.
- The 600s idle timeout and 3600s flush interval are per-session; with N
  sessions they run independently. That is correct, but it means N times
  the ledger writes -- fine at N=2, worth a glance if `HOSTS` ever grows.

## Explicitly NOT in scope for this item

- Do not revisit the topo derivation. #127 settled it: role-based, from
  `ContainerRole`, at all three sites. If a fourth site appears that
  derives topology from host counts or host identity, that is a #127
  regression, not this item.
- Do not make this a blocker for Phase 3. The N-node generalization will
  change what "a host" means for deployment purposes, but per-host
  telemetry is orthogonal and can land before or after.
- Do not attempt to reconstruct historically missed tokens as part of the
  code change. If the gap matters for a specific model, run
  `correct-ledger` against that host deliberately -- see #127's own audit
  note.
