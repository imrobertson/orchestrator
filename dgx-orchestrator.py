#!/usr/bin/env python3
"""
TETREL SECURITY - DGX CLUSTER ORCHESTRATOR
--------------------------------------------------------------------------------
Architecture Target: Dual DGX Spark (Grace Blackwell GB10, LPDDR5x Unified Memory).
vLLM Runtime Target: nvcr.io/nvidia/vllm:26.07-py3 (vLLM 0.27.x equivalent).

This orchestrator manages the lifecycle, network state, and tuning deployments 
of multi-node LLM serving over a 100GbE backplane via NCCL.
"""

import argparse
import datetime
import getpass
import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import time
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
    """
    Extracts HuggingFace authentication token from the environment or local cache.
    Crucial for deploying gating models like Meta Llama 3.3 / Llama 4.
    """
    if "HF_TOKEN" in os.environ and os.environ["HF_TOKEN"].strip():
        return os.environ["HF_TOKEN"].strip()
    
    secrets_file = BASE_DIR / ".secrets"
    if secrets_file.exists():
        try:
            for line in secrets_file.read_text().splitlines():
                if line.startswith("HF_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass

    hf_token_file = Path.home() / ".cache" / "huggingface" / "token"
    if hf_token_file.exists():
        try:
            return hf_token_file.read_text().strip()
        except Exception:
            pass

    return ""

def run_ssh(ip: str, user: str, command_list: list, capture: bool = True, timeout: int = 10) -> subprocess.CompletedProcess:
    """
    Executes remote commands via SSH. Quotes each token individually so OpenSSH's
    remote shell evaluation preserves whitespace, quotes, and JSON structures.
    """
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
    """
    Queries nvidia-smi telemetry line-by-line.
    Handles Grace Blackwell (GB10) LPDDR5x Unified Memory where standard memory 
    allocation fields often report [N/A] due to OS shared memory spaces.
    """
    cmd = ["/usr/bin/nvidia-smi", "--query-gpu=temperature.gpu,utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"]
    res = run_ssh(ip, user, cmd, capture=True, timeout=10)
    
    if res.returncode == 0 and res.stdout.strip():
        for line in res.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2:
                temp_str, util_str = parts[0], parts[1]
                if temp_str.isdigit() and util_str.isdigit():
                    # Fallback to Unified / 131072 MB if SMI fails to read Grace memory
                    mem_used = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else "Unified"
                    mem_total = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else "131072"
                    return {
                        "gpu_temp_c": int(temp_str),
                        "gpu_util_pct": int(util_str),
                        "mem_used_mb": mem_used,
                        "mem_total_mb": mem_total
                    }
    return {}

def get_cluster_status() -> dict:
    """Returns a full state map of Docker daemons, models, and telemetry across all nodes."""
    offline_mode = False
    if NETWORK_STATE_FILE.exists():
        try:
            offline_mode = "OFFLINE" in NETWORK_STATE_FILE.read_text().strip()
        except Exception:
            pass

    status_data = {
        "server_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S EST"),
        "network_mode": "Working in OFFLINE mode" if offline_mode else "Working in ONLINE mode",
        "hosts": {}
    }

    for host, meta in HOSTS.items():
        ip = meta["ip"]
        user = "tetrel"
        res = run_ssh(ip, user, ["docker", "ps", "--format", "{{.Names}}::{{.Image}}"], timeout=8)
        
        if res.returncode == 0:
            active_container, loaded_model = "None", "None"
            lines = [l.strip() for l in res.stdout.strip().splitlines() if l.strip()]
            for line in lines:
                parts = line.split("::")
                if parts:
                    c_name = parts[0]
                    if c_name in ["vllm-standalone", "vllm-head", "vllm-worker"]:
                        active_container = c_name
                        # Peek into container startup flags to find the actively loaded model
                        inspect_res = run_ssh(ip, user, ["docker", "inspect", c_name, "--format", "{{json .Config.Cmd}}"], timeout=5)
                        if inspect_res.returncode == 0 and "--model" in inspect_res.stdout:
                            try:
                                cmd_parts = json.loads(inspect_res.stdout.strip())
                                if "--model" in cmd_parts:
                                    idx = cmd_parts.index("--model")
                                    if idx + 1 < len(cmd_parts):
                                        loaded_model = cmd_parts[idx + 1].split("/")[-1]
                            except Exception:
                                loaded_model = "Active Container"

            telemetry = get_lightweight_telemetry(ip, user)
            status_data["hosts"][host] = {
                "ip": ip, "status": "ONLINE", "docker": "ONLINE",
                "container": active_container, "active_model": loaded_model, "telemetry": telemetry
            }
        else:
            status_data["hosts"][host] = {
                "ip": ip, "status": "OFFLINE", "docker": "UNREACHABLE",
                "container": "None", "active_model": "None", "telemetry": {}
            }
    return status_data

