= DGX Cluster Control Plane (`orchestrator`) — Recipe-Based Blackwell Architecture Edition =
This repository contains the `dgx-config` orchestration suite. It acts as the central control plane for managing distributed vLLM deployments across a twin-node RoCEv2 NVIDIA Grace Blackwell fabric.

''This document was substantially rewritten to match the current codebase rather than lightly edited — several claims in the previous version (the clone path, the model catalog format, one networking detail) were either stale or, on closer inspection against the actual code, weren't quite accurate to begin with. Corrections are called out explicitly below rather than silently folded in, so anyone who remembers the old version isn't left guessing what changed and why.''

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
* '''Host Model Cache Storage:''' `/home/tetrel/.cache/huggingface` is mapped directly to `/root/.cache/huggingface` inside vLLM containers via `volume_mount` to guarantee zero re-downloads across cold restarts. Also used to derive the Triton/CUDA JIT-compile cache mount paths (siblings of the HF cache dir) — see "JIT-Compile Caching" below.
* '''All host/network/tuning configuration now lives in `cluster_config.yaml`,''' not scattered across the Python files — see "Configuration" below. `models.yaml` still exists but is deprecated; see "Model Catalog" below for why.

== Configuration ==

`cluster_config.yaml`, at the repo root, is the single source of truth for:

