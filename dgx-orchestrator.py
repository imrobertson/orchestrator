#!/usr/bin/env python3
"""
TETREL SECURITY - DGX CLUSTER ORCHESTRATOR
--------------------------------------------------------------------------------
Architecture Target: Dual DGX Spark (Grace Blackwell GB10, LPDDR5x Unified Memory).
vLLM Runtime Target: nvcr.io/nvidia/vllm:26.07-py3 / eugr/spark-vllm-b12x:latest.

This orchestrator manages the lifecycle, network state, and tuning deployments 
of multi-node LLM serving over a 100GbE backplane via NCCL.
"""

import argparse
import datetime

# Human-maintained descriptive slug -- still bump this on every meaningful
# change, since "what was this deploy about" at a glance is genuinely
# useful and a hash alone doesn't convey it. The source-hash suffix
# appended to ORCHESTRATOR_VERSION below (see _compute_source_hash_suffix())
# is what actually answers "did my push/pull/restart take" now -- it's
# derived, not typed, so it can't be forgotten the way this slug already
# has been.
ORCHESTRATOR_VERSION_SLUG = "2026-08-28-primary-secondary-host-refactor"

from concurrent.futures import Future, ThreadPoolExecutor, as_completed
import getpass
import glob
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import signal
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Literal, Optional
import yaml

from common.config import legacy_hosts_dict, load_cluster_config
from common.constants import ContainerRole
from common.mods import ModBakeError, ModResolutionError, ensure_mods_baked, resolve_mod_tag
from common.runlog import archive_run_log
from common.recipes import (
    build_catalog_response,
    build_config_registry_entries,
    compute_config_hash,
    load_recipes,
)
from common.ssh import get_hf_token, resolve_user_identity_key, run_ssh

try:
    from fastapi import FastAPI, BackgroundTasks, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    import uvicorn
    from pydantic import BaseModel
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False

# --- Core Configurations ---
BASE_DIR = Path(os.getenv("BASE_DIR", Path(__file__).resolve().parent))

def _compute_source_hash_suffix() -> str:
    """
    Short hash of THIS FILE's own on-disk bytes, computed once at import
    time. Replaces an earlier git-commit-hash approach that assumed a
    live git checkout (with .git history) was reachable at runtime --
    not a safe assumption for a Dockerized daemon: a COPY-based image
    build routinely excludes .git (image size, avoiding leaked history),
    and a slim base image may not even have the git binary installed.
    Confirmed exactly this problem in production: `git rev-parse` had
    nothing to read inside the running container, silently and
    permanently falling back to "unknown" on every startup. It also
    would have made this code annoying to hand to anyone without access
    to this specific git history -- a real constraint the moment this
    needs to be shared outside this one repo.

    Hashing the file's own bytes needs nothing external: no git binary,
    no .git directory, no build-time plumbing to thread a commit hash
    into the container. It's arguably a MORE accurate answer to "is this
    exact code running" than a commit hash ever was, too -- it reflects
    literally what's on disk in this process right now, not what was
    last committed (which drifts the moment anyone hand-edits a file
    without committing, something this project's own interactive-
    against-production workflow makes a real possibility, not a
    hypothetical).

    Falls back to "unknown" if the file can't be read for any reason --
    never raises, since a version string failing to compute must never
    stop the daemon from starting. Prints the actual exception when this
    happens (unlike the silent "unknown" fallback this shipped with
    originally) -- that first version failed exactly this way in
    production with zero diagnostic trail, which is the same silent-
    except mistake this whole logging pass elsewhere in the file exists
    to catch. Use Path(__file__).resolve() rather than the bare __file__
    string: __file__ isn't guaranteed to already be an absolute path --
    it reflects how the interpreter was invoked, so a relative path
    resolved against a working directory that doesn't match where the
    file actually lives (a real risk in a containerized daemon's launch
    command) fails a bare read_bytes() silently.
    """
    try:
        return hashlib.sha256(Path(__file__).resolve().read_bytes()).hexdigest()[:8]
    except Exception as exc:
        print(f"[!] _compute_source_hash_suffix: failed to hash {__file__} - {exc}")
        return "unknown"

# Surfaced in /api/status, the CLI's `status` command, daemon startup logs,
# and the dashboard header. Confirm this after every deploy -- see
# _compute_source_hash_suffix()'s docstring for why the hash half of this
# is the load-bearing part.
ORCHESTRATOR_VERSION = f"{ORCHESTRATOR_VERSION_SLUG}+{_compute_source_hash_suffix()}"
MODELS_YAML_PATH = BASE_DIR / "models.yaml"
LEDGER_PATH = BASE_DIR / "model_ledger.json"
CONFIG_REGISTRY_PATH = BASE_DIR / "config_registry.json"
BENCHMARK_LEDGER_PATH = BASE_DIR / "benchmark_ledger.csv"
BENCHMARK_RESULTS_PATH = BASE_DIR / "benchmark_results.txt"
# Records catalog_key -> hf_path (+ derived cache dirname) every time a
# model is actually deployed. Exists so flush_model_cache() and
# find_cached_models() can still identify a model's cache directory by its
# OLD catalog key after the recipe file itself has been deleted -- without
# this, the recipe was the only record of that mapping, and deleting the
# recipe deleted the record along with it. Only starts covering a model
# from the first deploy after this was introduced; it can't retroactively
# know about something retired before that.
HF_PATH_LEDGER_PATH = BASE_DIR / "hf_path_ledger.json"
# Per-host record of exactly which recipe (catalog_key + topo_key +
# config_hash) is currently deployed, written at launch time and cleared at
# teardown time -- see ACTIVE_DEPLOYMENT_STATE below for why this exists and
# why it's disk-backed rather than memory-only.
ACTIVE_DEPLOYMENT_STATE_PATH = BASE_DIR / "active_deployment_state.json"

# --- Shared JSON state-file I/O ---
# model_ledger.json, hf_path_ledger.json, and active_deployment_state.json
# are three separate files on purpose -- they're indexed on different axes
# (catalog_key::topo_key, catalog_key, and host respectively) and have
# different lifecycles (the first two are cumulative/permanent history,
# the third is ephemeral and explicitly cleared at teardown). Combining
# them would mean every teardown does a read-modify-write of the entire
# permanent history just to drop one host's current-state entry, and would
# require one shared lock across write paths that today can never contend
# with each other. What they DID share was copy-pasted read/write
# boilerplate -- these two helpers are just that, factored out, not a step
# toward merging the data itself.
def _read_json_state(path: Path):
    """
    Best-effort JSON-object read for this module's small on-disk state/
    ledger files. Returns None if the file doesn't exist, doesn't parse,
    or doesn't contain a JSON object -- callers pick their own fallback
    (usually `_read_json_state(path) or {}`), since "never written yet"
    needs different handling in a couple of read-only callers (e.g.
    get_estimated_load_time() has a real default estimate to fall back to;
    _load_last_seen_raw() needs to distinguish "nothing recorded" from
    "recorded as empty").
    """
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None
    return data if isinstance(data, dict) else None

def _write_json_state(path: Path, data: dict) -> bool:
    """
    Best-effort JSON write for the same family of files. Failures are
    swallowed -- matching every existing writer this replaces
    (record_load_time(), _record_launch_success(), _record_hf_path(),
    SessionTracker._commit_session(), etc): a lost stats/state write must
    never break the deploy, teardown, or status-poll path that triggered
    it. Returns whether the write succeeded, for the rare caller that
    wants to know. NOT used by correct_ledger_parser's ledger-repair path
    (see ledger_set_lifetime() / the CLI command around line ~1930+) --
    that tool deliberately surfaces read/parse errors as explicit
    status="error" results and writes a timestamped backup before an
    unguarded write, since a human operator is meant to see exactly what
    went wrong there, not have it silently swallowed.
    """
    try:
        path.write_text(json.dumps(data, indent=2))
        return True
    except Exception:
        return False

HOSTS = legacy_hosts_dict()

# Every "default host" and "the 2-node pair" in this file used to be the
# literal strings "spark-4"/"spark-3", hardcoded independently in ~10
# different places. That's silently wrong the moment this same code is
# pointed at a different cluster_config.yaml (e.g. a second orchestrator
# instance scoped to a different node pair, like spark-5/spark-6) -- a
# 2-node deploy would target hosts that don't even exist in that config's
# HOSTS, and get_cluster_status()'s serving_host fallback would KeyError
# constantly whenever nothing happened to be deployed. Deriving these from
# HOSTS itself (in cluster_config.yaml's own listed order) means the exact
# same code is correct for any 2-host cluster_config.yaml with zero
# further changes -- and is a no-op for the existing spark-4/spark-3 setup,
# since spark-4 is still listed first there.
PRIMARY_HOST = next(iter(HOSTS), "spark-4")
SECONDARY_HOST = list(HOSTS.keys())[1] if len(HOSTS) > 1 else PRIMARY_HOST
PRIMARY_HOST_IP = HOSTS.get(PRIMARY_HOST, {}).get("ip", "10.0.14.43")

NETWORK_STATE_FILE = BASE_DIR / ".network_mode"
ANSI_ESCAPE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-9?]*[ -/]*[@-~])')

# --- JIT Cache Roots ---
# Kernel cache roots eligible for LRU eviction via prune_cluster_cache().
# Deliberately excludes ~/.nv/ComputeCache (driver-managed via
# CUDA_CACHE_MAXSIZE -- hand-pruning it fights the driver's own eviction)
# and ~/.cache/huggingface (model weights, a completely different lifecycle
# from JIT kernel artifacts). cache_inventory() below reports on both of
# those anyway, for visibility, but never evicts from them.
JIT_CACHE_ROOTS = [
    "~/.cache/triton",
    "~/.cache/tilelang",
    "~/.cache/deepgemm",
    "~/.cache/vllm",
    "~/.cache/flashinfer",
]

# Every cache root worth *looking at*, even ones prune_cluster_cache() will
# never touch. Used by cache_inventory() only.
INVENTORY_ROOTS = [
    ("triton", "~/.cache/triton"),
    ("tilelang", "~/.cache/tilelang"),
    ("deepgemm", "~/.cache/deepgemm"),
    ("vllm", "~/.cache/vllm"),
    ("flashinfer", "~/.cache/flashinfer"),
    ("compute_cache", "~/.nv/ComputeCache"),
    ("huggingface", "~/.cache/huggingface"),
    # "huggingface" above lists one level deep under ~/.cache/huggingface
    # itself (hub/, modules/, xet/ as three lumped entries) -- useless for
    # per-model attribution since every model's weights live inside that
    # single "hub" entry together. This root targets hub/ directly, so
    # each entry returned IS one model's models--org--repo directory,
    # individually sized -- what find_cached_models() needs.
    ("huggingface_models", "~/.cache/huggingface/hub"),
]

# Runs on each Spark over SSH. argv: <roots_json> <target_free_bytes> <dry_run:0|1>
#
# Evicts whole entry DIRECTORIES, never individual files. A Triton/TileLang
# cache entry is a directory of co-dependent artifacts (metadata json + ptx +
# cubin/so); deleting files piecemeal with `find -type f` leaves a
# half-entry that the loader treats as a hit and then fails to load the
# binary -- that's corruption, not a cache miss, and it doesn't announce
# itself: it just recompiles one kernel, once, unpredictably.
#
# Recency is max(atime, mtime) across the entry's files. Under noatime,
# atime is frozen at whatever it was at creation/extraction time, so mtime
# is the only usable signal -- this degrades gracefully to "least recently
# written" rather than silently treating every hot kernel as equally stale.
#
# Only evicts if free space is currently below target_free_bytes, and then
# strictly oldest-entry-first until the target is met. Above the floor,
# nothing is touched -- this is a safety-valve, not a housekeeping sweep.
_REMOTE_PRUNE_SCRIPT = r'''
import json, os, shutil, sys, time

roots = [os.path.expanduser(p) for p in json.loads(sys.argv[1])]
target_free = int(sys.argv[2])
dry_run = sys.argv[3] == "1"

probe = next((r for r in roots if os.path.isdir(r)), os.path.expanduser("~"))

def free_bytes():
    st = os.statvfs(probe)
    return st.f_bavail * st.f_frsize

# Report mount options so the caller can see if atime is even meaningful.
mount_opts = "unknown"
try:
    best = ""
    with open("/proc/mounts") as fh:
        for line in fh:
            parts = line.split()
            if len(parts) >= 4 and probe.startswith(parts[1]) and len(parts[1]) > len(best):
                best, mount_opts = parts[1], parts[3]
except Exception:
    pass

entries = []
for root in roots:
    if not os.path.isdir(root):
        continue
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        size = 0
        last_used = 0.0
        try:
            for dirpath, _dirnames, filenames in os.walk(path):
                for fn in filenames:
                    fp = os.path.join(dirpath, fn)
                    try:
                        st = os.lstat(fp)
                    except OSError:
                        continue
                    size += st.st_size
                    last_used = max(last_used, st.st_atime, st.st_mtime)
        except OSError:
            continue
        entries.append((last_used, size, path))

free_before = free_bytes()
result = {
    "mount_options": mount_opts,
    "free_before": free_before,
    "target_free": target_free,
    "entries_total": len(entries),
    "evicted": [],
    "bytes_freed": 0,
    "dry_run": dry_run,
    "errors": [],
}

if free_before < target_free:
    entries.sort(key=lambda e: e[0])  # least recently used first
    reclaimed = 0
    for last_used, size, path in entries:
        if free_before + reclaimed >= target_free:
            break
        age_days = round((time.time() - last_used) / 86400, 1)
        if not dry_run:
            try:
                shutil.rmtree(path)
            except OSError as exc:
                result["errors"].append("%s: %s" % (path, exc))
                continue
        reclaimed += size
        result["evicted"].append({
            "path": path, "bytes": size, "last_used": last_used,
            "age_days": age_days,
            "reason": "below free-space floor, LRU order",
        })
result["bytes_freed"] = reclaimed if free_before < target_free else 0
result["shortfall_bytes"] = max(0, target_free - free_before)
result["free_after"] = free_bytes()
print(json.dumps(result))
'''

# Age-based (not free-space-based) cleanup of ~/.cache/ray-logs/<run_id>/.
# Each top-level entry under the root is one deploy's run_id directory,
# created by _execute_deployment_impl()'s mkdir step and bind-mounted to
# /tmp/ray inside the container (see _jit_cache_mounts_and_env) so Ray's
# session dir -- including any crashed worker's stdout/stderr -- survives
# teardown. These are tiny relative to JIT/HF caches, so unlike
# _REMOTE_PRUNE_SCRIPT above, eviction here is unconditional on age alone,
# not gated behind a free-space floor. argv: <root> <retention_seconds> <dry_run:0|1>
_REMOTE_RAY_LOG_PRUNE_SCRIPT = r'''
import json, os, shutil, sys, time

root = os.path.expanduser(sys.argv[1])
cutoff = time.time() - float(sys.argv[2])
dry_run = sys.argv[3] == "1"

def dir_size_and_latest_mtime(path):
    total = 0
    latest = 0.0
    for dirpath, _dirnames, filenames in os.walk(path):
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            try:
                st = os.lstat(fp)
            except OSError:
                continue
            total += st.st_size
            latest = max(latest, st.st_mtime)
    return total, latest

result = {"root": root, "evicted": [], "bytes_freed": 0, "kept": 0, "dry_run": dry_run, "errors": []}

if os.path.isdir(root):
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        size, latest = dir_size_and_latest_mtime(path)
        # No files yet (e.g. mkdir just ran, deploy about to start) --
        # nothing to age off of, fall back to the dir's own mtime so a
        # just-created empty run dir is never mistaken for stale.
        age_ref = latest if latest > 0 else os.path.getmtime(path)
        if age_ref < cutoff:
            age_days = round((time.time() - age_ref) / 86400, 1)
            if not dry_run:
                try:
                    shutil.rmtree(path)
                except OSError as exc:
                    result["errors"].append("%s: %s" % (path, exc))
                    continue
            result["evicted"].append({"path": path, "bytes": size, "age_days": age_days})
            result["bytes_freed"] += size
        else:
            result["kept"] += 1

print(json.dumps(result))
'''

# Read-only inventory of everything under the cache roots. No deletion, no
# threshold, no free-space check -- safe to run at any time, including
# against a live production cluster. argv: <roots_json as [[label, path], ...]>
_REMOTE_INVENTORY_SCRIPT = r'''
import json, os, sys, time

roots = json.loads(sys.argv[1])
now = time.time()

report = {"generated_at": now, "roots": {}}

for label, root in roots:
    root = os.path.expanduser(root)
    entry = {"path": root, "exists": os.path.isdir(root), "entry_count": 0,
             "total_bytes": 0, "entries": []}
    if entry["exists"]:
        for name in sorted(os.listdir(root)):
            path = os.path.join(root, name)
            if not os.path.isdir(path):
                continue
            size = 0
            last_used = 0.0
            file_count = 0
            try:
                for dirpath, _dirnames, filenames in os.walk(path):
                    for fn in filenames:
                        fp = os.path.join(dirpath, fn)
                        try:
                            st = os.lstat(fp)
                        except OSError:
                            continue
                        size += st.st_size
                        last_used = max(last_used, st.st_atime, st.st_mtime)
                        file_count += 1
            except OSError:
                continue
            entry["entries"].append({
                "name": name, "bytes": size, "file_count": file_count,
                "last_used": last_used, "age_days": round((now - last_used) / 86400, 1),
            })
            entry["total_bytes"] += size
        entry["entry_count"] = len(entry["entries"])
        entry["entries"].sort(key=lambda e: e["last_used"])  # oldest first, LRU order
    report["roots"][label] = entry

probe = next((os.path.expanduser(p) for _l, p in roots if os.path.isdir(os.path.expanduser(p))),
             os.path.expanduser("~"))
st = os.statvfs(probe)
report["disk_free_bytes"] = st.f_bavail * st.f_frsize
report["disk_total_bytes"] = st.f_blocks * st.f_frsize

mount_opts = "unknown"
try:
    best = ""
    with open("/proc/mounts") as fh:
        for line in fh:
            parts = line.split()
            if len(parts) >= 4 and probe.startswith(parts[1]) and len(parts[1]) > len(best):
                best, mount_opts = parts[1], parts[3]
except Exception:
    pass
report["mount_options"] = mount_opts

print(json.dumps(report))
'''

# Read-only IPC/shared-memory inventory. No deletion. argv: none.
#
# /proc/sysvipc/shm and /proc/sysvipc/sem are stable kernel ABIs (see man 5
# proc) -- parsed via the header line itself (zip(header, parts)) rather
# than assuming fixed column positions, so this stays correct across kernel
# versions that add/remove columns.
#
# nattch is the kernel's own live attach count for a SysV shared memory
# segment -- 0 means provably nothing has it attached right now. This is a
# hard guarantee, not a heuristic, which is what makes the sweep script
# below safe to run unconditionally.
_REMOTE_IPC_INVENTORY_SCRIPT = r'''
import json, os, time

report = {"generated_at": time.time(), "shm_segments": [], "semaphores": [],
          "dev_shm_files": [], "dev_shm_disk": {}}

def parse_sysvipc_table(path):
    rows = []
    try:
        with open(path) as f:
            lines = f.read().splitlines()
        if not lines:
            return rows
        header = lines[0].split()
        for line in lines[1:]:
            parts = line.split()
            if len(parts) < len(header):
                continue
            rows.append(dict(zip(header, parts)))
    except Exception:
        pass
    return rows

for row in parse_sysvipc_table("/proc/sysvipc/shm"):
    try:
        report["shm_segments"].append({
            "shmid": row.get("shmid"),
            "key": row.get("key"),
            "size_bytes": int(row.get("size", 0)),
            "nattch": int(row.get("nattch", "1")),
            "cpid": row.get("cpid"),
            "lpid": row.get("lpid"),
        })
    except (ValueError, TypeError):
        continue

for row in parse_sysvipc_table("/proc/sysvipc/sem"):
    report["semaphores"].append({
        "semid": row.get("semid"),
        "key": row.get("key"),
        "nsems": row.get("nsems"),
    })

shm_dir = "/dev/shm"
try:
    for name in os.listdir(shm_dir):
        path = os.path.join(shm_dir, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        report["dev_shm_files"].append({
            "name": name,
            "bytes": st.st_size,
            "age_days": round((time.time() - st.st_mtime) / 86400, 2),
        })
except Exception as exc:
    report["dev_shm_error"] = str(exc)

try:
    st = os.statvfs(shm_dir)
    report["dev_shm_disk"] = {
        "total_bytes": st.f_blocks * st.f_frsize,
        "free_bytes": st.f_bavail * st.f_frsize,
    }
except Exception as exc:
    report["dev_shm_disk_error"] = str(exc)

print(json.dumps(report))
'''

# Removes ONLY SysV shared memory segments with nattch == 0 (provably
# unattached -- see note above). Never touches POSIX /dev/shm files
# directly: verifying those are truly orphaned would require cross-
# referencing every process's open fds AND memory maps across the whole
# host, which is a real check worth building but isn't safe to rush
# without testing against the actual hosts -- deliberately deferred, see
# docs/ROADMAP.md. argv: <dry_run:0|1>
#
# Only the actual `ipcrm -m` removal call is sudo'd (inside the script,
# below) -- NOT the outer python3 invocation. Reading /proc/sysvipc/shm
# needs no privilege at all. This matters because the call site
# deliberately does NOT wrap this whole script in sudo anymore: doing so
# would need a sudoers rule matching "python3 -c *", which grants
# passwordless root execution of arbitrary Python, not just this sweep.
# Scoping sudo to just "ipcrm -m *" here means the corresponding sudoers
# entry can be scoped that narrowly too -- see sweep_ipc_orphans()'s
# call site for the exact command this expects to be permitted.
_REMOTE_IPC_SWEEP_SCRIPT = r'''
import json, subprocess, sys

dry_run = sys.argv[1] == "1"
removed = []
errors = []

try:
    with open("/proc/sysvipc/shm") as f:
        lines = f.read().splitlines()
except Exception as exc:
    print(json.dumps({"removed": [], "errors": [{"shmid": None, "error": str(exc)}], "total_bytes_freed": 0, "dry_run": dry_run}))
    sys.exit(0)

if lines:
    header = lines[0].split()
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < len(header):
            continue
        row = dict(zip(header, parts))
        try:
            nattch = int(row.get("nattch", "1"))
            size = int(row.get("size", 0))
        except (ValueError, TypeError):
            continue
        if nattch != 0:
            continue  # attached somewhere -- never touch this, regardless of what it is or how old
        shmid = row.get("shmid")
        if dry_run:
            removed.append({"shmid": shmid, "bytes": size, "status": "would remove"})
            continue
        res = subprocess.run(["sudo", "ipcrm", "-m", shmid], capture_output=True, text=True)
        if res.returncode == 0:
            removed.append({"shmid": shmid, "bytes": size, "status": "removed"})
        else:
            errors.append({"shmid": shmid, "error": res.stderr.strip()})

total_bytes_freed = sum(e["bytes"] for e in removed)
print(json.dumps({"removed": removed, "errors": errors, "total_bytes_freed": total_bytes_freed, "dry_run": dry_run}))
'''
# wipe of every JIT cache root on the host. Run on each Spark over SSH.
# argv: <hf_cache_dir> <jit_roots_json> <dry_run:0|1>
#
# The HF weights removal is precise -- HuggingFace's own on-disk naming
# convention (models--{org}--{repo}) makes it possible to compute the exact
# directory for one model and remove only that.
#
# The JIT wipe, when requested, is NOT model-scoped -- it removes every
# entry under every given root unconditionally. This is deliberate: JIT
# cache entries are keyed by compiled-kernel signature, not by model name,
# and there's no reliable way to attribute an entry to one specific model
# (see docs/ROADMAP.md's "Cache integrity retrospection" entry). Passing an
# empty jit_roots list skips this half entirely.
_REMOTE_MODEL_FLUSH_SCRIPT = r'''
import json, os, shutil, sys

hf_cache_dir = sys.argv[1]
jit_roots = json.loads(sys.argv[2])
dry_run = sys.argv[3] == "1"

def dir_size(path):
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            try:
                total += os.lstat(fp).st_size
            except OSError:
                pass
    return total

result = {"hf_cache": None, "jit_caches": [], "dry_run": dry_run}

hf_path = os.path.expanduser(hf_cache_dir)
if os.path.isdir(hf_path):
    size = dir_size(hf_path)
    if dry_run:
        status = "would delete"
    else:
        try:
            shutil.rmtree(hf_path)
            status = "deleted"
        except OSError as exc:
            status = "error: %s" % exc
    result["hf_cache"] = {"path": hf_path, "bytes": size, "status": status}
else:
    result["hf_cache"] = {"path": hf_path, "bytes": 0, "status": "not found"}

for root in jit_roots:
    root_path = os.path.expanduser(root)
    if not os.path.isdir(root_path):
        continue
    for name in os.listdir(root_path):
        entry_path = os.path.join(root_path, name)
        if not os.path.isdir(entry_path):
            continue
        size = dir_size(entry_path)
        if dry_run:
            status = "would delete"
        else:
            try:
                shutil.rmtree(entry_path)
                status = "deleted"
            except OSError as exc:
                status = "error: %s" % exc
        result["jit_caches"].append({"path": entry_path, "bytes": size, "status": status})

print(json.dumps(result))
'''

