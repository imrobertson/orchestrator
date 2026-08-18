This is a quick reference for the DGX Spark cluster.

== Access ==

Go to `http://maestro:5000` or use `dgx-config menu` in the terminal.

=== Dashboard Usage Notes ===

* '''Grace Blackwell Unified Memory:''' GB10 uses LPDDR5x memory shared between the CPU and GPU. Standard memory queries return `[N/A]`, so the dashboard safely reports this as '''Unified / 131072 MB'''. Watch the `GPU: %` metric for compute saturation.
* '''Live Log Routing:''' The dashboard defaults to the `spark-4` (Head) node logs. If a 2-node deployment hangs, use the dropdown to switch to the `spark-3 Node` (Worker). Worker logs often contain the actual stack trace for cross-node networking timeouts.
* '''User ID / Auditor Tracking:''' The input box defaults to `dashboard_user`. Type your identifier here to inject your identity into the Docker execution context. This ensures remote `auth.log` files on the Spark nodes capture exactly who initiated deployments or teardowns.

== Essential Secrets & Key Management ==

To perform operations, your credentials must be configured.

* '''HuggingFace Token:''' Ensure `/opt/dgx-cluster-control/.secrets` exists and contains `HF_TOKEN="your_token"` to prevent authentication errors when pulling gated models. Alternatively, `export HF_TOKEN="your_token"` in your terminal before using the CLI.
* '''SSH Keys:''' If operating directly on `maestro` (bypassing Tailscale SSO), authorize your personal SSH key once so the orchestrator can audit deployments under your specific identity:

sudo -u tetrel dgx-config authorize-key --key ~/.ssh/id_ed25519.pub


== Deploying ==

Use the dashboard or run `dgx-config deploy --model MODEL --nodes N`.

=== Topology & Memory Guards ===
When selecting a model in the dashboard, invalid topologies are automatically hidden based on the model's capabilities (e.g., hiding 1-Node options for massive models) to prevent Out-Of-Memory (OOM) errors. If a valid 1-Node topology is selected, a secondary dropdown appears allowing you to target either `spark-4` or `spark-3`.

== Air-Gapped & Offline Operations ==

To deploy models without internet connectivity, you must pre-cache the assets and toggle the cluster into Offline Mode.

1. '''Pre-Cache Assets:''' Run the pre-fetcher script to download all Docker images and HuggingFace safetensors to the local NVMe cache on both nodes.

cd /opt/dgx-cluster-control
python3 cache_cluster_assets.py

2. '''Toggle Offline Mode:''' In the Web Dashboard, click the green '''"🌐 ONLINE MODE"''' badge to toggle to '''"🔒 OFFLINE MODE"'''. This injects `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1` into all ''new'' deployments, forcing them to load strictly from the local NVMe cache. (Note: This does not affect models that are already running).

== Teardown ==

Click "Teardown Runtimes" or run `dgx-config teardown`.

'''Note:''' This is a global nuke. It executes a forceful `docker rm -f` against all vLLM containers across '''both''' physical nodes simultaneously to instantly flush LPDDR5x memory pools and release port 8000.

== Performance Benchmarking ==

Validate real-world token throughput and latency using the integrated benchmark tool.

cd /opt/dgx-cluster-control
python3 benchmark.py

All runs are automatically appended to `benchmark_ledger.csv` with precise Time-To-First-Token (TTFT) and Decode speed metrics. The tool is hardened against vLLM Multi-Token Prediction (MTP) stream buffering to ensure mathematical accuracy.