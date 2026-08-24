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
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
import getpass
import json
import os
import pathlib
import re
import shlex
import signal
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
from common.recipes import build_catalog_response
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
MODELS_YAML_PATH = BASE_DIR / "models.yaml"
LEDGER_PATH = BASE_DIR / "model_ledger.json"
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

HOSTS = legacy_hosts_dict()

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
        res = subprocess.run(["ipcrm", "-m", shmid], capture_output=True, text=True)
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
    "phase": "idle",  # idle | signaling | stopping | removing | sweeping | done
    "message": "Idle",
    "last_run": None
}
TEARDOWN_STATE_LOCK = threading.Lock()

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
        self.lock = threading.Lock()
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
                
                self.start_p_tok = p_tok
                self.start_g_tok = g_tok
                self.start_d_tok = d_tok
                self.start_a_tok = a_tok

                self.flushed_p_tok = p_tok
                self.flushed_g_tok = g_tok
                self.flushed_d_tok = d_tok
                self.flushed_a_tok = a_tok
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
            
            data = {}
            if LEDGER_PATH.exists():
                try: data = json.loads(LEDGER_PATH.read_text())
                except Exception: pass
                
            key = f"{self.model}::{self.topo}"
            if key not in data or not isinstance(data[key], dict):
                data[key] = {"cached": [], "compiled": [], "downloaded": [], "lifetime": {"in": 0, "out": 0, "draft": 0, "accepted": 0}}
                
            if "lifetime" not in data[key]:
                data[key]["lifetime"] = {"in": 0, "out": 0, "draft": 0, "accepted": 0}
                
            data[key]["lifetime"]["in"] += int(p_diff)
            data[key]["lifetime"]["out"] += int(g_diff)
            data[key]["lifetime"]["draft"] += int(d_diff)
            data[key]["lifetime"]["accepted"] += int(a_diff)
            
            try: LEDGER_PATH.write_text(json.dumps(data, indent=2))
            except Exception: pass

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

def check_vllm_health(head_ip: str = "10.0.14.43", port: int = 8000) -> bool:
    url = f"http://{head_ip}:{port}/health"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=2) as response:
            return response.status == 200
    except Exception:
        return False

def wait_for_cluster_ready(head_ip: str = "10.0.14.43", timeout_sec: int = 900, poll_interval: int = 15) -> bool:
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
    data = {}
    if LEDGER_PATH.exists():
        try: data = json.loads(LEDGER_PATH.read_text())
        except Exception: data = {}
    key = f"{model}::{topo_key}"
    
    if key not in data or isinstance(data[key], list):
        data[key] = {"cached": [], "compiled": [], "downloaded": [], "lifetime": {"in": 0, "out": 0, "draft": 0, "accepted": 0}}
        
    existing = data[key].get(load_type, [])
    if existing and existing[-1] == duration_sec:
        return

    data[key].setdefault(load_type, []).append(duration_sec)
    data[key][load_type] = data[key][load_type][-20:]
    try: LEDGER_PATH.write_text(json.dumps(data, indent=2))
    except Exception: pass

def get_estimated_load_time(model: str, topo_key: str, load_type: str = "cached") -> tuple[int, bool]:
    default_ests = {"cached": 180, "compiled": 1500, "downloaded": 4500}
    default_est = default_ests.get(load_type, 180)
    
    if "deepseek" in model.lower():
        default_ests = {"cached": 700, "compiled": 1800, "downloaded": 5000}
        default_est = default_ests.get(load_type, 700)
        
    if not LEDGER_PATH.exists():
        return default_est, False
    try:
        data = json.loads(LEDGER_PATH.read_text())
        key_data = data.get(f"{model}::{topo_key}", {})
        if isinstance(key_data, dict):
            times = key_data.get(load_type, [])
            if times:
                return int(sum(times) / len(times)), True
    except Exception:
        pass
    return default_est, False

