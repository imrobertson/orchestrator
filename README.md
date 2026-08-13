= DGX Cluster Control Plane (<tt>dgx-cluster-control</tt>) — V3.9.8 Standalone Edition =

This repository contains the <tt>dgx-config</tt> orchestration suite[cite: 6]. It acts as the central control plane for managing distributed vLLM deployments across a twin-node RoCEv2 NVIDIA Grace Blackwell fabric[cite: 6].

----

== Target Infrastructure ==

This tool brokers deployments to the remote inference cluster[cite: 6].

* '''Hardware:''' 2x GB10 (Grace Blackwell) DGX Sparks[cite: 6]. Both nodes natively run the vLLM engine[cite: 6].
* '''spark-4 (Head Node):''' <tt>10.0.14.43</tt> (<tt>spark-9dbe</tt>)[cite: 6]. Features 128GB of Unified Memory running in strict headless mode[cite: 6].
* '''spark-3 (Worker Node):''' <tt>10.0.14.41</tt> (<tt>spark-6e63</tt>)[cite: 6]. Features 128GB of Unified Memory running in strict headless mode[cite: 6].

----

== Low-Level Network Fabric & Hardware Safeguards ==

=== 1. Network Interface Targets & Gloo/NCCL Bindings ===

* '''Management TCP Interface (<tt>enp1s0f0np0</tt>):''' All SSH orchestration, administrative commands, and <tt>dgx-config</tt> calls route strictly across the management subnet (<tt>10.0.14.x</tt>)[cite: 6].
* '''Master Store Rendezvous (<tt>10.0.14.43</tt>):''' <tt>dgx-orchestrator.py</tt> dynamically binds <tt>--master-addr</tt> to the head node's active management IP (<tt>10.0.14.43</tt>) over <tt>enp1s0f0np0</tt> for Gloo control-plane process registration[cite: 6]. This isolates control-plane rendezvous traffic from the high-throughput 200Gbps RoCEv2 data plane[cite: 6].
* '''Gloo & NCCL Interface Binding (<tt>enp1s0f0np0</tt>):''' Multi-node distributed topologies '''must''' pass <tt>GLOO_SOCKET_IFNAME=enp1s0f0np0</tt> and <tt>NCCL_SOCKET_IFNAME=enp1s0f0np0</tt>[cite: 6].
* '''NCCL CUDA Memory Driver Disabling (<tt>NCCL_CUMEM_ENABLE=0</tt>):''' Multi-node Grace Blackwell (GB10) deployments '''must''' enforce <tt>NCCL_CUMEM_ENABLE=0</tt> across environment manifests to prevent IPC buffer deadlocks during distributed rendezvous on unified LPDDR5x memory architectures.
* ⚠️ '''Critical Errata:''' PyTorch Gloo requires a physical Linux network device name (e.g., <tt>enp1s0f0np0</tt>)[cite: 6]. Passing an IP prefix or CIDR block (such as <tt>192.168.99.</tt>) or mismatching <tt>--master-addr</tt> against the declared <tt>GLOO_SOCKET_IFNAME</tt> causes PyTorch distributed backplanes to crash or drop into infinite C++ barrier spinlocks[cite: 6]. Do not pass <tt>TP_SOCKET_IFNAME</tt> as it is an NPU/Ascend-specific variable that causes auto-detection errors on CUDA runtimes.
* '''RoCEv2 Link Layer (200Gbps ConnectX-7):'''[cite: 6]
** '''InfiniBand / HCA Target:''' <tt>rocep1s0f0</tt>[cite: 6]
** '''RoCE GID Index:''' <tt>NCCL_IB_GID_INDEX=3</tt>[cite: 6]
** '''Inter-Node Port:''' Port <tt>29500</tt> bound across <tt>192.168.99.x</tt> for PyTorch distributed tensor data streams[cite: 6].

=== 2. OpenMP Thread Fencing & CPU Scheduling Protections ===

