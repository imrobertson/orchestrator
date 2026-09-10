# DGX Spark Cluster — Architecture & Migration Plan

This is the plan for where the system goes next: a phased path from the
original `models.yaml`-monolith design to a config-driven, recipe-based,
N-node-ready control plane — without a big-bang rewrite, because this runs
daily for you and a few team members now.

This supersedes the rough "Phase A / B / C" sketch from earlier discussion.
Splitting out recipe migration as its own phase (Phase 2 below) made more
sense once we agreed to retire `models.yaml` entirely rather than keep it as
a monolith — the earlier Phase A splits into Phases 1 and 3 here.

## Context & constraints

- The system is in daily use by you and a few teammates who now also deploy
  to the Spark pair. It is not a green-field project.
- No immediate plan to rack the other 4 Sparks, but the architecture should
  not preclude it — cheap to keep the door open now, expensive to retrofit
  later.
- Tokens and time are finite. Every phase below is scoped to be shippable on
  its own, independently valuable, and safe to pause after.

## Guiding principles for the migration

1. **Every phase leaves the system fully usable for daily work.** No phase
   requires "everything is broken until phase N+1 lands."
2. **Additive before subtractive.** A new path is built and verified
   alongside the old one; the old path is deleted only after a burn-in
   period with no observed regressions — not in the same commit that
   introduces the replacement.
3. **Every refactor phase gets a mechanical way to prove it changed nothing
   behaviorally.** Eyeballing a diff isn't enough for code that SSHes into
   production hardware and runs `docker run`. See "dry-run diffing" below.
4. **Schema decisions get made early even if the feature behind them isn't
   built yet**, so we don't pay for a second migration later. Example: the
   recipe schema gets capability fields (task, context class, latency class)
   in Phase 2, populated with real matching logic only in Phase 4.
5. **Multi-user changes the risk calculus.** "No auth on the API" was
   originally filed as lowest priority under a single-user assumption.
   That assumption is no longer true — see Open Decisions.
6. **Don't build or test N-node execution logic against hardware that
   doesn't exist yet.** Design the schema to not block it; defer the actual
   rank-based launch/locking rewrite until nodes are physically racked.

## Terminology: is this an "orchestrator"?

Worth being precise about, since it'll come up when explaining the
capability-request integration to teammates or the Pi harness later.

Strictly, what exists today is **not** an orchestrator in the sense the
term carries elsewhere in infra (Kubernetes, Nomad, Slurm). Those systems
share two traits this system doesn't have yet:

- **Scheduling** — deciding *where* something runs based on constraints and
  available resources. Today a human picks the model, topology, and (for
  1-node) the target host. That's imperative deployment automation, not
  scheduling.
- **Reconciliation** — a continuous loop comparing desired state to actual
  state and self-healing drift (a container dies, it gets rescheduled).
  Today, if a container crashes, `get_cluster_status()` will *report* that
  accurately, but nothing acts on it.

What exists today is more precisely a **deployment and lifecycle
management tool**, or a **control plane** — provisioning, teardown,
status/health monitoring, all imperative and operator-driven. Going
forward, this doc uses:

- **Control plane** — the whole off-node system: config, API, SSH
  execution, monitoring. What exists today, in full.
- **Scheduler / allocator** — the specific piece, introduced in Phase 4,
  that makes placement decisions from a capability request instead of a
  human picking a host. This is the piece that would make "orchestrator"
  literally accurate rather than just a filename (`dgx-orchestrator.py`).
- **Orchestrator** — reserved as the umbrella/product name (matches the
  existing filename, no reason to rename it), understood as aspirational
  until the scheduler and, eventually, a reconciliation loop exist.

Not in the phased plan yet, worth naming as a possible future phase: a
**reconciliation loop** (auto-restart on crash, with backoff to avoid
restart storms) would complete the technical picture. It's a meaningfully
different problem from anything scoped above and shouldn't be assumed as
part of Phase 4's allocator work.

## Target end-state architecture

```
cluster_config.yaml          single source of truth: hosts, ports, ssh,
                              network interfaces, container naming, and
                              deploy-time tuning knobs (shm_size, GPU clock
                              lock, JIT cache size -- landed ahead of
                              schedule, see Phase 5 note)
recipes/
  eugr/*.yaml                   hand-reviewed, hand-promoted translations of
                                 eugr recipes -- NOT an automated sync
                                 target (see EUGR-REFERENCE-NOTES.md); the
                                 actual raw sync + translation pipeline is
                                 eugr-samples/ -> tools/translate_eugr_recipes.py
                                 -> recipes/_translated_from_eugr/ -> human
                                 review -> here or recipes/local/
  local/*.yaml                  your own -- new models, forks of eugr's
                                 recipes, hand-authored from scratch
common/
  config.py                    loads + validates cluster_config.yaml
                                (pydantic schemas) -- landed
  recipes.py                    loads + validates recipes/{local,eugr}/*.yaml
                                (pydantic schemas) -- landed
  ssh.py                        run_ssh, key resolution, HF token lookup
                                (one implementation, not three) -- landed
  constants.py                  ContainerRole enum, etc. — no bare string
                                literals for container names anywhere else
                                -- landed
  docker_ops.py                 docker run command builders, testable
                                without SSH or live hardware -- not yet
                                extracted; still inline in
                                _execute_deployment_impl
dgx-orchestrator.py            imports common/, owns all cluster contact —
                              stays the only thing that ever touches the
                              Sparks over SSH, stays off-node
cache_cluster_assets.py        imports common/, no duplicated plumbing
benchmark.py                  imports common/ for host defaults

(later) allocator + /api/allocate
                              capability-based scheduling on top of the
                              same recipes + cluster_config data, once
                              N-node execution is proven solid
```

