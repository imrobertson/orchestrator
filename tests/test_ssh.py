#!/usr/bin/env python3
"""
Plain-assert tests for common/ssh.py. Run with:
    python3 tests/test_ssh.py

subprocess.run is monkeypatched in every test so no real SSH connection
is ever attempted; we only inspect the argv/kwargs run_ssh constructs.
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import common.ssh as ssh_mod
from common.ssh import run_ssh


class _Recorder:
    """Stand-in for subprocess.run that records the call and returns a
    fake, successful CompletedProcess without executing anything."""

    def __init__(self):
        self.args = None
        self.kwargs = None

    def __call__(self, args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")


def _patch_run(recorder):
    ssh_mod.subprocess.run = recorder


def _restore_run(original):
    ssh_mod.subprocess.run = original


def test_default_connect_timeout_and_no_tty():
    original = ssh_mod.subprocess.run
    recorder = _Recorder()
    _patch_run(recorder)
    try:
        run_ssh("10.0.14.43", "tetrel", ["echo", "hi"])
        assert "-o" in recorder.args and "ConnectTimeout=5" in recorder.args, (
            f"expected ConnectTimeout=5 in {recorder.args}"
        )
        assert "-t" not in recorder.args, f"did not expect -t in default call: {recorder.args}"
        print("PASS: default call produces ConnectTimeout=5 and no -t")
    finally:
        _restore_run(original)


def test_tty_true_places_dash_t_immediately_after_ssh():
    original = ssh_mod.subprocess.run
    recorder = _Recorder()
    _patch_run(recorder)
    try:
        run_ssh("10.0.14.43", "tetrel", ["docker", "pull", "img"], tty=True)
        assert recorder.args[0] == "ssh", f"expected ssh cmd first, got {recorder.args}"
        assert recorder.args[1] == "-t", f"expected -t immediately after ssh, got {recorder.args}"
        print("PASS: tty=True produces -t immediately after ssh")
    finally:
        _restore_run(original)


def test_connect_timeout_override():
    original = ssh_mod.subprocess.run
    recorder = _Recorder()
    _patch_run(recorder)
    try:
        run_ssh("10.0.14.43", "tetrel", ["echo", "hi"], connect_timeout=10)
        assert "ConnectTimeout=10" in recorder.args, (
            f"expected ConnectTimeout=10 in {recorder.args}"
        )
        print("PASS: connect_timeout=10 produces ConnectTimeout=10")
    finally:
        _restore_run(original)


def test_capture_true_passes_capture_output_true():
    original = ssh_mod.subprocess.run
    recorder = _Recorder()
    _patch_run(recorder)
    try:
        run_ssh("10.0.14.43", "tetrel", ["echo", "hi"], capture=True)
        assert recorder.kwargs.get("capture_output") is True, (
            f"expected capture_output=True, got kwargs={recorder.kwargs}"
        )
        assert recorder.kwargs.get("text") is True, (
            f"expected text=True, got kwargs={recorder.kwargs}"
        )
        print("PASS: capture=True passes capture_output=True")
    finally:
        _restore_run(original)


def test_capture_false_passes_no_capture_kwarg():
    original = ssh_mod.subprocess.run
    recorder = _Recorder()
    _patch_run(recorder)
    try:
        run_ssh("10.0.14.43", "tetrel", ["docker", "pull", "img"], capture=False)
        assert "capture_output" not in recorder.kwargs, (
            f"expected NO capture_output kwarg, got kwargs={recorder.kwargs}"
        )
        print("PASS: capture=False passes no capture_output kwarg whatsoever")
    finally:
        _restore_run(original)


def test_user_none_resolves_from_cluster_config_and_explicit_overrides():
    original = ssh_mod.subprocess.run
    recorder = _Recorder()
    _patch_run(recorder)
    try:
        run_ssh("10.0.14.43", None, ["echo", "hi"])
        assert "tetrel@10.0.14.43" in recorder.args, (
            f"expected user resolved to tetrel from cluster_config.yaml, got {recorder.args}"
        )
        print("PASS: user=None resolves to tetrel from cluster_config.yaml")

        run_ssh("10.0.14.43", "someone_else", ["echo", "hi"])
        assert "someone_else@10.0.14.43" in recorder.args, (
            f"expected explicit user to override, got {recorder.args}"
        )
        assert "tetrel@10.0.14.43" not in recorder.args
        print("PASS: explicit user overrides cluster_config.yaml default")
    finally:
        _restore_run(original)


def test_control_master_options_present_in_every_variant():
    original = ssh_mod.subprocess.run
    control_opts = ["ControlMaster=auto", "ControlPersist=60s", "ControlPath=/tmp/cm-%C"]

    variants = [
        dict(capture=True, tty=False),
        dict(capture=False, tty=True),
        dict(capture=True, tty=True, connect_timeout=10),
    ]

    for variant_kwargs in variants:
        recorder = _Recorder()
        _patch_run(recorder)
        try:
            run_ssh("10.0.14.43", "tetrel", ["echo", "hi"], **variant_kwargs)
            for opt in control_opts:
                assert opt in recorder.args, (
                    f"expected {opt} in every variant, missing from {recorder.args} "
                    f"(variant={variant_kwargs})"
                )
        finally:
            _restore_run(original)
    print("PASS: ControlMaster/ControlPersist/ControlPath present in every variant")


def test_timeout_expired_returns_returncode_124():
    original = ssh_mod.subprocess.run

    def _raise_timeout(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout", 10))

    _patch_run(_raise_timeout)
    try:
        res = run_ssh("10.0.14.43", "tetrel", ["echo", "hi"], timeout=1)
        assert res.returncode == 124, f"expected returncode 124 on timeout, got {res.returncode}"
        print("PASS: simulated TimeoutExpired returns returncode == 124")
    finally:
        _restore_run(original)


def test_generic_exception_returns_returncode_1():
    original = ssh_mod.subprocess.run

    def _raise_generic(args, **kwargs):
        raise OSError("no route to host")

    _patch_run(_raise_generic)
    try:
        res = run_ssh("10.0.14.43", "tetrel", ["echo", "hi"])
        assert res.returncode == 1, f"expected returncode 1 on generic exception, got {res.returncode}"
        assert "no route to host" in res.stderr
        print("PASS: generic exception returns returncode == 1 with exception text in stderr")
    finally:
        _restore_run(original)


if __name__ == "__main__":
    test_default_connect_timeout_and_no_tty()
    test_tty_true_places_dash_t_immediately_after_ssh()
    test_connect_timeout_override()
    test_capture_true_passes_capture_output_true()
    test_capture_false_passes_no_capture_kwarg()
    test_user_none_resolves_from_cluster_config_and_explicit_overrides()
    test_control_master_options_present_in_every_variant()
    test_timeout_expired_returns_returncode_124()
    test_generic_exception_returns_returncode_1()
    print("\nAll tests passed.")
