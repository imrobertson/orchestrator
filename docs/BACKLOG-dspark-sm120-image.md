# DSpark speculative decoding for DeepSeek-V4-Flash-0731 on GB10/SM120

**Status:** Working, validated on real 2-node hardware, 2026-08-29. Not yet
folded into the standard recipe catalog / orchestrator flow as a permanent
entry.
**Owner:** unassigned

## Result

`hazyumps/deepseek-v4-flash-gb10:sm121-cu130-20260727d` (a GB10-native
prebuilt image built on jasl's `vllm-project/vllm` PR #41834 SM12x
enablement) boots DeepSeek-V4-Flash-0731 with DSpark on spark-3/spark-4 and
serves real traffic. Confirmed via `DSpark draft model loaded: 99 params`,
active Markov sampler, and per-request acceptance-rate metrics — not just a
clean boot.

**Benchmark (`benchmark.py`, 3-pass, temperature=0.0):**
- Cold start: 44.7 tok/s decode, TTFT 0.12s
- Warm avg: 42.7 tok/s decode, TTFT 0.13s

Compare to the ~14 tok/s baseline on the current production image
(`eugr/spark-vllm-b12x:latest`, no working spec-decode path) that motivated
this investigation — roughly **3x improvement**, in line with the 30-60
tok/s third-party reports that originally justified this work.

**Working recipe:** `deepseek-v4-flash-0731-dspark-gb10-hazyumps.yaml`
(`tp_size: 2`, `max_model_len: 393216`, `max_num_seqs: 4`,
`gpu_memory_utilization: 0.8`, `--distributed-executor-backend ray`, no
explicit `--attention-backend`/`--moe-backend` — auto-selects FlashInfer
SM120 sparse-MLA decode + MARLIN MoE). See `TROUBLESHOOTING.md`'s Recipe
Tuning Reference for the full validated config detail.

## Dead end, for the record

`orthozany/vllm-jasl-dsv4:pr41834-2026-05-13` (originally the lead
candidate) is x86_64-only — built for RTX PRO 6000 workstation cards, no
arm64 tag exists. Fails immediately on GB10 with `Exec format error`. Don't
revisit unless an arm64 build of that specific image appears.

## Open items

1. **JIT warmup gap.** Several kernels JIT-compile mid-inference on first
   real requests rather than during startup warmup
   (`eagle_prepare_next_token_padded_kernel`, `_dspark_markov_probs_*`,
   others) — vLLM's own `jit_monitor.py` flags each as a latency spike.
   Likely explains why the earliest post-boot throughput readings looked
   lower/noisier than the later `benchmark.py` numbers. Not yet quantified
   separately from warmed-up steady-state performance.
2. **Missing tuned FP8 kernel config.** `N=4096,K=12288` on `NVIDIA_GB10`
   has no shape-specific config file on this image; falls back to a
   generic/sub-optimal W8A8 block-FP8 kernel. Worth generating a tuned
   config (vLLM's kernel-tuning benchmark scripts) if this shape turns out
   to be hot in real traffic.
3. **Re-run benchmark under `probabilistic` sampling.** Tonight's numbers
   are greedy (temperature=0.0), which matches vLLM's default draft-accept
   criterion but not the recipe's actual `draft_sample_method` intent in
   other configs. `benchmark.py --temperature` already supports this.
4. **Fold into the permanent catalog.** Currently a standalone recipe file,
   not yet promoted to a `validated` status marker or made the default
   DSpark path. Decide whether this replaces
   `deepseek-v4-flash-0731-dspark-sm120.yaml` (the dead orthozany-based
   file) outright or coexists during a trial period.
5. **`benchmark_ledger.csv` key mismatch.** Tonight's run logged under key
   `deepseek-v4-flash-0731-1M`, not the recipe actually used — breaks
   `enrich_catalog()`'s historical_tps join for this recipe. Check what
   `--model-key` was passed (or wasn't) and confirm the fix before trusting
   ledger-driven historical stats for this recipe going forward.
