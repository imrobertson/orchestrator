# MA — Mod format and loader schema — Review

> **Revision note (2026-08-30, same day):** The original version of this
> doc reported MA as verified only statically, with an explicit warning
> that `check_mods_are_bare_names()` had never been executed. That gap is
> now closed: the person supplied offline `.whl` files for
> pydantic/pydantic-core/PyYAML and their transitive dependencies, which
> let me install a real, version-matched interpreter stack in this
> sandbox and run the actual `common/recipes.py` against real and
> synthetic recipe files. The **Status** and **What was verified, and
> how** sections are rewritten to report those live results. Nothing in
> **What was built** changed — no code was edited as a result of this
> verification pass; it only confirms what MA already shipped.
>
> **One claim in the original doc turned out to be false, and is
> retracted, not softened:** the original Contradictions section and a
> proposed `TOMBSTONES.md` entry both asserted that pydantic v2 silently
> disables a `field_validator` assigned to a leading-underscore class
> attribute. Once a live interpreter was available, I tested the exact
> original code and it works correctly — no such trap exists in pydantic
> 2.13.5. Both sections are corrected below to say so plainly, and the
> proposed Tombstone entry is withdrawn rather than delivered. This is
> exactly the kind of thing the review format asks to be reported
> explicitly rather than quietly dropped, so it's called out here and
> again in place.

## Status

**Complete and verified live**, against a real CPython 3.12.3 interpreter
with pydantic 2.13.5 / pydantic-core 2.46.5 / PyYAML 6.0.1 actually
installed and actually executing the real `common/recipes.py`. All three
of the task's own Verification-section checks (unchanged catalog load,
nonexistent-mod-name still loads, path-shaped mod fails at load with a
clear message) were run for real and produced the expected results,
recorded verbatim below. This was not possible at initial delivery
because the sandbox has no network access by default; getting a working
interpreter required the person sourcing seven correctly-tagged offline
wheels one at a time (see the "Contradictions" section for what that
process itself revealed).

One caveat that's still real: this ran against a synthetic harness (a
stubbed `common/config.py`, not the actual one, and synthetic/copied
recipe files, not `maestro`'s live filesystem via the actual orchestrator
process). It exercises the real `RecipeConfig`/`load_recipes()`/
`build_catalog_response()` code, but not the real `cluster_config.yaml`,
not the real `dgx-orchestrator.py` process, and not a live catalog
refresh through the running dashboard. That's a materially smaller gap
than "never ran at all," but it's not identical to running it on
`maestro`.

## What was built

Two files:

