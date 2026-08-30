#!/usr/bin/env python3
"""
smoke_test_mc.py -- Task MC smoke test.

Exercises the REAL, unmodified project code:
  - common/recipes.py   (Task MA/MB, real pydantic-backed RecipeConfig)
  - common/mods.py      (Task MB, real bake/resolve logic)
  - common/ssh.py       (real run_ssh/resolve_user_identity_key/get_hf_token)
  - dgx-orchestrator.py (Task MC's integration point, this task's deliverable)

Only two modules are faked, because they were never part of any of these
tasks' deliverables and were not supplied: common/config.py (cluster
topology / cluster_config.yaml loader) and common/constants.py (the
ContainerRole name constants). Everything else imported below is your
real project source, loaded from disk unmodified.

No SSH, Docker daemon, or network access is used or required. The only
thing mocked is the actual OS-level process execution inside
subprocess.Popen (used by common/ssh.py's run_ssh()) and subprocess.run
(used by common/mods.py's _scp_to_host()) -- everything ABOVE that layer
(argument construction, ControlMaster options, timeout handling, mod
resolution/bake logic, catalog loading, pydantic schema validation, the
Task MC integration itself) runs for real. This is why this script can
run anywhere -- CI, a laptop, this sandbox -- with zero cluster access,
while still exercising genuine code paths rather than a hand-rolled
reimplementation of them.

WHAT THIS DOES NOT COVER (see MC-REVIEW.md's "no hardware" section for
the fuller list): real SSH/network failure modes, real Docker daemon
behavior, real `scp` transfer behavior, real GPU/NVML interaction, real
Ray cluster registration timing. Passing this script is a strong signal
that the Task MC integration logic is correct; it is not a substitute
for the one real 1-node and one real 2-node live deploy the task's own
verification section calls for.

Usage:
    BASE_DIR is managed internally (a fresh temp dir per run) -- do not
    set it yourself.

    python3 smoke_test_mc.py [--orchestrator PATH] [--common-dir PATH]

    --orchestrator  Path to dgx-orchestrator.py to test.
                     Default: ./dgx-orchestrator.py (next to this script).
    --common-dir    Path to the directory containing your real
                     recipes.py, mods.py, ssh.py (and, if you have them,
                     your real config.py / constants.py -- if present,
                     REAL config.py/constants.py are used instead of this
                     script's fakes, so this becomes an even more
                     faithful run).
                     Default: ./common (next to this script), falling
                     back to this script's bundled fakes for any of the
                     five modules not found there.

Exit code 0 = all checks passed. Non-zero = at least one failed; see the
FAIL lines above the summary for which.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--orchestrator", default=str(SCRIPT_DIR / "dgx-orchestrator.py"),
                    help="Path to dgx-orchestrator.py to test.")
    p.add_argument("--common-dir", default=str(SCRIPT_DIR / "common"),
                    help="Directory with your real recipes.py/mods.py/ssh.py "
                         "(and optionally config.py/constants.py).")
    p.add_argument("--cluster-config", default=None,
                    help="Path to your real cluster_config.yaml. Required if your real "
                         "config.py (found via --common-dir) is being used, since it reads "
                         "this file at import/call time -- the fixture's fake config.py does "
                         "NOT need this. If omitted, the script tries a couple of sensible "
                         "default locations next to --common-dir before giving up and falling "
                         "back to the bundled fake config.py/constants.py for this run.")
    p.add_argument("--keep-fixture", action="store_true",
                    help="Don't delete the temp BASE_DIR fixture on exit (for debugging).")
    return p.parse_args()


def find_cluster_config(explicit: str | None, common_dir: Path) -> Path | None:
    if explicit:
        p = Path(explicit).resolve()
        if not p.exists():
            sys.exit(f"[!] --cluster-config not found: {p}")
        return p
    for candidate in (common_dir.parent / "cluster_config.yaml", common_dir / "cluster_config.yaml"):
        if candidate.exists():
            return candidate
    return None


# ---------------------------------------------------------------------
# Fixture construction: a throwaway repo layout with real recipe YAML,
# a real mod payload, and a legacy models.yaml, all under one temp
# BASE_DIR.
# ---------------------------------------------------------------------

_RECIPE_TEMPLATE = """\
recipe_version: "1"
hf_path: "org/{name}"
gpu_util: 0.9
mods: {mods}
topologies:
  1_node:
    max_model_len: 32768
    tp_size: 1
    pp_size: 1
    env_vars: []
    vllm_args: ""
  2_node:
    max_model_len: 32768
    tp_size: 1
    pp_size: 2
    env_vars: []
    vllm_args: ""
