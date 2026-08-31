# Task MD — End-to-end proof with a no-op mod — Review

## Status

**Complete, verified live.** Every item in the task's own verification
section confirmed on real hardware, 1-node and 2-node: derived tag exists
on each host, marker file present inside the running container, model
serves normally, and a second identical deploy skips the bake — proven by
comparing `docker image inspect`'s `Created` timestamp before and after
the repeat deploy, not inferred from wall-clock speed alone.

## What was built

**`mods/_noop/run.sh`** — the no-op mod itself. Writes a single marker
file (`$WORKSPACE_DIR/_noop_marker.txt`) and exits 0. Refuses (exit 1)
if `WORKSPACE_DIR` is unset rather than guessing a path. Nothing else —
no vLLM interaction, no idempotency guard needed (unlike Task ME's real
mod, this runs exactly once per bake; `ensure_mods_baked()` skips the
bake, run.sh included, whenever the tag already exists).

**`recipes/local/_scratch-noop-test.yaml`** — scratch recipe carrying
`mods: [_noop]` against `Qwen/Qwen3-0.6B` (small, fast warmup) on the
cluster's validated `eugr/spark-vllm-b12x:latest` image. Not a
production model; safe to delete once this review is filed.

## What was verified, and how

### Pre-flight: dry-run before touching hardware

