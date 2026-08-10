# DGX Cluster Control Plane

Central orchestration repository for managing twin NVIDIA Grace Blackwell DGX Spark nodes, model deployments, readiness polling, network socket maintenance, and hardware frequency locking.

## Quick Usage
- `./cli/dgx-control status` - Check active cluster state
- `./cli/dgx-control pivot <model_key>` - Idempotently load/switch active VLM/LLM
- `./cli/dgx-control lock-clocks` - Enforce GPU hardware frequency locks