To prevent PyTorch and vLLM background worker threads from consuming 100% of available Grace ARM CPU cores during multi-node KV cache initialization, multi-node topologies enforce strict CPU thread limits[cite: 6]:

* '''Environment Fencing:''' <tt>OMP_NUM_THREADS=16</tt> and <tt>VLLM_CPU_OMP_THREADS=16</tt> are injected into container environment manifests[cite: 6].
* '''Host System Impact:''' Restricts OpenMP thread pools to 16 cores per socket, guaranteeing sufficient CPU scheduling headroom for <tt>sshd</tt>, system daemons, and status polling threads[cite: 6].

=== 3. Self-Healing SSH Transport & Execution Timeouts ===

To prevent orphaned SSH multiplex processes or hung remote Docker daemons from freezing the orchestration control plane[cite: 6]:

* '''Automatic Socket Purging:''' Every execution automatically purges stale control sockets in both <tt>/tmp/ssh-mux-*</tt> and <tt>~/.ssh/cm-*</tt>[cite: 6].
* '''Execution Timeout Guards:''' Remote command invocations (such as <tt>docker rm -f</tt> or <tt>docker logs</tt>) are wrapped in explicit <tt>timeout 10</tt> execution guards to guarantee non-blocking return codes if a remote node experiences a GPU driver or socket lockup[cite: 6].

=== 4. Headless Target Mode & DRM Semaphore Lock Prevention ===

DGX compute nodes must '''never''' run desktop GUI display managers (<tt>gdm3</tt>, <tt>gnome-shell</tt>)[cite: 6].

* '''The Lockup Mechanism:''' When vLLM worker threads (<tt>VLLM::Worker_PP</tt>) allocate multi-node tensor buffers, they acquire exclusive writer locks on the NVIDIA GPU driver's DRM memory semaphore[cite: 6]. Background desktop processes (<tt>gnome-shell</tt>, <tt>gsd-color</tt>) requesting reader locks block indefinitely, entering an uninterruptible kernel wait state (<tt>D</tt> state) that deadlocks user-space daemons (<tt>sshd</tt>, <tt>docker</tt>) and triggers kernel OOM cascades[cite: 6].
* '''Headless Conversion (Enforced across cluster):'''[cite: 6]
<pre>
sudo systemctl set-default multi-user.target
sudo systemctl stop gdm3
</pre>
* '''Reversion / Rollback Command:''' If physical local monitor output or desktop GUI debugging is required[cite: 6]:
<pre>
sudo systemctl set-default graphical.target
sudo systemctl start gdm3
</pre>

=== 5. Passwordless Sudo Elevation Rules ===

To ensure <tt>dgx-orchestrator.py</tt> can start cold-booted Docker daemons, set GPU clock locks, and inspect kernel logs without blocking interactive pipelines for SSH passwords[cite: 6], both nodes require the following drop-in rule in <tt>/etc/sudoers.d/tetrel</tt>[cite: 6]:

<pre>
tetrel ALL=(ALL) NOPASSWD: /usr/bin/systemctl, /usr/bin/nvidia-smi, /usr/bin/journalctl
</pre>

=== 6. Ubiquiti / UniFi Gateway IDS/IPS Mitigation ===

When executing <tt>dgx-config</tt> from a host located on a different VLAN or subnet (e.g., workstation <tt>192.168.1.x</tt> -> cluster <tt>10.0.14.x</tt>), automated rapid-fire SSH multiplexing and status polling can trigger UniFi Threat Management / Suricata (IPS/IDS) SSH brute-force heuristics[cite: 6].

* '''Symptom:''' Port 22 connections abruptly hang (<tt>Connection timed out</tt> or <tt>BLOCKED</tt>) specifically from the orchestrator workstation, while other machines or local L2 nodes continue to connect without issue[cite: 6].
* '''Resolution A (Permanent Gateway Unblock):''' Navigate to the UniFi Network Dashboard -> '''System Logs''' -> '''Security Detections''', locate the event blocking your workstation IP, and select '''Allow IP / Unblock Threat'''[cite: 6].
* '''Resolution B (Transparent L2 ProxyJump Bypass):''' Tunnel SSH traffic through an unblocked node on the target L2 management subnet to bypass inter-VLAN inspection rules completely[cite: 6]. Add the following to <tt>~/.ssh/config</tt> on the orchestration workstation[cite: 6]:

