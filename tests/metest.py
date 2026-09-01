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
import copy
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# This script lives in tests/, one level below the repo root -- when invoked
# as `python3 tests/smoke_test_gemma4_nvfp4.py`, Python puts tests/ on
# sys.path[0], not the repo root, so `common` is not importable as-is
# (dgx-orchestrator.py doesn't hit this because it lives at the repo root
# itself). Mirrors common/config.py's own BASE_DIR resolution -- respects
# the BASE_DIR env var docker-compose.yml sets (=/app in the orchestrator
# container) and falls back to computing it from this file's own location
# otherwise, rather than hardcoding /app.
_REPO_ROOT = Path(os.getenv("BASE_DIR", Path(__file__).resolve().parent.parent))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

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

# Named prompt presets, roughly matching AEON's own published category
# breakdown ("144 tok/s single (Coding), up to 158 (Extraction)") plus a
# "creative" category at the other end -- their sibling models' own docs
# describe DFlash's speedup as acceptance-rate-driven, varying a lot by
# workload. Exists because of a real, measured finding, not a hunch: the
# identical dflash config (n=10, pinned v0.23.0-dflashfix image) swung
# from 49.5 to 103.8 tok/s warm purely from swapping the prompt from a
# generic technical-overview request to a coding task. "coding" below is
# that exact prompt, verbatim, so results stay directly comparable to
# that already-measured data point rather than a close-but-different
# rewording. "default" (None) leaves benchmark.py's own built-in prompt
# untouched.
PROMPT_PRESETS: dict[str, str | None] = {
    "default": None,
    "coding": (
        "Write a Python function that implements a red-black tree with insert, "
        "delete, and search operations. Include full docstrings and type hints."
    ),
    "extraction": (
        "Extract the following fields as a JSON object from this invoice text: "
        "vendor name, invoice date, total amount, and line items with quantities "
        "and prices. Invoice text: \"Acme Supplies Inc. Invoice #4471. Date: "
        "2026-03-14. Item: Widget A, Qty: 12, Price: $4.50 each. Item: Widget B, "
        "Qty: 5, Price: $9.00 each. Subtotal: $99.00. Tax: $8.91. Total: $107.91.\" "
        "Respond with only the JSON object, no explanation."
    ),
    "creative": (
        "Write a warm, professional thank-you email to a colleague who stayed "
        "late to help you finish a big project before a deadline. Keep it to "
        "about 150 words."
    ),
}

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


class _Tee:
    """
    Duplicates writes to multiple streams -- used to save a full transcript
    of this run's own terminal output (the [PASS]/[FAIL] lines and the
    SUMMARY block) automatically, alongside the container-log capture
    save_container_logs() already does.

    This exists because relying on a human to notice a failure, remember
    to scroll back, and copy-paste the output before it scrolls away or
    the terminal closes has already failed twice in practice on this exact
    script -- once for container logs (fixed by save_container_logs()),
    once for the script's own summary output (this fix). The pattern is
    the same both times: don't make correctness depend on someone
    reacting fast enough. Capture automatically, in-process, every time.
    """
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            s.write(data)

    def flush(self):
        for s in self.streams:
            s.flush()


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
        # Block scalar (>-), not a quoted flow scalar: vllm_args for the mtp
        # stage embeds a JSON --speculative-config value containing double
        # quotes, and wrapping that in `vllm_args: "..."` breaks the YAML
        # parse the instant it hits the first embedded ". Confirmed via a
        # direct yaml.safe_load() repro before shipping this fix, not just
        # reasoned about -- the double-quoted version produced
        # "expected <block end>, but found '<scalar>'" and silently dropped
        # the mtp recipe from the catalog (surfaced as "Model ... not
        # defined in catalog", nothing pointing at the actual YAML syntax
        # error). A block scalar has no quote-delimiter to collide with --
        # verified round-trips to the exact original string and
        # shlex.split()s into the correct argv, single-line content only
        # (this codebase's own vllm_args values always are).
        "    vllm_args: >-",
        f"      {vllm_args}",
    ]
    path.write_text("\n".join(lines) + "\n")
    return path, recipe_name


LOG_DIR = BASE_DIR / "tests" / "logs"


