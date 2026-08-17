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
import getpass
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
import yaml

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
SHARED_KEY_PATH = BASE_DIR / "id_dgx_orchestrator"
MODELS_YAML_PATH = BASE_DIR / "models.yaml"
LOAD_TIMES_PATH = BASE_DIR / "load_times.json"

HOSTS = {
    "spark-4": {"ip": "10.0.14.43", "alias": "spark-9dbe", "role": "head"},
    "spark-3": {"ip": "10.0.14.41", "alias": "spark-6e63", "role": "worker"}
}

NETWORK_STATE_FILE = BASE_DIR / ".network_mode"
ANSI_ESCAPE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-9?]*[ -/]*[@-~])')


# --- Core Helpers ---
def resolve_user_identity_key() -> str:
    """
    OpenSSH strictness (0600) workaround for shared keys (0640).
    Auto-stages a copy of the shared key into ~/.ssh/id_dgx_orchestrator to prevent
    permission denied errors when routing cluster commands.
    """
    user_ssh_dir = Path.home() / ".ssh"
    user_ssh_dir.mkdir(parents=True, exist_ok=True)
    target_key = user_ssh_dir / "id_dgx_orchestrator"

    if SHARED_KEY_PATH.exists():
        try:
            if not target_key.exists() or target_key.stat().st_mtime < SHARED_KEY_PATH.stat().st_mtime:
                shutil.copy2(SHARED_KEY_PATH, target_key)
                os.chmod(target_key, 0o600)
            return str(target_key)
        except Exception:
            pass
    
    default_key = user_ssh_dir / "id_ed25519"
    if default_key.exists():
        return str(default_key)
    
    return str(SHARED_KEY_PATH)

def get_hf_token() -> str:
    """Extracts HuggingFace authentication token with safe fallbacks and warnings."""
    if "HF_TOKEN" in os.environ and os.environ["HF_TOKEN"].strip():
        return os.environ["HF_TOKEN"].strip()
    
    secrets_file = BASE_DIR / ".secrets"
    if secrets_file.exists():
        try:
            for line in secrets_file.read_text().splitlines():
                if line.startswith("HF_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except PermissionError:
            print(f"[!] Warning: You do not have permission to read {secrets_file}. Checking local cache...")
        except Exception:
            pass

    hf_token_file = Path.home() / ".cache" / "huggingface" / "token"
    if hf_token_file.exists():
        try:
            return hf_token_file.read_text().strip()
        except Exception:
            pass

    print("[!] Warning: No HF_TOKEN found in env, .secrets, or ~/.cache. Gated models may fail to download.")
    return ""

def run_ssh(ip: str, user: str, command_list: list, capture: bool = True, timeout: int = 10) -> subprocess.CompletedProcess:
    """Executes remote commands via SSH with quoted token evaluation."""
    key_path = resolve_user_identity_key()
    quoted_remote_cmd = " ".join(shlex.quote(str(arg)) for arg in command_list)
    ssh_cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=5",
        "-i", key_path,
        f"{user}@{ip}",
        quoted_remote_cmd
    ]

    try:
        res = subprocess.run(ssh_cmd, capture_output=capture, text=True, timeout=timeout)
        return res
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args=ssh_cmd, returncode=124, stdout="", stderr="Command execution timed out.")
    except Exception as e:
        return subprocess.CompletedProcess(args=ssh_cmd, returncode=1, stdout="", stderr=str(e))

def get_lightweight_telemetry(ip: str, user: str) -> dict:
    """Queries nvidia-smi telemetry line-by-line for Grace Blackwell unified memory."""
    cmd = ["/usr/bin/nvidia-smi", "--query-gpu=temperature.gpu,utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"]
    res = run_ssh(ip, user, cmd, capture=True, timeout=10)
    
    if res.returncode == 0 and res.stdout.strip():
        for line in res.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2:
                temp_str, util_str = parts[0], parts[1]
                if temp_str.isdigit() and util_str.isdigit():
                    mem_used = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else "Unified"
                    mem_total = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else "131072"
                    return {
                        "gpu_temp_c": int(temp_str),
                        "gpu_util_pct": int(util_str),
                        "mem_used_mb": mem_used,
                        "mem_total_mb": mem_total
                    }
    return {}

