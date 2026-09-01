## Task: Refactor `tests/metest.py` to a data-driven `STAGE_SPECS` table

### Context

`tests/metest.py` is a fire-and-forget smoke test / benchmark harness for a
DGX Spark cluster orchestrator (`dgx-orchestrator.py`, repo root
`~/docker/orchestrator`). It currently deploys and benchmarks three
hardcoded configurations of one model (Gemma 4 26B-A4B NVFP4):

- `baseline` — NVIDIA's official checkpoint, no speculative decoding
- `mtp` — same checkpoint + native MTP speculative decoding
- `dflash` — a different (uncensored) checkpoint + AEON-7's DFlash
  speculative decoding, via a different image entirely

It's invoked like:
```bash
docker exec -it dgx-orchestrator-api python3 tests/metest.py --stage mtp --image eugr/spark-vllm:latest --prompts coding,extraction --repeats 4
```

**The task:** replace the two hardcoded deploy functions
(`run_stage1()` for `baseline`/`mtp`, `run_dflash_stage()` for `dflash`)
with a single data table (`STAGE_SPECS`) plus one unified deploy function
that branches on a per-entry field, so adding a new model/engine
combination in the future means adding a dict entry, not writing a new
function. **DeepSeek-V4-Flash + DSpark speculative decoding is the next
real consumer of this** (tracked separately, HIGH priority, in
`BACKLOG-dspark-sm120-image.md`) — but that model's actual recipe values
aren't finalized yet, so **this task is scoped to the refactor only**:
reproduce the three existing stages' exact current behavior through the
new data-driven structure. Do not add DeepSeek/DSpark support as part of
this task.

### Files to attach

Required:
- `tests/metest.py` (the file being refactored — attach the current version)

Strongly recommended, for correctly understanding the deploy path this
script wraps:
- `dgx-orchestrator.py` (specifically `_execute_deployment_impl()` — the
  real deploy path `write_scratch_recipe()`+`deploy_via_recipe()` calls
  into via the CLI)
- `common/recipes.py` (the recipe schema `write_scratch_recipe()`
  generates YAML against — `RecipeConfig`, `mods` field, `MODS_DIR`,
  `load_recipes()`)
- `common/config.py`, `common/constants.py`, `common/ssh.py` (imported
  by `metest.py`; `ContainerRole.STANDALONE` matters specifically — see
  below)
- `cluster_config.yaml` (host inventory, `gpu_util_ceiling`, `ports`,
  `tuning.deploy_wait_timeout_sec`)

### What must NOT regress — each of these was a real bug, found and
fixed on live hardware, not theoretical

1. **YAML block-scalar quoting for `vllm_args`** (`write_scratch_recipe()`).
   `vllm_args` is written as a YAML block scalar (`>-`), NOT a
   double-quoted string. A double-quoted version broke silently the
   instant `vllm_args` contained an embedded `--speculative-config`
   JSON value with its own double quotes — the recipe file failed to
   parse, and the failure surfaced as a confusing "Model not defined in
   catalog" error with no indication of the actual YAML syntax error.
   Confirmed via a direct `yaml.safe_load()` repro before shipping the
   fix. Any new code path that writes `vllm_args` into a recipe YAML
   must preserve this.

2. **`wait_for_health()`'s stabilization re-check.** Doesn't trust the
   first successful `/health` poll alone — waits `stabilize_sec` (15s
   default) and re-checks before declaring ready. Exists because the
   identical config produced an HTTP 500 mid-request on one run and a
   flat connection-refused on the next — a crash landing a few seconds
   after `/health` first turns green, at a slightly different point
   relative to whatever request happened to be in flight.

3. **Pre-pull before `docker run`, with a long timeout, separate from
   the actual launch call.** `docker run` pulls a missing image inline,
   synchronously, before doing anything else. A timeout sized for
   "launch an already-cached container" (60-90s) is not sized for
   "first-time pull of a multi-GB image, then launch." This bit
   `baseline`/`mtp` for real against `eugr/spark-vllm:latest` (never
   pulled to this cluster before) even though it had already been fixed
   for `dflash` first — i.e. this needs to apply uniformly across every
   stage in the new structure, not be re-derived per stage.

4. **`save_container_logs()` + the `_Tee`-based full-transcript capture,
   called automatically, unconditionally, before any teardown.** Not
   gated behind `--keep`, not dependent on a human reacting fast enough
   to grab evidence before a shared cluster gets reused by someone else
   (this happened for real — a coworker needed the hardware and torn-down
   container logs were gone before they could be pulled by hand).
   Captured twice per stage when relevant: once right after health
   settles, once again if the benchmark itself fails (a crash triggered
   by the benchmark request would postdate the first snapshot).

5. **`ContainerRole.STANDALONE`'s literal container name used for the
   `dflash`-style raw `docker run` path**, specifically so
   `dgx-config teardown` (which greps for that exact name) still finds
   and removes a container that was launched OUTSIDE the normal
   recipe/CLI deploy path.