def load_model_catalog() -> dict:
    """
    Loads models.yaml and aggressively enforces global offline overrides.
    This prevents an accidental online validation check from hanging vLLM startup 
    on firewalled backplanes.
    """
    if not MODELS_YAML_PATH.exists():
        return {"catalog": {"models": {}}}
    try:
        with open(MODELS_YAML_PATH, "r") as f:
            config = yaml.safe_load(f) or {}
            
        # 1. Parse the global config headers
        global_hf = config.get('GLOBAL_HF_HUB_OFFLINE', 0)
        global_tf = config.get('GLOBAL_TRANSFORMERS_OFFLINE', 0)

        # 2. Iterate through all models and enforce global overrides on env_vars
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
                        
                        # Strip out existing declarations to avoid duplicated vars, then append force-state
                        if global_hf == 1:
                            topo_data['env_vars'] = [env for env in topo_data['env_vars'] if not env.startswith('HF_HUB_OFFLINE=')]
                            topo_data['env_vars'].append('HF_HUB_OFFLINE=1')

                        if global_tf == 1:
                            topo_data['env_vars'] = [env for env in topo_data['env_vars'] if not env.startswith('TRANSFORMERS_OFFLINE=')]
                            topo_data['env_vars'].append('TRANSFORMERS_OFFLINE=1')
                    
        return {"catalog": config}
    except Exception as e:
        return {"error": str(e), "catalog": {"models": {}}}

def get_estimated_load_time(model: str, topo_key: str) -> int:
    """Calculates historical moving average of container startup times."""
    if not LOAD_TIMES_PATH.exists(): return 90
    try:
        data = json.loads(LOAD_TIMES_PATH.read_text())
        times = data.get(f"{model}::{topo_key}", [])
        if times: return int(sum(times) / len(times))
    except Exception: pass
    return 90

def record_load_time(model: str, topo_key: str, duration_sec: int):
    """Saves startup telemetry to inform the UI of expected wait times."""
    if duration_sec > 300 or duration_sec < 5: return
    data = {}
    if LOAD_TIMES_PATH.exists():
        try: data = json.loads(LOAD_TIMES_PATH.read_text())
        except Exception: data = {}
    key = f"{model}::{topo_key}"
    data.setdefault(key, []).append(duration_sec)
    data[key] = data[key][-20:]  # Keep rolling average tight to last 20 launches
    try: LOAD_TIMES_PATH.write_text(json.dumps(data, indent=2))
    except Exception: pass

def execute_teardown(target_hosts: list = None) -> dict:
    """Forcefully destroys existing vLLM containers to free GPU Unified Memory."""
    results = {}
    hosts_to_clean = target_hosts if target_hosts else list(HOSTS.keys())
    for host in hosts_to_clean:
        if host not in HOSTS: continue
        ip = HOSTS[host]["ip"]
        res = run_ssh(ip, "tetrel", ["docker", "rm", "-f", "vllm-standalone", "vllm-head", "vllm-worker"], timeout=15)
        results[host] = "Purged" if res.returncode == 0 else f"Error: {res.stderr.strip()}"
    return results

