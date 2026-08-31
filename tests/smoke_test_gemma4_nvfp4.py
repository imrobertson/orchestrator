#!/usr/bin/env python3
"""
tests/smoke_test_gemma4_nvfp4.py -- fire-and-forget, staged smoke test for
Gemma 4 26B-A4B NVFP4 on this cluster.

    docker exec -it dgx-orchestrator-api python3 tests/smoke_test_gemma4_nvfp4.py

Three stages, run in order by default (--stage all), each gated on the
previous stage's health check before proceeding:

  baseline  -- nvidia/Gemma-4-26B-A4B-NVFP4, this cluster's eugr image,
               no mod, no speculative decoding. Sanity check only.
  mtp       -- same checkpoint/image + Gemma 4's native MTP speculative
               decoding (google/gemma-4-26B-A4B-it-assistant drafter,
               num_speculative_tokens=2 by default -- eugr's own tuned
               value; it beat the shipped default of 4 in their own
               tuning report). This is eugr's actual recipe shape: their
               gemma4-26b-a4b-nvfp4.yaml measured 54.9-56 tok/s
               single-stream this way (spark-vllm-docker issue #343).
  dflash    -- AEON-7's separate pipeline: their own checkpoint (an
               "uncensored" finetune, NOT NVIDIA's official weights),
               their own image, their own DFlash drafter. 144-158 tok/s
               single-stream claimed on their hardware. Opt-in-flavored
               even under --stage all (auto-skipped if mtp didn't come up
               healthy) -- read point 2 and 3 below before running it.

Four things worth stating up front, found while sourcing this rather than
assumed:

  1. gpu_util: AEON-7's own production notes for THIS model on THIS
     hardware (GB10's shared LPDDR5X pool) say "above ~0.8 the shared
     pool page-thrashes and stalls the box, and even 0.85 stalls." That
     is the most specific, most relevant source found on this exact
     question, and it directly contradicts "0.85 if reported stable" --
     by that source, 0.85 IS reported unstable, on this exact model, on
     this exact hardware family. So the default here is 0.75 (this
     cluster's own configured gpu_util_ceiling) for baseline/mtp, 0.65
     for dflash (AEON's own recommended production default). --gpu-util
     and --dflash-gpu-util override these if you want to push past them
     anyway -- the script warns loudly above 0.8 but does not refuse.

  2. The dflash stage is a structurally different deploy, not a flag
     change on the same recipe. AEON's image ships ENTRYPOINT bash and
     needs `--entrypoint vllm ... serve <path>`; this cluster's
     _execute_deployment_impl() always runs
     `python3 -m vllm.entrypoints.openai.api_server --model <hf_path>`
     against the image's own default entrypoint, and there is no
     per-recipe entrypoint override anywhere in the recipe schema or the
     deploy code. So this stage does NOT go through
     write_scratch_recipe()/the normal CLI deploy path the way baseline
     and mtp do -- it builds and runs the docker command directly over
     SSH. It uses the same container name (vllm-standalone, via
     common.constants.ContainerRole) so `dgx-config teardown` still finds
     and removes it correctly, but it does NOT get
     ACTIVE_DEPLOYMENT_STATE tracking, JIT-cache mounts, or dashboard
     visibility the way a real deploy does.

  3. AEON's checkpoint is not NVIDIA's official checkpoint wearing a
     faster hat. AEON-7/Gemma-4-26B-A4B-it-Uncensored-NVFP4 is quantized
     from an "uncensored" finetune (TrevorJS/gemma-4-26B-A4B-it-uncensored)
     using compressed-tensors NVFP4 (llmcompressor), a different format
     from NVIDIA's ModelOpt NVFP4 with a different tensor-naming
     convention, requiring AEON's OWN patched loader baked into their
     image. Task ME's mods/gemma4-nvfp4 targets the ModelOpt format's
     scale-key bug specifically and must not be applied here -- it fixes
     a different bug in a different naming scheme. This is a different
     checkpoint with its own content-moderation posture, not a drop-in
     speed upgrade to the same weights.

  4. MTP's assistant drafter needs transformers >= 5.8.0.dev0 (a
     pre-release requirement -- gemma4_assistant's model_type isn't
     registered before that, confirmed from vLLM PR #41745's own review
     notes) and vLLM >= 0.21.0 (MTP merged into stock vLLM 2026-05-06,
     shipped in 0.21.0 2026-05-15). eugr's own tuning report (issue #343)
     ran vLLM 0.26.1rc1 -- comfortably past both -- but that is eugr's
     machine, not this cluster's actual eugr/spark-vllm-b12x:latest pull.
     The baseline/mtp stages below are what actually confirm this image
     clears both floors, not an assumption carried over from research.

Honesty notes carried over from the previous revision:
  - The boot-log check is a keyword scan (marlin/nvfp4/fusedmoe/modelopt/
    mtp/dflash), not a confirmed exact-line match -- printed for you to
    eyeball, not asserted as a silent pass.
  - Throughput now comes from the cluster's own benchmark.py (3-pass,
    cold + 2 warm, decode_tps), shelled out to exactly the way
    _run_benchmark_worker() does -- not a hand-rolled measurement anymore.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from common.config import BASE_DIR, legacy_hosts_dict, load_cluster_config
from common.constants import ContainerRole
from common.ssh import run_ssh

HOSTS = legacy_hosts_dict()
PRIMARY_HOST = next(iter(HOSTS), None)

BASE_VLLM_ARGS = "--quantization modelopt --kv-cache-dtype fp8 --moe-backend marlin --trust-remote-code"
NVIDIA_HF_PATH = "nvidia/Gemma-4-26B-A4B-NVFP4"
EUGR_IMAGE = "eugr/spark-vllm-b12x:latest"
MTP_ASSISTANT = "google/gemma-4-26B-A4B-it-assistant"

DFLASH_IMAGE = "ghcr.io/aeon-7/aeon-vllm-ultimate:latest"
DFLASH_HF_PATH = "AEON-7/Gemma-4-26B-A4B-it-Uncensored-NVFP4"
DFLASH_DRAFTER = "z-lab/gemma-4-26B-A4B-it-DFlash"

DEFAULT_GPU_UTIL = 0.75          # this cluster's configured gpu_util_ceiling
DEFAULT_DFLASH_GPU_UTIL = 0.65   # AEON's own recommended production default
GPU_UTIL_STALL_WARNING_THRESHOLD = 0.8

RESULTS: list[tuple[str, bool, str]] = []
SUMMARY: dict[str, dict] = {}


def record(label: str, passed: bool, detail: str = "") -> bool:
    RESULTS.append((label, passed, detail))
    mark = "PASS" if passed else "FAIL"
    suffix = f" -- {detail}" if detail else ""
    print(f"[{mark}] {label}{suffix}")
    return passed


def warn_if_gpu_util_risky(stage: str, gpu_util: float) -> None:
    if gpu_util > GPU_UTIL_STALL_WARNING_THRESHOLD:
        print(
            f"[!] [{stage}] gpu_util={gpu_util} requested (> {GPU_UTIL_STALL_WARNING_THRESHOLD}). "
            f"AEON-7's own production notes for this exact model on this exact hardware family "
            f"report page-thrashing above ~0.8 and say even 0.85 stalls the box. Proceeding because "
            f"it was requested explicitly, not because it's recommended -- watch for a hung/unresponsive "
            f"host, not just a failed deploy, if this stage misbehaves."
        )


def _extract_last_json_object(stdout: str) -> dict:
    """
    dgx-orchestrator.py's CLI subcommands do `print(json.dumps(result,
    indent=2))` as their LAST action, but functions along the way may
    already have printed plain-text lines to the same stdout (e.g.
    common/ssh.py's get_hf_token() warning path). json.dumps(...,
    indent=2) always starts its output with a line that is exactly "{"
    and nothing else -- no progress-print line in this codebase does that
    -- so the last such line marks where the real JSON begins.
    """
    lines = stdout.splitlines()
    start = None
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip() == "{":
            start = i
            break
    if start is None:
        raise ValueError(f"No top-level JSON object found in CLI output:\n{stdout[-2000:]}")
    return json.loads("\n".join(lines[start:]))


def write_scratch_recipe(stage: str, hf_path: str, image: str | None, gpu_util: float, max_model_len: int, vllm_args: str) -> tuple[Path, str]:
    recipe_name = f"_scratch-gemma4-nvfp4-{stage}"
    path = BASE_DIR / "recipes" / "local" / f"{recipe_name}.yaml"

    lines = [
        'recipe_version: "1"',
        "",
        f"hf_path: {hf_path}",
    ]
    if image:
        lines.append(f"image: {image}")
    lines.append(f"gpu_util: {gpu_util}")
    lines.append("")
    lines.append("mods: []")
    lines += [
        "",
        "notes: >",
        f"  Scratch recipe generated by tests/smoke_test_gemma4_nvfp4.py, stage "
        f"'{stage}'. Regenerated on every run; deleted afterward unless --keep "
        f"is passed. Not a production recipe.",
        "",
        "topologies:",
        "  1_node:",
        f"    max_model_len: {max_model_len}",
        "    tp_size: 1",
        "    pp_size: 1",
        "    env_vars: []",
        f'    vllm_args: "{vllm_args}"',
    ]
    path.write_text("\n".join(lines) + "\n")
    return path, recipe_name


def check_vllm_health(ip: str, port: int) -> bool:
    try:
        req = urllib.request.Request(f"http://{ip}:{port}/health", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def wait_for_health(ip: str, port: int, timeout_sec: int, poll_interval: int = 10) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout_sec:
        if check_vllm_health(ip, port):
            return True
        time.sleep(poll_interval)
    return False


def deploy_via_recipe(stage: str, recipe_name: str, host: str, ip: str, port: int, wait_timeout: int) -> bool:
    """
    Deploys via the real CLI WITHOUT --wait, then independently polls
    /health ourselves. Deliberate: _execute_deployment_impl()'s wait=True
    path calls wait_for_cluster_ready() but never checks its result before
    returning {"status": "success", ...} -- a container that launched fine
    and then simply never became healthy still reports "success" from
    --wait. Passing --wait AND polling ourselves afterward would also
    double the worst-case wait, since the CLI's internal wait already
    burns up to wait_timeout regardless of outcome. So: no --wait, one
    poll, one timeout budget.
    """
    print(f"\n=== [{stage}] deploy (recipe {recipe_name}) ===")
    deploy_cmd = [sys.executable, "dgx-orchestrator.py", "cli", "deploy",
                  "--model", recipe_name, "--nodes", "1", "--head", host]
    t0 = time.time()
    try:
        res = subprocess.run(deploy_cmd, capture_output=True, text=True, cwd=str(BASE_DIR), timeout=300)
    except subprocess.TimeoutExpired:
        record(f"[{stage}] deploy command completes", False, "subprocess timed out after 300s (no --wait passed, so this should return in well under a minute -- a hang means the docker-run SSH call itself is stuck)")
        return False
    elapsed = time.time() - t0

    try:
        payload = _extract_last_json_object(res.stdout)
    except ValueError as exc:
        record(f"[{stage}] deploy command completes", False, f"could not parse CLI output: {exc}")
        return False

    launched_ok = res.returncode == 0 and payload.get("status") == "success"
    record(f"[{stage}] deploy command reports success (no immediate crash)", launched_ok,
           f"{elapsed:.0f}s" if launched_ok else payload.get("message", res.stderr.strip()[-400:]))
    if not launched_ok:
        return False

    print(f"    deploy command returned; independently polling /health (up to {wait_timeout}s)...")
    healthy = wait_for_health(ip, port, wait_timeout)
    record(f"[{stage}] /health confirmed ready (independent poll)", healthy)
    return healthy


def check_boot_log(stage: str, host: str, ip: str, user: str) -> None:
    print(f"\n--- [{stage}] boot log scan ({host}) ---")
    ps_res = run_ssh(ip, user, ["docker", "ps", "-a", "--format", "{{.Names}}"], timeout=10)
    containers = [c.strip() for c in ps_res.stdout.splitlines() if c.strip() in (ContainerRole.STANDALONE, ContainerRole.HEAD, ContainerRole.WORKER)]
    if not containers:
        record(f"[{stage}] container present on {host} for log check", False, "no vllm container found")
        return

    log_res = run_ssh(ip, user, ["docker", "logs", "--tail", "800", containers[0]], timeout=15)
    log_text = (log_res.stdout or "") + (log_res.stderr or "")
    keywords = ["marlin", "nvfp4", "fusedmoe", "modelopt", "mtp", "dflash", "cutlass"]
    hits = [kw for kw in keywords if kw in log_text.lower()]
    record(f"[{stage}] boot log contains a relevant backend/decoding keyword (scan, not a confirmed exact-line match)",
           bool(hits), ", ".join(hits) if hits else "no keyword matched -- read the excerpt below")

    print("    matching lines:")
    shown = 0
    for line in log_text.splitlines():
        if any(kw in line.lower() for kw in keywords):
            print(f"      {line.strip()[:220]}")
            shown += 1
    if not shown:
        print(f"      (none -- run `dgx-config logs {host}` by hand to see the full boot log)")


def run_real_benchmark(head_ip: str, model_key: str, max_tokens: int) -> tuple[dict | None, str]:
    """
    Shells out to this repo's own benchmark.py exactly the way
    dgx-orchestrator.py's _run_benchmark_worker() does (same argv shape),
    rather than hand-rolling a throughput measurement. Blocking, since
    this script needs the result before deciding whether to move to the
    next stage -- unlike the orchestrator's own /api/benchmark path,
    which backgrounds it.
    """
    cmd = [sys.executable, str(BASE_DIR / "benchmark.py"), "--host", head_ip,
           "--nodes", "1", "--model-key", model_key, "--max-tokens", str(max_tokens)]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, cwd=str(BASE_DIR), timeout=1200)
    except subprocess.TimeoutExpired:
        return None, "benchmark.py timed out after 1200s"
    if res.returncode != 0:
        tail_lines = (res.stderr or res.stdout or "no output").strip().splitlines()
        return None, tail_lines[-1] if tail_lines else "benchmark.py failed with no output"

    warm_m = re.search(r"Warm Avg \(Runs 2\+\)\s*: TTFT ([\d.]+)s \| Decode Speed: ([\d.]+) tok/s", res.stdout)
    if not warm_m:
        return None, f"could not parse benchmark.py output:\n{res.stdout[-800:]}"
    cold_m = re.search(r"Cold Start \(Run 1\)\s*: TTFT ([\d.]+)s \| Decode Speed: ([\d.]+) tok/s", res.stdout)

    return {
        "warm_ttft": float(warm_m.group(1)),
        "warm_decode_tps": float(warm_m.group(2)),
        "cold_ttft": float(cold_m.group(1)) if cold_m else None,
        "cold_decode_tps": float(cold_m.group(2)) if cold_m else None,
    }, res.stdout


def teardown() -> None:
    subprocess.run([sys.executable, "dgx-orchestrator.py", "cli", "teardown"],
                    cwd=str(BASE_DIR), capture_output=True, text=True, timeout=120)


def run_stage1(stage: str, args, cfg, host: str, ip: str, user: str) -> bool:
    """baseline or mtp -- both go through the real recipe/CLI deploy path."""
    gpu_util = args.gpu_util if args.gpu_util is not None else DEFAULT_GPU_UTIL
    max_model_len = args.max_model_len
    wait_timeout = args.wait_timeout or cfg.tuning.deploy_wait_timeout_sec

    vllm_args = BASE_VLLM_ARGS
    image = EUGR_IMAGE
    if stage == "mtp":
        spec_cfg = json.dumps({"method": "mtp", "model": MTP_ASSISTANT, "num_speculative_tokens": args.num_speculative_tokens})
        vllm_args = f"{BASE_VLLM_ARGS} --speculative-config '{spec_cfg}'"

    print(f"\n{'#' * 70}\n# stage: {stage}\n# hf_path={NVIDIA_HF_PATH} image={image} gpu_util={gpu_util}\n{'#' * 70}")
    warn_if_gpu_util_risky(stage, gpu_util)

    SUMMARY[stage] = {"deployed": False, "tps": None, "boot_log_hit": None}

    recipe_path, recipe_name = write_scratch_recipe(stage, NVIDIA_HF_PATH, image, gpu_util, max_model_len, vllm_args)
    try:
        deployed_ok = deploy_via_recipe(stage, recipe_name, host, ip, cfg.ports["vllm_api"], wait_timeout)
        SUMMARY[stage]["deployed"] = deployed_ok

        if deployed_ok:
            check_boot_log(stage, host, ip, user)
            SUMMARY[stage]["boot_log_hit"] = any(
                label.startswith(f"[{stage}] boot log contains") and passed
                for label, passed, _ in RESULTS
            )

            print(f"\n--- [{stage}] benchmark (3-pass, via benchmark.py) ---")
            bench, detail = run_real_benchmark(ip, recipe_name, args.max_tokens)
            if bench:
                SUMMARY[stage]["tps"] = bench["warm_decode_tps"]
                record(f"[{stage}] benchmark.py succeeds", True,
                       f"warm {bench['warm_decode_tps']:.1f} tok/s (TTFT {bench['warm_ttft']:.2f}s), "
                       f"cold {bench['cold_decode_tps']}")
            else:
                record(f"[{stage}] benchmark.py succeeds", False, detail)
        return deployed_ok
    finally:
        if not args.keep:
            print(f"\n--- tearing down after '{stage}' ---")
            teardown()
            try:
                recipe_path.unlink(missing_ok=True)
            except OSError:
                pass
        else:
            print(f"\n--keep set: leaving '{stage}' deployed (if it deployed) and {recipe_path} on disk.")


def run_dflash_stage(args, cfg, host: str, ip: str, user: str) -> bool:
    """
    AEON-7's DFlash pipeline. Does NOT go through write_scratch_recipe()/
    the CLI deploy path -- see module docstring point 2 for why (the image
    needs an --entrypoint override the recipe schema has no field for).
    Builds and runs the docker command directly over SSH, using
    ContainerRole.STANDALONE's literal name so `dgx-config teardown` still
    finds and removes it.
    """
    gpu_util = args.dflash_gpu_util
    stage = "dflash"
    print(f"\n{'#' * 70}\n# stage: {stage}\n# hf_path={DFLASH_HF_PATH} image={DFLASH_IMAGE} gpu_util={gpu_util}\n"
          f"# NOT NVIDIA's checkpoint -- see module docstring point 3.\n{'#' * 70}")
    warn_if_gpu_util_risky(stage, gpu_util)

    SUMMARY[stage] = {"deployed": False, "tps": None, "boot_log_hit": None}
    wait_timeout = args.wait_timeout or cfg.tuning.deploy_wait_timeout_sec

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
    docker_cmd = [
        "docker", "run", "-d", "--init",
        "--name", ContainerRole.STANDALONE,
        "--gpus", "all", "--ipc=host", "--net=host",
        "-e", "VLLM_ALLOW_LONG_MAX_MODEL_LEN=1",
        "-e", "TORCH_MATMUL_PRECISION=high",
        "-e", "PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True",
        "-e", "VLLM_TEST_FORCE_FP8_MARLIN=0",
        "-e", "VLLM_USE_FLASHINFER_SAMPLER=1",
        "-v", vol_mount,
        "--entrypoint", "vllm",
        DFLASH_IMAGE,
    ] + vllm_serve_args

    try:
        run_res = run_ssh(ip, user, docker_cmd, timeout=60)
        launched_ok = record(f"[{stage}] docker run launches", run_res.returncode == 0,
                              "" if run_res.returncode == 0 else run_res.stderr.strip()[:400])
        if not launched_ok:
            return False

        print(f"    container started; independently polling /health (up to {wait_timeout}s)...")
        healthy = wait_for_health(ip, cfg.ports["vllm_api"], wait_timeout)
        SUMMARY[stage]["deployed"] = healthy
        record(f"[{stage}] /health confirmed ready (independent poll)", healthy)
        if not healthy:
            return False

        check_boot_log(stage, host, ip, user)
        SUMMARY[stage]["boot_log_hit"] = any(
            label.startswith(f"[{stage}] boot log contains") and passed
            for label, passed, _ in RESULTS
        )

        print(f"\n--- [{stage}] benchmark (3-pass, via benchmark.py) ---")
        bench, detail = run_real_benchmark(ip, "dflash-scratch", args.max_tokens)
        if bench:
            SUMMARY[stage]["tps"] = bench["warm_decode_tps"]
            record(f"[{stage}] benchmark.py succeeds", True,
                   f"warm {bench['warm_decode_tps']:.1f} tok/s (TTFT {bench['warm_ttft']:.2f}s)")
        else:
            record(f"[{stage}] benchmark.py succeeds", False, detail)
        return healthy
    finally:
        if not args.keep:
            print(f"\n--- tearing down after '{stage}' ---")
            teardown()
        else:
            print(f"\n--keep set: leaving '{stage}' deployed (if it deployed).")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stage", choices=["baseline", "mtp", "dflash", "all"], default="all",
                         help="Which stage(s) to run. 'all' runs baseline -> mtp -> dflash in order, "
                              "skipping dflash if mtp did not come up healthy.")
    parser.add_argument("--host", default=None, help="Default: cluster's primary host (%s)" % PRIMARY_HOST)
    parser.add_argument("--gpu-util", type=float, default=None, help=f"baseline/mtp gpu_util. Default: {DEFAULT_GPU_UTIL} (cluster's gpu_util_ceiling)")
    parser.add_argument("--dflash-gpu-util", type=float, default=DEFAULT_DFLASH_GPU_UTIL, help=f"Default: {DEFAULT_DFLASH_GPU_UTIL} (AEON's own recommended production value)")
    parser.add_argument("--num-speculative-tokens", type=int, default=2, help="Default: 2 (eugr's own tuned value for mtp; also used for dflash's n if that stage runs)")
    parser.add_argument("--max-model-len", type=int, default=32768)
    parser.add_argument("--max-tokens", type=int, default=256, help="max_tokens passed to benchmark.py")
    parser.add_argument("--keep", action="store_true", help="Skip teardown and leave scratch recipe file(s) on disk for follow-up poking")
    parser.add_argument("--wait-timeout", type=int, default=None, help="Seconds to wait for /health. Default: cluster tuning.deploy_wait_timeout_sec")
    args = parser.parse_args()

    if PRIMARY_HOST is None:
        print("[!] No active hosts found via common.config.legacy_hosts_dict() -- check cluster_config.yaml.", file=sys.stderr)
        return 2

    cfg = load_cluster_config()
    host = args.host or PRIMARY_HOST
    if host not in HOSTS:
        print(f"[!] Unknown host {host!r}. Known hosts: {list(HOSTS)}", file=sys.stderr)
        return 2
    ip = HOSTS[host]["ip"]
    user = cfg.ssh_user

    stages = ["baseline", "mtp", "dflash"] if args.stage == "all" else [args.stage]

    for stage in stages:
        if stage == "dflash" and args.stage == "all" and not SUMMARY.get("mtp", {}).get("deployed"):
            print(
                "\n[!] Skipping 'dflash' -- 'mtp' did not come up healthy. Run `--stage dflash` "
                "explicitly if you want to try it anyway despite mtp's failure (it's a fully "
                "independent pipeline, see module docstring point 2 -- it doesn't depend on mtp "
                "having worked), otherwise fix mtp first."
            )
            break
        try:
            if stage in ("baseline", "mtp"):
                run_stage1(stage, args, cfg, host, ip, user)
            elif stage == "dflash":
                run_dflash_stage(args, cfg, host, ip, user)
        except KeyboardInterrupt:
            print(f"\n[!] Interrupted during '{stage}' -- tearing down before exit.")
            if not args.keep:
                teardown()
            raise

    print(f"\n{'=' * 70}\nSUMMARY\n{'=' * 70}")
    for stage in stages:
        s = SUMMARY.get(stage, {})
        tps_str = f"{s['tps']:.1f} tok/s" if s.get("tps") else "n/a"
        clears_55 = " (>= 55 tok/s)" if s.get("tps") and s["tps"] >= 55 else ""
        print(f"  {stage:10s} deployed={s.get('deployed')!s:5s} boot_log_hit={s.get('boot_log_hit')!s:5s} tps={tps_str}{clears_55}")

    n_pass = sum(1 for _, ok, _ in RESULTS if ok)
    n_total = len(RESULTS)
    print(f"\n{n_pass}/{n_total} checks passed.")
    return 0 if n_pass == n_total else 1


if __name__ == "__main__":
    sys.exit(main())
