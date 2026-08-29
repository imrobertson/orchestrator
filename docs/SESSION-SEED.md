# Session Seed — DGX Spark Cluster Orchestrator

Minimal context to bootstrap a new session without re-reading every doc.
Pull the referenced file if you need the full detail behind any line.

## What this is
Two-node NVIDIA GB10 DGX Spark cluster (`spark-3`, `spark-4`), running vLLM
+ Ray multi-node tensor parallelism, controlled off-node from `maestro` by
`dgx-orchestrator.py` (FastAPI + web dashboard `index.html`), driven via
`dgx-config` CLI over Tailscale SSH. Private repo: `imrobertson/orchestrator`.
Current version: **V4.8.5** — always confirm a deploy actually landed via
the version badge (dashboard header, next to Server Time) or
`orchestrator_version` in `/api/status` before trusting a fix is live; a
forgotten `git push` before `git pull`-on-`maestro` has silently left stale
code running before.

## Architecture state (`ARCHITECTURE-MIGRATION-PLAN.md`)
- Phase 2 (recipe-based catalog, `common/` package, `USE_LEGACY_CATALOG=1`
  rollback) — done. Filename is a recipe's sole identity; no `name:` field.
- Phase 3 (N-node generalization) — hardware-gated, not started. New
  constraint now written in: **hosts are fabric-connected pools, not one
  flat pool** — a deploy/allocator must never span pools. Current sidestep
  for a hypothetical second pair (`spark-5`/`spark-6`) is a second
  `maestro2` instance, not yet built.
- Host identity (`PRIMARY_HOST`/`SECONDARY_HOST`/`PRIMARY_HOST_IP`) is
  config-derived from `cluster_config.yaml`, not hardcoded — landed
  opportunistically in V4.8.5, not itself a Phase 3 start.
- `ROADMAP.md` is the sole canonical doc for runtime-robustness backlog
  (teardown hardening, cache integrity, engine health, recipe guardrails).
  It used to be accidentally duplicated inside the architecture doc — fixed;
  don't re-introduce that.

## The headline fix this session (`TOMBSTONES.md` #76, top of file)
Multi-hour dashboard freezes (08-25/27/28) were `SessionTracker`'s plain
`threading.Lock()` self-deadlocking on its own re-entrant flush path.
Fixed: `Lock()` → `RLock()`. If the dashboard ever looks frozen again,
check `stale`/`stale_for_seconds` in `/api/status` first — growing over
minutes/hours means a genuinely stuck computation, worth a `py-spy dump`.

## Active model & known-good recipe facts
- **DeepSeek-V4-Flash-0731**, 1M-context recipe: `tp_size: 2`/`pp_size: 1`,
  `gpu_util: 0.75` (lowered from 0.82 after a real Ray-OOM crash).
- MLA models (DeepSeek family): `--kv-cache-dtype fp8` only — `nvfp4`-family
  KV cache dtypes are a hard vLLM reject. Never pair `--quantization
  modelopt_fp4` with an explicit `--kv-cache-dtype` (entrypoint hook
  silently overrides it back to the broken value).
- `--moe-backend flashinfer_cutlass` is the only validated GB10 MoE kernel
  for this model family.

## Top two open priorities, in order
1. **DSpark/MTP speculative decoding isn't working — real perf gap.**
   Measured ~14 tok/s decode vs. 30-60 tok/s reported elsewhere on
   comparable GB10 hardware. Stock image has no working DSpark path for
   this checkpoint. Plan is fully scoped in `BACKLOG-dspark-sm120-image.md`
   (HIGH priority): pull `orthozany/vllm-jasl-dsv4:pr41834-2026-05-13`
   (jasl's `vllm` fork, PR #41834), smoke-test against the small,
   ready-to-go `recipes/local/deepseek-v4-flash-0731-dspark-sm120.yaml`
   before scaling up. Not yet pulled or tested.
2. **Recipe catalog hygiene** — some recipes can be selected and launched
   but are known/suspected to fail. `ROADMAP.md` has two scoped-but-unbuilt
   fixes: a known-bad-flag-combination linter, and a per-recipe/topology
   `status:` marker (validated/unconfirmed/known-bad) auto-promotable off
   the existing `config_hash`-keyed launch-success tracking. Needs an
   actual pass over `recipes/local/*.yaml` / `recipes/eugr/*.yaml` — not
   done yet, upload those files to move this forward.

## Other known gaps / unconfirmed items
- `index.html` changes this session (headSelect→serving_host sync, version
  badge) were only `node --check`'d, never clicked through in a browser.
- `dgx-config correct-ledger` was dry-run-previewed and said to be applied
  for real, but never explicitly reconfirmed.
- The first (08-25 06:15) crash is still unconfirmed as the same OOM
  mechanism as the second — its logs predate the Ray-log-persistence fix.
- GitHub MCP connector write access still broken (OAuth completes, App
  never installs) — use the "Add from GitHub" file picker or direct upload;
  deliver via `present_files`.

## Working style
- Patched files only, no `.patch` diffs. Full multi-line git commit
  messages. Small sequenced tasks over one long prompt. Flag ambiguity and
  the "why" explicitly rather than guessing. Honest diagnosis over
  speculative fixes — call out uncertainty. Secondary AI assistants
  (Gemini) have introduced real regressions before — review recent changes
  critically when diagnosing.

## Doc map
`README.md` (architecture overview) · `INSTALL.md` (setup) ·
`USERMANUAL.md` (usage + troubleshooting) · `UsageShortcut.md` (fast-path
cheat sheet) · `TROUBLESHOOTING.md` (vLLM arg tuning confidence levels +
incident log) · `TOMBSTONES.md` (full per-fix history, newest = highest
number, currently #76) · `ARCHITECTURE-MIGRATION-PLAN.md` (phased
migration plan) · `ROADMAP.md` (runtime-robustness backlog, canonical) ·
`BACKLOG-dspark-sm120-image.md` (DSpark project, HIGH priority).
