# tests/ab_test.py -- usage manual

Deploy, health-check, boot-log-scan, and benchmark one or two model
variants back to back, then print (and optionally compare) the results.
Fire-and-forget: run it, walk away, read the summary.

```bash
docker exec -it dgx-orchestrator-api python3 tests/ab_test.py \
    --variant-a <recipe-or-preset> --variant-b <recipe-or-preset>
```

`--variant-b` is optional -- give only `--variant-a` to profile a single
recipe. Give both to run the identical sweep against each and get an "A
vs B" table at the end.

## The three ways to specify a variant

Each side (`--variant-a`/`--variant-b`, or `--a-*`/`--b-*` overrides) is
auto-detected into one of three shapes:

| You give... | What happens |
|---|---|
| A name matching an existing `recipes/local/*.yaml` or `recipes/eugr/*.yaml` recipe, **no** `--a-*` flags | Deployed exactly as-is via the real CLI (`cli deploy --model <name>`) -- no scratch file, full mods pipeline, shares that recipe's `historical_tps` ledger entry with a normal dashboard deploy. |
| A recipe/preset name **plus** `--a-*` override flags | That recipe/preset used as a base, named fields overridden, written out as a throwaway scratch recipe. |
| No name, just `--a-hf-path` (+ others) | Fully ad-hoc, built from scratch. `--a-hf-path` is required here. |
| `--a-entrypoint` (with any of the above) | Forces the raw `docker run` path -- for images whose default entrypoint isn't the stock vLLM API server. Requires `--a-image` and `--a-serve-args`. Ignores `--a-vllm-args`/`--a-mods`. |

## Examples

**Profile one existing catalog recipe, as-is:**
```bash
python3 tests/ab_test.py --variant-a gemma4-26b-a4b-nvfp4
```

**Same recipe, but try a different gpu_util:**
```bash
python3 tests/ab_test.py --variant-a gemma4-26b-a4b-nvfp4 --a-gpu-util 0.7
```

**Compare two existing catalog recipes head-to-head:**
```bash
python3 tests/ab_test.py \
    --variant-a gemma4-26b-a4b-nvfp4 \
    --variant-b deepseek-v4-flash-0731-dspark-sm120
```

**Fully ad-hoc, no recipe file involved:**
```bash
python3 tests/ab_test.py --variant-a my-test \
    --a-hf-path nvidia/Some-New-Model --a-image eugr/spark-vllm-b12x:latest \
    --a-gpu-util 0.75 --a-vllm-args "--quantization modelopt --trust-remote-code"
```

**Raw docker-run for a non-standard entrypoint image:**
```bash
python3 tests/ab_test.py --variant-a aeon-dflash \
    --a-image ghcr.io/aeon-7/aeon-vllm-ultimate:latest --a-entrypoint vllm \
    --a-serve-args "serve AEON-7/Gemma-4-26B-A4B-it-Uncensored-NVFP4 --port 8000 --tensor-parallel-size 1"
```

**Built-in Gemma4 presets** (carried over from this script's original
purpose -- `gemma4-baseline`, `gemma4-mtp`, `gemma4-dflash`):
```bash
python3 tests/ab_test.py --variant-a gemma4-mtp --variant-b gemma4-dflash --prompts all --repeats 3
```

## Useful global flags

| Flag | Effect |
|---|---|
| `--prompts coding,extraction` / `--prompts all` | Run the benchmark against multiple named prompt presets (`default`, `coding`, `extraction`, `creative`) against the same deployed container -- no redeploy between prompts. |
| `--prompt "..."` | One-off custom prompt, overrides `--prompts`. |
| `--repeats N` | Fully independent deploy+benchmark+teardown, N times per variant. Prints mean/range per prompt at the end. |
| `--keep` | Skip teardown; leave the container (and scratch recipe file, if any) on disk for follow-up poking. |
| `--host spark-3` | Which host to deploy to. Defaults to `cluster_config.yaml`'s `default_deploy_target`, falling back to the cluster's primary host if that isn't set. **Refuses outright if the named host is marked `reserved:`** -- see below. |
| `--max-tokens` | `max_tokens` passed to `benchmark.py`. |
| `--wait-timeout` | Seconds to wait for `/health`. Defaults to `cluster_config.yaml`'s `tuning.deploy_wait_timeout_sec`. |

