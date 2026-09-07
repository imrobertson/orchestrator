# Reference: control surfaces, state ownership, and current limitations

**Written 2026-09-06, immediately after the reserved-hosts / per-host-readiness
work landed (`TOMBSTONES.md` #129–#131).** Its purpose is to be the written
baseline for the upcoming interface spike, so that spike starts from what the
system actually does rather than from reconstruction.

**This document describes behaviour, not intent.** Where something is a
deliberate design decision it says so; where it is a limitation inherited from
an assumption the system has outgrown, it says that instead. Several entries
here are the second kind, and they are the interesting ones for the spike.

**Retire it when the spike lands.** Anything in here that survives redesign
should move into `README.md` / `USERMANUAL.md`; anything that gets fixed should
move into `TOMBSTONES.md`. This file is deliberately not linked from the doc map
in `README.md`, so it can be deleted without leaving a dangling reference.

**A note on the name.** `errata` was avoided on purpose: `docs/errata.yaml`
already exists and means something specific and unrelated (machine-readable
recipe rules with provenance). Reusing the word for a prose document about
control surfaces would make both harder to find. `REFERENCE-` matches the
existing prefix convention (`REFERENCE-flashinfer-autotune-internals.md`).

**Confidence.** Everything in sections 1–4 was verified against the source this
session — `dgx-orchestrator.py` at
`ORCHESTRATOR_VERSION_SLUG = "2026-09-06-per-host-readiness-disk-pending-launch"`,
`html/index.html`, `dgx-config`, and `cluster_config.yaml`. Section 5's
limitations are each traceable to a specific code path, named inline. Section 7
is untested as written — it is a procedure, not a recorded result.

---

## 1. The control surfaces

There are two supported surfaces and one unsupported one that looks supported.

### Dashboard (`http://maestro:5000`)

Talks to the FastAPI daemon over `/api/*`. The daemon is a long-lived process
inside the `dgx-orchestrator-api` container. Every deploy/teardown issued here
runs **in the daemon's own process**.

### `dgx-config` (recommended for CLI work)

A bash wrapper that `docker exec`s into the running container:

```bash
exec docker exec $DOCKER_TTY -e USER="${USER:-ian}" \
    "$CONTAINER_NAME" python3 dgx-orchestrator.py cli "${PROCESSED_ARGS[@]}"
```

Three properties that matter and are easy to lose if this is ever bypassed:

- It runs **inside the container**, so `BASE_DIR=/app` (set by
  `docker-compose.yml`) matches the daemon's. All JSON state files are the same
  files the daemon reads.
- It **refuses to run if the container is down**, with a fix hint. That guards
  the coarsest version of the "is the daemon there" precondition, though not a
  wedged-but-running daemon.
- It forwards `$USER` for deploy attribution.

Critically, a `dgx-config` deploy is still a **separate, short-lived process**
from the daemon. It shares the container and the filesystem, not the daemon's
memory. Every piece of state that must survive the CLI process exiting has to be
on disk. This is the single most important structural fact in this document, and
the root cause of `TOMBSTONES.md` #129.

### Running `dgx-orchestrator.py` directly on maestro's host — do not

`python3 dgx-orchestrator.py cli deploy ...` executed on the bare host resolves
`BASE_DIR` via the `Path(__file__).resolve().parent` fallback, not to `/app`. The
deploy itself will work — it SSHes to the Sparks either way — but every state
file it writes lands in a directory the daemon never reads. The result is a
deploy that succeeds and registers nothing, silently. This is the same failure
shape as #129 in different clothing.

**Use `dgx-config`.** If a direct invocation is ever genuinely needed, set
`BASE_DIR` explicitly to match the daemon's.

---

## 2. State files: who writes, who reads

All under `BASE_DIR` (`/app` in the container).

| File | Written by | Read by | Lifecycle |
|---|---|---|---|
| `active_deployment_state.json` | deploy (any surface), at launch | daemon status poll, reserved-host guard | per-host; cleared on teardown |
| `pending_launch_state.json` | deploy (any surface), head host only | daemon status poll only | per-host; cleared on promotion, teardown, or staleness |
| `model_ledger.json` | daemon (phase data, launch history, lifetime tokens) | ETA estimates, dashboard meta strip | cumulative, permanent |
| `hf_path_ledger.json` | deploy | cache attribution, `flush-model-cache` | cumulative |
| `benchmark_ledger.csv` | `benchmark.py` | `enrich_catalog()`'s `historical_tps` | cumulative |
| `config_registry.json` | catalog build | `config_hash` → payload decoding | cumulative |

Deliberately separate files, indexed on different axes with different
lifecycles — see the comment above `ACTIVE_DEPLOYMENT_STATE_PATH` for why they
were not combined.

**The division of labour that matters:** a deploy writes only *facts about what
it launched*. Everything that requires *observing the result* — health, phases,
launch success — is written by the daemon's poll. A CLI deploy therefore
produces a complete record only if the daemon is alive to observe it.

### The daemon poll

`_telemetry_daemon_loop()` calls `get_cluster_status()` every 10 seconds
unconditionally, independent of whether any dashboard is open. That loop is what
promotes pending launches, archives run logs, records phase data, and accumulates
session stats. If it is wedged, deploys still work and nothing is recorded. It
prints on exception rather than swallowing — that print is the signal to look
for (added after the SessionTracker self-deadlock incident; see the `RLock` fix).

---

## 3. What a deploy registers today

Identical for dashboard and `dgx-config` as of 2026-09-06. Before that date the
CLI column was substantially worse — see #129.

| Record | Registers? | Notes |
|---|---|---|
| `ACTIVE_DEPLOYMENT_STATE` | Yes | disk, immediate |
| Dashboard dropdown + host cards | Yes | within one 4s poll |
| `launch_history` (the "✓ last launched successfully" marker) | Yes | ~10s after the host reaches READY |
| `runs[]` phase data + run-log archive | Yes | daemon-side, now per host |
| `hf_path_ledger.json` | Yes | deploy process, disk |
| Legacy `cached`/`compiled`/`downloaded` buckets | Only with `--wait`/`--benchmark` | `record_load_time()` is on that path only. Same on both surfaces. The tiered ETA reader prefers `runs[]`, so this is largely vestigial |
| `benchmark_ledger.csv` | Only with `--benchmark` | see §5 on the `--model-key` risk |
| Lifetime token accrual | **Only for `serving_host`** | see §5 |

**Preconditions for the launch marker specifically.** The host must actually
reach READY (a crash correctly records nothing and ages out after
`PENDING_LAUNCH_STALE_SEC`, 3h), and the host's `active_recipe_key` must match
the pending record. That key is only non-`None` when `ACTIVE_DEPLOYMENT_STATE`
holds an exact record corroborated by live container discovery — so promotion
means "the recipe this deploy launched is the one running on this host, and this
host is healthy", not a name resemblance.

**Historical data warning.** `launch_history` written before 2026-09-06 is
*incomplete*, not merely sparse — CLI-only and non-`serving_host` deploys never
recorded. Do not treat absence as evidence a recipe was never launched
successfully. `ROADMAP.md`'s per-recipe status-marker entry carries this caveat
because it proposes auto-promoting off exactly this data.

---

## 4. Display semantics you need in order to read the dashboard correctly

### `serving_host` is not "the important host"

It is the first host in `cluster_config.yaml` order holding a `STANDALONE` or
`HEAD` container. Nothing more. With a resident model on the first-listed host it
is always that host, permanently. Several things key off it, correctly, that
read as arbitrary if you assume it means something stronger: `cluster_ready`, the
live-metrics endpoint, and session tracking.

### Readiness is per host — as of 2026-09-06

Each host's `READY` reflects that host's own `/health`. The one inheritance left
is deliberate: a 2-node Ray `WORKER` exposes no API of its own, so its readiness
genuinely is the head's.

Before this change, every host inherited `serving_host`'s health, so a
still-compiling or dead model on the second node displayed as READY. **Old
screenshots and notes will disagree with current behaviour for this reason.**

### The model dropdown shows one recipe, and picks which one by intent

Detection order: the host your **Target** selector points at, then
`serving_host`, then `cluster_config.yaml` order. So a CLI deploy to `spark-3`
with the selector on `spark-4` shows `spark-4`'s recipe.

This is deliberate but unsatisfying, and it is squarely a spike question. Under
two independent 1-node deploys there is no single correct answer to "what is
running", and the current UI can only express one. Following the operator's
stated intent is the least-wrong option available without redesigning the panel —
it is not a good answer, just a defensible one.

### The ETA countdown is clamped, and the clamp is cosmetic

The backend re-derives its ETA from scratch every poll off a log-keyword phase
guess that genuinely flips mid-load. `index.html` enforces monotonicity so the
number only ever decreases within one countdown. This hides a real backend
problem rather than fixing it — `detect_model_stage()`'s live phase guess is
untouched by the Task C/D phase work. See the ETA clamp comment in `index.html`
and `ROADMAP.md`'s ETA/telemetry entry.

### Version badge

Next to Server Time (UTC). Shows `ORCHESTRATOR_VERSION`, which now carries a
derived source-hash suffix. **Check it after any deploy of the orchestrator
itself** — a forgotten `git push` before `git pull` on maestro has silently left
old code running more than once. Note it covers `dgx-orchestrator.py` only;
`common/` staleness is invisible (open ROADMAP entry).

---

## 5. Known limitations, current as of 2026-09-06

### Session tracking is single-host

`SESSION_TRACKER` is one global instance with one `self.model` and one
`self.topo`, fed from `serving_host`'s `/metrics`. With two independent 1-node
deploys, the non-serving model's `lifetime_in`/`lifetime_out` do not accrue and
its live session speed is not shown. The numbers `benchmark.py` reports are
unaffected.

This is the largest remaining gap for the scoped-deploy workflow, and it is the
one most likely to matter to the spike, since it is a *data model* limitation
surfacing as a *display* limitation. Backlog:
`docs/BACKLOG-session-tracker-multi-model.md` (produced, not landed).

Related hazard, already fixed but worth knowing: `SESSION_TRACKER.update()`
latches model and topo on its inactive→active transition and freezes them for
the session. A wrong topo latched at session start poisons every subsequent
flush under a fabricated ledger key — this is what `TOMBSTONES.md` #127 was.

### `benchmark_ledger.csv` can attribute to the wrong recipe

`benchmark.py --model-key` is what joins a benchmark row back to a catalog key.
A missing or stale value produces a legitimate-looking row attributed to the
wrong recipe, silently. Confirmed to have happened once
(`deepseek-v4-flash-0731-1M` vs `...-dspark`). Open ROADMAP entry.

### There is no host-level view of "what is this cluster running"

The Model Deployer panel is built around a single active deployment. Host cards
show per-host state correctly, but nothing in the UI presents "these two
independent models are up, here is each one's health, throughput, and history"
as a first-class view. Everything that does exist is a workaround for that
absence.

### `--dry-run` is not exempt from the reserved-host guard

Deliberate: it reports what a real deploy would do, so it reports the refusal a
real deploy would hit rather than printing a command that would be blocked.
Noted here because it surprises people.

---

## 6. Reserved hosts across the surfaces

`reserved: true` in `cluster_config.yaml` marks a host as carrying something that
must not be disturbed by routine work. It is a property of the **machine**, not
of any deployment — unlike `role`, which describes a host's part in one topology.

| Surface | Override mechanism |
|---|---|
| CLI | `--force` on `deploy` / `teardown` |
| Dashboard | Per-action confirm dialog; tick to enable "Proceed anyway" |
| API | `force: true` in the request body |

The dashboard deliberately has **no persistent force checkbox**. A checkbox holds
state: ticked once for a legitimate override and then forgotten, it would silently
skip the guard on the next deploy, possibly for a different person at the same
always-on dashboard. The dialog holds the override for exactly one request.

The dialog is raised by the server's **HTTP 423**, not by a client-side
prediction — so the text is the server's own refusal, naming what
`ACTIVE_DEPLOYMENT_STATE` records as running on that host. 423 is checked before
the generic 400/409 in both endpoints, because 409 on teardown already means
"cluster busy", which must not be clickable-past. The unforced attempt is free:
`check_reserved_hosts()` runs before `CLUSTER_OP_LOCK` and before any SSH.

Not covered by the guard: any raw `docker run` over SSH, which bypasses the
orchestrator entirely. `tests/ab_test.py` does its own check for this reason.

---

## 7. Verifying the ledger path end to end

**Untested as written — this is a procedure, not a recorded result.** Run it once
and record the outcome here if it is worth keeping.

```bash
# 1. confirm the running code is what you think it is
dgx-config status | grep orchestrator_version

# 2. deploy to the scratch node
dgx-config deploy --model <recipe> --nodes 1

# 3. immediately: pending record should exist, keyed by host
docker exec dgx-orchestrator-api cat /app/pending_launch_state.json

# 4. once the host shows READY, ~10s later: should be gone
docker exec dgx-orchestrator-api cat /app/pending_launch_state.json

# 5. ...and turned into launch history
docker exec dgx-orchestrator-api python3 -c \
  "import json;d=json.load(open('/app/model_ledger.json'));print(json.dumps(d['<recipe>::1_node'].get('launch_history'),indent=2))"
```

Step 4 empty and step 5 showing a `last_success_ts` with an incremented `count`
is the whole path demonstrated. Step 3 empty → the deploy side. Step 3 populated
but step 4 never clearing → the daemon side; check the container logs for
`_telemetry_daemon_loop` exception prints.

---

## 8. Questions this leaves for the interface spike

Not proposals. These are the decisions the current design defers, listed so the
spike does not have to rediscover them.

1. **What is the primary unit the UI is organised around?** Today it is
   implicitly "the cluster's one deployment", with host cards bolted alongside.
   The reserved-host work made "per-host deployment" the real unit. Almost every
   awkwardness in §4 and §5 follows from that mismatch.

2. **What should the model dropdown mean when two models are running?** It
   currently doubles as "what is running" *and* "what am I about to deploy",
   which are different questions that only had the same answer when the cluster
   ran one thing at a time.

3. **Should session/lifetime tracking become per host or per deployment?** Per
   deployment is the more durable answer under Phase 3's N-node generalisation,
   and it aligns with the declarative node-properties-vs-deployment-properties
   direction already agreed. Per host is cheaper now.

4. **Does teardown scope belong next to the button, or is it a property of a
   selected deployment?** The current inline selector is correct for the current
   UI and probably wrong for the redesigned one.

5. **How much of the reserved-host guard survives Phase 3?** Fabric-connected
   pools mean a deploy must never span pools; "reserved" then becomes one
   constraint among several rather than a special case with its own dialog.

6. **What is the story for a deployment that is neither current nor torn down?**
   Crashed, orphaned, stale-record — these have status strings today but no place
   in the interface that treats them as things to act on.
