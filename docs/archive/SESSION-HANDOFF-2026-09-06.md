# SESSION HANDOFF -- 2026-09-06: host-scoped teardown, reserved hosts, topo/ledger fix

Purpose of this document: carry the state of the 2026-09-06 session into a
fresh one whose single job is integrating the outcome into
`docs/WORKSTREAMS.md`. That file is ~108k and could not be pulled without
consuming the context needed to write a good entry, so the integration was
deliberately deferred rather than half-done.

**Read this file, then read `docs/WORKSTREAMS.md`, then write the entries.**
Everything below is verified against the actual source unless explicitly
marked otherwise.

---

## 1. What shipped (all landed, all verified)

Three defects and one feature, which turned out to be one problem wearing
three hats: **deployment topology being inferred from host identity or host
count rather than read from container role.**

### Feature: host-scoped teardown

The teardown core already accepted a `target_hosts` list and the deploy path
already scoped its own pre-deploy teardown correctly. Only the CLI and API
surfaces had no way to pass one.

- `resolve_teardown_hosts()` -- shared `--host`/`--all` resolver. Comma-
  delimited, whitespace-tolerant, order-preserving dedupe. Unknown host
  names are errors listing the valid names, not silent no-ops that report a
  clean teardown of nothing.
- **No default-to-all fallback.** Both `teardown` and `cli teardown` now
  require an explicit scope. A bare `dgx-config teardown` exits with a usage
  error.
- `TeardownRequest` wired into `/api/teardown`; `hosts=None` still means all
  hosts, so `index.html` is unchanged.

### TOMBSTONES #127: count-based topo derivation poisoning the ledger

Two sites derived topology by counting hosts with containers, so two
independent 1-node deploys read as `2_node`:

- `_compute_cluster_status_impl()` -- feeds `SESSION_TRACKER.update()` on the
  4-second status poll. **This is the live ledger write path.**
- `_detect_live_model_topo_metrics()` -- used by argument-free
  `correct-ledger` to choose which key to overwrite.

`SessionTracker.update()` latches `self.model`/`self.topo` only on the
inactive→active transition and freezes them for the session. Sessions go
inactive from exactly three places: 600s idle, `execute_teardown()`, and
`_execute_deployment_impl()`'s pre-deploy step. A session starting while both
hosts had containers therefore committed every flush under a fabricated
`<model>::2_node` key; `_load_last_seen_raw()` found no checkpoint, re-
baselined to current cumulative counters, and stranded the real history on
`::1_node`.

Fixed by deriving from the serving host's own `ContainerRole`, matching
`_finalize_host_status()`'s always-correct form. Paired with a survivor gate
in `execute_teardown()` that clears `SESSION_TRACKER.active` only when no
host recorded in `ACTIVE_DEPLOYMENT_STATE` survives.

### TOMBSTONES #128: head→worker mirroring

`_compute_cluster_status_impl()` mirrored the head host's model state onto
the worker's status row, gated only on both being RUNNING and healthy. With
two independent 1-node deploys that overwrote the secondary host's
`active_model`/`model_status`/`eta_*` with the primary's -- the dashboard
reporting a model on a node not running it. Display-only; ledger unaffected.
Now gated on both hosts actually running Ray `HEAD`/`WORKER` containers.

### Reserved hosts + separate default deploy target

`PRIMARY_HOST` was doing two unrelated jobs: structural head node and
telemetry-authoritative serving host, **and** fallback target for anything
unqualified. Those conflict once a node holds something long-lived.

- `cluster_config.yaml` gains top-level `default_deploy_target` and per-host
  `reserved`. Both optional, both defaulting to prior behaviour.
- `check_reserved_hosts()` enforced in `execute_deployment()` and
  `execute_teardown()`, so CLI, API and menu all inherit it. `--force`
  overrides. `dry_run` is **not** exempt. Whole-cluster teardown is checked
  too, since "everything" includes the reserved host.
- `--head` resolves by topology *after* parsing (argparse cannot see
  `--nodes`): `PRIMARY_HOST` for 2-node, `DEFAULT_DEPLOY_HOST` for 1-node.

---

## 2. Files delivered

