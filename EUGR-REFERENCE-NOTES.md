# eugr/spark-vllm-docker — Reference Notes

Companion note to `ARCHITECTURE-MIGRATION-PLAN.md`, but a different kind of
document: that plan is about *our* system; this one is a running reference
on a *third-party* repo we're borrowing recipes, mods, and design patterns
from. Update this as we review more of it — it's expected to stay partial.

## What it is, and why we care

`eugr/spark-vllm-docker` is a community-maintained toolkit for running vLLM
on DGX Spark hardware: a tuned build/image pipeline, a declarative recipe
system, and a runtime patch ("mods") mechanism for model-specific
compatibility fixes. We're not adopting it wholesale — see
`ARCHITECTURE-MIGRATION-PLAN.md` Phase 2 for why the execution model (SSH
peer-to-peer between the Sparks themselves) doesn't fit our off-node control
plane design. What we *are* borrowing: their recipe schema shape, the mods
pattern, and specific tested image tags/flags for models we deploy.

## Repo map (from the `filelisting.txt` snapshot, dated this conversation)

Reviewed in some depth already:
- `run-recipe.py`, `run-recipe.sh` — recipe loader/CLI, schema validation
- `launch-cluster.sh` — container lifecycle, mod application, on-node SSH orchestration
- `build-and-copy.sh` — image build/pull/tag/distribute
- `autodiscover.sh` — peer-scan cluster/network discovery
- `hf-download.sh` — model download + rsync distribution
- `Dockerfile`, `Dockerfile.mxfp4` — image build, confirmed `earlyoom` baked in
- `README.md`, `recipes/README.md` — public docs
- `AGENTS.md`, `docs/AGENT_DEVELOPMENT.md`, `docs/AGENT_RUNBOOK.md` — their own agent-instruction docs (the idea worth borrowing, not the content)
- `docs/NETWORKING.md` — mesh vs. switched topology, NCCL settings per topology
- Two real recipes: `recipes/deepseek-v4-flash-0731.yaml`, `recipes/diffusion-gemma-bf16.yaml`

Visible in the listing, **not yet reviewed** — see "If we pull more" below:
- `recipes/3x-spark-cluster/`, `4x-spark-cluster/`, `8x-spark-cluster/` — real N>2 topology examples
- `tests/expected_commands.sh` and the rest of `tests/` — possible golden-command regression pattern
- `.env.example` — canonical field reference for their config format
- `mods/drop-caches/`, `mods/gpu-mem-util-gb/` — directly relevant to our Phase 5 OOM-watchdog gap
- `mods/use-official-vllm/`, `mods/use-ngc-vllm/` — base-image-swap mods
- `docker/patch_vllm_*.py`, top-level `*.patch` files — build-time source patches; **not relevant to us** (see below), listed for completeness only
- `examples/*.sh`, `.github/workflows/test-recipes.yml` — lower priority

## Two distinct patch mechanisms — don't conflate them

