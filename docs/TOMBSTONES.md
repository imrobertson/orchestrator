# Control Plane Release Tombstones & Fix Log
### 48. In-RAM Telemetry Loss on Daemon Shutdown (V4.8.3)
* **The Trap:** Real-time token counts, session durations, and MTP (Multi-Token Prediction) hit rates were accumulated in memory to protect host NVMe drives from continuous disk writes. Unplanned daemon restarts, container updates, or host reboots wiped uncommitted session analytics.
* **The Fix:** Implemented a 1-hour periodic delta-checkpoint flush in `SessionTracker` and attached OS signal traps (`SIGTERM` / `SIGINT`) to `dgx-orchestrator.py` to force stateful commits to `model_ledger.json` before process termination.

### 47. Idle-Time Metric Pollution in Long-Running Sessions (V4.8.3)
* **The Trap:** Calculating Tokens Per Second (TPS) across a continuous session by subtracting session start time from session end time caused long idle periods (e.g., a 600-second quiet wait before closing a session) to dilute high-speed token generation bursts into artificially low averages.
* **The Fix:** Decoupled the 10-minute idle tripwire from the active compute timer. Active TPS is calculated strictly as `Total_Generated_Tokens / (Last_Active_TS - First_Active_TS)`. The 600-second idle period triggers session commits to `model_ledger.json` but is completely excluded from the time divisor.

### 46. Wrapped `bash -c` Shell Command Inspection Failure (V4.8.3)
* **The Trap:** Docker container inspection on Ray head nodes returned array-wrapped shell strings (e.g., `["bash", "-c", "ray start ... && python3 -m vllm ... --model <path>"]`). The inspection parser attempted to find `--model` as a discrete array element, threw an exception, and defaulted to labeling the active container as `"Active Container"`, preventing catalog matching.
* **The Fix:** Upgraded `_discover_host_container()` to execute a regex pattern (`--model\s+([^\s]+)`) across string-wrapped entrypoint commands to extract the model path regardless of shell layering.

### 45. Multi-Node Asymmetric Logging & Worker UI State Desync (V4.8.3)
* **The Trap:** In multi-node Ray deployments, the Head node (`spark-4`) acts as driver and logs shard-loading progress to Docker `stdout`. The Worker node (`spark-3`) only runs a passive `ray start` process and receives weights into VRAM over NCCL without outputting log progress. The orchestrator's log stage detector left `spark-3` permanently stuck on `Active Container` / `INITIALIZING` with a generic fallback ETA.
* **The Fix:** Updated `_compute_cluster_status_impl()` to treat active worker nodes as UI slaves to the head node during boot sequences—broadcasting the head node's detected model name, stage (`LOADING SHARDS`, `COMPILING KERNELS`), and active ETA across all worker UI cards simultaneously.

### 44. Grace Blackwell (GB10) Power Limit String Parsing (`PWR: 6/N/AW`) (V4.8.3)
* **The Trap:** On Grace Blackwell (GB10) unified superchips, power is dynamically managed across the package. `nvidia-smi` returns `N/A` or `[Not Supported]` for GPU-isolated power limits. The UI blindly concatenated the draw, limit, and unit strings, rendering `PWR: 6/N/AW`.
* **The Fix:** Sanitized telemetry parsing in `dgx-orchestrator.py` and string construction in `index.html`. If the hardware driver returns `N/A` for the limit, the dashboard gracefully falls back to displaying active draw only (`PWR: 6W`).

### 43. Multi-Node V1 Shared Memory (`/dev/shm`) Followers Crash (V4.8.1)
* **The Trap:** Attempting to deploy multi-node models using `--distributed-executor-backend mp` on the vLLM V1 engine triggered `AssertionError: collective_rpc should not be called on follower node` on `spark-3`. The `mp` backend relies on host IPC shared memory (`/dev/shm`), which cannot cross physical nodes.
* **The Fix:** Multi-node topologies across physical hosts must use `--distributed-executor-backend ray` and set `VLLM_USE_V1=0` in `env_vars` to force the V0 cross-host Ray executor.