| File | Destination | Status |
|---|---|---|
| `dgx-orchestrator.py` | repo root | In place (size confirmed on GitHub) |
| `cluster_config.yaml` | repo root | In place (size confirmed on GitHub) |
| `config.py` | `common/config.py` | Delivered |
| `ab_test.py` | `tests/ab_test.py` | Delivered |
| `deploy_gemma4_dflash.py` | repo root | Delivered |
| `BACKLOG-session-tracker-multi-model.md` | `docs/` | In place |
| `AB_TEST_USAGE.md` | `docs/` | Delivered |
| `UsageShortcut.md` | `docs/` | Delivered |
| `INSTALL.md` | `docs/` | Delivered |
| TOMBSTONES #127 + #128 | paste into `docs/TOMBSTONES.md` | Delivered as text |

`tests/metest.py` was **deleted** this session -- it was the pre-refactor
original that survived the 2026-09-01 rename to `tests/ab_test.py`. Do not
re-add it; `docs/BACKLOG-generalize-metest.md` is the historical record.

**Checked and confirmed needing no change:** `dgx-config` (pure argv
passthrough into `cli`), `autotuner_init.py` (flashinfer re-export shim),
`tests/run_overnight_tp_pp_ab.sh` (delegates to `ab_test.py`).

---

## 3. Verification status

Verified against real pydantic 2.13.5 and the real `cluster_config.yaml`
(wheels were installed offline mid-session, closing a gap that had been
explicitly flagged):

- Constant derivation: `PRIMARY_HOST=spark-4`, `SECONDARY_HOST=spark-3`,
  `DEFAULT_DEPLOY_HOST=spark-3`, `RESERVED_HOSTS=['spark-4']`.
- Resolver edge cases: dedupe, whitespace, typos, empty and comma-only
  strings, error-message contents.
- argparse rejects bare and conflicting teardown invocations (exit 2); all
  five valid invocations thread the correct host list and force flag.
- Guard behaviour across deploy, teardown, `--all`, `dry_run`, and `--force`.
- Backward compatibility: both new config keys omitted → identical prior
  behaviour.
- Cross-field validation: unknown / inactive / reserved `default_deploy_target`
  all raise with the file path and specific problem.
- pydantic coercion: `reserved: "true"` → `True`; `reserved: banana` →
  `ValidationError`; unknown per-host keys ignored, not fatal.
- `ab_test.py` and `deploy_gemma4_dflash.py` both exit 2 on a reserved host,
  before any SSH.

**Not verified:** as of the first draft of this document, nothing had been run
against real hardware.

**Superseded 2026-09-06 15:51 UTC -- now confirmed in production.** Container
restarted, all three verification commands pass:

- `dgx-config teardown` → usage error, `one of the arguments --host --all is
  required`.
- `dgx-config teardown --host spark-4` → `Refusing to tear down: spark-4
  (running qwen-3.8-27b-nvfp4/1_node) is marked reserved in
  cluster_config.yaml.` End to end: new CLI, new `config.py`, and
  `reserved: true` all live.
- `orchestrator_version` now
  `2026-09-06-reserved-hosts-scoped-teardown+fb112150` -- slug bumped.

**TOMBSTONES #128 confirmed fixed on hardware.** `/api/status` shows both
hosts RUNNING and healthy with `vllm-standalone` -- exactly the two-
independent-1-node-deploys case. spark-4 reports `Qwen3.8-27B-NVFP4`, spark-3
reports `Gemma-4-26B-A4B-NVFP4`. Pre-fix, the mirror would have overwritten
spark-3's row with spark-4's model.

**#127 was NOT directly observable in that output** and should not be recorded
as confirmed. The `1_node` in the refusal message comes from
`ACTIVE_DEPLOYMENT_STATE`'s `topo_key`, written at deploy time from `nodes` --
a different source than the status-path derivation the bug lived in.

---

## 4. Open items -- the raw material for the WORKSTREAMS entries

Ordered by my read of priority, but that ordering is a suggestion, not a
finding.

1. **Deploy and verify -- DONE 2026-09-06 15:51 UTC.** Committed, pulled,
   container restarted, all three verification commands pass, slug bumped to
   `2026-09-06-reserved-hosts-scoped-teardown`. Full output and what it does
   and does not prove is recorded in section 3. Listed here only so nobody
   redoes it. Everything below was still open at handoff time.

