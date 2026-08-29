# DSpark speculative decoding for DeepSeek-V4-Flash-0731 on GB10/SM120

**Status:** Working, validated on real 2-node hardware at two context sizes.
Promoted into the canonical recipe names. Catalog trim decision still open.
**Owner:** unassigned

## Result

`hazyumps/deepseek-v4-flash-gb10:sm121-cu130-20260727d` (GB10-native
prebuilt image, jasl's `vllm-project/vllm` PR #41834 SM12x enablement) boots
DeepSeek-V4-Flash-0731 with DSpark on spark-3/spark-4 and serves real
traffic — confirmed via draft model load, active Markov sampler, and
per-request acceptance metrics, not just a clean boot.

**Promoted recipes (canonical names, replacing the dead orthozany-based
`-sm120.yaml`):**
- `deepseek-v4-flash-0731-dspark.yaml` — 384K context, `max_num_seqs: 4`.
  Benchmark (`benchmark.py`, 3-pass, temp=0.0): cold 44.7 tok/s / TTFT
  0.12s, warm avg 42.7 tok/s / TTFT 0.13s. ~3x the ~14 tok/s baseline on
  stock `eugr/spark-vllm-b12x` (no working spec-decode path there).
- `deepseek-v4-flash-0731-dspark-512k.yaml` — 524288 context, `max_num_seqs: 1`
  (deliberately conservative — see KV pool math below). Boots and serves
  successfully; only a short benchmark run so far, not yet soaked over a
  long session.

Both auto-select FlashInfer SM120 sparse-MLA decode + MARLIN MoE with no
explicit `--attention-backend`/`--moe-backend`; `--distributed-executor-backend
ray` works fine on this image (no no-Ray workaround needed).

## Dead end, for the record

`orthozany/vllm-jasl-dsv4:pr41834-2026-05-13` — x86_64-only, no arm64 build
exists, fails immediately on GB10 with `Exec format error`. Don't revisit
unless an arm64 tag appears.

## Investigated and ruled out

**tonyd2wild's DSpark shared-expert loader bug** (real, well-documented,
+69% decode elsewhere: 25.7%→60.2% draft acceptance on the official 0731
checkpoint when fixed). Traced our image's actual loader source
(`vllm/models/deepseek_v4/nvidia/dspark.py`) line by line — it already
carries the complete shared-expert tensor mapping tonyd2wild's patch adds,
and the markov-tensor name collision their patch guards against can't occur
here due to a different code structure. **Confirmed not applicable to this
image**, not just assumed. Full reference doc saved as
`REFERENCE-dspark-shared-expert-fix.md`. Our 38-46% draft acceptance stands
unexplained by this specific bug — most likely just prompt-content
dependence (their own patched numbers ranged 33-78% by content type).

**NVFP4 KV cache (`nvfp4_ds_mla`) as a context-stretch lever** — researched
via tonyd2wild's separate, heavily-patched runtime (three staged Docker
builds, not a flag). Their own measurement: NVFP4 vs fp8 KV cache has **zero
effect on draft acceptance/speed** — its only benefit is KV pool size. Given
we already have comfortable pool headroom at 512K/`max_num_seqs:1` on plain
fp8, adopting this runtime is only worth it if pushing toward ~1M **with
real concurrency**, and it's a genuinely separate, heavier project (new
runtime lineage), not a recipe edit. Not pursued further for now.

## Open items

1. **Long-session validation for the 512K recipe.** Only a short benchmark
   run so far. Watch for the `TOMBSTONES.md` #7 failure mode (Ray's memory
   monitor OOM-killing a worker when unified-memory headroom runs out over
   a multi-hour session) before calling this production-ready.
2. **JIT warmup gap** — several kernels JIT-compile mid-inference on first
   real requests rather than during startup warmup. Likely explains
   lower/noisier early-request throughput vs. steady state. Not yet
   quantified separately.
