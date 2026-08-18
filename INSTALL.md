This repository contains the `dgx-config` orchestration suite for managing vLLM deployments across the twin-node NVIDIA Grace Blackwell fabric.

== Architecture ==

* '''`maestro`''': The control station. Runs the Docker compose stack for the web dashboard and API endpoints.
* '''`spark-4`''': Head compute node (10.0.14.43).
* '''`spark-3`''': Worker compute node (10.0.14.41).

Models are cached on the host at `/home/tetrel/.cache/huggingface` to prevent re-downloads.

'''Model Recipes:''' Configurations and image tags inside `models.yaml` are actively informed by upstream GitHub model recipes. We are converging our custom architectures with these upstream project recipes to allow for verbatim adoption in the future.

== Core Components ==

* `dgx-orchestrator.py`: The main API and daemon.
* `dgx-config`: A wrapper script that routes commands into the container.
* `index.html`: The web dashboard.
* `cache_cluster_assets.py`: Offline asset pre-fetcher. Parses `models.yaml` and parallel-downloads Docker images and HuggingFace safetensors to both DGX nodes for air-gapped capabilities.
* `benchmark.py`: MTP-aware streaming benchmark tool. Accurately times Time-To-First-Token (TTFT) and Decode tokens/second, specifically hardened against vLLM Multi-Token Prediction stream buffering. Results are appended to `benchmark_ledger.csv`.

== Network Configuration ==

* '''Management:''' 10.0.14.x subnet for SSH and Gloo.
* '''RoCEv2:''' ConnectX-7 (`rocep1s0f0`) for PyTorch tensor streams.

== Setup ==

1. Clone the repo to `/opt/dgx-cluster-control`:

sudo git clone [https://github.com/tetrelsec/dgx-cluster-control.git](https://github.com/tetrelsec/dgx-cluster-control.git) /opt/dgx-cluster-control
sudo chown -R tetrel:wheel /opt/dgx-cluster-control
cd /opt/dgx-cluster-control

2. Configure Essential Secrets (`HF_TOKEN`):
The orchestrator requires a HuggingFace authentication token to pull gated models and tokenizer configurations.

echo 'HF_TOKEN="hf_your_actual_token_here"' > /opt/dgx-cluster-control/.secrets
sudo chown tetrel:wheel /opt/dgx-cluster-control/.secrets
sudo chmod 600 /opt/dgx-cluster-control/.secrets

3. Stage the Master Orchestrator Key:
The control plane requires the shared SSH service key to execute remote Docker commands.

sudo mv /path/to/id_dgx_orchestrator /opt/dgx-cluster-control/
sudo chown tetrel:wheel /opt/dgx-cluster-control/id_dgx_orchestrator
sudo chmod 640 /opt/dgx-cluster-control/id_dgx_orchestrator

4. Run `docker compose up -d --build`.
5. Symlink the wrapper:

sudo ln -sf /opt/dgx-cluster-control/dgx-config /usr/local/bin/dgx-config

6. User Onboarding (Personal SSH Keys):
If operating locally on `maestro` (bypassing Tailscale SSO), users must authorize their personal SSH keys so the orchestrator can audit deployments under their specific identity:

sudo -u tetrel dgx-config authorize-key --key ~/.ssh/id_ed25519.pub