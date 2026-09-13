# tests/

What each of these is for, and which to reach for. Written 2026-09-10,
when a tree walk found six files here that no document mentioned.

**Three different questions, three different tools.** Reaching for the
wrong one is how this directory grew a near-duplicate harness twice.

| Question | Tool | Cost |
|---|---|---|
| Is variant A faster than variant B? | `ab_test.py` | a weight load per variant, per repeat |
| Is the model currently serving any *good*? | `agentic_eval.py` | seconds; deploys nothing |
| Does the code still do what it did? | `test_*.py` | instant |

---

## `ab_test.py` — variant comparison

Drives the full deploy → health → benchmark → teardown cycle for one or
two variants. `--repeats N` runs each variant independently N times and
prints `n`, mean, `range=min-max` and every value — **this is the tool
that satisfies the two-runs-minimum rule**, not `benchmark.py`, which can
only give you one warm mean.

Full flag reference: `docs/AB_TEST_USAGE.md`.

Two constraints that are structural, not bugs. It **refuses a reserved
host with no `--force` of its own**, deliberately — forcing onto a
reserved node should be an explicit `dgx-config deploy --force`, not
something saved into a benchmark invocation. And a 2-node comparison is
therefore unavailable while either host is reserved (`WORKSTREAMS.md`
WS-7, blocking K12).

## `agentic_eval.py` — is the model any good

Talks to whatever is already serving. **Never deploys, tears down, or
touches cluster state**, which is why it is safe against a reserved host
and why it is not folded into `ab_test.py`.

Five tests; three execute or verify rather than string-match:

1. `tool_loop` — cheap canary. A recipe missing
   `--enable-auto-tool-choice` / `--tool-call-parser` 400s here in
   seconds, before anything expensive runs.
2. `multi_hop` — read two files, correlate, answer.
3. `coding` — emitted `count_vowels()` is **executed** against five cases.
4. `bugfix` — find a seeded bug via tools, emit the corrected file; the
   result is **executed** and checked against the README's constraint.
5. `budget` — reasoning tokens burned before a one-word answer. This one
   earned its place: `qwen-3.6-35b-a3b-nvfp4-nothink` exists *because*
   thinking mode scored 0/5 on the coding suite.

```bash
python3 tests/agentic_eval.py --host 10.0.14.41 --port 8000
python3 tests/agentic_eval.py --host … --skip bugfix,budget --out /tmp/x.json
```

Merged 2026-09-10 from the old `agentic_eval.py` and `h2h_agentic.py`,
which shared a byte-identical `chat()` and had diverged. Kept the former's
plumbing (`--out`, stale-file clear, HTTP-body surfacing, `exit 2`) and the
latter's harder tasks. Two bugs fixed on the way in, both of which made
results *wrong* rather than merely noisy — see the module docstring.

**It has no decode-speed test on purpose.** `h2h_agentic.py` had one that
counted SSE deltas instead of tokens, which undercounts every
speculative-decode model by a factor that varies with acceptance rate —
TOMBSTONES #52, reintroduced in a copy. `benchmark.py` measures decode from
`stream_options` usage and was hardened for exactly this. One
implementation.

## `run_overnight_tp_pp_ab.sh` + `pairs.txt` — unattended sweeps

Runs `ab_test.py` over a list of variant pairs, one at a time, each to its
own timestamped log, continuing past failures and summarising at the end.
Reads `pairs.txt` (`label|variant_a|variant_b[|nodes]`) or takes pairs as
arguments.

Despite the name it is **not** TP-vs-PP specific — generalised 2026-09-03
to take any pair list, which covers parameter sweeps such as the
`qwen-3.5-122b` MTP token-depth comparison. Run it inside `tmux`.

A PASS means both sides booted and benchmarked, **not** that either won.
Read the `AGGREGATE ACROSS N REPEATS` block for that.

## `test_config.py`, `test_recipes.py`, `test_ssh.py`

Plain-assert tests for the matching `common/` module. Run them directly,
one at a time; pytest is not provisioned by this repository:

```bash
python3 tests/test_config.py
python3 tests/test_recipes.py
python3 tests/test_ssh.py
```

GitHub Actions runs those three scripts on Python 3.12 for every pull request
and every push to `main`. It also compiles all Python sources and runs the
documentation-reference, documentation-health, and tracked-file inventory
checks. The workflow is intentionally made from the repository's existing
commands rather than introducing pytest as a second test runner.

`test_recipes.py` compares the live recipe names and topology keys against
`data/recipe_catalog_snapshot.json`. This catches additions, removals, renames,
and topology changes without embedding counts throughout the assertions. It
also enforces an independent minimum of 30 recipes, so accidentally regenerating
the snapshot from a badly truncated catalog still fails. After reviewing an
intentional catalog change, update the snapshot with:

```bash
python3 tests/test_recipes.py --update-catalog-snapshot
python3 tests/test_recipes.py
```

Commit the recipe and snapshot together. Do not regenerate the snapshot merely
to make an unexplained CI failure green.

## `smoke_test_mc.py`, `smoke_test_mods.py`

Post-change smoke tests from the mods phase. Procedure:
`docs/SMOKE-TEST-PLAYBOOK.md`.

> **`smoke_test_mc.py` T9 tests a feature that no longer exists.** It
> writes a `models.yaml` into a temp dir and sets `USE_LEGACY_CATALOG=1` to
> exercise the legacy fallback, removed by TOMBSTONES #112. Either it
> passes and asserts nothing, or it has been red and unnoticed. Same class
> as `tools/verify/verify_recipe_equivalence.py`; same remedy — delete the
> case or banner it. `WORKSTREAMS.md` §6.

## `data/`, `logs/`

`data/` holds sweep result TSVs kept as reference. `logs/` is A/B run
output and is gitignored — anything in it that matters belongs in
`docs/MODELS.md`.

---

## Retired 2026-09-10

`sweep.py`, `sweep2.py`, `h2h_agentic.py`, `h2h_debug.py`, and four
`sweep*` result/transcript files that sat at the repo root.

`sweep.py` and `sweep2.py` were single-host overnight sweeps superseded by
`run_overnight_tp_pp_ab.sh`, whose own header records the 2026-09-03
generalisation done specifically "so it also covers things like the
qwen-3.5-122b MTP token-depth sweep **without a second script**". They had
also rotted past use: both hardcoded `/home/ian/docker/orchestrator` (the
repo is at `dev/`), both targeted `spark-3` while asserting *"NEVER touches
spark-4 (reserved guard enforces it)"* — the hosts swapped, so the guard now
protects `spark-3` and every deploy would be refused — and `sweep2.py`'s
entire candidate list referenced recipes that no longer exist.

Their `health_up()` also returned True on **empty** curl output, which is
absence-as-pass (#94's shape), and both passed `--model-key <x>-sweep`, so
their ledger rows could never join the catalog (#53's shape). `sweep2.py`
used one constant key for all three depth variants, making them
indistinguishable in the ledger — which defeated the sweep.

Worth knowing: `sweep2.py` is the provenance of the two figures in
`qwen-3.6-35b-a3b-nvfp4.yaml`'s header — 62.8 tok/s with
`moe-backend=latency`, 55.9 at MTP n=6. Those recipes were deleted; the
numbers survive only in that comment.
