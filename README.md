= DGX Cluster Control Plane (`orchestrator`) — Recipe-Based Blackwell Architecture Edition =
This repository contains the `dgx-config` orchestration suite. It acts as the central control plane for managing distributed vLLM deployments across a twin-node RoCEv2 NVIDIA Grace Blackwell fabric.

== Target Infrastructure & Control Plane Mapping ==

=== 1. What Runs Where ===

* '''`maestro` (Control Station & Web Dashboard Host):'''
 '''Role:''' Central control node. Runs the Docker container stack for the web dashboard, API endpoints, and the `dgx-config` delegate wrapper.
 '''Web Dashboard (Nginx):''' Runs in the `dgx-dashboard-ui` container on port `5000` (`http://maestro:5000`).
 '''API Orchestration Daemon:''' Runs in the `dgx-orchestrator-api` container on port `5001`.
 '''Containerized Environment:''' The Python application is fully isolated within a `python:3.12-slim` image, bypassing PEP 668 host constraints.
* '''`spark-4` (Head Compute Node):'''
 '''Management IP:''' `10.0.14.43` (`spark-9dbe`).
 '''Role:''' Primary vLLM head execution target (`vllm-head` / `vllm-standalone`).
 '''Hardware:''' NVIDIA Grace Blackwell GB10 (128GB LPDDR5x Unified Memory running headless).
 '''Docker Boot Policy:''' Docker auto-start is disabled to prevent GPU driver panics or OOM loops on host reboot. Start manually via `systemctl start docker`.
* '''`spark-3` (Worker Compute Node):'''
 '''Management IP:''' `10.0.14.41` (`spark-6e63`).
 '''Role:''' Distributed vLLM worker execution target (`vllm-worker`).
 '''Hardware:''' NVIDIA Grace Blackwell GB10 (128GB LPDDR5x Unified Memory running headless).
 '''Docker Boot Policy:''' Docker auto-start is disabled. Start manually via `systemctl start docker`.
* '''Host Model Cache Storage:''' `/home/tetrel/.cache/huggingface` is mapped directly to `/root/.cache/huggingface` inside vLLM containers via `volume_mount` to guarantee zero re-downloads across cold restarts. The same host cache root also derives the JIT-compile cache mounts for Triton, TileLang, DeepGEMM, and vLLM (`/root/.cache/{triton,tilelang,deepgemm,vllm}`) plus `/root/.nv/ComputeCache` for the CUDA compute cache. Use `dgx-config cache-inventory` to see current usage per host, and `dgx-config prune-cache` to reclaim space when a host runs low (see CLI reference below).

== Configuration ==

`cluster_config.yaml`, at the repo root, is the single source of truth for:

* '''`hosts:`''' — host inventory (management IP, backplane IP, volume mount, `active` flag). Loaded via `common/config.py`.
* '''`ports:`''' — `vllm_api`, `orchestrator_api`, `ray`, `master`.
* '''`tuning:`''' — deploy-time knobs (`shm_size_1node` / `shm_size_2node`, `gpu_clock_lock`, `deploy_wait_timeout_sec`, `jit_cache_maxsize_bytes`).

== Model Catalog ==

The catalog lives in `recipes/local/*.yaml` and `recipes/eugr/*.yaml` — one file per model. `models.yaml` is deprecated (supported via `USE_LEGACY_CATALOG=1`). This is a living, growing set: new variants (different precisions, context/throughput tradeoffs, single-node vs. multi-node builds of the same base model) get added as needed, not on a fixed schedule — treat `dgx-config status` or the dashboard dropdown as the source of truth over any static documentation, including this file.

=== Recipe Schema ===

```yaml
recipe_version: '1'
hf_path: org/Model-Name
image: eugr/spark-vllm-b12x:latest
gpu_util: 0.75
capability:
  task: null
  context_class: null
  latency_class: null
mods: []
topologies:
  2_node:
    max_model_len: 393216
    tp_size: 2
    pp_size: 1
    env_vars: [...]
    vllm_args: >-
      --trust-remote-code ...

```

== Deployment & Topology Architecture ==

