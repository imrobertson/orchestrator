= DGX Cluster Control Plane (dgx-cluster-control) — V4.7.0 Containerized Blackwell Architecture Edition =
This repository contains the `dgx-config` orchestration suite. It acts as the central control plane for managing distributed vLLM deployments across a twin-node RoCEv2 NVIDIA Grace Blackwell fabric.

== Target Infrastructure & Control Plane Mapping ==

=== 1. What Runs Where ===
* '''`maestro` (Control Station & Web Dashboard Host):'''
** '''Role:''' Central control node. Runs the Docker container stack for the web dashboard, API endpoints, and the `dgx-config` delegate wrapper.
** '''Web Dashboard (Nginx):''' Runs in the `dgx-dashboard-ui` container on port `5000` (`http://maestro:5000`).
** '''API Orchestration Daemon:''' Runs in the `dgx-orchestrator-api` container on port `5001`.
** '''Containerized Environment:''' The Python application is fully isolated within a `python:3.12-slim` image, bypassing PEP 668 host constraints. 

* '''`spark-4` (Head Compute Node):'''
** '''Management IP:''' `10.0.14.43` (`spark-9dbe`).
** '''Role:''' Primary vLLM head execution target (`vllm-head` / `vllm-standalone`).
** '''Hardware:''' NVIDIA Grace Blackwell GB10 (128GB LPDDR5x Unified Memory running headless).
** '''Docker Boot Policy:''' Docker auto-start is disabled to prevent GPU driver panics or OOM loops on host reboot. Start manually via `systemctl start docker`.

* '''`spark-3` (Worker Compute Node):'''
** '''Management IP:''' `10.0.14.41` (`spark-6e63`).
** '''Role:''' Distributed vLLM worker execution target (`vllm-worker`).
** '''Hardware:''' NVIDIA Grace Blackwell GB10 (128GB LPDDR5x Unified Memory running headless).
** '''Docker Boot Policy:''' Docker auto-start is disabled. Start manually via `systemctl start docker`.

* '''Host Model Cache Storage:''' `/home/tetrel/.cache/huggingface` is mapped directly to `/root/.cache/huggingface` inside vLLM containers via `volume_mount` to guarantee zero re-downloads across cold restarts.

== Deployment & Topology Architecture ==

=== 1. Dynamic Container Image Resolution ===
Image tags are no longer hardcoded into the Python orchestrator. The `models.yaml` configuration declares a global `default_image` (e.g., `"nvcr.io/nvidia/vllm:26.07-py3"`) ensuring cluster-wide updates only require a YAML change. Models requiring specific container branches can override this using a model-level `image` key.

=== 2. Global Multi-Model GPU VRAM Teardown Guard ===
To prevent multiple vLLM models from stacking on the same GPU and causing immediate CUDA Out-Of-Memory (OOM) or port `8000` conflicts:
* '''Pre-Deployment Purge:''' Before spawning any container compose manifest, `execute_deployment` executes a global `docker rm -f vllm-standalone vllm-head vllm-worker` across all target nodes. This completely flushes GPU VRAM prior to container initialization.

=== 3. OpenMP Thread Fencing & CPU Scheduling Protections ===
To prevent PyTorch and vLLM background worker threads from consuming 100% of available Grace ARM CPU cores during multi-node KV cache initialization, multi-node topologies enforce strict CPU thread limits:
* '''Environment Fencing:''' `OMP_NUM_THREADS=16` and `VLLM_CPU_OMP_THREADS=16` are injected into container environment manifests.
* '''Host System Impact:''' Restricts OpenMP thread pools to 16 cores per socket, guaranteeing sufficient CPU scheduling headroom for `sshd`, system daemons, and status polling threads.

== Network Fabric & Transport ==

=== 1. Network Interface Targets & Gloo/NCCL Bindings ===
* '''Management TCP Interface (`enp1s0f0np0`):''' All SSH orchestration, administrative commands, and `dgx-config` calls route strictly across the management subnet (10.0.14.x).
* '''Master Store Rendezvous (10.0.14.43):''' The orchestrator dynamically binds `--master-addr` to the head node's active management IP over `enp1s0f0np0` for Gloo control-plane process registration. This isolates control-plane rendezvous traffic from the high-throughput 200Gbps RoCEv2 data plane.
* '''Gloo & NCCL Interface Binding (`enp1s0f0np0`):''' Multi-node distributed topologies '''must''' pass `GLOO_SOCKET_IFNAME=enp1s0f0np0` and `NCCL_SOCKET_IFNAME=enp1s0f0np0`.
* '''NCCL CUDA Memory Driver Disabling (`NCCL_CUMEM_ENABLE=0`):''' Multi-node Grace Blackwell (GB10) deployments '''must''' enforce `NCCL_CUMEM_ENABLE=0` across environment manifests to prevent IPC buffer deadlocks during distributed rendezvous on unified LPDDR5x memory architectures.
* ⚠️ '''Critical Errata:''' PyTorch Gloo requires a physical Linux network device name. Passing an IP prefix or mismatching `--master-addr` against the declared `GLOO_SOCKET_IFNAME` causes PyTorch distributed backplanes to crash.
* '''RoCEv2 Link Layer (200Gbps ConnectX-7):'''
** '''InfiniBand / HCA Target:''' `rocep1s0f0`
** '''RoCE GID Index:''' `NCCL_IB_GID_INDEX=3`
** '''Inter-Node Port:''' Port `29500` bound across `192.168.99.x` for PyTorch distributed tensor data streams.