The load-bearing design choice: **recipes and cluster config are data,
`common/` is the only code that reads them, and `dgx-orchestrator.py` is the
only thing that ever opens an SSH connection to a Spark.** Nothing about
adding recipes, adding nodes, or adding an allocator changes that last
sentence.

---

## Phase 0 — Safety net
*Do this week. About one evening. Zero behavior change.*

**Goal:** make every later phase reversible and mechanically verifiable
before touching anything that runs against production hardware.

**Changes:**
- `git tag pre-migration-known-good` on the current working state.
- Add a `.dockerignore` excluding `.secrets`, `id_dgx_orchestrator`,
  `.git`, `load_times.json`, `benchmark_ledger.csv`, `__pycache__/` — a
  cheap, already-identified fix that costs nothing to do now.
- Add a `--dry-run` flag to `execute_deployment` that builds and prints the
  full `docker run` argument list without SSHing or executing anything.
  This is the single most useful tool for the phases that follow: it lets
  you diff "old code's command for model X" against "refactored code's
  command for model X" byte-for-byte, for every model in the catalog, in
  seconds, with zero cluster risk.
- Write a 10-line smoke-test script: hits `/api/status`, asserts both hosts
  report `reachable`, asserts the catalog loads and is non-empty. Run it
  after every phase below as a go/no-go gate.

**Status: landed.** `--dry-run` exists on `execute_deployment` /
`_execute_deployment_impl`, `tools/smoke_test.py` exists.

**Verification / rollback:** trivial — nothing here changes runtime
behavior, only adds tooling.

**Depends on:** nothing. Start immediately.

---

## Phase 1 — Config consolidation
*The `cluster_config.yaml` + `common/` extraction.*