## Per-side override flags (`--a-*` / `--b-*`)

`--{a,b}-hf-path`, `--{a,b}-image`, `--{a,b}-gpu-util`,
`--{a,b}-max-model-len`, `--{a,b}-vllm-args`, `--{a,b}-mods` (comma-
separated, recipe-path only), `--{a,b}-nodes` (1 or 2 -- **only** valid
for a pure named-recipe passthrough with a `2_node` topology; anything
scratch or raw-docker is always 1-node), `--{a,b}-entrypoint`,
`--{a,b}-serve-args`, `--{a,b}-docker-env` (repeatable `KEY=VAL`, raw-
docker only).

## Which host this lands on, and what it tears down

Both changed as of the reserved-host work (TOMBSTONES #127/#128), and
both matter if the cluster is running a resident service on one node
while you test on the other.

- **`--host` defaults to the scratch node, not the primary.** It resolves
  to `cluster_config.yaml`'s `default_deploy_target`. That key exists
  because the first host under `hosts:` is simultaneously the Ray head
  node, the telemetry-authoritative serving host, and -- previously --
  the fallback target for anything unqualified. A bare `ab_test.py` used
  to deploy straight over whatever that node was running. If
  `default_deploy_target` is unset the old behaviour is unchanged.

- **A `reserved:` host is refused, and there is no `--force` here.** If
  `--host` names a host marked `reserved: true`, the run exits 2 before
  deploying anything. That is deliberate: forcing work onto a reserved
  host should be an explicit `dgx-orchestrator.py deploy --force`, not a
  flag that quietly survives in a saved benchmark invocation. Pass
  `--host` explicitly to pick a different node.

- **Teardown is scoped to the host this run deployed to.** It no longer
  clears the whole cluster between variants or at the end. A run on one
  Spark leaves the other Spark's containers alone. (`cli teardown` itself
  now requires an explicit `--host` or `--all` -- there is no
  argument-free whole-cluster default any more.)

- **2-node variants still span both hosts.** `--a-nodes 2` deploys to the
  primary *and* secondary regardless of `--host`, so it will take down a
  resident service on either. If one of them is reserved, the deploy is
  refused; run it deliberately from the orchestrator with `--force` when
  you actually want that.

## Things worth knowing before you run it

- **No entrypoint override in the recipe schema.** Any image whose
  default entrypoint isn't the stock vLLM API server *must* go through
  `--a-entrypoint` (raw docker run). This is a structural limit of the
  deploy path, not something this script can paper over.
- **Mods only apply on the recipe path.** `--a-mods` and a catalog
  recipe's own `mods:` list are silently inert in raw-docker mode --
  that path never touches `_execute_deployment_impl()`'s mod-baking
  pipeline at all.
- **Raw-docker variants are invisible to the reserved-host guard in the
  orchestrator.** They shell a `docker run` straight at the host over
  SSH, so the orchestrator never sees them. The `--host` check described
  above is the only thing protecting a reserved host on that path, which
  is why it runs before anything else rather than being left to the
  deploy call.
- **gpu_util > 0.8 prints a warning, not a refusal.** The 0.8 threshold
  is extrapolated from one model's (Gemma4 NVFP4, AEON's own notes)
  production experience on this hardware family -- treat it as a
  caution, not a guarantee for every model.
- **Everything gets logged automatically**, win or lose: a full run
  transcript under `tests/logs/run-<timestamp>.log`, plus a container-log
  snapshot per variant (and a second one if the benchmark itself fails)
  -- nothing here depends on you reacting fast enough to grab evidence
  before teardown.
- **Only your own tokens are tracked while two hosts serve at once.**
  `SESSION_TRACKER` follows a single host, so a benchmark on the scratch
  node does not appear in the dashboard's session stats if the other node
  is also serving. `benchmark.py` measures directly, so the numbers this
  script reports are unaffected -- but don't cross-check them against the
  dashboard header. See `docs/BACKLOG-session-tracker-multi-model.md`.
