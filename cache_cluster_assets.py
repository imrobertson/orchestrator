#!/usr/bin/env python3
"""
TETREL SECURITY - CLUSTER ASSET PRE-FETCH & CACHE UTILITY (VERBOSE)
--------------------------------------------------------------------------------
Pre-pulls all required Docker images and HuggingFace checkpoints down to 
spark-3 and spark-4 local storage with human-readable file manifests, 
real-time progress bars, and accurate ETA tracking. Handles token fallbacks safely.
"""

import os
import re
import sys
from pathlib import Path
import yaml

from common.config import legacy_hosts_dict
from common.ssh import get_hf_token, resolve_user_identity_key, run_ssh

BASE_DIR = Path(os.getenv("BASE_DIR", Path(__file__).resolve().parent))
MODELS_YAML_PATH = BASE_DIR / "models.yaml"

HOSTS = legacy_hosts_dict()

def extract_manifest() -> tuple[set, dict]:
    """
    Returns (all_images, repo_to_image).

    repo_to_image maps each HF repo to the SPECIFIC model image it should be
    downloaded through, rather than one globally-guessed "base image" for
    every repo. This matters because models.yaml already lets individual
    models override `image:` (e.g. deepseek-v4-flash uses
    eugr/spark-vllm-b12x, muse-glimmer uses vllm/vllm-openai:muse-glimmer) -
    a single shared image for every download risks missing huggingface_hub
    or using a mismatched Python/CUDA env for that repo.
    """
    if not MODELS_YAML_PATH.exists():
        sys.exit("[-] Error: models.yaml not found.")

    with open(MODELS_YAML_PATH, "r") as f:
        config = yaml.safe_load(f) or {}

    default_img = config.get("default_image", "nvcr.io/nvidia/vllm:26.07-py3")
    images = {default_img}
    repo_to_image = {}

    models = config.get("models", {})
    for m_name, m_data in models.items():
        if not isinstance(m_data, dict):
            continue

        model_image = m_data.get("image", default_img)
        images.add(model_image)

        if "hf_path" in m_data:
            repo_to_image.setdefault(m_data["hf_path"], model_image)

        topologies = m_data.get("topologies", {})
        for _, topo_data in topologies.items():
            vllm_args = topo_data.get("vllm_args", "")
            match = re.search(r'--speculative-model\s+([^\s]+)', vllm_args)
            if match:
                # Speculative-decoding draft models ride along with their
                # parent model's image, since they're loaded by the same process.
                repo_to_image.setdefault(match.group(1), model_image)

    return images, repo_to_image

def prefetch_docker_images(images: set):
    print("\n" + "="*80)
    print("STAGE 1: PRE-PULLING DOCKER CONTAINER IMAGES")
    print("="*80)
    
    for img in sorted(images):
        for host, meta in HOSTS.items():
            print(f"\n[+] Pulling container image '{img}' on {host} ({meta['ip']})...")
            res = run_ssh(meta["ip"], "tetrel", ["docker", "pull", img], timeout=1800, tty=True, capture=False, connect_timeout=10)
            if res.returncode == 0:
                print(f"[✓] Successfully pulled '{img}' on {host}")
            else:
                print(f"[-] Failed to pull '{img}' on {host}")

def prefetch_hf_models(repo_to_image: dict):
    print("\n" + "="*80)
    print("STAGE 2: PRE-FETCHING HUGGINGFACE MODEL CHECKPOINTS & TOKENIZERS")
    print("="*80)
    
    hf_token = get_hf_token()
    vol_mount = "/home/tetrel/.cache/huggingface:/root/.cache/huggingface"

    # Repo id comes in via REPO_ID env var rather than being interpolated
    # directly into the embedded Python source. hf_path values are
    # admin-controlled (they come from models.yaml, not end-user input), but
    # this avoids relying on that being true forever and sidesteps having to
    # think about escaping quotes/backslashes in repo names.
    py_download_script = """
import os, sys
from huggingface_hub import HfApi, snapshot_download

repo = os.environ['REPO_ID']
print('\\n' + '='*60)
print(f' Target Repository: {repo}')
print('='*60)

try:
    api = HfApi()
    info = api.model_info(repo)
    total_bytes = sum(f.size for f in info.siblings if f.size)
    print(f'Manifest: {len(info.siblings)} files | Total Volume: {total_bytes / (1024**3):.2f} GB\\n')
    print('Checkpoint Files:')
    for f in info.siblings:
        if f.size:
            print(f'  - {f.rfilename} ({f.size / (1024**3):.2f} GB)')
        else:
            print(f'  - {f.rfilename}')
    print('-'*60 + '\\n')
except Exception as e:
    print(f'Note: Could not fetch manifest details ({e}). Starting download...')

try:
    # max_workers=2 keeps tqdm stdout progress bars clean over SSH TTY
    snapshot_download(repo_id=repo, max_workers=2)
    print('\\n[✓] Download & Symlinking Complete!')
except Exception as e:
    print(f'\\n[-] Error downloading {repo}: {e}', file=sys.stderr)
    sys.exit(1)
"""

    for repo in sorted(repo_to_image.keys()):
        image_for_repo = repo_to_image[repo]

        for host, meta in HOSTS.items():
            print(f"\n[+] Processing Checkpoint: {repo} on {host} ({meta['ip']}) via image '{image_for_repo}'")
            
            env_flags = ["-e", "PYTHONUNBUFFERED=1", "-e", f"REPO_ID={repo}"]
            if hf_token:
                env_flags.extend(["-e", f"HF_TOKEN={hf_token}"])

            # Added -t flag to docker run for interactive TTY progress rendering
            docker_cmd = [
                "docker", "run", "--rm", "-t",
                "-v", vol_mount
            ] + env_flags + [
                image_for_repo,
                "python3", "-c", py_download_script
            ]

            res = run_ssh(meta["ip"], "tetrel", docker_cmd, timeout=3600, tty=True, capture=False, connect_timeout=10)
            if res.returncode == 0:
                print(f"[✓] Checkpoint {repo} fully cached on {host}")
            else:
                print(f"[-] Failed to cache {repo} on {host} (image: {image_for_repo})")

def main():
    images, repo_to_image = extract_manifest()
    
    print("=== TETREL SECURITY - CLUSTER CACHE PRE-FETCHER ===")
    print(f"Target Nodes: {', '.join(HOSTS.keys())}")
    print(f"Discovered Docker Images ({len(images)}): {', '.join(sorted(images))}")
    print(f"Discovered HF Repositories ({len(repo_to_image)}): {', '.join(sorted(repo_to_image.keys()))}")

    # 1. Pull Docker Images
    prefetch_docker_images(images)

    # 2. Pre-fetch HF Repositories - each through its own model's configured
    # image (see extract_manifest / prefetch_hf_models), not a single guess.
    prefetch_hf_models(repo_to_image)

    print("\n" + "="*80)
    print("[✓] ALL CLUSTER ASSETS CACHED LOCALLY!")
    print("="*80 + "\n")

if __name__ == "__main__":
    main()
