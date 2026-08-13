= DGX Cluster Control Plane (<tt>dgx-cluster-control</tt>) — V3.9.4 Standalone Edition =

This repository contains the <tt>dgx-config</tt> orchestration suite[cite: 7]. It acts as the central control plane for managing distributed vLLM deployments across a twin-node RoCEv2 NVIDIA Grace Blackwell fabric[cite: 7].

----

== Target Infrastructure ==

This tool brokers deployments to the remote inference cluster[cite: 7].

* '''Hardware:''' 2x GB10 (Grace Blackwell) DGX Sparks[cite: 7]. Both nodes natively run the vLLM engine[cite: 7].
* '''spark-4 (Head Node):''' <tt>10.0.14.43</tt> (<tt>spark-9dbe</tt>)[cite: 7]. Features 128GB of Unified Memory[cite: 7].
* '''spark-3 (Worker Node):''' <tt>10.0.14.41</tt> (<tt>spark-6e63</tt>)[cite: 7]. Features 128GB of Unified Memory and operates with strict headless guardrails[cite: 7].

----

== Low-Level Network Fabric & Hardware Safeguards ==

=== 1. Network Interface Targets & Gloo/NCCL Bindings ===

* '''Management TCP Interface (<tt>enp1s0f0np0</tt>):''' All SSH orchestration, administrative commands, and <tt>dgx-config</tt> calls route strictly across the management subnet (<tt>10.0.14.x</tt>)[cite: 7].
* '''Gloo & NCCL Interface Binding (<tt>enp1s0f0np0</tt>):''' Multi-node distributed topologies '''must''' pass <tt>GLOO_SOCKET_IFNAME=enp1s0f0np0</tt> and <tt>NCCL_SOCKET_IFNAME=enp1s0f0np0</tt>[cite: 7].
* ⚠️ '''Critical Errata:''' PyTorch Gloo requires a physical Linux network device name (e.g., <tt>enp1s0f0np0</tt>)[cite: 7]. Passing an IP prefix or CIDR block (such as <tt>192.168.99.</tt>) causes <tt>ProcessGroupGloo</tt> to crash with <tt>RuntimeError: ifa != nullptr</tt>[cite: 7].
* '''RoCEv2 Link Layer (200Gbps ConnectX-7):'''[cite: 7]
** '''InfiniBand / HCA Target:''' <tt>rocep1s0f0</tt>[cite: 7]
** '''RoCE GID Index:''' <tt>NCCL_IB_GID_INDEX=3</tt>[cite: 7]
** '''Inter-Node Port:''' Port <tt>29500</tt> bound across <tt>192.168.99.x</tt> for PyTorch distributed backplanes[cite: 7].

=== 2. Self-Healing SSH Transport & Execution Timeouts ===

To prevent orphaned SSH multiplex processes or hung remote Docker daemons from freezing the orchestration control plane[cite: 7]:

* '''Automatic Socket Purging:''' Every execution automatically purges stale control sockets in both <tt>/tmp/ssh-mux-*</tt> and <tt>~/.ssh/cm-*</tt>[cite: 7].
* '''Execution Timeout Guards:''' Remote command invocations (such as <tt>docker rm -f</tt> or <tt>docker logs</tt>) are wrapped in explicit <tt>timeout 10</tt> execution guards to guarantee non-blocking return codes if a remote node experiences a GPU driver or socket lockup[cite: 7].

=== 3. Ubiquiti / UniFi Gateway IDS/IPS Mitigation ===

When executing <tt>dgx-config</tt> from a host located on a different VLAN or subnet (e.g., workstation <tt>192.168.1.x</tt> -> cluster <tt>10.0.14.x</tt>), automated rapid-fire SSH multiplexing and status polling can trigger UniFi Threat Management / Suricata (IPS/IDS) SSH brute-force heuristics[cite: 7].

* '''Symptom:''' Port 22 connections abruptly hang (<tt>Connection timed out</tt> or <tt>BLOCKED</tt>) specifically from the orchestrator workstation, while other machines or local L2 nodes continue to connect without issue[cite: 7].
* '''Resolution A (Permanent Gateway Unblock):''' Navigate to the UniFi Network Dashboard -> '''System Logs''' -> '''Security Detections''', locate the event blocking your workstation IP, and select '''Allow IP / Unblock Threat'''[cite: 7].
* '''Resolution B (Transparent L2 ProxyJump Bypass):''' Tunnel SSH traffic through an unblocked node on the target L2 management subnet to bypass inter-VLAN inspection rules completely[cite: 7]. Add the following to <tt>~/.ssh/config</tt> on the orchestration workstation[cite: 7]:

