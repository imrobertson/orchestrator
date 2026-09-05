# DIRECTION

Where this system is going and why. **Not a backlog** — for what is open,
blocked, or in flight, see `WORKSTREAMS.md`. For what has already broken and
how it was fixed, see `TOMBSTONES.md`.

This document replaces `ROADMAP.md` and `ARCHITECTURE-MIGRATION-PLAN.md`.
Both had drifted into holding three different things at once (direction,
backlog, and per-fix rationale), which is why entries in them went stale
without anyone noticing.

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
| 2 | Recipe migration — per-model YAML replaces `models.yaml` | **Complete** |
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
served name rather than from a recipe file. The catalog is currently mixed
(~6 dotted keys, ~3 underscored); convergence is a real migration with a
real cost and is sequenced in `WORKSTREAMS.md`, not to be done ad hoc.

---

## Documentation map

Seven living documents. Everything else is archive. The current `docs/`
holds 26 files; the disposition of every one is below, so nothing is retired
by omission.

### Living

| Document | Owns | Does not own |
|---|---|---|
| `README.md` | What the system is, architecture overview, model catalog | Anything time-sensitive |
| `INSTALL.md` / `USERMANUAL.md` | Setup and operation. Fold `UsageShortcut.md` in as a fast-path section | Design rationale |
| **`DIRECTION.md`** (this) | Where we're going, phase status, decisions of record | Individual open items |
| **`WORKSTREAMS.md`** | The canonical backlog: status, evidence, dependencies, kickoff prompts | Per-fix history |
| `TOMBSTONES.md` | Append-only per-fix history, newest/highest first | Current status of anything |
| **`errata.yaml`** | Machine-readable recipe rules — the linter's source of truth | Prose narrative |
| `TROUBLESHOOTING.md` | Incident log only (numbered, diagnostic) | Recipe tuning rules — those move to `errata.yaml` |
| `SMOKE-TEST-PLAYBOOK.md`, `AB_TEST_USAGE.md` | Operational procedure and tool usage | Status of the work they test |

### Disposition of everything else

| File | Disposition |
|---|---|
| `ROADMAP.md` | **Archive.** Direction → here; backlog → `WORKSTREAMS.md`. Two entries were stale; see WS-0. |
| `ARCHITECTURE-MIGRATION-PLAN.md` | **Archive.** Phase status → here. Phase 3's pool constraint preserved above. |
| `BACKLOG-dspark-sm120-image.md` | **Archive after transcription.** Headline result closed, catalog trim verified executed; five real open items folded into WS-9. |
| `BACKLOG-generalize-metest.md` | **Fold into `WORKSTREAMS.md`, then archive.** The `STAGE_SPECS` refactor. Check first whether the DSpark use case that motivated it still applies. |
| `stage_specs_refactor_prompt.md` | **Archive with the above** — it is that backlog's kickoff prompt. Move it into `WORKSTREAMS.md` if the work is still wanted. |
| `PHASE-2-PROMPTS.md` | **Archive.** Phase 2 complete. |
| `PHASE-MODS-PROMPTS.md` | **Append MA/MB/MC results, then archive.** Records only M0's today, so it reads as though the sequence stalled at the gate. |
| `MA/MB/MC/MD/ME-REVIEW.md` | **Archive.** ~166 KB of per-task review. Confirm first that nothing durable is only there — `TOMBSTONES.md` #85 cites `M{X}-REVIEW.md`'s "Contradictions" section directly. |
| `SESSION-CLOSEOUT-2026-09-02-FINAL.md` | **Archive.** Ported to WS-5; verify the port lost nothing first. |
| `SESSION-SEED.md` | **Archive**, unless it is a live onboarding aid — then fold into `USERMANUAL.md`. |
| `EUGR-REFERENCE-NOTES.md` + `EUGR-NOTES-UPDATE-2026-08-29.md` | **Merge the two, keep as reference.** External-ecosystem notes with no equivalent elsewhere. Add the note that we adopt eugr's mod *format* but not their delivery. |
| `REFERENCE-flashinfer-autotune-internals.md` | **Keep.** Same category as the row above — durable internals notes on a third-party dependency (FlashInfer's autotuner call chain, confirmed via direct source read across `TOMBSTONES.md` #116–#126), not project history and not a backlog item. Distilled specifically so a future TP-parallel-MoE hang doesn't require re-deriving the call chain from scratch. Line numbers inside it are pinned to one build and will drift — the doc says so up front. |
| `UsageShortcut.md` | **Fold into `USERMANUAL.md`** as a fast-path section. |
| `QUESTIONS.md` | **Triage.** 1.2 KB, untouched since 2026-08-22. Answered → `TOMBSTONES.md`; open → `WORKSTREAMS.md`; then archive. |
| `REFERENCE-dspark-shared-expert-fix.md` | **Does not exist.** Cited by `BACKLOG-dspark-sm120-image.md` as "saved as", but absent from `docs/` and the repo root. Either re-save it from the upstream source (tonyd2wild's repo) or delete the citation. The conclusion it supports — the shared-expert bug does not apply to our image — is preserved in `TROUBLESHOOTING.md` and WS-9. |

**The rule that keeps this from drifting again:** a document describing
*current state* must be checkable against code or an artifact. Where it
isn't, it belongs in `TOMBSTONES.md` (history, immutable) or `DIRECTION.md`
(intent, rarely changes) instead. Every stale entry found in the 2026-09-03
synthesis pass was in the third category — a status claim in a document
nobody re-read when the code moved.

**A second rule, learned from `EUGR-NOTES-UPDATE-2026-08-29.md`:** update
the document, don't append a dated companion to it. A `-UPDATE-<date>` file
is a merge someone has to do later, and it is how a doc set becomes 26
files.

**The rule that keeps this from drifting again:** a document describing
*current state* must be checkable against code or an artifact. Where it
isn't, it belongs in `TOMBSTONES.md` (history, immutable) or `DIRECTION.md`
(intent, rarely changes) instead. Every stale entry found in the 2026-09-03
synthesis pass was in the third category — a status claim in a document
nobody re-read when the code moved.

---

## Inputs to the architecture review

`TOMBSTONES.md` #27–#110 is the best evidence available about where this
architecture actually costs money. Sorted by recurrence rather than by
severity, five classes account for the large majority of entries. These are
the agenda for a refactor conversation.

**1. Identity is derived independently in many places.**
#41 (recipe `name:` vs filename), #53 (ledger key vs served basename), #57
(near-duplicate catalog keys), #77 (fuzzy served-name match is ambiguous by
construction), #91 (hash exclusion went stale), #92 (whitespace changed a
hash), #110 (one model, two ledger keys). The recurring shape: "what is this
model called" is answered by a different mechanism in the deploy path, the
telemetry path, the benchmark path, and the dashboard. `config_hash` and
`_resolve_active_recipe()` were both built to centralize this and both only
cover part of it. **This is the single largest class and the strongest
argument for a refactor.**

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
