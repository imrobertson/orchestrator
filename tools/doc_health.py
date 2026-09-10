#!/usr/bin/env python3
"""
tools/doc_health.py -- one command, one bill of health.

Runs every check the 2026-09-09 consolidation depends on, in order, and
grades the result. **Read-only by default.** Nothing is moved, deleted or
deployed unless you pass --finalize, and even then only the archive move
runs.

    python3 tools/doc_health.py              # inspect and grade
    python3 tools/doc_health.py --finalize   # also run the archive move

TOLERANCE IS THE POINT. Parts of this may already be done -- files moved
or deleted by hand, patches applied, a previous run half-completed. Every
check reports what it finds rather than assuming a starting state:

    DONE     already in its final position
    TODO     not done yet, and it is clear what to do
    MISSING  expected somewhere, found nowhere -- the one that matters
    STALE    present but should not be
    n/a      does not apply to this tree

A file that was deleted by hand without `git rm` reads as DONE, not as an
error. A file that was supposed to be merged somewhere and is simply gone
reads as MISSING, which is the failure this whole exercise exists to
prevent.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# --------------------------------------------------------------- expected

LIVING_DOCS = [
    "README.md", "docs/DOCMAP.md", "docs/INSTALL.md", "docs/USERMANUAL.md",
    "docs/MODELS.md", "docs/DIRECTION.md", "docs/WORKSTREAMS.md",
    "docs/TOMBSTONES.md", "docs/errata.yaml",
    "docs/SMOKE-TEST-PLAYBOOK.md", "docs/AB_TEST_USAGE.md",
]
LIVING_REFERENCE = [
    "docs/reference/community-sources.md",
    "docs/reference/flashinfer-autotune-internals.md",
]
LIVING_TOOLS = [
    "tools/check_doc_references.py", "tools/finalize_docs.py",
    "tools/run_verification.py", "tools/doc_health.py",
    "tools/verify/purpose.md", "tools/verify/_repo.py",
]
# name -> where its content should now live. MISSING if neither exists.
RETIRED = {
    "docs/ROADMAP.md": "docs/DIRECTION.md",
    "docs/ARCHITECTURE-MIGRATION-PLAN.md": "docs/DIRECTION.md",
    "docs/README.md": "README.md",
    "docs/UsageShortcut.md": "docs/USERMANUAL.md",
    "docs/REFERENCE-decode-speeds.md": "docs/MODELS.md",
    "docs/EUGR-REFERENCE-NOTES.md": "docs/reference/community-sources.md",
    "docs/EUGR-NOTES-UPDATE-2026-08-29.md": "docs/reference/community-sources.md",
    "docs/QUESTIONS.md": "docs/DIRECTION.md",
    "docs/TROUBLESHOOTING.md": "docs/errata.yaml",
    "docs/BACKLOG-dspark-sm120-image.md": "docs/WORKSTREAMS.md",
    "docs/BACKLOG-session-tracker-multi-model.md": "docs/WORKSTREAMS.md",
    "docs/SESSION-CLOSEOUT-2026-09-02-FINAL.md": "docs/WORKSTREAMS.md",
    "docs/SESSION-HANDOFF-2026-09-06.md": "docs/WORKSTREAMS.md",
    "docs/PHASE-2-PROMPTS.md": "docs/DIRECTION.md",
    "docs/PHASE-MODS-PROMPTS.md": "docs/DIRECTION.md",
    "docs/MA-REVIEW.md": "docs/errata.yaml",
    "docs/MB-REVIEW.md": "docs/errata.yaml",
    "docs/MC-REVIEW.md": "docs/errata.yaml",
    "docs/MD-REVIEW.md": "docs/errata.yaml",
    "docs/ME-REVIEW.md": "docs/errata.yaml",
    "tests/TESTING-MB.md": "docs/errata.yaml",
    "tests/TESTING-MC.md": "docs/errata.yaml",
}
DELETED = {"docs/SESSION-SEED.md":
           "actively misdirects; deleted rather than archived, deliberately"}
STAYS_PUT = {"docs/REFERENCE-control-surfaces.md":
             "expiry is 'when the interface spike lands'; WORKSTREAMS WS-0 item 8c"}
# things that should be GONE from the tree entirely
SHOULD_BE_ABSENT = {
    "models.yaml": "retired by TOMBSTONES #112",
    "recipes/eugr": "retired 2026-09-09; loader guards a missing dir",
    "tools/verify_pending_launch.py": "moved to tools/verify/",
    "tools/verify_recipe_equivalence.py": "moved to tools/verify/",
}
FIXED_RECIPES = [
    "recipes/local/qwen-3.8-27b.yaml",
    "recipes/local/qwen-3.8-27b-nvfp4-sqk2.yaml",
    "recipes/local/nemotron-3.5-lightning-nvfp4.yaml",
    "recipes/local/_nemotron-3.5-lightning-nvfp4-tools.yaml",
    "recipes/local/deepseek-v4-flash-0731-1M.yaml",
]

rows: list[tuple[str, str, str]] = []
notes: list[str] = []


def rec(state: str, item: str, note: str = "") -> None:
    rows.append((state, item, note))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".", type=Path)
    ap.add_argument("--finalize", action="store_true",
                    help="also run the archive move (tools/finalize_docs.py --write)")
    a = ap.parse_args()
    repo = a.repo.resolve()

    def ex(p: str) -> bool:
        return (repo / p).exists()

    print("=" * 74)
    print(f"DOC HEALTH  --  {repo}")
    print("=" * 74)

    # ------------------------------------------------------- 1 inventory
    rec("__HDR__", "[1] LIVING SET")
    for p in LIVING_DOCS + LIVING_REFERENCE + LIVING_TOOLS:
        rec("DONE" if ex(p) else "TODO", p,
            "" if ex(p) else "expected to exist and does not")

    rec("__HDR__", "[2] RETIRED SET  (archived, or already gone by hand)")
    for src, lands_in in RETIRED.items():
        name = Path(src).name
        archived = ex(f"docs/archive/{name}") or ex("docs/archive/docs-README.md") and name == "README.md"
        # The destination is checked FIRST and unconditionally. Being
        # archived proves the file MOVED, not that its content LANDED --
        # and the second is the thing that matters. An earlier version
        # short-circuited on `archived` and reported DONE for a file whose
        # destination had since been deleted, which is exactly the failure
        # that lost REFERENCE-dspark-shared-expert-fix.md.
        if not ex(lands_in):
            rec("MISSING", src,
                f"its content was supposed to be in {lands_in}, which does NOT exist"
                + (" (source is archived, so the content is only in the archive now)"
                   if archived else " -- and the source is gone too"))
        elif ex(src) and archived:
            rec("STALE", src, "present in BOTH docs/ and docs/archive/ -- remove the docs/ copy")
        elif ex(src):
            rec("TODO", src, f"still in place; content should be in {lands_in}")
        elif archived:
            rec("DONE", src, f"archived; content in {lands_in}")
        else:
            rec("DONE", src, f"gone by hand; its content is in {lands_in}")

    for src, why in DELETED.items():
        if ex(src):
            rec("TODO", src, f"still present; {why}")
        elif ex(f"docs/archive/{Path(src).name}"):
            rec("STALE", src, "archived, but this one was meant to be DELETED")
        else:
            rec("DONE", src, "deleted")

    for src, why in STAYS_PUT.items():
        if ex(src):
            rec("DONE", src, f"correctly left in place -- {why}")
        elif ex(f"docs/archive/{Path(src).name}"):
            rec("STALE", src, "archived, but it was meant to STAY -- moving it implies it is dead")
        else:
            rec("MISSING", src, "neither in docs/ nor archived")

    rec("__HDR__", "[3] SHOULD BE ABSENT")
    for p, why in SHOULD_BE_ABSENT.items():
        rec("DONE" if not ex(p) else "TODO", p, why)

    # -------------------------------------------------- 4 structural
    rec("__HDR__", "[4] STRUCTURAL CHECKS")
    t = repo / "docs/TOMBSTONES.md"
    if t.exists():
        nums = [int(m.group(1)) for m in re.finditer(r"(?m)^### (\d+)\.", t.read_text())]
        if nums and nums == list(range(max(nums), min(nums) - 1, -1)):
            rec("DONE", "TOMBSTONES numbering", f"{min(nums)}-{max(nums)} contiguous, descending")
        else:
            gaps = sorted(set(range(min(nums), max(nums) + 1)) - set(nums)) if nums else []
            rec("TODO", "TOMBSTONES numbering", f"gaps={gaps} dupes={sorted({x for x in nums if nums.count(x)>1})}")
    else:
        rec("MISSING", "TOMBSTONES numbering", "file absent")

    try:
        import yaml
        e = repo / "docs/errata.yaml"
        if e.exists():
            # Defensive: an empty or malformed errata.yaml used to crash this
            # whole check with an AttributeError. A health tool that dies on
            # bad input is useless exactly when something is wrong.
            try:
                d = yaml.safe_load(e.read_text()) or {}
            except Exception as exc:
                d = {}
                rec("TODO", "errata.yaml", f"does not parse: {exc}")
            ids = [r.get("id") for r in (d.get("rules") or []) if isinstance(r, dict)]
            need = {"E023", "E024", "E025"}
            if ids:
                rec("DONE" if need <= set(ids) else "TODO", "errata.yaml",
                    f"{len(ids)} rules; new={sorted(need & set(ids))} "
                    f"missing={sorted(need - set(ids))}")
            elif d:
                rec("TODO", "errata.yaml", "parsed but contains no rules")
        bad = []
        for r in FIXED_RECIPES:
            f = repo / r
            if not f.exists():
                bad.append(f"{Path(r).name}: absent"); continue
            try:
                doc = yaml.safe_load(f.read_text()) or {}
            except Exception as exc:
                bad.append(f"{Path(r).name}: does not parse -- {exc}"); continue
            if not isinstance(doc, dict):
                bad.append(f"{Path(r).name}: not a YAML mapping"); continue
            for tk, tv in (doc.get("topologies") or {}).items():
                args = tv.get("vllm_args", "") or ""
                if "--speculative-model" in args:
                    bad.append(f"{Path(r).name}::{tk}: E023 -- standalone --speculative-model")
                if tv.get("pp_size", 1) > 1 and "mtp" in args:
                    bad.append(f"{Path(r).name}::{tk}: E005 -- MTP with pp_size>1")
                if tk == "2_node" and "--distributed-executor-backend" not in args:
                    bad.append(f"{Path(r).name}::{tk}: E003 -- no ray backend")
                if tk == "2_node" and not doc.get("image"):
                    bad.append(f"{Path(r).name}::{tk}: E004 -- no image:, inherits default")
        rec("DONE" if not bad else "TODO", "fixed recipes vs errata",
            "clean" if not bad else "; ".join(bad))
    except ImportError:
        rec("n/a", "errata / recipe checks", "pyyaml not installed here")

    for state, item, note in rows:
        if state == "__HDR__":
            print(f"\n{item}")
            continue
        print(f"  {state:<8} {item:<52} {note}")

    # --------------------------------------------------- 5 delegated
    print("\n[5] REFERENCE CHECK")
    if ex("tools/check_doc_references.py"):
        r = subprocess.run([sys.executable, "tools/check_doc_references.py", "--repo", "."],
                           cwd=repo, capture_output=True, text=True)
        tail = [l for l in r.stdout.splitlines() if l.startswith(("DEFECTS", "WARNINGS",
                "UNRESOLVED", "RESULT"))]
        print("  " + "\n  ".join(tail) if tail else "  (no output)")
        ref_ok = "RESULT: no broken pointers." in r.stdout
    else:
        print("  tools/check_doc_references.py not present"); ref_ok = False

    print("\n[6] ARCHIVE MOVER")
    mv_ok = True
    if ex("tools/finalize_docs.py"):
        args = [sys.executable, "tools/finalize_docs.py"] + (["--write"] if a.finalize else [])
        r = subprocess.run(args, cwd=repo, capture_output=True, text=True)
        if "BLOCKED" in r.stdout:
            mv_ok = False
            print("  BLOCKED -- content has not landed; nothing moved:")
            for l in r.stdout.splitlines():
                if l.strip().startswith("x ") or "needs " in l:
                    print("   " + l.strip())
        else:
            last = [l for l in r.stdout.splitlines() if l.startswith(("[=]", "  moved", "  deleted", "  wrote"))]
            print("  " + "\n  ".join(last[-6:]) if last else "  (nothing to do)")
    else:
        print("  tools/finalize_docs.py not present"); mv_ok = False

    # ------------------------------------------------------- 7 verdict
    rows[:] = [r for r in rows if r[0] != "__HDR__"]
    counts = {k: sum(1 for s, _, _ in rows if s == k)
              for k in ("DONE", "TODO", "MISSING", "STALE", "n/a")}
    print("\n" + "=" * 74)
    print("BILL OF HEALTH")
    print("=" * 74)
    print(f"  DONE {counts['DONE']}   TODO {counts['TODO']}   "
          f"MISSING {counts['MISSING']}   STALE {counts['STALE']}   n/a {counts['n/a']}")

    if counts["MISSING"]:
        print("\n  MISSING is the one that matters. Each of these is a file that is")
        print("  gone AND whose destination is also absent -- content may be lost.")
        for s, i, n in rows:
            if s == "MISSING":
                print(f"    - {i}: {n}")

    if counts["STALE"]:
        print("\n  STALE -- present where it should not be:")
        for s, i, n in rows:
            if s == "STALE":
                print(f"    - {i}: {n}")

    todo = [(i, n) for s, i, n in rows if s == "TODO"]
    if todo:
        print(f"\n  TODO ({len(todo)}):")
        for i, n in todo:
            print(f"    - {i}: {n}")

    print("\n  NOT CHECKED HERE, and not checkable without the cluster:")
    print("    - whether any of the four fixed recipes actually deploys")
    print("    - whether the prefetcher now caches the four drafters")
    print("    Run tools/run_verification.py on maestro for both.")

    # Three verdicts, not two. An earlier version graded a tree with 22
    # outstanding TODOs as HEALTHY because nothing was lost -- true, and
    # useless. Work remaining and work lost are different problems and want
    # different words.
    lost = counts["MISSING"] or counts["STALE"] or not ref_ok or not mv_ok
    if lost:
        verdict, code = "NEEDS ATTENTION -- something is lost or misplaced, see above", 2
    elif counts["TODO"]:
        verdict, code = (f"INCOMPLETE -- {counts['TODO']} step(s) remaining, nothing lost. "
                         f"Work the TODO list above. NOTE: only use --finalize on a "
                         f"tree where docs/archive/ has NOT been populated by hand -- "
                         f"the mover rewrites docs/archive/README.md from what IT "
                         f"moved, so on a partly-hand-moved tree it would replace a "
                         f"good index with a near-empty one.", 1)
    else:
        verdict, code = "HEALTHY -- documentation set is consistent", 0
    print(f"\n  VERDICT: {verdict}")
    print("\n  Exit codes: 0 healthy, 1 incomplete, 2 needs attention.")
    return code


if __name__ == "__main__":
    sys.exit(main())
