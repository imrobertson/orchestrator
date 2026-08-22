# Phase 2 Implementation Prompts

Retire `models.yaml` in favour of per-model `recipes/*.yaml`. Hand these to
an implementing model **one at a time, in order**, in a fresh conversation
each time.

Phase 2 is riskier than Phase 1. Phase 1 was mechanical — same data, new
home. Phase 2 changes the *format* of 16 models across 23 topology
combinations, and the thing consuming that data includes a browser UI whose
expectations are invisible from the Python side. The sequencing below exists
to make every step reversible.

**Prerequisite:** Phase 1 complete, `phase-1-complete` tag pushed, daily
deploys running clean on the new `common/` code for at least a few days.

---

## Two constraints that must survive the migration

Both were absent from the original plan; they were found by reading the
running code. Anyone implementing Phase 2 without knowing them will ship a
broken dashboard.

**1. `/api/catalog`'s response shape is load-bearing for the browser UI.**
`index.html` does:

```js
globalCatalog = data.catalog.models || {};
const topologies = globalCatalog[model]?.topologies || {};
if (topologies['1_node']) { ... }
if (topologies['2_node']) { ... }
```

So the recipe loader must reassemble a dict shaped exactly like today's
parsed `models.yaml` — `{"catalog": {"models": {<name>: {..., "topologies":
{"1_node": {...}}}}}}` — regardless of how recipes are stored on disk. The
recipe files are an authoring format; the in-memory shape is an API
contract. Do not change the latter in this phase.

**2. Two global offline-mode keys are injected at load time, not stored per
model.** `models.yaml` has top-level `GLOBAL_HF_HUB_OFFLINE` and
`GLOBAL_TRANSFORMERS_OFFLINE`. When either is `1`, `load_model_catalog()`
strips any matching `HF_HUB_OFFLINE=` / `TRANSFORMERS_OFFLINE=` entry from
**every** topology's `env_vars` and appends its own. These are cluster-wide
switches toggled by `/api/toggle-network`. They must move to
`cluster_config.yaml` and keep being injected by the loader — never copied
into 16 individual recipe files.

---

## Task 2A — Recipe schema, loader, and conversion script