6. **The `--entrypoint` override problem is structural, not a flag.**
   `_execute_deployment_impl()` always runs
   `python3 -m vllm.entrypoints.openai.api_server` against an image's
   DEFAULT entrypoint. AEON's `dflash` image ships `ENTRYPOINT bash` and
   needs `--entrypoint vllm ... serve <path>` — there is no field in the
   recipe schema for this. This is *why* `run_dflash_stage()` bypasses
   `write_scratch_recipe()`/the CLI deploy path entirely and builds a raw
   `docker run` over SSH instead. **Any future stage whose image needs a
   non-default entrypoint has the same problem** — the new unified
   deploy function needs a clean way to express "this one goes through
   the real recipe/CLI path" vs. "this one needs a raw docker run,"
   without silently trying to force every stage through the recipe path
   and breaking on this exact issue again.

7. **`_extract_last_json_object()`'s JSON-boundary detection.**
   `dgx-orchestrator.py`'s CLI subcommands print
   `json.dumps(result, indent=2)` as their last action, but earlier
   calls (e.g. `common/ssh.py`'s `get_hf_token()` warning path) may
   already have printed plain-text lines to the same stdout. The
   detection relies on `json.dumps(..., indent=2)` always starting with
   a line that is exactly `{` and nothing else.

8. **No `--wait` passed to the CLI deploy call; health is polled
   independently instead.** `_execute_deployment_impl()`'s own
   `wait=True` path calls `wait_for_cluster_ready()` but never checks
   its result before returning `{"status": "success", ...}` — a
   container that launched fine and never became healthy still reports
   success. Passing `--wait` AND polling independently would also double
   the worst-case wait time for no benefit.

9. **`benchmark.py` integration via `run_real_benchmark()`** — shells out
   to the repo's own `benchmark.py` (3-pass: cold + 2 warm, `decode_tps`)
   exactly the way `_run_benchmark_worker()` does, rather than
   reimplementing throughput measurement. Regex-parses `"Warm Avg (Runs
   2+)"` / `"Cold Start (Run 1)"` lines from its stdout.

### Current structure to generalize (see actual file for full code)

- `write_scratch_recipe(stage, hf_path, image, gpu_util, max_model_len, vllm_args)`
  — writes `recipes/local/_scratch-gemma4-nvfp4-{stage}.yaml`, deleted
  after teardown unless `--keep`. Recipe-path stages only.
- `deploy_via_recipe(stage, recipe_name, host, ip, port, wait_timeout)`
  — shells to `dgx-orchestrator.py cli deploy --model <name> --nodes 1
  --head <host>` (no `--wait`, see point 8), parses the JSON response,
  then independently polls health.
- `run_stage1(stage, args, cfg, host, ip, user)` — the `baseline`/`mtp`
  deploy function. Computes `vllm_args`/`image`/`gpu_util` inline from
  module-level constants + CLI args, pre-pulls, calls
  `write_scratch_recipe()` → `deploy_via_recipe()`, then
  `check_boot_log()` → `run_benchmark_suite()`, tears down in `finally`.
- `run_dflash_stage(args, cfg, host, ip, user)` — the `dflash` deploy
  function. Builds `vllm_serve_args`/`docker_env`/`docker_cmd` inline
  (raw `docker run` with `--entrypoint vllm`), pre-pulls, launches via
  `run_ssh()` directly, polls health independently, same
  `check_boot_log()`/`run_benchmark_suite()`/teardown pattern.
- Both are called from `_run()`'s stage loop (`--stage
  {baseline,mtp,dflash,all}`), which also supports `--repeats N` (each
  repeat a fully independent deploy+benchmark+teardown, aggregated at
  the end into mean/range per prompt) and `--prompts` (named presets
  from `PROMPT_PRESETS`, run against ONE deploy per repeat, not
  redeployed per prompt).

### Proposed shape (from `BACKLOG-generalize-metest.md` — read that file
if attached, it has more detail)

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
}
```

`--stage` (or rename to `--config`/keep `--stage` with a data-driven
`choices` list built from `STAGE_SPECS.keys()` — your call) selects a
key. One deploy function branches on `uses_recipe_path`. Everything in
the "must not regress" list above stays exactly as-is underneath —
this is a refactor of the deploy-construction layer only.

### How to verify the refactor is correct (do this, don't just eyeball it)

The safest verification is a **byte-for-byte / argv-for-argv comparison**
against the current, known-working code, not just "does it run":

1. For the recipe-path stages (`baseline`, `mtp`): generate the recipe
   YAML both ways (old code path vs. new `STAGE_SPECS`-driven path) for
   the same inputs, and diff them. They should be identical.
2. For the raw-docker-run stage (`dflash`): construct the `docker_cmd`
   list both ways for the same inputs, and diff them (order and content
   of every `-e` flag, the entrypoint override, the volume mount, the
   full `vllm_serve_args` list). They should be identical.
3. Only after (1) and (2) pass should this be tested against real
   hardware — and even then, treat it as confirming the refactor didn't
   change behavior, not as the primary verification method (a live run
   costs real cluster time and a subtle argv difference might not
   surface as a visible failure, just quietly different flags).

### Explicitly out of scope for this task

- Adding DeepSeek-V4-Flash / DSpark as a new `STAGE_SPECS` entry — that
  model's actual tuned recipe values aren't finalized; bolting a
  half-guessed entry onto this refactor conflates two different tasks
  and risks shipping unverified DeepSeek config alongside a
  verified-safe Gemma4 refactor.
- Touching anything in the "must not regress" list beyond moving it —
  those are debugged and proven; this task is isolating the
  model-specific literals, not re-verifying working code.
- Changing `--repeats`/`--prompts`/`PROMPT_PRESETS` behavior — orthogonal
  to this refactor, already working, already verified.
