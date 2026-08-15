
# DGX Cluster Control Plane (dgx-cluster-control) — V4.6.3 Control-Plane & Blackwell Architecture Edition[cite: 1]



This repository contains the `dgx-config` orchestration suite[cite: 1]. It acts as the central control plane for managing distributed vLLM deployments across a twin-node RoCEv2 NVIDIA Grace Blackwell fabric[cite: 1].

---

## Target Infrastructure & Control Plane Mapping[cite: 1]

### 1. What Runs Where[cite: 1]

* **`codepolice` (Control Station & Web Dashboard Host):**[cite: 1]
* **Role:** Central control node[cite: 1]. Runs the administrative daemons, web dashboard, and API endpoints[cite: 1].


* **Web Dashboard (Nginx):** Runs in Docker on port `5000` (`http://codepolice:5000`)[cite: 1].


* **API Orchestration Daemon:** Runs `dgx-orchestrator.py daemon` on port `5001`[cite: 1].


* **Python Environment:** Hosts the `/opt/dgx-cluster-control/dgx-env` virtual environment and master shared identity key `/opt/dgx-cluster-control/id_dgx_orchestrator`[cite: 1].




* **`spark-4` (Head Compute Node):**[cite: 1]
* **Management IP:** `10.0.14.43` (`spark-9dbe`)[cite: 1].


* **Role:** Primary vLLM head execution target (`vllm-head` / `vllm-standalone`)[cite: 1].


* **Hardware:** NVIDIA Grace Blackwell GB10 (128GB LPDDR5x Unified Memory running headless)[cite: 1].




* **`spark-3` (Worker Compute Node):**[cite: 1]
* **Management IP:** `10.0.14.41` (`spark-6e63`)[cite: 1].


* **Role:** Distributed vLLM worker execution target (`vllm-worker`)[cite: 1].


* **Hardware:** NVIDIA Grace Blackwell GB10 (128GB LPDDR5x Unified Memory running headless)[cite: 1].




* **Host Model Cache Storage:** `/home/tetrel/.cache/huggingface` mapped directly to `/root/.cache/huggingface` inside vLLM containers via `volume_mount` to guarantee zero re-downloads across cold restarts[cite: 1].



---

## Low-Level Network Fabric & Hardware Safeguards[cite: 1]

### 1. Network Interface Targets & Gloo/NCCL Bindings[cite: 1]

* **Management TCP Interface (`enp1s0f0np0`):** All SSH orchestration, administrative commands, and `dgx-config` calls route strictly across the management subnet (10.0.14.x)[cite: 1].


* **Master Store Rendezvous (10.0.14.43):** `dgx-orchestrator.py` dynamically binds `--master-addr` to the head node's active management IP (10.0.14.43) over `enp1s0f0np0` for Gloo control-plane process registration[cite: 1]. This isolates control-plane rendezvous traffic from the high-throughput 200Gbps RoCEv2 data plane[cite: 1].


* **Gloo & NCCL Interface Binding (`enp1s0f0np0`):** Multi-node distributed topologies **must** pass `GLOO_SOCKET_IFNAME=enp1s0f0np0` and `NCCL_SOCKET_IFNAME=enp1s0f0np0`[cite: 1].


* **NCCL CUDA Memory Driver Disabling (`NCCL_CUMEM_ENABLE=0`):** Multi-node Grace Blackwell (GB10) deployments **must** enforce `NCCL_CUMEM_ENABLE=0` across environment manifests to prevent IPC buffer deadlocks during distributed rendezvous on unified LPDDR5x memory architectures[cite: 1].


* ⚠️ **Critical Errata:** PyTorch Gloo requires a physical Linux network device name[cite: 1]. Passing an IP prefix or mismatching `--master-addr` against the declared `GLOO_SOCKET_IFNAME` causes PyTorch distributed backplanes to crash[cite: 1].


* **RoCEv2 Link Layer (200Gbps ConnectX-7):**[cite: 1]
* **InfiniBand / HCA Target:** `rocep1s0f0`[cite: 1]

