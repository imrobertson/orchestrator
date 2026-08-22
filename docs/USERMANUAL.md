= DGX Cluster Orchestrator: User Manual =

Welcome to the '''Maestro Control Plane'''. This system deploys, monitors, and tears down large language models across our twin-node Grace Blackwell (GB10) cluster (<code>spark-3</code> and <code>spark-4</code>).

You can control the cluster using either the '''Web Dashboard''' or the '''Command Line Interface (CLI)'''. This manual covers both, plus adding new models, offline operation, benchmarking, and troubleshooting — including a few real incidents this system has actually hit, not just hypothetical ones.

''(Note: Because you access <code>maestro</code> via Tailscale SSH, your identity is automatically verified and audited for the SSH hop itself. No SSH key setup is required to reach <code>maestro</code>. Per-deploy attribution beyond that — the dashboard's "User ID / Auditor" field, the CLI's forwarded <code>$USER</code> — is self-reported, not independently authenticated. See Troubleshooting.)''

== Method 1: The Web Dashboard (Recommended) ==
The dashboard is the easiest way to visualize cluster health and manage models.

* '''Access the Dashboard:''' Open your web browser and navigate to <code>http://<MAESTRO_IP>:5000</code>.

=== Dashboard Features ===
* '''Header bar:''' live cluster-wide throughput (<code>SPEED: X tok/s</code>) and request concurrency (<code>THREADS: X active (Y queued)</code>) whenever a model is serving, plus server time and an '''ONLINE MODE''' / '''OFFLINE MODE''' indicator.
* '''Per-host panels (<code>spark-4</code>, <code>spark-3</code>):''' Docker daemon status, active container name and state, currently loaded model, model status (<code>READY</code>, a warmup/loading stage, or <code>NONE</code>), an ETA while a model is still loading, and live '''TEMP''' / '''GPU''' / '''MEM''' readings.
* '''Deploy a Model (Model Deployer panel):'''
# Select a model from the '''Select Model''' dropdown — populated live from the current recipe catalog, not a fixed list (see the catalog note below the table).
# Choose the '''Topology''' (1-Node or 2-Node) and, for a 1-node deploy, the '''Target''' host.
# Enter a '''User ID / Auditor''' — a self-reported label for tracking who deployed what, not an authenticated identity.
# Click '''Deploy Model'''.
* '''Teardown:''' Click the red '''Teardown Runtimes''' button to kill active models on both hosts and free up GPU memory.
* '''Live Logs:''' The full-width bottom panel shows a live terminal trace of the selected host's container logs. Health-check and metrics-polling lines are filtered out automatically so this stays readable during normal operation.

=== Understanding the ETA display ===
When a model is loading, the ETA shown (e.g. <code>~340s remaining</code>) comes from an actual learned average of past load times for that exact model+topology combination, stored in <code>load_times.json</code> — not a fixed guess. The first time you ever deploy a given model+topology combination, you'll see <code>(Initial run - no history)</code> since there's nothing to average yet; after that, every successful deploy refines the estimate. If a load runs long, the display switches to <code>Finishing startup (+Ns over est.)</code> rather than showing a nonsensical negative countdown.

== Method 2: The Command Line (<code>dgx-config</code>) ==
For users who prefer the terminal or want to script deployments, SSH into <code>maestro</code> and use the <code>dgx-config</code> wrapper.

=== The Interactive Menu ===
If you want a guided terminal experience, simply run:
<syntaxhighlight lang="bash">
dgx-config menu
</syntaxhighlight>
This prints cluster status (including live throughput/queue depth) and walks you through selecting a model, topology, and target host, then confirms before deploying. It also shows the estimated load time for each topology option before you commit.

