#!/usr/bin/env python3
"""
model_ledger.json cleanup, in advance of fuzzy-match ETA estimation.

Two kinds of operation, applied in that order -- DROPs first, then REKEYs,
so a REKEY destination can never collide with a key this same run is about
to remove:

DROP -- removes a key outright. Four classes, all explicit allowlists or
narrow structural signatures, never a fuzzy heuristic:

  1. PLACEHOLDER  -- "Active Container" / "None" / "Unknown". These are
     _discover_host_container()'s fallback when the `docker inspect` Cmd parse
     fails (dgx-orchestrator.py:2065/2076/2078), propagated verbatim through
     _resolve_catalog_key()'s no-match fallback. The load times underneath are
     real measurements of *some* model -- we just cannot say which, so they
     are unattributable rather than stale.

  2. SMOKE-TEST  -- any key whose model segment starts with "_". Confirmed
     convention: _scratch-*, _m0_probe are smoke-test residue. Correctly
     recorded, but not production recipes.

  3. RAW-BASENAME -- CamelCase HF basenames written before
     _resolve_catalog_key() became the single source of truth. Signature is
     consistent: `lifetime` populated, no `launch_history`, no
     `last_seen_raw`, no phase timings.

  4. ORPHANED-RECIPE -- the recipe file this key's stem names has been
     deleted, and the specific entry has no diagnostic content worth
     preserving under a new name (see REKEY below for the case where it
     does). Explicit allowlist, same as RAW-BASENAME: the ledger keys on
     filename stem and never deletes on its own (by design -- see
     TOMBSTONES.md #91), so this class only grows by a human confirming a
     specific deletion, never by a heuristic guessing one.

REKEY -- moves an entry to a new key, optionally dropping named fields from
the moved entry first. For when the old key's data has real diagnostic value
(phase timings, `runs[]`) that a plain DROP would discard, but the key
itself no longer names anything live -- typically a recipe rename where the
new file is a different key under this repo's dot-vs-underscore history
(TOMBSTONES.md #110) or similar. Refuses (does not partially apply) if the
destination key already exists, since silently merging two entries can
double-count `lifetime` totals from overlapping engine sessions -- see
TOMBSTONES.md #72's set-vs-add hazard and #110's own writeup in
WORKSTREAMS.md WS-4 for why. A destination collision is reported and left
for a human to resolve, never auto-merged.

Deliberately NOT touched:
  - Statistical outliers (e.g. gemma-4-31b::2_node downloaded [462, 14276, 440]).
    That is a mean-vs-median problem, handled separately.
  - Phase misclassification (compiled samples faster than cached samples on
    several deepseek keys). Not correctable without ground truth.

Idempotent: re-running on already-cleaned output is a no-op -- a REKEY
source key that no longer exists, or a DROP key that no longer exists, is
silently skipped rather than erroring.

Usage:
    python3 clean_ledger.py <ledger.json>                 # dry run, prints plan
    python3 clean_ledger.py <ledger.json> --apply         # writes in place
    python3 clean_ledger.py <ledger.json> --apply -o OUT  # writes to OUT
"""

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

PLACEHOLDER_NAMES = {"active container", "none", "unknown", ""}

# Explicit allowlist, not a heuristic. A generic "looks CamelCase" test would
# also catch a legitimately-uppercase future recipe key; these two are the
# only raw-basename keys actually present, and both are provably unmergeable
# (see _classify_drop()).
RAW_BASENAME_KEYS = {
    "DeepSeek-V4-Flash-0731::2_node",
    "Qwen3.8-27B-NVFP4::2_node",
}

# Explicit allowlist. A key only lands here after a human has confirmed the
# named recipe file no longer exists AND decided the entry has nothing worth
# rekeying (contrast REKEY_MAP below, for the case where it does).
ORPHANED_RECIPE_KEYS = {
    # nemotron-3.5-lightning-bf16.yaml (dot form) deleted 2026-09-03 --
    # TOMBSTONES.md #110. This key held only lifetime/last_seen_raw token
    # counts with no launch_history, no runs[] -- no config_hash to attribute
    # them to, no phase data to preserve. Its 1_node sibling has real data
    # and is a REKEY below instead, not a DROP.
    "nemotron-3.5-lightning-bf16::2_node":
        "orphan of deleted recipe (#110); no launch_history/runs[], "
        "nothing worth preserving",
}

