# 🚀 DGX Cluster Control Plane (`dgx-cluster-control`) — V3.8 Hardened Edition

This repository contains the `dgx-config` orchestration suite. It acts as the central control plane for managing distributed vLLM deployments across a twin-node RoCEv2 NVIDIA Grace Blackwell fabric.

---

## 🖥️ Target Infrastructure

This tool brokers deployments to the remote inference cluster.

* **Hardware:** 2x GB10 (Grace Blackwell) DGX Sparks. Both nodes natively run the vLLM engine.


* **spark-4 (Head Node):** `10.0.14.43` (`spark-9dbe`). Features 128GB of Unified Memory.


* **spark-3 (Worker Node):** `10.0.14.41` (`spark-6e63`). Features 128GB of Unified Memory and operates with strict headless guardrails.



---

## 🔬 Low-Level Network Fabric & Hardware Safeguards

### 1. Network Interface Targets & Gloo/NCCL Bindings

* **Management TCP Interface (`enp1s0f0np0`):** All SSH orchestration, administrative commands, and `dgx-config` calls route strictly across the management subnet (`10.0.14.x`).


* **Gloo & NCCL Interface Binding (`enp1s0f0np0`):** Multi-node distributed topologies **must** pass `GLOO_SOCKET_IFNAME=enp1s0f0np0` and `NCCL_SOCKET_IFNAME=enp1s0f0np0`.


* *⚠️ Critical Errata:* PyTorch Gloo requires a physical Linux network device name (e.g., `enp1s0f0np0`). Passing an IP prefix or CIDR block (such as `192.168.99.`) causes `ProcessGroupGloo` to crash with `RuntimeError: ifa != nullptr`.




* **RoCEv2 Link Layer (200Gbps ConnectX-7):**
* **InfiniBand / HCA Target:** `rocep1s0f0`

* **RoCE GID Index:** `NCCL_IB_GID_INDEX=3`

* **Inter-Node Port:** Port `29500` bound across `192.168.99.x` for PyTorch distributed backplanes.





### 2. Self-Healing SSH Transport & Execution Timeouts

To prevent orphaned SSH multiplex processes or hung remote Docker daemons from freezing the orchestration control plane:

* **Automatic Socket Purging:** Every execution automatically purges stale control sockets in both `/tmp/ssh-mux-*` and `~/.ssh/cm-*`.


* **Execution Timeout Guards:** Remote command invocations (such as `docker rm -f` or `docker logs`) are wrapped in explicit `timeout 10` execution guards to guarantee non-blocking return codes if a remote node experiences a GPU driver or socket lockup.



### 3. Ubiquiti / UniFi Gateway IDS/IPS Mitigation

When executing `dgx-config` from a host located on a different VLAN or subnet (e.g., workstation `192.168.1.x` $\rightarrow$ cluster `10.0.14.x`), automated rapid-fire SSH multiplexing and status polling can trigger UniFi Threat Management / Suricata (IPS/IDS) SSH brute-force heuristics.

* **Symptom:** Port 22 connections abruptly hang (`Connection timed out` or `BLOCKED`) specifically from the orchestrator workstation, while other machines or local L2 nodes continue to connect without issue.
* **Resolution A (Permanent Gateway Unblock):** Navigate to the UniFi Network Dashboard $\rightarrow$ **System Logs** $\rightarrow$ **Security Detections**, locate the event blocking your workstation IP, and select **Allow IP / Unblock Threat**.
* **Resolution B (Transparent L2 ProxyJump Bypass):** Tunnel SSH traffic through an unblocked node on the target L2 management subnet to bypass inter-VLAN inspection rules completely. Add the following to `~/.ssh/config` on the orchestration workstation:

```ssh
Host 10.0.14.43 spark-9dbe spark-4
    HostName 10.0.14.43
    User tetrel
    ProxyJump tetrel@10.0.14.41

```

### 4. GPU Power & Hardware Clock Locks

To prevent Over-Current Protection (OCP) power excursions during heavy batch inference on Grace Blackwell chips, `dgx-config` automatically executes hardware clock locking (`nvidia-smi -lgc 300,1800`) on target nodes prior to spinning up containers.

