# Control Plane Release Tombstones & Fix Log



### 60. Crashed Engine Misreported as Indefinite Warmup — Keyword Collision in Log Scanner (V4.8.4)
* **The Trap:** `detect_model_stage()` scanned container logs in reverse for progress keywords, including `"kv cache"` as a WARMUP signal. A vLLM startup crash (`ValueError: nvfp4 KV cache is not supported with MLA backends...`) contains that exact phrase inside its own error message, so the scanner matched the crash report itself as legitimate progress and reported `NOT READY - WARMUP` forever, with an ETA that counted up for over an hour with no historic data. Compounded by 2-node Ray deploys: the container's PID 1 is `ray start --block`, not the vLLM engine — the engine runs via a separate detached `docker exec -d`, so Docker correctly reports the container `RUNNING` long after the actual engine process has died. Container-level health alone can't be trusted for this launch path.
* **The Fix:** Added `_detect_crash_signature()`, which checks the log tail for an actual Python traceback *before* the keyword scan runs, and short-circuits to `CRASHED (ENGINE EXITED: <exception>)` if found — regardless of what substrings a future crash's error text happens to contain. `_finalize_host_status()` now skips ETA computation entirely for a crashed status instead of computing a countdown against a dead process.

### 59. No Mutual Exclusion Between Deploy, Teardown, and Benchmark Controls (V4.8.4)
* **The Trap:** Nothing on the dashboard stopped clicking Deploy while a teardown was mid-flight killing containers, or clicking Teardown while a deploy was mid-launch — both race against the same containers and can leave the cluster in a state neither operation intended. `CLUSTER_OP_LOCK` protected this server-side (a second call gets a "cluster busy" error), but the UI gave no indication a click would fail until the confusing error came back.
* **The Fix:** Added `applyOperationLocks()` in `index.html`, which cross-locks Deploy, Teardown, the entire deploy form (model/topology/head/user-id), and both benchmark controls whenever either deploy or teardown is in flight, driven by a local `isDeploying` flag and the polled `is_tearing_down` status field.