# --- Cluster Operation Lock ---
CLUSTER_OP_LOCK = threading.Lock()
CLUSTER_OP_LOCK_TIMEOUT = 5  # seconds

# --- Global Benchmark Worker State ---
BENCHMARK_STATE = {
    "running": False,
    "message": "Idle",
    "last_run": None
}
BENCHMARK_STATE_LOCK = threading.Lock()

# --- Global Teardown Progress State ---
# Teardown is otherwise a synchronous, blocking call (deploy depends on that --
# it must know containers are actually gone before launching new ones, so it
# can't be fire-and-forget the way benchmarking is). Since the grace-period
# rewrite it can now legitimately take up to ~3x TEARDOWN_GRACE_SEC, with zero
# visibility into which phase it's in. This dict is written by
# _execute_teardown_impl as it moves through phases and read by the status
# endpoint, which the dashboard is ALREADY polling on its own timer
# concurrently with the blocking teardown POST -- so the UI gets live phase
# updates without teardown itself needing to become async.
TEARDOWN_STATE = {
    "running": False,
    "phase": "idle",  # idle | signaling | stopping | removing | sweeping | done | error
    "message": "Idle",
    "last_run": None
}
TEARDOWN_STATE_LOCK = threading.Lock()

# --- Pending Launch Confirmation State ---
# "Last launched successfully" is keyed on config_hash (see
# common/recipes.py's compute_config_hash), not just model+topo, so it
# can't report stale success for a recipe that's since been edited into a
# materially different configuration. Recording it requires an actual
# post-deploy health confirmation, not just "the container didn't crash in
# the first 4 seconds" -- so it can't be recorded synchronously inside
# execute_deployment() for the common case (a plain dashboard "Deploy"
# click, which doesn't pass wait=True). Instead execute_deployment() drops
# a pending record here describing what it's waiting to see confirmed
# healthy, and _compute_cluster_status_impl() -- which already polls
# cluster_ready/matched_model/topo every 4s for the dashboard regardless --
# consumes it the moment a matching model+topo reports healthy. A deploy
# that gets torn down or overwritten before that happens just leaves the
# pending record to age out (see PENDING_LAUNCH_STALE_SEC) with nothing
# recorded, which is the correct outcome, not a bug to special-case.
PENDING_LAUNCH_STATE: dict = {"pending": None}
PENDING_LAUNCH_LOCK = threading.Lock()

# --- Active Deployment State (hash-based, disk-backed) ---
# The dashboard needs to know, per host, exactly which recipe is running --
# not "a model whose loaded checkpoint name happens to resemble this catalog
# entry". Two recipes can serve the identical checkpoint under different
# configs (e.g. deepseek-v4-flash-0731-1M vs. deepseek-v4-flash-0731-dspark-
# sm120 both report the same served model name), so anything that tries to
# reverse-guess the recipe from the served name alone is ambiguous by
# construction and will silently pick whichever catalog entry it happens to
# iterate to first. This record sidesteps that: execute_deployment() already
# knows exactly which recipe it launched and computes config_hash for it
# (see PENDING_LAUNCH_STATE above), so it writes that same identity here as
# the authoritative "what's actually running" record, and
# _finalize_host_status() reads it back directly instead of re-deriving it.
#
# Disk-backed (unlike PENDING_LAUNCH_STATE, which is fine to lose) because
# this needs to survive an orchestrator daemon restart while a model is
# still running on a host -- a memory-only version would silently fall back
# to the old ambiguous guessing after every restart, which is exactly the
# failure mode this exists to eliminate. Cleared per-host on teardown
# (including the pre-deploy teardown inside execute_deployment(), so a new
# deploy always starts from a clean record, never a stale one from whatever
# was previously running on that host).
#
# Deliberately has NO in-memory cache/global -- every read below goes
# straight to disk via _load_active_deployment_state(), same as
# model_ledger.json and hf_path_ledger.json already do via
# _read_json_state(). An earlier version of this DID cache the dict in a
# module-level global, mutated in place by _set_active_deployment()/
# _clear_active_deployment() -- which broke the moment a deploy or teardown
# ran through a *different process* than the long-running daemon (e.g. the
# `dgx-config`/CLI path, which is a genuine one-off process, not a request
# handled by the daemon). The CLI process wrote the correct record to disk
# and exited; the daemon's own in-memory copy never saw that write and kept
# serving a stale (or entirely absent) record via /api/status indefinitely
# -- confirmed in production: the file on disk had the correct
# catalog_key, /api/status still reported active_recipe_key: null. Always
# reading fresh trades a small JSON read (this file is a few hundred bytes)
# on every status poll for correctness across every process that can write
# it, which is the same trade-off the other two ledgers already made.
ACTIVE_DEPLOYMENT_STATE_LOCK = threading.Lock()

def _load_active_deployment_state() -> dict:
    return _read_json_state(ACTIVE_DEPLOYMENT_STATE_PATH) or {}

def _set_active_deployment(host: str, catalog_key: str, topo_key: str, config_hash: Optional[str]) -> None:
    with ACTIVE_DEPLOYMENT_STATE_LOCK:
        data = _load_active_deployment_state()
        data[host] = {
            "catalog_key": catalog_key,
            "topo_key": topo_key,
            "config_hash": config_hash,
            "set_ts": time.time(),
        }
        _write_json_state(ACTIVE_DEPLOYMENT_STATE_PATH, data)

def _clear_active_deployment(hosts: list) -> None:
    with ACTIVE_DEPLOYMENT_STATE_LOCK:
        data = _load_active_deployment_state()
        changed = False
        for h in hosts:
            if data.pop(h, None) is not None:
                changed = True
        if changed:
            _write_json_state(ACTIVE_DEPLOYMENT_STATE_PATH, data)
# Generous on purpose: some recipes set VLLM_ENGINE_INITIALIZATION_TIMEOUT
# up to 3600s for cold multi-hour JIT compiles. This just bounds how long a
# stale/superseded pending record can linger before being ignored -- it is
# not a deploy timeout and doesn't affect deploy behavior at all.
PENDING_LAUNCH_STALE_SEC = 3600 * 3

# --- Global Thread Pool ---
WORKER_POOL = ThreadPoolExecutor(max_workers=len(HOSTS) * 2)

# --- Status Call Bounding ---
STATUS_CALL_TIMEOUT_SEC = 12  # seconds

_STATUS_LOCK = threading.Lock()
_STATUS_INFLIGHT: Future | None = None
_STATUS_CACHE: dict | None = None
_STATUS_CACHE_TS = 0.0
_STATUS_CACHE_TTL_SEC = 2  # dedupe bursts of near-simultaneous polls

# --- Telemetry Session State ---
class SessionTracker:
    def __init__(self):
        # RLock, not Lock: update() holds this lock for its entire body,
        # and can itself call _commit_session() (which also acquires this
        # lock) when its periodic-flush-while-active condition fires --
        # any sustained session eventually hits this. A plain Lock cannot
        # be re-acquired by the thread already holding it and deadlocks
        # permanently right there, with no possible recovery short of
        # restarting the process. Confirmed via py-spy dump against a live
        # wedged production process on 2026-08-28 (Thread "ThreadPoolExecutor-0_1",
        # blocked inside _commit_session, called from update, called from
        # _compute_cluster_status_impl) -- this is the actual root cause of
        # the get_cluster_status() staleness incidents on 2026-08-25,
        # 2026-08-27, and 2026-08-28, not any of the WORKER_POOL/SSH-layer
        # theories investigated earlier the same day. Since
        # get_cluster_status() only ever keeps one _STATUS_INFLIGHT future
        # at a time, this single self-deadlocked thread wedges status
        # polling entirely, indefinitely, the first time any session runs
        # long enough to hit the 1-hour periodic flush.
        self.lock = threading.RLock()
        self.active = False
        self.first_active_ts = 0.0
        self.last_active_ts = 0.0
        self.last_flush_ts = 0.0
        self.model = None
        self.topo = None
        
        self.start_p_tok = 0.0
        self.start_g_tok = 0.0
        self.start_d_tok = 0.0
        self.start_a_tok = 0.0

        self.flushed_p_tok = 0.0
        self.flushed_g_tok = 0.0
        self.flushed_d_tok = 0.0
        self.flushed_a_tok = 0.0

        self.cur_p_tok = 0.0
        self.cur_g_tok = 0.0
        self.cur_d_tok = 0.0
        self.cur_a_tok = 0.0

    def _load_last_seen_raw(self, key: str) -> Optional[dict]:
        """
        Read the ledger's persisted raw-counter checkpoint for `key`
        (model::topo), if any. This is what makes update()'s active
        transition (below) durable across an orchestrator restart: without
        it, a freshly-instantiated tracker has cur_p_tok/cur_g_tok == 0.0,
        so the very first real metrics it sees always look like "growth
        from zero" -- which incorrectly re-baselines flushed_* to
        vLLM's CURRENT cumulative total and permanently discards
        everything generated before that moment, no matter how large.
        (Confirmed in production: 2026-08-25/26 outage, ~29M real prompt
        tokens and ~730K real generation tokens silently reduced to 190/763
        in the ledger after an orchestrator restart -- exactly this bug.)
        """
        data = _read_json_state(LEDGER_PATH)
        if data is None:
            return None
        entry = data.get(key)
        if not isinstance(entry, dict):
            return None
        raw = entry.get("last_seen_raw")
        return raw if isinstance(raw, dict) else None

    def update(self, metrics: dict, model: str, topo: str):
        with self.lock:
            p_tok = metrics.get("prompt_tokens", 0.0)
            g_tok = metrics.get("gen_tokens", 0.0)
            d_tok = metrics.get("draft_tokens", 0.0)
            a_tok = metrics.get("accepted_tokens", 0.0)
            reqs = metrics.get("running_requests", 0)

            now = time.time()
            is_moving = reqs > 0 or p_tok > self.cur_p_tok or g_tok > self.cur_g_tok or d_tok > self.cur_d_tok

            if not self.active and is_moving:
                self.active = True
                self.model = model
                self.topo = topo
                self.first_active_ts = now
                self.last_active_ts = now
                self.last_flush_ts = now

                # Default (no prior checkpoint, or engine genuinely
                # restarted since): baseline to current -- this is the
                # original "fresh start" behavior, correct when there's
                # nothing to resume from.
                baseline_p, baseline_g, baseline_d, baseline_a = p_tok, g_tok, d_tok, a_tok

                raw = self._load_last_seen_raw(f"{model}::{topo}")
                if raw is not None:
                    ckpt_p = raw.get("p", 0.0)
                    ckpt_g = raw.get("g", 0.0)
                    # Only resume from the checkpoint if vLLM's counters are
                    # still >= it -- i.e. the same engine process is still
                    # running (or a later one that's already surpassed the
                    # old count). If current counters are LOWER than the
                    # checkpoint, the engine itself was redeployed/reset
                    # since, and resuming would produce a negative diff --
                    # fall back to the fresh-start baseline set above.
                    if p_tok >= ckpt_p and g_tok >= ckpt_g:
                        baseline_p = ckpt_p
                        baseline_g = ckpt_g
                        baseline_d = raw.get("d", 0.0) if d_tok >= raw.get("d", 0.0) else d_tok
                        baseline_a = raw.get("a", 0.0) if a_tok >= raw.get("a", 0.0) else a_tok

                self.start_p_tok = baseline_p
                self.start_g_tok = baseline_g
                self.start_d_tok = baseline_d
                self.start_a_tok = baseline_a

                self.flushed_p_tok = baseline_p
                self.flushed_g_tok = baseline_g
                self.flushed_d_tok = baseline_d
                self.flushed_a_tok = baseline_a
            elif self.active and is_moving:
                self.last_active_ts = now
            
            self.cur_p_tok = p_tok
            self.cur_g_tok = g_tok
            self.cur_d_tok = d_tok
            self.cur_a_tok = a_tok

            if self.active:
                if (now - self.last_active_ts) > 600:
                    self._commit_session()
                    self.active = False
                elif (now - self.last_flush_ts) > 3600:
                    self._commit_session()

    def _commit_session(self):
        with self.lock:
            if not self.model or not self.topo:
                return
            
            p_diff = self.cur_p_tok - self.flushed_p_tok
            g_diff = self.cur_g_tok - self.flushed_g_tok
            d_diff = self.cur_d_tok - self.flushed_d_tok
            a_diff = self.cur_a_tok - self.flushed_a_tok
            
            if p_diff <= 0 and g_diff <= 0 and d_diff <= 0 and a_diff <= 0:
                return
            
            data = _read_json_state(LEDGER_PATH) or {}
                
            key = f"{self.model}::{self.topo}"
            if key not in data or not isinstance(data[key], dict):
                data[key] = {"cached": [], "compiled": [], "downloaded": [], "lifetime": {"in": 0, "out": 0, "draft": 0, "accepted": 0}}
                
            if "lifetime" not in data[key]:
                data[key]["lifetime"] = {"in": 0, "out": 0, "draft": 0, "accepted": 0}
                
            data[key]["lifetime"]["in"] += int(p_diff)
            data[key]["lifetime"]["out"] += int(g_diff)
            data[key]["lifetime"]["draft"] += int(d_diff)
            data[key]["lifetime"]["accepted"] += int(a_diff)

            # Raw cumulative-since-engine-boot counters as of this commit,
            # independent of the lifetime totals above. This is what
            # update() resumes from after a restart -- see
            # _load_last_seen_raw()'s docstring for why this matters.
            data[key]["last_seen_raw"] = {
                "p": self.cur_p_tok,
                "g": self.cur_g_tok,
                "d": self.cur_d_tok,
                "a": self.cur_a_tok,
            }

            _write_json_state(LEDGER_PATH, data)

            self.flushed_p_tok = self.cur_p_tok
            self.flushed_g_tok = self.cur_g_tok
            self.flushed_d_tok = self.cur_d_tok
            self.flushed_a_tok = self.cur_a_tok
            self.last_flush_ts = time.time()

    def get_live_stats(self) -> dict:
        with self.lock:
            if not self.active:
                return {"active": False}
                
            active_time = max(0.1, self.last_active_ts - self.first_active_ts)
            g_diff = self.cur_g_tok - self.start_g_tok
            d_diff = self.cur_d_tok - self.start_d_tok
            a_diff = self.cur_a_tok - self.start_a_tok
            
            tps = g_diff / active_time
            mtp_rate = (a_diff / d_diff * 100) if d_diff > 0 else 0.0
            
            return {
                "active": True,
                "duration_sec": int(active_time),
                "tps": round(tps, 1),
                "mtp_rate": round(mtp_rate, 1),
                "gen_tokens": int(g_diff),
                "draft_tokens": int(d_diff),
                "accepted_tokens": int(a_diff)
            }

SESSION_TRACKER = SessionTracker()

# --- Core Helpers ---
def get_lightweight_telemetry(ip: str, user: str) -> dict:
    """Queries GPU thermal, util, power, and host unified memory availability in a single pass."""
    cmd = [
        "bash", "-c", 
        "/usr/bin/nvidia-smi --query-gpu=temperature.gpu,utilization.gpu,power.draw,power.limit --format=csv,noheader,nounits | head -n 1 && "
        "awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo"
    ]
    res = run_ssh(ip, user, cmd, capture=True, timeout=10)
    
    telemetry = {
        "gpu_temp_c": "N/A", 
        "gpu_util_pct": "N/A", 
        "power_draw_w": "N/A", 
        "power_limit_w": "N/A", 
        "host_mem_avail_mb": "N/A"
    }
    
    if res.returncode == 0 and res.stdout.strip():
        lines = [l.strip() for l in res.stdout.strip().splitlines() if l.strip()]
        if len(lines) >= 1:
            gpu_parts = [p.strip() for p in lines[0].split(",")]
            
            if len(gpu_parts) >= 1 and gpu_parts[0].isdigit(): 
                telemetry["gpu_temp_c"] = int(gpu_parts[0])
            if len(gpu_parts) >= 2 and gpu_parts[1].isdigit(): 
                telemetry["gpu_util_pct"] = int(gpu_parts[1])
            
            if len(gpu_parts) >= 3:
                try: telemetry["power_draw_w"] = int(float(gpu_parts[2]))
                except ValueError: pass
                
            if len(gpu_parts) >= 4:
                try: telemetry["power_limit_w"] = int(float(gpu_parts[3]))
                except ValueError: pass
                
        if len(lines) >= 2 and lines[1].isdigit():
            telemetry["host_mem_avail_mb"] = int(lines[1])
            
    return telemetry

def check_vllm_health(head_ip: str = PRIMARY_HOST_IP, port: int = 8000) -> bool:
    url = f"http://{head_ip}:{port}/health"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=2) as response:
            return response.status == 200
    except Exception:
        return False

def wait_for_cluster_ready(head_ip: str = PRIMARY_HOST_IP, timeout_sec: int = 900, poll_interval: int = 15) -> bool:
    start_time = time.time()
    print(f"[+] Polling http://{head_ip}:8000/health until serving ready (Timeout: {timeout_sec}s)...")
    
    while time.time() - start_time < timeout_sec:
        if check_vllm_health(head_ip):
            elapsed = int(time.time() - start_time)
            print(f"[+] vLLM Engine is HEALTHY and serving! (Warmup took {elapsed}s)")
            return True
        time.sleep(poll_interval)
        
    print(f"[-] Timeout ({timeout_sec}s) reached waiting for cluster readiness.")
    return False

def parse_iso_time(ts_str: str) -> float:
    try:
        ts_clean = ts_str.split(".")[0].replace("Z", "")
        dt = datetime.datetime.strptime(ts_clean, "%Y-%m-%dT%H:%M:%S")
        return dt.replace(tzinfo=datetime.timezone.utc).timestamp()
    except Exception:
        return time.time()

_LOG_LINE_PREFIX_RE = re.compile(r'^\([^)]*\)\s*')  # e.g. "(APIServer pid=2331) "

_ARGPARSE_ERROR_RE = re.compile(r'^\S+:\s*error:\s')

def _detect_crash_signature(clean_text: str) -> Optional[str]:
    """
    Returns a short crash reason if the log tail contains either an
    unhandled Python traceback OR an argparse-style CLI usage error, else
    None.

    Traceback detection exists because detect_model_stage()'s keyword scan
    below matches on substrings anywhere in the log -- including inside an
    exception's OWN error message. A ValueError reading "nvfp4 KV cache is
    not supported with MLA backends" contains the literal phrase "kv
    cache" and silently matched the WARMUP bucket, reporting a
    definitively crashed engine as "NOT READY - WARMUP" with an ETA that
    counted up forever since nothing was ever going to finish. Checking
    for a traceback FIRST and short-circuiting avoids this whole class of
    false positive regardless of what a future crash's error text happens
    to contain -- not just this one specific message.

    The argparse branch exists because a malformed CLI argument (observed
    live: a flag accidentally inserted between --speculative-config and
    its JSON value, splitting them) causes vLLM's arg parser to exit via
    `parser.error()` -- which prints a usage dump and a single
    "<prog>: error: <message>" line, with NO traceback at all. Without
    this branch, that failure fell through every keyword bucket below
    (hyphenated flag names like `--kv-cache-dtype` don't collide with the
    space-delimited "kv cache" WARMUP keyword the way natural-language
    error prose does) and landed on the default "NOT READY - INITIALIZING"
    -- still wrong, still counting an ETA against a process that's already
    exited, just less dramatically wrong than the WARMUP misfire.

    Also matters because in a 2-node Ray deploy, the container's PID 1 is
    `ray start --block`, not the vLLM engine -- the engine runs as a
    separate `docker exec -d` process, detached from the container's own
    process tree. Docker correctly reports the container RUNNING (Ray is
    fine) even after the engine itself has crashed, so container-level
    health alone can't be trusted here; the logs are the only signal.
    """
    lower = clean_text.lower()
    lines = [l.strip() for l in clean_text.splitlines() if l.strip()]

    if "traceback (most recent call last)" in lower:
        # Walk backward for the actual exception line -- by Python
        # convention the last non-empty line after a traceback is
        # "SomeError: message" or "pkg.mod.SomeError: message". vLLM's
        # multi-process API server prefixes every log line with e.g.
        # "(APIServer pid=2331) ", which has to be stripped first or the
        # line-start anchor never matches.
        for line in reversed(lines):
            stripped = _LOG_LINE_PREFIX_RE.sub('', line)
            if re.match(r'^[\w.]+(?:Error|Exception):\s', stripped):
                return stripped[:160]
        return "Unhandled exception (see logs)"

    # No traceback -- check for an argparse-style usage error instead.
    # This happens early in process startup, before vLLM's multi-process
    # logging wrapper is even active, so the "(APIServer pid=...)" prefix
    # is typically absent -- the strip below is a no-op in that case,
    # which is fine.
    for line in reversed(lines):
        stripped = _LOG_LINE_PREFIX_RE.sub('', line)
        if _ARGPARSE_ERROR_RE.match(stripped):
            return stripped[:160]

    return None

def detect_model_stage(ip: str, user: str, c_name: str) -> str:
    res = run_ssh(ip, user, ["docker", "logs", "--tail", "250", c_name], timeout=10)
    raw_text = res.stdout + res.stderr
    clean_text = ANSI_ESCAPE.sub('', raw_text)

    crash = _detect_crash_signature(clean_text)
    if crash:
        return f"CRASHED (ENGINE EXITED: {crash})"

    lines = [l.strip().lower() for l in clean_text.splitlines() if l.strip()]

    for line in reversed(lines):
        if any(k in line for k in ["downloading", "fetching", "allocating", "huggingface"]):
            return "NOT READY - DOWNLOADING"
        if any(k in line for k in ["warming up", "warmup", "kv cache", "cuda graph", "mhc", "profiling", "capturing", "graph capture"]):
            return "NOT READY - WARMUP"
        if any(k in line for k in ["tilelang", "deepgemm", "kernel", "compiling", "jit", "tuning", "building"]):
            return "NOT READY - COMPILING KERNELS"
        if any(k in line for k in ["loading weights", "safetensors", "shard", "loading model", "checkpoint"]):
            return "NOT READY - LOADING SHARDS"

    return "NOT READY - INITIALIZING"

MAX_RECORDABLE_LOAD_SEC = 14400  # 4 hours

_RECORDED_LOAD_STARTS: dict[str, float] = {}
_RECORDED_LOAD_STARTS_LOCK = threading.Lock()

