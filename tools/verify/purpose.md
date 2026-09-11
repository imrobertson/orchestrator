# tools/verify/ — the evidence behind the tombstones

## What these are

Each harness here was written to prove one specific fix worked, during the
session that made it. They are the evidence behind a `TOMBSTONES.md` entry —
what turns "I changed it and it looked right" into "I asserted the property
and it held."

They are **not** a test suite. Nothing runs them on a schedule and they do
not cover the codebase. `tests/` is the test suite; this is the evidence
locker.

## Why they are committed

Most were not, until 2026-09-09. The comb of `TOMBSTONES.md` that day found
several scripts cited as the proof of a claim and present nowhere in the
repository. The claims read as sound; the evidence behind them was
re-runnable by nobody. That is the concrete cost of what `WORKSTREAMS.md`
§5 already names — the build-it-both-ways-and-diff pattern works, is applied
by hand per task, and is then thrown away.

Two were already committed, in `tools/` rather than here, and finding them
sharpened the point rather than blunting it: **one of the two could not run
where it sat.** `verify_pending_launch.py` resolved its target as
`Path(__file__).parent / "dgx-orchestrator.py"`, which in `tools/` means
`tools/dgx-orchestrator.py` — a file that has never existed. It was
committed from the scratch directory it was written in, without the one
edit that would have made it runnable, and nothing noticed because nothing
ran it. Being in the repo was never the property that mattered; being
runnable from where it lives is.

## Status, verified 2026-09-09

All eight were re-run from this directory against the current tree.

| Harness | Supports | Status |
|---|---|---|
| `verify_entrypoint_schema.py` | #133, schema 2→3→4→5 | **PASSES** (repaired — see below) |
| `verify_gpu_ceiling.py` | #139 | **PASSES** |
| `verify_loaded_model_parse.py` | #140, #141 | **PASSES** |
| `verify_secret_masking.py` | #86, #94, #138 | **PASSES** |
| `verify_pending_launch.py` | #127–#131, reserved-host guard, 423 mapping | **PASSES** (was unrunnable in `tools/`; repathed) |
| `verify_gemma4_recipe.py` | #133, the AEON DFlash recipe | **PASSES** — re-run 2026-09-10; reproduces the validated AEON argv flag for flag |
| `verify_glm_recipe.py` | #133, E019, E020 | **PASSES, AND THAT IS THE PROBLEM** — see below |
| `verify_recipe_equivalence.py` | Phase 2 Task 2C migration | **OBSOLETE — do not revive.** See its banner |

### What had to be repaired

**`verify_entrypoint_schema.py` was failing four assertions**, all from the
`_CONFIG_HASH_SCHEMA` 4→5 bump: three stale literal `== 4` checks and an
`expected_keys` set that predated `launch_argv_prefix`. Every *substantive*
assertion still passed — the None-vs-`""` distinction, the hash separation,
and the sabotage check that proves the harness is live. Repaired, and two
sections added while it was open:

- **[10]** `launch_argv_prefix` is hashed and order-significant. It caused
  the 4→5 bump and nothing asserted it.
- **[11]** `gpu_util_ceiling_exempt` is **excluded** from the hash. The
  exclusion is dated 2026-09-08 in `compute_config_hash()`'s docstring and
  rests on the field never reaching `docker run`. Asserted here so it cannot
  be reversed silently — the `mods` exclusion rested on the same premise and
  went stale without anyone noticing.

### Path rewiring

All six originally resolved paths from `Path(__file__).parent`, expecting a
flat scratch directory holding copies of `recipes.py`, `dgx-orchestrator.py`
and the recipe YAML under test. Moving them here broke all six. They now go
through `_repo.py`, which resolves the real tree.

That is worth more than an import fix. The scratch-copy habit is what let
the authoring copy of the GLM recipe drift from the repo copy — caught only
because a check in that session's patch script happened to assert against
the committed file. These harnesses now read what is actually committed.

## The one that must not be revived

`verify_recipe_equivalence.py` proved the `recipes/` path matched the
`models.yaml` path at the moment Phase 2 switched over. Both of its inputs
are gone — `models.yaml` was deleted by #112 step 6, and `MODELS_YAML_PATH`
has zero occurrences in `dgx-orchestrator.py`.

It is kept for provenance and banner-marked, not deleted, because the
failure mode if someone "fixes" it is silent: it captures
`old_load_model_catalog = mod.load_model_catalog`, which now resolves to
the **new** implementation. Restore a `models.yaml` and it compares the new
path against itself and prints EQUIVALENT. A green run proving nothing —
#83's exact shape. Write a fresh harness against both real implementations
if a future migration needs this proof.

## The caveat on `verify_glm_recipe.py`

**CONFIRMED 2026-09-10, not suspected.** `glm-5_3-flash-nvfp4-mtp.yaml`
line 203 sets `launch_argv_prefix: ["vllm", "serve", "{model}"]`. This
harness exits 0. **A green run from it currently means nothing.**

Its filename reference is updated (`_glm-5.3-flash-nvfp4-tp2` →
`glm-5_3-flash-nvfp4-mtp`), but **its argv reconstruction is wrong in a way
that matters.** It hardcodes

    python3 -m vllm.entrypoints.openai.api_server --model <path>

as the container argv. TOMBSTONES #140 added `launch_argv_prefix` precisely
because that module path cannot honour `--headless`, and the GLM recipe was
switched to `["vllm", "serve", "{model}"]`. If the recipe now sets that
field, this harness is reconstructing an argv the orchestrator no longer
emits, and its assertions would pass against a shape that is not deployed.

**Read the recipe before trusting a pass here.** Fixing it means teaching
the reconstruction about `launch_argv_prefix` and the `{model}` substitution
that suppresses the separate `--model` flag — which is exactly what
`verify_loaded_model_parse.py` section [3] already covers from the parsing
side.

## Other drift to expect

These were written against the state of the world on a specific day. Known
movement since, all real:

- **Host roles swapped.** `cluster_config.yaml`'s `hosts:` order reversed
  between 2026-09-06 and 2026-09-08 — `spark-3` is now head and reserved,
  `spark-4` is the scratch node. `verify_glm_recipe.py` hardcodes
  `HEAD = "spark-3"` / `10.0.14.41`, which happens to be correct *now*.
- **Recipes renamed and deleted.** Three DeepSeek recipes went in the
  2026-08-29 catalog trim.
- **Image tags float.** `eugr/spark-vllm-b12x:latest` moved ~480 dev
  revisions in three weeks (errata E015).
- **`recipes/eugr/`, `models.yaml` and `USE_LEGACY_CATALOG` retired**
  2026-09-09.

Do not treat a failure here as a regression without reading the harness
first. Most assert argv shape or pure-function behaviour rather than
touching hardware, which is why four of six still pass untouched.

## Adding to this directory

Name it `verify_<thing>.py`. Put the tombstone number it supports in a
docstring on line 1. Use `_repo.py` for every path. Assert the property,
not the absence of a crash — #83 is what happens when two broken checks
agree and produce a PASS, and #94 is what happens when you assert a marker
appeared instead of asserting the secret was gone. Where practical, include
a **sabotage section** that deliberately reintroduces the bug and confirms
the harness catches it; `verify_entrypoint_schema.py` [7] and
`verify_secret_masking.py` [6] both do this, and it is the only thing that
distinguishes a live harness from one that passes because it tests nothing.

## Not here

`clean_ledger.py` is cited by #111 and #122 but is a one-time ledger
maintenance tool, not a verification harness. It has not been recovered and
does not belong in this directory if it is.
