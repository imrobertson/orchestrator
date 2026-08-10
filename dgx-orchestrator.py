#!/usr/bin/env python3
import os
import sys
import yaml
import time
import argparse
import subprocess
import contextlib
from typing import List, Dict, Any, Optional, Tuple
from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel
import uvicorn

# ------------------------------------------------------------------------------
# PORTED EXPLICIT MULTIPLEXING ENGINE FROM SYNCME.SH (WITH DEVNULL FIX)
# ------------------------------------------------------------------------------
@contextlib.contextmanager
def ssh_mux_session(ip: str, user: str):
    """
    Explicitly spawns, manages, and terminates a master background SSH tunnel
    identical to the syncme.sh bash implementation to prevent fail2ban triggers.
    """
    socket_path = f"/tmp/ssh-mux-{user}-{ip}.sock"
    
    # Clean up any orphaned socket files from previously aborted runs
    if os.path.exists(socket_path):
        try: os.remove(socket_path)
        except Exception: pass

    # 1. Establish the master background tunnel
    # Equivalent to syncme.sh: ssh -f -N -M -S "$MUX_SOCKET"
    # DEVNULL redirect is CRITICAL here to prevent Python from hanging on inherited FDs
    cmd = [
        "ssh", "-f", "-N", "-M",
        "-S", socket_path,
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=5",
        f"{user}@{ip}"
    ]
    
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    except subprocess.TimeoutExpired:
        print(f"  [-] Critical: Multiplex connection to {ip} timed out.")
        sys.exit(1)
    
    try:
        yield socket_path
    finally:
        # 2. Explicitly sever the master SSH tunnel
        # Equivalent to syncme.sh: ssh -O exit -S "$MUX_SOCKET"
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
    
    # Force use of the master background tunnel if it is active
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
        res = subprocess.run(full_cmd, capture_output=capture, text=True, timeout=timeout)
        return res
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
        res = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=timeout)
        return res
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args=scp_cmd, returncode=124, stdout="", stderr="SCP Timeout.")

# ------------------------------------------------------------------------------
# INVENTORY CONTROL AND UTILITIES
# ------------------------------------------------------------------------------
def resolve_management_ip(host_details: dict) -> Tuple[Optional[str], str]:
    mgt = host_details.get('networks', {}).get('management', {})
    wired_ip = mgt.get('wired')
    wireless_ip = mgt.get('wireless')
    if wired_ip and local_ping(wired_ip): return wired_ip, "wired"
    if wireless_ip and local_ping(wireless_ip): return wireless_ip, "wireless"
    return None, "unreachable"

def find_host_by_identifier(identifier: str, hosts_config: dict) -> Tuple[Optional[str], Optional[dict]]:
    for host_name, details in hosts_config.items():
        if identifier == host_name or identifier in details.get('aliases', []): return host_name, details
        networks = details.get('networks', {})
        mgt = networks.get('management', {})
        if identifier in [mgt.get('wired'), mgt.get('wireless'), networks.get('backplane')]: return host_name, details
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

# ------------------------------------------------------------------------------
# CORE CLI ACTIONS
# ------------------------------------------------------------------------------
def build_deployment_string(model_id: str, role: str, head_backplane_ip: str, node_rank: int, config: dict, topo: dict, host_details: dict) -> str:
    hf_token = ""
    if os.path.exists(".secrets"):
        with open(".secrets", "r") as sf:
            for line in sf:
                if line.startswith("HF_TOKEN="): hf_token = line.strip().split("=", 1)[1].strip("'\" ")
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

    return f"""# AUTOGENERATED MANIFEST\nCLUSTER_IMAGE=nvcr.io/nvidia/vllm:26.05.post1-py3\nSTANDALONE_IMAGE=nvcr.io/nvidia/vllm:26.05.post1-py3\nCLUSTER_CONTAINER_NAME={container_name}\nSTANDALONE_CONTAINER_NAME={container_name}\nNODE_HEAD_MGT_IP={head_backplane_ip}\nSTANDALONE_VOLUME_MOUNT={host_details.get('volume_mount', '')}\nHF_TOKEN={hf_token}\nSTANDALONE_COMMAND=""\nCLUSTER_COMMAND=""\n{env_var_name}="{exec_cmd}"\n"""

def execute_deployment(model: str, target_nodes: int, is_batch: bool, head_identifier: str, dry_run: bool = False):
    with open("models.yaml", "r") as f: catalog = yaml.safe_load(f)
    if model not in catalog.get("models", {}): raise ValueError(f"Model missing: {model}")
    config = catalog["models"][model]; topo_key = f"{target_nodes}_node"
    if topo_key not in config.get("topologies", {}): raise ValueError(f"Topology missing: {topo_key}")
    topo_config = config["topologies"][topo_key]; hosts_config = catalog.get("hosts", {})
    active_hosts = ping_sweep(hosts_config)
    if len(active_hosts) < target_nodes: raise RuntimeError("Insufficient nodes online.")
    head_name, head_details = find_host_by_identifier(head_identifier, hosts_config)
    if head_name not in active_hosts: raise RuntimeError("Head node offline.")
    deploy_hosts = [head_name] + [n for n in active_hosts.keys() if n != head_name]
    deploy_hosts = deploy_hosts[:target_nodes]; head_backplane_ip = head_details['networks']['backplane']

    for rank, host_name in enumerate(deploy_hosts):
        role = "head" if rank == 0 else "worker"; host_details = hosts_config[host_name]
        ip, _ = active_hosts[host_name]; user = host_details['ssh_user']; compute_dir = host_details['compute_dir']
        
        # Deploy wrapped inside an explicit multiplex control loop
        with ssh_mux_session(ip, user):
            enforce_safeguards(ip, user, is_batch, dry_run)
            env_data = build_deployment_string(model, role, head_backplane_ip, rank, config, topo_config, host_details)
            if dry_run: continue
            temp_file = ".env.tmp"
            with open(temp_file, "w") as tf: tf.write(env_data)
            run_scp(ip, user, temp_file, f"{compute_dir}/.env")
            if os.path.exists(temp_file): os.remove(temp_file)
            compose_file = "docker-compose.cluster.yml" if target_nodes > 1 else "docker-compose.standalone.yml"
            run_ssh(ip, user, ["docker", "compose", "-f", f"{compute_dir}/{compose_file}", "up", "-d"], timeout=30)
    print("\n[✓] Sequence complete.")

