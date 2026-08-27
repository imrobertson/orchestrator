#!/usr/bin/env python3
"""
One-off correction for a model_ledger.json entry whose `lifetime` totals
were silently discarded by the SessionTracker restart bug (fixed in
dgx-orchestrator.py -- see SessionTracker._load_last_seen_raw()).

This is for manual, one-time use to repair a ledger entry using values you
observed directly from vLLM's own /metrics endpoint. It is NOT meant to be
run routinely -- with the durability fix in place, this class of drift
shouldn't recur.

Usage:
    python3 correct_ledger.py \\
        --ledger /path/to/model_ledger.json \\
        --model deepseek-v4-flash-0731-1M --topo 2node \\
        --prompt-tokens 28966407 --gen-tokens 730425 \\
        [--draft-tokens 0] [--accepted-tokens 0] \\
        [--dry-run]

Only touches the "lifetime" and "last_seen_raw" fields of the matched
key -- "cached"/"compiled"/"downloaded" and every other key are left
untouched. Writes a timestamped .bak of the whole file before any write.

Sets (does not add to) lifetime.in/out/draft/accepted to the values you
provide, since a single-launch key's whole lifetime history is the same
thing as its current live cumulative counters -- the existing (wrong,
small) lifetime numbers are a SUBSET of what you're providing here, not
something separate to add on top of. If the key has had more than one
launch historically, don't use this script as-is; the "set" semantics
only make sense for a single continuous engine lifetime.

Refuses to overwrite with SMALLER numbers than what's already recorded,
as a sanity check against transposed arguments or stale metrics -- pass
--force to override if you're certain.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ledger", required=True, type=Path, help="Path to model_ledger.json")
    ap.add_argument("--model", required=True, help="Model key, e.g. deepseek-v4-flash-0731-1M")
    ap.add_argument("--topo", required=True, help="Topology key, e.g. 2node")
    ap.add_argument("--prompt-tokens", required=True, type=float, help="Live vllm:prompt_tokens_total value")
    ap.add_argument("--gen-tokens", required=True, type=float, help="Live vllm:generation_tokens_total value")
    ap.add_argument("--draft-tokens", type=float, default=0.0, help="Live draft token counter, if applicable (default 0)")
    ap.add_argument("--accepted-tokens", type=float, default=0.0, help="Live accepted token counter, if applicable (default 0)")
    ap.add_argument("--dry-run", action="store_true", help="Show what would change without writing anything")
    ap.add_argument("--force", action="store_true", help="Allow overwriting with smaller values than currently recorded")
    args = ap.parse_args()

    if not args.ledger.is_file():
        print(f"ERROR: ledger file not found: {args.ledger}", file=sys.stderr)
        return 1

    try:
        data = json.loads(args.ledger.read_text())
    except Exception as exc:
        print(f"ERROR: could not parse {args.ledger} as JSON: {exc}", file=sys.stderr)
        return 1

    key = f"{args.model}::{args.topo}"
    entry = data.get(key)
    if not isinstance(entry, dict):
        print(f"ERROR: key {key!r} not found in ledger. Available keys:", file=sys.stderr)
        for k in data.keys():
            print(f"    {k}", file=sys.stderr)
        return 1

    current_lifetime = entry.get("lifetime", {"in": 0, "out": 0, "draft": 0, "accepted": 0})
    print(f"Key: {key}")
    print(f"Current lifetime: {current_lifetime}")
    print(f"Current last_seen_raw: {entry.get('last_seen_raw', '(none)')}")

    new_lifetime = {
        "in": int(args.prompt_tokens),
        "out": int(args.gen_tokens),
        "draft": int(args.draft_tokens),
        "accepted": int(args.accepted_tokens),
    }

    if not args.force:
        for field in ("in", "out", "draft", "accepted"):
            if new_lifetime[field] < current_lifetime.get(field, 0):
                print(
                    f"\nERROR: new lifetime.{field}={new_lifetime[field]} is LESS than the "
                    f"currently recorded {current_lifetime.get(field, 0)}. Refusing to overwrite "
                    f"-- this usually means stale/transposed values. Pass --force if this is "
                    f"genuinely intended.",
                    file=sys.stderr,
                )
                return 1

    new_raw = {"p": args.prompt_tokens, "g": args.gen_tokens, "d": args.draft_tokens, "a": args.accepted_tokens}

    print(f"\nNew lifetime:     {new_lifetime}")
    print(f"New last_seen_raw: {new_raw}")

    if args.dry_run:
        print("\n--dry-run: no changes written.")
        return 0

    backup_path = args.ledger.with_suffix(f".json.bak.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    shutil.copy2(args.ledger, backup_path)
    print(f"\nBackup written to: {backup_path}")

    entry["lifetime"] = new_lifetime
    entry["last_seen_raw"] = new_raw
    data[key] = entry

    args.ledger.write_text(json.dumps(data, indent=2))
    print(f"Wrote corrected ledger to: {args.ledger}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