<pre>
Host 10.0.14.43 spark-9dbe spark-4
    HostName 10.0.14.43
    User tetrel
    ProxyJump tetrel@10.0.14.41
</pre>

=== 7. GPU Power & Hardware Clock Locks ===

To prevent Over-Current Protection (OCP) power excursions during heavy batch inference on Grace Blackwell chips, <tt>dgx-config</tt> automatically executes hardware clock locking (<tt>nvidia-smi -lgc 300,1800</tt>) on target nodes prior to spinning up containers[cite: 6].

=== 8. vLLM Runtime Container Environment ===

* '''Base NGC Image:''' <tt>nvcr.io/nvidia/vllm:26.05.post1-py3</tt>[cite: 6]
* '''Host Driver Requirement:''' Data Center driver release 580.159+ / 595.58+ with active <tt>nvidia-fabricmanager</tt> services[cite: 6].
* '''Execution Engine:''' Deployments write environment manifests (<tt>.env</tt>) remotely and launch via <tt>docker compose -f docker-compose.[standalone|cluster].yml up -d</tt>[cite: 6].
* '''Fail-Fast Verification (V3.9.1):''' <tt>dgx-orchestrator.py</tt> verifies container instantiation 2 seconds post-launch, failing immediately if Docker rejects or drops the container instance[cite: 6].
* '''Proactive Self-Cleaning (V3.9.2):''' <tt>execute_deployment</tt> explicitly runs <tt>docker rm -f <container_name></tt> prior to deploying, preventing container naming conflicts during standalone execution[cite: 6].

----

== Installation & Setup (System Administrators) ==

To install the orchestrator so it is available to all authorized users on the system[cite: 6]:

# '''Clone the Repository to <tt>/opt</tt>:'''[cite: 6]
<pre>
sudo git clone <repo_url> /opt/dgx-cluster-control
sudo chown -R root:wheel /opt/dgx-cluster-control
sudo chmod -R 775 /opt/dgx-cluster-control
cd /opt/dgx-cluster-control
</pre>
# '''Build the Virtual Environment:'''[cite: 6]
<pre>
sudo python3 -m venv dgx-env
sudo dgx-env/bin/pip install -r requirements.txt
</pre>
# '''Create the Global Symlink:'''[cite: 6]
<pre>
sudo ln -s /opt/dgx-cluster-control/dgx-config /usr/local/bin/dgx-config
</pre>

----

== User Onboarding & SSH Key Errata (The Bootstrap Problem) ==

The orchestrator authenticates with the cluster using the '''caller's personal SSH key''' to ensure per-user auditing in the remote <tt>auth.log</tt>[cite: 6]. However, a new user cannot interact with the cluster until their public key is authorized on the Spark nodes[cite: 6].

Because manual SSH is blocked, a new user must "bootstrap" their key onto the cluster using one of two methods[cite: 6]:

=== Method A: Admin Authorization (Preferred) ===

An existing team member who already has cluster access can authorize the new user's key[cite: 6]:

<pre>
# An existing admin runs:
dgx-config authorize-key --key /home/newuser/.ssh/id_ed25519.pub
</pre>

=== Method B: The <tt>tetrel</tt> Fallback (Self-Serve) ===

If an admin is unavailable, a user in the <tt>wheel</tt> group can temporarily elevate to the <tt>tetrel</tt> service account (which already has a trusted key) to push their personal key[cite: 6]:

<pre>
# 1. Generate a key if you don't have one
ssh-keygen -t ed25519

# 2. Push your key using the tetrel service account
sudo -u tetrel /opt/dgx-cluster-control/dgx-config authorize-key --key ~/.ssh/id_ed25519.pub
</pre>

