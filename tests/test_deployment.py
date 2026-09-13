#!/usr/bin/env python3
"""Hardware-free regression tests for the deployment-stage contract."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from common.deployment import (
    DeploymentProgress,
    DeploymentStage,
    DeploymentStageResult,
)


def _load_orchestrator():
    path = REPO_ROOT / "dgx-orchestrator.py"
    spec = importlib.util.spec_from_file_location("orchestrator_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


ORCH = _load_orchestrator()


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


@contextmanager
def _patched(**replacements):
    originals = {}
    try:
        for name, replacement in replacements.items():
            originals[name] = getattr(ORCH, name)
            setattr(ORCH, name, replacement)
        yield
    finally:
        for name, original in originals.items():
            setattr(ORCH, name, original)


@contextmanager
def _isolated_deploy(teardown_result, run_ssh, **extra):
    original_commit = ORCH.SESSION_TRACKER._commit_session
    original_active = ORCH.SESSION_TRACKER.active
    replacements = {
        "_execute_teardown_impl": lambda target_hosts=None: teardown_result,
        "run_ssh": run_ssh,
        "_record_hf_path": lambda *args, **kwargs: None,
        "_set_active_deployment": lambda *args, **kwargs: None,
        "_set_pending_launch": lambda *args, **kwargs: None,
        "get_hf_token": lambda: None,
        "record_load_time": lambda *args, **kwargs: None,
        **extra,
    }
    ORCH.SESSION_TRACKER._commit_session = lambda: None
    try:
        with _patched(**replacements):
            yield
    finally:
        ORCH.SESSION_TRACKER._commit_session = original_commit
        ORCH.SESSION_TRACKER.active = original_active


def _model_for(topology, *, ray=False):
    models = ORCH.load_model_catalog()["catalog"]["models"]
    for name, model in models.items():
        topo = model.get("topologies", {}).get(topology)
        if topo is None:
            continue
        args = topo.get("vllm_args", "")
        uses_ray = (
            "--distributed-executor-backend" in args
            and "ray" in args
        )
        if uses_ray == ray:
            return name
    raise AssertionError(
        f"catalog has no {topology} recipe with ray={ray}"
    )


def _assert_failed_at(result, expected_stage):
    assert result["status"] == "error", result
    assert result["failed_stage"] == expected_stage.value, result
    assert result["stages"][-1]["stage"] == expected_stage.value, result
    assert result["stages"][-1]["status"] == "error", result


def test_contract_rejects_success_after_failure():
    stages = [
        DeploymentStageResult.failure(
            DeploymentStage.CONTAINER_LAUNCH,
            "simulated failure",
        )
    ]
    progress = DeploymentProgress()
    progress.record(stages[0])
    try:
        progress.success_response(
            "this must be rejected",
            targets=["spark-4"],
            head="spark-4",
        )
    except ValueError as exc:
        assert "container_launch" in str(exc)
    else:
        raise AssertionError("success response accepted a failed stage")
    print("PASS: contract rejects success after a failed stage")


def test_pre_deploy_teardown_failure_stops_launch():
    model = _model_for("1_node")
    head = ORCH.DEFAULT_DEPLOY_HOST
    ssh_calls = []

    def unexpected_ssh(*args, **kwargs):
        ssh_calls.append((args, kwargs))
        return _completed()

    with _isolated_deploy(
        {head: "Error: simulated docker rm failure"},
        unexpected_ssh,
    ):
        result = ORCH._execute_deployment_impl(
            model, 1, head, "test-user"
        )

    _assert_failed_at(result, DeploymentStage.PRE_DEPLOY_TEARDOWN)
    assert ssh_calls == [], "launch continued after teardown failure"
    print("PASS: failed pre-deploy teardown cannot return success or launch")


def test_host_preparation_failure_stops_launch():
    model = _model_for("1_node")
    head = ORCH.DEFAULT_DEPLOY_HOST
    docker_run_seen = False

    def fake_ssh(ip, user, command, timeout=30):
        nonlocal docker_run_seen
        if command[:3] == ["sudo", "nvidia-smi", "-lgc"]:
            return _completed(1, stderr="clock lock refused")
        if command[:2] == ["docker", "run"]:
            docker_run_seen = True
        return _completed()

    with _isolated_deploy({head: "Purged"}, fake_ssh):
        result = ORCH._execute_deployment_impl(
            model, 1, head, "test-user"
        )

    _assert_failed_at(result, DeploymentStage.HOST_PREPARATION)
    assert not docker_run_seen, "docker run executed after preparation failed"
    print("PASS: required host preparation failure stops launch")


def test_ray_engine_exec_failure_is_consumed():
    model = _model_for("2_node", ray=True)
    head = ORCH.PRIMARY_HOST
    targets = ORCH.deployment_target_hosts(2, head)

    def fake_ssh(ip, user, command, timeout=30):
        if command[:4] == [
            "docker", "exec", ORCH.ContainerRole.HEAD, "ray"
        ]:
            return _completed(stdout="2 active nodes")
        if command[:3] == ["docker", "exec", "-d"]:
            return _completed(17, stderr="simulated exec failure")
        return _completed()

    with _isolated_deploy(
        {host: "Purged" for host in targets},
        fake_ssh,
    ):
        result = ORCH._execute_deployment_impl(
            model, 2, head, "test-user"
        )

    _assert_failed_at(result, DeploymentStage.ENGINE_LAUNCH)
    assert result["stages"][-1]["details"]["returncode"] == 17
    print("PASS: failed detached Ray engine launch cannot return success")


def test_wait_timeout_is_a_deployment_failure():
    model = _model_for("1_node")
    head = ORCH.DEFAULT_DEPLOY_HOST

    def fake_ssh(ip, user, command, timeout=30):
        if command[:2] == ["docker", "ps"]:
            return _completed(stdout=f"{ORCH.ContainerRole.STANDALONE}\n")
        return _completed()

    with _isolated_deploy(
        {head: "Purged"},
        fake_ssh,
        wait_for_cluster_ready=lambda **kwargs: False,
    ):
        result = ORCH._execute_deployment_impl(
            model, 1, head, "test-user", wait=True
        )

    _assert_failed_at(result, DeploymentStage.READINESS)
    assert result["stages"][-1]["details"]["timeout_sec"] > 0
    print("PASS: requested readiness timeout cannot return success")


if __name__ == "__main__":
    test_contract_rejects_success_after_failure()
    test_pre_deploy_teardown_failure_stops_launch()
    test_host_preparation_failure_stops_launch()
    test_ray_engine_exec_failure_is_consumed()
    test_wait_timeout_is_a_deployment_failure()
    print("\nAll deployment contract tests passed.")
