#!/usr/bin/env python3
"""
tests/ab_test.py -- fire-and-forget A/B comparison rig for this cluster.

    docker exec -it dgx-orchestrator-api python3 tests/ab_test.py \\
        --variant-a <recipe-or-preset> --variant-b <recipe-or-preset>

Deploys, health-checks, boot-log-scans, and benchmarks one or two
"variants" -- each independently either (a) an existing recipe from the
real catalog (recipes/local/*.yaml, recipes/eugr/*.yaml), deployed via the
real dgx-orchestrator.py CLI path exactly as the dashboard would (mods and
all, no scratch file), (b) that same catalog recipe (or a small built-in
KNOWN_PRESETS bundle -- see below) with specific fields overridden via
--a-*/--b-* flags, synthesized into a throwaway scratch recipe, or (c) a
raw `docker run` with a custom --entrypoint, for images whose default
entrypoint isn't the stock vLLM API server (see "Structural constraint"
below). --variant-b is optional -- give only --variant-a to profile a
single recipe; give both to run the identical sweep against each and get
a side-by-side comparison at the end.

Which shape a `--variant-X NAME` resolves to is auto-detected, in this
order:
  1. NAME matches a KNOWN_PRESETS key (a handful of Gemma4 NVFP4 bundles
     carried over from this script's original, narrower purpose -- see
     KNOWN_PRESETS below).
  2. NAME matches a recipe in the real catalog (load_recipes()). If no
     --X-* override flags are also given, this deploys the recipe
     EXACTLY as it exists on disk (full mods pipeline, shares its
     historical_tps ledger entry with normal dashboard deploys of the
     same recipe). If any --X-* flags ARE given, that recipe is used as
     the BASE and the given fields are overridden on top of it, written
     out as a scratch recipe -- e.g. "same recipe, but gpu_util=0.7"
     without retyping hf_path/image/vllm_args by hand.
  3. NAME matches neither, but --X-hf-path (or other --X-* flags) are
     given: a from-scratch ad-hoc variant. --X-hf-path is required in
     this case (there is no base to inherit it from).
  4. --X-entrypoint is given (with any of the above, or on its own):
     forces the raw-docker-run path regardless of what NAME resolved to.
     Requires --X-image and --X-serve-args; does NOT go through
     write_scratch_recipe()/the CLI deploy path at all, so --X-vllm-args
     and --X-mods have no effect in this mode (a warning is printed if
     given).

Structural constraint, not a limitation of this script: the real deploy
path (_execute_deployment_impl() in dgx-orchestrator.py) always launches
`python3 -m vllm.entrypoints.openai.api_server` against an image's
DEFAULT entrypoint -- there is no per-recipe entrypoint override anywhere
in the recipe schema. Any variant whose image needs a different
entrypoint (AEON's DFlash image being the original example) can only ever
go through the raw-docker-run path (c) above, permanently, until/unless
the recipe schema itself grows an entrypoint field. This script cannot
paper over that with cleverer data modeling.

KNOWN_PRESETS carries this script's original three Gemma4 26B-A4B NVFP4
comparisons forward as convenience bundles (not real catalog recipes --
resolved in-process, same as the from-scratch ad-hoc path):

  gemma4-baseline -- nvidia/Gemma-4-26B-A4B-NVFP4, this cluster's eugr
                      image, no mod, no speculative decoding.
  gemma4-mtp      -- same checkpoint/image + Gemma 4's native MTP
                      speculative decoding (google/gemma-4-26B-A4B-it-
                      assistant drafter, --num-speculative-tokens default
                      2 -- eugr's own tuned value; spark-vllm-docker
                      issue #343 measured 54.9-56 tok/s single-stream
                      this way).
  gemma4-dflash   -- AEON-7's separate pipeline: their own "uncensored"
                      checkpoint (NOT NVIDIA's official weights), their
                      own image, their own DFlash drafter, raw-docker-run
                      path (see "Structural constraint" above). 144-158
                      tok/s single-stream claimed on AEON's own hardware.
                      AEON's checkpoint is a different quantization
                      format (compressed-tensors, not ModelOpt) with its
                      own content-moderation posture -- not a drop-in
                      speed upgrade to the official weights. AEON-7's own
                      production notes for gpu_util on this exact model/
                      hardware say "above ~0.8 the shared LPDDR5X pool
                      page-thrashes and stalls the box, and even 0.85
                      stalls" -- that's the empirical basis for this
                      script's GPU_UTIL_STALL_WARNING_THRESHOLD default
                      (0.8), extrapolated as a general caution for any
                      variant on this hardware family, not a guarantee
                      that holds for every model.

Honesty notes carried over from earlier revisions:
  - The boot-log check is a keyword scan (marlin/nvfp4/fusedmoe/modelopt/
    mtp/dflash/cutlass), not a confirmed exact-line match -- printed for
    you to eyeball, not asserted as a silent pass.
  - Throughput comes from the cluster's own benchmark.py (3-pass, cold +
    2 warm, decode_tps), shelled out to exactly the way
    _run_benchmark_worker() does.

Revision history:
  - Originally a 3-stage Gemma4-specific smoke test (baseline/mtp/dflash
    hardcoded as separate functions).
  - Refactored to a data-driven STAGE_SPECS table + one run_stage() (same
    3 Gemma4 stages, now literals in a dict instead of duplicated code).
  - Generalized to an A/B rig for ANY one or two variants -- named
    catalog recipes, ad-hoc overrides, or raw-docker -- with the 3
    original Gemma4 stages preserved as KNOWN_PRESETS rather than the
    only thing this script can run.
  - Renamed tests/metest.py -> tests/ab_test.py to match what it actually
    does now. No functional change from the previous revision.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# This script lives in tests/, one level below the repo root -- when invoked
# as `python3 tests/ab_test.py`, Python puts tests/ on sys.path[0], not the
# repo root, so `common` is not importable as-is (dgx-orchestrator.py
# doesn't hit this because it lives at the repo root itself). Mirrors
# common/config.py's own BASE_DIR resolution -- respects the BASE_DIR env
# var docker-compose.yml sets (=/app in the orchestrator container) and
# falls back to computing it from this file's own location otherwise,
# rather than hardcoding /app.
_REPO_ROOT = Path(os.getenv("BASE_DIR", Path(__file__).resolve().parent.parent))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from common.config import BASE_DIR, legacy_hosts_dict, load_cluster_config
from common.constants import ContainerRole
from common.recipes import load_recipes
from common.ssh import run_ssh

HOSTS = legacy_hosts_dict()
PRIMARY_HOST = next(iter(HOSTS), None)
# Mirrors dgx-orchestrator.py's own SECONDARY_HOST resolution exactly
# (same "second dict key, or fall back to primary if there's only one
# host" logic) -- kept in sync deliberately, since a 2-node deploy's
# target_hosts there is [PRIMARY_HOST, SECONDARY_HOST], and the pre-pull
# fix below needs to touch the same hosts the deploy step will actually
# use, not redefine that set independently. TOMBSTONES.md #106.
SECONDARY_HOST = list(HOSTS.keys())[1] if len(HOSTS) > 1 else PRIMARY_HOST

# Legacy constants -- only referenced by KNOWN_PRESETS now. A from-scratch
# ad-hoc or raw-docker variant sources every one of these from --a-*/--b-*
# flags instead; nothing here is a script-wide default for those anymore.
BASE_VLLM_ARGS = "--quantization modelopt --kv-cache-dtype fp8 --moe-backend marlin --trust-remote-code"
NVIDIA_HF_PATH = "nvidia/Gemma-4-26B-A4B-NVFP4"
EUGR_IMAGE = "eugr/spark-vllm-b12x:latest"
MTP_ASSISTANT = "google/gemma-4-26B-A4B-it-assistant"

DFLASH_IMAGE = "ghcr.io/aeon-7/aeon-vllm-ultimate:latest"
DFLASH_HF_PATH = "AEON-7/Gemma-4-26B-A4B-it-Uncensored-NVFP4"
DFLASH_DRAFTER = "z-lab/gemma-4-26B-A4B-it-DFlash"

# Named prompt presets, roughly matching AEON's own published category
# breakdown ("144 tok/s single (Coding), up to 158 (Extraction)") plus a
# "creative" category at the other end. Exists because of a real, measured
# finding on the dflash preset: the identical config (n=10, pinned
# v0.23.0-dflashfix image) swung from 49.5 to 103.8 tok/s warm purely from
# swapping the prompt from a generic technical-overview request to a
# coding task -- workload-dependence isn't Gemma4/dflash-specific, it's a
# property of speculative decoding acceptance rates generally, so these
# presets stay useful for any variant being compared here, not just the
# preset they were first measured on. "default" (None) leaves
# benchmark.py's own built-in prompt untouched.
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


def warn_if_gpu_util_risky(label: str, gpu_util: float) -> None:
    if gpu_util > GPU_UTIL_STALL_WARNING_THRESHOLD:
        print(
            f"[!] [{label}] gpu_util={gpu_util} requested (> {GPU_UTIL_STALL_WARNING_THRESHOLD}). "
            f"The empirical basis for this threshold is AEON-7's own production notes for Gemma4 "
            f"NVFP4 on this exact hardware family (GB10's shared LPDDR5X pool page-thrashes above "
            f"~0.8, and even 0.85 stalls) -- extrapolated here as a general caution for any variant "
            f"on this hardware, not a guarantee that holds for every model. Proceeding because it "
            f"was requested explicitly, not because it's recommended -- watch for a hung/unresponsive "
            f"host, not just a failed deploy, if this variant misbehaves."
        )


def _safe_label(label: str) -> str:
    """Filesystem/tag-safe rendering of a variant label, for scratch recipe filenames and log filenames."""
    return re.sub(r"[^A-Za-z0-9._-]", "-", label)


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


def write_scratch_recipe(label: str, hf_path: str, image: str | None, gpu_util: float, max_model_len: int,
                          vllm_args: str, mods: list[str]) -> tuple[Path, str]:
    recipe_name = f"_scratch-{_safe_label(label)}"
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
    # Bare directory names only (validated by RecipeConfig/_validate_mod_name
    # at load time regardless -- this is just how they're serialized). Empty
    # list is the strict-no-op case every recipe used before mods existed.
    lines.append(f"mods: [{', '.join(mods)}]" if mods else "mods: []")
    lines += [
        "",
        "notes: >",
        f"  Scratch recipe generated by tests/ab_test.py, variant "
        f"'{label}'. Regenerated on every run; deleted afterward unless --keep "
        f"is passed. Not a production recipe.",
        "",
        "topologies:",
        "  1_node:",
        f"    max_model_len: {max_model_len}",
        "    tp_size: 1",
        "    pp_size: 1",
        "    env_vars: []",
        # Block scalar (>-), not a quoted flow scalar: vllm_args for a
        # speculative-decoding variant embeds a JSON --speculative-config
        # value containing double quotes, and wrapping that in
        # `vllm_args: "..."` breaks the YAML parse the instant it hits the
        # first embedded ". Confirmed via a direct yaml.safe_load() repro
        # before shipping this fix, not just reasoned about -- the
        # double-quoted version produced "expected <block end>, but found
        # '<scalar>'" and silently dropped the recipe from the catalog
        # (surfaced as "Model ... not defined in catalog", nothing pointing
        # at the actual YAML syntax error). A block scalar has no
        # quote-delimiter to collide with -- verified round-trips to the
        # exact original string and shlex.split()s into the correct argv,
        # single-line content only (this codebase's own vllm_args values
        # always are).
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
    path = LOG_DIR / f"{_safe_label(label)}-{host}-{ts}.log"
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


def deploy_via_recipe(label: str, recipe_name: str, host: str, ip: str, port: int, wait_timeout: int, nodes: int = 1) -> bool:
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

    nodes is forwarded straight to `cli deploy --nodes`; only meaningful
    for a pure named-recipe passthrough whose catalog entry actually
    defines a "2_node" topology -- every scratch recipe this script writes
    itself only ever has a "1_node" topology (see write_scratch_recipe()).
    """
    print(f"\n=== [{label}] deploy (recipe {recipe_name}, nodes={nodes}) ===")
    deploy_cmd = [sys.executable, "dgx-orchestrator.py", "cli", "deploy",
                  "--model", recipe_name, "--nodes", str(nodes), "--head", host]
    t0 = time.time()
    try:
        res = subprocess.run(deploy_cmd, capture_output=True, text=True, cwd=str(BASE_DIR), timeout=300)
    except subprocess.TimeoutExpired:
        record(f"[{label}] deploy command completes", False, "subprocess timed out after 300s (no --wait passed, so this should return in well under a minute -- a hang means the docker-run SSH call itself is stuck)")
        return False
    elapsed = time.time() - t0

    try:
        payload = _extract_last_json_object(res.stdout)
    except ValueError as exc:
        record(f"[{label}] deploy command completes", False, f"could not parse CLI output: {exc}")
        return False

    launched_ok = res.returncode == 0 and payload.get("status") == "success"
    record(f"[{label}] deploy command reports success (no immediate crash)", launched_ok,
           f"{elapsed:.0f}s" if launched_ok else payload.get("message", res.stderr.strip()[-400:]))
    if not launched_ok:
        return False

    print(f"    deploy command returned; independently polling /health (up to {wait_timeout}s)...")
    healthy = wait_for_health(ip, port, wait_timeout)
    record(f"[{label}] /health confirmed ready (independent poll)", healthy)
    return healthy