`dgx-config deploy --model _scratch-noop-test --nodes {1,2} --head
spark-4 --dry-run`, both topologies, run before any live deploy. The
`mods.resolved_tag` in both responses matched a hash I precomputed
locally from the exact `run.sh` bytes shipped
(`eugr/spark-vllm-b12x:latest-mods-d470c7a26847e57b`), confirming the
file that landed on disk was byte-identical to what was generated and
that `resolve_mod_tag()`'s hash logic behaves as MB documented. Zero SSH
calls implied (pure/local per MB's design) — not independently measured
here, but consistent with MC's own scripted verification of that
property.

### 1-node

- **First deploy**: `{"status": "success", ...}`. Tag
  `...-mods-d470c7a26847e57b` present via `docker image inspect` on
  spark-4 (`Created: 2026-08-31T01:00:30.288613201Z`). Marker file
  content verified byte-exact via `od -c`
  (`Task MD no-op mod applied. WORKSPACE_DIR=/workspace/vllm`, no stray
  leading byte — an apparent leading `.` in one earlier terminal paste
  was confirmed to be a rendering artifact, not real file content).
  Health check `200` (confirmed via `curl -w "%{http_code}"`, not just
  `-s`, after an earlier `curl -s` with no `-w` was misread as a
  non-response — it was actually vLLM's normal empty-body 200).
- **Second identical deploy** (idempotency): warmup dropped from 135s to
  45s, and — the actual proof, not just the speed proxy —
  `docker image inspect`'s `Created` timestamp was unchanged
  (`01:00:30...`, byte-identical to the first inspect), confirming no
  rebake occurred.

### 2-node

- **First attempt failed** — see "Contradictions" below; not a defect
  in the mods mechanism, and the bake/marker/tag chain was already
  confirmed correct on both hosts *even during that failed attempt*
  (spark-4 unchanged at `01:00:30`, spark-3 freshly baked at
  `01:27:18`, both markers byte-correct) — the crash was entirely
  downstream, inside vLLM's own multi-node engine startup, after mod
  resolution/baking had already succeeded on both hosts.
- **After the recipe fix** (see below): `"[+] Waiting for Ray cluster to
  register worker nodes..."` appeared in the CLI output, confirming the
  Ray-based launch path was actually taken this time.
  `{"status": "success", ...}`. Health check `200`, confirmed via
  `-w "%{http_code}"`. Tag present on **both** spark-4
  (`01:00:30`, unchanged — no rebake needed, already had the tag from
  the 1-node run) and spark-3 (`01:27:18` — its own independent bake,
  first time seeing this tag on that host; confirms constraint 2, that
  independent per-host bakes of the same mod set converge on the same
  tag). Marker byte-correct on both (`vllm-head`, `vllm-worker`).
- **Second identical 2-node deploy** (idempotency): warmup dropped from
  135s to 60s; `Created` unchanged on **both** hosts afterward
  (`01:00:30` / `01:27:18`), confirming idempotency held on the host
  that baked for the first time in this run, not just the one that
  already had a head start.

## Contradictions and things the plan didn't specify

The task doc explicitly wants these surfaced. One came up, and it's a
real finding worth keeping even though it isn't a defect in Tasks
MA/MB/MC's actual deliverables:

**The scratch recipe I authored for this task initially used
`vllm_args: ""` on `2_node`, and that alone was enough to crash the
2-node deploy — not because of anything in the mods mechanism, but
because of how `_execute_deployment_impl()` picks a multi-node launch
strategy.** `use_ray` is computed as
`(nodes > 1) and ("--distributed-executor-backend" in vllm_args_list)
and ("ray" in vllm_args_list)`. An empty `vllm_args` makes that `False`,
which routes through the *other* branch: both containers run
`vllm serve` directly with `--nnodes`/`--node-rank`/`--master-addr`/
`--master-port`, and the worker gets `--headless` appended
automatically. That path crashed with
`AssertionError: collective_rpc should not be called on follower node`
from vLLM's own `multiproc_executor.py`, during KV-cache-spec
collection on the worker — after mod resolution and baking had already
succeeded on both hosts, so this was never a mods-integration failure.

Fixed by adding `--distributed-executor-backend ray` to the scratch
recipe's `2_node.vllm_args`, routing through the Ray-based branch
instead (`ray start --head` / `ray start --address=...` as the
entrypoint, with the real `vllm serve` invocation executed separately
against the head once Ray registers both nodes) — the same branch
`gemma-4-31b.yaml`, this cluster's one other live-verified 2-node
recipe, already uses successfully per MC's review.

This isn't a finding about Tasks MA/MB/MC's code — it's a recipe-
authoring trap for anyone hand-writing a 2-node scratch recipe from
scratch without copying an existing working one's `vllm_args` shape.
Worth a `TROUBLESHOOTING.md` or `ROADMAP.md` note (a known-bad-flag-
combination linter, already scoped as unbuilt tooling per the person's
memory, would have caught this specific case: `nodes: 2` combined with
an empty/non-Ray `vllm_args` on a recipe that isn't deliberately using
the headless path).

## Scope check

Per the plan's explicit note: no runtime mod application beyond what MB
already built, no `phase:` field, no mod-distribution registry — nothing
here needed any of them. `mods/_noop/run.sh` is the only new mod content;
it exercises the existing bake/resolve/deploy path end-to-end and adds no
new mechanism of its own.

## Changed files, in full

### `mods/_noop/run.sh`

```bash
#!/bin/bash
# mods/_noop/run.sh -- Task MD's proof mod. Touches nothing vLLM-related.
#
# Runs once, inside the throwaway bake container, per constraint 3
# (PHASE-MODS-PROMPTS.md): WORKSPACE_DIR is set by common/mods.py's
# _bake_on_host() to the base image's real Config.WorkingDir
# (/workspace/vllm on eugr/spark-vllm-b12x:latest, per M0/MB), not
# hardcoded here.
#
# Idempotency is not a concern for this file specifically -- unlike
# Task ME's real patch mod, this runs exactly once per bake (MB's
# ensure_mods_baked() skips the bake entirely, run.sh included, if the
# derived tag already exists on the host) -- but the write is still a
# clean overwrite (>) rather than an append, so a re-bake under a
# different tag (e.g. after a deliberate payload edit, per MB's
# check_payload_edit) never leaves a stale line from a prior bake.

set -euo pipefail

if [ -z "${WORKSPACE_DIR:-}" ]; then
    echo "mods/_noop/run.sh: WORKSPACE_DIR is unset -- refusing to guess a path." >&2
    exit 1
fi

marker="$WORKSPACE_DIR/_noop_marker.txt"
echo "Task MD no-op mod applied. WORKSPACE_DIR=$WORKSPACE_DIR" > "$marker"

echo "mods/_noop/run.sh: wrote $marker"
exit 0
```

### `recipes/local/_scratch-noop-test.yaml`

```yaml
recipe_version: "1"

# Tiny model deliberately -- this recipe's only job is to prove
# schema parse -> hash -> ship -> bake -> tag resolution -> deploy -> serve
# (Task MD) without a 10+ minute warmup eating the iteration loop.
# Same model MB's own smoke_test_mods.py --serve-check used.
hf_path: Qwen/Qwen3-0.6B

# Matches the cluster's validated image (M0/MB/MC all ran against this
# tag). Not the cluster_config.yaml default_image -- an explicit
# per-recipe override, same pattern gemma-4-31b.yaml already uses.
image: eugr/spark-vllm-b12x:latest

gpu_util: 0.5

mods:
  - _noop

notes: >
  Scratch recipe for Task MD's end-to-end no-op-mod proof. Not a
  production model -- safe to delete once MD closes out. Carries
  mods: [_noop] specifically so the dry-run and live-deploy paths
  exercise real mod resolution instead of the mods: [] no-op every
  other current recipe takes.

topologies:
  1_node:
    max_model_len: 4096
    tp_size: 1
    pp_size: 1
    env_vars: []
    vllm_args: ""
  2_node:
    max_model_len: 4096
    tp_size: 2
    pp_size: 1
    # Incident #1's rule, applied by default per Tombstones/TROUBLESHOOTING
    # guidance for 2-node Ray topologies -- this recipe has no history to
    # weigh against it the way gemma-4-31b.yaml does, so it stays on.
    env_vars:
      - "VLLM_USE_V1=0"
    # Required to route _execute_deployment_impl()'s use_ray check onto the
    # Ray-based launch path (ray start --head / --address=..., then a
    # separate `vllm serve` exec on the head once Ray registers both
    # nodes) instead of the --nnodes/--node-rank/--master-addr/--headless
    # manual path. Omitting this was the actual cause of Task MD's first
    # 2-node attempt crashing (AssertionError: collective_rpc should not
    # be called on follower node, from vllm's multiproc_executor.py) --
    # not a mods-mechanism defect. gemma-4-31b.yaml is this cluster's one
    # other real, live-verified 2-node recipe, and it goes through this
    # same Ray branch.
    vllm_args: "--distributed-executor-backend ray"
```

`common/mods.py`, `common/recipes.py`, `common/ssh.py`,
`dgx-orchestrator.py`: **unchanged** — this task only exercised the
existing MB/MC machinery against new content, it didn't modify any of
them.

## Recommended cleanup

`_scratch-noop-test.yaml` and `mods/_noop/` are scratch artifacts, not
production content. Safe to delete both once this review is filed,
unless there's value in keeping `_noop` around as a standing smoke-test
fixture for future mod-pipeline changes.
