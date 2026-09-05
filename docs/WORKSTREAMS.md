# WORKSTREAMS

The canonical backlog. Supersedes `ROADMAP.md`, `BACKLOG-*.md`, and the
backlog role of `ARCHITECTURE-MIGRATION-PLAN.md`. For direction and
decisions of record see `DIRECTION.md`; for per-fix history see
`TOMBSTONES.md`; for machine-readable recipe rules see `errata.yaml`.

Revision 2, 2026-09-03. Built from `TOMBSTONES.md` #27–#110,
`TROUBLESHOOTING.md` #1–14, `model_ledger.json`, and verification against
`dgx-orchestrator.py`, `common/recipes.py`, `tests/ab_test.py`.

## Evidence grades

| Grade | Means |
|---|---|
| **LIVE** | A real deploy produced a real number, and a named artifact holds it. |
| **LIVE-HEALTH** | A real deploy reached a healthy state but produced **no throughput number**. Not the same as benchmarked. |
| **LANDED** | Code is on disk and passes a syntax/harness check, but has never run against real hardware since the change. |
| **OPEN** | Not started, or started and explicitly unfinished. |
| **BLOCKED** | Named blocker. |

Two standing rules for anyone editing this file: a fix that landed but was
never run is **LANDED**, not done; and one measurement is one measurement,
stated as such.

**Ledger caveat.** `model_ledger.json` copies taken for handoff lag what is
running on `maestro`. The absence of a key is **not** evidence a deploy
didn't happen. Verify against the live ledger on `maestro`, not a snapshot.

---

# 1. Workstreams

## WS-0 — Documentation consolidation

**New, and the reason this file exists.** A synthesis pass on 2026-09-03
found seven places where a document described a state the code had moved
past. All seven pointed the same direction — docs behind code, never ahead —
which means entries were being read as open work after the work was done.

