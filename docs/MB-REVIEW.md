# Task MB Review — Bake, cache, and tag resolution

## Status: complete, verified on live hardware — final clean run 25/25

*Revision note: this replaces the first version of this review. That
version's live-hardware Entrypoint/Cmd results were a false pass (both
sides empty due to a test-tooling bug, not a real comparison) and its
"changed files" section predates a scope-crossing hardening fix made
after MA flagged a related contradiction. Both are corrected below.*

---

## What was built

**`common/mods.py`** — the deliverable per PHASE-MODS-PROMPTS.md's Task MB.
Given a base image tag and an ordered list of mod names, resolves and (if
needed) bakes a derived image tag on a target host. Public surface:

- `resolve_mod_tag(base_image, mod_names)` — pure, local, no SSH. Empty
  `mod_names` → `base_image` unchanged. Otherwise validates every named
  mod name's *shape* (via `common.recipes.validate_mod_name()` — see
  "changed files" below) and that the corresponding directory exists
  under `MODS_DIR`, hashes names + file contents in declared order,
  returns `<repo>:<sanitized-tag>-mods-<hash>`.
- `ensure_mods_baked(host, ip, base_image, mod_names, user=None)` — the
  host-side entry point. Skips the bake if the tag already exists on that
  host; otherwise ships the mod directories over, bakes, verifies, and
  cleans up.
- `ModResolutionError` / `ModBakeError` — the two failure modes, each
  always naming the host and/or mod and the target tag.

**`common/recipes.py`** — one small addition, made mid-task, out of MB's
originally declared scope. See "Changed files" below for why.

**`tests/smoke_test_mods.py`** — an automated harness exercising the exact
sequence Task MB's own "Verification" section specifies, plus the extra
scenarios worth checking on real hardware (Entrypoint/Cmd fidelity,
cross-host independence, an optional live serve check).

---

## What was verified, and how

### 1. Pure resolution logic (no hardware) — scripted double

Before ever touching a host, `resolve_mod_tag()` / `_hash_mod_set()` were
exercised against local fixture mod directories with a scripted
`common.ssh.run_ssh` double standing in for the real transport:

- Deterministic: same inputs → same tag, twice.
- Order-sensitive: `[a, b]` and `[b, a]` produce different tags.
- Content-sensitive: editing one byte in a mod's payload changes the tag.
- Missing mod: raises `ModResolutionError`, names the missing directory,
  makes no SSH call.
- **Path-shaped mod name: raises `ModResolutionError` from
  `resolve_mod_tag()` itself** — `../../../etc`, the exact eugr-shaped
  `mods/fix-gemma4-tool-parser` string, `a/b`, and `a\\b` were all
  rejected directly, with no dependency on an upstream caller having
  already gone through `RecipeConfig`'s pydantic validator. This is the
  defense-in-depth fix — see "Changed files" and "Contradictions" below.
- Empty mod set: returns `base_image` unchanged, makes zero SSH calls.
- Idempotent second bake: single `docker image inspect` round trip, no
  rebake.
- Failing `run.sh`: raises `ModBakeError`, no commit issued, throwaway
  container and staging directory both cleaned up.
- Cross-host independence: a tag existing on one host does not cause
  `ensure_mods_baked()` to skip the bake on a second host.

This caught one real bug in `mods.py` before it ever reached hardware
(missing `mkdir -p /tmp/mods` inside the throwaway container — `docker cp`
cannot create a missing *parent* directory, only the final path
component) once the double was tightened to actually enforce that
Docker semantic instead of accepting any `docker cp` call unconditionally.

### 2. Live hardware — spark-4 and spark-3, `eugr/spark-vllm-b12x:latest`

Run via `docker exec -it dgx-orchestrator-api python3 tests/smoke_test_mods.py --serve-check --hf-path Qwen/Qwen3-0.6B`
(matching `dgx-config`'s own execution model — everything runs inside the
container, never on the host directly).

**First live run: 21/22, one failure that led to a second, more serious
finding.** The "mods applied in declared order" check failed because
`docker run --rm <tag> cat mod_order.log` (no `--gpus` passed) legitimately
prints the NGC entrypoint's CUDA banner and driver warning before the file
content — that's the restored entrypoint working correctly, not a real
ordering bug, and the check was too strict. Loosened to look for the
marker lines anywhere in stdout rather than requiring them to be the first
two lines.

