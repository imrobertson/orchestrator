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

**Not verified:** nothing has been run against real hardware. The maestro
push/pull/deploy cycle had not happened as of this handoff.

---

## 4. Open items -- the raw material for the WORKSTREAMS entries

Ordered by my read of priority, but that ordering is a suggestion, not a
finding.

1. **Deploy to maestro and confirm.** Push locally → pull on maestro →
   deploy → confirm via the dashboard version badge or `orchestrator_version`
   in `/api/status`. A forgotten `git push` before `git pull` has silently
   left old code running before (TOMBSTONES #65). Nothing else on this list
   is real until this happens.

2. **Run a repo-wide grep for remaining broken callers.** I could not grep
   through the GitHub connector, and a bare `cli teardown` now fails at
   runtime rather than at import:
   ```bash
   grep -rn "cli.*teardown\|dgx-config teardown" --include="*.py" --include="*.sh" --include="*.md" .
   ```
   `autotuner.py` (128k) is the main unchecked file. `docs/USERMANUAL.md`,
   `docs/README.md`, root `README.md` and `docs/SMOKE-TEST-PLAYBOOK.md` are
   all *predicted* to contain stale teardown syntax or missing config-key
   documentation -- predicted, not confirmed; I did not open them.

3. **`index.html` pass.** Two real UX consequences, both correct-but-awkward:
   - The Teardown button posts no host list, so it means "all hosts", which
     includes the reserved one → returns 409 with the refusal in the existing
     error banner. Unusable while hermes is up.
   - `fetchStatus()` auto-syncs `headSelect.value` to `data.serving_host`.
     spark-4 always wins `serving_host`, so the deploy target selector
     continuously snaps to the one host it will then refuse.
   Small, unblocked, and the last rough edge in daily use.

4. **`model_ledger.json` audit.** Grep for `::2_node` keys belonging to models
   whose recipe defines no `2_node` topology. Any such key is fabricated and
   its `::1_node` counterpart is missing the corresponding tokens. Also: an
   argument-free `correct-ledger` run while two containers were up used the
   same bad heuristic to pick which key to repair.

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

7. **Per-host `CLUSTER_OP_LOCK`.** Still cluster-global, so a scoped teardown
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
  item 6 would cover.

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
