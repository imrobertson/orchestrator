#!/usr/bin/env python3
"""
Plain-assert tests for common/config.py. Run with:
    python3 tests/test_config.py
"""

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from common.config import load_cluster_config, active_hosts, legacy_hosts_dict


def test_load_cluster_config_default():
    cfg = load_cluster_config()
    assert len(cfg.hosts) == 2, f"expected 2 hosts, got {len(cfg.hosts)}"
    print("PASS: load_cluster_config() returns 2 hosts")


def test_legacy_hosts_dict_shape():
    result = legacy_hosts_dict()
    expected = {
        "spark-4": {"ip": "10.0.14.43", "alias": "spark-9dbe", "role": "head"},
        "spark-3": {"ip": "10.0.14.41", "alias": "spark-6e63", "role": "worker"},
    }
    assert result == expected, f"legacy_hosts_dict() mismatch: {result}"
    print("PASS: legacy_hosts_dict() matches expected legacy shape")


def test_inactive_host_excluded():
    yaml_content = """
ssh_user: tetrel
ssh_key_name: id_dgx_orchestrator
default_image: nvcr.io/nvidia/vllm:26.07-py3
gpu_util_ceiling: 0.75

ports:
  vllm_api: 8000
  orchestrator_api: 5001
  ray: 6379
  master: 29500

container_names:
  standalone: vllm-standalone
  head: vllm-head
  worker: vllm-worker

hosts:
  spark-4:
    alias: spark-9dbe
    role: head
    management_ip: 10.0.14.43
    backplane_ip: 192.168.99.2
    volume_mount: /home/tetrel/.cache/huggingface:/root/.cache/huggingface
    active: true
  spark-3:
    alias: spark-6e63
    role: worker
    management_ip: 10.0.14.41
    backplane_ip: 192.168.99.1
    volume_mount: /home/tetrel/.cache/huggingface:/root/.cache/huggingface
    active: false

network:
  topology: switched
  interface: enp1s0f0np0
  nccl_ib_hca: rocep1s0f0
"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False
    ) as f:
        f.write(yaml_content)
        temp_path = Path(f.name)

    try:
        hosts = active_hosts(temp_path)
        assert "spark-3" not in hosts, "inactive host should be excluded from active_hosts()"
        assert "spark-4" in hosts, "active host should be present in active_hosts()"
        print("PASS: inactive host excluded from active_hosts()")

        legacy = legacy_hosts_dict(temp_path)
        assert "spark-3" not in legacy, "inactive host should be excluded from legacy_hosts_dict()"
        assert legacy == {
            "spark-4": {"ip": "10.0.14.43", "alias": "spark-9dbe", "role": "head"}
        }, f"legacy_hosts_dict() mismatch: {legacy}"
        print("PASS: inactive host excluded from legacy_hosts_dict()")
    finally:
        temp_path.unlink()


def test_malformed_yaml_raises_clear_error():
    malformed_content = """
ssh_user: tetrel
hosts:
  spark-4:
    alias: spark-9dbe
    role: head
  this is not: [valid yaml structure
"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False
    ) as f:
        f.write(malformed_content)
        temp_path = Path(f.name)

    try:
        error_raised = False
        error_message = ""
        try:
            load_cluster_config(temp_path)
        except (ValueError, Exception) as exc:
            error_raised = True
            error_message = str(exc)

        assert error_raised, "malformed YAML should raise an error, not return defaults"
        assert str(temp_path) in error_message, (
            f"error message should name the file path, got: {error_message}"
        )
        print("PASS: malformed YAML raises a clear error naming the file path")
    finally:
        temp_path.unlink()


def test_missing_file_raises_clear_error():
    missing_path = Path(tempfile.gettempdir()) / "does_not_exist_cluster_config.yaml"
    if missing_path.exists():
        missing_path.unlink()

    error_raised = False
    error_message = ""
    try:
        load_cluster_config(missing_path)
    except FileNotFoundError as exc:
        error_raised = True
        error_message = str(exc)

    assert error_raised, "missing file should raise FileNotFoundError"
    assert str(missing_path) in error_message, (
        f"error message should name the file path, got: {error_message}"
    )
    print("PASS: missing file raises a clear error naming the file path")


def test_validation_error_raises_clear_error():
    invalid_content = """
ssh_user: tetrel
ssh_key_name: id_dgx_orchestrator
default_image: nvcr.io/nvidia/vllm:26.07-py3
gpu_util_ceiling: 0.75
ports:
  vllm_api: 8000
container_names:
  standalone: vllm-standalone
hosts:
  spark-4:
    alias: spark-9dbe
    role: head
    management_ip: 10.0.14.43
    # missing required fields: backplane_ip, volume_mount
network:
  topology: switched
  interface: enp1s0f0np0
  nccl_ib_hca: rocep1s0f0
"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False
    ) as f:
        f.write(invalid_content)
        temp_path = Path(f.name)

    try:
        error_raised = False
        error_message = ""
        try:
            load_cluster_config(temp_path)
        except ValueError as exc:
            error_raised = True
            error_message = str(exc)

        assert error_raised, "missing required fields should raise a validation error"
        assert str(temp_path) in error_message, (
            f"error message should name the file path, got: {error_message}"
        )
        print("PASS: validation error raises a clear error naming the file path")
    finally:
        temp_path.unlink()


if __name__ == "__main__":
    test_load_cluster_config_default()
    test_legacy_hosts_dict_shape()
    test_inactive_host_excluded()
    test_malformed_yaml_raises_clear_error()
    test_missing_file_raises_clear_error()
    test_validation_error_raises_clear_error()
    print("\nAll tests passed.")
