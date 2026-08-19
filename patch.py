#!/usr/bin/env python3
"""
TETREL SECURITY - ORCHESTRATOR & DASHBOARD METRICS PATCH
--------------------------------------------------------------------------------
Adds real-time vLLM engine metrics scraping (TPS & active request streams) 
to dgx-orchestrator.py and updates html/index.html header UI.
"""

from pathlib import Path
import re

BASE_DIR = Path(__file__).resolve().parent

# --- 1. Patch dgx-orchestrator.py ---
orch_file = BASE_DIR / "dgx-orchestrator.py"
if orch_file.exists():
    text = orch_file.read_text()

    # Add get_vllm_metrics helper if missing
    if "def get_vllm_metrics(" not in text:
        metrics_func = '''
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
'''
        # Insert before get_cluster_status
        text = text.replace("def get_cluster_status() -> dict:", metrics_func + "\ndef get_cluster_status() -> dict:")

    # Update get_cluster_status to include metrics in return payload
    if "system_tps" not in text:
        old_status_block = """    status_data = {
        "server_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S EST"),
        "network_mode": "Working in OFFLINE mode" if offline_mode else "Working in ONLINE mode",
        "cluster_ready": cluster_ready,
        "hosts": {}
    }"""
        new_status_block = """    vllm_metrics = get_vllm_metrics(HOSTS["spark-4"]["ip"]) if cluster_ready else {"tps": 0.0, "running_requests": 0, "waiting_requests": 0}

    status_data = {
        "server_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S EST"),
        "network_mode": "Working in OFFLINE mode" if offline_mode else "Working in ONLINE mode",
        "cluster_ready": cluster_ready,
        "system_tps": vllm_metrics["tps"],
        "running_requests": vllm_metrics["running_requests"],
        "waiting_requests": vllm_metrics["waiting_requests"],
        "hosts": {}
    }"""
        text = text.replace(old_status_block, new_status_block)

    # Update interactive_menu banner format
    old_menu_header = 'print(f"Server Time: {status[\'server_time\']} | Mode: {status[\'network_mode\']} | vLLM API Ready: {status[\'cluster_ready\']}\\n")'
    new_menu_header = 'print(f"Server Time: {status[\'server_time\']} | Mode: {status[\'network_mode\']} | API: {\'READY\' if status[\'cluster_ready\'] else \'OFFLINE\'} | TPS: {status.get(\'system_tps\', 0.0)} tok/s | Streams: {status.get(\'running_requests\', 0)} active ({status.get(\'waiting_requests\', 0)} queued)\\n")'
    text = text.replace(old_menu_header, new_menu_header)

    orch_file.write_text(text)
    print("[✓] Successfully patched dgx-orchestrator.py with TPS & stream counter metrics.")
else:
    print(f"[!] File not found: {orch_file}")

# --- 2. Patch html/index.html ---
html_file = BASE_DIR / "html" / "index.html"
if html_file.exists():
    html_text = html_file.read_text()

    # Add TPS and Stream counters to header status bar if not present
    if "tps-badge" not in html_text:
        # Patch JS polling function to render TPS & Streams
        js_find = "document.getElementById('cluster-ready').textContent = data.cluster_ready ? 'ONLINE' : 'OFFLINE';"
        js_replace = """document.getElementById('cluster-ready').textContent = data.cluster_ready ? 'ONLINE' : 'OFFLINE';
        if (document.getElementById('system-tps')) {
            document.getElementById('system-tps').textContent = `${data.system_tps || 0.0} tok/s`;
        }
        if (document.getElementById('active-streams')) {
            document.getElementById('active-streams').textContent = `${data.running_requests || 0} active (${data.waiting_requests || 0} queued)`;
        }"""
        html_text = html_text.replace(js_find, js_replace)

        # Inject metrics elements into HTML header grid
        header_find = 'id="cluster-ready"'
        if header_find in html_text:
            header_inject = 'id="cluster-ready">OFFLINE</span></div><div class="stat-box"><span class="stat-label">SPEEDOMETER</span><span id="system-tps" class="stat-value">0.0 tok/s</span></div><div class="stat-box"><span class="stat-label">THREADS / STREAMS</span><span id="active-streams" class="stat-value">0 active</span>'
            html_text = html_text.replace('id="cluster-ready">OFFLINE</span>', header_inject, 1)

        html_file.write_text(html_text)
        print("[✓] Successfully patched html/index.html dashboard UI.")
else:
    print(f"[!] File not found: {html_file}")