### 5. vLLM Runtime Container Environment

* **Base NGC Image:** `nvcr.io/nvidia/vllm:26.05.post1-py3`

* **Host Driver Requirement:** Data Center driver release 580.159+ / 595.58+ with active `nvidia-fabricmanager` services.


* **Command Pass-through:** Container command strings explicitly prepend `vllm serve` to bypass NGC shell entrypoint parsing limits.



---

## ⚙️ Installation & Setup (System Administrators)

To install the orchestrator so it is available to all authorized users on the system:

1. **Clone the Repository to `/opt`:**

```bash
sudo git clone <repo_url> /opt/dgx-cluster-control
sudo chown -R root:wheel /opt/dgx-cluster-control
sudo chmod -R 775 /opt/dgx-cluster-control
cd /opt/dgx-cluster-control

```

2. **Build the Virtual Environment:**

```bash
sudo python3 -m venv dgx-env
sudo dgx-env/bin/pip install -r requirements.txt

```

3. **Create the Global Symlink:**

```bash
sudo ln -s /opt/dgx-cluster-control/dgx-config /usr/local/bin/dgx-config

```

---

## 🔑 User Onboarding & SSH Key Errata (The Bootstrap Problem)

The orchestrator authenticates with the cluster using the **caller's personal SSH key** to ensure per-user auditing in the remote `auth.log`. However, a new user cannot interact with the cluster until their public key is authorized on the Spark nodes.

Because manual SSH is blocked, a new user must "bootstrap" their key onto the cluster using one of two methods:

### Method A: Admin Authorization (Preferred)

An existing team member who already has cluster access can authorize the new user's key:

```bash
# An existing admin runs:
dgx-config authorize-key --key /home/newuser/.ssh/id_ed25519.pub

```

### Method B: The `tetrel` Fallback (Self-Serve)

If an admin is unavailable, a user in the `wheel` group can temporarily elevate to the `tetrel` service account (which already has a trusted key) to push their personal key:

```bash
# 1. Generate a key if you don't have one
ssh-keygen -t ed25519

# 2. Push your key using the tetrel service account
sudo -u tetrel /opt/dgx-cluster-control/dgx-config authorize-key --key ~/.ssh/id_ed25519.pub

```

Once authorized, the user can run `dgx-config` natively without `sudo`.

---

## 🛠️ DGX Orchestrator (`dgx-config`) Guide

* **Check Cluster Status:**

```bash
dgx-config status

```

* **Tear Down Active Cluster Runtimes:** *(Must be run before pivoting models to release VRAM)*.



```bash
dgx-config teardown

```

* **Deploy a Model:**

```bash
dgx-config deploy --model <model_alias> --nodes <count>

```

* **Synchronize Compose Templates Across Nodes:**

```bash
dgx-config sync

```

* **Stream Live Remote Logs:**

```bash
dgx-config logs --host spark-4 --tail 100 -f

```

---

## 📚 Core Model Catalog (`models.yaml`)

When deploying, reference the exact aliases defined in `/opt/dgx-cluster-control/models.yaml`.

Note: Model footprints and VRAM capacities are tuned for Grace Blackwell (GB10) LPDDR5x unified memory constraints.

* **DeepSeek Architectures:**
* `deepseek-v4-pro`: `deepseek-ai/DeepSeek-V4-Pro` (Requires `--nodes 2`). Multi-node MoE pipeline parallel topology using `GLOO_SOCKET_IFNAME=enp1s0f0np0`.


* `deepseek-v4-flash`: `deepseek-ai/DeepSeek-V4-Flash` (Requires `--nodes 1`). Single-node high-throughput inference engine.




* **Qwen Architectures:**
* `qwen-3.5-122b`: `Qwen/Qwen3.5-122B-A10B-FP8` (Requires `--nodes 2`). Deep cluster MoE OCR and document extraction engine. Explicitly configured with `GLOO_SOCKET_IFNAME=enp1s0f0np0` and `NCCL_SOCKET_IFNAME=enp1s0f0np0`.




