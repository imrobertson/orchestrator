# [HIGH PRIORITY] Native DSpark speculative decoding for DeepSeek-V4-Flash-0731 on GB10/SM120

**Status:** Not started — evaluation complete, no image pulled or tested yet.
**Owner:** unassigned
**Depends on:** none (can run alongside the existing `eugr/spark-vllm-b12x` image as a second `image:` entry)

## Why

Both DeepSeek-V4-Flash-0731 recipes (`deepseek-v4-flash-0731-nvfp4.yaml`,
`deepseek-v4-flash-0731-1M.yaml`) currently run without speculative decoding.
This is correct and necessary on the current `eugr/spark-vllm-b12x:latest`
image — DSpark (the only spec-decode method 0731 ships; it has no MTP head)
has no working serving path on stock SM120 wheels, confirmed directly by the
NVFP4 checkpoint author's own model card. Running with it enabled either
crashes (wrong method) or silently produces near-zero draft acceptance
(generic fallback routing) — see incident writeups below.

DSpark's real speedup on this model class is substantial when it works
correctly — 2-4x decode throughput in third-party benchmarks — so this is
worth pursuing as its own project rather than leaving on the table
permanently.

## What we found

The out-of-tree SM12x DeepSeek-V4 support work is real, active, and
**genuinely corroborated** — not a single blogger's claim:

- **`jasl/vllm`, branch `ds4-sm120`** — an actual open PR against
  `vllm-project/vllm` (**PR #41834**), not just a personal fork. Adds the
  SM120/SM121 fallback + tuning stack DeepSeek V4 needs on client/workstation
  Blackwell.
- Independently confirmed working by at least four unrelated parties:
  - A vLLM issue tracking DeepGEMM SM12.x kernel gaps was tested specifically
    on **dual GB10 (DGX Spark, SM121, aarch64)** — our exact hardware class,
    not just RTX Pro 6000 workstation cards.
  - A separate user built and ran it on 8x RTX PRO 6000 (40-50 tok/s decode,
    up to 2000 tok/s prefill) and published a derived Docker image.
  - A third, unrelated poster confirmed it running on 2x RTX Pro 6000 with
    MTP enabled (~60 tok/s peak).
  - `hermia-ai`'s "stock vLLM, honest limits" writeup — which documents
    exactly what's broken on stock SM120 — cites this PR as the fix-in-flight
    for those same limits.
- **jasl's own test-harness repo explicitly recommends our exact topology
  fix**: *"On two-node GB10, use TP=2 PP=1 as the default DeepSeek V4
  bring-up shape."* — independent confirmation, from the person doing the
  kernel work, specific to two-node GB10. (We already applied this to
  `deepseek-v4-flash-0731-1M.yaml`.)

## Prebuilt images found (candidates, none pulled/tested yet)

| Image | Built from | Notes |
|---|---|---|
| `orthozany/vllm-jasl-dsv4:pr41834-2026-05-13` | `jasl/vllm@codex/ds4-sm120-min-enable` (PR #41834 head, 2026-05-12) + `jasl/DeepGEMM:sm120` | **Best provenance** — exact commit pinned, documented build. Tested/tuned for the **preview** checkpoint (`deepseek-ai/DeepSeek-V4-Flash`) with **MTP**, not confirmed against 0731 + DSpark. Known issue: scheduling falls over at concurrency ≥3 with long context + MTP=2 on `--max-num-seqs=4`. |
| `ununnilium/vllm-ds4-sm120:20260618-0` | `jasl/vllm@ds4-sm120-preview-dev` | Built by an independent user for their own repro; less rigorously documented than orthozany's tag. Same underlying fork lineage. |
| `lucifer1004/dsv4-flash-sm120` | Own build against SM120, runtime-only (no source worktrees) | Different lineage from jasl's fork — worth a separate evaluation, not yet cross-checked against DSpark-on-0731 support. |

**None of these are confirmed working with `deepseek-ai/DeepSeek-V4-Flash-0731`
specifically + DSpark specifically.** The PR's kernel enablement is
architecture-level (should be checkpoint-agnostic), but every concrete,
reported success we found used either the preview checkpoint + MTP, or
didn't specify. This is the first thing to verify empirically, not assume.

## Caveats going in

- **No single-node smoke test is possible for this checkpoint.** The full
  `deepseek-ai/DeepSeek-V4-Flash-0731` checkpoint is ~167GB; a single GB10
  has 128GB unified memory. It cannot fit on one node regardless of context
  length -- this is the same constraint vLLM's own recipe page cites as the
  reason TP=2 is mandatory for this model on GB10 at all. (An earlier version
  of this plan suggested a single-Spark test first; that was wrong and is
  corrected here.)
- All of this is **pre-merge, explicitly preview/dev-branch work** by its own
  maintainer's standards — jasl's own harness notes mark MTP as
  "exploratory" and `think-high` as "allowed-failure." Treat it accordingly:
  not a production swap-in.
- **Different kernel lineage from our current image.** `eugr/spark-vllm-b12x`
  uses its own `B12X_ATTN`/`b12x` custom kernel stack; jasl's fork carries a
  separate SM12x Triton/CuTeDSL kernel path. Our existing `b12x`-specific
  tuning (moe-backend flags, mods, JIT cache assumptions) likely won't carry
  over — this is a genuinely separate image/recipe, not a patch on the
  current one.
- Branch names have moved during development (`ds4-sm120`,
  `ds4-sm120-preview`, `ds4-sm120-preview-dev`, `codex/ds4-sm120-min-enable`)
  — check the PR and `jasl/vllm-ds4-sm120-harness` for current state before
  committing to a specific prebuilt tag, since some of the above may already
  be stale relative to the PR head.

## Next steps

1. **Pull `orthozany/vllm-jasl-dsv4:pr41834-2026-05-13`.** There is no cheaper
   single-node smoke test available: the real checkpoint (~167GB) doesn't
   fit on one GB10 (128GB unified memory) regardless of context length, so
   this has to boot 2-node from the start. A smoke-test recipe with minimal
   context/concurrency is ready to go:
   `recipes/local/deepseek-v4-flash-0731-dspark-sm120.yaml` — deliberately
   small (`max_model_len: 32768`, `max_num_seqs: 1`) purely to get a fast
   yes/no on whether DSpark loads and generates coherent, correctly-accepted
   output on this image/checkpoint combination, before spending a full
   1M-context cold-start cycle on it.
2. If that boots and DSpark actually accepts tokens (not just loads), scale
   `max_model_len`/`max_num_seqs` up incrementally from there rather than
   jumping straight to the 1M profile.
3. If it doesn't boot cleanly, the recipe's comments flag the two most likely
   first things to try: dropping `num_speculative_tokens` from 7 to match
   whatever this checkpoint's actual `dspark_block_size` turns out to be
   (third-party reports on a related but different DSpark checkpoint build
   saw crashes when these don't match), and confirming whether leaving
   `--attention-backend` unset actually does auto-select the sparse MLA path
   on this image as third-party notes for this fork claim -- neither of
   these is verified by us directly yet.
4. If DSpark doesn't load cleanly on 0731 with this image at all, check
   `jasl/vllm-ds4-sm120-harness`'s `docs/vllm_correctness_gates.md` and the
   PR's current review thread for whether 0731 + DSpark support has since
   landed on a newer branch head than what this prebuilt tag was cut from.
5. Cross-check `lucifer1004/dsv4-flash-sm120` as a second candidate if the
   jasl-lineage image doesn't pan out — different kernel lineage, not yet
   evaluated here.
6. Benchmark against the current no-spec-decode baseline
   (`deepseek-v4-flash-0731-nvfp4.yaml`) using `benchmark.py --temperature`
   before/after, same methodology as the earlier acceptance-rate debugging.