**Goal:** one file owns host/network/ssh truth; three duplicated `HOSTS`
dicts (plus `benchmark.py`'s partial fourth copy) become one load call.

**Changes:**
- Introduce `cluster_config.yaml` (schema below). `models.yaml`'s `hosts:`
  block — currently dead code, nothing reads it — gets deleted once this
  lands, not before.
- Extract `common/config.py` (pydantic-validated loader), `common/ssh.py`
  (`run_ssh`, `resolve_user_identity_key`, `get_hf_token` — currently
  duplicated near-verbatim between `dgx-orchestrator.py` and
  `cache_cluster_assets.py`), `common/constants.py` (`ContainerRole` enum
  replacing the bare `"vllm-standalone"` / `"vllm-head"` / `"vllm-worker"`
  string literals scattered through `dgx-orchestrator.py`).
- Both scripts import from `common/` instead of defining their own `HOSTS`.
  `benchmark.py`'s `DEFAULT_HOST_IP` becomes `cluster_config.hosts["spark-4"].management_ip`.

**Status: landed.**

**Verification / rollback:** for every model in the current catalog, run
`--dry-run deploy` before and after the refactor and diff the resulting
`docker run` arg lists — they must be identical. Then do one real deploy of
a small, low-stakes model (e.g. `qwen-2.5-coder-32b`, 1-node) end to end
before trusting it for the models you use daily. Roll back to the git tag
if anything mismatches.

**Depends on:** Phase 0's `--dry-run` flag.

**Risk:** low — mechanical, no logic changes, just where literals come from.

---

## Phase 2 — Recipe migration (retire `models.yaml`)
*Replace the monolithic model catalog with `recipes/*.yaml`, one file per model.*

**Goal:** what you asked for directly — easy to add/edit/version a single
model without touching a shared file, and a clean seam for borrowing (or
diverging from) eugr's tested recipes.

**Changes:**
- Design the recipe schema (draft below) — borrows eugr's
  `recipe_version` and `cluster_only` / `solo_only` flags, since those
  catch real configuration errors (deploying a topology combo that doesn't
  exist) before deploy time instead of after.
- Split `recipes/` into `eugr/` and `local/`. `eugr/` is a periodic,
  read-only-by-convention sync from eugr's `recipes/` directory for models
  where their tested build/flags are the right answer — pin to a specific
  commit or tag when you sync, don't track their `main` live. `local/` is
  everything you write yourselves, including forks of an `eugr/` recipe
  that need a tweak. The loader refuses to start if a name collides between
  the two directories, rather than silently picking one.
- Add an optional `mods:` field to the recipe schema now (empty list is
  fine to start) — this is the extension point for eugr's compatibility-fix
  pattern (runtime patches for model-specific bugs). `common/docker_ops.py`
  folds any listed mod commands into the container's startup command,
  executed the same way you already wrap the Ray-head `vllm serve` command
  in a `bash -c` string. Doesn't need real content on day one; the field
  existing means you're not doing a second schema migration when the first
  model actually needs a patch.
- Add capability fields to the schema now too (`task`, `context_class`,
  `latency_class`) — unpopulated or best-guess values are fine. Phase 4's
  allocator is the first thing that reads them for real.
- `common/config.py`'s loader globs `recipes/{eugr,local}/*.yaml`
  instead of parsing `models.yaml`'s `models:` block.
- **Migration mechanics:** for the burn-in period, load both old
  (`models.yaml`) and new (`recipes/`) representations and assert they
  produce identical `--dry-run` output for every model, logging a loud
  warning on any mismatch. Delete `models.yaml` only after this comparison
  has run clean through at least one full week of normal daily deploys —
  not on a fixed calendar date, on evidence.
- Before hand-writing 14 recipe files: confirm which of the current 14
  catalog models are actually still in active use. No reason to migrate
  cruft.
- **[New, 2026-08-20; superseded 2026-08-29] `mods:` execution — a real
  deliverable, mechanism now decided differently.** The 2026-08-20 version
  of this entry specified adapting eugr's `mods/<n>/run.sh` pattern applied
  via `docker exec` after the container reaches `RUNNING` and before the
  health-check poll. **That mechanism is wrong and has been replaced.** The
  format decision (a mod is a directory containing `run.sh`) stands; the
  delivery decision does not.

  The 08-20 decision was made against the mod *concept*, without reading any
  actual `run.sh`. Reading them showed both of its own named first
  candidates fall outside what an exec-based mechanism can do:
  `mods/gpu-mem-util-gb` rewrites eight vLLM source files including
  `vllm/engine/arg_utils.py`, adding a CLI argument that is parsed at
  process startup — unconditionally too late to apply by exec — and
  `mods/drop-caches` is a persistent host-level daemon
  (`/proc/sys/vm/drop_caches` is not namespaced), not a container-scoped
  operation at all.

  **Current decision: bake a derived image layer before launch.** Full
  rationale, the survey of eugr's actual mod library, the rejected
  alternatives, hard constraints (vendored payloads, per-host bake,
  `WORKSPACE_DIR` handling) and the implementation sequence live in
  **`ROADMAP.md` → "Model-specific mods: bake a derived image layer"**.
  That entry is authoritative; this one records only that the field exists,
  why, and that the mechanism changed. Do not restate the design here — the
  two documents must not duplicate each other.

  Still true from the 08-20 version: `recipe.mods` already exists in the
  schema and round-trips through `load_recipes()`, so this is an execution
  problem rather than a schema migration; and mod *content* is expected to
  come from eugr's library rather than be written from scratch.

**Status: mostly landed, with one piece corrected after landing.** Recipe
schema, `recipes/{local,eugr}` split, and the dual-load burn-in period all
shipped. One thing shipped differently than this draft originally
specified and is worth recording rather than quietly forgetting: **the
recipe schema originally included a `name:` field (see the old schema
draft below, now corrected) that was required to match the filename
stem.** That redundancy caused a real production incident — a recipe's
`name:` drifted out of sync with its filename during an unrelated merge,
and because `build_catalog_response()` fails closed on any single bad
recipe, the *entire* model catalog silently went empty (confirmed via the
dashboard's "Select Model" dropdown showing nothing). Fixed by removing
`name:` entirely — the filename stem is now the only identifier, so
there's structurally nothing left for it to disagree with. See
`common/recipes.py`'s module docstring and `EUGR-REFERENCE-NOTES.md`'s
2026-08-20 update for the full account. The schema draft later in this
document reflects the corrected, current shape — the version further down
that still shows a `name:` line and `schema_version:` (vs. the actual
`recipe_version:`) is stale and is fixed in this same edit.

**Verification / rollback:** the dual-load comparison above is the
verification. Rollback is trivial during burn-in — `models.yaml` still
exists and the loader can be flipped back to it with one flag until you
delete it for good.

**Depends on:** Phase 1 (recipes still need `cluster_config.yaml` for
volume mounts, ssh user, etc. — recipes describe the model, not the host).

**Risk:** medium — this is the phase with the most hand-migration (14 model
configs → 14 files), so it's the one most worth the dual-load safety net.
Confirmed higher-touch than expected in one respect: single-point-of-
failure risk from `build_catalog_response()`'s fail-everything-on-one-bad-
recipe behavior turned out to be real, not theoretical — see the incident
noted above. That failure mode itself is **not yet fixed** (only its one
trigger, the `name:` field, is gone) — containing the blast radius of a
future malformed recipe (skip-and-warn per-file instead of failing the
whole catalog) remains an open item, moved to Phase 5 below.

---

## Phase 3 — N-node generalization
*Gated on hardware. Do not start until the additional Sparks are actually racked.*

**Goal:** remove the places where "2" is hardcoded as a fact about the
universe rather than a fact about your current hardware.

**Already landed, ahead of this phase (V4.8.5):** the ten places that
hardcoded the literal strings `spark-3`/`spark-4`/`10.0.14.43` are gone —
`PRIMARY_HOST`/`SECONDARY_HOST`/`PRIMARY_HOST_IP` now derive from
`cluster_config.yaml`'s `hosts:` list instead (see `docs/TOMBSTONES.md`
#73). This is naming/derivation only, done opportunistically while fixing
a real bug (a hardcoded `target_hosts` in the 2-node deploy path), not a
start on Phase 3 proper — it doesn't touch node-count assumptions,
locking, or teardown scoping below.

**Network topology constraint — new, not yet reflected in code:**
a Spark host pair may sit on a network segment with no RoCEv2/ConnectX-7
fabric to another pair. This wasn't a real constraint to design against
while there was exactly one pair; it becomes load-bearing the moment a
second pair (e.g. `spark-5`/`spark-6`) exists, since NCCL/Gloo rendezvous
assumes fabric connectivity between whatever hosts a deploy targets.
Concretely: **hosts are a set of fabric-connected pools, not one flat
pool** — an N-node deploy or the eventual allocator must select all of its
targets from within a single pool, never span pools. The current sidestep
(a second `maestro2` orchestrator instance per pool, discussed but not
built — see `ROADMAP.md`) avoids needing this in code at all, by keeping
each pool under its own `cluster_config.yaml` and its own orchestrator
process. That's a reasonable stopgap for two isolated pairs, but the
underlying single-`serving_host`/single-global-`SessionTracker`
architecture doesn't go away — it will matter again the moment two pools
need to be visible from one dashboard, which is exactly when the "pool"
concept below needs to actually land in the schema and allocator rather
than being sidestepped.

**Changes (deferred, listed so the direction is clear when the time comes):**
- `nodes: Literal[1, 2]` → validated `int` checked against active host count
  in `cluster_config.yaml`, both in the Pydantic model and the CLI args.
- Head/worker binary role → rank-based host list (rank `0..N-1`). The
  Ray-backed path (`ray start --address=...`) already generalizes to N
  workers with no real change; the manual NCCL path (`--nnodes` /
  `--node-rank` / `--master-addr`) needs its host-selection logic to pull
  from a pool instead of a fixed pair.
- `cluster_config.yaml`'s `hosts:` schema gains a `pool:` (or `fabric:`)
  field per host, so hosts on disconnected segments are distinguishable in
  config, not just in someone's head. Host selection anywhere in the
  deploy path validates that all selected hosts share a pool before
  proceeding, rather than assuming any N hosts can be wired together.
- `CLUSTER_OP_LOCK` (currently one global lock) → per-host locking, so a
  2-node deploy on spark-3/4 and an independent 2-node deploy on two other
  nodes can run concurrently instead of one blocking the other for no
  reason. This is what actually removes the need for a `maestro2` stopgap,
  once it lands.
- `execute_teardown`'s always-nukes-everything behavior → accept a
  `target_hosts` param at the API/CLI level (the function already accepts
  it internally — this is a pre-identified gap, not new).
- Recipe topology keys generalize from `1_node`/`2_node` to whatever node
  counts actually apply (`4_node`, etc.) — this needs no schema change,
  just more keys, since the schema was never restricted to exactly two.

**Verification:** same `--dry-run` diffing pattern as Phase 1, plus this is
the first phase worth actually load-testing two concurrent independent
deploys once the per-host locking lands. Add a dry-run case that
deliberately spans two pools and confirms it's rejected, not just cases
that stay within one.

**Depends on:** Phase 2 (recipes must exist first — no reason to
generalize execution logic against a config format you're about to
delete), plus physical hardware.

**Risk:** deferred until scoped against real hardware, so risk is
unassessed for now — revisit sizing when the nodes arrive.

---

## Phase 4 — Capability layer & allocator
*The Pi-code-harness integration: "ask for capability, get an endpoint."*

**Goal:** move from "operator picks a specific model and topology" to
"caller expresses intent, system resolves and allocates."

**Changes:**
- Populate the `capability` fields added to the recipe schema back in Phase
  2 with real values across the catalog.
- Generalize `get_cluster_status()`'s phase-1/phase-2 discovery pattern
  (already parallelized, already solid) into a pool-state tracker: free /
  busy / unreachable per host, independent of a fixed 2-host assumption.
- Add a `POST /api/allocate {"task": ..., "latency_class": ...}` endpoint
  that resolves capability → recipe, picks free hosts from the pool
  (first-fit is enough at this scale — no need for real bin-packing yet),
  and calls the existing `execute_deployment` under the hood. This sits
  *above* `/api/deploy`, which stays as-is for direct/manual use.

**Verification:** integration test against the pool-state tracker with
mocked host states (some free, some busy, some unreachable) before pointing
a real harness at it.

**Depends on:** Phase 3 (an allocator over 2 fixed hosts isn't worth
building — the value is in allocating across a pool).

**Risk:** this is the largest net-new build in the plan, not a refactor —
scope it as its own project once Phases 1–3 are stable, not squeezed in
alongside them.

---

## Phase 5 — Ongoing hardening backlog
*Interleave opportunistically, not blocking, not strictly ordered.*

Ongoing hardening items, not urgent enough to block a phase but worth not
losing track of. Highlighting the ones worth moving up given the system is
now multi-user:

- **Auth on `/api/deploy`, `/api/teardown`, `/api/toggle-network`, and the
  wide-open + likely-invalid CORS config.** Filed as lowest priority under
  a single-user assumption that no longer holds — see Open Decisions below.
- **`HF_TOKEN` via env-file mount instead of `-e HF_TOKEN=...` in process
  args**, since it's now visible to more than one person's `docker inspect`.
- **Per-user attribution.** The shared `tetrel` SSH identity means
  `auth.log` on the Sparks can't distinguish which teammate ran what — only
  the unauthenticated, self-reported `user_id` field does that today.
- **[New, 2026-08-20] Contain `build_catalog_response()`'s blast radius.**
  One malformed recipe currently fails the *entire* catalog, not just
  itself (see the Phase 2 status note above for the incident this caused).
  Removing the `name:`/filename mismatch closed the one trigger we hit,
  but the underlying all-or-nothing failure mode is still there and will
  bite again the next time any recipe fails validation for any other
  reason. Fix: catch per-recipe validation errors inside the load loop,
  skip and warn (recipe name + reason) instead of propagating, return a
  catalog with everything *except* the bad file(s) rather than an empty
  one.
- **[New, 2026-08-20] `get_cluster_status()` polling hang + duplicate
  load-time recording — landed, but the pattern is worth generalizing.**
  Fixed: single-flight de-duplication + a hard per-call timeout
  (`STATUS_CALL_TIMEOUT_SEC`) so one unreachable host can't stall the
  whole status endpoint, plus `record_load_time()` no longer fires on
  every poll while a container sits idle-but-ready (it was recording an
  ever-growing "load time" once per poll interval instead of once at
  actual readiness). Backlog item: `execute_deployment`'s SSH-heavy paths
  (teardown, GPU clock lock, per-host docker run) have no equivalent
  per-call timeout ceiling yet — same class of risk (one unreachable host
  stalling an otherwise-independent operation), not yet audited the same
  way `get_cluster_status()` was.
- **[New, 2026-08-20] `EUGR_CONTAINER_IMAGE_MAP` maintenance.**
  `tools/translate_eugr_recipes.py`'s one manual-decision point — an
  unmapped `container:` value blocks translating that one recipe until a
  human adds an entry. Low-touch (per `EUGR-REFERENCE-NOTES.md`'s
  2026-08-20 update, eugr's own docs now steer new recipes toward the
  no-mapping-needed default), but worth a standing reminder here rather
  than only living in a script comment.
- Lower urgency, same backlog: `BackgroundTasks` for the blocking deploy
  path, per-model load-time defaults in the recipe instead of hardcoded in
  Python, `authorize-key` dedup, non-root Dockerfile user, pinned `nginx`
  tag, a `HEALTHCHECK`.
- **[New, 2026-08-20] Vestigial file cleanup.** `docker-compose.cluster.yml`
  / `docker-compose.standalone.yml` (dead — nothing reads them,
  `_execute_deployment_impl` builds `docker run` commands directly),
  `patch.py` (a stale one-shot find/replace script targeting code that has
  since changed shape — its target strings no longer match, so it's
  already inert), and a stray zero-byte file. Low-priority housekeeping,
  noted here so it isn't lost.

---

## Recipe schema (draft)

**Corrected 2026-08-20** — the version of this draft below previously
showed `schema_version:` and a `name:` field. Neither matches what
actually shipped: the real field is `recipe_version:` (not
`schema_version:`), and `name:` was removed entirely after the incident
described in the Phase 2 status note above — the recipe's catalog key is
its filename stem, and only its filename stem. This draft is now the
actual current shape (matches `common/recipes.py::RecipeConfig` and
`recipes/local/*.yaml` on disk), not aspirational.

```yaml
recipe_version: 1
# No `name:` field -- the filename (e.g. this file being
# qwen-2.5-coder-32b.yaml) IS the catalog key. See the correction note
# above for why that's deliberate, not an omission.
hf_path: Qwen/Qwen2.5-Coder-32B-Instruct
image: nvcr.io/nvidia/vllm:26.07-py3   # omit to use cluster_config's default_image
gpu_util: 0.70

# Optional now, real starting in Phase 4. Populate as you go, don't block
# recipe creation on filling these in.
capability:
  task: coding
  context_class: 32k
  latency_class: standard

# Optional runtime patches applied before `vllm serve` starts. Empty until
# a model actually needs one -- see eugr's mods/ directory for the pattern
# (a run.sh plus vendored payload files) this is modeled on. Execution
# mechanism decided 2026-08-29: mods are baked into a derived image layer
# before launch, NOT docker exec'd into a running container (the 2026-08-20
# exec plan was superseded -- see this doc's Phase 2 mods entry, and
# ROADMAP.md for the full design). Payloads must be vendored in-repo; no
# network fetches at bake time.
mods: []

topologies:
  1_node:
    max_model_len: 32768
    tp_size: 1
    pp_size: 1
    env_vars:
      - OMP_NUM_THREADS=16
      - VLLM_CPU_OMP_THREADS=16
    vllm_args: >-
      --trust-remote-code --kv-cache-dtype fp8 --enable-chunked-prefill
  2_node:
    cluster_only: true   # mirrors eugr's cluster_only/solo_only flags --
                          # fails loudly at load time, not at deploy time.
                          # Confirmed 2026-08-20 against real eugr recipes
                          # that this field is genuinely whole-recipe on
                          # their side (not per-topology like it is here)
                          # -- see EUGR-REFERENCE-NOTES.md and the Open
                          # Decisions entry below. Still inert on our side;
                          # not yet enforced anywhere.
    max_model_len: 131072
    tp_size: 1
    pp_size: 2
    env_vars:
      - OMP_NUM_THREADS=16
      - VLLM_CPU_OMP_THREADS=16
      - NCCL_CUMEM_ENABLE=0
    vllm_args: >-
      --disable-custom-all-reduce --trust-remote-code --kv-cache-dtype fp8
      --enable-chunked-prefill
```

## `cluster_config.yaml` (draft)

**Status: landed, plus more than originally drafted.** The `tuning:` block
below (`shm_size`, GPU clock lock, deploy wait/poll timeouts, JIT cache
size) wasn't in the original draft — added 2026-08-20 once those values
were found hardcoded as literals inside `_execute_deployment_impl` during
an unrelated debugging session. Documenting here since this file is meant
to track drift between plan and reality, not just the plan.

```yaml
ssh_user: tetrel
ssh_key: id_dgx_orchestrator
default_image: nvcr.io/nvidia/vllm:26.07-py3
recipes_dir: recipes
gpu_util_ceiling: 0.75   # enforced by the loader, not just documented

ports:
  vllm_api: 8000
  orchestrator_api: 5001
  ray: 6379
  master: 29500

container_names:
  standalone: vllm-standalone
  head: vllm-head
  worker: vllm-worker

# Added 2026-08-20, not in the original draft -- see the status note
# above. Defaults match what used to be hardcoded, so this section being
# entirely absent from an older config file is still valid (all fields
# have defaults in common/config.py's TuningConfig).
tuning:
  shm_size_1node: 16gb
  shm_size_2node: 64gb
  gpu_clock_lock: "300,1800"
  deploy_wait_timeout_sec: 900
  deploy_poll_interval_sec: 15
  jit_cache_maxsize_bytes: 10737418240

hosts:
  spark-4:
    alias: spark-9dbe
    management_ip: 10.0.14.43
    backplane_ip: 192.168.99.2
    volume_mount: /home/tetrel/.cache/huggingface:/root/.cache/huggingface
    active: true
  spark-3:
    alias: spark-6e63
    management_ip: 10.0.14.41
    backplane_ip: 192.168.99.1
    volume_mount: /home/tetrel/.cache/huggingface:/root/.cache/huggingface
    active: true
  # Future nodes can be pre-staged here with active: false — the loader and
  # allocator both ignore inactive hosts, so this is a safe place to write
  # down IPs/aliases as you plan the rack-out without them being live.

network:
  interface: enp1s0f0np0
  nccl_ib_hca: rocep1s0f0
```

The `active: true/false` flag is the concrete answer to "flexible enough to
add more nodes without needing them now" — you can write down the other
four Sparks' config as soon as you know their IPs, well before they're
racked, with zero effect on current behavior.

---

## Traceability: current → target

| Current location | Responsibility | Target |
|---|---|---|
| `models.yaml` → `hosts:` block | host inventory (unused today) | `cluster_config.yaml` → `hosts:`, then deleted |
| `models.yaml` → `models:` block | model + topology config | `recipes/*.yaml`, `models.yaml` deleted end of Phase 2 |
| `dgx-orchestrator.py` `HOSTS` dict | host inventory (copy 1) | `common/config.py` reads `cluster_config.yaml` |
| `cache_cluster_assets.py` `HOSTS` dict | host inventory (copy 2) | same |
| `benchmark.py` `DEFAULT_HOST_IP` | host inventory (copy 3, partial) | same |
| `run_ssh` / `resolve_user_identity_key` / `get_hf_token` in `dgx-orchestrator.py` | SSH plumbing | `common/ssh.py` |
| Near-identical copies in `cache_cluster_assets.py` | duplicate SSH plumbing | deleted, imports `common/ssh.py` |
| `"vllm-standalone"` / `"vllm-head"` / `"vllm-worker"` literals | container naming | `common/constants.py` → `ContainerRole` enum |
| Inline arg-building in `_execute_deployment_impl` | docker command construction | `common/docker_ops.py`, unit-testable without SSH |

---

## Open decisions

- **API auth, now that it's multi-user.** Token auth on `/api/deploy` and
  `/api/teardown` was originally deprioritized under a single-user
  assumption. Worth deciding now whether that assumption still holds, and
  if not, whether to fold a minimal shared-token check into Phase 1 rather
  than leaving it in the Phase 5 backlog indefinitely.
- **`eugr/` recipe sync cadence — partially resolved 2026-08-20.** The
  mechanism now exists and is real, not hypothetical:
  `eugr-samples/` (raw, unmodified sync target) →
  `tools/translate_eugr_recipes.py --write` (mechanical translation,
  never touches `recipes/local/` or `recipes/eugr/` directly) →
  `recipes/_translated_from_eugr/` (staging output) → human review → the
  reviewed file is moved into `recipes/local/` or `recipes/eugr/` by
  hand. What's still genuinely undecided is *cadence* — how often someone
  re-pulls `imrobertson/spark-vllm-docker-experiments` into
  `eugr-samples/` and re-runs the translator. Manual/occasional remains
  fine for now given the mods/`run.sh` porting work (Phase 2) is a bigger
  near-term priority than sync frequency.
- **Recipe pruning.** Confirm which of the current 14 cataloged models are
  actually in active use before migrating all of them in Phase 2.
- **`cluster_only` enforcement — still deferred, now with real evidence
  instead of a guess.** The field exists in the schema today
  (`common/recipes.py::TopologyConfig`) and round-trips through
  `load_recipes()`, but is deliberately inert: never read by
  `build_catalog_response()` or by `dgx-orchestrator.py`'s deploy path,
  same treatment as `capability`/`mods`. Confirmed (re-reading this doc's
  own Phase 2 section) that enforcement belongs in the **recipe loader, at
  load time** — not in `_execute_deployment_impl` at deploy time, which
  was floated and is wrong for this design. As of 2026-08-20 we have real
  eugr recipes confirming the exact shape of the mismatch this check would
  need to catch (not a guess anymore):
    - eugr's `cluster_only`/`solo_only` are whole-recipe flags, confirmed
      across 5 real files including two (`nemotron-3_5-lightning`,
      `qwen3-coder-next-fp8`) where both are `false`/absent and the same
      recipe is valid at *both* node counts. Our schema nests
      `cluster_only` per-topology instead (see the corrected draft
      above) — deliberately, since that's more expressive (a recipe could
      in principle want `1_node` cluster-only'd out and `2_node` not,
      which their whole-recipe flag can't express at all), but it does
      mean our version of "enforce this" is a genuinely different check
      than theirs, not a port of theirs.
    - We still only have `cluster_only`, not eugr's `solo_only` — see
      `EUGR-REFERENCE-NOTES.md`'s "Borrow directly" list, which names
      both, and the two solo-only real recipes
      (`diffusion-gemma-bf16`, `diffusion-gemma-nvfp4-thinking`) that
      would exercise it. Worth adding `solo_only` to the schema at the
      same time the validation rule gets pinned down, rather than a
      second migration.
  Still not blocking anything today, still fine to leave inert — but the
  "resolve concretely rather than guessing from docs" instruction from the
  previous version of this note has been satisfied; what's left is
  deciding the actual rule and writing it, whenever that becomes a
  priority. `verify_recipe_equivalence.py` needs a matching update
  *whenever* either field starts appearing in the catalog response (same
  treatment as the `hosts` exclusion it already has) — flagging here so
  that change isn't made in isolation later.
- **Mods execution mechanism — resolved twice, currently resolved as
  "bake a derived image layer" (2026-08-29).** Originally listed here as
  undecided; resolved 2026-08-20 in favour of eugr's `bash -c` /
  `docker exec` pattern; **that resolution was overturned 2026-08-29** once
  eugr's actual mod library was read rather than reasoned about. Of their
  ~10 mods, all but one are build-time modifications of the vLLM
  installation, and the decisive counterexample is `gpu-mem-util-gb`
  (patches a CLI argument parsed at process startup, so exec-after-RUNNING
  cannot work) — which the 08-20 plan had itself named as a first porting
  candidate. The one genuine runtime mod, `drop-caches`, turns out not to be
  a container-scoped operation at all and is tracked separately in
  `ROADMAP.md`. Current design, rationale, rejected alternatives and
  implementation sequence: **`ROADMAP.md` → "Model-specific mods: bake a
  derived image layer"**. Kept in this list rather than deleted because the
  reversal is itself worth knowing about — the lesson is that this decision
  was twice made from the mod *concept* and only became correct once the
  files were read.
- **Schema adoption — resolved 2026-08-20, not an open question
  anymore.** Whether to adopt eugr's flat `defaults:` + `command:`
  template shape as our own live schema (instead of maintaining a
  separate structured schema and translating at the boundary) came up
  directly. Decided no. Full reasoning lives in
  `EUGR-REFERENCE-NOTES.md`'s 2026-08-20 update section rather than
  duplicated here -- short version: their shape is optimized for a human
  interactively overriding CLI flags with nobody watching a template-
  rendering failure in real time; ours is optimized for a control plane
  where nobody's watching a given deploy at all, so the same looseness
  that helps them is a liability for us. Recorded here mainly so this
  doesn't get re-litigated from scratch next time someone notices how
  much of the eugr translation turned out to be mechanical.

## Suggested immediate next step

**Update 2026-08-28:** Superseding the 2026-08-20 update below — priorities
shifted after a real production TPS measurement came in well under
external reports for this hardware class (~14 tok/s decode observed vs.
30-60 tok/s reported elsewhere on comparable Spark clusters). That gap is
almost certainly the missing MTP/speculative decoding path, not a
migration-architecture problem, so it's tracked as its own document rather
than a phase here: **`BACKLOG-dspark-sm120-image.md`** (HIGH priority,
not started) — pulling and smoke-testing the `jasl/vllm` PR #41834 fork
against `deepseek-v4-flash-0731-dspark-sm120.yaml` is the concrete next
action, not any of the phases in this doc.

Alongside that: recipe catalog hygiene is now also a real, felt problem —
several recipes in the catalog can be selected and launched but are known
or suspected to fail (untested topology combinations, flag combinations
already documented as known-bad in `docs/TROUBLESHOOTING.md`, or ones that
were never actually exercised end-to-end). `ROADMAP.md`'s "Recipe-level
guardrails against known-bad flag combinations" entry already scopes a
linter for the flag-combination class of this problem but hasn't been
built. Worth broadening that effort to also cover an explicit
per-recipe/per-topology status marker (validated / unconfirmed / known-bad,
matching the confidence framework `docs/TROUBLESHOOTING.md` already uses)
surfaced in the dashboard dropdown and `dgx-config status`, so a recipe
that can be selected but is known not to work says so before someone
spends a cold-start cycle finding out. This needs an actual pass over the
current `recipes/local/*.yaml` and `recipes/eugr/*.yaml` files to be
concrete rather than hypothetical — not done as part of this update.

The `mods:` execution mechanism (Phase 2, previously the recommended next
step) is still open and still real, but no longer ahead of the above in
priority — it's not blocking anything the other two are blocking, and
neither performance nor a broken-recipe launch is something `mods:`
addresses.

**Update 2026-08-29:** that priority call has partly reversed. `mods:` is
now the thing blocking a whole class of models rather than a nice-to-have:
current-generation checkpoints (the triggering case being
`Gemma-4-26B-A4B-it-NVFP4`) require patched vLLM source to load at all, and
waiting for upstream is a ~6-month proposition per fix. The mechanism has
also been redesigned since the note above — see `ROADMAP.md`'s
"Model-specific mods: bake a derived image layer", which is now the
authoritative entry and carries a HIGH priority marker. Recipe catalog
hygiene remains real and unstarted; the DSpark work described above has
since completed successfully (see `BACKLOG-dspark-sm120-image.md`, now
recording a working validated configuration rather than an open
investigation).

---

*2026-08-20 update, superseded above but kept for history:* Phase 0 and
Phase 1 are done. Phase 2 is mostly done (recipe schema,
`recipes/{local,eugr}` split, dual-load burn-in all shipped) with one real
deliverable still open and now concretely scoped rather than vague: **the
`mods:` execution mechanism** (see Phase 2's new entry above) — porting
`mods/drop-caches` and `mods/gpu-mem-util-gb` from the real eugr repo is
the natural first real-world test of it, and both are already
independently useful for the Phase 5 OOM-watchdog gap.

Original Phase 0 framing, kept for history: it was intentionally small and
non-disruptive — a git tag, a `.dockerignore`, and a `--dry-run` flag. All
three happened without touching anything that runs against the live
cluster, and the `--dry-run` flag turned out to be exactly as useful as
hoped for verifying every phase since.

---

## Runtime robustness backlog

Day-to-day runtime robustness/behavior work (as opposed to the
config-format migration tracked above) lives in **`ROADMAP.md`**, tracked
against the control plane's own API version rather than this doc's Phase
1-5 numbering. It is the only copy — an earlier version of this document
had a stale, partial duplicate of that content appended below this point;
it's been removed rather than reconciled entry-by-entry, since `ROADMAP.md`
was confirmed to be the more current and complete of the two. Check there,
not here, for anything about teardown hardening, cache integrity, engine
health monitoring, or recipe-key collision detection.

