# Session Closeout — ETA/telemetry rework — FINAL (2026-09-02)

Supersedes both prior closeout docs from this session. This is the
complete record: what shipped, what's confirmed, what's genuinely left,
and a ready-to-use prompt for a new chat when one is needed.

## Status: feature complete, live, verified

| | Task | Status |
|---|---|---|
| A | Log archive capture | **Live, verified.** 5 real archives captured and analyzed across 3 distinct vLLM log vocabularies. |
| B | Config registry | **Live, verified.** 28 hashes, 0 collisions, exact hash match confirmed against an offline computation. |
| C | Phase-based run recording | **Live, verified, bug found and fixed.** First live capture after deploy hit a real gap (below); fix built, verified against the actual triggering archive, redeployed, confirmed live via clean restart + the tainted pre-fix entry's absent `pre_load_confidence` field proving the daemon picked up the new code. |
| D | Tiered ETA reader | **Live, deployed, correctly inert so far.** No key has 3+ same-slice runs yet — expected, not a problem. Zero behavior change confirmed against the pre-existing ledger before any of this landed. |

`orchestrator_version`: `+6b344453`, confirmed live, clean restart, no
traceback. `clean_ledger.py` run against the live ledger: 17 keys, 0 to
drop — genuinely clean, not just "no junk this run." Idempotency
confirmed directly: dry-run and `--apply` both reported identical
"0 to drop" back to back.

TOMBSTONES.md: 74 entries, #27–#100, contiguous, no gaps, no
duplicates.

## What "feature complete" means here, concretely

- Every load is archived in full (gzipped, redacted, provenance-tagged),
  not observed-and-discarded.
- Every load's phase durations are extracted from vLLM's own
  self-reported timing where possible, derived from timestamps only
  where nothing better exists, and the derived/degenerate cases are
  distinguishable from real measurements via explicit confidence fields
  (`pre_load_confidence`, `compile_stage_confidence`) rather than
  silently indistinguishable.
- Every config that has ever been launched (or even just loaded into the
  catalog) is decodable back to its exact payload via
  `config_hash` → `config_registry.json`, with genuine collisions
  visible and renames no longer misread as collisions.
- The ETA estimator prefers real phase-based history over the old
  keyword-guessed buckets once there's enough of it, never blends the
  two, and changes nothing for any key that doesn't have enough new data
  yet.
- A parser encountering a vocabulary it doesn't recognize fails loudly
  or flags the specific field it couldn't measure, rather than returning
  a plausible-looking wrong number. This was tested against real
  unexpected input during the session (the gemma-4-31b build) and
  worked as designed once the gap it exposed was fixed.

That's the actual scope of "the feature." Nothing above requires further
work to be true today.

## Backlog — real, not blocking, in rough priority order

1. **`compile_stage_confidence` is presence, not magnitude.** The real
   gemma4 archive is a *warm* run and still self-reports a small
   `torch.compile` pass every launch — presence/absence alone doesn't
   distinguish "trivial per-launch compile" from "genuine expensive cold
   JIT." Needs a magnitude threshold; no genuine cold-compile sample
   exists yet to calibrate one against.

2. **No download-phase marker exists.** All 5 real archives were warm
   starts with weights already on disk. `load_type="downloaded"` never
   uses tier 1 by design (see `_runs_slice_for_load_type`'s docstring).

   **Re: the SM120/DSpark pull as a source for these two** — it will
   very likely resolve #1 (a new vLLM fork means a genuine cold JIT
   compile), but probably **won't** resolve #2. A cold compile and a
   cold HF weight download are different events; the SM120 recipe almost
   certainly targets weights already cached on this cluster from prior
   dspark work. Don't expect one pull to close both gaps — plan for #2
   separately, whenever a model's weights are loaded for the first time
   on a node.