* **RoCE GID Index:** `NCCL_IB_GID_INDEX=3`[cite: 1]

* **Inter-Node Port:** Port `29500` bound across `192.168.99.x` for PyTorch distributed tensor data streams[cite: 1].





### 2. Grace Blackwell (GB10) LPDDR5x Unified Memory Telemetry[cite: 1]

NVIDIA Grace Blackwell architectures utilize LPDDR5x Unified Memory shared between the Grace CPU and Blackwell GPU[cite: 1]. As a result, standard queries like `nvidia-smi --query-gpu=memory.used,memory.total` return `[N/A]`[cite: 1].

* **Telemetry Line Parsing:** `get_lightweight_telemetry()` scans `nvidia-smi` lines individually[cite: 1]. If memory fields return `[N/A]`, the parser isolates temperature and utilization integers, reporting VRAM memory metrics safely as `Unified / 131072 MB` to prevent `ValueError` or `ModuleNotFoundError` crashes in control plane reporting[cite: 1].



### 3. SSH Remote Command Formatting & Shell Pipeline Avoidance[cite: 1]

Passing pipe characters (`|`) inside remote SSH formatting arguments (e.g., `ssh user@node "docker ps --format '{{.Names}}|{{.Image}}'"`) causes the remote Linux shell to evaluate the pipe as a local binary command rather than a formatting string, resulting in `Exit 2` or `Exit 127` errors[cite: 1].

* **Safe Delimiters:** `dgx-orchestrator.py` enforces double-colon delimiters (`::`) in Docker format strings (`'{{.Names}}::{{.Image}}'`), bypassing remote shell pipeline evaluation[cite: 1].



### 4. Wrapper Passthrough Logic (`dgx-config` vs `dgx-orchestrator.py`)[cite: 1]

The `/usr/local/bin/dgx-config` wrapper script hardcodes the `cli` argument when calling `dgx-orchestrator.py`[cite: 1]. To allow `dgx-config daemon --port 5001` to function without throwing `argument action: invalid choice: 'daemon'`, `dgx-orchestrator.py` includes a subcommand passthrough mapping that routes `cli daemon` directly into `daemon` mode[cite: 1].

### 5. Multi-User Shared Key Auto-Staging & Permissions[cite: 1]

* **Central Master Key:** `/opt/dgx-cluster-control/id_dgx_orchestrator` is set to `0640` ownership under `tetrel:wheel`[cite: 1].


* **Dynamic Auto-Staging (`resolve_user_identity_key`):** OpenSSH rejects private keys with group-read permissions[cite: 1]. Upon execution, `dgx-orchestrator.py` automatically checks and copies the shared key into the invoking user's `~/.ssh/id_dgx_orchestrator` with strict `0600` permissions, enabling non-root `wheel` users (e.g., `ian`) to run cluster commands natively[cite: 1].



### 6. Global Multi-Model GPU VRAM Teardown Guard[cite: 1]

To prevent multiple vLLM models from stacking on the same GPU and causing immediate CUDA Out-Of-Memory (OOM) or port `8000` conflicts:[cite: 1]

* **Pre-Deployment Purge:** Before spawning any container compose manifest, `execute_deployment` executes a global `docker rm -f vllm-standalone vllm-head vllm-worker` across all target nodes[cite: 1]. This completely flushes GPU VRAM prior to container initialization[cite: 1].



### 7. OpenMP Thread Fencing & CPU Scheduling Protections[cite: 1]

To prevent PyTorch and vLLM background worker threads from consuming 100% of available Grace ARM CPU cores during multi-node KV cache initialization, multi-node topologies enforce strict CPU thread limits:[cite: 1]

* **Environment Fencing:** `OMP_NUM_THREADS=16` and `VLLM_CPU_OMP_THREADS=16` are injected into container environment manifests[cite: 1].


* **Host System Impact:** Restricts OpenMP thread pools to 16 cores per socket, guaranteeing sufficient CPU scheduling headroom for `sshd`, system daemons, and status polling threads[cite: 1].