def check_vllm_health(head_ip: str = "10.0.14.43", port: int = 8000) -> bool:
    """Probes the vLLM /health endpoint to check if HTTP API serving is ready."""
    url = f"http://{head_ip}:{port}/health"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=2) as response:
            return response.status == 200
    except Exception:
        return False

def wait_for_cluster_ready(head_ip: str = "10.0.14.43", timeout_sec: int = 900, poll_interval: int = 15) -> bool:
    """Polls the vLLM /health endpoint until HTTP 200 ready state."""
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
    """Parses Docker StartedAt ISO timestamp into unix epoch seconds."""
    try:
        ts_clean = ts_str.split(".")[0].replace("Z", "")
        dt = datetime.datetime.strptime(ts_clean, "%Y-%m-%dT%H:%M:%S")
        return dt.replace(tzinfo=datetime.timezone.utc).timestamp()
    except Exception:
        return time.time()

def detect_model_stage(ip: str, user: str, c_name: str) -> str:
    """Inspects recent log output with ANSI stripping and case insensitivity."""
    res = run_ssh(ip, user, ["docker", "logs", "--tail", "250", c_name], timeout=10)
    raw_text = res.stdout + res.stderr
    clean_text = ANSI_ESCAPE.sub('', raw_text)
    lines = [l.strip().lower() for l in clean_text.splitlines() if l.strip()]

    for line in reversed(lines):
        if any(k in line for k in ["warming up", "warmup", "kv cache", "cuda graph", "mhc", "profiling", "capturing", "graph capture"]):
            return "NOT READY - WARMUP"
        if any(k in line for k in ["tilelang", "deepgemm", "kernel", "compiling", "jit", "tuning", "building"]):
            return "NOT READY - COMPILING KERNELS"
        if any(k in line for k in ["loading weights", "safetensors", "shard", "loading model", "checkpoint"]):
            return "NOT READY - LOADING SHARDS"

    return "NOT READY - INITIALIZING"

def record_load_time(model: str, topo_key: str, duration_sec: int):
    if duration_sec > 1800 or duration_sec < 60: return
    data = {}
    if LOAD_TIMES_PATH.exists():
        try: data = json.loads(LOAD_TIMES_PATH.read_text())
        except Exception: data = {}
    key = f"{model}::{topo_key}"
    
    existing = data.get(key, [])
    if existing and existing[-1] == duration_sec:
        return

    data.setdefault(key, []).append(duration_sec)
    data[key] = data[key][-20:]
    try: LOAD_TIMES_PATH.write_text(json.dumps(data, indent=2))
    except Exception: pass

def get_estimated_load_time(model: str, topo_key: str) -> tuple[int, bool]:
    default_est = 700 if "deepseek" in model.lower() else 180
    if not LOAD_TIMES_PATH.exists():
        return default_est, False
    try:
        data = json.loads(LOAD_TIMES_PATH.read_text())
        times = data.get(f"{model}::{topo_key}", [])
        if times:
            return int(sum(times) / len(times)), True
    except Exception:
        pass
    return default_est, False

def get_vllm_metrics(head_ip: str = "10.0.14.43", port: int = 8000) -> dict:
    """Scrapes vLLM Prometheus endpoint for system throughput (TPS) and request concurrency."""
    metrics = {"tps": 0.0, "running_requests": 0, "waiting_requests": 0}
    try:
        url = f"http://{head_ip}:{port}/metrics"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=1.5) as response:
            content = response.read().decode("utf-8")
            for line in content.splitlines():
                if line.startswith("#"): continue
                if "vllm:avg_generation_throughput_tok_per_s" in line:
                    metrics["tps"] = round(float(line.split()[-1]), 1)
                elif "vllm:num_requests_running" in line:
                    metrics["running_requests"] = int(float(line.split()[-1]))
                elif "vllm:num_requests_waiting" in line:
                    metrics["waiting_requests"] = int(float(line.split()[-1]))
    except Exception:
        pass
    return metrics

