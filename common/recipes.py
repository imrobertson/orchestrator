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