<pre>
Host 10.0.14.43 spark-9dbe spark-4
    HostName 10.0.14.43
    User tetrel
    ProxyJump tetrel@10.0.14.41
</pre>

=== 4. GPU Power & Hardware Clock Locks ===

To prevent Over-Current Protection (OCP) power excursions during heavy batch inference on Grace Blackwell chips, <tt>dgx-config</tt> automatically executes hardware clock locking (<tt>nvidia-smi -lgc 300,1800</tt>) on target nodes prior to spinning up containers[cite: 7].

=== 5. vLLM Runtime Container Environment ===

* '''Base NGC Image:''' <tt>nvcr.io/nvidia/vllm:26.05.post1-py3</tt>[cite: 7]
* '''Host Driver Requirement:''' Data Center driver release 580.159+ / 595.58+ with active <tt>nvidia-fabricmanager</tt> services[cite: 7].
* '''Execution Engine:''' Deployments write environment manifests (<tt>.env</tt>) remotely and launch via <tt>docker compose -f docker-compose.[standalone|cluster].yml up -d</tt>[cite: 7].
* '''Fail-Fast Verification (V3.9.1):''' <tt>dgx-orchestrator.py</tt> verifies container instantiation 2 seconds post-launch, failing immediately if Docker rejects or drops the container instance[cite: 7]. <tt>master_orchestrator.sh</tt> actively checks <tt>docker inspect</tt> status during engine startup to abort immediately if containers hit <tt>exited</tt> or <tt>dead</tt> states[cite: 7].
* '''Proactive Self-Cleaning (V3.9.2):''' <tt>execute_deployment</tt> explicitly runs <tt>docker rm -f <container_name></tt> prior to deploying, preventing container naming conflicts during standalone execution[cite: 7].

----

== Installation & Setup (System Administrators) ==

To install the orchestrator so it is available to all authorized users on the system[cite: 7]:

# '''Clone the Repository to <tt>/opt</tt>:'''
<pre>
sudo git clone <repo_url> /opt/dgx-cluster-control
sudo chown -R root:wheel /opt/dgx-cluster-control
sudo chmod -R 775 /opt/dgx-cluster-control
cd /opt/dgx-cluster-control
</pre>
# '''Build the Virtual Environment:'''
<pre>
sudo python3 -m venv dgx-env
sudo dgx-env/bin/pip install -r requirements.txt
</pre>
# '''Create the Global Symlink:'''
<pre>
sudo ln -s /opt/dgx-cluster-control/dgx-config /usr/local/bin/dgx-config
</pre>

----

== User Onboarding & SSH Key Errata (The Bootstrap Problem) ==

The orchestrator authenticates with the cluster using the '''caller's personal SSH key''' to ensure per-user auditing in the remote <tt>auth.log</tt>[cite: 7]. However, a new user cannot interact with the cluster until their public key is authorized on the Spark nodes[cite: 7].

Because manual SSH is blocked, a new user must "bootstrap" their key onto the cluster using one of two methods[cite: 7]:

=== Method A: Admin Authorization (Preferred) ===

An existing team member who already has cluster access can authorize the new user's key[cite: 7]:

<pre>
# An existing admin runs:
dgx-config authorize-key --key /home/newuser/.ssh/id_ed25519.pub
</pre>

=== Method B: The <tt>tetrel</tt> Fallback (Self-Serve) ===

If an admin is unavailable, a user in the <tt>wheel</tt> group can temporarily elevate to the <tt>tetrel</tt> service account (which already has a trusted key) to push their personal key[cite: 7]:

<pre>
# 1. Generate a key if you don't have one
ssh-keygen -t ed25519

# 2. Push your key using the tetrel service account
sudo -u tetrel /opt/dgx-cluster-control/dgx-config authorize-key --key ~/.ssh/id_ed25519.pub
</pre>

Once authorized, the user can run <tt>dgx-config</tt> natively without <tt>sudo</tt>[cite: 7].

----

== DGX Orchestrator (<tt>dgx-config</tt>) Guide ==

* '''Check Cluster Status:'''
<pre>
dgx-config status
</pre>

* '''Tear Down Active Cluster Runtimes:''' ''(Must be run before pivoting models to release VRAM)''[cite: 7].
<pre>
dgx-config teardown
</pre>

* '''Deploy a Model:'''
<pre>
dgx-config deploy --model <model_alias> --nodes <count>
</pre>

* '''Synchronize Compose Templates Across Nodes:'''
<pre>
dgx-config sync
</pre>