def check_boot_log(label: str, host: str, ip: str, user: str) -> None:
    print(f"\n--- [{label}] boot log scan ({host}) ---")
    ps_res = run_ssh(ip, user, ["docker", "ps", "-a", "--format", "{{.Names}}"], timeout=10)
    containers = [c.strip() for c in ps_res.stdout.splitlines() if c.strip() in (ContainerRole.STANDALONE, ContainerRole.HEAD, ContainerRole.WORKER)]
    if not containers:
        record(f"[{label}] container present on {host} for log check", False, "no vllm container found")
        return

    log_res = run_ssh(ip, user, ["docker", "logs", "--tail", "800", containers[0]], timeout=15)
    log_text = (log_res.stdout or "") + (log_res.stderr or "")
    keywords = ["marlin", "nvfp4", "fusedmoe", "modelopt", "mtp", "dflash", "cutlass"]
    hits = [kw for kw in keywords if kw in log_text.lower()]
    record(f"[{label}] boot log contains a relevant backend/decoding keyword (scan, not a confirmed exact-line match)",
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
    next variant -- unlike the orchestrator's own /api/benchmark path,
    which backgrounds it.

    prompt=None uses benchmark.py's own default (a 200-word technical-
    overview request) -- worth overriding when chasing a claim that was
    itself workload-specific. Throughput is genuinely workload-dependent
    for speculative-decoding variants (acceptance-rate-driven), so a
    single fixed prompt can sit in a very different regime than whatever
    workload a published number was measured on.
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


