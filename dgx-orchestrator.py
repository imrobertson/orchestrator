#!/usr/bin/env python3
import argparse
import datetime
import json
import os
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
BASE_DIR = Path("/opt/dgx-cluster-control")
SHARED_KEY_PATH = BASE_DIR / "id_dgx_orchestrator"
MODELS_YAML_PATH = BASE_DIR / "models.yaml"
LOAD_TIMES_PATH = BASE_DIR / "load_times.json"

HOSTS = {
    "spark-4": {"ip": "10.0.14.43", "alias": "spark-9dbe", "role": "head"},
    "spark-3": {"ip": "10.0.14.41", "alias": "spark-6e63", "role": "worker"}
}

NETWORK_STATE_FILE = BASE_DIR / ".network_mode"


# --- Helper Functions ---
def resolve_user_identity_key() -> str:
    """
    OpenSSH strictness (0600) workaround for shared keys (0640).
    Auto-stages a copy of the shared key into ~/.ssh/id_dgx_orchestrator if needed.
    """
    user_ssh_dir = Path.home() / ".ssh"
    user_ssh_dir.mkdir(parents=True, exist_ok=True)
    target_key = user_ssh_dir / "id_dgx_orchestrator"

    if SHARED_KEY_PATH.exists():
        try:
            shutil.copy2(SHARED_KEY_PATH, target_key)
            os.chmod(target_key, 0o600)
            return str(target_key)
        except Exception:
            pass
    
    # Fallback to default user key if staging fails
    default_key = user_ssh_dir / "id_ed25519"
    if default_key.exists():
        return str(default_key)
    
    return str(SHARED_KEY_PATH)