=== Quick Commands Reference ===
* '''Check Cluster Status:'''
<syntaxhighlight lang="bash">
dgx-config status
</syntaxhighlight>
* '''Deploy a Model:'''
<syntaxhighlight lang="bash">
dgx-config deploy --model qwen-2.5-coder-32b --nodes 2
</syntaxhighlight>
Useful extra flags:
:* <code>--head spark-3</code> — pick the target host for a 1-node deploy (default <code>spark-4</code>).
:* <code>--wait</code> — block until the model passes its HTTP health check instead of returning immediately. Recommended for scripted deploys where the next step depends on the model actually being ready.
:* <code>--benchmark</code> — after <code>--wait</code> succeeds, automatically run a 3-pass benchmark and save results to <code>benchmark_results.txt</code> / <code>benchmark_ledger.csv</code>.
:* <code>--dry-run</code> — print the exact <code>docker run</code> command(s) this deploy would send, without contacting either host or changing anything. See "Previewing a deploy" below.
* '''Clear the Cluster (Teardown):'''
<syntaxhighlight lang="bash">
dgx-config teardown
</syntaxhighlight>
* '''Check Container Logs:'''
<syntaxhighlight lang="bash">
dgx-config logs --host spark-4 --tail 50
</syntaxhighlight>
* '''Authorize a new SSH key on both hosts:'''
<syntaxhighlight lang="bash">
dgx-config authorize-key --key ~/.ssh/id_ed25519.pub
</syntaxhighlight>
Appends the given public key to <code>~/.ssh/authorized_keys</code> on both <code>spark-3</code> and <code>spark-4</code>. Rarely needed day-to-day — mainly for onboarding a new admin identity directly to the Spark hosts (separate from your Tailscale access to <code>maestro</code> itself).

=== Previewing a deploy (<code>--dry-run</code>) ===
Before deploying something unfamiliar, or if you're not sure a model/topology combination will actually work, run:
<syntaxhighlight lang="bash">
dgx-config deploy --model deepseek-v4-flash-0731-experimental --nodes 2 --dry-run
</syntaxhighlight>
This builds and prints the exact <code>docker run</code> command(s) the real deploy would send — every flag, every environment variable, every mount — without opening a single SSH connection or touching either host. It's the fastest way to confirm a recipe is doing what you expect, or to compare two recipes' actual generated commands side by side, with zero risk to a running cluster.

== Adding a New Model ==

Models are no longer defined in one shared file — each model is its own recipe file under <code>recipes/local/</code> (or <code>recipes/eugr/</code> for ones adapted from the community <code>eugr/spark-vllm-docker</code> project). To add one:

# Create <code>recipes/local/your-model-name.yaml</code>. '''The filename is the model's identity''' — it's exactly what you'll pass to <code>--model</code> and exactly what shows up in the dashboard dropdown. There is no separate internal name field to keep in sync with it (an earlier version of the schema had one; it caused a real outage when it drifted out of sync with the filename — see Troubleshooting).
# Fill in the required fields:
<syntaxhighlight lang="yaml">
recipe_version: '1'
hf_path: org/Your-Model-Name
gpu_util: 0.70
topologies:
  1_node:
    max_model_len: 32768
    tp_size: 1
    pp_size: 1
    env_vars:
      - OMP_NUM_THREADS=16
      - VLLM_CPU_OMP_THREADS=16
    vllm_args: >-
      --trust-remote-code --kv-cache-dtype fp8
