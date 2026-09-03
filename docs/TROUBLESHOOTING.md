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

**Validated:** `--distributed-executor-backend ray` for any 2-node
topology on a physical-host pair — no safe default exists without it. See
failure mode #1 below for why the alternative (`mp` backend) doesn't work
across physical hosts.

**Downgraded from Validated, 2026-09-02/03 — do not treat as blanket
truth.** This section previously stated `VLLM_USE_V1=0` alongside the Ray
flag as jointly required. Direct counter-evidence now exists on the same
build (`v0.1.dev20003+gad848fc41.d20260815`): a real 2-node DSpark deploy
(`deepseek-v4-flash-0731-dspark::2_node`) ran `distributed_executor_backend:
'ray'` successfully with `VLLM_USE_V1=0` never set at all — the log shows
`Initializing a V1 LLM engine` and the run completed `ready` with real
phase data. This is stronger evidence than the earlier gemma-4-31b
data point (which only showed the var having *no effect* when set,
2026-08-29) — here it wasn't set and nothing broke. **Do not carry
`VLLM_USE_V1=0` forward into a new 2-node recipe on this build without a
specific reason; it is not a confirmed requirement, and blindly copying
it forward (as `llama-3.3-70b.yaml`'s draft fix initially did, then
corrected) risks treating a stale rule as load-bearing when it may only
have mattered for whatever build/model combination `TOMBSTONES.md` #43
was originally written from.** Two data points (one "no effect," one
"not needed at all") isn't proof it's never needed — a genuine A/B on a
2-node deploy would settle this properly — but it's enough to stop
treating it as a default to copy without thinking.

**Trap confirmed 2026-08-31 (Task MD):** this isn't only "don't
deliberately choose the `mp` backend" — an *empty* `vllm_args` on a
2-node recipe hits the identical failure silently. `_execute_deployment_
impl()`'s `use_ray` check requires the literal tokens
`--distributed-executor-backend` and `ray` to both already be present in
`vllm_args`; if they're not — including simply never having set
`vllm_args` on a hand-authored recipe — the code does not error and does
not default to Ray. It silently falls through to the
`--nnodes`/`--node-rank`/`--master-addr`/`--headless` path instead, which
is exactly Incident #1's failure signature. Reproduced independently on a
from-scratch `Qwen/Qwen3-0.6B` TP=2 recipe with `vllm_args: ""` — nothing
DeepSeek- or Gemma-specific about the trigger. **Any new 2-node recipe
must explicitly carry `--distributed-executor-backend ray` in
`vllm_args`; there is no safe default to fall back on.**