- **`mods/<name>/run.sh`** — runtime, applied via `docker exec` after the
  container launches. This is what our `recipe.mods:` field maps to
  (confirmed against `launch-cluster.sh`'s `apply_mod_to_container()`).
- **`docker/patch_vllm_*.py` + top-level `*.patch`** — build-time, applied
  during `docker build` when compiling their own image from source. **We
  don't build our own images** (we pull prebuilt tags), so this mechanism
  has no equivalent in our system and nothing here needs porting.

## Confirmed against 5 real recipes (2026-08-18)

Five actual recipes pulled from the repo:
`deepseek-v4-flash-0731.yaml`, `nemotron-3_5-lightning.yaml`,
`diffusion-gemma-bf16.yaml`, `diffusion-gemma-nvfp4-thinking.yaml`,
`qwen3-coder-next-fp8.yaml`. Ran all five through our current
`common/recipes.py::load_recipes()` as-is (dropped verbatim into
`recipes/eugr/`, no translation). **All five fail** -- every one is missing
all three of our required fields (`hf_path`, `gpu_util`, `topologies`), and
every field they *do* carry (`model`, `container`, `defaults`, `env`,
`command`, `build_args`, `cluster_only`, `solo_only`, `description`) is one
our `RecipeConfig` doesn't know about, so pydantic v2's default behavior
silently drops all of it rather than erroring.

This is not a field-renaming problem. The two schemas represent the same
information in structurally incompatible shapes:

- **No `topologies` dict, ever.** There's a single flat `defaults:` dict
  (`tensor_parallel`, `gpu_memory_utilization`, `max_model_len`, etc.) and
  one `command:` block-scalar template with `{placeholder}` substitution
  (confirmed: `{{` really does escape to a literal `{` around the JSON
  blobs, e.g. `--speculative-config '{{"method":"dspark",...}}'` in
  `deepseek-v4-flash-0731.yaml` -- matches the `str.format(**params)`
  behavior noted below from reading `run-recipe.py`, now confirmed against
  real output). Node count isn't a schema field; per the earlier read of
  `launch-cluster.sh`, it's derived from what's actually in `command:`.
- **Confirmed: no recipe here uses pipeline parallelism at all.** All
  cross-node examples (`deepseek-v4-flash-0731`: `tensor_parallel: 2`,
  `nemotron-3_5-lightning`: `tensor_parallel: 2` via `-tp {tensor_parallel}`,
  `qwen3-coder-next-fp8`: same) use tensor parallelism across nodes, never
  `-pp`/`--pipeline-parallel-size`. Our own `recipes/local/deepseek-v4-flash`
  already independently chose `tp_size: 2, pp_size: 1` for its `2_node`
  topology -- consistent with eugr's choice for the same model family. But
  `recipes/local/qwen-2.5-coder-32b` and `llama-3.3-70b` chose the opposite
  (`pp_size: 2, tp_size: 1`) for theirs. **The TP-vs-PP choice is
  per-model, not a fixed convention on either side** -- a mechanical
  converter defaulting to one strategy would silently get some models
  wrong.
- **`cluster_only`/`solo_only` are whole-recipe booleans, not per-topology**,
  confirmed across all five (`deepseek-v4-flash-0731`: `cluster_only: true`
  alone; both diffusion-gemma variants: `solo_only: true` alone;
  `nemotron-3_5-lightning`: both explicitly `false`; `qwen3-coder-next-fp8`:
  neither present, plus a commented-out `#solo_only: true` referencing a
  tracked vLLM issue -- so "absent" and "both false" appear to mean the
  same thing: no constraint). This directly contradicts where our own
  draft schema puts it (`topologies.2_node.cluster_only`, see the
  migration plan's schema draft) and where `common/recipes.py` implements
  it today (nested in `TopologyConfig`). **Not resolving this now** --
  see `ARCHITECTURE-MIGRATION-PLAN.md`'s Open Decisions for why (our
  per-topology-explicit design makes the field look redundant at N=2, and
  the strongest case for it being genuinely useful is Phase 3's N>2
  generalization, which we haven't looked at examples for yet).
- **`container` (short logical name, e.g. `vllm-node-b12x`, `vllm-node`)
  is not our `image` (full registry ref, e.g. `eugr/spark-vllm-b12x:latest`).**
  It's an indirection into eugr's own build pipeline
  (`build-and-copy.sh`). Confirmed real deepseek-v4-flash-0731 uses
  `vllm-node-b12x` -- happens to be the same underlying image our existing
  `recipes/local/deepseek-v4-flash.yaml` already points at
  (`eugr/spark-vllm-b12x:latest`), but that's a manually-confirmed
  coincidence, not something derivable from the recipe file alone.
- **Command-template surgery risk, concretely, not hypothetically.**
  `deepseek-v4-flash-0731.yaml`'s `command:` already spells out
  `--tensor-parallel-size {tensor_parallel}` explicitly. Our orchestrator
  constructs that exact flag itself from `tp_size`. Naively dumping an
  eugr `command:` tail into our `vllm_args` would produce a duplicate
  `--tensor-parallel-size`, not a clean merge.
- **`build_args`** appears once (`deepseek-v4-flash-0731`: `--exp-b12x`),
  confirmed real, still no equivalent in our system (we don't build our
  own images) -- was previously "documented from the field table," now
  confirmed to actually appear in the wild.
- **Field-name mapping that IS mechanical:** `model` -> `hf_path`,
  `defaults.gpu_memory_utilization` -> `gpu_util`. That's the whole list.

**Bottom line: `recipes/eugr/` cannot be an automated byte-for-byte sync
target as currently scoped.** Every field-shape mismatch above needs a
judgment call (TP vs. PP, which image, which flags are already
orchestrator-supplied vs. genuinely extra) that a mechanical converter
can't safely make unattended. Translating eugr recipes into our schema is
real per-model work, not a sync script -- worth planning for as such
rather than assuming automation once someone gets to it.

## Confirmed mechanisms (recap, full detail in the migration plan)

| Thing | Confirmed behavior |
|---|---|
| Recipe schema | `recipe_version` (not `schema_version`), `name`, `container`, `command` required; `model`, `mods`, `defaults`, `env`, `build_args`, `cluster_only`, `solo_only` optional |
| Version mismatch | Soft warning, recipe still runs |
| `cluster_only`/`solo_only` mismatch | Hard error, actionable message with next-step commands |
| Node count | Derived downstream by parsing `-tp`/`-pp`/`-dp` out of the rendered command text — no explicit topology field on the recipe. We deliberately diverge here; see the design note in Phase 2 of the migration plan. |
| Template substitution | Plain Python `str.format(**params)` — `{{` escapes to literal `{`, explains the doubled braces around JSON blobs in real recipes like `deepseek-v4-flash-0731.yaml` |
| Execution model | Runs *from* one of the cluster nodes (plain local `docker run` for that node, SSH out to the rest) — confirmed at `launch-cluster.sh` line ~1097, not just inferred from docs |
| Image distribution | `docker pull` + local tag by default; `docker save \| ssh docker load` available for custom local builds without a registry |
| OOM protection | `earlyoom` baked into the image, invoked via a `launch-cluster.sh --earlyoom` flag with tuned thresholds |

## Borrow / adapt / skip

- **Borrow directly**: recipe field names (`recipe_version`, `cluster_only`, `solo_only`, `mods`), the mods directory+`run.sh` shape, the `cluster_only` error message shape, specific tested image tags for models we run (e.g. `eugr/spark-vllm-b12x` for DeepSeek V4 Flash).
- **Adapt**: mods execution — same copy-in-and-run idea, driven by our off-node SSH call instead of their local/SSH dual path. Recipe topology representation — we keep explicit `tp_size`/`pp_size` fields instead of deriving from parsed command text, since Phase 4's allocator needs to query them numerically.
- **Skip**: `launch-cluster.sh`/`autodiscover.sh` as our execution engine (peer-to-peer SSH between Sparks is the on-node brittleness we're deliberately avoiding); `docker save`/`load` distribution (not needed while we consume registry images); the full autodiscovery peer-scan as a live control-plane path (fine as a one-off setup helper if we rack more Sparks, not as part of the request path).

## Scale & maintenance-risk note

Now visible from the full listing: ~25 top-level recipes, 6 more nested
under `3x-`/`4x-`/`8x-spark-cluster/`, ~20 mod directories, largely
single-maintainer. Bigger and more actively maintained than it looked from
docs alone. Doesn't change the recommendation, just sharpens it — pin
whatever gets synced into `recipes/eugr/`, don't track their `main` live,
and treat this repo as a dependency with real bus-factor risk, not
infrastructure we can lean on staying stable indefinitely.

## If we pull more, priority order

1. **`recipes/4x-spark-cluster/*.yaml`, `recipes/8x-spark-cluster/glm-5.2-nvfp4.yaml`** — real N>2 examples, most directly useful once Phase 3 is actually in scope (hardware-gated, but cheap to review early).
2. **`tests/expected_commands.sh`** (+ `tests/test_recipes.sh`) — check whether this is a golden-command regression suite; if so, worth mirroring once our own `recipes/` exist.
3. **`.env.example`** — quick read, canonical field names for their config, mostly informational since we're not adopting `.env`.
4. **`mods/drop-caches/run.sh`, `mods/gpu-mem-util-gb/{run.sh,gpu_mem.patch}`** — directly relevant to the Phase 5 OOM-watchdog gap; may reveal a better fix than a static `gpu_util_ceiling` (e.g. specifying memory in absolute GB rather than a fraction, which fits GB10's shared memory model better).
5. **`mods/use-official-vllm/run.sh`, `mods/use-ngc-vllm/run.sh`** — relevant since we already default to NVIDIA's official image; may show a cleaner pattern for that than what we do today.

Not worth pulling: `docker/patch_vllm_*.py`, top-level `*.patch` files
(build-time, no equivalent in our system), `examples/*.sh` (nice-to-have
manual-launch references, low value), `.github/workflows/` (their CI, not
portable).
