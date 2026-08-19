#!/usr/bin/env python3
"""
Proves the new recipes/ + common.recipes.build_catalog_response() path
produces results identical to the old models.yaml + load_model_catalog()
path, before Task 2C switches dgx-orchestrator.py / cache_cluster_assets.py
over to it.

Two checks:

  1. Catalog structure -- deep-compares old vs. new build_catalog_response()
     output across all four combinations of the two offline flags.

  2. Rendered deploy commands -- for every model x topology dgx-orchestrator
     supports (23 combinations), drives the real dry-run deploy path
     (execute_deployment(..., dry_run=True)) once against the old catalog
     and once against the new one, and compares the exact docker run
     argument lists that would have been sent over SSH.

execute_deployment(dry_run=True) is a pure computation: per its own
docstring it "never calls run_ssh or mutates any cluster state." No SSH
connection, teardown, or GPU clock command is ever issued by this script.

This script is read-only with respect to production code: dgx-orchestrator.py
has a hyphen in its filename and can't be `import`ed normally, so it's loaded
via importlib and only ever monkeypatched on the in-memory module object
(swapping which load_model_catalog implementation it calls) -- the file on
disk is never touched.

Usage:
    python3 tools/verify_recipe_equivalence.py

Exit 0 if everything matches, 1 otherwise.
"""

from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import sys
import tempfile
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import common.config as config_mod
from common.recipes import build_catalog_response

ORCHESTRATOR_PATH = REPO_ROOT / "dgx-orchestrator.py"
MODELS_YAML_PATH = REPO_ROOT / "models.yaml"
CLUSTER_CONFIG_PATH = REPO_ROOT / "cluster_config.yaml"

OFFLINE_FLAG_COMBOS = [(0, 0), (1, 0), (0, 1), (1, 1)]


# --------------------------------------------------------------------------
# Loading dgx-orchestrator.py (hyphenated filename -> can't `import` it)
# --------------------------------------------------------------------------

def load_orchestrator_module():
    spec = importlib.util.spec_from_file_location(
        "dgx_orchestrator_under_test", ORCHESTRATOR_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(ORCHESTRATOR_PATH.parent))
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------
# Part 1 -- catalog structure comparison
# --------------------------------------------------------------------------

def _fmt_path(path: str, key) -> str:
    return f"{path}.{key}" if path else str(key)


def diff_structures(old, new, path: str = "") -> list[str]:
    """
    Path-qualified recursive diff. Dict key order is ignored; list order is
    NOT (env_vars order affects the generated docker run command).
    """
    diffs: list[str] = []

    if isinstance(old, dict) and isinstance(new, dict):
        old_keys, new_keys = set(old.keys()), set(new.keys())
        for k in sorted(old_keys - new_keys):
            diffs.append(f"{_fmt_path(path, k)}: present in OLD only (value={old[k]!r})")
        for k in sorted(new_keys - old_keys):
            diffs.append(f"{_fmt_path(path, k)}: present in NEW only (value={new[k]!r})")
        for k in sorted(old_keys & new_keys):
            diffs.extend(diff_structures(old[k], new[k], _fmt_path(path, k)))
        return diffs

    if isinstance(old, list) and isinstance(new, list):
        if old != new:
            diffs.append(f"{path}: {old!r} != {new!r}")
        return diffs

    if old != new:
        diffs.append(f"{path}: {old!r} != {new!r}")
    return diffs


def _write_models_yaml_with_flags(tmp_dir: Path, hf: int, tf: int) -> Path:
    data = yaml.safe_load(MODELS_YAML_PATH.read_text())
    data["GLOBAL_HF_HUB_OFFLINE"] = hf
    data["GLOBAL_TRANSFORMERS_OFFLINE"] = tf
    out_path = tmp_dir / f"models_hf{hf}_tf{tf}.yaml"
    out_path.write_text(yaml.safe_dump(data, sort_keys=False))
    return out_path


def _write_cluster_config_with_flags(tmp_dir: Path, hf: int, tf: int) -> Path:
    data = yaml.safe_load(CLUSTER_CONFIG_PATH.read_text())
    data["global_hf_hub_offline"] = hf
    data["global_transformers_offline"] = tf
    out_path = tmp_dir / f"cluster_config_hf{hf}_tf{tf}.yaml"
    out_path.write_text(yaml.safe_dump(data, sort_keys=False))
    return out_path