def authorize_user_key(public_key_path: str) -> dict:
    """Safe key authorization preventing double-escaped SSH execution bugs."""
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
    """Pushes local models.yaml state to all remote cluster nodes."""
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

def ensure_container_patch(target_hosts: list):
    """
    Distributes the python container patch to remote nodes.
    This fixes a specific FlashInfer Cutlass bug involving MXFP8 memory allocation.
    It is programmed defensively: if NVIDIA fixes this in a later container (e.g. 26.07)
    and the file no longer exists, it passes silently without crashing.
    """
    patch_path = BASE_DIR / "vllm_gb10_patch.py"
    patch_content = """import os, sys
# Dynamic launcher lookup fix for transformers pickling issue
main_mod = sys.modules.get("__main__")
if main_mod and not hasattr(main_mod, "launcher"):
    setattr(main_mod, "launcher", lambda *args, **kwargs: None)

target = "/usr/local/lib/python3.12/dist-packages/vllm/model_executor/layers/fused_moe/oracle/mxfp4.py"
if os.path.exists(target):
    try:
        with open(target, "r") as f:
            content = f.read()
        old_str = "return Mxfp4MoeBackend.FLASHINFER_CUTLASS_MXFP4_BF16"
        new_str = "return Mxfp4MoeBackend.FLASHINFER_CUTLASS_MXFP4_MXFP8"
        if old_str in content:
            content = content.replace(old_str, new_str)
            with open(target, "w") as f:
                f.write(content)
    except Exception:
        pass
"""
    if not patch_path.exists() or patch_path.read_text() != patch_content:
        patch_path.write_text(patch_content)
        patch_path.chmod(0o664)

    # Push to target remote nodes so the Docker daemon can correctly map the -v mount
    escaped_patch = shlex.quote(patch_content)
    for host in target_hosts:
        ip = HOSTS[host]["ip"]
        cmd = ["bash", "-c", f"mkdir -p /opt/dgx-cluster-control && echo {escaped_patch} > /opt/dgx-cluster-control/vllm_gb10_patch.py"]
        run_ssh(ip, "tetrel", cmd, timeout=10)

