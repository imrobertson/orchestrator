# DGX Spark Cluster — Architecture & Migration Plan

Companion document to `README-REVIEW.md`. That file is a status snapshot of
fixes already applied and issues already identified. This one is the plan
for where the system goes next: a phased path from the current
`models.yaml`-monolith design to a config-driven, recipe-based, N-node-ready
control plane — without a big-bang rewrite, because this runs daily for you
and a few team members now.

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
5. **Multi-user changes the risk calculus.** `README-REVIEW.md` filed
   "no auth on the API" as lowest priority under single-user assumptions.
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
                              network interfaces, container naming
recipes/
  eugr/*.yaml                  periodic read-only sync from eugr's recipes/
  local/*.yaml                 your own — new models, forks of eugr's recipes
common/
  config.py                    loads + validates cluster_config.yaml and
                                the recipes/ directory (pydantic schemas)
  ssh.py                        run_ssh, key resolution, HF token lookup
                                (one implementation, not three)
  constants.py                  ContainerRole enum, etc. — no bare string
                                literals for container names anywhere else
  docker_ops.py                 docker run command builders, testable
                                without SSH or live hardware
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
  `.git`, `load_times.json`, `benchmark_ledger.csv`, `__pycache__/` — this
  was already flagged in `README-REVIEW.md` and costs nothing to do now.
- Add a `--dry-run` flag to `execute_deployment` that builds and prints the
  full `docker run` argument list without SSHing or executing anything.
  This is the single most useful tool for the phases that follow: it lets
  you diff "old code's command for model X" against "refactored code's
  command for model X" byte-for-byte, for every model in the catalog, in
  seconds, with zero cluster risk.
- Write a 10-line smoke-test script: hits `/api/status`, asserts both hosts
  report `reachable`, asserts the catalog loads and is non-empty. Run it
  after every phase below as a go/no-go gate.

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
- Design the recipe schema (draft below) — borrows eugr's field name
  `recipe_version` (not `schema_version` — matching their exact naming means
  a recipe copied wholesale from `recipes/eugr/` drops in with zero
  translation) and their `cluster_only` / `solo_only` flags, since those
  catch real configuration errors (deploying a topology combo that doesn't
  exist) before deploy time instead of after.
- **Two validation failure modes, confirmed from eugr's actual
  `run-recipe.py`, worth copying exactly:** a `recipe_version` your loader
  doesn't recognize is a **soft warning** ("some features may not work
  correctly") that still proceeds — recipes shouldn't become unusable just
  because the loader is a version behind. A `cluster_only`/`solo_only`
  mismatch is a **hard error** that refuses to deploy, with the exact
  actionable message shape eugr uses:
  ```
  Error: Recipe 'X' requires cluster mode.
  This model is too large to run on a single node.

  Options:
    1. Deploy with --nodes 2
    2. ...
  ```
  Your current `_execute_deployment_impl` just returns `"Topology '2_node'
  not supported for model X"` — worth upgrading to this shape while you're
  already touching this validation path.
- **Design note — recorded here because a future reader diffing your
  recipes against a borrowed eugr recipe will notice the shape doesn't
  match, and should know it's deliberate, not drift:** eugr's real recipes
  have no `tp_size`/`pp_size` fields at all. Node count is derived
  downstream (in `launch-cluster.sh`) by parsing `-tp`/`-pp`/`-dp` back out
  of the rendered command string, so there's exactly one source of truth
  (the command) at the cost of a regex doing topology inference. Your
  schema keeps `tp_size`/`pp_size`/`max_model_len` as explicit typed fields
  per topology instead, with `vllm_args` remaining the free-text escape
  hatch for everything else — more redundant, but it's the version a Phase
  4 allocator can query numerically without parsing anything. Deliberate
  divergence, not an oversight.
- Split `recipes/` into `eugr/` and `local/`. `eugr/` is a periodic,
  read-only-by-convention sync from eugr's `recipes/` directory for models
  where their tested build/flags are the right answer — pin to a specific
  commit or tag when you sync, don't track their `main` live. `local/` is
  everything you write yourselves, including forks of an `eugr/` recipe
  that need a tweak. The loader refuses to start if a name collides between
  the two directories, rather than silently picking one.
- Add an optional `mods:` field to the recipe schema now — a flat list of
  mod directory paths (e.g. `mods/fix-glm4-moe`), matching eugr's actual
  format exactly (confirmed against their real recipes and `run-recipe.py`,
  not guessed from a forum post as in an earlier pass of this doc). Each mod
  is a directory (or zip) containing a `run.sh` entrypoint plus whatever
  supporting files it needs. `common/docker_ops.py` gets a
  `apply_mod_to_container()` equivalent: copy the mod directory into the
  container (via `scp` + `docker cp` for a remote host, same as your
  existing SSH plumbing), then `docker exec ... bash -c "cd <path> &&
  ./run.sh"` before the main `vllm serve` launch — same idea as eugr's,
  just driven by your off-node SSH call instead of their local/SSH dual
  path. Doesn't need real content on day one; the field existing means
  you're not doing a second schema migration when the first model actually
  needs a patch.
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

**Verification / rollback:** the dual-load comparison above is the
verification. Rollback is trivial during burn-in — `models.yaml` still
exists and the loader can be flipped back to it with one flag until you
delete it for good.

**Depends on:** Phase 1 (recipes still need `cluster_config.yaml` for
volume mounts, ssh user, etc. — recipes describe the model, not the host).

**Risk:** medium — this is the phase with the most hand-migration (14 model
configs → 14 files), so it's the one most worth the dual-load safety net.

---

## Phase 3 — N-node generalization
*Gated on hardware. Do not start until the additional Sparks are actually racked.*

**Goal:** remove the places where "2" is hardcoded as a fact about the
universe rather than a fact about your current hardware.

**Changes (deferred, listed so the direction is clear when the time comes):**
- `nodes: Literal[1, 2]` → validated `int` checked against active host count
  in `cluster_config.yaml`, both in the Pydantic model and the CLI args.
- Head/worker binary role → rank-based host list (rank `0..N-1`). The
  Ray-backed path (`ray start --address=...`) already generalizes to N
  workers with no real change; the manual NCCL path (`--nnodes` /
  `--node-rank` / `--master-addr`) needs its host-selection logic to pull
  from a pool instead of a fixed pair.
- `CLUSTER_OP_LOCK` (currently one global lock) → per-host locking, so a
  2-node deploy on spark-3/4 and an independent 2-node deploy on two other
  nodes can run concurrently instead of one blocking the other for no
  reason.
- `execute_teardown`'s always-nukes-everything behavior → accept a
  `target_hosts` param at the API/CLI level (the function already accepts
  it internally; this was already flagged in `README-REVIEW.md`).
- Recipe topology keys generalize from `1_node`/`2_node` to whatever node
  counts actually apply (`4_node`, etc.) — this needs no schema change,
  just more keys, since the schema was never restricted to exactly two.
- **Networking depends on how the new Sparks get physically wired, decide
  before writing code.** Per eugr's `NETWORKING.md`: TP works best at
  power-of-2 node counts (2/4/8); a 3-node (or non-power-of-2) mesh is
  mainly useful for pipeline/data parallelism instead. Two wiring options
  once you're past a pair:
  - **A proper switch** (e.g. Mikrotik CRS8xx-DDQ) — each Spark connects to
    the switch like today's pair, `cluster_config.yaml`'s `network:` block
    stays basically as-is, just with more `hosts:` entries.
  - **A cable mesh with no switch** — needs a different NIC wiring per
    node (port 0 on one Spark to port 1 on the next) and different NCCL
    settings than the current pair uses: `NCCL_IB_MERGE_NICS=0`,
    `NCCL_NET_PLUGIN=none`, `NCCL_IB_SUBNET_AWARE_ROUTING=1`, plus a
    dedicated out-of-band interface for coordination traffic (their 3-node
    example uses the onboard 10GbE port, not the ConnectX mesh links).
  Add a `network.topology: switched | mesh` field to `cluster_config.yaml`
  now (even unused) so this isn't a third schema migration — same
  "decide the field early, populate it late" principle as the recipe
  capability fields in Phase 2.

**Verification:** same `--dry-run` diffing pattern as Phase 1, plus this is
the first phase worth actually load-testing two concurrent independent
deploys once the per-host locking lands.

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

Everything already catalogued in `README-REVIEW.md`'s "Known issues"
section belongs here. Highlighting the ones worth moving up given the
system is now multi-user:

- **Auth on `/api/deploy`, `/api/teardown`, `/api/toggle-network`, and the
  wide-open + likely-invalid CORS config.** Filed as lowest priority under
  a single-user assumption that no longer holds — see Open Decisions below.
- **`HF_TOKEN` via env-file mount instead of `-e HF_TOKEN=...` in process
  args**, since it's now visible to more than one person's `docker inspect`.
- **Per-user attribution.** The shared `tetrel` SSH identity means
  `auth.log` on the Sparks can't distinguish which teammate ran what — only
  the unauthenticated, self-reported `user_id` field does that today.
- Lower urgency, same backlog: `BackgroundTasks` for the blocking deploy
  path, per-model load-time defaults in the recipe instead of hardcoded in
  Python, `authorize-key` dedup, non-root Dockerfile user, pinned `nginx`
  tag, a `HEALTHCHECK`.

---

## Recipe schema (draft)

```yaml
recipe_version: "1"   # matches eugr's field name exactly, so a recipe copied
                       # wholesale from recipes/eugr/ needs no translation.
                       # An unrecognized version is a soft warning at load
                       # time (recipe still runs) - not a hard failure. That's
                       # a different failure mode than cluster_only/solo_only
                       # below, which DO hard-fail; don't conflate the two.
name: qwen-2.5-coder-32b
hf_path: Qwen/Qwen2.5-Coder-32B-Instruct
image: nvcr.io/nvidia/vllm:26.07-py3   # omit to use cluster_config's default_image
gpu_util: 0.70

# Optional now, real starting in Phase 4. Populate as you go, don't block
# recipe creation on filling these in.
capability:
  task: coding
  context_class: 32k
  latency_class: standard

# Optional runtime patches applied before `vllm serve` starts. Each entry is
# a path to a mods/<name>/ directory containing a run.sh entrypoint (plus
# whatever supporting files it needs) - confirmed against eugr's actual
# mods mechanism, not the curl+patch one-liner assumed in an earlier pass
# of this doc. common/docker_ops.py copies the directory into the container
# and runs `cd <path> && ./run.sh` before the main vllm serve launch.
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
    cluster_only: true   # mirrors eugr's cluster_only/solo_only flags -
                          # hard-fails at deploy time with an actionable
                          # message (see Phase 2), not a silent fallback
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

**Why this schema keeps explicit `tp_size`/`pp_size` instead of deriving
node count from parsed vLLM flags the way eugr's real recipes do:** see the
design note in Phase 2 above. Short version - eugr's way has zero
redundancy but requires regex-parsing rendered command text to know the
topology; this schema is more redundant but lets Phase 4's allocator query
node requirements numerically without parsing anything. Deliberate,
recorded here so it doesn't look like an oversight next to a borrowed
`recipes/eugr/*.yaml` file that does it differently.

## `cluster_config.yaml` (draft)

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
  topology: switched   # switched | mesh - unused until Phase 3, but the
                        # field exists now so adding it isn't a schema
                        # migration later. See NETWORKING.md notes in Phase 3.
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
  `/api/teardown` was explicitly deprioritized in `README-REVIEW.md` under
  a single-user assumption. Worth deciding now whether that assumption
  still holds, and if not, whether to fold a minimal shared-token check
  into Phase 1 rather than leaving it in the Phase 5 backlog indefinitely.
- **`eugr/` recipe sync cadence.** How often to re-pull eugr's `recipes/`
  into `recipes/eugr/`, and whether that's a manual occasional task or
  something scripted.
- **Mods execution mechanism.** Whether `mods:` commands get wrapped in the
  same `bash -c` pattern already used for the Ray-head exec, or something
  more structured — fine to decide when the first real mod is needed rather
  than now.
- **Recipe pruning.** Confirm which of the current 14 cataloged models are
  actually in active use before migrating all of them in Phase 2.

## Suggested immediate next step

Phase 0 is intentionally small and non-disruptive: a git tag, a
`.dockerignore`, and a `--dry-run` flag. All three can happen this week
without touching anything that runs against the live cluster, and the
`--dry-run` flag is what makes every later phase mechanically verifiable
instead of "looks right, deploy it and see." Worth doing before Phase 1
starts, regardless of how the rest of the timeline shakes out.