def get_cluster_status() -> dict:
    offline_mode = False
    if NETWORK_STATE_FILE.exists():
        try:
            offline_mode = "OFFLINE" in NETWORK_STATE_FILE.read_text().strip()
        except Exception:
            pass

    cluster_ready = check_vllm_health(HOSTS["spark-4"]["ip"])
    vllm_metrics = get_vllm_metrics(HOSTS["spark-4"]["ip"]) if cluster_ready else {"tps": 0.0, "running_requests": 0, "waiting_requests": 0}

    catalog_models = load_model_catalog().get("catalog", {}).get("models", {})

    status_data = {
        "server_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S EST"),
        "network_mode": "Working in OFFLINE mode" if offline_mode else "Working in ONLINE mode",
        "cluster_ready": cluster_ready,
        "system_tps": vllm_metrics["tps"],
        "running_requests": vllm_metrics["running_requests"],
        "waiting_requests": vllm_metrics["waiting_requests"],
        "hosts": {}
    }

    discovered_head_model = "None"

    for host, meta in HOSTS.items():
        ip = meta["ip"]
        user = "tetrel"
        res = run_ssh(ip, user, ["docker", "ps", "-a", "--format", "{{.Names}}::{{.State}}::{{.Image}}"], timeout=10)
        
        if res.returncode == 0:
            active_container, container_state, loaded_model = "None", "None", "None"
            lines = [l.strip() for l in res.stdout.strip().splitlines() if l.strip()]
            for line in lines:
                parts = line.split("::")
                if parts:
                    c_name = parts[0]
                    c_state = parts[1] if len(parts) > 1 else "running"
                    if c_name in ["vllm-standalone", "vllm-head", "vllm-worker"]:
                        active_container = c_name
                        container_state = c_state.lower()
                        
                        inspect_res = run_ssh(ip, user, ["docker", "inspect", c_name, "--format", "{{json .Config.Cmd}}"], timeout=8)
                        if inspect_res.returncode == 0 and "--model" in inspect_res.stdout:
                            try:
                                cmd_parts = json.loads(inspect_res.stdout.strip())
                                if "--model" in cmd_parts:
                                    idx = cmd_parts.index("--model")
                                    if idx + 1 < len(cmd_parts):
                                        loaded_model = cmd_parts[idx + 1].split("/")[-1]
                                        if host == "spark-4":
                                            discovered_head_model = loaded_model
                            except Exception:
                                loaded_model = "Active Container"
                        else:
                            ps_res = run_ssh(ip, user, ["docker", "exec", c_name, "ps", "aux"], timeout=10)
                            if ps_res.returncode == 0 and "--model" in ps_res.stdout:
                                try:
                                    for part in ps_res.stdout.split():
                                        if "/" in part and ("DeepSeek" in part or "Qwen" in part or "Llama" in part or "model" in part or "gemma" in part):
                                            loaded_model = part.split("/")[-1]
                                            if host == "spark-4":
                                                discovered_head_model = loaded_model
                                            break
                                except Exception:
                                    loaded_model = "Active Container"
                            elif discovered_head_model != "None":
                                loaded_model = discovered_head_model
                            else:
                                loaded_model = "Active Container"
                        break

            if active_container != "None" and loaded_model in ["None", "Active Container"] and discovered_head_model != "None":
                loaded_model = discovered_head_model

            # Model key normalization for load_times.json matching
            matched_key = loaded_model
            if catalog_models and isinstance(catalog_models, dict):
                for cat_key, cat_data in catalog_models.items():
                    hf_path = cat_data.get("hf_path", "")
                    if hf_path.endswith(loaded_model) or cat_key == loaded_model or loaded_model in hf_path:
                        matched_key = cat_key
                        break

            eta_seconds = 0
            eta_display = "Ready" if cluster_ready else "N/A"
            topo_key = "2_node" if active_container in ["vllm-head", "vllm-worker"] else "1_node"

            if active_container != "None" and container_state == "running":
                if cluster_ready:
                    model_status = "READY"
                    if host == "spark-4":
                        time_res = run_ssh(ip, user, ["docker", "inspect", active_container, "--format", "{{.State.StartedAt}}"], timeout=5)
                        if time_res.returncode == 0 and time_res.stdout.strip():
                            start_ts = parse_iso_time(time_res.stdout.strip())
                            elapsed = int(time.time() - start_ts)
                            record_load_time(matched_key, topo_key, elapsed)
                else:
                    model_status = detect_model_stage(ip, user, active_container)
                    time_res = run_ssh(ip, user, ["docker", "inspect", active_container, "--format", "{{.State.StartedAt}}"], timeout=5)
                    if time_res.returncode == 0 and time_res.stdout.strip():
                        start_ts = parse_iso_time(time_res.stdout.strip())
                        elapsed = int(time.time() - start_ts)
                        est_total, has_history = get_estimated_load_time(matched_key, topo_key)
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

            telemetry = get_lightweight_telemetry(ip, user)
            status_data["hosts"][host] = {
                "ip": ip,
                "docker_status": "ONLINE",
                "container_name": active_container,
                "container_state": container_state.upper() if active_container != "None" else "NONE",
                "active_model": loaded_model,
                "model_status": model_status,
                "eta_seconds": eta_seconds,
                "eta_display": eta_display,
                "telemetry": telemetry
            }
        else:
            status_data["hosts"][host] = {
                "ip": ip,
                "docker_status": "UNREACHABLE",
                "container_name": "None",
                "container_state": "NONE",
                "active_model": "None",
                "model_status": "NONE",
                "eta_seconds": 0,
                "eta_display": "N/A",
                "telemetry": {}
            }
    return status_data