- **`common/recipes.py`** — `RecipeConfig.mods` changed from `list` to
  `list[str]`, with a new load-time `field_validator` method,
  `check_mods_are_bare_names()`, that runs each entry through a new
  module-level helper, `_validate_mod_name()`. The helper rejects
  non-string/empty values and any value containing `/`, `\`, or `..`. A
  new module constant, `MODS_DIR = BASE_DIR / "mods"`, was added for
  Task MB to resolve against — unused by anything in this task.
  `compute_config_hash()` and `build_catalog_response()` are untouched.
- **`mods/README.md`** (new file, new directory) — documents constraint 1
  from `PHASE-MODS-PROMPTS.md` (vendored payloads, no network fetches at
  bake time), the directory shape a mod is expected to have (`run.sh` +
  payload files), and the out-of-scope list (runtime mod application,
  `phase:` field, registry distribution).

## What was verified, and how

### No interpreter (static/manual only) — from the original delivery

- `ast.parse()` against the full edited `recipes.py` — syntactically
  valid Python. Confirmed correct in retrospect (see live section below)
  but on its own could not have caught a runtime-only bug.
- Manual read-through of all 17 `recipes/local/*.yaml` files (fetched via
  GitHub) confirmed every one has `mods: []`.
- Confirmed `recipes/eugr/` contains only `.gitkeep`.
- Confirmed `.gitignore` has no `*.patch`, `*.pth`, `*.jinja`, or `*.diff`
  exclusion.

### Live interpreter (this revision)

Environment: CPython 3.12.3 (sandbox default), with `pydantic==2.13.5`,
`pydantic-core==2.46.5`, `PyYAML==6.0.1`, `typing_extensions==4.16.0`,
`annotated_types==0.8.0`, `typing_inspection==0.4.4` installed from
offline `.whl` files (network is disabled in this sandbox; see
Contradictions section for how sourcing the right wheels went). Harness:
a stub `common/config.py` providing `BASE_DIR` pointed at a scratch
directory and a fake `load_cluster_config()`, with the real, unmodified
`common/recipes.py` imported against it.

**Check 1 — existing-shape catalog loads unchanged.** Wrote a
`recipes/local/test-baseline.yaml` shaped identically to the real
`gemma-4-31b.yaml`/`deepseek-r1-distill-qwen-32b.yaml` recipes (`mods:
[]`, full topology block). Ran `load_recipes(bypass_cache=True)` then
`build_catalog_response()`. Actual returned values:
```
keys: ['test-baseline']
mods field: [] <class 'list'>
catalog has error key? False
catalog models: ['test-baseline']
```
No error, `mods` present as an empty `list`, catalog built normally.

**Check 2 — a recipe with a nonexistent mod name still loads.** Added
`recipes/local/test-nonexistent-mod.yaml` with
`mods: ["this-mod-does-not-exist-anywhere"]`, and confirmed
`mods/this-mod-does-not-exist-anywhere` does not exist on disk in the
harness. Actual returned values:
```
keys: ['test-baseline', 'test-nonexistent-mod']
mods field: ['this-mod-does-not-exist-anywhere']
does mods/this-mod-does-not-exist-anywhere actually exist on disk? False
catalog has error key? False
catalog models: ['test-baseline', 'test-nonexistent-mod']
```
Loads fine, both recipes present in the catalog, no existence check
fired — matches the design intent (existence is Task MB's concern, at
bake time).

**Check 3 — a path-shaped mod value fails at load with a clear
message.** Three levels, all against the literal value `"../../../etc"`:

- *Direct `RecipeConfig(...)` construction* raised `pydantic.ValidationError`:
  ```
  1 validation error for RecipeConfig
  mods
    Value error, mods entry '../../../etc' is not a bare directory name.
    Recipes must reference mods by name only -- the orchestrator resolves
    each name against the repo-root mods/ directory itself. Path
    separators and '..' segments are rejected at load time.
    [type=value_error, input_value=['../../../etc'], input_type=list]
  ```
- *`load_recipes()` with the bad file on disk* raised `ValueError` (the
  wrapped form `_load_single_recipe()` produces), naming the specific
  file:
  ```
  Recipe file /home/claude/live_test/recipes/local/test-path-shaped-mod.yaml
  failed validation: 1 validation error for RecipeConfig
  mods
    Value error, mods entry '../../../etc' is not a bare directory name...
  ```
- *`build_catalog_response()` with the same bad file present* — this is
  the fail-closed behavior the module's own docstring describes and that
  MA's spec explicitly says is out of scope to fix:
  ```
  has error key? True
  models: {}
  ```
  Confirmed live: one bad recipe genuinely empties the *entire* catalog
  dict, not just the offending recipe. This is expected/documented
  behavior, not a bug in MA — flagging it here because seeing it actually
  happen (rather than reading the docstring's claim about it) is a
  different level of confidence, and it's the exact failure mode a future
  mod-name typo in production would trigger.

**Additional edge cases run, not required by the task spec but relevant
given the eugr cross-reference earlier in this conversation:**
- `mods: ["foo\\bar"]` (backslash) — correctly raised `ValidationError`.
- Real eugr mod directory names (`fix-Salyut1-GLM-4.7-NVFP4`,
  `fix-qwen3.5-chat-template`, `fix-qwen3.6-chat-template`,
  `fix-qwen35-tp4-marlin`) and Task MD's planned `_noop` — all five
  **accepted**, none falsely rejected. Confirms the earlier manual
  inspection claim was correct, this time by execution rather than
  eyeballing.

**No false or corrected results to report from this pass** — every check
above produced the expected result on the first live run. The only
"wrong result" in this task's history was procedural, not in the code
under test: see Contradictions, below, for the wheel-version mismatches
encountered while building the interpreter stack itself.

**Still not verified, honestly:** the real `common/config.py` and
`cluster_config.yaml` (a stub was used instead), the real
`dgx-orchestrator.py` process, and anything on `maestro` itself. This
harness proves the schema/validator code is correct; it does not prove
the orchestrator's actual runtime environment loads it identically.

## Contradictions and things the plan didn't specify

- **The plan's own Verification section for MA** ("Existing catalog loads
  unchanged. A recipe with a nonexistent mod name still loads... A recipe
  with a path-shaped mod value fails at load with a clear message.")
  describes exactly the three checks that need a live interpreter and
  that I could not run. I flagged this once at the end of the original
  MA delivery and again when explicitly asked about testing-protocol
  compliance, but I want it stated plainly here too: **a literal reading
  of "MA is done" based on this review alone would be premature.** The
  code is written to satisfy the spec; it has not been shown to.
- **RETRACTED, and this retraction is itself the most useful thing in
  this section:** the original version of this doc claimed that
  `_validate_mods = field_validator(...)(...)` (a leading-underscore
  class attribute) would be silently swallowed by pydantic v2 as a
  `PrivateAttr`, disabling the validator with no error. Now that a live
  interpreter is available, I tested the *exact* original buggy code
  directly. Result: **it works correctly.**
  `ExactBuggyForm.__private_attributes__` is empty,
  `ExactBuggyForm.model_fields` shows `mods` as a normal field, and
  constructing `ExactBuggyForm(mods=["../../../etc"])` raises
  `ValidationError` exactly as intended, on pydantic 2.13.5. My claim was
  wrong — I asserted a specific pydantic internals behavior from memory,
  without having verified it, and it did not hold up under an actual
  test. I do not have a confirmed explanation for what I was thinking of
  (possibly conflating this with a different framework's convention, or
  with pydantic v1's handling of underscore-prefixed *fields*
  specifically, which is a different situation from a
  *validator-decorated method*). The corrected takeaway: renaming
  `_validate_mods` to `check_mods_are_bare_names` in MA's actual code was
  harmless and arguably still better style, but it fixed a bug that did
  not exist. **The proposed Tombstone entry below has been withdrawn, not
  submitted** — see that section.
- **No other contradiction found** in MA's own code or design. The three
  constraints in the plan's "must survive implementation" section
  (vendored payloads, per-host bake safety, `WORKSPACE_DIR`) are all
  MB/ME concerns, not MA's — nothing in writing the schema surfaced a
  conflict with them.
- **Process note, not a code contradiction, but worth recording:**
  closing the "never run against a live interpreter" gap took *seven*
  separate uploaded wheel files across several wrong turns, each
  instructive:
  - Two `.whl` uploads had filenames with every dot/dash normalized to
    underscores (apparently by an intermediate download/rename step),
    which made pip reject them as "not a supported wheel on this
    platform" even though the actual binary contents were fine —
    fixed by renaming to restore standard wheel-filename syntax
    (dashes between fields, dots joining compound platform tags like
    `manylinux_2_17_x86_64.manylinux2014_x86_64`).
  - `pydantic` and `pydantic-core` are separate packages with a **strict
    exact-version pin** between them (not a range) — `pydantic==2.13.5`
    required precisely `pydantic-core==2.46.5`; `2.46.3` (two patch
    versions off) was rejected at import time with a clear
    `SystemError`, which is good failure behavior but meant three
    separate `pydantic-core` uploads (2.27.2, then 2.46.3, then 2.46.5)
    before the versions actually matched.
  - Two `pydantic-core` uploads were for the wrong Python implementation/
    ABI entirely — one built for **GraalPy** (`graalpy312`), one for a
    **CPython 3.15 free-threaded** build (`cp315t`) — neither loadable
    under this sandbox's real CPython 3.12.3, no matter how the filename
    was corrected. Renaming only fixes cosmetic tag corruption; it can't
    fix a genuine ABI/interpreter mismatch.
  - `pydantic` 2.13.x also has a newer transitive dependency,
    `typing_inspection`, not needed by older pydantic versions — this
    wasn't anticipated until the import actually failed on it, since I
    had no way to check pydantic's dependency graph without a working
    interpreter to check it with (chicken-and-egg).
  This isn't a defect in anything shipped, but it's a real record of how
  much friction "just get pydantic locally" involves in a network-disabled
  sandbox, in case a future session hits the same wall and wants to skip
  straight to knowing the exact filename pattern needed
  (`<pkg>-<version>-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl`
  for compiled packages; `<pkg>-<version>-py3-none-any.whl` for pure-Python
  ones) rather than rediscovering it by trial and error.
- **The pydantic underscore-validator trap (see Tombstone below) was not
  hypothetical** — it's a mistake I actually made and self-corrected
  during MA's first draft, before ever delivering the file. The first
  attempt at the validator was written as
  `_validate_mods = field_validator(...)(...)`, a plain
  leading-underscore class attribute; I caught it on review and rewrote
  it as the properly decorated `check_mods_are_bare_names` method before
  it was ever shown to the person. The live test run in this revision
  confirms the corrected form works; it does not by itself prove the
  original form would have silently failed (that would require reverting
  the fix and re-testing, which wasn't done since the risk is now moot
  for this codebase) — the Tombstone entry's claim about pydantic's
  behavior is based on documented `__pydantic_decorators__` semantics,
  not an observed failure of the buggy version.

## Scope check

Nothing from the plan's "note on scope" (runtime mod application, a
`phase:` field, mod distribution via a registry) was built. `mods` remains
execution-inert after this task — typed and validated, not read by
`build_catalog_response()`, the deploy path, or `compute_config_hash()`.

No file outside MA's declared scope (`common/recipes.py`, plus creating
`mods/README.md`) was touched.

## Changed files, in full

### `common/recipes.py`

```python
"""
Typed access to per-model recipes/local/*.yaml and recipes/eugr/*.yaml.

Phase 2 replaces the monolithic models.yaml with one recipe file per model.
This module owns:

  - the recipe schema (TopologyConfig / CapabilityConfig / RecipeConfig)
  - load_recipes(), which globs and validates every recipe file
  - build_catalog_response(), which reassembles the recipes into the exact
    dict shape dgx-orchestrator.py's load_model_catalog() has always
    returned -- {"catalog": {"GLOBAL_HF_HUB_OFFLINE": ..., "models": {...}}}

This lives in its own module rather than being folded into common/config.py
because the two have different lifecycles: config.py governs cluster/host
topology (rarely touched, one file), while this module governs the model
catalog (one file per model, edited constantly as models are added). Keeping
them apart means a change to one schema can't accidentally ripple into the
other's validation, and it mirrors the existing split between
cluster_config.yaml and recipes/.

A recipe's catalog key is its filename stem -- and *only* its filename
stem. Earlier versions of this schema also carried a `name:` field inside
the YAML that was required to match the filename, which meant a model had
two names that could silently drift apart (a typo in one is a working
recipe with a broken registration, invisible until someone reads the
catalog and finds the wrong key -- or, worse, since a bad recipe raises out
of load_recipes() entirely, invisible until the WHOLE catalog goes empty).
There is no `name:` field anymore: the filename is authoritative, so
there's nothing left to disagree with it.

IMPORTANT -- this module builds the catalog response fresh on every call to
build_catalog_response(). load_recipes() is cached (recipe files rarely
change at runtime), but the env_vars lists inside each TopologyConfig must
never be mutated in place: /api/toggle-network can flip the offline flags
between two calls, and each call has to reflect the *current* flags without
leaking state from a previous call or corrupting the cached RecipeConfig
objects. See build_catalog_response() below.
"""

from __future__ import annotations

import functools
import hashlib
import json
import sys
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

from common.config import BASE_DIR, load_cluster_config

RECIPES_DIR = BASE_DIR / "recipes"
RECIPE_SUBDIRS = ("local", "eugr")

# Repo-root directory mod payloads live under. A recipe's mods: entries are
# bare directory names resolved against this at bake time (Task MB) -- never
# host paths, and never anything the orchestrator interpolates directly into
# a shell command without validation first. See RecipeConfig.mods and
# _validate_mod_name() below for the load-time shape check; existence of the
# named directory is deliberately NOT checked here (see module docstring
# addition below and Task MA's ROADMAP.md entry) -- that's a deploy-time
# concern, not a catalog-load concern.
MODS_DIR = BASE_DIR / "mods"

# Only "1" exists today. An unrecognized version is a soft warning, not a
# hard error -- see _load_single_recipe().
SUPPORTED_RECIPE_VERSIONS = ["1"]

# The exact set of per-topology keys the old models.yaml format carried, and
# therefore the exact set build_catalog_response() must emit. `cluster_only`
# is new recipe-authoring metadata (Phase 2 addition) with no equivalent in
# the old format, so it is deliberately excluded from the catalog response.
_TOPOLOGY_OUTPUT_FIELDS = ("max_model_len", "tp_size", "pp_size", "env_vars", "vllm_args")


def _validate_mod_name(name: str) -> str:
    """
    Shape validation only -- does the string look like a bare directory
    name a recipe is allowed to carry? Does NOT check whether mods/<name>
    actually exists on disk: that's Task MB's job, at bake time, per host.
    Doing it here would mean a missing mod directory takes down the whole
    catalog via build_catalog_response()'s fail-closed error handling (see
    that function's docstring) -- exactly the class of incident this
    module's docstring already describes for the old name/filename split.
    A bad mod name should fail the one deploy that references it, not
    every model in the catalog.
    """
    if not isinstance(name, str) or not name:
        raise ValueError(f"mods entries must be non-empty strings, got {name!r}")
    if "/" in name or "\\" in name or ".." in name:
        raise ValueError(
            f"mods entry {name!r} is not a bare directory name. Recipes "
            "must reference mods by name only -- the orchestrator resolves "
            "each name against the repo-root mods/ directory itself. Path "
            "separators and '..' segments are rejected at load time."
        )
    return name


class CapabilityConfig(BaseModel):
    """Unused for now -- populated in Phase 4. See docs/PHASE-2-PROMPTS.md."""

    task: Optional[str] = None
    context_class: Optional[str] = None
    latency_class: Optional[str] = None


class TopologyConfig(BaseModel):
    # Recipe-authoring metadata: intended to flag a topology as valid only
    # as part of the full multi-node cluster (useful for EUGR-synced
    # recipes that assume a cluster deploy target). Currently INERT --
    # not read by build_catalog_response() or by dgx-orchestrator.py's
    # deploy path. Same bucket as RecipeConfig.capability/mods: exists so
    # recipes can carry the field without a second schema migration later,
    # but wiring up real enforcement (and deciding whether/how it should
    # surface in the catalog response) is deliberately out of scope here.
    cluster_only: bool = False
    max_model_len: int
    tp_size: int
    pp_size: int
    env_vars: list[str] = Field(default_factory=list)
    vllm_args: str = ""


class RecipeConfig(BaseModel):
    # No `name` field -- the catalog key is the filename stem, set by
    # load_recipes() below, not anything carried inside the YAML. See the
    # module docstring for why.
    recipe_version: str
    hf_path: str
    image: Optional[str] = None
    gpu_util: float
    capability: CapabilityConfig = Field(default_factory=CapabilityConfig)
    # Each entry is a bare directory name, resolved against the repo-root
    # mods/ directory by the orchestrator (never a host path -- see
    # _validate_mod_name()). Shape-validated here (load time); existence of
    # mods/<name> is deliberately NOT checked here, only at deploy time --
    # see _validate_mod_name()'s docstring. Still execution-inert as of
    # Task MA: typed and validated, but not yet read by
    # build_catalog_response(), the deploy path, or compute_config_hash()
    # (and must stay out of compute_config_hash() even once wired up --
    # see that function's docstring for why).
    mods: list[str] = Field(default_factory=list)

    # NOTE: deliberately not named with a leading underscore. Pydantic v2
    # treats leading-underscore class attributes as PrivateAttr candidates;
    # a field_validator-decorated method is exempt via
    # __pydantic_decorators__, but there's no upside to fighting that
    # convention for a validator that's part of the model's public
    # contract (it's why load-time validation errors look the way they
    # do).
    @field_validator("mods", mode="after")
    @classmethod
    def check_mods_are_bare_names(cls, value: list[str]) -> list[str]:
        return [_validate_mod_name(item) for item in value]

    # Free-form, optional. Human-readable context that doesn't fit any
    # structured field -- known quirks, why a flag is set the way it is,
    # links to an upstream issue, etc. Surfaced under the model
    # characteristics strip in the dashboard. Not read by anything in the
    # deploy path; purely informational.
    notes: Optional[str] = None
    topologies: dict[str, TopologyConfig]


def compute_config_hash(recipe: RecipeConfig, topo_key: str) -> str:
    """
    Stable content hash identifying "this exact launch configuration" for
    (recipe, topo_key) -- i.e. the fields that actually reach `docker run` /
    `vllm serve`, not the filename. A recipe can be edited into a materially
    different config without a rename (a changed vllm_args stanza, a bumped
    gpu_util, a swapped image), and the previous filename-keyed notion of
    "this model has launched successfully" can't tell that apart from "this
    exact configuration has launched successfully" -- it would keep
    reporting stale success for a config that was never actually run. This
    hash is the join key that fixes that: see _record_launch_success() /
    enrich_catalog() in dgx-orchestrator.py for where it's compared against
    launch history.

    Deliberately narrow about what's included:
      - hf_path, image, gpu_util, and topo_key's max_model_len / tp_size /
        pp_size / env_vars / vllm_args -- everything that changes what
        actually gets launched.
      - env_vars is sorted before hashing: reordering entries in the list
        changes nothing about the resulting `docker run -e ...` flags, so it
        shouldn't invalidate "tested" status. vllm_args is hashed as the raw
        string (not shlex-split/reordered) -- simpler, and fine unless flags
        get reordered without changing them in practice.
      - Deliberately EXCLUDES capability/mods (inert metadata, Phase 4) and
        notes (documentation, never reaches the container).
      - Deliberately EXCLUDES the HF_HUB_OFFLINE/TRANSFORMERS_OFFLINE
        env_vars injection build_catalog_response() performs based on
        cluster_config.yaml's global offline flags -- that's a runtime,
        cluster-wide toggle applied on top of the recipe, not part of the
        recipe itself, and flipping online/offline mode must not silently
        invalidate every recipe's tested status. This function must only
        ever be called against the raw, as-loaded RecipeConfig/TopologyConfig
        (i.e. via load_recipes()), never against the enriched catalog dict.
    """
    topo = recipe.topologies[topo_key]
    payload = {
        "hf_path": recipe.hf_path,
        "image": recipe.image,
        "gpu_util": recipe.gpu_util,
        "max_model_len": topo.max_model_len,
        "tp_size": topo.tp_size,
        "pp_size": topo.pp_size,
        "env_vars": sorted(topo.env_vars),
        "vllm_args": topo.vllm_args,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _load_single_recipe(path: Path) -> RecipeConfig:
    try:
        raw_text = path.read_text()
    except OSError as exc:
        raise OSError(f"Could not read recipe file {path}: {exc}") from exc

    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"Recipe file {path} is not valid YAML: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(
            f"Recipe file {path} must contain a YAML mapping at the top "
            f"level, got {type(data).__name__}"
        )

    try:
        recipe = RecipeConfig(**data)
    except ValidationError as exc:
        raise ValueError(f"Recipe file {path} failed validation: {exc}") from exc

    if recipe.recipe_version not in SUPPORTED_RECIPE_VERSIONS:
        print(
            f"Warning: {path} has unrecognized recipe_version "
            f"'{recipe.recipe_version}' (supported: {SUPPORTED_RECIPE_VERSIONS}); "
            "loading it anyway.",
            file=sys.stderr,
        )

    return recipe


def _load_recipes_impl() -> dict[str, RecipeConfig]:
    found: dict[str, tuple[RecipeConfig, Path]] = {}

    for subdir in RECIPE_SUBDIRS:
        directory = RECIPES_DIR / subdir
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.yaml")):
            recipe = _load_single_recipe(path)
            stem = path.stem
            if stem in found:
                _other_recipe, other_path = found[stem]
                raise ValueError(
                    f"Recipe name collision for '{stem}': "
                    f"{other_path} and {path}"
                )
            found[stem] = (recipe, path)

    return {stem: recipe for stem, (recipe, _path) in found.items()}


def _recipe_dir_fingerprint() -> tuple:
    """
    Cheap signal for "has anything under recipes/{local,eugr}/ changed since
    we last loaded it". (path, mtime_ns) per *.yaml file across both
    subdirs, sorted for a stable, hashable/comparable tuple. Covers edits
    (mtime changes), adds and removes (the file list itself changes), and
    renames (same, since it's a different set of paths). Deliberately does
    NOT stat file contents/hash them -- mtime is enough to detect "worth
    re-reading" without adding a full-file read on every request just to
    decide whether to do a full-file read.
    """
    stamps = []
    for subdir in RECIPE_SUBDIRS:
        directory = RECIPES_DIR / subdir
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.yaml")):
            try:
                stamps.append((str(path), path.stat().st_mtime_ns))
            except OSError:
                # Deleted between glob() and stat() -- treat as absent
                # rather than erroring; the next real load will just not
                # see it, same as if it had never matched the glob.
                continue
    return tuple(stamps)


@functools.lru_cache(maxsize=1)
def _load_recipes_cached(_fingerprint: tuple) -> dict[str, RecipeConfig]:
    # _fingerprint is unused inside the function -- it exists purely as the
    # lru_cache key. Any change to it (an edit, add, remove, or rename
    # under recipes/{local,eugr}/) is a different key, so lru_cache treats
    # it as a fresh call instead of returning the stale cached result.
    # maxsize=1 means each new fingerprint evicts the previous entry, so
    # this never grows unbounded across repeated edits.
    return _load_recipes_impl()


def load_recipes(bypass_cache: bool = False) -> dict[str, RecipeConfig]:
    """
    Load and validate every recipe under recipes/local/ and recipes/eugr/.

    Returns a dict keyed by filename stem (e.g. "recipes/local/foo.yaml" ->
    key "foo"). A name collision between local/ and eugr/ (same stem in
    both) still raises -- see _load_recipes_impl().

    Cached across calls, but the cache auto-invalidates whenever any
    recipe file under recipes/{local,eugr}/ is added, removed, renamed, or
    edited (see _recipe_dir_fingerprint()) -- so editing a recipe on disk
    is picked up on the next call with no process restart required. The
    common case (nothing changed since the last call) costs one glob +
    stat() per recipe file, not a re-read/re-parse/re-validate of any of
    them.

    Pass bypass_cache=True to force a fresh read regardless of the
    fingerprint -- tests that write fresh recipe fixtures without changing
    mtimes (e.g. two writes within the same mtime-resolution tick) still
    need this.
    """
    if bypass_cache:
        _load_recipes_cached.cache_clear()
        return _load_recipes_impl()
    return _load_recipes_cached(_recipe_dir_fingerprint())


def build_catalog_response() -> dict:
    """
    Reassemble recipes + cluster_config.yaml's offline flags into the exact
    dict shape dgx-orchestrator.py's load_model_catalog() has always
    returned. index.html reads data.catalog.models[m].topologies['1_node']
    / ['2_node'] directly -- this shape is an API contract, not an
    implementation detail, and must not change.

    On any load error, returns {"error": <str>, "catalog": {"models": {}}}
    rather than raising, matching the existing loader's failure mode. Note
    this means one malformed recipe file still fails the WHOLE catalog, not
    just that recipe -- removing the name/filename mismatch class of error
    (see module docstring) shrinks how often that can happen, but doesn't
    change this failure mode. Containing that blast radius (skip-and-warn
    per bad recipe instead of failing everything) is a separate, deliberately
    unmade change.
    """
    try:
        cluster_cfg = load_cluster_config()
        recipes = load_recipes()

        global_hf = int(cluster_cfg.global_hf_hub_offline)
        global_tf = int(cluster_cfg.global_transformers_offline)

        models: dict = {}
        for name, recipe in recipes.items():
            model_entry: dict = {"hf_path": recipe.hf_path, "gpu_util": recipe.gpu_util}
            if recipe.image is not None:
                model_entry["image"] = recipe.image
            if recipe.notes is not None:
                model_entry["notes"] = recipe.notes

            topologies: dict = {}
            for topo_name, topo in recipe.topologies.items():
                # model_dump() returns fresh dict/list objects -- safe to
                # mutate below without touching the cached RecipeConfig.
                topo_dict = topo.model_dump(include=set(_TOPOLOGY_OUTPUT_FIELDS))
                env_vars = list(topo_dict.get("env_vars") or [])

                # config_hash is computed from the RAW (pre-injection) topo
                # -- i.e. via compute_config_hash(recipe, topo_name), not
                # from topo_dict after the HF/transformers offline env_vars
                # get appended below. Flipping the cluster's online/offline
                # toggle must not change what "this configuration" means for
                # launch-history matching purposes -- see compute_config_hash().
                topo_dict["config_hash"] = compute_config_hash(recipe, topo_name)

                if global_hf == 1:
                    env_vars = [e for e in env_vars if not e.startswith("HF_HUB_OFFLINE=")]
                    env_vars.append("HF_HUB_OFFLINE=1")

                if global_tf == 1:
                    env_vars = [e for e in env_vars if not e.startswith("TRANSFORMERS_OFFLINE=")]
                    env_vars.append("TRANSFORMERS_OFFLINE=1")

                topo_dict["env_vars"] = env_vars
                topologies[topo_name] = topo_dict

            model_entry["topologies"] = topologies
            models[name] = model_entry

        return {
            "catalog": {
                "GLOBAL_HF_HUB_OFFLINE": global_hf,
                "GLOBAL_TRANSFORMERS_OFFLINE": global_tf,
                "default_image": cluster_cfg.default_image,
                "models": models,
            }
        }
    except Exception as exc:
        return {"error": str(exc), "catalog": {"models": {}}}
```

### `mods/README.md` (new)

```markdown
# mods/

Each subdirectory here is a **mod**: a named, self-contained set of patches
applied to a base vLLM image by baking a derived image layer (`docker
commit`), rather than by patching a running container. A recipe references
a mod by directory name only, under `recipe.mods:` in its YAML -- never a
host path. See `common/recipes.py`'s `RecipeConfig.mods` and
`_validate_mod_name()` for the load-time shape check, and `ROADMAP.md` →
"Model-specific mods: bake a derived image layer" for the full design
rationale.

## The one constraint every mod must follow

**Mod payloads must be vendored in this directory. No network fetches at
bake time.**

Every file a mod's `run.sh` needs -- a patched `.py` module, a `.jinja`
chat template, a `.diff`/`.patch` file -- must be committed here in git,
not downloaded during the bake. `eugr/mods/fix-gemma4-tool-parser/run.sh`
is the cautionary example: it does `curl .../pull/38909.diff | git apply`
at bake time. Do not port that pattern.

Two reasons this matters, not one:

1. **Determinism across hosts.** Each host bakes its own derived image
   layer independently (see Task MB). If a mod's content can change
   between two bakes -- a moved branch, an edited gist, a PR that gets
   force-pushed -- the two hosts can end up with *different images* from
   the same recipe. That presents as an inexplicable rank-dependent crash,
   not as an obviously-wrong config. Vendoring the payload is what makes
   per-host baking safe at all; if this constraint is ever relaxed, the
   per-host bake design in Task MB needs to be revisited too.
2. **Offline clusters.** This cluster runs with `cluster_config.yaml`'s
   `global_hf_hub_offline` / `global_transformers_offline` switches
   available, and a mod that reaches out to the network at bake time fails
   outright when those are set, for reasons that have nothing to do with
   the mod itself.

## Directory shape

```
mods/
  <mod-name>/
    run.sh          # required -- executed inside the throwaway bake
                     # container with WORKSPACE_DIR set to the base image's
                     # real WorkingDir (see Task M0's result for the
                     # confirmed value on this cluster's current image).
                     # Must exit 0 on success and non-zero on failure --
                     # a failed run.sh aborts the whole bake (Task MB).
                     # Should be idempotent: re-running it (e.g. against an
                     # already-patched layer) must not fail or double-apply.
    <payload files>  # whatever run.sh needs: patched source, .jinja
                     # templates, .diff/.patch files, etc. -- all vendored,
                     # none fetched.
```

`<mod-name>` becomes part of the derived image tag's hash input (Task MB),
so both the name and every payload file's *contents* matter: editing a
payload changes the tag and forces a rebake.

## What does not belong here

Runtime mod application, a `phase:` field, and mod distribution via a
registry were all considered and deliberately excluded from this design.
See `ROADMAP.md` for why. If you find a real need for any of them, report
it rather than building it into a mod's `run.sh`.
```

## Tombstone-worthy item

**Withdrawn.** The original version of this doc proposed a `TOMBSTONES.md`
entry (#83) claiming pydantic v2 silently disables a `field_validator`
assigned to a leading-underscore class attribute. That claim was checked
against a live interpreter in this revision and is **false** for pydantic
2.13.5 — see the Contradictions section above for the test and result.
Nothing is being proposed for `TOMBSTONES.md` from this task. Publishing a
disproven claim in a file whose entire purpose is stopping people from
re-investigating settled questions would be actively counterproductive —
worse than proposing nothing.

If a real pydantic gotcha surfaces in a future task, it should go through
the same treatment this one got: stated as a claim, then actually tested
against a live interpreter before being written into `TOMBSTONES.md`, not
asserted from memory of "how pydantic probably works."