# Explicit allowlist: old_key -> (new_key, fields_to_strip, reason).
# fields_to_strip are dropped from the moved entry before it's written under
# new_key. Refuses if new_key already exists in the ledger -- see module
# docstring.
REKEY_MAP: dict[str, tuple[str, list[str], str]] = {
    # nemotron-3.5-lightning-bf16.yaml (dot form) deleted 2026-09-03 --
    # TOMBSTONES.md #110. This key's launch_history attests to config_hash
    # 65c268515202a4f7, computed against the now-deleted file; the surviving
    # recipe (nemotron-3_5-lightning-bf16.yaml) adds an `image:` field the
    # deleted one never had, so that hash can never join to it regardless of
    # what happens to the rest of the entry. Everything else -- cached[],
    # compiled[], runs[] -- is real phase data with no such attribution
    # problem and is preserved under the surviving recipe's key so the ETA
    # estimator doesn't lose its only 1_node history for this model.
    #
    # NOTE (WORKSTREAMS.md F-c): this key's launch_history hash
    # (65c268515202a4f7) does not match its own runs[] entry's config_hash
    # (7f46f3161f0d6a4d). Unexplained, predates this rekey, and is a
    # separate question from whether the rekey itself is safe -- flagged
    # here so it isn't lost, not because it blocks the move.
    "nemotron-3.5-lightning-bf16::1_node": (
        "nemotron-3_5-lightning-bf16::1_node",
        ["launch_history"],
        "recipe renamed to underscore form (#110); launch_history's "
        "config_hash is stale against the deleted file and cannot join "
        "to the surviving recipe regardless",
    ),
}


def _split_key(key: str) -> tuple[str, str]:
    """Split 'model::topo' -> ('model', 'topo'). Keys without '::' return
    ('', key) so they fall through as unrecognised rather than crashing."""
    if "::" not in key:
        return "", key
    model, _, topo = key.rpartition("::")
    return model, topo


def _classify_drop(key: str) -> tuple[str, str] | None:
    """Return (class, reason) if the key should be dropped outright, else
    None. Does not consider REKEY_MAP -- a key can appear in at most one of
    the two tables, enforced in main()."""
    model, _topo = _split_key(key)

    if model.strip().lower() in PLACEHOLDER_NAMES:
        return ("PLACEHOLDER", "container-name parse fallback; unattributable")

    if model.startswith("_"):
        return ("SMOKE-TEST", "smoke-test convention; not a production recipe")

    if key in RAW_BASENAME_KEYS:
        return ("RAW-BASENAME", "pre-_resolve_catalog_key HF basename; ambiguous owner")

    if key in ORPHANED_RECIPE_KEYS:
        return ("ORPHANED-RECIPE", ORPHANED_RECIPE_KEYS[key])

    return None


