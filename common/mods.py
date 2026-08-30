"""
common/mods.py -- Task MB: bake, cache, and tag resolution for recipe.mods.

Continues from Task MA, which gave RecipeConfig.mods a real type (list of
bare directory names, shape-validated, existence deliberately unchecked at
catalog-load time). This module is where existence gets checked, and where
a mod set actually turns into something `docker run` can use: given a base
image tag and an ordered list of mod names, resolve_and_bake_mods() below
returns the tag of an image with those mods applied, baking it on the
target host first if that tag isn't already there.

Task MC wires this into _execute_deployment_impl() (per host, before that
host's `docker run`) -- not done here. This module has no dependency on
dgx-orchestrator.py and does not import it.

Design constraints (from PHASE-MODS-PROMPTS.md's "Three constraints" --
restated here because violating any of them silently is exactly the
failure mode this whole feature exists to avoid):

  1. Mod payloads must be vendored in-repo under MODS_DIR. This module
     never fetches anything over the network at bake time -- it only reads
     local files and ships them over SSH/scp. A mod's bytes come from what
     is on disk in this repo at bake time, full stop.
  2. Per-host bake (no shared registry, no `docker save | ssh docker load`)
     is safe only because of constraint 1: the same mod_names always
     produce the same bytes, so baking independently on two hosts produces
     the same image content under the same tag.
  3. Every run.sh executes with WORKSPACE_DIR set to the image's *actual*
     Config.WorkingDir, read fresh from the specific base_image being
     baked (via `docker inspect` on the throwaway container, not a
     hardcoded path) -- see _bake_on_host() below. M0 measured this as
     `/workspace/vllm` for eugr/spark-vllm-b12x:latest specifically; this
     module does not assume that holds for any other image.

Non-obvious implementation detail found while building this (flagged here
because a literal reading of "override the entrypoint, commit, done" ships
a broken image): `docker commit` bakes the *container's current* config,
not the base image's original config, for any field the container's
config was actively overridden on. The throwaway container's entrypoint
has to be overridden (to something inert that stays alive for `docker
exec`) for the whole bake -- so a plain `docker commit` at the end would
ship the derived image with Entrypoint=["sleep"] instead of the base
image's real entrypoint (the NGC CUDA-banner wrapper, per M0). That would
pass M0-style config-fidelity checks trivially (there'd be no base to
diff against in a real deploy) and only surface as a broken container at
serve time. _bake_on_host() reads the base image's real Entrypoint/Cmd
before overriding them, and restores both explicitly via `docker commit
--change` after the mods run. Every other Config field M0 checked
(Env/WorkingDir/Labels/ExposedPorts) is untouched by the bake and needs no
such restoration.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from typing import Optional

from common.recipes import MODS_DIR, validate_mod_name
from common.ssh import resolve_user_identity_key, run_ssh
from common.config import load_cluster_config

# Docker tag component grammar: [a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}
_TAG_INVALID_CHARS = re.compile(r"[^A-Za-z0-9_.-]")
_TAG_MAX_LEN = 128
_MOD_HASH_LEN = 16  # matches common/recipes.py's compute_config_hash() truncation length


class ModResolutionError(ValueError):
    """
    Raised by resolve_mod_tag() when a recipe references a mods/<name>
    directory that does not exist under MODS_DIR. This is the deploy-time
    existence check Task MA deliberately deferred out of catalog loading
    (see RecipeConfig.mods / _validate_mod_name() in common/recipes.py) --
    it belongs here, at the point a deploy is actually being resolved, so
    one missing mod directory fails the one deploy that references it
    rather than emptying the whole catalog.

    Subclasses ValueError so a bare `except ValueError` at a call site
    that hasn't been updated to know about this module still catches it;
    callers that want to distinguish "this specific mod is missing" from
    other ValueErrors can catch ModResolutionError directly.
    """


class ModBakeError(RuntimeError):
    """
    Raised when baking a mod set onto a specific host fails, for any
    reason: shipping a mod directory over, creating/starting the throwaway
    container, a run.sh exiting non-zero, the commit itself, or the
    post-commit existence verification. Every raise site in this module
    includes the host and the target tag in the message -- see M0's report
    on why a missing locally-baked tag otherwise reads as a registry
    credentials problem (Docker's actual error in that case is `pull
    access denied ... repository does not exist`, which sends an operator
    down entirely the wrong path on a 2-node deploy where the bake
    succeeded on one host and failed on the other).
    """


def _split_repo_tag(image: str) -> tuple[str, str]:
    """
    Splits "repo:tag" into ("repo", "tag"). If there's no ':' at all,
    treats the tag as "latest" (Docker's own default). Guards the
    registry-with-port case (e.g. "localhost:5000/repo") by only treating
    the LAST colon as a tag separator when nothing after it contains '/' --
    a colon followed by a '/' means it was a host:port, not a tag.
    """
    if ":" not in image:
        return image, "latest"
    repo, _, tag = image.rpartition(":")
    if "/" in tag:
        return image, "latest"
    return repo, tag


def _sanitize_tag_component(component: str) -> str:
    """Coerces an arbitrary string into a legal Docker tag component."""
    cleaned = _TAG_INVALID_CHARS.sub("-", component)
    if not cleaned or not re.match(r"^[A-Za-z0-9_]", cleaned):
        cleaned = f"t{cleaned}"
    return cleaned


def _hash_mod_set(mod_names: list[str]) -> str:
    """
    Hashes the mod set: names AND file contents, in declared order.
    Assumes every mods/<name> in mod_names already exists on disk --
    resolve_mod_tag() below validates that before calling this.

    Declaration order is part of the hash (mod_names is iterated as given,
    not sorted) -- two mods that both touch the same file produce a
    materially different result depending on which ran second, so the tag
    must depend on the order they're declared in the recipe, not just
    which mods are present. Within a single mod directory, files ARE
    walked in sorted-path order, purely for hash determinism across
    filesystems with different directory-entry ordering; that has no
    bearing on run.sh execution order, which is a separate, explicit
    per-mod-directory concept.
    """
    h = hashlib.sha256()
    for name in mod_names:
        h.update(f"MOD:{name}\n".encode())
        mod_dir = MODS_DIR / name
        rel_paths = sorted(p.relative_to(mod_dir) for p in mod_dir.rglob("*") if p.is_file())
        for rel_path in rel_paths:
            h.update(str(rel_path).encode())
            h.update((mod_dir / rel_path).read_bytes())
    return h.hexdigest()[:_MOD_HASH_LEN]


def resolve_mod_tag(base_image: str, mod_names: list[str]) -> str:
    """
    Pure/local: no SSH, no host access. Returns base_image unchanged if
    mod_names is empty (the strict-no-op case -- every existing recipe
    today). Otherwise validates that every named mod directory exists
    under MODS_DIR (raising ModResolutionError, naming every missing one,
    if not) and returns the deterministic derived tag
    "<base-repo>:<sanitized-base-tag>-mods-<hash>".

    Safe to call from a --dry-run path: it never touches the network or a
    host, so "report the resolved tag and what would be baked" (Task MC's
    dry-run requirement) can call this directly.
    """
    if not mod_names:
        return base_image

    # Defense in depth, not a redundant check: this module is independently
    # importable/callable outside the normal RecipeConfig -> load_recipes()
    # path (tests, a future CLI tool, a REPL), so it cannot assume every
    # mod_names list it's ever handed has already passed through Task MA's
    # check_mods_are_bare_names(). Reuses that exact validator (via
    # common.recipes.validate_mod_name()) rather than re-implementing the
    # same rule a second time -- see that function's docstring. Re-raised
    # as ModResolutionError so every failure mode this module's public API
    # can produce is one of this module's two documented exception types,
    # not a bare ValueError leaking a different module's error shape.
    for name in mod_names:
        try:
            validate_mod_name(name)
        except ValueError as exc:
            raise ModResolutionError(str(exc)) from exc

    missing = [name for name in mod_names if not (MODS_DIR / name).is_dir()]
    if missing:
        plural = "y" if len(missing) == 1 else "ies"
        raise ModResolutionError(
            f"Mod director{plural} not found under {MODS_DIR}: {', '.join(missing)}. "
            f"Referenced mods for this deploy: {mod_names!r}."
        )

    digest = _hash_mod_set(mod_names)
    repo, tag = _split_repo_tag(base_image)
    suffix = f"-mods-{digest}"
    base_component = _sanitize_tag_component(tag)[: _TAG_MAX_LEN - len(suffix)]
    return f"{repo}:{base_component}{suffix}"


def _scp_to_host(local_path: Path, ip: str, user: str, remote_dir: str, key_path: str, timeout: int = 300) -> subprocess.CompletedProcess:
    """
    Recursively copies local_path (a single mod directory) into remote_dir
    on the host. Standalone scp invocation rather than routed through
    run_ssh(): run_ssh() has no stdin plumbing (see common/ssh.py's module
    docstring), and piping directory contents through argv or a base64
    blob isn't worth building when scp already does exactly this. Mirrors
    run_ssh()'s connection options exactly, including ControlPath, so this
    transparently reuses whatever ControlMaster connection run_ssh() has
    already established to the same host rather than paying for a second
    SSH handshake.
    """
    scp_cmd = [
        "scp", "-r",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=10",
        "-o", "ControlMaster=auto",
        "-o", "ControlPersist=60s",
        "-o", "ControlPath=/tmp/cm-%C",
        "-i", key_path,
        str(local_path),
        f"{user}@{ip}:{remote_dir}",
    ]
    try:
        return subprocess.run(scp_cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            args=scp_cmd, returncode=124, stdout="",
            stderr=f"scp of {local_path} to {ip}:{remote_dir} timed out after {timeout}s: {exc}",
        )


def _bake_on_host(host: str, ip: str, user: str, key_path: str, base_image: str, mod_names: list[str], tag: str, run_timeout_sec: int) -> None:
    """
    Does the actual bake on one host. Raises ModBakeError on any failure;
    always leaves no dangling container or staging dir behind, whether it
    succeeds or fails (see _cleanup() below). Caller (ensure_mods_baked())
    has already confirmed the tag does NOT exist on this host -- this
    function does not re-check that.
    """
    bake_id = f"dgx-mods-bake-{hashlib.sha256(tag.encode()).hexdigest()[:8]}"
    remote_staging = f"/tmp/{bake_id}"
    container_name = bake_id

    def _cleanup(remove_container: bool = True) -> None:
        if remove_container:
            run_ssh(ip, user, ["docker", "rm", "-f", container_name], timeout=30)
        run_ssh(ip, user, ["rm", "-rf", remote_staging], timeout=15)

    mkdir_res = run_ssh(ip, user, ["mkdir", "-p", remote_staging], timeout=15)
    if mkdir_res.returncode != 0:
        raise ModBakeError(f"[{host}] Failed to create staging dir {remote_staging} for tag {tag}: {mkdir_res.stderr}")

    for name in mod_names:
        scp_res = _scp_to_host(MODS_DIR / name, ip, user, remote_staging + "/", key_path)
        if scp_res.returncode != 0:
            _cleanup(remove_container=False)
            raise ModBakeError(f"[{host}] Failed to ship mod '{name}' to {ip}:{remote_staging} for tag {tag}: {scp_res.stderr}")

    # Read the base image's REAL entrypoint/cmd before we override them for
    # the bake -- these get restored via `docker commit --change` at the
    # end. See this module's docstring for why skipping this ships a
    # broken image.
    entrypoint_res = run_ssh(ip, user, ["docker", "inspect", "--format", "{{json .Config.Entrypoint}}", base_image], timeout=15)
    cmd_res = run_ssh(ip, user, ["docker", "inspect", "--format", "{{json .Config.Cmd}}", base_image], timeout=15)
    if entrypoint_res.returncode != 0 or cmd_res.returncode != 0:
        _cleanup(remove_container=False)
        raise ModBakeError(
            f"[{host}] Could not read {base_image}'s Config.Entrypoint/Config.Cmd before baking "
            f"tag {tag} (needed to restore them after the throwaway container's entrypoint is "
            f"overridden for the bake): {entrypoint_res.stderr or cmd_res.stderr}"
        )
    orig_entrypoint = entrypoint_res.stdout.strip()
    orig_cmd = cmd_res.stdout.strip()

    create_res = run_ssh(
        ip, user,
        ["docker", "create", "--name", container_name, "--entrypoint", "sleep", base_image, "infinity"],
        timeout=30,
    )
    if create_res.returncode != 0:
        _cleanup(remove_container=False)
        raise ModBakeError(f"[{host}] Failed to create throwaway container from {base_image} for tag {tag}: {create_res.stderr}")

    start_res = run_ssh(ip, user, ["docker", "start", container_name], timeout=30)
    if start_res.returncode != 0:
        _cleanup()
        raise ModBakeError(f"[{host}] Failed to start throwaway container for tag {tag}: {start_res.stderr}")

    workspace_res = run_ssh(ip, user, ["docker", "inspect", "--format", "{{.Config.WorkingDir}}", container_name], timeout=15)
    workspace_dir = workspace_res.stdout.strip()
    if workspace_res.returncode != 0 or not workspace_dir:
        _cleanup()
        raise ModBakeError(
            f"[{host}] Could not determine WorkingDir for {base_image} (required as WORKSPACE_DIR "
            f"per constraint 3) while baking tag {tag}: {workspace_res.stderr or 'empty WorkingDir'}"
        )

    # `docker cp` can create the FINAL path component of its destination if
    # missing, but not a missing parent -- copying into a not-yet-existing
    # /tmp/mods/<name> fails with "Could not find the file /tmp/mods in
    # container" rather than creating it. mkdir it explicitly first.
    mkdir_container_res = run_ssh(ip, user, ["docker", "exec", container_name, "mkdir", "-p", "/tmp/mods"], timeout=15)
    if mkdir_container_res.returncode != 0:
        _cleanup()
        raise ModBakeError(f"[{host}] Failed to create /tmp/mods inside throwaway container for tag {tag}: {mkdir_container_res.stderr}")

    for name in mod_names:
        cp_res = run_ssh(
            ip, user,
            ["docker", "cp", f"{remote_staging}/{name}", f"{container_name}:/tmp/mods/{name}"],
            timeout=30,
        )
        if cp_res.returncode != 0:
            _cleanup()
            raise ModBakeError(f"[{host}] Failed to copy mod '{name}' into throwaway container for tag {tag}: {cp_res.stderr}")

        run_res = run_ssh(
            ip, user,
            ["docker", "exec", "-e", f"WORKSPACE_DIR={workspace_dir}", container_name, "bash", f"/tmp/mods/{name}/run.sh"],
            timeout=run_timeout_sec,
        )
        if run_res.returncode != 0:
            _cleanup()
            raise ModBakeError(
                f"[{host}] Mod '{name}' failed while baking tag {tag} "
                f"(run.sh exited {run_res.returncode}). Aborting -- a half-applied mod set is worse "
                f"than a refused deploy.\nstderr:\n{run_res.stderr.strip()}"
            )

    commit_cmd = ["docker", "commit"]
    commit_cmd += ["--change", f"ENTRYPOINT {orig_entrypoint}" if orig_entrypoint and orig_entrypoint != "null" else "ENTRYPOINT []"]
    commit_cmd += ["--change", f"CMD {orig_cmd}" if orig_cmd and orig_cmd != "null" else "CMD []"]
    commit_cmd += [container_name, tag]
    commit_res = run_ssh(ip, user, commit_cmd, timeout=60)
    if commit_res.returncode != 0:
        _cleanup()
        raise ModBakeError(f"[{host}] docker commit failed for tag {tag}: {commit_res.stderr}")

    _cleanup()

    verify_res = run_ssh(ip, user, ["docker", "image", "inspect", tag], timeout=15)
    if verify_res.returncode != 0:
        raise ModBakeError(
            f"[{host}] docker commit for {tag} reported success but `docker image inspect {tag}` "
            f"fails on {host} immediately afterward. Refusing to let a later `docker run` discover "
            f"this the hard way -- see this module's docstring / Task MB."
        )


def ensure_mods_baked(host: str, ip: str, base_image: str, mod_names: list[str], user: Optional[str] = None, run_timeout_sec: int = 600) -> str:
    """
    Resolves (base_image, mod_names) to a tag, baking it on `ip` first if
    that tag does not already exist there. Idempotent: calling this twice
    with the same arguments against a host that already has the tag makes
    exactly one SSH round trip (the `docker image inspect` existence
    check) and bakes nothing.

    Empty mod_names is a strict no-op -- returns base_image unchanged, no
    SSH connections made at all. This is the path every existing recipe
    takes today (mods: [] everywhere, per Task MA), and is what lets Task
    MC guarantee zero behavioural change for the current catalog.

    Raises ModResolutionError (before any SSH) if a named mod directory
    doesn't exist under MODS_DIR -- cheap and host-independent, so this
    also covers --dry-run's "report what would be baked" need.

    Raises ModBakeError if baking fails on THIS host for any reason. Every
    ModBakeError names both `host` and `tag`.
    """
    tag = resolve_mod_tag(base_image, mod_names)
    if not mod_names:
        return tag  # == base_image; no SSH made

    if user is None:
        user = load_cluster_config().ssh_user
    key_path = resolve_user_identity_key()

    exists = run_ssh(ip, user, ["docker", "image", "inspect", tag], timeout=15)
    if exists.returncode == 0:
        return tag  # already baked on this host

    _bake_on_host(host, ip, user, key_path, base_image, mod_names, tag, run_timeout_sec)
    return tag