**A second, distinct trap found 2026-09-02 — having the flag right isn't
enough if the image can't run it.** Even with `--distributed-executor-
backend ray` correctly present, the deploy still fails if the base image
never shipped the `ray` binary at all: `exec: ray: not found` before any
Python even starts. Confirmed on the current cluster `default_image`
(`nvcr.io/nvidia/vllm:26.07-py3`) — this is a *different* failure from
Incident #1's `mp`-backend assertion, not a rediscovery of it, and it
means the `mp`/headless fallback isn't even a fallback here: it's the
only thing that ever ran on this image, and it's the thing Incident #1
says doesn't work across physical hosts. `llama-3.3-70b`, `llama-4-fp4`,
`llama-4-fp8`, `qwen-3.6-27b-nvfp4`, and `qwen-2.5-coder-32b` all rely on
`default_image` for their `2_node` topology and are affected; recipes
pinned to `eugr/spark-vllm-b12x:latest` (gemma-4-31b, the DSpark family,
deepseek-r1-distill-qwen-32b) are unaffected — confirmed that image does
ship Ray. See `ROADMAP.md`'s "Recipe-level guardrails against known-bad
flag combinations" entry for the cross-recipe pattern this points to.

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

### Host identity — don't hardcode `spark-3`/`spark-4`

**Validated:** deriving host identity from `HOSTS` / `cluster_config.yaml` via the `PRIMARY_HOST` / `SECONDARY_HOST` / `PRIMARY_HOST_IP` constants.

**Known-bad:** hardcoding the literal strings `spark-3` / `spark-4` or the management IP anywhere in a new code path. This bit for real once already — a hardcoded `target_hosts` in the 2-node deploy path meant a deploy aimed at a different host pair would have silently targeted, and torn down, spark-3/4 instead (see `TOMBSTONES.md` #73). This matters more than it used to: a future host pair is not guaranteed to share a network segment or ConnectX-7 fabric with spark-3/4, so anything that assumes "the cluster" means exactly these two nodes on one fabric is a landmine, not just inflexible code.

**Open gap:** the primary/secondary derivation is naming-only — it doesn't yet express the constraint that a deployed pair must actually share a fabric. See `ARCHITECTURE-MIGRATION-PLAN.md`'s Phase 3 section.

### Recipe naming discipline

Not a `vllm_args` issue, but earned the hard way: two recipes with catalog
keys one keystroke apart (`deepseek-v4-flash-nvfp4` vs.
`deepseek-v4-flash-0731-nvfp4`) led directly to the wrong one being
deployed by simple typo-adjacent selection — not a deliberate choice. If
you're adding a variant of an existing model (different precision, longer
context, different tuning), make the distinguishing part of the filename
unambiguous. See `docs/USERMANUAL.md`'s "Adding a New Model" section and
`docs/ROADMAP.md`'s near-duplicate-key-detection entry for more.


DSpark on GB10-native image (hazyumps/deepseek-v4-flash-gb10)

Validated, 2026-08-29, real hardware: hazyumps/deepseek-v4-flash-gb10:sm121-cu130-20260727d (jasl PR #41834 fork, prebuilt for GB10/sm_121 aarch64) boots and serves DeepSeek-V4-Flash-0731 with DSpark. ~42-45 tok/s decode via benchmark.py (3-pass, temp=0), vs ~14 tok/s on stock eugr/spark-vllm-b12x. No --attention-backend/--moe-backend needed — auto-selects FlashInfer SM120 sparse-MLA decode + MARLIN MoE. --distributed-executor-backend ray works fine on this image too (no no-Ray workaround needed, despite one unrelated third-party repo needing that on a different stack). Config: max_num_seqs: 4, gpu_memory_utilization: 0.8, speculative_config: {method: dspark, num_speculative_tokens: 5, draft_sample_method: greedy}.

Known gap: first requests after boot pay a JIT tax — jit_monitor.py warnings for several kernels (eagle_prepare_next_token_padded_kernel, _dspark_markov_probs_*_kernel, etc.) compiling mid-inference rather than during warmup. Also missing a tuned FP8 kernel config for shape N=4096,K=12288 on NVIDIA_GB10 — falls back to default/sub-optimal. Both plausibly explain early-request throughput being lower than steady-state; not yet quantified separately.

Diagnostic note: don't trust loggers.py:310's periodic "Avg generation throughput" as a real perf number — it's a ~10s-window average diluted by idle/wait gaps within that window. Use benchmark.py's decode_tps (measured strictly first-token-to-last) for real comparisons.

512K context step validated (deepseek-v4-flash-0731-dspark-512k.yaml, max_num_seqs: 1) — boots and serves successfully on the same hazyumps image. Long-session OOM risk (see #7 above) not yet exercised at this context length — only a short benchmark run so far, not a multi-hour soak.

Shared-expert loader bug (tonyd2wild's DSPARK-SHARED-EXPERT-FIX.md) — checked, does NOT apply to this image. Third-party writeup documents a real vLLM bug where DSpark's draft weight loader drops 12 shared-expert tensors on the official 0731 checkpoint (silent, INFO-invisible, roughly halves decode/acceptance elsewhere: 25.7%→60.2% accept, 32.7→55.4 tok/s once patched). Traced our own image's loader (vllm/models/deepseek_v4/nvidia/dspark.py) line by line: it already carries the complete ("gate_up_proj","w1",0)/("gate_up_proj","w3",1) mapping tonyd2wild's patch adds, and the markov-tensor collision their patch carefully anchors around is structurally impossible here (markov tensors never get a layers. prefix, so they never reach the mapping loop at all). Confirmed via direct source trace, not inference — this build isn't affected. Our 38-46% draft acceptance is real but unexplained by this bug; likely just prompt-content-dependent (tonyd2wild's own patched numbers ranged 33-78% by content type).

Terminology trap: "NVFP4" means two unrelated things in this ecosystem. (1) NVFP4-quantized weights (e.g. auroter/DeepSeek-V4-Flash-0731-NVFP4) — a different checkpoint, orthogonal to DSpark. (2) nvfp4_ds_mla, an NVFP4 KV cache dtype — the thing already documented above as known-bad on stock vLLM for MLA models. Getting KV cache dtype working for real (as opposed to weight quantization) requires a heavily-patched third-party runtime (e.g. tonyd2wild's staged A/B/C build), not a flag on either of our current images. Its only benefit is KV pool size/context ceiling — confirmed zero effect on draft acceptance/speed.

### Gemma 4 31B dense (google/gemma-4-31B-it) on eugr/spark-vllm-b12x

Validated, 2026-08-29, real hardware, TP=2 across spark-3/spark-4:
`google/gemma-4-31B-it` (BF16 base) + `--quantization fp8` +
`--kv-cache-dtype fp8` + `--attention-backend TRITON_ATTN` +
`--distributed-executor-backend ray` + `VLLM_USE_V1=0`. 12.0 tok/s decode
(3-pass benchmark.py, temp=0.0), cold TTFT 0.99s, warm TTFT 0.11s — matches
independent community TP=2 figures (~11.2 tok/s) closely enough to treat
both flag choices as confirmed correct, not just non-crashing.

`--attention-backend TRITON_ATTN` is required, not optional: Gemma 4 has
heterogeneous head dimensions (256 local / 512 global) that the default
FlashInfer path doesn't handle correctly. Confirmed via
`Using AttentionBackendEnum.TRITON_ATTN backend.` appearing 3x across the
cluster in `docker logs vllm-head` — both TP ranks, both nodes.

`eugr/spark-vllm-b12x:latest` (no special tag needed) ships transformers
5.15.0 — well past the 5.5.0 floor `gemma4` architecture support needs.
Don't assume otherwise from eugr's `--tf5` build-flag naming; verify
directly with `docker run --rm <image> pip show transformers` rather than
inferring from tag conventions.

Benign startup noise, not a failure: `shm_broadcast.py:801 No available
shared memory broadcast block found in 60 seconds` logged twice during
boot (22:38–22:39), consistent with runtime FP8 quantization of the ~59GB
BF16 checkpoint still running when the queue polled. Did not recur once
serving started; benchmark came back clean. Would be worth investigating
if it ever recurs *during* active serving rather than only at boot.


UPDATE:: Using AttentionBackendEnum.TRITON_ATTN backend. repeated 3x across cluster, quantization=fp8 and kv_cache_dtype=fp8 in the engine config line, 12.0 tok/s decode at TP=2. That's the difference between "we set these flags" and "we confirmed vLLM resolved them" — which is exactly the standard your status: validated marker is meant to encode.

Env vars this build ignores (confirmed 2026-08-30). vLLM v0.1.dev20003+gad848fc41.d20260815 warns at boot about any VLLM_-prefixed variable it doesn't recognise. Three that were in this recipe were doing nothing and have been removed: VLLM_CPU_OMP_THREADS, VLLM_ENGINE_INITIALIZATION_TIMEOUT, VLLM_RPC_TIMEOUT. OMP_NUM_THREADS is not in that set and stays -- it's a real OpenMP variable, not a vLLM one.

VLLM_USE_V1=0 had no observable effect on the 2-node deploy: the engine logged Initializing a V1 LLM engine with the variable set. It has been removed from this recipe. See the general note below before applying that conclusion anywhere else -- Incident #1 mandates this variable for 2-node Ray topologies and was written from a real failure.

Validated single-node figures (2026-08-30): 6.7 tok/s decode, warm TTFT 0.17s, max_model_len: 32768, tp_size: 1. Matches the community single-node figure (~6.5 tok/s) as closely as the 2-node result matched theirs. Scaling 1-node -> 2-node is 1.79x (6.7 -> 12.0), consistent with the expected ~1.7x for TP=2 with inter-node all-reduce overhead. TTFT warms in stages over the first few requests (1.10s -> 0.30s -> 0.17s) and then holds at the floor across subsequent benchmark invocations, so the warmup is one-time per deploy, not per-invocation. Decode speed is completely insensitive to warmup state -- read TTFT and decode as separate signals.

NEW general note for the tuning reference
Unrecognised VLLM_* environment variables: two categories

vLLM logs WARNING [envs.py:2477] Unknown vLLM environment variable detected: X for any VLLM_-prefixed variable it doesn't know. Useful, but "unrecognised" does not mean "broken" and does not always mean "removable." Two distinct cases, and treating them the same makes the signal useless:

1. Recipe-authored -- actionable. Anything we put in a recipe's env_vars that this vLLM build doesn't consume. Confirmed inert on v0.1.dev20003+gad848fc41.d20260815: VLLM_CPU_OMP_THREADS, VLLM_ENGINE_INITIALIZATION_TIMEOUT, VLLM_RPC_TIMEOUT. Remove these -- but see the caution below.

2. Image-inherited -- not actionable. VLLM_BASE_DIR=/workspace/vllm is baked into eugr/spark-vllm-b12x as an image-level ENV (confirmed via docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}'). It appears in every container from that base and in every layer baked on top of it. No recipe change can remove it. Expect this warning forever on eugr-based images; it is not a defect and not worth investigating again. (It also independently corroborates /workspace/vllm as the image's working directory -- relevant to the mods bake work, see ROADMAP.md.)

Caution on removing category-1 variables. env_vars feeds compute_config_hash(), so deleting one resets the launch-success history for that recipe/topology. That's correct -- the launched configuration really did change -- but it means a bulk strip across the catalog would invalidate every recipe's validation history at once. Strip opportunistically when a recipe is being re-validated anyway.

VLLM_USE_V1=0 is a separate, unresolved case. vLLM recognises this variable (it never appears in the unknown-variable warnings), but on this build it appears to have no effect: the engine logged Initializing a V1 LLM engine with it set, on the 2-node Gemma 4 deploy and on subsequent runs. Incident #1 mandates it for all 2-node Ray cross-host topologies and was written from a real observed failure, so one contradicting data point is not grounds to delete the rule. Most likely this build has no V0 path left to fall back to, making the flag moot rather than wrong. Do not remove it from other 2-node recipes on the strength of this observation alone -- resolving it wants a deliberate A/B on a 2-node deploy. Flagged so the rule is neither trusted blindly nor dropped prematurely.
---

## Incident Log

### 1. Multi-Node Executor & Shared Memory Limits
* **Failure:** `AssertionError: collective_rpc should not be called on follower node` or workers hanging post-NCCL initialization.
* **Cause:** vLLM V1 `mp` (multiprocessing) backend relies on Linux shared memory (`/dev/shm`), which cannot cross physical network boundaries.
* **Rule:** 2-node topologies across physical hosts MUST pass `--distributed-executor-backend ray` and set `VLLM_USE_V1=0` in `env_vars` to force the V0 cross-host Ray executor.
* **Reproduced again, independently, 2026-08-31 (Task MD):** a from-scratch `_scratch-noop-test.yaml` (unrelated to whatever recipe originally triggered this incident) hit this exact signature purely from `vllm_args: ""` on `2_node`. The trigger doesn't have to be a deliberately wrong flag — an *absent* one is enough, since `use_ray` requires the Ray tokens to be explicitly present rather than defaulting to them if unset. See "Multi-node executor requirements" in the Recipe Tuning Reference above.

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

### 7. Silent Worker Death From Ray's Own Memory Monitor, Not a Driver Fault
* **Failure:** A worker container dies mid-session with no obvious error in `docker logs`; prior investigation on this exact symptom wrongly suspected a DeepGEMM/CUDA kernel bug from a compile warning logged right before the death.
* **Cause:** Ray's own node-memory monitor (`threshold_memory_monitor.cc`) OOM-kills a worker once host memory usage crosses ~95%, independent of GPU/VRAM headroom. On the GB10's unified LPDDR5x pool, a `gpu_util` set too high for the model/context size leaves too little headroom for JIT caches, page cache, and Ray/Python overhead over a multi-hour session — confirmed on the 1M-context DeepSeek recipe at `gpu_util: 0.82` (now lowered to `0.75`).
* **Rule:** Don't trust a compile warning near a crash timestamp as the cause without checking Ray's own logs first. This diagnosis is only possible at all because Ray's session directory (`/tmp/ray`) is now bind-mounted to a persistent, per-deploy host path — before that fix, a crashed worker's logs vanished the moment its container was torn down, which is exactly what left an earlier, likely-identical crash unconfirmed and permanently undiagnosable. If a worker dies with no persisted logs to check, that's the first gap to close, not a reason to guess.

### 8. Multi-Hour Dashboard Freeze From a Self-Deadlocked Tracker
* **Failure:** The dashboard shows a single frozen snapshot for hours — model status, telemetry, everything — with no error surfaced anywhere except container stdout. Restarting the daemon provides only temporary relief; the freeze recurs roughly an hour into the next real serving session.
* **Cause:** `SessionTracker`'s internal lock was a plain (non-reentrant) `threading.Lock()`. Its own periodic flush path re-acquires that same lock from the same thread once a session runs long enough (>1hr) — which a plain `Lock` cannot do, so the thread deadlocks itself permanently. Because status polling only ever keeps one computation in flight at a time, this single wedged thread freezes the entire dashboard, not just telemetry.
* **Rule:** Fixed by switching to `threading.RLock()`. If the dashboard ever looks frozen again with data that never changes across multiple polls, check `/api/status`'s `stale` / `stale_for_seconds` fields first — a value that's actually growing over minutes/hours (not just under a couple of seconds of normal poll latency) means the backend computation is genuinely stuck, not just slow, and is worth a `py-spy dump` against the live process rather than a guess.

## 9. Phantom "Link Detected" on Unconnected Second ConnectX-7 Port
Symptom: ethtool <second-port-iface> reports Link detected: yes and returns full module EEPROM data on both spark-3 and spark-4's second RDMA port, despite only one physical QSFP cable existing between the pair.
Cause: Driver/firmware returns stale/cached module data from whichever port initialized first — confirmed by identical vendor serial number reported on both ports. Not a real link.
Rule: Trust enp1s0f0np0 (the actually-cabled port, confirmed via matching real cable's serial). Don't chase this again if it resurfaces.

### 10. Fabricated / Nonexistent HF Repo Path (Gemma 4 31B FP8)
* **Failure:** `RepositoryNotFoundError: 401 Client Error... Repository Not
  Found` at config-creation time, before any model loading starts.
* **Cause:** `hf_path` pointed at `google/gemma-4-31B-it-FP8` — a repo that
  simply doesn't exist. Google only publishes `google/gemma-4-31B-it`
  (BF16); FP8 variants exist only under third-party namespaces
  (RedHatAI, prithivMLmods, vrfai, etc.), never under `google/`.
* **Compounding trap:** even a real third-party pre-quantized FP8 repo
  would have failed differently — `KeyError:
  'layers.0.mlp.down_proj.weight_scale'` in `gemma4.py` load_weights,
  a documented vLLM bug (#38912). The validated pattern for Gemma 4 is
  BF16 base + `--quantization fp8` (runtime dynamic quantization), not a
  pre-quantized checkpoint — opposite of the DeepSeek pattern above.
* **Rule:** Before writing an `hf_path`, confirm the repo actually exists
  under the stated org. For Gemma 4 specifically, always use the BF16
  base checkpoint with `--quantization fp8`, never a pre-quantized
  `-FP8` repo.

### 11. `exec: ray: not found` — Correct `--distributed-executor-backend ray` flag, image never shipped the binary
* **Failure:** `/opt/nvidia/nvidia_entrypoint.sh: line 55: exec: ray: not found`, before any Python or vLLM code runs at all.
* **Cause:** `--distributed-executor-backend ray` being present in `vllm_args` is necessary but not sufficient — the container's base image must also have `ray` installed. `nvcr.io/nvidia/vllm:26.07-py3` (this cluster's `default_image`) does not. Community precedent (`makiisthenes/dgx-spark-multinode-vllm-ray`) confirms newer NVIDIA NGC vLLM images dropped Ray by default and documents adding it back via a derived Dockerfile.
* **Rule:** Before assuming a 2-node recipe's only gap is the missing flag, confirm the target image actually has `ray` installed (`docker run <image> which ray`, or just watch the boot log for this exact error). `eugr/spark-vllm-b12x:latest` is confirmed to ship it; `default_image` is confirmed not to. A recipe needing both physical-host multi-node *and* an image without Ray has no working path today short of switching images or baking Ray in via the mods pipeline — see `ROADMAP.md`'s mods entry.

### 12. `File size mismatch` on HF download — genuinely corrupted upstream shards, not local network flakiness
* **Failure:** `RuntimeError: Task error: File size mismatch: expected N bytes but downloaded M bytes` from `huggingface_hub`'s Xet client, reproducibly the same exact byte counts across repeated attempts.
* **Cause:** Not a transient network or local-cache issue — confirmed via a documented HF discussion thread on the specific repo (`saricles/MiniMax-M2.7-NVFP4-GB10/discussions/3`) that three specific shards serve short of their reported `Content-Length` from HF's CDN itself, reproducible with `curl` and `git lfs pull` independently of any vLLM/Xet-specific code path.
* **Rule:** If the exact same expected/downloaded byte counts recur across attempts, stop retrying and check the repo's HF discussion tab before assuming it's our network, our cache, or our config — a deterministic short-by-the-same-amount result across attempts is the signature of a corrupted upstream file, not flakiness. Clearing the local cache does not help; every attempt re-fetches the same broken remote bytes.

### 13. MTP Speculative Decoding Rejects Pipeline Parallelism
* **Failure:** `NotImplementedError: Pipeline parallelism is not supported for this model. Supported models implement the SupportsPP interface.` — fires at `create_engine_config()`, before any weight load or GPU work starts.
* **Cause:** The MTP draft-model class (e.g. `Qwen3_5MTP`) doesn't implement `SupportsPP`, independent of whether the *target* model does. Any recipe combining `--speculative-config '{"method":"qwen3_next_mtp",...}'` (or any other MTP-family method) with `pp_size > 1` hits this — confirmed on `qwen-3.6-27b-nvfp4::2_node`, 2026-09-03.
* **Rule:** MTP speculative decoding on this cluster requires `tp_size` (or 1-node) — never pair it with `pp_size > 1`. Cheap to catch: it fails at config validation, before compile or weight load, so no real cluster time is lost finding out. Check any recipe carrying both an MTP `speculative-config` and a `pp_size > 1` topology before deploying it — see `TOMBSTONES.md` #104.

### 14. `spark-3`/`spark-4` Silently Drift to Different Cached Builds of the Same `:latest` Tag
* **Failure:** `vllm-worker` crashes on the worker host immediately after Ray starts, every single 2-node deploy attempt, regardless of recipe: `RuntimeError: Version mismatch: The cluster was started with: Ray: 2.58.0 ... This process ... was started with: Ray: 2.57.0`.
* **Cause:** `tests/ab_test.py`'s pre-pull step only ever ran `docker pull` on the head host, never the worker. `docker run` only auto-pulls when a tag is completely absent locally — a stale image already cached under the same `:latest` tag is never refreshed on its own. Once the upstream image moved, the head (always freshly pulled) and the worker (never touched by this step) silently ended up running genuinely different builds under the identical tag name, with no error until Ray's own head/worker version check caught it.
* **Rule:** If a 2-node deploy fails immediately after Ray starts on one side with a version-mismatch error, don't assume it's a recipe problem — check whether the two hosts' local image cache for that tag actually match (`docker run --rm <image> python3 -c "import ray; print(ray.__version__)"` on each host). Immediate fix: `docker pull <image>` by hand on whichever host is behind. `ab_test.py`'s pre-pull now targets every host a 2-node deploy will use, not just the head — see `TOMBSTONES.md` #106 — but this class of drift can still happen from any deploy path that only pulls on one side, including manual `docker pull` habits that only ever target the host someone happens to SSH into first.