Once authorized, the user can run <tt>dgx-config</tt> natively without <tt>sudo</tt>[cite: 6].

----

== DGX Orchestrator (<tt>dgx-config</tt>) Guide ==

* '''Check Cluster Status:'''[cite: 6]
<pre>
dgx-config status
</pre>

* '''Tear Down Active Cluster Runtimes:''' ''(Must be run before pivoting models to release VRAM)''[cite: 6].
<pre>
dgx-config teardown
</pre>

* '''Deploy a Model:'''[cite: 6]
<pre>
dgx-config deploy --model <model_alias> --nodes <count>
</pre>

* '''Synchronize Compose Templates Across Nodes:'''[cite: 6]
<pre>
dgx-config sync
</pre>

* '''Stream Live Remote Logs:'''[cite: 6]
<pre>
dgx-config logs --host spark-4 --tail 100 -f
</pre>

* '''Authorize Public SSH Key:'''[cite: 6]
<pre>
dgx-config authorize-key --key ~/.ssh/id_ed25519.pub
</pre>

=== Run the API Daemon (V3.9.1+) ===

The API Daemon option exposes a FastAPI web service on port 8080[cite: 6]. This allows external dashboards or automated workflow pipelines to programmatically trigger model deployments and teardowns over HTTP without direct interactive SSH sessions[cite: 6].

<pre>
dgx-config daemon --port 8080
</pre>

* '''POST <tt>/deploy</tt>''' - JSON Payload: <tt>{"model": "qwen-3.5-122b", "nodes": 2}</tt>[cite: 6]
* '''POST <tt>/teardown</tt>''' - Flushes active topologies across nodes[cite: 6].

----

== Core Model Catalog (<tt>models.yaml</tt>) ==

When deploying, reference the exact aliases defined in <tt>/opt/dgx-cluster-control/models.yaml</tt>[cite: 6].
''Note: Model footprints and VRAM capacities are tuned for Grace Blackwell (GB10) LPDDR5x unified memory constraints.''[cite: 6]

* '''DeepSeek Architectures:'''
** <tt>deepseek-v4-flash-nvfp4</tt>: <tt>Rarri/DeepSeek-V4-Flash-0731-NVFP4</tt> (Requires <tt>--nodes 2</tt>)[cite: 6]. Quantized NVFP4 release featuring ep-weight filtering[cite: 6]. Requires <tt>--disable-custom-all-reduce</tt> for RoCEv2 backplane stability, <tt>--no-async-scheduling</tt>, <tt>NCCL_CUMEM_ENABLE=0</tt>, and <tt>--num-gpu-blocks-override 8192</tt>[cite: 6].

* '''Nemotron Architectures:'''
** <tt>nemotron-3.5-lightning</tt>: <tt>nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4</tt> (Requires <tt>--nodes 1</tt>)[cite: 6]. Agentic automation core configured with DSpark speculative decoding[cite: 6].

* '''Multimodal & Agentic Vision Architectures:'''
** <tt>muse-glimmer-30b</tt>: <tt>meta-models/Muse-Glimmer-30B</tt> (Requires <tt>--nodes 1</tt>)[cite: 6]. 30B dense vision-language model leveraging DFlash speculative configurations[cite: 6].

