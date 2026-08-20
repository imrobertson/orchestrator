#!/usr/bin/env python3
"""
Translates EUGR-format recipes (eugr-samples/*.yaml, synced verbatim from
imrobertson/spark-vllm-docker-experiments) into our RecipeConfig schema
(recipes/local/*.yaml shape -- see common/recipes.py).

Confirmed against real recipe files, not just the eugr recipes/README.md
spec (see EUGR-REFERENCE-NOTES.md for the original field-by-field review).
This script encodes that review as executable logic instead of prose.

WHY THIS ISN'T A BLIND SYNC
----------------------------
The two schemas represent the same information in structurally different
shapes (flat `defaults:` + a `command:` string template vs. our explicit
per-topology `tp_size`/`pp_size`/`max_model_len`/`vllm_args`). Most of the
translation is mechanical -- direct field renames, or deriving one shape
from the other via fixed rules. A few things are NOT safely automatable and
are deliberately flagged for human review rather than guessed:

  - `container:` values other than the default "vllm-node" are an
    indirection into EUGR's own build pipeline, not a registry ref -- see
    EUGR_CONTAINER_IMAGE_MAP below. An unmapped container name means we
    don't know what image to pull; guessing wrong ships a broken deploy.
  - Any `defaults.*` value substituted into `command:` that isn't a valid
    number where our schema needs one (concretely: `max_model_len: auto`,
    seen in a real file) can't be silently coerced -- we don't know what
    context length "auto" resolves to on their side.
  - `build_args:` (build-time flags for THEIR build-and-copy.sh) has no
    equivalent in our system at all (we pull prebuilt tags, we don't build
    images) -- surfaced as an FYI, never silently dropped without a trace.

Everything else below is real, tested-against-real-files translation, not
a placeholder.

USAGE
-----
    python3 tools/translate_eugr_recipes.py [--in DIR] [--out DIR] [--write]

    --in DIR    Directory of raw EUGR recipe YAMLs (default: eugr-samples/)
    --out DIR   Where translated recipes are written (default:
                recipes/_translated_from_eugr/) -- NEVER recipes/local/
                directly. Translated output always needs a human to read
                the per-file report below and move genuinely-clean results
                into recipes/local/ themselves; see the module docstring
                of common/recipes.py for why silently landing unreviewed
                data in the live catalog is exactly the failure mode we're
                trying to avoid a repeat of.
    --write     Actually write output files. Without this flag, runs in
                report-only mode: shows what WOULD happen, writes nothing.
"""

from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_IN_DIR = REPO_ROOT / "eugr-samples"
DEFAULT_OUT_DIR = REPO_ROOT / "recipes" / "_translated_from_eugr"

# Confirmed real mapping, built from EUGR-REFERENCE-NOTES.md's manual
# confirmation and recipes/local/deepseek-v4-flash.yaml already pointing at
# this same image. "vllm-node" (their default) needs NO entry here -- it
# maps to omitting `image:` entirely, which falls back to
# cluster_config.yaml's default_image, exactly like most of our existing
# recipes/local/*.yaml already do.
#
# Add an entry here whenever a new `container:` value shows up in an EUGR
# recipe you want to translate. This table is the one thing in this script
# that requires a human decision to extend -- see the module docstring.
EUGR_CONTAINER_IMAGE_MAP: dict[str, str] = {
    "vllm-node-b12x": "eugr/spark-vllm-b12x:latest",
}
EUGR_DEFAULT_CONTAINER = "vllm-node"

# Flags our orchestrator's _execute_deployment_impl() already constructs
# itself (see dgx-orchestrator.py) from recipe fields other than
# vllm_args -- these must NOT also appear in the translated vllm_args, or
# the rendered docker run command would carry the same flag twice (the
# exact risk called out in EUGR-REFERENCE-NOTES.md). Matched against the
# rendered command's flag tokens, not the raw template text, so this
# works regardless of how EUGR phrases the substitution.
ORCHESTRATOR_INJECTED_FLAGS = {
    "--host", "--port", "-tp", "--tensor-parallel-size",
    "--pipeline-parallel-size", "--max-model-len",
    "--gpu-memory-utilization", "--model", "--nnodes", "--node-rank",
    "--master-addr", "--master-port", "--headless",
}


