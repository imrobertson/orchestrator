#!/usr/bin/env python3
"""
deploy_gemma4_dflash.py -- standalone deploy for AEON-7's Gemma 4 26B-A4B
DFlash pipeline.

    docker exec -it dgx-orchestrator-api python3 deploy_gemma4_dflash.py

This is NOT a recipes/local/*.yaml recipe, deliberately. AEON's image
ships `ENTRYPOINT bash` and needs `--entrypoint vllm ... serve <path>` to
actually run vLLM. _execute_deployment_impl() always runs
`python3 -m vllm.entrypoints.openai.api_server` against an image's
DEFAULT entrypoint, and the recipe schema has no field to override that.
A recipe YAML for this would parse fine, get cataloged, show up in the
dashboard as a normal deployable model, and then silently misbehave the
moment someone actually deployed it -- `bash python3 -m vllm.entrypoints...`
does nothing useful. So this deploys directly over SSH instead, using the
exact docker run shape AEON's own model card documents (adapted to
reference the HF repo id rather than a local pre-download + bind mount,
matching how every other model on this cluster is already handled via the
shared HF cache mount -- no reason to special-case this one).

It uses the literal `vllm-standalone` container name (ContainerRole.
STANDALONE), so `dgx-config teardown --host <node>` / the dashboard
teardown button still find and remove it correctly -- but it does NOT get
ACTIVE_DEPLOYMENT_STATE tracking or dashboard visibility the way a real
recipe-driven deploy does, since it never goes through
_execute_deployment_impl() at all.

That last point now cuts deeper than it used to. Because this script
never reaches _execute_deployment_impl(), the orchestrator's own
reserved-host guard never sees it either -- a raw `docker run` over SSH
is invisible to a check that lives in execute_deployment(). The
RESERVED_HOSTS check in main() below is therefore the ONLY thing standing
between this script and clobbering a reserved host, which is why it runs
before the pull rather than being left to any deploy path. There is
deliberately no --force here: forcing work onto a reserved host should be
an explicit act at the orchestrator, not a flag that ends up saved in
someone's shell history for a one-off deploy script.

VALIDATED PERFORMANCE (2026-09-01, measured with what was then
tests/metest.py --stage dflash --repeats 4 -- that script has since been
generalized and renamed to tests/ab_test.py, where the equivalent is
`--variant-a gemma4-dflash --repeats 4`; metest.py no longer exists, pinned to the exact image/config this script now defaults
to -- 4 independent fresh deploys per prompt, not repeated calls against
one running container):

    prompt       mean warm tok/s   range     vs. mtp (also n=4, same prompts)
    coding       103.4             0.8       mtp: 67.3  -- dflash +54%
    extraction   202.8             1.2       mtp: 72.9  -- dflash +178%
    creative      54.2             0.6       mtp: 54.6  -- essentially tied
    default       49.3             0.7       mtp: ~52 (n=1 only) -- roughly tied

Every dflash range above is under 1.3 tok/s across 4 independent boots --
this is not a noisy or lucky result, confirmed four times over. But it's
also genuinely workload-dependent, not a flat speedup: dflash is the
clear, decisive choice for coding/extraction-style workloads, and simply
ties mtp for general prose. If the actual use case is closer to
"creative"/general-purpose than "coding"/"extraction", the mtp recipe
(recipes/local/gemma4-26b-a4b-nvfp4.yaml) is the better default -- same
speed, official NVIDIA weights, no content-moderation caveat, and it
deploys through the normal recipe/dashboard path this script can't use.

The extraction number (202.8, above even AEON's own published ceiling of
"up to 158") is real and reproducible, not a fluke -- but the LEADING
hypothesis for why it's this high, not a confirmed root cause, is that
this specific synthetic extraction prompt produces unusually short,
highly predictable JSON output, which is close to a best case for
speculative decoding's acceptance rate. Worth a harder/longer real-world
extraction prompt before treating 202.8 as representative of extraction
workloads generally, rather than of this one prompt specifically.

Four things worth knowing before running this:

  1. gpu_util defaults to 0.65, not this cluster's usual 0.75/0.85 --
     validated at exactly this value across all 16 runs above (4 repeats
     x 4 prompts). AEON-7's own production notes for this exact model on
     this exact hardware (GB10's shared LPDDR5X pool) say "above ~0.8 the
     shared pool page-thrashes and stalls the box, and even 0.85 stalls."
     --gpu-util overrides this if you want to push past it anyway.

  2. This is NOT NVIDIA's official checkpoint. AEON-7/Gemma-4-26B-A4B-it-
     Uncensored-NVFP4 is quantized from an "uncensored" finetune
     (TrevorJS/gemma-4-26B-A4B-it-uncensored) using compressed-tensors
     NVFP4 (llmcompressor), a different format from the ModelOpt NVFP4
     the baseline/mtp stages use, with its own content-moderation
     posture worth being aware of. It needs its own patched loader
     (baked into AEON's image) -- Task ME's mods/gemma4-nvfp4 mod is for
     a different bug in a different naming convention and does not apply
     here.

  3. --image defaults to the PINNED tag
     (ghcr.io/aeon-7/aeon-vllm-ultimate:2026-06-18-v0.23.0-dflashfix),
     NOT AEON's ':latest'. This is deliberate, not caution for its own
     sake: ':latest' has moved at least twice since AEON's 144 tok/s
     claim was published and now serves their whole model fleet off one
     shared image. ':latest' was tested once against the 'default'
     prompt only (51.8 tok/s, close to the pinned tag's 49.3) -- it has
     NEVER been tested against coding/extraction/creative, so the
     validated numbers above only apply to the pinned tag. Don't assume
     ':latest' reproduces them without testing it directly.

  4. --num-speculative-tokens defaults to 10 (AEON's own documented
     value), not a smaller value -- this is what every validated number
     above was actually measured with.

  5. First run pulls a multi-GB image if this pinned tag hasn't been
     cached on this cluster before. That step alone can take a while --
     this script pulls explicitly first, with its own generous timeout,
     rather than letting `docker run`'s inline pull-if-missing blow
     through a timeout sized for "launch an already-cached container"
     (a bug that hit for real, twice, against two different images,
     during the testing that produced the numbers above).

After a successful deploy, check health/logs the normal way:
    dgx-config logs spark-4
    curl http://<head-ip>:8000/health

And tear down when you're done -- note that teardown now requires an
explicit scope, so name the host this deployed to rather than clearing
the whole cluster:
    dgx-config teardown --host <node>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

# See TOMBSTONES.md #87 -- a script outside the repo root needs this
# before any `common` import, dgx-orchestrator.py doesn't because it
# lives at the repo root itself.
_REPO_ROOT = Path(os.getenv("BASE_DIR", Path(__file__).resolve().parent))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from common.config import (
    default_deploy_target,
    legacy_hosts_dict,
    load_cluster_config,
    reserved_hosts,
)
from common.constants import ContainerRole
from common.ssh import run_ssh

HOSTS = legacy_hosts_dict()
PRIMARY_HOST = next(iter(HOSTS), None)

# Where this lands when --host isn't given. Previously PRIMARY_HOST, which
# is also the Ray head node and the telemetry-authoritative serving host --
# so on a cluster where that node holds something long-lived, a bare run
# of this script aimed straight at it. DEFAULT_DEPLOY_HOST is
# cluster_config.yaml's default_deploy_target when set and falls back to
# PRIMARY_HOST when it isn't, so this is a no-op for any config that
# hasn't opted in.
DEFAULT_DEPLOY_HOST = default_deploy_target() or PRIMARY_HOST
RESERVED_HOSTS = reserved_hosts()

LATEST_DFLASH_IMAGE = "ghcr.io/aeon-7/aeon-vllm-ultimate:latest"  # moving tag -- NOT what the validated numbers above were measured against; see docstring point 3
VALIDATED_DFLASH_IMAGE = "ghcr.io/aeon-7/aeon-vllm-ultimate:2026-06-18-v0.23.0-dflashfix"  # pinned -- this is what all 16 validated runs actually used
DFLASH_HF_PATH = "AEON-7/Gemma-4-26B-A4B-it-Uncensored-NVFP4"
DFLASH_DRAFTER = "z-lab/gemma-4-26B-A4B-it-DFlash"
DEFAULT_GPU_UTIL = 0.65  # validated across all 16 runs -- see module docstring point 1
DEFAULT_NUM_SPECULATIVE_TOKENS = 10  # AEON's own documented value -- what every validated number was measured with, see docstring point 4
GPU_UTIL_STALL_WARNING_THRESHOLD = 0.8


def check_vllm_health(ip: str, port: int) -> bool:
    try:
        req = urllib.request.Request(f"http://{ip}:{port}/health", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def wait_for_health(ip: str, port: int, timeout_sec: int, poll_interval: int = 10, stabilize_sec: int = 15) -> bool:
    """Doesn't trust the first successful poll alone -- see the smoke
    test's own wait_for_health() docstring for why (a crash landing
    seconds after /health first turns green is a real, observed failure
    mode on this exact model, just on a different pipeline)."""
    t0 = time.time()
    while time.time() - t0 < timeout_sec:
        if check_vllm_health(ip, port):
            time.sleep(stabilize_sec)
            if check_vllm_health(ip, port):
                return True
        time.sleep(poll_interval)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default=None,
                         help="Target host for this deploy. Default: %s (cluster_config.yaml's "
                              "default_deploy_target, falling back to the primary host). A host marked "
                              "reserved: true is refused outright -- there is no --force here, by design."
                              % DEFAULT_DEPLOY_HOST)
    parser.add_argument("--gpu-util", type=float, default=DEFAULT_GPU_UTIL, help=f"Default: {DEFAULT_GPU_UTIL} -- see module docstring point 1 before raising this")
    parser.add_argument("--num-speculative-tokens", type=int, default=DEFAULT_NUM_SPECULATIVE_TOKENS,
                         help=f"Default: {DEFAULT_NUM_SPECULATIVE_TOKENS} (AEON's own documented value). "
                              f"Every validated number in this script's docstring was measured at this "
                              f"exact value -- changing it means you're no longer running the validated "
                              f"config, just a plausible variant of it.")
    parser.add_argument("--max-model-len", type=int, default=32768)
    parser.add_argument("--image", default=VALIDATED_DFLASH_IMAGE,
                         help=f"Default: {VALIDATED_DFLASH_IMAGE} (pinned -- see module docstring point 3). "
                              f"AEON's own ':latest' ({LATEST_DFLASH_IMAGE}) has moved at least twice since "
                              f"their 144 tok/s claim was published and was only ever tested here against "
                              f"the 'default' prompt, never coding/extraction/creative -- don't assume it "
                              f"reproduces this script's validated numbers without testing it directly.")
    parser.add_argument("--force-flashinfer-moe", action="store_true",
                         help="Off by default. Forces VLLM_NVFP4_GEMM_BACKEND=flashinfer-cutlass, which this "
                              "image's own envs.py reports as an unrecognized variable (confirmed via boot log) "
                              "-- kept as an opt-in, not applied automatically, since a sibling AEON model's docs "
                              "suggest the auto-selected VLLM_CUTLASS backend may already be the intended "
                              "default, not something to correct.")
    parser.add_argument("--wait-timeout", type=int, default=None, help="Seconds to wait for /health. Default: cluster tuning.deploy_wait_timeout_sec")
    args = parser.parse_args()

    if PRIMARY_HOST is None:
        print("[!] No active hosts found via common.config.legacy_hosts_dict() -- check cluster_config.yaml.", file=sys.stderr)
        return 2

    cfg = load_cluster_config()
    host = args.host or DEFAULT_DEPLOY_HOST
    if host not in HOSTS:
        print(f"[!] Unknown host {host!r}. Known hosts: {list(HOSTS)}", file=sys.stderr)
        return 2

    # See module docstring: this script bypasses the orchestrator entirely,
    # so its reserved-host guard never fires for us. Check before the pull,
    # not after -- a refusal should cost nothing, and a multi-GB image pull
    # onto a host we're about to refuse is pure waste.
    #
    # Worth knowing what this protects against concretely: the docker run
    # below claims the literal `vllm-standalone` name. On a host already
    # serving something under that name, Docker rejects the name collision
    # -- so the failure mode was a confusing conflict error rather than
    # silent data loss. But it is still a deploy aimed at a machine someone
    # marked hands-off, and the collision only saves you while the resident
    # container happens to use the same name.
    if host in RESERVED_HOSTS:
        print(
            f"[!] Refusing to deploy to {host!r}: marked reserved in cluster_config.yaml.\n"
            f"    This script bypasses the orchestrator, so nothing else would have caught this.\n"
            f"    Pass --host explicitly to target a different node.",
            file=sys.stderr,
        )
        return 2
    ip = HOSTS[host]["ip"]
    user = cfg.ssh_user
    wait_timeout = args.wait_timeout or cfg.tuning.deploy_wait_timeout_sec
    gpu_util = args.gpu_util
    image = args.image

    print(f"hf_path={DFLASH_HF_PATH}\nimage={image}\nhost={host} gpu_util={gpu_util}\n"
          f"NOT NVIDIA's checkpoint -- see this script's own module docstring, point 2.")
    if gpu_util > GPU_UTIL_STALL_WARNING_THRESHOLD:
        print(
            f"[!] gpu_util={gpu_util} requested (> {GPU_UTIL_STALL_WARNING_THRESHOLD}). AEON-7's own "
            f"production notes for this exact model on this exact hardware family report page-thrashing "
            f"above ~0.8 and say even 0.85 stalls the box. Proceeding because it was requested "
            f"explicitly, not because it's recommended."
        )

    vol_mount = cfg.hosts[host].volume_mount
    spec_cfg = json.dumps({
        "method": "dflash",
        "model": DFLASH_DRAFTER,
        "num_speculative_tokens": args.num_speculative_tokens,
        "attention_backend": "flash_attn",
    })

    vllm_serve_args = [
        "serve", DFLASH_HF_PATH,
        "--served-model-name", "gemma4-aeon-uncensored",
        "--host", "0.0.0.0", "--port", str(cfg.ports["vllm_api"]),
        "--tensor-parallel-size", "1",
        "--dtype", "auto",
        "--quantization", "compressed-tensors",
        "--linear-backend", "flashinfer_cutlass",
        "--moe-backend", "cutlass",
        "--attention-backend", "triton_attn",
        "--max-model-len", str(args.max_model_len),
        "--max-num-seqs", "32",
        "--max-num-batched-tokens", "16384",
        "--gpu-memory-utilization", str(gpu_util),
        "--enable-chunked-prefill",
        "--enable-prefix-caching",
        "--trust-remote-code",
        "--enable-auto-tool-choice",
        "--tool-call-parser", "gemma4",
        "--reasoning-parser", "gemma4",
        "--speculative-config", spec_cfg,
    ]
    docker_env = [
        "-e", "VLLM_ALLOW_LONG_MAX_MODEL_LEN=1",
        "-e", "TORCH_MATMUL_PRECISION=high",
        "-e", "PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True",
        "-e", "VLLM_TEST_FORCE_FP8_MARLIN=0",
        "-e", "VLLM_USE_FLASHINFER_SAMPLER=1",
    ]
    if args.force_flashinfer_moe:
        docker_env += ["-e", "VLLM_USE_FLASHINFER_MOE_FP4=0", "-e", "VLLM_NVFP4_GEMM_BACKEND=flashinfer-cutlass"]

    docker_cmd = [
        "docker", "run", "-d", "--init",
        "--name", ContainerRole.STANDALONE,
        "--gpus", "all", "--ipc=host", "--net=host",
    ] + docker_env + [
        "-v", vol_mount,
        "--entrypoint", "vllm",
        image,
    ] + vllm_serve_args

    print(f"\npulling {image} on {host} if not already cached (first pull can take a while)...")
    pull_res = run_ssh(ip, user, ["docker", "pull", image], timeout=1800)
    if pull_res.returncode != 0:
        print(f"[FAIL] docker pull failed: {pull_res.stderr.strip()[:400]}", file=sys.stderr)
        return 1
    print("[OK] image pulled/cached")

    print(f"\nlaunching container on {host}...")
    run_res = run_ssh(ip, user, docker_cmd, timeout=90)
    if run_res.returncode != 0:
        print(f"[FAIL] docker run failed: {run_res.stderr.strip()[:400]}", file=sys.stderr)
        return 1
    print("[OK] container launched")

    print(f"\npolling /health on {host} (up to {wait_timeout}s)...")
    healthy = wait_for_health(ip, cfg.ports["vllm_api"], wait_timeout)
    if not healthy:
        print(
            f"[FAIL] never became healthy within {wait_timeout}s. Container is likely still running --\n"
            f"  check what happened: dgx-config logs {host}\n"
            f"  and tear down if it's stuck: dgx-config teardown --host {host}",
            file=sys.stderr,
        )
        return 1

    print(
        f"\n[OK] healthy and serving on {host}:{cfg.ports['vllm_api']}\n"
        f"  logs:     dgx-config logs {host}\n"
        f"  teardown: dgx-config teardown --host {host}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
