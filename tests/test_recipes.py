#!/usr/bin/env python3
"""
Plain-assert tests for common/recipes.py. Run with:
    python3 tests/test_recipes.py

Assumes tools/convert_models_yaml.py has already been run so
recipes/local/*.yaml exists (the Phase 2A verification steps run the
conversion first -- see PHASE-2-PROMPTS.md).
"""

import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import common.recipes as recipes_mod
from common.recipes import build_catalog_response, load_recipes
import common.config as config_mod


def _fresh_cluster_config():
    """
    Bypass load_cluster_config()'s lru_cache so tests that mutate the
    returned ClusterConfig (to flip offline flags) don't leak into other
    tests or into the real cache used elsewhere in the process.
    """
    config_mod.load_cluster_config.cache_clear()
    return config_mod.load_cluster_config()


def test_load_recipes_count():
    recipes = load_recipes(bypass_cache=True)
    assert len(recipes) == 16, f"expected 16 models, got {len(recipes)}"
    print(f"PASS: load_recipes() returns {len(recipes)} models")


def test_topology_count():
    recipes = load_recipes(bypass_cache=True)
    n1 = sum(1 for r in recipes.values() if "1_node" in r.topologies)
    n2 = sum(1 for r in recipes.values() if "2_node" in r.topologies)
    total = sum(len(r.topologies) for r in recipes.values())
    assert n1 == 12, f"expected 12 models with 1_node, got {n1}"
    assert n2 == 11, f"expected 11 models with 2_node, got {n2}"
    assert total == 23, f"expected 23 total topology combinations, got {total}"
    print(f"PASS: topology counts are 1_node={n1}, 2_node={n2}, total={total}")


def test_local_eugr_collision_raises():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        local_dir = tmp_path / "local"
        eugr_dir = tmp_path / "eugr"
        local_dir.mkdir()
        eugr_dir.mkdir()

        recipe_yaml = """
recipe_version: "1"
name: dupe-model
hf_path: someorg/dupe-model
gpu_util: 0.7
topologies:
  1_node:
    max_model_len: 4096
    tp_size: 1
    pp_size: 1
    env_vars: []
    vllm_args: "--trust-remote-code"
"""
        local_path = local_dir / "dupe-model.yaml"
        eugr_path = eugr_dir / "dupe-model.yaml"
        local_path.write_text(recipe_yaml)
        eugr_path.write_text(recipe_yaml)

        original_dir = recipes_mod.RECIPES_DIR
        recipes_mod.RECIPES_DIR = tmp_path
        try:
            error_raised = False
            error_message = ""
            try:
                load_recipes(bypass_cache=True)
            except ValueError as exc:
                error_raised = True
                error_message = str(exc)
            assert error_raised, "duplicate name across local/ and eugr/ should raise"
            assert str(local_path) in error_message, f"error should name {local_path}: {error_message}"
            assert str(eugr_path) in error_message, f"error should name {eugr_path}: {error_message}"
            print("PASS: local/eugr name collision raises, naming both paths")
        finally:
            recipes_mod.RECIPES_DIR = original_dir
            load_recipes(bypass_cache=True)


def test_unknown_recipe_version_warns_but_loads():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        local_dir = tmp_path / "local"
        local_dir.mkdir()
        (tmp_path / "eugr").mkdir()

        recipe_yaml = """
recipe_version: "99"
name: future-model
hf_path: someorg/future-model
gpu_util: 0.7
topologies:
  1_node:
    max_model_len: 4096
    tp_size: 1
    pp_size: 1
    env_vars: []
    vllm_args: "--trust-remote-code"
"""
        (local_dir / "future-model.yaml").write_text(recipe_yaml)

        original_dir = recipes_mod.RECIPES_DIR
        recipes_mod.RECIPES_DIR = tmp_path
        try:
            loaded = load_recipes(bypass_cache=True)
            assert "future-model" in loaded, "recipe with unknown version should still load"
            print("PASS: unknown recipe_version loads (warning printed to stderr)")
        finally:
            recipes_mod.RECIPES_DIR = original_dir
            load_recipes(bypass_cache=True)