* '''Qwen Architectures:'''
** <tt>qwen-3.6-27b-nvfp4</tt>: <tt>nvidia/Qwen3.6-27B-NVFP4</tt> (Supports <tt>--nodes 1</tt> or <tt>--nodes 2</tt>)[cite: 6].
** <tt>qwen-3.8-27b</tt>: <tt>Qwen/Qwen3.8-27B</tt> (Requires <tt>--nodes 1</tt>)[cite: 6].
** <tt>qwen-3.5-122b</tt>: <tt>Qwen/Qwen3.5-122B-A10B-FP8</tt> (Requires <tt>--nodes 2</tt>). Deep cluster MoE OCR and document extraction engine[cite: 6]. Pinched to <tt>gpu_util: 0.85</tt> and bound with <tt>--enforce-eager</tt> to prevent unified memory bus thrashing and CUDA graph autotune deadlocks[cite: 6]. Explicitly configured with <tt>GLOO_SOCKET_IFNAME=enp1s0f0np0</tt>, <tt>NCCL_SOCKET_IFNAME=enp1s0f0np0</tt>, <tt>NCCL_CUMEM_ENABLE=0</tt>, and OpenMP thread limiters (<tt>OMP_NUM_THREADS=16</tt>). Must '''not''' set <tt>--num-gpu-blocks-override 8192</tt> due to hybrid Mamba page size requirements.

* '''Llama & Gemma Architectures:'''
** <tt>llama-3.3-70b</tt>: <tt>meta-llama/Llama-3.3-70B-Instruct</tt> (Requires <tt>--nodes 2</tt>)[cite: 6]. Sharded via pipeline parallelism (<tt>pp_size: 2</tt>) across twin 128GB DGX nodes[cite: 6]. Pinned to <tt>max_model_len: 56000</tt> to prevent memory ceiling exhaustion. The single-node definition has been permanently retired[cite: 6].
** <tt>llama-4-fp4</tt>: <tt>nvidia/Llama-4-Scout-17B-16E-Instruct-FP4</tt> (Supports <tt>--nodes 1</tt> or <tt>--nodes 2</tt>)[cite: 6].
** <tt>llama-4-fp8</tt>: <tt>nvidia/Llama-4-Scout-17B-16E-Instruct-FP8</tt> (Supports <tt>--nodes 1</tt> or <tt>--nodes 2</tt>)[cite: 6].
** <tt>gemma-4-31b</tt>: <tt>google/gemma-4-31B-it</tt> (Requires <tt>--nodes 1</tt>)[cite: 6]. Reconstruction & layout sanitizer core[cite: 6].

----

= Release Tombstones & Fix Log =

=== 20. Ascend/NPU Network Interface Variable Contamination (V3.9.8) ===

* '''The Trap:''' Injecting <tt>TP_SOCKET_IFNAME</tt> into <tt>models.yaml</tt> caused unexpected socket auto-detection fallback errors during multi-node rendezvous. <tt>TP_SOCKET_IFNAME</tt> is strictly an Ascend/NPU-specific environment variable (<tt>vllm-ascend</tt>) and is not valid for NVIDIA CUDA runtimes.
* '''The Fix:''' Completely purged <tt>TP_SOCKET_IFNAME</tt> across all topology definitions in <tt>models.yaml</tt>.

=== 19. Grace Blackwell Multi-Node <tt>cuMem</tt> Rendezvous Deadlocks (V3.9.8) ===

* '''The Trap:''' Enabling CUDA Unified Memory IPC support in NCCL (<tt>cuMem</tt>) caused inter-node process group initialization to hang indefinitely on Grace Blackwell (GB10) LPDDR5x unified memory architectures during distributed rendezvous.
* '''The Fix:''' Injected <tt>NCCL_CUMEM_ENABLE=0</tt> across all <tt>2_node</tt> topology environment variable definitions in <tt>models.yaml</tt>.

=== 18. Qwen 3.5 122B Hybrid Mamba Block Size Over-Allocation (V3.9.8) ===

* '''The Trap:''' Hardcoding <tt>--num-gpu-blocks-override 8192</tt> on Qwen 3.5 122B (which enforces a non-standard block size of 4,176 tokens per block due to hybrid Mamba/Attention layers) calculated to 24.4 million tokens (~195 GiB VRAM). This vastly exceeded available physical VRAM, causing CUDA memory allocation to thrash Linux swap space and freeze on <tt>shm_broadcast.py</tt>.
* '''The Fix:''' Removed <tt>--num-gpu-blocks-override 8192</tt> from <tt>qwen-3.5-122b</tt> in <tt>models.yaml</tt>, allowing vLLM to automatically allocate its calculated 1,700 blocks (~24 GiB VRAM).