> ### Context
>
> You are working on a Python control plane managing a two-node NVIDIA DGX
> Spark cluster. It runs off-node in Docker and reaches the Sparks over SSH.
> It is in daily production use.
>
> A previous refactor moved host/network/SSH config into
> `cluster_config.yaml`, loaded and validated by `common/config.py` using
> pydantic v2. Model configuration still lives in a single monolithic
> `models.yaml`.
>
> Attached: `models.yaml`, `common/config.py`, `cluster_config.yaml`,
> `dgx-orchestrator.py`, `index.html`.
>
> ### Goal
>
> Replace the monolithic `models.yaml` with one YAML file per model under
> `recipes/`, so a model can be added or edited without touching a shared
> file. This task builds the new format and loader **alongside** the old one.
> Nothing is switched over or deleted yet.
>
> ### Current format
>
> `models.yaml` has top-level keys `GLOBAL_HF_HUB_OFFLINE`,
> `GLOBAL_TRANSFORMERS_OFFLINE`, `default_image`, `hosts` (dead, ignore it),
> and `models`. Each entry under `models` looks like:
>
> ```yaml
>   deepseek-v4-flash:
>     hf_path: deepseek-ai/DeepSeek-V4-Flash
>     image: eugr/spark-vllm-b12x:latest     # optional
>     gpu_util: 0.82
>     topologies:
>       2_node:
>         max_model_len: 32768
>         tp_size: 2
>         pp_size: 1
>         env_vars:
>           - OMP_NUM_THREADS=16
>           - NCCL_CUMEM_ENABLE=0
>         vllm_args: >-
>           --attention-backend B12X_ATTN --trust-remote-code
> ```
>
> There are 16 models, 12 with a `1_node` topology and 11 with a `2_node`
> topology.
>
> ### Your task, part 1 — recipe format
>
> Define this per-model format, one file per model at
> `recipes/local/<model-name>.yaml`:
>
> ```yaml
> recipe_version: "1"
> name: deepseek-v4-flash
> hf_path: deepseek-ai/DeepSeek-V4-Flash
> image: eugr/spark-vllm-b12x:latest    # optional; omit to use cluster default
> gpu_util: 0.82
>
> capability:                            # optional, unused for now
>   task: null
>   context_class: null
>   latency_class: null
>
> mods: []                               # optional, unused for now
>
> topologies:
>   2_node:
>     cluster_only: true                 # optional
>     max_model_len: 32768
>     tp_size: 2
>     pp_size: 1
>     env_vars:
>       - OMP_NUM_THREADS=16
>     vllm_args: >-
>       --attention-backend B12X_ATTN --trust-remote-code
> ```
>
> `name` must equal the filename stem. Recipes live in either
> `recipes/local/` or `recipes/eugr/` (the latter for files synced from an
> upstream community repo; create the directory with a `.gitkeep`, leave it
> empty).
>
> ### Your task, part 2 — loader
>
> Add to `common/config.py` (or a new `common/recipes.py`, your call — say
> which and why):
>
> - Pydantic models `TopologyConfig`, `CapabilityConfig`, `RecipeConfig`.
> - `load_recipes() -> dict` that globs `recipes/local/*.yaml` and
>   `recipes/eugr/*.yaml`, validates each, and returns a dict keyed by model
>   name.
> - **A name collision between `local/` and `eugr/` is a hard error** naming
>   both file paths. Do not silently prefer one.
> - **An unrecognized `recipe_version` is a soft warning** printed to stderr;
>   the recipe still loads. Supported versions list: `["1"]`.
> - A recipe whose `name` disagrees with its filename stem is a hard error.
> - `functools.lru_cache` it, with an argument to bypass the cache for tests.
>
> ### Your task, part 3 — the critical compatibility function
>
> Write `build_catalog_response() -> dict` that returns a structure
> **byte-identical** to what `dgx-orchestrator.py`'s existing
> `load_model_catalog()` returns today:
>
> ```python
> {"catalog": {
>     "GLOBAL_HF_HUB_OFFLINE": <int>,
>     "GLOBAL_TRANSFORMERS_OFFLINE": <int>,
>     "default_image": <str>,
>     "models": {<name>: {"hf_path": ..., "gpu_util": ..., "image": ...,
>                          "topologies": {"1_node": {...}, "2_node": {...}}}}
> }}
> ```
>
> Read `index.html` to see why: the browser reads
> `data.catalog.models[m].topologies['1_node']` directly. Changing this shape
> breaks the dashboard.
>
> It must replicate the existing loader's env_vars injection exactly:
> - Ensure every topology has an `env_vars` key (default `[]`).
> - If the HF offline flag is 1: remove any existing `HF_HUB_OFFLINE=` entry,
>   then append `HF_HUB_OFFLINE=1`.
> - Same for `TRANSFORMERS_OFFLINE=` when its flag is 1.
> - Order matters — append after filtering, exactly as the current code does.
>
> Read the offline flags and `default_image` from `cluster_config.yaml`, not
> from any recipe. Add these three keys to `cluster_config.yaml`:
> `global_hf_hub_offline: 0`, `global_transformers_offline: 0` (and
> `default_image` is already there). Extend the `ClusterConfig` pydantic model
> accordingly.
>
> Also replicate the current failure mode: on any load error, return
> `{"error": <str>, "catalog": {"models": {}}}` rather than raising.
>
> ### Your task, part 4 — conversion script
>
> Write `tools/convert_models_yaml.py` that reads `models.yaml` and emits the
> 16 recipe files into `recipes/local/`. **Do not hand-write the recipe
> files** — a generated conversion is auditable and repeatable; 16 hand-typed
> files are 16 chances to fluff a `vllm_args` string.
>
> - Preserve `vllm_args` block scalars (`>-`) rather than collapsing them to
>   one long line. Use `yaml.dump` with appropriate style settings, or
>   construct the output manually.
> - Do not copy `default_image` or the two `GLOBAL_*` keys into recipes.
> - Emit `capability` with null values and `mods: []` for every model.
> - Print a summary: models converted, topologies per model, any skipped.
>
> ### Constraints
>
> - Do not modify `dgx-orchestrator.py`, `cache_cluster_assets.py`, or
>   `benchmark.py` in this task.
> - Do not delete or modify `models.yaml`.
> - No new dependencies. pydantic 2.13.4 and PyYAML 6.0.3 are available.
>
> ### Verification (part of the task)
>
> Write `tests/test_recipes.py`, runnable as `python3 tests/test_recipes.py`,
> asserting:
>
> 1. `load_recipes()` returns 16 models after conversion.
> 2. Total topology count across all recipes is 23 (12 `1_node`, 11 `2_node`).
> 3. A duplicate name across `local/` and `eugr/` raises, naming both paths.
> 4. An unknown `recipe_version` warns but still loads.
> 5. A filename/`name` mismatch raises.
> 6. With both offline flags 0, `build_catalog_response()` contains no
>    injected `HF_HUB_OFFLINE=` or `TRANSFORMERS_OFFLINE=` entries.
> 7. With both flags 1, every one of the 23 topologies has exactly one
>    `HF_HUB_OFFLINE=1` and one `TRANSFORMERS_OFFLINE=1` in `env_vars`.
> 8. A recipe declaring `HF_HUB_OFFLINE=0` in its own `env_vars` ends up with
>    only `HF_HUB_OFFLINE=1` when the flag is on — the filter-then-append
>    behavior, not a duplicate.
>
> Run the conversion, run the tests, and show the output.