def save_container_logs(label: str, host: str, ip: str, user: str) -> Path | None:
    """
    Persists the full container log to disk immediately -- synchronously,
    before any teardown, regardless of pass/fail. Written under BASE_DIR
    (bind-mounted from the host per docker-compose.yml, not the
    container's own ephemeral filesystem), so it survives container
    removal even though it's captured via `docker logs` while the
    container is still up.

    This exists because of a real incident, not a hypothetical: a coworker
    needed the shared cluster between a failed run and the logs being
    pulled by hand, and the evidence was gone. Depending on a human
    reacting fast enough on hardware this script doesn't have exclusive
    claim to is not a plan -- capturing automatically, in-process, the
    moment something might be worth looking at later, is.
    """
    ps_res = run_ssh(ip, user, ["docker", "ps", "-a", "--format", "{{.Names}}"], timeout=10)
    containers = [c.strip() for c in ps_res.stdout.splitlines() if c.strip() in (ContainerRole.STANDALONE, ContainerRole.HEAD, ContainerRole.WORKER)]
    if not containers:
        print(f"    [{label}] no container found on {host} -- nothing to save.")
        return None

    log_res = run_ssh(ip, user, ["docker", "logs", containers[0]], timeout=30)
    log_text = (log_res.stdout or "") + (log_res.stderr or "")
    if not log_text.strip():
        print(f"    [{label}] docker logs returned empty output for {containers[0]} -- nothing to save.")
        return None

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    path = LOG_DIR / f"{label}-{host}-{ts}.log"
    path.write_text(log_text)
    print(f"    [{label}] full container log saved to {path} ({len(log_text)} bytes)")
    return path


