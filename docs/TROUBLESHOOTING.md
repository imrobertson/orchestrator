# Grace Blackwell (GB10) Cluster Troubleshooting & Diagnostics

## Recipe Tuning Reference (Twin-Spark GB10)

Distilled from real deploys, not guessed. Each entry is marked with a
confidence level, since a couple of real incidents came from treating an
untested `vllm_args` combination as if it were proven just because it was
written down in a recipe file:

* **Validated** — watched it actually serve a request successfully.
* **Known-bad** — watched it fail, understand the mechanism.
* **Unconfirmed** — present in a recipe at some point, but the deploy that
  carried it crashed on something else first, so it never actually got
  exercised. Don't treat this the same as Validated just because it's in
  git.

### KV cache dtype by attention architecture

**Known-bad:** any `nvfp4`-family `--kv-cache-dtype` (e.g. `nvfp4_ds_mla`)
on an MLA-architecture model (DeepSeek V4 and siblings). This isn't
orchestrator- or image-specific — it's a hard guard in vLLM's own
engine-config validation, landed alongside upstream PR #40177 ("Add nvfp4
kv cache support"), which wires NVFP4 KV cache through the generic
FlashInfer trtllm-gen path and explicitly excludes MLA models. Fails at
config-creation time, before any model loading or serving starts:
`pydantic_core.ValidationError: nvfp4 KV cache is not supported with MLA
(Multi-head Latent Attention) backends`.

**Validated:** `--kv-cache-dtype fp8` on MLA models. This is what
`deepseek-v4-flash-0731-nvfp4.yaml` actually runs.

**Rule of thumb:** if the model card or hf_path mentions MLA (DeepSeek V2/
V3/V4, and derivatives), don't reach for an `nvfp4` KV cache dtype no
matter how the identifier is named — `fp8` or `auto` only.

### Quantization flag / container entrypoint interaction

**Known-bad:** passing `--quantization modelopt_fp4` together with an
explicit `--kv-cache-dtype`. The container's entrypoint has a hook that
detects `modelopt_fp4` and silently overrides whatever KV cache dtype was
explicitly set back to `nvfp4_ds_mla` — which then trips the guard above,
even when the recipe author correctly wrote `fp8`. See failure mode #3
below for the original write-up.

**Validated workaround:** omit `--quantization modelopt_fp4` entirely and
let vLLM auto-detect quantization from the model's own `config.json`,
while keeping `--kv-cache-dtype fp8` explicit. This is the actual pattern
in the working DeepSeek V4 Flash 0731 recipe.

### MoE backend selection for GB10

**Validated:** `--moe-backend flashinfer_cutlass`. Required for GB10's
SwiGLU clamping behavior — see failure mode #2 below. This is what every
currently-working DeepSeek recipe uses.