### 58. Teardown Became a Multi-Phase ~60s Operation With Zero Progress Visibility (V4.8.4)
* **The Trap:** After the grace-period rewrite (see #54), teardown could legitimately take up to ~60s across three phases, but the dashboard button just showed a static "Tearing down..." label with no indication of which phase it was in or whether it was progressing.
* **The Fix:** Added `TEARDOWN_STATE` (mirroring the existing `BENCHMARK_STATE` pattern), written by `_execute_teardown_impl` at each phase transition (`signaling` → `stopping` → `removing` → `done`) and surfaced via `/api/status` as `is_tearing_down`/`teardown_message`. Teardown itself stays synchronous (deploy depends on that ordering) — the dashboard's existing 4s status poll picks up live phase text concurrently with the blocking POST.

### 57. Near-Duplicate Recipe Catalog Keys — Silent Model Repoint (V4.8.4)
* **The Trap:** `recipes/local/deepseek-v4-flash-nvfp4.yaml` (an older, distinct NVIDIA build) and `recipes/local/deepseek-v4-flash-0731-nvfp4.yaml` (the correct, working auroter 0731 build) coexisted with catalog keys one keystroke apart. A prior session silently repointed the former's `hf_path` to the *same* model as the latter while also swapping in an unvalidated `--kv-cache-dtype nvfp4_ds_mla` (see #56) — with no filename change to signal any of it happened. Deploying the wrong key crashed on a config that had never actually been tested end-to-end.
* **The Fix:** Deleted `deepseek-v4-flash-nvfp4.yaml` outright rather than reverting it to its original config — the older NVIDIA build it used to serve wasn't in active use, so removing it closes both the collision risk and the invalid kv-cache-dtype landmine in one move. No structural fix for the underlying class of error yet — near-duplicate catalog keys with no loader-level collision warning remains an open gap, tracked in `ROADMAP.md`.


### 56. vLLM Rejects `nvfp4`-Family KV Cache Dtype for MLA Models (V4.8.4)
* **The Trap:** `--kv-cache-dtype nvfp4_ds_mla` — a custom identifier intended as a GB10/`flashinfer_b12x` MLA cache bypass — crashes at engine-config-creation time with `pydantic_core.ValidationError: nvfp4 KV cache is not supported with MLA (Multi-head Latent Attention) backends`. This is a vLLM core validation guard (landed alongside upstream PR #40177, "Add nvfp4 kv cache support"), not an orchestrator or image issue — confirmed the pulled `eugr/spark-vllm-b12x:latest` image digest was unchanged across the working and broken deploys.
* **The Fix:** No code fix — this is a hard vLLM constraint. DeepSeek V4 Flash (and any other MLA-architecture model) must use `--kv-cache-dtype fp8` or `auto`. Any recipe attempting an `nvfp4`-family dtype on an MLA model needs to be caught before deploy, not after — a candidate for future recipe-schema validation.

### 55. Sequential Per-Host Teardown Left Worker NCCL-Connected to a Vanished Head (V4.8.4)
* **The Trap:** The grace-period rewrite (#54) processed hosts one at a time in a `for` loop. Head could complete its entire ~20-40s graceful shutdown and be fully gone before worker's teardown cycle even began — worker spent that whole window still alive and still NCCL/Ray-connected to a rank-0 head that had already vanished mid-collective-op, observed on the dashboard as the worker going "confused" and crashing on its own well before teardown ever touched it directly.
* **The Fix:** `_execute_teardown_impl` now runs each phase (signal, stop, remove) across all target hosts *concurrently* via `WORKER_POOL`, not one host fully torn down before the next starts, so head and worker are signaled and come down together.

### 54. Teardown Hard-Kill Risked Corrupting In-Flight JIT Compiles (V4.8.4)
* **The Trap:** Teardown SIGKILL'd host processes and ran `docker rm -f` with zero grace period. JIT compilation shells out to `nvcc`/`ptxas`/`cicc` as child subprocesses that write cache artifacts non-atomically; SIGKILL on the parent doesn't propagate to those children, so a hard-kill mid-compile could leave an orphaned compiler process writing into the persistent, shared cache directory unsupervised, or leave a half-written artifact at the path the loader treats as a cache hit on the next load — silent, one-time, unpredictable recompiles with no error surfaced anywhere. Compounded by the absence of `--init` on `docker run`, meaning the container's PID 1 had no proper zombie-reaping or signal-forwarding for exactly this kind of subprocess tree.
* **The Fix:** Added `TEARDOWN_GRACE_SEC` (20s): host processes now get SIGTERM and a real grace period before escalating to `-9`, and `docker stop --time N` runs before `docker rm -f` rather than skipping straight to it. Added `--init` to both the 1-node and 2-node `docker run` construction paths.

### 53. `historical_tps` Never Resolved — Ledger Key Mismatch (V4.8.4)
* **The Trap:** `enrich_catalog()` looked up `ledger_tps.get(m_key, "N/A")` where `m_key` is the catalog/recipe key, but `benchmark.py` logged the raw served HF model basename into the ledger's `Model` column — two different string spaces that never matched. `historical_tps` was silently `N/A` for every model, always.
* **The Fix:** Added `--model-key` to `benchmark.py`, threaded through from both the deploy-triggered and dashboard-triggered benchmark paths, so the ledger logs the catalog key directly. Old ledger rows predate this and won't retroactively match; `benchmark_ledger.csv` was rotated rather than backfilled.

### 52. Benchmark TTFT/Decode Speed Reported Zero for Reasoning Models (V4.8.4)
* **The Trap:** `run_benchmark_pass()` only started the TTFT clock and counted tokens when a stream chunk's `delta.content` was truthy. DeepSeek V4 (run with `--reasoning-parser deepseek_v4`) streams its initial tokens into `delta.reasoning_content` instead — for a reasoning-heavy response, `first_token_time` never fired and `decode_tps` reported `0.0`.
* **The Fix:** `run_benchmark_pass()` now starts the clock on either `content` or `reasoning_content`.

### 51. JIT Cache Pruning Deleted Individual Files, Risking Half-Written Cache Entries (V4.8.4)
* **The Trap:** `prune_cluster_cache()` ran `find ~/.cache/{tilelang,deepgemm,triton} -type f -atime +N -delete`. A Triton/TileLang cache entry is a *directory* of co-dependent artifacts (metadata JSON + compiled binary); deleting files piecemeal can leave a half-entry that the loader treats as a hit and then fails to load — corruption, not a clean miss. Also relied on `atime`, which some filesystem mount options don't reliably update.
* **The Fix:** Replaced with a remote Python script that evicts whole entry *directories*, strictly oldest-first (`max(atime, mtime)` per entry, degrading gracefully to mtime-only), only when a host is below `--min-free-gb`. Added a fully read-only `cache-inventory` command (entry counts, sizes, LRU order, mount options) safe to run against production at any time, and `--dry-run` on the prune path itself.

### 50. Orchestrator Daemon Self-Destruction via Broad Teardown `pkill` & Missing Lock Wrapper (V4.8.3)
* **The Trap:** Triggering a deployment executed `_execute_teardown_impl()`, which ran `sudo pkill -9 -f 'vllm|ray|python3'`. Because `dgx-orchestrator` runs as a `python3` process on `spark-4`, it killed its own HTTP server mid-request, causing browser deployment calls to fail with `Failed to fetch`. Additionally, `execute_deployment()` was missing its `CLUSTER_OP_LOCK` wrapper definition during refactoring, raising a backend `NameError`.
* **The Fix:** Re-implemented `execute_deployment()` with `CLUSTER_OP_LOCK` enforcement, and updated host teardown commands to explicitly filter out `dgx-orchestrator` PIDs (`grep -v 'dgx-orchestrator'`) before issuing process kill signals.

### 49. Speculative Metric Key Mismatch & Strict Recipe MTP Checks (V4.8.3)
* **The Trap:** vLLM and DeepSeek-V4 speculative models expose draft/accepted token counters under `vllm:spec_decode_num_draft_tokens_total` and `vllm:spec_decode_num_accepted_tokens_total`. The scraper was listening for legacy `vllm:num_spec_tokens_*` keys, resulting in zeroed draft stats. Furthermore, `enrich_catalog()` strictly checked CLI flags for `mtp_enabled`, hiding speculative UI metrics for models using integrated draft heads or alternative flags like `--speculative-config`.
* **The Fix:** Updated `get_vllm_metrics()` to parse `vllm:spec_decode_num_*` Prometheus metrics, and expanded `enrich_catalog()` to check case-insensitively for speculative keywords (`speculative`, `mtp`, `draft`, `nextn`, `proposal`) or historical ledger activity.

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