=== 1. Dynamic Container Image Resolution ===
Image tags are not hardcoded into the Python orchestrator. `cluster_config.yaml` declares a global `default_image` ensuring cluster-wide updates only require a YAML change. Individual recipes can override this with a model-level `image:` key.

=== 2. Global Multi-Model GPU VRAM Teardown Guard ===
To prevent multiple vLLM models from stacking on the same GPU and causing immediate CUDA Out-Of-Memory (OOM) or port `8000` conflicts:

* '''Pre-Deployment Purge:''' Before spawning any container, `execute_deployment` tears down any existing containers across all target nodes first. Teardown is graceful, not an immediate kill: it sends SIGTERM to host processes and issues `docker stop` (allowing each container up to `TEARDOWN_GRACE_SEC` to exit cleanly) before falling back to `docker rm -f` as a backstop for anything still standing. All target hosts are torn down concurrently, not one after another — sequential per-host teardown left a worker node briefly alive and NCCL-connected to an already-vanished head during a prior release, which this concurrency avoids. Expect teardown to take up to roughly `3 x TEARDOWN_GRACE_SEC` seconds on a 2-node deploy; the dashboard's Teardown button reflects live phase progress for the duration.

=== 3. OpenMP Thread Fencing & CPU Scheduling Protections ===
To prevent PyTorch and vLLM background worker threads from consuming 100% of available Grace ARM CPU cores during multi-node KV cache initialization, multi-node topologies enforce strict CPU thread limits:

* '''Environment Fencing:''' `OMP_NUM_THREADS=16` and `VLLM_CPU_OMP_THREADS=16` are set per-recipe in `env_vars:`.
* '''Host System Impact:''' Restricts OpenMP thread pools to 16 cores per socket, guaranteeing sufficient CPU scheduling headroom for `sshd`, system daemons, and status polling threads.

=== 4. JIT-Compile Caching ===
Every deploy mounts a persistent JIT-compile cache covering Triton, TileLang, DeepGEMM, and vLLM's own kernel cache, plus the CUDA compute cache, all derived from the same host directory as the HuggingFace cache mount and sized via `cluster_config.yaml`'s `tuning.jit_cache_maxsize_bytes`. Use `dgx-config cache-inventory` to inspect what's cached per host (entry counts, sizes, age, LRU order) — read-only, safe to run against a live cluster at any time. `dgx-config prune-cache` performs LRU eviction of whole cache entries, and only when a host is below a configurable free-space floor; it never touches an individual file within an entry, since partial deletion of a Triton/TileLang cache entry can leave it in a state the loader treats as a hit and then fails to load.

== Network Fabric & Transport ==

=== 1. Network Interface Targets & Gloo/NCCL Bindings ===

* '''Management TCP Interface (`enp1s0f0np0`):''' All SSH orchestration, administrative commands, and `dgx-config` calls route strictly across the management subnet (10.0.14.x).
* '''Master Store Rendezvous:''' The orchestrator binds `--master-addr` to the head node's management IP (`10.0.14.43` for `spark-4`). `--master-port` comes from `cluster_config.yaml`'s `ports.master` (`29500`).
* '''Gloo & NCCL Interface Binding (`enp1s0f0np0`):''' Multi-node distributed topologies must pass `GLOO_SOCKET_IFNAME=enp1s0f0np0` and `NCCL_SOCKET_IFNAME=enp1s0f0np0`.
* '''NCCL CUDA Memory Driver Disabling (`NCCL_CUMEM_ENABLE=0`):''' Multi-node Grace Blackwell (GB10) deployments must enforce `NCCL_CUMEM_ENABLE=0` across environment manifests to prevent IPC buffer deadlocks on unified memory architectures.
* '''RoCEv2 Link Layer (200Gbps ConnectX-7):'''
'''InfiniBand / HCA Target:''' `rocep1s0f0`
'''RoCE GID Index:''' `NCCL_IB_GID_INDEX=3`

== Grace Blackwell (GB10) Hardware Safeguards ==

