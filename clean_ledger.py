#!/usr/bin/env python3
"""
One-time model_ledger.json cleanup, in advance of fuzzy-match ETA estimation.

Removes three classes of key that are inert today (nothing reads across keys)
but become active contaminants the moment get_estimated_load_time() starts
scanning sibling keys:

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

Deliberately NOT touched:
  - Statistical outliers (e.g. gemma-4-31b::2_node downloaded [462, 14276, 440]).
    That is a mean-vs-median problem, handled separately.
  - Phase misclassification (compiled samples faster than cached samples on
    several deepseek keys). Not correctable without ground truth.
  - Keys with current-schema shape but sparse data (e.g.
    nemotron-3.5-lightning-bf16::2_node). Legitimate, just incomplete.

Idempotent: re-running on already-cleaned output is a no-op.

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
# (see _classify()).
RAW_BASENAME_KEYS = {
    "DeepSeek-V4-Flash-0731::2_node",
    "Qwen3.8-27B-NVFP4::2_node",
}


def _split_key(key: str) -> tuple[str, str]:
    """Split 'model::topo' -> ('model', 'topo'). Keys without '::' return
    ('', key) so they fall through as unrecognised rather than crashing."""
    if "::" not in key:
        return "", key
    model, _, topo = key.rpartition("::")
    return model, topo


def _classify(key: str) -> tuple[str, str] | None:
    """Return (class, reason) if the key should be dropped, else None."""
    model, _topo = _split_key(key)

    if model.strip().lower() in PLACEHOLDER_NAMES:
        return ("PLACEHOLDER", "container-name parse fallback; unattributable")

    if model.startswith("_"):
        return ("SMOKE-TEST", "smoke-test convention; not a production recipe")

    if key in RAW_BASENAME_KEYS:
        return ("RAW-BASENAME", "pre-_resolve_catalog_key HF basename; ambiguous owner")

    return None


def _summarise(entry) -> str:
    """One-line description of what a key actually holds, so the drop plan
    shows what is being given up rather than just naming the key."""
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

    drops: dict[str, tuple[str, str]] = {}
    for key in data:
        verdict = _classify(key)
        if verdict:
            drops[key] = verdict

    print(f"[*] {args.ledger}: {len(data)} keys, {len(drops)} to drop, "
          f"{len(data) - len(drops)} to keep\n")

    if not drops:
        print("[*] nothing to do -- ledger is already clean.")
        return 0

    for cls in ("PLACEHOLDER", "SMOKE-TEST", "RAW-BASENAME"):
        members = [(k, r) for k, (c, r) in drops.items() if c == cls]
        if not members:
            continue
        print(f"  {cls}  ({members[0][1]})")
        for key, _reason in members:
            print(f"    - {key}")
            print(f"        {_summarise(data[key])}")
        print()

    cleaned = {k: v for k, v in data.items() if k not in drops}

    if not args.apply:
        print("[*] dry run -- nothing written. Re-run with --apply to commit.")
        return 0

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
    return 0


if __name__ == "__main__":
    sys.exit(main())