=== 2. Wrapper Passthrough Logic & Safeguards (`dgx-config`) ===
The `/usr/local/bin/dgx-config` script acts as a host-aware Docker execution delegate:
* '''Dynamic TTY Allocation:''' Automatically detects pipeline chaining, applying `-it` for interactive menus and `-i` for automated pipelines.
* '''Smart Path Staging:''' Intercepts `--key` flags targeting absolute host paths and auto-stages them into `~/.ssh/` so the container volume mount can safely resolve the credentials.
* '''Host Identity Auditing:''' Injects `-e USER` into the container namespace to accurately capture the active host identity (e.g., `ian`) in remote node `auth.log` files.
* '''Self-Healing SSH Transport:''' Flushes stale `~/.ssh/cm-*` multiplex sockets from within the container filesystem prior to any SSH orchestration routine.

== Grace Blackwell (GB10) Hardware Safeguards ==

=== 1. LPDDR5x Unified Memory Telemetry ===
NVIDIA Grace Blackwell architectures utilize LPDDR5x Unified Memory shared between the Grace CPU and Blackwell GPU. Standard queries like `nvidia-smi --query-gpu=memory.used,memory.total` return `[N/A]`.
* '''Telemetry Line Parsing:''' The cluster control plane scans `nvidia-smi` lines individually. If memory fields return `[N/A]`, the parser isolates temperature and utilization integers, reporting VRAM memory metrics safely as `Unified / 131072 MB`.

=== 2. Headless Target Mode & DRM Semaphore Lock Prevention ===
DGX compute nodes must '''never''' run desktop GUI display managers (`gdm3`, `gnome-shell`).
* '''Headless Conversion:''' `sudo systemctl set-default multi-user.target && sudo systemctl stop gdm3`.

== Installation & Setup Guide ==

Execute these steps on the control station node ('''`maestro`'''):

=== 1. Clone Repository & Assign Permissions ===
<syntaxhighlight lang="bash">
sudo git clone https://github.com/tetrelsec/dgx-cluster-control.git /opt/dgx-cluster-control
sudo chown -R tetrel:wheel /opt/dgx-cluster-control
</syntaxhighlight>

=== 2. Lock Down Master Key Permissions ===
<syntaxhighlight lang="bash">
sudo chown tetrel:wheel /opt/dgx-cluster-control/id_dgx_orchestrator
sudo chmod 640 /opt/dgx-cluster-control/id_dgx_orchestrator
</syntaxhighlight>

=== 3. Deploy Docker Compose Stack ===
<syntaxhighlight lang="bash">
cd /opt/dgx-cluster-control
docker compose up -d --build
</syntaxhighlight>

=== 4. Create Global CLI Symlink ===
<syntaxhighlight lang="bash">
sudo ln -sf /opt/dgx-cluster-control/dgx-config /usr/local/bin/dgx-config
</syntaxhighlight>

== User Onboarding & Key Authorization ==

The orchestrator leverages the Docker control plane to securely proxy commands to the cluster, ensuring all deployment actions are audited natively.

=== Default Workflow: SSO & Tailscale SSH ===
Users accessing `maestro` via Tailscale SSH require '''zero local setup'''. The `dgx-config` wrapper automatically captures your SSO identity and injects it into the execution container (`-e USER`). The container securely utilizes the central service key to orchestrate the cluster, ensuring remote `auth.log` files correctly attribute actions to your SSO user.

=== Local Network / Admin Users ===
If you are bypassing Tailscale SSH and accessing `maestro` locally, you must authorize your personal SSH key so the orchestrator can securely audit your deployments. 

Users in the `wheel` group must elevate once to register their public SSH key across `spark-4` and `spark-3`:
<syntaxhighlight lang="bash">
sudo -u tetrel dgx-config authorize-key --key ~/.ssh/id_ed25519.pub
</syntaxhighlight>

Once authorized, you may execute `dgx-config` natively without `sudo`.

== Interface Reference Guide ==

