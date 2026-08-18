#!/usr/bin/env python3
"""
TETREL SECURITY - ORCHESTRATOR SMOKE TEST
--------------------------------------------------------------------------------
Minimal go/no-go gate for the DGX Spark orchestrator's control plane
(dgx-orchestrator.py running in "daemon" mode / the orchestrator-api
container). Run this after every migration phase - config consolidation,
recipe migration, N-node generalization, whatever - before trusting the
change for a real deploy.

This intentionally does NOT deploy or touch any model. It only checks that
the control plane itself is intact: both hosts are reachable over SSH from
the orchestrator, and the model catalog loads and isn't empty. If either of
those regresses, nothing downstream (deploy, teardown, dashboard) can be
trusted either, so this is the cheapest possible early warning.

Usage:
    python3 smoke_test.py
    python3 smoke_test.py --url http://10.0.14.50:5001

Exit code 0  = all checks passed, safe to proceed.
Exit code 1  = something regressed - do NOT proceed to the next migration
               phase, and do not trust the control plane for a real deploy
               until this passes again.
"""

import argparse
import sys

try:
    import requests
except ImportError:
    print("[-] The 'requests' package is required (already in requirements.txt).")
    print("    pip install requests --break-system-packages")
    sys.exit(2)

DEFAULT_API_URL = "http://localhost:5001"
REQUEST_TIMEOUT_SEC = 10


def check_status(base_url: str) -> tuple[bool, str]:
    """Hits /api/status and confirms every host the orchestrator knows about
    is actually reachable over SSH (docker_status == ONLINE). A host showing
    UNREACHABLE here means the control plane can't even query it, let alone
    deploy to it - this is the same signal the dashboard's host cards use."""
    try:
        resp = requests.get(f"{base_url}/api/status", timeout=REQUEST_TIMEOUT_SEC)
        resp.raise_for_status()
    except Exception as e:
        return False, f"/api/status request failed: {e}"

    try:
        data = resp.json()
    except Exception as e:
        return False, f"/api/status did not return valid JSON: {e}"

    hosts = data.get("hosts", {})
    if not hosts:
        return False, "/api/status returned no hosts at all."

    unreachable = [
        host for host, info in hosts.items()
        if info.get("docker_status") != "ONLINE"
    ]
    if unreachable:
        details = ", ".join(
            f"{h} ({hosts[h].get('docker_status', 'UNKNOWN')})" for h in unreachable
        )
        return False, f"Host(s) not reachable: {details}"

    return True, f"All {len(hosts)} host(s) reachable: {', '.join(sorted(hosts.keys()))}"


def check_catalog(base_url: str) -> tuple[bool, str]:
    """Hits /api/catalog and confirms the model catalog loaded and is
    non-empty. A refactor that breaks the YAML loader (or, post-Phase-2, the
    recipes/ globber) shows up here as an empty or malformed catalog rather
    than a confusing downstream 404 on /api/deploy."""
    try:
        resp = requests.get(f"{base_url}/api/catalog", timeout=REQUEST_TIMEOUT_SEC)
        resp.raise_for_status()
    except Exception as e:
        return False, f"/api/catalog request failed: {e}"

    try:
        data = resp.json()
    except Exception as e:
        return False, f"/api/catalog did not return valid JSON: {e}"

    if "error" in data:
        return False, f"Catalog loader reported an error: {data['error']}"

    models = data.get("catalog", {}).get("models", {})
    if not isinstance(models, dict) or not models:
        return False, "Model catalog is empty or malformed."

    return True, f"Catalog loaded with {len(models)} model(s): {', '.join(sorted(models.keys()))}"


def main():
    parser = argparse.ArgumentParser(description="Orchestrator control-plane smoke test / go-no-go gate")
    parser.add_argument(
        "--url", default=DEFAULT_API_URL,
        help=f"Base URL of the orchestrator API (default: {DEFAULT_API_URL})",
    )
    args = parser.parse_args()
    base_url = args.url.rstrip("/")

    checks = [
        ("Cluster status / host reachability", check_status),
        ("Model catalog", check_catalog),
    ]

    print(f"=== ORCHESTRATOR SMOKE TEST ({base_url}) ===\n")

    all_passed = True
    for name, fn in checks:
        passed, detail = fn(base_url)
        marker = "[\u2713] PASS" if passed else "[-] FAIL"
        print(f"{marker} \u2014 {name}: {detail}")
        if not passed:
            all_passed = False

    print()
    if all_passed:
        print("[\u2713] All smoke tests passed. Safe to proceed.")
        sys.exit(0)
    else:
        print("[-] One or more smoke tests failed. Do NOT proceed to the next")
        print("    migration phase, and don't trust the control plane for a")
        print("    real deploy until this passes again.")
        sys.exit(1)


if __name__ == "__main__":
    main()
