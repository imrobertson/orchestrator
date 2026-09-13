# DGX Cluster Control Plane (`orchestrator`)

Recipe-based control plane for managing distributed vLLM deployments across an
NVIDIA Grace Blackwell (GB10) DGX Spark cluster. Provides a FastAPI
orchestration daemon, a web dashboard, and the `dgx-config` CLI wrapper.

**Setting up a new control station?** → **[`docs/INSTALL.md`](docs/INSTALL.md)**.
That guide is written to be followed from scratch on any LAN, not just this one.

---

## Contents

- [What runs where](#what-runs-where)
- [Configuration](#configuration)
- [Model catalog](#model-catalog)
- [Deployment & topology architecture](#deployment--topology-architecture)
- [Network fabric & transport](#network-fabric--transport)
- [Grace Blackwell (GB10) hardware safeguards](#grace-blackwell-gb10-hardware-safeguards)
- [Installation](#installation)
- [User onboarding & key authorization](#user-onboarding--key-authorization)
- [Interface reference](#interface-reference)
- [Operational documentation](#operational-documentation)

---

## What runs where

### `maestro` — control station & dashboard host

Central control node. Runs the Docker Compose stack; needs no GPU.

| Component | Container | Port |
| --- | --- | --- |
| Web dashboard (nginx) | `dgx-dashboard-ui` | `5000` |
| API orchestration daemon | `dgx-orchestrator-api` | `5001` |

The Python application is fully isolated inside a `python:3.12-slim` image,
bypassing PEP 668 host constraints. The repo directory is bind-mounted to
`/app`, so a code edit on `maestro` takes effect on the next container restart
without a rebuild.

The dashboard is served on `5000` but its JavaScript calls the API at
`http://<same hostname>:5001/api` (`API_PORT` in `html/index.html`). **Both
ports must be reachable from the browser** — opening only `5000` gives a page
that loads and then reports "API Connection Lost".

### Compute nodes

Host roles are **read from `cluster_config.yaml`, not hardcoded**.
`PRIMARY_HOST` / `SECONDARY_HOST` / `PRIMARY_HOST_IP` in `dgx-orchestrator.py`
are derived from the first/second entry under `hosts:` (see
`docs/TOMBSTONES.md` #73). Pointing the orchestrator at a different host pair is
a config change, not a code change.

Current cluster, as declared in `cluster_config.yaml`:

| Host | Alias | Management IP | `role:` | Notes |
| --- | --- | --- | --- | --- |
| `spark-3` | `spark-6e63` | `10.0.14.41` | `head` | Listed first → `PRIMARY_HOST`. `reserved: true` (carries the resident hermes agent) |
| `spark-4` | `spark-9dbe` | `10.0.14.43` | `worker` | `default_deploy_target` — where an unqualified 1-node deploy lands |

Both are NVIDIA Grace Blackwell GB10, 128 GB LPDDR5x unified memory, running
headless.

**Docker boot policy:** Docker auto-start is disabled on both nodes to prevent
GPU driver panics or OOM loops on host reboot. Start manually with
`systemctl start docker`.

**Host model cache:** `/home/tetrel/.cache/huggingface` is mapped to
`/root/.cache/huggingface` inside vLLM containers via each host's
`volume_mount`, guaranteeing zero re-downloads across cold restarts. The same
host cache root also derives the JIT-compile cache mounts for Triton, TileLang,
DeepGEMM and vLLM (`/root/.cache/{triton,tilelang,deepgemm,vllm}`) plus
`/root/.nv/ComputeCache` for the CUDA compute cache.

---

## Configuration

`cluster_config.yaml` at the repo root is the single source of truth, loaded and
validated by `common/config.py`. Validation is strict and happens at load — a
bad value raises with the file path and the specific problem rather than
falling back to a default.

| Key | Purpose |
| --- | --- |
| `ssh_user` | Account the orchestrator SSHes into the compute nodes as |
| `ssh_key_name` | Filename of the shared cluster key, expected at the repo root |
| `default_image` | Cluster-wide container image; overridable per recipe |
| `gpu_util_ceiling` | Advisory ceiling on a recipe's `gpu_util` |
| `gpu_util_ceiling_enforce` | `off` (default) / `warn` / `error` |
| `default_deploy_target` | Host an unqualified **1-node** operation lands on |
| `global_hf_hub_offline`, `global_transformers_offline` | Cluster-wide offline switches, toggled at runtime by `/api/toggle-network` |
| `ports:` | `vllm_api`, `orchestrator_api`, `ray`, `master` |
| `container_names:` | `standalone`, `head`, `worker` |
| `hosts:` | Per-host inventory — `alias`, `role`, `management_ip`, `backplane_ip`, `volume_mount`, `active`, `reserved` |
| `network:` | `topology`, `interface`, `nccl_ib_hca` |
| `tuning:` | Deploy-time knobs — see below |

### Host roles vs. host purpose

Two keys control where unqualified work lands and what it is allowed to
disturb. Both are optional; omit them and the orchestrator behaves exactly as it
did before they existed.

- **`default_deploy_target:`** (top level) — which host a 1-node operation goes
  to when nothing names one. Affects `deploy` with no `--head`,
  `tests/ab_test.py` with no `--host`, and the interactive menu's default
  selection. **2-node deploys are not affected**: the Ray head stays on the
  first host listed under `hosts:`, so the head node is consistent across
  topologies. Unset, this falls back to that same first host.

  This key exists because the first host under `hosts:` was doing two unrelated
  jobs — naming the structural head node and the telemetry-authoritative serving
  host, *and* acting as the fallback target for anything unqualified. Those
  conflict as soon as one node holds something long-lived: the node you most
  want to stay authoritative is also the node a bare `deploy` would flatten.

- **`reserved: true`** (per host, under `hosts:`) — marks a host as carrying
  something that must not be disturbed by routine work. Deploy and teardown both
  refuse to touch it without `--force`, including whole-cluster teardowns. This
  is a property of the **machine**, not of any deployment: unlike `role:`, which
  describes a host's part in one particular topology, being reserved is a
  statement about what the box is for.

`default_deploy_target` is validated at load to name a known, `active: true`,
**non-reserved** host. A reserved default target would be self-defeating — every
unqualified deploy would land on the one host that then refuses it.

Note that 2-node deploys always span both hosts, so any 2-node work needs
`--force` while either host is reserved, and will take that host's service down.
That is intended: it should be a decision, not a surprise.

### `tuning:` knobs

| Key | Default | Purpose |
| --- | --- | --- |
| `shm_size_1node` / `shm_size_2node` | `16gb` / `64gb` | `--shm-size` passed to `docker run` |
| `gpu_clock_lock` | `300,1800` | Passed to `nvidia-smi -lgc` before every deploy, for benchmark consistency |
| `deploy_wait_timeout_sec` / `deploy_poll_interval_sec` | `900` / `15` | `wait_for_cluster_ready()` budget and cadence under `--wait` / `--benchmark` |
| `jit_cache_maxsize_bytes` | `10737418240` | `CUDA_CACHE_MAXSIZE` for the mounted JIT cache |
| `debug_launch_blocking` | `false` | Sets `CUDA_LAUNCH_BLOCKING=1`. Costs real decode throughput — only for chasing a live repro |
| `crash_log_retention_days` | `7` | Age-based retention for persisted Ray/crash logs |

Omitting the `tuning:` section entirely keeps an older config working unchanged.

---

## Model catalog

The catalog lives in `recipes/local/*.yaml` — one file per model. A recipe's
catalog key is its filename stem and nothing else; there is no `name:` field
inside the YAML to drift out of sync with it.

This is a living, growing set: new variants (different precisions,
context/throughput tradeoffs, single-node vs. multi-node builds of the same base
model) get added as needed, not on a fixed schedule. **Treat `dgx-config status`
or the dashboard dropdown as the source of truth over any static documentation,
including this file.**

### Recipe shape

`common/recipes.py` is the authoritative schema — the model there carries the
full field set with rationale comments. A representative recipe
(`recipes/local/gemma4-26b-a4b-nvfp4.yaml`, trimmed):

```yaml
recipe_version: "1"
hf_path: nvidia/Gemma-4-26B-A4B-NVFP4
image: eugr/spark-vllm:latest      # overrides cluster_config.yaml's default_image
gpu_util: 0.75
mods: []
notes: >
  Free-form. Surfaced in the dashboard under the metadata strip.
topologies:
  1_node:
    max_model_len: 32768
    tp_size: 1
    pp_size: 1
    env_vars: []
    vllm_args: >-
      --quantization modelopt --kv-cache-dtype fp8 --trust-remote-code
```

Optional fields, all verified against `common/recipes.py`:

| Field | Purpose |
| --- | --- |
| `image:` | Overrides `cluster_config.yaml`'s `default_image` |
| `entrypoint:` | Passed to `docker run --entrypoint`. `""` neutralizes the image's own ENTRYPOINT — required for anything built on the official `vllm/vllm-openai` base. Absent and `""` are different launches; test with `is not None`, never truthiness |
| `model_path_override:` | The literal `--model` argv value, when it must be a local directory rather than an HF repo id. Its last path segment must match `hf_path`'s basename, case included |
| `extra_mounts:` | Extra `-v` bind mounts, applied identically on every host. Usually paired with `model_path_override` |
| `launch_argv_prefix:` | The argv before the engine flags. Default is `python3 -m vllm.entrypoints.openai.api_server`; a multi-node worker needs `["vllm", "serve", "{model}"]`, because only `vllm serve` honours `--headless` |
| `gpu_util_ceiling_exempt:` | Standing permission to exceed `gpu_util_ceiling`. Excluded from `config_hash` |
| `mods:` | Bare directory names under `mods/`, resolved and baked into a derived image tag before launch. **Order-significant** |
| `capability:` | `task` / `context_class` / `latency_class`. Reserved for Phase 4, deliberately inert |
| `notes:` | Free-form; surfaced in the dashboard |
| `topologies.<n>.cluster_only:` | Recipe-authoring metadata. Currently inert — read by nothing |

`compute_config_hash()` is at schema 5. It covers everything that changes what
actually launches; `capability`, `notes` and `gpu_util_ceiling_exempt` are
excluded. Every schema bump orphans existing launch history, which is why
recipes periodically revert to showing as never-launched.

---

## Deployment & topology architecture

### 1. Dynamic container image resolution

Image tags are not hardcoded into the Python orchestrator. `cluster_config.yaml`
declares a global `default_image`, so cluster-wide updates only require a YAML
change. Individual recipes override this with a model-level `image:` key.

### 2. Global multi-model VRAM teardown guard

To prevent multiple vLLM models stacking on the same GPU and causing immediate
CUDA OOM or port `8000` conflicts, `execute_deployment` tears down any existing
containers across all target nodes before spawning anything.

Teardown is graceful, not an immediate kill: it sends SIGTERM to host processes
and issues `docker stop` (allowing each container up to `TEARDOWN_GRACE_SEC` to
exit cleanly) before falling back to `docker rm -f` as a backstop. All target
hosts are torn down **concurrently** — sequential per-host teardown left a
worker briefly alive and NCCL-connected to an already-vanished head during a
prior release. Expect up to roughly `3 × TEARDOWN_GRACE_SEC` on a 2-node deploy;
the dashboard's Teardown button reflects live phase progress throughout.

### 3. OpenMP thread fencing

To stop PyTorch and vLLM background worker threads consuming 100% of the Grace
ARM cores during multi-node KV cache initialization, multi-node topologies set
`OMP_NUM_THREADS=16` and `VLLM_CPU_OMP_THREADS=16` per-recipe in `env_vars:`.
This guarantees CPU scheduling headroom for `sshd`, system daemons, and status
polling threads.

### 4. JIT-compile caching

Every deploy mounts a persistent JIT-compile cache covering Triton, TileLang,
DeepGEMM and vLLM's own kernel cache, plus the CUDA compute cache — all derived
from the same host directory as the HuggingFace cache mount and sized via
`tuning.jit_cache_maxsize_bytes`.

`dgx-config cache-inventory` inspects what is cached per host (entry counts,
sizes, age, LRU order) — read-only, safe against a live cluster.
`dgx-config prune-cache` performs LRU eviction of **whole cache entries**, and
only when a host is below a configurable free-space floor. It never deletes an
individual file within an entry, since a partially-deleted Triton/TileLang entry
can leave a state the loader treats as a hit and then fails to load.

### 5. Crash log persistence

Every deploy binds a per-run, per-host directory
(`~/.cache/ray-logs/<deploy_run_id>/<host>`) to each container's `/tmp/ray`, so
a crashed worker's Ray session logs — stdout/stderr included — survive container
teardown. This is the difference between a crash being diagnosable and
permanently a mystery.

`dgx-config prune-ray-logs --retention-days N --dry-run` reclaims this space on
an age basis (default 7 days) rather than waiting on free-space pressure, since
these logs stay small enough to otherwise accumulate indefinitely.

### 6. Version tracking & deploy confirmation

`ORCHESTRATOR_VERSION` in `dgx-orchestrator.py` is a hand-bumped string surfaced
in four places: daemon startup logs, every `/api/status` response, the CLI
`status` summary line, and a badge in the dashboard header next to Server Time.

This exists because "the code was edited but never actually deployed" is a real
failure mode here — the deploy workflow (`git push` locally → `git pull` on
`maestro` → restart the container) has a step where a forgotten `git push`
silently leaves the daemon running old code with no error anywhere. **Check the
version badge after any deploy of a `dgx-orchestrator.py` change** rather than
assuming it landed.

Caveat: this hashes `dgx-orchestrator.py` only, so a change confined to
`common/*.py` will not move it. It answers "which orchestrator", not "which
entire codebase".

### 7. Status cache staleness

`/api/status` responses carry `stale` / `stale_for_seconds`, reflecting how long
the currently-served snapshot has actually been in use. A wedged internal
computation surfaces this instead of silently serving a frozen snapshot
indefinitely. **If the dashboard ever looks frozen, check these two fields
first.** Not yet surfaced as a dashboard banner; API-only.

### 8. Deployment-stage result contract

Every deploy now reports an ordered `stages` list in addition to the existing
`status`, `message`, `targets`, and `head` fields. Each stage has an explicit
`success`, `error`, or `skipped` result; an error response also names
`failed_stage`. A deployment cannot report success after any required stage
has failed.

The deploy path consumes teardown, host preparation, container launch, Ray
registration, detached engine launch, container-survival, state-recording,
readiness, and requested benchmark results. In particular, a `--wait` timeout
is an error rather than a plausible success, and a failed detached Ray engine
command is surfaced immediately. A deploy without `--wait` still returns after
launch and container-survival checks; its readiness and benchmark stages are
reported as `skipped`, not implied to have passed.

---

## Network fabric & transport

- **Management TCP interface (`enp1s0f0np0`)** — all SSH orchestration,
  administrative commands, and `dgx-config` calls route strictly across the
  management subnet (`10.0.14.x`).
- **Master store rendezvous** — the orchestrator binds `--master-addr` to the
  head node's management IP. `--master-port` comes from `ports.master`
  (`29500`). This rides the management network, not the RoCE fabric.
- **Gloo & NCCL interface binding** — multi-node topologies must pass
  `GLOO_SOCKET_IFNAME=enp1s0f0np0` and `NCCL_SOCKET_IFNAME=enp1s0f0np0`.
- **`NCCL_CUMEM_ENABLE=0`** — multi-node GB10 deployments must set this across
  environment manifests to prevent IPC buffer deadlocks on unified memory.
- **RoCEv2 link layer (200 Gbps ConnectX-7)** — HCA target `rocep1s0f0`, GID
  index `NCCL_IB_GID_INDEX=3`.

---

## Grace Blackwell (GB10) hardware safeguards

### LPDDR5x unified memory telemetry

GB10 shares LPDDR5x unified memory between the Grace CPU and Blackwell GPU.
Standard VRAM queries return `[N/A]`. The telemetry parser isolates temperature
and utilization integers and reports memory as `Unified / 131072 MB`.

### Headless target mode

Compute nodes must never run a desktop GUI display manager (`gdm3`,
`gnome-shell`) — DRM semaphore locks otherwise interfere with the GPU. Convert
with:

```bash
sudo systemctl set-default multi-user.target
sudo systemctl stop gdm3
```

---

## Installation

Full step-by-step guide, including standing up a control station on a different
LAN and preparing fresh compute nodes: **[`docs/INSTALL.md`](docs/INSTALL.md)**.

Short version, for a `maestro` on an already-configured cluster:

```bash
mkdir -p ~/docker && cd ~/docker
git clone https://github.com/imrobertson/orchestrator.git
cd orchestrator

cp .secrets.example .secrets      # then edit in your real HF_TOKEN
chmod 600 .secrets

cp /path/to/id_dgx_orchestrator . # shared cluster SSH key
chmod 600 id_dgx_orchestrator

docker compose up -d --build
sudo ln -sf ~/docker/orchestrator/dgx-config /usr/local/bin/dgx-config

dgx-config status                 # verify
```

---

## User onboarding & key authorization

### Default workflow: SSO & Tailscale SSH

Users reaching `maestro` over Tailscale SSH need zero local setup. The
`dgx-config` wrapper captures the host shell's `$USER` and injects it into the
execution container (`-e USER`) for attribution.

### Local network / admin users

If bypassing Tailscale SSH, authorize your personal SSH key:

```bash
dgx-config authorize-key --key ~/.ssh/id_ed25519.pub
```

This authorizes the key for cluster access. It does **not** make per-deploy
attribution cryptographically verified — see `docs/USERMANUAL.md`'s
troubleshooting section if that distinction matters for your use case.

---

## Interface reference

### Web dashboard — `http://<maestro>:5000`

- API routing is dynamic via `window.location.hostname`, so the dashboard works
  under any hostname the browser can also reach on port `5001`.
- Live per-host telemetry, model status, and load ETA (monotonically clamped so
  the countdown never runs backwards between polls).
- Real-time Docker logs in a full-width bottom panel, with a Copy button that
  falls back to `execCommand` on plain-HTTP origins where the Clipboard API is
  unavailable.
- Deploy, Teardown and benchmark controls lock each other out while any one is
  in flight. Teardown shows live phase progress (signaling → stopping →
  removing).
- Reserved hosts are badged, and a deploy or teardown that would disturb one
  raises a per-action confirm dialog driven by the server's HTTP 423 refusal —
  deliberately **not** a persistent "force" checkbox, which could be left
  silently armed for a later operation.

### Interactive CLI menu — `dgx-config menu`

Renders active runtimes, throughput/queue depth and GPU telemetry across all
nodes, and prompts model selection directly from the recipe catalog.

### CLI commands

| Task | Command |
| --- | --- |
| Check status | `dgx-config status` |
| Deploy a model | `dgx-config deploy --model <key> --nodes 2` |
| Deploy and block until healthy | `dgx-config deploy --model <key> --nodes 2 --wait` |
| Preview a deploy | `dgx-config deploy --model <key> --nodes 2 --dry-run` |
| Deploy onto a reserved host | add `--force` |
| Tear down one host | `dgx-config teardown --host spark-4` |
| Tear down everything | `dgx-config teardown --all` |
| Remote container logs | `dgx-config logs --host spark-4 --tail 100` |
| Authorize an SSH key | `dgx-config authorize-key --key ~/.ssh/id_ed25519.pub` |
| Inspect JIT cache (read-only) | `dgx-config cache-inventory` |
| Reclaim JIT cache space | `dgx-config prune-cache --min-free-gb 50 --headroom-gb 20 --dry-run` |
| Reclaim crash log space | `dgx-config prune-ray-logs --retention-days 7 --dry-run` |
| Sweep orphaned IPC segments | `dgx-config sweep-ipc-orphans --dry-run` |
| Repair a ledger undercount | `dgx-config correct-ledger --dry-run` |

`teardown` deliberately has **no default scope** — it requires either `--host`
or `--all`. Most read-only-ish commands accept `--dry-run` and are safe to run
against a live cluster.

---

## Operational documentation

| Document | Contents |
| --- | --- |
| [`docs/DOCMAP.md`](docs/DOCMAP.md) | Which document owns what, and where a new fact goes. Paste it at the start of a session |
| [`docs/INSTALL.md`](docs/INSTALL.md) | Control-station and compute-node setup, from scratch |
| [`docs/USERMANUAL.md`](docs/USERMANUAL.md) | Operating the cluster: dashboard, `dgx-config`, deploy, teardown, offline mode, benchmarking, troubleshooting |
| [`docs/MODELS.md`](docs/MODELS.md) | The catalog: topology, context, concurrency, architecture, speculative method and depth, measured speed, validation status |
| [`docs/DIRECTION.md`](docs/DIRECTION.md) | Where the system is going, phase status, decisions of record. Supersedes `ROADMAP.md` and `ARCHITECTURE-MIGRATION-PLAN.md` |
| [`docs/WORKSTREAMS.md`](docs/WORKSTREAMS.md) | The canonical backlog: status, evidence, dependencies, kickoff prompts |
| [`docs/TOMBSTONES.md`](docs/TOMBSTONES.md) | Append-only per-fix history and incident log, newest and highest-numbered first |
| [`docs/errata.yaml`](docs/errata.yaml) | Machine-readable recipe rules — the linter's source of truth |
| [`docs/SMOKE-TEST-PLAYBOOK.md`](docs/SMOKE-TEST-PLAYBOOK.md) | Post-change verification procedure |
| [`docs/AB_TEST_USAGE.md`](docs/AB_TEST_USAGE.md) | A/B benchmark harness usage |
| [`docs/reference/`](docs/reference/) | Durable notes on third-party dependencies: `community-sources.md` (where every image, checkpoint and fix came from, with URLs) and `flashinfer-autotune-internals.md` |
| [`tools/smoke_test.py`](tools/smoke_test.py) | Control-plane go/no-go gate: every host reachable, catalog non-empty. Exit 0/1. Run it after any change before trusting a deploy — an empty catalog surfaces here in a second rather than as a confusing failure downstream |
| [`tools/doc_health.py`](tools/doc_health.py) | One command for the state of the documentation set. Tolerant of work done by hand; exit 0 healthy, 1 incomplete, 2 something lost |
| [`tools/file_inventory.py`](tools/file_inventory.py) | Every tracked file must have a documented purpose. This enforces it |
| [`tools/check_doc_references.py`](tools/check_doc_references.py) | Broken-link and phantom-file checker. Advisory; `--strict` to fail |
| [`tools/finalize_docs.py`](tools/finalize_docs.py) | Manifest-driven archive mover; refuses to retire anything whose content has not landed elsewhere |
| [`tools/run_verification.py`](tools/run_verification.py) | Read-only verification sweep against a live cluster. Writes one pasteable report |
| [`tools/real_dry_run_capture.py`](tools/real_dry_run_capture.py) | Dumps every model × topology's dry-run argv to JSON for byte-diffing before and after a refactor — the #90 verification pattern, automated. Note a 2-node capture holds only the Ray bootstrap |
| [`tools/verify/`](tools/verify/) | The verification harnesses behind individual tombstone entries. Not a test suite — see its `purpose.md` |

`UsageShortcut.md` is folded into `USERMANUAL.md`, `REFERENCE-decode-speeds.md`
into `MODELS.md`, and the EUGR notes into `docs/reference/community-sources.md`.
`TROUBLESHOOTING.md` is being dissolved into `TOMBSTONES.md` and `errata.yaml`.

Anything in `docs/` not listed above is archive material — see
[`docs/DIRECTION.md`](docs/DIRECTION.md)'s documentation map for the disposition
of every file.