def get_vllm_metrics(head_ip: str = "10.0.14.43", port: int = 8000) -> dict:
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
    except Exception:
        pass
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
    data = {}
    if HF_PATH_LEDGER_PATH.exists():
        try: data = json.loads(HF_PATH_LEDGER_PATH.read_text())
        except Exception: data = {}
    data[catalog_key] = {
        "hf_path": hf_path,
        "cache_dirname": _hf_cache_dirname(hf_path),
        "last_deployed": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    try: HF_PATH_LEDGER_PATH.write_text(json.dumps(data, indent=2))
    except Exception: pass

def _load_hf_path_ledger() -> dict:
    if not HF_PATH_LEDGER_PATH.exists():
        return {}
    try:
        return json.loads(HF_PATH_LEDGER_PATH.read_text())
    except Exception:
        return {}

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
        # ipcrm on a segment owned by a different uid (containers commonly
        # run as root inside; the SSH user may not be root) needs
        # elevated privileges, same as the sudo usage already established
        # elsewhere in this file (e.g. sudo nvidia-smi, sudo kill).
        cmd = ["sudo", "python3", "-c", _REMOTE_IPC_SWEEP_SCRIPT, "1" if dry_run else "0"]
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
            except Exception:
                loaded_model = "Active Container"
        else:
            ps_res = run_ssh(ip, user, ["docker", "exec", c_name, "ps", "aux"], timeout=10)
            if ps_res.returncode == 0 and "--model" in ps_res.stdout:
                try:
                    for part in ps_res.stdout.split():
                        if "/" in part and any(fam in part for fam in ["DeepSeek", "Qwen", "Llama", "model", "gemma", "Nemotron", "Muse", "Glimmer"]):
                            loaded_model = part.split("/")[-1]
                            break
                except Exception:
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

def _finalize_host_status(host: str, meta: dict, info: dict, cluster_ready: bool, container_info: dict, serving_host: str, catalog_models: dict) -> tuple:
    ip = meta["ip"]
    user = None
    telemetry = get_lightweight_telemetry(ip, user)

    if not info.get("reachable"):
        return host, {
            "ip": ip, "docker_status": "UNREACHABLE", "container_name": "None",
            "container_state": "NONE", "active_model": "None", "model_status": "NONE",
            "eta_seconds": 0, "eta_display": "N/A", "telemetry": telemetry
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

    matched_key = _resolve_catalog_key(loaded_model, catalog_models)

    eta_seconds = 0
    eta_display = "Ready" if cluster_ready else "N/A"
    topo_key = "2_node" if active_container in [ContainerRole.HEAD, ContainerRole.WORKER] else "1_node"

    if is_crashed:
        model_status = f"CRASHED ({container_state.upper()})"
        eta_display = "Check Docker Logs"
        eta_seconds = 0
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
        "telemetry": telemetry
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

    serving_host = "spark-4"
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
    matched_model = _resolve_catalog_key(raw_loaded_model, catalog_models)
    topo = "2_node" if len([h for h, i in container_info.items() if i.get("active_container") != "None"]) > 1 else "1_node"
    if cluster_ready:
        SESSION_TRACKER.update(vllm_metrics, matched_model, topo)

    with BENCHMARK_STATE_LOCK:
        is_benchmarking = BENCHMARK_STATE["running"]
        benchmark_msg = BENCHMARK_STATE["message"]

    with TEARDOWN_STATE_LOCK:
        is_tearing_down = TEARDOWN_STATE["running"]
        teardown_phase = TEARDOWN_STATE["phase"]
        teardown_msg = TEARDOWN_STATE["message"]

    status_data = {
        "server_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S EST"),
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
                "eta_seconds": 0, "eta_display": "N/A", "telemetry": {}
            }
        else:
            _, host_status = result
            status_data["hosts"][host] = host_status

    # Strictly guarded worker state mirroring
    head_s = status_data["hosts"].get("spark-4")
    worker_s = status_data["hosts"].get("spark-3")
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
        print(f"[!] get_cluster_status(): in-flight computation failed or timed out: {e}")
        with _STATUS_LOCK:
            if _STATUS_CACHE is not None:
                return _STATUS_CACHE
        raise

    with _STATUS_LOCK:
        _STATUS_CACHE = result
        _STATUS_CACHE_TS = time.monotonic()

    return result

def enrich_catalog(catalog_dict: dict) -> dict:
    """Fail-soft catalog enricher with ultra-permissive MTP and model metadata checks."""
    models = catalog_dict.get("catalog", {}).get("models", {})
    if not isinstance(models, dict):
        return catalog_dict

    ledger_data = {}
    if LEDGER_PATH.exists():
        try: ledger_data = json.loads(LEDGER_PATH.read_text())
        except Exception: pass

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
                vllm_args_lower = vllm_args.lower()
                
                seq_match = re.search(r'--max-num-seqs\s+(\d+)', vllm_args)
                t_data["max_num_seqs"] = seq_match.group(1) if seq_match else "Uncapped"

                kv_match = re.search(r'--kv-cache-dtype\s+([^\s]+)', vllm_args)
                t_data["kv_dtype"] = (kv_match.group(1).upper() + " KV") if kv_match else "AUTO KV"
                
                l_key = f"{m_key}::{t_key}"
                lt_stats = ledger_data.get(l_key, {}).get("lifetime", {})
                d_tok = lt_stats.get("draft", 0)
                a_tok = lt_stats.get("accepted", 0)

                mtp_keywords = ["speculative", "mtp", "draft", "nextn", "proposal", "ngram", "lookahead"]
                has_spec_flag = any(kw in vllm_args_lower for kw in mtp_keywords)
                has_spec_name = any(kw in m_key_lower or kw in hf_path for kw in ["flash", "deepseek-v4", "mtp", "speculative"])
                
                t_data["mtp_enabled"] = has_spec_flag or has_spec_name or (d_tok > 0)

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

def load_model_catalog() -> dict:
    if os.environ.get("USE_LEGACY_CATALOG") == "1": raw_cat = _load_model_catalog_legacy()
    else: raw_cat = build_catalog_response()
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
        proc_futures = {h: WORKER_POOL.submit(_teardown_host_processes, ip) for h, ip in ips.items()}
        internal_futures = {h: WORKER_POOL.submit(_teardown_host_container_internals, ip) for h, ip in ips.items()}
        for f in proc_futures.values(): f.result()
        for f in internal_futures.values(): f.result()

        _set_teardown_state(True, "stopping",
                             f"Gracefully stopping containers on {host_list} (up to {TEARDOWN_GRACE_SEC}s)...")
        stop_futures = {h: WORKER_POOL.submit(_teardown_host_containers, ip) for h, ip in ips.items()}
        for f in stop_futures.values(): f.result()

        _set_teardown_state(True, "removing", f"Removing containers on {host_list}...")
        rm_futures = {h: WORKER_POOL.submit(_teardown_host_rm, ip) for h, ip in ips.items()}
        for h, f in rm_futures.items():
            res = f.result()
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
        # showing "in progress" forever with no way to clear it.
        _set_teardown_state(False, "done", f"Teardown complete for {host_list}.", mark_last_run=True)

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
        head_ip = HOSTS[head]["ip"] if head in HOSTS else "10.0.14.43"
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
        with BENCHMARK_STATE_LOCK:
            BENCHMARK_STATE["running"] = False

def execute_standalone_benchmark(head: str, nodes: int, model_key: Optional[str] = None) -> dict:
    """Launches benchmark.py asynchronously without blocking HTTP response or tearing down containers."""
    with BENCHMARK_STATE_LOCK:
        if BENCHMARK_STATE["running"]:
            return {"status": "error", "message": "A benchmark pass is already running in the background."}

    head_ip = HOSTS[head]["ip"] if head in HOSTS else "10.0.14.43"
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

def _execute_deployment_impl(model: str, nodes: int, head: str, user_id: str, wait: bool = False, run_benchmark: bool = False, dry_run: bool = False) -> dict:
    deploy_start_time = time.time()
    docker_run_commands: dict = {}

    if nodes not in (1, 2): return {"status": "error", "message": f"Invalid 'nodes' value {nodes!r}: must be 1 or 2."}
    if head not in HOSTS: return {"status": "error", "message": f"Invalid 'head' value {head!r}: must be one of {list(HOSTS.keys())}."}

    target_hosts = ["spark-4", "spark-3"] if nodes == 2 else [head]
    catalog_resp = load_model_catalog()
    models_catalog = catalog_resp.get("catalog", {}).get("models", {})

    if model not in models_catalog: return {"status": "error", "message": f"Model '{model}' not defined in catalog."}

    model_config = models_catalog[model]
    topologies = model_config.get("topologies", {})
    topo_key = "2_node" if nodes == 2 else "1_node"

    if topo_key not in topologies: return {"status": "error", "message": f"Topology '{topo_key}' not supported for model '{model}'."}

    offline_mode = False
    if NETWORK_STATE_FILE.exists():
        try: offline_mode = "OFFLINE" in NETWORK_STATE_FILE.read_text().strip()
        except Exception: pass
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
    try: vllm_args_list = shlex.split(vllm_args_raw)
    except Exception: vllm_args_list = vllm_args_raw.split()

    use_ray = (nodes > 1) and ("--distributed-executor-backend" in vllm_args_list) and ("ray" in vllm_args_list)
    tuning = load_cluster_config().tuning

    if not dry_run:
        SESSION_TRACKER._commit_session()
        SESSION_TRACKER.active = False
        _execute_teardown_impl(target_hosts=target_hosts)
        for h in target_hosts:
            ip = HOSTS[h]["ip"]
            run_ssh(ip, None, ["sudo", "nvidia-smi", "-lgc", tuning.gpu_clock_lock], timeout=10)
            run_ssh(ip, None, ["bash", "-c", "mkdir -p ~/.cache/tilelang ~/.cache/deepgemm ~/.cache/triton ~/.cache/vllm ~/.cache/flashinfer"], timeout=10)

    default_img = catalog_resp.get("catalog", {}).get("default_image", load_cluster_config().default_image)
    image_tag = model_config.get("image", default_img)
    compat_mount = "/dev/null:/etc/ld.so.conf.d/00-cuda-compat.conf"

    def _jit_cache_mounts_and_env(vol_mount: str) -> tuple[list[str], list[str]]:
        host_hf_dir = vol_mount.split(":", 1)[0]
        host_cache_root = str(Path(host_hf_dir).parent)
        mounts = [
            "-v", f"{host_cache_root}/triton:/root/.cache/triton",
            "-v", f"{host_cache_root}/tilelang:/root/.cache/tilelang",
            "-v", f"{host_cache_root}/deepgemm:/root/.cache/deepgemm",
            "-v", f"{host_cache_root}/vllm:/root/.cache/vllm",
            "-v", f"{host_cache_root}/flashinfer:/root/.cache/flashinfer",
            "-v", f"{host_cache_root}/nv_compute_cache:/root/.nv/ComputeCache"
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
        return mounts, env

    head_ip = HOSTS[head]["ip"]
    hf_token = get_hf_token()

    if nodes == 1:
        ip = HOSTS[head]["ip"]
        vol_mount = load_cluster_config().hosts[head].volume_mount
        jit_mounts, jit_env = _jit_cache_mounts_and_env(vol_mount)
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

        docker_cmd = [
            "docker", "run", "-d", "--init",
            "--name", ContainerRole.STANDALONE,
            "--net=host", "--ipc=host", f"--shm-size={tuning.shm_size_1node}",
            "--gpus", "all",
            "-v", vol_mount,
            "-v", compat_mount
        ] + jit_mounts + env_flags + [image_tag] + container_args

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
            jit_mounts, jit_env = _jit_cache_mounts_and_env(vol_mount)
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
            ] + jit_mounts + env_flags + [image_tag] + entrypoint_cmd

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
        return {
            "status": "dry_run",
            "message": f"Dry-run for {model} across {nodes} node(s) - no SSH connections made, nothing executed.",
            "targets": target_hosts,
            "head": head,
            "docker_run_commands": docker_run_commands,
        }

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
    print(f"Server Time: {status['server_time']} | Mode: {status['network_mode']} | API: {'READY' if status['cluster_ready'] else 'OFFLINE'} (serving: {status.get('serving_host', 'spark-4')}) | TPS: {status.get('system_tps', 0.0)} tok/s | Streams: {status.get('running_requests', 0)} active ({status.get('waiting_requests', 0)} queued)\n")

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
        head = "spark-4"

        if nodes == 1:
            h_choice = input("Target head node for 1-node deploy (1: spark-4, 2: spark-3) [1]: ").strip()
            if h_choice == "2": head = "spark-3"

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
        head: str = "spark-4"
        user_id: str = "dashboard_user"
        wait: bool = False
        run_benchmark: bool = False
        dry_run: bool = False

    class BenchmarkRequest(BaseModel):
        head: str = "spark-4"
        nodes: Literal[1, 2] = 2
        model: Optional[str] = None  # catalog key, threaded to benchmark.py --model-key for ledger join

    class NetworkToggleRequest(BaseModel):
        offline: bool

    class PruneCacheRequest(BaseModel):
        min_free_gb: int = 50
        headroom_gb: int = 20
        dry_run: bool = False

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
    def api_teardown(): return execute_teardown()

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

def _telemetry_daemon_loop():
    """Background polling loop for maintaining lifetime and session analytics."""
    while True:
        try:
            get_cluster_status()
        except Exception:
            pass
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
    deploy_parser.add_argument("--head", default="spark-4")
    deploy_parser.add_argument("--wait", action="store_true", help="Block until HTTP /health passes")
    deploy_parser.add_argument("--benchmark", action="store_true", help="Automatically run benchmark.py when ready")
    deploy_parser.add_argument("--dry-run", action="store_true", help="Print the docker run command(s) this deploy would send, without SSHing or executing anything")
    deploy_parser.add_argument("-y", "--yes", action="store_true")

    logs_parser = subparsers.add_parser("logs")
    logs_parser.add_argument("--host", default="spark-4")
    logs_parser.add_argument("--tail", type=int, default=40)

    auth_parser = subparsers.add_parser("authorize-key")
    auth_parser.add_argument("--key", required=True)
    
    prune_parser = subparsers.add_parser("prune-cache")
    prune_parser.add_argument("--min-free-gb", type=int, default=50, help="Eviction floor. Above this, nothing is touched.")
    prune_parser.add_argument("--headroom-gb", type=int, default=20, help="When evicting, free up to floor+headroom so the next deploy doesn't re-trigger immediately.")
    prune_parser.add_argument("--dry-run", action="store_true", help="Report what would be evicted and why, without deleting anything. Safe against production.")

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
    cli_deploy.add_argument("--head", default="spark-4")
    cli_deploy.add_argument("--wait", action="store_true")
    cli_deploy.add_argument("--benchmark", action="store_true")
    cli_deploy.add_argument("--dry-run", action="store_true", help="Print the docker run command(s) this deploy would send, without SSHing or executing anything")
    cli_deploy.add_argument("-y", "--yes", action="store_true")

    cli_logs = cli_sub.add_parser("logs")
    cli_logs.add_argument("--host", default="spark-4")
    cli_logs.add_argument("--tail", type=int, default=40)

    cli_auth = cli_sub.add_parser("authorize-key")
    cli_auth.add_argument("--key", required=True)
    
    cli_prune = cli_sub.add_parser("prune-cache")
    cli_prune.add_argument("--min-free-gb", type=int, default=50)
    cli_prune.add_argument("--headroom-gb", type=int, default=20)
    cli_prune.add_argument("--dry-run", action="store_true")

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
