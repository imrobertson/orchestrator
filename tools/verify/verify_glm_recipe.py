#!/usr/bin/env python3
"""
Reconstruct the docker commands _execute_deployment_impl() would emit for
the patched _glm-5_3-flash-nvfp4-tp2.yaml, on both target hosts, and check
each fix this recipe now depends on is actually present in the argv that
would reach the wire -- not just present in the YAML.
"""
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _repo import add_common_to_path, recipe_path
BASE_DIR = add_common_to_path()

import verify_entrypoint_schema  # noqa: F401 (installs stubs)
import yaml

recipes = sys.modules["recipes"]
# Renamed 2026-09-09: was _glm-5.3-flash-nvfp4-tp2 (underscore prefix,
# dotted version, "tp2"). See DIRECTION.md on the dot/underscore convention.
raw = yaml.safe_load(recipe_path("glm-5_3-flash-nvfp4-mtp.yaml").read_text())
topo_raw = raw["topologies"]["2_node"]

recipe = recipes.RecipeConfig(
    recipe_version=raw["recipe_version"], hf_path=raw["hf_path"], image=raw["image"],
    entrypoint=raw["entrypoint"], model_path_override=raw["model_path_override"],
    extra_mounts=raw["extra_mounts"], gpu_util=raw["gpu_util"], mods=raw.get("mods", []),
    topologies={"2_node": recipes.TopologyConfig(**topo_raw)},
)

vllm_args_list = shlex.split(topo_raw["vllm_args"])
use_ray = ("--distributed-executor-backend" in vllm_args_list) and ("ray" in vllm_args_list)
model_effective = recipe.model_path_override or recipe.hf_path
entrypoint_flag = [] if recipe.entrypoint is None else ["--entrypoint", recipe.entrypoint]
extra_mount_flags = []
for m in recipe.extra_mounts:
    extra_mount_flags += ["-v", m]

print("=" * 70)
print("[1] use_ray computed from vllm_args")
print(f"  use_ray = {use_ray}   (must be False)")
assert use_ray is False, "ray flag still present -- fix did not take"

print("\n[2] --model value vs hf_path (identity must stay separate)")
print(f"  hf_path (identity)      : {recipe.hf_path}")
print(f"  model_effective (--model): {model_effective}")
assert model_effective != recipe.hf_path
assert model_effective == "/models/GLM-5.3-Flash-NVFP4"

# The constraint that catalog key resolution depends on. Without this,
# _discover_host_container() -> _resolve_catalog_key() returns a string
# that is not a catalog key, and the ledger/session tracker key on a
# fabricated name whenever ACTIVE_DEPLOYMENT_STATE's exact record is
# absent. Case-sensitive on purpose -- that is how the matcher compares.
hf_basename = recipe.hf_path.split("/")[-1]
path_basename = model_effective.split("/")[-1]
print(f"  hf_path basename        : {hf_basename}")
print(f"  container path basename : {path_basename}")
catalog_match = (recipe.hf_path.endswith(path_basename)
                 or path_basename in recipe.hf_path)
print(f"  resolves to catalog entry: {catalog_match}")
assert path_basename == hf_basename, (
    f"container path basename {path_basename!r} != hf_path basename "
    f"{hf_basename!r} -- catalog key resolution will silently fall back "
    f"to a non-catalog key. See RecipeConfig.model_path_override.")
assert catalog_match

HEAD, WORKER = "spark-3", "spark-4"
HEAD_IP, MASTER_PORT = "10.0.14.41", "29500"

for host, rank in [(HEAD, 0), (WORKER, 1)]:
    container_args = [
        "python3", "-m", "vllm.entrypoints.openai.api_server",
        "--model", model_effective,
        "--tensor-parallel-size", str(topo_raw["tp_size"]),
        "--pipeline-parallel-size", str(topo_raw["pp_size"]),
        "--nnodes", "2",
        "--node-rank", str(rank),
        "--master-addr", HEAD_IP,
        "--master-port", MASTER_PORT,
        "--gpu-memory-utilization", str(recipe.gpu_util),
        "--max-model-len", str(topo_raw["max_model_len"]),
    ]
    if rank > 0:
        container_args.append("--headless")
    container_args.extend(vllm_args_list)

    docker_cmd = [
        "docker", "run", "-d", "--init", "--name", "vllm-head" if rank == 0 else "vllm-worker",
        "--net=host", "--ipc=host", "--shm-size=64gb", "--privileged",
        "--cap-add", "IPC_LOCK", "--device", "/dev/infiniband:/dev/infiniband",
        "--gpus", "all",
        "-v", "/home/tetrel/.cache/huggingface:/root/.cache/huggingface",
    ] + extra_mount_flags + entrypoint_flag + [recipe.image] + container_args

    print(f"\n[3] {host} (rank {rank}) reconstructed docker run")
    print("  " + " \\\n    ".join(shlex.quote(a) for a in docker_cmd))

    assert "--entrypoint" in docker_cmd and docker_cmd[docker_cmd.index("--entrypoint") + 1] == ""
    assert "-v" in docker_cmd
    mount_idx = docker_cmd.index("/var/tmp/glm-5.3-flash-nvfp4:/models/GLM-5.3-Flash-NVFP4")
    assert docker_cmd[mount_idx - 1] == "-v"
    assert "ray" not in docker_cmd
    assert "--block" not in docker_cmd  # the flag that prefix-matched to --block-size
    assert model_effective in docker_cmd
    assert recipe.hf_path not in docker_cmd  # identity string must NOT leak into argv

print("\n" + "=" * 70)
print("ALL ASSERTIONS PASSED")
print("""
What this confirms:
  - no 'ray'/'--block' tokens reach argv -> the argparse prefix-match
    crash (`vllm serve ray start ... --block` -> --block-size) cannot recur
  - --entrypoint '' is present and precedes the image -> ENTRYPOINT
    ["vllm","serve"] is neutralized, our argv runs verbatim
  - the staged local path is bind-mounted AND passed as --model, while
    hf_path itself never appears in argv -> Glm5NextProcessor.from_pretrained()
    gets a real directory, not a repo id it can't open()

What this does NOT confirm (needs the actual hardware):
  - that /var/tmp/glm-5.3-flash-nvfp4 has actually been populated on both
    hosts via `huggingface-cli download` -- an empty/missing directory at
    that mount point fails differently (ENOENT inside the container) but
    just as fatally
  - that this image's vLLM build actually supports --nnodes/--node-rank
    the same way upstream's launcher exercises it -- inferred from the
    sibling repo's stated approach, not observed on this image directly
""")