**Your verification after 2A:**

```bash
python3 tools/convert_models_yaml.py
ls recipes/local/*.yaml | wc -l      # expect 16
python3 tests/test_recipes.py
git status --short                    # models.yaml must be UNMODIFIED
```

---

## Task 2B — Equivalence harness

This is the task that makes Phase 2 safe. It changes no production code.

> ### Context
>
> Same repo. `recipes/local/*.yaml` and a recipe loader with
> `build_catalog_response()` now exist alongside the still-live
> `models.yaml` and `load_model_catalog()`.
>
> Attach: `dgx-orchestrator.py`, `common/config.py` (or `common/recipes.py`),
> `models.yaml`.
>
> ### Your task
>
> Write `tools/verify_recipe_equivalence.py` that proves the new recipe path
> produces identical results to the old `models.yaml` path.
>
> It must compare two things:
>
> **1. Catalog structure.** Call the old `load_model_catalog()` and the new
> `build_catalog_response()` and deep-compare. Report any difference as a
> path-qualified diff, e.g.
> `models.qwen-2.5-coder-32b.topologies.1_node.env_vars: [...] != [...]`.
> Ignore key ordering within dicts; **do not** ignore list ordering, since
> `env_vars` order affects the generated `docker run` command.
>
> Run this comparison four times, once for each combination of the two
> offline flags (0/0, 1/0, 0/1, 1/1), so the injection logic is covered in
> every state rather than only the current one.
>
> **2. Rendered deploy commands.** For every model and every topology it
> declares (23 combinations), invoke the existing dry-run path:
>
> ```
> python3 dgx-orchestrator.py deploy --model <name> --nodes <n> --head spark-4 --dry-run
> ```
>
> once with the orchestrator reading the old catalog and once reading the
> new one, and compare the emitted `docker run` argument lists exactly.
>
> Since `dgx-orchestrator.py` is not yet switched over, drive this by
> monkeypatching its `load_model_catalog` symbol via `importlib` (the file
> has a hyphen in its name and cannot be imported normally). Do not modify
> `dgx-orchestrator.py` to make this easier.
>
> Exit 0 if everything matches, 1 otherwise, printing a clear summary:
> combinations checked, differences found, and the first three differences in
> full.
>
> ### Constraints
>
> - Read-only with respect to production code. No SSH, no cluster contact —
>   the dry-run path already guarantees this; do not add network calls.
> - Must be runnable repeatedly and produce identical output each time.

**Your verification after 2B:**

```bash
python3 tools/verify_recipe_equivalence.py && echo "EQUIVALENT"
```

If this doesn't pass, fix the recipes or the loader — not the harness — and
do not proceed to 2C.

---

## Task 2C — Switch the consumers over

> ### Context
>
> Same repo. Recipes and their loader exist and have been proven equivalent
> to `models.yaml` by `tools/verify_recipe_equivalence.py`.
>
> Attach: `dgx-orchestrator.py`, `cache_cluster_assets.py`,
> `common/config.py` (or `common/recipes.py`), `tools/verify_recipe_equivalence.py`.
>
> ### Your task
>
> Point both catalog consumers at the recipe loader, behind a flag that can
> switch back instantly.
>
> **`dgx-orchestrator.py`:**
> - Replace the body of `load_model_catalog()` so it calls
>   `build_catalog_response()` from the recipe loader.
> - Keep the function name, signature, and return shape exactly as they are.
>   Every existing call site (there are five, including the `/api/catalog`
>   route) must remain untouched.
> - Add an environment-variable escape hatch: if `USE_LEGACY_CATALOG=1` is
>   set, fall back to the original `models.yaml` parsing. Keep the old
>   implementation intact as a private function for this purpose. This is the
>   rollback lever during burn-in — it must work without a code change or
>   redeploy.
>
> **`cache_cluster_assets.py`:**
> - It currently parses `models.yaml` directly in `extract_manifest()`, with
>   its own `yaml.safe_load` and its own traversal, independent of
>   `dgx-orchestrator.py`'s loader.
> - Rewrite it to consume the recipe loader instead. Its per-model `image`
>   override logic is deliberate (documented in its docstring — different
>   models pull through different images) and must be preserved exactly,
>   including the speculative-decoding draft-model handling.
> - Honour the same `USE_LEGACY_CATALOG=1` escape hatch.
>
> ### Critical constraints
>
> - Pure swap. Zero behavior change is the acceptance criterion.
> - Do not delete `models.yaml` or the legacy parsing code in this task.
> - Do not change `/api/catalog`'s response shape. `index.html` depends on it.
> - Do not "improve" anything you touch along the way.
>
> ### Verification (part of the task)
>
> Report:
> 1. `python3 tools/verify_recipe_equivalence.py` still exits 0.
> 2. The same run with `USE_LEGACY_CATALOG=1` also exits 0.
> 3. `python3 -m py_compile` succeeds on both modified files.
> 4. Every call site of `load_model_catalog()` you confirmed unchanged.
> 5. A diff of `extract_manifest()` old vs new, with the image-override and
>    draft-model logic highlighted so it can be reviewed line by line.