</syntaxhighlight>
Only define the topologies (<code>1_node</code>, <code>2_node</code>) the model actually supports — a model that needs both Sparks' memory should only have a <code>2_node</code> block; deploying it with <code>--nodes 1</code> will then fail with a clear error instead of silently misbehaving.
# Optionally set <code>image:</code> if the model needs a non-default container (most don't — omitting it falls back to the cluster's default image).
# Save the file, then confirm it loaded:
<syntaxhighlight lang="bash">
dgx-config status
</syntaxhighlight>
If your model doesn't show up, or the '''entire''' dropdown looks empty (not just your new model missing), see "The model catalog looks empty" under Troubleshooting below — this is a real failure mode, not a hypothetical one.
# Sanity-check the generated command before a real deploy:
<syntaxhighlight lang="bash">
dgx-config deploy --model your-model-name --nodes 1 --dry-run
</syntaxhighlight>

== Air-Gapped / Offline Operations ==

* '''Pre-Cache Assets:''' Before disconnecting the cluster from the internet, run <code>cache_cluster_assets.py</code> on <code>maestro</code>. It reads the live model catalog and parallel-downloads both the Docker images and the HuggingFace safetensors for every cataloged model to both Spark nodes, so nothing needs a live download once you're offline.
* '''Toggle Offline Mode:''' The dashboard's network mode indicator switches the cluster into offline mode, which injects <code>HF_HUB_OFFLINE=1</code> and <code>TRANSFORMERS_OFFLINE=1</code> into every '''new''' container deployment from that point on (it doesn't retroactively affect an already-running container). Use this once assets are pre-cached, to guarantee no deploy silently tries to reach the internet and hangs.

== Benchmarking ==

Run a standalone benchmark against an already-deployed, healthy model:
<syntaxhighlight lang="bash">
python3 benchmark.py --host spark-4 --nodes 2
</syntaxhighlight>
Or have a deploy trigger one automatically once it's confirmed healthy:
<syntaxhighlight lang="bash">
dgx-config deploy --model qwen-2.5-coder-32b --nodes 1 --wait --benchmark
</syntaxhighlight>
Results (time-to-first-token, decode tokens/sec) are written to <code>benchmark_results.txt</code> and appended to <code>benchmark_ledger.csv</code> for historical comparison across runs.

== Model Catalog & Capabilities ==

Use the CLI keys in the table below with the <code>--model</code> flag, or select them from the Web Dashboard.

'''This table is a point-in-time snapshot, not the source of truth.''' The catalog is generated live from one YAML file per model — adding, removing, or editing a model doesn't require touching this document, which means this table can and will drift out of date. '''If this list disagrees with what <code>dgx-config status</code> or the dashboard's dropdown actually show you, trust the running system, not this page.'''

{| class="wikitable"
|-
! Model CLI Key
! Topology (max context)
! Notes
|-
| <code>deepseek-r1-distill-qwen-32b</code>
| 1-Node (32,768) / 2-Node (131,072)
| Reasoning-distilled into a mid-weight model; DeepSeek-R1 style chain-of-thought parser.
|-
| <code>deepseek-v4-flash</code>
| 2-Node (32,768)
| Full-precision DeepSeek V4 Flash, B12X-optimized image.
|-
| <code>deepseek-v4-flash-nvfp4</code>
| 2-Node (32,768)
| NVFP4-quantized variant, Ray-backed distributed execution.
|-
| <code>deepseek-v4-flash-0731-experimental</code>
| 2-Node (131,072)
| Newer DeepSeek V4 Flash checkpoint (0731), B12X image. Experimental — expect rougher edges than the non-experimental entries.
|-
| <code>deepseek-v4-flash-0731-experimental-nvfp4</code>
| 2-Node (393,216)
| NVFP4 variant of the above; largest context window in the catalog. Also experimental.
|-
| <code>nemotron-3.5-lightning-bf16</code><br><code>nemotron-3.5-lightning-nvfp4</code>
| 1-Node (131,072)
| Speculative decoding (DSpark draft model) for low-latency chat.
|-
| <code>muse-glimmer-30b</code><br><code>muse-glimmer-30b-nvfp4</code>
| 1-Node (131,072)
| Tool-calling and reasoning-parser support tuned for this model family.
|-
| <code>qwen-2.5-coder-32b</code>
| 1-Node (32,768) / 2-Node (131,072)
| Code generation and debugging.
|-
| <code>qwen-3.5-122b</code>
| 2-Node (32,768)
| Largest parameter count in the catalog; general-purpose heavyweight reasoning.
|-
| <code>qwen-3.6-27b-nvfp4</code>
| 1-Node / 2-Node (262,144)
| Long-context, MTP speculative decoding.
|-
| <code>qwen-3.8-27b</code><br><code>qwen-3.8-27b-nvfp4</code>
| 1-Node / 2-Node (262,144)
| Long-context, tool-calling, MTP speculative decoding.
|-
| <code>llama-3.3-70b</code>
| 2-Node (56,000)
| General-purpose chat and instruction following.
|-
| <code>llama-4-fp4</code>
| 1-Node (32,768) / 2-Node (32,768)
| FP4-quantized Llama 4 Scout.
|-
| <code>llama-4-fp8</code>
| 1-Node (16,384) / 2-Node (65,536)
| FP8-quantized Llama 4 Scout — note the smaller 1-node context vs. the FP4 variant.
|-
| <code>gemma-4-31b</code>
| 1-Node (32,768)
| General-purpose instruction tuning from Google.
|}

