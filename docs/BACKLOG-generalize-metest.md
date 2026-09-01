# BACKLOG: Generalize tests/metest.py into a reusable tuning harness

**STATUS: DONE (2026-09-01).** Landed in two passes -- see "Resolution"
below for what actually shipped, where it deviated from the shape
sketched here, and what's still explicitly out of scope. This document
is kept as the historical record of the original ask; treat the
Resolution section as authoritative over the rest of the file where they
disagree.

**Priority:** MEDIUM-HIGH -- not urgent, but the DSpark/DeepSeek-V4-Flash
backlog item (BACKLOG-dspark-sm120-image.md, HIGH) is the next natural
consumer of this, so doing the generalization now pays for itself almost
immediately rather than sitting as speculative future-proofing.

**Origin:** Built while chasing Gemma 4 26B-A4B NVFP4's MTP-vs-DFlash
throughput comparison (2026-08-31/09-01). What started as a one-model
smoke test accumulated several genuinely reusable, hard-won pieces along
the way -- this backlog item is about extracting those into something
that isn't hardcoded to Gemma 4.

## What's already generic (no rewrite needed, just needs to move)

- `wait_for_health()`'s stabilization re-check -- doesn't trust a single
  successful `/health` poll, catches a crash landing seconds after first
  going green. Born from a real, reproduced incident (identical config
  surfaced as an HTTP 500 on one run, flat connection-refused on the
  next).
- `save_container_logs()` + the `_Tee`-based full-transcript capture --
  both exist because evidence got lost twice in practice (once to a
  coworker needing the shared cluster mid-run, once to not thinking to
  scroll back before the terminal output was gone). Model-agnostic by
  construction.
- `run_real_benchmark()`'s integration with this repo's own
  `benchmark.py` (real 3-pass cold/warm methodology, not a hand-rolled
  measurement) -- already takes `model_key`/`max_tokens`/`prompt` as
  plain arguments.
- `resolve_prompts()` / `PROMPT_PRESETS` / `run_benchmark_suite()` -- the
  multi-prompt sweep against a single already-deployed container, no
  redeploy between prompts. Exists because of a measured, non-obvious
  finding: an identical dflash config swung from 49.5 to 103.8 tok/s
  warm purely from prompt choice (technical-overview vs. coding task).
  This finding likely generalizes to other speculative-decoding setups,
  which is exactly why it's worth not having to rediscover it per model.
- The pre-pull-before-`docker run` fix (explicit `docker pull` with a
  long timeout ahead of the actual launch, so a never-before-cached
  image doesn't blow through a timeout sized for "launch something
  already local"). Hit for real, twice, against two different images.
- The `ContainerRole.STANDALONE` naming trick that lets `dgx-config
  teardown` clean up a raw `docker run` deploy that never went through
  `_execute_deployment_impl()` at all (needed for any image with a
  non-default ENTRYPOINT, which DFlash's AEON image isn't the only one
  of).

## What's hardcoded to Gemma 4 / this one pipeline pair today

- `NVIDIA_HF_PATH`, `MTP_ASSISTANT`, `DFLASH_HF_PATH`, `DFLASH_DRAFTER`,
  `BASE_VLLM_ARGS` -- all literal constants, not configuration.
- `--stage` is a fixed three-way enum (`baseline`/`mtp`/`dflash`), not an
  arbitrary named config.
- Two separate, hand-written deploy functions
  (`run_stage1()` going through `write_scratch_recipe()` + the real CLI
  deploy path; `run_dflash_stage()` building a raw `docker run` directly
  over SSH for the entrypoint-override case) that don't share a common
  data-driven definition of what a "stage" actually is.

Using this harness for a different model today means editing the source,
not passing new flags -- that's the actual gap between "worked great for
one model" and "general tuning harness."

## Proposed shape

Replace the two hardcoded deploy functions with one data table plus one
deploy function that branches on a single field:

```python
STAGE_SPECS = {
    "gemma4-baseline": dict(
        hf_path=NVIDIA_HF_PATH, image=EUGR_IMAGE, vllm_args=BASE_VLLM_ARGS,
        uses_recipe_path=True,
    ),
    "gemma4-mtp": dict(
        hf_path=NVIDIA_HF_PATH, image=EUGR_IMAGE,
        vllm_args_template=lambda args: f"{BASE_VLLM_ARGS} --speculative-config '{...}'",
        uses_recipe_path=True,
    ),
    "gemma4-dflash": dict(
        hf_path=DFLASH_HF_PATH, image=DFLASH_IMAGE, docker_env=[...],
        uses_recipe_path=False, entrypoint_override="vllm",
    ),
    # future: "deepseek-dspark-baseline", "deepseek-dspark-sm120-tuned", ...
}
```

`--stage` (or rename to `--config` once it's no longer a fixed 3-way
enum) selects a key. The reusable infrastructure listed above stays
exactly as-is underneath -- this is a refactor of the deploy-construction
layer only, not a rewrite of anything that's already been debugged.

## Explicitly NOT in scope for this item

- Don't touch anything in the "already generic" list above beyond moving
  it as-is -- those pieces are debugged and proven; the refactor's job is
  isolating the model-specific literals, not re-verifying working code.
- Don't start this mid-comparison on a live tuning question (e.g. not
  while the Gemma 4 MTP-vs-DFlash prompt sweep is still in flight) --
  restructuring deploy logic while trusting its numbers for an active
  decision risks introducing a bug into results someone's about to act
  on. Do it as its own dedicated pass once the current comparison is
  settled.