=== 1. LPDDR5x Unified Memory Telemetry ===
NVIDIA Grace Blackwell architectures utilize LPDDR5x Unified Memory shared between the Grace CPU and Blackwell GPU. Standard queries return `[N/A]`. The telemetry parser isolates temperature and utilization integers, reporting VRAM memory metrics safely as `Unified / 131072 MB`.

=== 2. Headless Target Mode & DRM Semaphore Lock Prevention ===
DGX compute nodes must never run desktop GUI display managers (`gdm3`, `gnome-shell`).
Convert via `sudo systemctl set-default multi-user.target && sudo systemctl stop gdm3`.

== Installation & Setup Guide ==

=== 1. Clone Repository ===

```bash
mkdir -p ~/docker && cd ~/docker
git clone [https://github.com/imrobertson/orchestrator.git](https://github.com/imrobertson/orchestrator.git)
cd orchestrator

```

=== 2. Configure Essential Secrets (`HF_TOKEN`) ===

```bash
echo 'HF_TOKEN="hf_your_actual_token_here"' > .secrets
chmod 600 .secrets

```

=== 3. Lock Down Master Key Permissions ===

```bash
chmod 600 id_dgx_orchestrator

```

=== 4. Deploy Docker Compose Stack ===

```bash
docker compose up -d --build
sudo ln -sf ~/docker/orchestrator/dgx-config /usr/local/bin/dgx-config

```

== User Onboarding & Key Authorization ==

=== Default Workflow: SSO & Tailscale SSH ===
Users accessing `maestro` via Tailscale SSH require zero local setup. The `dgx-config` wrapper automatically captures the host shell's `$USER` and injects it into the execution container (`-e USER`).

=== Local Network / Admin Users ===
If bypassing Tailscale SSH, authorize your personal SSH key:

```bash
dgx-config authorize-key --key ~/.ssh/id_ed25519.pub

```

== Interface Reference Guide ==

=== Interactive CLI Menu (`dgx-config menu`) ===

* Renders active runtimes, throughput/queue-depth, and GPU telemetry across all Spark nodes.
* Prompts model selection directly from `recipes/local/` and `recipes/eugr/`.

=== Web Dashboard (`http://maestro:5000`) ===

* Dynamic API routing via `window.location.hostname`.
* Displays real-time Docker logs in a full-width bottom panel.
* Deploy, Teardown, and benchmark controls lock each other out while any one of them is in flight — Teardown shows live phase progress (signaling, stopping, removing) for the duration rather than a static "in progress" label.

=== CLI Command Options ===

* '''Check Status:''' `dgx-config status`
* '''Purge Active Runtimes:''' `dgx-config teardown`
* '''Deploy Model:''' `dgx-config deploy --model deepseek-v4-flash-0731-nvfp4 --nodes 2`
* '''Deploy and Block Until Healthy:''' `dgx-config deploy --model deepseek-v4-flash-0731-nvfp4 --nodes 2 --wait`
* '''Preview Deploy (Dry Run):''' `dgx-config deploy --model deepseek-v4-flash-0731-nvfp4 --nodes 2 --dry-run`
* '''View Remote Container Logs:''' `dgx-config logs --host spark-4 --tail 100`
* '''Authorize SSH Key:''' `dgx-config authorize-key --key ~/.ssh/id_ed25519.pub`
* '''Inspect JIT Cache Usage (read-only):''' `dgx-config cache-inventory`
* '''Reclaim JIT Cache Space:''' `dgx-config prune-cache --min-free-gb 50 --headroom-gb 20` (add `--dry-run` to preview without deleting anything — safe against a live cluster)

== Operational Documentation & Incident History ==

For detailed failure mode resolutions, migration specs, and historical release logs, refer to the `docs/` repository:

* **Hardware & Runtime Diagnostics:** `docs/TROUBLESHOOTING.md`
* **Release Tombstones & Fix History:** `docs/TOMBSTONES.md`
* **Catalog Migration & Architecture Plan:** `docs/ARCHITECTURE-MIGRATION-PLAN.md`
* **Upstream Reference Notes:** `docs/EUGR-REFERENCE-NOTES.md`
* **Runtime Robustness Roadmap (v4 -> v5):** `docs/ROADMAP.md`