def check_vllm_health(ip: str, port: int) -> bool:
    try:
        req = urllib.request.Request(f"http://{ip}:{port}/health", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def wait_for_health(ip: str, port: int, timeout_sec: int, poll_interval: int = 10, stabilize_sec: int = 15) -> bool:
    """
    Doesn't trust the first successful /health poll alone. Confirmed live:
    the identical recipe/config produced an HTTP 500 mid-request on one run
    and a flat connection-refused on the very next -- consistent with a
    crash landing a few seconds after /health first turns green, at a
    slightly different point relative to whatever request happens to be in
    flight when it does. A health check that's accurate at the instant it
    runs and stale by the time a caller acts on it is worse than useless --
    it actively hides the crash behind a passing check. Re-confirms health
    holds for stabilize_sec more seconds before calling it ready; a
    stabilization check that fails falls through to the normal poll loop
    rather than failing outright, so a single transient blip doesn't burn
    the whole timeout budget.

    This raises confidence, it doesn't replace an actual crash traceback --
    if the real failure is a slow background CUDA graph capture that only
    fails well past stabilize_sec, this still won't catch it in time. The
    only real answer is `docker logs` on the container while it still has
    something to show.
    """
    t0 = time.time()
    while time.time() - t0 < timeout_sec:
        if check_vllm_health(ip, port):
            time.sleep(stabilize_sec)
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


def run_real_benchmark(head_ip: str, model_key: str, max_tokens: int, prompt: str | None = None) -> tuple[dict | None, str]:
    """
    Shells out to this repo's own benchmark.py exactly the way
    dgx-orchestrator.py's _run_benchmark_worker() does (same argv shape),
    rather than hand-rolling a throughput measurement. Blocking, since
    this script needs the result before deciding whether to move to the
    next stage -- unlike the orchestrator's own /api/benchmark path,
    which backgrounds it.

    prompt=None uses benchmark.py's own default (a 200-word technical-
    overview request) -- worth overriding when chasing a claim that was
    itself workload-specific. AEON's own dflash numbers were split by
    category ("144 tok/s single (Coding)", "up to 158 (Extraction)"),
    and DFlash's speedup is explicitly acceptance-rate-driven, varying a
    lot by prompt type on a sibling AEON model's own docs -- a fixed
    technical-overview prompt may simply sit in a different
    acceptance-rate regime than whatever "Coding" meant in their test.
    """
    cmd = [sys.executable, str(BASE_DIR / "benchmark.py"), "--host", head_ip,
           "--nodes", "1", "--model-key", model_key, "--max-tokens", str(max_tokens)]
    if prompt:
        cmd += ["--prompt", prompt]
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


def resolve_prompts(args) -> list[tuple[str, str | None]]:
    """
    A raw --prompt always wins outright (single custom run, unnamed).
    Otherwise --prompts selects from PROMPT_PRESETS: a comma-separated
    list of names, "all" for every preset, or the default of just
    "default" (benchmark.py's own built-in prompt, unchanged) when
    neither flag is given -- so existing single-prompt invocations keep
    working exactly as before.
    """
    if args.prompt:
        return [("custom", args.prompt)]
    spec = args.prompts if args.prompts is not None else "default"
    names = list(PROMPT_PRESETS.keys()) if spec == "all" else [n.strip() for n in spec.split(",") if n.strip()]
    unknown = [n for n in names if n not in PROMPT_PRESETS]
    if unknown:
        raise SystemExit(f"[!] Unknown --prompts value(s): {unknown}. Known presets: {list(PROMPT_PRESETS)}")
    return [(n, PROMPT_PRESETS[n]) for n in names]


def run_benchmark_suite(stage: str, ip: str, model_key: str, args) -> bool:
    """
    Runs benchmark.py once per selected prompt preset against the SAME
    already-deployed container -- no redeploy between prompts. A redeploy
    (image pull, weight load, torch.compile, CUDA graph capture) costs
    minutes per prompt and none of that changes between prompts on an
    already-running server; only the request itself differs.

    Exists because of a real, measured finding: the identical dflash
    config (n=10, pinned v0.23.0-dflashfix image) swung from 49.5 to
    103.8 tok/s warm purely from changing the prompt (generic technical
    overview vs. a coding task). AEON's own published numbers were split
    by category ("Coding" vs "Extraction") for exactly this reason --
    comparing two pipelines on a single fixed prompt risks comparing them
    on a workload that happens to favor one over the other for reasons
    that have nothing to do with which pipeline is actually faster.

    Populates SUMMARY[stage]["by_prompt"] with one entry per preset
    actually run, and mirrors the FIRST preset's result into
    SUMMARY[stage]["tps"]/["cold_tps"] for backward compatibility with
    the final summary table and every single-number comparison made
    earlier in this conversation.
    """
    prompts = resolve_prompts(args)
    SUMMARY[stage]["by_prompt"] = {}
    any_ok = False
    for name, prompt_text in prompts:
        label = f"[{stage}:{name}]"
        print(f"\n--- {label} benchmark (3-pass, via benchmark.py) ---")
        # model_key suffixed per-prompt so benchmark_ledger.csv rows stay
        # distinguishable -- otherwise multiple prompts under one run
        # would silently share a ledger key and overwrite each other's
        # historical_tps lookup.
        bench, detail = run_real_benchmark(ip, f"{model_key}-{name}", args.max_tokens, prompt_text)
        if bench:
            any_ok = True
            SUMMARY[stage]["by_prompt"][name] = {"warm": bench["warm_decode_tps"], "cold": bench["cold_decode_tps"]}
            cold_str = f"{bench['cold_decode_tps']:.1f}" if bench["cold_decode_tps"] is not None else "n/a"
            record(f"{label} benchmark.py succeeds", True,
                   f"warm {bench['warm_decode_tps']:.1f} tok/s (TTFT {bench['warm_ttft']:.2f}s), cold {cold_str} tok/s")
        else:
            SUMMARY[stage]["by_prompt"][name] = {"warm": None, "cold": None}
            record(f"{label} benchmark.py succeeds", False, detail)

    first_name = prompts[0][0]
    first = SUMMARY[stage]["by_prompt"].get(first_name, {})
    SUMMARY[stage]["tps"] = first.get("warm")
    SUMMARY[stage]["cold_tps"] = first.get("cold")
    return any_ok


def teardown() -> None:
    subprocess.run([sys.executable, "dgx-orchestrator.py", "cli", "teardown"],
                    cwd=str(BASE_DIR), capture_output=True, text=True, timeout=120)


def run_stage1(stage: str, args, cfg, host: str, ip: str, user: str) -> bool:
    """baseline or mtp -- both go through the real recipe/CLI deploy path."""
    gpu_util = args.gpu_util if args.gpu_util is not None else DEFAULT_GPU_UTIL
    max_model_len = args.max_model_len
    wait_timeout = args.wait_timeout or cfg.tuning.deploy_wait_timeout_sec

    vllm_args = BASE_VLLM_ARGS
    image = args.image or EUGR_IMAGE
    if stage == "mtp":
        spec_cfg = json.dumps({"method": "mtp", "model": MTP_ASSISTANT, "num_speculative_tokens": args.num_speculative_tokens})
        vllm_args = f"{BASE_VLLM_ARGS} --speculative-config '{spec_cfg}'"

    print(f"\n{'#' * 70}\n# stage: {stage}\n# hf_path={NVIDIA_HF_PATH} image={image} gpu_util={gpu_util}\n{'#' * 70}")
    warn_if_gpu_util_risky(stage, gpu_util)

    SUMMARY[stage] = {"deployed": False, "tps": None, "cold_tps": None, "boot_log_hit": None, "by_prompt": {}}

    # Same fix as the dflash stage, for the same reason: dgx-orchestrator.py's
    # own docker run call is sized for "launch an already-cached container,"
    # and a never-before-pulled image (the whole point of --image overrides
    # like this) blows straight through that timeout via docker run's inline
    # pull-if-missing behavior. Pre-pulling here, with a real timeout, means
    # the orchestrator's own docker run has nothing left to download by the
    # time it runs -- confirmed necessary in practice, not just in theory:
    # this exact timeout fired for real on eugr/spark-vllm:latest, a
    # perfectly valid image, purely because it had never been pulled here
    # before.
    print(f"    pulling {image} on {host} if not already cached (first pull can take a while)...")
    pull_res = run_ssh(ip, user, ["docker", "pull", image], timeout=1800)
    pulled_ok = record(f"[{stage}] docker pull succeeds", pull_res.returncode == 0,
                        "" if pull_res.returncode == 0 else pull_res.stderr.strip()[:400])
    if not pulled_ok:
        return False

    recipe_path, recipe_name = write_scratch_recipe(stage, NVIDIA_HF_PATH, image, gpu_util, max_model_len, vllm_args)
    try:
        deployed_ok = deploy_via_recipe(stage, recipe_name, host, ip, cfg.ports["vllm_api"], wait_timeout)
        SUMMARY[stage]["deployed"] = deployed_ok

        # Always, launch succeeded or not, health passed or not -- see
        # save_container_logs()'s own docstring for why this can't be
        # conditional on --keep or on anything succeeding first.
        try:
            save_container_logs(stage, host, ip, user)
        except Exception as exc:
            print(f"    [{stage}] log capture itself failed (non-fatal): {exc}")

        if deployed_ok:
            check_boot_log(stage, host, ip, user)
            SUMMARY[stage]["boot_log_hit"] = any(
                label.startswith(f"[{stage}] boot log contains") and passed
                for label, passed, _ in RESULTS
            )

            print(f"\n--- [{stage}] benchmark suite (via benchmark.py) ---")
            any_bench_ok = run_benchmark_suite(stage, ip, recipe_name, args)
            if not any_bench_ok:
                # A crash triggered BY the benchmark request itself would
                # postdate the snapshot taken right after health passed --
                # grab a second, fresher one now rather than assume the
                # first one already caught it.
                try:
                    save_container_logs(f"{stage}-post-benchmark-failure", host, ip, user)
                except Exception as exc:
                    print(f"    [{stage}] post-failure log capture itself failed (non-fatal): {exc}")
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
    dflash_image = args.dflash_image
    stage = "dflash"
    print(f"\n{'#' * 70}\n# stage: {stage}\n# hf_path={DFLASH_HF_PATH} image={dflash_image} gpu_util={gpu_util}\n"
          f"# NOT NVIDIA's checkpoint -- see module docstring point 3.\n{'#' * 70}")
    warn_if_gpu_util_risky(stage, gpu_util)

    SUMMARY[stage] = {"deployed": False, "tps": None, "cold_tps": None, "boot_log_hit": None, "by_prompt": {}}
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
    docker_env = [
        "-e", "VLLM_ALLOW_LONG_MAX_MODEL_LEN=1",
        "-e", "TORCH_MATMUL_PRECISION=high",
        "-e", "PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True",
        "-e", "VLLM_TEST_FORCE_FP8_MARLIN=0",
        "-e", "VLLM_USE_FLASHINFER_SAMPLER=1",
    ]
    if args.dflash_force_flashinfer_moe:
        # Off by default now -- see this run's own boot log: "Unknown vLLM
        # environment variable detected: VLLM_NVFP4_GEMM_BACKEND". A sibling
        # AEON model's docs (Qwen3.6-27B) say explicitly not to force this
        # AWAY from CUTLASS ("do NOT force VLLM_NVFP4_GEMM_BACKEND=marlin --
        # that's the workaround for stock vLLM builds where CUTLASS is
        # broken on SM121"), implying CUTLASS -- what this image already
        # auto-selected -- is the intended default, not a fallback to
        # correct. Forcing this var was very possibly solving a problem
        # that didn't exist. Kept as an explicit opt-in flag rather than
        # deleted outright, in case a different image tag does recognize it.
        docker_env += ["-e", "VLLM_USE_FLASHINFER_MOE_FP4=0", "-e", "VLLM_NVFP4_GEMM_BACKEND=flashinfer-cutlass"]

    docker_cmd = [
        "docker", "run", "-d", "--init",
        "--name", ContainerRole.STANDALONE,
        "--gpus", "all", "--ipc=host", "--net=host",
    ] + docker_env + [
        "-v", vol_mount,
        "--entrypoint", "vllm",
        dflash_image,
    ] + vllm_serve_args

    try:
        # docker run pulls a missing image inline, synchronously, before
        # doing anything else -- and this image has never been pulled to
        # this cluster before. A 60s timeout sized for "launch an
        # already-cached container" is not sized for "first-time pull of a
        # multi-GB image, then launch." Pulling explicitly first, with its
        # own generous timeout, means a slow pull and a genuine launch
        # failure show up as two different, distinguishable failures
        # instead of one undifferentiated "Command execution timed out."
        print(f"    pulling {dflash_image} on {host} if not already cached (first pull can take a while)...")
        pull_res = run_ssh(ip, user, ["docker", "pull", dflash_image], timeout=1800)
        pulled_ok = record(f"[{stage}] docker pull succeeds", pull_res.returncode == 0,
                            "" if pull_res.returncode == 0 else pull_res.stderr.strip()[:400])
        if not pulled_ok:
            return False

        run_res = run_ssh(ip, user, docker_cmd, timeout=90)
        launched_ok = record(f"[{stage}] docker run launches", run_res.returncode == 0,
                              "" if run_res.returncode == 0 else run_res.stderr.strip()[:400])
        if not launched_ok:
            return False

        print(f"    container started; independently polling /health (up to {wait_timeout}s)...")
        healthy = wait_for_health(ip, cfg.ports["vllm_api"], wait_timeout)
        SUMMARY[stage]["deployed"] = healthy
        record(f"[{stage}] /health confirmed ready (independent poll)", healthy)

        try:
            save_container_logs(stage, host, ip, user)
        except Exception as exc:
            print(f"    [{stage}] log capture itself failed (non-fatal): {exc}")

        if not healthy:
            return False

        check_boot_log(stage, host, ip, user)
        SUMMARY[stage]["boot_log_hit"] = any(
            label.startswith(f"[{stage}] boot log contains") and passed
            for label, passed, _ in RESULTS
        )

        print(f"\n--- [{stage}] benchmark suite (via benchmark.py) ---")
        any_bench_ok = run_benchmark_suite(stage, ip, "dflash-scratch", args)
        if not any_bench_ok:
            try:
                save_container_logs(f"{stage}-post-benchmark-failure", host, ip, user)
            except Exception as exc:
                print(f"    [{stage}] post-failure log capture itself failed (non-fatal): {exc}")
        return healthy
    finally:
        if not args.keep:
            print(f"\n--- tearing down after '{stage}' ---")
            teardown()
        else:
            print(f"\n--keep set: leaving '{stage}' deployed (if it deployed).")


def main() -> int:
    """
    Thin wrapper: sets up a full stdout+stderr transcript of this run
    (LOG_DIR/run-<timestamp>.log) before anything else happens -- including
    before argparse runs, since an argparse error (a bad flag, a missing
    value) prints to stderr, not stdout, and a stdout-only tee would have
    missed exactly the kind of mistake that already happened once on this
    script (the --image flag not existing yet). Restores real
    stdout/stderr and closes the file in `finally`, so this holds even on
    KeyboardInterrupt or an unhandled exception, not just the clean-exit
    path.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    transcript_path = LOG_DIR / f"run-{time.strftime('%Y%m%d-%H%M%S')}.log"
    transcript_file = open(transcript_path, "w")
    real_stdout, real_stderr = sys.stdout, sys.stderr
    sys.stdout = _Tee(real_stdout, transcript_file)
    sys.stderr = _Tee(real_stderr, transcript_file)
    print(f"[transcript] full output of this run is being saved to {transcript_path}")
    try:
        return _run(sys.argv[1:])
    finally:
        sys.stdout, sys.stderr = real_stdout, real_stderr
        transcript_file.close()


def _run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stage", choices=["baseline", "mtp", "dflash", "all"], default="all",
                         help="Which stage(s) to run. 'all' runs baseline -> mtp -> dflash in order, "
                              "skipping dflash if mtp did not come up healthy.")
    parser.add_argument("--host", default=None, help="Default: cluster's primary host (%s)" % PRIMARY_HOST)
    parser.add_argument("--image", default=None,
                         help=f"Override the image for baseline/mtp (default: {EUGR_IMAGE}). Does NOT affect "
                              f"dflash, which always uses its own AEON image regardless of this flag.")
    parser.add_argument("--gpu-util", type=float, default=None, help=f"baseline/mtp gpu_util. Default: {DEFAULT_GPU_UTIL} (cluster's gpu_util_ceiling)")
    parser.add_argument("--dflash-gpu-util", type=float, default=DEFAULT_DFLASH_GPU_UTIL, help=f"Default: {DEFAULT_DFLASH_GPU_UTIL} (AEON's own recommended production value)")
    parser.add_argument("--dflash-image", default=DFLASH_IMAGE,
                         help=f"Default: {DFLASH_IMAGE}. AEON's own ':latest' has moved at least twice since "
                              f"their 144 tok/s single-stream claim was published (v0.23.0-dflashfix -> "
                              f"v0.24.0 -> ...) and now serves their whole model fleet off one shared image -- "
                              f"try --dflash-image ghcr.io/aeon-7/aeon-vllm-ultimate:2026-06-18-v0.23.0-dflashfix "
                              f"to test against the actual build that number was measured on.")
    parser.add_argument("--dflash-force-flashinfer-moe", action="store_true",
                         help="Off by default. Forces VLLM_NVFP4_GEMM_BACKEND=flashinfer-cutlass, which this "
                              "image's own envs.py reports as an unrecognized variable (confirmed via boot log) "
                              "-- kept as an opt-in, not applied automatically, since a sibling AEON model's docs "
                              "suggest the auto-selected VLLM_CUTLASS backend may already be the intended default, "
                              "not something to correct.")
    parser.add_argument("--num-speculative-tokens", type=int, default=2, help="Default: 2 (eugr's own tuned value for mtp; also used for dflash's n if that stage runs)")
    parser.add_argument("--max-model-len", type=int, default=32768)
    parser.add_argument("--max-tokens", type=int, default=256, help="max_tokens passed to benchmark.py")
    parser.add_argument("--prompt", default=None,
                         help="A single raw prompt string, overriding --prompts entirely. Prefer --prompts "
                              "for named, reusable presets -- this is for a genuinely one-off custom string.")
    parser.add_argument("--prompts", default=None,
                         help=f"Comma-separated preset names to run in a suite against the SAME deployed "
                              f"container (no redeploy between them), or 'all' for every preset. Known: "
                              f"{list(PROMPT_PRESETS)}. Default: just 'default' (benchmark.py's own built-in "
                              f"prompt). Exists because throughput here is genuinely workload-dependent -- a "
                              f"single fixed prompt measured a 49.5 vs 103.8 tok/s swing on the identical "
                              f"dflash config, purely from prompt choice. Ignored if --prompt is also given.")
    parser.add_argument("--keep", action="store_true", help="Skip teardown and leave scratch recipe file(s) on disk for follow-up poking")
    parser.add_argument("--repeats", type=int, default=1,
                         help="Run each selected stage this many times (each a fully independent fresh "
                              "deploy+benchmark+teardown, not repeated benchmark.py calls against one "
                              "already-running container -- boot-to-boot variance is exactly what earlier "
                              "runs showed matters, e.g. mtp's 47.8-54.8 tok/s spread across identical "
                              "config). Prints an aggregate (mean/range per prompt) after all repeats "
                              "instead of leaving that arithmetic to be done by hand across chat turns. "
                              "Default: 1 (today's behavior, unchanged).")
    parser.add_argument("--wait-timeout", type=int, default=None, help="Seconds to wait for /health. Default: cluster tuning.deploy_wait_timeout_sec")
    args = parser.parse_args(argv)

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
    repeat_results: dict[str, list[dict]] = {}

    for stage in stages:
        if stage == "dflash" and args.stage == "all" and not SUMMARY.get("mtp", {}).get("deployed"):
            print(
                "\n[!] Skipping 'dflash' -- 'mtp' did not come up healthy. Run `--stage dflash` "
                "explicitly if you want to try it anyway despite mtp's failure (it's a fully "
                "independent pipeline, see module docstring point 2 -- it doesn't depend on mtp "
                "having worked), otherwise fix mtp first."
            )
            break
        repeat_results[stage] = []
        for repeat_idx in range(1, args.repeats + 1):
            if args.repeats > 1:
                print(f"\n{'@' * 70}\n@ {stage} -- repeat {repeat_idx}/{args.repeats}\n{'@' * 70}")
            try:
                if stage in ("baseline", "mtp"):
                    run_stage1(stage, args, cfg, host, ip, user)
                elif stage == "dflash":
                    run_dflash_stage(args, cfg, host, ip, user)
            except KeyboardInterrupt:
                print(f"\n[!] Interrupted during '{stage}' (repeat {repeat_idx}/{args.repeats}) -- tearing down before exit.")
                if not args.keep:
                    teardown()
                raise
            # Snapshot SUMMARY[stage] before the next repeat overwrites it --
            # run_stage1()/run_dflash_stage() both reset SUMMARY[stage] at
            # their own start, so without this copy every repeat but the
            # last would be lost.
            repeat_results[stage].append(copy.deepcopy(SUMMARY.get(stage, {})))

    if args.repeats > 1:
        print(f"\n{'=' * 70}\nAGGREGATE ACROSS {args.repeats} REPEATS\n{'=' * 70}")
        for stage in stages:
            runs = repeat_results.get(stage, [])
            if not runs:
                continue
            prompt_names: list[str] = []
            for r in runs:
                for name in (r.get("by_prompt") or {}):
                    if name not in prompt_names:
                        prompt_names.append(name)
            n_deployed = sum(1 for r in runs if r.get("deployed"))
            print(f"  {stage}: {n_deployed}/{len(runs)} repeats deployed successfully")
            for name in prompt_names:
                warms = [
                    r["by_prompt"][name]["warm"]
                    for r in runs
                    if r.get("by_prompt", {}).get(name, {}).get("warm") is not None
                ]
                if not warms:
                    print(f"    {name:12s} no successful runs")
                    continue
                mean = sum(warms) / len(warms)
                values_str = ", ".join(f"{w:.1f}" for w in warms)
                print(f"    {name:12s} n={len(warms)} mean={mean:.1f} tok/s  range={min(warms):.1f}-{max(warms):.1f}  values=[{values_str}]")

    print(f"\n{'=' * 70}\nSUMMARY (last repeat only -- see AGGREGATE above if --repeats > 1)\n{'=' * 70}")
    for stage in stages:
        s = SUMMARY.get(stage, {})
        by_prompt = s.get("by_prompt") or {}
        print(f"  {stage:10s} deployed={s.get('deployed')!s:5s} boot_log_hit={s.get('boot_log_hit')!s:5s}")
        if len(by_prompt) > 1:
            for name, vals in by_prompt.items():
                warm_str = f"{vals['warm']:.1f}" if vals.get("warm") is not None else "n/a"
                cold_str = f"{vals['cold']:.1f}" if vals.get("cold") is not None else "n/a"
                clears_55 = " (>= 55 tok/s)" if vals.get("warm") and vals["warm"] >= 55 else ""
                print(f"    {name:12s} warm={warm_str:>6s} tok/s cold={cold_str:>6s} tok/s{clears_55}")
        else:
            warm_str = f"{s['tps']:.1f}" if s.get("tps") is not None else "n/a"
            cold_str = f"{s['cold_tps']:.1f}" if s.get("cold_tps") is not None else "n/a"
            clears_55 = " (>= 55 tok/s)" if s.get("tps") and s["tps"] >= 55 else ""
            print(f"    warm={warm_str:>6s} tok/s cold={cold_str:>6s} tok/s{clears_55}")

    n_pass = sum(1 for _, ok, _ in RESULTS if ok)
    n_total = len(RESULTS)
    print(f"\n{n_pass}/{n_total} checks passed.")
    return 0 if n_pass == n_total else 1


if __name__ == "__main__":
    sys.exit(main())