=== 17. PyTorch Gloo Master Address Mismatch & CPU Thread Starvation (V3.9.6) ===

* '''The Trap:''' Passing <tt>--master-addr 192.168.99.2</tt> (backplane IP) while enforcing <tt>GLOO_SOCKET_IFNAME=enp1s0f0np0</tt> (management NIC <tt>10.0.14.43</tt>) caused PyTorch Gloo's C++ distributed backend to enter an infinite barrier spinlock during process registration at <tt>kv_cache_utils.py:1732</tt>[cite: 6]. Unfenced OpenMP worker threads maxed out ARM Grace CPU cores at 100% utilization, causing <tt>sshd</tt> banner exchange timeouts and user-space process hangs[cite: 6].
* '''The Fix:''' Updated <tt>dgx-orchestrator.py</tt> to resolve <tt>--master-addr</tt> to the head node management IP via <tt>head_backplane_ip, _ = resolve_management_ip(head_details)</tt>[cite: 6]. Injected <tt>OMP_NUM_THREADS=16</tt> and <tt>VLLM_CPU_OMP_THREADS=16</tt> into <tt>models.yaml</tt> environment variables to fence CPU background workers and preserve host scheduling headroom for <tt>sshd</tt>[cite: 6].

=== 16. GNOME Shell DRM Semaphore Deadlocks & Headless Mode Enforcement (V3.9.5) ===

* '''The Trap:''' Running GNOME Desktop (<tt>gdm3</tt>, <tt>gnome-shell</tt>) on DGX compute nodes caused vLLM pipeline workers (<tt>VLLM::Worker_PP</tt>) to acquire exclusive writer locks on GPU driver memory semaphores (<tt>rw-semaphore</tt>)[cite: 6]. Background desktop processes (<tt>gnome-shell</tt>, <tt>gsd-color</tt>) requesting reader locks blocked for >800 seconds, cascading into user-space <tt>D</tt>-state process hangs, <tt>sshd</tt> banner exchange timeouts, and kernel OOM killer cascades[cite: 6].
* '''The Fix:''' Permanently converted all DGX nodes to headless target mode (<tt>sudo systemctl set-default multi-user.target && sudo systemctl stop gdm3</tt>)[cite: 6]. Reclaimed ~2–4GB of host system RAM per node and completely eliminated display driver lockup vectors[cite: 6].
* '''Rollback Command:''' <tt>sudo systemctl set-default graphical.target && sudo systemctl start gdm3</tt>[cite: 6]

=== 15. The V1 Engine Async Scheduling Bug (V3.9.4) ===

* '''The Trap:''' A known bug in vLLM on Blackwell GB10 (SM 12.1) causes the engine to crash with an <tt>AttributeError</tt> (missing <tt>sampled_token_ids</tt>) the moment it receives its first inference request due to flawed async scheduling[cite: 6].
* '''The Fix:''' Injected <tt>--no-async-scheduling</tt> globally across all <tt>models.yaml</tt> topologies to force the batch queue to bypass the broken method[cite: 6].

=== 14. The Unified Memory Profiler Trap (V3.9.4) ===

* '''The Trap:''' During startup, vLLM profiles available memory to size the KV cache[cite: 6]. On the GB10's 128GB Unified Memory, the profiler falsely registers the OS's evictable page cache as free VRAM, drastically over-allocating the KV cache and pushing the entire system into Linux swap space[cite: 6].
* '''The Fix:''' Added <tt>--num-gpu-blocks-override 8192</tt> to standard <tt>vllm_args</tt> definitions to hardcode the KV cache size, completely bypassing the flawed automatic profiler[cite: 6].

=== 13. The PyTorch Expandable Segments Fatal Trap (V3.9.3) ===

