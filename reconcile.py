#!/usr/bin/env python3
"""
reconcile.py -- fix the placement divergences in the ACTUAL tree.

`place_changes.py` assumed an untouched repo. That is not the situation:
most files were dropped in as we went, `docs/archive/` already exists and
is populated, and the move was done by hand. This script fixes only the
specific divergences, and is safe to run against a tree where some or all
of them are already correct.

    python3 reconcile.py --repo ~/docker/orchestrator                  # dry run
    python3 reconcile.py --repo ~/docker/orchestrator --write
    python3 reconcile.py --repo . --write --from ~/Downloads/<unpacked>

`--from` is optional and only needed to place the four `tools/*.py`
scripts, which are not yet in the tree.

TWO OF THESE ARE ACTUAL MISTAKES, not leftovers:

  SMOKE-TEST-PLAYBOOK.md was archived. It is a LIVING document -- it is
  in README.md's doc map, DIRECTION.md's living table, and DOCMAP.md. It
  is not superseded by anything and nothing absorbed its content.

  REFERENCE-control-surfaces.md was archived. It was explicitly meant to
  stay: its retirement condition is "when the interface spike lands",
  which has not happened, and archiving it says it is dead when it is
  not. This is the exact case WORKSTREAMS WS-0 item 8c was written for.

Both are recoverable because archiving preserved them. That is the
argument for archiving over deleting, demonstrated within a day of making
it.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

# (kind, source, destination_or_None, why)
FIXES = [
    ("restore", "docs/archive/SMOKE-TEST-PLAYBOOK.md", "docs/SMOKE-TEST-PLAYBOOK.md",
     "LIVING document, archived by mistake. Listed in README.md's doc map, "
     "DIRECTION.md's living table and DOCMAP.md; nothing absorbed it."),

    ("restore", "docs/archive/REFERENCE-control-surfaces.md", "docs/REFERENCE-control-surfaces.md",
     "Meant to STAY. Its expiry is 'when the interface spike lands', which has "
     "not happened. Archiving implies dead; it is not. WORKSTREAMS WS-0 item 8c."),

    ("move", "docs/archive/REFERENCE-flashinfer-autotune-internals.md",
     "docs/reference/flashinfer-autotune-internals.md",
     "Live reference material, not archive. Belongs in docs/reference/ alongside "
     "community-sources.md, with the REFERENCE- prefix dropped."),

    ("move", "docs/community-sources.md", "docs/reference/community-sources.md",
     "Landed at docs/ root; docs/reference/ was never created. The subdirectory "
     "is the point -- it keeps third-party notes out of the operator's line of sight."),

    ("delete", "docs/archive/SESSION-SEED.md", None,
     "The one file meant to be DELETED rather than archived. It cites TOMBSTONES "
     "#76 as current (now #148), recommends an image confirmed x86_64-only, names "
     "a recipe that no longer exists, and frames DSpark as broken when it runs at "
     "42-44 tok/s. Archiving leaves that trap findable."),

    ("archive", "docs/QUESTIONS.md", "docs/archive/QUESTIONS.md",
     "Its four design traps are now decisions of record in DIRECTION.md."),

    ("archive", "docs/ROADMAP.md", "docs/archive/ROADMAP.md",
     "85KB, superseded by DIRECTION.md (direction) and WORKSTREAMS.md (backlog)."),
]

TOOLS = [
    ("tools/doc_health.py", "tools/doc_health.py"),
    ("tools/check_doc_references.py", "tools/check_doc_references.py"),
    ("tools/finalize_docs.py", "tools/finalize_docs.py"),
    ("run_verification.py", "tools/run_verification.py"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".", type=Path)
    ap.add_argument("--from", dest="src", type=Path, default=None)
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    repo = a.repo.expanduser().resolve()
    src = a.src.expanduser().resolve() if a.src else None

    if not (repo / "dgx-orchestrator.py").exists():
        print(f"[-] {repo} does not look like the orchestrator repo.")
        return 2

    print("=" * 74)
    print(f"RECONCILE  --  {'WRITE' if a.write else 'DRY RUN'}   {repo}")
    print("=" * 74)

    todo, done = [], []
    for kind, s_rel, d_rel, why in FIXES:
        s = repo / s_rel
        d = repo / d_rel if d_rel else None
        if kind == "delete":
            (done if not s.exists() else todo).append((kind, s_rel, d_rel, why))
        elif d and d.exists() and not s.exists():
            done.append((kind, s_rel, d_rel, why))
        elif s.exists():
            todo.append((kind, s_rel, d_rel, why))
        else:
            done.append(("absent", s_rel, d_rel, why + "  [neither path exists -- check this]"))

    print(f"\nTO FIX ({len(todo)}):")
    for kind, s_rel, d_rel, why in todo:
        print(f"\n  [{kind.upper()}] {s_rel}")
        if d_rel:
            print(f"      -> {d_rel}")
        print(f"      {why}")
    if not todo:
        print("  nothing -- placement is already correct")

    if done:
        print(f"\nALREADY CORRECT ({len(done)}):")
        for kind, s_rel, d_rel, why in done:
            note = " <-- INVESTIGATE" if kind == "absent" else ""
            print(f"  {d_rel or s_rel}{note}")

    tool_todo = []
    if src:
        for s_rel, d_rel in TOOLS:
            if not (src / s_rel).is_file():
                continue
            d = repo / d_rel
            if not d.exists() or d.read_bytes() != (src / s_rel).read_bytes():
                tool_todo.append((s_rel, d_rel))
        print(f"\nTOOLS TO PLACE ({len(tool_todo)}):")
        for _, d_rel in tool_todo:
            print(f"  {d_rel}")
        if not tool_todo:
            print("  none -- all four already present and identical")
    else:
        missing = [d for _, d in TOOLS if not (repo / d).exists()]
        if missing:
            print(f"\nTOOLS MISSING ({len(missing)}) -- pass --from <download dir> to place them:")
            for d in missing:
                print(f"  {d}")

    if not a.write:
        print(f"\n[=] Dry run. {len(todo)} placement fix(es), "
              f"{len(tool_todo)} tool(s). Re-run with --write.")
        return 0

    for kind, s_rel, d_rel, why in todo:
        s = repo / s_rel
        if kind == "delete":
            s.unlink()
            print(f"  deleted {s_rel}")
        else:
            d = repo / d_rel
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(s), str(d))
            print(f"  moved   {s_rel} -> {d_rel}")

    for s_rel, d_rel in tool_todo:
        d = repo / d_rel
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / s_rel, d)
        print(f"  placed  {d_rel}")

    print("\n  NOTE: these were plain filesystem moves. Tell git about them:")
    print("    git add -A docs/ tools/")
    print("\n  Then:  python3 tools/doc_health.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
