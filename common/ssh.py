"""
Unified SSH transport for the DGX orchestrator tooling.

Consolidates run_ssh / resolve_user_identity_key / get_hf_token, which
previously existed as two near-verbatim-but-drifted copies in
dgx-orchestrator.py and cache_cluster_assets.py. Both call sites can use
this single implementation without behavioral change:

  - dgx-orchestrator.py wants capture=True (default), a short
    connect_timeout (5s), no TTY, and a short overall timeout (10s) -
    dead hosts must fail fast, not hang.
  - cache_cluster_assets.py wants capture=False so `docker pull` / model
    download progress bars stream live to the terminal, tty=True so the
    remote process gets a pseudo-TTY to render those bars, a longer
    connect_timeout (10s), and long overall timeouts (1800-3600s) for
    slow transfers.

Only dgx-orchestrator.py should ever SSH into the Sparks; that invariant
is unaffected by this module existing, since it's still dgx-orchestrator.py
(and cache_cluster_assets.py, its asset-prefetch sibling) calling into it.
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
from pathlib import Path

from common.config import load_cluster_config

# Mirrors common/config.py's BASE_DIR resolution: this file lives in
# common/, so the repo root is one level up. BASE_DIR can still be
# overridden via the BASE_DIR env var, same as dgx-orchestrator.py and
# cache_cluster_assets.py.
BASE_DIR = Path(os.getenv("BASE_DIR", Path(__file__).resolve().parent.parent))


def resolve_user_identity_key() -> str:
    """
    OpenSSH strictness (0600) workaround for shared keys (0640).
    Auto-stages a copy of the shared key into ~/.ssh/<ssh_key_name> to
    prevent permission denied errors when routing cluster commands.

    The shared key's filename is sourced from cluster_config.yaml's
    ssh_key_name (previously the hardcoded "id_dgx_orchestrator" string
    in dgx-orchestrator.py); all other behavior, including the BASE_DIR
    resolution and the id_ed25519 / shared-key fallbacks, is identical.
    """
    key_name = load_cluster_config().ssh_key_name
    shared_key_path = BASE_DIR / key_name

    user_ssh_dir = Path.home() / ".ssh"
    user_ssh_dir.mkdir(parents=True, exist_ok=True)
    target_key = user_ssh_dir / key_name

    if shared_key_path.exists():
        try:
            if not target_key.exists() or target_key.stat().st_mtime < shared_key_path.stat().st_mtime:
                shutil.copy2(shared_key_path, target_key)
                os.chmod(target_key, 0o600)
            return str(target_key)
        except Exception:
            pass

    default_key = user_ssh_dir / "id_ed25519"
    if default_key.exists():
        return str(default_key)

    return str(shared_key_path)


def get_hf_token() -> str:
    """Extracts HuggingFace authentication token with safe fallbacks and warnings."""
    if "HF_TOKEN" in os.environ and os.environ["HF_TOKEN"].strip():
        return os.environ["HF_TOKEN"].strip()

    secrets_file = BASE_DIR / ".secrets"
    if secrets_file.exists():
        try:
            for line in secrets_file.read_text().splitlines():
                key, sep, value = line.partition("=")
                if sep and key.strip().upper() == "HF_TOKEN":
                    token = value.strip().strip('"').strip("'")
                    if token:
                        return token
                    print(f"[!] Warning: {secrets_file} has an HF_TOKEN line but its value is empty after "
                          f"stripping whitespace/quotes. Checking local cache...")
                    break
        except PermissionError:
            print(f"[!] Warning: You do not have permission to read {secrets_file}. Checking local cache...")
        except Exception as exc:
            print(f"[!] Warning: Failed to parse {secrets_file} ({type(exc).__name__}: {exc}). "
                  f"Checking local cache...")

    hf_token_file = Path.home() / ".cache" / "huggingface" / "token"
    if hf_token_file.exists():
        try:
            token = hf_token_file.read_text().strip()
            if token:
                return token
            print(f"[!] Warning: {hf_token_file} exists but is empty.")
        except Exception as exc:
            print(f"[!] Warning: Failed to read {hf_token_file} ({type(exc).__name__}: {exc}).")

    print("[!] Warning: No HF_TOKEN found in env, .secrets, or ~/.cache. Gated models may fail to download.")
    return ""


def run_ssh(
    ip: str,
    user: str | None = None,
    command_list: list | None = None,
    capture: bool = True,
    timeout: int = 10,
    tty: bool = False,
    connect_timeout: int = 5,
) -> subprocess.CompletedProcess:
    """
    Executes remote commands via SSH with quoted token evaluation.

    user=None resolves to ssh_user from cluster_config.yaml; an explicit
    user argument always wins.

    capture=True runs with capture_output=True, text=True (the
    orchestrator's default: callers parse res.stdout). capture=False runs
    with NO capture kwargs at all, so output streams live to the caller's
    terminal (the asset-cache path: docker pull / snapshot_download
    progress bars). This distinction is load-bearing and must not be
    collapsed.

    tty=True inserts -t as the first argument after ssh, allocating a
    pseudo-TTY (needed for progress bars to render over SSH).

    connect_timeout populates -o ConnectTimeout=N; timeout is the overall
    subprocess timeout.

    Process-tree cleanup on timeout: the ssh child is launched via
    os.setsid (its own session/process group, separate from ours) so that
    a timeout kills the WHOLE tree via os.killpg, not just the immediate
    ssh process plain subprocess.run(..., timeout=...) would track.
    Confirmed by direct test that this matters: with ControlPersist
    backgrounding, the tracked ssh process can exit/be killed well within
    `timeout` while a persisted control-master child it spawned keeps
    running as an orphan indefinitely afterward -- subprocess.run's
    single-process kill does correctly bound OUR wait time (it does not
    hang), but leaves that orphan behind. Over a long daemon uptime that
    orphan accumulation is real, undesirable process/fd/PID leakage, even
    though it isn't what caused a WORKER_POOL thread to look "stuck"
    (that thread does get its result back on schedule either way -- see
    dgx-orchestrator.py's WORKER_POOL sizing/sharing for the more likely
    explanation for actual multi-hour status-poll staleness, which is a
    separate concern from this file).
    """
    if user is None:
        user = load_cluster_config().ssh_user
    if command_list is None:
        command_list = []

    key_path = resolve_user_identity_key()
    quoted_remote_cmd = " ".join(shlex.quote(str(arg)) for arg in command_list)

    ssh_cmd = ["ssh"]
    if tty:
        ssh_cmd.append("-t")  # Allocate pseudo-TTY for progress bars

    ssh_cmd.extend([
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", f"ConnectTimeout={connect_timeout}",
        # Detect a connection that LOOKS established at the TCP level but
        # is actually dead (peer vanished without a clean FIN/RST -- host
        # reboot, network partition, VM pause, etc.) instead of relying
        # solely on our own outer subprocess timeout to eventually notice.
        # 2 missed keepalives at 5s apart = ssh gives up on its own within
        # ~10-15s of genuine silence. Harmless during an active transfer
        # (data flow itself demonstrates liveness); only matters for
        # otherwise-idle connections, which is exactly the long-lived,
        # mostly-idle ControlPersist master case this is aimed at.
        "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=2",
        # Reuse one TCP+auth handshake per host across calls instead of
        # paying a fresh SSH negotiation every time. dgx-config already
        # cleans up /tmp/cm-* sockets on every invocation - this is what
        # actually creates the sockets it was cleaning up.
        "-o", "ControlMaster=auto",
        "-o", "ControlPersist=60s",
        "-o", "ControlPath=/tmp/cm-%C",
        "-i", key_path,
        f"{user}@{ip}",
        quoted_remote_cmd,
    ])

    popen_kwargs = {"preexec_fn": os.setsid}
    if capture:
        popen_kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    # capture=False: no stdout/stderr kwargs at all, same as before -- the
    # child inherits our real stdout/stderr so progress bars stream live.

    proc = subprocess.Popen(ssh_cmd, **popen_kwargs)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(args=ssh_cmd, returncode=proc.returncode, stdout=stdout, stderr=stderr)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass  # already gone
        try:
            # Short, not a second full timeout budget: if anything is
            # still holding the pipe open at this point, it's because it
            # deliberately detached into its own session (e.g. a real
            # ControlPersist master, by design -- see this function's
            # docstring) and killpg cannot reach it regardless of how
            # long we wait here. Waiting longer only adds latency without
            # improving the outcome.
            stdout, stderr = proc.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        return subprocess.CompletedProcess(args=ssh_cmd, returncode=124, stdout=stdout, stderr=stderr or "Command execution timed out.")
    except Exception as e:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        return subprocess.CompletedProcess(args=ssh_cmd, returncode=1, stdout="", stderr=str(e))