### 42. YAML Folded Scalar Comment Pollution in `vllm_args` (V4.8.1)
* **The Trap:** Inlining bash comments (`#`) inside a YAML folded block scalar (`vllm_args: >-`) resulted in all newlines being flattened into a single space. `shlex.split()` parsed the comments as literal CLI arguments, causing `api_server.py: error: unrecognized arguments: # ...`.
* **The Fix:** Shifted all operational notes and comments strictly outside of the `vllm_args: >-` string scalar.

### 41. Recipe Catalog Empty Due to Filename/`name:` Field Drift (V4.8.0)
* **The Trap:** The original recipe schema carried both a filename and an internal `name:` field, required to match. During a merge, two recipes' `name:` fields drifted out of sync with their filenames. Because `build_catalog_response()` fails closed on any single bad recipe, the entire model catalog silently went empty — not just the two broken files — with no error surfaced anywhere the dashboard user could see.
* **The Fix:** Removed `name:` from the schema entirely. The filename is now the only identifier a recipe has, so there's structurally nothing left for it to disagree with. The whole-catalog-fails-on-one-bad-recipe behavior itself is unchanged and is tracked separately.

### 40. Dashboard Polling Hang & Duplicate Load-Time Recording (V4.8.0)
* **The Trap:** Two separate bugs, both invisible under light use. (1) `get_cluster_status()` made several sequential SSH round trips per host with no overall deadline; under frequent dashboard polling, a single unreachable host could cause requests to back up faster than they drained, presenting as a fully hung dashboard. (2) `record_load_time()` was called on every single status poll while a container sat idle-but-ready, not just once at actual readiness — `load_times.json` entries grew without bound instead of capturing one real cold-start duration, silently corrupting the ETA estimator's historical data.
* **The Fix:** `get_cluster_status()` now single-flights concurrent callers and enforces a hard wall-clock ceiling (`STATUS_CALL_TIMEOUT_SEC`) via bounded per-host futures instead of unbounded `as_completed()`. `record_load_time()` now tracks which container instance has already been recorded and fires at most once per instance.

### 39. Docker Control Plane & Delegate Wrapper (V4.7.0)
* **The Trap:** Host-level PEP 668 constraints and local `venv` drift on `codepolice` made cross-environment management fragile and difficult to upgrade.
* **The Fix:** Containerized the full control plane stack inside a `python:3.12-slim` Docker image on `maestro`. Re-engineered the `dgx-config` CLI wrapper into a context-aware Docker delegate that transparently forwards TTY flags, injects host identity, auto-stages external key files, and handles socket cleanup directly inside the container namespace.

### 38. Grace Blackwell (GB10) MXFP4 MoE Engine & Activation Patch (V4.6.3)
* **The Trap:** On Grace Blackwell (GB10) GPUs under vLLM `0.21.0`, TRTLLM, DeepGEMM, and Triton MXFP4 MoE kernels fail device compatibility checks. Marlin fails with a `KeyError: 'layers.0.ffn.experts.w13_input_scale'` on raw HuggingFace safetensors. `FlashInferExperts` (`--moe-backend flashinfer_cutlass`) is the only valid GB10 kernel, but defaults to `FLASHINFER_CUTLASS_MXFP4_BF16` (BF16 activations) while DeepSeek-V4 requires FP8 activations (`FLASHINFER_CUTLASS_MXFP4_MXFP8`). Passing `flashinfer_cutlass_afp8` is rejected by vLLM's CLI parser.
* **The Fix:** Configured the recipe to use `--moe-backend flashinfer_cutlass` and injected a container entrypoint `sed` patch (`sed -i "s/FLASHINFER_CUTLASS_MXFP4_BF16/FLASHINFER_CUTLASS_MXFP4_MXFP8/g" ...`) to force the FP8 activation Cutlass engine at runtime.

### 37. Multi-Node vLLM V1 Engine `--nnodes` & `--node-rank` Flags (V4.6.3)
* **The Trap:** Multi-node container launches using vLLM V1 (`nvcr.io/nvidia/vllm:26.05.post1-py3`) omitted `--nnodes` and `--node-rank`. V1's `multiproc_executor` defaulted to single-host execution and attempted to allocate all pipeline-parallel ranks onto `spark-4`'s single physical GPU, triggering a `local_world_size <= visible_device_count` crash.
* **The Fix:** Updated `execute_deployment()` in `dgx-orchestrator.py` to inject `--nnodes <nodes>` and `--node-rank 0/1` explicitly into `docker run` commands.