def load_model_catalog() -> dict:
    if not MODELS_YAML_PATH.exists():
        return {"catalog": {"models": {}}}
    try:
        with open(MODELS_YAML_PATH, "r") as f:
            config = yaml.safe_load(f) or {}
            
        global_hf = config.get('GLOBAL_HF_HUB_OFFLINE', 0)
        global_tf = config.get('GLOBAL_TRANSFORMERS_OFFLINE', 0)

        models = config.get('models', {})
        if isinstance(models, dict):
            for model_name, model_data in models.items():
                if not isinstance(model_data, dict):
                    continue
                topologies = model_data.get('topologies', {})
                if isinstance(topologies, dict):
                    for topo_name, topo_data in topologies.items():
                        if not isinstance(topo_data, dict):
                            continue
                        if 'env_vars' not in topo_data:
                            topo_data['env_vars'] = []
                        
                        if global_hf == 1:
                            topo_data['env_vars'] = [env for env in topo_data['env_vars'] if not env.startswith('HF_HUB_OFFLINE=')]
                            topo_data['env_vars'].append('HF_HUB_OFFLINE=1')

                        if global_tf == 1:
                            topo_data['env_vars'] = [env for env in topo_data['env_vars'] if not env.startswith('TRANSFORMERS_OFFLINE=')]
                            topo_data['env_vars'].append('TRANSFORMERS_OFFLINE=1')
                    
        return {"catalog": config}
    except Exception as e:
        return {"error": str(e), "catalog": {"models": {}}}

def execute_teardown(target_hosts: list = None) -> dict:
    results = {}
    hosts_to_clean = target_hosts if target_hosts else list(HOSTS.keys())
    for host in hosts_to_clean:
        if host not in HOSTS: continue
        ip = HOSTS[host]["ip"]
        res = run_ssh(ip, "tetrel", ["docker", "rm", "-f", "vllm-standalone", "vllm-head", "vllm-worker"], timeout=15)
        results[host] = "Purged" if res.returncode == 0 else f"Error: {res.stderr.strip()}"
    return results

def authorize_user_key(public_key_path: str) -> dict:
    key_file = Path(public_key_path).expanduser()
    if not key_file.exists():
        return {"status": "error", "message": f"Key file not found: {public_key_path}"}
    
    pub_key_content = key_file.read_text().strip()
    escaped_key = shlex.quote(pub_key_content)
    results = {}
    
    for host, meta in HOSTS.items():
        ip = meta["ip"]
        cmd = ["bash", "-c", f"mkdir -p ~/.ssh && chmod 700 ~/.ssh && echo {escaped_key} >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"]
        res = run_ssh(ip, "tetrel", cmd, timeout=10)
        results[host] = "Authorized" if res.returncode == 0 else f"Failed: {res.stderr.strip()}"
    return {"status": "success", "details": results}