* '''The Trap:''' Standard optimization guides recommend setting <tt>PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True</tt> for Grace Blackwell unified memory[cite: 6]. However, this setting is currently incompatible with vLLM's memory pool and causes EngineCore initialization to fail with a fatal <tt>SIGABRT</tt> during CUDA Graph binding and KV Cache initialization[cite: 6].
* '''The Fix:''' Ensure this variable is strictly removed across all cluster node deployments[cite: 6].

=== 12. DeepSeek RoCEv2 All-Reduce Hangs (V3.9.3) ===

* '''The Trap:''' vLLM hangs indefinitely during worker initialization on Grace Blackwell PCIe GPUs when executing custom all-reduce operations across a standard TCP/RoCEv2 network backplane without NVLink[cite: 6].
* '''The Fix:''' Enforced the <tt>--disable-custom-all-reduce</tt> flag across all twin-node architectures (including DeepSeek NVFP4 variants) in <tt>models.yaml</tt>[cite: 6].

=== 11. Proactive Standalone Self-Cleaning (V3.9.2) ===

* '''The Trap:''' Running <tt>dgx-config deploy</tt> natively failed if a stopped or crashed container from a previous deployment occupied the target container name (<tt>vllm-standalone</tt> / <tt>vllm-head</tt> / <tt>vllm-worker</tt>), causing <tt>docker compose up -d</tt> to conflict or fail silently[cite: 6].
* '''The Fix:''' Injected a proactive <tt>docker rm -f <container_name></tt> pass into <tt>dgx-orchestrator.py</tt> immediately before pushing the updated <tt>.env</tt> manifest and launching Docker Compose[cite: 6].

=== 10. The Cold Image Pull Telemetry Blindspot (V3.9.1) ===

* '''The Trap:''' Following a <tt>docker system prune</tt>, launching a deployment required pulling base image layers from NGC[cite: 6]. Because container instantiation stalled during download, readiness polling loops printed generic initialization states for up to 15 minutes, causing false assumptions of host deadlocks[cite: 6].
* '''The Fix:''' Added pre-flight image checks to identify cold image states and log active pull operations[cite: 6].

=== 9. The Llama 70B Single-Node OOM & Readiness Polling Blindspot (V3.9.1) ===

* '''The Trap:''' Attempting to deploy <tt>llama-3.3-70b</tt> on a single DGX node triggered an immediate <tt>CUDA out of memory</tt> exception during layer initialization[cite: 6]. The container crashed into an <tt>Exited (137)</tt> state[cite: 6]. However, the readiness loop continuously polled HTTP <tt>/health</tt> without inspecting container status, causing the orchestrator to hang for 25 minutes printing <tt>Initializing engine environment...</tt>[cite: 6].
* '''The Fix:''' Updated <tt>models.yaml</tt> to permanently enforce <tt>--nodes 2</tt> for <tt>llama-3.3-70b</tt> deployments[cite: 6]. Injected an active <tt>docker inspect</tt> fail-fast check inside deployment scripts to detect <tt>exited</tt> or <tt>dead</tt> container states immediately, dump the last 50 log lines to stderr, and abort execution[cite: 6]. 

=== 8. Llama-3.3 CUDA Graph Kernel Lockup ===

* '''Bug:''' Attempting a deployment of Llama-3.3-70B occasionally froze the primary node's memory bus during PyTorch Inductor autotune graph compilation, causing SSH responsiveness to completely lock[cite: 6].
* '''Fix:''' Bound the <tt>--enforce-eager</tt> flag strictly to <tt>llama-3.3-70b</tt>'s definition in <tt>models.yaml</tt> to permanently bypass CUDA graph generation[cite: 6].

=== 7. Daemon Lifecycle Management (Cold-Boot Protections) ===

* '''Bug:''' Host nodes configured to auto-start Docker occasionally fell into reboot loops when encountering corrupted runtime configurations[cite: 6].
* '''Fix:''' Docker and Containerd services were permanently disabled on boot across the cluster[cite: 6]. Added remote wakeup injections directly into <tt>dgx-orchestrator.py</tt> to ensure daemons are safely initialized via the SSH multiplexer socket prior to deployment execution[cite: 6].

