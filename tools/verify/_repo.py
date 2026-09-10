"""
Path resolution for the verify_*.py harnesses.

These scripts were originally written to run from a flat scratch directory
holding copies of `recipes.py`, `dgx-orchestrator.py` and whichever recipe
YAML was under test. Committing them under tools/verify/ (2026-09-09) means
`Path(__file__).parent` is no longer the repo root, so every path they use
had to move through here.

One module rather than the same three lines in six files: the scratch-copy
habit is what let the authoring copy of the GLM recipe drift from the repo
copy (see TOMBSTONES #140's commit note), and resolving against the real
tree is the fix for that as much as for the import.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = REPO_ROOT / "dgx-orchestrator.py"


def _require(path: Path, what: str) -> Path:
    if not path.exists():
        raise SystemExit(
            f"[-] {what} not found at {path}\n"
            f"    tools/verify/_repo.py resolves the repo root as parents[2] of\n"
            f"    its own location. If these scripts have been moved, fix it here\n"
            f"    rather than in each harness."
        )
    return path


def orchestrator_source() -> str:
    return _require(ORCHESTRATOR, "dgx-orchestrator.py").read_text()


def recipe_path(filename: str) -> Path:
    return _require(REPO_ROOT / "recipes" / "local" / filename, f"recipe {filename}")


def add_common_to_path() -> Path:
    """Make `import recipes` resolve to common/recipes.py, as the harnesses expect."""
    common = _require(REPO_ROOT / "common" / "recipes.py", "common/recipes.py").parent
    if str(common) not in sys.path:
        sys.path.insert(0, str(common))
    return REPO_ROOT
