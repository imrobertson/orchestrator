#!/usr/bin/env python3
"""
tools/file_inventory.py -- every tracked file must have a documented purpose.

    python3 tools/file_inventory.py            # report
    python3 tools/file_inventory.py --strict   # exit 1 if anything is orphaned

A file is ACCOUNTED FOR if any living document names it, or the
orchestrator imports it, or it matches a convention below that makes its
purpose self-evident. Anything else is ORPHANED: it may be perfectly good
code, but nobody reading the docs would know it exists or why.

WHY THIS EXISTS. The 2026-09-09/10 consolidation found `models.yaml`
handling in a file nobody had mapped, a verification harness committed to
a path where it could not run, and 27 tracked files no document mentions.
None of those were caught by reading. All of them were caught by diffing
one list against another. That is what this does, on demand, in a second.

ORPHANED IS NOT "DELETE". It is one of three things, and the report says
which it cannot tell apart:
  - a DOC GAP: the file is fine, the doc map should name it
  - DEAD: its inputs or callers are gone
  - UNKNOWN: only the author knows
Deciding is a human job. Finding them is not.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# Conventions that document themselves. Keep this list SHORT and justified;
# every entry is a hole in the check.
SELF_EVIDENT = [
    (re.compile(r"^tests/test_[a-z_]+\.py$"),
     "unit test, named for its module — pytest convention"),
    (re.compile(r"^recipes/local/.*\.yaml$"),
     "model recipe — the catalog is docs/MODELS.md, per-file docs are the headers"),
    (re.compile(r"^common/[a-z_]+\.py$"),
     "orchestrator package module — README's architecture section"),
    (re.compile(r"^\.(gitignore|dockerignore)$|^\.secrets\.example$"),
     "repo plumbing"),
    (re.compile(r"^tools/verify/"),
     "verification harness — tools/verify/purpose.md"),
    (re.compile(r"^docs/archive/"),
     "retired — docs/archive/README.md"),
]

# Any directory-level README counts as documentation of what is in that
# directory -- that is what they are for. The first version of this list
# enumerated them by hand and missed tests/README.md, so the six files that
# file documents were still reported as orphans. Enumerate by pattern.
DOC_GLOBS = ["README.md", "*/README.md", "*/*/README.md",
             "docs/*.md", "docs/reference/*.md", "docs/errata.yaml",
             "tools/verify/purpose.md"]
CODE_GLOBS = ["dgx-orchestrator.py", "dgx-config", "common/*.py",
              "benchmark.py", "cache_cluster_assets.py"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".", type=Path)
    ap.add_argument("--strict", action="store_true")
    a = ap.parse_args()
    repo = a.repo.resolve()

    try:
        tracked = subprocess.run(["git", "ls-files"], cwd=repo, capture_output=True,
                                 text=True, check=True).stdout.split()
    except Exception as exc:
        print(f"[-] git ls-files failed: {exc}")
        return 2

    docs, code = "", ""
    for g in DOC_GLOBS:
        for p in repo.glob(g):
            if p.is_file():
                docs += p.read_text(errors="ignore")
    for g in CODE_GLOBS:
        for p in repo.glob(g):
            if p.is_file():
                code += p.read_text(errors="ignore")

    orphans, evident, documented = [], 0, 0
    for rel in sorted(tracked):
        name = Path(rel).name
        if any(rx.match(rel) for rx, _ in SELF_EVIDENT):
            evident += 1
            continue
        if name in docs or rel in docs or name in code:
            documented += 1
            continue
        orphans.append(rel)

    print("=" * 72)
    print("FILE INVENTORY -- every tracked file should have a documented purpose")
    print("=" * 72)
    print(f"\n  {len(tracked)} tracked   {documented} named in a doc or the code   "
          f"{evident} self-evident by convention   {len(orphans)} ORPHANED")

    if orphans:
        print("\nORPHANED -- nothing names these:\n")
        for o in orphans:
            print(f"  ? {o}")
        print("\n  Each is a DOC GAP (add it to the doc map), DEAD (its inputs or")
        print("  callers are gone), or UNKNOWN (ask the author). This tool cannot")
        print("  tell those apart and should not try. Resolve, don't suppress --")
        print("  adding entries to SELF_EVIDENT to silence it defeats the point.")
    else:
        print("\n  No orphans. Every tracked file is accounted for.")

    return 1 if (orphans and a.strict) else 0


if __name__ == "__main__":
    sys.exit(main())
