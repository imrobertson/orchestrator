#!/usr/bin/env python3
"""
TETREL SECURITY - CLUSTER ASSET PRE-FETCH & CACHE UTILITY (VERBOSE)
--------------------------------------------------------------------------------
Pre-pulls every Docker image and HuggingFace checkpoint the live recipe
catalog references onto each configured host, with human-readable file
manifests and real-time progress. Handles token fallbacks safely.

Work is SERIAL by design -- one image or repo on one host at a time -- so a
cold cache takes hours, not minutes. Timeouts are 1800s per image pull and
3600s per checkpoint.

KNOWN GAP: recipes using `model_path_override` / `extra_mounts` to stage
weights outside the shared HF cache are not covered here. Stage those by
hand per the recipe's own instructions. See WORKSTREAMS.md WS-3.
"""

import json
import re
import shlex
import sys

from common.config import legacy_hosts_dict, load_cluster_config
from common.recipes import build_catalog_response
from common.ssh import get_hf_token, run_ssh

HOSTS = legacy_hosts_dict()

DEFAULT_IMAGE_FALLBACK = "nvcr.io/nvidia/vllm:26.07-py3"


def _speculative_config_blobs(vllm_args: str) -> list[str]:
    """
    Return the raw JSON string(s) passed to --speculative-config.

    shlex.split is what unwraps the single-quoting these blobs always carry
    (E011 requires vllm_args be a YAML block scalar precisely so the inner
    double quotes survive). If the args are malformed enough that shlex
    refuses, the caller falls back to a regex rather than dropping the
    recipe silently.
    """
    try:
        tokens = shlex.split(vllm_args)
    except ValueError:
        return []

    blobs = []
    for i, tok in enumerate(tokens):
        if tok == "--speculative-config" and i + 1 < len(tokens):
            blobs.append(tokens[i + 1])
        elif tok.startswith("--speculative-config="):
            blobs.append(tok.split("=", 1)[1])
    return blobs


def _draft_model_repos(vllm_args: str, model_key: str) -> list[str]:
    """
    Extract every speculative-decoding draft checkpoint a topology needs.

    TWO forms, and only handling the first is why offline deploys of every
    working spec-decode recipe used to fail at load:

      1. `--speculative-model <repo>` -- a separate flag. Rejected by the
         current builds (see errata E023); present only in recipes that
         cannot launch. Still parsed, because a recipe carrying it is
         exactly the one someone is about to fix.
      2. `--speculative-config '{"method": ..., "model": <repo>, ...}'` --
         the drafter inside the JSON. This is what every working recipe in
         the catalog uses: DFlash, DSpark and MTP drafters all arrive here.

    Returns [] for recipes with no drafter, which is most of them.
    """
    repos = []

    m = re.search(r"--speculative-model\s+(\S+)", vllm_args)
    if m:
        repos.append(m.group(1))

    blobs = _speculative_config_blobs(vllm_args)

    if not blobs and "--speculative-config" in vllm_args:
        # shlex could not tokenize the args. Do not fail closed and do not
        # fail silently -- scan for the model key directly and say so.
        print(
            f"[!] {model_key}: could not tokenize vllm_args to read "
            f"--speculative-config; falling back to a raw scan for its "
            f"draft model. Verify the drafter cached before going offline."
        )
        for m in re.finditer(r'"model"\s*:\s*"([^"]+)"', vllm_args):
            repos.append(m.group(1))
        return repos

    for blob in blobs:
        try:
            cfg = json.loads(blob)
        except (ValueError, TypeError):
            print(
                f"[!] {model_key}: --speculative-config is not valid JSON; "
                f"its draft model will NOT be pre-fetched. Stage it by hand."
            )
            continue
        if not isinstance(cfg, dict):
            continue
        draft = cfg.get("model")
        if isinstance(draft, str) and draft:
            repos.append(draft)

    return repos


def extract_manifest() -> tuple[set, dict]:
    """
    Read the live recipe catalog and return (all_images, repo_to_image).

    repo_to_image maps each HF repo to the SPECIFIC image it should be
    downloaded through, rather than one globally-guessed base image for
    every repo. Recipes override `image:` individually, and a shared image
    risks missing huggingface_hub or using a mismatched Python/CUDA env for
    that repo.

    Draft checkpoints ride along with their parent model's image, since the
    same process loads both.
    """
    resp = build_catalog_response()
    if "error" in resp:
        sys.exit(f"[-] Error loading recipe catalog: {resp['error']}")

    config = resp["catalog"]

    default_img = config.get("default_image", DEFAULT_IMAGE_FALLBACK)
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
            vllm_args = topo_data.get("vllm_args", "") or ""
            for draft_repo in _draft_model_repos(vllm_args, m_name):
                repo_to_image.setdefault(draft_repo, model_image)

    return images, repo_to_image


def prefetch_docker_images(images: set):
    print("\n" + "=" * 80)
    print("STAGE 1: PRE-PULLING DOCKER CONTAINER IMAGES")
    print("=" * 80)

    for img in sorted(images):
        for host, meta in HOSTS.items():
            print(f"\n[+] Pulling container image '{img}' on {host} ({meta['ip']})...")
            # user=None lets common/ssh.py resolve ssh_user from
            # cluster_config.yaml, matching every run_ssh call site in
            # dgx-orchestrator.py. Do not hardcode an account here.
            res = run_ssh(meta["ip"], None, ["docker", "pull", img],
                          timeout=1800, tty=True, capture=False, connect_timeout=10)
            if res.returncode == 0:
                print(f"[✓] Successfully pulled '{img}' on {host}")
            else:
                print(f"[-] Failed to pull '{img}' on {host}")


def prefetch_hf_models(repo_to_image: dict):
    print("\n" + "=" * 80)
    print("STAGE 2: PRE-FETCHING HUGGINGFACE MODEL CHECKPOINTS & TOKENIZERS")
    print("=" * 80)

    hf_token = get_hf_token()
    cluster = load_cluster_config()

    # Repo id comes in via REPO_ID env var rather than being interpolated
    # directly into the embedded Python source. hf_path values are
    # admin-controlled (they come from recipes, not end-user input), but
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
            # Per-host mount from cluster_config.yaml, same source
            # _execute_deployment_impl() uses. Hosts are not required to
            # share a home directory.
            try:
                vol_mount = cluster.hosts[host].volume_mount
            except (KeyError, AttributeError):
                print(f"[-] No volume_mount configured for {host}; skipping {repo} there.")
                continue

            print(f"\n[+] Processing Checkpoint: {repo} on {host} ({meta['ip']}) via image '{image_for_repo}'")

            env_flags = ["-e", "PYTHONUNBUFFERED=1", "-e", f"REPO_ID={repo}"]
            if hf_token:
                env_flags.extend(["-e", f"HF_TOKEN={hf_token}"])

            # -t gives the download an interactive TTY for progress rendering
            docker_cmd = [
                "docker", "run", "--rm", "-t",
                "-v", vol_mount
            ] + env_flags + [
                image_for_repo,
                "python3", "-c", py_download_script
            ]

            res = run_ssh(meta["ip"], None, docker_cmd,
                          timeout=3600, tty=True, capture=False, connect_timeout=10)
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

    print("\n" + "=" * 80)
    print("[✓] ALL CLUSTER ASSETS CACHED LOCALLY!")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    main()