### 8. Headless Target Mode & DRM Semaphore Lock Prevention[cite: 1]

DGX compute nodes must **never** run desktop GUI display managers (`gdm3`, `gnome-shell`)[cite: 1].

* **Headless Conversion:** `sudo systemctl set-default multi-user.target && sudo systemctl stop gdm3`[cite: 1].



### 9. Self-Healing SSH Transport & Execution Timeouts[cite: 1]

* **Automatic Socket Purging:** Every execution automatically purges stale control sockets in `/tmp/ssh-mux-*` and `~/.ssh/cm-*`[cite: 1].


* **Execution Timeout Guards:** Remote command invocations are wrapped in explicit `timeout 10` execution guards to guarantee non-blocking return codes if a remote node experiences a GPU driver or socket lockup[cite: 1].



---

## Installation & Setup Guide[cite: 1]

Execute these steps on the control station node (**`codepolice`**):[cite: 1]

### 1. Clone Repository & Assign Permissions[cite: 1]

```bash
sudo git clone [https://github.com/tetrelsec/dgx-cluster-control.git](https://github.com/tetrelsec/dgx-cluster-control.git) /opt/dgx-cluster-control
sudo chown -R tetrel:wheel /opt/dgx-cluster-control


```

### 2. Lock Down Master Key Permissions



```bash
sudo chown tetrel:wheel /opt/dgx-cluster-control/id_dgx_orchestrator
sudo chmod 640 /opt/dgx-cluster-control/id_dgx_orchestrator


```

### 3. Bootstrap Virtual Environment (`dgx-env`)



Because `/opt/dgx-cluster-control` is owned by `tetrel:wheel`, execute virtual environment creation and package installation under `sudo -u tetrel`:

```bash
sudo -u tetrel python3 -m venv /opt/dgx-cluster-control/dgx-env
sudo -u tetrel /opt/dgx-cluster-control/dgx-env/bin/python -m ensurepip
sudo -u tetrel /opt/dgx-cluster-control/dgx-env/bin/python -m pip install -r /opt/dgx-cluster-control/requirements.txt


```

### 4. Create Global Symlink



```bash
sudo ln -s /opt/dgx-cluster-control/dgx-config /usr/local/bin/dgx-config


```

### 5. Deploy Web Dashboard Container (Port 5000)



```bash
cd /opt/dgx-cluster-control
docker compose up -d


```

### 6. Launch API Orchestration Daemon (Port 5001)



To ensure the API daemon remains running after closing your terminal session, launch it with `disown`:

```bash
pkill -f "dgx-orchestrator.py daemon"
nohup /opt/dgx-cluster-control/dgx-config daemon --port 5001 > /opt/dgx-cluster-control/api.log 2>&1 & disown


```

Verify daemon operation:

```bash
cat /opt/dgx-cluster-control/api.log
# Expected output: Uvicorn running on [http://0.0.0.0:5001](http://0.0.0.0:5001)


```

---

## User Onboarding & Key Authorization



The orchestrator authenticates with the cluster using the caller's personal SSH key to ensure per-user auditing in remote `auth.log` files.

A user in the `wheel` group elevates once to register their public SSH key across `spark-4` and `spark-3`:

```bash
sudo -u tetrel dgx-config authorize-key --key ~/.ssh/id_ed25519.pub


```

Once authorized, the user executes `dgx-config` natively without `sudo`.

---

## Interface Reference Guide



### Interactive CLI Menu (`dgx-config menu`)



* Renders active cluster runtimes, container states, and GPU telemetry across all Spark nodes.


* Prompts model selection directly from `models.yaml`.


* Automatically restricts topology selection based on model capabilities (1-node vs 2-node).


* Estimates warm restart completion times using `load_times.json`.



### Web Dashboard (`http://codepolice:5000`)



* **Dynamic API Base Routing:** Uses `window.location.hostname` (`http://${window.location.hostname}:5001/api`) to ensure frontend requests route properly from any client browser.


* **Full-Width Terminal Trace:** Displays real-time Docker logs in a full-width bottom panel.