=== Interactive CLI Menu (`dgx-config menu`) ===
* Renders active cluster runtimes, container states, and GPU telemetry across all Spark nodes.
* Prompts model selection directly from `models.yaml`.
* Automatically restricts topology selection based on model capabilities (1-node vs 2-node).
* Estimates warm restart completion times using `load_times.json`.

=== Web Dashboard (`http://maestro:5000`) ===
* '''Dynamic API Base Routing:''' Uses `window.location.hostname` (`http://${window.location.hostname}:5001/api`) to ensure frontend requests route properly from any client browser.
* '''Full-Width Terminal Trace:''' Displays real-time Docker logs in a full-width bottom panel.
* '''Topology Selector Protection:''' Dynamically filters node topology dropdown options based on model definitions in `models.yaml`.

=== CLI Command Options ===
* '''Check Status:''' `dgx-config status`
* '''Purge Active Runtimes:''' `dgx-config teardown`
* '''Deploy Model:''' `dgx-config deploy --model deepseek-v4-flash-nvfp4 --nodes 2`
* '''View Remote Container Logs:''' `dgx-config logs --host spark-4 --tail 100`

== Core Model Catalog (`models.yaml`) Summary ==

* '''DeepSeek-V4 Flash NVFP4 (`deepseek-v4-flash-nvfp4`):''' Clustered 2-node deployment using `Rarri/DeepSeek-V4-Flash-0731-NVFP4`. Leverages `--moe-backend flashinfer_cutlass` with a container entrypoint patch forcing `FLASHINFER_CUTLASS_MXFP4_MXFP8` FP8 activation scaling, `--disable-custom-all-reduce`, and `--kv-cache-dtype fp8`.
* '''Nemotron 3.5 Lightning (`nemotron-3.5-lightning`):''' Single-node deployment utilizing DSpark speculative decoding.
* '''Muse Glimmer 30B (`muse-glimmer-30b` / `muse-glimmer-30b-nvfp4`):''' Multimodal vision-language model leveraging DFlash speculative configurations.
* '''Qwen Architectures (`qwen-3.8-27b`, `qwen-3.8-27b-nvfp4`, `qwen-3.6-27b-nvfp4`, `qwen-3.5-122b`):''' Qwen 122B MoE locked to 2-node pipeline parallelism with `--enforce-eager`.
* '''Llama & Gemma (`llama-3.3-70b`, `llama-4-fp4`, `llama-4-fp8`, `gemma-4-31b`):''' Llama 70B locked to 2-node pipeline parallelism.

== Release Tombstones & Fix Log ==

=== 39. Docker Control Plane & Delegate Wrapper (V4.7.0) ===
* '''The Trap:''' Host-level PEP 668 constraints and local `venv` drift on `codepolice` made cross-environment management fragile and difficult to upgrade.
* '''The Fix:''' Containerized the full control plane stack inside a `python:3.12-slim` Docker image on `maestro`. Re-engineered the `dgx-config` CLI wrapper into a context-aware Docker delegate that transparently forwards TTY flags, injects host identity, auto-stages external key files, and handles socket cleanup directly inside the container namespace.

=== 38. Grace Blackwell (GB10) MXFP4 MoE Engine & Activation Patch (V4.6.3) ===
* '''The Trap:''' On Grace Blackwell (GB10) GPUs under vLLM `0.21.0`, TRTLLM, DeepGEMM, and Triton MXFP4 MoE kernels fail device compatibility checks. Marlin fails with a `KeyError: 'layers.0.ffn.experts.w13_input_scale'` on raw HuggingFace safetensors. `FlashInferExperts` (`--moe-backend flashinfer_cutlass`) is the only valid GB10 kernel, but defaults to `FLASHINFER_CUTLASS_MXFP4_BF16` (BF16 activations) while DeepSeek-V4 requires FP8 activations (`FLASHINFER_CUTLASS_MXFP4_MXFP8`). Passing `flashinfer_cutlass_afp8` is rejected by vLLM's CLI parser.
* '''The Fix:''' Configured `models.yaml` to use `--moe-backend flashinfer_cutlass` and injected a container entrypoint `sed` patch in `dgx-orchestrator.py` (`sed -i "s/FLASHINFER_CUTLASS_MXFP4_BF16/FLASHINFER_CUTLASS_MXFP4_MXFP8/g" ...`) to force the FP8 activation Cutlass engine at runtime.

=== 37. Multi-Node vLLM V1 Engine `--nnodes` & `--node-rank` Flags (V4.6.3) ===
* '''The Trap:''' Multi-node container launches using vLLM V1 (`nvcr.io/nvidia/vllm:26.05.post1-py3`) omitted `--nnodes` and `--node-rank`. V1's `multiproc_executor` defaulted to single-host execution and attempted to allocate all pipeline-parallel ranks onto `spark-4`'s single physical GPU, triggering a `local_world_size <= visible_device_count` crash.
* '''The Fix:''' Updated `execute_deployment()` in `dgx-orchestrator.py` to inject `--nnodes <nodes>` and `--node-rank 0/1` explicitly into `docker run` commands.