def execute_sync() -> dict:
    results = {}
    if not MODELS_YAML_PATH.exists():
        return {"status": "error", "message": "models.yaml missing locally."}
    yaml_content = MODELS_YAML_PATH.read_text()
    escaped_yaml = shlex.quote(yaml_content)
    for host, meta in HOSTS.items():
        ip = meta["ip"]
        cmd = ["bash", "-c", f"mkdir -p /opt/dgx-cluster-control && echo {escaped_yaml} > /opt/dgx-cluster-control/models.yaml"]
        res = run_ssh(ip, "tetrel", cmd, timeout=10)
        results[host] = "Synced models.yaml" if res.returncode == 0 else f"Failed: {res.stderr.strip()}"
    return {"status": "success", "details": results}

def execute_deployment(model: str, nodes: int, head: str, user_id: str, wait: bool = False, run_benchmark: bool = False) -> dict:
    deploy_start_time = time.time()
    target_hosts = ["spark-4", "spark-3"] if nodes == 2 else [head]
    
    catalog_resp = load_model_catalog()
    models_catalog = catalog_resp.get("catalog", {}).get("models", {})

    if model not in models_catalog:
        return {"status": "error", "message": f"Model '{model}' not defined in models.yaml catalog."}

    model_config = models_catalog[model]
    topologies = model_config.get("topologies", {})
    topo_key = "2_node" if nodes == 2 else "1_node"

    if topo_key not in topologies:
        return {"status": "error", "message": f"Topology '{topo_key}' not supported for model '{model}'."}

    # Fetch live cluster offline state to explicitly bind to docker arguments
    offline_mode = False
    if NETWORK_STATE_FILE.exists():
        try:
            offline_mode = "OFFLINE" in NETWORK_STATE_FILE.read_text().strip()
        except Exception:
            pass
    offline_val = "1" if offline_mode else "0"

    topo_config = topologies[topo_key]
    hf_path = model_config.get("hf_path", model)
    gpu_util = model_config.get("gpu_util", 0.70)
    max_model_len = topo_config.get("max_model_len", 32768)
    tp_size = topo_config.get("tp_size", 1)
    pp_size = topo_config.get("pp_size", nodes)
    
    vllm_args_raw = topo_config.get("vllm_args", "")
    try:
        vllm_args_list = shlex.split(vllm_args_raw)
    except Exception:
        vllm_args_list = vllm_args_raw.split()

    use_ray = (nodes > 1) and ("--distributed-executor-backend" in vllm_args_list) and ("ray" in vllm_args_list)

    execute_teardown(target_hosts=target_hosts)

    for h in target_hosts:
        ip = HOSTS[h]["ip"]
        run_ssh(ip, "tetrel", ["sudo", "nvidia-smi", "-lgc", "300,1800"], timeout=10)

    default_img = catalog_resp.get("catalog", {}).get("default_image", "nvcr.io/nvidia/vllm:26.07-py3")
    image_tag = model_config.get("image", default_img)

    vol_mount = "/home/tetrel/.cache/huggingface:/root/.cache/huggingface"
    compat_mount = "/dev/null:/etc/ld.so.conf.d/00-cuda-compat.conf"
    
    head_ip = HOSTS[head]["ip"]
    hf_token = get_hf_token()

    if nodes == 1:
        ip = HOSTS[head]["ip"]
        env_flags = [
            "-e", "PYTHONUNBUFFERED=1",
            "-e", "NVIDIA_DISABLE_REQUIRE=true",
            "-e", f"HF_HUB_OFFLINE={offline_val}",
            "-e", f"TRANSFORMERS_OFFLINE={offline_val}"
        ]
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
            "docker", "run", "-d",
            "--name", "vllm-standalone",
            "--net=host", "--ipc=host", "--shm-size=16gb",
            "--gpus", "all",
            "-v", vol_mount,
            "-v", compat_mount
        ] + env_flags + [image_tag] + container_args

        res = run_ssh(ip, "tetrel", docker_cmd, timeout=60)
        if res.returncode != 0:
            return {"status": "error", "message": f"Docker run command failed on {head}: {res.stderr}"}
    else:
        vllm_head_args = None
        for host in target_hosts:
            ip = HOSTS[host]["ip"]
            role_name = "vllm-head" if host == head else "vllm-worker"
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
            ]
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
                    "--master-port", "29500",
                    "--gpu-memory-utilization", str(gpu_util),
                    "--max-model-len", str(max_model_len)
                ]
                if node_rank > 0 and "--headless" not in vllm_args_list:
                    container_args.append("--headless")
                container_args.extend(vllm_args_list)

            if host == head:
                vllm_head_args = container_args

            if use_ray:
                if host == head:
                    entrypoint_cmd = ["ray", "start", "--head", "--port=6379", "--num-gpus=1", "--block"]
                else:
                    entrypoint_cmd = ["ray", "start", f"--address={head_ip}:6379", "--num-gpus=1", "--block"]
            else:
                entrypoint_cmd = container_args

            docker_cmd = [
                "docker", "run", "-d",
                "--name", role_name,
                "--net=host", "--ipc=host", "--shm-size=64gb",
                "--privileged",
                "--cap-add", "IPC_LOCK",
                "--device", "/dev/infiniband:/dev/infiniband",
                "--gpus", "all",
                "-v", vol_mount,
                "-v", compat_mount
            ] + env_flags + [image_tag] + entrypoint_cmd

            res = run_ssh(ip, "tetrel", docker_cmd, timeout=60)
            if res.returncode != 0:
                return {"status": "error", "message": f"Docker run failed on {host}: {res.stderr}"}

        if use_ray and vllm_head_args:
            print("[+] Waiting for Ray cluster to register worker nodes (max 60s)...")
            worker_hosts = [k for k, v in HOSTS.items() if v["role"] == "worker"]
            worker_ip = HOSTS[worker_hosts[0]]["ip"] if worker_hosts else ""
            
            for _ in range(30):
                check_ray = run_ssh(head_ip, "tetrel", ["docker", "exec", "vllm-head", "ray", "status"], timeout=10)
                if check_ray.returncode == 0 and (worker_ip in check_ray.stdout or "2 active nodes" in check_ray.stdout or "Healthy: 2" in check_ray.stdout):
                    break
                time.sleep(2)
            
            exec_str = " ".join(shlex.quote(arg) for arg in vllm_head_args)
            vllm_exec_cmd = ["docker", "exec", "-d", "vllm-head", "bash", "-c", f"{exec_str} > /proc/1/fd/1 2>&1"]
            run_ssh(head_ip, "tetrel", vllm_exec_cmd, timeout=30)

    time.sleep(4)
    for host in target_hosts:
        ip = HOSTS[host]["ip"]
        check_res = run_ssh(ip, "tetrel", ["docker", "ps", "--filter", "status=running", "--format", "{{.Names}}"], timeout=10)
        running = [c.strip() for c in check_res.stdout.splitlines() if c.strip() in ["vllm-standalone", "vllm-head", "vllm-worker"]]
        
        if not running:
            target_role = "vllm-standalone" if nodes == 1 else ("vllm-head" if host == head else "vllm-worker")
            log_res = run_ssh(ip, "tetrel", ["docker", "logs", "--tail", "50", target_role], timeout=10)
            err_log = log_res.stdout.strip() or log_res.stderr.strip() or "No logs captured."
            return {"status": "error", "message": f"Container '{target_role}' crashed on {host} immediately after startup.\nLogs:\n{err_log}"}

    if wait or run_benchmark:
        head_ip = HOSTS[head]["ip"]
        is_ready = wait_for_cluster_ready(head_ip=head_ip, timeout_sec=900)
        
        if is_ready:
            total_duration = int(time.time() - deploy_start_time)
            record_load_time(model, topo_key, total_duration)

            if run_benchmark:
                print("[+] Triggering 3-pass performance benchmark...")
                time.sleep(30)
                bench_res = subprocess.run(["python3", "benchmark.py"], capture_output=True, text=True)
                bench_file = BASE_DIR / "benchmark_results.txt"
                bench_file.write_text(bench_res.stdout)
                print(f"[+] Benchmark completed. Results written to {bench_file}")

    return {
        "status": "success",
        "message": f"Deployment sequence for {model} across {nodes} node(s) initiated.",
        "targets": target_hosts
    }

