---

# 🚀 DGX Cluster Control Plane (`dgx-cluster-control`)

This repository contains the `dgx-config` orchestration suite. It acts as the central control plane for managing distributed vLLM deployments across a twin-node RoCEv2 NVIDIA Grace Blackwell fabric.

## 🖥️ Target Infrastructure

This tool brokers deployments to the remote inference cluster.

* **Hardware:** 2x GB10 (Grace Blackwell) DGX Sparks. Both nodes natively run the vLLM engine.


* **spark-4 (Head Node):** `10.0.14.43`. Features 128GB of Unified Memory.


* **spark-3 (Worker Node):** `10.0.14.41`. Features 128GB of Unified Memory and operates with strict headless guardrails.



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

*Once authorized, the user can run `dgx-config` natively without `sudo`.*

---

## 🛠️ DGX Orchestrator (`dgx-config`) Guide

* **Check Cluster Status:**
```bash
dgx-config status

```


* **Tear Down the Cluster:** *(Must be run before pivoting models to release VRAM)*.


```bash
dgx-config teardown

```


* **Deploy a Model:**
```bash
dgx-config deploy --model <model_alias> --nodes <count>

```



---

## 📚 Core Model Catalog (`models.yaml`)

When deploying, reference the exact aliases defined in `/opt/dgx-cluster-control/models.yaml`.

*Note: Model footprint and VRAM capacities are heavily tuned to prevent OS starvation.*

* **DeepSeek Architectures (Stable 384K Context):**
* *Warning: While these models theoretically support 1M+ context, the NVIDIA community has confirmed that pushing beyond 384K on our 2x GB10 (256GB Unified Memory) setup causes hardware freezes. The definitions below are locked to `393216` context tokens and `0.80` GPU utilization.*
* `deepseek-v4-flash-0731`: (Requires `--nodes 2`)


* `deepseek-v4-flash-nvfp4`: (Requires `--nodes 2`)


* `deepseek-v4-flash-dspark`: (Requires `--nodes 2`)




* **Agentic / Automation Cores:**
* `nemotron-3.5-lightning`: (Requires `--nodes 1`)




* **Vision / OCR Cores:**
* `qwen-3.5-122b`: (Requires `--nodes 2`)




* **Llama & Gemma Architectures:**
* `llama-3.3-70b`: Fast Distillation Core (Supports `--nodes 1` or `--nodes 2`)


* `llama-4-fp8`: Scout FP8 (Supports `--nodes 1` or `--nodes 2`)


* `gemma-4-31b`: Layout Sanitizer (Requires `--nodes 1`)

Here are the release tombstones and upgrade errata documenting the complete architectural shift we achieved tonight. You can drop these directly into your repository's `CHANGELOG.md` or release notes.

---

# 🪦 Release Tombstones: DGX Orchestrator v3.8

### 1. `dgx-orchestrator.py` (Core Engine)

* **Global Execution & Path Resolution Fix:** Replaced hardcoded `open("models.yaml")` calls with a dynamic `get_catalog()` helper. The script now safely resolves its absolute path relative to `__file__`, preventing `FileNotFoundError` crashes when executed globally via the `/usr/local/bin/` symlink.
* **Concurrency-Safe Temp Files:** The `.env` manifest payload is now written to a host-specific temporary file (`.env.tmp.{host_name}`) before SCP transfer, preventing race conditions during parallel multi-node deployments.
* **New Native SSH Key Distribution:** Added the `authorize-key` CLI command. It piggybacks on the multiplexed socket engine to securely inject local public keys into the remote nodes' `~/.ssh/authorized_keys`, checking for duplicates to maintain a clean ledger.
* **Docker Subshell Quoting Bug Fixed:** Wrapped the `docker ps --format` argument in single quotes (`'{{.Names}} ({{.Status}})'`). This prevents the remote SSH bash interpreter from evaluating the parentheses as a subshell, fixing the blank "ACTIVE RUNTIMES" dashboard bug.
* **Dynamic Dashboard Model ID Injection:** The `status` command now executes a secondary SSH command to `cat` the `.env` payload, parses it natively in Python, and dynamically appends the loaded HuggingFace model alias to the dashboard output (e.g., `➔ [DeepSeek-V4-Flash]`).
* **Dynamic Cluster Volume Mounts:** Added `CLUSTER_VOLUME_MOUNT` to the deployment string compiler, removing the dependency on hardcoded `tetrel` paths.

### 2. `dgx-config` (Bash Wrapper)

* **Execution Paradigm Shift:** Disabled the `sudo -u tetrel` elevation layer. The wrapper now defaults to executing as the calling user, enforcing per-user SSH key authentication for strict auditing.
* **VENV Error Trapping:** Added explicit, copy-pasteable terminal instructions for rebuilding the virtual environment if the `dgx-env/bin/python` binary is missing.
* **Directory Context Switching:** Enforced `cd "$SCRIPT_DIR"` prior to execution to ensure relative `.secrets` files resolve correctly.

### 3. `models.yaml` (Hardware Topology Catalog)

* **DeepSeek 2x GB10 Hardware Limits Enforced:**
* Reduced DeepSeek V4 Flash context windows (`max_model_len`) from `1048576` (1M) to the community-validated `393216` (384K) to prevent unified memory exhaustion and hard hardware freezes on the 256GB cluster.
* Dropped DeepSeek `gpu_util` to `0.80` and explicitly capped `--max-num-seqs` to `4` to protect host OS memory margins.



### 4. `docker-compose.cluster.yml`

* **Volume Decoupling:** Removed the hardcoded `/home/tetrel/.cache/...` volume mount string. Replaced it with `${CLUSTER_VOLUME_MOUNT}`, bringing it to parity with the standalone compose file and allowing dynamic injection from `models.yaml`.

---

# ⚠️ Upgrade Errata (Action Required)

If you are upgrading an existing ingestion workstation or pipeline from v3.7 or earlier, please review the following breaking changes:

**1. SSH Authentication & Pipeline Breakage (Critical)**
The `dgx-config` wrapper no longer brokers commands through the shared `tetrel` account via `sudo`.

* **Impact:** Any user, cron job, or automated ingestion script (e.g., `master_pipeline.py`) that attempts to run a `deploy`, `status`, or `teardown` command will fail with `Permission denied (publickey)` if they have not provisioned their own SSH keys.
* **Remediation:** Before starting the ingestion pipeline, the executing user *must* run `dgx-config authorize-key` to push their public key to the Spark cluster.

**2. CLI Output Parsing (Regex Breakage)**
The standard output of `dgx-config status` has been enriched.

* **Impact:** Legacy scripts utilizing rigid Regex anchored to the end of the line (e.g., `(Up \d+ hours)$`) to verify container health will fail.
* **Remediation:** Update scraping logic to account for the new appended model string (e.g., `(Up 2 days) ➔ [llama-3.3-70b-instruct]`).

**3. DeepSeek Context Length Truncation**

* **Impact:** Due to hardware constraints on the dual Grace Blackwell nodes, DeepSeek requests exceeding 384,000 tokens will now be rejected by the vLLM engine at the API level (previously, they would execute and subsequently hard-lock the machine).
* **Remediation:** Ensure your prompt chunking logic limits context windows to `< 384K` before transmission to the cluster.