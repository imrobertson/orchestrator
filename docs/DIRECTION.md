# DIRECTION

Where this system is going and why. **Not a backlog** — for what is open,
blocked, or in flight, see `WORKSTREAMS.md`. For what has already broken and
how it was fixed, see `TOMBSTONES.md`.

This document replaces `ROADMAP.md` and `ARCHITECTURE-MIGRATION-PLAN.md`.
Both had drifted into holding three different things at once (direction,
backlog, and per-fix rationale), which is why entries in them went stale
without anyone noticing.

Last reviewed end-to-end: **2026-09-09**.

---

## What this system is

An off-node control plane (`maestro`) that deploys and operates vLLM model
servers across a pool of NVIDIA GB10 DGX Spark hosts (`spark-3`, `spark-4`
today). Models are declared as per-model YAML recipes; the orchestrator
resolves a recipe plus a topology into a `docker run` invocation on one or
two hosts, tracks the result, and exposes status through a dashboard, a
FastAPI service, and the `dgx-config` CLI.

Three properties are load-bearing and everything below serves them:

1. **A deploy should be reproducible from a committed file.** The recipe is
   the unit of truth. Anything that reaches the container and isn't in the
   recipe is a gap.
2. **A wrong answer must be louder than no answer.** This codebase's
   dominant historical failure mode is not crashing — it is returning a
   plausible value that is wrong. Confidence labels, `stale` markers, and
   the `validated`/`unconfirmed`/`known-bad` vocabulary all exist for this.
3. **Cold starts are expensive.** A failed deploy can cost 30+ minutes.
   Anything catchable before `docker run` should be caught before
   `docker run`.

---

## Phase status

Phase numbering tracks the config-format migration, not the API version.

| Phase | Scope | Status |
|---|---|---|
| 0 | Safety net | Complete |
| 1 | Config consolidation (`cluster_config.yaml`) | Complete |
| 2 | Recipe migration — per-model YAML replaces `models.yaml` | **Complete.** Code removal finished 2026-09-03 in `dgx-orchestrator.py` and 2026-09-09 in `cache_cluster_assets.py`, which had been missed; see WS-0a. |
| 3 | N-node generalization | **Hardware-gated.** Do not start until additional Sparks are racked. Two inputs should land first — see below. |
| 4 | Capability layer and allocator | Not started. `RecipeConfig.capability` exists and is deliberately inert, reserved for this. |
| 5 | Ongoing hardening | Continuous; tracked in `WORKSTREAMS.md`, not here. |

### Phase 3's one architectural commitment

**Hosts are a set of fabric-connected pools, not one flat pool.** A pair may
sit on a network segment with no RoCEv2/ConnectX-7 fabric to another pair.
Any N-node deploy or allocator must select all of its targets from within a
single pool and must never span pools. This is currently expressed in prose
only; nothing in code enforces it.

Two things want to land *before* the hardware arrives, because afterwards
they become break-everything migrations:

- **Interface names out of recipes.** Every 2-node recipe hardcodes
  `NCCL_SOCKET_IFNAME`/`GLOO_SOCKET_IFNAME`. A second pool with different
  NIC names silently invalidates the entire catalog for that pool.
- **A decision on `config_hash` stability across a topology-key change.**
  Every historical hash is bound to today's `1_node`/`2_node` keys. Phase 3
  restructures exactly those. Decide now whether to version the hash or make
  it topology-key-independent, or lose the accumulated launch-validation
  history at the moment Phase 3 lands.

`maestro2` — a second orchestrator instance per pool, each with its own
`cluster_config.yaml` — remains the sanctioned stopgap. Per-host locking is
what actually removes the need for it.

---

## Directional commitments (decisions of record)

These are settled. Reopen them only with new evidence, and record the
reopening.

**Model-specific patches are baked into a derived image layer, not applied
to a running container.** A mod is a directory of `run.sh` plus vendored
payload; the orchestrator resolves a recipe's mod set to a deterministic tag
and bakes it per-host before launch. Rejected: `extra_mounts` (can't express
`git apply` or a `.pth` hook, and requires files to pre-exist identically on
every host), hand-maintained per-model images (erases the fact that vLLM was
modified), and eugr's own `docker exec`-into-a-running-container delivery
(too late for anything parsed at process startup). Shipped.