def record_load_time(model: str, topo_key: str, duration_sec: int, load_type: str = "cached"):
    if duration_sec > MAX_RECORDABLE_LOAD_SEC or duration_sec < 10: return
    data = _read_json_state(LEDGER_PATH) or {}
    key = f"{model}::{topo_key}"
    
    if key not in data or isinstance(data[key], list):
        data[key] = {"cached": [], "compiled": [], "downloaded": [], "lifetime": {"in": 0, "out": 0, "draft": 0, "accepted": 0}}
        
    existing = data[key].get(load_type, [])
    if existing and existing[-1] == duration_sec:
        return

    data[key].setdefault(load_type, []).append(duration_sec)
    data[key][load_type] = data[key][load_type][-20:]
    _write_json_state(LEDGER_PATH, data)

def _record_launch_success(model: str, topo_key: str, config_hash: str):
    """
    Records that this exact (model, topo_key, config_hash) combination was
    confirmed healthy by a post-deploy health check. Called from the status
    polling loop (_compute_cluster_status_impl), not from execute_deployment
    directly -- see PENDING_LAUNCH_STATE's comment for why. Failure here
    must never break status polling, so this fails silently like
    record_load_time()/SessionTracker._commit_session() do.
    """
    data = _read_json_state(LEDGER_PATH) or {}

    key = f"{model}::{topo_key}"
    if key not in data or not isinstance(data[key], dict):
        data[key] = {"cached": [], "compiled": [], "downloaded": [], "lifetime": {"in": 0, "out": 0, "draft": 0, "accepted": 0}}

    launch_history = data[key].setdefault("launch_history", {})
    prior = launch_history.get(config_hash, {})
    launch_history[config_hash] = {
        "last_success_ts": time.time(),
        "count": prior.get("count", 0) + 1,
    }

    _write_json_state(LEDGER_PATH, data)

def get_estimated_load_time(model: str, topo_key: str, load_type: str = "cached") -> tuple[int, bool]:
    """Estimated seconds-to-ready for (model, topo_key) in `load_type`, plus
    whether that estimate came from recorded history or a hardcoded default.

    Uses the MEDIAN of recorded samples, not the mean. Every phase list in
    this ledger that has more than two samples follows the same shape: a
    tight cluster of warm runs plus exactly one cold run an order of
    magnitude longer. Concretely, at the time this changed:

        deepseek-v4-flash-0731-1M::2_node compiled
            [320, 321, 322, 323, 344, 391, 2408]   mean 632  median 323
        deepseek-v4-flash-0731-dspark::2_node compiled
            [372..387 x9, 1689]                    mean 508  median 377
        gemma-4-31b::2_node downloaded
            [440, 462, 14276]                      mean 5059 median 462

    One cold run permanently inflating every subsequent estimate by 2-11x
    is worse than ignoring it: the countdown parks on a number the load
    will never approach, which reads as a stalled dashboard. The median
    tracks "what this usually takes" and one anomalous sample cannot move
    it.

    Known limits, neither of which median fixes:
      - The high sample is often NOT noise -- it is the genuine first-JIT
        run, filed into the same bucket as the warm reruns because
        record_load_time() classifies by substring-scanning `docker logs
        --tail 5000` at READY. So a genuinely cold first deploy will now
        underestimate and land in "Finishing startup (+Ns over est.)".
        That is the honest failure direction, but the real fix is discrete
        per-phase timestamps, not a different average.
      - No recency weighting. A config change that legitimately shifts
        load time takes ceil(n/2) runs to move the median. The phase lists
        are not keyed by config_hash the way launch_history is.
    """
    default_ests = {"cached": 180, "compiled": 1500, "downloaded": 4500}
    default_est = default_ests.get(load_type, 180)
    
    if "deepseek" in model.lower():
        default_ests = {"cached": 700, "compiled": 1800, "downloaded": 5000}
        default_est = default_ests.get(load_type, 700)
        
    data = _read_json_state(LEDGER_PATH)
    if data is None:
        return default_est, False
    key_data = data.get(f"{model}::{topo_key}", {})
    if isinstance(key_data, dict):
        times = key_data.get(load_type, [])
        # Guard the list shape as well as its truthiness -- this file is
        # hand-editable (see ledger_set_lifetime()'s CLI command) and a
        # malformed entry must degrade to the default, not raise inside a
        # status poll.
        if times and isinstance(times, list):
            numeric = [t for t in times if isinstance(t, (int, float))]
            if numeric:
                return int(statistics.median(numeric)), True
    return default_est, False

def get_vllm_metrics(head_ip: str = PRIMARY_HOST_IP, port: int = 8000) -> dict:
    metrics = {
        "tps": 0.0, "running_requests": 0, "waiting_requests": 0,
        "prompt_tokens": 0.0, "gen_tokens": 0.0, "draft_tokens": 0.0, "accepted_tokens": 0.0
    }
    try:
        url = f"http://{head_ip}:{port}/metrics"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=1.5) as response:
            content = response.read().decode("utf-8")
            for line in content.splitlines():
                if line.startswith("#"): continue
                parts = line.split()
                if not parts: continue
                try: val = float(parts[-1])
                except ValueError: continue

                if "vllm:avg_generation_throughput_tok_per_s" in line:
                    metrics["tps"] = round(val, 1)
                elif "vllm:num_requests_running" in line:
                    metrics["running_requests"] = int(val)
                elif "vllm:num_requests_waiting" in line:
                    metrics["waiting_requests"] = int(val)
                elif "vllm:prompt_tokens_total" in line:
                    metrics["prompt_tokens"] = val
                elif "vllm:generation_tokens_total" in line:
                    metrics["gen_tokens"] = val
                elif any(k in line for k in ["vllm:spec_decode_num_draft_tokens_total", "vllm:num_spec_tokens_draft_total"]):
                    metrics["draft_tokens"] = val
                elif any(k in line for k in ["vllm:spec_decode_num_accepted_tokens_total", "vllm:num_spec_tokens_accepted_total"]):
                    metrics["accepted_tokens"] = val
    except Exception as exc:
        # Unlike check_vllm_health() above -- whose failures are routine
        # during normal model warmup -- a parse exception here means the
        # /metrics response came back but didn't scrape the way this
        # function expects (format change, unexpected content, etc).
        # That's not routine, and silently returning all-zero metrics
        # looks identical to "genuinely idle cluster" on the dashboard --
        # exactly the kind of silent-wrong-number failure worth a signal
        # for, so it doesn't take a "why is TPS stuck at 0" investigation
        # to notice.
        print(f"[!] get_vllm_metrics: failed to parse /metrics response from {head_ip}:{port} - {exc}")
    return metrics

def cache_inventory(target_hosts: list = None) -> dict:
    """
    Read-only snapshot of every cache root on each host: what's there, how
    big, how old, in LRU order (oldest first). No thresholds, no deletion --
    safe to run against a live cluster at any time. See INVENTORY_ROOTS for
    what's covered (includes huggingface and ComputeCache for visibility,
    even though prune_cluster_cache() never touches those).
    """
    hosts_to_check = target_hosts if target_hosts else list(HOSTS.keys())
    results = {}

    for host in hosts_to_check:
        if host not in HOSTS:
            continue
        ip = HOSTS[host]["ip"]
        cmd = ["python3", "-c", _REMOTE_INVENTORY_SCRIPT, json.dumps(INVENTORY_ROOTS)]
        res = run_ssh(ip, None, cmd, capture=True, timeout=60)

        if res.returncode != 0:
            results[host] = {"status": "error", "message": res.stderr.strip() or "inventory script failed"}
            continue

        try:
            data = json.loads(res.stdout.strip())
        except Exception as exc:
            results[host] = {"status": "error", "message": f"unparseable output: {exc}"}
            continue

        gb = lambda b: round(b / (1024 ** 3), 2)
        roots_summary = {}
        for label, r in data["roots"].items():
            roots_summary[label] = {
                "exists": r["exists"],
                "entry_count": r["entry_count"],
                "total_gb": gb(r["total_bytes"]),
                "oldest": r["entries"][0] if r["entries"] else None,
                "newest": r["entries"][-1] if r["entries"] else None,
                "entries": r["entries"],
            }

        results[host] = {
            "status": "ok",
            "mount_options": data["mount_options"],
            "atime_reliable": "noatime" not in data["mount_options"],
            "disk_free_gb": gb(data["disk_free_bytes"]),
            "disk_total_gb": gb(data["disk_total_bytes"]),
            "roots": roots_summary,
        }

    return {"status": "success", "hosts": results}

def _hf_cache_dirname(hf_path: str) -> str:
    """
    Reproduces HuggingFace Hub's on-disk cache directory naming convention
    for a repo_id -- 'org/Repo-Name' -> 'models--org--Repo-Name'. This is
    what huggingface_hub's snapshot_download() actually creates under
    ~/.cache/huggingface/hub/ (used both by vLLM's own model loading and
    by cache_cluster_assets.py's pre-fetch path), so it's a reliable target
    to compute without needing to inspect the filesystem first.
    """
    return "models--" + hf_path.replace("/", "--")

