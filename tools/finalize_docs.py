#!/usr/bin/env python3
"""
tools/finalize_docs.py -- move the retired documents to their resting places.

Manifest-driven, dry run by default, `git mv` where available so history
follows the file.

WHY THIS REFUSES TO RUN RATHER THAN JUST MOVING FILES
-----------------------------------------------------
Archiving a document whose content has not landed somewhere else is how
`REFERENCE-dspark-shared-expert-fix.md` was lost -- cited by two files as
"saved as", present in none, and nobody noticed until a line-by-line comb
eight days later. So each entry carries a PRECONDITION, and the script
exits without touching anything if one is unmet. A blocked archive is a
reminder; a completed one that lost something is a silent hole.

The four dispositions:

  archive   -> docs/archive/, once its precondition holds
  reference -> docs/reference/, still live, just relocated
  delete    -> gone. Exactly one file qualifies; see its reason.
  skip      -> deliberately left in docs/, with a reason recorded in the
               index so its presence is not mistaken for an oversight

USAGE
    python3 tools/finalize_docs.py                 # dry run
    python3 tools/finalize_docs.py --write
    python3 tools/finalize_docs.py --write --no-check   # skip the ref check
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

# (source, disposition, one-line reason, precondition)
# precondition: a repo-relative path that must EXIST, or None.
MANIFEST: list[tuple[str, str, str, str | None]] = [
    # --- superseded by DIRECTION.md ---
    ("docs/ROADMAP.md", "archive",
     "Direction moved to DIRECTION.md, backlog to WORKSTREAMS.md.", "docs/DIRECTION.md"),
    ("docs/ARCHITECTURE-MIGRATION-PLAN.md", "archive",
     "Phase status moved to DIRECTION.md; Phase 3's pool constraint preserved there.",
     "docs/DIRECTION.md"),

    # --- merged into a living document ---
    # Renamed on the way in: docs/archive/README.md is the archive INDEX,
    # and GitHub renders it when browsing the directory. Archiving this
    # file under its own name would have the index silently overwrite it --
    # caught in a dry run, 2026-09-09.
    ("docs/README.md", "archive:docs-README.md",
     "Diverged near-duplicate of the root README.md; its unique sections were folded in. "
     "Renamed on archiving so it does not collide with this index.",
     "README.md"),
    ("docs/UsageShortcut.md", "archive",
     "Merged into USERMANUAL.md. It was the NEWER of the two -- the merge ran shortcut -> manual.",
     "docs/USERMANUAL.md"),
    ("docs/REFERENCE-decode-speeds.md", "archive",
     "Merged into MODELS.md, which now owns measured throughput.", "docs/MODELS.md"),
    ("docs/EUGR-REFERENCE-NOTES.md", "archive",
     "Merged into reference/community-sources.md, including the two REPLACE blocks never applied.",
     "docs/reference/community-sources.md"),
    ("docs/EUGR-NOTES-UPDATE-2026-08-29.md", "archive",
     "Its own merge instructions were finally applied; content is in reference/community-sources.md.",
     "docs/reference/community-sources.md"),
    ("docs/QUESTIONS.md", "archive",
     "Contained no questions -- four design traps, now decisions of record in DIRECTION.md.",
     "docs/DIRECTION.md"),
    ("docs/TROUBLESHOOTING.md", "archive",
     "Dissolved: 10 incidents already covered, #9/#12 became TOMBSTONES #147/#148, "
     "Incident #2 became errata E025, guidance to USERMANUAL.md, figures to MODELS.md. "
     "NOTE its Incident #N series is separate from TOMBSTONES' numbering.",
     "docs/errata.yaml"),
    ("docs/BACKLOG-dspark-sm120-image.md", "archive",
     "Five open items are in WORKSTREAMS WS-9; link list in reference/community-sources.md; "
     "item 6 (catalog trim) verified done.", "docs/reference/community-sources.md"),

    # --- session scaffolding ---
    ("docs/SESSION-CLOSEOUT-2026-09-02-FINAL.md", "archive",
     "Ported to WS-5. Half the file was new-chat scaffolding.", "docs/WORKSTREAMS.md"),
    ("docs/SESSION-HANDOFF-2026-09-06.md", "archive",
     "Sec 7 doc edits applied; Sec 4 items 5-8 and Sec 8 transcribed to WORKSTREAMS.",
     "docs/WORKSTREAMS.md"),
    ("docs/BACKLOG-session-tracker-multi-model.md", "archive",
     "Transcribed into WORKSTREAMS.", "docs/WORKSTREAMS.md"),

    # --- completed phases ---
    ("docs/PHASE-2-PROMPTS.md", "archive", "Phase 2 complete.", "docs/DIRECTION.md"),
    ("docs/PHASE-MODS-PROMPTS.md", "archive",
     "Mods phase complete; append MA/MB/MC results before archiving if you want them here.",
     "docs/DIRECTION.md"),
    ("docs/MA-REVIEW.md", "archive", "Per-task review, mods phase.", "docs/errata.yaml"),
    ("docs/MB-REVIEW.md", "archive", "Per-task review, mods phase.", "docs/errata.yaml"),
    ("docs/MC-REVIEW.md", "archive", "Per-task review, mods phase.", "docs/errata.yaml"),
    ("docs/MD-REVIEW.md", "archive",
     "Per-task review. Its Contradictions section is errata material -- confirm it landed first.",
     "docs/errata.yaml"),
    ("docs/ME-REVIEW.md", "archive", "Per-task review, mods phase.", "docs/errata.yaml"),
    ("tests/TESTING-MB.md", "archive", "Per-task test notes, mods phase.", "docs/errata.yaml"),
    ("tests/TESTING-MC.md", "archive", "Per-task test notes, mods phase.", "docs/errata.yaml"),

    # --- relocate, still live ---
    ("docs/REFERENCE-flashinfer-autotune-internals.md", "reference",
     "Durable third-party internals. Still live, just moved out of the operator's line of sight.",
     None),

    # --- delete ---
    ("docs/SESSION-SEED.md", "delete",
     "The only file deleted rather than archived. Not merely stale -- it cites TOMBSTONES #76 as "
     "current (now #148), recommends an image confirmed x86_64-only, names a recipe that no longer "
     "exists, and frames DSpark as broken when it runs at 42-44 tok/s. As onboarding it actively "
     "misdirects, and archiving it leaves that trap findable.", None),

    # --- skip, deliberately ---
    ("docs/REFERENCE-control-surfaces.md", "skip",
     "NOT retired. Its own expiry condition is 'when the interface spike lands', which has not "
     "happened. Deliberately absent from the doc map so it can be deleted later without leaving a "
     "dangling reference. Moving it to archive would imply it is dead. Tracked in WORKSTREAMS WS-0 "
     "item 8c so the condition is known outside the file itself.", None),
]

ARCHIVE = "docs/archive"
REFERENCE = "docs/reference"


def git_available(repo: Path) -> bool:
    try:
        return subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                              cwd=repo, capture_output=True, text=True).returncode == 0
    except FileNotFoundError:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".", type=Path)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--no-check", action="store_true",
                    help="skip the reference check afterwards (not recommended)")
    a = ap.parse_args()
    repo = a.repo.resolve()
    use_git = git_available(repo)

    # Some of this may already have been done by hand -- files deleted or
    # moved without `git rm` / `git mv`. That is fine and expected; the
    # manifest reports them as "already gone" rather than erroring. But a
    # dirty tree makes a `git mv` failure hard to read, so say so up front.
    if use_git:
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                               capture_output=True, text=True).stdout.strip()
        if dirty:
            n = len(dirty.splitlines())
            print(f"[!] Working tree has {n} uncommitted change(s).")
            print("    Not a blocker -- `git mv` still works and anything already")
            print("    removed by hand is reported as 'already gone'. But commit or")
            print("    stash first if you want this move to be a reviewable diff")
            print("    rather than mixed in with whatever else is pending.\n")

    plan, blocked, absent, skipped = [], [], [], []

    for src, disp, reason, precond in MANIFEST:
        s = repo / src
        if not s.exists():
            absent.append((src, disp))
            continue
        if disp == "skip":
            skipped.append((src, reason))
            continue
        if precond and not (repo / precond).exists():
            blocked.append((src, precond))
            continue
        rename_to = None
        if disp.startswith("archive:"):
            disp, rename_to = "archive", disp.split(":", 1)[1]
        dest_dir = ARCHIVE if disp == "archive" else REFERENCE
        if disp == "delete":
            plan.append((src, None, reason, disp))
        else:
            name = rename_to or Path(src).name
            if disp == "reference" and name.startswith("REFERENCE-"):
                name = name[len("REFERENCE-"):]
            plan.append((src, f"{dest_dir}/{name}", reason, disp))

    index_path = f"{ARCHIVE}/README.md"
    collide = [p for p in plan if p[1] == index_path]
    if collide:
        print(f"[-] A planned destination collides with the archive index ({index_path}):")
        for c in collide:
            print(f"      {c[0]}")
        print("    Give it an explicit archive:<newname> disposition. Nothing written.")
        return 1

    print("=" * 72)
    print(f"FINALIZE DOCS -- {'WRITE' if a.write else 'DRY RUN'}   (git mv: {use_git})")
    print("=" * 72)

    if blocked:
        print("\nBLOCKED -- content has not landed anywhere; refusing to archive:")
        for src, precond in blocked:
            print(f"  x {src}\n      needs {precond} to exist first")
        print("\nNothing has been moved. Land the content, then re-run.")
        print("This guard exists because REFERENCE-dspark-shared-expert-fix.md")
        print("was archived-by-omission and lost; see TOMBSTONES #144's neighbours.")
        return 1

    for src, dest, reason, disp in plan:
        arrow = "DELETE" if dest is None else f"-> {dest}"
        print(f"\n  {src}\n      {arrow}\n      {reason}")
    for src, reason in skipped:
        print(f"\n  {src}\n      SKIP (stays in docs/)\n      {reason}")
    if absent:
        print("\nAlready gone or never present:")
        for src, disp in absent:
            print(f"  - {src} ({disp})")

    if not a.write:
        print(f"\n[=] Dry run. {len(plan)} action(s), {len(skipped)} skipped. Re-run with --write.")
        return 0

    (repo / ARCHIVE).mkdir(parents=True, exist_ok=True)
    (repo / REFERENCE).mkdir(parents=True, exist_ok=True)

    for src, dest, reason, disp in plan:
        s, d = repo / src, (repo / dest if dest else None)
        if disp == "delete":
            if use_git:
                subprocess.run(["git", "rm", "-q", src], cwd=repo, check=False)
            if s.exists():
                s.unlink()
            print(f"  deleted {src}")
        else:
            if use_git:
                subprocess.run(["git", "mv", src, dest], cwd=repo, check=False)
            if s.exists():
                shutil.move(str(s), str(d))
            print(f"  moved   {src} -> {dest}")

    index = [
        "# docs/archive/",
        "",
        "Retired documents. **Nothing here is current.** Every one was",
        "superseded, merged, or completed, and its content lives somewhere in",
        "the living set -- the line under each name says where.",
        "",
        "Kept rather than deleted because provenance is repeatedly what made a",
        "finding recoverable: several corrections in the 2026-09-09 pass were",
        "only possible because a superseded document still said what it used to",
        "say. Git history is not a substitute -- nobody greps deleted files.",
        "",
        "Retired 2026-09-09 unless noted.",
        "",
    ]
    for src, dest, reason, disp in sorted(plan, key=lambda r: r[0]):
        if disp == "archive":
            index.append(f"### `{Path(src).name}`\n\n{reason}\n")
    index += ["## Deleted, not archived\n"]
    for src, dest, reason, disp in plan:
        if disp == "delete":
            index.append(f"### `{Path(src).name}`\n\n{reason}\n")
    index += ["## Deliberately left in `docs/`\n"]
    for src, reason in skipped:
        index.append(f"### `{Path(src).name}`\n\n{reason}\n")

    idx = repo / ARCHIVE / "README.md"
    # The index is generated from what THIS RUN moved. On a tree where the
    # archive was populated by hand, that set is a fraction of what is
    # actually there, and writing it would replace a complete index with a
    # near-empty one -- silently, since both are called README.md. Refuse.
    if idx.exists():
        print(f"  SKIPPED {ARCHIVE}/README.md -- an index already exists.")
        print(f"          This run moved {sum(1 for r in plan if r[3] == 'archive')} file(s); "
              f"the archive holds "
              f"{len(list((repo / ARCHIVE).glob('*.md'))) - 1}.")
        print("          Overwriting would describe only what this run touched.")
        print("          Add the entries by hand, or delete the index and re-run.")
    else:
        idx.write_text("\n".join(index), encoding="utf-8")
        print(f"  wrote   {ARCHIVE}/README.md")

    if a.no_check:
        print("\n[!] Reference check skipped (--no-check).")
        return 0

    print("\n" + "=" * 72)
    print("Running the reference check against the moved tree...")
    print("=" * 72)
    r = subprocess.run([sys.executable, "tools/check_doc_references.py", "--repo", "."],
                       cwd=repo)
    if r.returncode != 0:
        print("\n[!] The reference check reported defects. The move completed;")
        print("    fix the links before committing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