* **Topology Selector Protection:** Dynamically filters node topology dropdown options based on model definitions in `models.yaml`.



### CLI Command Options



* **Check Status:** `dgx-config status`

* **Purge Active Runtimes:** `dgx-config teardown`

* **Deploy Model:** `dgx-config deploy --model deepseek-v4-flash-nvfp4 --nodes 2`

* **View Remote Container Logs:** `dgx-config logs --host spark-4 --tail 100`


---

## Core Model Catalog (`models.yaml`) Summary



* **DeepSeek-V4 Flash NVFP4 (`deepseek-v4-flash-nvfp4`):** Clustered 2-node deployment using `Rarri/DeepSeek-V4-Flash-0731-NVFP4`. Leverages `--moe-backend flashinfer_cutlass` with a container entrypoint patch forcing `FLASHINFER_CUTLASS_MXFP4_MXFP8` FP8 activation scaling, `--disable-custom-all-reduce`, and `--kv-cache-dtype fp8`.


* **Nemotron 3.5 Lightning (`nemotron-3.5-lightning`):** Single-node deployment utilizing DSpark speculative decoding.


* **Muse Glimmer 30B (`muse-glimmer-30b` / `muse-glimmer-30b-nvfp4`):** Multimodal vision-language model leveraging DFlash speculative configurations.


* **Qwen Architectures (`qwen-3.8-27b`, `qwen-3.8-27b-nvfp4`, `qwen-3.6-27b-nvfp4`, `qwen-3.5-122b`):** Qwen 122B MoE locked to 2-node pipeline parallelism with `--enforce-eager`.


* **Llama & Gemma (`llama-3.3-70b`, `llama-4-fp4`, `llama-4-fp8`, `gemma-4-31b`):** Llama 70B locked to 2-node pipeline parallelism.



---

## Release Tombstones & Fix Log



### 38. Grace Blackwell (GB10) MXFP4 MoE Engine & Activation Patch (V4.6.3)

* **The Trap:** On Grace Blackwell (GB10) GPUs under vLLM `0.21.0`, TRTLLM, DeepGEMM, and Triton MXFP4 MoE kernels fail device compatibility checks. Marlin fails with a `KeyError: 'layers.0.ffn.experts.w13_input_scale'` on raw HuggingFace safetensors. `FlashInferExperts` (`--moe-backend flashinfer_cutlass`) is the only valid GB10 kernel, but defaults to `FLASHINFER_CUTLASS_MXFP4_BF16` (BF16 activations) while DeepSeek-V4 requires FP8 activations (`FLASHINFER_CUTLASS_MXFP4_MXFP8`). Passing `flashinfer_cutlass_afp8` is rejected by vLLM's CLI parser.
* **The Fix:** Configured `models.yaml` to use `--moe-backend flashinfer_cutlass` and injected a container entrypoint `sed` patch in `dgx-orchestrator.py` (`sed -i "s/FLASHINFER_CUTLASS_MXFP4_BF16/FLASHINFER_CUTLASS_MXFP4_MXFP8/g" ...`) to force the FP8 activation Cutlass engine at runtime.

### 37. Multi-Node vLLM V1 Engine `--nnodes` & `--node-rank` Flags (V4.6.3)

* **The Trap:** Multi-node container launches using vLLM V1 (`nvcr.io/nvidia/vllm:26.05.post1-py3`) omitted `--nnodes` and `--node-rank`. V1's `multiproc_executor` defaulted to single-host execution and attempted to allocate all pipeline-parallel ranks onto `spark-4`'s single physical GPU, triggering a `local_world_size <= visible_device_count` crash.
* **The Fix:** Updated `execute_deployment()` in `dgx-orchestrator.py` to inject `--nnodes <nodes>` and `--node-rank 0/1` explicitly into `docker run` commands.

### 36. HuggingFace Auth Token Discovery (`get_hf_token`) (V4.6.3)

