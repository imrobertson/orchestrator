= DGX Cluster Orchestrator: User Manual =

Welcome to the '''Maestro Control Plane'''. This system allows you to effortlessly deploy, monitor, and tear down large language models across our twin-node Grace Blackwell (GB10) cluster (<code>spark-3</code> and <code>spark-4</code>). 

You can control the cluster using either the '''Web Dashboard''' or the '''Command Line Interface (CLI)'''. 

''(Note: Because you access <code>maestro</code> via Tailscale SSH, your identity is automatically verified and audited. No SSH key setup is required!)''

== Method 1: The Web Dashboard (Recommended) ==
The dashboard is the easiest way to visualize cluster health and manage models.

* '''Access the Dashboard:''' Open your web browser and navigate to <code>http://<MAESTRO_IP>:5000</code>.

=== Dashboard Features ===
* '''Live Status:''' The top panel shows the real-time health of <code>spark-3</code> and <code>spark-4</code>, including GPU temperature, utilization, and memory usage. 
* '''Deploy a Model:''' 
# Select a model from the dropdown (options are dynamically loaded from our catalog).
# Choose the topology (e.g., 1-Node or 2-Node).
# Click '''Deploy Model'''.
* '''Teardown:''' Click the red '''Teardown Active Runtimes''' button to instantly kill active models and free up GPU memory. 
* '''View Logs:''' Scroll to the full-width bottom panel to see a live terminal trace of the container startup and execution logs.

== Method 2: The Command Line (<code>dgx-config</code>) ==
For users who prefer the terminal or want to script deployments, SSH into <code>maestro</code> and use the <code>dgx-config</code> wrapper.

=== The Interactive Menu ===
If you want a guided terminal experience, simply run:
<syntaxhighlight lang="bash">
dgx-config menu
</syntaxhighlight>
This will print the cluster status and provide a step-by-step prompt to select and deploy a model.

=== Quick Commands Reference ===
* '''Check Cluster Status:'''
<syntaxhighlight lang="bash">
dgx-config status
</syntaxhighlight>
* '''Deploy a Model:'''
<syntaxhighlight lang="bash">
dgx-config deploy --model qwen-2.5-coder-32b --nodes 2
</syntaxhighlight>
* '''Clear the Cluster (Teardown):'''
<syntaxhighlight lang="bash">
dgx-config teardown
</syntaxhighlight>
* '''Check Container Logs:'''
<syntaxhighlight lang="bash">
dgx-config logs --host spark-4 --tail 50
</syntaxhighlight>

== Model Catalog & Capabilities (As of August 15, 2026) ==

Use the CLI keys in the table below with the <code>--model</code> flag, or select them from the Web Dashboard.

{| class="wikitable"
|-
! Model CLI Key
! Topology
! Context Window
! Strengths & Primary Use Case
|-
| <code>deepseek-v4-flash-nvfp4</code>
| 2-Node (Cluster)
| 65,536
| High-speed, highly optimized FP8 reasoning & logic.
|-
| <code>deepseek-r1-nvfp4</code>
| 2-Node (Cluster)
| 65,536
| Advanced reasoning, chain-of-thought, and complex problem-solving.
|-
| <code>deepseek-r1-distill-qwen-32b</code>
| 1-Node / 2-Node
| 32,768 / 131,072
| Highly efficient reasoning distilled into a medium-weight model architecture.
|-
| <code>nemotron-3.5-lightning-nvfp4</code><br><code>nemotron-3.5-lightning-bf16</code>
| 1-Node (Standalone)
| 131,072
| Speculative decoding setup designed for ultra-low latency enterprise chat.
|-
| <code>muse-glimmer-30b</code><br><code>muse-glimmer-30b-nvfp4</code>
| 1-Node (Standalone)
| 131,072
| Multimodal vision-language model tasks and tool-calling.
|-
| <code>qwen-3.8-27b</code><br><code>qwen-3.8-27b-nvfp4</code>
| 1-Node / 2-Node
| 262,144
| Massive context window; ideal for large document QA and extensive needle-in-a-haystack tasks.
|-
| <code>qwen-3.6-27b-nvfp4</code>
| 1-Node / 2-Node
| 262,144
| Long-context processing accelerated by speculative next-token prediction.
|-
| <code>qwen-3.5-122b</code>
| 2-Node (Cluster)
| 32,768
| Heavyweight knowledge, deep analysis, and robust instruction following.
|-
| <code>qwen-2.5-coder-32b</code>
| 1-Node / 2-Node
| 32,768 / 131,072
| Best-in-class code generation, debugging, and software engineering tasks.
|-
| <code>gemma-4-31b</code>
| 1-Node (Standalone)
| 32,768
| Strong general-purpose instruction tuning from Google.
|-
| <code>llama-4-fp4</code><br><code>llama-4-fp8</code>
| 1-Node / 2-Node
| 16,384 / 65,536
| Next-gen general reasoning and strict instruction following.
|-
| <code>llama-3.3-70b</code>
| 2-Node (Cluster)
| 56,000
| Highly capable, reliable general-purpose chat and language tasks.
|}

== ⚠️ Troubleshooting & Important Notes ==

* '''Node Shows as "OFFLINE" or "UNREACHABLE":''' 
If a spark node reboots, '''Docker does not start automatically''' (this is a deliberate safeguard against boot-looping crashed GPUs). To fix this, SSH into the offline node and start Docker manually:
<syntaxhighlight lang="bash">
ssh tetrel@10.0.14.43 "sudo systemctl start docker"
</syntaxhighlight>
* '''"Out of Memory" (OOM) Errors:'''
If a deployment fails instantly, it usually means the previous model wasn't cleaned up properly. Run <code>dgx-config teardown</code> to flush the GPUs before trying again.
* '''Single-Node vs. Multi-Node:'''
Pay attention to the topology requirements. Models that exceed 128GB of VRAM (like 70B+ parameters) must be deployed with <code>--nodes 2</code> to split the memory across both machines.
