"""
Typed access to cluster_config.yaml.

This module is the single source of truth for cluster host inventory,
replacing the hardcoded HOSTS dicts previously duplicated across
dgx-orchestrator.py, cache_cluster_assets.py, and benchmark.py.

Nothing in this module enforces gpu_util_ceiling; it is carried as data
only, per the task's constraints.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, ValidationError

# --- Path resolution (mirrors the BASE_DIR pattern used by the existing
# scripts, e.g. dgx-orchestrator.py: Path(os.getenv("BASE_DIR", <repo root>))).
# From common/config.py, the repo root is one level up from this file's dir.
BASE_DIR = Path(os.getenv("BASE_DIR", Path(__file__).resolve().parent.parent))
CLUSTER_CONFIG_PATH = BASE_DIR / "cluster_config.yaml"


class HostConfig(BaseModel):
    alias: str
    role: str
    management_ip: str
    backplane_ip: str
    volume_mount: str
    active: bool = True


class NetworkConfig(BaseModel):
    topology: str
    interface: str
    nccl_ib_hca: str


class ClusterConfig(BaseModel):
    ssh_user: str
    ssh_key_name: str
    default_image: str
    gpu_util_ceiling: float
    ports: dict[str, int]
    container_names: dict[str, str]
    hosts: dict[str, HostConfig]
    network: NetworkConfig
    # Cluster-wide offline-mode switches, toggled by /api/toggle-network.
    # Injected into every recipe topology's env_vars at catalog-build time
    # by common/recipes.py::build_catalog_response() -- never per-model.
    global_hf_hub_offline: int = 0
    global_transformers_offline: int = 0


@functools.lru_cache(maxsize=None)
def load_cluster_config(path: Optional[Path] = None) -> ClusterConfig:
    """
    Load and validate cluster_config.yaml.

    Cached per-process (keyed on `path`) so the file is read once. Pass an
    explicit `path` (e.g. in tests) to load a different file; the default
    (None) resolves to CLUSTER_CONFIG_PATH.

    Raises FileNotFoundError or ValueError naming the file path and the
    specific problem on failure. Never falls back to defaults.
    """
    config_path = Path(path) if path is not None else CLUSTER_CONFIG_PATH

    if not config_path.is_file():
        raise FileNotFoundError(
            f"Cluster config file not found: {config_path}"
        )

    try:
        raw_text = config_path.read_text()
    except OSError as exc:
        raise OSError(f"Could not read cluster config file {config_path}: {exc}") from exc

    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ValueError(
            f"Cluster config file {config_path} is not valid YAML: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise ValueError(
            f"Cluster config file {config_path} must contain a YAML mapping "
            f"at the top level, got {type(data).__name__}"
        )

    try:
        return ClusterConfig(**data)
    except ValidationError as exc:
        raise ValueError(
            f"Cluster config file {config_path} failed validation: {exc}"
        ) from exc


def active_hosts(path: Optional[Path] = None) -> dict[str, HostConfig]:
    """Return only hosts where active is true."""
    cfg = load_cluster_config(path)
    return {name: host for name, host in cfg.hosts.items() if host.active}


def legacy_hosts_dict(path: Optional[Path] = None) -> dict[str, dict]:
    """
    Backwards-compatibility shim matching the shape of the hardcoded HOSTS
    dict in dgx-orchestrator.py:

        {"spark-4": {"ip": ..., "alias": ..., "role": ...}, ...}

    Built from active hosts only, with `ip` sourced from `management_ip`.
    """
    return {
        name: {"ip": host.management_ip, "alias": host.alias, "role": host.role}
        for name, host in active_hosts(path).items()
    }