def _summarise(entry) -> str:
    """One-line description of what a key actually holds, so the plan shows
    what is being given up (DROP) or carried over (REKEY) rather than just
    naming the key."""
    if not isinstance(entry, dict):
        return f"non-dict entry ({type(entry).__name__})"

    bits = []
    for phase in ("cached", "compiled", "downloaded"):
        samples = entry.get(phase) or []
        if samples:
            mean = int(sum(samples) / len(samples))
            bits.append(f"{phase} n={len(samples)} mean={mean}s")

    life = entry.get("lifetime") or {}
    if any(life.values()):
        bits.append(
            "lifetime in={in_} out={out} draft={draft} acc={acc}".format(
                in_=life.get("in", 0), out=life.get("out", 0),
                draft=life.get("draft", 0), acc=life.get("accepted", 0),
            )
        )

    if entry.get("launch_history"):
        bits.append(f"launch_history x{len(entry['launch_history'])}")
    if entry.get("last_seen_raw"):
        bits.append("last_seen_raw")
    if entry.get("runs"):
        bits.append(f"runs x{len(entry['runs'])}")

    return "; ".join(bits) if bits else "empty"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ledger", type=Path, help="path to model_ledger.json")
    ap.add_argument("--apply", action="store_true",
                    help="actually write; default is dry run")
    ap.add_argument("-o", "--output", type=Path, default=None,
                    help="write here instead of in place")
    ap.add_argument("--no-backup", action="store_true",
                    help="skip the .bak-<ts> copy when writing in place")
    args = ap.parse_args()

    # Fail loudly at import/parse time, not silently at runtime, if a key
    # was ever added to both tables -- the two operations are meant to be
    # mutually exclusive per source key.
    overlap = set(REKEY_MAP) & set(ORPHANED_RECIPE_KEYS)
    if overlap:
        print(f"[!] key(s) in both REKEY_MAP and ORPHANED_RECIPE_KEYS: "
              f"{sorted(overlap)} -- fix the tables, refusing to run", file=sys.stderr)
        return 2

    if not args.ledger.is_file():
        print(f"[!] not a file: {args.ledger}", file=sys.stderr)
        return 2

    try:
        data = json.loads(args.ledger.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[!] could not read ledger: {exc}", file=sys.stderr)
        return 2

    if not isinstance(data, dict):
        print(f"[!] ledger root is {type(data).__name__}, expected object", file=sys.stderr)
        return 2

    # ---- plan: drops ----
    drops: dict[str, tuple[str, str]] = {}
    for key in data:
        verdict = _classify_drop(key)
        if verdict:
            drops[key] = verdict

    # ---- plan: rekeys ----
    # Present in the ledger, and not scheduled for drop (can't be, per the
    # overlap check above, but keep the guard cheap and explicit).
    rekeys: dict[str, tuple[str, list[str], str]] = {
        old: spec for old, spec in REKEY_MAP.items()
        if old in data and old not in drops
    }

    # A rekey destination colliding with an EXISTING, un-dropped key is a
    # conflict this tool refuses to resolve on its own -- see docstring.
    # A destination that only exists because this same key used to be there
    # (idempotent re-run) is fine and not a conflict.
    rekey_conflicts: dict[str, str] = {}
    for old, (new, _fields, _reason) in rekeys.items():
        if new in data and new != old:
            rekey_conflicts[old] = new
    for old in rekey_conflicts:
        del rekeys[old]

    skipped_missing = [old for old in REKEY_MAP if old not in data and old not in drops]

    total_ops = len(drops) + len(rekeys)
    print(f"[*] {args.ledger}: {len(data)} keys -- "
          f"{len(drops)} to drop, {len(rekeys)} to rekey, "
          f"{len(rekey_conflicts)} rekey conflict(s), "
          f"{len(data) - len(drops) - len(rekeys)} untouched\n")

    if rekey_conflicts:
        print("  REKEY CONFLICT -- destination already exists, not touching either side")
        for old, new in rekey_conflicts.items():
            print(f"    - {old}  ->  {new}  (destination already present)")
            print(f"        source : {_summarise(data[old])}")
            print(f"        dest   : {_summarise(data[new])}")
            print(f"        Resolve manually -- this tool will not guess whether")
            print(f"        these represent the same engine lifetime (see")
            print(f"        TOMBSTONES.md #72 on why summing lifetimes can")
            print(f"        double-count).")
        print()

    if skipped_missing:
        print(f"  (skipped, already absent -- idempotent re-run: {skipped_missing})\n")

    if total_ops == 0 and not rekey_conflicts:
        print("[*] nothing to do -- ledger is already clean.")
        return 0

    for cls in ("PLACEHOLDER", "SMOKE-TEST", "RAW-BASENAME", "ORPHANED-RECIPE"):
        members = [(k, r) for k, (c, r) in drops.items() if c == cls]
        if not members:
            continue
        print(f"  DROP: {cls}")
        for key, reason in members:
            print(f"    - {key}")
            print(f"        reason : {reason}")
            print(f"        holds  : {_summarise(data[key])}")
        print()

    if rekeys:
        print("  REKEY")
        for old, (new, fields, reason) in rekeys.items():
            entry = data[old]
            stripped = [f for f in fields if f in entry]
            print(f"    - {old}")
            print(f"        ->     {new}")
            print(f"        reason : {reason}")
            print(f"        holds  : {_summarise(entry)}")
            if stripped:
                print(f"        strips : {stripped}")
        print()

    if not args.apply:
        print("[*] dry run -- nothing written. Re-run with --apply to commit.")
        return 0

    cleaned = {k: v for k, v in data.items() if k not in drops}
    for old, (new, fields, _reason) in rekeys.items():
        entry = dict(cleaned.pop(old))
        for field in fields:
            entry.pop(field, None)
        cleaned[new] = entry

    target = args.output or args.ledger

    if target == args.ledger and not args.no_backup:
        backup = args.ledger.with_suffix(f".json.bak-{int(time.time())}")
        shutil.copy2(args.ledger, backup)
        print(f"[+] backup: {backup}")

    # Write via a sibling temp file + replace so a crash mid-write cannot
    # leave a truncated ledger behind -- the daemon reads this file on every
    # 4s status poll.
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(cleaned, indent=2) + "\n")
    tmp.replace(target)
    print(f"[+] wrote {target}: {len(cleaned)} keys")

    if rekey_conflicts:
        print(f"[!] {len(rekey_conflicts)} rekey conflict(s) left unresolved -- "
              f"see above, exiting 1 so this doesn't look fully clean", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
