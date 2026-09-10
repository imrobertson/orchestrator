# DOCMAP

Paste this at the start of a session. It says which document owns what, and
which one to write to. It deliberately contains no status, no version numbers,
and no cluster state — those go stale, and a stale map is worse than none.

Repo: `imrobertson/orchestrator`. Control plane for vLLM on a GB10 DGX Spark
cluster, operated from an off-node host (`maestro`).

---

## Read order for a cold start

1. `README.md` — what the system is and how it fits together
2. `docs/DIRECTION.md` — where it's going, and the decisions already settled
3. `docs/WORKSTREAMS.md` — what is actually open

Then whichever of the below the task touches. Don't read the rest.

---

## Who owns what

| Document | Owns | Does **not** own |
|---|---|---|
| `README.md` | Architecture, what runs where, `cluster_config.yaml` reference, recipe schema overview | Anything time-sensitive; anything measured |
| `docs/INSTALL.md` | Standing up a control station and preparing compute nodes | Day-to-day operation |
| `docs/USERMANUAL.md` | Operating the cluster: dashboard, `dgx-config`, deploying, teardown, offline mode, benchmarking, troubleshooting | Design rationale; per-model facts |
| `docs/MODELS.md` | The catalog: which recipes exist, topology, measured speed, what each is good for, validation status | How to deploy; why the system works this way |
| `docs/DIRECTION.md` | Intent, phase status, decisions of record | Individual open items; per-fix detail |
| `docs/WORKSTREAMS.md` | The canonical backlog: status, evidence, dependencies, kickoff prompts | History; intent |
| `docs/TOMBSTONES.md` | Append-only per-fix history and incident log. Newest entry at top, highest number | Current status of anything |
| `docs/errata.yaml` | Machine-readable recipe rules with provenance — the linter's source of truth | Prose narrative |
| `docs/reference/` | Durable notes on third-party internals (FlashInfer, upstream eugr conventions) | Our own history or plans |
| `docs/archive/` | Retired documents, kept for provenance only | Anything. Do not read unless tracing a specific historical claim |

---

## Where a new fact goes

Decide by what *kind* of claim it is, not by which file you happen to have open.

| The thing you learned | Goes in |
|---|---|
| A bug and its fix | `TOMBSTONES.md`, new entry at top |
| A recipe flag combination that does or doesn't work | `errata.yaml` |
| A measured throughput or latency number | `MODELS.md` |
| A new open item, or a change to one | `WORKSTREAMS.md` |
| A decision about how the system should work | `DIRECTION.md` |
| A new command, flag, or operating procedure | `USERMANUAL.md` |
| A change to setup or prerequisites | `INSTALL.md` |
| Something about a third-party dependency's internals | `docs/reference/` |

---

## Source of truth, when documents disagree

Documents lose to artifacts. In descending order:

1. **The code and config.** `cluster_config.yaml`, `common/config.py`,
   `common/recipes.py`, `dgx-orchestrator.py`, `html/index.html`,
   `recipes/*/`.
2. **The running system.** `dgx-config status`, `/api/status`, the dashboard.
3. **Ledgers.** `model_ledger.json`, `benchmark_ledger.csv`.
4. **These documents.**

Host identity, reserved-ness, ports, and default deploy target all derive from
`cluster_config.yaml` at daemon start. Never assert which host is head, which
is reserved, or which is the default target from a document — read the file.
The `hosts:` block's **order is load-bearing**: the first entry is
`PRIMARY_HOST`, the 2-node Ray head, and the host whose `/metrics` feed session
tracking. Reordering it moves all three.

---

## Rules that keep this from becoming 31 files again

- **Update the document. Never append a dated companion to it.** No
  `-UPDATE-<date>`, no `SESSION-HANDOFF-<date>`, no `-FINAL`. A dated companion
  is a merge someone has to do later, and it is how this doc set got to 31.
- **A document describing current state must be checkable against code or an
  artifact.** If a claim can't be checked that way, it is either history
  (`TOMBSTONES.md`) or intent (`DIRECTION.md`), not status.
- **Don't carry history in a current document.** A living doc says what is true
  now. "This used to behave differently" belongs in `TOMBSTONES.md`, with the
  living doc citing the entry number if the old behaviour still confuses people.
- **One fact, one home.** If you're about to write something that already lives
  in another file, link to it instead. Duplicated prose diverges; it always has
  here.
- **Retire by moving to `docs/archive/`, not by leaving it in place.** A doc
  left in `docs/` is a doc someone will read and believe.

---

## Working conventions

- Full replacement files, not diffs or patches.
- Small sequenced changes. Call out cross-file edits explicitly rather than
  expanding scope silently.
- Verify claims against the actual code or a real run. Do not state a mechanism
  you inferred as though you read it — say which one it was.
- `--dry-run` before trusting anything on the deploy path.
- Two benchmark runs minimum before a number is recorded, and record the
  **range**, not the mean.
- After deploying an orchestrator change, confirm it landed: version badge next
  to Server Time, or `orchestrator_version` in `/api/status`. Note it hashes
  `dgx-orchestrator.py` only — a change confined to `common/*.py` will not move
  it.
- Flag contradictions directly. Leave corrections visible rather than quietly
  revising an earlier claim.