class Skip(Exception):
    """Raised to abandon translating one file; caught in main()'s loop."""


def _require(data: dict, key: str, path: Path):
    if key not in data or data[key] in (None, ""):
        raise Skip(f"missing required field '{key}'")
    return data[key]


def _resolve_image(container: str | None, warnings: list[str]) -> str | None:
    if not container or container == EUGR_DEFAULT_CONTAINER:
        return None
    if container in EUGR_CONTAINER_IMAGE_MAP:
        return EUGR_CONTAINER_IMAGE_MAP[container]
    raise Skip(
        f"container '{container}' has no known image mapping -- add it to "
        f"EUGR_CONTAINER_IMAGE_MAP in this script once you know the "
        f"registry ref, then re-run"
    )


def _topologies_to_emit(data: dict) -> list[str]:
    cluster_only = bool(data.get("cluster_only", False))
    solo_only = bool(data.get("solo_only", False))
    if cluster_only and solo_only:
        raise Skip("cluster_only and solo_only are both true -- contradictory, needs human review")
    if cluster_only:
        return ["2_node"]
    if solo_only:
        return ["1_node"]
    return ["1_node", "2_node"]


def _render_command(command_template: str, defaults: dict, warnings: list[str]) -> str:
    """
    str.format(**defaults) is the exact mechanism EUGR's own run-recipe.py
    uses (confirmed in EUGR-REFERENCE-NOTES.md) -- including the `{{` / `}}`
    -> literal brace behavior around JSON blobs, which str.format() already
    does natively. No custom escaping logic needed here; this is not an
    approximation of their templating, it's the same one.
    """
    try:
        return command_template.format(**defaults)
    except KeyError as exc:
        raise Skip(f"command template references undefined default {exc}")


def _extract_vllm_args(rendered_command: str, warnings: list[str]) -> str:
    """
    Strips the `vllm serve <model> \\` prefix and line-continuation
    backslashes, tokenizes what's left, drops every flag (and its value,
    where it takes one) that our orchestrator already constructs itself
    (see ORCHESTRATOR_INJECTED_FLAGS), and rejoins what remains as the
    vllm_args string for our schema.
    """
    text = rendered_command.strip()
    # Line-continuation backslashes and newlines are formatting only.
    text = text.replace("\\\n", " ").replace("\\", " ")

    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        raise Skip(f"could not tokenize rendered command: {exc}")

    # Remove the leading "vllm", "serve", "<model-id>" tokens if present
    # (defensive -- handles the command starting the same line as "vllm serve").
    while tokens and tokens[0] in ("vllm", "serve"):
        tokens.pop(0)
    if tokens and "/" in tokens[0] and not tokens[0].startswith("-"):
        tokens.pop(0)  # the model id, e.g. "deepseek-ai/DeepSeek-V4-Flash-0731"

    out: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        flag = tok.split("=", 1)[0] if tok.startswith("-") else tok
        if flag in ORCHESTRATOR_INJECTED_FLAGS:
            i += 1
            # If the flag didn't carry its value via "=", the next token is
            # the value -- skip it too, unless it's clearly another flag.
            if "=" not in tok and i < len(tokens) and not tokens[i].startswith("-"):
                i += 1
            continue
        out.append(tok)
        i += 1

    return " ".join(shlex.quote(t) for t in out)


