This repository contains the `dgx-config` orchestration suite for managing vLLM deployments across the twin-node NVIDIA Grace Blackwell fabric.

== Architecture ==

* '''`maestro`''': The control station. Runs the Docker compose stack for the web dashboard and API endpoints.
* '''`spark-4`''': Head compute node (10.0.14.43).
* '''`spark-3`''': Worker compute node (10.0.14.41).

Models are cached on the host at `/home/tetrel/.cache/huggingface` to prevent re-downloads.

'''Model Recipes:''' Model configuration lives in `recipes/local/*.yaml` and `recipes/eugr/*.yaml` — one file per model, not a single shared `models.yaml` (that file still exists as a rollback fallback only — see `README.md`). Some recipes are adapted from the community `eugr/spark-vllm-docker` project; `tools/translate_eugr_recipes.py` handles the mechanical part of that conversion, with anything genuinely ambiguous (an unmapped container image, a non-numeric context length) flagged for a human rather than guessed. See `../README.md` and `EUGR-REFERENCE-NOTES.md` for the full detail — this file stays a setup guide, not an architecture reference.

== Core Components ==

* `dgx-orchestrator.py`: The main API and daemon.
* `dgx-config`: A wrapper script that routes commands into the container.
* `common/`: shared config loading (`cluster_config.yaml`), recipe loading, and SSH plumbing — imported by everything below rather than duplicated in each script.
* `index.html` (in `html/`): The web dashboard.
* `cache_cluster_assets.py`: Offline asset pre-fetcher. Reads the live recipe catalog and parallel-downloads Docker images and HuggingFace safetensors to both DGX nodes for air-gapped capabilities.
* `benchmark.py`: MTP-aware streaming benchmark tool. Accurately times Time-To-First-Token (TTFT) and Decode tokens/second, specifically hardened against vLLM Multi-Token Prediction stream buffering. Results are appended to `benchmark_ledger.csv`.

== Network Configuration ==

* '''Management:''' 10.0.14.x subnet for SSH, Gloo, and the `--master-addr`/`--master-port` rendezvous (confirmed against the deploy code — this rides the management network, not the RoCE fabric).
* '''RoCEv2:''' ConnectX-7 (`rocep1s0f0`) for the actual NCCL tensor/all-reduce traffic, steered there via `NCCL_SOCKET_IFNAME` / `NCCL_IB_HCA` / `NCCL_IB_GID_INDEX` env vars.

Host/port/network values all live in `cluster_config.yaml` at the repo root, not hardcoded in the Python — see `README.md`'s "Configuration" section if you need to change any of them. Host identity throughout `dgx-orchestrator.py` (`PRIMARY_HOST`/`SECONDARY_HOST`/`PRIMARY_HOST_IP`) is now derived from this file rather than hardcoded, so pointing the orchestrator at a different host pair is a config change, not a code change — see `docs/TOMBSTONES.md` #73 for the history and `README.md`'s host mapping section for the current pair.

After editing and redeploying `dgx-orchestrator.py`, confirm the new code actually landed before trusting it: check the version badge next to Server Time on the dashboard, or `orchestrator_version` in `dgx-config status` / `/api/status`, against what you expect. A forgotten `git push` before `git pull`-ing on `maestro` silently leaves the daemon running the old code with no error surfaced anywhere — this has happened for real (see `docs/TOMBSTONES.md` #65).

== Setup ==

1. Clone the repo:

mkdir -p ~/docker && cd ~/docker
git clone https://github.com/imrobertson/orchestrator.git
cd orchestrator

2. Configure Essential Secrets (`HF_TOKEN`):
The orchestrator requires a HuggingFace authentication token to pull gated models and tokenizer configurations. See `.secrets.example` for the expected format.

echo 'HF_TOKEN="hf_your_actual_token_here"' > .secrets
chmod 600 .secrets

3. Stage the Master Orchestrator Key:
The control plane requires the shared SSH service key to execute remote Docker commands.

mv /path/to/id_dgx_orchestrator .
chmod 600 id_dgx_orchestrator

4. Run `docker compose up -d --build`.
5. Symlink the wrapper:

sudo ln -sf ~/docker/orchestrator/dgx-config /usr/local/bin/dgx-config

6. User Onboarding (Personal SSH Keys):
If operating locally on `maestro` (bypassing Tailscale SSO), users must authorize their personal SSH keys so the orchestrator can reach `spark-3`/`spark-4` on their behalf:

dgx-config authorize-key --key ~/.ssh/id_ed25519.pub

Note this authorizes the key for cluster access — it does not make per-deploy attribution cryptographically verified. See `USERMANUAL.md`'s Troubleshooting section if that distinction matters for your use case.