**Mod payloads are vendored in-repo. No network fetches at bake time.** Each
host bakes locally, so a mod whose content can change between two bakes
produces different images on head and worker. Per-host baking is safe *only*
because of this constraint; if it is ever relaxed, the bake-once-and-
distribute question reopens.

**vLLM is the only engine today, and that is an absence rather than a
choice.** There is no `engine:` field. `_execute_deployment_impl()` hardcodes
vLLM's entrypoint and flag names, and `common/phase_extract.py` is built
entirely on vLLM's log vocabulary — a non-vLLM deploy would not fail, it
would silently lose all phase telemetry. Adding SGLang is an entrypoint and
flag-translation problem; adding llama.cpp is a different problem (GGUF, not
safetensors) and should be scoped separately.

**The ledger is append-only and keyed on the recipe filename stem. It never
deletes.** A renamed recipe leaves its old key behind carrying real history.
This is deliberate — old records stay readable — and it means orphan keys are
expected, not a defect. Distinguishing an orphan from a real gap requires
tooling that does not yet exist.

**Catalog keys use an underscore as the decimal separator**
(`nemotron-3_5-lightning-bf16`, not `nemotron-3.5-...`). The technical
difference is negligible; the diagnostic value is not. HF model names always
carry the literal dot (`NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16`), so a
key appearing with a dot is immediately identifiable as derived from the
served name rather than from a recipe file. The catalog is still mixed —
**as of 2026-09-09, of 35 recipes: 12 dotted, 6 underscored, 17 with no
version decimal at all.** Convergence is a real migration with a real cost
and is sequenced in `WORKSTREAMS.md` (K3 Part 2), not to be done ad hoc. Note
the rename list there cannot converge on its own: the catalog has been adding
dot-form names faster than the list is maintained, so the load-time validator
rejecting a `.` in a recipe stem should land *first*, and the rename run once
against a set that has stopped growing.

**The reserved host is the head host, and that is derived, not chosen.**
Settled 2026-09-09, closing `WORKSTREAMS.md` F-l. The head always has
something running on it that will answer; a worker cannot be relied on to.
Reserving it protects the one node guaranteed to serve a response — so
`hosts:` ordering, `role: head` and `reserved: true` move together, and
`default_deploy_target` is always the other node. The 2026-09-07/08 swap
(spark-4 → spark-3 as head and reserved) follows this rule rather than
breaking it. **The coupling is not enforced in code:** `common/config.py`
validates only that `default_deploy_target` is not reserved, so an edit
reordering `hosts:` without moving `reserved:` produces exactly the
arrangement this rule exists to prevent. A warning when the first host is not
the reserved one would close it.

**Provenance lives on the recipe and in one reference file, never in a
directory.** Settled 2026-09-09. Recipes carry their own lineage in header
comments and `notes:` — which checkpoint, which image, why not the obvious
alternative, and the traceback if there was one. `docs/reference/community-sources.md`
is the index: who published what, the URLs, and whether a source is in use,
investigated-and-set-aside, or a dead end not to retry. Rejected: a
`recipes/eugr/` sync directory (retired 2026-09-09, it had been empty since
creation) and a structured `source:` schema field, which would be a schema
bump — and F-j records two `config_hash` orphaning bumps already. We adopt
eugr's mod *format* but not their delivery, and do not track them as a
leading edge; the working model is best-of from community posts, cooked into
our own recipes.

### Four design traps, kept because each cost real time

Absorbed from `QUESTIONS.md` 2026-09-09. These are not open questions — they
are settled principles that keep getting re-learned.

1. **A snapshot is not a counter.** Telemetry that reports a current value
   cannot be summed over time, and telemetry that reports a running total
   cannot be differenced without knowing when it reset. Decide which one a
   field is before building on it.