### 36. HuggingFace Auth Token Discovery (`get_hf_token`) (V4.6.3)
* **The Trap:** Unauthenticated HuggingFace Hub requests triggered rate-limiting warnings or failed private checkpoint downloads.
* **The Fix:** Implemented `get_hf_token()` in `common/ssh.py` to search environment variables, the project's `.secrets` file, or `~/.cache/huggingface/token`, injecting `-e HF_TOKEN=<token>` directly into vLLM containers.

### 35. SSH Multi-Token Argument Quoting via `shlex.quote` (V4.6.3)
* **The Trap:** Passing complex CLI arguments or JSON configuration strings (`--attention-config '{"use_fp4_indexer_cache": true}'`) through `run_ssh()` allowed remote shell layers to strip inner quotes or mangle JSON formatting.
* **The Fix:** Updated `run_ssh()` to process command lists using `shlex.quote` on each token, ensuring preserved remote evaluation across OpenSSH shell boundaries.

### 34. Web Dashboard Dynamic Hostname Routing (V4.6.2)
* **The Trap:** Hardcoding `10.0.14.43` or `localhost` as `API_BASE` in `index.html` caused cross-origin requests or connection failures when opening the dashboard from external workstations.
* **The Fix:** Updated `index.html` to evaluate `window.location.hostname` dynamically.

### 33. Grace Blackwell (GB10) Unified Memory Telemetry Parser (V4.6.2)
* **The Trap:** Grace Blackwell LPDDR5x Unified Memory returns `[N/A]` for standard `nvidia-smi` memory queries, causing strict integer parsing checks to reject telemetry lines and return empty metrics.
* **The Fix:** Updated `get_lightweight_telemetry()` in `dgx-orchestrator.py` to parse temperature and GPU utilization independently while assigning `Unified / 131072 MB` for VRAM fields.

### 32. SSH Remote Shell Pipeline Syntax Expansion Bug (V4.6.2)
* **The Trap:** Executing `docker ps --format '{{.Names}}|{{.Image}}'` over SSH caused the remote Bash shell to interpret `|` as a shell pipe, throwing `Exit 2` or `Exit 127` errors.
* **The Fix:** Replaced `|` delimiters with double colons (`::`) across remote Docker format strings.

### 31. Wrapper Subcommand Passthrough for Daemon Execution (V4.6.1)
* **The Trap:** Running `dgx-config daemon` caused the wrapper to pass `cli daemon` to `dgx-orchestrator.py`, triggering `argparse` choice validation errors.
* **The Fix:** Updated `dgx-orchestrator.py` to map `args.subcommand == "cli"` and `args.cli_action == "daemon"` directly into daemon execution mode.

### 30. Virtual Environment Bootstrap & Ownership (V4.6.1)
* **The Trap:** Running `pip install` as a non-service user on the host failed with permission errors.
* **The Fix:** Standardized on the containerized stack (see Tombstone #39) rather than a host-level `venv`, sidestepping this class of problem entirely.

### 29. Web Dashboard Full-Width Logs & Dynamic Topology Selector (V4.6.0)
* **The Trap:** Squeezed log panels obscured long trace outputs, and invalid topology options allowed users to attempt single-node deployments of 70B+ models.
* **The Fix:** Moved live logs to a full-width bottom panel and added dynamic catalog parsing in JavaScript to filter valid topology choices.

### 28. YAML Argument Comment Pollution & Syntax Sanitization (V4.6.0)
* **The Trap:** Inline bash comments inside folded YAML multiline strings (`>-`) were parsed as literal command-line flags, causing vLLM initialization to fail.
* **The Fix:** Stripped all inline comments and trailing formatting artifacts from the catalog source.

### 27. Multi-User Shared Key Auto-Staging & OpenSSH 0600 Strictness (V4.5.0)
* **The Trap:** Group-readable permissions (`0640`) on shared SSH keys triggered OpenSSH `bad permissions` rejections.
* **The Fix:** Implemented `resolve_user_identity_key()` to auto-stage key copies into `~/.ssh/id_dgx_orchestrator` with `0600` permissions.
