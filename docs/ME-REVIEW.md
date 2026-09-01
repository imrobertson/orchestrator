# Task ME — Gemma 4 26B-A4B NVFP4 mod — Review

## Status

**Complete — but the actual deliverable ended up almost entirely
different from what the task started as.** Task ME's original scope was
narrow: wrap a known-needed patch (`gemma4_patched.py`) for the community
`bg-digitalservices` NVFP4 checkpoint as a mod, matching MA-MD's
bake/resolve/deploy pipeline. That mod was built and is real — but
Sept-2026 research done at the start of this task found that the
checkpoint it targets is very possibly not the one worth deploying at
all, and the task grew into a full live-hardware investigation: which
checkpoint, which image, which speculative-decoding method, and (once
real numbers came back) which one for which workload. That investigation
is what actually consumed this task, produced two committed production
artifacts, found a real upstream vLLM bug, and is what this review
mostly documents.

## What was built

**`mods/gemma4-nvfp4/`** (`run.sh` + `gemma4_patched.py`) — the
originally-scoped mod. Vendored, verified (sha256-matched against the
installed file inside the derived image on real hardware), structurally
correct. **Not used in the final deploy path** — see "Contradictions"
below for why — but kept in the repo as a real, working artifact for the
one case it's actually for: deploying the community `bg-digitalservices`
checkpoint specifically, which nothing in this task's final scope
targeted.

**`tests/metest.py`** — a fire-and-forget staged smoke test that grew
from a single-mod verification script into the actual tool this whole
investigation ran on. Final capabilities, each added because something
real broke or something real needed answering, not speculatively:

- Three deploy paths (`baseline`, `mtp` — via the real recipe/CLI deploy
  path; `dflash` — via a raw `docker run` over SSH, since AEON's image
  needs an `--entrypoint` override the recipe schema has no field for)
- Automatic, unconditional log + full-transcript capture before any
  teardown (not gated behind `--keep` — evidence got lost twice in
  practice before this existed: once to a coworker needing the shared
  cluster, once to not thinking to scroll back)
- A health check that doesn't trust the first successful `/health` poll
  alone (re-confirms after a stabilization window — a crash landing
  seconds after first going healthy is a real, twice-reproduced failure
  mode)
- Named, reusable prompt presets (`PROMPT_PRESETS`) and a multi-prompt
  sweep against one deploy (`run_benchmark_suite()`) — exists because an
  identical config swung from 49 to 203 tok/s purely on prompt choice
- `--repeats N` with real statistical aggregation (mean/range per
  prompt across N independent fresh deploys) — not repeated calls
  against one running container
- `--image`/`--dflash-image` overrides, used to isolate a real upstream
  bug (see below)