* **Llama & Gemma Architectures:**
* `llama-3.3-70b`: `meta-llama/Llama-3.3-70B-Instruct` (Supports `--nodes 1` or `--nodes 2`). Optimized via chunked prefill (`--enable-chunked-prefill`), FP8 KV cache, and CUDA graph compilation.


* `llama-4-fp4`: `nvidia/Llama-4-Scout-17B-16E-Instruct-FP4` (Supports `--nodes 1` or `--nodes 2`). Ultra-fast single-node frontmatter & metadata extraction core.


* `llama-4-fp8`: `nvidia/Llama-4-Scout-17B-16E-Instruct-FP8` (Supports `--nodes 1` or `--nodes 2`).


* `gemma-4-31b`: `google/gemma-4-31B-it` (Requires `--nodes 1`). Reconstruction & layout sanitizer core.





---

# 🪦 Release Tombstones & Fix Log: DGX Orchestrator v3.8 Hardened

### 1. PyTorch Gloo Device Binding Fix (`GLOO_SOCKET_IFNAME`)

* **Bug:** Multi-node deployments crashed during worker initialization with `RuntimeError: ifa != nullptr. Unable to find address for: 192.168.99.`.


* **Fix:** Replaced the IP-prefix value `192.168.99.` with the physical host network device `enp1s0f0np0` in `models.yaml`. Gloo uses Linux `getifaddrs()` interface matching and requires explicit interface names.



### 2. NCCL Socket Interface Alignment (`NCCL_SOCKET_IFNAME`)

* **Bug:** Worker processes reported `NCCL WARN Bootstrap : no socket interface found` when initialized with IP subnet prefixes.


* **Fix:** Aligned `NCCL_SOCKET_IFNAME=enp1s0f0np0` across all multi-node topology definitions in `models.yaml`.



### 3. SSH Multiplexer Self-Healing & Socket Cleanup

* **Bug:** Stale `/tmp/ssh-mux-*` socket files from interrupted SSH sessions caused subsequent `dgx-config` executions to hang indefinitely.


* **Fix:** Updated both `dgx-config` and `dgx-orchestrator.py` to purge `/tmp/ssh-mux-*` socket files before initiating connection attempts.



### 4. Non-Blocking Command Execution Timeouts

* **Bug:** If a remote node experienced a Docker lock or GPU driver freeze during teardown or log inspection, the host CLI hung forever.


* **Fix:** Wrapped remote SSH command calls in `timeout 10` guards to ensure hard exit codes and self-healing continuity in bash orchestrators.



### 5. Early SSH Authentication Error Trapping

* **Bug:** `ssh_mux_session` context manager suppressed SSH verification failures, allowing invalid authentication loops to proceed to file transfers.


* **Fix:** Added explicit `if res.returncode != 0:` checking inside the multiplex setup to abort with clear diagnostics if SSH keys fail.



### 6. Inter-VLAN Gateway Intrusion Prevention (IDS/IPS Errata)

* **Bug:** Rapid automated SSH multiplexing across subnets triggered UniFi Gateway IPS threat rules, causing port 22 connections to silently drop for the management host.
* **Fix:** Added documentation for UniFi Threat Management exception rules alongside `ProxyJump` tunneling to route SSH traffic over uninspected L2 local domains.

---

# ⚠️ Upgrade Errata (Action Required)

If upgrading from v3.7 or earlier, review the following breaking operational changes:

1. **Gloo/NCCL Configuration Update (Critical)**
* Existing `models.yaml` files containing `GLOO_SOCKET_IFNAME=192.168.99.` or `192.168.99.0/24` **must** be updated to `GLOO_SOCKET_IFNAME=enp1s0f0np0`.




2. **SSH Authentication & Pipeline Breakage**
* The `dgx-config` wrapper no longer brokers commands through the shared `tetrel` account via `sudo`.


* Executing users or cron agents *must* run `dgx-config authorize-key` to register their public SSH keys across all cluster nodes prior to starting pipelines.