#!/usr/bin/env python3
"""
Validate gemma4-26b-a4b-aeon-dflash.yaml against the patched schema, then
reconstruct the docker command _execute_deployment_impl() would emit and
diff its vLLM args against deploy_gemma4_dflash.py's VALIDATED argv.

The point is not "does the YAML parse" -- it's "does this recipe launch
the same thing the validated script launched". Anything in the diff is a
real behavioural difference that needs a reason.
"""
import json
import shlex
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _repo import add_common_to_path, recipe_path
BASE_DIR = add_common_to_path()

# Reuse the pydantic/common stubs from the schema harness.
import verify_entrypoint_schema as harness  # noqa: F401  (installs stubs)

recipes = sys.modules["recipes"]

RECIPE_PATH = recipe_path("gemma4-26b-a4b-aeon-dflash.yaml")

import yaml

raw = yaml.safe_load(RECIPE_PATH.read_text())
print("=" * 68)
print("[1] YAML parse")
print(f"  entrypoint repr : {raw['entrypoint']!r}")
print(f"  == ''           : {raw['entrypoint'] == ''}")
print(f"  is None         : {raw['entrypoint'] is None}")
print(f"  gpu_util        : {raw['gpu_util']}")
print(f"  topologies      : {list(raw['topologies'])}")

# Build through the model (stubbed pydantic -- shape check, not coercion).
topo_raw = raw["topologies"]["1_node"]
recipe = recipes.RecipeConfig(
    recipe_version=raw["recipe_version"],
    hf_path=raw["hf_path"],
    image=raw["image"],
    entrypoint=raw["entrypoint"],
    gpu_util=raw["gpu_util"],
    notes=raw.get("notes"),
    mods=raw.get("mods", []),
    topologies={"1_node": recipes.TopologyConfig(**topo_raw)},
)

print("\n[2] config hash")
h = recipes.compute_config_hash(recipe, "1_node")
payload = recipes.build_config_payload(recipe, "1_node")
print(f"  config_hash     : {h}")
print(f"  payload schema  : {payload['_schema']}")
print(f"  payload.entrypoint: {payload['entrypoint']!r}")

# Prove entrypoint participates: same recipe without it must hash differently.
recipe_no_ep = recipes.RecipeConfig(
    recipe_version=raw["recipe_version"], hf_path=raw["hf_path"],
    image=raw["image"], gpu_util=raw["gpu_util"], mods=[],
    topologies={"1_node": recipes.TopologyConfig(**topo_raw)},
)
h_no_ep = recipes.compute_config_hash(recipe_no_ep, "1_node")
print(f"  same recipe w/o entrypoint: {h_no_ep}")
print(f"  differs         : {h != h_no_ep}")

print("\n[3] reconstruct the orchestrator's container argv")
gpu_util = recipe.gpu_util
max_model_len = topo_raw["max_model_len"]
vllm_args_list = shlex.split(topo_raw["vllm_args"])

container_args = [
    "python3", "-m", "vllm.entrypoints.openai.api_server",
    "--model", recipe.hf_path,
    "--gpu-memory-utilization", str(gpu_util),
    "--max-model-len", str(max_model_len),
] + vllm_args_list

entrypoint_flag = [] if recipe.entrypoint is None else ["--entrypoint", recipe.entrypoint]
docker_cmd = [
    "docker", "run", "-d", "--init",
    "--name", "vllm-standalone",
    "--net=host", "--ipc=host", "--shm-size=16gb",
    "--gpus", "all",
] + entrypoint_flag + [recipe.image] + container_args

print("  " + " ".join(shlex.quote(a) for a in docker_cmd[:14]) + " \\")
print("      ... " + " ".join(shlex.quote(a) for a in docker_cmd[14:20]) + " ...")
print(f"\n  --entrypoint present: {'--entrypoint' in docker_cmd}")
idx = docker_cmd.index("--entrypoint")
print(f"  --entrypoint value  : {docker_cmd[idx+1]!r}")
print(f"  image immediately follows: {docker_cmd[idx+2] == recipe.image}")

print("\n[4] diff vLLM args vs the VALIDATED standalone script")

# From deploy_gemma4_dflash.py, minus `serve <path>` (the script's own
# entrypoint convention) and minus --host/--port (see recipe comment).
validated_spec = json.dumps({
    "method": "dflash",
    "model": "z-lab/gemma-4-26B-A4B-it-DFlash",
    "num_speculative_tokens": 10,
    "attention_backend": "flash_attn",
})
validated = {
    "--served-model-name": "gemma4-aeon-uncensored",
    "--tensor-parallel-size": "1",
    "--dtype": "auto",
    "--quantization": "compressed-tensors",
    "--linear-backend": "flashinfer_cutlass",
    "--moe-backend": "cutlass",
    "--attention-backend": "triton_attn",
    "--max-model-len": "32768",
    "--max-num-seqs": "32",
    "--max-num-batched-tokens": "16384",
    "--gpu-memory-utilization": "0.65",
    "--enable-chunked-prefill": None,
    "--enable-prefix-caching": None,
    "--trust-remote-code": None,
    "--enable-auto-tool-choice": None,
    "--tool-call-parser": "gemma4",
    "--reasoning-parser": "gemma4",
    "--speculative-config": validated_spec,
}

# Parse what the recipe actually produces (skip the module prefix).
produced = {}
toks = container_args[3:]
i = 0
while i < len(toks):
    t = toks[i]
    if t.startswith("--"):
        if i + 1 < len(toks) and not toks[i + 1].startswith("--"):
            produced[t] = toks[i + 1]
            i += 2
        else:
            produced[t] = None
            i += 1
    else:
        i += 1

ok = True
for flag, val in sorted(validated.items()):
    got = produced.get(flag, "<MISSING>")
    if flag == "--speculative-config":
        match = got not in ("<MISSING>", None) and json.loads(got) == json.loads(val)
    else:
        match = (got == val)
    if not match:
        ok = False
        print(f"  MISMATCH {flag}: validated={val!r} recipe={got!r}")

extra = set(produced) - set(validated) - {"--model"}
for flag in sorted(extra):
    print(f"  EXTRA in recipe (not in validated script): {flag}={produced[flag]!r}")
    ok = False

if ok and not extra:
    print("  every validated flag reproduced exactly, no extras")

print("\n[5] spec-config survived YAML + shlex intact")
sc = produced.get("--speculative-config")
parsed = json.loads(sc)
print(f"  parsed: {parsed}")
print(f"  method=dflash          : {parsed['method'] == 'dflash'}")
print(f"  num_speculative_tokens : {parsed['num_speculative_tokens']} (validated: 10)")
print(f"  drafter                : {parsed['model']}")

print("\n" + "=" * 68)
print("OK" if ok else "DIFFERENCES FOUND -- read the diff above")
sys.exit(0 if ok else 1)