* '''Stream Live Remote Logs:'''
<pre>
dgx-config logs --host spark-4 --tail 100 -f
</pre>

* '''Authorize Public SSH Key:'''
<pre>
dgx-config authorize-key --key ~/.ssh/id_ed25519.pub
</pre>

=== Run the API Daemon (V3.9.1+) ===

The API Daemon option exposes a FastAPI web service on port 8080[cite: 7]. This allows external dashboards or automated workflow pipelines to programmatically trigger model deployments and teardowns over HTTP without direct interactive SSH sessions[cite: 7].

<pre>
dgx-config daemon --port 8080
</pre>

* '''POST <tt>/deploy</tt>''' - JSON Payload: <tt>{"model": "qwen-3.5-122b", "nodes": 2}</tt>[cite: 7]
* '''POST <tt>/teardown</tt>''' - Flushes active topologies across nodes[cite: 7].

----

== Core Model Catalog (<tt>models.yaml</tt>) ==

When deploying, reference the exact aliases defined in <tt>/opt/dgx-cluster-control/models.yaml</tt>[cite: 7].
''Note: Model footprints and VRAM capacities are tuned for Grace Blackwell (GB10) LPDDR5x unified memory constraints.''[cite: 7]

* '''DeepSeek Architectures:'''
** <tt>deepseek-v4-flash-nvfp4</tt>: <tt>Rarri/DeepSeek-V4-Flash-0731-NVFP4</tt> (Requires <tt>--nodes 2</tt>)[cite: 5]. Quantized NVFP4 release featuring ep-weight filtering[cite: 5]. Requires <tt>--disable-custom-all-reduce</tt> for RoCEv2 backplane stability, <tt>--no-async-scheduling</tt>, and <tt>--num-gpu-blocks-override 8192</tt>[cite: 5].

* '''Nemotron Architectures:'''
** <tt>nemotron-3.5-lightning</tt>: <tt>nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4</tt> (Requires <tt>--nodes 1</tt>)[cite: 5]. Agentic automation core configured with DSpark speculative decoding[cite: 5].

* '''Multimodal & Agentic Vision Architectures:'''
** <tt>muse-glimmer-30b</tt>: <tt>meta-models/Muse-Glimmer-30B</tt> (Requires <tt>--nodes 1</tt>)[cite: 5]. 30B dense vision-language model leveraging DFlash speculative configurations[cite: 5].

* '''Qwen Architectures:'''
** <tt>qwen-3.6-27b-nvfp4</tt>: <tt>nvidia/Qwen3.6-27B-NVFP4</tt> (Supports <tt>--nodes 1</tt> or <tt>--nodes 2</tt>)[cite: 5].
** <tt>qwen-3.8-27b</tt>: <tt>Qwen/Qwen3.8-27B</tt> (Requires <tt>--nodes 1</tt>)[cite: 5].
** <tt>qwen-3.5-122b</tt>: <tt>Qwen/Qwen3.5-122B-A10B-FP8</tt> (Requires <tt>--nodes 2</tt>)[cite: 5]. Deep cluster MoE OCR and document extraction engine[cite: 5]. Explicitly configured with <tt>GLOO_SOCKET_IFNAME=enp1s0f0np0</tt> and <tt>NCCL_SOCKET_IFNAME=enp1s0f0np0</tt>[cite: 5].

* '''Llama & Gemma Architectures:'''
** <tt>llama-3.3-70b</tt>: <tt>meta-llama/Llama-3.3-70B-Instruct</tt> (Requires <tt>--nodes 2</tt>)[cite: 5]. Sharded via pipeline parallelism (<tt>pp_size: 2</tt>) across twin 128GB DGX nodes to prevent OOM memory ceiling exhaustion. The single-node definition has been permanently retired.
** <tt>llama-4-fp4</tt>: <tt>nvidia/Llama-4-Scout-17B-16E-Instruct-FP4</tt> (Supports <tt>--nodes 1</tt> or <tt>--nodes 2</tt>)[cite: 5].
** <tt>llama-4-fp8</tt>: <tt>nvidia/Llama-4-Scout-17B-16E-Instruct-FP8</tt> (Supports <tt>--nodes 1</tt> or <tt>--nodes 2</tt>)[cite: 5].
** <tt>gemma-4-31b</tt>: <tt>google/gemma-4-31B-it</tt> (Requires <tt>--nodes 1</tt>)[cite: 5]. Reconstruction & layout sanitizer core[cite: 5].

----

= Release Tombstones & Fix Log =

=== 15. The V1 Engine Async Scheduling Bug (V3.9.4) ===