def test_filename_name_mismatch_raises():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        local_dir = tmp_path / "local"
        local_dir.mkdir()
        (tmp_path / "eugr").mkdir()

        recipe_yaml = """
recipe_version: "1"
name: wrong-name
hf_path: someorg/mismatched
gpu_util: 0.7
topologies:
  1_node:
    max_model_len: 4096
    tp_size: 1
    pp_size: 1
    env_vars: []
    vllm_args: "--trust-remote-code"
"""
        bad_path = local_dir / "actual-filename.yaml"
        bad_path.write_text(recipe_yaml)

        original_dir = recipes_mod.RECIPES_DIR
        recipes_mod.RECIPES_DIR = tmp_path
        try:
            error_raised = False
            error_message = ""
            try:
                load_recipes(bypass_cache=True)
            except ValueError as exc:
                error_raised = True
                error_message = str(exc)
            assert error_raised, "filename/name mismatch should raise"
            assert str(bad_path) in error_message, f"error should name {bad_path}: {error_message}"
            print("PASS: filename/name mismatch raises, naming the file")
        finally:
            recipes_mod.RECIPES_DIR = original_dir
            load_recipes(bypass_cache=True)


def test_offline_flags_both_zero_no_injection():
    cfg = _fresh_cluster_config()
    cfg.global_hf_hub_offline = 0
    cfg.global_transformers_offline = 0

    resp = build_catalog_response()
    assert "error" not in resp, f"unexpected error: {resp.get('error')}"
    models = resp["catalog"]["models"]
    for name, model in models.items():
        for topo_name, topo in model["topologies"].items():
            for env in topo["env_vars"]:
                assert not env.startswith("HF_HUB_OFFLINE="), (
                    f"{name}.{topo_name} should have no injected HF_HUB_OFFLINE with flags off"
                )
                assert not env.startswith("TRANSFORMERS_OFFLINE="), (
                    f"{name}.{topo_name} should have no injected TRANSFORMERS_OFFLINE with flags off"
                )
    print("PASS: both offline flags 0 -> no injected offline env vars")
    config_mod.load_cluster_config.cache_clear()


def test_offline_flags_both_one_inject_exactly_once():
    cfg = _fresh_cluster_config()
    cfg.global_hf_hub_offline = 1
    cfg.global_transformers_offline = 1

    resp = build_catalog_response()
    assert "error" not in resp, f"unexpected error: {resp.get('error')}"
    models = resp["catalog"]["models"]
    checked = 0
    for name, model in models.items():
        for topo_name, topo in model["topologies"].items():
            hf_matches = [e for e in topo["env_vars"] if e.startswith("HF_HUB_OFFLINE=")]
            tf_matches = [e for e in topo["env_vars"] if e.startswith("TRANSFORMERS_OFFLINE=")]
            assert hf_matches == ["HF_HUB_OFFLINE=1"], (
                f"{name}.{topo_name}: expected exactly one HF_HUB_OFFLINE=1, got {hf_matches}"
            )
            assert tf_matches == ["TRANSFORMERS_OFFLINE=1"], (
                f"{name}.{topo_name}: expected exactly one TRANSFORMERS_OFFLINE=1, got {tf_matches}"
            )
            checked += 1
    assert checked == 23, f"expected to check 23 topology combinations, checked {checked}"
    print(f"PASS: both offline flags 1 -> exactly one injected entry each, across {checked} topologies")
    config_mod.load_cluster_config.cache_clear()


def test_offline_flag_filters_existing_entry_not_duplicates():
    """
    A recipe that itself declares HF_HUB_OFFLINE=0 in env_vars must end up
    with only HF_HUB_OFFLINE=1 when the global flag is on -- filter-then-
    append, not append-alongside-the-existing-entry.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        local_dir = tmp_path / "local"
        local_dir.mkdir()
        (tmp_path / "eugr").mkdir()

        recipe_yaml = """
