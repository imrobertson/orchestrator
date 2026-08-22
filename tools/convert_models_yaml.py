#!/usr/bin/env python3
"""
One-shot conversion: models.yaml -> recipes/local/<model-name>.yaml.

Run once as part of the Phase 2 migration:

    python3 tools/convert_models_yaml.py

Deliberately does NOT touch models.yaml (still the live source for
dgx-orchestrator.py / cache_cluster_assets.py until Task 2C) and does not
import common/recipes.py, so this script has zero dependency on the new
loader actually being correct -- it's a dumb, auditable transcription.

Per-model recipes carry only what varies per model. `capability` and `mods`
are emitted empty/null on every recipe (unused until later phases -- see
"A note on scope" in docs/PHASE-2-PROMPTS.md). default_image and the two
GLOBAL_* offline flags are cluster-wide and are NOT copied into recipes;
they now live in cluster_config.yaml.

Recipes carry no `name:` field -- the filename stem is the catalog key.
An earlier version of this script emitted `name: <model>` into every
recipe, which required it to always match the filename; common/recipes.py
no longer has anywhere to put that field, so it isn't emitted here either.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent
MODELS_YAML_PATH = BASE_DIR / "models.yaml"
RECIPES_LOCAL_DIR = BASE_DIR / "recipes" / "local"

RECIPE_VERSION = "1"

# Long vllm_args strings (the common case) get dumped as a folded block
# scalar (`>-`) so they stay readable across multiple lines instead of
# collapsing into one very long line. Short ones are left as a plain
# (quoted) scalar, matching how the shorter entries already look in
# models.yaml.
_FOLD_THRESHOLD = 60


class _FoldedStr(str):
    """Marker type: dump this string with block-folded ('>') style."""


def _represent_folded_str(dumper: yaml.Dumper, data: _FoldedStr):
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style=">")


yaml.add_representer(_FoldedStr, _represent_folded_str, Dumper=yaml.SafeDumper)


def _vllm_args_value(raw: str):
    return _FoldedStr(raw) if len(raw) > _FOLD_THRESHOLD else raw


def build_recipe_dict(name: str, model_data: dict) -> dict:
    # `name` is used only to look up model_data and to compute the output
    # filename in main() below -- it is deliberately NOT written into the
    # recipe dict. The filename stem is the catalog key; see the module
    # docstring.
    recipe: dict = {
        "recipe_version": RECIPE_VERSION,
        "hf_path": model_data["hf_path"],
    }
    if "image" in model_data:
        recipe["image"] = model_data["image"]
    recipe["gpu_util"] = model_data["gpu_util"]
    recipe["capability"] = {"task": None, "context_class": None, "latency_class": None}
    recipe["mods"] = []

    topologies_out: dict = {}
    for topo_name, topo_data in (model_data.get("topologies") or {}).items():
        topologies_out[topo_name] = {
            "max_model_len": topo_data["max_model_len"],
            "tp_size": topo_data["tp_size"],
            "pp_size": topo_data["pp_size"],
            "env_vars": list(topo_data.get("env_vars", [])),
            "vllm_args": _vllm_args_value(topo_data.get("vllm_args", "")),
        }
    recipe["topologies"] = topologies_out

    return recipe


def main() -> int:
    if not MODELS_YAML_PATH.exists():
        print(f"error: {MODELS_YAML_PATH} not found", file=sys.stderr)
        return 1

    raw = yaml.safe_load(MODELS_YAML_PATH.read_text()) or {}
    models = raw.get("models", {})
    if not isinstance(models, dict):
        print("error: models.yaml has no top-level 'models' mapping", file=sys.stderr)
        return 1

    RECIPES_LOCAL_DIR.mkdir(parents=True, exist_ok=True)

    converted: list[str] = []
    skipped: list[tuple[str, str]] = []
    topo_counts: dict[str, int] = {}

    for name, model_data in models.items():
        if not isinstance(model_data, dict):
            skipped.append((name, f"entry is not a mapping ({type(model_data).__name__})"))
            continue
        if "hf_path" not in model_data or "gpu_util" not in model_data:
            skipped.append((name, "missing required hf_path/gpu_util"))
            continue

        recipe_dict = build_recipe_dict(name, model_data)
        out_path = RECIPES_LOCAL_DIR / f"{name}.yaml"

        with out_path.open("w") as f:
            yaml.dump(
                recipe_dict,
                f,
                Dumper=yaml.SafeDumper,
                sort_keys=False,
                default_flow_style=False,
                width=88,
            )

        converted.append(name)
        for topo_name in recipe_dict["topologies"]:
            topo_counts[topo_name] = topo_counts.get(topo_name, 0) + 1

    print(f"Converted {len(converted)} model(s) to {RECIPES_LOCAL_DIR}/")
    total_topologies = sum(topo_counts.values())
    for topo_name in sorted(topo_counts):
        print(f"  {topo_name}: {topo_counts[topo_name]}")
    print(f"  total topology combinations: {total_topologies}")

    if skipped:
        print(f"\nSkipped {len(skipped)} entr(y/ies):")
        for name, reason in skipped:
            print(f"  {name}: {reason}")

    print("\nModels converted:")
    for name in converted:
        topos = ", ".join(sorted(build_recipe_dict(name, models[name])["topologies"]))
        print(f"  {name}: [{topos}]")

    return 0


if __name__ == "__main__":
    sys.exit(main())