def get_container_logs(host: str, tail: int = 40) -> dict:
    if host not in HOSTS: return {"logs": ["Invalid target host specified."]}
    
    ip = HOSTS[host]["ip"]
    res = run_ssh(ip, "tetrel", ["docker", "ps", "-a", "--format", "{{.Names}}"], timeout=10)
    if res.returncode != 0: return {"logs": ["Failed to connect to host."]}

    containers = [c.strip() for c in res.stdout.strip().splitlines() if c.strip() in ["vllm-standalone", "vllm-head", "vllm-worker"]]
    if not containers: return {"logs": ["No active vLLM containers on this node."]}

    c_name = containers[0]
    # Fetch a larger tail buffer from Docker to compensate for filtered health/metrics requests
    fetch_tail = max(tail * 5, 400)
    log_res = run_ssh(ip, "tetrel", ["docker", "logs", "--tail", str(fetch_tail), c_name], timeout=10)
    
    raw_logs = log_res.stdout.splitlines() if log_res.returncode == 0 else log_res.stderr.splitlines()
    clean_logs = [ANSI_ESCAPE.sub('', line) for line in raw_logs]

    # Suppress HTTP health check and Prometheus metrics polling requests
    filtered_logs = [
        line for line in clean_logs 
        if not any(endpoint in line for endpoint in ["GET /health", "GET /metrics"])
    ]

    # Retain the exact requested line depth from the filtered log buffer
    final_logs = filtered_logs[-tail:] if len(filtered_logs) > tail else filtered_logs

    return {"logs": final_logs if final_logs else ["Log buffer empty."]}