def translate_one(path: Path) -> tuple[str, dict, list[str]]:
    """
    Returns (stem, recipe_dict, warnings). Raises Skip with a reason if the
    file can't be safely translated at all.
    """
    warnings: list[str] = []
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise Skip("file is not a YAML mapping at the top level")

    hf_path = _require(data, "model", path)
    defaults = data.get("defaults") or {}
    gpu_util = _require(defaults, "gpu_memory_utilization", path)

    command_template = _require(data, "command", path)

    image = _resolve_image(data.get("container"), warnings)

    if "build_args" in data:
        warnings.append(
            f"source recipe has build_args={data['build_args']!r} -- no equivalent "
            f"in our system (we pull prebuilt images), dropped. FYI only, not blocking."
        )

    mods = list(data.get("mods") or [])

    topo_names = _topologies_to_emit(data)

    topologies: dict = {}
    for topo_name in topo_names:
        topo_defaults = dict(defaults)
        topo_defaults["tensor_parallel"] = 1 if topo_name == "1_node" else int(
            defaults.get("tensor_parallel", 2)
        )

        max_model_len = topo_defaults.get("max_model_len")
        if not isinstance(max_model_len, int):
            raise Skip(
                f"defaults.max_model_len={max_model_len!r} is not an integer "
                f"(topology {topo_name}) -- needs a human to pick the real number, "
                f"not something this script should guess"
            )

        rendered = _render_command(command_template, topo_defaults, warnings)
        vllm_args = _extract_vllm_args(rendered, warnings)

        topo_entry = {
            "max_model_len": max_model_len,
            "tp_size": topo_defaults["tensor_parallel"],
            "pp_size": 1,  # confirmed: EUGR recipes never use pipeline parallelism
            "env_vars": [f"{k}={v}" for k, v in (data.get("env") or {}).items()],
            "vllm_args": vllm_args,
        }
        if topo_name == "2_node" and data.get("cluster_only"):
            topo_entry["cluster_only"] = True

        topologies[topo_name] = topo_entry

    recipe: dict = {
        "recipe_version": "1",
        "hf_path": hf_path,
        "gpu_util": float(gpu_util),
        "capability": {"task": None, "context_class": None, "latency_class": None},
        "mods": mods,
        "topologies": topologies,
    }
    if image is not None:
        recipe["image"] = image

    return path.stem, recipe, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--in", dest="in_dir", type=Path, default=DEFAULT_IN_DIR)
    parser.add_argument("--out", dest="out_dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--write", action="store_true", help="Actually write output files")
    args = parser.parse_args()

    if not args.in_dir.is_dir():
        print(f"error: input directory {args.in_dir} does not exist", file=sys.stderr)
        return 1

    source_files = sorted(args.in_dir.glob("*.yaml"))
    if not source_files:
        print(f"No .yaml files found in {args.in_dir}")
        return 0

    translated = 0
    skipped: list[tuple[Path, str]] = []
    warned: list[tuple[str, list[str]]] = []

    for path in source_files:
        try:
            stem, recipe, warnings = translate_one(path)
        except Skip as exc:
            skipped.append((path, str(exc)))
            continue

        if warnings:
            warned.append((stem, warnings))

        if args.write:
            args.out_dir.mkdir(parents=True, exist_ok=True)
            out_path = args.out_dir / f"{stem}.yaml"
            with out_path.open("w") as f:
                yaml.dump(recipe, f, sort_keys=False, default_flow_style=False, width=88)

        translated += 1
        topo_list = ", ".join(recipe["topologies"].keys())
        print(f"OK      {path.name:45s} -> topologies: [{topo_list}]")

    print()
    if warned:
        print(f"{len(warned)} translated file(s) have non-blocking warnings:")
        for stem, msgs in warned:
            for m in msgs:
                print(f"  [{stem}] {m}")
        print()

    if skipped:
        print(f"{len(skipped)} file(s) SKIPPED (need human review, nothing written):")
        for path, reason in skipped:
            print(f"  {path.name}: {reason}")
        print()

    mode = "WROTE" if args.write else "WOULD WRITE (dry run, pass --write to actually write)"
    print(f"{mode} {translated} recipe(s) to {args.out_dir}")
    print(f"Reminder: {args.out_dir} is a staging area, not recipes/local/ -- "
          f"review each file, THEN move the ones you trust into recipes/local/ "
          f"yourself.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