def run_ssh(ip: str, user: str, command_list: list, capture: bool = True, timeout: int = 10) -> subprocess.CompletedProcess:
    key_path = resolve_user_identity_key()
    ssh_cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=5",
        "-i", key_path,
        f"{user}@{ip}"
    ] + command_list

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
    Handles Grace Blackwell (GB10) LPDDR5x Unified Memory where memory fields return [N/A].
    """
    cmd = ["/usr/bin/nvidia-smi", "--query-gpu=temperature.gpu,utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"]
    res = run_ssh(ip, user, cmd, capture=True, timeout=10)
    if res.returncode != 0:
        res = run_ssh(ip, user, ["nvidia-smi", "--query-gpu=temperature.gpu,utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"], capture=True, timeout=10)

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


def get_cluster_status() -> dict:
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
        
        # Safe format without pipe characters to avoid remote shell pipeline parsing bugs
        res = run_ssh(ip, user, ["docker", "ps", "--format", "'{{.Names}}::{{.Image}}'"], timeout=8)
        
        if res.returncode == 0:
            docker_status = "ONLINE"
            host_status = "ONLINE"
            active_container = "None"
            loaded_model = "None"

            lines = [l.strip().strip("'") for l in res.stdout.strip().splitlines() if l.strip()]
            for line in lines:
                parts = line.split("::")
                if parts:
                    c_name = parts[0]
                    if c_name in ["vllm-standalone", "vllm-head", "vllm-worker"]:
                        active_container = c_name
                        # Inspect container to extract model arg
                        inspect_res = run_ssh(ip, user, ["docker", "inspect", c_name, "--format", "'{{json .Config.Cmd}}'"], timeout=5)
                        if inspect_res.returncode == 0 and "--model" in inspect_res.stdout:
                            try:
                                cmd_parts = json.loads(inspect_res.stdout.strip().strip("'"))
                                if "--model" in cmd_parts:
                                    idx = cmd_parts.index("--model")
                                    if idx + 1 < len(cmd_parts):
                                        loaded_model = cmd_parts[idx + 1].split("/")[-1]
                            except Exception:
                                loaded_model = "Active Container"
                        else:
                            loaded_model = "vLLM Instance"

            telemetry = get_lightweight_telemetry(ip, user)

            status_data["hosts"][host] = {
                "ip": ip,
                "status": host_status,
                "docker": docker_status,
                "container": active_container,
                "active_model": loaded_model,
                "telemetry": telemetry
            }
        else:
            status_data["hosts"][host] = {
                "ip": ip,
                "status": "OFFLINE",
                "docker": "UNREACHABLE",
                "container": "None",
                "active_model": "None",
                "telemetry": {}
            }

    return status_data


def load_model_catalog() -> dict:
    if not MODELS_YAML_PATH.exists():
        return {"models": {}}
    try:
        with open(MODELS_YAML_PATH, "r") as f:
            return {"catalog": yaml.safe_load(f)}
    except Exception as e:
        return {"error": str(e), "catalog": {}}


def execute_teardown() -> dict:
    results = {}
    for host, meta in HOSTS.items():
        ip = meta["ip"]
        res = run_ssh(ip, "tetrel", ["docker", "rm", "-f", "vllm-standalone", "vllm-head", "vllm-worker"], timeout=15)
        results[host] = "Purged" if res.returncode == 0 else f"Error: {res.stderr.strip()}"
    return results


def execute_deployment(model: str, nodes: int, head: str, user_id: str) -> dict:
    # 1. Global Pre-Deployment Teardown (Flushes VRAM & removes containers)
    execute_teardown()

    # 2. Lock Clocks on Target Nodes
    target_hosts = ["spark-4", "spark-3"] if nodes == 2 else [head]
    for h in target_hosts:
        ip = HOSTS[h]["ip"]
        run_ssh(ip, "tetrel", ["sudo", "nvidia-smi", "-lgc", "300,1800"], timeout=10)

    # 3. Deployment Hook Placeholder
    return {
        "status": "success",
        "message": f"Deployment sequence for {model} across {nodes} node(s) initiated by {user_id}.",
        "targets": target_hosts
    }


def get_container_logs(host: str, tail: int = 40) -> dict:
    if host not in HOSTS:
        return {"logs": ["Invalid target host specified."]}
    
    ip = HOSTS[host]["ip"]
    res = run_ssh(ip, "tetrel", ["docker", "ps", "--format", "{{.Names}}"], timeout=5)
    if res.returncode != 0:
        return {"logs": ["Failed to connect to host or Docker daemon unreachable."]}

    containers = [c.strip() for c in res.stdout.strip().splitlines() if c.strip() in ["vllm-standalone", "vllm-head", "vllm-worker"]]
    if not containers:
        return {"logs": ["No active vLLM containers running on this node."]}

    c_name = containers[0]
    log_res = run_ssh(ip, "tetrel", ["docker", "logs", "--tail", str(tail), c_name], timeout=10)
    
    logs = log_res.stdout.splitlines() if log_res.returncode == 0 else log_res.stderr.splitlines()
    return {"logs": logs if logs else ["Log buffer empty."]}


# --- FastAPI Web Server Definition ---
if HAS_FASTAPI:
    app = FastAPI(title="Tetrel Security DGX Control Plane API", version="4.6.0")
    
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    class DeployRequest(BaseModel):
        model: str
        nodes: int
        head: str = "spark-4"
        user_id: str = "dashboard_user"

    class NetworkToggleRequest(BaseModel):
        offline: bool

    @app.get("/api/status")
    def api_status():
        return get_cluster_status()

    @app.get("/api/catalog")
    def api_catalog():
        return load_model_catalog()

    @app.get("/api/logs/{host}")
    def api_logs(host: str, tail: int = 40):
        return get_container_logs(host, tail)

    @app.post("/api/deploy")
    def api_deploy(req: DeployRequest):
        res = execute_deployment(req.model, req.nodes, req.head, req.user_id)
        if res.get("status") != "success":
            raise HTTPException(status_code=400, detail=res.get("message"))
        return res

    @app.post("/api/teardown")
    def api_teardown():
        return execute_teardown()

    @app.post("/api/toggle-network")
    def api_toggle_network(req: NetworkToggleRequest):
        mode_str = "OFFLINE" if req.offline else "ONLINE"
        try:
            NETWORK_STATE_FILE.write_text(f"Working in {mode_str} mode")
            return {"status": "success", "mode": mode_str}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))


# --- CLI / Main Entry Point ---
def main():
    parser = argparse.ArgumentParser(description="Tetrel Security DGX Cluster Orchestrator")
    subparsers = parser.add_subparsers(dest="subcommand")

    # Daemon subcommand
    daemon_parser = subparsers.add_parser("daemon", help="Run FastAPI API Daemon Service")
    daemon_parser.add_argument("--port", type=int, default=5001, help="Port to bind daemon server")

    # CLI subcommand (wrapper compatibility)
    cli_parser = subparsers.add_parser("cli", help="CLI Mode Wrapper")
    cli_sub = cli_parser.add_subparsers(dest="cli_action")
    
    # Register actions under CLI wrapper
    for action in ["deploy", "teardown", "status", "logs", "sync", "authorize-key", "menu", "daemon"]:
        p = cli_sub.add_parser(action)
        if action == "daemon":
            p.add_argument("--port", type=int, default=5001)

    # Top-level direct actions
    subparsers.add_parser("status")
    subparsers.add_parser("teardown")
    subparsers.add_parser("menu")

    args = parser.parse_args()

    # Handle wrapper pass-through (e.g. dgx-config daemon)
    if args.subcommand == "cli" and args.cli_action == "daemon":
        args.subcommand = "daemon"
        args.port = getattr(args, "port", 5001)

    if args.subcommand == "daemon":
        if not HAS_FASTAPI:
            print("[-] Error: fastapi and uvicorn packages are required to run daemon mode.")
            print("[-] Run: pip3 install --user fastapi uvicorn pydantic pyyaml")
            sys.exit(1)
        print(f"[+] Starting Tetrel Security API Daemon on port {args.port}...")
        uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="info")

    elif args.subcommand in ["status", "cli"] and getattr(args, "cli_action", None) == "status":
        print(json.dumps(get_cluster_status(), indent=2))

    elif args.subcommand in ["teardown", "cli"] and getattr(args, "cli_action", None) == "teardown":
        print("[+] Initiating Cluster Teardown...")
        print(json.dumps(execute_teardown(), indent=2))

    else:
        # Default fallback to menu
        print("=== Tetrel Security DGX Orchestrator CLI ===")
        print(json.dumps(get_cluster_status(), indent=2))

if __name__ == "__main__":
    main()