def interactive_menu():
    print("=== [ TETREL SECURITY ] DGX Cluster Orchestrator ===")
    status = get_cluster_status()
    print(f"Server Time: {status['server_time']} | Mode: {status['network_mode']} | API: {'READY' if status['cluster_ready'] else 'OFFLINE'} | TPS: {status.get('system_tps', 0.0)} tok/s | Streams: {status.get('running_requests', 0)} active ({status.get('waiting_requests', 0)} queued)\n")

    for h, data in status["hosts"].items():
        tele = data.get("telemetry", {})
        temp = f"{tele.get('gpu_temp_c', 'N/A')}°C" if 'gpu_temp_c' in tele else "N/A"
        util = f"{tele.get('gpu_util_pct', 'N/A')}%" if 'gpu_util_pct' in tele else "N/A"
        mem = f"{tele.get('mem_used_mb', 'N/A')}/{tele.get('mem_total_mb', 'N/A')} MB" if 'mem_used_mb' in tele else "N/A"
        print(f"[{h}] Docker: {data['docker_status']} | Container: {data['container_name']} ({data['container_state']}) | Model: {data['active_model']} | Status: {data['model_status']} | ETA: {data['eta_display']} | TEMP: {temp} | GPU: {util} | MEM: {mem}")
    print("-" * 85)

    catalog_data = load_model_catalog().get("catalog", {}).get("models", {})
    models = list(catalog_data.keys())

    if not models:
        print("[-] No models found in models.yaml.")
        return

    print("\nAvailable Models:")
    for idx, m in enumerate(models, 1):
        print(f"  {idx}. {m}")

    try:
        choice = input("\nSelect a model number to deploy (or 'q' to quit): ").strip()
        if choice.lower() == 'q' or not choice.isdigit(): return

        selected_model = models[int(choice) - 1]
        topologies = catalog_data[selected_model].get("topologies", {})

        print(f"\nAvailable Topologies for {selected_model}:")
        topo_keys = list(topologies.keys())
        for idx, t in enumerate(topo_keys, 1):
            est, _ = get_estimated_load_time(selected_model, t)
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
    app = FastAPI(title="Tetrel Security DGX Control Plane API", version="4.6.3")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

    class DeployRequest(BaseModel):
        model: str
        nodes: int
        head: str = "spark-4"
        user_id: str = "dashboard_user"
        wait: bool = False
        run_benchmark: bool = False

    class NetworkToggleRequest(BaseModel):
        offline: bool

    @app.get("/api/status")
    def api_status(): return get_cluster_status()

    @app.get("/api/catalog")
    def api_catalog(): return load_model_catalog()

    @app.get("/api/logs/{host}")
    def api_logs(host: str, tail: int = 40): return get_container_logs(host, tail)

    @app.post("/api/deploy")
    def api_deploy(req: DeployRequest):
        res = execute_deployment(req.model, req.nodes, req.head, req.user_id, wait=req.wait, run_benchmark=req.run_benchmark)
        if res.get("status") != "success": raise HTTPException(status_code=400, detail=res.get("message", "Deployment failed"))
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