'''On the "Strengths & Primary Use Case" column from earlier versions of this table:''' removed rather than guessed. The catalog schema has fields for exactly this (task / context class / latency class), but they aren't populated for any model yet — planned, not live. Once they are, this table (or better, a link straight to the live catalog) can show real capability data instead of hand-written summaries that can silently go stale.

== ⚠️ Troubleshooting & Important Notes ==

* '''Node Shows as "OFFLINE" or "UNREACHABLE":'''
If a spark node reboots, '''Docker does not start automatically''' (this is a deliberate safeguard against boot-looping crashed GPUs). To fix this, SSH into the offline node and start Docker manually:
<syntaxhighlight lang="bash">
ssh tetrel@10.0.14.43 "sudo systemctl start docker"
</syntaxhighlight>
(Use <code>10.0.14.41</code> for <code>spark-3</code>.)

* '''"Out of Memory" (OOM) Errors:'''
If a deployment fails instantly, it usually means the previous model wasn't cleaned up properly. Run <code>dgx-config teardown</code> to flush the GPUs before trying again.

* '''Single-Node vs. Multi-Node:'''
Pay attention to the topology requirements — not every model supports both. Several catalog entries are 2-node-only (e.g. everything in the DeepSeek V4 Flash family, <code>qwen-3.5-122b</code>, <code>llama-3.3-70b</code>) because they need both Sparks' memory to fit. Trying to deploy a 2-node-only model with <code>--nodes 1</code> fails with a clear error rather than deploying incorrectly. If you're unsure, use <code>--dry-run</code> first.

* '''A deploy seems stuck on "COMPILING KERNELS":'''
This is a real, expected stage on a genuinely fresh container — Triton/CUDA JIT-compiles kernels on first run, which can take a while. If it seems to be taking unusually long every single time (not just once), the persistent JIT cache mount may not be set up correctly on that host; worth flagging to an admin rather than repeatedly retrying, since retrying doesn't help if the cache mount itself is the problem.

* '''The model dropdown / catalog looks completely empty:'''
This has happened for real, not just in theory: a single malformed recipe file can currently take down the '''entire''' catalog, not just itself, because the loader fails closed rather than skipping the one bad file. If the dropdown is unexpectedly empty (versus just missing one model you expected), that's the most likely cause — check whether a recipe file was recently added or edited, and flag it rather than assuming user error. <code>dgx-config status</code> from the CLI sometimes surfaces more detail than the dashboard does.

* '''"User ID / Auditor" isn't an identity check:'''
Both the dashboard field and the CLI's user attribution are self-reported labels, not authentication — anyone can type anything there. Your Tailscale SSH session to <code>maestro</code> itself is verified and audited, but per-deploy attribution beyond that is currently on the honor system.

* '''My new recipe file doesn't show up, or breaks the whole catalog:'''
Most commonly a YAML syntax error, or a topology block missing one of the required fields (<code>max_model_len</code>, <code>tp_size</code>, <code>pp_size</code>). Double-check indentation and that every topology you define has all three. Since one bad file can currently break every model's listing (see above), it's worth removing a newly-added file and confirming the catalog comes back before assuming the problem is elsewhere.