Separately, the Entrypoint/Cmd fidelity check "passed" with `'' vs ''` —
both sides empty. That result was **not trustworthy**: this script's own
`ssh_run()` helper (written for independent verification, so checks
wouldn't trust the same code path being tested) was a hand-rolled SSH
invocation that dropped `common/ssh.py`'s `run_ssh()`'s per-argument
`shlex.quote()`-then-join step. OpenSSH re-concatenates multiple argv
elements into one string for the remote shell regardless of local
argv boundaries, so `--format {{json .Config.Entrypoint}}` (an internal
space between `json` and `.Config.Entrypoint`) silently split into
`--format {{json` plus a stray extra argument — `docker inspect` failed,
both sides returned empty stdout, and the check didn't assert `returncode`
so two broken calls agreeing read as a pass. This is the identical failure
class `common/ssh.py`'s own docstring already describes fixing once
(`run_ssh()` "previously existed as two near-verbatim-but-drifted
copies") — reproduced by writing a second SSH transport for "test
independence" instead of reusing the already-correct one. Written up as
`TOMBSTONES.md` #83 (draft delivered separately). Fixed by making
`ssh_run()` a one-line adapter over `common.ssh.run_ssh()`, and by adding
explicit `returncode == 0` assertions to every check that reads stdout.

**Final live run after both fixes: 25/25 passed**, including:

- First bake on spark-4: tag created, present via `docker image inspect`,
  both test mods' `run.sh` executed in declared order (verified via a
  shared log file each mod appends to under `$WORKSPACE_DIR`).
- **Entrypoint/Cmd fidelity — now a real comparison, not a false pass.**
  `docker inspect --format '{{json .Config.Entrypoint}}'` on base vs.
  derived tag returned `["/opt/nvidia/nvidia_entrypoint.sh"]` for both,
  byte-identical (confirmed live: `'["/opt/nvidia/nvidia_entrypoint.sh"]' vs
  '["/opt/nvidia/nvidia_entrypoint.sh"]'`). `Cmd` matched (`null` vs
  `null`). This confirms the throwaway container's `--entrypoint sleep`
  override did NOT leak into the committed image — the `docker commit
  --change` restoration works, verified against real values this time.
- `WorkingDir` preserved at `/workspace/vllm` on both base and derived,
  matching M0's finding.
- No dangling bake containers or `/tmp` staging directories left behind,
  on success or on the deliberate failure path.
- Idempotent second call: same tag, ~0.02s (single existence check, no
  rebake).
- Payload edit → new tag, confirmed present on host.
- Deliberately failing `run.sh` → `ModBakeError` raised, no partial tag
  left behind, cleanup confirmed.
- Independent bake on spark-3 (separate from spark-4's bake) produced the
  byte-identical tag, confirming determinism holds across hosts, not just
  across repeated calls on one host.
- Live serve check: `Qwen/Qwen3-0.6B` actually launched inside the
  mods-derived image via `docker run` + `vllm.entrypoints.openai.api_server`
  — the derived image doesn't just look correct via `docker inspect`, it
  runs.

The defense-in-depth fix (`validate_mod_name()` — see "Changed files"
below) was verified separately via the scripted double only, not
re-confirmed on live hardware: it's pure input validation with zero
SSH/Docker interaction, so the scripted result (four path-shaped inputs
rejected, bare names unaffected, full 24-check regression suite still
clean) is sufficient. Worth a real re-run at some point regardless, as a
matter of course rather than because anything here is expected to surface
new.

---

## Contradictions and things the plan didn't specify

The task doc explicitly wants these surfaced, and says they matter more
than a smooth completion. Four came up:

**1. `docker commit` on a container with an overridden entrypoint silently
carries the override into the derived image — the plan's wording doesn't
mention this.** Task MB's requirements say: "docker create/run a throwaway
container from the base with its normal entrypoint overridden to
something inert, copy the mod directories in, run each run.sh..., then
docker commit to the tag." Read literally, that ships an image whose
`Entrypoint` is `["sleep"]` instead of the base image's real entrypoint —
passing M0-style config checks trivially (there's no real base to diff
against in a naive test) and only surfacing as a broken container at
serve time. `_bake_on_host()` reads the base image's actual
`Config.Entrypoint` / `Config.Cmd` before overriding them for the bake,
and restores both explicitly via `docker commit --change` afterward.
**Now confirmed correct against real values on live hardware** (see
above), not just by design intent.

**2. `docker cp` cannot create a missing parent directory on its
destination inside the container — the plan's "copy the mod directories
in" step doesn't call this out, and it wasn't findable without live
hardware.** `docker cp <src> container:/tmp/mods/<name>` fails with
`Could not find the file /tmp/mods in container` unless `/tmp/mods`
already exists — `docker cp` will create the *final* path component of a
destination that doesn't exist, but not an absent parent. Fixed with an
explicit `docker exec <container> mkdir -p /tmp/mods` before the per-mod
copy loop.

**3. Not a contradiction in `mods.py` itself, but a process lesson worth
recording (`TOMBSTONES.md` #83 draft, delivered separately):** building
the smoke test's independent-verification layer, a second, hand-rolled SSH
transport was written specifically so verification wouldn't trust the
same code path being tested — and doing so reproduced almost exactly the
bug `common/ssh.py`'s own module docstring already describes fixing once.
See the live-hardware section above for the concrete failure this caused
on the first real run (a false-passing Entrypoint/Cmd check). "Independent
verification" should mean not calling `mods.py`'s own functions to check
its own output — not re-deriving SSH transport correctness from scratch.

**4. A held thought from Task MA, confirmed not to affect MB's design but
acted on as a hardening fix.** Eugr's real mod library references mods
with a `mods/` prefix (`mods/fix-gemma4-tool-parser`) — a relative path,
not the bare name MA's schema requires. MA's `check_mods_are_bare_names()`
already rejects that at recipe-load time by design, so this was flagged
as a "know this when you port a mod later" note, not a code contradiction
— confirmed here that MB's build never constructs or receives an
eugr-shaped value anywhere, and the note doesn't block or change anything
MB shipped. Tracing it through surfaced an adjacent, real gap, though:
**`common/mods.py`'s `resolve_mod_tag()` never independently re-validated
the bare-name shape — it fully trusted the caller.** In the real
production path this is fine (MC will only ever call it with
`recipe.mods`, already validated by `RecipeConfig`), but `common/mods.py`
is independently importable and callable outside that path (tests, a
future CLI tool, a REPL). A path-shaped name reaching `resolve_mod_tag()`
directly would resolve via plain `Path` division — which does not itself
reject `..` segments — and could cause MB to `scp`/`exec` the contents of
an arbitrary directory during a bake. Not a live vulnerability today (the
one real call site is already safe), but a defense-in-depth gap: the rule
was only enforced upstream of the module that actually acts on it. Fixed
by exporting MA's `_validate_mod_name()` as a public
`validate_mod_name()` in `common/recipes.py` and calling it from
`resolve_mod_tag()` — one implementation, reused, not a second copy that
could drift from the first. Verified: the exact eugr-shaped string from
the held thought (`mods/fix-gemma4-tool-parser`), a `..` traversal
attempt, and two other path-shaped inputs are all now rejected by
`resolve_mod_tag()` itself; bare names are unaffected; full scripted
regression suite (24 checks) still clean.

---

## Scope check

Per the plan's explicit note: no runtime mod application, no `phase:`
field, and no mod-distribution registry were touched or needed here —
this task's scope (bake/cache/tag-resolution as a standalone function) was
sufficient as specified, and nothing encountered during implementation
surfaced a real need for any of the three excluded items.

One real scope crossing did happen, and is called out explicitly rather
than folded in quietly: `common/recipes.py` — a file Task MA owns, outside
MB's declared scope — was touched to add `validate_mod_name()`. This was a
hardening fix surfaced by tracing through a held thought from MA's own
review, not new MA functionality, not a redesign of MA's validation rule,
and not something MB's design required in order to ship. It's included in
"Changed files" below for that reason.

---

## Changed files, in full

### `common/mods.py`

Task MB's actual deliverable.

```python
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

```

### `common/recipes.py`

**Scope-crossing hardening fix, not new MA functionality.** One addition:
`validate_mod_name()`, a thin public wrapper around the existing
(unchanged, still the only implementation) `_validate_mod_name()`. No
behavior change to `RecipeConfig`, `load_recipes()`, or anything else in
this file.

```python
"""
Typed access to per-model recipes/local/*.yaml and recipes/eugr/*.yaml.

Phase 2 replaces the monolithic models.yaml with one recipe file per model.
This module owns:

  - the recipe schema (TopologyConfig / CapabilityConfig / RecipeConfig)
  - load_recipes(), which globs and validates every recipe file
  - build_catalog_response(), which reassembles the recipes into the exact
    dict shape dgx-orchestrator.py's load_model_catalog() has always
    returned -- {"catalog": {"GLOBAL_HF_HUB_OFFLINE": ..., "models": {...}}}

This lives in its own module rather than being folded into common/config.py
because the two have different lifecycles: config.py governs cluster/host
topology (rarely touched, one file), while this module governs the model
catalog (one file per model, edited constantly as models are added). Keeping
them apart means a change to one schema can't accidentally ripple into the
other's validation, and it mirrors the existing split between
cluster_config.yaml and recipes/.

A recipe's catalog key is its filename stem -- and *only* its filename
stem. Earlier versions of this schema also carried a `name:` field inside
the YAML that was required to match the filename, which meant a model had
two names that could silently drift apart (a typo in one is a working
recipe with a broken registration, invisible until someone reads the
catalog and finds the wrong key -- or, worse, since a bad recipe raises out
of load_recipes() entirely, invisible until the WHOLE catalog goes empty).
There is no `name:` field anymore: the filename is authoritative, so
there's nothing left to disagree with it.

IMPORTANT -- this module builds the catalog response fresh on every call to
build_catalog_response(). load_recipes() is cached (recipe files rarely
change at runtime), but the env_vars lists inside each TopologyConfig must
never be mutated in place: /api/toggle-network can flip the offline flags
between two calls, and each call has to reflect the *current* flags without
leaking state from a previous call or corrupting the cached RecipeConfig
objects. See build_catalog_response() below.
"""

from __future__ import annotations

import functools
import hashlib
import json
import sys
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

from common.config import BASE_DIR, load_cluster_config

RECIPES_DIR = BASE_DIR / "recipes"
RECIPE_SUBDIRS = ("local", "eugr")

# Repo-root directory mod payloads live under. A recipe's mods: entries are
# bare directory names resolved against this at bake time (Task MB) -- never
# host paths, and never anything the orchestrator interpolates directly into
# a shell command without validation first. See RecipeConfig.mods and
# _validate_mod_name() below for the load-time shape check; existence of the
# named directory is deliberately NOT checked here (see module docstring
# addition below and Task MA's ROADMAP.md entry) -- that's a deploy-time
# concern, not a catalog-load concern.
MODS_DIR = BASE_DIR / "mods"

# Only "1" exists today. An unrecognized version is a soft warning, not a
# hard error -- see _load_single_recipe().
SUPPORTED_RECIPE_VERSIONS = ["1"]

# The exact set of per-topology keys the old models.yaml format carried, and
# therefore the exact set build_catalog_response() must emit. `cluster_only`
# is new recipe-authoring metadata (Phase 2 addition) with no equivalent in
# the old format, so it is deliberately excluded from the catalog response.
_TOPOLOGY_OUTPUT_FIELDS = ("max_model_len", "tp_size", "pp_size", "env_vars", "vllm_args")


def _validate_mod_name(name: str) -> str:
    """
    Shape validation only -- does the string look like a bare directory
    name a recipe is allowed to carry? Does NOT check whether mods/<name>
    actually exists on disk: that's Task MB's job, at bake time, per host.
    Doing it here would mean a missing mod directory takes down the whole
    catalog via build_catalog_response()'s fail-closed error handling (see
    that function's docstring) -- exactly the class of incident this
    module's docstring already describes for the old name/filename split.
    A bad mod name should fail the one deploy that references it, not
    every model in the catalog.
    """
    if not isinstance(name, str) or not name:
        raise ValueError(f"mods entries must be non-empty strings, got {name!r}")
    if "/" in name or "\\" in name or ".." in name:
        raise ValueError(
            f"mods entry {name!r} is not a bare directory name. Recipes "
            "must reference mods by name only -- the orchestrator resolves "
            "each name against the repo-root mods/ directory itself. Path "
            "separators and '..' segments are rejected at load time."
        )
    return name


def validate_mod_name(name: str) -> str:
    """
    Public entry point for other modules -- concretely, common/mods.py
    (Task MB) -- to reuse this exact bare-name check rather than
    re-implementing it or trusting a caller to have already gone through
    RecipeConfig's pydantic validation. _validate_mod_name() stays the one
    implementation (this is a thin public wrapper, not a second copy) so
    the rule enforced at recipe-load time and the rule enforced at
    bake/resolve time can never independently drift apart -- the exact
    failure class common/ssh.py's own docstring already describes fixing
    once for a different pair of near-duplicate functions.

    Added as a scope-crossing hardening fix during Task MB, not new Task
    MA functionality: MB's resolve_mod_tag() is independently importable
    and callable outside the normal RecipeConfig -> load_recipes() path
    (tests, a future CLI tool, a REPL), so it cannot safely assume every
    mod_names list it's ever handed has already passed through
    check_mods_are_bare_names(). Without this, a path-shaped name reaching
    resolve_mod_tag() directly (bypassing RecipeConfig) would resolve via
    plain Path division -- which does not itself reject '..' segments --
    and could cause MB to scp/exec the contents of an arbitrary directory
    during a bake.
    """
    return _validate_mod_name(name)


class CapabilityConfig(BaseModel):
    """Unused for now -- populated in Phase 4. See docs/PHASE-2-PROMPTS.md."""

    task: Optional[str] = None
    context_class: Optional[str] = None
    latency_class: Optional[str] = None


class TopologyConfig(BaseModel):
    # Recipe-authoring metadata: intended to flag a topology as valid only
    # as part of the full multi-node cluster (useful for EUGR-synced
    # recipes that assume a cluster deploy target). Currently INERT --
    # not read by build_catalog_response() or by dgx-orchestrator.py's
    # deploy path. Same bucket as RecipeConfig.capability/mods: exists so
    # recipes can carry the field without a second schema migration later,
    # but wiring up real enforcement (and deciding whether/how it should
    # surface in the catalog response) is deliberately out of scope here.
    cluster_only: bool = False
    max_model_len: int
    tp_size: int
    pp_size: int
    env_vars: list[str] = Field(default_factory=list)
    vllm_args: str = ""


class RecipeConfig(BaseModel):
    # No `name` field -- the catalog key is the filename stem, set by
    # load_recipes() below, not anything carried inside the YAML. See the
    # module docstring for why.
    recipe_version: str
    hf_path: str
    image: Optional[str] = None
    gpu_util: float
    capability: CapabilityConfig = Field(default_factory=CapabilityConfig)
    # Each entry is a bare directory name, resolved against the repo-root
    # mods/ directory by the orchestrator (never a host path -- see
    # _validate_mod_name()). Shape-validated here (load time); existence of
    # mods/<name> is deliberately NOT checked here, only at deploy time --
    # see _validate_mod_name()'s docstring. Still execution-inert as of
    # Task MA: typed and validated, but not yet read by
    # build_catalog_response(), the deploy path, or compute_config_hash()
    # (and must stay out of compute_config_hash() even once wired up --
    # see that function's docstring for why).
    mods: list[str] = Field(default_factory=list)

    # NOTE: deliberately not named with a leading underscore. Pydantic v2
    # treats leading-underscore class attributes as PrivateAttr candidates;
    # a field_validator-decorated method is exempt via
    # __pydantic_decorators__, but there's no upside to fighting that
    # convention for a validator that's part of the model's public
    # contract (it's why load-time validation errors look the way they
    # do).
    @field_validator("mods", mode="after")
    @classmethod
    def check_mods_are_bare_names(cls, value: list[str]) -> list[str]:
        return [_validate_mod_name(item) for item in value]

    # Free-form, optional. Human-readable context that doesn't fit any
    # structured field -- known quirks, why a flag is set the way it is,
    # links to an upstream issue, etc. Surfaced under the model
    # characteristics strip in the dashboard. Not read by anything in the
    # deploy path; purely informational.
    notes: Optional[str] = None
    topologies: dict[str, TopologyConfig]


def compute_config_hash(recipe: RecipeConfig, topo_key: str) -> str:
    """
    Stable content hash identifying "this exact launch configuration" for
    (recipe, topo_key) -- i.e. the fields that actually reach `docker run` /
    `vllm serve`, not the filename. A recipe can be edited into a materially
    different config without a rename (a changed vllm_args stanza, a bumped
    gpu_util, a swapped image), and the previous filename-keyed notion of
    "this model has launched successfully" can't tell that apart from "this
    exact configuration has launched successfully" -- it would keep
    reporting stale success for a config that was never actually run. This
    hash is the join key that fixes that: see _record_launch_success() /
    enrich_catalog() in dgx-orchestrator.py for where it's compared against
    launch history.

    Deliberately narrow about what's included:
      - hf_path, image, gpu_util, and topo_key's max_model_len / tp_size /
        pp_size / env_vars / vllm_args -- everything that changes what
        actually gets launched.
      - env_vars is sorted before hashing: reordering entries in the list
        changes nothing about the resulting `docker run -e ...` flags, so it
        shouldn't invalidate "tested" status. vllm_args is hashed as the raw
        string (not shlex-split/reordered) -- simpler, and fine unless flags
        get reordered without changing them in practice.
      - Deliberately EXCLUDES capability/mods (inert metadata, Phase 4) and
        notes (documentation, never reaches the container).
      - Deliberately EXCLUDES the HF_HUB_OFFLINE/TRANSFORMERS_OFFLINE
        env_vars injection build_catalog_response() performs based on
        cluster_config.yaml's global offline flags -- that's a runtime,
        cluster-wide toggle applied on top of the recipe, not part of the
        recipe itself, and flipping online/offline mode must not silently
        invalidate every recipe's tested status. This function must only
        ever be called against the raw, as-loaded RecipeConfig/TopologyConfig
        (i.e. via load_recipes()), never against the enriched catalog dict.
    """
    topo = recipe.topologies[topo_key]
    payload = {
        "hf_path": recipe.hf_path,
        "image": recipe.image,
        "gpu_util": recipe.gpu_util,
        "max_model_len": topo.max_model_len,
        "tp_size": topo.tp_size,
        "pp_size": topo.pp_size,
        "env_vars": sorted(topo.env_vars),
        "vllm_args": topo.vllm_args,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _load_single_recipe(path: Path) -> RecipeConfig:
    try:
        raw_text = path.read_text()
    except OSError as exc:
        raise OSError(f"Could not read recipe file {path}: {exc}") from exc

    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"Recipe file {path} is not valid YAML: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(
            f"Recipe file {path} must contain a YAML mapping at the top "
            f"level, got {type(data).__name__}"
        )

    try:
        recipe = RecipeConfig(**data)
    except ValidationError as exc:
        raise ValueError(f"Recipe file {path} failed validation: {exc}") from exc

    if recipe.recipe_version not in SUPPORTED_RECIPE_VERSIONS:
        print(
            f"Warning: {path} has unrecognized recipe_version "
            f"'{recipe.recipe_version}' (supported: {SUPPORTED_RECIPE_VERSIONS}); "
            "loading it anyway.",
            file=sys.stderr,
        )

    return recipe


def _load_recipes_impl() -> dict[str, RecipeConfig]:
    found: dict[str, tuple[RecipeConfig, Path]] = {}

    for subdir in RECIPE_SUBDIRS:
        directory = RECIPES_DIR / subdir
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.yaml")):
            recipe = _load_single_recipe(path)
            stem = path.stem
            if stem in found:
                _other_recipe, other_path = found[stem]
                raise ValueError(
                    f"Recipe name collision for '{stem}': "
                    f"{other_path} and {path}"
                )
            found[stem] = (recipe, path)

    return {stem: recipe for stem, (recipe, _path) in found.items()}


def _recipe_dir_fingerprint() -> tuple:
    """
    Cheap signal for "has anything under recipes/{local,eugr}/ changed since
    we last loaded it". (path, mtime_ns) per *.yaml file across both
    subdirs, sorted for a stable, hashable/comparable tuple. Covers edits
    (mtime changes), adds and removes (the file list itself changes), and
    renames (same, since it's a different set of paths). Deliberately does
    NOT stat file contents/hash them -- mtime is enough to detect "worth
    re-reading" without adding a full-file read on every request just to
    decide whether to do a full-file read.
    """
    stamps = []
    for subdir in RECIPE_SUBDIRS:
        directory = RECIPES_DIR / subdir
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.yaml")):
            try:
                stamps.append((str(path), path.stat().st_mtime_ns))
            except OSError:
                # Deleted between glob() and stat() -- treat as absent
                # rather than erroring; the next real load will just not
                # see it, same as if it had never matched the glob.
                continue
    return tuple(stamps)


@functools.lru_cache(maxsize=1)
def _load_recipes_cached(_fingerprint: tuple) -> dict[str, RecipeConfig]:
    # _fingerprint is unused inside the function -- it exists purely as the
    # lru_cache key. Any change to it (an edit, add, remove, or rename
    # under recipes/{local,eugr}/) is a different key, so lru_cache treats
    # it as a fresh call instead of returning the stale cached result.
    # maxsize=1 means each new fingerprint evicts the previous entry, so
    # this never grows unbounded across repeated edits.
    return _load_recipes_impl()


def load_recipes(bypass_cache: bool = False) -> dict[str, RecipeConfig]:
    """
    Load and validate every recipe under recipes/local/ and recipes/eugr/.

    Returns a dict keyed by filename stem (e.g. "recipes/local/foo.yaml" ->
    key "foo"). A name collision between local/ and eugr/ (same stem in
    both) still raises -- see _load_recipes_impl().

    Cached across calls, but the cache auto-invalidates whenever any
    recipe file under recipes/{local,eugr}/ is added, removed, renamed, or
    edited (see _recipe_dir_fingerprint()) -- so editing a recipe on disk
    is picked up on the next call with no process restart required. The
    common case (nothing changed since the last call) costs one glob +
    stat() per recipe file, not a re-read/re-parse/re-validate of any of
    them.

    Pass bypass_cache=True to force a fresh read regardless of the
    fingerprint -- tests that write fresh recipe fixtures without changing
    mtimes (e.g. two writes within the same mtime-resolution tick) still
    need this.
    """
    if bypass_cache:
        _load_recipes_cached.cache_clear()
        return _load_recipes_impl()
    return _load_recipes_cached(_recipe_dir_fingerprint())


def build_catalog_response() -> dict:
    """
    Reassemble recipes + cluster_config.yaml's offline flags into the exact
    dict shape dgx-orchestrator.py's load_model_catalog() has always
    returned. index.html reads data.catalog.models[m].topologies['1_node']
    / ['2_node'] directly -- this shape is an API contract, not an
    implementation detail, and must not change.

    On any load error, returns {"error": <str>, "catalog": {"models": {}}}
    rather than raising, matching the existing loader's failure mode. Note
    this means one malformed recipe file still fails the WHOLE catalog, not
    just that recipe -- removing the name/filename mismatch class of error
    (see module docstring) shrinks how often that can happen, but doesn't
    change this failure mode. Containing that blast radius (skip-and-warn
    per bad recipe instead of failing everything) is a separate, deliberately
    unmade change.
    """
    try:
        cluster_cfg = load_cluster_config()
        recipes = load_recipes()

        global_hf = int(cluster_cfg.global_hf_hub_offline)
        global_tf = int(cluster_cfg.global_transformers_offline)

        models: dict = {}
        for name, recipe in recipes.items():
            model_entry: dict = {"hf_path": recipe.hf_path, "gpu_util": recipe.gpu_util}
            if recipe.image is not None:
                model_entry["image"] = recipe.image
            if recipe.notes is not None:
                model_entry["notes"] = recipe.notes

            topologies: dict = {}
            for topo_name, topo in recipe.topologies.items():
                # model_dump() returns fresh dict/list objects -- safe to
                # mutate below without touching the cached RecipeConfig.
                topo_dict = topo.model_dump(include=set(_TOPOLOGY_OUTPUT_FIELDS))
                env_vars = list(topo_dict.get("env_vars") or [])

                # config_hash is computed from the RAW (pre-injection) topo
                # -- i.e. via compute_config_hash(recipe, topo_name), not
                # from topo_dict after the HF/transformers offline env_vars
                # get appended below. Flipping the cluster's online/offline
                # toggle must not change what "this configuration" means for
                # launch-history matching purposes -- see compute_config_hash().
                topo_dict["config_hash"] = compute_config_hash(recipe, topo_name)

                if global_hf == 1:
                    env_vars = [e for e in env_vars if not e.startswith("HF_HUB_OFFLINE=")]
                    env_vars.append("HF_HUB_OFFLINE=1")

                if global_tf == 1:
                    env_vars = [e for e in env_vars if not e.startswith("TRANSFORMERS_OFFLINE=")]
                    env_vars.append("TRANSFORMERS_OFFLINE=1")

                topo_dict["env_vars"] = env_vars
                topologies[topo_name] = topo_dict

            model_entry["topologies"] = topologies
            models[name] = model_entry

        return {
            "catalog": {
                "GLOBAL_HF_HUB_OFFLINE": global_hf,
                "GLOBAL_TRANSFORMERS_OFFLINE": global_tf,
                "default_image": cluster_cfg.default_image,
                "models": models,
            }
        }
    except Exception as exc:
        return {"error": str(exc), "catalog": {"models": {}}}

```

### `tests/smoke_test_mods.py`

```python
#!/usr/bin/env python3
"""
tests/smoke_test_mods.py -- automated live-hardware smoke test for
common/mods.py (Task MB).

Runs the same sequence TESTING-MB.md walks through by hand, as one script:
creates throwaway test mods, bakes them on a real host, independently
verifies the result via plain `docker inspect` (never through the module
under test), confirms idempotent skip / payload-edit rebake / failing-mod
abort / per-host independence, then cleans up.

Run from inside the dgx-orchestrator-api container (matches how dgx-config
already delegates every CLI invocation -- see dgx-config's docker exec
wrapper). Since docker-compose.yml bind-mounts `.:/app`, anything dropped
into tests/ on the host is already visible inside the container with no
rebuild:

    docker exec -it dgx-orchestrator-api python3 tests/smoke_test_mods.py
    docker exec -it dgx-orchestrator-api python3 tests/smoke_test_mods.py --hosts spark-4
    docker exec -it dgx-orchestrator-api python3 tests/smoke_test_mods.py --keep
    docker exec -it dgx-orchestrator-api python3 tests/smoke_test_mods.py --serve-check --hf-path Qwen/Qwen3-0.6B

Exits 0 if every check passed, 1 otherwise. Does not touch
dgx-orchestrator.py or the real deploy path -- this only exercises
common/mods.py's public functions directly (Task MC hasn't happened yet).

Nothing this script creates is meant to be committed: mods/_test_* are
throwaway fixtures for validating MB's mechanics, not the real mods/_noop/
Task MD is responsible for.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Bootstrap: this script lives one level below the repo root (tests/), same
# as common/*.py lives one level below it (common/). Mirrors common/ssh.py's
# own BASE_DIR resolution exactly -- same env var, same file-relative
# fallback -- so this agrees with what common.ssh.BASE_DIR resolves to
# rather than computing a second, potentially-divergent notion of "repo
# root". Without this, `python3 tests/smoke_test_mods.py` fails to import
# `common` at all: Python puts the SCRIPT's own directory (tests/) on
# sys.path, not the repo root, regardless of cwd or host-vs-container.
_REPO_ROOT = Path(os.getenv("BASE_DIR", Path(__file__).resolve().parent.parent))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import yaml

from common.ssh import BASE_DIR, run_ssh
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
    """
    Thin adapter over common.ssh.run_ssh() for this script's independent
    verification calls. This used to be its own hand-rolled SSH
    invocation, built specifically so verification wouldn't trust the same
    code path being tested -- but that "independence" instinct was aimed
    at the wrong layer. It should mean "don't call ensure_mods_baked() to
    verify its own output", not "don't reuse a correct, already-audited
    SSH transport". The hand-rolled version silently dropped run_ssh()'s
    per-argument shlex.quote before joining into the remote command
    string; ssh itself then re-concatenates multiple argv elements into
    ONE string for the remote shell, so any argument containing a space
    silently split into extra, wrong tokens on the far end -- see
    TOMBSTONES.md #83 for the concrete failure this caused (a false-passing
    Entrypoint/Cmd check).
    """
    return run_ssh(ip, user, list(cmd), timeout=timeout)


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
    # The derived image's real entrypoint (restored, not the throwaway
    # "sleep" override -- see the Entrypoint/Cmd checks below) still fires
    # here since --gpus wasn't passed: the NGC wrapper's driver warning
    # legitimately precedes the actual `cat` output. That's the entrypoint
    # restoration working correctly, not a mods-ran-out-of-order bug -- so
    # check that the marker lines appear in order ANYWHERE in stdout,
    # rather than requiring them to be the first two lines.
    marker_lines = [ln for ln in r.stdout.splitlines() if "marker_a" in ln or "marker_b" in ln]
    order_ok = (
        r.returncode == 0
        and len(marker_lines) == 2
        and "marker_a" in marker_lines[0]
        and "marker_b" in marker_lines[1]
    )
    passed &= record(f"[{host}] mods applied in declared order", order_ok, r.stdout.strip() or r.stderr.strip())

    ep_a = ssh_run(ip, user, "docker", "inspect", "--format", "{{json .Config.Entrypoint}}", base_image)
    ep_b = ssh_run(ip, user, "docker", "inspect", "--format", "{{json .Config.Entrypoint}}", tag)
    passed &= record(f"[{host}] Entrypoint inspect calls succeeded", ep_a.returncode == 0 and ep_b.returncode == 0, (ep_a.stderr or ep_b.stderr).strip())
    base_ep, derived_ep = ep_a.stdout.strip(), ep_b.stdout.strip()
    passed &= record(f"[{host}] Entrypoint restored to match base", base_ep == derived_ep, f"{base_ep!r} vs {derived_ep!r}")

    cmd_a = ssh_run(ip, user, "docker", "inspect", "--format", "{{json .Config.Cmd}}", base_image)
    cmd_b = ssh_run(ip, user, "docker", "inspect", "--format", "{{json .Config.Cmd}}", tag)
    passed &= record(f"[{host}] Cmd inspect calls succeeded", cmd_a.returncode == 0 and cmd_b.returncode == 0, (cmd_a.stderr or cmd_b.stderr).strip())
    base_cmd, derived_cmd = cmd_a.stdout.strip(), cmd_b.stdout.strip()
    passed &= record(f"[{host}] Cmd restored to match base", base_cmd == derived_cmd, f"{base_cmd!r} vs {derived_cmd!r}")

    wd_a = ssh_run(ip, user, "docker", "inspect", "--format", "{{.Config.WorkingDir}}", base_image)
    wd_b = ssh_run(ip, user, "docker", "inspect", "--format", "{{.Config.WorkingDir}}", tag)
    passed &= record(f"[{host}] WorkingDir inspect calls succeeded", wd_a.returncode == 0 and wd_b.returncode == 0, (wd_a.stderr or wd_b.stderr).strip())
    base_wd, derived_wd = wd_a.stdout.strip(), wd_b.stdout.strip()
    passed &= record(f"[{host}] WorkingDir preserved", base_wd == derived_wd and bool(derived_wd), derived_wd)

    r = ssh_run(ip, user, "docker", "ps", "-a", "--filter", "name=dgx-mods-bake", "--format", "{{.Names}}")
    passed &= record(f"[{host}] no dangling bake containers", r.returncode == 0 and r.stdout.strip() == "", r.stdout.strip() or r.stderr.strip())

    r = ssh_run(ip, user, "bash", "-c", "ls /tmp | grep dgx-mods-bake || true")
    passed &= record(f"[{host}] no leftover staging dirs", r.returncode == 0 and r.stdout.strip() == "", r.stdout.strip() or r.stderr.strip())

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
    passed &= record(f"[{host}] no dangling container after failure", r.returncode == 0 and r.stdout.strip() == "", r.stdout.strip() or r.stderr.strip())

    r = ssh_run(ip, user, "bash", "-c", "ls /tmp | grep dgx-mods-bake || true")
    passed &= record(f"[{host}] no leftover staging dir after failure", r.returncode == 0 and r.stdout.strip() == "", r.stdout.strip() or r.stderr.strip())
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

```