def main():
    parser = argparse.ArgumentParser(description="Tetrel Security DGX Cluster Orchestrator")
    subparsers = parser.add_subparsers(dest="subcommand")

    subparsers.add_parser("daemon").add_argument("--port", type=int, default=5001)
    subparsers.add_parser("status")
    subparsers.add_parser("teardown")
    subparsers.add_parser("menu")
    subparsers.add_parser("sync")

    deploy_parser = subparsers.add_parser("deploy")
    deploy_parser.add_argument("--model", required=True)
    deploy_parser.add_argument("--nodes", type=int, default=2)
    deploy_parser.add_argument("--head", default="spark-4")
    deploy_parser.add_argument("--wait", action="store_true", help="Block until HTTP /health passes")
    deploy_parser.add_argument("--benchmark", action="store_true", help="Automatically run benchmark.py when ready")
    deploy_parser.add_argument("-y", "--yes", action="store_true")

    logs_parser = subparsers.add_parser("logs")
    logs_parser.add_argument("--host", default="spark-4")
    logs_parser.add_argument("--tail", type=int, default=40)

    auth_parser = subparsers.add_parser("authorize-key")
    auth_parser.add_argument("--key", required=True)

    cli_parser = subparsers.add_parser("cli")
    cli_sub = cli_parser.add_subparsers(dest="cli_action")

    for cmd in ["status", "teardown", "menu", "sync"]:
        cli_sub.add_parser(cmd)
        
    cli_sub.add_parser("daemon").add_argument("--port", type=int, default=5001)

    cli_deploy = cli_sub.add_parser("deploy")
    cli_deploy.add_argument("--model", required=True)
    cli_deploy.add_argument("--nodes", type=int, default=2)
    cli_deploy.add_argument("--head", default="spark-4")
    cli_deploy.add_argument("--wait", action="store_true")
    cli_deploy.add_argument("--benchmark", action="store_true")
    cli_deploy.add_argument("-y", "--yes", action="store_true")

    cli_logs = cli_sub.add_parser("logs")
    cli_logs.add_argument("--host", default="spark-4")
    cli_logs.add_argument("--tail", type=int, default=40)

    cli_auth = cli_sub.add_parser("authorize-key")
    cli_auth.add_argument("--key", required=True)

    args = parser.parse_args()
    subcommand = args.subcommand
    if subcommand == "cli": subcommand = getattr(args, "cli_action", None) or "menu"

    if subcommand == "daemon":
        if not HAS_FASTAPI: sys.exit("[-] Error: fastapi and uvicorn are required for daemon mode.")
        port = getattr(args, "port", 5001)
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")

    elif subcommand == "status": print(json.dumps(get_cluster_status(), indent=2))
    elif subcommand == "teardown": print(json.dumps(execute_teardown(), indent=2))
    elif subcommand == "deploy": print(json.dumps(execute_deployment(args.model, args.nodes, args.head, os.environ.get("USER") or getpass.getuser(), wait=getattr(args, "wait", False), run_benchmark=getattr(args, "benchmark", False)), indent=2))
    elif subcommand == "logs": print("\n".join(get_container_logs(args.host, args.tail).get("logs", [])))
    elif subcommand == "authorize-key": print(json.dumps(authorize_user_key(args.key), indent=2))
    elif subcommand == "sync": print(json.dumps(execute_sync(), indent=2))
    else: interactive_menu()

if __name__ == "__main__":
    main()