def run_benchmark_suite(side: str, label: str, ip: str, model_key: str, args) -> bool:
    """
    Runs benchmark.py once per selected prompt preset against the SAME
    already-deployed container -- no redeploy between prompts. A redeploy
    (image pull, weight load, torch.compile, CUDA graph capture) costs
    minutes per prompt and none of that changes between prompts on an
    already-running server; only the request itself differs.

    Exists because of a real, measured finding (see module docstring):
    comparing two variants on a single fixed prompt risks comparing them
    on a workload that happens to favor one over the other for reasons
    that have nothing to do with which variant is actually faster.

    Populates SUMMARY[side]["by_prompt"] with one entry per preset
    actually run, and mirrors the FIRST preset's result into
    SUMMARY[side]["tps"]/["cold_tps"] for backward compatibility with the
    final summary table and every single-number comparison.
    """
    prompts = resolve_prompts(args)
    SUMMARY[side]["by_prompt"] = {}
    any_ok = False
    for name, prompt_text in prompts:
        plabel = f"[{label}:{name}]"
        print(f"\n--- {plabel} benchmark (3-pass, via benchmark.py) ---")
        # model_key suffixed per-prompt so benchmark_ledger.csv rows stay
        # distinguishable -- otherwise multiple prompts under one run
        # would silently share a ledger key and overwrite each other's
        # historical_tps lookup.
        bench, detail = run_real_benchmark(ip, f"{model_key}-{name}", args.max_tokens, prompt_text)
        if bench:
            any_ok = True
            SUMMARY[side]["by_prompt"][name] = {"warm": bench["warm_decode_tps"], "cold": bench["cold_decode_tps"]}
            cold_str = f"{bench['cold_decode_tps']:.1f}" if bench["cold_decode_tps"] is not None else "n/a"
            record(f"{plabel} benchmark.py succeeds", True,
                   f"warm {bench['warm_decode_tps']:.1f} tok/s (TTFT {bench['warm_ttft']:.2f}s), cold {cold_str} tok/s")
        else:
            SUMMARY[side]["by_prompt"][name] = {"warm": None, "cold": None}
            record(f"{plabel} benchmark.py succeeds", False, detail)

    first_name = prompts[0][0]
    first = SUMMARY[side]["by_prompt"].get(first_name, {})
    SUMMARY[side]["tps"] = first.get("warm")
    SUMMARY[side]["cold_tps"] = first.get("cold")
    return any_ok


def teardown() -> None:
    subprocess.run([sys.executable, "dgx-orchestrator.py", "cli", "teardown"],
                    cwd=str(BASE_DIR), capture_output=True, text=True, timeout=120)