def execute_deployment(model: str, nodes: int, head: str, user_id: str) -> dict:
    """Orchestrates container deployment. Handles 1-node and 2-node PP pipelines."""
    target_hosts = ["spark-4", "spark-3"] if nodes == 2 else [head]
    ensure_container_patch(target_hosts)
    
    catalog_resp = load_model_catalog()
    models_catalog = catalog_resp.get("catalog", {}).get("models", {})

    if model not in models_catalog:
        return {"status": "error", "message": f"Model '{model}' not defined in models.yaml catalog."}

    model_config = models_catalog[model]
    topologies = model_config.get("topologies", {})
    topo_key = "2_node" if nodes == 2 else "1_node"

    if topo_key not in topologies:
        return {"status": "error", "message": f"Topology '{topo_key}' not supported for model '{model}'."}

    topo_config = topologies[topo_key]
    hf_path = model_config.get("hf_path", model)
    gpu_util = model_config.get("gpu_util", 0.70)  # Safe GB10 default fallback
    max_model_len = topo_config.get("max_model_len", 32768)
    tp_size = topo_config.get("tp_size", 1)
    pp_size = topo_config.get("pp_size", nodes)
    
    vllm_args_raw = topo_config.get("vllm_args", "")
    try:
        vllm_args_list = shlex.split(vllm_args_raw)
    except Exception:
        vllm_args_list = vllm_args_raw.split()

    execute_teardown(target_hosts=target_hosts)

    # Hardware clock lock for sustained inference stability
    for h in target_hosts:
        ip = HOSTS[h]["ip"]
        run_ssh(ip, "tetrel", ["sudo", "nvidia-smi", "-lgc", "300,1800"], timeout=10)

    # Dynamic container image resolution from models.yaml catalog with fallback
    default_img = catalog_resp.get("catalog", {}).get("default_image", "nvcr.io/nvidia/vllm:26.07-py3")
    image_tag = model_config.get("image", default_img)

    vol_mount = "/home/tetrel/.cache/huggingface:/root/.cache/huggingface"
    patch_mount = "/opt/dgx-cluster-control/vllm_gb10_patch.py:/usr/local/lib/python3.12/dist-packages/sitecustomize.py"
    
    head_ip = HOSTS[head]["ip"]
    hf_token = get_hf_token()

    if nodes == 1:
        ip = HOSTS[head]["ip"]
        env_flags = [
            "-e", "PYTHONUNBUFFERED=1",
            "-e", "NVIDIA_DISABLE_REQUIRE=true",
            "-e", "VLLM_ENABLE_CUDA_COMPATIBILITY=1",
            "-e", "LD_LIBRARY_PATH=/usr/local/cuda/compat:$LD_LIBRARY_PATH"
        ]
        if hf_token: env_flags.extend(["-e", f"HF_TOKEN={hf_token}"])

        # INJECT ENV VARS FOR 1-NODE (Fixes Grace CPU context-switch thrashing)
        for ev in topo_config.get("env_vars", []):
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
            "-v", patch_mount
        ] + env_flags + [image_tag] + container_args

        res = run_ssh(ip, "tetrel", docker_cmd, timeout=15)
        if res.returncode != 0:
            return {"status": "error", "message": f"Docker run command failed on {head}: {res.stderr}"}
    else:
        for host in target_hosts:
            ip = HOSTS[host]["ip"]
            role_name = "vllm-head" if host == head else "vllm-worker"
            node_rank = 0 if host == head else 1

            env_flags = [
                "-e", "PYTHONUNBUFFERED=1",
                "-e", "NCCL_DEBUG=INFO",
                "-e", "NVIDIA_DISABLE_REQUIRE=true",
                "-e", "VLLM_ENABLE_CUDA_COMPATIBILITY=1",
                "-e", "LD_LIBRARY_PATH=/usr/local/cuda/compat:$LD_LIBRARY_PATH",
                "-e", "NCCL_IB_DISABLE=0",
                "-e", "NCCL_P2P_DISABLE=0",
                "-e", "NCCL_IB_HCA=rocep1s0f0",
                "-e", "NCCL_IB_GID_INDEX=3",
                "-e", "NCCL_SOCKET_IFNAME=enp1s0f0np0",
                "-e", "GLOO_SOCKET_IFNAME=enp1s0f0np0",
                "-e", "NCCL_BUFFSIZE=16777216",
                "-e", "NCCL_NSOCKS_PER_THREAD=4",
                "-e", "NCCL_SOCKET_DRV_BUFFSIZE=2097152",
                "-e", "NCCL_CUMEM_ENABLE=0"
            ]
            if hf_token: env_flags.extend(["-e", f"HF_TOKEN={hf_token}"])

            for ev in topo_config.get("env_vars", []):
                env_flags.extend(["-e", ev])

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
            ] + vllm_args_list

            docker_cmd = [
                "docker", "run", "-d",
                "--name", role_name,
                "--net=host", "--ipc=host", "--shm-size=64gb",
                "--privileged",
                "--cap-add", "IPC_LOCK",
                "--device", "/dev/infiniband:/dev/infiniband",
                "--gpus", "all",
                "-v", vol_mount,
                "-v", patch_mount
            ] + env_flags + [image_tag] + container_args

            res = run_ssh(ip, "tetrel", docker_cmd, timeout=15)
            if res.returncode != 0:
                return {"status": "error", "message": f"Docker run failed on {host}: {res.stderr}"}

    # Verify launch stability
    time.sleep(4)
    for host in target_hosts:
        ip = HOSTS[host]["ip"]
        check_res = run_ssh(ip, "tetrel", ["docker", "ps", "--format", "{{.Names}}"], timeout=5)
        running = [c.strip() for c in check_res.stdout.splitlines() if c.strip() in ["vllm-standalone", "vllm-head", "vllm-worker"]]
        if not running:
            target_role = "vllm-standalone" if nodes == 1 else ("vllm-head" if host == head else "vllm-worker")
            log_res = run_ssh(ip, "tetrel", ["docker", "logs", "--tail", "30", target_role], timeout=5)
            err_log = log_res.stdout.strip() or log_res.stderr.strip() or "No logs captured."
            return {"status": "error", "message": f"Container '{target_role}' crashed on {host}.\nLogs:\n{err_log}"}

    return {
        "status": "success",
        "message": f"Deployment sequence for {model} across {nodes} node(s) initiated.",
        "targets": target_hosts
    }