## Resolution (2026-09-01)

Landed in two dedicated passes, both after the Gemma 4 MTP-vs-DFlash
prompt sweep this backlog item was explicitly deferred behind actually
settled (Task ME closed out same day, `ME-REVIEW.md` written) -- the
sequencing constraint above was honored, not skipped.

**Pass 1 -- the STAGE_SPECS refactor, as sketched above.** Replaced
`run_stage1()`/`run_dflash_stage()` with a `STAGE_SPECS` data table plus
one `run_stage()` that branches on `uses_recipe_path`, exactly the shape
proposed in this document. Verified via a standalone before/after
construction-diff harness (recipe YAML for the recipe-path stages,
`docker_cmd` argv for the raw-docker stage) rather than a visual
read-through -- all three original stages' construction matched the
pre-refactor code byte-for-byte / argv-for-argv.

**Pass 2 -- generalized well past the original sketch, then renamed.**
The proposed shape above assumed `--stage` would become an arbitrary
*named config* selecting into a fixed table. What actually shipped goes
further: each side (`--variant-a`/`--variant-b`) auto-detects into one of
four shapes -- a `KNOWN_PRESETS` bundle (the original three Gemma4
stages, preserved), an **existing catalog recipe deployed exactly as-is**
(no scratch file at all, full mods pipeline, shares its `historical_tps`
ledger entry with a normal dashboard deploy -- not something the original
sketch anticipated), that same preset/recipe with specific fields
overridden on top, or a fully ad-hoc / raw-docker-run variant. `--stage`
is gone; `--variant-a`/`--variant-b` (optional independently, so a single
recipe can be profiled alone or two compared head-to-head) replaced it.
The script was renamed `tests/metest.py` -> `tests/ab_test.py` to match
what it actually does now.

This pass also caught a real regression via the same before/after
construction-diff verification method Pass 1 established: generalizing
the `dflash` preset into a reusable builder silently reordered several
`docker run` flags (functionally inert, but not the byte-for-byte parity
the verification standard requires) -- caught by the diff harness before
it ever reached real hardware, not by review. See `TOMBSTONES.md` #90.

**What's still explicitly NOT done, deliberately:**
- No DeepSeek-V4-Flash / DSpark `STAGE_SPECS`/`KNOWN_PRESETS` entry --
  same reasoning as this document's original "Explicitly NOT in scope"
  section: that model's tuned recipe values aren't finalized. It's now
  the natural next real user of `--variant-a`/`--variant-b` (as either
  an existing catalog recipe once one exists, or an ad-hoc variant via
  `--a-hf-path`/`--a-vllm-args` in the meantime) rather than a hardcoded
  preset -- no further generalization work is needed to support it.
- No live-hardware run of the generalized tool yet -- verification so far
  is construction-layer only (argv/YAML diffing against stubs), per this
  document's own "Explicitly NOT in scope" list not extending to
  re-verifying already-proven pieces, but a real run against the cluster
  is still worth doing before trusting a comparison's numbers.
- 2-node support exists only for the pure named-recipe-passthrough path
  (forwarded straight to the real CLI's own topology handling); ad-hoc
  scratch recipes and the raw-docker path remain 1-node-only, same as
  before this generalization.

See `tests/AB_TEST_USAGE.md` for the full usage reference, and
`USERMANUAL.md` / `UsageShortcut.md` for where this is now cross-linked
from the cluster's main docs.