# ---------------------------------------------------------------------------
# KNOWN_PRESETS -- this script's original 3 Gemma4 NVFP4 comparisons,
# carried forward as convenience bundles. Each is a function of `args`
# (not a static dict) only because gemma4-mtp/gemma4-dflash's speculative
# config embeds --num-speculative-tokens, a global CLI flag. Resolved once
# per side, then treated identically to a from-scratch ad-hoc spec (a
# preset name can still be layered with --a-*/--b-* overrides on top).
# ---------------------------------------------------------------------------

def _preset_gemma4_baseline(args) -> dict:
    return dict(hf_path=NVIDIA_HF_PATH, image=EUGR_IMAGE, gpu_util=DEFAULT_GPU_UTIL,
                vllm_args=BASE_VLLM_ARGS, mods=[], max_model_len=None, uses_recipe_path=True)


def _preset_gemma4_mtp(args) -> dict:
    spec_cfg = json.dumps({"method": "mtp", "model": MTP_ASSISTANT, "num_speculative_tokens": args.num_speculative_tokens})
    return dict(hf_path=NVIDIA_HF_PATH, image=EUGR_IMAGE, gpu_util=DEFAULT_GPU_UTIL,
                vllm_args=f"{BASE_VLLM_ARGS} --speculative-config '{spec_cfg}'",
                mods=[], max_model_len=None, uses_recipe_path=True)


