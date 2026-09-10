# docs/archive/

Retired documents. **Nothing here is current.** Every one was superseded,
merged, or completed, and its content lives somewhere in the living set —
the line under each name says where.

Kept rather than deleted because provenance is repeatedly what made a
finding recoverable. Several corrections in the 2026-09-09 pass were only
possible because a superseded document still said what it used to say, and
two documents archived by mistake that day were restored the next
precisely *because* archiving preserved them. Git history is not a
substitute — nobody greps deleted files.

Retired 2026-09-09/10.

---

## Superseded by `DIRECTION.md`

### `ROADMAP.md`

Direction moved to `DIRECTION.md`, backlog to `WORKSTREAMS.md`. 85 KB. It
had drifted into holding direction, backlog and per-fix rationale at once,
which is why entries in it went stale without anyone noticing.

### `ARCHITECTURE-MIGRATION-PLAN.md`

Phase status moved to `DIRECTION.md`. Phase 3's one architectural
commitment — hosts are fabric-connected pools and a deploy must never span
pools — is preserved there.

### `QUESTIONS.md`

Contained no questions. Four durable design traps (snapshot-vs-counter,
state asymmetry, storage lifecycle, hardware-string parsing), now decisions
of record in `DIRECTION.md`.

## Merged into a living document

### `UsageShortcut.md`

Merged into `USERMANUAL.md`. It was the **newer** of the two, so the merge
ran shortcut → manual, not the reverse. It also contained four paragraphs
pasted in twice.

### `REFERENCE-decode-speeds.md`

Merged into `MODELS.md`, which now owns measured throughput. Note it
characterised `gemma-4-31b` as having "neither quantization nor speculative
decoding"; that is wrong and `MODELS.md` records the correction — the
checkpoint is BF16 but the recipe applies runtime `--quantization fp8`.

### `EUGR-REFERENCE-NOTES.md` and `EUGR-NOTES-UPDATE-2026-08-29.md`

Merged into `docs/reference/community-sources.md`. The `-UPDATE` file was
written as merge instructions against the other and was **never applied**,
so the base file carried a passage its own follow-up called "misleading in
a way that led to a wrong design decision" for eleven days. Both REPLACE
blocks are applied in the merged version.

### `TROUBLESHOOTING.md`

Dissolved. Ten of its fourteen incidents were already covered elsewhere.
Incidents #9 and #12 were covered **nowhere** and became TOMBSTONES #147
and #148. Incident #2's SwiGLU rule became errata **E025** — E017's
evidence block had been citing `incidents: [2]` and would otherwise have
pointed into this archive. Operational guidance went to `USERMANUAL.md`,
the Gemma-4-31b figures to `MODELS.md`.

**Its `Incident #N` numbering is a separate series from `TOMBSTONES.md`'s
`#N`.** Do not merge them without reading TOMBSTONES' citation convention;
a bare `#7` meaning tombstone #71 already cost real time once.

### `BACKLOG-dspark-sm120-image.md`

Five open items are in `WORKSTREAMS.md` WS-9; the annotated link list is in
`docs/reference/community-sources.md`; item 6 (catalog trim) is verified
done. Note it cites `REFERENCE-dspark-shared-expert-fix.md` as "saved as" —
that file has never existed in this repository.

### `BACKLOG-session-tracker-multi-model.md`

Transcribed into `WORKSTREAMS.md` WS-5. Its concrete example is inverted
relative to today: it was written when hermes was on `spark-4`, and the
2026-09-07/08 host swap moved it to `spark-3`. The mechanism it describes
is unchanged.

## Session scaffolding

### `SESSION-HANDOFF-2026-09-06.md`

§7's doc edits are applied; §4 items 5–8 and §8 are transcribed into
`WORKSTREAMS.md` WS-3, WS-5 and WS-8, including the decisive `docker
inspect` check for the open hermes-deploy question.

### `SESSION-CLOSEOUT-2026-09-02-FINAL.md`

Ported to WS-5. Roughly half the file was new-chat scaffolding and file
lists.

## Completed phases

### `PHASE-2-PROMPTS.md`

Phase 2 complete.

### `PHASE-MODS-PROMPTS.md`

Mods phase complete. Records only M0's results, so it reads as though the
sequence stalled at the gate; MA/MB/MC results were never appended.

### `MA-REVIEW.md`, `MB-REVIEW.md`, `MC-REVIEW.md`, `MD-REVIEW.md`, `ME-REVIEW.md`, and `TESTING-MB.md` / `TESTING-MC.md`

Per-task review and test notes from the mods phase, ~185 KB. The durable
content is each review file's **Contradictions** section — `MD-REVIEW.md`'s
is `errata.yaml` material (an empty `2_node.vllm_args` makes `use_ray`
evaluate false and routes to the `--headless` path).

**Checked before archiving, 2026-09-10.** `TOMBSTONES.md` #85 does cite
`M{X}-REVIEW.md`'s Contradictions section, item 5 — but as "for the full
reasoning" behind a decision the entry already states completely
(`ModBakeError` partial-2-node behaviour was left matching existing
`docker run` partial-failure behaviour rather than given new rollback
semantics). Supporting detail, not load-bearing, and it is preserved here.
No tombstone cites `TESTING-MB.md` or `TESTING-MC.md` at all. The two
TESTING files came from `tests/`, not `docs/`.

---

## Deleted, not archived

### `SESSION-SEED.md`

The only file deleted. Not merely stale — it cites TOMBSTONES #76 as
current (now #148), recommends `orthozany/vllm-jasl-dsv4` which is
confirmed x86_64-only and fails on GB10, names a recipe that no longer
exists, and frames DSpark as broken when it runs at 42–44 tok/s. As
onboarding it actively misdirects, and archiving would leave that trap
findable.

### `docs/README.md`

Deleted rather than archived. It was a diverged near-duplicate of the root
`README.md`, and its unique sections — crash-log persistence, version
tracking, status staleness, the config-derived host identity paragraph, and
four CLI commands — were folded into the root copy first.

---

## Deliberately NOT here

### `SMOKE-TEST-PLAYBOOK.md` and `REFERENCE-control-surfaces.md`

Both were archived on 2026-09-10 by mistake and restored to `docs/`.

`SMOKE-TEST-PLAYBOOK.md` is a living document, listed in `README.md`'s doc
map, `DIRECTION.md`'s living table and `DOCMAP.md`. Nothing supersedes it.

`REFERENCE-control-surfaces.md` carries its own retirement condition —
"when the interface spike lands" — which has not happened. It is
deliberately absent from the doc map so it can be deleted later without
leaving a dangling reference; archiving it says it is dead when it is not.
Its condition is tracked in `WORKSTREAMS.md` WS-0 item 8c so it is known
outside the file itself.

### `REFERENCE-flashinfer-autotune-internals.md`

Live reference material, now at `docs/reference/`. Its line numbers are
pinned to one build and will drift; the file says so up front.

*(nothing else)*