* **The Trap:** Unauthenticated HuggingFace Hub requests triggered rate-limiting warnings or failed private checkpoint downloads.
* **The Fix:** Implemented `get_hf_token()` in `dgx-orchestrator.py` to search environment variables, `/opt/dgx-cluster-control/.secrets`, or `~/.cache/huggingface/token`, injecting `-e HF_TOKEN=<token>` directly into vLLM containers.

### 35. SSH Multi-Token Argument Quoting via `shlex.quote` (V4.6.3)

* **The Trap:** Passing complex CLI arguments or JSON configuration strings (`--attention-config '{"use_fp4_indexer_cache": true}'`) through `run_ssh()` allowed remote shell layers to strip inner quotes or mangle JSON formatting.
* **The Fix:** Updated `run_ssh()` to process command lists using `shlex.quote` on each token, ensuring preserved remote evaluation across OpenSSH shell boundaries.

### 34. Web Dashboard Dynamic Hostname Routing (V4.6.2)



* **The Trap:** Hardcoding `10.0.14.43` or `localhost` as `API_BASE` in `index.html` caused cross-origin requests or connection failures when opening the dashboard from external workstations.


* **The Fix:** Updated `index.html` to evaluate `window.location.hostname` dynamically (`http://${window.location.hostname}:5001/api`).



### 33. Grace Blackwell (GB10) Unified Memory Telemetry Parser (V4.6.2)



* **The Trap:** Grace Blackwell LPDDR5x Unified Memory returns `[N/A]` for standard `nvidia-smi` memory queries, causing strict integer parsing checks to reject telemetry lines and return empty metrics.


* **The Fix:** Updated `get_lightweight_telemetry()` in `dgx-orchestrator.py` to parse temperature and GPU utilization independently while assigning `Unified / 131072 MB` for VRAM fields.



### 32. SSH Remote Shell Pipeline Syntax Expansion Bug (V4.6.2)



* **The Trap:** Executing `docker ps --format '{{.Names}}|{{.Image}}'` over SSH caused the remote Bash shell to interpret `|` as a shell pipe, throwing `Exit 2` or `Exit 127` errors.


* **The Fix:** Replaced `|` delimiters with double colons (`::`) across remote Docker format strings.



### 31. Wrapper Subcommand Passthrough for Daemon Execution (V4.6.1)



* **The Trap:** Running `dgx-config daemon` caused the wrapper to pass `cli daemon` to `dgx-orchestrator.py`, triggering `argparse` choice validation errors.


* **The Fix:** Updated `dgx-orchestrator.py` to map `args.subcommand == "cli"` and `args.cli_action == "daemon"` directly into daemon execution mode.



### 30. Virtual Environment Bootstrap & Ownership (`tetrel:wheel`) (V4.6.1)



* **The Trap:** Executing `ensurepip` or `pip install` as non-root user `ian` on `/opt/dgx-cluster-control/dgx-env` failed with `[Errno 13] Permission denied`.


* **The Fix:** Standardized virtual environment management commands to run via `sudo -u tetrel`.



### 29. Web Dashboard Full-Width Logs & Dynamic Topology Selector (V4.6.0)



* **The Trap:** Squeezed log panels obscured long trace outputs, and invalid topology options allowed users to attempt single-node deployments of 70B+ models.


* **The Fix:** Moved live logs to a full-width bottom panel and added dynamic catalog parsing in JavaScript to filter valid topology choices.



### 28. YAML Argument Comment Pollution & Syntax Sanitization (V4.6.0)



* **The Trap:** Inline bash comments inside folded YAML multiline strings (`>-`) were parsed as literal command-line flags, causing vLLM initialization to fail.


* **The Fix:** Stripped all inline comments and trailing formatting artifacts from `models.yaml`.



### 27. Multi-User Shared Key Auto-Staging & OpenSSH 0600 Strictness (V4.5.0)



* **The Trap:** Group-readable permissions (`0640`) on shared SSH keys triggered OpenSSH `bad permissions` rejections.


* **The Fix:** Implemented `resolve_user_identity_key()` in `dgx-orchestrator.py` to auto-stage key copies into `~/.ssh/id_dgx_orchestrator` with `0600` permissions.

