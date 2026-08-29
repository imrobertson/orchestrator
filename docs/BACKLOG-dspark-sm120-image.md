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

**Bumped to the top of the active priority list, 2026-08-28:** production
decode throughput on this recipe has been measured at ~14 tok/s, against
30-60 tok/s reported by third parties on comparable GB10/SM120 hardware
(see "What we found" below) — a gap wide enough that it's unlikely to be
explained by tuning alone, and consistent with running without any working
speculative decoding path at all. Worth confirming this is actually the
gap (rather than something else entirely) before assuming the DSpark work
below is guaranteed to close it, but it's the most concrete, best-understood
lever available right now and the only one with this much external
corroboration.

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


Update 2026-08-29: orthozany image is x86_64-only; found a real GB10-native path

Smoke test attempt result: orthozany/vllm-jasl-dsv4:pr41834-2026-05-13 fails immediately on spark-3/spark-4 with:

[FATAL tini (7)] exec /home/user/jasl-dsv4/.venv/bin/vllm failed: Exec format error

Confirmed via docker image inspect ... --format '{{.Architecture}}' → amd64. This image was built for RTX PRO 6000 Blackwell (workstation, x86_64), not Grace (arm64). No arm64 tag exists — checked Docker Hub; only pr41834-2026-05-13 and tmp-2026-05-05-snap, both same x86_64 lineage. The recipe smoke test never actually ran — this was purely an architecture mismatch, so we still have zero signal on whether jasl's PR #41834 kernel path itself works on our hardware.

Better path found: hazyumps/deepseek-v4-flash-gb10 (GitHub, Apache-2.0)

A maintained reproduction recipe built specifically for 2× GB10/DGX Spark (sm_121, aarch64) on top of the same jasl/vllm PR #41834 enablement. Not a prebuilt image — ships a documented build process, RoCE/NCCL tuning, and an sm_121-specific indexer patch (bf16 + fused Triton top-k; apparently sm_121 has no native lightning-indexer kernel). Reports going from "crashes/wedges/~12 tok/s" to "stable, 384K, ~31 tok/s single-stream decode, ~405 tok/s prefill @ 9k" on real dual-GB10 hardware — in our target 30-60 tok/s decode range.

Repo layout:

docs/BUILD.md — the image build (jasl/vllm fork, CUDA 13, arch 12.1a, NCCL 2.30.4)
docs/NETWORK.md — RoCE + RDMA passthrough + NCCL 2.30.4 (fixes a documented shm_broadcast deadlock/wedge)
scripts/start_head.sh / start_worker.sh — tuned launch (TP=2+EP, MTP n=2, 384K/0.80 mem-util, NCCL 2.30.4)
patches/sm12x_deep_gemm_fallbacks.py — the indexer fix, bind-mounted, no rebuild
verify/ — boot-watch, patch-correctness gate, prefill/decode probe scripts

Open question before adopting: repo's top-level README targets deepseek-ai/DeepSeek-V4-Flash generically with MTP (n=2) spec decode. A separate page within the same repo references the -0731 GA checkpoint specifically, noting GA moved spec-decode from a single MTP head to 3 DSpark draft groups + a markov head (--speculative-config method dspark, num_speculative_tokens: 5, draft_sample_method: greedy — matches what we're already running). Need to confirm which config path in the repo actually targets -0731/DSpark before treating the default Quickstart as DSpark-ready.

Secondary source, not yet evaluated: blog post at al-engr.com ("DeepSeek V4 Flash on Dual DGX Spark: What Broke, and the Recipe That Works") — independent PR #41834 + jasl-fork writeup with its own failure log. Worth a skim if the hazyumps build path hits problems; hazyumps is the more structured option and should be tried first.

Next steps:

Review hazyumps/deepseek-v4-flash-gb10 docs/BUILD.md and docs/TUNING.md in full, confirm the -0731/DSpark config path.
Build the image per their instructions (this is real effort — not a docker pull — budget accordingly).
Run the same smoke-test approach as before: small max_model_len/max_num_seqs, --enforce-eager kept initially, watch for the same failure signatures we've now catalogued (silent worker death during autotune, gloo barrier drops) in case this build has its own issues.
If it boots and generates coherently, this becomes the new candidate image for deepseek-v4-flash-0731-dspark-sm120.yaml, replacing orthozany/vllm-jasl-dsv4:pr41834-2026-05-13.