def get_container_logs(host: str, tail: int = 40) -> dict:
    if host not in HOSTS: return {"logs": ["Invalid target host specified."]}
    
    ip = HOSTS[host]["ip"]
    res = run_ssh(ip, "tetrel", ["docker", "ps", "--format", "{{.Names}}"], timeout=5)
    if res.returncode != 0: return {"logs": ["Failed to connect to host."]}

    containers = [c.strip() for c in res.stdout.strip().splitlines() if c.strip() in ["vllm-standalone", "vllm-head", "vllm-worker"]]
    if not containers: return {"logs": ["No active vLLM containers on this node."]}

    c_name = containers[0]
    log_res = run_ssh(ip, "tetrel", ["docker", "logs", "--tail", str(tail), c_name], timeout=10)
    
    logs = log_res.stdout.splitlines() if log_res.returncode == 0 else log_res.stderr.splitlines()
    return {"logs": logs if logs else ["Log buffer empty."]}

def interactive_menu():
    print("=== [ TETREL SECURITY ] DGX Cluster Orchestrator ===")
    status = get_cluster_status()
    print(f"Server Time: {status['server_time']} | Mode: {status['network_mode']}\n")

    for h, data in status["hosts"].items():
        tele = data.get("telemetry", {})
        temp = f"{tele.get('gpu_temp_c', 'N/A')}°C" if 'gpu_temp_c' in tele else "N/A"
        util = f"{tele.get('gpu_util_pct', 'N/A')}%" if 'gpu_util_pct' in tele else "N/A"
        mem = f"{tele.get('mem_used_mb', 'N/A')}/{tele.get('mem_total_mb', 'N/A')} MB" if 'mem_used_mb' in tele else "N/A"
        print(f"[{h}] Status: {data['status']} | Docker: {data['docker']} | Model: {data['active_model']} | Temp: {temp} | Util: {util} | Memory: {mem}")
    print("-" * 75)

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
            est = get_estimated_load_time(selected_model, t)
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

        confirm = input(f"\nDeploy {selected_model} ({selected_topo}) with head {head}? (y/N): ").strip().lower()
        if confirm == 'y':
            print(f"[+] Launching deployment sequence for {selected_model}...")
            start_time = time.time()
            res = execute_deployment(selected_model, nodes, head, user_id)
            print(json.dumps(res, indent=2))
            if res.get("status") == "success":
                record_load_time(selected_model, selected_topo, int(time.time() - start_time))
    except (IndexError, ValueError) as e:
        print(f"[-] Invalid selection: {e}")

if HAS_FASTAPI:
    app = FastAPI(title="Tetrel Security DGX Control Plane API", version="4.6.2")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

    class DeployRequest(BaseModel):
        model: str; nodes: int; head: str = "spark-4"; user_id: str = "dashboard_user"

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
        res = execute_deployment(req.model, req.nodes, req.head, req.user_id)
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
    elif subcommand == "deploy": print(json.dumps(execute_deployment(args.model, args.nodes, args.head, os.environ.get("USER") or getpass.getuser()), indent=2))
    elif subcommand == "logs": print("\n".join(get_container_logs(args.host, args.tail).get("logs", [])))
    elif subcommand == "authorize-key": print(json.dumps(authorize_user_key(args.key), indent=2))
    elif subcommand == "sync": print(json.dumps(execute_sync(), indent=2))
    else: interactive_menu()

if __name__ == "__main__":
    main()