**`recipes/local/gemma4-26b-a4b-nvfp4.yaml`** — real, committed
production recipe. NVIDIA's official checkpoint, MTP speculative
decoding, `num_speculative_tokens=2`, `image: eugr/spark-vllm:latest`
(mainline, not this cluster's usual `-b12x` — see Tombstone #89).
Values backed by 8 live runs (4× `n=2`, 4× `n=4`), not guessed.

**`deploy_gemma4_dflash.py`** — the `dflash` equivalent of a production
recipe. Can't be a `recipes/local/*.yaml` (same entrypoint-override
reason as `metest.py`'s `dflash` stage) — a normal recipe file would get
cataloged and silently misbehave the moment someone deployed it. Defaults
now reproduce the exact validated config: pinned image tag
(`2026-06-18-v0.23.0-dflashfix`, not AEON's moving `:latest`),
`gpu_util=0.65`, `num_speculative_tokens=10`. Docstring carries the full
validated performance table and states plainly when `mtp` is the better
choice instead.

**`BACKLOG-generalize-metest.md`** — not yet landed, scoped and ready.
Proposes replacing `metest.py`'s two hand-written deploy functions with
a `STAGE_SPECS` data table so adding a future model (DeepSeek-V4-Flash +
DSpark is the named next consumer) means adding a dict entry, not a new
function. Deliberately scoped as refactor-only, explicitly NOT to be
started mid-comparison — a lesson pulled directly from this task's own
experience of trusting numbers while still actively changing the code
that produced them.

## What was verified, and how

**`mods/gemma4-nvfp4`**: sha256 of the installed `gemma4.py` inside the
derived image matched the vendored payload exactly, on real hardware, via
`ensure_mods_baked()`'s normal bake path. Never exercised end-to-end
against a real deploy of the community checkpoint, since nothing in this
task's final scope used that checkpoint — the mod is proven at the
bake/install layer, not at the "does the community checkpoint actually
serve" layer.

**`baseline`** (NVIDIA official checkpoint, no mod, no speculative
decoding): 1 live run, 29.7 tok/s. Confirms the mod isn't needed for
this checkpoint — the entire premise the community-checkpoint mod was
built against doesn't apply here.

**`mtp`**: 8 live runs total on `eugr/spark-vllm:latest` — `n=2` (4
runs: 52.7/50.5/53.2/51.3, mean 51.9, range 2.7) vs `n=4` (4 runs:
49.1/50.2/54.8/47.8, mean 50.5, range 7.0). `n=2` chosen: comparable
mean, meaningfully tighter spread, and never triggers the "may result in
lower acceptance rate" multi-step warning `n=4` logs on every boot.
Additionally verified with real coding/extraction prompts, 4 repeats
each: coding mean 67.3 (range 1.5), extraction mean 72.9 (range 0.6) —
tighter than the original comparison runs, consistent with those prompts
producing more deterministic output.

**`dflash`**: the largest single finding of this task. First runs
(default prompt only) showed `dflash` roughly tied with `mtp` — which,
taken alone, would have made `dflash` look like a dead end not worth
pursuing. Chasing why AEON's own 144 tok/s claim didn't reproduce led
through two false leads (version drift, a missing env var) before
landing on the actual variable: **prompt choice**, not image version or
config. Confirmed with 4 prompts × 4 repeats each (16 independent fresh
deploys) on the pinned `v0.23.0-dflashfix` image:

| prompt | mtp mean (n=4) | dflash mean (n=4) | dflash range |
|---|---|---|---|
| default | ~52 (n=1) | 49.3 | 0.7 |
| coding | 67.3 | 103.4 | 0.8 |
| extraction | 72.9 | 202.8 | 1.2 |
| creative | 54.6 (n=1) | 54.2 | 0.6 |

Every `dflash` range under 1.3 tok/s across 4 independent boots — not a
noisy or lucky result. `dflash` decisively wins coding/extraction, ties
`mtp` on general prose.

## Contradictions and things the plan didn't specify

**The task's entire premise shifted underneath it, and that's the single
biggest finding here, not a footnote.** Task ME started as "wrap the
patch for the community checkpoint." Research at the very start of the
task found that checkpoint's whole reason for needing a patch (a vLLM
scale-key mapping bug) doesn't apply to NVIDIA's official checkpoint —
so the mod that got built is real and correct, but the investigation
that actually mattered went a completely different direction from what
was originally scoped. Worth surfacing plainly rather than writing a
review that pretends the original plan and the actual outcome were the
same thing.

**A real, reproducible upstream vLLM bug was found and worked around,
not just a config-tuning result.** `eugr/spark-vllm-b12x:latest` (this
cluster's usual image) crashes on the first real MTP request with a
`TypeError` in `Gemma4Proposer._greedy_sample()` — a base-class/subclass
signature mismatch, confirmed via full traceback capture. Mainline
`eugr/spark-vllm:latest` doesn't have this bug. This is now Tombstone
#89. Worth being deliberate about: this means `mtp` and `-b12x`'s own
MoE-kernel speedup can't currently be had together for this model — a
real, current limitation, not something this task attempted to fix
upstream.

**A second real bug was found in this task's own tooling, not the
cluster's core code**, and is now Tombstone #88: `metest.py`'s
scratch-recipe generator broke silently on any `vllm_args` containing
embedded JSON (i.e. any `--speculative-config`), because it used a
YAML double-quoted string instead of a block scalar. Surfaced as a
confusing "Model not defined in catalog" error with zero indication of
the actual YAML syntax problem. Reproduced directly against
`yaml.safe_load()` before shipping the fix.

**One thing worth flagging as unresolved, not swept under the "tied"
conclusion above**: `dflash`'s extraction number (202.8 tok/s, above
even AEON's own published ceiling) is real and reproducible — 4 runs,
1.2 tok/s range — but the leading explanation for *why* it's this high
(a short, highly predictable synthetic JSON-extraction prompt, close to
a best case for speculative-decoding acceptance rate) is a hypothesis,
not a confirmed root cause. Worth a harder, longer extraction prompt
before treating 202.8 as representative of extraction workloads
generally rather than of this one prompt specifically.

**Scope creep on `--repeats`/`--prompts` was real and worth naming.** An
early decision to test only `coding`/`extraction` (not the full preset
set) for cost reasons was later judged, correctly, as a mistake — those
extra prompts ran against an already-deployed container in the same
sweep, so the marginal cost of including them was small relative to the
deploy cost already being paid, and the decision to omit them was made
unilaterally rather than surfaced as a tradeoff. Corrected on the next
full sweep (`--prompts all`), but the first `mtp` repeat run never got
`default`/`creative` coverage and wasn't worth re-running purely to fix
that gap after the fact.

## Scope check

Originally scoped: mod-wrapping only. Actually delivered: mod-wrapping
(complete, unused in the end) + a full three-pipeline live-hardware
investigation + two production deployment artifacts + a generalization
backlog item + two new Tombstone entries. This is a much larger surface
than MA-MD's reviews cover, called out explicitly rather than writing a
review that only covers the originally-scoped 20% of what happened.

## Changed/created files, in full

- `mods/gemma4-nvfp4/run.sh`, `mods/gemma4-nvfp4/gemma4_patched.py` —
  new, complete, verified at the bake layer, unused in the final deploy
  path (see above).
- `tests/metest.py` — new, ~890 lines, iterated extensively across this
  task (see "What was built" for the full capability list).
- `recipes/local/gemma4-26b-a4b-nvfp4.yaml` — new, real production
  recipe, values backed by 8 live runs.
- `deploy_gemma4_dflash.py` — new, repo-root standalone deploy script
  (not a recipe, can't be one — see above), defaults match the 16-run
  validated config.
- `BACKLOG-generalize-metest.md` — new, not yet landed in the repo.
- `TOMBSTONES.md` — two new entries, #88 and #89, both landed in this
  review's own pass (numbering re-verified contiguous 27-89 after
  insertion, no duplicates or gaps).

`common/mods.py`, `common/recipes.py`, `common/ssh.py`,
`dgx-orchestrator.py`: **unchanged.** This task's mod-building portion
exercised MB/MC's existing machinery exactly as designed; the
speculative-decoding investigation used the existing CLI deploy path
(`mtp`/`baseline`) and a deliberately separate raw-`docker run` path
(`dflash`) rather than modifying the orchestrator's own deploy code.

## Recommended next steps

1. **`BACKLOG-generalize-metest.md`** — the natural next piece of work,
   given DeepSeek-V4-Flash/DSpark is sitting as a HIGH-priority backlog
   item that would directly benefit from `metest.py` no longer being
   Gemma4-specific. Deliberately not started as part of this task — see
   the backlog file's own explicit scoping.
2. **Extraction-number root cause** — flagged above as a real open
   question, not urgent, but worth resolving before citing 202.8 tok/s
   as a general "dflash is great at extraction" claim rather than a
   result tied to one specific synthetic prompt.
3. **`mods/gemma4-nvfp4` has no current consumer.** Not a problem to fix
   — it's correct and kept intentionally — but worth knowing it exists
   for exactly one scenario (deploying the community `bg-digitalservices`
   checkpoint) that nothing currently in this cluster's recipes targets.
4. **The `-b12x` fork's `Gemma4Proposer` bug (Tombstone #89) is worth
   reporting upstream** (to whichever of eugr's repo or lukealonso's
   fork actually owns that file) — this task worked around it, it didn't
   fix it, and the workaround (stay on mainline for MTP) means this
   cluster can't currently get both `-b12x`'s kernel speedup and MTP
   speculative decoding for this model at the same time.