def get_remote_logs(host_identifier: str, tail: int, follow: bool):
    with open("models.yaml", "r") as f: catalog = yaml.safe_load(f)
    host_name, details = find_host_by_identifier(host_identifier, catalog.get("hosts", {}))
    ip, _ = resolve_management_ip(details); user = details['ssh_user']
    
    with ssh_mux_session(ip, user):
        res = run_ssh(ip, user, ["docker", "ps", "--filter", "name=vllm", "--format", "{{.Names}}"], capture=True, timeout=15)
        containers = [c.strip() for c in res.stdout.splitlines() if c.strip()]
        if not containers: print("[-] No active models running."); return
        log_args = ["docker", "logs", "--tail", str(tail)]
        if follow: log_args.append("-f")
        log_args.append(containers[0])
        run_ssh(ip, user, log_args, interactive=follow)

def sync_compose_templates(dry_run: bool = False):
    with open("models.yaml", "r") as f: catalog = yaml.safe_load(f)
    hosts_config = catalog.get("hosts", {})
    active_hosts = ping_sweep(hosts_config)
    for host_name, details in hosts_config.items():
        if host_name not in active_hosts: continue
        ip, _ = active_hosts[host_name]; user = details['ssh_user']; compute_dir = details['compute_dir']
        if dry_run: continue
        with ssh_mux_session(ip, user):
            run_ssh(ip, user, ["mkdir", "-p", compute_dir], timeout=15)
            for t in ["docker-compose.standalone.yml", "docker-compose.cluster.yml"]:
                run_scp(ip, user, t, f"{compute_dir}/{t}", timeout=15)
    print("\n[✓] Synchronized templates.")

def check_cluster_status():
    with open("models.yaml", "r") as f: catalog = yaml.safe_load(f)
    hosts_config = catalog.get("hosts", {}); active_hosts = ping_sweep(hosts_config)
    print(f"\n{'HOST':<12} | {'ACTIVE IP':<20} | {'DOCKER':<8} | {'ACTIVE RUNTIMES'}")
    for host_name, details in hosts_config.items():
        if host_name not in active_hosts: print(f"{host_name:<12} | {'OFFLINE':<20} | {'OFFLINE':<8}"); continue
        ip, _ = active_hosts[host_name]; user = details['ssh_user']; compute_dir = details['compute_dir']
        
        with ssh_mux_session(ip, user):
            res = run_ssh(ip, user, ["docker", "info"], capture=True, timeout=15)
            if res.returncode != 0: print(f"{host_name:<12} | {ip:<20} | {'STOPPED':<8}"); continue
            
            c_res = run_ssh(ip, user, ["docker", "ps", "--format", "{{.Names}} ({{.Status}})"], capture=True, timeout=15)
            print(f"{host_name:<12} | {ip:<20} | {'ACTIVE':<8} | {c_res.stdout.strip()}")

# ------------------------------------------------------------------------------
# API ENTRY ENGINE
# ------------------------------------------------------------------------------
app = FastAPI()
class DeployRequest(BaseModel):
    model: str; nodes: int; head: Optional[str] = "spark-4"; dry_run: Optional[bool] = False

@app.post("/deploy")
async def api_deploy(request: DeployRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(execute_deployment, request.model, request.nodes, True, request.head, request.dry_run)
    return {"status": "accepted"}

def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="mode", required=True)
    cli = subparsers.add_parser("cli")
    cli.add_argument("action", choices=["deploy", "teardown", "status", "logs", "sync"])
    cli.add_argument("--model"); cli.add_argument("--nodes", type=int, default=1)
    cli.add_argument("--head", default="spark-4"); cli.add_argument("--host", default="spark-4")
    cli.add_argument("--tail", type=int, default=100); cli.add_argument("-f", action="store_true")
    cli.add_argument("-y", action="store_true"); cli.add_argument("--dry-run", action="store_true")
    daemon = subparsers.add_parser("daemon"); daemon.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    if args.mode == "daemon": uvicorn.run(app, host="0.0.0.0", port=args.port)
    elif args.mode == "cli":
        if args.action == "status": check_cluster_status()
        elif args.action == "sync": sync_compose_templates(args.dry_run)
        elif args.action == "logs": get_remote_logs(args.host, args.tail, args.f)
        elif args.action == "deploy": execute_deployment(args.model, args.nodes, args.y, args.head, args.dry_run)
        elif args.action == "teardown":
            print("[*] Tearing down active topologies across active nodes...")
            with open("models.yaml", "r") as f: catalog = yaml.safe_load(f)
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

if __name__ == "__main__": main()