2. **State is asymmetric.** "Started" and "stopped" are observed by different
   mechanisms with different failure modes. Do not assume the absence of one
   implies the other.
3. **Storage has a lifecycle nobody owns by default.** Caches, logs and
   ledgers all grow. Every one of them needs a retention decision at the
   moment it is created, not the first time a disk fills.
4. **Hardware strings are not a stable interface.** Parsing device names,
   driver versions or `nvidia-smi` output couples you to a vendor's
   formatting choices. Isolate the parse; never spread it.

---

## Documentation map

**Eight living documents plus two reference files.** Everything else is
archive. The disposition of every file in `docs/` is below, so nothing is
retired by omission.

Consolidation status as of 2026-09-09: the merges marked **merged** below
have been performed; the rest are still to do and are tracked in WS-0.

### Living

| Document | Owns | Does not own |
|---|---|---|
| `README.md` (repo root) | What the system is, architecture, `cluster_config.yaml` reference, recipe schema | Anything time-sensitive; anything measured |
| `DOCMAP.md` | Which document owns what, and where a new fact goes. Written to be pasted at the start of a session | Any status, version or cluster state — deliberately |
| `INSTALL.md` | Standing up a control station and preparing compute nodes | Day-to-day operation |
| `USERMANUAL.md` | Operating the cluster: dashboard, `dgx-config`, deploy, teardown, offline mode, benchmarking, troubleshooting | Design rationale; per-model facts |
| `MODELS.md` | The catalog: topology, context, concurrency, architecture, speculative method and depth, measured speed, validation status. Carries its own last-verified date | How to deploy; why the system works this way |
| **`DIRECTION.md`** (this) | Where we're going, phase status, decisions of record | Individual open items |
| **`WORKSTREAMS.md`** | The canonical backlog: status, evidence, dependencies, kickoff prompts | Per-fix history; intent |
| `TOMBSTONES.md` | Append-only per-fix history and incident log, newest and highest-numbered first | Current status of anything |
| **`errata.yaml`** | Machine-readable recipe rules — the linter's source of truth | Prose narrative |
| `SMOKE-TEST-PLAYBOOK.md`, `AB_TEST_USAGE.md` | Operational procedure and tool usage | Status of the work they test |

### Reference (`docs/reference/`)

Durable notes on third-party dependencies. Not our history, not a backlog.
Kept in a subdirectory so the top-level `docs/` listing shows only what an
operator needs.

| File | Why it stays |
|---|---|
| `community-sources.md` | Where every image, checkpoint and fix came from, with URLs, split into in-use / investigated-not-pursued / dead-end. **Merged 2026-09-09** from `EUGR-REFERENCE-NOTES.md`, `EUGR-NOTES-UPDATE-2026-08-29.md` and `BACKLOG-dspark-sm120-image.md`'s link list. |
| `flashinfer-autotune-internals.md` | FlashInfer's autotuner call chain, confirmed by direct source read across `TOMBSTONES.md` #116–#126, distilled so a future TP-parallel-MoE hang doesn't require re-deriving it. Line numbers are pinned to one build and will drift; the doc says so up front. |

### Disposition of everything else