**Your verification after 2C — do all of these:**

```bash
docker compose restart orchestrator-api
python3 smoke_test.py

# Dashboard check — the thing no Python test covers.
# Open the UI, confirm the model dropdown populates and that
# 1-node/2-node options appear correctly per model.

# Re-run the Phase 1 baseline diff one more time.
diff -r /tmp/phase1-baseline /tmp/phase2-after
```

Then a real 1-node deploy of a low-stakes model before daily use.

---

## Task 2D — Burn-in, then delete

**Do not run this task for at least one week of normal daily deploys after
2C**, with `USE_LEGACY_CATALOG` unset the whole time. The burn-in is the
point; there is no way to rush it and still have it mean anything.

Before starting, confirm: no unexplained deploy failures, dashboard model
list correct, `cache_cluster_assets.py` has run at least once successfully
against the new loader, and offline mode has been toggled at least once via
`/api/toggle-network` (this exercises the `GLOBAL_*` injection path, which
otherwise sits dormant).

> ### Context
>
> Same repo, one week after switching catalog consumers to `recipes/`. The
> `USE_LEGACY_CATALOG=1` fallback has not been needed.
>
> Attach: `dgx-orchestrator.py`, `cache_cluster_assets.py`, `models.yaml`.
>
> ### Your task
>
> 1. Delete `models.yaml`.
> 2. Delete the legacy parsing functions and the `USE_LEGACY_CATALOG` escape
>    hatch from both `dgx-orchestrator.py` and `cache_cluster_assets.py`.
> 3. Delete `MODELS_YAML_PATH` from both files and anywhere else it appears.
> 4. Delete `tools/convert_models_yaml.py` — it was single-use.
> 5. Keep `tools/verify_recipe_equivalence.py`, but strip the old-path
>    comparison, leaving only the "render every recipe's deploy command"
>    half. That half stays useful forever as a golden-command regression
>    check, and Phase 3 will want it.
> 6. Grep the whole repo — including `index.html`, `Dockerfile`,
>    `docker-compose.yml`, `dgx-config`, and `README-REVIEW.md` — for any
>    remaining reference to `models.yaml`. Report every hit and fix
>    documentation references.
>
> ### Constraints
>
> - Deletion only. No refactoring, no renames, no behavior changes.
>
> ### Verification
>
> Show `git diff --stat`, the full grep output for `models.yaml`, and
> confirmation that the surviving verification script still runs clean.

**Your verification after 2D:**

```bash
python3 tools/verify_recipe_equivalence.py    # golden-command half only
python3 tests/test_recipes.py
docker compose restart orchestrator-api
python3 smoke_test.py
git add -A && git commit -m "Phase 2: retire models.yaml in favour of recipes/"
git tag phase-2-complete
git push origin main --tags
```

---

## What to send back for review

Per task: the diff, the test output, and the equivalence-harness result. For
2C specifically, also say whether the dashboard dropdown still populates —
that's the one failure mode with no automated coverage, and the one most
likely to bite.

## A note on scope

Nothing in Phase 2 populates `capability` or `mods`. Both fields are
deliberately created empty. `capability` gets real values in Phase 4 when
the allocator can use them; `mods` gets its first entry whenever a model
actually needs a runtime patch. Creating the fields now costs nothing and
avoids a second schema migration — but resist the urge to fill them in
speculatively while converting.