def _preset_gemma4_dflash(args) -> dict:
    spec_cfg = json.dumps({
        "method": "dflash", "model": DFLASH_DRAFTER,
        "num_speculative_tokens": args.num_speculative_tokens, "attention_backend": "flash_attn",
    })

    def build_serve_args(port: int, max_model_len: int, gpu_util: float) -> list[str]:
        # port/max_model_len/gpu_util aren't known until resolve_variant()
        # has cfg/the final gpu_util in hand -- returned as a closure
        # (not a static list + later .append()) so every flag lands at its
        # ORIGINAL argv position, not tacked on at the end. Order has no
        # functional effect on vLLM's own argparse-based CLI, but this
        # keeps the construction byte-for-byte identical to the
        # pre-generalization script rather than merely equivalent.
        return [
            "serve", DFLASH_HF_PATH,
            "--served-model-name", "gemma4-aeon-uncensored",
            "--host", "0.0.0.0", "--port", str(port),
            "--tensor-parallel-size", "1",
            "--dtype", "auto",
            "--quantization", "compressed-tensors",
            "--linear-backend", "flashinfer_cutlass",
            "--moe-backend", "cutlass",
            "--attention-backend", "triton_attn",
            "--max-model-len", str(max_model_len),
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

    return dict(hf_path=DFLASH_HF_PATH, image=DFLASH_IMAGE, gpu_util=DEFAULT_DFLASH_GPU_UTIL,
                uses_recipe_path=False, entrypoint_override="vllm",
                vllm_serve_args_builder=build_serve_args,
                docker_env=[
                    "-e", "VLLM_ALLOW_LONG_MAX_MODEL_LEN=1",
                    "-e", "TORCH_MATMUL_PRECISION=high",
                    "-e", "PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True",
                    "-e", "VLLM_TEST_FORCE_FP8_MARLIN=0",
                    "-e", "VLLM_USE_FLASHINFER_SAMPLER=1",
                ],
                max_model_len=None)


KNOWN_PRESETS = {
    "gemma4-baseline": _preset_gemma4_baseline,
    "gemma4-mtp": _preset_gemma4_mtp,
    "gemma4-dflash": _preset_gemma4_dflash,
}


def resolve_variant(side: str, args, cfg) -> dict | None:
    """
    Auto-detects and fully resolves one side ('a' or 'b') into a spec dict
    consumed by run_stage(). Returns None if this side was not requested
    at all (no --variant-X and no --X-* override flags). See module
    docstring for the resolution order.
    """
    name = getattr(args, side)
    ov = {
        "hf_path": getattr(args, f"{side}_hf_path"),
        "image": getattr(args, f"{side}_image"),
        "gpu_util": getattr(args, f"{side}_gpu_util"),
        "max_model_len": getattr(args, f"{side}_max_model_len"),
        "vllm_args": getattr(args, f"{side}_vllm_args"),
        "mods": getattr(args, f"{side}_mods"),
        "nodes": getattr(args, f"{side}_nodes"),
        "entrypoint": getattr(args, f"{side}_entrypoint"),
        "serve_args": getattr(args, f"{side}_serve_args"),
        "docker_env": getattr(args, f"{side}_docker_env"),
    }
    # "nodes" is deliberately excluded here: --{side}-nodes alone (no other
    # --{side}-* override) must still qualify as a pure named-recipe
    # passthrough, or a 2-node-only catalog recipe (e.g. one with no
    # 1_node topology) can never be resolved at all -- catalog_nodes would
    # default to 1, topo_key would resolve to "1_node", and the recipe
    # lookup would fail outright before nodes is ever consulted again.
    # Combining --{side}-nodes with any REAL override (vllm_args, etc.)
    # still correctly falls through to the ad-hoc branch below, which
    # rejects nodes > 1 there on its own terms. TOMBSTONES.md #105.
    any_override = (any(v is not None for k, v in ov.items() if k not in ("docker_env", "nodes"))
                     or bool(ov["docker_env"]))

    if name is None and not any_override:
        return None  # side not requested

    # --- raw docker-run path: forced by --X-entrypoint, regardless of what
    # NAME resolved to (a preset/catalog match's hf_path/image/gpu_util are
    # NOT inherited here -- raw-docker is deliberately self-contained, see
    # module docstring "Structural constraint"). ---
    if ov["entrypoint"] is not None:
        if ov["vllm_args"] is not None or ov["mods"] is not None:
            print(f"[!] --{side}-vllm-args/--{side}-mods have no effect with --{side}-entrypoint "
                  f"(raw docker-run path does not go through write_scratch_recipe()/the CLI deploy path) "
                  f"-- ignored.")
        if not ov["image"] or not ov["serve_args"]:
            raise SystemExit(f"--{side}-entrypoint requires --{side}-image and --{side}-serve-args "
                              f"(the full argv after the entrypoint, e.g. 'serve MODEL --port 8000 ...') "
                              f"-- raw docker-run has no catalog/recipe to source these from.")
        if ov["nodes"] not in (None, 1):
            raise SystemExit(f"--{side}-nodes is not supported for the raw docker-run path "
                              f"(single host, single container only).")
        label = name or f"variant-{side}"
        docker_env = list(ov["docker_env"] or [])
        docker_env_flags = []
        for kv in docker_env:
            docker_env_flags += ["-e", kv]
        return dict(
            label=label, hf_path=ov["hf_path"], image=ov["image"],
            gpu_util=ov["gpu_util"] if ov["gpu_util"] is not None else DEFAULT_GPU_UTIL,
            max_model_len=ov["max_model_len"] or args.max_model_len,
            uses_recipe_path=False, entrypoint_override=ov["entrypoint"],
            vllm_serve_args=shlex.split(ov["serve_args"]), docker_env=docker_env_flags,
            benchmark_model_key=f"{_safe_label(label)}-scratch", nodes=1,
        )

    # --- recipe-path family: resolve a base (preset, catalog recipe, or
    # neither), then layer any --X-* overrides on top. ---
    base: dict | None = None
    base_kind: str | None = None
    catalog_nodes = ov["nodes"] or 1

    if name in KNOWN_PRESETS:
        base = KNOWN_PRESETS[name](args)
        base_kind = "preset"
        if base.get("uses_recipe_path") is False:
            # gemma4-dflash: raw-docker preset, selected by name alone (no
            # --X-entrypoint needed since the preset already carries one).
            # Overrides here are limited to image/gpu_util/docker_env --
            # vllm_args/mods don't apply, same as the explicit-entrypoint
            # case above.
            if ov["vllm_args"] is not None or ov["mods"] is not None:
                print(f"[!] --{side}-vllm-args/--{side}-mods have no effect on preset {name!r} "
                      f"(raw docker-run path) -- ignored.")
            if ov["nodes"] not in (None, 1):
                raise SystemExit(f"--{side}-nodes is not supported for preset {name!r} (raw docker-run path).")
            label = name
            image = ov["image"] if ov["image"] is not None else base["image"]
            gpu_util = ov["gpu_util"] if ov["gpu_util"] is not None else base["gpu_util"]
            max_model_len = ov["max_model_len"] or args.max_model_len
            serve_args = base["vllm_serve_args_builder"](cfg.ports["vllm_api"], max_model_len, gpu_util)
            docker_env_flags = list(base["docker_env"])
            for kv in (ov["docker_env"] or []):
                docker_env_flags += ["-e", kv]
            return dict(
                label=label, hf_path=base["hf_path"], image=image, gpu_util=gpu_util,
                max_model_len=max_model_len, uses_recipe_path=False,
                entrypoint_override=base["entrypoint_override"],
                vllm_serve_args=serve_args, docker_env=docker_env_flags,
                benchmark_model_key=f"{_safe_label(label)}-scratch", nodes=1,
            )
    elif name is not None:
        try:
            recipes = load_recipes()
        except Exception as exc:
            raise SystemExit(f"Could not load recipe catalog to resolve --variant-{side} {name!r}: {exc}")
        recipe_obj = recipes.get(name)
        if recipe_obj is not None:
            topo_key = "2_node" if catalog_nodes == 2 else "1_node"
            if topo_key not in recipe_obj.topologies:
                raise SystemExit(f"Recipe {name!r} has no {topo_key!r} topology (requested via --{side}-nodes).")
            topo = recipe_obj.topologies[topo_key]
            base = dict(hf_path=recipe_obj.hf_path, image=recipe_obj.image, gpu_util=recipe_obj.gpu_util,
                        vllm_args=topo.vllm_args, mods=list(recipe_obj.mods), max_model_len=topo.max_model_len)
            base_kind = "catalog"
        elif not any_override:
            raise SystemExit(
                f"--variant-{side} {name!r} is not a known preset ({list(KNOWN_PRESETS)}) and not a recipe "
                f"in the catalog (recipes/local/ or recipes/eugr/). Pass --{side}-hf-path (and other "
                f"--{side}-* flags) to build an ad-hoc variant instead."
            )

    if base is not None and base_kind == "catalog" and not any_override:
        # Pure named-recipe passthrough: deploy the real recipe exactly as
        # it exists on disk, no scratch file, full mods pipeline, and
        # (since model_key == the recipe name) shares its historical_tps
        # ledger entry with a normal dashboard deploy of the same recipe.
        return dict(label=name, hf_path=base["hf_path"], image=base["image"], gpu_util=base["gpu_util"],
                    mods=base["mods"], uses_recipe_path=True, scratch=False, recipe_name=name, nodes=catalog_nodes)

    # Ad-hoc (from scratch, or a preset/catalog recipe as a base with
    # explicit overrides layered on top -- override wins field-by-field,
    # never merged).
    label = name or f"variant-{side}"
    hf_path = ov["hf_path"] if ov["hf_path"] is not None else (base["hf_path"] if base else None)
    if not hf_path:
        raise SystemExit(f"--{side}-hf-path is required (no --variant-{side} matched a known preset or "
                          f"catalog recipe to inherit hf_path from).")
    image = ov["image"] if ov["image"] is not None else (base["image"] if base else None)
    gpu_util = ov["gpu_util"] if ov["gpu_util"] is not None else (base["gpu_util"] if base else DEFAULT_GPU_UTIL)
    vllm_args = ov["vllm_args"] if ov["vllm_args"] is not None else (base["vllm_args"] if base else "")
    mods = [m.strip() for m in ov["mods"].split(",") if m.strip()] if ov["mods"] is not None else (base["mods"] if base else [])
    max_model_len = ov["max_model_len"] if ov["max_model_len"] is not None else ((base or {}).get("max_model_len") or args.max_model_len)
    if ov["nodes"] not in (None, 1):
        raise SystemExit(f"--{side}-nodes > 1 is only supported for a pure named-recipe passthrough (no "
                          f"--{side}-* overrides); ad-hoc scratch recipes here are always 1-node.")
    return dict(label=label, hf_path=hf_path, image=image, gpu_util=gpu_util, vllm_args=vllm_args, mods=mods,
                max_model_len=max_model_len, uses_recipe_path=True, scratch=True, nodes=1)


def run_stage(side: str, spec: dict, args, cfg, host: str, ip: str, user: str) -> bool:
    """
    Unified deploy+benchmark+teardown driver for one resolved variant spec
    (see resolve_variant()). Branches once, early, on
    spec["uses_recipe_path"], and again on spec.get("scratch", True) within
    the recipe-path branch (pure catalog passthrough vs. a synthesized
    scratch recipe):

      - uses_recipe_path=True, scratch=False: `cli deploy --model <name>`
        directly against an existing catalog recipe -- no scratch file,
        full mods pipeline, same path a dashboard deploy takes.
      - uses_recipe_path=True, scratch=True: write_scratch_recipe() +
        deploy_via_recipe(), for ad-hoc or preset-with-overrides variants.
      - uses_recipe_path=False: a raw `docker run` over SSH with an
        --entrypoint override, for images whose default entrypoint isn't
        the stock vLLM API server.

    The recipe-path and raw-docker-run branches intentionally do NOT
    converge into one identical post-launch flow: the recipe path calls
    save_container_logs() unconditionally after the deploy attempt (even
    on failure, since a container may exist regardless of what the CLI
    reported), while the raw-docker-run path skips log capture entirely if
    `docker run` itself never launched a container (nothing to capture).
    This asymmetry predates this generalization and is preserved
    deliberately, not "fixed," per the same scoping this script's earlier
    refactor used.
    """
    label = spec["label"]
    composed = f"{side}:{label}"
    gpu_util = spec["gpu_util"]
    image = spec["image"] or cfg.default_image
    hf_path = spec.get("hf_path") or "n/a"
    wait_timeout = args.wait_timeout or cfg.tuning.deploy_wait_timeout_sec

    mods_note = f" mods={spec['mods']}" if spec.get("mods") else ""
    path_note = ("raw-docker" if not spec["uses_recipe_path"]
                 else ("catalog passthrough" if not spec.get("scratch", True) else "scratch recipe"))
    print(f"\n{'#' * 70}\n# variant: {composed}  [{path_note}]\n"
          f"# hf_path={hf_path} image={image} gpu_util={gpu_util} nodes={spec.get('nodes', 1)}{mods_note}\n{'#' * 70}")
    warn_if_gpu_util_risky(composed, gpu_util)

    SUMMARY[side] = {"deployed": False, "tps": None, "cold_tps": None, "boot_log_hit": None, "by_prompt": {}, "label": label}

    # Pre-pull before docker run, with a long timeout, applies uniformly to
    # every variant regardless of path -- docker run (and the mod-bake
    # pipeline's own `docker create`) pulls a missing image inline,
    # synchronously, and a timeout sized for "launch an already-cached
    # container" is not sized for "first-time pull of a multi-GB image,
    # then launch." This has bitten for real, more than once, exactly when
    # a never-before-pulled image was involved.
    #
    # Pulls on EVERY host a 2-node deploy will actually use, not just the
    # head -- previously only pulled on `host` (the head), which let
    # spark-3 and spark-4 silently drift to different cached versions of
    # a `:latest` tag (spark-4 gets refreshed here every run; spark-3
    # never does). Ray's own head/worker version check then fails hard
    # the moment the worker tries to join -- confirmed live, 2026-09-03,
    # `RuntimeError: Version mismatch: ... Ray: 2.58.0 ... Ray: 2.57.0`
    # crashing `vllm-worker` on spark-3 immediately after startup, on
    # every single attempt (deterministic, not flaky -- both hosts just
    # had genuinely different bits on disk under the same tag). See
    # TOMBSTONES.md #106.
    pull_hosts = [PRIMARY_HOST, SECONDARY_HOST] if spec.get("nodes", 1) == 2 else [host]
    for pull_host in dict.fromkeys(pull_hosts):  # de-dupe, preserve order (matters if PRIMARY_HOST == SECONDARY_HOST on a single-host dev setup)
        pull_ip = HOSTS[pull_host]["ip"]
        print(f"    pulling {image} on {pull_host} if not already cached (first pull can take a while)...")
        pull_res = run_ssh(pull_ip, user, ["docker", "pull", image], timeout=1800)
        pulled_ok = record(f"[{composed}] docker pull succeeds on {pull_host}", pull_res.returncode == 0,
                            "" if pull_res.returncode == 0 else pull_res.stderr.strip()[:400])
        if not pulled_ok:
            return False

    recipe_path: Path | None = None
    try:
        if spec["uses_recipe_path"]:
            if spec.get("scratch", True) is False:
                recipe_name = spec["recipe_name"]
            else:
                recipe_path, recipe_name = write_scratch_recipe(
                    label, hf_path, spec["image"], gpu_util, spec["max_model_len"], spec["vllm_args"], spec["mods"])
            deployed_ok = deploy_via_recipe(composed, recipe_name, host, ip, cfg.ports["vllm_api"], wait_timeout, nodes=spec.get("nodes", 1))
            model_key = recipe_name
            SUMMARY[side]["deployed"] = deployed_ok

            # Always, launch succeeded or not, health passed or not -- see
            # save_container_logs()'s own docstring for why this can't be
            # conditional on --keep or on anything succeeding first.
            try:
                save_container_logs(composed, host, ip, user)
            except Exception as exc:
                print(f"    [{composed}] log capture itself failed (non-fatal): {exc}")
        else:
            docker_cmd = [
                "docker", "run", "-d", "--init",
                "--name", ContainerRole.STANDALONE,
                "--gpus", "all", "--ipc=host", "--net=host",
            ] + spec["docker_env"] + [
                "-v", cfg.hosts[host].volume_mount,
                "--entrypoint", spec["entrypoint_override"],
                image,
            ] + spec["vllm_serve_args"]

            run_res = run_ssh(ip, user, docker_cmd, timeout=90)
            launched_ok = record(f"[{composed}] docker run launches", run_res.returncode == 0,
                                  "" if run_res.returncode == 0 else run_res.stderr.strip()[:400])
            if not launched_ok:
                return False

            print(f"    container started; independently polling /health (up to {wait_timeout}s)...")
            deployed_ok = wait_for_health(ip, cfg.ports["vllm_api"], wait_timeout)
            model_key = spec.get("benchmark_model_key", f"{_safe_label(label)}-scratch")
            SUMMARY[side]["deployed"] = deployed_ok
            record(f"[{composed}] /health confirmed ready (independent poll)", deployed_ok)

            try:
                save_container_logs(composed, host, ip, user)
            except Exception as exc:
                print(f"    [{composed}] log capture itself failed (non-fatal): {exc}")

            if not deployed_ok:
                return False

        if deployed_ok:
            check_boot_log(composed, host, ip, user)
            SUMMARY[side]["boot_log_hit"] = any(
                lbl.startswith(f"[{composed}] boot log contains") and passed
                for lbl, passed, _ in RESULTS
            )

            print(f"\n--- [{composed}] benchmark suite (via benchmark.py) ---")
            any_bench_ok = run_benchmark_suite(side, composed, ip, model_key, args)
            if not any_bench_ok:
                # A crash triggered BY the benchmark request itself would
                # postdate the snapshot taken right after health passed --
                # grab a second, fresher one now rather than assume the
                # first one already caught it.
                try:
                    save_container_logs(f"{composed}-post-benchmark-failure", host, ip, user)
                except Exception as exc:
                    print(f"    [{composed}] post-failure log capture itself failed (non-fatal): {exc}")
        return deployed_ok
    finally:
        if not args.keep:
            print(f"\n--- tearing down after '{composed}' ---")
            teardown()
            if recipe_path is not None:
                try:
                    recipe_path.unlink(missing_ok=True)
                except OSError:
                    pass
        else:
            if recipe_path is not None:
                print(f"\n--keep set: leaving '{composed}' deployed (if it deployed) and {recipe_path} on disk.")
            else:
                print(f"\n--keep set: leaving '{composed}' deployed (if it deployed).")


def main() -> int:
    """
    Thin wrapper: sets up a full stdout+stderr transcript of this run
    (LOG_DIR/run-<timestamp>.log) before anything else happens -- including
    before argparse runs, since an argparse error (a bad flag, a missing
    value) prints to stderr, not stdout, and a stdout-only tee would have
    missed exactly the kind of mistake that already happened once on this
    script. Restores real stdout/stderr and closes the file in `finally`,
    so this holds even on KeyboardInterrupt or an unhandled exception, not
    just the clean-exit path.
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
    parser.add_argument("--host", default=None, help="Default: cluster's primary host (%s)" % PRIMARY_HOST)

    for side in ("a", "b"):
        S = side.upper()
        parser.add_argument(f"--variant-{side}", dest=side, default=None,
                             help=f"Recipe name or KNOWN_PRESETS key for variant {S} ({list(KNOWN_PRESETS)}). "
                                  f"Combine with --{side}-* flags to override specific fields; omit --{side}-* "
                                  f"entirely to deploy a matching catalog recipe exactly as-is. Omit "
                                  f"--variant-{side} and all --{side}-* flags to skip variant {S} entirely.")
        parser.add_argument(f"--{side}-hf-path", dest=f"{side}_hf_path", default=None,
                             help=f"HF path for variant {S}. Required if --variant-{side} doesn't match a "
                                  f"preset/catalog recipe to inherit it from.")
        parser.add_argument(f"--{side}-image", dest=f"{side}_image", default=None,
                             help=f"Image override for variant {S}. Omitted for a scratch recipe falls back "
                                  f"to cluster_config.yaml's default_image at deploy time.")
        parser.add_argument(f"--{side}-gpu-util", dest=f"{side}_gpu_util", type=float, default=None,
                             help=f"gpu_util override for variant {S}. Default: {DEFAULT_GPU_UTIL} unless inherited from a preset/recipe.")
        parser.add_argument(f"--{side}-max-model-len", dest=f"{side}_max_model_len", type=int, default=None,
                             help=f"max_model_len override for variant {S}. Default: --max-model-len.")
        parser.add_argument(f"--{side}-vllm-args", dest=f"{side}_vllm_args", default=None,
                             help=f"Full vllm_args string override for variant {S} (recipe-path only; no effect with --{side}-entrypoint).")
        parser.add_argument(f"--{side}-mods", dest=f"{side}_mods", default=None,
                             help=f"Comma-separated mod names for variant {S} (recipe-path only; bare directory names under mods/, e.g. gemma4-nvfp4).")
        parser.add_argument(f"--{side}-nodes", dest=f"{side}_nodes", type=int, choices=[1, 2], default=None,
                             help=f"Node count for variant {S}. Only supported for a pure named-recipe passthrough with a 2_node topology; scratch/raw-docker variants are always 1-node.")
        parser.add_argument(f"--{side}-entrypoint", dest=f"{side}_entrypoint", default=None,
                             help=f"Forces the raw docker-run path for variant {S} (e.g. 'vllm'), for images whose default entrypoint isn't the stock vLLM API server. Requires --{side}-image and --{side}-serve-args.")
        parser.add_argument(f"--{side}-serve-args", dest=f"{side}_serve_args", default=None,
                             help=f"Full argv (shlex-split) after the --{side}-entrypoint override, e.g. \"serve MODEL --port 8000 --tensor-parallel-size 1\".")
        parser.add_argument(f"--{side}-docker-env", dest=f"{side}_docker_env", action="append", default=None, metavar="KEY=VAL",
                             help=f"Extra -e KEY=VAL for variant {S}'s docker run (repeatable). Raw-docker: added on top of any preset's own env. Recipe-path scratch: no effect (env_vars come from the recipe topology, not this flag).")

    parser.add_argument("--num-speculative-tokens", type=int, default=2, help="Default: 2 -- only affects the gemma4-mtp/gemma4-dflash KNOWN_PRESETS' speculative-config.")
    parser.add_argument("--max-model-len", type=int, default=32768, help="Default max_model_len for any side that doesn't specify its own.")
    parser.add_argument("--max-tokens", type=int, default=256, help="max_tokens passed to benchmark.py")
    parser.add_argument("--prompt", default=None,
                         help="A single raw prompt string, overriding --prompts entirely. Prefer --prompts "
                              "for named, reusable presets -- this is for a genuinely one-off custom string.")
    parser.add_argument("--prompts", default=None,
                         help=f"Comma-separated preset names to run in a suite against the SAME deployed "
                              f"container (no redeploy between them), or 'all' for every preset. Known: "
                              f"{list(PROMPT_PRESETS)}. Default: just 'default' (benchmark.py's own built-in "
                              f"prompt). Ignored if --prompt is also given.")
    parser.add_argument("--keep", action="store_true", help="Skip teardown and leave scratch recipe file(s) on disk for follow-up poking")
    parser.add_argument("--repeats", type=int, default=1,
                         help="Run each requested variant this many times (each a fully independent fresh "
                              "deploy+benchmark+teardown). Prints an aggregate (mean/range per prompt) after "
                              "all repeats. Default: 1.")
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

    specs: dict[str, dict] = {}
    for side in ("a", "b"):
        spec = resolve_variant(side, args, cfg)
        if spec is not None:
            specs[side] = spec

    if not specs:
        print("[!] Neither --variant-a/--a-* nor --variant-b/--b-* were given -- nothing to run. "
              "Pass at least one side.", file=sys.stderr)
        return 2

    sides = [s for s in ("a", "b") if s in specs]
    repeat_results: dict[str, list[dict]] = {}

    for side in sides:
        spec = specs[side]
        repeat_results[side] = []
        for repeat_idx in range(1, args.repeats + 1):
            if args.repeats > 1:
                print(f"\n{'@' * 70}\n@ {side}:{spec['label']} -- repeat {repeat_idx}/{args.repeats}\n{'@' * 70}")
            try:
                run_stage(side, spec, args, cfg, host, ip, user)
            except KeyboardInterrupt:
                print(f"\n[!] Interrupted during '{side}:{spec['label']}' (repeat {repeat_idx}/{args.repeats}) -- tearing down before exit.")
                if not args.keep:
                    teardown()
                raise
            # Snapshot SUMMARY[side] before the next repeat overwrites it --
            # run_stage() resets SUMMARY[side] at its own start, so without
            # this copy every repeat but the last would be lost.
            repeat_results[side].append(copy.deepcopy(SUMMARY.get(side, {})))

    if args.repeats > 1:
        print(f"\n{'=' * 70}\nAGGREGATE ACROSS {args.repeats} REPEATS\n{'=' * 70}")
        for side in sides:
            runs = repeat_results.get(side, [])
            if not runs:
                continue
            label = specs[side]["label"]
            prompt_names: list[str] = []
            for r in runs:
                for name in (r.get("by_prompt") or {}):
                    if name not in prompt_names:
                        prompt_names.append(name)
            n_deployed = sum(1 for r in runs if r.get("deployed"))
            print(f"  {side}:{label}: {n_deployed}/{len(runs)} repeats deployed successfully")
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
    for side in sides:
        s = SUMMARY.get(side, {})
        label = specs[side]["label"]
        by_prompt = s.get("by_prompt") or {}
        print(f"  {side}:{label:20s} deployed={s.get('deployed')!s:5s} boot_log_hit={s.get('boot_log_hit')!s:5s}")
        if len(by_prompt) > 1:
            for name, vals in by_prompt.items():
                warm_str = f"{vals['warm']:.1f}" if vals.get("warm") is not None else "n/a"
                cold_str = f"{vals['cold']:.1f}" if vals.get("cold") is not None else "n/a"
                print(f"    {name:12s} warm={warm_str:>6s} tok/s cold={cold_str:>6s} tok/s")
        else:
            warm_str = f"{s['tps']:.1f}" if s.get("tps") is not None else "n/a"
            cold_str = f"{s['cold_tps']:.1f}" if s.get("cold_tps") is not None else "n/a"
            print(f"    warm={warm_str:>6s} tok/s cold={cold_str:>6s} tok/s")

    if len(sides) == 2:
        a_summary, b_summary = SUMMARY.get("a", {}), SUMMARY.get("b", {})
        a_label, b_label = specs["a"]["label"], specs["b"]["label"]
        print(f"\n{'=' * 70}\nA vs B: {a_label}  vs  {b_label}\n{'=' * 70}")
        prompt_names = list((a_summary.get("by_prompt") or {}).keys()) or list((b_summary.get("by_prompt") or {}).keys())
        for name in prompt_names:
            a_warm = (a_summary.get("by_prompt", {}).get(name) or {}).get("warm")
            b_warm = (b_summary.get("by_prompt", {}).get(name) or {}).get("warm")
            a_str = f"{a_warm:.1f}" if a_warm is not None else "n/a"
            b_str = f"{b_warm:.1f}" if b_warm is not None else "n/a"
            if a_warm is not None and b_warm is not None and a_warm > 0:
                delta = f"  ({b_warm - a_warm:+.1f} tok/s, {(b_warm / a_warm - 1) * 100:+.1f}%)"
            else:
                delta = ""
            print(f"    {name:12s} a={a_str:>6s} tok/s  b={b_str:>6s} tok/s{delta}")

    n_pass = sum(1 for _, ok, _ in RESULTS if ok)
    n_total = len(RESULTS)
    print(f"\n{n_pass}/{n_total} checks passed.")
    return 0 if n_pass == n_total else 1


if __name__ == "__main__":
    sys.exit(main())