"""

_MODELS_YAML = """\
GLOBAL_HF_HUB_OFFLINE: 0
GLOBAL_TRANSFORMERS_OFFLINE: 0
default_image: "eugr/spark-vllm-b12x:latest"
models:
  legacy-model:
    hf_path: "org/legacy-model"
    gpu_util: 0.85
    topologies:
      1_node:
        max_model_len: 32768
        tp_size: 1
        pp_size: 1
        env_vars: []
        vllm_args: ""
"""


def build_fixture(base_dir: Path) -> None:
    (base_dir / "recipes" / "local").mkdir(parents=True, exist_ok=True)
    (base_dir / "recipes" / "eugr").mkdir(parents=True, exist_ok=True)
    (base_dir / "mods" / "fake-mod").mkdir(parents=True, exist_ok=True)

    for name, mods in [
        ("test-model-nomods", "[]"),
        ("test-model-mods", '["fake-mod"]'),
        ("test-model-badmod", '["missing-mod"]'),
    ]:
        (base_dir / "recipes" / "local" / f"{name}.yaml").write_text(
            _RECIPE_TEMPLATE.format(name=name, mods=mods)
        )

    (base_dir / "mods" / "fake-mod" / "run.sh").write_text(
        '#!/bin/bash\necho "fake mod applied" > "$WORKSPACE_DIR/.fake_mod_marker"\n'
    )
    (base_dir / "models.yaml").write_text(_MODELS_YAML)


# ---------------------------------------------------------------------
# Fake common.config / common.constants -- written into the fixture's
# common/ dir only if the real ones aren't found in --common-dir. Real
# recipes.py/mods.py/ssh.py are ALWAYS copied in from --common-dir (or
# this script exits with an error) -- those three are never faked.
# ---------------------------------------------------------------------

_FAKE_CONFIG_PY = '''\
import os
from pathlib import Path
from types import SimpleNamespace

BASE_DIR = Path(os.environ["BASE_DIR"])

_HOSTS = {
    "spark-3": {"ip": "100.64.0.3", "role": "head"},
    "spark-4": {"ip": "100.64.0.4", "role": "worker"},
}


def legacy_hosts_dict():
    return dict(_HOSTS)


def load_cluster_config():
    tuning = SimpleNamespace(
        shm_size_1node="32g", shm_size_2node="32g", gpu_clock_lock="1980",
        jit_cache_maxsize_bytes=4294967296, debug_launch_blocking=False,
        deploy_wait_timeout_sec=180, deploy_poll_interval_sec=2,
        crash_log_retention_days=7,
    )
    hosts = {
        "spark-3": SimpleNamespace(volume_mount="/mnt/hf-cache:/root/.cache/huggingface"),
        "spark-4": SimpleNamespace(volume_mount="/mnt/hf-cache:/root/.cache/huggingface"),
    }
    return SimpleNamespace(
        ssh_key_name="id_dgx_orchestrator", ssh_user="ian", hosts=hosts,
        ports={"master": 29500, "ray": 6379}, default_image="eugr/spark-vllm-b12x:latest",
        global_hf_hub_offline=0, global_transformers_offline=0, tuning=tuning,
    )
'''

_FAKE_CONSTANTS_PY = '''\
class ContainerRole:
    STANDALONE = "vllm-standalone"
    HEAD = "vllm-head"
    WORKER = "vllm-worker"
'''

_REQUIRED_REAL_MODULES = ["recipes.py", "mods.py", "ssh.py"]


def build_common_package(work_dir: Path, common_dir: Path, force_fake_config: bool = False) -> bool:
    """Returns True if the REAL config.py was used (as opposed to this
    script's fake) -- callers need this to know whether a real
    cluster_config.yaml must also be supplied.

    force_fake_config=True overrides even a real config.py/constants.py
    found in common_dir -- used when a real config.py exists but no
    cluster_config.yaml could be found for it, so the run degrades
    cleanly to full-fake rather than a mixed real-config/no-cluster-yaml
    state that would just crash on import."""
    pkg = work_dir / "common"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").touch()

    missing = [m for m in _REQUIRED_REAL_MODULES if not (common_dir / m).exists()]
    if missing:
        sys.exit(
            f"[!] --common-dir {common_dir} is missing required real module(s): {missing}. "
            f"recipes.py, mods.py, and ssh.py must be your real, unmodified project files -- "
            f"this smoke test does not fake them."
        )
    for m in _REQUIRED_REAL_MODULES:
        shutil.copy2(common_dir / m, pkg / m)

    used_real_config = False
    for fname, fake_src in [("config.py", _FAKE_CONFIG_PY), ("constants.py", _FAKE_CONSTANTS_PY)]:
        real = common_dir / fname
        if real.exists() and not force_fake_config:
            shutil.copy2(real, pkg / fname)
            print(f"[i] Using REAL {fname} from {common_dir} (not this script's fake).")
            if fname == "config.py":
                used_real_config = True
        else:
            if real.exists() and force_fake_config:
                print(f"[i] REAL {fname} found but overridden with this script's fake for this run "
                      f"(see cluster_config.yaml note above).")
            (pkg / fname).write_text(fake_src)
    return used_real_config


# ---------------------------------------------------------------------
# Transport double: intercepts subprocess.Popen (used by real
# common.ssh.run_ssh) and subprocess.run (used by real
# common.mods._scp_to_host). Everything ABOVE this layer -- ssh_cmd
# construction, ControlMaster flags, capture handling, timeout logic,
# mod resolve/bake control flow, the Task MC integration itself -- is
# real code under real test.
# ---------------------------------------------------------------------

class Transport:
    def __init__(self):
        self.calls = []          # [{"ip": ..., "cmd": [...]}]
        self.preseeded_tags = {}  # ip -> set(tag)
        self.baked_tags = {}      # ip -> set(tag)

    def reset(self):
        self.calls.clear()
        self.baked_tags.clear()

    def handle(self, ip: str, cmd: list) -> tuple[str, str, int]:
        self.calls.append({"ip": ip, "cmd": cmd})

        if cmd[:3] == ["docker", "image", "inspect"]:
            tag = cmd[3] if len(cmd) > 3 else None
            known = self.preseeded_tags.get(ip, set()) | self.baked_tags.get(ip, set())
            if tag in known:
                return "[{}]\n", "", 0
            return "", f"No such image: {tag}", 1

        if cmd[:2] == ["docker", "commit"]:
            tag = cmd[-1] if cmd else None
            if tag:
                self.baked_tags.setdefault(ip, set()).add(tag)
            return "sha256:fakelayer\n", "", 0

        if cmd[:3] == ["docker", "inspect", "--format"]:
            fmt = cmd[3] if len(cmd) > 3 else ""
            if "Entrypoint" in fmt:
                return '["/opt/nvidia/nvidia_entrypoint.sh"]\n', "", 0
            if "Cmd" in fmt:
                return "null\n", "", 0
            if "WorkingDir" in fmt:
                return "/workspace/vllm\n", "", 0
            return "\n", "", 0

        if cmd[:2] == ["docker", "ps"]:
            return "vllm-standalone\nvllm-head\nvllm-worker\n", "", 0

        # mkdir, docker create/start/exec/cp/rm, docker run, docker logs,
        # nvidia-smi, rm -rf, bash -- all succeed.
        return "", "", 0


TRANSPORT = Transport()


class FakeCompletedProcess:
    def __init__(self, args, returncode, stdout, stderr):
        self.args = args
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakePopen:
    """Drop-in stand-in for subprocess.Popen, as used inside real
    common/ssh.py's run_ssh(). Parses the real ssh_cmd argv real run_ssh()
    built (including the quoted remote-command string) to recover the
    target ip and the original remote command list, then dispatches to
    TRANSPORT -- so run_ssh()'s own argument-construction logic is
    exercised for real; only the actual `ssh` process launch is faked."""

    def __init__(self, args, **kwargs):
        self._args = args
        target = args[-2]
        remote_cmd_str = args[-1]
        self._ip = target.split("@", 1)[1] if "@" in target else target
        cmd = shlex.split(remote_cmd_str)
        self._stdout, self._stderr, self.returncode = TRANSPORT.handle(self._ip, cmd)
        self.pid = 999999

    def communicate(self, timeout=None):
        return self._stdout, self._stderr

    def wait(self, timeout=None):
        return self.returncode


def fake_scp_run(cmd, *args, **kwargs):
    """Stand-in for subprocess.run, patched only inside common.mods (used
    by _scp_to_host()). Intercepts scp specifically; anything else falls
    through to the real subprocess.run."""
    if cmd and cmd[0] == "scp":
        return FakeCompletedProcess(cmd, 0, "", "")
    return _real_subprocess_run(cmd, *args, **kwargs)


_real_subprocess_run = subprocess.run


# ---------------------------------------------------------------------
# Test runner plumbing
# ---------------------------------------------------------------------

RESULTS = {"pass": 0, "fail": 0}


def check(label: str, cond: bool, detail=""):
    if cond:
        RESULTS["pass"] += 1
        print(f"PASS  {label}")
    else:
        RESULTS["fail"] += 1
        print(f"FAIL  {label}\n      {detail}")


def docker_runs(calls):
    return [c for c in calls if c["cmd"][:2] == ["docker", "run"]]


def docker_commits(calls):
    return [c for c in calls if c["cmd"][:2] == ["docker", "commit"]]


def docker_image_inspects(calls):
    return [c for c in calls if c["cmd"][:3] == ["docker", "image", "inspect"]]


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main():
    args = parse_args()
    orchestrator_path = Path(args.orchestrator).resolve()
    common_dir = Path(args.common_dir).resolve()

    # Convenience: if a directory was passed (e.g. `--orchestrator .`),
    # look for dgx-orchestrator.py inside it rather than failing with an
    # opaque spec_from_file_location() -> None -> AttributeError three
    # calls downstream. Still a hard error if it's not there -- this is
    # a narrow convenience, not a general search.
    if orchestrator_path.is_dir():
        candidate = orchestrator_path / "dgx-orchestrator.py"
        if candidate.exists():
            print(f"[i] --orchestrator {orchestrator_path} is a directory; using {candidate}")
            orchestrator_path = candidate
        else:
            sys.exit(
                f"[!] --orchestrator {orchestrator_path} is a directory and does not contain "
                f"dgx-orchestrator.py. Pass the path to the actual .py file, "
                f"e.g. --orchestrator {orchestrator_path}/dgx-orchestrator.py"
            )

    if not orchestrator_path.exists():
        sys.exit(f"[!] --orchestrator not found: {orchestrator_path}")
    if orchestrator_path.suffix != ".py":
        sys.exit(f"[!] --orchestrator does not look like a .py file: {orchestrator_path}")

    work_dir = Path(tempfile.mkdtemp(prefix="dgx-mc-smoke-"))
    base_dir = work_dir / "base"
    home_dir = work_dir / "home"
    base_dir.mkdir()
    home_dir.mkdir()

    try:
        build_fixture(base_dir)

        # A real config.py (if present in --common-dir) reads
        # cluster_config.yaml at call/import time -- the fixture's fake
        # config.py doesn't need this file at all, so it's only fetched
        # when it's actually going to be used.
        real_config_present = (common_dir / "config.py").exists()
        force_fake_config = False
        if real_config_present:
            cc_path = find_cluster_config(args.cluster_config, common_dir)
            if cc_path is not None:
                shutil.copy2(cc_path, base_dir / "cluster_config.yaml")
                print(f"[i] Using real cluster_config.yaml: {cc_path}")
            else:
                print(
                    "[!] A real config.py was found in --common-dir, but no cluster_config.yaml "
                    "was given (--cluster-config) or found next to --common-dir. Falling back to "
                    "this script's fake config.py/constants.py for this run -- pass "
                    "--cluster-config /path/to/cluster_config.yaml for a fully-real run."
                )
                force_fake_config = True

        used_real_config = build_common_package(work_dir, common_dir, force_fake_config=force_fake_config)

        os.environ["BASE_DIR"] = str(base_dir)
        os.environ["HOME"] = str(home_dir)  # keep resolve_user_identity_key()'s ~/.ssh writes sandboxed
        sys.path.insert(0, str(work_dir))

        # Import the REAL common.ssh, then patch subprocess-level
        # execution only (see FakePopen/fake_scp_run docstrings above).
        import common.ssh as real_ssh
        real_ssh.subprocess.Popen = FakePopen

        import common.mods as real_mods
        real_mods.subprocess.run = fake_scp_run

        spec = importlib.util.spec_from_file_location("dgx_orchestrator", str(orchestrator_path))
        if spec is None or spec.loader is None:
            sys.exit(
                f"[!] Could not build an import spec for {orchestrator_path} -- "
                f"is this actually a Python file (not a directory or package)?"
            )
        dgx = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(dgx)

        print(f"[i] Loaded {orchestrator_path}")
        print(f"[i] ORCHESTRATOR_VERSION = {getattr(dgx, 'ORCHESTRATOR_VERSION', '?')}")

        # Host IPs come from whatever HOSTS ended up being (real
        # cluster_config.yaml if used_real_config, this script's fake
        # 100.64.0.x otherwise) -- assertions below must never hardcode
        # an expected IP, since it depends entirely on which config was
        # actually loaded.
        try:
            head_ip = dgx.HOSTS["spark-3"]["ip"]
            worker_ip = dgx.HOSTS["spark-4"]["ip"]
        except KeyError as exc:
            sys.exit(
                f"[!] dgx.HOSTS does not contain expected host key {exc} -- this smoke test's "
                f"fixture/assertions assume hosts named 'spark-3' and 'spark-4' exist "
                f"(spark-3 as head). If your real cluster_config.yaml uses different host "
                f"names, this script needs updating, not your recipe/deploy code."
            )
        print(f"[i] Using host IPs from {'REAL' if used_real_config else 'fake'} config: "
              f"spark-3={head_ip}, spark-4={worker_ip}")

        # Same reasoning as head_ip/worker_ip above: default_image must
        # never be hardcoded to this script's fake fixture value, since a
        # real cluster_config.yaml can (and, per Ian's real one, does)
        # carry a completely different default_image. Read it from
        # whichever config actually ended up loaded.
        default_image = dgx.load_cluster_config().default_image
        print(f"[i] Using default_image from {'REAL' if used_real_config else 'fake'} config: {default_image}")
        print()

        # -----------------------------------------------------------
        # T1/T2: --dry-run, mods: [] -- must be a strict no-op: zero SSH
        # calls, and the response dict's key set must be exactly the
        # pre-MC set (no 'mods' key at all).
        # -----------------------------------------------------------
        expected_keys = {"status", "message", "targets", "head", "docker_run_commands"}

        TRANSPORT.reset()
        r1 = dgx._execute_deployment_impl("test-model-nomods", 1, "spark-3", "smoke", dry_run=True)
        check("1-node dry-run (mods:[]): status == dry_run", r1.get("status") == "dry_run", r1)
        check("1-node dry-run (mods:[]): zero SSH calls", len(TRANSPORT.calls) == 0, TRANSPORT.calls)
        check("1-node dry-run (mods:[]): key set unchanged (no 'mods' key)", set(r1.keys()) == expected_keys, r1.keys())
        check(
            "1-node dry-run (mods:[]): image arg is unmodified base image",
            default_image in r1["docker_run_commands"]["spark-3"],
            r1["docker_run_commands"]["spark-3"],
        )

        TRANSPORT.reset()
        r2 = dgx._execute_deployment_impl("test-model-nomods", 2, "spark-3", "smoke", dry_run=True)
        check("2-node dry-run (mods:[]): status == dry_run", r2.get("status") == "dry_run", r2)
        check("2-node dry-run (mods:[]): zero SSH calls", len(TRANSPORT.calls) == 0, TRANSPORT.calls)
        check("2-node dry-run (mods:[]): key set unchanged (no 'mods' key)", set(r2.keys()) == expected_keys, r2.keys())
        check(
            "2-node dry-run (mods:[]): both hosts present",
            set(r2["docker_run_commands"].keys()) == {"spark-3", "spark-4"},
            r2["docker_run_commands"].keys(),
        )

        # -----------------------------------------------------------
        # T3: --dry-run WITH mods -- still zero SSH, reports resolved
        # tag + what-would-be-baked, image arg is the derived tag.
        # -----------------------------------------------------------
        TRANSPORT.reset()
        r3 = dgx._execute_deployment_impl("test-model-mods", 1, "spark-3", "smoke", dry_run=True)
        check("1-node dry-run (mods present): status == dry_run", r3.get("status") == "dry_run", r3)
        check("1-node dry-run (mods present): zero SSH calls", len(TRANSPORT.calls) == 0, TRANSPORT.calls)
        check("1-node dry-run (mods present): 'mods' key reports spark-3", "mods" in r3 and "spark-3" in r3.get("mods", {}), r3)
        resolved_3 = r3.get("mods", {}).get("spark-3", {}).get("resolved_tag")
        check(
            "1-node dry-run (mods present): resolved tag is derived",
            bool(resolved_3) and "-mods-" in resolved_3,
            resolved_3,
        )
        check(
            "1-node dry-run (mods present): docker_run_commands uses resolved tag",
            bool(resolved_3) and resolved_3 in r3["docker_run_commands"]["spark-3"],
            r3["docker_run_commands"]["spark-3"],
        )

        # -----------------------------------------------------------
        # T4: mod resolution failure aborts before any container starts,
        # in both dry-run and live, 1-node and 2-node.
        # -----------------------------------------------------------
        TRANSPORT.reset()
        r4a = dgx._execute_deployment_impl("test-model-badmod", 1, "spark-3", "smoke", dry_run=True)
        check("dry-run, bad mod: status == error", r4a.get("status") == "error", r4a)
        check("dry-run, bad mod: zero SSH calls", len(TRANSPORT.calls) == 0, TRANSPORT.calls)

        TRANSPORT.reset()
        r4b = dgx._execute_deployment_impl("test-model-badmod", 1, "spark-3", "smoke", dry_run=False)
        check("live 1-node, bad mod: status == error", r4b.get("status") == "error", r4b)
        check("live 1-node, bad mod: no docker run occurred", len(docker_runs(TRANSPORT.calls)) == 0, TRANSPORT.calls)

        TRANSPORT.reset()
        r4c = dgx._execute_deployment_impl("test-model-badmod", 2, "spark-3", "smoke", dry_run=False)
        check("live 2-node, bad mod: status == error", r4c.get("status") == "error", r4c)
        check("live 2-node, bad mod: no docker run occurred on either host", len(docker_runs(TRANSPORT.calls)) == 0, TRANSPORT.calls)

        # -----------------------------------------------------------
        # T5: live deploy, mods: [] -- exactly N docker runs, base image
        # unchanged, ZERO 'docker image inspect' calls at all (that call
        # only happens when mod_names is non-empty).
        # -----------------------------------------------------------
        TRANSPORT.reset()
        r5a = dgx._execute_deployment_impl("test-model-nomods", 1, "spark-3", "smoke", dry_run=False)
        check("live 1-node (mods:[]): status == success", r5a.get("status") == "success", r5a)
        check("live 1-node (mods:[]): exactly one docker run", len(docker_runs(TRANSPORT.calls)) == 1, TRANSPORT.calls)
        check("live 1-node (mods:[]): zero docker image inspect", len(docker_image_inspects(TRANSPORT.calls)) == 0, TRANSPORT.calls)
        check(
            "live 1-node (mods:[]): docker run uses base image",
            default_image in docker_runs(TRANSPORT.calls)[0]["cmd"],
            docker_runs(TRANSPORT.calls),
        )

        TRANSPORT.reset()
        r5b = dgx._execute_deployment_impl("test-model-nomods", 2, "spark-3", "smoke", dry_run=False)
        check("live 2-node (mods:[]): status == success", r5b.get("status") == "success", r5b)
        check("live 2-node (mods:[]): exactly two docker runs", len(docker_runs(TRANSPORT.calls)) == 2, TRANSPORT.calls)
        check("live 2-node (mods:[]): zero docker image inspect", len(docker_image_inspects(TRANSPORT.calls)) == 0, TRANSPORT.calls)

        # -----------------------------------------------------------
        # T6: live deploy, mods present, 1-node -- a real bake sequence
        # runs (one docker commit), and the final docker run uses the
        # derived tag, not the base image.
        # -----------------------------------------------------------
        TRANSPORT.reset()
        r6 = dgx._execute_deployment_impl("test-model-mods", 1, "spark-3", "smoke", dry_run=False)
        check("live 1-node (mods present): status == success", r6.get("status") == "success", r6)
        check("live 1-node (mods present): exactly one docker commit (bake happened)", len(docker_commits(TRANSPORT.calls)) == 1, TRANSPORT.calls)
        runs_6 = docker_runs(TRANSPORT.calls)
        check("live 1-node (mods present): exactly one docker run", len(runs_6) == 1, runs_6)
        check(
            "live 1-node (mods present): docker run uses derived tag, not base",
            bool(runs_6) and any("-mods-" in a for a in runs_6[0]["cmd"]) and default_image not in runs_6[0]["cmd"],
            runs_6,
        )

        # -----------------------------------------------------------
        # T7: idempotency -- tag already present on host -> exactly one
        # 'docker image inspect', zero 'docker commit' (no re-bake).
        # -----------------------------------------------------------
        from common.mods import resolve_mod_tag
        derived_tag = resolve_mod_tag(default_image, ["fake-mod"])
        TRANSPORT.reset()
        TRANSPORT.preseeded_tags = {head_ip: {derived_tag}}
        r7 = dgx._execute_deployment_impl("test-model-mods", 1, "spark-3", "smoke", dry_run=False)
        check("live 1-node, tag pre-baked: status == success", r7.get("status") == "success", r7)
        check("live 1-node, tag pre-baked: exactly one docker image inspect", len(docker_image_inspects(TRANSPORT.calls)) == 1, TRANSPORT.calls)
        check("live 1-node, tag pre-baked: zero docker commit (no re-bake)", len(docker_commits(TRANSPORT.calls)) == 0, TRANSPORT.calls)
        TRANSPORT.preseeded_tags = {}

        # -----------------------------------------------------------
        # T8: live deploy, mods present, 2-node -- bake happens
        # independently on BOTH hosts (constraint 2 in mods.py's
        # docstring), converging on the SAME derived tag.
        # -----------------------------------------------------------
        TRANSPORT.reset()
        r8 = dgx._execute_deployment_impl("test-model-mods", 2, "spark-3", "smoke", dry_run=False)
        check("live 2-node (mods present): status == success", r8.get("status") == "success", r8)
        commits_8 = docker_commits(TRANSPORT.calls)
        check("live 2-node (mods present): two docker commits (one per host)", len(commits_8) == 2, commits_8)
        check("live 2-node (mods present): commits happened on two different hosts", {c["ip"] for c in commits_8} == {head_ip, worker_ip}, commits_8)
        check("live 2-node (mods present): both hosts baked to the same tag", len({c["cmd"][-1] for c in commits_8}) == 1, commits_8)
        runs_8 = docker_runs(TRANSPORT.calls)
        check("live 2-node (mods present): two docker runs, both using the derived tag",
              len(runs_8) == 2 and all(any("-mods-" in a for a in c["cmd"]) for c in runs_8),
              runs_8)

        # -----------------------------------------------------------
        # T9: USE_LEGACY_CATALOG=1 -- a model with NO backing RecipeConfig
        # (comes from models.yaml, not recipes/) must fall back to
        # mod_names=[] cleanly rather than erroring.
        # -----------------------------------------------------------
        os.environ["USE_LEGACY_CATALOG"] = "1"
        try:
            TRANSPORT.reset()
            r9 = dgx._execute_deployment_impl("legacy-model", 1, "spark-3", "smoke", dry_run=True)
            check("legacy catalog model: dry-run status == dry_run (no crash from missing RecipeConfig)", r9.get("status") == "dry_run", r9)
            check("legacy catalog model: zero SSH calls", len(TRANSPORT.calls) == 0, TRANSPORT.calls)
            check("legacy catalog model: no 'mods' key (mod_names correctly empty)", "mods" not in r9, r9)
        finally:
            del os.environ["USE_LEGACY_CATALOG"]

        print()
        print(f"TOTAL: {RESULTS['pass']} passed, {RESULTS['fail']} failed")
        return 1 if RESULTS["fail"] else 0

    finally:
        if args.keep_fixture:
            print(f"[i] Fixture kept at {work_dir}")
        else:
            shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
