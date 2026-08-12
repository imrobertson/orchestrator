#!/usr/bin/env python3
# ==============================================================================
# 🚀 DGX SPARK CLUSTER ORCHESTRATOR (V3.8 - PRODUCTION RELEASE)
# ==============================================================================
import os
import sys
import yaml
import time
import argparse
import subprocess
import contextlib
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel
import uvicorn

# ==============================================================================
# UTILITY: CONFIGURATION & PATH RESOLUTION
# ==============================================================================
def get_catalog() -> dict:
    catalog_path = Path(__file__).resolve().parent / "models.yaml"
    if not catalog_path.exists():
        print(f"[-] CRITICAL: Cannot find cluster catalog at {catalog_path}")
        sys.exit(1)
        
    with open(catalog_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

# ==============================================================================
# CORE TRANSPORT LAYER: SSH MULTIPLEXING ENGINE
# ==============================================================================
@contextlib.contextmanager
def ssh_mux_session(ip: str, user: str):
    socket_path = f"/tmp/ssh-mux-{user}-{ip}.sock"
    if os.path.exists(socket_path):
        try: os.remove(socket_path)
        except Exception: pass

    cmd = [
        "ssh", "-f", "-N", "-M",
        "-S", socket_path,
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=5",
        f"{user}@{ip}"
    ]
    
    try:
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        if res.returncode != 0:
            print(f"  [-] Critical: SSH key verification failed for {user}@{ip}.")
            sys.exit(1)
    except subprocess.TimeoutExpired:
        print(f"  [-] Critical: Multiplex connection to {ip} timed out.")
        sys.exit(1)
    
    try:
        yield socket_path
    finally:
        exit_cmd = [
            "ssh", "-O", "exit",
            "-S", socket_path,
            f"{user}@{ip}"
        ]
        subprocess.run(exit_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        if os.path.exists(socket_path):
            try: os.remove(socket_path)
            except Exception: pass

def local_ping(ip: str) -> bool:
    res = subprocess.run(["ping", "-c", "1", "-W", "1", ip], capture_output=True)
    return res.returncode == 0

def run_ssh(ip: str, user: str, cmd_args: List[str], interactive: bool = False, capture: bool = False, timeout: Optional[float] = None) -> subprocess.CompletedProcess:
    socket_path = f"/tmp/ssh-mux-{user}-{ip}.sock"
    ssh_base = ["ssh", "-o", "StrictHostKeyChecking=no"]
    
    if os.path.exists(socket_path):
        ssh_base.extend(["-o", f"ControlPath={socket_path}"])
        
    if interactive:
        ssh_base.append("-t")
    else:
        ssh_base.extend([
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=5",
            "-o", "GSSAPIAuthentication=no"
        ])
        
    full_cmd = ssh_base + [f"{user}@{ip}"] + cmd_args
    try:
        return subprocess.run(full_cmd, capture_output=capture, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args=full_cmd, returncode=124, stdout="", stderr="SSH Timeout.")

def run_scp(ip: str, user: str, local_path: str, remote_path: str, timeout: float = 15) -> subprocess.CompletedProcess:
    socket_path = f"/tmp/ssh-mux-{user}-{ip}.sock"
    scp_cmd = [
        "scp",
        "-o", "StrictHostKeyChecking=no",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=5"
    ]
    if os.path.exists(socket_path):
        scp_cmd.extend(["-o", f"ControlPath={socket_path}"])
        
    scp_cmd.extend([local_path, f"{user}@{ip}:{remote_path}"])
    try:
        return subprocess.run(scp_cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args=scp_cmd, returncode=124, stdout="", stderr="SCP Timeout.")

# ==============================================================================
# INVENTORY CONTROL AND UTILITIES
# ==============================================================================
def resolve_management_ip(host_details: dict) -> Tuple[Optional[str], str]:
    mgt = host_details.get('networks', {}).get('management', {})
    wired_ip = mgt.get('wired')
    wireless_ip = mgt.get('wireless')
    
    if wired_ip and local_ping(wired_ip): return wired_ip, "wired"
    if wireless_ip and local_ping(wireless_ip): return wireless_ip, "wireless"
    return None, "unreachable"

def find_host_by_identifier(identifier: str, hosts_config: dict) -> Tuple[Optional[str], Optional[dict]]:
    for host_name, details in hosts_config.items():
        if identifier == host_name or identifier in details.get('aliases', []):
            return host_name, details
        networks = details.get('networks', {})
        mgt = networks.get('management', {})
        if identifier in [mgt.get('wired'), mgt.get('wireless'), networks.get('backplane')]:
            return host_name, details
    return None, None

def ping_sweep(hosts_config: Dict[str, Any]) -> Dict[str, Tuple[str, str]]:
    print("\n[*] Probing cluster topology...")
    active_hosts = {}
    
    for host_name, details in hosts_config.items():
        ip, interface = resolve_management_ip(details)
        if ip:
            print(f"  [+] Node {host_name} is online over {interface} ({ip}).")
            active_hosts[host_name] = (ip, interface)
        else:
            print(f"  [-] Node {host_name} is completely offline/unresponsive.")
            
    return active_hosts

def enforce_safeguards(ip: str, user: str, is_batch: bool, dry_run: bool = False):
    print(f"  [*] Enforcing pre-flight checks on {ip}...")
    if dry_run: return

    res = run_ssh(ip, user, ["docker", "info"], capture=True, timeout=15)
    
    if res.returncode == 255:
        print("  [-] Critical: SSH verification rejected. Load keys: eval $(ssh-agent -s) && ssh-add")
        sys.exit(1)
        
    if res.returncode != 0:
        print(f"  [!] Docker appears offline. Remote response: {res.stderr.strip()}")
        if not is_batch:
            ans = input("  [?] Attempt to start Docker? (y/N): ")
            if ans.lower() != 'y': sys.exit(1)
        print("  [*] Starting Docker. IF IT HANGS HERE, TYPE YOUR REMOTE SUDO PASSWORD AND PRESS ENTER.")
        run_ssh(ip, user, ["sudo", "systemctl", "start", "docker.socket", "docker.service"], interactive=True)
    else:
        print("  [+] Docker engine status: ACTIVE")

    print("  [*] Locking GPU power limits. IF IT HANGS HERE, TYPE YOUR REMOTE SUDO PASSWORD AND PRESS ENTER.")
    run_ssh(ip, user, ["sudo", "nvidia-smi", "-lgc", "300,1800"], interactive=True)

# ==============================================================================
# CORE CLI ACTIONS
# ==============================================================================
def authorize_user_key(pubkey_path: Optional[str] = None):
    if not pubkey_path:
        for candidate in ["~/.ssh/id_ed25519.pub", "~/.ssh/id_rsa.pub"]:
            p = Path(candidate).expanduser()
            if p.exists():
                pubkey_path = str(p)
                break

    if not pubkey_path or not Path(pubkey_path).expanduser().exists():
        print("[-] ERROR: No SSH public key found. Specify path using --key /path/to/key.pub")
        return
        
    key_file = Path(pubkey_path).expanduser()
    key_data = key_file.read_text().strip()
    
    print(f"\n[*] Authorizing public key ({key_file.name}) across Spark cluster...")
    
    catalog = get_catalog()
    hosts_config = catalog.get("hosts", {})
    active_hosts = ping_sweep(hosts_config)
    
    for host_name, details in hosts_config.items():
        if host_name not in active_hosts: continue
        ip, _ = active_hosts[host_name]
        user = details['ssh_user']
        
        remote_cmd = (
            f"mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
            f"grep -qF '{key_data}' ~/.ssh/authorized_keys 2>/dev/null || "
            f"echo '{key_data}' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
        )
        
        with ssh_mux_session(ip, user):
            res = run_ssh(ip, user, ["bash", "-c", remote_cmd], capture=True, timeout=10)
            if res.returncode == 0:
                print(f"  [+] {host_name} ({ip}): Key successfully authorized.")
            else:
                print(f"  [-] {host_name} ({ip}): Authorization failed. Error: {res.stderr.strip()}")

def build_deployment_string(model_id: str, role: str, head_backplane_ip: str, node_rank: int, config: dict, topo: dict, host_details: dict) -> str:
    hf_token = ""
    secrets_path = Path(__file__).resolve().parent / ".secrets"
    if secrets_path.exists():
        with open(secrets_path, "r") as sf:
            for line in sf:
                if line.startswith("HF_TOKEN="):
                    hf_token = line.strip().split("=", 1)[1].strip("'\" ")
                    
    if not hf_token: hf_token = os.getenv("HF_TOKEN", "")
    
    hf_path = config.get("hf_path")
    gpu_util = config.get("gpu_util", topo.get("gpu_util", 0.85))
    dtype_flag = f"--kv-cache-dtype {config['dtype']}" if "dtype" in config else ""
    max_len = topo.get("max_model_len", 32768)
    tp_size = topo.get("tp_size", 1)
    pp_size = topo.get("pp_size", 1)
    vllm_args = topo.get("vllm_args", "")
    
    base_env_vars = [
        "VLLM_ENGINE_ITERATION_TIMEOUT_S=1200",
        "VLLM_RPC_TIMEOUT=1200000",
        "VLLM_DISTRIBUTED_TIMEOUT_MINUTES=15",
        "TORCH_DISTRIBUTED_TIMEOUT=3600",
        "TORCH_NCCL_HEARTBEAT_TIMEOUT_SEC=3600",
        "NCCL_TIMEOUT=3600",
        "GLOO_TIMEOUT=3600",
        "TORCH_INDUCTOR_RECOMPILE_LIMIT=100"
    ]
    all_env_vars = base_env_vars + topo.get("env_vars", [])
    env_prefixes = " ".join(all_env_vars)
    env_cmd = f"env {env_prefixes} " if env_prefixes else ""

    if pp_size > 1:
        container_name = f"vllm-{role}"
        headless_flag = " --headless" if role == "worker" else ""
        exec_cmd = f"{env_cmd}vllm serve --model {hf_path} {dtype_flag} --tensor-parallel-size {tp_size} --pipeline-parallel-size {pp_size} --nnodes {pp_size} --node-rank {node_rank} --master-addr {head_backplane_ip} --master-port 29500 --max-model-len {max_len} --gpu-memory-utilization {gpu_util} {vllm_args}{headless_flag} --port 8000"
        env_var_name = "CLUSTER_COMMAND"
    else:
        container_name = "vllm-standalone"
        exec_cmd = f"{env_cmd}vllm serve --model {hf_path} {dtype_flag} --tensor-parallel-size {tp_size} --pipeline-parallel-size {pp_size} --max-model-len {max_len} --gpu-memory-utilization {gpu_util} {vllm_args} --port 8000"
        env_var_name = "STANDALONE_COMMAND"

    return f"""# AUTOGENERATED MANIFEST
CLUSTER_IMAGE=nvcr.io/nvidia/vllm:26.05.post1-py3
STANDALONE_IMAGE=nvcr.io/nvidia/vllm:26.05.post1-py3
CLUSTER_CONTAINER_NAME={container_name}
STANDALONE_CONTAINER_NAME={container_name}
NODE_HEAD_MGT_IP={head_backplane_ip}
STANDALONE_VOLUME_MOUNT={host_details.get('volume_mount', '')}
CLUSTER_VOLUME_MOUNT={host_details.get('volume_mount', '')}
HF_TOKEN={hf_token}
STANDALONE_COMMAND=""
CLUSTER_COMMAND=""
{env_var_name}="{exec_cmd}"
"""

def execute_deployment(model: str, target_nodes: int, is_batch: bool, head_identifier: str, dry_run: bool = False):
    catalog = get_catalog()
    
    if model not in catalog.get("models", {}):
        raise ValueError(f"Model missing from catalog: {model}")
        
    config = catalog["models"][model]
    topo_key = f"{target_nodes}_node"
    
    if topo_key not in config.get("topologies", {}):
        raise ValueError(f"Topology missing for {model}: {topo_key}")
        
    topo_config = config["topologies"][topo_key]
    hosts_config = catalog.get("hosts", {})
    
    active_hosts = ping_sweep(hosts_config)
    if len(active_hosts) < target_nodes:
        raise RuntimeError("Insufficient nodes online to meet topology requirements.")
        
    head_name, head_details = find_host_by_identifier(head_identifier, hosts_config)
    if head_name not in active_hosts:
        raise RuntimeError(f"Requested Head node ({head_identifier}) is offline.")
        
    deploy_hosts = [head_name] + [n for n in active_hosts.keys() if n != head_name]
    deploy_hosts = deploy_hosts[:target_nodes]
    head_backplane_ip = head_details['networks']['backplane']

    for rank, host_name in enumerate(deploy_hosts):
        role = "head" if rank == 0 else "worker"
        host_details = hosts_config[host_name]
        ip, _ = active_hosts[host_name]
        user = host_details['ssh_user']
        compute_dir = host_details['compute_dir']
        
        with ssh_mux_session(ip, user):
            enforce_safeguards(ip, user, is_batch, dry_run)
            env_data = build_deployment_string(model, role, head_backplane_ip, rank, config, topo_config, host_details)
            
            if dry_run: continue
            
            temp_file = Path(__file__).resolve().parent / f".env.tmp.{host_name}"
            with open(temp_file, "w") as tf:
                tf.write(env_data)
                
            run_scp(ip, user, str(temp_file), f"{compute_dir}/.env")
            if temp_file.exists():
                os.remove(temp_file)
                
            compose_file = "docker-compose.cluster.yml" if target_nodes > 1 else "docker-compose.standalone.yml"
            run_ssh(ip, user, ["docker", "compose", "-f", f"{compute_dir}/{compose_file}", "up", "-d"], timeout=30)
            
    print("\n[✓] Deployment sequence complete.")

def get_remote_logs(host_identifier: str, tail: int, follow: bool):
    catalog = get_catalog()
    host_name, details = find_host_by_identifier(host_identifier, catalog.get("hosts", {}))
    
    if not host_name:
        print(f"[-] Node {host_identifier} not found in catalog.")
        return
        
    ip, _ = resolve_management_ip(details)
    user = details['ssh_user']
    
    if not ip:
        print(f"[-] Node {host_name} is currently offline.")
        return
    
    with ssh_mux_session(ip, user):
        res = run_ssh(ip, user, ["docker", "ps", "--filter", "name=vllm", "--format", "'{{.Names}}'"], capture=True, timeout=15)
        containers = [c.strip().strip("'\"") for c in res.stdout.splitlines() if c.strip()]
        
        if not containers:
            print(f"[-] No active models running on {host_name}.")
            return
            
        log_args = ["docker", "logs", "--tail", str(tail)]
        if follow: log_args.append("-f")
        log_args.append(containers[0])
        
        print(f"[*] Streaming logs for {containers[0]} on {host_name}...")
        run_ssh(ip, user, log_args, interactive=follow)

def sync_compose_templates(dry_run: bool = False):
    catalog = get_catalog()
    hosts_config = catalog.get("hosts", {})
    active_hosts = ping_sweep(hosts_config)
    
    for host_name, details in hosts_config.items():
        if host_name not in active_hosts: continue
        
        ip, _ = active_hosts[host_name]
        user = details['ssh_user']
        compute_dir = details['compute_dir']
        
        if dry_run: continue
        
        with ssh_mux_session(ip, user):
            run_ssh(ip, user, ["mkdir", "-p", compute_dir], timeout=15)
            for template in ["docker-compose.standalone.yml", "docker-compose.cluster.yml"]:
                local_path = str(Path(__file__).resolve().parent / template)
                if Path(local_path).exists():
                    run_scp(ip, user, local_path, f"{compute_dir}/{template}", timeout=15)
                else:
                    print(f"  [!] Missing local template: {template}")
                    
    print("\n[✓] Synchronized templates.")

def check_cluster_status():
    catalog = get_catalog()
    hosts_config = catalog.get("hosts", {})
    active_hosts = ping_sweep(hosts_config)
    
    print(f"\n{'HOST':<12} | {'ACTIVE IP':<20} | {'DOCKER':<8} | {'ACTIVE RUNTIMES'}")
    print("-" * 75)
    
    for host_name, details in hosts_config.items():
        if host_name not in active_hosts:
            print(f"{host_name:<12} | {'OFFLINE':<20} | {'OFFLINE':<8}")
            continue
            
        ip, _ = active_hosts[host_name]
        user = details['ssh_user']
        compute_dir = details['compute_dir']
        
        with ssh_mux_session(ip, user):
            res = run_ssh(ip, user, ["docker", "info"], capture=True, timeout=15)
            if res.returncode != 0:
                print(f"{host_name:<12} | {ip:<20} | {'STOPPED':<8}")
                continue
            
            c_res = run_ssh(ip, user, ["docker", "ps", "--format", "'{{.Names}} ({{.Status}})'"], capture=True, timeout=15)
            
            m_res = run_ssh(ip, user, ["cat", f"{compute_dir}/.env"], capture=True, timeout=5)
            model_id = ""
            for line in m_res.stdout.splitlines():
                if "--model " in line:
                    model_id = line.split("--model ")[1].split()[0].strip("\"'").split("/")[-1]
                    break
                    
            container_output = c_res.stdout.strip().strip("'\"")
            active_str = f"{container_output}  ➔  [{model_id}]" if container_output and model_id else container_output
            
            print(f"{host_name:<12} | {ip:<20} | {'ACTIVE':<8} | {active_str}")

# ==============================================================================
# FASTAPI ENGINE (OPTIONAL DAEMON MODE)
# ==============================================================================
app = FastAPI()

class DeployRequest(BaseModel):
    model: str
    nodes: int
    head: Optional[str] = "spark-4"
    dry_run: Optional[bool] = False

@app.post("/deploy")
async def api_deploy(request: DeployRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(execute_deployment, request.model, request.nodes, True, request.head, request.dry_run)
    return {"status": "accepted"}

# ==============================================================================
# MAIN ENTRYPOINT / CLI ROUTER
# ==============================================================================
def main():
    parser = argparse.ArgumentParser(description="DGX Spark Cluster Orchestrator")
    subparsers = parser.add_subparsers(dest="mode", required=True)
    
    cli = subparsers.add_parser("cli", help="Command Line Interface Mode")
    cli.add_argument("action", choices=["deploy", "teardown", "status", "logs", "sync", "authorize-key"])
    cli.add_argument("--model", type=str, help="Model alias from models.yaml")
    cli.add_argument("--nodes", type=int, default=1, choices=[1, 2], help="Number of nodes to deploy to")
    cli.add_argument("--head", default="spark-4", type=str, help="Hostname of the head node")
    cli.add_argument("--host", default="spark-4", type=str, help="Target hostname for remote logs")
    cli.add_argument("--tail", type=int, default=100, help="Number of log lines to stream")
    cli.add_argument("-f", action="store_true", help="Follow live log stream")
    cli.add_argument("-y", action="store_true", help="Bypass sanity check confirmations (batch mode)")
    cli.add_argument("--dry-run", action="store_true", help="Simulate deployment without modifying infrastructure")
    cli.add_argument("--key", type=str, default=None, help="Path to local public key file for authorize-key")
    
    daemon = subparsers.add_parser("daemon", help="Run FastAPI Background Daemon")
    daemon.add_argument("--port", type=int, default=8080)
    
    args = parser.parse_args()

    if args.mode == "daemon":
        uvicorn.run(app, host="0.0.0.0", port=args.port)
        
    elif args.mode == "cli":
        if args.action == "status":
            check_cluster_status()
        elif args.action == "sync":
            sync_compose_templates(args.dry_run)
        elif args.action == "logs":
            get_remote_logs(args.host, args.tail, args.f)
        elif args.action == "authorize-key":
            authorize_user_key(args.key)
        elif args.action == "deploy":
            execute_deployment(args.model, args.nodes, args.y, args.head, args.dry_run)
        elif args.action == "teardown":
            print("[*] Tearing down active topologies across active nodes...")
            catalog = get_catalog()
            hosts_config = catalog.get("hosts", {})
            active_hosts = ping_sweep(hosts_config)
            
            for host_name, details in hosts_config.items():
                if host_name in active_hosts:
                    ip, interface = active_hosts[host_name]
                    user = details['ssh_user']
                    compute_dir = details['compute_dir']
                    
                    print(f"  -> Shutting down runtimes on {host_name} ({ip} via {interface})...")
                    with ssh_mux_session(ip, user):
                        for compose_file in ["docker-compose.cluster.yml", "docker-compose.standalone.yml"]:
                            run_ssh(ip, user, ["docker", "compose", "-f", f"{compute_dir}/{compose_file}", "down"])
                            
            print("[✓] Teardown complete.")

if __name__ == "__main__":
    main()