| File | Disposition |
|---|---|
| `ROADMAP.md` | **Archive.** Direction → here; backlog → `WORKSTREAMS.md`. |
| `ARCHITECTURE-MIGRATION-PLAN.md` | **Archive.** Phase status → here. Phase 3's pool constraint preserved above. |
| `docs/README.md` | **Archive.** A diverged near-duplicate of the root `README.md`; its unique content (crash-log persistence, version tracking, status staleness, config-derived host identity, four CLI commands) was folded into the root copy 2026-09-09. |
| `UsageShortcut.md` | **Merged 2026-09-09** into `USERMANUAL.md`. It was the *newer* of the two — the merge ran shortcut → manual, not the reverse. Archive. |
| `REFERENCE-decode-speeds.md` | **Merged 2026-09-09** into `MODELS.md`, which now owns measured throughput. Archive. |
| `EUGR-REFERENCE-NOTES.md` + `EUGR-NOTES-UPDATE-2026-08-29.md` | **Merged 2026-09-09** into `reference/community-sources.md`. The `-UPDATE` file's two REPLACE blocks had never been applied, so the base file had carried a passage its own follow-up called misleading for eleven days. Archive both. |
| `BACKLOG-dspark-sm120-image.md` | **Archive.** Its five open items are in WS-9 and its link list is in `reference/community-sources.md`. Item 6, the catalog trim, is verified done. |
| `QUESTIONS.md` | **Absorbed above, 2026-09-09.** It contained no questions — four durable design traps, which are decisions-of-record material. Archive. |
| `TROUBLESHOOTING.md` | **Dissolved 2026-09-09; archive.** Ten of its fourteen incidents were already covered. Two were covered nowhere and are now TOMBSTONES #147 (phantom ConnectX-7 link) and #148 (deterministic byte mismatch = corrupted upstream shard). Incident #2's SwiGLU rule became errata **E025** — E017's evidence block had cited `incidents: [2]` and would otherwise have been left pointing into an archived file. Its operational guidance is in `USERMANUAL.md` and its Gemma-4-31b figures in `MODELS.md`. **Note its Incident numbering is a separate series from this file's** — see TOMBSTONES' citation convention before merging anything. |
| `BACKLOG-session-tracker-multi-model.md` | **Fold into `WORKSTREAMS.md`, then archive.** Named in WS-11's carried-out items but never transcribed. The last untranscribed backlog file. |
| `SESSION-HANDOFF-2026-09-06.md` | **Archive once §4 items 5–8 and §8 reach `WORKSTREAMS.md`.** §7's doc edits are applied. |
| `SESSION-CLOSEOUT-2026-09-02-FINAL.md` | **Archive.** Ported to WS-5; verify the port lost nothing first. |
| `SESSION-SEED.md` | **Delete, do not fold.** Not merely stale — it cites TOMBSTONES #76 as current (now #143), recommends an image confirmed x86_64-only, names a recipe that no longer exists, and frames DSpark as broken when it runs at 42–44 tok/s. As onboarding it actively misdirects. |
| `REFERENCE-control-surfaces.md` | **Keep until the interface spike lands, then delete.** It says so itself, and is deliberately absent from the doc map above so it can be removed without leaving a dangling reference. |
| `PHASE-2-PROMPTS.md` | **Archive.** Phase 2 complete. |
| `PHASE-MODS-PROMPTS.md` | **Append MA/MB/MC results, then archive.** Records only M0's today, so it reads as though the sequence stalled at the gate. |
| `MA/MB/MC/MD/ME-REVIEW.md`, `tests/TESTING-MB.md`, `tests/TESTING-MC.md` | **Archive.** ~185 KB of per-task review. Confirm first that nothing durable is only there — `TOMBSTONES.md` #85 cites `M{X}-REVIEW.md`'s "Contradictions" section directly, and `MD-REVIEW.md`'s contradictions section is `errata.yaml` material. |
| `REFERENCE-dspark-shared-expert-fix.md` | **Does not exist.** Cited by `BACKLOG-dspark-sm120-image.md` as "saved as" and by `TROUBLESHOOTING.md`, but absent from `docs/` and the repo root. Either re-save it from tonyd2wild's repo or delete both citations. The conclusion it supports — the shared-expert bug does not apply to our image — survives in WS-9 and `reference/community-sources.md`. |

**Archive by moving to `docs/archive/`, not by leaving files in place.** A
document left in `docs/` is a document someone will read and believe. A
`docs/archive/README.md` holding one line per retired file — what it was and
why it went — is worth more than a synthesized narrative history, which
would just become the next unread file. `TOMBSTONES.md` already is the
history.

**The rule that keeps this from drifting again:** a document describing
*current state* must be checkable against code or an artifact. Where it
isn't, it belongs in `TOMBSTONES.md` (history, immutable) or `DIRECTION.md`
(intent, rarely changes) instead. Every stale entry found in the 2026-09-03
synthesis pass was in the third category — a status claim in a document
nobody re-read when the code moved.