=== 6. Inter-VLAN Gateway Intrusion Prevention (IDS/IPS Errata) ===

* '''Bug:''' Rapid automated SSH multiplexing across subnets triggered UniFi Gateway IPS threat rules, causing port 22 connections to silently drop for the management host[cite: 6].
* '''Fix:''' Added documentation for UniFi Threat Management exception rules alongside <tt>ProxyJump</tt> tunneling to route SSH traffic over uninspected L2 local domains[cite: 6].

=== 5. Early SSH Authentication Error Trapping ===

* '''Bug:''' <tt>ssh_mux_session</tt> context manager suppressed SSH verification failures, allowing invalid authentication loops to proceed to file transfers[cite: 6].
* '''Fix:''' Added explicit <tt>if res.returncode != 0:</tt> checking inside the multiplex setup to abort with clear diagnostics if SSH keys fail[cite: 6].

=== 4. Non-Blocking Command Execution Timeouts ===

* '''Bug:''' If a remote node experienced a Docker lock or GPU driver freeze during teardown or log inspection, the host CLI hung forever[cite: 6].
* '''Fix:''' Wrapped remote SSH command calls in <tt>timeout 10</tt> guards to ensure hard exit codes and self-healing continuity in bash orchestrators[cite: 6].

=== 3. SSH Multiplexer Self-Healing & Socket Cleanup ===

* '''Bug:''' Stale <tt>/tmp/ssh-mux-*</tt> socket files from interrupted SSH sessions caused subsequent <tt>dgx-config</tt> executions to hang indefinitely[cite: 6].
* '''Fix:''' Updated both <tt>dgx-config</tt> and <tt>dgx-orchestrator.py</tt> to purge <tt>/tmp/ssh-mux-*</tt> socket files before initiating connection attempts[cite: 6].

=== 2. NCCL Socket Interface Alignment (<tt>NCCL_SOCKET_IFNAME</tt>) ===

* '''Bug:''' Worker processes reported <tt>NCCL WARN Bootstrap : no socket interface found</tt> when initialized with IP subnet prefixes[cite: 6].
* '''Fix:''' Aligned <tt>NCCL_SOCKET_IFNAME=enp1s0f0np0</tt> across all multi-node topology definitions in <tt>models.yaml</tt>[cite: 6].

=== 1. PyTorch Gloo Device Binding Fix (<tt>GLOO_SOCKET_IFNAME</tt>) ===

* '''Bug:''' Multi-node deployments crashed during worker initialization with <tt>RuntimeError: ifa != nullptr. Unable to find address for: 192.168.99.</tt>[cite: 6].
* '''Fix:''' Replaced the IP-prefix value <tt>192.168.99.</tt> with the physical host network device <tt>enp1s0f0np0</tt> in <tt>models.yaml</tt>[cite: 6]. Gloo uses Linux <tt>getifaddrs()</tt> interface matching and requires explicit interface names[cite: 6].

----

= ⚠️ Upgrade Errata (Action Required) =

If upgrading from v3.7 or earlier, review the following breaking operational changes[cite: 6]:

# '''Gloo/NCCL Configuration Update (Critical)'''[cite: 6]
#* Existing <tt>models.yaml</tt> files containing <tt>GLOO_SOCKET_IFNAME=192.168.99.</tt> or <tt>192.168.99.0/24</tt> '''must''' be updated to <tt>GLOO_SOCKET_IFNAME=enp1s0f0np0</tt>[cite: 6].
# '''SSH Authentication & Pipeline Breakage'''[cite: 6]
#* The <tt>dgx-config</tt> wrapper no longer brokers commands through the shared <tt>tetrel</tt> account via <tt>sudo</tt>[cite: 6].
#* Executing users or cron agents ''must'' run <tt>dgx-config authorize-key</tt> to register their public SSH keys across all cluster nodes prior to starting pipelines[cite: 6].