* '''The Trap:''' A known bug in vLLM on Blackwell GB10 (SM 12.1) causes the engine to crash with an <tt>AttributeError</tt> (missing <tt>sampled_token_ids</tt>) the moment it receives its first inference request due to flawed async scheduling.
* '''The Fix:''' Injected <tt>--no-async-scheduling</tt> globally across all <tt>models.yaml</tt> topologies to force the batch queue to bypass the broken method.

=== 14. The Unified Memory Profiler Trap (V3.9.4) ===

* '''The Trap:''' During startup, vLLM profiles available memory to size the KV cache. On the GB10's 128GB Unified Memory, the profiler falsely registers the OS's evictable page cache as free VRAM, drastically over-allocating the KV cache and pushing the entire system into Linux swap space.
* '''The Fix:''' Added <tt>--num-gpu-blocks-override 8192</tt> to all <tt>vllm_args</tt> definitions to hardcode the KV cache size, completely bypassing the flawed automatic profiler.

=== 13. The PyTorch Expandable Segments Fatal Trap (V3.9.3) ===

* '''The Trap:''' Standard optimization guides recommend setting <tt>PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True</tt> for Grace Blackwell unified memory. However, this setting is currently incompatible with vLLM's memory pool and causes EngineCore initialization to fail with a fatal <tt>SIGABRT</tt> during CUDA Graph binding and KV Cache initialization.
* '''The Fix:''' Ensure this variable is strictly removed across all cluster node deployments.

=== 12. DeepSeek RoCEv2 All-Reduce Hangs (V3.9.3) ===

* '''The Trap:''' vLLM hangs indefinitely during worker initialization on Grace Blackwell PCIe GPUs when executing custom all-reduce operations across a standard TCP/RoCEv2 network backplane without NVLink.
* '''The Fix:''' Enforced the <tt>--disable-custom-all-reduce</tt> flag across all twin-node architectures (including DeepSeek NVFP4 variants) in <tt>models.yaml</tt>.

=== 11. Proactive Standalone Self-Cleaning (V3.9.2) ===

* '''The Trap:''' Running <tt>dgx-config deploy</tt> natively failed if a stopped or crashed container from a previous deployment occupied the target container name (<tt>vllm-standalone</tt> / <tt>vllm-head</tt> / <tt>vllm-worker</tt>), causing <tt>docker compose up -d</tt> to conflict or fail silently[cite: 7].
* '''The Fix:''' Injected a proactive <tt>docker rm -f <container_name></tt> pass into <tt>dgx-orchestrator.py</tt> immediately before pushing the updated <tt>.env</tt> manifest and launching Docker Compose[cite: 7].

=== 10. The Cold Image Pull Telemetry Blindspot (V3.9.1) ===

* '''The Trap:''' Following a <tt>docker system prune</tt>, launching a deployment required pulling base image layers from NGC[cite: 7]. Because container instantiation stalled during download, readiness polling loops printed generic initialization states for up to 15 minutes, causing false assumptions of host deadlocks[cite: 7].
* '''The Fix:''' Added pre-flight image checks to identify cold image states and log active pull operations[cite: 7].

=== 9. The Llama 70B Single-Node OOM & Readiness Polling Blindspot (V3.9.1) ===

* '''The Trap:''' Attempting to deploy <tt>llama-3.3-70b</tt> on a single DGX node triggered an immediate <tt>CUDA out of memory</tt> exception during layer initialization[cite: 7]. The container crashed into an <tt>Exited (137)</tt> state[cite: 7]. However, <tt>master_orchestrator.sh</tt>'s <tt>wait_for_vllm_readiness</tt> loop continuously polled HTTP <tt>/health</tt> without inspecting container status, causing the orchestrator to hang for 25 minutes printing <tt>Initializing engine environment...</tt>[cite: 7].
* '''The Fix:''' Updated <tt>models.yaml</tt> and <tt>master_orchestrator.sh</tt> to permanently enforce <tt>--nodes 2</tt> for <tt>llama-3.3-70b</tt> deployments[cite: 7]. Injected an active <tt>docker inspect</tt> fail-fast check inside <tt>wait_for_vllm_readiness</tt> to detect <tt>exited</tt> or <tt>dead</tt> container states immediately, dump the last 50 log lines to stderr, and abort execution[cite: 7]. 

=== 8. Llama-3.3 CUDA Graph Kernel Lockup ===

* '''Bug:''' Attempting a deployment of Llama-3.3-70B occasionally froze the primary node's memory bus during PyTorch Inductor autotune graph compilation, causing SSH responsiveness to completely lock[cite: 7].
* '''Fix:''' Bound the <tt>--enforce-eager</tt> flag strictly to <tt>llama-3.3-70b</tt>'s definition in <tt>models.yaml</tt> to permanently bypass CUDA graph generation[cite: 7].