| Item | Status |
|---|---|
| `DIRECTION.md` written (replaces `ROADMAP.md` + `ARCHITECTURE-MIGRATION-PLAN.md`) | **LANDED** — drafted, not yet committed |
| `WORKSTREAMS.md` written (this file) | **LANDED** |
| `errata.yaml` written (replaces `TROUBLESHOOTING.md`'s tuning reference) | **LANDED** — 17 rules, each with provenance |
| Retire `ROADMAP.md`, `ARCHITECTURE-MIGRATION-PLAN.md`, `BACKLOG-*.md`, `PHASE-MODS-PROMPTS.md`, `SESSION-*.md`, `M*-REVIEW.md` | **OPEN** |
| Trim `TROUBLESHOOTING.md` to the incident log only | **OPEN** |
| Append MA/MB/MC results to the mods record before archiving `PHASE-MODS-PROMPTS.md` | **OPEN** |
| Update in-code doc pointers that name retired files | **OPEN** — several exist in `dgx-orchestrator.py` and `recipes.py` docstrings |
| Retire `models.yaml` and its code paths (Phase 2's last remnant) | **DONE in code, 2026-09-03** — see WS-0a |

**Stale claims to correct, with evidence** (all verified against attached code):

1. `ROADMAP.md`'s "`compute_config_hash()` hashes `vllm_args` as a raw
   string" — **false.** `recipes.py:222` implements
   `_canonicalize_vllm_args()`; `_CONFIG_HASH_SCHEMA = 2`. Fixed by #92.
   What survives from that entry is only its third bullet: normalizing
   `--speculative-config`'s embedded JSON, which #92 left as a deliberate
   known false negative. Carried below as a real open item.
2. `ROADMAP.md`'s mods entry: "What exists today: nothing … `mods` excluded
   from `compute_config_hash()`, never read by the deploy path" — **all
   three clauses false.** `recipes.py:326` includes `mods` as an ordered
   list; `dgx-orchestrator.py:3393` `_resolve_host_image_tag()` is called
   from both the 1-node (3607) and 2-node (3705) branches. The entry's
   design rationale is worth keeping in `DIRECTION.md`; its status is not.
3. `TOMBSTONES.md` #73's pointer says the pool constraint "still needs [to
   be] written in" to the migration plan. It's there now, including the
   `pool:`/`fabric:` field and a dry-run case that must reject a
   pool-spanning deploy.
4. `ROADMAP.md` describes `ab_test.py`'s scratch naming as
   `f"{label}-scratch"`. The code uses **two** conventions:
   `write_scratch_recipe()` produces the stem `_scratch-{label}` (prefix,
   `ab_test.py:274`) while `benchmark_model_key` uses `{label}-scratch`
   (suffix, lines 725/761). The ledger confirms the prefix form
   (`_scratch-gemma4-nvfp4-mtp::1_node`). This matters because that entry
   proposed using the convention to tell scratch variants from committed
   recipes — it would have to check both.
5. `TROUBLESHOOTING.md` contradicts itself on `VLLM_USE_V1=0` — promoted to
   a real experiment, see WS-2.
6. `dgx-orchestrator.py:3929` still declares `FastAPI(..., version="4.8.4")`
   and `ORCHESTRATOR_VERSION_SLUG` is still
   `"2026-08-28-primary-secondary-host-refactor"`, while `TOMBSTONES.md`
   records V4.8.5/4.8.6/4.8.7 work. Since the deploy-confirmation habit
   depends on that badge moving, decide whether these are meant to be
   bumped per change and were missed, or are deliberately coarse.
7. `SESSION-CLOSEOUT-2026-09-02-FINAL.md` — **confirmed present** in
   `docs/` (9.8 KB, 2026-09-02). Its ETA/telemetry content is ported into
   WS-5. Verify the port lost nothing, then archive.
8. **`recipes/eugr/` is empty** — it contains a `.gitkeep` and nothing else.
   Every document reference to `recipes/eugr/*.yaml` (in `ROADMAP.md`, the
   mods sequence, and the status-marker scope) describes an empty directory.
   The catalog is `recipes/local/`, 24 files, entirely (25 as of the models.yaml removal pass, minus the deleted nemotron duplicate).
9. **Documents cite deleted recipes.** `TROUBLESHOOTING.md` names
   `deepseek-v4-flash-0731-nvfp4.yaml` as the working example for its
   validated fp8-on-MLA rule; that file was deleted in the DSpark catalog
   trim. The rule is right, the example is stale. The `qwen-3.5-122b.yaml`
   retirement (#107) and the removal of the `qwen-3.6-27b-nvfp4` PP file
   left similar dangling references. A grep for recipe filenames across
   `docs/` catches the rest cheaply — do it during this pass.
10. **`ORCHESTRATOR_VERSION_SLUG` vs. the docs' own version vocabulary.**
    Related to item 6: `TOMBSTONES.md` entries are tagged `V4.8.6`, `V4.8.7`,
    and `V?.?.?`. The `V?.?.?` tags (every entry from #90 up, excepting a
    few) mean nobody has assigned versions since the slug stopped moving.
    Decide whether version tags still earn their place in that file or
    should be dropped in favour of dates, which are already there and are
    never wrong.

### WS-0a — Retire `models.yaml`

Phase 2 is complete: every model is a recipe. `models.yaml` survives only as
a fallback behind an env var, and the surface is small and fully mapped.

| Touchpoint | Location | Action |
|---|---|---|
| `MODELS_YAML_PATH` constant | `dgx-orchestrator.py:120` | Delete |
| `_load_model_catalog_legacy()` | `dgx-orchestrator.py:2932-2955` | Delete (24 lines, self-contained) |
| `USE_LEGACY_CATALOG` branch | `dgx-orchestrator.py:3034` | `load_model_catalog()` collapses to `raw_cat = build_catalog_response()` |
| `USE_LEGACY_CATALOG != "1"` guard | `dgx-orchestrator.py:3790` | Remove the guard; `PENDING_LAUNCH_STATE` / `ACTIVE_DEPLOYMENT_STATE` recording becomes unconditional |
| Legacy-mode comment | `dgx-orchestrator.py:3524-3530` | Simplify — the "may not even come from recipes/ at all" case disappears, though the `try`/fallback around `load_recipes()` should stay (an unrelated malformed recipe must not block a good deploy) |
| `models.yaml` | repo root | Delete, or move to `examples/` if it has reference value |

**One thing that is NOT part of this.** `legacy_hosts_dict()`
(`common/config.py`, imported at `dgx-orchestrator.py:46`, used at line 192
for `HOSTS`) is about the *cluster host* config shape, not `models.yaml`.
Same word, unrelated mechanism. Do not delete it in the same pass.

**Two real behaviour changes worth naming rather than discovering:**

1. Removing the guard at 3790 means every deploy records launch state.
   That is the intent — the guard existed because the legacy catalog has no
   per-recipe `config_hash` to key against — but it moves a path that was
   previously skipped in one mode into always-on.
2. `GLOBAL_HF_HUB_OFFLINE` / `GLOBAL_TRANSFORMERS_OFFLINE` are handled
   inside `_load_model_catalog_legacy()` by rewriting each topology's
   `env_vars`. **Confirm the recipe path has an equivalent** before
   deleting, or the offline switches silently stop applying. This is the
   one place where deletion could remove behaviour rather than just a
   fallback.

**Status: applied 2026-09-03.** `dgx-orchestrator.py` 4399 → 4376 lines.
The offline-switch trap cleared on inspection: `build_catalog_response()`
(`common/recipes.py:577-620`) already performs the identical strip-then-append
injection for `HF_HUB_OFFLINE`/`TRANSFORMERS_OFFLINE` and emits the same
`GLOBAL_*` catalog keys, sourcing both from `cluster_config.yaml` instead of
`models.yaml`'s top-level keys. No behaviour was removed.

**Verified by AST equivalence rather than by reading the diff**, per #90's
standard. Reparsed the original, programmatically hoisted the guarded block
out of its `if` and collapsed the catalog branch to its `else`, then compared
`ast.dump()` against the edited file: `_execute_deployment_impl` and
`load_model_catalog` are both identical. `_load_model_catalog_legacy` is the
only function removed; no function was added; no other top-level function's
AST changed. `legacy_hosts_dict` untouched, as intended.

**Still outstanding at code-review time:** a live `--dry-run` argv diff on a
real 1-node and 2-node deploy, plus the actual deletion of `models.yaml`
from the repo. Both since done — see the closeout below.

**CLOSED, 2026-09-03.** Full sequence run against production (`maestro`,
port 5001 — not FastAPI's default 8000, corrected mid-verification):

1. `git pull` + full `docker compose down && up -d` (not `restart` —
   confirmed elsewhere in this doc, e.g. #102, that a bind-mounted `common/`
   or `dgx-orchestrator.py` change needs a full container cycle to actually
   reload).
2. Badge moved `+9c5fd079` → `+3fdf79c5` exactly as predicted from the file's
   own sha256 before deploying — confirms the new code, not just a new
   container.
3. Catalog count 25 (pre-existing 27 was **my own miscount** when writing
   this entry originally — corrected throughout this file).
4. `GLOBAL_HF_HUB_OFFLINE`/`GLOBAL_TRANSFORMERS_OFFLINE` both present and 0
   post-restart — the one place deletion could have silently removed
   behaviour did not.
5. `--dry-run` argv diff, before vs. after, on both a 2-node
   (`qwen-2_5-coder-32b-tp`) and a 1-node (`gemma4-26b-a4b-nvfp4`) recipe:
   **the only differing lines were the `deploy_run_id` timestamp** embedded
   in the ray-logs bind-mount path (`.../qwen-2_5-coder-32b-tp-<unix-ts>/...`,
   two lines per host, expected since each invocation mints a fresh run id).
   Every other constructed `docker run` argument was byte-identical. This is
   the strongest evidence in this document of a refactor preserving
   behaviour end-to-end — AST equivalence *and* live argv equivalence,
   not one or the other.
6. `git rm models.yaml && git commit && git push` — confirmed gone from the
   repo.
7. The operator separately deleted the duplicate
   `nemotron-3.5-lightning-bf16.yaml` (the #110 fix — see WS-4) in the same
   session. Catalog now 24. **Ledger not yet touched** — the orphaned
   `nemotron-3.5-lightning-bf16::1_node`/`::2_node` ledger entries still
   reference a file that no longer exists. This is now the ordinary
   orphan-from-rename/delete case `tools/reconcile_ledger.py` exists to
   classify, not a live duplicate-`hf_path` collision risk (E018) — that
   part is resolved by construction, since only one recipe with that
   `hf_path` remains.

---

## WS-1 — Qwen topology (TP vs PP) and MTP validation

Started as the largest cluster of open work in this doc. As of 2026-09-04,
both of its live threads — the coder TP-vs-PP A/B and the 122B MTP
depth sweep — are closed with real, decisive results. What remains is a
single deferred decision (converting other `pp_size: 2` recipes) and one
never-reproduced inference (`qwen-3.5-122b`'s MTP/PP incompatibility) —
see D-1.

| Item | Status | Evidence |
|---|---|---|
| MTP + `pp_size > 1` is hard-incompatible | **LIVE** | `NotImplementedError: Pipeline parallelism is not supported for this model`, at `create_engine_config()`, on `qwen-3.6-27b-nvfp4::2_node`. Traced to the *draft* class (`Qwen3_5MTP`), not the target model. #104, incident #13, `errata.yaml` E005. |
| Same verdict applied to `qwen-3.5-122b` | **By mechanism, not reproduced** | #107 reasons from model-agnosticism and declines to burn a deploy cycle. Reasonable; still inference, not a second data point. |
| `qwen-3.5-122b.yaml` (PP file) retired | **Done** | #107. Contingency (a working TP replacement) satisfied. |
| `qwen-3.5-122b-tp` throughput | **LIVE** | 3-pass warm avg **40.9 tok/s** decode (38.4 cold, TTFT 49.25s cold / ~0.19s warm), `benchmark_ledger.csv`. Confirmed real by the operator; the handoff ledger snapshot simply lagged. One run, one number. |
| `qwen-3.5-122b-mtp2` / `-mtp4` | **LIVE, DECISIVE — 2026-09-04. No single winner; real Pareto trade-off.** | Both `ab_test.py` runs completed clean overnight: `mtp2` vs `-tp` (n=2 vs n=3) at 54/54 checks; `mtp2` vs `mtp4` (n=2 vs n=4) also `deployed=True, boot_log_hit=True` both sides. Full n=2/3/4 ordering derived transitively (n=2 independently re-measured in both runs, reproduced within ~0.2 tok/s both times — no third pairwise run needed): **extraction monotonically increases with depth** (46.3→51.3→54.4 tok/s, +17.5% n=2→n=4) while **coding monotonically decreases** (45.4→43.8→41.6, −8.5%) and **creative mildly decreases** (43.3→42.7→41.5, −4%); **default is flat** (~40.6 across all three, one low-outlier sample at n=4 widening its range to 38.6–41.3, not yet individually investigated). This directly contradicts a single "depth 3 is best" framing — it's two categories moving in opposite directions, increasingly so with depth, matching acceptance-rate theory (predictable/templated continuations reward longer draft chains; free-form generation pays an increasing verify-discard cost). Second independent confirmation of the same principle the DFlash sweep established (49–203 tok/s swing across categories) — now on a different model and speculative method entirely. **Practical read: pick depth per workload, there is no universal answer** — if this model serves a known-skewed traffic mix, that should drive the choice, not a borrowed "best overall" number. |
| `qwen-3.6-27b-nvfp4` TP side | **LIVE-HEALTH** | MTP genuinely engaging on both ranks (`Detected MTP model. Sharing target model embedding weights with the draft model`, `Worker_TP0`/`TP1`), full engine init, `GET /health 200 OK` at 03:40:58. No throughput number. |
| `qwen-3.6-27b-nvfp4` as a TP-vs-PP pair | **Dead** | PP side cannot boot. TP would win by forfeit. Correctly abandoned. |
| `qwen-2.5-coder-32b` pair | **LIVE-HEALTH** | No speculative-config either side, so clean of E005. Both sides confirmed loading via dashboard 2026-09-03. Also fixed: `max_model_len: 262144` against a real 32768 context (YaRN would allow 131072 and wasn't configured); both corrected to `32768`. |
| **The A/B run** | **LIVE, DECISIVE — 2026-09-03** | Completed (`tests/logs/run-20260903-154922.log`). **TP=2 beats PP=2 by ~1.9x on `qwen-2_5-coder-32b`**, uniformly: PP mean 3.5-3.6 tok/s vs TP 6.7 tok/s across all four prompt categories (+86% to +91%). 3/3 repeats deployed successfully per side; ranges are extremely tight (PP 3.5-3.6, TP 6.6-6.8). 12 measurements per side. This is the best-evidenced result in the entire doc set — every other throughput figure here is a single 3-pass run. |
| `ab_test.py` `--{side}-nodes` bug | **LIVE-VERIFIED** | #105. Six real 2-node deploys reached the catalog passthrough path via `--a-nodes 2 --b-nodes 2` (`nodes=2` confirmed in each variant header). Promoted from LANDED. |
| `ab_test.py` head-only pre-pull | **LIVE-VERIFIED** | #106. Pre-pull ran on **both** `spark-4` and `spark-3` on all six deploys, all PASS. No Ray version mismatch recurred. Promoted from LANDED. |
| Convert other `pp_size: 2` recipes to TP | **DECISION REVERSED for `llama-4-fp8` — do not convert.** For `qwen-2_5-coder-32b`, TP is proven ~2x faster on identical hardware and image; convert or retire that PP file. For `llama-4-fp8`, the analogous A/B surfaced a real TP-side defect instead of a throughput answer — see the new row directly below and #116. **PP remains the only reliable topology for this model today.** |
| `llama-4-fp8-tp` — its own A/B, as WS-1 flagged it would need | **CLOSED BY DECISION (#122), AND #119's mechanism is now DISCONFIRMED (#125), not just doubted — the real root cause of the original hang is genuinely unknown.** 3-run `ab_test.py` (2026-09-04): PP 3/3 clean (11.8–12.0 tok/s). TP 1/3 succeeded (16.9/16.7/16.8/16.5 tok/s — **not a valid PP-vs-TP data point**), 2/3 hung, killed by the 900s poll ceiling; a full manual deploy crashed with a Gloo TCP transport failure after ~48 minutes. Diagnosis arc, ten entries: #116 (mid-hang trace → `_profile_single_kernel`'s `all_reduce`) → #117 (outer `world.barrier()` confirmed symmetric) → #118 (a *second*, per-tactic `all_reduce` reopens the mechanism) → #119 (~~confirmed~~: `MoERunner._cache_key_extras()` bakes `local_expert_offset` into the cache key — real code, but its applicability to this deploy did not hold up) → #120 (why rank-divergent cache results can't persist — mechanism stands independent of whether it applied here, general pattern, see WS-9) → #121 (correction: fixable via two real paths against the image itself, neither a change to this repo) → #122 (decision: neither path pursued — no relationship with the fork's maintainer, limited time, working PP fallback already exists) → #123 (reopening: `enable_expert_parallel` defaults `False`, never overridden in this deploy's boot log) → #124 (further evidence: `parallel_state.py`, the file printing the `EP rank` label, has zero reference to the flag under its primary name) → **#125 (CLOSED): the renaming gap #124 left open is resolved — `parallel_state.py` has no reference to the flag under any name, including `enable_ep`; the only two conditionals near the print gate on `enable_eplb`, a different flag entirely. The `EP rank` boot-log label is confirmed unconditional bookkeeping. EP was not active on this deploy. #119's mechanism did not cause this incident's hang.** **The real cause remains unknown** — TP0's instant cache hit against TP1's ~48-minute live sweep before the crash is real and unexplained by anything confirmed in this arc; closing the EP hypothesis supplied no replacement one. `llama-4-fp8` stays on PP=2 regardless — **#122's decision stands, now with less to reopen it, since there is no confirmed mechanism left to fix even if time became available.** Full record: #116–#125. |
| Boot-log backend scan | **BROKEN, fires on every run** | All six deploys reported `boot_log_hit=False` — "no keyword matched". That is 6 of the run's 6 failed checks (48/54 passed); everything else was clean. The scan's keyword list does not match what these recipes emit, so it produces a false alarm every time, which is the "warning that always fires" failure mode. Consequence: the `TROUBLESHOOTING.md` standard of "we confirmed vLLM *resolved* the backend" is **not** met for this result — only "we set the flags". Recoverable: the full container logs were saved (`a-...-155759.log`, 124632 bytes, and siblings). Grep those rather than re-running. |
| `--{side}-tp-size` / `--{side}-pp-size` overrides | **OPEN, would pay for itself** | Would make this comparison a one-off command instead of a permanent second catalog entry each time. |

**Structural constraint, worth knowing before touching `ab_test.py`:** only
the pure named-recipe passthrough branch can reach a 2-node topology. Every
scratch recipe the script writes has a `1_node` topology only
(`ab_test.py:417-418`), and any real `--{side}-*` override forces the 1-node
ad-hoc path by construction. This also blocks the WS-2 experiment — see
there.

---

## WS-2 — Multi-node reliability, and settling `VLLM_USE_V1=0`

Mostly closed and unusually well-evidenced. One genuinely contested rule
remains, and it now has a customer: the linter.

| Item | Status | Evidence |
|---|---|---|
| Missing Ray flag silently routes to `--headless` | **LIVE** | Incident #1 + Task MD's from-scratch repro with `vllm_args: ""`. `errata.yaml` E003. |
| `default_image` doesn't ship `ray` | **LIVE** | `exec: ray: not found`. Incident #11. E004. |
| `llama-4-fp8::2_node` | **LIVE** | Ledger: `ready`, hash `c9097cbf66ec5e01`, compile 31.78s `reported_no_cache`, 3 ranks. |
| `llama-4-fp4::2_node` | **LIVE** | Ledger: `ready`, real cold `download_sec: 395.548035` at `reported` confidence. First-ever deploy of that topology. |
| `llama-3.3-70b::2_node` | **LIVE** | Best-evidenced fix in the set. Ledger shows the whole arc: two `crashed` runs under `a6e57cfa2cf641bd`, then `ready` under `16ed51feeb5685e4` with `weight_load_sec: 463.57`. |
| `qwen-3.6-27b-nvfp4`, `qwen-2.5-coder-32b` carry the same fix | **LANDED** | Not individually confirmed by a deploy that produced a number. |
| Image cache drift between hosts | **LIVE** | Ray 2.58.0 vs 2.57.0, deterministic across 6/6 attempts on two unrelated recipes. #106, incident #14. |
| Drift can recur from any other path | **OPEN by design** | `ab_test.py` is fixed; nothing generalizes the guarantee to manual `docker pull` habits or other deploy paths. |
| **`VLLM_USE_V1=0`: is it required, inert, or harmful?** | **RESOLVED on the current build, 2026-09-03** | `vllm.envs` no longer defines the name at all on `v0.1.dev20482+g83cb22a0e.d20260903` — TOMBSTONES #113, `errata.yaml` E015 (now `enforce: warn`, `confidence: known_bad`, scoped to that exact build string). K9's two-recipe A/B is no longer needed to answer the question; it closed by direct inspection instead. Historical positions (required / no-effect / not-needed, all from the older `builds.original_pin`) are preserved in E015's `positions` list, not deleted. |

### Why `VLLM_USE_V1=0` is worth the time

The original rule (#43, V4.8.1) bundles two things that are not the same
claim: *use the Ray executor* and *force the V0 engine*. The failure it was
written from — `collective_rpc should not be called on follower node` — is an
`mp`-backend failure across physical hosts. The fix that provably addresses
that is `--distributed-executor-backend ray`. `VLLM_USE_V1=0` rode along on
the theory that V0's Ray executor was the thing that worked.

Since then, on build `v0.1.dev20003+gad848fc41.d20260815`:

- **Set, no effect** — gemma-4-31b 2-node, 2026-08-29: variable set, engine
  logged `Initializing a V1 LLM engine` anyway.
- **Not set, worked** — `deepseek-v4-flash-0731-dspark::2_node`, 2026-09-02:
  never set, ran `distributed_executor_backend: 'ray'`, reached `ready` with
  real phase data.

The operator's hypothesis — needed sometimes, harmful other times — is
worth testing rather than assuming, but note the third possibility the
evidence points at more strongly: **the build may have no V0 path left**, in
which case the variable is structurally inert and the question closes
without a single deploy. That check is thirty seconds and should run first.

**A practical constraint discovered while designing this:** the A/B cannot
be run through `ab_test.py` on a 2-node topology. `--{side}-docker-env`
counts as an override (`ab_test.py:692` excludes only `"nodes"`), which
forces the 1-node ad-hoc path, and scratch recipes are 1-node only. The
experiment therefore needs **two committed catalog recipes** differing only
in that env var, run through the passthrough path. That is also the better
design: two recipes means two `config_hash` values and clean ledger
separation.

---

## WS-3 — Recipe catalog guardrails and the linter

Directly requested. `errata.yaml` (delivered) is the data half; the code
half is unbuilt.

| Item | Status | Notes |
|---|---|---|
| `errata.yaml` — structured rules with provenance | **LANDED** | 17 rules. Each carries `confidence`, `enforce`, `scope`, and `evidence`. Rules scoped to a specific build or image must not fire outside it. |
| Linter reading `errata.yaml` | **OPEN** | Soft warning at `load_recipes()` time or `tools/lint_recipes.py` in CI. **Never a hard failure** — `build_catalog_response()` fails closed and one exception empties the whole catalog (#41). |
| Render the human tuning reference *from* `errata.yaml` | **OPEN** | Do not maintain both by hand. Maintaining two copies is precisely how `TROUBLESHOOTING.md`'s tuning section came to contradict its own incident log. |
| Per-recipe/topology `status:` marker | **OPEN** | Schema field defaulting to `unconfirmed`, auto-promoting from `PENDING_LAUNCH_STATE`'s `config_hash`-keyed success tracking, surfaced as a badge in the dashboard dropdown and `dgx-config status`. Prerequisite already paid — see D-2. |
| Initial `status:` values across the catalog | **UNBLOCKED, 2026-09-03** | The catalog is 24 files in `recipes/local/` — `recipes/eugr/` contains only a `.gitkeep` and is empty, so every doc reference to `recipes/eugr/*.yaml` describes nothing. Readable directly via the GitHub connector. The pass has not been done, but nothing prevents it now. |
| Unknown-`VLLM_*` boot-log scraper | **OPEN** | Parse the boot log, don't maintain an allow-list — vLLM already emits the warning for the build actually running. Check recipe `env_vars` only; image-inherited hits report differently or not at all. Much cheaper once WS-7's log retention exists. |
| Normalize `--speculative-config`'s embedded JSON in `config_hash` | **OPEN, small** | The one genuinely live piece of the otherwise-stale ROADMAP hash entry. #92 left it deliberately: flag values are opaque strings to the canonicalizer, so reformatting the JSON inside changes the hash. |
| Near-duplicate catalog key detection at load | **OPEN** | Exact stem collisions already raise; nothing flags edit-distance-close keys or a shared `hf_path`. `find_cached_models()` already computes the `hf_path → catalog_key` map — build it as a shared helper. |

---

## WS-4 — Ledger and key identity

The root of `DIRECTION.md`'s bug-class 1, and the workstream most likely to
justify a refactor.

| Item | Status | Notes |
|---|---|---|
| **`nemotron-3.5-lightning-bf16` two-key split (#110)** | **CLOSED end to end, 2026-09-03/04** | Duplicate `hf_path` recipe deleted; `errata.yaml` E018 guards the class going forward; `clean_ledger.py` extended, applied to production, verified live-read, committed. See below for the full record. |
| Naming convergence, dot vs underscore | **DECIDED, sequenced** | Underscore. See below. |
| `tools/reconcile_ledger.py` (read-only) | **OPEN** | The prerequisite for the rename pass. |
| `benchmark_ledger.csv` `--model-key` mismatch | **OPEN** | Confirmed once: the DSpark validation run logged under `deepseek-v4-flash-0731-1M` while validating `-dspark`. Fix: have the orchestrator's own benchmark caller always pass `--model-key` derived from the recipe actually being benchmarked, plus warn loudly when the `model_id.split("/")[-1]` fallback matches nothing in the catalog. An audit of existing rows has never been done. |
| Orphan keys from renames | **Expected, not cleaned** | The ledger keys on stem and never deletes — deliberate. Currently visible: `deepseek-v4-flash-0731-dspark-sm120`, `-dspark-gb10-hazyumps-512k` (renames per #91's own correction), `qwen-3.5-122b::2_node` (recipe retired by #107). |

### #110: root cause CONFIRMED and the duplicate recipe RESOLVED, 2026-09-03

**Two live recipe files carried an identical `hf_path`** (confirmed by direct
repo read, before either was touched):

| File | `hf_path` | `image:` | Topologies |
|---|---|---|---|
| `recipes/local/nemotron-3.5-lightning-bf16.yaml` | `nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16` | **absent** → falls back to `default_image` | `1_node` only |
| `recipes/local/nemotron-3_5-lightning-bf16.yaml` | *identical* | `eugr/spark-vllm-b12x:latest` | `1_node` + `2_node` |

This reclassified #110: it was never a novel key-derivation divergence
between two subsystems. It is the near-duplicate catalog-entry class — #57
and, precisely, #77, whose own text says two recipes serving the identical
checkpoint "collide under that match by construction, so the code silently
took whichever catalog entry it happened to iterate to first."
`_resolve_catalog_key()` matches on `hf_path.endswith(loaded_model) or
cat_key == loaded_model or loaded_model in hf_path`, and both files
satisfied it. First match wins, and dict order decided which.

**The dot-form recipe file (`nemotron-3.5-lightning-bf16.yaml`) has since
been deleted by the operator, 2026-09-03.** That resolves the duplicate-key
collision at its source: only one recipe with this `hf_path` remains, so
`_resolve_catalog_key()` can no longer return an ambiguous match for it.
`errata.yaml` E018's `confirmed_instance` for this pair is closed; the rule
itself stays live for the general class.

**Two further observations from the same read, still true generally:**

- `SessionTracker`'s `topo` is computed independently of the recipe —
  `"2_node" if len([hosts with an active container]) > 1 else "1_node"`
  (`_compute_cluster_status_impl`). That is how a `::2_node` ledger key can
  exist for a file that only ever had a `1_node` block, and it is worth
  knowing generally: the telemetry topology is an observation of the
  cluster, not a property of the deployed recipe.
- The deleted file carried **no `image:` field**, so it would have fallen
  back to `default_image` (no Ray). Harmless while `1_node`-only, but it was
  a live, selectable recipe one topology-addition away from `errata.yaml`
  E004 — the exact trap #108 fixed proactively on its underscore twin.

The call-site map below is retained because it is still the accurate
description of which writer produces which field — this part of #110's
investigation outlives the specific incident:

- **`lifetime` / `last_seen_raw`** ← `SessionTracker._commit_session()`,
  keyed `f"{self.model}::{self.topo}"`, where `self.model` arrives from
  `SESSION_TRACKER.update(vllm_metrics, matched_model, topo)` in
  `_compute_cluster_status_impl` — and `matched_model` is
  `_resolve_active_recipe(...)[0]`.
- **`launch_history`** ← `_record_launch_success(pending["model"], …)`,
  where `pending["model"]` comes from the deploy path (the recipe stem).
- **`runs[]`** ← `record_run_phases(model, topo_key, …)`, same deploy-side key.

`_resolve_active_recipe()` (`dgx-orchestrator.py:2255`) prefers
`ACTIVE_DEPLOYMENT_STATE`'s exact `catalog_key`, and otherwise falls through
to `_resolve_catalog_key()`, whose no-match fallback returns `loaded_model`
**unchanged**.

That detail is what ruled out the "telemetry derives its key from the served
name" reading: the fallback would have produced the raw served basename
`NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16`, not
`nemotron-3.5-lightning-bf16`. Nothing in `_resolve_catalog_key()`
lowercases or abbreviates. Both observed keys were real catalog keys,
because both recipe files were real. Consistent with that, the
**1-node** dot-form key carries `launch_history` and `runs[]`, which are
deploy-side writes and only ever happen from a recipe stem.

### What's left: the ledger, not the recipe — still OPEN

The recipe-level fix is done. Two orphaned ledger keys remain, and the
operator has explicitly deferred touching them:
`nemotron-3.5-lightning-bf16::1_node` and `::2_node` now reference a stem
with no corresponding file. This is no longer the E018 collision-risk case
(nothing can currently resolve *to* that key from a live deploy) — it is a
plain rename/delete orphan, the ordinary case `tools/reconcile_ledger.py`
exists to classify, same shape as the pre-existing
`deepseek-v4-flash-0731-dspark-sm120`/`-gb10-hazyumps-512k` orphans from #91.

**The two keys are not equivalent and should not be handled identically —
recorded here since a `sed`-based bulk cleanup was considered and correctly
rejected:**

- **`::2_node`** — `lifetime` (124/1487) and `last_seen_raw` only. No
  `launch_history`, no `runs[]`. No diagnostic content beyond token counts
  that cannot be attributed to a specific config. Safe to drop outright.
- **`::1_node`** — real phase data: `cached [597,611,551]`,
  `compiled [571,674,1017,3001]`, and a full `runs[]` record (total 522.2s,
  weight_load 465.51s). Dropping this outright would leave the surviving
  `nemotron-3_5-lightning-bf16::1_node` topology with **no load-time
  history**, forcing the ETA estimator back onto its hardcoded default on
  the next 1-node deploy of this model. Recommended: **rekey** to
  `nemotron-3_5-lightning-bf16::1_node` (merging with whatever's already
  there under that key, if anything) and drop only its `launch_history`,
  which attests to a `config_hash` (`65c268515202a4f7`) computed against
  the now-deleted file — the surviving recipe adds `image:`, so that
  specific hash can never join to it regardless. **One caveat before
  rekeying:** this key's own `launch_history` hash doesn't match its own
  `runs[]` record's `config_hash` (`7f46f3161f0d6a4d`) — see F-c. That's
  a pre-existing internal inconsistency in the entry, unrelated to the
  rekey itself, but worth a second look before trusting the `runs[]`
  record's numbers wholesale.
- **`clean_ledger.py` extended, tested, applied, and committed — CLOSED,
  2026-09-03/04.** Added a `REKEY` operation alongside the existing `DROP`:
  an explicit `REKEY_MAP` (old key → new key, fields to strip, reason),
  applied after all drops so a rekey destination can never collide with a
  key the same run is removing. Refuses — does not partially apply — if the
  destination already exists, reports both sides' contents, and exits 1 so
  the failure can't be mistaken for a clean run; this mirrors the tool's
  existing conservative bias (explicit allowlists, no heuristics, dry-run
  default) rather than introducing a new one. `nemotron-3.5-lightning-bf16::2_node`
  moved from the stale "Deliberately NOT touched — legitimate, just
  incomplete" note into a fourth `DROP` class, `ORPHANED-RECIPE`; the
  `::1_node` entry is the one `REKEY_MAP` entry, stripping only
  `launch_history`.

  Verified twice, not once: first against a local copy before handover
  (dry run produced exactly the two expected operations; `--apply` moved
  `cached`/`compiled`/`runs[]` intact while dropping only `launch_history`;
  a re-run against the cleaned output was a true no-op; a synthetic
  destination-conflict test left both sides of a colliding pair completely
  untouched and exited 1 while an unrelated pending drop in the same run
  still went through). Then run for real, on `maestro`, against production
  `model_ledger.json` — dry run matched the local-copy plan exactly
  (1 drop, 1 rekey, 0 conflicts); `--apply` produced the identical result
  (26 keys, rekeyed entry present, `launch_history` absent, both old
  dot-form keys gone) with a `.bak-1788484650` safety copy; confirmed
  `dgx-config status` showed nothing deployed beforehand so nothing raced
  the daemon's live poll of the file. Live-read confirmed, not assumed —
  traced `get_estimated_load_time()` (`dgx-orchestrator.py:1285`) to
  `data = _read_json_state(LEDGER_PATH)` called inline on every invocation,
  so the change was live for the daemon immediately, no restart needed.
  Committed to git the same session. **#110/#111 fully closed end to end**
  — recipe deleted, `errata.yaml` E018 guards the class going forward,
  ledger cleaned, change committed.

**On merge safety, for the record — no longer live, but the reasoning stays
useful for the reconciliation tool generally.** Both 2-node keys carried an
*identical* `last_seen_raw` (`p: 93.0, g: 768.0`) while their `lifetime`
totals differed (124/1487 vs 62/709). `last_seen_raw` is the raw cumulative
counter at commit time, written by `_commit_session()` alongside `lifetime`.
Identical values on both keys is consistent with the two entries covering
**overlapping** rather than disjoint spans of the same engine lifetime — in
which case summing the `lifetime` totals would have double-counted. Same
hazard #72 documents for `correct-ledger`: set-vs-add semantics only hold
for a single continuous lifetime, and the `--force` guard cannot catch the
multi-lifetime case because the summed values usually still look larger than
what they replace. This is why the recommendation above is *rekey the
1-node entry, drop the 2-node entry* rather than *merge the two* — merging
was never proposed and still isn't.

### Naming: converge on underscore, but not yet

The live catalog is genuinely mixed — 6 ledger keys carry a dot as a decimal
separator (`qwen-3.8-27b-nvfp4`, `qwen-3.5-122b`, `llama-3.3-70b`,
`nemotron-3.5-lightning-bf16`, and topology variants), 3 carry an underscore
(`minimax-m2_7-nvfp4-gb10`, `nemotron-3_5-lightning-bf16`, plus
`qwen-2_5-coder-32b`). Dots are the older convention; underscores arrived
with more recently generated recipes.

**Recommendation: underscore.** Not on aesthetic grounds and not because
either parses badly — `Path.stem` handles both correctly. The reason is
diagnostic: HF model names always carry the literal dot
(`NVIDIA-Nemotron-3.5-Lightning-…`), so if catalog keys never do, then **any
key containing a dot is immediately identifiable as derived from the served
name rather than from a recipe file.** That turns a naming convention into a
free signal for exactly the class of bug #110 belongs to. Enforce it with a
load-time validator rejecting `.` in a recipe stem — a few lines that make
recurrence structurally impossible, which is worth more than the choice
itself.

**Sequencing matters more than the choice.** Renaming a recipe orphans its
`launch_history` (the ledger keys on stem and never deletes). Renaming ~6
recipes before `tools/reconcile_ledger.py` exists means manufacturing six
orphans at precisely the moment nothing can tell an orphan from a real gap.
**Build the reconciliation tool first, then rename in one deliberate pass,
then turn on the validator.** `qwen-2_5-coder-32b` is already
underscore-correct and needs no rename; its A/B (WS-1) completed
2026-09-03 with a decisive TP-wins result, so there is no live experiment
left to confound.

---

## WS-5 — ETA / telemetry and run-log phase data

Tasks A–D shipped 2026-09-02. Residue is small and precisely scoped.

**Shipped:** run-log archive capture, a `config_hash → config_registry.json`
decoder (28 hashes, 0 collisions at the time), phase-based run recording
replacing keyword-guessed buckets, and a tiered ETA reader. Bugs found while
shipping: #93–#100.

| Item | Status | Evidence |
|---|---|---|
| Download-phase marker (#101) | **LIVE, n=3** | Ledger corroborates every cited figure exactly: `llama-4-fp4::1_node` `download_sec: 319.087079` with `unaccounted_sec` corrected 376.45 → 57.367661; `nemotron-3-nano-30b-a3b-nvfp4::1_node` at `106.291579`; `llama-4-fp4::2_node` at `395.548035`. #101's own caution stands: generalization beyond these stacks is still open. |
| Compile-cache confidence split (#102) | **LIVE + deployed** | Ledger confirms both cited samples as `reported_no_cache` (gemma-4-31b 78.32s, llama-4-fp8 31.78s). `_inductor_cache_disabled_for()` present at `dgx-orchestrator.py:2364` and threaded through `archive_run_log()`. Confirmed live post-restart (`+6b344453` → `+fd079` after a full `down` + `up -d` — `docker compose up -d` alone does not pick up source changes). |
| Does `resolve_mod_tag()` hash mod **names** or **contents**? | **OPEN — cheapest high-value question in the set** | If names, editing `mods/<n>/run.sh` leaves the tag unchanged, `ensure_mods_baked()` treats it as a cache hit, and the edit silently never reaches the container — while `config_hash` correctly reports no change, because `mods` is hashed by name there too. Two dry-runs either side of a real edit settles it. **Do not paste raw dry-run output** (#86). |
| Is `capability` safe to exclude from `config_hash`? | **OPEN** | Excluded on the same "inert metadata" premise that went stale for `mods`, never re-verified. `capability` is confirmed *intentionally* unused today (reserved for Phase 4), which rules out "dead code, delete it" but does not answer the exclusion question. |
| Live in-progress countdown | **OPEN, unscoped** | `detect_model_stage()`'s keyword phase guess is untouched by Tasks C/D and still flips mid-load. Read #96 first: the `index.html` clamp is **cosmetic** and documented as such ("smooth and wrong rather than jumpy and wrong"). A real fix plausibly reads the same self-reported-duration lines `phase_extract.py` already parses, live rather than post-hoc — a design effort, not a patch. |
| Tainted pre-fix `gemma-4-31b::2_node` entry | **Accepted, no action** | `pre_load_sec` wrong in one entry, `total_sec` fine, nothing reads the wrong field, ages out past the 20-run cap. |
| Median-not-mean ETA (#95) | **Landed, with a stated regression** | A genuinely cold first deploy now **underestimates** and lands in "Finishing startup (+Ns over est.)". Deliberate: an overrun message is honest, a countdown parked at 5059s is not. |

---

## WS-6 — Mods / derived-image bake

Built and wired. The remaining items are small.

| Item | Status | Evidence |
|---|---|---|
| M0 — `docker commit` fidelity gate | **PASSED 2026-08-30** | `.Config` byte-identical between base and commit on `eugr/spark-vllm-b12x:latest` (arm64/GB10); `WorkingDir = /workspace/vllm`; NGC entrypoint fires from the committed image. `docker create` alone sufficed. |
| MA/MB — schema, bake, cache, tag resolution | **LANDED** | `common/mods.py` with `ensure_mods_baked()`, `resolve_mod_tag()`, and two distinct exception types. |
| MC — deploy-path integration | **LANDED + live-verified** | `_resolve_host_image_tag()` called from both branches — the "don't write the resolution logic twice" requirement was met. |
| `mods` in `config_hash` | **Done** | `recipes.py:326`, ordered list, deliberately **not** sorted: mods bake in sequence and a later one can overwrite an earlier one, so `["a","b"]` ≠ `["b","a"]`. Called out in both the field comment and the docstring so nobody "fixes" it for consistency. |
| Bake-failure blast-radius asymmetry | **Documented, not fixed** | #85: `ModResolutionError` always aborts before any container starts; `ModBakeError` can leave host 1 running when host 2 fails. Left matching existing `docker run` partial-failure behaviour on purpose. If rollback is ever added it must cover both uniformly. |
| `common/mods.py` docstring names a function that doesn't exist | **OPEN, trivial** | #84: docstring says `resolve_and_bake_mods()`; the function is `ensure_mods_baked()`. |
| `mods/gemma4-nvfp4` | **Built, unused** | sha256-verified, but targets the `bg-digitalservices` checkpoint, which nothing deployed currently uses. |
| Baked-layer retention | **OPEN** | Needs the treatment `crash_log_retention_days` gives Ray logs. Layers are small relative to weights; not urgent. |

---

## WS-7 — Runtime robustness

Grouped because they share a shape: things that fail quietly or lose
evidence. Ordered by what each unblocks.

| Item | Status | Notes |
|---|---|---|
| Retain the full vLLM container log per deploy | **OPEN — highest leverage here** | Docker already persists stdout via `json-file`; containers run `-d` without `--rm`, so nothing needs flushing. Design: capture at teardown before `docker rm`, plus a low-frequency incremental `docker logs --since` net (60s, must be incremental). **Do not** `docker logs -f` from the daemon — orphaned children are an existing bug class (#81). Storage: reuse `~/.cache/ray-logs/<deploy_run_id>/<host>/`, with its own `vllm_log_retention_hours` (default 24) — not `crash_log_retention_days`, since Ray dumps are tiny and vLLM logs are not. Keep whole logs initially: a head/tail policy would discard exactly the hours in which slow degradations are visible. **Check first** whether `/etc/docker/daemon.json` sets `log-opts max-size`/`max-file`; if it does, the whole premise changes. |
| `/api/status` reports nothing about `common/` | **OPEN** | `docker-compose.yml` bind-mounts `.:/app`, so a `common/*.py` edit lands on disk instantly while the daemon keeps running the imported version. `orchestrator_version` cannot catch this — it hashes `dgx-orchestrator.py` only. Cost a real debugging round. Options: a `modules` block hashing what was loaded at import time, or simply always restarting the API container on deploy (cheaper, strictly more reliable). |
| Engine health monitoring | **Partial** | `_detect_crash_signature()` catches tracebacks and argparse errors. Nothing checks whether the engine **process** is alive — a segfault, OOM-kill, or silent hang produces neither signature. Track the `docker exec -d`'d PID; treat "container RUNNING, engine absent, health never passed" as unambiguously CRASHED. Keep the log scan as a fast path: it reports the *reason*. |
| `--dry-run` embeds live `HF_TOKEN` | **OPEN, no fix applied** | #86. Became a real incident when a dry-run response containing a real token was pasted into a conversation. Decide deliberately: mask before it reaches any response dict, or document the output as sensitive. Masking touches the same `env_flags` path real deploys use. |
| Teardown: orphaned compile children | **Partial** | 4.8.4 added grace period, graceful stop, `--init`. Not fixed: the grace period has a ceiling; `ps aux | grep -E 'vllm|ray'` can't match bare `ptxas`/`nvcc`/`cicc`; nothing checks whether a compile is in flight before killing. |
| Host-level `ps aux` step is inert for containers | **Understood, keep the note** | Structurally cannot see containerized processes; kept only as a bare-metal safety net. Documented so nobody "fixes" it thinking it was the protection. |
| POSIX `/dev/shm` files never swept | **OPEN, second real confirmation 2026-09-04** | SysV segments with `nattch == 0` are swept every teardown (a hard kernel guarantee). `/dev/shm` needs a `/proc/*/fd` + `/proc/*/maps` cross-reference — buildable, but riskier to get subtly wrong, deliberately deferred. SysV semaphores are inventoried, not swept. #116 independently confirmed this gap while investigating an unrelated deadlock: 29 orphaned `psm_*` files on spark-4 spanning ~36 hours, zero on spark-3, `ipcs -m` clean on both. Capacity-innocent (`df -h /dev/shm`: 1.3M/61G) and ruled out as that bug's cause, but the leak itself is real and this makes two suspected-or-confirmed incidents (2026-08-23, 2026-09-04) rather than one. |
| Cache integrity retrospection | **OPEN, wants ground truth** | One concrete artifact: a `tilelang` entry named `tmp` with an implausible ~56-year age. The heuristic needs real Triton/TileLang/DeepGEMM cache-layout contracts before it can be trusted. |
| Two fixes never verified in their real UI path | **OPEN, small** | #78's teardown error toast (no failing teardown existed to test against) and #66's `headSelect` sync (only `node --check`'d). |

---

## WS-8 — Phase 3 inputs

Hardware-gated. Only two items should move before the hardware.

| Item | Status |
|---|---|
| Interface names out of recipe `env_vars`, derived from `cluster_config.yaml` | **OPEN — external deadline.** Every 2-node recipe hardcodes `NCCL_SOCKET_IFNAME`/`GLOO_SOCKET_IFNAME=enp1s0f0np0` while `cluster_config.yaml` also declares `network.interface`. Same value, two places; the recipe copy is what reaches `docker run`. Confirmed prior art: eugr's recipes carry no interface names at all — `autodiscover.sh` derives them at launch. |
| `config_hash` stability across a topology-key change | **DECISION, not code.** Version the hash or make it topology-key-independent — before more data accumulates under the current scheme. |
| Mesh-vs-switched NCCL variable sets | **OPEN, deferred.** Four active CX7 interfaces means mesh and a different variable set. `cluster_config.yaml` already reserves the distinction with `network.topology: switched`. |
| Second cabled ConnectX-7 port between spark-3/4 | **Documented, unused.** Confirmed genuinely cabled. Beware the known false positive: the *other* port reports `Link detected: yes` from cached firmware data (incident #9). |
| Pool constraint in code | **OPEN.** Written in `DIRECTION.md`; nothing enforces it. |

---

## WS-9 — DSpark, engines, and image selection

`BACKLOG-dspark-sm120-image.md` is folded in here and can be archived. Its
headline result is closed; **five of its six open items are real and
survive**, so archiving it without transcribing them would have lost work.

| Item | Status | Notes |
|---|---|---|
| DSpark on the GB10-native image | **LIVE, two context sizes** | `hazyumps/deepseek-v4-flash-gb10:sm121-cu130-20260727d` (jasl PR #41834 SM12x enablement, GB10-native prebuilt). `deepseek-v4-flash-0731-dspark` (384K, `max_num_seqs: 4`): 3-pass, temp=0, cold **44.7 tok/s** / TTFT 0.12s, warm avg **42.7 tok/s** / TTFT 0.13s — ~3× the ~14 tok/s stock `eugr/spark-vllm-b12x` baseline, which has no working spec-decode path at all. Confirmed via draft-model load, active Markov sampler, and per-request acceptance metrics, not just a clean boot. Auto-selects FlashInfer SM120 sparse-MLA decode + MARLIN MoE with no explicit backend flags; Ray works. |
| `orthozany/vllm-jasl-dsv4:pr41834-2026-05-13` | **Dead end, recorded** | x86_64-only, no arm64 build exists, `Exec format error` on GB10. Don't revisit unless an arm64 tag appears. |
| Catalog trim | **DONE** | The backlog's item 6 recommended cutting `deepseek-v4-flash-0731-b12x-nospec.yaml` and `deepseek-v4-flash-0731-nvfp4.yaml`, leaving `dspark` / `dspark-512k` / `1M`. Verified against the repo: exactly those three remain. `-sm120.yaml` is also gone, confirming #91's rename correction. |
| **512K long-session soak** | **OPEN** | `deepseek-v4-flash-0731-dspark-512k` (524288, `max_num_seqs: 1`, deliberately conservative) boots and serves, but only a short benchmark — never soaked. Watch specifically for #7's failure mode: Ray's memory monitor OOM-killing a worker as unified-memory headroom runs out over hours. Not production-ready until this runs. |
| **JIT warmup gap** | **OPEN, unquantified** | Several kernels JIT-compile mid-inference on first real requests rather than during startup warmup. Likely explains lower/noisier early-request throughput vs. steady state. Never measured separately. |
| **Missing tuned FP8 kernel config** | **OPEN** | Shape `N=4096,K=12288` on `NVIDIA_GB10` falls back to generic W8A8 block-FP8. Worth generating a tuned config if the shape proves hot in real traffic. |
| **Re-benchmark under `probabilistic` sampling** | **OPEN** | Current numbers are one prompt shape under greedy. Third-party data shows acceptance ranging 33% (prose) to 78% (templated bulk generation), so our 38–46% is probably prompt-dependent rather than a ceiling. Use `--repeats` and multiple prompt categories. |
| Shared-expert loader bug (tonyd2wild) | **Ruled out by source trace** | Their patch is real and worth +69% decode elsewhere (25.7%→60.2% acceptance), but our image's `vllm/models/deepseek_v4/nvidia/dspark.py` already carries the complete mapping, and the markov-tensor collision their patch guards against cannot occur here. Confirmed by reading the loader, not assumed. The backlog cites a saved `REFERENCE-dspark-shared-expert-fix.md`; **that file is not in the repo** (F-i). The finding itself is preserved here and in `TROUBLESHOOTING.md`. |
| NVFP4 KV cache (`nvfp4_ds_mla`) as a context lever | **Deliberately not pursued** | Needs a heavily-patched third-party runtime (three staged Docker builds), not a flag. Their own measurement: zero effect on draft acceptance or speed; the only benefit is KV pool size. Only worth revisiting when pushing toward ~1M **with real concurrency**. A separate runtime lineage, not a recipe edit. |
| `benchmark_ledger.csv` key mismatch | **OPEN** | Independently recorded here and in WS-4 — same incident (the validating run logged under `deepseek-v4-flash-0731-1M`), not two bugs. |
| SGLang | **OPEN, unstarted** | Add `engine:` to the schema defaulting to `vllm`; branch entrypoint/flag construction. Either build an SGLang phase extractor or explicitly accept no phase telemetry — silent data loss is worse than a documented gap. |
| llama.cpp | **OPEN, different problem** | GGUF, not safetensors. Scope separately. |

**Worth periodically re-checking:** vLLM PR #41834 (jasl's SM12x
enablement, the fork our image builds on) and PR #46995 (DSpark, merged to
main). If GB10/SM120 support lands upstream, the fork dependency disappears
entirely. Not evaluated: `MiaAI-Lab`'s alternative image
(`ghcr.io/anemll/dspark-vllm-gx10`); `drowzeys`' concurrency patch, relevant
only for high concurrency *and* long context together.

### General pattern: FlashInfer's collective-cardinality fragility under TP-parallel MoE — corrected scope per #126; the EP-specific version of this was closed as a hypothesis for `llama-4-fp8-tp` (#125)

Discovered debugging `llama-4-fp8-tp` (WS-1, TOMBSTONES #116–#126). **Two
layers here, and they should not be conflated:** a specific mechanism
(#119's EP-based cache-key divergence) that was proposed, doubted, and
then confirmed *not* to have caused this particular incident (#123–#125);
and a broader, genuinely durable structural fragility (#126) that the
deeper chase past #125 surfaced, confirmed real by direct source read, but
never confirmed to be *this* incident's actual trigger either. Read the
full arc before treating anything here as a diagnosis template.

**The specific mechanism, and why it's confirmed not to apply here:**
`llama-4-fp8-tp`'s boot log showed `TP rank X, EP rank Y` for both ranks,
which #119 read as proof expert-parallel sharding was active — the premise
a cache-key-divergence mechanism was built on
(`MoERunner._cache_key_extras()` bakes `local_expert_offset` into the
FlashInfer cache key, genuinely, per direct source read). #125 confirmed,
by reading `vllm/distributed/parallel_state.py` directly, that the
`EP rank` print is **unconditional rank bookkeeping** — not gated on
`enable_expert_parallel` under that name or any rename the codebase is
confirmed to use elsewhere. Combined with `enable_expert_parallel`
defaulting `False` and never being overridden in this deploy's own boot
log: **EP was not active on this deploy.** The `EP rank` label appears in
every vLLM boot log on this build regardless of whether expert sharding is
happening — it is not diagnostic of anything on its own, on this build or
any build sharing this print statement.

**The broader, durable finding (#126) — this is the one worth carrying
forward, and it does not depend on EP at all:** FlashInfer's `choose_one`
has two nested loops with different safety properties. The *outer* loop,
over `profiles` (buckets), is safely rank-agnostic — bucket boundaries come
from `tuning_config`, identical by construction across ranks. But
`rank_tactics()` — called from inside `get_valid_tactics()`, itself called
once per bucket — has an *inner* loop over `valid_tactics`, a list whose
**length is computed fresh per call with zero cross-rank synchronization.**
If any two ranks' local shapes ever produce a different-length result here
— from expert-parallel sharding (ruled out for this incident), from plain
uneven tensor-parallel sharding of an expert's own weight matrices (a
dimension that doesn't divide cleanly by `tp_size`), or from anything else
— every call to `_profile_single_kernel()` inside that inner loop carries
its own `all_reduce()` (confirmed in #118), and collective cardinality
breaks silently. This is also the mechanism that explains why a hang can
present as dozens of *completed* outer-bucket progress-bar cycles before
failing: the actual divergence lives one level deeper than what the
visible progress bar tracks. **Any future TP-parallel MoE model on this
cluster — with or without EP — inherits this exact structural fragility.**
Tracing the specific trigger stopped at a JIT-compiled CUDA extension
boundary (`get_cutlass_fused_moe_module()` → `build_and_load()`); the
mechanism above is confirmed as real and possible, not confirmed as what
actually happened here. **Full call-chain map, triage checklist, and a
confirmed/ruled-out/open table: `REFERENCE-flashinfer-autotune-internals.md`**
— read that first if this recurs, before re-deriving any of this from grep.

**What this means for `llama-4-fp8-tp` specifically:** the real cause of
the original observation — TP0 hitting FlashInfer's baked autotune cache
instantly while TP1 live-tuned for ~48 minutes before the Gloo transport
crashed — remains genuinely unknown, and deliberately not pursued further
(#126) once the chase reached compiled code scoped to one FlashInfer
version, one vLLM commit, and one architecture unlikely to still matter by
the time anyone would act on it. Not pursued further, per #122's standing
decision, which holds regardless of mechanism.

**Why this section is being kept at all:** the underlying code paths are
real and worth knowing about structurally, even with zero confirmed
instances of either mechanism causing a problem on this cluster.
`_cache_key_extras()`'s EP-based divergence and `flashinfer_autotune()`'s
leader-only save gate (`vllm/model_executor/warmup/kernel_warmup.py`, no
persistent mount for the cache path) are both confirmed by direct source
read, and both would be real problems **if** a future deploy genuinely
runs with `enable_expert_parallel=True` under TP. `rank_tactics()`'s
unsynchronized inner-loop length is a real problem **whenever** any
per-rank shape divergence occurs at all, EP or not. Neither
`llama-4-fp8-tp` nor any other recipe on this cluster has been confirmed
to trigger either.

**The two things worth carrying forward, precisely stated:**
1. The boot log's `TP rank X, EP rank Y` line is **not evidence of
   anything** — do not use it to diagnose a future incident, on this build
   or any build sharing this print statement.
2. If a future TP-parallel MoE model shows the same instant-cache-hit-vs-
   long-live-tune split between ranks, this is a real, known-possible
   class of bug independent of EP — check `enable_expert_parallel`'s
   actual value first (ruling that specific variant in or out), but do not
   assume ruling out EP means ruling out this whole class. The underlying
   fragility is in `rank_tactics()`'s unsynchronized tactic-count, not in
   expert-parallelism specifically.

---

# 2. Dependency chains

**D-1 — the Qwen A/B chain: CLOSED 2026-09-03**

```
catalog-key naming (underscore, not dot)           CLEARED
  └─> ab_test.py --{side}-nodes bug (#105)         CLEARED
       └─> ab_test.py head-only pre-pull (#106)    CLEARED
            └─> A/B RUN COMPLETED                  ✓ TP wins ~1.9x, n=3/side
                 ├─> #105 + #106 now live-verified, not syntax-only
                 └─> decision unblocked: convert/retire qwen-2_5-coder-32b (PP)

llama-4-fp8's own A/B, run 2026-09-04:                                       ✓
  new recipe llama-4-fp8-tp.yaml, byte-identical to the PP original except
  tp_size/pp_size -- diffed programmatically before the run to confirm
    └─> RESULT IS A DEFECT, NOT A THROUGHPUT ANSWER: TP hangs 2/3 runs,
        eventually crashes with a Gloo TCP transport failure. Root cause
        arc, four entries, ending in CONFIRMATION not just hypothesis:
          #116: mid-hang py-spy -> _profile_single_kernel's all_reduce
          #117: outer world.barrier() confirmed symmetric on both ranks;
                over-broadly ruled out any collective asymmetry
          #118: SECOND, per-tactic all_reduce found one level deeper,
                reopens the mechanism at correct granularity; the
                rank-divergent-cache-key half unconfirmed
          #119: CONFIRMED -- MoERunner._cache_key_extras() bakes
                local_expert_offset into the cache key; this deploy's
                own boot log shows TP0/TP1 ARE EP0/EP1 -- genuinely
                different shards, different keys, different cache
                coverage for nominally the same profile step. Baked
                image cache covers one offset; the other rank live-
                tunes from scratch, EVERY deploy, permanently.
                [SEE #123 BELOW -- this "CONFIRMED" label itself got
                 reopened; do not read this line in isolation.]
        NOT PATCHABLE WITHIN THIS REPO -- flashinfer/fused_moe/ confirmed
        not this codebase's code, five times over. #116-#120.
        (#120: TP1's live results can't persist either -- leader-only
         save gate + no host-mounted cache path. General pattern,
         written up standalone in WS-9, not llama-4-fp8-specific.)
        (#121 CORRECTION: "not fixable" above was scoped too broadly --
         means "not by editing dgx-orchestrator.py/common/*.py," not
         "unfixable, period." The IMAGE itself is a different codebase
         with two real paths: upstream to whoever maintains
         eugr/spark-vllm-b12x, or a local rebuild if build access
         exists. Which applies here is unconfirmed, not assumed.)
        (#122 DECISION: neither path pursued -- no relationship with the
         fork maintainer, limited time, PP=2 already works. CLOSED, not
         open-and-waiting. If circumstances change, #119-#121 already
         have the exact code locations and patches needed.)
        (#123 REOPENING, after #122's decision was already made:
         enable_expert_parallel defaults FALSE in this exact build
         (docker run --rm against the image, vllm.__version__ confirmed
         matching), and the original failing deploy's own boot log
         never overrode it. The EP-rank labels #119 relied on may be
         routine parallel-state bookkeeping, not proof EP was sharding
         anything. #122's decision to not pursue further stands
         regardless -- but the mechanism #119-#121 describe may not be
         what actually happened. If ever revisited: check
         parallel_state.py's group-construction logic FIRST.)
        (#124-#125 CLOSED: parallel_state.py -- the file printing the
         EP rank label, confirmed via direct import -- has ZERO
         reference to enable_expert_parallel under ANY name, including
         the enable_ep rename multiproc_executor.py is confirmed to use
         elsewhere. The only two nearby conditionals gate on
         enable_eplb, a DIFFERENT flag (load-balancing, not the same as
         expert-parallel itself). The print is unconditional bookkeeping.
         EP was NOT active on this deploy. #119's mechanism did NOT
         cause this incident. THE REAL CAUSE IS UNKNOWN -- closing the
         EP hypothesis supplied no replacement one. #122's decision
         stands, now with less reason to ever reopen it: no confirmed,
         actionable mechanism remains even if time became available.)
        ├─> PP remains the only reliable topology for this model
        ├─> repeat 1's clean TP numbers held separately, not comparable
        ├─> cache-priming workaround now DEAD, not just downgraded --
        │   the divergence is structural (EP shard assignment), no
        │   amount of pre-warming touches it
        ├─> ONE real uninvestigated option surfaced by #119: does
        │   --enable-expert-parallel false (or equivalent) exist for
        │   this model/image? Would remove the rank-divergent key
        │   entirely, at whatever cost EP was buying. Not checked.
        └─> two dead-end hypotheses chased and ruled out along the way,
            before the (now confirmed, after three revisions) real cause
            was found (orphaned /dev/shm files; wrong cache-mount host
            path checked twice) -- correctly not written up as final
            at any of the three earlier stages

remaining in WS-1, CLOSED 2026-09-04:
  qwen-3.5-122b MTP token-depth sweep (mtp2 / tp[n=3] / mtp4)                ✓
    ├─> Ray versions confirmed matching (2.58.0, both hosts) before launch --
    │   #106's pre-pull fix held, no repeat of the version-drift crash
    ├─> run 1: mtp2 vs tp[n=3], --repeats 3, 54/54 checks, boot_log_hit=True
    ├─> run 2: mtp2 vs mtp4, --repeats 3, deployed=True/boot_log_hit=True
    │   both sides -- ran cleanly behind run 1 as queued, no race
    ├─> result: NO SINGLE BEST DEPTH -- extraction +17.5% n=2->n=4
    │   (monotonic up), coding -8.5% n=2->n=4 (monotonic down), creative
    │   -4% (mild monotonic down), default flat. Contradicts the "3 is
    │   best overall" framing this run was testing -- it's a genuine
    │   Pareto trade-off across categories, not a total ordering. Second
    │   independent confirmation of the DFlash sweep's core finding
    │   (speculative throughput is sharply workload-dependent), now on a
    │   different model and method. Full writeup: TOMBSTONES.md #115.
    ├─> source of the "3 best / 2 second / 4 too big" claim itself was
    │   never traced to a specific document before this run started --
    │   still not traced. Given the result contradicts it as a categorical
    │   claim, tracing the source matters less now than it would have if
    │   the run had corroborated it; not pursuing further unless the claim
    │   resurfaces elsewhere.
    └─> CORRECTION to a hypothesis in TOMBSTONES #113: this run shares
        #113's exact build string and correctly reports boot_log_hit=True
        both times, undercutting #113's "likely explains the coder A/B's
        boot_log_hit=False" build-drift guess. See TOMBSTONES #115 for the
        revised, more mundane explanation (keyword list vs. what these
        specific recipes' logs contain, independent of build).
```


**D-2 — the status-marker chain (prerequisite already paid)**

```
order-insensitive config_hash (#92)   DONE — recipes.py:222
mods in config_hash, schema 2 (#91)   DONE — recipes.py:326
  └─> per-recipe status: marker auto-promoting off config_hash   ← unblocked
       ├─> initial values need a catalog pass          ← UNBLOCKED (repo readable)
       └─> unknown-VLLM_* check folds in here, not separately

counter-pressure:
Phase 3 topology-key change ──(orphans every hash)──> the marker's data source
  └─> decide hash versioning BEFORE more data accumulates
```

**D-3 — the log-retention chain**

```
retain full vLLM container log per deploy
  ├─> makes the unknown-VLLM_* check trivial (grep a file, not race a stream)
  ├─> feeds the status marker ("launched without ignored config")
  └─> prerequisite for any live warning surface — a live scraper with
      no persistence still loses everything at teardown
```

**D-4 — the identity chain (this one has a strict order)**

```
tools/reconcile_ledger.py (read-only report)
  ├─> produces the evidence to decide #110's merge question
  └─> makes orphans distinguishable from real gaps
       └─> ONLY THEN: rename dotted recipes to underscore, one pass
            └─> ONLY THEN: load-time validator rejecting "." in a stem
                 (structural prevention, once nothing legitimate violates it)

hard ordering constraint: renaming before the tool exists manufactures
six orphans at exactly the moment nothing can classify them.
also: do not rename qwen-2_5-coder-32b until D-1 completes.
```

**D-5 — the mods correctness chain (an open question under a shipped feature)**

```
mods bake pipeline (MA/MB/MC)   SHIPPED
mods in config_hash (#91)       SHIPPED
  └─> does resolve_mod_tag() hash NAMES or CONTENTS?   UNANSWERED
       └─> if names: editing a mod's run.sh silently never reaches the
           container, and config_hash is correct while the image is not
```

**D-6 — the errata/linter chain**

```
errata.yaml (structured rules)          DELIVERED
  ├─> tools/lint_recipes.py reads it
  ├─> the human tuning reference renders FROM it (never maintained twice)
  └─> E015 (VLLM_USE_V1) RESOLVED 2026-09-03 by direct inspection —
       enforce: warn, scoped to the confirmed build string, not to
       "always". K9's experiment is moot; see TOMBSTONES #113. E014
       sits right next to it, deliberately NOT re-scoped without
       re-verification — the pattern to follow for any rule discovered
       stale after a build moves.
```

**D-7 — the Phase 3 fence**

```
spark-5/spark-6 racked  ──gates──>  Phase 3 proper
  but these want to land BEFORE the hardware:
  ├─> NCCL/Gloo ifname derivation out of recipe env_vars
  └─> config_hash topology-key versioning decision
```

---

# 3. Kickoff prompts

Paste-ready into a fresh chat. Each names what to send back, because
"report the contradiction rather than working around it" is the part that
keeps getting lost.

---

## K1 — Finish the Qwen TP-vs-PP A/B

> ### Context
>
> Repo `imrobertson/orchestrator`, two-node GB10 DGX Spark cluster, control
> plane off-node on `maestro`. You are finishing one measurement that three
> separate tool bugs have already been fixed to enable, and that has never
> completed.
>
> `qwen-2_5-coder-32b.yaml` (PP=2) versus `qwen-2_5-coder-32b-tp.yaml`
> (TP=2). The originally-intended pair (`qwen-3.6-27b-nvfp4`) is dead as a
> comparison: both files carry MTP speculative decoding, which is
> hard-incompatible with `pp_size > 1`, so the PP side cannot boot and TP
> would win by forfeit. See `TOMBSTONES.md` #104 and `errata.yaml` E005.
>
> ### Why this hasn't happened yet
>
> Four blockers in sequence, all cleared: catalog keys use underscores not
> dots; `ab_test.py`'s `any_override` counted `--{side}-nodes` as an
> override, making a 2-node catalog topology structurally unreachable
> (#105); the pre-pull only targeted the head, letting `spark-3` drift to a
> stale `:latest` and crashing 6/6 attempts on a Ray version mismatch
> (#106); and both `max_model_len` values were 262144 against a real 32768
> context, since corrected.
>
> Fixes #105 and #106 are `py_compile`-clean and have **never run against a
> live 2-node deploy**. This run is their verification as much as it is the
> measurement.
>
> ### What to do
>
> ```
> docker exec dgx-orchestrator-api python3 tests/ab_test.py \
>     --variant-a qwen-2_5-coder-32b \
>     --variant-b qwen-2_5-coder-32b-tp \
>     --a-nodes 2 --b-nodes 2 \
>     --prompts all \
>     --repeats 3
> ```
>
> Before trusting any result, confirm both hosts are on the same build:
> `docker run --rm <image> python3 -c "import ray; print(ray.__version__)"`
> on each. If they differ, `docker pull` by hand on whichever is behind and
> **say so** — that means the #106 fix did not do its job.
>
> ### What to report back
>
> Per-prompt-category mean and range across the 3 repeats for both sides,
> not one aggregate number: speculative and topology effects on this cluster
> are sharply workload-dependent (an identical DFlash config measured
> 49–203 tok/s across four prompt categories). State plainly if the run
> failed and at which stage. A failed run that identifies a fifth blocker is
> a better outcome than a number produced by quietly working around one.
>
> ### Do not
>
> Do not convert any other `pp_size: 2` recipe on this result alone until
> it's discussed — `llama-4-fp8`'s confirmed-`ready` PP=2 deploy is direct
> in-house proof PP can be correct on this exact stack for at least one
> model. `qwen-2_5-coder-32b` needs no rename (already underscore-form) and
> its A/B completed 2026-09-03 — nothing left to sequence around there.

---

## K9 — Settle `VLLM_USE_V1=0` (run it to ground)

> ### Context
>
> Repo `imrobertson/orchestrator`. One recipe rule is contested across three
> mutually incompatible pieces of evidence, and it needs to be resolved
> precisely because it is about to become a linter rule (`errata.yaml`
> E015 -- now `enforce: warn`, scoped precisely to the build string it was
> confirmed dead on, per TOMBSTONES #113. Left as the worked example for
> *why* scope-then-widen matters: E014 sits right next to it, still scoped
> to the older build, deliberately NOT widened without re-verification --
> see E014's own `needs_reverification` note.
>
> The three positions, all real:
>
> 1. **Required.** `TROUBLESHOOTING.md` Incident #1 / `TOMBSTONES.md` #43
>    (V4.8.1): 2-node cross-host topologies must pass
>    `--distributed-executor-backend ray` **and** set `VLLM_USE_V1=0`.
>    Written from a real observed failure —
>    `AssertionError: collective_rpc should not be called on follower node`.
> 2. **Inert.** gemma-4-31b 2-node, 2026-08-29: the variable was set and the
>    engine logged `Initializing a V1 LLM engine` anyway.
> 3. **Unnecessary.** `deepseek-v4-flash-0731-dspark::2_node`, 2026-09-02:
>    never set at all; ran `distributed_executor_backend: 'ray'` and reached
>    `ready` with real phase data.
>
> ### The framing that matters
>
> The original rule bundles two claims that are not the same: *use the Ray
> executor* and *force the V0 engine*. The failure it was written from is an
> `mp`-backend failure across physical hosts, and the fix that provably
> addresses that is the Ray flag. `VLLM_USE_V1=0` rode along on the theory
> that V0's Ray executor was the thing that worked. It may be required in
> some configurations and harmful in others — or it may simply be inert on
> this build because no V0 path remains. Test, don't assume.
>
> ### Step 0 — thirty seconds, may close the question outright
>
> On the current build (`v0.1.dev20003+gad848fc41.d20260815`), inside the
> image, determine whether a V0 engine path still exists at all and whether
> the variable is even read:
>
> ```
> docker run --rm <image> python3 -c "import vllm.envs as e; print(e.VLLM_USE_V1)"
> docker run --rm -e VLLM_USE_V1=0 <image> python3 -c "import vllm.envs as e; print(e.VLLM_USE_V1)"
> ```
>
> Then check whether the installed vLLM still ships a V0 executor/engine
> module at all. If it does not, the variable is structurally inert on this
> build, the question closes without a deploy, and the remaining work is
> purely documentation. Report this before doing anything expensive.
>
> ### Step 1 — the A/B, if step 0 doesn't settle it
>
> **This cannot be run through `tests/ab_test.py`.** `--{side}-docker-env`
> counts as an override, and any override forces the 1-node ad-hoc path
> (`ab_test.py:692` excludes only `"nodes"` from `any_override`), while
> scratch recipes are 1-node only. So:
>
> - Create **two committed catalog recipes**, identical except that one sets
>   `VLLM_USE_V1=0` in `env_vars` and the other does not. Two recipes means
>   two `config_hash` values and clean ledger separation — that is a feature,
>   not overhead.
> - Run both as 2-node deploys through the normal path.
> - Do this on at least two structurally different recipes: one plain
>   TP=2 (e.g. a `qwen-2_5-coder-32b`-shaped config) and one carrying a
>   speculative config, since the hypothesis "needed sometimes" most
>   plausibly turns on engine features rather than on the topology alone.
>
> ### What to capture per run
>
> - Whether the boot log says `Initializing a V1 LLM engine` or a V0
>   equivalent.
> - The resolved `distributed_executor_backend` from the engine config line.
> - Whether the deploy reached `ready` **and served a real request** — not
>   just `/health`. `TOMBSTONES.md` #89 is the cautionary case: a model that
>   booted cleanly and passed `/health` crashed on the first real generation.
> - The `config_hash` for each side, so the ledger rows are attributable.
>
> ### What to report back
>
> **This has been answered — K9 is now historical, not a live task.**
> Direct inspection (2026-09-03) found `vllm.envs` no longer defines
> `VLLM_USE_V1` at all on the current build, and the whole V0 executor
> package is absent — `errata.yaml` E015 records the verdict as
> `known_bad`/`enforce: warn`, scoped to that exact build string, not to
> "always". `TOMBSTONES.md` #113 has the full writeup. If this prompt is
> ever reused for a *future* recurrence of the same question (a different
> contested env var, or this one again after another build move), follow
> the pattern E015 now demonstrates: scope the verdict to the specific
> build checked, and leave any older build-scoped rule (like E014, sitting
> right next to it) untouched rather than assuming it still holds.
>
> If the answer is "inert on this build," **do not mass-strip the variable
> from existing recipes.** Every `env_vars` change alters
> `compute_config_hash()` and resets that recipe's launch-success history.
> Strip opportunistically as recipes are re-validated.

---

## K2 — Recipe linter reading `errata.yaml`

> ### Context
>
> Repo `imrobertson/orchestrator`. Several recipes can be selected and
> launched from the dashboard or CLI but are known or suspected to fail,
> wasting a real cold-start cycle (sometimes 30+ minutes).
>
> `errata.yaml` already exists and holds 17 rules distilled from real
> incidents, each with `confidence`, `enforce`, `scope`, and `evidence`.
> **You are building the consumer, not the rules.** Do not invent new rules;
> new ones come from real incidents only.
>
> ### Before you start — a stale-documentation warning
>
> If you encounter a claim that `compute_config_hash()` hashes `vllm_args`
> as a raw string, it is out of date. `common/recipes.py:222` implements
> `_canonicalize_vllm_args()` and `_CONFIG_HASH_SCHEMA = 2` includes `mods`
> as an ordered list. Read the code, not the older prose.
>
> ### Goal
>
> `tools/lint_recipes.py`, plus optional soft warnings at `load_recipes()`
> time, evaluating every recipe/topology against `errata.yaml`.
>
> ### Hard requirements
>
> 1. **Never hard-fail the catalog.** `build_catalog_response()` fails
>    closed, so a single linter exception would empty the entire model list
>    with nothing surfaced to the dashboard user — this has happened
>    (`TOMBSTONES.md` #41). Report; never raise out of `load_recipes()`.
> 2. **Honour `enforce`.** `enforce: none` rules are documentation. The
>    linter reads them so a human can query them; it must not emit findings
>    for them. E015 (`VLLM_USE_V1`) is a worked example of the opposite
>    case: it started at `none` pending an experiment, and is now `warn`
>    because the experiment (well, direct inspection) actually happened —
>    honour whichever state a rule is in; don't assume `none` always means
>    "leave it alone forever."
> 3. **Honour `scope`.** A rule proven on one build or image must not fire
>    outside it. The cautionary case is `VLLM_BASE_DIR`: image-inherited, no
>    recipe can remove it, so a checker that flags it fires forever and
>    trains people to ignore the output.
> 4. **Render the human doc from the same file.** Do not maintain a prose
>    tuning reference by hand alongside this — maintaining two copies is
>    exactly how the previous tuning reference came to contradict its own
>    incident log.
>
> ### Also in scope if it stays small
>
> An optional `status:` field on the recipe schema
> (`validated`/`unconfirmed`/`known-bad`, defaulting to `unconfirmed`),
> auto-promoted from `PENDING_LAUNCH_STATE`'s existing `config_hash`-keyed
> launch-success tracking, surfaced as a badge in the dashboard dropdown and
> `dgx-config status`. The prerequisite (an order-insensitive hash) is
> already done. Assigning **initial** values needs a pass over
> `recipes/local/` (24 files; `recipes/eugr/` is empty apart from a
> `.gitkeep`, so ignore every doc reference to it). Readable directly from
> the repo — this half is no longer blocked on an upload.

---

## K3 — Read-only ledger reconciliation, then a naming migration

> ### Context
>
> Repo `imrobertson/orchestrator`. Nothing today can look at two similar
> ledger keys and say what their relationship is. Two real incidents:
> `deepseek-v4-flash-nvfp4` vs `-0731-nvfp4` (two legitimately separate
> recipes, keys one keystroke apart, wrong one deployed — #57), and
> `nemotron-3.5-lightning-bf16` vs `nemotron-3_5-lightning-bf16` (#110).
>
> The catalog is also mixed on convention: roughly 6 keys use a dot as a
> decimal separator, 3 use an underscore. The decision of record is to
> converge on **underscore** — because HF model names always carry the
> literal dot, so a key containing one is then immediately identifiable as
> derived from the served name rather than from a recipe file.
>
> ### Part 1 — `tools/reconcile_ledger.py` (read-only)
>
> For every key in `model_ledger.json`:
> 1. Does a live recipe file with that exact stem exist?
> 2. If not, `git log --follow` across `recipes/` for a file that once had
>    this stem — separating "renamed/retired" from "never committed."
> 3. For any two keys within a small edit distance, compare their most
>    recent `config_hash`. `config_registry.json` already decodes a hash
>    back to its exact payload; use it.
>
> **Output a report. Never merge, rename, or delete.** This repo has already
> made that mistake once: `TOMBSTONES.md` #91 originally cited two ledger
> hash "collisions" as evidence of a bug, and both turned out to be renames
> working exactly as designed. That correction is recorded rather than
> quietly revised, and it is why this tool reports instead of acting.
>
> ### Known inputs — one already resolved, one not
>
> **#110's recipe-level cause is resolved; the ledger side is what you're
> reporting on.** Two recipe files used to carry the identical `hf_path`
> (`nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16`):
> `nemotron-3.5-lightning-bf16.yaml` (no `image:` field, `1_node` only) and
> `nemotron-3_5-lightning-bf16.yaml` (`eugr/spark-vllm-b12x:latest`,
> `1_node` + `2_node`). Both satisfied `_resolve_catalog_key()`'s match
> condition — #77's documented ambiguity, first-match-wins by dict iteration
> order. **The dot-form file has since been deleted by the operator.** Only
> `nemotron-3_5-lightning-bf16.yaml` remains, so this specific collision is
> closed at the recipe level.
>
> What's left is the ledger: `nemotron-3.5-lightning-bf16::1_node` and
> `::2_node` are now orphans of a deleted file — the ordinary rename/delete
> case, same shape as the pre-existing `-dspark-sm120`/`-gb10-hazyumps-512k`
> orphans from #91. Confirm your tool classifies them that way (recipe
> absent, no `git log --follow` history needed since the deletion is
> recent and known) rather than flagging them as a live collision, which
> they no longer are.
>
> Provenance, so you don't re-derive it: `lifetime`/`last_seen_raw` come from
> `SessionTracker._commit_session()`, keyed on `self.model` from
> `SESSION_TRACKER.update(…, matched_model, topo)` where
> `matched_model = _resolve_active_recipe(...)[0]`; `launch_history` comes
> from `_record_launch_success(pending["model"], …)` on the deploy path.
> `_resolve_active_recipe()` prefers `ACTIVE_DEPLOYMENT_STATE`'s exact key
> and otherwise falls back to `_resolve_catalog_key()`, whose no-match
> fallback returns `loaded_model` **unchanged** — which is what ruled out
> "telemetry derives its key from the served name" as the mechanism; that
> fallback would have produced the raw HF basename, not a recipe-stem-shaped
> key.
>
> **The two orphaned keys are not equivalent — do not treat them
> identically in your report.** `::2_node` holds only `lifetime` and
> `last_seen_raw`, no `launch_history`, no `runs[]` — no diagnostic content.
> `::1_node` holds real phase data: `cached [597,611,551]`,
> `compiled [571,674,1017,3001]`, a full `runs[]` record. Recommend (report
> only — the operator will act separately via `clean_ledger.py`): drop
> `::2_node` outright; rekey `::1_node` to
> `nemotron-3_5-lightning-bf16::1_node` and drop only its `launch_history`,
> since that hash was computed against the now-deleted file and can never
> join to the surviving recipe (which adds `image:`) regardless of what
> happens to the rest of the entry.
>
> On why this is a rekey-and-drop rather than a merge: both 2-node keys
> carried an **identical** `last_seen_raw` (`p: 93.0, g: 768.0`) while their
> `lifetime` totals differed (124/1487 vs 62/709) — consistent with
> overlapping rather than disjoint spans of the same engine lifetime, in
> which case summing would have double-counted. Same hazard
> `TOMBSTONES.md` #72 documents for `correct-ledger`'s set-vs-add
> semantics, where `--force` cannot catch the multi-lifetime case. This
> reasoning is for your report's general methodology, not a decision to
> re-litigate for this specific pair — a merge was never proposed here.
>
> Note also that `SessionTracker`'s `topo` is derived from the count of
> hosts with an active container, not from the deployed recipe — which is
> how a `::2_node` ledger key can exist for a file that only ever had a
> `1_node` block. Expect that pattern elsewhere; it is not corruption.
>
> ### Part 2 — only after Part 1 exists
>
> Two pieces, in order.
>
> **(a) Resolve the confirmed duplicate.** `nemotron-3_5-lightning-bf16.yaml`
> is clearly authoritative (newer, has `image:`, has the working `2_node`,
> live-deployed 2026-09-03). The dot file is stale and additionally carries
> the `image:`-absent trap that `errata.yaml` E004 exists for. Deleting it
> orphans `nemotron-3.5-lightning-bf16::1_node`'s real `launch_history` and
> `runs[]` — which is precisely why Part 1 comes first.
>
> **(b) Rename the remaining dotted stems to underscore, in one pass.** The
> full set, verified against `recipes/local/` (note `recipes/eugr/` contains
> only a `.gitkeep` — it is empty, so the catalog is `recipes/local/`
> entirely):
>
> ```
> llama-3.3-70b.yaml              -> llama-3_3-70b.yaml
> nemotron-3.5-lightning-nvfp4.yaml -> nemotron-3_5-lightning-nvfp4.yaml
> qwen-3.5-122b-tp.yaml           -> qwen-3_5-122b-tp.yaml
> qwen-3.5-122b-mtp2.yaml         -> qwen-3_5-122b-mtp2.yaml
> qwen-3.5-122b-mtp4.yaml         -> qwen-3_5-122b-mtp4.yaml
> qwen-3.8-27b.yaml               -> qwen-3_8-27b.yaml
> qwen-3.8-27b-nvfp4.yaml         -> qwen-3_8-27b-nvfp4.yaml
> qwen-3.8-27b-nvfp4-sqk2.yaml    -> qwen-3_8-27b-nvfp4-sqk2.yaml
> nemotron-3.5-lightning-bf16.yaml -> DELETE (duplicate, see (a))
> ```
>
> Then add a load-time validator rejecting `.` in a recipe stem, so
> recurrence is structurally impossible rather than a convention.
>
> `qwen-2_5-coder-32b` needs no rename (already underscore-form) and its
> A/B (K1) completed 2026-09-03 with a decisive result — nothing gates
> starting this pass now.
>
> ### Also fold in — and harden the resolver
>
> `benchmark.py --model-key` can silently mismatch the recipe actually
> benchmarked; an audit of existing `benchmark_ledger.csv` rows has never
> been done. `BACKLOG-dspark-sm120-image.md` open item 5 is the same
> incident, independently recorded — treat that as corroboration, not a
> second bug.
>
> Separately worth doing while here: make `_resolve_catalog_key()` *detect*
> an ambiguous match (more than one catalog entry satisfying its condition)
> rather than silently returning the first. Today, dict iteration order is
> load-bearing and nothing says so.

---

## K4 — Two cheap verification questions left by the config-hash work

> ### Context
>
> Repo `imrobertson/orchestrator`. Two standalone verification tasks, both
> flagged open in `TOMBSTONES.md` #91. They are grouped because they are the
> same mistake one layer apart: a comment asserting a field is inert is a
> claim about a point in time, and it does not update itself when the field
> gets wired up.
>
> ### Question 1 — does `resolve_mod_tag()`'s digest hash mod NAMES or CONTENTS?
>
> If names: editing `mods/<n>/run.sh` leaves the derived tag unchanged,
> `ensure_mods_baked()` treats it as a cache hit, skips the rebake, and the
> edit **silently never reaches the container** — while
> `compute_config_hash()` still reports no change, because `mods` is hashed
> by name there too (correctly, per #91).
>
> Settle it with two `--dry-run` invocations either side of a real edit to a
> mod's `run.sh`, comparing the resolved tag.
>
> **Do not paste raw dry-run output.** `docker_run_commands` is the literal
> argv including `-e HF_TOKEN=<real token>`, and this has already leaked a
> live credential into a conversation once (`TOMBSTONES.md` #86). Report
> only the tag strings.
>
> ### Question 2 — is `capability` still safe to exclude from `config_hash`?
>
> Excluded on the same "inert metadata" premise that went stale for `mods`,
> never re-verified. `capability` is confirmed *intentionally* unused today,
> reserved for a Phase 4 feature where an agent queries the orchestrator for
> task-suited models — which rules out "dead code, delete it" but does not
> answer the exclusion question.
>
> The check that matters: trace whether any `capability` value can reach
> `docker run`, the entrypoint, or anything that changes what the container
> executes — the same trace that made `mods` a real defect. If it cannot,
> re-state the exclusion **with a date**, rather than leaving it as an
> inherited assumption.
>
> ### What to report back
>
> A direct answer to each, with evidence. If Question 1 comes back "names,"
> say so and stop — the fix is a separate decision, not something to bolt on.

---

## K5 — Retain the full vLLM container log per deploy

> ### Context
>
> Repo `imrobertson/orchestrator`. When a deployment is torn down,
> `docker rm` deletes the container's log with it. The only record of what
> vLLM actually said — resolved backends, whether a quantization flag took,
> ignored env vars, a crash traceback — is gone. `TOMBSTONES.md` #7 solved
> this for Ray crash logs via a persistent bind mount; nothing does the
> equivalent for the vLLM container's own stdout.
>
> In one session that log answered: which attention backend actually
> resolved, whether `--quantization fp8` took, that `VLLM_USE_V1=0` was
> being ignored, that `VLLM_BASE_DIR` is image-inherited. All of it would
> have been unrecoverable an hour later. It is also the prerequisite for the
> unknown-`VLLM_*` env-var check and for the `VLLM_USE_V1` experiment's
> evidence capture.
>
> ### Check this first — it can invalidate the premise
>
> Whether the Docker daemon on the Sparks sets `log-opts
> max-size`/`max-file` in `/etc/docker/daemon.json`. If so, the json-file
> log is already rotating and older lines may be gone before anything
> captures them — which turns this into a logging-driver configuration
> problem instead.
>
> ### Design (settled — do not redesign)
>
> Docker persists container stdout continuously via `json-file`; containers
> run `-d` and **not** `--rm`, so a crashed engine's output survives the
> container exiting. Nothing needs flushing. So:
>
> 1. **Capture at teardown, before `docker rm`** — the one moment the data
>    is actually at risk.
> 2. **A low-frequency incremental safety net** (`docker logs --since
>    <last_capture>`, 60s is ample), because teardown alone is fragile here
>    specifically: teardown reporting success on per-host failure is a
>    documented bug class, and a host reboot, a manual `docker rm`, or a
>    `docker system prune` all bypass the teardown path entirely. It must be
>    incremental — a full re-read re-ships the whole log every minute.
>
> **Do not stream `docker logs -f` from the daemon.** A long-lived follow
> process per container is another thing to leak, and orphaned children are
> already a known bug class here (#81).
>
> ### Storage
>
> Reuse `~/.cache/ray-logs/<deploy_run_id>/<host>/` — already per-deploy,
> already persistently bind-mounted, already covered by
> `dgx-config prune-ray-logs`. Add a **separate**
> `vllm_log_retention_hours` to `cluster_config.yaml`'s `tuning:` block
> (default 24); Ray crash dumps are tiny and a full vLLM log is not.
>
> **Keep whole logs initially. Do not truncate.** A head/tail policy is the
> likely end state, but picking the numbers before profiling real sizes is a
> guess — and the failure modes this is most valuable for are the *slow*
> ones (Ray memory-monitor OOM kill, multi-hour session freeze), where
> head/tail discards precisely the window the degradation is visible in.
> Measure across a DeepSeek 512K deploy and a short Gemma one, then decide.

---

## K6 — Stop `--dry-run` from emitting live credentials

> ### Context
>
> Repo `imrobertson/orchestrator`. `docker_run_commands` in a `--dry-run`
> response is the literal argv `docker run` would receive, including
> `-e HF_TOKEN=<real token>` whenever `get_hf_token()` finds one.
>
> `--dry-run` reads as "nothing real happens," which makes its output feel
> safe to paste into a bug report or a chat. This is not hypothetical: it
> became a real incident during Task MC's verification when a dry-run
> response containing a real token was pasted into a conversation while
> confirming the output looked correct (`TOMBSTONES.md` #86). No fix has
> been applied. It also constrains K4, which needs dry-run output.
>
> ### The decision to make deliberately
>
> Either mask any `-e (HF_TOKEN|.*_TOKEN|.*_KEY)=...` value before it is
> ever added to a response dict — so `--dry-run`, `docker_run_commands`, and
> any future JSON or log surface are safe to paste anywhere — or document
> loudly that dry-run output is sensitive. Masking is probably right, but it
> touches the same code path real deploys use to build `env_flags`, which is
> why #86 says it wants its own task rather than being bolted onto an
> unrelated one.
>
> ### Read before writing the regex
>
> `TOMBSTONES.md` #94: redacting `authorization: <value>` with `\S+`
> absorbed the word `Bearer` and wrote the actual credential immediately
> after a `***REDACTED***` marker — output that looks *more* redacted than an
> untouched line. The rule that came out of it: **a redaction test must
> assert the secret string is absent, never that the marker appeared.** The
> latter would have passed.

---

## K7 — Interface names out of recipes, before the next pool exists

> ### Context
>
> Repo `imrobertson/orchestrator`. Every 2-node recipe carries
> `NCCL_SOCKET_IFNAME=enp1s0f0np0` and `GLOO_SOCKET_IFNAME=enp1s0f0np0` in
> `env_vars`. `cluster_config.yaml` *also* declares `network.interface:
> enp1s0f0np0` and `network.nccl_ib_hca: rocep1s0f0`. Same value, two
> places, and the recipe copy is what reaches `docker run`.
>
> Same class as the `PRIMARY_HOST`/`SECONDARY_HOST` hardcoding eliminated in
> V4.8.5 (`TOMBSTONES.md` #73): host identity in a file that shouldn't own
> it. Invisible today because there is one pool with one NIC name. The
> moment a second fabric pool exists with different interface names, **every
> 2-node recipe is silently wrong for that pool**, with no mechanism to vary
> it per-pool because it is baked into per-model YAML.
>
> ### Why now, when Phase 3 is hardware-gated
>
> The deadline is external. This wants to land *before* the hardware
> arrives; afterwards it is a break-every-recipe migration.
>
> ### Confirmed prior art
>
> eugr does not do this. `launch-cluster.sh`'s `get_env_flags()` injects
> `NCCL_SOCKET_IFNAME`, `MN_IF_NAME`, `UCX_NET_DEVICES` and per-node
> `VLLM_HOST_IP`/`RAY_NODE_IP_ADDRESS` from values `autodiscover.sh` derived
> at launch. Their recipes carry no interface names at all.
>
> ### Goal
>
> - Derive the interface env vars in the deploy path from
>   `cluster_config.yaml`'s `network:` block (eventually per-pool), and
>   strip them from recipe `env_vars`.
> - Confirm whether the 2-node loop sets `VLLM_HOST_IP` and the Ray node-IP
>   vars **per host** or once for all hosts. eugr sets them per-node
>   explicitly; getting this wrong across pools would be subtle and hard to
>   attribute.
> - Decide whether the mesh-vs-switched variable set belongs in
>   `cluster_config.yaml` as data or in code keyed off `network.topology`,
>   which already reserves the distinction with `switched`.
>
> ### Warning
>
> Every `env_vars` change alters `compute_config_hash()` and resets that
> recipe's launch-success history. Stripping these catalog-wide invalidates
> every 2-node recipe's validation history at once. Decide deliberately
> whether to accept that in one pass or strip opportunistically.

---

## K10 — Retire `models.yaml`

> ### Context
>
> Repo `imrobertson/orchestrator`. Phase 2 (per-model recipe files replacing
> the monolithic `models.yaml`) is complete. `models.yaml` survives only as
> a fallback reachable by setting `USE_LEGACY_CATALOG=1`. Removing it is
> mechanical; the surface is fully mapped in `WORKSTREAMS.md` WS-0a, which
> you should read first rather than re-deriving it.
>
> ### Scope
>
> Six touchpoints: the `MODELS_YAML_PATH` constant
> (`dgx-orchestrator.py:120`), `_load_model_catalog_legacy()` (2932-2955),
> the branch in `load_model_catalog()` (3034), the
> `USE_LEGACY_CATALOG != "1"` guard in `_execute_deployment_impl` (3790), a
> comment at 3524-3530, and `models.yaml` itself at the repo root.
>
> ### Two traps
>
> 1. **Do not touch `legacy_hosts_dict()`.** It is imported at line 46 and
>    used at line 192 for `HOSTS`. It concerns the *cluster host* config
>    shape and has nothing to do with `models.yaml` beyond sharing the word
>    "legacy."
> 2. **`GLOBAL_HF_HUB_OFFLINE` / `GLOBAL_TRANSFORMERS_OFFLINE` are applied
>    inside the function being deleted.** `_load_model_catalog_legacy()`
>    rewrites each topology's `env_vars` to honour them. Confirm the recipe
>    path has an equivalent before deleting, and say so explicitly in your
>    report. If it does not, that is a behaviour regression, not a cleanup —
>    stop and report rather than working around it.
>
> ### Verification
>
> `--dry-run` diff a representative 1-node and 2-node deploy before and
> after the change; the constructed `docker run` argv must be byte-identical.
> That before/after construction-diff is this repo's own established
> standard for a refactor that claims to preserve behaviour — see
> `TOMBSTONES.md` #90, where a visually clean refactor silently reordered
> flags and only an explicit argv diff caught it. **Do not paste raw dry-run
> output** — it embeds `HF_TOKEN` in plaintext (#86); diff it locally and
> report the result.
>
> Then `grep -rn "USE_LEGACY_CATALOG\|models\.yaml" .` should come back
> clean apart from `TOMBSTONES.md` history.

---

## K8 — Documentation consolidation

> ### Context
>
> Repo `imrobertson/orchestrator`. A synthesis pass on 2026-09-03 replaced
> `ROADMAP.md` and `ARCHITECTURE-MIGRATION-PLAN.md` with `DIRECTION.md`
> (direction and decisions of record) and `WORKSTREAMS.md` (the backlog),
> and extracted `TROUBLESHOOTING.md`'s recipe tuning reference into
> `errata.yaml` (machine-readable, the linter's source of truth).
>
> That pass found seven places where a document described a state the code
> had moved past — all in the same direction, docs behind code. The full
> list with file/line evidence is in `WORKSTREAMS.md` WS-0.
>
> ### What to do
>
> 1. Commit `DIRECTION.md`, `WORKSTREAMS.md`, `errata.yaml`.
> 2. Trim `TROUBLESHOOTING.md` to the incident log only (#1–14). The tuning
>    reference is now `errata.yaml`; render any human-readable version from
>    it rather than maintaining a second copy by hand.
> 3. Work the per-file disposition table in `DIRECTION.md`'s Documentation
>    map — it covers all 26 files in `docs/`, so nothing is retired by
>    omission. Three need care before archiving:
>    - `PHASE-MODS-PROMPTS.md` records only M0's result; append MA/MB/MC's
>      or it reads as though the sequence stalled at the gate.
>    - `M*-REVIEW.md` is ~166 KB. Confirm nothing durable exists only there
>      — `TOMBSTONES.md` #85 cites `M{X}-REVIEW.md`'s "Contradictions"
>      section directly.
>    - `REFERENCE-dspark-shared-expert-fix.md` is referenced but not
>      duplicated anywhere. It must survive.
> 4. Merge `EUGR-NOTES-UPDATE-2026-08-29.md` into `EUGR-REFERENCE-NOTES.md`
>    and fold `UsageShortcut.md` into `USERMANUAL.md`.
> 5. **Grep `docs/` for every recipe filename and fix dangling references.**
>    At least three recipes have been deleted or renamed while prose kept
>    citing them: `deepseek-v4-flash-0731-nvfp4.yaml` (cited in
>    `TROUBLESHOOTING.md` as the validated fp8-on-MLA example),
>    `qwen-3.5-122b.yaml` (retired per #107), and the `qwen-3.6-27b-nvfp4`
>    PP file. Also strip references to `recipes/eugr/*.yaml` — that
>    directory holds only a `.gitkeep`.
> 6. Update in-code doc pointers naming retired files — several exist in
>    `dgx-orchestrator.py` and `common/recipes.py` docstrings.
> 7. Record the reconciliation in-file rather than silently.
>    `TOMBSTONES.md`'s own 2026-08-31 header comment is the precedent.
>
> ### The rule this is meant to establish
>
> A document describing *current state* must be checkable against code or an
> artifact. Where it isn't, it belongs in `TOMBSTONES.md` (history,
> immutable) or `DIRECTION.md` (intent, rarely changes). Every stale entry
> found in this pass was in the third category — a status claim in a file
> nobody re-read when the code moved.
>
> ### What to send back
>
> Changed files in full, not diffs. And anything found that contradicts the
> WS-0 list — a contradiction found during the work is worth more than a
> clean completion.

---

# 4. Open flags

Reduced from eleven to six; five were resolved by the operator or by reading
the code. Nothing here is silently decided.

**Resolved since revision 1:**
`TOMBSTONES.md` #110 is present in the current file (rev 1 had a snapshot
ending at #109). The three Qwen deploys with no ledger key are real and
confirmed — handoff ledger copies lag `maestro`, and that caveat is now
recorded at the top of this file. Naming convergence is decided (underscore,
sequenced in D-4). The two stale ROADMAP entries are cited with file/line
evidence in WS-0 and slated for removal. `VLLM_USE_V1=0` is promoted from a
flag to a workstream with an experiment (K9).

**F-a — `resolve_mod_tag()`: names or contents?** Unanswered, and it sits
underneath a shipped feature. If names, a mod edit silently never reaches
the container while every hash correctly reports no change. K4.

**F-b — `TOMBSTONES.md` #95's series order.** #95 quotes
`gemma-4-31b::2_node` downloaded as `[440, 462, 14276]` and its reasoning
leans on list order being chronological. The ledger holds the same three
values in a different order: `[462, 14276, 440]`. Cannot tell whether the
entry quoted a reordered copy, a sample was appended and one trimmed, or the
chronological-order premise is wrong — and that premise is load-bearing for
#95's argument that the cold sample is never the oldest entry.

**F-c — the orphaned `nemotron-3.5-lightning-bf16::1_node` key's hash
mismatch.** (Dot form — the deleted recipe's ledger key, not the surviving
`nemotron-3_5-lightning-bf16`.) Its `launch_history` hash
(`65c268515202a4f7`) does not match its own run's `config_hash`
(`7f46f3161f0d6a4d`). Possibly expected (config changed between the
recorded success and the archived run, back when the file still existed),
possibly another symptom — not resolved by the recipe's deletion, since
the question is about the ledger's internal consistency at the time the
data was written, not about whether the file exists now. Not addressed
anywhere in the docs. Feed this into the same `clean_ledger.py` pass
handling this key's rekey/drop (WS-4) — worth knowing before deciding
whether to trust the `runs[]` record being carried over. The reconciliation
tool should report this pattern generally, not just for this key.

**F-d — RESOLVED.** `BACKLOG-dspark-sm120-image.md` is genuinely closed on
its headline result, and its catalog trim was actually executed (verified
against the repo). Five real open items survived inside it and are now in
WS-9. One correction to the previous revision of this file: it claimed
`REFERENCE-dspark-shared-expert-fix.md` must survive the archive pass. **That
file does not exist** — the claim was propagated from the backlog's own "saved
as" wording without verification. See F-i.

**F-i — `REFERENCE-dspark-shared-expert-fix.md` does not exist.**
`BACKLOG-dspark-sm120-image.md` states the tonyd2wild write-up was "saved
as" that filename. It is absent from both `docs/` and the repo root
(verified). Revision 2 of this file repeated the claim and marked the file
as must-keep; that was propagation of an unverified assertion, not
verification. The finding it supports — the shared-expert loader bug does
not apply to our image, confirmed by direct source trace — is preserved in
`TROUBLESHOOTING.md` and WS-9, so nothing load-bearing is lost. Either
re-save the reference from tonyd2wild's repo or drop the citation. Treat
this as an instance of the same class the doc consolidation exists to fix: a
"saved as X" claim is a status claim, and status claims go stale.

**F-g — Documents cite recipes that no longer exist.**
`TROUBLESHOOTING.md` names `deepseek-v4-flash-0731-nvfp4.yaml` as the
working example for its validated `--kv-cache-dtype fp8`-on-MLA rule; that
file has been deleted (correctly, per the DSpark backlog's trim). The rule
is still right, the example is stale. Corrected in `errata.yaml` E001.
Expect more of these: the trim, the `qwen-3.5-122b.yaml` retirement (#107),
and the `qwen-3.6-27b-nvfp4` PP file's removal all left prose references
behind. A grep for recipe filenames across `docs/` during the WS-0 pass
would catch the rest cheaply.

**F-h — The dot/underscore split is wider than the ledger showed.** The
ledger surfaced one pair. The repo shows the convention is genuinely mixed
across eight more files, and that `qwen-3_6-27b-nvfp4-tp.yaml` (underscore)
is what actually exists while every document refers to it as
`qwen-3.6-27b-nvfp4` (dot). The rename list is in K3 Part 2.

**F-e — every throughput number in the doc set is a single measurement.**
40.9 (`qwen-3.5-122b-tp`), 48.4 (`nemotron-3.5-lightning-bf16::2_node`),
87.0 (`nemotron-3-nano-30b-a3b-nvfp4::2_node`), 12.0 (`gemma-4-31b` TP=2),
42–45 (DSpark) are all one 3-pass run each, reported in prose, held in
`benchmark_ledger.csv`. `model_ledger.json` independently corroborates that
the underlying *deploys* happened, which is the strongest available
cross-check, but holds no throughput data. This is not a criticism of the
numbers — it is the reason `--repeats N` exists and why single-measurement
comparisons of speculative configs are explicitly untrusted here.

**F-f — RESOLVED.** `SESSION-CLOSEOUT-2026-09-02-FINAL.md` is present in
`docs/` (9.8 KB, 2026-09-02); it simply wasn't in the handoff. Its content
is ported into WS-5. Diff the port against the original before archiving.

---

# 5. On the architecture review

`DIRECTION.md` closes with the five recurring bug classes derived from
`TOMBSTONES.md` #27–#110. The short version, because it bears on
sequencing here:

Classes 2 (failures returning plausible values), 4 (state inferred from log
text), and 5 (config-to-argv construction) are being managed adequately —
each has a working convention and targeted fixes that hold.

Classes 1 (identity derived independently in many places) and 3 (the control
plane reasoning about processes it cannot see) keep producing new instances
*despite* having been fixed several times each. #110 is class 1's newest
instance, arriving after `config_hash`, `_resolve_active_recipe()`, and
`ACTIVE_DEPLOYMENT_STATE` were all built specifically to centralize this. A
class that keeps recurring after three centralizing fixes is a structural
problem, not a series of bugs.

**A refactor is worth considering if and only if it collapses classes 1 and
3.** WS-4 is the cheapest way to find out: `tools/reconcile_ledger.py`
produces, for the first time, a complete picture of every identity the
system has ever minted and which of them agree. That artifact is a far
better input to a refactor decision than any amount of further reading.