3. **Two open questions from earlier in this session, never resolved:**
   - Does `resolve_mod_tag()`'s digest hash mod *names* or mod
     *contents*? If names, editing `mods/<name>/run.sh` leaves the tag
     unchanged, `ensure_mods_baked()` treats it as a cache hit, and the
     edit silently never reaches the container. Settle via two dry-runs
     either side of a real edit — **don't paste raw dry-run output**,
     it has leaked `HF_TOKEN` in plaintext before.
   - Is `capability` still genuinely inert and safe to exclude from
     `config_hash`? Excluded on the same "inert metadata" assumption
     that went stale for `mods` (TOMBSTONES #91) — never re-verified.

4. **The live in-progress countdown is unchanged.**
   `detect_model_stage()`'s log-keyword phase guess, used while a
   container is still loading, is untouched by Tasks C/D and can still
   flip mid-load for the reason the `index.html` ETA clamp exists in the
   first place. A real fix here is a bigger, unscoped piece of work —
   plausibly reading toward the same self-reported-duration lines
   `phase_extract.py` already knows how to parse, live rather than
   post-hoc, but that's a design question, not a quick patch.

5. **The tainted pre-fix `gemma-4-31b::2_node` ledger entry** — no
   action needed, noted for completeness. `pre_load_sec` is wrong in
   that one entry, `total_sec` is fine, nothing currently reads the
   wrong field for anything that matters, and it'll dilute out of any
   median or age out past the 20-run cap on its own.

## Prompt for a new chat, when one is needed

Use this as the opening message. Fill in the bracketed context line for
whatever actually prompted the new chat — general debugging, the SM120
work, or something else entirely. Everything else in the prompt applies
regardless of trigger.

---

> I'm continuing work on a DGX Spark cluster orchestrator
> (`imrobertson/orchestrator`) — specifically an ETA/telemetry rework
> that shipped and was fully verified live in a prior session (Tasks
> A–D: run-log archiving, a config-hash decoder ring, phase-based load
> recording replacing keyword-guessed buckets, and a tiered ETA
> estimator). I'm attaching that session's closeout doc
> (`SESSION-CLOSEOUT-2026-09-02-FINAL.md`) — read it in full before
> doing anything else, it has the complete status, the backlog, and
> several corrections to claims made earlier in that session that you
> should trust over anything else you might infer independently.
>
> [Why I'm here: e.g. "The DSpark/SM120 pull happened and I have a
> genuinely cold-compile archive now — want to calibrate the
> compile_stage_confidence magnitude threshold backlog item" / "Live
> load estimates look wrong for X" / "Something broke after deploying
> Y" — state the actual trigger here.]
>
> Also attached: the current `dgx-orchestrator.py`, `common/recipes.py`,
> `common/runlog.py`, `common/phase_extract.py`, and `TOMBSTONES.md`
> from that session. If I have newer real run-log archives (`.log.gz`
> files) captured since, I'll attach those too — check their vocabulary
> against what `phase_extract.py` already knows before assuming they'll
> parse cleanly; this session found a real gap in exactly that spot on
> the very first live capture after deploy (TOMBSTONES #100), so don't
> assume a new build's log phrasing matches what's already handled.
>
> Working style: small sequenced changes, full replacement files, test
> against real captured data before trusting a fix, state assumptions
> and known gaps explicitly rather than implying more confidence than
> the evidence supports, and surface contradictions or mistakes directly
> rather than quietly revising past claims.

---

## Files needed for that new chat

By the time it happens, more real archives will likely exist — attach
whatever's accumulated, not just what's listed below from this session.

**Essential — new chat can't safely continue without these:**
- `dgx-orchestrator.py` (current: `+6b344453`)
- `common/recipes.py`
- `common/runlog.py`
- `common/phase_extract.py` (**the post-fix version — confirm you're
  attaching this session's final copy, not an earlier download from
  mid-session**)
- `TOMBSTONES.md` (74 entries, #27–#100)
- This closeout document

**Very likely relevant if the trigger is SM120/cold-compile work
specifically:**
- `BACKLOG-dspark-sm120-image.md`
- Whatever fresh `.log.gz` archive(s) the SM120 pull produces —
  ideally both a genuinely cold run AND at least one subsequent warm
  rerun of the same config, so the compile-duration magnitude can be
  compared against a same-config warm baseline rather than eyeballed
  against a different model's numbers

**Helpful, situational:**
- `index.html` (only if dashboard/display work is in scope)
- `clean_ledger.py` (already run and confirmed idempotent this
  session — only needed again if the ledger has accumulated new junk)
- The current live `model_ledger.json` or `config_registry.json`, if
  the new chat's task involves reasoning about accumulated real data
  rather than just code

## Key numbers for reference

- Live `orchestrator_version`: `+6b344453`
- Live ledger: 17 keys, confirmed clean, idempotency confirmed
- TOMBSTONES: 74 entries, #27–#100
- Real archives analyzed: 5, across 3 vLLM log vocabularies
  (gemma4-MTP-style, dspark-style, gemma-4-31b-style)
- `gemma4-26b-a4b-nvfp4::1_node` config_hash: `6c1b11350dd75caa`
- `MIN_RUNS_FOR_PHASE_ESTIMATE`: 3, applied per-slice not per-key
- Corrected pre-load fractions (supersedes an earlier wrong 58–65%
  claim from mid-session): gemma4 1_node 13% · dspark 2_node 32–33% ·
  gemma-4-31b 2_node 31%