def _record_hf_path(catalog_key: str, hf_path: str) -> None:
    """
    Persists catalog_key -> hf_path (+ derived cache dirname) every time a
    model is deployed, so it can still be identified by that same catalog
    key later even after its recipe file is deleted. Fires regardless of
    whether the deploy attempt goes on to succeed -- the recipe genuinely
    did specify this hf_path for this key at this point in time, which is
    exactly the provenance record this exists to preserve.
    """
    data = _read_json_state(HF_PATH_LEDGER_PATH) or {}
    data[catalog_key] = {
        "hf_path": hf_path,
        "cache_dirname": _hf_cache_dirname(hf_path),
        "last_deployed": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _write_json_state(HF_PATH_LEDGER_PATH, data)

def _load_hf_path_ledger() -> dict:
    return _read_json_state(HF_PATH_LEDGER_PATH) or {}

def _resolve_model_to_hf_path(model: str) -> Optional[str]:
    """
    Resolution order:
    1. A live catalog key -- current recipe still exists.
    2. A catalog key recorded in the historical hf_path ledger -- the
       recipe has since been deleted, but a prior deploy recorded the
       mapping (see _record_hf_path). This is what makes "flush the
       cache for a model I retired a while ago" usable by its old,
       memorable catalog key rather than requiring the exact raw
       hf_path (org/repo) to be typed correctly from memory.
    3. A raw HF repo_id (org/repo) passed directly -- the fallback for a
       model that was never deployed through this orchestrator (no
       ledger entry) or predates the ledger's introduction.
    """
    catalog = load_model_catalog().get("catalog", {}).get("models", {})
    if model in catalog:
        return catalog[model].get("hf_path")

    ledger = _load_hf_path_ledger()
    if model in ledger:
        return ledger[model].get("hf_path")

    if "/" in model:
        return model
    return None

def flush_model_cache(model: str, include_jit: bool = False, dry_run: bool = False, force: bool = False, target_hosts: list = None) -> dict:
    """
    Clears the on-disk cache for one model: HuggingFace weights always,
    JIT/compute kernel caches optionally via include_jit.

    Two real use cases this covers:
    - Corruption: a partial/interrupted HF download leaves a directory the
      loader will happily read as complete, which can surface as a
      garbage-output or crash-on-load bug with no obvious cause. Deleting
      the cache directory forces a clean re-download on the next deploy.
    - Retirement: HF weights are by far the largest disk consumer per
      model (multi-hundred-GB to multi-TB, vs. single-digit-MB JIT
      caches) -- this is what actually matters for reclaiming space once
      a model is no longer wanted.

    include_jit does a FULL wipe of every JIT cache root on the target
    host(s), not a model-scoped one -- JIT entries are keyed by compiled-
    kernel signature (shapes, dtypes), not by model name, and we don't
    have documented ground truth on Triton/TileLang/DeepGEMM/FlashInfer's
    internal naming conventions to reliably attribute an entry to one
    model (see docs/ROADMAP.md's "Cache integrity retrospection" entry).
    This means include_jit affects every OTHER model's compiled kernels on
    that host too, not just the target one -- opt-in and stated plainly in
    every place this result gets printed, for exactly that reason.

    The "is this model currently active" check below is best-effort, not a
    guarantee: get_cluster_status()'s active_model field falls back to the
    literal string "Active Container" whenever the loaded model can't be
    parsed from the container command line -- which is the common case for
    a 2-node Ray deploy (see docs/TROUBLESHOOTING.md #6 / ROADMAP.md's
    engine-health-monitoring entry for why). In that situation this check
    will not detect an active model and will not block. Confirm via
    `dgx-config status` yourself before flushing anything you're not
    certain is idle -- don't rely on this guard alone.
    """
    hf_path = _resolve_model_to_hf_path(model)
    if not hf_path:
        return {"status": "error",
                "message": f"'{model}' not found in the current catalog and doesn't look like an HF repo_id (org/repo). "
                           f"For an already-retired model, pass its hf_path directly."}

    hosts_to_check = [h for h in (target_hosts if target_hosts else list(HOSTS.keys())) if h in HOSTS]

    if not force:
        status = get_cluster_status()
        for host in hosts_to_check:
            active_model = status.get("hosts", {}).get(host, {}).get("active_model", "None")
            if active_model != "None" and (active_model in hf_path or hf_path.endswith(active_model)):
                return {"status": "error",
                        "message": f"Model appears currently loaded on {host} (active_model='{active_model}'). "
                                   f"Teardown first, or pass force=True to override. NOTE: this check cannot see "
                                   f"an active model on a 2-node Ray deploy where the engine's command line wasn't "
                                   f"parseable (shows as 'Active Container') -- verify with 'dgx-config status' "
                                   f"yourself if you're not sure."}

    hf_cache_dir = f"~/.cache/huggingface/hub/{_hf_cache_dirname(hf_path)}"
    jit_roots_arg = JIT_CACHE_ROOTS if include_jit else []

    verb = "Dry-run evaluating" if dry_run else "Flushing"
    print(f"[+] {verb} cache for '{model}' (hf_path: {hf_path}){' + full JIT cache wipe' if include_jit else ''}...")

    results = {}
    for host in hosts_to_check:
        ip = HOSTS[host]["ip"]
        cmd = ["python3", "-c", _REMOTE_MODEL_FLUSH_SCRIPT, hf_cache_dir, json.dumps(jit_roots_arg), "1" if dry_run else "0"]
        res = run_ssh(ip, None, cmd, capture=True, timeout=120)

        if res.returncode != 0:
            msg = res.stderr.strip() or "flush script failed"
            print(f"  [{host}] ERROR: {msg}")
            results[host] = {"status": "error", "message": msg}
            continue

        try:
            data = json.loads(res.stdout.strip())
        except Exception as exc:
            results[host] = {"status": "error", "message": f"unparseable output: {exc}"}
            continue

        gb = lambda b: round(b / (1024 ** 3), 2)
        hf_result = data["hf_cache"]
        jit_results = data["jit_caches"]
        jit_gb = gb(sum(e["bytes"] for e in jit_results))

        results[host] = {
            "status": "ok",
            "hf_cache_path": hf_result["path"],
            "hf_cache_gb": gb(hf_result["bytes"]),
            "hf_cache_action": hf_result["status"],
            "jit_entries_flushed": len(jit_results),
            "jit_gb_flushed": jit_gb,
            "dry_run": dry_run,
        }

        print(f"  [{host}] HF cache ({results[host]['hf_cache_gb']} GB): {hf_result['status']}")
        if include_jit:
            print(f"  [{host}] JIT caches: {len(jit_results)} entries, {jit_gb} GB -- ALL models on this host affected, not just '{model}'.")

    return {"status": "success", "model": model, "hf_path": hf_path, "include_jit": include_jit, "details": results}

def find_cached_models(target_hosts: list = None) -> dict:
    """
    Cross-references every per-model directory under
    ~/.cache/huggingface/hub/ (via the huggingface_models inventory root)
    against the live catalog and the historical hf_path ledger, so
    retired/orphaned model weight caches can be found without needing to
    already know their names -- the whole point of this command versus
    flush_model_cache(), which requires you to name a specific model.

    Three statuses per cached directory:
    - "active": hf_path matches a model currently in the catalog.
    - "retired (known)": no longer in the catalog, but a prior deploy's
      ledger entry (_record_hf_path) identifies which catalog key it
      used to be.
    - "orphaned (no record)": matches neither. Either never deployed
      through this orchestrator, or the model was retired before the
      hf_path ledger existed to record it -- the ledger only covers
      deploys from its own introduction forward, it can't retroactively
      know about anything before that. Still fully identifiable and
      flushable by its raw cache directory name (pass it to
      flush_model_cache as a raw hf_path: replace the leading
      "models--" and the remaining "--" separators back to "/" -- e.g.
      "models--org--Model-Name" -> "org/Model-Name" -- though the exact
      original hf_path can't be guaranteed reconstructible if the repo
      name itself legitimately contained "--").
    """
    catalog = load_model_catalog().get("catalog", {}).get("models", {})
    live_dirname_to_key = {}
    for key, m in catalog.items():
        hf_path = m.get("hf_path")
        if hf_path:
            live_dirname_to_key[_hf_cache_dirname(hf_path)] = key

    ledger = _load_hf_path_ledger()
    ledger_dirname_to_key = {}
    for key, entry in ledger.items():
        dirname = entry.get("cache_dirname")
        if not dirname and entry.get("hf_path"):
            dirname = _hf_cache_dirname(entry["hf_path"])
        # Live catalog entries take priority if a key somehow appears in
        # both -- the ledger is a fallback, not an override.
        if dirname and dirname not in live_dirname_to_key:
            ledger_dirname_to_key[dirname] = key

    inv = cache_inventory(target_hosts)
    results = {}
    for host, data in inv.get("hosts", {}).items():
        if data.get("status") != "ok":
            results[host] = data
            continue

        hub_root = data.get("roots", {}).get("huggingface_models", {})
        entries = hub_root.get("entries", [])
        annotated = []
        for e in entries:
            dirname = e["name"]
            if dirname in live_dirname_to_key:
                status = "active"
                matched_model = live_dirname_to_key[dirname]
            elif dirname in ledger_dirname_to_key:
                status = "retired (known)"
                matched_model = ledger_dirname_to_key[dirname]
            else:
                status = "orphaned (no record)"
                matched_model = None

            annotated.append({
                "cache_dirname": dirname,
                "matched_model": matched_model,
                "status": status,
                "gb": round(e["bytes"] / (1024 ** 3), 2),
                "age_days": e["age_days"],
            })

        annotated.sort(key=lambda x: x["gb"], reverse=True)
        results[host] = {"status": "ok", "models": annotated}

    return {"status": "success", "hosts": results}

def ipc_inventory(target_hosts: list = None) -> dict:
    """
    Read-only snapshot of SysV shared memory segments, SysV semaphore
    arrays, and POSIX /dev/shm files on each host. No deletion, safe
    against a live cluster at any time.

    This matters because our containers run with --ipc=host: Ray's plasma
    object store (shared-memory-backed) and vLLM/PyTorch's own
    multiprocessing shared memory usage all live in the HOST's own IPC
    namespace, not an isolated per-container one. A process killed
    abruptly rather than shut down cleanly can leak shared memory that
    persists on the host indefinitely -- these aren't ordinary process
    memory that the kernel reclaims on exit, they have to be explicitly
    unlinked. See sweep_ipc_orphans() for the safe cleanup half of this.
    """
    hosts_to_check = target_hosts if target_hosts else list(HOSTS.keys())
    results = {}

    for host in hosts_to_check:
        if host not in HOSTS:
            continue
        ip = HOSTS[host]["ip"]
        res = run_ssh(ip, None, ["python3", "-c", _REMOTE_IPC_INVENTORY_SCRIPT], capture=True, timeout=30)

        if res.returncode != 0:
            results[host] = {"status": "error", "message": res.stderr.strip() or "ipc inventory script failed"}
            continue

        try:
            data = json.loads(res.stdout.strip())
        except Exception as exc:
            results[host] = {"status": "error", "message": f"unparseable output: {exc}"}
            continue

        gb = lambda b: round(b / (1024 ** 3), 3)
        shm_segments = data.get("shm_segments", [])
        orphaned = [s for s in shm_segments if s.get("nattch", 1) == 0]
        attached = [s for s in shm_segments if s.get("nattch", 1) != 0]

        disk = data.get("dev_shm_disk", {})
        dev_shm_files = sorted(data.get("dev_shm_files", []), key=lambda f: f["bytes"], reverse=True)

        results[host] = {
            "status": "ok",
            "shm_segments_total": len(shm_segments),
            "shm_segments_attached": len(attached),
            "shm_segments_orphaned": len(orphaned),
            "shm_orphaned_gb": gb(sum(s["size_bytes"] for s in orphaned)),
            "shm_attached_gb": gb(sum(s["size_bytes"] for s in attached)),
            "semaphore_count": len(data.get("semaphores", [])),
            "dev_shm_disk_free_gb": gb(disk.get("free_bytes", 0)) if disk else None,
            "dev_shm_disk_total_gb": gb(disk.get("total_bytes", 0)) if disk else None,
            "dev_shm_files": [
                {"name": f["name"], "gb": gb(f["bytes"]), "age_days": f["age_days"]}
                for f in dev_shm_files
            ],
            "orphaned_shm_segments": orphaned,
        }

    return {"status": "success", "hosts": results}

def sweep_ipc_orphans(target_hosts: list = None, dry_run: bool = False) -> dict:
    """
    Removes SysV shared memory segments with nattch == 0 -- provably
    unattached, a hard kernel-tracked guarantee, not a heuristic. Safe to
    run unconditionally: a segment still in use by anything, on this
    workload or any other on the shared host, is never touched.

    Deliberately does NOT touch POSIX /dev/shm files -- see the
    _REMOTE_IPC_SWEEP_SCRIPT module comment for why that's a real check
    worth building later rather than rushing now.
    """
    hosts_to_check = [h for h in (target_hosts if target_hosts else list(HOSTS.keys())) if h in HOSTS]
    verb = "Dry-run evaluating" if dry_run else "Sweeping"
    print(f"[+] {verb} orphaned SysV shared memory segments...")

    results = {}
    for host in hosts_to_check:
        ip = HOSTS[host]["ip"]
        # NOT sudo'd here -- the script itself only escalates for the
        # actual `ipcrm -m` removal call (see _REMOTE_IPC_SWEEP_SCRIPT's
        # comment). Reading /proc/sysvipc/shm needs no privilege, and
        # keeping the outer invocation unprivileged means the sudoers
        # entry on each host only needs to permit "ipcrm -m *", not
        # arbitrary "python3 -c *".
        cmd = ["python3", "-c", _REMOTE_IPC_SWEEP_SCRIPT, "1" if dry_run else "0"]
        res = run_ssh(ip, None, cmd, capture=True, timeout=30)

        if res.returncode != 0:
            msg = res.stderr.strip() or "ipc sweep script failed"
            print(f"  [{host}] ERROR: {msg}")
            results[host] = {"status": "error", "message": msg}
            continue

        try:
            data = json.loads(res.stdout.strip())
        except Exception as exc:
            results[host] = {"status": "error", "message": f"unparseable output: {exc}"}
            continue

        gb = lambda b: round(b / (1024 ** 3), 3)
        results[host] = {
            "status": "ok",
            "segments_removed": len(data["removed"]),
            "gb_freed": gb(data["total_bytes_freed"]),
            "errors": data["errors"],
            "dry_run": data["dry_run"],
        }

        if results[host]["segments_removed"] == 0:
            print(f"  [{host}] No orphaned segments found.")
        else:
            action = "would remove" if dry_run else "removed"
            print(f"  [{host}] {action} {results[host]['segments_removed']} orphaned segment(s), {results[host]['gb_freed']} GB.")
        for err in data["errors"]:
            print(f"  [{host}] error removing shmid {err.get('shmid')}: {err.get('error')}")

    return {"status": "success", "details": results}

def prune_cluster_cache(min_free_gb: int = 50, headroom_gb: int = 20, dry_run: bool = False) -> dict:
    """
    LRU-evict JIT kernel caches, but ONLY on hosts currently below the free
    space floor -- above the floor, nothing is touched on that host.

    min_free_gb  -- floor. If free space is at or above this, no eviction
                    is considered at all.
    headroom_gb  -- when a host IS below the floor, evict oldest-first
                    until free space reaches (min_free_gb + headroom_gb),
                    not just up to the floor -- otherwise the very next
                    deploy's cache growth re-triggers eviction immediately.
    dry_run      -- report exactly what would be evicted and why, without
                    deleting anything. Read-only; safe against production.
    """
    target_free_bytes = (min_free_gb + headroom_gb) * (1024 ** 3)
    verb = "Dry-run evaluating" if dry_run else "Evaluating"
    print(f"[+] {verb} JIT caches (floor: {min_free_gb} GB, evict target: {min_free_gb + headroom_gb} GB)...")

    results = {}
    for host, meta in HOSTS.items():
        ip = meta["ip"]
        cmd = [
            "python3", "-c", _REMOTE_PRUNE_SCRIPT,
            json.dumps(JIT_CACHE_ROOTS),
            str(target_free_bytes),
            "1" if dry_run else "0",
        ]
        res = run_ssh(ip, None, cmd, capture=True, timeout=180)

        if res.returncode != 0:
            msg = res.stderr.strip() or "prune script failed"
            print(f"  [{host}] ERROR: {msg}")
            results[host] = {"status": "error", "message": msg}
            continue

        try:
            data = json.loads(res.stdout.strip())
        except Exception as exc:
            results[host] = {"status": "error", "message": f"unparseable output: {exc}"}
            continue

        gb = lambda b: round(b / (1024 ** 3), 2)
        summary = {
            "status": "ok",
            "mount_options": data["mount_options"],
            "atime_reliable": "noatime" not in data["mount_options"],
            "free_before_gb": gb(data["free_before"]),
            "free_after_gb": gb(data["free_after"]),
            "entries_total": data["entries_total"],
            "entries_evicted": len(data["evicted"]),
            "evicted": data["evicted"],
            "gb_freed": gb(data["bytes_freed"]),
            "dry_run": data["dry_run"],
            "errors": data["errors"],
        }

        if not summary["atime_reliable"]:
            print(f"  [{host}] NOTE: mount options include noatime -- atime is frozen, LRU order is falling back to mtime.")

        if summary["entries_evicted"] == 0:
            print(f"  [{host}] {summary['free_before_gb']} GB free (floor {min_free_gb} GB). Nothing considered.")
        else:
            action = "would evict" if dry_run else "evicted"
            print(f"  [{host}] {summary['free_before_gb']} GB free -> {action} {summary['entries_evicted']}/{data['entries_total']} entries ({summary['gb_freed']} GB), now {summary['free_after_gb']} GB.")
            for ev in summary["evicted"]:
                print(f"      - {ev['path']} ({gb(ev['bytes'])} GB, {ev['age_days']}d old) -- {ev['reason']}")

        if summary["errors"]:
            for err in summary["errors"]:
                print(f"  [{host}] eviction error: {err}")

        results[host] = summary

    return {"status": "success", "details": results}

def prune_cluster_ray_logs(retention_days: int = None, dry_run: bool = False) -> dict:
    """
    Age-based cleanup of ~/.cache/ray-logs/<run_id>/ across the cluster --
    the per-deploy Ray session dirs bind-mounted into every container so a
    crashed worker's logs survive teardown (see _jit_cache_mounts_and_env).

    Deliberately NOT modeled on prune_cluster_cache()'s free-space-floor
    eviction: these dirs are tiny relative to JIT/HF caches, so instead of
    reacting to disk pressure, anything older than retention_days is
    removed on every call regardless of current free space.

    retention_days -- defaults to cluster_config.yaml's
                       tuning.crash_log_retention_days.
    dry_run        -- report exactly what would be evicted and its age,
                       without deleting anything. Read-only; safe against
                       production.
    """
    tuning = load_cluster_config().tuning
    if retention_days is None:
        retention_days = getattr(tuning, "crash_log_retention_days", 7)
    retention_seconds = retention_days * 86400

    verb = "Dry-run evaluating" if dry_run else "Evaluating"
    print(f"[+] {verb} ray-logs for cleanup (retention: {retention_days}d)...")

    results = {}
    for host, meta in HOSTS.items():
        ip = meta["ip"]
        cmd = [
            "python3", "-c", _REMOTE_RAY_LOG_PRUNE_SCRIPT,
            "~/.cache/ray-logs",
            str(retention_seconds),
            "1" if dry_run else "0",
        ]
        res = run_ssh(ip, None, cmd, capture=True, timeout=120)

        if res.returncode != 0:
            msg = res.stderr.strip() or "prune script failed"
            print(f"  [{host}] ERROR: {msg}")
            results[host] = {"status": "error", "message": msg}
            continue

        try:
            data = json.loads(res.stdout.strip())
        except Exception as exc:
            results[host] = {"status": "error", "message": f"unparseable output: {exc}"}
            continue

        gb = lambda b: round(b / (1024 ** 3), 3)
        summary = {
            "status": "ok",
            "runs_evicted": len(data["evicted"]),
            "runs_kept": data["kept"],
            "evicted": data["evicted"],
            "gb_freed": gb(data["bytes_freed"]),
            "dry_run": data["dry_run"],
            "errors": data["errors"],
        }

        action = "would evict" if dry_run else "evicted"
        if summary["runs_evicted"] == 0:
            print(f"  [{host}] {summary['runs_kept']} run(s) within retention. Nothing to evict.")
        else:
            print(f"  [{host}] {action} {summary['runs_evicted']} run(s) older than {retention_days}d "
                  f"({summary['gb_freed']} GB), kept {summary['runs_kept']}.")
            for ev in summary["evicted"]:
                print(f"      - {ev['path']} ({gb(ev['bytes'])} GB, {ev['age_days']}d old)")

        if summary["errors"]:
            for err in summary["errors"]:
                print(f"  [{host}] eviction error: {err}")

        results[host] = summary

    return {"status": "success", "details": results}

def _detect_live_model_topo_metrics() -> dict:
    """
    Fresh, uncached detection of "what's actually running right now" --
    serving host, catalog-resolved model key, topology, and live vLLM
    /metrics values. Mirrors the same resolution _compute_cluster_status_impl()
    uses (so the key this produces matches what SESSION_TRACKER itself
    would use), but deliberately does NOT go through get_cluster_status()'s
    cache: correct_ledger_entry() exists specifically to recover from
    situations where that cache went stale/wedged, so relying on it here
    would be circular.
    """
    host_order = list(HOSTS.keys())
    futures = [WORKER_POOL.submit(_discover_host_container, host, meta) for host, meta in HOSTS.items()]
    deadline = time.monotonic() + STATUS_CALL_TIMEOUT_SEC
    results = _collect_bounded(futures, host_order, deadline, "Live detection")
    container_info = {h: (r if r is not None else {"host": h, "reachable": False}) for h, r in results.items()}

    serving_host = None
    for host in HOSTS:
        if container_info.get(host, {}).get("active_container") in (ContainerRole.STANDALONE, ContainerRole.HEAD):
            serving_host = host
            break
    if serving_host is None:
        return {"status": "error", "message": "No host has an active head/standalone vLLM container right now -- nothing to detect."}

    serving_ip = HOSTS[serving_host]["ip"]
    if not check_vllm_health(serving_ip):
        return {"status": "error", "message": f"vLLM on {serving_host} ({serving_ip}) is not responding to /health -- can't fetch live metrics."}

    vllm_metrics = get_vllm_metrics(serving_ip)
    catalog_models = load_model_catalog().get("catalog", {}).get("models", {})
    raw_loaded_model = container_info.get(serving_host, {}).get("loaded_model", "Unknown")
    matched_model, _, _ = _resolve_active_recipe(
        serving_host, catalog_models, raw_loaded_model, require_active=False
    )
    topo = "2_node" if len([h for h, i in container_info.items() if i.get("active_container") != "None"]) > 1 else "1_node"

    return {
        "status": "success",
        "serving_host": serving_host,
        "model": matched_model,
        "topo": topo,
        "prompt_tokens": vllm_metrics["prompt_tokens"],
        "gen_tokens": vllm_metrics["gen_tokens"],
        "draft_tokens": vllm_metrics["draft_tokens"],
        "accepted_tokens": vllm_metrics["accepted_tokens"],
    }

def correct_ledger_entry(
    model: Optional[str] = None,
    topo: Optional[str] = None,
    prompt_tokens: Optional[float] = None,
    gen_tokens: Optional[float] = None,
    draft_tokens: Optional[float] = None,
    accepted_tokens: Optional[float] = None,
    dry_run: bool = False,
    force: bool = False,
) -> dict:
    """
    One-off manual correction for a model_ledger.json entry's `lifetime`
    totals and `last_seen_raw` checkpoint, using values observed from
    vLLM's own /metrics endpoint (vllm:prompt_tokens_total /
    vllm:generation_tokens_total etc).

    Any of model/topo/prompt_tokens/gen_tokens left as None triggers
    live auto-detection via _detect_live_model_topo_metrics() -- i.e. the
    default, argument-free call corrects whatever's currently deployed
    using its own current live counters. Pass explicit values instead to
    correct a DIFFERENT (not currently loaded) model+topo key, or to
    supply a metrics snapshot you captured earlier rather than "right now".
    draft_tokens/accepted_tokens default to 0.0 if left unset and not
    auto-detected (e.g. explicit model/topo given without those two).

    This exists to repair drift caused by the SessionTracker restart bug
    (see SessionTracker._load_last_seen_raw()'s docstring) -- a fresh
    tracker instance re-baselining to "now" instead of resuming from a
    checkpoint, silently discarding real usage. That bug is fixed going
    forward; this is for correcting a ledger entry already damaged by it.

    Sets (does not add to) lifetime.in/out/draft/accepted, since a
    single-launch key's whole lifetime history IS its current live
    cumulative counters -- the existing (wrong, small) numbers are a
    subset of what's being provided here, not something to add on top
    of. If the key has had more than one distinct launch historically,
    this "set" semantics doesn't apply cleanly; don't use it as-is.

    Refuses to overwrite with smaller numbers than currently recorded
    unless force=True, as a sanity check against stale/transposed values.

    Writes a timestamped .bak of the whole ledger file before any write.
    Only touches the matched key's "lifetime" and "last_seen_raw" fields;
    every other key, and "cached"/"compiled"/"downloaded" on the matched
    key, are left untouched.
    """
    detected = None
    if model is None or topo is None or prompt_tokens is None or gen_tokens is None:
        detected = _detect_live_model_topo_metrics()
        if detected["status"] != "success":
            return detected
        model = model if model is not None else detected["model"]
        topo = topo if topo is not None else detected["topo"]
        prompt_tokens = prompt_tokens if prompt_tokens is not None else detected["prompt_tokens"]
        gen_tokens = gen_tokens if gen_tokens is not None else detected["gen_tokens"]
        if draft_tokens is None:
            draft_tokens = detected["draft_tokens"]
        if accepted_tokens is None:
            accepted_tokens = detected["accepted_tokens"]

    draft_tokens = draft_tokens if draft_tokens is not None else 0.0
    accepted_tokens = accepted_tokens if accepted_tokens is not None else 0.0

    if not LEDGER_PATH.exists():
        return {"status": "error", "message": f"ledger file not found: {LEDGER_PATH}"}

    try:
        data = json.loads(LEDGER_PATH.read_text())
    except Exception as exc:
        return {"status": "error", "message": f"could not parse {LEDGER_PATH} as JSON: {exc}"}

    key = f"{model}::{topo}"
    entry = data.get(key)
    if not isinstance(entry, dict):
        return {"status": "error", "message": f"key {key!r} not found in ledger", "available_keys": list(data.keys())}

    current_lifetime = entry.get("lifetime", {"in": 0, "out": 0, "draft": 0, "accepted": 0})

    new_lifetime = {
        "in": int(prompt_tokens),
        "out": int(gen_tokens),
        "draft": int(draft_tokens),
        "accepted": int(accepted_tokens),
    }

    if not force:
        for field in ("in", "out", "draft", "accepted"):
            if new_lifetime[field] < current_lifetime.get(field, 0):
                return {
                    "status": "error",
                    "message": (
                        f"new lifetime.{field}={new_lifetime[field]} is LESS than the currently "
                        f"recorded {current_lifetime.get(field, 0)}. Refusing to overwrite -- this "
                        f"usually means stale/transposed values. Pass force=True if this is "
                        f"genuinely intended."
                    ),
                }

    new_raw = {"p": prompt_tokens, "g": gen_tokens, "d": draft_tokens, "a": accepted_tokens}

    result = {
        "status": "success",
        "key": key,
        "auto_detected": detected is not None,
        "detected_serving_host": detected.get("serving_host") if detected else None,
        "current_lifetime": current_lifetime,
        "current_last_seen_raw": entry.get("last_seen_raw"),
        "new_lifetime": new_lifetime,
        "new_last_seen_raw": new_raw,
        "dry_run": dry_run,
        "backup_path": None,
    }

    if dry_run:
        return result

    backup_path = LEDGER_PATH.with_suffix(f".json.bak.{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    shutil.copy2(LEDGER_PATH, backup_path)
    result["backup_path"] = str(backup_path)

    entry["lifetime"] = new_lifetime
    entry["last_seen_raw"] = new_raw
    data[key] = entry
    LEDGER_PATH.write_text(json.dumps(data, indent=2))

    return result

def _discover_host_container(host: str, meta: dict) -> dict:
    ip = meta["ip"]
    user = None
    info = {
        "host": host, "ip": ip, "reachable": False,
        "active_container": "None", "container_state": "None", "loaded_model": "None",
        "is_crashed": False
    }

    res = run_ssh(ip, user, ["docker", "ps", "-a", "--format", "{{.Names}}::{{.State}}::{{.Image}}"], timeout=10)
    if res.returncode != 0:
        return info
    info["reachable"] = True

    lines = [l.strip() for l in res.stdout.strip().splitlines() if l.strip()]
    for line in lines:
        parts = line.split("::")
        if not parts:
            continue
        c_name = parts[0]
        c_state = parts[1] if len(parts) > 1 else "running"
        is_crashed = c_state.lower().startswith("exited") or "dead" in c_state.lower()
        
        if c_name not in [ContainerRole.STANDALONE, ContainerRole.HEAD, ContainerRole.WORKER]:
            continue

        info["active_container"] = c_name
        info["container_state"] = c_state.lower()
        info["is_crashed"] = is_crashed

        loaded_model = "None"
        inspect_res = run_ssh(ip, user, ["docker", "inspect", c_name, "--format", "{{json .Config.Cmd}}"], timeout=8)
        if inspect_res.returncode == 0 and "--model" in inspect_res.stdout:
            try:
                cmd_parts = json.loads(inspect_res.stdout.strip())
                if len(cmd_parts) >= 2 and cmd_parts[0] == "bash" and "-c" in cmd_parts:
                    bash_cmd = cmd_parts[-1]
                    model_match = re.search(r'--model\s+([^\s]+)', bash_cmd)
                    if model_match:
                        loaded_model = model_match.group(1).split("/")[-1]
                elif "--model" in cmd_parts:
                    idx = cmd_parts.index("--model")
                    if idx + 1 < len(cmd_parts):
                        loaded_model = cmd_parts[idx + 1].split("/")[-1]
            except Exception as exc:
                # Falling back to a placeholder here means active_model
                # (and downstream matched_key resolution, when
                # ACTIVE_DEPLOYMENT_STATE doesn't already have an exact
                # record for this host) silently degrades to the fuzzy
                # match's least reliable input. Worth knowing when the
                # `docker inspect` Cmd shape changes under us rather than
                # discovering it only once someone notices "Active
                # Container" on the dashboard.
                print(f"[!] _discover_host_container({host}): failed to parse inspected Cmd for model name - {exc}")
                loaded_model = "Active Container"
        else:
            ps_res = run_ssh(ip, user, ["docker", "exec", c_name, "ps", "aux"], timeout=10)
            if ps_res.returncode == 0 and "--model" in ps_res.stdout:
                try:
                    for part in ps_res.stdout.split():
                        if "/" in part and any(fam in part for fam in ["DeepSeek", "Qwen", "Llama", "model", "gemma", "Nemotron", "Muse", "Glimmer"]):
                            loaded_model = part.split("/")[-1]
                            break
                except Exception as exc:
                    print(f"[!] _discover_host_container({host}): failed to parse `ps aux` output for model name - {exc}")
                    loaded_model = "Active Container"
            else:
                loaded_model = "Active Container"

        info["loaded_model"] = loaded_model
        break

    return info

def _resolve_catalog_key(loaded_model: str, catalog_models: dict) -> str:
    """Map a raw served model name (HF basename pulled from --model, e.g.
    'DeepSeek-V4-Flash-0731-NVFP4') to its catalog/recipe key (filename
    stem, e.g. 'deepseek-v4-flash-0731-nvfp4'). Falls back to loaded_model
    itself if no catalog entry matches, so callers always get a usable key.

    Single source of truth: any code that keys a ledger, session tracker,
    or historical lookup by "the model" must resolve through here first,
    or its key will silently never join with enrich_catalog()'s l_key.
    """
    matched_key = loaded_model
    if catalog_models and isinstance(catalog_models, dict):
        for cat_key, cat_data in catalog_models.items():
            hf_path = cat_data.get("hf_path", "")
            if hf_path.endswith(loaded_model) or cat_key == loaded_model or loaded_model in hf_path:
                matched_key = cat_key
                break
    return matched_key

def _resolve_active_recipe(host: str, catalog_models: dict, loaded_model: str,
                            active_container: str = "None", require_active: bool = True) -> tuple:
    """
    Returns (catalog_key, config_hash, is_exact) for whatever recipe is
    actually running on `host`. Single source of truth for the "prefer
    ACTIVE_DEPLOYMENT_STATE's exact record, fall back to _resolve_catalog_key's
    fuzzy served-name match" resolution -- see ACTIVE_DEPLOYMENT_STATE's
    module comment for why the fuzzy match alone is ambiguous (two recipes
    can serve the identical checkpoint under different configs). Was
    previously duplicated three times (one per caller below) with subtly
    different gating each time; consolidated here so the gating logic only
    needs to be reasoned about once.

    config_hash is only ever non-None when is_exact is True. Callers that
    need to distinguish "this is the confirmed-active recipe" from "this is
    just our best guess" (e.g. deciding whether to report active_recipe_key
    to the dashboard at all) should check is_exact, not just catalog_key
    truthiness -- catalog_key is always a usable string either way.

    require_active=True (the default) only trusts ACTIVE_DEPLOYMENT_STATE
    when active_container != "None" -- guards against a stale record
    surviving an out-of-band `docker rm` (an operator on maestro, not
    through the orchestrator) that discovery would otherwise catch. Pass
    require_active=False when the caller has already independently
    confirmed the host is actively serving (e.g. via check_vllm_health()),
    so there's nothing further to gate on.
    """
    deployment = _load_active_deployment_state().get(host)
    if require_active and active_container == "None":
        deployment = None
    if deployment and deployment.get("catalog_key") in catalog_models:
        return deployment["catalog_key"], deployment.get("config_hash"), True
    return _resolve_catalog_key(loaded_model, catalog_models), None, False

def _finalize_host_status(host: str, meta: dict, info: dict, cluster_ready: bool, container_info: dict, serving_host: str, catalog_models: dict) -> tuple:
    ip = meta["ip"]
    user = None
    telemetry = get_lightweight_telemetry(ip, user)

    if not info.get("reachable"):
        return host, {
            "ip": ip, "docker_status": "UNREACHABLE", "container_name": "None",
            "container_state": "NONE", "active_model": "None", "model_status": "NONE",
            "eta_seconds": 0, "eta_display": "N/A", "telemetry": telemetry,
            "active_recipe_key": None, "active_config_hash": None
        }

    active_container = info["active_container"]
    container_state = info["container_state"]
    loaded_model = info["loaded_model"]
    is_crashed = info.get("is_crashed", False)

    head_crashed = False
    for h, cinfo in container_info.items():
        if HOSTS.get(h, {}).get("role") == "head" and cinfo.get("is_crashed"):
            head_crashed = True
            break

    # Prefer the recorded ACTIVE_DEPLOYMENT_STATE (exact, hash-based) over
    # the fuzzy served-name match -- see _resolve_active_recipe()'s
    # docstring.
    matched_key, active_config_hash, is_exact_match = _resolve_active_recipe(
        host, catalog_models, loaded_model, active_container=active_container
    )

    eta_seconds = 0
    eta_display = "Ready" if cluster_ready else "N/A"
    topo_key = "2_node" if active_container in [ContainerRole.HEAD, ContainerRole.WORKER] else "1_node"

    if is_crashed:
        model_status = f"CRASHED ({container_state.upper()})"
        eta_display = "Check Docker Logs"
        eta_seconds = 0
        # A crashed load is frequently the MORE informative sample -- it is
        # the one someone will want to explain later, and today its logs
        # vanish the moment the container is torn down. Same once-per-
        # container-start guard as the READY path, keyed on StartedAt so a
        # container that crashes, is redeployed, and crashes again produces
        # two distinct archives.
        crash_time_res = run_ssh(ip, user, ["docker", "inspect", active_container,
                                            "--format", "{{.State.StartedAt}}"], timeout=5)
        if crash_time_res.returncode == 0 and crash_time_res.stdout.strip():
            crash_start_ts = parse_iso_time(crash_time_res.stdout.strip())
            crash_key = f"crash:{host}:{active_container}"
            with _RECORDED_LOAD_STARTS_LOCK:
                crash_already = _RECORDED_LOAD_STARTS.get(crash_key) == crash_start_ts
                if not crash_already:
                    _RECORDED_LOAD_STARTS[crash_key] = crash_start_ts
            if not crash_already:
                archive_run_log(
                    host=host, ip=ip, user=user, container=active_container,
                    model_key=matched_key, topo_key=topo_key,
                    started_ts=crash_start_ts, outcome="crashed",
                    elapsed_sec=int(time.time() - crash_start_ts),
                )
    elif active_container == ContainerRole.WORKER and head_crashed:
        model_status = "ORPHANED (HEAD CRASHED)"
        eta_display = "Requires Teardown"
        eta_seconds = 0
    elif active_container != "None" and container_state == "running":
        if cluster_ready and host == serving_host:
            model_status = "READY"
            time_res = run_ssh(ip, user, ["docker", "inspect", active_container, "--format", "{{.State.StartedAt}}"], timeout=5)
            if time_res.returncode == 0 and time_res.stdout.strip():
                start_ts = parse_iso_time(time_res.stdout.strip())
                record_key = f"{host}:{active_container}"
                with _RECORDED_LOAD_STARTS_LOCK:
                    already_recorded = _RECORDED_LOAD_STARTS.get(record_key) == start_ts
                    if not already_recorded:
                        _RECORDED_LOAD_STARTS[record_key] = start_ts
                if not already_recorded:
                    elapsed = int(time.time() - start_ts)
                    log_res = run_ssh(ip, user, ["docker", "logs", "--tail", "5000", active_container], timeout=15)
                    logs_lower = (log_res.stdout + log_res.stderr).lower()
                    if "downloading" in logs_lower or "fetching" in logs_lower:
                        load_type = "downloaded"
                    elif "tilelang completes" in logs_lower or "jit compilation" in logs_lower or "compiling" in logs_lower:
                        load_type = "compiled"
                    else:
                        load_type = "cached"
                        
                    record_load_time(matched_key, topo_key, elapsed, load_type)

                    # Archive the full log alongside the timing. This runs
                    # inside the same already_recorded guard, so it fires
                    # once per container start, not once per 4s poll.
                    # archive_run_log() is best-effort and swallows its own
                    # failures -- see common/runlog.py -- so nothing here
                    # needs guarding beyond that. The classification above
                    # is passed as `load_type` for context only: it is the
                    # substring-scan guess this archive exists to let a
                    # later reader check, not a fact.
                    archive_run_log(
                        host=host, ip=ip, user=user, container=active_container,
                        model_key=matched_key, topo_key=topo_key,
                        started_ts=start_ts, outcome="ready",
                        load_type=load_type, elapsed_sec=elapsed,
                    )
        elif cluster_ready:
            model_status = "READY"
        else:
            model_status = detect_model_stage(ip, user, active_container)

            if model_status.startswith("CRASHED"):
                eta_display = "Check Docker Logs"
                eta_seconds = 0
            else:
                if isinstance(telemetry.get("gpu_util_pct"), int) and telemetry["gpu_util_pct"] > 75:
                    model_status = "WARMING UP (CUDA GRAPHS)"

                time_res = run_ssh(ip, user, ["docker", "inspect", active_container, "--format", "{{.State.StartedAt}}"], timeout=5)
                if time_res.returncode == 0 and time_res.stdout.strip():
                    start_ts = parse_iso_time(time_res.stdout.strip())
                    elapsed = int(time.time() - start_ts)

                    current_load_type = "cached"
                    if "DOWNLOADING" in model_status:
                        current_load_type = "downloaded"
                    elif "COMPILING" in model_status:
                        current_load_type = "compiled"

                    est_total, has_history = get_estimated_load_time(matched_key, topo_key, current_load_type)
                    remaining = est_total - elapsed
                    eta_seconds = max(0, remaining)

                    if remaining > 0:
                        suffix = "" if has_history else " (Initial run - no history)"
                        eta_display = f"~{remaining}s remaining{suffix}"
                    else:
                        overrun = elapsed - est_total
                        if has_history:
                            eta_display = f"Finishing startup (+{overrun}s over est.)"
                        else:
                            eta_display = f"Loading... ({elapsed}s elapsed - no historic data)"
    elif active_container != "None":
        model_status = f"STOPPED ({container_state.upper()})"
        eta_display = "N/A"
    else:
        model_status = "NONE"
        eta_display = "N/A"

    return host, {
        "ip": ip,
        "docker_status": "CRASHED" if is_crashed else "ONLINE",
        "container_name": active_container,
        "container_state": container_state.upper() if active_container != "None" else "NONE",
        "active_model": loaded_model,
        "model_status": model_status,
        "eta_seconds": eta_seconds,
        "eta_display": eta_display,
        "telemetry": telemetry,
        # Exact catalog key + config_hash for the recipe actually running on
        # this host, per ACTIVE_DEPLOYMENT_STATE above -- this is what the
        # dashboard should key its model-select sync off of, not active_model
        # (which is just the served checkpoint name and is ambiguous between
        # recipes that happen to serve the same checkpoint).
        "active_recipe_key": matched_key if is_exact_match else None,
        "active_config_hash": active_config_hash
    }

def _collect_bounded(futures: list, host_order: list, deadline: float, label: str) -> dict:
    results = {}
    for host, fut in zip(host_order, futures):
        remaining = max(0.0, deadline - time.monotonic())
        try:
            results[host] = fut.result(timeout=remaining)
        except Exception:
            print(f"[!] {label} timed out or failed for host '{host}' - reporting unreachable.")
            results[host] = None
    return results

def _compute_cluster_status_impl() -> dict:
    call_deadline = time.monotonic() + STATUS_CALL_TIMEOUT_SEC

    offline_mode = False
    if NETWORK_STATE_FILE.exists():
        try:
            offline_mode = "OFFLINE" in NETWORK_STATE_FILE.read_text().strip()
        except Exception:
            pass

    host_order = list(HOSTS.keys())
    futures_phase1 = [WORKER_POOL.submit(_discover_host_container, host, meta) for host, meta in HOSTS.items()]
    phase1_results = _collect_bounded(futures_phase1, host_order, call_deadline, "Phase 1 discovery")
    container_info = {
        host: (res if res is not None else {"host": host, "reachable": False})
        for host, res in phase1_results.items()
    }

    serving_host = PRIMARY_HOST
    for host in HOSTS:
        if container_info.get(host, {}).get("active_container") in (ContainerRole.STANDALONE, ContainerRole.HEAD):
            serving_host = host
            break
    serving_ip = HOSTS[serving_host]["ip"]

    cluster_ready = check_vllm_health(serving_ip)
    vllm_metrics = get_vllm_metrics(serving_ip) if cluster_ready else {"tps": 0.0, "running_requests": 0, "waiting_requests": 0}

    # catalog_models must load before we resolve the session-tracker key --
    # SESSION_TRACKER commits lifetime stats keyed on the catalog/recipe key
    # (matching enrich_catalog()'s l_key), not the raw served HF basename.
    # Previously this used the raw loaded_model name directly, which never
    # matched the catalog key, so lifetime_in/lifetime_out silently stayed
    # at 0 in the dashboard even though sessions were being tracked.
    catalog_models = load_model_catalog().get("catalog", {}).get("models", {})

    raw_loaded_model = container_info.get(serving_host, {}).get("loaded_model", "Unknown")
    serving_active_container = container_info.get(serving_host, {}).get("active_container", "None")
    matched_model, _, _ = _resolve_active_recipe(
        serving_host, catalog_models, raw_loaded_model, active_container=serving_active_container
    )
    topo = "2_node" if len([h for h, i in container_info.items() if i.get("active_container") != "None"]) > 1 else "1_node"
    if cluster_ready:
        SESSION_TRACKER.update(vllm_metrics, matched_model, topo)

    # Consume any pending launch-confirmation record. See PENDING_LAUNCH_STATE's
    # comment for why this happens here (in the already-running status poll)
    # rather than synchronously in execute_deployment(). This must never
    # raise or block status polling -- wrapped defensively.
    try:
        with PENDING_LAUNCH_LOCK:
            pending = PENDING_LAUNCH_STATE.get("pending")
        if pending is not None:
            age = time.time() - pending["started_ts"]
            if age > PENDING_LAUNCH_STALE_SEC:
                with PENDING_LAUNCH_LOCK:
                    if PENDING_LAUNCH_STATE.get("pending") is pending:
                        PENDING_LAUNCH_STATE["pending"] = None
            elif cluster_ready and pending["model"] == matched_model and pending["topo_key"] == topo:
                _record_launch_success(pending["model"], pending["topo_key"], pending["config_hash"])
                with PENDING_LAUNCH_LOCK:
                    if PENDING_LAUNCH_STATE.get("pending") is pending:
                        PENDING_LAUNCH_STATE["pending"] = None
    except Exception as exc:
        # This block should never actually raise -- it's arithmetic and
        # dict lookups on data this same process wrote. If it does, that's
        # a genuine bug (e.g. a future refactor of PENDING_LAUNCH_STATE's
        # shape) and silently eating it here would just mean launch-success
        # telemetry quietly stops recording with zero indication why.
        print(f"[!] _compute_cluster_status_impl: pending launch-confirmation consumption failed unexpectedly - {exc}")

    with BENCHMARK_STATE_LOCK:
        is_benchmarking = BENCHMARK_STATE["running"]
        benchmark_msg = BENCHMARK_STATE["message"]

    with TEARDOWN_STATE_LOCK:
        is_tearing_down = TEARDOWN_STATE["running"]
        teardown_phase = TEARDOWN_STATE["phase"]
        teardown_msg = TEARDOWN_STATE["message"]

    status_data = {
        "orchestrator_version": ORCHESTRATOR_VERSION,
        # Emit real, unambiguous UTC. Previously this used naive
        # datetime.datetime.now() (which, since this container's system
        # clock is UTC, was already correct-in-value) with a hardcoded
        # " EST" string suffix -- the value was never Eastern time, just
        # mislabeled. That mislabel is the likely cause of the dashboard
        # showing a ~5h-into-the-future clock: something downstream
        # apparently read "EST", assumed it needed EST->UTC conversion
        # (+5h, and DST-unaware even if it hadn't been a mislabel to begin
        # with), and applied that offset to data that didn't need it.
        # Explicit tz-aware UTC with an unambiguous "UTC" suffix removes
        # any excuse for a consumer to apply its own offset "correction".
        "server_time": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "network_mode": "Working in OFFLINE mode" if offline_mode else "Working in ONLINE mode",
        "cluster_ready": cluster_ready,
        "serving_host": serving_host,
        "system_tps": vllm_metrics["tps"],
        "running_requests": vllm_metrics["running_requests"],
        "waiting_requests": vllm_metrics["waiting_requests"],
        "session_stats": SESSION_TRACKER.get_live_stats(),
        "is_benchmarking": is_benchmarking,
        "benchmark_message": benchmark_msg,
        "is_tearing_down": is_tearing_down,
        "teardown_phase": teardown_phase,
        "teardown_message": teardown_msg,
        "hosts": {}
    }

    futures_phase2 = [
        WORKER_POOL.submit(_finalize_host_status, host, meta, container_info.get(host, {}), cluster_ready, container_info, serving_host, catalog_models)
        for host, meta in HOSTS.items()
    ]
    phase2_results = _collect_bounded(futures_phase2, host_order, call_deadline, "Phase 2 finalize")

    for host in HOSTS:
        result = phase2_results.get(host)
        if result is None:
            status_data["hosts"][host] = {
                "ip": HOSTS[host]["ip"], "docker_status": "TIMEOUT", "container_name": "None",
                "container_state": "NONE", "active_model": "None", "model_status": "NONE",
                "eta_seconds": 0, "eta_display": "N/A", "telemetry": {},
                "active_recipe_key": None, "active_config_hash": None
            }
        else:
            _, host_status = result
            status_data["hosts"][host] = host_status

    # Strictly guarded worker state mirroring
    head_s = status_data["hosts"].get(PRIMARY_HOST)
    worker_s = status_data["hosts"].get(SECONDARY_HOST)
    if head_s and worker_s:
        head_is_active = (head_s["container_state"] == "RUNNING" and 
                          head_s["active_model"] != "None" and 
                          not head_s["model_status"].startswith("CRASHED"))
        
        worker_is_healthy_runner = (worker_s["container_state"] == "RUNNING" and 
                                    not worker_s["model_status"].startswith("CRASHED") and 
                                    not worker_s["model_status"].startswith("ORPHANED"))
        
        if head_is_active and worker_is_healthy_runner:
            worker_s["active_model"] = head_s["active_model"]
            worker_s["model_status"] = head_s["model_status"]
            worker_s["eta_seconds"] = head_s["eta_seconds"]
            worker_s["eta_display"] = head_s["eta_display"]
            # Worker already gets its own ACTIVE_DEPLOYMENT_STATE entry
            # written directly at deploy time (execute_deployment() sets it
            # for every host in target_hosts, head and worker alike), so
            # this mirror is a defensive fallback only -- covers the case
            # where worker's own container discovery didn't resolve a
            # recipe key for some reason but head's did.
            if not worker_s.get("active_recipe_key"):
                worker_s["active_recipe_key"] = head_s["active_recipe_key"]
                worker_s["active_config_hash"] = head_s["active_config_hash"]

    return status_data

def get_cluster_status() -> dict:
    global _STATUS_INFLIGHT, _STATUS_CACHE, _STATUS_CACHE_TS

    with _STATUS_LOCK:
        now = time.monotonic()
        if _STATUS_CACHE is not None and (now - _STATUS_CACHE_TS) < _STATUS_CACHE_TTL_SEC:
            return _STATUS_CACHE

        if _STATUS_INFLIGHT is None or _STATUS_INFLIGHT.done():
            _STATUS_INFLIGHT = WORKER_POOL.submit(_compute_cluster_status_impl)
        inflight = _STATUS_INFLIGHT

    try:
        result = inflight.result(timeout=STATUS_CALL_TIMEOUT_SEC + 2)
    except Exception as e:
        with _STATUS_LOCK:
            if _STATUS_CACHE is not None:
                # Serving a stale snapshot rather than propagating the
                # failure. This is deliberate (a transient blip shouldn't
                # 500 the dashboard), but silently doing this indefinitely
                # is exactly what let a wedged WORKER_POOL serve the same
                # frozen status for hours undetected (2026-08-25 and
                # 2026-08-27 incidents). Stamp the response with how stale
                # it actually is so a consumer -- dashboard banner, alert,
                # whatever -- can tell "slow poll" apart from "this has
                # been dead for hours" instead of both looking identically
                # fresh.
                stale_for = round(time.monotonic() - _STATUS_CACHE_TS, 1)
                print(f"[!] get_cluster_status(): in-flight computation failed or timed out ({e}); "
                      f"serving cache that is {stale_for}s stale.")
                stale_result = dict(_STATUS_CACHE)
                stale_result["stale"] = True
                stale_result["stale_for_seconds"] = stale_for
                return stale_result
        raise

    result["stale"] = False
    result["stale_for_seconds"] = 0.0

    with _STATUS_LOCK:
        _STATUS_CACHE = result
        _STATUS_CACHE_TS = time.monotonic()

    return result

def enrich_catalog(catalog_dict: dict) -> dict:
    """Fail-soft catalog enricher with ultra-permissive MTP and model metadata checks."""
    models = catalog_dict.get("catalog", {}).get("models", {})
    if not isinstance(models, dict):
        return catalog_dict

    ledger_data = _read_json_state(LEDGER_PATH) or {}

    ledger_tps = {}
    if BENCHMARK_LEDGER_PATH.exists():
        try:
            for line in BENCHMARK_LEDGER_PATH.read_text().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 3:
                    m_name, tps_val = parts[1], parts[2]
                    try: ledger_tps[m_name] = f"{round(float(tps_val))} tps"
                    except ValueError: pass
        except Exception: pass

    for m_key, m_data in models.items():
        if not isinstance(m_data, dict): continue

        hf_path = str(m_data.get("hf_path", "")).lower()
        m_key_lower = m_key.lower()

        if "nvfp4" in m_key_lower or "nvfp4" in hf_path or "fp4" in m_key_lower: precision_label = "NVFP4"
        elif "fp8" in m_key_lower or "fp8" in hf_path: precision_label = "FP8"
        elif "int4" in m_key_lower or "awq" in m_key_lower: precision_label = "INT4"
        else: precision_label = "BF16"

        m_data["precision_label"] = precision_label

        topologies = m_data.get("topologies", {})
        if isinstance(topologies, dict):
            for t_key, t_data in topologies.items():
                if not isinstance(t_data, dict): continue

                vllm_args = str(t_data.get("vllm_args", ""))

                seq_match = re.search(r'--max-num-seqs\s+(\d+)', vllm_args)
                t_data["max_num_seqs"] = seq_match.group(1) if seq_match else "Uncapped"

                kv_match = re.search(r'--kv-cache-dtype\s+([^\s]+)', vllm_args)
                t_data["kv_dtype"] = (kv_match.group(1).upper() + " KV") if kv_match else "AUTO KV"
                
                l_key = f"{m_key}::{t_key}"
                ledger_entry = ledger_data.get(l_key, {})
                lt_stats = ledger_entry.get("lifetime", {})
                d_tok = lt_stats.get("draft", 0)
                a_tok = lt_stats.get("accepted", 0)

                # "Last launched successfully" is looked up by config_hash
                # (injected into t_data by build_catalog_response(), see
                # compute_config_hash()'s docstring), not just m_key/t_key --
                # so editing a recipe's flags/gpu_util/etc. correctly shows
                # as untested again rather than reporting stale success for
                # a configuration that was never actually the one launched.
                launch_history = ledger_entry.get("launch_history", {})
                cfg_hash = t_data.get("config_hash")
                launch_record = launch_history.get(cfg_hash) if cfg_hash else None
                t_data["last_launch_success_ts"] = launch_record["last_success_ts"] if launch_record else None
                t_data["last_launch_success_count"] = launch_record["count"] if launch_record else 0

                # mtp_enabled reflects ONLY whether the CURRENT recipe
                # actually configures speculative decoding. Deliberately NOT
                # a name-based heuristic ("flash"/"deepseek-v4" in the model
                # key or hf_path) and deliberately NOT "have we ever seen
                # nonzero draft/accepted counters for this model+topo key" --
                # both of those keep reporting true forever after a recipe
                # is edited to remove spec decode entirely, which is exactly
                # what happened here: deepseek-v4-flash-0731-nvfp4 ran dspark
                # earlier, it was found broken (near-zero acceptance), and
                # the recipe now carries no --speculative-config at all --
                # but the name heuristic still matches "flash"/"deepseek-v4"
                # regardless, and the old draft/accepted counters from that
                # earlier dspark run are still sitting in the ledger under
                # this same model::topo key (that ledger entry isn't scoped
                # by config_hash the way launch_history now is), so d_tok > 0
                # kept the panel alive independently even if the name check
                # were removed. "--speculative-config" is the literal vLLM
                # flag this orchestrator emits for every spec-decode recipe
                # (see _execute_deployment_impl's container_args
                # construction) -- checking for its presence directly is a
                # precise, unambiguous signal tied to what's actually
                # configured right now, not a fuzzy/stale proxy for it.
                t_data["mtp_enabled"] = "--speculative-config" in vllm_args

                est_c, has_c = get_estimated_load_time(m_key, t_key, "cached")
                est_j, has_j = get_estimated_load_time(m_key, t_key, "compiled")
                
                if has_j and est_j > (est_c + 60): t_data["avg_load_display"] = f"~{est_c}s (C) / ~{est_j}s (JIT)"
                else: t_data["avg_load_display"] = f"~{est_c}s" if has_c else "N/A"
                    
                # NOTE: ledger_tps is keyed on whatever benchmark.py logged in
                # the "Model" column. Historically that was the raw served
                # HF basename, which never matches m_key (the catalog/recipe
                # key) -- this lookup was silently always "N/A". benchmark.py
                # now accepts --model-key so new rows key on m_key directly;
                # old rows in benchmark_ledger.csv predate that and won't
                # match until the ledger is rotated or backfilled.
                t_data["historical_tps"] = ledger_tps.get(m_key, "N/A")
                
                t_data["lifetime_in"] = lt_stats.get("in", 0)
                t_data["lifetime_out"] = lt_stats.get("out", 0)
                t_data["hist_mtp_rate"] = round((a_tok / d_tok * 100), 1) if d_tok > 0 else 0.0

    return catalog_dict

def _load_model_catalog_legacy() -> dict:
    if not MODELS_YAML_PATH.exists(): return {"catalog": {"models": {}}}
    try:
        with open(MODELS_YAML_PATH, "r") as f: config = yaml.safe_load(f) or {}
        global_hf = config.get('GLOBAL_HF_HUB_OFFLINE', 0)
        global_tf = config.get('GLOBAL_TRANSFORMERS_OFFLINE', 0)
        models = config.get('models', {})
        if isinstance(models, dict):
            for model_name, model_data in models.items():
                if not isinstance(model_data, dict): continue
                topologies = model_data.get('topologies', {})
                if isinstance(topologies, dict):
                    for topo_name, topo_data in topologies.items():
                        if not isinstance(topo_data, dict): continue
                        if 'env_vars' not in topo_data: topo_data['env_vars'] = []
                        if global_hf == 1:
                            topo_data['env_vars'] = [env for env in topo_data['env_vars'] if not env.startswith('HF_HUB_OFFLINE=')]
                            topo_data['env_vars'].append('HF_HUB_OFFLINE=1')
                        if global_tf == 1:
                            topo_data['env_vars'] = [env for env in topo_data['env_vars'] if not env.startswith('TRANSFORMERS_OFFLINE=')]
                            topo_data['env_vars'].append('TRANSFORMERS_OFFLINE=1')
        return {"catalog": config}
    except Exception as e:
        return {"error": str(e), "catalog": {"models": {}}}

def _sync_config_registry() -> None:
    """
    Maintain config_registry.json: an append-only decoder ring mapping every
    config_hash this cluster has ever computed back to the payload it was
    computed from.

    A config_hash is a good answer to "has THIS configuration launched
    successfully" and a bad anchor for anything else. It is not a complete
    description of what ran (mutable image tags like `:latest` move
    underneath it; mods payload *contents* can change while the mod's name
    doesn't), it is schema-versioned and so goes un-joinable on a bump, and
    it is deliberately non-unique -- two recipes with identical configs are
    supposed to collide. This registry does not fix any of that. It makes
    the hash *decodable*, which is a different and achievable goal:

      - Diffing two recipe variants becomes reading two payloads rather
        than guessing from filenames.
      - A schema bump stops being destructive: hashes nothing computes
        anymore stay readable, so historical launch_history and run records
        can still be explained after the fact.
      - Genuine collisions surface the day they appear via the `sources`
        list, instead of being reverse-engineered out of the ledger later
        -- and, just as usefully, a RENAME stops looking like one. The
        ledger keys on filename stem and never deletes, so a renamed
        recipe leaves its old name's launch_history behind carrying the
        same hash as the new name; read from the ledger alone that is
        indistinguishable from two live recipes colliding, and was in
        fact misread that way. This registry is rebuilt from recipes that
        currently exist, so the old name simply drops out.

    APPEND-ONLY, deliberately. Entries are never removed and an existing
    entry's payload is never rewritten -- a hash is a pure function of its
    payload, so a hash already on disk cannot legitimately decode to
    something else, and letting it be overwritten would destroy exactly the
    history this exists to keep. `sources` is refreshed (recipe files get
    renamed, added, deleted) but `first_seen` is preserved.

    Best-effort throughout: this is a traceability aid, and a failure here
    must never break catalog load. Silent on error, same contract as
    _write_json_state().
    """
    try:
        entries = build_config_registry_entries(load_recipes())
    except Exception:
        return
    if not entries:
        return

    registry = _read_json_state(CONFIG_REGISTRY_PATH) or {}
    now = time.time()
    dirty = False

    for digest, entry in entries.items():
        existing = registry.get(digest)
        if existing is None:
            registry[digest] = {
                "first_seen": now,
                "last_seen": now,
                "_schema": entry["_schema"],
                "payload": entry["payload"],
                "sources": entry["sources"],
            }
            dirty = True
            continue
        # Payload is never rewritten -- see docstring. Only the mutable
        # bookkeeping around it moves.
        if existing.get("sources") != entry["sources"]:
            existing["sources"] = entry["sources"]
            dirty = True
        existing["last_seen"] = now
        dirty = True

    if dirty:
        _write_json_state(CONFIG_REGISTRY_PATH, registry)


def load_model_catalog() -> dict:
    if os.environ.get("USE_LEGACY_CATALOG") == "1": raw_cat = _load_model_catalog_legacy()
    else: raw_cat = build_catalog_response()
    # Registry sync rides the catalog path so every config that exists gets
    # recorded, not just ones that have launched -- an unlaunched config is
    # exactly the one whose payload you want when working out why it never
    # ran. load_recipes() is lru_cached, so the common case is a cache hit
    # plus a dict comparison.
    _sync_config_registry()
    return enrich_catalog(raw_cat)

# In-flight JIT compilation shells out to nvcc/ptxas/cicc as child
# subprocesses that write cache artifacts non-atomically. SIGKILL (-9) on
# the parent does not propagate to those children -- it can't be caught or
# forwarded at all -- so a hard-kill mid-compile can leave an orphaned
# compiler process still writing into the persistent, shared cache
# directory with nobody supervising it, or leave a half-written artifact
# at the path the loader treats as a cache hit on the next load. This
# grace period gives an in-flight compile a real chance to finish and (if
# the library does it right) atomically rename its output into place
# before anything gets force-killed. It does not guarantee safety for a
# compile that's still running past the window -- see docs/ROADMAP.md.
TEARDOWN_GRACE_SEC = 20

def _teardown_host_processes(ip: str) -> None:
    """
    Phase 1a of teardown for one host: SIGTERM any stray vllm/ray
    processes running directly on the bare host (outside any container),
    give them TEARDOWN_GRACE_SEC, then escalate to -9 for stragglers.

    IMPORTANT LIMITATION: none of our docker run invocations set
    --pid=host, so every container gets its own isolated PID namespace --
    this means `ps aux` run here, over SSH directly against the host, has
    NO visibility into processes running inside a container at all. This
    step only ever catches a genuinely bare-metal stray process (e.g.
    something run manually outside the normal deploy path during
    debugging) -- it is NOT what reaches the vLLM engine or Ray inside a
    container. See _teardown_host_container_internals for what actually
    does that. Kept as a harmless, cheap safety net for the rare bare-
    metal case, not because it's load-bearing for the common one.
    """
    cleanup_cmd = ["bash", "-c", (
        "PIDS=$(ps aux | grep -E 'vllm|ray' | grep -v 'dgx-orchestrator' | awk '{print $2}'); "
        "if [ -n \"$PIDS\" ]; then "
        "echo \"$PIDS\" | xargs -r sudo kill -TERM 2>/dev/null || true; "
        f"sleep {TEARDOWN_GRACE_SEC}; "
        "echo \"$PIDS\" | xargs -r sudo kill -9 2>/dev/null || true; "
        "fi"
    )]
    run_ssh(ip, None, cleanup_cmd, timeout=TEARDOWN_GRACE_SEC + 15)

def _teardown_host_container_internals(ip: str) -> None:
    """
    Phase 1b of teardown for one host: reaches INSIDE each container via
    `docker exec` -- the only thing that can actually see and signal
    container-internal processes, given no --pid=host sharing (see
    _teardown_host_processes above) -- to gracefully stop the vLLM engine
    and Ray before the container itself is ever stopped or removed.

    This closes two real gaps:
    1. In a 2-node Ray deploy, the vLLM engine runs as a separate
       `docker exec -d` process, detached from the container's own PID 1
       (`ray start --block`). `docker stop`'s SIGTERM only reaches PID 1
       -- the engine itself was previously never signaled at all, only
       ever killed via the abrupt kernel-level namespace teardown at
       `docker rm -f` time.
    2. These containers run with --ipc=host, so Ray's shared-memory-backed
       plasma object store lives in the HOST's own IPC namespace, not an
       isolated per-container one. A process killed abruptly rather than
       shut down cleanly can leak shared memory that persists on the host
       indefinitely -- it isn't reclaimed the way ordinary process memory
       is, it has to be explicitly unlinked, which only happens on a
       clean shutdown. `ray stop` is Ray's own designed command for this,
       and a better tool than a generic SIGTERM for releasing Ray's own
       IPC allocations correctly.

    Applied uniformly to all three container role names on every host --
    harmless no-ops for whichever ones don't exist. The 1-node path's own
    container genuinely IS PID 1's child there, so this is redundant-but-
    safe in that case specifically, not load-bearing the way it is for a
    2-node Ray deploy.
    """
    for role in (ContainerRole.STANDALONE, ContainerRole.HEAD, ContainerRole.WORKER):
        # Graceful pass.
        run_ssh(ip, None, ["docker", "exec", role, "pkill", "-TERM", "-f", "vllm.entrypoints.openai.api_server"], timeout=10)
        run_ssh(ip, None, ["docker", "exec", role, "ray", "stop"], timeout=TEARDOWN_GRACE_SEC + 5)
        # Escalation pass -- whatever's still alive gets forced.
        run_ssh(ip, None, ["docker", "exec", role, "pkill", "-9", "-f", "vllm.entrypoints.openai.api_server"], timeout=10)
        run_ssh(ip, None, ["docker", "exec", role, "ray", "stop", "--force"], timeout=15)

def _teardown_host_containers(ip: str) -> None:
    """Phase 2: graceful docker stop (SIGTERM + grace) for every role that
    might exist on this host. Errors here (e.g. container doesn't exist)
    are expected and ignored -- _teardown_host_rm below is the actual
    cleanup guarantee."""
    for role in (ContainerRole.STANDALONE, ContainerRole.HEAD, ContainerRole.WORKER):
        run_ssh(ip, None, ["docker", "stop", "--time", str(TEARDOWN_GRACE_SEC), role],
                timeout=TEARDOWN_GRACE_SEC + 10)

def _teardown_host_rm(ip: str):
    """Phase 3: final rm -f as a backstop for anything that ignored SIGTERM
    and docker stop's own escalation. By this point it's had a real grace
    period, so this is a safety net for genuinely wedged containers, not
    the primary kill mechanism."""
    return run_ssh(ip, None, ["docker", "rm", "-f", ContainerRole.STANDALONE, ContainerRole.HEAD, ContainerRole.WORKER], timeout=15)

def _set_teardown_state(running: bool, phase: str, message: str, mark_last_run: bool = False):
    with TEARDOWN_STATE_LOCK:
        TEARDOWN_STATE["running"] = running
        TEARDOWN_STATE["phase"] = phase
        TEARDOWN_STATE["message"] = message
        if mark_last_run:
            TEARDOWN_STATE["last_run"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def _teardown_results_are_clean(results: dict, hosts_to_clean: list) -> bool:
    """
    True only if every host we set out to tear down has a recorded result
    and none of them indicate a failure (results[h] is either "Purged",
    or that plus a shm-sweep suffix, or an "Error: ..." string -- see the
    "removing" phase above). An empty or partial `results` -- e.g. an
    exception raised before the "removing" phase ever populated it --
    counts as NOT clean: treating "we don't actually know what happened"
    as success is the exact bug this exists to close. See
    _execute_teardown_impl's `finally` block and api_teardown() for the
    two places this matters: the live teardown_message shown while
    polling, and whether /api/teardown returns an error status the
    dashboard's existing error-toast handling can react to.
    """
    if not hosts_to_clean:
        return True
    return all(
        h in results and not str(results[h]).lower().startswith("error")
        for h in hosts_to_clean
    )

def _execute_teardown_impl(target_hosts: list = None) -> dict:
    """
    Runs each teardown phase across ALL target hosts CONCURRENTLY, not one
    host fully torn down before the next starts.

    This matters specifically for a 2-node deploy: if head and worker are
    torn down sequentially with real grace periods (as an earlier version
    of this function did), head can finish its entire ~20-40s graceful
    shutdown and be fully gone before worker's teardown even begins.
    Worker spends that whole window still alive and still NCCL/Ray-
    connected to a rank-0 head that just vanished out from under it mid-
    collective-op -- observed on the dashboard as the worker going
    "confused" and then crashing on its own, well before we ever touched
    it. Running each phase across all hosts in parallel means head and
    worker are signaled together and come down together, closing that
    window.

    Also writes live progress into TEARDOWN_STATE as it moves through
    phases -- see the module comment above that dict for why. This runs
    unconditionally, whether called from the standalone teardown endpoint
    or from inside a deploy's own pre-deploy teardown, so both get the same
    visibility on the dashboard.

    The signaling phase now also reaches INSIDE each container via
    _teardown_host_container_internals (docker exec), not just the
    host's own (largely inert -- see that function's docstring) process
    table. And a new "sweeping" phase runs after removal to clear
    orphaned (nattch==0) SysV shared memory segments -- see
    sweep_ipc_orphans for why this is safe to do unconditionally. Together
    these are what make "clean slate on every deploy" an actual guarantee
    rather than a hope: _execute_deployment_impl calls this same function
    as its own pre-deploy step, so every deploy gets it too, not just a
    manually-triggered teardown.
    """
    results = {}
    hosts_to_clean = [h for h in (target_hosts if target_hosts else list(HOSTS.keys())) if h in HOSTS]
    ips = {h: HOSTS[h]["ip"] for h in hosts_to_clean}
    host_list = ", ".join(hosts_to_clean) or "no hosts"

    try:
        _set_teardown_state(True, "signaling",
                             f"Signaling processes on {host_list} (SIGTERM, up to {TEARDOWN_GRACE_SEC}s grace)...")
        # Bounded waits below, sized to each phase's own known worst case
        # (sum of its sequential run_ssh timeouts -- see each function's
        # docstring), not unbounded .result(). A genuinely wedged
        # container (e.g. left in a bad state by an OOM kill or a hung
        # engine) can make these leaf functions slow, but they were
        # already individually timeout-bounded at the run_ssh layer; nothing
        # here previously stopped an orchestration-level wait from blocking
        # longer than that anyway, and left no defense if a future change
        # to one of these functions ever removed that bound by accident.
        # Confirmed by direct test: an unbounded .result() here, combined
        # with a genuinely stuck sub-operation, starves get_cluster_status()
        # of the WORKER_POOL threads it needs -- dashboard status freezes
        # for as long as the stuck operation runs.
        proc_futures = {h: WORKER_POOL.submit(_teardown_host_processes, ip) for h, ip in ips.items()}
        internal_futures = {h: WORKER_POOL.submit(_teardown_host_container_internals, ip) for h, ip in ips.items()}
        _collect_bounded(list(proc_futures.values()), list(proc_futures.keys()),
                          time.monotonic() + TEARDOWN_GRACE_SEC + 25, "Teardown: process signaling")
        _collect_bounded(list(internal_futures.values()), list(internal_futures.keys()),
                          time.monotonic() + 3 * (TEARDOWN_GRACE_SEC + 40) + 10, "Teardown: container-internal signaling")

        _set_teardown_state(True, "stopping",
                             f"Gracefully stopping containers on {host_list} (up to {TEARDOWN_GRACE_SEC}s)...")
        stop_futures = {h: WORKER_POOL.submit(_teardown_host_containers, ip) for h, ip in ips.items()}
        _collect_bounded(list(stop_futures.values()), list(stop_futures.keys()),
                          time.monotonic() + 3 * (TEARDOWN_GRACE_SEC + 10) + 10, "Teardown: docker stop")

        _set_teardown_state(True, "removing", f"Removing containers on {host_list}...")
        rm_futures = {h: WORKER_POOL.submit(_teardown_host_rm, ip) for h, ip in ips.items()}
        rm_results = _collect_bounded(list(rm_futures.values()), list(rm_futures.keys()),
                                       time.monotonic() + 30, "Teardown: container removal")
        for h in hosts_to_clean:
            res = rm_results.get(h)
            if res is None:
                results[h] = "Error: docker rm timed out -- host may still have stale containers, check manually."
            else:
                results[h] = "Purged" if res.returncode == 0 else f"Error: {res.stderr.strip()}"

        _set_teardown_state(True, "sweeping", f"Sweeping orphaned shared memory on {host_list}...")
        sweep_result = sweep_ipc_orphans(target_hosts=hosts_to_clean, dry_run=False)
        for h in hosts_to_clean:
            s = sweep_result.get("details", {}).get(h, {})
            if s.get("status") == "ok" and s.get("segments_removed", 0) > 0:
                results[h] = f"{results.get(h, 'Purged')} (+ {s['segments_removed']} orphaned shm segment(s), {s['gb_freed']} GB freed)"

        time.sleep(2)
        return results
    finally:
        # Always resets, whether teardown succeeded or a phase raised --
        # otherwise a mid-teardown exception would leave the dashboard
        # showing "in progress" forever with no way to clear it. But
        # "resets" must not mean "always claims success": previously this
        # message was hardcoded to "Teardown complete" regardless of what
        # `results` actually says, so a genuine per-host docker-rm failure
        # was reported identically to a clean teardown -- both to whatever
        # polls teardown_message live, AND (via api_teardown(), see below)
        # to the dashboard's final toast. That's what let a host silently
        # keep its container running while the UI said "done" until
        # someone noticed the drift and killed it by hand.
        teardown_ok = _teardown_results_are_clean(results, hosts_to_clean)
        if teardown_ok:
            final_message = f"Teardown complete for {host_list}."
        else:
            failed = {h: results.get(h, "no result recorded (exception before this host's teardown finished)")
                      for h in hosts_to_clean
                      if h not in results or str(results[h]).lower().startswith("error")}
            final_message = f"Teardown completed WITH ERRORS on {list(failed.keys())} -- check manually: {failed}"
        _set_teardown_state(False, "done" if teardown_ok else "error", final_message, mark_last_run=True)
        # Always clear ACTIVE_DEPLOYMENT_STATE for these hosts too, same
        # unconditional-reset reasoning as TEARDOWN_STATE above -- a host
        # with no container running must never keep reporting a stale
        # "this recipe is active" record, whether teardown fully succeeded
        # or a phase raised partway through. If the container is actually
        # still running because teardown failed, _resolve_active_recipe()'s
        # live-discovery corroboration (see its docstring) will surface
        # that honestly via the fuzzy-match fallback instead of continuing
        # to show a stale exact record that teardown was already trying to
        # invalidate.
        _clear_active_deployment(hosts_to_clean)

def execute_teardown(target_hosts: list = None) -> dict:
    acquired = CLUSTER_OP_LOCK.acquire(timeout=CLUSTER_OP_LOCK_TIMEOUT)
    if not acquired: return {"status": "error", "message": "Cluster is busy with another deploy/teardown operation. Try again shortly."}
    try:
        SESSION_TRACKER._commit_session()
        SESSION_TRACKER.active = False
        return _execute_teardown_impl(target_hosts=target_hosts)
    finally:
        CLUSTER_OP_LOCK.release()

def execute_deployment(model: str, nodes: int, head: str, user_id: str, wait: bool = False, run_benchmark: bool = False, dry_run: bool = False) -> dict:
    """Thread-safe public wrapper for cluster model deployments."""
    acquired = CLUSTER_OP_LOCK.acquire(timeout=CLUSTER_OP_LOCK_TIMEOUT)
    if not acquired: return {"status": "error", "message": "Cluster is busy with another deploy/teardown operation. Try again shortly."}
    try:
        return _execute_deployment_impl(model, nodes, head, user_id, wait=wait, run_benchmark=run_benchmark, dry_run=dry_run)
    finally:
        CLUSTER_OP_LOCK.release()

def _run_benchmark_worker(head: str, nodes: int, model_key: Optional[str] = None):
    with BENCHMARK_STATE_LOCK:
        BENCHMARK_STATE["running"] = True
        BENCHMARK_STATE["message"] = f"Executing 3-pass benchmark against {head}..."

    try:
        head_ip = HOSTS[head]["ip"] if head in HOSTS else PRIMARY_HOST_IP
        print(f"[+] Running background 3-pass benchmark against {head} ({head_ip})...")

        cmd = [sys.executable, str(BASE_DIR / "benchmark.py"), "--host", head_ip, "--nodes", str(nodes)]
        if model_key:
            cmd.extend(["--model-key", model_key])

        # Hard ceiling on the subprocess: 3 passes x up to 300s urllib
        # timeout each, plus slack. Without this, a wedged socket inside
        # benchmark.py leaves BENCHMARK_STATE["running"] True forever,
        # which permanently trips the re-entry guard in
        # execute_standalone_benchmark() and pins the dashboard button on
        # "BENCHMARKING IN PROGRESS" until the daemon is restarted.
        bench_res = subprocess.run(
            cmd, capture_output=True, text=True, cwd=str(BASE_DIR), timeout=1200
        )

        if bench_res.returncode != 0:
            err = (bench_res.stderr or bench_res.stdout or "no output").strip()
            tail = err.splitlines()[-1] if err.splitlines() else err
            print(f"[!] benchmark.py exited {bench_res.returncode}: {err}")
            with BENCHMARK_STATE_LOCK:
                BENCHMARK_STATE["message"] = f"Benchmark failed (exit {bench_res.returncode}): {tail}"
            return  # leave the last good benchmark_results.txt untouched

        BENCHMARK_RESULTS_PATH.write_text(bench_res.stdout)
        print(f"[+] Benchmark completed. Written to {BENCHMARK_RESULTS_PATH}")
        with BENCHMARK_STATE_LOCK:
            BENCHMARK_STATE["message"] = "Benchmark completed successfully."
            BENCHMARK_STATE["last_run"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    except subprocess.TimeoutExpired:
        print("[!] Benchmark subprocess timed out after 1200s and was killed.")
        with BENCHMARK_STATE_LOCK:
            BENCHMARK_STATE["message"] = "Benchmark timed out after 1200s."
    except Exception as e:
        print(f"[!] Background benchmark execution failed: {e}")
        with BENCHMARK_STATE_LOCK:
            BENCHMARK_STATE["message"] = f"Benchmark failed: {e}"
    finally:
        # Flush whatever tokens this benchmark run generated into the
        # lifetime ledger immediately, rather than leaving them sitting in
        # SESSION_TRACKER's live accumulator until its own 10-minute-idle or
        # 1-hour-active commit triggers fire. Without this, the dashboard's
        # Lifetime Tokens figure could sit stale for up to 10 minutes after
        # a benchmark completes even though the tokens were genuinely
        # generated -- _commit_session() itself already no-ops safely if
        # there's no real delta, so this is safe to call unconditionally,
        # including on the failure/timeout paths above (a failed or timed-
        # out run may still have generated real tokens before it died).
        SESSION_TRACKER._commit_session()
        with BENCHMARK_STATE_LOCK:
            BENCHMARK_STATE["running"] = False

def execute_standalone_benchmark(head: str, nodes: int, model_key: Optional[str] = None) -> dict:
    """Launches benchmark.py asynchronously without blocking HTTP response or tearing down containers."""
    with BENCHMARK_STATE_LOCK:
        if BENCHMARK_STATE["running"]:
            return {"status": "error", "message": "A benchmark pass is already running in the background."}

    head_ip = HOSTS[head]["ip"] if head in HOSTS else PRIMARY_HOST_IP
    if not check_vllm_health(head_ip):
        return {"status": "error", "message": f"vLLM engine on {head} ({head_ip}) is not responding to health checks."}

    threading.Thread(target=_run_benchmark_worker, args=(head, nodes, model_key), daemon=True).start()
    return {"status": "success", "message": f"Benchmark background task initiated for {head}."}

def authorize_user_key(public_key_path: str) -> dict:
    key_file = Path(public_key_path).expanduser()
    if not key_file.exists(): return {"status": "error", "message": f"Key file not found: {public_key_path}"}
    pub_key_content = key_file.read_text().strip()
    escaped_key = shlex.quote(pub_key_content)
    results = {}
    for host, meta in HOSTS.items():
        ip = meta["ip"]
        cmd = ["bash", "-c", f"mkdir -p ~/.ssh && chmod 700 ~/.ssh && echo {escaped_key} >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"]
        res = run_ssh(ip, None, cmd, timeout=10)
        results[host] = "Authorized" if res.returncode == 0 else f"Failed: {res.stderr.strip()}"
    return {"status": "success", "details": results}

def _resolve_host_image_tag(host: str, ip: str, base_image: str, mod_names: list, dry_run: bool) -> str:
    """
    Task MC's one shared mod-resolution entry point. Called once per target
    host from BOTH the 1-node and 2-node branches of
    _execute_deployment_impl(), immediately before that host's `docker run`
    -- never inlined separately in each branch. See PHASE-MODS-PROMPTS.md's
    Task MC requirements for why: this repo has already paid for the same
    logic drifting apart across a 1-node/2-node split once (see
    common/ssh.py's module docstring for the run_ssh/get_hf_token
    precedent).

    Empty mod_names is a strict no-op in both dry_run and live-deploy
    cases: returns base_image unchanged with zero SSH round trips. This is
    the path every existing recipe takes today (mods: [] everywhere), and
    is what keeps --dry-run output byte-identical to before this function
    existed -- both resolve_mod_tag() and ensure_mods_baked() implement
    this fast path themselves (see common/mods.py), so it is not
    re-implemented here a third time.

    dry_run=True calls resolve_mod_tag() -- pure/local, no SSH, no bake --
    so "report the resolved tag and what would be baked; make no SSH
    connections and bake nothing" holds even for a recipe that DOES carry
    mods.

    dry_run=False calls ensure_mods_baked(), which bakes on `ip` first if
    that tag isn't already there, then returns the live tag.

    Raises ModResolutionError (missing/invalid mod name -- pure/local, so
    identical for every host given the same base_image/mod_names; it
    always surfaces on the first host _execute_deployment_impl touches,
    before that host's docker run and therefore before any host's) or
    ModBakeError (this host's bake specifically failed). Both are left
    unhandled here -- the caller decides how a mid-deploy abort is
    reported.
    """
    if dry_run:
        return resolve_mod_tag(base_image, mod_names)
    return ensure_mods_baked(host, ip, base_image, mod_names)


def _execute_deployment_impl(model: str, nodes: int, head: str, user_id: str, wait: bool = False, run_benchmark: bool = False, dry_run: bool = False) -> dict:
    deploy_start_time = time.time()
    docker_run_commands: dict = {}
    # Populated only for hosts whose mod set is non-empty (see
    # _resolve_host_image_tag() / Task MC) -- stays {} for every existing
    # recipe (mods: [] everywhere), so it is only added to the dry_run
    # response dict below when non-empty. That is what keeps --dry-run
    # output byte-identical to before this change for the whole current
    # catalog.
    mods_report: dict = {}

    if nodes not in (1, 2): return {"status": "error", "message": f"Invalid 'nodes' value {nodes!r}: must be 1 or 2."}
    if head not in HOSTS: return {"status": "error", "message": f"Invalid 'head' value {head!r}: must be one of {list(HOSTS.keys())}."}

    target_hosts = [PRIMARY_HOST, SECONDARY_HOST] if nodes == 2 else [head]
    catalog_resp = load_model_catalog()
    models_catalog = catalog_resp.get("catalog", {}).get("models", {})

    if model not in models_catalog: return {"status": "error", "message": f"Model '{model}' not defined in catalog."}

    model_config = models_catalog[model]
    topologies = model_config.get("topologies", {})
    topo_key = "2_node" if nodes == 2 else "1_node"

    if topo_key not in topologies: return {"status": "error", "message": f"Topology '{topo_key}' not supported for model '{model}'."}

    offline_mode = False
    if NETWORK_STATE_FILE.exists():
        try:
            offline_mode = "OFFLINE" in NETWORK_STATE_FILE.read_text().strip()
        except Exception as exc:
            # Once per deploy, not once per 4s status poll -- worth a
            # signal here specifically, since a silently-wrong offline_mode
            # changes which --hf-token/network flags actually get passed
            # to the container this deploy launches.
            print(f"[!] _execute_deployment_impl: failed to read {NETWORK_STATE_FILE} - {exc}")
    offline_val = "1" if offline_mode else "0"

    topo_config = topologies[topo_key]
    hf_path = model_config.get("hf_path", model)
    if not dry_run:
        _record_hf_path(model, hf_path)
    gpu_util = model_config.get("gpu_util", 0.75)
    max_model_len = topo_config.get("max_model_len", 32768)
    tp_size = topo_config.get("tp_size", 1)
    pp_size = topo_config.get("pp_size", nodes)
    
    vllm_args_raw = topo_config.get("vllm_args", "")
    try:
        vllm_args_list = shlex.split(vllm_args_raw)
    except Exception as exc:
        # Falling back to a naive .split() here is exactly the failure
        # mode behind the documented "Argument splitting bug" -- it breaks
        # on any quoted value containing spaces (e.g. JSON passed to
        # --speculative-config), silently handing vLLM a mis-split argv
        # instead of raising where a human would see it. Print it so a
        # shlex-unparseable vllm_args string in a recipe shows up in the
        # daemon log at deploy time, not as a mysterious argparse error
        # three layers downstream inside the container.
        print(f"[!] _execute_deployment_impl({model}): vllm_args failed shlex.split(), falling back to naive whitespace split - {exc}")
        vllm_args_list = vllm_args_raw.split()

    use_ray = (nodes > 1) and ("--distributed-executor-backend" in vllm_args_list) and ("ray" in vllm_args_list)
    tuning = load_cluster_config().tuning

    # Per-deploy id for crash log persistence (see _jit_cache_mounts_and_env below).
    # Keyed by model+timestamp so concurrent/successive deploys never clobber each
    # other's Ray session dir, and each host gets its own subdir so head/worker
    # logs from the same run don't collide.
    deploy_run_id = f"{re.sub(r'[^A-Za-z0-9._-]', '-', model)}-{int(time.time())}"

    if not dry_run:
        SESSION_TRACKER._commit_session()
        SESSION_TRACKER.active = False
        _execute_teardown_impl(target_hosts=target_hosts)
        for h in target_hosts:
            ip = HOSTS[h]["ip"]
            run_ssh(ip, None, ["sudo", "nvidia-smi", "-lgc", tuning.gpu_clock_lock], timeout=10)
            run_ssh(ip, None, ["bash", "-c", "mkdir -p ~/.cache/tilelang ~/.cache/deepgemm ~/.cache/triton ~/.cache/vllm ~/.cache/flashinfer"], timeout=10)
            run_ssh(ip, None, ["bash", "-c", f"mkdir -p ~/.cache/ray-logs/{deploy_run_id}/{h}"], timeout=10)

    default_img = catalog_resp.get("catalog", {}).get("default_image", load_cluster_config().default_image)
    image_tag = model_config.get("image", default_img)
    compat_mount = "/dev/null:/etc/ld.so.conf.d/00-cuda-compat.conf"

    # Task MC: resolve this recipe's mods (Task MA/MB) against image_tag,
    # per target host, right before that host's docker run -- see
    # _resolve_host_image_tag() above. build_catalog_response() deliberately
    # does not surface `mods` in the catalog dict yet (see common/recipes.py
    # -- still execution-inert as of Task MA/MB), so mod_names is read
    # straight from the raw RecipeConfig via load_recipes(), not from
    # model_config. Under USE_LEGACY_CATALOG=1, or for any model not backed
    # by a recipes/{local,eugr}/*.yaml file, there is no RecipeConfig at
    # all -- mod_names stays [] (strict no-op), exactly as if the recipe
    # had an explicit mods: []. A recipe-loading error here (e.g. an
    # unrelated malformed recipe file elsewhere in recipes/) must not block
    # this deploy, since this model's own recipe may be perfectly fine (or
    # may not even come from recipes/ at all under legacy mode) -- so it is
    # caught and logged, not raised, falling back to mod_names = [].
    mod_names: list = []
    try:
        recipe_obj = load_recipes().get(model)
        if recipe_obj is not None:
            mod_names = list(recipe_obj.mods)
    except Exception as exc:
        print(f"[!] _execute_deployment_impl({model}): failed to load recipe for mod resolution "
              f"({type(exc).__name__}: {exc}); proceeding with no mods.")

    def _jit_cache_mounts_and_env(vol_mount: str, log_subdir: str) -> tuple[list[str], list[str]]:
        host_hf_dir = vol_mount.split(":", 1)[0]
        host_cache_root = str(Path(host_hf_dir).parent)
        mounts = [
            "-v", f"{host_cache_root}/triton:/root/.cache/triton",
            "-v", f"{host_cache_root}/tilelang:/root/.cache/tilelang",
            "-v", f"{host_cache_root}/deepgemm:/root/.cache/deepgemm",
            "-v", f"{host_cache_root}/vllm:/root/.cache/vllm",
            "-v", f"{host_cache_root}/flashinfer:/root/.cache/flashinfer",
            "-v", f"{host_cache_root}/nv_compute_cache:/root/.nv/ComputeCache",
            # Ray writes its session dir (including per-worker stdout/stderr and any
            # crash traceback) under /tmp/ray by default. That's container-local and
            # vanishes on teardown, which is exactly what left us with nothing to
            # inspect after the 2026-08-25 silent RayWorkerProc death. Binding it to
            # a host path keyed by deploy_run_id + host makes it survive teardown.
            "-v", f"{host_cache_root}/ray-logs/{deploy_run_id}/{log_subdir}:/tmp/ray",
        ]
        env = [
            "-e", "TRITON_CACHE_DIR=/root/.cache/triton",
            "-e", "CUDA_CACHE_PATH=/root/.nv/ComputeCache",
            "-e", "TILELANG_CACHE_DIR=/root/.cache/tilelang",
            "-e", "TL_CACHE_DIR=/root/.cache/tilelang",
            "-e", "DEEPGEMM_CACHE_DIR=/root/.cache/deepgemm",
            "-e", "VLLM_CACHE_DIR=/root/.cache/vllm",
            "-e", f"CUDA_CACHE_MAXSIZE={tuning.jit_cache_maxsize_bytes}",
        ]
        # Opt-in debug mode: forces synchronous CUDA kernel launches so a kernel-level
        # fault raises a normal Python/CUDA traceback at the actual failing launch
        # instead of surfacing later as an opaque "died unexpectedly" with no stack.
        # Costs real throughput (kernels no longer queue async) - leave off for normal
        # serving and flip on in cluster_config.yaml only while chasing a repro.
        if getattr(tuning, "debug_launch_blocking", False):
            env += ["-e", "CUDA_LAUNCH_BLOCKING=1"]
        return mounts, env

    head_ip = HOSTS[head]["ip"]
    hf_token = get_hf_token()

    if nodes == 1:
        ip = HOSTS[head]["ip"]
        vol_mount = load_cluster_config().hosts[head].volume_mount
        jit_mounts, jit_env = _jit_cache_mounts_and_env(vol_mount, head)
        env_flags = [
            "-e", "PYTHONUNBUFFERED=1",
            "-e", "NVIDIA_DISABLE_REQUIRE=true",
            "-e", f"HF_HUB_OFFLINE={offline_val}",
            "-e", f"TRANSFORMERS_OFFLINE={offline_val}"
        ] + jit_env
        if hf_token: env_flags.extend(["-e", f"HF_TOKEN={hf_token}"])

        for ev in topo_config.get("env_vars", []):
            if not ev.startswith("HF_HUB_OFFLINE=") and not ev.startswith("TRANSFORMERS_OFFLINE="):
                env_flags.extend(["-e", ev])

        container_args = [
            "python3", "-m", "vllm.entrypoints.openai.api_server",
            "--model", hf_path,
            "--gpu-memory-utilization", str(gpu_util),
            "--max-model-len", str(max_model_len)
        ] + vllm_args_list

        # Bake happens per target host, before that host's docker run --
        # see _resolve_host_image_tag(). Mod resolution failure (bad/
        # missing mod name) aborts here, before this host's docker run and
        # therefore before any container starts.
        try:
            host_image_tag = _resolve_host_image_tag(head, ip, image_tag, mod_names, dry_run)
        except (ModResolutionError, ModBakeError) as exc:
            return {"status": "error", "message": f"Mod resolution failed for {model} on {head}: {exc}"}
        if mod_names:
            mods_report[head] = {"base_image": image_tag, "mod_names": mod_names, "resolved_tag": host_image_tag}

        docker_cmd = [
            "docker", "run", "-d", "--init",
            "--name", ContainerRole.STANDALONE,
            "--net=host", "--ipc=host", f"--shm-size={tuning.shm_size_1node}",
            "--gpus", "all",
            "-v", vol_mount,
            "-v", compat_mount
        ] + jit_mounts + env_flags + [host_image_tag] + container_args

        res = None if dry_run else run_ssh(ip, None, docker_cmd, timeout=60)
        if dry_run: docker_run_commands[head] = docker_cmd
        elif res.returncode != 0: return {"status": "error", "message": f"Docker run command failed on {head}: {res.stderr}"}
    else:
        vllm_head_args = None
        cluster_ports = load_cluster_config().ports
        master_port = str(cluster_ports.get("master", 29500))
        ray_port = str(cluster_ports.get("ray", 6379))

        for host in target_hosts:
            ip = HOSTS[host]["ip"]
            vol_mount = load_cluster_config().hosts[host].volume_mount
            jit_mounts, jit_env = _jit_cache_mounts_and_env(vol_mount, host)
            role_name = ContainerRole.HEAD if host == head else ContainerRole.WORKER
            node_rank = 0 if host == head else 1

            env_flags = [
                "-e", "PYTHONUNBUFFERED=1",
                "-e", "NCCL_DEBUG=INFO",
                "-e", "NVIDIA_DISABLE_REQUIRE=true",
                "-e", "NCCL_IB_DISABLE=0",
                "-e", "NCCL_P2P_DISABLE=0",
                "-e", "NCCL_IB_HCA=rocep1s0f0",
                "-e", "NCCL_IB_GID_INDEX=3",
                "-e", "NCCL_SOCKET_IFNAME=enp1s0f0np0",
                "-e", "GLOO_SOCKET_IFNAME=enp1s0f0np0",
                "-e", "NCCL_BUFFSIZE=16777216",
                "-e", "NCCL_NSOCKS_PER_THREAD=4",
                "-e", "NCCL_SOCKET_DRV_BUFFSIZE=2097152",
                "-e", "NCCL_CUMEM_ENABLE=0",
                "-e", f"HF_HUB_OFFLINE={offline_val}",
                "-e", f"TRANSFORMERS_OFFLINE={offline_val}"
            ] + jit_env
            if hf_token: env_flags.extend(["-e", f"HF_TOKEN={hf_token}"])

            for ev in topo_config.get("env_vars", []):
                if not ev.startswith("HF_HUB_OFFLINE=") and not ev.startswith("TRANSFORMERS_OFFLINE="):
                    env_flags.extend(["-e", ev])

            if use_ray:
                container_args = [
                    "python3", "-m", "vllm.entrypoints.openai.api_server",
                    "--model", hf_path,
                    "--tensor-parallel-size", str(tp_size),
                    "--pipeline-parallel-size", str(pp_size),
                    "--gpu-memory-utilization", str(gpu_util),
                    "--max-model-len", str(max_model_len)
                ] + vllm_args_list
            else:
                container_args = [
                    "python3", "-m", "vllm.entrypoints.openai.api_server",
                    "--model", hf_path,
                    "--tensor-parallel-size", str(tp_size),
                    "--pipeline-parallel-size", str(pp_size),
                    "--nnodes", str(nodes),
                    "--node-rank", str(node_rank),
                    "--master-addr", head_ip,
                    "--master-port", master_port,
                    "--gpu-memory-utilization", str(gpu_util),
                    "--max-model-len", str(max_model_len)
                ]
                if node_rank > 0 and "--headless" not in vllm_args_list: container_args.append("--headless")
                container_args.extend(vllm_args_list)

            if host == head: vllm_head_args = container_args

            if use_ray:
                if host == head: entrypoint_cmd = ["ray", "start", "--head", f"--port={ray_port}", "--num-gpus=1", "--block"]
                else: entrypoint_cmd = ["ray", "start", f"--address={head_ip}:{ray_port}", "--num-gpus=1", "--block"]
            else:
                entrypoint_cmd = container_args

            # Bake happens per target host, before that host's docker run --
            # see _resolve_host_image_tag(). Mod resolution failure (bad/
            # missing mod name) is host-independent given a fixed
            # (image_tag, mod_names), so if it's going to happen it happens
            # on the first host in target_hosts, before that host's docker
            # run and therefore before any container on either host starts.
            # A bake failure specific to this host aborts here too, but (like
            # a plain docker-run failure below) does not roll back a prior
            # host that already started -- consistent with this loop's
            # existing partial-failure handling.
            try:
                host_image_tag = _resolve_host_image_tag(host, ip, image_tag, mod_names, dry_run)
            except (ModResolutionError, ModBakeError) as exc:
                return {"status": "error", "message": f"Mod resolution failed for {model} on {host}: {exc}"}
            if mod_names:
                mods_report[host] = {"base_image": image_tag, "mod_names": mod_names, "resolved_tag": host_image_tag}

            docker_cmd = [
                "docker", "run", "-d", "--init",
                "--name", role_name,
                "--net=host", "--ipc=host", f"--shm-size={tuning.shm_size_2node}",
                "--privileged",
                "--cap-add", "IPC_LOCK",
                "--device", "/dev/infiniband:/dev/infiniband",
                "--gpus", "all",
                "-v", vol_mount,
                "-v", compat_mount
            ] + jit_mounts + env_flags + [host_image_tag] + entrypoint_cmd

            res = None if dry_run else run_ssh(ip, None, docker_cmd, timeout=60)
            if dry_run: docker_run_commands[host] = docker_cmd
            elif res.returncode != 0: return {"status": "error", "message": f"Docker run failed on {host}: {res.stderr}"}

        if use_ray and vllm_head_args and not dry_run:
            print("[+] Waiting for Ray cluster to register worker nodes (max 60s)...")
            worker_hosts = [k for k, v in HOSTS.items() if v["role"] == "worker"]
            worker_ip = HOSTS[worker_hosts[0]]["ip"] if worker_hosts else ""
            
            for _ in range(30):
                check_ray = run_ssh(head_ip, None, ["docker", "exec", ContainerRole.HEAD, "ray", "status"], timeout=10)
                if check_ray.returncode == 0 and (worker_ip in check_ray.stdout or "2 active nodes" in check_ray.stdout or "Healthy: 2" in check_ray.stdout):
                    break
                time.sleep(2)
            
            exec_str = " ".join(shlex.quote(arg) for arg in vllm_head_args)
            vllm_exec_cmd = ["docker", "exec", "-d", ContainerRole.HEAD, "bash", "-c", f"{exec_str} > /proc/1/fd/1 2>&1"]
            run_ssh(head_ip, None, vllm_exec_cmd, timeout=30)

    if dry_run:
        dry_run_result = {
            "status": "dry_run",
            "message": f"Dry-run for {model} across {nodes} node(s) - no SSH connections made, nothing executed.",
            "targets": target_hosts,
            "head": head,
            "docker_run_commands": docker_run_commands,
        }
        # Only added when at least one host actually has mods to report --
        # every existing recipe (mods: [] everywhere) leaves mods_report
        # empty, so this key is simply absent for them, keeping --dry-run
        # output byte-identical to before Task MC for the whole current
        # catalog. The resolved tag is already visible inline inside
        # docker_run_commands either way (it's the image argument);
        # mods_report additionally spells out "what would be baked" per
        # Task MC's dry-run requirement.
        if mods_report:
            dry_run_result["mods"] = mods_report
        return dry_run_result

    time.sleep(4)
    for host in target_hosts:
        ip = HOSTS[host]["ip"]
        check_res = run_ssh(ip, None, ["docker", "ps", "--filter", "status=running", "--format", "{{.Names}}"], timeout=10)
        running = [c.strip() for c in check_res.stdout.splitlines() if c.strip() in [ContainerRole.STANDALONE, ContainerRole.HEAD, ContainerRole.WORKER]]
        
        if not running:
            target_role = ContainerRole.STANDALONE if nodes == 1 else (ContainerRole.HEAD if host == head else ContainerRole.WORKER)
            log_res = run_ssh(ip, None, ["docker", "logs", "--tail", "50", target_role], timeout=10)
            err_log = log_res.stdout.strip() or log_res.stderr.strip() or "No logs captured."
            return {"status": "error", "message": f"Container '{target_role}' crashed on {host} immediately after startup.\nLogs:\n{err_log}"}

    # Drop a pending launch-confirmation record regardless of wait/
    # run_benchmark -- this is what lets a plain "Deploy" click (which
    # doesn't pass wait=True) still eventually get marked as launched
    # successfully, via the status-polling loop rather than blocking this
    # request on a full health-check wait. Silently skipped under the
    # legacy models.yaml catalog, which has no per-recipe config_hash to
    # key against, and silently skipped (not fatal) if hashing fails for
    # any other reason -- this is a QoL marker, not load-bearing for deploy
    # itself.
    #
    # The same recipe/config_hash resolution also feeds ACTIVE_DEPLOYMENT_STATE
    # (unlike PENDING_LAUNCH_STATE, this one IS load-bearing -- it's what the
    # dashboard reads to know which recipe is actually running, so it's set
    # for every host in target_hosts here, at the point we know each of them
    # has a container confirmed running post-launch, not gated on a later
    # health check the way launch-success recording is).
    if os.environ.get("USE_LEGACY_CATALOG") != "1":
        try:
            recipe = load_recipes().get(model)
            if recipe is not None:
                cfg_hash = compute_config_hash(recipe, topo_key)
                with PENDING_LAUNCH_LOCK:
                    PENDING_LAUNCH_STATE["pending"] = {
                        "model": model,
                        "topo_key": topo_key,
                        "config_hash": cfg_hash,
                        "started_ts": time.time(),
                    }
                for h in target_hosts:
                    _set_active_deployment(h, model, topo_key, cfg_hash)
        except Exception as exc:
            # PENDING_LAUNCH_STATE staying unset here is genuinely fine
            # (it's just a launch-success telemetry marker, as noted
            # above) -- but ACTIVE_DEPLOYMENT_STATE staying unset is NOT
            # fine. It's what lets the dashboard show the correct recipe
            # instead of falling back to the ambiguous fuzzy served-name
            # match (see ACTIVE_DEPLOYMENT_STATE's module comment -- this
            # whole mechanism exists specifically to fix that ambiguity).
            # A silent failure here would silently reintroduce it. Print
            # so a broken load_recipes()/compute_config_hash() call after
            # this deploy shows up in the daemon log immediately, not as
            # a confusing "why did the dropdown revert again" report.
            print(f"[!] _execute_deployment_impl({model}): failed to record ACTIVE_DEPLOYMENT_STATE - {exc}")

    if wait or run_benchmark:
        head_ip = HOSTS[head]["ip"]
        is_ready = wait_for_cluster_ready(head_ip=head_ip, timeout_sec=tuning.deploy_wait_timeout_sec, poll_interval=tuning.deploy_poll_interval_sec)
        
        if is_ready:
            total_duration = int(time.time() - deploy_start_time)
            record_load_time(model, topo_key, total_duration, "cached")

            if run_benchmark:
                execute_standalone_benchmark(head=head, nodes=nodes, model_key=model)

    return {
        "status": "success",
        "message": f"Deployment sequence for {model} across {nodes} node(s) initiated.",
        "targets": target_hosts,
        "head": head
    }

def get_container_logs(host: str, tail: int = 40) -> dict:
    if host not in HOSTS: return {"logs": ["Invalid target host specified."]}
    
    ip = HOSTS[host]["ip"]
    res = run_ssh(ip, None, ["docker", "ps", "-a", "--format", "{{.Names}}"], timeout=10)
    if res.returncode != 0: return {"logs": ["Failed to connect to host."]}

    containers = [c.strip() for c in res.stdout.strip().splitlines() if c.strip() in [ContainerRole.STANDALONE, ContainerRole.HEAD, ContainerRole.WORKER]]
    if not containers: return {"logs": ["No active vLLM containers on this node."]}

    c_name = containers[0]
    fetch_tail = max(tail * 5, 400)
    log_res = run_ssh(ip, None, ["docker", "logs", "--tail", str(fetch_tail), c_name], timeout=10)
    
    raw_logs = log_res.stdout.splitlines() if log_res.returncode == 0 else log_res.stderr.splitlines()
    clean_logs = [ANSI_ESCAPE.sub('', line) for line in raw_logs]
    filtered_logs = [line for line in clean_logs if not any(endpoint in line for endpoint in ["GET /health", "GET /metrics"])]
    final_logs = filtered_logs[-tail:] if len(filtered_logs) > tail else filtered_logs

    return {"logs": final_logs if final_logs else ["Log buffer empty."]}

def interactive_menu():
    print("=== [ TETREL SECURITY ] DGX Cluster Orchestrator ===")
    status = get_cluster_status()
    print(f"[{status.get('orchestrator_version', 'unknown')}] Server Time: {status['server_time']} | Mode: {status['network_mode']} | API: {'READY' if status['cluster_ready'] else 'OFFLINE'} (serving: {status.get('serving_host', PRIMARY_HOST)}) | TPS: {status.get('system_tps', 0.0)} tok/s | Streams: {status.get('running_requests', 0)} active ({status.get('waiting_requests', 0)} queued)\n")

    for h, data in status["hosts"].items():
        tele = data.get("telemetry", {})
        temp = f"{tele.get('gpu_temp_c', 'N/A')}°C" if 'gpu_temp_c' in tele and tele['gpu_temp_c'] != 'N/A' else "N/A"
        util = f"{tele.get('gpu_util_pct', 'N/A')}%" if 'gpu_util_pct' in tele and tele['gpu_util_pct'] != 'N/A' else "N/A"
        
        pwr_draw = tele.get('power_draw_w', 'N/A')
        pwr_lim = tele.get('power_limit_w', 'N/A')
        if pwr_draw != 'N/A' and pwr_lim != 'N/A': pwr = f"{pwr_draw}/{pwr_lim}W"
        elif pwr_draw != 'N/A': pwr = f"{pwr_draw}W"
        else: pwr = "N/A"
            
        ram = f"{tele.get('host_mem_avail_mb', 'N/A')} MB Free" if 'host_mem_avail_mb' in tele and tele['host_mem_avail_mb'] != 'N/A' else "N/A"
        print(f"[{h}] Docker: {data['docker_status']} | Container: {data['container_name']} ({data['container_state']}) | Model: {data['active_model']} | Status: {data['model_status']} | ETA: {data['eta_display']} | TEMP: {temp} | GPU: {util} | PWR: {pwr} | RAM: {ram}")
    print("-" * 85)

    catalog_data = load_model_catalog().get("catalog", {}).get("models", {})
    models = list(catalog_data.keys())

    if not models:
        print("[-] No models found in catalog.")
        return

    print("\nAvailable Models:")
    for idx, m in enumerate(models, 1): print(f"  {idx}. {m}")

    try:
        choice = input("\nSelect a model number to deploy (or 'q' to quit): ").strip()
        if choice.lower() == 'q' or not choice.isdigit(): return

        selected_model = models[int(choice) - 1]
        topologies = catalog_data[selected_model].get("topologies", {})

        print(f"\nAvailable Topologies for {selected_model}:")
        topo_keys = list(topologies.keys())
        for idx, t in enumerate(topo_keys, 1):
            est, _ = get_estimated_load_time(selected_model, t, "cached")
            print(f"  {idx}. {t} (Est. Warm Load Time: ~{est}s)")

        t_choice = input("Select topology number: ").strip()
        if not t_choice.isdigit(): return

        selected_topo = topo_keys[int(t_choice) - 1]
        nodes = 2 if selected_topo == "2_node" else 1
        head = PRIMARY_HOST

        if nodes == 1:
            h_choice = input(f"Target head node for 1-node deploy (1: {PRIMARY_HOST}, 2: {SECONDARY_HOST}) [1]: ").strip()
            if h_choice == "2": head = SECONDARY_HOST

        user_name = os.environ.get("USER") or getpass.getuser()
        user_id = input(f"Enter User ID / Auditor [{user_name}]: ").strip() or user_name
        
        wait_choice = input("Block until vLLM passes HTTP health check? (y/N) [y]: ").strip().lower() or 'y'
        do_wait = wait_choice == 'y'
        
        bench_choice = input("Automatically run benchmark after health check? (y/N) [n]: ").strip().lower()
        do_bench = bench_choice == 'y'

        confirm = input(f"\nDeploy {selected_model} ({selected_topo}) with head {head}? (y/N): ").strip().lower()
        if confirm == 'y':
            print(f"[+] Launching deployment sequence for {selected_model}...")
            res = execute_deployment(selected_model, nodes, head, user_id, wait=do_wait, run_benchmark=do_bench)
            print(json.dumps(res, indent=2))
    except (IndexError, ValueError) as e:
        print(f"[-] Invalid selection: {e}")

if HAS_FASTAPI:
    app = FastAPI(title="Tetrel Security DGX Control Plane API", version="4.8.4")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

    class DeployRequest(BaseModel):
        model: str
        nodes: Literal[1, 2]
        head: str = PRIMARY_HOST
        user_id: str = "dashboard_user"
        wait: bool = False
        run_benchmark: bool = False
        dry_run: bool = False

    class BenchmarkRequest(BaseModel):
        head: str = PRIMARY_HOST
        nodes: Literal[1, 2] = 2
        model: Optional[str] = None  # catalog key, threaded to benchmark.py --model-key for ledger join

    class NetworkToggleRequest(BaseModel):
        offline: bool

    class PruneCacheRequest(BaseModel):
        min_free_gb: int = 50
        headroom_gb: int = 20
        dry_run: bool = False

    class PruneRayLogsRequest(BaseModel):
        retention_days: Optional[int] = None  # None -> tuning.crash_log_retention_days
        dry_run: bool = False

    class CorrectLedgerRequest(BaseModel):
        model: Optional[str] = None
        topo: Optional[str] = None
        prompt_tokens: Optional[float] = None
        gen_tokens: Optional[float] = None
        draft_tokens: Optional[float] = None
        accepted_tokens: Optional[float] = None
        dry_run: bool = False
        force: bool = False

    class FlushModelCacheRequest(BaseModel):
        model: str  # catalog key, or a raw HF repo_id (org/repo) for an already-retired model
        include_jit: bool = False
        dry_run: bool = False
        force: bool = False

    class SweepIpcRequest(BaseModel):
        dry_run: bool = False

    @app.get("/api/status")
    def api_status(): return get_cluster_status()

    @app.get("/api/catalog")
    def api_catalog(): return load_model_catalog()

    @app.get("/api/logs/{host}")
    def api_logs(host: str, tail: int = 40): return get_container_logs(host, tail)

    @app.post("/api/deploy")
    def api_deploy(req: DeployRequest):
        res = execute_deployment(req.model, req.nodes, req.head, req.user_id, wait=req.wait, run_benchmark=req.run_benchmark, dry_run=req.dry_run)
        if res.get("status") not in ("success", "dry_run"): raise HTTPException(status_code=400, detail=res.get("message", "Deployment failed"))
        return res

    @app.post("/api/benchmark")
    def api_benchmark(req: BenchmarkRequest):
        res = execute_standalone_benchmark(req.head, req.nodes, model_key=req.model)
        if res.get("status") != "success": raise HTTPException(status_code=400, detail=res.get("message", "Benchmark failed"))
        return res

    @app.post("/api/teardown")
    def api_teardown():
        results = execute_teardown()
        # execute_teardown() has two possible shapes: a top-level
        # {"status": "error", "message": ...} if it never even started
        # (e.g. CLUSTER_OP_LOCK busy -- see execute_teardown()'s own early
        # return), or a per-host results map on the normal path where each
        # value is "Purged" (+ optional shm-sweep suffix) or "Error: ...".
        # Previously this endpoint returned either shape as a bare 200 OK
        # with no inspection at all, so the dashboard's existing
        # `!response.ok` error-toast branch (see index.html's
        # teardownRuntimes()) never fired even when a host genuinely
        # failed to tear down -- the CLI surfaced the same failure because
        # it prints the raw results dict instead of discarding it. This
        # brings /api/teardown in line with api_deploy()/api_benchmark()
        # above, which already both check their result's status.
        if isinstance(results, dict) and results.get("status") == "error":
            raise HTTPException(status_code=409, detail=results.get("message", "Teardown could not start."))
        failed = {h: r for h, r in results.items() if isinstance(r, str) and r.lower().startswith("error")}
        if failed:
            raise HTTPException(status_code=500, detail=f"Teardown failed on: {failed}")
        return results

    @app.post("/api/toggle-network")
    def api_toggle_network(req: NetworkToggleRequest):
        mode_str = "OFFLINE" if req.offline else "ONLINE"
        try:
            NETWORK_STATE_FILE.write_text(f"Working in {mode_str} mode")
            return {"status": "success", "mode": mode_str}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
            
    @app.get("/api/cache-inventory")
    def api_cache_inventory():
        return cache_inventory()

    @app.post("/api/prune-cache")
    def api_prune_cache(req: PruneCacheRequest):
        return prune_cluster_cache(req.min_free_gb, req.headroom_gb, req.dry_run)

    @app.post("/api/prune-ray-logs")
    def api_prune_ray_logs(req: PruneRayLogsRequest):
        return prune_cluster_ray_logs(req.retention_days, req.dry_run)

    @app.post("/api/correct-ledger")
    def api_correct_ledger(req: CorrectLedgerRequest):
        return correct_ledger_entry(
            req.model, req.topo, req.prompt_tokens, req.gen_tokens,
            req.draft_tokens, req.accepted_tokens, req.dry_run, req.force,
        )

    @app.post("/api/flush-model-cache")
    def api_flush_model_cache(req: FlushModelCacheRequest):
        res = flush_model_cache(req.model, include_jit=req.include_jit, dry_run=req.dry_run, force=req.force)
        if res.get("status") != "success": raise HTTPException(status_code=400, detail=res.get("message", "Flush failed"))
        return res

    @app.get("/api/list-cached-models")
    def api_list_cached_models():
        return find_cached_models()

    @app.get("/api/ipc-inventory")
    def api_ipc_inventory():
        return ipc_inventory()

    @app.post("/api/sweep-ipc-orphans")
    def api_sweep_ipc_orphans(req: SweepIpcRequest):
        return sweep_ipc_orphans(dry_run=req.dry_run)

_SSH_MUX_FLUSH_INTERVAL_SEC = 300  # every 5 minutes

def _flush_stale_ssh_multiplex_sockets():
    """
    Remove this container's own SSH ControlMaster multiplex sockets
    (~/.ssh/cm-* and /tmp/cm-*). This is the exact same hygiene step
    dgx-config's CLI wrapper already runs before every single invocation
    ("Flush Stale SSH Multiplex Sockets inside Container") -- but every
    CLI call is short-lived and gets a fresh flush for free, while this
    long-running daemon process never flushes for itself across a
    multi-day uptime.

    Why it matters here specifically: run_ssh()'s caller-side timeout
    (e.g. .result(timeout=...)) only stops the CALLER from waiting: it
    does not kill the underlying SSH subprocess if that subprocess is
    itself hung (e.g. on a dead/stale multiplexed control connection).
    WORKER_POOL has only len(HOSTS)*2 threads total; a thread stuck
    forever on a hung SSH call never comes back to the pool. Losing all
    of them one at a time over a long enough uptime is believed to be
    what caused get_cluster_status() to wedge and silently serve a
    stale cache for hours on 2026-08-25 and again on 2026-08-27.

    Removing the socket FILE while a live connection still has it open
    is safe on Linux -- the open fd keeps working, and the next SSH
    invocation through that ControlPath just establishes a fresh master.
    This is prophylactic housekeeping, not a fix for an SSH call that's
    ALREADY hung (see run_ssh()'s own timeout handling for that half).
    """
    removed = 0
    for pattern in ("/root/.ssh/cm-*", "/tmp/cm-*"):
        for path in glob.glob(pattern):
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
    if removed:
        print(f"[+] Flushed {removed} stale SSH multiplex socket(s)")

def _telemetry_daemon_loop():
    """Background polling loop for maintaining lifetime and session analytics."""
    iterations = 0
    while True:
        try:
            get_cluster_status()
        except Exception as exc:
            # This is the top-level background watchdog -- if
            # get_cluster_status() is raising here, SESSION_TRACKER isn't
            # accumulating, launch-success/ACTIVE_DEPLOYMENT_STATE aren't
            # being consumed/recorded, and nothing else in this process
            # would ever surface that on its own. Silently swallowing it
            # (the previous behavior) is exactly how the 08-25/27/28
            # SessionTracker self-deadlock went unnoticed for hours --
            # see the RLock fix's TOMBSTONES entry. A repeated print here
            # if this loop is genuinely wedged is a feature, not noise:
            # it's the signal that would have made that incident visible
            # in minutes instead of hours.
            print(f"[!] _telemetry_daemon_loop: get_cluster_status() raised - {exc}")
        iterations += 1
        if iterations % max(1, _SSH_MUX_FLUSH_INTERVAL_SEC // 10) == 0:
            try:
                _flush_stale_ssh_multiplex_sockets()
            except Exception as exc:
                print(f"[!] _telemetry_daemon_loop: _flush_stale_ssh_multiplex_sockets() raised - {exc}")
        time.sleep(10)

def handle_shutdown(signum, frame):
    """Intercepts orchestrator stops/restarts to safely flush RAM data to NVMe."""
    print("\n[+] Orchestrator daemon shutting down, flushing state to ledger...")
    SESSION_TRACKER._commit_session()
    sys.exit(0)

def main():
    parser = argparse.ArgumentParser(description="Tetrel Security DGX Cluster Orchestrator")
    subparsers = parser.add_subparsers(dest="subcommand")

    subparsers.add_parser("daemon").add_argument("--port", type=int, default=5001)
    subparsers.add_parser("status")
    subparsers.add_parser("teardown")
    subparsers.add_parser("menu")
    subparsers.add_parser("cache-inventory", help="Read-only cache snapshot across the cluster. No deletion, safe against production.")

    deploy_parser = subparsers.add_parser("deploy")
    deploy_parser.add_argument("--model", required=True)
    deploy_parser.add_argument("--nodes", type=int, default=2, choices=[1, 2])
    deploy_parser.add_argument("--head", default=PRIMARY_HOST)
    deploy_parser.add_argument("--wait", action="store_true", help="Block until HTTP /health passes")
    deploy_parser.add_argument("--benchmark", action="store_true", help="Automatically run benchmark.py when ready")
    deploy_parser.add_argument("--dry-run", action="store_true", help="Print the docker run command(s) this deploy would send, without SSHing or executing anything")
    deploy_parser.add_argument("-y", "--yes", action="store_true")

    logs_parser = subparsers.add_parser("logs")
    logs_parser.add_argument("--host", default=PRIMARY_HOST)
    logs_parser.add_argument("--tail", type=int, default=40)

    auth_parser = subparsers.add_parser("authorize-key")
    auth_parser.add_argument("--key", required=True)
    
    prune_parser = subparsers.add_parser("prune-cache")
    prune_parser.add_argument("--min-free-gb", type=int, default=50, help="Eviction floor. Above this, nothing is touched.")
    prune_parser.add_argument("--headroom-gb", type=int, default=20, help="When evicting, free up to floor+headroom so the next deploy doesn't re-trigger immediately.")
    prune_parser.add_argument("--dry-run", action="store_true", help="Report what would be evicted and why, without deleting anything. Safe against production.")

    prune_ray_logs_parser = subparsers.add_parser("prune-ray-logs", help="Age-based cleanup of persisted Ray/crash logs under ~/.cache/ray-logs. Not free-space-gated like prune-cache -- runs older than --retention-days are removed regardless of disk pressure.")
    prune_ray_logs_parser.add_argument("--retention-days", type=int, default=None, help="Defaults to cluster_config.yaml's tuning.crash_log_retention_days.")
    prune_ray_logs_parser.add_argument("--dry-run", action="store_true", help="Report what would be evicted and its age, without deleting anything. Safe against production.")

    correct_ledger_parser = subparsers.add_parser("correct-ledger", help="Manually correct a model_ledger.json entry's lifetime totals and last_seen_raw checkpoint. With no args, auto-detects whatever's currently deployed and its live /metrics values. Pass --model/--topo/--prompt-tokens/--gen-tokens explicitly to override or to correct a different (not currently loaded) key. One-off repair tool, not for routine use -- see correct_ledger_entry()'s docstring.")
    correct_ledger_parser.add_argument("--model", default=None, help="Omit to auto-detect the currently-deployed model's catalog key.")
    correct_ledger_parser.add_argument("--topo", default=None, help="Omit to auto-detect (1_node/2_node) from what's currently running.")
    correct_ledger_parser.add_argument("--prompt-tokens", type=float, default=None, help="Omit to auto-fetch live vllm:prompt_tokens_total from the currently-serving host.")
    correct_ledger_parser.add_argument("--gen-tokens", type=float, default=None, help="Omit to auto-fetch live vllm:generation_tokens_total from the currently-serving host.")
    correct_ledger_parser.add_argument("--draft-tokens", type=float, default=None)
    correct_ledger_parser.add_argument("--accepted-tokens", type=float, default=None)
    correct_ledger_parser.add_argument("--dry-run", action="store_true", help="Preview the change without writing anything.")
    correct_ledger_parser.add_argument("--force", action="store_true", help="Allow overwriting with smaller values than currently recorded.")

    flush_parser = subparsers.add_parser("flush-model-cache", help="Clear one model's HuggingFace weights cache (and optionally JIT caches) to recover from corruption or reclaim space for a retired model.")
    flush_parser.add_argument("--model", required=True, help="Catalog key (live or historically-recorded), or a raw HF repo_id (org/repo) as a last resort.")
    flush_parser.add_argument("--jit", action="store_true", help="Also wipe ALL JIT/compute caches on the target host(s) -- affects every model's compiled kernels on that host, not just this one.")
    flush_parser.add_argument("--dry-run", action="store_true", help="Report what would be deleted and its size, without deleting anything.")
    flush_parser.add_argument("--force", action="store_true", help="Proceed even if the model appears currently loaded (see the command's own caveat about this check's reliability on 2-node Ray deploys).")

    subparsers.add_parser("list-cached-models", help="Cross-reference cached model weights against the live catalog and deploy history to surface active/retired/orphaned caches worth reviewing. Read-only.")

    subparsers.add_parser("ipc-inventory", help="Read-only snapshot of SysV shared memory/semaphores and /dev/shm usage per host. Safe against production.")

    sweep_ipc_parser = subparsers.add_parser("sweep-ipc-orphans", help="Remove SysV shared memory segments with zero attached processes (provably orphaned, safe). Runs automatically as part of teardown; also available standalone.")
    sweep_ipc_parser.add_argument("--dry-run", action="store_true", help="Report what would be removed and its size, without deleting anything.")

    cli_parser = subparsers.add_parser("cli")
    cli_sub = cli_parser.add_subparsers(dest="cli_action")

    for cmd in ["status", "teardown", "menu", "cache-inventory", "list-cached-models", "ipc-inventory"]:
        cli_sub.add_parser(cmd)
        
    cli_sub.add_parser("daemon").add_argument("--port", type=int, default=5001)

    cli_deploy = cli_sub.add_parser("deploy")
    cli_deploy.add_argument("--model", required=True)
    cli_deploy.add_argument("--nodes", type=int, default=2, choices=[1, 2])
    cli_deploy.add_argument("--head", default=PRIMARY_HOST)
    cli_deploy.add_argument("--wait", action="store_true")
    cli_deploy.add_argument("--benchmark", action="store_true")
    cli_deploy.add_argument("--dry-run", action="store_true", help="Print the docker run command(s) this deploy would send, without SSHing or executing anything")
    cli_deploy.add_argument("-y", "--yes", action="store_true")

    cli_logs = cli_sub.add_parser("logs")
    cli_logs.add_argument("--host", default=PRIMARY_HOST)
    cli_logs.add_argument("--tail", type=int, default=40)

    cli_auth = cli_sub.add_parser("authorize-key")
    cli_auth.add_argument("--key", required=True)
    
    cli_prune = cli_sub.add_parser("prune-cache")
    cli_prune.add_argument("--min-free-gb", type=int, default=50)
    cli_prune.add_argument("--headroom-gb", type=int, default=20)
    cli_prune.add_argument("--dry-run", action="store_true")

    cli_prune_ray_logs = cli_sub.add_parser("prune-ray-logs")
    cli_prune_ray_logs.add_argument("--retention-days", type=int, default=None)
    cli_prune_ray_logs.add_argument("--dry-run", action="store_true")

    cli_correct_ledger = cli_sub.add_parser("correct-ledger")
    cli_correct_ledger.add_argument("--model", default=None)
    cli_correct_ledger.add_argument("--topo", default=None)
    cli_correct_ledger.add_argument("--prompt-tokens", type=float, default=None)
    cli_correct_ledger.add_argument("--gen-tokens", type=float, default=None)
    cli_correct_ledger.add_argument("--draft-tokens", type=float, default=None)
    cli_correct_ledger.add_argument("--accepted-tokens", type=float, default=None)
    cli_correct_ledger.add_argument("--dry-run", action="store_true")
    cli_correct_ledger.add_argument("--force", action="store_true")

    cli_flush = cli_sub.add_parser("flush-model-cache")
    cli_flush.add_argument("--model", required=True)
    cli_flush.add_argument("--jit", action="store_true")
    cli_flush.add_argument("--dry-run", action="store_true")
    cli_flush.add_argument("--force", action="store_true")

    cli_sweep_ipc = cli_sub.add_parser("sweep-ipc-orphans")
    cli_sweep_ipc.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()
    subcommand = args.subcommand
    if subcommand == "cli": subcommand = getattr(args, "cli_action", None) or "menu"

    if subcommand == "daemon":
        if not HAS_FASTAPI: sys.exit("[-] Error: fastapi and uvicorn are required for daemon mode.")
        
        signal.signal(signal.SIGINT, handle_shutdown)
        signal.signal(signal.SIGTERM, handle_shutdown)
        
        port = getattr(args, "port", 5001)
        print(f"[+] dgx-orchestrator daemon starting -- version: {ORCHESTRATOR_VERSION}")
        t = threading.Thread(target=_telemetry_daemon_loop, daemon=True)
        t.start()
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")

    elif subcommand == "status": print(json.dumps(get_cluster_status(), indent=2))
    elif subcommand == "teardown": print(json.dumps(execute_teardown(), indent=2))
    elif subcommand == "deploy": print(json.dumps(execute_deployment(args.model, args.nodes, args.head, os.environ.get("USER") or getpass.getuser(), wait=getattr(args, "wait", False), run_benchmark=getattr(args, "benchmark", False), dry_run=getattr(args, "dry_run", False)), indent=2))
    elif subcommand == "logs": print("\n".join(get_container_logs(args.host, args.tail).get("logs", [])))
    elif subcommand == "authorize-key": print(json.dumps(authorize_user_key(args.key), indent=2))
    elif subcommand == "cache-inventory":
        inv = cache_inventory()
        for host, data in inv["hosts"].items():
            if data.get("status") != "ok":
                print(f"[{host}] ERROR: {data.get('message')}")
                continue
            print(f"\n=== {host} === (mount: {data['mount_options']}, atime_reliable={data['atime_reliable']}, "
                  f"free: {data['disk_free_gb']}/{data['disk_total_gb']} GB)")
            for label, r in data["roots"].items():
                if not r["exists"]:
                    continue
                print(f"  {label}: {r['entry_count']} entries, {r['total_gb']} GB")
                if r["oldest"]:
                    print(f"    oldest: {r['oldest']['name']} ({r['oldest']['age_days']}d, {round(r['oldest']['bytes']/(1024**3), 2)} GB)")
                if r["newest"]:
                    print(f"    newest: {r['newest']['name']} ({r['newest']['age_days']}d, {round(r['newest']['bytes']/(1024**3), 2)} GB)")
    elif subcommand == "prune-cache":
        res = prune_cluster_cache(getattr(args, "min_free_gb", 50), getattr(args, "headroom_gb", 20), getattr(args, "dry_run", False))
        for host, s in res["details"].items():
            if s.get("status") != "ok":
                print(f"[{host}] ERROR: {s.get('message')}")
                continue
            verb = "WOULD EVICT" if s["dry_run"] else "EVICTED"
            print(f"\n=== {host} === free: {s['free_before_gb']} GB (floor {getattr(args, 'min_free_gb', 50)} GB, atime_reliable={s['atime_reliable']})")
            if s["entries_evicted"] == 0:
                print("  Above floor -- nothing considered.")
            else:
                print(f"  {verb} {s['entries_evicted']}/{s['entries_total']} entries, {s['gb_freed']} GB, oldest-first.")
        print("\n--- full detail ---")
        print(json.dumps(res, indent=2))
    elif subcommand == "prune-ray-logs":
        res = prune_cluster_ray_logs(getattr(args, "retention_days", None), getattr(args, "dry_run", False))
        for host, s in res["details"].items():
            if s.get("status") != "ok":
                print(f"[{host}] ERROR: {s.get('message')}")
                continue
            verb = "WOULD EVICT" if s["dry_run"] else "EVICTED"
            print(f"\n=== {host} ===")
            if s["runs_evicted"] == 0:
                print(f"  {s['runs_kept']} run(s) within retention -- nothing to evict.")
            else:
                print(f"  {verb} {s['runs_evicted']} run(s), {s['gb_freed']} GB, kept {s['runs_kept']}.")
        print("\n--- full detail ---")
        print(json.dumps(res, indent=2))
    elif subcommand == "correct-ledger":
        res = correct_ledger_entry(
            getattr(args, "model", None), getattr(args, "topo", None),
            getattr(args, "prompt_tokens", None), getattr(args, "gen_tokens", None),
            getattr(args, "draft_tokens", None), getattr(args, "accepted_tokens", None),
            getattr(args, "dry_run", False), getattr(args, "force", False),
        )
        if res["status"] != "success":
            print(f"ERROR: {res['message']}")
            if "available_keys" in res:
                print("Available keys:")
                for k in res["available_keys"]:
                    print(f"    {k}")
        else:
            if res["auto_detected"]:
                print(f"Auto-detected from currently-serving host: {res['detected_serving_host']}")
            verb = "Would set" if res["dry_run"] else "Set"
            print(f"Key: {res['key']}")
            print(f"Current lifetime:     {res['current_lifetime']}")
            print(f"Current last_seen_raw: {res['current_last_seen_raw']}")
            print(f"\n{verb} lifetime:      {res['new_lifetime']}")
            print(f"{verb} last_seen_raw:  {res['new_last_seen_raw']}")
            if res["backup_path"]:
                print(f"\nBackup written to: {res['backup_path']}")
            if res["dry_run"]:
                print("\n--dry-run: no changes written.")
        print("\n--- full detail ---")
        print(json.dumps(res, indent=2))
    elif subcommand == "flush-model-cache":
        res = flush_model_cache(args.model, include_jit=getattr(args, "jit", False),
                                 dry_run=getattr(args, "dry_run", False), force=getattr(args, "force", False))
        if res.get("status") != "success":
            print(json.dumps(res, indent=2))
        else:
            for host, s in res["details"].items():
                if s.get("status") != "ok":
                    print(f"[{host}] ERROR: {s.get('message')}")
                    continue
                verb = "Would flush" if s["dry_run"] else "Flushed"
                print(f"\n=== {host} === {verb} HF cache for {res['hf_path']}: {s['hf_cache_gb']} GB ({s['hf_cache_action']})")
                if res["include_jit"]:
                    print(f"  Also {verb.lower()} {s['jit_entries_flushed']} JIT cache entries ({s['jit_gb_flushed']} GB) -- ALL models on this host affected, not just '{res['model']}'.")
            print("\n--- full detail ---")
            print(json.dumps(res, indent=2))
    elif subcommand == "list-cached-models":
        res = find_cached_models()
        for host, data in res["hosts"].items():
            if data.get("status") != "ok":
                print(f"[{host}] ERROR: {data.get('message')}")
                continue
            print(f"\n=== {host} ===")
            if not data["models"]:
                print("  No cached model weights found.")
                continue
            for m in data["models"]:
                label = m["matched_model"] or m["cache_dirname"]
                print(f"  [{m['status']}] {label} -- {m['gb']} GB, {m['age_days']}d old")
        print("\n--- full detail ---")
        print(json.dumps(res, indent=2))
    elif subcommand == "ipc-inventory":
        inv = ipc_inventory()
        for host, data in inv["hosts"].items():
            if data.get("status") != "ok":
                print(f"[{host}] ERROR: {data.get('message')}")
                continue
            print(f"\n=== {host} ===")
            print(f"  SysV shm: {data['shm_segments_total']} segments "
                  f"({data['shm_segments_attached']} attached, {data['shm_attached_gb']} GB | "
                  f"{data['shm_segments_orphaned']} orphaned, {data['shm_orphaned_gb']} GB)")
            print(f"  Semaphore arrays: {data['semaphore_count']}")
            if data['dev_shm_disk_total_gb'] is not None:
                print(f"  /dev/shm disk: {data['dev_shm_disk_free_gb']}/{data['dev_shm_disk_total_gb']} GB free")
            if data["dev_shm_files"]:
                print("  /dev/shm files (largest first):")
                for f in data["dev_shm_files"][:10]:
                    print(f"    {f['name']} -- {f['gb']} GB, {f['age_days']}d old")
        print("\n--- full detail ---")
        print(json.dumps(inv, indent=2))
    elif subcommand == "sweep-ipc-orphans":
        res = sweep_ipc_orphans(dry_run=getattr(args, "dry_run", False))
        print("\n--- full detail ---")
        print(json.dumps(res, indent=2))
    else: interactive_menu()

if __name__ == "__main__":
    main()