recipe_version: "1"
name: offline-flag-model
hf_path: someorg/offline-flag-model
gpu_util: 0.7
topologies:
  1_node:
    max_model_len: 4096
    tp_size: 1
    pp_size: 1
    env_vars:
      - HF_HUB_OFFLINE=0
      - OTHER_VAR=1
    vllm_args: "--trust-remote-code"
"""
        (local_dir / "offline-flag-model.yaml").write_text(recipe_yaml)

        original_dir = recipes_mod.RECIPES_DIR
        recipes_mod.RECIPES_DIR = tmp_path
        cfg = _fresh_cluster_config()
        cfg.global_hf_hub_offline = 1
        cfg.global_transformers_offline = 0
        try:
            load_recipes(bypass_cache=True)
            resp = build_catalog_response()
            assert "error" not in resp, f"unexpected error: {resp.get('error')}"
            env_vars = resp["catalog"]["models"]["offline-flag-model"]["topologies"]["1_node"]["env_vars"]
            hf_matches = [e for e in env_vars if e.startswith("HF_HUB_OFFLINE=")]
            assert hf_matches == ["HF_HUB_OFFLINE=1"], (
                f"expected the recipe's own HF_HUB_OFFLINE=0 to be filtered out and replaced, "
                f"got {hf_matches}"
            )
            assert "OTHER_VAR=1" in env_vars, "unrelated env_vars entries must survive untouched"
            print("PASS: recipe's own HF_HUB_OFFLINE=0 is filtered then replaced, not duplicated")
        finally:
            recipes_mod.RECIPES_DIR = original_dir
            config_mod.load_cluster_config.cache_clear()
            load_recipes(bypass_cache=True)


def test_cluster_only_is_parsed_but_not_exposed_in_catalog():
    """
    cluster_only is recipe-authoring metadata for future use (see
    common/recipes.py's TopologyConfig docstring) -- it must round-trip
    through load_recipes() so EUGR-synced recipes can set it today, but
    must NOT appear in build_catalog_response()'s output. Once something
    downstream actually reads it, this test's second assertion is the one
    to update -- it's a deliberate tripwire, not an oversight.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        local_dir = tmp_path / "local"
        local_dir.mkdir()
        (tmp_path / "eugr").mkdir()

        recipe_yaml = """
recipe_version: "1"
name: cluster-only-model
hf_path: someorg/cluster-only-model
gpu_util: 0.7
topologies:
  2_node:
    cluster_only: true
    max_model_len: 4096
    tp_size: 1
    pp_size: 2
    env_vars: []
    vllm_args: "--trust-remote-code"
"""
        (local_dir / "cluster-only-model.yaml").write_text(recipe_yaml)

        original_dir = recipes_mod.RECIPES_DIR
        recipes_mod.RECIPES_DIR = tmp_path
        cfg = _fresh_cluster_config()
        try:
            recipes = load_recipes(bypass_cache=True)
            assert recipes["cluster-only-model"].topologies["2_node"].cluster_only is True, (
                "cluster_only should parse through load_recipes()"
            )

            resp = build_catalog_response()
            assert "error" not in resp, f"unexpected error: {resp.get('error')}"
            topo = resp["catalog"]["models"]["cluster-only-model"]["topologies"]["2_node"]
            assert "cluster_only" not in topo, (
                f"cluster_only leaked into the catalog response: {topo.keys()}"
            )
            print("PASS: cluster_only round-trips through load_recipes() but stays out of the catalog response")
        finally:
            recipes_mod.RECIPES_DIR = original_dir
            config_mod.load_cluster_config.cache_clear()
            load_recipes(bypass_cache=True)


if __name__ == "__main__":
    test_load_recipes_count()
    test_topology_count()
    test_local_eugr_collision_raises()
    test_unknown_recipe_version_warns_but_loads()
    test_filename_name_mismatch_raises()
    test_offline_flags_both_zero_no_injection()
    test_offline_flags_both_one_inject_exactly_once()
    test_offline_flag_filters_existing_entry_not_duplicates()
    test_cluster_only_is_parsed_but_not_exposed_in_catalog()
    print("\nAll tests passed.")
