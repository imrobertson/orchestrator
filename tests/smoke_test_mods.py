#!/usr/bin/env python3
"""
scripts/smoke_test_mods.py -- automated live-hardware smoke test for
common/mods.py (Task MB).

Runs the same sequence TESTING-MB.md walks through by hand, as one script:
creates throwaway test mods, bakes them on a real host, independently
verifies the result via plain `docker inspect` (never through the module
under test), confirms idempotent skip / payload-edit rebake / failing-mod
abort / per-host independence, then cleans up.

Run from the repo root on `maestro`:

    python3 scripts/smoke_test_mods.py
    python3 scripts/smoke_test_mods.py --hosts spark-4          # single host
    python3 scripts/smoke_test_mods.py --keep                   # leave artifacts for inspection
    python3 scripts/smoke_test_mods.py --serve-check --hf-path facebook/opt-125m

Exits 0 if every check passed, 1 otherwise. Does not touch
dgx-orchestrator.py or the real deploy path -- this only exercises
common/mods.py's public functions directly (Task MC hasn't happened yet).

Nothing this script creates is meant to be committed: mods/_test_* are
throwaway fixtures for validating MB's mechanics, not the real mods/_noop/
Task MD is responsible for.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

from common.ssh import BASE_DIR, resolve_user_identity_key
from common.recipes import MODS_DIR
from common.mods import ensure_mods_baked, resolve_mod_tag, ModBakeError, ModResolutionError

DEFAULT_BASE_IMAGE = "eugr/spark-vllm-b12x:latest"

TEST_MODS = {
    "_test_marker_a": (
        "#!/bin/bash\n"
        "set -e\n"
        'echo "marker_a applied at $(date -u +%FT%TZ), WORKSPACE_DIR=$WORKSPACE_DIR" '
        '>> "$WORKSPACE_DIR/mod_order.log"\n'
    ),
    "_test_marker_b": (
        "#!/bin/bash\n"
        "set -e\n"
        'echo "marker_b applied at $(date -u +%FT%TZ), WORKSPACE_DIR=$WORKSPACE_DIR" '
        '>> "$WORKSPACE_DIR/mod_order.log"\n'
    ),
    "_test_failing": (
        "#!/bin/bash\n"
        'echo "this mod deliberately fails" >&2\n'
        "exit 1\n"
    ),
}

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, passed: bool, detail: str = "") -> bool:
    RESULTS.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    print(f"[{status}] {name}" + (f" -- {detail}" if detail else ""))
    return passed


def load_hosts(selected: list[str] | None) -> dict[str, dict]:
    cfg_path = BASE_DIR / "cluster_config.yaml"
    raw = yaml.safe_load(cfg_path.read_text())
    ssh_user = raw["ssh_user"]
    all_hosts = raw["hosts"]
    names = selected or list(all_hosts.keys())
    missing = [n for n in names if n not in all_hosts]
    if missing:
        print(f"[!] Unknown host(s) in --hosts: {missing}. Known: {list(all_hosts.keys())}")
        sys.exit(2)
    return {n: {"ip": all_hosts[n]["management_ip"], "user": ssh_user} for n in names}


def ssh_run(ip: str, user: str, *cmd: str, timeout: int = 30) -> subprocess.CompletedProcess:
    key_path = resolve_user_identity_key()
    full = [
        "ssh", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=10", "-i", key_path, f"{user}@{ip}",
    ] + list(cmd)
    try:
        return subprocess.run(full, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(args=full, returncode=124, stdout="", stderr=str(exc))


def setup_test_mods() -> list[Path]:
    created = []
    for name, script in TEST_MODS.items():
        d = MODS_DIR / name
        if d.exists():
            print(f"[!] {d} already exists -- refusing to overwrite. Remove it and re-run.")
            sys.exit(2)
        d.mkdir(parents=True)
        run_sh = d / "run.sh"
        run_sh.write_text(script)
        run_sh.chmod(0o755)
        created.append(d)
    return created


def teardown_test_mods(dirs: list[Path]) -> None:
    for d in dirs:
        shutil.rmtree(d, ignore_errors=True)


def cleanup_host_images(ip: str, user: str, tags: set[str]) -> None:
    for tag in tags:
        ssh_run(ip, user, "docker", "rmi", "-f", tag)


# --- individual checks -------------------------------------------------

def check_local_resolution(base_image: str) -> bool:
    ok = True
    try:
        resolve_mod_tag(base_image, ["does_not_exist_xyz"])
        ok = record("resolution: missing mod raises", False, "did not raise")
    except ModResolutionError as e:
        ok &= record("resolution: missing mod raises", True, str(e))

    ok &= record(
        "resolution: empty mod set is a no-op tag",
        resolve_mod_tag(base_image, []) == base_image,
    )
    return ok


def check_first_bake(host: str, ip: str, user: str, base_image: str) -> tuple[bool, str]:
    try:
        tag = ensure_mods_baked(host, ip, base_image, ["_test_marker_a", "_test_marker_b"], user=user)
    except ModBakeError as e:
        return record(f"[{host}] first bake", False, str(e)), ""

    passed = record(f"[{host}] first bake completed", True, tag)

    r = ssh_run(ip, user, "docker", "image", "inspect", tag)
    passed &= record(f"[{host}] tag present after bake", r.returncode == 0, r.stderr.strip())

    r = ssh_run(ip, user, "docker", "run", "--rm", tag, "cat", "/workspace/vllm/mod_order.log")
    lines = r.stdout.splitlines()
    order_ok = r.returncode == 0 and len(lines) >= 2 and "marker_a" in lines[0] and "marker_b" in lines[1]
    passed &= record(f"[{host}] mods applied in declared order", order_ok, r.stdout.strip() or r.stderr.strip())

    base_ep = ssh_run(ip, user, "docker", "inspect", "--format", "{{json .Config.Entrypoint}}", base_image).stdout.strip()
    derived_ep = ssh_run(ip, user, "docker", "inspect", "--format", "{{json .Config.Entrypoint}}", tag).stdout.strip()
    passed &= record(f"[{host}] Entrypoint restored to match base", base_ep == derived_ep, f"{base_ep!r} vs {derived_ep!r}")

    base_cmd = ssh_run(ip, user, "docker", "inspect", "--format", "{{json .Config.Cmd}}", base_image).stdout.strip()
    derived_cmd = ssh_run(ip, user, "docker", "inspect", "--format", "{{json .Config.Cmd}}", tag).stdout.strip()
    passed &= record(f"[{host}] Cmd restored to match base", base_cmd == derived_cmd, f"{base_cmd!r} vs {derived_cmd!r}")

    base_wd = ssh_run(ip, user, "docker", "inspect", "--format", "{{.Config.WorkingDir}}", base_image).stdout.strip()
    derived_wd = ssh_run(ip, user, "docker", "inspect", "--format", "{{.Config.WorkingDir}}", tag).stdout.strip()
    passed &= record(f"[{host}] WorkingDir preserved", base_wd == derived_wd, derived_wd)

    r = ssh_run(ip, user, "docker", "ps", "-a", "--filter", "name=dgx-mods-bake", "--format", "{{.Names}}")
    passed &= record(f"[{host}] no dangling bake containers", r.stdout.strip() == "", r.stdout.strip())

    r = ssh_run(ip, user, "bash", "-c", "ls /tmp | grep dgx-mods-bake || true")
    passed &= record(f"[{host}] no leftover staging dirs", r.stdout.strip() == "", r.stdout.strip())

    return passed, tag


def check_idempotent(host: str, ip: str, user: str, base_image: str, expected_tag: str) -> bool:
    t0 = time.time()
    tag_again = ensure_mods_baked(host, ip, base_image, ["_test_marker_a", "_test_marker_b"], user=user)
    elapsed = time.time() - t0
    passed = record(f"[{host}] idempotent second call returns same tag", tag_again == expected_tag)
    passed &= record(f"[{host}] idempotent second call is fast ({elapsed:.2f}s)", elapsed < 10)
    return passed


def check_payload_edit(host: str, ip: str, user: str, base_image: str, original_tag: str) -> tuple[bool, str]:
    mod_dir = MODS_DIR / "_test_marker_a"
    run_sh = mod_dir / "run.sh"
    run_sh.write_text(run_sh.read_text() + 'echo "edited" >> "$WORKSPACE_DIR/mod_order.log"\n')

    new_tag = ensure_mods_baked(host, ip, base_image, ["_test_marker_a", "_test_marker_b"], user=user)
    passed = record(f"[{host}] payload edit produced a new tag", new_tag != original_tag, new_tag)

    r = ssh_run(ip, user, "docker", "image", "inspect", new_tag)
    passed &= record(f"[{host}] rebaked tag exists", r.returncode == 0, r.stderr.strip())
    return passed, new_tag


def check_failing_mod(host: str, ip: str, user: str, base_image: str) -> bool:
    would_be_tag = resolve_mod_tag(base_image, ["_test_marker_a", "_test_failing"])
    try:
        ensure_mods_baked(host, ip, base_image, ["_test_marker_a", "_test_failing"], user=user)
        passed = record(f"[{host}] failing mod raises ModBakeError", False, "did not raise")
    except ModBakeError as e:
        passed = record(f"[{host}] failing mod raises ModBakeError", True, str(e))

    r = ssh_run(ip, user, "docker", "image", "inspect", would_be_tag)
    passed &= record(f"[{host}] no partial tag left behind", r.returncode != 0)

    r = ssh_run(ip, user, "docker", "ps", "-a", "--filter", "name=dgx-mods-bake", "--format", "{{.Names}}")
    passed &= record(f"[{host}] no dangling container after failure", r.stdout.strip() == "")

    r = ssh_run(ip, user, "bash", "-c", "ls /tmp | grep dgx-mods-bake || true")
    passed &= record(f"[{host}] no leftover staging dir after failure", r.stdout.strip() == "")
    return passed


def check_cross_host_independence(host: str, ip: str, user: str, base_image: str, tag: str) -> bool:
    r_before = ssh_run(ip, user, "docker", "image", "inspect", tag)
    passed = record(f"[{host}] tag does not pre-exist here", r_before.returncode != 0)

    resolved = ensure_mods_baked(host, ip, base_image, ["_test_marker_a", "_test_marker_b"], user=user)
    passed &= record(f"[{host}] independently baked, tag matches", resolved == tag, resolved)

    r_after = ssh_run(ip, user, "docker", "image", "inspect", tag)
    passed &= record(f"[{host}] tag present after independent bake", r_after.returncode == 0)
    return passed


def check_serve(host: str, ip: str, user: str, tag: str, hf_path: str) -> bool:
    container = "mods-smoke-serve"
    ssh_run(ip, user, "docker", "rm", "-f", container)  # in case a previous run left one
    r = ssh_run(
        ip, user, "docker", "run", "-d", "--name", container, "--net=host", "--gpus", "all",
        "-v", "/home/tetrel/.cache/huggingface:/root/.cache/huggingface",
        tag,
        "python3", "-m", "vllm.entrypoints.openai.api_server",
        "--model", hf_path, "--gpu-memory-utilization", "0.5", "--max-model-len", "4096",
        timeout=60,
    )
    passed = record(f"[{host}] serve container started", r.returncode == 0, r.stderr.strip())
    if passed:
        print(f"    -> started {container} on {host}; check `docker logs -f {container}` "
              f"and curl :8000/health, then `docker rm -f {container}` when done.")
    return passed


# --- main ----------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hosts", type=str, default=None, help="Comma-separated host names from cluster_config.yaml (default: all)")
    parser.add_argument("--base-image", type=str, default=DEFAULT_BASE_IMAGE)
    parser.add_argument("--keep", action="store_true", help="Skip cleanup: leave baked tags on hosts and local mods/_test_* dirs in place")
    parser.add_argument("--serve-check", action="store_true", help="Also actually run the baked image and start vLLM (needs --hf-path)")
    parser.add_argument("--hf-path", type=str, default=None)
    args = parser.parse_args()

    if args.serve_check and not args.hf_path:
        parser.error("--serve-check requires --hf-path")

    hosts = load_hosts(args.hosts.split(",") if args.hosts else None)
    host_names = list(hosts.keys())
    if not host_names:
        print("[!] No hosts resolved from cluster_config.yaml")
        return 2

    primary, primary_meta = host_names[0], hosts[host_names[0]]
    secondary_names = host_names[1:]

    print(f"Hosts under test: {host_names}")
    print(f"Base image: {args.base_image}")
    print()

    created_mods = setup_test_mods()
    tags_to_clean: dict[str, set[str]] = {h: set() for h in host_names}
    overall = True

    try:
        overall &= check_local_resolution(args.base_image)

        passed, tag = check_first_bake(primary, primary_meta["ip"], primary_meta["user"], args.base_image)
        overall &= passed
        if not tag:
            print("[!] First bake failed -- aborting remaining checks.")
            return 1
        tags_to_clean[primary].add(tag)

        overall &= check_idempotent(primary, primary_meta["ip"], primary_meta["user"], args.base_image, tag)

        passed, new_tag = check_payload_edit(primary, primary_meta["ip"], primary_meta["user"], args.base_image, tag)
        overall &= passed
        if new_tag:
            tags_to_clean[primary].add(new_tag)

        # _test_marker_a was mutated in place by check_payload_edit above, so
        # every check from here on must compare against the CURRENT expected
        # tag (new_tag), not the pre-edit one (tag) -- the fixture on disk
        # has moved on and later bakes will reflect that.
        current_tag = new_tag or tag

        overall &= check_failing_mod(primary, primary_meta["ip"], primary_meta["user"], args.base_image)

        for h in secondary_names:
            meta = hosts[h]
            overall &= check_cross_host_independence(h, meta["ip"], meta["user"], args.base_image, current_tag)
            tags_to_clean[h].add(current_tag)

        if args.serve_check:
            overall &= check_serve(primary, primary_meta["ip"], primary_meta["user"], current_tag, args.hf_path)

    finally:
        if not args.keep:
            for h in host_names:
                cleanup_host_images(hosts[h]["ip"], hosts[h]["user"], tags_to_clean[h])
            teardown_test_mods(created_mods)
            print("\nCleaned up baked tags and local test mod fixtures.")
        else:
            print("\n--keep set: left baked tags on hosts and mods/_test_* in place.")

    print()
    n_pass = sum(1 for _, ok, _ in RESULTS if ok)
    n_total = len(RESULTS)
    print(f"{n_pass}/{n_total} checks passed.")
    if not overall:
        print("SMOKE TEST FAILED -- see FAIL lines above.")
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