def run_catalog_structure_comparison(mod, old_load_model_catalog) -> list[str]:
    all_diffs: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        for hf, tf in OFFLINE_FLAG_COMBOS:
            models_yaml_variant = _write_models_yaml_with_flags(tmp_dir, hf, tf)
            cluster_config_variant = _write_cluster_config_with_flags(tmp_dir, hf, tf)

            mod.MODELS_YAML_PATH = models_yaml_variant
            config_mod.CLUSTER_CONFIG_PATH = cluster_config_variant
            config_mod.load_cluster_config.cache_clear()

            old_resp = old_load_model_catalog()
            new_resp = build_catalog_response()

            if "error" in old_resp:
                all_diffs.append(f"[flags hf={hf} tf={tf}] OLD load_model_catalog() errored: {old_resp['error']}")
                continue
            if "error" in new_resp:
                all_diffs.append(f"[flags hf={hf} tf={tf}] NEW build_catalog_response() errored: {new_resp['error']}")
                continue

            old_catalog = copy.deepcopy(old_resp["catalog"])
            new_catalog = new_resp["catalog"]

            # models.yaml's dead top-level `hosts` key (host inventory now
            # lives entirely in cluster_config.yaml) is not present as of
            # the current models.yaml, and the recipe format never carried
            # it -- this pop is a defensive no-op kept in case it's ever
            # reintroduced upstream.
            old_catalog.pop("hosts", None)

            combo_diffs = diff_structures(old_catalog, new_catalog)
            all_diffs.extend(f"[flags hf={hf} tf={tf}] {d}" for d in combo_diffs)

    # Restore real paths + cache state for Part 2.
    mod.MODELS_YAML_PATH = MODELS_YAML_PATH
    config_mod.CLUSTER_CONFIG_PATH = CLUSTER_CONFIG_PATH
    config_mod.load_cluster_config.cache_clear()

    return all_diffs


# --------------------------------------------------------------------------
# Part 2 -- rendered deploy command comparison
# --------------------------------------------------------------------------

def get_model_topology_combinations() -> list[tuple[str, str]]:
    resp = build_catalog_response()
    if "error" in resp:
        raise RuntimeError(f"build_catalog_response() errored: {resp['error']}")
    combos = []
    for name, model in resp["catalog"]["models"].items():
        for topo_name in model.get("topologies", {}):
            combos.append((name, topo_name))
    return combos


def capture_docker_run_cmds(mod, catalog_fn, model_name: str, nodes: int) -> dict[str, list[str]]:
    """
    Drive the real dry-run deploy path. execute_deployment(dry_run=True)
    never calls run_ssh (see its docstring) -- no mocking needed, no host
    is ever contacted.
    """
    mod.load_model_catalog = catalog_fn

    with contextlib.redirect_stdout(io.StringIO()):  # swallow "[!] No HF_TOKEN..." noise
        result = mod.execute_deployment(
            model_name, nodes, "spark-4", "verify-equiv-user", dry_run=True
        )

    if result.get("status") != "dry_run":
        topo = "2_node" if nodes == 2 else "1_node"
        raise RuntimeError(
            f"{model_name}.{topo}: expected dry_run status from execute_deployment(), "
            f"got {result!r}"
        )

    # ContainerRole values are a StrEnum; stringify for stable diffing/printing.
    return {
        host: [str(arg) for arg in cmd]
        for host, cmd in result["docker_run_commands"].items()
    }


def run_deploy_command_comparison(mod, old_load_model_catalog) -> tuple[list[str], int]:
    diffs: list[str] = []
    combos = get_model_topology_combinations()

    try:
        for model_name, topo_name in combos:
            nodes = 2 if topo_name == "2_node" else 1

            old_cmds = capture_docker_run_cmds(mod, old_load_model_catalog, model_name, nodes)
            new_cmds = capture_docker_run_cmds(mod, build_catalog_response, model_name, nodes)

            if old_cmds != new_cmds:
                diffs.append(
                    f"{model_name}.{topo_name}: docker run args differ\n"
                    f"    OLD: {old_cmds!r}\n"
                    f"    NEW: {new_cmds!r}"
                )
    finally:
        mod.load_model_catalog = old_load_model_catalog

    return diffs, len(combos)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    mod = load_orchestrator_module()
    old_load_model_catalog = mod.load_model_catalog  # the untouched, still-live implementation

    print("Part 1: catalog structure (4 offline-flag combinations)...")
    structure_diffs = run_catalog_structure_comparison(mod, old_load_model_catalog)
    print(f"  {len(OFFLINE_FLAG_COMBOS)} flag combinations checked, {len(structure_diffs)} difference(s) found")

    print("Part 2: rendered deploy commands (every model x topology, via execute_deployment(dry_run=True))...")
    command_diffs, combo_count = run_deploy_command_comparison(mod, old_load_model_catalog)
    print(f"  {combo_count} model x topology combinations checked, {len(command_diffs)} difference(s) found")

    total_diffs = len(structure_diffs) + len(command_diffs)

    print()
    print("=" * 78)
    print("SUMMARY")
    print("=" * 78)
    print(f"Catalog structure:    {len(OFFLINE_FLAG_COMBOS)} combinations checked, {len(structure_diffs)} differences")
    print(f"Rendered deploy cmds: {combo_count} combinations checked, {len(command_diffs)} differences")
    print(f"Total differences:    {total_diffs}")

    if total_diffs:
        print()
        print("First differences (up to 3 from each part, in full):")
        for d in structure_diffs[:3]:
            print(f"  [catalog structure] {d}")
        for d in command_diffs[:3]:
            print(f"  [deploy command] {d}")
        print()
        print("FAILED: new recipe path does not match old models.yaml path.")
        return 1

    print()
    print("EQUIVALENT: new recipe path matches old models.yaml path exactly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
