#!/usr/bin/env python3
"""
tools/check_doc_references.py -- do the documents point at things that exist?

Built 2026-09-09 after a consolidation pass found three files cited in the
present tense that had never existed (`REFERENCE-dspark-shared-expert-fix.md`,
`BACKLOG-generalize-metest.md`, `stage_specs_refactor_prompt.md`), two live
"go read this" links in INSTALL.md to documents about to be archived, and a
translator whose default input directory had been deleted months earlier.
Every one of those was found by a human reading carefully. None of them
needed to be.

THE DISTINCTION THIS TOOL EXISTS TO MAKE
----------------------------------------
A naive "no references to archived files" rule flags the wrong things and
gets switched off within a week. DIRECTION.md's disposition table MUST name
every file it retires -- that is its job. community-sources.md MUST name
the documents it absorbed -- that is provenance. TOMBSTONES.md cites
deleted recipes constantly and is correct to: it is an append-only history.

So:

  A MARKDOWN LINK  [text](path)  is a POINTER. A reader clicks it and
  expects to arrive somewhere. If it does not resolve: DEFECT.

  A CODE SPAN  `FILE.md`  is a NAME. Prose can legitimately name a file
  that is archived, deleted, or never existed, as long as the sentence
  around it is honest. Reported as INFO, never as a failure.

The one exception is a link that resolves INTO docs/archive/ from a living
document -- that resolves fine and is still usually wrong, so it is a
WARNING rather than a defect.

USAGE
    python3 tools/check_doc_references.py              # advisory, exit 0
    python3 tools/check_doc_references.py --strict     # exit 1 on defects
    python3 tools/check_doc_references.py --info       # also list INFO lines

Advisory by default, deliberately. Turning on a blocking check before you
know its false-positive rate is how people learn to skip it.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Living documents. Anything not listed here is not walked -- archive and
# reference material are allowed to point wherever they historically did.
LIVING = [
    "README.md",
    "docs/DOCMAP.md",
    "docs/INSTALL.md",
    "docs/USERMANUAL.md",
    "docs/MODELS.md",
    "docs/DIRECTION.md",
    "docs/WORKSTREAMS.md",
    "docs/TOMBSTONES.md",
    "docs/SMOKE-TEST-PLAYBOOK.md",
    "docs/AB_TEST_USAGE.md",
    "docs/reference/community-sources.md",
    "docs/reference/flashinfer-autotune-internals.md",
    "tools/verify/purpose.md",
]

# Code-span tokens that look like repo paths but are not, and should not be
# resolved. Kept short on purpose -- a long exclusion list is a checker
# quietly going blind.
NOT_REPO_PATHS = re.compile(
    r"""^(
        .*\.(?:py|md|yaml|yml|json|csv|txt|sh|html)$  # placeholder, narrowed below
    )$""",
    re.X,
)

# Files referenced in prose that live outside this repo (vLLM internals,
# upstream sources). Naming them is correct; resolving them is not.
FOREIGN = re.compile(
    # vLLM internals, named by source file in tombstones and errata.
    r"^(vllm/.*|flashinfer/.*|v1/.*|serve\.py|envs\.py|loggers\.py|arg_utils\.py|api_server\.py|"
    r"parallel_state\.py|autotuner\.py|runners\.py|shm_broadcast\.py|"
    r"gemma4\.py|dspark\.py|multiproc_executor\.py|llm_base_proposer\.py|"
    # eugr's own tooling and mod layout.
    r"run-recipe\.py|run\.sh|launch-cluster\.sh|autodiscover\.sh|"
    r"build-and-copy\.sh|config\.json|\.env\.example|expected_commands\.sh|"
    r"tests/expected_commands\.sh|tests/test_recipes\.sh|recipes/[0-9]x-spark-cluster/.*|"
    # Documents in OTHER people's repositories, cited by community-sources.
    r"docs/BUILD\.md|docs/TUNING\.md|OFFICIAL_MAIN_PORT_PLAN\.md|"
    r"RUNTIME-BAKEOFF-2026-07-29\.md)$"
)

LINK_RE = re.compile(r"(?<!!)\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
SPAN_RE = re.compile(r"`([A-Za-z0-9_./\-]+\.(?:md|py|yaml|yml|json|csv|sh|html))`")


def classify(repo: Path, source: Path, target: str) -> tuple[str, str] | None:
    """Return (severity, note) or None if the target is fine."""
    if target.startswith(("http://", "https://", "mailto:", "#")):
        return None
    path = target.split("#", 1)[0]
    if not path:
        return None
    resolved = (source.parent / path).resolve()
    try:
        rel = resolved.relative_to(repo.resolve())
    except ValueError:
        return ("DEFECT", f"resolves outside the repo -> {resolved}")
    if not resolved.exists():
        return ("DEFECT", f"does not exist -> {rel}")
    if "archive" in rel.parts:
        return ("WARN", f"a living document links INTO the archive -> {rel}")
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".", type=Path)
    ap.add_argument("--strict", action="store_true", help="exit 1 if any DEFECT")
    ap.add_argument("--info", action="store_true", help="also print INFO lines")
    a = ap.parse_args()
    repo = a.repo.resolve()

    defects, warns, infos, missing_docs = [], [], [], []
    phantoms: dict[str, list[str]] = {}

    for relname in LIVING:
        src = repo / relname
        if not src.is_file():
            missing_docs.append(relname)
            continue
        text = src.read_text(encoding="utf-8")

        for label, target in LINK_RE.findall(text):
            verdict = classify(repo, src, target)
            if verdict:
                sev, note = verdict
                (defects if sev == "DEFECT" else warns).append(
                    f"{relname}: [{label}]({target}) -- {note}")

        for token in set(SPAN_RE.findall(text)):
            if FOREIGN.match(token):
                continue
            # A leading "-" means the regex clipped a filename out of the
            # middle of prose ("...-mtp2.yaml"), not a real path. A leading
            # "/" is a system path, not ours.
            if token.startswith(("-", "/")):
                continue
            # Bare basenames ("index.html") are how prose usually refers to
            # a file whose directory is obvious from context. Resolve them
            # anywhere in the tree before calling them missing.
            if "/" not in token and any(
                    q for q in repo.rglob(token)
                    if ".git" not in q.parts and "archive" not in q.parts):
                continue
            cands = [repo / token, repo / "docs" / token,
                     repo / "docs" / "reference" / token,
                     repo / "recipes" / "local" / token,
                     repo / "tools" / token, repo / "tools" / "verify" / token,
                     repo / "tests" / token, repo / "common" / token]
            if any(c.exists() for c in cands):
                continue
            if (repo / "docs" / "archive" / token).exists():
                infos.append(f"{relname}: names `{token}` -- in docs/archive/")
            else:
                phantoms.setdefault(token, []).append(relname)

    print("=" * 72)
    print("DOC REFERENCE CHECK")
    print("=" * 72)
    if missing_docs:
        print("\nLiving documents listed in this checker but absent from the tree:")
        for m in missing_docs:
            print(f"  ? {m}")
        print("  (update LIVING in this file, or the doc set moved without it)")

    print(f"\nDEFECTS ({len(defects)}) -- broken links a reader would follow:")
    for d in defects:
        print(f"  x {d}")
    if not defects:
        print("  none")

    print(f"\nWARNINGS ({len(warns)}) -- links from living docs into the archive:")
    for w in warns:
        print(f"  ! {w}")
    if not warns:
        print("  none")

    # The signal worth reading. A token named in prose that exists NOWHERE
    # -- not live, not archived -- is either a phantom (the class that cost
    # this project three files and real hours) or an honest mention of
    # something deleted. Grouped by TOKEN, not by file: a phantom cited five
    # times is one problem, and seeing the citation count is how you tell a
    # typo from a belief.
    print(f"\nUNRESOLVED NAMES ({len(phantoms)}) -- named in prose, present nowhere:")
    if phantoms:
        for token, where in sorted(phantoms.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            print(f"  ? `{token}`  ({len(where)}x: {', '.join(sorted(set(where)))})")
        print("\n  Each is EITHER an honest mention of something deleted -- history and")
        print("  disposition tables do this correctly and constantly -- OR a phantom.")
        print("  Read the sentence around it. The three that cost real time here were")
        print("  all cited in the PRESENT tense as though they could be opened.")
    else:
        print("  none")

    print(f"\nINFO ({len(infos)}) -- prose names a file that is now in docs/archive/.")
    print("  Expected. Retired-but-named is what a disposition table looks like.")
    if a.info:
        for i in sorted(infos):
            print(f"  - {i}")
    elif infos:
        print(f"  (re-run with --info to list all {len(infos)})")

    print()
    if defects:
        print(f"RESULT: {len(defects)} defect(s).")
        return 1 if a.strict else 0
    print("RESULT: no broken pointers.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
