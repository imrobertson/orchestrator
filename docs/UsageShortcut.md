This is a quick reference for the DGX Spark cluster.

== Access ==

Go to `http://maestro:5000` or use `dgx-config menu` in the terminal.

=== Dashboard Usage Notes ===

* '''Grace Blackwell Unified Memory:''' GB10 uses LPDDR5x memory shared between the CPU and GPU. Standard memory queries return `[N/A]`, so the dashboard safely reports this as '''Unified / 131072 MB'''. Watch the `GPU: %` metric for compute saturation.
* '''Live Log Routing:''' The dashboard defaults to the `spark-4` (Head) node logs. If a 2-node deployment hangs, use the dropdown to switch to the `spark-3 Node` (Worker). Worker logs often contain the actual stack trace for cross-node networking timeouts.
* '''User ID / Auditor Tracking:''' The input box defaults to `dashboard_user`. Type your identifier here to inject your identity into the Docker execution context. This is self-reported, not authenticated — it helps distinguish who ran what in casual review, but don't treat it as a verified audit trail.
* '''Version Badge:''' The header, next to Server Time (shown in UTC), displays the currently-running orchestrator version. Useful for confirming a fix actually deployed rather than assuming it did.

== Essential Secrets & Key Management ==

To perform operations, your credentials must be configured.

* '''HuggingFace Token:''' Ensure `~/docker/orchestrator/.secrets` exists and contains `HF_TOKEN="your_token"` to prevent authentication errors when pulling gated models. Alternatively, `export HF_TOKEN="your_token"` in your terminal before using the CLI.
* '''SSH Keys:''' If operating directly on `maestro` (bypassing Tailscale SSO), authorize your personal SSH key once so the orchestrator can reach the Spark hosts on your behalf:

dgx-config authorize-key --key ~/.ssh/id_ed25519.pub


== Deploying ==

Use the dashboard or run `dgx-config deploy --model MODEL --nodes N`.

Not sure a model/topology combo is valid, or want to sanity-check what will actually get sent before committing? Add `--dry-run` — prints the exact `docker run` command(s), no SSH connection made, nothing touched:

dgx-config deploy --model MODEL --nodes N --dry-run

=== Topology & Memory Guards ===
When selecting a model in the dashboard, invalid topologies are automatically hidden based on the model's recipe (e.g., hiding 1-Node options for models whose recipe only defines a `2_node` topology) to prevent Out-Of-Memory (OOM) errors. If a valid 1-Node topology is selected, a secondary dropdown appears allowing you to target either `spark-4` or `spark-3`.

== Air-Gapped & Offline Operations ==

To deploy models without internet connectivity, you must pre-cache the assets and toggle the cluster into Offline Mode.

1. '''Pre-Cache Assets:''' Run the pre-fetcher script to download all Docker images and HuggingFace safetensors to the local NVMe cache on both nodes.

cd ~/docker/orchestrator
python3 cache_cluster_assets.py

2. '''Toggle Offline Mode:''' In the Web Dashboard, click the green '''"🌐 ONLINE MODE"''' badge to toggle to '''"🔒 OFFLINE MODE"'''. This injects `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1` into all ''new'' deployments, forcing them to load strictly from the local NVMe cache. (Note: This does not affect models that are already running).

== Teardown ==

Click "Teardown Runtimes" or run `dgx-config teardown`.

'''Note:''' This is graceful, not instant. It sends SIGTERM to the engine and Ray inside each container, then `docker stop` (with a grace period), and only falls back to `docker rm -f` for anything still standing — this protects in-progress JIT compiles from corruption, which a hard kill can leave half-written. Expect it to take up to roughly a minute on a 2-node cluster; the dashboard's Teardown button shows live phase progress for the duration rather than a static "in progress" label. It also sweeps orphaned shared-memory segments left behind by Ray/vLLM on both hosts as its final step. While teardown (or a deploy) is running, the other dashboard controls lock to prevent a conflicting operation starting mid-flight.

== Performance Benchmarking ==

Validate real-world token throughput and latency using the integrated benchmark tool.

cd ~/docker/orchestrator
python3 benchmark.py

All runs are automatically appended to `benchmark_ledger.csv` with precise Time-To-First-Token (TTFT) and Decode speed metrics. The tool is hardened against vLLM Multi-Token Prediction (MTP) stream buffering to ensure mathematical accuracy.

== If something looks wrong ==

* '''Model dropdown totally empty''' (not just missing one model): a single malformed recipe file can currently break the whole catalog, not just itself. Worth flagging rather than assuming it's just slow to load — see `USERMANUAL.md`'s Troubleshooting section.
* '''Dashboard frozen — same numbers for a long time:''' a stale backend computation is possible, not just a slow poll. Check `stale`/`stale_for_seconds` at the API level if you can, and flag it rather than assuming it'll clear itself — see `docs/TOMBSTONES.md` #76 for the history.
* '''Everything else:''' `USERMANUAL.md` has the fuller troubleshooting list; this page is deliberately just the fast path.