3. **Missing tuned FP8 kernel config** for shape `N=4096,K=12288` on
   `NVIDIA_GB10` — falls back to generic/sub-optimal W8A8 block-FP8. Worth
   generating a tuned config if this shape proves hot in real traffic.
4. **Re-run benchmark under `probabilistic` sampling** and with a more
   structured/repetitive prompt (tonyd2wild's data: 78% acceptance on
   templated bulk generation vs 33% on prose) to see where our real
   acceptance ceiling sits, rather than one prompt shape.
5. **`benchmark_ledger.csv` key mismatch** — the validating run logged under
   `deepseek-v4-flash-0731-1M`, not the recipe actually used. Confirm
   `--model-key` is passed correctly on future runs against these recipes
   or `historical_tps` lookups won't join.
6. **Catalog trim — awaiting final confirmation.** Recommended cutting
   `deepseek-v4-flash-0731-b12x-nospec.yaml` (redundant with just using
   `1M.yaml` as the one non-DSpark fallback — `max_model_len` is a ceiling,
   not a reservation, so a 1M-capable instance should serve short requests
   fine) and `deepseek-v4-flash-0731-nvfp4.yaml` (unofficial third-party
   checkpoint, no DSpark support, unclear benefit over the official
   checkpoint which already auto-resolves MoE experts to fp4 with zero
   flags set). Net catalog: `dspark.yaml`, `dspark-512k.yaml`, `1M.yaml`.
   Not yet actually deleted — confirm before removing.


====== USEFUL LINKS ========
Actively in use / load-bearing:

hazyumps/deepseek-v4-flash-gb10 — https://github.com/hazyumps/deepseek-v4-flash-gb10 — the repo behind our working image. docs/TUNING.md and docs/BUILD.md are worth reading in full if you ever need to build rather than pull.
deepseek-ai/DeepSeek-V4-Flash-0731 — https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash-0731 — the checkpoint itself. Its Discussions tab (esp. #17) has real deployment chatter worth searching if something checkpoint-specific comes up.

Deep reference — saved locally, but the source is worth bookmarking too:

tonyd2wild/DeepSeek-v4-Flash-0731-DSpark-1M-NVFP4-KV-2x-DGX-Spark — https://github.com/tonyd2wild/DeepSeek-v4-Flash-0731-DSpark-1M-NVFP4-KV-2x-DGX-Spark — the single most information-dense source tonight. Already have DSPARK-SHARED-EXPERT-FIX.md saved as REFERENCE-dspark-shared-expert-fix.md — make sure that file rides along to the new chat too, it's not in TROUBLESHOOTING.md, just referenced from it.
Their RUNTIME-BAKEOFF-2026-07-29.md and OFFICIAL_MAIN_PORT_PLAN.md (in the same repo) — didn't pull these in full tonight, worth a read if you ever chase the nvfp4_ds_mla KV cache path or want the vLLM-main-vs-fork performance comparison.

Investigated, deliberately not pursued (context for why, if it comes up again):

drowzeys/Keys-Concurrency-Patch-for-DSpark-DeepSeek-V4-Flash — https://github.com/drowzeys/Keys-Concurrency-Patch-for-DSpark-DeepSeek-V4-Flash — relevant only if you push toward high concurrency + long context together.
vLLM PR #41834 (jasl's SM12x enablement, the fork our image is built on) and PR #46995 (DSpark, merged into vLLM main) — worth periodically re-checking whether GB10/SM120 support has landed upstream, obsoleting the fork dependency entirely.

Not evaluated, lower priority:

MiaAI-Lab/DeepSeek-v4-Flash-DSpark-2x-DGX-Spark — alternative image (ghcr.io/anemll/dspark-vllm-gx10), never tried.
al-engr.com blog post and the Level1Techs forum thread — secondary corroboration, skim only if something else breaks and you want a second data point.

Dead end — don't retry:

orthozany/vllm-jasl-dsv4:pr41834-2026-05-13 — x86_64 only, confirmed no arm64 build exists.