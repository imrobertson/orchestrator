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


== Reserved Hosts ==

A host can be marked `reserved: true` in `cluster_config.yaml` when it carries something that must not be disturbed by routine work — a resident agent, a shared endpoint the team depends on. '''Deploy and teardown both refuse to touch a reserved host without `--force`.'''

This is why a bare `dgx-config teardown --all` can come back with a refusal rather than doing anything: "everything" includes the reserved host. Same for the dashboard's Teardown button, which posts no host list and therefore means the whole cluster — expect it to show a refusal in the error banner while a reserved host is up. Use `--host <other-node>` to clear just the node you meant.

Two things a reserved host does '''not''' protect against:
* '''2-node deploys always span both hosts''', so any 2-node work needs `--force` and will take the reserved host's service down. That's intended — it should be a decision, not a surprise.
* '''Raw `docker run` over SSH bypasses the orchestrator entirely''' and never sees this guard. `tests/ab_test.py` does its own check for that reason; anything hand-rolled won't.

Which host is reserved is a property of the machine, not of any deployment — see the comments in `cluster_config.yaml` itself.

== Deploying ==

Use the dashboard or run `dgx-config deploy --model MODEL --nodes N`.

'''Where it lands if you don't say:''' a 1-node deploy with no `--head` goes to `cluster_config.yaml`'s `default_deploy_target` (the scratch node). A 2-node deploy still puts the head on the first host listed under `hosts:`, so the head node stays the same across topologies. Pass `--head <node>` to override either. Add `--force` if the target is reserved and you mean it.

Not sure a model/topology combo is valid, or want to sanity-check what will actually get sent before committing? Add `--dry-run` — prints the exact `docker run` command(s), no SSH connection made, nothing touched:

dgx-config deploy --model MODEL --nodes N --dry-run

Note that `--dry-run` is '''not''' exempt from the reserved-host check. It reports what a real deploy would do, so it reports the refusal a real deploy would hit rather than printing a command that would in fact be blocked.

=== Topology & Memory Guards ===
When selecting a model in the dashboard, invalid topologies are automatically hidden based on the model's recipe (e.g., hiding 1-Node options for models whose recipe only defines a `2_node` topology) to prevent Out-Of-Memory (OOM) errors. If a valid 1-Node topology is selected, a secondary dropdown appears allowing you to target either `spark-4` or `spark-3`.

Be aware that this dropdown auto-syncs to whichever host is currently serving, which is not necessarily the one you want to deploy to — if the serving host is reserved, the selector will keep landing on it and the deploy will be refused. Change it by hand, or use the CLI, which defaults to the scratch node.

== Air-Gapped & Offline Operations ==

To deploy models without internet connectivity, you must pre-cache the assets and toggle the cluster into Offline Mode.

1. '''Pre-Cache Assets:''' Run the pre-fetcher script to download all Docker images and HuggingFace safetensors to the local NVMe cache on both nodes.

cd ~/docker/orchestrator
python3 cache_cluster_assets.py

2. '''Toggle Offline Mode:''' In the Web Dashboard, click the green '''"🌐 ONLINE MODE"''' badge to toggle to '''"🔒 OFFLINE MODE"'''. This injects `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1` into all ''new'' deployments, forcing them to load strictly from the local NVMe cache. (Note: This does not affect models that are already running).

== Teardown ==

Click "Teardown Runtimes", or run teardown with an explicit scope:

dgx-config teardown --host spark-3
dgx-config teardown --host spark-3,spark-4
dgx-config teardown --all

'''There is no argument-free teardown any more.''' A bare `dgx-config teardown` exits with a usage error rather than clearing the cluster. That's deliberate: destroying every runtime should be something you typed on purpose, not something you got by default. An unrecognised host name is also an error listing the valid names, rather than a silent no-op that reports a clean teardown of nothing.

'''Note:''' This is graceful, not instant. It sends SIGTERM to the engine and Ray inside each container, then `docker stop` (with a grace period), and only falls back to `docker rm -f` for anything still standing — this protects in-progress JIT compiles from corruption, which a hard kill can leave half-written. Expect it to take up to roughly a minute on a 2-node cluster; the dashboard's Teardown button shows live phase progress for the duration rather than a static "in progress" label. It also sweeps orphaned shared-memory segments left behind by Ray/vLLM on the targeted hosts as its final step. While teardown (or a deploy) is running, the other dashboard controls lock to prevent a conflicting operation starting mid-flight.

Tearing down one host leaves the other host's session tracking alone — the dashboard's session stats and the model ledger keep accruing for whatever is still serving, rather than resetting as they used to.

== Performance Benchmarking ==

Validate real-world token throughput and latency using the integrated benchmark tool.

cd ~/docker/orchestrator
python3 benchmark.py

All runs are automatically appended to `benchmark_ledger.csv` with precise Time-To-First-Token (TTFT) and Decode speed metrics. The tool is hardened against vLLM Multi-Token Prediction (MTP) stream buffering to ensure mathematical accuracy.

== A/B Testing Two Recipes ==

Skip the manual deploy/benchmark/teardown cycle — `tests/ab_test.py` does the whole thing for one or two variants automatically, logs everything regardless of pass/fail, and prints a side-by-side comparison if you gave it two:

python3 tests/ab_test.py --variant-a recipe-one --variant-b recipe-two --prompts all

Either side can be an existing recipe name, an existing recipe with fields overridden, or a fully ad-hoc config — see `docs/AB_TEST_USAGE.md` for the full reference. Two things worth knowing up front: an image with a non-default entrypoint (needs `--a-entrypoint`) can't go through mods, and can't use `--a-nodes 2` — that's a structural limit of the deploy path, not a flag you're missing. And `--host` now defaults to the scratch node and refuses a reserved host outright, with no `--force` of its own.

== If something looks wrong ==

* '''Model dropdown totally empty''' (not just missing one model): a single malformed recipe file can currently break the whole catalog, not just itself. Worth flagging rather than assuming it's just slow to load — see `USERMANUAL.md`'s Troubleshooting section.
* '''Dashboard frozen — same numbers for a long time:''' a stale backend computation is possible, not just a slow poll. Check `stale`/`stale_for_seconds` at the API level if you can, and flag it rather than assuming it'll clear itself — see `docs/TOMBSTONES.md` #76 for the history.
* '''Teardown or deploy refused with "marked reserved":''' working as intended — see the Reserved Hosts section above. Re-run with `--force` if you actually mean it, or scope to the other host.
* '''Dashboard session speed looks wrong while two models are serving:''' only one host is tracked at a time. See `docs/BACKLOG-session-tracker-multi-model.md` — the numbers `benchmark.py` reports are unaffected.
* '''Everything else:''' `USERMANUAL.md` has the fuller troubleshooting list; this page is deliberately just the fast path.