**Unconfirmed:** `--moe-backend flashinfer_b12x` ("decoupled NVFP4 MoE
kernels," per a recipe comment that introduced it). It was only ever
present in the recipe that also carried the known-bad kv-cache-dtype
above, and that deploy crashed during config validation — before MoE
execution was ever reached. Nothing here confirms `flashinfer_b12x` works
*or* that it's broken; it simply hasn't been exercised. Don't treat it as
validated just because a recipe comment described what it's supposed to
do.

### Multi-node executor requirements

**Validated:** `--distributed-executor-backend ray` plus `VLLM_USE_V1=0`
in `env_vars` for any 2-node topology. See failure mode #1 below for why
the alternative (`mp` backend) doesn't work across physical hosts.

### Speculative decoding / MTP (DSpark) config shape

**Unconfirmed.** A `--speculative-config
'{"method":"dspark","num_speculative_tokens":5,"draft_sample_method":"probabilistic","moe_backend":"b12x"}'`
block appeared in the same recipe as the two known-bad items above, and
for the same reason — the deploy carrying it never got past config
validation, so this specific shape has never actually run. Treat MTP/
DSpark on DeepSeek V4 as an open experiment, not a documented working
pattern, until someone deploys it on a recipe that's otherwise known-good
(e.g. layered onto the validated `fp8` / `flashinfer_cutlass` base) and it
actually serves traffic.

### JIT / compute cache mount isolation

**Known-bad (subtle):** mounting a single broad host directory to
`/root/.cache` when that same host directory's `huggingface` subfolder is
*also* separately mounted to `/root/.cache/huggingface`. Docker resolves
the overlap by path specificity, so it "works" — until heavy concurrent
mmap operations (which is exactly what loading HuggingFace safetensors
does) hit unpredictably through the broader parent mount instead of the
more specific one. Real, reproducible corruption risk under heavy I/O, not
a hypothetical.

**Validated fix:** mount each JIT cache root explicitly and separately —
`triton`, `tilelang`, `deepgemm`, `vllm`, `flashinfer` each to their own
non-overlapping `/root/.cache/<name>` target, with the HuggingFace mount
entirely outside that tree. This is what `_jit_cache_mounts_and_env()`
does today. If you're hand-rolling a `docker run` outside the orchestrator
for testing, replicate this shape rather than a single broad `.cache`
bind.

### Speculative Decoding / MTP (DSpark) Constraints

**Validated:** `--speculative-config '{"method":"dspark","num_speculative_tokens":5,"draft_sample_method":"probabilistic","moe_backend":"flashinfer_cutlass"}'`.

* **Minimum Draft Token Floor:** DSpark enforces a strict minimum block size of 5 tokens (`num_speculative_tokens >= 5`). Setting values below 5 (e.g. 2 or 3) fails at config-validation time: `pydantic_core.ValidationError: DSpark requires num_speculative_tokens >= dspark_block_size (5)`.
* **Topology Binding:** DSpark speculative decoding cannot span pipeline stages. Multi-node deployments using DSpark **must** use Tensor Parallelism (`tp_size: 2`, `pp_size: 1`). Setting `pp_size: 2` breaks speculative state evaluation.

### Attention Backend Selection (FlashInfer vs Native)

**Validated:** `--attention-backend FLASH_ATTN` for guaranteed boot stability when using non-standard tensor shapes or speculative decoding.

**Known-bad (under speculation):** `--attention-backend B12X_ATTN` when combined with DeepSeek-V4 Sparse MLA attention and speculative draft tokens on GB10 (SM120). FlashInfer's JIT autotuner (`sparse_mla_sm120_decode_dsv4`) fails to match non-standard draft tensor shapes to pre-compiled tuning buckets, falling back to unoptimized runners or timing out during startup.

### Recipe naming discipline

Not a `vllm_args` issue, but earned the hard way: two recipes with catalog
keys one keystroke apart (`deepseek-v4-flash-nvfp4` vs.
`deepseek-v4-flash-0731-nvfp4`) led directly to the wrong one being
deployed by simple typo-adjacent selection — not a deliberate choice. If
you're adding a variant of an existing model (different precision, longer
context, different tuning), make the distinguishing part of the filename
unambiguous. See `docs/USERMANUAL.md`'s "Adding a New Model" section and
`docs/ROADMAP.md`'s near-duplicate-key-detection entry for more.

---

## Incident Log

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

### 6. Crashed Engine Misreported as Indefinite Warmup
* **Failure:** Dashboard shows `NOT READY - WARMUP` indefinitely, ETA counting up with no historic data, for a container that's actually dead.
* **Cause:** In a 2-node Ray deploy, the container's PID 1 is `ray start --block`, not the vLLM engine — the engine runs as a separate detached `docker exec -d` process. Docker correctly reports the container `RUNNING` long after the engine itself crashed. The status logic's log-keyword scanner could also match words inside a crash's own error message (e.g. "kv cache") as if they were progress indicators.
* **Rule:** Status detection now checks for an actual Python traceback in the logs first and reports `CRASHED (ENGINE EXITED: ...)` before falling through to keyword matching. If a model ever looks stuck loading far past its estimate with no error surfaced, check `dgx-config logs` directly rather than trusting the status badge alone — container-level "running" doesn't guarantee the engine inside it is alive.