=== 7. Daemon Lifecycle Management (Cold-Boot Protections) ===

* '''Bug:''' Host nodes configured to auto-start Docker occasionally fell into reboot loops when encountering corrupted runtime configurations[cite: 7].
* '''Fix:''' Docker and Containerd services were permanently disabled on boot across the cluster[cite: 7]. Added remote wakeup injections directly into <tt>dgx-orchestrator.py</tt> to ensure daemons are safely initialized via the SSH multiplexer socket prior to deployment execution[cite: 7].

=== 6. Inter-VLAN Gateway Intrusion Prevention (IDS/IPS Errata) ===

* '''Bug:''' Rapid automated SSH multiplexing across subnets triggered UniFi Gateway IPS threat rules, causing port 22 connections to silently drop for the management host[cite: 7].
* '''Fix:''' Added documentation for UniFi Threat Management exception rules alongside <tt>ProxyJump</tt> tunneling to route SSH traffic over uninspected L2 local domains[cite: 7].

=== 5. Early SSH Authentication Error Trapping ===

* '''Bug:''' <tt>ssh_mux_session</tt> context manager suppressed SSH verification failures, allowing invalid authentication loops to proceed to file transfers[cite: 7].
* '''Fix:''' Added explicit <tt>if res.returncode != 0:</tt> checking inside the multiplex setup to abort with clear diagnostics if SSH keys fail[cite: 7].

=== 4. Non-Blocking Command Execution Timeouts ===

* '''Bug:''' If a remote node experienced a Docker lock or GPU driver freeze during teardown or log inspection, the host CLI hung forever[cite: 7].
* '''Fix:''' Wrapped remote SSH command calls in <tt>timeout 10</tt> guards to ensure hard exit codes and self-healing continuity in bash orchestrators[cite: 7].

=== 3. SSH Multiplexer Self-Healing & Socket Cleanup ===

* '''Bug:''' Stale <tt>/tmp/ssh-mux-*</tt> socket files from interrupted SSH sessions caused subsequent <tt>dgx-config</tt> executions to hang indefinitely[cite: 7].
* '''Fix:''' Updated both <tt>dgx-config</tt> and <tt>dgx-orchestrator.py</tt> to purge <tt>/tmp/ssh-mux-*</tt> socket files before initiating connection attempts[cite: 7].

=== 2. NCCL Socket Interface Alignment (<tt>NCCL_SOCKET_IFNAME</tt>) ===

* '''Bug:''' Worker processes reported <tt>NCCL WARN Bootstrap : no socket interface found</tt> when initialized with IP subnet prefixes[cite: 7].
* '''Fix:''' Aligned <tt>NCCL_SOCKET_IFNAME=enp1s0f0np0</tt> across all multi-node topology definitions in <tt>models.yaml</tt>[cite: 7].

=== 1. PyTorch Gloo Device Binding Fix (<tt>GLOO_SOCKET_IFNAME</tt>) ===

* '''Bug:''' Multi-node deployments crashed during worker initialization with <tt>RuntimeError: ifa != nullptr. Unable to find address for: 192.168.99.</tt>[cite: 7].
* '''Fix:''' Replaced the IP-prefix value <tt>192.168.99.</tt> with the physical host network device <tt>enp1s0f0np0</tt> in <tt>models.yaml</tt>[cite: 7]. Gloo uses Linux <tt>getifaddrs()</tt> interface matching and requires explicit interface names[cite: 7].

----

= ⚠️ Upgrade Errata (Action Required) =

If upgrading from v3.7 or earlier, review the following breaking operational changes[cite: 7]:

# '''Gloo/NCCL Configuration Update (Critical)'''[cite: 7]
#* Existing <tt>models.yaml</tt> files containing <tt>GLOO_SOCKET_IFNAME=192.168.99.</tt> or <tt>192.168.99.0/24</tt> '''must''' be updated to <tt>GLOO_SOCKET_IFNAME=enp1s0f0np0</tt>[cite: 7].
# '''SSH Authentication & Pipeline Breakage'''[cite: 7]
#* The <tt>dgx-config</tt> wrapper no longer brokers commands through the shared <tt>tetrel</tt> account via <tt>sudo</tt>[cite: 7].
#* Executing users or cron agents ''must'' run <tt>dgx-config authorize-key</tt> to register their public SSH keys across all cluster nodes prior to starting pipelines[cite: 7].