2. **Stale teardown syntax in four doc lines.** RESOLVED as a code question:
   a repo-wide grep was run 2026-09-06 and confirmed **no broken code callers
   remain anywhere**, including `autotuner.py` (128k), which had been the main
   unchecked file. The grep pattern
   `"cli.*teardown\|dgx-config teardown"` across `*.py`/`*.sh`/`*.md` matches
   argv lists as well as shell invocations, so a Python caller would have
   shown.

   Still to fix, docs only -- each shows a bare `dgx-config teardown`, which
   is now a usage error:
   - `README.md:163` and `docs/README.md:174` ("Purge Active Runtimes")
   - `docs/USERMANUAL.md:54` (command reference) and `:271` ("Run
     `dgx-config teardown` to flush the GPUs before trying again")

   Deliberately NOT changed: `docs/stage_specs_refactor_prompt.md:96` and the
   two `docs/TOMBSTONES.md` hits (2188, 2289). All three are historical
   narrative describing past events and are correct as written.

   `docs/SMOKE-TEST-PLAYBOOK.md` did not match the grep, so the earlier
   prediction that it contained stale teardown syntax was wrong. It may still
   want the new `cluster_config.yaml` keys documented; that was never
   confirmed either way.

3. **`index.html` pass.** Two real UX consequences, both correct-but-awkward:
   - The Teardown button posts no host list, so it means "all hosts", which
     includes the reserved one → returns 409 with the refusal in the existing
     error banner. Unusable while hermes is up.
   - `fetchStatus()` auto-syncs `headSelect.value` to `data.serving_host`.
     spark-4 always wins `serving_host`, so the deploy target selector
     continuously snaps to the one host it will then refuse.
   Small, unblocked, and the last rough edge in daily use.

4. **`model_ledger.json` audit -- COMPLETE 2026-09-06, came back clean.**
   22 distinct `::2_node` keys checked against `recipes/*/`:
   **17 `ok`, 5 `NO RECIPE`, 0 `FABRICATED`.**

   No key exists whose recipe lacks a `2_node` topology. **No evidence of
   TOMBSTONES #127 poisoning in the ledger.**

   All five `NO RECIPE` entries are orphaned history from deliberate
   deletions or renames, not corruption:
   - `deepseek-v4-flash-0731-dspark-sm120` and
     `deepseek-v4-flash-0731-nvfp4` -- deliberately cut, see
     `BACKLOG-dspark-sm120-image.md` (the former was "the dead
     orthozany-based `-sm120.yaml`" that the promoted canonical recipes
     replaced).
   - `deepseek-v4-flash-0731-dspark-gb10-hazyumps-512k` -- intermediate name
     from the hazyumps work, since renamed to `-dspark-512k`.
   - `deepseek-v4-flash-0731` and `qwen-3.5-122b` -- pre-rename ancestors of
     recipes that still exist.

   **Why a clean result is plausible rather than lucky:** poisoning requires
   two containers serving simultaneously, which is a NEW operating mode
   (resident agent plus concurrent testing). Most of this ledger predates it.
   The exposure window is narrow and recent.

   **Residual, still not disprovable:** for the 17 `ok` models, a 1-node run
   while the other host had a container would have landed poisoned tokens in
   the model's *legitimate* `::2_node` key -- same key, same counters, no
   marker. Undetectable after the fact. Given the narrow window this is
   unlikely, but for any model whose numbers matter, re-baseline rather than
   trust. `qwen-3.8-27b-nvfp4` is the one with actual exposure: it came back
   `ok` (it does have a 2-node topology) and was serving 1-node on spark-4
   while spark-3 also served, at the time of the audit.

   Note also that an argument-free `correct-ledger` run while two containers
   were up used the same bad heuristic to pick which key to repair, so past
   repair attempts may themselves have written to the wrong entry.

   **Related, different file, same failure shape:**
   `BACKLOG-dspark-sm120-image.md` open item 5 records a
   `benchmark_ledger.csv` key mismatch -- a validating run logged under
   `deepseek-v4-flash-0731-1M` rather than the recipe actually used. Two
   ledgers, both keyed on something derived rather than recorded. Argues for
   the linter in item 6 covering ledger keys, not just topology.

   **Side effect:** `BACKLOG-dspark-sm120-image.md` open item 6 (catalog
   trim) is DONE -- both recommended deletions have happened and the
   remaining three recipes match its predicted net catalog exactly. That doc
   still says "Not yet actually deleted"; replacement text for item 6 was
   drafted in-session.

5. **Multi-model session tracker.** Backlog doc written and in place at
   `docs/BACKLOG-session-tracker-multi-model.md`. Two independent pieces:
   make the telemetry host preference explicit (small, worth doing alone),
   and per-host tracker instances (larger, touches the `/api/status`
   `session_stats` shape that `index.html` reads directly).

6. **Role-derivation linter.** Grep-shaped check flagging any topology or role
   decision derived from host identity, host ordering, or a count of hosts
   rather than from `ContainerRole` or the deployment record. Would have
   caught both #127 and #128. Fits alongside the existing `ROADMAP.md` items
   for a known-bad-flag-combination linter and per-recipe `status:` markers.

7. **Out-of-band containers are invisible to `ACTIVE_DEPLOYMENT_STATE` --
   OPEN QUESTION with a live instance.** At 15:51 UTC on 2026-09-06, spark-3
   was serving `Gemma-4-26B-A4B-NVFP4` with `active_recipe_key: null` and
   `active_config_hash: null`. The orchestrator has no record of that deploy,
   so it came up out-of-band: a raw `docker run` over SSH, most likely
   `deploy_gemma4_dflash.py` or `ab_test.py`'s raw-docker path. The resident
   hermes agent was testing and benchmarking recipes around that time; whether
   it was going through the orchestrator at all is **unconfirmed and worth
   establishing first**, because it sets the blast radius for item 4.

   Two concrete consequences, both already documented as caveats but now
   known to be live rather than hypothetical:
   - The survivor gate in `execute_teardown()` reads
     `ACTIVE_DEPLOYMENT_STATE`, so an unrecorded container cannot hold the
     session open. Tearing down spark-4 in that state computes zero survivors
     and deactivates `SESSION_TRACKER` even though spark-3 keeps serving.
   - A reserved-host refusal naming such a host falls back to "contents
     unknown" instead of naming the model.

   Making the gate robust would mean consulting live container state rather
   than recorded deployment state -- a real design change, not a tweak.
   **Fold this into `docs/BACKLOG-session-tracker-multi-model.md`**, which
   currently does not mention it; it belongs next to the per-host tracker
   work since both concern what "a host is serving something" actually means.

   Related: if hermes was deploying out-of-band routinely, the contaminated-
   legitimate-key case described in item 4 is more likely than it would
   otherwise be, since those deploys never recorded a topology anywhere.

8. **Per-host `CLUSTER_OP_LOCK`.** Still cluster-global, so a scoped teardown
   serializes against any deploy. Explicitly Phase 3; not attempted.

---

## 5. Operational consequences someone will hit

- **Overnight A/B runs are 1-node-only while spark-4 is reserved.**
  `run_overnight_tp_pp_ab.sh` defaults `nodes=2`, as does every `pairs.txt`
  line omitting the field. 2-node always spans both hosts, so every 2-node
  pair is now refused. The script runs `set -uo pipefail` (not `-e`), so it
  records `FAIL` per pair and continues -- an unattended run would spend the
  night failing every pair. Before a 2-node night: take hermes down and drop
  `reserved:` from spark-4. `ab_test.py` has no `--force` by design.

- **2-node work and a resident hermes are genuinely mutually exclusive.** The
  guard makes that explicit rather than silently killing hermes; it does not
  create the constraint.

- **Raw `docker run` over SSH bypasses the orchestrator guard entirely.**
  `deploy_gemma4_dflash.py` and `ab_test.py`'s raw-docker path each carry
  their own reserved-host check for this reason. Anything new that shells
  docker directly needs one too -- this is the class of gap the linter in
  item 6 would cover. See item 7: this is not hypothetical, spark-3 was in
  exactly this state on 2026-09-06.

- **`session_stats` looks wrong immediately after a container restart, and
  isn't.** The 15:51 UTC status showed `duration_sec: 0` with `tps: 10520.0`.
  `SESSION_TRACKER` is in-memory, so the restart reset it to inactive and the
  first poll with moving counters computed a delta against a near-zero
  elapsed time. It normalizes on the next poll -- don't chase it.

  Worth noticing rather than dismissing, though: that re-activation IS the
  inactive→active latch window from #127, and it fired with both hosts
  holding containers. The deploy that fixed the bug also walked straight
  through the scenario that used to trigger it.

---

## 6. Architectural direction agreed this session

Phase 3 should generalize so any node can be head or worker, declaratively
rather than by inference or hardcoding. The refinement agreed on top of that:

**The useful axis is node properties vs. deployment properties, not
hardcoded vs. declarative.** Head and worker are properties of a *deployment*,
not of a node -- so `cluster_config.yaml`'s per-host `role:` field is a
hardcode in declarative clothing and should *dissolve* in Phase 3 rather than
generalize. What legitimately belongs on the node is what's true regardless of
what's deployed: fabric pool, GPU and memory, IP, and reserved-ness. Which
node is head *this time* belongs in the deployment record.

`ContainerRole` is already the correct primitive and is already authoritative.
Every bug this session was a site that declined to consult it. `PRIMARY_HOST =
next(iter(HOSTS))` makes node identity a function of YAML *ordering*, which is
an accident rather than a declaration.

The caution attached: declarative is not itself the guarantee. The guarantee is
that derived state is computed once, recorded, and *read* -- not re-derived
independently at each site. This session had three separate derivations of
topology and two were wrong, and all three would still have been wrong had the
input come from a config file each site interpreted for itself.

Reserved-ness survives this transition cleanly -- it is a real node property --
so the work in section 1 will not need unwinding in Phase 3.

Note: `docs/ARCHITECTURE-MIGRATION-PLAN.md` is stale; it was consolidated into
`DIRECTION.md` + `WORKSTREAMS.md` on 2026-09-05. Also still unrecorded there:
the Phase 3 constraint that hosts are fabric-connected pools, so a deploy or
allocator must never span pools.

---

## 7. Pending doc edits -- ready to apply, not yet applied

These were drafted and confirmed in-session but deliberately left for the next
session to apply, so they land as proper file edits rather than hand-pasted
lines. All four teardown lines below currently show a bare
`dgx-config teardown`, which is now a usage error.

**`README.md` line 163 and `docs/README.md` line 174** -- both currently read
`* '''Purge Active Runtimes:''' \`dgx-config teardown\``. Replace with:

```
* '''Purge Active Runtimes:''' `dgx-config teardown --host <node>` (or `--all` for the whole cluster — there is no argument-free form)
```

**`docs/USERMANUAL.md` line 54** -- a bare command in the reference list.
Replace `dgx-config teardown` with:

```
dgx-config teardown --host <node>
```

**`docs/USERMANUAL.md` line 271** -- currently "If a deployment fails
instantly, it usually means the previous model wasn't cleaned up properly. Run
`<code>dgx-config teardown</code>` to flush the GPUs before trying again."
Replace with:

```
If a deployment fails instantly, it usually means the previous model wasn't cleaned up properly. Run <code>dgx-config teardown --host &lt;node&gt;</code> to flush that host's GPUs before trying again — teardown now requires an explicit <code>--host</code> or <code>--all</code>, and refuses a host marked <code>reserved:</code> in <code>cluster_config.yaml</code> without <code>--force</code>.
```

**`docs/BACKLOG-dspark-sm120-image.md` open item 6** -- currently says "Not
yet actually deleted — confirm before removing". Both deletions have happened
and the remaining three recipes match its own predicted net catalog exactly.
Replace item 6 with:

```markdown
6. **Catalog trim — DONE.** Cut `deepseek-v4-flash-0731-b12x-nospec.yaml`
   and `deepseek-v4-flash-0731-nvfp4.yaml` as recommended. Net catalog
   confirmed 2026-09-06: `deepseek-v4-flash-0731-dspark.yaml`,
   `deepseek-v4-flash-0731-dspark-512k.yaml`,
   `deepseek-v4-flash-0731-1M.yaml`. Both deleted names still appear in
   `model_ledger.json` as orphaned `::2_node` keys — expected, benign, and
   not evidence of the TOMBSTONES #127 poisoning.
```

**Do NOT change** the two `docs/TOMBSTONES.md` hits (lines 2188, 2289) or
`docs/stage_specs_refactor_prompt.md` line 96. All three are historical
narrative describing past events and are correct as written.

---

## 8. Hermes deploy mechanism -- OPEN, needs investigating with Ian

**The question:** was the resident hermes agent deploying through the
orchestrator, or shelling `docker run` directly? Unresolved as of handoff.
Ian flagged it as something to work through together rather than assume.

**Why it matters, and why it matters less than it first appeared.** The
ledger audit (item 4) came back clean regardless, so this is no longer
load-bearing for data integrity. What it still determines is whether
spark-3's `active_recipe_key: null` is a one-off or a standing pattern --
which sets how much weight to put on `ACTIVE_DEPLOYMENT_STATE` as a source of
truth, and therefore how urgent the survivor-gate design change in section 4, item 7 is.

**What was actually observed** (2026-09-06 15:51 UTC, `/api/status`):
spark-3 RUNNING `vllm-standalone`, `active_model:
"Gemma-4-26B-A4B-NVFP4"`, `active_recipe_key: null`,
`active_config_hash: null`.

**One clue already in hand, worth not over-reading.** That model name looks
like NVIDIA's official checkpoint, not AEON's. `deploy_gemma4_dflash.py`
passes `--served-model-name gemma4-aeon-uncensored`, which is *not* what is
showing -- so that script is unlikely to be the source. `ab_test.py`'s
raw-docker path requires an explicit `--a-entrypoint`. And both of
`ab_test.py`'s recipe paths go through `cli deploy`, which *would* have
recorded state. So none of the obvious candidates fit cleanly, which is
exactly why this needs looking at rather than guessing.

**Decisive check -- run this first.** The orchestrator always launches
`python3 -m vllm.entrypoints.openai.api_server --model <hf_path>` against the
image's default entrypoint. A raw `docker run` from either script overrides
the entrypoint. So the container itself records which path created it:

```bash
ssh spark-3 'docker inspect vllm-standalone \
  --format "{{.Created}}|{{json .Config.Entrypoint}}|{{json .Config.Cmd}}|{{.Config.Image}}"'
```

**Supporting evidence, in rough order of usefulness:**

```bash
cat active_deployment_state.json          # BASE_DIR/active_deployment_state.json (line 142)
ls -lt run_logs/ | head -20               # orchestrator archives a run log per deploy
```

`active_deployment_state.json` is disk-backed, so a container restart does not
clear it -- a `null` there means the deploy was genuinely never recorded,
rather than lost to the restart. If `run_logs/` has no entry matching that
container's creation time, that is corroboration the deploy never went through
`_execute_deployment_impl()` at all.

**If it turns out hermes was deploying out-of-band:** that is not misbehaviour
to correct so much as a gap to close -- nothing currently *requires* going
through the orchestrator, and the raw-docker paths exist for legitimate
reasons (entrypoint overrides the recipe schema cannot express). The useful
outcome is deciding whether out-of-band deploys should register themselves in
`ACTIVE_DEPLOYMENT_STATE`, which would fix section 4 item 7's root cause too.

---

## 9. A correction worth carrying forward

Working context carried into this session had DSpark/DeepSeek-V4-Flash as the
top open priority: ~14 tok/s decode, with a plan to pull
`orthozany/vllm-jasl-dsv4:pr41834-2026-05-13` and smoke-test against
`recipes/local/deepseek-v4-flash-0731-dspark-sm120.yaml`.

**Every part of that is superseded.** `BACKLOG-dspark-sm120-image.md` records
the orthozany image as a confirmed dead end (x86_64 only, `Exec format error`
on GB10, "don't revisit unless an arm64 tag appears"), the
`hazyumps/deepseek-v4-flash-gb10:sm121-cu130-20260727d` image as working and
validated on real 2-node hardware at 42-44 tok/s (~3x baseline), and the
`-sm120.yaml` recipe as deliberately deleted in favour of the promoted
canonical names.

Recorded here because the same stale framing may exist in other carried-over
context. **Read `BACKLOG-dspark-sm120-image.md` before treating DSpark as
open work** -- its real open items are long-session validation of the 512K
recipe, the JIT warmup gap, a missing tuned FP8 kernel config, and a
re-benchmark under probabilistic sampling. Not "get it working."