**A second rule, learned from `EUGR-NOTES-UPDATE-2026-08-29.md`:** update
the document, don't append a dated companion to it. A `-UPDATE-<date>` file
is a merge someone has to do later, and it is how a doc set reaches thirty
files.

**A third, learned the hard way on 2026-09-09:** one fact, one home. Where
the same paragraph exists in two files, it diverges — and the copy nobody
re-reads is the one that steers someone wrong. Three separate corrections
that session were the same stale claim living in two places at once.

---

## Inputs to the architecture review

`TOMBSTONES.md` #27–#110 is the evidence this section was derived from.
**It has not been re-derived since; entries #111–#143 are not reflected
below.** Sorted by recurrence rather than by severity, five classes account
for the large majority of entries. These are the agenda for a refactor
conversation.

**1. Identity is derived independently in many places.**
#41 (recipe `name:` vs filename), #53 (ledger key vs served basename), #57
(near-duplicate catalog keys), #77 (fuzzy served-name match is ambiguous by
construction), #91 (hash exclusion went stale), #92 (whitespace changed a
hash), #110 (one model, two ledger keys). The recurring shape: "what is this
model called" is answered by a different mechanism in the deploy path, the
telemetry path, the benchmark path, and the dashboard. `config_hash` and
`_resolve_active_recipe()` were both built to centralize this and both only
cover part of it. **This is the single largest class and the strongest
argument for a refactor.** Still producing instances: the 2026-09-09 audit
found the dashboard's benchmark button labelling ledger rows from the deploy
form rather than from what is serving, and `ab_test.py` writing a third
distinct key convention that cannot join the catalog at all.

**2. Failures that return plausible values.**
#78 (teardown reported success on real per-host failure), #79 (version
suffix silently `+unknown` twice), #82 (silent HF token failure, twice, via
two different paths), #83 (two broken checks agreeing produced a PASS), #93
(a failed `docker logs` archived as though it were the log), #97 (substring
match), #100 (partial marker mismatch reported 100% of runtime as one
phase). The repo has already derived the right rule from these — assert what
a failure path *returned*, not that it survived — but it is a convention, not
a structure.

**3. The control plane reasons about processes it cannot see.**
#50, #55, #63, #64, #69, #70, #80, #81. Container PID namespaces,
`--ipc=host`, a `docker exec -d`'d engine detached from PID 1, and
`dgx-config` running as a different process from the daemon inside the same
container. Each was fixed individually; the underlying model — "the
orchestrator can observe and signal what it launched" — is still not true.

**4. State is inferred from scraping log text.**
#46, #60, #95, #96, #97, #98, #99, #100, #101, #102. Phase boundaries,
crash detection, ETA, and rank identity all derive from parsing vLLM's
prose. Tasks A–D made this much better (real self-reported durations,
per-field confidence) without changing the fundamental coupling — which is
also what makes multi-engine support expensive.

**5. Config-to-argv construction has no round-trip check.**
#28, #35, #42, #86, #88, #90, #92. YAML scalar handling, `shlex` quoting
across SSH, flag reordering, and a credential rendered in plaintext. The
verification pattern that catches these (build the artifact both ways and
diff it byte-for-byte) exists and works — #90 caught a real regression
before it reached hardware — but is applied by hand, per task.

**A refactor is worth considering if and only if it collapses class 1 and
class 3.** Classes 2, 4, and 5 are being managed adequately by convention
and targeted fixes; classes 1 and 3 keep producing new instances despite
having been "fixed" several times each, which is the signature of a
structural problem rather than a series of bugs.

**A sixth pattern, not yet a class, worth watching.** Rules written from real
incidents are being violated by recipes that nobody checks, because nothing
reads `errata.yaml`. The 2026-09-09 audit found four recipes violating three
`enforce: error` rules — E003, E004, E005 — each of which had been fixed
elsewhere at least once, and one of which (E023, `--speculative-model`) had
its diagnosis written into a *different recipe's header* and never reached
the broken file. That is not an identity problem or an observability problem.
It is the absence of the linter, and it makes WS-3 cheaper to justify than
its size suggests.