=== 36. HuggingFace Auth Token Discovery (`get_hf_token`) (V4.6.3) ===
* '''The Trap:''' Unauthenticated HuggingFace Hub requests triggered rate-limiting warnings or failed private checkpoint downloads.
* '''The Fix:''' Implemented `get_hf_token()` in `dgx-orchestrator.py` to search environment variables, `/opt/dgx-cluster-control/.secrets`, or `~/.cache/huggingface/token`, injecting `-e HF_TOKEN=<token>` directly into vLLM containers.

=== 35. SSH Multi-Token Argument Quoting via `shlex.quote` (V4.6.3) ===
* '''The Trap:''' Passing complex CLI arguments or JSON configuration strings (`--attention-config '{"use_fp4_indexer_cache": true}'`) through `run_ssh()` allowed remote shell layers to strip inner quotes or mangle JSON formatting.
* '''The Fix:''' Updated `run_ssh()` to process command lists using `shlex.quote` on each token, ensuring preserved remote evaluation across OpenSSH shell boundaries.

=== 34. Web Dashboard Dynamic Hostname Routing (V4.6.2) ===
* '''The Trap:''' Hardcoding `10.0.14.43` or `localhost` as `API_BASE` in `index.html` caused cross-origin requests or connection failures when opening the dashboard from external workstations.
* '''The Fix:''' Updated `index.html` to evaluate `window.location.hostname` dynamically (`http://${window.location.hostname}:5001/api`).

=== 33. Grace Blackwell (GB10) Unified Memory Telemetry Parser (V4.6.2) ===
* '''The Trap:''' Grace Blackwell LPDDR5x Unified Memory returns `[N/A]` for standard `nvidia-smi` memory queries, causing strict integer parsing checks to reject telemetry lines and return empty metrics.
* '''The Fix:''' Updated `get_lightweight_telemetry()` in `dgx-orchestrator.py` to parse temperature and GPU utilization independently while assigning `Unified / 131072 MB` for VRAM fields.

=== 32. SSH Remote Shell Pipeline Syntax Expansion Bug (V4.6.2) ===
* '''The Trap:''' Executing `docker ps --format '{{.Names}}|{{.Image}}'` over SSH caused the remote Bash shell to interpret `|` as a shell pipe, throwing `Exit 2` or `Exit 127` errors.
* '''The Fix:''' Replaced `|` delimiters with double colons (`::`) across remote Docker format strings.

=== 31. Wrapper Subcommand Passthrough for Daemon Execution (V4.6.1) ===
* '''The Trap:''' Running `dgx-config daemon` caused the wrapper to pass `cli daemon` to `dgx-orchestrator.py`, triggering `argparse` choice validation errors.
* '''The Fix:''' Updated `dgx-orchestrator.py` to map `args.subcommand == "cli"` and `args.cli_action == "daemon"` directly into daemon execution mode.

=== 30. Virtual Environment Bootstrap & Ownership (`tetrel:wheel`) (V4.6.1) ===
* '''The Trap:''' Executing `ensurepip` or `pip install` as non-root user `ian` on `/opt/dgx-cluster-control/dgx-env` failed with `[Errno 13] Permission denied`.
* '''The Fix:''' Standardized virtual environment management commands to run via `sudo -u tetrel`.

=== 29. Web Dashboard Full-Width Logs & Dynamic Topology Selector (V4.6.0) ===
* '''The Trap:''' Squeezed log panels obscured long trace outputs, and invalid topology options allowed users to attempt single-node deployments of 70B+ models.
* '''The Fix:''' Moved live logs to a full-width bottom panel and added dynamic catalog parsing in JavaScript to filter valid topology choices.

=== 28. YAML Argument Comment Pollution & Syntax Sanitization (V4.6.0) ===
* '''The Trap:''' Inline bash comments inside folded YAML multiline strings (`>-`) were parsed as literal command-line flags, causing vLLM initialization to fail.
* '''The Fix:''' Stripped all inline comments and trailing formatting artifacts from `models.yaml`.

=== 27. Multi-User Shared Key Auto-Staging & OpenSSH 0600 Strictness (V4.5.0) ===
* '''The Trap:''' Group-readable permissions (`0640`) on shared SSH keys triggered OpenSSH `bad permissions` rejections.
* '''The Fix:''' Implemented `resolve_user_identity_key()` in `dgx-orchestrator.py` to auto-stage key copies into `~/.ssh/id_dgx_orchestrator` with `0600` permissions.
