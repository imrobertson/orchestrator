#!/usr/bin/env python3
"""
Plain-assert tests for common/ssh.py. Run with:
    python3 tests/test_ssh.py

subprocess.Popen is replaced in every test so no real SSH connection is
attempted; we only inspect the argv/kwargs run_ssh constructs.
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import common.ssh as ssh_mod
from common.ssh import run_ssh


class _FakePopen:
    """Popen-shaped recorder with configurable communicate() behavior."""

    calls = []
    total_calls = 0
    next_returncode = 0
    next_stdout = ""
    next_stderr = ""
    next_communicate_effects = []

    @classmethod
    def reset(cls, *, returncode=0, stdout="", stderr="",
              communicate_effects=None):
        cls.calls = []
        cls.next_returncode = returncode
        cls.next_stdout = stdout
        cls.next_stderr = stderr
        cls.next_communicate_effects = list(communicate_effects or [])

    def __init__(self, args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.returncode = type(self).next_returncode
        self.pid = 999999999
        self._stdout = type(self).next_stdout
        self._stderr = type(self).next_stderr
        self._communicate_effects = list(type(self).next_communicate_effects)
        self.communicate_timeouts = []
        type(self).calls.append(self)
        type(self).total_calls += 1

    def communicate(self, timeout=None):
        self.communicate_timeouts.append(timeout)
        if self._communicate_effects:
            effect = self._communicate_effects.pop(0)
            if isinstance(effect, BaseException):
                raise effect
            return effect
        return self._stdout, self._stderr


def _patch_popen(**config):
    original = ssh_mod.subprocess.Popen
    _FakePopen.reset(**config)
    ssh_mod.subprocess.Popen = _FakePopen
    return original


def _restore_popen(original):
    ssh_mod.subprocess.Popen = original


def _captured_popen(expected_calls=1):
    assert len(_FakePopen.calls) == expected_calls, (
        f"FakePopen was invoked {len(_FakePopen.calls)} time(s), expected "
        f"{expected_calls}; refusing to inspect an unproven transport double"
    )
    return _FakePopen.calls[-1]


def test_default_connect_timeout_and_no_tty():
    original = _patch_popen()
    try:
        run_ssh("10.0.14.43", "tetrel", ["echo", "hi"])
        proc = _captured_popen()
        assert "-o" in proc.args and "ConnectTimeout=5" in proc.args, (
            f"expected ConnectTimeout=5 in {proc.args}"
        )
        assert "-t" not in proc.args, f"did not expect -t in default call: {proc.args}"
        print("PASS: default call produces ConnectTimeout=5 and no -t")
    finally:
        _restore_popen(original)


def test_tty_true_places_dash_t_immediately_after_ssh():
    original = _patch_popen()
    try:
        run_ssh("10.0.14.43", "tetrel", ["docker", "pull", "img"], tty=True)
        proc = _captured_popen()
        assert proc.args[0] == "ssh", f"expected ssh cmd first, got {proc.args}"
        assert proc.args[1] == "-t", f"expected -t immediately after ssh, got {proc.args}"
        print("PASS: tty=True produces -t immediately after ssh")
    finally:
        _restore_popen(original)


def test_connect_timeout_override():
    original = _patch_popen()
    try:
        run_ssh("10.0.14.43", "tetrel", ["echo", "hi"], connect_timeout=10)
        proc = _captured_popen()
        assert "ConnectTimeout=10" in proc.args, (
            f"expected ConnectTimeout=10 in {proc.args}"
        )
        print("PASS: connect_timeout=10 produces ConnectTimeout=10")
    finally:
        _restore_popen(original)


def test_capture_true_passes_pipe_capture_kwargs():
    original = _patch_popen()
    try:
        run_ssh("10.0.14.43", "tetrel", ["echo", "hi"], capture=True)
        proc = _captured_popen()
        assert proc.kwargs.get("stdout") is subprocess.PIPE, (
            f"expected stdout=PIPE, got kwargs={proc.kwargs}"
        )
        assert proc.kwargs.get("stderr") is subprocess.PIPE, (
            f"expected stderr=PIPE, got kwargs={proc.kwargs}"
        )
        assert proc.kwargs.get("text") is True, (
            f"expected text=True, got kwargs={proc.kwargs}"
        )
        print("PASS: capture=True passes stdout/stderr PIPE and text=True")
    finally:
        _restore_popen(original)


def test_capture_false_passes_no_capture_kwargs():
    original = _patch_popen()
    try:
        run_ssh("10.0.14.43", "tetrel", ["docker", "pull", "img"], capture=False)
        proc = _captured_popen()
        for name in ("stdout", "stderr", "text"):
            assert name not in proc.kwargs, (
                f"expected no {name} kwarg, got kwargs={proc.kwargs}"
            )
        print("PASS: capture=False passes no stdout/stderr/text kwargs")
    finally:
        _restore_popen(original)


def test_user_none_resolves_from_cluster_config_and_explicit_overrides():
    original = _patch_popen()
    try:
        run_ssh("10.0.14.43", None, ["echo", "hi"])
        proc = _captured_popen()
        assert "tetrel@10.0.14.43" in proc.args, (
            f"expected user resolved to tetrel from cluster_config.yaml, got {proc.args}"
        )
        print("PASS: user=None resolves to tetrel from cluster_config.yaml")

        run_ssh("10.0.14.43", "someone_else", ["echo", "hi"])
        proc = _captured_popen(expected_calls=2)
        assert "someone_else@10.0.14.43" in proc.args, (
            f"expected explicit user to override, got {proc.args}"
        )
        assert "tetrel@10.0.14.43" not in proc.args
        print("PASS: explicit user overrides cluster_config.yaml default")
    finally:
        _restore_popen(original)


def test_control_master_options_present_in_every_variant():
    control_opts = [
        "ControlMaster=auto", "ControlPersist=60s", "ControlPath=/tmp/cm-%C"
    ]
    variants = [
        dict(capture=True, tty=False),
        dict(capture=False, tty=True),
        dict(capture=True, tty=True, connect_timeout=10),
    ]

    for variant_kwargs in variants:
        original = _patch_popen()
        try:
            run_ssh("10.0.14.43", "tetrel", ["echo", "hi"], **variant_kwargs)
            proc = _captured_popen()
            for opt in control_opts:
                assert opt in proc.args, (
                    f"expected {opt} in every variant, missing from {proc.args} "
                    f"(variant={variant_kwargs})"
                )
        finally:
            _restore_popen(original)
    print("PASS: ControlMaster/ControlPersist/ControlPath present in every variant")


def test_timeout_expired_returns_returncode_124():
    timeout = subprocess.TimeoutExpired(cmd=["ssh"], timeout=1)
    original = _patch_popen(communicate_effects=[timeout, ("", "")])
    try:
        res = run_ssh("10.0.14.43", "tetrel", ["echo", "hi"], timeout=1)
        proc = _captured_popen()
        assert proc.communicate_timeouts == [1, 1], (
            f"expected timed-out communicate then 1s cleanup, got "
            f"{proc.communicate_timeouts}"
        )
        assert res.returncode == 124, (
            f"expected returncode 124 on timeout, got {res.returncode}"
        )
        print("PASS: simulated communicate() TimeoutExpired returns returncode == 124")
    finally:
        _restore_popen(original)


def test_generic_exception_returns_returncode_1():
    original = _patch_popen(communicate_effects=[OSError("no route to host")])
    try:
        res = run_ssh("10.0.14.43", "tetrel", ["echo", "hi"])
        _captured_popen()
        assert res.returncode == 1, (
            f"expected returncode 1 on generic exception, got {res.returncode}"
        )
        assert "no route to host" in res.stderr
        print("PASS: simulated communicate() exception returns returncode == 1")
    finally:
        _restore_popen(original)


if __name__ == "__main__":
    _FakePopen.total_calls = 0
    test_default_connect_timeout_and_no_tty()
    test_tty_true_places_dash_t_immediately_after_ssh()
    test_connect_timeout_override()
    test_capture_true_passes_pipe_capture_kwargs()
    test_capture_false_passes_no_capture_kwargs()
    test_user_none_resolves_from_cluster_config_and_explicit_overrides()
    test_control_master_options_present_in_every_variant()
    test_timeout_expired_returns_returncode_124()
    test_generic_exception_returns_returncode_1()
    assert _FakePopen.total_calls == 12, (
        f"expected FakePopen to intercept all 12 launches, got "
        f"{_FakePopen.total_calls}"
    )
    print("\nPASS: FakePopen intercepted all 12 process launches; no real SSH ran.")
    print("All tests passed.")
