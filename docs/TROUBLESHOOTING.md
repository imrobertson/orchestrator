# Grace Blackwell (GB10) Cluster Troubleshooting & Diagnostics

### 1. Multi-Node Executor & Shared Memory Limits
* **Failure:** `AssertionError: collective_rpc should not be called on follower node` or workers hanging post-NCCL initialization.
* **Cause:** vLLM V1 `mp` (multiprocessing) backend relies on Linux shared memory (`/dev/shm`), which cannot cross physical network boundaries.
* **Rule:** 2-node topologies across physical hosts MUST pass `--distributed-executor-backend ray` and set `VLLM_USE_V1=0` in `env_vars` to force the V0 cross-host Ray executor.

### 2. GB10 SwiGLU Clamp Validation Error
* **Failure:** `ValueError` during initialization regarding `swiglu_limit=10.0` or missing MoE backends.
* **Cause:** DeepSeek-V4 enforces SwiGLU clamping (`10.0`), which standard `flashinfer_b12x` kernels do not apply on GB10 silicon.
* **Rule:** Explicitly pass `--moe-backend flashinfer_cutlass` in `vllm_args`. If using speculative decoding (DSpark), enforce `"moe_backend":"flashinfer_cutlass"` inside `--speculative-config`.

### 3. Container Parser Override Loop (NVFP4 KV Cache)
* **Failure:** Pydantic validation errors stating `nvfp4_ds_mla` is unsupported.
* **Cause:** Passing `--quantization modelopt_fp4` directly triggers an internal container entrypoint hook that forces `--kv-cache-dtype nvfp4_ds_mla`, breaking DeepSeek MLA cache requirements.
* **Rule:** Omit `--quantization modelopt_fp4` from CLI arguments. Allow vLLM to auto-detect quantization from `config.json` while maintaining explicit `--kv-cache-dtype fp8`.

### 4. YAML Folded Block Scalar Comment Pollution
* **Failure:** `api_server.py: error: unrecognized arguments: # ...`
* **Cause:** YAML folded scalars (`>-`) flatten all newlines into spaces. `shlex.split()` parses bash `#` comments as literal string arguments.
* **Rule:** Keep all operational notes and comments strictly outside of the `vllm_args: >-` string scalar.

### 5. Ray Worker Log Redirection
* **Diagnostic Note:** Ray worker stdout/stderr outputs are piped internally to `/tmp/ray/session_latest/logs/worker*.out` inside the container namespace. Standalone `docker logs` commands only observe top-level daemon initialization.