* '''`hosts:`''' — the host inventory (management IP, backplane IP, volume mount, `active` flag). Loaded via `common/config.py`, which every script imports rather than keeping its own copy — this used to be duplicated across `dgx-orchestrator.py`, `cache_cluster_assets.py`, and `benchmark.py` independently; that duplication is gone.
* '''`ports:`''' — `vllm_api`, `orchestrator_api`, `ray`, `master`. These are actually read by the deploy path now (not always true historically — see Release Tombstone #41 below).
* '''`tuning:`''' — deploy-time knobs that used to be hardcoded literals inside `dgx-orchestrator.py`: `shm_size_1node` / `shm_size_2node`, `gpu_clock_lock`, `deploy_wait_timeout_sec` / `deploy_poll_interval_sec`, `jit_cache_maxsize_bytes`. All have defaults matching the old hardcoded values, so an older `cluster_config.yaml` missing this whole section still works unchanged.

== Model Catalog ==

'''The catalog moved from a single `models.yaml` file to `recipes/local/*.yaml` and `recipes/eugr/*.yaml` — one file per model.''' `models.yaml` is kept only as an explicit rollback path (`USE_LEGACY_CATALOG=1` environment variable falls back to the original parsing, unchanged, with no code edit or redeploy needed) and should be treated as deprecated, not as the live source of truth. See `docs/ARCHITECTURE-MIGRATION-PLAN.md`'s Phase 2 for the full migration writeup.

A recipe's catalog key — the name you pass to `--model` — is its filename, and only its filename. Earlier recipe files also carried a `name:` field inside the YAML that was required to match the filename; that redundancy caused a real incident (a drifted `name:` field took the entire catalog empty, not just one model) and has since been removed entirely from the schema. If you're hand-authoring or editing a recipe file, the filename is the only place its identity lives.

=== Recipe schema, in brief ===

```yaml
recipe_version: '1'
hf_path: org/Model-Name          # HuggingFace model id
image: nvcr.io/nvidia/vllm:...   # optional; omit to use cluster_config.yaml's default_image
gpu_util: 0.70
capability:                       # schema exists now, not populated yet (planned: Phase 4)
  task: null
  context_class: null
  latency_class: null
mods: []                          # planned execution mechanism, see Roadmap below
topologies:
  1_node:
    max_model_len: 32768
    tp_size: 1
    pp_size: 1
    env_vars: [OMP_NUM_THREADS=16, VLLM_CPU_OMP_THREADS=16]
    vllm_args: >-
      --trust-remote-code --kv-cache-dtype fp8
  2_node:
    max_model_len: 131072
    tp_size: 1
    pp_size: 2
    env_vars: [...]
    vllm_args: >-
      --disable-custom-all-reduce --trust-remote-code --kv-cache-dtype fp8
```

Drop a new file in `recipes/local/` to add a model — no code change, no shared-file merge conflict with anyone else's in-flight edits, and it shows up in the dashboard dropdown / `dgx-config status` on the next catalog load. A recipe missing a required topology, or otherwise failing validation, will currently prevent the *entire* catalog from loading, not just that one recipe — see "Known limitations" below; check `dgx-config status` after adding one before assuming it worked.

=== Borrowing from eugr/spark-vllm-docker ===

Some recipes and hardening ideas are adapted from the community `eugr/spark-vllm-docker` project (see `docs/EUGR-REFERENCE-NOTES.md` for the full review). Their recipe format is structurally different from ours (a flat `defaults:` dict plus a `command:` string template, vs. our explicit per-topology fields) — deliberately not adopted wholesale; the reasoning is recorded in `docs/EUGR-REFERENCE-NOTES.md`'s 2026-08-20 update rather than repeated here. `tools/translate_eugr_recipes.py` mechanically converts what's translatable and refuses (rather than guesses) on the handful of things that genuinely need a human — most commonly an unmapped container image, or a value like `max_model_len: auto` that has no derivable numeric equivalent. Translated output lands in `recipes/_translated_from_eugr/` for review, never directly in `recipes/local/` or `recipes/eugr/`.

== Deployment & Topology Architecture ==

=== 1. Dynamic Container Image Resolution ===
Image tags are not hardcoded into the Python orchestrator. `cluster_config.yaml` declares a global `default_image` (e.g., `"nvcr.io/nvidia/vllm:26.07-py3"`) ensuring cluster-wide updates only require a YAML change. Individual recipes can override this with a model-level `image:` key.

=== 2. Global Multi-Model GPU VRAM Teardown Guard ===
To prevent multiple vLLM models from stacking on the same GPU and causing immediate CUDA Out-Of-Memory (OOM) or port `8000` conflicts:

* '''Pre-Deployment Purge:''' Before spawning any container, `execute_deployment` executes a global `docker rm -f vllm-standalone vllm-head vllm-worker` across all target nodes. This completely flushes GPU VRAM prior to container initialization.

=== 3. OpenMP Thread Fencing & CPU Scheduling Protections ===
To prevent PyTorch and vLLM background worker threads from consuming 100% of available Grace ARM CPU cores during multi-node KV cache initialization, multi-node topologies enforce strict CPU thread limits:

* '''Environment Fencing:''' `OMP_NUM_THREADS=16` and `VLLM_CPU_OMP_THREADS=16` are set per-recipe in `env_vars:`.
* '''Host System Impact:''' Restricts OpenMP thread pools to 16 cores per socket, guaranteeing sufficient CPU scheduling headroom for `sshd`, system daemons, and status polling threads.

=== 4. JIT-Compile Caching ===
Every deploy mounts a persistent Triton/CUDA JIT-compile cache (`TRITON_CACHE_DIR`, `CUDA_CACHE_PATH`) derived from the same host directory as the HuggingFace cache mount, sized via `cluster_config.yaml`'s `tuning.jit_cache_maxsize_bytes`. Without this, a container that has to JIT-compile kernels on first request — the dashboard reports this as `NOT READY - COMPILING KERNELS` — pays that cost from cold on every fresh container instead of once.

== Network Fabric & Transport ==

=== 1. Network Interface Targets & Gloo/NCCL Bindings ===

* '''Management TCP Interface (`enp1s0f0np0`):''' All SSH orchestration, administrative commands, and `dgx-config` calls route strictly across the management subnet (10.0.14.x).
* '''Master Store Rendezvous:''' The orchestrator binds `--master-addr` to the head node's '''management''' IP (`10.0.14.43` for `spark-4`), read from `cluster_config.yaml`'s `hosts.<name>.management_ip` — confirmed directly against `_execute_deployment_impl()`. `--master-port` comes from `cluster_config.yaml`'s `ports.master` (`29500` by default).
* '''Note on the backplane IP:''' `cluster_config.yaml` also carries a `backplane_ip` (`192.168.99.x`) per host, but as of this writing it is not read anywhere in the deploy path — `common/config.py`'s `legacy_hosts_dict()` (what `dgx-orchestrator.py` actually uses) only carries `management_ip` through. In practice this means the master-store rendezvous (the small control-plane handshake) travels over the '''management''' network, not the RoCE backplane — an earlier version of this document claimed the opposite for the rendezvous port specifically. The heavy tensor/all-reduce traffic itself is still steered onto the RoCE fabric, but by `NCCL_SOCKET_IFNAME` / `NCCL_IB_HCA` / `NCCL_IB_GID_INDEX` env vars, independently of `--master-addr`/`--master-port` — not by an explicit `backplane_ip`-based bind. Worth resolving one way or the other (either wire `backplane_ip` into the rendezvous bind for real, or remove the unused field) rather than leaving the config carrying a value nothing reads.
* '''Gloo & NCCL Interface Binding (`enp1s0f0np0`):''' Multi-node distributed topologies '''must''' pass `GLOO_SOCKET_IFNAME=enp1s0f0np0` and `NCCL_SOCKET_IFNAME=enp1s0f0np0`.
* '''NCCL CUDA Memory Driver Disabling (`NCCL_CUMEM_ENABLE=0`):''' Multi-node Grace Blackwell (GB10) deployments '''must''' enforce `NCCL_CUMEM_ENABLE=0` across environment manifests to prevent IPC buffer deadlocks during distributed rendezvous on unified LPDDR5x memory architectures.
* ⚠️ '''Critical Errata:''' PyTorch Gloo requires a physical Linux network device name. Passing an IP prefix or mismatching `--master-addr` against the declared `GLOO_SOCKET_IFNAME` causes PyTorch distributed backplanes to crash.
* '''RoCEv2 Link Layer (200Gbps ConnectX-7):'''
 '''InfiniBand / HCA Target:''' `rocep1s0f0`
 '''RoCE GID Index:''' `NCCL_IB_GID_INDEX=3`

=== 2. Wrapper Passthrough Logic & Safeguards (`dgx-config`) ===
The `/usr/local/bin/dgx-config` script acts as a host-aware Docker execution delegate:

* '''Dynamic TTY Allocation:''' Automatically detects pipeline chaining, applying `-it` for interactive menus and `-i` for automated pipelines.
* '''Smart Path Staging:''' Intercepts `--key` flags targeting absolute host paths and auto-stages them into `~/.ssh/` so the container volume mount can safely resolve the credentials.
* '''Host Identity Auditing:''' Injects `-e USER` into the container namespace to accurately capture the active host identity (e.g., `ian`) in remote node `auth.log` files. Note this is self-reported (whatever the shell's `$USER` is), not cryptographically verified — see the User Manual's Troubleshooting section.
* '''Self-Healing SSH Transport:''' Flushes stale `~/.ssh/cm-*` and `/tmp/cm-*` multiplex sockets from within the container filesystem prior to any SSH orchestration routine.

== Grace Blackwell (GB10) Hardware Safeguards ==

=== 1. LPDDR5x Unified Memory Telemetry ===
NVIDIA Grace Blackwell architectures utilize LPDDR5x Unified Memory shared between the Grace CPU and Blackwell GPU. Standard queries like `nvidia-smi --query-gpu=memory.used,memory.total` return `[N/A]`.

* '''Telemetry Line Parsing:''' The cluster control plane scans `nvidia-smi` lines individually. If memory fields return `[N/A]`, the parser isolates temperature and utilization integers, reporting VRAM memory metrics safely as `Unified / 131072 MB`.

=== 2. Headless Target Mode & DRM Semaphore Lock Prevention ===
DGX compute nodes must '''never''' run desktop GUI display managers (`gdm3`, `gnome-shell`).

* '''Headless Conversion:''' `sudo systemctl set-default multi-user.target && sudo systemctl stop gdm3`.

== Installation & Setup Guide ==

'''Corrected from an earlier version of this document:''' the clone path and repo URL below previously referenced `tetrelsec/dgx-cluster-control` cloned to `/opt/dgx-cluster-control` — that doesn't match either the actual GitHub repo (`imrobertson/orchestrator`) or the path the `dgx-config` wrapper script itself expects (`~/docker/orchestrator` — see its own error message: `cd ~/docker/orchestrator && docker compose up -d`). Corrected below to match what the code actually expects, not what an earlier doc assumed.

Execute these steps on the control station node ('''`maestro`'''):

=== 1. Clone Repository ===

mkdir -p ~/docker && cd ~/docker
git clone https://github.com/imrobertson/orchestrator.git
cd orchestrator


=== 2. Configure Essential Secrets (`HF_TOKEN`) ===
The orchestrator requires a HuggingFace authentication token to pull gated models (like Llama) and tokenizer configurations. Create a `.secrets` file in the project root — see `.secrets.example` for the expected format:

echo 'HF_TOKEN="hf_your_actual_token_here"' > .secrets
chmod 600 .secrets


''Note: `get_hf_token()` in `dgx-orchestrator.py` automatically parses this file (or falls back to the `HF_TOKEN` environment variable, or `~/.cache/huggingface/token`) and securely injects it into deployed containers at runtime.''

=== 3. Lock Down Master Key Permissions ===

chmod 600 id_dgx_orchestrator


=== 4. Deploy Docker Compose Stack ===

docker compose up -d --build


=== 5. Create Global CLI Symlink ===

sudo ln -sf ~/docker/orchestrator/dgx-config /usr/local/bin/dgx-config


== User Onboarding & Key Authorization ==

The orchestrator leverages the Docker control plane to securely proxy commands to the cluster.

=== Default Workflow: SSO & Tailscale SSH ===
Users accessing `maestro` via Tailscale SSH require '''zero local setup'''. The `dgx-config` wrapper automatically captures the host shell's `$USER` and injects it into the execution container (`-e USER`), and the container uses a central service key to actually reach `spark-3`/`spark-4`. Worth being precise about what this does and doesn't guarantee: it means the '''SSH hop to `maestro` itself''' is Tailscale-verified, but the `USER` value forwarded into the container — and separately, the `user_id` field the dashboard and CLI both collect per-deploy — are self-reported strings, not independently authenticated. See the User Manual's Troubleshooting section for what this means in practice.

=== Local Network / Admin Users ===
If you are bypassing Tailscale SSH and accessing `maestro` locally, you must authorize your personal SSH key so the orchestrator can reach the Spark hosts on your behalf:

dgx-config authorize-key --key ~/.ssh/id_ed25519.pub


This appends your public key to `~/.ssh/authorized_keys` on both `spark-3` and `spark-4`.

== Interface Reference Guide ==

=== Interactive CLI Menu (`dgx-config menu`) ===

* Renders active cluster runtimes, container states, live throughput/queue-depth, and GPU telemetry across all Spark nodes.
* Prompts model selection directly from the live recipe catalog (`recipes/local/` + `recipes/eugr/`).
* Automatically restricts topology selection to what the selected recipe actually defines (1-node vs 2-node).
* Estimates warm restart completion times using `load_times.json`, which records real observed load durations per model+topology over time.

=== Web Dashboard (`http://maestro:5000`) ===

* '''Dynamic API Base Routing:''' Uses `window.location.hostname` to ensure frontend requests route properly from any client browser, not just `localhost`.
* '''Full-Width Terminal Trace:''' Displays real-time Docker logs in a full-width bottom panel, with health-check and metrics-polling noise filtered out.
* '''Topology Selector Protection:''' Dynamically filters the node topology dropdown to what the selected recipe actually supports.
* '''Live throughput/concurrency:''' header bar shows tokens/sec and active/queued request counts, scraped from vLLM's own `/metrics` Prometheus endpoint when a model is serving.

=== CLI Command Options ===

* '''Check Status:''' `dgx-config status`
* '''Purge Active Runtimes:''' `dgx-config teardown`
* '''Deploy Model:''' `dgx-config deploy --model deepseek-v4-flash-nvfp4 --nodes 2`
* '''Deploy and Block Until Healthy:''' `dgx-config deploy --model qwen-2.5-coder-32b --nodes 1 --wait`
* '''Preview a Deploy Without Touching Any Host:''' `dgx-config deploy --model qwen-2.5-coder-32b --nodes 2 --dry-run` — prints the exact `docker run` command(s) that would be sent; makes zero SSH connections.
* '''View Remote Container Logs:''' `dgx-config logs --host spark-4 --tail 100`
* '''Authorize a New SSH Key on Both Hosts:''' `dgx-config authorize-key --key ~/.ssh/id_ed25519.pub`

=== Air-Gapped / Offline Operations ===

* '''Pre-Cache Assets:''' `cache_cluster_assets.py` reads the live catalog and parallel-downloads Docker images and HuggingFace safetensors to both DGX nodes. Run this on `maestro` prior to toggling offline mode.
* '''Offline Mode Toggle:''' Within the Web Dashboard, the network mode indicator toggles the cluster into offline mode, injecting `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1` into all new container deployments.

=== Performance Benchmarking ===

* '''Streaming Benchmark (`benchmark.py`):''' Times Time-To-First-Token (TTFT) and Decode tokens/second.
* '''MTP Protection:''' Specifically hardened against vLLM Multi-Token Prediction (MTP) stream buffering to prevent division-by-zero errors.
* '''Ledger Tracking:''' Results, including exact token counts and engine configurations, are appended to `benchmark_ledger.csv`.

== Known limitations ==

Being upfront about the current rough edges rather than only documenting what works:

* '''One bad recipe fails the whole catalog.''' `build_catalog_response()` currently fails closed on any single recipe validation error — the entire model list goes empty, not just the broken recipe. This already caused one real production incident (see Release Tombstone #41). Containing this to per-recipe skip-and-warn is a tracked backlog item, not yet fixed.
* '''`user_id` / auditor fields are self-reported, not authenticated.''' See "User Onboarding" above.
* '''`backplane_ip` in `cluster_config.yaml` is currently unused.''' See the Network Fabric section above.
* '''`mods:` in the recipe schema is parsed but not yet executed.''' Planned — see `docs/ARCHITECTURE-MIGRATION-PLAN.md`'s Phase 2 for the concrete scope (adapting `eugr/spark-vllm-docker`'s `mods/<name>/run.sh` pattern via `docker exec`, applied after the container reaches `RUNNING` and before the health-check poll begins).

For the fuller roadmap (N-node generalization, the capability-based allocator, API auth), see `docs/ARCHITECTURE-MIGRATION-PLAN.md`.

== Release Tombstones & Fix Log ==

=== 41. Recipe Catalog Empty Due to Filename/`name:` Field Drift (V4.8.0) ===

* '''The Trap:''' The original recipe schema carried both a filename and an internal `name:` field, required to match. During a merge, two recipes' `name:` fields drifted out of sync with their filenames. Because `build_catalog_response()` fails closed on any single bad recipe, the '''entire''' model catalog silently went empty — not just the two broken files — with no error surfaced anywhere the dashboard user could see.
* '''The Fix:''' Removed `name:` from the schema entirely. The filename is now the only identifier a recipe has, so there's structurally nothing left for it to disagree with. The whole-catalog-fails-on-one-bad-recipe behavior itself is unchanged and is tracked separately — see "Known limitations" above.

=== 40. Dashboard Polling Hang & Duplicate Load-Time Recording (V4.8.0) ===

* '''The Trap:''' Two separate bugs, both invisible under light use. (1) `get_cluster_status()` made several sequential SSH round trips per host with no overall deadline; under frequent dashboard polling, a single unreachable host could cause requests to back up faster than they drained, presenting as a fully hung dashboard. (2) `record_load_time()` was called on every single status poll while a container sat idle-but-ready, not just once at actual readiness — `load_times.json` entries grew without bound instead of capturing one real cold-start duration, silently corrupting the ETA estimator's historical data.
* '''The Fix:''' `get_cluster_status()` now single-flights concurrent callers and enforces a hard wall-clock ceiling (`STATUS_CALL_TIMEOUT_SEC`) via bounded per-host futures instead of unbounded `as_completed()`. `record_load_time()` now tracks which container instance has already been recorded and fires at most once per instance.

=== 39. Docker Control Plane & Delegate Wrapper (V4.7.0) ===

* '''The Trap:''' Host-level PEP 668 constraints and local `venv` drift on `codepolice` made cross-environment management fragile and difficult to upgrade.
* '''The Fix:''' Containerized the full control plane stack inside a `python:3.12-slim` Docker image on `maestro`. Re-engineered the `dgx-config` CLI wrapper into a context-aware Docker delegate that transparently forwards TTY flags, injects host identity, auto-stages external key files, and handles socket cleanup directly inside the container namespace.

=== 38. Grace Blackwell (GB10) MXFP4 MoE Engine & Activation Patch (V4.6.3) ===

* '''The Trap:''' On Grace Blackwell (GB10) GPUs under vLLM `0.21.0`, TRTLLM, DeepGEMM, and Triton MXFP4 MoE kernels fail device compatibility checks. Marlin fails with a `KeyError: 'layers.0.ffn.experts.w13_input_scale'` on raw HuggingFace safetensors. `FlashInferExperts` (`--moe-backend flashinfer_cutlass`) is the only valid GB10 kernel, but defaults to `FLASHINFER_CUTLASS_MXFP4_BF16` (BF16 activations) while DeepSeek-V4 requires FP8 activations (`FLASHINFER_CUTLASS_MXFP4_MXFP8`). Passing `flashinfer_cutlass_afp8` is rejected by vLLM's CLI parser.
* '''The Fix:''' Configured the recipe to use `--moe-backend flashinfer_cutlass` and injected a container entrypoint `sed` patch (`sed -i "s/FLASHINFER_CUTLASS_MXFP4_BF16/FLASHINFER_CUTLASS_MXFP4_MXFP8/g" ...`) to force the FP8 activation Cutlass engine at runtime.

=== 37. Multi-Node vLLM V1 Engine `--nnodes` & `--node-rank` Flags (V4.6.3) ===

* '''The Trap:''' Multi-node container launches using vLLM V1 (`nvcr.io/nvidia/vllm:26.05.post1-py3`) omitted `--nnodes` and `--node-rank`. V1's `multiproc_executor` defaulted to single-host execution and attempted to allocate all pipeline-parallel ranks onto `spark-4`'s single physical GPU, triggering a `local_world_size <= visible_device_count` crash.
* '''The Fix:''' Updated `execute_deployment()` in `dgx-orchestrator.py` to inject `--nnodes <nodes>` and `--node-rank 0/1` explicitly into `docker run` commands.

=== 36. HuggingFace Auth Token Discovery (`get_hf_token`) (V4.6.3) ===

* '''The Trap:''' Unauthenticated HuggingFace Hub requests triggered rate-limiting warnings or failed private checkpoint downloads.
* '''The Fix:''' Implemented `get_hf_token()` in `common/ssh.py` to search environment variables, the project's `.secrets` file, or `~/.cache/huggingface/token`, injecting `-e HF_TOKEN=<token>` directly into vLLM containers.

=== 35. SSH Multi-Token Argument Quoting via `shlex.quote` (V4.6.3) ===

* '''The Trap:''' Passing complex CLI arguments or JSON configuration strings (`--attention-config '{"use_fp4_indexer_cache": true}'`) through `run_ssh()` allowed remote shell layers to strip inner quotes or mangle JSON formatting.
* '''The Fix:''' Updated `run_ssh()` to process command lists using `shlex.quote` on each token, ensuring preserved remote evaluation across OpenSSH shell boundaries.

=== 34. Web Dashboard Dynamic Hostname Routing (V4.6.2) ===

* '''The Trap:''' Hardcoding `10.0.14.43` or `localhost` as `API_BASE` in `index.html` caused cross-origin requests or connection failures when opening the dashboard from external workstations.
* '''The Fix:''' Updated `index.html` to evaluate `window.location.hostname` dynamically.

=== 33. Grace Blackwell (GB10) Unified Memory Telemetry Parser (V4.6.2) ===

* '''The Trap:''' Grace Blackwell LPDDR5x Unified Memory returns `[N/A]` for standard `nvidia-smi` memory queries, causing strict integer parsing checks to reject telemetry lines and return empty metrics.
* '''The Fix:''' Updated `get_lightweight_telemetry()` in `dgx-orchestrator.py` to parse temperature and GPU utilization independently while assigning `Unified / 131072 MB` for VRAM fields.

=== 32. SSH Remote Shell Pipeline Syntax Expansion Bug (V4.6.2) ===

* '''The Trap:''' Executing `docker ps --format '{{.Names}}|{{.Image}}'` over SSH caused the remote Bash shell to interpret `|` as a shell pipe, throwing `Exit 2` or `Exit 127` errors.
* '''The Fix:''' Replaced `|` delimiters with double colons (`::`) across remote Docker format strings.

=== 31. Wrapper Subcommand Passthrough for Daemon Execution (V4.6.1) ===

* '''The Trap:''' Running `dgx-config daemon` caused the wrapper to pass `cli daemon` to `dgx-orchestrator.py`, triggering `argparse` choice validation errors.
* '''The Fix:''' Updated `dgx-orchestrator.py` to map `args.subcommand == "cli"` and `args.cli_action == "daemon"` directly into daemon execution mode.

=== 30. Virtual Environment Bootstrap & Ownership (V4.6.1) ===

* '''The Trap:''' Running `pip install` as a non-service user on the host failed with permission errors.
* '''The Fix:''' Standardized on the containerized stack (see Tombstone #39) rather than a host-level `venv`, sidestepping this class of problem entirely.

=== 29. Web Dashboard Full-Width Logs & Dynamic Topology Selector (V4.6.0) ===

* '''The Trap:''' Squeezed log panels obscured long trace outputs, and invalid topology options allowed users to attempt single-node deployments of 70B+ models.
* '''The Fix:''' Moved live logs to a full-width bottom panel and added dynamic catalog parsing in JavaScript to filter valid topology choices.

=== 28. YAML Argument Comment Pollution & Syntax Sanitization (V4.6.0) ===

* '''The Trap:''' Inline bash comments inside folded YAML multiline strings (`>-`) were parsed as literal command-line flags, causing vLLM initialization to fail.
* '''The Fix:''' Stripped all inline comments and trailing formatting artifacts from the catalog source.

=== 27. Multi-User Shared Key Auto-Staging & OpenSSH 0600 Strictness (V4.5.0) ===

* '''The Trap:''' Group-readable permissions (`0640`) on shared SSH keys triggered OpenSSH `bad permissions` rejections.
* '''The Fix:''' Implemented `resolve_user_identity_key()` to auto-stage key copies into `~/.ssh/id_dgx_orchestrator` with `0600` permissions.

''(Earlier tombstones, #1–26, predate this rewrite and aren't reproduced here — check version history if you need them.)''
